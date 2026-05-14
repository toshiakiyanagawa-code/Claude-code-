"""Post-processing dictionary replacement for ASR transcripts.

編集者が登録した「ASR の出力 → 正しい綴り」のペアを、文字起こし完了直後に
transcript に適用するモジュール。

設計 (codex 相談):
  - 保存先: ``<work_dir>/dictionary.json`` (編集部共有のグローバル辞書)
  - マッチ単位: 連続 N Word の完全一致 (NFKC + 空白除去 + casefold)
  - アルゴリズム: Segment 内で左から右へ greedy、各位置で最長一致優先
  - 適用: transcript.json 書き出し前に永続適用
  - 履歴: ``<stem>.transcript.dict-ops.jsonl`` に sidecar として残す (undo 用)

過剰置換のリスクは「完全一致 + 最長一致 + sidecar undo」で抑える。
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DICTIONARY_VERSION = 1
DICT_OPS_VERSION = 1


@dataclass(frozen=True, slots=True)
class DictEntry:
    id: str
    from_: str
    to: str
    enabled: bool = True
    priority: int = 100
    max_words: int = 5
    max_conf: float | None = None

    @property
    def from_normalized(self) -> str:
        return _normalize(self.from_)


@dataclass(frozen=True, slots=True)
class Dictionary:
    entries: tuple[DictEntry, ...]

    def active_sorted(self) -> list[DictEntry]:
        """Enabled entries sorted by length desc, priority desc (file order tiebreak)."""
        active = [e for e in self.entries if e.enabled and e.from_.strip()]
        return sorted(
            active,
            key=lambda e: (-len(e.from_normalized), -e.priority),
        )


def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "").casefold()


def load_dictionary(path: Path) -> Dictionary:
    """Load dictionary from JSON. Returns empty Dictionary if file is missing or empty."""
    if not path.exists():
        return Dictionary(entries=())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Dictionary(entries=())
    raw_entries = data.get("entries") or []
    entries: list[DictEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            continue
        from_ = raw.get("from")
        to = raw.get("to")
        if not isinstance(from_, str) or not isinstance(to, str):
            continue
        if not from_.strip():
            continue
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            entry_id = f"dict_{i:04d}"
        enabled = raw.get("enabled", True)
        priority = raw.get("priority", 100)
        max_words = raw.get("max_words", 5)
        max_conf = raw.get("max_conf")
        entries.append(
            DictEntry(
                id=entry_id,
                from_=from_,
                to=to,
                enabled=bool(enabled) if isinstance(enabled, bool) else True,
                priority=int(priority) if isinstance(priority, int) and not isinstance(priority, bool) else 100,
                max_words=int(max_words) if isinstance(max_words, int) and not isinstance(max_words, bool) else 5,
                max_conf=float(max_conf) if isinstance(max_conf, (int, float)) and not isinstance(max_conf, bool) else None,
            )
        )
    return Dictionary(entries=tuple(entries))


def save_dictionary(path: Path, dictionary: Dictionary) -> None:
    payload = {
        "version": DICTIONARY_VERSION,
        "entries": [
            {
                "id": e.id,
                "from": e.from_,
                "to": e.to,
                "enabled": e.enabled,
                "priority": e.priority,
                "max_words": e.max_words,
                "max_conf": e.max_conf,
            }
            for e in dictionary.entries
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_dictionary(
    transcript_dict: dict[str, Any],
    dictionary: Dictionary,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply dictionary replacements to a transcript-shaped dict in-place-style.

    Returns ``(new_transcript_dict, ops)``. ``new_transcript_dict`` is a *new*
    dict — the input is not mutated. ``ops`` is a list of sidecar records.

    Empty dictionary → returns the input unchanged-shape and an empty ops list.
    """
    active = dictionary.active_sorted()
    if not active:
        return transcript_dict, []

    new_tx = dict(transcript_dict)
    segments = list(new_tx.get("segments") or [])
    new_segments: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []

    for seg_index, seg in enumerate(segments):
        if not isinstance(seg, dict):
            new_segments.append(seg)
            continue
        words = list(seg.get("words") or [])
        new_words, seg_ops = _apply_to_segment(words, active, seg_index)
        new_seg = dict(seg)
        new_seg["words"] = new_words
        if seg_ops:
            new_seg["text"] = _rejoin_words_text(seg.get("text"), words, new_words)
        new_segments.append(new_seg)
        ops.extend(seg_ops)

    new_tx["segments"] = new_segments
    return new_tx, ops


def _apply_to_segment(
    words: list[dict[str, Any]],
    active: list[DictEntry],
    seg_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    i = 0
    n = len(words)
    while i < n:
        matched_entry: DictEntry | None = None
        matched_span: int = 0
        for entry in active:
            span = _try_match_at(words, i, entry)
            if span > 0:
                matched_entry = entry
                matched_span = span
                break
        if matched_entry is None:
            out.append(words[i])
            i += 1
            continue
        before_words = [_word_snapshot(w) for w in words[i : i + matched_span]]
        merged = _merge_span(words[i : i + matched_span], matched_entry.to)
        out.append(merged)
        ops.append(
            {
                "version": DICT_OPS_VERSION,
                "op": "dict_replace",
                "entry_id": matched_entry.id,
                "note": f"dict:{matched_entry.from_}=>{matched_entry.to}",
                "segment_index": seg_index,
                "word_index_in_segment": len(out) - 1,
                "start": merged.get("start"),
                "end": merged.get("end"),
                "before_words": before_words,
                "after_words": [_word_snapshot(merged)],
            }
        )
        i += matched_span
    return out, ops


def _try_match_at(words: list[dict[str, Any]], pos: int, entry: DictEntry) -> int:
    """Return the number of words consumed if entry matches at pos, else 0."""
    target = entry.from_normalized
    if not target:
        return 0
    max_span = min(entry.max_words, len(words) - pos)
    concat = ""
    for span in range(1, max_span + 1):
        word = words[pos + span - 1]
        text = word.get("text") if isinstance(word, dict) else None
        if not isinstance(text, str):
            return 0
        concat += text
        if _normalize(concat) == target:
            if entry.max_conf is not None:
                if not _confidence_ok(words[pos : pos + span], entry.max_conf):
                    return 0
            return span
        if not target.startswith(_normalize(concat)):
            return 0
    return 0


def _confidence_ok(span_words: list[dict[str, Any]], max_conf: float) -> bool:
    """Return True if avg confidence of the span is <= max_conf (or any None)."""
    confs: list[float] = []
    for w in span_words:
        c = w.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            confs.append(float(c))
    if not confs:
        return True
    avg = sum(confs) / len(confs)
    return avg <= max_conf


def _merge_span(span: list[dict[str, Any]], new_text: str) -> dict[str, Any]:
    first = span[0]
    last = span[-1]
    confs = [w.get("confidence") for w in span if isinstance(w.get("confidence"), (int, float))]
    merged: dict[str, Any] = {
        "id": first.get("id"),
        "start": first.get("start"),
        "end": last.get("end"),
        "text": new_text,
    }
    if confs:
        merged["confidence"] = min(float(c) for c in confs)
    else:
        merged["confidence"] = first.get("confidence")
    return merged


def _word_snapshot(w: dict[str, Any]) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "text": w.get("text"),
        "start": w.get("start"),
        "end": w.get("end"),
    }
    if "confidence" in w:
        snap["confidence"] = w.get("confidence")
    if "id" in w:
        snap["id"] = w.get("id")
    return snap


def _rejoin_words_text(
    old_text: Any,
    old_words: list[dict[str, Any]],
    new_words: list[dict[str, Any]],
) -> str:
    """Reconstruct ``segment.text`` from words, preserving the original join style.

    日本語 transcript は通常 word を空白なしで連結するが、英語混在の場合は
    " ".join になっていることがある。faster-whisper の出力に合わせるため、
    元の ``segment.text`` が `" ".join(words)` と一致したらスペース区切りで
    再構築する。
    """
    new_raw = [w.get("text", "") for w in new_words if isinstance(w, dict)]
    if isinstance(old_text, str) and old_words:
        old_raw = [w.get("text", "") for w in old_words if isinstance(w, dict)]
        if old_text == " ".join(old_raw):
            return " ".join(new_raw)
    return "".join(new_raw)


def dict_ops_path_for(transcript_path: Path) -> Path:
    """Sidecar path for dict-ops next to the transcript.

    ``<stem>.transcript.json`` -> ``<stem>.transcript.dict-ops.jsonl``
    """
    name = transcript_path.name
    if name.endswith(".transcript.json"):
        return transcript_path.with_name(name[: -len(".json")] + ".dict-ops.jsonl")
    return transcript_path.with_name(name + ".dict-ops.jsonl")


def write_dict_ops(path: Path, ops: list[dict[str, Any]]) -> None:
    if not ops:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for op in ops:
            f.write(json.dumps(op, ensure_ascii=False) + "\n")
