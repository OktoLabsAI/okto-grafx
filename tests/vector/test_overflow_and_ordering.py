"""Two defects a round-one critic found, and the states they leave behind (B1, B2).

**B1** -- ``math.fsum`` raises ``OverflowError`` when its exact running sum leaves double range,
so the guard written on the RESULT of an operation never received one and a non-``Grafx``
exception left the port. Only ``dot`` was exposed, because the other metrics square their terms
and reach the guard through an infinity. The port is fixed at its own door and tested there; what
this module adds is the reachable path through the engine, at default configuration.

**B2** -- an installer that publishes state before the step that can still refuse leaves an entry
half-reachable: countable but unrankable, invisible to a search and fatal to a walk. The rule is
one this build has paid for repeatedly, so the test asserts the STATE after a refusal rather than
only that the refusal happened.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxError, GrafxIndexError, GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import DistanceMetric

from .conftest import (
    RecordingMetrics,
    SnapshotDouble,
    StepClock,
    TransactionDouble,
    VectorFixture,
)

OVERFLOWING: tuple[float, ...] = (1e308, 1e308, -1e308)
"""Three finite components of a float64 space whose exact sum leaves the range of a double.

Every component is storable, every product against a unit vector is finite, and the float32 range
check does not apply to a float64 space -- so nothing on the write path can refuse this, which is
what made the overflow reachable from ordinary configuration.
"""


def _dot_space(
    metrics: RecordingMetrics, clock: StepClock
) -> tuple[VectorFixture, object, object]:
    """Return a database with one float64 dot-product space and its table."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=4096)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    return database, space, table


def test_the_write_door_accepts_the_components_that_overflow_a_sum(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The premise of the whole module, asserted rather than assumed (amendment A72).

    If validation refused these components the overflow would be unreachable and every test below
    would be vacuous. It does not: they are finite, and finiteness is what the write door checks.
    """
    database, space, _table = _dot_space(metrics, clock)
    assert database.engine.validate_vector(space, OVERFLOWING) == OVERFLOWING


def test_a_search_over_an_overflowing_vector_refuses_in_the_taxonomy(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """B1 through the engine: the escape was an OverflowError out of a public door."""
    database, space, table = _dot_space(metrics, clock)
    database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    database.insert_row(table, 2, 0, space, OVERFLOWING, csn=11)
    with pytest.raises(GrafxError) as failure:
        database.engine.search(
            space="space", query=(1.0, 1.0, 1.0), k=2, snapshot=SnapshotDouble(1000)
        )
    assert isinstance(failure.value, GrafxError)
    assert failure.value.details.get("reason") == "non_finite_result"


def test_no_overflow_escapes_as_a_non_grafx_exception_from_any_regime(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """Both regimes reach the same arithmetic, so both are asserted to refuse in the taxonomy."""
    for threshold in (4096, 0):
        database = VectorFixture(
            metrics=metrics, clock=clock, exact_scan_threshold=threshold
        )
        space = database.create_space(
            "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
        )
        table = database.create_table("Chunk", "space")
        database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
        try:
            database.insert_row(table, 2, 0, space, OVERFLOWING, csn=11)
            database.engine.search(
                space="space", query=(1.0, 1.0, 1.0), k=2, snapshot=SnapshotDouble(1000)
            )
        except GrafxError:
            continue
        except BaseException as escaped:  # noqa: BLE001 - the defect this test exists for
            raise AssertionError(
                f"a {type(escaped).__name__} escaped the engine at threshold {threshold}"
            ) from escaped


def test_a_refused_graph_install_leaves_no_half_reachable_entry(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """B2: nothing becomes reachable before every step that can still refuse has succeeded.

    The install publishes three mappings and then inserts into the graph, and the insertion
    scores the new node against its neighbours -- so it can refuse. Publishing first left an
    entry that the planner counted, no search could rank, and every later walk turned into a bare
    ``KeyError``. The assertion is therefore about the STATE the refusal leaves, not about the
    refusal.
    """
    database, space, table = _dot_space(metrics, clock)
    database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    index.graph()
    ref = database.heap.insert(
        table,
        2,
        (2, 0, _stored(database, space, OVERFLOWING)),
        11,
    )
    txn = TransactionDouble()
    database.engine.stage_insert("space", 2, ref, OVERFLOWING, 11, txn)
    with pytest.raises(GrafxError):
        database.engine.commit("space", txn, 11)

    # The store is correct: the entry is durable, because the log record was correct.
    assert len(index.walk()) == 2
    assert index.live_count() == 2

    # The PROPERTY, not the symptom (amendment A71). A refusal that published first would leave
    # the graph holding a half-linked node and the three mappings naming it, so the derived state
    # and the store would disagree about what exists. Asserting that a search still raises would
    # pass either way -- the same arithmetic refuses it in both -- so what is asserted is that
    # the derived state was DISCARDED, which is the only outcome that cannot disagree.
    assert index._snapshot is None  # noqa: SLF001 - the derived state under test

    # And every door of the store still answers, none of them with a bare KeyError.
    for call in (
        lambda: index.walk(),
        lambda: index.lookup(index.key_for((1.0, 1.0, 1.0)), SnapshotDouble(1000)),
        lambda: index.live_count(),
        lambda: index.visible_count(SnapshotDouble(1000)),
    ):
        call()
    with pytest.raises(GrafxError):
        index.search((1.0, 1.0, 1.0), 2, SnapshotDouble(1000))


def test_the_index_recovers_once_the_offending_row_is_gone(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A refusal must be recoverable, or one bad row would end the space permanently."""
    database, space, table = _dot_space(metrics, clock)
    good = database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    ref = database.heap.insert(
        table, 2, (2, 0, _stored(database, space, OVERFLOWING)), 11
    )
    index = database.engine.index("space")
    index.graph()
    txn = TransactionDouble()
    database.engine.stage_insert("space", 2, ref, OVERFLOWING, 11, txn)
    with pytest.raises(GrafxError):
        database.engine.commit("space", txn, 11)

    removal = TransactionDouble()
    database.engine.stage_delete("space", 2, ref, OVERFLOWING, 20, removal)
    database.engine.commit("space", removal, 20)
    reclaim = TransactionDouble()
    database.engine.reconcile("space", 20, reclaim)
    database.engine.commit("space", reclaim, 21)

    result = database.engine.search(
        space="space", query=(1.0, 1.0, 1.0), k=2, snapshot=SnapshotDouble(1000)
    )
    assert [hit.record_id for hit in result.hits] == [1]
    assert result.hits[0].ref == good
    assert len(index.walk()) == 1


def test_a_cosine_space_refuses_the_same_vector_through_its_norm(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The metric decides which guard answers, and both answers are in the taxonomy.

    Under cosine the squares overflow to an infinity and the result guard catches it; under dot
    the products stay finite and the accumulation raises. Naming both is what stops a later
    reader concluding the result guard was sufficient on its own.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=4096)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.COSINE, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    ref = database.heap.insert(
        table, 2, (2, 0, _stored(database, space, OVERFLOWING)), 11
    )
    database.engine.index("space").graph()
    txn = TransactionDouble()
    database.engine.stage_insert("space", 2, ref, OVERFLOWING, 11, txn)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.commit("space", txn, 11)
    assert failure.value.details["reason"] == "non_finite_result"


def test_an_index_registered_now_is_readable_by_a_cold_reader(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A freshly created index file must be readable after its cache is dropped.

    Registration writes the header and the bucket heads; a reader that meets the file with a cold
    cache reads them back from the device. This is the path a stale-looking header would break,
    and it belongs in this suite because the vector index is the newest caller of it.
    """
    database, space, table = _dot_space(metrics, clock)
    database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    database.pool.invalidate()
    assert index.header.built_through_lsn >= 0
    assert len(index.walk()) == 1
    assert index.check_freshness(index.built_through_lsn) is False
    assert space.name == "space" and table.name == "Chunk"


def _stored(database: VectorFixture, space: object, values: tuple[float, ...]) -> object:
    """Return the storable vector value of these components in one space."""
    from okto_grafx.domain.model.value import VectorValue

    return VectorValue(
        database.engine.validate_vector(space, values),  # type: ignore[arg-type]
        space.space_id,  # type: ignore[attr-defined]
        space.storage_dtype,  # type: ignore[attr-defined]
    )


MIXED_SIGN_OVERFLOW: tuple[float, ...] = (1e300, 1e300, 0.0)
"""Two finite components whose PRODUCTS overflow with opposite signs against the query below.

The distinction from :data:`OVERFLOWING` is the whole point. There the products stay finite and
only their exact sum leaves the range, so ``math.fsum`` answers ``OverflowError``. Here each
product is itself an infinity, one of each sign, and ``fsum`` takes a third exit nothing in this
module reached before: ``ValueError: -inf + inf in fsum``.
"""

MIXED_SIGN_QUERY: tuple[float, ...] = (1e300, -1e300, 0.0)
"""The query that turns :data:`MIXED_SIGN_OVERFLOW` into partials of both signs."""


def test_products_overflowing_with_opposite_signs_refuse_in_the_taxonomy(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """``fsum`` has three ways out, and the third one had no test.

    Every component here is finite and passes both ``validate_components`` and
    ``validate_query_components``, so nothing on the write or read path refuses it. Unguarded,
    the sum leaves a bare ``ValueError`` -- a non-``Grafx*`` escape from the section 8.8 door --
    and only from the STDLIB adapter: ``NumpyVectorMath`` refuses this case correctly, so one
    query would answer under ``[accel]`` and crash on the default.

    Both regimes are driven, because the guard sits under both.
    """
    for threshold in (4096, 0):
        database = VectorFixture(
            metrics=metrics, clock=clock, exact_scan_threshold=threshold
        )
        space = database.create_space(
            "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
        )
        table = database.create_table("Chunk", "space")
        database.insert_row(table, 1, 0, space, MIXED_SIGN_OVERFLOW, csn=10)
        with pytest.raises(GrafxVectorValidationError) as refusal:
            database.engine.search(
                space="space", query=MIXED_SIGN_QUERY, k=1, snapshot=SnapshotDouble(1000)
            )
        assert refusal.value.details["reason"] == "non_finite_result"


def test_a_length_that_overflows_is_not_a_vector_of_length_one(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The second ``fsum`` site, in the domain, where the adapter parity tests cannot reach it.

    A hundred components of ~1.34e154 each have a finite square; their exact sum does not fit a
    double. ``_require_unit_length`` computed it unguarded, so a call named verbatim in section
    8.8 -- and whose whole purpose is to refuse -- left a bare ``OverflowError`` instead.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=4096)
    space = database.create_space(
        "unit", 100, metric=DistanceMetric.COSINE, storage_dtype="float64", normalized=True
    )
    enormous = (1.3407807929942596e154,) * 100
    assert all(component * component != float("inf") for component in enormous)
    with pytest.raises(GrafxVectorValidationError) as refusal:
        database.engine.validate_vector(space, enormous)
    assert refusal.value.details["reason"] == "not_normalized"


def test_a_refusal_on_a_COLD_graph_leaves_nothing_cached(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The same rule as the test above, on the path that test cannot reach.

    That one calls ``index.graph()`` first, so the refusal always lands on a COMPLETE graph and
    is answered by the late guard inside ``_install``. The two EARLY refusals -- the resolver
    failing and a dimension mismatch -- happen while the graph is still being filled, and they
    used to re-raise with the partial graph published. Every later call took the
    ``self._graph is not None`` fast path and answered out of a fragment.

    LESSONS L24: the exact regime is unaffected, because the heap is the authority there, so the
    tested path was the safe one again. What is asserted is that the derived state was discarded,
    which is the only outcome that cannot disagree with the store.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    for record_id in range(1, 6):
        database.insert_row(table, record_id, 0, space, (float(record_id), 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    assert index._snapshot is None  # noqa: SLF001 - the graph must be COLD

    resolved = index._resolve  # noqa: SLF001
    calls = {"n": 0}

    def refuse_partway(ref: object) -> object:
        calls["n"] += 1
        if calls["n"] > 2:
            raise GrafxIndexError("the resolver could not read this row", index="space")
        return resolved(ref)

    index._resolve = refuse_partway  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(GrafxError):
        index.graph()
    index._resolve = resolved  # type: ignore[assignment]  # noqa: SLF001

    assert index._snapshot is None  # noqa: SLF001 - no picture, so no graph and no maps

    # The transient condition is over, so the very next search must answer completely rather than
    # out of the fragment the failed build left behind.
    result = database.engine.search(
        space="space", query=(3.0, 1.0, 1.0), k=5, snapshot=SnapshotDouble(1000)
    )
    assert result.regime == "approximate"
    assert result.achieved_k == 5


def test_a_refusal_on_a_WARM_graph_discards_it_too(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The twin of the COLD test, on the path the cold fix did not cover.

    ``graph()`` wrapped the cold build; ``_note()`` on every commit and redo reaches the same
    ``_install`` with the graph already warm, and its two early refusals -- the resolver failing,
    a dimension mismatch -- re-raised with the graph intact. A search then answered out of a
    graph missing the row for the rest of the session: five nodes, ``live_count`` six, ``stale``
    False, the exact regime answering six. Same symptom as B3, one path over.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    for record_id in range(1, 6):
        database.insert_row(table, record_id, 0, space, (float(record_id), 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    warm = database.engine.search(
        space="space", query=(3.0, 1.0, 1.0), k=5, snapshot=SnapshotDouble(1000)
    )
    assert warm.achieved_k == 5
    assert index._snapshot is not None  # noqa: SLF001 - the graph must be WARM

    resolved = index._resolve  # noqa: SLF001
    refusals = {"left": 1}

    def refuse_once(ref: object) -> object:
        if refusals["left"]:
            refusals["left"] -= 1
            raise GrafxIndexError("the resolver could not read this row", index="space")
        return resolved(ref)

    index._resolve = refuse_once  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(GrafxError):
        database.insert_row(table, 6, 0, space, (6.0, 1.0, 1.0), csn=11)
    index._resolve = resolved  # type: ignore[assignment]  # noqa: SLF001

    # The property, not the symptom: the derived state was discarded, so the next search
    # rebuilds from the store and answers every live row -- including the sixth, whose entry
    # the store holds whether or not the graph was ready to take it.
    assert index._snapshot is None  # noqa: SLF001
    after = database.engine.search(
        space="space", query=(3.0, 1.0, 1.0), k=10, snapshot=SnapshotDouble(1000)
    )
    assert after.regime == "approximate"
    assert after.achieved_k == index.live_count()


def test_a_search_finishes_on_the_picture_it_started_with_when_the_graph_is_discarded_under_it(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """Deterministic half of C9 round-3 B5: the maps are replaced, never edited, under a search.

    A commit on another thread discards and rebuilds the graph while a traversal runs. The
    traversal used to read ``self._entry_of_node[node]`` on every step, so the moment the maps
    were replaced it died with a bare ``KeyError`` out of the section 8.8 door. Here the host's
    own candidate filter -- code the traversal calls DURING expansion -- discards the graph on
    its first call, which is the same interleaving made deterministic. The search must finish,
    on the picture it started with.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    for record_id in range(1, 9):
        database.insert_row(table, record_id, 0, space, (float(record_id), 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    calls = {"n": 0}

    class DiscardingFilter:
        def admits(self, record_id: object) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                index.invalidate_graph()
            return True

        def cardinality(self) -> int:
            return 8

    result = database.engine.search(
        space="space",
        query=(4.0, 1.0, 1.0),
        k=8,
        snapshot=SnapshotDouble(1000),
        candidate_filter=DiscardingFilter(),
    )
    assert result.regime == "approximate"
    assert result.achieved_k == 8
    assert calls["n"] >= 1


def test_a_node_has_its_entry_before_the_graph_can_reach_it(
    metrics: RecordingMetrics, clock: StepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic half of C9 round-3 B5: the entry is registered BEFORE ``graph.insert``.

    ``graph.insert`` links the node into the connectivity chain; from that instant a traversal on
    another thread can reach it and ask for its entry. Registering after the insert left a window
    in which a reachable node had no entry -- a bare ``KeyError`` out of the section 8.8 door --
    and that window is the thread test's probabilistic catch. Here the graph class itself is
    instrumented: inside ``insert``, before linking, the entry must already be there.
    """
    from okto_grafx.domain.vector.hnsw import HnswGraph
    from okto_grafx.engine import vector_engine as engine_module

    observed: list[bool] = []
    owner: dict[str, object] = {}

    class ObservingGraph(HnswGraph):
        def insert(self, node: int, components: object) -> None:  # type: ignore[override]
            index = owner.get("index")
            if index is not None:
                picture = index._snapshot  # noqa: SLF001 - the ordering under test
                observed.append(picture is not None and node in picture.entry_of_node)
            return super().insert(node, components)

    monkeypatch.setattr(engine_module, "HnswGraph", ObservingGraph)
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0)
    space = database.create_space(
        "space", 3, metric=DistanceMetric.DOT, storage_dtype="float64"
    )
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 1.0, 1.0), csn=10)
    index = database.engine.index("space")
    index.graph()  # warm: the next insert goes through the live graph's insert
    owner["index"] = index
    database.insert_row(table, 2, 0, space, (2.0, 1.0, 1.0), csn=11)
    assert observed == [True], observed
