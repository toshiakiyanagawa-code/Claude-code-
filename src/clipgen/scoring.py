"""候補動画のスコアリング.

トレンド分析(docs/clipgen/trend_analysis.md §6.2)の指標を実装。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .sources import looks_defamatory

# 「快」「絶句」「パニック」フォーマット語に近いタイトル要素
_FORMAT_BOOSTS = [
    (re.compile(r"【[^】]*(爆笑|笑|微笑)[^】]*】"), 0.10, "format:laugh"),
    (re.compile(r"【[^】]*(絶句|失言|炎上|激怒)[^】]*】"), 0.18, "format:shock"),
    (re.compile(r"【[^】]*(論破|完全論破|ぶった斬)[^】]*】"), 0.15, "format:debate"),
    (re.compile(r"【[^】]*(衝撃|暴露|本音)[^】]*】"), 0.12, "format:reveal"),
    (re.compile(r"切り抜き"), 0.05, "format:clip_label"),
]

_HOT_PEOPLE = [
    "高市早苗",
    "高市",
    "玉木",
    "石破",
    "神谷",
    "山本太郎",
    "石丸",
    "米山",
    "安住",
    "馬場",
    "吉村",
]

_PRIMARY_SOURCE_CATEGORIES = {"party_official", "parliament_official", "politician_official"}

# 出力フォーマットの定義
TARGET_SHORT = "short"
TARGET_LONG = "long"
VALID_TARGETS = (TARGET_SHORT, TARGET_LONG)


@dataclass
class Candidate:
    video_id: str
    title: str
    channel_id: str
    channel_handle: str | None
    channel_title: str
    published_at: datetime
    duration_sec: int | None
    view_count: int
    like_count: int | None
    comment_count: int | None
    description: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    permission_reason: str = ""
    permission_category: str = ""
    permission_scope: str = ""
    allowed: bool = False
    usage_status: str = "manual_review"
    target_format: str = "short"

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _hours_since(published_at: datetime, *, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return max((now - published_at).total_seconds() / 3600.0, 0.5)


def score_candidate(
    c: Candidate,
    *,
    now: datetime | None = None,
    target_format: str = TARGET_SHORT,
) -> Candidate:
    """候補をスコアリングし、c.score と c.score_breakdown を埋める。

    target_format:
      - "short": 60秒以下を優遇、>480秒は減点
      - "long":  60〜600秒を中立、>=480秒（公式中継・記者会見など）を優遇
    """
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}, got {target_format!r}")
    hours = _hours_since(c.published_at, now=now)
    bd: dict[str, float] = {}

    vph = c.view_count / hours
    # log(101) ≒ 4.61 を 1.0 として 0〜2.0+ にスケール
    bd["views_per_hour"] = round(math.log1p(vph) / math.log(101) * 1.0, 3)

    if c.like_count is not None and c.view_count > 0:
        ratio = c.like_count / c.view_count
        bd["like_ratio"] = round(min(ratio / 0.02, 1.0) * 0.4 - (0.2 if ratio < 0.01 else 0.0), 3)
    else:
        bd["like_ratio"] = 0.0

    if c.comment_count is not None and c.view_count > 0:
        density = c.comment_count / c.view_count
        if density < 1e-4 and c.view_count > 100_000:
            bd["comment_density"] = -0.5
            c.risk_flags.append("suspicious:low_comment_density")
        else:
            bd["comment_density"] = round(min(density / 1e-3, 1.0) * 0.2, 3)
    else:
        bd["comment_density"] = 0.0

    bd["freshness_boost"] = round(math.exp(-hours / 48.0) * 0.4, 3)

    title = c.title or ""
    kb = 0.0
    matched_people: list[str] = []
    for p in _HOT_PEOPLE:
        if p in title:
            matched_people.append(p)
            kb += 0.08
            if kb >= 0.16:
                break
    bd["keyword_boost"] = round(kb, 3)
    if matched_people:
        c.risk_flags.append("people:" + ",".join(sorted(set(matched_people))))

    fb = 0.0
    for pat, weight, tag in _FORMAT_BOOSTS:
        if pat.search(title):
            fb += weight
            c.risk_flags.append(tag)
    bd["format_boost"] = round(fb, 3)

    if c.duration_sec is not None:
        if target_format == TARGET_SHORT:
            if c.duration_sec <= 60:
                bd["duration_fit"] = 0.1
            elif c.duration_sec > 480:
                bd["duration_fit"] = -0.1
            else:
                bd["duration_fit"] = 0.0
        else:  # TARGET_LONG
            if c.duration_sec >= 480:
                bd["duration_fit"] = 0.15
            elif c.duration_sec >= 180:
                bd["duration_fit"] = 0.05
            elif c.duration_sec < 60:
                bd["duration_fit"] = -0.1
            else:
                bd["duration_fit"] = 0.0
    else:
        bd["duration_fit"] = 0.0

    if c.allowed and c.permission_category in _PRIMARY_SOURCE_CATEGORIES:
        bd["primary_source"] = 0.15
    else:
        bd["primary_source"] = 0.0

    if looks_defamatory(c.title):
        c.risk_flags.append("defamation_review_required")

    raw_score = sum(bd.values())
    if "tv_hint_in_text" in c.risk_flags:
        bd["tv_hint_multiplier"] = 0.7
        c.score = round(raw_score * 0.7, 3)
    else:
        c.score = round(raw_score, 3)

    c.score_breakdown = bd
    return c
