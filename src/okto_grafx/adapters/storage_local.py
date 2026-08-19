"""The filesystem storage adapter (CONTRACT.md section 4.1, SPEC-M1 FR-5, FR-6, TR-3).

This module is one of the two places in Okto Grafx where a real file is opened, and the only
place where a platform difference between the POSIX family and the Windows family is allowed to
exist. It implements the StorageDevice port on top of a plain directory.

Four properties are structural rather than conventional.

*No positional write primitive.* Only two internal helpers ever move bytes: one appends at the
end of a file, the other overwrites a page whose offset was proved to sit entirely inside the
current end of file, and which proves afterwards that the file did not grow. There is no code
path that seeks past the end and writes, so the beyond end of file zero fill signature of the
reference engine cannot be produced by this adapter even by a caller that tries.

*Identity never depends on case (TR-3).* A logical name is matched segment by segment against
the real directory entries, so ``HEAP.DAT`` never resolves to ``heap.dat`` on a case insensitive
volume. A request that differs from a stored name only by case is refused with a typed error
instead of silently overwriting the stored file.

*A queued deletion is never keyed on a live name (A17).* Recycling frees the logical name at
once and remembers the file by an identity that cannot be reused, and the deletion pass proves
that identity again immediately before it unlinks anything. A file published later under a name
that was once recycled is therefore never destroyed by a late deletion pass.

*Nothing but a Grafx error leaves this module (TR-6).* Every OSError is classified and
translated. The translation table is deliberate and documented here:

===============================  ==========================================
observed condition               error raised
===============================  ==========================================
out of space, quota, file limit  GrafxDeviceFull (retryable)
partial append                   GrafxDeviceFull (retryable)
fsync failure                    GrafxDurabilityBarrierFailed
inadmissible or colliding name   GrafxUnsupportedOperation
grow through truncate_log        GrafxUnsupportedOperation
create over an existing file     GrafxUnsupportedOperation
use after close                  GrafxUnsupportedOperation
missing file, unallocated page   GrafxCorruptionDetected
short page, unaligned file       GrafxCorruptionDetected
sharing violation, access denied GrafxStorageError (retryable, A11-revised)
any other device failure         GrafxStorageError (retryable, A11-revised)
===============================  ==========================================

GrafxCorruptionDetected is reserved for conditions that can be attributed to damaged bytes,
because recovery turns corruption into truncation, quarantine and forensic ledger entries
(FR-8, FR-10). An antivirus holding a handle is not damage and must never manufacture an
integrity incident.

*Concurrency.* One device instance may be shared by several threads of the same process: every
public method takes the device lock, so an append or a page write is atomic against the others.
The lock says nothing about other processes, which are coordinated by the writer lease.
"""

from __future__ import annotations

import contextlib
import errno
import os
import threading
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import TypeVar

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_LOGICAL_NAME_LENGTH",
    "MAX_NAME_SEGMENT_LENGTH",
    "MAX_OPEN_FILES",
    "MAX_ALLOCATION_PAGES",
    "PENDING_DELETE_MARKER",
    "RESERVED_DEVICE_NAMES",
    "RETRY_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "MAX_RETRY_SLEEP_SECONDS",
    "MAX_PENDING_DELETE_ATTEMPTS",
    "WRITE_CHUNK_BYTES",
    "IS_WINDOWS",
    "SHARE_DELETE_AVAILABLE",
    "normalize_logical_name",
    "find_case_conflict",
    "validate_page_size",
    "validate_allocation",
    "validate_read_range",
    "as_payload",
    "refuse_operation",
    "refuse_missing_file",
    "refuse_unaligned_file",
    "refuse_page_not_allocated",
    "refuse_page_payload",
    "refuse_missing_barrier",
    "refuse_not_a_directory",
    "refuse_not_a_file",
    "LocalStorageDevice",
]

DEFAULT_PAGE_SIZE: int = 8192
"""Page size used when the caller does not state one; the same default as DatabaseConfig."""

MIN_PAGE_SIZE: int = 512
"""Smallest page this device accepts: a page must hold its 32-byte header and a payload."""

MAX_PAGE_SIZE: int = 65536
"""Largest page this device accepts: slot offsets inside a page are 16-bit."""

MAX_LOGICAL_NAME_LENGTH: int = 255
"""Longest logical name, chosen so the resulting path stays portable across both families."""

MAX_NAME_SEGMENT_LENGTH: int = 128
"""Longest single segment of a logical name, well inside the limit of every common file system."""

MAX_OPEN_FILES: int = 64
"""How many descriptors one device keeps cached before it evicts the least recently used one."""

MAX_ALLOCATION_PAGES: int = 1 << 20
"""Most pages one allocate call may add, so a wrong number cannot ask for an endless file."""

PENDING_DELETE_MARKER: str = ".pending-delete-"
"""Reserved infix of a file that was renamed out of the way while a handle still held it.

A logical name may never carry this infix, which is what makes a pending delete name an identity
that no future file can take over (A17).
"""

RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
)
"""Names Windows resolves to a character device; refused on every family so names stay portable."""

RETRY_ATTEMPTS: int = 5
"""Bounded number of tries for an operation a virus scanner or an indexer can briefly refuse."""

RETRY_BACKOFF_SECONDS: float = 0.005
"""First backoff between two tries; it doubles per try and is capped by MAX_RETRY_SLEEP_SECONDS."""

MAX_RETRY_SLEEP_SECONDS: float = 0.05
"""Longest single backoff. Five tries therefore wait at most 5+10+20+40 milliseconds in total."""

MAX_PENDING_DELETE_ATTEMPTS: int = 256
"""How many passes a deferred deletion is retried automatically before it needs an explicit ask."""

WRITE_CHUNK_BYTES: int = 1 << 20
"""Largest block written by a single system call, so a big allocation stays bounded in memory."""

IS_WINDOWS: bool = os.name == "nt"
"""True on the Windows family. The only platform switch of the engine lives in the adapters."""

_FORBIDDEN_NAME_CHARACTERS: frozenset[str] = frozenset('<>:"|?*\\')
"""Characters Windows refuses inside a file name, plus the backslash, which is never a separator."""

_DEVICE_FULL_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (
        getattr(errno, "ENOSPC", None),
        getattr(errno, "EDQUOT", None),
        getattr(errno, "EFBIG", None),
    )
    if isinstance(code, int)
)
"""Errno values that mean the device refused to grow."""

_DEVICE_FULL_WINERRORS: frozenset[int] = frozenset({39, 112})
"""ERROR_HANDLE_DISK_FULL and ERROR_DISK_FULL, which Windows reports without a useful errno."""

_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EINTR", None),
    )
    if isinstance(code, int)
)
"""Errno values a scanner, an indexer or a still open handle can produce for a moment."""

_TRANSIENT_WINERRORS: frozenset[int] = frozenset({5, 32, 33})
"""ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION."""

_BINARY_FLAG: int = getattr(os, "O_BINARY", 0)
"""O_BINARY exists only on Windows; it is zero elsewhere, which keeps the open flags uniform.

A15: a file opened in text mode on Windows stops at the first 0x1A byte, which ordinary record
payloads contain about once every 256 bytes. Every descriptor of this device is binary.
"""

_GENERIC_READ: int = 0x80000000
_GENERIC_WRITE: int = 0x40000000
_FILE_SHARE_READ: int = 0x00000001
_FILE_SHARE_WRITE: int = 0x00000002
_FILE_SHARE_DELETE: int = 0x00000004
_CREATE_NEW: int = 1
_OPEN_EXISTING: int = 3
_FILE_ATTRIBUTE_NORMAL: int = 0x00000080

_T = TypeVar("_T")


def _load_windows_opener() -> tuple[object, object, int] | None:
    """Prepare CreateFileW so this device can share delete access with the rest of the system.

    A16: the CRT opens a file without FILE_SHARE_DELETE, which makes two Okto Grafx processes
    block each other's recycling on Windows. Opening through CreateFileW with the delete share
    bit lets a holder keep reading a segment while the owner deletes it, which is exactly the
    mechanism FR-6 and AC-9 describe. Returns None on every other family, and also when the
    call cannot be prepared, in which case the device falls back to os.open.
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        library = ctypes.WinDLL("kernel32", use_last_error=True)
        library.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        library.CreateFileW.restype = wintypes.HANDLE
        library.CloseHandle.argtypes = [wintypes.HANDLE]
        library.CloseHandle.restype = wintypes.BOOL
        invalid = ctypes.c_void_p(-1).value
    except (ImportError, AttributeError, OSError, ValueError):  # pragma: no cover - hostile host
        return None
    return (library, (ctypes, msvcrt), int(invalid))


_WINDOWS_OPENER: tuple[object, object, int] | None = _load_windows_opener()
"""The prepared CreateFileW entry point, or None when this family does not need one."""

SHARE_DELETE_AVAILABLE: bool = _WINDOWS_OPENER is not None or not IS_WINDOWS
"""True when a handle of this device does not block another process from deleting the file."""


def refuse_operation(reason: str, message: str, **details: object) -> GrafxUnsupportedOperation:
    """Build the typed refusal used for an inadmissible name or an inadmissible request."""
    return GrafxUnsupportedOperation(message, reason=reason, **details)


def refuse_missing_file(file: str, operation: str) -> GrafxCorruptionDetected:
    """Build the typed failure raised when an operation names a file the device does not hold."""
    return GrafxCorruptionDetected(
        f"File {file!r} does not exist on this device, so {operation} cannot be served.",
        reason="missing_file",
        file=file,
        operation=operation,
    )


def refuse_unaligned_file(file: str, size: int) -> GrafxCorruptionDetected:
    """Build the failure raised when a paged operation meets a file that is not page aligned."""
    return GrafxCorruptionDetected(
        f"File {file!r} holds {size} bytes, which is not a whole number of pages.",
        reason="unaligned_paged_file",
        file=file,
        size=size,
    )


def refuse_page_not_allocated(
    file: str, page_index: object, page_count: int, operation: str
) -> GrafxCorruptionDetected:
    """Build the failure raised when a page index was never allocated by the device."""
    return GrafxCorruptionDetected(
        f"Page {page_index!r} of {file!r} is not allocated; the file holds {page_count} pages.",
        reason="page_not_allocated",
        file=file,
        page=page_index,
        operation=operation,
    )


def refuse_page_payload(file: str, page_index: object, actual: int, expected: int) -> GrafxCorruptionDetected:
    """Build the failure raised when a page write does not carry exactly one page of bytes."""
    return GrafxCorruptionDetected(
        f"A page write to {file!r} carried {actual} bytes instead of {expected}.",
        reason="page_size_mismatch",
        file=file,
        page=page_index,
    )


def refuse_not_a_directory(file: str, parent: str) -> GrafxUnsupportedOperation:
    """Build the refusal raised when a segment of a name is itself a stored file."""
    return refuse_operation(
        "not_a_directory",
        f"File {file!r} cannot be created because {parent!r} is a file, not a directory.",
        file=file,
        parent=parent,
    )


def refuse_not_a_file(file: str) -> GrafxUnsupportedOperation:
    """Build the refusal raised when a name already denotes a directory of the namespace."""
    return refuse_operation(
        "not_a_file",
        f"Name {file!r} already holds other files, so it cannot become a file itself.",
        file=file,
    )


def refuse_missing_barrier(file: str) -> GrafxDurabilityBarrierFailed:
    """Build the failure raised when a durability barrier names a file the device does not hold."""
    return GrafxDurabilityBarrierFailed(
        f"A durability barrier named {file!r}, which does not exist on this device.",
        reason="missing_file",
        file=file,
    )


def normalize_logical_name(file: object) -> str:
    """Validate a logical name and return it unchanged.

    A logical name is always relative and always uses the forward slash as its separator, on
    every platform. Absolute names, drive letters, parent traversal, empty segments, characters
    Windows refuses, names that resolve to a Windows character device and the reserved pending
    delete infix are all refused with GrafxUnsupportedOperation.
    """
    if not isinstance(file, str):
        raise refuse_operation(
            "not_a_string", f"A logical file name must be a string, got {type(file).__name__}."
        )
    if not file:
        raise refuse_operation("empty_name", "A logical file name must not be empty.")
    if len(file) > MAX_LOGICAL_NAME_LENGTH:
        raise refuse_operation(
            "name_too_long",
            f"A logical file name holds at most {MAX_LOGICAL_NAME_LENGTH} characters.",
            file=file,
        )
    if "\\" in file:
        raise refuse_operation(
            "backslash_separator",
            "A logical file name separates its segments with '/', never with a backslash.",
            file=file,
        )
    if file.startswith("/"):
        raise refuse_operation(
            "absolute_name", "A logical file name must be relative to the database directory.", file=file
        )
    if len(file) >= 2 and file[1] == ":":
        raise refuse_operation(
            "absolute_name", "A logical file name must not carry a drive letter.", file=file
        )
    for segment in file.split("/"):
        _validate_segment(file, segment)
    return file


def _validate_segment(file: str, segment: str) -> None:
    """Refuse one segment of a logical name that no portable file system would accept."""
    if not segment:
        raise refuse_operation(
            "empty_segment", "A logical file name must not hold an empty segment.", file=file
        )
    if segment == ".":
        raise refuse_operation(
            "relative_segment", "A logical file name must not hold a '.' segment.", file=file
        )
    if segment == "..":
        raise refuse_operation(
            "parent_traversal", "A logical file name must not climb out of the database directory.", file=file
        )
    if len(segment) > MAX_NAME_SEGMENT_LENGTH:
        raise refuse_operation(
            "segment_too_long",
            f"A segment of a logical file name holds at most {MAX_NAME_SEGMENT_LENGTH} characters.",
            file=file,
        )
    if segment != segment.strip() or segment.endswith("."):
        raise refuse_operation(
            "padded_segment",
            "A segment must not start or end with a space and must not end with a dot.",
            file=file,
        )
    for character in segment:
        if character in _FORBIDDEN_NAME_CHARACTERS or not 32 <= ord(character) < 127:
            raise refuse_operation(
                "forbidden_character",
                f"A logical file name must not hold the character {character!r}.",
                file=file,
            )
    if segment.split(".", 1)[0].upper() in RESERVED_DEVICE_NAMES:
        raise refuse_operation(
            "reserved_device_name",
            "A logical file name must not resolve to a reserved character device.",
            file=file,
        )
    if PENDING_DELETE_MARKER in segment:
        raise refuse_operation(
            "reserved_infix",
            f"A logical file name must not hold the reserved infix {PENDING_DELETE_MARKER!r}.",
            file=file,
        )


def find_case_conflict(requested: str, existing: Iterable[str]) -> str | None:
    """Return the stored name that differs from the requested one only by case, if there is one."""
    folded = requested.casefold()
    for candidate in existing:
        if candidate != requested and candidate.casefold() == folded:
            return candidate
    return None


def _is_device_full(failure: OSError) -> bool:
    """Return True when the operating system said the device refused to grow."""
    winerror = getattr(failure, "winerror", None)
    return failure.errno in _DEVICE_FULL_ERRNOS or winerror in _DEVICE_FULL_WINERRORS


def _is_transient(failure: OSError) -> bool:
    """Return True for the sharing and permission failures a retry can still win."""
    winerror = getattr(failure, "winerror", None)
    return failure.errno in _TRANSIENT_ERRNOS or winerror in _TRANSIENT_WINERRORS


def _remove_file(path: str) -> None:
    """Delete one real path. Isolated so a test can make deletion fail on any platform."""
    os.remove(path)


def _identity_of(path: str) -> tuple[int, int] | None:
    """Return the device and index pair that identifies a real file, or None when unknowable.

    On both families ``st_dev`` and ``st_ino`` name the file itself rather than the path, so a
    file replaced under the same name gets a different pair. A file system that reports no index
    yields None, and a deletion pass refuses to unlink a live name it cannot identify (A17).
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not info.st_ino:
        return None
    return (info.st_dev, info.st_ino)


class LocalStorageDevice:
    """StorageDevice backed by a real directory, with POSIX and Windows treated as equal citizens."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_open_files: int = MAX_OPEN_FILES,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_backoff_seconds: float = RETRY_BACKOFF_SECONDS,
    ) -> None:
        """Open a device over the directory, creating it when it does not exist yet."""
        self._page_size = validate_page_size(page_size)
        self._max_open_files = _validate_positive("max_open_files", max_open_files)
        self._retry_attempts = _validate_positive("retry_attempts", retry_attempts)
        self._retry_backoff = _validate_backoff(retry_backoff_seconds)
        self._root = _validate_root(root)
        self._lock = threading.RLock()
        self._handles: dict[str, int] = {}
        self._dirty: set[str] = set()
        self._deferred: dict[str, tuple[int, tuple[int, int] | None]] = {}
        self._pending_serial = 0
        self._closed = False
        try:
            os.makedirs(self._root, exist_ok=True)
        except OSError as failure:
            raise self._device_failure("open_root", self._root, failure) from failure
        self._adopt_pending_deletes()

    # --- identity -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Bounded label of this device family, safe to use as a metric label value (TR-7)."""
        return "local"

    @property
    def page_size(self) -> int:
        """Size in bytes of every page this device reads and writes."""
        return self._page_size

    @property
    def root(self) -> str:
        """Absolute path of the directory this device owns."""
        return self._root

    # --- namespace ----------------------------------------------------------------------

    def exists(self, file: str) -> bool:
        """Return True when the exact name exists; a name that differs only by case is refused."""
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            return self._resolve_identity(name)

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create an empty file. With exclusive set, an already existing file is refused."""
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            self.retry_pending_deletes()
            if self._resolve_identity(name):
                if exclusive:
                    raise refuse_operation(
                        "file_exists", f"File {name!r} already exists on this device.", file=name
                    )
                return
            self._require_directory_parents(name)
            path = self._physical_path(name)
            if os.path.isdir(path):
                raise refuse_not_a_file(name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError as failure:
                raise self._device_failure("create", name, failure) from failure
            self._forget_deferred(name)
            descriptor = self._retry("create", name, lambda: _open_descriptor(path, create_new=True))
            self._admit(name, descriptor)
            self._dirty.add(name)

    def remove(self, file: str) -> None:
        """Delete the named file. Removing a file the device does not hold is a failure."""
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            self.retry_pending_deletes()
            if not self._resolve_identity(name):
                raise refuse_missing_file(name, "remove")
            self._release(name)
            self._dirty.discard(name)
            path = self._physical_path(name)
            self._retry("remove", name, lambda: _remove_file(path))
            self._forget_deferred(name)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every logical name starting with the prefix, sorted, deferred deletions apart."""
        if not isinstance(prefix, str):
            raise refuse_operation(
                "not_a_string", f"A name prefix must be a string, got {type(prefix).__name__}."
            )
        with self._lock:
            self._require_open()
            return tuple(sorted(name for name in self._walk() if name.startswith(prefix)))

    def file_size(self, file: str) -> int:
        """Return the size of the named file in bytes."""
        name = normalize_logical_name(file)
        with self._lock:
            return self._size(name)

    def atomic_replace(self, source: str, target: str) -> None:
        """Move source onto target so a reader observes either the old or the new content."""
        source_name = normalize_logical_name(source)
        target_name = normalize_logical_name(target)
        with self._lock:
            self._require_open()
            if source_name == target_name:
                raise refuse_operation(
                    "same_file", "atomic_replace needs two different names.", file=source_name
                )
            if not self._resolve_identity(source_name):
                raise refuse_missing_file(source_name, "atomic_replace")
            # The answer is discarded on purpose: replacing a missing target is legal, replacing
            # a target that differs from the asked name only by case is not.
            self._resolve_identity(target_name)
            self._require_directory_parents(target_name)
            self._release(source_name)
            self._release(target_name)
            source_path = self._physical_path(source_name)
            target_path = self._physical_path(target_name)
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
            except OSError as failure:
                raise self._device_failure("atomic_replace", target_name, failure) from failure
            # os.replace is the only move that overwrites an existing target on both families;
            # os.rename fails on Windows the moment the target exists.
            self._retry("atomic_replace", target_name, lambda: os.replace(source_path, target_path))
            self._dirty.discard(source_name)
            self._dirty.add(target_name)
            # Any deletion still queued for either name refers to a file that is now gone or
            # replaced: dropping it here is what keeps a late pass from destroying live data.
            self._forget_deferred(source_name)
            self._forget_deferred(target_name)

    def recycle(self, file: str) -> bool:
        """Release a file, deferring the deletion when the platform cannot complete it now.

        The logical name leaves the namespace before this call returns (A17): POSIX unlinks the
        file, and Windows renames it to a reserved pending delete name first, so a later create
        under the same name always succeeds. The answer is True when the space is already back
        and False when the platform still holds it, in which case a later pass reclaims it.

        This never raises because somebody holds a handle. When the holder denies both deletion
        and rename, which only a handle opened without delete sharing can do, the file keeps its
        name, the answer is False and the queued deletion is keyed on the identity of that exact
        file, so a file published later under the same name is never destroyed.
        """
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            self.retry_pending_deletes()
            if not self._resolve_identity(name):
                # Recycling is a reclamation loop and must converge: a name that is already gone
                # has already been reclaimed. remove() is the strict door for the same effect.
                return True
            self._release(name)
            self._dirty.discard(name)
            path = self._physical_path(name)
            identity = _identity_of(path)
            if not IS_WINDOWS:
                try:
                    _remove_file(path)
                except OSError:
                    return self._defer(path, identity)
                self._forget_deferred(name)
                return True
            return self._recycle_on_windows(name, path, identity)

    def pending_deletes(self) -> tuple[str, ...]:
        """Return the device relative names whose deletion the platform deferred, sorted."""
        with self._lock:
            return tuple(sorted(self._deferred))

    def retry_pending_deletes(self, *, force: bool = False) -> int:
        """Try every deferred deletion once and return how many files were finally reclaimed.

        One pass costs a couple of system calls per deferred file and never sleeps, so it can be
        driven from any path; create, remove, recycle and close all drive it, and a caller that
        recycles only once still gets its space back. A file that survived
        MAX_PENDING_DELETE_ATTEMPTS passes is left alone until the caller asks with force, which
        is the bounded attempt rule of FR-6; pending_deletes() keeps reporting it either way.

        Nothing is unlinked before its identity is proved again (A17): a pending delete name can
        never belong to a live file, and a deferral that still carries a live name is only acted
        on when the file at that name is still the very file that was recycled.
        """
        with self._lock:
            if not self._deferred:
                return 0
            reclaimed = 0
            for relative, state in tuple(self._deferred.items()):
                attempts, identity = state
                if attempts >= MAX_PENDING_DELETE_ATTEMPTS and not force:
                    continue
                path = os.path.join(self._root, *relative.split("/"))
                if not self._entry_present(path):
                    del self._deferred[relative]
                    reclaimed += 1
                    continue
                if not _may_unlink(relative, identity, path):
                    self._deferred[relative] = (attempts + 1, identity)
                    continue
                try:
                    _remove_file(path)
                except FileNotFoundError:
                    del self._deferred[relative]
                    reclaimed += 1
                except OSError:
                    self._deferred[relative] = (attempts + 1, identity)
                else:
                    if self._entry_present(path):
                        # The platform armed its own pending delete: the entry disappears when
                        # the last handle closes, and the next pass will see it gone.
                        self._deferred[relative] = (attempts + 1, identity)
                    else:
                        del self._deferred[relative]
                        reclaimed += 1
            return reclaimed

    # --- paged space --------------------------------------------------------------------

    def page_count(self, file: str) -> int:
        """Return how many whole pages the named file currently holds."""
        name = normalize_logical_name(file)
        with self._lock:
            return self._page_count(name, self._size(name))

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by count zero filled pages and return the first new page index."""
        name = normalize_logical_name(file)
        validate_allocation(name, count)
        with self._lock:
            descriptor = self._descriptor(name)
            size = self._descriptor_size(name, descriptor)
            first = self._page_count(name, size)
            remaining = count * self._page_size
            written = 0
            while written < remaining:
                block = min(WRITE_CHUNK_BYTES, remaining - written)
                self._append(name, descriptor, bytes(block), rollback_to=size)
                written += block
            return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return exactly page_size bytes from an already allocated page."""
        name = normalize_logical_name(file)
        with self._lock:
            descriptor = self._descriptor(name)
            size = self._descriptor_size(name, descriptor)
            offset = self._page_offset(name, page_index, size, "read_page")
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                data = _read_exactly(descriptor, self._page_size)
            except OSError as failure:
                raise self._device_failure("read_page", name, failure, page=page_index) from failure
            if len(data) != self._page_size:
                raise GrafxCorruptionDetected(
                    f"Page {page_index} of {name!r} ended after {len(data)} bytes.",
                    reason="short_page",
                    file=name,
                    page=page_index,
                )
            return data

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Overwrite one already allocated page; the payload must be exactly page_size bytes."""
        name = normalize_logical_name(file)
        payload = as_payload(name, data)
        if len(payload) != self._page_size:
            raise refuse_page_payload(name, page_index, len(payload), self._page_size)
        with self._lock:
            descriptor = self._descriptor(name)
            size = self._descriptor_size(name, descriptor)
            offset = self._page_offset(name, page_index, size, "write_page")
            # The offset was proved to sit entirely inside the current end of file, so this write
            # cannot extend the file. It is the only overwrite in the adapter.
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                written = _write_everything(descriptor, payload)
            except OSError as failure:
                raise self._device_failure("write_page", name, failure, page=page_index) from failure
            self._dirty.add(name)
            if written != len(payload):
                raise GrafxDeviceFull(
                    f"A page write to {name!r} stored {written} of {len(payload)} bytes.",
                    file=name,
                    page=page_index,
                )
            self._prove_no_growth(name, descriptor, page_index, size)

    # --- append-only log space ----------------------------------------------------------

    def append_log(self, file: str, payload: bytes) -> int:
        """Append the payload and return the new total size; a partial append is a device full."""
        name = normalize_logical_name(file)
        data = as_payload(name, payload)
        with self._lock:
            descriptor = self._descriptor(name)
            size = self._descriptor_size(name, descriptor)
            if not data:
                return size
            return self._append(name, descriptor, data, rollback_to=size)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return up to length bytes starting at offset; a read past the end returns what is there."""
        name = normalize_logical_name(file)
        validate_read_range(name, offset, length)
        with self._lock:
            descriptor = self._descriptor(name)
            if length == 0:
                return b""
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                return _read_exactly(descriptor, length)
            except OSError as failure:
                raise self._device_failure("read_log", name, failure, offset=offset) from failure

    def log_size(self, file: str) -> int:
        """Return the current size in bytes of an append-only file."""
        name = normalize_logical_name(file)
        with self._lock:
            return self._size(name)

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink the file to size. Growing through this door is refused."""
        name = normalize_logical_name(file)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise refuse_operation(
                "invalid_size", "truncate_log needs a size of zero or more.", file=name, size=size
            )
        with self._lock:
            descriptor = self._descriptor(name)
            current = self._descriptor_size(name, descriptor)
            if size > current:
                raise refuse_operation(
                    "truncate_would_grow",
                    f"truncate_log only shrinks; {name!r} holds {current} bytes and {size} was asked.",
                    file=name,
                    size=size,
                    current=current,
                )
            try:
                os.ftruncate(descriptor, size)
            except OSError as failure:
                raise self._device_failure("truncate_log", name, failure) from failure
            self._dirty.add(name)

    # --- durability ---------------------------------------------------------------------

    def durable_barrier(self, file: str | None = None) -> None:
        """Flush the file to stable storage; with None every file with unflushed writes is flushed.

        A file is remembered as unflushed the moment it is written, whether or not its descriptor
        is still cached, so an eviction from the descriptor cache can never let a write escape a
        barrier that names no file.

        On POSIX the directory that holds the file is flushed as well, because a file that was
        just created is only reachable after its directory entry is itself durable. Windows has
        no directory handle to flush and does not need one, which is the single platform branch
        of the durability path (TR-3).
        """
        with self._lock:
            self._require_open()
            if file is None:
                names = tuple(sorted(self._dirty | set(self._handles)))
            else:
                names = (normalize_logical_name(file),)
            for name in names:
                try:
                    descriptor = self._descriptor(name)
                except GrafxCorruptionDetected as failure:
                    raise refuse_missing_barrier(name) from failure
                try:
                    os.fsync(descriptor)
                except OSError as failure:
                    raise GrafxDurabilityBarrierFailed(
                        f"The durability barrier of {name!r} did not complete.",
                        reason="fsync_failed",
                        file=name,
                        errno=failure.errno,
                    ) from failure
            if not IS_WINDOWS:
                self._synchronize_directories(names)
            self._dirty.difference_update(names)

    # --- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        """Close every cached descriptor. A closed device refuses further work."""
        with self._lock:
            if not self._closed:
                # Closing our own handles is often what lets a queued deletion finally complete.
                with contextlib.suppress(GrafxUnsupportedOperation):
                    self.retry_pending_deletes()
            for descriptor in self._handles.values():
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            self._handles.clear()
            self._closed = True
            with contextlib.suppress(OSError):
                self._sweep_pending_deletes()

    def __enter__(self) -> LocalStorageDevice:
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

    def _sweep_pending_deletes(self) -> None:
        """Try the queued deletions once more after the descriptors of this device are gone."""
        for relative, state in tuple(self._deferred.items()):
            path = os.path.join(self._root, *relative.split("/"))
            if not self._entry_present(path):
                del self._deferred[relative]
                continue
            if not _may_unlink(relative, state[1], path):
                continue
            with contextlib.suppress(OSError):
                _remove_file(path)
                if not self._entry_present(path):
                    del self._deferred[relative]

    def _recycle_on_windows(self, name: str, path: str, identity: tuple[int, int] | None) -> bool:
        """Free the logical name first, then delete the file behind it (FR-6, AC-9, A17).

        Renaming before deleting is what makes the name free at once: a Windows deletion of a
        held file only arms a pending delete, and the directory entry stays until the last
        handle closes, which would make a later create under the same name fail.
        """
        pending = self._pending_path(path)
        try:
            os.replace(path, pending)
        except OSError:
            # A holder that denies delete sharing refuses the rename as well; the direct
            # deletion is the only thing left to try.
            try:
                _remove_file(path)
            except OSError:
                return self._defer(path, identity)
            if self._entry_present(path):
                return self._defer(path, identity)
            self._forget_deferred(name)
            return True
        self._forget_deferred(name)
        try:
            _remove_file(pending)
        except OSError:
            return self._defer(pending, identity)
        if self._entry_present(pending):
            return self._defer(pending, identity)
        return True

    def _pending_path(self, path: str) -> str:
        """Return an unused reserved name for a file whose deletion has to wait."""
        while True:
            self._pending_serial += 1
            candidate = f"{path}{PENDING_DELETE_MARKER}{self._pending_serial}"
            if not os.path.lexists(candidate):
                return candidate

    def _defer(self, path: str, identity: tuple[int, int] | None) -> bool:
        """Queue one real path for a later deletion pass and report the deferral."""
        relative = self._relative(path)
        attempts = self._deferred.get(relative, (0, None))[0]
        self._deferred[relative] = (attempts, identity if identity is not None else _identity_of(path))
        return False

    def _forget_deferred(self, name: str) -> None:
        """Drop a queued deletion that was keyed on a logical name which is now live again."""
        self._deferred.pop(name, None)

    def _entry_present(self, path: str) -> bool:
        """Return True when the real directory still lists this exact entry."""
        parent = os.path.dirname(path)
        base = os.path.basename(path)
        try:
            return base in os.listdir(parent)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True

    def _require_open(self) -> None:
        """Refuse any use of a device whose descriptors were already released."""
        if self._closed:
            raise refuse_operation("device_closed", "This storage device is closed.")

    def _physical_path(self, name: str) -> str:
        """Translate a logical name into the real path it denotes inside the database directory."""
        return os.path.join(self._root, *name.split("/"))

    def _require_directory_parents(self, name: str) -> None:
        """Refuse a name whose parent segment is itself a stored file, on both families alike."""
        current = self._root
        for segment in name.split("/")[:-1]:
            current = os.path.join(current, segment)
            if os.path.isfile(current):
                raise refuse_not_a_directory(name, self._relative(current))

    def _adopt_pending_deletes(self) -> None:
        """Take over the deferred deletions a previous run left behind, and keep serials unique."""
        for directory, _, files in os.walk(self._root):
            relative = os.path.relpath(directory, self._root)
            prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
            for entry in files:
                marker = entry.rfind(PENDING_DELETE_MARKER)
                if marker < 0:
                    continue
                name = prefix + entry
                self._deferred.setdefault(name, (0, _identity_of(os.path.join(directory, entry))))
                tail = entry[marker + len(PENDING_DELETE_MARKER) :]
                if tail.isdigit():
                    self._pending_serial = max(self._pending_serial, int(tail))

    def _entries(self, directory: str, name: str) -> tuple[str, ...]:
        """Return the real entries of one directory, empty when the directory is not there."""
        try:
            return tuple(os.listdir(directory))
        except (FileNotFoundError, NotADirectoryError):
            return ()
        except OSError as failure:
            raise self._device_failure("list", name, failure) from failure

    def _resolve_identity(self, name: str) -> bool:
        """Return True when the exact name exists; refuse a stored name that differs only by case.

        Every segment is matched against the real directory entry, so a case insensitive volume
        cannot make ``Wal/x.wal`` resolve to ``wal/x.wal`` behind the back of the engine. The
        answer is True only for a regular file: a directory is not part of the namespace.
        """
        current = self._root
        for segment in name.split("/"):
            entries = self._entries(current, name)
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
            current = os.path.join(current, segment)
        return os.path.isfile(current)

    def _walk(self) -> Iterable[str]:
        """Yield every logical name held by the device, deferred deletions excluded."""
        for directory, _, files in os.walk(self._root):
            relative = os.path.relpath(directory, self._root)
            prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
            for entry in files:
                if PENDING_DELETE_MARKER in entry:
                    continue
                yield prefix + entry

    def _descriptor(self, name: str) -> int:
        """Return the cached descriptor of a file, opening and admitting it when it is not cached."""
        self._require_open()
        cached = self._handles.pop(name, None)
        if cached is not None:
            self._handles[name] = cached
            return cached
        if not self._resolve_identity(name):
            raise refuse_missing_file(name, "open")
        path = self._physical_path(name)
        descriptor = self._retry("open", name, lambda: _open_descriptor(path, create_new=False))
        self._admit(name, descriptor)
        return descriptor

    def _admit(self, name: str, descriptor: int) -> None:
        """Cache one descriptor, evicting the least recently used one when the cache is full."""
        self._handles[name] = descriptor
        while len(self._handles) > self._max_open_files:
            oldest = next(iter(self._handles))
            self._release(oldest)

    def _release(self, name: str) -> None:
        """Close and forget the cached descriptor of one file, if there is one.

        Nothing is buffered in user space: every write reaches the operating system through a
        single system call, so releasing a descriptor can never lose a byte. What a barrier
        still owes the file is remembered in the unflushed set, not in the descriptor cache.
        """
        descriptor = self._handles.pop(name, None)
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _size(self, name: str) -> int:
        """Return the current size of a file in bytes."""
        descriptor = self._descriptor(name)
        return self._descriptor_size(name, descriptor)

    def _descriptor_size(self, name: str, descriptor: int) -> int:
        """Return the size the operating system reports for an open descriptor."""
        try:
            return os.fstat(descriptor).st_size
        except OSError as failure:
            raise self._device_failure("size", name, failure) from failure

    def _page_count(self, name: str, size: int) -> int:
        """Return how many whole pages a file holds, refusing a file that is not page aligned."""
        if size % self._page_size:
            raise refuse_unaligned_file(name, size)
        return size // self._page_size

    def _page_offset(self, name: str, page_index: PageIndex, size: int, operation: str) -> int:
        """Return the byte offset of an allocated page, refusing an index that was never allocated."""
        count = self._page_count(name, size)
        if not isinstance(page_index, int) or isinstance(page_index, bool) or not 0 <= page_index < count:
            raise refuse_page_not_allocated(name, page_index, count, operation)
        return page_index * self._page_size

    def _prove_no_growth(self, name: str, descriptor: int, page_index: PageIndex, size: int) -> None:
        """Refuse to leave a page write that made the file longer than it was.

        The offset was already proved to sit inside the file, so this can only happen when
        something outside the single writer rule shrank the file between the two calls. The
        extension this write would leave behind is exactly the hole FR-5 forbids, so it is cut
        off again and the damage is reported instead of being kept.
        """
        after = self._descriptor_size(name, descriptor)
        if after <= size:
            return
        self._undo_append(descriptor, size)
        raise GrafxCorruptionDetected(
            f"A page write to {name!r} would have grown the file from {size} to {after} bytes.",
            reason="page_write_grew_file",
            file=name,
            page=page_index,
            size=size,
            observed=after,
        )

    def _append(self, name: str, descriptor: int, payload: bytes, *, rollback_to: int) -> int:
        """Append bytes at the end of a file and return the new size, or undo and refuse."""
        try:
            os.lseek(descriptor, 0, os.SEEK_END)
            written = _write_everything(descriptor, payload)
        except OSError as failure:
            self._undo_append(descriptor, rollback_to)
            raise self._device_failure("append", name, failure) from failure
        self._dirty.add(name)
        if written != len(payload):
            self._undo_append(descriptor, rollback_to)
            raise GrafxDeviceFull(
                f"An append to {name!r} stored {written} of {len(payload)} bytes.",
                file=name,
                requested=len(payload),
                stored=written,
            )
        return self._descriptor_size(name, descriptor)

    def _undo_append(self, descriptor: int, size: int) -> None:
        """Cut a partial append back off, so a refused append never leaves a fragment behind."""
        with contextlib.suppress(OSError):
            os.ftruncate(descriptor, size)

    def _relative(self, path: str) -> str:
        """Return the device relative form of a real path, using the logical separator."""
        return os.path.relpath(path, self._root).replace(os.sep, "/")

    def _synchronize_directories(self, names: tuple[str, ...]) -> None:
        """Flush every directory that holds one of the named files. POSIX only."""
        directories = {os.path.dirname(self._physical_path(name)) for name in names}
        directories.add(self._root)
        for directory in sorted(directories):
            try:
                descriptor = os.open(directory, os.O_RDONLY)
            except OSError as failure:
                raise GrafxDurabilityBarrierFailed(
                    "A durability barrier could not open the directory that holds the file.",
                    reason="directory_open_failed",
                    errno=failure.errno,
                ) from failure
            try:
                os.fsync(descriptor)
            except OSError as failure:
                raise GrafxDurabilityBarrierFailed(
                    "A durability barrier could not flush the directory that holds the file.",
                    reason="directory_fsync_failed",
                    errno=failure.errno,
                ) from failure
            finally:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _retry(self, operation: str, name: str, action: Callable[[], _T]) -> _T:
        """Run an operation a bounded number of times while the platform refuses it briefly.

        A virus scanner or a search indexer holds a handle for a few milliseconds and makes a
        create, an open, a replace or a delete fail with a sharing violation. The wait doubles
        from RETRY_BACKOFF_SECONDS and is capped by MAX_RETRY_SLEEP_SECONDS; after
        RETRY_ATTEMPTS tries the failure is translated and raised.
        """
        backoff = self._retry_backoff
        for attempt in range(1, self._retry_attempts + 1):
            try:
                return action()
            except OSError as failure:
                if attempt == self._retry_attempts or not _is_transient(failure):
                    raise self._device_failure(operation, name, failure, attempts=attempt) from failure
                time.sleep(min(backoff, MAX_RETRY_SLEEP_SECONDS))
                backoff *= 2.0
        raise self._exhausted(operation, name)

    def _exhausted(self, operation: str, name: str) -> GrafxStorageError:
        """Build the failure for a retry loop that ended without a result and without an error."""
        return GrafxStorageError(
            f"The operation {operation} on {name!r} did not complete within the retry budget.",
            retryable=True,
            reason="retry_budget_exhausted",
            file=name,
            operation=operation,
            attempts=self._retry_attempts,
        )

    def _device_failure(
        self, operation: str, name: str, failure: OSError, **details: object
    ) -> GrafxDeviceFull | GrafxStorageError:
        """Translate an operating system failure into the typed error of the taxonomy.

        A11-revised: only damaged bytes are corruption. A sharing violation, a permission
        failure or any other access failure is a retryable storage error, because reporting it
        as corruption would send recovery into truncation, quarantine and a forensic ledger
        entry for a file whose bytes are perfect.
        """
        if _is_device_full(failure):
            return GrafxDeviceFull(
                f"The device refused {operation} on {name!r} because it is full.",
                file=name,
                operation=operation,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                **details,
            )
        details.setdefault("attempts", 1)
        return GrafxStorageError(
            f"The device failed {operation} on {name!r}: {failure.strerror or 'unspecified failure'}.",
            retryable=True,
            reason="access_failed",
            file=name,
            operation=operation,
            errno=failure.errno,
            winerror=getattr(failure, "winerror", None),
            **details,
        )


def _may_unlink(relative: str, identity: tuple[int, int] | None, path: str) -> bool:
    """Return True when this exact file may still be unlinked by a deletion pass (A17).

    A pending delete name can never belong to a live file, because no logical name may carry
    the reserved infix. Any other name has to prove that the file behind it is still the very
    file that was recycled; a file replaced in the meantime has a different identity and is left
    alone, which is what keeps a late pass from destroying published data.
    """
    if PENDING_DELETE_MARKER in relative:
        return True
    if identity is None:
        return False
    return _identity_of(path) == identity


def _open_descriptor(path: str, *, create_new: bool) -> int:
    """Open a binary read and write descriptor, sharing delete access on Windows (A16, A15)."""
    if _WINDOWS_OPENER is None:
        flags = os.O_RDWR | _BINARY_FLAG | (os.O_CREAT | os.O_EXCL if create_new else 0)
        return os.open(path, flags)
    library, modules, invalid = _WINDOWS_OPENER
    ctypes_module, msvcrt_module = modules  # type: ignore[misc]
    handle = library.CreateFileW(  # type: ignore[attr-defined]
        path,
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _CREATE_NEW if create_new else _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == invalid:
        raise ctypes_module.WinError(ctypes_module.get_last_error())
    try:
        return msvcrt_module.open_osfhandle(handle, _BINARY_FLAG)
    except OSError:
        library.CloseHandle(handle)  # type: ignore[attr-defined]
        raise


def validate_allocation(file: str, count: object) -> int:
    """Return a usable page count, refusing zero, a negative number and an endless request."""
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise refuse_operation(
            "invalid_page_count", "allocate needs a count of one page or more.", file=file, count=count
        )
    if count > MAX_ALLOCATION_PAGES:
        raise refuse_operation(
            "invalid_page_count",
            f"allocate adds at most {MAX_ALLOCATION_PAGES} pages in one call.",
            file=file,
            count=count,
        )
    return count


def _validate_root(root: object) -> str:
    """Return the absolute path of the directory the device owns, refusing anything unusable."""
    try:
        return os.path.abspath(os.fspath(root))  # type: ignore[arg-type]
    except (TypeError, ValueError) as failure:
        raise GrafxConfigurationError(
            f"Invalid configuration for 'root': a usable directory path is required. Got {root!r}.",
            field="root",
            value=repr(root),
        ) from failure


def validate_page_size(page_size: object) -> int:
    """Return a usable page size, refusing anything the on-disk format cannot carry."""
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise GrafxConfigurationError(
            f"Invalid configuration for 'page_size': an integer is required. Got {page_size!r}.",
            field="page_size",
            value=page_size,
        )
    if page_size & (page_size - 1) or not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE:
        raise GrafxConfigurationError(
            f"Invalid configuration for 'page_size': a power of two between {MIN_PAGE_SIZE} and "
            f"{MAX_PAGE_SIZE} is required. Got {page_size!r}.",
            field="page_size",
            value=page_size,
        )
    return page_size


def _validate_positive(field: str, value: object) -> int:
    """Return a strictly positive integer parameter or refuse the construction of the device."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GrafxConfigurationError(
            f"Invalid configuration for {field!r}: a value greater than zero is required. Got {value!r}.",
            field=field,
            value=value,
        )
    return value


def _validate_backoff(value: object) -> float:
    """Return a usable first backoff or refuse the construction of the device."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise GrafxConfigurationError(
            f"Invalid configuration for 'retry_backoff_seconds': a value of zero or more is "
            f"required. Got {value!r}.",
            field="retry_backoff_seconds",
            value=value,
        )
    return min(float(value), MAX_RETRY_SLEEP_SECONDS)


def as_payload(file: str, payload: object) -> bytes:
    """Return the payload as bytes, refusing anything that is not a byte sequence."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    raise refuse_operation(
        "not_a_payload",
        f"A write to {file!r} needs bytes, got {type(payload).__name__}.",
        file=file,
    )


def validate_read_range(file: str, offset: object, length: object) -> None:
    """Refuse a read whose offset or length is not a natural number."""
    for field, value in (("offset", offset), ("length", length)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise refuse_operation(
                "invalid_range",
                f"A read of {file!r} needs a {field} of zero or more, got {value!r}.",
                file=file,
                field=field,
                value=value,
            )


def _write_everything(descriptor: int, payload: bytes) -> int:
    """Write as many bytes as the device accepts and return how many actually reached it."""
    view = memoryview(payload)
    total = 0
    while total < len(view):
        written = os.write(descriptor, view[total:])
        if written <= 0:
            break
        total += written
    return total


def _read_exactly(descriptor: int, length: int) -> bytes:
    """Read up to length bytes, stopping early only at the real end of the file."""
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
