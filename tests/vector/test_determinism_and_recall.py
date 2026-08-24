"""Determinism and measured recall (CONTRACT 13.4, SPEC-VEC AC-6, BR-8).

Two claims that are only worth anything when measured.

**Determinism.** Nothing in this component may depend on the iteration order of a set, on the
address of an object or on a clock. The tests here run the same work several times and over
freshly built engines, and require the results to be identical -- scores included, because a
ranking that agreed on identifiers while the scores moved would still be a moving answer.

**Recall.** The approximate regime is allowed to be approximate, and the calibrated target says
how approximate. It is measured against brute force over a corpus large enough that the beam is
far narrower than the corpus, so a graph that degraded would show here rather than being hidden
by a beam wide enough to visit everything.
"""

from __future__ import annotations

import pytest

from bench.harness.gate import DEFAULT_RECALL_TARGET
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.vector.hnsw import HnswGraph
from okto_grafx.domain.vector.planner import REGIME_APPROXIMATE

from .conftest import (
    RecordingMetrics,
    TransactionDouble,
    SnapshotDouble,
    StepClock,
    VectorFixture,
    seeded_vectors,
)

RECALL_CORPUS: int = 250
RECALL_DIMENSION: int = 16
RECALL_EF_CONSTRUCTION: int = 64
RECALL_EF_SEARCH: int = 32
RECALL_K: int = 10


def _recall_graph(corpus: list[tuple[float, ...]]) -> HnswGraph:
    """Return a graph built over one corpus with the beam widths this module measures at."""
    graph = HnswGraph(
        PureVectorMath(),
        DistanceMetric.COSINE,
        seed=0x51DE,
        ef_construction=RECALL_EF_CONSTRUCTION,
    )
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
    return graph


def _measure_recall(
    graph: HnswGraph, corpus: list[tuple[float, ...]], live: set[int], queries: range
) -> float:
    """Return recall at k over a set of queries, against brute force on the live rows."""
    math = PureVectorMath()
    candidates = [
        (index + 1, values) for index, values in enumerate(corpus) if (index + 1) in live
    ]
    found = 0
    expected = 0
    for position in queries:
        query = corpus[position]
        ranked, _stats = graph.search(query, RECALL_EF_SEARCH)
        got = {node for _score, node in ranked[:RECALL_K]}
        truth = {
            identifier
            for identifier, _score in math.top_k(
                query, candidates, RECALL_K, DistanceMetric.COSINE
            )
        }
        found += len(got & truth)
        expected += len(truth)
    return found / expected


# --- recall ------------------------------------------------------------------------------------


@pytest.mark.slow
def test_the_traversal_meets_the_calibrated_recall_target(
    metrics: RecordingMetrics,
) -> None:
    """AC-6 and BR-8: recall is measured against ground truth, not asserted."""
    corpus = seeded_vectors(RECALL_CORPUS, RECALL_DIMENSION, seed=0xBEEF)
    graph = _recall_graph(corpus)
    recall = _measure_recall(
        graph, corpus, set(range(1, RECALL_CORPUS + 1)), range(0, RECALL_CORPUS, 10)
    )
    assert recall >= DEFAULT_RECALL_TARGET, recall
    assert metrics is not None


@pytest.mark.slow
def test_the_beam_is_far_narrower_than_the_corpus_so_the_measurement_is_real() -> None:
    """A recall of one means nothing if the traversal visited everything; this shows it did not.

    Amendment A72: the fixture has to be proved to be the state it claims. A beam as wide as the
    corpus would make the previous test tautological, so the traversal is asserted to be a
    genuine approximation before its recall is trusted.
    """
    corpus = seeded_vectors(RECALL_CORPUS, RECALL_DIMENSION, seed=0xBEEF)
    graph = _recall_graph(corpus)
    _ranked, stats = graph.search(corpus[0], RECALL_EF_SEARCH)
    assert not stats.exhaustive
    assert stats.visited < RECALL_CORPUS


@pytest.mark.slow
def test_recall_survives_heavy_reconciliation() -> None:
    """A graph that decayed into its chain under churn would fail here rather than silently."""
    corpus = seeded_vectors(RECALL_CORPUS, RECALL_DIMENSION, seed=0xF00D)
    graph = _recall_graph(corpus)
    removed = {node for node in range(1, RECALL_CORPUS + 1) if node % 3 == 0}
    for node in sorted(removed):
        graph.remove(node)
    assert graph.is_connected_at_layer_zero()
    live = set(range(1, RECALL_CORPUS + 1)) - removed
    recall = _measure_recall(graph, corpus, live, range(0, RECALL_CORPUS, 10))
    assert recall >= DEFAULT_RECALL_TARGET, recall


# --- determinism ---------------------------------------------------------------------------------


def _search_signature(
    metrics: RecordingMetrics, clock: StepClock, *, threshold: int
) -> list[tuple[int, float, int, int]]:
    """Return a fully built database's answer to a fixed query, as comparable values."""
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=threshold, ef_search=24
    )
    space = database.create_space("space", 12, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(60, 12, seed=0x1CE)
    for index, values in enumerate(corpus):
        database.insert_row(table, index + 1, index % 3, space, values, csn=10 + index)
    result = database.engine.search(
        space="space",
        query=corpus[7],
        k=12,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of(range(1, 55)),
    )
    return [(hit.record_id, hit.score, hit.ref.page, hit.ref.slot) for hit in result.hits]


@pytest.mark.parametrize("threshold", [1000, 0])
def test_a_freshly_built_database_answers_identically_every_time(
    metrics: RecordingMetrics, clock: StepClock, threshold: int
) -> None:
    """Five independent builds, one answer, in both regimes (CONTRACT 13.4)."""
    first = _search_signature(metrics, clock, threshold=threshold)
    assert first
    for _repeat in range(4):
        assert _search_signature(metrics, clock, threshold=threshold) == first


@pytest.mark.parametrize("threshold", [1000, 0])
def test_repeating_one_search_returns_the_same_answer(
    metrics: RecordingMetrics, clock: StepClock, threshold: int
) -> None:
    """The engine holds no state that a search mutates, so repetition cannot drift."""
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=threshold, ef_search=24
    )
    space = database.create_space("space", 10, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(40, 10, seed=0x2222)
    for index, values in enumerate(corpus):
        database.insert_row(table, index + 1, 0, space, values, csn=10 + index)
    snapshot = SnapshotDouble(1000)
    first = database.engine.search(space="space", query=corpus[2], k=8, snapshot=snapshot)
    for _repeat in range(5):
        again = database.engine.search(space="space", query=corpus[2], k=8, snapshot=snapshot)
        assert again.hits == first.hits
        assert again.regime == first.regime
        assert again.achieved_k == first.achieved_k


def test_the_exact_regime_does_not_depend_on_the_order_rows_were_inserted(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The exact answer is a property of the data; the traversal order is not part of it."""
    corpus = seeded_vectors(30, 8, seed=0x3333)
    answers = []
    for order in (range(30), reversed(range(30))):
        database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
        space = database.create_space("space", 8, storage_dtype="float64")
        table = database.create_table("Chunk", "space")
        for position, index in enumerate(order):
            database.insert_row(
                table, index + 1, 0, space, corpus[index], csn=10 + position
            )
        result = database.engine.search(
            space="space", query=corpus[4], k=6, snapshot=SnapshotDouble(1000)
        )
        answers.append([(hit.record_id, hit.score) for hit in result.hits])
    assert answers[0] == answers[1]


def test_the_regime_of_a_search_does_not_depend_on_anything_but_the_numbers(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The plan is a function of two counts and a threshold, so it cannot vary between runs."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=5)
    space = database.create_space("space", 6, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(20, 6, seed=0x4444)
    for index, values in enumerate(corpus):
        database.insert_row(table, index + 1, 0, space, values, csn=10 + index)
    regimes = {
        database.engine.search(
            space="space", query=corpus[0], k=3, snapshot=SnapshotDouble(1000)
        ).regime
        for _repeat in range(5)
    }
    assert regimes == {REGIME_APPROXIMATE}


def test_an_index_replayed_from_its_own_records_answers_identically(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """Replay is the recovery path, so the index it produces must be the index that was lost.

    The records are the ones the store staged into a transaction, which is exactly what the log
    would carry. Feeding them to a second, empty index of the same definition has to produce the
    same entries and the same ranking -- the property that lets a rebuilt index be trusted.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=24)
    space = database.create_space("space", 8, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(35, 8, seed=0x5555)
    records = []
    for index, values in enumerate(corpus):
        ref = database.heap.insert(
            table, index + 1, (index + 1, 0, _vector(database, space, values)), 10 + index
        )
        txn = TransactionDouble()
        records.append(
            database.engine.stage_insert("space", index + 1, ref, values, 10 + index, txn)
        )
        database.engine.commit("space", txn, 10 + index)
    snapshot = SnapshotDouble(1000)
    original = database.engine.search(space="space", query=corpus[3], k=8, snapshot=snapshot)

    replayed = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=24)
    replayed.catalog_store.catalog.add_space(space)
    replayed.catalog_store.save()
    replayed.catalog_store.catalog.add_table(table)
    replayed.catalog_store.save()
    for index, values in enumerate(corpus):
        replayed.heap.insert(
            table, index + 1, (index + 1, 0, _vector(replayed, space, values)), 10 + index
        )
    rebuilt = replayed.engine.attach(table, "space")
    for record in records:
        rebuilt.apply(record)
    assert len(rebuilt.walk()) == len(database.engine.index("space").walk())
    scored, _stats = rebuilt.search(corpus[3], 8, snapshot)
    assert [item.record_id for item in scored] == [hit.record_id for hit in original.hits]


def _vector(database: VectorFixture, space, values):
    """Return the storable vector value of these components in one space."""
    from okto_grafx.domain.model.value import VectorValue

    return VectorValue(
        database.engine.validate_vector(space, values), space.space_id, space.storage_dtype
    )
