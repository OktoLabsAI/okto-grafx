"""Deterministic logical-memory admission for blocking query operators.

This counter deliberately measures the versioned byte representations retained by an
operator, plus documented fixed slots.  It is not an RSS estimator: Python object headers,
allocator arenas and temporary interpreter work are outside its claim.  The distinction makes
the same configured limit admit or refuse the same logical rows on every supported platform.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxQueryBudgetExceeded

__all__ = ["LogicalMemoryBudget"]


class LogicalMemoryBudget:
    """One execution-local byte counter shared by a blocking operator and its spill adapter."""

    __slots__ = ("_limit", "_operator", "_retained", "_peak")

    def __init__(self, limit: int, *, operator: str) -> None:
        if type(limit) is not int or limit <= 0:
            raise ValueError(
                "A logical memory budget must be a positive exact integer."
            )
        if type(operator) is not str or not operator:
            raise ValueError("A logical memory budget must name its operator.")
        self._limit = limit
        self._operator = operator
        self._retained = 0
        self._peak = 0

    @property
    def limit(self) -> int:
        """Configured logical byte ceiling."""
        return self._limit

    @property
    def retained(self) -> int:
        """Logical bytes currently owned by the operator and adapter."""
        return self._retained

    @property
    def available(self) -> int:
        """Logical bytes available before the next admission would be refused."""
        return self._limit - self._retained

    @property
    def peak(self) -> int:
        """Largest admitted logical byte count reached by this execution."""
        return self._peak

    def try_reserve(self, amount: int) -> bool:
        """Reserve ``amount`` when it fits, returning whether admission succeeded."""
        if type(amount) is not int or amount < 0:
            raise ValueError(
                "A logical memory reservation must be a non-negative exact integer."
            )
        observed = self._retained + amount
        if observed > self._limit:
            return False
        self._retained = observed
        if observed > self._peak:
            self._peak = observed
        return True

    def reserve(self, amount: int, *, reason: str = "retained_record") -> None:
        """Reserve ``amount`` or raise the public, fail-closed budget refusal."""
        if self.try_reserve(amount):
            return
        observed = self._retained + amount
        raise GrafxQueryBudgetExceeded(
            f"Operator {self._operator} would exceed query_memory_budget_bytes: "
            f"limit {self._limit}, observed {observed}.",
            field="query_memory_budget_bytes",
            limit=self._limit,
            observed=observed,
            retained=self._retained,
            requested=amount,
            operator=self._operator,
            reason=reason,
        )

    def release(self, amount: int) -> None:
        """Release a prior reservation, refusing counter corruption immediately."""
        if type(amount) is not int or amount < 0 or amount > self._retained:
            raise RuntimeError(
                "Logical query-memory accounting was released out of balance."
            )
        self._retained -= amount
