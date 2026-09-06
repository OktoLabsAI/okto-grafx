"""Record-aware lookup, rebuild, and verification for logical-identity indexes."""

from __future__ import annotations

from okto_grafx.domain.index import (
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    IndexVisibility,
    record_id_key,
)
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager

from .conftest import (
    TEST_BUCKET_COUNT,
    RecordingMetrics,
    SnapshotDouble,
    TransactionDouble,
)

BORN = 31
UNSIGNED_RECORD_ID = (1 << 63) + 41


def _register_identity(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    table: TableDef,
) -> HashIndex:
    definition = IndexDefinition(
        name=f"rid_t_{table.table_id:08x}",
        table_id=table.table_id,
        table_name=table.name,
        positions=(),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
    )
    index = HashIndex(definition, pool, metrics)
    manager.register(index)
    return index


def test_identity_lookup_validates_the_unsigned_record_id_from_the_heap(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    identity = _register_identity(manager, pool, metrics, person_table)
    values = (7, "Ada")
    ref = heap_store.insert(person_table, UNSIGNED_RECORD_ID, values, BORN)
    txn = TransactionDouble()
    manager.stage_row_insert(
        txn,
        person_table.table_id,
        ref,
        values,
        BORN,
        record_id=UNSIGNED_RECORD_ID,
        table_name=person_table.name,
        table=person_table,
    )
    manager.commit(txn, BORN)

    found = manager.lookup_versions(
        identity.name,
        record_id_key(UNSIGNED_RECORD_ID),
        SnapshotDouble(BORN),
    )

    assert tuple(item_ref for item_ref, _version in found) == (ref,)
    assert found[0][1].record_id == UNSIGNED_RECORD_ID
    assert found[0][1].values == values


def test_identity_rebuild_rederives_the_key_from_each_heap_record_id(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    identity = _register_identity(manager, pool, metrics, person_table)
    ref = heap_store.insert(person_table, UNSIGNED_RECORD_ID, (9, "Grace"), BORN)
    manager.open(BORN)
    txn = TransactionDouble(txn_id=2)

    staged = manager.rebuild(identity.name, txn, BORN)
    manager.commit(txn, BORN)
    manager.clear_stale(identity.name, BORN)

    assert staged == 2, "one reset plus one identity entry"
    assert tuple((entry.key, entry.ref) for entry in identity.walk()) == (
        (record_id_key(UNSIGNED_RECORD_ID), ref),
    )
    assert manager.lookup(
        identity.name,
        record_id_key(UNSIGNED_RECORD_ID),
        SnapshotDouble(BORN),
    ) == (ref,)
    assert manager.verify(identity.name) == ()


def test_identity_verifier_reports_a_value_independent_wrong_key_and_omission(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    identity = _register_identity(manager, pool, metrics, person_table)
    ref = heap_store.insert(person_table, UNSIGNED_RECORD_ID, (11, "Lin"), BORN)
    wrong_record_id = UNSIGNED_RECORD_ID + 1
    txn = TransactionDouble(txn_id=3)
    identity.stage_insert(txn, record_id_key(wrong_record_id), ref, BORN)
    manager.commit(txn, BORN)

    findings = manager.verify(identity.name)

    assert {finding.kind for finding in findings} == {"stale_entry", "missing_entry"}
    assert all(finding.ref == ref for finding in findings)
    assert (
        manager.lookup(
            identity.name,
            record_id_key(wrong_record_id),
            SnapshotDouble(BORN),
        )
        == ()
    )
