"""SRT 字幕の文字起こし精度を上げる後処理.

2026-05-15 codex 推奨 Step 1:
- 固有名詞辞書による誤認識補正 (context-aware)
- 改行位置の保護 (固有名詞・肩書を跨いで切らない)
- 助詞・句読点直後で改行

辞書: src/clipgen/data/transcript_corrections.json
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_DICT_PATH = Path(__file__).parent / "data" / "transcript_corrections.json"

# 改行候補スコアリング用
_PARTICLES = set("はがをにでともへからまでより")
_PUNCTS = set("。、！？!?")
_DEFAULT_SUFFIXES = ("記者", "氏", "さん", "首相", "総理", "議員", "代表", "大臣", "知事")


@dataclass
class Term:
    canonical: str
    category: str
    aliases: list[str]
    asr_confusions: list[str]
    context_terms: list[str]
    protect_suffixes: list[str]


def load_terms(path: Path = _DICT_PATH) -> list[Term]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Term(
            canonical=t["canonical"],
            category=t.get("category", ""),
            aliases=t.get("aliases", []),
            asr_confusions=t.get("asr_confusions", []),
            context_terms=t.get("context_terms", []),
            protect_suffixes=t.get("protect_suffixes", []),
        )
        for t in data.get("terms", [])
    ]


def correct_text(text: str, terms: list[Term], context: str = "") -> str:
    """ASR の固有名詞誤認識を正規表記に置換 (context-aware).

    context: 周辺 cue や番組タイトルなど、文脈チェック用テキスト (ローカル限定推奨)

    短い confusion (≤2 文字) は context 必須 + 2 件以上ヒットを要求。
    confusion は長い順に処理して部分一致による誤置換を防ぐ。
    """
    out = text
    full_context = (text + " " + context)
    for term in terms:
        # 長い confusion から処理 (短い誤一致を防ぐ)
        sorted_confusions = sorted(term.asr_confusions, key=len, reverse=True)
        for confusion in sorted_confusions:
            if confusion not in out:
                continue
            # context check: 関連語が周辺にあるか
            if term.context_terms:
                hits = sum(1 for c in term.context_terms if c in full_context)
                # 短い confusion (≤2 chars) は 2 件以上の context 必須
                required = 2 if len(confusion) <= 2 else 1
                if hits < required:
                    continue
            out = out.replace(confusion, term.canonical)
    return out


def protected_spans(text: str, terms: list[Term]) -> list[tuple[int, int]]:
    """text 内で改行禁止のスパン (start, end) のリストを返す.

    canonical/alias の出現位置 + 直後の肩書/接尾辞まで含める。
    """
    spans: list[tuple[int, int]] = []
    for term in terms:
        surfaces = [term.canonical] + term.aliases
        # default + term-specific の union (codex review 反映)
        all_suffixes = list(dict.fromkeys(list(_DEFAULT_SUFFIXES) + list(term.protect_suffixes)))
        for surface in surfaces:
            start = 0
            while True:
                idx = text.find(surface, start)
                if idx < 0:
                    break
                end = idx + len(surface)
                # 直後の肩書 (記者/氏/さん 等) を含める
                for suffix in sorted(all_suffixes, key=len, reverse=True):
                    if text[end:end + len(suffix)] == suffix:
                        end += len(suffix)
                        break
                spans.append((idx, end))
                start = end
    # マージ重複
    if not spans:
        return spans
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


def smart_wrap(
    text: str,
    terms: list[Term] | None = None,
    max_chars: int = 17,
    max_lines: int = 2,
) -> str:
    """改行を入れる. 固有名詞スパン回避 + 助詞/句読点優先."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    if terms is None:
        terms = []

    spans = protected_spans(text, terms)

    def in_span(i: int) -> bool:
        return any(a < i < b for a, b in spans)

    lines: list[str] = []
    remaining = text
    while len(remaining) > max_chars and len(lines) < max_lines - 1:
        target = max_chars
        best = None
        # 候補は target-4 から target+2 まで
        for i in range(max(target - 5, 2), min(target + 3, len(remaining) - 1)):
            if in_span(i):
                continue
            score = -abs(i - target)
            prev_ch = remaining[i - 1]
            cur_ch = remaining[i]
            if prev_ch in _PUNCTS:
                score += 50
            if prev_ch in _PARTICLES:
                score += 18
            # canonical 末尾で切る
            if any(remaining[:i].endswith(t.canonical) for t in terms):
                score += 10
            # その後が句読点なら避ける (句読点を行頭に置かない)
            if cur_ch in _PUNCTS:
                score -= 25
            if best is None or score > best[0]:
                best = (score, i)
        if not best:
            # fallback: hard cut at max_chars (避けるべきだが)
            cut = max_chars
            # Try to find any non-span cut point
            for i in range(max_chars, min(max_chars + 5, len(remaining))):
                if not in_span(i):
                    cut = i
                    break
        else:
            cut = best[1]
        lines.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        # 最後の行は欠落させずに全文保持 (字幕欠落のほうが表示超過より重大、codex review 反映)
        lines.append(remaining)
    return "\n".join(lines)


def process_srt_cues(
    cues: list[tuple[float, float, str]],
    terms: list[Term] | None = None,
    program_context: str = "",
    max_chars_per_line: int = 17,
    max_lines: int = 2,
) -> list[tuple[float, float, str]]:
    """cue 列に対して、辞書補正 + 改行を一括適用.

    cues: [(start_sec, end_sec, text), ...]
    program_context: 動画タイトル等、context_terms 判定用テキスト
    """
    if terms is None:
        terms = load_terms()
    out = []
    # codex review 反映: 全 cue 結合ではなく、現 cue + 前後 2 cue + program_context をローカル文脈に
    for i, (s, e, t) in enumerate(cues):
        neighbors = []
        if i >= 2:
            neighbors.append(cues[i - 2][2])
        if i >= 1:
            neighbors.append(cues[i - 1][2])
        if i + 1 < len(cues):
            neighbors.append(cues[i + 1][2])
        if i + 2 < len(cues):
            neighbors.append(cues[i + 2][2])
        local_context = " ".join(neighbors) + " " + program_context
        corrected = correct_text(t, terms, context=local_context)
        wrapped = smart_wrap(corrected, terms, max_chars=max_chars_per_line, max_lines=max_lines)
        out.append((s, e, wrapped))
    return out
