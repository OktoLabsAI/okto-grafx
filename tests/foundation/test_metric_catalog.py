"""The frozen metric catalog of CONTRACT.md section 9 (post-amendment A1).

The naming rules in ``domain/ports/metrics.py`` are only worth something if the catalogue the
project actually ships satisfies them. Without this file a future edit to
``METRIC_UNIT_SUFFIXES`` or to the forbidden label list could outlaw half the shipped metrics
with the whole suite still green, and C8 would discover it at registration time.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import (
    FORBIDDEN_LABEL_NAMES,
    METRIC_NAME_PREFIX,
    METRIC_UNIT_SUFFIXES,
    LabelSpec,
    MetricDescriptor,
    MetricKind,
)

LabelPlan = tuple[tuple[str, "frozenset[str] | None"], ...]

# Transcribed verbatim from CONTRACT.md section 9. A label whose domain the contract does not
# enumerate is declared unenumerated here, which is what C8 will do at registration.
CATALOG: tuple[tuple[str, LabelPlan], ...] = (
    # M1
    ("oktografx_lease_wait_seconds", (("outcome", frozenset({"granted", "timeout", "takeover"})),)),
    ("oktografx_write_conflicts_total", ()),
    ("oktografx_commit_retries_total", ()),
    ("oktografx_active_transactions", (("mode", frozenset({"read", "write"})),)),
    ("oktografx_fsync_duration_seconds", (("target", frozenset({"wal", "data"})),)),
    ("oktografx_barrier_failures_total", ()),
    ("oktografx_read_view_drops_total", (("view_origin", frozenset({"own", "foreign"})),)),
    ("oktografx_wal_size_bytes", ()),
    ("oktografx_wal_segments", ()),
    ("oktografx_wal_truncation_lag_segments", (("reader_present", frozenset({"true", "false"})),)),
    ("oktografx_checksum_verifications_total", (("kind", frozenset({"page", "record"})),)),
    ("oktografx_checksum_failures_total", (("kind", frozenset({"page", "record"})),)),
    ("oktografx_recovery_replays_total", ()),
    (
        "oktografx_recovery_discarded_records_total",
        (("origin_class", frozenset({"reapplicable", "forensic"})),),
    ),
    ("oktografx_ledger_depth", (("origin_class", None),)),
    ("oktografx_ledger_oldest_entry_age_seconds", (("origin_class", None),)),
    ("oktografx_quarantine_entries", ()),
    ("oktografx_buffer_budget_used_bytes", (("db", None),)),
    ("oktografx_buffer_budget_exceeded_total", (("db", None),)),
    ("oktografx_database_opens_total", ()),
    ("oktografx_recoveries_total", (("outcome", None),)),
    (
        "oktografx_baseline_ceiling_multiple",
        (("ceiling", frozenset({"durable_commit", "point_read", "open_replay", "vector_recall"})),),
    ),
    # VEC
    ("oktografx_vector_recall_ratio", ()),
    ("oktografx_vector_query_latency_seconds", (("regime", None), ("phase", None))),
    ("oktografx_vector_exact_fallback_total", ()),
    ("oktografx_vector_achieved_k", ()),
    ("oktografx_vector_filter_selectivity_ratio", ()),
    ("oktografx_vector_tombstone_backlog", ()),
    ("oktografx_vector_reconciliation_total", ()),
    ("oktografx_vector_index_entries", (("space", None),)),
    ("oktografx_vector_space_retired_total", ()),
    ("oktografx_vector_space_coverage_ratio", (("space", None),)),
    ("oktografx_vector_index_age_seconds", (("space", None),)),
    # Query
    (
        "oktografx_query_phase_duration_seconds",
        (("phase", frozenset({"parse", "plan", "execute"})),),
    ),
    ("oktografx_query_rows_returned_count", ()),
    ("oktografx_query_errors_total", (("code", None),)),
)

CATALOG_LABEL_NAMES: frozenset[str] = frozenset(
    label for _, labels in CATALOG for label, _ in labels
)

DURATION_METRICS: tuple[str, ...] = tuple(
    name for name, _ in CATALOG if name.endswith("_seconds")
)


def _labels(plan: LabelPlan) -> tuple[LabelSpec, ...]:
    """Build the LabelSpec tuple a catalog entry declares."""
    specifications: list[LabelSpec] = []
    for name, values in plan:
        if values is None:
            specifications.append(LabelSpec(name=name))
        else:
            specifications.append(
                LabelSpec(name=name, allowed_values=values, max_cardinality=max(8, len(values)))
            )
    return tuple(specifications)


def test_the_transcription_has_the_size_the_contract_declares() -> None:
    # A guard on the transcription itself: section 9 lists 36 metrics over 13 label names.
    assert len(CATALOG) == 36
    assert len({name for name, _ in CATALOG}) == 36
    assert len(CATALOG_LABEL_NAMES) == 13
    assert len(DURATION_METRICS) == 6


@pytest.mark.parametrize(("name", "labels"), CATALOG, ids=[row[0] for row in CATALOG])
def test_every_catalog_metric_constructs(name: str, labels: LabelPlan) -> None:
    descriptor = MetricDescriptor(
        name=name,
        kind=MetricKind.GAUGE,
        description="A metric of the frozen catalog.",
        labels=_labels(labels),
    )
    assert descriptor.name == name
    assert descriptor.name.startswith(METRIC_NAME_PREFIX)
    assert any(name.endswith(suffix) for suffix in METRIC_UNIT_SUFFIXES)
    assert len(descriptor.labels) == len(labels)


@pytest.mark.parametrize("name", DURATION_METRICS)
def test_every_duration_metric_also_constructs_as_a_histogram(name: str) -> None:
    descriptor = MetricDescriptor(
        name=name,
        kind=MetricKind.HISTOGRAM,
        description="A duration of the frozen catalog.",
        unit="seconds",
        buckets=(0.001, 0.01, 0.1, 1.0, 10.0),
    )
    assert descriptor.kind is MetricKind.HISTOGRAM


@pytest.mark.parametrize("label", sorted(CATALOG_LABEL_NAMES))
def test_every_catalog_label_is_declarable(label: str) -> None:
    assert LabelSpec(name=label).name == label
    assert label not in FORBIDDEN_LABEL_NAMES


def test_the_catalog_carries_the_names_amendment_a1_froze() -> None:
    names = {name for name, _ in CATALOG}
    assert "oktografx_vector_filter_selectivity_ratio" in names
    assert "oktografx_query_rows_returned_count" in names
    for outdated in ("oktografx_vector_filter_selectivity", "oktografx_query_rows_returned"):
        assert outdated not in names
        with pytest.raises(GrafxConfigurationError):
            MetricDescriptor(
                name=outdated, kind=MetricKind.GAUGE, description="An outdated metric name."
            )


def test_the_accepted_suffixes_are_the_ones_amendment_a1_froze() -> None:
    assert METRIC_UNIT_SUFFIXES == frozenset(
        {
            "_seconds",
            "_bytes",
            "_total",
            "_ratio",
            "_count",
            "_multiple",
            "_k",
            "_entries",
            "_segments",
            "_transactions",
            "_backlog",
            "_depth",
        }
    )


def test_every_suffix_the_catalog_needs_is_declared() -> None:
    used = {
        suffix
        for name, _ in CATALOG
        for suffix in METRIC_UNIT_SUFFIXES
        if name.endswith(suffix)
    }
    assert used <= METRIC_UNIT_SUFFIXES
    assert used, "the catalog would be empty of recognised units"
