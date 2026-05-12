from podedit.edit import keep_ranges_from_deletes


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
