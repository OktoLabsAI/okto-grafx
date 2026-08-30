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

The page size vocabulary belongs to C1 (A24): this module imports MIN_PAGE_SIZE, MAX_PAGE_SIZE
and validate_page_size from okto_grafx.domain.page instead of stating bounds of its own.

*Identity never depends on case (TR-3).* A logical name is matched segment by segment against
the real directory entries, so ``HEAP.DAT`` never resolves to ``heap.dat`` on a case insensitive
volume. A request that differs from a stored name only by case is refused with a typed error
instead of silently overwriting the stored file.

*A queued deletion is never keyed on a live name (A27).* Recycling frees the logical name, and a
deletion is queued only under the reserved pending delete name the file was moved to, which no
logical name may ever carry. When the name cannot be freed at all, the device queues nothing and
says so, because it has no reliable way to notice later that somebody re-claimed the name: an
identity stamp was measured and cannot see a size preserving page write, since the modification
time of a file only advances once per tick. A file published later under a name that was once
recycled is therefore never destroyed by a late deletion pass.

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
import stat
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
    GrafxError,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
# A24: the page size vocabulary has exactly one owner. The bounds are imported rather than
# restated, and MIN_PAGE_SIZE and MAX_PAGE_SIZE are carried here only so a reader of this module
# resolves them to the very objects C1 defines, never to a second opinion.
from okto_grafx.domain.page import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE as MAX_PAGE_SIZE,
    MIN_PAGE_SIZE as MIN_PAGE_SIZE,
    validate_page_size,
)

__all__ = [
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
    "validate_allocation",
    "validate_read_range",
    "as_payload",
    "refuse_operation",
    "refuse_missing_file",
    "refuse_unaligned_file",
    "refuse_page_not_allocated",
    "refuse_page_payload",
    "barrier_failure",
    "barrier_failure_from",
    "refuse_not_a_directory",
    "refuse_not_a_file",
    "LocalStorageDevice",
]

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
that no future file can take over (A17, A27).
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

REALPATH_STABILITY_ATTEMPTS: int = 8
"""Bounded containment probes when Windows exposes an unlinked NTFS replacement target."""

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

_PERMANENT_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (
        getattr(errno, "EEXIST", None),
        getattr(errno, "ENOTDIR", None),
        getattr(errno, "EISDIR", None),
        getattr(errno, "ENAMETOOLONG", None),
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOENT", None),
        getattr(errno, "EBADF", None),
        getattr(errno, "EROFS", None),
        getattr(errno, "ELOOP", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "ENODEV", None),
        getattr(errno, "ENXIO", None),
    )
    if isinstance(code, int)
)
"""Conditions that cannot clear on their own, so a caller reading ``retryable`` must not spin.

The transient table above answers "can a scanner be holding this for a moment?"; this one answers
the other half of the same question, per A66.1: **can this condition clear if the caller simply
waits?** A regular file where a directory belongs, a name that is too long, a bad descriptor, a
read-only volume: none of these resolve by retrying, and A47 tells callers to retry whatever says
it is retryable. Anything in neither table keeps the retryable default, which is the safe answer
for a condition nobody has classified -- ``EMFILE`` and ``ENFILE`` really do clear as other
handles close, and an unexplained ``EIO`` may be a transient bus error.
"""

_PERMANENT_WINERRORS: frozenset[int] = frozenset({2, 3, 80, 87, 123, 161, 183, 206, 267})
"""FILE_NOT_FOUND, PATH_NOT_FOUND, FILE_EXISTS, INVALID_PARAMETER, INVALID_NAME, BAD_PATHNAME,
ALREADY_EXISTS, FILENAME_EXCED_RANGE and DIRECTORY.

Measured on this build: CPython derives ``errno`` from ``winerror``, and every number here maps
to one already listed above -- 183 and 80 to EEXIST, 2, 3, 161 and 206 to ENOENT, 87 and 123 to
EINVAL, 267 to ENOTDIR. So this set classifies nothing the errno set does not, and it exists to
keep the answer right if that mapping ever changes. It is documentation of intent with a guard
attached, not a load-bearing branch, and the punch list says so."""

_BINARY_FLAG: int = getattr(os, "O_BINARY", 0)
"""O_BINARY exists only on Windows; it is zero elsewhere, which keeps the open flags uniform.

A15: a file opened in text mode on Windows stops at the first 0x1A byte, which ordinary record
payloads contain about once every 256 bytes. Every descriptor of this device is binary.
"""

_DELETE_ACCESS: int = 0x00010000
_FILE_RENAME_INFO_EX: int = 22
_RENAME_REPLACE_IF_EXISTS: int = 0x00000001
_RENAME_POSIX_SEMANTICS: int = 0x00000002
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

    The same entry point also prepares SetFileInformationByHandle, which is what CF-5 needs to
    publish over a file another participant is holding open.
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
        library.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        library.SetFileInformationByHandle.restype = wintypes.BOOL
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


def barrier_failure(message: str, *, reason: str, retryable: bool = False, **details: object) -> GrafxDurabilityBarrierFailed:
    """Build the one failure type a durability barrier is allowed to raise (A28).

    The access classification of A11-revised travels in ``details`` rather than in the class,
    because the caller counts exactly this type into ``oktografx_barrier_failures_total``: an
    access failure that escaped as another class would keep that metric silent for the very
    case A11-revised was written about.
    """
    failure = GrafxDurabilityBarrierFailed(message, reason=reason, **details)
    failure.details["retryable"] = retryable
    return failure


def barrier_failure_from(failure: GrafxError) -> GrafxDurabilityBarrierFailed:
    """Convert any failure met while serving a barrier into the type that door must raise."""
    return barrier_failure(
        f"A durability barrier could not be served: {failure.message}",
        reason=str(failure.details.get("reason", failure.code)),
        retryable=failure.retryable,
        file=failure.details.get("file"),
        errno=failure.details.get("errno"),
        winerror=failure.details.get("winerror"),
        attempts=failure.details.get("attempts", 1),
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
    if _is_pending_delete(segment):
        raise refuse_operation(
            "reserved_infix",
            f"A logical file name must not hold the reserved infix {PENDING_DELETE_MARKER!r}.",
            file=file,
        )


def _is_pending_delete(entry: str) -> bool:
    """Return True when a real name belongs to the reserved pending delete space.

    The comparison ignores case, because a case insensitive volume would otherwise let a
    logical name shadow a queued deletion by spelling the reserved infix differently.
    """
    return PENDING_DELETE_MARKER in entry.lower()


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


def _is_permanent(failure: OSError) -> bool:
    """Return True when waiting cannot change the answer, so the caller must not retry.

    There is no precedence rule here because the two classifications cannot both match: the
    transient and permanent sets are disjoint, which
    test_the_two_classifications_of_a_failure_cannot_both_claim_it pins so that an editor who
    lists a number in both is told at once rather than falling into whichever branch came first.
    """
    winerror = getattr(failure, "winerror", None)
    return failure.errno in _PERMANENT_ERRNOS or winerror in _PERMANENT_WINERRORS


def _remove_file(path: str) -> None:
    """Delete one real path. Isolated so a test can make deletion fail on any platform."""
    os.remove(path)


def _is_queueable(relative: str) -> bool:
    """Return True only for a name a deletion may be queued under (A27).

    A deferred deletion may only ever be keyed on a name no logical name can collide with, so
    the queue holds pending delete names and nothing else. Identity stamps were tried and do not
    work: a size preserving page write leaves device, index, size and modification time
    unchanged on NTFS, because the timestamp only advances once per tick.
    """
    return _is_pending_delete(relative)


def _deletion_namespace(name: str) -> str:
    """Return the top-level namespace an automatic pending-delete retry may affect.

    Root files form one data namespace (the empty string); reserved directories such as
    ``control``, ``wal`` and ``index`` remain independent. A reader-registration create under
    ``control/`` can therefore reclaim control debris without deleting queued database/WAL/index
    evidence merely because the same raw device serves both planes.
    """
    head, separator, _tail = name.partition("/")
    return head if separator else ""


def _missing_directory_chain(path: str) -> tuple[str, ...]:
    """Return missing directories from the first absent ancestor through ``path``.

    The snapshot is taken before ``makedirs``.  Only these names can create namespace debt for
    their parents; a root that already existed when the adapter opened must not make every
    later barrier fsync a directory the adapter did not publish.
    """
    missing: list[str] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    missing.reverse()
    return tuple(missing)


def _is_redirected_path(path: str, information: os.stat_result | None = None) -> bool:
    """Return True for a symlink, junction or any Windows reparse-point component."""
    try:
        details = os.lstat(path) if information is None else information
    except OSError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction):
        with contextlib.suppress(OSError):
            return bool(is_junction(path))
    return False


def _comparable_real_path(path: str) -> str:
    """Return one stable spelling for containment comparisons.

    Windows ``realpath`` obtains a ``\\?\\`` final path and normally verifies the ordinary
    spelling before removing that prefix.  An atomic replacement between those two system calls
    can make the verification fail even though both spellings name the same path, leaving the
    prefix in only one operand of ``commonpath``.  That transient representation difference is
    not a path escape.  Canonical DOS-drive and UNC spellings are therefore compared without the
    extended-length prefix; device namespaces such as ``\\?\\GLOBALROOT`` remain untouched and
    still fail closed against an ordinary database root.
    """
    resolved = os.path.realpath(path)
    if IS_WINDOWS:
        folded = resolved.casefold()
        if folded.startswith("\\\\?\\unc\\"):
            resolved = "\\\\" + resolved[8:]
        elif folded.startswith("\\\\?\\"):
            candidate = resolved[4:]
            drive, tail = os.path.splitdrive(candidate)
            if len(drive) == 2 and drive[1] == ":" and tail.startswith(("\\", "/")):
                resolved = candidate
    return os.path.normcase(os.path.normpath(resolved))


def _is_ntfs_deleted_real_path(path: str) -> bool:
    """Return whether ``path`` is NTFS's private name for a just-unlinked open file."""
    drive, tail = os.path.splitdrive(os.path.normcase(path))
    components = tail.lstrip("\\/").replace("/", "\\").split("\\")
    return (
        len(drive) == 2
        and drive[1] == ":"
        and len(components) >= 2
        and components[0] == "$extend"
        and components[1] == "$deleted"
    )


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
        create_root: bool = True,
    ) -> None:
        """Open a device over the directory, optionally refusing an absent root."""
        self._page_size = validate_page_size(page_size)
        self._max_open_files = _validate_positive("max_open_files", max_open_files)
        self._retry_attempts = _validate_positive("retry_attempts", retry_attempts)
        self._retry_backoff = _validate_backoff(retry_backoff_seconds)
        self._root = _validate_root(root)
        self._lock = threading.RLock()
        self._handles: dict[str, int] = {}
        # The proved physical path each cached descriptor was opened through, so that a warm
        # hit checks the entry's identity without proving the whole chain again (A63 warm test).
        self._paths: dict[str, str] = {}
        self._dirty: set[str] = set()
        # File bytes and namespace entries have different durability authorities on POSIX.
        # ``_dirty`` records descriptors whose bytes still need fsync; this set records the
        # directories whose entries changed.  Keeping the latter independently is essential
        # for remove(): once a file is gone there is deliberately no descriptor left for a
        # later global barrier to discover, but its parent directory still owes an fsync.
        self._dirty_directories: set[str] = set()
        self._deferred: dict[str, int] = {}
        self._pending_serial = 0
        self._closed = False
        missing_directories = _missing_directory_chain(self._root)
        if missing_directories and not create_root:
            raise refuse_operation(
                "missing_root",
                f"There is no database at {self._root!r}; an observational open cannot create it.",
                file=self._root,
                path=self._root,
                field="read_only",
                create_root=False,
            )
        if create_root:
            try:
                os.makedirs(self._root, exist_ok=True)
            except OSError as failure:
                raise self._device_failure("open_root", self._root, failure) from failure
        try:
            root_information = os.lstat(self._root)
        except OSError as failure:
            raise self._device_failure("inspect_root", self._root, failure) from failure
        if _is_redirected_path(self._root, root_information):
            raise refuse_operation(
                "redirected_root",
                "A database root may not itself be a symlink, junction or reparse point.",
                file=self._root,
            )
        self._root_real = _comparable_real_path(self._root)
        self._root_identity = (root_information.st_dev, root_information.st_ino)
        # Publishing a newly-created root changes its PARENT namespace, not the root itself.
        # For a multi-level makedirs chain each created directory contributes exactly the parent
        # that names it.  The first barrier flushes root -> ... -> first pre-existing ancestor.
        for created in missing_directories:
            self._dirty_directories.add(os.path.dirname(created))
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
            if self._resolve_identity(name):
                if exclusive:
                    raise refuse_operation(
                        "file_exists", f"File {name!r} already exists on this device.", file=name
                    )
                # A23: the caller now owns this name again, so whatever deletion was queued for
                # it is abandoned. The device never destroys bytes it acknowledged.
                self._forget_deferred(name)
                return
            self._require_directory_parents(name)
            path = self._physical_path(name)
            if os.path.isdir(path):
                raise refuse_not_a_file(name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError as failure:
                raise self._device_failure("create", name, failure) from failure
            self._acknowledge_namespace(path)
            self._forget_deferred(name)
            descriptor = self._retry("create", name, lambda: _open_descriptor(path, create_new=True))
            self._admit(name, descriptor, path)
            self._acknowledge(name)
            # Draining afterwards keeps this door an independent trigger for the deletion queue
            # without letting a pass destroy the very file the caller just asked about.
            self._retry_pending_deletes(namespace=_deletion_namespace(name))

    def remove(self, file: str) -> None:
        """Delete the named file. Removing a file the device does not hold is a failure."""
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            if not self._resolve_identity(name):
                raise refuse_missing_file(name, "remove")
            self._release(name)
            self._dirty.discard(name)
            path = self._physical_path(name)
            self._retry("remove", name, lambda: _remove_file(path))
            self._acknowledge_namespace(path)
            self._forget_deferred(name)
            # Draining afterwards keeps this door an independent trigger for the deletion queue
            # without letting a pass take the very file the caller asked about.
            self._retry_pending_deletes(namespace=_deletion_namespace(name))

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every logical name starting with the prefix, sorted, deferred deletions apart."""
        if not isinstance(prefix, str):
            raise refuse_operation(
                "not_a_string", f"A name prefix must be a string, got {type(prefix).__name__}."
            )
        with self._lock:
            self._require_open()
            # Only the directory the prefix names is walked: ``"wal/"`` walks ``wal`` and
            # ``"control/writer.lease."`` walks ``control``; a prefix without a slash walks
            # the root as before. Names outside that directory cannot start with the prefix.
            below = prefix.rpartition("/")[0]
            return tuple(
                sorted(name for name in self._walk(below=below) if name.startswith(prefix))
            )

    def file_size(self, file: str) -> int:
        """Return the size of the named file in bytes."""
        name = normalize_logical_name(file)
        with self._lock:
            return self._size(name)

    def atomic_replace(self, source: str, target: str) -> None:
        """Move source onto target so a reader observes either the old or the new content.

        This holds while other processes hold the target open, which is what publishing the
        control files of section 6.1 needs, and is proved by
        test_a_control_file_is_published_over_a_reader_in_another_process.
        """
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
            if os.path.isdir(target_path):
                raise refuse_not_a_file(target_name)
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
            except OSError as failure:
                raise self._device_failure("atomic_replace", target_name, failure) from failure
            # _publish_over is the only move that overwrites an existing target on both
            # families: os.rename fails on Windows the moment the target exists, and os.replace
            # fails there whenever another participant holds the target open (CF-5).
            self._retry("atomic_replace", target_name, lambda: _publish_over(source_path, target_path))
            self._acknowledge_namespace(source_path)
            self._acknowledge_namespace(target_path)
            self._dirty.discard(source_name)
            self._acknowledge(target_name)
            # Any deletion still queued for either name refers to a file that is now gone or
            # replaced: dropping it here is what keeps a late pass from destroying live data.
            self._forget_deferred(source_name)
            self._forget_deferred(target_name)

    def recycle(self, file: str) -> bool:
        """Release a file, deferring the deletion when the platform cannot complete it now.

        Whenever the platform allows it, the logical name leaves the namespace before this call
        returns (A17, A23): POSIX unlinks the file, and Windows renames it to a reserved pending
        delete name first, so a later create under the same name succeeds. The answer is True
        when the space is already back and False when the platform still holds it.

        This never raises because somebody holds a handle. When the holder denies both deletion
        and rename, which only a handle opened without delete sharing can do, the file keeps its
        name, the answer is False and NOTHING is queued (A27): the device refuses to carry a
        deletion intent across a window in which another instance could re-claim the name, so
        reclaiming that space becomes the business of the caller, which asks again.
        """
        name = normalize_logical_name(file)
        with self._lock:
            self._require_open()
            self._retry_pending_deletes(namespace=_deletion_namespace(name))
            if not self._resolve_identity(name):
                # Recycling is a reclamation loop and must converge: a name that is already gone
                # has already been reclaimed. remove() is the strict door for the same effect.
                return True
            self._release(name)
            self._dirty.discard(name)
            return self._free_the_name(name, self._physical_path(name))

    def pending_deletes(self) -> tuple[str, ...]:
        """Return the device relative names whose deletion the platform deferred, sorted."""
        with self._lock:
            return tuple(sorted(self._deferred))

    def retry_pending_deletes(self, *, force: bool = False) -> int:
        """Try every deferred deletion once and return how many files were finally reclaimed.

        One pass costs a couple of system calls per deferred file and never sleeps. This explicit
        door and writable close drive the whole queue; create, remove and recycle automatically
        drive only the top-level namespace they name, so control-plane traffic cannot erase
        queued WAL/index/data evidence. A file that survived
        MAX_PENDING_DELETE_ATTEMPTS passes is left alone until the caller asks with force, which
        is the bounded attempt rule of FR-6; pending_deletes() keeps reporting it either way.

        Nothing in the queue can collide with a logical name (A27), so a pass can never destroy
        a file another instance re-claimed: the queue holds reserved pending delete names only.
        """
        with self._lock:
            return self._retry_pending_deletes(force=force)

    def _retry_pending_deletes(
        self,
        *,
        force: bool = False,
        namespace: str | None = None,
    ) -> int:
        """Retry queued names in one automatic namespace, or every name when explicitly asked."""
        if not self._deferred:
            return 0
        reclaimed = 0
        for relative, attempts in tuple(self._deferred.items()):
            if namespace is not None and _deletion_namespace(relative) != namespace:
                continue
            path = os.path.join(self._root, *relative.split("/"))
            if not self._entry_present(path):
                # Checked before the attempt cap: a file that is already gone must be
                # retired from the queue, otherwise an operator reads a phantom forever.
                del self._deferred[relative]
                self._acknowledge_namespace(path)
                reclaimed += 1
                continue
            if attempts >= MAX_PENDING_DELETE_ATTEMPTS and not force:
                continue
            try:
                _remove_file(path)
            except FileNotFoundError:
                del self._deferred[relative]
                self._acknowledge_namespace(path)
                reclaimed += 1
            except OSError:
                self._deferred[relative] = attempts + 1
            else:
                if self._entry_present(path):
                    # The platform armed its own pending delete: the entry disappears when
                    # the last handle closes, and the next pass will see it gone.
                    self._deferred[relative] = attempts + 1
                else:
                    del self._deferred[relative]
                    self._acknowledge_namespace(path)
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
            descriptor = self._descriptor(name, "write")
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
            descriptor = self._descriptor(name, "read")
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
            descriptor = self._descriptor(name, "write")
            size = self._descriptor_size(name, descriptor)
            offset = self._page_offset(name, page_index, size, "write_page")
            # The offset was proved to sit entirely inside the current end of file, so this write
            # cannot extend the file. It is the only overwrite in the adapter.
            try:
                os.lseek(descriptor, offset, os.SEEK_SET)
                written = _write_everything(descriptor, payload)
            except OSError as failure:
                raise self._device_failure("write_page", name, failure, page=page_index) from failure
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
            descriptor = self._descriptor(name, "write")
            size = self._descriptor_size(name, descriptor)
            if not data:
                return size
            return self._append(name, descriptor, data, rollback_to=size)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return up to length bytes starting at offset; a read past the end returns what is there."""
        name = normalize_logical_name(file)
        validate_read_range(name, offset, length)
        with self._lock:
            descriptor = self._descriptor(name, "read")
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
            descriptor = self._descriptor(name, "write")
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

        A28: every failure of this door is a GrafxDurabilityBarrierFailed, whatever caused it.
        The caller counts exactly that type into oktografx_barrier_failures_total, so an access
        failure that escaped as another class would make the metric silent for the very case
        A11-revised was written about; the classification travels in ``details`` instead
        (``reason``, ``errno``, ``winerror``, ``attempts``, ``retryable``).
        """
        names: tuple[str, ...] = ()
        directories: tuple[str, ...] = ()
        with self._lock:
            try:
                self._require_open()
                names = self._barrier_targets(file)
                # A named barrier still flushes every outstanding namespace mutation.  A
                # rename can change two different parent directories and a removed file has
                # no name left from which to rediscover either one.  Flushing this small set is
                # conservative and mirrors what fsync(parent) already does for sibling names.
                directories = tuple(self._dirty_directories)
                for name in names:
                    descriptor = self._descriptor(name, "read")
                    try:
                        os.fsync(descriptor)
                    except OSError as failure:
                        raise barrier_failure(
                            f"The durability barrier of {name!r} did not complete.",
                            reason="fsync_failed",
                            retryable=_is_transient(failure),
                            file=name,
                            errno=failure.errno,
                            winerror=getattr(failure, "winerror", None),
                            attempts=1,
                        ) from failure
                if not IS_WINDOWS:
                    self._synchronize_directories(names, extra=directories)
            except GrafxDurabilityBarrierFailed:
                raise
            except GrafxError as failure:
                raise barrier_failure_from(failure) from failure
            self._dirty.difference_update(names)
            self._dirty_directories.difference_update(directories)

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
            self._paths.clear()
            self._closed = True

    def close_read_only(self) -> None:
        """Close descriptors without driving the writable pending-deletion queue."""
        with self._lock:
            for descriptor in self._handles.values():
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            self._handles.clear()
            self._paths.clear()
            self._closed = True

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

    def _barrier_targets(self, file: object) -> tuple[str, ...]:
        """Return the names one barrier has to flush: the one it names, or everything unflushed."""
        if file is None:
            return tuple(sorted(self._dirty | set(self._handles)))
        return (normalize_logical_name(file),)

    def _free_the_name(self, name: str, path: str) -> bool:
        """Take a logical name out of the namespace and report whether the space is already back.

        POSIX unlinks in one step, which frees the name even while another process reads the
        file. Windows cannot: deleting a held file only arms a pending delete and the directory
        entry lingers, so the file is renamed to a reserved name first and deleted behind it.
        The rename is the fallback on POSIX too, for the rare directory that refuses an unlink,
        so an injected refusal behaves the same way on both families.

        A27: a deletion is queued only when the rename succeeded, because only then does it
        carry a name no future file can take over. When the name cannot be freed at all, the
        device queues NOTHING and simply says so; reclaiming that space is then the business of
        the caller, which knows whether the name is still garbage and asks again.
        """
        if not IS_WINDOWS:
            try:
                _remove_file(path)
            except OSError:
                pass
            else:
                self._acknowledge_namespace(path)
                return True
        pending = self._pending_path(path)
        try:
            os.replace(path, pending)
        except OSError:
            # A holder that denies delete sharing refuses the rename as well; the direct
            # deletion is the only thing left to try, and it may not leave an entry behind.
            try:
                _remove_file(path)
            except OSError:
                return False
            self._acknowledge_namespace(path)
            return not self._entry_present(path)
        self._acknowledge_namespace(path)
        self._acknowledge_namespace(pending)
        self._forget_deferred(name)
        try:
            _remove_file(pending)
        except OSError:
            return self._defer(pending)
        self._acknowledge_namespace(pending)
        if self._entry_present(pending):
            return self._defer(pending)
        return True

    def _pending_path(self, path: str) -> str:
        """Return an unused reserved name for a file whose deletion has to wait."""
        while True:
            self._pending_serial += 1
            candidate = f"{path}{PENDING_DELETE_MARKER}{self._pending_serial}"
            if not os.path.lexists(candidate):
                return candidate

    def _defer(self, path: str) -> bool:
        """Queue one real path for a later deletion pass and report the deferral (A27).

        Only a pending delete name may be queued: it cannot collide with any logical name, so a
        later pass can never destroy a file somebody re-claimed in the meantime. Anything else
        is a bug in this module and is refused rather than queued.
        """
        relative = self._relative(path)
        if not _is_queueable(relative):
            raise refuse_operation(
                "unqueueable_deletion",
                f"A deletion may only be queued under a reserved name, not {relative!r}.",
                file=relative,
            )
        self._deferred.setdefault(relative, 0)
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
        path = os.path.join(self._root, *name.split("/"))
        self._require_safe_path(name)
        return path

    def _require_root_identity(self, name: str) -> None:
        """Refuse a root that was exchanged or redirected after this adapter opened it."""
        try:
            information = os.lstat(self._root)
        except OSError as failure:
            raise self._device_failure("inspect_root", name, failure) from failure
        observed = (information.st_dev, information.st_ino)
        if (
            observed != self._root_identity
            or _is_redirected_path(self._root, information)
            or _comparable_real_path(self._root) != self._root_real
        ):
            raise refuse_operation(
                "redirected_root",
                "The database root was replaced or redirected after the storage device opened.",
                file=name,
                root=self._root,
            )

    def _require_contained(self, name: str, path: str) -> None:
        """Refuse a real path whose resolution leaves the opened database root."""
        for _attempt in range(REALPATH_STABILITY_ATTEMPTS):
            resolved = _comparable_real_path(path)
            try:
                common = os.path.commonpath((self._root_real, resolved))
            except ValueError:
                common = ""
            if os.path.normcase(os.path.normpath(common)) == self._root_real:
                return
            if not (IS_WINDOWS and _is_ntfs_deleted_real_path(resolved)):
                break
            # os.replace can unlink the old file between CPython's two final-path syscalls. The
            # first handle then resolves under C:\$Extend\$Deleted. Never normalize or accept
            # that device path: retry the logical name, bounded, and require a contained result.
        raise refuse_operation(
            "path_escape",
            f"Logical file {name!r} resolves outside the database root.",
            file=name,
            component=self._relative(path),
        )

    def _require_safe_path(self, name: str) -> None:
        """Refuse every existing redirected component of one logical file path."""
        self._require_root_identity(name)
        current = self._root
        for segment in name.split("/"):
            current = os.path.join(current, segment)
            try:
                information = os.lstat(current)
            except (FileNotFoundError, NotADirectoryError):
                # Missing suffixes are safe only while their eventual resolution stays under
                # root; a preceding symlink was already met and refused above.
                self._require_contained(name, current)
                continue
            except OSError as failure:
                raise self._device_failure("inspect_path", name, failure) from failure
            if _is_redirected_path(current, information):
                raise refuse_operation(
                    "redirected_path",
                    f"Logical file {name!r} crosses a symlink, junction or reparse point.",
                    file=name,
                    component=self._relative(current),
                )
            if not (
                stat.S_ISDIR(information.st_mode)
                or stat.S_ISREG(information.st_mode)
            ):
                raise refuse_operation(
                    "unsupported_entry_type",
                    f"Logical file {name!r} crosses a filesystem entry that is neither a "
                    "regular file nor a directory.",
                    file=name,
                    component=self._relative(current),
                    mode=stat.S_IFMT(information.st_mode),
                )
            self._require_contained(name, current)

    def _require_directory_parents(self, name: str) -> None:
        """Refuse a name whose parent segment is itself a stored file, on both families alike."""
        self._require_safe_path(name)
        current = self._root
        for segment in name.split("/")[:-1]:
            current = os.path.join(current, segment)
            if os.path.isfile(current):
                raise refuse_not_a_directory(name, self._relative(current))

    def _adopt_pending_deletes(self) -> None:
        """Take over the deferred deletions a previous run left behind, and keep serials unique."""
        for name in self._walk(include_pending=True):
            entry = name.rsplit("/", 1)[-1]
            marker = entry.lower().rfind(PENDING_DELETE_MARKER)
            if marker < 0:
                continue
            self._deferred.setdefault(name, 0)
            tail = entry[marker + len(PENDING_DELETE_MARKER) :]
            if tail.isdigit():
                self._pending_serial = max(self._pending_serial, int(tail))

    def _entries(self, directory: str, name: str) -> tuple[str, ...]:
        """Return the real entries of one directory, empty when the directory is not there."""
        self._require_root_identity(name)
        self._require_contained(name, directory)
        if os.path.lexists(directory):
            try:
                information = os.lstat(directory)
            except OSError as failure:
                raise self._device_failure("inspect_path", name, failure) from failure
            if _is_redirected_path(directory, information):
                raise refuse_operation(
                    "redirected_path",
                    f"Logical file {name!r} crosses a symlink, junction or reparse point.",
                    file=name,
                    component=self._relative(directory),
                )
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
        self._require_safe_path(name)
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

    def _walk(self, *, include_pending: bool = False, below: str = "") -> Iterable[str]:
        """Yield regular files without ever following or ignoring a redirected component.

        ``below`` names one stored directory (``"wal"``, ``"quarantine/a"``) and confines the
        walk to it. The way down is proved as every logical name is proved -- root identity,
        then each segment inspected without following it, refused when redirected or foreign,
        and contained -- and a directory that is not there yields nothing.

        Containment is inherited, not re-derived per entry. The root is proved by real path,
        every directory is proved before it is entered, and an entry that ``lstat`` shows to be
        a plain file or directory rather than a symlink, junction or reparse point cannot
        resolve anywhere but under the directory that lists it. Resolving the real path of
        every entry again was the dominant cost of every WAL discovery on a 346-file board
        (fase 0a: 4.059 ``realpath`` calls per commit, ~2,1 s of a 2,6 s commit).
        """
        self._require_root_identity(self._root)
        directory, prefix = self._root, ""
        for segment in below.split("/") if below else ():
            if not segment:
                return  # no stored name has an empty segment, so nothing can match
            name = prefix + segment
            candidate = os.path.join(directory, segment)
            try:
                information = os.lstat(candidate)
            except (FileNotFoundError, NotADirectoryError):
                return  # nothing is stored below a directory that is not there
            except OSError as failure:
                raise self._device_failure("inspect_path", name, failure) from failure
            self._refuse_foreign_component(name, candidate, information)
            if not stat.S_ISDIR(information.st_mode):
                return  # a stored file has nothing below it
            self._require_contained(name, candidate)
            directory, prefix = candidate, name + "/"
        pending: list[tuple[str, str]] = [(directory, prefix)]
        while pending:
            directory, prefix = pending.pop()
            try:
                with os.scandir(directory) as scan:
                    entries = tuple(scan)
            except OSError as failure:
                raise self._device_failure("list", prefix or self._root, failure) from failure
            for entry in entries:
                name = prefix + entry.name
                try:
                    information = entry.stat(follow_symlinks=False)
                except OSError as failure:
                    raise self._device_failure("inspect_path", name, failure) from failure
                self._refuse_foreign_component(name, entry.path, information)
                if stat.S_ISDIR(information.st_mode):
                    pending.append((entry.path, name + "/"))
                elif include_pending or not _is_pending_delete(entry.name):
                    yield name

    def _refuse_foreign_component(
        self, name: str, path: str, information: os.stat_result
    ) -> None:
        """Refuse a stored component that is redirected, or neither a file nor a directory."""
        if _is_redirected_path(path, information):
            raise refuse_operation(
                "redirected_path",
                f"Stored namespace component {name!r} is a symlink, junction or "
                "reparse point and will not be followed or ignored.",
                file=name,
                component=name,
            )
        if not (stat.S_ISDIR(information.st_mode) or stat.S_ISREG(information.st_mode)):
            raise refuse_operation(
                "unsupported_entry_type",
                f"Stored namespace component {name!r} is neither a regular file nor "
                "a directory and will not be opened or ignored.",
                file=name,
                component=name,
                mode=stat.S_IFMT(information.st_mode),
            )

    def _descriptor(self, name: str, intent: str) -> int:
        """Return the cached descriptor of a file, opening and admitting it when it is not cached.

        ``intent`` is the single choke point of A26.1: it is a required argument, so a write
        method cannot be added without stating that it acknowledges bytes, and stating it is
        what makes the device owe that name a barrier. A read states ``"read"`` and changes
        nothing; a barrier reads, because flushing a name is not acknowledging new bytes (A29).
        """
        if intent not in ("read", "write"):
            raise refuse_operation(
                "invalid_intent", f"A descriptor is taken to read or to write, not to {intent!r}."
            )
        self._require_open()
        cached = self._handles.pop(name, None)
        if cached is not None and not self._still_names(name, cached):
            # The directory entry moved from under the handle: another participant published
            # over this name with atomic_replace, and the cached descriptor now reads the OLD
            # file -- forever. A long-lived process that had once read control/commit.state kept
            # reading the number it saw first, never learned of anyone else's commits, and had
            # every commit of its own refused as a conflict with a world it could not see. The
            # handle is closed and the name re-opened from the directory (C9 round-3 finding,
            # routed to C2; the CF-5 rename was only ever half of publication).
            with contextlib.suppress(OSError):
                os.close(cached)
            cached = None
        if cached is not None:
            self._handles[name] = cached
            if intent == "write":
                self._acknowledge(name)
            return cached
        if not self._resolve_identity(name):
            raise refuse_missing_file(name, "open")
        path = self._physical_path(name)
        descriptor = self._retry("open", name, lambda: _open_descriptor(path, create_new=False))
        self._admit(name, descriptor, path)
        # A29: the name joins the unflushed set only now, once it is known to be valid and open.
        # A refused write that recorded a name would make every later global barrier fail, and
        # under FR-5 that means no commit on this device could ever succeed again.
        if intent == "write":
            self._acknowledge(name)
        return descriptor

    def _acknowledge(self, name: str) -> None:
        """Record that this device is acknowledging bytes for a logical name (A26).

        The device owes the name a durability barrier from now on. It also abandons any deletion
        queued for that name: under A27 the queue can only hold reserved names, so this can
        never match, and it stays as the guard that makes the rule true by construction rather
        than by argument.
        """
        self._dirty.add(name)
        self._forget_deferred(name)

    def _acknowledge_namespace(self, path: str) -> None:
        """Remember every owned directory whose namespace may have changed.

        The immediate parent owns the file entry; each ancestor owns the entry for the nested
        directory below it.  Remembering the complete chain makes creation of a new nested
        namespace durable in the same child-before-parent barrier as its first file.  Existing
        ancestors may be recorded again -- an idempotent and deliberately conservative debt.
        """
        directory = os.path.dirname(path)
        while True:
            self._dirty_directories.add(directory)
            if os.path.normcase(directory) == os.path.normcase(self._root):
                return
            parent = os.path.dirname(directory)
            if parent == directory:
                # All callers pass a path below _root.  Keep this guard fail-safe if that
                # invariant is ever broken rather than walking forever at a volume root.
                return
            directory = parent

    def _admit(self, name: str, descriptor: int, path: str | None = None) -> None:
        """Cache one descriptor, evicting the least recently used one when the cache is full.

        ``path`` is the proved physical path the descriptor was opened through; a caller that
        cannot vouch for one leaves it out, and the next warm hit proves the name again.
        """
        self._handles[name] = descriptor
        if path is None:
            self._paths.pop(name, None)
        else:
            self._paths[name] = path
        while len(self._handles) > self._max_open_files:
            oldest = next(iter(self._handles))
            self._release(oldest)

    def _still_names(self, name: str, descriptor: int) -> bool:
        """Return True when the directory entry for the name is still the file the descriptor holds.

        Identity is ``(st_dev, st_ino)`` on both families -- on Windows Python reports the volume
        serial and the 64-bit file index there, so a file published over the name by another
        process shows a different identity even though the name is unchanged. A path that cannot
        be examined at all (gone, unreadable) is not the held file either, and the caller then
        re-resolves the name and refuses it honestly.

        The path is the one the descriptor was opened through, proved when it was admitted.
        Proving the whole chain again on every warm hit -- the root by real path, each segment
        by ``lstat`` and real path -- cost ~2 ms per page read and 6,2 s of one 17-statement
        operation on the acceptance board (fase 0a). A component redirected since admission
        cannot make this ``stat`` agree with the held descriptor unless it leads to the very
        same file, and any disagreement sends the caller through the full resolution, which
        refuses the redirect.
        """
        path = self._paths.get(name)
        if path is None:
            path = self._physical_path(name)  # admitted without a proved path: prove it now
        try:
            current = os.stat(path)
            held = os.fstat(descriptor)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)

    def _release(self, name: str) -> None:
        """Close and forget the cached descriptor of one file, if there is one.

        Nothing is buffered in user space: every write reaches the operating system through a
        single system call, so releasing a descriptor can never lose a byte. What a barrier
        still owes the file is remembered in the unflushed set, not in the descriptor cache.
        """
        descriptor = self._handles.pop(name, None)
        self._paths.pop(name, None)
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _size(self, name: str) -> int:
        """Return the current size of a file in bytes."""
        descriptor = self._descriptor(name, "read")
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

    def _synchronize_directories(
        self, names: tuple[str, ...], *, extra: tuple[str, ...] = ()
    ) -> None:
        """Flush file parents plus outstanding namespace directories. POSIX only."""
        directories = {os.path.dirname(self._physical_path(name)) for name in names}
        directories.update(extra)
        directories.add(self._root)
        # A child entry is fixed before the parent entry that makes that child reachable.
        for directory in sorted(
            directories,
            key=lambda path: (path.count(os.sep), path),
            reverse=True,
        ):
            try:
                descriptor = os.open(directory, os.O_RDONLY)
            except OSError as failure:
                raise barrier_failure(
                    "A durability barrier could not open the directory that holds the file.",
                    reason="directory_open_failed",
                    retryable=_is_transient(failure),
                    errno=failure.errno,
                    winerror=getattr(failure, "winerror", None),
                    attempts=1,
                ) from failure
            try:
                os.fsync(descriptor)
            except OSError as failure:
                raise barrier_failure(
                    "A durability barrier could not flush the directory that holds the file.",
                    reason="directory_fsync_failed",
                    retryable=_is_transient(failure),
                    errno=failure.errno,
                    winerror=getattr(failure, "winerror", None),
                    attempts=1,
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
        failure or any other access failure is a storage error, because reporting it as
        corruption would send recovery into truncation, quarantine and a forensic ledger entry
        for a file whose bytes are perfect.

        Two details of that error are load-bearing for the caller. ``retryable`` is advice A47
        acts on, so a condition that cannot clear -- a regular file where the database directory
        belongs -- says False and stops the caller spinning forever. And the message is ours, in
        en-US: the text the platform supplies is localized, so it travels in
        ``details["platform_message"]`` where a human can still read it and no gate has to
        pretend a runtime string is ASCII (G1, A7).
        """
        if _is_device_full(failure):
            return GrafxDeviceFull(
                f"The device refused {operation} on {name!r} because it is full.",
                file=name,
                operation=operation,
                errno=failure.errno,
                winerror=getattr(failure, "winerror", None),
                platform_message=failure.strerror,
                **details,
            )
        details.setdefault("attempts", 1)
        permanent = _is_permanent(failure)
        return GrafxStorageError(
            f"The device failed {operation} on {name!r}, and the condition is "
            f"{'permanent' if permanent else 'worth retrying'}.",
            retryable=not permanent,
            reason="permanently_refused" if permanent else "access_failed",
            file=name,
            operation=operation,
            errno=failure.errno,
            winerror=getattr(failure, "winerror", None),
            platform_message=failure.strerror,
            **details,
        )


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


def _publish_over(source: str, target: str) -> None:
    """Move source onto target so a reader observes either the old or the new content (CF-5).

    Measured on Windows, and this is the whole of the defect: ``os.replace`` onto a target that
    any process holds open fails with ERROR_ACCESS_DENIED **even when every holder shares delete
    access**, because MoveFileEx frees the target's directory entry eagerly and an open handle
    forbids that. Sharing delete is necessary for ``remove`` and it is not sufficient here, so
    the share mode was never the missing piece: the primitive was.

    Renaming with POSIX semantics is the primitive that has the behaviour the port describes.
    The directory entry is replaced in one step, a reader that already opened the old file goes
    on reading it to the end, and a reader that opens the name afterwards sees the new content
    whole. That is what proves multi-process publication of the control files of section 6.1.

    Where the platform cannot do it -- a build older than the one that introduced the flag, a
    file system that does not implement it, or a holder that denies delete access -- this falls
    back to os.replace, which is what every POSIX family uses in the first place.
    """
    if _WINDOWS_OPENER is None:
        os.replace(source, target)
        return
    try:
        _windows_posix_replace(source, target)
    except (OSError, ValueError):
        # A request this platform cannot even express is a reason to use the ordinary
        # primitive, not a reason to let a non-Grafx failure out of a port door.
        os.replace(source, target)


def _windows_posix_replace(source: str, target: str) -> None:
    """Replace target with source through SetFileInformationByHandle, POSIX style."""
    library, modules, invalid = _WINDOWS_OPENER  # type: ignore[misc]
    ctypes_module, _ = modules  # type: ignore[misc]
    handle = library.CreateFileW(  # type: ignore[attr-defined]
        source,
        _DELETE_ACCESS | _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == invalid:
        raise ctypes_module.WinError(ctypes_module.get_last_error())
    try:
        request = _rename_request(ctypes_module, target)
        moved = library.SetFileInformationByHandle(  # type: ignore[attr-defined]
            handle, _FILE_RENAME_INFO_EX, ctypes_module.byref(request), ctypes_module.sizeof(request)
        )
        if not moved:
            raise ctypes_module.WinError(ctypes_module.get_last_error())
    finally:
        library.CloseHandle(handle)  # type: ignore[attr-defined]


def _rename_request(ctypes_module: object, target: str) -> object:
    """Return a FILE_RENAME_INFO asking for a POSIX style replacement of target.

    The offsets are computed by ctypes rather than by hand. Writing them by hand is how the
    first version of this function produced a file whose NAME was garbage while the call still
    reported success, which no assertion about content would have caught.

    The name is measured in UTF-16 CODE UNITS, not in code points. ``c_wchar`` is one unit and
    ``len()`` counts code points, so every character outside the basic plane costs one unit more
    than a length in characters accounts for. A logical name can never carry one, but the
    database root is an arbitrary string the caller chooses, and sizing this buffer from ``len``
    made a root holding an emoji either overflow the array or fill it with no room for the
    terminator -- and with no terminator the kernel takes the name from whatever follows.
    Proved by test_a_control_record_is_published_through_a_root_outside_the_ascii_plane.
    """
    from ctypes import wintypes  # noqa: PLC0415 - Windows only, and only on this path

    units = len(target.encode("utf-16-le")) // 2

    class _FileRenameInfo(ctypes_module.Structure):  # type: ignore[attr-defined, misc]
        """FILE_RENAME_INFO with the name sized for this one call."""

        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes_module.c_wchar * (units + 1)),  # type: ignore[attr-defined]
        ]

    request = _FileRenameInfo()
    request.Flags = _RENAME_REPLACE_IF_EXISTS | _RENAME_POSIX_SEMANTICS
    request.RootDirectory = None
    # The length is in bytes and excludes the terminator, which the buffer still carries.
    request.FileNameLength = units * 2
    request.FileName = target
    return request


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
