"""Seam analysis (W6): inspect short audio windows around each cut boundary.

W5 left every cut at the user-selected sample with a fixed 10 ms equal-power
crossfade. That works most of the time but creates audible clicks when the
boundary lands mid-vowel or mid-plosive. W6 introduces a *pre-analysis* layer:
before invoking the main ffmpeg render, we extract small audio windows around
each cut boundary, decide:

  - the nearest zero-crossing within ±20 ms (sample-precise click suppression);
  - whether the boundary sits in silence, voiced material, or a consonant
    transient (drives the per-seam crossfade length).

The module is intentionally narrow — pure-numpy analysis on short windows
loaded via ffmpeg+soundfile. Audio buffers stay tiny (≤ 200 ms × 2 channels
× float32 ≈ 80 KB per seam) so we never approach the OOM that killed the
W5 numpy-buffer prototype.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
import soundfile as sf


class SeamClass(str, Enum):
    SILENCE = "silence"
    VOICE = "voice"
    CONSONANT = "consonant"  # transient / plosive
    UNKNOWN = "unknown"


# Crossfade length defaults per content class. Picked from the W6 brief:
#   - silence/room tone: longer fade is inaudible and bridges noise floor
#   - voice (sustained vowel-ish material): medium fade smooths the splice
#   - consonant/transient: shorter fade — long fade smears the attack and
#     sounds worse than a clean cut
DEFAULT_XFADE_MS = {
    SeamClass.SILENCE: 50.0,
    SeamClass.VOICE: 20.0,
    SeamClass.CONSONANT: 8.0,
    SeamClass.UNKNOWN: 10.0,  # falls back to the W5 default
}


@dataclass(frozen=True, slots=True)
class SeamAnalysis:
    seam_sec: float                # original cut-boundary timestamp on source timeline
    snapped_sec: float             # post zero-cross snap; may differ by < 20 ms
    klass: SeamClass
    recommended_xfade_ms: float    # per-seam pick from DEFAULT_XFADE_MS, overridable
    rms_db: float                  # window energy
    spectral_flux: float           # heuristic transient detector
    zero_cross_offset_samples: int # snap displacement; 0 if no snap needed


# ---------- low-level helpers ----------

def _extract_window(source: Path, center_sec: float, half_window_ms: float,
                    target_sr: int) -> tuple[np.ndarray, int]:
    """Decode a small window centered on ``center_sec`` from ``source``.

    Streams via ffmpeg → temp wav → soundfile, so the input format doesn't
    matter and total RAM is bounded by the window size, not the file size.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    start = max(0.0, center_sec - half_window_ms / 1000.0)
    dur = (half_window_ms * 2) / 1000.0
    fd, tmp = tempfile.mkstemp(prefix="podedit-seam-", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{start:.6f}", "-t", f"{dur:.6f}",
             "-i", str(source),
             "-ar", str(target_sr), "-ac", "1",
             "-c:a", "pcm_f32le", tmp],
            check=True, capture_output=True, text=True,
        )
        audio, sr = sf.read(tmp, dtype="float32", always_2d=False)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return audio, sr


def find_zero_cross(audio: np.ndarray, target_sample: int,
                    search_window_samples: int) -> int:
    """Return the sample index of the zero-crossing closest to ``target_sample``.

    Splicing at a zero-crossing eliminates the step discontinuity that
    produces a "click" — both signals are at amplitude 0, so the concat point
    is continuous in value (even if not in derivative).

    If no zero-crossing exists in the search window the original index is
    returned.
    """
    if audio.size == 0:
        return target_sample
    lo = max(0, target_sample - search_window_samples)
    hi = min(audio.size - 1, target_sample + search_window_samples)
    if hi <= lo:
        return target_sample
    region = audio[lo:hi + 1]
    # Detect sign changes between consecutive samples.
    signs = np.signbit(region)
    crossings = np.where(np.diff(signs))[0]
    if crossings.size == 0:
        return target_sample
    # Convert region-local indices to global ones and pick the one closest to
    # target_sample. The +0 keeps us on the sample *before* the crossing,
    # which is the conventional convention for splice points.
    global_idx = lo + crossings
    best = int(global_idx[np.argmin(np.abs(global_idx - target_sample))])
    return best


def _rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -float("inf")
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if rms <= 1e-9:
        return -float("inf")
    return 20.0 * np.log10(rms)


def _spectral_flux(x: np.ndarray, sr: int, hop_ms: float = 5.0) -> float:
    """Return a single scalar: peak frame-to-frame spectral change in the window.

    Implementation is a poor man's STFT: short FFT frames, take the magnitude
    spectrum, sum the positive differences between consecutive frames. A
    high flux means the audio changed timbre suddenly (typical of consonants
    and other transients).
    """
    n = x.size
    if n < 64:
        return 0.0
    hop = max(64, int(hop_ms * sr / 1000.0))
    frame = hop * 2
    if frame > n:
        return 0.0
    # Trim to a whole number of hops
    n_frames = (n - frame) // hop + 1
    if n_frames < 2:
        return 0.0
    prev_mag = None
    fluxes = []
    for i in range(n_frames):
        start = i * hop
        seg = x[start:start + frame] * np.hanning(frame).astype(np.float32)
        mag = np.abs(np.fft.rfft(seg))
        if prev_mag is not None:
            diff = mag - prev_mag
            diff[diff < 0] = 0.0
            fluxes.append(float(diff.sum()))
        prev_mag = mag
    return max(fluxes) if fluxes else 0.0


# ---------- per-seam analysis ----------

# Thresholds tuned for normalized speech at ~-20 to -16 LUFS source levels.
# These aren't sacred — W6 evaluation should validate them on the real episode.
SILENCE_RMS_DB = -45.0       # below this, treat as silence/room tone
CONSONANT_FLUX_THRESHOLD = 2.5  # peak spectral flux above this = transient
ZERO_CROSS_WINDOW_MS = 20.0  # ±20 ms search per the W6 brief
ANALYSIS_WINDOW_MS = 50.0    # half-window around the seam for RMS/flux


def analyze_seam(
    source: Path,
    seam_sec: float,
    target_sr: int = 16000,
) -> SeamAnalysis:
    """Classify the cut boundary at ``seam_sec`` and return snap + xfade hints.

    Uses a 16 kHz mono mixdown internally because it's cheap to decode and
    classification works fine on band-limited speech.
    """
    audio, sr = _extract_window(source, seam_sec, ANALYSIS_WINDOW_MS, target_sr)
    if audio.size == 0:
        return SeamAnalysis(
            seam_sec=seam_sec, snapped_sec=seam_sec, klass=SeamClass.UNKNOWN,
            recommended_xfade_ms=DEFAULT_XFADE_MS[SeamClass.UNKNOWN],
            rms_db=-float("inf"), spectral_flux=0.0, zero_cross_offset_samples=0,
        )

    rms_db = _rms_db(audio)
    flux = _spectral_flux(audio, sr)

    if rms_db < SILENCE_RMS_DB:
        klass = SeamClass.SILENCE
    elif flux > CONSONANT_FLUX_THRESHOLD:
        klass = SeamClass.CONSONANT
    else:
        klass = SeamClass.VOICE

    # Where is the seam in the decoded window? `_extract_window` requests
    # ``seam - half_window`` as the ffmpeg ``-ss``; if that clamps to 0 (seam
    # is closer than 50 ms to the file head), the window starts at 0 and the
    # seam isn't at the geometric center anymore. Same logic for tail clips.
    window_start = max(0.0, seam_sec - ANALYSIS_WINDOW_MS / 1000.0)
    seam_offset_samples = max(0, min(audio.size - 1, int(round((seam_sec - window_start) * sr))))

    # Tighter zero-cross window for consonants — moving the splice by 20 ms
    # inside a plosive smears the attack worse than the click we're avoiding.
    if klass == SeamClass.SILENCE:
        snapped_sample = seam_offset_samples
    else:
        snap_ms = 10.0 if klass == SeamClass.CONSONANT else ZERO_CROSS_WINDOW_MS
        snap_window = int(snap_ms * sr / 1000)
        snapped_sample = find_zero_cross(audio, seam_offset_samples, snap_window)
    offset = snapped_sample - seam_offset_samples
    snapped_sec = seam_sec + offset / sr

    return SeamAnalysis(
        seam_sec=seam_sec,
        snapped_sec=snapped_sec,
        klass=klass,
        recommended_xfade_ms=DEFAULT_XFADE_MS[klass],
        rms_db=rms_db,
        spectral_flux=flux,
        zero_cross_offset_samples=offset,
    )


# ---------- click detection over a finished render ----------

def detect_clicks(
    audio: np.ndarray,
    sr: int,
    expected_seams_sec: list[float] | None = None,
    *,
    window_ms: float = 5.0,
    delta_threshold: float = 0.25,
) -> list[dict]:
    """Find suspicious sample-to-sample jumps in a rendered output.

    The metric is the absolute value of the *signed* sample diff — clicks
    show up as a sudden swing in amplitude, e.g. +0.5 → -0.5 is a 1.0 delta
    that ``abs(audio)`` then ``diff`` would silently miss (|0.5|-|−0.5|=0).
    For stereo we take the per-channel max so a click on either channel is
    flagged.

    Returns one record per click candidate:
    ``{position_sec, delta, near_seam}``. ``near_seam`` is True if the click
    sits within ``window_ms`` of any listed expected seam.
    """
    if audio.size < 2:
        return []
    if audio.ndim == 2:
        # Per-channel signed diff first, then take the largest absolute swing
        # across channels at each sample.
        per_chan = np.abs(np.diff(audio, axis=0))
        diffs = np.max(per_chan, axis=1)
    else:
        diffs = np.abs(np.diff(audio))
    if diffs.size == 0:
        return []
    candidates = np.where(diffs > delta_threshold)[0]
    win = int(window_ms * sr / 1000)
    out: list[dict] = []
    for c in candidates:
        pos_sec = float(c / sr)
        near = False
        if expected_seams_sec:
            for s in expected_seams_sec:
                if abs(pos_sec - s) * sr <= win:
                    near = True
                    break
        out.append({"position_sec": pos_sec, "delta": float(diffs[c]), "near_seam": near})
    return out
