"""iStock search-results crawler using Playwright (real Chromium).

User explicitly authorized this approach (2026-05-13) — see
docs/photo_candidates_plan.md and memory project_photo_candidates_decisions.md.

Why this module exists:
- Direct httpx / headless-shell requests are blocked by iStock's bot wall.
- A full Chromium + playwright-stealth bypasses the bot wall reliably.

Public API:
- `crawl_search(query_ja, *, limit=8) -> list[IstockSearchHit]`
- `is_available() -> bool`  (returns False if playwright/chromium is missing)

This module is intentionally **synchronous** at the boundary so the rest of the
codebase doesn't have to deal with asyncio. The async work happens inside the
single `asyncio.run()` call per search.

Rate-limiting & ethics:
- 2.5s minimum spacing between searches (per process).
- Realistic Chrome UA + Japanese locale.
- Results are cached locally so we don't hammer iStock for repeated queries.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_CACHE_PATH = Path("data/istock_search_cache.json")

# Process-wide rate limit. iStock is sensitive — keep this generous.
# iStock 側のブロック (HTTP failure) を引き起こさないために 2.5s をデフォルトに戻す。
# Codespaces 側のタイムアウト緩和は別の手段 (slot 並列化 + LLM/iStock 両方の cache)
# で吸収する。緊急時のみ CMS_ENTRY_ASSISTANT_ISTOCK_INTERVAL_S で下げてよい。
import os as _os
_MIN_SEARCH_INTERVAL_S = float(
    _os.getenv("CMS_ENTRY_ASSISTANT_ISTOCK_INTERVAL_S") or "2.5"
)
del _os
_LAST_SEARCH_AT: float = 0.0


@dataclass
class IstockSearchHit:
    """One result row from an iStock search-results page."""

    asset_id: str
    thumbnail_url: str = ""
    alt: str = ""
    photographer_username: str = ""
    detail_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _CacheEntry:
    fetched_at: str
    query: str
    hits: list[dict] = field(default_factory=list)
    error: str = ""


def is_available() -> bool:
    """Return True if Playwright + Chromium are importable on this machine."""
    try:
        import playwright  # noqa: F401
        from playwright_stealth import Stealth  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Cache (a simple JSON dict keyed by normalized query)
# ---------------------------------------------------------------------------


def _cache_key(query: str) -> str:
    return query.strip().lower()


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Async search worker (one search per call). The caller invokes via asyncio.run.
# ---------------------------------------------------------------------------


async def _do_search(query: str, *, limit: int) -> list[IstockSearchHit]:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    encoded = urllib.parse.quote(query)
    url = (
        f"https://www.istockphoto.com/jp/search/2/image?phrase={encoded}"
        f"&assetfiletype=image&excludenudity=true&mediatype=photography"
    )

    stealth = Stealth()
    hits: list[IstockSearchHit] = []
    async with async_playwright() as p:
        # `channel="chromium"` selects the full Chromium build (not the
        # lightweight headless-shell, which iStock fingerprints aggressively).
        browser = await p.chromium.launch(
            channel="chromium",
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
                viewport={"width": 1366, "height": 800},
            )
            await stealth.apply_stealth_async(ctx)
            page = await ctx.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status >= 400:
                return []
            if "bot-wall" in page.url:
                return []
            # Allow gallery hydration to settle.
            await page.wait_for_timeout(2500)

            # Pull hits directly from the asset-card DOM.
            raw_rows = await page.evaluate(
                """
                () => {
                    const out = [];
                    const cards = document.querySelectorAll('[data-asset-id], [data-testid="gallery-mosaic-asset"]');
                    cards.forEach(card => {
                        const id = card.getAttribute('data-asset-id') || '';
                        if (!id) return;
                        const img = card.querySelector('img');
                        const link = card.querySelector('a[href*="gm"]');
                        const photographer = card.querySelector('[itemprop="author"] [itemprop="name"], [itemprop="creator"] [itemprop="name"]');
                        out.push({
                            id,
                            thumbnail_url: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
                            alt: img ? (img.getAttribute('alt') || '') : '',
                            photographer_username: photographer ? (photographer.getAttribute('content') || photographer.textContent || '') : '',
                            detail_url: link ? link.getAttribute('href') : '',
                        });
                    });
                    return out;
                }
                """
            )

            seen: set[str] = set()
            for row in raw_rows or []:
                aid = (row.get("id") or "").strip()
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                detail = row.get("detail_url") or ""
                if detail and detail.startswith("/"):
                    detail = "https://www.istockphoto.com" + detail
                hits.append(
                    IstockSearchHit(
                        asset_id=aid,
                        thumbnail_url=row.get("thumbnail_url", "").strip(),
                        alt=row.get("alt", "").strip(),
                        photographer_username=(row.get("photographer_username") or "").strip(),
                        detail_url=detail,
                    )
                )
                if len(hits) >= limit:
                    break
        finally:
            await browser.close()
    return hits


# ---------------------------------------------------------------------------
# Public sync entry point with rate-limit and cache
# ---------------------------------------------------------------------------


def crawl_search(
    query: str,
    *,
    limit: int = 8,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    max_age_days: int = 30,
    force_refresh: bool = False,
) -> list[IstockSearchHit]:
    """Search iStock for `query` and return up to `limit` hits.

    Cached results are reused when fresh (< `max_age_days`), unless
    `force_refresh=True`. Rate limit is enforced even on cache miss.
    """
    if not is_available():
        return []
    if not query or not query.strip():
        return []
    cache_path = Path(cache_path)
    cache = _load_cache(cache_path)
    key = _cache_key(query)
    now_ts = time.time()

    # Cache hit?
    if not force_refresh and key in cache:
        entry = cache[key]
        try:
            fetched_at = datetime.fromisoformat(entry.get("fetched_at", "")).timestamp()
        except Exception:
            fetched_at = 0
        if now_ts - fetched_at < max_age_days * 86400 and entry.get("hits"):
            return [IstockSearchHit(**h) for h in entry["hits"][:limit]]

    # Rate limit
    global _LAST_SEARCH_AT
    wait = _MIN_SEARCH_INTERVAL_S - (time.monotonic() - _LAST_SEARCH_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_SEARCH_AT = time.monotonic()

    try:
        hits = asyncio.run(_do_search(query, limit=limit))
    except Exception as exc:
        # エラーはディスクに永続化しない (再試行が常に効くようにする)。
        # 例えば過去の "asyncio.run cannot be called from a running event loop" のような
        # 環境依存エラーをキャッシュに残すと、次回も誤って空候補を返してしまう。
        # 既存のエラーエントリがあれば併せて掃除しておく。
        cache.pop(key, None)
        _save_cache(cache_path, cache)
        return []

    cache[key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": query,
        "hits": [h.to_dict() for h in hits],
        "error": "",
    }
    _save_cache(cache_path, cache)
    return hits


def crawl_many(
    queries: Iterable[str],
    *,
    limit_per_query: int = 6,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
) -> dict[str, list[IstockSearchHit]]:
    """Run `crawl_search` over a list of queries. Cache + rate-limit apply."""
    out: dict[str, list[IstockSearchHit]] = {}
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        out[q] = crawl_search(q, limit=limit_per_query, cache_path=cache_path)
    return out


# Pattern used elsewhere to convert iStock paths to absolute URLs
_REL_URL_RE = re.compile(r"^/jp/photo/-gm(\d+)")


def asset_page_url(asset_id: str) -> str:
    return f"https://www.istockphoto.com/jp/photo/-gm{asset_id}"
