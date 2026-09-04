"""The vector math port (CONTRACT.md section 4.6, SPEC-VEC FR-7 and TR-1).

The pure Python implementation behind this port is the correctness oracle; an accelerated
adapter is only ever allowed to agree with it. Keeping the arithmetic behind a port is what
keeps numpy out of the domain entirely.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from typing import Protocol, runtime_checkable

__all__ = ["DistanceMetric", "PreparedVectorMath", "VectorMath"]


class DistanceMetric(str, Enum):
    """How two vectors of one embedding space are compared."""

    COSINE = "cosine"
    DOT = "dot"
    EUCLIDEAN = "euclidean"


@runtime_checkable
class VectorMath(Protocol):
    """Distance and ranking primitives, with a single score orientation for every metric."""

    @property
    def name(self) -> str:
        """Short, bounded label of this implementation, safe to use as a metric label value."""
        ...

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the dot product of two vectors of equal length."""
        ...

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the cosine similarity of two vectors of equal length."""
        ...

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the Euclidean distance between two vectors of equal length."""
        ...

    def norm(self, a: Sequence[float]) -> float:
        """Return the Euclidean length of one vector."""
        ...

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        """Return the vector scaled to unit length."""
        ...

    def score(self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric) -> float:
        """HIGHER IS BETTER for every metric: cosine/dot as-is, euclidean returns -distance."""
        ...

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        """Descending by score; ties broken by ascending candidate id for determinism."""
        ...


@runtime_checkable
class PreparedVectorMath(Protocol):
    """Optional capability of a ``VectorMath``: prepare one query for scoring many vectors.

    A traversal scores one query against hundreds of stored vectors, and part of every score
    depends on the query alone -- its norm under cosine, its converted array in an accelerator.
    ``prepare`` returns a callable that scores one stored vector against the query under one
    metric with that part computed once (VEC-4).

    The contract is exactness, not approximation. For every stored vector the callable answers
    what ``score(query, values, metric)`` answers, bit for bit, and refuses what it refuses with
    the same error in the same order -- including a query that cannot be measured, which is
    refused at the first call rather than at ``prepare``, so that preparing a query nothing is
    ever scored against refuses nothing. The graph asks for this capability with ``isinstance``
    and scores through ``score`` when an adapter does not declare it, so a host adapter that
    implements only ``VectorMath`` keeps working unchanged.
    """

    def prepare(
        self, query: Sequence[float], metric: DistanceMetric
    ) -> Callable[[Sequence[float]], float]:
        """Return a scorer of stored vectors against ``query`` under ``metric``."""
        ...
