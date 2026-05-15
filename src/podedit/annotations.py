"""Transcript annotation helpers for Japanese podcast editing.

The first annotation layer is W14's deterministic filler/aizuchi detector.
It is intentionally conservative: filler candidates can be recommended for
one-click deletion, but aizuchi are marked only as listening hints because
``はい``/``うん`` often carry meaning in Japanese conversation.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Literal


ANNOTATION_SCHEMA_VERSION = 1
FILLER_DETECTOR_VERSION = "ja-filler-heuristic-v1"

AnnotationType = Literal["filler", "aizuchi"]

_STRIP_RE = re.compile(r"[\s、。．，,.!?！？…・:：;；「」『』（）()［］\[\]【】\"'“”‘’\-~〜]+")

# High-confidence non-semantic fillers. These are safe enough to offer as
# delete candidates, with undo still doing the final safety work in the UI.
#
# Note: ``あの`` / ``あのー`` / ``うーん`` / ``そのー`` were initially in this
# set but codex review (2026-05-14) flagged that they can carry semantic
# weight — ``あの`` as a deictic ("あの本"), ``うーん`` as a thoughtful pause
# that shouldn't be trimmed without speaker cue context. They live in the
# weak set below until a pause / standalone heuristic is in place.
_RECOMMENDED_FILLERS = {
    "え",
    "えー",
    "あー",
    "ええっと",
    "えっと",
    "えと",
    "えーと",
    "えーっと",
    "んー",
}

# Conversation fillers that can be meaningful depending on context. We mark
# them, but keep them out of the one-click delete set.
_WEAK_FILLERS = {
    "まあ",
    "ま",
    "なんか",
    "なんというか",
    "こう",
    "その",
    "やっぱ",
    "やっぱり",
    "あの",
    "あのー",
    "そのー",
    "そのう",
    "うーん",
}

_AIZUCHI = {
    "はい",
    "うん",
    "ううん",
    "へえ",
    "へー",
    "ほう",
    "なるほど",
    "そうですね",
    "そう",
    "ですね",
    "ええ",
}


@dataclass(frozen=True, slots=True)
class Annotation:
    id: str
    type: AnnotationType
    start: float
    end: float
    text: str
    word_ids: list[str]
    confidence: float
    delete_recommended: bool
    status: Literal["pending"] = "pending"
    source: str = FILLER_DETECTOR_VERSION
    reason: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Candidate:
    type: AnnotationType
    start: float
    end: float
    text: str
    word_ids: list[str]
    confidence: float
    delete_recommended: bool
    reason: str
    segment_id: str
    word_index: int


def detect_filler_annotations(transcript_data: dict) -> list[dict]:
    """Return deterministic filler/aizuchi annotations for a transcript dict."""
    candidates: list[_Candidate] = []
    for seg_idx, seg in enumerate(transcript_data.get("segments") or []):
        words = seg.get("words") or []
        seg_id = str(seg.get("id") or f"s{seg_idx}")
        segment_words = [
            _normalize_word(str(w.get("text", "")))
            for w in words
        ]
        for word_idx, word in enumerate(words):
            candidate = _classify_word(
                word=word,
                seg_id=seg_id,
                word_idx=word_idx,
                segment_words=segment_words,
            )
            if candidate is not None:
                candidates.append(candidate)

    annotations = [
        _candidate_span_to_annotation(span).to_dict()
        for span in _merge_candidates(candidates)
    ]
    annotations.sort(key=lambda a: (a["start"], a["end"], a["id"]))
    return annotations


def build_annotation_payload(transcript_data: dict) -> dict:
    """Return the API payload for the current transcript's annotation layer."""
    return {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "source": FILLER_DETECTOR_VERSION,
        "annotations": detect_filler_annotations(transcript_data),
    }


def _classify_word(
    *,
    word: dict,
    seg_id: str,
    word_idx: int,
    segment_words: list[str],
) -> _Candidate | None:
    text = str(word.get("text", ""))
    norm = _normalize_word(text)
    if not norm:
        return None

    kind: AnnotationType | None = None
    confidence = 0.0
    delete_recommended = False
    reason: str | None = None

    if norm in _RECOMMENDED_FILLERS or _looks_like_drawn_out_filler(norm):
        kind = "filler"
        confidence = 0.93
        delete_recommended = True
        reason = "lexicon:recommended_filler"
    elif norm in _WEAK_FILLERS:
        kind = "filler"
        confidence = 0.68
        delete_recommended = False
        reason = "lexicon:weak_filler"
    elif norm in _AIZUCHI:
        kind = "aizuchi"
        # Standalone short responses are more likely to be true aizuchi, but
        # still not auto-delete material because they often signal agreement.
        nonempty_words = [w for w in segment_words if w]
        confidence = 0.82 if len(nonempty_words) <= 2 else 0.58
        delete_recommended = False
        reason = "lexicon:aizuchi"

    if kind is None or reason is None:
        return None

    try:
        start = float(word["start"])
        end = float(word["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None

    return _Candidate(
        type=kind,
        start=start,
        end=end,
        text=text,
        word_ids=[_word_id(word, seg_id, word_idx)],
        confidence=confidence,
        delete_recommended=delete_recommended,
        reason=reason,
        segment_id=seg_id,
        word_index=word_idx,
    )


def _merge_candidates(candidates: list[_Candidate]) -> list[list[_Candidate]]:
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda c: (c.start, c.end, c.segment_id, c.word_index))
    spans: list[list[_Candidate]] = [[candidates[0]]]
    for candidate in candidates[1:]:
        prev = spans[-1][-1]
        same_segment = candidate.segment_id == prev.segment_id
        adjacent_word = candidate.word_index == prev.word_index + 1
        short_gap = candidate.start - prev.end <= 0.35
        same_policy = (
            candidate.type == prev.type
            and candidate.delete_recommended == prev.delete_recommended
        )
        if same_segment and adjacent_word and short_gap and same_policy:
            spans[-1].append(candidate)
        else:
            spans.append([candidate])
    return spans


def _candidate_span_to_annotation(span: list[_Candidate]) -> Annotation:
    first = span[0]
    last = span[-1]
    word_ids = [wid for candidate in span for wid in candidate.word_ids]
    text = "".join(candidate.text for candidate in span)
    confidence = min(candidate.confidence for candidate in span)
    reason = first.reason
    if len({candidate.reason for candidate in span}) > 1:
        reason = "merged"
    ann_id = _annotation_id(first.type, word_ids)
    return Annotation(
        id=ann_id,
        type=first.type,
        start=first.start,
        end=last.end,
        text=text,
        word_ids=word_ids,
        confidence=round(confidence, 3),
        delete_recommended=all(candidate.delete_recommended for candidate in span),
        reason=reason,
        metadata={"word_count": len(word_ids)},
    )


def _annotation_id(kind: AnnotationType, word_ids: list[str]) -> str:
    raw = f"{kind}:{','.join(word_ids)}".encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()[:10]
    return f"ann-{kind}-{digest}"


def _word_id(word: dict, seg_id: str, word_idx: int) -> str:
    return str(word.get("id") or f"{seg_id}-w{word_idx}")


def _normalize_word(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _STRIP_RE.sub("", text)
    return text


def _looks_like_drawn_out_filler(norm: str) -> bool:
    if len(norm) < 2:
        return False
    chars = set(norm)
    return (
        ("ー" in chars and chars <= {"え", "ー"})
        or ("ー" in chars and chars <= {"あ", "ー"})
        or ("ー" in chars and chars <= {"ん", "ー"})
        or ("ー" in chars and chars <= {"う", "ー"})
        or len(norm) >= 3 and chars <= {"え"}
    )
