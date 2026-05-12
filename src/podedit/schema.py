"""Transcript JSON schema (v1).

Timestamps are anchored to the *original* (source) audio timeline in seconds.
The 16kHz mono copy used for ASR is recorded separately as a derived artifact;
edits/renders must reference ``source_audio.path``, not ``asr_audio.path``.

Stable IDs on segments and words are required by edit-ops and UI selection in
later weeks; do not regenerate them when re-saving the same transcript.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = 1


@dataclass(slots=True)
class AudioRef:
    path: str
    duration_sec: float
    sample_rate: int
    channels: int
    codec: str
    sha256: str | None = None  # populated when audio is the source of truth for an EditSession


@dataclass(slots=True)
class ModelConfig:
    model: str
    language: str
    requested_device: str
    resolved_device: str
    compute_type: str
    beam_size: int
    vad_filter: bool


@dataclass(slots=True)
class Word:
    id: str  # stable within this transcript file, format: "s{seg}-w{idx}"
    start: float
    end: float
    text: str
    confidence: float | None = None


@dataclass(slots=True)
class Segment:
    id: str  # stable within this transcript file, format: "s{idx}"
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass(slots=True)
class Transcript:
    schema_version: int
    source_audio: AudioRef  # source of truth for editing/rendering
    asr_audio: AudioRef     # 16kHz mono derived copy used by Whisper
    language: str
    model_config: ModelConfig
    created_at: str         # ISO 8601 UTC
    segments: list[Segment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
