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
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import PageType
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.engine.buffer_pool import BufferPool
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
