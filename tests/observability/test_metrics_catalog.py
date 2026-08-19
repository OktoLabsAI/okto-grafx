"""The frozen metric catalog against CONTRACT.md section 9 (SPEC-M1 FR-14, TR-7, BR-12, AC-14).

The expected set of names is written out by hand below rather than derived from the catalog, so
that adding, renaming or losing a metric fails here instead of quietly changing what the engine
promises. A second gate reads section 9 of the contract itself, which catches the opposite drift:
a metric the contract requires and the code never declared.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from okto_grafx import errors as public_errors
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import (
    FORBIDDEN_LABEL_NAMES,
    METRIC_NAME_PREFIX,
    METRIC_UNIT_SUFFIXES,
    UNBOUNDED_LABEL_CARDINALITY_LIMIT,
    MetricDescriptor,
    MetricKind,
    MetricsSink,
)
from okto_grafx.engine.metrics_catalog import (
    LATENCY_BUCKETS_SECONDS,
    METRIC_CATALOG,
    NEIGHBOR_COUNT_BUCKETS,
    RATIO_BUCKETS,
    ROW_COUNT_BUCKETS,
    MetricEmitter,
    metric,
    metric_names,
    register_catalog,
)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONTRACT: Path = PROJECT_ROOT / "docs" / "architecture" / "CONTRACT.md"

EXPECTED_METRIC_NAMES: frozenset[str] = frozenset(
    {
        # SPEC-M1 OR-1
        "oktografx_lease_wait_seconds",
        "oktografx_write_conflicts_total",
        "oktografx_commit_retries_total",
        "oktografx_active_transactions",
        # SPEC-M1 OR-2
        "oktografx_fsync_duration_seconds",
        "oktografx_barrier_failures_total",
        "oktografx_wal_size_bytes",
        "oktografx_wal_segments",
        "oktografx_wal_truncation_lag_segments",
        # SPEC-M1 OR-3
        "oktografx_checksum_verifications_total",
        "oktografx_checksum_failures_total",
        "oktografx_recovery_replays_total",
        "oktografx_recovery_discarded_records_total",
        "oktografx_ledger_depth",
        "oktografx_ledger_oldest_entry_age_seconds",
        "oktografx_quarantine_entries",
        # SPEC-M1 OR-4
        "oktografx_buffer_budget_used_bytes",
        "oktografx_buffer_budget_exceeded_total",
        "oktografx_database_opens_total",
        "oktografx_recoveries_total",
        "oktografx_baseline_ceiling_multiple",
        # SPEC-VEC OR-1 and OR-2, with the names of amendment A1
        "oktografx_vector_recall_ratio",
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
        # Query surface
        "oktografx_query_phase_duration_seconds",
        "oktografx_query_rows_returned_count",
        "oktografx_query_errors_total",
    }
)
"""Transcribed from CONTRACT.md section 9 with the renames of amendment A1 applied."""

EXPECTED_LABELS: dict[str, tuple[str, ...]] = {
    "oktografx_lease_wait_seconds": ("outcome",),
    "oktografx_active_transactions": ("mode",),
    "oktografx_fsync_duration_seconds": ("target",),
    "oktografx_wal_truncation_lag_segments": ("reader_present",),
    "oktografx_checksum_verifications_total": ("kind",),
    "oktografx_checksum_failures_total": ("kind",),
    "oktografx_recovery_discarded_records_total": ("origin_class",),
    "oktografx_ledger_depth": ("origin_class",),
    "oktografx_ledger_oldest_entry_age_seconds": ("origin_class",),
    "oktografx_buffer_budget_used_bytes": ("db",),
    "oktografx_buffer_budget_exceeded_total": ("db",),
    "oktografx_recoveries_total": ("outcome",),
    "oktografx_baseline_ceiling_multiple": ("ceiling",),
    "oktografx_vector_query_latency_seconds": ("regime", "phase"),
    "oktografx_vector_index_entries": ("space",),
    "oktografx_vector_space_coverage_ratio": ("space",),
    "oktografx_vector_index_age_seconds": ("space",),
    "oktografx_query_phase_duration_seconds": ("phase",),
    "oktografx_query_errors_total": ("code",),
}
"""The labels CONTRACT.md section 9 attaches to a metric; every other metric carries none."""


class _RecordingSink:
    """A sink that remembers what it was asked to register, without any aggregation."""

    def __init__(self) -> None:
        self.registrations: list[MetricDescriptor] = []

    @property
    def enabled(self) -> bool:
        return True

    def register(self, descriptor: MetricDescriptor) -> None:
        self.registrations.append(descriptor)

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        return None

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        return None

    def observe(self, name: str, value: float, labels: object = None) -> None:
        return None

    def time(self, name: str, labels: object = None) -> object:
        return None

    def snapshot(self) -> dict[str, object]:
        return {}


def _contract_section_nine() -> str:
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index("## 9. Metric catalog")
    end = text.index("## 10.", start)
    return text[start:end]


def test_the_catalog_holds_exactly_the_metrics_the_contract_freezes() -> None:
    names = metric_names()
    assert names == EXPECTED_METRIC_NAMES, {
        "missing": sorted(EXPECTED_METRIC_NAMES - names),
        "unexpected": sorted(names - EXPECTED_METRIC_NAMES),
    }
    assert len(METRIC_CATALOG) == len(EXPECTED_METRIC_NAMES) == 35


def test_the_catalog_matches_section_nine_of_the_contract_itself() -> None:
    # The opposite drift: a metric the contract requires that the code never declared.
    declared = set(re.findall(r"oktografx_[a-z0-9_]+", _contract_section_nine()))
    assert declared == metric_names(), {
        "in the contract only": sorted(declared - metric_names()),
        "in the catalog only": sorted(metric_names() - declared),
    }


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_every_descriptor_is_a_validated_metric_descriptor(descriptor: MetricDescriptor) -> None:
    assert isinstance(descriptor, MetricDescriptor)
    assert descriptor.name.startswith(METRIC_NAME_PREFIX)
    assert any(descriptor.name.endswith(suffix) for suffix in METRIC_UNIT_SUFFIXES)
    assert isinstance(descriptor.kind, MetricKind)
    # Rebuilding it re-runs the whole frozen validator, which is what makes this a real check.
    assert MetricDescriptor(
        name=descriptor.name,
        kind=descriptor.kind,
        description=descriptor.description,
        unit=descriptor.unit,
        labels=descriptor.labels,
        buckets=descriptor.buckets,
    ) == descriptor


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_every_description_is_an_en_us_sentence(descriptor: MetricDescriptor) -> None:
    description = descriptor.description
    assert description.isascii()
    assert description[0].isupper()
    assert description.endswith(".")
    assert len(description.split()) >= 3


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_every_label_declares_a_bounded_domain(descriptor: MetricDescriptor) -> None:
    for label in descriptor.labels:
        assert label.name not in FORBIDDEN_LABEL_NAMES
        if label.allowed_values is None:
            assert label.max_cardinality <= UNBOUNDED_LABEL_CARDINALITY_LIMIT
            assert label.name in {"db", "space"}, (
                f"{descriptor.name} leaves the label {label.name!r} unenumerated"
            )
        else:
            assert label.allowed_values
            assert len(label.allowed_values) <= label.max_cardinality


@pytest.mark.parametrize(
    ("name", "labels"), sorted(EXPECTED_LABELS.items()), ids=sorted(EXPECTED_LABELS)
)
def test_the_labelled_metrics_carry_the_labels_of_the_contract(
    name: str, labels: tuple[str, ...]
) -> None:
    assert tuple(label.name for label in metric(name).labels) == labels


def test_no_other_metric_carries_a_label() -> None:
    unlabelled = {
        descriptor.name for descriptor in METRIC_CATALOG if not descriptor.labels
    }
    assert unlabelled == metric_names() - set(EXPECTED_LABELS)


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_buckets_belong_to_histograms_and_are_strictly_increasing(
    descriptor: MetricDescriptor,
) -> None:
    if descriptor.kind is not MetricKind.HISTOGRAM:
        assert descriptor.buckets == ()
        return
    assert descriptor.buckets
    assert list(descriptor.buckets) == sorted(set(descriptor.buckets))
    assert descriptor.buckets in {
        LATENCY_BUCKETS_SECONDS,
        RATIO_BUCKETS,
        NEIGHBOR_COUNT_BUCKETS,
        ROW_COUNT_BUCKETS,
    }


def test_the_latency_ladder_spans_microseconds_to_half_a_minute() -> None:
    assert LATENCY_BUCKETS_SECONDS[0] == pytest.approx(0.00005)
    assert LATENCY_BUCKETS_SECONDS[-1] == pytest.approx(30.0)
    ratios = [
        LATENCY_BUCKETS_SECONDS[index + 1] / LATENCY_BUCKETS_SECONDS[index]
        for index in range(len(LATENCY_BUCKETS_SECONDS) - 1)
    ]
    assert max(ratios) <= 3.0


def test_the_ratio_ladder_covers_the_closed_unit_interval() -> None:
    assert RATIO_BUCKETS[0] == 0.0
    assert RATIO_BUCKETS[-1] == 1.0
    assert 0.9 in RATIO_BUCKETS and 0.95 in RATIO_BUCKETS and 0.99 in RATIO_BUCKETS


def test_the_neighbour_and_row_ladders_are_small_integers() -> None:
    assert all(float(edge).is_integer() for edge in NEIGHBOR_COUNT_BUCKETS)
    assert all(float(edge).is_integer() for edge in ROW_COUNT_BUCKETS)
    assert NEIGHBOR_COUNT_BUCKETS[0] == 1.0
    assert ROW_COUNT_BUCKETS[0] == 0.0


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_the_unit_agrees_with_the_name_suffix(descriptor: MetricDescriptor) -> None:
    if descriptor.name.endswith("_seconds"):
        assert descriptor.unit == "seconds"
    elif descriptor.name.endswith("_bytes"):
        assert descriptor.unit == "bytes"
    elif descriptor.name.endswith("_ratio"):
        assert descriptor.unit == "ratio"
    elif descriptor.name.endswith("_total"):
        assert descriptor.unit == ""


def test_the_error_code_label_covers_every_code_of_the_taxonomy() -> None:
    # A code that escapes a query and is not in the domain would be refused in production, which
    # is exactly what TR-7 forbids; the drift is caught here instead.
    declared = metric("oktografx_query_errors_total").labels[0].allowed_values
    assert declared is not None
    taxonomy = {getattr(public_errors, name).code for name in public_errors.__all__}
    assert taxonomy <= declared, sorted(taxonomy - declared)
    assert len(declared) <= metric("oktografx_query_errors_total").labels[0].max_cardinality


def test_metric_returns_the_frozen_descriptor_and_refuses_a_stranger() -> None:
    descriptor = metric("oktografx_ledger_depth")
    assert descriptor.kind is MetricKind.GAUGE
    assert descriptor.description == "Number of unapplied-work ledger entries by origin class."
    with pytest.raises(GrafxConfigurationError) as failure:
        metric("oktografx_not_a_metric_total")
    assert "not part of the frozen catalog" in str(failure.value)


def test_metric_names_is_an_immutable_set() -> None:
    names = metric_names()
    assert isinstance(names, frozenset)
    assert names is metric_names()


def test_register_catalog_declares_everything_and_repeats_without_effect() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    register_catalog(sink)
    assert {descriptor.name for descriptor in sink.aggregator.descriptors()} == metric_names()
    assert sink.render().count("# TYPE ") == len(METRIC_CATALOG)


def test_register_catalog_uses_only_the_port() -> None:
    sink = _RecordingSink()
    register_catalog(sink)
    assert [descriptor.name for descriptor in sink.registrations] == [
        descriptor.name for descriptor in METRIC_CATALOG
    ]


def test_register_catalog_is_harmless_on_the_no_op_sink() -> None:
    sink = NoOpMetricsSink()
    register_catalog(sink)
    assert sink.snapshot() == {}


def test_every_catalog_sink_satisfies_the_port() -> None:
    assert isinstance(NoOpMetricsSink(), MetricsSink)
    assert isinstance(OpenMetricsSink(), MetricsSink)


def test_the_emitter_forwards_and_keeps_the_guard_visible() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    emitter = MetricEmitter(sink)
    assert emitter.enabled is True
    assert emitter.sink is sink
    emitter.increment("oktografx_write_conflicts_total")
    emitter.set_gauge("oktografx_wal_size_bytes", 4096.0)
    emitter.observe("oktografx_vector_achieved_k", 3.0)
    with emitter.time("oktografx_query_phase_duration_seconds", {"phase": "parse"}):
        pass
    assert sink.sample_value("oktografx_write_conflicts_total") == 1.0
    assert sink.sample_value("oktografx_wal_size_bytes") == 4096.0
    assert sink.sample_value("oktografx_vector_achieved_k") == 1.0
    assert sink.sample_value("oktografx_query_phase_duration_seconds", {"phase": "parse"}) == 1.0


def test_the_emitter_over_a_no_op_sink_reports_disabled_and_forwards_nothing() -> None:
    emitter = MetricEmitter(NoOpMetricsSink())
    assert emitter.enabled is False
    emitter.increment("oktografx_write_conflicts_total")
    with emitter.time("oktografx_fsync_duration_seconds", {"target": "wal"}):
        pass
    assert emitter.sink.snapshot() == {}


def test_the_emitter_reflects_a_sink_that_is_not_recording() -> None:
    sink = OpenMetricsSink(enabled=False)
    register_catalog(sink)
    emitter = MetricEmitter(sink)
    assert emitter.enabled is False
    emitter.increment("oktografx_write_conflicts_total")
    assert sink.sample_value("oktografx_write_conflicts_total") is None
