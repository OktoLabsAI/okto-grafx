"""A working stack for the executor tests: real stores, real index, real vector subsystem.

The doubles here are the two the engine cannot supply for itself -- a snapshot and a transaction
-- and nothing else. Everything the query engine reads through is the real component: the C1
buffer pool, catalog and heap, the C7 index framework, the C9 vector subsystem. A test that
passes against a hand-written double of the thing it consumes proves that the double agrees with
the test, which is the failure L5 describes.

Rows are written into the heap DIRECTLY, by the fixture, and never through the engine under
test. That is amendment A92: a setup loop that arranges state with the behaviour being verified
cannot fail honestly, because a regression in the write path would quietly change what the read
path is asked about.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import Csn, PageIndex, RecordRef
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import Value, ValueType, VectorValue
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.txn.context import RowIntent, RowOperation
from okto_grafx.domain.txn.partitions import partition_of
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager, ProximityIndex
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.vector_engine import VectorEngine

PAGE_SIZE: int = 1024
"""Small enough that a table spans several pages in a handful of rows."""

SPACE_NAME: str = "minilm_v2"
SPACE_DIMENSION: int = 4

VECTOR_INDEX_PREFIX: str = "vector_"
"""The prefix the vector subsystem gives the index it attaches to a (table, space) pair."""


class MemoryDevice:
    """An in-memory StorageDevice with the paged and append-only semantics of the port."""

    def __init__(self, page_size: int = PAGE_SIZE) -> None:
        self._page_size = page_size
        self._pages: dict[str, list[bytes]] = {}
        self._logs: dict[str, bytearray] = {}

    @property
    def name(self) -> str:
        """Return the name this device reports."""
        return "query-memory"

    @property
    def page_size(self) -> int:
        """Return the size of one page."""
        return self._page_size

    def exists(self, file: str) -> bool:
        """Return True when the device holds that file."""
        return file in self._pages

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create one file, refusing an existing name when the caller asked exclusively."""
        if exclusive and self.exists(file):
            raise GrafxUnsupportedOperation(f"File {file!r} already exists.", file=file)
        self._pages.setdefault(file, [])
        self._logs.setdefault(file, bytearray())

    def remove(self, file: str) -> None:
        """Forget one file."""
        self._pages.pop(file, None)
        self._logs.pop(file, None)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every file whose name starts with the prefix, sorted."""
        return tuple(sorted(name for name in self._pages if name.startswith(prefix)))

    def file_size(self, file: str) -> int:
        """Return how many bytes the file holds."""
        return len(self._logs.get(file, b"")) + len(self._pages.get(file, ())) * self._page_size

    def atomic_replace(self, source: str, target: str) -> None:
        """Publish one file over another."""
        self._pages[target] = self._pages.pop(source, [])
        self._logs[target] = self._logs.pop(source, bytearray())

    def recycle(self, file: str) -> bool:
        """Release one file; the space returns at once here."""
        self.remove(file)
        return True

    def page_count(self, file: str) -> int:
        """Return how many pages the file holds."""
        return len(self._pages.get(file, ()))

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by that many zero-filled pages and return the first new index."""
        pages = self._pages.setdefault(file, [])
        first = len(pages)
        pages.extend(bytes(self._page_size) for _ in range(count))
        return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return the bytes of one allocated page."""
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages):
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} is not allocated.", file=file, page=page_index
            )
        return pages[page_index]

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Write one whole allocated page."""
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages) or len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Refused a page write to {file!r} at {page_index}.", file=file, page=page_index
            )
        pages[page_index] = bytes(data)

    def append_log(self, file: str, payload: bytes) -> int:
        """Append to a log and return its new size."""
        log = self._logs.setdefault(file, bytearray())
        log.extend(payload)
        return len(log)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return a slice of a log."""
        return bytes(self._logs.get(file, bytearray())[offset : offset + length])

    def log_size(self, file: str) -> int:
        """Return how many bytes a log holds."""
        return len(self._logs.get(file, bytearray()))

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink a log, refusing to grow one."""
        log = self._logs.setdefault(file, bytearray())
        if size > len(log):
            raise GrafxUnsupportedOperation(
                f"truncate_log only shrinks; {file!r} holds {len(log)} bytes.", file=file
            )
        del log[size:]

    def durable_barrier(self, file: str | None = None) -> None:
        """Do nothing; this device holds everything in memory."""
        return None


class ManualClock:
    """A clock that moves only when a test asks it to."""

    def __init__(self, monotonic: float = 100.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        """Return the local reading, advancing it a little so a duration is never negative."""
        self._monotonic += 0.001
        return self._monotonic

    def wall(self) -> float:
        """Return the wall reading."""
        return self._wall


class RecordingMetrics:
    """A metrics sink that keeps every emission, so a test can assert what was published."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.registered: list[str] = []
        self.emissions: list[tuple[str, str, float, dict[str, str]]] = []

    @property
    def enabled(self) -> bool:
        """Return whether this sink collects anything."""
        return self._enabled

    def register(self, descriptor: object) -> None:
        """Record one declared metric."""
        self.registered.append(descriptor.name)  # type: ignore[attr-defined]

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        """Record one counter emission."""
        self.emissions.append(("increment", name, value, dict(labels or {})))

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        """Record one gauge emission."""
        self.emissions.append(("gauge", name, value, dict(labels or {})))

    def observe(self, name: str, value: float, labels: object = None) -> None:
        """Record one histogram observation."""
        self.emissions.append(("observe", name, value, dict(labels or {})))

    def time(self, name: str, labels: object = None) -> object:
        """Return a context manager that records nothing; the engine times explicitly."""
        return _NullTimer()

    def named(self, name: str) -> list[tuple[str, float, dict[str, str]]]:
        """Return every emission of one metric, in order."""
        return [
            (kind, value, labels)
            for kind, emitted, value, labels in self.emissions
            if emitted == name
        ]


class _NullTimer:
    """A timing context manager that records nothing."""

    def __enter__(self) -> None:
        """Enter and record nothing."""
        return None

    def __exit__(self, *exception: object) -> bool:
        """Leave and record nothing."""
        return False


@dataclass(frozen=True, slots=True)
class SnapshotDouble:
    """The visibility predicate of CONTRACT.md section 8.5, and nothing else."""

    read_lsn: int

    def visible(self, xmin: int, xmax: int) -> bool:
        """Return whether a version born at xmin and ended at xmax belongs to this view."""
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)


@dataclass(slots=True)
class PartitionsDouble:
    """The one thing a transaction's owner is asked for: which partition a key belongs to."""

    partitions_per_table: int = 8

    def partition_of(self, table_id: int, key: bytes) -> int:
        """Return the partition a row of this table with this key belongs to."""
        return partition_of(table_id, key, self.partitions_per_table)


@dataclass(slots=True)
class TransactionDouble:
    """The part of a transaction a statement touches, with the SAME doors the real one has.

    It records rather than reimplements: ``stage_row_insert`` validates its arguments the way
    the real context does and appends a real ``RowIntent``, so a test that passes here is
    exercising the real argument contract. A double weaker than the thing it stands in for
    certifies nothing (LESSONS L12), which is also why the write path has an end-to-end test
    through the real composition root beside these.
    """

    snapshot: SnapshotDouble
    txn_id: int = 1
    staged_records: list[object] = field(default_factory=list)
    staged_pages: dict[tuple[str, int], bytes] = field(default_factory=dict)
    row_intents: list[RowIntent] = field(default_factory=list)
    row_refs: list[object] = field(default_factory=list)
    read_partitions: set[int] = field(default_factory=set)
    write_partitions: set[int] = field(default_factory=set)
    owner: PartitionsDouble = field(default_factory=PartitionsDouble)

    def stage_record(self, record: object) -> None:
        """Stage one log record."""
        self.staged_records.append(record)

    def stage_page_image(self, file: str, page_index: int, image: bytes) -> None:
        """Stage the bytes one page should hold."""
        self.staged_pages[(file, page_index)] = bytes(image)

    def stage_row_insert(
        self, table: object, values: object, *, record_id: int | None = None
    ) -> None:
        """Stage one row, refusing the arguments the real context refuses."""
        if table is None:
            raise GrafxConfigurationError(
                "A staged row must name the table it belongs to.", field="table"
            )
        if record_id is not None and (
            isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1
        ):
            raise GrafxConfigurationError(
                f"A staged row identity must be a positive integer; got {record_id!r}.",
                field="record_id",
            )
        self.row_intents.append(
            RowIntent(table=table, values=tuple(values), record_id=record_id)
        )

    def stage_row_update(self, table: object, reference: object, values: object) -> None:
        """Stage a new version of a stored row, refusing what the real context refuses.

        The double carries this door because a double weaker than the real adapter certifies
        nothing (LESSONS L12): while it lacked the method, every SET reaching it raised
        ``AttributeError`` -- a non-``Grafx*`` escape -- and the tests read that as the engine
        correctly refusing an operator it had no door for.
        """
        if table is None:
            raise GrafxConfigurationError(
                "A staged row must name the table it belongs to.", field="table"
            )
        if reference is None:
            raise GrafxConfigurationError(
                "A staged row update must name the row it replaces.", field="reference"
            )
        self.row_intents.append(
            RowIntent(
                table=table,
                values=tuple(values),
                operation=RowOperation.UPDATE,
                reference=reference,
            )
        )

    def stage_row_delete(self, table: object, reference: object) -> None:
        """Stage the end of a stored row, refusing what the real context refuses."""
        if table is None:
            raise GrafxConfigurationError(
                "A staged row must name the table it belongs to.", field="table"
            )
        if reference is None:
            raise GrafxConfigurationError(
                "A staged row delete must name the row it ends.", field="reference"
            )
        self.row_intents.append(
            RowIntent(table=table, operation=RowOperation.DELETE, reference=reference)
        )

    def note_write(self, partition: int) -> None:
        """Record that this transaction wrote a partition."""
        self.write_partitions.add(partition)

    def note_read(self, partition: int) -> None:
        """Record that this transaction read a partition."""
        self.read_partitions.add(partition)


@dataclass(slots=True)
class QueryStack:
    """Everything one executor test needs, assembled once."""

    device: MemoryDevice
    codec: PageCodecV1
    pool: BufferPool
    catalog_store: CatalogStore
    heap: HeapStore
    indexes: IndexManager
    vectors: VectorEngine
    engine: QueryEngine
    metrics: RecordingMetrics
    clock: ManualClock
    tables: dict[str, TableDef] = field(default_factory=dict)

    def table(self, name: str) -> TableDef:
        """Return one table of this stack by name."""
        return self.catalog_store.catalog.table(name)

    def snapshot(self, read_lsn: int = 1000) -> SnapshotDouble:
        """Return a snapshot at that log position."""
        return SnapshotDouble(read_lsn=read_lsn)

    def transaction(self, read_lsn: int = 1000) -> TransactionDouble:
        """Return a transaction reading at that log position."""
        return TransactionDouble(snapshot=self.snapshot(read_lsn))

    def insert(
        self, table_name: str, record_id: int, values: Sequence[Value], csn: Csn = 1
    ) -> RecordRef:
        """Write one row directly into the heap and into the row indexes that cover it.

        The VECTOR index of a table is deliberately not written here. Its entries are keyed on
        the vector and the record identity rather than on the row's positional key, so the
        generic row-insert door would file an entry under a key no search will ever ask for --
        and then :meth:`add_vector` would file the real one, leaving two entries for one row.
        The fixture writes each index through the door that owns it.
        """
        table = self.table(table_name)
        ref = self.heap.insert(table, record_id, tuple(values), csn)
        transaction = self.transaction()
        for index in self.indexes.indexes_for(table.table_id):
            if index.name.startswith(VECTOR_INDEX_PREFIX):
                continue
            index.stage_insert(
                transaction, index.definition.key_for(tuple(values)), ref, csn
            )
        self.indexes.commit(transaction, csn)
        return ref

    def end(self, table_name: str, ref: RecordRef, values: Sequence[Value], csn: Csn) -> None:
        """End one row with a delete, in the heap and in every index that covers it."""
        table = self.table(table_name)
        self.heap.delete(table, ref, csn)
        transaction = self.transaction()
        for index in self.indexes.indexes_for(table.table_id):
            if index.name.startswith(VECTOR_INDEX_PREFIX):
                continue
            index.stage_delete(
                transaction, index.definition.key_for(tuple(values)), ref, csn
            )
        self.indexes.commit(transaction, csn)

    def add_vector(
        self, record_id: int, ref: RecordRef, components: Sequence[float], csn: Csn = 1
    ) -> None:
        """Add one vector version to the embedding space of this stack."""
        transaction = self.transaction()
        record = self.vectors.stage_insert(
            SPACE_NAME, record_id, ref, tuple(components), csn, transaction
        )
        self.indexes.commit(transaction, csn)
        assert record is not None


def build_query_stack(*, budget_pages: int = 64, with_indexes: bool = True) -> QueryStack:
    """Assemble a database with a person table, a chunk table and one embedding space."""
    device = MemoryDevice()
    codec = PageCodecV1(page_size=device.page_size)
    metrics = RecordingMetrics()
    clock = ManualClock()
    pool = BufferPool(
        device,
        codec,
        metrics,
        budget_bytes=budget_pages * device.page_size,
        db_label="query",
    )
    catalog_store = CatalogStore(pool)
    catalog_store.bootstrap()
    heap = HeapStore(pool, catalog_store)
    heap.bootstrap()
    catalog = catalog_store.catalog
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name=SPACE_NAME,
            dimension=SPACE_DIMENSION,
            metric=DistanceMetric.COSINE,
            normalized=False,
        )
    )
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Person",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="name", type=ValueType.STRING),
                ColumnDef(name="age", type=ValueType.INT64),
                ColumnDef(name="city", type=ValueType.STRING),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="Chunk",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="layer", type=ValueType.INT64),
                ColumnDef(
                    name="embedding", type=ValueType.VECTOR_F32, vector_space=SPACE_NAME
                ),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=3,
            name="Knows",
            kind="rel",
            columns=(ColumnDef(name="since", type=ValueType.INT64),),
            from_table="Person",
            to_table="Person",
        )
    )
    catalog_store.save()
    indexes = IndexManager(pool, heap, metrics)
    if with_indexes:
        indexes.register(
            HashIndex(
                IndexDefinition(
                    name="person_id",
                    table_id=1,
                    table_name="Person",
                    positions=(0,),
                    visibility=IndexVisibility.EXACT,
                    bucket_count=4,
                ),
                pool,
                metrics,
            )
        )
        indexes.register(
            ProximityIndex(
                IndexDefinition(
                    name="person_city",
                    table_id=1,
                    table_name="Person",
                    positions=(3,),
                    visibility=IndexVisibility.PROXIMITY,
                    bucket_count=4,
                ),
                pool,
                metrics,
            )
        )
    vectors = VectorEngine(
        catalog=catalog_store,
        heap=heap,
        math=PureVectorMath(),
        metrics=metrics,
        clock=clock,
        pool=pool,
        indexes=indexes,
        exact_scan_threshold=4096,
    )
    # An index covers a (table, space) pair, so it is created when the table declares the
    # column rather than when the space is declared. The fixture builds its tables directly on
    # the catalog, so it does the attaching directly too.
    vectors.attach(catalog.table("Chunk"), SPACE_NAME)
    engine = QueryEngine(
        catalog=catalog_store,
        heap=heap,
        pool=pool,
        metrics=metrics,
        clock=clock,
        indexes=indexes,
        vectors=vectors,
    )
    return QueryStack(
        device=device,
        codec=codec,
        pool=pool,
        catalog_store=catalog_store,
        heap=heap,
        indexes=indexes,
        vectors=vectors,
        engine=engine,
        metrics=metrics,
        clock=clock,
    )


def vector(components: Sequence[float]) -> VectorValue:
    """Return the stored shape of one embedding of the space this stack declares."""
    return VectorValue(values=tuple(float(item) for item in components), space_ref=1)
