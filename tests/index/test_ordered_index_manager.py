"""Ordered artifacts inside the ordinary secondary-index transaction framework."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index import (
    ORDERED_KEY_DERIVATION,
    IndexDefinition,
    IndexLayout,
    IndexVisibility,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import Timestamp, ValueType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.ordered_index import OrderedIndex

from .conftest import MemoryDevice, RecordingMetrics, SnapshotDouble, TransactionDouble, make_pool


def _stack() -> tuple[
    MemoryDevice,
    BufferPool,
    CatalogStore,
    HeapStore,
    TableDef,
    IndexDefinition,
    OrderedIndex,
    IndexManager,
]:
    device = MemoryDevice()
    metrics = RecordingMetrics()
    pool = make_pool(device, metrics, budget_pages=48)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    table = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name="Event",
        kind="node",
        columns=(
            ColumnDef("created_at", ValueType.TIMESTAMP, nullable=False),
            ColumnDef("id", ValueType.STRING, nullable=False),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()
    definition = IndexDefinition.on(
        table,
        name="event_by_created_at_id",
        columns=("created_at", "id"),
        visibility=IndexVisibility.EXACT,
        bucket_count=1,
        key_derivation=ORDERED_KEY_DERIVATION,
        artifact_nonce=701,
        layout=IndexLayout.ORDERED,
    )
    index = OrderedIndex(definition, pool, metrics)
    manager = IndexManager(pool, heap, metrics)
    manager.register(index, complete_through=0)
    return device, pool, catalog, heap, table, definition, index, manager


def test_manager_commits_one_cow_generation_and_heap_validates_exact_lookup() -> None:
    _device, _pool, _catalog, heap, table, definition, index, manager = _stack()
    txn = TransactionDouble(txn_id=9)
    rows: list[tuple[object, tuple[object, ...]]] = []
    for number in range(80):
        values = (Timestamp(number), f"id-{number:04d}")
        ref = heap.insert(table, number + 1, values, xmin=10)
        rows.append((ref, values))
        manager.stage_row_insert(txn, table.table_id, ref, values, 10)
    before = index.open_root()

    assert manager.commit(txn, 10) == 80

    after = index.open_root()
    assert after.generation == before.generation + 1
    assert after.entry_count == 80
    selected_ref, selected_values = rows[37]
    assert manager.lookup(
        index.name,
        definition.key_for(selected_values),
        SnapshotDouble(10),
    ) == (selected_ref,)
    assert manager.verify(index.name) == ()


def test_manager_rollback_and_replay_retry_preserve_one_exact_answer() -> None:
    device, _pool, _catalog, heap, table, definition, index, manager = _stack()
    values = (Timestamp(20), "twenty")
    ref = heap.insert(table, 20, values, xmin=20)
    aborted = TransactionDouble(txn_id=10)
    manager.stage_row_insert(aborted, table.table_id, ref, values, 20)

    assert manager.rollback(aborted) == 1
    with pytest.raises(GrafxIndexError) as unavailable:
        manager.lookup(index.name, definition.key_for(values), SnapshotDouble(20))
    assert unavailable.value.details["field"] == "index_view_unavailable"

    replay = TransactionDouble(txn_id=11)
    manager.stage_row_insert(replay, table.table_id, ref, values, 20)
    record = replay.with_lsns(30)[0]
    assert manager.apply(record)
    pages_after_first = device.page_count(index.file)
    assert manager.apply(record)

    assert device.page_count(index.file) == pages_after_first
    assert manager.lookup(
        index.name, definition.key_for(values), SnapshotDouble(30)
    ) == (ref,)


def test_detached_ordered_generation_bulk_builds_history_and_remains_unregistered() -> None:
    _device, pool, _catalog, heap, table, definition, _index, manager = _stack()
    old_values = (Timestamp(1), "same")
    old = heap.insert(table, 1, old_values, xmin=3)
    new_values = (Timestamp(2), "same")
    new = heap.update(table, old, new_values, xmin=8)
    pool.flush(heap.file)
    detached_definition = IndexDefinition.on(
        table,
        name="event_by_created_at_id_next",
        columns=("created_at", "id"),
        visibility=IndexVisibility.EXACT,
        bucket_count=1,
        key_derivation=ORDERED_KEY_DERIVATION,
        artifact_nonce=definition.artifact_nonce + 1,
        layout=IndexLayout.ORDERED,
    )

    detached = manager._build_detached_exact_generation(
        detached_definition, through_lsn=8
    )

    assert isinstance(detached, OrderedIndex)
    assert {(entry.ref, entry.dead_csn) for entry in detached.walk()} == {
        (old, 8),
        (new, 0),
    }
    assert detached.open_root().applied_through_lsn == 8
    assert manager._verify_entries(detached) == ()
    assert manager._verify_coverage(detached) == ()
    assert detached not in manager.indexes()


def test_partitioned_recovery_coalesces_one_ordered_store_into_one_root() -> None:
    device, _pool, _catalog, heap, table, _definition, index, manager = _stack()
    txn = TransactionDouble(txn_id=12)
    for number in range(60):
        values = (Timestamp(number), f"replay-{number:04d}")
        ref = heap.insert(table, number + 1, values, xmin=70)
        manager.stage_row_insert(txn, table.table_id, ref, values, 70)
    records = txn.with_lsns(100)
    before = index.open_root()

    assert manager.apply_partitioned_replay_batch(records) == (index.file,)

    after = index.open_root()
    pages_after = device.page_count(index.file)
    assert after.generation == before.generation + 1
    assert after.applied_through_lsn == 159
    assert after.entry_count == 60
    assert manager.apply_partitioned_replay_batch(records) == (index.file,)
    assert index.open_root() == after
    assert device.page_count(index.file) == pages_after
