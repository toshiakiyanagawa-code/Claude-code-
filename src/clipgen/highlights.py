"""字幕(SRT/WebVTT)からハイライト窓を検出する.

スコア要素:
  - 感嘆符(!？!?)密度
  - 笑い表現(笑/(笑)/wwww など)
  - 否定/批判の語彙(違う/間違い/おかしい)
  - ホットワード(絶句/論破/失言/暴露/激怒/激詰め)
  - 大文字/カタカナ強調(語数の急増)
  - 話者交代(— or > 等)

短尺/長尺で出力ウィンドウのサイズ・件数を切り替える。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from .scoring import TARGET_LONG, TARGET_SHORT, VALID_TARGETS

# ハイライトキーワード(スコア重み)
_KEYWORD_WEIGHTS: dict[str, float] = {
    "絶句": 1.0,
    "論破": 1.0,
    "完全論破": 1.2,
    "失言": 0.8,
    "暴露": 0.8,
    "炎上": 0.6,
    "激怒": 0.9,
    "激詰め": 0.9,
    "本音": 0.5,
    "鼻で笑う": 1.1,
    "ぶった斬": 0.8,
    "おかしい": 0.4,
    "違います": 0.4,
    "間違い": 0.3,
    "(笑)": 0.5,
    "笑": 0.2,
}
_EXCLAMATION_PATTERN = re.compile(r"[!?！？]")
_LAUGHTER_PATTERN = re.compile(r"(?:w{2,}|ｗ{2,}|（笑）|\(笑\))")


@dataclass
class Cue:
    """1行分の字幕."""

    start_sec: float
    end_sec: float
    text: str


@dataclass
class Highlight:
    start_sec: float
    end_sec: float
    score: float
    rationale: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return max(self.end_sec - self.start_sec, 0.0)

    def as_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 2),
            "end_sec": round(self.end_sec, 2),
            "duration_sec": round(self.duration_sec, 2),
            "score": round(self.score, 3),
            "rationale": self.rationale,
            "keywords": self.keywords,
        }


# ---------- SRT/VTT パーサ ----------

_TS_RE = re.compile(r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)")


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(text: str) -> list[Cue]:
    """SRT / WebVTT 双方を受け付ける簡易パーサ.

    `WEBVTT` ヘッダ、空行、NOTE コメントを飛ばし、cue setting 付きの
    `HH:MM:SS.mmm --> HH:MM:SS.mmm align:start` 形式も許容する。
    """
    cues: list[Cue] = []
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if not line or line == "WEBVTT":
            i += 1
            continue

        if line.startswith("NOTE"):
            i += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            continue

        match = _TS_RE.search(line)
        if match is None and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            match = _TS_RE.search(next_line)
            if match is not None:
                i += 1

        if match is None:
            i += 1
            continue

        start = _ts_to_sec(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _ts_to_sec(match.group(5), match.group(6), match.group(7), match.group(8))
        i += 1

        body_lines: list[str] = []
        while i < len(lines):
            body_line = lines[i].strip()
            if not body_line:
                break
            if body_line.startswith("NOTE"):
                break
            body_lines.append(body_line)
            i += 1

        body = " ".join(body_lines).strip()
        if body:
            cues.append(Cue(start, end, body))

    return cues


# ---------- スコアリング ----------


def _score_cue(text: str) -> tuple[float, list[str], list[str]]:
    """1 cue のスコア、根拠ラベル、ヒットしたキーワードを返す."""
    if not text:
        return 0.0, [], []
    score = 0.0
    rationale: list[str] = []
    keywords: list[str] = []

    for kw, w in _KEYWORD_WEIGHTS.items():
        if kw in text:
            score += w
            keywords.append(kw)

    excls = len(_EXCLAMATION_PATTERN.findall(text))
    if excls:
        score += min(excls * 0.2, 0.8)
        rationale.append(f"exclaim*{excls}")

    laughs = len(_LAUGHTER_PATTERN.findall(text))
    if laughs:
        score += min(laughs * 0.3, 0.6)
        rationale.append(f"laughter*{laughs}")

    if keywords:
        rationale.append("kw:" + ",".join(keywords))

    return score, rationale, keywords


# ---------- 窓抽出 ----------


def _merge_window(
    cues: list[Cue],
    start_idx: int,
    target_duration: float,
) -> tuple[int, int]:
    """start_idx から target_duration を満たすまで前後に伸ばし、最終的な (start, end) を返す."""
    if not cues:
        return start_idx, start_idx
    left = right = start_idx
    while right + 1 < len(cues) and cues[right].end_sec - cues[left].start_sec < target_duration:
        right += 1
    # 必要なら左にも伸ばす
    while left > 0 and cues[right].end_sec - cues[left - 1].start_sec <= target_duration:
        left -= 1
    return left, right


def detect_highlights(
    cues: list[Cue],
    *,
    target_format: str = TARGET_SHORT,
    short_window_sec: float = 50.0,
    long_window_sec: float = 120.0,
    max_long_windows: int = 5,
    long_total_target_sec: float = 600.0,
    min_total_sec: float | None = None,
    min_score: float = 0.3,
) -> list[Highlight]:
    """字幕 cues からハイライト窓を抽出する.

    short: 最高スコア窓 1 件 (≤60s)
    long:  上位 N 窓 (合計 8〜12 分目安)
    """
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}")
    if not cues:
        return []

    cue_scores = [_score_cue(c.text) for c in cues]

    if target_format == TARGET_SHORT:
        best_idx = max(range(len(cues)), key=lambda i: cue_scores[i][0])
        best_score = cue_scores[best_idx][0]
        if best_score < min_score:
            return []
        left, right = _merge_window(cues, best_idx, short_window_sec)
        kws: list[str] = []
        rationale: list[str] = []
        agg_score = 0.0
        for i in range(left, right + 1):
            s, r, k = cue_scores[i]
            agg_score += s
            rationale.extend(r)
            kws.extend(k)
        start = max(cues[left].start_sec - 2.0, 0.0)
        end = min(cues[right].end_sec + 2.0, cues[left].start_sec + 60.0)
        return [
            Highlight(
                start_sec=start,
                end_sec=end,
                score=round(agg_score, 3),
                rationale=sorted(set(rationale)),
                keywords=sorted(set(kws)),
            )
        ]

    # long
    candidates: list[Highlight] = []
    for i, c in enumerate(cues):
        s, r, k = cue_scores[i]
        if s < min_score:
            continue
        left, right = _merge_window(cues, i, long_window_sec)
        agg_score = 0.0
        kws: list[str] = []
        rationale: list[str] = []
        for j in range(left, right + 1):
            s2, r2, k2 = cue_scores[j]
            agg_score += s2
            rationale.extend(r2)
            kws.extend(k2)
        candidates.append(
            Highlight(
                start_sec=cues[left].start_sec,
                end_sec=cues[right].end_sec,
                score=round(agg_score, 3),
                rationale=sorted(set(rationale)),
                keywords=sorted(set(kws)),
            )
        )

    candidates.sort(key=lambda h: h.score, reverse=True)
    picked: list[Highlight] = []
    total = 0.0
    total_target = long_total_target_sec if min_total_sec is None else min_total_sec

    for h in candidates:
        if any(_overlaps(h, p) for p in picked):
            continue
        picked.append(h)
        total += h.duration_sec
        if total >= total_target:
            break
        if min_total_sec is None and len(picked) >= max_long_windows:
            break

    picked.sort(key=lambda h: h.start_sec)
    return picked


def _overlaps(a: Highlight, b: Highlight) -> bool:
    return not (a.end_sec <= b.start_sec or b.end_sec <= a.start_sec)


def format_duration(sec: float) -> str:
    return str(timedelta(seconds=int(sec)))
