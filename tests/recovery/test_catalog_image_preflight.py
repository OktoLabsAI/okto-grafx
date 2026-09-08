"""Complete schema snapshots through the real WAL, fenced recovery and control publication."""
from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.page import Page
from okto_grafx.domain.txn.commit_record import CommitPayload
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.commit_state_store import CommitStateStore

from .conftest import CATALOG_FILE, DESCRIPTOR, Stack, build_stack


def append_activation(stack: Stack, *, omit: int | None = None) -> tuple[Catalog, tuple[tuple[int, bytes], ...], int]:
    """Emit a durable real batch, optionally missing one otherwise valid image."""
    def batch(sequence: int) -> tuple[Catalog, tuple[tuple[int, bytes], ...], list[WalRecord]]:
        value = stack.catalog.read_from_pages().upgrade_index_catalog().enable_commit_catalog(sequence)
        images = []
        records = []
        for index, raw in stack.catalog.stage(value):
            page = Page.from_bytes(raw)
            page.page_lsn = sequence
            page.seq = 2
            image = page.to_bytes()
            images.append((index, image))
            if index != omit:
                records.append(WalRecord(WalRecordType.WRITE_PAGE,
                    encode_page_write(CATALOG_FILE, index, image),
                    descriptor=DESCRIPTOR, epoch=1, txn_id=7))
        records.append(WalRecord(WalRecordType.COMMIT,
            CommitPayload.build(snapshot_lsn=0).encode(), descriptor=DESCRIPTOR, epoch=1, txn_id=7))
        return value, tuple(images), records

    _value, _images, preview = batch(1)
    terminal = stack.wal.planned_terminal_lsn(preview)
    value, images, records = batch(terminal)
    assert stack.wal.append_many(records, expected_terminal_lsn=terminal) == terminal
    stack.wal.barrier()
    stack.pool.flush()
    return value, images, terminal


def stored_bytes(storage: MemoryStorageDevice) -> dict[str, bytes]:
    return {name: storage.read_log(name, 0, storage.file_size(name)) for name in storage.list_files("")}


@pytest.mark.parametrize("mask", range(4))
@pytest.mark.parametrize("torn", [False, True])
def test_native_activation_recovery_across_every_two_page_apply_cut(stack: Stack, mask: int, torn: bool) -> None:
    value, images, terminal = append_activation(stack)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    assert len(images) == 2
    for position, (index, raw) in enumerate(images):
        if mask & (1 << position):
            storage.write_page(CATALOG_FILE, index, bytes(len(raw)) if torn else raw)
    reopened = build_stack(storage, bootstrap=False)
    reopened.recovery().run()
    assert reopened.catalog.read_from_pages().serialize() == value.serialize()
    assert CommitStateStore(storage, owner_id="read-result").read().last_committed_lsn == terminal
    # Reopening again repeats native replay; history activation and the durable
    # data/control bytes remain identical, rather than creating a second outcome.
    before = stored_bytes(storage)
    again = build_stack(storage, bootstrap=False)
    again.recovery().run()
    assert again.catalog.read_from_pages().serialize() == value.serialize()
    assert stored_bytes(storage) == before


@pytest.mark.parametrize("omit", [0, 1])
@pytest.mark.parametrize("already_applied", [False, True])
def test_native_incomplete_catalog_refuses_without_any_persisted_change(stack: Stack, omit: int, already_applied: bool) -> None:
    _value, images, _terminal = append_activation(stack, omit=omit)
    storage = stack.storage
    assert isinstance(storage, MemoryStorageDevice)
    if already_applied:
        for index, raw in images:
            storage.write_page(CATALOG_FILE, index, raw)
    reopened = build_stack(storage, bootstrap=False)
    before = stored_bytes(storage)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        reopened.recovery().run()
    assert failure.value.details["field"] == "catalog_images"
    assert stored_bytes(storage) == before
