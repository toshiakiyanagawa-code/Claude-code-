"""政治系切り抜きハイライト選定のための多軸スコアリング.

2026-05-14 codex リサーチ後の方針 + 同日 review 後の修正:
- 政治密度(人名・党名・省庁・政策語・数字) を主軸に
- 配信運営語は hard/soft に分離 (スパチャ等は soft)
- 辞書は canonical entity → aliases。最長一致でカウント、entity 単位で 1 票
- 候補窓は 10s stride、35/45/60 (short) または 90/120/150 (long) 秒
- 政治密度は窓長で正規化 (per 60 sec)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .highlights import Cue, Highlight, TARGET_SHORT, TARGET_LONG, VALID_TARGETS

_DICT_PATH = Path(__file__).parent / "data" / "political_dict.json"


@dataclass
class EntityList:
    """canonical -> list[alias]。長い alias から走査して最長一致 1 票で済ます."""

    entries: list[tuple[str, list[str]]]  # [(canonical, [alias...sorted desc by length]), ...]

    @classmethod
    def from_objs(cls, objs: list[dict | str]) -> "EntityList":
        normalized: list[tuple[str, list[str]]] = []
        for o in objs:
            if isinstance(o, str):
                normalized.append((o, [o]))
            else:
                aliases = sorted(set(o.get("aliases") or [o["canonical"]]), key=len, reverse=True)
                normalized.append((o["canonical"], aliases))
        return cls(entries=normalized)

    def count_hits(self, text: str) -> tuple[int, list[str]]:
        """テキスト中の entity ヒット数と canonical 名リストを返す.

        alias の最長一致でその entity を 1 票としてカウント。
        textから一度マッチした文字位置はマスクして他 entity との二重カウントを防ぐ。
        """
        mask = bytearray(len(text), )
        # actually use char-index list
        consumed = [False] * len(text)
        matched: list[str] = []
        for canonical, aliases in self.entries:
            hit = False
            for alias in aliases:
                start = 0
                while True:
                    idx = text.find(alias, start)
                    if idx < 0:
                        break
                    if any(consumed[idx:idx + len(alias)]):
                        start = idx + 1
                        continue
                    # mark consumed
                    for k in range(idx, idx + len(alias)):
                        consumed[k] = True
                    hit = True
                    break  # entity 1 票
                if hit:
                    break
            if hit:
                matched.append(canonical)
        return len(matched), matched


@dataclass
class PoliticalDict:
    politicians: EntityList
    parties: EntityList
    ministries: EntityList
    policy_terms: EntityList
    international: EntityList
    incident_terms: EntityList
    number_patterns: list[re.Pattern]
    hook_phrases: EntityList
    conflict_phrases: EntityList
    hard_admin_phrases: EntityList
    soft_admin_phrases: EntityList
    weights: dict[str, float]

    @classmethod
    def load(cls, path: Path = _DICT_PATH) -> "PoliticalDict":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            politicians=EntityList.from_objs(data["politicians"]),
            parties=EntityList.from_objs(data["parties"]),
            ministries=EntityList.from_objs(data["ministries"]),
            policy_terms=EntityList.from_objs(data["policy_terms"]),
            international=EntityList.from_objs(data["international"]),
            incident_terms=EntityList.from_objs(data["incident_terms"]),
            number_patterns=[re.compile(p) for p in data["number_patterns"]],
            hook_phrases=EntityList.from_objs(data["hook_phrases"]),
            conflict_phrases=EntityList.from_objs(data["conflict_phrases"]),
            hard_admin_phrases=EntityList.from_objs(data["hard_admin_phrases"]),
            soft_admin_phrases=EntityList.from_objs(data["soft_admin_phrases"]),
            weights=data["weights"],
        )


@dataclass
class WindowFeatures:
    """窓 1 個の評価特徴量."""

    start_sec: float
    end_sec: float
    text: str
    head_text: str  # 冒頭 5 秒分

    political_density: float = 0.0  # per-60-sec normalized
    specificity: float = 0.0
    hook_score: float = 0.0
    conflict_score: float = 0.0
    incident_score: float = 0.0
    hard_admin_penalty: float = 0.0
    soft_admin_penalty: float = 0.0
    legacy_keyword_score: float = 0.0

    matched_politicians: list[str] = field(default_factory=list)
    matched_parties: list[str] = field(default_factory=list)
    matched_policies: list[str] = field(default_factory=list)
    matched_numbers: list[str] = field(default_factory=list)
    matched_admin_hard: list[str] = field(default_factory=list)
    matched_admin_soft: list[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return max(self.end_sec - self.start_sec, 0.0)

    def composite_score(self) -> float:
        """評価軸の重み付き合算。admin_penalty は減算。"""
        return (
            0.20 * self.political_density
            + 0.14 * self.specificity
            + 0.18 * self.hook_score
            + 0.15 * self.conflict_score
            + 0.08 * self.incident_score
            + 0.05 * self.legacy_keyword_score
            - 1.0 * self.hard_admin_penalty
            - 1.0 * self.soft_admin_penalty
        )

    def as_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 2),
            "end_sec": round(self.end_sec, 2),
            "duration_sec": round(self.duration_sec, 2),
            "composite_score": round(self.composite_score(), 3),
            "political_density": round(self.political_density, 3),
            "specificity": round(self.specificity, 3),
            "hook_score": round(self.hook_score, 3),
            "conflict_score": round(self.conflict_score, 3),
            "incident_score": round(self.incident_score, 3),
            "hard_admin_penalty": round(self.hard_admin_penalty, 3),
            "soft_admin_penalty": round(self.soft_admin_penalty, 3),
            "legacy_keyword_score": round(self.legacy_keyword_score, 3),
            "matched_politicians": self.matched_politicians,
            "matched_parties": self.matched_parties,
            "matched_policies": self.matched_policies,
            "matched_numbers": self.matched_numbers,
            "matched_admin_hard": self.matched_admin_hard,
            "matched_admin_soft": self.matched_admin_soft,
            "head_text": self.head_text[:200],
        }


def _count_pattern_hits(text: str, patterns: list[re.Pattern]) -> tuple[int, list[str]]:
    matches: list[str] = []
    for p in patterns:
        matches.extend(p.findall(text))
    return len(matches), matches


def score_window(
    window_text: str,
    head_text: str,
    pdict: PoliticalDict,
    window_dur: float = 60.0,
) -> WindowFeatures:
    """窓全体 + 冒頭の特徴量を計算した WindowFeatures を返す (start/end は呼出側で設定)."""
    wf = WindowFeatures(start_sec=0.0, end_sec=window_dur, text=window_text, head_text=head_text)

    # 政治密度 (raw count → per-60-sec normalization)
    p_count, p_match = pdict.politicians.count_hits(window_text)
    party_count, party_match = pdict.parties.count_hits(window_text)
    min_count, min_match = pdict.ministries.count_hits(window_text)
    pol_count, pol_match = pdict.policy_terms.count_hits(window_text)
    intl_count, intl_match = pdict.international.count_hits(window_text)
    raw_density = (
        p_count * pdict.weights.get("politician", 1.5)
        + party_count * pdict.weights.get("party", 1.2)
        + min_count * pdict.weights.get("ministry", 1.0)
        + pol_count * pdict.weights.get("policy_term", 1.0)
        + intl_count * pdict.weights.get("international", 1.0)
    )
    norm = 60.0 / max(window_dur, 1.0)
    wf.political_density = raw_density * norm
    wf.matched_politicians = p_match
    wf.matched_parties = party_match
    wf.matched_policies = pol_match

    # 具体性
    num_count, num_match = _count_pattern_hits(window_text, pdict.number_patterns)
    raw_spec = (
        num_count * pdict.weights.get("number_pattern", 0.8)
        + (p_count + party_count + min_count) * 0.3
    )
    wf.specificity = raw_spec * norm
    wf.matched_numbers = num_match

    # フック性 (冒頭 5 秒は norm 適用しない; 短いほうが集中している)
    head_hook_count, _ = pdict.hook_phrases.count_hits(head_text)
    full_hook_count, _ = pdict.hook_phrases.count_hits(window_text)
    wf.hook_score = head_hook_count * pdict.weights.get("hook_phrase", 0.6) * 2.0 + (
        full_hook_count - head_hook_count
    ) * pdict.weights.get("hook_phrase", 0.6)

    # 対立
    conf_count, _ = pdict.conflict_phrases.count_hits(window_text)
    wf.conflict_score = conf_count * pdict.weights.get("conflict_phrase", 0.7) * norm

    # 事件性
    inc_count, _ = pdict.incident_terms.count_hits(window_text)
    wf.incident_score = inc_count * pdict.weights.get("incident_term", 1.3) * norm

    # admin hard / soft
    hard_count, hard_match = pdict.hard_admin_phrases.count_hits(window_text)
    wf.hard_admin_penalty = hard_count * abs(pdict.weights.get("hard_admin", -2.0))
    wf.matched_admin_hard = hard_match

    soft_count, soft_match = pdict.soft_admin_phrases.count_hits(window_text)
    wf.soft_admin_penalty = soft_count * abs(pdict.weights.get("soft_admin", -0.4))
    wf.matched_admin_soft = soft_match

    return wf


def generate_candidate_windows(
    cues: list[Cue],
    *,
    target_format: str = TARGET_SHORT,
    window_lens: tuple[float, ...] | None = None,
    stride_sec: float = 10.0,
) -> list[tuple[float, float]]:
    """sliding window で候補(start, end) を量産。末尾を必ず含める."""
    if target_format not in VALID_TARGETS:
        raise ValueError(f"target_format must be one of {VALID_TARGETS}")
    if not cues:
        return []
    if window_lens is None:
        window_lens = (35.0, 45.0, 60.0) if target_format == TARGET_SHORT else (90.0, 120.0, 150.0)

    total_start = cues[0].start_sec
    total_end = cues[-1].end_sec
    windows: list[tuple[float, float]] = []
    for wlen in window_lens:
        if total_end - total_start < wlen:
            # 入力が短すぎ → 全区間を 1 個だけ
            windows.append((total_start, total_end))
            continue
        s = total_start
        while s + wlen <= total_end:
            windows.append((s, s + wlen))
            s += stride_sec
        # 末尾 window が抜けていれば足す
        tail_start = total_end - wlen
        if windows[-1][0] < tail_start:
            windows.append((tail_start, total_end))
    return windows


def text_in_window(cues: list[Cue], start: float, end: float) -> str:
    parts = []
    for c in cues:
        if c.end_sec <= start or c.start_sec >= end:
            continue
        parts.append(c.text)
    return " ".join(parts).strip()


def select_political_highlights(
    cues: list[Cue],
    *,
    target_format: str = TARGET_SHORT,
    pdict: PoliticalDict | None = None,
    top_k: int = 12,
    min_composite_score: float = 0.0,
    long_total_target_sec: float = 600.0,
    max_long_windows: int = 5,
) -> tuple[list[Highlight], list[WindowFeatures]]:
    """新しい政治密度ベースでハイライトを選ぶ.

    Returns: (selected highlights, all scored windows)
    """
    if not cues:
        return [], []
    if pdict is None:
        pdict = PoliticalDict.load()

    from .highlights import _score_cue
    cue_kw_scores = [_score_cue(c.text)[0] for c in cues]

    windows = generate_candidate_windows(cues, target_format=target_format)
    features: list[WindowFeatures] = []
    for start, end in windows:
        body = text_in_window(cues, start, end)
        head = text_in_window(cues, start, start + 5.0)
        wf = score_window(body, head, pdict, window_dur=end - start)
        wf.start_sec = start
        wf.end_sec = end
        # legacy
        norm = 60.0 / max(end - start, 1.0)
        wf.legacy_keyword_score = (
            sum(
                kw_score
                for c, kw_score in zip(cues, cue_kw_scores)
                if c.end_sec > start and c.start_sec < end
            )
            * norm
        )
        features.append(wf)

    features.sort(key=lambda w: w.composite_score(), reverse=True)
    qualified = [w for w in features if w.composite_score() >= min_composite_score]

    if target_format == TARGET_SHORT:
        if not qualified:
            return [], features
        top = qualified[0]
        h = Highlight(
            start_sec=top.start_sec,
            end_sec=top.end_sec,
            score=round(top.composite_score(), 3),
            rationale=[
                f"political_density={top.political_density:.2f}",
                f"specificity={top.specificity:.2f}",
                f"hook={top.hook_score:.2f}",
                f"conflict={top.conflict_score:.2f}",
                f"incident={top.incident_score:.2f}",
                f"hard_admin={top.hard_admin_penalty:.2f}",
                f"soft_admin={top.soft_admin_penalty:.2f}",
            ],
            keywords=top.matched_politicians + top.matched_parties + top.matched_policies,
        )
        return [h], features

    # long: 上位 N 件、overlap > 50% は除外
    selected: list[WindowFeatures] = []
    total_dur = 0.0
    for cand in qualified:
        overlap = False
        for sel in selected:
            inter = max(0.0, min(cand.end_sec, sel.end_sec) - max(cand.start_sec, sel.start_sec))
            if cand.duration_sec > 0 and inter / cand.duration_sec > 0.5:
                overlap = True
                break
        if overlap:
            continue
        selected.append(cand)
        total_dur += cand.duration_sec
        if len(selected) >= max_long_windows or total_dur >= long_total_target_sec:
            break

    selected.sort(key=lambda w: w.start_sec)
    highlights = [
        Highlight(
            start_sec=w.start_sec,
            end_sec=w.end_sec,
            score=round(w.composite_score(), 3),
            rationale=[
                f"political_density={w.political_density:.2f}",
                f"specificity={w.specificity:.2f}",
                f"hook={w.hook_score:.2f}",
            ],
            keywords=w.matched_politicians + w.matched_parties + w.matched_policies,
        )
        for w in selected
    ]
    return highlights, features


def apply_overlap_filter(
    rerank_items: list,
    *,
    max_windows: int = 5,
    overlap_threshold: float = 0.5,
) -> list:
    """LLM rerank 後の long で同じ箇所が複数選ばれないようにする.

    rerank_items: (.start_sec, .end_sec) を持つオブジェクトのリスト (LLM スコア降順前提)
    """
    selected = []
    for r in rerank_items:
        skip = False
        for s in selected:
            inter = max(0.0, min(r.end_sec, s.end_sec) - max(r.start_sec, s.start_sec))
            duration = r.end_sec - r.start_sec
            if duration > 0 and inter / duration > overlap_threshold:
                skip = True
                break
        if skip:
            continue
        selected.append(r)
        if len(selected) >= max_windows:
            break
    return selected
