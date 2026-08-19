"""The local process coordinator (CONTRACT.md sections 4.3 and 6.1, SPEC-M1 FR-7, BR-7, BR-10).

This adapter answers three questions for every participant of a database directory: who holds
the writer epoch, which readers are alive and what snapshot each of them pins, and who is inside
a short critical section right now. It answers them with two mechanisms and nothing else:

* **durable control files, written through the StorageDevice port.** The lease lives in
  ``control/writer.lease`` and every reader in ``control/readers/<reader_id>.reader``. Both are
  fixed-layout, checksummed records published with ``atomic_replace``, so a reader of the file
  sees either the whole previous record or the whole next one, never a half-updated one.
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

import os
import struct
import threading
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from typing import TypeVar

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxSchemaVersionMismatch,
    GrafxStaleEpoch,
)
from okto_grafx.domain.ids import Epoch, Lsn
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.coordination import DeadOwnerReport, Lease, ReaderHandle
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice

__all__ = [
    "CONTROL_DIRECTORY",
    "LEASE_FILE_NAME",
    "LEASE_FORMAT_VERSION",
    "LEASE_MAGIC",
    "LEASE_SECTION",
    "LEASE_WAIT_METRIC",
    "LOCK_FILE_SUFFIX",
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

_MAX_WAIT_ITERATIONS: int = 1_000_000
"""Defensive bound on any wait loop, so an injected clock that never advances cannot hang a host."""

_MAX_IDENTIFIER_LENGTH: int = 96
"""Longest accepted participant identifier. It has to fit in a file name on every platform."""

_IDENTIFIER_EXTRA_CHARACTERS: frozenset[str] = frozenset({".", "-", "_"})
"""Punctuation accepted inside an identifier, all of it safe in a file name."""

_Record = TypeVar("_Record", "LeaseRecord", "ReaderRecord")
"""Either control record, so one retrying reader serves both without losing its type."""


def _reject(reason: str, **details: object) -> GrafxConfigurationError:
    """Build the typed configuration error used for every constructor and argument refusal."""
    return GrafxConfigurationError(reason, **details)


def _validate_identifier(label: str, value: str) -> str:
    """Return the identifier when it is safe as a file name, else raise a configuration error.

    Identifiers become file names, and file identity must never depend on case (CONTRACT.md
    section 11 item 9), so an upper-case character is refused rather than silently folded.
    """
    if not isinstance(value, str) or not value:
        raise _reject(f"The {label} must be a non-empty string.", field=label, value=repr(value))
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        raise _reject(
            f"The {label} must be at most {_MAX_IDENTIFIER_LENGTH} characters long.",
            field=label,
            length=len(value),
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
        raise _reject(f"The {label} must be greater than zero.", field=label, value=number)
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
        raise _reject(f"The {label} must be an integer.", field=label, value=repr(value))
    if value < 0:
        raise _reject(f"The {label} must not be negative.", field=label, value=value)
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
    owner = _validate_identifier("owner_id", record.owner_id).encode("ascii")
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


def _decoded_identifier(raw: bytes, start: int, length: int, file: str) -> str:
    """Return the ASCII identifier stored at the given offset, or report corruption."""
    chunk = raw[start : start + length]
    if len(chunk) != length:
        raise GrafxCorruptionDetected(
            "The control record ends before its identifier is complete.", file=file
        )
    try:
        return chunk.decode("ascii")
    except UnicodeDecodeError as failure:
        raise GrafxCorruptionDetected(
            "The control record carries an identifier outside ASCII.", file=file
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
            "The lease record is shorter than its own header.", file=file, length=len(raw)
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
        raise GrafxCorruptionDetected("The lease record carries a foreign magic.", file=file)
    _verify_envelope(
        raw,
        version=version,
        current=LEASE_FORMAT_VERSION,
        total=total,
        expected=_LEASE_HEADER_SIZE + owner_len + _CHECKSUM_SIZE,
        file=file,
    )
    owner = _decoded_identifier(raw, _LEASE_HEADER_SIZE, owner_len, file)
    return LeaseRecord(
        owner_id=owner,
        epoch=epoch,
        heartbeat_seq=sequence,
        ttl_seconds=ttl,
        wall_stamp=wall,
        held=bool(flags & _FLAG_ACTIVE),
        superseded_epoch=superseded,
    )


def decode_reader_record(raw: bytes, *, file: str = READERS_DIRECTORY_NAME) -> ReaderRecord:
    """Decode a reader registration, refusing anything that is not one whole valid record."""
    if len(raw) < _READER_HEADER_SIZE + _CHECKSUM_SIZE:
        raise GrafxCorruptionDetected(
            "The reader record is shorter than its own header.", file=file, length=len(raw)
        )
    try:
        fields = struct.unpack_from(_READER_HEADER, raw, 0)
    except struct.error as failure:
        raise GrafxCorruptionDetected(
            "The reader record header is unreadable.", file=file
        ) from failure
    magic, version, flags, reader_len, snapshot, sequence, wall, total, _reserved = fields
    if magic != READER_MAGIC:
        raise GrafxCorruptionDetected("The reader record carries a foreign magic.", file=file)
    _verify_envelope(
        raw,
        version=version,
        current=READER_FORMAT_VERSION,
        total=total,
        expected=_READER_HEADER_SIZE + reader_len + _CHECKSUM_SIZE,
        file=file,
    )
    reader = _decoded_identifier(raw, _READER_HEADER_SIZE, reader_len, file)
    return ReaderRecord(
        reader_id=reader,
        snapshot_lsn=snapshot,
        heartbeat_seq=sequence,
        wall_stamp=wall,
        active=bool(flags & _FLAG_ACTIVE),
    )


class _Section:
    """One entered critical section: how deep the re-entry counter is and what holds the lock."""

    __slots__ = ("depth", "handle")

    def __init__(self, handle: object) -> None:
        self.depth: int = 1
        self.handle: object = handle


class LocalProcessCoordinator:
    """Cross-process agreement on the writer epoch, on live readers and on short critical sections.

    Construction takes the two ports it needs plus the one thing a port cannot express: the real
    directory that holds the advisory lock files. Pass ``lock_directory`` for any database backed
    by a real file system; leave it None for the in-memory device, whose namespace is not shared
    with another process anyway, and the sections degrade to process-local locks.

    Re-entrancy of ``exclusive()``: **allowed and counted**. The same coordinator, the same
    thread and the same section name may nest; the operating-system lock is taken once and
    released when the outermost block exits. A different thread, a different coordinator instance
    or a different process always contends for real.
    """

    def __init__(
        self,
        storage: StorageDevice,
        clock: Clock,
        *,
        owner_id: str | None = None,
        lock_directory: str | None = None,
        control_directory: str = CONTROL_DIRECTORY,
        ttl_seconds: float = 5.0,
        owner_stall_threshold: float = 5.0,
        reader_stall_threshold: float = 15.0,
        section_timeout: float = 10.0,
        poll_interval: float = 0.005,
        epoch_cache_seconds: float = 0.0,
        metrics: MetricsSink | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """Bind the ports and the control-file layout this participant will use."""
        self._storage = storage
        self._clock = clock
        self._metrics = metrics
        self._sleeper: Callable[[float], None] = time.sleep if sleeper is None else sleeper
        self._owner: str = (
            _validate_identifier("owner_id", owner_id)
            if owner_id is not None
            else f"p{os.getpid():d}-{uuid.uuid4().hex[:12]}"
        )
        control = _validate_identifier("control_directory", control_directory)
        self._lease_file: str = f"{control}/{LEASE_FILE_NAME}"
        self._readers_prefix: str = f"{control}/{READERS_DIRECTORY_NAME}/"
        self._ttl: float = _require_positive("ttl_seconds", ttl_seconds)
        self._owner_stall: float = _require_positive("owner_stall_threshold", owner_stall_threshold)
        self._reader_stall: float = _require_positive(
            "reader_stall_threshold", reader_stall_threshold
        )
        self._section_timeout: float = _require_non_negative("section_timeout", section_timeout)
        self._poll: float = _require_positive("poll_interval", poll_interval)
        self._epoch_cache_seconds: float = _require_non_negative(
            "epoch_cache_seconds", epoch_cache_seconds
        )
        self._lock_directory: str | None = self._prepare_lock_directory(lock_directory)

        self._state_lock = threading.RLock()
        self._sections: dict[tuple[int, str], _Section] = {}
        self._local_locks: dict[str, threading.Lock] = {}
        self._held: Lease | None = None
        self._basis: LeaseRecord | None = None
        self._basis_seen: bool = False
        self._owner_key: tuple[str, int, int] | None = None
        self._owner_key_at: float = clock.monotonic()
        self._epoch_cache: tuple[float, Epoch] | None = None
        self._epoch_ceiling: Epoch = 0
        self._readers: dict[str, ReaderHandle] = {}
        self._reader_sequences: dict[str, int] = {}
        self._reader_samples: dict[str, tuple[int, float]] = {}
        self._reader_counter: int = 0

    def __repr__(self) -> str:
        """Return a representation naming the owner and the control files this coordinator uses."""
        return f"LocalProcessCoordinator(owner_id={self._owner!r}, lease_file={self._lease_file!r})"

    # --- identity and epoch --------------------------------------------------------------------

    def owner_id(self) -> str:
        """Return the stable identity of this participant."""
        return self._owner

    def current_epoch(self) -> Epoch:
        """Return the epoch published by the lease file; 0 when none was ever published."""
        return self._published_epoch(force=True)

    def validate_epoch(self, epoch: Epoch) -> None:
        """Raise GrafxStaleEpoch unless this exact epoch is the published one and is still held.

        Three conditions, all necessary and all checked before the caller can reach the device
        (BR-7, AC-6): a lease record exists, it is held, and its epoch is the one offered. The
        first test is free: epochs never decrease, so an epoch below the highest one this
        coordinator has ever observed is refused without touching the device at all. Inside an
        ``exclusive()`` section the published epoch is always re-read, which is what makes the
        optional staleness window of the cache safe for the commit protocol, whose only device
        writes happen inside that section.
        """
        offered = _require_index("epoch", epoch)
        if offered < self._epoch_ceiling:
            raise GrafxStaleEpoch(
                "The offered epoch is below an epoch already observed on this database.",
                epoch=offered,
                observed_epoch=self._epoch_ceiling,
                owner_id=self._owner,
            )
        record = self._published_record(force=self._inside_section())
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
            stalled = not vacant and self._owner_stalled(now, self._owner_stall) is not None
            if vacant or stalled:
                claimed = self._try_claim(record, budget=max(deadline - now, 0.0))
                if claimed is not None:
                    self._observe_wait("takeover" if stalled else "granted", claimed[1] - started)
                    return claimed[0]
            now = self._clock.monotonic()
            if now >= deadline or iterations >= _MAX_WAIT_ITERATIONS:
                self._observe_wait("timeout", now - started)
                raise GrafxLeaseTimeout(
                    "The writer lease was not granted before the timeout elapsed.",
                    timeout_seconds=budget,
                    waited_seconds=now - started,
                    owner_id=self._owner,
                )
            self._sleep(min(self._poll, deadline - now))

    def renew_lease(self, lease: Lease) -> Lease:
        """Advance the heartbeat of a held lease, or raise GrafxLeaseStolen when it moved on."""
        with self.exclusive(LEASE_SECTION, timeout=self._section_timeout):
            now = self._clock.monotonic()
            record = self._observe_lease(self._read_lease_record(), now)
            if (
                record is None
                or not record.held
                or record.owner_id != lease.owner_id
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

        Releasing a lease that was already taken over is a no-op: closing a database must never
        raise because somebody else already won the epoch.
        """
        with self.exclusive(LEASE_SECTION, timeout=self._section_timeout):
            now = self._clock.monotonic()
            record = self._observe_lease(self._read_lease_record(), now)
            if (
                record is not None
                and record.held
                and record.owner_id == lease.owner_id
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
            held = self._held
            if held is not None and held.epoch == lease.epoch and held.owner_id == lease.owner_id:
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
        """
        now = self._clock.monotonic()
        if not self._basis_seen:
            self._observe_lease(self._read_lease_record(), now)
        basis = self._basis
        if basis is not None and basis.held and self._owner_stalled(now, self._owner_stall) is None:
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
        with self._state_lock:
            self._reader_counter += 1
            reader_id = f"{self._owner}-r{self._reader_counter:04d}"
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
            self._reader_samples[reader_id] = (1, self._clock.monotonic())
        return handle

    def refresh_reader(self, handle: ReaderHandle) -> None:
        """Advance the heartbeat of a registration, re-creating its file when it was pruned.

        Re-creating matters: pruning a reader that had merely gone quiet must never end its
        snapshot, because BR-10 forbids evicting a live reader under any circumstance.
        """
        reader_id = _validate_identifier("reader_id", handle.reader_id)
        pinned = _require_index("snapshot_lsn", handle.snapshot_lsn)
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
        for name in self._storage.list_files(self._readers_prefix):
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
        with self._state_lock:
            for reader_id in [key for key in self._reader_samples if key not in observed]:
                if reader_id not in self._readers:
                    self._reader_samples.pop(reader_id, None)
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
        return self._section(_validate_identifier("section name", name.lower()), timeout)

    @contextmanager
    def _section(self, name: str, timeout: float) -> Iterator[None]:
        """Take the named section, counting re-entry from the same thread of this coordinator."""
        budget = _require_non_negative("timeout", timeout)
        key = (threading.get_ident(), name)
        with self._state_lock:
            entered = self._sections.get(key)
            if entered is not None:
                entered.depth += 1
        if entered is not None:
            try:
                yield
            finally:
                with self._state_lock:
                    entered.depth -= 1
            return
        handle = self._take_lock(name, budget)
        with self._state_lock:
            self._sections[key] = _Section(handle)
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
        lease = Lease(
            owner_id=self._owner,
            epoch=record.epoch,
            acquired_monotonic=moment,
            heartbeat_seq=record.heartbeat_seq,
            ttl_seconds=record.ttl_seconds,
        )
        self._held = lease
        return lease

    def _try_claim(self, basis: LeaseRecord | None, *, budget: float) -> tuple[Lease, float] | None:
        """Attempt the compare-and-set that installs this participant, returning None when it lost.

        The section attempt is bounded by what is left of the budget the caller gave to the
        acquisition, so waiting for the section can never outlast the timeout that was asked for.
        """
        try:
            with self.exclusive(LEASE_SECTION, timeout=min(self._section_timeout, budget)):
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
                return None
            stall = now - self._owner_key_at
        return stall if stall > threshold else None

    def _observe_lease(self, record: LeaseRecord | None, now: float) -> LeaseRecord | None:
        """Record one observation of the lease file and return the record that was observed."""
        epoch = 0 if record is None else record.epoch
        key = record.liveness_key() if record is not None and record.held else None
        with self._state_lock:
            self._basis = record
            self._basis_seen = True
            self._epoch_cache = (now, epoch)
            if epoch > self._epoch_ceiling:
                self._epoch_ceiling = epoch
            if key != self._owner_key:
                self._owner_key = key
                self._owner_key_at = now
        return record

    def _published_record(self, *, force: bool) -> LeaseRecord | None:
        """Return the published lease record, honouring the staleness window of the cache."""
        now = self._clock.monotonic()
        with self._state_lock:
            cache = self._epoch_cache
            basis = self._basis
            window = self._epoch_cache_seconds
            # A window of zero means "never reuse": a frozen or coarse clock must not turn a
            # zero-width window into an unbounded one, which is what a non-strict test would do.
            fresh = cache is not None and window > 0.0 and (now - cache[0]) < window
        if not force and fresh and self._basis_seen:
            return basis
        return self._observe_lease(self._read_lease_record(), now)

    def _published_epoch(self, *, force: bool) -> Epoch:
        """Return the epoch of the published lease record, or 0 when no record exists."""
        record = self._published_record(force=force)
        return 0 if record is None else record.epoch

    def _inside_section(self) -> bool:
        """Return True when this thread of this coordinator currently holds any named section."""
        thread = threading.get_ident()
        with self._state_lock:
            return any(owner == thread for owner, _name in self._sections)

    # --- internals: reader state ------------------------------------------------------------------

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
        return self._read_record(self._lease_file, decode_lease_record)

    def _read_reader_record(self, name: str) -> ReaderRecord | None:
        """Read and decode one reader registration, returning None when the file went away."""
        return self._read_record(name, decode_reader_record)

    def _read_record(self, name: str, decode: Callable[..., _Record]) -> _Record | None:
        """Read and decode a whole control record, retrying across a concurrent replacement.

        Reading takes two calls, the size and the bytes, and another participant may publish a
        record of a different length between them. That is a benign race, not damage, so the
        read is repeated; only a mismatch that survives every attempt is reported as corruption.
        A file that disappears mid-read simply reads as absent, which is what a pruned reader
        registration is.
        """
        storage = self._storage
        failure: GrafxError | None = None
        reason: str | None = None
        for _attempt in range(_READ_ATTEMPTS):
            if not storage.exists(name):
                return None
            try:
                size = storage.log_size(name)
                if size == 0:
                    return None
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
                failure = error
            except OSError as error:
                # A device that lets a platform failure through must still not reach the caller
                # as anything other than a typed error (CONTRACT.md section 11 item 5).
                reason = str(error)
        if failure is not None:
            raise failure
        if reason is not None:
            raise GrafxCorruptionDetected(
                "The control file could not be read.", file=name, reason=reason
            )
        return None

    def _publish_lease(self, record: LeaseRecord) -> None:
        """Write the lease record atomically and adopt it as this participant own observation."""
        self._publish(self._lease_file, encode_lease_record(record))
        self._observe_lease(record, self._clock.monotonic())

    def _publish_reader(self, record: ReaderRecord) -> None:
        """Write one reader registration atomically."""
        self._publish(self._reader_file(record.reader_id), encode_reader_record(record))

    def _publish(self, target: str, payload: bytes) -> None:
        """Publish a whole control record through the port: write a temporary file, then replace."""
        storage = self._storage
        temporary = f"{target}.{self._owner}.tmp"
        if storage.exists(temporary):
            storage.remove(temporary)
        storage.create(temporary, exclusive=True)
        storage.append_log(temporary, payload)
        storage.durable_barrier(temporary)
        storage.atomic_replace(temporary, target)
        storage.durable_barrier(target)

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
        except OSError as failure:
            raise _reject(
                "The lock directory could not be created.",
                lock_directory=lock_directory,
                reason=str(failure),
            ) from failure
        return lock_directory

    def _take_lock(self, name: str, timeout: float) -> object:
        """Take the advisory lock of a section, or raise GrafxLeaseTimeout on expiry."""
        if self._lock_directory is None:
            return self._take_local_lock(name, timeout)
        return self._take_file_lock(name, timeout)

    def _take_local_lock(self, name: str, timeout: float) -> object:
        """Take the process-local lock that stands in for a section with no lock directory."""
        with self._state_lock:
            lock = self._local_locks.setdefault(name, threading.Lock())
        deadline = self._clock.monotonic() + timeout
        iterations = 0
        while True:
            iterations += 1
            if lock.acquire(blocking=False):
                return lock
            now = self._clock.monotonic()
            if now >= deadline or iterations >= _MAX_WAIT_ITERATIONS:
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
        path = os.path.join("" if directory is None else directory, f"{name}{LOCK_FILE_SUFFIX}")
        deadline = self._clock.monotonic() + timeout
        handle = self._open_lock_file(path, name, deadline)
        try:
            self._wait_for_os_lock(handle, name, timeout, deadline)
        except BaseException:
            # No path out of here may leak the descriptor: on Windows an open handle is what
            # keeps a file undeletable, and on both families it is what holds the lock.
            try:
                os.close(handle)
            except OSError:
                pass
            raise
        return handle

    def _wait_for_os_lock(self, handle: int, name: str, timeout: float, deadline: float) -> None:
        """Take the advisory lock on an open handle, waiting on the injected clock until expiry."""
        iterations = 0
        while True:
            iterations += 1
            try:
                _acquire_os_lock(handle)
                return
            except OSError:
                now = self._clock.monotonic()
                if now >= deadline or iterations >= _MAX_WAIT_ITERATIONS:
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
        iterations = 0
        while True:
            iterations += 1
            try:
                return os.open(path, os.O_RDWR | os.O_CREAT | _BINARY_FLAG, 0o600)
            except OSError as failure:
                now = self._clock.monotonic()
                if now >= deadline or iterations >= _MAX_WAIT_ITERATIONS:
                    raise GrafxLeaseTimeout(
                        "The lock file of the section could not be opened before the timeout.",
                        section=name,
                        reason=str(failure),
                        owner_id=self._owner,
                    ) from failure
                self._sleep(min(self._poll, deadline - now))

    def _drop_lock(self, handle: object) -> None:
        """Release the advisory lock of a section. It never raises: the caller may be unwinding."""
        if isinstance(handle, int):
            try:
                _release_os_lock(handle)
            except OSError:
                # Closing the handle releases the lock on both families, so a failed explicit
                # unlock changes nothing that the close below does not already guarantee.
                pass
            finally:
                try:
                    os.close(handle)
                except OSError:
                    pass
            return
        release = getattr(handle, "release", None)
        if release is not None:
            release()

    # --- internals: timing and metrics ---------------------------------------------------------

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
