"""The in-memory storage adapter (CONTRACT.md section 5, SPEC-M1 TR-3).

``DatabaseConfig(path=":memory:")`` selects this device. It is not a toy: it is the faithful
twin of :class:`~okto_grafx.adapters.storage_local.LocalStorageDevice`, so the whole engine test
suite can run against it and observe the same behaviour it observes against a real directory.

Faithfulness is enforced by construction rather than by discipline: the name rules, the payload
rules and every typed refusal are imported from the local adapter, so the two devices cannot
drift apart. The only intended differences are the ones the medium forces:

* bytes live in a ``bytearray`` per file instead of in a directory;
* ``durable_barrier`` has nothing to flush, so it only proves that the named file exists;
* ``recycle`` always reclaims immediately, because no handle can hold a name here.
"""

from __future__ import annotations

import threading
from types import TracebackType

from okto_grafx.adapters.storage_local import (
    as_payload,
    find_case_conflict,
    normalize_logical_name,
    barrier_failure_from,
    refuse_missing_file,
    refuse_not_a_directory,
    refuse_not_a_file,
    refuse_operation,
    refuse_page_not_allocated,
    refuse_page_payload,
    refuse_unaligned_file,
    validate_allocation,
    validate_read_range,
)
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import DEFAULT_PAGE_SIZE, validate_page_size

__all__ = ["MemoryStorageDevice"]


class MemoryStorageDevice:
    """StorageDevice backed by byte buffers, with the semantics of the local device."""

    def __init__(self, *, page_size: int = DEFAULT_PAGE_SIZE, capacity_bytes: int | None = None) -> None:
        """Open an empty device; capacity_bytes, when given, is the size the device refuses to pass."""
        self._page_size = validate_page_size(page_size)
        self._capacity = _validate_capacity(capacity_bytes)
        self._files: dict[str, bytearray] = {}
        self._directories: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False

    # --- identity -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Bounded label of this device family, safe to use as a metric label value (TR-7)."""
        return "memory"

    @property
    def page_size(self) -> int:
        """Size in bytes of every page this device reads and writes."""
        return self._page_size

    @property
    def capacity_bytes(self) -> int | None:
        """Total size this device refuses to exceed, or None when it is only bounded by memory."""
        return self._capacity

    def used_bytes(self) -> int:
        """Return how many bytes every file of this device holds together."""
        with self._lock:
            return sum(len(buffer) for buffer in self._files.values())

    # --- namespace ----------------------------------------------------------------------

    def exists(self, file: str) -> bool:
        """Return True when the exact name exists; a name that differs only by case is refused."""
        with self._lock:
            name = normalize_logical_name(file)
            self._require_open()
            return self._resolve_identity(name)

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create an empty file. With exclusive set, an already existing file is refused."""
        with self._lock:
            name = normalize_logical_name(file)
            self._require_open()
            if self._resolve_identity(name):
                if exclusive:
                    raise refuse_operation(
                        "file_exists", f"File {name!r} already exists on this device.", file=name
                    )
                return
            self._require_directory_parents(name)
            self._require_not_a_directory(name)
            self._files[name] = bytearray()
            self._remember_directories(name)

    def remove(self, file: str) -> None:
        """Delete the named file. Removing a file the device does not hold is a failure."""
        with self._lock:
            name = normalize_logical_name(file)
            self._require_open()
            if not self._resolve_identity(name):
                raise refuse_missing_file(name, "remove")
            del self._files[name]

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every logical name starting with the prefix, sorted."""
        if not isinstance(prefix, str):
            raise refuse_operation(
                "not_a_string", f"A name prefix must be a string, got {type(prefix).__name__}."
            )
        with self._lock:
            self._require_open()
            return tuple(sorted(name for name in self._files if name.startswith(prefix)))

    def file_size(self, file: str) -> int:
        """Return the size of the named file in bytes."""
        with self._lock:
            return len(self._buffer(normalize_logical_name(file)))

    def atomic_replace(self, source: str, target: str) -> None:
        """Move source onto target so a reader observes either the old or the new content."""
        with self._lock:
            source_name = normalize_logical_name(source)
            target_name = normalize_logical_name(target)
            self._require_open()
            if source_name == target_name:
                raise refuse_operation(
                    "same_file", "atomic_replace needs two different names.", file=source_name
                )
            if not self._resolve_identity(source_name):
                raise refuse_missing_file(source_name, "atomic_replace")
            # Replacing a missing target is legal; replacing a target that differs from the asked
            # name only by case is not, which is what this resolution refuses.
            self._resolve_identity(target_name)
            self._require_directory_parents(target_name)
            self._require_not_a_directory(target_name)
            self._files[target_name] = self._files.pop(source_name)
            self._remember_directories(target_name)

    def recycle(self, file: str) -> bool:
        """Release a file. No handle can hold a name here, so the space is always reclaimed."""
        with self._lock:
            name = normalize_logical_name(file)
            self._require_open()
            self._resolve_identity(name)
            self._files.pop(name, None)
            return True

    def pending_deletes(self) -> tuple[str, ...]:
        """Return the deletions the platform deferred. This device never defers one."""
        with self._lock:
            return ()

    def retry_pending_deletes(self, *, force: bool = False) -> int:
        """Try every deferred deletion once. This device never defers one, so nothing happens."""
        with self._lock:
            return 0

    # --- paged space --------------------------------------------------------------------

    def page_count(self, file: str) -> int:
        """Return how many whole pages the named file currently holds."""
        with self._lock:
            name = normalize_logical_name(file)
            return self._page_count(name, self._buffer(name))

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by count zero filled pages and return the first new page index."""
        with self._lock:
            name = normalize_logical_name(file)
            validate_allocation(name, count)
            buffer = self._buffer(name)
            first = self._page_count(name, buffer)
            self._reserve(name, count * self._page_size)
            buffer.extend(bytes(count * self._page_size))
            return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return exactly page_size bytes from an already allocated page."""
        with self._lock:
            name = normalize_logical_name(file)
            buffer = self._buffer(name)
            offset = self._page_offset(name, page_index, buffer, "read_page")
            return bytes(buffer[offset : offset + self._page_size])

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Overwrite one already allocated page; the payload must be exactly page_size bytes."""
        with self._lock:
            name = normalize_logical_name(file)
            payload = as_payload(name, data)
            if len(payload) != self._page_size:
                raise refuse_page_payload(name, page_index, len(payload), self._page_size)
            buffer = self._buffer(name)
            offset = self._page_offset(name, page_index, buffer, "write_page")
            # The offset was proved to sit inside the buffer, so this assignment can never grow it.
            buffer[offset : offset + self._page_size] = payload

    # --- append-only log space ----------------------------------------------------------

    def append_log(self, file: str, payload: bytes) -> int:
        """Append the payload and return the new total size; a partial append is a device full."""
        with self._lock:
            name = normalize_logical_name(file)
            data = as_payload(name, payload)
            buffer = self._buffer(name)
            if not data:
                return len(buffer)
            self._reserve(name, len(data))
            buffer.extend(data)
            return len(buffer)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Fill length bytes from offset unless EOF is reached, then return what remains."""
        with self._lock:
            name = normalize_logical_name(file)
            validate_read_range(name, offset, length)
            buffer = self._buffer(name)
            if length == 0:
                return b""
            return bytes(buffer[offset : offset + length])

    def log_size(self, file: str) -> int:
        """Return the current size in bytes of an append-only file."""
        with self._lock:
            name = normalize_logical_name(file)
            return len(self._buffer(name))

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink the file to size. Growing through this door is refused."""
        with self._lock:
            name = normalize_logical_name(file)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise refuse_operation(
                    "invalid_size", "truncate_log needs a size of zero or more.", file=name, size=size
                )
            buffer = self._buffer(name)
            if size > len(buffer):
                raise refuse_operation(
                    "truncate_would_grow",
                    f"truncate_log only shrinks; {name!r} holds {len(buffer)} bytes and {size} was asked.",
                    file=name,
                    size=size,
                    current=len(buffer),
                )
            del buffer[size:]

    # --- durability ---------------------------------------------------------------------

    def durable_barrier(self, file: str | None = None) -> None:
        """Prove the named file exists. There is no stable storage behind this device to flush.

        A28: like the local device, every failure of this door is a GrafxDurabilityBarrierFailed
        carrying the classification in its details, so a caller cannot tell the two families
        apart by the type it has to count.
        """
        with self._lock:
            try:
                self._require_open()
                if file is None:
                    return
                name = normalize_logical_name(file)
                if not self._resolve_identity(name):
                    raise refuse_missing_file(name, "open")
            except GrafxDurabilityBarrierFailed:
                raise
            except GrafxError as failure:
                raise barrier_failure_from(failure) from failure

    # --- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        """Refuse further work. The bytes stay, exactly as a closed directory keeps its files."""
        with self._lock:
            self._closed = True

    def reopen(self) -> None:
        """Accept work again over the same bytes: the twin of opening the same directory again."""
        with self._lock:
            self._closed = False

    def __enter__(self) -> MemoryStorageDevice:
        """Return the device itself so it can be used as a context manager."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the device when the block ends, successfully or not."""
        self.close()

    # --- internals ----------------------------------------------------------------------

    def _require_open(self) -> None:
        """Refuse any use of a device that was already closed."""
        if self._closed:
            raise refuse_operation("device_closed", "This storage device is closed.")

    def _entries_at(self, prefix: str) -> tuple[str, ...]:
        """Return every name that directly follows the prefix in the stored namespace.

        Directories are part of the answer even when they hold no file any more: a real
        directory outlives the files that were created in it, so the twin keeps it too and both
        devices go on refusing a name that differs from it only by case.
        """
        entries: set[str] = set()
        for stored in self._files:
            if stored.startswith(prefix):
                entries.add(stored[len(prefix) :].split("/", 1)[0])
        for directory in self._directories:
            if directory.startswith(prefix) and len(directory) > len(prefix):
                entries.add(directory[len(prefix) :].split("/", 1)[0])
        return tuple(sorted(entries))

    def _remember_directories(self, name: str) -> None:
        """Keep every directory a name implies, the way a file system keeps a real directory."""
        segments = name.split("/")[:-1]
        for depth in range(1, len(segments) + 1):
            self._directories.add("/".join(segments[:depth]))

    def _require_not_a_directory(self, name: str) -> None:
        """Refuse a name that already denotes a directory of this namespace."""
        if name in self._directories:
            raise refuse_not_a_file(name)

    def _resolve_identity(self, name: str) -> bool:
        """Return True when the exact name exists; refuse a stored name that differs only by case.

        Every segment is matched on its own, exactly as the local device matches a segment
        against a real directory entry, so both devices refuse the same names.
        """
        prefix = ""
        for segment in name.split("/"):
            entries = self._entries_at(prefix)
            if segment not in entries:
                conflict = find_case_conflict(segment, entries)
                if conflict is not None:
                    raise refuse_operation(
                        "case_collision",
                        f"File {name!r} differs only by case from the stored name {conflict!r}.",
                        file=name,
                        stored=conflict,
                    )
                return False
            prefix = f"{prefix}{segment}/"
        return name in self._files

    def _require_directory_parents(self, name: str) -> None:
        """Refuse a name whose parent segment is itself a stored file, exactly as the local device."""
        prefix = ""
        for segment in name.split("/")[:-1]:
            prefix = f"{prefix}{segment}"
            if prefix in self._files:
                raise refuse_not_a_directory(name, prefix)
            prefix = f"{prefix}/"

    def _buffer(self, name: str) -> bytearray:
        """Return the bytes of one file, refusing a name the device does not hold."""
        self._require_open()
        if not self._resolve_identity(name):
            raise refuse_missing_file(name, "open")
        return self._files[name]

    def _reserve(self, name: str, extra: int) -> None:
        """Refuse a growth that would take the device past the capacity it was given."""
        if self._capacity is None:
            return
        if self.used_bytes() + extra > self._capacity:
            raise GrafxDeviceFull(
                f"The device holds {self._capacity} bytes and cannot store {extra} more.",
                file=name,
                requested=extra,
                capacity=self._capacity,
            )

    def _page_count(self, name: str, buffer: bytearray) -> int:
        """Return how many whole pages a file holds, refusing a file that is not page aligned."""
        if len(buffer) % self._page_size:
            raise refuse_unaligned_file(name, len(buffer))
        return len(buffer) // self._page_size

    def _page_offset(self, name: str, page_index: PageIndex, buffer: bytearray, operation: str) -> int:
        """Return the byte offset of an allocated page, refusing an index that was never allocated."""
        count = self._page_count(name, buffer)
        if not isinstance(page_index, int) or isinstance(page_index, bool) or not 0 <= page_index < count:
            raise refuse_page_not_allocated(name, page_index, count, operation)
        return page_index * self._page_size


def _validate_capacity(capacity_bytes: object) -> int | None:
    """Return a usable capacity, or None when the device is only bounded by the host memory."""
    if capacity_bytes is None:
        return None
    if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int) or capacity_bytes <= 0:
        raise refuse_operation(
            "invalid_capacity",
            f"A device capacity must be a positive number of bytes, got {capacity_bytes!r}.",
            value=capacity_bytes,
        )
    return capacity_bytes
