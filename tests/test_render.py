"""Render-pipeline tests for W5.

The synthetic input is a 5s, 48 kHz, mono ramp from 0..1 so each sample's
amplitude encodes its position in seconds — that lets us assert which source
sample landed where in the output without doing FFT-based comparisons.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from podedit.edit import MoveOp, compile_timeline
from podedit.render import RenderError, render_cuts, render_segments


SR = 48000


@pytest.fixture
def ramp_audio(tmp_path: Path) -> tuple[Path, float]:
    duration = 5.0
    n = int(duration * SR)
    # Mono ramp: sample i has value (i / n), so output[k] ≈ source_position(k) / n.
    samples = np.linspace(0.0, 1.0, n, dtype=np.float32)
    src = tmp_path / "ramp.wav"
    sf.write(str(src), samples, SR, subtype="PCM_16")
    return src, duration


def _read(p: Path) -> tuple[np.ndarray, int]:
    a, sr = sf.read(str(p), dtype="float32", always_2d=False)
    return a, sr


def test_no_deletes_outputs_full_audio(ramp_audio, tmp_path: Path) -> None:
    src, dur = ramp_audio
    out = tmp_path / "out.wav"
    result = render_cuts(src, dur, deletes=[], output=out, crossfade_ms=0, lufs_target=None)
    a, sr = _read(out)
    assert sr == SR
    # PCM_16 round-trips with ~1.5e-5 quantization error; the start/end of the ramp
    # should still be recognizable.
    assert a[0] == pytest.approx(0.0, abs=2e-4)
    assert a[-1] == pytest.approx(1.0, abs=2e-4)
    assert result.duration_in == pytest.approx(dur, abs=1e-3)
    assert result.duration_out == pytest.approx(dur, abs=1e-3)
    assert result.crossfade_ms == 0.0


def test_single_delete_at_head_no_xfade(ramp_audio, tmp_path: Path) -> None:
    """Deleting [0, 1) should drop the first second; output starts at the ramp value at t=1s."""
    src, dur = ramp_audio
    out = tmp_path / "out.wav"
    result = render_cuts(src, dur, [(0.0, 1.0)], out, crossfade_ms=0, lufs_target=None)
    a, _ = _read(out)
    assert result.duration_out == pytest.approx(4.0, abs=2e-3)
    # First output sample corresponds to source sample at t≈1s, value ≈ 0.2.
    assert a[0] == pytest.approx(0.2, abs=5e-4)
    assert a[-1] == pytest.approx(1.0, abs=5e-4)


def test_single_delete_at_tail_no_xfade(ramp_audio, tmp_path: Path) -> None:
    src, dur = ramp_audio
    out = tmp_path / "out.wav"
    result = render_cuts(src, dur, [(4.0, 5.0)], out, crossfade_ms=0, lufs_target=None)
    a, _ = _read(out)
    assert result.duration_out == pytest.approx(4.0, abs=2e-3)
    # Last output sample corresponds to source at t≈4s, value ≈ 0.8.
    assert a[-1] == pytest.approx(0.8, abs=5e-4)


def test_middle_delete_with_crossfade_smooths_boundary(ramp_audio, tmp_path: Path) -> None:
    """A hard splice on a ramp creates a discontinuity at the boundary. The
    equal-power crossfade should smear it across the fade window so the
    sample-to-sample diff at the seam shrinks toward zero."""
    src, dur = ramp_audio
    deletes = [(2.0, 3.0)]
    out_hard = tmp_path / "hard.wav"
    out_xfade = tmp_path / "xfade.wav"
    render_cuts(src, dur, deletes, out_hard, crossfade_ms=0, lufs_target=None)
    render_cuts(src, dur, deletes, out_xfade, crossfade_ms=50, lufs_target=None)
    a_hard, _ = _read(out_hard)
    a_xfade, _ = _read(out_xfade)

    # The seam is at edited time 2s (sample 96000 in the hard cut). The
    # max absolute first-difference in a small window around the seam should
    # be noticeably smaller with the crossfade in place.
    seam_hard = int(2.0 * SR)
    win = 200
    diff_hard = np.max(np.abs(np.diff(a_hard[seam_hard - win:seam_hard + win])))
    # With crossfade the cumulative length is shorter (50 ms removed); look near
    # the moved seam to find the smoothed region.
    seam_xfade = int((2.0 - 0.025) * SR)
    diff_xfade = np.max(np.abs(np.diff(a_xfade[seam_xfade - win:seam_xfade + win])))
    assert diff_xfade < diff_hard * 0.5, (
        f"crossfade should reduce seam discontinuity; hard={diff_hard:.4f} xfade={diff_xfade:.4f}"
    )


def test_lufs_normalization_brings_loud_signal_down(tmp_path: Path) -> None:
    """A 1 kHz sine at -3 dB peak is much hotter than -16 LUFS; the renderer
    should attenuate it. Skip the test if pyloudnorm isn't installed."""
    pyln = pytest.importorskip("pyloudnorm")
    sr = SR
    dur = 3.0
    n = int(dur * sr)
    t = np.arange(n) / sr
    sine = (0.7 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    src = tmp_path / "sine.wav"
    sf.write(str(src), sine, sr, subtype="PCM_16")
    out = tmp_path / "norm.wav"

    result = render_cuts(src, dur, [], out, crossfade_ms=0, lufs_target=-16.0)
    # We only measure the output (pyloudnorm post-render). lufs_in is reserved
    # for a future two-pass loudnorm path that has the pre-normalize value.
    assert result.lufs_out is not None
    assert result.sample_rate == SR, "loudnorm should be resampled back to source rate"
    # ffmpeg single-pass loudnorm lands within ~1 LU of the target on a clean
    # sine. Allow some slack for the true-peak ceiling pulling the level back.
    assert -17.0 <= result.lufs_out <= -14.0


def test_all_audio_deleted_raises(ramp_audio, tmp_path: Path) -> None:
    src, dur = ramp_audio
    with pytest.raises(RenderError):
        render_cuts(src, dur, [(0.0, dur)], tmp_path / "x.wav", crossfade_ms=0, lufs_target=None)


def test_overlapping_deletes_merged_in_output(ramp_audio, tmp_path: Path) -> None:
    """Two overlapping deletes should land as a single merged cut in the output."""
    src, dur = ramp_audio
    deletes = [(1.0, 2.5), (2.0, 3.0)]  # merged: (1.0, 3.0)
    out = tmp_path / "out.wav"
    result = render_cuts(src, dur, deletes, out, crossfade_ms=0, lufs_target=None)
    # Expected duration_out = 5 - (3 - 1) = 3s
    assert result.duration_out == pytest.approx(3.0, abs=5e-3)


def test_render_segments_supports_move_op(ramp_audio, tmp_path: Path) -> None:
    src, dur = ramp_audio
    out = tmp_path / "move.wav"
    segments = compile_timeline(dur, [
        MoveOp(op_id="move1", op="move", src_start=1.0, src_end=2.0, target_edited_t=4.0),
    ])

    result = render_segments(src, segments, out, source_duration=dur, move_count=1, crossfade_ms=0, lufs_target=None)
    a, sr = _read(out)

    assert sr == SR
    assert result.duration_out == pytest.approx(5.0, abs=2e-3)
    assert result.keeps == [(0.0, 1.0), (2.0, 4.0), (1.0, 2.0), (4.0, 5.0)]
    assert result.segments_count == 4
    assert result.move_count == 1
    # The inserted segment starts at edited t=3s and comes from source t=1s.
    assert a[int(3.0 * SR)] == pytest.approx(0.2, abs=5e-4)


def test_move_render_with_crossfade_preserves_duration(ramp_audio, tmp_path: Path) -> None:
    """Regression: ffmpeg's atrim can't rewind on a single input. With a
    move op the segments are non-monotonic in source time, and a chained
    acrossfade chain on top of `[0:a]atrim` silently produced ~25 s less
    output than expected. The renderer now switches to per-segment
    `-ss/-t -i` inputs when source order isn't monotonic. This test would
    have caught it (the existing move test used crossfade_ms=0 so it never
    invoked acrossfade).
    """
    src, dur = ramp_audio
    out = tmp_path / "move_xfade.wav"
    segments = compile_timeline(dur, [
        MoveOp(op_id="move1", op="move", src_start=1.0, src_end=2.0, target_edited_t=4.0),
    ])
    result = render_segments(
        src, segments, out, source_duration=dur, move_count=1,
        crossfade_ms=10.0, lufs_target=None,
    )
    # 4 segments → 3 seams. Each 10 ms acrossfade trims 10 ms from total.
    # Expected: 5.0 - 3 * 0.010 = 4.97 s. Allow ffmpeg frame-align slack.
    assert result.duration_out == pytest.approx(4.97, abs=0.05)
    assert result.segments_count == 4
    assert result.move_count == 1
