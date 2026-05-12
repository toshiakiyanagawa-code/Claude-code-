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

from .edit import DeleteOp, TimelineSegment, compile_timeline
from .seam_eval import SeamAnalysis, analyze_seam


class RenderError(RuntimeError):
    pass


# Bump when output-affecting render logic changes so cached previews aren't
# silently reused across upgrades. Server preview cache keys include this.
RENDERER_VERSION = "w7.5"


# Output formats accepted by ``render_cuts``. Picked from the extension of the
# requested output path. mp3/aac/ogg are lossy and intended for distribution;
# wav/flac are lossless and meant for further editing or archival.
_FORMAT_CODECS: dict[str, list[str]] = {
    "wav":  ["-acodec", "pcm_s16le"],
    "flac": ["-acodec", "flac"],
    "mp3":  ["-acodec", "libmp3lame", "-b:a", "96k"],
    "ogg":  ["-acodec", "libvorbis", "-q:a", "5"],
    "m4a":  ["-acodec", "aac", "-b:a", "96k"],
}


def _codec_args_for(output: Path) -> list[str]:
    fmt = output.suffix.lower().lstrip(".")
    if fmt not in _FORMAT_CODECS:
        raise RenderError(
            f"Unsupported output format {output.suffix!r}; expected one of "
            f"{sorted(_FORMAT_CODECS)}"
        )
    return list(_FORMAT_CODECS[fmt])


@dataclass(frozen=True, slots=True)
class LoudnormStats:
    """Pass-1 measurement output from ffmpeg's ``loudnorm`` filter."""
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float


def _measure_loudnorm(
    source: Path,
    keeps: list[tuple[float, float]],
    seam_xfades_ms: list[float],
    *,
    lufs_target: float,
    true_peak_ceiling_dbtp: float,
    source_sample_rate: int | None,
    timeout_sec: float | None = None,
) -> LoudnormStats:
    """Run ffmpeg pass-1 to measure integrated LUFS / true peak / LRA.

    ffmpeg writes its loudnorm JSON to *stderr* even when ``-f null`` is the
    output target. We discard the audio (``-f null -``) and just scrape the
    final JSON block.
    """
    # Same non-monotonic problem as in the apply pass: when move ops put keeps
    # out of source order, a single -i + atrim chain can't rewind. Detect and
    # switch to per-segment -ss/-t inputs.
    use_multi_input = not _is_source_monotonic(keeps)

    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        if use_multi_input:
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[k{i}]")
        else:
            parts.append(f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[k{i}]")
    n = len(keeps)
    if n == 1:
        cur = "[k0]"
    else:
        cur = "[k0]"
        for i in range(1, n):
            x = seam_xfades_ms[i - 1] if i - 1 < len(seam_xfades_ms) else 0.0
            out_label = "[xall]" if i == n - 1 else f"[x{i - 1}]"
            if x > 0:
                parts.append(f"{cur}[k{i}]acrossfade=d={x / 1000.0:.6f}:c1=qsin:c2=qsin{out_label}")
            else:
                parts.append(f"{cur}[k{i}]concat=n=2:v=0:a=1{out_label}")
            cur = out_label
    parts.append(
        f"{cur}loudnorm=I={lufs_target}:TP={true_peak_ceiling_dbtp}:LRA=11:print_format=json[out]"
    )
    fc = ";".join(parts)
    if use_multi_input:
        input_args: list[str] = []
        for s, e in keeps:
            input_args.extend(["-ss", f"{s:.6f}", "-t", f"{(e - s):.6f}", "-i", str(source)])
    else:
        input_args = ["-i", str(source)]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostats",
             *input_args,
             "-filter_complex", fc, "-map", "[out]",
             "-f", "null", "-"],
            capture_output=True, text=True, check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(
            f"loudnorm measurement pass timed out after {timeout_sec}s"
        ) from e
    if proc.returncode != 0:
        raise RenderError(f"loudnorm measurement pass failed: {proc.stderr.strip()[-400:]}")
    # The JSON block is emitted at the very end of stderr.
    import json as _json
    err = proc.stderr
    brace_start = err.rfind("{")
    brace_end = err.rfind("}")
    if brace_start < 0 or brace_end < brace_start:
        raise RenderError("loudnorm measurement: no JSON in ffmpeg stderr")
    blob = err[brace_start:brace_end + 1]
    try:
        data = _json.loads(blob)
    except _json.JSONDecodeError as e:
        raise RenderError(f"loudnorm measurement: malformed JSON ({e})") from e
    return LoudnormStats(
        input_i=float(data["input_i"]),
        input_tp=float(data["input_tp"]),
        input_lra=float(data["input_lra"]),
        input_thresh=float(data["input_thresh"]),
        target_offset=float(data["target_offset"]),
    )


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
    # W7 additions.
    output_format: str = "wav"             # "wav" | "mp3" | "flac" | "ogg" | "m4a"
    lufs_two_pass: bool = False            # True when we ran the measure pass first
    lufs_measured_input: float | None = None  # pass-1 integrated LUFS of the input
    segments_count: int = 0
    move_count: int = 0


def _ebur128_measure(path: Path, *, timeout_sec: float | None = None) -> tuple[float | None, float | None]:
    """Streaming integrated-LUFS + true-peak via ffmpeg's ebur128 filter.

    Doesn't need to load the audio into memory, so this works for full
    30-minute episodes and any output format ffmpeg can decode. Returns
    (lufs, dBTP) or (None, None) if the summary couldn't be parsed.
    """
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found for ebur128 measurement")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-i", str(path),
             "-filter_complex", "ebur128=peak=true",
             "-f", "null", "-"],
            capture_output=True, text=True, check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"ebur128 measurement timed out after {timeout_sec}s") from e
    if proc.returncode != 0:
        raise RenderError(f"ebur128 measurement failed: {proc.stderr.strip()[-300:]}")
    text = proc.stderr
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    integrated = peak = None
    for line in summary.splitlines():
        line = line.strip()
        if line.startswith("I:"):
            try:
                integrated = float(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("Peak:"):
            try:
                peak = float(line.split()[1])
            except (IndexError, ValueError):
                pass
    return integrated, peak


def _probe_output_duration_and_rate(path: Path) -> tuple[float, int]:
    """Return (duration_sec, sample_rate) for any output ffmpeg can produce.

    soundfile only handles wav/flac/ogg; for mp3/m4a we fall back to ffprobe.
    """
    if shutil.which("ffprobe") is None:
        # Best-effort: try soundfile and accept that mp3 won't work without ffprobe.
        info = sf.info(str(path))
        return info.frames / info.samplerate, info.samplerate
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration:stream=sample_rate",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RenderError(f"ffprobe failed on output: {e.stderr.strip()}") from e
    duration_sec = 0.0
    sample_rate = 0
    for line in out.stdout.splitlines():
        if line.startswith("duration="):
            duration_sec = float(line.split("=", 1)[1])
        elif line.startswith("sample_rate="):
            sample_rate = int(line.split("=", 1)[1])
    return duration_sec, sample_rate


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


def _is_source_monotonic(keeps: list[tuple[float, float]]) -> bool:
    """True when every keep begins at or after the previous keep ends.

    Single-input + multiple `atrim` only works for monotonic source access:
    ffmpeg's demuxer advances forward and atrim can't rewind. When move ops
    are present the segments are in EDITED order and may jump backward in
    source space, so we need the per-segment ``-ss/-t -i`` workaround below.
    """
    for i in range(1, len(keeps)):
        if keeps[i][0] < keeps[i - 1][1] - 1e-6:
            return False
    return True


def _build_filtergraph(
    keeps: list[tuple[float, float]],
    seam_xfades_ms: list[float],
    lufs_target: float | None,
    true_peak_ceiling_dbtp: float,
    source_sample_rate: int | None,
    *,
    measured: LoudnormStats | None = None,
    multi_input: bool = False,
) -> str:
    """Build the ffmpeg filter graph.

    ``seam_xfades_ms`` has one entry per interior seam (``len(keeps) - 1``).
    A zero or negative value means the seam is a hard concat; a positive
    value drives an equal-power ``acrossfade``. Mixing is supported per-seam
    so a transient cut can stay short while an adjacent silence-to-silence
    cut takes the full 50 ms.

    When ``multi_input=True`` each keep range is expected to arrive as its
    own input stream (``[0:a]`` ... ``[N-1:a]``) — see ``_render_multi_input``
    — so the head of every chain is just ``asetpts=PTS-STARTPTS`` instead of
    ``atrim`` on a shared input.
    """
    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        if multi_input:
            # Caller passes one -ss/-t input per segment, so we already have
            # the right bytes — just reset PTS to 0 for the chain.
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[k{i}]")
        else:
            # Single input + atrim. Only valid for monotonic source order.
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
        loud_label = "[loud]" if source_sample_rate else "[out]"
        loudnorm = (
            f"loudnorm=I={lufs_target}:TP={true_peak_ceiling_dbtp}:LRA=11"
            ":linear=true"  # required for measured-pass parameters to take effect
        )
        if measured is not None:
            # Two-pass mode: feed the pass-1 measurements back so loudnorm can
            # plan an exact gain rather than guessing from a streaming look-ahead.
            loudnorm += (
                f":measured_I={measured.input_i:.3f}"
                f":measured_TP={measured.input_tp:.3f}"
                f":measured_LRA={measured.input_lra:.3f}"
                f":measured_thresh={measured.input_thresh:.3f}"
                f":offset={measured.target_offset:.3f}"
            )
        parts.append(f"{cur}{loudnorm}{loud_label}")
        if source_sample_rate:
            # loudnorm internally upsamples to 192 kHz, so resample back to the
            # source rate to keep output files at the rate users expect.
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


def render_segments(
    source: Path,
    segments: list[TimelineSegment],
    output: Path,
    *,
    source_duration: float | None = None,
    move_count: int = 0,
    crossfade_ms: float = 10.0,
    lufs_target: float | None = -16.0,
    true_peak_ceiling_dbtp: float = -1.0,
    seam_analysis: bool = True,
    lufs_two_pass: bool = False,
    ffmpeg_timeout_sec: float | None = None,
) -> RenderResult:
    """Render edited-order timeline segments from ``source``.

    Seams use an equal-power constant-energy crossfade (``acrossfade c1=qsin
    c2=qsin``); the splice trims ``crossfade_ms`` from each seam. Set
    ``crossfade_ms=0`` for a hard splice, or ``lufs_target=None`` to skip
    loudness normalization.
    """
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found on PATH")

    keeps = [(seg.source_start, seg.source_end) for seg in segments]
    if not keeps:
        raise RenderError("All audio is deleted; no output to render.")
    if source_duration is None:
        source_duration = max((e for _, e in keeps), default=0.0)

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
    codec_args = _codec_args_for(output)

    measured: LoudnormStats | None = None
    if lufs_target is not None and lufs_two_pass:
        measured = _measure_loudnorm(
            source, keeps, seam_xfades_ms,
            lufs_target=lufs_target,
            true_peak_ceiling_dbtp=true_peak_ceiling_dbtp,
            source_sample_rate=source_sample_rate,
            timeout_sec=ffmpeg_timeout_sec,
        )

    # If any move op put segments in non-monotonic source order, a single
    # ``-i SOURCE`` + atrim can't rewind — ffmpeg's demuxer only goes forward,
    # so atrim returns empty/truncated streams for ranges earlier than ones
    # already consumed. Open the file once per segment with ``-ss/-t -i`` and
    # let acrossfade see them as independent inputs. This is a real W7.5
    # correctness fix; without it move renders silently drop ~25 s of audio.
    use_multi_input = not _is_source_monotonic(keeps)

    fc = _build_filtergraph(
        keeps, seam_xfades_ms, lufs_target, true_peak_ceiling_dbtp, source_sample_rate,
        measured=measured, multi_input=use_multi_input,
    )

    if use_multi_input:
        input_args: list[str] = []
        for s, e in keeps:
            input_args.extend(["-ss", f"{s:.6f}", "-t", f"{(e - s):.6f}", "-i", str(source)])
    else:
        input_args = ["-i", str(source)]
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-nostats",
                *input_args,
                "-filter_complex", fc,
                "-map", "[out]",
                *codec_args,
                str(output),
            ],
            check=True, capture_output=True, text=True,
            timeout=ffmpeg_timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        # Don't leave a half-written file behind if we killed the encode early.
        try:
            output.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass
        raise RenderError(f"ffmpeg render timed out after {ffmpeg_timeout_sec}s") from e
    except subprocess.CalledProcessError as e:
        try:
            output.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass
        raise RenderError(f"ffmpeg render failed: {e.stderr.strip()}") from e

    # Probe the output for accurate duration / sample rate. For mp3/ogg/m4a we
    # fall through to ffprobe since soundfile can only read wav/flac/ogg etc.
    duration_out, sample_rate = _probe_output_duration_and_rate(output)

    lufs_in: float | None = measured.input_i if measured is not None else None
    lufs_out: float | None = None
    true_peak_out: float | None = None
    if lufs_target is not None and duration_out <= 600 and output.suffix.lower() in (".wav", ".flac"):
        # Short lossless output: cheap to read via soundfile + pyloudnorm.
        lufs_out, true_peak_out = _measure_loudness(output)
    elif lufs_target is not None and shutil.which("ffmpeg") is not None:
        # Any other case (mp3, long episode, ogg, m4a): ffmpeg-side ebur128
        # measurement so we never need to fully load the file. The summary
        # block ends with "I:  X LUFS" / "Peak: Y dBFS" lines we can scrape.
        try:
            lufs_out, true_peak_out = _ebur128_measure(output, timeout_sec=ffmpeg_timeout_sec)
        except RenderError:
            pass  # post-check is informational; never let it fail the render

    return RenderResult(
        output=output,
        keeps=keeps,
        duration_in=source_duration,
        duration_out=duration_out,
        sample_rate=sample_rate,
        crossfade_ms=applied_xfade,
        crossfade_ms_requested=requested_xfade,
        lufs_in=lufs_in,
        lufs_out=lufs_out,
        true_peak_dbtp=true_peak_out,
        seam_xfades_ms=seam_xfades_ms,
        seam_analyses=analyses,
        seam_analysis_used=seam_used,
        output_format=output.suffix.lower().lstrip("."),
        lufs_two_pass=lufs_two_pass and measured is not None,
        lufs_measured_input=measured.input_i if measured else None,
        segments_count=len(keeps),
        move_count=move_count,
    )


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
    lufs_two_pass: bool = False,
    ffmpeg_timeout_sec: float | None = None,
) -> RenderResult:
    """Backward-compatible delete-only shim over ``render_segments``."""
    ops = [
        DeleteOp(op_id=f"render-delete-{i}", op="delete", start=s, end=e)
        for i, (s, e) in enumerate(deletes)
    ]
    segments = compile_timeline(source_duration, ops)
    return render_segments(
        source,
        segments,
        output,
        source_duration=source_duration,
        move_count=0,
        crossfade_ms=crossfade_ms,
        lufs_target=lufs_target,
        true_peak_ceiling_dbtp=true_peak_ceiling_dbtp,
        seam_analysis=seam_analysis,
        lufs_two_pass=lufs_two_pass,
        ffmpeg_timeout_sec=ffmpeg_timeout_sec,
    )
