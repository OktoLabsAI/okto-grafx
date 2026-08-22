"""Segment names and the horizon rule (SPEC-M1 FR-6, BR-10).

BR-10 is one sentence -- a segment is recyclable when its last sequence number is below the
smallest active snapshot, never because no reader is present and never by evicting one -- and
:func:`recyclable_prefix` is that sentence as code. It is tested here on its own, without a
device, because a rule this consequential should be provable without a file system.

The three refusals are the load-bearing part: the newest segment is never offered, an unreadable
segment stops the walk, and the walk stops at the first segment that must stay so the surviving
log can never have a hole in the middle of it.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import NO_LSN
from okto_grafx.domain.wal.segment import (
    MAX_SEGMENT_NUMBER,
    MIN_SEGMENT_NUMBER,
    SEGMENT_NUMBER_DIGITS,
    SEGMENT_SUFFIX,
    SegmentInfo,
    parse_segment_number,
    recyclable_prefix,
    segment_name,
)


def _segment(number: int, first: int, last: int, *, records: int = 1) -> SegmentInfo:
    """Return one segment description with the sequence range a test cares about."""
    return SegmentInfo(
        number=number,
        name=segment_name("wal", number),
        first_lsn=first,
        last_lsn=last,
        size_bytes=1024,
        record_count=records,
    )


# --- names ---------------------------------------------------------------------------------


def test_a_segment_name_is_the_one_the_contract_shows() -> None:
    """CONTRACT.md section 6.1 writes wal/<000000000001>.wal, and a plain sort is numeric."""
    assert SEGMENT_NUMBER_DIGITS == 12
    assert SEGMENT_SUFFIX == ".wal"
    assert segment_name("wal", 1) == "wal/000000000001.wal"
    assert segment_name("wal", 987654321) == "wal/000987654321.wal"
    assert sorted([segment_name("wal", 10), segment_name("wal", 9)]) == [
        segment_name("wal", 9),
        segment_name("wal", 10),
    ]


def test_a_name_round_trips_through_its_number() -> None:
    """Discovery reads names off the device, so parsing has to invert building exactly."""
    for number in (MIN_SEGMENT_NUMBER, 2, 999, MAX_SEGMENT_NUMBER):
        assert parse_segment_number("wal", segment_name("wal", number)) == number


@pytest.mark.parametrize(
    "name",
    [
        "wal/1.wal",
        "wal/000000000001.log",
        "wal/000000000001.wal.bak",
        "wal/00000000000a.wal",
        "wal/000000000000.wal",
        "other/000000000001.wal",
        "wal/sub/000000000001.wal",
        "000000000001.wal",
        "",
    ],
)
def test_a_name_that_is_not_a_segment_is_answered_with_none(name: str) -> None:
    """A quarantine copy or an operator's note in the directory must not stop an open."""
    assert parse_segment_number("wal", name) is None


@pytest.mark.parametrize("number", [0, -1, MAX_SEGMENT_NUMBER + 1, True, "3"])
def test_building_a_name_refuses_a_number_that_does_not_fit(number: object) -> None:
    """A number outside the padded field would produce a name of the wrong length."""
    with pytest.raises(GrafxConfigurationError) as caught:
        segment_name("wal", number)  # type: ignore[arg-type]
    assert caught.value.details["field"] == "number"


def test_building_a_name_refuses_an_empty_directory() -> None:
    """The directory is part of the logical name the storage port receives."""
    with pytest.raises(GrafxConfigurationError) as caught:
        segment_name("", 1)
    assert caught.value.details["field"] == "directory"


# --- the horizon rule (BR-10) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("horizon", "expected"),
    [(0, 0), (1, 0), (5, 0), (6, 1), (10, 1), (11, 2), (15, 2), (16, 2), (1_000, 2)],
)
def test_the_recyclable_prefix_follows_the_horizon(horizon: int, expected: int) -> None:
    """A segment goes only when every record it holds is strictly below the horizon."""
    segments = [_segment(1, 1, 5), _segment(2, 6, 10), _segment(3, 11, 15)]
    assert recyclable_prefix(segments, horizon) == expected


def test_the_newest_segment_is_never_offered() -> None:
    """It is where the next record goes, so recycling it would delete the log in use."""
    assert recyclable_prefix([_segment(1, 1, 5)], 1_000) == 0
    assert recyclable_prefix([], 1_000) == 0


def test_a_reader_holding_an_old_snapshot_keeps_exactly_its_segments(
) -> None:
    """AC-8: the segments below the reader go, the ones it needs stay, and it is never evicted."""
    segments = [_segment(number, number * 10 - 9, number * 10) for number in range(1, 6)]
    reader_horizon = 25
    count = recyclable_prefix(segments, reader_horizon)
    assert count == 2
    kept = segments[count:]
    assert kept[0].first_lsn <= reader_horizon <= kept[-1].last_lsn
    assert all(segment.last_lsn >= reader_horizon for segment in kept)


def test_a_segment_whose_records_could_not_be_read_stops_the_walk() -> None:
    """An unknown range is not a range below the horizon, so it is never reclaimed."""
    segments = [
        _segment(1, 1, 5),
        SegmentInfo(2, segment_name("wal", 2), NO_LSN, NO_LSN, 900, 0),
        _segment(3, 11, 15),
        _segment(4, 16, 20),
    ]
    assert recyclable_prefix(segments, 1_000) == 1


def test_the_walk_stops_rather_than_leaving_a_hole() -> None:
    """A gap in the middle would read, on the next open, as records lost from the log."""
    segments = [_segment(1, 1, 5), _segment(2, 6, 10), _segment(3, 11, 15), _segment(4, 16, 20)]
    assert recyclable_prefix(segments, 8) == 1


def test_an_empty_horizon_recycles_nothing() -> None:
    """Horizon zero means nothing has been published yet, which is not a licence to reclaim."""
    segments = [_segment(1, 1, 5), _segment(2, 6, 10)]
    assert recyclable_prefix(segments, 0) == 0


@pytest.mark.parametrize("horizon", [-1, True, "5", 1.5, None])
def test_the_horizon_must_be_a_sequence_number(horizon: object) -> None:
    """A bool would mean one, and one is a horizon that reclaims almost nothing silently."""
    with pytest.raises(GrafxConfigurationError) as caught:
        recyclable_prefix([_segment(1, 1, 5), _segment(2, 6, 10)], horizon)  # type: ignore[arg-type]
    assert caught.value.details["field"] == "horizon_lsn"


def test_a_readable_segment_is_the_one_with_records() -> None:
    """The predicate the walk stops on is asserted directly, so it cannot drift silently."""
    assert _segment(1, 1, 5).is_readable is True
    assert SegmentInfo(2, "wal/000000000002.wal", NO_LSN, NO_LSN, 0, 0).is_readable is False
    assert SegmentInfo(3, "wal/000000000003.wal", 1, 4, 100, 0).is_readable is False
