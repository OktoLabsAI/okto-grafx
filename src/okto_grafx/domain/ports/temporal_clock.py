"""Exact wall instants for query values, never leases, deadlines or OCC authority."""

from __future__ import annotations

from typing import Protocol

from okto_grafx.domain.model.temporal_values import TemporalInstant


class TemporalClock(Protocol):
    """Port for concurrent reads of exact POSIX wall-clock instants."""
    def now(self) -> TemporalInstant:
        """Read exact POSIX wall time, with no monotonic guarantees.

        Implementations shared by query participants must permit concurrent reads.
        Nanosecond units do not promise a physical clock resolution of one ns.
        """
        ...


__all__ = [
    'TemporalClock',
]
