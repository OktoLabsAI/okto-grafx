"""Nanosecond wall-clock adapter, separate from the engine's liveness Clock."""

from __future__ import annotations

from time import time_ns

from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.model.temporal_values import TemporalInstant, _integer, _MIN_I64, _MAX_I64


class SystemTemporalClock:
    """Wall-clock adapter returning validated POSIX seconds and nanoseconds."""
    __slots__ = ()

    def now(self) -> TemporalInstant:
        """Read system wall time and split it into an exact validated temporal instant."""
        try:
            raw = time_ns()
        except OSError as error:
            raise GrafxUnsupportedOperation("The system temporal clock could not be read.",
                                            field="temporal_clock") from error
        _integer(raw, "epoch_nanoseconds", _MIN_I64 * 1_000_000_000, (_MAX_I64 + 1) * 1_000_000_000 - 1)
        seconds, nanos = divmod(raw, 1_000_000_000)
        return TemporalInstant(seconds, nanos)


__all__ = [
    'SystemTemporalClock',
]
