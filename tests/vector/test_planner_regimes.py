"""The two-regime planner and the boundary between the regimes (FR-5, BR-2, AC-5, AC-6, VTS-5, VTS-6).

The threshold is a decision with consequences, so it is pinned by its literal value and checked
against the composition root that also declares it. What matters more than the number is that
the number does not change the ANSWER: a query whose filtered set straddles the threshold must
return the same neighbours on either side, and that is the central test of this module.

The label and the achieved neighbour count are never omitted and never inferred: an approximate
result says so, and a search that could not reach the requested k says that too rather than
handing back a short list that looks complete (BR-2).
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.filter import RecordIdFilter, admits_everything
from okto_grafx.domain.vector.planner import (
    DEFAULT_EXACT_SCAN_THRESHOLD,
    REGIME_APPROXIMATE,
    REGIME_EXACT,
    REGIMES,
    plan_regime,
)
from okto_grafx.runtime.config import DatabaseConfig

from .conftest import (
    RecordingMetrics,
    SnapshotDouble,
    StepClock,
    VectorFixture,
    seeded_vectors,
)

DIMENSION: int = 12
CORPUS: int = 60


class DeclaringFilter:
    """A filter that admits everything and declares whatever cardinality a test needs.

    Separating the declaration from the membership is what makes the planner testable: the
    regime is chosen from the DECLARED number, and the rows returned are decided by ``admits``,
    so a test can move the plan across the threshold without changing the answer it expects.
    """

    def __init__(self, declared: int | None) -> None:
        self._declared = declared

    @property
    def cardinality(self) -> int | None:
        """Return the declared estimate, which may be None for a filter that cannot count."""
        return self._declared

    def admits(self, record_id: int) -> bool:
        """Admit every record."""
        return admits_everything(record_id)


def _corpus_database(
    metrics: RecordingMetrics, clock: StepClock, *, threshold: int, ef_search: int = 64
) -> tuple[VectorFixture, list[tuple[float, ...]]]:
    """Return a database holding a seeded corpus, with the regime threshold a test asks for."""
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=threshold, ef_search=ef_search
    )
    space = database.create_space("space", DIMENSION, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(CORPUS, DIMENSION, seed=0xBEEF)
    for index, values in enumerate(corpus):
        database.insert_row(table, index + 1, index % 4, space, values, csn=10 + index)
    return database, corpus


def _brute_force(
    database: VectorFixture, corpus: list[tuple[float, ...]], query, k: int, admitted=None
) -> list[int]:
    """Return the ground truth ranking, computed without the index taking part at all."""
    candidates = [
        (index + 1, values)
        for index, values in enumerate(corpus)
        if admitted is None or (index + 1) in admitted
    ]
    ranked = database.math.top_k(query, candidates, k, DistanceMetric.COSINE)
    return [identifier for identifier, _score in ranked]


# --- the threshold is a pinned decision ---------------------------------------------------


def test_the_default_threshold_is_the_literal_the_contract_freezes() -> None:
    """A56 and A68: the constant is asserted by value, never against an expression of itself."""
    assert DEFAULT_EXACT_SCAN_THRESHOLD == 4096


def test_the_threshold_agrees_with_the_composition_root() -> None:
    """Two declarations of one calibrated number must not drift apart (A24 in spirit)."""
    assert DatabaseConfig(path=":memory:").vector_exact_scan_threshold == (
        DEFAULT_EXACT_SCAN_THRESHOLD
    )


def test_the_two_regime_labels_are_the_bounded_domain_of_the_metric_label() -> None:
    """The label domain is closed, so a third regime would have to be declared before it exists."""
    assert REGIMES == {"exact", "approximate"} == {REGIME_EXACT, REGIME_APPROXIMATE}


# --- the planner in isolation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [(0, REGIME_EXACT), (1, REGIME_EXACT), (9, REGIME_EXACT), (10, REGIME_EXACT), (11, REGIME_APPROXIMATE)],
)
def test_the_regime_turns_over_exactly_at_the_threshold(estimate: int, expected: str) -> None:
    """At or below the threshold is exact; the first value above it is approximate."""
    plan = plan_regime(space_size=1000, filter_cardinality=estimate, threshold=10)
    assert plan.regime == expected


def test_a_filter_that_cannot_count_itself_is_estimated_at_the_size_of_the_space() -> None:
    """An unknown filter must not be able to talk the planner into believing it is selective."""
    plan = plan_regime(space_size=5000, filter_cardinality=None, threshold=4096)
    assert plan.estimate == 5000
    assert plan.regime == REGIME_APPROXIMATE


def test_a_filter_can_never_claim_to_be_larger_than_the_space() -> None:
    """The space is the upper bound of any subset of it, so an inflated claim is clamped."""
    plan = plan_regime(space_size=10, filter_cardinality=1_000_000, threshold=100)
    assert plan.estimate == 10
    assert plan.regime == REGIME_EXACT


def test_selectivity_of_an_empty_space_is_zero_and_not_a_division_by_zero() -> None:
    """The ratio histogram already reserves 0.0 for a filter that excluded everything."""
    plan = plan_regime(space_size=0, filter_cardinality=None, threshold=10)
    assert plan.selectivity == 0.0


def test_the_planner_refuses_a_negative_count() -> None:
    """A count below zero describes nothing and is a caller error."""
    with pytest.raises(GrafxConfigurationError):
        plan_regime(space_size=-1, filter_cardinality=None, threshold=10)


# --- the regimes end to end ----------------------------------------------------------------


def test_a_selective_filter_is_answered_by_an_exact_scan_with_perfect_recall(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """AC-5 and VTS-5: below the threshold the plan scans, and the answer is the ground truth."""
    database, corpus = _corpus_database(metrics, clock, threshold=10)
    admitted = frozenset(range(1, 9))
    result = database.engine.search(
        space="space",
        query=corpus[0],
        k=5,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of(admitted),
    )
    assert result.regime == REGIME_EXACT
    assert result.filter_cardinality == 8
    assert [hit.record_id for hit in result.hits] == _brute_force(
        database, corpus, corpus[0], 5, admitted
    )
    assert metrics.snapshot()["oktografx_vector_exact_fallback_total"] == 1.0


def test_a_broad_filter_is_answered_by_a_traversal_that_meets_the_recall_target(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """AC-6 and VTS-6: above the threshold the plan traverses, labelled, with measured recall."""
    database, corpus = _corpus_database(metrics, clock, threshold=4, ef_search=16)
    query = corpus[3]
    result = database.engine.search(
        space="space", query=query, k=10, snapshot=SnapshotDouble(1000)
    )
    assert result.regime == REGIME_APPROXIMATE
    truth = _brute_force(database, corpus, query, 10)
    recall = len(set(hit.record_id for hit in result.hits) & set(truth)) / len(truth)
    assert recall >= DatabaseConfig(path=":memory:").vector_recall_target
    assert result.achieved_k == 10


def test_the_two_regimes_return_the_same_answer_across_the_threshold(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The central property: which side of the threshold a query falls on cannot change it.

    The filter admits the same rows in both runs and declares a cardinality that puts the plan
    on one side and then the other. The beam is at least as wide as the corpus, so the traversal
    is exhaustive and must return exactly what the scan returns.
    """
    database, corpus = _corpus_database(metrics, clock, threshold=30, ef_search=CORPUS)
    query = corpus[11]
    snapshot = SnapshotDouble(1000)
    below = database.engine.search(
        space="space",
        query=query,
        k=7,
        snapshot=snapshot,
        candidate_filter=DeclaringFilter(30),
    )
    above = database.engine.search(
        space="space",
        query=query,
        k=7,
        snapshot=snapshot,
        candidate_filter=DeclaringFilter(31),
    )
    assert below.regime == REGIME_EXACT
    assert above.regime == REGIME_APPROXIMATE
    assert [hit.record_id for hit in below.hits] == [hit.record_id for hit in above.hits]
    assert [hit.score for hit in below.hits] == [hit.score for hit in above.hits]
    assert [hit.ref for hit in below.hits] == [hit.ref for hit in above.hits]
    assert below.hits[0].record_id == _brute_force(database, corpus, query, 1)[0]


@pytest.mark.parametrize("selective", [1, 5, 17, 40])
def test_the_regimes_agree_under_a_real_filter_too(
    metrics: RecordingMetrics, clock: StepClock, selective: int
) -> None:
    """The agreement is a property of the search, not of the unfiltered case."""
    database, corpus = _corpus_database(metrics, clock, threshold=CORPUS, ef_search=CORPUS)
    admitted = frozenset(range(1, selective + 1))
    query = corpus[5]
    snapshot = SnapshotDouble(1000)
    exact = database.engine.search(
        space="space",
        query=query,
        k=4,
        snapshot=snapshot,
        candidate_filter=RecordIdFilter.of(admitted),
    )
    assert exact.regime == REGIME_EXACT
    lowered = _corpus_database(metrics, clock, threshold=0, ef_search=CORPUS)[0]
    traversed = lowered.engine.search(
        space="space",
        query=query,
        k=4,
        snapshot=snapshot,
        candidate_filter=RecordIdFilter.of(admitted),
    )
    assert traversed.regime == REGIME_APPROXIMATE
    assert [hit.record_id for hit in exact.hits] == [hit.record_id for hit in traversed.hits]
    assert [hit.record_id for hit in exact.hits] == _brute_force(
        database, corpus, query, 4, admitted
    )


# --- the label and the count are never silent ----------------------------------------------


def test_a_search_that_cannot_reach_k_says_so_rather_than_padding(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """BR-2: under-k is visible in achieved_k beside the k that was asked for."""
    database, corpus = _corpus_database(metrics, clock, threshold=1000)
    result = database.engine.search(
        space="space",
        query=corpus[0],
        k=25,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of({1, 2, 3}),
    )
    assert result.requested_k == 25
    assert result.achieved_k == 3
    assert len(result.hits) == 3


def test_a_search_of_an_empty_space_returns_an_empty_labelled_result(
    database: VectorFixture,
) -> None:
    """Nothing to rank is still a result with a regime and a count, never an exception."""
    database.create_space("void", 3)
    database.create_table("Chunk", "void")
    result = database.engine.search(
        space="void", query=(1.0, 0.0, 0.0), k=5, snapshot=SnapshotDouble(1000)
    )
    assert result.hits == ()
    assert result.achieved_k == 0
    assert result.requested_k == 5
    assert result.regime in REGIMES


def test_the_result_names_the_space_it_answered_for(database: VectorFixture) -> None:
    """A caller holding two results must be able to tell them apart without bookkeeping."""
    space = database.create_space("named", 3)
    table = database.create_table("Chunk", "named")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    result = database.engine.search(
        space="named", query=(1.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert result.space == "named"


def test_the_filter_cardinality_is_absent_when_there_is_no_filter(
    database: VectorFixture,
) -> None:
    """None means "no filter narrowed this", which is different from "a filter admitted none"."""
    space = database.create_space("open", 3)
    table = database.create_table("Chunk", "open")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    result = database.engine.search(
        space="open", query=(1.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert result.filter_cardinality is None


@pytest.mark.parametrize("k", [0, -3])
def test_a_neighbour_count_below_one_is_refused(database: VectorFixture, k: int) -> None:
    """A request for no neighbours describes no result and is a caller error."""
    database.create_space("space", 3)
    with pytest.raises(GrafxConfigurationError) as failure:
        database.engine.search(
            space="space", query=(1.0, 0.0, 0.0), k=k, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["field"] == "k"


def test_a_snapshot_that_cannot_answer_visibility_is_refused(
    database: VectorFixture,
) -> None:
    """The visibility rule is taken structurally, so the argument must be able to answer it."""
    database.create_space("space", 3)
    with pytest.raises(GrafxConfigurationError) as failure:
        database.engine.search(
            space="space", query=(1.0, 0.0, 0.0), k=1, snapshot=object()
        )
    assert failure.value.details["field"] == "snapshot"


def test_a_search_of_a_space_the_catalog_does_not_know_is_refused(
    database: VectorFixture,
) -> None:
    """A name that names nothing is a configuration error rather than an empty result."""
    with pytest.raises(GrafxConfigurationError):
        database.engine.search(
            space="absent", query=(1.0,), k=1, snapshot=SnapshotDouble(1000)
        )


def test_the_size_of_the_space_is_the_work_the_plan_would_do(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The estimate describes the scan, not the view the snapshot happens to have.

    An exact scan reads every entry of the index and discards the ones the snapshot cannot see,
    so the work is the size of the index. Estimating from the snapshot's view instead would put
    a query that must read sixty rows into the regime meant for six -- and it would cost a pass
    over the whole index on every query to find that out.
    """
    database, corpus = _corpus_database(metrics, clock, threshold=CORPUS // 2)
    old_reader = SnapshotDouble(12)
    assert database.engine.index("space").visible_count(old_reader) < CORPUS // 2
    assert database.engine.index("space").live_count() > CORPUS // 2
    result = database.engine.search(
        space="space", query=corpus[0], k=3, snapshot=old_reader
    )
    assert result.regime == REGIME_APPROXIMATE
