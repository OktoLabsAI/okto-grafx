"""The frozen metric catalog against CONTRACT.md section 9 (SPEC-M1 FR-14, TR-7, BR-12, AC-14).

The expected set of names is written out by hand below rather than derived from the catalog, so
that adding, renaming or losing a metric fails here instead of quietly changing what the engine
promises. A second gate reads section 9 of the contract itself, which catches the opposite drift:
a metric the contract requires and the code never declared.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx import errors as public_errors
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.ports.metrics import (
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
        "oktografx_commits_with_metadata_total",
        "oktografx_commit_metadata_bytes_total",
        "oktografx_commit_id_high_watermark_count",
        "oktografx_active_transactions",
        "oktografx_commit_window_duration_seconds",
        "oktografx_commit_phase_duration_seconds",
        "oktografx_commit_pages_logged_total",
        "oktografx_commit_wal_bytes_total",
        "oktografx_commit_frames_examined_total",
        "oktografx_commit_flushes_total",
        "oktografx_commit_foreign_commits_total",
        "oktografx_commit_retargets_total",
        # SPEC-M1 OR-2
        "oktografx_fsync_duration_seconds",
        "oktografx_barrier_failures_total",
        "oktografx_read_view_drops_total",
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
        "oktografx_buffer_retained_estimate_bytes",
        "oktografx_buffer_budget_exceeded_total",
        "oktografx_descriptor_cache_hits_total",
        "oktografx_descriptor_cache_misses_total",
        "oktografx_descriptor_cache_evictions_total",
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

EXPECTED_METRICS: dict[str, tuple[str, str]] = {
    "oktografx_commits_with_metadata_total": (
        "counter",
        "Locally acknowledged writing commits carrying provenance metadata.",
    ),
    "oktografx_commit_metadata_bytes_total": (
        "counter",
        "Canonical metadata bytes in locally acknowledged writing commits.",
    ),
    "oktografx_commit_id_high_watermark_count": (
        "gauge",
        "Last locally acknowledged tracked commit sequence; qualified by this store.",
    ),
    "oktografx_lease_wait_seconds": (
        "histogram",
        "Time a writer waited for the coordination lease, by outcome.",
    ),
    "oktografx_write_conflicts_total": (
        "counter",
        "Transactions refused by optimistic partition validation.",
    ),
    "oktografx_commit_retries_total": (
        "counter",
        "Commit attempts retried after a write conflict.",
    ),
    "oktografx_active_transactions": (
        "gauge",
        "Transactions currently open, by mode.",
    ),
    "oktografx_commit_window_duration_seconds": (
        "histogram",
        "Duration of one write-commit coordination interval, by window and interval.",
    ),
    "oktografx_commit_phase_duration_seconds": (
        "histogram",
        "Duration of one write-commit phase while the commit section is held.",
    ),
    "oktografx_commit_pages_logged_total": (
        "counter",
        "Page images included in write-commit log batches.",
    ),
    "oktografx_commit_wal_bytes_total": (
        "counter",
        "Physical bytes added to the live WAL by successful write-commit appends.",
    ),
    "oktografx_commit_frames_examined_total": (
        "counter",
        "Resident and retired-pinned frames traversed by write-commit flush, modified-page, and "
        "dirty-page scans.",
    ),
    "oktografx_commit_flushes_total": (
        "counter",
        "Buffer-pool flush calls executed by write commits.",
    ),
    "oktografx_commit_foreign_commits_total": (
        "counter",
        "Foreign durable commits completed before a local write commit.",
    ),
    "oktografx_commit_retargets_total": (
        "counter",
        "Write-commit log batches retargeted after segment planning.",
    ),
    "oktografx_fsync_duration_seconds": (
        "histogram",
        "Duration of a durability barrier, by target file class.",
    ),
    "oktografx_barrier_failures_total": (
        "counter",
        "Durability barriers that failed.",
    ),
    "oktografx_read_view_drops_total": (
        "counter",
        "Read views begun over a moved commit token, by publication origin.",
    ),
    "oktografx_wal_size_bytes": (
        "gauge",
        "Total size of the live write-ahead log.",
    ),
    "oktografx_wal_segments": (
        "gauge",
        "Live write-ahead log segments on disk.",
    ),
    "oktografx_wal_truncation_lag_segments": (
        "gauge",
        "Segments held back from recycling, by presence of a live reader.",
    ),
    "oktografx_checksum_verifications_total": (
        "counter",
        "Checksum verifications performed, by verified object.",
    ),
    "oktografx_checksum_failures_total": (
        "counter",
        "Checksum verifications that failed, by verified object.",
    ),
    "oktografx_recovery_replays_total": (
        "counter",
        "Write-ahead log records replayed by recovery.",
    ),
    "oktografx_recovery_discarded_records_total": (
        "counter",
        "Records discarded by recovery, by ledger origin class.",
    ),
    "oktografx_ledger_depth": (
        "gauge",
        "Number of unapplied-work ledger entries by origin class.",
    ),
    "oktografx_ledger_oldest_entry_age_seconds": (
        "gauge",
        "Age of the oldest unapplied-work ledger entry, by origin class.",
    ),
    "oktografx_quarantine_entries": (
        "gauge",
        "Quarantined byte ranges kept for forensic inspection.",
    ),
    "oktografx_buffer_budget_used_bytes": (
        "gauge",
        "Buffer pool memory currently held, by database.",
    ),
    "oktografx_buffer_retained_estimate_bytes": (
        "gauge",
        "Estimated Python memory retained by the buffer pool, by database and estimator.",
    ),
    "oktografx_buffer_budget_exceeded_total": (
        "counter",
        "Page pins refused because the buffer budget was exhausted, by database.",
    ),
    "oktografx_descriptor_cache_hits_total": (
        "counter",
        "Valid cached descriptors returned without reopening a logical file.",
    ),
    "oktografx_descriptor_cache_misses_total": (
        "counter",
        "Descriptor lookups that found no valid cached handle and required resolution.",
    ),
    "oktografx_descriptor_cache_evictions_total": (
        "counter",
        "Cached descriptors released by the bounded least-recently-used admission policy.",
    ),
    "oktografx_database_opens_total": (
        "counter",
        "Database open operations completed.",
    ),
    "oktografx_recoveries_total": (
        "counter",
        "Recovery runs completed, by outcome.",
    ),
    "oktografx_baseline_ceiling_multiple": (
        "gauge",
        "Measured multiple of the calibrated baseline ceiling, by ceiling.",
    ),
    "oktografx_vector_recall_ratio": (
        "gauge",
        "Recall of the most recent calibrated vector search measurement.",
    ),
    "oktografx_vector_query_latency_seconds": (
        "histogram",
        "Duration of one vector search phase, by regime and phase.",
    ),
    "oktografx_vector_exact_fallback_total": (
        "counter",
        "Vector searches answered by an exact scan of the filtered set.",
    ),
    "oktografx_vector_achieved_k": (
        "histogram",
        "Neighbors actually returned by a vector search.",
    ),
    "oktografx_vector_filter_selectivity_ratio": (
        "histogram",
        "Fraction of an embedding space that survived the candidate filter.",
    ),
    "oktografx_vector_tombstone_backlog": (
        "gauge",
        "Vector index tombstones awaiting reconciliation.",
    ),
    "oktografx_vector_reconciliation_total": (
        "counter",
        "Vector index reconciliation passes completed.",
    ),
    "oktografx_vector_index_entries": (
        "gauge",
        "Live vector index entries, by embedding space.",
    ),
    "oktografx_vector_space_retired_total": (
        "counter",
        "Embedding spaces moved to the retired state.",
    ),
    "oktografx_vector_space_coverage_ratio": (
        "gauge",
        "Fraction of the rows of an embedding space that carry an index entry.",
    ),
    "oktografx_vector_index_age_seconds": (
        "gauge",
        "Time since the last vector index build or reconciliation, by space.",
    ),
    "oktografx_query_phase_duration_seconds": (
        "histogram",
        "Duration of one query phase.",
    ),
    "oktografx_query_rows_returned_count": (
        "histogram",
        "Rows returned by one query.",
    ),
    "oktografx_query_errors_total": (
        "counter",
        "Queries that ended in an error, by error code.",
    ),
}
"""Pinned by hand: the kind and the en-US description of every metric.

The name alone is not the contract. The kind is what the ``# TYPE`` line publishes and what
decides whether ``rate()`` over a series means anything, and the description is the ``# HELP``
line an operator reads at three in the morning. Both were flipping freely with a green suite,
because only the handful of metrics some test happens to emit were protected incidentally.
"""


EXPECTED_LABELS: dict[str, tuple[str, ...]] = {
    "oktografx_lease_wait_seconds": ("outcome",),
    "oktografx_active_transactions": ("mode",),
    "oktografx_commit_window_duration_seconds": ("window", "interval"),
    "oktografx_commit_phase_duration_seconds": ("phase",),
    "oktografx_fsync_duration_seconds": ("target",),
    "oktografx_wal_truncation_lag_segments": ("reader_present",),
    "oktografx_checksum_verifications_total": ("kind",),
    "oktografx_checksum_failures_total": ("kind",),
    "oktografx_read_view_drops_total": ("view_origin",),
    "oktografx_recovery_discarded_records_total": ("origin_class",),
    "oktografx_ledger_depth": ("origin_class",),
    "oktografx_ledger_oldest_entry_age_seconds": ("origin_class",),
    "oktografx_buffer_budget_used_bytes": ("db",),
    "oktografx_buffer_retained_estimate_bytes": ("db", "estimator"),
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
    assert len(METRIC_CATALOG) == len(EXPECTED_METRIC_NAMES) == 51


def test_the_catalog_matches_section_nine_of_the_contract_itself() -> None:
    # The opposite drift: a metric the contract requires that the code never declared.
    declared = set(re.findall(r"oktografx_[a-z0-9_]+", _contract_section_nine()))
    assert declared == metric_names(), {
        "in the contract only": sorted(declared - metric_names()),
        "in the catalog only": sorted(metric_names() - declared),
    }


# The descriptor validator of the port already enforces the prefix, the unit suffix, the shape of
# the name and the type of the kind: a catalog that broke any of them would fail at import, so a
# test asserting them again can only ever pass. What the validator cannot know is which kind and
# which words this project chose, and that is what the pinned table above holds.
@pytest.mark.parametrize("name", sorted(EXPECTED_METRICS), ids=sorted(EXPECTED_METRICS))
def test_the_kind_and_description_of_a_metric_are_frozen(name: str) -> None:
    kind, description = EXPECTED_METRICS[name]
    descriptor = metric(name)
    assert descriptor.kind.value == kind, (
        f"{name} is declared as a {descriptor.kind.value} but the contract pins it as a {kind}; "
        f"the kind is what the # TYPE line publishes"
    )
    assert descriptor.description == description, (
        f"{name} changed the # HELP line an operator reads"
    )


def test_the_pinned_table_covers_the_catalog_exactly() -> None:
    # A pin that drifts out of step with the catalog protects nothing.
    assert set(EXPECTED_METRICS) == metric_names()
    assert set(EXPECTED_METRICS) == EXPECTED_METRIC_NAMES


def test_every_metric_kind_is_one_the_exposition_can_render() -> None:
    kinds = {kind for kind, _ in EXPECTED_METRICS.values()}
    assert kinds == {"counter", "gauge", "histogram"}
    assert all(descriptor.kind.value in kinds for descriptor in METRIC_CATALOG)


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_every_description_is_an_en_us_sentence(descriptor: MetricDescriptor) -> None:
    # ASCII, the trailing period and the two-word minimum are enforced by the descriptor
    # validator, so only the two rules this project adds on top of it are asserted here.
    description = descriptor.description
    assert description[0].isupper(), (
        f"{descriptor.name} does not start its help text with a capital"
    )
    assert len(description.split()) >= 3, (
        f"{descriptor.name} has a help text that says too little"
    )


UNENUMERATED_LABEL_BOUNDS: dict[str, int] = {"db": 64, "space": 64}
"""The declared ceiling of every label that does not enumerate its values.

For an enumerated label the value domain is the bound. For these two it is this integer and
nothing else, so it is the single place an unbounded label is stopped (G7, TR-7). Checking the
names without the numbers leaves the number free to change in one token.
"""


@pytest.mark.parametrize(
    ("label_name", "bound"),
    sorted(UNENUMERATED_LABEL_BOUNDS.items()),
    ids=sorted(UNENUMERATED_LABEL_BOUNDS),
)
def test_the_bound_of_each_unenumerated_label_is_frozen(
    label_name: str, bound: int
) -> None:
    found = [
        (descriptor.name, label)
        for descriptor in METRIC_CATALOG
        for label in descriptor.labels
        if label.name == label_name
    ]
    assert found, f"no metric declares the label {label_name!r} any more"
    for metric_name, label in found:
        assert label.allowed_values is None, (
            f"{metric_name} now enumerates {label_name!r}; the pinned bound no longer applies"
        )
        assert label.max_cardinality == bound, (
            f"{metric_name} declares at most {label.max_cardinality} values for {label_name!r}, "
            f"but the catalogue pins {bound}"
        )
        assert label.max_cardinality <= UNBOUNDED_LABEL_CARDINALITY_LIMIT


def test_no_other_label_is_left_unenumerated() -> None:
    unenumerated = {
        label.name
        for descriptor in METRIC_CATALOG
        for label in descriptor.labels
        if label.allowed_values is None
    }
    assert unenumerated == set(UNENUMERATED_LABEL_BOUNDS)


@pytest.mark.parametrize(
    "descriptor", METRIC_CATALOG, ids=[descriptor.name for descriptor in METRIC_CATALOG]
)
def test_only_the_two_free_form_labels_are_left_unenumerated(
    descriptor: MetricDescriptor,
) -> None:
    # The forbidden names and the bound of an unenumerated label are refused by the LabelSpec
    # validator at import. What it cannot know is that this catalog allows exactly two labels to
    # go unenumerated, db and space, because those two carry a short hash or a catalog name.
    for label in descriptor.labels:
        if label.allowed_values is None:
            assert label.name in {"db", "space"}, (
                f"{descriptor.name} leaves the label {label.name!r} unenumerated"
            )


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
def test_every_histogram_uses_one_of_the_four_declared_ladders(
    descriptor: MetricDescriptor,
) -> None:
    # That buckets exist on a histogram, are absent elsewhere and increase strictly is the
    # descriptor validator's job. That a histogram uses one of the four ladders this module
    # documents, rather than an ad-hoc list, is this project's rule and only this test holds it.
    if descriptor.kind is not MetricKind.HISTOGRAM:
        return
    assert descriptor.buckets in {
        LATENCY_BUCKETS_SECONDS,
        RATIO_BUCKETS,
        NEIGHBOR_COUNT_BUCKETS,
        ROW_COUNT_BUCKETS,
    }, f"{descriptor.name} declares an ad-hoc bucket ladder"


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


def _every_reachable_error_class() -> set[type[GrafxError]]:
    """Return every GrafxError subclass in the package, not only the publicly re-exported ones.

    The public list is the taxonomy an application sees; it is not the set of codes that can
    reach a metric label. A component may declare its own internal subclass, and a query
    boundary that writes ``{"code": failure.code}`` will hand that code straight to the sink.
    Importing every module is what makes those visible to ``__subclasses__``.  The subclass
    registry is process-global, though, so test doubles and host-application subclasses may be
    present too; only classes declared by this package are reachable production error types.
    """
    for module in pkgutil.walk_packages(okto_grafx.__path__, f"{okto_grafx.__name__}."):
        # A module of another component that cannot be imported right now also cannot raise
        # anything in production; the walk is a superset finder, never a build gate for others.
        with contextlib.suppress(Exception):
            importlib.import_module(module.name)

    found: set[type[GrafxError]] = set()
    pending: list[type[GrafxError]] = [GrafxError]
    while pending:
        current = pending.pop()
        for subclass in current.__subclasses__():
            if subclass not in found:
                found.add(subclass)
                pending.append(subclass)
    package_prefix = f"{okto_grafx.__name__}."
    return {
        error
        for error in found | {GrafxError}
        if error.__module__ == okto_grafx.__name__
        or error.__module__.startswith(package_prefix)
    }


def test_the_error_walk_ignores_a_foreign_hostile_subclass() -> None:
    class _ForeignTestDouble(GrafxError):
        """A host-owned subclass whose diagnostic descriptor is deliberately unsafe."""

        @property
        def code(self) -> str:
            raise AssertionError("a foreign descriptor must never be inspected")

    assert _ForeignTestDouble not in _every_reachable_error_class()


def test_the_error_code_label_covers_every_reachable_error_code() -> None:
    # A code that escapes a query and is not in the domain would be refused in production, which
    # is exactly what TR-7 forbids; the drift is caught here instead.
    label = metric("oktografx_query_errors_total").labels[0]
    declared = label.allowed_values
    assert declared is not None

    reachable = {error.code for error in _every_reachable_error_class()}
    assert reachable <= declared, sorted(reachable - declared)
    assert len(declared) <= label.max_cardinality

    # The public taxonomy is a subset of the walk; asserting it separately keeps the gate honest
    # if the walk ever finds nothing.
    public = {getattr(public_errors, name).code for name in public_errors.__all__}
    assert public <= declared, sorted(public - declared)
    assert len(reachable) > len(public), (
        "the subclass walk found no internal error class, so it is not proving anything"
    )


def test_the_internal_error_codes_the_walk_is_there_to_catch_are_declared() -> None:
    # These two are owned by C1 and are not in the public re-export list, which is exactly why
    # a gate built on that list could not see them.
    declared = metric("oktografx_query_errors_total").labels[0].allowed_values
    assert declared is not None
    assert {"page_full", "schema_mismatch"} <= declared


def test_every_reachable_error_code_can_actually_be_emitted() -> None:
    # The end-to-end statement of the same property: the sink accepts every one of them.
    sink = OpenMetricsSink()
    register_catalog(sink)
    for error in sorted(_every_reachable_error_class(), key=lambda item: item.code):
        sink.increment("oktografx_query_errors_total", 1.0, {"code": error.code})
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.increment(
            "oktografx_query_errors_total", 1.0, {"code": "not_a_declared_code"}
        )


def test_metric_returns_the_frozen_descriptor_and_refuses_a_stranger() -> None:
    descriptor = metric("oktografx_ledger_depth")
    assert descriptor.kind is MetricKind.GAUGE
    assert (
        descriptor.description
        == "Number of unapplied-work ledger entries by origin class."
    )
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
    assert {
        descriptor.name for descriptor in sink.aggregator.descriptors()
    } == metric_names()
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
    assert (
        sink.sample_value("oktografx_query_phase_duration_seconds", {"phase": "parse"})
        == 1.0
    )


class _RefusingSink:
    """A disabled sink that would notice being called, which is what makes the guard visible.

    Every sink the other tests use drops the call itself when it is not recording, so the guard
    inside the emitter is invisible: removing it changes nothing anybody can see. This one
    reports itself disabled and then raises if anyone emits to it anyway.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return False

    def register(self, descriptor: MetricDescriptor) -> None:
        self.calls.append("register")

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        raise AssertionError("increment reached a sink that reported itself disabled")

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        raise AssertionError("set_gauge reached a sink that reported itself disabled")

    def observe(self, name: str, value: float, labels: object = None) -> None:
        raise AssertionError("observe reached a sink that reported itself disabled")

    def time(self, name: str, labels: object = None) -> object:
        self.calls.append("time")
        return contextlib.nullcontext()

    def snapshot(self) -> dict[str, object]:
        return {}


def test_the_emitter_does_not_forward_to_a_sink_that_reports_itself_disabled() -> None:
    sink = _RefusingSink()
    emitter = MetricEmitter(sink)
    assert emitter.enabled is False
    # None of these may reach the sink: the emitter reads the flag before it forwards.
    emitter.increment("oktografx_write_conflicts_total")
    emitter.set_gauge("oktografx_wal_size_bytes", 1.0)
    emitter.observe("oktografx_vector_achieved_k", 3.0)
    assert sink.calls == []


def test_the_emitter_still_hands_back_the_timer_of_a_disabled_sink() -> None:
    # time() is the one call that must go through even when nothing is recorded, because the
    # caller needs a context manager to enter either way.
    sink = _RefusingSink()
    with MetricEmitter(sink).time(
        "oktografx_fsync_duration_seconds", {"target": "wal"}
    ):
        pass
    assert sink.calls == ["time"]


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
