"""Library scanner (W7.6): list audio files and their transcript status.

Used by the server's ``GET /api/library`` to populate the in-UI file picker.
Scope: a flat directory of audio files plus a work directory where the
matching ``<stem>.transcript.json`` lives. We deliberately don't recurse —
the UI assumes one library directory.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_AUDIO_SUFFIXES = {".m4a", ".mp4", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus"}


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    name: str                  # filename only, e.g. "episode01.m4a"
    audio_path: str            # absolute path
    duration_sec: float | None  # None if no transcript and we couldn't probe cheaply
    has_transcript: bool
    transcript_path: str | None
    has_session: bool
    session_path: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def scan_library(audio_dir: Path, work_dir: Path) -> list[LibraryEntry]:
    """List audio files in ``audio_dir`` paired with transcript/session status from ``work_dir``."""
    entries: list[LibraryEntry] = []
    if not audio_dir.exists():
        return entries

    for path in sorted(audio_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        # Skip the faststart-remuxed derivatives the server writes back into
        # work_dir; we don't want them listed as separate library entries.
        if path.name.endswith(".faststart" + path.suffix):
            continue

        transcript_path = work_dir / f"{path.stem}.transcript.json"
        session_path = work_dir / f"{path.stem}.session.json"

        duration: float | None = None
        if transcript_path.exists():
            try:
                with transcript_path.open() as f:
                    data = json.load(f)
                src = data.get("source_audio") or {}
                if src.get("duration_sec") is not None:
                    duration = float(src["duration_sec"])
            except (OSError, ValueError, json.JSONDecodeError):
                # A malformed transcript shouldn't hide the audio entry — the
                # user can re-transcribe via the CLI.
                pass

        entries.append(LibraryEntry(
            name=path.name,
            audio_path=str(path),
            duration_sec=duration,
            has_transcript=transcript_path.exists(),
            transcript_path=str(transcript_path) if transcript_path.exists() else None,
            has_session=session_path.exists(),
            session_path=str(session_path) if session_path.exists() else None,
        ))
    return entries
