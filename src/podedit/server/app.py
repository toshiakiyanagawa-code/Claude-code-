"""FastAPI app for the local web UI.

W3: serve transcript + audio with click-to-seek.
W4: persist EditSession + KPI events to disk; the UI POSTs back on every change.

Single-tenant local service: one (audio, transcript, session) triple per server
process, configured at startup via ``create_app``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..audio import probe as audio_probe
from ..edit import EditSession, sha256_of_file
from ..schema import AudioRef

STATIC_DIR = Path(__file__).parent / "static"

DURATION_TOLERANCE_SEC = 0.5


class AudioTranscriptMismatch(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServeConfig:
    audio_path: Path
    transcript_path: Path
    session_path: Path  # JSON; auto-loaded if exists, auto-saved on UI changes
    kpi_log_path: Path  # JSONL; one line per UI event


def _validate_audio_matches_transcript(audio_path: Path, transcript_data: dict) -> None:
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


def _source_audio_ref_from_transcript(transcript_data: dict, audio_path: Path) -> AudioRef:
    src = transcript_data.get("source_audio") or {}
    return AudioRef(
        path=src.get("path", str(audio_path)),
        duration_sec=float(src.get("duration_sec", 0.0)),
        sample_rate=int(src.get("sample_rate", 0)),
        channels=int(src.get("channels", 0)),
        codec=src.get("codec", ""),
        sha256=src.get("sha256"),
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic file replace: write to a sibling tempfile and rename.

    Avoids the half-written-session footgun if uvicorn dies mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_app(config: ServeConfig) -> FastAPI:
    if not config.audio_path.exists():
        raise FileNotFoundError(f"audio not found: {config.audio_path}")
    if not config.transcript_path.exists():
        raise FileNotFoundError(f"transcript not found: {config.transcript_path}")

    transcript_data = json.loads(config.transcript_path.read_text())
    _validate_audio_matches_transcript(config.audio_path, transcript_data)

    # Load or create the EditSession we'll mutate via POSTs.
    if config.session_path.exists():
        session = EditSession.from_dict(json.loads(config.session_path.read_text()))
    else:
        session = EditSession.new(
            source_audio=_source_audio_ref_from_transcript(transcript_data, config.audio_path),
            transcript_ref=str(config.transcript_path),
        )

    # Hold the session under a lock so concurrent autosave POSTs don't interleave.
    session_lock = Lock()

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
        media_type = _guess_media_type(config.audio_path)
        return FileResponse(config.audio_path, media_type=media_type, filename=config.audio_path.name)

    @app.get("/api/session")
    def get_session() -> dict:
        with session_lock:
            return session.to_dict()

    @app.put("/api/session")
    def put_session(body: dict) -> dict:
        nonlocal session
        try:
            new_session = EditSession.from_dict(body)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid session: {e}") from e

        # The incoming session must belong to the audio we're currently serving.
        # Skipping these checks would let a stray PUT (different episode / wrong
        # timeline_basis / out-of-range ops) silently overwrite the file on disk.
        if new_session.timeline_basis != "source_audio_seconds":
            raise HTTPException(
                status_code=400,
                detail=f"timeline_basis must be 'source_audio_seconds', got {new_session.timeline_basis!r}",
            )
        expected_src = transcript_data.get("source_audio") or {}
        expected_sha = expected_src.get("sha256")
        actual_sha = new_session.source_audio.sha256
        if expected_sha and actual_sha and expected_sha != actual_sha:
            raise HTTPException(
                status_code=400,
                detail=f"session source_audio.sha256 {actual_sha[:12]}… doesn't match served audio {expected_sha[:12]}…",
            )
        expected_duration = expected_src.get("duration_sec")
        if expected_duration is not None:
            if abs(new_session.source_audio.duration_sec - float(expected_duration)) > DURATION_TOLERANCE_SEC:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"session source duration {new_session.source_audio.duration_sec:.2f}s "
                        f"doesn't match served audio {float(expected_duration):.2f}s"
                    ),
                )
            for op in new_session.ops:
                if op.start < 0 or op.end > float(expected_duration) + 1e-3 or op.end <= op.start:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"op {op.op_id} range {op.start}-{op.end} falls outside "
                            f"[0, {float(expected_duration):.2f}]"
                        ),
                    )

        with session_lock:
            session = new_session
            _atomic_write_text(
                config.session_path,
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            )
        return {"saved_at": time.time(), "path": str(config.session_path), "ops": len(session.ops)}

    @app.post("/api/kpi/event")
    def post_kpi(event: dict) -> dict:
        record = {"server_ts": time.time(), **event}
        _append_jsonl(config.kpi_log_path, record)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
