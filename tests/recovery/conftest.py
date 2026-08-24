"""Fixtures for the recovery, ledger, quarantine and verification suite (C6).

Everything here is deterministic. The clock answers what a test set, the metrics sink records what
it was told, and the stack is assembled from the real delivered components -- C1's buffer pool,
heap and catalog, C4's log -- rather than from doubles, because the properties under test are
properties of how those pieces behave together when the bytes underneath them are damaged.

``commit_pages`` drives the frozen commit protocol of CONTRACT.md section 8.5 steps 3.4 to 3.7
directly: append the page writes and the COMMIT with one batch, barrier, apply the images, publish
the commit state. It is deliberately not a call into C5's transaction manager -- the crash matrix
of AC-4 has to address each WRITE POINT of that sequence individually, and a driver this suite
owns is what lets a test say exactly which one it cut the process off at. What it must not do is
differ from the protocol, so the numbering, the ordering and the barrier placement here are the
contract's, step for step.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.ids import NO_LSN, PageIndex
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.domain.txn.commit_record import CommitPayload
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool, apply_page_image
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.verifier import Verifier
from okto_grafx.engine.wal_manager import WalManager

PAGE_SIZE: int = 512
"""Small page, so a whole file fits in a failure message."""

SEGMENT_BYTES: int = 4096
"""Roomy enough that a test rolls a segment only when it means to."""

DESCRIPTOR: str = "hash-v1;partitions_per_table=8"
"""The granularity descriptor of CONTRACT.md section 6.5."""

HEAP_FILE: str = "heap.dat"
CATALOG_FILE: str = "catalog.dat"


class FrozenClock:
    """A clock that answers exactly what the test set, so no assertion depends on real time."""

    def __init__(self, monotonic: float = 100.0, wall: float = 1_700_000_000.0) -> None:
        """Start at the two readings the test asked for."""
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        """Return the local reading used for liveness, which none of C6 consults."""
        return self._monotonic

    def wall(self) -> float:
        """Return the human-facing reading a ledger entry and a manifest are stamped with."""
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both readings forward by the same amount."""
        self._monotonic += seconds
        self._wall += seconds


class RecordingMetricsSink:
    """A metrics sink that keeps what it was given, so a test can assert on the series."""

    def __init__(self, *, enabled: bool = True) -> None:
        """Start empty, either collecting or refusing to collect."""
        self._enabled = enabled
        self.registered: list[MetricDescriptor] = []
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.observations: list[tuple[str, float, tuple[tuple[str, str], ...]]] = []

    @property
    def enabled(self) -> bool:
        """Return whether this sink collects anything at all."""
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        """Remember a declared metric."""
        self.registered.append(descriptor)

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a counter series."""
        key = (name, _key(labels))
        self.counters[key] = self.counters.get(key, 0.0) + value

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Set a gauge series."""
        self.gauges[(name, _key(labels))] = value

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record one histogram observation."""
        self.observations.append((name, value, _key(labels)))

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> AbstractContextManager[None]:
        """Return a context manager that records that the block was timed."""
        return self._timed()

    @contextmanager
    def _timed(self) -> Iterator[None]:
        """Record the timing of one block without reading a clock."""
        yield

    def snapshot(self) -> Mapping[str, object]:
        """Return the current values, which is what a gate would read."""
        return {"counters": dict(self.counters), "gauges": dict(self.gauges)}

    def counter(self, name: str, **labels: str) -> float:
        """Return one counter series, or zero when it has never fired."""
        return self.counters.get((name, _key(labels or None)), 0.0)

    def gauge(self, name: str, **labels: str) -> float | None:
        """Return one gauge series, or None when it has never been set."""
        return self.gauges.get((name, _key(labels or None)))


def _key(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Return a hashable, order-independent form of a label mapping."""
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


class RefusingProbe:
    """A control-record probe that always says the record is damaged."""

    def __init__(self, failure: BaseException) -> None:
        """Keep the failure every read of a control record will raise."""
        self._failure = failure
        self.reads: list[str] = []

    def read_control_record(self, file: str) -> None:
        """Record the request and raise the failure this probe was built with."""
        self.reads.append(file)
        raise self._failure


class HealthyProbe:
    """A control-record probe that says every record it is shown reads cleanly."""

    def __init__(self) -> None:
        """Start with nothing read."""
        self.reads: list[str] = []

    def read_control_record(self, file: str) -> None:
        """Record the request and return, which is how a probe reports a healthy record."""
        self.reads.append(file)


class StackCoordinator:
    """The minimal COMPLETE coordinator a single-process stack needs (M0C fencing).

    Recovery refuses to scan or mutate without one, so the stack wires this deterministic
    double by default: ``exclusive`` records section entries and exits, ``reader_horizon``
    answers what a test set. Tests about the fence itself override or remove it.
    """

    def __init__(self) -> None:
        self.sections: list[tuple[str, str, float]] = []
        self.active: int = 0
        self.horizon: int | None = None

    def owner_id(self) -> str:
        """Name the recovery owner the commit-state store records."""
        return "stack"

    def reader_horizon(self) -> int | None:
        """Answer the horizon a test seeded; None means no live reader holds the log."""
        return self.horizon

    @contextmanager
    def exclusive(self, name: str, *, timeout: float) -> Iterator[None]:
        """Record one section acquisition and hold it for the body."""
        self.sections.append(("enter", name, timeout))
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self.sections.append(("exit", name, timeout))


@dataclass
class Stack:
    """One assembled database: its device, its stores, and the C6 components over them."""

    storage: object
    codec: PageCodecV1
    clock: FrozenClock
    metrics: RecordingMetricsSink
    pool: BufferPool
    catalog: CatalogStore
    heap: HeapStore
    wal: WalManager
    ledger: LedgerStore
    quarantine: QuarantineStore

    def recovery(self, **overrides: object) -> RecoveryManager:
        """Return a recovery manager over this stack, with anything the test wants changed."""
        settings: dict[str, object] = {
            "catalog": self.catalog,
            "coordinator": StackCoordinator(),
        }
        settings.update(overrides)
        return RecoveryManager(
            self.storage,  # type: ignore[arg-type]
            self.wal,
            self.ledger,
            self.quarantine,
            self.pool,
            self.metrics,  # type: ignore[arg-type]
            **settings,  # type: ignore[arg-type]
        )

    def verifier(self, **overrides: object) -> Verifier:
        """Return a verifier over this stack, with anything the test wants changed."""
        settings: dict[str, object] = {"heap": self.heap, "catalog": self.catalog}
        settings.update(overrides)
        return Verifier(self.pool, self.metrics, **settings)  # type: ignore[arg-type]


def build_stack(
    device: object,
    *,
    clock: FrozenClock | None = None,
    metrics: RecordingMetricsSink | None = None,
    bootstrap: bool = True,
    budget_bytes: int = 512 * 1024,
) -> Stack:
    """Assemble a database over a device: pool, catalog, heap, log, ledger and quarantine."""
    the_clock = clock if clock is not None else FrozenClock()
    the_metrics = metrics if metrics is not None else RecordingMetricsSink()
    codec = PageCodecV1(page_size=device.page_size)  # type: ignore[attr-defined]
    pool = BufferPool(
        device,  # type: ignore[arg-type]
        codec,
        the_metrics,  # type: ignore[arg-type]
        budget_bytes=budget_bytes,
        db_label="recovery",
    )
    catalog = CatalogStore(pool)
    heap = HeapStore(pool, catalog)
    if bootstrap:
        catalog.bootstrap()
        heap.bootstrap()
    wal = WalManager(
        device,  # type: ignore[arg-type]
        the_clock,  # type: ignore[arg-type]
        the_metrics,  # type: ignore[arg-type]
        segment_bytes=SEGMENT_BYTES,
        descriptor=DESCRIPTOR,
    )
    wal.open()
    quarantine = QuarantineStore(device, the_clock, the_metrics)  # type: ignore[arg-type]
    ledger = LedgerStore(
        device,
        the_clock,
        the_metrics,
        quarantine=quarantine,  # type: ignore[arg-type]
    )
    return Stack(
        storage=device,
        codec=codec,
        clock=the_clock,
        metrics=the_metrics,
        pool=pool,
        catalog=catalog,
        heap=heap,
        wal=wal,
        ledger=ledger,
        quarantine=quarantine,
    )


def make_page_image(
    codec: PageCodecV1,
    payloads: Sequence[bytes],
    *,
    page_index: PageIndex = 1,
    page_type: int = int(PageType.HEAP),
    page_lsn: int = NO_LSN,
) -> bytes:
    """Return a valid page image holding these payloads, one per slot."""
    page = Page(page_type, page_size=codec.page_size, page_index=page_index)
    page.page_lsn = page_lsn
    for payload in payloads:
        page.insert_slot(payload)
    return codec.encode_page(page)


def commit_pages(
    stack: Stack,
    pages: Sequence[tuple[str, PageIndex, bytes]],
    *,
    txn_id: int = 1,
    epoch: int = 1,
    partitions: Sequence[int] = (7,),
) -> int:
    """Run the frozen commit protocol over these page images and return the commit number.

    CONTRACT.md section 8.5 steps 3.4 to 3.7, in order: one batch of WRITE_PAGE records followed
    by the COMMIT, then the barrier -- and no acknowledgement before it returns (BR-4) -- then the
    page images, then the published commit state. Each of those is a write point the fault bench
    can address, which is what the AC-4 matrix walks.
    """
    predicted = stack.wal.last_lsn + len(pages) + 1
    records: list[WalRecord] = []
    stamped: list[tuple[str, PageIndex, bytes]] = []
    for file, page_index, image in pages:
        page = stack.codec.decode_page(image, verify=True)
        if page.page_lsn < predicted:
            page.page_lsn = predicted
        body = stack.codec.encode_page(page)
        stamped.append((file, page_index, body))
        records.append(
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                epoch=epoch,
                txn_id=txn_id,
                payload=encode_page_write(file, page_index, body),
                descriptor=DESCRIPTOR,
            )
        )
    payload = CommitPayload.build(
        snapshot_lsn=stack.wal.last_lsn, read_partitions=(), write_partitions=partitions
    )
    records.append(
        WalRecord(
            record_type=int(WalRecordType.COMMIT),
            epoch=epoch,
            txn_id=txn_id,
            payload=payload.encode(),
            descriptor=DESCRIPTOR,
        )
    )
    lsn = stack.wal.append_many(records)
    stack.wal.barrier()
    for file, page_index, body in stamped:
        apply_page_image(stack.pool, file, page_index, body)
    stack.pool.flush()
    _publish_commit_state(stack, lsn)
    return lsn


def _publish_commit_state(stack: Stack, lsn: int) -> None:
    """Publish the commit state of section 6.1 the way step 3.7 does: by atomic replace."""
    staging = f"{COMMIT_STATE_FILE}.staging"
    device = stack.storage
    if device.exists(staging):  # type: ignore[attr-defined]
        device.remove(staging)  # type: ignore[attr-defined]
    device.create(staging, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(  # type: ignore[attr-defined]
        staging, CommitState(last_committed_lsn=lsn, last_csn=lsn).encode()
    )
    device.durable_barrier(staging)  # type: ignore[attr-defined]
    device.atomic_replace(staging, COMMIT_STATE_FILE)  # type: ignore[attr-defined]


def digest_of_file(device: object, file: str) -> bytes:
    """Return every byte of a paged file, so a test can prove it was not touched (AC-5)."""
    pages = device.page_count(file)  # type: ignore[attr-defined]
    return b"".join(device.read_page(file, index) for index in range(pages))  # type: ignore[attr-defined]


@pytest.fixture
def clock() -> FrozenClock:
    """Return a clock whose readings the test controls."""
    return FrozenClock()


@pytest.fixture
def metrics() -> RecordingMetricsSink:
    """Return a sink that records every emission."""
    return RecordingMetricsSink()


@pytest.fixture
def memory_device() -> Iterator[MemoryStorageDevice]:
    """Yield a device over byte buffers."""
    device = MemoryStorageDevice(page_size=PAGE_SIZE)
    try:
        yield device
    finally:
        device.close()


@pytest.fixture
def local_device(tmp_path: Path) -> Iterator[LocalStorageDevice]:
    """Yield a device over a real directory."""
    device = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        yield device
    finally:
        device.close()


@pytest.fixture
def fault_device(memory_device: MemoryStorageDevice) -> FaultInjectingStorageDevice:
    """Yield the deterministic fault-injecting twin of FR-16 over a memory device."""
    return FaultInjectingStorageDevice(memory_device, seed=20260820)


@pytest.fixture
def stack(
    memory_device: MemoryStorageDevice,
    clock: FrozenClock,
    metrics: RecordingMetricsSink,
) -> Stack:
    """Return a bootstrapped database over the memory device."""
    return build_stack(memory_device, clock=clock, metrics=metrics)


@pytest.fixture
def local_stack(
    local_device: LocalStorageDevice, clock: FrozenClock, metrics: RecordingMetricsSink
) -> Stack:
    """Return a bootstrapped database over a real directory."""
    return build_stack(local_device, clock=clock, metrics=metrics)


@pytest.fixture
def make_stack(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> Callable[..., Stack]:
    """Return a factory that builds another stack over any device the test supplies."""

    def build(device: object, **overrides: object) -> Stack:
        overrides.setdefault("clock", clock)
        overrides.setdefault("metrics", metrics)
        return build_stack(device, **overrides)  # type: ignore[arg-type]

    return build
