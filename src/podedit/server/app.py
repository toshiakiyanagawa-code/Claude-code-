"""FastAPI app for the local web UI.

W3: serve transcript + audio with click-to-seek.
W4: persist EditSession + KPI events to disk; the UI POSTs back on every change.

Single-tenant local service: one (audio, transcript, session) triple per server
process, configured at startup via ``create_app``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
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
from ..render import RENDERER_VERSION, RenderError, render_cuts
from ..schema import AudioRef
from ..waveform import WaveformError, get_or_compute_waveform

# Garbage collection knobs for the preview cache. Previews are ~340 MB / 30 min,
# so a small file count keeps the workdir from blowing up on the user's host.
PREVIEW_GC_MAX_FILES = 3
PREVIEW_GC_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB combined

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


def _moov_atom_position(path: Path, head_bytes: int = 2 * 1024 * 1024) -> str:
    """Return 'head', 'tail', or 'none' depending on where the m4a/mp4 moov atom sits.

    A browser <audio> element can't seek reliably inside an m4a until it has the
    moov atom. When moov is at the tail, seeks (incl. JS ``currentTime = x``)
    can be silently dropped or deferred until the whole file is downloaded.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(min(head_bytes, size))
        if b"moov" in head:
            return "head"
        if size > head_bytes:
            with path.open("rb") as f:
                f.seek(max(0, size - head_bytes))
                tail = f.read()
            if b"moov" in tail:
                return "tail"
    except OSError:
        return "none"
    return "none"


def _ensure_faststart(src: Path, dst: Path) -> Path:
    """Remux ``src`` to ``dst`` with the moov atom at the head (faststart).

    Stream-copies audio/video without re-encoding, so it's effectively a
    metadata move. Skips work if dst already exists and is newer than src.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH; cannot faststart-remux audio")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-c", "copy",
            "-movflags", "+faststart",
            str(dst),
        ],
        check=True, capture_output=True, text=True,
    )
    return dst


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

    # If we're serving an m4a/mp4 with moov-atom-at-tail, the browser can't
    # respond to JS-driven currentTime seeks until the whole file is downloaded,
    # which breaks W4's preview-skip. Remux a faststart-ordered copy once and
    # serve that instead. Cached in the same dir as the session file.
    serve_audio_path = config.audio_path
    if config.audio_path.suffix.lower() in (".m4a", ".mp4", ".mov") and \
            _moov_atom_position(config.audio_path) == "tail":
        cached = config.session_path.parent / f"{config.audio_path.stem}.faststart{config.audio_path.suffix}"
        serve_audio_path = _ensure_faststart(config.audio_path, cached)

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

    # Cache-buster: changes when the served-audio file changes (e.g. after we
    # generate a new faststart-remux). Used by the UI in the audio <src>.
    serve_audio_tag = f"{int(serve_audio_path.stat().st_mtime)}-{serve_audio_path.stat().st_size}"

    @app.get("/api/audio/info")
    def audio_info() -> dict:
        src = transcript_data.get("source_audio") or {}
        return {
            "name": Path(src.get("path", str(config.audio_path))).name,
            "duration_sec": src.get("duration_sec"),
            "sample_rate": src.get("sample_rate"),
            "channels": src.get("channels"),
            "codec": src.get("codec"),
            "url": f"/api/audio?v={serve_audio_tag}",
            "serve_audio_filename": serve_audio_path.name,
            "serve_audio_bytes": serve_audio_path.stat().st_size,
        }

    @app.get("/api/audio")
    def audio() -> FileResponse:
        media_type = _guess_media_type(serve_audio_path)
        # no-store: the audio file changes when we re-remux (e.g. different
        # faststart). Combined with the ?v=<tag> query in /api/audio/info, this
        # guarantees the browser fetches the current file after any change.
        return FileResponse(
            serve_audio_path, media_type=media_type, filename=serve_audio_path.name,
            headers={"Cache-Control": "no-store"},
        )

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

    # ------- preview render (W5) -------
    # Renders the current session to a wav using the W5 PCM pipeline. Cached by
    # a hash of (deletes + opts + renderer version), so re-clicking with no
    # edits is a free no-op and a renderer upgrade invalidates stale cache.
    # Render is synchronous; for the 30-min episode it takes ~75 s.
    @app.post("/api/preview/render")
    def render_preview(opts: dict | None = None) -> dict:
        opts = opts or {}
        crossfade_ms = float(opts.get("crossfade_ms", 10.0))
        lufs_target = opts.get("lufs_target", -16.0)
        if lufs_target is not None:
            lufs_target = float(lufs_target)
        true_peak = float(opts.get("true_peak_ceiling_dbtp", -1.0))

        with session_lock:
            deletes = sorted(
                (round(op.start, 3), round(op.end, 3))
                for op in session.ops if op.op == "delete"
            )
            source_duration = session.source_audio.duration_sec

        # Cache key covers every parameter that can change the bytes on disk.
        # Including RENDERER_VERSION means a code change automatically
        # invalidates previously-rendered previews.
        cache_key_blob = json.dumps(
            {
                "deletes": deletes,
                "crossfade_ms": crossfade_ms,
                "lufs_target": lufs_target,
                "true_peak_ceiling_dbtp": true_peak,
                "renderer_version": RENDERER_VERSION,
            },
            sort_keys=True,
        ).encode()
        cache_key = hashlib.sha256(cache_key_blob).hexdigest()[:16]
        preview_path = config.session_path.parent / f"{config.audio_path.stem}.preview.{cache_key}.wav"

        cached = preview_path.exists()
        if not cached:
            _gc_previews(config.session_path.parent, config.audio_path.stem, keep=preview_path)
            t0 = time.time()
            try:
                result = render_cuts(
                    config.audio_path,
                    source_duration=source_duration,
                    deletes=list(deletes),
                    output=preview_path,
                    crossfade_ms=crossfade_ms,
                    lufs_target=lufs_target,
                    true_peak_ceiling_dbtp=true_peak,
                )
            except RenderError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e
            _append_jsonl(config.kpi_log_path, {
                "server_ts": time.time(), "type": "server.preview.rendered",
                "cache_key": cache_key, "wall_sec": time.time() - t0,
                "duration_in": result.duration_in, "duration_out": result.duration_out,
                "n_keeps": len(result.keeps),
                "crossfade_ms_requested": result.crossfade_ms_requested,
                "crossfade_ms_applied": result.crossfade_ms,
                "lufs_target": lufs_target, "lufs_out": result.lufs_out,
                "true_peak_dbtp": result.true_peak_dbtp,
                "renderer_version": result.renderer_version,
            })

        st = preview_path.stat()
        tag = f"{int(st.st_mtime)}-{st.st_size}"
        # Mirror the (possibly drift-from-keeps-sum) actual output duration back
        # to the UI so its scrubber max can match the rendered file exactly.
        try:
            import soundfile as sf
            info_out = sf.info(str(preview_path))
            preview_duration = info_out.frames / info_out.samplerate
        except Exception:
            preview_duration = None
        return {
            "cache_key": cache_key,
            "url": f"/api/preview-audio/{cache_key}?v={tag}",
            "cached": cached,
            "bytes": st.st_size,
            "duration_sec": preview_duration,
            "ops_hash": hashlib.sha256(
                json.dumps(deletes).encode()
            ).hexdigest()[:16],
        }

    _CACHE_KEY_RE = re.compile(r"^[a-f0-9]{1,64}$")

    @app.get("/api/preview-audio/{cache_key}")
    def preview_audio(cache_key: str) -> FileResponse:
        if not _CACHE_KEY_RE.match(cache_key):
            raise HTTPException(status_code=400, detail="invalid cache key")
        p = config.session_path.parent / f"{config.audio_path.stem}.preview.{cache_key}.wav"
        if not p.exists():
            raise HTTPException(status_code=404, detail="preview not rendered; POST /api/preview/render first")
        return FileResponse(p, media_type="audio/wav", filename=p.name,
                            headers={"Cache-Control": "no-store"})

    # ------- waveform (W7) -------
    # Pre-decoded envelope for the UI. Cached as JSON next to the session,
    # recomputed when the source mtime changes or the schema bumps.
    @app.get("/api/waveform")
    def waveform(points: int = 4000) -> JSONResponse:
        if points <= 0 or points > 20_000:
            raise HTTPException(status_code=400, detail="points must be in (0, 20000]")
        cache_path = config.session_path.parent / f"{config.audio_path.stem}.waveform.{points}.json"
        try:
            wf = get_or_compute_waveform(config.audio_path, cache_path, target_points=points)
        except WaveformError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return JSONResponse(wf.to_dict())

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _gc_previews(dir_: Path, stem: str, *, keep: Path) -> list[Path]:
    """Evict old preview wavs so the workdir doesn't accumulate hundreds of MB.

    Keeps at most ``PREVIEW_GC_MAX_FILES`` files, and at most
    ``PREVIEW_GC_MAX_BYTES`` of combined size, dropping oldest-first by mtime.
    ``keep`` is preserved unconditionally (it's the cache target we're about to
    write, so deleting it would defeat the cache).
    """
    pattern = f"{stem}.preview.*.wav"
    files = sorted(
        (p for p in dir_.glob(pattern) if p.exists() and p != keep),
        key=lambda p: p.stat().st_mtime,
    )
    removed: list[Path] = []
    # Cap by file count first
    while len(files) >= PREVIEW_GC_MAX_FILES:
        victim = files.pop(0)
        try:
            victim.unlink()
            removed.append(victim)
        except OSError:
            pass
    # Then cap by combined bytes
    total = sum(p.stat().st_size for p in files if p.exists())
    while total > PREVIEW_GC_MAX_BYTES and files:
        victim = files.pop(0)
        try:
            sz = victim.stat().st_size
            victim.unlink()
            total -= sz
            removed.append(victim)
        except OSError:
            pass
    return removed


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
