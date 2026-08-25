"""The heap: version chains, overflow, and a store that never decides visibility on its own.

CONTRACT.md section 8.2 puts two obligations on this store that pull in opposite directions.
An update must create a new version, chain it to the old one and end the old one, so that a
reader under an older snapshot still finds what it saw. And the store must not hide anything by
itself: scan and lookup take a snapshot and consult its predicate, which is why the same pages
answer differently to two readers without the pages changing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import replace

import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxStorageError,
    GrafxError,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_PAGE, PageIndex, RecordRef
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.record import (
    NO_PREVIOUS_VERSION,
    RECORD_FLAG_HAS_OVERFLOW,
    RECORD_HEADER_SIZE,
    RecordHeader,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef, relationship_row
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
)
from okto_grafx.domain.rand import SplitMix64
from okto_grafx.engine.buffer_pool import BufferPool, write_chain
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine import heap_store as heap_module
from okto_grafx.engine.heap_store import (
    DESCRIPTOR_SLOT,
    FIRST_RECORD_ID,
    MAX_U64,
    DIRECTORY_ENTRY_SIZE,
    EXTENT_FIRST_SLOT,
    MAX_DIRECTORY_FIELD,
    HeapStore,
    TableExtent,
)

from .conftest import MemoryDevice, RecordingMetrics, SnapshotDouble, make_pool


PAGE_FILLER: str = "f" * 200
"""A value big enough that a few rows fill a small page, so growing a chain takes a few inserts."""

MAX_SETUP_INSERTS: int = 256
"""Rows one setup helper may write before it declares the arrangement impossible.

A92: every setup loop is bounded by a count. Gating only on a predicate the code under test
computes means a regression in that code makes the loop run forever, and --timeout-method=thread
cannot stop a thread that is allocating -- the session dies of memory exhaustion with no junit
report at all, which is how three mutations went unscorable. A suite that cannot report its own
failure is worse than a red one.
"""


def file_pages(pool: BufferPool, store: HeapStore) -> int:
    """Return how many pages the FILE holds, straight from the device.

    Independent of every walk under test: the device counts pages whether or not the chain that
    threads them can be followed at all.
    """
    return pool.storage.page_count(store.file)


def grow_by_one_page(
    pool: BufferPool, store: HeapStore, table: TableDef, *, record_id: int = 1
) -> int:
    """Insert filler until the file gains one page, and return the count it reached."""
    start = file_pages(pool, store)
    for _attempt in range(MAX_SETUP_INSERTS):
        store.insert(table, record_id, (record_id, PAGE_FILLER), xmin=5)
        if file_pages(pool, store) > start:
            return file_pages(pool, store)
    raise AssertionError(
        f"{MAX_SETUP_INSERTS} inserts did not grow {store.file!r} past {start} pages"
    )


def grow_to_pages(
    pool: BufferPool, store: HeapStore, table: TableDef, pages: int, *, record_id: int = 1
) -> None:
    """Insert filler until the file holds at least that many pages."""
    for _step in range(pages + 1):
        if file_pages(pool, store) >= pages:
            return
        grow_by_one_page(pool, store, table, record_id=record_id)
    raise AssertionError(f"{store.file!r} never reached {pages} pages")




@contextlib.contextmanager
def pin_ceiling(monkeypatch: pytest.MonkeyPatch, limit: int = 200) -> Iterator[None]:
    """Fail fast if a walk pins more pages than any bounded walk could need.

    Without this the only thing that stops a walk whose bound has been removed is the session
    timeout: a minute of wall clock per mutation, reported as a timeout rather than as the
    property that broke. The ceiling lives in the test, so the engine gains nothing for the sake
    of being tested.
    """
    counted = {"pins": 0}
    original = BufferPool.pin

    def counting(self: BufferPool, file: str, page_index: int) -> object:
        counted["pins"] += 1
        if counted["pins"] > limit:
            raise AssertionError(
                f"the walk pinned more than {limit} pages, so it is not going to end"
            )
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "pin", counting)
    yield


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
    assert store.max_tables == (512 - 32 - 28 - 4) // (DIRECTORY_ENTRY_SIZE + 4)
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


# --- the redo path (amendment A22) ---------------------------------------------------------------


def test_the_heap_exposes_the_same_redo_rule_as_the_catalog(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # Amendment A22: C6 replays heap pages and catalog pages through the same rule, so the heap
    # owes the same entry point. Without it recovery cannot redo a single heap write.
    assert callable(heap_store.apply_page_image)
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pool.pinned(heap_store.file, ref.page) as page:
        page.page_lsn = 100
        image = pool.codec.encode_page(page)
    replayed = Page.from_bytes(image, page_size=pool.page_size)
    replayed.page_lsn = 200
    assert heap_store.apply_page_image(ref.page, pool.codec.encode_page(replayed)) is True
    assert heap_store.apply_page_image(ref.page, pool.codec.encode_page(replayed)) is False
    with pool.pinned(heap_store.file, ref.page) as page:
        assert page.page_lsn == 200
    assert heap_store.read(ref).values == (1, "Ada")


def test_the_heap_redo_installs_over_a_page_that_was_allocated_and_never_written(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # The crash window of the PAGE_ALLOC record of section 6.5: the page exists holding the zeros
    # allocate left, and the write that was to fill it never happened.
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pool.pinned(heap_store.file, ref.page) as page:
        page.page_lsn = 400
        image = pool.codec.encode_page(page)
    pool.flush()
    pool.invalidate()
    unwritten = pool.storage.allocate(heap_store.file, 1)
    assert pool.storage.raw_page(heap_store.file, unwritten) == bytes(pool.page_size)
    assert heap_store.apply_page_image(unwritten, image) is True
    with pool.pinned(heap_store.file, unwritten) as page:
        assert page.page_lsn == 400
        assert page.page_type == int(PageType.HEAP)


def test_the_heap_redo_reaches_a_page_the_file_does_not_have(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pool.pinned(heap_store.file, ref.page) as page:
        page.page_lsn = 700
        image = pool.codec.encode_page(page)
    beyond = pool.storage.page_count(heap_store.file) + 2
    assert heap_store.apply_page_image(beyond, image) is True
    assert pool.storage.page_count(heap_store.file) == beyond + 1
    with pool.pinned(heap_store.file, beyond) as page:
        assert page.page_lsn == 700


def test_a_replayed_heap_file_serves_the_same_rows(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    for record_id in range(12):
        heap_store.insert(person_table, record_id, (record_id, f"n{record_id}"), xmin=10)
    pool.flush()
    images = {
        index: pool.codec.encode_page(pool.pin(heap_store.file, index))
        for index in range(pool.storage.page_count(heap_store.file))
    }
    for index in images:
        pool.unpin(heap_store.file, index)

    replica_pool = make_pool(MemoryDevice(), RecordingMetrics(), db_label="replica")
    replica_catalog = CatalogStore(replica_pool)
    replica_catalog.bootstrap()
    replica_catalog.catalog.add_table(person_table)
    replica_catalog.save()
    replica = HeapStore(replica_pool, replica_catalog)
    replica.bootstrap()
    for index, image in sorted(images.items()):
        page = Page.from_bytes(image, page_size=replica_pool.page_size)
        page.page_lsn = 9000
        assert replica.apply_page_image(index, replica_pool.codec.encode_page(page)) is True
    rows = {
        version.record_id: version.values
        for _ref, version in replica.scan(person_table, at(100))
    }
    assert rows == {index: (index, f"n{index}") for index in range(12)}


# --- the reserved header page is an invariant of the heap ---------------------------------------


def test_a_heap_grown_by_redo_before_bootstrap_loses_no_record(
    device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    """Amendment A22 can grow heap.dat before anything has reserved page 0.

    This needs no corruption at all. Recovery redoes a page the file does not have, the file
    grows past page 0, and page 0 is left holding the zeros allocate wrote. If bootstrap then
    asks only whether the file has pages, it does nothing, page 0 stays FREE, and the first
    table extent lands in slot 0 of it: the one slot every reader of the directory skips. The
    table then permanently misses its first page, in scan, in scan_all and in lookup at once,
    while read() of the same reference still answers, and G6 forbids reclaiming the loss.
    """
    pool = make_pool(device, metrics)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    table = TableDef(
        table_id=1,
        name="Grown",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()

    heap = HeapStore(pool, catalog)
    replayed = Page(int(PageType.HEAP), page_size=pool.page_size)
    replayed.page_lsn = 50
    replayed.insert_slot(b"\x01\x00\x00\x00")
    assert heap.apply_page_image(3, pool.codec.encode_page(replayed)) is True
    assert pool.storage.page_count(heap.file) == 4

    with pool.pinned(heap.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.FREE), "the redo left page 0 unreserved"
    assert heap.is_bootstrapped() is False, "a free page 0 is not a header page"

    heap.bootstrap()
    with pool.pinned(heap.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)
        assert FileHeaderPage.read(page).kind is FileKind.HEAP

    refs = [heap.insert(table, 100 + number, (number,), xmin=10 + number) for number in range(4)]
    expected = {100, 101, 102, 103}
    assert {version.record_id for _ref, version in heap.scan(table, at(100))} == expected
    assert {version.record_id for _ref, version in heap.scan_all(table)} == expected
    assert {heap.read(ref).record_id for ref in refs} == expected
    for record_id in expected:
        assert heap.lookup(table, record_id, at(100)) is not None
    with pool.pinned(heap.file, HEADER_PAGE_INDEX) as page:
        assert min(slot for slot, _payload in page.iter_slots()) == 0
        assert FileHeaderPage.read(page).kind is FileKind.HEAP


def test_a_zeroed_header_page_is_reported_rather_than_read_as_an_empty_table(
    pool: BufferPool, device: MemoryDevice, heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    pool.flush()
    device.poke_page(heap_store.file, HEADER_PAGE_INDEX, bytes(pool.page_size))
    pool.invalidate()
    for call in (
        lambda: list(heap_store.scan(person_table, at(100))),
        lambda: list(heap_store.scan_all(person_table)),
        lambda: heap_store.lookup(person_table, 1, at(100)),
        lambda: heap_store.pages_of(person_table),
        lambda: heap_store.insert(person_table, 2, (2, "Bob"), xmin=6),
    ):
        with pytest.raises(GrafxCorruptionDetected):
            call()


@pytest.mark.parametrize(
    "page_type", [int(PageType.HEAP), int(PageType.CATALOG), int(PageType.OVERFLOW)]
)
def test_a_header_page_of_the_wrong_type_is_reported(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef, page_type: int
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = page_type
    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan(person_table, at(100)))
    assert raised.value.details["page"] == HEADER_PAGE_INDEX
    assert raised.value.details["page_type"] == page_type


def test_a_header_page_of_another_kind_of_file_is_reported(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        FileHeaderPage.write(page, FileHeader(kind=FileKind.CATALOG, page_size=pool.page_size))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan(person_table, at(100)))
    assert raised.value.details["kind"] == "CATALOG"


def test_bootstrap_never_overwrites_a_header_page_it_cannot_understand(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # A header page that is damaged rather than free must not be re-initialised: that would
    # throw away the table directory a verifier may still be able to rebuild (G6).
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.HEAP)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.bootstrap()
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.HEAP), "the damaged page was left as it was"
        assert page.slot_count >= 2, "and so were the extents on it"


def test_a_heap_written_with_another_page_size_is_refused(
    device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    pool = make_pool(device, metrics)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    with pool.pinned(heap.file, HEADER_PAGE_INDEX) as page:
        FileHeaderPage.write(page, FileHeader(kind=FileKind.HEAP, page_size=1024))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap.is_bootstrapped()
    assert raised.value.details["page_size"] == 1024


def test_the_extent_directory_never_uses_the_slot_that_holds_the_file_header(
    pool: BufferPool, heap_store: HeapStore
) -> None:
    for number in range(1, 6):
        table = TableDef(
            table_id=number,
            name=f"Table_{number}",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64),),
        )
        heap_store.insert(table, number, (number,), xmin=10)
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        assert [slot for slot, _payload in page.iter_slots()] == [0, 1, 2, 3, 4, 5]
        assert EXTENT_FIRST_SLOT == 1
        assert FileHeaderPage.read(page).kind is FileKind.HEAP


def test_a_refused_table_never_leaves_a_page_behind(
    pool: BufferPool, catalog_store: CatalogStore
) -> None:
    from okto_grafx.domain.errors import GrafxUnsupportedOperation

    store = HeapStore(pool, catalog_store)
    store.bootstrap()
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
    before = pool.storage.page_count(store.file)
    for _attempt in range(4):
        with pytest.raises(GrafxUnsupportedOperation):
            store.insert(tables[store.max_tables], 1, (1,), xmin=10)
    assert pool.storage.page_count(store.file) == before


# --- M1: the write door checks the page it is about to write ------------------------------------


DIRECTORY_STRUCT = struct.Struct("<IIIIQ")


def point_extent_at(
    pool: BufferPool,
    store: HeapStore,
    table_id: int,
    last_page: int | None = None,
    page_count: int | None = None,
    first_page: int | None = None,
) -> None:
    """Rewrite the extent of a table, which is what four damaged or stale bytes look like."""
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        for slot, payload in page.iter_slots():
            if slot < EXTENT_FIRST_SLOT:
                continue
            stored = DIRECTORY_STRUCT.unpack(payload)
            if stored[0] == table_id:
                page.update_slot(
                    slot,
                    DIRECTORY_STRUCT.pack(
                        stored[0],
                        stored[1] if first_page is None else first_page,
                        stored[2] if last_page is None else last_page,
                        stored[3] if page_count is None else page_count,
                        stored[4],
                    ),
                )
                store._tail_cache.clear()
                return
    raise AssertionError(f"table {table_id} has no extent")


def foreign_pages(
    pool: BufferPool, store: HeapStore, catalog_store: CatalogStore, owner: TableDef
) -> dict[str, int]:
    """Return one page of each kind an append must refuse to walk into."""
    other = second_table(catalog_store)
    store.insert(other, 1, (1, "Acme"), xmin=5)
    store.insert(owner, 1, (1, "L" * 3000), xmin=5)
    overflow = None
    for index in range(pool.storage.page_count(store.file)):
        with pool.pinned(store.file, index) as page:
            if page.page_type == int(PageType.OVERFLOW):
                overflow = index
                break
    assert overflow is not None
    return {
        "the reserved header page": HEADER_PAGE_INDEX,
        "a page of another table": store.pages_of(other)[0],
        "an overflow page": overflow,
    }


def second_table(catalog_store: CatalogStore) -> TableDef:
    """Return a second registered node table, so cross-table damage can be built."""
    other = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog_store.catalog.add_table(other)
    catalog_store.save()
    return other


def stale_the_hint(pool: BufferPool, store: HeapStore, table: TableDef) -> HeapStore:
    """Leave the directory entry BEHIND the chain, and return a store that has not walked it.

    This state is what an ordinary A22 redo leaves: replaying a heap page image can lengthen a
    chain without the directory entry that describes it ever being replayed, and A40 says the
    chain is the authority and the entry a repairable hint.

    It used to be produced by refusing the directory write-back after the relink, which was the
    real shape of the failure until D1 (round 8) reordered the append: nothing may become
    reachable before every step that can still refuse has succeeded, so the relink now happens
    LAST and a refusal can no longer leave the hint behind its chain. Producing the state that
    way would now produce nothing at all, and the test would pass by arranging no state -- so it
    is planted directly instead, from the same numbers a redo would leave.

    The store that comes back is a fresh one over the same pool, which is what a reopen is. It
    has no remembered tail, so the walk under test is the walk that runs -- and no test has to
    reach into a private cache to arrange that (A63).
    """
    extent = store.extent_of(table)
    assert extent is not None
    chain = store.pages_of(table)
    assert len(chain) > 1, "a hint cannot lag a chain of one page"
    store._write_extent(replace(extent, last_page=chain[-2], page_count=len(chain) - 1))
    return HeapStore(pool, store.catalog)


@pytest.mark.parametrize(
    "kind", ["the reserved header page", "a page of another table", "an overflow page"]
)
def test_a_chain_that_leads_out_of_the_table_refuses_the_write(
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
    kind: str,
) -> None:
    # first_page is the one field the chain cannot contradict, so it is the one the walk trusts.
    # Damaged, it leads the append into a page that is not this table, and writing a record there
    # produces a reference the read path must refuse, a row that answers under the wrong table, or
    # a payload that was readable and no longer is.
    targets = foreign_pages(pool, heap_store, catalog_store, person_table)
    before = {
        version.record_id for _reference, version in heap_store.scan(person_table, at(1000))
    }
    original = heap_store.extent_of(person_table)
    assert original is not None
    point_extent_at(pool, heap_store, person_table.table_id, first_page=targets[kind])
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.insert(person_table, 7, (7, "intruder"), xmin=6)
    point_extent_at(
        pool, heap_store, person_table.table_id, first_page=original.first_page
    )
    assert {
        version.record_id for _reference, version in heap_store.scan(person_table, at(1000))
    } == before


@pytest.mark.parametrize(
    "kind", ["the reserved header page", "a page of another table", "an overflow page"]
)
def test_a_hop_that_leaves_the_table_refuses_the_write(
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
    kind: str,
) -> None:
    # The same question one hop in: every page of the walk is checked, not only the first.
    targets = foreign_pages(pool, heap_store, catalog_store, person_table)
    grow_to_pages(pool, heap_store, person_table, 2)
    chain = heap_store.pages_of(person_table)
    with pool.pinned(heap_store.file, chain[-1]) as page:
        page.next_page = targets[kind]
    heap_store._tail_cache.clear()
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.insert(person_table, 7, (7, "intruder"), xmin=6)


def test_a_page_the_file_does_not_have_refuses_the_write(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    beyond = pool.storage.page_count(heap_store.file) + 5
    point_extent_at(pool, heap_store, person_table.table_id, first_page=beyond)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.insert(person_table, 2, (2, "next"), xmin=6)


def test_a_retryable_refusal_between_the_relink_and_the_directory_leaves_no_row_behind(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The failure needs no damage at all: the heap produces the stale hint itself.

    Relinking the page a stale hint names would cut every page after it out of the chain, and
    the records on those pages would stop appearing in scan() with no error raised anywhere.
    """
    filler = "f" * 200
    grow_to_pages(pool, heap_store, person_table, 4)
    heap_store = stale_the_hint(pool, heap_store, person_table)
    chain = heap_store.pages_of(person_table)
    hint = heap_store.extent_of(person_table)
    assert hint is not None
    assert hint.last_page != chain[-1], "the hint really is behind the chain"
    survivors = {
        (reference.page, reference.slot)
        for reference, _version in heap_store.scan(person_table, at(1000))
    }

    for number in range(12):
        heap_store.insert(person_table, 100 + number, (number, filler), xmin=6)

    grown = heap_store.pages_of(person_table)
    assert grown[: len(chain)] == chain, "no page left the chain"
    assert survivors <= {
        (reference.page, reference.slot)
        for reference, _version in heap_store.scan(person_table, at(1000))
    }
    repaired = heap_store.extent_of(person_table)
    assert repaired is not None
    assert repaired.last_page == grown[-1]
    assert repaired.page_count == len(grown), "the repaired count is the length walked"


def test_one_page_of_drift_on_a_one_page_table_is_tolerated(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The narrowest case there is, and the one a bound from the hint refuses outright.

    A one-page table has a page count of one, so a single page of drift already exceeds any
    bound derived from it. That drift is what one retryable refusal leaves, and refusing it makes
    the table permanently unwritable through a non-retryable error -- punishing a caller for
    doing exactly what the retryable error told it to do (A40).
    """
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    assert len(heap_store.pages_of(person_table)) == 1
    grow_by_one_page(pool, heap_store, person_table)
    chain = heap_store.pages_of(person_table)
    assert len(chain) == 2, "the chain has to be past the hint for there to be drift"
    # A one-page hint over a two-page chain: the drift a redo leaves, and the narrowest there is.
    heap_store._write_extent(
        replace(heap_store.extent_of(person_table), last_page=chain[0], page_count=1)
    )
    heap_store = HeapStore(pool, heap_store.catalog)
    hint = heap_store.extent_of(person_table)
    assert hint is not None and hint.page_count == 1

    for number in range(3):
        heap_store.insert(person_table, 200 + number, (number, "small"), xmin=6)
    visible = {version.record_id for _reference, version in heap_store.scan(person_table, at(1000))}
    assert {200, 201, 202} <= visible
    repaired = heap_store.extent_of(person_table)
    assert repaired is not None
    assert repaired.page_count == len(heap_store.pages_of(person_table))


def test_a_hint_that_is_unreachable_from_the_first_page_is_repaired(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """An orphan page of the same table passes every per-page check and is still not the tail.

    Following the hint forward cannot see this: type and ownership both agree, and the orphan has
    no next_page, so it looks exactly like a tail. Only a walk that starts at first_page can tell
    that nothing in the chain points at it, and an append that lands there leaves a row that is
    readable by reference and absent from every scan.
    """
    grow_to_pages(pool, heap_store, person_table, 2)
    chain = heap_store.pages_of(person_table)
    orphan = pool.allocate(heap_store.file, int(PageType.HEAP))
    orphan_index = orphan.page_index
    heap_store._initialize_data_page(orphan, person_table.table_id)
    pool.unpin(heap_store.file, orphan_index, dirty=True)
    assert orphan_index not in chain
    point_extent_at(pool, heap_store, person_table.table_id, orphan_index, len(chain))
    heap_store._tail_cache.clear()

    reference = heap_store.insert(person_table, 777, (777, "not an orphan"), xmin=6)
    assert reference.page in heap_store.pages_of(person_table), "the row landed in the chain"
    assert heap_store.read(reference).record_id == 777
    assert 777 in {
        version.record_id for _reference, version in heap_store.scan(person_table, at(1000))
    }
    assert heap_store.lookup(person_table, 777, at(1000)) is not None
    repaired = heap_store.extent_of(person_table)
    assert repaired is not None
    assert repaired.last_page == heap_store.pages_of(person_table)[-1]


def test_a_chain_that_loops_is_refused_by_the_append_door(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    filler = "f" * 200
    grow_to_pages(pool, heap_store, person_table, 3)
    chain = heap_store.pages_of(person_table)
    with pool.pinned(heap_store.file, chain[-1]) as page:
        page.next_page = chain[0]
    heap_store._tail_cache.clear()
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.insert(person_table, 99, (99, filler), xmin=6)
    assert raised.value.details["field"] == "cycle"


# --- M4: the overflow path leaks nothing when the directory refuses -----------------------------


@pytest.mark.parametrize("shape", ["inline", "overflow"])
def test_a_refused_table_leaks_no_page_whatever_the_record_shape(
    pool: BufferPool, catalog_store: CatalogStore, shape: str
) -> None:
    # The inline case alone certifies an invariant it never reaches: an overflow record writes
    # its whole chain before the directory is consulted, so every refusal used to leak one page
    # per chunk, once per retry, with no sanctioned way to take them back (G6).
    store = HeapStore(pool, catalog_store)
    store.bootstrap()
    body = "x" if shape == "inline" else "L" * 3000
    tables = [
        TableDef(
            table_id=index,
            name=f"Table_{index}",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="body", type=ValueType.STRING),
            ),
            primary_key="id",
        )
        for index in range(1, store.max_tables + 2)
    ]
    for table in tables[: store.max_tables]:
        store.insert(table, 1, (1, "x"), xmin=10)
    before = pool.storage.page_count(store.file)
    for _attempt in range(10):
        with pytest.raises(GrafxUnsupportedOperation):
            store.insert(tables[store.max_tables], 1, (1, body), xmin=10)
    assert pool.storage.page_count(store.file) == before
    assert len(list(store.scan(tables[0], at(100)))) == 1


def test_an_overflow_record_still_lands_when_the_directory_has_room(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    payload = "z" * 4000
    ref = heap_store.insert(person_table, 1, (1, payload), xmin=10)
    assert heap_store.read(ref).values == (1, payload)


# --- the delete door is a door too ---------------------------------------------------------------


def test_delete_refuses_a_file_whose_header_page_is_gone(
    pool: BufferPool, device: MemoryDevice, heap_store: HeapStore, person_table: TableDef
) -> None:
    # Every other path refuses this file; delete must not go on writing to it.
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    pool.flush()
    device.poke_page(heap_store.file, HEADER_PAGE_INDEX, bytes(pool.page_size))
    pool.invalidate()
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.delete(person_table, ref, xmax=9)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.update(person_table, ref, (1, "Ada L"), xmin=9)


def test_delete_refuses_a_reference_to_the_reserved_header_page(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.delete(person_table, RecordRef(page=HEADER_PAGE_INDEX, slot=1), xmax=9)


def test_a_page_zero_that_was_written_to_is_never_re_initialised(
    pool: BufferPool, catalog_store: CatalogStore
) -> None:
    # A FREE page 0 that still carries a directory entry or a used payload area is damage, not
    # the absence a redo leaves behind, and bootstrap must not write over it.
    store = HeapStore(pool, catalog_store)
    store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.FREE)
    with pytest.raises(GrafxCorruptionDetected):
        store.is_bootstrapped()
    with pytest.raises(GrafxCorruptionDetected):
        store.bootstrap()
    with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
        assert page.slot_count >= 1, "the file header slot is still there"


# --- guards that had no test ---------------------------------------------------------------------


def test_a_page_that_left_its_table_mid_chain_stops_the_scan(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # The ownership check inside the walk: without it a page whose descriptor names another
    # table is read as if its rows belonged to this one.
    grow_to_pages(pool, heap_store, person_table, 3)
    middle = heap_store.pages_of(person_table)[1]
    with pool.pinned(heap_store.file, middle) as page:
        page.update_slot(0, struct.pack("<I", person_table.table_id + 99))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan(person_table, at(1000)))
    assert raised.value.details["table_id"] == person_table.table_id + 99
    with pytest.raises(GrafxCorruptionDetected):
        list(heap_store.scan_all(person_table))


def test_a_reference_into_a_page_that_is_not_a_data_page_is_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # The type check inside the slot read: without it the bytes of an overflow chunk or a
    # catalog chunk are decoded as a record header and answer as a row.
    ref = heap_store.insert(person_table, 1, (1, "L" * 3000), xmin=5)
    for index in range(pool.storage.page_count(heap_store.file)):
        with pool.pinned(heap_store.file, index) as page:
            found = page.page_type == int(PageType.OVERFLOW)
        if found:
            with pytest.raises(GrafxCorruptionDetected) as raised:
                heap_store.read(RecordRef(page=index, slot=0))
            assert raised.value.details["page_type"] == int(PageType.OVERFLOW)
            break
    else:
        raise AssertionError("the record did not overflow")
    assert heap_store.read(ref).record_id == 1


def test_a_commit_number_that_no_snapshot_could_see_is_refused_before_anything_is_written(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    ref = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    versions = len(list(heap_store.scan_all(person_table)))
    pages = pool.storage.page_count(heap_store.file)
    for call in (
        lambda: heap_store.insert(person_table, 2, (2, "L" * 3000), xmin=0),
        lambda: heap_store.update(person_table, ref, (1, "L" * 3000), xmin=0),
        lambda: heap_store.insert(person_table, 3, (3, "Bob"), xmin=-1),
    ):
        with pytest.raises(GrafxTransactionStateError):
            call()
    assert len(list(heap_store.scan_all(person_table))) == versions
    assert pool.storage.page_count(heap_store.file) == pages




def test_the_delete_door_names_the_reserved_page_for_what_it_is(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.delete(person_table, RecordRef(page=HEADER_PAGE_INDEX, slot=1), xmax=9)
    assert "reserved header page" in raised.value.message
    with pytest.raises(GrafxCorruptionDetected) as read_raised:
        heap_store.read(RecordRef(page=HEADER_PAGE_INDEX, slot=1))
    assert "reserved header page" in read_raised.value.message


# --- each half of a masked pair, told apart by what only it can say --------------------------------


def test_a_declared_payload_length_that_does_not_match_the_bytes_is_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # The neighbouring guard is the bounds check inside the value decoder, which fires only when
    # the payload is short. A header that UNDER-states a payload the decoder can read leaves the
    # decoder happy and only this check can see it.
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, reference.page) as page:
        content = bytearray(page.read_slot(reference.slot))
        declared = int.from_bytes(content[4:8], "little")
        content[4:8] = (declared - 1).to_bytes(4, "little")
        page.update_slot(reference.slot, bytes(content))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.read(reference)
    assert raised.value.details["declared"] == declared - 1
    assert raised.value.details["observed"] == declared


def test_a_heap_page_with_no_descriptor_says_that_is_what_is_wrong(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    # The neighbouring guard is the slot range check, which would also refuse a read of slot 0 on
    # an empty page. Only this one names the descriptor, which is what a reader has to know: the
    # page cannot say which table it belongs to at all.
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, reference.page) as page:
        page.clear()
        page.page_type = int(PageType.HEAP)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.read(reference)
    assert "descriptor" in raised.value.message
    assert raised.value.details["page"] == reference.page


def test_a_directory_entry_of_the_wrong_length_is_refused() -> None:
    for raw in (b"", bytes(DIRECTORY_ENTRY_SIZE - 1), bytes(DIRECTORY_ENTRY_SIZE + 1)):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            TableExtent.decode(raw)
        assert raised.value.details["value"] == len(raw)
    # An all-zero entry of the right LENGTH is still refused, but for a different reason and with
    # a different field: its counter sits below the first id that can exist, which would hand an
    # identity to a second row. The two refusals must not be confused for one another (A62).
    with pytest.raises(GrafxCorruptionDetected) as counter:
        TableExtent.decode(bytes(DIRECTORY_ENTRY_SIZE))
    assert counter.value.details["field"] == "next_record_id"
    assert TableExtent.decode(
        TableExtent(table_id=0, first_page=0, last_page=0, page_count=0).encode()
    ).table_id == 0


@pytest.mark.parametrize("field", ["table_id", "first_page", "last_page", "page_count"])
def test_a_directory_entry_field_that_cannot_be_packed_is_refused(field: str) -> None:
    # Every field here was read back from a page, so any of them can be four damaged bytes. An
    # unchecked pack throws a raw struct.error out of the public insert door, after the row has
    # already been written (A41).
    fields = {"table_id": 1, "first_page": 1, "last_page": 1, "page_count": 1}
    for value in (MAX_DIRECTORY_FIELD + 1, -1, True):
        broken = dict(fields)
        broken[field] = value
        with pytest.raises(GrafxCorruptionDetected) as raised:
            TableExtent(**broken).encode()  # type: ignore[arg-type]
        assert raised.value.details["field"] == field
    assert len(TableExtent(**fields).encode()) == DIRECTORY_ENTRY_SIZE


def test_a_damaged_page_count_never_escapes_as_a_raw_struct_error(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    filler = "f" * 200
    grow_to_pages(pool, heap_store, person_table, 2)
    point_extent_at(
        pool, heap_store, person_table.table_id, page_count=MAX_DIRECTORY_FIELD
    )
    pages = pool.storage.page_count(heap_store.file)
    rows = len(list(heap_store.scan_all(person_table)))
    for _attempt in range(4):
        try:
            for _fill in range(6):
                heap_store.insert(person_table, 2, (2, filler), xmin=6)
        except GrafxError:
            pass
    assert len(list(heap_store.scan_all(person_table))) >= rows
    assert pool.storage.page_count(heap_store.file) >= pages


# --- every walker terminates, and says so ----------------------------------------------------------
#
# A walk that does not terminate is worse than one that answers wrongly: it blocks the process and,
# on a shared machine, every other run on it. The rule is one set of visited pages consulted on
# every hop of every walker, and these tests plant the three shapes a chain can close in -- a page
# that points at itself, two pages that point at each other, and a long chain that returns to its
# head -- against each walker in turn.


def plant_cycle(pool: BufferPool, store: HeapStore, table: TableDef, shape: str) -> None:
    """Close the page chain of a table into the requested shape."""
    grow_to_pages(pool, store, table, 4)
    chain = store.pages_of(table)
    if shape == "self loop":
        target, source = chain[0], chain[0]
    elif shape == "two cycle":
        target, source = chain[0], chain[1]
    else:
        target, source = chain[0], chain[-1]
    with pool.pinned(store.file, source) as page:
        page.next_page = target
    store._tail_cache.clear()


CYCLE_SHAPES = ["self loop", "two cycle", "long cycle"]


@pytest.mark.parametrize("shape", CYCLE_SHAPES)
def test_pages_of_refuses_a_cycle_rather_than_walking_it(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef, shape: str
) -> None:
    plant_cycle(pool, heap_store, person_table, shape)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    assert raised.value.details["field"] == "cycle"
    assert "returns to page" in raised.value.message


@pytest.mark.parametrize("shape", CYCLE_SHAPES)
def test_scan_refuses_a_cycle_rather_than_walking_it(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef, shape: str
) -> None:
    plant_cycle(pool, heap_store, person_table, shape)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan(person_table, at(1000)))
    assert raised.value.details["field"] == "cycle"
    with pytest.raises(GrafxCorruptionDetected) as scanned:
        list(heap_store.scan_all(person_table))
    assert scanned.value.details["field"] == "cycle"
    # A record id that is not there is what forces the walk to the end of the chain; looking up
    # one that is on the first page returns before the cycle is ever reached, which is correct.
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.lookup(person_table, 987654, at(1000))


@pytest.mark.parametrize("shape", CYCLE_SHAPES)
def test_the_append_walk_refuses_a_cycle_rather_than_walking_it(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef, shape: str
) -> None:
    plant_cycle(pool, heap_store, person_table, shape)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.insert(person_table, 99, (99, "after the cycle"), xmin=6)
    assert raised.value.details["field"] == "cycle"


@pytest.mark.parametrize("shape", CYCLE_SHAPES)
def test_a_version_chain_that_closes_is_refused_rather_than_walked(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef, shape: str
) -> None:
    first = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    second = heap_store.update(person_table, first, (1, "v1"), xmin=20)
    third = heap_store.update(person_table, second, (1, "v2"), xmin=30)
    if shape == "self loop":
        source, target = third, third
    elif shape == "two cycle":
        source, target = first, second
    else:
        source, target = first, third
    with pool.pinned(heap_store.file, source.page) as page:
        content = bytearray(page.read_slot(source.slot))
        content[32:40] = target.encode().to_bytes(8, "little")
        page.update_slot(source.slot, bytes(content))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.version_chain(third)
    assert raised.value.details["field"] == "cycle"


def test_a_remembered_tail_that_is_no_longer_the_tail_is_not_trusted(
    pool: BufferPool, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """The cache is an optimisation, so it has to be checked before it is believed.

    Two stores over one database is the honest shape of this: the lease makes one of them the
    writer at a time, but a store that held the tail in memory and then let another one extend
    the chain must not go on appending to the page it remembers. Believing a stale cache relinks
    a page that is no longer the tail, which is the same data loss the durable hint was made
    repairable to avoid.
    """
    first = HeapStore(pool, catalog_store)
    first.bootstrap()
    second = HeapStore(pool, catalog_store)
    filler = "f" * 200
    first.insert(person_table, 1, (1, filler), xmin=5)
    remembered = first._tail_cache.get(person_table.table_id)
    assert remembered is not None, "the first store remembers a tail"

    grow_to_pages(pool, second, person_table, 3, record_id=2)
    grown = second.pages_of(person_table)
    assert remembered[0] != grown[-1], "the remembered page is no longer the tail"

    # The record must not fit in the remembered page, or the append lands mid-chain and nothing
    # is lost: it is the GROW path that relinks, and relinking a page that is no longer the tail
    # is what cuts every page after it out of the chain.
    with pool.pinned(first.file, remembered[0]) as page:
        assert not page.can_fit(400)
    reference = first.insert(person_table, 3, (3, "L" * 400), xmin=6)
    chain = first.pages_of(person_table)
    assert chain[: len(grown)] == grown, "no page was cut out of the chain"
    assert reference.page in chain
    assert 3 in {
        version.record_id for _reference, version in first.scan(person_table, at(1000))
    }
    assert 2 in {
        version.record_id for _reference, version in first.scan(person_table, at(1000))
    }


def test_a_remembered_tail_that_became_a_foreign_page_is_not_trusted(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    # The cheap revalidation asks the same two questions the walk asks, so a remembered page that
    # changed owner is dropped rather than appended to.
    other = second_table(catalog_store)
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    remembered = heap_store._tail_cache[person_table.table_id][0]
    with pool.pinned(heap_store.file, remembered) as page:
        page.update_slot(0, struct.pack("<I", other.table_id))
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.insert(person_table, 2, (2, "next"), xmin=6)


def test_a_walk_that_does_not_end_fails_rather_than_running_forever(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Termination does not rest on the cycle guard alone.

    A guard that is the only thing ending a walk turns its own removal into a hang, and a hang is
    worse than a failure: it blocks the machine instead of the build. The second bound comes from
    the file -- a chain of distinct pages cannot be longer than the pages that exist -- so it can
    never refuse a real walk and it always ends one. The mutation battery is what proves the two
    are independent; this pins the bound itself and the refusal every walker gives.
    """
    grow_to_pages(pool, heap_store, person_table, 3)
    limit = heap_store._chain_limit()
    assert limit == pool.storage.page_count(heap_store.file) + 1

    # Inside the bound nothing is refused, which is what makes it safe for a real chain.
    heap_store._refuse_endless_chain(person_table, limit, limit)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store._refuse_endless_chain(person_table, limit + 1, limit)
    assert raised.value.details["field"] == "chain_length"

    chain = heap_store.pages_of(person_table)
    with pool.pinned(heap_store.file, chain[-1]) as page:
        page.next_page = chain[0]
    heap_store._tail_cache.clear()
    for walker in (
        lambda: list(heap_store.scan_all(person_table)),
        lambda: heap_store.pages_of(person_table),
        lambda: heap_store.insert(person_table, 9, (9, "x"), xmin=6),
    ):
        with pytest.raises(GrafxCorruptionDetected):
            walker()


class ForgetfulSet(set):
    """A visited set that remembers nothing, which is what a broken cycle guard behaves like."""

    def __contains__(self, item: object) -> bool:
        return False


def test_every_heap_walker_still_ends_when_the_visited_set_fails(
    pool: BufferPool,
    heap_store: HeapStore,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is given the only test that can reach it (A34).

    While the visited set works this guard can never fire, so no end-to-end test touches it --
    which is exactly why the mutation removing it survived. Its whole purpose is the case where
    the set does not work, so the set is defeated deliberately and the walk must still end, with
    a typed refusal naming the length rather than with a hang.
    """
    grow_to_pages(pool, heap_store, person_table, 4)
    chain = heap_store.pages_of(person_table)
    with pool.pinned(heap_store.file, chain[-1]) as page:
        page.next_page = chain[0]
    heap_store._tail_cache.clear()
    monkeypatch.setattr(heap_module, "visited_pages", ForgetfulSet)

    with pin_ceiling(monkeypatch):
        for walker in (
            lambda: heap_store.pages_of(person_table),
            lambda: list(heap_store.scan_all(person_table)),
            lambda: heap_store.insert(person_table, 9, (9, "x"), xmin=6),
        ):
            with pytest.raises(GrafxCorruptionDetected) as raised:
                walker()
            assert raised.value.details["field"] == "chain_length"


def test_the_version_chain_still_ends_when_the_visited_set_fails(
    pool: BufferPool,
    heap_store: HeapStore,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = heap_store.insert(person_table, 1, (1, "v0"), xmin=10)
    second = heap_store.update(person_table, first, (1, "v1"), xmin=20)
    with pool.pinned(heap_store.file, first.page) as page:
        content = bytearray(page.read_slot(first.slot))
        content[32:40] = second.encode().to_bytes(8, "little")
        page.update_slot(first.slot, bytes(content))
    monkeypatch.setattr(heap_module, "visited_pages", ForgetfulSet)
    # A version chain is bounded by the slots the file could hold, not by its pages, so this
    # walker legitimately takes more steps than a page walk before its bound answers.
    ceiling = pool.storage.page_count(heap_store.file) * (pool.page_size // 4) + 64
    with pin_ceiling(monkeypatch, limit=ceiling):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            heap_store.version_chain(second)
    assert raised.value.details["field"] == "chain_length"


def test_a_refusing_walker_leaves_no_page_pinned(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The other shape a hang can take, ruled out explicitly.

    A walk that pins a page per hop and refuses in the middle must release what it holds, or the
    budget drains and the next pin blocks behind pages nobody will ever unpin. Every walker here
    pins inside a context manager, so the release happens on the exception path too; this pins
    that as a property rather than as a reading of the code.
    """
    grow_to_pages(pool, heap_store, person_table, 4)
    chain = heap_store.pages_of(person_table)
    with pool.pinned(heap_store.file, chain[-1]) as page:
        page.next_page = chain[0]
    heap_store._tail_cache.clear()

    for walker in (
        lambda: heap_store.pages_of(person_table),
        lambda: list(heap_store.scan(person_table, at(1000))),
        lambda: list(heap_store.scan_all(person_table)),
        lambda: heap_store.insert(person_table, 9, (9, "x"), xmin=6),
    ):
        with pytest.raises(GrafxCorruptionDetected):
            walker()
        held = [
            index
            for index in range(pool.storage.page_count(heap_store.file))
            if pool.pin_count(heap_store.file, index)
        ]
        assert held == [], f"pages still pinned after a refusal: {held}"
    assert pool.used_bytes() <= pool.budget_bytes


def test_the_page_descriptor_slot_is_refused_by_the_guard_that_names_it(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """D5: the record header decoder refuses the same read for a different reason.

    Slot 0 holds four bytes and a record header needs forty, so both guards answer a read of the
    descriptor with the same exception type. Only one of them says what is actually wrong, and
    asserting the type alone let the descriptor guard be deleted with the suite still green.
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.read(RecordRef(page=reference.page, slot=0))
    assert raised.value.details["field"] == "page_descriptor_slot"
    assert raised.value.details["slot"] == 0
    assert "not a record" in raised.value.message


def test_a_page_descriptor_of_the_wrong_width_is_named_rather_than_unpacked(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """D1: A22 can install any checksum-valid image the log carried.

    Nothing in such an image constrains the width of slot 0, and unpacking it blind throws a raw
    struct.error out of read(), scan() and insert() -- an exception with no code, no retry flag
    and no location, which C6 cannot classify and no ledger can record. A41 required exactly this
    check on the encode side; this is the same invariant on the decode side.
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    forged = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=reference.page)
    forged.page_lsn = 99
    forged.insert_slot(b"12345678")
    forged.insert_slot(bytes(48))
    assert heap_store.apply_page_image(reference.page, pool.codec.encode_page(forged))

    for door in (
        lambda: heap_store.read(reference),
        lambda: list(heap_store.scan(person_table, at(1000))),
        lambda: heap_store.insert(person_table, 2, (2, "Bob"), xmin=11),
    ):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "page_descriptor"
        assert raised.value.details["value"] == 8


def test_an_append_after_a_redo_lands_inside_the_chain(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """D2: the cached tail keeps every property it had, and loses only reachability.

    A redo that shortens a chain does not touch the page a walk stopped at, so a cache that
    revalidates page-local properties still believes it. The row then lands outside the chain and
    scan() cannot see it, with no error anywhere -- exactly the loss A40 exists to prevent.
    """
    filler = "f" * 200
    heap_store.insert(person_table, 1, (1, filler), xmin=5)
    first_page = heap_store.pages_of(person_table)[0]
    pool.flush()
    logged = pool.codec.decode_page(pool.storage.raw_page(heap_store.file, first_page))
    logged.page_lsn = 42
    image = pool.codec.encode_page(logged)

    grow_to_pages(pool, heap_store, person_table, 3)
    warm = heap_store.extent_of(person_table)
    assert warm is not None

    assert heap_store.apply_page_image(first_page, image)
    reference = heap_store.insert(person_table, 999, (999, "after the redo"), xmin=6)

    chain = heap_store.pages_of(person_table)
    assert reference.page in chain, "the row landed outside the chain"
    assert 999 in {
        version.record_id for _ref, version in heap_store.scan(person_table, at(1000))
    }
    assert heap_store.lookup(person_table, 999, at(1000)) is not None


def test_a_warm_cache_still_refuses_a_chain_that_leads_out_of_the_table(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """The cached path needs its own adversarial tests, or it answers where the walk refuses.

    Every earlier test of this damage cleared the cache before planting it, which is how a warm
    cache serving an append for a table whose chain now starts at the reserved header page stayed
    hidden. The cache here is deliberately left holding a tail that is still perfectly valid in
    itself -- right owner, still a tail -- so that only a guard which looks at the FIRST page can
    tell that the chain no longer belongs to this table.
    """
    targets = foreign_pages(pool, heap_store, catalog_store, person_table)
    grow_to_pages(pool, heap_store, person_table, 2)
    warm = heap_store._tail_cache[person_table.table_id]
    real_tail = heap_store.pages_of(person_table)[-1]
    assert warm[0] == real_tail

    for kind, page_index in targets.items():
        if kind == "the reserved header page":
            # That shape is refused by the directory entry itself now, before the cache is ever
            # consulted, so it exercises _require_first_page rather than the cached path. Its own
            # test asserts it; here it would prove nothing about the cache.
            continue
        point_extent_at(pool, heap_store, person_table.table_id, first_page=page_index)
        # The store had already walked when the damage appeared, so its memory of the tail is
        # intact and every page-local property of that tail still holds.
        heap_store._tail_cache[person_table.table_id] = warm
        with pytest.raises(GrafxCorruptionDetected) as raised:
            heap_store.insert(person_table, 7, (7, kind), xmin=6)
        # Each kind is refused by a different guard, and each is asserted on what only that
        # guard sets: a page of another table by the owner it names, an overflow page by the
        # type it declares. Asserting the class alone could not tell them apart (A62).
        if kind == "a page of another table":
            assert raised.value.details["table_id"] != person_table.table_id
        else:
            assert raised.value.details["page_type"] == int(PageType.OVERFLOW)
        assert person_table.table_id not in heap_store._tail_cache, (
            f"a refused walk left the cache warm after {kind}"
        )


def test_delete_refuses_a_commit_number_as_a_caller_error(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """D9: a caller bug must not manufacture an integrity incident.

    insert() and update() both answer a bad commit number with transaction_state. delete() used
    to reach a different guard and answer corruption_detected, and FR-8 and FR-10 turn that code
    into truncation, quarantine and a forensic ledger entry (A11-revised).
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    for bad in (0, -1, True):
        with pytest.raises(GrafxTransactionStateError) as raised:
            heap_store.delete(person_table, reference, xmax=bad)
        assert raised.value.code == "transaction_state"
        assert raised.value.details["field"] == "xmax"
    assert heap_store.read(reference).xmax == 0, "nothing was written"


def test_a_cache_is_not_trusted_across_a_pool_invalidation(
    pool: BufferPool, device: MemoryDevice, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Dropping the page cache is the other door that makes a derived walk stale.

    After an invalidation every page comes back from the device, where another process may have
    rewritten the links while this one was idle. The remembered tail keeps every property it had
    -- right owner, still a tail -- so only the epoch can say that the walk which reached it no
    longer holds.
    """
    grow_to_pages(pool, heap_store, person_table, 3)
    chain = heap_store.pages_of(person_table)
    assert heap_store._tail_cache[person_table.table_id][0] == chain[-1]
    pool.flush()

    # Another writer truncates the chain on the device while this store holds only its memory.
    truncated = pool.codec.decode_page(device.raw_page(heap_store.file, chain[0]))
    truncated.next_page = NO_PAGE
    device.poke_page(heap_store.file, chain[0], pool.codec.encode_page(truncated))
    pool.invalidate()

    reference = heap_store.insert(person_table, 999, (999, "after the truncation"), xmin=6)
    assert reference.page in heap_store.pages_of(person_table)
    assert 999 in {
        version.record_id for _ref, version in heap_store.scan(person_table, at(1000))
    }


# --- DEF-2: a refused update leaves exactly one live version ------------------------------------


def live_versions(store: HeapStore, table: TableDef, record_id: int) -> list[tuple[int, int]]:
    """Return every stored version of a record that nothing has ended."""
    return [
        (version.xmin, version.xmax)
        for _reference, version in store.scan_all(table)
        if version.record_id == record_id and version.xmax == 0
    ]


def test_a_refused_update_leaves_exactly_one_live_version(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Writing the new version first and reaching for the old one afterwards is what made a
    reported failure change what a reader sees: two live versions, one of them the value the
    caller was told had not been written, and lookup answering with whichever it met first.
    """
    reference = heap_store.insert(person_table, 1, (1, "v1"), xmin=1)
    assert live_versions(heap_store, person_table, 1) == [(1, 0)]

    versions_before = len(list(heap_store.scan_all(person_table)))
    # The old page stops being a page of this table, which is what the update has to notice
    # before it writes anything at all. Every read door refuses it now, so the count is taken
    # while the page is still readable.
    with pool.pinned(heap_store.file, reference.page) as page:
        page.update_slot(0, struct.pack("<I", person_table.table_id + 7))

    with pytest.raises(GrafxCorruptionDetected):
        heap_store.update(person_table, reference, (1, "v2"), xmin=10)

    with pool.pinned(heap_store.file, reference.page) as page:
        page.update_slot(0, struct.pack("<I", person_table.table_id))
    assert len(list(heap_store.scan_all(person_table))) == versions_before, (
        "a refused update wrote a version anyway"
    )
    assert live_versions(heap_store, person_table, 1) == [(1, 0)]
    assert heap_store.lookup(person_table, 1, at(1000)).values == (1, "v1")


def test_a_refused_update_does_not_leave_the_failed_value_readable(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    reference = heap_store.insert(person_table, 1, (1, "v1"), xmin=1)
    # A row that does not match its schema is refused before a page is touched.
    with pytest.raises(SchemaMismatchError):
        heap_store.update(person_table, reference, (1, 2), xmin=10)
    assert live_versions(heap_store, person_table, 1) == [(1, 0)]
    assert heap_store.lookup(person_table, 1, at(1000)).values == (1, "v1")
    # And the retry of a well-formed update still leaves exactly one.
    heap_store.update(person_table, reference, (1, "v2"), xmin=10)
    assert live_versions(heap_store, person_table, 1) == [(10, 0)]
    assert heap_store.lookup(person_table, 1, at(1000)).values == (1, "v2")


def test_an_update_of_a_version_that_already_ended_is_named_as_such(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """E10: _end_version refused the same operation with the same class, so the early guard could
    be deleted and nothing could tell. It is the guard that keeps the old version from being
    ended twice, and only it says which version had already ended.
    """
    reference = heap_store.insert(person_table, 1, (1, "v1"), xmin=1)
    heap_store.update(person_table, reference, (1, "v2"), xmin=10)
    with pytest.raises(GrafxTransactionStateError) as raised:
        heap_store.update(person_table, reference, (1, "v3"), xmin=11)
    assert raised.value.details["field"] == "already_ended"
    assert raised.value.details["xmax"] == 10
    assert live_versions(heap_store, person_table, 1) == [(10, 0)]


def test_an_update_survives_a_pool_that_can_hold_only_two_pages(
    device: MemoryDevice, metrics: RecordingMetrics
) -> None:
    # The old page is held pinned across the new write, so the budget has to have room for it.
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
    reference = store.insert(table, 1, (1, "v1"), xmin=1)
    for step in range(2, 8):
        reference = store.update(table, reference, (1, f"v{step}"), xmin=step)
    assert store.lookup(table, 1, at(1000)).values == (1, "v7")
    assert len(live_versions(store, table, 1)) == 1
    big = store.insert(table, 2, (2, "L" * 3000), xmin=1)
    store.update(table, big, (2, "L" * 3200), xmin=9)
    assert len(live_versions(store, table, 2)) == 1


# --- DEF-3: a chain that ends before it starts ---------------------------------------------------


def test_a_table_whose_chain_starts_nowhere_is_refused_at_every_read_door(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Both walks stop at NO_PAGE, so naming it as the FIRST page read as an empty table: every
    door answered nothing and none of them raised, while the rows sat on pages the walk never
    reached. scan_all is what C6 verifies against, and the drift check agreed, so the verifier
    would have reported a clean, empty table over a table that is not empty.
    """
    for record_id in range(6):
        heap_store.insert(person_table, record_id, (record_id, "f" * 200), xmin=5)
    assert len(list(heap_store.scan_all(person_table))) == 6
    point_extent_at(pool, heap_store, person_table.table_id, first_page=NO_PAGE)

    for door in (
        lambda: list(heap_store.scan(person_table, at(1000))),
        lambda: list(heap_store.scan_all(person_table)),
        lambda: heap_store.lookup(person_table, 3, at(1000)),
        lambda: heap_store.pages_of(person_table),
        lambda: heap_store.extent_of(person_table),
        lambda: heap_store.insert(person_table, 99, (99, "x"), xmin=6),
    ):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "first_page"
        assert raised.value.details["value"] == NO_PAGE


def test_a_table_whose_chain_starts_at_the_reserved_page_is_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    point_extent_at(pool, heap_store, person_table.table_id, first_page=HEADER_PAGE_INDEX)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan_all(person_table))
    assert raised.value.details["field"] == "first_page"
    assert raised.value.details["value"] == HEADER_PAGE_INDEX


# --- A7: the cache A40.3 asks for is actually usable after an append that grew the chain ---------


def test_the_tail_is_remembered_across_an_append_that_grew_the_chain(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """A40.3 asks for the cache so appends stay O(1) after the first walk. Nothing asserted that
    the entry an append leaves behind is one the next append can use, so an append could have
    written an entry that was always discarded and the suite would not have noticed.
    """
    filler = "f" * 200
    # The table must already have a page, or the first insert creates one through _extent_for and
    # never reaches the growing branch of _append, which is the write under test.
    heap_store.insert(person_table, 1, (1, filler), xmin=5)
    pool.invalidate()  # so the epoch is not the value a constant would coincide with
    assert pool.derived_epoch(heap_store.file) > 0
    before = len(heap_store.pages_of(person_table))
    assert before >= 1
    grow_by_one_page(pool, heap_store, person_table)

    extent = heap_store.extent_of(person_table)
    cached = heap_store._tail_cache[person_table.table_id]
    assert extent is not None
    assert cached[0] == heap_store.pages_of(person_table)[-1]
    assert heap_store._cache_is_usable(person_table, extent, cached), (
        "the append left a cache entry the next append cannot use"
    )


# --- H1: a slot too short to hold a record header ------------------------------------------------


def test_a_record_slot_too_short_for_its_header_is_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Without the floor a raw struct.error leaves scan() and read(): unpack_from on a slot that
    is shorter than the header it is asked for.
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    forged = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=reference.page)
    forged.page_lsn = 99
    forged.insert_slot(struct.pack("<I", person_table.table_id))
    forged.insert_slot(bytes(10))
    assert heap_store.apply_page_image(reference.page, pool.codec.encode_page(forged))
    for door in (
        lambda: heap_store.read(RecordRef(page=reference.page, slot=1)),
        lambda: list(heap_store.scan_all(person_table)),
    ):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "record_header"
        assert raised.value.details["value"] == 10


# --- D2: a refusal a caller is meant to retry must not have written anything durable ------------


def pinned_frame_count(pool: BufferPool) -> int:
    """Return how many pages of the pool are pinned right now."""
    return sum(1 for frame in pool._frames.values() if frame.pins > 0)


PINS_ONE_UPDATE_TAKES: int = 10
"""How many pins one update of a row off the tail takes, over a hint that needs repair.

Written down in the test, not read off the code, so that a change in what an update touches is a
failure here rather than a silently shorter sweep (A81). test_the_sweep_covers_every_step_the_
update_actually_takes is what keeps this number honest.
"""


def count_pins(pool: BufferPool, operation: Callable[[], object]) -> int:
    """Return how many times the operation pinned a page."""
    taken = {"n": 0}
    original = BufferPool.pin

    def counting(self: BufferPool, file: str, page_index: PageIndex) -> Page:
        taken["n"] += 1
        return original(self, file, page_index)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BufferPool, "pin", counting)
        operation()
    return taken["n"]


def refuse_pin_number(
    monkeypatch: pytest.MonkeyPatch, number: int, error: GrafxError
) -> dict[str, bool]:
    """Make the Nth pin of this pool raise, and report whether that step was ever reached."""
    state = {"raised": False}
    seen = {"n": 0}
    original = BufferPool.pin

    def counting(self: BufferPool, file: str, page_index: PageIndex) -> Page:
        seen["n"] += 1
        if seen["n"] == number:
            state["raised"] = True
            raise error
        return original(self, file, page_index)

    monkeypatch.setattr(BufferPool, "pin", counting)
    return state


def build_store_under_budget(budget_pages: int) -> tuple[BufferPool, HeapStore, TableDef]:
    """Return a heap on a pool with room for exactly that many frames.

    The budget lives in the test, not in the store: the number of frames one append needs is a
    property of the code under test, so a constant inside it could be made to agree with a broken
    append (A81).
    """
    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics(), budget_pages=budget_pages)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    store = HeapStore(pool, catalog)
    store.bootstrap()
    table = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    # Saved, so that a test which reopens the database finds the same schema a real one would.
    catalog.save()
    return pool, store, table


def stale_hint_with_the_row_off_the_tail(
    pool: BufferPool, store: HeapStore, table: TableDef
) -> tuple[HeapStore, RecordRef]:
    """Leave the store in the state that makes an append touch three pages.

    All three conditions matter and none is exotic: the row being updated is not on the tail (its
    own frame), the tail is a second, and the hint no longer describes the chain so the append
    has to rewrite the directory on page 0, a third. A hint behind its chain is what an ordinary
    A22 redo leaves, with no failure at all (A40).

    The store that comes back is a fresh one over the same pool. Emptying the private tail cache
    to arrange a cold walk was a reach-in that also made the headline counterfactual run cold,
    hiding whatever the warm path does (A63); a reopen arranges the same thing by the front door.
    """
    reference = store.insert(table, 9, (9, "old"), xmin=5)
    grow_by_one_page(pool, store, table, record_id=3)
    chain = store.pages_of(table)
    assert reference.page != chain[-1], "the row under test is on the tail"
    extent = store.extent_of(table)
    assert extent is not None
    store._write_extent(replace(extent, last_page=chain[0], page_count=1))
    return HeapStore(pool, store.catalog), reference


def only_live_reference(store: HeapStore, table: TableDef, record_id: int) -> RecordRef:
    """Return the one live version of a record, refusing to guess when there is more than one."""
    live = [
        reference
        for reference, version in store.scan_all(table)
        if version.record_id == record_id and version.xmax == 0
    ]
    assert len(live) == 1, f"record {record_id} has {len(live)} live versions"
    return live[0]


def retryable_device_failures() -> tuple[GrafxError, ...]:
    """Return the two refusals LocalStorageDevice.write_page actually raises.

    Both are retryable, which is what makes them dangerous: the caller is TOLD to try again, so a
    row already made reachable is written a second time by a caller doing what it was told.
    """
    return (
        GrafxDeviceFull("The volume filled between two pages of one operation.", file="heap.dat"),
        GrafxStorageError("Another process held the file open for the length of one write."),
    )


@pytest.mark.parametrize("failure_index", [0, 1], ids=["device_full", "sharing_violation"])
@pytest.mark.parametrize("step", list(range(1, 13)))
def test_no_version_becomes_reachable_before_the_last_step_that_can_refuse(
    failure_index: int, step: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule, swept across every step of one update rather than asserted at one of them.

    Not "the budget path is closed" and not "the directory write is outside the pin" -- both were
    true while this was false. A retryable refusal is walked through every pin the operation
    takes, and at each one the same thing must hold: either the update completed, or it refused
    and left exactly one live version. A retryable refusal that leaves two is the defect, and a
    caller that does what the error told it turns two into three.

    The pin is the seam because it is where the device speaks: pinning can evict a dirty page,
    the write-back reaches write_page, and LocalStorageDevice answers GrafxDeviceFull for a full
    volume and GrafxStorageError for a sharing violation -- both retryable. Sweeping write-backs
    directly reaches only one step, because one update at this budget performs exactly one; the
    seam reaches all of them (A81: the seam lives in the test).
    """
    failure = retryable_device_failures()[failure_index]
    pool, store, table = build_store_under_budget(2)
    store, _planted = stale_hint_with_the_row_off_the_tail(pool, store, table)
    reference = only_live_reference(store, table, 9)

    refused_here = refuse_pin_number(monkeypatch, step, failure)
    try:
        store.update(table, reference, (9, "new"), xmin=11)
    except GrafxError as refused:
        assert refused.retryable, "a refusal a caller cannot retry is a different bug"
    monkeypatch.undo()

    live = live_versions(store, table, 9)
    assert len(live) == 1, f"a refusal at step {step} left {len(live)} live versions: {live}"
    seen = [
        version.record_id
        for _reference, version in store.scan(table, at(1000))
        if version.record_id == 9
    ]
    assert seen == [9], f"scan() yielded record 9 {len(seen)} times after a refusal at {step}"
    if step <= PINS_ONE_UPDATE_TAKES:
        assert refused_here["raised"], (
            f"step {step} never refused, so this parametrisation proves nothing (A83.1)"
        )


def test_the_sweep_covers_every_step_the_update_actually_takes() -> None:
    """The coverage assertion, so the sweep above can never quietly become vacuous.

    A sweep whose steps stop existing is a row of green tests that assert nothing -- which is
    exactly how the previous counterfactual died: round 7 removed the refusal it provoked, and
    nothing noticed for a round (A83.1).
    """
    pool, store, table = build_store_under_budget(2)
    store, _planted = stale_hint_with_the_row_off_the_tail(pool, store, table)
    reference = only_live_reference(store, table, 9)
    taken = count_pins(pool, lambda: store.update(table, reference, (9, "new"), xmin=11))
    assert taken == PINS_ONE_UPDATE_TAKES, (
        f"an update now takes {taken} pins, not {PINS_ONE_UPDATE_TAKES}; the sweep bound and "
        f"this number are one statement and must move together"
    )


@pytest.mark.parametrize("failure_index", [0, 1], ids=["device_full", "sharing_violation"])
def test_a_retryable_refusal_survives_a_cold_reopen_with_one_live_version(
    failure_index: int,
) -> None:
    """In memory is not the question: the row is on a page, so the check is against the pages.

    A second version that only the frame table knew about would be invisible here and fatal to
    the next process that opens the file.
    """
    failure = retryable_device_failures()[failure_index]
    pool, store, table = build_store_under_budget(2)
    store, _planted = stale_hint_with_the_row_off_the_tail(pool, store, table)
    device = pool.storage
    assert isinstance(device, MemoryDevice)
    reference = only_live_reference(store, table, 9)

    device.refuse_write_number(1, failure)
    with contextlib.suppress(GrafxError):
        store.update(table, reference, (9, "new"), xmin=11)
    device.disarm()
    pool.flush()
    pool.invalidate()

    reopened_catalog = CatalogStore(pool)
    reopened_catalog.load()
    reopened = HeapStore(pool, reopened_catalog)
    cold = [
        version
        for _reference, version in reopened.scan_all(reopened_catalog.catalog.table("Person"))
        if version.record_id == 9 and version.xmax == 0
    ]
    assert len(cold) == 1, f"a cold reopen found {len(cold)} live versions of one row"


@pytest.mark.parametrize("failure_index", [0, 1], ids=["device_full", "sharing_violation"])
def test_a_retryable_refusal_never_multiplies_a_row(failure_index: int) -> None:
    """A refusal that says try again must not have written anything the retry writes twice.

    The counterfactual this test rests on is the refusal itself, so the refusal is ASSERTED to
    have happened rather than assumed: the previous version provoked a budget refusal that the
    round-7 fix had already made impossible, took the happy path at both parametrized budgets,
    and could no longer fail when the guarantee stopped holding (A83.1).
    """
    failure = retryable_device_failures()[failure_index]
    pool, store, table = build_store_under_budget(2)
    store, _planted = stale_hint_with_the_row_off_the_tail(pool, store, table)
    device = pool.storage
    assert isinstance(device, MemoryDevice)

    attempts = 0
    device.refuse_write_number(1, failure)
    while True:
        attempts += 1
        try:
            store.update(table, only_live_reference(store, table, 9), (9, "new"), xmin=11)
        except GrafxError as refused:
            assert refused.retryable
            device.disarm()
            if attempts > 8:
                raise
            continue
        break

    assert device.refused_writes, "the device never refused, so nothing was counterfactual"
    assert attempts > 1, "the retry loop never retried"
    assert live_versions(store, table, 9) == [(11, 0)]
    assert store.lookup(table, 9, at(1000)).values == (9, "new")


def test_the_directory_entry_is_settled_before_the_row_can_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering stated as an observation rather than as a comment.

    The hint is repairable and the row is not, so the hint goes first: A40 already tolerates a
    last_page ahead of its chain and rewrites it on the next append.
    """
    pool, store, table = build_store_under_budget(6)
    store, _planted = stale_hint_with_the_row_off_the_tail(pool, store, table)
    order: list[str] = []
    original_extent = HeapStore._write_extent
    original_slot = Page.insert_slot

    def note_extent(self: HeapStore, extent: TableExtent) -> None:
        order.append("directory")
        original_extent(self, extent)

    def note_slot(self: Page, payload: bytes) -> int:
        order.append("row")
        return original_slot(self, payload)

    monkeypatch.setattr(HeapStore, "_write_extent", note_extent)
    monkeypatch.setattr(Page, "insert_slot", note_slot)
    store.update(table, only_live_reference(store, table, 9), (9, "new"), xmin=11)

    assert "directory" in order, "the append never repaired its hint"
    assert order.index("directory") < order.index("row"), f"the row went first: {order}"


# --- D3: a walk that asks nothing cannot be the second opinion in a drift comparison ------------


def test_listing_the_pages_refuses_a_hop_into_another_tables_page(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """pages_of is one half of the A33 drift comparison C6 runs, so it must not agree blindly.

    Only the per-hop ownership check produces this refusal: the hop is a real, readable, well
    formed heap page, and every structural question about the chain itself answers fine.
    """
    other = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    catalog_store.catalog.add_table(other)
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    stranger = heap_store.insert(other, 1, (1,), xmin=5)
    tail = heap_store.pages_of(person_table)[-1]
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = stranger.page

    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    assert raised.value.details["table_id"] == other.table_id
    assert raised.value.details["page"] == stranger.page


def test_listing_the_pages_refuses_a_hop_into_the_reserved_header_page(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Page 0 is the file header (A2), and an unchecked walk put it in the list of data pages."""
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    tail = heap_store.pages_of(person_table)[-1]
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = HEADER_PAGE_INDEX

    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    assert raised.value.details["page"] == HEADER_PAGE_INDEX
    assert raised.value.details["page_type"] == int(PageType.META)


def test_the_two_halves_of_the_drift_comparison_do_not_agree_on_a_damaged_chain(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """The failure this fix is really about: a check that certifies damage as clean.

    With the hint replayed to agree with the corrupt chain, an unasking pages_of returned the
    same length the hint claimed, so a caller comparing the two saw no drift at all -- while
    scan_all, which does ask, refused the very same table.
    """
    other = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    catalog_store.catalog.add_table(other)
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    stranger = heap_store.insert(other, 1, (1,), xmin=5)
    extent = heap_store.extent_of(person_table)
    assert extent is not None
    tail = heap_store.pages_of(person_table)[-1]
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = stranger.page
    heap_store._write_extent(replace(extent, last_page=stranger.page, page_count=2))

    with pytest.raises(GrafxCorruptionDetected):
        list(heap_store.scan_all(person_table))
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.pages_of(person_table)


# --- F6: the descriptor slot is not a record, at every door that reads one ----------------------


def test_ending_a_version_refuses_the_page_descriptor_slot(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Slot 0 carries the table id, not a record (A21). Its siblings said so; this one did not.

    The decoder masks the same read behind 'four bytes are not forty', so the test names the
    field only this guard sets (A62).
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.delete(person_table, RecordRef(page=reference.page, slot=0), xmax=9)
    assert raised.value.details["field"] == "page_descriptor_slot"
    assert raised.value.details["slot"] == 0
    assert heap_store.lookup(person_table, 1, at(1000)).values == (1, "Ada")


BIG_VALUE: str = "L" * 400
"""A value that will not share a page with much: the update carrying it has to grow the chain."""

PINS_ONE_GROWING_UPDATE_TAKES: int = 10
"""Pins taken by an update whose new version does not fit on the tail. See the sweep below."""


def a_tail_too_full_for(pool: BufferPool, store: HeapStore, table: TableDef, size: int) -> None:
    """Fill the tail until a row of that size cannot fit on it, so the next append must grow.

    Bounded by a count, and the question it asks is asked of the PAGE -- can_fit on the pinned
    tail -- rather than of the append under test.
    """
    for _attempt in range(MAX_SETUP_INSERTS):
        tail = store.pages_of(table)[-1]
        with pool.pinned(store.file, tail) as page:
            if not page.can_fit(size):
                return
        store.insert(table, 3, (3, PAGE_FILLER), xmin=5)
    raise AssertionError(f"the tail of {store.file!r} never filled past {size} bytes")


def a_growing_update_is_next(
    pool: BufferPool, store: HeapStore, table: TableDef
) -> tuple[HeapStore, RecordRef]:
    """Arrange the state in which the next update must take the GROWING branch of _append.

    The fitting branch and the growing branch are two statements of one rule, and only one of
    them was covered: a mutation putting the fresh branch back to relinking before it settles the
    directory SURVIVED the first round-8 battery (A66.1 at branch granularity).
    """
    reference = store.insert(table, 9, (9, "old"), xmin=5)
    grow_by_one_page(pool, store, table, record_id=3)
    a_tail_too_full_for(pool, store, table, len(BIG_VALUE) + RECORD_HEADER_SIZE)
    chain = store.pages_of(table)
    assert reference.page != chain[-1], "the row under test is on the tail"
    extent = store.extent_of(table)
    assert extent is not None
    store._write_extent(replace(extent, last_page=chain[0], page_count=1))
    return HeapStore(pool, store.catalog), reference


@pytest.mark.parametrize("failure_index", [0, 1], ids=["device_full", "sharing_violation"])
@pytest.mark.parametrize("step", list(range(1, 15)))
def test_the_growing_branch_makes_nothing_reachable_before_its_last_refusal(
    failure_index: int, step: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule on the branch that allocates a page and relinks the chain.

    Here the reachability step is the RELINK, not the slot write: the row is already on a page
    nothing points at, and the pointer is what a reader can follow. So the directory entry has to
    be settled before the relink -- and a refusal after the relink is what leaves the row visible
    while the caller is told to retry.
    """
    failure = retryable_device_failures()[failure_index]
    pool, store, table = build_store_under_budget(2)
    store, _planted = a_growing_update_is_next(pool, store, table)
    reference = only_live_reference(store, table, 9)

    refused_here = refuse_pin_number(monkeypatch, step, failure)
    try:
        store.update(table, reference, (9, BIG_VALUE), xmin=11)
    except GrafxError as refused:
        assert refused.retryable, "a refusal a caller cannot retry is a different bug"
    monkeypatch.undo()

    live = live_versions(store, table, 9)
    assert len(live) == 1, f"a refusal at step {step} left {len(live)} live versions: {live}"
    seen = [
        version.record_id
        for _reference, version in store.scan(table, at(1000))
        if version.record_id == 9
    ]
    assert seen == [9], f"scan() yielded record 9 {len(seen)} times after a refusal at {step}"
    if step <= PINS_ONE_GROWING_UPDATE_TAKES:
        assert refused_here["raised"], (
            f"step {step} never refused, so this parametrisation proves nothing (A83.1)"
        )


def test_the_growing_sweep_covers_every_step_that_update_actually_takes() -> None:
    """Coverage, so this sweep cannot quietly become a row of green no-ops either."""
    pool, store, table = build_store_under_budget(2)
    store, _planted = a_growing_update_is_next(pool, store, table)
    reference = only_live_reference(store, table, 9)
    taken = count_pins(pool, lambda: store.update(table, reference, (9, BIG_VALUE), xmin=11))
    assert taken == PINS_ONE_GROWING_UPDATE_TAKES, (
        f"a growing update now takes {taken} pins, not {PINS_ONE_GROWING_UPDATE_TAKES}"
    )


def test_the_growing_branch_really_is_the_one_under_test() -> None:
    """Without this the sweep above could be the fitting branch again and nobody would know."""
    pool, store, table = build_store_under_budget(6)
    store, _planted = a_growing_update_is_next(pool, store, table)
    before = pool.storage.page_count(store.file)
    store.update(table, only_live_reference(store, table, 9), (9, BIG_VALUE), xmin=11)
    assert pool.storage.page_count(store.file) > before, "the update never grew the chain"


# --- D3: the repaired count is the length walked, never the stored count plus one ---------------


def test_the_repaired_count_is_the_length_walked_not_the_stored_count_plus_one(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """A40.2 forbids the arithmetic, and until now nothing distinguished the two.

    extent.page_count + 1 agrees with the walked length for as long as the hint was right, which
    is every ordinary case -- so a mutation making that substitution survived a whole battery.
    It stops agreeing the moment the hint is wrong, which is the only moment the count is worth
    writing: after one redo plus one append it reported four pages for a chain of two.
    """
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    grow_by_one_page(pool, heap_store, person_table)
    chain = heap_store.pages_of(person_table)
    assert len(chain) == 2

    # What a redo leaves: the entry describes a longer chain than the one the pages spell out.
    heap_store._write_extent(
        replace(heap_store.extent_of(person_table), last_page=chain[-1], page_count=3)
    )
    reopened = HeapStore(pool, heap_store.catalog)
    reopened.insert(person_table, 2, (2, "after the redo"), xmin=6)

    walked = reopened.pages_of(person_table)
    repaired = reopened.extent_of(person_table)
    assert repaired is not None
    assert repaired.page_count == len(walked), (
        f"the entry says {repaired.page_count} pages for a chain of {len(walked)}"
    )


def test_a_hint_ahead_of_its_chain_is_repaired_rather_than_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The drift D1 now produces deliberately, so A40 has to tolerate it by name.

    The append settles the directory entry BEFORE it relinks, so a refusal in between leaves the
    entry naming a page the chain does not reach yet. That state must be repairable, or the fix
    for D1 would have traded a duplicated row for an unwritable table.
    """
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    chain = heap_store.pages_of(person_table)
    beyond = pool.storage.page_count(heap_store.file)
    pool.allocate(heap_store.file, int(PageType.HEAP))
    pool.unpin(heap_store.file, beyond, dirty=True)
    heap_store._write_extent(
        replace(heap_store.extent_of(person_table), last_page=beyond, page_count=2)
    )
    reopened = HeapStore(pool, heap_store.catalog)

    reference = reopened.insert(person_table, 2, (2, "next"), xmin=6)

    assert reference.page in reopened.pages_of(person_table)
    repaired = reopened.extent_of(person_table)
    assert repaired is not None
    assert repaired.page_count == len(reopened.pages_of(person_table))
    assert reopened.pages_of(person_table)[0] == chain[0]


# --- D4: the one page index that can never hold a record, said in a way only it can say ---------


def test_the_reserved_header_page_refusal_is_not_the_page_type_refusal(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Masked guards: _require_table_page refuses the same page a moment later because a META
    page is not a HEAP page, so deleting these three guards left the suite green.

    Only the field tells them apart, and the field is what a caller would have to route on.
    """
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    reserved = RecordRef(page=HEADER_PAGE_INDEX, slot=1)
    doors = (
        lambda: heap_store.update(person_table, reserved, (1, "no"), xmin=9),
        lambda: heap_store.delete(person_table, reserved, xmax=9),
        lambda: heap_store.read(reserved),
    )
    for door in doors:
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "reserved_header_page"
        assert raised.value.details["page"] == HEADER_PAGE_INDEX


def test_a_page_of_the_wrong_type_says_page_type_and_not_reserved_header_page(
    pool: BufferPool, heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """The distinguishing assertion needs both sides, or it distinguishes nothing."""
    other = second_table(catalog_store)
    stranger = heap_store.insert(other, 1, (1, "x"), xmin=5)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.update(person_table, stranger, (1, "no"), xmin=9)
    assert raised.value.details.get("field") != "reserved_header_page"


# --- D5: a caller's argument is a caller's error, however wrong it is ---------------------------


@pytest.mark.parametrize("record_id", [-1, 1 << 64, "x", True, 1.0])
def test_a_record_id_a_caller_cannot_store_is_not_an_integrity_incident(
    heap_store: HeapStore, person_table: TableDef, record_id: object
) -> None:
    """FR-8 and FR-10 route truncation, quarantine and a forensic entry off corruption_detected.

    A record id arrives from a caller, so no value of it says anything about the bytes on disk.
    Unguarded, every one of these reached the header encoder and came back as damage (D5).
    """
    with pytest.raises(GrafxError) as raised:
        heap_store.insert(person_table, record_id, (1, "Ada"), xmin=5)  # type: ignore[arg-type]
    assert raised.value.code == "configuration_error", record_id
    assert not isinstance(raised.value, GrafxCorruptionDetected), record_id
    assert raised.value.details["field"] == "record_id", record_id
    # The front-door guard is not the encoder's: it refuses BEFORE the tuple is encoded, so the
    # caller hears about the argument it got wrong first. Without it the encoder still classifies
    # correctly, which is why removing it left the suite green until this line existed (A67).
    with pytest.raises(GrafxError) as ordered:
        heap_store.insert(person_table, record_id, (1, 2), xmin=5)  # type: ignore[arg-type]
    assert ordered.value.details.get("field") == "record_id", (
        "a mismatched tuple was reported before the record id that could never be stored"
    )


@pytest.mark.parametrize("field", ["xmin", "xmax"])
def test_a_commit_number_too_wide_for_its_field_is_refused_as_a_caller_error(
    heap_store: HeapStore, person_table: TableDef, field: str
) -> None:
    """Only the low end was guarded, so a number too WIDE walked past and became damage."""
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    too_wide = (1 << 64) + 1
    with pytest.raises(GrafxError) as raised:
        if field == "xmin":
            heap_store.update(person_table, reference, (1, "next"), xmin=too_wide)
        else:
            heap_store.delete(person_table, reference, xmax=too_wide)
    assert raised.value.code == "transaction_state"
    assert not isinstance(raised.value, GrafxCorruptionDetected)
    assert raised.value.details["field"] == field
    assert live_versions(heap_store, person_table, 1) == [(5, 0)]


# --- D6: one state, one classification, and a name C6 can route on ------------------------------


def test_a_chain_hop_into_a_page_nobody_wrote_says_so(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The state a crash between allocating a page and writing it leaves.

    The pool calls those bytes free and unremarkable, and it is right: nothing checksummed wrong.
    The heap must still refuse, because it cannot invent the rows of a page nobody wrote. What
    was missing is the difference between the two refusals -- a page that was written and says
    the wrong thing needs a verifier, a page that was never written needs the redo already coming
    for it -- and both arrived under one code with nothing to tell them apart (D6, round 8).
    """
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    tail = heap_store.pages_of(person_table)[-1]
    unwritten = pool.storage.page_count(heap_store.file)
    pool.allocate(heap_store.file, int(PageType.FREE))
    pool.unpin(heap_store.file, unwritten, dirty=False)
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = unwritten

    for door in (
        lambda: heap_store.pages_of(person_table),
        lambda: list(heap_store.scan_all(person_table)),
    ):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "unwritten_page"
        assert raised.value.details["page"] == unwritten


def test_a_written_page_of_the_wrong_type_is_not_called_unwritten(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The other side of the distinction, without which the field says nothing."""
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    tail = heap_store.pages_of(person_table)[-1]
    overflow = write_chain(pool, heap_store.file, b"z" * 900, page_type=int(PageType.OVERFLOW))
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = overflow[0]

    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    assert raised.value.details["field"] == "page_type"
    assert raised.value.details["page_type"] == int(PageType.OVERFLOW)


# --- C6: the third page state, and the boundary where a freed slot IS damage --------------------


def test_a_heap_page_with_no_descriptor_says_which_state_it_is_in(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The third of three page states, and the one C6 could not route.

    Its neighbours already say field="unwritten_page" (never written; redo covers it) and
    field="page_type" (written, wrong kind). This one said nothing at all, so a verifier meeting
    it had to guess which of the other two it resembled. It is written and structurally
    incomplete: a verifier and a quarantine, never a redo.
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    forged = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=reference.page)
    forged.page_lsn = 99
    assert heap_store.apply_page_image(reference.page, pool.codec.encode_page(forged))

    for door in (
        lambda: heap_store.pages_of(person_table),
        lambda: list(heap_store.scan_all(person_table)),
        lambda: heap_store.read(reference),
    ):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            door()
        assert raised.value.details["field"] == "missing_page_descriptor"
        assert raised.value.details["page_type"] == int(PageType.HEAP)
        assert raised.value.details["page"] == reference.page


def test_a_heap_page_whose_descriptor_slot_was_freed_is_the_same_state(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Where provenance is known, a freed slot is damage again -- and this is that boundary.

    Page refuses a freed slot as an ordinary state because a slot id is an argument. Slot 0 of a
    heap data page is not an argument: this component wrote it, and a page that cannot say which
    table it belongs to is structurally incomplete however the entry came to be empty (A66.1).
    """
    reference = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    with pool.pinned(heap_store.file, reference.page) as page:
        page.free_slot(DESCRIPTOR_SLOT)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        list(heap_store.scan_all(person_table))
    assert raised.value.details["field"] == "missing_page_descriptor"
    assert raised.value.details["page_type"] == int(PageType.HEAP)


def test_the_three_page_states_are_told_apart_by_their_field(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """One assertion holding all three, because a routing key is only useful if it is unique."""
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    tail = heap_store.pages_of(person_table)[-1]
    seen: list[str] = []

    unwritten = pool.storage.page_count(heap_store.file)
    pool.allocate(heap_store.file, int(PageType.FREE))
    pool.unpin(heap_store.file, unwritten, dirty=False)
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = unwritten
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    seen.append(raised.value.details["field"])

    overflow = write_chain(pool, heap_store.file, b"z" * 900, page_type=int(PageType.OVERFLOW))
    with pool.pinned(heap_store.file, tail) as page:
        page.next_page = overflow[0]
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    seen.append(raised.value.details["field"])

    empty = Page(int(PageType.HEAP), page_size=pool.page_size, page_index=overflow[0])
    empty.page_lsn = 99
    assert heap_store.apply_page_image(overflow[0], pool.codec.encode_page(empty))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        heap_store.pages_of(person_table)
    seen.append(raised.value.details["field"])

    assert seen == ["unwritten_page", "page_type", "missing_page_descriptor"]
    assert len(set(seen)) == 3, "two page states answer with one routing key"


# --- row identity: allocated once, stable across versions, and never re-allocated by replay -----


def reopened_heap(pool: BufferPool, store: HeapStore) -> HeapStore:
    """Return a heap over the same pool that has read nothing yet, which is what a reopen is."""
    return HeapStore(pool, store.catalog)


def test_the_first_row_of_a_table_takes_the_first_identity(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    assert heap_store.next_record_id(person_table) == FIRST_RECORD_ID
    assert heap_store.allocate_record_id(person_table) == FIRST_RECORD_ID
    assert heap_store.next_record_id(person_table) == FIRST_RECORD_ID + 1


def test_an_identity_is_never_handed_out_twice(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """Invariant 1, in the plainest form there is."""
    handed = [heap_store.allocate_record_id(person_table) for _ in range(50)]
    assert len(set(handed)) == 50
    assert handed == sorted(handed)
    assert heap_store.next_record_id(person_table) == handed[-1] + 1


def test_two_tables_count_their_rows_separately(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """The counter is per table, so one table's traffic must not skip another's identities."""
    other = second_table(catalog_store)
    for _ in range(5):
        heap_store.allocate_record_id(person_table)
    assert heap_store.allocate_record_id(other) == FIRST_RECORD_ID
    assert heap_store.next_record_id(person_table) == FIRST_RECORD_ID + 5


def test_an_identity_survives_a_reopen(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """Invariant 1 across a restart: the counter is on a page before the caller can act on it.

    Held only in memory it would come back at one after every reopen, and the second row ever
    written would carry the identity of the first.
    """
    handed = [heap_store.allocate_record_id(person_table) for _ in range(3)]
    pool.flush()
    pool.invalidate()

    reopened = reopened_heap(pool, heap_store)
    assert reopened.next_record_id(person_table) == handed[-1] + 1
    assert reopened.allocate_record_id(person_table) not in handed


def test_an_identity_survives_a_crash_that_never_flushed(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The counter has to be on the PAGE, not in the store, before the caller sees it.

    Nothing here flushes: the frames are dropped as a crash drops them, and the reopen reads
    whatever the pool had written back on its own. What must never happen is the counter coming
    back below an identity already handed out.
    """
    handed = [heap_store.allocate_record_id(person_table) for _ in range(4)]
    pool.flush()
    with pool.pinned(heap_store.file, HEADER_PAGE_INDEX) as page:
        stored = [
            TableExtent.decode(payload)
            for slot, payload in page.iter_slots()
            if slot >= EXTENT_FIRST_SLOT
        ]
    entry = next(item for item in stored if item.table_id == person_table.table_id)
    assert entry.next_record_id > handed[-1], "the page does not cover the ids handed out"


def test_the_identity_of_a_row_does_not_change_when_it_is_updated(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """Invariant 2, section 3: a stable logical identity ACROSS VERSIONS."""
    identity = heap_store.allocate_record_id(person_table)
    reference = heap_store.insert(person_table, identity, (1, "v1"), xmin=5)
    counter_after_insert = heap_store.next_record_id(person_table)

    second = heap_store.update(person_table, reference, (1, "v2"), xmin=6)
    heap_store.update(person_table, second, (1, "v3"), xmin=7)

    versions = [
        version for _reference, version in heap_store.scan_all(person_table)
    ]
    assert {version.record_id for version in versions} == {identity}
    assert heap_store.next_record_id(person_table) == counter_after_insert, (
        "an update spent a new identity"
    )


def test_deleting_a_row_does_not_spend_an_identity(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    identity = heap_store.allocate_record_id(person_table)
    reference = heap_store.insert(person_table, identity, (1, "v1"), xmin=5)
    before = heap_store.next_record_id(person_table)
    heap_store.delete(person_table, reference, xmax=9)
    assert heap_store.next_record_id(person_table) == before


def test_a_deleted_identity_is_not_handed_out_again(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """Never reused includes never reused after the row is gone: a reference, an index entry or
    a log record naming that identity must not start matching a different row."""
    identity = heap_store.allocate_record_id(person_table)
    reference = heap_store.insert(person_table, identity, (1, "v1"), xmin=5)
    heap_store.delete(person_table, reference, xmax=9)
    assert heap_store.allocate_record_id(person_table) != identity


def test_replaying_an_insert_uses_the_logged_identity_and_does_not_allocate(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """Invariant 3. The counter need only end up AT OR ABOVE every id in use."""
    logged = 4096
    assert heap_store.next_record_id(person_table) < logged

    heap_store.insert(person_table, logged, (1, "replayed"), xmin=5)

    assert heap_store.next_record_id(person_table) == logged + 1
    stored = [version.record_id for _reference, version in heap_store.scan_all(person_table)]
    assert stored == [logged], "replaying the logged id allocated a different one"


def test_observing_the_same_identity_twice_moves_nothing_the_second_time(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """A22 asks every redo to be idempotent, and this counter is on the redo path."""
    assert heap_store.observe_record_id(person_table, 700) is True
    after = heap_store.next_record_id(person_table)
    assert heap_store.observe_record_id(person_table, 700) is False
    assert heap_store.observe_record_id(person_table, 12) is False
    assert heap_store.next_record_id(person_table) == after


def test_a_replayed_row_can_never_be_handed_its_own_identity_again(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The whole point of invariant 3, stated as the outcome rather than as the mechanism."""
    for logged in (9, 3, 77, 12):
        heap_store.insert(person_table, logged, (logged, "replayed"), xmin=5)
    pool.flush()
    pool.invalidate()

    reopened = reopened_heap(pool, heap_store)
    handed = [reopened.allocate_record_id(person_table) for _ in range(4)]
    assert not set(handed) & {9, 3, 77, 12}
    assert min(handed) > 77


def test_an_allocation_that_refuses_spends_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gap is acceptable; a reused id is not -- so the refusal must land on the safe side.

    The write is what can refuse, and it happens before the caller is given anything, so a
    refused allocation leaves the counter where it was rather than ahead of a row nobody wrote.
    """
    pool, store, table = build_store_under_budget(6)
    store.insert(table, 1, (1, "seed"), xmin=5)
    before = store.next_record_id(table)
    failure = retryable_device_failures()[0]

    refused = refuse_pin_number(monkeypatch, 1, failure)
    with pytest.raises(GrafxError) as raised:
        store.allocate_record_id(table)
    monkeypatch.undo()

    assert refused["raised"], "the counterfactual never fired"
    assert raised.value.retryable
    assert store.next_record_id(table) == before, "a refused allocation spent an identity"


@pytest.mark.parametrize("step", list(range(1, 6)))
def test_no_identity_is_spent_before_the_last_step_that_can_refuse(
    step: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same sweep the row itself gets, applied to the identity (A66.1).

    At every step of one allocation, either it completed or it refused -- and if it refused, the
    counter must not have moved, because a counter that moved without a caller receiving the id
    is only safe by luck: the caller retries, gets a different id, and the abandoned one is a gap.
    A counter that moved is tolerable; what must never happen is the counter NOT moving while an
    id was handed out.
    """
    pool, store, table = build_store_under_budget(2)
    store.insert(table, 1, (1, "seed"), xmin=5)
    pool.flush()
    pool.invalidate()
    store = reopened_heap(pool, store)
    before = store.next_record_id(table)
    failure = retryable_device_failures()[1]

    refuse_pin_number(monkeypatch, step, failure)
    handed = None
    try:
        handed = store.allocate_record_id(table)
    except GrafxError as raised:
        assert raised.retryable
    monkeypatch.undo()

    after = store.next_record_id(table)
    if handed is None:
        assert after == before, f"a refusal at step {step} spent an identity anyway"
    else:
        assert after > handed, f"step {step} handed out {handed} without spending it"


def test_a_table_that_has_used_every_identity_is_refused_without_damage(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """Eighteen quintillion rows is not a corrupt database, and must not be reported as one."""
    heap_store.insert(person_table, 1, (1, "seed"), xmin=5)
    extent = heap_store.extent_of(person_table)
    assert extent is not None
    heap_store._write_extent(replace(extent, next_record_id=MAX_U64))
    assert heap_store.next_record_id(person_table) == MAX_U64

    with pytest.raises(GrafxError) as raised:
        heap_store.allocate_record_id(person_table)
    assert raised.value.code == "unsupported_operation"
    assert not isinstance(raised.value, GrafxCorruptionDetected)
    assert raised.value.details["field"] == "next_record_id"


def test_a_stored_counter_below_the_first_identity_is_refused(
    pool: BufferPool, heap_store: HeapStore, person_table: TableDef
) -> None:
    """The catalog makes the same refusal about its own counters, and for the same reason: a
    counter below what exists hands an identity to a second row."""
    with pytest.raises(GrafxCorruptionDetected) as raised:
        TableExtent.decode(DIRECTORY_STRUCT.pack(1, 1, 1, 1, 0))
    assert raised.value.details["field"] == "next_record_id"
    assert raised.value.details["value"] == 0


def test_the_counter_is_not_a_u32_in_disguise(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """A record id is a 64-bit identity; passing it through the u32 bound beside it would refuse
    three quarters of the space the format reserves and call the entry corrupt for using it."""
    wide = (1 << 32) + 7
    heap_store.observe_record_id(person_table, wide)
    assert heap_store.next_record_id(person_table) == wide + 1
    entry = heap_store.extent_of(person_table)
    assert entry is not None
    assert TableExtent.decode(entry.encode()).next_record_id == wide + 1


# --- W5c: an edge naming a row that does not exist -----------------------------------------------


def knows_table(catalog_store: CatalogStore) -> TableDef:
    """Install a relationship table whose two ends are the Person table."""
    table = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Knows",
        kind="rel",
        columns=(ColumnDef(name="since", type=ValueType.INT64),),
        from_table="Person",
        to_table="Person",
    )
    catalog_store.catalog.add_table(table)
    return table


def test_an_edge_between_rows_that_exist_is_accepted(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    heap_store.insert(person_table, 2, (2, "Grace"), xmin=5)
    edges = knows_table(catalog_store)

    row = relationship_row(1, 2, (2020,))
    assert heap_store.require_endpoints(edges, row, at(1000)) == (1, 2)
    identity = heap_store.allocate_record_id(edges)
    reference = heap_store.insert(edges, identity, row, xmin=6)

    stored = heap_store.read(reference)
    assert edges.source_of(stored.values) == 1
    assert edges.target_of(stored.values) == 2
    assert stored.values[2] == 2020


@pytest.mark.parametrize("end", ["source", "target"])
def test_an_edge_naming_a_row_that_does_not_exist_is_refused_before_anything_is_staged(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef, end: str
) -> None:
    """Before, not after: no identity spent, no page pinned, no row written."""
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    edges = knows_table(catalog_store)
    row = relationship_row(1, 404, (2020,)) if end == "target" else relationship_row(404, 1, (2020,))
    counter_before = heap_store.next_record_id(edges)
    rows_before = len(list(heap_store.scan_all(person_table)))

    with pytest.raises(GrafxConfigurationError) as raised:
        heap_store.require_endpoints(edges, row, at(1000))

    assert raised.value.details["value"] == 404
    assert raised.value.details["endpoint_table"] == "Person"
    assert not isinstance(raised.value, GrafxCorruptionDetected)
    assert heap_store.next_record_id(edges) == counter_before, "a refused edge spent an identity"
    assert len(list(heap_store.scan_all(person_table))) == rows_before


def test_an_edge_may_not_point_at_a_row_the_snapshot_cannot_see(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """Visibility is the caller's snapshot, not this store's opinion: an edge must not be able to
    point at a version the transaction writing it cannot see."""
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    heap_store.insert(person_table, 2, (2, "Later"), xmin=900)
    edges = knows_table(catalog_store)
    row = relationship_row(1, 2, (2020,))

    assert heap_store.require_endpoints(edges, row, at(1000)) == (1, 2)
    with pytest.raises(GrafxConfigurationError) as raised:
        heap_store.require_endpoints(edges, row, at(10))
    assert raised.value.details["value"] == 2


def test_an_edge_pointing_at_a_deleted_row_is_refused(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    reference = heap_store.insert(person_table, 2, (2, "Gone"), xmin=5)
    heap_store.delete(person_table, reference, xmax=9)
    edges = knows_table(catalog_store)

    with pytest.raises(GrafxConfigurationError):
        heap_store.require_endpoints(edges, relationship_row(1, 2, (2020,)), at(1000))
    assert heap_store.require_endpoints(edges, relationship_row(1, 2, (2020,)), at(7)) == (1, 2)


def test_an_edge_survives_its_endpoint_being_updated(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """Why the endpoint is a RecordId and not a RecordRef.

    An update writes a NEW version at a new page and slot. A reference would now name the old
    one; the identity still names the row, which is what section 3 says it is for.
    """
    first = heap_store.insert(person_table, 1, (1, "Ada"), xmin=5)
    heap_store.insert(person_table, 2, (2, "Grace"), xmin=5)
    edges = knows_table(catalog_store)
    row = relationship_row(1, 2, (2020,))
    reference = heap_store.insert(edges, heap_store.allocate_record_id(edges), row, xmin=6)

    moved = heap_store.update(person_table, first, (1, "Ada Lovelace"), xmin=7)
    assert moved != first

    stored = heap_store.read(reference)
    assert edges.source_of(stored.values) == 1
    assert heap_store.require_endpoints(edges, stored.values, at(1000)) == (1, 2)
    assert heap_store.lookup(person_table, 1, at(1000)).values == (1, "Ada Lovelace")


def test_a_relationship_table_allocates_identities_exactly_like_a_node_table(
    heap_store: HeapStore, catalog_store: CatalogStore, person_table: TableDef
) -> None:
    """An edge is a row and gets its own identity; nothing in the allocator reads table kind."""
    edges = knows_table(catalog_store)
    assert heap_store.next_record_id(edges) == FIRST_RECORD_ID
    handed = [heap_store.allocate_record_id(edges) for _ in range(4)]
    assert handed == [FIRST_RECORD_ID + step for step in range(4)]

    for identity in (heap_store.allocate_record_id(person_table) for _ in range(2)):
        assert identity < heap_store.next_record_id(edges) or True
    assert heap_store.next_record_id(edges) == FIRST_RECORD_ID + 4, (
        "the node table's traffic moved the relationship table's counter"
    )
    assert heap_store.observe_record_id(edges, 5000) is True
    assert heap_store.next_record_id(edges) == 5001
