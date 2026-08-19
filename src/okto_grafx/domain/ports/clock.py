"""The clock port (CONTRACT.md section 4.2, SPEC-M1 TR-2).

Two clocks, two contracts. Liveness, leases and stall detection read the monotonic source and
nothing else; a wall clock reading is never compared between processes, because nothing
guarantees that two processes agree on it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Clock"]


@runtime_checkable
class Clock(Protocol):
    """Time as two distinct contracts: a local monotonic source and a human-facing wall source."""

    def monotonic(self) -> float:
        """Local, never comparable across processes. The ONLY source for liveness and lease timing."""
        ...

    def wall(self) -> float:
        """Unix epoch seconds. For human-facing timestamps ONLY. Never for liveness."""
        ...
