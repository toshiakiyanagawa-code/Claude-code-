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
from ..edit import EditSession, compile_timeline, sha256_of_file
from ..library import scan_library
from ..render import RENDERER_VERSION, RenderError, render_segments
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
    library_dir: Path | None = None  # parent dir for the library file picker; defaults to audio_path's parent
    work_dir: Path | None = None  # parent dir for transcripts/sessions; defaults to session_path's parent


class ServeState:
    """Mutable bundle of "what we're currently serving" state.

    Created at startup from the bootstrap ``ServeConfig`` and then mutated by
    ``POST /api/library/select`` when the user switches files in the UI. All
    route handlers read from ``state``; ``ServeConfig`` is only the bootstrap.
    """

    def __init__(self, config: ServeConfig) -> None:
        self.library_dir: Path = (config.library_dir or config.audio_path.parent).resolve()
        self.work_dir: Path = (config.work_dir or config.session_path.parent).resolve()
        # All these get filled in by load_active() below.
        self.audio_path: Path = config.audio_path
        self.transcript_path: Path = config.transcript_path
        self.session_path: Path = config.session_path
        self.kpi_log_path: Path = config.kpi_log_path
        self.serve_audio_path: Path = config.audio_path
        self.transcript_data: dict = {}
        self.session: EditSession | None = None
        self.session_lock = Lock()

    def serve_audio_tag(self) -> str:
        st = self.serve_audio_path.stat()
        return f"{int(st.st_mtime)}-{st.st_size}"

    def load_active(self, audio_path: Path, transcript_path: Path) -> None:
        """Switch the active (audio, transcript, session) triple. Validates fully."""
        if not audio_path.exists():
            raise FileNotFoundError(f"audio not found: {audio_path}")
        if not transcript_path.exists():
            raise FileNotFoundError(f"transcript not found: {transcript_path}")
        transcript_data = json.loads(transcript_path.read_text())
        _validate_audio_matches_transcript(audio_path, transcript_data)

        # Faststart-remux m4a if moov sits at the tail, same trick we use at boot.
        serve_audio_path = audio_path
        if audio_path.suffix.lower() in (".m4a", ".mp4", ".mov") and _moov_atom_position(audio_path) == "tail":
            cached = self.work_dir / f"{audio_path.stem}.faststart{audio_path.suffix}"
            serve_audio_path = _ensure_faststart(audio_path, cached)

        session_path = self.work_dir / f"{audio_path.stem}.session.json"
        kpi_log_path = self.work_dir / f"{audio_path.stem}.kpi.jsonl"
        if session_path.exists():
            session = EditSession.from_dict(json.loads(session_path.read_text()))
        else:
            session = EditSession.new(
                source_audio=_source_audio_ref_from_transcript(transcript_data, audio_path),
                transcript_ref=str(transcript_path),
            )

        # Under the session lock so a concurrent autosave POST can't trip over a
        # half-switched state.
        with self.session_lock:
            self.audio_path = audio_path
            self.transcript_path = transcript_path
            self.session_path = session_path
            self.kpi_log_path = kpi_log_path
            self.serve_audio_path = serve_audio_path
            self.transcript_data = transcript_data
            self.session = session


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
    state = ServeState(config)
    state.load_active(config.audio_path, config.transcript_path)
    # ``state.session_path`` and friends may differ from ``config.*`` after
    # load_active() because we always derive them from the work_dir + audio
    # stem now. The bootstrap config still pins down library_dir / work_dir.

    app = FastAPI(title="podedit", docs_url="/api/docs", redoc_url=None)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/transcript")
    def transcript() -> JSONResponse:
        return JSONResponse(state.transcript_data)

    @app.get("/api/audio/info")
    def audio_info() -> dict:
        src = state.transcript_data.get("source_audio") or {}
        return {
            "name": Path(src.get("path", str(state.audio_path))).name,
            "duration_sec": src.get("duration_sec"),
            "sample_rate": src.get("sample_rate"),
            "channels": src.get("channels"),
            "codec": src.get("codec"),
            "url": f"/api/audio?v={state.serve_audio_tag()}",
            "serve_audio_filename": state.serve_audio_path.name,
            "serve_audio_bytes": state.serve_audio_path.stat().st_size,
        }

    @app.get("/api/audio")
    def audio() -> FileResponse:
        media_type = _guess_media_type(state.serve_audio_path)
        return FileResponse(
            state.serve_audio_path, media_type=media_type, filename=state.serve_audio_path.name,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/session")
    def get_session() -> dict:
        with state.session_lock:
            return state.session.to_dict()

    @app.put("/api/session")
    def put_session(body: dict) -> dict:
        # Shape-validate the payload outside the lock — it's CPU-only.
        try:
            new_session = EditSession.from_dict(body)
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"invalid session: {e}") from e
        if new_session.timeline_basis != "source_audio_seconds":
            raise HTTPException(
                status_code=400,
                detail=f"timeline_basis must be 'source_audio_seconds', got {new_session.timeline_basis!r}",
            )

        # The remaining validation depends on which file is currently active.
        # If we let library/select race in between, autosaved ops for ep1 could
        # be saved to ep2's session_path. Hold the lock from the moment we
        # decide which transcript/session_path applies all the way through
        # the atomic write.
        with state.session_lock:
            transcript_data = state.transcript_data
            target_session_path = state.session_path
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
                    if op.op == "delete":
                        src_start, src_end = op.start, op.end
                    elif op.op == "move":
                        src_start, src_end = op.src_start, op.src_end
                        if op.target_edited_t < 0:
                            raise HTTPException(
                                status_code=400,
                                detail=f"op {op.op_id} target_edited_t must be >= 0",
                            )
                    else:
                        raise HTTPException(status_code=400, detail=f"unsupported op {op.op!r}")
                    if src_start < 0 or src_end > float(expected_duration) + 1e-3 or src_end <= src_start:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"op {op.op_id} range {src_start}-{src_end} falls outside "
                                f"[0, {float(expected_duration):.2f}]"
                            ),
                        )
            state.session = new_session
            _atomic_write_text(
                target_session_path,
                json.dumps(state.session.to_dict(), ensure_ascii=False, indent=2),
            )
            return {"saved_at": time.time(), "path": str(target_session_path), "ops": len(state.session.ops)}

    @app.post("/api/kpi/event")
    def post_kpi(event: dict) -> dict:
        record = {"server_ts": time.time(), **event}
        _append_jsonl(state.kpi_log_path, record)
        return {"ok": True}

    # ------- preview render (W5) -------
    # Renders the current session to a wav using the W5 PCM pipeline. Cached by
    # a hash of (ops + opts + renderer version), so re-clicking with no
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

        # Snapshot every piece of active state the render depends on inside one
        # lock acquisition. If library/select races in mid-render, the render
        # still writes to the snapshot's preview path, references the snapshot's
        # audio file, and logs the snapshot's KPI path — never a mix.
        with state.session_lock:
            ops_for_render = list(state.session.ops)
            ops_blob = state.session.to_dict().get("ops", [])
            source_duration = state.session.source_audio.duration_sec
            snap_audio_path = state.audio_path
            snap_work_dir = state.work_dir
            snap_kpi_log = state.kpi_log_path
            snap_audio_stem = state.audio_path.stem

        # Cache key covers every parameter that can change the bytes on disk.
        # Including RENDERER_VERSION means a code change automatically
        # invalidates previously-rendered previews.
        cache_key_blob = json.dumps(
            {
                "ops": ops_blob,
                "crossfade_ms": crossfade_ms,
                "lufs_target": lufs_target,
                "true_peak_ceiling_dbtp": true_peak,
                "renderer_version": RENDERER_VERSION,
            },
            sort_keys=True,
        ).encode()
        cache_key = hashlib.sha256(cache_key_blob).hexdigest()[:16]
        preview_path = snap_work_dir / f"{snap_audio_stem}.preview.{cache_key}.wav"

        cached = preview_path.exists()
        if not cached:
            _gc_previews(snap_work_dir, snap_audio_stem, keep=preview_path)
            t0 = time.time()
            try:
                segments = compile_timeline(source_duration, ops_for_render)
                result = render_segments(
                    snap_audio_path,
                    segments=segments,
                    output=preview_path,
                    source_duration=source_duration,
                    move_count=sum(1 for op in ops_for_render if op.op == "move"),
                    crossfade_ms=crossfade_ms,
                    lufs_target=lufs_target,
                    true_peak_ceiling_dbtp=true_peak,
                )
            except RenderError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e
            _append_jsonl(snap_kpi_log, {
                "server_ts": time.time(), "type": "server.preview.rendered",
                "cache_key": cache_key, "wall_sec": time.time() - t0,
                "duration_in": result.duration_in, "duration_out": result.duration_out,
                "n_keeps": len(result.keeps),
                "segments_count": result.segments_count,
                "move_count": result.move_count,
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
                json.dumps(ops_blob, sort_keys=True).encode()
            ).hexdigest()[:16],
        }

    _CACHE_KEY_RE = re.compile(r"^[a-f0-9]{1,64}$")

    @app.get("/api/preview-audio/{cache_key}")
    def preview_audio(cache_key: str) -> FileResponse:
        if not _CACHE_KEY_RE.match(cache_key):
            raise HTTPException(status_code=400, detail="invalid cache key")
        p = state.work_dir / f"{state.audio_path.stem}.preview.{cache_key}.wav"
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
        cache_path = state.work_dir / f"{state.audio_path.stem}.waveform.{points}.json"
        try:
            wf = get_or_compute_waveform(state.audio_path, cache_path, target_points=points)
        except WaveformError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return JSONResponse(wf.to_dict())

    # ------- library (W7.6) -------
    # Lets the UI swap which (audio, transcript, session) triple is being
    # served without restarting the server. The browser is expected to do a
    # full page reload after a successful POST so every cached resource
    # (transcript, waveform, audio) is re-fetched against the new state.
    @app.get("/api/library")
    def library() -> JSONResponse:
        entries = scan_library(state.library_dir, state.work_dir)
        active_name = state.audio_path.name
        return JSONResponse({
            "library_dir": str(state.library_dir),
            "work_dir": str(state.work_dir),
            "active": active_name,
            "entries": [e.to_dict() for e in entries],
        }, headers={"Cache-Control": "no-store"})

    @app.post("/api/library/select")
    def library_select(body: dict) -> dict:
        from ..library import SUPPORTED_AUDIO_SUFFIXES

        name = (body or {}).get("name")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="name must be a plain filename in the library dir")
        candidate_audio = state.library_dir / name
        # Defense in depth: even if scan_library skipped the file (wrong suffix
        # / directory / dotfile / faststart derivative), reject it here too so
        # a hand-crafted POST can't switch the server onto something weird.
        if candidate_audio.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"unsupported audio suffix {candidate_audio.suffix!r}")
        if not candidate_audio.exists() or not candidate_audio.is_file():
            raise HTTPException(status_code=404, detail=f"{name!r} not found in library")
        candidate_transcript = state.work_dir / f"{candidate_audio.stem}.transcript.json"
        if not candidate_transcript.exists():
            raise HTTPException(
                status_code=409,
                detail=f"no transcript found for {name!r}; run `podedit transcribe` first",
            )
        try:
            state.load_active(candidate_audio, candidate_transcript)
        except (FileNotFoundError, AudioTranscriptMismatch) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _append_jsonl(state.kpi_log_path, {
            "server_ts": time.time(), "type": "server.library.selected",
            "name": name, "audio_path": str(state.audio_path),
        })
        return {
            "ok": True,
            "active": name,
            "audio_path": str(state.audio_path),
            "transcript_path": str(state.transcript_path),
            "session_path": str(state.session_path),
        }

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
