"""Focused checks for the defensive heap extent-directory slot memo."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import HEADER_PAGE_INDEX, Page
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore, TableExtent


def _table(table_id: int) -> TableDef:
    return TableDef(
        table_id=table_id,
        name=f"Table_{table_id}",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64),),
    )


def _populate(store: HeapStore, count: int) -> tuple[TableDef, ...]:
    tables = tuple(_table(table_id) for table_id in range(1, count + 1))
    for table in tables:
        store.insert(table, 1, (1,), xmin=10)
    return tables


def test_a_warm_extent_slot_avoids_the_directory_walk(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 8)[-1]
    expected = heap_store._find_extent(table.table_id)
    assert expected is not None

    def unexpected_walk(_page: Page) -> object:
        raise AssertionError("a warm extent lookup walked the directory")

    monkeypatch.setattr(Page, "iter_slot_views", unexpected_walk)
    assert heap_store._find_extent(table.table_id) == expected
    heap_store._write_extent(replace(expected, next_record_id=expected.next_record_id + 1))


def test_a_stale_slot_hint_falls_back_without_touching_the_wrong_table(
    heap_store: HeapStore,
) -> None:
    first, second = _populate(heap_store, 2)
    first_extent = heap_store._find_extent(first.table_id)
    second_extent = heap_store._find_extent(second.table_id)
    assert first_extent is not None
    assert second_extent is not None

    heap_store._extent_slots[first.table_id] = heap_store._extent_slots[second.table_id]
    assert heap_store._find_extent(first.table_id) == first_extent
    assert heap_store._extent_slots[first.table_id] != heap_store._extent_slots[second.table_id]

    heap_store._extent_slots[first.table_id] = heap_store._extent_slots[second.table_id]
    updated = replace(first_extent, next_record_id=first_extent.next_record_id + 7)
    heap_store._write_extent(updated)
    assert heap_store._find_extent(first.table_id) == updated
    assert heap_store._find_extent(second.table_id) == second_extent


def test_a_cached_slot_rewritten_for_another_table_is_never_overwritten(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    first, second = _populate(heap_store, 2)
    first_extent = heap_store._find_extent(first.table_id)
    second_extent = heap_store._find_extent(second.table_id)
    assert first_extent is not None
    assert second_extent is not None
    second_slot = heap_store._extent_slots[second.table_id]

    replacement = replace(first_extent, next_record_id=first_extent.next_record_id + 100)
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        page.update_slot(second_slot, replacement.encode())

    assert heap_store._find_extent(second.table_id) is None
    heap_store._extent_slots[second.table_id] = second_slot
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        before = tuple(bytes(payload) for _slot, payload in page.iter_slot_views())

    with pytest.raises(GrafxCorruptionDetected):
        heap_store._write_extent(
            replace(second_extent, next_record_id=second_extent.next_record_id + 1)
        )

    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        after = tuple(bytes(payload) for _slot, payload in page.iter_slot_views())
    assert after == before


def test_an_applied_page_image_eagerly_invalidates_extent_slots(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    table = _populate(heap_store, 1)[0]
    assert heap_store._find_extent(table.table_id) is not None
    assert heap_store._extent_slots

    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        replayed = pool.codec.decode_page(pool.codec.encode_page(page), verify=True)
    replayed.page_lsn += 100

    assert heap_store.apply_page_image(
        HEADER_PAGE_INDEX, pool.codec.encode_page(replayed)
    )
    assert heap_store._extent_slots == {}
    assert heap_store._extent_slots_epoch is None
    assert heap_store._find_extent(table.table_id) is not None


def test_epoch_and_header_proof_changes_rebuild_only_current_slot_hints(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    table = _populate(heap_store, 2)[-1]
    assert heap_store._find_extent(table.table_id) is not None
    heap_store._extent_slots[999_999] = 1

    pool.invalidate(heap_store.file)
    assert heap_store._find_extent(table.table_id) is not None
    assert 999_999 not in heap_store._extent_slots

    heap_store._extent_slots[999_999] = 1
    heap_store._bootstrapped_epoch = None
    assert heap_store._find_extent(table.table_id) is not None
    assert 999_999 not in heap_store._extent_slots


def test_a_missing_extent_still_reaches_the_corruption_route(
    heap_store: HeapStore,
) -> None:
    present = _populate(heap_store, 1)[0]
    heap_store._extent_slots[999] = heap_store._extent_slots[present.table_id]
    missing = TableExtent(table_id=999, first_page=1, last_page=1, page_count=1)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store._write_extent(missing)

    assert raised.value.details["table_id"] == 999


@pytest.mark.parametrize("table_count", (1, 12))
def test_extent_decode_count_is_constant_after_the_slot_is_warm(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch, table_count: int
) -> None:
    table = _populate(heap_store, table_count)[-1]
    assert heap_store._find_extent(table.table_id) is not None
    original = TableExtent.decode
    calls = 0

    def counted_decode(cls: type[TableExtent], raw: bytes | memoryview) -> TableExtent:
        del cls
        nonlocal calls
        calls += 1
        return original(raw)

    monkeypatch.setattr(TableExtent, "decode", classmethod(counted_decode))
    for _iteration in range(10):
        extent = heap_store._find_extent(table.table_id)
        assert extent is not None
        heap_store._write_extent(extent)

    assert calls == 20


def test_a_cold_fallback_decodes_only_the_matching_extent(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 12)[-1]
    original = TableExtent.decode
    calls = 0

    def counted_decode(cls: type[TableExtent], raw: bytes | memoryview) -> TableExtent:
        del cls
        nonlocal calls
        calls += 1
        return original(raw)

    monkeypatch.setattr(TableExtent, "decode", classmethod(counted_decode))
    heap_store._extent_slots.clear()

    assert heap_store._find_extent(table.table_id) is not None
    assert calls == 1
