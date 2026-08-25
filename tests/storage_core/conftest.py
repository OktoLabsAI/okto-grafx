"""Fixtures for the storage core (C1).

These doubles are deliberately owned by this directory rather than shared with the foundation
suite: the storage core is the component under test here, and a test that fails must fail
because of C1 and not because another component reshaped a fake.

``MemoryDevice`` is a StorageDevice with the exact semantics of the port: pages are allocated
before they are written, a log only grows at the end, and nothing is ever written at an
arbitrary offset. It also carries two deterministic hooks, ``page_reader`` and ``read_calls``,
which is what lets a test reproduce a torn read without a thread and without a clock.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore

SMALL_PAGE_SIZE: int = 512
"""A small page keeps the fixtures fast and makes overflow and page growth easy to reach."""


class MemoryDevice:
    """An in-memory StorageDevice with the append-only and paged semantics of the port."""

    def __init__(
        self,
        page_size: int = SMALL_PAGE_SIZE,
        *,
        name: str = "memory",
        trace: list[str] | None = None,
    ) -> None:
        self._page_size = page_size
        self._name = name
        self.trace: list[str] = [] if trace is None else trace
        self._pages: dict[str, list[bytes]] = {}
        self._logs: dict[str, bytearray] = {}
        self.read_calls: list[tuple[str, PageIndex]] = []
        self.write_calls: list[tuple[str, PageIndex]] = []
        self.barriers: list[str | None] = []
        self.page_reader: Callable[[str, PageIndex, bytes], bytes] | None = None
        self.writes_attempted: int = 0
        self._refuse_at_write: int | None = None
        self._refusal: GrafxError | None = None
        self.refused_writes: list[tuple[str, PageIndex]] = []

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
        raw = pages[page_index]
        if self.page_reader is not None:
            raw = self.page_reader(file, page_index, raw)
        return raw

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages) or len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Refused a page write to {file!r} at {page_index}.", file=file, page=page_index
            )
        # The one refusal route the whole C1 suite never had. LocalStorageDevice.write_page
        # answers GrafxDeviceFull for a partial write or a full volume and GrafxStorageError for
        # a sharing violation, and BOTH are retryable -- so this is where a caller is told to try
        # again. Every door that writes reaches the device through here, and until this hook
        # existed no test could put a retryable failure in front of one (D2, round 8).
        self.writes_attempted += 1
        if self.writes_attempted == self._refuse_at_write and self._refusal is not None:
            self.refused_writes.append((file, page_index))
            self._refuse_at_write = None
            raise self._refusal
        self.write_calls.append((file, page_index))
        pages[page_index] = bytes(data)

    def refuse_write_number(self, number: int, error: GrafxError) -> None:
        """Arm one refusal for the Nth write-back this device is asked to perform.

        Counting writes rather than naming a page is what makes the sweep possible: which page a
        door writes back is an eviction decision, and a test that named one would prove the
        property at whichever step that page happened to fall. Numbering lets a test walk the
        refusal through EVERY step of an operation and assert the same invariant at each, which
        is the only way to state "nothing becomes reachable before the last step that can
        refuse" as a test rather than as a claim about one step.
        """
        self.writes_attempted = 0
        self._refuse_at_write = number
        self._refusal = error

    def disarm(self) -> None:
        """Stop refusing, which is what a caller retrying after a transient failure meets."""
        self._refuse_at_write = None
        self._refusal = None

    def raw_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return the stored bytes of a page without going through the read hooks."""
        return self._pages[file][page_index]

    def poke_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Replace the stored bytes of a page, which is how a test corrupts one."""
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
        self.trace.append("barrier")
        self.barriers.append(file)


class RecordingMetrics:
    """A MetricsSink that keeps every registration and every emission with its labels."""

    def __init__(
        self, *, enabled: bool = True, trace: list[str] | None = None
    ) -> None:
        self._enabled = enabled
        self.registered: dict[str, MetricDescriptor] = {}
        self.calls: list[tuple[str, str, float, dict[str, str]]] = []
        self.trace: list[str] = [] if trace is None else trace

    @property
    def enabled(self) -> bool:
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        self.registered[descriptor.name] = descriptor

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self.calls.append(("increment", name, value, dict(labels or {})))

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        self.calls.append(("set_gauge", name, value, dict(labels or {})))

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        self.calls.append(("observe", name, value, dict(labels or {})))

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> contextlib.AbstractContextManager[None]:
        return self._timed(name, dict(labels or {}))

    @contextlib.contextmanager
    def _timed(self, name: str, labels: dict[str, str]) -> Iterator[None]:
        # The exit is recorded whatever happens inside. A real sink that only observed the happy
        # path would never time the operation a caller most wants timed, the one that failed.
        self.trace.append(f"time_enter:{name}")
        self.calls.append(("time_enter", name, 0.0, labels))
        try:
            yield
        finally:
            self.trace.append(f"time_exit:{name}")
            self.calls.append(("time_exit", name, 0.0, labels))

    def snapshot(self) -> Mapping[str, object]:
        return {name: descriptor.kind.value for name, descriptor in self.registered.items()}

    def values_of(self, name: str) -> list[float]:
        """Return every value emitted under that metric name, in order."""
        return [value for _kind, emitted, value, _labels in self.calls if emitted == name]

    def labels_of(self, name: str) -> list[dict[str, str]]:
        """Return the labels of every emission of that metric name, in order."""
        return [labels for _kind, emitted, _value, labels in self.calls if emitted == name]

    def timed(self, name: str) -> list[dict[str, str]]:
        """Return the labels of every completed timed block of that metric name, in order."""
        return [
            labels
            for kind, emitted, _value, labels in self.calls
            if emitted == name and kind == "time_exit"
        ]


class SnapshotDouble:
    """The visibility predicate of CONTRACT.md section 8.5, which C5 will own for real."""

    def __init__(self, read_lsn: int) -> None:
        self.read_lsn = read_lsn

    def visible(self, xmin: int, xmax: int) -> bool:
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)


def make_pool(
    device: MemoryDevice,
    metrics: RecordingMetrics,
    *,
    budget_pages: int = 8,
    db_label: str = "testdb",
    page_sequence_fence: Callable[[str, PageIndex], bool] | None = None,
) -> BufferPool:
    """Build a pool over the device with a budget expressed in whole pages."""
    return BufferPool(
        device,
        PageCodecV1(device.page_size),
        metrics,
        budget_bytes=device.page_size * budget_pages,
        db_label=db_label,
        page_sequence_fence=page_sequence_fence,
    )


@pytest.fixture
def trace() -> list[str]:
    """Return the ordered trace the device and the metrics sink of one test both write to.

    Timing a block is a claim about what happened INSIDE it. A test that only sees that a block
    was entered and left cannot tell a barrier that was timed from one that was moved out of the
    timer, so both parties append to one list and the test reads the order.
    """
    return []


@pytest.fixture
def device(trace: list[str]) -> MemoryDevice:
    """Return a fresh in-memory device with small pages, writing into the shared trace."""
    return MemoryDevice(trace=trace)


@pytest.fixture
def metrics(trace: list[str]) -> RecordingMetrics:
    """Return a metrics sink that records everything, writing into the shared trace."""
    return RecordingMetrics(trace=trace)


@pytest.fixture
def codec() -> PageCodecV1:
    """Return the version 1 codec bound to the fixture page size."""
    return PageCodecV1(SMALL_PAGE_SIZE)


@pytest.fixture
def pool(device: MemoryDevice, metrics: RecordingMetrics) -> BufferPool:
    """Return a buffer pool over the fixture device with room for eight pages."""
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
def snapshots() -> Callable[[int], SnapshotDouble]:
    """Return a factory of snapshots at a given read position."""

    def build(read_lsn: int) -> SnapshotDouble:
        return SnapshotDouble(read_lsn)

    return build


def iter_pages(device: MemoryDevice, file: str) -> Iterator[bytes]:
    """Yield the stored image of every page of a file, in order."""
    for index in range(device.page_count(file)):
        yield device.raw_page(file, index)
