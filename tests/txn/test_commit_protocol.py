"""The frozen commit protocol, step by step (CONTRACT.md section 8.5; SPEC-M1 FR-5, BR-4, BR-7).

The steps are observed on the call trail of the fault-injecting device and on a coordinator that
writes down what it was asked. Asserting the OUTCOME of a commit would pass for a commit that
acknowledged before its barrier, which is the one thing BR-4 exists to forbid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxDeviceFull,
    GrafxIndexError,
    GrafxRecoveryRefused,
    GrafxStaleEpoch,
    GrafxStorageError,
    GrafxTransactionStateError,
)
from okto_grafx.domain.txn import (
    page_partition,
    COMMIT_STATE_FILE,
    WalRecordType,
    CommitPayload,
    CommitState,
    TransactionState,
    decode_page_write,
)
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.buffer_pool import apply_page_image
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.txn_manager import (
    ACTIVE_TRANSACTIONS,
    TRANSACTION_MANAGER_METRICS,
    TransactionManager,
)
from okto_grafx.engine.vector_engine import VectorHnswIndex
from tests.vector.conftest import SnapshotDouble, VectorFixture
from shared_device import SharedDirectoryDevice
from txn_support import (
    DEFAULT_PAGE_SIZE,
    WAL_FILE,
    Stack,
    TracingCoordinator,
    build_stack,
    make_page_image,
    page_lsn_of,
    read_page_payloads,
)

HEAP = "heap.dat"


def _stage(stack: Stack, page_index: int = 3, payload: bytes = b"row") -> object:
    """Open a write transaction that changes one page of the heap."""
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, page_index, make_page_image(stack.codec, [payload], page_index=page_index))
    txn.note_write(stack.manager.partition_of(1, payload))
    return txn


def _poisonable_vector(stack: Stack) -> tuple[VectorFixture, VectorHnswIndex]:
    """Return a real vector registry with a published graph over its own device."""
    database = VectorFixture(metrics=stack.metrics, clock=stack.clock)
    space = database.create_space("poison", 4)
    table = database.create_table("Chunk", space.name)
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=1)
    index = database.engine.index(space.name)
    index.snapshot()
    return database, index


def _fault_stack(root: Path, **overrides: object) -> tuple[Stack, FaultInjectingStorageDevice]:
    """Build a participant whose every device call is recorded and can be interrupted."""
    device = FaultInjectingStorageDevice(
        LocalStorageDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=7
    )
    stack = build_stack(root, storage=device, **overrides)  # type: ignore[arg-type]
    return stack, device


# --- step order -------------------------------------------------------------------------------


def test_no_page_is_written_before_the_log_barrier_returns(database_root: Path) -> None:
    """BR-4 and AC-11: durability is proved by ORDER, not by the outcome of the commit."""
    stack, device = _fault_stack(database_root)
    txn = _stage(stack)
    device.clear_trail()
    stack.manager.commit(txn)
    methods = [(record.method, record.file) for record in device.trail()]
    barrier = next(
        index
        for index, (method, file) in enumerate(methods)
        if method == "durable_barrier" and file == WAL_FILE
    )
    appends = [index for index, (method, file) in enumerate(methods) if method == "append_log" and file == WAL_FILE]
    writes = [index for index, (method, _file) in enumerate(methods) if method == "write_page"]
    assert appends, "the commit appended nothing to the log"
    assert max(appends) < barrier
    assert writes and min(writes) > barrier


def test_the_published_state_is_replaced_only_after_every_page_is_in_place(
    database_root: Path,
) -> None:
    """The whole of cross-process snapshot isolation: publish LAST, so a snapshot is complete."""
    stack, device = _fault_stack(database_root)
    txn = stack.manager.begin("write")
    for page_index in (3, 4, 5):
        txn.owner._stage_page_image(txn,
            HEAP, page_index, make_page_image(stack.codec, [b"row"], page_index=page_index)
        )
    txn.note_write(stack.manager.partition_of(1, b"row"))
    device.clear_trail()
    stack.manager.commit(txn)
    methods = [(record.method, record.file, record.args_summary) for record in device.trail()]
    publish = next(
        index
        for index, (method, _file, summary) in enumerate(methods)
        if method == "atomic_replace" and COMMIT_STATE_FILE in summary
    )
    writes = [index for index, (method, file, _s) in enumerate(methods) if method == "write_page" and file == HEAP]
    assert len(writes) >= 3
    assert max(writes) < publish


def test_commit_reuses_the_state_read_inside_the_section_before_publishing(
    database_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CQ-1: one state read preserves the checkpoint and publication stays under commit."""
    stack = build_stack(database_root)
    trail: list[str] = []
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        TracingCoordinator(stack.coordinator, trail),
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"row"))

    reads = 0
    original_read = CommitStateStore.read
    original_publish = CommitStateStore.publish

    def counted_read(store: CommitStateStore) -> CommitState:
        nonlocal reads
        reads += 1
        return original_read(store)

    def traced_publish(store: CommitStateStore, state: CommitState) -> None:
        trail.append("publish:commit.state")
        original_publish(store, state)

    monkeypatch.setattr(CommitStateStore, "read", counted_read)
    monkeypatch.setattr(CommitStateStore, "publish", traced_publish)
    trail.clear()
    manager.commit(txn)

    assert reads == 1
    assert trail.index("enter:commit") < trail.index("publish:commit.state")
    assert trail.index("publish:commit.state") < trail.index("leave:commit")


def test_the_epoch_is_validated_before_any_byte_reaches_the_device(database_root: Path) -> None:
    """Step 2 and BR-7: the guard runs before the commit section, not inside the writes."""
    trail: list[str] = []
    stack, device = _fault_stack(database_root)
    stack.manager  # the stack is built; wrap its coordinator for the manager under test
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        TracingCoordinator(stack.coordinator, trail),
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"row"))
    trail.clear()
    device.clear_trail()
    manager.commit(txn)
    assert trail.index("validate_epoch") < trail.index("enter:commit")
    assert trail.count("validate_epoch") >= 2, "step 3.1 re-validates inside the section"
    inside = trail[trail.index("enter:commit") : trail.index("leave:commit")]
    assert "validate_epoch" in inside


def test_the_lease_is_released_only_after_the_commit_section_is_left(
    database_root: Path,
) -> None:
    """Lock order: nothing here holds the commit section while asking for the lease section."""
    trail: list[str] = []
    stack, _device = _fault_stack(database_root)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        TracingCoordinator(stack.coordinator, trail),
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"row"))
    trail.clear()
    manager.commit(txn)
    assert trail.index("acquire_writer_lease") < trail.index("enter:commit")
    assert trail.index("leave:commit") < trail.index("release_lease")


def test_a_takeover_inside_the_commit_window_refuses_before_the_first_byte(
    database_root: Path,
) -> None:
    """AC-6 and TS-6: a holder of the previous epoch is refused with nothing on the device."""
    stack, device = _fault_stack(database_root)
    successor = build_stack(database_root, storage=device, owner_id="participant-b")

    def steal() -> None:
        # C3 refuses to steal a lease whose owner is still moving, so the successor observes the
        # record once and then finds it unchanged after its own stall threshold has passed. Its
        # clock is its own: nothing about a monotonic reading crosses a process boundary.
        successor.coordinator.detect_dead_owner(stall_threshold=5.0)
        successor.clock.advance(30.0)
        successor.coordinator.takeover()

    trail: list[str] = []
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        TracingCoordinator(stack.coordinator, trail, on_section=steal),
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"row"], page_index=3))
    txn.note_write(manager.partition_of(1, b"row"))
    before = stack.storage.log_size(WAL_FILE)
    device.clear_trail()
    with pytest.raises(GrafxStaleEpoch):
        manager.commit(txn)
    assert stack.storage.log_size(WAL_FILE) == before
    assert not [record for record in device.trail() if record.method == "append_log" and record.file == WAL_FILE]
    assert not [record for record in device.trail() if record.method == "write_page"]


# --- what a commit leaves behind ---------------------------------------------------------------


def test_a_committed_page_holds_the_bytes_and_the_commit_number(stack: Stack) -> None:
    txn = _stage(stack, page_index=4, payload=b"committed")
    report = stack.manager.commit(txn)
    assert report.wrote is True and report.durable is True
    assert read_page_payloads(stack.pool, HEAP, 4) == (b"committed",)
    assert page_lsn_of(stack.pool, HEAP, 4) == report.csn


def test_the_logged_image_carries_the_commit_number_so_a_replay_can_apply_it(
    stack: Stack,
) -> None:
    """A logged image stamped with zero would be refused by its own redo rule and lost."""
    txn = _stage(stack, page_index=4, payload=b"redoable")
    report = stack.manager.commit(txn)
    writes = [record for record in stack.wal.records() if record.record_type == WalRecordType.WRITE_PAGE]
    assert len(writes) == 1
    written = decode_page_write(writes[0].payload)
    assert written.file == HEAP and written.page_index == 4
    replayed = stack.codec.decode_page(written.image, verify=True)
    assert replayed.page_lsn == report.csn


def test_the_commit_record_carries_the_sets_that_were_validated(stack: Stack) -> None:
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 6, make_page_image(stack.codec, [b"x"], page_index=6))
    txn.note_read(stack.manager.partition_of(1, b"a"))
    txn.note_write(stack.manager.partition_of(2, b"b"))
    report = stack.manager.commit(txn)
    commits = [record for record in stack.wal.records() if record.record_type == WalRecordType.COMMIT]
    assert len(commits) == 1 and commits[0].lsn == report.csn
    payload = CommitPayload.decode(commits[0].payload)
    assert payload.snapshot_lsn == 0
    assert payload.read_partitions == (stack.manager.partition_of(1, b"a"),)
    # The write set carries the row partition the caller declared AND the page this commit
    # overwrote: a page image replaces the whole page, so the page is part of what this commit
    # claims and part of what another participant must be able to conflict against (E1).
    assert stack.manager.partition_of(2, b"b") in payload.write_partitions
    assert page_partition(HEAP, 6) in payload.write_partitions
    assert [touch.page_index for touch in payload.page_touches] == [6]


def test_the_published_state_names_the_commit_and_keeps_the_checkpoint(stack: Stack) -> None:
    """The checkpoint belongs to whoever checkpoints; a commit must not reset it."""
    stack.manager.commit(_stage(stack, page_index=3))
    published = stack.manager.published_state()
    stack.storage.remove(COMMIT_STATE_FILE)
    stack.storage.create(COMMIT_STATE_FILE)
    stack.storage.append_log(
        COMMIT_STATE_FILE,
        CommitState(
            last_committed_lsn=published.last_committed_lsn,
            last_csn=published.last_csn,
            checkpoint_lsn=1,
        ).encode(),
    )
    report = stack.manager.commit(_stage(stack, page_index=7, payload=b"second"))
    after = stack.manager.published_state()
    assert after.last_committed_lsn == report.csn
    assert after.last_csn == report.csn
    assert after.checkpoint_lsn == 1


def test_the_published_lsn_never_goes_backwards_across_commits(stack: Stack) -> None:
    seen = [stack.manager.published_lsn()]
    for index in range(4):
        stack.manager.commit(_stage(stack, page_index=3 + index, payload=bytes([index])))
        seen.append(stack.manager.published_lsn())
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


# --- the read-only path (step 1) ----------------------------------------------------------------


def test_a_read_transaction_commits_without_touching_the_device(database_root: Path) -> None:
    stack, device = _fault_stack(database_root)
    stack.manager.commit(_stage(stack, page_index=3))
    txn = stack.manager.begin("read")
    device.clear_trail()
    report = stack.manager.commit(txn)
    assert report.wrote is False
    assert report.durable is True
    assert report.csn == txn.snapshot.read_lsn
    assert not [
        record
        for record in device.trail()
        if (record.method == "append_log" and record.file == WAL_FILE)
        or record.method == "write_page"
    ]


def test_a_write_transaction_that_staged_nothing_writes_no_commit_record(stack: Stack) -> None:
    """A record whose only content is that nothing happened is a record nobody can use."""
    before = len(stack.wal.records())
    txn = stack.manager.begin("write")
    txn.note_read(stack.manager.partition_of(1, b"a"))
    report = stack.manager.commit(txn)
    assert report.wrote is False
    assert len(stack.wal.records()) == before


def test_a_caller_cannot_stage_a_checksum_valid_physical_page(stack: Stack) -> None:
    """Encoded heap/catalog bytes are an engine capability, never a public mutation API."""
    txn = stack.manager.begin("write")
    image = make_page_image(stack.codec, [b"caller-authored"], page_index=7)

    with pytest.raises(GrafxConfigurationError) as raised:
        txn.stage_page_image(HEAP, 7, image)

    assert raised.value.details["field"] == "page_image_provenance"
    assert txn.page_images == {}
    stack.manager.rollback(txn)


def test_direct_page_map_mutation_is_refused_before_wal_or_publication(stack: Stack) -> None:
    """The observable context map carries no authority without a matching private proof."""
    txn = stack.manager.begin("write")
    txn.page_images[(HEAP, 7)] = make_page_image(
        stack.codec, [b"caller-authored"], page_index=7
    )
    txn.note_write(stack.manager.partition_of(1, b"caller-authored"))
    before_records = tuple(stack.wal.records())
    before_state = stack.manager.published_state()

    with pytest.raises(GrafxConfigurationError) as raised:
        stack.manager.commit(txn)

    assert raised.value.details["field"] == "page_image_provenance"
    assert tuple(stack.wal.records()) == before_records
    assert stack.manager.published_state() == before_state
    stack.manager.rollback(txn)


def test_replacing_a_privately_staged_image_breaks_its_proof(stack: Stack) -> None:
    """A proof seals the bytes as well as the file/page key."""
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(
        txn,
        HEAP,
        7,
        make_page_image(stack.codec, [b"trusted"], page_index=7),
    )
    txn.page_images[(HEAP, 7)] = make_page_image(
        stack.codec, [b"replacement"], page_index=7
    )
    before_records = tuple(stack.wal.records())

    with pytest.raises(GrafxConfigurationError) as raised:
        stack.manager.commit(txn)

    assert raised.value.details["field"] == "page_image_provenance"
    assert tuple(stack.wal.records()) == before_records
    stack.manager.rollback(txn)


def test_a_read_transaction_cannot_stage_work(stack: Stack) -> None:
    txn = stack.manager.begin("read")
    with pytest.raises(GrafxTransactionStateError):
        txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(stack.codec, [b"x"], page_index=3))
    with pytest.raises(GrafxTransactionStateError):
        txn.note_write(1)


# --- rollback ------------------------------------------------------------------------------------


def test_a_rollback_leaves_no_trace_anywhere(database_root: Path) -> None:
    """FR-2 and section 8.6 step 6: nothing of an open transaction ever reached a data file."""
    stack, device = _fault_stack(database_root)
    stack.manager.commit(_stage(stack, page_index=3, payload=b"first"))
    log_before = stack.storage.log_size(WAL_FILE)
    state_before = stack.manager.published_state()
    page_before = read_page_payloads(stack.pool, HEAP, 3)
    txn = _stage(stack, page_index=3, payload=b"never")
    device.clear_trail()
    stack.manager.rollback(txn)
    assert stack.storage.log_size(WAL_FILE) == log_before
    assert stack.manager.published_state() == state_before
    assert read_page_payloads(stack.pool, HEAP, 3) == page_before
    assert not [
        record
        for record in device.trail()
        if (record.method == "append_log" and record.file == WAL_FILE)
        or record.method == "write_page"
    ]
    assert txn.state is TransactionState.ABORTED


def test_a_rolled_back_transaction_holds_nothing(stack: Stack) -> None:
    """Everything the transaction accumulated is dropped, not only the parts that were durable."""
    txn = _stage(stack)
    stack.manager.rollback(txn)
    assert txn.page_images == {}
    assert txn.write_partitions == set()
    assert txn.read_partitions == set()
    assert txn.pending_records == []
    assert txn.row_intents == []
    assert txn.row_refs == []


def test_rolling_back_twice_is_a_no_op(stack: Stack) -> None:
    txn = _stage(stack)
    stack.manager.rollback(txn)
    stack.manager.rollback(txn)
    assert txn.state is TransactionState.ABORTED


def test_a_committed_transaction_cannot_be_rolled_back(stack: Stack) -> None:
    txn = _stage(stack)
    stack.manager.commit(txn)
    with pytest.raises(GrafxTransactionStateError):
        stack.manager.rollback(txn)


def test_a_committed_transaction_cannot_be_committed_again(stack: Stack) -> None:
    txn = _stage(stack)
    stack.manager.commit(txn)
    with pytest.raises(GrafxTransactionStateError):
        stack.manager.commit(txn)


def test_closing_abandons_every_open_transaction(stack: Stack) -> None:
    first = _stage(stack, page_index=3)
    second = stack.manager.begin("read")
    stack.manager.close()
    assert first.state is TransactionState.ABORTED
    assert second.state is TransactionState.ABORTED
    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None


# --- amendment A74: the coordinator that granted the lease -----------------------------------


def test_a_transaction_cannot_be_committed_through_another_manager(
    make_stack: object,
) -> None:
    """A74: the lease that authorises a commit belongs to ONE coordinator."""
    first = make_stack()  # type: ignore[operator]
    second = make_stack()  # type: ignore[operator]
    txn = first.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(first.codec, [b"x"], page_index=3))
    txn.note_write(first.manager.partition_of(1, b"x"))
    with pytest.raises(GrafxTransactionStateError) as raised:
        second.manager.commit(txn)
    assert raised.value.details["txn_id"] == txn.txn_id


def test_a_transaction_cannot_be_rolled_back_through_another_manager(
    make_stack: object,
) -> None:
    first = make_stack()  # type: ignore[operator]
    second = make_stack()  # type: ignore[operator]
    txn = first.manager.begin("read")
    with pytest.raises(GrafxTransactionStateError):
        second.manager.rollback(txn)


def test_something_that_is_not_a_transaction_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError):
        stack.manager.commit(object())  # type: ignore[arg-type]


# --- failure on the way through ------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("foreign adapter failure"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_any_escape_after_partial_committed_redo_latches_the_handle(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """A non-Grafx exception class cannot make an applied prefix safe to publish over."""
    vectors, index = _poisonable_vector(stack)
    stack.manager._index_manager = vectors.registry  # noqa: SLF001 - explicit composition seam
    image = make_page_image(
        stack.codec,
        [b"applied-prefix"],
        page_index=4,
        page_lsn=1,
    )

    def fail_after_one_page(
        manager: TransactionManager, checkpoint: int, through: int
    ) -> int:
        assert manager is stack.manager
        assert (checkpoint, through) == (0, 1)
        assert apply_page_image(stack.pool, HEAP, 4, image) is True
        raise failure

    monkeypatch.setattr(
        TransactionManager,
        "_redo_onto_device_unchecked",
        fail_after_one_page,
    )

    with pytest.raises(type(failure)) as escaped:
        stack.manager._redo_onto_device(0, 1)

    assert escaped.value is failure
    with stack.pool.pinned(HEAP, 4) as page:
        assert tuple(payload for _slot, payload in page.iter_slots()) == (
            b"applied-prefix",
        )
    assert stack.manager.recovery_required is True
    assert index.stale
    assert index._snapshot is None  # noqa: SLF001 - proves the derived graph was retired
    with pytest.raises(GrafxIndexError) as refused:
        index.search((1.0, 0.0, 0.0, 0.0), 1, SnapshotDouble(10))
    assert refused.value.details["field"] == "stale"
    with pytest.raises(GrafxRecoveryRefused) as blocked:
        stack.manager.published_lsn()
    assert blocked.value.details["field"] == "recovery_required"


def test_a_foreign_escape_after_gap_redo_latches_before_it_repropagates(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer durable-gap door also latches failures that occur after redo returned."""
    descriptor = "hash-v1;partitions_per_table=8"
    stack.wal.append(
        WalRecord(
            record_type=int(WalRecordType.COMMIT),
            descriptor=descriptor,
            epoch=1,
            txn_id=91,
        )
    )
    image = make_page_image(
        stack.codec,
        [b"completed-prefix"],
        page_index=4,
        page_lsn=1,
    )

    def apply_gap(
        manager: TransactionManager, checkpoint: int, through: int
    ) -> int:
        assert manager is stack.manager
        assert (checkpoint, through) == (0, 1)
        assert apply_page_image(stack.pool, HEAP, 4, image) is True
        return 1

    failure = RuntimeError("publication callback failed")

    def fail_publication(manager: TransactionManager, state: CommitState) -> None:
        assert manager is stack.manager
        assert state.last_committed_lsn == 1
        assert stack.wal.barriers == 1, "gap completion must barrier before publication"
        raise failure

    monkeypatch.setattr(TransactionManager, "_redo_onto_device", apply_gap)
    monkeypatch.setattr(TransactionManager, "_publish", fail_publication)

    with pytest.raises(RuntimeError) as escaped:
        stack.manager._complete_committed_gap()

    assert escaped.value is failure
    with stack.pool.pinned(HEAP, 4) as page:
        assert tuple(payload for _slot, payload in page.iter_slots()) == (
            b"completed-prefix",
        )
    assert stack.manager.recovery_required is True
    with pytest.raises(GrafxRecoveryRefused) as blocked:
        stack.manager.published_lsn()
    assert blocked.value.details["field"] == "recovery_required"


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("gap barrier failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_a_visible_commit_cannot_be_redone_or_published_without_a_gap_barrier(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    stack.wal.append(
        WalRecord(
            record_type=int(WalRecordType.COMMIT),
            descriptor="hash-v1;partitions_per_table=8",
            epoch=1,
            txn_id=92,
        )
    )
    calls = 0

    def fail_barrier() -> None:
        nonlocal calls
        calls += 1
        raise failure

    def forbidden(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("redo/publication ran before WAL durability was established")

    monkeypatch.setattr(stack.wal, "barrier", fail_barrier)
    monkeypatch.setattr(TransactionManager, "_redo_onto_device", forbidden)
    monkeypatch.setattr(TransactionManager, "_publish", forbidden)

    with pytest.raises(type(failure)) as escaped:
        stack.manager._complete_committed_gap()

    assert escaped.value is failure
    assert calls == 1
    assert stack.manager.recovery_required is True
    assert stack.manager._read_commit_state() == CommitState()


def test_a_full_device_during_the_append_leaves_no_page_and_no_publication(
    database_root: Path,
) -> None:
    """AC-10: the transaction aborts and nothing partial is visible afterwards."""
    stack, device = _fault_stack(database_root)
    stack.manager.commit(_stage(stack, page_index=3, payload=b"first"))
    before_state = stack.manager.published_state()
    before_page = read_page_payloads(stack.pool, HEAP, 4) if stack.storage.page_count(HEAP) > 4 else ()
    txn = _stage(stack, page_index=4, payload=b"never")
    device.clear_trail()
    # Lease acquisition writes its atomic-replace temporary first; the second append is WAL.
    device.fill_device_on("append_log", 2)
    with pytest.raises(GrafxDeviceFull):
        stack.manager.commit(txn)
    device.disarm()
    assert stack.manager.published_state() == before_state
    if stack.storage.page_count(HEAP) > 4:
        assert read_page_payloads(stack.pool, HEAP, 4) == before_page
    assert txn.state is TransactionState.ACTIVE


@pytest.mark.parametrize(
    "cleanup_failure",
    (RuntimeError("index cleanup failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_foreign_unstage_cleanup_never_replaces_the_append_failure_and_latches(
    database_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: BaseException,
) -> None:
    stack, device = _fault_stack(database_root)
    txn = _stage(stack, page_index=4, payload=b"never")
    cleanup_calls: list[object] = []

    def fail_index_cleanup(
        manager: TransactionManager, cleaned: object
    ) -> int:
        assert manager is stack.manager
        assert cleaned is txn
        cleanup_calls.append(cleaned)
        raise cleanup_failure

    monkeypatch.setattr(TransactionManager, "_drop_index_changes", fail_index_cleanup)
    device.clear_trail()
    # Lease acquisition writes its atomic-replace temporary first; the second append is WAL.
    device.fill_device_on("append_log", 2)

    with pytest.raises(GrafxDeviceFull) as escaped:
        stack.manager.commit(txn)

    device.disarm()
    assert isinstance(escaped.value, GrafxDeviceFull)
    assert cleanup_calls == [txn]
    assert stack.manager.recovery_required is True
    with pytest.raises(GrafxRecoveryRefused):
        stack.manager.begin("write")


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("lease release failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_lease_release_escape_happens_only_after_committed_outcome_is_settled(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    txn = _stage(stack, page_index=4, payload=b"committed-before-cleanup")
    coordinator_type = type(stack.coordinator)
    original = coordinator_type.release_lease
    observed_states: list[TransactionState] = []

    def release_then_fail(coordinator: object, lease: object) -> None:
        original(coordinator, lease)  # type: ignore[arg-type]
        if coordinator is stack.coordinator:
            observed_states.append(txn.state)
            raise failure

    monkeypatch.setattr(coordinator_type, "release_lease", release_then_fail)

    report = stack.manager.commit(txn)

    assert report.durable is True and report.wrote is True
    assert observed_states == [TransactionState.COMMITTED]
    assert txn.state is TransactionState.COMMITTED
    assert txn.commit_csn == stack.manager.published_state().last_committed_lsn
    assert stack.manager.open_transactions == 0
    assert stack.manager.recovery_required is False


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("reader close failed"), KeyboardInterrupt()),
    ids=("runtime-error", "keyboard-interrupt"),
)
def test_reader_close_escape_cannot_leave_a_committed_transaction_active(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    txn = stack.manager.begin("read")
    coordinator_type = type(stack.coordinator)
    original = coordinator_type.unregister_reader
    observed_states: list[TransactionState] = []

    def close_then_fail(coordinator: object, handle: object) -> None:
        original(coordinator, handle)  # type: ignore[arg-type]
        if coordinator is stack.coordinator:
            observed_states.append(txn.state)
            raise failure

    monkeypatch.setattr(coordinator_type, "unregister_reader", close_then_fail)

    report = stack.manager.commit(txn)

    assert report.durable is True and report.wrote is False
    assert txn.state is TransactionState.COMMITTED
    assert stack.manager.open_transactions == 0
    assert observed_states == [], "CE-2 keeps the participant pin until manager close"

    with pytest.raises(type(failure)) as raised:
        stack.manager.close()
    assert raised.value is failure
    assert observed_states == [TransactionState.COMMITTED]
    assert stack.manager.closed is True
    assert stack.manager.close_quiesced is True
    assert stack.manager.close_complete is True
    stack.manager.close()


def test_a_failure_after_the_barrier_is_reported_as_already_committed(
    database_root: Path,
) -> None:
    """A durable commit must never be reported in a way that invites the caller to redo it."""
    stack, device = _fault_stack(database_root)
    txn = _stage(stack, page_index=4, payload=b"durable")
    device.clear_trail()
    device.fill_device_on("write_page", 1)
    with pytest.raises(GrafxDeviceFull) as raised:
        stack.manager.commit(txn)
    device.disarm()
    assert raised.value.details["committed"] is True
    assert raised.value.details["retryable"] is False
    assert raised.value.retryable is False
    assert raised.value.details["csn"] == txn.commit_csn
    assert txn.state is TransactionState.COMMITTED
    assert stack.manager.recovery_required is True
    with pytest.raises(GrafxRecoveryRefused) as blocked:
        stack.manager.published_lsn()
    assert blocked.value.details["field"] == "recovery_required"


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("foreign post-barrier apply failure"),
        KeyboardInterrupt("interrupted post-barrier apply"),
        SystemExit("terminated post-barrier apply"),
    ),
    ids=("runtime-error", "keyboard-interrupt", "system-exit"),
)
def test_foreign_post_barrier_apply_escape_cannot_hide_the_one_durable_commit(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """RuntimeError is typed; KI/SystemExit keep identity, all forbid retry after one COMMIT."""
    txn = _stage(stack, page_index=4, payload=b"durable-before-foreign-apply")

    def fail_apply(
        manager: TransactionManager,
        _images: object,
    ) -> None:
        assert manager is stack.manager
        assert stack.wal.barriers == 1
        raise failure

    monkeypatch.setattr(TransactionManager, "_apply_images", fail_apply)

    if isinstance(failure, Exception):
        with pytest.raises(GrafxTransactionStateError) as escaped:
            stack.manager.commit(txn)
        assert escaped.value.__cause__ is failure
        assert escaped.value.retryable is False
        assert escaped.value.details == {
            "operation": "commit",
            "committed": True,
            "csn": txn.commit_csn,
            "durable": True,
            "recovery_required": True,
            "original_type": "RuntimeError",
            "retryable": False,
        }
    else:
        with pytest.raises(type(failure)) as escaped:
            stack.manager.commit(txn)
        assert escaped.value is failure
        note = "\n".join(getattr(failure, "__notes__", ()))
        assert f"commit {txn.commit_csn} is already durable" in note
        assert "durable=True" in note
        assert "recovery_required=True" in note
        assert "do not retry" in note

    commits = tuple(
        record
        for record in stack.wal.records()
        if record.record_type == WalRecordType.COMMIT
    )
    assert len(commits) == 1
    assert commits[0].txn_id == txn.txn_id
    assert commits[0].lsn == txn.commit_csn
    assert txn.state is TransactionState.COMMITTED
    assert stack.manager.open_transactions == 0
    assert stack.manager.recovery_required is True


# --- metrics ---------------------------------------------------------------------------------------


def test_the_manager_registers_only_names_from_the_frozen_catalog(stack: Stack) -> None:
    names = {descriptor.name for descriptor in TRANSACTION_MANAGER_METRICS}
    assert names == {
        "oktografx_write_conflicts_total",
        "oktografx_commit_retries_total",
        "oktografx_active_transactions",
    }
    assert names <= {descriptor.name for descriptor in stack.metrics.registered}


def test_the_open_transaction_gauge_rises_and_falls(stack: Stack) -> None:
    first = stack.manager.begin("read")
    second = stack.manager.begin("read")
    assert stack.metrics.gauge_values(ACTIVE_TRANSACTIONS, "mode", "read")[-1] == 2.0
    stack.manager.commit(first)
    stack.manager.rollback(second)
    assert stack.metrics.gauge_values(ACTIVE_TRANSACTIONS, "mode", "read")[-1] == 0.0


def test_a_disabled_sink_is_never_asked_to_record_anything(database_root: Path) -> None:
    """DoD item 6: this component's hot path pays nothing when the no-op sink is selected.

    The assertion names the three metrics of CONTRACT.md section 9 that C5 owns rather than
    asking whether the sink saw anything at all. The sink is shared with every other component
    of the database, and what THEY do with it is their contract to keep: an assertion that read
    the whole sink would go red the day a sibling registered a descriptor, which is a fact about
    that sibling and not about this hot path.
    """
    from txn_support import RecordingMetricsSink

    mine = {descriptor.name for descriptor in TRANSACTION_MANAGER_METRICS}
    metrics = RecordingMetricsSink(enabled=False)
    stack = build_stack(database_root, metrics=metrics)
    stack.manager.commit(_stage(stack, page_index=3))
    stack.manager.rollback(stack.manager.begin("read"))
    assert [entry for entry in metrics.counters if entry[0] in mine] == []
    assert [entry for entry in metrics.gauges if entry[0] in mine] == []
    assert [
        descriptor for descriptor in metrics.registered if descriptor.name in mine
    ] == []


class _RewoundLog:
    """A log that hands a fresh commit a sequence number the database has already published.

    Two real shapes produce it. A truncation by recovery can leave the log reaching less far
    than the published state; and carried finding CF-6 is the sharper one -- a log holding its
    segment index in memory from ``open()`` gives two participants the SAME number, so the
    second commit is numbered at or below what the first already published.

    Either way the commit must be refused before the barrier. Publishing it would move the
    published number backwards or sideways onto a commit that already owns it, and a reader
    taking a snapshot afterwards would be reading a database that had forgotten a commit it
    confirmed.
    """

    def __init__(self, inner: object, rewind_to: int) -> None:
        self._inner = inner
        self._rewind_to = rewind_to
        self._used = False
        self.barriers = 0

    @property
    def last_lsn(self) -> int:
        """Report a sequence number below the published one, exactly once."""
        if not self._used:
            return self._rewind_to
        return self._inner.last_lsn  # type: ignore[attr-defined]

    def planned_terminal_lsn(self, records: object) -> int:
        """Preview the same rewound terminal the append will report."""
        return self._rewind_to + len(list(records))  # type: ignore[arg-type]

    def append_many(
        self, records: object, *, expected_terminal_lsn: int | None = None
    ) -> int:
        """Assign numbers from the rewound position, as a re-used segment index would."""
        self._used = True
        assigned = self._rewind_to + len(list(records))  # type: ignore[arg-type]
        assert expected_terminal_lsn in (None, assigned)
        return assigned

    def barrier(self) -> None:
        """Count the barrier, so a test can prove one was never taken."""
        self.barriers += 1

    def force_barrier_range(self, first_lsn: int, through_lsn: int) -> tuple[str, ...]:
        """Delegate gap durability to the real log underneath."""
        return self._inner.force_barrier_range(  # type: ignore[attr-defined,no-any-return]
            first_lsn, through_lsn
        )

    def read_from(self, lsn: int) -> object:
        """Answer from the real log underneath."""
        return self._inner.read_from(lsn)  # type: ignore[attr-defined]


def test_a_commit_numbered_at_or_below_the_published_one_is_refused_before_the_barrier(
    database_root: Path,
) -> None:
    """Follow-up P3, and defence in depth for carried finding CF-6.

    A fresh commit is appended after everything the log holds, so its number is above the
    published one by construction. A log that says otherwise is numbering two commits the same,
    and the comparison turns that from a silent overwrite into a typed refusal -- taken BEFORE
    the barrier, so nothing was made durable and nothing was acknowledged.
    """
    stack = build_stack(database_root)
    stack.manager.commit(_stage(stack, page_index=3, payload=b"first"))
    stack.manager.commit(_stage(stack, page_index=4, payload=b"second"))
    published = stack.manager.commit(_stage(stack, page_index=5, payload=b"third"))
    assert stack.manager.published_lsn() == published.csn
    log = _RewoundLog(stack.wal, rewind_to=0)
    manager = TransactionManager(
        log,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 9, make_page_image(stack.codec, [b"rewound"], page_index=9))
    txn.note_write(manager.partition_of(1, b"rewound"))
    with pytest.raises(GrafxTransactionStateError) as raised:
        manager.commit(txn)
    assert raised.value.details["field"] == "csn"
    assert raised.value.details["published_lsn"] == published.csn
    assert raised.value.details["value"] <= published.csn
    assert log.barriers == 0, "the refusal must come before anything is made durable"
    assert manager.recovery_required is True
    with pytest.raises(GrafxRecoveryRefused) as blocked:
        manager.published_lsn()
    assert blocked.value.details["field"] == "recovery_required"
    assert stack.manager.published_lsn() == published.csn
    assert txn.state is TransactionState.ACTIVE


def test_an_ordinary_commit_passes_the_forward_number_check(stack: Stack) -> None:
    """The other side of the guard, so it cannot be satisfied by refusing everything (A85)."""
    first = stack.manager.commit(_stage(stack, page_index=3, payload=b"a"))
    second = stack.manager.commit(_stage(stack, page_index=4, payload=b"b"))
    assert second.csn > first.csn
    assert stack.manager.published_lsn() == second.csn



class _StaleSizeDevice:
    """A device that answers one size question about one file with a stale, larger number.

    That is what a reader sees when another participant replaces the published state between the
    two calls a read takes: the size belongs to the record that was there, the bytes to the one
    that is there now. It is a benign race and not damage, and the read has to ride it out.

    It is armed by the test rather than from birth, so nothing fires while the stack is still
    being assembled.
    """

    def __init__(self, inner: object, file: str) -> None:
        self._inner = inner
        self._file = file
        self.armed = False
        self.inflated = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        """Return the label of the device underneath."""
        return self._inner.name  # type: ignore[attr-defined]

    @property
    def page_size(self) -> int:
        """Return the page size of the device underneath."""
        return self._inner.page_size  # type: ignore[attr-defined]

    def log_size(self, file: str) -> int:
        """Answer one armed question about the watched file with a size that is too large."""
        real = self._inner.log_size(file)  # type: ignore[attr-defined]
        if self.armed and file == self._file:
            self.armed = False
            self.inflated += 1
            return real + 16
        return real


def test_a_published_state_replaced_between_two_reads_is_re_read_not_reported_as_damage(
    database_root: Path,
) -> None:
    """The size and the bytes come from two calls, and something can land between them.

    Reporting corruption here would take a database down over a race that resolves itself, and
    the code that resolves it is the re-read: without it the very first attempt raises.
    """
    device = _StaleSizeDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE), COMMIT_STATE_FILE
    )
    stack = build_stack(database_root, storage=device)
    report = stack.manager.commit(_stage(stack, page_index=3, payload=b"raced"))
    device.armed = True
    assert stack.manager.published_lsn() == report.csn
    assert device.inflated == 1, "the stale size was never served, so nothing was ridden out"


class _FlakyReadDevice:
    """A device that refuses one read of one file with a transient, retryable failure.

    This is the antivirus and indexer condition amendment A11-revised describes: the bytes are
    fine, the access was not, and the classification travels in ``details["retryable"]`` rather
    than in the class of the exception (A28, A47).
    """

    def __init__(self, inner: object, file: str, *, retryable: bool) -> None:
        self._inner = inner
        self._file = file
        self._retryable = retryable
        self.armed = False
        self.refusals = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        """Return the label of the device underneath."""
        return self._inner.name  # type: ignore[attr-defined]

    @property
    def page_size(self) -> int:
        """Return the page size of the device underneath."""
        return self._inner.page_size  # type: ignore[attr-defined]

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Refuse one armed read of the watched file, then behave."""
        if self.armed and file == self._file:
            self.armed = False
            self.refusals += 1
            raise GrafxStorageError(
                "The device refused this read for this probe.",
                file=file,
                retryable=self._retryable,
            )
        return self._inner.read_log(file, offset, length)  # type: ignore[attr-defined]


def test_a_transient_refusal_reading_the_published_state_is_tried_again(
    database_root: Path,
) -> None:
    """A47: the retry decision reads the classification in the details, not the class."""
    device = _FlakyReadDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE),
        COMMIT_STATE_FILE,
        retryable=True,
    )
    stack = build_stack(database_root, storage=device)
    report = stack.manager.commit(_stage(stack, page_index=3, payload=b"flaky"))
    device.armed = True
    assert stack.manager.published_lsn() == report.csn
    assert device.refusals == 1, "the refusal never fired, so nothing was ridden out"


def test_a_permanent_refusal_reading_the_published_state_is_reported(
    database_root: Path,
) -> None:
    """The other side of the same predicate: a failure that says it is permanent is not retried."""
    device = _FlakyReadDevice(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE),
        COMMIT_STATE_FILE,
        retryable=False,
    )
    stack = build_stack(database_root, storage=device)
    stack.manager.commit(_stage(stack, page_index=3, payload=b"permanent"))
    device.armed = True
    with pytest.raises(GrafxStorageError) as raised:
        stack.manager.published_lsn()
    assert raised.value.details["file"] == COMMIT_STATE_FILE
    assert device.refusals == 1


class _LeaseCounter:
    """A device that counts how often the writer lease record is republished."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.publishes = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        """Return the label of the device underneath."""
        return self._inner.name  # type: ignore[attr-defined]

    @property
    def page_size(self) -> int:
        """Return the page size of the device underneath."""
        return self._inner.page_size  # type: ignore[attr-defined]

    def atomic_replace(self, source: str, target: str) -> None:
        """Count a republication of the lease record, then let it happen."""
        if "writer.lease" in target:
            self.publishes += 1
        self._inner.atomic_replace(source, target)  # type: ignore[attr-defined]


def test_a_retained_lease_is_published_once_and_not_per_commit(
    database_root: Path,
) -> None:
    """The lease is 4 of the 9 barriers a commit takes, and it is the one that need not repeat.

    Section 8.5 step 2 requires the epoch to be VALIDATED before any device call, and amendment
    A74 requires that validation to go through the coordinator that granted the lease. Neither
    says the lease must be acquired again for each commit. Holding it removes two publications
    and four barriers per commit; measured 9 barriers to 5 on this bench.
    """
    device = _LeaseCounter(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE)
    )
    stack = build_stack(database_root, storage=device, retain_lease=True)
    for page in (3, 4, 5, 6):
        txn = stack.manager.begin("write")
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(stack.codec, [bytes([page])], page_index=page)
        )
        txn.note_write(stack.manager.partition_of(1, bytes([page])))
        assert stack.manager.commit(txn).wrote is True
    assert device.publishes <= 2, (
        f"a retained lease republishes once, not per commit; saw {device.publishes}"
    )


def test_the_default_publishes_the_lease_for_every_commit(database_root: Path) -> None:
    """The other side, so the option cannot be satisfied by doing nothing either way."""
    device = _LeaseCounter(
        SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE)
    )
    stack = build_stack(database_root, storage=device)
    for page in (3, 4, 5, 6):
        txn = stack.manager.begin("write")
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(stack.codec, [bytes([page])], page_index=page)
        )
        txn.note_write(stack.manager.partition_of(1, bytes([page])))
        stack.manager.commit(txn)
    assert device.publishes >= 8, (
        f"the default acquires and releases per commit; saw {device.publishes}"
    )


def test_a_retained_lease_keeps_one_epoch_across_commits(database_root: Path) -> None:
    """Every grant that changes ownership mints an epoch; holding one stops the churn."""
    stack = build_stack(database_root, retain_lease=True)
    epochs = []
    for page in (3, 4, 5):
        txn = stack.manager.begin("write")
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(stack.codec, [bytes([page])], page_index=page)
        )
        txn.note_write(stack.manager.partition_of(1, bytes([page])))
        stack.manager.commit(txn)
        epochs.append(txn.epoch)
    assert len(set(epochs)) == 1, f"a retained lease keeps one epoch; saw {epochs}"


def test_closing_gives_a_retained_lease_back(database_root: Path) -> None:
    """A retained lease outliving its manager makes every other participant wait out the stall."""
    holder = build_stack(database_root, retain_lease=True, owner_id="holder")
    txn = holder.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(holder.codec, [b"x"], page_index=3))
    txn.note_write(holder.manager.partition_of(1, b"x"))
    holder.manager.commit(txn)
    holder.manager.close()

    successor = build_stack(database_root, owner_id="successor")
    other = successor.manager.begin("write")
    other.owner._stage_page_image(other, HEAP, 4, make_page_image(successor.codec, [b"y"], page_index=4))
    other.note_write(successor.manager.partition_of(2, b"y"))
    assert successor.manager.commit(other).wrote is True


def test_the_default_still_lets_two_participants_take_turns(make_stack) -> None:
    """FR-3 process half: with the default, a second participant is never locked out.

    This is why holding the lease is not the default. A participant holds ONE lease and renews
    it, so it never looks stalled and a second participant waiting for it times out. The default
    gives the lease back after every commit, which is what lets disjoint writers take turns.
    """
    first = make_stack()
    second = make_stack()
    for index, participant in enumerate((first, second, first, second)):
        page = 10 + index
        txn = participant.manager.begin("write")
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(participant.codec, [bytes([index])], page_index=page)
        )
        txn.note_write(participant.manager.partition_of(index + 1, bytes([index])))
        assert participant.manager.commit(txn).wrote is True


def test_a_policy_flag_that_is_not_a_flag_is_refused(database_root: Path) -> None:
    from okto_grafx.domain.errors import GrafxConfigurationError

    with pytest.raises(GrafxConfigurationError) as raised:
        build_stack(database_root, retain_lease="yes")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "retain_lease"
