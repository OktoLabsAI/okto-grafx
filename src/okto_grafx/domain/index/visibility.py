"""The dual visibility rule of secondary indexes (CONTRACT.md section 8.7, SPEC-M1 SD-3, BR-11).

An index is derived state, and the two kinds of index derive differently. The rule a caller may
rely on, stated once, here:

**EXACT** -- ``lookup`` returns a SUPERSET of the heap locations whose row currently carries the
key and is visible to the snapshot. It may return a location whose row was deleted, superseded,
or committed after the snapshot opened, and the caller MUST validate every hit against the heap
under its own snapshot. It never OMITS a location whose row is visible under the snapshot and
carries the key, provided the index is fresh (see :mod:`okto_grafx.engine.index_manager`). The
index is allowed to be stale only in the optimistic direction; the heap is the truth.

**PROXIMITY** -- ``lookup`` returns EXACTLY the heap locations the snapshot may see, decided from
the entry alone. Validating every candidate against the heap would defeat the purpose of a
proximity structure, so visibility rests on the entry's own birth stamp and on a tombstone, and
a tombstoned entry is physically removed only once the snapshot horizon has passed it.

The two predicates below are the whole of it, and they are deliberately hostile to being mixed:
:func:`entry_visible` REFUSES an unversioned entry rather than guessing, so an exact index cannot
be read under the proximity rule even by accident. There is no matching guard in the other
direction and there does not need to be one: reading a proximity index under the exact rule means
validating its hits against the heap, which is slow and correct rather than fast and wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import NO_CSN, Csn, Lsn
from okto_grafx.domain.index.entry import IndexEntry

__all__ = [
    "IndexVisibility",
    "ReconcileReport",
    "SnapshotLike",
    "entry_visible",
    "is_reclaimable",
]


class IndexVisibility(str, Enum):
    """The visibility class a secondary index declares (CONTRACT.md section 8.7)."""

    EXACT = "exact"
    PROXIMITY = "proximity"

    @classmethod
    def parse(cls, value: object) -> IndexVisibility:
        """Return the visibility class for this value, refusing anything that is not one."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for candidate in cls:
                if candidate.value == value:
                    return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in cls)
        raise GrafxIndexError(
            f"An index visibility must be one of {allowed}; got {value!r}.",
            field="visibility",
            value=repr(value),
        )


@runtime_checkable
class SnapshotLike(Protocol):
    """Anything that can answer the visibility predicate of CONTRACT.md section 8.5.

    Taken structurally for the reason amendment A19 gives: the predicate belongs to the
    transaction manager, and asking for it by shape rather than by class keeps the index from
    holding a visibility rule of its own.
    """

    def visible(self, xmin: int, xmax: int) -> bool:
        """Return True when a version created at xmin and ended at xmax belongs to this view."""
        ...


def entry_visible(entry: IndexEntry, snapshot: SnapshotLike) -> bool:
    """Return True when a snapshot may see the row this VERSIONED entry points at.

    Refusing an unversioned entry is the load-bearing half. An exact entry carries no birth
    stamp, so the only answers available here would be "always visible" or "never visible", and
    both are wrong: the first returns deleted rows, the second omits live ones. The honest answer
    is that the question does not apply, and saying so with a typed error is what stops an exact
    index from being consumed under the proximity contract.
    """
    if not isinstance(entry, IndexEntry):
        raise GrafxIndexError(
            f"A visibility decision needs an IndexEntry; got {type(entry).__name__}.",
            field="entry",
            value=type(entry).__name__,
        )
    if not entry.versioned:
        raise GrafxIndexError(
            "An unversioned index entry carries no birth stamp, so no snapshot can decide it; "
            "an exact hit is a candidate and must be validated against the heap.",
            field="versioned",
            value=False,
            visibility=IndexVisibility.EXACT.value,
        )
    if not isinstance(snapshot, SnapshotLike):
        raise GrafxIndexError(
            f"A visibility decision needs a snapshot; got {type(snapshot).__name__}.",
            field="snapshot",
            value=type(snapshot).__name__,
        )
    return bool(snapshot.visible(entry.born_csn, entry.dead_csn))


def is_reclaimable(entry: IndexEntry, horizon: Lsn) -> bool:
    """Return True when no live snapshot could still want this entry.

    The rule, and why the comparison is inclusive. An entry ended at commit number ``C`` is
    invisible to a snapshot at ``read_lsn`` exactly when ``C <= read_lsn``. The horizon is the
    lowest ``read_lsn`` any live snapshot holds, so every live snapshot satisfies
    ``read_lsn >= horizon``; if ``C <= horizon`` then ``C <= read_lsn`` holds for all of them and
    not one of them can see the entry. An entry that was never ended is never reclaimable, at any
    horizon, because a row that still carries the key is a row a future snapshot will ask for.

    The caller owns the honesty of the horizon it passes. CONTRACT.md section 8.3 already fixes
    what that number is for segment recycling -- ``min(reader_horizon or checkpoint_lsn,
    checkpoint_lsn)`` -- and SPEC-VEC TR-3 says tombstone reconciliation reuses that same
    mechanism rather than inventing a second lifetime.
    """
    if not isinstance(entry, IndexEntry):
        raise GrafxIndexError(
            f"A reclamation decision needs an IndexEntry; got {type(entry).__name__}.",
            field="entry",
            value=type(entry).__name__,
        )
    horizon_value = _require_horizon(horizon)
    return not entry.live and entry.dead_csn <= horizon_value


def _require_horizon(horizon: object) -> Csn:
    """Return the horizon as a commit number, refusing anything that is not one."""
    if isinstance(horizon, bool) or not isinstance(horizon, int):
        raise GrafxIndexError(
            f"A snapshot horizon must be an integer; got {type(horizon).__name__}.",
            field="horizon",
            value=repr(horizon),
        )
    if horizon < NO_CSN:
        raise GrafxIndexError(
            f"A snapshot horizon must not be negative; got {horizon}.",
            field="horizon",
            value=horizon,
        )
    return horizon


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconciliation pass over one index did (CONTRACT.md section 8.7).

    ``retained`` counts the tombstoned entries the horizon did not release yet, which is the
    backlog SPEC-VEC OR-2 asks to be observable; ``scanned`` counts every entry the pass looked
    at, so a report of zero removals says whether the pass ran or found nothing to do.

    ``reclaimable`` and ``removed`` are deliberately two numbers. A pass given no transaction
    MEASURES -- it may not remove anything, because every removal has to be a log record and a
    cleanup the log never saw is exactly what SPEC-VEC BR-3 forbids -- so it reports what the
    horizon has released without touching it. A pass given a transaction removes them, and the
    two numbers are then equal. One number would have made "nothing to do" and "not allowed to do
    it" the same reading.
    """

    index: str
    horizon: Lsn
    scanned: int
    reclaimable: int
    removed: int
    retained: int
    pages_touched: int

    @property
    def backlog(self) -> int:
        """Return the tombstoned entries still held back by the horizon."""
        return self.retained
