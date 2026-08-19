"""Test doubles for the C3 suite: a manual clock, a directory-backed device and instrumentation.

None of this lives in ``src/``. The manual clock is what makes every timing assertion in this
suite deterministic with no real sleeping: the coordinator takes its sleeper as a parameter, so a
"sleep" here simply advances the fake monotonic reading.

The directory device is a minimal but honest implementation of the StorageDevice port over a real
file system. C2 ships the production device; this one exists so the coordinator can be exercised
across two real processes before that wave lands, and so this suite never imports a concrete
device from ``src/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxUnsupportedOperation,
)

__all__ = [
    "BINARY",
    "DirectoryStorageDevice",
    "HookStorageDevice",
    "ManualClock",
    "RecordingMetricsSink",
    "WRITE_OPERATIONS",
]

BINARY: int = getattr(os, "O_BINARY", 0)
"""``os.O_BINARY`` where it exists. Omitting it on Windows costs a trailing 0x1A byte."""

WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "create",
        "remove",
        "recycle",
        "atomic_replace",
        "append_log",
        "truncate_log",
        "allocate",
        "write_page",
        "durable_barrier",
    }
)
"""Every device call that can change a byte on the device or force one out to it."""


class ManualClock:
    """A Clock whose readings only move when the test moves them.

    ``monotonic`` and ``wall`` are independent on purpose: ``jump_wall`` moves the human-facing
    reading forwards or backwards without touching the monotonic one, which is exactly the
    situation FR-7 forbids any liveness decision from noticing.
    """

    def __init__(self, monotonic: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(monotonic)
        self._wall = float(wall)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        """Return the current fake monotonic reading."""
        return self._monotonic

    def wall(self) -> float:
        """Return the current fake wall reading."""
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both readings forwards, the way real time does."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)

    def jump_wall(self, seconds: float) -> None:
        """Move the wall reading only, forwards or backwards, as an operator or NTP would."""
        self._wall += float(seconds)

    def sleep(self, seconds: float) -> None:
        """Stand in for a real sleep: record it and advance the monotonic reading."""
        self.slept.append(float(seconds))
        self.advance(seconds)

    def freeze_sleep(self, seconds: float) -> None:
        """Stand in for a sleep that does not advance time, to exercise the wait bounds."""
        self.slept.append(float(seconds))


class RecordingMetricsSink:
    """A metrics sink that keeps every observation so a test can assert on it."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.observations: list[tuple[str, float, dict[str, str]]] = []
        self.registered: list[object] = []

    @property
    def enabled(self) -> bool:
        """Return whether the hot path should pay for an observation."""
        return self._enabled

    def register(self, descriptor: object) -> None:
        """Accept a descriptor without validating it; C8 owns the catalog."""
        self.registered.append(descriptor)

    def increment(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        """Record a counter increment."""
        self.observations.append((name, value, dict(labels or {})))

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a gauge assignment."""
        self.observations.append((name, value, dict(labels or {})))

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram observation."""
        self.observations.append((name, value, dict(labels or {})))

    def outcomes(self, name: str) -> list[str]:
        """Return the ``outcome`` label of every observation of one metric, in order."""
        return [labels["outcome"] for metric, _value, labels in self.observations if metric == name]


class DirectoryStorageDevice:
    """A StorageDevice over a real directory, good enough to run two processes against one database."""

    def __init__(self, root: str | Path, *, page_size: int = 8192) -> None:
        self._root = Path(root)
        self._page_size = int(page_size)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        """Return the device name."""
        return f"directory:{self._root}"

    @property
    def page_size(self) -> int:
        """Return the page size of this device."""
        return self._page_size

    @property
    def root(self) -> Path:
        """Return the directory this device is rooted at."""
        return self._root

    def _path(self, file: str) -> Path:
        return self._root.joinpath(*file.split("/"))

    def exists(self, file: str) -> bool:
        """Return whether the named file exists."""
        return self._path(file).exists()

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create the file, making its parent directories on the way."""
        path = self._path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if exclusive:
                raise GrafxUnsupportedOperation(f"File {file!r} already exists.", file=file)
            return
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | BINARY, 0o600)
        os.close(handle)

    def remove(self, file: str) -> None:
        """Remove the file when it exists."""
        path = self._path(file)
        if path.exists():
            path.unlink()

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every file whose slash-separated name starts with the prefix."""
        names: list[str] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            name = "/".join(path.relative_to(self._root).parts)
            if name.startswith(prefix):
                names.append(name)
        return tuple(names)

    def file_size(self, file: str) -> int:
        """Return the size of the file in bytes."""
        return self._path(file).stat().st_size

    def atomic_replace(self, source: str, target: str) -> None:
        """Replace the target with the source in one step."""
        target_path = self._path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._path(source), target_path)

    def recycle(self, file: str) -> bool:
        """Release the file, reporting False when the platform deferred the deletion."""
        path = self._path(file)
        if not path.exists():
            return True
        try:
            path.unlink()
        except PermissionError:
            return False
        return True

    def page_count(self, file: str) -> int:
        """Return how many whole pages the file holds."""
        return self.file_size(file) // self._page_size

    def allocate(self, file: str, count: int = 1) -> int:
        """Grow the file by whole zero-filled pages and return the first new index."""
        path = self._path(file)
        first = self.page_count(file)
        with path.open("ab") as handle:
            handle.write(b"\x00" * (self._page_size * count))
        return first

    def read_page(self, file: str, page_index: int) -> bytes:
        """Return the bytes of one page."""
        with self._path(file).open("rb") as handle:
            handle.seek(page_index * self._page_size)
            return handle.read(self._page_size)

    def write_page(self, file: str, page_index: int, data: bytes) -> None:
        """Overwrite one already allocated page."""
        if len(data) != self._page_size:
            raise GrafxUnsupportedOperation("A page write must carry exactly one page.", file=file)
        if page_index >= self.page_count(file):
            raise GrafxUnsupportedOperation("A page write needs an allocated page.", file=file)
        with self._path(file).open("r+b") as handle:
            handle.seek(page_index * self._page_size)
            handle.write(data)

    def append_log(self, file: str, payload: bytes) -> int:
        """Append to the log file and return its new total size."""
        path = self._path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("ab") as handle:
                written = handle.write(payload)
        except OSError as failure:
            raise GrafxDeviceFull("The device refused the append.", file=file) from failure
        if written != len(payload):
            raise GrafxDeviceFull("The device accepted only part of the append.", file=file)
        return self.file_size(file)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return a byte range of the log file."""
        with self._path(file).open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    def log_size(self, file: str) -> int:
        """Return the size of the log file."""
        return self.file_size(file)

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink the log file to the given size."""
        path = self._path(file)
        if size > self.file_size(file):
            raise GrafxUnsupportedOperation("A log can only be truncated.", file=file)
        with path.open("r+b") as handle:
            handle.truncate(size)

    def durable_barrier(self, file: str | None = None) -> None:
        """Force the named file out to the device."""
        if file is None:
            return
        path = self._path(file)
        if not path.exists():
            raise GrafxDurabilityBarrierFailed("No such file to flush.", file=file)
        try:
            # Two Windows traps in one line. The handle must be writable, because flushing a
            # read-only handle is refused; and it must be binary, because the C runtime opens in
            # text mode by default and commits a text-mode handle by truncating the file at a
            # trailing 0x1A byte, silently losing it.
            handle = os.open(path, os.O_RDWR | BINARY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as failure:
            raise GrafxDurabilityBarrierFailed("The barrier did not complete.", file=file) from failure


class HookStorageDevice:
    """A device wrapper that counts calls, can forbid writes, and can run a hook before a call.

    The hook is what makes the takeover interleaving test deterministic: the k-th device call of
    one participant becomes the point where the other participant runs. The hook never fires
    inside itself, so the second participant is free to use its own device without recursion.
    """

    def __init__(
        self,
        inner: DirectoryStorageDevice,
        *,
        forbid_writes: bool = False,
        hook: Callable[[str, int], None] | None = None,
        hook_at: int | None = None,
    ) -> None:
        self._inner = inner
        self.forbid_writes = forbid_writes
        self.hook = hook
        self.hook_at = hook_at
        self.calls: list[str] = []
        self.write_calls: list[str] = []
        self.touched: list[tuple[str, str]] = []
        self._inside_hook = False
        self.hook_fired = False

    @property
    def name(self) -> str:
        """Return the wrapped device name."""
        return self._inner.name

    @property
    def page_size(self) -> int:
        """Return the wrapped device page size."""
        return self._inner.page_size

    @property
    def inner(self) -> DirectoryStorageDevice:
        """Return the wrapped device."""
        return self._inner

    def reset(self) -> None:
        """Forget every recorded call."""
        self.calls.clear()
        self.write_calls.clear()
        self.touched.clear()

    def _record(self, operation: str, file: str | None = None) -> None:
        self.calls.append(operation)
        self.touched.append((operation, "" if file is None else file))
        if operation in WRITE_OPERATIONS:
            self.write_calls.append(operation)
            if self.forbid_writes:
                raise AssertionError(f"The device was written through {operation!r} when it must not be.")
        if self._inside_hook or self.hook is None or self.hook_at is None:
            return
        if len(self.calls) != self.hook_at:
            return
        self._inside_hook = True
        self.hook_fired = True
        try:
            self.hook(operation, len(self.calls))
        finally:
            self._inside_hook = False

    def exists(self, file: str) -> bool:
        """Delegate after recording the call."""
        self._record("exists", file)
        return self._inner.exists(file)

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Delegate after recording the call."""
        self._record("create", file)
        self._inner.create(file, exclusive=exclusive)

    def remove(self, file: str) -> None:
        """Delegate after recording the call."""
        self._record("remove", file)
        self._inner.remove(file)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Delegate after recording the call."""
        self._record("list_files", prefix)
        return self._inner.list_files(prefix)

    def file_size(self, file: str) -> int:
        """Delegate after recording the call."""
        self._record("file_size", file)
        return self._inner.file_size(file)

    def atomic_replace(self, source: str, target: str) -> None:
        """Delegate after recording the call."""
        self._record("atomic_replace", f"{source}->{target}")
        self._inner.atomic_replace(source, target)

    def recycle(self, file: str) -> bool:
        """Delegate after recording the call."""
        self._record("recycle", file)
        return self._inner.recycle(file)

    def page_count(self, file: str) -> int:
        """Delegate after recording the call."""
        self._record("page_count", file)
        return self._inner.page_count(file)

    def allocate(self, file: str, count: int = 1) -> int:
        """Delegate after recording the call."""
        self._record("allocate", file)
        return self._inner.allocate(file, count)

    def read_page(self, file: str, page_index: int) -> bytes:
        """Delegate after recording the call."""
        self._record("read_page", file)
        return self._inner.read_page(file, page_index)

    def write_page(self, file: str, page_index: int, data: bytes) -> None:
        """Delegate after recording the call."""
        self._record("write_page", file)
        self._inner.write_page(file, page_index, data)

    def append_log(self, file: str, payload: bytes) -> int:
        """Delegate after recording the call."""
        self._record("append_log", file)
        return self._inner.append_log(file, payload)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Delegate after recording the call."""
        self._record("read_log", file)
        return self._inner.read_log(file, offset, length)

    def log_size(self, file: str) -> int:
        """Delegate after recording the call."""
        self._record("log_size", file)
        return self._inner.log_size(file)

    def truncate_log(self, file: str, size: int) -> None:
        """Delegate after recording the call."""
        self._record("truncate_log", file)
        self._inner.truncate_log(file, size)

    def durable_barrier(self, file: str | None = None) -> None:
        """Delegate after recording the call."""
        self._record("durable_barrier", file or "")
        self._inner.durable_barrier(file)
