"""Waveform peaks for UI display (W7).

Computes a downsampled (min, max) envelope of the source audio: each output
bin stores the lowest and highest sample amplitude in that bin's source-time
slice. UI renders the pair as a vertical line per bin, which is enough to
visualize the audio's overall energy without streaming the raw samples.

The whole module deliberately decodes through ffmpeg → soundfile so the input
format doesn't matter, and decodes at 16 kHz mono — band-limited speech is
plenty accurate for an envelope display and the temp wav is small.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf


# Bump if the schema or numeric encoding of waveform JSON changes. Server
# caches embed this so clients refresh after a server upgrade.
WAVEFORM_VERSION = 1


class WaveformError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Waveform:
    version: int
    duration_sec: float
    sample_rate: int
    target_points: int
    step_sec: float
    min: list[float]
    max: list[float]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "duration_sec": self.duration_sec,
            "sample_rate": self.sample_rate,
            "target_points": self.target_points,
            "step_sec": self.step_sec,
            "min": self.min,
            "max": self.max,
        }


def compute_waveform(source: Path, *, target_points: int = 4000) -> Waveform:
    """Decode ``source`` at 16 kHz mono and bin it into ``target_points`` envelope cells.

    Memory is bounded by the temp wav size (≤ 2 MB for 30 min @ 16 kHz mono
    s16le) and the numpy array of the same. Safe to call on multi-hour files
    without the W5 OOM risk.
    """
    if shutil.which("ffmpeg") is None:
        raise WaveformError("ffmpeg not found on PATH")
    if target_points <= 0:
        raise WaveformError("target_points must be > 0")

    fd, tmp = tempfile.mkstemp(prefix="podedit-wf-", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(source),
             "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", tmp],
            check=True, capture_output=True, text=True,
        )
        audio, sr = sf.read(tmp, dtype="float32", always_2d=False)
    except subprocess.CalledProcessError as e:
        raise WaveformError(f"ffmpeg decode failed: {e.stderr.strip()}") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    n = int(audio.size)
    if n == 0:
        return Waveform(
            version=WAVEFORM_VERSION, duration_sec=0.0, sample_rate=sr,
            target_points=0, step_sec=0.0, min=[], max=[],
        )

    bins = min(target_points, n)
    chunk = n // bins
    if chunk < 1:
        chunk = 1
        bins = n
    usable = chunk * bins
    grid = audio[:usable].reshape(bins, chunk)
    peaks_min = grid.min(axis=1).tolist()
    peaks_max = grid.max(axis=1).tolist()
    # Don't lose the tail. If reshape truncated `n - usable` samples (always
    # < chunk), fold them into the final bin so the envelope covers the whole
    # source duration. Without this the waveform UI ends a few ms before the
    # actual EOF.
    if n > usable:
        tail = audio[usable:]
        peaks_min[-1] = min(peaks_min[-1], float(tail.min()))
        peaks_max[-1] = max(peaks_max[-1], float(tail.max()))

    return Waveform(
        version=WAVEFORM_VERSION,
        duration_sec=n / sr,
        sample_rate=sr,
        target_points=bins,
        step_sec=chunk / sr,
        min=[float(x) for x in peaks_min],
        max=[float(x) for x in peaks_max],
    )


def get_or_compute_waveform(
    source: Path,
    cache_path: Path,
    *,
    target_points: int = 4000,
    audio_sha256: str | None = None,
) -> Waveform:
    """Cached variant. Reads ``cache_path`` if it's up to date with the source.

    The cache invalidates on any of:
      - waveform schema bump (``WAVEFORM_VERSION``),
      - source mtime newer than cache mtime,
      - explicit ``audio_sha256`` mismatch (if the caller has one).
    """
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            ok = (
                data.get("version") == WAVEFORM_VERSION
                and data.get("target_points") == target_points
                and cache_path.stat().st_mtime >= source.stat().st_mtime
                and (audio_sha256 is None or data.get("source_sha256") == audio_sha256)
            )
            if ok:
                return Waveform(
                    version=data["version"],
                    duration_sec=float(data["duration_sec"]),
                    sample_rate=int(data["sample_rate"]),
                    target_points=int(data["target_points"]),
                    step_sec=float(data["step_sec"]),
                    min=list(data["min"]),
                    max=list(data["max"]),
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # fall through to recompute

    wf = compute_waveform(source, target_points=target_points)
    payload = wf.to_dict()
    if audio_sha256 is not None:
        payload["source_sha256"] = audio_sha256
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False))
    return wf
