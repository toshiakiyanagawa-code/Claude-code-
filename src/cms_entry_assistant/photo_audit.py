"""Generate a visual audit report for photo suggestion quality.

The report compares:
  - photos actually used in matched President Online articles
  - current rule-based iStock search suggestions
  - cached iStock candidate thumbnails when available

It is intentionally static HTML so editors can open it locally and mark obvious
misses without running the web app.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path

from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_docx, parse_text
from cms_entry_assistant.instruction_parser import derive_from_manuscript
from cms_entry_assistant.istock_crawler import IstockSearchHit, crawl_search
from cms_entry_assistant.models import IstockSearchSuggestion
from cms_entry_assistant.photo_preferences import PreferencesStore, UsageHistory, rank_hits


@dataclass
class AuditStats:
    manuscripts_total: int = 0
    matched_articles: int = 0
    unmatched_articles: int = 0
    suggestions_total: int = 0
    suggestions_with_cached_hits: int = 0
    cache_misses: int = 0
    refreshed_queries: int = 0


@dataclass
class AuditCandidate:
    suggestion: IstockSearchSuggestion
    hits: list[IstockSearchHit] = field(default_factory=list)
    cache_hit: bool = False


@dataclass
class AuditArticle:
    file_name: str
    title: str
    url: str = ""
    published_title: str = ""
    actual_images: list[dict] = field(default_factory=list)
    candidates: list[AuditCandidate] = field(default_factory=list)


@dataclass
class AuditReport:
    stats: AuditStats
    articles: list[AuditArticle]


def build_photo_audit(
    manuscripts_dir: Path | str = Path("data/manuscripts"),
    *,
    published_path: Path | str = Path("data/published_articles.json"),
    cache_path: Path | str = Path("data/istock_search_cache.json"),
    preferences_path: Path | str = Path("data/photo_preferences.json"),
    history_path: Path | str = Path("data/photo_usage_history.json"),
    article_limit: int = 0,
    slots_per_article: int = 4,
    hits_per_slot: int = 5,
    refresh_missing: bool = False,
    max_refresh: int = 0,
) -> AuditReport:
    """Build an in-memory audit report.

    `refresh_missing` is intentionally opt-in because a full corpus can require
    many iStock requests. `max_refresh` caps the number of cache misses fetched
    in one run; 0 means no cap when refresh is enabled.
    """

    manuscripts_dir = Path(manuscripts_dir)
    published = _load_json(Path(published_path))
    cache_path = Path(cache_path)
    cache = _load_json(cache_path)
    stats = AuditStats()
    articles: list[AuditArticle] = []
    prefs = PreferencesStore(preferences_path)
    history = UsageHistory(history_path)
    refreshed = 0
    refreshed_keys: set[str] = set()

    manuscript_paths = sorted(
        [
            *manuscripts_dir.glob("*.docx"),
            *manuscripts_dir.glob("*.txt"),
            *manuscripts_dir.glob("*.md"),
        ]
    )
    stats.manuscripts_total = len(manuscript_paths)

    for path in manuscript_paths:
        published_record = published.get(path.name, {})
        if not published_record or published_record.get("error") or "pages" not in published_record:
            stats.unmatched_articles += 1
            continue
        if article_limit and len(articles) >= article_limit:
            continue

        manuscript = _parse_manuscript(path)
        draft = convert(
            manuscript,
            derive_from_manuscript(manuscript),
            config=ConversionConfig(allow_network=False),
        )
        suggestions = draft.photo_suggestions[:slots_per_article]
        candidates: list[AuditCandidate] = []
        for suggestion in suggestions:
            stats.suggestions_total += 1
            hits, cache_hit = _hits_for_suggestion(
                suggestion,
                cache=cache,
                prefs=prefs,
                history=history,
                limit=hits_per_slot,
            )
            cache_key = _cache_key(suggestion.query_ja)
            if (
                not cache_hit
                and refresh_missing
                and cache_key not in refreshed_keys
                and (max_refresh <= 0 or refreshed < max_refresh)
            ):
                refreshed_keys.add(cache_key)
                refreshed_hits = crawl_search(
                    suggestion.query_ja,
                    limit=hits_per_slot,
                    cache_path=cache_path,
                    force_refresh=False,
                )
                refreshed += 1
                stats.refreshed_queries += 1
                cache = _load_json(cache_path)
                hits, cache_hit = _hits_for_suggestion(
                    suggestion,
                    cache=cache,
                    prefs=prefs,
                    history=history,
                    limit=hits_per_slot,
                )
            if cache_hit:
                stats.suggestions_with_cached_hits += 1
            else:
                stats.cache_misses += 1
            candidates.append(AuditCandidate(suggestion=suggestion, hits=hits, cache_hit=cache_hit))

        actual_images = _flatten_actual_images(published_record)
        articles.append(
            AuditArticle(
                file_name=path.name,
                title=draft.selected_title or published_record.get("manuscript_title", ""),
                url=published_record.get("url", ""),
                published_title=published_record.get("published_title", ""),
                actual_images=actual_images,
                candidates=candidates,
            )
        )
        stats.matched_articles += 1

    return AuditReport(stats=stats, articles=articles)


def render_photo_audit_html(report: AuditReport) -> str:
    stats = report.stats
    parts = [
        "<!doctype html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>CMS Photo Candidate Audit</title>",
        "<style>",
        _CSS,
        "</style>",
        "</head>",
        "<body>",
        "<header>",
        "<h1>CMS Photo Candidate Audit</h1>",
        "<p>公開済み記事の実写真と、現行ツールの写真候補を横並びで確認する監査レポートです。</p>",
        '<div class="stats">',
        _stat("原稿", stats.manuscripts_total),
        _stat("公開記事一致", stats.matched_articles),
        _stat("一致なし", stats.unmatched_articles),
        _stat("候補枠", stats.suggestions_total),
        _stat("キャッシュあり", stats.suggestions_with_cached_hits),
        _stat("未取得", stats.cache_misses),
        _stat("今回取得", stats.refreshed_queries),
        "</div>",
        "</header>",
        "<main>",
    ]

    for article in report.articles:
        parts.append(_article_html(article))

    parts.extend(["</main>", "</body>", "</html>"])
    return "\n".join(parts)


def write_photo_audit_html(report: AuditReport, out_path: Path | str) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_photo_audit_html(report), encoding="utf-8")
    return out_path


def _parse_manuscript(path: Path):
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    if path.suffix.lower() in {".txt", ".md"}:
        return parse_text(path)
    raise ValueError(f"unsupported manuscript format: {path.suffix}")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _cache_key(query: str) -> str:
    return (query or "").strip().lower()


def _hits_for_suggestion(
    suggestion: IstockSearchSuggestion,
    *,
    cache: dict,
    prefs: PreferencesStore,
    history: UsageHistory,
    limit: int,
) -> tuple[list[IstockSearchHit], bool]:
    entry = cache.get(_cache_key(suggestion.query_ja))
    if not entry or not entry.get("hits"):
        return [], False
    hits = [IstockSearchHit(**hit) for hit in entry.get("hits", [])]
    ranked = rank_hits(
        hits,
        preferences=prefs,
        history=history,
        limit=limit,
        query_context=suggestion.query_ja,
    )
    return ranked, True


def _flatten_actual_images(article: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page in article.get("pages", []):
        for img in page.get("images", []):
            src = img.get("src", "")
            if not src or src in seen:
                continue
            seen.add(src)
            out.append({**img, "page": page.get("page", 1)})
    return out


def _article_html(article: AuditArticle) -> str:
    title = _esc(article.title or article.published_title or article.file_name)
    url = _esc(article.url)
    link = f' <a href="{url}" target="_blank" rel="noreferrer">公開記事</a>' if url else ""
    parts = [
        '<section class="article">',
        f"<h2>{title}</h2>",
        f'<p class="meta">{_esc(article.file_name)}{link}</p>',
        '<div class="compare">',
        '<section class="panel actual">',
        "<h3>実際の記事写真</h3>",
        '<div class="image-grid">',
    ]
    if article.actual_images:
        for img in article.actual_images:
            parts.append(_actual_image_html(img))
    else:
        parts.append('<p class="empty">公開記事写真なし</p>')
    parts.extend(
        [
            "</div>",
            "</section>",
            '<section class="panel candidates">',
            "<h3>現行ツールの候補</h3>",
        ]
    )
    for candidate in article.candidates:
        parts.append(_candidate_html(candidate))
    parts.extend(["</section>", "</div>", "</section>"])
    return "\n".join(parts)


def _actual_image_html(img: dict) -> str:
    src = _esc(img.get("src", ""))
    caption = _esc(img.get("caption") or img.get("alt") or "")
    source = _esc(img.get("source", ""))
    role = _esc(img.get("role", ""))
    page = _esc(str(img.get("page", "")))
    return (
        '<figure class="image-card">'
        f'<img src="{src}" alt="{caption}" loading="lazy">'
        f"<figcaption><b>{role or 'image'} p{page}</b><br>{caption}<br>"
        f'<span class="muted">{source}</span></figcaption>'
        "</figure>"
    )


def _candidate_html(candidate: AuditCandidate) -> str:
    suggestion = candidate.suggestion
    search_url = _esc(suggestion.search_url_ja)
    parts = [
        '<article class="slot">',
        f"<h4>{_esc(suggestion.slot_label)} <span>[{_esc(suggestion.type_code)}] {_esc(suggestion.type_label)}</span></h4>",
        f'<p class="query">{_esc(suggestion.query_ja)} <a href="{search_url}" target="_blank" rel="noreferrer">検索</a></p>',
        f'<p class="reason">{_esc(suggestion.rationale)}</p>',
    ]
    if suggestion.note:
        parts.append(f'<p class="note">{_esc(suggestion.note)}</p>')
    if not candidate.cache_hit:
        parts.append('<p class="empty">候補サムネイル未取得。検索リンクで確認してください。</p>')
    elif candidate.hits:
        parts.append('<div class="thumb-row">')
        for hit in candidate.hits:
            parts.append(_hit_html(hit))
        parts.append("</div>")
    else:
        parts.append('<p class="empty">キャッシュに候補なし</p>')
    parts.append("</article>")
    return "\n".join(parts)


def _hit_html(hit: IstockSearchHit) -> str:
    src = _esc(hit.thumbnail_url)
    alt = _esc(hit.alt)
    detail = _esc(hit.detail_url)
    photographer = _esc(hit.photographer_username)
    asset = _esc(hit.asset_id)
    inner = (
        f'<img src="{src}" alt="{alt}" loading="lazy">'
        f'<span class="asset">gm{asset}</span>'
        f'<span class="photographer">{photographer}</span>'
    )
    if detail:
        return f'<a class="thumb" href="{detail}" target="_blank" rel="noreferrer">{inner}</a>'
    return f'<div class="thumb">{inner}</div>'


def _stat(label: str, value: int) -> str:
    return f'<div><b>{value}</b><span>{_esc(label)}</span></div>'


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


_CSS = """
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
body { margin: 0; background: #f6f7f8; color: #1f2933; }
header { position: sticky; top: 0; z-index: 2; background: #fff; border-bottom: 1px solid #d8dee4; padding: 16px 24px; }
h1 { margin: 0 0 6px; font-size: 24px; }
h2 { margin: 0 0 6px; font-size: 20px; line-height: 1.45; }
h3 { margin: 0 0 12px; font-size: 16px; }
h4 { margin: 0 0 8px; font-size: 14px; line-height: 1.4; }
h4 span { color: #637083; font-weight: 600; }
p { margin: 0 0 8px; line-height: 1.55; }
a { color: #005ea8; }
main { padding: 20px 24px 48px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.stats div { background: #edf2f7; border: 1px solid #d8dee4; border-radius: 6px; padding: 8px 12px; min-width: 92px; }
.stats b { display: block; font-size: 18px; }
.stats span { color: #637083; font-size: 12px; }
.article { background: #fff; border: 1px solid #d8dee4; border-radius: 8px; margin-bottom: 20px; padding: 16px; }
.meta, .muted { color: #637083; font-size: 12px; }
.compare { display: grid; grid-template-columns: minmax(280px, 0.72fr) minmax(420px, 1.28fr); gap: 16px; align-items: start; }
.panel { min-width: 0; }
.image-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.image-card { margin: 0; border: 1px solid #d8dee4; border-radius: 6px; overflow: hidden; background: #fbfcfd; }
.image-card img { width: 100%; aspect-ratio: 16 / 9; object-fit: cover; display: block; background: #e5e9ef; }
figcaption { padding: 8px; font-size: 12px; line-height: 1.45; }
.slot { border: 1px solid #d8dee4; border-radius: 6px; padding: 12px; margin-bottom: 12px; background: #fbfcfd; }
.query { font-weight: 700; }
.reason { color: #344054; font-size: 13px; }
.note { color: #7a4b00; background: #fff4d6; border: 1px solid #f3d58b; border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.thumb-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(116px, 1fr)); gap: 8px; }
.thumb { display: block; text-decoration: none; color: inherit; border: 1px solid #d8dee4; border-radius: 6px; overflow: hidden; background: #fff; min-width: 0; }
.thumb img { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; display: block; background: #e5e9ef; }
.thumb span { display: block; padding: 4px 6px; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset { font-weight: 700; }
.photographer { color: #637083; }
.empty { color: #8a4b00; background: #fff8e8; border: 1px dashed #e7bd73; border-radius: 6px; padding: 10px; font-size: 13px; }
@media (max-width: 900px) {
  header { position: static; }
  .compare { grid-template-columns: 1fr; }
}
"""
