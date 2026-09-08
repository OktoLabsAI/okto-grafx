"""Native journal recovery: real durable WAL, crash subsets and publication fences."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxRecoveryRefused, GrafxTransactionBudgetExceeded
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry, CommitKind
from okto_grafx.domain.txn.commit_identity import CommitId, assign_commit_time
from okto_grafx.domain.txn.commit_record import CommitPayload
from okto_grafx.domain.txn.records import encode_page_write_record
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.commit_catalog_store import (
    COMMIT_DIRECTORY_FILE as DIR, COMMIT_STREAM_FILE as DATA,
    CommitCatalogPageImage, CommitCatalogStore,
)
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.wal_manager import WalManager

from .conftest import DESCRIPTOR, Stack, build_stack
from .test_catalog_image_preflight import append_activation, stored_bytes

UUID = bytes(range(16))


def materialize(storage: MemoryStorageDevice, file: str, index: int, raw: bytes) -> None:
    if not storage.exists(file):
        storage.create(file)
    count = storage.page_count(file)
    if index >= count:
        storage.allocate(file, index + 1 - count)
    storage.write_page(file, index, raw)


def journal_batches(
    stack: Stack, *, compress: bool = False, count: int = 3,
    horizon: int | None = None, uuid: bytes = UUID, include_heap: bool = True,
) -> tuple[int, list[int], dict[tuple[str, int], bytes]]:
    if horizon is None:
        _catalog, _schema, horizon = append_activation(stack)
    pages: dict[tuple[str, int], bytes] = {}
    store = CommitCatalogStore(lambda file, index: pages[file, index], database_uuid=uuid, page_size=stack.pool.page_size)
    initial = store.plan_initialize(activation_sequence=horizon)
    pages.update({(image.file, image.page_index): image.raw for image in initial.images})
    terminals: list[int] = []
    for i in range(count):
        previous = terminals[-1] if terminals else horizon

        def batch(sequence: int) -> tuple[list[WalRecord], tuple[CommitCatalogPageImage, ...]]:
            value = CommitCatalogEntry(CommitId(uuid, sequence), assign_commit_time(Timestamp(sequence), None),
                                       kind=CommitKind.MAINTENANCE if i == 1 else CommitKind.DATA)
            plan = store.plan_append(value)
            unstamped = plan.images + ((initial.images[1],) if i == 0 else ())
            stamped = []
            records = []
            # An ordinary effect before journal validation must remain unapplied on refusal.
            heap = Page(PageType.HEAP, page_size=stack.pool.page_size, page_lsn=sequence, seq=2)
            heap.insert_slot(b"ordinary committed data")
            encoded = encode_page_write_record("heap.dat", 1, heap.to_bytes(), compress=compress)
            if include_heap:
                records.append(WalRecord(WalRecordType.WRITE_PAGE, encoded.payload, descriptor=DESCRIPTOR,
                                         epoch=2 + i, txn_id=7, format_version=encoded.format_version, flags=encoded.flags))
            for image in unstamped:
                page = Page.from_bytes(image.raw)
                page.page_lsn, page.seq = sequence, 2
                raw = page.to_bytes()
                stamped.append(CommitCatalogPageImage(image.file, image.page_index, raw))
                encoded = encode_page_write_record(image.file, image.page_index, raw, compress=compress)
                records.append(WalRecord(WalRecordType.WRITE_PAGE, encoded.payload, descriptor=DESCRIPTOR,
                                         epoch=2 + i, txn_id=7, format_version=encoded.format_version, flags=encoded.flags))
            records.append(WalRecord(WalRecordType.COMMIT, CommitPayload.build(snapshot_lsn=previous).encode(),
                                     descriptor=DESCRIPTOR, epoch=2 + i, txn_id=7))
            return records, tuple(stamped)

        sequence = previous + 1
        for _ in range(8):
            records, images = batch(sequence)
            planned = stack.wal.planned_terminal_lsn(records)
            if planned == sequence:
                break
            sequence = planned
        else:
            raise AssertionError("WAL terminal preview did not converge")
        assert stack.wal.append_many(records, expected_terminal_lsn=sequence) == sequence
        stack.wal.barrier()
        pages.update({(image.file, image.page_index): image.raw for image in images})
        terminals.append(sequence)
    return horizon, terminals, pages


@pytest.mark.parametrize("mask", range(16))
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("unwritten", [False, True])
def test_native_history_replays_every_four_page_apply_subset_and_repeats(
    stack: Stack, mask: int, compress: bool, unwritten: bool,
) -> None:
    horizon, terminals, pages = journal_batches(stack, compress=compress)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    assert len(pages) == 4
    for position, ((file, index), raw) in enumerate(pages.items()):
        if mask & (1 << position):
            materialize(storage, file, index, bytes(len(raw)) if unwritten else raw)
    reopened = build_stack(storage, bootstrap=False)
    reopened.recovery(database_uuid=UUID).run()
    history = CommitCatalogStore(storage.read_page, database_uuid=UUID, page_size=stack.pool.page_size)
    head = history.verify()
    assert (head.entry_count, head.activation_sequence, head.last_sequence) == (3, horizon, terminals[-1])
    for i, sequence in enumerate(terminals):
        entry = history.lookup(CommitId(UUID, sequence), read_lsn=terminals[-1])
        assert entry is not None and entry.kind == (CommitKind.MAINTENANCE if i == 1 else CommitKind.DATA)
    assert CommitStateStore(storage, owner_id="reader").read().last_committed_lsn == terminals[-1]
    before = stored_bytes(storage)
    build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    assert stored_bytes(storage) == before


def test_same_pool_dirty_replay_repeats_then_flushes(stack: Stack) -> None:
    _horizon, terminals, _pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    replay = committed_replay(stack.wal.read_from(1))
    redo = CommitRedo(stack.pool, database_uuid=UUID)
    first = redo.apply(replay, _checkpoint_lsn=0)
    assert first.page_images_applied > 0 and stack.pool.has_dirty_pages(DIR)
    second = redo.apply(replay, _checkpoint_lsn=0)
    assert second.page_images_applied == 0
    redo.flush(second)
    assert not stack.pool.has_dirty_pages(DIR)
    assert CommitCatalogStore(storage.read_page, database_uuid=UUID, page_size=stack.pool.page_size).verify().last_sequence == terminals[-1]


@pytest.mark.parametrize("file,index", [(DIR, 0), (DATA, 0), (DIR, 1), (DATA, 1)])
@pytest.mark.parametrize("damage", ["foreign_uuid", "future", "same_lsn_changed", "crc", "odd", "wrong_role"])
def test_bad_target_refuses_before_any_ordinary_effect_or_publication(
    stack: Stack, file: str, index: int, damage: str,
) -> None:
    _horizon, terminals, pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    raw = pages[file, index]
    page = Page.from_bytes(raw)
    if damage in {"foreign_uuid", "same_lsn_changed", "wrong_role"}:
        slot = 1 if index == 0 else 0
        data = bytearray(page.read_slot(slot))
        offset = 12 if damage == "foreign_uuid" else 10 if damage == "wrong_role" else len(data) - 1
        data[offset] ^= 1
        page.update_slot(slot, bytes(data))
    elif damage == "future":
        page.page_lsn = terminals[-1] + 1
    elif damage == "odd":
        page.seq = 3
    raw = page.to_bytes()
    if damage == "crc":
        raw = bytes([raw[0] ^ 1]) + raw[1:]
    materialize(storage, file, index, raw)
    reopened = build_stack(storage, bootstrap=False)
    before = stored_bytes(storage)
    with pytest.raises(GrafxCorruptionDetected):
        reopened.recovery(database_uuid=UUID).run()
    assert stored_bytes(storage) == before


@pytest.mark.parametrize("cached", [False, True])
def test_qualified_native_replay_will_not_publish_missing_middle_history(stack: Stack, cached: bool) -> None:
    _horizon, _terminals, pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    if cached:
        for (file, index), raw in pages.items():
            materialize(storage, file, index, raw)
    replay = committed_replay(record for record in stack.wal.read_from(1)
                             if record.epoch != 3 or record.record_type == WalRecordType.COMMIT)
    before = stored_bytes(storage)
    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(stack.pool, database_uuid=UUID).apply(replay, _checkpoint_lsn=0)
    assert stored_bytes(storage) == before


def test_checkpointed_history_reopens_without_replaying_activation(stack: Stack) -> None:
    horizon, terminals, _pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    control = CommitStateStore(storage, owner_id="checkpoint-fixture")
    state = control.read()
    # All journal files were barriered by native recovery before state publication.
    control.publish(replace(state, checkpoint_lsn=terminals[0]), previous=state)
    before = stored_bytes(storage)
    build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    assert stored_bytes(storage) == before
    assert horizon < terminals[0] < terminals[-1]


def test_unqualified_dispatcher_keeps_journal_refusal(stack: Stack) -> None:
    journal_batches(stack)
    replay = committed_replay(stack.wal.read_from(1))
    with pytest.raises(GrafxRecoveryRefused) as failure:
        CommitRedo(stack.pool).apply(replay, _checkpoint_lsn=0)
    assert failure.value.details["field"] == "commit_catalog_replay"


@pytest.mark.parametrize("damage", ["foreign_uuid", "future", "same_lsn_changed"])
def test_dirty_resident_target_cannot_bypass_physical_validation(stack: Stack, damage: str) -> None:
    _horizon, terminals, pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    for (file, index), raw in pages.items():
        materialize(storage, file, index, raw)
    with stack.pool.pinned(DIR, 0) as page:
        if damage == "future":
            page.page_lsn = terminals[-1] + 1
        else:
            slot_data = bytearray(page.read_slot(1))
            slot_data[12 if damage == "foreign_uuid" else -1] ^= 1
            page.update_slot(1, bytes(slot_data))
    before = stored_bytes(storage)
    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(stack.pool, database_uuid=UUID).apply(committed_replay(stack.wal.read_from(1)), _checkpoint_lsn=0)
    assert stored_bytes(storage) == before


@pytest.mark.parametrize("phase", ["wal", DIR, DATA])
def test_barrier_failure_never_publishes_history_and_retry_completes(
    stack: Stack, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _horizon, terminals, _pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    before = stored_bytes(storage)
    control = CommitStateStore(storage, owner_id="reader")
    previous = control.read()
    real_barrier = MemoryStorageDevice.durable_barrier

    def fail_data(device: MemoryStorageDevice, file: str | None = None) -> None:
        if device is storage and file == phase:
            raise OSError("injected journal data barrier failure")
        real_barrier(device, file)

    def fail_wal(_wal: WalManager, _first: int, _through: int) -> tuple[str, ...]:
        raise OSError("injected WAL barrier failure")

    with monkeypatch.context() as fault:
        if phase == "wal":
            fault.setattr(WalManager, "force_barrier_range", fail_wal)
        else:
            fault.setattr(MemoryStorageDevice, "durable_barrier", fail_data)
        with pytest.raises(OSError, match="injected"):
            build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    assert control.read() == previous
    if phase == "wal":
        assert stored_bytes(storage) == before
    build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    assert control.read().last_committed_lsn == terminals[-1]


def test_native_image_cardinality_is_admitted_before_decompression(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    journal_batches(stack, compress=True)
    original = next(record for record in stack.wal.read_from(1) if record.format_version == 2 and record.flags & 16)
    records = tuple(replace(original, lsn=i + 1) for i in range(155))
    terminal = WalRecord(WalRecordType.COMMIT, lsn=200, epoch=original.epoch, txn_id=original.txn_id)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Image decoding preceded admission")

    monkeypatch.setattr("okto_grafx.engine.commit_redo.decode_page_write", forbidden)
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitRedo(stack.pool, database_uuid=UUID).preflight(committed_replay((*records, terminal)), _checkpoint_lsn=0)


@pytest.mark.parametrize("cut", [1, 2, 3, 4])
@pytest.mark.parametrize("after_write", [False, True])
def test_interruption_at_each_native_journal_write_replays_without_duplicate_history(
    stack: Stack, cut: int, after_write: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _horizon, terminals, _pages = journal_batches(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    control = CommitStateStore(storage, owner_id="reader")
    previous = control.read()
    real_write = MemoryStorageDevice.write_page
    offered = 0

    def interrupted(device: MemoryStorageDevice, file: str, index: int, raw: bytes) -> None:
        nonlocal offered
        journal = device is storage and file in {DIR, DATA}
        if journal:
            offered += 1
        if journal and offered == cut and not after_write:
            raise OSError("interrupted journal write")
        real_write(device, file, index, raw)
        if journal and offered == cut:
            raise OSError("interrupted journal write")

    with monkeypatch.context() as fault:
        fault.setattr(MemoryStorageDevice, "write_page", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    assert offered == cut and control.read() == previous
    build_stack(storage, bootstrap=False).recovery(database_uuid=UUID).run()
    history = CommitCatalogStore(storage.read_page, database_uuid=UUID, page_size=stack.pool.page_size)
    assert history.verify().entry_count == 3
    assert control.read().last_committed_lsn == terminals[-1]


def test_public_checkpoint_and_reopen_cover_journal_after_idle_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as database:
        database.ensure_identity_indexes()
        with database.begin("write") as txn:
            assert database._transactions.prepare_commit_catalog_activation(txn._context)
        horizon = database._catalog.catalog.commit_catalog_activation
        assert horizon is not None
        database.checkpoint()
        stack = build_stack(database._storage, bootstrap=False)
        _horizon, terminals, _pages = journal_batches(stack, horizon=horizon,
            uuid=database.identity.database_uuid, include_heap=False)
        database.checkpoint()
        assert database._transactions.published_lsn() == terminals[-1]
    # A completely new pool has no modified-page ledger. An idle checkpoint must
    # still barrier both journal files before advancing/publishing its control.
    with connect(root, page_size=512, partitions_per_table=8) as database:
        barriers: list[str] = []
        real_barrier = database._pool.durability_barrier

        def record_barrier(file: str) -> None:
            barriers.append(file)
            real_barrier(file)

        monkeypatch.setattr(type(database._pool), "durability_barrier", lambda _pool, file: record_barrier(file))
        database.checkpoint()
        assert {DIR, DATA}.issubset(barriers)
        history = CommitCatalogStore(database._storage.read_page, database_uuid=database.identity.database_uuid, page_size=512)
        assert history.verify().entry_count == 3
