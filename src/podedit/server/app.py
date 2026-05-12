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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True, slots=True)
class ServeConfig:
    audio_path: Path
    transcript_path: Path


def create_app(config: ServeConfig) -> FastAPI:
    if not config.audio_path.exists():
        raise FileNotFoundError(f"audio not found: {config.audio_path}")
    if not config.transcript_path.exists():
        raise FileNotFoundError(f"transcript not found: {config.transcript_path}")

    app = FastAPI(title="podedit", docs_url="/api/docs", redoc_url=None)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/transcript")
    def transcript() -> JSONResponse:
        data = json.loads(config.transcript_path.read_text())
        return JSONResponse(data)

    @app.get("/api/audio/info")
    def audio_info() -> dict:
        # Pull the audio metadata that's already cached inside the transcript JSON.
        data = json.loads(config.transcript_path.read_text())
        src = data.get("source_audio") or {}
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
