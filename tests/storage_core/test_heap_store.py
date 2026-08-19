"""The heap: version chains, overflow, and a store that never decides visibility on its own.

CONTRACT.md section 8.2 puts two obligations on this store that pull in opposite directions.
An update must create a new version, chain it to the old one and end the old one, so that a
reader under an older snapshot still finds what it saw. And the store must not hide anything by
itself: scan and lookup take a snapshot and consult its predicate, which is why the same pages
answer differently to two readers without the pages changing.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import NO_PAGE, RecordRef
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.record import (
    NO_PREVIOUS_VERSION,
    RECORD_FLAG_HAS_OVERFLOW,
    RECORD_HEADER_SIZE,
    RecordHeader,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import HEADER_PAGE_INDEX, FileHeaderPage, FileKind, PageType
from okto_grafx.domain.rand import SplitMix64
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore

from .conftest import MemoryDevice, RecordingMetrics, SnapshotDouble, make_pool


def at(read_lsn: int) -> SnapshotDouble:
    """Return a snapshot positioned at that commit sequence number."""
    return SnapshotDouble(read_lsn)


# --- the file --------------------------------------------------------------------------------


def test_bootstrapping_creates_the_reserved_header_page(
    pool: BufferPool, catalog_store: CatalogStore
) -> None:
    store = HeapStore(pool, catalog_store)
    assert not store.is_bootstrapped()
    store.bootstrap()
    assert store.is_bootstrapped()
    assert pool.storage.page_count(store.file) == 1
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)
        assert FileHeaderPage.read(page).kind is FileKind.HEAP


def test_bootstrapping_twice_changes_nothing(heap_store: HeapStore) -> None:
    heap_store.bootstrap()
    heap_store.bootstrap()
    assert heap_store._pool.storage.page_count(heap_store.file) == 1


def test_working_against_a_heap_that_was_never_created_is_refused(
    pool: BufferPool, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    store = HeapStore(pool, catalog_store, file="never.dat")
    with pytest.raises(GrafxCorruptionDetected):
        store.insert(person_table, 1, (1, "Ada"), xmin=10)


def test_a_budget_too_small_for_the_heap_is_refused(
    device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    tight = make_pool(device, metrics, budget_pages=1)
    with pytest.raises(GrafxConfigurationError):
        HeapStore(tight, CatalogStore(make_pool(device, metrics)))


# --- inserting and reading ---------------------------------------------------------------------


def test_an_inserted_record_reads_back(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    assert ref.page >= 1, "no record ever lives on the reserved header page"
    assert ref.slot >= 1, "slot 0 of a data page is the page descriptor"
    version = heap_store.read(ref)
    assert version.record_id == 1
    assert version.values == (1, "Ada")
    assert version.xmin == 10
    assert version.xmax == 0
    assert version.prev is None
    assert version.live
    assert not version.deleted
    assert version.table_id == person_table.table_id
    assert version.schema_version == person_table.schema_version


def test_records_spill_onto_new_pages_that_stay_chained(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    refs = [
        heap_store.insert(person_table, index, (index, f"name-{index}"), xmin=10 + index)
        for index in range(40)
    ]
    pages = heap_store.pages_of(person_table)
    assert len(pages) > 3
    assert len({ref.page for ref in refs}) == len(pages)
    assert [ref.page for ref in refs] == sorted(ref.page for ref in refs)
    for ref, index in zip(refs, range(40)):
        assert heap_store.read(ref).values == (index, f"name-{index}")


def test_every_data_page_records_the_table_it_belongs_to(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    other = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    catalog_store.catalog.add_table(other)
    catalog_store.save()
    person = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    company = heap_store.insert(other, 1, (1,), xmin=11)
    assert person.page != company.page
    assert heap_store.read(person).table_id == person_table.table_id
    assert heap_store.read(company).table_id == other.table_id
    assert set(heap_store.pages_of(person_table)).isdisjoint(heap_store.pages_of(other))


def test_reading_the_reserved_header_page_as_a_record_is_refused(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.read(RecordRef(page=HEADER_PAGE_INDEX, slot=1))


def test_reading_the_page_descriptor_as_a_record_is_refused(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.read(RecordRef(page=ref.page, slot=0))


def test_a_tuple_that_does_not_match_the_table_never_reaches_a_page(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    before = heap_store._pool.storage.page_count(heap_store.file)
    with pytest.raises(SchemaMismatchError):
        heap_store.insert(person_table, 1, (1, 2), xmin=10)
    with pytest.raises(SchemaMismatchError):
        heap_store.insert(person_table, 1, (1,), xmin=10)
    assert heap_store._pool.storage.page_count(heap_store.file) == before


def test_a_version_written_under_another_schema_version_is_refused(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    evolved = TableDef(
        table_id=person_table.table_id,
        name=person_table.name,
        kind="node",
        columns=person_table.columns,
        primary_key=person_table.primary_key,
        schema_version=2,
    )
    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        heap_store._decode_version(evolved, heap_store._read_slot(ref)[1])
    assert raised.value.details["stored_schema_version"] == 1
    assert raised.value.details["current_schema_version"] == 2


# --- visibility is the caller decision ------------------------------------------------------


def test_a_scan_shows_a_version_only_to_a_snapshot_that_can_see_it(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    for index in range(5):
        heap_store.insert(person_table, index, (index, f"n{index}"), xmin=10 * (index + 1))
    assert len(list(heap_store.scan(person_table, at(0)))) == 0
    assert len(list(heap_store.scan(person_table, at(10)))) == 1
    assert len(list(heap_store.scan(person_table, at(35)))) == 3
    assert len(list(heap_store.scan(person_table, at(1000)))) == 5


def test_scan_all_shows_what_a_snapshot_hides(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    heap_store.delete(person_table, ref, xmax=20)
    assert list(heap_store.scan(person_table, at(50))) == []
    every = list(heap_store.scan_all(person_table))
    assert len(every) == 1
    assert every[0][1].xmax == 20
    assert every[0][1].deleted


def test_the_store_holds_no_opinion_about_visibility(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)

    class NothingIsVisible:
        def visible(self, xmin: int, xmax: int) -> bool:
            return False

    class EverythingIsVisible:
        def visible(self, xmin: int, xmax: int) -> bool:
            return True

    assert list(heap_store.scan(person_table, NothingIsVisible())) == []
    assert len(list(heap_store.scan(person_table, EverythingIsVisible()))) == 1


def test_a_scan_of_a_table_with_no_page_yields_nothing(
    heap_store: HeapStore, catalog_store: CatalogStore
) -> None:
    empty = TableDef(
        table_id=99,
        name="Empty",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64),),
    )
    assert list(heap_store.scan(empty, at(100))) == []
    assert heap_store.pages_of(empty) == ()
    assert heap_store.lookup(empty, 1, at(100)) is None


def test_lookup_returns_the_version_the_snapshot_can_see(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 7, (7, "Ada"), xmin=10)
    heap_store.update(person_table, ref, (7, "Ada Lovelace"), xmin=20)
    assert heap_store.lookup(person_table, 7, at(15)).values == (7, "Ada")
    assert heap_store.lookup(person_table, 7, at(25)).values == (7, "Ada Lovelace")
    assert heap_store.lookup(person_table, 7, at(5)) is None
    assert heap_store.lookup(person_table, 999, at(100)) is None


# --- version chains --------------------------------------------------------------------------


def test_an_update_writes_a_new_version_and_ends_the_old_one(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    first = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    second = heap_store.update(person_table, first, (1, "Ada L"), xmin=20)
    assert second != first
    old = heap_store.read(first)
    new = heap_store.read(second)
    assert old.xmax == 20, "the old version ends exactly where the new one begins"
    assert not old.deleted, "an update is not a delete"
    assert new.xmin == 20
    assert new.xmax == 0
    assert new.prev == first
    assert new.record_id == old.record_id == 1
    assert new.values == (1, "Ada L")


def test_a_chain_of_updates_is_walkable_backwards(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    chain = [ref]
    for step in range(1, 6):
        ref = heap_store.update(person_table, ref, (1, f"v{step}"), xmin=10 * (step + 1))
        chain.append(ref)
    assert heap_store.version_chain(ref) == tuple(reversed(chain))
    for step, link in enumerate(reversed(chain)):
        assert heap_store.read(link).values == (1, f"v{5 - step}")
    assert heap_store.read(chain[0]).prev is None


def test_the_end_of_a_chain_is_the_literal_zero(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    _table_id, content = heap_store._read_slot(ref)
    assert RecordHeader.decode(content).prev_version == NO_PREVIOUS_VERSION == 0
    later = heap_store.update(person_table, ref, (1, "Ada L"), xmin=20)
    _table_id, content = heap_store._read_slot(later)
    assert RecordHeader.decode(content).prev_version == ref.encode()
    assert RecordHeader.decode(content).prev_version != 0


def test_every_snapshot_along_a_chain_sees_exactly_one_version(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    for step in range(1, 5):
        ref = heap_store.update(person_table, ref, (1, f"v{step}"), xmin=10 * (step + 1))
    for read_lsn in range(10, 60):
        visible = [
            version for _ref, version in heap_store.scan(person_table, at(read_lsn))
        ]
        assert len(visible) == 1, read_lsn
        assert visible[0].values == (1, f"v{min((read_lsn - 10) // 10, 4)}")


def test_updating_a_version_that_already_ended_is_refused(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    heap_store.update(person_table, ref, (1, "Ada L"), xmin=20)
    with pytest.raises(GrafxTransactionStateError):
        heap_store.update(person_table, ref, (1, "Ada M"), xmin=30)


def test_a_delete_ends_the_version_and_marks_it(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    heap_store.delete(person_table, ref, xmax=20)
    version = heap_store.read(ref)
    assert version.xmax == 20
    assert version.deleted
    assert not version.live
    assert version.values == (1, "Ada"), "a delete leaves the payload in place"
    assert heap_store.lookup(person_table, 1, at(15)).values == (1, "Ada")
    assert heap_store.lookup(person_table, 1, at(25)) is None


def test_deleting_twice_is_refused(heap_store: HeapStore, person_table: TableDef) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    heap_store.delete(person_table, ref, xmax=20)
    with pytest.raises(GrafxTransactionStateError):
        heap_store.delete(person_table, ref, xmax=30)


def test_ending_a_version_at_zero_is_refused(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pytest.raises(GrafxTransactionStateError):
        heap_store.delete(person_table, ref, xmax=0)


def test_deleting_the_last_version_of_a_chain_hides_the_whole_record(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    ref = heap_store.update(person_table, ref, (1, "v1"), xmin=20)
    heap_store.delete(person_table, ref, xmax=30)
    assert heap_store.lookup(person_table, 1, at(25)).values == (1, "v1")
    assert heap_store.lookup(person_table, 1, at(35)) is None
    assert len(heap_store.version_chain(ref)) == 2


def test_a_version_of_another_table_is_refused(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    other = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    catalog_store.catalog.add_table(other)
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.delete(other, ref, xmax=20)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.update(other, ref, (2,), xmin=20)


def test_a_version_chain_that_loops_is_reported(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    first = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    second = heap_store.update(person_table, first, (1, "v1"), xmin=20)
    with heap_store._pool.pinned(heap_store.file, first.page) as page:
        content = bytearray(page.read_slot(first.slot))
        header = RecordHeader.decode(bytes(content))
        looped = RecordHeader(
            record_id=header.record_id,
            xmin=header.xmin,
            xmax=header.xmax,
            prev_version=second.encode(),
            payload_len=header.payload_len,
            schema_version=header.schema_version,
            flags=header.flags,
        )
        page.update_slot(first.slot, looped.encode() + bytes(content[RECORD_HEADER_SIZE:]))
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.version_chain(second)


# --- overflow ---------------------------------------------------------------------------------


def test_a_payload_larger_than_a_page_is_stored_in_a_chain(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    long_name = "n" * 4000
    ref = heap_store.insert(person_table, 1, (1, long_name), xmin=10)
    _table_id, content = heap_store._read_slot(ref)
    header = RecordHeader.decode(content)
    assert header.has_overflow
    assert header.flags & RECORD_FLAG_HAS_OVERFLOW
    assert len(content) == RECORD_HEADER_SIZE + 4, "only the first chain page stays inline"
    assert header.payload_len > heap_store.inline_capacity
    assert heap_store.read(ref).values == (1, long_name)


def test_an_overflowed_record_takes_part_in_scans_and_chains(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    payload = "z" * 5000
    ref = heap_store.insert(person_table, 1, (1, payload), xmin=10)
    later = heap_store.update(person_table, ref, (1, payload + "!"), xmin=20)
    visible = [version for _ref, version in heap_store.scan(person_table, at(50))]
    assert len(visible) == 1
    assert visible[0].values == (1, payload + "!")
    assert heap_store.read(ref).values == (1, payload)
    assert heap_store.version_chain(later) == (later, ref)


def test_a_record_exactly_at_the_inline_limit_stays_inline(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    # The payload is an INT64 (9 bytes) plus a STRING (5 bytes of framing) plus its characters.
    fits = heap_store.inline_capacity - RECORD_HEADER_SIZE - 9 - 5
    ref = heap_store.insert(person_table, 1, (1, "a" * fits), xmin=10)
    _table_id, content = heap_store._read_slot(ref)
    assert not RecordHeader.decode(content).has_overflow
    assert len(content) == heap_store.inline_capacity
    assert heap_store.read(ref).values == (1, "a" * fits)

    spills = heap_store.insert(person_table, 2, (2, "a" * (fits + 1)), xmin=11)
    _table_id, content = heap_store._read_slot(spills)
    assert RecordHeader.decode(content).has_overflow
    assert heap_store.read(spills).values == (2, "a" * (fits + 1))


def test_an_overflow_chain_survives_a_flush_and_a_cold_read(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    payload = "".join(chr(97 + index % 26) for index in range(6000))
    ref = heap_store.insert(person_table, 1, (1, payload), xmin=10)
    pool.flush()
    pool.invalidate()
    assert heap_store.read(ref).values == (1, payload)


# --- durability and pressure ------------------------------------------------------------------


def test_the_heap_survives_a_pool_that_can_hold_only_two_pages(
    device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    tight = make_pool(device, metrics, budget_pages=2, db_label="tight")
    catalog = CatalogStore(tight)
    catalog.bootstrap()
    table = TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()
    store = HeapStore(tight, catalog)
    store.bootstrap()
    refs = [store.insert(table, index, (index, f"n{index}"), xmin=10) for index in range(30)]
    assert len({ref.page for ref in refs}) > 1
    assert len(list(store.scan(table, at(100)))) == 30
    long_ref = store.insert(table, 99, (99, "L" * 3000), xmin=11)
    assert store.read(long_ref).values[1] == "L" * 3000


def test_the_whole_heap_survives_a_cold_reopen(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    generator = SplitMix64(20260819)
    written: dict[int, tuple[int, str]] = {}
    for record_id in range(60):
        length = generator.next_below(120)
        values = (record_id, "v" * length)
        heap_store.insert(person_table, record_id, values, xmin=10)
        written[record_id] = values
    pool.flush()
    pool.invalidate()
    reread = {
        version.record_id: version.values
        for _ref, version in heap_store.scan(person_table, at(100))
    }
    assert reread == written


def test_two_heaps_over_two_databases_stay_independent(
    metrics: RecordingMetrics, person_table: TableDef
) -> None:
    stores = []
    for label in ("alpha", "beta"):
        pool = make_pool(MemoryDevice(), RecordingMetrics(), db_label=label)
        catalog = CatalogStore(pool)
        catalog.bootstrap()
        catalog.catalog.add_table(person_table)
        catalog.save()
        store = HeapStore(pool, catalog)
        store.bootstrap()
        stores.append(store)
    stores[0].insert(person_table, 1, (1, "only in alpha"), xmin=10)
    assert len(list(stores[0].scan(person_table, at(100)))) == 1
    assert len(list(stores[1].scan(person_table, at(100)))) == 0


def test_a_page_chain_that_loops_is_reported(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    for index in range(40):
        heap_store.insert(person_table, index, (index, f"n{index}"), xmin=10)
    pages = heap_store.pages_of(person_table)
    assert len(pages) > 2
    with heap_store._pool.pinned(heap_store.file, pages[-1]) as page:
        assert page.next_page == NO_PAGE
        page.next_page = pages[0]
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.pages_of(person_table)
    with pytest.raises(GrafxCorruptionDetected):
        list(heap_store.scan(person_table, at(100)))


def test_the_repr_says_what_fits_inline(heap_store: HeapStore) -> None:
    assert "heap.dat" in repr(heap_store)
    assert str(heap_store.inline_capacity) in repr(heap_store)


def test_the_table_directory_of_one_heap_file_is_bounded_and_says_so(
    pool: BufferPool, catalog_store: CatalogStore
) -> None:
    from okto_grafx.domain.errors import GrafxUnsupportedOperation

    store = HeapStore(pool, catalog_store)
    store.bootstrap()
    assert store.max_tables == (512 - 32 - 28 - 4) // 20
    tables = [
        TableDef(
            table_id=index,
            name=f"Table_{index}",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64),),
        )
        for index in range(1, store.max_tables + 2)
    ]
    for table in tables[: store.max_tables]:
        store.insert(table, 1, (1,), xmin=10)
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        store.insert(tables[store.max_tables], 1, (1,), xmin=10)
    assert raised.value.details["max_tables"] == store.max_tables
    assert raised.value.details["table"] == tables[store.max_tables].name
    # Every table that did fit is still readable, so the refusal changed nothing else.
    for table in tables[: store.max_tables]:
        assert len(list(store.scan(table, at(100)))) == 1
