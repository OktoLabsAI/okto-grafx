"""Two threads meeting the derived HNSW graph on its first use (P0.5; CONTRACT.md section 8.8).

The graph is derived state, built on the first search after open or after an invalidation. Until
this round it was PUBLISHED before it was filled: ``graph()`` assigned ``self._graph`` and then
installed the entries one by one, so a second search arriving mid-build took the
``self._graph is not None`` fast path and traversed the fragment -- eight live rows, three
nodes, ``stale`` False, ``achieved_k`` reported as if complete. Found by Codex's audit
(EVOLUTION_PLAN_CODEX.md P0.5), and made deterministic here: the ``VectorMath`` port is the lever,
because every insert past the first scores the new node through it, so parking the calling
thread at the Nth score call is a point INSIDE a cold build that a test can hold and release.

Four interleavings:

* a search arriving mid-build WAITS for the build and answers from the complete snapshot
  (pre-fix: it answered at once, from the fragment; a single-flight that does not hold --
  no wait, or a patience of zero -- fails the same assertions);
* a builder that fails after another builder published does not erase that success (pre-fix:
  its ``except: invalidate_graph()`` dropped whatever was published);
* a commit landing during the build is not certified into a snapshot that lacks it -- the
  regression for the fixed protocol itself, which a naive "build in locals, stamp the header at
  the end" would fail, and which the pre-fix tree happens to pass for the wrong reason (it noted
  the commit into the partial graph it had already published);
* a build that calls back into its own index (host code behind the ``VectorMath`` port calling
  ``search``) does not wait on itself (the M0A cross-review's self-wait).

Everything runs through the public door with the default composition, so the guard the assembly
hands in is the one under test. Every answer is asserted by the record ids it returned, not by
its count alone. The one private reading, ``index._snapshot``, is the derived state whose
publication is the property; ``index._resolve`` is wrapped only to COUNT builds by the entries
they resolve.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
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
ALL_IDS: tuple[int, ...] = tuple(range(1, ROWS + 1))
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


class ReentrantMath(GatingMath):
    """Host code that, once, calls ``search()`` on the same space from inside the cold build."""

    def __init__(self) -> None:
        super().__init__(
            park_at=10**9
        )  # never parks; the gate is not what this one is for
        self.database: Any = None
        self.inner: object = None
        self.inner_seconds: float = -1.0
        self._reentered = False

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        if not self._reentered and self.database is not None:
            self._reentered = True
            started = time.monotonic()
            self.inner = _search(self.database)
            self.inner_seconds = time.monotonic() - started
        return super().score(a, b, metric)


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


def _search(database: Any, k: int = ROWS) -> tuple[int, str, tuple[int, ...]]:
    """Search under a fresh read snapshot; return (achieved_k, regime, sorted record ids)."""
    reader = database.begin("read")
    try:
        # This suite instruments interleavings *inside* VectorEngine. The public facade holds the
        # recovery-safe participant section around a search, intentionally excluding a same-
        # participant commit; use the owned engine here so the unit-level HNSW windows remain
        # reachable without publishing it to library callers.
        hits = database._vectors.search(
            space="s",
            k=k,
            query=[1.0, 0.0, 0.25, 0.0],
            snapshot=reader.snapshot,
        )
        return (
            hits.achieved_k,
            hits.regime,
            tuple(sorted(hit.record_id for hit in hits.hits)),
        )
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


def _count_resolves(index: Any) -> Counter[int]:
    """Wrap the index's resolver to count, per thread, the entries a build resolves."""
    resolved = index._resolve  # noqa: SLF001 - builds are counted by what they resolve
    resolves: Counter[int] = Counter()

    def counting_resolve(ref: object) -> object:
        resolves[threading.get_ident()] += 1
        return resolved(ref)

    index._resolve = counting_resolve  # type: ignore[assignment]  # noqa: SLF001
    return resolves


def test_a_search_arriving_mid_build_waits_and_answers_from_a_complete_snapshot(
    tmp_path: Path,
) -> None:
    """The first P0.5 interleaving: two cold searches, the second lands inside the first's build.

    Pre-fix the second search returned at once out of the fragment (three nodes of eight).
    Post-fix it WAITS behind the build and answers from the complete snapshot, and the two
    searches agree. The wait is asserted, not assumed: with the builder still parked one second
    after the second search started, the second thread must still be alive (waiting) and must
    have resolved no entry of its own. A search that finished, or built for itself, is the
    defect -- or a single-flight that does not hold (``_BUILD_WAIT_SLICES = 0``, or no wait).
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    idents: dict[str, int] = {}
    try:
        _populate(database, ROWS)
        resolves = _count_resolves(database._vectors.index("s"))

        def search_as(name: str) -> None:
            idents[name] = threading.get_ident()
            _search_into(database, outcomes, name)

        first = threading.Thread(target=search_as, args=("first",))
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        second = threading.Thread(target=search_as, args=("second",))
        second.start()
        second.join(1.0)
        assert second.is_alive(), (
            f"the second search finished while the build was still parked: {outcomes}"
        )
        assert resolves[idents["second"]] == 0, "the second search built for itself"
        math.release()
        _finish(first, second)
        assert resolves[idents["second"]] == 0, "the second search built after waiting"
    finally:
        math.release()
        database.close()
        release_ports(registry)
    assert outcomes["first"] == (ROWS, "approximate", ALL_IDS)
    assert outcomes["second"] == (ROWS, "approximate", ALL_IDS), outcomes


def test_a_builder_failing_after_another_published_does_not_erase_the_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second P0.5 interleaving: builder A fails AFTER builder B published a complete snapshot.

    Post-fix a search that meets a build in flight waits behind it, so holding TWO builders at
    once takes shrinking that patience: the names do not exist pre-fix (``raising=False``), and
    pre-fix the second search does not wait at all, which is the first defect. Then the parked
    builder is made to fail on its next resolve, and what must survive is B's snapshot: the
    failure drops A's locals and nothing else, and a third search answers from B's snapshot
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
        index = database._vectors.index("s")
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
        assert outcomes["second"] == (ROWS, "approximate", ALL_IDS), outcomes
        published = index._snapshot  # noqa: SLF001 - the property: B's complete snapshot
        assert published is not None

        math.release()
        _finish(first)
        assert outcomes["first"] == ("ERROR", "GrafxIndexError"), outcomes
        assert index._snapshot is published  # noqa: SLF001 - A's failure erased nothing
        index._resolve = resolved  # type: ignore[assignment]  # noqa: SLF001
        assert _search(database) == (ROWS, "approximate", ALL_IDS)
        assert index._snapshot is published  # noqa: SLF001 - and nothing was rebuilt
    finally:
        math.release()
        database.close()
        release_ports(registry)


def test_a_commit_landing_during_the_build_is_not_certified_into_a_snapshot_that_lacks_it(
    tmp_path: Path,
) -> None:
    """The third P0.5 interleaving: a commit lands while the cold build is parked.

    The build was started over seven rows; the eighth is committed while it is parked. Nothing
    is published for that commit to note into, so the snapshot the build publishes lacks the
    row -- which is fine for the search that built it (the row's commit is above its
    transaction's snapshot) and must NOT be fine for the next one: the snapshot carries the
    header reading from BEFORE the walk, the next search finds it behind and rebuilds. A build
    that stamped the header at the end would certify the seven-row snapshot as current and
    answer seven for ever.
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
        assert outcomes["first"] == (ROWS - 1, "approximate", ALL_IDS[:-1]), outcomes
        assert _search(database) == (ROWS, "approximate", ALL_IDS)
    finally:
        math.release()
        database.close()
        release_ports(registry)


def test_a_build_that_calls_back_into_its_own_index_does_not_wait_on_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M0A cross-review's self-wait, through the public door.

    The build scores through the VectorMath port, and the port is host code: here it calls
    ``search()`` on the same space once, from inside the cold build. Before the fix the inner
    search found the build in flight and waited for it -- for itself -- until the patience ran
    out (sixty seconds under the defaults). The patience is shortened here so the pre-fix run
    ends inside the test, at two seconds; the inner search must finish well inside that, and
    both searches must answer every row.
    """
    from okto_grafx.engine import vector_engine as engine_module

    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(engine_module, "_BUILD_WAIT_SLICES", 4, raising=False)
    math = ReentrantMath()
    database, registry = _open(tmp_path / "db", math)
    try:
        _populate(database, ROWS)
        math.database = database
        outer = _search(database)
    finally:
        database.close()
        release_ports(registry)
    assert math.inner is not None, "the port never called back into the index"
    assert math.inner == (ROWS, "approximate", ALL_IDS)
    assert outer == (ROWS, "approximate", ALL_IDS)
    assert math.inner_seconds < 1.0, (
        f"the re-entrant search took {math.inner_seconds:.2f}s: it waited on its own build"
    )


class _SeesEveryLiveVersion:
    """A structural ``SnapshotLike`` that admits every live version: the predicate is the caller's.

    ``SnapshotLike`` is taken by shape (A19), so a caller may bring any predicate. What such a
    caller is promised is LINEARIZATION, not "everything at return": the snapshot a search
    answers from was certified for a header position the build verified, so it holds every
    commit at or below that position; a commit that lands after the build's last catch-up pass
    is not promised at return -- the mark stays behind the header, and the NEXT search takes it
    (see the commit-storm test). A transaction's fixed snapshot cannot tell the difference.
    """

    def visible(self, xmin: int, xmax: int) -> bool:
        return xmin != 0 and xmax == 0


def _search_permissive(database: Any) -> tuple[int, str, tuple[int, ...]]:
    hits = database._vectors.search(
        space="s",
        k=2 * ROWS,
        query=[1.0, 0.0, 0.25, 0.0],
        snapshot=_SeesEveryLiveVersion(),
    )
    return (
        hits.achieved_k,
        hits.regime,
        tuple(sorted(hit.record_id for hit in hits.hits)),
    )


def test_a_commit_landing_during_the_build_is_caught_up_before_the_snapshot_is_published(
    tmp_path: Path,
) -> None:
    """The M0A cross-review's blocking regression: a commit during the build is taken in.

    Seven rows, the cold build parked, the eighth row committed, the build released. Under a
    predicate that admits every live version, ``main@12c67c8`` answered eight -- the commit was
    noted into the partial graph it had already published -- and ``8c88e9d`` answered seven and
    then eight, because the build published a snapshot with the commit neither noted nor caught
    up. The build now catches up before publishing: the commit landed before the last pass, so
    the search that built answers all eight (and would answer them under a fixed snapshot taken
    after the commit, too).
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}

    def search_into(name: str) -> None:
        try:
            outcomes[name] = _search_permissive(database)
        except Exception as failure:  # noqa: BLE001 - the outcome IS what is recorded
            outcomes[name] = ("ERROR", type(failure).__name__)

    try:
        _populate(database, ROWS - 1)
        first = threading.Thread(target=search_into, args=("first",))
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        with database.begin("write") as txn:
            _insert(txn, ROWS)
        math.release()
        _finish(first)
        assert outcomes["first"] == (ROWS, "approximate", ALL_IDS), outcomes
        assert _search_permissive(database) == (ROWS, "approximate", ALL_IDS)
    finally:
        math.release()
        database.close()
        release_ports(registry)


def test_a_commit_landing_during_the_catch_up_pass_is_caught_up_too(
    tmp_path: Path,
) -> None:
    """The catch-up pass has the same window the build had, and closes it the same way.

    Seven rows; the build is parked; the eighth is committed; the build is released and its
    catch-up pass begins -- and is parked again, INSIDE that pass, on the builder's eighth resolve
    (the first entry the pass's rebuild resolves, after the pass read the header). The ninth row is
    committed there. The pass must certify with the header it read BEFORE its walk, find the
    header moved, and run again: a pass that certified with a header read after its walk would
    publish a snapshot missing the ninth row as current, and the next search would answer eight.
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    parked_in_catch_up = threading.Event()
    release_catch_up = threading.Event()
    try:
        _populate(database, ROWS - 1)
        index = database._vectors.index("s")
        resolved = index._resolve  # noqa: SLF001 - the resolve of the eighth row is the hook
        resolves = Counter()

        def parking_resolve(ref: object) -> object:
            me = threading.get_ident()
            resolves[me] += 1
            if me == math.builder_ident and resolves[me] == ROWS:
                parked_in_catch_up.set()
                release_catch_up.wait(PATIENCE)
            return resolved(ref)

        index._resolve = parking_resolve  # type: ignore[assignment]  # noqa: SLF001
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
        assert parked_in_catch_up.wait(PATIENCE), (
            "the catch-up pass never resolved row 8"
        )
        with database.begin("write") as txn:
            _insert(txn, ROWS + 1)
        release_catch_up.set()
        _finish(first)
        expected = tuple(range(1, ROWS + 2))
        assert _search(database, k=ROWS + 1) == (ROWS + 1, "approximate", expected)
    finally:
        math.release()
        release_catch_up.set()
        database.close()
        release_ports(registry)


EXTRA_ID: int = 100
"""The Cypher id of the row the reconcile transaction also writes (its RECORD id is the next one
the heap hands out): a reconcile alone declares no partition, and a transaction that could never
be refused by optimistic validation is refused outright."""


def _delete_and_reconcile(database: Any, record_id: int) -> None:
    """Tombstone one row, then reconcile its entry away (beside one row write), through the door."""
    with database.begin("write") as txn:
        txn.execute("MATCH (v:V {id: $i}) DELETE v", {"i": record_id})
    horizon = database.transactions.published_state().last_csn
    with database.begin("write") as txn:
        _insert(txn, EXTRA_ID)
        database._vectors.reconcile("s", horizon, txn._context)


def _search_permissive_into(
    database: Any, outcomes: dict[str, object], name: str
) -> None:
    try:
        outcomes[name] = _search_permissive(database)
    except Exception as failure:  # noqa: BLE001 - the outcome IS what is recorded
        outcomes[name] = ("ERROR", type(failure).__name__)


def test_an_entry_reconciled_away_during_the_build_is_not_in_the_published_snapshot(
    tmp_path: Path,
) -> None:
    """The M0A verification's finding (Codex): a deleted row survived the catch-up as live.

    Eight rows; the cold build is parked; the second row is tombstoned AND reconciled away, so
    the walk no longer holds it (a ninth row, id 100, rides the reconcile transaction); the
    build is released. A catch-up that only offered the new walk to the old picture kept the
    old copy of row 2 -- live, with no ending stamp -- and published it as current: searches
    answered ids 1..8. A rebuilt picture holds exactly what the store holds: every search
    excludes 2, includes the ninth record, and the next one does not reuse a wrong cache either.
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    expected = tuple(
        sorted((set(ALL_IDS) - {2}) | {ROWS + 1})
    )  # record id, not the Cypher id
    try:
        _populate(database, ROWS)
        first = threading.Thread(
            target=_search_permissive_into, args=(database, outcomes, "first")
        )
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        _delete_and_reconcile(database, 2)
        assert len(database._vectors.index("s").walk()) == ROWS  # A72: 2 gone, 100 there
        math.release()
        _finish(first)
        assert outcomes["first"] == (ROWS, "approximate", expected), outcomes
        assert _search_permissive(database) == (ROWS, "approximate", expected)
        assert _search(database) == (ROWS, "approximate", expected)
    finally:
        math.release()
        database.close()
        release_ports(registry)


def test_an_entry_reconciled_away_during_the_catch_up_pass_is_not_in_the_published_snapshot(
    tmp_path: Path,
) -> None:
    """The same finding, one window later: the removal lands inside the catch-up pass.

    Seven rows; the build is parked; the eighth row is committed (so a catch-up pass will run);
    the build is released and its pass is parked on the builder's eighth resolve -- inside the
    pass, after it read the header and took its walk. Row 2 is tombstoned and reconciled away
    there (row 100 rides the reconcile transaction). The pass certifies with the reading it
    took before its walk, finds the header moved, and rebuilds again: the published snapshot
    excludes 2 and holds records 1, 3..8 and the ninth.
    """
    math = GatingMath()
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    parked_in_catch_up = threading.Event()
    release_catch_up = threading.Event()
    expected = tuple(sorted((set(range(1, ROWS + 1)) - {2}) | {ROWS + 1}))  # record id
    try:
        _populate(database, ROWS - 1)
        index = database._vectors.index("s")
        resolved = index._resolve  # noqa: SLF001 - the builder's eighth resolve is the hook
        resolves = Counter()

        def parking_resolve(ref: object) -> object:
            me = threading.get_ident()
            resolves[me] += 1
            if me == math.builder_ident and resolves[me] == ROWS:
                parked_in_catch_up.set()
                release_catch_up.wait(PATIENCE)
            return resolved(ref)

        index._resolve = parking_resolve  # type: ignore[assignment]  # noqa: SLF001
        first = threading.Thread(
            target=_search_permissive_into, args=(database, outcomes, "first")
        )
        first.start()
        assert math.parked.wait(PATIENCE), (
            "the first search never reached the parked score"
        )
        with database.begin("write") as txn:
            _insert(txn, ROWS)
        math.release()
        assert parked_in_catch_up.wait(PATIENCE), (
            "the catch-up pass never began resolving"
        )
        _delete_and_reconcile(database, 2)
        assert (
            len(index.walk()) == ROWS
        )  # A72: eight rows, minus the reconciled, plus 100
        release_catch_up.set()
        _finish(first)
        assert outcomes["first"] == (ROWS, "approximate", expected), outcomes
        assert _search_permissive(database) == (ROWS, "approximate", expected)
        assert _search(database) == (ROWS, "approximate", expected)
    finally:
        math.release()
        release_catch_up.set()
        database.close()
        release_ports(registry)


def test_a_commit_storm_through_every_pass_leaves_the_mark_behind_and_the_next_search_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness and honesty under a commit storm: the cap holds and nothing is over-promised.

    A commit lands after the walk of the build and after the walk of each of the three catch-up
    passes (the builder is parked on the first resolve of each: cumulative resolves 1, 8, 16
    and 25 over seven, eight, nine and ten entries). The build cannot chase the store for ever,
    so it publishes after the last pass with the mark honestly BEHIND the header: the search that
    built answers the ten rows its last walk saw, not the eleventh; the next search finds the
    mark behind, rebuilds, and answers all eleven. The third outcome is the one never allowed:
    a snapshot certified for a position it did not verify.
    """
    from okto_grafx.engine import vector_engine as engine_module

    monkeypatch.setattr(engine_module, "_BUILD_CATCH_UP_PASSES", 3, raising=False)
    math = GatingMath(park_at=10**9)  # the gate is not the lever here; the resolver is
    database, registry = _open(tmp_path / "db", math)
    outcomes: dict[str, object] = {}
    park_points = {1, 8, 16, 25}
    parked = threading.Event()
    released = threading.Event()
    builder: dict[str, int] = {}

    def search_into(name: str) -> None:
        builder["ident"] = threading.get_ident()
        _search_permissive_into(database, outcomes, name)

    try:
        _populate(database, ROWS - 1)
        index = database._vectors.index("s")
        resolved = index._resolve  # noqa: SLF001 - the first resolve of each walk is the hook
        resolves = Counter()

        def parking_resolve(ref: object) -> object:
            me = threading.get_ident()
            resolves[me] += 1
            if me == builder.get("ident") and resolves[me] in park_points:
                released.clear()
                parked.set()
                released.wait(PATIENCE)
            return resolved(ref)

        index._resolve = parking_resolve  # type: ignore[assignment]  # noqa: SLF001
        first = threading.Thread(target=search_into, args=("first",))
        first.start()
        for record_id in range(
            ROWS, ROWS + 4
        ):  # rows 8, 9, 10, 11: one per parked walk
            assert parked.wait(PATIENCE), (
                f"the builder never parked before row {record_id}"
            )
            parked.clear()
            with database.begin("write") as txn:
                _insert(txn, record_id)
            released.set()
        _finish(first)
        ten = tuple(range(1, ROWS + 3))
        eleven = tuple(range(1, ROWS + 4))
        # The last pass walked ten rows; the eleventh landed after that walk.
        assert outcomes["first"] == (ROWS + 2, "approximate", ten), outcomes
        published = index._snapshot  # noqa: SLF001 - the honesty of the mark is the property
        assert published is not None
        assert published.mark != index.built_through_lsn, "certified past its walk"
        # The next search sees the mark behind, rebuilds, and answers everything.
        assert _search_permissive(database) == (ROWS + 3, "approximate", eleven)
        assert _search(database, k=ROWS + 3) == (ROWS + 3, "approximate", eleven)
    finally:
        released.set()
        math.release()
        database.close()
        release_ports(registry)
