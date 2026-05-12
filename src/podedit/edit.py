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

ESS_SCHEMA_VERSION = 1


@dataclass(slots=True)
class DeleteOp:
    op_id: str
    op: Literal["delete"]
    start: float  # inclusive, seconds on source timeline
    end: float    # exclusive, seconds on source timeline
    note: str | None = None


@dataclass(slots=True)
class EditSession:
    schema_version: int
    timeline_basis: Literal["source_audio_seconds"]
    source_audio: AudioRef
    transcript_ref: str | None  # path to transcript JSON (if any)
    created_at: str
    ops: list[DeleteOp] = field(default_factory=list)

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

    @classmethod
    def from_dict(cls, data: dict) -> "EditSession":
        ver = data.get("schema_version")
        if ver != ESS_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported EditSession schema_version {ver!r} (expected {ESS_SCHEMA_VERSION})"
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
        ops = [
            DeleteOp(
                op_id=o["op_id"],
                op=o["op"],
                start=float(o["start"]),
                end=float(o["end"]),
                note=o.get("note"),
            )
            for o in data.get("ops", [])
        ]
        return cls(
            schema_version=ver,
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
