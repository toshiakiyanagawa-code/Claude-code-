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
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from .edit import keep_ranges_from_deletes
from .seam_eval import SeamAnalysis, analyze_seam


class RenderError(RuntimeError):
    pass


# Bump when output-affecting render logic changes so cached previews aren't
# silently reused across upgrades. Server preview cache keys include this.
RENDERER_VERSION = "w6.0"


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: Path
    keeps: list[tuple[float, float]]
    duration_in: float
    duration_out: float
    sample_rate: int
    crossfade_ms: float          # the *applied* xfade (may be clamped below requested)
    crossfade_ms_requested: float
    lufs_in: float | None
    lufs_out: float | None
    true_peak_dbtp: float | None
    renderer_version: str = RENDERER_VERSION
    # W6 additions. ``seam_xfades_ms`` is one entry per interior boundary in
    # the same order as keeps; ``seam_analyses`` carries the underlying
    # classification + zero-cross snap displacement for diagnostic logging.
    seam_xfades_ms: list[float] = field(default_factory=list)
    seam_analyses: list[dict] = field(default_factory=list)
    seam_analysis_used: bool = False


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
    seam_xfades_ms: list[float],
    lufs_target: float | None,
    true_peak_ceiling_dbtp: float,
    source_sample_rate: int | None,
) -> str:
    """Build the ffmpeg filter graph.

    ``seam_xfades_ms`` has one entry per interior seam (``len(keeps) - 1``).
    A zero or negative value means the seam is a hard concat; a positive
    value drives an equal-power ``acrossfade``. Mixing is supported per-seam
    so a transient cut can stay short while an adjacent silence-to-silence
    cut takes the full 50 ms.
    """
    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        # asetpts=PTS-STARTPTS resets the timestamps so the downstream filter
        # sees each chunk starting at 0.
        parts.append(f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[k{i}]")

    n = len(keeps)
    if n == 1:
        cur = "[k0]"
    else:
        # qsin/qsin = quarter-sine curves; sin²+cos²=1, so combined energy at
        # the splice stays constant (equal-power crossfade). A hard concat per
        # seam falls through to the n=2 acconcat path.
        cur = "[k0]"
        for i in range(1, n):
            xfade = seam_xfades_ms[i - 1] if i - 1 < len(seam_xfades_ms) else 0.0
            out_label = "[xall]" if i == n - 1 else f"[x{i - 1}]"
            if xfade > 0:
                d_sec = xfade / 1000.0
                parts.append(f"{cur}[k{i}]acrossfade=d={d_sec:.6f}:c1=qsin:c2=qsin{out_label}")
            else:
                parts.append(f"{cur}[k{i}]concat=n=2:v=0:a=1{out_label}")
            cur = out_label

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
    seam_analysis: bool = True,
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

    requested_xfade = crossfade_ms

    # Optional pre-analysis pass (W6): for each interior boundary, snap toward
    # a zero-crossing within ±20 ms and pick a content-aware crossfade length.
    # We modify the keep ranges in place (mutable copies) and build a per-seam
    # xfade list that the filter graph consumes.
    analyses: list[dict] = []
    mutable_keeps = [list(k) for k in keeps]
    seam_xfades_ms: list[float] = []
    seam_used = False
    if seam_analysis and len(mutable_keeps) > 1:
        seam_used = True
        for i in range(1, len(mutable_keeps)):
            orig_end = float(mutable_keeps[i - 1][1])
            orig_start = float(mutable_keeps[i][0])
            end_seam: SeamAnalysis = analyze_seam(source, orig_end)
            start_seam: SeamAnalysis = analyze_seam(source, orig_start)

            # Bound the snap so we never:
            #  - rewind past the keep's own start (`start < end` must hold);
            #  - cross more than half the original delete on either side, so
            #    snap can't eat into the *other* keep's content.
            delete_span = max(0.0, orig_start - orig_end)
            half_span = delete_span / 2.0 if delete_span > 0 else 0.020
            keep_left_start = mutable_keeps[i - 1][0]
            keep_right_end = mutable_keeps[i][1]

            new_end = max(keep_left_start + 1e-3, min(end_seam.snapped_sec, orig_end + half_span))
            new_start = min(keep_right_end - 1e-3, max(start_seam.snapped_sec, orig_start - half_span))
            mutable_keeps[i - 1][1] = new_end
            mutable_keeps[i][0] = new_start

            # Take the shorter recommendation so a transient on either side
            # caps the fade. The user's ``crossfade_ms`` still acts as an
            # upper bound so a UI slider stays authoritative.
            xfade = min(end_seam.recommended_xfade_ms, start_seam.recommended_xfade_ms, crossfade_ms)
            seam_xfades_ms.append(xfade)
            analyses.append({
                "seam_index": i,
                "end_seam_sec": end_seam.seam_sec,
                "end_snapped_sec": new_end,
                "end_class": end_seam.klass.value,
                "end_rms_db": end_seam.rms_db,
                "end_flux": end_seam.spectral_flux,
                "start_seam_sec": start_seam.seam_sec,
                "start_snapped_sec": new_start,
                "start_class": start_seam.klass.value,
                "start_rms_db": start_seam.rms_db,
                "start_flux": start_seam.spectral_flux,
                "applied_xfade_ms": xfade,
            })
    else:
        # Uniform xfade fallback (W5 behavior).
        seam_xfades_ms = [crossfade_ms] * max(0, len(mutable_keeps) - 1)
    keeps = [tuple(k) for k in mutable_keeps]

    # acrossfade requires both inputs to be at least d long. Clamp per-seam.
    for idx, x in enumerate(seam_xfades_ms):
        if x <= 0:
            continue
        left_len = (keeps[idx][1] - keeps[idx][0]) * 1000.0
        right_len = (keeps[idx + 1][1] - keeps[idx + 1][0]) * 1000.0
        cap = max(0.0, min(left_len, right_len) - 1.0)
        if cap < x:
            seam_xfades_ms[idx] = cap

    applied_xfade = max(seam_xfades_ms) if seam_xfades_ms else 0.0

    source_sample_rate = _probe_source_sample_rate(source) if lufs_target is not None else None
    fc = _build_filtergraph(keeps, seam_xfades_ms, lufs_target, true_peak_ceiling_dbtp, source_sample_rate)
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
        # Round-trip post-render measurement. For long outputs the file would
        # have to be loaded fully into memory (~700 MB for 30 min stereo
        # float64) so cap by duration — the bench KPI is more useful for
        # short audition previews than for full episodes.
        if duration_out <= 600:  # 10 minutes
            lufs_out, true_peak_out = _measure_loudness(output)

    return RenderResult(
        output=output,
        keeps=keeps,
        duration_in=source_duration,
        duration_out=duration_out,
        sample_rate=info.samplerate,
        crossfade_ms=applied_xfade,
        crossfade_ms_requested=requested_xfade,
        lufs_in=lufs_in,
        lufs_out=lufs_out,
        true_peak_dbtp=true_peak_out,
        seam_xfades_ms=seam_xfades_ms,
        seam_analyses=analyses,
        seam_analysis_used=seam_used,
    )
