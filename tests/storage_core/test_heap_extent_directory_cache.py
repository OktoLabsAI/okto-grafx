"""Focused checks for the defensive heap extent-directory slot memo."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import Value, ValueType
from okto_grafx.domain.page import HEADER_PAGE_INDEX, Page, PageType
from okto_grafx.engine import heap_store as heap_module
from okto_grafx.engine.buffer_pool import BufferPool, apply_page_image
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


def test_heap_structure_classifier_registration_is_idempotent_per_pool_file(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    second = HeapStore(pool, heap_store.catalog, file=heap_store.file)
    assert second.file == heap_store.file


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


def test_hot_extent_read_and_write_each_validate_and_use_one_header_pin(
    pool: BufferPool,
    heap_store: HeapStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _populate(heap_store, 1)[0]
    expected = heap_store._find_extent(table.table_id)
    assert expected is not None
    header_pins = 0
    original = BufferPool.pin

    def counted(self: BufferPool, file: str, page_index: int) -> Page:
        nonlocal header_pins
        if file == heap_store.file and page_index == HEADER_PAGE_INDEX:
            header_pins += 1
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "pin", counted)

    assert heap_store._find_extent(table.table_id) == expected
    heap_store._write_extent(expected)

    assert header_pins == 2


def test_cold_header_access_revalidates_the_operational_pin_after_bootstrap_probe(
    pool: BufferPool,
    heap_store: HeapStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _populate(heap_store, 1)[0]
    pool.flush()
    pool.invalidate(heap_store.file)
    original = HeapStore.is_bootstrapped

    def probe_then_damage(store: HeapStore) -> bool:
        bootstrapped = original(store)
        with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
            page.page_type = int(PageType.HEAP)
        return bootstrapped

    monkeypatch.setattr(HeapStore, "is_bootstrapped", probe_then_damage)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store._find_extent(table.table_id)

    assert raised.value.details["page"] == HEADER_PAGE_INDEX
    assert raised.value.details["page_type"] == int(PageType.HEAP)


def test_reserved_insert_reuses_its_just_validated_extent_once(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 1)[0]
    heap_store.observe_record_id(table, 9)
    original_find = HeapStore._find_extent
    calls = 0

    def counted_find(store: HeapStore, table_id: int) -> TableExtent | None:
        nonlocal calls
        calls += 1
        return original_find(store, table_id)

    monkeypatch.setattr(HeapStore, "_find_extent", counted_find)

    reference = heap_store.insert_reserved(table, 3, (3,), xmin=20)

    assert calls == 1
    assert any(
        found == reference and version.record_id == 3
        for found, version in heap_store.scan_all(table)
    )
    assert heap_store.next_record_id(table) == 10


def test_ordinary_insert_reuses_the_extent_that_advanced_its_identity_floor(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 1)[0]
    original_find = HeapStore._find_extent
    calls = 0

    def counted_find(store: HeapStore, table_id: int) -> TableExtent | None:
        nonlocal calls
        calls += 1
        return original_find(store, table_id)

    monkeypatch.setattr(HeapStore, "_find_extent", counted_find)

    reference = heap_store.insert(table, 2, (2,), xmin=20)

    assert calls == 1
    assert any(
        found == reference and version.record_id == 2
        for found, version in heap_store.scan_all(table)
    )
    assert heap_store.next_record_id(table) == 3


def test_first_ordinary_insert_reuses_the_extent_that_it_created(
    heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _table(1)
    original_find = HeapStore._find_extent
    calls = 0

    def counted_find(store: HeapStore, table_id: int) -> TableExtent | None:
        nonlocal calls
        calls += 1
        return original_find(store, table_id)

    monkeypatch.setattr(HeapStore, "_find_extent", counted_find)

    reference = heap_store.insert(table, 1, (1,), xmin=20)

    assert calls == 1
    assert any(
        found == reference and version.record_id == 1
        for found, version in heap_store.scan_all(table)
    )
    assert heap_store.next_record_id(table) == 2


def test_ordinary_insert_rechecks_extent_when_epoch_moves_during_observation(
    pool: BufferPool, heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 1)[0]
    pool.flush()
    original_find = HeapStore._find_extent
    original_observe = HeapStore._observe_record_id_extent
    calls = 0

    def counted_find(store: HeapStore, table_id: int) -> TableExtent | None:
        nonlocal calls
        calls += 1
        return original_find(store, table_id)

    def observe_before_cache_drop(
        store: HeapStore,
        stored_table: TableDef,
        record_id: int,
    ) -> tuple[TableExtent, bool]:
        result = original_observe(store, stored_table, record_id)
        pool.invalidate(heap_store.file)
        return result

    monkeypatch.setattr(HeapStore, "_find_extent", counted_find)
    monkeypatch.setattr(HeapStore, "_observe_record_id_extent", observe_before_cache_drop)

    reference = heap_store.insert(table, 2, (2,), xmin=20)

    assert calls == 2
    assert any(
        found == reference and version.record_id == 2
        for found, version in heap_store.scan_all(table)
    )
    assert heap_store.next_record_id(table) == 3


def test_reserved_insert_rechecks_extent_after_derived_epoch_change(
    pool: BufferPool, heap_store: HeapStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _populate(heap_store, 1)[0]
    heap_store.observe_record_id(table, 9)
    pool.flush()
    original_find = HeapStore._find_extent
    original_encode = heap_module.encode_tuple
    calls = 0

    def counted_find(store: HeapStore, table_id: int) -> TableExtent | None:
        nonlocal calls
        calls += 1
        return original_find(store, table_id)

    def encode_after_foreign_view(
        encoded_table: TableDef, values: tuple[Value, ...]
    ) -> bytes:
        pool.begin_read_view(object(), allow_writeback=False)
        return original_encode(encoded_table, values)

    monkeypatch.setattr(HeapStore, "_find_extent", counted_find)
    monkeypatch.setattr(heap_module, "encode_tuple", encode_after_foreign_view)

    reference = heap_store.insert_reserved(table, 3, (3,), xmin=20)

    assert calls == 2
    assert any(
        found == reference and version.record_id == 3
        for found, version in heap_store.scan_all(table)
    )
    assert heap_store.next_record_id(table) == 10


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


def test_a_content_only_header_image_preserves_extent_slots_and_derived_epoch(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    table = _populate(heap_store, 1)[0]
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    assert heap_store._extent_slots
    before_slots = dict(heap_store._extent_slots)
    before_slot_epoch = heap_store._extent_slots_epoch
    before_structure = pool.structure_epoch(heap_store.file)
    before_derived = heap_store._derived_read_epoch()

    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        replayed = pool.codec.decode_page(pool.codec.encode_page(page), verify=True)
    slot = before_slots[table.table_id]
    replayed.update_slot(
        slot,
        replace(
            extent,
            last_page=extent.last_page,
            page_count=extent.page_count + 7,
            next_record_id=extent.next_record_id + 100,
        ).encode(),
    )
    replayed.page_lsn += 100

    assert heap_store.apply_page_image(
        HEADER_PAGE_INDEX, pool.codec.encode_page(replayed)
    )
    assert pool.structure_epoch(heap_store.file) == before_structure
    assert heap_store._derived_read_epoch() == before_derived
    assert heap_store._extent_slots == before_slots
    assert heap_store._extent_slots_epoch == before_slot_epoch
    assert heap_store._find_extent(table.table_id) == TableExtent.decode(
        replayed.read_slot(slot)
    )


def test_a_row_content_and_csn_image_preserves_all_heap_memos(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    table = _table(1)
    ref = heap_store.insert(table, 1, (1,), xmin=10)
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    heap_store._resolve_tail(table, extent)
    before_tail = heap_store._tail_cache[table.table_id]
    before_slots = dict(heap_store._extent_slots)
    before_slot_epoch = heap_store._extent_slots_epoch
    before_structure = pool.structure_epoch(heap_store.file)
    before_derived = heap_store._derived_read_epoch()

    with pool.pinned(heap_store.file, ref.page) as page:
        replayed = pool.codec.decode_page(pool.codec.encode_page(page), verify=True)
    payload = replayed.read_slot(ref.slot)
    header = RecordHeader.decode(payload)
    replayed.update_slot(
        ref.slot,
        replace(header, xmax=20).encode() + payload[RECORD_HEADER_SIZE:],
    )
    replayed.page_lsn += 100

    assert heap_store.apply_page_image(ref.page, pool.codec.encode_page(replayed))
    assert pool.structure_epoch(heap_store.file) == before_structure
    assert heap_store._derived_read_epoch() == before_derived
    assert heap_store._tail_cache[table.table_id] == before_tail
    assert heap_store._extent_slots == before_slots
    assert heap_store._extent_slots_epoch == before_slot_epoch


def test_batch_one_appends_keep_hot_tail_and_extent_paths_after_baseline(
    pool: BufferPool,
    heap_store: HeapStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _table(1)
    heap_store.insert(table, 1, (1,), xmin=10)
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    heap_store._resolve_tail(table, extent)
    before_structure = pool.structure_epoch(heap_store.file)
    directory_walks = 0
    tail_walks = 0
    original_slots = Page.iter_slot_views
    original_visited = heap_module.visited_pages

    def count_directory_walk(page: Page) -> object:
        nonlocal directory_walks
        directory_walks += 1
        return original_slots(page)

    def count_tail_walk() -> set[int]:
        nonlocal tail_walks
        tail_walks += 1
        return original_visited()

    monkeypatch.setattr(Page, "iter_slot_views", count_directory_walk)
    monkeypatch.setattr(heap_module, "visited_pages", count_tail_walk)

    for record_id in range(2, 18):
        previous_tail = heap_store._tail_cache[table.table_id][0]
        ref = heap_store.insert(table, record_id, (record_id,), xmin=10 + record_id)
        current_tail = heap_store._tail_cache[table.table_id][0]
        for page_index in {
            HEADER_PAGE_INDEX,
            previous_tail,
            current_tail,
            ref.page,
        }:
            with pool.pinned(heap_store.file, page_index) as page:
                committed = pool.codec.decode_page(
                    pool.codec.encode_page(page), verify=True
                )
            committed.page_lsn += 1
            assert apply_page_image(
                pool,
                heap_store.file,
                page_index,
                pool.codec.encode_page(committed),
            )
        current_extent = heap_store._find_extent(table.table_id)
        assert current_extent is not None
        assert heap_store._resolve_tail(table, current_extent)[0] == current_tail

    assert pool.structure_epoch(heap_store.file) == before_structure
    assert directory_walks == 0
    assert tail_walks == 0
    assert heap_store._extent_slots[table.table_id] >= 1


@pytest.mark.parametrize("change", ("growth", "relink", "descriptor", "meta"))
def test_structural_page_images_move_the_epoch_and_revoke_extent_slots(
    pool: BufferPool, heap_store: HeapStore, change: str
) -> None:
    table = _populate(heap_store, 1)[0]
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    assert heap_store._extent_slots
    before = pool.structure_epoch(heap_store.file)

    if change == "growth":
        page_index = pool.storage.page_count(heap_store.file) + 1
        replayed = Page(
            int(PageType.HEAP), page_size=pool.page_size, page_index=page_index
        )
        replayed.insert_slot(table.table_id.to_bytes(4, "little"))
    elif change == "meta":
        page_index = HEADER_PAGE_INDEX
        with pool.pinned(heap_store.file, page_index) as page:
            replayed = pool.codec.decode_page(pool.codec.encode_page(page), verify=True)
        slot = heap_store._extent_slots[table.table_id]
        replayed.update_slot(
            slot,
            replace(extent, first_page=extent.first_page + 100).encode(),
        )
    else:
        page_index = extent.first_page
        with pool.pinned(heap_store.file, page_index) as page:
            replayed = pool.codec.decode_page(pool.codec.encode_page(page), verify=True)
        if change == "relink":
            replayed.next_page = (
                extent.first_page if replayed.next_page == NO_PAGE else NO_PAGE
            )
        else:
            replayed.update_slot(0, (table.table_id + 100).to_bytes(4, "little"))
    replayed.page_lsn += 100

    assert apply_page_image(
        pool, heap_store.file, page_index, pool.codec.encode_page(replayed)
    )
    assert pool.structure_epoch(heap_store.file) == before + 1
    heap_store._sync_extent_slots_epoch()
    assert heap_store._extent_slots == {}
    assert heap_store._extent_slots_epoch is not None


def test_a_foreign_read_view_still_revokes_heap_derived_state(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    table = _populate(heap_store, 1)[0]
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    heap_store._resolve_tail(table, extent)
    pool.flush()
    assert pool.begin_read_view("baseline", allow_writeback=False)

    # Warm every memo under the established view, then move to a foreign one.
    extent = heap_store._find_extent(table.table_id)
    assert extent is not None
    heap_store._resolve_tail(table, extent)
    heap_store._extent_slots[999_999] = 1
    old_tail = heap_store._tail_cache[table.table_id]
    before = heap_store._derived_read_epoch()

    assert pool.begin_read_view("foreign", allow_writeback=False)
    assert heap_store._derived_read_epoch() > before
    assert not heap_store._cache_is_usable(table, extent, old_tail)
    assert heap_store._find_extent(table.table_id) is not None
    assert 999_999 not in heap_store._extent_slots


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
