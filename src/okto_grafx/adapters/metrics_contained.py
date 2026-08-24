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
durability. ``snapshot`` and ``publish`` are explicit operator requests rather than recording
callbacks. Their failures therefore remain observable so the public facade can preserve an
existing Grafx refusal or process signal and translate an ordinary host exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from threading import Lock, local
from time import perf_counter

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
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            return None

    def __exit__(self, kind: object, value: object, trace: object) -> bool:
        try:
            self._inner.__exit__(kind, value, trace)  # type: ignore[attr-defined]
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            pass
        return False


class _DeferredTimer(AbstractContextManager):
    """Measure locally and enqueue one histogram observation without calling host code."""

    __slots__ = ("_labels", "_name", "_owner", "_started")

    def __init__(self, owner: ContainedMetricsSink, name: str, labels: object) -> None:
        self._owner = owner
        self._name = name
        self._labels = labels
        self._started = 0.0

    def __enter__(self) -> None:
        self._started = perf_counter()

    def __exit__(self, kind: object, value: object, trace: object) -> bool:
        self._owner._defer_call(
            "observe",
            self._name,
            perf_counter() - self._started,
            self._labels,
        )
        return False


class _DeferredState(local):
    """Per-thread nesting and callbacks waiting for the outer critical section to leave."""

    def __init__(self) -> None:
        self.depth: int = 0
        self.transition_depth: int = 0
        self.calls: list[tuple[str, tuple[object, ...]]] = []


class ContainedMetricsSink:
    """The sink the ENGINE sees: every recording door absorbs what the inner sink raises."""

    __slots__ = (
        "_enabled_hint",
        "_inner",
        "_state",
        "_transition_guard",
        "_transitions",
    )

    def __init__(self, inner: MetricsSink) -> None:
        self._inner = inner
        self._state = _DeferredState()
        self._transition_guard = Lock()
        self._transitions = 0
        try:
            self._enabled_hint = bool(inner.enabled)
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            self._enabled_hint = False

    @property
    def inner(self) -> MetricsSink:
        """Return the sink this shell contains, for the doors that own its lifecycle."""
        return self._inner

    @property
    def enabled(self) -> bool:
        """Return the inner sink's answer, and False when even asking raises."""
        if self._state.depth > 0:
            # Asking the host is itself a callback. During an engine transition use the last
            # answer observed outside it; recording calls are queued behind the same boundary.
            return self._enabled_hint
        try:
            self._enabled_hint = bool(self._inner.enabled)
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            self._enabled_hint = False
        return self._enabled_hint

    @property
    def transition_active(self) -> bool:
        """Return whether this execution thread is inside a contained engine transition."""
        state = self._state
        return state.depth > 0 or state.transition_depth > 0

    @property
    def facade_transition_active(self) -> bool:
        """Return whether any thread still owes publication of one facade outcome."""
        with self._transition_guard:
            return self._transitions > 0

    @contextmanager
    def transition(self) -> Iterator[None]:
        """Track one facade lifecycle outcome without invoking or delaying host callbacks.

        The thread-local count is process mechanism and therefore belongs in this adapter, not
        the pure engine. It distinguishes a callback re-entering its own transition from another
        thread that must contend for the manager's participant section normally. A separately
        guarded total lets the facade quiesce that manager without releasing lower dependencies
        until every thread has finished publishing its wrapper/schema outcome.
        """
        state = self._state
        state.transition_depth += 1
        with self._transition_guard:
            self._transitions += 1
        try:
            yield
        finally:
            with self._transition_guard:
                self._transitions -= 1
            state.transition_depth -= 1

    @contextmanager
    def defer(self) -> Iterator[None]:
        """Queue host callbacks until the outermost transition on this thread has left.

        The engine wraps participant lifecycle sections with this context. WAL, coordinator and
        pool metrics may therefore be *recorded* while a transaction is changing state, but the
        host sink cannot re-enter ``Database.close`` until the section and its lease/commit
        nesting are gone. Nested sections share one FIFO and only the outermost exit drains it.
        """
        state = self._state
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                calls = state.calls
                state.calls = []
                for method, arguments in calls:
                    self._deliver(method, arguments)

    def _defer_call(self, method: str, *arguments: object) -> None:
        """Append one recording call to the current thread's FIFO."""
        self._state.calls.append((method, arguments))

    def _record(self, method: str, *arguments: object) -> None:
        """Queue one recording call in a transition, otherwise contain and deliver it now."""
        if self._state.depth > 0:
            self._defer_call(method, *arguments)
            return
        self._deliver(method, arguments)

    def _deliver(self, method: str, arguments: tuple[object, ...]) -> None:
        """Call one host recording door without letting it control the engine outcome."""
        try:
            getattr(self._inner, method)(*arguments)
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            return

    def register(self, descriptor: MetricDescriptor) -> None:
        """Register on the inner sink, absorbing a refusal of a catalogued descriptor."""
        self._record("register", descriptor)

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        """Record a counter movement, absorbing whatever the inner sink raises."""
        self._record("increment", name, value, labels)

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        """Record a gauge, absorbing whatever the inner sink raises."""
        self._record("set_gauge", name, value, labels)

    def observe(self, name: str, value: float, labels: object = None) -> None:
        """Record an observation, absorbing whatever the inner sink raises."""
        self._record("observe", name, value, labels)

    def time(self, name: str, labels: object = None) -> AbstractContextManager:
        """Return the inner timer wrapped so neither entering nor leaving it can raise."""
        if self._state.depth > 0:
            return _DeferredTimer(self, name, labels)
        try:
            return _ContainedTimer(self._inner.time(name, labels))
        except BaseException:  # noqa: BLE001 - telemetry never controls engine outcome
            return nullcontext()

    def snapshot(self) -> object:
        """Return the inner snapshot for the facade to validate and detach.

        Unlike a recording callback, this is an explicit read whose caller acts on the answer.
        Returning an empty mapping after a sink failure would make "no metrics" indistinguishable
        from "the metrics could not be observed", so failures cross this adapter unchanged and
        are classified at :meth:`Database.snapshot_metrics`.
        """
        return self._inner.snapshot()

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
