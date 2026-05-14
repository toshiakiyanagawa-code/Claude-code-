"""FastAPI app for the local web UI.

W3: serve transcript + audio with click-to-seek.
W4: persist EditSession + KPI events to disk; the UI POSTs back on every change.

Single-tenant local service: one (audio, transcript, session) triple per server
process, configured at startup via ``create_app``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.types import Scope


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that disables browser caching.

    Why: podedit is a local dev tool; users edit app.js / index.html and then
    hard-refresh expecting to see the change. Default StaticFiles only sends
    Last-Modified, which on Codespace forwarded ports plays badly with edge
    caches and produces "stuck on old JS" symptoms (the W7.6 library modal
    bug). For a localhost tool, always-fresh is the right tradeoff.
    """
    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

from ..annotations import build_annotation_payload
from ..audio import probe as audio_probe
from ..edit import EditSession, compile_timeline, sha256_of_file
from ..library import SUPPORTED_AUDIO_SUFFIXES, list_directory, scan_library
from ..render import RENDERER_VERSION, RenderError, render_segments
from ..schema import AudioRef
from ..waveform import WaveformError, get_or_compute_waveform
from .jobs import TranscriptionJobManager

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
    audio_path: Path | None
    transcript_path: Path | None
    session_path: Path | None  # JSON; auto-loaded if exists, auto-saved on UI changes
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
        default_work_dir = config.work_dir or (
            config.session_path.parent if config.session_path is not None else Path(".podedit/work")
        )
        default_library_dir = config.library_dir or (
            config.audio_path.parent if config.audio_path is not None else Path.cwd()
        )
        self.library_dir: Path = default_library_dir.resolve()
        self.work_dir: Path = default_work_dir.resolve()
        # All these get filled in by load_active() below.
        self.audio_path: Path | None = None
        self.transcript_path: Path | None = None
        self.session_path: Path | None = config.session_path
        self.kpi_log_path: Path = config.kpi_log_path
        self.serve_audio_path: Path | None = None
        self.transcript_data: dict = {}
        self.session: EditSession | None = None
        self.session_lock = Lock()

    def has_active(self) -> bool:
        return (
            self.audio_path is not None
            and self.transcript_path is not None
            and self.session_path is not None
            and self.serve_audio_path is not None
            and self.session is not None
            and bool(self.transcript_data)
        )

    def serve_audio_tag(self) -> str:
        if self.serve_audio_path is None:
            raise RuntimeError("no audio loaded")
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


def _pin_inode_via_hardlink(source: Path, *, dir: Path, prefix: str, suffix: str) -> Path:
    """Create a transient hardlink to ``source`` in ``dir`` so the inode is
    pinned for the duration of an outgoing stream / ffmpeg run.

    Codex W8 follow-up: without this, a concurrent ``_gc_previews`` pass can
    unlink the canonical cache file between our existence check and the actual
    open (FileResponse opens after the route returns; ffmpeg opens after we
    spawn it). Since hardlinks share the same inode on POSIX, the bytes
    survive even when the canonical name is gone. Caller must unlink the
    returned path (FileResponse via BackgroundTask, ffmpeg via try/finally).

    A theoretical TOCTOU between the unlink-then-link exists: another mkstemp
    could pick the same 8-random-char name in the microsecond gap. Probability
    is effectively zero (62^8 namespace ≈ 2.18e14) and failure would be a
    FileExistsError that surfaces as a clean OSError, not corruption.
    """
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(dir))
    os.close(fd)
    # mkstemp leaves an empty file at the path; remove it so os.link can
    # publish the hardlink at the same name. The tiny race window between
    # unlink and link is acceptable (see docstring).
    os.unlink(name)
    os.link(str(source), name)
    return Path(name)


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


def _save_last_active(state: ServeState) -> None:
    """Atomically record the currently-active (audio, transcript) pair.

    Lets a subsequent ``podedit serve`` (without --audio) restore the last
    file the user was working on, so an editor mid-task doesn't lose context
    across server restarts or Codespace rebuilds.

    Tmp name is suffixed with a random token so two concurrent saves don't
    rename the same tempfile out from under each other (a 500 path codex
    flagged in review).
    """
    last_active_path = state.work_dir / "last_active.json"
    tmp_path = state.work_dir / f"last_active.json.{secrets.token_hex(4)}.tmp"
    try:
        tmp_path.write_text(json.dumps({
            "audio_path": str(state.audio_path),
            "transcript_path": str(state.transcript_path),
            "ts": time.time(),
        }))
        os.replace(tmp_path, last_active_path)
    finally:
        # If write_text raised before os.replace, drop the dangling tmp.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _try_load_last_active(state: ServeState) -> bool:
    """Try to restore the last active (audio, transcript) pair on startup.

    Returns True if restoration succeeded. Any failure (missing file, missing
    audio/transcript on disk, JSON corruption, audio/transcript mismatch) is
    silently swallowed and the server stays in empty state.
    """
    last_active_path = state.work_dir / "last_active.json"
    try:
        data = json.loads(last_active_path.read_text())
        audio_path = Path(data["audio_path"])
        transcript_path = Path(data["transcript_path"])
        if not audio_path.exists() or not transcript_path.exists():
            return False
        state.load_active(audio_path, transcript_path)
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        FileNotFoundError,
        AudioTranscriptMismatch,
    ):
        return False
    print(f"restored last active audio: {state.audio_path}")
    try:
        _append_jsonl(state.kpi_log_path, {
            "server_ts": time.time(),
            "type": "server.library.restored",
            "audio_path": str(state.audio_path),
            "transcript_path": str(state.transcript_path),
        })
    except OSError:
        # KPI logging is best-effort during startup; never let a write
        # failure here block a successful restore.
        pass
    return True


def create_app(config: ServeConfig) -> FastAPI:
    state = ServeState(config)
    if config.audio_path is not None and config.transcript_path is not None:
        state.load_active(config.audio_path, config.transcript_path)
    elif config.audio_path is None:
        _try_load_last_active(state)
    transcription_jobs = TranscriptionJobManager(work_dir=state.work_dir)
    # Opportunistic startup cleanup of W8 transient hardlinks. BackgroundTask
    # usually deletes these on response end, but if the process died mid-
    # response or the client aborted, dotfile orphans accumulate. We only
    # remove ones older than ``_STARTUP_CLEANUP_AGE_SEC`` so that if a second
    # podedit process shares this work_dir (rare but possible), we don't yank
    # the live transient links the other process has just created and is
    # streaming through. (Codex W8 final review flagged this multi-process
    # race.) A fresh orphan from a still-running sibling will be cleaned by
    # the next process restart, by which point it's old enough.
    _STARTUP_CLEANUP_AGE_SEC = 600  # 10 minutes — far longer than any realistic stream
    now_ts = time.time()
    for orphan in state.work_dir.glob(".*"):
        # Match dotfile prefixes the hardlink helper uses, plus the mp3
        # transcode tempfile pattern (`.<basename>.<rand>.inprogress.mp3`)
        # so a crashed ffmpeg run leaves no residue.
        matches_helper_prefix = any(
            orphan.name.startswith(p) for p in (".export-", ".audition-", ".mp3src-")
        )
        if not (matches_helper_prefix or orphan.name.endswith(".inprogress.mp3")):
            continue
        try:
            if now_ts - orphan.stat().st_mtime < _STARTUP_CLEANUP_AGE_SEC:
                continue
            orphan.unlink()
        except OSError:
            pass
    # Per-cache-key render lock. Without it, two concurrent /api/preview/render
    # calls for the same key (e.g. Export + Audition fired in parallel from
    # multiple tabs) both miss `preview_path.exists()` and both run ffmpeg
    # with -y against the same wav, corrupting the file while either side
    # reads it. Codex flagged this in the W8 review. ``_render_locks_mu``
    # protects the dict itself; the per-key Lock is the actual gate.
    _render_locks: dict[str, Lock] = {}
    _render_locks_mu = Lock()

    def _lock_for(cache_key: str) -> Lock:
        with _render_locks_mu:
            lk = _render_locks.get(cache_key)
            if lk is None:
                lk = Lock()
                _render_locks[cache_key] = lk
            return lk
    # ``state.session_path`` and friends may differ from ``config.*`` after
    # load_active() because we always derive them from the work_dir + audio
    # stem now. The bootstrap config still pins down library_dir / work_dir.

    app = FastAPI(title="podedit", docs_url="/api/docs", redoc_url=None)
    no_audio_detail = "no audio loaded yet — pick a file via the Open dialog"

    def require_active() -> None:
        if not state.has_active():
            raise HTTPException(status_code=503, detail=no_audio_detail)

    @app.get("/api/health")
    def health() -> dict:
        codespace_name = os.environ.get("CODESPACE_NAME") or None
        return {
            "ok": True,
            "is_codespaces": bool(os.environ.get("CODESPACES")),
            "codespace_name": codespace_name,
            "chunked_upload_chunk_size": 512 * 1024,
        }

    @app.get("/api/transcript")
    def transcript() -> JSONResponse:
        require_active()
        return JSONResponse(state.transcript_data)

    @app.get("/api/annotations/fillers")
    def filler_annotations() -> JSONResponse:
        require_active()
        return JSONResponse(
            build_annotation_payload(state.transcript_data),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/audio/info")
    def audio_info() -> dict:
        require_active()
        src = state.transcript_data.get("source_audio") or {}
        assert state.audio_path is not None
        assert state.serve_audio_path is not None
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
        require_active()
        assert state.serve_audio_path is not None
        media_type = _guess_media_type(state.serve_audio_path)
        return FileResponse(
            state.serve_audio_path, media_type=media_type, filename=state.serve_audio_path.name,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/session")
    def get_session() -> dict:
        require_active()
        with state.session_lock:
            assert state.session is not None
            return state.session.to_dict()

    @app.put("/api/session")
    def put_session(body: dict) -> dict:
        require_active()
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
            assert state.session_path is not None
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
        require_active()
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
            assert state.session is not None
            assert state.audio_path is not None
            ops_for_render = list(state.session.ops)
            ops_blob = state.session.to_dict().get("ops", [])
            source_duration = state.session.source_audio.duration_sec
            snap_audio_path = state.audio_path
            snap_work_dir = state.work_dir
            snap_kpi_log = state.kpi_log_path
            snap_audio_stem = state.audio_path.stem

        # Cache key covers every parameter that can change the bytes on disk.
        # Including RENDERER_VERSION means a code change automatically
        # invalidates previously-rendered previews. Including ``source_name``
        # disambiguates files with the same stem in the library (Codex W8
        # review caught ``episode.wav`` and ``episode.mp3`` colliding on
        # ``episode.preview.<key>.wav`` if their ops happened to match).
        cache_key_blob = json.dumps(
            {
                "source_name": snap_audio_path.name,
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

        # Serialize concurrent renders of the same cache key. The second caller
        # that arrives during a render will block here, then observe the file
        # already exists and return cached=True. This prevents the wav-cache
        # corruption Codex flagged in the W8 review.
        cached = preview_path.exists()
        with _lock_for(cache_key):
            # Re-check inside the lock — the first caller may have just finished.
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
        require_active()
        if not _CACHE_KEY_RE.match(cache_key):
            raise HTTPException(status_code=400, detail="invalid cache key")
        # Snapshot active state under the lock so a racing library/select
        # can't swap stem/work_dir while we resolve the path.
        with state.session_lock:
            assert state.audio_path is not None
            snap_work_dir = state.work_dir
            snap_stem = state.audio_path.stem
        p = snap_work_dir / f"{snap_stem}.preview.{cache_key}.wav"
        if not p.exists():
            raise HTTPException(status_code=404, detail="preview not rendered; POST /api/preview/render first")
        # Same GC-race protection as /api/export: pin the inode via hardlink
        # so a concurrent _gc_previews can't unlink before Starlette opens.
        try:
            link = _pin_inode_via_hardlink(
                p, dir=snap_work_dir,
                prefix=f".audition-{cache_key}-", suffix=".wav",
            )
        except OSError:
            raise HTTPException(status_code=404, detail="preview disappeared")

        def _cleanup(path: Path = link) -> None:
            try:
                path.unlink()
            except OSError:
                pass

        return FileResponse(
            link, media_type="audio/wav", filename=p.name,
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(_cleanup),
        )

    # ------- export (W8) -------
    # Same bytes the audition player streams, but framed as a download. wav is
    # served straight from the preview cache (already 2-pass loudnorm'd, true-
    # peak-limited, sample-precise). mp3 is transcoded once via ffmpeg LAME and
    # cached as ``<stem>.preview.<key>.mp3`` so re-downloads are free.
    @app.get("/api/export/{cache_key}")
    def export_audio(cache_key: str, fmt: str = "wav") -> FileResponse:
        require_active()
        if not _CACHE_KEY_RE.match(cache_key):
            raise HTTPException(status_code=400, detail="invalid cache key")
        if fmt not in ("wav", "mp3"):
            raise HTTPException(status_code=400, detail="fmt must be 'wav' or 'mp3'")
        # Snapshot the active state under the session lock so a concurrent
        # /api/library/select can't swap the audio stem / work_dir / kpi log
        # mid-export. Codex flagged this in the W8 review.
        with state.session_lock:
            assert state.audio_path is not None
            snap_work_dir = state.work_dir
            snap_stem = state.audio_path.stem
            snap_kpi_log = state.kpi_log_path
        wav_path = snap_work_dir / f"{snap_stem}.preview.{cache_key}.wav"
        if not wav_path.exists():
            raise HTTPException(
                status_code=404,
                detail="render not found; POST /api/preview/render first",
            )

        if fmt == "wav":
            served_path = wav_path
            media_type = "audio/wav"
            download_name = f"{snap_stem}.edited.wav"
        else:  # mp3
            mp3_path = snap_work_dir / f"{snap_stem}.preview.{cache_key}.mp3"
            if not mp3_path.exists():
                if shutil.which("ffmpeg") is None:
                    raise HTTPException(
                        status_code=500,
                        detail="ffmpeg not found on PATH; cannot transcode to mp3",
                    )
                # libmp3lame V2 (~190 kbps VBR, good quality at ~14 MB / 30 min).
                # Concurrency: two requests for the same cache_key could race
                # on a shared ".inprogress" name and corrupt each other's mp3.
                # We use a unique mkstemp() tmp per request, then publish via
                # ``os.link`` (no-clobber atomic): if another concurrent request
                # already produced the final mp3, link() fails and we just serve
                # what's there. timeout=600s caps a wedged ffmpeg so a worker
                # thread can't be held forever. The tmp filename starts with
                # ``.`` so scan_library skips it even when work_dir==library_dir.
                #
                # We also hardlink-pin the *source* wav before ffmpeg opens it:
                # a concurrent _gc_previews could otherwise unlink the wav
                # between our existence check above and ffmpeg's open(). Codex
                # flagged this in the W8 final review.
                try:
                    wav_pinned = _pin_inode_via_hardlink(
                        wav_path, dir=snap_work_dir,
                        prefix=f".mp3src-{cache_key}-", suffix=".wav",
                    )
                except OSError:
                    raise HTTPException(
                        status_code=404,
                        detail="render disappeared before mp3 transcode",
                    )
                fd, tmp_str = tempfile.mkstemp(
                    prefix=f".{mp3_path.name}.",
                    suffix=".inprogress.mp3",
                    dir=str(mp3_path.parent),
                )
                os.close(fd)  # ffmpeg reopens the path itself
                tmp = Path(tmp_str)
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-loglevel", "error",
                            "-i", str(wav_pinned),
                            "-codec:a", "libmp3lame", "-q:a", "2",
                            # ffmpeg infers format from extension, but our tmp
                            # ends in .inprogress.mp3 — force the muxer to be
                            # explicit (and robust if mkstemp ever changes).
                            "-f", "mp3",
                            str(tmp),
                        ],
                        check=True, capture_output=True, text=True,
                        timeout=600,
                    )
                    try:
                        os.link(tmp, mp3_path)
                    except FileExistsError:
                        # Lost the race — that's fine, serve the winner's file.
                        pass
                except subprocess.TimeoutExpired as e:
                    raise HTTPException(
                        status_code=504,
                        detail=f"mp3 transcode timed out after {e.timeout}s",
                    ) from e
                except subprocess.CalledProcessError as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"mp3 transcode failed: {e.stderr.strip()[:240]}",
                    ) from e
                finally:
                    # Always clean up the per-request tmp AND the wav pin.
                    # On link success the tmp is redundant; on failure we
                    # don't want orphans cluttering work_dir.
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    try:
                        wav_pinned.unlink()
                    except OSError:
                        pass
            served_path = mp3_path
            media_type = "audio/mpeg"
            download_name = f"{snap_stem}.edited.mp3"

        # TOCTOU + GC race: even after we verified the file exists, a concurrent
        # /api/preview/render's GC pass or a library/select-driven cleanup could
        # remove the canonical preview path between this handler returning and
        # Starlette's FileResponse opening it. Pin the inode via a transient
        # hardlink so the bytes survive even if the canonical name is unlinked.
        try:
            link_path = _pin_inode_via_hardlink(
                served_path, dir=snap_work_dir,
                prefix=f".export-{cache_key}-", suffix=f".{fmt}",
            )
        except OSError:
            raise HTTPException(
                status_code=404,
                detail="render disappeared during export",
            )
        try:
            bytes_now = link_path.stat().st_size
        except OSError:
            try:
                link_path.unlink()
            except OSError:
                pass
            raise HTTPException(status_code=404, detail="render disappeared during export")

        _append_jsonl(snap_kpi_log, {
            "server_ts": time.time(), "type": "server.export.served",
            "cache_key": cache_key, "fmt": fmt,
            "bytes": bytes_now,
            "filename": download_name,
        })
        # Content-Disposition: attachment + filename + filename* (RFC 5987).
        # Japanese filenames break the bare ``filename="..."`` form because
        # Starlette encodes headers as latin-1, so include both forms: an
        # ASCII fallback for ancient parsers and a percent-encoded UTF-8 form
        # that modern browsers prefer. Sanitization rules for the fallback:
        #   - replace any char outside [A-Za-z0-9._-] with "_"
        #   - if that collapses to empty / dot-only, use a generic stem
        #   - HTTP-quoted-string specials (", \\) can never appear after the
        #     character class above, so no extra escaping is needed.
        utf8_q = urllib.parse.quote(download_name, safe="")
        ascii_safe = re.sub(r"[^A-Za-z0-9._-]", "_", download_name)
        if not ascii_safe.strip("._"):
            ascii_safe = f"podedit-export.edited.{fmt}"

        def _cleanup_link(path: Path = link_path) -> None:
            try:
                path.unlink()
            except OSError:
                pass

        return FileResponse(
            link_path,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{ascii_safe}"; '
                    f"filename*=UTF-8''{utf8_q}"
                ),
            },
            background=BackgroundTask(_cleanup_link),
        )

    # ------- waveform (W7) -------
    # Pre-decoded envelope for the UI. Cached as JSON next to the session,
    # recomputed when the source mtime changes or the schema bumps.
    @app.get("/api/waveform")
    def waveform(points: int = 4000) -> JSONResponse:
        require_active()
        if points <= 0 or points > 20_000:
            raise HTTPException(status_code=400, detail="points must be in (0, 20000]")
        assert state.audio_path is not None
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
    def _resolve_browse_path(raw_path: str | None) -> Path:
        if raw_path is None or raw_path == "":
            candidate = state.library_dir
        else:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = state.library_dir / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as e:
            raise HTTPException(status_code=404, detail=f"path not found: {candidate}") from e

    @app.get("/api/library")
    def library() -> JSONResponse:
        entries = scan_library(state.library_dir, state.work_dir)
        active_name = state.audio_path.name if state.audio_path is not None else None
        return JSONResponse({
            "library_dir": str(state.library_dir),
            "path": str(state.library_dir),
            "parent": None,
            "work_dir": str(state.work_dir),
            "active": active_name,
            "active_path": str(state.audio_path.resolve()) if state.audio_path is not None else None,
            "entries": [e.to_dict() for e in entries],
        }, headers={"Cache-Control": "no-store"})

    @app.get("/api/fs/browse")
    def fs_browse(path: str | None = None) -> JSONResponse:
        # Single-user local dev tool. The Codespace forwarded URL is gated by the
        # user's GitHub auth and the server runs as the same user — so we trust
        # any path the process can read. If you fork this for multi-user use,
        # reintroduce a containment check.
        candidate_dir = _resolve_browse_path(path)
        if not candidate_dir.exists() or not candidate_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"directory not found: {candidate_dir}")
        try:
            payload = list_directory(candidate_dir, state.work_dir)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=f"permission denied: {candidate_dir}") from e
        payload["library_dir"] = str(state.library_dir)
        payload["work_dir"] = str(state.work_dir)
        payload["active"] = state.audio_path.name if state.audio_path is not None else None
        payload["active_path"] = str(state.audio_path.resolve()) if state.audio_path is not None else None
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    def _multipart_boundary(content_type: str) -> bytes:
        match = re.search(r'(?:^|;)\s*boundary=(?:"([^"]+)"|([^;]+))', content_type)
        if not match:
            raise HTTPException(status_code=400, detail="multipart boundary is required")
        boundary = (match.group(1) or match.group(2) or "").strip()
        if not boundary:
            raise HTTPException(status_code=400, detail="multipart boundary is required")
        return ("--" + boundary).encode("ascii", "strict")

    def _filename_from_multipart_headers(header_bytes: bytes) -> str:
        try:
            header_text = header_bytes.decode("latin1")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail="invalid multipart headers") from e
        disposition = ""
        for line in header_text.split("\r\n")[1:]:
            if line.lower().startswith("content-disposition:"):
                disposition = line
                break
        name_match = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition)
        if not name_match or name_match.group(1) != "file":
            raise HTTPException(status_code=400, detail="multipart field must be named 'file'")
        filename_match = re.search(r'(?:^|;)\s*filename="([^"]*)"', disposition)
        if not filename_match:
            raise HTTPException(status_code=400, detail="multipart file filename is required")
        filename = filename_match.group(1)
        try:
            return filename.encode("latin1").decode("utf-8")
        except UnicodeError:
            # Keep legacy behavior for non-UTF-8 multipart filenames.
            return filename

    def _validate_upload_basename(filename: str) -> str:
        raw_filename = (filename or "").strip()
        basename = os.path.basename(raw_filename)
        if (
            not raw_filename
            or basename != raw_filename
            or "/" in raw_filename
            or "\\" in raw_filename
            or raw_filename.startswith(".")
            or ".." in raw_filename
        ):
            raise HTTPException(status_code=400, detail="filename must be a plain non-hidden basename")
        if Path(basename).suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"unsupported audio suffix {Path(basename).suffix!r}")
        return basename

    @app.post("/api/library/upload")
    async def library_upload(request: Request) -> JSONResponse:
        max_bytes = 500 * 1024 * 1024
        boundary = _multipart_boundary(request.headers.get("content-type", ""))
        marker = b"\r\n" + boundary
        keep_bytes = len(marker) + 4  # boundary plus possible "--\r\n"

        stream = request.stream().__aiter__()
        header_buffer = b""
        while b"\r\n\r\n" not in header_buffer:
            try:
                chunk = await stream.__anext__()
            except StopAsyncIteration as e:
                raise HTTPException(status_code=400, detail="multipart body is empty") from e
            header_buffer += chunk
            if len(header_buffer) > 64 * 1024 and b"\r\n\r\n" not in header_buffer:
                raise HTTPException(status_code=400, detail="multipart headers too large")

        header_bytes, file_buffer = header_buffer.split(b"\r\n\r\n", 1)
        if not header_bytes.startswith(boundary + b"\r\n"):
            raise HTTPException(status_code=400, detail="multipart file part is required")
        basename = _validate_upload_basename(_filename_from_multipart_headers(header_bytes))
        uploads_dir = state.work_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        final_path = uploads_dir / basename
        tmp_path: str | None = None
        total = 0
        pending = b""
        found_boundary = False

        def write_capped(dst, data: bytes) -> None:
            nonlocal total
            if not data:
                return
            total += len(data)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail="upload exceeds 500 MB limit")
            dst.write(data)

        def consume(dst, data: bytes) -> None:
            nonlocal pending, found_boundary
            if found_boundary:
                return
            combined = pending + data
            idx = combined.find(marker)
            if idx >= 0:
                write_capped(dst, combined[:idx])
                pending = b""
                found_boundary = True
                return
            if len(combined) > keep_bytes:
                write_capped(dst, combined[:-keep_bytes])
                pending = combined[-keep_bytes:]
            else:
                pending = combined

        try:
            with tempfile.NamedTemporaryFile(dir=uploads_dir, delete=False) as tmp:
                tmp_path = tmp.name
                consume(tmp, file_buffer)
                while not found_boundary:
                    try:
                        chunk = await stream.__anext__()
                    except StopAsyncIteration:
                        break
                    consume(tmp, chunk)
                if not found_boundary:
                    raise HTTPException(status_code=400, detail="multipart closing boundary not found")
            try:
                os.link(tmp_path, final_path)
            except FileExistsError as e:
                raise HTTPException(status_code=409, detail=f"{basename!r} already exists in uploads") from e
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

        _append_jsonl(state.kpi_log_path, {
            "server_ts": time.time(),
            "type": "server.library.uploaded",
            "basename": basename,
            "bytes": total,
        })
        return JSONResponse({
            "ok": True,
            "path": str(final_path),
            "basename": basename,
            "bytes": total,
        }, headers={"Cache-Control": "no-store"})

    CHUNK_SIZE = 512 * 1024
    MAX_FILE_BYTES = 500 * 1024 * 1024
    UPLOAD_SESSION_TTL_SECONDS = 3600
    UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")
    upload_locks: dict[str, asyncio.Lock] = {}

    def _chunk_upload_dir() -> Path:
        chunks_dir = state.work_dir / "uploads" / ".chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        return chunks_dir

    def _chunk_session_path(upload_id: str) -> Path:
        return _chunk_upload_dir() / f"{upload_id}.json"

    def _chunk_tmp_path(upload_id: str) -> Path:
        return _chunk_upload_dir() / f"{upload_id}.part"

    def _validate_upload_id(upload_id: str) -> str:
        if not UPLOAD_ID_PATTERN.match(upload_id or ""):
            raise HTTPException(status_code=400, detail="invalid upload_id")
        return upload_id

    def _chunk_upload_lock(upload_id: str) -> asyncio.Lock:
        lock = upload_locks.get(upload_id)
        if lock is None:
            lock = asyncio.Lock()
            upload_locks[upload_id] = lock
        return lock

    def _safe_unlink_chunk_tmp(upload_id: str, tmp_path_value: str | None) -> None:
        """Unlink tmp file only if it resolves to .chunks/<upload_id>.part."""
        if not tmp_path_value:
            return
        try:
            expected = _chunk_tmp_path(upload_id).resolve()
            actual = Path(tmp_path_value).resolve()
        except OSError:
            return
        if actual != expected:
            return
        try:
            actual.unlink()
        except FileNotFoundError:
            pass

    def _cleanup_expired_chunk_uploads() -> None:
        chunks_dir = _chunk_upload_dir()
        now = time.time()
        for session_path in chunks_dir.glob("*.json"):
            try:
                with session_path.open("r", encoding="utf-8") as f:
                    session = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if now - float(session.get("created_ts", 0)) <= UPLOAD_SESSION_TTL_SECONDS:
                continue
            upload_id = session_path.stem
            try:
                session_path.unlink()
            except FileNotFoundError:
                pass
            _safe_unlink_chunk_tmp(upload_id, session.get("tmp_path"))
            upload_locks.pop(upload_id, None)

    def _load_chunk_session(upload_id: str) -> dict:
        upload_id = _validate_upload_id(upload_id)
        session_path = _chunk_session_path(upload_id)
        try:
            with session_path.open("r", encoding="utf-8") as f:
                session = json.load(f)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail="upload session not found") from e
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail="upload session is corrupt") from e
        if time.time() - float(session.get("created_ts", 0)) > UPLOAD_SESSION_TTL_SECONDS:
            try:
                session_path.unlink()
            except FileNotFoundError:
                pass
            _safe_unlink_chunk_tmp(upload_id, session.get("tmp_path"))
            upload_locks.pop(upload_id, None)
            raise HTTPException(status_code=404, detail="upload session expired")
        return session

    def _safe_chunk_tmp_path(upload_id: str, tmp_path_value: str) -> Path:
        """Return the expected .chunks/<upload_id>.part path, rejecting anything else."""
        expected = _chunk_tmp_path(upload_id).resolve()
        try:
            actual = Path(tmp_path_value).resolve()
        except OSError as e:
            raise HTTPException(status_code=500, detail="upload temp path is invalid") from e
        if actual != expected:
            raise HTTPException(status_code=500, detail="upload temp path is invalid")
        return actual

    def _save_chunk_session(upload_id: str, session: dict) -> None:
        session_path = _chunk_session_path(upload_id)
        tmp_session_path = session_path.with_suffix(".json.tmp")
        with tmp_session_path.open("w", encoding="utf-8") as f:
            json.dump(session, f, separators=(",", ":"))
        os.replace(tmp_session_path, session_path)

    @app.post("/api/library/upload/init")
    async def library_upload_init(request: Request) -> JSONResponse:
        _cleanup_expired_chunk_uploads()
        try:
            payload = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail="invalid JSON body") from e
        basename = _validate_upload_basename(str(payload.get("basename") or ""))
        try:
            size = int(payload.get("size"))
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="size must be an integer") from e
        if size < 0:
            raise HTTPException(status_code=400, detail="size must be non-negative")
        if size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="upload exceeds 500 MB limit")

        overwrite = bool(payload.get("overwrite"))
        uploads_dir = state.work_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        final_path = uploads_dir / basename
        # Use lstat to inspect the target without following symlinks. exists()
        # would follow the link and let us silently delete the wrong file.
        try:
            entry_stat = os.lstat(final_path)
        except FileNotFoundError:
            entry_stat = None
        if entry_stat is not None:
            if not overwrite:
                # Structured payload: client uses has_transcript to decide
                # between zero-click "open the existing file" and the
                # prompt-the-editor flow when the existing file has no
                # transcript yet (re-transcribe vs. overwrite).
                transcript_path = state.work_dir / f"{final_path.stem}.transcript.json"
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "already_exists",
                        "basename": basename,
                        "existing_path": str(final_path),
                        "has_transcript": transcript_path.exists(),
                    },
                )
            # Refuse anything that isn't a regular file owned by the server
            # process. This blocks symlinks (which we never create), dirs,
            # special files, and any leftover from another user that happens
            # to live in the uploads dir.
            import stat as _stat
            if not _stat.S_ISREG(entry_stat.st_mode):
                raise HTTPException(status_code=400, detail="cannot overwrite non-file")
            if entry_stat.st_uid != os.getuid():
                raise HTTPException(status_code=400, detail="cannot overwrite file owned by another user")
            # Belt-and-braces containment check: resolve must stay inside
            # uploads_dir (final_path is built from validated basename, so
            # this is mostly redundant, but cheap).
            try:
                resolved = final_path.resolve(strict=True)
                resolved.relative_to(uploads_dir.resolve())
            except (OSError, ValueError) as e:
                raise HTTPException(status_code=400, detail="cannot overwrite this path") from e
            # Atomic-ish: unlink uses the un-resolved path so we're operating
            # on the same dirent we just lstat'd. A concurrent rename between
            # lstat and unlink could in principle target a different dirent;
            # this is acceptable under the trusted-uploads-dir assumption
            # (single server process owns this directory).
            try:
                final_path.unlink()
            except FileNotFoundError:
                pass  # someone else cleaned up; proceed to fresh upload
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"failed to remove existing file: {e}") from e

        upload_id = secrets.token_urlsafe(32)
        tmp_path = _chunk_tmp_path(upload_id)
        tmp_path.touch(mode=0o600, exist_ok=False)
        _save_chunk_session(upload_id, {
            "basename": basename,
            "size": size,
            "received": 0,
            "last_index": -1,
            "tmp_path": str(tmp_path),
            "created_ts": time.time(),
        })
        return JSONResponse({
            "upload_id": upload_id,
            "chunk_size": CHUNK_SIZE,
        }, headers={"Cache-Control": "no-store"})

    @app.put("/api/library/upload/{upload_id}/chunk")
    async def library_upload_chunk(upload_id: str, request: Request) -> JSONResponse:
        _cleanup_expired_chunk_uploads()
        upload_id = _validate_upload_id(upload_id)
        async with _chunk_upload_lock(upload_id):
            session = _load_chunk_session(upload_id)
            try:
                chunk_index = int(request.headers.get("x-chunk-index", ""))
            except ValueError as e:
                raise HTTPException(status_code=400, detail="X-Chunk-Index must be an integer") from e
            expected_index = int(session["last_index"]) + 1
            if chunk_index != expected_index:
                raise HTTPException(status_code=409, detail=f"expected chunk index {expected_index}")

            chunk = await request.body()
            if len(chunk) > CHUNK_SIZE:
                raise HTTPException(status_code=413, detail="chunk exceeds 512 KB limit")
            received = int(session["received"]) + len(chunk)
            size = int(session["size"])
            if received > size or received > MAX_FILE_BYTES:
                raise HTTPException(status_code=413, detail="upload exceeds declared size")

            tmp_path = _safe_chunk_tmp_path(upload_id, str(session["tmp_path"]))
            with tmp_path.open("ab") as f:
                f.write(chunk)
            session["received"] = received
            session["last_index"] = chunk_index
            _save_chunk_session(upload_id, session)
            return JSONResponse({
                "next_index": chunk_index + 1,
                "bytes_received": received,
            }, headers={"Cache-Control": "no-store"})

    @app.post("/api/library/upload/{upload_id}/finalize")
    async def library_upload_finalize(upload_id: str, request: Request) -> JSONResponse:
        _cleanup_expired_chunk_uploads()
        upload_id = _validate_upload_id(upload_id)
        async with _chunk_upload_lock(upload_id):
            session = _load_chunk_session(upload_id)
            try:
                payload = await request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail="invalid JSON body") from e
            try:
                chunks = int(payload.get("chunks"))
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail="chunks must be an integer") from e
            if chunks != int(session["last_index"]) + 1:
                raise HTTPException(status_code=409, detail="chunks does not match received chunk count")
            total = int(session["received"])
            if total != int(session["size"]):
                raise HTTPException(status_code=409, detail="upload is incomplete")

            basename = _validate_upload_basename(session["basename"])
            uploads_dir = state.work_dir / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            final_path = uploads_dir / basename
            if final_path.exists():
                raise HTTPException(status_code=409, detail=f"{basename!r} already exists in uploads")

            tmp_path = _safe_chunk_tmp_path(upload_id, str(session["tmp_path"]))
            if not tmp_path.exists():
                raise HTTPException(status_code=404, detail="upload temp file not found")
            os.replace(tmp_path, final_path)
            try:
                _chunk_session_path(upload_id).unlink()
            except FileNotFoundError:
                pass

            _append_jsonl(state.kpi_log_path, {
                "server_ts": time.time(),
                "type": "server.library.uploaded",
                "basename": basename,
                "bytes": total,
                "via": "chunked",
            })
        upload_locks.pop(upload_id, None)
        return JSONResponse({
            "ok": True,
            "path": str(final_path),
            "basename": basename,
            "bytes": total,
        }, headers={"Cache-Control": "no-store"})

    def _audio_from_library_request(body: dict, *, require_string_name: bool = False) -> tuple[Path, str]:
        body = body or {}
        raw_path = body.get("path")
        if raw_path is not None:
            if not isinstance(raw_path, str):
                raise HTTPException(status_code=400, detail="path must be a string")
            candidate_audio = _resolve_browse_path(raw_path)
            if candidate_audio.name.startswith("."):
                raise HTTPException(status_code=400, detail="hidden audio files are not supported")
            label = str(candidate_audio)
        else:
            name = body.get("name")
            if require_string_name and not isinstance(name, str):
                raise HTTPException(status_code=400, detail="name must be a string")
            if not name or not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
                raise HTTPException(status_code=400, detail="name must be a plain filename in the library dir")
            candidate_audio = (state.library_dir / name).resolve()
            label = name
        # Defense in depth: even if scan_library skipped the file (wrong suffix
        # / directory / dotfile / faststart derivative), reject it here too so
        # a hand-crafted POST can't switch the server onto something weird.
        if candidate_audio.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"unsupported audio suffix {candidate_audio.suffix!r}")
        if candidate_audio.name.endswith(".faststart" + candidate_audio.suffix):
            raise HTTPException(status_code=400, detail="faststart derivatives are not selectable")
        if not candidate_audio.exists() or not candidate_audio.is_file():
            raise HTTPException(status_code=404, detail=f"{label!r} not found")
        if not os.access(candidate_audio, os.R_OK):
            raise HTTPException(status_code=403, detail=f"permission denied: {label!r}")
        return candidate_audio, label

    @app.post("/api/library/select")
    def library_select(body: dict) -> dict:
        candidate_audio, label = _audio_from_library_request(body)
        candidate_transcript = state.work_dir / f"{candidate_audio.stem}.transcript.json"
        if not candidate_transcript.exists():
            raise HTTPException(
                status_code=409,
                detail=f"no transcript found for {label!r}; run `podedit transcribe` first",
            )
        try:
            state.load_active(candidate_audio, candidate_transcript)
        except (FileNotFoundError, AudioTranscriptMismatch) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _save_last_active(state)
        _append_jsonl(state.kpi_log_path, {
            "server_ts": time.time(), "type": "server.library.selected",
            "name": candidate_audio.name, "audio_path": str(state.audio_path),
        })
        return {
            "ok": True,
            "active": candidate_audio.name,
            "audio_path": str(state.audio_path),
            "transcript_path": str(state.transcript_path),
            "session_path": str(state.session_path),
        }

    # ------- transcribe (W7.7) -------
    # Lets the UI kick off `podedit transcribe` for any library audio that
    # doesn't have a transcript yet. The actual ASR runs in a worker thread
    # owned by ``transcription_jobs``; this endpoint just validates the
    # request and starts the job. The UI polls /status.
    @app.post("/api/library/transcribe")
    def library_transcribe(body: dict) -> JSONResponse:
        body = body or {}
        raw_model = body.get("model", "tiny")
        if not isinstance(raw_model, str):
            raise HTTPException(status_code=400, detail="model must be a string")
        model = raw_model.strip() or "tiny"
        # Hardcoded allow-list — protects against a hand-crafted POST sending
        # a weird model id that faster-whisper might still try to fetch.
        allowed_models = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}
        if model not in allowed_models:
            raise HTTPException(status_code=400, detail=f"unsupported model {model!r}")
        # beam_size: optional, defaults to 1 (greedy). Allow 1-5 since beams
        # above 5 give negligible quality gains and waste a lot of CPU on
        # a 2-core box.
        raw_beam = body.get("beam_size", 1)
        if not isinstance(raw_beam, int) or isinstance(raw_beam, bool):
            raise HTTPException(status_code=400, detail="beam_size must be an int")
        if raw_beam < 1 or raw_beam > 5:
            raise HTTPException(status_code=400, detail="beam_size must be in [1, 5]")
        beam_size = raw_beam
        # W9 accuracy options. Both are optional strings. Bound the length so
        # a hand-crafted POST can't ship megabytes of "biasing" payload and
        # OOM the decoder context. faster-whisper docs note the model uses
        # the first ~200 tokens of initial_prompt; 2000 chars is well above
        # that. hotwords is similarly capped — it's meant for a small vocab.
        #
        # Contract: we distinguish "field absent" from "field present empty
        # string". Absent => let the JobManager pick its default JA podcast
        # prompt. Empty string => caller explicitly wants raw decoding (no
        # biasing), useful for ablation. For hotwords there's no useful
        # default, so empty/absent both collapse to None.
        if "initial_prompt" in body:
            raw_prompt = body["initial_prompt"]
            if not isinstance(raw_prompt, str):
                raise HTTPException(status_code=400, detail="initial_prompt must be a string")
            if len(raw_prompt) > 2000:
                raise HTTPException(status_code=400, detail="initial_prompt too long (max 2000 chars)")
            initial_prompt = raw_prompt  # may be "" — caller asked for no biasing
        else:
            initial_prompt = None  # JobManager applies its default
        raw_hotwords = body.get("hotwords", "")
        if not isinstance(raw_hotwords, str):
            raise HTTPException(status_code=400, detail="hotwords must be a string")
        if len(raw_hotwords) > 2000:
            raise HTTPException(status_code=400, detail="hotwords too long (max 2000 chars)")
        hotwords = raw_hotwords or None
        candidate_audio, label = _audio_from_library_request(body, require_string_name=("path" not in body))
        name = candidate_audio.name
        transcript_path = state.work_dir / f"{candidate_audio.stem}.transcript.json"
        if transcript_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"transcript already exists for {label!r}",
            )
        try:
            job = transcription_jobs.start(
                name=name,
                audio_path=candidate_audio,
                transcript_path=transcript_path,
                model=model,
                beam_size=beam_size,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
            )
        except RuntimeError as e:
            # Another job is in flight.
            raise HTTPException(status_code=409, detail=str(e)) from e
        _append_jsonl(state.kpi_log_path, {
            "server_ts": time.time(), "type": "server.transcribe.started",
            "name": name, "model": model, "beam_size": beam_size,
            # Length only (not the prompt body) — we don't want big prompts
            # spilling into kpi.jsonl on every job.
            # Length only (not the prompt body) so we don't leak custom prompt
            # text into KPI logs. -1 if the field wasn't supplied (uses default).
            "initial_prompt_len": len(initial_prompt) if initial_prompt is not None else -1,
            "hotwords_len": len(raw_hotwords),
            "job_id": job["job_id"],
        })
        return JSONResponse(job, headers={"Cache-Control": "no-store"})

    @app.get("/api/library/transcribe/status")
    def library_transcribe_status() -> JSONResponse:
        snap = transcription_jobs.snapshot()
        return JSONResponse(
            {"job": snap},
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="ui")
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
