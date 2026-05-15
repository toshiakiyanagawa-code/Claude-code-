"""Fetch President Online articles matching a list of manuscript titles.

For each title in ``data/manuscripts_index.json``:
  1. Search president.jp via ``/list/search?fulltext=<title>``
  2. Re-rank candidates by Jaccard token overlap with the manuscript title
     (site search returns by popularity/date, not relevance).
  3. Fetch the article + all paginated sub-pages.
  4. Extract per-photo: src, alt, caption, photographer (iStock username if any),
     and the non-iStock attribution string if present.

Output: ``data/published_articles.json``

Run:
    uv run python -m cms_entry_assistant.pjp_fetcher
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

INDEX_PATH = Path("data/manuscripts_index.json")
OUT_PATH = Path("data/published_articles.json")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Throttle: keep this generous since we're crawling our own publication's site.
_RATE_SEC = 1.5

# iStock attribution: 「写真＝iStock.com／<username>」 (full- or half-width / or =)
_ISTOCK_RE = re.compile(r"iStock\.com\s*[／/]\s*([^\s<）)、,]+)")
# Generic 写真＝ attribution (catches Wikimedia, Getty, 撮影=人物名 etc.)
_PHOTO_ATTR_RE = re.compile(r"写真[＝=]\s*([^<\n）)]{1,80})")


def _classify_source(caption: str) -> tuple[str, str]:
    """Return (source_label, photographer_or_empty) from a caption string."""
    cap = caption or ""
    m = _ISTOCK_RE.search(cap)
    if m:
        return "iStock", m.group(1).strip()
    if "Wikimedia" in cap or "CC-BY" in cap:
        return "Wikimedia/CC", ""
    if "Getty" in cap or "EPA" in cap or "AFP" in cap or "ロイター" in cap or "時事通信" in cap or "共同通信" in cap:
        return "報道(通信社)", ""
    if "提供" in cap or "撮影" in cap:
        return "提供/撮影", ""
    if "※写真はイメージです" in cap or "写真はイメージ" in cap:
        return "イメージ(出典不明)", ""
    if "写真＝" in cap or "写真=" in cap:
        return "その他クレジット", ""
    return "不明", ""


def jaccard_tokens(a: str, b: str) -> float:
    """Crude Japanese token overlap: 2-gram sets."""

    def grams(s: str) -> set[str]:
        s = re.sub(r"[\s　【】『』「」［］()（）/／・|｜!！?？…：:]+", "", s)
        return {s[i : i + 2] for i in range(len(s) - 1)}

    A, B = grams(a), grams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


_TRIVIAL_PUNCT = "｢｣「」『』”\"'’“…・,，.、。!！?？()（）[]［］【】※"


def _normalize_for_search(s: str) -> str:
    """Strip punctuation noise so president.jp's full-text matcher can hit."""
    out = []
    for ch in s:
        if ch in _TRIVIAL_PUNCT:
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _query_variants(title: str) -> list[str]:
    """Try the full title first, then progressively shorter sub-strings.

    President Online's full-text search misses on hits with too much noise
    (half-width brackets, curly quotes, "..."). So we fall back to the
    middle slice of the title which tends to be the most distinctive.
    """
    norm = _normalize_for_search(title)
    variants = [norm]
    # Pick a distinctive substring before "…" / "..."
    head = re.split(r"…+|\.\.\.", norm, maxsplit=1)
    if head and head[0] and head[0] != norm:
        variants.append(head[0].strip())
    # Inner clause
    inner = re.findall(r"[一-龯ぁ-んァ-ヶー\w]{4,}", norm)
    if len(inner) >= 2:
        variants.append(" ".join(inner[:3]))
    # Dedup preserving order
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _search_one(page, query: str) -> tuple[list[dict], str]:
    url = f"https://president.jp/list/search?fulltext={quote(query)}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return [], f"goto error: {exc}"
    await page.wait_for_timeout(2500)
    headline = await page.evaluate(
        "() => document.querySelector('h1.article-list__headline')?.textContent?.trim() || ''"
    )
    cands = await page.evaluate(
        """() => {
        const seen = new Map();
        document.querySelectorAll(
            'section.article-list li.article-list__item a[href^="/articles/-/"]'
        ).forEach(a => {
            const m = (a.getAttribute('href')||'').match(/^\\/articles\\/-\\/(\\d+)/);
            if (!m) return;
            const id = m[1];
            const t = (a.textContent||'').trim();
            if (!seen.has(id) || seen.get(id).text.length < t.length) {
                seen.set(id, {id, text: t});
            }
        });
        return Array.from(seen.values()).slice(0, 40);
    }"""
    )
    return cands, headline


async def search_and_pick(page, manuscript_title: str) -> tuple[str | None, list[dict]]:
    """Search president.jp and return (best_article_id, ranked_candidates).

    Tries the full normalized title first; if no confident match, falls back
    to shorter variants. Returns the candidate with the highest Jaccard
    2-gram overlap to the manuscript title (threshold 0.18).
    """
    all_scored: list[dict] = []
    seen_ids: set[str] = set()
    last_headline = ""
    for variant in _query_variants(manuscript_title):
        cands, headline = await _search_one(page, variant)
        last_headline = headline or last_headline
        for c in cands:
            if c["id"] in seen_ids:
                continue
            seen_ids.add(c["id"])
            s = jaccard_tokens(manuscript_title, c["text"])
            all_scored.append({**c, "score": round(s, 3), "queried": variant})
        all_scored.sort(key=lambda x: -x["score"])
        if all_scored and all_scored[0]["score"] >= 0.4:
            break  # confident hit, stop searching variants

    if not all_scored:
        return None, [{"headline": last_headline, "note": "no search-result items"}]

    top = all_scored[0]
    if top["score"] < 0.18:
        return None, all_scored[:5]
    return top["id"], all_scored[:5]


async def extract_article(page, article_id: str) -> dict:
    """Fetch all paginated sub-pages of an article and pull image+attribution data."""
    base = f"https://president.jp/articles/-/{article_id}"
    try:
        await page.goto(base, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return {"id": article_id, "error": f"goto failed: {exc}"}
    await page.wait_for_timeout(2000)
    published_title = await page.title()

    # Discover sub-pages
    subpages = await page.evaluate(
        f"""() => {{
            const set = new Set();
            document.querySelectorAll('a[href^="/articles/-/{article_id}/"]').forEach(a => {{
                const h = a.getAttribute('href');
                const m = h.match(/^\\/articles\\/-\\/{article_id}\\/(\\d+)$/);
                if (m) set.add(parseInt(m[1]));
            }});
            return Array.from(set).sort((a,b) => a-b);
        }}"""
    )
    page_numbers = [1] + [n for n in subpages if n > 1]

    pages_data = []
    for n in page_numbers:
        url = base if n == 1 else f"{base}/{n}"
        if n > 1:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                pages_data.append({"page": n, "error": f"goto failed: {exc}"})
                continue
            await page.wait_for_timeout(_RATE_SEC * 1000)

        # Scroll to bottom once to trigger lazy-loading of body images.
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, 0);")
        await page.wait_for_timeout(400)

        # Scroll once to trigger lazy-loaded body images.
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, 0);")
        await page.wait_for_timeout(400)

        images = await page.evaluate(
            """() => {
            const out = [];

            // Find the source-credit div ("写真＝...") nearest to the given element,
            // checking siblings of the image, its parent (<a>), and the parent's parent.
            function findSource(img) {
                // 1. Check siblings of img.parent (typically <a><img></a> → next div.source)
                const containers = [img.parentElement, img.parentElement?.parentElement];
                for (const c of containers) {
                    if (!c) continue;
                    let sib = c.nextElementSibling;
                    let hops = 0;
                    while (sib && hops < 4) {
                        const cls = (sib.className||'') + '';
                        const txt = (sib.textContent||'').trim();
                        if (cls.includes('source') || /写真[＝=]/.test(txt) || cls.includes('caption')) {
                            return txt;
                        }
                        sib = sib.nextElementSibling; hops++;
                    }
                }
                return '';
            }

            // Hero image lives in .article-head.
            document.querySelectorAll('.article-head img').forEach(img => {
                if (!img.src || img.src.includes('icons/') || img.naturalWidth < 200) return;
                // Hero's source credit lives inside .article-head (often .article-head .source)
                const headSource = img.closest('.article-head')?.querySelector('.source, [class*="source"]');
                const cap = headSource ? (headSource.textContent||'').trim() : findSource(img);
                out.push({
                    role: 'hero',
                    src: img.src,
                    alt: (img.alt||'').trim(),
                    caption: cap,
                    w: img.naturalWidth, h: img.naturalHeight,
                });
            });

            // Body images live in .article-body. The credit is a div.source sibling.
            document.querySelectorAll('.article-body img').forEach(img => {
                if (!img.src || img.src.includes('icons/') || img.naturalWidth < 200) return;
                out.push({
                    role: 'body',
                    src: img.src,
                    alt: (img.alt||'').trim(),
                    caption: findSource(img),
                    w: img.naturalWidth, h: img.naturalHeight,
                });
            });

            return out;
        }"""
        )
        html = await page.content()
        # Find photo-attribution strings near the figcaptions (we already have them above,
        # but iStock-only also appears in plain "写真＝iStock.com／…" outside figcaption).
        all_attrs = sorted(set(_PHOTO_ATTR_RE.findall(html)))
        istock_names = sorted(set(_ISTOCK_RE.findall(html)))

        # Per-image: classify source from caption text
        for img in images:
            img["source"], img["photographer"] = _classify_source(img.get("caption", ""))

        pages_data.append(
            {
                "page": n,
                "url": url,
                "images": images,
                "photo_attrs": all_attrs,
                "istock_usernames": istock_names,
            }
        )

    return {
        "id": article_id,
        "url": base,
        "published_title": published_title,
        "page_count": len(pages_data),
        "pages": pages_data,
    }


async def _new_browser(p):
    browser = await p.chromium.launch(
        channel="chromium",
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(
        user_agent=UA, locale="ja-JP", viewport={"width": 1366, "height": 800}
    )
    page = await ctx.new_page()
    return browser, page


async def run(
    manuscripts: list[dict], existing: dict[str, dict], *, restart_every: int = 10
) -> dict[str, dict]:
    """Run the full pipeline over a list of manuscript records.

    Restarts the Chromium browser every ``restart_every`` records to release
    memory — useful on tight-memory hosts where Playwright's RSS creeps up.
    """
    from playwright.async_api import async_playwright

    results: dict[str, dict] = dict(existing)
    pending = [r for r in manuscripts if r["file"] not in results or results[r["file"]].get("error")]
    print(f"to fetch: {len(pending)} / {len(manuscripts)} (rest already in cache)")

    async with async_playwright() as p:
        browser, page = await _new_browser(p)
        try:
            for i, rec in enumerate(pending):
                if i > 0 and i % restart_every == 0:
                    await browser.close()
                    print(f"  [restart browser at i={i}]")
                    browser, page = await _new_browser(p)
                key = rec["file"]
                title = rec.get("title", "")
                if not title:
                    results[key] = {"error": "no title"}
                    continue
                print(f"\n[{i+1}/{len(pending)}] {key}")
                print(f"  manuscript title: {title[:60]}")
                try:
                    article_id, scored = await search_and_pick(page, title)
                    await page.wait_for_timeout(_RATE_SEC * 1000)
                except Exception as exc:
                    results[key] = {"error": f"search exception: {exc}", "manuscript_title": title}
                    print(f"  -> ERROR search: {exc}")
                    continue
                if not article_id:
                    best = scored[0].get("score") if scored and "score" in scored[0] else "n/a"
                    print(f"  -> no match (best score: {best})")
                    results[key] = {
                        "manuscript_title": title,
                        "candidates": scored,
                        "error": "no confident match",
                    }
                    OUT_PATH.write_text(
                        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    continue
                print(f"  -> matched id={article_id} score={scored[0]['score']}")
                try:
                    art = await extract_article(page, article_id)
                except Exception as exc:
                    results[key] = {
                        "error": f"extract exception: {exc}",
                        "manuscript_title": title,
                        "candidates": scored,
                    }
                    print(f"  -> ERROR extract: {exc}")
                    continue
                art["manuscript_title"] = title
                art["candidates"] = scored
                results[key] = art
                OUT_PATH.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        finally:
            await browser.close()
    return results


def main(limit: int | None = None) -> None:
    if not INDEX_PATH.exists():
        print(f"[!] missing {INDEX_PATH} — run ingest_manuscripts first", file=sys.stderr)
        sys.exit(1)
    manuscripts = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    manuscripts = [m for m in manuscripts if m.get("title") and not m.get("error")]
    if limit:
        manuscripts = manuscripts[:limit]
    existing = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    results = asyncio.run(run(manuscripts, existing))
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    matched = sum(1 for v in results.values() if not v.get("error"))
    print(f"\n=== done: {matched}/{len(results)} matched, saved to {OUT_PATH}")


if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit_arg)
