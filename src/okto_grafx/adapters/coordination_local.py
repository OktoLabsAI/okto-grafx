"""The local process coordinator (CONTRACT.md sections 4.3 and 6.1, SPEC-M1 FR-7, BR-7, BR-10).

This adapter answers three questions for every participant of a database directory: who holds
the writer epoch, which readers are alive and what snapshot each of them pins, and who is inside
a short critical section right now. It answers them with two mechanisms and nothing else:

* **durable control files, written through the StorageDevice port.** The lease lives in
  ``control/writer.lease`` and every reader in ``control/readers/<reader_id>.reader``. Legacy
  databases publish whole records with ``atomic_replace``. Format-2 databases alternate two
  checksummed lease pages; reader records remain whole-file publications until CE-2 gives them a
  participant lifetime. In both forms a reader sees one complete generation, never torn bytes.
* **an operating-system advisory lock per named section.** ``exclusive()`` takes a real file
  lock (``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX) so the read-modify-write of the
  lease is atomic between processes and, decisively, so a participant killed without cleanup
  releases the section the moment its handles are closed by the kernel.

**Liveness is measured only with the local monotonic clock (FR-7).** Two processes share no
origin for ``Clock.monotonic()``, so a monotonic reading is never written to a file and never
compared across processes. What travels between processes is ``heartbeat_seq``, a counter the
owner increments on every renewal. An observer samples the pair ``(heartbeat_seq, its own
monotonic reading)`` and concludes that the owner is dead only when the counter has not moved
across an interval measured entirely with the observer own clock. ``wall_stamp`` is carried in
the records for a human reading a hex dump and participates in no decision at all: moving the
system clock by an hour in either direction changes nothing here.

**The epoch decides, never the data (BR-7).** Every change of ownership increments the epoch,
and ``validate_epoch`` refuses any epoch that is not the published one before the caller reaches
the device, which is what makes a zombie writer structurally harmless rather than merely
unlikely.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import threading
import time
import uuid
import weakref
import zlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from math import isfinite
from typing import TypeVar

from okto_grafx.adapters.control_record_io import read_control_if_exists
from okto_grafx.domain.control_record import (
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxSchemaVersionMismatch,
    GrafxStaleEpoch,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import Epoch, Lsn
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.coordination import DeadOwnerReport, Lease, ReaderHandle
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "CONTROL_DIRECTORY",
    "LEASE_FILE_NAME",
    "LEASE_FORMAT_VERSION",
    "LEASE_MAGIC",
    "LEASE_SECTION",
    "LEASE_WAIT_METRIC",
    "LOCK_FILE_SUFFIX",
    "TEMPORARY_SUFFIX",
    "LOCK_MECHANISM",
    "PLATFORM_FAMILY",
    "READERS_DIRECTORY_NAME",
    "READER_FILE_SUFFIX",
    "READER_FORMAT_VERSION",
    "READER_MAGIC",
    "LeaseRecord",
    "LocalProcessCoordinator",
    "ReaderRecord",
    "decode_lease_record",
    "decode_reader_record",
    "encode_lease_record",
    "encode_reader_record",
]

# --- platform branch (TR-3: one contract, one mechanism per operating-system family) ----------

_IS_WINDOWS: bool = os.name == "nt"

if _IS_WINDOWS:  # pragma: no cover - the other family is covered on the other family
    import msvcrt
else:  # pragma: no cover - same reason, mirrored
    import fcntl

PLATFORM_FAMILY: str = "windows" if _IS_WINDOWS else "posix"
"""Which family of advisory lock this build selected. Declared so a test can assert the branch."""

LOCK_MECHANISM: str = "msvcrt.locking" if _IS_WINDOWS else "fcntl.flock"
"""The exact primitive behind ``exclusive()``.

Both give cross-process and cross-handle exclusion.
"""

_BINARY_FLAG: int = getattr(os, "O_BINARY", 0)
"""``os.O_BINARY`` where the platform has it.

Opening without it on Windows selects the text mode of the C runtime, whose commit truncates a
file at a trailing 0x1A byte. The lock file carries no payload, but the flag is set anyway so
this module never demonstrates the pattern that would lose a byte somewhere else.
"""

_LOCK_BYTES: int = 1
"""Size of the locked byte range. One byte at offset zero is enough to name the whole section."""

# --- control-file layout (FROZEN for C6 recovery and C13 bench readers) -----------------------

CONTROL_DIRECTORY: str = "control"
"""Default control sub-directory inside the storage namespace (CONTRACT.md section 6.1)."""

LEASE_FILE_NAME: str = "writer.lease"
"""Name of the lease file inside the control directory."""

READERS_DIRECTORY_NAME: str = "readers"
"""Name of the reader registry directory inside the control directory."""

READER_FILE_SUFFIX: str = ".reader"
"""Suffix that makes a reader registration recognisable and separates it from a temporary file."""

TEMPORARY_SUFFIX: str = ".tmp"
"""Suffix of the file a control record is written to before it replaces its target."""

LOCK_FILE_SUFFIX: str = ".lock"
"""Suffix of the advisory lock file that backs one named section."""

LEASE_MAGIC: bytes = b"OKTOLEAS"
"""Magic of the lease record. Eight bytes, so a hex dump names the file immediately."""

READER_MAGIC: bytes = b"OKTORDER"
"""Magic of a reader registration record."""

LEASE_FORMAT_VERSION: int = 1
"""Current lease record version. A newer version on disk is a typed refusal, never a guess."""

READER_FORMAT_VERSION: int = 1
"""Current reader record version."""

LEASE_SECTION: str = "writer.lease"
"""Name of the section that serialises every read-modify-write of the lease file."""

LEASE_WAIT_METRIC: str = "oktografx_lease_wait_seconds"
"""Metric this adapter observes on every acquisition attempt (CONTRACT.md section 9)."""

EMITTED_METRICS: tuple[str, ...] = (LEASE_WAIT_METRIC,)
"""Every metric this component emits, on every path including the error and takeover paths.

A sink that enforces registration refuses a name it has never seen, so a component that emits
without registering fails the moment it meets the real sink rather than a permissive double. The
descriptors come from the frozen catalog, which is what keeps them from drifting away from the
ones a dashboard and the continuous integration gate read.
"""

_LEASE_HEADER: str = "<8sHHIQQddQII"
_LEASE_HEADER_SIZE: int = struct.calcsize(_LEASE_HEADER)
_READER_HEADER: str = "<8sHHIQQdII"
_READER_HEADER_SIZE: int = struct.calcsize(_READER_HEADER)
_CHECKSUM_SIZE: int = 4
_CHECKSUM_FORMAT: str = "<I"

_FLAG_ACTIVE: int = 0x0001
"""Bit zero of the flags field: the lease is held, or the reader registration is live."""

_READ_ATTEMPTS: int = 4
"""How many times a control file is re-read before its content is called corrupt.

``atomic_replace`` publishes a whole record at once, but a reader that samples the size and the
bytes in two calls can straddle a replacement and see a length that no longer matches. Re-reading
resolves that benign race; only a persistent mismatch is real damage.
"""

_PUBLISH_ATTEMPTS: int = 5
"""How many times a control record is published before the device failure is reported.

A sharing violation from an antivirus scanner or a search indexer lasts milliseconds; the whole
budget here is tens of milliseconds, which is short enough to stay inside a commit window and
long enough to cover the overwhelming majority of them (TR-3).
"""

_MAX_PUBLISH_BACKOFF: float = 0.04
"""Ceiling of the exponential backoff between two publish attempts, in seconds."""

_WAIT_ITERATION_FLOOR: int = 1_000
"""Smallest iteration budget any wait gets, so a tiny timeout still polls a few times."""

_WAIT_ITERATION_MARGIN: int = 4
"""How many times the expected iteration count a wait may spend before it gives up.

The bound exists for one case only: an injected clock that does not advance, where the deadline
never arrives and the loop would spin for ever. Four times the expected count cannot be reached
by a wait whose clock moves, and it stops one whose clock does not in milliseconds rather than
in minutes.
"""

_MAX_WAIT_ITERATIONS: int = (1 << 63) - 1
"""Saturation point for the frozen-clock guard of an exceptionally large finite timeout."""

_MAX_IDENTIFIER_LENGTH: int = 96
"""Longest accepted identifier of any kind. It has to fit in a file name on every platform."""

_IDENTIFIER_VALIDATION_CACHE_SIZE: int = 512
"""Maximum successful exact-string identifier proofs retained process-locally."""

_READER_SUFFIX_BUDGET: int = 8
"""Room a reader identifier needs on top of the stored owner identifier, as in ``-r0001``."""

_INSTANCE_NONCE_LENGTH: int = 8
"""Hex characters of the per-instance nonce that makes a reader identifier unique.

The owner identifier can be supplied by the caller and two coordinators can therefore be built
with the same one. That is a configuration mistake rather than an attack, and it used to make
both instances mint the same reader identifier: the second registration silently replaced the
first, the horizon jumped forward, and C4 would have recycled segments a live reader still
needed with nothing raised anywhere (BR-10, AC-8). The nonce is what makes that impossible.
"""

_MAX_STORED_OWNER_LENGTH: int = _MAX_IDENTIFIER_LENGTH - _READER_SUFFIX_BUDGET
"""Longest owner identifier that may reach the record.

Shorter than the identifier ceiling on purpose: every reader identifier is built from the stored
owner identifier plus a counter, so an owner accepted at the ceiling would make
``register_reader`` fail forever. The two doors have to agree.
"""

_MAX_CONFIGURED_OWNER_LENGTH: int = (
    _MAX_STORED_OWNER_LENGTH - _INSTANCE_NONCE_LENGTH - 1
)
"""Longest owner identifier a CALLER may configure.

The instance nonce and its separator are added before the identity reaches the disk, so the
caller budget is the stored budget less the room that composition needs.
"""

_MAX_UINT64: int = 0xFFFFFFFFFFFFFFFF
"""Largest value any counter of these records can carry. Above it the record cannot be encoded."""

_IDENTIFIER_EXTRA_CHARACTERS: frozenset[str] = frozenset({".", "-", "_"})
"""Punctuation accepted inside an identifier, all of it safe in a file name."""

_Record = TypeVar("_Record", "LeaseRecord", "ReaderRecord")
"""Either control record, so one retrying reader serves both without losing its type."""


_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    {errno.EACCES, errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.EWOULDBLOCK}
)
"""Error numbers that mean "somebody is in the way right now", not "this will never work"."""

_TRANSIENT_WINERRORS: frozenset[int] = frozenset({5, 32, 33})
"""Access denied and the two sharing violations. An antivirus scan produces all three."""

_CONTENTION_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EWOULDBLOCK,
        errno.EAGAIN,
        errno.EACCES,
        getattr(errno, "EDEADLOCK", errno.EDEADLK),
        errno.EDEADLK,
    }
)
"""Error numbers an advisory-lock primitive uses to say "somebody else holds it right now".

Everything else those primitives can answer -- no lock support on the mount, a bad descriptor,
an argument the platform refuses -- never clears by waiting, so reporting it as a busy section
blames a holder that does not exist and invites a caller to retry a path that cannot work.
"""

_MISSING_WINERRORS: frozenset[int] = frozenset({2, 3})
"""The file and the path were not found. On POSIX the same condition arrives as ENOENT."""


def _is_transient(failure: OSError) -> bool:
    """Return whether a device failure is the kind that improves on its own (A11-revised).

    The classification decides the ``retryable`` flag a caller branches on, so the default has
    to be the honest one: a sharing violation reported as permanent forbids the one action that
    would have worked, and an unreachable path reported as retryable invites an endless loop.
    """
    winerror = getattr(failure, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return failure.errno in _TRANSIENT_ERRNOS


def _storage_failure(
    message: str, failure: OSError, *, attempts: int, **details: object
) -> GrafxStorageError:
    """Give a RAW platform failure a type, classified from its own errno.

    Only an ``OSError`` reaches here. A device failure that already carries a Grafx class keeps
    it -- the class is part of the answer, and inventing a retry flag for a failure this adapter
    has itself decided not to retry is a contradiction a caller cannot see through.
    """
    built = GrafxStorageError(
        message,
        retryable=_is_transient(failure),
        attempts=attempts,
        errno=failure.errno,
        winerror=getattr(failure, "winerror", None),
        detail=str(failure),
        **details,
    )
    # A47 again, from the other side: whoever retries around THIS error reads the details.
    built.details["retryable"] = built.retryable
    return built


def _classified_retryable(failure: BaseException) -> bool | None:
    """Return the retry classification a device failure carries, or None when it carries none.

    Amendment A47: the classification travels in ``details["retryable"]``, never in the exception
    class. A28 folded EVERY barrier failure into one class and put the access classification in
    the details, so a predicate that switched on the class would report the transient antivirus
    touch TR-3 names as permanent on the barrier while riding out the identical condition on the
    call before it.
    """
    if isinstance(failure, GrafxError):
        classification = failure.details.get("retryable")
        if isinstance(classification, bool):
            return classification
    return None


def _worth_retrying(failure: BaseException) -> bool:
    """Return whether a device failure is the kind another attempt might survive.

    The classification in the details decides whenever the device provided one. Without one there
    is no information to act on, so the answer is the conservative one: a raw platform error is
    read for a transient errno, a storage error is trusted with its own flag, and anything else
    is an answer rather than an obstacle and is reported at once.
    """
    classified = _classified_retryable(failure)
    if classified is not None:
        return classified
    if isinstance(failure, GrafxStorageError):
        return failure.retryable
    return isinstance(failure, OSError) and _is_transient(failure)


def _is_missing(failure: BaseException) -> bool:
    """Return whether a failure says the file simply is not there."""
    if not isinstance(failure, OSError):
        return False
    winerror = getattr(failure, "winerror", None)
    if winerror is not None:
        return winerror in _MISSING_WINERRORS
    return failure.errno == errno.ENOENT


def _worth_publishing_again(failure: BaseException) -> bool:
    """Return whether a failed publication is worth another attempt.

    Publishing builds its own temporary file from nothing, so a temporary that went missing
    underneath it is not an obstacle at all: the next attempt makes a new one. That is the single
    condition where a retry is a certainty rather than a hope, and reporting it as permanent is
    exactly the refusal A11-revised names -- forbidding the one action that would have worked.
    """
    return _is_missing(failure) or _worth_retrying(failure)


_SHARED_SECTIONS: dict[int, dict[str, threading.Lock]] = {}
"""Section locks shared by every coordinator over one storage namespace, for the lockless mode.

This is the one piece of module-level state in the adapter, and it is what makes the mode without
a lock directory honest. Sections have to be at least PROCESS wide: an in-memory device is never
shared between processes, but it is routinely shared between two coordinator objects, and a lock
that lives inside one object excludes nobody. The table is keyed by the identity of the namespace
object, so two databases never serialise against each other, and a finaliser drops the entry when
the namespace is collected, so the identity can never be reused underneath a live table.
"""

_SHARED_SECTIONS_GUARD: threading.Lock = threading.Lock()
"""Guards the registry above. Held only long enough to hand out one lock."""


def _shared_section_lock(namespace: object, key: str) -> threading.Lock:
    """Return the process-wide lock of one section within one storage namespace."""
    token = id(namespace)
    with _SHARED_SECTIONS_GUARD:
        table = _SHARED_SECTIONS.get(token)
        if table is None:
            table = {}
            _SHARED_SECTIONS[token] = table
            try:
                weakref.finalize(namespace, _SHARED_SECTIONS.pop, token, None)
            except TypeError:
                # A namespace that cannot be referenced weakly keeps its table for the life of
                # the process. That leaks one small dictionary and never a correctness property.
                pass
        lock = table.get(key)
        if lock is None:
            lock = threading.Lock()
            table[key] = lock
        return lock


def _reject(message: str, **details: object) -> GrafxConfigurationError:
    """Build the typed configuration error used for every constructor and argument refusal.

    The parameter is named ``message`` so that ``reason`` stays available as a detail key: a
    call that passes both must not collide with the positional parameter of this helper.
    """
    return GrafxConfigurationError(message, **details)


def _validate_identifier(
    label: str, value: str, *, limit: int = _MAX_IDENTIFIER_LENGTH
) -> str:
    """Return the identifier when it is safe as a file name, else raise a configuration error.

    Identifiers become file names, and file identity must never depend on case (CONTRACT.md
    section 11 item 9), so an upper-case character is refused rather than silently folded. A
    relative traversal is refused for the same family of reasons: a control directory of ``..``
    would put the lease outside the database it is supposed to protect.
    """
    if type(label) is str and type(value) is str and type(limit) is int:
        _validate_exact_identifier(label, value, limit)
        return value
    return _validate_identifier_uncached(label, value, limit=limit)


@lru_cache(maxsize=_IDENTIFIER_VALIDATION_CACHE_SIZE)
def _validate_exact_identifier(label: str, value: str, limit: int) -> None:
    """Remember a bounded successful proof for immutable built-in strings only."""
    _validate_identifier_uncached(label, value, limit=limit)


def _validate_identifier_uncached(
    label: str, value: str, *, limit: int = _MAX_IDENTIFIER_LENGTH
) -> str:
    """Apply the canonical identifier grammar without consulting derived state."""
    if not isinstance(value, str) or not value:
        raise _reject(
            f"The {label} must be a non-empty string.", field=label, value=repr(value)
        )
    if len(value) > limit:
        raise _reject(
            f"The {label} must be at most {limit} characters long.",
            field=label,
            length=len(value),
        )
    if value in {".", ".."} or ".." in value:
        raise _reject(
            f"The {label} must not name a relative path, so it cannot leave the database.",
            field=label,
            value=value,
        )
    if not value.isascii():
        raise _reject(f"The {label} must be ASCII.", field=label, value=value)
    for character in value:
        allowed = (
            character.isdigit()
            or (character.isalpha() and character.islower())
            or character in _IDENTIFIER_EXTRA_CHARACTERS
        )
        if not allowed:
            raise _reject(
                f"The {label} accepts lower-case letters, digits, '.', '-' and '_' only, "
                "because file identity must not depend on case.",
                field=label,
                value=value,
                character=character,
            )
    return value


def _require_positive(label: str, value: float) -> float:
    """Return the value when it is a strictly positive real number, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(f"The {label} must be a number.", field=label, value=repr(value))
    number = float(value)
    if not number > 0.0 or number != number:
        raise _reject(
            f"The {label} must be greater than zero.", field=label, value=number
        )
    return number


def _require_non_negative(label: str, value: float) -> float:
    """Return the value when it is a non-negative real number, else raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(f"The {label} must be a number.", field=label, value=repr(value))
    number = float(value)
    if number < 0.0 or number != number:
        raise _reject(f"The {label} must not be negative.", field=label, value=number)
    return number


def _require_index(label: str, value: int) -> int:
    """Return the value when it is a non-negative integer, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(
            f"The {label} must be an integer.", field=label, value=repr(value)
        )
    if value < 0:
        raise _reject(f"The {label} must not be negative.", field=label, value=value)
    if value > _MAX_UINT64:
        raise _reject(
            f"The {label} must fit in the 64 bits the record reserves for it.",
            field=label,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """The durable content of the lease file: who owns which epoch, and where its heartbeat is."""

    owner_id: str
    epoch: Epoch
    heartbeat_seq: int
    ttl_seconds: float
    wall_stamp: float
    held: bool
    superseded_epoch: Epoch

    def liveness_key(self) -> tuple[str, int, int]:
        """Return the triple an observer samples: a change in it is proof of progress."""
        return (self.owner_id, self.epoch, self.heartbeat_seq)


@dataclass(frozen=True, slots=True)
class ReaderRecord:
    """The durable content of one reader registration: the snapshot it pins and its heartbeat."""

    reader_id: str
    snapshot_lsn: Lsn
    heartbeat_seq: int
    wall_stamp: float
    active: bool

    def liveness_key(self) -> tuple[str, int]:
        """Return the pair an observer samples for this reader."""
        return (self.reader_id, self.heartbeat_seq)


@dataclass(slots=True)
class _ReusableDescriptorScope:
    """One thread-local interval in which an unlocked section descriptor may stay open."""

    depth: int = 1
    descriptor: int | None = None
    borrowed: bool = False
    closing: bool = False
    # How many nested intervals asked for the revalidated variant. A parked descriptor names
    # the file it was opened through, never the path: a lock file replaced between two
    # operations leaves the descriptor on an orphaned inode, and locking an orphan always
    # succeeds while another process locks the live file. Counting rather than flagging keeps
    # the guard while ANY long interval is open, including one nested inside a short one.
    revalidations: int = 0
    # Once a long interval has touched this scope the guard stays for the rest of its life. A
    # long interval nested inside a short one has already stretched real time across the short
    # one, so letting the outer frame reuse that descriptor unproved would restore exactly the
    # window the guard exists to close.
    ever_revalidated: bool = False

    @property
    def revalidated(self) -> bool:
        """Return whether a parked descriptor must be re-proved before it is locked again."""
        return self.revalidations > 0 or self.ever_revalidated


@dataclass(frozen=True, slots=True)
class _FileSectionHandle:
    """A locked descriptor and the exact optional scope from which it was borrowed."""

    descriptor: int
    scope_key: tuple[int, str] | None = None
    scope: _ReusableDescriptorScope | None = None


def _finish(header: bytes, identifier: str) -> bytes:
    """Append the identifier bytes and the trailing checksum to an encoded header."""
    payload = header + identifier.encode("ascii")
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return payload + struct.pack(_CHECKSUM_FORMAT, checksum)


def encode_lease_record(record: LeaseRecord) -> bytes:
    """Encode a lease record into its fixed layout.

    Layout, little-endian: ``magic 8B | format_version u16 | flags u16 | owner_len u32 |
    epoch u64 | heartbeat_seq u64 | ttl_seconds f64 | wall_stamp f64 | superseded_epoch u64 |
    total_length u32 | reserved u32`` (64 bytes), then the ASCII owner identifier, then
    ``crc32 u32`` over everything before it. ``wall_stamp`` is diagnostic only.
    """
    owner = _validate_identifier(
        "owner_id", record.owner_id, limit=_MAX_STORED_OWNER_LENGTH
    ).encode("ascii")
    total = _LEASE_HEADER_SIZE + len(owner) + _CHECKSUM_SIZE
    header = struct.pack(
        _LEASE_HEADER,
        LEASE_MAGIC,
        LEASE_FORMAT_VERSION,
        _FLAG_ACTIVE if record.held else 0,
        len(owner),
        _require_index("epoch", record.epoch),
        _require_index("heartbeat_seq", record.heartbeat_seq),
        float(record.ttl_seconds),
        float(record.wall_stamp),
        _require_index("superseded_epoch", record.superseded_epoch),
        total,
        0,
    )
    return _finish(header, record.owner_id)


def encode_reader_record(record: ReaderRecord) -> bytes:
    """Encode a reader registration into its fixed layout.

    Layout, little-endian: ``magic 8B | format_version u16 | flags u16 | reader_len u32 |
    snapshot_lsn u64 | heartbeat_seq u64 | wall_stamp f64 | total_length u32 | reserved u32``
    (48 bytes), then the ASCII reader identifier, then ``crc32 u32``.
    """
    reader = _validate_identifier("reader_id", record.reader_id).encode("ascii")
    total = _READER_HEADER_SIZE + len(reader) + _CHECKSUM_SIZE
    header = struct.pack(
        _READER_HEADER,
        READER_MAGIC,
        READER_FORMAT_VERSION,
        _FLAG_ACTIVE if record.active else 0,
        len(reader),
        _require_index("snapshot_lsn", record.snapshot_lsn),
        _require_index("heartbeat_seq", record.heartbeat_seq),
        float(record.wall_stamp),
        total,
        0,
    )
    return _finish(header, record.reader_id)


def _decoded_identifier(
    raw: bytes, start: int, length: int, file: str, limit: int
) -> str:
    """Return the ASCII identifier stored at the given offset, or report corruption.

    The decoder accepts exactly what the encoder can produce and nothing else: a stored
    identifier carrying a separator, a NUL or an upper-case letter was not written by this build,
    and letting it through would turn a damaged record into a file name somewhere else.
    """
    # No length check here: the envelope has already established that the record is exactly
    # header + identifier + checksum bytes long, so this slice always has the length it asked
    # for. A guard for a case the caller has made impossible is a guard no input can reach.
    chunk = raw[start : start + length]
    try:
        text = chunk.decode("ascii")
    except UnicodeDecodeError as failure:
        raise GrafxCorruptionDetected(
            "The control record carries an identifier outside ASCII.", file=file
        ) from failure
    try:
        return _validate_identifier("stored identifier", text, limit=limit)
    except GrafxConfigurationError as failure:
        raise GrafxCorruptionDetected(
            "The control record carries an identifier this build could not have written.",
            file=file,
            identifier=repr(text),
        ) from failure


def _verify_envelope(
    raw: bytes, *, version: int, current: int, total: int, expected: int, file: str
) -> None:
    """Check the version, the declared length and the checksum of a control record."""
    if version > current:
        raise GrafxSchemaVersionMismatch(
            "The control record was written by a newer build of Okto Grafx.",
            file=file,
            found_version=version,
            supported_version=current,
        )
    if total != expected or total != len(raw):
        raise GrafxCorruptionDetected(
            "The control record declares a length that does not match its bytes.",
            file=file,
            declared=total,
            actual=len(raw),
        )
    stored = struct.unpack_from(_CHECKSUM_FORMAT, raw, total - _CHECKSUM_SIZE)[0]
    computed = zlib.crc32(raw[: total - _CHECKSUM_SIZE]) & 0xFFFFFFFF
    if stored != computed:
        raise GrafxCorruptionDetected(
            "The control record failed its own checksum.",
            file=file,
            stored_checksum=stored,
            computed_checksum=computed,
        )


def decode_lease_record(raw: bytes, *, file: str = LEASE_FILE_NAME) -> LeaseRecord:
    """Decode a lease record, refusing anything that is not exactly one whole valid record."""
    if len(raw) < _LEASE_HEADER_SIZE + _CHECKSUM_SIZE:
        raise GrafxCorruptionDetected(
            "The lease record is shorter than its own header.",
            file=file,
            length=len(raw),
        )
    try:
        fields = struct.unpack_from(_LEASE_HEADER, raw, 0)
    except struct.error as failure:
        raise GrafxCorruptionDetected(
            "The lease record header is unreadable.", file=file
        ) from failure
    magic, version, flags, owner_len = fields[0:4]
    epoch, sequence, ttl, wall, superseded, total = fields[4:10]
    if magic != LEASE_MAGIC:
        raise GrafxCorruptionDetected(
            "The lease record carries a foreign magic.", file=file
        )
    _verify_envelope(
        raw,
        version=version,
        current=LEASE_FORMAT_VERSION,
        total=total,
        expected=_LEASE_HEADER_SIZE + owner_len + _CHECKSUM_SIZE,
        file=file,
    )
    owner = _decoded_identifier(
        raw, _LEASE_HEADER_SIZE, owner_len, file, _MAX_STORED_OWNER_LENGTH
    )
    return LeaseRecord(
        owner_id=owner,
        epoch=epoch,
        heartbeat_seq=sequence,
        ttl_seconds=ttl,
        wall_stamp=wall,
        held=bool(flags & _FLAG_ACTIVE),
        superseded_epoch=superseded,
    )


def decode_reader_record(
    raw: bytes, *, file: str = READERS_DIRECTORY_NAME
) -> ReaderRecord:
    """Decode a reader registration, refusing anything that is not one whole valid record."""
    if len(raw) < _READER_HEADER_SIZE + _CHECKSUM_SIZE:
        raise GrafxCorruptionDetected(
            "The reader record is shorter than its own header.",
            file=file,
            length=len(raw),
        )
    try:
        fields = struct.unpack_from(_READER_HEADER, raw, 0)
    except struct.error as failure:
        raise GrafxCorruptionDetected(
            "The reader record header is unreadable.", file=file
        ) from failure
    magic, version, flags, reader_len, snapshot, sequence, wall, total, _reserved = (
        fields
    )
    if magic != READER_MAGIC:
        raise GrafxCorruptionDetected(
            "The reader record carries a foreign magic.", file=file
        )
    _verify_envelope(
        raw,
        version=version,
        current=READER_FORMAT_VERSION,
        total=total,
        expected=_READER_HEADER_SIZE + reader_len + _CHECKSUM_SIZE,
        file=file,
    )
    reader = _decoded_identifier(
        raw, _READER_HEADER_SIZE, reader_len, file, _MAX_IDENTIFIER_LENGTH
    )
    return ReaderRecord(
        reader_id=reader,
        snapshot_lsn=snapshot,
        heartbeat_seq=sequence,
        wall_stamp=wall,
        active=bool(flags & _FLAG_ACTIVE),
    )


class LocalProcessCoordinator:
    """Cross-process agreement on the writer epoch, on live readers and on short critical sections.

    Construction takes the two ports it needs plus the one thing a port cannot express: the real
    directory that holds the advisory lock files.

    Two modes, and the difference is exactly how far a section reaches:

    * ``lock_directory`` set -- the production mode for any database backed by a real file
      system. Sections are operating-system advisory locks, so they exclude across **processes**,
      across coordinator instances and across threads, and the kernel releases them when a
      participant dies without cleanup.
    * ``lock_directory=None`` -- the mode amendment A13 selects for ``:memory:``. Sections are
      **process-wide** locks keyed by the identity of the storage namespace: two coordinators
      over one in-memory device contend for real, two coordinators over two different devices do
      not, and nothing crosses a process boundary. That is the whole truth for this mode, and it
      is sound because a memory device is never shared between processes in the first place.
      Passing None while the device IS shared between processes would leave the epoch unguarded.

    The namespace of that second mode is the storage object itself, which is the right answer
    whenever two coordinators are given the same device. When they reach one namespace through
    different objects -- a wrapper, a recording decorator -- pass the same ``namespace`` token to
    both, because no port can tell the adapter that two device objects are the same store. The
    token is matched by IDENTITY, so it must be the same OBJECT: two equal strings built at
    runtime are two namespaces, and the coordinators holding them would not contend.

    Re-entrancy of ``exclusive()``: **allowed and counted**. The same coordinator, the same
    thread and the same section name may nest; the lock is taken once and released when the
    outermost block exits. A different thread, a different coordinator instance or a different
    process always contends for real, in both modes.
    """

    def __init__(
        self,
        storage: StorageDevice,
        clock: Clock,
        *,
        owner_id: str | None = None,
        lock_directory: str | None = None,
        namespace: object | None = None,
        control_directory: str = CONTROL_DIRECTORY,
        ttl_seconds: float = 5.0,
        owner_stall_threshold: float = 5.0,
        reader_stall_threshold: float = 15.0,
        section_timeout: float = 10.0,
        poll_interval: float = 0.005,
        metrics: MetricsSink | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """Bind the ports and the control-file layout this participant will use."""
        self._storage = storage
        self._clock = clock
        self._metrics = metrics
        if metrics is not None:
            # Registering is the emitter's job. Leaving it to the composition root works only
            # until the next component emits something the root did not know about, and the root
            # cannot know which metrics a component intends to emit.
            for name in EMITTED_METRICS:
                metrics.register(metric(name))
        self._sleeper: Callable[[float], None] = (
            time.sleep if sleeper is None else sleeper
        )
        configured = (
            _validate_identifier(
                "owner_id", owner_id, limit=_MAX_CONFIGURED_OWNER_LENGTH
            )
            if owner_id is not None
            else f"p{os.getpid():d}-{uuid.uuid4().hex[:12]}"
        )
        self._nonce: str = uuid.uuid4().hex[:_INSTANCE_NONCE_LENGTH]
        # The identity that reaches the disk carries the instance nonce. A configured owner name
        # can be repeated by a caller, and the lease record is the evidence BR-7 rests on: with a
        # shared name two coordinators answer to each other's record, and an out-of-band deletion
        # then lets both install the same epoch and both be told yes. The reader registry was
        # hardened this way already; the lease is where the harm is worse.
        self._configured_owner: str = configured
        self._owner: str = f"{configured}-{self._nonce}"
        self._reader_prefix: str = f"{self._owner}-r"
        control = _validate_identifier("control_directory", control_directory)
        self._control: str = control
        self._lease_file: str = f"{control}/{LEASE_FILE_NAME}"
        self._readers_prefix: str = f"{control}/{READERS_DIRECTORY_NAME}/"
        self._ttl: float = _require_positive("ttl_seconds", ttl_seconds)
        self._owner_stall: float = _require_positive(
            "owner_stall_threshold", owner_stall_threshold
        )
        self._reader_stall: float = _require_positive(
            "reader_stall_threshold", reader_stall_threshold
        )
        self._section_timeout: float = _require_non_negative(
            "section_timeout", section_timeout
        )
        self._poll: float = _require_positive("poll_interval", poll_interval)
        self._lock_directory: str | None = self._prepare_lock_directory(lock_directory)
        self._namespace: object = storage if namespace is None else namespace

        self._state_lock = threading.RLock()
        self._sections: dict[tuple[int, str], object] = {}
        self._descriptor_scopes: dict[tuple[int, str], _ReusableDescriptorScope] = {}
        self._held: Lease | None = None
        self._installed: Lease | None = None
        self._installed_epochs: set[Epoch] = set()
        self._basis: LeaseRecord | None = None
        self._basis_seen: bool = False
        self._owner_key: tuple[str, int, int] | None = None
        self._owner_key_at: float = clock.monotonic()
        self._readers: dict[str, ReaderHandle] = {}
        self._reader_sequences: dict[str, int] = {}
        self._reader_samples: dict[str, tuple[int, float]] = {}
        self._stray_samples: dict[str, float] = {}
        self._reader_counter: int = 0
        self._lease_slots: TwoSlotControlRecordStore | None = None
        self._control_database_uuid: bytes | None = None
        self._control_format_version: int = 1

    def __repr__(self) -> str:
        """Return a representation naming the owner and the control files this coordinator uses."""
        return f"LocalProcessCoordinator(owner_id={self._owner!r}, lease_file={self._lease_file!r})"

    def bind_control_record_format(
        self, *, database_uuid: bytes, format_version: int
    ) -> None:
        """Bind the on-disk control envelope after the database identity has been opened.

        The coordinator is constructed before ``grafx.meta`` can be trusted because it supplies
        the first-open section.  No lease operation happens in that interval.  Assembly calls
        this door exactly once after opening (and, for writable legacy databases, upgrading) the
        identity.  Directly composed coordinators that never call it retain the format-1
        whole-file protocol, preserving their existing contract.
        """
        if type(format_version) is not int or format_version not in {1, 2}:
            raise GrafxConfigurationError(
                "The coordinator control format must be version 1 or 2.",
                field="format_version",
                value=repr(format_version),
            )
        with self._state_lock:
            if self._control_database_uuid is not None and (
                self._control_database_uuid != database_uuid
                or self._control_format_version != format_version
            ):
                raise GrafxConfigurationError(
                    "A coordinator cannot be rebound to a different database or control format.",
                    field="control_format",
                    state="different_binding",
                )
            if self._control_database_uuid == database_uuid:
                return
            if self._held is not None or self._readers:
                raise GrafxConfigurationError(
                    "The control format must be bound before leases or readers are acquired.",
                    field="control_format",
                    state="active",
                )
            if format_version == 1:
                if self._lease_slots is not None:
                    raise GrafxConfigurationError(
                        "A coordinator already bound to format 2 cannot be rebound to format 1.",
                        field="control_format",
                        state="already_bound",
                    )
                self._control_database_uuid = database_uuid
                self._control_format_version = 1
                return
            nonce = int.from_bytes(uuid.uuid4().bytes[:8], "little")
            candidate = TwoSlotControlRecordStore(
                self._storage,
                file=self._lease_file,
                record_kind=ControlRecordKind.LEASE,
                database_uuid=database_uuid,
                file_nonce=nonce,
                temporary=f"{self._lease_file}.{self._owner}.tmp",
                read_if_exists=read_control_if_exists,
            )
            if self._lease_slots is not None:
                return
            self._lease_slots = candidate
            self._control_database_uuid = database_uuid
            self._control_format_version = 2
            pin = getattr(self._storage, "pin_descriptor", None)
            if callable(pin):
                pin(self._lease_file)

    # --- identity and epoch --------------------------------------------------------------------

    def owner_id(self) -> str:
        """Return the stable identity of this participant."""
        return self._owner

    def current_epoch(self) -> Epoch:
        """Return the epoch published by the lease file; 0 when none was ever published."""
        return self._published_epoch()

    def validate_epoch(self, epoch: Epoch) -> None:
        """Raise GrafxStaleEpoch unless this exact epoch is the published one and is still held.

        Three conditions, all necessary and all checked before the caller can reach the device
        (BR-7, AC-6): a lease record exists, it is held, and its epoch is the one offered. The
        published record is read every time. There is deliberately no staleness window, no cache
        and no remembered high-water mark: CONTRACT.md section 4.3 states the guarantee
        unconditionally and section 8.5 step 2 places a validation OUTSIDE the commit section
        precisely as the guard that runs before any device call, so anything that answered from
        memory would turn that step into a no-op for the one caller it exists to stop.

        A remembered ceiling did live here, refusing an epoch below the highest ever observed
        without touching the device. It was removed rather than repaired. Epochs only rise within
        ONE control plane, and C6 restoring from quarantine is the sanctioned way a control plane
        is replaced; because steps 2 and 3.1 make a committing writer a validate-only caller, a
        guard that refused from memory and never re-read wedged that writer for the life of the
        process. A read of a small file is cheap; being wrong about who may write is not.
        """
        offered = _require_index("epoch", epoch)
        held = self._held
        if held is None or held.epoch != offered:
            raise GrafxStaleEpoch(
                "This participant does not hold the lease whose epoch it offered.",
                epoch=offered,
                held_epoch=None if held is None else held.epoch,
                owner_id=self._owner,
            )
        record = self._published_record()
        if record is None:
            raise GrafxStaleEpoch(
                "No writer epoch is published for this database.",
                epoch=offered,
                owner_id=self._owner,
            )
        if not record.held:
            raise GrafxStaleEpoch(
                "The writer lease is not held, so no epoch authorises a write.",
                epoch=offered,
                published_epoch=record.epoch,
                owner_id=self._owner,
            )
        if record.epoch != offered:
            raise GrafxStaleEpoch(
                "The offered epoch is not the published one.",
                epoch=offered,
                published_epoch=record.epoch,
                published_owner=record.owner_id,
                owner_id=self._owner,
            )
        if record.owner_id != self._owner:
            raise GrafxStaleEpoch(
                "The published lease at this epoch belongs to another participant.",
                epoch=offered,
                published_owner=record.owner_id,
                owner_id=self._owner,
            )

    # --- writer lease --------------------------------------------------------------------------

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        """Take the writer role, waiting up to the timeout, and raise GrafxLeaseTimeout on expiry.

        The lease is granted when no record exists, when the previous owner released it, or when
        the previous owner has stalled beyond the configured threshold measured with this
        participant own monotonic clock. Every grant that changes ownership increments the epoch.
        """
        budget = _require_non_negative("timeout", timeout)
        started = self._clock.monotonic()
        deadline = started + budget
        allowance = self._iteration_budget(budget)
        iterations = 0
        while True:
            iterations += 1
            now = self._clock.monotonic()
            record = self._observe_lease(self._read_lease_record(), now)
            existing = self._held
            if (
                existing is not None
                and record is not None
                and record.held
                and record.owner_id == self._owner
                and record.epoch == existing.epoch
            ):
                self._observe_wait("granted", now - started)
                return existing
            vacant = record is None or not record.held
            stalled = (
                not vacant and self._owner_stalled(now, self._owner_stall) is not None
            )
            if vacant or stalled:
                claimed = self._try_claim(record, budget=max(deadline - now, 0.0))
                if claimed is not None:
                    self._observe_wait(
                        "takeover" if stalled else "granted", claimed[1] - started
                    )
                    return claimed[0]
            now = self._clock.monotonic()
            if now >= deadline or iterations >= allowance:
                self._observe_wait("timeout", now - started)
                raise GrafxLeaseTimeout(
                    "The writer lease was not granted before the timeout elapsed.",
                    timeout_seconds=budget,
                    waited_seconds=now - started,
                    owner_id=self._owner,
                )
            self._sleep(min(self._poll, deadline - now))

    def renew_lease(self, lease: Lease) -> Lease:
        """Advance the heartbeat of a lease THIS participant holds, or raise GrafxLeaseStolen.

        The identity that decides is this participant own, never the one the argument claims. A
        lease is a durable statement about a process, so honouring somebody else lease would let
        a participant that holds nothing forge the heartbeat of another owner -- and one forged
        heartbeat resets the stall baseline of every observer, which is exactly how a dead owner
        would be kept alive forever against FR-7 and AC-7.
        """
        if lease.owner_id != self._owner:
            raise GrafxLeaseStolen(
                "This lease belongs to another participant and cannot be renewed here.",
                owner_id=self._owner,
                lease_owner=lease.owner_id,
                epoch=lease.epoch,
            )
        with self.exclusive(LEASE_SECTION, timeout=self._section_timeout):
            now = self._clock.monotonic()
            held = self._held
            if held is None or held.epoch != lease.epoch:
                raise GrafxLeaseStolen(
                    "This participant does not hold the lease it was asked to renew.",
                    owner_id=self._owner,
                    epoch=lease.epoch,
                    held_epoch=None if held is None else held.epoch,
                )
            record = self._observe_lease(self._read_lease_record(), now)
            if (
                record is None
                or not record.held
                or record.owner_id != self._owner
                or record.epoch != lease.epoch
            ):
                self._held = None
                raise GrafxLeaseStolen(
                    "The writer lease is no longer held by this owner at this epoch.",
                    owner_id=lease.owner_id,
                    epoch=lease.epoch,
                    published_owner=None if record is None else record.owner_id,
                    published_epoch=None if record is None else record.epoch,
                )
            renewed = replace(
                record,
                heartbeat_seq=record.heartbeat_seq + 1,
                ttl_seconds=self._ttl,
                wall_stamp=self._clock.wall(),
            )
            self._publish_lease(renewed)
            granted = Lease(
                owner_id=self._owner,
                epoch=renewed.epoch,
                acquired_monotonic=lease.acquired_monotonic,
                heartbeat_seq=renewed.heartbeat_seq,
                ttl_seconds=renewed.ttl_seconds,
            )
            self._held = granted
            return granted

    def release_lease(self, lease: Lease) -> None:
        """Give the writer role up so the next participant does not have to wait out the stall.

        Releasing a lease of THIS participant that was already taken over is a no-op: closing a
        database must never raise because somebody else already won the epoch. Releasing a lease
        this participant never held is refused outright, because disowning a lease nobody asked
        to give up hands the writer role to whoever asks next, with no stall to wait out.

        The identity that decides is the same one the renewal uses: not the owner name the
        argument carries, which two participants can share by a configuration mistake, but the
        lease this coordinator itself installed.
        """
        if lease.owner_id != self._owner:
            raise GrafxLeaseStolen(
                "This lease belongs to another participant and cannot be released here.",
                owner_id=self._owner,
                lease_owner=lease.owner_id,
                epoch=lease.epoch,
            )
        with self._state_lock:
            held_here = lease.epoch in self._installed_epochs
            installed = self._installed
        if not held_here:
            raise GrafxLeaseStolen(
                "This participant never held the lease it was asked to release.",
                owner_id=self._owner,
                epoch=lease.epoch,
                last_installed_epoch=None if installed is None else installed.epoch,
            )
        with self.exclusive(LEASE_SECTION, timeout=self._section_timeout):
            now = self._clock.monotonic()
            record = self._observe_lease(self._read_lease_record(), now)
            if (
                record is not None
                and record.held
                and record.owner_id == self._owner
                and record.epoch == lease.epoch
            ):
                self._publish_lease(
                    replace(
                        record,
                        held=False,
                        heartbeat_seq=record.heartbeat_seq + 1,
                        wall_stamp=self._clock.wall(),
                    )
                )
            current = self._held
            if current is not None and current.epoch == lease.epoch:
                self._held = None

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None:
        """Return a report when the published heartbeat has not moved for longer than the threshold.

        The interval is measured entirely with this observer own monotonic clock, so the wall
        clock of either process is irrelevant. A first observation can only establish the
        baseline and therefore never reports a death: proof of a stall needs two samples.
        """
        threshold = _require_positive("stall_threshold", stall_threshold)
        now = self._clock.monotonic()
        record = self._observe_lease(self._read_lease_record(), now)
        if record is None or not record.held:
            return None
        stall = self._owner_stalled(now, threshold)
        if stall is None:
            return None
        return DeadOwnerReport(
            owner_id=record.owner_id,
            last_heartbeat_seq=record.heartbeat_seq,
            observed_stall_seconds=stall,
        )

    def takeover(self) -> Lease:
        """Increment the epoch and become the owner, atomically against every concurrent takeover.

        The operation is a compare-and-set against the record this coordinator last observed: it
        succeeds only while the lease file still says exactly what it said when the decision to
        take over was made. Two participants racing over the same dead owner therefore produce
        one winner and exactly one epoch increment; the loser raises GrafxLeaseStolen and, having
        refreshed its view, fails its own writes with GrafxStaleEpoch (AC-7).

        Evidence is required as well as atomicity. A held lease is taken over only when this
        participant has itself observed the heartbeat stall beyond its configured threshold; an
        owner that is merely inconvenient raises the retryable GrafxLeaseTimeout, and so does an
        owner that advanced its heartbeat after the observation, because it is demonstrably alive.

        The threshold applied here is ``owner_stall_threshold`` from construction, never the one
        passed to ``detect_dead_owner``. The two can therefore disagree, and the divergence is
        deliberately one-way: asking ``detect_dead_owner`` for a SHORTER threshold cannot lower
        the bar this method sets, so a caller can be more cautious than the configuration but
        never less. Amendment A13 maps both from ``lease_ttl_seconds``, so a wired database has
        them equal.

        The wait is reported through oktografx_lease_wait_seconds, which is the metric TS-7 reads
        to see how long the surviving participant took to replace a killed one.
        """
        started = self._clock.monotonic()
        try:
            lease = self._perform_takeover(started)
        except GrafxLeaseTimeout:
            self._observe_wait("timeout", self._clock.monotonic() - started)
            raise
        self._observe_wait("takeover", self._clock.monotonic() - started)
        return lease

    def _perform_takeover(self, now: float) -> Lease:
        """Run the compare-and-set of the takeover, without reporting anything about the wait."""
        if not self._basis_seen:
            self._observe_lease(self._read_lease_record(), now)
        basis = self._basis
        if (
            basis is not None
            and basis.held
            and self._owner_stalled(now, self._owner_stall) is None
        ):
            raise GrafxLeaseTimeout(
                "The lease owner has not been observed to stall, so there is nothing to take over.",
                owner_id=self._owner,
                published_owner=basis.owner_id,
                published_epoch=basis.epoch,
                stall_threshold_seconds=self._owner_stall,
            )
        with self.exclusive(LEASE_SECTION, timeout=self._section_timeout):
            moment = self._clock.monotonic()
            current = self._read_lease_record()
            if not _same_record(current, basis):
                self._observe_lease(current, moment)
                if (
                    basis is not None
                    and current is not None
                    and current.held
                    and current.owner_id == basis.owner_id
                    and current.epoch == basis.epoch
                ):
                    raise GrafxLeaseTimeout(
                        "The lease owner advanced its heartbeat, so it is not dead after all.",
                        owner_id=self._owner,
                        published_owner=current.owner_id,
                        published_epoch=current.epoch,
                    )
                raise GrafxLeaseStolen(
                    "The lease changed between the observation and the takeover.",
                    owner_id=self._owner,
                    observed_epoch=0 if basis is None else basis.epoch,
                    published_epoch=None if current is None else current.epoch,
                    published_owner=None if current is None else current.owner_id,
                )
            return self._install(current, moment)

    # --- reader registry -----------------------------------------------------------------------

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle:
        """Publish a live reader and the snapshot it pins, with no lock and no writer involved.

        Registration writes one file that belongs to this reader alone, so readers never block
        writers and writers never block readers (FR-2).
        """
        pinned = _require_index("snapshot_lsn", snapshot_lsn)
        # No fallible host clock read may happen after the durable record is published but
        # before this coordinator adopts its handle. Otherwise register_reader could raise
        # without returning the only capability able to withdraw an own record -- and own
        # registrations are deliberately never TTL-pruned. An earlier sample is conservative:
        # it can only make the first heartbeat due sooner.
        observed_at = self._clock.monotonic()
        with self._state_lock:
            self._reader_counter += 1
            reader_id = f"{self._reader_prefix}{self._reader_counter:04d}"
        record = ReaderRecord(
            reader_id=reader_id,
            snapshot_lsn=pinned,
            heartbeat_seq=1,
            wall_stamp=self._clock.wall(),
            active=True,
        )
        self._publish_reader(record)
        handle = ReaderHandle(reader_id=reader_id, snapshot_lsn=pinned)
        with self._state_lock:
            self._readers[reader_id] = handle
            self._reader_sequences[reader_id] = 1
            self._reader_samples[reader_id] = (1, observed_at)
        return handle

    def refresh_reader(self, handle: ReaderHandle) -> None:
        """Advance the heartbeat of a registration, re-creating its file when it was pruned.

        Re-creating matters: pruning a reader that had merely gone quiet must never end its
        snapshot, because BR-10 forbids evicting a live reader under any circumstance.
        """
        reader_id = _validate_identifier("reader_id", handle.reader_id)
        pinned = _require_index("snapshot_lsn", handle.snapshot_lsn)
        with self._state_lock:
            known = reader_id in self._readers
        if not known:
            raise GrafxUnsupportedOperation(
                "This coordinator did not issue that reader registration, or already withdrew "
                "it, so it cannot prove anything about whether it is alive.",
                reader_id=reader_id,
                owner_id=self._owner,
                issued_here=self._issued_here(reader_id),
            )
        with self._state_lock:
            sequence = self._reader_sequences.get(reader_id, 0) + 1
            self._reader_sequences[reader_id] = sequence
            self._readers[reader_id] = handle
        self._publish_reader(
            ReaderRecord(
                reader_id=reader_id,
                snapshot_lsn=pinned,
                heartbeat_seq=sequence,
                wall_stamp=self._clock.wall(),
                active=True,
            )
        )
        with self._state_lock:
            self._reader_samples[reader_id] = (sequence, self._clock.monotonic())

    def unregister_reader(self, handle: ReaderHandle) -> None:
        """Remove a registration and release the snapshot it pinned. Repeating it is a no-op."""
        reader_id = _validate_identifier("reader_id", handle.reader_id)
        if not self._issued_here(reader_id):
            raise GrafxUnsupportedOperation(
                "This coordinator did not issue that reader registration, so withdrawing it "
                "here would end a snapshot another participant is still reading under.",
                reader_id=reader_id,
                owner_id=self._owner,
            )
        with self._state_lock:
            self._readers.pop(reader_id, None)
            self._reader_sequences.pop(reader_id, None)
            self._reader_samples.pop(reader_id, None)
        self._discard(self._reader_file(reader_id))

    def reader_horizon(self) -> Lsn | None:
        """Return the minimum snapshot LSN over live readers, or None when none is live.

        A reader whose heartbeat has been still for longer than the configured threshold is
        **pruned**: it is dead, and its registration is removed. That is not the same act as
        evicting a live reader, which BR-10 forbids and this method never does, no matter how far
        behind the horizon it holds. The registrations this participant owns are never pruned at
        all, because for those the answer is not an inference: the process is running this code.
        """
        now = self._clock.monotonic()
        horizon: Lsn | None = None
        observed: set[str] = set()
        strays: list[str] = []
        for name in self._list_files(self._readers_prefix):
            if name.endswith(TEMPORARY_SUFFIX):
                strays.append(name)
                continue
            if not name.endswith(READER_FILE_SUFFIX):
                continue
            record = self._read_reader_record(name)
            if record is None or not record.active:
                self._discard(name)
                continue
            observed.add(record.reader_id)
            if not self._reader_alive(record, now):
                self._discard(name)
                continue
            if horizon is None or record.snapshot_lsn < horizon:
                horizon = record.snapshot_lsn
        self._sweep_reader_temporaries(strays, now)
        with self._state_lock:
            for reader_id in [
                key for key in self._reader_samples if key not in observed
            ]:
                if reader_id not in self._readers:
                    self._reader_samples.pop(reader_id, None)
        return horizon

    def observe_reader_horizon(self) -> Lsn | None:
        """Return a conservative reader horizon without pruning or publishing anything.

        This optional capability exists for read-only diagnostics. Unlike
        :meth:`reader_horizon`, it makes no liveness inference: every decodable active final
        registration remains a pin even when its heartbeat would be TTL-stalled. Empty,
        inactive and temporary records are ignored but deliberately left untouched. Corrupt
        final records still fail closed because their snapshot is unknowable.
        """
        horizon: Lsn | None = None
        for name in self._list_files(self._readers_prefix):
            if name.endswith(TEMPORARY_SUFFIX) or not name.endswith(READER_FILE_SUFFIX):
                continue
            record = self._read_reader_record(name)
            if record is None or not record.active:
                continue
            if horizon is None or record.snapshot_lsn < horizon:
                horizon = record.snapshot_lsn
        return horizon

    # --- critical sections ---------------------------------------------------------------------

    def exclusive(self, name: str, *, timeout: float) -> AbstractContextManager[None]:
        """Enter a short cross-process critical section, raising GrafxLeaseTimeout on expiry.

        The section is meant for the commit window and for the takeover: it is held across a
        read-modify-write of a control file and nothing else. Holding it across long work is a
        caller-side defect, because every other participant of the database waits behind it.
        The block releases the lock on the way out, including when the body raises.
        """
        if not isinstance(name, str):
            raise _reject("The section name must be a string.", value=repr(name))
        return self._section(
            _validate_identifier("section name", name.lower()), timeout
        )

    def reuse_unlocked_section_descriptor(
        self, name: str
    ) -> AbstractContextManager[None]:
        """Keep only an unlocked file descriptor open for a bounded caller-owned interval.

        This optional, private-performance capability never keeps the advisory lock itself:
        every :meth:`exclusive` entry still acquires the operating-system lock and every exit
        still releases it. Reuse is restricted to the same thread and normalized section name,
        and any uncertain acquire or release discards the descriptor instead of caching it.
        Coordinators without a lock directory use their normal process-local lock unchanged.
        """
        if not isinstance(name, str):
            raise _reject("The section name must be a string.", value=repr(name))
        normalized = _validate_identifier("section name", name.lower())
        return self._reuse_unlocked_section_descriptor(normalized)

    def _reuse_revalidated_unlocked_section_descriptor(
        self, name: str
    ) -> AbstractContextManager[None] | None:
        """Keep an unlocked descriptor across an interval whose length the CALLER decides.

        The short variant above lives inside one engine call, so the window in which the lock
        file could be replaced is bounded by that call. This one may span separate statements of
        one transaction, and a caller may hold it open for as long as it likes. Over that window
        the descriptor's file may be unlinked and recreated, and a descriptor kept from before
        the replacement names an orphaned inode: locking it always succeeds while another
        process locks the live file, so mutual exclusion would be lost with no error anywhere.

        The longer window is therefore paid for with a proof rather than with trust. Before a
        parked descriptor is locked again its physical identity is compared with the identity
        the path names now, and any mismatch or any doubt closes it and opens the file afresh.
        Every ``exclusive`` still takes and releases the real operating-system lock; only the
        open descriptor is reused, and only while it is still provably the same file.

        ``None`` means this coordinator has nothing to reuse -- it holds no lock directory, so
        its sections are process-local. Answering with a live no-op context would make a caller
        pay a frame per statement to enter an interval that can never park anything.
        """
        if not isinstance(name, str):
            raise _reject("The section name must be a string.", value=repr(name))
        normalized = _validate_identifier("section name", name.lower())
        if self._lock_directory is None:
            return None
        return self._reuse_unlocked_section_descriptor(normalized, revalidated=True)

    @contextmanager
    def _reuse_unlocked_section_descriptor(
        self, name: str, *, revalidated: bool = False
    ) -> Iterator[None]:
        """Bound descriptor reuse to one thread and one exact section name."""
        if self._lock_directory is None:
            yield
            return

        key = (threading.get_ident(), name)
        with self._state_lock:
            scope = self._descriptor_scopes.get(key)
            if scope is None or scope.closing:
                scope = _ReusableDescriptorScope()
                self._descriptor_scopes[key] = scope
            else:
                scope.depth += 1
            if revalidated:
                scope.revalidations += 1
                scope.ever_revalidated = True
        try:
            yield
        finally:
            descriptor: int | None = None
            with self._state_lock:
                current = self._descriptor_scopes.get(key)
                if current is scope:
                    if revalidated:
                        scope.revalidations -= 1
                    scope.depth -= 1
                    if scope.depth == 0:
                        scope.closing = True
                        if not scope.borrowed:
                            self._descriptor_scopes.pop(key, None)
                            descriptor = scope.descriptor
                            scope.descriptor = None
            if descriptor is not None:
                self._close_file_descriptor(descriptor)

    @contextmanager
    def _section(self, name: str, timeout: float) -> Iterator[None]:
        """Take the named section, counting re-entry from the same thread of this coordinator."""
        budget = _require_non_negative("timeout", timeout)
        key = (threading.get_ident(), name)
        with self._state_lock:
            reentered = key in self._sections
        if reentered:
            # The frame that took the lock is the frame that releases it, so a nested frame has
            # nothing to do on the way in or on the way out.
            yield
            return
        handle = self._take_lock(name, budget)
        with self._state_lock:
            self._sections[key] = handle
        try:
            yield
        finally:
            with self._state_lock:
                self._sections.pop(key, None)
            self._drop_lock(handle)

    # --- internals: lease state ------------------------------------------------------------------

    def _install(self, basis: LeaseRecord | None, moment: float) -> Lease:
        """Publish this participant as the owner of the next epoch and return the lease."""
        previous = 0 if basis is None else basis.epoch
        record = LeaseRecord(
            owner_id=self._owner,
            epoch=previous + 1,
            heartbeat_seq=1,
            ttl_seconds=self._ttl,
            wall_stamp=self._clock.wall(),
            held=True,
            superseded_epoch=previous,
        )
        self._publish_lease(record)
        self._sweep_lease_temporaries()
        lease = Lease(
            owner_id=self._owner,
            epoch=record.epoch,
            acquired_monotonic=moment,
            heartbeat_seq=record.heartbeat_seq,
            ttl_seconds=record.ttl_seconds,
        )
        self._held = lease
        # Kept after the lease is given up or taken away, so that releasing a lease this
        # participant really did hold stays quiet while releasing one it never held is refused.
        # The set holds every epoch this instance installed, which is one entry per takeover and
        # therefore one entry per death of another participant: a handful over a process life.
        self._installed = lease
        self._installed_epochs.add(lease.epoch)
        return lease

    def _try_claim(
        self, basis: LeaseRecord | None, *, budget: float
    ) -> tuple[Lease, float] | None:
        """Attempt the compare-and-set that installs this participant, returning None when it lost.

        The section attempt is bounded by what is left of the budget the caller gave to the
        acquisition, so waiting for the section can never outlast the timeout that was asked for.
        """
        try:
            with self.exclusive(
                LEASE_SECTION, timeout=min(self._section_timeout, budget)
            ):
                moment = self._clock.monotonic()
                current = self._read_lease_record()
                if not _same_record(current, basis):
                    self._observe_lease(current, moment)
                    return None
                return (self._install(current, moment), moment)
        except GrafxLeaseTimeout:
            return None

    def _owner_stalled(self, now: float, threshold: float) -> float | None:
        """Return how long the heartbeat has been still, when that exceeds the threshold."""
        with self._state_lock:
            if self._owner_key is None:
                # Defensive: every caller checks that the record is held before asking, so this
                # is unreachable today. It stays because the alternative to returning "no stall"
                # for an unobserved lease is measuring one against a baseline that was never set.
                return None
            stall = now - self._owner_key_at
        return stall if stall > threshold else None

    def _observe_lease(
        self, record: LeaseRecord | None, now: float
    ) -> LeaseRecord | None:
        """Record one observation of the lease file and return the record that was observed."""
        # No held term here: every caller establishes that the lease is held before it asks
        # about a stall, and the 2x2 shows either check alone holds the property. Keeping
        # both would be a guard no input can distinguish from its neighbour.
        key = record.liveness_key() if record is not None else None
        with self._state_lock:
            self._basis = record
            self._basis_seen = True
            if key != self._owner_key:
                self._owner_key = key
                self._owner_key_at = now
        return record

    def _published_record(self) -> LeaseRecord | None:
        """Return the lease record as the device holds it right now, with no cache in between."""
        return self._observe_lease(self._read_lease_record(), self._clock.monotonic())

    def _published_epoch(self) -> Epoch:
        """Return the epoch of the published lease record, or 0 when no record exists."""
        record = self._published_record()
        return 0 if record is None else record.epoch

    # --- internals: reader state ------------------------------------------------------------------

    def _sweep_reader_temporaries(self, strays: list[str], now: float) -> None:
        """Release the temporaries of publications that will never finish, and only those.

        A temporary is evidence of nothing on its own: the one being written right now by another
        participant looks exactly like the one a process left behind when it died, and a first
        registration has no record yet to prove it is alive. So the rule is the same one this
        component uses everywhere else -- evidence measured over time on the local monotonic
        clock. A publication lives for microseconds; a temporary still present after a reader is
        allowed to stay silent belongs to nobody. Sweeping by anything else deletes another
        participant registration mid-flight, which is the eviction BR-10 forbids.
        """
        with self._state_lock:
            for name in strays:
                first_seen = self._stray_samples.setdefault(name, now)
                if (now - first_seen) > self._reader_stall:
                    self._discard(name)
                    self._stray_samples.pop(name, None)
            for name in [key for key in self._stray_samples if key not in strays]:
                self._stray_samples.pop(name, None)

    def _issued_here(self, reader_id: str) -> bool:
        """Return whether this coordinator instance minted that reader identifier.

        Identity is decided by what this coordinator issued, never by what the argument claims,
        which is the same rule the lease uses. The per-instance nonce in the prefix is what makes
        the answer exact even when two coordinators share an owner identifier, and the counter is
        checked as well so the answer means "I minted this one" rather than "this looks like
        something I could have minted".
        """
        if not reader_id.startswith(self._reader_prefix):
            return False
        suffix = reader_id[len(self._reader_prefix) :]
        if not suffix.isdigit():
            return False
        with self._state_lock:
            return 0 < int(suffix) <= self._reader_counter

    def _reader_file(self, reader_id: str) -> str:
        """Return the storage name of one reader registration."""
        return f"{self._readers_prefix}{reader_id}{READER_FILE_SUFFIX}"

    def _reader_alive(self, record: ReaderRecord, now: float) -> bool:
        """Return whether a reader registration still counts as live for the horizon."""
        with self._state_lock:
            if record.reader_id in self._readers:
                return True
            sample = self._reader_samples.get(record.reader_id)
            if sample is None or sample[0] != record.heartbeat_seq:
                self._reader_samples[record.reader_id] = (record.heartbeat_seq, now)
                return True
            return (now - sample[1]) <= self._reader_stall

    # --- internals: control files -------------------------------------------------------------

    def _read_lease_record(self) -> LeaseRecord | None:
        """Read and decode the lease file, returning None when nothing is published yet."""
        if self._lease_slots is not None:
            record = self._lease_slots.read()
            return (
                None
                if record is None
                else decode_lease_record(record.payload, file=self._lease_file)
            )
        return self._read_record(
            self._lease_file, decode_lease_record, empty_is_absent=False
        )

    def _read_reader_record(self, name: str) -> ReaderRecord | None:
        """Read and decode one reader registration, returning None when the file went away."""
        return self._read_record(name, decode_reader_record, empty_is_absent=True)

    def _read_record(
        self, name: str, decode: Callable[..., _Record], *, empty_is_absent: bool
    ) -> _Record | None:
        """Read and decode a whole control record, retrying across a concurrent replacement.

        Reading takes two calls, the size and the bytes, and another participant may publish a
        record of a different length between them. That is a benign race, not damage, so the
        read is repeated; only a mismatch that survives every attempt is reported as corruption.
        A file that disappears mid-read simply reads as absent, which is what a pruned reader
        registration is.
        """
        storage = self._storage
        damage: GrafxError | None = None
        attempts = 0
        backoff = self._poll
        while True:
            attempts += 1
            try:
                # The probe is a device call like any other: it can fail for the same transient
                # reasons as the read that follows it, and leaving it outside this guard made a
                # sharing violation on the probe fatal where one line later it was ridden out.
                if not storage.exists(name):
                    return None
                size = storage.log_size(name)
                if size == 0:
                    if empty_is_absent:
                        return None
                    # The file existing is itself evidence that something was published, and
                    # every other malformed shape of it fails closed. Read as absence, an empty
                    # lease restarts the epoch lineage at 1 and re-issues a number a live
                    # participant may still hold.
                    raise GrafxCorruptionDetected(
                        "The control file exists but holds no record at all.", file=name
                    )
                raw = storage.read_log(name, 0, size)
                if len(raw) != size:
                    raise GrafxCorruptionDetected(
                        "The control file returned fewer bytes than its declared size.",
                        file=name,
                        declared=size,
                        actual=len(raw),
                    )
                return decode(raw, file=name)
            except GrafxCorruptionDetected as error:
                # A length that does not match is the signature of a replacement between the two
                # calls, so the immediate retry is the fix; only damage survives all of them.
                damage = error
            except GrafxError as error:
                # A typed failure already carries the answer the device chose, and that class is
                # part of the answer: a version mismatch is permanent, a storage error may not be,
                # and a caller obeying A47 branches on what arrives. Re-labelling it here is how
                # the publish path used to tell callers to retry a condition it had itself given
                # up on after one attempt.
                if attempts >= _READ_ATTEMPTS or not _worth_retrying(error):
                    error.details.setdefault("file", name)
                    error.details["attempts"] = attempts
                    raise
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)
                continue
            except OSError as error:
                # Being unable to READ a file says nothing about the bytes in it. Reporting that
                # as corruption would manufacture an integrity incident, and in this engine an
                # integrity incident means truncation, quarantine and forensic ledger entries
                # (A11-revised, FR-8, FR-10).
                if attempts >= _READ_ATTEMPTS or not _worth_retrying(error):
                    # A straddle seen on an earlier attempt is NOT evidence against the bytes:
                    # this module calls it a benign race, and only a mismatch that survives every
                    # attempt is damage. Raising it here because a later attempt failed to reach
                    # the file manufactures the integrity incident FR-8 and FR-10 act on.
                    raise _storage_failure(
                        "The control file could not be read.",
                        error,
                        attempts=attempts,
                        file=name,
                    ) from error
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)
                continue
            if attempts >= _READ_ATTEMPTS:
                # Reaching here means the corruption arm caught the last attempt, and that arm
                # always records what it caught -- so there is exactly one thing to raise. An
                # alternative branch here would be one no input can take.
                raise damage

    def _list_files(self, prefix: str) -> tuple[str, ...]:
        """List control files under a prefix, riding out a transient failure of the device."""
        attempts = 0
        backoff = self._poll
        while True:
            attempts += 1
            try:
                return self._storage.list_files(prefix)
            except GrafxError as failure:
                if attempts >= _READ_ATTEMPTS or not _worth_retrying(failure):
                    failure.details.setdefault("file", prefix)
                    failure.details["attempts"] = attempts
                    raise
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)
            except OSError as failure:
                if attempts >= _READ_ATTEMPTS or not _worth_retrying(failure):
                    raise _storage_failure(
                        "The control directory could not be listed.",
                        failure,
                        attempts=attempts,
                        file=prefix,
                    ) from failure
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)

    def _publish_lease(self, record: LeaseRecord) -> None:
        """Write the lease record atomically and adopt it as this participant own observation."""
        payload = encode_lease_record(record)
        if self._lease_slots is None:
            self._publish(self._lease_file, payload)
        else:
            self._lease_slots.publish(payload)
        self._observe_lease(record, self._clock.monotonic())

    def _publish_reader(self, record: ReaderRecord) -> None:
        """Write one reader registration atomically."""
        self._publish(self._reader_file(record.reader_id), encode_reader_record(record))

    def _publish(self, target: str, payload: bytes) -> None:
        """Publish a whole control record, riding out the access failures of a shared file system.

        Another participant reading the lease file, an antivirus scanner or a search indexer can
        make the replacement fail with a sharing violation on Windows (TR-3). That is transient,
        and every attempt starts from a fresh temporary file, so repeating the sequence is safe.
        A full device, a failed barrier or damaged bytes are answers rather than obstacles and
        are reported at once.
        """
        attempts = 0
        backoff = self._poll
        while True:
            attempts += 1
            try:
                self._publish_once(target, payload)
                return
            except GrafxError as failure:
                if attempts >= _PUBLISH_ATTEMPTS or not _worth_publishing_again(
                    failure
                ):
                    # The class the device chose is part of the answer and is kept: A25 counts
                    # exactly GrafxDurabilityBarrierFailed into the barrier metric, and a device
                    # that is full has to reach the caller as a device that is full.
                    failure.details.setdefault("file", target)
                    failure.details["attempts"] = attempts
                    raise
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)
            except OSError as failure:
                if attempts >= _PUBLISH_ATTEMPTS or not _worth_publishing_again(
                    failure
                ):
                    raise _storage_failure(
                        "The control record could not be published.",
                        failure,
                        attempts=attempts,
                        file=target,
                    ) from failure
                self._sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_PUBLISH_BACKOFF)

    def _publish_once(self, target: str, payload: bytes) -> None:
        """Write the temporary file and replace the target with it, exactly once."""
        storage = self._storage
        temporary = f"{target}.{self._owner}.tmp"
        if storage.exists(temporary):
            storage.remove(temporary)
        storage.create(temporary, exclusive=True)
        storage.append_log(temporary, payload)
        storage.durable_barrier(temporary)
        storage.atomic_replace(temporary, target)
        storage.durable_barrier(target)

    def _sweep_lease_temporaries(self) -> None:
        """Release every stray temporary of the lease file left behind by a crashed participant.

        Safe precisely here and nowhere else: every publication of the lease happens inside the
        lease section, and this runs while that section is held, so no live participant can have
        a temporary of its own outstanding. Without the sweep the strays of every crash stay for
        the life of the database.
        """
        prefix = f"{self._lease_file}."
        try:
            names = self._list_files(prefix)
        except (GrafxError, OSError):
            return
        for name in names:
            if name.startswith(prefix) and name.endswith(TEMPORARY_SUFFIX):
                self._discard(name)

    def _discard(self, name: str) -> None:
        """Release a control file that describes no live participant, tolerating deferral."""
        try:
            self._storage.recycle(name)
        except (GrafxError, OSError):
            # A control file that refuses to go away changes no decision: the record it holds was
            # already excluded from the horizon, and the next pass tries again.
            return

    # --- internals: locks ---------------------------------------------------------------------

    def _prepare_lock_directory(self, lock_directory: str | None) -> str | None:
        """Create the advisory-lock directory when one was configured, and return its path."""
        if lock_directory is None:
            return None
        if not isinstance(lock_directory, str) or not lock_directory:
            raise _reject("The lock_directory must be a non-empty string or None.")
        try:
            os.makedirs(lock_directory, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as failure:
            raise _reject(
                "The lock directory path is taken by something that is not a directory.",
                lock_directory=lock_directory,
                detail=str(failure),
            ) from failure
        except ValueError as failure:
            # An embedded NUL is not an OSError on any platform, and it never becomes valid.
            raise _reject(
                "The lock directory path is not a usable path.",
                lock_directory=lock_directory,
                detail=str(failure),
            ) from failure
        except OSError as failure:
            # Through the one builder, so the classification is mirrored into the details here
            # exactly as it is everywhere else: a caller obeying A47 reads the details, and this
            # is the database-open path (A13).
            raise _storage_failure(
                "The lock directory could not be created.",
                failure,
                attempts=1,
                lock_directory=lock_directory,
            ) from failure
        return lock_directory

    def _take_lock(self, name: str, timeout: float) -> object:
        """Take the advisory lock of a section, or raise GrafxLeaseTimeout on expiry."""
        if self._lock_directory is None:
            return self._take_local_lock(name, timeout)
        return self._take_file_lock(name, timeout)

    def _take_local_lock(self, name: str, timeout: float) -> object:
        """Take the process-wide lock that stands in for a section with no lock directory."""
        lock = _shared_section_lock(self._namespace, f"{self._control}/{name}")
        deadline = self._clock.monotonic() + timeout
        allowance = self._iteration_budget(timeout)
        iterations = 0
        while True:
            iterations += 1
            if lock.acquire(blocking=False):
                return lock
            now = self._clock.monotonic()
            if now >= deadline or iterations >= allowance:
                raise GrafxLeaseTimeout(
                    "The section was still held when the timeout elapsed.",
                    section=name,
                    timeout_seconds=timeout,
                    owner_id=self._owner,
                )
            self._sleep(min(self._poll, deadline - now))

    def _take_file_lock(self, name: str, timeout: float) -> object:
        """Take the operating-system advisory lock that backs a section between processes."""
        directory = self._lock_directory
        path = os.path.join(
            "" if directory is None else directory, f"{name}{LOCK_FILE_SUFFIX}"
        )
        deadline = self._clock.monotonic() + timeout
        key = (threading.get_ident(), name)
        scope: _ReusableDescriptorScope | None = None
        handle: int | None = None
        revalidate = False
        with self._state_lock:
            candidate = self._descriptor_scopes.get(key)
            if (
                candidate is not None
                and not candidate.closing
                and not candidate.borrowed
            ):
                scope = candidate
                scope.borrowed = True
                handle = scope.descriptor
                scope.descriptor = None
                revalidate = scope.revalidated
        try:
            if (
                handle is not None
                and revalidate
                and not self._descriptor_names(handle, path)
            ):
                # The parked descriptor no longer names what the path names, or the answer could
                # not be obtained. Either way it is not the file this section is about, and
                # locking it would report exclusion this coordinator does not have. Closing
                # before the retake makes the fallback a plain cold open, not a second chance.
                #
                # The proof itself is inside this frame because it is fallible like every other
                # step here: a host interrupt or an injected failure while proving must leave the
                # borrow released and the descriptor closed, exactly as a failed open would.
                self._close_file_descriptor(handle)
                handle = None
            if handle is None:
                handle = self._open_lock_file(path, name, deadline)
            self._wait_for_os_lock(handle, name, timeout, deadline)
            section = _FileSectionHandle(
                handle, key if scope is not None else None, scope
            )
        except BaseException:
            # No path out of here -- including construction of the Python handle after the
            # advisory lock was acquired -- may leak the descriptor. On Windows an open handle
            # keeps the file undeletable, and on both families it is what holds the lock.
            if scope is not None:
                self._finish_descriptor_borrow(key, scope, descriptor=None)
            if handle is not None:
                self._close_file_descriptor(handle)
            raise
        return section

    def _wait_for_os_lock(
        self, handle: int, name: str, timeout: float, deadline: float
    ) -> None:
        """Take the advisory lock on an open handle, waiting on the injected clock until expiry.

        Only contention is a busy section. A mount without advisory locking answers ENOLCK for
        ever, and a bad descriptor answers EBADF for ever; both are device failures, and the
        sibling that opens this same file already says so.
        """
        allowance = self._iteration_budget(timeout)
        iterations = 0
        while True:
            iterations += 1
            try:
                _acquire_os_lock(handle)
                return
            except OSError as failure:
                if failure.errno not in _CONTENTION_ERRNOS:
                    raise _storage_failure(
                        "The advisory lock of the section could not be taken.",
                        failure,
                        attempts=iterations,
                        section=name,
                        owner_id=self._owner,
                    ) from failure
                now = self._clock.monotonic()
                if now >= deadline or iterations >= allowance:
                    raise GrafxLeaseTimeout(
                        "The section was still held when the timeout elapsed.",
                        section=name,
                        timeout_seconds=timeout,
                        owner_id=self._owner,
                    ) from None
                self._sleep(min(self._poll, deadline - now))

    def _open_lock_file(self, path: str, name: str, deadline: float) -> int:
        """Open the lock file, retrying briefly while the platform keeps the handle busy.

        An anti-virus scanner or a desktop indexer can hold a freshly created file open for a few
        milliseconds and answer a second open with a sharing violation (TR-3). That is transient,
        so it is retried inside the budget of the caller rather than reported as a broken setup.
        """
        allowance = self._iteration_budget(max(deadline - self._clock.monotonic(), 0.0))
        iterations = 0
        while True:
            iterations += 1
            try:
                return os.open(path, os.O_RDWR | os.O_CREAT | _BINARY_FLAG, 0o600)
            except OSError as failure:
                now = self._clock.monotonic()
                if now >= deadline or iterations >= allowance:
                    # Nobody holds this section: the file itself cannot be opened. Reporting a
                    # lease timeout would blame a holder that does not exist and would tell a
                    # caller to retry a path that may never work (A11-revised).
                    raise _storage_failure(
                        "The lock file of the section could not be opened.",
                        failure,
                        attempts=iterations,
                        section=name,
                        owner_id=self._owner,
                    ) from failure
                self._sleep(min(self._poll, deadline - now))

    def _drop_lock(self, handle: object) -> None:
        """Release the advisory lock of a section. It never raises: the caller may be unwinding."""
        if isinstance(handle, int):
            # Preserve the pre-capability private shape for narrow test doubles and subclasses
            # that override _take_lock while still delegating cleanup here.
            try:
                _release_os_lock(handle)
            except OSError:
                pass
            finally:
                self._close_file_descriptor(handle)
            return
        if isinstance(handle, _FileSectionHandle):
            descriptor = handle.descriptor
            reusable = False
            try:
                _release_os_lock(descriptor)
            except OSError:
                # Closing the handle releases the lock on both families, so a failed explicit
                # unlock invalidates reuse and falls through to the fail-closed close.
                pass
            else:
                reusable = handle.scope is not None
            finally:
                if (
                    reusable
                    and handle.scope_key is not None
                    and handle.scope is not None
                ):
                    reused = self._finish_descriptor_borrow(
                        handle.scope_key,
                        handle.scope,
                        descriptor=descriptor,
                    )
                else:
                    reused = False
                    if handle.scope_key is not None and handle.scope is not None:
                        self._finish_descriptor_borrow(
                            handle.scope_key,
                            handle.scope,
                            descriptor=None,
                        )
                if not reused:
                    self._close_file_descriptor(descriptor)
            return
        release = getattr(handle, "release", None)
        if release is not None:
            release()

    @staticmethod
    def _descriptor_names(descriptor: int, path: str) -> bool:
        """Return whether an open descriptor still is the file the path names right now.

        Identity is ``(st_dev, st_ino)`` on both families -- on Windows Python reports the volume
        serial and the 64-bit file index -- so a lock file unlinked and recreated between two
        operations answers with a different identity even though the name did not change. The
        path is inspected WITHOUT being followed: a name that became a link is not the file this
        descriptor was opened through, whatever it now leads to. Anything that cannot be
        examined at all is treated the same way, because a descriptor that cannot be proved is
        exactly as unusable as one that is proved wrong.
        """
        try:
            held = os.fstat(descriptor)
            named = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        if not (stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode)):
            return False
        return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)

    def _finish_descriptor_borrow(
        self,
        key: tuple[int, str],
        scope: _ReusableDescriptorScope,
        *,
        descriptor: int | None,
    ) -> bool:
        """Return a certainly unlocked descriptor to its exact live scope, or evict it."""
        with self._state_lock:
            current = self._descriptor_scopes.get(key)
            if current is not scope:
                return False
            scope.borrowed = False
            if (
                descriptor is not None
                and scope.depth > 0
                and not scope.closing
                and scope.descriptor is None
            ):
                scope.descriptor = descriptor
                return True
            if scope.depth == 0 or scope.closing:
                self._descriptor_scopes.pop(key, None)
            return False

    @staticmethod
    def _close_file_descriptor(descriptor: int) -> None:
        """Close a section descriptor quietly; close is the fail-closed unlock fallback."""
        try:
            os.close(descriptor)
        except OSError:
            pass

    # --- internals: timing and metrics ---------------------------------------------------------

    def _iteration_budget(self, timeout: float) -> int:
        """Return how many polls a wait of this length may spend before it gives up."""
        expected = timeout / self._poll
        largest_expected = (
            _MAX_WAIT_ITERATIONS - _WAIT_ITERATION_FLOOR
        ) / _WAIT_ITERATION_MARGIN
        if not isfinite(expected) or expected >= largest_expected:
            return _MAX_WAIT_ITERATIONS
        return _WAIT_ITERATION_FLOOR + int(_WAIT_ITERATION_MARGIN * expected)

    def _sleep(self, seconds: float) -> None:
        """Wait for the given interval through the injected sleeper, never below zero."""
        self._sleeper(seconds if seconds > 0.0 else 0.0)

    def _observe_wait(self, outcome: str, seconds: float) -> None:
        """Report how long an acquisition attempt waited and how it ended."""
        metrics = self._metrics
        if metrics is None or not metrics.enabled:
            return
        metrics.observe(LEASE_WAIT_METRIC, max(seconds, 0.0), {"outcome": outcome})


def _same_record(current: LeaseRecord | None, basis: LeaseRecord | None) -> bool:
    """Return True when the lease file still says exactly what the observation said."""
    if current is None or basis is None:
        return current is None and basis is None
    return (
        current.owner_id == basis.owner_id
        and current.epoch == basis.epoch
        and current.heartbeat_seq == basis.heartbeat_seq
        and current.held == basis.held
    )


def _acquire_os_lock(handle: int) -> None:
    """Take the exclusive advisory lock without blocking; OSError means somebody else holds it."""
    if _IS_WINDOWS:  # pragma: no cover - the counterpart runs on the other family
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, _LOCK_BYTES)
        return
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pragma: no cover - mirrored


def _release_os_lock(handle: int) -> None:
    """Release the exclusive advisory lock taken by :func:`_acquire_os_lock`."""
    if _IS_WINDOWS:  # pragma: no cover - the counterpart runs on the other family
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, _LOCK_BYTES)
        return
    fcntl.flock(handle, fcntl.LOCK_UN)  # pragma: no cover - mirrored
