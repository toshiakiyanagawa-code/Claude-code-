"""タイトル案 / サムネ文言案を生成する.

入力: Candidate (channel_title, title, risk_flags, permission_*), Highlight (keywords, score)
出力: TitleSuggestion / ThumbnailSuggestion のリスト

煽り強度は環境変数 CLIPGEN_AGGRESSIVENESS で 0..3 を取り、テンプレ表現を切り替える。
- 0: 中立 ('解説', 'まとめ')
- 1: 軽い煽り ('注目', '話題')
- 2: 標準煽り ('絶句', '論破')
- 3: 強煽り ('完全論破', '激怒', '炎上')
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .scoring import TARGET_LONG, TARGET_SHORT, Candidate, VALID_TARGETS
from .highlights import Highlight


@dataclass
class TitleSuggestion:
    text: str
    format: str  # short / long
    style: str  # neutral / mild / standard / aggressive
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "format": self.format,
            "style": self.style,
            "flags": self.flags,
        }


@dataclass
class ThumbnailSuggestion:
    line1: str
    line2: str
    style: str
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "line1": self.line1,
            "line2": self.line2,
            "style": self.style,
            "flags": self.flags,
        }


# ホット人物名(タイトル内検出用) — scoring.py と整合
_HOT_PEOPLE_NAMES = [
    "高市早苗",
    "玉木雄一郎",
    "玉木",
    "石破茂",
    "石破",
    "神谷宗幣",
    "神谷",
    "山本太郎",
    "石丸伸二",
    "石丸",
    "米山隆一",
    "米山",
    "安住淳",
    "安住",
    "馬場伸幸",
    "馬場",
    "吉村洋文",
    "吉村",
]

_AGGRESSIVENESS_STYLES = {
    0: "neutral",
    1: "mild",
    2: "standard",
    3: "aggressive",
}

_KEYWORD_PRIORITY = ["完全論破", "論破", "絶句", "失言", "暴露", "激怒", "激詰め", "炎上", "鼻で笑う", "本音"]
_SENSATIONAL_WORDS = ["大炎上", "議場凍る", "完全論破", "絶句", "激怒", "失言", "暴露"]


def _get_aggressiveness() -> int:
    raw = os.environ.get("CLIPGEN_AGGRESSIVENESS", "2")
    try:
        v = int(raw)
    except ValueError:
        v = 2
    return max(0, min(3, v))


def _detect_person(title: str) -> str:
    for name in _HOT_PEOPLE_NAMES:
        if name in title:
            return name
    return ""


def _pick_keyword(highlight: Highlight | None) -> str:
    if not highlight or not highlight.keywords:
        return ""
    for kw in _KEYWORD_PRIORITY:
        if kw in highlight.keywords:
            return kw
    return highlight.keywords[0]


def _short_templates(style: str) -> list[str]:
    if style == "neutral":
        return [
            "{person}、{topic}を解説",
            "{person}が語る{topic}のポイント",
            "{topic}についての{person}の発言",
        ]
    if style == "mild":
        return [
            "【注目】{person}、{topic}に発言",
            "話題の{person}、{topic}でこう答えた",
            "{person} {topic}まとめ",
        ]
    if style == "standard":
        return [
            "【{state}】{person}、{topic}の瞬間",
            "{person}『{topic}』に{state}",
            "【切り抜き】{person} {topic}{state}",
        ]
    # aggressive
    return [
        "【{state}】{person}、{topic}で完全論破",
        "【※炎上】{person}『{topic}』にネット騒然",
        "{person}『{topic}』に絶句！議場凍りつく",
        "【完全論破】{person} {topic} 議場が静まり返った瞬間",
    ]


def _long_templates(style: str) -> list[str]:
    if style == "neutral":
        return [
            "{person}の{topic}論まとめ — 国会答弁ダイジェスト",
            "{topic}をめぐる{person}の主張を解説",
            "{person} × {topic} 完全ノーカット解説",
        ]
    if style == "mild":
        return [
            "話題の{person}『{topic}』完全解説 — 何があった？",
            "{person}の{topic}発言、その背景と論点を整理",
            "【特集】{person} {topic}まとめ｜何が起きた？",
        ]
    if style == "standard":
        return [
            "【特集】{person} {topic}、{state}の理由｜国会答弁 全まとめ",
            "{person}『{topic}』に{state}した瞬間まとめ",
            "なぜ{person}は{topic}で{state}したのか — 完全解説",
        ]
    return [
        "【完全保存版】{person} {topic}で議場を凍りつかせた瞬間まとめ",
        "{person} VS 野党 — {topic}を{state}するまでの全記録",
        "【特集】{person}『{topic}』で大炎上した瞬間 — 完全論破まとめ",
    ]


def _thumbnail_templates(style: str, target_format: str) -> list[tuple[str, str]]:
    if target_format == TARGET_SHORT:
        if style == "neutral":
            return [("{person}", "{topic}"), ("{topic}を解説", "{person}")]
        if style == "mild":
            return [("注目発言", "{person} × {topic}"), ("話題", "{person}『{topic}』")]
        if style == "standard":
            return [("{state}", "{person} × {topic}"), ("{person}", "{topic}に{state}")]
        return [
            ("※完全論破", "{person} × {topic}"),
            ("議場凍る", "{person}『{topic}』"),
            ("ネット騒然", "{state}の瞬間"),
        ]
    # long
    if style == "neutral":
        return [("{person} 解説", "{topic} ノーカット"), ("特集", "{person} × {topic}")]
    if style == "mild":
        return [("注目特集", "{person} × {topic}"), ("話題", "{person}『{topic}』")]
    if style == "standard":
        return [("特集", "{person} {topic}{state}"), ("完全解説", "{person} × {topic}")]
    return [
        ("完全保存版", "{person} {topic}"),
        ("議場凍る", "{person} 完全論破"),
        ("大炎上", "{person} × {topic}"),
    ]


_TOPIC_PATTERN = re.compile(r"([一-鿿]{2,5})(?:について|を|の|発言|答弁|論破|失言|炎上)")


def _detect_topic(candidate_title: str, fallback_keyword: str) -> str:
    if fallback_keyword:
        return fallback_keyword
    m = _TOPIC_PATTERN.search(candidate_title)
    if m:
        return m.group(1)
    return "国会答弁"


def _state_word(style: str, keyword: str) -> str:
    if style == "neutral":
        return "発言"
    if style == "mild":
        return "注目"
    if style == "standard":
        return keyword or "絶句"
    return keyword or "完全論破"


def _resolve_state(style: str, topic: str, highlight: Highlight | None, keyword: str) -> str:
    state = _state_word(style, keyword)
    if state != topic:
        return state

    if highlight:
        for kw in _KEYWORD_PRIORITY:
            if kw in highlight.keywords and kw != topic:
                return kw

    return "発言"


def _base_flags(candidate: Candidate) -> tuple[list[str], int | None]:
    flags: list[str] = []
    if "defamation_review_required" in candidate.risk_flags:
        flags.append("REVIEW")
    if candidate.usage_status == "manual_review":
        flags.append("MANUAL")

    if candidate.usage_status == "manual_review" or "defamation_review_required" in candidate.risk_flags:
        flags.append("FORCED_DOWNGRADE")
        return flags, 1
    return flags, None


def _contains_sensational(text: str) -> bool:
    return any(word in text for word in _SENSATIONAL_WORDS)


def _with_sensational_flag(flags: list[str], *texts: str) -> list[str]:
    out = list(flags)
    if any(_contains_sensational(text) for text in texts) and "SENSATIONAL" not in out:
        out.append("SENSATIONAL")
    return out


def _normalize_length(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def generate_titles(
    candidate: Candidate,
    highlight: Highlight | None = None,
    *,
    target_format: str = TARGET_SHORT,
    aggressiveness: int | None = None,
    max_count: int = 4,
) -> list[TitleSuggestion]:
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}")
    level = _get_aggressiveness() if aggressiveness is None else max(0, min(3, aggressiveness))

    flags, forced_level = _base_flags(candidate)
    if forced_level is not None:
        level = min(level, forced_level)

    style = _AGGRESSIVENESS_STYLES[level]

    person = _detect_person(candidate.title) or "発言者"
    keyword = _pick_keyword(highlight)
    topic = _detect_topic(candidate.title, keyword)
    state = _resolve_state(style, topic, highlight, keyword)

    templates = _short_templates(style) if target_format == TARGET_SHORT else _long_templates(style)
    limit = 25 if target_format == TARGET_SHORT else 45

    out: list[TitleSuggestion] = []
    for tmpl in templates[:max_count]:
        text = tmpl.format(person=person, topic=topic, state=state)
        prefix = "[REVIEW] " if "REVIEW" in flags else ""
        normalized = prefix + _normalize_length(text, limit + len(prefix))
        out.append(
            TitleSuggestion(
                text=normalized,
                format=target_format,
                style=style,
                flags=_with_sensational_flag(flags, normalized),
            )
        )
    return out


def generate_thumbnails(
    candidate: Candidate,
    highlight: Highlight | None = None,
    *,
    target_format: str = TARGET_SHORT,
    aggressiveness: int | None = None,
    max_count: int = 3,
) -> list[ThumbnailSuggestion]:
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}")
    level = _get_aggressiveness() if aggressiveness is None else max(0, min(3, aggressiveness))

    flags, forced_level = _base_flags(candidate)
    if forced_level is not None:
        level = min(level, forced_level)

    style = _AGGRESSIVENESS_STYLES[level]

    person = _detect_person(candidate.title) or "発言者"
    keyword = _pick_keyword(highlight)
    topic = _detect_topic(candidate.title, keyword)
    state = _resolve_state(style, topic, highlight, keyword)

    line_limit = 12 if target_format == TARGET_SHORT else 18

    templates = _thumbnail_templates(style, target_format)[:max_count]
    out: list[ThumbnailSuggestion] = []
    for l1, l2 in templates:
        line1 = _normalize_length(l1.format(person=person, topic=topic, state=state), line_limit)
        line2 = _normalize_length(l2.format(person=person, topic=topic, state=state), line_limit)
        out.append(
            ThumbnailSuggestion(
                line1=line1,
                line2=line2,
                style=style,
                flags=_with_sensational_flag(flags, line1, line2),
            )
        )
    return out
