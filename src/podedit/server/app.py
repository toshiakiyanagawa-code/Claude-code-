"""FastAPI app for the local web UI (W3).

A single-tenant local service: one (audio, transcript) pair per server process,
configured at startup via ``create_app``. The UI is plain HTML + JS served from
``static/`` — no Node/Vite build step in W3. React/Vite arrives when W4 needs
proper state management for edit ops.

Audio is served with HTTP Range support so the browser ``<audio>`` element can
seek without re-downloading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..audio import probe as audio_probe
from ..edit import sha256_of_file

STATIC_DIR = Path(__file__).parent / "static"

DURATION_TOLERANCE_SEC = 0.5


class AudioTranscriptMismatch(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServeConfig:
    audio_path: Path
    transcript_path: Path


def _validate_audio_matches_transcript(audio_path: Path, transcript_data: dict) -> None:
    """Fail loudly if --audio and --transcript clearly don't belong together.

    Catches the easy footgun: `serve --audio ep1.m4a --transcript ep2.json`,
    where the UI would otherwise run with timestamps that don't line up with
    the audio. Uses SHA-256 if the transcript carries one, otherwise falls
    back to (duration, sample_rate, channels, codec) checks.
    """
    src = transcript_data.get("source_audio") or {}
    actual = audio_probe(audio_path)

    issues: list[str] = []
    expected_duration = src.get("duration_sec")
    if expected_duration is not None and abs(actual.duration_sec - float(expected_duration)) > DURATION_TOLERANCE_SEC:
        issues.append(
            f"duration {actual.duration_sec:.2f}s differs from transcript {float(expected_duration):.2f}s "
            f"(tolerance {DURATION_TOLERANCE_SEC}s)"
        )
    if src.get("sample_rate") is not None and actual.sample_rate != int(src["sample_rate"]):
        issues.append(f"sample_rate {actual.sample_rate} != transcript {src['sample_rate']}")
    if src.get("channels") is not None and actual.channels != int(src["channels"]):
        issues.append(f"channels {actual.channels} != transcript {src['channels']}")
    if src.get("codec") and actual.codec != src["codec"]:
        issues.append(f"codec {actual.codec!r} != transcript {src['codec']!r}")

    expected_sha = src.get("sha256")
    if expected_sha:
        actual_sha = sha256_of_file(audio_path)
        if actual_sha != expected_sha:
            issues.append(
                f"SHA-256 mismatch: audio {actual_sha[:12]}…, transcript {expected_sha[:12]}…"
            )

    if issues:
        bullet = "\n  - "
        raise AudioTranscriptMismatch(
            f"Audio file does not match transcript.\n  - " + bullet.join(issues)
        )


def create_app(config: ServeConfig) -> FastAPI:
    if not config.audio_path.exists():
        raise FileNotFoundError(f"audio not found: {config.audio_path}")
    if not config.transcript_path.exists():
        raise FileNotFoundError(f"transcript not found: {config.transcript_path}")

    transcript_data = json.loads(config.transcript_path.read_text())
    _validate_audio_matches_transcript(config.audio_path, transcript_data)

    app = FastAPI(title="podedit", docs_url="/api/docs", redoc_url=None)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/transcript")
    def transcript() -> JSONResponse:
        return JSONResponse(transcript_data)

    @app.get("/api/audio/info")
    def audio_info() -> dict:
        src = transcript_data.get("source_audio") or {}
        return {
            "name": Path(src.get("path", str(config.audio_path))).name,
            "duration_sec": src.get("duration_sec"),
            "sample_rate": src.get("sample_rate"),
            "channels": src.get("channels"),
            "codec": src.get("codec"),
            "url": "/api/audio",
        }

    @app.get("/api/audio")
    def audio() -> FileResponse:
        # FastAPI/Starlette FileResponse handles Range requests so the browser
        # can seek without re-downloading the whole file.
        media_type = _guess_media_type(config.audio_path)
        return FileResponse(config.audio_path, media_type=media_type, filename=config.audio_path.name)

    # Static UI mounted last so /api/* takes priority.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app


def _guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
    }.get(suffix, "application/octet-stream")
