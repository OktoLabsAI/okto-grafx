"""The transaction manager (CONTRACT.md section 8.5; SPEC-M1 FR-2, FR-3, FR-4, BR-6, BR-9).

This is the component that decides what a transaction sees and whether it may commit. Two rules
carry everything else.

**Nothing reaches a data file before the commit.** A write transaction stages page images; the
commit appends them to the log, barriers, and only then applies them. That is why an aborted
transaction needs no undo (CONTRACT.md section 8.6 step 6). Proved by
``test_a_rollback_leaves_no_trace_anywhere``,
``test_no_page_is_written_before_the_log_barrier_returns`` and
``test_a_crash_at_every_write_point_leaves_a_recoverable_database``.

**The published state is the only snapshot source.** ``control/commit.state`` is written as the
LAST act of a commit, after the pages of that commit are in place. A reader picks its snapshot
from that file, so on every path where a commit completes, a number it can read is a number
whose pages already exist: a partially applied commit is not merely hidden by the visibility
predicate, it is unreachable as a snapshot. Proved by
``test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it`` and
``test_the_published_state_is_replaced_only_after_every_page_is_in_place``.

Post-barrier failure is a durable commit gap, never permission to continue. The participant first
tries the same shared redo used by startup; if completion still fails, it latches
``recovery_required`` before releasing its local section. Begin, commit, checkpoint and every
page-touching facade door then refuse until operator recovery proves and publishes the whole WAL
prefix. A later transaction therefore cannot publish over a commit whose pages or index effects
remain only in the log.

Two decisions carried from the review record are worth stating where they are implemented.

*Amendment A74 -- validate with the coordinator that granted the lease.* A transaction is bound
to the manager that opened it and is refused by any other, and the lease guard validates through
the coordinator it acquired from. Proved by
``test_a_transaction_cannot_be_committed_through_another_manager`` and
``test_the_epoch_is_validated_before_any_byte_reaches_the_device``.

*Carried finding CF-2 -- register before selecting.* ``register_reader`` publishes before the
registration is visible elsewhere, so a horizon pass in that window can miss a brand-new reader.
:meth:`TransactionManager.begin` therefore reads the published LSN, registers the reader at that
value, and only then selects the snapshot from a second reading. Every horizon computed during
the window is bounded by the published LSN of that moment, which is bounded by the snapshot the
reader ends up with -- so no horizon can ever have passed a snapshot this manager hands out.
Proved by ``test_a_reader_pins_its_snapshot_before_the_snapshot_is_chosen``.

*The writer lease is held for a COMMIT, not for a transaction.* FR-3 says N processes hold write
transactions at the same time and CONTRACT.md section 4.3 says the lease identifies the epoch
holder rather than serialising transactions -- yet only one participant can hold that lease, and
A74 requires a committing writer to hold the one it validates. Both are satisfied by taking the
lease for the commit window: transactions stage their work with nothing on the device and under
no lease at all, and a commit acquires the lease, validates, writes and releases. Disjoint
writers are then ordered at the commit section of step 3, which they already share, and are
never refused merely because another writer exists, which is what BR-6 forbids. Proved by
``test_two_processes_writing_disjoint_partitions_both_commit`` and
``test_a_takeover_inside_the_commit_window_refuses_before_the_first_byte``.

Lock order, because a commit takes two cross-process sections: the lease section is entered and
LEFT before the commit section is entered, and the lease is released only after the commit
section has been left. No path here holds the commit section while asking for the lease, so two
participants cannot hold one section each and wait for the other. Proved by
``test_the_lease_is_released_only_after_the_commit_section_is_left``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, is_dataclass, replace
from time import perf_counter_ns
from types import TracebackType
from typing import Any, Literal

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxRecoveryRefused,
    GrafxSnapshotReclaimed,
    GrafxSchemaVersionMismatch,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
    HEAP_RECLAIM_V1_CAPABILITY,
    WAL_RECORD_V2_CAPABILITY,
    Catalog,
)
from okto_grafx.domain.ids import (
    NO_CSN,
    NO_LSN,
    PROVISIONAL_CSN,
    Csn,
    Epoch,
    Lsn,
    PageIndex,
    RecordRef,
    TxnId,
)
from okto_grafx.domain.index.records import IndexOperation, change_of
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.index.definition import (
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    automatic_index_definitions,
)
from okto_grafx.domain.index.keys import (
    custom_index_sizing,
    identity_index_sizing,
    rehash_index_sizing,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.index.visibility import ReconcileReport
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.coordination import ProcessCoordinator
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.page.layout import MAX_U64
from okto_grafx.domain.recovery.decision import CommittedReplay, committed_replay
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import ENDPOINT_COLUMN_COUNT, TableDef, encode_tuple
from okto_grafx.domain.page import HEADER_PAGE_INDEX, Page
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_record import CommitPayload, FileIdMap, PageTouch
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FORMAT_VERSION, CommitState
from okto_grafx.domain.txn.context import (
    CommitReport,
    PendingRowRef,
    RowIntent,
    RowOperation,
    TransactionContext,
    TransactionMode,
    TransactionState,
)
from okto_grafx.domain.txn.intents import (
    plan_relationship_endpoints,
    reduce_row_intents,
)
from okto_grafx.domain.txn.partitions import (
    page_partition,
    partition_key,
    partition_of,
    validate_partitions_per_table,
)
from okto_grafx.domain.txn.records import (
    WalRecord,
    WalRecordLike,
    WalRecordType,
    decode_page_write_location,
    encode_page_write,
    encode_page_write_record,
    is_redoable_page_file,
)
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.buffer_pool import BufferPool, _BufferWorkProbe, apply_page_image
from okto_grafx.engine.commit_state_store import (
    COMMIT_STATE_READ_ATTEMPTS,
    CommitStateStore,
)
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.heap_store import FIRST_RECORD_ID, HeapVacuumPlan
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.engine.coordination import (
    COMMIT_SECTION,
    DEFAULT_RENEWAL_FRACTION,
    LeaseGuard,
    ReaderRegistration,
    recyclable_horizon,
)
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "ACTIVE_TRANSACTIONS",
    "COMMIT_FLUSHES_TOTAL",
    "COMMIT_FOREIGN_COMMITS_TOTAL",
    "COMMIT_FRAMES_EXAMINED_TOTAL",
    "COMMIT_PAGES_LOGGED_TOTAL",
    "COMMIT_PHASE_DURATION_SECONDS",
    "COMMIT_RETRIES_TOTAL",
    "COMMIT_RETARGETS_TOTAL",
    "COMMIT_SECTION",
    "COMMIT_WAL_BYTES_TOTAL",
    "COMMIT_WINDOW_DURATION_SECONDS",
    "PARTICIPANT_SECTION_PREFIX",
    "COMMIT_STATE_READ_ATTEMPTS",
    "TRANSACTION_MANAGER_METRICS",
    "WRITE_CONFLICTS_TOTAL",
    "TransactionManager",
]

WRITE_CONFLICTS_TOTAL: str = "oktografx_write_conflicts_total"
COMMIT_RETRIES_TOTAL: str = "oktografx_commit_retries_total"
ACTIVE_TRANSACTIONS: str = "oktografx_active_transactions"
COMMIT_WINDOW_DURATION_SECONDS: str = "oktografx_commit_window_duration_seconds"
COMMIT_PHASE_DURATION_SECONDS: str = "oktografx_commit_phase_duration_seconds"
COMMIT_PAGES_LOGGED_TOTAL: str = "oktografx_commit_pages_logged_total"
COMMIT_WAL_BYTES_TOTAL: str = "oktografx_commit_wal_bytes_total"
COMMIT_FRAMES_EXAMINED_TOTAL: str = "oktografx_commit_frames_examined_total"
COMMIT_FLUSHES_TOTAL: str = "oktografx_commit_flushes_total"
COMMIT_FOREIGN_COMMITS_TOTAL: str = "oktografx_commit_foreign_commits_total"
COMMIT_RETARGETS_TOTAL: str = "oktografx_commit_retargets_total"

TRANSACTION_MANAGER_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (
        WRITE_CONFLICTS_TOTAL,
        COMMIT_RETRIES_TOTAL,
        ACTIVE_TRANSACTIONS,
        COMMIT_WINDOW_DURATION_SECONDS,
        COMMIT_PHASE_DURATION_SECONDS,
        COMMIT_PAGES_LOGGED_TOTAL,
        COMMIT_WAL_BYTES_TOTAL,
        COMMIT_FRAMES_EXAMINED_TOTAL,
        COMMIT_FLUSHES_TOTAL,
        COMMIT_FOREIGN_COMMITS_TOTAL,
        COMMIT_RETARGETS_TOTAL,
    )
)
"""The descriptors this component registers and emits, taken from the frozen catalog (G7).

A metric is a contract, so the name is looked up rather than declared a second time: a name that
is not in CONTRACT.md section 9 cannot be emitted from here at all.
"""

PARTICIPANT_SECTION_PREFIX: str = "txn-"
"""Prefix of the section that serialises the THREADS of one participant.

The name carries a digest of the owner identity, so the section belongs to one participant
and one participant only: two processes never contend on it, and two threads of one process
always do. A digest collision between two participants costs them a serialisation neither
needed and can never cost a correctness property, which is why 32 bits are enough here.
"""

_NO_EPOCH: Epoch = 0

_READ_VIEW_MAX_RECORDS: int = 512
_READ_VIEW_MAX_BYTES: int = 2 * 1024 * 1024
_READ_VIEW_MAX_TARGETS: int = 1024
_READ_VIEW_DELTA_TYPES: frozenset[int] = frozenset(
    {
        int(WalRecordType.WRITE_PAGE),
        int(WalRecordType.COMMIT),
        int(WalRecordType.INDEX_WRITE),
        int(WalRecordType.INDEX_RECONCILE),
        int(WalRecordType.SEGMENT_HEADER),
    }
)

_LIVE_FLAGS: int = ~1
"""Mask that clears the deleted bit of a record header, from CONTRACT.md section 6.4."""


@dataclass(frozen=True, slots=True)
class _HeapVersionStamp:
    """One version-header field a commit must stamp on one exact heap page."""

    reference: RecordRef
    is_birth: bool


@dataclass(slots=True)
class _MaterializedAttempt:
    """The stamped page values and header plan of one commit attempt."""

    txn_id: int
    csn: Csn
    pages: dict[tuple[str, PageIndex], Page]
    page_stamps: dict[PageIndex, tuple[_HeapVersionStamp, ...]] | None = None


@dataclass(frozen=True, slots=True)
class _RowWrite:
    """One heap change a commit made: the version it created and the version it ended.

    An insert is a birth alone, a delete an ending alone, and an update is both -- which is why
    they travel together. Both halves carry the same commit number, so a snapshot below it finds
    exactly one live version and a snapshot at or above it finds exactly one.

    The table, durable RecordId and two value tuples travel with the refs because an index may be
    keyed on either the row values or its logical identity. Ending the entry a version had needs
    the authoritative version the heap held inside the commit section; a caller's copy can be one
    version stale. An update keeps one RecordId while moving to a new physical reference.
    """

    born: RecordRef | None
    ended: RecordRef | None
    table: object = None
    born_values: tuple[object, ...] = ()
    ended_values: tuple[object, ...] = ()
    record_id: int | None = None


@dataclass(slots=True)
class _IdentityLease:
    """One process-local, burn-only slice below a table's durable identity floor."""

    next_id: int
    stop: int


@dataclass(frozen=True, slots=True)
class _IdentityPlan:
    """The exact reduced row batch and the durable identities it may materialise."""

    intents: tuple[RowIntent, ...]
    record_ids: dict[int, int]
    leased_positions: frozenset[int]
    reservation_lsn: Lsn | None = None


@dataclass(slots=True)
class _IndexCatalogActivationPlan:
    """Detached exact generations one transaction must finish before publishing catalog v2."""

    definitions: tuple[IndexDefinition, ...]
    page_images: tuple[tuple[tuple[str, PageIndex], bytes], ...]
    read_partitions: frozenset[int]
    write_partitions: frozenset[int]
    state: Literal["planned", "built", "failed"] = "planned"


@dataclass(frozen=True, slots=True)
class _ReadViewToken:
    """The durable publication that every frame in one local read view may reflect."""

    last_committed_lsn: Lsn
    checkpoint_lsn: Lsn


@dataclass(frozen=True, slots=True)
class _ReadViewChanges:
    """Exact physical targets proved to have changed between two durable publications."""

    pages: frozenset[tuple[str, PageIndex]]
    files: frozenset[str]
    catalog_changed: bool


@dataclass(frozen=True, slots=True)
class _CheckpointTarget:
    """The exact durable prefix whose data writes phase B puts on the platter."""

    target_lsn: Lsn
    target_csn: Csn
    base_checkpoint_lsn: Lsn
    barrier_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LocalAppliedPrefix:
    """One checkpoint-based WAL prefix this process already applied and flushed.

    This is an advisory, process-local proof only.  It never replaces WAL lineage, the
    checkpoint control record, or any cross-process authority.  A generic publication or a
    foreign read view revokes it, and checkpoint consumes it only for the exact interval from
    ``checkpoint_lsn`` through ``last_committed_lsn``.
    """

    checkpoint_lsn: Lsn
    applied_through_lsn: Lsn


class _DisabledCommitTrace:
    """Reusable context for the default no-op metrics path.

    It is deliberately one stateless singleton: a write commit with metrics disabled allocates
    no timer, mapping, label set or context-manager object.
    """

    __slots__ = ()

    def __enter__(self) -> None:
        """Return the disabled trace marker."""
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Never suppress the commit outcome."""
        del exc_type, exc_value, traceback
        return False


_DISABLED_COMMIT_TRACE = _DisabledCommitTrace()


class _CommitBufferScope:
    """Keep a buffer-work probe inside the participant section that owns the commit."""

    __slots__ = ("_manager", "_previous", "_trace")

    def __init__(self, manager: TransactionManager, trace: _CommitTrace) -> None:
        self._manager = manager
        self._previous: _CommitTrace | None = None
        self._trace = trace

    def __enter__(self) -> None:
        """Attach only after the participant section has serialized local commits."""
        self._previous = self._manager._active_commit_trace
        self._manager._active_commit_trace = self._trace
        self._trace.attach_buffer()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Detach before that participant section can admit the next local commit."""
        del exc_type, exc_value, traceback
        self._manager._active_commit_trace = None
        self._trace.detach_buffer()
        self._manager._active_commit_trace = self._previous
        return False


def _commit_buffer_scope(
    manager: TransactionManager,
    trace: _CommitTrace | None,
) -> _CommitBufferScope | _DisabledCommitTrace:
    """Return an enabled scope or the allocation-free disabled singleton."""
    return (
        _CommitBufferScope(manager, trace)
        if trace is not None
        else _DISABLED_COMMIT_TRACE
    )


class _CommitTrace:
    """Collect one write-commit trace locally and emit it after every lock is released.

    Timing uses Python's captured process-local performance counter rather than the host-supplied
    ``Clock`` port, and counter arithmetic is data-only. The metrics sink is not called until
    :meth:`__exit__`, which is deliberately the outer context around the participant section.
    Consequently no host callback runs under the writer lease, ``COMMIT_SECTION`` or the
    process-local participant section, and telemetry can never change the commit outcome.
    """

    __slots__ = (
        "_metrics",
        "_pool",
        "_buffer_work",
        "_buffer_attached",
        "_delivery_safe",
        "_timing_enabled",
        "_wait_started",
        "_hold_started",
        "_windows",
        "_phase",
        "_phase_started",
        "_phases",
        "_counters",
    )

    def __init__(self, metrics: MetricsSink, pool: BufferPool) -> None:
        self._metrics = metrics
        self._pool = pool
        self._buffer_work = _BufferWorkProbe()
        self._buffer_attached = False
        self._delivery_safe = True
        self._timing_enabled = True
        self._wait_started: dict[str, float] = {}
        self._hold_started: dict[str, float] = {}
        self._windows: list[tuple[str, str, float]] = []
        self._phase: str | None = None
        self._phase_started: float | None = None
        self._phases: dict[str, float] = {}
        self._counters: dict[str, int] = {}

    def __enter__(self) -> _CommitTrace:
        """Return this collector; buffer accounting waits for participant serialization."""
        return self

    def attach_buffer(self) -> None:
        """Attach data-only accounting after the participant section is acquired."""
        try:
            self._buffer_attached = self._pool._attach_work_probe(self._buffer_work)
        except BaseException:  # noqa: BLE001 - instrumentation is outcome-neutral
            self._buffer_attached = False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Finish open intervals and publish without masking any transaction outcome."""
        del exc_type, exc_value, traceback
        if not self._delivery_safe:
            return False
        self._finish_open_intervals()
        for window, interval, seconds in tuple(self._windows):
            try:
                self._metrics.observe(
                    COMMIT_WINDOW_DURATION_SECONDS,
                    seconds,
                    {"window": window, "interval": interval},
                )
            except BaseException:  # noqa: BLE001 - telemetry is outcome-neutral
                continue
        for phase, seconds in tuple(self._phases.items()):
            try:
                self._metrics.observe(
                    COMMIT_PHASE_DURATION_SECONDS,
                    seconds,
                    {"phase": phase},
                )
            except BaseException:  # noqa: BLE001 - telemetry is outcome-neutral
                continue
        for name, value in tuple(self._counters.items()):
            if value <= 0:
                continue
            try:
                self._metrics.increment(name, float(value))
            except BaseException:  # noqa: BLE001 - telemetry is outcome-neutral
                continue
        return False

    def suppress_delivery(self) -> None:
        """Drop this trace when releasing an exclusive boundary could not be proved."""
        self._delivery_safe = False

    def detach_buffer(self) -> None:
        """Finish buffer accounting before the serialized participant window opens again."""
        attached = self._buffer_attached
        self._buffer_attached = False
        if not attached:
            return
        try:
            self._pool._detach_work_probe(self._buffer_work)
        except BaseException:  # noqa: BLE001 - instrumentation is outcome-neutral
            pass
        try:
            self.increment(COMMIT_FLUSHES_TOTAL, self._buffer_work.flushes)
            self.increment(
                COMMIT_FRAMES_EXAMINED_TOTAL,
                self._buffer_work.frames_examined,
            )
        except BaseException:  # noqa: BLE001 - instrumentation is outcome-neutral
            pass

    def start_window(self, window: str) -> None:
        """Start measuring acquisition of one bounded coordination window."""
        reading = self._reading()
        if reading is not None:
            self._wait_started[window] = reading

    def acquire_window(self, window: str) -> None:
        """Close a wait interval and start its matching hold interval."""
        started = self._wait_started.pop(window, None)
        reading = self._reading()
        if reading is None:
            return
        if started is not None:
            self._windows.append((window, "wait", max(reading - started, 0.0)))
        self._hold_started[window] = reading
        if window == "commit_section":
            self._phase = "other"
            self._phase_started = reading

    def release_window(self, window: str) -> None:
        """Close one held interval; commit-section phases reconcile to this reading."""
        started = self._hold_started.pop(window, None)
        reading = self._reading()
        if reading is None:
            return
        if window == "commit_section":
            self._finish_phase(reading)
        if started is not None:
            self._windows.append((window, "hold", max(reading - started, 0.0)))

    def fail_window(self, window: str) -> None:
        """Close a failed acquisition at its exception boundary, before unwind adds noise."""
        started = self._wait_started.pop(window, None)
        reading = self._reading()
        if started is not None and reading is not None:
            self._windows.append((window, "wait", max(reading - started, 0.0)))

    def phase(self, phase: str) -> None:
        """Move the commit-section clock to a closed-cardinality phase."""
        if "commit_section" not in self._hold_started or phase == self._phase:
            return
        reading = self._reading()
        if reading is None:
            return
        self._finish_phase(reading)
        self._phase = phase
        self._phase_started = reading

    def increment(self, name: str, value: int = 1) -> None:
        """Accumulate a non-negative per-attempt count without touching the sink."""
        if value > 0:
            self._counters[name] = self._counters.get(name, 0) + value

    def capture_batch(
        self,
        images: Sequence[tuple[str, PageIndex, bytes]],
        wal_bytes: int | None,
    ) -> None:
        """Record a successful append's page count and trusted physical WAL growth."""
        self.increment(COMMIT_PAGES_LOGGED_TOTAL, len(images))
        if wal_bytes is not None:
            self.increment(COMMIT_WAL_BYTES_TOTAL, wal_bytes)

    def _reading(self) -> float | None:
        """Read the trusted process timer, never a host callback, for diagnostics only."""
        if not self._timing_enabled:
            return None
        try:
            return perf_counter_ns() / 1_000_000_000
        except BaseException:  # noqa: BLE001 - instrumentation is strictly outcome-neutral
            self._timing_enabled = False
            self._wait_started.clear()
            self._hold_started.clear()
            self._windows.clear()
            self._phase = None
            self._phase_started = None
            self._phases.clear()
            return None

    def _finish_phase(self, reading: float) -> None:
        """Accumulate the current phase through ``reading`` when one is active."""
        phase = self._phase
        started = self._phase_started
        if phase is None or started is None:
            return
        self._phases[phase] = self._phases.get(phase, 0.0) + max(reading - started, 0.0)
        self._phase_started = reading

    def _finish_open_intervals(self) -> None:
        """Close failed acquisitions and defensive leaked holds at the last safe boundary."""
        reading = self._reading()
        if reading is None:
            return
        for window, started in tuple(self._wait_started.items()):
            self._windows.append((window, "wait", max(reading - started, 0.0)))
        self._wait_started.clear()
        if "commit_section" in self._hold_started:
            self._finish_phase(reading)
        for window, started in tuple(self._hold_started.items()):
            self._windows.append((window, "hold", max(reading - started, 0.0)))
        self._hold_started.clear()
        self._phase = None
        self._phase_started = None


class _ReaderPin:
    """One live reader registration together with the reading at which it was last refreshed."""

    __slots__ = ("registration", "last_refresh")

    def __init__(self, registration: ReaderRegistration, last_refresh: float) -> None:
        self.registration: ReaderRegistration = registration
        self.last_refresh: float = last_refresh


class TransactionManager:
    """Opens transactions, decides what they see, and runs the frozen commit protocol."""

    __slots__ = (
        "_wal",
        "_pool",
        "_heap",
        "_catalog",
        "_coordinator",
        "_commit_state_store",
        "_commit_redo",
        "_clock",
        "_metrics",
        "_active_commit_trace",
        "_index_manager",
        "_index_sync",
        "_index_authority_sync_required",
        "_heap_reclaim_capable",
        "_wal_record_v2_capable",
        "_catalog_changes_are_wal_logged",
        "_partitions_per_table",
        "_identity_lease_size",
        "_identity_leases",
        "_index_catalog_activation_plans",
        "_identity_process",
        "_identity_process_invalid",
        "_process_identity_provider",
        "_commit_lock_timeout",
        "_dirty_mark",
        "_lease_timeout",
        "_reader_stall_threshold",
        "_refresh_interval",
        "_descriptor",
        "_materialized",
        "_file_ids",
        "_participant_section_name",
        "_retain_lease",
        "_lease_guard",
        "_next_txn_id",
        "_open",
        "_transaction_descriptor_scopes",
        "_participant_pin",
        "_published_high_water",
        "_own_published_lsn",
        "_local_applied_prefix",
        "_recovery_required",
        "_page_staging_capability",
        "_mode_counts",
        "_max_transaction_rows",
        "_max_transaction_bytes",
        "_max_wal_batch_bytes",
        "_max_index_build_entries",
        "_automatic_index_expected_cardinality",
        "_automatic_index_bucket_count",
        "_writable",
        "_closed",
        "_close_quiesced",
        "_close_complete",
        "_close_finalizing",
    )

    def __init__(
        self,
        wal: Any,
        pool: BufferPool,
        heap: Any,
        catalog: Any,
        coordinator: ProcessCoordinator,
        clock: Clock,
        metrics: MetricsSink,
        index_manager: Any,
        *,
        partitions_per_table: int,
        commit_lock_timeout: float,
        identity_lease_size: int = 64,
        lease_timeout: float | None = None,
        reader_stall_threshold: float | None = None,
        descriptor: str = "",
        retain_lease: bool = False,
        index_sync: Callable[[], object] | None = None,
        writable: bool = True,
        max_transaction_rows: int | None = None,
        max_transaction_bytes: int | None = None,
        max_wal_batch_bytes: int | None = None,
        max_index_build_entries: int | None = None,
        automatic_index_expected_cardinality: int | None = None,
        database_uuid: bytes | None = None,
        control_format_version: int = 1,
        control_file_nonce: int = 0,
        process_identity_provider: Callable[[], object] | None = None,
        catalog_changes_are_wal_logged: bool = False,
    ) -> None:
        """Build a manager over one database.

        The keyword arguments after the two the contract names are configuration this
        component cannot invent and must not guess:

        * ``lease_timeout`` -- how long a commit waits for the writer lease. It defaults to
          ``commit_lock_timeout`` so a manager built exactly as CONTRACT.md section 8.5 spells
          it still works; the composition root passes ``lease_timeout_seconds``.
        * ``reader_stall_threshold`` -- the threshold C3 prunes a quiet reader at, which is what
          amendment A46 makes this component schedule against. ``None`` means "refresh at every
          opportunity", the conservative reading: a manager that was told nothing about the
          threshold must never be the reason a reader misses it.
        * ``descriptor`` -- the granularity descriptor of SD-1. It is passed in rather than
          rebuilt here because ``DatabaseConfig`` already produces that exact string and two
          places producing one format string is how the two stop agreeing (amendment A24).
        * ``writable`` -- the capability to open write transactions or checkpoint. Read-only
          composition passes ``False`` so both doors refuse before coordination, WAL or storage;
          the compatible default remains ``True`` for existing composition roots.
        * the three transaction ``max_*`` values -- opt-in transaction admission limits.
          ``None`` preserves existing behaviour; direct composition must provide exact positive
          integers.
        * ``max_index_build_entries`` -- the separate opt-in admission limit for detached exact
          generation batches; it does not change transaction row/byte accounting.
        * ``catalog_changes_are_wal_logged`` -- a composition proof that every runtime catalog
          mutation is staged on a transaction and therefore appears in the CE-3 WAL interval.
          Direct compositions default to the conservative legacy answer.
        """
        if not isinstance(writable, bool):
            raise GrafxConfigurationError(
                f"writable is a capability flag; got {type(writable).__name__}.",
                field="writable",
                value=type(writable).__name__,
            )
        self._writable: bool = writable
        self._wal: Any = wal
        self._pool: BufferPool = pool
        self._heap: Any = heap
        self._catalog: Any = catalog
        self._coordinator: ProcessCoordinator = coordinator
        self._commit_state_store = CommitStateStore(
            pool.storage,
            owner_id=coordinator.owner_id(),
            database_uuid=database_uuid,
            control_format_version=control_format_version,
            file_nonce=control_file_nonce,
        )
        self._commit_redo = CommitRedo(pool, index_manager)
        self._clock: Clock = clock
        self._metrics: MetricsSink = metrics
        self._active_commit_trace: _CommitTrace | None = None
        self._index_manager: Any = index_manager
        self._index_sync: Callable[[], object] | None = index_sync
        self._index_authority_sync_required: bool = False
        self._heap_reclaim_capable: bool = False
        self._wal_record_v2_capable: bool = False
        if type(catalog_changes_are_wal_logged) is not bool:
            raise GrafxConfigurationError(
                "catalog_changes_are_wal_logged must be exactly True or False.",
                field="catalog_changes_are_wal_logged",
                value=repr(catalog_changes_are_wal_logged),
            )
        self._catalog_changes_are_wal_logged = catalog_changes_are_wal_logged
        (
            self._automatic_index_bucket_count,
            self._automatic_index_expected_cardinality,
        ) = custom_index_sizing(
            expected_cardinality=automatic_index_expected_cardinality
        )
        self._refresh_heap_reclaim_capability()
        self._refresh_wal_record_v2_capability()
        self._partitions_per_table: int = validate_partitions_per_table(
            partitions_per_table
        )
        self._identity_lease_size: int = _require_positive_int(
            "identity_lease_size", identity_lease_size
        )
        if process_identity_provider is None:
            # Direct/internal composition historically supplies only a coordinator. Capture its
            # construction identity once so fail-fast doors never turn an ownership check into
            # operational coordination. Production assembly injects ``os.getpid`` and therefore
            # gets the stronger post-fork guard.
            construction_identity = coordinator.owner_id()

            def provider() -> object:
                """Return the process identity captured for direct composition."""
                return construction_identity
        else:
            provider = process_identity_provider
        if not callable(provider):
            raise GrafxConfigurationError(
                "process_identity_provider must be callable.",
                field="process_identity_provider",
                value=type(provider).__name__,
            )
        self._process_identity_provider: Callable[[], object] = provider
        self._identity_process: object = provider()
        self._identity_process_invalid: bool = False
        self._identity_leases: dict[int, _IdentityLease] = {}
        self._index_catalog_activation_plans: dict[
            TxnId, _IndexCatalogActivationPlan
        ] = {}
        self._commit_lock_timeout: float = _require_timeout(
            "commit_lock_timeout", commit_lock_timeout
        )
        self._lease_timeout: float = (
            self._commit_lock_timeout
            if lease_timeout is None
            else _require_timeout("lease_timeout", lease_timeout)
        )
        self._reader_stall_threshold: float | None = (
            None
            if reader_stall_threshold is None
            else _require_timeout("reader_stall_threshold", reader_stall_threshold)
        )
        self._refresh_interval: float = (
            0.0
            if self._reader_stall_threshold is None
            else self._reader_stall_threshold * DEFAULT_RENEWAL_FRACTION
        )
        if not isinstance(descriptor, str):
            raise GrafxConfigurationError(
                f"The granularity descriptor must be a string; got {type(descriptor).__name__}.",
                field="descriptor",
                value=type(descriptor).__name__,
            )
        self._descriptor: str = descriptor
        # The page VALUES the current commit attempt stamped, bound to that attempt's txn id
        # and materialised CSN: the retarget of the SAME attempt re-stamps and re-encodes
        # them instead of decoding the logged bytes again; anything else takes the
        # verifying path. Cleared once the attempt's batch is final.
        self._materialized: _MaterializedAttempt | None = None
        self._file_ids: FileIdMap = FileIdMap(
            heap_file=_file_name_of(heap, "heap.dat"),
            catalog_file=_file_name_of(catalog, "catalog.dat"),
        )
        if not isinstance(retain_lease, bool):
            raise GrafxConfigurationError(
                f"retain_lease is a policy flag; got {type(retain_lease).__name__}.",
                field="retain_lease",
                value=type(retain_lease).__name__,
            )
        self._retain_lease: bool = retain_lease
        self._max_transaction_rows = _require_optional_positive_limit(
            "max_transaction_rows", max_transaction_rows
        )
        self._max_transaction_bytes = _require_optional_positive_limit(
            "max_transaction_bytes", max_transaction_bytes
        )
        self._max_wal_batch_bytes = _require_optional_positive_limit(
            "max_wal_batch_bytes", max_wal_batch_bytes
        )
        self._max_index_build_entries = _require_optional_positive_limit(
            "max_index_build_entries", max_index_build_entries
        )
        self._lease_guard: LeaseGuard | None = None
        self._participant_section_name: str = (
            f"{PARTICIPANT_SECTION_PREFIX}"
            f"{crc32c(coordinator.owner_id().encode('utf-8')):08x}"
        )
        self._next_txn_id: TxnId = 1
        self._open: dict[TxnId, TransactionContext] = {}
        # Long-lived descriptor reuse is owned by the same manager that owns transaction
        # settlement.  The values carry no lock or durable authority between operations: the
        # concrete coordinator parks only a proved-unlocked participant lock-file descriptor.
        # Keeping the map here lets commit, rollback, retry and terminal close all converge on
        # the same drain even when no public Transaction wrapper survives.
        self._transaction_descriptor_scopes: dict[TxnId, Any] = {}
        # CE-2: ONE reader registration per participant, opened lazily by the first begin and
        # withdrawn only by close. Its pin is deferred and monotone -- it follows the oldest
        # open snapshot, never passes it (BR-10), and is republished on the refresh cadence
        # rather than on every transaction boundary.
        self._participant_pin: _ReaderPin | None = None
        self._published_high_water: Lsn = NO_LSN
        # The last commit number THIS manager published through step 3.7 (CQ-2/QW-4). Only
        # _publish_commit_state remembers it: a gap completion or a checkpoint publishes
        # through _publish and must never be remembered here, because their LSN can carry a
        # foreign commit and an "own" read view over it would keep frames that predate it.
        # Compared by equality only -- recovery can republish a smaller number and a foreign
        # checkpoint republishes the same one.
        self._own_published_lsn: Lsn | None = None
        # A bounded, revocable witness that this exact process applied and flushed every
        # ordinary DML effect after one completed checkpoint.  It is deliberately absent on
        # open and is never persisted: another participant remains authoritative through WAL.
        self._local_applied_prefix: _LocalAppliedPrefix | None = None
        self._recovery_required: bool = False
        self._closed: bool = False
        self._close_quiesced: bool = False
        self._close_complete: bool = False
        self._close_finalizing: bool = False
        # An identity token, never exported through a public door. QueryEngine receives only the
        # bound private staging callback, so encoded physical pages cannot be supplied through a
        # caller-reachable TransactionContext and later mistaken for store-produced state.
        self._page_staging_capability: object = object()
        # The page set a commit attempt is measured against; see _attempt_pages. Set at the top
        # of every attempt and NOT cleared afterwards: every reader of it runs inside the attempt
        # that set it, under the participant section, so a stale value is unreachable rather than
        # guarded against. Empty until the first attempt.
        self._dirty_mark: frozenset[tuple[str, PageIndex]] = frozenset()
        self._mode_counts: dict[str, int] = {
            TransactionMode.READ.value: 0,
            TransactionMode.WRITE.value: 0,
        }
        if metrics.enabled:
            for descriptor_value in TRANSACTION_MANAGER_METRICS:
                metrics.register(descriptor_value)
            for mode_name in self._mode_counts:
                metrics.set_gauge(ACTIVE_TRANSACTIONS, 0.0, {"mode": mode_name})

    # --- identity ---------------------------------------------------------------------------

    @property
    def partitions_per_table(self) -> int:
        """Return the conflict granularity this manager validates at."""
        return self._partitions_per_table

    @property
    def commit_lock_timeout(self) -> float:
        """Return how long a commit waits for the cross-process commit section."""
        return self._commit_lock_timeout

    @property
    def lease_timeout(self) -> float:
        """Return how long a commit waits for the writer lease."""
        return self._lease_timeout

    @property
    def reader_stall_threshold(self) -> float | None:
        """Return the reader stall threshold this manager schedules refreshes against (A46)."""
        return self._reader_stall_threshold

    @property
    def refresh_interval(self) -> float:
        """Return how much monotonic time may pass between two refreshes of one reader."""
        return self._refresh_interval

    @property
    def index_manager(self) -> Any:
        """Return the index manager this database was built with, or None when there is none.

        BR-11 has two halves and this component owes both. The records an index stages are
        appended inside the same commit as the heap write they belong to, which is what
        ``pending_records`` carries; and the changes those records describe are applied to the
        index itself once the commit is durable, which is what :meth:`_apply_index_changes` and
        the rollback path do. Logging an index change and never applying it leaves a committed
        row in the heap and in the log and invisible to every lookup until something rebuilds --
        a missing row for an exact index, a silently short answer for a proximity one.
        """
        return self._index_manager

    @property
    def writable(self) -> bool:
        """Return whether this manager may open or perform persistent write work."""
        return self._writable

    @property
    def open_transactions(self) -> int:
        """Return how many transactions this manager currently has open."""
        return len(self._open)

    @property
    def recovery_required(self) -> bool:
        """Return True while a durable commit gap forbids new snapshots and writes."""
        return self._recovery_required

    @property
    def closed(self) -> bool:
        """Return True once terminal close has started for this manager."""
        return self._closed

    @property
    def close_quiesced(self) -> bool:
        """Return True once no transaction, reader pin, or retained guard is still attached."""
        return self._close_quiesced

    @property
    def close_complete(self) -> bool:
        """Return True once terminal cleanup and its outcome-neutral telemetry are complete."""
        return self._close_complete

    @property
    def transition_active(self) -> bool:
        """Return True while an injected lifecycle/participant boundary is active.

        Every supported ``connect`` assembly shares one ContainedMetrics capability with this
        manager and its facade. False for a sink without that optional capability is retained
        only for direct internal construction; it does not promise callback reentrancy safety.
        """
        try:
            return bool(getattr(self._metrics, "transition_active", False))
        except BaseException:  # noqa: BLE001 - host telemetry cannot control lifecycle
            return False

    def request_close(self) -> None:
        """Publish the terminal latch without trying to interrupt an in-flight transition."""
        self._require_process_owner("request close")
        self._local_applied_prefix = None
        self._closed = True

    @contextmanager
    def database_release_section(self) -> Iterator[None]:
        """Serialize the facade's short claim to release this manager's dependencies."""
        if not self._close_complete:
            raise GrafxTransactionStateError(
                "Database dependencies cannot be released before transaction close completes.",
                operation="release database dependencies",
                close_complete=False,
            )
        with self._participant_section():
            if not self._close_complete:
                raise GrafxTransactionStateError(
                    "Transaction close changed before dependency release could be claimed.",
                    operation="release database dependencies",
                    close_complete=False,
                )
            yield

    def require_recovery(self) -> None:
        """Latch this participant closed until a complete operator recovery succeeds."""
        self._require_not_closed("require recovery")
        with self._participant_section():
            self._require_not_closed("require recovery")
            self._identity_leases.clear()
            self._local_applied_prefix = None
            self._recovery_required = True

    def recovery_completed(self) -> None:
        """Release the latch only after durable state covers this process's high-water mark."""
        self._require_not_closed("complete recovery")
        with self._participant_section():
            self._require_not_closed("complete recovery")
            durable = self._read_commit_state()
            if durable.last_committed_lsn < self._published_high_water:
                raise GrafxRecoveryRefused(
                    "Recovery returned without publishing every durable commit this participant "
                    "already observed; the handle remains recovery-required.",
                    field="recovery_required",
                    published_lsn=durable.last_committed_lsn,
                    required_lsn=self._published_high_water,
                )
            self._identity_leases.clear()
            # Recovery is deliberately never a source of process-local checkpoint authority.
            # The next completed checkpoint may seed a fresh witness.
            self._local_applied_prefix = None
            self._recovery_required = False

    # --- snapshots --------------------------------------------------------------------------

    def partition_of(self, table_id: int, key: bytes) -> int:
        """Return the partition key a row of this table with this key belongs to (FR-4)."""
        return partition_of(table_id, key, self._partitions_per_table)

    def _stage_page_image(
        self,
        txn: TransactionContext,
        file: str,
        page_index: PageIndex,
        image: bytes,
    ) -> None:
        """Accept one physical image from a trusted engine collaborator.

        This is intentionally private.  The query/store layer receives the bound callable, not
        the capability itself; the context seals the accepted location and bytes so direct
        mutation of its observable ``page_images`` map is detected before commit writes WAL.
        """
        self._require_owned(txn)
        txn._stage_page_image(
            file,
            page_index,
            image,
            capability=self._page_staging_capability,
        )

    def _require_fresh_index_catalog_transaction(
        self,
        txn: TransactionContext,
        *,
        operation: str,
        purpose: str,
    ) -> None:
        """Require the untouched write transaction used by one detached catalog build.

        A detached shadow contains only the durable heap photographed by preparation.  The
        transaction must therefore be dedicated to publishing that shadow: prior reads would
        make its fencing ambiguous, while prior or later writes could be omitted from the full
        build.  Keep this boundary shared by explicit identity activation and custom exact-index
        creation so neither path can quietly acquire a weaker transaction contract.
        """

        self._require_not_closed(operation)
        self._require_owned(txn)
        self._require_active(txn)
        self._require_writable(operation)
        if txn.mode is not TransactionMode.WRITE:
            raise GrafxTransactionStateError(
                f"{purpose} requires a write transaction.",
                operation=operation,
                txn_id=txn.txn_id,
                mode=txn.mode.value,
            )
        if txn.txn_id in self._index_catalog_activation_plans:
            raise GrafxTransactionStateError(
                "This transaction already owns a detached index-catalog activation plan.",
                operation=operation,
                txn_id=txn.txn_id,
            )
        if (
            txn.wrote
            or txn.read_partitions
            or txn.row_refs
            or txn._page_image_proofs
            or txn._staging_marks
            or txn._pending_row_refs
            or txn._effective_row_tables is not None
            or txn._staged_payload_bytes != 0
            or txn._next_pending_token != -1
            or txn.conflicts != 0
        ):
            raise GrafxTransactionStateError(
                f"{purpose} requires a fresh, dedicated transaction.",
                operation=operation,
                field="activation_transaction",
                txn_id=txn.txn_id,
            )

    def prepare_identity_index_activation(self, txn: TransactionContext) -> bool:
        """Stage one explicit, atomic catalog-v2 identity-index activation.

        Preparation photographs committed catalog/index authority under ``COMMIT_SECTION`` and
        stages only catalog page values.  The physical generations are deliberately deferred to
        :meth:`_build_index_catalog_activation`: commit's first OCC pass must still get the last
        word, and the detached files must be built while the writer lease and commit section are
        both held.  ``False`` is the durable no-op result for a v2 catalog whose required identity
        generations are already active and fresh.
        """

        operation = "prepare identity-index activation"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="Identity-index activation",
        )

        with self._participant_section():
            self._require_not_closed("prepare identity-index activation")
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "Identity-index activation needs the concrete persistent catalog.",
                        operation="prepare identity-index activation",
                        field="catalog",
                        value=type(source).__name__,
                    )
                manager = self._index_manager
                if manager is None:
                    raise GrafxUnsupportedOperation(
                        "Identity-index activation needs the exact index manager.",
                        operation="prepare identity-index activation",
                        field="indexes",
                    )
                published = self._published_state_in_section().last_committed_lsn
                definitions = self._plan_identity_index_activation(
                    txn,
                    source,
                    published,
                )
                if definitions is None:
                    return False

                candidate, runtime_definitions = definitions
                self._stage_index_catalog_activation_plan(
                    txn,
                    candidate,
                    runtime_definitions,
                    published,
                    operation=operation,
                )
                return True

    def prepare_heap_reclaim_activation(self, txn: TransactionContext) -> bool:
        """Stage the one-way catalog capability required before physical heap reclaim.

        Catalog v2 must already be active.  This keeps its potentially expensive detached index
        build in the explicit identity-activation phase and makes the vacuum capability commit a
        small, independently recoverable metadata boundary.  ``False`` is the durable no-op when
        this build's required bit is already present.
        """

        operation = "prepare heap-reclaim activation"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="Heap-reclaim activation",
        )
        with self._participant_section():
            self._require_not_closed(operation)
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "Heap-reclaim activation needs the concrete persistent catalog.",
                        operation=operation,
                        field="catalog",
                        value=type(source).__name__,
                    )
                if source.format_version != CATALOG_FORMAT_VERSION:
                    raise GrafxUnsupportedOperation(
                        "Heap-reclaim activation requires catalog v2; run "
                        "maintenance.ensure_identity_indexes() first.",
                        operation=operation,
                        field="format_version",
                        value=source.format_version,
                        required=CATALOG_FORMAT_VERSION,
                        remedy="maintenance.ensure_identity_indexes",
                    )
                if HEAP_RECLAIM_V1_CAPABILITY in source.required_capabilities():
                    return False
                candidate = Catalog.deserialize(source.serialize())
                candidate.enable_heap_reclaim()
                for page_index, image in self._catalog.stage(candidate):
                    self._stage_page_image(
                        txn,
                        self._file_ids.catalog_file,
                        page_index,
                        image,
                    )
                return True

    def prepare_wal_record_v2_activation(self, txn: TransactionContext) -> bool:
        """Stage the durable capability fence before any compressed page record is emitted."""

        operation = "prepare WAL-record-v2 activation"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="WAL-record-v2 activation",
        )
        with self._participant_section():
            self._require_not_closed(operation)
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "WAL-record-v2 activation needs the concrete persistent catalog.",
                        operation=operation,
                        field="catalog",
                        value=type(source).__name__,
                    )
                if source.format_version != CATALOG_FORMAT_VERSION:
                    raise GrafxUnsupportedOperation(
                        "WAL-record-v2 activation requires catalog v2; run "
                        "maintenance.ensure_identity_indexes() first.",
                        operation=operation,
                        field="format_version",
                        value=source.format_version,
                        required=CATALOG_FORMAT_VERSION,
                        remedy="maintenance.ensure_identity_indexes",
                    )
                if source.requires_capability(WAL_RECORD_V2_CAPABILITY):
                    return False
                candidate = Catalog.deserialize(source.serialize())
                candidate.enable_wal_record_v2()
                for page_index, image in self._catalog.stage(candidate):
                    self._stage_page_image(
                        txn,
                        self._file_ids.catalog_file,
                        page_index,
                        image,
                    )
                return True

    def prepare_vacuum(
        self,
        txn: TransactionContext,
        tables: Sequence[TableDef],
        horizon: Lsn,
        *,
        max_versions: int | None = None,
    ) -> tuple[HeapVacuumPlan, tuple[ReconcileReport, ...], Lsn, Lsn]:
        """Stage one atomic heap/index reclaim pass and its monotonic snapshot floor."""

        operation = "prepare MVCC vacuum"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="MVCC vacuum",
        )
        with self._participant_section():
            self._require_not_closed(operation)
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "MVCC vacuum needs the concrete persistent catalog.",
                        operation=operation,
                        field="catalog",
                        value=type(source).__name__,
                    )
                if HEAP_RECLAIM_V1_CAPABILITY not in source.required_capabilities():
                    raise GrafxUnsupportedOperation(
                        "MVCC vacuum requires the published heap_reclaim_v1 capability.",
                        operation=operation,
                        field="required_capabilities",
                        value=HEAP_RECLAIM_V1_CAPABILITY,
                    )
                selected_tables = tuple(tables)
                table_ids = {table.table_id for table in selected_tables}
                plan = self._heap.plan_vacuum(
                    selected_tables,
                    horizon,
                    max_versions=max_versions,
                )
                manager = self._index_manager
                active_indexes = getattr(manager, "active_indexes", None)
                if not callable(active_indexes):
                    raise GrafxUnsupportedOperation(
                        "MVCC vacuum needs the catalog-authoritative index manager.",
                        operation=operation,
                        field="indexes",
                    )
                indexes = tuple(active_indexes(catalog=source))
                reports = tuple(
                    index.reconcile(horizon, txn)
                    for index in indexes
                    if index.definition.table_id in table_ids
                )
                removed_indexes = sum(report.removed for report in reports)
                reclaimed_versions = sum(
                    table.reclaimed_versions for table in plan.tables
                )
                old_floor = self._heap.reclaim_floor()
                if reclaimed_versions == 0 and removed_indexes == 0:
                    return plan, reports, old_floor, old_floor

                for page_index, image in plan.page_images:
                    self._stage_page_image(
                        txn,
                        self._heap_file,
                        page_index,
                        image,
                    )
                floor = self._heap.plan_reclaim_floor(horizon)
                self._stage_page_image(
                    txn,
                    self._heap_file,
                    floor.page_index,
                    floor.image,
                )
                self._declare_complete_table_reads(
                    txn, {table.table_id: table for table in selected_tables}
                )
                txn.note_read(
                    page_partition(self._file_ids.catalog_file, HEADER_PAGE_INDEX)
                )
                return plan, reports, floor.old_floor, floor.new_floor

    def prepare_custom_exact_index(
        self,
        txn: TransactionContext,
        *,
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        bucket_count: int,
        expected_cardinality: int | None,
    ) -> CatalogIndexDefinition:
        """Seal a full custom exact-index build into one fresh write transaction.

        This is an internal transaction-manager port, not a general transaction operation.  The
        caller must dedicate an untouched write transaction to it and must not add reads, DML,
        page images or another catalog plan before or after this call.  Preparation validates the
        custom definition first, composes any required v1-to-v2 automatic activation or v2
        identity repair into the same catalog candidate, fences every partition of every scanned
        table, preflights the complete detached-build batch, and stages only catalog images.
        Commit later constructs all nonced shadows under the writer lease and ``COMMIT_SECTION``;
        the catalog WAL commit remains their sole publication point.

        The returned value is the exact logical definition planned for publication, including
        its final ACTIVE generation nonce and resolved bucket count.
        """

        operation = "prepare custom exact index"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="Custom exact-index creation",
        )

        with self._participant_section():
            self._require_not_closed(operation)
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "Custom exact-index creation needs the concrete persistent catalog.",
                        operation=operation,
                        field="catalog",
                        value=type(source).__name__,
                    )
                manager = self._index_manager
                if manager is None:
                    raise GrafxUnsupportedOperation(
                        "Custom exact-index creation needs the exact index manager.",
                        operation=operation,
                        field="indexes",
                    )

                # Every caller-controlled refusal is decided before activation planning can
                # allocate a nonce or add table interests to the transaction.
                table, provisional = self._validate_custom_exact_index_request(
                    source,
                    manager=manager,
                    name=name,
                    table_name=table_name,
                    positions=positions,
                    bucket_count=bucket_count,
                    expected_cardinality=expected_cardinality,
                    operation=operation,
                )
                published = self._published_state_in_section().last_committed_lsn
                activation = self._plan_identity_index_activation(
                    txn,
                    source,
                    published,
                    retain_noop_candidate=True,
                )
                # ``retain_noop_candidate`` makes the private planner total for this composing
                # operation; the optional return remains for the idempotent identity-only door.
                assert activation is not None
                candidate, runtime_definitions = activation

                occupied = self._index_generation_nonces(candidate)
                nonce = manager._allocate_detached_generation_nonce(occupied)
                generation = IndexGenerationDescriptor(
                    artifact_nonce=nonce,
                    bucket_count=bucket_count,
                    state=IndexGenerationState.ACTIVE,
                )
                logical = replace(provisional, generations=(generation,))
                candidate.add_index_definition(logical)
                custom_runtime = logical.runtime_definition(generation)
                complete_runtime = (*runtime_definitions, custom_runtime)
                self._declare_complete_table_reads(txn, {table.table_id: table})

                # Full authority validation and quota admission both precede catalog staging;
                # physical g_* creation remains deferred until commit owns both global fences.
                candidate.serialize()
                self._stage_index_catalog_activation_plan(
                    txn,
                    candidate,
                    complete_runtime,
                    published,
                    operation=operation,
                )
                return logical

    def prepare_index_rehash(
        self,
        txn: TransactionContext,
        *,
        name: str,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
    ) -> CatalogIndexDefinition:
        """Seal one growth-only foreground rehash into a dedicated transaction.

        The detached generation is built later by the ordinary commit path, after the first
        OCC pass and while the writer lease plus ``COMMIT_SECTION`` are held.  Catalog v2 keeps
        the former ACTIVE generation as STALE.  A v1 automatic exact index is coactivated with
        catalog v2 in the same commit, replacing its not-yet-built migration generation so the
        target is scanned exactly once.
        """

        operation = "prepare exact-index rehash"
        self._require_fresh_index_catalog_transaction(
            txn,
            operation=operation,
            purpose="Exact-index rehash",
        )

        with self._participant_section():
            self._require_not_closed(operation)
            self._require_current_active(txn)
            self._require_recovery_complete()
            with self.schema_artifact_section(sync_if=lambda: True):
                source = getattr(self._catalog, "catalog", None)
                if not isinstance(source, Catalog):
                    raise GrafxUnsupportedOperation(
                        "Exact-index rehash needs the concrete persistent catalog.",
                        operation=operation,
                        field="catalog",
                        value=type(source).__name__,
                    )
                manager = self._index_manager
                if manager is None:
                    raise GrafxUnsupportedOperation(
                        "Exact-index rehash needs the exact index manager.",
                        operation=operation,
                        field="indexes",
                    )

                published = self._published_state_in_section().last_committed_lsn
                if source.format_version == CATALOG_LEGACY_FORMAT_VERSION:
                    return self._prepare_legacy_index_rehash(
                        txn,
                        source,
                        manager=manager,
                        published_lsn=published,
                        name=name,
                        bucket_count=bucket_count,
                        expected_cardinality=expected_cardinality,
                        operation=operation,
                    )
                if source.format_version != CATALOG_FORMAT_VERSION:
                    raise GrafxSchemaVersionMismatch(
                        f"Catalog format {source.format_version} cannot rehash indexes.",
                        field="format_version",
                        value=source.format_version,
                        supported=CATALOG_FORMAT_VERSION,
                    )

                logical = source.index_definition(name)
                active = logical.active_generation()
                if active is None:
                    raise GrafxIndexError(
                        f"Index {logical.name!r} has no ACTIVE generation to rehash.",
                        operation=operation,
                        field="generation_state",
                        value=None,
                        index=logical.name,
                    )
                # Resolve and open the exact catalog-selected store before allocating a nonce.
                # This proves that the descriptor names a complete physical definition; a stale
                # freshness flag is deliberately repairable by the full shadow build.
                selected = manager.active_index(logical.name, catalog=source)
                selected.open()
                resolved_count, resolved_expected = rehash_index_sizing(
                    active.bucket_count,
                    bucket_count=bucket_count,
                    expected_cardinality=expected_cardinality,
                )

                candidate = Catalog.deserialize(source.serialize())
                candidate_logical = candidate.index_definition(logical.name)
                occupied = self._index_generation_nonces(candidate)
                nonce = manager._allocate_detached_generation_nonce(occupied)
                shadow = IndexGenerationDescriptor(
                    artifact_nonce=nonce,
                    bucket_count=resolved_count,
                    state=IndexGenerationState.BUILDING,
                )
                replacement = (
                    replace(
                        candidate_logical,
                        expected_cardinality=resolved_expected,
                    )
                    .with_generation(shadow)
                    .activate_generation(nonce)
                )
                # Keep the immediately retired generation as the rollback/audit descriptor,
                # but do not grow the catalog with every historical rehash.  Older files remain
                # untouched on storage and become ordinary retained orphans; nonce allocation
                # also inventories physical files, so their identities are never reused.
                replacement = replace(
                    replacement,
                    generations=tuple(
                        sorted(
                            (
                                replacement.generation(active.artifact_nonce),
                                replacement.generation(nonce),
                            ),
                            key=lambda generation: generation.artifact_nonce,
                        )
                    ),
                )
                candidate.replace_index_definition(replacement)
                table = candidate.table_by_id(replacement.table_id)
                self._declare_complete_table_reads(txn, {table.table_id: table})
                candidate.serialize()
                self._stage_index_catalog_activation_plan(
                    txn,
                    candidate,
                    (replacement.runtime_definition(replacement.generation(nonce)),),
                    published,
                    operation=operation,
                )
                return replacement

    def _prepare_legacy_index_rehash(
        self,
        txn: TransactionContext,
        source: Catalog,
        *,
        manager: object,
        published_lsn: Lsn,
        name: str,
        bucket_count: int | None,
        expected_cardinality: int | None,
        operation: str,
    ) -> CatalogIndexDefinition:
        """Compose one v1 automatic exact rehash with the one-way v2 activation."""

        key = name.lower() if isinstance(name, str) else ""
        legacy = next(
            (
                definition
                for definition in self._automatic_exact_activation_definitions(source)
                if definition.registry_key == key
            ),
            None,
        )
        if legacy is None:
            raise GrafxIndexError(
                f"Catalog v1 has no automatic exact index named {name!r} to rehash.",
                operation=operation,
                field="index_authority",
                value=repr(name),
                index=repr(name),
            )
        # v1 has no durable logical custom-index authority.  Only a schema-derived automatic
        # definition selected above can cross the compatibility fence through this operation.
        selected = manager.active_index(legacy.name, catalog=source)
        selected.open()
        resolved_count, resolved_expected = rehash_index_sizing(
            legacy.bucket_count,
            bucket_count=bucket_count,
            expected_cardinality=expected_cardinality,
        )

        activation = self._plan_identity_index_activation(
            txn,
            source,
            published_lsn,
            retain_noop_candidate=True,
        )
        assert activation is not None
        candidate, runtime_definitions = activation
        planned = candidate.index_definition(legacy.name)
        planned_generation = planned.active_generation()
        if planned_generation is None:
            raise GrafxCorruptionDetected(
                f"Catalog-v2 activation did not plan an ACTIVE generation for {legacy.name!r}.",
                operation=operation,
                field="generation_state",
                index=legacy.name,
            )
        resized_generation = replace(
            planned_generation,
            bucket_count=resolved_count,
        )
        replacement = replace(
            planned,
            expected_cardinality=resolved_expected,
            generations=(resized_generation,),
        )
        candidate.replace_index_definition(replacement)
        resized_runtime = replacement.runtime_definition(resized_generation)
        complete_runtime = tuple(
            resized_runtime
            if definition.registry_key == legacy.registry_key
            else definition
            for definition in runtime_definitions
        )
        if (
            sum(
                definition.registry_key == legacy.registry_key
                for definition in runtime_definitions
            )
            != 1
        ):
            raise GrafxCorruptionDetected(
                f"Catalog-v2 activation planned an ambiguous target for {legacy.name!r}.",
                operation=operation,
                field="index_authority",
                index=legacy.name,
            )
        candidate.serialize()
        self._stage_index_catalog_activation_plan(
            txn,
            candidate,
            complete_runtime,
            published_lsn,
            operation=operation,
        )
        return replacement

    @staticmethod
    def _validate_custom_exact_index_request(
        source: Catalog,
        *,
        manager: object,
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        bucket_count: int,
        expected_cardinality: int | None,
        operation: str,
    ) -> tuple[TableDef, CatalogIndexDefinition]:
        """Validate one custom definition without changing catalog, txn or nonce state."""

        if not isinstance(table_name, str):
            raise GrafxConfigurationError(
                "A custom exact index must name one committed node table.",
                operation=operation,
                field="table",
                value=repr(table_name),
            )
        table = source.table(table_name)
        if table.kind != "node":
            raise GrafxUnsupportedOperation(
                f"Custom exact index {name!r} cannot target relationship table "
                f"{table.name!r}.",
                operation=operation,
                field="table",
                value=table.name,
                table=table.name,
                kind=table.kind,
            )

        # A harmless placeholder nonce lets the generation descriptor validate bucket sizing
        # before the real provider is consulted.  It is never inserted into any catalog.
        placeholder = IndexGenerationDescriptor(
            artifact_nonce=1,
            bucket_count=bucket_count,
            state=IndexGenerationState.ACTIVE,
        )
        provisional = CatalogIndexDefinition(
            name=name,
            table_id=table.table_id,
            table_name=table.name,
            positions=positions,
            visibility=IndexVisibility.EXACT,
            automatic=False,
            expected_cardinality=expected_cardinality,
            generations=(placeholder,),
        )
        for position in provisional.positions:
            if position >= len(table.columns):
                raise GrafxConfigurationError(
                    f"Index {provisional.name!r} position {position} is outside node table "
                    f"{table.name!r}, whose stored arity is {len(table.columns)}.",
                    operation=operation,
                    field="positions",
                    value=position,
                    index=provisional.name,
                    table=table.name,
                )
        if source.has_index_definition(provisional.name):
            raise GrafxConfigurationError(
                f"Catalog index name {provisional.name!r} is already defined without regard "
                "to case.",
                operation=operation,
                field="name",
                value=provisional.name,
                index=provisional.name,
            )

        indexes = getattr(manager, "indexes", None)
        if callable(indexes) and any(
            getattr(getattr(index, "definition", None), "registry_key", None)
            == provisional.registry_key
            for index in indexes()
        ):
            raise GrafxConfigurationError(
                f"Index name {provisional.name!r} is already registered without regard to "
                "case.",
                operation=operation,
                field="name",
                value=provisional.name,
                index=provisional.name,
            )

        automatic_names = {
            definition.registry_key
            for known_table in source.tables()
            for definition in automatic_index_definitions(known_table)
        }
        if provisional.registry_key in automatic_names:
            raise GrafxConfigurationError(
                f"Custom index {provisional.name!r} collides with the reserved "
                "schema-derived automatic index namespace.",
                operation=operation,
                field="name",
                value=provisional.name,
                index=provisional.name,
            )
        return table, provisional

    def _stage_index_catalog_activation_plan(
        self,
        txn: TransactionContext,
        candidate: Catalog,
        runtime_definitions: tuple[IndexDefinition, ...],
        published_lsn: Lsn,
        *,
        operation: str,
    ) -> None:
        """Admit and seal one catalog candidate without constructing physical shadows."""

        self._validate_index_build_entry_budget(
            runtime_definitions,
            published_lsn,
            txn_id=txn.txn_id,
            operation=operation,
        )
        staged = self._catalog.stage(candidate)
        for page_index, image in staged:
            self._stage_page_image(
                txn,
                self._file_ids.catalog_file,
                page_index,
                image,
            )
        self._index_catalog_activation_plans[txn.txn_id] = _IndexCatalogActivationPlan(
            runtime_definitions,
            tuple(sorted(txn.page_images.items())),
            frozenset(txn.read_partitions),
            frozenset(txn.write_partitions),
        )

    def _validate_index_build_entry_budget(
        self,
        definitions: Sequence[IndexDefinition],
        through_lsn: Lsn,
        *,
        txn_id: TxnId,
        operation: str = "prepare identity-index activation",
    ) -> None:
        """Refuse an oversized activation batch before staging catalog or index bytes."""

        limit = self._max_index_build_entries
        if limit is None:
            return
        count = getattr(
            self._index_manager,
            "_count_detached_exact_generation_entries",
            None,
        )
        if not callable(count):
            raise GrafxUnsupportedOperation(
                "Index-build admission needs exact detached-generation accounting.",
                operation=operation,
                field="indexes",
                value=type(self._index_manager).__name__,
            )
        observed = 0
        for definition in definitions:
            observed += count(
                definition,
                through_lsn,
                remaining=limit - observed,
            )
            if observed > limit:
                raise GrafxTransactionBudgetExceeded(
                    f"Index shadow-build batch for transaction {txn_id} would exceed "
                    f"max_index_build_entries: limit {limit}, observed {observed}.",
                    field="max_index_build_entries",
                    limit=limit,
                    observed=observed,
                    txn_id=txn_id,
                )

    def _plan_identity_index_activation(
        self,
        txn: TransactionContext,
        source: Catalog,
        published_lsn: Lsn,
        *,
        retain_noop_candidate: bool = False,
    ) -> tuple[Catalog, tuple[IndexDefinition, ...]] | None:
        """Return a detached catalog candidate and every generation it must build."""

        candidate = Catalog.deserialize(source.serialize())
        endpoint_tables = self._identity_endpoint_tables(source)
        occupied = self._index_generation_nonces(source)
        runtime_definitions: list[IndexDefinition] = []

        def allocate() -> int:
            nonce = self._index_manager._allocate_detached_generation_nonce(occupied)
            # Allocation is discovery rather than reservation.  Remembering the result in this
            # plan is therefore mandatory: two planned shadows must never be offered one nonce.
            occupied.add(nonce)
            return nonce

        if source.format_version == CATALOG_LEGACY_FORMAT_VERSION:
            logical_definitions: list[CatalogIndexDefinition] = []
            indexed_tables: dict[int, TableDef] = {}
            for definition in self._automatic_exact_activation_definitions(source):
                if self._automatic_index_expected_cardinality is not None:
                    definition = replace(
                        definition,
                        bucket_count=self._automatic_index_bucket_count,
                    )
                table = source.table_by_id(definition.table_id)
                nonce = allocate()
                generation = IndexGenerationDescriptor(
                    artifact_nonce=nonce,
                    bucket_count=definition.bucket_count,
                    state=IndexGenerationState.ACTIVE,
                )
                logical = CatalogIndexDefinition(
                    name=definition.name,
                    table_id=definition.table_id,
                    table_name=definition.table_name,
                    positions=definition.positions,
                    visibility=definition.visibility,
                    key_derivation=definition.key_derivation,
                    automatic=True,
                    expected_cardinality=(self._automatic_index_expected_cardinality),
                    generations=(generation,),
                )
                logical_definitions.append(logical)
                runtime_definitions.append(logical.runtime_definition(generation))
                indexed_tables[table.table_id] = table

            snapshot = Snapshot(published_lsn)
            for table in endpoint_tables:
                visible_rows = sum(
                    1 for _ref, _version in self._heap.scan(table, snapshot)
                )
                expected, bucket_count = identity_index_sizing(
                    visible_rows,
                    expected_cardinality=self._automatic_index_expected_cardinality,
                )
                nonce = allocate()
                generation = IndexGenerationDescriptor(
                    artifact_nonce=nonce,
                    bucket_count=bucket_count,
                    state=IndexGenerationState.ACTIVE,
                )
                logical = CatalogIndexDefinition(
                    name=identity_index_name(table.table_id),
                    table_id=table.table_id,
                    table_name=table.name,
                    positions=(),
                    visibility=IndexVisibility.EXACT,
                    key_derivation=RECORD_ID_KEY_DERIVATION,
                    automatic=True,
                    expected_cardinality=expected,
                    generations=(generation,),
                )
                logical_definitions.append(logical)
                runtime_definitions.append(logical.runtime_definition(generation))
                indexed_tables[table.table_id] = table

            self._declare_complete_table_reads(txn, indexed_tables)
            candidate.upgrade_index_catalog(logical_definitions)
            return candidate, tuple(runtime_definitions)

        if source.format_version != CATALOG_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"Catalog format {source.format_version} cannot activate identity indexes.",
                field="format_version",
                value=source.format_version,
                supported=CATALOG_FORMAT_VERSION,
            )

        changed_tables: dict[int, TableDef] = {}
        snapshot = Snapshot(published_lsn)
        for table in endpoint_tables:
            name = identity_index_name(table.table_id)
            try:
                logical = source.index_definition(name)
            except GrafxConfigurationError:
                # A valid stored v2 catalog normally cannot take this branch.  Keeping the
                # explicit repair shape is useful for a candidate that gained a relationship in
                # the same future DDL protocol, while validation still owns the final verdict.
                logical = None

            active = None if logical is None else logical.active_generation()
            needs_generation = active is None
            if active is not None:
                # Existing-only synchronization in schema_artifact_section has already resolved
                # this exact catalog generation.  Absence or physical-definition mismatch must
                # propagate fail-closed; only a well-formed but stale generation is rebuildable.
                index = self._index_manager.active_index(name, catalog=source)
                required = self._heap.committed_high_water(table)
                needs_generation = bool(
                    index.check_freshness(
                        published_lsn,
                        required_lsn=required,
                        persist=False,
                    )
                )
            if not needs_generation:
                continue

            visible_rows = sum(1 for _ref, _version in self._heap.scan(table, snapshot))
            expected, sized_bucket_count = identity_index_sizing(
                visible_rows,
                expected_cardinality=self._automatic_index_expected_cardinality,
            )
            if logical is None:
                bucket_count = sized_bucket_count
                generations: tuple[IndexGenerationDescriptor, ...] = ()
                expected_cardinality = expected
            else:
                bucket_count = (
                    active.bucket_count
                    if active is not None
                    else max(
                        generation.bucket_count for generation in logical.generations
                    )
                    if logical.generations
                    else sized_bucket_count
                )
                generations = tuple(
                    generation.mark_stale() for generation in logical.generations
                )
                expected_cardinality = logical.expected_cardinality

            nonce = allocate()
            generation = IndexGenerationDescriptor(
                artifact_nonce=nonce,
                bucket_count=bucket_count,
                state=IndexGenerationState.ACTIVE,
            )
            replacement = CatalogIndexDefinition(
                name=name,
                table_id=table.table_id,
                table_name=table.name,
                positions=(),
                visibility=IndexVisibility.EXACT,
                key_derivation=RECORD_ID_KEY_DERIVATION,
                automatic=True,
                expected_cardinality=expected_cardinality,
                generations=tuple(
                    sorted(
                        (*generations, generation),
                        key=lambda item: item.artifact_nonce,
                    )
                ),
            )
            if logical is None:
                candidate.add_index_definition(replacement)
            else:
                candidate.replace_index_definition(replacement)
            runtime_definitions.append(replacement.runtime_definition(generation))
            changed_tables[table.table_id] = table

        if not runtime_definitions and not retain_noop_candidate:
            return None
        self._declare_complete_table_reads(txn, changed_tables)
        # Serialize now, before staging, so complete v2 authority validation cannot be deferred
        # until after a detached file has been built.
        candidate.serialize()
        return candidate, tuple(runtime_definitions)

    def _automatic_exact_activation_definitions(
        self, catalog: Catalog
    ) -> tuple[IndexDefinition, ...]:
        """Choose one deterministic v1 automatic path for each physical registry name.

        Table names are case-sensitive while index files and registry keys are not.  A valid v1
        catalog can therefore contain ``Person`` and ``person`` while only one of ``pk_Person``
        and ``pk_person`` is attachable; the other table deliberately uses the heap fallback.
        Catalog v2 preserves that supported state by promoting the registered winner, or the
        first table-id/name candidate when no process-local winner exists, rather than refusing
        the whole migration or silently swapping which table is accelerated.
        """

        grouped: dict[str, list[IndexDefinition]] = {}
        for table in catalog.tables():
            for definition in automatic_index_definitions(table):
                if definition.visibility is IndexVisibility.EXACT:
                    grouped.setdefault(definition.registry_key, []).append(definition)

        registered: dict[str, IndexDefinition] = {}
        indexes = getattr(self._index_manager, "indexes", None)
        if callable(indexes):
            registered = {
                index.definition.registry_key: index.definition for index in indexes()
            }

        selected: list[IndexDefinition] = []
        for key in sorted(grouped):
            candidates = sorted(
                grouped[key],
                key=lambda item: (item.table_id, item.table_name, item.name),
            )
            incumbent = registered.get(key)
            winner = next(
                (
                    candidate
                    for candidate in candidates
                    if incumbent is not None
                    and type(incumbent) is type(candidate)
                    and incumbent.name == candidate.name
                    and incumbent.table_id == candidate.table_id
                    and incumbent.table_name == candidate.table_name
                    and incumbent.positions == candidate.positions
                    and incumbent.visibility is candidate.visibility
                    and incumbent.key_derivation == candidate.key_derivation
                ),
                None,
            )
            if winner is not None:
                assert incumbent is not None
                selected.append(incumbent)
            else:
                selected.append(candidates[0])
        return tuple(selected)

    def _index_generation_nonces(self, catalog: Catalog) -> set[int]:
        """Return every catalog or legacy-artifact nonce that this activation must avoid."""

        occupied = {
            generation.artifact_nonce
            for definition in catalog.index_definitions()
            for generation in definition.generations
        }
        projected = getattr(self._index_manager, "_registered_artifact_nonces", None)
        if callable(projected):
            occupied.update(projected())
        else:
            # Compatibility for narrow/alternative registry implementations predating the
            # projection.  Their definitions cannot be assumed to carry catalog-v2 authority,
            # so retain the validating header route they implemented.
            indexes = getattr(self._index_manager, "indexes", None)
            if callable(indexes):
                for index in indexes():
                    header = index.open()
                    if header.artifact_nonce:
                        occupied.add(header.artifact_nonce)
        return occupied

    @staticmethod
    def _identity_endpoint_tables(catalog: Catalog) -> tuple[TableDef, ...]:
        """Return each node table referenced by a relationship exactly once."""

        by_name = {table.name: table for table in catalog.tables()}
        selected: dict[int, TableDef] = {}
        for relation in catalog.tables():
            if relation.kind != "rel":
                continue
            for name in (relation.from_table, relation.to_table):
                table = by_name.get(str(name))
                if table is None or table.kind != "node":
                    raise GrafxCorruptionDetected(
                        f"Relationship {relation.name!r} points at missing or non-node "
                        f"endpoint {name!r}.",
                        field="endpoint",
                        value=name,
                        table=relation.name,
                    )
                selected[table.table_id] = table
        return tuple(selected[key] for key in sorted(selected))

    def _declare_complete_table_reads(
        self, txn: TransactionContext, tables: dict[int, TableDef]
    ) -> None:
        """Fence every row partition of each heap table a detached build will scan."""

        for table_id in sorted(tables):
            for partition in range(self._partitions_per_table):
                txn.note_read(partition_key(table_id, partition))

    def _build_index_catalog_activation(
        self, txn: TransactionContext, through_lsn: Lsn
    ) -> None:
        """Durably build planned shadows while commit owns writer and publication fences."""

        plan = self._index_catalog_activation_plans.get(txn.txn_id)
        if plan is None:
            return
        self._validate_index_catalog_activation_plan(txn, plan)
        if plan.state == "built":
            return
        if plan.state == "failed":
            raise GrafxTransactionStateError(
                "A failed detached index-catalog plan cannot be reused; roll back and retry "
                "with new generation nonces.",
                operation="build detached index-catalog activation",
                txn_id=txn.txn_id,
            )
        try:
            for definition in plan.definitions:
                self._index_manager._build_detached_exact_generation(
                    definition,
                    through_lsn,
                )
        except BaseException:
            plan.state = "failed"
            raise
        plan.state = "built"

    @staticmethod
    def _validate_index_catalog_activation_plan(
        txn: TransactionContext,
        plan: _IndexCatalogActivationPlan,
    ) -> None:
        """Refuse any work added to the private activation transaction after planning.

        Detached generations describe only the fenced durable heap.  Letting the same
        transaction add a row afterwards would materialise that row provisionally before the
        shadow build, where it is intentionally invisible, and could publish an ACTIVE index
        that omits its own commit.  Every identity/custom catalog-build door requires a fresh
        dedicated transaction; this seal is the defensive invariant at the manager boundary.
        """

        if (
            txn.row_intents
            or txn.pending_records
            or txn.row_refs
            or txn._staging_marks
            or txn._pending_row_refs
            or txn._effective_row_tables not in (None, frozenset())
            or tuple(sorted(txn.page_images.items())) != plan.page_images
            or frozenset(txn.read_partitions) != plan.read_partitions
            or frozenset(txn.write_partitions) != plan.write_partitions
        ):
            raise GrafxTransactionStateError(
                "A detached index-catalog transaction was modified after its shadow plan was "
                "sealed; roll it back and retry the catalog build by itself.",
                operation="build detached index-catalog activation",
                field="activation_transaction",
                txn_id=txn.txn_id,
            )

    def published_state(self) -> CommitState:
        """Return the published commit state, or an empty one when nothing was ever published."""
        self._require_not_closed("read published state")
        with self._participant_section():
            self._require_not_closed("read published state")
            return self._published_state_in_section()

    def _published_state_in_section(self) -> CommitState:
        """Read published state for an operation that already owns lifecycle serialisation."""
        self._require_recovery_complete()
        durable = self._read_commit_state()
        if durable.last_committed_lsn >= self._published_high_water:
            return durable
        # This process committed durably and then failed to publish. The log is the authority on
        # what is committed (section 8.5 step 3.5), so the number this manager knows outranks the
        # file it could not write -- otherwise its own next transaction would be handed a
        # snapshot below a commit it has already acknowledged.
        return CommitState(
            last_committed_lsn=self._published_high_water,
            last_csn=self._published_high_water,
            checkpoint_lsn=durable.checkpoint_lsn,
            format_version=durable.format_version,
        )

    @staticmethod
    def _read_view_token_of(state: CommitState) -> _ReadViewToken:
        """Return the complete durable identity of one cache read view."""

        return _ReadViewToken(
            last_committed_lsn=state.last_committed_lsn,
            checkpoint_lsn=state.checkpoint_lsn,
        )

    def _read_view_changes(
        self,
        previous: _ReadViewToken,
        current: _ReadViewToken,
    ) -> _ReadViewChanges | None:
        """Prove a bounded committed WAL delta, or decline the optimisation.

        ``None`` is deliberately not an error: it tells BufferPool to retain the conservative
        full refresh.  The proof is accepted only when the exact contiguous interval after the
        previous publication is still retained, ends at the current COMMIT, contains only record
        kinds this build can classify, and fits all three internal work budgets.  No commit,
        checkpoint, recovery or WAL-format rule is weakened by this fast path.

        Every supported mutation of an existing index header that has no logical index record is
        currently co-published with a heap ``WRITE_PAGE`` (sparse and missing observations are the
        concrete cases).  A future path that can move such a header without a heap write must add
        its physical target here or make this proof decline to the full refresh.
        """

        if (
            previous.checkpoint_lsn != current.checkpoint_lsn
            or current.last_committed_lsn <= previous.last_committed_lsn
        ):
            return None
        read_bounded = getattr(self._wal, "read_bounded", None)
        if not callable(read_bounded):
            return None
        first = previous.last_committed_lsn + 1
        through = current.last_committed_lsn
        try:
            records = read_bounded(
                first,
                through,
                max_records=_READ_VIEW_MAX_RECORDS,
                max_bytes=_READ_VIEW_MAX_BYTES,
            )
            if (
                not isinstance(records, tuple)
                or not records
                or len(records) > _READ_VIEW_MAX_RECORDS
                or not all(isinstance(record, WalRecord) for record in records)
            ):
                return None
            if any(
                record.lsn != first + offset for offset, record in enumerate(records)
            ):
                return None
            if records[-1].lsn != through:
                return None
            if records[-1].record_type != int(WalRecordType.COMMIT):
                return None
            if any(
                record.record_type not in _READ_VIEW_DELTA_TYPES for record in records
            ):
                return None

            replay = committed_replay(records)
            if replay.incomplete_effects or replay.last_committed_lsn != through:
                return None

            pages: set[tuple[str, PageIndex]] = set()
            files: set[str] = set()
            heap_changed = False
            catalog_changed = False
            for record in replay.effects:
                if record.record_type == int(WalRecordType.WRITE_PAGE):
                    write = decode_page_write_location(record.payload)
                    if (
                        not isinstance(write.file, str)
                        or not write.file
                        or "\x00" in write.file
                    ):
                        return None
                    pages.add((write.file, write.page_index))
                    heap_changed = heap_changed or write.file == self._heap_file
                    catalog_changed = (
                        catalog_changed or write.file == self._file_ids.catalog_file
                    )
                    continue
                if record.record_type not in (
                    int(WalRecordType.INDEX_WRITE),
                    int(WalRecordType.INDEX_RECONCILE),
                ):
                    return None
                manager = self._index_manager
                if manager is None:
                    return None
                change = change_of(record)
                active_index = getattr(manager, "active_index", manager.index)
                index_file = getattr(active_index(change.index), "file", None)
                if (
                    not isinstance(index_file, str)
                    or not index_file
                    or "\x00" in index_file
                ):
                    return None
                files.add(index_file)

            if heap_changed:
                # A sparse index observation intentionally emits no INDEX_WRITE: there is no
                # entry to replay.  Its live apply still advances the index coverage certificate,
                # which changes page 0.  The WAL delta therefore has to name that implicit
                # physical effect or a later participant can retain an older clean header until
                # its own post-barrier flush discovers the page-0 CAS conflict.  Do not infer the
                # affected tables from caller-declared logical partitions -- that set is an OCC
                # interest, not a complete row-intent certificate.  One header per registered
                # index is the bounded conservative proof; explicit index records above still
                # dominate it by targeting their whole file.
                manager = self._index_manager
                if manager is not None:
                    indexes_of = getattr(
                        manager, "active_indexes", getattr(manager, "indexes", None)
                    )
                    if not callable(indexes_of):
                        return None
                    indexes = indexes_of()
                    if (
                        not isinstance(indexes, tuple)
                        or len(indexes) > _READ_VIEW_MAX_TARGETS
                    ):
                        return None
                    for index in indexes:
                        index_file = getattr(index, "file", None)
                        if (
                            not isinstance(index_file, str)
                            or not index_file
                            or "\x00" in index_file
                        ):
                            return None
                        pages.add((index_file, HEADER_PAGE_INDEX))

            # The catalog has no page-zero freshness certificate and is therefore always a
            # whole-file target when a foreign publication moves.  Count it in the proof budget
            # even though BufferPool also adds it defensively at the mutation boundary.
            budget_files = files | {self._file_ids.catalog_file}
            pages = {key for key in pages if key[0] not in budget_files}
            if len(budget_files) + len(pages) > _READ_VIEW_MAX_TARGETS:
                return None
            return _ReadViewChanges(
                pages=frozenset(pages),
                files=frozenset(files),
                catalog_changed=catalog_changed,
            )
        except (AttributeError, GrafxError, KeyError, OSError, TypeError, ValueError):
            # A stale/shape-incompatible optional collaborator, recycled interval, malformed
            # payload, storage race or WAL damage can only disable the optimisation. The existing
            # full refresh remains authoritative; process-control failures still propagate.
            return None

    def _establish_read_view(
        self,
        state: CommitState,
        *,
        own: bool,
        allow_writeback: bool = True,
    ) -> bool:
        """Attach the pool and report whether foreign index authority may have changed.

        A proved CE-3 interval distinguishes ordinary heap/index DML from a catalog write.  If
        the interval cannot be proved, a moved foreign token or the first unbased view is
        conservatively treated as a possible catalog change.  The caller uses this one-bit
        answer to refresh the process-local index registry only at an authority boundary, never
        after every provable foreign DML.
        """

        token = self._read_view_token_of(state)
        changes: _ReadViewChanges | None = None
        catalog_may_have_changed = False
        with self._close_wait_hazard():
            previous = self._pool.read_view_token()
            # Composition attaches the initial registry before a BufferPool read-view token
            # exists.  A foreign publication may land between those two events, so the first
            # transactional/non-transactional view has no baseline from which it can prove that
            # catalog authority stayed put and must take the conservative one-time sync.
            catalog_may_have_changed = not isinstance(previous, _ReadViewToken)
            effective_own = (
                own
                and isinstance(previous, _ReadViewToken)
                and previous.checkpoint_lsn == token.checkpoint_lsn
            )
            if (
                not effective_own
                and isinstance(previous, _ReadViewToken)
                and previous != token
            ):
                changes = self._read_view_changes(previous, token)
                catalog_may_have_changed = changes is None or changes.catalog_changed
            # A WAL-only composition may retain a resident catalog when its concrete base did
            # not move, when this manager demonstrably published the new view, or when a complete
            # CE-3 interval proves that no catalog page changed.  Legacy/direct compositions
            # keep catalog.dat unfenced on every view, preserving their historical visibility
            # rule.  A first view and every moved foreign view whose CE-3 proof declines likewise
            # retain the conservative whole-file refresh.
            catalog_view_is_proved = (
                isinstance(previous, _ReadViewToken)
                and (previous == token or effective_own)
            ) or (changes is not None and not changes.catalog_changed)
            unfenced_catalog = (
                None
                if self._catalog_changes_are_wal_logged and catalog_view_is_proved
                else self._file_ids.catalog_file
            )
            if allow_writeback:
                # Preserve the established call shape for deliberately narrow BufferPool test
                # doubles and custom compositions.
                self._pool.begin_read_view(
                    token,
                    own=effective_own,
                    unfenced_file=unfenced_catalog,
                    changed_pages=None if changes is None else changes.pages,
                    changed_files=() if changes is None else changes.files,
                    expected_previous=previous,
                )
            else:
                self._pool.begin_read_view(
                    token,
                    own=effective_own,
                    allow_writeback=False,
                    unfenced_file=unfenced_catalog,
                    changed_pages=None if changes is None else changes.pages,
                    changed_files=() if changes is None else changes.files,
                    expected_previous=previous,
                )
        if not effective_own:
            # A foreign view consumes any previous own-publication provenance. A later numeric
            # coincidence is not proof that the resident frames came from this participant.
            self._own_published_lsn = None
            prefix = self._local_applied_prefix
            checkpoint_seed_is_current = (
                prefix is not None
                and prefix.checkpoint_lsn == state.checkpoint_lsn
                and prefix.applied_through_lsn == state.last_committed_lsn
                and state.checkpoint_lsn == state.last_committed_lsn
            )
            if not checkpoint_seed_is_current:
                self._local_applied_prefix = None
        return catalog_may_have_changed or self._index_authority_sync_required

    def _synchronize_read_index_authority(self, published_lsn: Lsn) -> None:
        """Adopt exact-index authority changed by a foreign catalog publication.

        Catalog pages are already attached to the selected read view.  Refreshing their derived
        object before the existing-only callback makes a long-lived handle replace a retired
        physical generation before its next statement is planned.  The callback never creates
        an artifact; a missing or malformed catalog-selected file therefore remains fail-closed.
        """

        retry_required = self._index_authority_sync_required
        self._index_authority_sync_required = True
        with self._close_wait_hazard():
            image_of = getattr(self._catalog, "persisted_image", None)
            before = image_of() if callable(image_of) else None
            refresh = getattr(self._catalog, "refresh", None)
            if callable(refresh):
                refresh()
            after = image_of() if callable(image_of) else None
            observe = getattr(self._index_manager, "observe_published_lsn", None)
            if callable(observe):
                observe(published_lsn)

            # A recycled/oversized WAL interval or checkpoint movement makes CE-3 decline even
            # when catalog.dat did not change.  Its immutable before/after bytes are then a
            # cheaper complete authority proof than listing index/ and reopening every header.
            # A prior failed sync is never skipped by equality: it remains latched until the
            # existing-only callback has successfully adopted every selected generation.
            authority_unchanged = (
                not retry_required
                and before is not None
                and after is not None
                and before == after
            )
            if not authority_unchanged:
                # A real or unprovable registry boundary revokes local replay provenance.
                # Byte-identical immutable catalog authority performs no sync and may retain an
                # exact checkpoint seed; eviction of that advisory seed would be harmless but
                # would make every newly completed checkpoint unusable on its first statement.
                self._local_applied_prefix = None
            if self._index_sync is not None and not authority_unchanged:
                self._index_sync()
            self._refresh_heap_reclaim_capability()
            self._refresh_wal_record_v2_capability()
        self._index_authority_sync_required = False

    def _refresh_heap_reclaim_capability(self) -> None:
        """Cache the one-way catalog fence so legacy begins retain their zero-I/O hot path."""

        source = getattr(self._catalog, "catalog", None)
        self._heap_reclaim_capable = bool(
            isinstance(source, Catalog)
            and source.format_version == CATALOG_FORMAT_VERSION
            and source.requires_capability(HEAP_RECLAIM_V1_CAPABILITY)
        )

    def _refresh_wal_record_v2_capability(self) -> None:
        """Cache the persisted WAL-v2 fence without adding I/O to commit materialisation."""

        source = getattr(self._catalog, "catalog", None)
        self._wal_record_v2_capable = bool(
            isinstance(source, Catalog)
            and source.format_version == CATALOG_FORMAT_VERSION
            and source.requires_capability(WAL_RECORD_V2_CAPABILITY)
        )

    def _synchronize_committed_indexes(
        self,
        txn: TransactionContext,
        published_lsn: Lsn,
        *,
        authority_may_have_changed: bool = True,
    ) -> None:
        """Adopt foreign DDL before a row commit can build its WAL batch.

        CatalogStore refreshes its derived catalog when a foreign read view drops the catalog
        frames, but the index registry is process-local.  A participant opened before that DDL
        could therefore resolve and materialise the new table while staging no index record at
        all.  With no registered object, IndexManager's missing-observation guard also had
        nowhere to persist a stale verdict; a cold recovery could then certify the short file as
        a complete replay.

        This door runs only inside the existing cross-process COMMIT_SECTION, after the first
        OCC pass has accepted the transaction's original snapshot and before provenance checks
        or row materialisation.  It creates no index: production's callback is the existing-only
        composition route.  The final inventory proof is deliberately pre-WAL and limited to
        committed tables this transaction writes, so speculative indexes of another local DDL
        transaction remain registered and a deliberately unindexed table keeps its scan fallback.
        """

        reduced_intents = reduce_row_intents(txn.row_intents)
        if not reduced_intents:
            # A pre-staged physical/catalog transaction has no heap/index effect to protect.
            # In particular, do not interpret pages it deliberately staged before the first OCC
            # pass has had the opportunity to reject a foreign winner at the original snapshot.
            return

        manager = self._index_manager
        catalog = self._catalog
        if authority_may_have_changed:
            refresh = getattr(catalog, "refresh", None)
            if callable(refresh):
                refresh()
            observe = getattr(manager, "observe_published_lsn", None)
            if callable(observe):
                observe(published_lsn)
            if self._index_sync is not None:
                self._index_sync()
        if manager is None:
            return

        committed_catalog = getattr(catalog, "catalog", None)
        tables_of = getattr(committed_catalog, "tables", None)
        if not callable(tables_of):
            return
        written_table_identities = {
            (
                getattr(intent.table, "table_id", None),
                getattr(intent.table, "name", None),
            )
            for intent in reduced_intents
        }
        written_tables = tuple(
            table
            for table in tables_of()
            if (
                getattr(table, "table_id", None),
                getattr(table, "name", None),
            )
            in written_table_identities
        )
        missing_for = getattr(manager, "unregistered_persistent_indexes_for", None)
        if not written_tables or not callable(missing_for):
            return
        try:
            missing = tuple(missing_for(written_tables, catalog=committed_catalog))
        except TypeError:
            # Compatibility collaborators may still expose the pre-authority signature.
            missing = tuple(missing_for(written_tables))
        if not missing:
            return
        raise GrafxTransactionStateError(
            "A row commit reached a committed table whose persistent automatic index exists "
            "but is still absent from this process's registry. The commit was refused before "
            "WAL append so recovery can never certify an omitted index effect.",
            field="index_registry",
            indexes=list(missing),
            table_ids=sorted(getattr(table, "table_id") for table in written_tables),
            txn_id=txn.txn_id,
        )

    def _staged_artifact_conflict(
        self, txn: TransactionContext
    ) -> tuple[int, ...] | None:
        """Translate displaced physical index ownership into ordinary pre-WAL OCC.

        Only the two provenance refusals are retryable conflicts.  Digest, visibility, page or
        structural failures retain their original terminal/corruption classification.
        """
        validate = getattr(self._index_manager, "validate_staged_artifacts", None)
        if not callable(validate):
            return None
        row_tables: dict[tuple[object, object], object] = {}
        for intent in reduce_row_intents(txn.row_intents):
            table = intent.table
            row_tables[
                (getattr(table, "table_id", None), getattr(table, "name", None))
            ] = table
        try:
            validate(txn, row_tables=tuple(row_tables.values()))
        except GrafxIndexError as failure:
            if failure.retryable is not True or failure.details.get("field") not in {
                "index_registry",
                "artifact_nonce",
            }:
                raise
            file = failure.details.get("file")
            if not isinstance(file, str) or not file:
                raise
            return (page_partition(file, HEADER_PAGE_INDEX),)
        return None

    def recyclable_horizon(self) -> Lsn:
        """Return the LSN below which a WAL segment may be recycled (BR-10, CF-11).

        This is the one expression BR-10 is, wired to the two things only this component has
        both of: the reader horizon the coordinator reports and the checkpoint this manager
        publishes. C3 owns the arithmetic and C4 owns the recycling; what was missing was
        anybody joining them, which is why ``WalManager.recycle`` had no caller anywhere and the
        log grew without bound.

        It deliberately does not recycle anything. Choosing WHEN to reclaim is a lifecycle
        decision -- on a checkpoint, on close, on a maintenance pass -- and that belongs to
        whoever owns the lifecycle. This is the number that decision needs.
        """
        self._require_not_closed("read the recyclable horizon")
        with self._participant_section():
            self._require_not_closed("read the recyclable horizon")
            return self._recyclable_horizon_in_section()

    def observational_recyclable_horizon(self) -> Lsn:
        """Return a checkpoint-capped horizon without pruning reader registrations.

        The frozen coordinator port's ordinary ``reader_horizon`` may prune stale records,
        which is correct for WAL lifecycle but not for a zero-write census. The local adapter
        offers a narrower optional observation capability. A custom coordinator without it
        falls back to ``NO_LSN``: less bloat may be reported, but the diagnostic never mutates or
        overstates what an unknown participant set permits.
        """
        self._require_not_closed("observe the recyclable horizon")
        with self._participant_section():
            self._require_not_closed("observe the recyclable horizon")
            observe = getattr(self._coordinator, "observe_reader_horizon", None)
            with self._close_wait_hazard():
                reader_horizon = observe() if callable(observe) else NO_LSN
            return recyclable_horizon(
                reader_horizon,
                self._published_state_in_section().checkpoint_lsn,
            )

    def _recyclable_horizon_in_section(self) -> Lsn:
        """Compute the horizon for an operation that already owns lifecycle serialisation."""
        return recyclable_horizon(
            self._reader_horizon(),
            self._published_state_in_section().checkpoint_lsn,
        )

    def published_lsn(self) -> Lsn:
        """Return the LSN of the last commit this database has published (section 8.5 step 3.2)."""
        return self.published_state().last_committed_lsn

    def assert_recovery_complete(self) -> None:
        """Refuse public work that could touch cached pages while redo is incomplete.

        The latch protects more than publication. A failed post-COMMIT replay can leave an old
        dirty frame in this participant while another participant installs a newer page. Reads
        that cause eviction and explicit flushes can write that old frame too, so the database
        facade uses this assertion before every page-touching operator door.
        """
        self._require_not_closed("assert recovery is complete")
        self._require_recovery_complete()

    @contextmanager
    def page_access_section(
        self,
        *,
        fresh_read_view: bool = False,
        allow_writeback: bool = True,
        transaction: TransactionContext | None = None,
    ) -> Iterator[None]:
        """Keep the recovery latch stable for one page-touching public operation.

        A check performed immediately before a flush, query or verification still leaves a
        scheduling window: another thread can fail after its WAL barrier, latch this participant
        recovery-required, and leave the first thread free to evict or write an older frame. The
        participant section closes that window because every path that can set the latch after a
        durable commit holds the same section. An operation that enters first finishes before the
        latch can be set; one that enters afterwards refuses before touching the pool.

        ``fresh_read_view`` additionally rebases clean cached pages on the latest published LSN.
        Verification needs that boundary because its physical pass reads the device directly
        while its heap and index passes use the resident stores. Without a rebase after a foreign
        commit, one report can compare a current index image with an older cached heap page and
        report damage that disappears on reopen. Ordinary transaction reads establish the same
        view in :meth:`begin`; this option gives non-transactional verification that guarantee
        without opening a synthetic transaction or changing reader/writer concurrency.
        ``allow_writeback=False`` is for observational maintenance: an unproved refresh refuses
        dirty resident state instead of implicitly publishing it.

        ``transaction`` is a private performance hint used only by the public statement door.
        After the first participant access it may retain this exact section's file descriptor in
        an unlocked, identity-revalidated scope.  The current access was acquired before that
        scope exists and stays entirely canonical; later accesses still take and release the
        operating-system lock.  Unknown/custom coordinators simply have no qualifying private
        capability and keep this method's historical path.
        """
        page_access = getattr(self._metrics, "page_access", None)
        boundary = page_access() if callable(page_access) else nullcontext()
        # Publish the cross-thread hazard before even trying the participant section. Close
        # cannot otherwise distinguish the acquire-to-body window from ordinary facade outcome
        # settlement and may wait on a section whose upcoming host callback waits for close.
        with boundary:
            self._require_not_closed("access database pages")
            with self._participant_section():
                self._require_not_closed("access database pages")
                self._require_recovery_complete()
                if fresh_read_view:
                    published = self._published_state_in_section()
                    catalog_may_have_changed = self._establish_read_view(
                        published,
                        own=published.last_committed_lsn == self._own_published_lsn,
                        allow_writeback=allow_writeback,
                    )
                    if catalog_may_have_changed:
                        self._synchronize_read_index_authority(
                            published.last_committed_lsn
                        )
                if transaction is not None:
                    self._retain_transaction_descriptor_scope_in_section(transaction)
                    # The concrete capability is callback-free, but retain the lifecycle guard
                    # beside the optional boundary: a deliberately hostile direct composition
                    # must never turn a re-entrant close into permission to touch a page.
                    self._require_not_closed("access database pages")
                yield

    @contextmanager
    def quiescent_maintenance_section(
        self, *, confirm_quiescent: bool
    ) -> Iterator[None]:
        """Serialize this process and enforce vacuum v1's explicit operator assertion.

        The participant section excludes concurrent threads of this handle for the complete
        foreground operation.  It intentionally does not translate an expired reader TTL into
        proof about another process; only the caller can assert that every other Grafx process,
        including older binaries, has been stopped.
        """

        if confirm_quiescent is not True:
            raise GrafxUnsupportedOperation(
                "MVCC vacuum v1 requires confirm_quiescent=True after every other Grafx "
                "process has been stopped.",
                operation="vacuum",
                field="confirm_quiescent",
                value=repr(confirm_quiescent),
                required=True,
            )
        self._require_not_closed("enter quiescent maintenance")
        self._require_writable("vacuum MVCC history")
        with self._participant_section():
            self._require_not_closed("enter quiescent maintenance")
            self._require_recovery_complete()
            if self._open:
                raise GrafxTransactionStateError(
                    "MVCC vacuum v1 requires this process to have no open user transaction.",
                    operation="vacuum",
                    field="open_transactions",
                    value=len(self._open),
                )
            yield

    @contextmanager
    def schema_artifact_section(
        self, *, sync_if: Callable[[], bool] | None = None
    ) -> Iterator[None]:
        """Serialize DDL artifact attach/reclaim/unwind with every commit publisher.

        The caller already owns this participant's section.  This adds only the same
        cross-process ``COMMIT_SECTION`` used by commit; it is never entered by begin or an
        ordinary read.  Keeping creation, orphan displacement and rollback release in this
        authority closes the namespace race without holding a lock across user transaction time.
        """
        with (
            self._coordinator_section(
                COMMIT_SECTION, timeout=self._commit_lock_timeout
            ),
            self._hold_wal_tail(),
        ):
            durable = self._complete_committed_gap()
            self._establish_read_view(
                durable,
                own=durable.last_committed_lsn == self._own_published_lsn,
            )
            refresh = getattr(self._catalog, "refresh", None)
            if callable(refresh):
                refresh()
            observe = getattr(self._index_manager, "observe_published_lsn", None)
            if callable(observe):
                observe(durable.last_committed_lsn)
            if self._index_sync is not None and sync_if is not None and sync_if():
                with self._close_wait_hazard():
                    self._index_sync()
            yield

    def _require_recovery_complete(self) -> None:
        """Refuse work that could publish over a durable commit missing from the pages."""
        if not self._recovery_required:
            return
        # The durable floor may have moved in a commit whose apply/publication is being repaired.
        # Dropping local ranges turns every uncertain remainder into a harmless gap.
        self._identity_leases.clear()
        raise GrafxRecoveryRefused(
            "A previous durable commit or redo could not be completed on this handle. Roll "
            "back any open transaction and run database.recover() before beginning or "
            "committing more work.",
            field="recovery_required",
            required_lsn=self._published_high_water,
        )

    def _require_writable(self, operation: str) -> None:
        """Refuse persistent work before a lease, WAL, pool, or device door is reached."""
        if self._writable:
            return
        raise GrafxUnsupportedOperation(
            f"Cannot {operation} through a read-only transaction manager.",
            operation=operation,
            read_only=True,
        )

    def _require_not_closed(self, operation: str) -> None:
        """Refuse work after terminal close before a collaborator can be reached."""
        self._require_process_owner(operation)
        if not self._closed:
            return
        raise GrafxTransactionStateError(
            f"Cannot {operation} after this transaction manager has been closed.",
            operation=operation,
            closed=True,
        )

    # --- life of a transaction ----------------------------------------------------------------

    def begin(self, mode: str) -> TransactionContext:
        """Open a transaction in ``"read"`` or ``"write"`` mode and fix the view it reads under.

        The order of the three steps is the answer to carried finding CF-2 and is not
        interchangeable:

        1. read the published LSN -- a floor, and a lower bound on the snapshot this call ends up
           with;
        2. register the reader AT that floor, so the pin is visible to every other process before
           any snapshot is handed out;
        3. read the published LSN again and take the larger of the two as the snapshot.

        A horizon pass that ran during step 2 could not see this reader, but every horizon it
        could have computed is bounded by the published LSN of that instant, and every such value
        is at most the reading taken in step 3. So the horizon can never have passed the snapshot
        that is returned, which is exactly what CF-2 asks C5 to guarantee. Registering AFTER
        selecting -- the obvious order -- gives that guarantee away.
        """
        self._require_not_closed("begin a transaction")
        parsed = TransactionMode.parse(mode)
        if parsed is TransactionMode.WRITE:
            self._require_writable("begin a write transaction")
        with self._participant_section():
            self._require_not_closed("begin a transaction")
            txn, open_now = self._begin_in_section(parsed)
        self._ensure_begin_publishable(txn)
        # A91: the metrics sink is host code and is called with nothing of this component held.
        self._publish_gauge(parsed.value, open_now)
        # A raw MetricsSink is legal in an explicitly composed manager. Its callback may request
        # close and return normally, so revalidate after telemetry as well as after section-exit
        # deferrals. No caller may receive an ACTIVE context from a terminal manager.
        self._ensure_begin_publishable(txn)
        return txn

    def _begin_in_section(
        self, mode: TransactionMode
    ) -> tuple[TransactionContext, int]:
        """Open and register one transaction while the participant section is held.

        This helper emits no host metric. Retry uses it immediately after forgetting the old
        context, so no close or competing lifecycle door can enter between settlement of the old
        context and publication of its successor. The participant registration, once opened,
        belongs to the manager and therefore survives a failed begin.
        """
        self._require_not_closed("begin a transaction")
        if mode is TransactionMode.WRITE:
            self._require_writable("begin a write transaction")
        self._require_recovery_complete()
        transaction: TransactionContext | None = None
        counted = False
        try:
            floor = self._published_state_in_section()
            self._require_not_closed("begin a transaction")
            # CE-2: the participant registration answers for every transaction. When one is
            # already standing, its pin is at or below this floor by monotonicity (it only
            # ever advances to open-snapshot or published floors), so CF-2's order survives:
            # a visible pin bounds every horizon before the snapshot below is handed out. A
            # standing registration refreshed within the interval cannot have been pruned
            # (the observer TTL is three intervals), and one that is due is advanced or
            # republished below. The floor has to be read FIRST: when no transaction is open it
            # is the only safe forward target, and refreshing before reading it would renew an
            # obsolete pin for another full interval.
            standing = (
                self._participant_pin is not None
                and not self._participant_pin.registration.closed
            )
            self._ensure_participant_pin(floor.last_committed_lsn)
            refreshed = 0
            if standing:
                refreshed = self._refresh_due_readers(floor=floor.last_committed_lsn)
            self._require_not_closed("begin a transaction")
            # A standing pin still within its refresh interval was visible at or below floor
            # before this begin and published nothing during it. CF-2 therefore needs no second
            # control-record read in that stable regime. First publication, recreation and every
            # due refresh retain the original second read because another writer may have
            # committed while the pin was being published.
            selected = (
                floor
                if standing and refreshed == 0
                else self._published_state_in_section()
            )
            self._require_not_closed("begin a transaction")
            view = (
                selected
                if selected.last_committed_lsn >= floor.last_committed_lsn
                else floor
            )
            read_lsn = view.last_committed_lsn
            # L22: derived state needs a SHARED signal to invalidate it, and the published
            # commit number is the one this component already watches -- it moves whenever any
            # participant commits and nowhere else. Without this, a transaction opened in a
            # participant that had already read a table answers from frames cached before
            # somebody else committed: no error, no missing file, just fewer rows than exist.
            own_view = read_lsn == self._own_published_lsn
            catalog_may_have_changed = self._establish_read_view(view, own=own_view)
            if catalog_may_have_changed:
                self._synchronize_read_index_authority(read_lsn)
            if self._heap_reclaim_capable:
                self._require_snapshot_retained(read_lsn)
            self._require_not_closed("begin a transaction")
            transaction = TransactionContext(
                txn_id=self._next_txn_id,
                mode=mode,
                snapshot=Snapshot(read_lsn),
                epoch=_NO_EPOCH,
                owner=self,
                page_staging_capability=self._page_staging_capability,
                max_transaction_rows=self._max_transaction_rows,
                max_transaction_bytes=self._max_transaction_bytes,
            )
            self._require_not_closed("begin a transaction")
            self._open[transaction.txn_id] = transaction
            self._mode_counts[mode.value] += 1
            counted = True
            self._next_txn_id += 1
            return transaction, self._mode_counts[mode.value]
        except BaseException as failure:
            if transaction is not None:
                self._open.pop(transaction.txn_id, None)
            if counted and self._mode_counts[mode.value] > 0:
                self._mode_counts[mode.value] -= 1
            # CE-2: the participant registration is never withdrawn by a failed begin -- it
            # belongs to the manager, its standing pin is monotone-safe, and close() owns the
            # withdrawal. There is deliberately no per-transaction cleanup left to do here.
            del failure
            raise

    def _require_snapshot_retained(self, snapshot_lsn: Lsn) -> None:
        """Refuse a logical view older than the heap history retained on disk."""

        floor = self._heap.reclaim_floor()
        if floor > snapshot_lsn:
            raise GrafxSnapshotReclaimed(
                f"Snapshot {snapshot_lsn} predates heap reclaim floor {floor}; reopen the "
                "transaction or cursor against the current publication.",
                snapshot_lsn=snapshot_lsn,
                reclaim_floor_lsn=floor,
                file=self._heap.file,
                remedy="reopen",
            )

    def _ensure_begin_publishable(self, txn: TransactionContext) -> None:
        """Never return a live context after close won during a deferred/host callback."""
        if not self._closed and txn.active:
            return
        cleanup_failure: BaseException | None = None
        # A deferred callback at section exit can run close to completion: the context is then
        # already ABORTED/forgotten and the coordinator/storage may already be released. That is
        # a terminal answer requiring no new collaborator call. Reacquire only for the one state
        # proving dependencies are still owned: this exact context remains ACTIVE in `_open`.
        if self._open.get(txn.txn_id) is txn and txn.active:
            try:
                self.close()
            except BaseException as failure:
                cleanup_failure = failure
        refusal = GrafxTransactionStateError(
            "Close won while a transaction was being opened; no live context was returned.",
            operation="begin a transaction",
            txn_id=txn.txn_id,
            closed=True,
        )
        if cleanup_failure is not None:
            _note_cleanup_failure(refusal, cleanup_failure)
        raise refusal

    def rollback(self, txn: TransactionContext) -> None:
        """Abandon a transaction, leaving no trace of it anywhere.

        There is nothing to undo and nothing to log. A transaction's pages live in its own
        staging map until the commit appends them, so a rollback drops that map and withdraws the
        reader registration. No ABORT record is written: BR-2 says a path that can fail
        synchronously does so instead of writing, and a record describing work that never reached
        the device would be a discard with a trace of its own to explain.

        Rolling back a transaction that is already rolled back is a no-op, because closing a
        database must be able to abandon whatever is open without first asking what state it is
        in (CONTRACT.md section 10).
        """
        self._require_owned(txn)
        if self._closed and txn.state is TransactionState.ABORTED:
            return
        self._require_not_closed("roll back an active transaction")
        with self._participant_section():
            # Ownership and state are re-read while settlement is serialised.  A preliminary
            # check cannot carry across the wait for this section: commit, retry or another
            # rollback may have settled this exact context in that window.
            self._require_owned(txn)
            if txn.state is TransactionState.ABORTED:
                return
            mode, open_now, cleanup_failure = self._rollback_active_in_section(txn)
        if cleanup_failure is not None:
            raise cleanup_failure
        self._publish_gauge(mode, open_now)

    def commit(self, txn: TransactionContext) -> CommitReport:
        """Run the frozen commit protocol of CONTRACT.md section 8.5 and report the outcome."""
        self._require_not_closed("commit a transaction")
        self._require_owned(txn)
        self._require_active(txn)
        if txn.mode is TransactionMode.WRITE:
            self._require_writable("commit a write transaction")
        if txn.mode is TransactionMode.READ or not txn.wrote:
            return self._commit_without_writing(txn)
        return self._commit_with_writing(txn)

    def retry(self, txn: TransactionContext) -> TransactionContext:
        """Abandon a transaction that optimistic validation refused and open its successor.

        A refused transaction is left ACTIVE and free of side effects, which is what
        ``retryable`` promises -- but committing it again would compare the same snapshot against
        the same record and be refused for the same reason, forever. The retry that BR-6 and
        TS-2 describe is a fresh transaction at a fresh snapshot, above the commit that won, and
        this is the one door that produces one.

        The successor carries the refusal count, so the commit that finally succeeds is the one
        that reports ``oktografx_commit_retries_total``. Without that the counter would have
        nobody who could honestly emit it: a manager cannot tell a caller's second attempt from
        its first unless the two are linked.
        """
        self._require_not_closed("retry a transaction")
        self._require_owned(txn)
        if txn.mode is TransactionMode.WRITE:
            self._require_writable("retry a write transaction")
        settlement_failure: BaseException | None = None
        with self._participant_section():
            self._require_not_closed("retry a transaction")
            self._require_current_active(txn)
            if txn.mode is TransactionMode.WRITE:
                self._require_writable("retry a write transaction")
            if txn.conflicts <= 0:
                raise GrafxTransactionStateError(
                    "Only a transaction that optimistic validation refused can be retried "
                    "through this door; nothing has refused this one.",
                    txn_id=txn.txn_id,
                    conflicts=txn.conflicts,
                )
            carried = txn.conflicts
            mode = txn.mode
            finished_mode, open_now, cleanup_failure = self._rollback_active_in_section(
                txn
            )
            if cleanup_failure is not None:
                settlement_failure = cleanup_failure
            else:
                try:
                    successor, _successor_count = self._begin_in_section(mode)
                    successor.adopt_conflicts(carried)
                except BaseException as failure:
                    # The old context is already ABORTED. _begin_in_section has withdrawn any
                    # partial registration, so the decremented count is the final state this
                    # failed retry must publish after releasing the section.
                    settlement_failure = failure
        if settlement_failure is not None:
            try:
                self._publish_gauge(finished_mode, open_now)
            except BaseException as metric_failure:
                _note_cleanup_failure(settlement_failure, metric_failure)
            raise settlement_failure
        # Success replaced one context with another in the same mode, so the externally visible
        # gauge did not change. Emitting zero and then one would expose a state that never existed
        # outside the participant section and would put host callbacks between the two pins.
        self._ensure_begin_publishable(successor)
        return successor

    def _rollback_active_in_section(
        self, txn: TransactionContext
    ) -> tuple[str, int, BaseException | None]:
        """Settle one current transaction as aborted while the participant section is held.

        The exact context and ACTIVE state are checked again here, immediately before reader
        refresh or index cleanup can reach a collaborator.  Both public rollback and retry use
        this one transition so two lifecycle doors cannot each believe they won.
        """
        self._require_current_active(txn)
        self._refresh_finished_door()
        mode = txn.mode.value
        cleanup_failure: BaseException | None = None
        try:
            with self._close_wait_hazard():
                self._drop_index_changes(txn)
        except BaseException as failure:
            cleanup_failure = failure
            self._recovery_required = True
        txn.mark_aborted()
        cleanup_failure = _first_failure(
            cleanup_failure,
            self._release_reader(txn),
        )
        open_now, descriptor_failure = self._forget(txn, mode)
        cleanup_failure = _first_failure(cleanup_failure, descriptor_failure)
        return mode, open_now, cleanup_failure

    def _abort_for_close_in_section(
        self, txn: TransactionContext
    ) -> BaseException | None:
        """Abort and forget one context without letting any cleanup failure stop the next.

        Close is terminal, so it has a stronger obligation than an ordinary rollback: reader
        refresh, index cleanup and pin withdrawal each get their attempt, but none may leave the
        context ACTIVE or tracked.  The first failure is retained and later failures become
        notes; the caller raises it only after every context, pin, lease and metric is handled.
        """
        failure: BaseException | None = None
        try:
            with self._close_wait_hazard():
                self._drop_index_changes(txn)
        except BaseException as index_failure:
            self._recovery_required = True
            failure = _accumulate_failure(failure, index_failure)
        if txn.state is TransactionState.ACTIVE:
            try:
                txn.mark_aborted()
            except BaseException as mark_failure:
                # A host can monkeypatch even an internal context method.  Terminal close still
                # cannot leave that wrapper ACTIVE, so reproduce the method's data-only final
                # state after retaining the hostile failure.
                failure = _accumulate_failure(failure, mark_failure)
                txn._state = TransactionState.ABORTED
                txn.read_partitions.clear()
                txn.write_partitions.clear()
                txn.pending_records.clear()
                txn.page_images.clear()
                txn._page_image_proofs.clear()
                txn.row_intents.clear()
                txn.row_refs.clear()
                txn._staged_payload_bytes = 0
                txn._next_pending_token = -1
                txn._pending_row_refs.clear()
                txn._staging_marks.clear()
        reader_failure = self._release_reader(txn)
        failure = _accumulate_failure(failure, reader_failure)
        failure = _accumulate_failure(
            failure,
            self._drain_transaction_descriptor_scope(txn.txn_id),
        )
        self._index_catalog_activation_plans.pop(txn.txn_id, None)
        self._open.pop(txn.txn_id, None)
        mode = txn.mode.value
        if self._mode_counts[mode] > 0:
            self._mode_counts[mode] -= 1
        return failure

    # --- reader scheduling (amendment A46) -----------------------------------------------------

    def refresh_due_readers(self, now_monotonic: float | None = None) -> int:
        """Refresh every reader registration whose interval has elapsed; return how many moved.

        Amendment A46 puts this here rather than in the coordination layer: ``ReaderRegistration``
        has no ``renew_if_due`` counterpart to ``LeaseGuard``, a reader that misses the stall
        threshold is pruned, and nothing in C3 reminds anybody. A long read transaction that never
        refreshed would lose the pin that keeps its own log segments alive.

        The reading comes from the injected clock, or from the caller when it has one -- the same
        shape lease renewal uses, and for the same reason: a monotonic reading is local to a
        process and this layer owns no clock of its own.
        """
        self._require_not_closed("refresh readers")
        with self._participant_section():
            self._require_not_closed("refresh readers")
            pin = self._participant_pin
            if pin is None or pin.registration.closed:
                return 0
            now = self._monotonic() if now_monotonic is None else float(now_monotonic)
            if now < pin.last_refresh + self._refresh_interval:
                return 0
            floor = None
            if not self._open:
                floor = self._published_state_in_section().last_committed_lsn
            return self._refresh_due_readers(now, floor=floor)

    def _refresh_due_readers(
        self, now_monotonic: float | None = None, *, floor: Lsn | None = None
    ) -> int:
        """Refresh the participant registration when due and return whether it moved.

        ``floor`` is a published LSN the caller already read while holding the participant
        section. It lets a participant with no open transactions move its deferred pin forward
        without an extra state read in every lifecycle door.
        """
        pin = self._participant_pin
        if pin is None or pin.registration.closed:
            return 0
        now = self._monotonic() if now_monotonic is None else float(now_monotonic)
        if now < pin.last_refresh + self._refresh_interval:
            return 0
        self._advance_participant_pin(now, floor=floor)
        return 1

    def _refresh_finished_door(self) -> int:
        """Give a finishing door the due-gated republish CE-2's acceptance names.

        The NEXT_STEPS CE-2 row is explicit that an operation longer than the refresh interval
        has its OVERDUE pin republished at commit -- moved, never removed. The transaction being
        finished remains part of the open floor until its outcome is settled. Excluding it here
        could advance the pin beyond a transaction that remains active if a later commit step
        fails. A directly composed manager with no known stall threshold retains the old
        finishing-door behavior only for its sole transaction: that transaction was excluded
        from the per-transaction refresh set, so commit/rollback did not add a publication. If
        another transaction survives the door, however, its shared pin must be republished now;
        explicit ticks and later begins also refresh immediately in that conservative mode.
        """
        if self._refresh_interval <= 0.0 and len(self._open) <= 1:
            return 0
        return self._refresh_due_readers()

    def _ensure_participant_pin(self, floor: Lsn) -> ReaderRegistration:
        """Return the participant's standing registration, opening it at ``floor`` when absent.

        Opening happens at most once per manager lifetime (plus once after each close-drain,
        which clears it). The pin is published BEFORE any snapshot is selected above it, which
        is the CF-2 ordering; every later begin finds the standing pin already at or below its
        own floor.
        """
        pin = self._participant_pin
        if pin is not None and not pin.registration.closed:
            return pin.registration
        # Read the fallible host clock before publishing the durable registration. If it raises,
        # no record exists to become an ownerless pin that this coordinator will never prune.
        # A timestamp taken slightly before publication is conservative: it can only make the
        # first refresh happen earlier, never after the observer's stall deadline.
        opened_at = self._monotonic()
        with self._close_wait_hazard():
            registration = ReaderRegistration.open(self._coordinator, floor)
        self._participant_pin = _ReaderPin(registration, opened_at)
        return registration

    def _open_snapshot_floor(self) -> Lsn | None:
        """Return the oldest snapshot still open in this participant."""
        floors = [txn.snapshot.read_lsn for txn in self._open.values()]
        return min(floors) if floors else None

    def _advance_participant_pin(
        self,
        now: float | None = None,
        *,
        floor: Lsn | None = None,
    ) -> None:
        """Republish the participant pin at the highest position BR-10 allows right now.

        The safe target is the oldest open snapshot. With none open, the pin advances only
        when the caller HANDS a floor it already holds -- the checkpoint passes the number it
        is about to publish, and begin passes the floor it just read -- so this door never
        reads the published state itself and never adds a read to the frozen protocols. The
        write is one control-file publication, the same one that proves liveness, so a
        registration a foreign observer pruned is recreated by the very call that moves it.
        """
        pin = self._participant_pin
        if pin is None or pin.registration.closed:
            return
        target = self._open_snapshot_floor()
        if target is None:
            target = floor
        with self._close_wait_hazard():
            if target is not None and target > pin.registration.snapshot_lsn:
                pin.registration.advance(target)
            else:
                pin.registration.refresh()
        pin.last_refresh = self._monotonic() if now is None else now

    def checkpoint(self) -> RecycleReport:
        """Put the committed state on the platter, publish the checkpoint, and reclaim the log behind it.

        This is the lifecycle door BR-10 was waiting for (CF-11): ``WalManager.recycle`` and
        ``recyclable_horizon`` existed and nothing called either, so the log grew without bound.
        The checkpoint is also what lets recovery start somewhere other than the first record.

        Three steps, in this order, with only the state-sensitive parts under the commit section:

        1. **Redo the log onto the device from the old checkpoint.** This is not optional and it
           is not a performance choice. The commit protocol applies a commit's page images to the
           committing process's OWN pool and deliberately does not put them on the platter
           (section 8.5 step 6: the log is the authority). Another participant's committed pages
           may therefore exist only in the log and in that participant's memory. A checkpoint
           that flushed only this pool and then recycled the segments would destroy the only
           durable copy of those pages; if that participant then crashed, its acknowledged commits
           would be gone. Replaying the images here through the same idempotent door recovery
           uses (``apply_page_image``) makes the device complete for EVERY commit at or below the
           number about to be published, whatever any other process holds in memory.
        2. **Flush every dirty page, release the writer/commit fences, and barrier the data.**
           The immutable target from step 1 is the only prefix that barrier may certify. Other
           processes may commit while the device is busy, but their newer WAL remains retained.
        3. **Reacquire current writer/commit authority** and publish the greater of this target
           and an already-newer checkpoint, beside the current commit numbers. The checkpoint
           never advances to a commit that arrived during the unfenced barrier.

        Only then is the log asked to recycle, up to the horizon the reader registry and the new
        checkpoint allow together. A reader still pinned below the checkpoint holds the horizon
        back on its own account; nothing here evicts it.
        """
        manager = self._index_manager
        transition = None if manager is None else manager.open
        return self._checkpoint(
            transition,
            concurrent_data_barrier=True,
            seed_local_applied_prefix=True,
        )[0]

    def refresh_index_inventory(
        self, *, persist_stale: bool = True
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """Open every index against one cross-process-stable published picture.

        Recovery and startup both finish their own fenced work before the facade can publish
        its index inventory.  A foreign commit in that gap is harmless only when the published
        position and every table/header certificate are photographed under the same commit
        section.  The participant section alone cannot provide that property: it coordinates
        threads of this handle, not another process.

        This is deliberately a narrow index door rather than a general callback under
        ``COMMIT_SECTION``.  It changes no normal reader/writer premise and does not repair an
        index; a real stale verdict remains fail-closed exactly as :meth:`IndexManager.open`
        defines it.
        """
        self._require_not_closed("refresh index inventory")
        manager = self._index_manager
        if manager is None:
            return (), ()
        with self._participant_section():
            self._require_not_closed("refresh index inventory")
            self._require_recovery_complete()
            with self._coordinator_section(
                COMMIT_SECTION, timeout=self._commit_lock_timeout
            ):
                published = self._published_state_in_section().last_committed_lsn
                committed_catalog = getattr(self._catalog, "catalog", None)
                active_indexes = getattr(manager, "active_indexes", None)
                registered = tuple(
                    active_indexes(catalog=committed_catalog)
                    if callable(active_indexes)
                    else manager.indexes()
                )
                try:
                    stale = tuple(
                        manager.open(
                            published,
                            persist_stale=persist_stale,
                            catalog=committed_catalog,
                        )
                    )
                except TypeError:
                    stale = tuple(manager.open(published, persist_stale=persist_stale))
        return registered, stale

    def checkpoint_and_claim_index_rebuild(self, index: Any, reason: str) -> int:
        """Retire the redo and claim a rebuild generation without leaving the section.

        A claim MOVES the index's durable generation, and a RESET some other rebuild
        already committed is replayable only while its own generation is current.
        Claiming outside this section is therefore enough to wedge a database on its own:
        measured, a claim that then failed its heap scan moved the header from 6 to 16 and
        left a retained RESET at token 8 that no reopen could replay.
        """
        if not isinstance(reason, str) or not reason:
            raise GrafxConfigurationError(
                "Claiming an index rebuild needs a reason a reader can act on.",
                field="reason",
                value=repr(reason),
            )
        return self._checkpoint(lambda _published: index._claim_rebuild(reason))[1]

    def checkpoint_and_clear_index_rebuild(
        self, manager: Any, name: str, position: int, rebuild_token: int = 0
    ) -> None:
        """Finish a deferred rebuild: retire the redo, then clear under the same section.

        The clear is this operation's commit point. It cannot run under a page fence
        alone: a concurrent checkpoint can sit between a replayed RESET and its inserts,
        and commits on the rebuilt table can land between the rebuild and the clear.

        Two things are then true at once and both are kept. Physically the checkpoint
        redid the log onto the index, so the header may record what it proved. Logically
        the entries were derived at the scanned position, so this process must not answer
        a newer snapshot from them -- and it does not, because the completion marker
        fences reads there for as long as this handle holds it.
        """
        self._checkpoint(
            lambda published: manager.clear_stale(
                name, position, advance_to=published, rebuild_token=rebuild_token
            ),
            transition_is_commit_point=True,
        )

    def _checkpoint(
        self,
        transition: Callable[[int], Any] | None,
        *,
        transition_is_commit_point: bool = False,
        concurrent_data_barrier: bool = False,
        seed_local_applied_prefix: bool = False,
    ) -> tuple[RecycleReport, Any]:
        """Run the checkpoint, keeping a completed commit point above its own cleanup.

        The handler lives out here, OUTSIDE the participant section, because leaving that
        section is itself work that can fail. A normal failure there after the commit point
        completed is cleanup like any other: raising it would report a refusal for an index
        that is durably healthy. Process control still passes.
        """
        state: dict[str, Any] = {"completed": False, "recycled": None, "outcome": None}
        try:
            return self._checkpoint_in_section(
                transition,
                state,
                transition_is_commit_point,
                concurrent_data_barrier,
                seed_local_applied_prefix,
            )
        except BaseException as failure:
            # A failed checkpoint never carries advisory local redo authority into a retry.
            # WAL and the published checkpoint remain the only recovery proof.
            self._local_applied_prefix = None
            if (
                transition_is_commit_point
                and state["completed"]
                and state["recycled"] is not None
                and isinstance(failure, Exception)
            ):
                self._recovery_required = True
                return state["recycled"], state["outcome"]
            raise

    def _checkpoint_in_section(
        self,
        transition: Callable[[int], Any] | None,
        state: dict[str, Any],
        transition_is_commit_point: bool,
        concurrent_data_barrier: bool = False,
        seed_local_applied_prefix: bool = False,
    ) -> tuple[RecycleReport, Any]:
        """Run the checkpoint, running one index transition before the section is left.

        Anything that moves an index's durable generation has to happen after the redo it
        would strand has been retired, and before anything else can claim, so it belongs
        inside this section rather than in a second call by the caller.
        """
        self._require_not_closed("checkpoint")
        self._require_writable("checkpoint")
        with self._participant_section():
            self._require_not_closed("checkpoint")
            self._require_recovery_complete()
            # A split checkpoint cannot keep this participant's reader registration refreshed
            # while one device barrier is blocked. If a transaction is open long enough for a
            # foreign observer to prune that pin, the unfenced phase would let another
            # checkpoint recycle WAL beneath its live snapshot. Keep the original monolithic
            # fence whenever this handle owns an open transaction; ordinary maintenance, which
            # has no live snapshot to protect, takes the concurrent path.
            if concurrent_data_barrier and not self._open:
                return self._checkpoint_with_concurrent_barrier_in_section(
                    transition, state, seed_local_applied_prefix
                )
            lease = self._hold_lease()
            try:
                self._validate_lease(lease)
                with (
                    self._coordinator_section(
                        COMMIT_SECTION, timeout=self._commit_lock_timeout
                    ),
                    self._hold_wal_tail(),
                ):
                    self._validate_lease(lease)
                    published = self._complete_committed_gap()
                    with self._close_wait_hazard():
                        self._pool.begin_read_view(published.last_committed_lsn)
                    self._redo_onto_device(
                        published.checkpoint_lsn, published.last_committed_lsn
                    )
                    with self._close_wait_hazard():
                        self._pool.checkpoint()
                    self._publish(
                        CommitState(
                            last_committed_lsn=published.last_committed_lsn,
                            last_csn=published.last_csn,
                            checkpoint_lsn=published.last_committed_lsn,
                            format_version=published.format_version,
                        ),
                        previous=published,
                    )
                    # Recycling belongs to the same stable-WAL picture as redo and checkpoint
                    # publication.  Releasing COMMIT_SECTION before this call let startup
                    # recovery scan while segments were disappearing underneath it.
                    # CE-2: the participant's own pin is deferred and its coordinator never
                    # prunes it, so it is advanced HERE, before the horizon is computed --
                    # otherwise this manager would hold its own recycling hostage forever.
                    # The floor handed over is the number this checkpoint just published.
                    self._advance_participant_pin(floor=published.last_committed_lsn)
                    reader_horizon = self._reader_horizon()
                    with self._close_wait_hazard():
                        state["recycled"] = self._wal.recycle(
                            recyclable_horizon(
                                reader_horizon, published.last_committed_lsn
                            ),
                            reader_present=reader_horizon is not None,
                        )
                    if transition is not None:
                        # Inside the section, after redo, publication and recycle: the
                        # RESET this checkpoint retired cannot be replayed any more, and
                        # nothing else can move a generation between the two halves.
                        state["outcome"] = transition(published.last_committed_lsn)
                        state["completed"] = True
            except BaseException as failure:
                cleanup_failure = self._drop_lease(lease)
                if cleanup_failure is not None:
                    self._recovery_required = True
                    _note_cleanup_failure(failure, cleanup_failure)
                if (
                    transition_is_commit_point
                    and state["completed"]
                    and isinstance(failure, Exception)
                ):
                    # The commit point already happened on the device. Whatever failed
                    # after it is cleanup, and reporting it as the operation's failure
                    # would tell a caller the rebuild did not happen while leaving an
                    # index that answers from it. The doubt is latched instead.
                    # Process-control signals still pass: a store does not swallow those
                    # because a page landed.
                    self._recovery_required = True
                    return state["recycled"], state["outcome"]
                raise
            cleanup_failure = self._drop_lease(lease)
            if cleanup_failure is not None:
                if not (transition_is_commit_point and state["completed"]):
                    raise cleanup_failure
                # Same rule, the ordinary exit: the lease is in doubt but the commit
                # point stands, so latch the doubt and return the success that happened.
                self._recovery_required = True
            if seed_local_applied_prefix and not self._recovery_required:
                # A completed checkpoint is the only seed. Later ordinary local DML may extend
                # this empty prefix; open/recovery or an arbitrary existing WAL interval may not.
                self._local_applied_prefix = _LocalAppliedPrefix(
                    checkpoint_lsn=published.last_committed_lsn,
                    applied_through_lsn=published.last_committed_lsn,
                )
        return state["recycled"], state["outcome"]

    def _checkpoint_with_concurrent_barrier_in_section(
        self,
        transition: Callable[[int], Any] | None,
        state: dict[str, Any],
        seed_local_applied_prefix: bool = False,
    ) -> tuple[RecycleReport, Any]:
        """Barrier one frozen prefix without blocking foreign writers on the device wait.

        The caller holds only this participant's local section across A/B/C. Phase A freezes and
        flushes a target while holding the writer lease, COMMIT_SECTION and the WAL tail. Phase B
        holds none of those cross-process fences. Phase C reacquires fresh authority, catches up
        the local view, and publishes only the prefix phase B proved durable.

        Index rebuild claim/clear never enter this method: their durable generation transition
        depends on the original monolithic section and remains on that path deliberately.
        """
        # Phase A: make one exact committed prefix complete on the device and write every dirty
        # frame, but do not claim durability or reclaim its WAL yet.
        lease = self._hold_lease()
        try:
            self._validate_lease(lease)
            with (
                self._coordinator_section(
                    COMMIT_SECTION, timeout=self._commit_lock_timeout
                ),
                self._hold_wal_tail(),
            ):
                self._validate_lease(lease)
                published = self._complete_committed_gap()
                with self._close_wait_hazard():
                    self._pool.begin_read_view(published.last_committed_lsn)
                self._redo_onto_device(
                    published.checkpoint_lsn, published.last_committed_lsn
                )
                with self._close_wait_hazard():
                    self._pool.flush()
                barrier_files = {
                    self._file_ids.heap_file,
                    self._file_ids.catalog_file,
                }
                # Preserve the old global checkpoint's coverage for every paged extension this
                # pool actually wrote, even when it is not part of today's built-in registry.
                # Bootstrap staging names can remain in the modification ledger after atomic
                # rename, so only names that still exist are durable targets. Foreign images
                # skipped as already-newer are covered by the canonical inventory below.
                with self._close_wait_hazard():
                    barrier_files.update(
                        file
                        for file, _page_index in self._pool.modified_pages()
                        if self._pool.storage.exists(file)
                    )
                manager = self._index_manager
                if manager is not None:
                    barrier_files.update(index.file for index in manager.indexes())
                target = _CheckpointTarget(
                    target_lsn=published.last_committed_lsn,
                    target_csn=published.last_csn,
                    base_checkpoint_lsn=published.checkpoint_lsn,
                    barrier_files=tuple(sorted(barrier_files)),
                )
        except BaseException as failure:
            cleanup_failure = self._drop_lease(lease, force=True)
            if cleanup_failure is not None:
                self._recovery_required = True
                _note_cleanup_failure(failure, cleanup_failure)
            raise
        cleanup_failure = self._drop_lease(lease, force=True)
        if cleanup_failure is not None:
            # The target is not published, but an uncertain writer authority must not be
            # followed by an intentionally unfenced device wait on this handle.
            self._recovery_required = True
            raise cleanup_failure

        # Phase B: the slow data barrier is the entire unfenced region. WAL remains the durable
        # authority until phase C publishes the frozen target, so failure or process death here
        # leaves the previous checkpoint and every required log record intact.
        with self._close_wait_hazard():
            # A foreign process may already have written an equal-or-newer page image through
            # its own storage instance. Redo then correctly skips the image, but this instance's
            # ordinary dirty set cannot know that the foreign write still needs a data barrier.
            # Name every paged file whose state phase A accepted. A trailing global barrier would
            # fsync all of these descriptors a second time on LocalStorageDevice because its
            # conservative global target is ``dirty | open handles``; the named inventory is the
            # complete supported paged-file set (heap, catalog and every registered index).
            for file in target.barrier_files:
                self._pool.durability_barrier(file)

        # Phase C: a foreign writer or checkpoint may have advanced the durable publication
        # during B. Reacquire a fresh lease and stable WAL picture, never regress that state, and
        # never promote a commit beyond the prefix phase B actually barriered.
        lease = self._hold_lease()
        try:
            self._validate_lease(lease)
            with (
                self._coordinator_section(
                    COMMIT_SECTION, timeout=self._commit_lock_timeout
                ),
                self._hold_wal_tail(),
            ):
                self._validate_lease(lease)
                current = self._complete_committed_gap()
                if (
                    current.last_committed_lsn < target.target_lsn
                    or current.last_csn < target.target_csn
                    or current.checkpoint_lsn < target.base_checkpoint_lsn
                ):
                    self._recovery_required = True
                    raise GrafxRecoveryRefused(
                        "The durable publication moved behind the checkpoint target while its "
                        "data barrier was in progress; no checkpoint was published or recycled.",
                        field="checkpoint_target",
                        target_lsn=target.target_lsn,
                        target_csn=target.target_csn,
                        base_checkpoint_lsn=target.base_checkpoint_lsn,
                        current_last_committed_lsn=current.last_committed_lsn,
                        current_last_csn=current.last_csn,
                        current_checkpoint_lsn=current.checkpoint_lsn,
                    )

                # A commit published during B may exist only in WAL and another participant's
                # pool. Drop the phase-A view, then replay exactly the suffix still above the
                # newest published checkpoint. A concurrent checkpoint may already have recycled
                # the older prefix, which is why replay starts at the larger physical proof.
                self._establish_read_view(current, own=False)
                replay_from = _larger(target.target_lsn, current.checkpoint_lsn)
                # Even an empty suffix performs the registry synchronisation owned by the redo
                # door. A newer checkpoint may have installed foreign DDL and recycled its WAL
                # while B was running; skipping the empty range would leave this long-lived
                # handle without the newly durable indexes.
                self._redo_onto_device(replay_from, current.last_committed_lsn)

                checkpoint_lsn = _larger(current.checkpoint_lsn, target.target_lsn)
                self._publish(
                    CommitState(
                        last_committed_lsn=current.last_committed_lsn,
                        last_csn=current.last_csn,
                        checkpoint_lsn=checkpoint_lsn,
                        format_version=current.format_version,
                    ),
                    previous=current,
                )
                self._advance_participant_pin(floor=checkpoint_lsn)
                reader_horizon = self._reader_horizon()
                with self._close_wait_hazard():
                    state["recycled"] = self._wal.recycle(
                        recyclable_horizon(reader_horizon, checkpoint_lsn),
                        reader_present=reader_horizon is not None,
                    )
                if transition is not None:
                    state["outcome"] = transition(current.last_committed_lsn)
                    state["completed"] = True
        except BaseException as failure:
            cleanup_failure = self._drop_lease(lease)
            if cleanup_failure is not None:
                self._recovery_required = True
                _note_cleanup_failure(failure, cleanup_failure)
            raise
        cleanup_failure = self._drop_lease(lease)
        if cleanup_failure is not None:
            self._local_applied_prefix = None
            raise cleanup_failure
        if (
            seed_local_applied_prefix
            and not self._recovery_required
            and checkpoint_lsn == current.last_committed_lsn
        ):
            # Phase C saw no commit beyond the prefix phase B barriered.  Seed only that exact
            # empty suffix; a foreign commit during B leaves last_committed above checkpoint and
            # deliberately gets no certificate.
            self._local_applied_prefix = _LocalAppliedPrefix(
                checkpoint_lsn=checkpoint_lsn,
                applied_through_lsn=checkpoint_lsn,
            )
        return state["recycled"], state["outcome"]

    def _complete_committed_gap(self) -> CommitState:
        """Complete any durable WAL COMMIT another participant has not yet published.

        ``commit.state`` is the snapshot publication, while the WAL barrier is the durability
        authority. A participant can therefore die after a complete COMMIT became durable and
        before its page redo or state publication. Another long-lived participant must close
        that gap while it owns ``COMMIT_SECTION`` and before it publishes a later commit;
        otherwise the later page image can overwrite a page that never received the earlier
        commit, and replaying in WAL order preserves the overwrite permanently.
        """
        trace = self._active_commit_trace
        try:
            durable = self._read_commit_state()
            with self._close_wait_hazard():
                records = self._wal.read_from(durable.last_committed_lsn + 1)
                foreign_commit_count = [0] if trace is not None else None
                tail = committed_replay(
                    _count_foreign_commits(
                        records,
                        durable.last_committed_lsn,
                        foreign_commit_count,
                    )
                    if foreign_commit_count is not None
                    else records
                )
            if tail.incomplete_effects:
                pending = tail.incomplete_effects
                raise GrafxRecoveryRefused(
                    "The retained WAL contains durable page or index effects without a "
                    "provable COMMIT or ABORT. This participant will not advance the snapshot "
                    "over effects that may already have reached the device.",
                    field="wal_lineage",
                    incomplete_effect_lsns=tuple(record.lsn for record in pending),
                    incomplete_transactions=tuple(
                        sorted({(record.epoch, record.txn_id) for record in pending})
                    ),
                )
            target = tail.last_committed_lsn
            if target <= durable.last_committed_lsn:
                return durable
            # A complete record is visible in the page cache before it is necessarily durable.
            # The participant that appended it may have escaped after the physical write and
            # before its own barrier.  Establish WAL durability here before redo can put any of
            # its data pages on the device or commit.state can publish its outcome.
            with self._close_wait_hazard():
                self._wal.force_barrier_range(
                    durable.last_committed_lsn + 1,
                    target,
                )
            self._redo_onto_device(durable.checkpoint_lsn, target)
            completed = CommitState(
                last_committed_lsn=target,
                last_csn=target,
                checkpoint_lsn=durable.checkpoint_lsn,
                format_version=self._commit_state_format_for_catalog(
                    durable, inspect_durable_catalog=True
                ),
            )
            self._publish(completed, previous=durable)
            self._published_high_water = _larger(self._published_high_water, target)
            if trace is not None and foreign_commit_count is not None:
                trace.increment(COMMIT_FOREIGN_COMMITS_TOTAL, foreign_commit_count[0])
            return completed
        except BaseException:
            # Redo can already have installed a prefix of the durable transaction when an
            # adapter, callback, or process-control exception escapes.  The exception class
            # does not make that prefix safe to publish over: latch first and preserve the
            # original exception (including KeyboardInterrupt/SystemExit) unchanged.
            self._recovery_required = True
            raise

    def _redo_onto_device(self, checkpoint: Lsn, through: Lsn) -> int:
        """Replay a commit range and fail closed for every index if the pass is incomplete."""
        try:
            with self._close_wait_hazard():
                return self._redo_onto_device_unchecked(checkpoint, through)
        except BaseException as failure:
            self._recovery_required = True
            manager = self._index_manager
            exclude = getattr(manager, "mark_all_stale", None)
            if callable(exclude):
                reason = (
                    failure.code
                    if isinstance(failure, GrafxError)
                    else type(failure).__name__
                )
                try:
                    with self._close_wait_hazard():
                        exclude(
                            f"Committed redo did not complete ({reason}); this handle cannot "
                            "prove any index is complete.",
                            persist=False,
                        )
                except BaseException:  # noqa: BLE001 - never replace the redo failure
                    pass
            raise

    def _can_skip_locally_applied_redo(
        self,
        checkpoint: Lsn,
        through: Lsn,
        *,
        touched_catalog: bool,
        index_records: Sequence[WalRecord],
    ) -> bool:
        """Say whether an exact checkpoint suffix was already applied by this process.

        The witness is deliberately narrower than idempotence. It begins only at a checkpoint,
        extends only across ordinary local DML, and is consumed only for that exact
        ``checkpoint -> published`` pair after WAL lineage and payloads have been preflighted.
        Any uncertainty returns ``False`` and leaves the canonical replay untouched.
        """

        prefix = self._local_applied_prefix
        if (
            prefix is None
            or self._recovery_required
            or touched_catalog
            or through <= checkpoint
            or prefix.checkpoint_lsn != checkpoint
            or prefix.applied_through_lsn != through
            or self._own_published_lsn != through
            or self._index_authority_sync_required
            or type(self._index_manager) is not IndexManager
        ):
            return False
        if any(change_of(record).operation is IndexOperation.RESET for record in index_records):
            return False
        # Live commit flushes heap and index files before publishing. A dirty resident frame is
        # evidence that some later/local work escaped that completed prefix, so it revokes the
        # shortcut rather than being folded into the checkpoint by assumption.
        return not self._pool.has_dirty_pages()

    def _redo_onto_device_unchecked(self, checkpoint: Lsn, through: Lsn) -> int:
        """Install every committed effect above the checkpoint and at or below ``through``.

        The redo rule is C1's ``apply_page_image``: an image whose number the page already carries
        is left alone, so replaying what this process applied itself changes nothing, and an image
        another process committed lands exactly as recovery would land it. Returns how many images
        were installed.
        """
        records: list[WalRecord] = []
        for record in self._wal.read_from(checkpoint + 1):
            if record.lsn > through:
                break
            records.append(record)
        replay = committed_replay(records)
        if checkpoint > through:
            raise GrafxRecoveryRefused(
                "The published checkpoint is ahead of the commit watermark; checkpoint cannot "
                "derive a safe WAL range.",
                field="checkpoint_lsn",
                checkpoint_lsn=checkpoint,
                last_committed_lsn=through,
            )
        if through > checkpoint:
            expected = checkpoint + 1
            for record in records:
                if record.lsn != expected:
                    raise GrafxRecoveryRefused(
                        f"Checkpoint expected WAL record {expected}, but observed "
                        f"{record.lsn}; no page, watermark or segment was changed.",
                        field="wal_lineage",
                        expected_lsn=expected,
                        observed_lsn=record.lsn,
                    )
                expected += 1
            if (
                not records
                or records[-1].lsn != through
                or replay.last_committed_lsn != through
            ):
                raise GrafxRecoveryRefused(
                    "The retained WAL does not prove the commit watermark checkpoint was asked "
                    "to publish; no page, watermark or segment was changed.",
                    field="wal_lineage",
                    checkpoint_lsn=checkpoint,
                    last_committed_lsn=through,
                    last_record_lsn=records[-1].lsn if records else NO_LSN,
                    recovered_lsn=replay.last_committed_lsn,
                )
        page_records = tuple(
            record
            for record in replay.effects
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )
        index_records = tuple(
            record
            for record in replay.effects
            if record.record_type
            in (int(WalRecordType.INDEX_WRITE), int(WalRecordType.INDEX_RECONCILE))
        )
        page_replay = CommittedReplay(
            effects=page_records, last_committed_lsn=replay.last_committed_lsn
        )
        index_replay = CommittedReplay(
            effects=index_records, last_committed_lsn=replay.last_committed_lsn
        )

        # A long-lived participant may checkpoint WAL written by another process. Its catalog
        # and index registry can therefore predate a committed CREATE TABLE. Install catalog
        # pages first, adopt their authoritative image, and only then register and dispatch the
        # logical index effects that name the newly-created index. The complete plan is still
        # decoded before the first page moves; only registry-dependent checks for a name the
        # catalog pages introduce are deferred to the strict index-only preflight after sync.
        touched_catalog = any(
            decode_page_write_location(record.payload).file
            == self._file_ids.catalog_file
            for record in page_records
        )
        redo_passage = object()
        full_preflight = self._commit_redo.preflight(
            replay,
            allow_unregistered_indexes=touched_catalog,
            _passage=redo_passage,
        )
        skip_reapply = self._can_skip_locally_applied_redo(
            checkpoint,
            through,
            touched_catalog=touched_catalog,
            index_records=index_records,
        )
        page_result = None
        if not skip_reapply:
            page_preflight = self._commit_redo._project_page_preflight(
                replay,
                page_replay,
                full_preflight,
                allow_unregistered_indexes=touched_catalog,
                passage=redo_passage,
            )
            if page_preflight is None:
                raise GrafxRecoveryRefused(
                    "The checkpoint page subplan no longer matches its complete preflight.",
                    field="preflighted_replay",
                )
            page_result = self._commit_redo.apply(
                page_replay,
                _preflighted=page_preflight,
                _passage=redo_passage,
            )
        if touched_catalog:
            self._catalog.adopt(self._catalog.read_from_pages())
        if self._index_sync is not None:
            self._index_sync()

        manager = self._index_manager
        watermarks = None
        if manager is not None:
            # Only after lineage is proven and foreign schema is adopted may this conservative
            # verdict write a stale flag for every now-known persistent index. Heap pages are
            # already applied and index replay never moves them, so one photo serves this pass
            # and the completion mark below (ST-7).
            watermarks = manager.table_watermark_photo()
            manager.check_replay_floor(checkpoint, watermarks=watermarks)
        index_result = None
        if not skip_reapply:
            index_result = self._commit_redo.apply(index_replay)
            assert page_result is not None
            for result in (page_result, index_result):
                self._commit_redo.flush(result)
        # A skipped replay necessarily has ``touched_catalog is False``. The complete preflight
        # above therefore already used strict index registration/generation validation; running
        # a second preflight over ``index_replay`` would only decode every logical effect again.
        if manager is not None and replay.last_committed_lsn > NO_LSN:
            manager.mark_built_through(replay.last_committed_lsn, watermarks=watermarks)
        if skip_reapply:
            return len(replay.effects)
        assert page_result is not None and index_result is not None
        return page_result.effects_replayed + index_result.effects_replayed

    def close(self) -> None:
        """Terminally close this manager after attempting every owned cleanup.

        CONTRACT.md section 10 says closing a database with an open transaction aborts it and
        never corrupts. RuntimeError, KeyboardInterrupt and SystemExit from one cleanup are
        evidence to report only after every other context, pin, retained lease and metric has
        received its attempt. The terminal latch is set before waiting for the participant
        section, so a failed or delayed close can never leave the wrapper able to commit later;
        cleanup itself starts only after every lifecycle winner already in flight is quiescent.
        """
        self.request_close()
        if self._close_complete:
            return
        # Same-context callbacks (metrics, clock, coordinator hooks) can ask the Database to
        # close while this manager is halfway through begin/commit. The coordinator deliberately
        # permits re-entry, so exclusion alone cannot identify that recursion. Defer cleanup;
        # the terminal request remains published and a later close retries after the winner has
        # returned. Safe leak is preferable to releasing its pool/device underneath it.
        if self.transition_active or self._close_finalizing:
            return
        self._close_finalizing = True
        failure: BaseException | None = None
        guard: LeaseGuard | None = None
        try:
            while not self._close_quiesced:
                entered = False
                try:
                    with self._participant_section():
                        entered = True
                        for txn in list(self._open.values()):
                            try:
                                txn_failure = self._abort_for_close_in_section(txn)
                            except BaseException as unexpected_failure:
                                # Defence in depth around the fail-complete helper. Even a
                                # monkeypatched helper cannot skip the remaining wrappers/maps.
                                txn_failure = unexpected_failure
                                if txn.state is TransactionState.ACTIVE:
                                    try:
                                        txn.mark_aborted()
                                    except BaseException as mark_failure:
                                        txn_failure = _accumulate_failure(
                                            txn_failure,
                                            mark_failure,
                                        )
                                        txn._state = TransactionState.ABORTED
                                        txn.read_partitions.clear()
                                        txn.write_partitions.clear()
                                        txn.pending_records.clear()
                                        txn.page_images.clear()
                                        txn._page_image_proofs.clear()
                                        txn.row_intents.clear()
                                        txn.row_refs.clear()
                                        txn._staged_payload_bytes = 0
                                        txn._next_pending_token = -1
                                        txn._pending_row_refs.clear()
                                        txn._staging_marks.clear()
                                self._open.pop(txn.txn_id, None)
                                self._index_catalog_activation_plans.pop(
                                    txn.txn_id, None
                                )
                                failure = _accumulate_failure(
                                    failure,
                                    self._drain_transaction_descriptor_scope(
                                        txn.txn_id
                                    ),
                                )
                            failure = _accumulate_failure(failure, txn_failure)
                        # Defence in depth for a monkeypatched abort helper: every retained
                        # descriptor scope is transaction-owned and terminal close must attempt
                        # all of them before lower resources are released.
                        for txn_id in tuple(self._transaction_descriptor_scopes):
                            failure = _accumulate_failure(
                                failure,
                                self._drain_transaction_descriptor_scope(txn_id),
                            )
                        pin = self._participant_pin
                        if pin is not None:
                            failure = _accumulate_failure(
                                failure,
                                self._close_reader_quietly(pin.registration),
                            )
                        self._participant_pin = None
                        self._identity_leases.clear()
                        self._index_catalog_activation_plans.clear()
                        self._open.clear()
                        for mode_name in self._mode_counts:
                            self._mode_counts[mode_name] = 0
                        guard = self._lease_guard
                        self._lease_guard = None
                        self._close_quiesced = True
                except GrafxLeaseTimeout as timeout_failure:
                    if not entered:
                        # A live winner still holds the participant section. Close is quiescence,
                        # so a configured attempt timeout is not permission to release resources.
                        continue
                    failure = _accumulate_failure(failure, timeout_failure)
                except BaseException as section_failure:
                    failure = _accumulate_failure(failure, section_failure)
                break
            if not self._close_quiesced:
                if failure is not None:
                    raise failure
                return
            if guard is not None and not guard.released:
                # A retained lease outliving its manager makes every other participant wait out
                # the stall threshold before it can write at all.
                failure = _accumulate_failure(failure, _release_quietly(guard))
            for mode_name in self._mode_counts:
                self._publish_gauge(mode_name, 0)
            # Outcome-neutral gauges have received their attempt and cannot fail this line. Mark
            # completion before surfacing cleanup evidence so Database may safely release the
            # lower layers even when this call reports a reader/index/lease cleanup failure.
            self._close_complete = True
            if failure is not None:
                raise failure
        finally:
            self._close_finalizing = False

    @contextmanager
    def recovery_section(self) -> Iterator[None]:
        """Hold this participant quiescent for operator recovery.

        The open-transaction check and the whole recovery pass share the participant section, so
        a racing ``begin()`` cannot slip between them. Startup recovery does not need this door:
        no TransactionManager exists yet. Cross-process exclusion remains RecoveryManager's own
        responsibility through ``COMMIT_SECTION``.
        """
        self._require_not_closed("run recovery")
        with self._participant_section():
            self._require_not_closed("run recovery")
            if self._open:
                raise GrafxTransactionStateError(
                    "Recovery requires this database handle to have no open transactions.",
                    field="open_transactions",
                    open_transactions=len(self._open),
                )
            yield

    # --- the commit protocol -------------------------------------------------------------------

    def _commit_without_writing(self, txn: TransactionContext) -> CommitReport:
        """Finish a transaction that put nothing in the log (section 8.5 step 1).

        A read transaction is one. So is a write transaction that staged nothing: it has no
        partition to validate and no bytes to append, so appending a COMMIT record for it would
        add a record whose only content is that nothing happened.
        """
        csn: Csn = txn.snapshot.read_lsn
        mode = txn.mode.value
        with self._participant_section():
            self._require_not_closed("commit a transaction")
            self._require_current_active(txn)
            if txn.mode is TransactionMode.WRITE:
                self._require_writable("commit a write transaction")
            self._refresh_finished_door()
            txn.mark_committed(csn)
            # Reader withdrawal is best-effort after the outcome is settled. Its registration
            # has a TTL; surfacing a foreign cleanup exception here would invite a caller to
            # retry an operation this transaction has already completed.
            self._release_reader(txn)
            open_now, _descriptor_failure = self._forget(txn, mode)
        self._publish_gauge(mode, open_now)
        return CommitReport(csn=csn, durable=True, wrote=False)

    def _commit_with_writing(self, txn: TransactionContext) -> CommitReport:
        """Run steps 2 to 4 for a transaction that has work to append.

        The whole acquire-commit-release window is inside the participant section, so the
        threads of one participant take their turns at it rather than ending one another's
        epoch (see :meth:`_participant_section`). Another PROCESS is not held up by it: its
        section carries a different name and it contends only where CONTRACT.md section 8.5
        already says it must, at the commit section of step 3.
        """
        retried = txn.conflicts > 0
        conflict: tuple[int, ...] | None = None
        committed: Csn = NO_CSN
        mode = txn.mode.value
        # Assigned inside the section on every path that publishes it; the name exists here only
        # so no path can read it before the section has settled it.
        open_now = 0
        post_barrier_failure: BaseException | None = None
        cleanup_failure: BaseException | None = None
        identities: _IdentityPlan | None = None
        catalog_touched = False
        local_prefix_candidate: _LocalAppliedPrefix | None = None
        local_commit_can_extend_prefix = False
        instrumentation = (
            _CommitTrace(self._metrics, self._pool)
            if not self._retain_lease and _metrics_are_enabled(self._metrics)
            else _DISABLED_COMMIT_TRACE
        )
        participant = (
            self._participant_section()
            if instrumentation is _DISABLED_COMMIT_TRACE
            else self._measured_participant_section(instrumentation)
        )
        # Contexts leave in reverse order: telemetry emits only after the participant section,
        # writer lease and COMMIT_SECTION have all been settled. The innermost probe detaches
        # before the participant section can admit another local commit.
        with (
            instrumentation as commit_trace,
            participant,
            _commit_buffer_scope(self, commit_trace),
        ):
            self._require_not_closed("commit a transaction")
            self._require_current_active(txn)
            self._require_writable("commit a write transaction")
            self._require_recovery_complete()
            self._validate_staged_inputs(txn)
            activation_plan = self._index_catalog_activation_plans.get(txn.txn_id)
            if activation_plan is not None:
                self._validate_index_catalog_activation_plan(txn, activation_plan)
            txn.validate_budgets()
            # Preserve the long-standing fail-before-lock boundary for malformed or obviously
            # below-floor explicit ids. A fresh cross-process proof is repeated later under the
            # global commit lock before any such id is accepted.
            self._validate_explicit_identity_floors(txn)
            self._refresh_finished_door()
            if commit_trace is not None:
                commit_trace.start_window("writer_lease")
            try:
                lease = self._hold_lease()
            except BaseException as failure:
                if commit_trace is not None:
                    commit_trace.fail_window("writer_lease")
                    if not isinstance(failure, GrafxLeaseTimeout):
                        commit_trace.suppress_delivery()
                raise
            if commit_trace is not None:
                commit_trace.acquire_window("writer_lease")
            try:
                # Step 2: the epoch is confirmed before any byte can reach the device (BR-7,
                # AC-6), through the coordinator that granted this very lease (A74).
                self._validate_lease(lease)
                with (
                    self._coordinator_section(
                        COMMIT_SECTION,
                        timeout=self._commit_lock_timeout,
                    ),
                    self._hold_wal_tail(),
                ):
                    self._validate_lease(lease)  # step 3.1
                    durable = self._complete_committed_gap()
                    current = durable.last_committed_lsn
                    # The commit decides against the picture as it is NOW, not as this pool last
                    # cached it. Optimistic validation reads the log, but everything else the commit
                    # consults -- the catalog, the pages a row will land on -- comes through the pool,
                    # and a stale one makes a correct predicate decide against the wrong picture
                    # (defect E1, second half; LESSONS L22).
                    own_view = current == self._own_published_lsn
                    index_authority_may_have_changed = self._establish_read_view(
                        durable, own=own_view
                    )
                    validated_through = current
                    # Validation has two tiers, and the first is not redundant. Caller-declared
                    # logical interests and pre-staged pages are known now, so validate them
                    # BEFORE touching the heap; otherwise the heap can refuse a version another
                    # participant already ended with transaction_state, which tells a caller to
                    # stop where write_conflict would tell it to retry. The physical pages a row
                    # lands on are knowable only after it is materialised, so those newly
                    # discovered interests receive their bounded second pass below. Nothing of
                    # the provisional rows is durable yet, and a refusal abandons them beyond
                    # the reach of every snapshot (defect E1, carried finding CF-A).
                    # Freeze exactly what the first OCC pass proved.  Logical interests and
                    # physical images staged before commit belong to the transaction's original
                    # snapshot for their whole lifetime.  Only page partitions discovered by
                    # materialising rows from the fresh view below may use a newer baseline.
                    snapshot_interest = frozenset(
                        txn.read_partitions | txn.write_partitions
                    )
                    pre_staged_pages = frozenset(txn.staged_pages())
                    if conflict is None:
                        if commit_trace is not None:
                            # Completing a durable commit left behind by another participant is
                            # recovery/application work, not optimistic validation. Keep it in
                            # ``other`` and begin OCC only at the first conflict predicate; an
                            # expensive foreign gap must not make the OCC phase look expensive.
                            commit_trace.phase("occ")
                        with self._close_wait_hazard():
                            conflict = self._find_conflict(
                                txn, interested_partitions=snapshot_interest
                            )  # step 3.3
                    if conflict is None:
                        # A stale logical snapshot must lose at the first OCC gate before any
                        # catalog/index synchronization can touch the pool or invoke host
                        # observability. A surviving row writer still adopts every committed
                        # persistent artifact here, before provenance validation,
                        # materialisation or a WAL byte.
                        with self._close_wait_hazard():
                            self._synchronize_committed_indexes(
                                txn,
                                current,
                                authority_may_have_changed=(
                                    index_authority_may_have_changed
                                ),
                            )
                    if conflict is None:
                        # Physical ownership is a second, pre-materialisation gate.  The first
                        # logical OCC above remains integral and decides every stale snapshot
                        # before nonce/registry provenance is consulted.
                        conflict = self._staged_artifact_conflict(txn)
                    if conflict is None:
                        if commit_trace is not None:
                            commit_trace.phase("materialize")
                        identities, pending_identity_ranges = (
                            self._prepare_identity_plan(txn)
                        )
                        if pending_identity_ranges:
                            durable, identities = self._reserve_identity_plan(
                                txn,
                                identities,
                                pending_identity_ranges,
                                previous=durable,
                                lease=lease,
                            )
                            current = durable.last_committed_lsn
                            if self._closed:
                                self._identity_leases.clear()
                                refusal = GrafxTransactionStateError(
                                    "The identity-floor reservation became durable, but close "
                                    "was requested before the user transaction could run. The "
                                    "range remains burned and the user transaction did not "
                                    "commit.",
                                    operation="continue user commit after identity reservation",
                                    metadata_committed=True,
                                    user_transaction_committed=False,
                                    reservation_csn=identities.reservation_lsn,
                                    closed=True,
                                    txn_id=txn.txn_id,
                                )
                                refusal.details["retryable"] = False
                                raise refusal
                        if (
                            tuple(reduce_row_intents(txn.row_intents))
                            != identities.intents
                        ):
                            raise GrafxTransactionStateError(
                                "The staged row intents changed while their durable identities "
                                "were being reserved; the burned identities remain gaps and "
                                "this transaction must be restaged.",
                                field="row_intents",
                                txn_id=txn.txn_id,
                            )
                        if current > validated_through:
                            # A CN-1 refill is a real durable commit between the first OCC pass
                            # and row materialisation.  Fresh heap pages may use its image as
                            # their baseline, but caller-declared logical interests and
                            # pre-staged pages may not.  Revalidate only that already-proved set
                            # over the incremental interval and do not forgive the refill: a
                            # pre-staged heap page 0 is stale relative to the new durable floor.
                            if commit_trace is not None:
                                commit_trace.phase("occ")
                            with self._close_wait_hazard():
                                conflict = self._find_conflict(
                                    txn,
                                    interested_partitions=snapshot_interest,
                                    baseline_lsn=validated_through,
                                )
                    rows: tuple[_RowWrite, ...] = ()
                    # ``current`` includes an identity-floor subcommit when this attempt needed
                    # one.  It is the durable image the heap will materialise from, not a promise
                    # about a future image.  COMMIT_SECTION prevents a foreign publisher from
                    # moving it while rows are being built.
                    materialization_lsn = current
                    staging_mark = len(txn.pending_records)
                    # The mark this attempt's page set is measured against. The window opens
                    # and the reading is taken HERE, after the read view has settled and before
                    # a single page is written, so that everything the pool changes from here on
                    # is this attempt's doing and nothing that was already dirty is mistaken for
                    # it. See _attempt_pages.
                    self._pool.forget_modified()
                    self._dirty_mark = self._pool.modified_pages()
                    try:
                        if conflict is None:
                            if identities is None:
                                raise GrafxTransactionStateError(
                                    "A row-writing commit reached the heap without an identity "
                                    "plan.",
                                    field="identity_plan",
                                    txn_id=txn.txn_id,
                                )
                            # Inside the guard, not before it: a refusal on the SECOND intent
                            # of a batch used to leave the first one written and never
                            # abandoned -- a phantom row the next commit of anyone flushed and
                            # published (C5 round-2 B1).
                            if commit_trace is not None:
                                commit_trace.phase("materialize")
                            with self._close_wait_hazard():
                                rows = self._write_rows(txn, identities)
                            materialized_pages = self._declare_page_interest(txn, rows)
                            materialized_interest = self._materialized_page_delta(
                                txn,
                                snapshot_interest=snapshot_interest,
                                pre_staged_pages=pre_staged_pages,
                                materialized_pages=materialized_pages,
                            )
                            if commit_trace is not None:
                                commit_trace.phase("occ")
                            with self._close_wait_hazard():
                                conflict = self._find_conflict(
                                    txn,
                                    interested_partitions=materialized_interest,
                                    baseline_lsn=materialization_lsn,
                                )  # step 3.3, page half
                        if conflict is None:
                            # AFTER ordinary validation cleared and immediately before
                            # the records exist: a rebuild generation claimed since this
                            # RESET was staged makes the record unreplayable, and this is
                            # the only place to catch it without turning an honest write
                            # conflict into an index error.
                            validate_generations = getattr(
                                self._index_manager,
                                "validate_staged_rebuild_generations",
                                None,
                            )
                            if callable(validate_generations):
                                validate_generations(txn)
                            if commit_trace is not None:
                                commit_trace.phase("build_records")
                            with self._close_wait_hazard():
                                records, images, materialized_csn = self._build_records(
                                    txn, lease.epoch, rows
                                )
                            txn.validate_budgets()
                            with self._close_wait_hazard():
                                planned_csn = self._wal.planned_terminal_lsn(records)
                            raw_batch_rolls = planned_csn != materialized_csn
                            if planned_csn != materialized_csn:
                                with self._close_wait_hazard():
                                    records, images = self._retarget_commit_batch(
                                        txn,
                                        records,
                                        images,
                                        rows,
                                        old_csn=materialized_csn,
                                        new_csn=planned_csn,
                                        epoch=lease.epoch,
                                    )
                                if commit_trace is not None:
                                    commit_trace.increment(COMMIT_RETARGETS_TOTAL)
                            self._materialized = None
                            if not raw_batch_rolls:
                                records = self._compress_page_records(records, images)
                            self._validate_wal_batch_budget(txn, records)
                            if commit_trace is not None and (
                                txn.txn_id in self._index_catalog_activation_plans
                            ):
                                commit_trace.phase("index")
                            with self._close_wait_hazard():
                                self._build_index_catalog_activation(txn, current)
                            catalog_touched = any(
                                file == self._file_ids.catalog_file
                                for file, _page_index, _image in images
                            )
                            local_commit_can_extend_prefix = (
                                not catalog_touched
                                and activation_plan is None
                                and identities is not None
                                and type(self._index_manager) is IndexManager
                                and not any(
                                    record.record_type
                                    in (
                                        int(WalRecordType.INDEX_WRITE),
                                        int(WalRecordType.INDEX_RECONCILE),
                                    )
                                    and change_of(record).operation
                                    is IndexOperation.RESET
                                    for record in records
                                )
                            )
                            self._validate_lease(lease)
                            if commit_trace is not None:
                                commit_trace.phase("append")
                            wal_bytes_before = (
                                _trusted_wal_bytes(self._wal)
                                if commit_trace is not None
                                else None
                            )
                            with self._close_wait_hazard():
                                committed = self._wal.append_many(
                                    records,
                                    expected_terminal_lsn=planned_csn,
                                )  # step 3.4
                            if commit_trace is not None:
                                wal_bytes_after = _trusted_wal_bytes(self._wal)
                                commit_trace.capture_batch(
                                    images,
                                    None
                                    if wal_bytes_before is None
                                    or wal_bytes_after is None
                                    else wal_bytes_after - wal_bytes_before,
                                )
                            _require_forward_commit(committed, current)
                            if commit_trace is not None:
                                commit_trace.phase("barrier")
                            with self._close_wait_hazard():
                                self._wal.barrier()  # step 3.5 -- durable here
                            # The WAL outcome is irrevocable at this instant. Settle it before
                            # page/index apply, publication, lease release, reader cleanup or any
                            # other fallible callback can run and tempt a caller to retry an ACTIVE
                            # transaction whose COMMIT is already durable.
                            txn.bind_epoch(lease.epoch)
                            txn.mark_committed(committed)
                    except BaseException as failure:
                        if commit_trace is not None:
                            commit_trace.phase("other")
                        with self._close_wait_hazard():
                            wal_is_damaged = _wal_is_damaged(self._wal)
                        if committed > NO_CSN or wal_is_damaged:
                            # A complete COMMIT record was appended, but the barrier or the
                            # forward-order proof did not finish, or append rollback could not put
                            # the segment back. Its outcome is now uncertain: no later transaction
                            # on this handle may step over it until operator recovery resolves the
                            # WAL bytes that survived.
                            self._recovery_required = True
                        with self._close_wait_hazard():
                            abandoned = self._abandon_rows(rows)
                        with self._close_wait_hazard():
                            unstaged = self._unstage_index_changes(txn, staging_mark)
                        failed_cleanup = _first_failure(abandoned, unstaged)
                        if failed_cleanup is not None:
                            self._recovery_required = True
                            _note_cleanup_failure(failure, failed_cleanup)
                        raise
                    if conflict is not None:
                        if commit_trace is not None:
                            commit_trace.phase("other")
                        with self._close_wait_hazard():
                            abandoned = self._abandon_rows(rows)
                        with self._close_wait_hazard():
                            unstaged = self._unstage_index_changes(txn, staging_mark)
                        cleanup_failure = _first_failure(abandoned, unstaged)
                        if cleanup_failure is not None:
                            self._recovery_required = True
                    else:
                        self._published_high_water = _larger(
                            self._published_high_water, committed
                        )
                        try:
                            if commit_trace is not None:
                                commit_trace.phase("apply")
                            with self._close_wait_hazard():
                                self._apply_images(images)  # step 3.6
                            if commit_trace is not None:
                                commit_trace.phase("index")
                            with self._close_wait_hazard():
                                self._apply_index_changes(txn, committed)  # step 3.6
                            if catalog_touched:
                                # Catalog pages are now the durable runtime authority.  Adopt and
                                # attach only existing files before publishing the v2 commit-state
                                # fence; a referenced shadow that cannot be reopened is a
                                # post-barrier failure and follows the ordinary redo path.
                                with self._close_wait_hazard():
                                    self._catalog.adopt(self._catalog.read_from_pages())
                                    observe = getattr(
                                        self._index_manager,
                                        "observe_published_lsn",
                                        None,
                                    )
                                    if callable(observe):
                                        observe(committed)
                                    if self._index_sync is not None:
                                        self._index_sync()
                            if commit_trace is not None:
                                commit_trace.phase("publish")
                            if local_commit_can_extend_prefix:
                                # _publish revokes every advisory proof before touching the
                                # control record. Retain the candidate only in this stack frame;
                                # it is restored below solely after publication succeeds.
                                local_prefix_candidate = self._local_applied_prefix
                            with self._close_wait_hazard():
                                self._publish_commit_state(
                                    durable,
                                    committed,
                                    catalog_touched=catalog_touched,
                                )  # step 3.7
                            if (
                                local_prefix_candidate is not None
                                and local_prefix_candidate.checkpoint_lsn
                                == durable.checkpoint_lsn
                                and local_prefix_candidate.applied_through_lsn
                                == durable.last_committed_lsn
                            ):
                                self._local_applied_prefix = _LocalAppliedPrefix(
                                    checkpoint_lsn=(
                                        local_prefix_candidate.checkpoint_lsn
                                    ),
                                    applied_through_lsn=committed,
                                )
                        except BaseException as failure:
                            if commit_trace is not None:
                                commit_trace.phase("other")
                            post_barrier_failure = failure
                            if isinstance(failure, GrafxError):
                                try:
                                    with self._close_wait_hazard():
                                        recovered = self._recover_post_barrier(
                                            txn,
                                            durable,
                                            committed,
                                            rows,
                                            catalog_touched=catalog_touched,
                                        )
                                except BaseException as recovery_failure:
                                    # Recovery is cleanup for the already-recorded failure.  It may
                                    # add evidence, but must never replace the failure that caused
                                    # the cleanup to run.
                                    _note_cleanup_failure(failure, recovery_failure)
                                    recovered = False
                            else:
                                # KeyboardInterrupt/SystemExit or foreign adapter failures after
                                # the WAL barrier cannot make the durable commit disappear. Settle
                                # the transaction as committed, poison the handle, then re-raise.
                                recovered = False
                            if not recovered:
                                self._recovery_required = True
            except BaseException as failure:
                self._local_applied_prefix = None
                lease_failure = self._drop_lease(lease)
                if lease_failure is not None:
                    self._recovery_required = True
                    _note_cleanup_failure(failure, lease_failure)
                raise
            if conflict is None:
                cleanup_failure = _first_failure(
                    cleanup_failure,
                    self._release_reader(txn),
                )
                open_now, descriptor_failure = self._forget(txn, mode)
                cleanup_failure = _first_failure(cleanup_failure, descriptor_failure)
            else:
                txn.mark_conflicted()
            lease_failure = self._drop_lease(lease)
            cleanup_failure = _first_failure(cleanup_failure, lease_failure)
            if cleanup_failure is not None or post_barrier_failure is not None:
                self._local_applied_prefix = None
            if lease_failure is not None and conflict is not None:
                # This is still a pre-barrier refusal.  An interrupted foreign lease cleanup may
                # have left the writer authority uncertain, so the handle cannot immediately
                # retry and write under an assumption about which epoch survived.
                self._recovery_required = True
        # Everything below runs with no section and no lease held. A metrics sink is supplied by
        # the host and may do anything at all, including re-entering this API, so it is called
        # only once every invariant this component owns has been settled (amendment A91).
        if post_barrier_failure is not None:
            if cleanup_failure is not None:
                _note_cleanup_failure(post_barrier_failure, cleanup_failure)
            if isinstance(post_barrier_failure, GrafxError):
                raise _already_committed(
                    post_barrier_failure,
                    committed,
                    recovery_required=self._recovery_required,
                )
            if isinstance(post_barrier_failure, Exception):
                # Adapter code is required to translate platform exceptions, but a hostile or
                # incomplete custom port may still let one through.  After the barrier it must
                # never look like an ordinary RuntimeError that application retry code can
                # repeat.  Preserve it as the cause of one typed, machine-readable terminal
                # outcome instead.
                raise _foreign_already_committed(
                    post_barrier_failure,
                    committed,
                    recovery_required=self._recovery_required,
                ) from post_barrier_failure
            # Process-control signals retain their exact identity and semantics.  Their note is
            # deliberately explicit because a caller that catches one during shutdown still
            # needs to know that repeating this transaction would duplicate durable work.
            _note_durable_commit(
                post_barrier_failure,
                committed,
                recovery_required=self._recovery_required,
            )
            raise post_barrier_failure
        # Lease/reader cleanup happens only after a durable outcome and is best-effort. A raw
        # RuntimeError or KeyboardInterrupt here would make a confirmed COMMIT look retryable;
        # the coordinator TTL is the recovery mechanism for a foreign cleanup that did not take.
        if conflict is not None:
            self._increment_metric(WRITE_CONFLICTS_TOTAL)
            raise GrafxWriteConflict(
                "This transaction read or wrote a partition that another commit wrote after "
                "its snapshot was taken.",
                txn_id=txn.txn_id,
                snapshot_lsn=txn.snapshot.read_lsn,
                partitions=list(conflict),
            )
        if retried:
            self._increment_metric(COMMIT_RETRIES_TOTAL)
        self._publish_gauge(mode, open_now)
        return CommitReport(csn=committed, durable=True, wrote=True)

    def _declare_page_interest(
        self,
        txn: TransactionContext,
        rows: Sequence[_RowWrite],
    ) -> frozenset[tuple[str, PageIndex]]:
        """Declare interest in every page this commit is about to overwrite, then refuse silence.

        Two things happen here, and the second is the one defect E1 was about.

        First, every page the commit will write gets a partition naming it. A page image replaces
        the whole page, so two commits that write the same page conflict however disjoint the
        rows they thought they were touching were -- and the pages a row write lands on are only
        known now, which is why this runs inside the commit section rather than at staging time.
        Without it, three participants each creating a table all reported ``durable=True`` and
        two of them were lying: the third catalog image replaced the other two, the log still
        held every record, and ``verify`` called it clean.

        Second, a commit that has staged durable state and declares interest in NOTHING is
        refused outright. The predicate short-circuits on an empty set -- correctly, since an
        empty set intersects nothing -- so "I touched nothing" and "I forgot to say what I
        touched" were the same value, and only one of them is safe. They are now different: a
        transaction with nothing to write still commits through the read-only path, and one with
        something to write must say what.
        """
        materialized: set[tuple[str, PageIndex]] = set()
        for file, page_index in txn.staged_pages():
            txn.write_partitions.add(page_partition(file, page_index))
        for page_index in self._pages_touched_by(rows):
            partition = page_partition(self._heap_file, page_index)
            txn.write_partitions.add(partition)
            materialized.add((self._heap_file, page_index))
        # Every page this attempt actually modified, which is a superset of the two above and
        # is the one that includes a relinked chain page. Without it two participants appending
        # to one table both rewrote the same tail page and neither conflicted, because the page
        # that carried the difference was in nobody's interest set.
        for file, page_index in self._attempt_pages():
            partition = page_partition(file, page_index)
            txn.write_partitions.add(partition)
            materialized.add((file, page_index))
        if txn.wrote and not (txn.read_partitions or txn.write_partitions):
            raise GrafxTransactionStateError(
                "This transaction staged durable work and declared interest in no partition, so "
                "optimistic validation could never refuse it and a concurrent commit could "
                "replace what it wrote. Record what it touched with note_read or note_write.",
                txn_id=txn.txn_id,
                field="write_partitions",
                pending_records=len(txn.pending_records),
                page_images=len(txn.page_images),
                row_intents=len(txn.row_intents),
            )
        return frozenset(materialized)

    def _materialized_page_delta(
        self,
        txn: TransactionContext,
        *,
        snapshot_interest: frozenset[int],
        pre_staged_pages: frozenset[tuple[str, PageIndex]],
        materialized_pages: frozenset[tuple[str, PageIndex]],
    ) -> frozenset[int]:
        """Return only physical interests legitimately discovered from the fresh commit view.

        The first OCC pass freezes every interest the caller brought into commit, including
        internal pre-staged page images.  Heap materialisation may add page partitions because
        the target pages are unknowable before the current heap image is read.  Nothing else may
        change either set in that window: accepting a late logical partition here would silently
        move it from the transaction snapshot to the newer materialisation baseline, while losing
        an earlier partition would erase a conflict the first pass was required to preserve.

        Digest collisions are conservative.  A materialised page whose partition was already in
        ``snapshot_interest`` stays on the old baseline; it is not returned as new merely because
        this attempt reached the same numeric partition through another page.
        """
        current_staged_pages = frozenset(txn.staged_pages())
        staged_drift = current_staged_pages.symmetric_difference(pre_staged_pages)
        staged_overlap = pre_staged_pages.intersection(materialized_pages)
        fresh_partitions = frozenset(
            page_partition(file, page_index)
            for file, page_index in materialized_pages - pre_staged_pages
        )
        current_interest = frozenset(txn.read_partitions | txn.write_partitions)
        missing = snapshot_interest - current_interest
        added = current_interest - snapshot_interest
        eligible = fresh_partitions - snapshot_interest
        if missing or added != eligible or staged_drift or staged_overlap:
            raise GrafxTransactionStateError(
                "The transaction interest set changed while rows were being materialised. Only "
                "physical pages discovered from the current durable view, and never a page with "
                "a pre-staged image, may be added after the first optimistic validation.",
                field="transaction_interest",
                txn_id=txn.txn_id,
                missing=sorted(missing),
                added=sorted(added),
                materialized=sorted(eligible),
                staged_drift=sorted(staged_drift),
                staged_overlap=sorted(staged_overlap),
            )
        return frozenset(eligible)

    def _validate_staged_inputs(self, txn: TransactionContext) -> None:
        """Refuse caller-reachable durable inputs before the commit mutates a page or WAL."""
        # Row intents are public, mutable staging state. Validate them before even the index
        # provenance hook below: a malformed pending identity must not reach any collaborator,
        # and certainly must not be mistaken for a physical RecordRef by the heap.
        self._validate_row_intents(txn)
        unproved = txn.unproved_page_images()
        if unproved:
            raise GrafxConfigurationError(
                "Every physical page image in a transaction must carry the exact proof emitted "
                "by this manager's private store staging capability.",
                field="page_image_provenance",
                pages=list(unproved),
                txn_id=txn.txn_id,
            )
        for file, _page_index in txn.staged_pages():
            if not is_redoable_page_file(file):
                raise GrafxConfigurationError(
                    "A transaction page image must target heap.dat, catalog.dat or a canonical "
                    "index/<identifier>.idx file.",
                    field="file",
                    file=file,
                    txn_id=txn.txn_id,
                )
        # Caller-reachable pending_records needs the same early proof as physical pages. Keep
        # the second validation in _build_records too: legitimate row work can make the index
        # manager stage additional records later in this same attempt.
        self._validate_pending_index_records(txn, tuple(txn.pending_records))

    def _validate_row_intents(self, txn: TransactionContext) -> None:
        """Prove row intent shape and pending-reference provenance, then reduce safely."""
        pending_inserts: dict[int, PendingRowRef] = {}
        stored_tables: dict[RecordRef, int] = {}
        for position, raw_intent in enumerate(txn.row_intents):
            if not isinstance(raw_intent, RowIntent):
                raise GrafxConfigurationError(
                    "Every staged row change must be a RowIntent.",
                    field="row_intents",
                    position=position,
                    value=type(raw_intent).__name__,
                    txn_id=txn.txn_id,
                )
            intent = raw_intent
            operation = intent.operation
            if not isinstance(operation, RowOperation):
                raise GrafxConfigurationError(
                    "Every staged row intent must carry a supported RowOperation.",
                    field="row_operation",
                    position=position,
                    value=repr(operation),
                    txn_id=txn.txn_id,
                )
            table_id = getattr(intent.table, "table_id", None)
            if (
                isinstance(table_id, bool)
                or not isinstance(table_id, int)
                or table_id < 1
            ):
                raise GrafxConfigurationError(
                    "Every staged row intent must name a table with a positive table_id.",
                    field="table_id",
                    position=position,
                    value=repr(table_id),
                    txn_id=txn.txn_id,
                )
            reference = intent.reference
            if operation is RowOperation.INSERT:
                if reference is None:
                    continue  # accepted legacy insert; new inserts receive PendingRowRef
                if not isinstance(reference, PendingRowRef):
                    raise GrafxTransactionStateError(
                        "An insert may carry only its transaction-local pending reference.",
                        field="pending_row_reference",
                        position=position,
                        value=repr(reference),
                        txn_id=txn.txn_id,
                    )
            elif not isinstance(reference, (PendingRowRef, RecordRef)):
                raise GrafxTransactionStateError(
                    "An update or delete must name a physical or pending row reference.",
                    field="reference",
                    position=position,
                    value=repr(reference),
                    txn_id=txn.txn_id,
                )

            if isinstance(reference, PendingRowRef):
                if (
                    reference.txn_id != txn.txn_id
                    or reference.table_id != table_id
                    or not txn.owns_pending_row_ref(reference)
                ):
                    raise GrafxTransactionStateError(
                        "A pending row reference must be the exact live identity emitted by "
                        "this transaction for this table.",
                        field="pending_row_reference",
                        position=position,
                        value=repr(reference),
                        txn_id=txn.txn_id,
                        table_id=table_id,
                    )
                identity = id(reference)
                if operation is RowOperation.INSERT:
                    if identity in pending_inserts:
                        raise GrafxTransactionStateError(
                            "One pending row reference cannot name two inserts.",
                            field="pending_row_reference",
                            position=position,
                            value=repr(reference),
                            txn_id=txn.txn_id,
                        )
                    pending_inserts[identity] = reference
                elif pending_inserts.get(identity) is not reference:
                    raise GrafxTransactionStateError(
                        "A pending update or delete must follow the insert that emitted its "
                        "exact reference in this transaction.",
                        field="pending_row_reference",
                        position=position,
                        value=repr(reference),
                        txn_id=txn.txn_id,
                    )
            elif isinstance(reference, RecordRef):
                previous_table = stored_tables.setdefault(reference, table_id)
                if previous_table != table_id:
                    raise GrafxTransactionStateError(
                        "One physical row reference cannot be staged against two tables.",
                        field="reference",
                        position=position,
                        value=repr(reference),
                        txn_id=txn.txn_id,
                    )

        # The same pure reducer used by the writer is also the final sequence validator, and the
        # same pure planner proves every pending endpoint. Both run HERE, before the commit
        # section, so a promise this transaction cannot keep is refused while nothing of it has
        # been written; the writer runs them again on the same intents and, being pure, must
        # reach the same verdict.
        settled = reduce_row_intents(txn.row_intents)
        plan_relationship_endpoints(
            settled, txn_id=txn.txn_id, owns=txn.owns_pending_row_ref
        )
        # Identities a batch chose for itself are checked here too, and for the same reason: two
        # rows of one table under one identity must be refused while the transaction still has
        # nothing on a page, not when the second row reaches a heap that already holds the first.
        # The reuse check runs against the picture the caller had; it runs AGAIN inside the
        # section, against the settled read view, for the same reason optimistic validation does
        # -- this pass reports a caller's own mistake before the transaction spends anything, and
        # the later one decides against the picture as it is when the commit actually happens.
        self._refuse_reused_identities(txn, self._reserved_record_ids(txn, settled))

    def _validate_pending_index_records(
        self, txn: TransactionContext, records: Sequence[object]
    ) -> None:
        """Require staged logical records to match the index manager's private staging."""
        if not records:
            return
        manager = self._index_manager
        validator = getattr(manager, "validate_staged_records", None)
        if manager is None or not callable(validator):
            raise GrafxConfigurationError(
                "This transaction staged logical index records without an index manager that "
                "can prove their matching live changes.",
                field="pending_records",
                count=len(records),
                txn_id=txn.txn_id,
            )
        with self._close_wait_hazard():
            validator(txn, records)

    def _find_conflict(
        self,
        txn: TransactionContext,
        *,
        interested_partitions: frozenset[int] | None = None,
        baseline_lsn: Lsn | None = None,
    ) -> tuple[int, ...] | None:
        """Return the partitions that make this commit conflict, or None when none do.

        By default the predicate is the one CONTRACT.md section 8.5 step 3.3 states: a conflict
        exists when a COMMIT record appended after this transaction's snapshot WROTE a partition
        this transaction read or wrote.  The page-half pass supplies the strictly bounded
        exception: only physical partitions first discovered while materialising from a newer
        durable view are compared from that view's LSN.  Caller-declared logical interests and
        pre-staged page images never enter through that door.

        Both halves matter. Dropping the read set would let a transaction that decided something
        from a row commit after that row changed underneath it. Adding the other side's READ set
        would refuse two transactions that merely looked at the same partition, which BR-6 calls
        out by name: conflict is intersection, never the existence of another writer.
        """
        interested = (
            txn.read_partitions | txn.write_partitions
            if interested_partitions is None
            else interested_partitions
        )
        if not interested:
            # Nothing to intersect with. Skipping the scan cannot change the answer, because the
            # intersection of an empty set with anything is empty.
            return None
        floor = txn.snapshot.read_lsn if baseline_lsn is None else baseline_lsn
        if floor < txn.snapshot.read_lsn:
            raise GrafxTransactionStateError(
                "Optimistic validation cannot use a baseline older than the transaction's "
                "snapshot.",
                field="baseline_lsn",
                txn_id=txn.txn_id,
                baseline_lsn=floor,
                snapshot_lsn=txn.snapshot.read_lsn,
            )
        self._require_log_retains_from(floor + 1)
        for record in self._wal.read_from(floor + 1):
            if record.record_type != WalRecordType.COMMIT:
                continue
            if record.lsn <= floor:
                # The authority on the range is this comparison, not the argument passed to
                # read_from: a log that answers a start LSN generously must not be able to turn
                # a commit that this transaction has already seen into a conflict.
                continue
            payload = CommitPayload.decode(record.payload)
            overlap = interested.intersection(payload.write_partitions)
            if overlap:
                return tuple(sorted(overlap))
        return None

    def _require_log_retains_from(self, start: Lsn) -> None:
        """Refuse optimistic validation when the log has recycled records it would have to read.

        The predicate of step 3.3 is an intersection with every COMMIT above the snapshot. A
        checkpoint in another participant can recycle segments up to the horizon, and the horizon
        can pass this transaction's snapshot when its reader pin has gone stale -- idle longer
        than the stall threshold -- and was pruned. ``read_from`` then silently starts at the
        first retained segment, the conflicting COMMIT is simply not there, and a stale
        transaction commits over it: write skew, and step 3.3 not evaluated (C5 round-2 B4).

        A refusal here is retryable: a fresh snapshot sits above the horizon by construction.
        The log double used by part of the suite has no segments; the real log always does.
        """
        segments = getattr(self._wal, "segments", None)
        if segments is None:
            return
        retained = [info for info in segments() if info.first_lsn != NO_LSN]
        if not retained:
            return
        lowest = retained[0].first_lsn
        if lowest > start and self._wal.last_lsn >= start:
            raise GrafxWriteConflict(
                "Optimistic validation cannot run: the log no longer holds the commits above "
                "this transaction's snapshot (they were recycled by a checkpoint), so the "
                "conflict check would be answered from a log with a hole in it. Retry from a "
                "fresh snapshot.",
                snapshot_lsn=start - 1,
                lowest_retained_lsn=lowest,
            )

    def _validate_wal_batch_budget(
        self, txn: TransactionContext, records: Sequence[WalRecordLike]
    ) -> None:
        """Refuse an oversized final commit batch before the WAL builds its byte blob."""
        limit = self._max_wal_batch_bytes
        if limit is None:
            return
        observed = sum(record.encoded_length() for record in records)  # type: ignore[attr-defined]
        if observed <= limit:
            return
        raise GrafxTransactionBudgetExceeded(
            f"Transaction {txn.txn_id} would exceed max_wal_batch_bytes: limit {limit}, "
            f"observed {observed}.",
            field="max_wal_batch_bytes",
            limit=limit,
            observed=observed,
            txn_id=txn.txn_id,
        )

    def _compress_page_records(
        self,
        records: Sequence[WalRecordLike],
        images: Sequence[tuple[str, PageIndex, bytes]],
    ) -> list[WalRecordLike]:
        """Use WAL-v2 zlib images only after the durable capability fence is visible.

        The caller offers this helper only when the exact raw batch stays in its current WAL
        segment. Compression can only shorten that batch, so it cannot change the segment-roll
        decision or the terminal LSN already stamped into every page and logical effect. A raw
        batch that rolls deliberately remains v1; that bounded fallback avoids a compressed-size
        feedback loop while retargeting pages to the roll header's additional LSN.
        """

        if not self._wal_record_v2_capable or not images:
            return list(records)
        page_count = len(images)
        if len(records) <= page_count:
            raise GrafxTransactionStateError(
                "A materialised commit batch must end in COMMIT after its page effects.",
                field="records",
                value=len(records),
                page_records=page_count,
            )
        compressed_records: list[WalRecordLike] = []
        for position, (file, page_index, image) in enumerate(images):
            source = records[position]
            if not isinstance(source, WalRecord) or source.record_type != int(
                WalRecordType.WRITE_PAGE
            ):
                raise GrafxTransactionStateError(
                    "Materialised page images and WRITE_PAGE records lost their shared order.",
                    field="record_type",
                    value=getattr(source, "record_type", None),
                    position=position,
                )
            encoded = encode_page_write_record(
                file,
                page_index,
                image,
                compress=True,
            )
            compressed_records.append(
                replace(
                    source,
                    payload=encoded.payload,
                    format_version=encoded.format_version,
                    flags=encoded.flags,
                )
            )
        compressed_records.extend(records[page_count:])
        return compressed_records

    def _build_records(
        self,
        txn: TransactionContext,
        epoch: Epoch,
        rows: Sequence[_RowWrite] = (),
    ) -> tuple[list[WalRecordLike], list[tuple[str, PageIndex, bytes]], Csn]:
        """Return the records this commit appends and the page images it will then apply.

        The first materialisation uses the contiguous-batch candidate so every encoded length is
        known, including logical index effects. WalManager then previews whether that exact body
        rolls and inserts its own SEGMENT_HEADER. If it does, :meth:`_retarget_commit_batch`
        rewrites the local images and private index staging to the exact terminal LSN before
        ``append_many(expected_terminal_lsn=...)`` revalidates the plan without writing a byte.

        The same corrected bytes travel in WRITE_PAGE and are installed after the barrier. Thus
        heap headers, index changes, page_lsn, the COMMIT record and CommitReport all name one
        CSN, while the live frames stay provisional until durability is established.
        """
        base = _require_lsn("last_lsn", self._wal.last_lsn)
        page_stamps = self._group_page_stamps(rows)
        staged = list(txn.staged_pages())
        # Preserve the pre-TXN-4 deterministic page order; the grouping map follows row order.
        for page_index in sorted(page_stamps):
            if (self._heap_file, page_index) not in txn.page_images:
                staged.append((self._heap_file, page_index))
        # The measured set, so a page this attempt changed without a row landing on it is
        # carried by the log too. A chain link that is not logged is a change no redo can
        # reproduce: the page it makes reachable is in the log, the fact that it is reachable
        # is not, and a replay rebuilds a heap that has lost the tail of every table.
        for file, page_index in self._attempt_pages():
            if (file, page_index) not in txn.page_images:
                staged.append((file, page_index))
        staged = sorted(set(staged))
        for file, _page_index in staged:
            if not is_redoable_page_file(file):
                raise GrafxConfigurationError(
                    "A commit can log pages only for heap.dat, catalog.dat or a canonical "
                    "index/<identifier>.idx file.",
                    field="file",
                    file=file,
                    txn_id=txn.txn_id,
                )
        # The index changes are staged HERE, between knowing the batch length and building the
        # records, because they are part of that batch: they lengthen it, and the number they
        # carry is the number the lengthened batch gives the COMMIT record. Counting them first
        # is what breaks that circle; `_stage_index_changes` refuses if the count was wrong.
        index_record_count = self._index_record_count(txn, rows)
        predicted = (
            base + len(staged) + len(txn.pending_records) + index_record_count + 1
        )
        if predicted >= PROVISIONAL_CSN:
            raise GrafxTransactionStateError(
                "The write-ahead log has exhausted its usable commit-number space; the maximum "
                "unsigned value is reserved for provisional heap versions.",
                field="last_lsn",
                value=base,
            )
        manager = self._index_manager
        uses_canonical_index_staging = (
            type(manager) is IndexManager
            and getattr(self._index_record_count, "__func__", self._index_record_count)
            is TransactionManager._index_record_count
            and getattr(
                self._stage_index_changes, "__func__", self._stage_index_changes
            )
            is TransactionManager._stage_index_changes
        )
        if uses_canonical_index_staging:
            # Count and staging run in this same COMMIT_SECTION against the same immutable
            # catalog authority.  Carry that one-shot observation into the verifier instead of
            # asking the catalog the identical question a second time.  Custom managers and
            # overridden hooks retain the legacy double-call path because their calls may be
            # observable or deliberately time-varying.
            self._stage_index_changes(
                txn,
                rows,
                predicted,
                _expected_record_count=index_record_count,
            )
        else:
            self._stage_index_changes(txn, rows, predicted)
        pending = list(txn.pending_records)
        self._validate_pending_index_records(txn, pending)
        images: list[tuple[str, PageIndex, bytes]] = []
        records: list[WalRecordLike] = []
        self._materialized = _MaterializedAttempt(
            txn_id=int(txn.txn_id),
            csn=predicted,
            pages={},
            page_stamps=page_stamps,
        )
        for file, page_index in staged:
            image = txn.page_images.get((file, page_index))
            stamps = page_stamps.get(page_index, ()) if file == self._heap_file else ()
            if image is None:
                # Materialised in this process: the resident frame is the authority and was
                # verified when it entered the pool. Copy it, stamp the copy, encode ONCE.
                stamped = self._local_image(file, page_index, predicted, stamps)
            else:
                # Staged by a collaborator as bytes: the bytes are the authority and are
                # decoded with verification before anything is stamped into them.
                stamped = self._committed_image(
                    file,
                    page_index,
                    image,
                    predicted,
                    stamps,
                )
            images.append((file, page_index, stamped))
            records.append(
                WalRecord(
                    record_type=int(WalRecordType.WRITE_PAGE),
                    epoch=epoch,
                    txn_id=txn.txn_id,
                    payload=encode_page_write(file, page_index, stamped),
                    descriptor=self._descriptor,
                )
            )
        records.extend(self._with_commit_epoch(record, epoch) for record in pending)
        payload = CommitPayload.build(
            snapshot_lsn=txn.snapshot.read_lsn,
            read_partitions=txn.read_partitions,
            write_partitions=txn.write_partitions,
            page_touches=[
                PageTouch(file_id=self._file_ids.id_of(file), page_index=page_index)
                for file, page_index in staged
            ],
        )
        records.append(
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=epoch,
                txn_id=txn.txn_id,
                payload=payload.encode(),
                descriptor=self._descriptor,
            )
        )
        return records, images, predicted

    def _retarget_commit_batch(
        self,
        txn: TransactionContext,
        records: Sequence[WalRecordLike],
        images: Sequence[tuple[str, PageIndex, bytes]],
        rows: Sequence[_RowWrite],
        *,
        old_csn: Csn,
        new_csn: Csn,
        epoch: Epoch,
    ) -> tuple[list[WalRecordLike], list[tuple[str, PageIndex, bytes]]]:
        """Retarget one materialised batch to the WAL's exact terminal LSN.

        ``_build_records`` knows the cardinality and encoded size of every effect, but only the
        WAL knows whether that size forces a segment roll and therefore inserts a header LSN.
        Retargeting rewrites local page-image values and private logical-index staging only; live
        heap frames remain provisional until the barrier. The final ``append_many`` revalidates
        this terminal before its first byte, closing drift between the preview and append.
        """
        if new_csn <= NO_CSN or new_csn >= PROVISIONAL_CSN:
            raise GrafxTransactionStateError(
                "A commit batch must target a usable, non-provisional WAL sequence number.",
                field="commit_csn",
                value=new_csn,
            )
        manager = self._index_manager
        if txn.pending_records:
            retarget = getattr(manager, "retarget_staged", None)
            if manager is None or not callable(retarget):
                raise GrafxTransactionStateError(
                    "The WAL roll changed the commit number, but the index registry cannot "
                    "atomically retarget its private staging before append.",
                    field="index_manager",
                    old_csn=old_csn,
                    new_csn=new_csn,
                )
            retarget(txn, old_csn, new_csn)
            self._validate_pending_index_records(txn, tuple(txn.pending_records))

        if not records or records[-1].record_type != int(WalRecordType.COMMIT):
            raise GrafxTransactionStateError(
                "A materialised commit batch must end in exactly one COMMIT record.",
                field="records",
                count=len(records),
            )
        pending = list(txn.pending_records)
        if len(records) != len(images) + len(pending) + 1:
            raise GrafxTransactionStateError(
                "Retargeting changed the staged-record cardinality of a materialised commit.",
                field="pending_records",
                records=len(records),
                pages=len(images),
                pending=len(pending),
            )

        corrected_images: list[tuple[str, PageIndex, bytes]] = []
        corrected_records: list[WalRecordLike] = []
        attempt = self._materialized
        # Only the pages THIS attempt materialised at old_csn may be re-stamped: a page value
        # left by another transaction or by an earlier materialisation of this one would
        # carry bytes the log never saw, so any mismatch takes the verifying path.
        reusable_attempt = (
            attempt
            if attempt is not None
            and attempt.txn_id == int(txn.txn_id)
            and attempt.csn == old_csn
            else None
        )
        reusable = {} if reusable_attempt is None else reusable_attempt.pages
        page_stamps = (
            reusable_attempt.page_stamps
            if reusable_attempt is not None and reusable_attempt.page_stamps is not None
            else self._group_page_stamps(rows)
        )
        self._materialized = None
        for file, page_index, image in images:
            page = reusable.get((file, page_index))
            stamps = page_stamps.get(page_index, ()) if file == self._heap_file else ()
            if page is None:
                # Not produced by _build_records in this attempt (a caller-built batch):
                # the bytes are all there is, so they are verified before being re-stamped.
                corrected = self._committed_image(
                    file,
                    page_index,
                    image,
                    new_csn,
                    stamps,
                )
            else:
                # The page value was already validated (decoded with verification, or
                # copied from a verified frame) and stamped with old_csn: re-stamp it to
                # the terminal LSN and encode once more, without decoding the bytes again.
                self._stamp_page(page, file, page_index, new_csn, stamps)
                corrected = self._pool.codec.encode_page(page)
            corrected_images.append((file, page_index, corrected))
            corrected_records.append(
                WalRecord(
                    record_type=int(WalRecordType.WRITE_PAGE),
                    epoch=epoch,
                    txn_id=txn.txn_id,
                    payload=encode_page_write(file, page_index, corrected),
                    descriptor=self._descriptor,
                )
            )
        corrected_records.extend(
            self._with_commit_epoch(record, epoch) for record in pending
        )
        corrected_records.append(records[-1])
        return corrected_records, corrected_images

    def _with_commit_epoch(self, record: WalRecordLike, epoch: Epoch) -> WalRecordLike:
        """Return the staged record carrying the epoch that is COMMITTING it.

        A record is staged when the work is done and appended when the commit runs, and the two
        are different instants: this component takes the writer lease for the commit window, so
        at staging time there is no epoch to carry. Section 6.5 says the header field names the
        epoch of the bearer, and the bearer is the writer that put the record in the log -- which
        makes staging time the wrong instant to stamp, and an unstamped record worse than wrong:
        C4 refuses a batch carrying an epoch older than the log already holds, so a record staged
        with a zero would take the whole commit down with it.

        A staged record that is not a dataclass is appended exactly as it came. This component
        does not own the record type and will not rebuild one it does not recognise.
        """
        if getattr(record, "epoch", None) == epoch:
            return record
        if is_dataclass(record) and not isinstance(record, type):
            return replace(record, epoch=epoch)  # type: ignore[type-var]
        return record

    def _index_row_at(
        self, reference: RecordRef
    ) -> tuple[int | None, tuple[object, ...]]:
        """Return the durable identity and values an ended index entry must derive from.

        Read from the heap rather than taken from the caller: inside the commit section the heap
        is the authority, and a caller's copy of a row can be one version behind. A reference the
        heap cannot read yields no identity and no values. The heap mutation that follows retains
        its own refusal, while an active identity index also fails closed rather than deriving a
        key for the wrong logical row.
        """
        if self._index_manager is None:
            return None, ()
        try:
            version = self._heap.read(reference)
            return int(version.record_id), tuple(version.values)
        except GrafxError:
            return None, ()

    def _index_record_count(
        self, txn: TransactionContext, rows: Sequence[_RowWrite]
    ) -> int:
        """Return how many log records the index staging of these rows will produce.

        The number is needed BEFORE the staging happens, because the commit number every index
        change carries is the sequence number of the COMMIT record, and that number depends on
        how long the batch is -- which these very records lengthen. Counting first breaks the
        circle without a provisional stamp to correct afterwards.

        Keeping the count and the staging in step is an invariant in two places (A66), so the
        canonical path carries this count into :meth:`_stage_index_changes` and compares it with
        what staging actually produced. Custom hooks retain the earlier second count because a
        host may make the invocation itself observable.
        """
        manager = self._index_manager
        if manager is None:
            return 0
        total = 0
        for row in rows:
            table_id = getattr(row.table, "table_id", None)
            if table_id is None:
                continue
            table_name = getattr(row.table, "name", None)
            if not isinstance(table_name, str):
                table_name = None
            if row.ended is not None:
                total += manager.row_entry_count(
                    table_id,
                    row.ended_values,
                    record_id=row.record_id,
                    table_name=table_name,
                    table=row.table,
                    txn=txn,
                )
            if row.born is not None:
                total += manager.row_entry_count(
                    table_id,
                    row.born_values,
                    record_id=row.record_id,
                    table_name=table_name,
                    table=row.table,
                    txn=txn,
                )
        return total

    def _stage_index_changes(
        self,
        txn: TransactionContext,
        rows: Sequence[_RowWrite],
        csn: Csn,
        *,
        _expected_record_count: int | None = None,
    ) -> None:
        """Stage, on every index covering each written row, the entries that row owes it.

        This is the seam that makes a secondary index real. Without it ``IndexManager``'s three
        staging doors have no caller at all: a commit writes rows to the heap, applies whatever
        was staged into the indexes, and nothing between the two ever stages anything -- so every
        index stays empty for ever, a lookup answers nothing, and a similarity search returns no
        hits for rows that are visible in the heap. It is not specific to the vector subsystem;
        an ordinary hash index is equally empty.

        The order is delete-then-insert for an update, and both halves always run even when the
        key did not change: an update writes a new version at a NEW location, so the entry that
        pointed at the old one has to end whatever its key looks like.
        """
        self._refuse_unresolved_rows(txn, rows)
        manager = self._index_manager
        if manager is None:
            return
        before = len(txn.pending_records)
        expected = (
            self._index_record_count(txn, rows)
            if _expected_record_count is None
            else _expected_record_count
        )
        for row in rows:
            table_id = getattr(row.table, "table_id", None)
            if table_id is None:
                continue
            table_name = getattr(row.table, "name", None)
            if not isinstance(table_name, str):
                table_name = None
            if row.ended is not None:
                manager.stage_row_delete(
                    txn,
                    table_id,
                    row.ended,
                    row.ended_values,
                    csn,
                    record_id=row.record_id,
                    table_name=table_name,
                    table=row.table,
                )
            if row.born is not None:
                manager.stage_row_insert(
                    txn,
                    table_id,
                    row.born,
                    row.born_values,
                    csn,
                    record_id=row.record_id,
                    table_name=table_name,
                    table=row.table,
                )
        produced = len(txn.pending_records) - before
        if produced != expected:
            raise GrafxTransactionStateError(
                f"The index staging produced {produced} log records where the commit number was "
                f"computed for {expected}, so the entries would carry a number the log did not "
                f"assign.",
                txn_id=txn.txn_id,
                expected=expected,
                produced=produced,
            )

    def _recover_post_barrier(
        self,
        txn: TransactionContext,
        previous: CommitState,
        committed: Csn,
        rows: Sequence[_RowWrite],
        *,
        catalog_touched: bool = False,
    ) -> bool:
        """Close the P4 window for THIS commit as far as it can be closed from here.

        The commit is durable: the barrier returned. What failed is the apply -- page images,
        index changes, or the publication -- and left to a later commit's publication, the rows
        would become visible with the index behind them: a lookup answering fewer rows than exist,
        which is the wrong answer this component's index seam exists to prevent (found by the
        thread-concurrency test under suite load: ``index_entry_missing`` after a commit refused
        post-barrier with a retryable budget error).

        So the commit is REDONE from the log through the idempotent doors recovery uses -- page
        images and index records alike -- and published. Only if the redo itself fails are the
        indexes that cover this commit's tables marked STALE, durably: a lookup then refuses
        until a rebuild, which is honest where a short answer is not. Nothing here raises; the
        failure that got us here is the one the caller reports.
        """
        try:
            self._drop_index_changes(txn)
            self._redo_onto_device(previous.last_committed_lsn, committed)
            self._publish_commit_state(
                previous, committed, catalog_touched=catalog_touched
            )
            return True
        except BaseException:
            pass
        manager = self._index_manager
        if manager is None:
            return False
        tables = {
            (
                getattr(row.table, "table_id", None),
                getattr(row.table, "name", None),
            )
            for row in rows
        }
        for table_id, table_name in tables:
            if table_id is None or not isinstance(table_name, str):
                continue
            active_for = getattr(manager, "active_indexes_for", manager.indexes_for)
            for index in active_for(table_id, table_name=table_name):
                try:
                    index.mark_stale(
                        f"commit {committed} was durable but could not be applied to this index "
                        f"and the redo from the log failed too"
                    )
                except BaseException:
                    continue
        return False

    def _apply_index_changes(self, txn: TransactionContext, csn: Csn) -> int:
        """Apply what this transaction staged into the indexes, and return how many moved.

        It runs in the step 6 region, after the barrier of step 3.5 and beside the page images,
        because that is where C7 says the call belongs: the number handed over is the one the log
        assigned, and no index page is touched before the commit is durable.

        A database built with no index manager has nothing to apply, which is the ordinary state
        of this component's own suite and of any database with no secondary index.
        """
        manager = self._index_manager
        if manager is None:
            return 0
        fenced_commit = getattr(manager, "_commit_under_write_authority", None)
        applied = int(
            fenced_commit(txn, csn)
            if callable(fenced_commit)
            else manager.commit(txn, csn)
        )
        return applied

    def _drop_index_changes(self, txn: TransactionContext) -> int:
        """Drop what this transaction staged into the indexes, and return how many were dropped.

        An abandoned transaction leaves nothing anywhere, and the index staging is part of
        anywhere: a change left staged under a transaction number that will never commit would be
        applied by whatever reused that number next.
        """
        manager = self._index_manager
        if manager is None:
            return 0
        return int(manager.rollback(txn))

    @property
    def _heap_file(self) -> str:
        """Return the file the heap of this database writes."""
        return self._file_ids.heap_file

    def _refuse_unresolved_rows(
        self, txn: TransactionContext, rows: Sequence[_RowWrite]
    ) -> None:
        """Refuse a written row whose values still name a private identity.

        The rows are what the indexes are keyed on and what the WAL carries, so this is the last
        place a transient token could turn into a durable one. It should be unreachable -- the
        values came from intents this commit already resolved -- and it is stated anyway, because
        an invariant that is only true by construction stops being true the moment someone adds a
        second way to build a row.
        """
        for row in rows:
            for values in (row.born_values, row.ended_values):
                for slot, value in enumerate(values or ()):
                    if isinstance(value, PendingRowRef):
                        raise GrafxTransactionStateError(
                            "A written row still names a pending identity, which must never "
                            "reach an index or the log.",
                            field="row_values",
                            slot=slot,
                            value=repr(value),
                            table=getattr(row.table, "name", None),
                            txn_id=txn.txn_id,
                        )

    def _write_rows(
        self, txn: TransactionContext, identities: _IdentityPlan
    ) -> tuple[_RowWrite, ...]:
        """Write the rows this transaction staged and return where each one landed.

        The staged intents are settled first (see :func:`reduce_row_intents`), because a stored row
        can be named more than once by one transaction and every version may be ended exactly
        once. Settling is not an optimisation: without it, a transaction that updates a row and
        then deletes it asks the heap to end the same version twice, and the heap refuses -- so a
        perfectly ordinary pair of statements fails inside the commit section, where the only
        refusals still meant to be possible are device failures.

        This runs inside the commit section, after optimistic validation has passed, so the only
        refusals still ahead are device failures. The identity of a row with none is allocated
        here for the same reason: an id burned by a commit that then fails leaves a GAP in the
        sequence, which no reader can observe, where an id handed out twice would put two rows
        under one identity.

        The rows are written with the reserved provisional sentinel.  The live frames retain that
        sentinel through append and the WAL barrier; :meth:`_committed_image` corrects only local
        image copies for the WAL.  Thus eviction or failed cleanup before durability can persist
        unreachable space, but never a birth or ending a real snapshot can observe.  Step 3.6
        installs the corrected images only after the barrier returns.
        """
        txn._effective_row_tables = None
        if not txn.row_intents:
            txn._effective_row_tables = frozenset()
            return ()
        heap = self._heap
        provisional = PROVISIONAL_CSN
        written: list[_RowWrite] = []
        try:
            effective_row_tables = self._write_intents(
                txn, heap, provisional, written, identities
            )
        except BaseException as failure:
            # The intents already written are abandoned HERE, by the one frame that knows
            # about them. The caller sees only what this method returns, and a refusal on the
            # second intent of a batch returned nothing -- so the first intent stayed written,
            # unabandoned, and the next commit of anyone flushed and published it as a row no
            # transaction ever committed (C5 round-2 B1).
            cleanup_failure = self._abandon_rows(tuple(written))
            if cleanup_failure is not None:
                self._recovery_required = True
                _note_cleanup_failure(failure, cleanup_failure)
            raise
        txn._effective_row_tables = effective_row_tables
        txn.row_refs = [item.born for item in written if item.born is not None]
        return tuple(written)

    def _write_intents(
        self,
        txn: TransactionContext,
        heap: object,
        provisional: Csn,
        written: list[_RowWrite],
        identities: _IdentityPlan,
    ) -> frozenset[tuple[int, str]]:
        """Write settled intents and return complete identities of materialized tables."""
        effective_row_tables: set[tuple[int, str]] = set()
        for position, intent in enumerate(self._resolved_intents(txn, identities)):
            effective_row_tables.add((intent.table.table_id, intent.table.name))
            if intent.operation is RowOperation.DELETE:
                record_id, ending = self._index_row_at(intent.reference)
                heap.delete(intent.table, intent.reference, provisional)
                written.append(
                    _RowWrite(
                        born=None,
                        ended=intent.reference,
                        table=intent.table,
                        ended_values=ending,
                        record_id=record_id,
                    )
                )
                continue
            if intent.operation is RowOperation.UPDATE:
                record_id, ending = self._index_row_at(intent.reference)
                reference = heap.update(
                    intent.table, intent.reference, intent.values, provisional
                )
                written.append(
                    _RowWrite(
                        born=reference,
                        ended=intent.reference,
                        table=intent.table,
                        born_values=tuple(intent.values),
                        ended_values=ending,
                        record_id=record_id,
                    )
                )
                continue
            # Planned above, never None here: the identity had to exist before the row was
            # written, because an edge staged in this same transaction may already carry it.
            record_id = intent.record_id
            if position in identities.leased_positions:
                reference = heap.insert_reserved(
                    intent.table, record_id, intent.values, provisional
                )
            else:
                # The first row of a table has no extent to reserve yet.  Its ordinary insert
                # creates the extent and advances the floor atomically with the user commit;
                # leasing starts on the next transaction.
                reference = heap.insert(
                    intent.table, record_id, intent.values, provisional
                )
            written.append(
                _RowWrite(
                    born=reference,
                    ended=None,
                    table=intent.table,
                    born_values=tuple(intent.values),
                    record_id=record_id,
                )
            )
        return frozenset(effective_row_tables)

    def _resolved_intents(
        self, txn: TransactionContext, identities: _IdentityPlan
    ) -> tuple[RowIntent, ...]:
        """Return the settled intents with every pending identity turned into a stored value.

        This runs before the first row is written, and that ordering is the whole point. An edge
        staged against a node the same transaction is still creating carries that node's PRIVATE
        identity, which is a negative token that must never reach the heap, an index or the WAL.
        Resolving it needs the node's durable id, and the node's durable id is not allocated until
        the row is written -- so the ids are PLANNED first, from the counter each table would hand
        out next, and only then does anything move.

        Planning rather than allocating also keeps the failure shape right. An identity spent by
        an attempt that then refuses leaves a GAP in the sequence, which no reader can observe;
        an identity handed out twice would put two rows under one name. Planning spends nothing:
        the counter is advanced by ``observe_record_id`` as each row actually lands, so an attempt
        abandoned here leaves the counter exactly where it was.
        """
        settled = identities.intents
        plans = plan_relationship_endpoints(
            settled, txn_id=txn.txn_id, owns=txn.owns_pending_row_ref
        )
        planned = identities.record_ids
        endpoints: dict[int, dict[int, int]] = {}
        for plan in plans:
            endpoints.setdefault(plan.relationship_position, {})[plan.slot] = planned[
                plan.endpoint_position
            ]
        resolved = list(settled)
        for position, intent in enumerate(settled):
            identity = planned.get(position)
            slots = endpoints.get(position)
            if identity is None and slots is None:
                continue
            values = intent.values
            if slots is not None:
                values = tuple(
                    slots.get(slot, value) for slot, value in enumerate(intent.values)
                )
            resolved[position] = replace(
                intent,
                values=values,
                record_id=intent.record_id if identity is None else identity,
            )
        settled_rows = tuple(resolved)
        self._refuse_unresolved_intents(txn, settled_rows)
        return settled_rows

    def _prepare_identity_plan(
        self, txn: TransactionContext
    ) -> tuple[_IdentityPlan, dict[int, tuple[object, tuple[int, ...]]]]:
        """Spend cached identities and describe the ranges that still need a durable refill.

        The caller owns the participant section for the whole commit.  Keeping preparation in
        that same section is a lifecycle invariant: once commit wins selection, ``close`` and
        ``rollback`` cannot enter between identity preparation and the commit attempt.  Only
        process-local cache entries are consumed here.  Any shared floor advance is deferred to
        :meth:`_reserve_identity_plan`, under the writer lease and COMMIT_SECTION.

        A slice is burn-only.  It is advanced before an identity reaches the row writer and is
        never rewound, so conflict, rollback, close and crash can create gaps but cannot reuse an
        id.  Tables without an extent stay on the legacy path because creating the first extent
        before the surrounding catalog/user transaction commits would break atomicity.
        """
        intents = tuple(reduce_row_intents(txn.row_intents))
        self._sync_identity_process()

        groups: dict[int, tuple[object, list[int]]] = {}
        for position, intent in enumerate(intents):
            if intent.operation is not RowOperation.INSERT:
                continue
            table_id = getattr(intent.table, "table_id", None)
            group = groups.setdefault(table_id, (intent.table, []))
            group[1].append(position)

        planned: dict[int, int] = {}
        leased_positions: set[int] = set()
        pending: dict[int, tuple[object, tuple[int, ...]]] = {}
        for table_id in sorted(groups):
            table, positions_list = groups[table_id]
            positions = tuple(positions_list)
            explicit = tuple(
                position
                for position in positions
                if intents[position].record_id is not None
            )
            for position in explicit:
                planned[position] = intents[position].record_id  # type: ignore[assignment]
            implicit = tuple(
                position for position in positions if position not in explicit
            )
            if explicit:
                # A named id invalidates every local inference about the shared floor.  Burn the
                # cached remainder first, then fail early when our current durable view already
                # proves the id is below the floor.  The locked refill repeats this proof against
                # a fresh cross-process view before accepting anything.
                self._identity_leases.pop(table_id, None)
                extent = self._heap.extent_of(table)
                if extent is not None:
                    explicit_ids = tuple(
                        int(intents[position].record_id) for position in explicit
                    )
                    below = tuple(
                        identity
                        for identity in explicit_ids
                        if identity < int(extent.next_record_id)
                    )
                    if below:
                        self._raise_explicit_below_identity_floor(
                            txn,
                            table,
                            table_id,
                            below,
                            int(extent.next_record_id),
                        )
                pending[table_id] = (table, positions)
                continue
            lease = self._identity_leases.get(table_id)
            if lease is not None and lease.stop - lease.next_id >= len(implicit):
                start = lease.next_id
                lease.next_id += len(implicit)  # burn before hand-off
                if lease.next_id >= lease.stop:
                    self._identity_leases.pop(table_id, None)
                for offset, position in enumerate(implicit):
                    planned[position] = start + offset
                    leased_positions.add(position)
                continue
            # A short remainder is deliberately not split across two intervals.  Dropping it
            # burns it, and one fresh reservation covers the whole batch deterministically.
            self._identity_leases.pop(table_id, None)
            pending[table_id] = (table, positions)

        return (
            _IdentityPlan(
                intents=intents,
                record_ids=planned,
                leased_positions=frozenset(leased_positions),
            ),
            pending,
        )

    def _validate_explicit_identity_floors(self, txn: TransactionContext) -> None:
        """Reject explicit ids this participant already proves are below a durable floor.

        This early check deliberately performs no cache mutation and no durable work.  It keeps
        caller-controlled identity refusals ahead of lease acquisition while the locked planner
        below remains authoritative when another process has advanced a floor since this pool's
        current view.
        """
        intents = tuple(reduce_row_intents(txn.row_intents))
        for table_id, (table, identities) in self._reserved_record_ids(
            txn, intents
        ).items():
            extent = self._heap.extent_of(table)
            if extent is None:
                continue
            floor = int(extent.next_record_id)
            below = tuple(
                sorted(identity for identity in identities if identity < floor)
            )
            if below:
                self._raise_explicit_below_identity_floor(
                    txn, table, table_id, below, floor
                )

    def _reserve_identity_plan(
        self,
        txn: TransactionContext,
        base: _IdentityPlan,
        pending: dict[int, tuple[object, tuple[int, ...]]],
        *,
        previous: CommitState,
        lease: LeaseGuard,
    ) -> tuple[CommitState, _IdentityPlan]:
        """Durably reserve every pending table while this commit owns global writer ordering.

        The refill is a distinct WAL transaction, but not a recursive public commit.  The outer
        commit already owns the participant section, writer lease, COMMIT_SECTION and WAL-tail
        guard; a private context is materialised directly through the same append/barrier/apply/
        publish protocol.  Therefore no lifecycle or metric callback can enter between selecting
        the user commit and completing its metadata prerequisite, and every process observes
        disjoint ranges in the same order in which ordinary commits are already serialised.
        """
        try:
            floor_plan = None
            for refresh_attempt in range(2):
                planned = dict(base.record_ids)
                leased_positions = set(base.leased_positions)
                floors: dict[object, int] = {}
                expected_floors: dict[int, int] = {}
                cache_ranges: dict[int, tuple[int, int]] = {}
                existing_positions: dict[int, tuple[int, ...]] = {}

                # The outer commit established a current read view under COMMIT_SECTION.  Repeat
                # the explicit reuse scan against it; a row may have committed since the user's
                # older snapshot.
                self._refuse_reused_identities(
                    txn, self._reserved_record_ids(txn, base.intents)
                )
                for table_id in sorted(pending):
                    table, positions = pending[table_id]
                    extent = self._heap.extent_of(table)
                    explicit = tuple(
                        position
                        for position in positions
                        if base.intents[position].record_id is not None
                    )
                    implicit = tuple(
                        position
                        for position in positions
                        if base.intents[position].record_id is None
                    )
                    explicit_ids = tuple(
                        int(base.intents[position].record_id) for position in explicit
                    )
                    if extent is None:
                        cursor = max(
                            (
                                FIRST_RECORD_ID,
                                *(identity + 1 for identity in explicit_ids),
                            )
                        )
                        for position in explicit:
                            identity = int(base.intents[position].record_id)
                            self._require_unexhausted_identity(
                                txn, table, table_id, identity
                            )
                            planned[position] = identity
                        for position in implicit:
                            self._require_unexhausted_identity(
                                txn, table, table_id, cursor
                            )
                            planned[position] = cursor
                            cursor += 1
                        # Not leased: the user transaction must create the extent and floor
                        # atomically through HeapStore.insert.
                        continue

                    floor = int(extent.next_record_id)
                    below = tuple(
                        identity for identity in explicit_ids if identity < floor
                    )
                    if below:
                        self._raise_explicit_below_identity_floor(
                            txn, table, table_id, below, floor
                        )
                    for position in explicit:
                        identity = int(base.intents[position].record_id)
                        self._require_unexhausted_identity(
                            txn, table, table_id, identity
                        )
                        planned[position] = identity

                    if explicit_ids:
                        cursor = max(floor, max(explicit_ids) + 1)
                    else:
                        cursor = floor
                    for position in implicit:
                        self._require_unexhausted_identity(txn, table, table_id, cursor)
                        planned[position] = cursor
                        cursor += 1

                    # An implicit-only refill has ``identity_lease_size`` total slots, including
                    # this batch.  An explicit high-water jump gets that many cache slots beyond
                    # the batch, preserving the established explicit+implicit behaviour.
                    if not explicit_ids:
                        stop = floor + max(self._identity_lease_size, len(implicit))
                    else:
                        stop = cursor + self._identity_lease_size
                    stop = min(stop, MAX_U64)
                    if stop <= floor:
                        self._require_unexhausted_identity(txn, table, table_id, floor)
                    floors[table] = stop
                    expected_floors[table_id] = floor
                    cache_ranges[table_id] = (cursor, stop)
                    existing_positions[table_id] = positions

                if not floors:
                    return (
                        previous,
                        _IdentityPlan(
                            intents=base.intents,
                            record_ids=planned,
                            leased_positions=frozenset(leased_positions),
                        ),
                    )

                try:
                    floor_plan = self._heap.plan_record_id_floors(floors)
                except GrafxTransactionStateError as stale_floor:
                    if (
                        stale_floor.details.get("field") != "next_record_id"
                        or "old_floor" not in stale_floor.details
                        or refresh_attempt > 0
                    ):
                        raise
                    # A stale resident frame proposed a floor the detached device page already
                    # passed.  No range was staged or spent; force a full local re-read while the
                    # global commit lock prevents the device from moving again, then rebuild.
                    self._pool.begin_read_view(object())
                    continue
                observed_floors = {
                    advance.table_id: advance.old_floor
                    for advance in floor_plan.advances
                }
                if observed_floors != expected_floors:
                    if refresh_attempt > 0:
                        raise GrafxTransactionStateError(
                            "The resident and durable identity floors still disagree after a "
                            "locked fresh read; no identity was handed to the user transaction.",
                            field="next_record_id",
                            expected_floors=expected_floors,
                            observed_floors=observed_floors,
                            txn_id=txn.txn_id,
                        )
                    self._pool.begin_read_view(object())
                    continue
                break
            if floor_plan is None:
                raise GrafxTransactionStateError(
                    "A locked identity-floor reservation could not establish one stable page "
                    "zero image.",
                    field="next_record_id",
                    txn_id=txn.txn_id,
                )

            reserved_state, reservation_lsn = self._commit_identity_floor_plan(
                floor_plan.page_index,
                floor_plan.image,
                previous=previous,
                lease=lease,
                user_txn_id=txn.txn_id,
            )
            self._sync_identity_process()
            for table_id, positions in existing_positions.items():
                leased_positions.update(positions)
                next_id, stop = cache_ranges[table_id]
                if next_id < stop:
                    self._identity_leases[table_id] = _IdentityLease(next_id, stop)
                else:
                    self._identity_leases.pop(table_id, None)
            return (
                reserved_state,
                _IdentityPlan(
                    intents=base.intents,
                    record_ids=planned,
                    leased_positions=frozenset(leased_positions),
                    reservation_lsn=reservation_lsn,
                ),
            )
        except BaseException:
            # Any uncertainty burns every local remainder.  Durable floors are monotone, so this
            # loses only capacity and never makes an identity reusable.
            self._identity_leases.clear()
            raise

    def _commit_identity_floor_plan(
        self,
        page_index: PageIndex,
        image: bytes,
        *,
        previous: CommitState,
        lease: LeaseGuard,
        user_txn_id: TxnId,
    ) -> tuple[CommitState, Lsn]:
        """Commit one detached floor image through WAL before its identities can be used."""
        trace = self._active_commit_trace
        reservation = TransactionContext(
            txn_id=self._next_txn_id,
            mode=TransactionMode.WRITE,
            snapshot=Snapshot(previous.last_committed_lsn),
            epoch=_NO_EPOCH,
            owner=self,
            page_staging_capability=self._page_staging_capability,
        )
        self._next_txn_id += 1
        # The context is intentionally not present in ``_open``; use the same unforgeable
        # capability directly instead of the public-context ownership gate.
        reservation._stage_page_image(
            self._heap_file,
            page_index,
            image,
            capability=self._page_staging_capability,
        )
        reservation.note_write(page_partition(self._heap_file, page_index))

        # A metadata page is not caller row payload.  The database-wide WAL batch limit still
        # applies, and the private transaction contains exactly one page plus one COMMIT.
        self._pool.forget_modified()
        self._dirty_mark = self._pool.modified_pages()
        committed: Csn = NO_CSN
        epoch = lease.epoch
        try:
            if trace is not None:
                trace.phase("build_records")
            with self._close_wait_hazard():
                records, images, materialized_csn = self._build_records(
                    reservation, epoch
                )
            with self._close_wait_hazard():
                planned_csn = self._wal.planned_terminal_lsn(records)
            raw_batch_rolls = planned_csn != materialized_csn
            if planned_csn != materialized_csn:
                with self._close_wait_hazard():
                    records, images = self._retarget_commit_batch(
                        reservation,
                        records,
                        images,
                        (),
                        old_csn=materialized_csn,
                        new_csn=planned_csn,
                        epoch=epoch,
                    )
                if trace is not None:
                    trace.increment(COMMIT_RETARGETS_TOTAL)
            self._materialized = None
            if not raw_batch_rolls:
                records = self._compress_page_records(records, images)
            self._validate_wal_batch_budget(reservation, records)
            self._validate_lease(lease)
            if trace is not None:
                trace.phase("append")
            wal_bytes_before = (
                _trusted_wal_bytes(self._wal) if trace is not None else None
            )
            with self._close_wait_hazard():
                committed = self._wal.append_many(
                    records,
                    expected_terminal_lsn=planned_csn,
                )
            if trace is not None:
                wal_bytes_after = _trusted_wal_bytes(self._wal)
                trace.capture_batch(
                    images,
                    None
                    if wal_bytes_before is None or wal_bytes_after is None
                    else wal_bytes_after - wal_bytes_before,
                )
            _require_forward_commit(committed, previous.last_committed_lsn)
            if trace is not None:
                trace.phase("barrier")
            with self._close_wait_hazard():
                self._wal.barrier()
            reservation.bind_epoch(epoch)
            reservation.mark_committed(committed)
        except BaseException:
            if committed > NO_CSN or _wal_is_damaged(self._wal):
                self._recovery_required = True
            raise

        self._published_high_water = _larger(self._published_high_water, committed)
        local_prefix_candidate = (
            self._local_applied_prefix
            if type(self._index_manager) is IndexManager
            else None
        )
        try:
            if trace is not None:
                trace.phase("apply")
            with self._close_wait_hazard():
                self._apply_images(images)
            if trace is not None:
                trace.phase("publish")
            with self._close_wait_hazard():
                self._publish_commit_state(previous, committed)
            if (
                local_prefix_candidate is not None
                and not self._recovery_required
                and not self._index_authority_sync_required
                and not self._pool.has_dirty_pages()
                and local_prefix_candidate.checkpoint_lsn
                == previous.checkpoint_lsn
                and local_prefix_candidate.applied_through_lsn
                == previous.last_committed_lsn
            ):
                # The CN-1 floor reservation is itself a complete local heap-only commit: its
                # WAL is durable, its one page is applied and flushed, and its state is now
                # published under the same COMMIT_SECTION as the caller's row commit. Preserve
                # that exact prefix so large INSERT batches do not make the checkpoint shortcut
                # permanently unreachable merely because they crossed an identity lease.
                self._local_applied_prefix = _LocalAppliedPrefix(
                    checkpoint_lsn=local_prefix_candidate.checkpoint_lsn,
                    applied_through_lsn=committed,
                )
        except BaseException as failure:
            if trace is not None:
                trace.phase("other")
            if isinstance(failure, GrafxError):
                try:
                    self._redo_onto_device(previous.last_committed_lsn, committed)
                    self._publish_commit_state(previous, committed)
                    recovered = True
                except BaseException as recovery_failure:
                    _note_cleanup_failure(failure, recovery_failure)
                    recovered = False
            else:
                recovered = False
            if not recovered:
                self._recovery_required = True
            if not isinstance(failure, Exception):
                failure.add_note(
                    f"Identity-floor metadata commit {committed} is durable, but user "
                    f"transaction {user_txn_id} did not run."
                )
                raise
            refusal = GrafxTransactionStateError(
                "The identity-floor reservation became durable, but the user transaction did "
                "not commit. Its reserved range was burned; roll back and restage the user "
                "transaction after any required recovery.",
                operation="reserve row identities",
                metadata_committed=True,
                user_transaction_committed=False,
                reservation_csn=committed,
                recovery_required=self._recovery_required,
                txn_id=user_txn_id,
            )
            refusal.details["retryable"] = False
            raise refusal from failure

        return (
            CommitState(
                last_committed_lsn=committed,
                last_csn=committed,
                checkpoint_lsn=previous.checkpoint_lsn,
                format_version=previous.format_version,
            ),
            committed,
        )

    def _raise_explicit_below_identity_floor(
        self,
        txn: TransactionContext,
        table: object,
        table_id: int,
        identities: tuple[int, ...],
        floor: int,
    ) -> None:
        """Refuse a named id that may belong to any participant's open durable range."""
        raise GrafxTransactionStateError(
            "An explicit row identity below the durable identity floor may belong to another "
            "process's reserved but not-yet-used range.",
            field="record_id",
            value=min(identities),
            explicit_ids=identities,
            durable_floor=floor,
            table=getattr(table, "name", None),
            table_id=table_id,
            txn_id=txn.txn_id,
        )

    def _sync_identity_process(self) -> None:
        """Prove this manager still belongs to the process that created its coordination state."""
        self._require_process_owner("use cached row identities")

    def _require_process_owner(self, operation: str) -> None:
        """Fail closed after fork before inherited locks, pins, leases or caches are touched."""
        current = self._process_identity_provider()
        if not self._identity_process_invalid and current == self._identity_process:
            return
        self._identity_leases.clear()
        self._identity_process_invalid = True
        raise GrafxTransactionStateError(
            "This transaction manager was inherited across a process boundary. Its reader "
            "registration, writer coordination and identity ranges belong to the creating "
            "process; open a new database connection in this process.",
            operation=operation,
            field="process_identity",
            creating_process=repr(self._identity_process),
            current_process=repr(current),
            inherited_process=True,
        )

    def _reserved_record_ids(
        self, txn: TransactionContext, intents: Sequence[RowIntent]
    ) -> dict[int, tuple[object, frozenset[int]]]:
        """Return the identities this batch named for itself, per table, with their table.

        Three refusals live here rather than at the heap. Two inserts of one table under one
        identity is the failure this exists to stop, and by the time the second one reaches the
        heap the first is already on a page. The DOMAIN is checked for the same reason: the
        staging door validates what it is handed, but ``row_intents`` is a public mutable list, so
        a caller can put a boolean or a negative number in an intent after staging it -- and a
        negative one is what a private token looks like.
        """
        chosen: dict[int, tuple[object, set[int]]] = {}
        for position, intent in enumerate(intents):
            if intent.operation is not RowOperation.INSERT or intent.record_id is None:
                continue
            table_id = getattr(intent.table, "table_id", None)
            identity = intent.record_id
            if (
                isinstance(identity, bool)
                or not isinstance(identity, int)
                or identity < FIRST_RECORD_ID
                or identity >= MAX_U64
            ):
                raise GrafxTransactionStateError(
                    "A staged row identity must be an integer inside the durable identity "
                    f"domain, from {FIRST_RECORD_ID} up to but not including {MAX_U64}.",
                    field="record_id",
                    position=position,
                    value=repr(identity),
                    table=getattr(intent.table, "name", None),
                    table_id=table_id,
                    txn_id=txn.txn_id,
                )
            _table, taken = chosen.setdefault(table_id, (intent.table, set()))
            if identity in taken:
                raise GrafxTransactionStateError(
                    "Two inserts of one table cannot name the same row identity.",
                    field="record_id",
                    position=position,
                    value=identity,
                    table=getattr(intent.table, "name", None),
                    table_id=table_id,
                    txn_id=txn.txn_id,
                )
            taken.add(identity)
        return {
            table_id: (table, frozenset(taken))
            for table_id, (table, taken) in chosen.items()
        }

    def _refuse_reused_identities(
        self,
        txn: TransactionContext,
        reserved: dict[int, tuple[object, frozenset[int]]],
    ) -> None:
        """Refuse an identity a row of that table already carries, gap or no gap.

        A counter that has moved past an identity nobody used leaves a GAP, and a gap costs
        nothing -- no reader can observe it. An identity a row already carries is a different
        thing entirely: ``observe_record_id`` only ever raises the counter, so it accepts a number
        BELOW it without a word, and the heap would then hold two rows under one name. That is the
        one half of "gaps are fine, reuse is never" that nothing else was checking.

        The walk covers ended versions too. An identity whose row was deleted was still handed
        out, and a snapshot opened before the delete is still entitled to read it.
        """
        if not reserved:
            return
        heap = self._heap
        for table_id, (table, identities) in reserved.items():
            for _ref, version in heap.scan_all(table):
                if version.record_id not in identities:
                    continue
                raise GrafxTransactionStateError(
                    "A staged row identity is already carried by a row of that table; an unused "
                    "gap may be taken, an identity in use may not.",
                    field="record_id",
                    value=version.record_id,
                    table=getattr(table, "name", None),
                    table_id=table_id,
                    txn_id=txn.txn_id,
                )

    def _require_unexhausted_identity(
        self, txn: TransactionContext, table: object, table_id: object, identity: int
    ) -> None:
        """Refuse an identity at or above the exhausted marker, one step before the heap would.

        The same boundary ``allocate_record_id`` draws: the marker is not a usable id, because a
        counter with nowhere to move to is a counter that hands one identity out twice.
        """
        if identity < MAX_U64:
            return
        raise GrafxUnsupportedOperation(
            f"Table {getattr(table, 'name', None)!r} has no row identity left below the "
            f"exhausted marker {MAX_U64}.",
            table=getattr(table, "name", None),
            table_id=table_id,
            field="next_record_id",
            value=identity,
            txn_id=txn.txn_id,
        )

    def _refuse_unresolved_intents(
        self, txn: TransactionContext, intents: Sequence[RowIntent]
    ) -> None:
        """Refuse anything still carrying a private identity, and re-encode what will be stored.

        A typed refusal rather than an ``assert``: this is the boundary that keeps a transient
        token out of the heap, the indexes and the WAL, and a check that disappears under ``-O``
        is not a boundary. Re-encoding is the second half of the same guarantee -- the values that
        will be written are checked against the schema HERE, before the first of them lands, so a
        row whose resolved endpoint does not fit its column refuses while nothing has moved.
        """
        for position, intent in enumerate(intents):
            if intent.operation is RowOperation.DELETE:
                continue
            for slot, value in enumerate(intent.values):
                if isinstance(value, PendingRowRef):
                    raise GrafxTransactionStateError(
                        "A row reached the heap boundary still naming a pending identity; every "
                        "endpoint must be resolved to a stored row before anything is written.",
                        field="values",
                        position=position,
                        slot=slot,
                        value=repr(value),
                        table=getattr(intent.table, "name", None),
                        txn_id=txn.txn_id,
                    )
            if intent.operation is RowOperation.INSERT and intent.record_id is None:
                raise GrafxTransactionStateError(
                    "An insert reached the heap boundary with no planned identity.",
                    field="record_id",
                    position=position,
                    table=getattr(intent.table, "name", None),
                    txn_id=txn.txn_id,
                )
            self._refuse_unstored_endpoints(txn, intent, position)
            encode_tuple(intent.table, intent.values)

    def _refuse_unstored_endpoints(
        self, txn: TransactionContext, intent: RowIntent, position: int
    ) -> None:
        """Refuse a relationship whose endpoints are not both identities a row can carry.

        Stated rather than inherited. The column type would catch a string and the heap would
        catch a value outside the INT64 endpoint domain, but neither says that ZERO and negative
        numbers are the interesting cases here: a private token IS a negative integer, so "looks
        like an int" is exactly the check that would let one through.
        """
        if getattr(intent.table, "kind", None) != "rel":
            return
        for slot in range(min(ENDPOINT_COLUMN_COUNT, len(intent.values))):
            endpoint = intent.values[slot]
            if (
                isinstance(endpoint, bool)
                or not isinstance(endpoint, int)
                or endpoint < FIRST_RECORD_ID
            ):
                raise GrafxTransactionStateError(
                    "A relationship endpoint must be a positive stored row identity by the time "
                    "the row is written.",
                    field="endpoint",
                    position=position,
                    slot=slot,
                    value=repr(endpoint),
                    table=getattr(intent.table, "name", None),
                    txn_id=txn.txn_id,
                )

    def _unstage_index_changes(
        self, txn: TransactionContext, mark: int
    ) -> BaseException | None:
        """Undo what :meth:`_stage_index_changes` staged for an attempt that did not commit.

        Staging appends to ``txn.pending_records`` and to every index's own staging area. An
        attempt refused AFTER staging -- the log refusing the batch, a lease stolen between
        validate and append -- must leave neither behind: the next ``commit(txn)`` would stage
        again and carry BOTH sets, the first stamped with a number the log never assigned, and
        apply live index entries for versions that were abandoned (C5 round-2 B3).
        """
        del txn.pending_records[mark:]
        failure: BaseException | None = None
        try:
            txn._reconcile_staged_payload_bytes()
        except BaseException as budget_failure:
            failure = budget_failure
        try:
            self._drop_index_changes(txn)
        except BaseException as index_failure:
            failure = _first_failure(failure, index_failure)
        return failure

    def _abandon_rows(
        self,
        rows: Sequence[_RowWrite],
    ) -> BaseException | None:
        """Make rows written by a commit that then failed unreachable to every snapshot.

        The rows are in the buffer pool by the time the log is asked for anything, and the pool
        is shared: the next commit that succeeds flushes that file, and it would carry these
        rows to the device with it. The reserved provisional stamp is outside the legal snapshot
        domain, so even total cleanup failure leaves a birth invisible and an ending ineffective.

        Cleanup still normalises births to NO_CSN and endings to NO_CSN when possible, reducing
        residue for verifier/maintenance. It is best-effort rather than a correctness dependency:
        the sentinel already converts an impossible cleanup into leaked space, never a result.

        This must not raise. It runs while a failure is already unwinding, and replacing that
        failure with one about cleaning up after it would hide the reason the commit is being
        abandoned at all.
        """
        failure: BaseException | None = None
        for item in rows:
            try:
                if item.born is not None:
                    # A version nobody committed carries no commit number, and the visibility
                    # predicate refuses one outright.
                    self._restamp(item.born, xmin=NO_CSN)
                if item.ended is not None:
                    # The version this commit was going to end is live again: it was only ever
                    # ended on behalf of a commit that did not happen.
                    self._restamp(item.ended, xmax=NO_CSN, flags=_LIVE_FLAGS)
            except BaseException as cleanup_failure:
                failure = _first_failure(failure, cleanup_failure)
        # The restamp keeps any live holder of these pages coherent; what happens to the FRAMES
        # is decided below, and it is decided for the whole attempt at once.
        touched = {
            (self._heap_file, page_index) for page_index in self._pages_touched_by(rows)
        }
        # The measured set, and the reason the enumeration above stopped being enough on its own.
        # The attempt may have relinked a page no row of it ever landed on, and it may have
        # dirtied pages before it raised and produced no rows at all -- in which case the
        # enumeration names nothing while the pool still holds the attempt's writes.
        try:
            touched.update(self._attempt_pages())
        except BaseException as cleanup_failure:
            failure = _first_failure(failure, cleanup_failure)
        return _first_failure(failure, self._undo_pages(touched))

    def _undo_pages(self, touched: set[tuple[str, PageIndex]]) -> BaseException | None:
        """Take back the pages of a commit that did not happen -- all of them the same way.

        TWO WAYS TO TAKE A PAGE BACK, and which one is available is not a choice.

        DISCARD, for a page the device has never seen. The frame holds the page as it looked
        during the refused attempt, and written back -- by the next flush, or by a read view
        dropping frames -- it would land over pages another participant has since committed (C5
        round-2 B2: rows lost, verify clean). Dropped unwritten, the device is the truth the next
        pin re-reads, and the attempt leaves nothing anywhere.

        WRITE BACK, for a page the device has ALREADY seen. Discarding one of those undoes
        nothing: the device carries the attempt's bytes whatever happens to the frame, and the
        frame is the only copy that carries the restamp which makes those rows invisible.
        Discarding it therefore throws away the repair and keeps the damage.

        **The two must not be mixed within one attempt, and mixing them was a defect.** A commit
        whose working set is larger than the buffer budget has some of its pages evicted -- and
        therefore written -- while it is still running. Discarding the rest then left the device
        holding a chain whose link had been written and whose target had not, which a later walk
        follows into a page nobody wrote (`corruption_detected`, on a database in which nothing
        went wrong), or left the attempt's rows readable under a stamp no commit ever assigned,
        with `verify()` reporting clean. Measured at a 512-byte page size: a refused attempt of 60
        rows leaked readable rows at every frame budget from 4 to 58, and raised
        `corruption_detected` at 59 to 62; only at 63 and above, where nothing had been evicted,
        did the old undo work. It was measured at 1024.

        So: if ANY page of the attempt reached the device, EVERY page of it is written there in
        its restamped form. The result is a chain the device can walk, whose rows carry no commit
        number and are therefore invisible to every snapshot -- space leaked, exactly like the page
        an append abandons (G6), and never a row a reader can meet. If NO page reached the device,
        every frame is dropped and the device never learns the attempt existed.

        This runs while a failure is already unwinding, so nothing here may raise: replacing that
        failure with one about cleaning up after it would hide the reason the commit is being
        abandoned at all.
        """
        failure: BaseException | None = None
        try:
            escaped = touched & self._pool.pages_written_back()
        except BaseException as cleanup_failure:
            failure = cleanup_failure
            escaped = frozenset(touched)  # cannot tell: assume the device has seen them
        for file, page_index in sorted(touched):
            try:
                if escaped:
                    self._pool.write_back(file, page_index)
                else:
                    self._pool.discard(file, page_index)
            except BaseException as cleanup_failure:
                failure = _first_failure(failure, cleanup_failure)
        return failure

    def _restamp(
        self,
        reference: RecordRef,
        *,
        xmin: Csn | None = None,
        xmax: Csn | None = None,
        flags: int | None = None,
    ) -> None:
        """Rewrite the commit numbers of one stored version, through C1's own header value.

        The record header is C1's format and is read and written as a value, never by reaching
        into the bytes. Everything not named here -- the identity, the payload, the chain -- is
        carried through exactly as it was.
        """
        with self._pool.pinned(self._heap_file, reference.page) as page:
            self._restamp_page(
                page,
                reference,
                xmin=xmin,
                xmax=xmax,
                flags=flags,
            )

    def _restamp_page(
        self,
        page: Page,
        reference: RecordRef,
        *,
        xmin: Csn | None = None,
        xmax: Csn | None = None,
        flags: int | None = None,
    ) -> None:
        """Rewrite one version header in the supplied page value."""
        payload = page.read_slot(reference.slot)
        header = RecordHeader.decode(payload[:RECORD_HEADER_SIZE])
        corrected = RecordHeader(
            record_id=header.record_id,
            xmin=header.xmin if xmin is None else xmin,
            xmax=header.xmax if xmax is None else xmax,
            prev_version=header.prev_version,
            payload_len=header.payload_len,
            schema_version=header.schema_version,
            flags=header.flags if flags is None else header.flags & flags,
            reserved=header.reserved,
        )
        if corrected == header:
            return
        page.update_slot(
            reference.slot, corrected.encode() + payload[RECORD_HEADER_SIZE:]
        )

    def _attempt_pages(self) -> tuple[tuple[str, PageIndex], ...]:
        """Return every page THIS commit attempt has modified, in a fixed order.

        THE DEFECT THIS EXISTS FOR. Three things need to know which pages a commit changed --
        the log, so a redo reproduces them; the interest set, so two commits that write one page
        conflict; and the abandonment, so a refused attempt leaves nothing behind. All three
        asked :meth:`_pages_touched_by`, which re-derives the answer from where the ROWS landed,
        and a heap append changes a page no row lands on: the previous last page, whose
        ``next_page`` is what makes the new page reachable. That link was therefore never logged,
        never declared, and -- the part that corrupted databases -- never undone. A refused
        attempt left the pool holding a tail page pointing at the page it had just abandoned, and
        the next commit of any participant flushed that link to the device. A later walk followed
        it into a page nobody ever wrote and refused with corruption_detected, on a database in
        which nothing had gone wrong. Three processes appending to one table reproduced it inside
        a minute; one process never could, because one process never abandons and re-appends
        against a tail another attempt has already moved.

        So the answer is no longer re-derived. It is MEASURED, against the mark taken before the
        attempt wrote anything: whatever the pool holds dirty that it did not hold dirty then is
        what this attempt changed. An enumeration has to name every site that touches a page and
        is silently short by one the day a site is added; a measurement cannot be short, because
        it asks the pool what happened rather than asking the code what it intended.

        Pages that were already dirty are excluded rather than swept in. They belong to no
        attempt this commit can undo, and discarding one on abandonment would throw away a change
        this commit never made.
        """
        return tuple(sorted(self._pool.modified_pages() - self._dirty_mark))

    def _pages_touched_by(self, rows: Sequence[_RowWrite]) -> tuple[PageIndex, ...]:
        """Return every heap page a row write can have changed, in a fixed order.

        Only pages that actually carry a born or ended version are derived here.  Structural
        writes -- a first extent, tail growth, hint repair, and any real page-zero mutation -- are
        measured by :meth:`_attempt_pages`, so omitting an unchanged header removes false sharing
        without hiding a byte that must reach WAL or abandonment.
        """
        if not rows:
            return ()
        touched: set[PageIndex] = set()
        for item in rows:
            if item.born is not None:
                touched.add(item.born.page)
            if item.ended is not None:
                # The page of the version being ENDED changes too: its header now says when it
                # stopped being current. A commit that logged the new version and not the end of
                # the old one would replay into two live versions of one record.
                touched.add(item.ended.page)
        return tuple(sorted(touched))

    def _group_page_stamps(
        self, rows: Sequence[_RowWrite]
    ) -> dict[PageIndex, tuple[_HeapVersionStamp, ...]]:
        """Classify each materialized row once into its page-local header effects.

        A commit may stage many physical pages for a row batch.  Walking the complete batch for
        every page makes stamping O(P*R), even though one page can use only references that name
        that page.  This transient index preserves the original row order and the original
        birth-before-ending order within an update; each page then pays only for its own effects.
        It is neither persisted nor shared, and a retarget reuses the exact plan retained by the
        materialized attempt.
        """
        grouped: dict[PageIndex, list[_HeapVersionStamp]] = {}
        for item in rows:
            if item.born is not None:
                grouped.setdefault(item.born.page, []).append(
                    _HeapVersionStamp(reference=item.born, is_birth=True)
                )
            if item.ended is not None:
                grouped.setdefault(item.ended.page, []).append(
                    _HeapVersionStamp(reference=item.ended, is_birth=False)
                )
        return {page_index: tuple(stamps) for page_index, stamps in grouped.items()}

    def _stamp_page(
        self,
        page: Page,
        file: str,
        page_index: PageIndex,
        csn: Csn,
        stamps: Sequence[_HeapVersionStamp],
    ) -> None:
        """Stamp the commit number into a page VALUE: page_lsn and the heap headers this commit owns.

        Heap work has to be materialised before the page half of optimistic validation is known,
        but a WAL append can still fail after that.  The resident/device version therefore keeps
        the reserved provisional sentinel until the WAL barrier returns.  Only a local value is
        rewritten to the predicted commit number; its encoding is the byte-identical image
        appended to WAL and installed by :meth:`_apply_images` after the barrier.
        """
        if page.page_lsn < csn:
            page.page_lsn = csn
        if file == self._heap_file:
            for stamp in stamps:
                if stamp.is_birth:
                    self._restamp_page(page, stamp.reference, xmin=csn)
                else:
                    self._restamp_page(page, stamp.reference, xmax=csn)

    def _committed_image(
        self,
        file: str,
        page_index: PageIndex,
        image: bytes,
        csn: Csn,
        stamps: Sequence[_HeapVersionStamp],
    ) -> bytes:
        """Return the post-commit image of BYTES a collaborator staged, after verifying them.

        A pre-staged image is the authority for its page and came from outside this frame
        pool, so it is decoded with verification, structurally and by checksum, before the
        commit number is stamped into it. The decoded value is kept for a retarget.
        """
        page = self._pool.codec.decode_page(image, verify=True)
        self._stamp_page(page, file, page_index, csn, stamps)
        self._remember_materialized(file, page_index, page)
        return self._pool.codec.encode_page(page)

    def _local_image(
        self,
        file: str,
        page_index: PageIndex,
        csn: Csn,
        stamps: Sequence[_HeapVersionStamp],
    ) -> bytes:
        """Return the post-commit image of a page THIS process materialised, encoded once.

        The resident frame was verified when it entered the pool and is the authority for
        its page, so the image is built from an independent copy of it -- stamp the copy,
        encode the copy -- instead of encoding the frame, decoding and verifying the bytes
        this process just produced, and encoding them again. The frame is never touched:
        it stays provisional until the WAL barrier returns. The copy is kept for a retarget.
        """
        with self._pool.pinned(file, page_index) as resident:
            page = resident.copy()
        self._stamp_page(page, file, page_index, csn, stamps)
        self._remember_materialized(file, page_index, page)
        return self._pool.codec.encode_page(page)

    def _remember_materialized(
        self, file: str, page_index: PageIndex, page: Page
    ) -> None:
        """Keep a stamped page value for the retarget of the attempt that produced it."""
        if self._materialized is not None:
            self._materialized.pages[(file, page_index)] = page

    def _apply_images(
        self,
        images: Sequence[tuple[str, PageIndex, bytes]],
    ) -> None:
        """Apply the staged pages under the redo rule of step 6, then put them on the device.

        The apply door is C1's, and deliberately: the rule -- grow the file if the page is
        missing, apply when the resident page is free or older, leave it alone when it is not --
        is the same rule recovery replays with (amendment A22), and writing it twice would give
        this component and recovery two chances to disagree about one invariant.

        The flush is not optional and it is not a performance choice. A page applied into this
        process's buffer pool is invisible to every OTHER process until it reaches the device,
        and the commit is about to publish a state that tells those processes the page is there.
        Publishing a number whose pages only exist in one process's memory is exactly the
        partial commit AC-3 forbids. It is a flush and NOT a barrier: section 8.5 step 6 says
        the data files are not fsynced here, because the log is the authority on durability and
        the redo is idempotent.

        The images arrive already stamped by :meth:`_build_records` and are installed exactly as
        the log carries them, byte for byte. Re-stamping them here with the number the log
        actually returned would leave the page and the record disagreeing about the page, and a
        later replay would then produce a page that is not the one this commit wrote.
        """
        trace = self._active_commit_trace
        touched: set[str] = set()
        for file, page_index, image in images:
            apply_page_image(self._pool, file, page_index, image)
            touched.add(file)
        if trace is not None:
            trace.phase("flush")
        for file in sorted(touched):
            self._pool.flush(file)

    def _publish_commit_state(
        self,
        previous: CommitState,
        committed: Csn,
        *,
        catalog_touched: bool = False,
    ) -> None:
        """Publish the new commit state, preserving the checkpoint another component set.

        The checkpoint LSN is read and written back rather than replaced. It belongs to whoever
        checkpoints, and a commit that reset it to zero would tell recovery to replay the whole
        log and tell the recycler that nothing may ever be released.

        The number published is the commit number itself, with no comparison against what is
        already there. That used to be a three-way maximum, and the maximum is now provably
        dead: ``_require_forward_commit`` has refused this commit before the barrier unless its
        number is ABOVE ``previous``, and nothing else can publish while the commit section is
        held. A battery confirmed it -- with the guard in place, replacing the maximum with the
        commit number could not be made to fail. A second mechanism that no input can distinguish
        from the first is not defence in depth, it is a guarantee nobody can prove (A67, A83),
        so it is gone and the guard is the one answer.

        ``previous`` is the durable state read by :meth:`_complete_committed_gap` under this same
        COMMIT_SECTION. Reusing it avoids a second control-record read and is safe because every
        publisher of ``commit.state`` is serialised by that section.
        """
        state = CommitState(
            last_committed_lsn=committed,
            last_csn=committed,
            checkpoint_lsn=previous.checkpoint_lsn,
            format_version=self._commit_state_format_for_catalog(
                previous, inspect_durable_catalog=catalog_touched
            ),
        )
        self._publish(state, previous=previous)
        # Remembered only here, and only after the publish landed: this is the one door that
        # publishes a commit THIS manager produced (both the live step 3.7 and its post-barrier
        # redo republication call it). The gap completion and the checkpoint publish through
        # _publish directly and stay foreign to the read-view exemption.
        self._own_published_lsn = committed

    def _commit_state_format_for_catalog(
        self, previous: CommitState, *, inspect_durable_catalog: bool
    ) -> int:
        """Return the monotonic mixed-fleet fence required by durable catalog authority.

        A commit that did not touch the catalog preserves the already-published monotonic fence
        without parsing an O(catalog) payload. A catalog commit and foreign-gap completion call
        this only after page images have been applied and flushed, and read the pages
        non-destructively. They therefore cannot confuse an unsaved LIVE catalog copy with
        durable authority.

        Direct unit compositions historically pass small catalog doubles. A double with no
        semantic ``format_version`` cannot request a promotion, so the established version is
        preserved. Production always wires ``CatalogStore``.
        """

        if not inspect_durable_catalog:
            return previous.format_version
        reader = getattr(self._catalog, "read_from_pages", None)
        if not callable(reader):
            return previous.format_version
        observed = getattr(reader(), "format_version", None)
        if observed is None:
            return previous.format_version
        if observed == CATALOG_FORMAT_VERSION:
            return COMMIT_STATE_FORMAT_VERSION
        if observed != CATALOG_LEGACY_FORMAT_VERSION:
            raise GrafxCorruptionDetected(
                f"The runtime catalog reports unsupported format version {observed!r}.",
                field="format_version",
                value=observed,
                supported=CATALOG_FORMAT_VERSION,
            )
        if previous.format_version == COMMIT_STATE_FORMAT_VERSION:
            raise GrafxCorruptionDetected(
                "The published commit-state fence is version 2 but durable catalog authority "
                "is version 1; a writer cannot downgrade either side.",
                field="format_version",
                commit_state_format=previous.format_version,
                catalog_format=observed,
            )
        return previous.format_version

    def _publish(self, state: CommitState, *, previous: CommitState) -> None:
        """Write the state to a temporary of this participant and replace the published file.

        Every generic publication forfeits the own-view provenance FIRST: a gap completion or
        a checkpoint can carry a foreign commit, and a number published here must never be met
        by a later read view as "my own". _publish_commit_state re-marks it, after ITS publish
        returned, for the one number this manager provably produced itself.
        """
        self._own_published_lsn = None
        self._local_applied_prefix = None
        with self._close_wait_hazard():
            self._commit_state_store.publish(state, previous=previous)

    def _read_commit_state(self) -> CommitState:
        """Read the published state, riding out a device condition that is worth trying again.

        The read takes two device calls, the size and the bytes, and another participant may
        publish between them. That is a benign race and NOT a reason to retry: every published
        record is the same fixed size and arrives whole through ``atomic_replace``, so a stale
        size larger than the file yields the complete record anyway and decodes. A branch that
        re-read on a length mismatch was here and was removed rather than kept: the full matrix
        of (guard, no guard) x (stale size, damaged bytes) gives the same answer in all four
        cells, so nothing could ever have shown it working (A67, A93).

        What IS worth trying again is a transient device condition, and the decision reads
        ``details["retryable"]`` rather than the class of the failure, because A28 put the
        classification in the details and A47 makes every retry predicate read it there.

        There is no sleep between attempts: this layer owns no clock it may wait on (G2), and
        the condition being ridden out is a sharing violation of a few milliseconds that the
        device below has already backed off for.
        """
        with self._close_wait_hazard():
            return self._commit_state_store.read()

    # --- internals ---------------------------------------------------------------------------

    def _hold_lease(self) -> LeaseGuard:
        """Return the writer lease this commit will validate under, acquiring one if needed.

        A retained lease is renewed rather than re-acquired, on the schedule C3 sets from the
        time to live: two renewals may fail before any other participant would call this owner
        stalled. A renewal that reports the lease was taken over is not an error to propagate --
        the successor is legitimate and this participant simply needs a current epoch -- so the
        guard is dropped and a fresh lease acquired, which is exactly what the per-commit
        placement did every time.
        """
        if not self._retain_lease:
            with self._close_wait_hazard():
                return LeaseGuard.acquire(
                    self._coordinator, timeout=self._lease_timeout
                )
        guard = self._lease_guard
        if guard is not None and not guard.released:
            try:
                with self._close_wait_hazard():
                    guard.renew_if_due(self._clock.monotonic())
                return guard
            except GrafxLeaseStolen:
                self._lease_guard = None
        with self._close_wait_hazard():
            guard = LeaseGuard.acquire(self._coordinator, timeout=self._lease_timeout)
        self._lease_guard = guard
        return guard

    def _drop_lease(
        self,
        lease: LeaseGuard,
        *,
        force: bool = False,
    ) -> BaseException | None:
        """Give the lease up and return, rather than raise, a foreign cleanup failure.

        ``force`` is reserved for the A/B checkpoint boundary. A retained lease there would
        keep every other process out for the whole data barrier and defeat the concurrency the
        split exists to provide, so the cached guard is detached before release is attempted.
        """
        trace = self._active_commit_trace
        if (
            not force
            and self._retain_lease
            and lease is self._lease_guard
            and not lease.released
        ):
            if trace is not None:
                # This is the commit's occupancy of a retained lease. The intentional idle
                # retention between operations is outside a write-commit attempt and therefore
                # outside D-26's per-commit denominator.
                trace.release_window("writer_lease")
            return None
        if force and lease is self._lease_guard:
            self._lease_guard = None
        try:
            with self._close_wait_hazard():
                failure = _release_quietly(lease)
            if failure is not None and trace is not None:
                trace.suppress_delivery()
            return failure
        except BaseException:
            if trace is not None:
                trace.suppress_delivery()
            raise
        finally:
            if trace is not None:
                trace.release_window("writer_lease")

    def _validate_lease(self, lease: LeaseGuard) -> None:
        """Validate one coordinator-owned lease under the narrow close-wait hazard."""
        with self._close_wait_hazard():
            lease.validate()

    def _monotonic(self) -> float:
        """Read the host clock without letting a callback make close wait on this section."""
        with self._close_wait_hazard():
            return self._clock.monotonic()

    def _reader_horizon(self) -> Lsn | None:
        """Read the coordinator horizon under the narrow close-wait hazard."""
        with self._close_wait_hazard():
            return self._coordinator.reader_horizon()

    def _close_reader_quietly(
        self, registration: ReaderRegistration
    ) -> BaseException | None:
        """Withdraw a coordinator reader without turning its callback into a close deadlock."""
        with self._close_wait_hazard():
            return _close_quietly(registration)

    @contextmanager
    def _close_wait_hazard(self) -> Iterator[None]:
        """Consume the standard adapter's host-free narrow callback capability."""
        capability = getattr(self._metrics, "close_wait_hazard", None)
        boundary = capability() if callable(capability) else nullcontext()
        with boundary:
            yield

    @contextmanager
    def _coordinator_section(
        self,
        name: str,
        *,
        timeout: float,
    ) -> Iterator[object]:
        """Enter each foreign context phase under a hazard, but never mark its body.

        A coordinator owns the factory and both context-protocol callbacks. Any of those may
        synchronously ask another thread to close the database while this participant section
        is held. Keeping the marker around the yielded engine body would instead suppress
        normal close/quiescence during commit and schema settlement, so the phases are expanded
        explicitly here.
        """
        trace = self._active_commit_trace
        measured = trace is not None and name == COMMIT_SECTION
        if measured:
            trace.start_window("commit_section")
        try:
            with self._close_wait_hazard():
                section = self._coordinator.exclusive(name, timeout=timeout)
            with self._close_wait_hazard():
                entered = section.__enter__()
        except BaseException as failure:
            if measured:
                trace.fail_window("commit_section")
                if not isinstance(failure, GrafxLeaseTimeout):
                    trace.suppress_delivery()
            raise
        if measured:
            trace.acquire_window("commit_section")
        try:
            yield entered
        except BaseException as failure:
            if measured:
                trace.phase("other")
            try:
                with self._close_wait_hazard():
                    suppressed = bool(
                        section.__exit__(type(failure), failure, failure.__traceback__)
                    )
            except BaseException:
                if measured:
                    trace.suppress_delivery()
                raise
            finally:
                if measured:
                    trace.release_window("commit_section")
            if not suppressed:
                raise
        else:
            if measured:
                trace.phase("other")
            try:
                with self._close_wait_hazard():
                    section.__exit__(None, None, None)
            except BaseException:
                if measured:
                    trace.suppress_delivery()
                raise
            finally:
                if measured:
                    trace.release_window("commit_section")

    @contextmanager
    def _hold_wal_tail(self) -> Iterator[None]:
        """Reuse one WAL-tail picture when the concrete WAL offers the CQ-1 capability."""
        hold = getattr(self._wal, "hold_tail", None)
        if not callable(hold):
            # TransactionManager deliberately accepts narrow collaborator doubles and adapters.
            # They remain compatible; the concrete WalManager takes the optimized path.
            yield
            return
        with hold():
            yield

    @contextmanager
    def _participant_descriptor_scope(self) -> Iterator[None]:
        """Reuse only an unlocked participant-section descriptor for one bounded batch.

        The concrete local coordinator offers this optional optimization. Narrow collaborator
        doubles and alternate coordinators keep their existing behavior. As with section entry,
        only foreign context-protocol callbacks are close hazards; the yielded body deliberately
        is not, so lazy input and mapping callbacks may close without deadlocking.
        """
        with self._close_wait_hazard():
            capability = getattr(
                self._coordinator, "reuse_unlocked_section_descriptor", None
            )
            scope = (
                capability(self._participant_section_name)
                if callable(capability)
                else None
            )
        if scope is None:
            yield
            return

        with self._close_wait_hazard():
            entered = scope.__enter__()
        try:
            yield entered
        except BaseException as failure:
            try:
                with self._close_wait_hazard():
                    scope.__exit__(type(failure), failure, failure.__traceback__)
            except BaseException as cleanup_failure:
                _note_cleanup_failure(failure, cleanup_failure)
            # This private optimization never owns an operation's outcome. A custom scope may
            # release resources here, but its truthy return cannot suppress the failure whose
            # unwind brought it here.
            raise
        else:
            with self._close_wait_hazard():
                scope.__exit__(None, None, None)

    def _retain_transaction_descriptor_scope_in_section(
        self, txn: TransactionContext
    ) -> None:
        """Install one identity-revalidated unlocked descriptor scope for a live transaction.

        The caller already owns the participant section, which makes the first installation
        single-flight without adding an engine lock.  Discovery deliberately uses the concrete
        class dictionary: inheriting from the local coordinator is not enough to inherit this
        long-lived, callback-free capability.  A custom subclass must explicitly implement the
        private method again and accept the same no-authority/no-failure contract.

        Installation happens after the current section was acquired.  Consequently the first
        operation remains canonical, the second can park its freshly opened descriptor, and only
        the third and later operations reuse it.  Retrofitting the already-locked current handle
        would save one fixed open at the cost of broadening the borrow protocol.
        """
        txn_id = txn.txn_id
        if txn_id in self._transaction_descriptor_scopes:
            return
        if self._open.get(txn_id) is not txn or not txn.active:
            return
        capability = type(self._coordinator).__dict__.get(
            "_reuse_revalidated_unlocked_section_descriptor"
        )
        if not callable(capability):
            return
        with self._close_wait_hazard():
            scope = capability(self._coordinator, self._participant_section_name)
            if scope is None:
                return
            scope.__enter__()
        if self._closed or self._open.get(txn_id) is not txn or not txn.active:
            # A hostile explicit opt-in may request close from its enter callback.  It receives
            # no chance to strand the just-entered scope or resurrect the terminal transaction.
            with self._close_wait_hazard():
                scope.__exit__(None, None, None)
            return
        self._transaction_descriptor_scopes[txn_id] = scope

    def _drain_transaction_descriptor_scope(
        self, txn_id: TxnId
    ) -> BaseException | None:
        """Pop and close one transaction's unlocked descriptor scope, fail-completely.

        Pop precedes foreign protocol exit so a re-entrant or failing explicit custom opt-in
        cannot make a terminal transaction look live in this map.  The concrete local scope
        closes descriptors quietly.  Retaining a returned failure is defence in depth for direct
        compositions that deliberately re-declare the private capability.
        """
        scope = self._transaction_descriptor_scopes.pop(txn_id, None)
        if scope is None:
            return None
        try:
            with self._close_wait_hazard():
                scope.__exit__(None, None, None)
        except BaseException as failure:
            return failure
        return None

    @contextmanager
    def _participant_section(self) -> Iterator[None]:
        """Enter the section that serialises the THREADS of this participant.

        FR-3 asks for N processes AND N threads holding write transactions, and for every commit
        whose partition sets are disjoint to CONFIRM. The processes half falls out of the lease
        being per participant; the threads half does not, and cannot: a participant holds ONE
        lease, so two threads that each acquire and release "it" end each other's epoch and the
        loser is refused with a non-retryable stale_epoch. What FR-3 requires is that they all
        confirm, not that they run at the same instant, so the threads of one participant are
        serialised here and every one of them confirms.

        The mutex is the coordinator's own section rather than a lock built here, because the
        engine owns no mechanism (G2) and may not import threading. The name carries a digest of
        this participant's identity, so the section is participant-local: another PROCESS never
        waits on it, which is what keeps the processes half of FR-3 concurrent.

        It is re-entrant from the same thread, so a door that takes it may call another that
        does. Lock order is participant -> lease -> commit on every path, and nothing anywhere
        takes them the other way round.

        Proved by test_every_thread_of_one_participant_confirms_on_disjoint_partitions,
        test_threads_opening_and_abandoning_transactions_never_report_damage and
        test_readers_and_writers_of_one_participant_share_the_pin_table_safely; that it does not
        hold another participant up is proved by the two-process tests, which still run
        concurrently with it in place.
        """
        self._require_process_owner("enter the transaction participant section")
        defer = getattr(self._metrics, "defer", None)
        deferred = defer() if callable(defer) else nullcontext()
        # Deferral surrounds lock acquisition too: coordinator wait metrics are host callbacks,
        # and a callback that asks Database.close() while this thread is trying to enter must
        # request terminal state without recursively releasing dependencies. The adapter lowers
        # its deferral depth before draining the FIFO, after the real section has been released.
        with deferred:
            with self._coordinator_section(
                self._participant_section_name,
                timeout=self._commit_lock_timeout,
            ):
                yield

    @contextmanager
    def _measured_participant_section(self, trace: _CommitTrace) -> Iterator[None]:
        """Enter the existing section and suppress delivery if its release is uncertain."""
        try:
            section = self._participant_section()
            entered = section.__enter__()
        except BaseException:
            trace.suppress_delivery()
            raise
        try:
            yield entered
        except BaseException as failure:
            try:
                suppressed = bool(
                    section.__exit__(type(failure), failure, failure.__traceback__)
                )
            except BaseException:
                trace.suppress_delivery()
                raise
            if not suppressed:
                raise
        else:
            try:
                section.__exit__(None, None, None)
            except BaseException:
                trace.suppress_delivery()
                raise

    def _require_owned(self, txn: TransactionContext) -> None:
        """Refuse a transaction this manager did not open (amendment A74).

        The lease that authorises a commit is installed by one coordinator, and C3's
        ``validate_epoch`` answers about the lease IT installed. So a transaction committed
        through a different manager would be validated against a lease that has nothing to do
        with the work it is about to write. Binding the transaction to its manager makes that
        unrepresentable rather than merely discouraged.

        The owner pointer is necessary but not sufficient: ``TransactionContext`` is public and
        a caller can construct one that names this manager and reuses a live transaction number.
        An ACTIVE context must therefore be the exact object in ``_open``.  Settled contexts are
        no longer registered because retaining them would leak one object per transaction, so
        their private manager capability distinguishes a genuine idempotent rollback from a
        caller-built lookalike.
        """
        self._require_process_owner("use a transaction context")
        if not isinstance(txn, TransactionContext):
            raise GrafxConfigurationError(
                f"A transaction must be a TransactionContext; got {type(txn).__name__}.",
                field="txn",
                value=type(txn).__name__,
            )
        if txn.owner is not self:
            raise GrafxTransactionStateError(
                "This transaction was opened by another transaction manager and cannot be "
                "committed or rolled back here.",
                txn_id=txn.txn_id,
            )
        tracked = self._open.get(txn.txn_id)
        if tracked is not None and tracked is not txn:
            raise GrafxTransactionStateError(
                "That transaction identity does not match the context this manager opened "
                "under the same transaction number.",
                txn_id=txn.txn_id,
                reason="transaction_identity_mismatch",
            )
        if txn.active and tracked is not txn:
            raise GrafxTransactionStateError(
                "This active transaction is not the exact context registered by this manager.",
                txn_id=txn.txn_id,
                reason="transaction_not_registered",
            )
        if txn._page_staging_capability is not self._page_staging_capability:
            raise GrafxTransactionStateError(
                "This settled transaction does not carry the private capability of a context "
                "opened by this manager.",
                txn_id=txn.txn_id,
                reason="transaction_capability_mismatch",
            )

    def _require_current_active(self, txn: TransactionContext) -> None:
        """Require the exact registered context to remain ACTIVE at the settlement instant.

        Callers use this only while holding the participant section.  The same checks outside
        that section are useful fail-fast diagnostics but cannot authorise lifecycle work: a
        competing thread can settle the transaction before the section is acquired.
        """
        self._require_owned(txn)
        self._require_active(txn)

    def _require_active(self, txn: TransactionContext) -> None:
        """Refuse a transaction that has already committed or rolled back."""
        if not txn.active:
            raise GrafxTransactionStateError(
                f"Transaction {txn.txn_id} is {txn.state.value} and cannot be used again.",
                txn_id=txn.txn_id,
                state=txn.state.value,
            )

    def _release_reader(self, txn: TransactionContext) -> BaseException | None:
        """Finish one transaction against the participant pin, withdrawing nothing.

        CE-2: the registration belongs to the PARTICIPANT and only :meth:`close` withdraws it.
        The finished transaction merely stops holding the floor down -- the pin advances past
        it at the next due refresh, the next checkpoint, or the next begin's refresh call
        (E-CE2-1 rewrote the frozen step 1 that used to unregister here).
        """
        del txn  # the pin is per-participant; nothing per-transaction remains to detach
        return None

    def _forget(
        self, txn: TransactionContext, mode: str
    ) -> tuple[int, BaseException | None]:
        """Drop a finished transaction and return its remaining count plus cleanup evidence.

        The count is RETURNED rather than published here so the caller can emit it once the
        participant section is released: a metrics sink is host code and must never be called
        while this component holds anything (A91).  Descriptor-scope cleanup is returned beside
        it so rollback/retry can report cleanup failure while a durable commit keeps its already
        final outcome non-retryable.
        """
        descriptor_failure = self._drain_transaction_descriptor_scope(txn.txn_id)
        self._open.pop(txn.txn_id, None)
        self._index_catalog_activation_plans.pop(txn.txn_id, None)
        if self._mode_counts[mode] > 0:
            self._mode_counts[mode] -= 1
        return self._mode_counts[mode], descriptor_failure

    def _publish_gauge(self, mode: str, open_now: int) -> None:
        """Publish one gauge without letting telemetry change a lifecycle outcome."""
        try:
            if self._metrics.enabled:
                self._metrics.set_gauge(
                    ACTIVE_TRANSACTIONS,
                    float(open_now),
                    {"mode": mode},
                )
        except BaseException:  # noqa: BLE001 - telemetry is strictly outcome-neutral
            return

    def _increment_metric(self, name: str) -> None:
        """Increment one counter without exposing a raw host BaseException."""
        try:
            if self._metrics.enabled:
                self._metrics.increment(name)
        except BaseException:  # noqa: BLE001 - telemetry is strictly outcome-neutral
            return

    def __repr__(self) -> str:
        """Return a representation naming the granularity and how many transactions are open."""
        return (
            f"TransactionManager(partitions_per_table={self._partitions_per_table}, "
            f"open_transactions={len(self._open)})"
        )


def _metrics_are_enabled(metrics: MetricsSink) -> bool:
    """Read the optional telemetry capability without changing a transaction outcome."""
    try:
        return metrics.enabled is True
    except BaseException:  # noqa: BLE001 - a metrics adapter is never transaction authority
        return False


def _trusted_wal_bytes(wal: object) -> int | None:
    """Return cached live bytes only for the engine's exact, callback-free WAL implementation."""
    if type(wal) is not WalManager:
        return None
    return wal._total_bytes  # noqa: SLF001 - exact trusted type; diagnostics must not refresh I/O


def _count_foreign_commits(
    records: Iterable[WalRecord], floor: Lsn, count: list[int]
) -> Iterator[WalRecord]:
    """Yield a WAL stream unchanged while counting complete outcomes above ``floor``."""
    for record in records:
        if record.lsn > floor and record.record_type == int(WalRecordType.COMMIT):
            count[0] += 1
        yield record


def _require_positive_int(field: str, value: object) -> int:
    """Return an exact positive integer for direct manager composition."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GrafxConfigurationError(
            f"{field} must be a positive integer; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return int(value)


def _require_optional_positive_limit(field: str, value: int | None) -> int | None:
    """Return an exact optional positive limit for direct manager composition."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GrafxConfigurationError(
            f"{field} must be a positive integer or None; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return int(value)


def _require_timeout(label: str, value: float) -> float:
    """Return the timeout when it is a positive, finite number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxConfigurationError(
            f"{label} must be a number of seconds; got {type(value).__name__}.",
            field=label,
            value=repr(value),
        )
    seconds = float(value)
    if seconds != seconds or seconds <= 0.0 or seconds == float("inf"):
        raise GrafxConfigurationError(
            f"{label} must be a positive, finite number of seconds; got {value!r}.",
            field=label,
            value=repr(value),
        )
    return seconds


def _require_forward_commit(committed: Csn, published: Lsn) -> Csn:
    """Refuse a commit number the log put at or below what the database has already published.

    A fresh commit is appended after everything the log holds, so its sequence number is above
    the published one by construction -- inside the commit section nothing else can have moved
    either. A log that answers otherwise is numbering two commits the same, and carrying on
    would barrier a record that overwrites one already acknowledged and publish a state that
    moves backwards; every reader that then took a snapshot would be reading a database that had
    forgotten a commit it confirmed.

    The check runs BEFORE the barrier, so nothing was acknowledged. The append has nevertheless
    returned after placing a complete COMMIT record in the WAL; without a successful barrier its
    crash outcome is uncertain, and reusing a sequence number is itself broken lineage. The
    caller therefore latches this participant recovery-required instead of stepping over or
    silently truncating the conflicting tail.

    This is defence in depth for carried finding CF-6, where a real log holding its segment
    index in memory issued one number to two participants. Fixing that belongs to the log; this
    comparison costs nothing and turns the next variant of it into a typed refusal instead of a
    silent overwrite.
    """
    if committed > published:
        return committed
    raise GrafxTransactionStateError(
        f"The log assigned commit number {committed} to a commit appended after commit number "
        f"{published} was already published; a fresh commit is always above it.",
        field="csn",
        value=committed,
        published_lsn=published,
    )


def _require_lsn(label: str, value: object) -> Lsn:
    """Return the value when the log answered with a usable sequence number."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxTransactionStateError(
            f"The log reported {label} as {type(value).__name__}; an integer is required.",
            field=label,
            value=repr(value),
        )
    if value < NO_LSN:
        raise GrafxTransactionStateError(
            f"The log reported {label} as {value}, which is not a sequence number.",
            field=label,
            value=value,
        )
    return value


def _file_name_of(store: object, fallback: str) -> str:
    """Return the file a store writes, or the well-known name when it does not say."""
    name = getattr(store, "file", None)
    return name if isinstance(name, str) and name else fallback


def _larger(left: int, right: int) -> int:
    """Return the larger of two numbers without importing anything to do it."""
    return left if left > right else right


def _release_quietly(lease: LeaseGuard) -> BaseException | None:
    """Release a lease, never letting the release replace what the caller is already handling.

    A commit that reached the barrier is durable whatever happens to the lease afterwards, and a
    commit that failed before it has a failure of its own to report. Either way the lease is
    reclaimed by the stall threshold, which is the mechanism FR-7 already relies on.
    """
    try:
        lease.release()
    except BaseException as failure:
        return failure
    return None


def _close_quietly(registration: ReaderRegistration) -> BaseException | None:
    """Withdraw a registration without letting the withdrawal replace a failure in flight."""
    try:
        registration.close()
    except BaseException as failure:
        return failure
    return None


def _first_failure(
    first: BaseException | None, second: BaseException | None
) -> BaseException | None:
    """Return the first cleanup failure so later cleanup never replaces it."""
    return first if first is not None else second


def _accumulate_failure(
    first: BaseException | None, later: BaseException | None
) -> BaseException | None:
    """Retain the first failure and attach every later cleanup failure as evidence."""
    if later is None:
        return first
    if first is None:
        return later
    _note_cleanup_failure(first, later)
    return first


def _note_cleanup_failure(primary: BaseException, cleanup: BaseException) -> None:
    """Attach cleanup evidence without changing the exception identity being propagated."""
    try:
        primary.add_note(f"Cleanup also failed ({type(cleanup).__name__}): {cleanup}")
    except BaseException:  # noqa: BLE001 - diagnostics must never replace either failure
        return


def _wal_is_damaged(wal: object) -> bool:
    """Return True when append rollback left WAL damage or sticky outcome uncertainty."""
    try:
        damage = getattr(wal, "damage")
    except AttributeError:
        return False
    except BaseException:
        return True
    try:
        uncertain = bool(getattr(wal, "append_uncertain", False))
    except BaseException:
        uncertain = True
    return damage is not None or uncertain


def _already_committed(
    failure: GrafxError,
    csn: Csn,
    *,
    recovery_required: bool = True,
) -> GrafxError:
    """Return the failure marked as one that happened AFTER the commit became durable.

    The barrier of step 3.5 has returned by the time this is reached, so the transaction is
    committed no matter what failed next: applying its pages and publishing the state are both
    work that recovery redoes from the log. Reporting a plain failure here would invite the one
    response that causes real harm -- a caller retrying work the database has already accepted --
    so the classification says exactly that, in the field amendment A47 makes every retry
    predicate read.
    """
    failure.retryable = False
    failure.details["committed"] = True
    failure.details["csn"] = csn
    failure.details["durable"] = True
    failure.details["recovery_required"] = recovery_required
    failure.details["retryable"] = False
    return failure


def _foreign_already_committed(
    failure: Exception,
    csn: Csn,
    *,
    recovery_required: bool,
) -> GrafxTransactionStateError:
    """Translate a foreign post-barrier escape into a non-retryable durable outcome."""
    translated = GrafxTransactionStateError(
        f"Commit {csn} is already durable; {type(failure).__name__} escaped while applying "
        "or publishing its post-barrier effects. Do not retry this transaction; recover the "
        "database handle before further work.",
        operation="commit",
        committed=True,
        csn=csn,
        durable=True,
        recovery_required=recovery_required,
        original_type=type(failure).__name__,
    )
    translated.details["retryable"] = False
    return translated


def _note_durable_commit(
    failure: BaseException,
    csn: Csn,
    *,
    recovery_required: bool,
) -> None:
    """Annotate a process-control signal without changing its identity or propagation."""
    try:
        failure.add_note(
            f"Okto Grafx commit {csn} is already durable (committed=True, durable=True, "
            f"recovery_required={recovery_required}); do not retry this transaction."
        )
    except BaseException:  # noqa: BLE001 - diagnostics cannot replace a process-control signal
        return
