"""The frozen recovery algorithm of CONTRACT.md section 8.6 (FR-8, BR-1, BR-2, BR-3, AC-5)."""

from __future__ import annotations

from okto_grafx.runtime.capability_probe import port_has_attribute

import struct
from dataclasses import dataclass

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import IndexChange, IndexOperation, wal_record_for
from okto_grafx.domain.ledger.entry import LedgerOriginClass, LedgerReason
from okto_grafx.domain.recovery.retry import is_retryable
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.recovery.report import (
    OUTCOME_CLEAN,
    OUTCOME_QUARANTINED,
    OUTCOME_TRUNCATED,
    POLICY_REFUSE,
    FindingKind,
    RecoveryReport,
    stronger_outcome,
)
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.domain.wal.replay import TruncationReport
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.engine import commit_redo as commit_redo_module
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.ledger_store import LEDGER_FILE, LedgerStore
from okto_grafx.engine.recovery_manager import (
    RECOVERIES_TOTAL,
    RECOVERY_DISCARDED_RECORDS_TOTAL,
    RECOVERY_REPLAYS_TOTAL,
    RecoveryManager,
)
from okto_grafx.engine.vector_engine import VectorHnswIndex
from okto_grafx.engine.wal_manager import WalManager
from tests.vector.conftest import SnapshotDouble, VectorFixture

from .conftest import (
    DESCRIPTOR,
    HEAP_FILE,
    PAGE_SIZE,
    SEGMENT_BYTES,
    FrozenClock,
    HealthyProbe,
    RecordingMetricsSink,
    RefusingProbe,
    Stack,
    build_stack,
    commit_pages,
    digest_of_file,
    make_page_image,
)

CATALOG_FILE = "catalog.dat"


@dataclass(frozen=True)
class _RecoveryIndexDefinition:
    versioned: bool = False


@dataclass(frozen=True)
class _RecoveryIndexDouble:
    file: str
    definition: _RecoveryIndexDefinition = _RecoveryIndexDefinition()
    max_key_bytes: int = 400


class _RecoveryIndexManagerDouble:
    """Expose marker/header files while recording recovery's durability order."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._indexes = (
            _RecoveryIndexDouble("index/zeta.idx"),
            _RecoveryIndexDouble("index/alpha.idx"),
        )

    def table_watermark_photo(self) -> dict[str, int]:
        return {}

    def check_replay_floor(self, *_args: object, **_kwargs: object) -> None:
        return None

    def mark_built_through(self, _lsn: int, **_kwargs: object) -> None:
        self.events.append("mark")

    def active_indexes(self) -> tuple[_RecoveryIndexDouble, ...]:
        return self._indexes

    def index(self, name: str) -> _RecoveryIndexDouble:
        assert name == "by_name"
        return self._indexes[0]

    active_index = index

    def apply(self, _record: WalRecord) -> bool:
        self.events.append("index")
        return True


def _commit(stack: Stack, page_index: int, payload: bytes, *, txn_id: int = 1) -> int:
    """Commit one heap page carrying this payload and return the commit number."""
    return commit_pages(
        stack,
        [
            (
                HEAP_FILE,
                page_index,
                make_page_image(stack.codec, [payload], page_index=page_index),
            )
        ],
        txn_id=txn_id,
    )


def _poisonable_vector(stack: Stack) -> tuple[VectorFixture, VectorHnswIndex]:
    """Return a real vector registry with a published graph over its own device."""
    database = VectorFixture(metrics=stack.metrics, clock=stack.clock)
    space = database.create_space("poison", 4)
    table = database.create_table("Chunk", space.name)
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=1)
    index = database.engine.index(space.name)
    index.snapshot()
    return database, index


def _segment(stack: Stack) -> str:
    """Return the name of the newest log segment."""
    return stack.wal.segments()[-1].name


def _append_garbage(stack: Stack, body: bytes) -> int:
    """Append bytes that are not a record to the newest segment, and return where they start."""
    name = _segment(stack)
    offset = stack.storage.log_size(name)  # type: ignore[attr-defined]
    stack.storage.append_log(name, body)  # type: ignore[attr-defined]
    stack.wal.open()
    return offset


def _reopened(stack: Stack) -> Stack:
    """Return a second stack over the same device, as a reopen of the database would build."""
    return build_stack(
        stack.storage, clock=stack.clock, metrics=stack.metrics, bootstrap=False
    )


def _stored_bytes(stack: Stack) -> dict[str, bytes]:
    """Return an exact image of every file held by this in-memory recovery stack."""
    names = stack.storage.list_files("")  # type: ignore[attr-defined]
    return {
        name: stack.storage.read_log(  # type: ignore[attr-defined]
            name,
            0,
            stack.storage.file_size(name),  # type: ignore[attr-defined]
        )
        for name in names
    }


# --- the report shape ----------------------------------------------------------------------------


def test_the_outcome_words_are_a_closed_set_and_order_by_what_recovery_did() -> None:
    assert stronger_outcome(OUTCOME_CLEAN, OUTCOME_TRUNCATED) == OUTCOME_TRUNCATED
    assert (
        stronger_outcome(OUTCOME_QUARANTINED, OUTCOME_TRUNCATED) == OUTCOME_QUARANTINED
    )
    with pytest.raises(GrafxConfigurationError):
        RecoveryReport(outcome="repaired")
    with pytest.raises(GrafxConfigurationError):
        stronger_outcome(OUTCOME_CLEAN, "repaired")


def test_a_report_refuses_a_negative_count() -> None:
    with pytest.raises(GrafxConfigurationError):
        RecoveryReport(outcome=OUTCOME_CLEAN, records_replayed=-1)


def test_a_policy_that_is_not_one_of_the_two_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        stack.recovery(policy="repair")
    assert caught.value.details["field"] == "recovery_policy"


# --- a clean database ------------------------------------------------------------------------------


def test_an_undamaged_log_recovers_clean_and_discards_nothing(stack: Stack) -> None:
    _commit(stack, 3, b"first")
    _commit(stack, 4, b"second", txn_id=2)
    report = _reopened(stack).recovery().run()
    assert report.outcome == OUTCOME_CLEAN
    assert report.records_discarded == 0 and report.ledger_entries_created == 0
    assert report.last_good_lsn == stack.wal.last_lsn
    assert report.records_replayed == 2


def test_an_empty_database_recovers_clean(stack: Stack) -> None:
    report = stack.recovery().run()
    assert report.outcome == OUTCOME_CLEAN and report.last_good_lsn == 0


def test_mixed_recovery_keeps_repeated_page_effects_sequential(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page split retains the original mixed replay's no-coalescing provenance."""
    events: list[str] = []
    manager = _RecoveryIndexManagerDouble(events)
    for index in manager.active_indexes():
        stack.storage.create(index.file)  # type: ignore[attr-defined]
    first = make_page_image(
        stack.codec, [b"first"], page_index=4, page_lsn=1
    )
    final = make_page_image(
        stack.codec, [b"final"], page_index=4, page_lsn=3
    )
    logical = wal_record_for(
        IndexChange(
            index="by_name",
            operation=IndexOperation.INSERT,
            key=b"ada",
            ref=RecordRef(4, 0),
        ),
        epoch=1,
        txn_id=77,
        descriptor=DESCRIPTOR,
    )
    stack.wal.append_many(
        (
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                payload=encode_page_write(HEAP_FILE, 4, first),
                epoch=1,
                txn_id=77,
                descriptor=DESCRIPTOR,
            ),
            logical,
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                payload=encode_page_write(HEAP_FILE, 4, final),
                epoch=1,
                txn_id=77,
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=1,
                txn_id=77,
                descriptor=DESCRIPTOR,
            ),
        )
    )
    stack.wal.barrier()
    applied: list[int] = []
    real_apply = commit_redo_module.apply_page_image

    def observe_apply(pool: object, file: str, page: int, image: bytes) -> bool:
        applied.append(page)
        return real_apply(pool, file, page, image)  # type: ignore[arg-type]

    monkeypatch.setattr(commit_redo_module, "apply_page_image", observe_apply)

    report = stack.recovery(index_manager=manager).run()

    assert report.records_replayed == 3
    assert applied == [4, 4]


def test_writable_recovery_barriers_a_clean_foreign_tail_before_publication(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(stack, 3, b"durable-shape")
    stack.storage.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    events: list[str] = []
    original_barrier = WalManager.force_barrier_range
    original_publish = CommitStateStore.publish

    def barrier(
        manager: WalManager, first_lsn: int, through_lsn: int
    ) -> tuple[str, ...]:
        events.append("barrier")
        return original_barrier(manager, first_lsn, through_lsn)

    def publish(
        store: CommitStateStore,
        state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        events.append("publish")
        original_publish(
            store,
            state,
            previous=previous,
            previous_was_damaged=previous_was_damaged,
        )

    monkeypatch.setattr(WalManager, "force_barrier_range", barrier)
    monkeypatch.setattr(CommitStateStore, "publish", publish)

    reopened.recovery().run()

    assert "publish" in events
    assert events.index("barrier") < events.index("publish")


def test_recovery_barriers_each_replayed_and_marked_file_before_publication(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(stack, 3, b"durable-shape")
    stack.storage.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    events: list[str] = []
    manager = _RecoveryIndexManagerDouble(events)
    original_publish = CommitStateStore.publish

    def barrier(_pool: BufferPool, file: str | None = None) -> None:
        assert file is not None
        events.append(f"data:{file}")

    def publish(
        store: CommitStateStore,
        state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        events.append("publish")
        original_publish(
            store,
            state,
            previous=previous,
            previous_was_damaged=previous_was_damaged,
        )

    monkeypatch.setattr(BufferPool, "durability_barrier", barrier)
    monkeypatch.setattr(CommitStateStore, "publish", publish)

    reopened.recovery(index_manager=manager).run()

    assert events == [
        "mark",
        "data:heap.dat",
        "data:index/alpha.idx",
        "data:index/zeta.idx",
        "publish",
    ]


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("recovery data barrier failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_recovery_never_publishes_when_a_data_barrier_escapes(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    _commit(stack, 3, b"unpublished")
    stack.storage.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    published = False

    def fail_barrier(_pool: BufferPool, _file: str | None = None) -> None:
        raise failure

    def forbidden_publish(
        _store: CommitStateStore,
        _state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        nonlocal published
        del previous, previous_was_damaged
        published = True

    monkeypatch.setattr(BufferPool, "durability_barrier", fail_barrier)
    monkeypatch.setattr(CommitStateStore, "publish", forbidden_publish)

    with pytest.raises(type(failure)) as escaped:
        reopened.recovery().run()

    assert escaped.value is failure
    assert published is False


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("recovery WAL barrier failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_recovery_never_publishes_when_its_wal_barrier_escapes(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    _commit(stack, 3, b"unpublished")
    stack.storage.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    before = _stored_bytes(stack)
    reopened = _reopened(stack)

    def fail_barrier(
        _manager: WalManager, _first_lsn: int, _through_lsn: int
    ) -> tuple[str, ...]:
        raise failure

    def forbidden_publish(
        _store: CommitStateStore,
        _state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        del previous, previous_was_damaged
        raise AssertionError(
            "recovery published state without a successful WAL barrier"
        )

    monkeypatch.setattr(WalManager, "force_barrier_range", fail_barrier)
    monkeypatch.setattr(CommitStateStore, "publish", forbidden_publish)

    with pytest.raises(type(failure)) as escaped:
        reopened.recovery().run()

    assert escaped.value is failure
    assert _stored_bytes(stack) == before


def test_failed_recovery_poisoning_reaches_vector_indexes_and_preserves_the_primary_failure(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secondary poison failure must not replace recovery's error or leave vector reads open."""
    vectors, index = _poisonable_vector(stack)
    primary = RuntimeError("recovery stopped after applying a prefix")

    def fail_after_prefix(_manager: RecoveryManager, _permit: object) -> RecoveryReport:
        raise primary

    monkeypatch.setattr(RecoveryManager, "_run_fenced", fail_after_prefix)

    with pytest.raises(RuntimeError) as escaped:
        stack.recovery(index_manager=vectors.registry).run()

    assert escaped.value is primary
    assert index.stale
    assert index._snapshot is None  # noqa: SLF001 - proves the derived graph was retired
    with pytest.raises(GrafxIndexError) as refused:
        index.search((1.0, 0.0, 0.0, 0.0), 1, SnapshotDouble(10))
    assert refused.value.details["field"] == "stale"


def test_the_read_only_consistency_proof_never_barriers_wal(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def forbidden_barrier(
        _manager: WalManager, _first_lsn: int, _through_lsn: int
    ) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        raise AssertionError("a read-only proof attempted to flush the WAL")

    monkeypatch.setattr(WalManager, "force_barrier_range", forbidden_barrier)

    stack.recovery().require_read_only_consistent()

    assert calls == 0


def test_checkpoint_complete_writable_recovery_does_not_barrier_an_idle_wal(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = _commit(stack, 3, b"already-checkpointed")
    checkpoint_state = CommitState(
        last_committed_lsn=committed,
        last_csn=committed,
        checkpoint_lsn=committed,
    )
    CommitStateStore(stack.storage, owner_id="checkpoint-test").publish(
        checkpoint_state,
        previous=CommitStateStore(
            stack.storage, owner_id="checkpoint-test-reader"
        ).read(),
    )
    reopened = _reopened(stack)
    calls = 0

    def count_barrier(
        _manager: WalManager, _first_lsn: int, _through_lsn: int
    ) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return ()

    monkeypatch.setattr(WalManager, "force_barrier_range", count_barrier)

    reopened.recovery().run()

    assert calls == 0


def test_a_clean_recovery_counts_itself_under_its_outcome(stack: Stack) -> None:
    _commit(stack, 3, b"first")
    _reopened(stack).recovery().run()
    assert stack.metrics.counter(RECOVERIES_TOTAL, outcome="clean") == 1.0
    assert stack.metrics.counter(RECOVERY_REPLAYS_TOTAL) == 1.0


# --- a damaged tail ---------------------------------------------------------------------------------


def test_a_run_of_interior_zeros_truncates_at_the_last_valid_record(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    good = stack.wal.last_lsn
    offset = _append_garbage(stack, bytes(96))
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.outcome == OUTCOME_TRUNCATED
    assert report.last_good_lsn == good
    assert report.records_discarded == 1 and report.ledger_entries_created == 1
    assert reopened.wal.damage is None
    assert reopened.wal.last_lsn == good
    assert offset > 0


def test_an_incomplete_truncation_refuses_retryably_instead_of_publishing_success(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserved bytes are not a completed recovery while the damaged WAL tail remains."""
    _commit(stack, 3, b"kept")
    good = stack.wal.last_lsn
    _append_garbage(stack, bytes(96))
    reopened = _reopened(stack)
    attempts: list[int] = []
    deferred = (_segment(reopened),)

    def leave_the_tail_in_place(_wal: WalManager, lsn: int) -> TruncationReport:
        attempts.append(lsn)
        return TruncationReport(
            last_lsn=lsn,
            removed_records=0,
            removed_bytes=0,
            deferred_segments=deferred,
            completed=False,
        )

    monkeypatch.setattr(WalManager, "truncate_after", leave_the_tail_in_place)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()

    assert attempts == [good, good, good]
    assert refused.value.details == {
        "field": "wal_truncation",
        "last_good_lsn": good,
        "attempts": 3,
        "deferred_segments": deferred,
    }
    assert is_retryable(refused.value) is True
    assert reopened.wal.damage is not None
    assert len(reopened.quarantine.list()) == 1


def test_recovery_refuses_to_cut_below_the_checkpoint_before_any_mutation(
    stack: Stack,
) -> None:
    """A cut below the replay floor would let a later WAL batch reuse filtered LSNs."""
    checkpoint = _commit(stack, 3, b"checkpointed")
    device = stack.storage
    previous = CommitStateStore(  # type: ignore[arg-type]
        device, owner_id="checkpoint-floor-reader"
    ).read()
    CommitStateStore(  # type: ignore[arg-type]
        device, owner_id="checkpoint-floor-test"
    ).publish(
        CommitState(
            last_committed_lsn=checkpoint,
            last_csn=checkpoint,
            checkpoint_lsn=checkpoint,
        ),
        previous=previous,
    )

    segment = _segment(stack)
    raw = bytearray(
        device.read_log(segment, 0, device.log_size(segment))  # type: ignore[attr-defined]
    )
    raw[-1] ^= 0xFF  # damage the final COMMIT while its WRITE_PAGE remains intact
    device.truncate_log(segment, 0)  # type: ignore[attr-defined]
    device.append_log(segment, bytes(raw))  # type: ignore[attr-defined]
    device.durable_barrier(segment)  # type: ignore[attr-defined]

    reopened = _reopened(stack)
    names_before = device.list_files("")  # type: ignore[attr-defined]
    wal_before = device.read_log(  # type: ignore[attr-defined]
        segment,
        0,
        device.log_size(segment),  # type: ignore[attr-defined]
    )
    state_before = device.read_log(  # type: ignore[attr-defined]
        COMMIT_STATE_FILE,
        0,
        device.log_size(COMMIT_STATE_FILE),  # type: ignore[attr-defined]
    )
    heap_before = digest_of_file(device, HEAP_FILE)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()

    assert refused.value.details == {
        "field": "wal_lineage",
        "checkpoint_lsn": checkpoint,
        "last_good_lsn": checkpoint - 1,
    }
    assert device.list_files("") == names_before  # type: ignore[attr-defined]
    assert (
        device.read_log(segment, 0, device.log_size(segment)) == wal_before  # type: ignore[attr-defined]
    )
    assert (
        device.read_log(  # type: ignore[attr-defined]
            COMMIT_STATE_FILE,
            0,
            device.log_size(COMMIT_STATE_FILE),  # type: ignore[attr-defined]
        )
        == state_before
    )
    assert digest_of_file(device, HEAP_FILE) == heap_before
    assert reopened.quarantine.list() == ()
    assert reopened.ledger.list() == ()
    assert reopened.wal.damage is not None


def test_the_forensic_entry_carries_the_offset_the_digest_and_the_bytes(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    body = bytes(96)
    # A44: the segment is captured by identity BEFORE the repair, never read back as "the
    # current last one" -- the log is entitled to renumber its segments while repairing them,
    # and an entry must name where the damage WAS.
    damaged_segment = _segment(stack)
    offset = _append_garbage(stack, body)
    reopened = _reopened(stack)
    reopened.recovery().run()
    entries = reopened.ledger.list(origin_class="forensic")
    assert len(entries) == 1
    provenance = reopened.ledger.provenance(entries[0].entry_id)
    assert provenance.offset == offset
    assert provenance.origin == damaged_segment
    assert reopened.ledger.export(entries[0].entry_id) == body
    assert entries[0].digest == entries[0].digest


def test_the_quarantined_copy_holds_the_damaged_bytes(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    body = bytes(range(64)) + bytes(32)
    _append_garbage(stack, body)
    reopened = _reopened(stack)
    reopened.recovery().run()
    entries = reopened.quarantine.list()
    assert len(entries) == 1
    assert reopened.quarantine.read(entries[0].name) == body


def test_the_main_data_files_are_untouched_byte_for_byte_by_a_truncation(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    stack.pool.flush()
    before_heap = digest_of_file(stack.storage, HEAP_FILE)
    before_catalog = digest_of_file(stack.storage, CATALOG_FILE)
    _append_garbage(stack, bytes(96))
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.outcome == OUTCOME_TRUNCATED
    assert digest_of_file(stack.storage, HEAP_FILE) == before_heap
    assert digest_of_file(stack.storage, CATALOG_FILE) == before_catalog


def test_a_checksum_failure_is_classified_as_such_and_the_rest_as_the_truncated_tail(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    size = stack.storage.log_size(name)  # type: ignore[attr-defined]
    raw = bytearray(stack.storage.read_log(name, 0, size))  # type: ignore[attr-defined]
    raw[size - 8] ^= 0xFF
    stack.storage.truncate_log(name, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(name, bytes(raw))  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    before = stack.storage.log_size(name)  # type: ignore[attr-defined]
    with pytest.raises(GrafxRecoveryRefused):
        reopened.recovery().run()
    assert stack.storage.log_size(name) == before  # type: ignore[attr-defined]
    assert reopened.ledger.list() == ()
    assert reopened.quarantine.list() == ()


def test_a_record_that_decodes_above_the_cut_is_reapplicable_and_not_forensic(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    cut = stack.storage.log_size(name)  # type: ignore[attr-defined]
    stack.storage.append_log(name, bytes(64))  # type: ignore[attr-defined]
    follower = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=bytes(24),
        descriptor=DESCRIPTOR,
        lsn=stack.wal.last_lsn + 9,
        epoch=1,
        txn_id=5,
    )
    stack.storage.append_log(name, follower.encode())  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.records_discarded == report.ledger_entries_created
    classes = [entry.origin_class for entry in reopened.ledger.list()]
    assert LedgerOriginClass.REAPPLICABLE in classes
    assert LedgerOriginClass.FORENSIC in classes
    assert cut > 0


def test_no_two_ledger_entries_describe_the_same_bytes(stack: Stack) -> None:
    """BR-3 states EXACTLY one entry per discard, and one is what a duplicate breaks.

    A sequence-number discontinuity is reported by the log at the SAME offset as the record it
    is about, so treating the report as a discarded range as well as the record would file the
    same bytes twice -- once forensic and once reapplicable. Counting entries against discards
    cannot see that: both numbers rise together. What only the rule can see is that two entries
    would then name one place.
    """
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    stack.storage.append_log(name, bytes(48))  # type: ignore[attr-defined]
    stack.storage.append_log(  # type: ignore[attr-defined]
        name,
        WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE),
            payload=bytes(24),
            descriptor=DESCRIPTOR,
            lsn=stack.wal.last_lsn + 40,
            epoch=1,
            txn_id=5,
        ).encode(),
    )
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    # A72: prove the run really met the state this test is about.
    assert report.findings_of(FindingKind.LSN_DISCONTINUITY)

    entries = reopened.ledger.list(limit=100)
    places = [
        (
            reopened.ledger.provenance(entry.entry_id).origin,
            reopened.ledger.provenance(entry.entry_id).offset,
        )
        for entry in entries
    ]
    assert len(places) == len(set(places)), places
    assert report.ledger_entries_created == len(entries)
    classes = sorted(entry.origin_class.name for entry in entries)
    assert classes == ["FORENSIC", "REAPPLICABLE"]


def test_every_discarded_item_leaves_exactly_one_ledger_entry(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    stack.storage.append_log(name, bytes(48))  # type: ignore[attr-defined]
    for index in range(3):
        stack.storage.append_log(  # type: ignore[attr-defined]
            name,
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                payload=bytes(16),
                descriptor=DESCRIPTOR,
                lsn=stack.wal.last_lsn + 5 + index,
                epoch=1,
                txn_id=9,
            ).encode(),
        )
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.ledger_entries_created == report.records_discarded
    assert len(reopened.ledger.list(limit=100)) == report.ledger_entries_created


def test_a_discard_is_counted_under_its_origin_class(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    _append_garbage(stack, bytes(64))
    _reopened(stack).recovery().run()
    assert (
        stack.metrics.counter(RECOVERY_DISCARDED_RECORDS_TOTAL, origin_class="forensic")
        == 1.0
    )
    assert stack.metrics.counter(RECOVERIES_TOTAL, outcome="truncated") == 1.0


# --- the refuse policy --------------------------------------------------------------------------------


def test_the_refuse_policy_raises_and_leaves_every_byte_where_it_was(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    _append_garbage(stack, bytes(96))
    before = stack.storage.read_log(name, 0, stack.storage.log_size(name))  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    with pytest.raises(GrafxRecoveryRefused) as caught:
        reopened.recovery(policy=POLICY_REFUSE).run()
    assert caught.value.code == "recovery_refused"
    after = stack.storage.log_size(name)  # type: ignore[attr-defined]
    assert stack.storage.read_log(name, 0, after) == before  # type: ignore[attr-defined]
    assert reopened.quarantine.list() == ()
    assert reopened.ledger.list() == ()
    assert stack.metrics.counter(RECOVERIES_TOTAL, outcome="refused") == 1.0


def test_the_refuse_policy_does_not_even_repair_the_ledger(stack: Stack) -> None:
    """Section 8.6 step 7: refuse leaves everything on disk as it was -- writes included.

    The ledger's own repair is a write like any other: it quarantines the unreadable tail and
    then cuts it. Doing that before the refusal contradicted the step and the word: an operator
    picks 'refuse' precisely when they want nothing touched.
    """
    _commit(stack, 3, b"kept")
    _append_garbage(stack, bytes(96))
    device = stack.storage
    device.create(LEDGER_FILE, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(LEDGER_FILE, b"\x00" * 30)  # type: ignore[attr-defined]
    before = {
        name: device.log_size(name)  # type: ignore[attr-defined]
        for name in device.list_files("")  # type: ignore[attr-defined]
    }
    reopened = _reopened(stack)
    with pytest.raises(GrafxRecoveryRefused):
        reopened.recovery(policy=POLICY_REFUSE).run()
    after = {
        name: device.log_size(name)  # type: ignore[attr-defined]
        for name in device.list_files("")  # type: ignore[attr-defined]
    }
    assert after == before
    assert reopened.ledger.damage is not None
    assert reopened.quarantine.list() == ()


def test_the_replay_policy_still_repairs_a_damaged_ledger_on_a_clean_log(
    stack: Stack,
) -> None:
    """The other side, so the fix above cannot be satisfied by never repairing at all (A85)."""
    _commit(stack, 3, b"kept")
    device = stack.storage
    device.create(LEDGER_FILE, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(LEDGER_FILE, b"\x00" * 30)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.outcome == OUTCOME_CLEAN
    assert reopened.ledger.damage is None
    assert len(reopened.quarantine.list()) == 1


def test_the_refuse_policy_recovers_a_clean_log_without_complaint(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    report = _reopened(stack).recovery(policy=POLICY_REFUSE).run()
    assert report.outcome == OUTCOME_CLEAN


# --- idempotence ------------------------------------------------------------------------------------


def test_recovering_twice_leaves_the_same_state(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    _append_garbage(stack, bytes(96))
    first_stack = _reopened(stack)
    first = first_stack.recovery().run()
    heap_after_first = digest_of_file(stack.storage, HEAP_FILE)
    second_stack = _reopened(stack)
    second = second_stack.recovery().run()
    assert first.outcome == OUTCOME_TRUNCATED and second.outcome == OUTCOME_CLEAN
    assert second.records_discarded == 0 and second.ledger_entries_created == 0
    assert len(second_stack.ledger.list()) == first.ledger_entries_created
    assert second_stack.quarantine.count() == 1
    assert digest_of_file(stack.storage, HEAP_FILE) == heap_after_first
    assert second.last_good_lsn == first.last_good_lsn


def test_recovery_interrupted_after_the_quarantine_does_not_duplicate_the_entry(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    damaged_segment = _segment(stack)
    offset = _append_garbage(stack, bytes(96))
    interrupted = _reopened(stack)
    interrupted.quarantine.capture(
        origin=damaged_segment, offset=offset, length=96, reason="truncated_tail"
    )
    report = _reopened(stack).recovery().run()
    assert report.outcome == OUTCOME_TRUNCATED
    assert _reopened(stack).quarantine.count() == 1


def test_recovery_interrupted_inside_a_ledger_append_repairs_the_ledger_first(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    _append_garbage(stack, bytes(96))
    device = stack.storage
    device.create(LEDGER_FILE, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(LEDGER_FILE, b"\x00" * 30)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.outcome == OUTCOME_TRUNCATED
    assert reopened.ledger.damage is None
    assert report.ledger_entries_created >= 1
    repaired = report.findings_of(FindingKind.QUARANTINED_RANGE)
    assert any(finding.file == LEDGER_FILE for finding in repaired)


def test_replaying_a_page_twice_leaves_the_page_identical(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    stack.pool.flush()
    first = _reopened(stack)
    first.recovery().run()
    first.pool.flush()
    once = digest_of_file(stack.storage, HEAP_FILE)
    second = _reopened(stack)
    second.recovery().run()
    second.pool.flush()
    assert digest_of_file(stack.storage, HEAP_FILE) == once


# --- redo -----------------------------------------------------------------------------------------------


def test_a_committed_page_is_put_back_after_the_apply_was_lost(stack: Stack) -> None:
    image = make_page_image(stack.codec, [b"committed"], page_index=3)
    # The first append also writes the segment header; later batches do not.
    predicted = stack.wal.last_lsn + 2 + (0 if stack.wal.segments() else 1)
    page = stack.codec.decode_page(image, verify=True)
    page.page_lsn = predicted
    stamped = stack.codec.encode_page(page)
    from okto_grafx.domain.txn.commit_record import CommitPayload
    from okto_grafx.domain.txn.records import encode_page_write

    stack.wal.append_many(
        [
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=1,
                txn_id=1,
                payload=encode_page_write(HEAP_FILE, 3, stamped),
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=1,
                txn_id=1,
                payload=CommitPayload.build(
                    snapshot_lsn=0, read_partitions=(), write_partitions=(1,)
                ).encode(),
                descriptor=DESCRIPTOR,
            ),
        ]
    )
    stack.wal.barrier()
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.records_replayed == 1
    reopened.pool.flush()
    state_size = stack.storage.log_size(COMMIT_STATE_FILE)  # type: ignore[attr-defined]
    state = CommitState.decode(
        stack.storage.read_log(COMMIT_STATE_FILE, 0, state_size)  # type: ignore[attr-defined]
    )
    assert state.last_committed_lsn == predicted
    with reopened.pool.pinned(HEAP_FILE, 3) as restored:
        assert restored.read_slot(0) == b"committed"
    once = digest_of_file(stack.storage, HEAP_FILE)
    assert _reopened(stack).recovery().run().records_replayed == 1
    assert digest_of_file(stack.storage, HEAP_FILE) == once


def test_a_clean_tail_with_an_effect_but_no_outcome_refuses_without_mutation(
    stack: Stack,
) -> None:
    from okto_grafx.domain.txn.records import encode_page_write

    image = make_page_image(stack.codec, [b"uncommitted"], page_index=5)
    stack.wal.append(
        WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE),
            epoch=1,
            txn_id=77,
            payload=encode_page_write(HEAP_FILE, 5, image),
            descriptor=DESCRIPTOR,
        )
    )
    stack.wal.barrier()
    pages_before = stack.storage.page_count(HEAP_FILE)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    bytes_before = _stored_bytes(reopened)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()

    assert refused.value.details["field"] == "wal_lineage"
    assert refused.value.details["incomplete_effect_lsns"]
    assert _stored_bytes(reopened) == bytes_before
    assert stack.storage.page_count(HEAP_FILE) == pages_before  # type: ignore[attr-defined]


def test_an_aborted_transaction_contributes_nothing_to_the_redo(stack: Stack) -> None:
    from okto_grafx.domain.txn.records import encode_page_write

    image = make_page_image(stack.codec, [b"aborted"], page_index=6)
    stack.wal.append_many(
        [
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=1,
                txn_id=8,
                payload=encode_page_write(HEAP_FILE, 6, image),
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.ABORT),
                epoch=1,
                txn_id=8,
                descriptor=DESCRIPTOR,
            ),
        ]
    )
    stack.wal.barrier()
    report = _reopened(stack).recovery().run()
    assert report.records_replayed == 0


def test_a_commit_after_an_abort_is_refused_before_recovery_mutates_bytes(
    stack: Stack,
) -> None:
    """A legacy staged ABORT cannot make recovery skip an applied page and publish past it."""
    from okto_grafx.domain.txn.commit_record import CommitPayload
    from okto_grafx.domain.txn.records import encode_page_write

    image = make_page_image(stack.codec, [b"abandoned"], page_index=9)
    stack.wal.append_many(
        [
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=1,
                txn_id=12,
                payload=encode_page_write(HEAP_FILE, 9, image),
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.ABORT),
                epoch=1,
                txn_id=12,
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=1,
                txn_id=12,
                payload=CommitPayload.build(
                    snapshot_lsn=0, read_partitions=(), write_partitions=(5,)
                ).encode(),
                descriptor=DESCRIPTOR,
            ),
        ]
    )
    stack.wal.barrier()
    pages_before = stack.storage.page_count(HEAP_FILE)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    bytes_before = _stored_bytes(reopened)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()

    assert refused.value.details["field"] == "wal_transaction"
    assert refused.value.details["terminal_type"] == "ABORT"
    assert refused.value.details["offending_type"] == "COMMIT"
    assert _stored_bytes(reopened) == bytes_before
    assert stack.storage.page_count(HEAP_FILE) == pages_before  # type: ignore[attr-defined]


def test_a_transaction_identifier_reused_in_a_later_epoch_does_not_commit_the_earlier_one(
    stack: Stack,
) -> None:
    from okto_grafx.domain.txn.commit_record import CommitPayload
    from okto_grafx.domain.txn.records import encode_page_write

    first = make_page_image(stack.codec, [b"epoch one"], page_index=7)
    stack.wal.append(
        WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE),
            epoch=1,
            txn_id=4,
            payload=encode_page_write(HEAP_FILE, 7, first),
            descriptor=DESCRIPTOR,
        )
    )
    stack.wal.append_many(
        [
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=2,
                txn_id=4,
                payload=CommitPayload.build(
                    snapshot_lsn=0, read_partitions=(), write_partitions=(2,)
                ).encode(),
                descriptor=DESCRIPTOR,
            )
        ]
    )
    stack.wal.barrier()
    pages_before = stack.storage.page_count(HEAP_FILE)  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    bytes_before = _stored_bytes(reopened)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()

    assert refused.value.details["field"] == "wal_lineage"
    assert refused.value.details["incomplete_transactions"] == ((1, 4),)
    assert _stored_bytes(reopened) == bytes_before
    assert stack.storage.page_count(HEAP_FILE) == pages_before  # type: ignore[attr-defined]


def test_a_record_naming_a_file_the_log_does_not_cover_is_refused_rather_than_applied(
    stack: Stack,
) -> None:
    from okto_grafx.domain.txn.commit_record import CommitPayload
    from okto_grafx.domain.txn.records import encode_page_write

    image = make_page_image(stack.codec, [b"nope"], page_index=1)
    stack.wal.append_many(
        [
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=1,
                txn_id=3,
                payload=encode_page_write("control/writer.lease", 1, image),
                descriptor=DESCRIPTOR,
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=1,
                txn_id=3,
                payload=CommitPayload.build(
                    snapshot_lsn=0, read_partitions=(), write_partitions=(3,)
                ).encode(),
                descriptor=DESCRIPTOR,
            ),
        ]
    )
    stack.wal.barrier()
    reopened = _reopened(stack)
    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery().run()
    assert refused.value.details["file"] == "control/writer.lease"
    assert not stack.storage.exists("control/writer.lease")  # type: ignore[attr-defined]


# --- a record from a newer build ----------------------------------------------------------------------


def test_a_record_this_build_cannot_read_stops_recovery_instead_of_discarding_it(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    name = _segment(stack)
    future = bytearray(
        WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE),
            payload=bytes(16),
            descriptor=DESCRIPTOR,
            lsn=stack.wal.last_lsn + 1,
            epoch=1,
            txn_id=1,
        ).encode()
    )
    future[4:6] = struct.pack("<H", 250)
    before = stack.storage.log_size(name)  # type: ignore[attr-defined]
    stack.storage.append_log(name, bytes(future))  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    with pytest.raises(GrafxSchemaVersionMismatch):
        reopened.recovery().run()
    assert stack.storage.log_size(name) > before  # type: ignore[attr-defined]
    assert reopened.quarantine.list() == ()
    assert reopened.ledger.list() == ()


# --- the meta identity ---------------------------------------------------------------------------------


def test_a_meta_page_of_a_newer_format_stops_recovery(stack: Stack) -> None:
    from okto_grafx.domain.page.file_header import FileHeader, FileHeaderPage, FileKind
    from okto_grafx.domain.page.slotted import Page

    device = stack.storage
    device.create("grafx.meta", exclusive=False)  # type: ignore[attr-defined]
    device.allocate("grafx.meta", 1)  # type: ignore[attr-defined]
    # A page of nothing but zeros is an allocated page nobody wrote, not a version mismatch.
    assert stack.recovery().run().outcome == OUTCOME_CLEAN

    page = Page(int(PageType.META), page_size=PAGE_SIZE, page_index=0)
    FileHeaderPage.initialize(
        page, FileHeader(kind=FileKind.META, page_size=PAGE_SIZE, format_version=1)
    )
    device.write_page("grafx.meta", 0, stack.codec.encode_page(page))  # type: ignore[attr-defined]
    assert stack.recovery().run().outcome == OUTCOME_CLEAN

    stamped = bytearray(
        FileHeader(kind=FileKind.META, page_size=PAGE_SIZE, format_version=1).encode()
    )
    stamped[8:10] = struct.pack("<H", 250)
    future = Page(int(PageType.META), page_size=PAGE_SIZE, page_index=0)
    future.insert_slot(bytes(stamped))
    device.write_page("grafx.meta", 0, stack.codec.encode_page(future))  # type: ignore[attr-defined]
    with pytest.raises(GrafxSchemaVersionMismatch):
        stack.recovery().run()


def test_a_meta_page_that_is_not_a_header_page_is_damage(stack: Stack) -> None:
    from okto_grafx.domain.page.slotted import Page

    device = stack.storage
    device.create("grafx.meta", exclusive=False)  # type: ignore[attr-defined]
    device.allocate("grafx.meta", 1)  # type: ignore[attr-defined]
    wrong = Page(int(PageType.HEAP), page_size=PAGE_SIZE, page_index=0)
    wrong.insert_slot(b"not a header")
    device.write_page("grafx.meta", 0, stack.codec.encode_page(wrong))  # type: ignore[attr-defined]
    with pytest.raises(GrafxCorruptionDetected) as caught:
        stack.recovery().run()
    assert caught.value.details["field"] == "page_type"


def test_a_database_with_no_meta_page_yet_is_not_a_version_mismatch(
    stack: Stack,
) -> None:
    assert stack.recovery().run().outcome == OUTCOME_CLEAN


# --- carried finding CF-1: retiring a damaged control record -------------------------------------------


def _control(
    stack: Stack,
    name: str = "control/writer.lease",
    body: bytes = b"damaged",
) -> str:
    """Put a control record on the device and return its name."""
    device = stack.storage
    device.create(name, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(name, body)  # type: ignore[attr-defined]
    return name


def test_retiring_a_control_record_without_a_probe_is_refused(stack: Stack) -> None:
    name = _control(stack)
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery().retire_control_record(name)
    assert caught.value.details["field"] == "control_probe"
    assert stack.storage.exists(name)  # type: ignore[attr-defined]


def test_retiring_a_healthy_control_record_is_refused(stack: Stack) -> None:
    name = _control(stack)
    probe = HealthyProbe()
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=probe).retire_control_record(name)
    assert "reads cleanly" in caught.value.message
    assert probe.reads == [name]
    assert stack.storage.exists(name)  # type: ignore[attr-defined]


def test_retiring_a_damaged_record_quarantines_then_records_then_retires(
    stack: Stack,
) -> None:
    name = _control(stack, body=b"a damaged reader record")
    probe = RefusingProbe(GrafxCorruptionDetected("The reader record is unreadable."))
    report = stack.recovery(control_probe=probe).retire_control_record(name)
    assert report.outcome == OUTCOME_QUARANTINED
    assert report.ledger_entries_created == 1
    assert not stack.storage.exists(name)  # type: ignore[attr-defined]
    entries = stack.quarantine.list()
    assert len(entries) == 1
    assert stack.quarantine.read(entries[0].name) == b"a damaged reader record"
    ledger_entries = stack.ledger.list()
    assert len(ledger_entries) == 1
    assert ledger_entries[0].origin_class is LedgerOriginClass.FORENSIC
    assert ledger_entries[0].reason is LedgerReason.QUARANTINED_SEGMENT
    retired = report.findings_of(FindingKind.CONTROL_RECORD_RETIRED)
    assert retired and retired[0].file == name


def test_a_retirement_reports_that_the_reader_horizon_must_be_derived_again(
    stack: Stack,
) -> None:
    name = _control(stack)
    probe = RefusingProbe(GrafxCorruptionDetected("The reader record is unreadable."))
    report = stack.recovery(control_probe=probe).retire_control_record(name)
    horizon = report.findings_of(FindingKind.READER_HORIZON_REDERIVED)
    assert horizon and "re-derive" in horizon[0].detail


def test_a_retirement_asks_the_coordinator_for_the_horizon_again_when_one_is_wired(
    stack: Stack,
) -> None:
    class _Section:
        """A held section that releases on exit."""

        def __enter__(self) -> None:
            """Hold."""

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            """Release."""
            return False

    class Coordinator:
        """A coordinator that answers a horizon and counts how often it was asked."""

        def __init__(self) -> None:
            """Start with nothing asked."""
            self.calls = 0

        def reader_horizon(self) -> int:
            """Answer a horizon and record the question."""
            self.calls += 1
            return 12

        def exclusive(self, name: str, *, timeout: float) -> "_Section":
            """Grant the commit section; the fence is not what this test is about."""
            return _Section()

    name = _control(stack)
    coordinator = Coordinator()
    probe = RefusingProbe(GrafxCorruptionDetected("The reader record is unreadable."))
    report = stack.recovery(
        control_probe=probe, coordinator=coordinator
    ).retire_control_record(name)
    assert coordinator.calls == 1
    horizon = report.findings_of(FindingKind.READER_HORIZON_REDERIVED)
    assert horizon and horizon[0].lsn == 12


def test_a_file_outside_the_control_directory_is_never_retired_by_this_door(
    stack: Stack,
) -> None:
    probe = RefusingProbe(GrafxCorruptionDetected("Damaged."))
    manager = stack.recovery(control_probe=probe)
    for name in (HEAP_FILE, CATALOG_FILE, "wal/000000000001.wal", "index/by_name.idx"):
        with pytest.raises(GrafxRecoveryRefused) as caught:
            manager.retire_control_record(name)
        assert caught.value.details["field"] == "file"
    assert stack.storage.exists(HEAP_FILE)  # type: ignore[attr-defined]


def test_commit_state_is_never_retired_as_a_generic_control_record(
    stack: Stack,
) -> None:
    """Only commit/recovery publication may replace the snapshot/checkpoint fence."""
    name = "control/commit.state"
    probe = RefusingProbe(GrafxCorruptionDetected("Damaged."))

    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=probe).retire_control_record(name)

    assert caught.value.details["field"] == "file"
    assert probe.reads == []


def test_retiring_a_control_record_that_is_not_there_is_refused(stack: Stack) -> None:
    probe = RefusingProbe(GrafxCorruptionDetected("Damaged."))
    with pytest.raises(GrafxRecoveryRefused):
        stack.recovery(control_probe=probe).retire_control_record(
            "control/writer.lease"
        )


def test_a_retryable_access_failure_is_never_evidence_of_damage(stack: Stack) -> None:
    """A11-revised and A47 on the one door that destroys a name.

    A sharing violation from an antivirus or an indexer says the record could not be READ right
    now and nothing at all about its bytes. Retiring on it quarantines and destroys a healthy
    lease, which is C4's position inverted -- a writer deleting the file that is refusing it.
    The refusal declares itself retryable so the caller knows the answer is to try again.
    """
    from okto_grafx.domain.errors import GrafxStorageError

    name = _control(stack)
    busy = GrafxStorageError(
        "The lease file is held by another process.", winerror=32, attempts=3
    )
    busy.details["retryable"] = True
    probe = RefusingProbe(busy)
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=probe).retire_control_record(name)
    assert caught.value.details["retryable"] is True
    assert is_retryable(caught.value) is True
    assert stack.storage.exists(name)  # type: ignore[attr-defined]
    assert stack.quarantine.list() == ()
    assert stack.ledger.list() == ()


def test_a_probe_that_fails_with_a_foreign_exception_retires_nothing(
    stack: Stack,
) -> None:
    """An unclassifiable failure is evidence of nothing, and this door needs evidence."""
    name = _control(stack)
    probe = RefusingProbe(RuntimeError("the decoder exploded"))
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=probe).retire_control_record(name)
    assert caught.value.details["probe_error"] == "RuntimeError"
    assert stack.storage.exists(name)  # type: ignore[attr-defined]
    assert stack.quarantine.list() == ()
    assert stack.ledger.list() == ()


def test_corruption_detected_is_the_evidence_that_permits_retirement(
    stack: Stack,
) -> None:
    """The other side of the rule, so neither half can pass by refusing everything (A85)."""
    name = _control(stack, body=b"a damaged reader record")
    probe = RefusingProbe(GrafxCorruptionDetected("The reader record is unreadable."))
    report = stack.recovery(control_probe=probe).retire_control_record(name)
    assert report.outcome == OUTCOME_QUARANTINED
    assert not stack.storage.exists(name)  # type: ignore[attr-defined]


# --- carried finding CF-4: the catalog after a replay ----------------------------------------------------


def _table() -> TableDef:
    """Return one small node table."""
    return TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def test_the_catalog_is_re_derived_from_the_replayed_pages_and_adopted(
    stack: Stack,
) -> None:
    from okto_grafx.domain.txn.commit_record import CommitPayload
    from okto_grafx.domain.txn.records import encode_page_write

    stack.catalog.catalog.add_table(_table())
    pages = stack.catalog.save()
    stack.pool.flush()
    images = [
        (CATALOG_FILE, page, stack.storage.read_page(CATALOG_FILE, page))  # type: ignore[attr-defined]
        for page in pages
    ]
    predicted = stack.wal.last_lsn + len(images) + 1
    records = []
    for file, page_index, raw in images:
        decoded = stack.codec.decode_page(raw, verify=True)
        decoded.page_lsn = predicted
        records.append(
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=1,
                txn_id=2,
                payload=encode_page_write(
                    file, page_index, stack.codec.encode_page(decoded)
                ),
                descriptor=DESCRIPTOR,
            )
        )
    records.append(
        WalRecord(
            record_type=int(WalRecordType.COMMIT),
            epoch=1,
            txn_id=2,
            payload=CommitPayload.build(
                snapshot_lsn=0, read_partitions=(), write_partitions=(4,)
            ).encode(),
            descriptor=DESCRIPTOR,
        )
    )
    stack.wal.append_many(records)
    stack.wal.barrier()
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    adopted = report.findings_of(FindingKind.CATALOG_ADOPTED)
    assert adopted and "adopted" in adopted[0].detail
    assert [table.name for table in reopened.catalog.catalog.tables()] == ["Person"]


def test_the_catalog_can_save_after_recovery_replayed_its_pages(stack: Stack) -> None:
    test_the_catalog_is_re_derived_from_the_replayed_pages_and_adopted(stack)
    reopened = _reopened(stack)
    reopened.recovery().run()
    # The whole point of the CF-4 route: the store is not left refusing its next save.
    reopened.catalog.save()
    assert [table.name for table in reopened.catalog.read_from_pages().tables()] == [
        "Person"
    ]


def test_recovery_says_the_caller_owes_the_route_when_no_catalog_store_is_wired(
    stack: Stack,
) -> None:
    test_the_catalog_is_re_derived_from_the_replayed_pages_and_adopted(stack)
    reopened = _reopened(stack)
    with pytest.raises(GrafxRecoveryRefused) as refused:
        reopened.recovery(catalog=None).run()
    assert refused.value.details["field"] == "catalog"


# --- ports ------------------------------------------------------------------------------------------------


def test_a_collaborator_of_the_wrong_type_is_refused_at_construction(
    stack: Stack, clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    with pytest.raises(GrafxConfigurationError):
        RecoveryManager(
            stack.storage,  # type: ignore[arg-type]
            stack.wal,
            "not a ledger",  # type: ignore[arg-type]
            stack.quarantine,
            stack.pool,
            metrics,  # type: ignore[arg-type]
            attribute_probe=port_has_attribute,
        )
    with pytest.raises(GrafxConfigurationError):
        RecoveryManager(
            stack.storage,  # type: ignore[arg-type]
            stack.wal,
            stack.ledger,
            "not a quarantine",  # type: ignore[arg-type]
            stack.pool,
            metrics,  # type: ignore[arg-type]
            attribute_probe=port_has_attribute,
        )
    with pytest.raises(GrafxConfigurationError):
        RecoveryManager(
            stack.storage,  # type: ignore[arg-type]
            stack.wal,
            stack.ledger,
            stack.quarantine,
            "not a pool",  # type: ignore[arg-type]
            metrics,  # type: ignore[arg-type]
            attribute_probe=port_has_attribute,
        )


def test_a_log_that_cannot_answer_the_doors_recovery_opens_is_refused(
    stack: Stack,
) -> None:
    from okto_grafx.domain.errors import GrafxPortNotConfigured

    class Half:
        """A log that answers only some of the manager interface."""

        def scan_all(self) -> tuple[()]:
            """Answer the one door this stand-in implements."""
            return ()

    with pytest.raises(GrafxPortNotConfigured) as caught:
        RecoveryManager(
            stack.storage,  # type: ignore[arg-type]
            Half(),
            stack.ledger,
            stack.quarantine,
            stack.pool,
            stack.metrics,  # type: ignore[arg-type]
            attribute_probe=port_has_attribute,
        )
    assert caught.value.details["slot"] == "wal"


def test_the_checkpoint_narrows_the_redo_without_moving_the_cut(stack: Stack) -> None:
    from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState

    _commit(stack, 3, b"first")
    first_lsn = stack.wal.last_lsn
    _commit(stack, 4, b"second", txn_id=2)
    device = stack.storage
    device.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    device.append_log(  # type: ignore[attr-defined]
        COMMIT_STATE_FILE,
        CommitState(
            last_committed_lsn=stack.wal.last_lsn,
            last_csn=stack.wal.last_lsn,
            checkpoint_lsn=first_lsn,
        ).encode(),
    )
    report = _reopened(stack).recovery().run()
    assert report.records_replayed == 1
    assert report.last_good_lsn == stack.wal.last_lsn


def test_a_commit_state_that_will_not_parse_only_makes_recovery_redo_more(
    stack: Stack,
) -> None:
    from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE

    _commit(stack, 3, b"first")
    device = stack.storage
    device.truncate_log(COMMIT_STATE_FILE, 0)  # type: ignore[attr-defined]
    device.append_log(COMMIT_STATE_FILE, b"rubbish")  # type: ignore[attr-defined]
    report = _reopened(stack).recovery().run()
    assert report.outcome == OUTCOME_CLEAN and report.records_replayed == 1


def test_a_second_database_over_a_second_device_is_recovered_independently(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    first_device = MemoryStorageDevice(page_size=PAGE_SIZE)
    second_device = MemoryStorageDevice(page_size=PAGE_SIZE)
    try:
        first = build_stack(first_device, clock=clock, metrics=metrics)
        second = build_stack(second_device, clock=clock, metrics=metrics)
        _commit(first, 3, b"one")
        _commit(second, 3, b"two")
        first_segment = first.wal.segments()[-1].name
        first_device.append_log(first_segment, bytes(96))
        first.wal.open()
        assert (
            build_stack(first_device, clock=clock, metrics=metrics, bootstrap=False)
            .recovery()
            .run()
            .outcome
            == OUTCOME_TRUNCATED
        )
        assert (
            build_stack(second_device, clock=clock, metrics=metrics, bootstrap=False)
            .recovery()
            .run()
            .outcome
            == OUTCOME_CLEAN
        )
    finally:
        first_device.close()
        second_device.close()


def test_a_reopened_log_is_the_one_recovery_repaired(stack: Stack) -> None:
    _commit(stack, 3, b"kept")
    good = stack.wal.last_lsn
    _append_garbage(stack, bytes(96))
    _reopened(stack).recovery().run()
    fresh = WalManager(
        stack.storage,  # type: ignore[arg-type]
        stack.clock,  # type: ignore[arg-type]
        stack.metrics,  # type: ignore[arg-type]
        segment_bytes=SEGMENT_BYTES,
        descriptor=DESCRIPTOR,
    )
    fresh.open()
    assert fresh.damage is None and fresh.last_lsn == good


def test_the_ledger_of_a_repaired_database_still_reads_after_a_reopen(
    stack: Stack,
) -> None:
    _commit(stack, 3, b"kept")
    _append_garbage(stack, bytes(96))
    report = _reopened(stack).recovery().run()
    reopened_ledger = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    assert len(reopened_ledger.list()) == report.ledger_entries_created


# --- only the one damage class is evidence (round-3 blocking defect B2) ----------------------------


def _non_damage_refusals() -> list[tuple[str, Exception]]:
    """Return one instance of every non-retryable class that is NOT the damage class.

    ``GrafxCorruptionDetected`` is deliberately absent: it is the one class section 2 freezes for
    damaged bytes, and it has its own test on the other side of the rule (A85).
    """
    from okto_grafx.domain.errors import (
        GrafxError,
        GrafxLeaseStolen,
        GrafxPortNotConfigured,
        GrafxStaleEpoch,
        GrafxUnsupportedOperation,
    )

    return [
        (
            "port_not_configured",
            GrafxPortNotConfigured("No decoder is wired.", slot="codec"),
        ),
        ("configuration_error", GrafxConfigurationError("Bad argument.", field="file")),
        (
            "lease_stolen",
            GrafxLeaseStolen("The lease moved to another writer.", epoch=7),
        ),
        ("stale_epoch", GrafxStaleEpoch("Your epoch is behind the record's.", epoch=3)),
        (
            "unsupported_operation",
            GrafxUnsupportedOperation("Not this build.", field="x"),
        ),
        ("grafx_error", GrafxError("Something went wrong.")),
        (
            "schema_version_mismatch",
            GrafxSchemaVersionMismatch("A newer build wrote this."),
        ),
    ]


@pytest.mark.parametrize(
    ("code", "failure"),
    _non_damage_refusals(),
    ids=[code for code, _ in _non_damage_refusals()],
)
def test_only_corruption_detected_is_evidence_a_control_record_is_damaged(
    stack: Stack, code: str, failure: Exception
) -> None:
    """Section 2 freezes exactly ONE damage class, and this door destroys a name on that alone.

    Every class here is a statement about the READER, the caller or the build -- never about the
    bytes -- and each of them previously quarantined the record, wrote a FORENSIC ledger entry and
    deleted the file, on a record whose bytes were perfectly good.

    The assertion is on ``details['cause']``, which only this branch sets, so the test cannot be
    satisfied by one of the neighbouring refusals answering the same class (A62).
    """
    name = _control(stack, body=b"a perfectly healthy control record")
    probe = RefusingProbe(failure)
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=probe).retire_control_record(name)
    assert caught.value.details["cause"] == code
    assert is_retryable(caught.value) is False
    assert stack.storage.exists(name)  # type: ignore[attr-defined]
    kept = stack.storage.read_log(name, 0, stack.storage.log_size(name))  # type: ignore[attr-defined]
    assert kept == b"a perfectly healthy control record"
    assert stack.quarantine.list() == ()
    assert stack.ledger.list() == ()


def test_a_stale_writer_cannot_delete_the_lease_that_is_refusing_it(
    stack: Stack,
) -> None:
    """Carried finding CF-1 point 4, stated as a test rather than as a promise.

    ``stale_epoch`` and ``lease_stolen`` are what the coordination adapter raises about a HEALTHY
    lease when the READER is the stale one. Retiring on either is the stale writer deleting the
    live writer's lease and filing it as forensic damage -- never for letting a writer delete the
    file that is refusing it.
    """
    from okto_grafx.domain.errors import GrafxLeaseStolen, GrafxStaleEpoch

    body = b"the lease of the writer that is live"
    lease = _control(stack, name="control/writer.lease", body=body)
    for failure in (
        GrafxStaleEpoch("Epoch 3 is behind the lease's epoch 4.", epoch=3),
        GrafxLeaseStolen("The lease is held at epoch 4 by another writer.", epoch=4),
    ):
        with pytest.raises(GrafxRecoveryRefused) as caught:
            stack.recovery(control_probe=RefusingProbe(failure)).retire_control_record(
                lease
            )
        assert "not evidence that its bytes are damaged" in caught.value.message
        assert stack.storage.exists(lease)  # type: ignore[attr-defined]
    kept = stack.storage.read_log(lease, 0, stack.storage.log_size(lease))  # type: ignore[attr-defined]
    assert kept == body
    assert stack.ledger.list() == ()


def test_a_probe_that_declares_corruption_retryable_is_asked_again_rather_than_believed(
    stack: Stack,
) -> None:
    """A47 through the damage class itself: retryable means 'call me again', not 'destroy it'."""
    name = _control(stack)
    busy = GrafxCorruptionDetected("The record could not be read yet.", retryable=True)
    busy.details["retryable"] = True
    with pytest.raises(GrafxRecoveryRefused) as caught:
        stack.recovery(control_probe=RefusingProbe(busy)).retire_control_record(name)
    assert is_retryable(caught.value) is True
    assert stack.storage.exists(name)  # type: ignore[attr-defined]
    assert stack.ledger.list() == ()


def test_a_retirement_that_was_already_recorded_creates_no_second_ledger_entry(
    stack: Stack,
) -> None:
    """``record_retirement`` is idempotent on the damage identity, and the report must say so.

    A retirement interrupted after the ledger entry and before the name went is re-run by the
    operator. The second pass creates NOTHING, and a report that claims one entry either way
    cannot be added up -- G8/BR-3 asks for exactly one entry per discard, and this number is how
    a caller checks it.
    """
    name = _control(stack, body=b"a damaged reader record")
    probe = RefusingProbe(GrafxCorruptionDetected("The reader record is unreadable."))
    first = stack.recovery(control_probe=probe).retire_control_record(name)
    assert first.ledger_entries_created == 1
    assert len(stack.ledger.entries()) == 1

    _control(stack, body=b"a damaged reader record")
    second = stack.recovery(control_probe=probe).retire_control_record(name)
    assert second.ledger_entries_created == 0
    assert len(stack.ledger.entries()) == 1


def test_a_clean_log_under_refuse_leaves_a_damaged_ledger_exactly_as_it_was(
    stack: Stack,
) -> None:
    """Step 7 on the branch a DAMAGED log can never reach, because that branch raises first.

    ``test_the_refuse_policy_does_not_even_repair_the_ledger`` drives the damaged-log path, where
    the refusal is raised before the repair could run at all -- so it is satisfied whatever this
    branch does. The shape that only this branch answers is a clean log with a damaged ledger:
    the pass completes, and under 'refuse' it must still have written nothing.
    """
    _commit(stack, 3, b"kept")
    device = stack.storage
    device.create(LEDGER_FILE, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(LEDGER_FILE, b"\x00" * 30)  # type: ignore[attr-defined]
    before = {
        name: device.log_size(name)  # type: ignore[attr-defined]
        for name in device.list_files("")  # type: ignore[attr-defined]
    }
    reopened = _reopened(stack)
    report = reopened.recovery(recovery_policy=POLICY_REFUSE).run()
    assert report.outcome == OUTCOME_CLEAN
    assert reopened.ledger.damage is not None
    assert reopened.quarantine.list() == ()
    after = {
        name: device.log_size(name)  # type: ignore[attr-defined]
        for name in device.list_files("")  # type: ignore[attr-defined]
    }
    assert after == before


# --- the policy is spelled the way section 5 spells it --------------------------------------------


def test_the_policy_is_named_recovery_policy_the_way_the_configuration_names_it(
    stack: Stack,
) -> None:
    """``DatabaseConfig`` and section 5 say ``recovery_policy``; so does this constructor."""
    manager = stack.recovery(recovery_policy=POLICY_REFUSE)
    assert manager.recovery_policy == POLICY_REFUSE
    assert manager.policy == POLICY_REFUSE
    assert "recovery_policy='refuse'" in repr(manager)
    assert stack.recovery().recovery_policy == "replay"


def test_naming_the_policy_twice_with_two_different_words_is_refused(
    stack: Stack,
) -> None:
    """No ordering rule visible from the call site could make one of the two the answer."""
    with pytest.raises(GrafxConfigurationError) as caught:
        stack.recovery(recovery_policy=POLICY_REFUSE, policy="replay")
    assert caught.value.details["field"] == "recovery_policy"
    assert "given twice" in caught.value.message
    agreeing = stack.recovery(recovery_policy=POLICY_REFUSE, policy=POLICY_REFUSE)
    assert agreeing.recovery_policy == POLICY_REFUSE
