"""Record-aware transaction staging for unsigned logical-identity indexes."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import (
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    IndexOperation,
    IndexVisibility,
    change_of,
    record_id_key,
)
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.index_manager import HashIndex, IndexManager

from .conftest import (
    TEST_BUCKET_COUNT,
    RecordingMetrics,
    TransactionDouble,
    exact_definition,
)


def _register_identity(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    table: TableDef,
) -> None:
    definition = IndexDefinition(
        name=f"rid_t_{table.table_id:08x}",
        table_id=table.table_id,
        table_name=table.name,
        positions=(),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
    )
    manager.register(HashIndex(definition, pool, metrics))


def test_identity_insert_and_quota_cover_the_unsigned_domain_with_empty_values(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    _register_identity(manager, pool, metrics, person_table)
    record_id = (1 << 63) + 17
    ref = RecordRef(7, 3)
    txn = TransactionDouble()

    assert (
        manager.row_entry_count(
            person_table.table_id,
            (),
            record_id=record_id,
            table_name=person_table.name,
            table=person_table,
        )
        == 1
    )

    records = manager.stage_row_insert(
        txn,
        person_table.table_id,
        ref,
        (),
        11,
        record_id=record_id,
        table_name=person_table.name,
        table=person_table,
    )

    assert len(records) == 1
    assert change_of(records[0]).operation is IndexOperation.INSERT
    assert change_of(records[0]).key == record_id_key(record_id)
    assert change_of(records[0]).ref == ref


def test_identity_update_keeps_one_logical_key_across_physical_versions(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    _register_identity(manager, pool, metrics, person_table)
    record_id = (1 << 63) + 29
    old_ref = RecordRef(4, 1)
    new_ref = RecordRef(9, 2)

    records = manager.stage_row_update(
        TransactionDouble(),
        person_table.table_id,
        old_ref,
        (),
        new_ref,
        (),
        13,
        record_id=record_id,
        table_name=person_table.name,
        table=person_table,
    )
    changes = tuple(change_of(record) for record in records)

    assert tuple(change.operation for change in changes) == (
        IndexOperation.TOMBSTONE,
        IndexOperation.INSERT,
    )
    assert tuple(change.key for change in changes) == (
        record_id_key(record_id),
        record_id_key(record_id),
    )
    assert tuple(change.ref for change in changes) == (old_ref, new_ref)


def test_identity_delete_keys_the_identity_of_the_ended_version(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    _register_identity(manager, pool, metrics, person_table)
    ended_record_id = (1 << 64) - 2
    ended_ref = RecordRef(12, 5)

    records = manager.stage_row_delete(
        TransactionDouble(),
        person_table.table_id,
        ended_ref,
        (),
        17,
        record_id=ended_record_id,
        table_name=person_table.name,
        table=person_table,
    )
    change = change_of(records[0])

    assert change.operation is IndexOperation.TOMBSTONE
    assert change.key == record_id_key(ended_record_id)
    assert change.ref == ended_ref


def test_column_indexes_keep_the_legacy_value_only_staging_contract(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    definition = exact_definition(person_table)
    manager.register(HashIndex(definition, pool, metrics))
    values = (7, "Ada")
    ref = RecordRef(2, 1)

    # Exact indexes owe a fixed entry even when DELETE quota planning no longer has values.
    assert (
        manager.row_entry_count(
            person_table.table_id,
            (),
            table_name=person_table.name,
            table=person_table,
        )
        == 1
    )

    records = manager.stage_row_insert(
        TransactionDouble(),
        person_table.table_id,
        ref,
        values,
        19,
        table_name=person_table.name,
        table=person_table,
    )

    assert change_of(records[0]).key == definition.key_for(values)


def test_identity_staging_and_quota_refuse_a_missing_record_id(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    _register_identity(manager, pool, metrics, person_table)
    txn = TransactionDouble()
    old_ref = RecordRef(3, 1)
    new_ref = RecordRef(5, 2)
    table_arguments = {
        "table_name": person_table.name,
        "table": person_table,
    }

    with pytest.raises(GrafxIndexError, match="RecordId") as quota_refused:
        manager.row_entry_count(person_table.table_id, (), **table_arguments)
    with pytest.raises(GrafxIndexError, match="RecordId"):
        manager.stage_row_insert(
            txn, person_table.table_id, new_ref, (), 23, **table_arguments
        )
    with pytest.raises(GrafxIndexError, match="RecordId"):
        manager.stage_row_delete(
            txn, person_table.table_id, old_ref, (), 23, **table_arguments
        )
    with pytest.raises(GrafxIndexError, match="RecordId"):
        manager.stage_row_update(
            txn,
            person_table.table_id,
            old_ref,
            (),
            new_ref,
            (),
            23,
            **table_arguments,
        )

    assert quota_refused.value.details["field"] == "record_id"
    assert txn.staged == []
