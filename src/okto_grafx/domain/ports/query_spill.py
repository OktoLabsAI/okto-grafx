"""Opaque, ordered temporary-record spill for bounded query execution.

The engine owns record meaning and hands this port only versioned bytes plus a total comparator.
The adapter owns temporary directories, files and external merging.  Neither side needs to know
the other's mechanism, and no durable database namespace is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol, runtime_checkable

from okto_grafx.domain.query.memory import LogicalMemoryBudget

__all__ = [
    "SpillComparator",
    "QuerySpillFactory",
    "QuerySpillSorter",
    "QuerySpillWorkspace",
]

SpillComparator = Callable[[bytes, bytes], int]
"""A total comparison over versioned opaque keys: negative, zero or positive."""


@runtime_checkable
class QuerySpillSorter(Protocol):
    """One append-then-read ordered stream inside a temporary workspace."""

    def append(self, key: bytes, payload: bytes) -> None:
        """Append one immutable record before iteration starts."""
        ...

    def records(self) -> Iterator[tuple[bytes, bytes]]:
        """Yield every appended record in total key order exactly once."""
        ...

    def close(self) -> None:
        """Release every run owned by this sorter; idempotent."""
        ...


@runtime_checkable
class QuerySpillWorkspace(Protocol):
    """A query-operator workspace sharing one logical byte budget across all sorters."""

    def sorter(self, comparator: SpillComparator) -> QuerySpillSorter:
        """Create an empty sorter using ``comparator`` as its total order."""
        ...

    def reserve(self, amount: int, *, reason: str) -> None:
        """Reserve core-owned state after flushing adapter buffers that can make room."""
        ...

    def release(self, amount: int) -> None:
        """Release a prior core-owned logical reservation."""
        ...

    def close(self) -> None:
        """Close every sorter and remove all temporary artifacts; idempotent."""
        ...


@runtime_checkable
class QuerySpillFactory(Protocol):
    """Open isolated temporary workspaces without exposing host paths to the engine."""

    def open(self, budget: LogicalMemoryBudget) -> QuerySpillWorkspace:
        """Open a workspace charged against the supplied execution-local counter."""
        ...
