"""Deterministic logical-memory admission for blocking query operators.

This counter deliberately measures the versioned byte representations retained by an
operator, plus documented fixed slots.  It is not an RSS estimator: Python object headers,
allocator arenas and temporary interpreter work are outside its claim.  The distinction makes
the same configured limit admit or refuse the same logical rows on every supported platform.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxQueryBudgetExceeded

__all__ = ["LogicalMemoryBudget"]


class _LogicalMemoryObservation:
    """Peak of simultaneous admitted tariffs; no allocator or RSS claim.

    Internal opt-in observations are single-execution intervals by default; the
    explicit sequence mode keeps the same live ledger across owned statements.
    Closing revokes
    updates from budgets that outlive the interval. Tokens identify budgets, not
    operators: a spill workspace sharing a budget must not count it twice.
    """

    __slots__ = ("_values", "current", "_peak", "_baseline", "_active", "_reason", "_statement", "_cursors", "_sequence")

    def __init__(self, *, sequence: bool = False) -> None:
        self._values: dict[object, int] = {}
        self.current = 0
        self._peak = 0
        self._baseline = 0
        self._active = True
        self._reason: str | None = None
        self._statement: object | None = None
        self._cursors = 0
        self._sequence = sequence

    def update(self, token: object, retained: int) -> None:
        """Record the current retained tariff for one live budget token."""
        if not self._active:
            return
        if retained < 0:
            self.invalidate("negative_retained_bytes")
            return
        self.current += retained - self._values.get(token, 0)
        if retained:
            self._values[token] = retained
        else:
            self._values.pop(token, None)
        self._peak = max(self._peak, self.current)

    def freeze_baseline(self) -> None:
        """Remember retention before the measured sequence begins."""
        self._baseline = self.current

    def invalidate(self, reason: str) -> None:
        """Keep the first reason an observation cannot certify a peak."""
        if self._active and self._reason is None:
            self._reason = reason

    def claim(self, statement: object) -> None:
        """Bind work to one statement unless sequence mode was requested."""
        if self._statement is None:
            self._statement = statement
        elif self._statement is not statement and not self._sequence:
            self.invalidate("multiple_statements_in_interval")

    def close(self) -> None:
        """Revoke the observer after checking statement and cursor ownership."""
        if self._statement is None:
            self.invalidate("no_statement_observed")
        if self._cursors:
            self.invalidate("cursor_outlives_observation")
        self._active = False
        self._values.clear()
        self._statement = None

    def result(self) -> dict[str, object]:
        """Return the observed peak or an explicit coverage failure reason."""
        return {
            "logical_memory_peak_bytes": self._peak if self._reason is None else None,
            "baseline_retained_bytes": self._baseline,
            "coverage": ("simultaneous_sequence_query_budget_and_handle_owner_endpoint_tariffs_v1"
                         if self._sequence else "simultaneous_query_budget_and_handle_owner_endpoint_tariffs_v1"),
            "unavailable_reason": self._reason,
        }


class LogicalMemoryBudget:
    """One execution-local byte counter shared by a blocking operator and its spill adapter."""

    __slots__ = ("_limit", "_operator", "_retained", "_peak", "_observation", "_observation_token")

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
        self._observation: _LogicalMemoryObservation | None = None
        self._observation_token: object | None = None

    def _observe(self, observation: _LogicalMemoryObservation) -> None:
        """Attach an internal observer without changing admission or public type."""
        self._observation = observation
        if self._observation_token is None:
            self._observation_token = object()
        observation.update(self._observation_token, self._retained)

    def _end_observation(self) -> None:
        """End this tariff's observed lifetime without changing quota accounting."""
        if self._observation is not None:
            self._observation.update(self._observation_token, 0)
            self._observation = None

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
        if self._observation is not None:
            self._observation.update(self._observation_token, observed)
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
        if self._observation is not None:
            self._observation.update(self._observation_token, self._retained)
