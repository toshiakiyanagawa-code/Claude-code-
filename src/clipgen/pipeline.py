"""候補抽出パイプライン: 検索 → 詳細 → 許諾チェック → スコアリング → 出力."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .scoring import TARGET_SHORT, VALID_TARGETS, Candidate, score_candidate
from .sources import (
    Channel,
    check_channel_permission,
    load_allowlist,
    load_blocklist,
    load_seed_queries,
    looks_like_tv_source,
)
from .youtube_client import (
    SearchParams,
    YouTubeAPIError,
    YouTubeClient,
    candidate_from_video_item,
    load_mock_search,
)

DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MIN_VIEWS = 30_000
BLOCKED_PERMISSION_SCOPES = {"tv_broadcast", "news_agency_clip"}
LOGGER = logging.getLogger(__name__)


def build_queries(seeds: dict[str, list[str]]) -> list[str]:
    """seed_queries.json から検索文字列を組み立てる."""
    people = seeds.get("people", [])
    formats = seeds.get("format_words", [])
    topics = seeds.get("topics", [])
    queries: list[str] = []
    for p in people:
        for f in formats:
            queries.append(f"{p} {f}")
    for t in topics:
        queries.append(t)
    return queries


def _has_blocklist_match(c: Candidate) -> bool:
    return any(flag.startswith("blocklist_match") for flag in c.risk_flags)


def _resolve_channel_ids(channels: list[Channel], resolved: dict[str, str]) -> list[Channel]:
    out: list[Channel] = []
    for ch in channels:
        if ch.channel_id:
            out.append(ch)
            continue
        cid = resolved.get(ch.handle.lower())
        out.append(replace(ch, channel_id=cid) if cid else ch)
    return out


def filter_and_score(
    candidates: Iterable[Candidate],
    *,
    allowlist: list[Channel],
    blocklist: list[Channel],
    block_keywords: list[str],
    min_views: int = DEFAULT_MIN_VIEWS,
    now: datetime | None = None,
    include_blocked: bool = False,
    target_format: str = TARGET_SHORT,
) -> list[Candidate]:
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}, got {target_format!r}")
    out: list[Candidate] = []
    for c in candidates:
        if c.view_count < min_views:
            continue
        perm = check_channel_permission(
            channel_id=c.channel_id,
            channel_handle=c.channel_handle,
            channel_title=c.channel_title,
            channel_description=c.description,
            allowlist=allowlist,
            blocklist=blocklist,
            block_keywords=block_keywords,
        )
        c.allowed = perm.allowed
        c.permission_reason = perm.reason
        c.permission_category = perm.category
        c.permission_scope = perm.scope
        c.risk_flags.extend(perm.risk_flags)
        if looks_like_tv_source(c.title) or looks_like_tv_source(c.description):
            c.risk_flags.append("tv_hint_in_text")

        if c.permission_scope in BLOCKED_PERMISSION_SCOPES:
            c.allowed = False
            c.usage_status = "blocked"
            c.risk_flags.append(f"blocked_permission_scope:{c.permission_scope}")
        elif _has_blocklist_match(c):
            c.usage_status = "blocked"
        elif c.allowed and "tv_hint_in_text" not in c.risk_flags:
            c.usage_status = "cleared"
        else:
            c.usage_status = "manual_review"

        c.target_format = target_format
        c = score_candidate(c, now=now, target_format=target_format)
        if "defamation_review_required" in c.risk_flags and c.usage_status == "cleared":
            c.usage_status = "manual_review"

        if c.usage_status == "blocked" and not include_blocked:
            continue
        out.append(c)
    out.sort(key=lambda x: (x.usage_status == "cleared", x.score), reverse=True)
    return out


def run_pipeline_mock(
    mock_path: Path,
    *,
    now: datetime | None = None,
    include_blocked: bool = False,
    target_format: str = TARGET_SHORT,
) -> list[Candidate]:
    allowlist = load_allowlist()
    blocklist_channels, block_keywords = load_blocklist()
    raw = load_mock_search(mock_path)
    return filter_and_score(
        raw,
        allowlist=allowlist,
        blocklist=blocklist_channels,
        block_keywords=block_keywords,
        now=now,
        include_blocked=include_blocked,
        target_format=target_format,
    )


def run_pipeline_live(
    api_key: str | None = None,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_per_query: int = 10,
    query_limit: int | None = None,
    min_views: int = DEFAULT_MIN_VIEWS,
    now: datetime | None = None,
    include_blocked: bool = False,
    target_format: str = TARGET_SHORT,
    client: YouTubeClient | None = None,
) -> list[Candidate]:
    if client is None:
        client = YouTubeClient(api_key=api_key)
    allowlist = load_allowlist()
    blocklist_channels, block_keywords = load_blocklist()
    all_handles = sorted({ch.handle for ch in [*allowlist, *blocklist_channels] if ch.handle and not ch.channel_id})
    if all_handles:
        try:
            resolved = client.handles_to_channel_ids(all_handles)
            allowlist = _resolve_channel_ids(allowlist, resolved)
            blocklist_channels = _resolve_channel_ids(blocklist_channels, resolved)
        except YouTubeAPIError as exc:
            LOGGER.warning("failed to resolve channel handles: %s", exc)

    seeds = load_seed_queries()
    queries = build_queries(seeds)
    if query_limit is not None:
        queries = queries[: max(0, query_limit)]
    published_after = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)

    seen_ids: set[str] = set()
    video_ids: list[str] = []
    for q in queries:
        try:
            items = client.search(
                SearchParams(query=q, max_results=max_per_query, published_after=published_after)
            )
        except YouTubeAPIError as exc:
            LOGGER.warning("search failed for query %r: %s", q, exc)
            continue
        for it in items:
            vid = it.get("id", {}).get("videoId")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                video_ids.append(vid)

    # videos.list は 50 件単位
    enriched: list[Candidate] = []
    for chunk_start in range(0, len(video_ids), 50):
        chunk = video_ids[chunk_start : chunk_start + 50]
        try:
            details = client.videos(chunk)
        except YouTubeAPIError as exc:
            LOGGER.warning("videos lookup failed: %s", exc)
            continue
        for d in details:
            enriched.append(candidate_from_video_item(d))

    return filter_and_score(
        enriched,
        allowlist=allowlist,
        blocklist=blocklist_channels,
        block_keywords=block_keywords,
        min_views=min_views,
        now=now,
        include_blocked=include_blocked,
        target_format=target_format,
    )


def candidates_to_dict(cands: list[Candidate]) -> list[dict]:
    out = []
    for c in cands:
        d = asdict(c)
        d["url"] = c.url
        d["published_at"] = c.published_at.isoformat()
        d["audit"] = {
            "reviewed_by": "",
            "decision": "",
            "decided_at": "",
            "notes": "",
        }
        out.append(d)
    return out


def write_json(cands: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(candidates_to_dict(cands), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
