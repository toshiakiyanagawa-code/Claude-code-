import json

from podedit.edit import EditSession, keep_ranges_from_deletes
from podedit.schema import AudioRef


def test_no_deletes_keeps_everything() -> None:
    assert keep_ranges_from_deletes(100.0, []) == [(0.0, 100.0)]


def test_single_delete_middle() -> None:
    assert keep_ranges_from_deletes(100.0, [(30.0, 40.0)]) == [(0.0, 30.0), (40.0, 100.0)]


def test_delete_at_start() -> None:
    assert keep_ranges_from_deletes(100.0, [(0.0, 10.0)]) == [(10.0, 100.0)]


def test_delete_at_end() -> None:
    assert keep_ranges_from_deletes(100.0, [(90.0, 100.0)]) == [(0.0, 90.0)]


def test_delete_entire_span() -> None:
    assert keep_ranges_from_deletes(100.0, [(0.0, 100.0)]) == []


def test_overlapping_deletes_merged() -> None:
    # 10-30 and 20-50 overlap -> 10-50 cut
    assert keep_ranges_from_deletes(100.0, [(10.0, 30.0), (20.0, 50.0)]) == [
        (0.0, 10.0),
        (50.0, 100.0),
    ]


def test_adjacent_deletes_merged() -> None:
    # 10-30 and 30-50 are touching -> merge to 10-50
    assert keep_ranges_from_deletes(100.0, [(10.0, 30.0), (30.0, 50.0)]) == [
        (0.0, 10.0),
        (50.0, 100.0),
    ]


def test_unsorted_deletes() -> None:
    assert keep_ranges_from_deletes(100.0, [(70.0, 80.0), (20.0, 30.0)]) == [
        (0.0, 20.0),
        (30.0, 70.0),
        (80.0, 100.0),
    ]


def test_clamps_out_of_bounds_deletes() -> None:
    # Deletes that extend past duration are clamped, not rejected
    assert keep_ranges_from_deletes(100.0, [(-5.0, 10.0), (95.0, 200.0)]) == [(10.0, 95.0)]


def test_zero_duration_returns_empty() -> None:
    assert keep_ranges_from_deletes(0.0, []) == []


def test_invalid_delete_dropped() -> None:
    # end <= start is silently dropped at this layer (CLI rejects upstream)
    assert keep_ranges_from_deletes(100.0, [(50.0, 50.0), (60.0, 70.0)]) == [
        (0.0, 60.0),
        (70.0, 100.0),
    ]


def test_completely_out_of_range_deletes_ignored() -> None:
    # Deletes entirely outside [0, duration) are dropped without affecting output
    assert keep_ranges_from_deletes(100.0, [(-30.0, -10.0), (120.0, 130.0)]) == [(0.0, 100.0)]


def test_floating_point_near_adjacent_not_merged() -> None:
    # Tiny float gap should produce two cuts, not one — confirms we use strict
    # comparison, not approximate
    keeps = keep_ranges_from_deletes(100.0, [(10.0, 20.0), (20.0000001, 30.0)])
    assert keeps == [(0.0, 10.0), (20.0, 20.0000001), (30.0, 100.0)]


def test_multiple_deletes_covering_whole_duration() -> None:
    assert keep_ranges_from_deletes(100.0, [(0.0, 60.0), (60.0, 100.0)]) == []


def test_negative_duration_returns_empty() -> None:
    assert keep_ranges_from_deletes(-1.0, [(0.0, 1.0)]) == []


def test_session_roundtrip_via_dict() -> None:
    src = AudioRef(
        path="ep.m4a", duration_sec=100.0, sample_rate=48000, channels=2, codec="aac",
        sha256="abc123",
    )
    s = EditSession.new(source_audio=src, transcript_ref="ep.transcript.json")
    s.add_delete(10.0, 15.0, note="filler")
    s.add_delete(30.0, 32.5)

    blob = json.dumps(s.to_dict())
    loaded = EditSession.from_dict(json.loads(blob))

    assert loaded.schema_version == s.schema_version
    assert loaded.timeline_basis == "source_audio_seconds"
    assert loaded.source_audio.sha256 == "abc123"
    assert loaded.transcript_ref == "ep.transcript.json"
    assert len(loaded.ops) == 2
    assert (loaded.ops[0].start, loaded.ops[0].end) == (10.0, 15.0)
    assert loaded.ops[0].note == "filler"
    assert loaded.ops[1].note is None


def test_session_from_dict_rejects_wrong_schema_version() -> None:
    import pytest

    src = AudioRef(path="x.m4a", duration_sec=1.0, sample_rate=44100, channels=1, codec="aac")
    s = EditSession.new(source_audio=src)
    bad = s.to_dict()
    bad["schema_version"] = 999
    with pytest.raises(ValueError):
        EditSession.from_dict(bad)
