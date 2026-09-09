"""Per-operation logical allocation envelope, never a process RSS measurement."""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxQueryBudgetExceeded

__all__: list[str] = []


class SearchMemory:
    """Simultaneously retained phase tariffs and deterministic peak diagnostics."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.current = {}
        self.peaks = {}
        self.peak = 0

    def set(self, phase: str, amount: int) -> None:
        """Record simultaneous logical retention and refuse an over-budget peak."""
        self.current[phase] = amount
        self.peaks[phase] = max(amount, self.peaks.get(phase, 0))
        self.peak = max(self.peak, sum(self.current.values()))
        if self.peak > self.limit:
            raise GrafxQueryBudgetExceeded(
                "Hybrid aggregate logical-memory budget exceeded.", resource="hybrid_memory",
            )
