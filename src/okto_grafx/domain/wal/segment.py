"""Segment naming and the horizon rule that decides which of them may go (BR-10, FR-6).

A segment is one append-only file of the log, named ``<directory>/<twelve digits>.wal``. The
number only ever increases, and it is never reused: a name that has been recycled may still be
held open by another process on Windows, and re-claiming it would mean writing into a file whose
destruction is queued.

:func:`recyclable_prefix` is the whole of BR-10 in one function, and it is written as a PREFIX on
purpose. Segments carry increasing log sequence numbers, so the recyclable ones are always the
oldest ones; stopping at the first segment that must stay is what keeps the surviving log a
contiguous run of records. A hole in the middle would read, on the next open, as a log that lost
records -- which is the failure this component exists to make impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import NO_LSN, Lsn

__all__ = [
    "SEGMENT_SUFFIX",
    "SEGMENT_NUMBER_DIGITS",
    "MIN_SEGMENT_NUMBER",
    "MAX_SEGMENT_NUMBER",
    "SegmentInfo",
    "segment_name",
    "parse_segment_number",
    "recyclable_prefix",
]

SEGMENT_SUFFIX: str = ".wal"
"""Every segment file ends with this, and nothing else in the directory does."""

SEGMENT_NUMBER_DIGITS: int = 12
"""Digits in a segment name, zero padded, so a plain sort is a numeric sort."""

MIN_SEGMENT_NUMBER: int = 1
"""Segments are numbered from one; zero is reserved for no segment."""

MAX_SEGMENT_NUMBER: int = 10**SEGMENT_NUMBER_DIGITS - 1
"""The largest number that still fits the padded name."""


def segment_name(directory: str, number: int) -> str:
    """Return the logical file name of one segment, refusing a number that does not fit.

    The directory is joined with a forward slash on every platform, because that is what the
    storage port declares a logical name to be.
    """
    if not isinstance(directory, str) or not directory:
        raise GrafxConfigurationError(
            "A segment directory must be a non-empty string.",
            field="directory",
            value=repr(directory),
        )
    if isinstance(number, bool) or not isinstance(number, int):
        raise GrafxConfigurationError(
            f"A segment number must be an integer; got {type(number).__name__}.",
            field="number",
            value=repr(number),
        )
    if not MIN_SEGMENT_NUMBER <= number <= MAX_SEGMENT_NUMBER:
        raise GrafxConfigurationError(
            f"A segment number runs from {MIN_SEGMENT_NUMBER} to {MAX_SEGMENT_NUMBER}; "
            f"got {number}.",
            field="number",
            value=number,
        )
    return f"{directory}/{number:0{SEGMENT_NUMBER_DIGITS}d}{SEGMENT_SUFFIX}"


def parse_segment_number(directory: str, name: str) -> int | None:
    """Return the number of a segment file, or None when the name is not a segment at all.

    Anything the log did not write is answered with None rather than an error: a directory may
    hold a quarantine copy, an operator's note or a file another tool left behind, and none of
    those is a reason to refuse to open the log.
    """
    if not isinstance(name, str) or not isinstance(directory, str) or not directory:
        return None
    prefix = f"{directory}/"
    if not name.startswith(prefix) or not name.endswith(SEGMENT_SUFFIX):
        return None
    stem = name[len(prefix) : len(name) - len(SEGMENT_SUFFIX)]
    if len(stem) != SEGMENT_NUMBER_DIGITS or not stem.isdigit() or not stem.isascii():
        return None
    number = int(stem)
    if not MIN_SEGMENT_NUMBER <= number <= MAX_SEGMENT_NUMBER:
        return None
    return number


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """What the log knows about one segment file on disk.

    ``first_lsn`` and ``last_lsn`` cover the records the log could actually read. A segment whose
    bytes are damaged from its first record onwards reports :data:`NO_LSN` for both and a record
    count of zero, which is exactly the state that must never be treated as recyclable: an empty
    range says nothing about what the file holds.
    """

    number: int
    name: str
    first_lsn: Lsn
    last_lsn: Lsn
    size_bytes: int
    record_count: int

    @property
    def is_readable(self) -> bool:
        """Return True when at least one record of this segment decoded."""
        return self.record_count > 0 and self.last_lsn != NO_LSN


def recyclable_prefix(segments: Sequence[SegmentInfo], horizon_lsn: Lsn) -> int:
    """Return how many leading segments may be recycled under this horizon (BR-10).

    A segment qualifies only when every record it holds is below the horizon, which is
    ``segment.last_lsn < horizon_lsn``. Three refusals matter more than the rule itself:

    * the newest segment is never offered, because it is where the next record goes;
    * a segment whose records could not be read is never offered, because an unknown range is
      not a range below the horizon;
    * the walk stops at the first segment that must stay, so the surviving log is always a
      contiguous run and never a prefix with a hole in it.

    The horizon comes from :func:`okto_grafx.engine.coordination.recyclable_horizon`, which is
    where the reader registry and the checkpoint are combined. Nothing here looks at whether a
    reader exists: BR-10 is explicit that recycling follows the horizon and never the absence of
    readers.
    """
    if isinstance(horizon_lsn, bool) or not isinstance(horizon_lsn, int) or horizon_lsn < 0:
        raise GrafxConfigurationError(
            f"A recycling horizon must be a log sequence number of zero or more; "
            f"got {horizon_lsn!r}.",
            field="horizon_lsn",
            value=repr(horizon_lsn),
        )
    count = 0
    for segment in segments[:-1]:
        if not segment.is_readable:
            break
        if segment.last_lsn >= horizon_lsn:
            break
        count += 1
    return count
