"""Audio renderer (W2): apply delete ops to a source file via ffmpeg.

W2 uses an ffmpeg ``atrim`` + ``concat`` filtergraph — no crossfade, no de-click.
That is intentional: W2 only proves "cuts land at the right samples." Sample-
precise PCM rendering, zero-cross snap, and content-aware fades arrive in W5.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .edit import keep_ranges_from_deletes


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RenderResult:
    output: Path
    keeps: list[tuple[float, float]]
    duration_in: float
    duration_out: float


def _build_filtergraph(keeps: list[tuple[float, float]]) -> str:
    if not keeps:
        raise RenderError("No keep ranges; nothing to render.")
    parts: list[str] = []
    for i, (s, e) in enumerate(keeps):
        # asetpts=PTS-STARTPTS is required after atrim so concat sees a 0-based PTS.
        parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[k{i}]")
    inputs = "".join(f"[k{i}]" for i in range(len(keeps)))
    parts.append(f"{inputs}concat=n={len(keeps)}:v=0:a=1[out]")
    return ";".join(parts)


def render_cuts(
    source: Path,
    source_duration: float,
    deletes: list[tuple[float, float]],
    output: Path,
) -> RenderResult:
    """Apply ``deletes`` to ``source`` and write a wav to ``output``."""
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found on PATH")

    keeps = keep_ranges_from_deletes(source_duration, deletes)
    if not keeps:
        raise RenderError("All audio is deleted; no output to render.")

    fc = _build_filtergraph(keeps)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
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

    duration_out = sum(e - s for s, e in keeps)
    return RenderResult(output=output, keeps=keeps, duration_in=source_duration, duration_out=duration_out)
