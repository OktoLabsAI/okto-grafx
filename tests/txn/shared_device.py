"""A storage device over a real directory that keeps NO open handle between calls.

It exists because of a measured Windows behaviour that belongs to C2, not to C5, and that would
otherwise make every multi-participant test in this suite fail for a reason outside the component
under test.

Measured on this machine, with a holder that opened a file through ``CreateFileW`` with
``FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`` -- which is exactly what
``LocalStorageDevice`` does under amendment A16:

* ``os.remove`` on that file SUCCEEDS and the name leaves the namespace at once, which is the
  behaviour A16 records;
* ``os.replace`` ONTO that file FAILS with ``WinError 5``, from another thread of the same
  process and from another process alike.

``LocalStorageDevice`` caches its descriptors, so as soon as one participant has READ
``control/writer.lease`` or ``control/commit.state``, no other participant can publish either of
them through ``atomic_replace`` -- the primitive CONTRACT.md section 6.1 requires for both files
and the one C3 publishes the lease with. Opening and closing per call removes the holder, and
with it the problem.

Everything else here is ordinary: real files, real bytes, real processes, and the same port
semantics C2 implements.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex

__all__ = ["RETRY_ATTEMPTS", "SharedDirectoryDevice"]

RETRY_ATTEMPTS: int = 8
"""How many times a Windows sharing violation is ridden out before it is reported."""

_BINARY: int = getattr(os, "O_BINARY", 0)
_T = TypeVar("_T")


class SharedDirectoryDevice:
    """A StorageDevice over a directory, opening and closing a descriptor per call."""

    def __init__(self, root: str | Path, *, page_size: int = 512) -> None:
        """Open the device over the directory, creating it when it is not there."""
        self._root = str(Path(root).resolve())
        self._page_size = int(page_size)
        os.makedirs(self._root, exist_ok=True)

    @property
    def name(self) -> str:
        """Return the bounded label of this device family."""
        return "shared-directory"

    @property
    def page_size(self) -> int:
        """Return the page size every paged file of this device uses."""
        return self._page_size

    # --- namespace ------------------------------------------------------------------------

    def exists(self, file: str) -> bool:
        """Return True when the named file is present."""
        return os.path.isfile(self._path(file))

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create the file, making its parent directories first."""
        path = self._path(file)
        os.makedirs(os.path.dirname(path) or self._root, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | _BINARY | (os.O_EXCL if exclusive else 0)
        os.close(self._retry(lambda: os.open(path, flags), file, "create"))

    def remove(self, file: str) -> None:
        """Delete the named file."""
        self._retry(lambda: os.remove(self._path(file)), file, "remove")

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every file whose slash-separated name starts with the prefix."""
        found: list[str] = []
        for directory, _subdirectories, names in os.walk(self._root):
            for name in names:
                relative = os.path.relpath(os.path.join(directory, name), self._root)
                slashed = relative.replace(os.sep, "/")
                if slashed.startswith(prefix):
                    found.append(slashed)
        return tuple(sorted(found))

    def file_size(self, file: str) -> int:
        """Return how many bytes the file holds."""
        return os.path.getsize(self._path(file))

    def atomic_replace(self, source: str, target: str) -> None:
        """Replace the target with the source in one step."""
        path = self._path(target)
        os.makedirs(os.path.dirname(path) or self._root, exist_ok=True)
        self._retry(lambda: os.replace(self._path(source), path), target, "atomic_replace")

    def recycle(self, file: str) -> bool:
        """Release the file, reporting whether the space came back at once."""
        try:
            os.remove(self._path(file))
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    # --- paged space ----------------------------------------------------------------------

    def page_count(self, file: str) -> int:
        """Return how many whole pages the file holds."""
        return self.file_size(file) // self._page_size

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by whole zero-filled pages and return the first new index."""
        if count <= 0:
            raise GrafxUnsupportedOperation(
                "allocate needs a count of one page or more.", file=file
            )
        descriptor = self._open(file, os.O_RDWR, "allocate")
        try:
            size = os.fstat(descriptor).st_size
            os.lseek(descriptor, size, os.SEEK_SET)
            os.write(descriptor, bytes(self._page_size * count))
            return size // self._page_size
        finally:
            os.close(descriptor)

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return the bytes of one page."""
        descriptor = self._open(file, os.O_RDONLY, "read_page")
        try:
            os.lseek(descriptor, page_index * self._page_size, os.SEEK_SET)
            raw = os.read(descriptor, self._page_size)
        finally:
            os.close(descriptor)
        if len(raw) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} is short.", file=file, page=page_index
            )
        return raw

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Write the bytes of one already allocated page."""
        if len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"A page image must be {self._page_size} bytes.", file=file, page=page_index
            )
        if page_index >= self.page_count(file):
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} is not allocated.", file=file, page=page_index
            )
        descriptor = self._open(file, os.O_RDWR, "write_page")
        try:
            os.lseek(descriptor, page_index * self._page_size, os.SEEK_SET)
            os.write(descriptor, bytes(data))
        finally:
            os.close(descriptor)

    # --- append-only log space --------------------------------------------------------------

    def append_log(self, file: str, payload: bytes) -> int:
        """Append to the file and return its new total size."""
        descriptor = self._open(file, os.O_RDWR | os.O_APPEND, "append_log")
        try:
            written = os.write(descriptor, bytes(payload))
            if written != len(payload):
                raise GrafxDeviceFull("The device stored only part of the payload.", file=file)
            return os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return a byte range of the file."""
        descriptor = self._open(file, os.O_RDONLY, "read_log")
        try:
            os.lseek(descriptor, offset, os.SEEK_SET)
            return os.read(descriptor, length)
        finally:
            os.close(descriptor)

    def log_size(self, file: str) -> int:
        """Return how many bytes the file holds."""
        return self.file_size(file)

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink the file; growing through this door is a programming error."""
        if size > self.file_size(file):
            raise GrafxUnsupportedOperation("truncate_log only shrinks.", file=file)
        descriptor = self._open(file, os.O_RDWR, "truncate_log")
        try:
            os.ftruncate(descriptor, size)
        finally:
            os.close(descriptor)

    # --- durability -------------------------------------------------------------------------

    def durable_barrier(self, file: str | None = None) -> None:
        """Put the named file, or every file, on the platter."""
        names = (file,) if file is not None else self.list_files()
        for name in names:
            if name is None or not self.exists(name):
                continue
            try:
                descriptor = os.open(self._path(name), os.O_RDWR | _BINARY)
            except OSError as failure:
                raise GrafxDurabilityBarrierFailed(
                    f"The device could not open {name!r} to make it durable.",
                    file=name,
                    reason="access_failed",
                    retryable=True,
                ) from failure
            try:
                os.fsync(descriptor)
            except OSError as failure:
                raise GrafxDurabilityBarrierFailed(
                    f"The device could not make {name!r} durable.", file=name
                ) from failure
            finally:
                os.close(descriptor)

    # --- internals ---------------------------------------------------------------------------

    def _path(self, file: str) -> str:
        """Return the real path of a slash-separated device name."""
        return os.path.join(self._root, *str(file).split("/"))

    def _open(self, file: str, flags: int, operation: str) -> int:
        """Open a descriptor for one call, riding out a brief sharing violation."""
        return self._retry(lambda: os.open(self._path(file), flags | _BINARY), file, operation)

    @staticmethod
    def _retry(action: Callable[[], _T], file: str, operation: str) -> _T:
        """Run an operation while Windows briefly refuses it, then report the failure typed."""
        backoff = 0.001
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return action()
            except FileNotFoundError:
                raise
            except PermissionError as failure:
                if attempt == RETRY_ATTEMPTS:
                    raise GrafxStorageError(
                        f"The device failed {operation} on {file!r}.",
                        file=file,
                        operation=operation,
                        attempts=attempt,
                    ) from failure
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 0.05)
        raise GrafxStorageError(
            f"The device failed {operation} on {file!r}.", file=file, operation=operation
        )
