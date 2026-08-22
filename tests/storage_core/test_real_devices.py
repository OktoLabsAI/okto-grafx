"""The storage core over the real storage adapters, not over a double.

Everything else in this directory runs against the MemoryDevice of the local conftest, which is
fast and controllable but is still a test double. This file is the proof that the same code
works against the delivered adapters of C2: an in-memory device, a real directory on this
platform, and the deterministic fault-injecting twin.

The fault bench earns its place here for one reason: a partial page write is exactly the failure
the torn-read protocol of CONTRACT.md section 6.3 exists for, and reproducing it from a seed is
the only way to test it without a second process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice, FaultPlan
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxTransactionStateError,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import PageType
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.engine.buffer_pool import BufferPool, apply_page_image
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore

from .conftest import RecordingMetrics, SnapshotDouble

PAGE_SIZE: int = 512


def person() -> TableDef:
    """Return the table these tests store."""
    return TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )


def pool_over(device: StorageDevice, *, budget_pages: int = 6) -> BufferPool:
    """Return a buffer pool over any device satisfying the port."""
    return BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size * budget_pages,
        db_label="real",
    )


def exercise(pool: BufferPool) -> None:
    """Create a catalog and a heap, write records, and read every one of them back cold."""
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    table = person()
    catalog.catalog.add_table(table)
    catalog.save()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    expected = {}
    for record_id in range(25):
        name = "n" * (record_id * 13 % 200)
        heap.insert(table, record_id, (record_id, name), xmin=10)
        expected[record_id] = (record_id, name)
    long_ref = heap.insert(table, 999, (999, "L" * 3000), xmin=10)
    pool.flush()
    pool.invalidate()

    reopened = CatalogStore(pool)
    loaded = reopened.load()
    assert loaded == catalog.catalog
    reread = HeapStore(pool, reopened)
    stored = {
        version.record_id: version.values
        for _ref, version in reread.scan(loaded.table("Person"), SnapshotDouble(100))
    }
    assert stored.pop(999) == (999, "L" * 3000)
    assert stored == expected
    assert reread.read(long_ref).values[1] == "L" * 3000


def test_the_storage_core_runs_over_the_in_memory_device() -> None:
    device = MemoryStorageDevice(page_size=PAGE_SIZE)
    exercise(pool_over(device))


def test_the_storage_core_runs_over_a_real_directory(tmp_path: Path) -> None:
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        exercise(pool_over(device))
    finally:
        device.close()


def test_a_database_written_to_disk_is_read_back_by_a_second_device(tmp_path: Path) -> None:
    root = tmp_path / "db"
    writer = LocalStorageDevice(root, page_size=PAGE_SIZE)
    table = person()
    try:
        pool = pool_over(writer)
        catalog = CatalogStore(pool)
        catalog.bootstrap()
        catalog.catalog.add_table(table)
        catalog.save()
        heap = HeapStore(pool, catalog)
        heap.bootstrap()
        heap.insert(table, 1, (1, "Ada"), xmin=10)
        pool.flush()
        writer.durable_barrier()
    finally:
        writer.close()

    reader = LocalStorageDevice(root, page_size=PAGE_SIZE)
    try:
        pool = pool_over(reader)
        catalog = CatalogStore(pool)
        loaded = catalog.load()
        assert loaded.table("Person") == table
        heap = HeapStore(pool, catalog)
        rows = [version.values for _ref, version in heap.scan(table, SnapshotDouble(100))]
        assert rows == [(1, "Ada")]
    finally:
        reader.close()


def test_a_partially_written_page_is_detected_rather_than_believed() -> None:
    inner = MemoryStorageDevice(page_size=PAGE_SIZE)
    device = FaultInjectingStorageDevice(
        inner,
        seed=20260819,
        plan=FaultPlan(
            partial_write_method="write_page",
            partial_write_occurrence=1,
            partial_write_bytes=PAGE_SIZE // 2,
        ),
    )
    pool = pool_over(device)
    device.create("heap.dat")
    page = pool.allocate("heap.dat", int(PageType.HEAP))
    page.insert_slot(b"a payload that the device will only half write")
    pool.unpin("heap.dat", page.page_index, dirty=True)
    pool.flush()
    pool.invalidate()
    with pytest.raises(GrafxCorruptionDetected) as raised:
        pool.pin("heap.dat", 0)
    assert raised.value.details["file"] == "heap.dat"
    assert raised.value.details["page"] == 0
    assert raised.value.retryable is False


def test_the_fault_bench_is_reproducible_from_its_seed() -> None:
    outcomes = []
    for _attempt in range(2):
        device = FaultInjectingStorageDevice(
            MemoryStorageDevice(page_size=PAGE_SIZE),
            seed=7,
            plan=FaultPlan(
                partial_write_method="write_page",
                partial_write_occurrence=2,
                partial_write_bytes=64,
            ),
        )
        pool = pool_over(device)
        device.create("heap.dat")
        for number in range(3):
            page = pool.allocate("heap.dat", int(PageType.HEAP))
            page.insert_slot(f"page-{number}".encode())
            pool.unpin("heap.dat", page.page_index, dirty=True)
        pool.flush()
        pool.invalidate()
        readable = []
        for index in range(3):
            try:
                pool.pin("heap.dat", index)
                readable.append(index)
                pool.unpin("heap.dat", index)
            except GrafxCorruptionDetected:
                pass
        outcomes.append(tuple(readable))
    assert outcomes[0] == outcomes[1]
    assert len(outcomes[0]) == 2, "exactly the half-written page is unreadable"


# --- two participants over one directory: what a live reader must see ---------------------------


def participant(root: Path) -> tuple[BufferPool, CatalogStore, HeapStore]:
    """Return one participant over the directory: its own device, pool, catalog and heap.

    Its own device and its own pool is what makes this a participant rather than a second handle:
    two processes share the directory and share nothing else, and every counter this component
    keeps is process-local by construction.
    """
    device = LocalStorageDevice(root, page_size=PAGE_SIZE)
    pool = BufferPool(
        device,
        PageCodecV1(device.page_size),
        RecordingMetrics(),
        budget_bytes=device.page_size * 64,
        db_label="crossdb",
    )
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    return pool, catalog, heap


def visible_ids(heap: HeapStore, table: TableDef, read_lsn: int = 1000) -> list[int]:
    """Return the record ids a snapshot at that point can see, in order."""
    return sorted(
        version.record_id for _ref, version in heap.scan(table, SnapshotDouble(read_lsn))
    )


def written_table(catalog: CatalogStore) -> TableDef:
    """Install the table both participants use, and persist it."""
    definition = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(definition)
    catalog.save()
    return definition


def test_a_live_participant_sees_rows_another_committed_after_it_read(tmp_path: Path) -> None:
    """The defect C5 found, and the property D1 actually asks for.

    A participant that had already read went on answering from frames it cached before another
    process committed: no error, no missing file, just fewer rows than exist. A participant that
    opened AFTER the commit saw everything, which is what made it look like it worked.

    This is not the snapshot guarantee doing its job. The reader below takes a NEW snapshot after
    the foreign commit, and a new snapshot is entitled to nothing except the truth at or below
    its own read point.
    """
    writer_pool, writer_catalog, writer_heap = participant(tmp_path)
    table = written_table(writer_catalog)
    writer_heap.insert(table, 1, (1, "before"), xmin=5)
    writer_pool.checkpoint()

    reader_pool, reader_catalog, reader_heap = participant(tmp_path)
    reader_table = reader_catalog.catalog.table("Person")
    assert visible_ids(reader_heap, reader_table) == [1], "the reader must read first"

    writer_heap.insert(table, 2, (2, "after"), xmin=6)
    writer_heap.insert(table, 3, (3, "later"), xmin=7)
    writer_pool.checkpoint()

    assert reader_pool.begin_read_view() is True
    assert visible_ids(reader_heap, reader_table) == [1, 2, 3]


def test_every_read_door_of_a_live_participant_answers_from_the_new_view(
    tmp_path: Path,
) -> None:
    """A66.1: the unit is the door, not the store. One door left answering from a stale frame is
    the same defect with a smaller blast radius."""
    writer_pool, writer_catalog, writer_heap = participant(tmp_path)
    table = written_table(writer_catalog)
    first = writer_heap.insert(table, 1, (1, "before"), xmin=5)
    writer_pool.checkpoint()

    reader_pool, reader_catalog, reader_heap = participant(tmp_path)
    reader_table = reader_catalog.catalog.table("Person")
    assert visible_ids(reader_heap, reader_table) == [1]
    before_pages = reader_heap.pages_of(reader_table)
    before_extent = reader_heap.extent_of(reader_table)

    for number in range(2, 40):
        writer_heap.insert(table, number, (number, "L" * 120), xmin=6)
    writer_pool.checkpoint()
    reader_pool.begin_read_view()

    assert visible_ids(reader_heap, reader_table) == list(range(1, 40))
    assert [
        version.record_id for _ref, version in reader_heap.scan_all(reader_table)
    ] == list(range(1, 40))
    assert reader_heap.lookup(reader_table, 39, SnapshotDouble(1000)) is not None
    assert reader_heap.read(first).record_id == 1
    grown = reader_heap.pages_of(reader_table)
    assert len(grown) > len(before_pages), "the chain grew and the reader never saw it"
    after_extent = reader_heap.extent_of(reader_table)
    assert after_extent is not None and before_extent is not None
    assert after_extent.page_count == len(grown)
    assert after_extent.next_record_id > before_extent.next_record_id


def test_a_live_participant_sees_a_table_another_created(tmp_path: Path) -> None:
    """The same defect in the other store, and the worse half of it: the catalog in memory is a
    derived answer that no cache drop refreshes, because dropping frames says nothing to an
    object that is not holding one."""
    writer_pool, writer_catalog, _writer_heap = participant(tmp_path)
    written_table(writer_catalog)
    writer_pool.checkpoint()

    reader_pool, reader_catalog, _reader_heap = participant(tmp_path)
    assert [entry.name for entry in reader_catalog.catalog.tables()] == ["Person"]

    writer_catalog.catalog.add_table(
        TableDef(
            table_id=writer_catalog.catalog.next_table_id(),
            name="Company",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
            primary_key="id",
        )
    )
    writer_catalog.save()
    writer_pool.checkpoint()
    reader_pool.begin_read_view()

    assert [entry.name for entry in reader_catalog.catalog.tables()] == ["Person", "Company"]


def test_a_read_view_that_nothing_has_changed_costs_nothing(tmp_path: Path) -> None:
    """The token is what keeps the boundary affordable enough to be called every time.

    Without one, a participant that begins a transaction per read pays a full cache drop for
    every one of them -- and a door that is expensive to call correctly gets called wrongly.
    """
    pool, catalog, heap = participant(tmp_path)
    table = written_table(catalog)
    heap.insert(table, 1, (1, "row"), xmin=5)
    pool.checkpoint()

    assert pool.begin_read_view("lsn-7") is True
    resident = [
        index
        for index in range(pool.storage.page_count(heap.file))
        if pool.is_resident(heap.file, index)
    ]
    visible_ids(heap, table)
    assert pool.begin_read_view("lsn-7") is False, "the same token dropped the cache anyway"
    assert any(pool.is_resident(heap.file, index) for index in range(1, 3)), (
        "an unchanged view still threw the cache away"
    )
    assert pool.begin_read_view("lsn-8") is True
    assert not resident or not all(
        pool.is_resident(heap.file, index) for index in resident
    )


def test_a_read_view_with_no_token_always_re_derives(tmp_path: Path) -> None:
    """A caller with nothing to watch must get the safe answer, not the fast one."""
    pool, catalog, heap = participant(tmp_path)
    table = written_table(catalog)
    heap.insert(table, 1, (1, "row"), xmin=5)
    pool.checkpoint()
    visible_ids(heap, table)

    assert pool.begin_read_view() is True
    assert pool.begin_read_view() is True
    assert pool.begin_read_view(None) is True


def test_beginning_a_read_view_writes_dirty_frames_before_it_forgets_them(
    tmp_path: Path,
) -> None:
    """It forgets what was read, never what was written -- the same promise invalidate makes."""
    pool, catalog, heap = participant(tmp_path)
    table = written_table(catalog)
    heap.insert(table, 1, (1, "row"), xmin=5)

    pool.begin_read_view()

    reader_pool, reader_catalog, reader_heap = participant(tmp_path)
    assert visible_ids(reader_heap, reader_catalog.catalog.table("Person")) == [1]


def test_a_participant_with_unsaved_schema_is_told_rather_than_silently_reloaded(
    tmp_path: Path,
) -> None:
    """The D7 rule survives the fix: re-deriving must never be a way to lose a caller's work."""
    writer_pool, writer_catalog, _writer_heap = participant(tmp_path)
    written_table(writer_catalog)
    writer_pool.checkpoint()

    reader_pool, reader_catalog, _reader_heap = participant(tmp_path)
    reader_catalog.catalog.add_table(
        TableDef(
            table_id=99,
            name="Mine",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
            primary_key="id",
        )
    )
    assert reader_catalog.has_unsaved_changes()

    writer_catalog.catalog.add_table(
        TableDef(
            table_id=writer_catalog.catalog.next_table_id(),
            name="Theirs",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
            primary_key="id",
        )
    )
    writer_catalog.save()
    writer_pool.checkpoint()
    reader_pool.begin_read_view()

    with pytest.raises(GrafxTransactionStateError) as raised:
        reader_catalog.refresh()
    assert raised.value.details["field"] == "structure_epoch"


# --- a schema change is staged, not written: over a real directory ------------------------------


def commit_staged(
    pool: BufferPool, file: str, images: tuple[tuple[int, bytes], ...], *, csn: int
) -> None:
    """Apply staged page images the way CONTRACT.md section 8.5 step 6 applies them."""
    for page_index, image in sorted(images):
        page = pool.codec.decode_page(image, verify=True)
        if page.page_lsn < csn:
            page.page_lsn = csn
        apply_page_image(pool, file, page_index, pool.codec.encode_page(page))
    pool.flush(file)


def staged_change(store: CatalogStore, name: str, table_id: int, columns: int = 2):
    """Build one CREATE TABLE on a copy of the stored catalog and stage its images."""
    catalog = store.read_from_pages()
    catalog.add_table(
        TableDef(
            table_id=table_id,
            name=name,
            kind="node",
            columns=tuple(
                ColumnDef(name=f"column_{index}", type=ValueType.STRING)
                for index in range(columns)
            ),
        )
    )
    return store.stage(catalog)


def test_a_refused_schema_change_leaves_the_catalog_file_on_disk_byte_identical(
    tmp_path: Path,
) -> None:
    """The file on this platform's filesystem, not a double: same length, same bytes.

    A refused statement had already written its pages through the pool, so the next flush of the
    catalog file carried them to disk and the file header stopped describing the chain. Staging
    hands back bytes instead, and a refusal is a value nobody kept -- so the file is not merely
    logically unchanged, it is unchanged.
    """
    pool, catalog, _heap = participant(tmp_path)
    written_table(catalog)
    pool.checkpoint()
    before = (tmp_path / catalog.file).read_bytes()

    staged = staged_change(catalog, "Refused", 90, columns=60)
    assert len(staged) >= 3, "the change has to need a page the file does not have"
    del staged
    pool.checkpoint()

    assert (tmp_path / catalog.file).read_bytes() == before
    assert not catalog.catalog.has_table("Refused")

    reader_pool, reader_catalog, _reader_heap = participant(tmp_path)
    assert not reader_catalog.load().has_table("Refused")
    assert reader_pool.used_bytes() >= 0


def test_a_committed_schema_change_is_visible_to_a_second_participant(
    tmp_path: Path,
) -> None:
    """Two devices, two pools, two stores: they share the directory and nothing else."""
    pool, catalog, _heap = participant(tmp_path)
    pool.checkpoint()

    commit_staged(pool, catalog.file, staged_change(catalog, "Person", 1, columns=60), csn=31)

    _reader_pool, reader_catalog, _reader_heap = participant(tmp_path)
    loaded = reader_catalog.load()
    assert loaded.has_table("Person")
    assert len(loaded.table("Person").columns) == 60


def test_a_participant_that_had_already_read_sees_a_committed_schema_change_on_a_new_view(
    tmp_path: Path,
) -> None:
    """L22 on the derived answer that holds no frames.

    The reader's catalog is derived from pages it already walked, and every epoch this component
    keeps is process-local, so nothing the WRITER does moves the reader's reading. The reader
    supplies the signal it already watches through begin_read_view, and the catalog -- which
    caches nothing the pool can drop -- re-reads on exactly that signal.
    """
    writer_pool, writer_catalog, _writer_heap = participant(tmp_path)
    writer_pool.checkpoint()

    reader_pool, reader_catalog, _reader_heap = participant(tmp_path)
    assert reader_catalog.catalog.is_empty(), "the reader has to have derived an answer first"

    commit_staged(
        writer_pool, writer_catalog.file, staged_change(writer_catalog, "Person", 1), csn=41
    )

    assert not reader_catalog.catalog.has_table(
        "Person"
    ), "C1 cannot see a foreign commit on its own, and must not pretend to"
    assert reader_pool.begin_read_view("commit-41") is True
    assert reader_catalog.catalog.has_table("Person")
    assert reader_pool.begin_read_view("commit-41") is False
