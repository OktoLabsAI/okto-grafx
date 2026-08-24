"""Two threads meeting the derived HNSW graph on its first use (P0.5; CONTRACT.md section 8.8).

The graph is derived state, built on the first search after open or after an invalidation. Until
this round it was PUBLISHED before it was filled: ``graph()`` assigned ``self._graph`` and then
installed the entries one by one, so a second search arriving mid-build took the
``self._graph is not None`` fast path and traversed the fragment -- eight live rows, three
nodes, ``stale`` False, ``achieved_k`` reported as if complete. Found by Codex's audit
(EVOLUTION_PLAN_CODEX.md P0.5), and made deterministic here: the ``VectorMath`` port is the lever,
because every insert past the first scores the new node through it, so parking the calling
thread at the Nth score call is a point INSIDE a cold build that a test can hold and release.

Three interleavings, in the order the handoff lists them:

* a search arriving mid-build answers from a COMPLETE picture (pre-fix: from the fragment);
* a builder that fails after another builder published does not erase that success (pre-fix:
  its ``except: invalidate_graph()`` dropped whatever was published);
* a commit landing during the build is not certified into a picture that lacks it -- the
  regression for the fixed protocol itself, which a naive "build in locals, stamp the header at
  the end" would fail, and which the pre-fix tree happens to pass for the wrong reason (it noted
  the commit into the partial graph it had already published).

Everything runs through the public door with the default composition, so the guard the assembly
hands in is the one under test. The one private reading, ``index._snapshot``, is the derived
state whose publication is the property.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

ROWS: int = 8
PARK_AT: int = 4
"""The score call the builder is parked at: past the first insert, well before the eighth."""
PATIENCE: float = 10.0
"""How long a parked builder waits for its release before it goes on regardless."""


class GatingMath:
    """A ``VectorMath`` that parks the FIRST thread to score -- the cold builder -- at one call.

    Every other thread scores straight through, so a second search, a commit noting into a warm
    graph, or a second builder are never held by the gate meant for the first. The port is the
    right lever because it is what the composition root hands in: no private hook, no patched
    class, the same code path a host with its own adapter would run.
    """

    def __init__(self, *, park_at: int = PARK_AT, patience: float = PATIENCE) -> None:
        self._inner = PureVectorMath()
        self._park_at = park_at
        self._patience = patience
        self._lock = threading.Lock()
        self._builder: int | None = None
        self._calls = 0
        self.parked = threading.Event()
        self.released = threading.Event()

    @property
    def name(self) -> str:
        return "gating"

    @property
    def builder_ident(self) -> int | None:
        """Return the identity of the thread the gate holds (the first one that scored)."""
        return self._builder

    def release(self) -> None:
        """Let the parked builder go on."""
        self.released.set()

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
            self.released.wait(self._patience)
        return self._inner.score(a, b, metric)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        return self._inner.top_k(query, candidates, k, metric)


def _components(record_id: int) -> tuple[float, float, float, float]:
    """Return a distinct, non-degenerate vector for one row."""
    return (1.0 - record_id * 0.05, record_id * 0.05, 0.25, 0.0)


def _open(root: Path, math: GatingMath) -> tuple[Any, Any]:
    """Open a database through the public door, with the gating math bound in the registry.

    Returns the database and the registry: ports a caller supplied are not closed by the
    database, so the teardown releases them itself.
    """
    config = DatabaseConfig(path=str(root), vector_exact_scan_threshold=0)
    registry = build_default_registry(config)
    registry.bind("vector_math", math)
    return connect(root, registry=registry, vector_exact_scan_threshold=0), registry


def _populate(database: Any, rows: int) -> None:
    """Declare the space and the table, and commit ``rows`` vectors."""
    with database.begin("write") as txn:
        txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        txn.execute("CREATE NODE TABLE V(id INT64, e VECTOR(s), PRIMARY KEY(id))")
    with database.begin("write") as txn:
        for record_id in range(1, rows + 1):
            _insert(txn, record_id)


def _insert(txn: Any, record_id: int) -> None:
    a, b, c, d = _components(record_id)
    txn.execute(
        "CREATE (:V {id: $i, e: [$a, $b, $c, $d]})",
        {"i": record_id, "a": a, "b": b, "c": c, "d": d},
    )


def _search(database: Any, k: int = ROWS) -> tuple[int, str]:
    """Search under a fresh read snapshot and return (achieved_k, regime)."""
    reader = database.begin("read")
    try:
        hits = database.vectors.search(
            space="s",
            k=k,
            query=[1.0, 0.0, 0.25, 0.0],
            snapshot=reader.context.snapshot,
        )
        return (hits.achieved_k, hits.regime)
    finally:
        reader.rollback()


def _search_into(database: Any, outcomes: dict[str, object], name: str) -> None:
    """Thread body: record what the search answered, or the refusal it raised."""
    try:
        outcomes[name] = _search(database)
    except Exception as failure:  # noqa: BLE001 - the outcome IS what is recorded
        outcomes[name] = ("ERROR", type(failure).__name__)


def _finish(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(PATIENCE)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert not stuck, f"these searches never finished: {stuck}"


def test_a_search_arriving_mid_build_answers_from_a_complete_picture(
    tmp_path: Path,
) -> None:
    """The first P0.5 interleaving: two cold searches, the second lands inside the first's build.

    Pre-fix the second search returned at once out of the fragment (three nodes of eight).
    Post-fix it waits behind the build and answers from the complete picture, and the two
    searches agree. A bounded join is what tells the two apart without a private hook: the
    second thread finishing inside one second is the defect, waiting is the fix.
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    try:
        _populate(database, ROWS)
        first = threading.Thread(
            target=_search_into, args=(database, outcomes, "first")
        )
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        second = threading.Thread(
            target=_search_into, args=(database, outcomes, "second")
        )
        second.start()
        second.join(1.0)
        math.release()
        _finish(first, second)
    finally:
        math.release()
        database.close()
        release_ports(registry)
    assert outcomes["first"] == (ROWS, "approximate")
    assert outcomes["second"] == (ROWS, "approximate"), outcomes


def test_a_builder_failing_after_another_published_does_not_erase_the_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second P0.5 interleaving: builder A fails AFTER builder B published a complete picture.

    Post-fix a search that meets a build in flight waits behind it, so holding TWO builders at
    once takes shrinking that patience: the names do not exist pre-fix (``raising=False``), and
    pre-fix the second search does not wait at all, which is the first defect. Then the parked
    builder is made to fail on its next resolve, and what must survive is B's picture: the
    failure drops A's locals and nothing else, and a third search answers from B's picture
    without rebuilding.
    """
    from okto_grafx.engine import vector_engine as engine_module

    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SLICES", 1, raising=False)
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    try:
        _populate(database, ROWS)
        index = database.vectors.index("s")
        resolved = index._resolve  # noqa: SLF001 - the refusal is injected where it arises

        def refuse_the_parked_builder_after_release(ref: object) -> object:
            if math.released.is_set() and threading.get_ident() == math.builder_ident:
                raise GrafxIndexError("the resolver could not read this row", index="s")
            return resolved(ref)

        index._resolve = refuse_the_parked_builder_after_release  # type: ignore[assignment]  # noqa: SLF001

        first = threading.Thread(
            target=_search_into, args=(database, outcomes, "first")
        )
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        second = threading.Thread(
            target=_search_into, args=(database, outcomes, "second")
        )
        second.start()
        _finish(second)
        assert outcomes["second"] == (ROWS, "approximate"), outcomes
        published = index._snapshot  # noqa: SLF001 - the property: B's complete picture
        assert published is not None

        math.release()
        _finish(first)
        assert outcomes["first"] == ("ERROR", "GrafxIndexError"), outcomes
        assert index._snapshot is published  # noqa: SLF001 - A's failure erased nothing
        index._resolve = resolved  # type: ignore[assignment]  # noqa: SLF001
        assert _search(database) == (ROWS, "approximate")
        assert index._snapshot is published  # noqa: SLF001 - and nothing was rebuilt
    finally:
        math.release()
        database.close()
        release_ports(registry)


def test_a_commit_landing_during_the_build_is_not_certified_into_a_picture_that_lacks_it(
    tmp_path: Path,
) -> None:
    """The third P0.5 interleaving: a commit lands while the cold build is parked.

    The build was started over seven rows; the eighth is committed while it is parked. Nothing
    is published for that commit to note into, so the picture the build publishes lacks the
    row -- which is fine for the search that built it (the row's commit is above its snapshot)
    and must NOT be fine for the next one: the picture carries the header reading from BEFORE
    the walk, the next search finds it behind and rebuilds. A build that stamped the header at
    the end would certify the seven-row picture as current and answer seven for ever.
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    try:
        _populate(database, ROWS - 1)
        first = threading.Thread(
            target=_search_into, args=(database, outcomes, "first")
        )
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        with database.begin("write") as txn:
            _insert(txn, ROWS)
        math.release()
        _finish(first)
        assert outcomes["first"] == (ROWS - 1, "approximate"), outcomes
        assert _search(database) == (ROWS, "approximate")
    finally:
        math.release()
        database.close()
        release_ports(registry)
