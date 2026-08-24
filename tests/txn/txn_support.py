"""Doubles and builders for the transaction-manager suite (C5).

Two of them are worth explaining.

``LogWal`` stands in for C4. It implements the part of CONTRACT.md section 8.3 that C5 calls --
``last_lsn``, commit-LSN planning, append/barrier and ``read_from`` -- over a real
``StorageDevice``, so
the same double serves an in-process test over the memory device and a two-process test over a
real directory. Its framing is its own; the WAL record format belongs to C4 and nothing here
depends on it.

``build_stack`` assembles the real C1 buffer pool, the real C1 catalog and heap stores and the
real C3 coordinator around it, because the properties this suite is about -- a snapshot never
seeing a partial commit, a page never being lost between the barrier and the apply -- are
properties of those pieces working together and cannot be observed against a mock.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_LSN, Lsn, PageIndex
from okto_grafx.domain.page import Page, PageType, crc32c
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.txn.records import WalRecordLike
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.txn_manager import TransactionManager
from shared_device import SharedDirectoryDevice

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "WAL_FILE",
    "LogRecord",
    "LogWal",
    "ManualClock",
    "RecordingMetricsSink",
    "SharedDirectoryDevice",
    "Stack",
    "TracingCoordinator",
    "build_stack",
    "decode_or_none",
    "make_page_image",
    "page_lsn_of",
    "read_page_payloads",
]

DEFAULT_PAGE_SIZE: int = 512
"""Small pages keep the fixtures cheap; nothing in this component depends on the size."""

WAL_FILE: str = "wal/000000000001.wal"
"""The single segment this stand-in log writes. Segmentation belongs to C4."""

_HEADER: struct.Struct = struct.Struct("<IHHIQQQII")
_MAGIC: int = 0x5852474F
_CHECKSUM: struct.Struct = struct.Struct("<I")


class ManualClock:
    """A clock whose readings only move when a test moves them."""

    def __init__(self, monotonic: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(monotonic)
        self._wall = float(wall)

    def monotonic(self) -> float:
        """Return the current monotonic reading."""
        return self._monotonic

    def wall(self) -> float:
        """Return the current wall reading."""
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both readings forward by the same amount."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)


class RecordingMetricsSink:
    """A metrics sink that keeps every registration and every emission for assertions."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self.registered: list[MetricDescriptor] = []
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []
        self.observations: list[tuple[str, float, dict[str, str]]] = []

    @property
    def enabled(self) -> bool:
        """Return whether this sink collects anything."""
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        """Record a registration."""
        self.registered.append(descriptor)

    def increment(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record a counter increment."""
        self.counters.append((name, float(value), dict(labels or {})))

    def set_gauge(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record a gauge write."""
        self.gauges.append((name, float(value), dict(labels or {})))

    def observe(
        self, name: str, value: float, labels: Mapping[str, str] | None = None
    ) -> None:
        """Record a histogram observation."""
        self.observations.append((name, float(value), dict(labels or {})))

    def time(self, name: str, labels: Mapping[str, str] | None = None):  # noqa: ANN201
        """Return a context manager that records nothing but the name."""
        import contextlib

        @contextlib.contextmanager
        def _timed() -> Iterator[None]:
            yield
            self.observations.append((name, 0.0, dict(labels or {})))

        return _timed()

    def snapshot(self) -> Mapping[str, object]:
        """Return the totals a gate would read."""
        totals: dict[str, float] = {}
        for name, value, _labels in self.counters:
            totals[name] = totals.get(name, 0.0) + value
        return dict(totals)

    def total(self, name: str) -> float:
        """Return the accumulated value of one counter."""
        return sum(value for emitted, value, _labels in self.counters if emitted == name)

    def gauge_values(self, name: str, label: str, value: str) -> list[float]:
        """Return every value written to one gauge series."""
        return [
            reading
            for emitted, reading, labels in self.gauges
            if emitted == name and labels.get(label) == value
        ]


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One record as the stand-in log stores and returns it."""

    record_type: int
    lsn: Lsn
    epoch: int
    txn_id: int
    payload: bytes
    descriptor: str = ""
    flags: int = 0
    format_version: int = 1


class LogWal:
    """The part of a WalManager that C5 calls, over a real storage device.

    Records are appended one call at a time on purpose: a crash between two of them is a state a
    real log can be left in, and a bench that appended a whole commit atomically could not
    produce it.
    """

    def __init__(self, storage: StorageDevice, *, file: str = WAL_FILE) -> None:
        self._storage = storage
        self._file = file
        self._last_lsn: Lsn = NO_LSN
        self._scanned: int = 0
        self.appended: list[LogRecord] = []
        self.barriers: int = 0
        if not storage.exists(file):
            storage.create(file)
        self._refresh()

    @property
    def file(self) -> str:
        """Return the segment this log writes."""
        return self._file

    @property
    def last_lsn(self) -> Lsn:
        """Return the highest sequence number the log has assigned, rescanning if it grew."""
        self._refresh()
        return self._last_lsn

    def append(self, record: WalRecordLike) -> Lsn:
        """Append one record and return the sequence number it was given."""
        return self.append_many([record])

    def planned_terminal_lsn(self, records: Sequence[WalRecordLike]) -> Lsn:
        """Return the terminal LSN; this deliberately non-segmented double never rolls."""
        self._refresh()
        return self._last_lsn + len(records)

    def append_many(
        self,
        records: Sequence[WalRecordLike],
        *,
        expected_terminal_lsn: Lsn | None = None,
    ) -> Lsn:
        """Append several records in order and return the sequence number of the last one."""
        self._refresh()
        planned = self._last_lsn + len(records)
        if expected_terminal_lsn is not None and planned != expected_terminal_lsn:
            raise GrafxCorruptionDetected(
                "The test WAL tail changed after commit planning.",
                reason="planned_terminal_lsn_drift",
                expected_terminal_lsn=expected_terminal_lsn,
                planned_terminal_lsn=planned,
            )
        assigned = self._last_lsn
        for record in records:
            assigned += 1
            stored = LogRecord(
                record_type=int(record.record_type),
                lsn=assigned,
                epoch=int(getattr(record, "epoch", 0)),
                txn_id=int(getattr(record, "txn_id", 0)),
                payload=bytes(record.payload),
                descriptor=str(getattr(record, "descriptor", "")),
                flags=int(getattr(record, "flags", 0)),
                format_version=int(getattr(record, "format_version", 1)),
            )
            self._storage.append_log(self._file, _encode(stored))
            self.appended.append(stored)
        self._refresh()
        return assigned

    def barrier(self) -> None:
        """Make everything appended so far durable."""
        self._storage.durable_barrier(self._file)
        self.barriers += 1

    def force_barrier_range(self, first_lsn: Lsn, through_lsn: Lsn) -> tuple[str, ...]:
        """Force the stand-in's sole file independently of ordinary barrier bookkeeping."""
        del first_lsn, through_lsn
        self.barrier()
        return (self._file,)

    def read_from(self, lsn: Lsn) -> Iterator[LogRecord]:
        """Yield every record whose sequence number is at or above the given one."""
        for record in self._scan():
            if record.lsn >= lsn:
                yield record

    def records(self) -> tuple[LogRecord, ...]:
        """Return everything currently in the segment, in order."""
        return tuple(self._scan())

    def total_bytes(self) -> int:
        """Return how many bytes the segment holds."""
        return self._storage.log_size(self._file)

    def _refresh(self) -> None:
        """Bring the cached tail position up to date with the file."""
        size = self._storage.log_size(self._file)
        if size == self._scanned:
            return
        last = NO_LSN
        for record in self._scan():
            last = record.lsn
        self._last_lsn = last
        self._scanned = size

    def _scan(self) -> Iterator[LogRecord]:
        """Walk the segment from the start, stopping at the first record that is not intact."""
        size = self._storage.log_size(self._file)
        offset = 0
        while offset + _HEADER.size <= size:
            head = self._storage.read_log(self._file, offset, _HEADER.size)
            magic, version, record_type, total, lsn, epoch, txn_id, dlen, plen = (
                _HEADER.unpack(head)
            )
            if magic != _MAGIC or total < _HEADER.size or offset + total > size:
                return
            raw = self._storage.read_log(self._file, offset, total)
            (stored,) = _CHECKSUM.unpack_from(raw, total - _CHECKSUM.size)
            if stored != crc32c(raw[: total - _CHECKSUM.size]):
                return
            body = _HEADER.size
            descriptor = raw[body : body + dlen].decode("utf-8")
            payload = raw[body + dlen : body + dlen + plen]
            yield LogRecord(
                record_type=record_type,
                lsn=lsn,
                epoch=epoch,
                txn_id=txn_id,
                payload=payload,
                descriptor=descriptor,
                flags=0,
                format_version=version,
            )
            offset += total


def _encode(record: LogRecord) -> bytes:
    """Return the bytes of one stored record, checksum included."""
    descriptor = record.descriptor.encode("utf-8")
    total = _HEADER.size + len(descriptor) + len(record.payload) + _CHECKSUM.size
    head = _HEADER.pack(
        _MAGIC,
        record.format_version,
        record.record_type,
        total,
        record.lsn,
        record.epoch,
        record.txn_id,
        len(descriptor),
        len(record.payload),
    )
    body = head + descriptor + record.payload
    return body + _CHECKSUM.pack(crc32c(body))


class TracingCoordinator:
    """A coordinator that delegates everything and writes down what it was asked, in order.

    It also lets a test run a callback exactly when the commit section is entered, which is the
    only place from which the window between step 2 and step 3.1 of the commit protocol can be
    reached from outside.
    """

    def __init__(
        self,
        inner: object,
        trail: list[str],
        *,
        on_section: object = None,
        on_register: object = None,
        on_section_named: str = "commit",
    ) -> None:
        self._inner = inner
        self._trail = trail
        self._on_section = on_section
        self._on_register = on_register
        # The manager takes more than one section, so a hook that fired on every one of them
        # would run at an instant the test did not mean. It names the section it is about.
        self._on_section_named = on_section_named

    @property
    def inner(self) -> object:
        """Return the coordinator this one delegates to."""
        return self._inner

    def owner_id(self) -> str:
        return self._inner.owner_id()

    def current_epoch(self) -> int:
        return self._inner.current_epoch()

    def acquire_writer_lease(self, *, timeout: float):  # noqa: ANN201
        self._trail.append("acquire_writer_lease")
        return self._inner.acquire_writer_lease(timeout=timeout)

    def renew_lease(self, lease):  # noqa: ANN001, ANN201
        self._trail.append("renew_lease")
        return self._inner.renew_lease(lease)

    def release_lease(self, lease) -> None:  # noqa: ANN001
        self._trail.append("release_lease")
        self._inner.release_lease(lease)

    def validate_epoch(self, epoch: int) -> None:
        self._trail.append("validate_epoch")
        self._inner.validate_epoch(epoch)

    def detect_dead_owner(self, *, stall_threshold: float):  # noqa: ANN201
        return self._inner.detect_dead_owner(stall_threshold=stall_threshold)

    def takeover(self):  # noqa: ANN201
        self._trail.append("takeover")
        return self._inner.takeover()

    def register_reader(self, snapshot_lsn: int):  # noqa: ANN201
        self._trail.append("register_reader")
        if self._on_register is not None:
            self._on_register(snapshot_lsn)
        return self._inner.register_reader(snapshot_lsn)

    def refresh_reader(self, handle) -> None:  # noqa: ANN001
        self._trail.append("refresh_reader")
        self._inner.refresh_reader(handle)

    def unregister_reader(self, handle) -> None:  # noqa: ANN001
        self._trail.append("unregister_reader")
        self._inner.unregister_reader(handle)

    def reader_horizon(self):  # noqa: ANN201
        self._trail.append("reader_horizon")
        return self._inner.reader_horizon()

    def exclusive(self, name: str, *, timeout: float):  # noqa: ANN201
        import contextlib

        @contextlib.contextmanager
        def _section() -> Iterator[None]:
            with self._inner.exclusive(name, timeout=timeout):
                self._trail.append(f"enter:{name}")
                if self._on_section is not None and name == self._on_section_named:
                    self._on_section()
                try:
                    yield
                finally:
                    self._trail.append(f"leave:{name}")

        return _section()


@dataclass(slots=True)
class Stack:
    """Everything one database needs, built and wired."""

    root: Path | None
    storage: StorageDevice
    codec: PageCodecV1
    pool: BufferPool
    catalog: CatalogStore
    heap: HeapStore
    coordinator: LocalProcessCoordinator
    clock: ManualClock
    metrics: RecordingMetricsSink
    wal: LogWal
    manager: TransactionManager


def build_stack(
    root: Path | None = None,
    *,
    storage: StorageDevice | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    partitions_per_table: int = 8,
    commit_lock_timeout: float = 5.0,
    lease_timeout: float | None = None,
    reader_stall_threshold: float | None = None,
    owner_id: str = "participant-a",
    clock: ManualClock | None = None,
    metrics: RecordingMetricsSink | None = None,
    budget_bytes: int = 512 * 1024,
    db_label: str = "txn",
    wal_factory: object = None,
    retain_lease: bool = False,
) -> Stack:
    """Assemble a working database around a transaction manager."""
    device = storage if storage is not None else _device(root, page_size)
    the_clock = clock if clock is not None else ManualClock()
    the_metrics = metrics if metrics is not None else RecordingMetricsSink()
    codec = PageCodecV1(page_size=device.page_size)
    pool = BufferPool(device, codec, the_metrics, budget_bytes=budget_bytes, db_label=db_label)
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    coordinator = LocalProcessCoordinator(
        device,
        the_clock,
        metrics=the_metrics,
        owner_id=owner_id,
        ttl_seconds=5.0,
        owner_stall_threshold=5.0,
        reader_stall_threshold=15.0,
        section_timeout=5.0,
        lock_directory=None if root is None else str(root / "control"),
    )
    wal = LogWal(device) if wal_factory is None else wal_factory(device, the_clock, the_metrics)
    manager = TransactionManager(
        wal,
        pool,
        heap,
        catalog,
        coordinator,
        the_clock,
        the_metrics,
        None,
        partitions_per_table=partitions_per_table,
        commit_lock_timeout=commit_lock_timeout,
        lease_timeout=lease_timeout,
        reader_stall_threshold=reader_stall_threshold,
        descriptor=f"hash-v1;partitions_per_table={partitions_per_table}",
        retain_lease=retain_lease,
    )
    return Stack(
        root=root,
        storage=device,
        codec=codec,
        pool=pool,
        catalog=catalog,
        heap=heap,
        coordinator=coordinator,
        clock=the_clock,
        metrics=the_metrics,
        wal=wal,
        manager=manager,
    )


def _device(root: Path | None, page_size: int) -> StorageDevice:
    """Return a directory device, or a memory device when no directory is given.

    The directory device is the handle-free one from ``shared_device``. See that module for the
    measured Windows behaviour that makes ``LocalStorageDevice`` unusable for two participants
    over one directory; it is a C2 finding and not a property of this component.
    """
    if root is None:
        return MemoryStorageDevice(page_size=page_size)
    return SharedDirectoryDevice(root, page_size=page_size)


def make_page_image(
    codec: PageCodecV1,
    payloads: Sequence[bytes],
    *,
    page_index: PageIndex = 1,
    page_type: int = int(PageType.HEAP),
    page_lsn: int = NO_LSN,
) -> bytes:
    """Return a valid page image holding these payloads, one per slot."""
    page = Page(
        page_type,
        page_size=codec.page_size,
        page_index=page_index,
        page_lsn=page_lsn,
        seq=2,
    )
    for payload in payloads:
        page.insert_slot(payload)
    return codec.encode_page(page)


def _fresh_pool(pool: BufferPool) -> BufferPool:
    """Return a pool over the same device that has never seen a page.

    Reading through the pool that wrote is not evidence that anything reached the device: an
    invalidate writes dirty pages before it drops them, so a missing flush would be repaired by
    the very call meant to expose it. A second process has no such pool, and neither does this.
    """
    return BufferPool(
        pool.storage,
        pool.codec,
        _SilentMetrics(),
        budget_bytes=pool.budget_bytes,
        db_label="probe",
    )


class _SilentMetrics:
    """A sink that collects nothing, so a probe never disturbs a metric a test is reading."""

    @property
    def enabled(self) -> bool:
        return False

    def register(self, descriptor: MetricDescriptor) -> None:
        return None

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        return None

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        return None

    def observe(self, name: str, value: float, labels: object = None) -> None:
        return None

    def time(self, name: str, labels: object = None):  # noqa: ANN201
        import contextlib

        return contextlib.nullcontext()

    def snapshot(self) -> Mapping[str, object]:
        return {}


def read_page_payloads(
    pool: BufferPool, file: str, page_index: PageIndex
) -> tuple[bytes, ...]:
    """Return the live slot payloads of one page as the DEVICE holds it."""
    with _fresh_pool(pool).pinned(file, page_index) as page:
        return tuple(payload for _slot, payload in page.iter_slots())


def page_lsn_of(pool: BufferPool, file: str, page_index: PageIndex) -> int:
    """Return the page LSN the DEVICE currently holds for one page."""
    with _fresh_pool(pool).pinned(file, page_index) as page:
        return page.page_lsn


def decode_or_none(image: bytes, codec: PageCodecV1) -> Page | None:
    """Return the decoded page, or None when the bytes are not a page at all."""
    try:
        return codec.decode_page(image, verify=True)
    except GrafxCorruptionDetected:
        return None
