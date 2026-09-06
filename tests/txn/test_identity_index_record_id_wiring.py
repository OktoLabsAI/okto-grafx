"""Durable row identities cross the transaction-to-index boundary unchanged."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from okto_grafx.domain.index import (
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    IndexVisibility,
    record_id_key,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.index_manager import HashIndex, IndexManager
from txn_support import Stack, build_stack


RECORD_ID = (1 << 63) + 41


class _RecordingIndexManager(IndexManager):
    """Run the real index implementation while recording its row-level seam."""

    __slots__ = ("delete_calls", "insert_calls", "quota_calls")

    def __init__(self, stack: Stack) -> None:
        super().__init__(stack.pool, stack.heap, stack.metrics)
        self.quota_calls: list[tuple[int, tuple[object, ...], object | None]] = []
        self.insert_calls: list[
            tuple[int, object, tuple[object, ...], int, object | None]
        ] = []
        self.delete_calls: list[
            tuple[int, object, tuple[object, ...], int, object | None]
        ] = []

    def clear_observations(self) -> None:
        self.quota_calls.clear()
        self.insert_calls.clear()
        self.delete_calls.clear()

    def row_entry_count(
        self,
        table_id: int,
        values: tuple[object, ...],
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
        txn: object | None = None,
    ) -> int:
        self.quota_calls.append((table_id, tuple(values), record_id))
        return super().row_entry_count(
            table_id,
            values,
            record_id=record_id,
            table_name=table_name,
            table=table,
            txn=txn,  # type: ignore[arg-type]
        )

    def stage_row_insert(
        self,
        txn: object,
        table_id: int,
        ref: object,
        values: tuple[object, ...],
        csn: int,
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[object, ...]:
        self.insert_calls.append((table_id, ref, tuple(values), csn, record_id))
        return super().stage_row_insert(
            txn,  # type: ignore[arg-type]
            table_id,
            ref,  # type: ignore[arg-type]
            values,
            csn,
            record_id=record_id,
            table_name=table_name,
            table=table,
        )

    def stage_row_delete(
        self,
        txn: object,
        table_id: int,
        ref: object,
        values: tuple[object, ...],
        csn: int,
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[object, ...]:
        self.delete_calls.append((table_id, ref, tuple(values), csn, record_id))
        return super().stage_row_delete(
            txn,  # type: ignore[arg-type]
            table_id,
            ref,  # type: ignore[arg-type]
            values,
            csn,
            record_id=record_id,
            table_name=table_name,
            table=table,
        )


def _table() -> TableDef:
    return TableDef(
        table_id=1,
        name="person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="label", type=ValueType.STRING),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _wired(
    database_root: Path,
) -> tuple[Stack, TableDef, _RecordingIndexManager, HashIndex]:
    stack = build_stack(database_root)
    table = _table()
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)

    indexes = _RecordingIndexManager(stack)
    identity = indexes.register(
        HashIndex(
            IndexDefinition(
                name="rid_t_00000001",
                table_id=table.table_id,
                table_name=table.name,
                positions=(),
                visibility=IndexVisibility.EXACT,
                key_derivation=RECORD_ID_KEY_DERIVATION,
            ),
            stack.pool,
            stack.metrics,
        )
    )
    stack.manager._index_manager = indexes
    return stack, table, indexes, identity


def _insert(stack: Stack, table: TableDef, values: tuple[object, ...]) -> object:
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, values, record_id=RECORD_ID)
    txn.note_write(stack.manager.partition_of(table.table_id, b"identity-wiring"))
    stack.manager.commit(txn)
    return txn.row_refs[0]


def test_canonical_manager_counts_index_records_once_per_row_version(
    database_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One immutable commit-section view supplies quota prediction and verification."""
    stack = build_stack(database_root)
    table = _table()
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    indexes = IndexManager(stack.pool, stack.heap, stack.metrics)
    indexes.register(
        HashIndex(
            IndexDefinition(
                name="rid_t_00000001",
                table_id=table.table_id,
                table_name=table.name,
                positions=(),
                visibility=IndexVisibility.EXACT,
                key_derivation=RECORD_ID_KEY_DERIVATION,
            ),
            stack.pool,
            stack.metrics,
        )
    )
    stack.manager._index_manager = indexes
    original = IndexManager.row_entry_count
    calls = 0

    def counted(manager: IndexManager, *args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        return original(manager, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(IndexManager, "row_entry_count", counted)

    _insert(stack, table, (7, "trusted"))

    assert calls == 1, "the first quota count is carried into staging verification"


def test_canonical_manager_resolves_active_indexes_once_per_written_row(
    database_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quota prediction and staging share one immutable table-local index projection."""
    stack = build_stack(database_root)
    table = _table()
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    indexes = IndexManager(stack.pool, stack.heap, stack.metrics)
    indexes.register(
        HashIndex(
            IndexDefinition(
                name="rid_t_00000001",
                table_id=table.table_id,
                table_name=table.name,
                positions=(),
                visibility=IndexVisibility.EXACT,
                key_derivation=RECORD_ID_KEY_DERIVATION,
            ),
            stack.pool,
            stack.metrics,
        )
    )
    stack.manager._index_manager = indexes
    original = IndexManager.active_indexes_for
    calls = 0

    def counted(
        manager: IndexManager, *args: object, **kwargs: object
    ) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        return original(manager, *args, **kwargs)

    monkeypatch.setattr(IndexManager, "active_indexes_for", counted)

    _insert(stack, table, (7, "trusted"))

    # Two other commit-protocol validations resolve table authority independently. The row-level
    # quota/staging pair contributes only one call; before LV-3 the same commit contributed three.
    assert calls == 3


def test_insert_passes_the_resolved_unsigned_identity_to_quota_and_staging(
    database_root: Path,
) -> None:
    stack, table, indexes, identity = _wired(database_root)
    values = (7, "trusted")

    reference = _insert(stack, table, values)

    assert stack.heap.read(reference).record_id == RECORD_ID
    assert indexes.quota_calls == [
        (table.table_id, values, RECORD_ID),
        (table.table_id, values, RECORD_ID),
    ]
    assert [call[:3] + call[4:] for call in indexes.insert_calls] == [
        (table.table_id, reference, values, RECORD_ID)
    ]
    assert [entry.ref for entry in identity.candidates(record_id_key(RECORD_ID))] == [
        reference
    ]


def test_update_uses_the_heap_identity_for_both_physical_versions(
    database_root: Path,
) -> None:
    stack, table, indexes, _identity = _wired(database_root)
    old_values = (7, "trusted")
    new_values = (7, "updated")
    old_ref = _insert(stack, table, old_values)
    indexes.clear_observations()

    txn = stack.manager.begin("write")
    txn.stage_row_update(table, old_ref, new_values)
    # ``record_id`` is mutable test-facing context.  A forged caller hint must not replace the
    # durable identity already stored in the heap version being updated.
    txn.row_intents[-1] = replace(txn.row_intents[-1], record_id=RECORD_ID + 1)
    txn.note_write(stack.manager.partition_of(table.table_id, b"identity-wiring"))
    stack.manager.commit(txn)
    new_ref = txn.row_refs[0]

    assert stack.heap.read(old_ref).record_id == RECORD_ID
    assert stack.heap.read(new_ref).record_id == RECORD_ID
    assert indexes.quota_calls == [
        (table.table_id, old_values, RECORD_ID),
        (table.table_id, new_values, RECORD_ID),
        (table.table_id, old_values, RECORD_ID),
        (table.table_id, new_values, RECORD_ID),
    ]
    assert [call[:3] + call[4:] for call in indexes.delete_calls] == [
        (table.table_id, old_ref, old_values, RECORD_ID)
    ]
    assert [call[:3] + call[4:] for call in indexes.insert_calls] == [
        (table.table_id, new_ref, new_values, RECORD_ID)
    ]


def test_delete_derives_identity_and_values_from_the_heap_not_caller_context(
    database_root: Path,
) -> None:
    stack, table, indexes, _identity = _wired(database_root)
    durable_values = (7, "trusted")
    old_ref = _insert(stack, table, durable_values)
    indexes.clear_observations()

    txn = stack.manager.begin("write")
    txn.stage_row_delete(table, old_ref)
    assert txn.row_intents[-1].values == ()
    txn.row_intents[-1] = replace(
        txn.row_intents[-1],
        values=(999, "forged"),
        record_id=RECORD_ID + 1,
    )
    txn.note_write(stack.manager.partition_of(table.table_id, b"identity-wiring"))
    stack.manager.commit(txn)

    ended = stack.heap.read(old_ref)
    assert ended.record_id == RECORD_ID
    assert ended.xmax > 0
    assert indexes.quota_calls == [
        (table.table_id, durable_values, RECORD_ID),
        (table.table_id, durable_values, RECORD_ID),
    ]
    assert [call[:3] + call[4:] for call in indexes.delete_calls] == [
        (table.table_id, old_ref, durable_values, RECORD_ID)
    ]
    assert indexes.insert_calls == []
