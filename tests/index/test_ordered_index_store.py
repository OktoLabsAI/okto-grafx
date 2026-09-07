"""Persistent ordered artifact, stable certificates and mandatory heap revalidation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.index import (
    ORDERED_KEY_DERIVATION,
    ORDERED_ROOT_PAGE_A,
    ORDERED_ROOT_PAGE_B,
    IndexDefinition,
    IndexEntry,
    IndexLayout,
    IndexVisibility,
    OrderedRootDescriptor,
    decode_ordered_root_page,
    make_ordered_root_page,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import Timestamp, ValueType
from okto_grafx.domain.page import Page
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.ordered_index import OrderedIndex

from .conftest import MemoryDevice, RecordingMetrics, make_pool


def _table(catalog: CatalogStore, *, name: str = "Event") -> TableDef:
    table = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name=name,
        kind="node",
        columns=(
            ColumnDef("created_at", ValueType.TIMESTAMP, nullable=False),
            ColumnDef("id", ValueType.STRING, nullable=False),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()
    return table


def _definition(table: TableDef, *, nonce: int = 91) -> IndexDefinition:
    return IndexDefinition.on(
        table,
        name=f"ordered_{table.name}",
        columns=("created_at", "id"),
        visibility=IndexVisibility.EXACT,
        bucket_count=1,
        key_derivation=ORDERED_KEY_DERIVATION,
        artifact_nonce=nonce,
        layout=IndexLayout.ORDERED,
    )


def _entry(definition: IndexDefinition, ref: object, values: tuple[object, ...]) -> IndexEntry:
    return IndexEntry(
        key=definition.key_for(values),
        ref=ref,  # type: ignore[arg-type]
        versioned=False,
    )


def _stack(
    device: MemoryDevice | None = None,
) -> tuple[MemoryDevice, BufferPool, CatalogStore, HeapStore, TableDef, IndexDefinition]:
    actual = MemoryDevice() if device is None else device
    pool = make_pool(actual, RecordingMetrics(), budget_pages=32)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    table = _table(catalog)
    return actual, pool, catalog, heap, table, _definition(table)


def test_bulk_artifact_publishes_tree_roots_then_static_header_and_reopens() -> None:
    device, pool, _catalog, heap, table, definition = _stack()
    entries: list[IndexEntry] = []
    for number in range(90):
        values = (Timestamp(number), f"id-{number:04d}")
        ref = heap.insert(table, number + 1, values, xmin=10)
        entries.append(_entry(definition, ref, values))

    index = OrderedIndex(definition, pool)
    descriptor = index.create(reversed(entries), applied_through_lsn=10)

    assert descriptor.entry_count == 90
    assert index.is_created()
    assert index.open() == descriptor
    assert index.verify().entry_count == 90
    assert device.barriers[-3:] == [index.file, index.file, index.file]
    assert device.write_calls[-1] == (index.file, 0)

    cold = OrderedIndex(
        definition,
        make_pool(device, RecordingMetrics(), budget_pages=4),
    )
    candidates = cold.candidates_desc(Snapshot(10), limit=4)
    assert [entry.key for entry in candidates] == sorted(
        (entry.key for entry in entries), reverse=True
    )[:4]


def test_visible_read_skips_invisible_and_stale_exact_candidates_before_limit() -> None:
    _device, pool, _catalog, heap, table, definition = _stack()
    old_values = (Timestamp(9), "old")
    old_ref = heap.insert(table, 1, old_values, xmin=5)
    new_values = (Timestamp(8), "renamed")
    new_ref = heap.update(table, old_ref, new_values, xmin=12)
    visible_values = (Timestamp(7), "visible")
    visible_ref = heap.insert(table, 2, visible_values, xmin=6)
    future_values = (Timestamp(100), "future")
    future_ref = heap.insert(table, 3, future_values, xmin=30)

    entries = (
        _entry(definition, old_ref, old_values),
        _entry(definition, new_ref, new_values),
        _entry(definition, visible_ref, visible_values),
        _entry(definition, future_ref, future_values),
    )
    index = OrderedIndex(definition, pool)
    index.create(entries, applied_through_lsn=30)

    selected = index.visible_desc(heap, table, Snapshot(20), limit=2)

    assert [version.record_id for _ref, version in selected] == [1, 2]
    assert [ref for ref, _version in selected] == [new_ref, visible_ref]
    bounded = index.visible_desc(
        heap,
        table,
        Snapshot(20),
        upper_key=definition.key_for(new_values),
        limit=10,
    )
    assert [version.record_id for _ref, version in bounded] == [2]


def test_one_damaged_root_degrades_and_two_damaged_roots_refuse() -> None:
    device, pool, _catalog, heap, table, definition = _stack()
    values = (Timestamp(1), "one")
    ref = heap.insert(table, 1, values, xmin=4)
    index = OrderedIndex(definition, pool)
    expected = index.create((_entry(definition, ref, values),), applied_through_lsn=4)

    for root_page in (ORDERED_ROOT_PAGE_A, ORDERED_ROOT_PAGE_B):
        raw = bytearray(device.raw_page(index.file, root_page))
        raw[-1] ^= 0x5A
        device.poke_page(index.file, root_page, bytes(raw))
        if root_page == ORDERED_ROOT_PAGE_A:
            assert index.open() == expected

    with pytest.raises(GrafxCorruptionDetected) as both:
        index.open()
    assert both.value.details["field"] == "ordered_root"


def test_root_drift_retries_the_complete_candidate_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, pool, _catalog, heap, table, definition = _stack()
    entries: list[IndexEntry] = []
    for number in range(12):
        values = (Timestamp(number), str(number))
        ref = heap.insert(table, number + 1, values, xmin=7)
        entries.append(_entry(definition, ref, values))
    index = OrderedIndex(definition, pool)
    initial = index.create(entries, applied_through_lsn=7)
    original = OrderedIndex._read_certificate
    calls = 0

    def racing(candidate: OrderedIndex):  # type: ignore[no-untyped-def]
        nonlocal calls
        certificate = original(candidate)
        calls += 1
        if calls == 1:
            newer = replace(initial, generation=2)
            page = make_ordered_root_page(
                newer, ORDERED_ROOT_PAGE_B, page_size=pool.page_size
            )
            device.poke_page(index.file, ORDERED_ROOT_PAGE_B, page.to_bytes())
        return certificate

    monkeypatch.setattr(OrderedIndex, "_read_certificate", racing)

    selected = index.candidates_desc(Snapshot(7), limit=3)

    assert len(selected) == 3
    assert calls >= 4


def test_stable_root_behind_snapshot_refuses_and_wrong_table_reference_is_corruption() -> None:
    _device, pool, catalog, heap, table, definition = _stack()
    values = (Timestamp(1), "one")
    ref = heap.insert(table, 1, values, xmin=4)
    index = OrderedIndex(definition, pool)
    index.create((_entry(definition, ref, values),), applied_through_lsn=4)

    with pytest.raises(GrafxIndexError) as stale:
        index.candidates_desc(Snapshot(5), limit=1)
    assert stale.value.details["field"] == "index_view_unavailable"
    assert stale.value.retryable is True

    other = _table(catalog, name="Other")
    other_values = (Timestamp(2), "other")
    other_ref = heap.insert(other, 1, other_values, xmin=4)
    foreign_definition = _definition(table, nonce=92)
    foreign = OrderedIndex(foreign_definition, pool)
    foreign.create(
        (_entry(foreign_definition, other_ref, other_values),),
        applied_through_lsn=4,
    )

    with pytest.raises(GrafxCorruptionDetected) as mismatch:
        foreign.visible_desc(heap, table, Snapshot(4), limit=1)
    assert mismatch.value.details["field"] == "table_id"


def test_root_page_payload_round_trip_used_by_store_is_self_contained() -> None:
    """Pin the test helper's mutation to a complete valid root image, not raw byte surgery."""

    descriptor = OrderedRootDescriptor(
        artifact_nonce=1,
        generation=1,
        root_page=3,
        height=1,
        entry_count=1,
        definition_digest=bytes(16),
    )
    page = make_ordered_root_page(descriptor, ORDERED_ROOT_PAGE_A, page_size=512)
    decoded = Page.from_bytes(page.to_bytes(), page_size=512, page_index=ORDERED_ROOT_PAGE_A)
    assert decode_ordered_root_page(decoded) == descriptor
