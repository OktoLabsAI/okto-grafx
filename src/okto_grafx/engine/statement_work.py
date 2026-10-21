"""Revocable observation of physical statement work, without cooperative debits."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from okto_grafx.domain.ports.scoped_value import ScopedValue

__all__ = ["StatementWorkScope", "add_work"]


class _Observation:
    __slots__ = ("statistics", "active")

    def __init__(self, statistics: dict[str, int]) -> None:
        self.statistics = statistics
        self.active = True


class StatementWorkScope:
    """Transport only engine-owned counters; copied contexts cannot outlive a pull.

    Bind around execution or one cursor pull, never across a yield to its caller.
    The engine resolves the transport before entering a pinned/guarded section.
    Observers mutate local integers/dictionaries, never invoke a sink or control.
    """

    __slots__ = ("_transport",)

    def __init__(self, transport: ScopedValue) -> None:
        self._transport = transport

    @contextmanager
    def bind(self, statistics: dict[str, int]) -> Iterator[None]:
        """Restore the prior binding and revoke copied payloads on every exit."""
        observation = _Observation(statistics)
        try:
            with self._transport.bind(observation):
                yield
        finally:
            observation.active = False

    def current_statistics(self) -> dict[str, int] | None:
        """Return only a live native payload; absence never fabricates zero work."""
        value = self._transport.get()
        return value.statistics if type(value) is _Observation and value.active else None


def add_work(statistics: dict[str, int] | None, name: str, amount: int = 1) -> None:
    """Accumulate an observed count without introducing a control checkpoint."""
    if statistics is not None:
        statistics[name] = statistics.get(name, 0) + amount
