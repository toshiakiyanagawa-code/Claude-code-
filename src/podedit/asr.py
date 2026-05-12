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
) -> tuple[Transcript, Iterator[Segment]]:
    """Run ASR. Returns the Transcript shell and a generator yielding per-segment progress.

    The caller drives the generator; segments are appended to ``Transcript.segments``
    as they arrive (streaming, bounded memory for long episodes). Timestamps are
    anchored to ``source_audio`` (the original, not the 16k mono derived).
    """
    from faster_whisper import WhisperModel

    resolved = resolve_device(cfg)
    model = WhisperModel(cfg.model, device=resolved.device, compute_type=resolved.compute_type)

    segments_iter, info = model.transcribe(
        str(asr_audio_path),
        language=cfg.language,
        beam_size=cfg.beam_size,
        word_timestamps=True,
        vad_filter=cfg.vad_filter,
        vad_parameters={"min_silence_duration_ms": 500},
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
