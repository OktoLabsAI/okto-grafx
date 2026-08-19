"""The no-op metrics adapter: the destination that costs nothing (SPEC-M1 FR-14, OR-6, BR-12).

An embedded database is measured by what it does when nobody is watching. The default install of
Okto Grafx exposes OpenMetrics, but an application that wants none of it selects this adapter,
and selecting it must be free: no allocation, no string formatting, no lock, no branch on a
dictionary lookup. That is why every emitting method here has an empty body and why ``time()``
returns one shared, stateless context manager instead of building a new one per call.

The guarantee is verified rather than asserted: ``tests/observability/test_metrics_noop.py``
drives a hundred thousand iterations of the hot path and compares the allocator delta against an
empty control loop of the same length, which is the allocation-count test SPEC-M1 OR-6 asks for.

``enabled`` is False, which is the signal every caller reads before it builds a labels mapping.
The empty bodies below are the second line of defence, for the call sites where there is nothing
to build and the guard would cost more than the call.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from types import MappingProxyType, TracebackType

from okto_grafx.domain.ports.metrics import MetricDescriptor

__all__ = ["EMPTY_SNAPSHOT", "NULL_TIMER", "NullTimer", "NoOpMetricsSink"]


class NullTimer(AbstractContextManager[None]):
    """A context manager that measures nothing and holds nothing, so it can be shared forever.

    It carries no state at all, which is what makes a single module level instance safe to hand
    to every caller, on every thread, and to re-enter as many times as the call stack nests.
    """

    __slots__ = ()

    def __enter__(self) -> None:
        """Enter the timed block and return nothing."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Leave the timed block without recording anything and without swallowing an error."""
        return False


NULL_TIMER: NullTimer = NullTimer()
"""The single timer instance the no-op sink hands out, so ``time()`` allocates nothing."""

EMPTY_SNAPSHOT: Mapping[str, object] = MappingProxyType({})
"""The single empty mapping the no-op sink returns, so ``snapshot()`` allocates nothing."""


class NoOpMetricsSink:
    """The MetricsSink that collects nothing at all.

    Registration is accepted and discarded: a component may declare its catalog against this sink
    without knowing which adapter the composition root selected, and the descriptor has already
    validated itself on construction, so nothing is lost by not keeping it.
    """

    __slots__ = ()

    @property
    def enabled(self) -> bool:
        """Return False, which is the flag every hot path reads before it allocates."""
        return False

    def register(self, descriptor: MetricDescriptor) -> None:
        """Accept a declaration and keep nothing."""

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Discard a counter increment."""

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Discard a gauge assignment."""

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Discard a histogram observation."""

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> AbstractContextManager[None]:
        """Return the shared timer, the same object on every call, which allocates nothing."""
        return NULL_TIMER

    def snapshot(self) -> Mapping[str, object]:
        """Return the shared empty mapping; there is nothing to report and nothing to copy."""
        return EMPTY_SNAPSHOT
