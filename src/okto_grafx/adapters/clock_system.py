"""The system clock adapter (CONTRACT.md section 4.2, SPEC-M1 TR-2 and FR-7).

Two readings, two contracts, and the separation is the whole point. ``monotonic()`` is a local
counter with an arbitrary origin: it never goes backwards, it is immune to an operator changing
the system time or to a daylight-saving step, and it is meaningless outside this process.
``wall()`` is Unix epoch seconds: comparable between machines in principle, and worthless as
evidence of liveness in practice, because nothing forces two processes to agree on it.

Every lease, heartbeat and stall decision in :mod:`okto_grafx.adapters.coordination_local` reads
``monotonic()``. ``wall()`` appears only in the human-facing stamp carried by the control files.
"""

from __future__ import annotations

import time

__all__ = ["SystemClock"]


class SystemClock:
    """The production Clock: a local monotonic source for liveness and a wall source for humans."""

    __slots__ = ()

    def monotonic(self) -> float:
        """Return the local monotonic reading in seconds.

        It is never comparable with the reading of another process.
        """
        return time.monotonic()

    def wall(self) -> float:
        """Return Unix epoch seconds.

        For human-facing stamps only, and never for a liveness decision.
        """
        return time.time()

    def __repr__(self) -> str:
        """Return the stable representation of this adapter."""
        return "SystemClock()"
