"""Fixtures for the vector subsystem (C9).

The doubles here are owned by this directory. A failure in this suite must be attributable to
C9, so the pieces a test builds on are either the real delivered components -- the in-memory
device, the page codec, the buffer pool, the catalog store and the heap store -- or a double
small enough to read in one screen.

Two of them exist to make a claim observable rather than to stand in for something:

* ``RecordingMetrics`` keeps every emission with its labels, so a test can assert that a regime
  was published rather than assuming it was;
* ``StepClock`` advances by a fixed amount on every reading, so a duration is a count of calls
  and never a wall reading. That is what keeps a latency assertion deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.ids import Csn, RecordRef
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType, VectorValue
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.rand import SplitMix64
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.vector_engine import VectorEngine

PAGE_SIZE: int = 4096
"""Large enough that a vector of a few dozen components fits inline in one heap slot."""

BUDGET_BYTES: int = 256 * PAGE_SIZE
"""Room for enough pages that no test in this suite meets the budget refusal by accident."""

FIXTURE_READ_LSN: int = 1_000_000
"""Closed-world ceiling maintained by the fixture's coupled heap/index helpers."""


@dataclass
class Emission:
    """One number a component published, with the labels it carried."""

    kind: str
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


class RecordingMetrics:
    """A MetricsSink that keeps everything, so a test can read what was published."""

    def __init__(self) -> None:
        self.registered: dict[str, MetricDescriptor] = {}
        self.emissions: list[Emission] = []
        self.timed: list[str] = []

    @property
    def enabled(self) -> bool:
        """Return True: this sink collects, which is the point of it."""
        return True

    def register(self, descriptor: MetricDescriptor) -> None:
        """Record a declaration, refusing a second, different declaration of one name."""
        existing = self.registered.get(descriptor.name)
        if existing is not None and existing != descriptor:
            raise AssertionError(f"metric {descriptor.name!r} registered twice, differently")
        self.registered[descriptor.name] = descriptor

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record one counter increment."""
        self.emissions.append(Emission("increment", name, value, dict(labels or {})))

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record one gauge setting."""
        self.emissions.append(Emission("gauge", name, value, dict(labels or {})))

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record one histogram observation."""
        self.emissions.append(Emission("observe", name, value, dict(labels or {})))

    @contextmanager
    def time(self, name: str, labels: Mapping[str, str] | None = None) -> Iterator[None]:
        """Record that a block was timed, without pretending to measure it."""
        self.timed.append(name)
        yield

    def snapshot(self) -> Mapping[str, object]:
        """Return the last value published under each name and label set."""
        current: dict[str, float] = {}
        for emission in self.emissions:
            key = emission.name
            if emission.labels:
                tail = ",".join(f"{k}={v}" for k, v in sorted(emission.labels.items()))
                key = f"{key}{{{tail}}}"
            if emission.kind == "increment":
                current[key] = current.get(key, 0.0) + emission.value
            else:
                current[key] = emission.value
        return current

    def values_of(self, name: str) -> list[Emission]:
        """Return every emission published under one metric name, in order."""
        return [emission for emission in self.emissions if emission.name == name]


class SilentMetrics:
    """The no-op shape of the port, for asserting that a hot path allocates nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        """Return False, which is what a hot path guards on."""
        return False

    def register(self, descriptor: MetricDescriptor) -> None:
        """Accept a declaration and forget it."""
        self.calls.append("register")

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record that something was published although the sink is disabled."""
        self.calls.append(f"increment:{name}")

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record that something was published although the sink is disabled."""
        self.calls.append(f"gauge:{name}")

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record that something was published although the sink is disabled."""
        self.calls.append(f"observe:{name}")

    @contextmanager
    def time(self, name: str, labels: Mapping[str, str] | None = None) -> Iterator[None]:
        """Do nothing, in the shape of the port."""
        self.calls.append(f"time:{name}")
        yield

    def snapshot(self) -> Mapping[str, object]:
        """Return nothing, which is what a no-op sink holds."""
        return {}


class StepClock:
    """A Clock whose monotonic reading advances by a fixed step on every call."""

    def __init__(self, step: float = 0.25) -> None:
        self.step = step
        self.reads = 0
        self._now = 1000.0

    def monotonic(self) -> float:
        """Return the next reading, which is the previous one plus the step."""
        self.reads += 1
        value = self._now
        self._now += self.step
        return value

    def wall(self) -> float:
        """Return a fixed human-facing reading; nothing in this component uses it."""
        return 1_700_000_000.0


class BackwardClock:
    """A Clock whose monotonic reading goes BACKWARDS, which the port forbids.

    The port says a monotonic reading never goes back, so a component may not produce a negative
    duration when a host adapter breaks that promise: a negative observation would be refused by
    a histogram or silently recorded as an impossible latency. The clamp that prevents it is only
    reachable through a clock like this one.
    """

    def __init__(self, step: float = 0.5) -> None:
        self.step = step
        self.reads = 0
        self._now = 1000.0

    def monotonic(self) -> float:
        """Return a reading earlier than the previous one."""
        self.reads += 1
        value = self._now
        self._now -= self.step
        return value

    def wall(self) -> float:
        """Return a fixed human-facing reading."""
        return 1_700_000_000.0


class RecordingEvents:
    """An EventSink that keeps every notice, so a lifecycle claim can be asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        """Record one notice."""
        self.events.append((event, dict(payload)))

    def names(self) -> list[str]:
        """Return the names of the notices in order."""
        return [name for name, _payload in self.events]


class TransactionDouble:
    """The part of a transaction an index needs: a number and a door to stage a record.

    Shaped exactly like the staging half of C5's transaction context, and deliberately not
    imported from it: a failure in this suite must be attributable to C9 rather than to a sibling
    mid-build. The staged records are kept so a test can assert what would reach the log.
    """

    _next_id = [0]

    def __init__(self, txn_id: int | None = None, epoch: int = 1) -> None:
        if txn_id is None:
            TransactionDouble._next_id[0] += 1
            txn_id = TransactionDouble._next_id[0]
        self._txn_id = txn_id
        self.epoch = epoch
        self.records: list[object] = []

    @property
    def txn_id(self) -> int:
        """Return the process-local number of this transaction."""
        return self._txn_id

    def stage_record(self, record: object) -> None:
        """Keep a log record this transaction would append at commit."""
        self.records.append(record)


@dataclass(frozen=True, slots=True)
class SnapshotDouble:
    """The visibility predicate of CONTRACT.md section 8.5, character for character."""

    read_lsn: int

    def visible(self, xmin: int, xmax: int) -> bool:
        """Return True when a version created at xmin and ended at xmax belongs to this view."""
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)


def seeded_vectors(count: int, dimension: int, seed: int) -> list[tuple[float, ...]]:
    """Return a reproducible corpus of vectors drawn from one seed.

    The components are drawn uniformly and then centred, so the corpus has no preferred
    direction and a nearest-neighbour answer is decided by the data rather than by a bias every
    vector shares.
    """
    generator = SplitMix64(seed)
    corpus: list[tuple[float, ...]] = []
    for _index in range(count):
        corpus.append(
            tuple(generator.next_float() * 2.0 - 1.0 for _position in range(dimension))
        )
    return corpus


def unit(values: Sequence[float]) -> tuple[float, ...]:
    """Return the vector scaled to unit length, for a space that declares normalized vectors."""
    return PureVectorMath().normalize(values)


class VectorFixture:
    """One database with a vector engine over it, and the shortcuts a test needs on top.

    The point of this class is that a test states what it wants -- a space, some rows, a search
    -- and never repeats the six lines of composition that stand between those and a real heap.
    """

    def __init__(
        self,
        *,
        metrics: object,
        clock: object,
        events: RecordingEvents | None = None,
        exact_scan_threshold: int = 4096,
        ef_search: int = 64,
        seed: int = 0x5EED,
        page_size: int = PAGE_SIZE,
        math: object | None = None,
        guard: object | None = None,
    ) -> None:
        self.device = MemoryStorageDevice(page_size=page_size)
        self.codec = PageCodecV1(page_size)
        self.metrics = metrics
        self.clock = clock
        self.events = events
        self.pool = BufferPool(
            self.device,
            self.codec,
            metrics,  # type: ignore[arg-type]
            budget_bytes=BUDGET_BYTES,
            db_label="vector_suite",
        )
        self.catalog_store = CatalogStore(self.pool)
        self.catalog_store.bootstrap()
        self.heap = HeapStore(self.pool, self.catalog_store)
        self.heap.bootstrap()
        self.registry = IndexManager(self.pool, self.heap, metrics)  # type: ignore[arg-type]
        self.math = PureVectorMath() if math is None else math
        self.engine = VectorEngine(
            catalog=self.catalog_store,
            heap=self.heap,
            pool=self.pool,
            indexes=self.registry,
            math=self.math,  # type: ignore[arg-type]
            metrics=metrics,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
            events=events,  # type: ignore[arg-type]
            exact_scan_threshold=exact_scan_threshold,
            seed=seed,
            ef_search=ef_search,
            guard=guard,  # type: ignore[arg-type]
        )
        self.table: TableDef | None = None

    # --- schema --------------------------------------------------------------------------

    def create_space(
        self,
        name: str,
        dimension: int,
        *,
        metric: DistanceMetric = DistanceMetric.COSINE,
        normalized: bool = False,
        storage_dtype: str = "float32",
    ) -> EmbeddingSpaceDef:
        """Create one embedding space through the engine and return its definition."""
        definition = EmbeddingSpaceDef(
            space_id=self.catalog_store.catalog.next_space_id(),
            name=name,
            dimension=dimension,
            metric=metric,
            normalized=normalized,
            storage_dtype=storage_dtype,
            state="active",
        )
        self.engine.create_space(definition)
        return self.catalog_store.catalog.space(name)

    def attach(self, table: TableDef, space_name: str) -> object:
        """Register the index of one space over one table, which is what makes it searchable."""
        # This fixture mutates its catalog synchronously and has no independent WAL publisher.
        # Passing that catalog lets attach certify the new empty index before attachment-time
        # metrics inspect every registered space (including this one).
        index = self.engine.attach(table, space_name, catalog=self.catalog_store.catalog)
        # The fixture has no independent WAL writer: every exposed row mutation updates the heap
        # and its vector index in one helper. Its far-future snapshots therefore name this
        # synthetic closed-world ceiling, not an unrepresented backlog of commits.
        self.registry.mark_built_through(FIXTURE_READ_LSN)
        return index

    def create_table(self, name: str, space_name: str) -> TableDef:
        """Create one node table whose third column stores vectors of a space."""
        table = TableDef(
            table_id=self.catalog_store.catalog.next_table_id(),
            name=name,
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="layer", type=ValueType.INT64),
                ColumnDef(
                    name="embedding",
                    type=self.catalog_store.catalog.space(space_name).value_type,
                    vector_space=space_name,
                ),
            ),
            primary_key="id",
        )
        self.catalog_store.catalog.add_table(table)
        self.catalog_store.save()
        self.table = table
        self.attach(table, space_name)
        return table

    # --- rows ----------------------------------------------------------------------------

    def insert_row(
        self,
        table: TableDef,
        record_id: int,
        layer: int,
        space: EmbeddingSpaceDef,
        values: Sequence[float],
        csn: Csn,
        *,
        apply_index: bool = True,
    ) -> RecordRef:
        """Write one row with a vector into the heap and, by default, into the index."""
        stored = self.engine.validate_vector(space, values)
        ref = self.heap.insert(
            table,
            record_id,
            (record_id, layer, VectorValue(stored, space.space_id, space.storage_dtype)),
            csn,
        )
        if apply_index:
            txn = TransactionDouble()
            self.engine.stage_insert(space.name, record_id, ref, stored, csn, txn)
            self.engine.commit(space.name, txn, csn)
        return ref

    def delete_row(
        self,
        table: TableDef,
        ref: RecordRef,
        record_id: int,
        space: EmbeddingSpaceDef,
        values: Sequence[float],
        csn: Csn,
        *,
        apply_index: bool = True,
    ) -> None:
        """End one row in the heap and, by default, tombstone its index entry."""
        self.heap.delete(table, ref, csn)
        if apply_index:
            txn = TransactionDouble()
            self.engine.stage_delete(space.name, record_id, ref, values, csn, txn)
            self.engine.commit(space.name, txn, csn)


@pytest.fixture
def metrics() -> RecordingMetrics:
    """Return a metrics sink that records every emission."""
    return RecordingMetrics()


@pytest.fixture
def clock() -> StepClock:
    """Return a clock whose readings advance by a fixed step."""
    return StepClock()


@pytest.fixture
def events() -> RecordingEvents:
    """Return an event sink that records every lifecycle notice."""
    return RecordingEvents()


@pytest.fixture
def database(
    metrics: RecordingMetrics, clock: StepClock, events: RecordingEvents
) -> VectorFixture:
    """Return a bootstrapped database with a vector engine over it."""
    return VectorFixture(metrics=metrics, clock=clock, events=events)


@pytest.fixture
def snapshot() -> SnapshotDouble:
    """Return a snapshot open far enough ahead to see every commit a test writes."""
    return SnapshotDouble(FIXTURE_READ_LSN)
