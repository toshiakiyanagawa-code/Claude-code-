"""Edit-session data model and helpers (W2).

An ``EditSession`` is the append-only ops log that maps from a source audio to
a rendered output. Ops carry timestamps in *source audio seconds* — never in
transcript word IDs — so re-running ASR does not invalidate them.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .schema import AudioRef, now_iso

ESS_SCHEMA_VERSION = 2


@dataclass(slots=True)
class DeleteOp:
    op_id: str
    op: Literal["delete"]
    start: float  # inclusive, seconds on source timeline
    end: float    # exclusive, seconds on source timeline
    note: str | None = None


@dataclass(slots=True)
class MoveOp:
    op_id: str
    op: Literal["move"]
    src_start: float
    src_end: float
    target_edited_t: float
    note: str | None = None


@dataclass(slots=True)
class TimelineSegment:
    source_start: float
    source_end: float
    edited_start: float
    edited_end: float
    origin_op_id: str | None = None


EditOp = DeleteOp | MoveOp


@dataclass(slots=True)
class EditSession:
    schema_version: int
    timeline_basis: Literal["source_audio_seconds"]
    source_audio: AudioRef
    transcript_ref: str | None  # path to transcript JSON (if any)
    created_at: str
    ops: list[EditOp] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def new(cls, source_audio: AudioRef, transcript_ref: str | None = None) -> "EditSession":
        return cls(
            schema_version=ESS_SCHEMA_VERSION,
            timeline_basis="source_audio_seconds",
            source_audio=source_audio,
            transcript_ref=transcript_ref,
            created_at=now_iso(),
            ops=[],
        )

    def add_delete(self, start: float, end: float, note: str | None = None) -> DeleteOp:
        if end <= start:
            raise ValueError(f"delete end ({end}) must be > start ({start})")
        op = DeleteOp(op_id=f"op-{uuid.uuid4().hex[:8]}", op="delete", start=start, end=end, note=note)
        self.ops.append(op)
        return op

    def add_move(
        self,
        src_start: float,
        src_end: float,
        target_edited_t: float,
        note: str | None = None,
    ) -> MoveOp:
        if src_end <= src_start:
            raise ValueError(f"move src_end ({src_end}) must be > src_start ({src_start})")
        if target_edited_t < 0:
            raise ValueError(f"move target_edited_t ({target_edited_t}) must be >= 0")
        op = MoveOp(
            op_id=f"op-{uuid.uuid4().hex[:8]}",
            op="move",
            src_start=src_start,
            src_end=src_end,
            target_edited_t=target_edited_t,
            note=note,
        )
        self.ops.append(op)
        return op

    @classmethod
    def from_dict(cls, data: dict) -> "EditSession":
        ver = data.get("schema_version")
        if ver not in (1, 2):
            raise ValueError(
                f"Unsupported EditSession schema_version {ver!r} (expected 1 or {ESS_SCHEMA_VERSION})"
            )
        src = data["source_audio"]
        source_audio = AudioRef(
            path=src["path"],
            duration_sec=float(src["duration_sec"]),
            sample_rate=int(src["sample_rate"]),
            channels=int(src["channels"]),
            codec=src["codec"],
            sha256=src.get("sha256"),
        )
        ops: list[EditOp] = []
        for o in data.get("ops", []):
            if o["op"] == "delete":
                ops.append(DeleteOp(
                    op_id=o["op_id"],
                    op=o["op"],
                    start=float(o["start"]),
                    end=float(o["end"]),
                    note=o.get("note"),
                ))
            elif ver == 2 and o["op"] == "move":
                ops.append(MoveOp(
                    op_id=o["op_id"],
                    op=o["op"],
                    src_start=float(o["src_start"]),
                    src_end=float(o["src_end"]),
                    target_edited_t=float(o["target_edited_t"]),
                    note=o.get("note"),
                ))
            else:
                raise ValueError(f"Unsupported op {o.get('op')!r} for schema_version {ver}")
        return cls(
            schema_version=ESS_SCHEMA_VERSION,
            timeline_basis=data["timeline_basis"],
            source_audio=source_audio,
            transcript_ref=data.get("transcript_ref"),
            created_at=data["created_at"],
            ops=ops,
        )


def sha256_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def keep_ranges_from_deletes(
    duration: float,
    deletes: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return the non-overlapping keep ranges given a list of delete ranges.

    Delete ranges may overlap or be unsorted; they are merged before
    complementing. The result tiles ``[0, duration)`` minus the merged deletes.
    """
    if duration <= 0:
        return []
    cleaned: list[tuple[float, float]] = []
    for s, e in deletes:
        s_c = max(0.0, s)
        e_c = min(duration, e)
        if e_c > s_c:  # filter AFTER clamping so fully out-of-range ranges drop out
            cleaned.append((s_c, e_c))
    if not cleaned:
        return [(0.0, duration)]
    cleaned.sort()
    merged: list[tuple[float, float]] = [cleaned[0]]
    for s, e in cleaned[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))

    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if s > cursor:
            keeps.append((cursor, s))
        cursor = e
    if cursor < duration:
        keeps.append((cursor, duration))
    return keeps


def compile_timeline(source_duration: float, ops: list[EditOp]) -> list[TimelineSegment]:
    """Replay primitive edit ops into edited-order source segments.

    ``MoveOp.target_edited_t`` is interpreted on the edited timeline that exists
    immediately before that move is applied. After cutting the moved material,
    the insertion point is translated into the post-cut timeline.
    """
    if source_duration <= 0:
        return []
    segments = [TimelineSegment(0.0, source_duration, 0.0, source_duration)]
    for op in ops:
        if op.op == "delete":
            segments, _ = _cut_source_range(segments, op.start, op.end, None)
        elif op.op == "move":
            if _target_inside_source_range(segments, op.target_edited_t, op.src_start, op.src_end):
                continue
            segments, cut = _cut_source_range(segments, op.src_start, op.src_end, op.op_id)
            if cut:
                target = _translate_target_after_cut(op.target_edited_t, cut)
                segments = _insert_segments_at(segments, cut, target)
        segments = _renumber_segments(segments)
    return segments


def _renumber_segments(segments: list[TimelineSegment]) -> list[TimelineSegment]:
    edited = 0.0
    out: list[TimelineSegment] = []
    for seg in segments:
        length = seg.source_end - seg.source_start
        if length <= 0:
            continue
        out.append(TimelineSegment(
            seg.source_start,
            seg.source_end,
            edited,
            edited + length,
            seg.origin_op_id,
        ))
        edited += length
    return out


def _cut_source_range(
    segments: list[TimelineSegment],
    start: float,
    end: float,
    moved_origin_op_id: str | None,
) -> tuple[list[TimelineSegment], list[TimelineSegment]]:
    kept: list[TimelineSegment] = []
    cut: list[TimelineSegment] = []
    for seg in segments:
        cut_start = max(seg.source_start, start)
        cut_end = min(seg.source_end, end)
        if cut_end <= cut_start:
            kept.append(seg)
            continue
        if seg.source_start < cut_start:
            kept.append(TimelineSegment(
                seg.source_start, cut_start, seg.edited_start,
                seg.edited_start + (cut_start - seg.source_start),
                seg.origin_op_id,
            ))
        cut.append(TimelineSegment(
            cut_start, cut_end,
            seg.edited_start + (cut_start - seg.source_start),
            seg.edited_start + (cut_end - seg.source_start),
            moved_origin_op_id if moved_origin_op_id is not None else seg.origin_op_id,
        ))
        if cut_end < seg.source_end:
            kept.append(TimelineSegment(
                cut_end, seg.source_end,
                seg.edited_start + (cut_end - seg.source_start),
                seg.edited_end,
                seg.origin_op_id,
            ))
    return _renumber_segments(kept), cut


def _translate_target_after_cut(target: float, cut: list[TimelineSegment]) -> float:
    removed_before = 0.0
    for seg in cut:
        if seg.edited_end <= target:
            removed_before += seg.edited_end - seg.edited_start
        elif seg.edited_start < target < seg.edited_end:
            removed_before += target - seg.edited_start
    return max(0.0, target - removed_before)


def _insert_segments_at(
    segments: list[TimelineSegment],
    inserts: list[TimelineSegment],
    target: float,
) -> list[TimelineSegment]:
    total = sum(seg.source_end - seg.source_start for seg in segments)
    target = max(0.0, min(target, total))
    out: list[TimelineSegment] = []
    inserted = False
    for seg in segments:
        if not inserted and target <= seg.edited_start:
            out.extend(inserts)
            inserted = True
        if not inserted and seg.edited_start < target < seg.edited_end:
            source_split = seg.source_start + (target - seg.edited_start)
            out.append(TimelineSegment(
                seg.source_start, source_split, seg.edited_start, target, seg.origin_op_id,
            ))
            out.extend(inserts)
            out.append(TimelineSegment(
                source_split, seg.source_end, target, seg.edited_end, seg.origin_op_id,
            ))
            inserted = True
        else:
            out.append(seg)
    if not inserted:
        out.extend(inserts)
    return _renumber_segments(out)


def _target_inside_source_range(
    segments: list[TimelineSegment],
    target: float,
    source_start: float,
    source_end: float,
) -> bool:
    for seg in segments:
        if seg.edited_start <= target < seg.edited_end:
            src = seg.source_start + (target - seg.edited_start)
            return source_start <= src < source_end
    return False
