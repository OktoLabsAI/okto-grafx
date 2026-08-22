"""What the vector subsystem publishes, and what it survives (OR-1, OR-2, FR-10, G7, A91).

Every number this component emits comes from the frozen catalogue of CONTRACT.md section 9 by
lookup, so a metric it could not name does not exist. The tests here assert three separable
things: that the names and labels are catalogue names and catalogue label values; that the
numbers say what happened; and that a host sink which fails cannot fail the operation it was
watching, nor leave this component as a foreign exception type.

``oktografx_vector_recall_ratio`` is deliberately NOT emitted here. Recall is a measurement
against ground truth, which the engine does not have at query time, and FR-8 assigns its
publication to the calibration harness. Emitting 1.0 from the exact regime would fill the gauge
the continuous integration gate reads with a number nobody measured.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxIndexError
from okto_grafx.engine.metrics_catalog import metric, metric_names
from okto_grafx.engine.vector_engine import (
    PHASE_PLAN,
    PHASE_TRAVERSE,
    PHASE_VALIDATE,
    VectorEngine,
)
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.vector.planner import REGIMES

from .conftest import (
    BackwardClock,
    RecordingEvents,
    TransactionDouble,
    RecordingMetrics,
    SilentMetrics,
    SnapshotDouble,
    StepClock,
    VectorFixture,
    seeded_vectors,
)

EMITTED: tuple[str, ...] = (
    "oktografx_vector_query_latency_seconds",
    "oktografx_vector_exact_fallback_total",
    "oktografx_vector_achieved_k",
    "oktografx_vector_filter_selectivity_ratio",
    "oktografx_vector_tombstone_backlog",
    "oktografx_vector_reconciliation_total",
    "oktografx_vector_index_entries",
    "oktografx_vector_space_retired_total",
    "oktografx_vector_space_coverage_ratio",
    "oktografx_vector_index_age_seconds",
)
"""Every metric this component publishes, which must all be catalogue names."""


def _busy(metrics: object, clock: object, *, threshold: int = 1000) -> VectorFixture:
    """Return a database that has exercised every publishing path once."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=threshold)
    space = database.create_space("space", 6)
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(12, 6, seed=0x1111)
    refs = [
        database.insert_row(table, index + 1, index % 2, space, values, csn=10 + index)
        for index, values in enumerate(corpus)
    ]
    database.delete_row(table, refs[0], 1, space, corpus[0], csn=100)
    database.engine.reconcile("space", 100)
    database.engine.search(
        space="space", query=corpus[1], k=3, snapshot=SnapshotDouble(1000)
    )
    return database


def test_every_metric_this_component_emits_is_a_catalogue_name() -> None:
    """A metric name is a contract; inventing one is not available to this component."""
    for name in EMITTED:
        assert metric(name).name == name
    assert set(EMITTED) <= metric_names()


def test_a_name_outside_the_catalogue_cannot_be_looked_up() -> None:
    """The lookup is what makes the previous test meaningful rather than a tautology."""
    with pytest.raises(GrafxConfigurationError):
        metric("oktografx_vector_invented_total")


def test_the_engine_publishes_only_catalogue_names(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """Anything published under a name the catalogue does not hold would be caught here."""
    _busy(metrics, clock)
    published = {emission.name for emission in metrics.emissions}
    vector_names = {name for name in published if name.startswith("oktografx_vector_")}
    assert vector_names <= set(EMITTED)
    assert vector_names, "the fixture published no vector metric at all"


def test_the_recall_gauge_is_left_to_the_calibration_harness(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A gauge filled with an unmeasured 1.0 would make the recall gate meaningless."""
    _busy(metrics, clock)
    assert metrics.values_of("oktografx_vector_recall_ratio") == []


@pytest.mark.parametrize("phase", [PHASE_PLAN, PHASE_TRAVERSE, PHASE_VALIDATE])
def test_every_phase_of_a_search_is_timed_and_labelled(
    metrics: RecordingMetrics, clock: StepClock, phase: str
) -> None:
    """FR-10: latency is published per phase, so a regression names where it happened."""
    _busy(metrics, clock)
    latencies = metrics.values_of("oktografx_vector_query_latency_seconds")
    phases = {emission.labels["phase"] for emission in latencies}
    assert phase in phases
    assert all(emission.value >= 0.0 for emission in latencies)


def test_the_regime_label_of_every_latency_is_one_of_the_two(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The label domain is bounded at registration, so an unbounded value would be refused."""
    _busy(metrics, clock)
    latencies = metrics.values_of("oktografx_vector_query_latency_seconds")
    assert {emission.labels["regime"] for emission in latencies} <= REGIMES


def test_the_phase_labels_are_the_ones_the_descriptor_declares(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A label value outside the declared set is what the bounded domain rule exists to stop."""
    descriptor = metric("oktografx_vector_query_latency_seconds")
    declared = {
        label.name: label.allowed_values for label in descriptor.labels
    }
    _busy(metrics, clock)
    for emission in metrics.values_of("oktografx_vector_query_latency_seconds"):
        assert emission.labels["phase"] in declared["phase"]
        assert emission.labels["regime"] in declared["regime"]


def test_the_exact_fallback_counter_moves_only_for_the_exact_regime(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The counter is how an operator sees how often the planner chose to scan."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    database.engine.search(
        space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert metrics.snapshot()["oktografx_vector_exact_fallback_total"] == 1.0
    lowered = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0)
    other = lowered.create_space("other", 4)
    other_table = lowered.create_table("Chunk", "other")
    lowered.insert_row(other_table, 1, 0, other, (1.0, 0.0, 0.0, 0.0), csn=10)
    lowered.engine.search(
        space="other", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert metrics.snapshot()["oktografx_vector_exact_fallback_total"] == 1.0


def test_the_achieved_neighbour_count_is_published(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """OR-1 names achieved_k, and it is the number a caller would otherwise have to infer."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    for index in range(3):
        database.insert_row(table, index + 1, 0, space, (float(index), 1.0, 0.0, 0.0), csn=10 + index)
    database.engine.search(
        space="space", query=(0.0, 1.0, 0.0, 0.0), k=10, snapshot=SnapshotDouble(1000)
    )
    assert metrics.values_of("oktografx_vector_achieved_k")[-1].value == 3.0


def test_the_filter_selectivity_is_published_as_a_fraction_of_the_space(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """Selectivity is the number the two-regime decision was taken on, so it is published."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    for index in range(10):
        database.insert_row(table, index + 1, 0, space, (float(index), 1.0, 0.0, 0.0), csn=10 + index)
    database.engine.search(
        space="space",
        query=(0.0, 1.0, 0.0, 0.0),
        k=2,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of({1, 2, 3, 4}),
    )
    assert metrics.values_of("oktografx_vector_filter_selectivity_ratio")[-1].value == 0.4


def test_the_index_age_is_published_per_space(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """OR-2 names the age of an index, which only a clock port can answer."""
    _busy(metrics, clock)
    ages = metrics.values_of("oktografx_vector_index_age_seconds")
    assert ages
    assert all(emission.labels == {"space": "space"} for emission in ages)
    assert all(emission.value >= 0.0 for emission in ages)


def test_the_space_label_carries_the_catalog_name_and_nothing_else(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """TR-7: a label value is a bounded, non-identifying token, never a path or free text."""
    _busy(metrics, clock)
    for name in (
        "oktografx_vector_index_entries",
        "oktografx_vector_space_coverage_ratio",
        "oktografx_vector_index_age_seconds",
    ):
        for emission in metrics.values_of(name):
            assert set(emission.labels) == {"space"}
            assert emission.labels["space"] == "space"


# --- the disabled sink -------------------------------------------------------------------------


def test_nothing_is_published_when_the_sink_collects_nothing(clock: StepClock) -> None:
    """A hot path guards on the flag, so a no-op sink is never asked to do anything."""
    silent = SilentMetrics()
    database = VectorFixture(metrics=silent, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    silent.calls.clear()
    database.engine.search(
        space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert silent.calls == []


def test_the_clock_is_not_read_at_all_when_the_sink_collects_nothing(
    clock: StepClock,
) -> None:
    """The reading exists for the metric; without a metric there is nothing to read it for."""
    silent = SilentMetrics()
    database = VectorFixture(metrics=silent, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    before = clock.reads
    database.engine.search(
        space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert clock.reads == before


# --- host code that fails ----------------------------------------------------------------------


class BrokenMetrics(RecordingMetrics):
    """A metrics sink that fails the way a host sink can, in the middle of an operation."""

    def observe(self, name: str, value: float, labels: object = None) -> None:
        """Fail with an ordinary exception, which is what a broken host sink does."""
        raise RuntimeError("the scrape endpoint is gone")


class BrokenEvents:
    """An event sink that fails on every notice."""

    def emit(self, event: str, payload: object) -> None:
        """Fail with an ordinary exception."""
        raise RuntimeError("the log destination is gone")


class BrokenFilter:
    """A candidate filter whose predicate fails, which decides the result rather than reports it."""

    @property
    def cardinality(self) -> int:
        """Return a cardinality that puts the plan in the exact regime."""
        return 1

    def admits(self, record_id: int) -> bool:
        """Fail with an ordinary exception."""
        raise RuntimeError("the plan node is gone")


def test_a_metrics_sink_that_fails_does_not_fail_the_search(clock: StepClock) -> None:
    """A correct answer must not be lost because nobody could be told the search happened."""
    database = VectorFixture(
        metrics=BrokenMetrics(), clock=clock, exact_scan_threshold=1000
    )
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    result = database.engine.search(
        space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k == 1


def test_an_event_sink_that_fails_does_not_undo_the_operation_it_watched(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A91 at this boundary: the notice is the last step, and it runs with no lock held."""
    database = VectorFixture(
        metrics=metrics, clock=clock, events=BrokenEvents(), exact_scan_threshold=1000
    )
    database.create_space("space", 4)
    assert database.engine.space("space").name == "space"
    database.engine.retire_space("space")
    assert database.engine.space("space").state == "retired"


def test_a_candidate_filter_that_fails_is_reported_in_the_taxonomy(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A predicate decides the result, so its failure is translated rather than dropped."""
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.search(
            space="space",
            query=(1.0, 0.0, 0.0, 0.0),
            k=1,
            snapshot=SnapshotDouble(1000),
            candidate_filter=BrokenFilter(),
        )
    assert failure.value.details["field"] == "candidate_filter"
    assert isinstance(failure.value.__cause__, RuntimeError)


def test_a_filter_whose_estimate_fails_is_treated_as_unknown(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """An estimate that cannot be obtained is an unknown estimate, which the planner handles."""

    class UnreadableCardinality:
        """A filter whose cardinality property fails and whose predicate works."""

        @property
        def cardinality(self) -> int:
            """Fail with an ordinary exception."""
            raise RuntimeError("the estimator is gone")

        def admits(self, record_id: int) -> bool:
            """Admit everything."""
            return True

    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    result = database.engine.search(
        space="space",
        query=(1.0, 0.0, 0.0, 0.0),
        k=1,
        snapshot=SnapshotDouble(1000),
        candidate_filter=UnreadableCardinality(),
    )
    assert result.achieved_k == 1
    assert result.filter_cardinality is None


def test_an_invalid_emission_of_this_component_stays_loud(clock: StepClock) -> None:
    """The publication guard drops a HOST failure and never this component's own defect."""

    class StrictMetrics(RecordingMetrics):
        """A sink that refuses a metric outside the frozen catalogue, as C8's sink does."""

        def observe(self, name: str, value: float, labels: object = None) -> None:
            """Refuse every emission with a Grafx error, which must not be swallowed."""
            raise GrafxConfigurationError(
                f"Metric {name!r} is not registered.", field="name", value=name
            )

    database = VectorFixture(metrics=StrictMetrics(), clock=clock, exact_scan_threshold=1000)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    with pytest.raises(GrafxConfigurationError):
        database.engine.search(
            space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
        )


def test_the_engine_holds_no_lock_and_starts_no_thread() -> None:
    """A91 cannot arise where there is no lock; the surface is enumerated to show there is none."""
    source = VectorEngine.__module__
    assert source == "okto_grafx.engine.vector_engine"
    assert "threading" not in VectorEngine.__init__.__code__.co_names
    assert not any(name.startswith("_lock") for name in VectorEngine.__slots__)


def test_the_lifecycle_notices_reach_a_working_sink(
    metrics: RecordingMetrics, clock: StepClock, events: RecordingEvents
) -> None:
    """The guard drops a failure, so a test must also show a working sink still hears."""
    database = VectorFixture(
        metrics=metrics, clock=clock, events=events, exact_scan_threshold=1000
    )
    database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    space = database.engine.space("space")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    database.delete_row(table, ref, 1, space, (1.0, 0.0, 0.0, 0.0), csn=20)
    txn = TransactionDouble()
    database.engine.reconcile("space", 100, txn)
    database.engine.commit("space", txn, 110)
    assert events.names() == ["vector.space_created", "vector.index_reconciled"]
    assert events.events[1][1]["removed"] == 1
    assert events.events[1][1]["horizon"] == 100


def test_a_clock_that_goes_backwards_never_produces_a_negative_latency(
    metrics: RecordingMetrics,
) -> None:
    """The port promises a monotonic reading; a component may not publish a negative duration."""
    database = VectorFixture(
        metrics=metrics, clock=BackwardClock(), exact_scan_threshold=1000
    )
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    database.engine.search(
        space="space", query=(1.0, 0.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    latencies = metrics.values_of("oktografx_vector_query_latency_seconds")
    assert latencies
    assert all(emission.value == 0.0 for emission in latencies)


def test_an_index_age_is_never_negative_under_a_clock_that_goes_backwards(
    metrics: RecordingMetrics,
) -> None:
    """The same promise, on the gauge that subtracts two readings taken far apart."""
    database = VectorFixture(
        metrics=metrics, clock=BackwardClock(), exact_scan_threshold=1000
    )
    space = database.create_space("space", 4)
    database.create_table("Chunk", "space")
    assert space.name == "space"
    ages = metrics.values_of("oktografx_vector_index_age_seconds")
    assert ages
    assert all(emission.value >= 0.0 for emission in ages)
