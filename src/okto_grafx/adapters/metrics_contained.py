"""A containment shell around a host-supplied metrics sink (amendment A91, both halves).

THE PROBLEM THIS EXISTS FOR. A sink arrives through the registry, which validates its SHAPE and
cannot validate its behaviour, and the engine calls it from inside public doors -- including
AFTER a commit's invariants have settled. A sink that raised there made a durably committed
transaction report failure, and the caller's ordinary reaction -- retry the "failed" statement --
duplicated the row: data duplication reached through the public surface, with the engine correct
at every step. The composition already contains foreign exceptions at ``connect`` ("only Grafx
types leave a public door"); this is the same rule applied to every recording call for the life
of the database.

Telemetry is never load-bearing (G7 makes the CATALOGUE a contract, not the delivery), so a
recording failure is absorbed: the alternative on the post-commit path is reporting a lie about
durability. ``publish`` is the one door whose failures a caller acts on -- it is an explicit
request to write a document -- so a Grafx refusal passes through it and anything else becomes a
typed refusal rather than an escape.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink

__all__ = ["ContainedMetricsSink"]


class _ContainedTimer(AbstractContextManager):
    """Run a sink's timer context without letting either half of it raise."""

    __slots__ = ("_inner",)

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __enter__(self) -> object:
        try:
            return self._inner.__enter__()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the whole point of the shell
            return None

    def __exit__(self, kind: object, value: object, trace: object) -> bool:
        try:
            self._inner.__exit__(kind, value, trace)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return False


class ContainedMetricsSink:
    """The sink the ENGINE sees: every recording door absorbs what the inner sink raises."""

    __slots__ = ("_inner",)

    def __init__(self, inner: MetricsSink) -> None:
        self._inner = inner

    @property
    def inner(self) -> MetricsSink:
        """Return the sink this shell contains, for the doors that own its lifecycle."""
        return self._inner

    @property
    def enabled(self) -> bool:
        """Return the inner sink's answer, and False when even asking raises."""
        try:
            return bool(self._inner.enabled)
        except Exception:  # noqa: BLE001
            return False

    def register(self, descriptor: MetricDescriptor) -> None:
        """Register on the inner sink, absorbing a refusal of a catalogued descriptor."""
        try:
            self._inner.register(descriptor)
        except Exception:  # noqa: BLE001
            return

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        """Record a counter movement, absorbing whatever the inner sink raises."""
        try:
            self._inner.increment(name, value, labels)
        except Exception:  # noqa: BLE001
            return

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        """Record a gauge, absorbing whatever the inner sink raises."""
        try:
            self._inner.set_gauge(name, value, labels)
        except Exception:  # noqa: BLE001
            return

    def observe(self, name: str, value: float, labels: object = None) -> None:
        """Record an observation, absorbing whatever the inner sink raises."""
        try:
            self._inner.observe(name, value, labels)
        except Exception:  # noqa: BLE001
            return

    def time(self, name: str, labels: object = None) -> AbstractContextManager:
        """Return the inner timer wrapped so neither entering nor leaving it can raise."""
        try:
            return _ContainedTimer(self._inner.time(name, labels))
        except Exception:  # noqa: BLE001
            return nullcontext()

    def snapshot(self) -> object:
        """Return the inner snapshot, or an empty one when asking raises."""
        try:
            return self._inner.snapshot()
        except Exception:  # noqa: BLE001
            return {}

    def publish(self) -> object:
        """Publish through the inner sink; a Grafx refusal passes, anything else is typed.

        The one door whose failure a caller ACTS on: publishing is an explicit request to write
        a document, so absorbing its refusal would be silent loss, which is the opposite trade
        from the recording doors.
        """
        publish = getattr(self._inner, "publish", None)
        if not callable(publish):
            return None
        try:
            return publish()
        except GrafxError:
            raise
        except Exception as failure:  # noqa: BLE001
            raise GrafxConfigurationError(
                f"The metrics sink failed to publish: {type(failure).__name__}: {failure}",
                field="metrics",
                value=type(self._inner).__name__,
            ) from failure

    def __repr__(self) -> str:
        """Return a short representation naming the contained sink."""
        return f"ContainedMetricsSink({self._inner!r})"
