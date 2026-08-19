"""Shared fixtures for the Okto Grafx suite.

The fakes below exist to prove that the ports are satisfiable by an ordinary object and to give
the registry something real to bind. They are deliberately minimal: the production adapters
arrive with C1, C2, C3, C8 and C9, and they will be tested against their own behaviour, not
against these stand-ins.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.ids import Epoch, Lsn, PageIndex
from okto_grafx.domain.ports import (
    DeadOwnerReport,
    DistanceMetric,
    Lease,
    MetricDescriptor,
    ReaderHandle,
)
from okto_grafx.runtime.registry import PortRegistry

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
"""Repository root, used by the packaging and boundary gates."""

SOURCE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
"""Root of the installed package tree."""


class FakeStorageDevice:
    """In-memory stand-in for StorageDevice with the append-only and paged semantics of the port."""

    def __init__(self, page_size: int = 8192) -> None:
        self._page_size = page_size
        self._pages: dict[str, list[bytes]] = {}
        self._logs: dict[str, bytearray] = {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def page_size(self) -> int:
        return self._page_size

    def exists(self, file: str) -> bool:
        return file in self._pages or file in self._logs

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
        return len(self._logs.get(file, b"")) + len(self._pages.get(file, ())) * self._page_size

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
        return pages[page_index]

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages) or len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Refused a page write to {file!r} at {page_index}.", file=file, page=page_index
            )
        pages[page_index] = bytes(data)

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
            raise GrafxUnsupportedOperation(
                f"truncate_log only shrinks; {file!r} holds {len(log)} bytes.", file=file
            )
        del log[size:]

    def durable_barrier(self, file: str | None = None) -> None:
        return None


class FakeClock:
    """Deterministic clock: monotonic advances only when the test asks for it."""

    def __init__(self, monotonic: float = 100.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move the monotonic and the wall reading forward by the same amount."""
        self._monotonic += seconds
        self._wall += seconds


class FakeCoordinator:
    """Single-participant coordinator: enough shape to satisfy the port, no cross-process claims."""

    def __init__(self) -> None:
        self._epoch: Epoch = 1
        self._readers: dict[str, Lsn] = {}
        self._next_reader = 0

    def owner_id(self) -> str:
        return "fake-owner"

    def current_epoch(self) -> Epoch:
        return self._epoch

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        return Lease(
            owner_id=self.owner_id(),
            epoch=self._epoch,
            acquired_monotonic=0.0,
            heartbeat_seq=1,
            ttl_seconds=timeout,
        )

    def renew_lease(self, lease: Lease) -> Lease:
        return Lease(
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            acquired_monotonic=lease.acquired_monotonic,
            heartbeat_seq=lease.heartbeat_seq + 1,
            ttl_seconds=lease.ttl_seconds,
        )

    def release_lease(self, lease: Lease) -> None:
        return None

    def validate_epoch(self, epoch: Epoch) -> None:
        return None

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None:
        return None

    def takeover(self) -> Lease:
        self._epoch += 1
        return self.acquire_writer_lease(timeout=1.0)

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle:
        self._next_reader += 1
        reader_id = f"reader-{self._next_reader}"
        self._readers[reader_id] = snapshot_lsn
        return ReaderHandle(reader_id=reader_id, snapshot_lsn=snapshot_lsn)

    def refresh_reader(self, handle: ReaderHandle) -> None:
        return None

    def unregister_reader(self, handle: ReaderHandle) -> None:
        self._readers.pop(handle.reader_id, None)

    def reader_horizon(self) -> Lsn | None:
        return min(self._readers.values()) if self._readers else None

    def exclusive(self, name: str, *, timeout: float) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


class FakePageCodec:
    """Identity codec: it proves the shape of the port without claiming to be CRC-32C."""

    def __init__(self, page_size: int = 8192) -> None:
        self._page_size = page_size

    @property
    def format_version(self) -> int:
        return 1

    def checksum(self, payload: bytes) -> int:
        return sum(payload) & 0xFFFFFFFF

    def encode_page(self, page: object) -> bytes:
        return bytes(self._page_size)

    def decode_page(self, raw: bytes, *, verify: bool = True) -> object:
        return raw


class RecordingMetricsSink:
    """Metrics sink that keeps every call, so a test can assert on what was emitted."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.registered: dict[str, MetricDescriptor] = {}
        self.calls: list[tuple[str, str, float]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        self.registered[descriptor.name] = descriptor

    def increment(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("increment", name, value))

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("set_gauge", name, value))

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("observe", name, value))

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> contextlib.AbstractContextManager[None]:
        return self._timed(name)

    @contextlib.contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        self.calls.append(("time_enter", name, 0.0))
        yield
        self.calls.append(("time_exit", name, 0.0))

    def snapshot(self) -> Mapping[str, object]:
        return {name: descriptor.kind.value for name, descriptor in self.registered.items()}


class FakeVectorMath:
    """Minimal vector arithmetic in pure Python, sufficient to satisfy the port."""

    @property
    def name(self) -> str:
        return "fake"

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        return float(sum(x * y for x, y in zip(a, b)))

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        denominator = self.norm(a) * self.norm(b)
        return self.dot(a, b) / denominator if denominator else 0.0

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        return float(sum((x - y) ** 2 for x, y in zip(a, b))) ** 0.5

    def norm(self, a: Sequence[float]) -> float:
        return float(sum(x * x for x in a)) ** 0.5

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        length = self.norm(a)
        return tuple(x / length for x in a) if length else tuple(float(x) for x in a)

    def score(self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric) -> float:
        if metric is DistanceMetric.COSINE:
            return self.cosine(a, b)
        if metric is DistanceMetric.DOT:
            return self.dot(a, b)
        return -self.euclidean(a, b)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        scored = [(identifier, self.score(query, vector, metric)) for identifier, vector in candidates]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


class RecordingEventSink:
    """Event sink that keeps every event, so a test can assert on what was published."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        self.events.append((event, dict(payload)))


@pytest.fixture
def fake_ports() -> dict[str, object]:
    """Return one fresh adapter per required port slot."""
    return {
        "storage": FakeStorageDevice(),
        "clock": FakeClock(),
        "coordinator": FakeCoordinator(),
        "codec": FakePageCodec(),
        "metrics": RecordingMetricsSink(),
        "vector_math": FakeVectorMath(),
        "events": RecordingEventSink(),
    }


@pytest.fixture
def complete_registry(fake_ports: dict[str, object]) -> PortRegistry:
    """Return a registry with every required slot bound to a fake adapter."""
    registry = PortRegistry()
    for slot, instance in fake_ports.items():
        registry.bind(slot, instance)
    return registry
