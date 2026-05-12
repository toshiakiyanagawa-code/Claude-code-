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


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    device: str
    compute_type: str


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
    from faster_whisper import WhisperModel

    resolved = resolve_device(cfg)
    if model_handle is None:
        model = WhisperModel(cfg.model, device=resolved.device, compute_type=resolved.compute_type)
    else:
        model = model_handle

    segments_iter, info = model.transcribe(
        str(asr_audio_path),
        language=cfg.language,
        beam_size=cfg.beam_size,
        word_timestamps=True,
        vad_filter=cfg.vad_filter,
        vad_parameters={"min_silence_duration_ms": 500},
        # Speed knobs — see ASRConfig docstring for the trade-offs.
        temperature=cfg.temperature,
        condition_on_previous_text=cfg.condition_on_previous_text,
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
            temperature=cfg.temperature,
            condition_on_previous_text=cfg.condition_on_previous_text,
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
