"""Seam-analysis unit tests (W6).

Most of the module decodes via ffmpeg so the full path is only smoke-tested
on synthetic wav input. ``find_zero_cross`` and the click detector are pure
numpy and get covered here.
"""
from __future__ import annotations

import numpy as np

from podedit.seam_eval import (
    SeamClass,
    detect_clicks,
    find_zero_cross,
    DEFAULT_XFADE_MS,
)


def test_find_zero_cross_returns_target_when_no_crossing() -> None:
    # All-positive signal — no zero crossing anywhere.
    audio = np.array([0.5, 0.6, 0.7, 0.8], dtype=np.float32)
    assert find_zero_cross(audio, target_sample=2, search_window_samples=5) == 2


def test_find_zero_cross_picks_nearest_crossing() -> None:
    # Crossings live between samples 2-3 (0.1 → -0.1) and between 5-6 (-0.1 → 0.1).
    # find_zero_cross returns the sample *before* the sign flip.
    audio = np.array([0.5, 0.3, 0.1, -0.1, -0.3, -0.1, 0.1, 0.3], dtype=np.float32)
    # Target is sample 4. The two pre-crossing indices are 2 and 5 — 5 is closer.
    assert find_zero_cross(audio, target_sample=4, search_window_samples=10) == 5
    # And targeting an earlier sample picks the earlier crossing.
    assert find_zero_cross(audio, target_sample=1, search_window_samples=10) == 2


def test_find_zero_cross_respects_window() -> None:
    # Same signal as above but with a tight window — should fall back to target.
    audio = np.array([0.5, 0.3, 0.1, -0.1, -0.3, -0.1, 0.1, 0.3], dtype=np.float32)
    # Window of 1 around sample 0 excludes any crossing.
    assert find_zero_cross(audio, target_sample=0, search_window_samples=1) == 0


def test_find_zero_cross_handles_empty_audio() -> None:
    assert find_zero_cross(np.array([], dtype=np.float32), target_sample=0,
                           search_window_samples=10) == 0


def test_detect_clicks_flags_spike() -> None:
    sr = 1000
    audio = np.zeros(sr, dtype=np.float32)
    audio[500] = 0.9  # sudden spike — should register as a click candidate
    clicks = detect_clicks(audio, sr, delta_threshold=0.3)
    assert any(abs(c["position_sec"] - 0.5) < 0.01 for c in clicks)


def test_detect_clicks_near_seam_flag() -> None:
    sr = 1000
    audio = np.zeros(sr, dtype=np.float32)
    audio[500] = 0.9
    clicks = detect_clicks(audio, sr, expected_seams_sec=[0.5], delta_threshold=0.3, window_ms=5)
    assert clicks and clicks[0]["near_seam"] is True


def test_detect_clicks_quiet_signal_returns_empty() -> None:
    # Tone at constant amplitude — diffs are ~zero, nothing crosses threshold.
    sr = 48000
    t = np.arange(sr) / sr
    audio = (0.1 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)
    clicks = detect_clicks(audio, sr, delta_threshold=0.3)
    assert clicks == []


def test_default_xfade_table_covers_every_seam_class() -> None:
    for k in SeamClass:
        assert k in DEFAULT_XFADE_MS
        assert 0.0 < DEFAULT_XFADE_MS[k] <= 100.0
