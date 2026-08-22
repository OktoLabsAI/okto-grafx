"""Fixtures for the secondary-index framework (C7).

The doubles here belong to this directory rather than being shared, for the reason the storage
core gives for the same choice: a test that fails must fail because of the component under test
and not because another component reshaped a fake.

``MemoryDevice`` speaks the ``StorageDevice`` port exactly -- pages are allocated before they are
written, logs only grow at the end, nothing is written at an arbitrary offset -- plus two
deliberate hooks: a numbered write refusal, so a retryable device failure can be walked through
every step of an operation, and ``poke_page``, so a test can damage a page the way a device would
and watch the walk refuse instead of hang.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import Csn, PageIndex, RecordRef
from okto_grafx.domain.index import IndexDefinition, IndexVisibility
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import Value, ValueType
from okto_grafx.domain.page import HEADER_PAGE_INDEX
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager, ProximityIndex

SMALL_PAGE_SIZE: int = 512
"""A small page keeps the fixtures fast and makes a bucket chain grow in a few inserts."""

TEST_BUCKET_COUNT: int = 4
"""Few buckets, so a test can arrange two keys in one chain without searching for a collision."""


class MemoryDevice:
    """An in-memory StorageDevice with the append-only and paged semantics of the port."""

    def __init__(self, page_size: int = SMALL_PAGE_SIZE, *, name: str = "memory") -> None:
        self._page_size = page_size
        self._name = name
        self._pages: dict[str, list[bytes]] = {}
        self._logs: dict[str, bytearray] = {}
        self.read_calls: list[tuple[str, PageIndex]] = []
        self.write_calls: list[tuple[str, PageIndex]] = []
        self.barriers: list[str | None] = []
        self.writes_attempted: int = 0
        self.refused_writes: list[tuple[str, PageIndex]] = []
        self._refuse_at_write: int | None = None
        self._refusal: GrafxError | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def page_size(self) -> int:
        return self._page_size

    def exists(self, file: str) -> bool:
        return file in self._pages

    def create(self, file: str, *, exclusive: bool = True) -> None:
        if exclusive and self.exists(file):
            raise GrafxUnsupportedOperation(f"File {file!r} already exists.", file=file)
        self._pages.setdefault(file, [])
        self._logs.setdefault(file, bytearray())

    def remove(self, file: str) -> None:
        self._pages.pop(file, None)
        self._logs.pop(file, None)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        return tuple(sorted(name for name in self._pages if name.startswith(prefix)))

    def file_size(self, file: str) -> int:
        return len(self._pages.get(file, ())) * self._page_size + len(self._logs.get(file, b""))

    def atomic_replace(self, source: str, target: str) -> None:
        self._pages[target] = self._pages.pop(source, [])
        self._logs[target] = self._logs.pop(source, bytearray())

    def recycle(self, file: str) -> bool:
        self.remove(file)
        return True

    def page_count(self, file: str) -> int:
        return len(self._pages.get(file, ()))

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        pages = self._pages.setdefault(file, [])
        first = len(pages)
        pages.extend(bytes(self._page_size) for _ in range(count))
        return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages):
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} is not allocated.", file=file, page=page_index
            )
        self.read_calls.append((file, page_index))
        return pages[page_index]

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages) or len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Refused a page write to {file!r} at {page_index}.", file=file, page=page_index
            )
        self.writes_attempted += 1
        if self.writes_attempted == self._refuse_at_write and self._refusal is not None:
            self.refused_writes.append((file, page_index))
            self._refuse_at_write = None
            raise self._refusal
        self.write_calls.append((file, page_index))
        pages[page_index] = bytes(data)

    def refuse_write_number(self, number: int, error: GrafxError) -> None:
        """Arm one refusal for the Nth write-back this device is asked to perform."""
        self.writes_attempted = 0
        self._refuse_at_write = number
        self._refusal = error

    def disarm(self) -> None:
        """Stop refusing, which is what a caller retrying a transient failure meets."""
        self._refuse_at_write = None
        self._refusal = None

    def raw_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return the stored bytes of a page without going through the pool."""
        return self._pages[file][page_index]

    def poke_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Replace the stored bytes of a page, which is how a test damages one."""
        self._pages[file][page_index] = bytes(data)

    def append_log(self, file: str, payload: bytes) -> int:
        log = self._logs.setdefault(file, bytearray())
        log.extend(payload)
        return len(log)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        return bytes(self._logs.get(file, bytearray())[offset : offset + length])

    def log_size(self, file: str) -> int:
        return len(self._logs.get(file, bytearray()))

    def truncate_log(self, file: str, size: int) -> None:
        log = self._logs.setdefault(file, bytearray())
        if size > len(log):
            raise GrafxUnsupportedOperation(f"truncate_log only shrinks {file!r}.", file=file)
        del log[size:]

    def durable_barrier(self, file: str | None = None) -> None:
        self.barriers.append(file)


class RecordingMetrics:
    """A MetricsSink that keeps every registration and every emission with its labels."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.registered: dict[str, MetricDescriptor] = {}
        self.calls: list[tuple[str, str, float, dict[str, str]]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        self.registered[descriptor.name] = descriptor

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self._record("increment", name, value, labels)

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        self._record("set_gauge", name, value, labels)

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self._record("observe", name, value, labels)

    def _record(
        self, kind: str, name: str, value: float, labels: Mapping[str, str] | None
    ) -> None:
        if name not in self.registered:
            raise AssertionError(
                f"Metric {name!r} was emitted without being registered on this sink."
            )
        self.calls.append((kind, name, value, dict(labels or {})))

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> contextlib.AbstractContextManager[None]:
        return self._timed(name, dict(labels or {}))

    @contextlib.contextmanager
    def _timed(self, name: str, labels: dict[str, str]) -> Iterator[None]:
        self.calls.append(("time_enter", name, 0.0, labels))
        try:
            yield
        finally:
            self.calls.append(("time_exit", name, 0.0, labels))

    def snapshot(self) -> Mapping[str, object]:
        return {name: descriptor.kind.value for name, descriptor in self.registered.items()}

    def values_of(self, name: str) -> list[float]:
        """Return every value emitted under that metric name, in order."""
        return [value for _kind, emitted, value, _labels in self.calls if emitted == name]


class SnapshotDouble:
    """The visibility predicate of CONTRACT.md section 8.5, which C5 owns for real."""

    def __init__(self, read_lsn: int) -> None:
        self.read_lsn = read_lsn

    def visible(self, xmin: int, xmax: int) -> bool:
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)

    def __repr__(self) -> str:
        return f"SnapshotDouble(read_lsn={self.read_lsn})"


class TransactionDouble:
    """The part of a transaction an index needs: a number and a door to stage a record.

    It collects the records rather than logging them, which is what lets a test assert that a
    staged change reached the log and that an index page did not move until the commit.
    """

    def __init__(self, txn_id: int = 1, epoch: int = 1) -> None:
        self.txn_id = txn_id
        self.epoch = epoch
        self.staged: list[object] = []

    def stage_record(self, record: object) -> None:
        self.staged.append(record)

    def with_lsns(self, first: int) -> list[object]:
        """Return the staged records carrying consecutive log positions from ``first``.

        The log assigns positions on append, so a test replaying staged records has to give them
        the numbers a real append would have given them.
        """
        numbered: list[object] = []
        for offset, record in enumerate(self.staged):
            numbered.append(record.with_lsn(first + offset))
        return numbered


def make_pool(
    device: MemoryDevice,
    metrics: RecordingMetrics,
    *,
    budget_pages: int = 16,
    db_label: str = "testdb",
) -> BufferPool:
    """Build a pool over the device with a budget expressed in whole pages."""
    return BufferPool(
        device,
        PageCodecV1(device.page_size),
        metrics,
        budget_bytes=device.page_size * budget_pages,
        db_label=db_label,
    )


@pytest.fixture
def device() -> MemoryDevice:
    """Return a fresh in-memory device with small pages."""
    return MemoryDevice()


@pytest.fixture
def metrics() -> RecordingMetrics:
    """Return a metrics sink that records every registration and emission."""
    return RecordingMetrics()


@pytest.fixture
def pool(device: MemoryDevice, metrics: RecordingMetrics) -> BufferPool:
    """Return a buffer pool over the fixture device."""
    return make_pool(device, metrics)


@pytest.fixture
def catalog_store(pool: BufferPool) -> CatalogStore:
    """Return a bootstrapped catalog store."""
    store = CatalogStore(pool)
    store.bootstrap()
    return store


@pytest.fixture
def heap_store(pool: BufferPool, catalog_store: CatalogStore) -> HeapStore:
    """Return a bootstrapped heap store sharing the pool with the catalog."""
    store = HeapStore(pool, catalog_store)
    store.bootstrap()
    return store


@pytest.fixture
def person_table(catalog_store: CatalogStore) -> TableDef:
    """Return a two-column node table that is registered in the catalog."""
    table = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog_store.catalog.add_table(table)
    catalog_store.save()
    return table


@pytest.fixture
def manager(
    pool: BufferPool, heap_store: HeapStore, metrics: RecordingMetrics
) -> IndexManager:
    """Return an index manager over the fixture pool and heap."""
    return IndexManager(pool, heap_store, metrics)


def exact_definition(
    table: TableDef, *, name: str = "person_by_name", columns: Sequence[str] = ("name",)
) -> IndexDefinition:
    """Return the definition of an exact index over these columns of the table."""
    return IndexDefinition.on(
        table,
        name=name,
        columns=columns,
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
    )


def proximity_definition(
    table: TableDef, *, name: str = "person_near_name", columns: Sequence[str] = ("name",)
) -> IndexDefinition:
    """Return the definition of a proximity index over these columns of the table."""
    return IndexDefinition.on(
        table,
        name=name,
        columns=columns,
        visibility=IndexVisibility.PROXIMITY,
        bucket_count=TEST_BUCKET_COUNT,
    )


@pytest.fixture
def exact_index(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> HashIndex:
    """Return a registered exact hash index over the name column of the person table."""
    index = HashIndex(exact_definition(person_table), pool, metrics)
    manager.register(index)
    return index


@pytest.fixture
def proximity_index(
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> ProximityIndex:
    """Return a registered proximity index over the name column of the person table."""
    index = ProximityIndex(proximity_definition(person_table), pool, metrics)
    manager.register(index)
    return index


@pytest.fixture
def txn() -> TransactionDouble:
    """Return a transaction double that collects the records staged into it."""
    return TransactionDouble()


@pytest.fixture
def snapshots() -> Callable[[int], SnapshotDouble]:
    """Return a factory of snapshots at a given read position."""

    def build(read_lsn: int) -> SnapshotDouble:
        return SnapshotDouble(read_lsn)

    return build


def insert_row(
    heap: HeapStore, table: TableDef, record_id: int, values: tuple[Value, ...], xmin: Csn
) -> RecordRef:
    """Insert one row into the heap and return where it landed."""
    return heap.insert(table, record_id, values, xmin)


class Database:
    """One whole database of fixtures, for a test that needs a second, independent one.

    A test that compares what a commit produced against what a replay produced needs two of
    everything, and building them from the same function is what makes the comparison about the
    code rather than about how the two were set up.
    """

    __slots__ = (
        "device",
        "metrics",
        "pool",
        "catalog",
        "heap",
        "manager",
        "table",
        "exact",
        "proximity",
    )

    def __init__(self, *, name: str = "memory", budget_pages: int = 16) -> None:
        self.device = MemoryDevice(name=name)
        self.metrics = RecordingMetrics()
        self.pool = make_pool(self.device, self.metrics, budget_pages=budget_pages)
        self.catalog = CatalogStore(self.pool)
        self.catalog.bootstrap()
        self.heap = HeapStore(self.pool, self.catalog)
        self.heap.bootstrap()
        self.table = TableDef(
            table_id=self.catalog.catalog.next_table_id(),
            name="Person",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="name", type=ValueType.STRING),
            ),
            primary_key="id",
        )
        self.catalog.catalog.add_table(self.table)
        self.catalog.save()
        self.manager = IndexManager(self.pool, self.heap, self.metrics)
        self.exact = HashIndex(exact_definition(self.table), self.pool, self.metrics)
        self.manager.register(self.exact)
        self.proximity = ProximityIndex(
            proximity_definition(self.table), self.pool, self.metrics
        )
        self.manager.register(self.proximity)

    def insert(self, record_id: int, name: str, csn: Csn) -> RecordRef:
        """Insert one row into the heap without telling any index, and return where it landed."""
        return self.heap.insert(self.table, record_id, (record_id, name), csn)

    def key(self, record_id: int, name: str) -> bytes:
        """Return the key both indexes file that row under."""
        return self.exact.definition.key_for((record_id, name))

    def entries(self) -> dict[str, tuple[object, ...]]:
        """Return the entries of every index, keyed by index name, for comparison."""
        return {
            index.name: tuple(
                (entry.key, entry.ref, entry.versioned, entry.born_csn, entry.dead_csn)
                for entry in index.walk()
            )
            for index in self.manager.indexes()
        }


def build_database(*, name: str = "memory", budget_pages: int = 16) -> Database:
    """Return a whole database with both kinds of index registered."""
    return Database(name=name, budget_pages=budget_pages)


@pytest.fixture
def database() -> Database:
    """Return a database with both kinds of index registered."""
    return build_database()


def header_on_device(device: MemoryDevice, file: str) -> IndexHeader:
    """Return the index header a reader of the RAW DEVICE BYTES would find.

    Nothing here goes through a buffer pool, and that is the whole point. A durability claim read
    back through the pool that made the write is satisfied by the page cache: the assertion
    follows the writer's own answer instead of asking the device, which is how the flag this
    component's read gate depends on was asserted "durable" for a whole round while the file
    carried a zero. Any test about what SURVIVES asks here, or through :func:`cold_view`.
    """
    codec = PageCodecV1(device.page_size)
    page = codec.decode_page(device.raw_page(file, HEADER_PAGE_INDEX), page_index=HEADER_PAGE_INDEX)
    return IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))


def damage_index_header(
    device: MemoryDevice, file: str, offset: int, value: int
) -> IndexHeader:
    """Change one byte of the stored index header and leave the PAGE checksum correct.

    Damage that a checksum catches proves nothing about the guards behind it: the page refuses
    first and every later check is unreachable. This writes the page back through the codec, so
    the bytes are a page the device would hand over without complaint and the only thing wrong
    with them is the field under test. Returns the header the file now carries.
    """
    codec = PageCodecV1(device.page_size)
    page = codec.decode_page(device.raw_page(file, HEADER_PAGE_INDEX), page_index=HEADER_PAGE_INDEX)
    raw = bytearray(page.read_slot(INDEX_HEADER_SLOT))
    raw[offset] = value
    page.update_slot(INDEX_HEADER_SLOT, bytes(raw))
    device.poke_page(file, HEADER_PAGE_INDEX, codec.encode_page(page))
    return IndexHeader.decode(bytes(raw))


class ColdView:
    """A whole second participant over the same device, with a cache that has never been warm.

    This is what another process sees, and what this process sees after a restart: every page is
    read from the device because no frame of this pool has ever held one. A test that wants to
    know whether something reached the platter opens one of these instead of reusing the pool
    that wrote it.
    """

    __slots__ = ("pool", "catalog", "heap", "manager", "exact", "proximity")

    def __init__(self, source: Database) -> None:
        self.pool = make_pool(source.device, RecordingMetrics())
        self.catalog = CatalogStore(self.pool)
        self.catalog.bootstrap()
        self.heap = HeapStore(self.pool, self.catalog)
        self.heap.bootstrap()
        self.manager = IndexManager(self.pool, self.heap, RecordingMetrics())
        self.exact = HashIndex(
            exact_definition(source.table), self.pool, RecordingMetrics()
        )
        self.manager.register(self.exact)
        self.proximity = ProximityIndex(
            proximity_definition(source.table), self.pool, RecordingMetrics()
        )
        self.manager.register(self.proximity)


def cold_view(source: Database, *, publish: bool = True) -> ColdView:
    """Return a second participant reading the device the source database wrote.

    The heap and the catalog are flushed first, because a cold reader that cannot find the TABLE
    fails for a reason that has nothing to do with the index under test -- and a test that cannot
    distinguish "the index lost its flag" from "the catalog was still in the cache" measures
    neither (A72: prove the fixture produced the state it claims).
    """
    if publish:
        for file in (source.catalog.file, source.heap.file):
            source.pool.flush(file)
    return ColdView(source)
