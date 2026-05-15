"""ASR via faster-whisper. CPU int8 by default; GPU detected if available."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .audio import AudioInfo
from .schema import (
    ModelConfig,
    Segment,
    Transcript,
    Word,
    now_iso,
    SCHEMA_VERSION,
)


@dataclass(frozen=True, slots=True)
class ASRConfig:
    model: str = "small"  # tiny | base | small | medium | large-v3 | large-v3-turbo
    language: str = "ja"
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "auto"  # auto -> int8 on cpu, float16 on cuda
    beam_size: int = 5
    vad_filter: bool = True
    # Decoding options. faster-whisper's `temperature` accepts either a scalar
    # (no fallback) or a sequence (try each on bad segments — high compression
    # ratio, low avg logprob, or no_speech failures). We default to the full
    # ladder for safety; the in-UI worker can override to a shorter ladder for
    # extra speed at controlled risk. ``condition_on_previous_text=True`` is
    # also the faster-whisper default — the CLI keeps it. The in-UI worker
    # explicitly flips it off, because for podcast Japanese with VAD on it
    # tends to reduce cross-segment repetition more than it helps quality.
    temperature: tuple[float, ...] | float = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    condition_on_previous_text: bool = True
    # Accuracy levers (W9). Both bias the decoder toward domain vocab with
    # small runtime overhead relative to ASR.
    # ``initial_prompt`` is conditioning text (e.g. "日本語のポッドキャスト
    # 会話") that improves rare-word recognition; ``hotwords`` is
    # faster-whisper's alternative API with stronger biasing for a small
    # fixed vocabulary (proper nouns, jargon). Either or both may be set.
    initial_prompt: str | None = None
    hotwords: str | None = None
    # Speed knobs (P1 ASR speedup). All CPU-only paths — for GPU these are
    # ignored or overridden.
    # ``cpu_threads`` — 0 = let CTranslate2 pick (typically nproc). Explicit
    # setting helps reproducibility and prevents oversubscription when running
    # alongside other heavy workloads in the same Codespace.
    # ``num_workers`` — number of internal pipeline workers; 1 means single
    # in-flight decode. CT2 default is 1; we keep that on CPU since multiple
    # workers contend for the same cores.
    # ``batched`` / ``batch_size`` — when True, route through
    # ``BatchedInferencePipeline`` which uses VAD to slice the audio and
    # decodes the slices in parallel inside CT2. On 2-core CPU box, codex
    # recommends batch_size 2-4 (above that, contention costs outweigh
    # parallelism). beam_size can stay at 5.
    cpu_threads: int = 0
    num_workers: int = 1
    batched: bool = False
    batch_size: int = 4


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    device: str
    compute_type: str


def _first_temperature(temperature: tuple[float, ...] | float) -> float:
    """Pick the first temperature step. faster-whisper's BatchedInferencePipeline
    only consults a single temperature value (not the full ladder), so we
    capture the effective value explicitly when ``batched=True``."""
    if isinstance(temperature, tuple):
        if not temperature:
            raise ValueError("temperature ladder must not be empty")
        return temperature[0]
    return temperature


def resolve_device(cfg: ASRConfig) -> ResolvedConfig:
    import ctranslate2

    device = cfg.device
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"

    if cfg.compute_type == "auto":
        compute = "float16" if device == "cuda" else "int8"
    else:
        compute = cfg.compute_type
    return ResolvedConfig(device=device, compute_type=compute)


def transcribe(
    source_audio: AudioInfo,
    asr_audio_path: Path,
    cfg: ASRConfig,
    *,
    model_handle: "WhisperModel | None" = None,
) -> tuple[Transcript, Iterator[Segment]]:
    """Run ASR. Returns the Transcript shell and a generator yielding per-segment progress.

    The caller drives the generator; segments are appended to ``Transcript.segments``
    as they arrive (streaming, bounded memory for long episodes). Timestamps are
    anchored to ``source_audio`` (the original, not the 16k mono derived).

    ``model_handle`` lets callers reuse a previously-instantiated WhisperModel
    across multiple ``transcribe()`` calls — saves the 3-10s model-load cost
    that would otherwise be paid once per job. The in-UI job manager uses
    this; the CLI passes None and pays load every run.
    """
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    resolved = resolve_device(cfg)
    # ``batched`` is only honored on CPU. On CUDA the serial path is
    # already GPU-batched internally, and faster-whisper's
    # BatchedInferencePipeline targets CPU silence skipping.
    effective_batched = cfg.batched and resolved.device == "cpu"
    if effective_batched and not cfg.vad_filter:
        # BatchedInferencePipeline requires VAD to slice the audio.
        raise ValueError("batched transcription requires vad_filter=True")
    # faster-whisper v1.2.1 quirks for BatchedInferencePipeline:
    #   * forces ``condition_on_previous_text=False`` internally — we make it
    #     explicit so it lands in transcript metadata
    #   * uses only the first temperature value (no ladder fallback) — we
    #     collapse the ladder explicitly so the recorded config matches
    effective_temperature = (
        _first_temperature(cfg.temperature) if effective_batched else cfg.temperature
    )
    effective_condition_on_previous_text = (
        False if effective_batched else cfg.condition_on_previous_text
    )

    if model_handle is None:
        model = WhisperModel(
            cfg.model,
            device=resolved.device,
            compute_type=resolved.compute_type,
            cpu_threads=cfg.cpu_threads,
            num_workers=cfg.num_workers,
        )
    else:
        model = model_handle

    # VAD tune (codex review of P1 speedup):
    #   - min_silence_duration_ms=500 skips short silences but keeps natural pauses
    #   - speech_pad_ms=300 pads each speech island so VAD-trimmed segments
    #     don't clip word starts/ends in conversational JA
    # Note: this applies to both serial and batched paths, so transcripts
    # produced after this change may have slightly different segment
    # boundaries than older runs (no longer bit-for-bit identical).
    vad_parameters: dict = {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 300,
    }

    if effective_batched:
        # BatchedInferencePipeline slices via VAD and decodes the slices in
        # parallel inside CT2. v1.2.1 forces condition_on_previous_text=False
        # internally and only uses the first temperature value, so this is
        # NOT bit-for-bit equivalent to serial — beam-quality decoding per
        # slice is preserved, but cross-slice context is gone and the
        # temperature fallback ladder is disabled.
        pipeline = BatchedInferencePipeline(model=model)
        segments_iter, info = pipeline.transcribe(
            str(asr_audio_path),
            language=cfg.language,
            beam_size=cfg.beam_size,
            word_timestamps=True,
            vad_filter=cfg.vad_filter,
            vad_parameters=vad_parameters,
            temperature=effective_temperature,
            batch_size=cfg.batch_size,
            initial_prompt=cfg.initial_prompt if cfg.initial_prompt is not None else None,
            hotwords=cfg.hotwords or None,
        )
    else:
        segments_iter, info = model.transcribe(
            str(asr_audio_path),
            language=cfg.language,
            beam_size=cfg.beam_size,
            word_timestamps=True,
            vad_filter=cfg.vad_filter,
            vad_parameters=vad_parameters,
            # Speed knobs — see ASRConfig docstring for the trade-offs.
            temperature=effective_temperature,
            condition_on_previous_text=effective_condition_on_previous_text,
            # Accuracy knobs (W9). Pass only if set so we don't override faster-
            # whisper's defaults when callers don't care.
            initial_prompt=cfg.initial_prompt if cfg.initial_prompt is not None else None,
            hotwords=cfg.hotwords or None,
        )

    # Probe the derived 16k mono once for the asr_audio AudioRef.
    from .audio import probe as audio_probe
    asr_ref = audio_probe(asr_audio_path).to_ref()

    tx = Transcript(
        schema_version=SCHEMA_VERSION,
        source_audio=source_audio.to_ref(),
        asr_audio=asr_ref,
        language=info.language,
        model_config=ModelConfig(
            model=cfg.model,
            language=cfg.language,
            requested_device=cfg.device,
            resolved_device=resolved.device,
            compute_type=resolved.compute_type,
            beam_size=cfg.beam_size,
            vad_filter=cfg.vad_filter,
            # Record what actually went into faster-whisper, not what the
            # caller asked for. Under batched=True we collapse the ladder
            # and force cond=False so the recorded config reflects reality.
            temperature=effective_temperature,
            condition_on_previous_text=effective_condition_on_previous_text,
            initial_prompt=cfg.initial_prompt,
            hotwords=cfg.hotwords,
        ),
        created_at=now_iso(),
    )

    def _drive() -> Iterator[Segment]:
        for idx, s in enumerate(segments_iter):
            seg_id = f"s{idx}"
            seg = Segment(
                id=seg_id,
                start=float(s.start),
                end=float(s.end),
                text=s.text,
                words=[
                    Word(
                        id=f"{seg_id}-w{w_idx}",
                        start=float(w.start),
                        end=float(w.end),
                        text=w.word,
                        confidence=(float(w.probability) if w.probability is not None else None),
                    )
                    for w_idx, w in enumerate(s.words or [])
                ],
            )
            tx.segments.append(seg)
            yield seg

    return tx, _drive()
