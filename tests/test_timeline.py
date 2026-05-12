import pytest

from podedit.edit import DeleteOp, MoveOp, TimelineSegment, compile_timeline, keep_ranges_from_deletes


def d(start: float, end: float, op_id: str = "d") -> DeleteOp:
    return DeleteOp(op_id=op_id, op="delete", start=start, end=end)


def m(start: float, end: float, target: float, op_id: str = "m") -> MoveOp:
    return MoveOp(op_id=op_id, op="move", src_start=start, src_end=end, target_edited_t=target)


def ranges(segs: list[TimelineSegment]) -> list[tuple[float, float]]:
    return [(s.source_start, s.source_end) for s in segs]


def edited(segs: list[TimelineSegment]) -> list[tuple[float, float]]:
    return [(s.edited_start, s.edited_end) for s in segs]


def test_empty_source_duration_returns_no_segments() -> None:
    assert compile_timeline(0.0, []) == []


def test_no_ops_single_full_segment() -> None:
    segs = compile_timeline(10.0, [])
    assert ranges(segs) == [(0.0, 10.0)]
    assert edited(segs) == [(0.0, 10.0)]


def test_delete_only_matches_keep_ranges_single_delete() -> None:
    ops = [d(3.0, 4.0)]
    assert ranges(compile_timeline(10.0, ops)) == keep_ranges_from_deletes(10.0, [(3.0, 4.0)])


def test_delete_only_matches_keep_ranges_overlapping_deletes() -> None:
    ops = [d(2.0, 5.0, "d1"), d(4.0, 7.0, "d2")]
    assert ranges(compile_timeline(10.0, ops)) == keep_ranges_from_deletes(10.0, [(2.0, 5.0), (4.0, 7.0)])


def test_single_move_middle_to_start() -> None:
    segs = compile_timeline(10.0, [m(3.0, 5.0, 0.0, "move1")])
    assert ranges(segs) == [(3.0, 5.0), (0.0, 3.0), (5.0, 10.0)]
    assert segs[0].origin_op_id == "move1"
    assert edited(segs) == [(0.0, 2.0), (2.0, 5.0), (5.0, 10.0)]


def test_single_move_middle_to_end() -> None:
    segs = compile_timeline(10.0, [m(3.0, 5.0, 10.0, "move1")])
    assert ranges(segs) == [(0.0, 3.0), (5.0, 10.0), (3.0, 5.0)]
    assert edited(segs) == [(0.0, 3.0), (3.0, 8.0), (8.0, 10.0)]


def test_move_target_inside_segment_splits_at_translated_pre_op_time() -> None:
    segs = compile_timeline(10.0, [m(2.0, 4.0, 8.0, "move1")])
    assert ranges(segs) == [(0.0, 2.0), (4.0, 8.0), (2.0, 4.0), (8.0, 10.0)]
    assert edited(segs) == [(0.0, 2.0), (2.0, 6.0), (6.0, 8.0), (8.0, 10.0)]


def test_move_target_beyond_edited_length_clamps_to_end() -> None:
    segs = compile_timeline(10.0, [m(1.0, 2.0, 99.0, "move1")])
    assert ranges(segs) == [(0.0, 1.0), (2.0, 10.0), (1.0, 2.0)]


def test_move_target_inside_source_range_is_no_op() -> None:
    segs = compile_timeline(10.0, [m(2.0, 5.0, 3.0, "move1")])
    assert ranges(segs) == [(0.0, 10.0)]
    assert segs[0].origin_op_id is None


def test_delete_then_move_overlapping_deleted_range_moves_only_remaining_piece() -> None:
    segs = compile_timeline(10.0, [d(3.0, 5.0, "del1"), m(2.0, 6.0, 0.0, "move1")])
    assert ranges(segs) == [(2.0, 3.0), (5.0, 6.0), (0.0, 2.0), (6.0, 10.0)]
    assert [s.origin_op_id for s in segs[:2]] == ["move1", "move1"]


def test_move_then_delete_can_delete_from_moved_piece() -> None:
    segs = compile_timeline(10.0, [m(2.0, 5.0, 0.0, "move1"), d(3.0, 4.0, "del1")])
    assert ranges(segs) == [(2.0, 3.0), (4.0, 5.0), (0.0, 2.0), (5.0, 10.0)]


def test_move_deleted_entire_remaining_range_removes_source_everywhere() -> None:
    segs = compile_timeline(6.0, [m(2.0, 4.0, 0.0, "move1"), d(2.0, 4.0, "del1")])
    assert ranges(segs) == [(0.0, 2.0), (4.0, 6.0)]


def test_edited_segments_tile_without_gaps_after_mixed_ops() -> None:
    segs = compile_timeline(12.0, [d(1.0, 2.0, "d1"), m(5.0, 7.0, 3.0, "m1"), d(9.0, 10.0, "d2")])
    cursor = 0.0
    for seg in segs:
        assert seg.edited_start == pytest.approx(cursor)
        cursor = seg.edited_end
    assert cursor == pytest.approx(sum(e - s for s, e in ranges(segs)))
