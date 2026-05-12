"""Background transcription jobs for the UI (W7.7).

The UI's Open dialog lets the user kick off ``podedit transcribe`` for any
library audio that doesn't have a transcript yet. We can't run that
synchronously in an HTTP handler — even ``tiny`` on a 30-minute episode is
multi-minute. So we spawn a worker thread, expose a polling status endpoint,
and let the modal UI re-fetch the library list when it sees ``status=done``.

Design constraints:

* **One job at a time**. whisper is CPU/GPU heavy; running two concurrent
  jobs would just thrash. ``POST /api/library/transcribe`` returns 409 if
  another is running.
* **The web request returns immediately**. The worker thread owns the long
  task; the request handler only validates and starts.
* **The worker survives the UI**. If the user closes the modal or the
  browser tab, the worker keeps transcribing and the transcript still lands
  on disk. The next time they Open the modal, the file will simply show
  ``has_transcript: true``.
* **State is plain JSON-shaped**. The status endpoint returns a dict that
  the UI can render without further parsing.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..asr import ASRConfig, transcribe
from ..audio import probe as audio_probe, to_wav_16k_mono
from ..edit import sha256_of_file


# Subdirectory under work_dir for derived 16k mono wavs. We deliberately namespace
# these so the CLI's <work_dir>/<stem>.16k.wav convention can't collide with a
# user-owned audio file in the library (which would happen if someone runs
# `serve --work-dir==--library-dir`). Codex flagged this as a data-loss path.
ASR_DERIVED_SUBDIR = "_podedit_asr"


JobStatus = Literal["queued", "running", "done", "error"]


@dataclass
class TranscriptionJob:
    """Snapshot of a transcription job. Always returned via ``to_dict`` so the
    UI sees plain JSON-shaped state."""
    job_id: str
    name: str                       # library filename, e.g. "ep2.m4a"
    audio_path: str
    transcript_path: str
    model: str
    status: JobStatus = "queued"
    started_at: float = 0.0         # epoch seconds, set when status flips to running
    finished_at: float = 0.0
    duration_sec: float | None = None     # set after probe
    progress_audio_sec: float = 0.0       # how far through the audio ASR has covered
    segments: int = 0                     # how many segments produced so far
    error: str | None = None
    log: list[str] = field(default_factory=list)  # short human-readable steps

    def to_dict(self) -> dict:
        elapsed = 0.0
        if self.started_at:
            elapsed = (self.finished_at or time.time()) - self.started_at
        rtf = None
        if elapsed > 0 and self.progress_audio_sec > 0:
            rtf = elapsed / self.progress_audio_sec
        return {
            "job_id": self.job_id,
            "name": self.name,
            "audio_path": self.audio_path,
            "transcript_path": self.transcript_path,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": elapsed,
            "duration_sec": self.duration_sec,
            "progress_audio_sec": self.progress_audio_sec,
            "segments": self.segments,
            "rtf": rtf,
            "error": self.error,
            "log": list(self.log[-12:]),  # tail only — keeps payload tiny
        }


class TranscriptionJobManager:
    """Single-job manager. Holds the most-recent job; refuses concurrent starts."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
        self._lock = threading.Lock()
        self._current: TranscriptionJob | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        # WhisperModel cache. faster-whisper takes 3-10s to instantiate even
        # for ``tiny``; keeping the most-recent handle around saves that on
        # repeat jobs (same model + compute_type). Keyed by (model, compute_type)
        # since changing either requires reloading from scratch. We only keep
        # one handle to bound memory — tiny is ~75MB, small ~470MB.
        self._cached_model_key: tuple[str, str] | None = None
        self._cached_model = None  # type: ignore[var-annotated]

    def snapshot(self) -> dict | None:
        """Return the current/last job state, or None if nothing has run yet."""
        with self._lock:
            return self._current.to_dict() if self._current else None

    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.status == "running"

    # A small library of bias prompts so the UI's quality preset can pick
    # one without the user typing Japanese into the modal. Generic enough
    # to avoid over-biasing toward a single topic; podcast-shaped so the
    # decoder favors conversational form (です/ます, aizuchi) over news-
    # script form. Empty string means "let faster-whisper default win".
    DEFAULT_JA_PODCAST_PROMPT = "日本語のポッドキャスト会話。話し言葉、自然な相槌、固有名詞を含みます。"

    def start(
        self,
        *,
        name: str,
        audio_path: Path,
        transcript_path: Path,
        model: str = "tiny",
        language: str = "ja",
        beam_size: int = 1,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> dict:
        """Start a new transcription job. Raises RuntimeError if one is in flight.

        ``beam_size`` defaults to 1 (greedy) for the in-UI worker — that's the
        single biggest speed knob on CPU. The CLI keeps its higher-default of
        5 for batch / non-interactive use where quality matters more than
        latency. See ASRConfig docstring for the speed-vs-WER trade-off.

        ``initial_prompt`` / ``hotwords`` (W9): bias the decoder toward
        podcast Japanese / specific vocabulary. Tri-state semantics for
        ``initial_prompt``:
          * ``None``         → fall back to ``DEFAULT_JA_PODCAST_PROMPT``.
          * ``""``           → explicit "no biasing" (faster-whisper raw
                                decoding). Useful for ablation tests.
          * any other string → use as the prompt verbatim.
        ``hotwords`` is simpler — there's no useful default, so empty
        strings collapse to ``None`` (= no biasing).
        """
        with self._lock:
            if self._current is not None and self._current.status in ("queued", "running"):
                raise RuntimeError(
                    f"Another transcription is already running: {self._current.name!r}"
                )
            self._seq += 1
            job = TranscriptionJob(
                job_id=f"t{self._seq}-{int(time.time())}",
                name=name,
                audio_path=str(audio_path),
                transcript_path=str(transcript_path),
                model=model,
                status="queued",
            )
            self._current = job

        # Preserve tri-state for prompt (see docstring). For hotwords, collapse
        # empty / whitespace to None since there's no default to suppress.
        if isinstance(initial_prompt, str):
            prompt = initial_prompt  # may be "" — that's a meaningful "no biasing" signal
        else:
            prompt = None
        hw = hotwords.strip() if isinstance(hotwords, str) and hotwords.strip() else None

        # Spawn worker outside the lock so the start() call returns fast.
        t = threading.Thread(
            target=self._run_job,
            args=(job, audio_path, transcript_path, model, language, beam_size, prompt, hw),
            daemon=True,
            name=f"podedit-transcribe-{job.job_id}",
        )
        self._thread = t
        t.start()
        with self._lock:
            return job.to_dict()

    # ----- worker -----

    def _set(self, job: TranscriptionJob, **fields: object) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(job, k, v)

    def _append_log(self, job: TranscriptionJob, line: str) -> None:
        with self._lock:
            job.log.append(line)

    def _get_or_load_model(self, model: str, compute_type: str):
        """Return a cached WhisperModel for (model, compute_type), loading on miss.

        On a cache miss, we drop the previously-cached handle *before* loading
        the new one — otherwise both would briefly be resident, which matters
        on RAM-constrained boxes when stepping from small to large-v3.
        """
        from faster_whisper import WhisperModel

        key = (model, compute_type)
        with self._lock:
            if self._cached_model_key == key and self._cached_model is not None:
                return self._cached_model, True  # cache hit
            # Evict before loading so peak memory is one model, not two.
            self._cached_model = None
            self._cached_model_key = None
        # Load outside the lock — model construction does disk I/O and weight
        # quantization that can take seconds.
        instance = WhisperModel(model, device="cpu", compute_type=compute_type)
        with self._lock:
            self._cached_model = instance
            self._cached_model_key = key
        return instance, False

    def _run_job(
        self,
        job: TranscriptionJob,
        audio_path: Path,
        transcript_path: Path,
        model: str,
        language: str,
        beam_size: int,
        initial_prompt: str | None,
        hotwords: str | None,
    ) -> None:
        try:
            self._set(job, status="running", started_at=time.time())
            self._append_log(job, f"probe {audio_path.name}")
            info = audio_probe(audio_path)
            self._set(job, duration_sec=info.duration_sec)
            self._append_log(
                job,
                f"probed: {info.duration_sec:.1f}s {info.sample_rate}Hz {info.channels}ch",
            )

            # The ASR pipeline wants 16k mono — same derivation the CLI uses,
            # but namespaced into a subdir so we can't overwrite a user file
            # that happens to be named ``<stem>.16k.wav`` (e.g. when library_dir
            # and work_dir point at the same place).
            asr_dir = self._work_dir / ASR_DERIVED_SUBDIR
            asr_dir.mkdir(parents=True, exist_ok=True)
            asr_wav = asr_dir / f"{audio_path.stem}.16k.wav"
            self._append_log(job, f"decode -> {ASR_DERIVED_SUBDIR}/{asr_wav.name}")
            to_wav_16k_mono(audio_path, asr_wav)

            # Pull the WhisperModel from cache if we can, paying the 3-10s
            # load cost only on a miss. The asr.transcribe() function accepts
            # an existing handle via model_handle=... so we don't reinstantiate.
            compute_type = "int8"  # CPU-only on the in-UI worker by design
            t0 = time.time()
            whisper_model, cache_hit = self._get_or_load_model(model, compute_type)
            load_ms = (time.time() - t0) * 1000
            self._append_log(
                job,
                f"model {'cached' if cache_hit else 'loaded'} ({model}/{compute_type}) in {load_ms:.0f}ms",
            )

            # Fast-mode opt-in for the in-UI worker. The CLI keeps the
            # historical defaults (full temperature ladder, cond=True). Here:
            #   - beam_size: from caller (UI default 1 = greedy → 1.65x speedup).
            #   - condition_on_previous_text=False: trims cross-segment state;
            #     for podcast JA with VAD this also reduces repetition loops.
            #   - temperature: we keep the FULL ladder, not a scalar 0.0.
            #     Greedy decoding without any recovery is what Codex flagged
            #     as a quality risk — bad segments would be accepted as-is.
            #     The ladder is only entered for segments that fail
            #     compression-ratio/logprob thresholds, so steady-state
            #     throughput is unchanged.
            # Tri-state prompt resolution (see start() docstring):
            #   None  → default JA podcast prompt (cheap quality bump)
            #   ""    → caller explicitly asked for raw decoding; honor it
            #   "..." → caller-supplied prompt, use verbatim
            if initial_prompt is None:
                effective_prompt = self.DEFAULT_JA_PODCAST_PROMPT
            else:
                effective_prompt = initial_prompt  # may be ""

            cfg = ASRConfig(
                model=model, language=language,
                beam_size=beam_size,
                compute_type=compute_type,
                device="cpu",
                condition_on_previous_text=False,
                # Use the dataclass default (full ladder) — explicit for clarity.
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                initial_prompt=effective_prompt,
                hotwords=hotwords,
            )
            if initial_prompt is None:
                prompt_tag = "default-prompt"
            elif initial_prompt == "":
                prompt_tag = "no-prompt"
            else:
                prompt_tag = f"user-prompt ({len(initial_prompt)} chars)"
            hw_tag = f", hw={len(hotwords)} chars" if hotwords else ""
            self._append_log(job, f"asr start (beam={beam_size}, cond=False, {prompt_tag}{hw_tag})")
            tx, gen = transcribe(info, asr_wav, cfg, model_handle=whisper_model)

            for seg in gen:
                # ASR yields one segment at a time. Update progress with the
                # furthest audio second we've seen — that's what the UI shows
                # as "covered". seg.end is in source-audio seconds (the ASR
                # generator anchors to source, not the 16k derived clip).
                self._set(
                    job,
                    progress_audio_sec=max(job.progress_audio_sec, float(seg.end)),
                    segments=len(tx.segments),
                )

            # The CLI computes sha256 by default. We do too, so library
            # entries don't suddenly start failing mismatch checks later.
            tx.source_audio.sha256 = sha256_of_file(audio_path)
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(tx.to_dict(), ensure_ascii=False, indent=2)
            # Atomic, no-clobber publish: write the payload to a sibling
            # tempfile, then ``os.link`` it to the final path. ``link`` is
            # atomic on POSIX and fails with FileExistsError if the target
            # already exists — so even if a CLI run produces the transcript
            # mid-job, we won't overwrite it. The tempfile is unlinked after
            # publish (or on any failure) so we don't leak partials.
            fd, tmp = tempfile.mkstemp(
                prefix=transcript_path.name + ".",
                dir=str(transcript_path.parent),
            )
            published = False
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(payload)
                try:
                    os.link(tmp, transcript_path)
                    published = True
                except FileExistsError as e:
                    raise RuntimeError(
                        f"transcript already exists at {transcript_path}; "
                        f"a concurrent transcribe wrote it during this job"
                    ) from e
            finally:
                # Always clean up the tempfile — whether publish succeeded
                # (link created a second name; tmp is now redundant) or it
                # failed (we don't want orphans cluttering work_dir).
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if not published:
                # Defensive: the link branch should have already raised, but
                # don't fall through to "wrote …" log on a non-publish path.
                raise RuntimeError(f"failed to publish transcript to {transcript_path}")
            self._append_log(
                job,
                f"wrote {transcript_path.name} "
                f"({len(tx.segments)} segs, "
                f"{sum(len(s.words) for s in tx.segments)} words)",
            )
            self._set(job, status="done", finished_at=time.time())
        except Exception as e:
            tb = traceback.format_exc(limit=4)
            self._append_log(job, f"FAILED: {type(e).__name__}: {e}")
            self._set(
                job,
                status="error",
                error=f"{type(e).__name__}: {e}\n{tb}",
                finished_at=time.time(),
            )
