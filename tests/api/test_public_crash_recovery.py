"""Crash completion through the public API, including the primary-key access path.

The unit recovery suite can prove that one page image or one logical index record is
idempotent.  These tests prove the composition claim that matters to an application: once a
COMMIT record is durable, a process death before data/index apply and ``commit.state``
publication is completed by the next ``connect`` as one unit.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from okto_grafx import Database, connect
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxError,
    GrafxRecoveryRefused,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.index import index_file
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.txn.commit_record import CommitPayload
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
import okto_grafx.engine.commit_redo as commit_redo_module
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.index_manager import IndexStore, primary_key_index_name
from okto_grafx.engine.ledger_store import LEDGER_FILE
from okto_grafx.runtime.bootstrap import coordinator_settings, install_checksum
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

PAGE_SIZE = 512
TABLE = "P"
INDEX = primary_key_index_name(TABLE)
KEYED_READ = "MATCH (p:P) WHERE p.id = $id RETURN p.name"


class _PersistentPageWriteFailure(FaultInjectingStorageDevice):
    """Refuse every page write until the test explicitly releases the device."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        super().__init__(inner, seed=20260827)
        self.block_page_writes = False

    def write_page(self, file: str, page_index: int, data: bytes) -> None:
        if self.block_page_writes:
            raise GrafxDeviceFull(
                "The test device is persistently refusing page writes.",
                file=file,
                page=page_index,
            )
        super().write_page(file, page_index, data)


class _PersistentCommitStatePublishFailure(FaultInjectingStorageDevice):
    """Let commit effects land, but refuse every publication until released."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        super().__init__(inner, seed=20260829)
        self.block_state_publication = False

    def atomic_replace(self, source: str, target: str) -> None:
        if self.block_state_publication and target == COMMIT_STATE_FILE:
            raise GrafxDeviceFull(
                "The test device is persistently refusing commit-state publication.",
                file=target,
            )
        super().atomic_replace(source, target)


def _registry(
    storage: FaultInjectingStorageDevice, *, namespace: object
) -> PortRegistry:
    """Compose the shipped ports over one reusable fault-injection namespace."""
    config = DatabaseConfig(path=":memory:", page_size=PAGE_SIZE)
    install_checksum(config)
    clock = SystemClock()
    metrics = NoOpMetricsSink()
    registry = PortRegistry()
    registry.bind("storage", storage)
    registry.bind("clock", clock)
    registry.bind("codec", PageCodecV1(PAGE_SIZE))
    registry.bind("metrics", metrics)
    registry.bind("events", LoggingEventSink())
    registry.bind("vector_math", PureVectorMath())
    registry.bind(
        "coordinator",
        LocalProcessCoordinator(
            storage,
            clock,
            namespace=namespace,
            metrics=metrics,
            **coordinator_settings(config),
        ),
    )
    return registry


def _connect(
    storage: FaultInjectingStorageDevice,
    *,
    namespace: object,
    read_only: bool = False,
) -> Database:
    return connect(
        ":memory:",
        page_size=PAGE_SIZE,
        read_only=read_only,
        registry=_registry(storage, namespace=namespace),
    )


def _published(storage: MemoryStorageDevice) -> CommitState:
    size = storage.log_size(COMMIT_STATE_FILE)
    return CommitState.decode(storage.read_log(COMMIT_STATE_FILE, 0, size))


def _operators(database: Database, statement: str) -> set[str]:
    """Return the public explain-plan operator names for ``statement``."""
    found: set[str] = set()
    pending = [database.explain(statement)]
    while pending:
        node = pending.pop()
        found.add(type(node).__name__)
        children = getattr(node, "children", None)
        pending.extend(children() if callable(children) else (children or ()))
    return found


def _heap_images(storage: MemoryStorageDevice) -> tuple[bytes, ...]:
    return tuple(
        storage.read_page("heap.dat", page)
        for page in range(storage.page_count("heap.dat"))
    )


def _recovery_artifacts(storage: MemoryStorageDevice) -> dict[str, bytes]:
    """Snapshot every byte recovery may otherwise publish, cut or preserve here."""
    exact = {
        COMMIT_STATE_FILE,
        "catalog.dat",
        "heap.dat",
        index_file(INDEX),
        LEDGER_FILE,
    }
    prefixes = (f"{COMMIT_STATE_FILE}.", "wal/", "indexes/", "ledger/", "quarantine/")
    names = tuple(
        name
        for name in storage.list_files("")
        if name in exact or name.startswith(prefixes)
    )
    return {
        name: storage.read_log(name, 0, storage.file_size(name)) for name in names
    }


def _durable_row_crash() -> tuple[
    MemoryStorageDevice,
    FaultInjectingStorageDevice,
    Database,
    int,
    int,
]:
    """Leave one row commit durable in WAL but absent from data, index and publication."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=20260824)

    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")

    crashed = _connect(fault, namespace=memory)
    published_before = _published(memory).last_committed_lsn
    txn = crashed.begin("write")
    txn.execute("CREATE (:P {id: 7, name: 'durable'})")

    # The first data-page write belongs to step 3.6.  The WAL append and its barrier have
    # already returned, while neither a heap page nor an index change has reached its file.
    fault.clear_trail()
    fault.crash_on("write_page", occurrence=1, moment="before")
    with pytest.raises(SimulatedCrash) as stopped:
        txn.commit()
    fault.disarm()

    committed = crashed.wal.last_lsn
    assert committed > published_before
    assert _published(memory).last_committed_lsn == published_before
    assert stopped.value.file == "heap.dat"
    assert any(
        call.method == "durable_barrier"
        and call.outcome == "ok"
        and call.file is not None
        and call.file.startswith("wal/")
        and call.sequence < stopped.value.sequence
        for call in fault.trail()
    )

    durable_types = {
        record.record_type for record in crashed._wal.read_from(published_before + 1)
    }
    assert int(WalRecordType.WRITE_PAGE) in durable_types
    assert int(WalRecordType.INDEX_WRITE) in durable_types
    assert int(WalRecordType.COMMIT) in durable_types
    return memory, fault, crashed, published_before, committed


def _rows(database: Database) -> tuple[tuple[object, ...], ...]:
    return database.execute(KEYED_READ, {"id": 7}).rows


def test_reopen_completes_a_durable_commit_into_heap_index_and_publication() -> None:
    memory, fault, _crashed, old_lsn, committed = _durable_row_crash()

    recovered = _connect(fault, namespace=memory)
    try:
        assert recovered.recovery_report.records_replayed > 0
        assert old_lsn < recovered.transactions.published_lsn() == committed
        assert _published(memory).last_committed_lsn == committed
        assert recovered.stale_indexes == ()
        assert "IndexSeek" in _operators(recovered, KEYED_READ)
        assert _rows(recovered) == (("durable",),)

        entries_after_first_recovery = recovered.inspect_index(INDEX)
        assert len(entries_after_first_recovery) == 1
        assert recovered.verify("all").findings == ()

        # A second pass over the same retained WAL must neither duplicate the logical index
        # entry nor move the publication.  It also has to keep the keyed answer stable.
        state_after_first_recovery = _published(memory)
        assert recovered.recover().outcome == "clean"
        assert _published(memory) == state_after_first_recovery
        assert recovered.inspect_index(INDEX) == entries_after_first_recovery
        assert _rows(recovered) == (("durable",),)
        assert recovered.verify("all").findings == ()
    finally:
        recovered.close()

    # A new object graph supplies the final idempotence check: no cached page or index object
    # from the successful recovery can be responsible for the answer.
    with _connect(fault, namespace=memory) as reopened:
        assert _published(memory).last_committed_lsn == committed
        assert reopened.transactions.published_lsn() == committed
        assert reopened.inspect_index(INDEX) == entries_after_first_recovery
        assert "IndexSeek" in _operators(reopened, KEYED_READ)
        assert _rows(reopened) == (("durable",),)
        assert reopened.verify("all").findings == ()


def test_missing_mandatory_index_redo_refuses_publication_and_page_flush() -> None:
    memory, fault, _crashed, old_lsn, committed = _durable_row_crash()
    state_before = _published(memory)
    heap_before = _heap_images(memory)
    assert state_before.last_committed_lsn == old_lsn < committed

    # The catalog still declares the primary-key index, but its file cannot be adopted.  The
    # durable INDEX_WRITE is mandatory work: recovery may not publish the commit after applying
    # only its heap half.
    memory.remove(index_file(INDEX))
    with pytest.raises(GrafxRecoveryRefused) as refused:
        _connect(fault, namespace=memory)

    assert refused.value.details["field"] == "index"
    assert refused.value.details["index"] == INDEX
    assert _published(memory) == state_before
    assert _heap_images(memory) == heap_before


def test_a_late_invalid_effect_preflights_before_stale_or_control_bytes_move() -> (
    None
):
    """A stale-floor verdict cannot persist before the complete redo plan validates."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=20260830)
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        setup.checkpoint()

    database = _connect(fault, namespace=memory)
    try:
        state = _published(memory)
        assert state.checkpoint_lsn > 0
        index = database._indexes.index(INDEX)
        header = index.header
        assert header.built_through_lsn >= state.checkpoint_lsn
        # Make the replay-floor verdict actionable: on the old ordering the first non-pure step
        # persisted INDEX_FLAG_STALE here before the later corrupt page image was decoded.
        with database._pool.pinned(index.file, 0) as page:
            page.update_slot(
                INDEX_HEADER_SLOT,
                replace(header, built_through_lsn=0).encode(),
            )
        database._pool.flush(index.file)

        page_index = memory.page_count("heap.dat")
        valid_page = Page(
            int(PageType.HEAP),
            page_size=PAGE_SIZE,
            page_index=page_index,
            page_lsn=database._wal.last_lsn + 1,
            seq=2,
        )
        valid_page.insert_slot(b"would-be-prefix")
        valid_image = database._codec.encode_page(valid_page)
        epoch = max(
            (record.epoch for record in database._wal.read_from(1)),
            default=database._coordinator.current_epoch(),
        )
        txn_id = 903
        database._wal.append_many(
            (
                WalRecord(
                    record_type=int(WalRecordType.WRITE_PAGE),
                    payload=encode_page_write(
                        "heap.dat", page_index, valid_image
                    ),
                    epoch=epoch,
                    txn_id=txn_id,
                ),
                WalRecord(
                    record_type=int(WalRecordType.WRITE_PAGE),
                    payload=encode_page_write(
                        "heap.dat", page_index + 1, b"not-a-page-image"
                    ),
                    epoch=epoch,
                    txn_id=txn_id,
                ),
                WalRecord(
                    record_type=int(WalRecordType.COMMIT),
                    payload=CommitPayload(
                        snapshot_lsn=state.last_committed_lsn
                    ).encode(),
                    epoch=epoch,
                    txn_id=txn_id,
                ),
            )
        )
        database._wal.barrier()
        before = _recovery_artifacts(memory)

        with pytest.raises(GrafxCorruptionDetected) as refused:
            database.recover()

        assert refused.value.details["file"] == "heap.dat"
        assert refused.value.details["page"] == page_index + 1
        assert _recovery_artifacts(memory) == before
    finally:
        database.close()


def test_a_failed_operator_recovery_cannot_later_certify_a_short_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient logical-redo failure cannot become a later wrong keyed answer.

    The heap half of the unpublished commit is already dirty when index redo fails. A later,
    unrelated commit is allowed either to refuse while recovery is required or to proceed with
    the failed index marked stale. It must never advance that short index through the later
    commit and let a keyed lookup confidently omit the recovered row.
    """
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=20260825)
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")

    operator = _connect(fault, namespace=memory)
    crashed = _connect(fault, namespace=memory)
    state_before = _published(memory)
    txn = crashed.begin("write")
    txn.execute("CREATE (:P {id: 7, name: 'durable'})")
    fault.clear_trail()
    fault.crash_on("write_page", occurrence=1, moment="before")
    with pytest.raises(SimulatedCrash):
        txn.commit()
    fault.disarm()
    unpublished_commit = crashed.wal.last_lsn
    assert state_before.last_committed_lsn < unpublished_commit

    def fail_logical_redo(
        index: IndexStore, change: object, lsn: int
    ) -> bool:
        del change, lsn
        raise GrafxDeviceFull(
            "The transient device fault reached logical index redo.", file=index.file
        )

    with monkeypatch.context() as injected:
        injected.setattr(IndexStore, "_apply_change", fail_logical_redo)
        with pytest.raises(GrafxDeviceFull):
            operator.recover()

    assert _published(memory) == state_before

    try:
        with operator.begin("write") as later:
            later.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
    except GrafxError:
        # A recovery-required/poisoned handle is the strict fail-closed answer.
        assert _published(memory) == state_before
    else:
        # Continuing is also safe when the failed index was conservatively excluded from plans.
        assert _published(memory).last_committed_lsn > unpublished_commit
        assert _rows(operator) == (("durable",),)
        assert operator.execute("MATCH (p:P) RETURN p.name").rows == (("durable",),)


def test_a_failed_checkpoint_page_redo_cannot_be_skipped_by_a_later_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit cannot publish past a durable WAL commit checkpoint failed to install.

    Marking indexes stale is sufficient when heap redo completed and logical redo failed. It is
    not sufficient when page redo itself failed: a fallback scan would also omit the durable row.
    The handle must either require a successful retry before another write or complete the older
    heap effect before the later commit advances publication.
    """
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=20260826)
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")

    checkpointer = _connect(fault, namespace=memory)
    crashed = _connect(fault, namespace=memory)
    state_before = _published(memory)
    txn = crashed.begin("write")
    txn.execute("CREATE (:P {id: 7, name: 'durable'})")
    fault.clear_trail()
    fault.crash_on("write_page", occurrence=1, moment="before")
    with pytest.raises(SimulatedCrash):
        txn.commit()
    fault.disarm()
    unpublished_commit = crashed.wal.last_lsn
    assert state_before.last_committed_lsn < unpublished_commit

    def fail_page_redo(
        pool: object, file: str, page_index: int, image: bytes
    ) -> bool:
        del pool, page_index, image
        raise GrafxDeviceFull(
            "The transient device fault reached page redo.", file=file
        )

    with monkeypatch.context() as injected:
        injected.setattr(commit_redo_module, "apply_page_image", fail_page_redo)
        with pytest.raises(GrafxDeviceFull):
            checkpointer.checkpoint()

    assert _published(memory) == state_before

    try:
        with checkpointer.begin("write") as later:
            later.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
    except GrafxError:
        # A recovery-required handle preserves the old publication until checkpoint is retried.
        assert _published(memory) == state_before
    else:
        # If writes remain available, the older durable commit must have been completed first.
        assert _published(memory).last_committed_lsn > unpublished_commit
        assert _rows(checkpointer) == (("durable",),)
        assert checkpointer.execute("MATCH (p:P) RETURN p.name").rows == (("durable",),)


def test_the_same_handle_requires_recovery_after_its_post_barrier_redo_fails() -> None:
    """A private high-water mark can never become a snapshot over missing page bytes.

    The first commit is durable in WAL, but both its ordinary page flush and its immediate redo
    fail.  A transaction already open in this same participant must not commit over that gap,
    and a newly opened transaction must not take the unpublished high-water mark as its snapshot.
    Once operator recovery succeeds, the original row must survive a later commit to the same
    heap and a checkpoint/reopen cycle.
    """
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = _PersistentPageWriteFailure(memory)
    database = _connect(fault, namespace=memory)
    try:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        database.checkpoint()

        waiting = database.begin("write")
        waiting.execute("CREATE (:P {id: 8, name: 'after'})")

        durable = database.begin("write")
        durable.execute("CREATE (:P {id: 7, name: 'before'})")
        fault.block_page_writes = True
        with pytest.raises(GrafxError) as failed_apply:
            durable.commit()
        fault.block_page_writes = False
        assert failed_apply.value.details["committed"] is True

        with pytest.raises(GrafxError) as blocked_statement:
            waiting.execute("MATCH (p:P) RETURN p.name")
        assert blocked_statement.value.details["field"] == "recovery_required"

        with pytest.raises(GrafxError) as blocked_commit:
            waiting.commit()
        assert blocked_commit.value.details["field"] == "recovery_required"
        waiting.rollback()

        with pytest.raises(GrafxError) as blocked_begin:
            database.begin("read")
        assert blocked_begin.value.details["field"] == "recovery_required"

        with pytest.raises(GrafxError) as blocked_checkpoint:
            database.checkpoint()
        assert blocked_checkpoint.value.details["field"] == "recovery_required"

        with pytest.raises(GrafxError) as blocked_flush:
            database.flush()
        assert blocked_flush.value.details["field"] == "recovery_required"

        with pytest.raises(GrafxError) as blocked_verify:
            database.verify("pages")
        assert blocked_verify.value.details["field"] == "recovery_required"

        recovered = database.recover()
        assert recovered.records_replayed > 0
        assert database.execute(KEYED_READ, {"id": 7}).rows == (("before",),)

        with database.begin("write") as later:
            later.execute("CREATE (:P {id: 8, name: 'after'})")
        database.checkpoint()
        assert database.execute(KEYED_READ, {"id": 7}).rows == (("before",),)
        assert database.execute(KEYED_READ, {"id": 8}).rows == (("after",),)
    finally:
        database.close()

    with _connect(fault, namespace=memory) as reopened:
        assert reopened.execute(KEYED_READ, {"id": 7}).rows == (("before",),)
        assert reopened.execute(KEYED_READ, {"id": 8}).rows == (("after",),)


def test_flush_cannot_race_a_post_barrier_recovery_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page operation either finishes before the latch or refuses without reaching the pool."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = _PersistentCommitStatePublishFailure(memory)
    database = _connect(fault, namespace=memory)
    try:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        database.checkpoint()

        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 7, name: 'durable'})")

        original_flush = BufferPool.flush
        flush_entered = threading.Event()
        release_flush = threading.Event()
        flush_done = threading.Event()
        commit_done = threading.Event()
        flush_calls: list[tuple[str, str | None]] = []
        flush_results: list[int] = []
        flush_failures: list[BaseException] = []
        commit_failures: list[BaseException] = []

        def paused_flush(pool: BufferPool, file: str | None = None) -> int:
            flush_calls.append((threading.current_thread().name, file))
            if pool is database._pool and threading.current_thread().name == "public-flush":
                flush_entered.set()
                if not release_flush.wait(timeout=5.0):
                    raise AssertionError("the test did not release the public flush")
            return original_flush(pool, file)

        def run_flush() -> None:
            try:
                flush_results.append(database.flush())
            except BaseException as failure:
                flush_failures.append(failure)
            finally:
                flush_done.set()

        def run_commit() -> None:
            try:
                transaction.commit()
            except BaseException as failure:
                commit_failures.append(failure)
            finally:
                commit_done.set()

        monkeypatch.setattr(BufferPool, "flush", paused_flush)
        flush_thread = threading.Thread(target=run_flush, name="public-flush")
        flush_thread.start()
        assert flush_entered.wait(timeout=5.0)

        fault.block_state_publication = True
        commit_thread = threading.Thread(target=run_commit, name="post-barrier-commit")
        commit_thread.start()

        # The flush owns the participant section. The commit cannot append/barrier or set the
        # latch until that earlier page operation has completely left the pool.
        assert not commit_done.wait(timeout=0.2)
        # WAL and transaction publication observations join the participant section as well, so
        # they intentionally wait behind this paused flush instead of reading straddled state;
        # commit_done is the non-blocking evidence.

        release_flush.set()
        flush_thread.join(timeout=5.0)
        commit_thread.join(timeout=5.0)
        assert flush_done.is_set() and commit_done.is_set()
        assert flush_failures == []
        assert len(flush_results) == 1
        assert len(commit_failures) == 1
        assert isinstance(commit_failures[0], GrafxError)
        assert commit_failures[0].details["committed"] is True  # type: ignore[union-attr]
        assert database.transactions.recovery_required is True

        calls_after_latch = len(flush_calls)
        with pytest.raises(GrafxRecoveryRefused) as blocked:
            database.flush()
        assert blocked.value.details["field"] == "recovery_required"
        assert len(flush_calls) == calls_after_latch
    finally:
        fault.block_state_publication = False
        database.close()


def test_an_already_open_participant_completes_a_foreign_gap_before_same_page_write() -> None:
    """A later full-page image cannot permanently replace an unapplied durable commit."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = _PersistentPageWriteFailure(memory)
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        setup.checkpoint()

    later_writer = _connect(fault, namespace=memory)
    failed_writer = _connect(fault, namespace=memory)
    try:
        state_before = _published(memory)
        durable = failed_writer.begin("write")
        durable.execute("CREATE (:P {id: 7, name: 'before'})")
        fault.block_page_writes = True
        with pytest.raises(GrafxError) as failed_apply:
            durable.commit()
        fault.block_page_writes = False
        assert failed_apply.value.details["committed"] is True
        foreign_commit = failed_writer.wal.last_lsn
        assert _published(memory) == state_before

        # This participant predates the foreign COMMIT and therefore begins from the old
        # publication. Its first attempt must complete that COMMIT and then conflict on the heap
        # page, instead of publishing a replacement image over the missing row.
        stale = later_writer.begin("write")
        stale.execute("CREATE (:P {id: 8, name: 'after'})")
        with pytest.raises(GrafxWriteConflict):
            stale.commit()
        stale.rollback()
        assert _published(memory).last_committed_lsn == foreign_commit
        assert later_writer.execute(KEYED_READ, {"id": 7}).rows == (("before",),)

        with later_writer.begin("write") as retry:
            retry.execute("CREATE (:P {id: 8, name: 'after'})")
        later_writer.checkpoint()
        assert later_writer.execute(KEYED_READ, {"id": 7}).rows == (("before",),)
        assert later_writer.execute(KEYED_READ, {"id": 8}).rows == (("after",),)
    finally:
        failed_writer.close()
        later_writer.close()

    with _connect(fault, namespace=memory) as reopened:
        assert reopened.execute(KEYED_READ, {"id": 7}).rows == (("before",),)
        assert reopened.execute(KEYED_READ, {"id": 8}).rows == (("after",),)


@pytest.mark.parametrize(
    "lost_commit", ["checksum", "truncate_exact_commit", "checksum_first_effect"]
)
def test_public_recovery_refuses_effects_whose_commit_outcome_was_lost(
    lost_commit: str,
) -> None:
    """Applied effects cannot become a ghost row under a later global watermark.

    The row's heap and primary-key effects reach storage, but publishing its COMMIT fails. If
    the only copy of that COMMIT is then damaged or disappears exactly at its boundary, recovery
    has no UNDO record and cannot prove whether those physical effects landed. It must leave all
    relevant bytes untouched and prevent a normal reopen from publishing later work over them.
    """
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = _PersistentCommitStatePublishFailure(memory)
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        setup.checkpoint()

    failed = _connect(fault, namespace=memory)
    state_before = _published(memory)
    heap_before = _heap_images(memory)
    index_name = index_file(INDEX)
    index_before = memory.read_log(index_name, 0, memory.file_size(index_name))
    transaction = failed.begin("write")
    transaction.execute("CREATE (:P {id: 7, name: 'orphan'})")
    fault.block_state_publication = True
    try:
        with pytest.raises(GrafxError) as publish_failure:
            transaction.commit()
    finally:
        fault.block_state_publication = False

    assert publish_failure.value.details["committed"] is True
    assert _published(memory) == state_before
    assert _heap_images(memory) != heap_before
    assert memory.read_log(index_name, 0, memory.file_size(index_name)) != index_before

    commit_item = next(
        item
        for item in failed._wal.scan_all()
        if item.record is not None
        and item.record.lsn == failed.wal.last_lsn
        and item.record.record_type == int(WalRecordType.COMMIT)
    )
    if lost_commit == "checksum_first_effect":
        effect_item = next(
            item
            for item in failed._wal.scan_all()
            if item.record is not None
            and item.record.lsn > state_before.last_committed_lsn
            and item.record.record_type == int(WalRecordType.WRITE_PAGE)
        )
        size = memory.file_size(effect_item.segment)
        segment = bytearray(memory.read_log(effect_item.segment, 0, size))
        checksum_byte = effect_item.offset + effect_item.record.encoded_length() - 1
        segment[checksum_byte] ^= 0xFF
        memory.truncate_log(effect_item.segment, 0)
        memory.append_log(effect_item.segment, bytes(segment))
    elif lost_commit == "checksum":
        size = memory.file_size(commit_item.segment)
        segment = bytearray(memory.read_log(commit_item.segment, 0, size))
        segment[-1] ^= 0xFF
        memory.truncate_log(commit_item.segment, 0)
        memory.append_log(commit_item.segment, bytes(segment))
    else:
        memory.truncate_log(commit_item.segment, commit_item.offset)
    memory.durable_barrier(commit_item.segment)
    failed.close()

    before_recovery = _recovery_artifacts(memory)
    assert not any(name.startswith(("ledger/", "quarantine/")) for name in before_recovery)

    # Two ordinary opens model the attempted later writer B: neither may obtain a Database from
    # which it could advance the watermark, and a refusal is a strictly byte-identical decision.
    for _attempt in range(2):
        with pytest.raises(GrafxRecoveryRefused) as refused:
            _connect(fault, namespace=memory)
        assert refused.value.details["field"] == "wal_lineage"
        assert refused.value.details["incomplete_effect_lsns"]
        assert refused.value.details["incomplete_transactions"]
        assert _recovery_artifacts(memory) == before_recovery
        assert _published(memory) == state_before


def test_read_only_open_refuses_an_applied_ddl_effect_with_no_outcome() -> None:
    """Read-only bootstrap must not adopt an ambiguously applied catalog page."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = _PersistentCommitStatePublishFailure(memory)
    with _connect(fault, namespace=memory) as setup:
        setup.checkpoint()

    failed = _connect(fault, namespace=memory)
    state_before = _published(memory)
    catalog_before = memory.read_log(
        "catalog.dat", 0, memory.file_size("catalog.dat")
    )
    transaction = failed.begin("write")
    transaction.execute(
        "CREATE NODE TABLE Ambiguous(id INT64, name STRING, PRIMARY KEY(id))"
    )
    fault.block_state_publication = True
    try:
        with pytest.raises(GrafxError) as publish_failure:
            transaction.commit()
    finally:
        fault.block_state_publication = False

    assert publish_failure.value.details["committed"] is True
    assert _published(memory) == state_before
    assert (
        memory.read_log("catalog.dat", 0, memory.file_size("catalog.dat"))
        != catalog_before
    )

    commit_item = next(
        item
        for item in failed._wal.scan_all()
        if item.record is not None
        and item.record.lsn == failed.wal.last_lsn
        and item.record.record_type == int(WalRecordType.COMMIT)
    )
    memory.truncate_log(commit_item.segment, commit_item.offset)
    memory.durable_barrier(commit_item.segment)
    failed.close()

    before_read_only = _recovery_artifacts(memory)
    with pytest.raises(GrafxUnsupportedOperation) as refused:
        _connect(fault, namespace=memory, read_only=True)

    assert refused.value.details["field"] == "read_only_consistency"
    assert refused.value.details["incomplete_effect_lsns"]
    assert _recovery_artifacts(memory) == before_read_only
    assert _published(memory) == state_before


def test_public_connect_quarantines_a_torn_ledger_tail_and_opens() -> None:
    """The composition root gives LedgerStore the quarantine used by startup recovery."""
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault = FaultInjectingStorageDevice(memory, seed=20260828)
    with _connect(fault, namespace=memory):
        pass

    torn = b"\x00" * 30
    memory.create(LEDGER_FILE, exclusive=False)
    memory.append_log(LEDGER_FILE, torn)

    with _connect(fault, namespace=memory) as reopened:
        entries = reopened.quarantine.list()
        assert reopened.recovery_report.outcome == "clean"
        assert reopened.ledger.damage is None
        assert memory.log_size(LEDGER_FILE) == 0
        assert len(entries) == 1
        assert entries[0].manifest.origin == LEDGER_FILE
        assert reopened.read_quarantine(entries[0].name) == torn


REL_TABLE = "R"
"""A relationship table, so a crash can be aimed at a statement that ends more than one row."""


def _surviving(database: Database) -> tuple[int, ...]:
    """Return the node identities a reader can still see, ordered."""
    return tuple(sorted(row[0] for row in database.execute("MATCH (p:P) RETURN p.id").rows))


def _live_edges(database: Database) -> tuple[tuple[object, object], ...]:
    """Return the endpoints of every relationship version still live on the pages, ordered.

    Walked rather than queried, because the join needs BOTH endpoints: an edge whose source is
    gone drops out of a traversal whether it was ended with the node or merely left behind, and
    telling those two apart is the whole claim here.
    """
    definition = database.catalog.catalog.table(REL_TABLE)
    return tuple(
        sorted(
            (version.values[0], version.values[1])
            for _ref, version in database._heap.scan_all(definition)
            if version.live
        )
    )


class _CrashBeforeTheLogAccepts(FaultInjectingStorageDevice):
    """Stop the process before the WAL accepts anything, leaving nothing to replay.

    Aimed by FILE, not by call number. The commit path appends to control files as well, and the
    first ``append_log`` of a commit turned out to be a writer lease -- so an occurrence count
    would have crashed somewhere else entirely while still looking like a WAL cut.
    """

    def __init__(self, inner: MemoryStorageDevice) -> None:
        super().__init__(inner, seed=20260826)
        self.armed = False

    def append_log(self, file: str, payload: bytes) -> int:
        if self.armed and file.startswith("wal/"):
            raise SimulatedCrash(
                f"The test device stopped the process before the WAL accepted {file!r}.",
                sequence=len(self.trail()),
                method="append_log",
                file=file,
                moment="before",
            )
        return super().append_log(file, payload)


def _detach_crash(
    aim: str,
) -> tuple[MemoryStorageDevice, FaultInjectingStorageDevice, int, SimulatedCrash]:
    """Cut one DETACH DELETE off at one write point and report where it stopped.

    The victim carries one outgoing and one incoming edge, so the statement ends three rows in
    three different states of the same commit -- which is what makes "all or none" a claim with
    something to say.
    """
    memory = MemoryStorageDevice(page_size=PAGE_SIZE)
    fault: FaultInjectingStorageDevice = (
        _CrashBeforeTheLogAccepts(memory)
        if aim == "log"
        else FaultInjectingStorageDevice(memory, seed=20260826)
    )
    with _connect(fault, namespace=memory) as setup:
        with setup.begin("write") as txn:
            txn.execute(f"CREATE NODE TABLE {TABLE}(id INT64, name STRING, PRIMARY KEY(id))")
            txn.execute(f"CREATE REL TABLE {REL_TABLE}(FROM {TABLE} TO {TABLE}, w INT64)")
        with setup.begin("write") as txn:
            for identity in (1, 2, 3):
                txn.execute(f"CREATE (:{TABLE} {{id: $i, name: 'n'}})", {"i": identity})
        with setup.begin("write") as txn:
            for source, target in ((1, 2), (3, 1)):
                txn.execute(
                    f"MATCH (a:{TABLE}), (b:{TABLE}) WHERE a.id = $a AND b.id = $b "
                    f"CREATE (a)-[:{REL_TABLE} {{w: 1}}]->(b)",
                    {"a": source, "b": target},
                )

    crashed = _connect(fault, namespace=memory)
    published_before = _published(memory).last_committed_lsn
    txn = crashed.begin("write")
    txn.execute(f"MATCH (p:{TABLE}) WHERE p.id = 1 DETACH DELETE p")
    fault.clear_trail()
    if aim == "log":
        fault.armed = True
    else:
        fault.crash_on(aim, occurrence=1, moment="before")
    with pytest.raises(SimulatedCrash) as stopped:
        txn.commit()
    if aim == "log":
        fault.armed = False
    else:
        fault.disarm()
    return memory, fault, published_before, stopped.value


def test_a_crashed_detach_delete_publishes_the_node_with_its_edges_or_neither() -> None:
    """Where the process died decides WHETHER the statement lands, never WHICH PART of it does.

    A detach ends a node and the relationships hanging from it as one statement. Half of that
    surviving a crash is the shape that cannot be allowed: a node gone with its edges still
    standing is the orphan the detach exists to prevent, and edges gone with the node still
    standing is a graph quietly missing relationships nobody deleted.
    """
    # Cut AFTER the COMMIT record is durable: the next open completes all three ends.
    memory, fault, before, stopped = _detach_crash("write_page")
    assert stopped.file == "heap.dat"
    assert _published(memory).last_committed_lsn == before  # not published yet, but durable
    with _connect(fault, namespace=memory) as recovered:
        assert recovered.recovery_report.records_replayed > 0
        assert _published(memory).last_committed_lsn > before
        assert _surviving(recovered) == (2, 3)
        assert _live_edges(recovered) == ()  # BOTH incident edges came with the node
        assert recovered.verify("all").findings == ()

    # Cut BEFORE anything of the statement reached the log: none of the three ends exists.
    memory, fault, before, stopped = _detach_crash("log")
    assert stopped.file is not None and stopped.file.startswith("wal/")
    with _connect(fault, namespace=memory) as reopened:
        assert _published(memory).last_committed_lsn == before
        assert _surviving(reopened) == (1, 2, 3)
        # The node kept BOTH of its relationships, not one of them.
        assert len(_live_edges(reopened)) == 2
        assert reopened.verify("all").findings == ()
