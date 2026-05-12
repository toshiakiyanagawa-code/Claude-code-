"""Audio renderer (W5): sample-precise cuts with equal-power crossfade.

Runs ffmpeg as a streaming filtergraph: ``atrim`` cuts at sample boundaries,
``acrossfade`` smooths each seam with a constant-energy curve (``qsin`` for
equal-power), and optional ``loudnorm`` does EBU R128 normalization with a
true-peak ceiling. ffmpeg streams the audio rather than loading it all into
memory, so this path scales to multi-hour episodes without the ~700 MB-per-
channel-pair RAM cost a numpy-buffer renderer would incur.

The result wav is always PCM s16le (podcast-friendly, lossless, predictable
size). Re-encoding to mp3/aac for distribution happens in W7 export.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .edit import keep_ranges_from_deletes


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: Path
    keeps: list[tuple[float, float]]
    duration_in: float
    duration_out: float
    sample_rate: int
    crossfade_ms: float
    lufs_in: float | None
    lufs_out: float | None
    true_peak_dbtp: float | None


def _probe_source_sample_rate(source: Path) -> int | None:
    """Return the audio stream's sample rate, or None if probing fails."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(source)],
            check=True, capture_output=True, text=True,
        )
        return int(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _build_filtergraph(
    keeps: list[tuple[float, float]],
    xfade_ms: float,
    lufs_target: float | None,
    true_peak_ceiling_dbtp: float,
    source_sample_rate: int | None,
) -> str:
    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        # asetpts=PTS-STARTPTS resets the timestamps so the downstream filter
        # sees each chunk starting at 0.
        parts.append(f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[k{i}]")

    n = len(keeps)
    if n == 1:
        cur = "[k0]"
    elif xfade_ms > 0:
        # qsin/qsin = quarter-sine curves; sin²+cos²=1, so the combined energy
        # at the splice stays constant (equal-power crossfade).
        d_sec = xfade_ms / 1000.0
        cur = "[k0]"
        for i in range(1, n):
            out_label = "[xall]" if i == n - 1 else f"[x{i - 1}]"
            parts.append(f"{cur}[k{i}]acrossfade=d={d_sec:.6f}:c1=qsin:c2=qsin{out_label}")
            cur = out_label
    else:
        # Hard splice — concat the keep ranges with no smoothing.
        labels = "".join(f"[k{i}]" for i in range(n))
        parts.append(f"{labels}concat=n={n}:v=0:a=1[xall]")
        cur = "[xall]"

    if lufs_target is not None:
        # Single-pass loudnorm. Two-pass (measure then apply) is more accurate
        # and lands at W7 export. loudnorm internally upsamples to 192 kHz, so
        # we aresample back to the source rate to avoid bloating the output.
        loud_label = "[loud]" if source_sample_rate else "[out]"
        parts.append(
            f"{cur}loudnorm=I={lufs_target}:TP={true_peak_ceiling_dbtp}:LRA=11{loud_label}"
        )
        if source_sample_rate:
            parts.append(f"{loud_label}aresample={source_sample_rate}[out]")
    else:
        parts.append(f"{cur}anull[out]")

    return ";".join(parts)


def _measure_loudness(path: Path) -> tuple[float | None, float | None]:
    """Measure integrated LUFS + true-peak dBTP of a wav. pyloudnorm optional."""
    try:
        import pyloudnorm as pyln
    except ImportError:
        return None, None
    try:
        audio, sr = sf.read(str(path), dtype="float64", always_2d=True)
    except sf.LibsndfileError:
        return None, None
    if audio.size == 0:
        return None, None
    meter = pyln.Meter(sr)
    try:
        lufs = float(meter.integrated_loudness(audio))
    except ValueError:
        # pyloudnorm raises on signals shorter than its gating window (~400ms).
        return None, None
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    peak_dbtp = 20.0 * np.log10(peak) if peak > 0 else float("-inf")
    return lufs, peak_dbtp


def render_cuts(
    source: Path,
    source_duration: float,
    deletes: list[tuple[float, float]],
    output: Path,
    *,
    crossfade_ms: float = 10.0,
    lufs_target: float | None = -16.0,
    true_peak_ceiling_dbtp: float = -1.0,
) -> RenderResult:
    """Apply ``deletes`` to ``source`` and write a wav with sample-precise cuts.

    Cuts use an equal-power constant-energy crossfade (``acrossfade c1=qsin
    c2=qsin``); the splice trims ``crossfade_ms`` from each seam. Set
    ``crossfade_ms=0`` for a hard splice, or ``lufs_target=None`` to skip
    loudness normalization. The output is always PCM s16le wav.
    """
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found on PATH")

    keeps = keep_ranges_from_deletes(source_duration, deletes)
    if not keeps:
        raise RenderError("All audio is deleted; no output to render.")

    source_sample_rate = _probe_source_sample_rate(source) if lufs_target is not None else None
    fc = _build_filtergraph(keeps, crossfade_ms, lufs_target, true_peak_ceiling_dbtp, source_sample_rate)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-nostats",
                "-i", str(source),
                "-filter_complex", fc,
                "-map", "[out]",
                "-acodec", "pcm_s16le",
                str(output),
            ],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RenderError(f"ffmpeg render failed: {e.stderr.strip()}") from e

    # Probe the output for accurate duration / sample rate — ffmpeg drops the
    # crossfade-overlap samples (~xfade_ms per seam) so we don't pre-compute it.
    info = sf.info(str(output))
    duration_out = info.frames / info.samplerate

    lufs_in: float | None = None
    lufs_out: float | None = None
    true_peak_out: float | None = None
    if lufs_target is not None:
        # Round-trip measurement for the bench / KPI. Skips if pyloudnorm or the
        # output is too short for the gating window.
        lufs_out, true_peak_out = _measure_loudness(output)

    return RenderResult(
        output=output,
        keeps=keeps,
        duration_in=source_duration,
        duration_out=duration_out,
        sample_rate=info.samplerate,
        crossfade_ms=crossfade_ms,
        lufs_in=lufs_in,
        lufs_out=lufs_out,
        true_peak_dbtp=true_peak_out,
    )
