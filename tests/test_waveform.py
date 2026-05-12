"""Waveform-module tests (W7).

ffmpeg is required for decode, so we synthesize wav files on disk and let
``compute_waveform`` decode them like any other input. The temp wav is
discarded inside the function, so the only side effect we care about is the
returned envelope.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from podedit.waveform import (
    WAVEFORM_VERSION,
    Waveform,
    WaveformError,
    compute_waveform,
    get_or_compute_waveform,
)


SR = 16000


def _write_sine(path: Path, *, duration: float, freq: float = 440.0, amp: float = 0.5,
                sr: int = SR) -> None:
    n = int(duration * sr)
    t = np.arange(n) / sr
    samples = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(str(path), samples, sr, subtype="PCM_16")


def test_compute_returns_expected_length_and_step(tmp_path: Path) -> None:
    src = tmp_path / "tone.wav"
    _write_sine(src, duration=2.0)
    wf = compute_waveform(src, target_points=200)
    assert wf.version == WAVEFORM_VERSION
    assert wf.target_points == 200
    assert wf.duration_sec == pytest.approx(2.0, abs=1e-2)
    assert len(wf.min) == len(wf.max) == 200
    # step_sec * target_points ≈ duration (minus tail truncation)
    assert wf.step_sec * wf.target_points <= wf.duration_sec + 1e-6


def test_peaks_track_amplitude(tmp_path: Path) -> None:
    """A 440 Hz sine at 0.5 amplitude should produce ±0.5 envelope per bin."""
    src = tmp_path / "tone.wav"
    _write_sine(src, duration=1.0, amp=0.5)
    wf = compute_waveform(src, target_points=100)
    # Allow some headroom for PCM16 quantization (~3e-5) + a sub-cycle bin that
    # might not reach full peak.
    assert min(wf.min) <= -0.45
    assert max(wf.max) >= 0.45
    assert all(mn <= mx for mn, mx in zip(wf.min, wf.max))


def test_target_points_capped_to_sample_count(tmp_path: Path) -> None:
    """Asking for more bins than samples should silently cap to sample count."""
    src = tmp_path / "short.wav"
    n = 10
    samples = np.linspace(-1.0, 1.0, n, dtype=np.float32)
    sf.write(str(src), samples, SR, subtype="PCM_16")
    wf = compute_waveform(src, target_points=1000)
    assert wf.target_points == n
    assert len(wf.min) == len(wf.max) == n


def test_invalid_target_points_raises(tmp_path: Path) -> None:
    src = tmp_path / "tone.wav"
    _write_sine(src, duration=0.5)
    with pytest.raises(WaveformError):
        compute_waveform(src, target_points=0)


def test_cache_hit_avoids_recompute(tmp_path: Path) -> None:
    src = tmp_path / "tone.wav"
    _write_sine(src, duration=0.5)
    cache = tmp_path / "cache.json"
    wf1 = get_or_compute_waveform(src, cache, target_points=128)
    mtime1 = cache.stat().st_mtime
    # Second call should reuse the cache; the file mtime should be unchanged.
    wf2 = get_or_compute_waveform(src, cache, target_points=128)
    assert cache.stat().st_mtime == mtime1
    assert wf2.target_points == wf1.target_points
    assert wf2.duration_sec == pytest.approx(wf1.duration_sec)
    assert wf2.min == wf1.min


def test_cache_invalidates_on_target_points_change(tmp_path: Path) -> None:
    src = tmp_path / "tone.wav"
    _write_sine(src, duration=0.5)
    cache = tmp_path / "cache.json"
    wf_a = get_or_compute_waveform(src, cache, target_points=64)
    wf_b = get_or_compute_waveform(src, cache, target_points=128)
    assert wf_a.target_points == 64
    assert wf_b.target_points == 128
