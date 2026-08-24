"""Secondary-index behavior around the heap's reserved pre-WAL commit stamp."""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.index import (
    IndexChange,
    IndexEntry,
    IndexHeader,
    IndexOperation,
    IndexVisibility,
    change_of,
)
from okto_grafx.domain.txn import Snapshot
from okto_grafx.engine.verifier import Verifier

from .conftest import Database, RecordingMetrics, cold_view

BORN = 10
COMMITTED_LATER = 20


class PendingTransaction:
    """The staging protocol plus the concrete pending-record list retargeting owns."""

    def __init__(self, txn_id: int) -> None:
        self.txn_id = txn_id
        self.epoch = 1
        self.pending_records: list[object] = []

    def stage_record(self, record: object) -> None:
        self.pending_records.append(record)


def _indexed_seed(database: Database) -> RecordRef:
    """Write one committed row and both index entries it owes."""
    txn = PendingTransaction(1)
    ref = database.heap.insert(database.table, 1, (1, "Ada"), BORN)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.commit(txn, BORN)
    return ref


@pytest.mark.parametrize("next_operation", ("update", "delete"))
def test_a_reopened_provisional_update_is_ignored_by_indexes_and_remains_repairable(
    database: Database, next_operation: str
) -> None:
    """A flushed ghost neither enters a rebuild nor poisons lookup or later row work."""
    original = _indexed_seed(database)
    ghost = database.heap.update(
        database.table,
        original,
        (1, "never committed"),
        PROVISIONAL_CSN,
    )
    database.pool.flush(database.heap.file)

    reopened = cold_view(database)
    snapshot = Snapshot(COMMITTED_LATER)
    old_key = database.key(1, "Ada")
    ghost_key = database.key(1, "never committed")

    assert reopened.heap.read(original).live is True
    assert reopened.heap.read(ghost).live is False
    assert reopened.heap.lookup(database.table, 1, snapshot).values == (1, "Ada")  # type: ignore[union-attr]
    assert reopened.manager.lookup(reopened.exact.name, old_key, snapshot) == (
        original,
    )
    assert reopened.manager.lookup(reopened.proximity.name, old_key, snapshot) == (
        original,
    )
    assert reopened.manager.lookup(reopened.exact.name, ghost_key, snapshot) == ()
    assert reopened.manager.lookup(reopened.proximity.name, ghost_key, snapshot) == ()
    assert reopened.manager.verify() == ()
    assert (
        Verifier(
            reopened.pool,
            RecordingMetrics(),
            heap=reopened.heap,
            catalog=reopened.catalog,
            indexes=reopened.manager.indexes(),
        )
        .verify()
        .findings
        == ()
    )

    rebuild = PendingTransaction(2)
    assert reopened.manager.rebuild(reopened.proximity.name, rebuild, BORN) == 2
    changes = tuple(change_of(record) for record in rebuild.pending_records)
    assert tuple(change.operation for change in changes) == (
        IndexOperation.RESET,
        IndexOperation.INSERT,
    )
    assert changes[1].ref == original
    assert changes[1].csn == BORN
    reopened.manager.rollback(rebuild)

    if next_operation == "update":
        replacement = reopened.heap.update(
            database.table, original, (1, "Grace"), COMMITTED_LATER
        )
        assert reopened.heap.read(replacement).live is True
        assert [
            version.values
            for _ref, version in reopened.heap.scan(database.table, snapshot)
        ] == [(1, "Grace")]
    else:
        reopened.heap.delete(database.table, original, COMMITTED_LATER)
        assert reopened.heap.lookup(database.table, 1, snapshot) is None


def test_retarget_staged_updates_registry_and_wal_records_together(
    database: Database,
) -> None:
    """The exact index keeps zero while every predicted versioned stamp moves."""
    txn = PendingTransaction(7)
    ref = database.heap.insert(database.table, 1, (1, "Ada"), BORN)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.stage_row_delete(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    pending_identity = txn.pending_records
    original_records = tuple(pending_identity)

    retargeted = database.manager.retarget_staged(txn, BORN, COMMITTED_LATER)

    assert txn.pending_records is pending_identity
    assert tuple(txn.pending_records) == retargeted
    assert len(retargeted) == len(original_records) == 4
    decoded = tuple(change_of(record) for record in retargeted)
    assert tuple(change.csn for change in decoded) == (
        0,
        COMMITTED_LATER,
        COMMITTED_LATER,
        COMMITTED_LATER,
    )
    assert tuple(database.exact.pending(txn)) == (decoded[0], decoded[2])
    assert tuple(database.proximity.pending(txn)) == (decoded[1], decoded[3])


def test_retarget_staged_refuses_the_sentinel_before_mutating_any_list(
    database: Database,
) -> None:
    txn = PendingTransaction(8)
    ref = database.heap.insert(database.table, 1, (1, "Ada"), BORN)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    records_before = tuple(txn.pending_records)
    staged_before = tuple(
        change for index in database.manager.indexes() for change in index.pending(txn)
    )

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.retarget_staged(txn, BORN, PROVISIONAL_CSN)

    assert refused.value.details["field"] == "new_csn"
    assert tuple(txn.pending_records) == records_before
    assert (
        tuple(
            change
            for index in database.manager.indexes()
            for change in index.pending(txn)
        )
        == staged_before
    )


def test_persisted_index_entries_refuse_the_provisional_stamp_as_corruption() -> None:
    entry = IndexEntry(key=b"k", ref=RecordRef(2, 1), versioned=True, born_csn=BORN)
    image = bytearray(entry.encode())
    # flags u8 + key length u16 + ref u64 = born_csn starts at byte 11.
    struct.pack_into("<Q", image, 11, PROVISIONAL_CSN)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexEntry.decode(bytes(image))

    assert refused.value.details["field"] == "born_csn"


def test_persisted_index_headers_refuse_the_reserved_log_position() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=1,
        bucket_count=4,
        digest=b"d" * 16,
    )
    image = bytearray(header.encode())
    # format u16 + visibility u8 + flags u8 + table/bucket u32 = built LSN at byte 12.
    struct.pack_into("<Q", image, 12, PROVISIONAL_CSN)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexHeader.decode(bytes(image))

    assert refused.value.details["field"] == "built_through_lsn"


def test_index_wal_changes_refuse_the_provisional_stamp() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexChange(
            index="person_near_name",
            operation=IndexOperation.INSERT,
            key=b"k",
            ref=RecordRef(2, 1),
            csn=PROVISIONAL_CSN,
            versioned=True,
        )

    assert refused.value.details["field"] == "csn"

    valid = IndexChange(
        index="person_near_name",
        operation=IndexOperation.INSERT,
        key=b"k",
        ref=RecordRef(2, 1),
        csn=BORN,
        versioned=True,
    )
    payload = bytearray(valid.encode())
    # format u16 + operation u8 + flags u8 + ref u64 = csn starts at byte 12.
    struct.pack_into("<Q", payload, 12, PROVISIONAL_CSN)

    with pytest.raises(GrafxCorruptionDetected) as damaged:
        IndexChange.decode(bytes(payload))

    assert damaged.value.details["field"] == "csn"
