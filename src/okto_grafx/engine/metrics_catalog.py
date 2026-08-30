"""The frozen metric catalog of Okto Grafx (CONTRACT.md section 9; SPEC-M1 FR-14, TR-7, BR-12).

Every metric the engine is allowed to emit is declared here, once, as a MetricDescriptor. The
descriptor validates itself on construction, so importing this module is already the naming gate
of G7: a bad prefix, a missing unit suffix, a description that is not en-US or a label with an
unbounded domain fails the import of the whole package rather than a scrape in production.

This module is part of the pure core: it holds declarations and lookups, never a clock, never a
lock and never a socket. Aggregation and exposition live in the adapters.

Bucket choices, and why
-----------------------
``LATENCY_BUCKETS_SECONDS`` spans 50 microseconds to 30 seconds on a roughly 2.5x ladder. The
lower end is set by the fastest operation worth timing at all, a buffer pool hit served from
memory; the upper end is set by the slowest one that is still a normal outcome rather than a
failure, a lease wait bounded by ``lease_timeout_seconds`` (10 s by default) and a commit window
bounded by ``commit_lock_timeout_seconds`` (30 s by default). A 2.5x ladder keeps the relative
error of an interpolated quantile near 25 percent across the range, which is enough to see a
regression of the durable commit ceiling of D5 without paying for 40 series per label set.

``RATIO_BUCKETS`` covers the closed interval [0, 1] and is denser at both ends, because that is
where the decisions are: a filter selectivity of 0 means the predicate excluded everything and
the two-regime planner had nothing to scan, and a recall near 1 is exactly where the calibrated
target of SPEC-VEC sits, so 0.9, 0.95 and 0.99 must be distinguishable.

``NEIGHBOR_COUNT_BUCKETS`` holds small integers on a 1-2-5 ladder up to 1000, matching the k a
caller can plausibly ask a vector search for. ``ROW_COUNT_BUCKETS`` is a decade ladder from an
empty result to a million rows, so an accidental full scan is one bucket away from being obvious.

Reading a histogram named with a count suffix
---------------------------------------------
Amendment A1 froze ``oktografx_query_rows_returned_count``. Recorded as a histogram, its exposed
series are ``..._count_bucket``, ``..._count_sum`` and ``..._count_count``. The doubled tail is a
consequence of the frozen name meeting the exposition format, not a defect: the distribution of
result sizes is the operational question, and a gauge of the last query would answer none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import (
    LabelSpec,
    MetricDescriptor,
    MetricKind,
    MetricsSink,
)

__all__ = [
    "LATENCY_BUCKETS_SECONDS",
    "METRIC_CATALOG",
    "NEIGHBOR_COUNT_BUCKETS",
    "RATIO_BUCKETS",
    "ROW_COUNT_BUCKETS",
    "MetricEmitter",
    "metric",
    "metric_names",
    "register_catalog",
]

LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.00005,
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)
"""Latency ladder in seconds, from a memory-served page hit to a timed-out lease wait."""

RATIO_BUCKETS: tuple[float, ...] = (
    0.0,
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    0.75,
    0.9,
    0.95,
    0.99,
    1.0,
)
"""Ratio ladder on the closed interval [0, 1], dense where the operational decisions are."""

NEIGHBOR_COUNT_BUCKETS: tuple[float, ...] = (
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1000.0,
)
"""Small integer ladder for the number of neighbors a vector search returns."""

ROW_COUNT_BUCKETS: tuple[float, ...] = (
    0.0,
    1.0,
    10.0,
    100.0,
    1000.0,
    10000.0,
    100000.0,
    1000000.0,
)
"""Decade ladder for the size of a query result set, starting at the empty result."""

_OUTCOME_LEASE = LabelSpec(
    name="outcome",
    allowed_values=frozenset({"granted", "timeout", "takeover"}),
    max_cardinality=3,
)
_MODE = LabelSpec(name="mode", allowed_values=frozenset({"read", "write"}), max_cardinality=2)
_TARGET = LabelSpec(name="target", allowed_values=frozenset({"wal", "data"}), max_cardinality=2)
_READER_PRESENT = LabelSpec(
    name="reader_present", allowed_values=frozenset({"true", "false"}), max_cardinality=2
)
_CHECKSUM_KIND = LabelSpec(
    name="kind", allowed_values=frozenset({"page", "record"}), max_cardinality=2
)
_ORIGIN_CLASS = LabelSpec(
    name="origin_class",
    allowed_values=frozenset({"reapplicable", "forensic"}),
    max_cardinality=2,
)
_RECOVERY_OUTCOME = LabelSpec(
    name="outcome",
    allowed_values=frozenset({"clean", "truncated", "quarantined", "refused"}),
    max_cardinality=4,
)
_CEILING = LabelSpec(
    name="ceiling",
    allowed_values=frozenset({"durable_commit", "point_read", "open_replay", "vector_recall"}),
    max_cardinality=4,
)
_DATABASE = LabelSpec(name="db", allowed_values=None, max_cardinality=64)
_SPACE = LabelSpec(name="space", allowed_values=None, max_cardinality=64)
_REGIME = LabelSpec(
    name="regime", allowed_values=frozenset({"exact", "approximate"}), max_cardinality=2
)
_VECTOR_PHASE = LabelSpec(
    name="phase",
    allowed_values=frozenset({"plan", "traverse", "validate"}),
    max_cardinality=3,
)
_QUERY_PHASE = LabelSpec(
    name="phase", allowed_values=frozenset({"parse", "plan", "execute"}), max_cardinality=3
)
_VIEW_ORIGIN = LabelSpec(
    name="view_origin", allowed_values=frozenset({"own", "foreign"}), max_cardinality=2
)
_ERROR_CODE = LabelSpec(
    name="code",
    allowed_values=frozenset(
        {
            "grafx_error",
            "write_conflict",
            "lease_timeout",
            "lease_stolen",
            "stale_epoch",
            "corruption_detected",
            "device_full",
            "durability_barrier_failed",
            "recovery_refused",
            "buffer_budget_exceeded",
            "transaction_budget_exceeded",
            "schema_version_mismatch",
            "port_not_configured",
            "transaction_state",
            "ledger_error",
            "quarantine_error",
            "index_error",
            "query_error",
            "query_budget_exceeded",
            "parse_error",
            "plan_error",
            "vector_validation",
            "embedding_space_mismatch",
            "space_retired",
            "configuration_error",
            "unsupported_operation",
            "storage_error",
            "page_full",
            "schema_mismatch",
        }
    ),
    max_cardinality=48,
)
"""Every error code a query can end with, which is every GrafxError code in the package.

The public re-export list of CONTRACT.md section 2 is not the whole taxonomy: a component may
declare an internal GrafxError subclass with its own code, and the moment a query boundary writes
``except GrafxError as failure: ... {"code": failure.code}`` that code arrives here. A code that
is not declared would then be refused at emission time, in production, which is exactly what TR-7
forbids. The catalog test therefore walks every module of the package and asserts this domain
covers every reachable subclass, so a new error class fails the build instead of a scrape.
"""

METRIC_CATALOG: tuple[MetricDescriptor, ...] = (
    # SPEC-M1 OR-1: concurrency
    MetricDescriptor(
        name="oktografx_lease_wait_seconds",
        kind=MetricKind.HISTOGRAM,
        description="Time a writer waited for the coordination lease, by outcome.",
        unit="seconds",
        labels=(_OUTCOME_LEASE,),
        buckets=LATENCY_BUCKETS_SECONDS,
    ),
    MetricDescriptor(
        name="oktografx_write_conflicts_total",
        kind=MetricKind.COUNTER,
        description="Transactions refused by optimistic partition validation.",
    ),
    MetricDescriptor(
        name="oktografx_commit_retries_total",
        kind=MetricKind.COUNTER,
        description="Commit attempts retried after a write conflict.",
    ),
    MetricDescriptor(
        name="oktografx_active_transactions",
        kind=MetricKind.GAUGE,
        description="Transactions currently open, by mode.",
        unit="transactions",
        labels=(_MODE,),
    ),
    # SPEC-M1 OR-2: durability and write-ahead log
    MetricDescriptor(
        name="oktografx_fsync_duration_seconds",
        kind=MetricKind.HISTOGRAM,
        description="Duration of a durability barrier, by target file class.",
        unit="seconds",
        labels=(_TARGET,),
        buckets=LATENCY_BUCKETS_SECONDS,
    ),
    MetricDescriptor(
        name="oktografx_barrier_failures_total",
        kind=MetricKind.COUNTER,
        description="Durability barriers that failed.",
    ),
    MetricDescriptor(
        name="oktografx_read_view_drops_total",
        kind=MetricKind.COUNTER,
        description="Read views begun over a moved commit token, by publication origin.",
        labels=(_VIEW_ORIGIN,),
    ),
    MetricDescriptor(
        name="oktografx_wal_size_bytes",
        kind=MetricKind.GAUGE,
        description="Total size of the live write-ahead log.",
        unit="bytes",
    ),
    MetricDescriptor(
        name="oktografx_wal_segments",
        kind=MetricKind.GAUGE,
        description="Live write-ahead log segments on disk.",
        unit="segments",
    ),
    MetricDescriptor(
        name="oktografx_wal_truncation_lag_segments",
        kind=MetricKind.GAUGE,
        description="Segments held back from recycling, by presence of a live reader.",
        unit="segments",
        labels=(_READER_PRESENT,),
    ),
    # SPEC-M1 OR-3: integrity, recovery and ledger
    MetricDescriptor(
        name="oktografx_checksum_verifications_total",
        kind=MetricKind.COUNTER,
        description="Checksum verifications performed, by verified object.",
        labels=(_CHECKSUM_KIND,),
    ),
    MetricDescriptor(
        name="oktografx_checksum_failures_total",
        kind=MetricKind.COUNTER,
        description="Checksum verifications that failed, by verified object.",
        labels=(_CHECKSUM_KIND,),
    ),
    MetricDescriptor(
        name="oktografx_recovery_replays_total",
        kind=MetricKind.COUNTER,
        description="Write-ahead log records replayed by recovery.",
    ),
    MetricDescriptor(
        name="oktografx_recovery_discarded_records_total",
        kind=MetricKind.COUNTER,
        description="Records discarded by recovery, by ledger origin class.",
        labels=(_ORIGIN_CLASS,),
    ),
    MetricDescriptor(
        name="oktografx_ledger_depth",
        kind=MetricKind.GAUGE,
        description="Number of unapplied-work ledger entries by origin class.",
        unit="entries",
        labels=(_ORIGIN_CLASS,),
    ),
    MetricDescriptor(
        name="oktografx_ledger_oldest_entry_age_seconds",
        kind=MetricKind.GAUGE,
        description="Age of the oldest unapplied-work ledger entry, by origin class.",
        unit="seconds",
        labels=(_ORIGIN_CLASS,),
    ),
    MetricDescriptor(
        name="oktografx_quarantine_entries",
        kind=MetricKind.GAUGE,
        description="Quarantined byte ranges kept for forensic inspection.",
        unit="entries",
    ),
    # SPEC-M1 OR-4: memory, lifecycle and baseline
    MetricDescriptor(
        name="oktografx_buffer_budget_used_bytes",
        kind=MetricKind.GAUGE,
        description="Buffer pool memory currently held, by database.",
        unit="bytes",
        labels=(_DATABASE,),
    ),
    MetricDescriptor(
        name="oktografx_buffer_budget_exceeded_total",
        kind=MetricKind.COUNTER,
        description="Page pins refused because the buffer budget was exhausted, by database.",
        labels=(_DATABASE,),
    ),
    MetricDescriptor(
        name="oktografx_database_opens_total",
        kind=MetricKind.COUNTER,
        description="Database open operations completed.",
    ),
    MetricDescriptor(
        name="oktografx_recoveries_total",
        kind=MetricKind.COUNTER,
        description="Recovery runs completed, by outcome.",
        labels=(_RECOVERY_OUTCOME,),
    ),
    MetricDescriptor(
        name="oktografx_baseline_ceiling_multiple",
        kind=MetricKind.GAUGE,
        description="Measured multiple of the calibrated baseline ceiling, by ceiling.",
        unit="multiple",
        labels=(_CEILING,),
    ),
    # SPEC-VEC OR-1: vector search by regime
    MetricDescriptor(
        name="oktografx_vector_recall_ratio",
        kind=MetricKind.GAUGE,
        description="Recall of the most recent calibrated vector search measurement.",
        unit="ratio",
    ),
    MetricDescriptor(
        name="oktografx_vector_query_latency_seconds",
        kind=MetricKind.HISTOGRAM,
        description="Duration of one vector search phase, by regime and phase.",
        unit="seconds",
        labels=(_REGIME, _VECTOR_PHASE),
        buckets=LATENCY_BUCKETS_SECONDS,
    ),
    MetricDescriptor(
        name="oktografx_vector_exact_fallback_total",
        kind=MetricKind.COUNTER,
        description="Vector searches answered by an exact scan of the filtered set.",
    ),
    MetricDescriptor(
        name="oktografx_vector_achieved_k",
        kind=MetricKind.HISTOGRAM,
        description="Neighbors actually returned by a vector search.",
        unit="neighbors",
        buckets=NEIGHBOR_COUNT_BUCKETS,
    ),
    MetricDescriptor(
        name="oktografx_vector_filter_selectivity_ratio",
        kind=MetricKind.HISTOGRAM,
        description="Fraction of an embedding space that survived the candidate filter.",
        unit="ratio",
        buckets=RATIO_BUCKETS,
    ),
    # SPEC-VEC OR-2: vector index and reconciliation
    MetricDescriptor(
        name="oktografx_vector_tombstone_backlog",
        kind=MetricKind.GAUGE,
        description="Vector index tombstones awaiting reconciliation.",
        unit="entries",
    ),
    MetricDescriptor(
        name="oktografx_vector_reconciliation_total",
        kind=MetricKind.COUNTER,
        description="Vector index reconciliation passes completed.",
    ),
    MetricDescriptor(
        name="oktografx_vector_index_entries",
        kind=MetricKind.GAUGE,
        description="Live vector index entries, by embedding space.",
        unit="entries",
        labels=(_SPACE,),
    ),
    MetricDescriptor(
        name="oktografx_vector_space_retired_total",
        kind=MetricKind.COUNTER,
        description="Embedding spaces moved to the retired state.",
    ),
    MetricDescriptor(
        name="oktografx_vector_space_coverage_ratio",
        kind=MetricKind.GAUGE,
        description="Fraction of the rows of an embedding space that carry an index entry.",
        unit="ratio",
        labels=(_SPACE,),
    ),
    MetricDescriptor(
        name="oktografx_vector_index_age_seconds",
        kind=MetricKind.GAUGE,
        description="Time since the last vector index build or reconciliation, by space.",
        unit="seconds",
        labels=(_SPACE,),
    ),
    # Query surface
    MetricDescriptor(
        name="oktografx_query_phase_duration_seconds",
        kind=MetricKind.HISTOGRAM,
        description="Duration of one query phase.",
        unit="seconds",
        labels=(_QUERY_PHASE,),
        buckets=LATENCY_BUCKETS_SECONDS,
    ),
    MetricDescriptor(
        name="oktografx_query_rows_returned_count",
        kind=MetricKind.HISTOGRAM,
        description="Rows returned by one query.",
        unit="rows",
        buckets=ROW_COUNT_BUCKETS,
    ),
    MetricDescriptor(
        name="oktografx_query_errors_total",
        kind=MetricKind.COUNTER,
        description="Queries that ended in an error, by error code.",
        labels=(_ERROR_CODE,),
    ),
)
"""Every metric of CONTRACT.md section 9, in reading order: M1, then vector, then query."""

_CATALOG_BY_NAME: dict[str, MetricDescriptor] = {
    descriptor.name: descriptor for descriptor in METRIC_CATALOG
}

_METRIC_NAMES: frozenset[str] = frozenset(_CATALOG_BY_NAME)

if len(_CATALOG_BY_NAME) != len(METRIC_CATALOG):  # pragma: no cover - a duplicate never ships
    raise GrafxConfigurationError(
        "The metric catalog declares the same metric name more than once.",
        field="name",
        value=sorted(_CATALOG_BY_NAME),
    )


def metric(name: str) -> MetricDescriptor:
    """Return the frozen descriptor of a metric, or refuse a name that is not in the catalog."""
    descriptor = _CATALOG_BY_NAME.get(name)
    if descriptor is None:
        raise GrafxConfigurationError(
            f"Metric {name!r} is not part of the frozen catalog.",
            field="name",
            value=name,
        )
    return descriptor


def metric_names() -> frozenset[str]:
    """Return the name of every metric in the catalog, which is what the dashboard gate reads."""
    return _METRIC_NAMES


def register_catalog(sink: MetricsSink) -> None:
    """Declare every metric of the catalog on a sink.

    Registration is the authority: a sink that was given this catalog refuses an emission under
    any other name. Calling this more than once on the same sink changes nothing, because a sink
    accepts a repeated registration of an identical descriptor and rejects a conflicting one.
    """
    for descriptor in METRIC_CATALOG:
        sink.register(descriptor)


class MetricEmitter:
    """A thin facade over a MetricsSink that keeps the enabled guard in sight of the caller.

    The wrapper forwards to the sink and re-checks the flag, but it deliberately does not hide
    it: ``enabled`` stays public, and a hot path that would have to build a labels mapping or
    format a value must still read it first. Guarding inside the emitter would only save the
    call, never the allocation the caller makes before the call.
    """

    __slots__ = ("_sink",)

    def __init__(self, sink: MetricsSink) -> None:
        self._sink = sink

    @property
    def sink(self) -> MetricsSink:
        """Return the sink this emitter forwards to."""
        return self._sink

    @property
    def enabled(self) -> bool:
        """Return the enabled flag of the sink, so the caller can guard before it allocates."""
        return self._sink.enabled

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a counter when the sink collects anything."""
        if self._sink.enabled:
            self._sink.increment(name, value, labels)

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set a gauge when the sink collects anything."""
        if self._sink.enabled:
            self._sink.set_gauge(name, value, labels)

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one histogram observation when the sink collects anything."""
        if self._sink.enabled:
            self._sink.observe(name, value, labels)

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> AbstractContextManager[None]:
        """Return the timing context manager of the sink, no-op sink included."""
        return self._sink.time(name, labels)
