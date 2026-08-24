"""The single-flight build, owned by its thread: no self-wait, and a real wait for everybody else.

Two properties the M0A cross-review asked to see proved rather than assumed:

* A build that calls back into its own index must not wait on itself. The build scores through
  the ``VectorMath`` port, and the port is host-supplied code that may do anything -- including
  ``search()`` on the same index. Before the fix that inner search found ``_building`` set and
  waited for a build that could not finish until the inner search returned: sixty seconds of a
  search waiting for itself under the defaults. The guard now says which thread is asking, and
  the builder's own call builds its own picture at once.
* A search that meets ANOTHER thread's build waits for it and builds nothing of its own. This is
  what single-flight means, and it is asserted by counting: the waiting thread called
  ``wait_for`` at least once and resolved zero entries.

Both use a recording guard -- the assembly's own ``ConditionGuard``, with its ``wait_for`` calls
counted per thread -- so the assertions are structural, not timing.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from okto_grafx.adapters.graph_guard import ConditionGuard
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.ports.vectormath import DistanceMetric

from .conftest import RecordingMetrics, SnapshotDouble, StepClock, VectorFixture

ROWS = 8


class RecordingGuard(ConditionGuard):
    """The real guard, counting how many times each thread waited on it."""

    __slots__ = ("waits",)

    def __init__(self) -> None:
        super().__init__()
        self.waits: Counter[int] = Counter()

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        self.waits[threading.get_ident()] += 1
        return super().wait_for(predicate, timeout)


class _Delegating:
    """A VectorMath that forwards everything to the pure oracle; subclasses intercept score()."""

    def __init__(self) -> None:
        self._inner = PureVectorMath()

    @property
    def name(self) -> str:
        return "probe"

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.dot(a, b)

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.cosine(a, b)

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.euclidean(a, b)

    def norm(self, a: Sequence[float]) -> float:
        return self._inner.norm(a)

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        return self._inner.normalize(a)

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        return self._inner.score(a, b, metric)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        return self._inner.top_k(query, candidates, k, metric)


class ReentrantMath(_Delegating):
    """Host code that, once, calls search() on the same index from inside the build."""

    def __init__(self) -> None:
        super().__init__()
        self.search: Callable[[], Any] | None = None
        self.inner_result: Any = None
        self._reentered = False

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        if not self._reentered and self.search is not None:
            self._reentered = True
            self.inner_result = self.search()
        return super().score(a, b, metric)


class GatingMath(_Delegating):
    """Parks the FIRST thread that scores -- the cold builder -- at its Nth call until released."""

    def __init__(self, *, park_at: int = 4) -> None:
        super().__init__()
        self._park_at = park_at
        self._lock = threading.Lock()
        self._builder: int | None = None
        self._calls = 0
        self.parked = threading.Event()
        self.released = threading.Event()

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        me = threading.get_ident()
        with self._lock:
            if self._builder is None:
                self._builder = me
            park = False
            if me == self._builder:
                self._calls += 1
                park = self._calls == self._park_at
        if park:
            self.parked.set()
            self.released.wait(10.0)
        return super().score(a, b, metric)


def _database(
    metrics: RecordingMetrics, clock: StepClock, math: Any, guard: Any
) -> tuple[Any, Any]:
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=0, math=math, guard=guard
    )
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    for record_id in range(1, ROWS + 1):
        database.insert_row(
            table, record_id, 0, space, (float(record_id), 1.0, 1.0), csn=10
        )
    return database, space


def _ids(result: Any) -> tuple[int, ...]:
    """Return the record ids a search answered, sorted."""
    return tuple(sorted(hit.record_id for hit in result.hits))


def _search(database: Any) -> Any:
    return database.engine.search(
        space="space", query=(4.0, 1.0, 1.0), k=ROWS, snapshot=SnapshotDouble(1000)
    )


def test_a_build_that_calls_back_into_its_own_index_does_not_wait_on_itself(
    metrics: RecordingMetrics, clock: StepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_grafx.engine import vector_engine as engine_module

    # Short patience, so the pre-fix self-wait ends inside the test rather than after a minute;
    # the property itself is the count of waits, which the fix takes to zero.
    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SLICES", 4, raising=False)
    guard = RecordingGuard()
    math = ReentrantMath()
    database, _space = _database(metrics, clock, math, guard)
    math.search = lambda: _search(database)

    outer = _search(database)

    assert math.inner_result is not None, "the port never called back into the index"
    assert _ids(math.inner_result) == tuple(range(1, ROWS + 1))
    assert _ids(outer) == tuple(range(1, ROWS + 1))
    assert guard.waits[threading.get_ident()] == 0, (
        f"the builder's own re-entrant search waited {guard.waits[threading.get_ident()]} "
        "time(s) on the build it is part of"
    )


def test_a_search_meeting_another_threads_build_waits_and_builds_nothing(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    guard = RecordingGuard()
    math = GatingMath()
    database, _space = _database(metrics, clock, math, guard)
    index = database.engine.index("space")
    resolved = index._resolve  # noqa: SLF001 - counting builds by the entries they resolve
    resolves: Counter[int] = Counter()

    def counting_resolve(ref: object) -> object:
        resolves[threading.get_ident()] += 1
        return resolved(ref)

    index._resolve = counting_resolve  # type: ignore[assignment]  # noqa: SLF001

    outcomes: dict[str, Any] = {}
    idents: dict[str, int] = {}

    def run(name: str) -> None:
        idents[name] = threading.get_ident()
        outcomes[name] = _search(database)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert math.parked.wait(10.0), "the first search never reached the parked score"
    second = threading.Thread(target=run, args=("second",))
    second.start()
    # The second search must be WAITING behind the build: at least one wait on the guard, and
    # nothing resolved by it. The builder is still parked, so this is the state, not a race.
    deadline = threading.Event()
    for _ in range(200):
        if guard.waits[idents.get("second", -1)] >= 1:
            break
        deadline.wait(0.01)
    try:
        assert guard.waits[idents["second"]] >= 1, (
            "the second search never waited for the build"
        )
        assert resolves[idents["second"]] == 0, "the second search built for itself"
        assert second.is_alive(), (
            "the second search finished while the build was still parked"
        )
    finally:
        math.released.set()
        first.join(10.0)
        second.join(10.0)
    assert not first.is_alive() and not second.is_alive()
    assert _ids(outcomes["first"]) == tuple(range(1, ROWS + 1))
    assert _ids(outcomes["second"]) == tuple(range(1, ROWS + 1))
    assert resolves[idents["second"]] == 0, (
        "the second search built for itself after waiting"
    )
