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

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, is_dataclass, replace
from typing import Any

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxRecoveryRefused,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
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
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.coordination import ProcessCoordinator
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.page.layout import MAX_U64
from okto_grafx.domain.recovery.decision import CommittedReplay, committed_replay
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import ENDPOINT_COLUMN_COUNT, encode_tuple
from okto_grafx.domain.page import HEADER_PAGE_INDEX, Page
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_record import CommitPayload, FileIdMap, PageTouch
from okto_grafx.domain.txn.commit_state import CommitState
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
    partition_of,
    validate_partitions_per_table,
)
from okto_grafx.domain.txn.records import (
    WalRecord,
    WalRecordLike,
    WalRecordType,
    decode_page_write,
    encode_page_write,
    is_redoable_page_file,
)
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.buffer_pool import BufferPool, apply_page_image
from okto_grafx.engine.commit_state_store import (
    COMMIT_STATE_READ_ATTEMPTS,
    CommitStateStore,
)
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.heap_store import FIRST_RECORD_ID
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
    "COMMIT_RETRIES_TOTAL",
    "COMMIT_SECTION",
    "PARTICIPANT_SECTION_PREFIX",
    "COMMIT_STATE_READ_ATTEMPTS",
    "TRANSACTION_MANAGER_METRICS",
    "WRITE_CONFLICTS_TOTAL",
    "TransactionManager",
]

WRITE_CONFLICTS_TOTAL: str = "oktografx_write_conflicts_total"
COMMIT_RETRIES_TOTAL: str = "oktografx_commit_retries_total"
ACTIVE_TRANSACTIONS: str = "oktografx_active_transactions"

TRANSACTION_MANAGER_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (WRITE_CONFLICTS_TOTAL, COMMIT_RETRIES_TOTAL, ACTIVE_TRANSACTIONS)
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

_LIVE_FLAGS: int = ~1
"""Mask that clears the deleted bit of a record header, from CONTRACT.md section 6.4."""


@dataclass(frozen=True, slots=True)
class _RowWrite:
    """One heap change a commit made: the version it created and the version it ended.

    An insert is a birth alone, a delete an ending alone, and an update is both -- which is why
    they travel together. Both halves carry the same commit number, so a snapshot below it finds
    exactly one live version and a snapshot at or above it finds exactly one.

    The table and the two value tuples travel with the refs because the secondary indexes are
    keyed on the VALUES, not on the reference: ending the entry a version had needs the values
    that version carried, and they are gone from the caller's hands by the time the commit runs.
    ``ended_values`` is read from the heap rather than taken from the caller, because the heap is
    the authority inside the commit section and a caller's copy can be one version stale.
    """

    born: RecordRef | None
    ended: RecordRef | None
    table: object = None
    born_values: tuple[object, ...] = ()
    ended_values: tuple[object, ...] = ()


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
        "_index_manager",
        "_index_sync",
        "_partitions_per_table",
        "_commit_lock_timeout",
        "_dirty_mark",
        "_lease_timeout",
        "_reader_stall_threshold",
        "_refresh_interval",
        "_descriptor",
        "_file_ids",
        "_participant_section_name",
        "_retain_lease",
        "_lease_guard",
        "_next_txn_id",
        "_open",
        "_pins",
        "_published_high_water",
        "_own_published_lsn",
        "_recovery_required",
        "_page_staging_capability",
        "_mode_counts",
        "_max_transaction_rows",
        "_max_transaction_bytes",
        "_max_wal_batch_bytes",
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
        lease_timeout: float | None = None,
        reader_stall_threshold: float | None = None,
        descriptor: str = "",
        retain_lease: bool = False,
        index_sync: Callable[[], object] | None = None,
        writable: bool = True,
        max_transaction_rows: int | None = None,
        max_transaction_bytes: int | None = None,
        max_wal_batch_bytes: int | None = None,
        database_uuid: bytes | None = None,
        control_format_version: int = 1,
        control_file_nonce: int = 0,
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
        * the three ``max_*`` values -- opt-in transaction admission limits. ``None`` preserves
          existing behaviour; direct composition must provide exact positive integers.
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
        self._index_manager: Any = index_manager
        self._index_sync: Callable[[], object] | None = index_sync
        self._partitions_per_table: int = validate_partitions_per_table(
            partitions_per_table
        )
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
        self._lease_guard: LeaseGuard | None = None
        self._participant_section_name: str = (
            f"{PARTICIPANT_SECTION_PREFIX}"
            f"{crc32c(coordinator.owner_id().encode('utf-8')):08x}"
        )
        self._next_txn_id: TxnId = 1
        self._open: dict[TxnId, TransactionContext] = {}
        self._pins: dict[TxnId, _ReaderPin] = {}
        self._published_high_water: Lsn = NO_LSN
        # The last commit number THIS manager published through step 3.7 (CQ-2/QW-4). Only
        # _publish_commit_state remembers it: a gap completion or a checkpoint publishes
        # through _publish and must never be remembered here, because their LSN can carry a
        # foreign commit and an "own" read view over it would keep frames that predate it.
        # Compared by equality only -- recovery can republish a smaller number and a foreign
        # checkpoint republishes the same one.
        self._own_published_lsn: Lsn | None = None
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
        )

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
    def page_access_section(self) -> Iterator[None]:
        """Keep the recovery latch stable for one page-touching public operation.

        A check performed immediately before a flush, query or verification still leaves a
        scheduling window: another thread can fail after its WAL barrier, latch this participant
        recovery-required, and leave the first thread free to evict or write an older frame. The
        participant section closes that window because every path that can set the latch after a
        durable commit holds the same section. An operation that enters first finishes before the
        latch can be set; one that enters afterwards refuses before touching the pool.
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
                yield

    def _require_recovery_complete(self) -> None:
        """Refuse work that could publish over a durable commit missing from the pages."""
        if not self._recovery_required:
            return
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

        This helper emits no host metric.  Retry uses it immediately after forgetting the old
        context, so no close or competing lifecycle door can enter between withdrawal of the old
        pin and publication of its successor.  Every failure after registration attempts to
        withdraw the partial pin and leaves no local tracking entry behind.
        """
        self._require_not_closed("begin a transaction")
        if mode is TransactionMode.WRITE:
            self._require_writable("begin a write transaction")
        self._require_recovery_complete()
        registration: ReaderRegistration | None = None
        transaction: TransactionContext | None = None
        counted = False
        try:
            self._refresh_due_readers()
            self._require_not_closed("begin a transaction")
            floor = self._published_state_in_section().last_committed_lsn
            self._require_not_closed("begin a transaction")
            with self._close_wait_hazard():
                registration = ReaderRegistration.open(self._coordinator, floor)
            self._require_not_closed("begin a transaction")
            selected = self._published_state_in_section().last_committed_lsn
            self._require_not_closed("begin a transaction")
            read_lsn = selected if selected > floor else floor
            # L22: derived state needs a SHARED signal to invalidate it, and the published
            # commit number is the one this component already watches -- it moves whenever any
            # participant commits and nowhere else. Without this, a transaction opened in a
            # participant that had already read a table answers from frames cached before
            # somebody else committed: no error, no missing file, just fewer rows than exist.
            own_view = read_lsn == self._own_published_lsn
            with self._close_wait_hazard():
                self._pool.begin_read_view(
                    read_lsn,
                    own=own_view,
                    unfenced_file=self._file_ids.catalog_file,
                )
            if not own_view:
                # The token was met without provenance once; a later token that happens to
                # return to the old own number (a foreign recovery can republish it) must
                # not be met as own either. Only the next _publish_commit_state re-arms.
                self._own_published_lsn = None
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
            opened_at = self._monotonic()
            self._require_not_closed("begin a transaction")
            self._open[transaction.txn_id] = transaction
            self._pins[transaction.txn_id] = _ReaderPin(registration, opened_at)
            self._mode_counts[mode.value] += 1
            counted = True
            self._next_txn_id += 1
            return transaction, self._mode_counts[mode.value]
        except BaseException as failure:
            if transaction is not None:
                self._open.pop(transaction.txn_id, None)
                self._pins.pop(transaction.txn_id, None)
            if counted and self._mode_counts[mode.value] > 0:
                self._mode_counts[mode.value] -= 1
            cleanup_failure = (
                None
                if registration is None
                else self._close_reader_quietly(registration)
            )
            if cleanup_failure is not None:
                _note_cleanup_failure(failure, cleanup_failure)
            raise

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
        self._refresh_due_readers(skip=txn.txn_id)
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
        open_now = self._forget(txn, mode)
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
            self._refresh_due_readers(skip=txn.txn_id)
        except BaseException as refresh_failure:
            failure = _accumulate_failure(failure, refresh_failure)
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
            return self._refresh_due_readers(now_monotonic, skip=None)

    def _refresh_due_readers(
        self, now_monotonic: float | None = None, *, skip: TxnId | None = None
    ) -> int:
        """Refresh every due registration except the one named, and return how many moved.

        ``skip`` names a transaction that is being finished. Proving a reader alive one call
        before withdrawing it is a control-file write nobody reads, and this component is called
        on every begin, commit and rollback.
        """
        if not self._pins:
            return 0
        now = self._monotonic() if now_monotonic is None else float(now_monotonic)
        interval = self._refresh_interval
        refreshed = 0
        for txn_id, pin in list(self._pins.items()):
            if txn_id == skip or pin.registration.closed:
                continue
            if now < pin.last_refresh + interval:
                continue
            with self._close_wait_hazard():
                pin.registration.refresh()
            pin.last_refresh = now
            refreshed += 1
        return refreshed

    def checkpoint(self) -> RecycleReport:
        """Put the committed state on the platter, publish the checkpoint, and reclaim the log behind it.

        This is the lifecycle door BR-10 was waiting for (CF-11): ``WalManager.recycle`` and
        ``recyclable_horizon`` existed and nothing called either, so the log grew without bound.
        The checkpoint is also what lets recovery start somewhere other than the first record.

        Three steps, in this order, under the commit section so no commit is half-published while
        the checkpoint decides what is safe:

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
        2. **Flush and barrier every dirty page of this pool** (``BufferPool.checkpoint``), which
           now includes the pages the redo installed.
        3. **Publish** ``checkpoint_lsn = last_committed_lsn`` beside the unchanged commit numbers.

        Only then is the log asked to recycle, up to the horizon the reader registry and the new
        checkpoint allow together. A reader still pinned below the checkpoint holds the horizon
        back on its own account; nothing here evicts it.
        """
        return self._checkpoint(None)[0]

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
                transition, state, transition_is_commit_point
            )
        except BaseException as failure:
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
                        )
                    )
                    # Recycling belongs to the same stable-WAL picture as redo and checkpoint
                    # publication.  Releasing COMMIT_SECTION before this call let startup
                    # recovery scan while segments were disappearing underneath it.
                    reader_present = self._reader_horizon() is not None
                    with self._close_wait_hazard():
                        state["recycled"] = self._wal.recycle(
                            self._recyclable_horizon_in_section(),
                            reader_present=reader_present,
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
        try:
            durable = self._read_commit_state()
            with self._close_wait_hazard():
                tail = committed_replay(
                    self._wal.read_from(durable.last_committed_lsn + 1)
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
            )
            self._publish(completed)
            self._published_high_water = _larger(self._published_high_water, target)
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
            decode_page_write(record.payload).file == self._file_ids.catalog_file
            for record in page_records
        )
        self._commit_redo.preflight(
            replay,
            allow_unregistered_indexes=touched_catalog,
        )
        page_result = self._commit_redo.apply(page_replay)
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
        index_result = self._commit_redo.apply(index_replay)
        self._commit_redo.flush(page_result)
        self._commit_redo.flush(index_result)
        if manager is not None and replay.last_committed_lsn > NO_LSN:
            manager.mark_built_through(replay.last_committed_lsn, watermarks=watermarks)
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
                                pin = self._pins.pop(txn.txn_id, None)
                                if pin is not None:
                                    txn_failure = _accumulate_failure(
                                        txn_failure,
                                        self._close_reader_quietly(pin.registration),
                                    )
                            failure = _accumulate_failure(failure, txn_failure)
                        for pin in list(self._pins.values()):
                            failure = _accumulate_failure(
                                failure,
                                self._close_reader_quietly(pin.registration),
                            )
                        self._pins.clear()
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
            self._refresh_due_readers(skip=txn.txn_id)
            txn.mark_committed(csn)
            # Reader withdrawal is best-effort after the outcome is settled. Its registration
            # has a TTL; surfacing a foreign cleanup exception here would invite a caller to
            # retry an operation this transaction has already completed.
            self._release_reader(txn)
            open_now = self._forget(txn, mode)
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
        with self._participant_section():
            self._require_not_closed("commit a transaction")
            self._require_current_active(txn)
            self._require_writable("commit a write transaction")
            self._require_recovery_complete()
            self._validate_staged_inputs(txn)
            txn.validate_budgets()
            self._refresh_due_readers(skip=txn.txn_id)
            lease = self._hold_lease()
            try:
                # Step 2: the epoch is confirmed before any byte can reach the device (BR-7,
                # AC-6), through the coordinator that granted this very lease (A74).
                self._validate_lease(lease)
                with (
                    self._coordinator_section(
                        COMMIT_SECTION, timeout=self._commit_lock_timeout
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
                    with self._close_wait_hazard():
                        self._pool.begin_read_view(
                            current,
                            own=own_view,
                            unfenced_file=self._file_ids.catalog_file,
                        )
                    if not own_view:
                        self._own_published_lsn = None
                    # The rows go in BEFORE validation, because the pages they land on are
                    # part of what this commit will overwrite and therefore part of what it must
                    # declare. Nothing of them is durable yet, and a refusal below puts them
                    # beyond the reach of every snapshot (defect E1, carried finding CF-A).
                    # Validation runs TWICE, and the first pass is not redundant. The pages a
                    # row lands on are only known after the row is written, so the page half of
                    # the interest set cannot exist before then -- but writing the row first
                    # means the heap gets to refuse a version another participant has already
                    # ended, and it refuses with transaction_state, which tells a caller to stop
                    # where write_conflict would tell it to retry. Comparing what the caller
                    # declared BEFORE touching the heap keeps a real conflict reported as one.
                    with self._close_wait_hazard():
                        conflict = self._find_conflict(txn)  # step 3.3
                    rows: tuple[_RowWrite, ...] = ()
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
                            # Inside the guard, not before it: a refusal on the SECOND intent
                            # of a batch used to leave the first one written and never
                            # abandoned -- a phantom row the next commit of anyone flushed and
                            # published (C5 round-2 B1).
                            with self._close_wait_hazard():
                                rows = self._write_rows(txn)
                            self._declare_page_interest(txn, rows)
                            with self._close_wait_hazard():
                                conflict = self._find_conflict(
                                    txn
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
                            with self._close_wait_hazard():
                                records, images, materialized_csn = self._build_records(
                                    txn, lease.epoch, rows
                                )
                            txn.validate_budgets()
                            self._validate_wal_batch_budget(txn, records)
                            with self._close_wait_hazard():
                                planned_csn = self._wal.planned_terminal_lsn(records)
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
                                self._validate_wal_batch_budget(txn, records)
                            with self._close_wait_hazard():
                                committed = self._wal.append_many(
                                    records,
                                    expected_terminal_lsn=planned_csn,
                                )  # step 3.4
                            _require_forward_commit(committed, current)
                            with self._close_wait_hazard():
                                self._wal.barrier()  # step 3.5 -- durable here
                            # The WAL outcome is irrevocable at this instant. Settle it before
                            # page/index apply, publication, lease release, reader cleanup or any
                            # other fallible callback can run and tempt a caller to retry an ACTIVE
                            # transaction whose COMMIT is already durable.
                            txn.bind_epoch(lease.epoch)
                            txn.mark_committed(committed)
                    except BaseException as failure:
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
                            with self._close_wait_hazard():
                                self._apply_images(images)  # step 3.6
                            with self._close_wait_hazard():
                                self._apply_index_changes(txn, committed)  # step 3.6
                            with self._close_wait_hazard():
                                self._publish_commit_state(
                                    durable, committed
                                )  # step 3.7
                        except BaseException as failure:
                            post_barrier_failure = failure
                            if isinstance(failure, GrafxError):
                                try:
                                    with self._close_wait_hazard():
                                        recovered = self._recover_post_barrier(
                                            txn, durable, committed, rows
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
                open_now = self._forget(txn, mode)
            else:
                txn.mark_conflicted()
            lease_failure = self._drop_lease(lease)
            cleanup_failure = _first_failure(cleanup_failure, lease_failure)
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
        self, txn: TransactionContext, rows: Sequence[_RowWrite]
    ) -> None:
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
        for file, page_index in txn.staged_pages():
            txn.write_partitions.add(page_partition(file, page_index))
        for page_index in self._pages_touched_by(rows):
            txn.write_partitions.add(page_partition(self._heap_file, page_index))
        # Every page this attempt actually modified, which is a superset of the two above and
        # is the one that includes a relinked chain page. Without it two participants appending
        # to one table both rewrote the same tail page and neither conflicted, because the page
        # that carried the difference was in nobody's interest set.
        for file, page_index in self._attempt_pages():
            txn.write_partitions.add(page_partition(file, page_index))
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

    def _find_conflict(self, txn: TransactionContext) -> tuple[int, ...] | None:
        """Return the partitions that make this commit conflict, or None when none do.

        The predicate is the one CONTRACT.md section 8.5 step 3.3 states and nothing more: a
        conflict exists when a COMMIT record appended after this transaction's snapshot WROTE a
        partition this transaction read or wrote.

        Both halves matter. Dropping the read set would let a transaction that decided something
        from a row commit after that row changed underneath it. Adding the other side's READ set
        would refuse two transactions that merely looked at the same partition, which BR-6 calls
        out by name: conflict is intersection, never the existence of another writer.
        """
        interested = txn.read_partitions | txn.write_partitions
        if not interested:
            # Nothing to intersect with. Skipping the scan cannot change the answer, because the
            # intersection of an empty set with anything is empty.
            return None
        floor = txn.snapshot.read_lsn
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
        staged = list(txn.staged_pages())
        for page_index in self._pages_touched_by(rows):
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
        predicted = (
            base
            + len(staged)
            + len(txn.pending_records)
            + self._index_record_count(rows)
            + 1
        )
        if predicted >= PROVISIONAL_CSN:
            raise GrafxTransactionStateError(
                "The write-ahead log has exhausted its usable commit-number space; the maximum "
                "unsigned value is reserved for provisional heap versions.",
                field="last_lsn",
                value=base,
            )
        self._stage_index_changes(txn, rows, predicted)
        pending = list(txn.pending_records)
        self._validate_pending_index_records(txn, pending)
        images: list[tuple[str, PageIndex, bytes]] = []
        records: list[WalRecordLike] = []
        for file, page_index in staged:
            image = txn.page_images.get((file, page_index))
            if image is None:
                image = self._read_image(file, page_index)
            stamped = self._committed_image(
                file,
                page_index,
                image,
                predicted,
                rows,
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
        for file, page_index, image in images:
            corrected = self._committed_image(
                file,
                page_index,
                image,
                new_csn,
                rows,
            )
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

    def _values_at(self, reference: RecordRef) -> tuple[object, ...]:
        """Return the values the version at this reference carries, for the index to key on.

        Read from the heap rather than taken from the caller: inside the commit section the heap
        is the authority, and a caller's copy of a row can be one version behind. A reference the
        heap cannot read yields nothing, and the index staging then has no key to end -- which is
        the same outcome as a table with no index, and strictly better than ending the wrong one.
        """
        if self._index_manager is None:
            return ()
        try:
            return tuple(self._heap.read(reference).values)
        except GrafxError:
            return ()

    def _index_record_count(self, rows: Sequence[_RowWrite]) -> int:
        """Return how many log records the index staging of these rows will produce.

        The number is needed BEFORE the staging happens, because the commit number every index
        change carries is the sequence number of the COMMIT record, and that number depends on
        how long the batch is -- which these very records lengthen. Counting first breaks the
        circle without a provisional stamp to correct afterwards.

        Keeping the count and the staging in step is an invariant in two places (A66), so
        :meth:`_stage_index_changes` re-counts what it actually staged and refuses when the two
        disagree, rather than letting a silent drift put a wrong commit number on an entry.
        """
        manager = self._index_manager
        if manager is None:
            return 0
        total = 0
        for row in rows:
            table_id = getattr(row.table, "table_id", None)
            if table_id is None:
                continue
            if row.ended is not None:
                total += manager.row_entry_count(table_id, row.ended_values)
            if row.born is not None:
                total += manager.row_entry_count(table_id, row.born_values)
        return total

    def _stage_index_changes(
        self, txn: TransactionContext, rows: Sequence[_RowWrite], csn: Csn
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
        expected = self._index_record_count(rows)
        for row in rows:
            table_id = getattr(row.table, "table_id", None)
            if table_id is None:
                continue
            if row.ended is not None:
                manager.stage_row_delete(
                    txn, table_id, row.ended, row.ended_values, csn
                )
            if row.born is not None:
                manager.stage_row_insert(txn, table_id, row.born, row.born_values, csn)
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
            self._publish_commit_state(previous, committed)
            return True
        except BaseException:
            pass
        manager = self._index_manager
        if manager is None:
            return False
        tables = {getattr(row.table, "table_id", None) for row in rows}
        for table_id in tables:
            if table_id is None:
                continue
            for index in manager.indexes_for(table_id):
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
        return int(manager.commit(txn, csn))

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

    def _write_rows(self, txn: TransactionContext) -> tuple[_RowWrite, ...]:
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
        if not txn.row_intents:
            return ()
        heap = self._heap
        provisional = PROVISIONAL_CSN
        written: list[_RowWrite] = []
        try:
            self._write_intents(txn, heap, provisional, written)
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
        txn.row_refs = [item.born for item in written if item.born is not None]
        return tuple(written)

    def _write_intents(
        self,
        txn: TransactionContext,
        heap: object,
        provisional: Csn,
        written: list[_RowWrite],
    ) -> None:
        """Write each settled intent into the heap, appending to ``written`` as each one lands."""
        for intent in self._resolved_intents(txn, heap):
            if intent.operation is RowOperation.DELETE:
                ending = self._values_at(intent.reference)
                heap.delete(intent.table, intent.reference, provisional)
                written.append(
                    _RowWrite(
                        born=None,
                        ended=intent.reference,
                        table=intent.table,
                        ended_values=ending,
                    )
                )
                continue
            if intent.operation is RowOperation.UPDATE:
                ending = self._values_at(intent.reference)
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
                    )
                )
                continue
            # Planned above, never None here: the identity had to exist before the row was
            # written, because an edge staged in this same transaction may already carry it.
            record_id = intent.record_id
            heap.observe_record_id(intent.table, record_id)
            reference = heap.insert(intent.table, record_id, intent.values, provisional)
            written.append(
                _RowWrite(
                    born=reference,
                    ended=None,
                    table=intent.table,
                    born_values=tuple(intent.values),
                )
            )

    def _resolved_intents(
        self, txn: TransactionContext, heap: object
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
        settled = reduce_row_intents(txn.row_intents)
        plans = plan_relationship_endpoints(
            settled, txn_id=txn.txn_id, owns=txn.owns_pending_row_ref
        )
        planned = self._plan_record_ids(txn, settled, heap)
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

    def _plan_record_ids(
        self, txn: TransactionContext, intents: Sequence[RowIntent], heap: object
    ) -> dict[int, int]:
        """Return the durable identity each staged insert will take, by position, spending none.

        Deterministic per table, and in two passes rather than one. The identities a batch chose
        for ITSELF are collected first, and each table's cursor starts above all of them, so an
        implicit identity cannot land on an explicit one that appears LATER in the same batch --
        which a single pass cannot know about, and which is how two rows of one table end up
        under one identity while every step looks locally correct.
        """
        reserved = self._reserved_record_ids(txn, intents)
        self._refuse_reused_identities(txn, reserved)
        planned: dict[int, int] = {}
        cursors: dict[int, int] = {}
        for position, intent in enumerate(intents):
            if intent.operation is not RowOperation.INSERT:
                continue
            table = intent.table
            table_id = getattr(table, "table_id", None)
            if table_id not in cursors:
                taken = reserved.get(table_id, (None, frozenset()))[1]
                cursors[table_id] = max(
                    (heap.next_record_id(table), *(identity + 1 for identity in taken))
                )
            identity = intent.record_id
            if identity is None:
                identity = cursors[table_id]
                cursors[table_id] = identity + 1
            self._require_unexhausted_identity(txn, table, table_id, identity)
            planned[position] = identity
        return planned

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

    def _abandon_rows(self, rows: Sequence[_RowWrite]) -> BaseException | None:
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
        if rows:
            touched.add((self._heap_file, HEADER_PAGE_INDEX))
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

        The reserved header page is always included: it carries the table directory, and every
        insert can move the extent hint and the identity counter on it. Including a page that did
        not change costs one image in the log and changes nothing else; missing one that did
        would leave a change with no record to redo it, which is the failure the log exists to
        prevent.
        """
        if not rows:
            return ()
        touched = {HEADER_PAGE_INDEX}
        for item in rows:
            if item.born is not None:
                touched.add(item.born.page)
            if item.ended is not None:
                # The page of the version being ENDED changes too: its header now says when it
                # stopped being current. A commit that logged the new version and not the end of
                # the old one would replay into two live versions of one record.
                touched.add(item.ended.page)
        return tuple(sorted(touched))

    def _committed_image(
        self,
        file: str,
        page_index: PageIndex,
        image: bytes,
        csn: Csn,
        rows: Sequence[_RowWrite],
    ) -> bytes:
        """Return the post-commit image without publishing the CSN into a live frame.

        Heap work has to be materialised before the page half of optimistic validation is known,
        but a WAL append can still fail after that.  The resident/device version therefore keeps
        the reserved provisional sentinel until the WAL barrier returns.  Only this local copy is
        rewritten to the predicted commit number; it is the byte-identical image appended to WAL
        and installed by :meth:`_apply_images` after the barrier.
        """
        page = self._pool.codec.decode_page(image, verify=True)
        if page.page_lsn < csn:
            page.page_lsn = csn
        if file == self._heap_file:
            for item in rows:
                if item.born is not None and item.born.page == page_index:
                    self._restamp_page(page, item.born, xmin=csn)
                if item.ended is not None and item.ended.page == page_index:
                    self._restamp_page(page, item.ended, xmax=csn)
        return self._pool.codec.encode_page(page)

    def _read_image(self, file: str, page_index: PageIndex) -> bytes:
        """Return the current image of one resident page, as the log will carry it."""
        with self._pool.pinned(file, page_index) as page:
            return self._pool.codec.encode_page(page)

    def _apply_images(self, images: Sequence[tuple[str, PageIndex, bytes]]) -> None:
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
        touched: set[str] = set()
        for file, page_index, image in images:
            apply_page_image(self._pool, file, page_index, image)
            touched.add(file)
        for file in sorted(touched):
            self._pool.flush(file)

    def _publish_commit_state(self, previous: CommitState, committed: Csn) -> None:
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
        )
        self._publish(state)
        # Remembered only here, and only after the publish landed: this is the one door that
        # publishes a commit THIS manager produced (both the live step 3.7 and its post-barrier
        # redo republication call it). The gap completion and the checkpoint publish through
        # _publish directly and stay foreign to the read-view exemption.
        self._own_published_lsn = committed

    def _publish(self, state: CommitState) -> None:
        """Write the state to a temporary of this participant and replace the published file.

        Every generic publication forfeits the own-view provenance FIRST: a gap completion or
        a checkpoint can carry a foreign commit, and a number published here must never be met
        by a later read view as "my own". _publish_commit_state re-marks it, after ITS publish
        returned, for the one number this manager provably produced itself.
        """
        self._own_published_lsn = None
        with self._close_wait_hazard():
            self._commit_state_store.publish(state)

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

    def _drop_lease(self, lease: LeaseGuard) -> BaseException | None:
        """Give the lease up and return, rather than raise, a foreign cleanup failure."""
        if self._retain_lease and lease is self._lease_guard and not lease.released:
            return None
        with self._close_wait_hazard():
            return _release_quietly(lease)

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
    def _coordinator_section(self, name: str, *, timeout: float) -> Iterator[object]:
        """Enter each foreign context phase under a hazard, but never mark its body.

        A coordinator owns the factory and both context-protocol callbacks. Any of those may
        synchronously ask another thread to close the database while this participant section
        is held. Keeping the marker around the yielded engine body would instead suppress
        normal close/quiescence during commit and schema settlement, so the phases are expanded
        explicitly here.
        """
        with self._close_wait_hazard():
            section = self._coordinator.exclusive(name, timeout=timeout)
        with self._close_wait_hazard():
            entered = section.__enter__()
        try:
            yield entered
        except BaseException as failure:
            with self._close_wait_hazard():
                suppressed = bool(
                    section.__exit__(type(failure), failure, failure.__traceback__)
                )
            if not suppressed:
                raise
        else:
            with self._close_wait_hazard():
                section.__exit__(None, None, None)

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
        """Detach a transaction's reader pin and return any foreign withdrawal failure."""
        pin = self._pins.pop(txn.txn_id, None)
        return None if pin is None else self._close_reader_quietly(pin.registration)

    def _forget(self, txn: TransactionContext, mode: str) -> int:
        """Drop a finished transaction and return how many of its mode are still open.

        The count is RETURNED rather than published here so the caller can emit it once the
        participant section is released: a metrics sink is host code and must never be called
        while this component holds anything (A91).
        """
        self._open.pop(txn.txn_id, None)
        if self._mode_counts[mode] > 0:
            self._mode_counts[mode] -= 1
        return self._mode_counts[mode]

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
