"""Fixtures for the write-ahead log suite (C4).

Everything here is deterministic on purpose. The clock returns numbers a test chooses, the
metrics sink records what it was told rather than formatting it, and the log is built over the
in-memory device unless a test is about the real file system. Nothing sleeps and nothing depends
on how fast the machine is.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.ports.metrics import MetricDescriptor
from okto_grafx.domain.wal import WalRecord, WalRecordType
from okto_grafx.engine.wal_manager import WalManager

PAGE_SIZE: int = 512
"""Small page, so a whole device fits in a failure message."""

DESCRIPTOR: str = "hash-v1;partitions_per_table=64"
"""The granularity descriptor of CONTRACT.md section 6.5, used by every test that needs one."""

SEGMENT_BYTES: int = 4096
"""Roomy enough that a test rolls only when it means to."""


class FrozenClock:
    """A clock that answers exactly what the test set, so no assertion depends on real time."""

    def __init__(self, monotonic: float = 100.0, wall: float = 1_700_000_000.0) -> None:
        """Start at the two readings the test asked for."""
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        """Return the local reading used for liveness, which the log never consults."""
        return self._monotonic

    def wall(self) -> float:
        """Return the human-facing reading stamped into a segment header."""
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
        self.timed: list[str] = []

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
        return self._timed(name)

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        """Record the timing of one block without reading a clock."""
        self.timed.append(name)
        yield

    def snapshot(self) -> Mapping[str, object]:
        """Return the current values, which is what a gate would read."""
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "observations": list(self.observations),
        }

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


def make_record(
    txn_id: int = 1,
    *,
    record_type: int = WalRecordType.WRITE_PAGE,
    payload: bytes = b"",
    epoch: int = 1,
    descriptor: str = "",
) -> WalRecord:
    """Return one ordinary record, with the fields a test usually wants to vary."""
    return WalRecord(
        record_type=record_type,
        payload=payload if payload else bytes(16),
        descriptor=descriptor,
        epoch=epoch,
        txn_id=txn_id,
    )


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
def make_wal(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> Callable[..., WalManager]:
    """Return a factory that opens a log over any device."""

    def _build(
        device: object,
        *,
        segment_bytes: int = SEGMENT_BYTES,
        descriptor: str = DESCRIPTOR,
        directory: str = "wal",
        open_now: bool = True,
    ) -> WalManager:
        manager = WalManager(
            device,
            clock,
            metrics,
            directory=directory,
            segment_bytes=segment_bytes,
            descriptor=descriptor,
        )
        if open_now:
            manager.open()
        return manager

    return _build


@pytest.fixture
def wal(make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice) -> WalManager:
    """Return an opened log over the in-memory device."""
    return make_wal(memory_device)


HOLDER_SOURCE: str = (
    "import sys\n"
    "path, mode, source_root = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "if mode == 'grafx':\n"
    "    import os\n"
    "    sys.path.insert(0, source_root)\n"
    "    from okto_grafx.adapters.storage_local import LocalStorageDevice\n"
    "    root, _, name = path.replace(os.sep, '/').rpartition('/wal/')\n"
    "    holder = LocalStorageDevice(root, page_size=512)\n"
    "    holder.read_log('wal/' + name, 0, 4)\n"
    "else:\n"
    "    holder = open(path, 'rb')\n"
    "sys.stdout.write('held\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
)
"""Program of the second process: hold a handle, say so, wait for permission to let go.

Two kinds of holder, because they are two different situations. ``grafx`` opens the segment
through this project's own adapter, which shares delete on Windows (A16) -- that is the slow
reader of AC-9, and the platform is free to arm a pending delete behind it. The plain holder
opens without delete sharing, which is what an antivirus or an indexer looks like, and A23 and
A27 then leave the name in place until the caller asks again.
"""


class FileHolder:
    """A second process holding one file open, released when the test asks for it."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        """Take ownership of a child that already announced it holds the file."""
        self._process = process

    def release(self) -> None:
        """Let the child close the handle and wait for it to be gone."""
        if self._process.poll() is not None:  # pragma: no cover - only if the child died early
            return
        assert self._process.stdin is not None
        try:
            self._process.stdin.write("\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError):  # pragma: no cover - only if the child died early
            self._process.kill()
        self._process.wait(timeout=30)

    def kill(self) -> None:
        """Stop the child whatever state it is in."""
        if self._process.poll() is None:  # pragma: no cover - only on a failing test
            self._process.kill()
            self._process.wait(timeout=30)


@pytest.fixture
def holder_process() -> Iterator[Callable[..., FileHolder]]:
    """Return a factory that starts a second process holding a real file open."""
    holders: list[FileHolder] = []

    def _hold(path: Path, *, mode: str = "plain") -> FileHolder:
        # A94: the child resolves okto_grafx from this checkout, never from whatever an editable
        # install happens to point at.
        source_root = str(Path(__file__).resolve().parents[2] / "src")
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SOURCE, str(path), mode, source_root],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        if line != "held":  # pragma: no cover - only when the child could not start
            process.kill()
            _, complaint = process.communicate(timeout=30)
            raise AssertionError(
                f"the second process could not hold {path}: {complaint.strip() or 'no output'}"
            )
        holder = FileHolder(process)
        holders.append(holder)
        return holder

    try:
        yield _hold
    finally:
        for holder in holders:
            holder.kill()
