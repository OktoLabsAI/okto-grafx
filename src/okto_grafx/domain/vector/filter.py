"""What a similarity search is allowed to return, and how big that set is expected to be.

A candidate filter is the seam between the query planner of C10 and the vector subsystem: the
graph traversal and the exact scan both evaluate it, and neither ever sees the predicate that
produced it. Two things are asked of it and nothing else -- whether one record is admitted, and
how many records it expects to admit -- because those are exactly the two questions the
two-regime planner and the ACORN traversal need answered (SPEC-VEC FR-5, BR-6).

``cardinality`` is an ESTIMATE and is documented as one. The planner treats None as "as large as
the space", so a filter that cannot count itself can never talk the planner into believing it is
selective. A filter that reports a number smaller than the set it actually admits changes which
regime runs and therefore the recall label; it never makes a returned row wrong, because
``admits`` is what decides membership and it is asked about every candidate.

``admits`` is host-supplied code. It is called with no lock of any kind held, because this
component holds no lock at all -- there is no shared mutable state behind a vector search, so
the boundary amendment A91 guards cannot exist here.

A filter that raises is never swallowed, because a predicate decides MEMBERSHIP: dropping its
failure would silently shrink the result, which is the wrong-result shape BR-2 exists to
prevent. Nor does a foreign exception leave the engine, which promises only ``Grafx*`` types
(CONTRACT.md section 11 item 5). The engine translates it into ``GrafxIndexError`` with the
original attached, so the caller learns that its own predicate failed. That behaviour is pinned
by ``test_a_candidate_filter_that_fails_is_reported_in_the_taxonomy``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import RecordId

__all__ = [
    "CandidateFilter",
    "RecordIdFilter",
    "admits_everything",
]


@runtime_checkable
class CandidateFilter(Protocol):
    """The predicate a similarity search evaluates during navigation."""

    @property
    def cardinality(self) -> int | None:
        """Return how many records this filter expects to admit, or None when unknown."""
        ...

    def admits(self, record_id: RecordId) -> bool:
        """Return True when this record may appear in the result."""
        ...


@dataclass(frozen=True, slots=True)
class RecordIdFilter:
    """The filter of an enumerated set of records, which knows its cardinality exactly.

    This is the shape a graph pattern produces: the rows that survived the node predicates, the
    relationship predicates and the traversal, handed to the vector search as one set so the
    search is a single operator rather than a fetch followed by a post-filter (BR-6).
    """

    record_ids: frozenset[int]

    def __post_init__(self) -> None:
        """Refuse anything that is not a set of record identifiers."""
        if not isinstance(self.record_ids, frozenset):
            raise GrafxConfigurationError(
                f"A record filter holds a frozenset of record identifiers; got "
                f"{type(self.record_ids).__name__}.",
                field="record_ids",
                value=type(self.record_ids).__name__,
            )
        for identifier in self.record_ids:
            if isinstance(identifier, bool) or not isinstance(identifier, int):
                raise GrafxConfigurationError(
                    f"A record filter holds integer record identifiers; got "
                    f"{type(identifier).__name__}.",
                    field="record_ids",
                    value=type(identifier).__name__,
                )

    @classmethod
    def of(cls, record_ids: Iterable[int]) -> RecordIdFilter:
        """Build a filter from any iterable of record identifiers."""
        return cls(frozenset(record_ids))

    @property
    def cardinality(self) -> int:
        """Return the exact number of records this filter admits."""
        return len(self.record_ids)

    def admits(self, record_id: RecordId) -> bool:
        """Return True when this record is one of the enumerated ones."""
        return record_id in self.record_ids


def admits_everything(record_id: RecordId) -> bool:
    """Return True for every record, which is the predicate of a search with no filter."""
    return True
