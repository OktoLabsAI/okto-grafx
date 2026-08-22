"""One entry of the unapplied-work ledger (CONTRACT.md section 6.6, SPEC-M1 FR-9, TR-5).

The ledger is the proof that a discard is recoverable rather than merely reported (G8, BR-3).
Every record recovery throws away leaves exactly one entry here, and the entry carries enough to
answer the two questions an operator asks afterwards: *what was lost*, and *can it be put back*.

The layout is frozen by CONTRACT.md section 6.6 and implemented byte for byte::

    magic u32 0x4C475258 | format_version u16 | entry_type u16 | total_length u32 |
    entry_id u64 | origin_class u8 | reason_code u8 | reserved u16 |
    lsn_start u64 | lsn_end u64 | epoch u64 | captured_at_wall f64 |
    digest 32B | payload_len u32 | payload | crc32c u32

Two fields do work that is easy to miss.

``origin_class`` is the whole of SD-4. **Reapplicable** means the bytes decoded: the operation is
known and can be handed back to an applier. **Forensic** means they did not: what survives is the
range itself, its digest and where it came from, and no amount of retrying will turn it into an
operation. Making a forensic entry reapplicable would invite a caller to replay garbage, which is
why :meth:`LedgerStore.reprocess` refuses one by class rather than by inspection.

``digest`` is a SHA-256 over the payload and the CRC is over everything before it. They answer
different questions: the CRC says the record on the device is the record that was written, and
the digest says the bytes a caller EXPORTS are the bytes that were captured -- a claim that has to
survive being copied out of this file into a bug report.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.page.checksum import crc32c

__all__ = [
    "LEDGER_ENTRY_HEADER_LENGTH",
    "LEDGER_FORMAT_VERSION",
    "LEDGER_MAGIC",
    "MAX_LEDGER_PAYLOAD_BYTES",
    "DIGEST_LENGTH",
    "LedgerEntry",
    "LedgerEntryType",
    "LedgerOriginClass",
    "LedgerReason",
    "digest_of",
]

LEDGER_MAGIC: int = 0x4C475258
"""The four bytes every ledger entry starts with (CONTRACT.md section 6.6)."""

LEDGER_FORMAT_VERSION: int = 1
"""The version this build writes. A reader accepts every version at or below its own."""

_HEADER: struct.Struct = struct.Struct("<IHHIQBBHQQQd32sI")
LEDGER_ENTRY_HEADER_LENGTH: int = _HEADER.size
"""Bytes of the fixed part of one entry, before its payload and its trailing checksum."""

_CHECKSUM: struct.Struct = struct.Struct("<I")

DIGEST_LENGTH: int = 32
"""Bytes of the SHA-256 digest the entry carries over its payload."""

MAX_LEDGER_PAYLOAD_BYTES: int = 64 * 1024 * 1024
"""Largest payload one entry carries. A damaged length past this is refused before allocating."""

_MAX_U8: int = 0xFF
_MAX_U16: int = 0xFFFF
_MAX_U32: int = 0xFFFFFFFF
_MAX_U64: int = 0xFFFFFFFFFFFFFFFF


class LedgerOriginClass(IntEnum):
    """How the lost work was classified when it was written down (SD-4, FR-9)."""

    REAPPLICABLE = 1
    FORENSIC = 2


class LedgerReason(IntEnum):
    """Why the work was discarded. The codes are frozen by CONTRACT.md section 6.6.

    They name the REASON FOR THE DISCARD, not the shape of the damage: a record thrown away
    because it sat above the cut is ``TRUNCATED_TAIL`` whether the bytes at the cut were zeros, a
    bad magic or a short record, and the precise decoder verdict travels in the payload envelope
    where it costs nothing and constrains nothing.
    """

    TRUNCATED_TAIL = 1
    CHECKSUM_FAILURE = 2
    STALE_EPOCH = 3
    DEVICE_FULL = 4
    QUARANTINED_SEGMENT = 5
    INDEX_RECONCILE_ORPHAN = 6


class LedgerEntryType(IntEnum):
    """What an entry says. The field is frozen by section 6.6; these values are C6's.

    ``DISCARD`` is the entry FR-9 is about: work that was lost involuntarily. The other two are
    receipts, and they exist because the ledger is the only append-only, checksummed, durable
    place this component owns. ``REPROCESS_RECEIPT`` is what makes ``reprocess`` exactly-once
    across a restart (AC-12), and ``RETIREMENT`` is the record of a control-plane name retired
    under carried finding CF-1.
    """

    DISCARD = 1
    REPROCESS_RECEIPT = 2
    RETIREMENT = 3


def digest_of(payload: bytes) -> bytes:
    """Return the SHA-256 of a payload, which is what an entry stores and an export proves."""
    return hashlib.sha256(bytes(payload)).digest()


def _require_unsigned(field: str, value: object, ceiling: int) -> int:
    """Return the value as an unsigned integer inside its field width, or refuse it.

    A refusal here is a caller's argument and never damaged bytes, so it answers
    ``configuration_error`` (A11-revised): turning a wrong argument into ``corruption_detected``
    would route it to truncation and quarantine, which is FR-8 acting on a typing mistake.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a ledger entry must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= ceiling:
        raise GrafxConfigurationError(
            f"The {field} of a ledger entry must be between 0 and {ceiling}; got {value}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One durable record of work that was lost involuntarily, or of a deliberate recovery act."""

    entry_id: int
    origin_class: LedgerOriginClass
    reason: LedgerReason
    payload: bytes = b""
    entry_type: LedgerEntryType = LedgerEntryType.DISCARD
    lsn_start: Lsn = NO_LSN
    lsn_end: Lsn = NO_LSN
    epoch: int = 0
    captured_at_wall: float = 0.0
    format_version: int = LEDGER_FORMAT_VERSION
    reserved: int = 0

    def __post_init__(self) -> None:
        """Check every field against the width section 6.6 gives it, before any packing."""
        _require_unsigned("entry_id", self.entry_id, _MAX_U64)
        _require_unsigned("lsn_start", self.lsn_start, _MAX_U64)
        _require_unsigned("lsn_end", self.lsn_end, _MAX_U64)
        _require_unsigned("epoch", self.epoch, _MAX_U64)
        _require_unsigned("format_version", self.format_version, _MAX_U16)
        _require_unsigned("reserved", self.reserved, _MAX_U16)
        if not isinstance(self.origin_class, LedgerOriginClass):
            raise GrafxConfigurationError(
                f"A ledger entry needs a LedgerOriginClass; got {self.origin_class!r}.",
                field="origin_class",
                value=repr(self.origin_class),
            )
        if not isinstance(self.reason, LedgerReason):
            raise GrafxConfigurationError(
                f"A ledger entry needs a LedgerReason; got {self.reason!r}.",
                field="reason",
                value=repr(self.reason),
            )
        if not isinstance(self.entry_type, LedgerEntryType):
            raise GrafxConfigurationError(
                f"A ledger entry needs a LedgerEntryType; got {self.entry_type!r}.",
                field="entry_type",
                value=repr(self.entry_type),
            )
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise GrafxConfigurationError(
                f"A ledger payload must be bytes; got {type(self.payload).__name__}.",
                field="payload",
                value=type(self.payload).__name__,
            )
        if not isinstance(self.payload, bytes):
            object.__setattr__(self, "payload", bytes(self.payload))
        if len(self.payload) > MAX_LEDGER_PAYLOAD_BYTES:
            raise GrafxConfigurationError(
                f"A ledger payload of {len(self.payload)} bytes is past the "
                f"{MAX_LEDGER_PAYLOAD_BYTES} one entry carries.",
                field="payload_len",
                value=len(self.payload),
            )
        if isinstance(self.captured_at_wall, bool) or not isinstance(
            self.captured_at_wall, (int, float)
        ):
            raise GrafxConfigurationError(
                "The capture time of a ledger entry must be a number; got "
                f"{type(self.captured_at_wall).__name__}.",
                field="captured_at_wall",
                value=repr(self.captured_at_wall),
            )
        object.__setattr__(self, "captured_at_wall", float(self.captured_at_wall))
        if self.lsn_end and self.lsn_start and self.lsn_end < self.lsn_start:
            raise GrafxConfigurationError(
                f"A ledger entry spans sequence numbers {self.lsn_start} to {self.lsn_end}, "
                "which runs backwards.",
                field="lsn_end",
                value=self.lsn_end,
            )

    @property
    def digest(self) -> bytes:
        """Return the SHA-256 of the payload this entry carries."""
        return digest_of(self.payload)

    @property
    def reapplicable(self) -> bool:
        """Return True when the operation decoded and an applier may be offered it."""
        return self.origin_class is LedgerOriginClass.REAPPLICABLE

    def encoded_length(self) -> int:
        """Return the number of bytes this entry occupies on the device."""
        return LEDGER_ENTRY_HEADER_LENGTH + len(self.payload) + _CHECKSUM.size

    def encode(self) -> bytes:
        """Return the entry as the bytes CONTRACT.md section 6.6 freezes."""
        total_length = self.encoded_length()
        _require_unsigned("total_length", total_length, _MAX_U32)
        header = _HEADER.pack(
            LEDGER_MAGIC,
            self.format_version,
            int(self.entry_type),
            total_length,
            self.entry_id,
            int(self.origin_class),
            int(self.reason),
            self.reserved,
            self.lsn_start,
            self.lsn_end,
            self.epoch,
            self.captured_at_wall,
            self.digest,
            len(self.payload),
        )
        body = header + self.payload
        return body + _CHECKSUM.pack(crc32c(body))

    @classmethod
    def decode(cls, raw: bytes, offset: int = 0) -> LedgerEntry:
        """Parse one entry that starts at this offset, refusing anything that is not one.

        Every refusal names a field and an offset, because the caller of this door is either the
        ledger reading its own file -- where the offset says which entry stopped the read -- or a
        test proving a specific mutilation is caught.
        """
        data = _require_bytes(raw)
        if offset < 0 or offset > len(data):
            raise GrafxConfigurationError(
                f"A ledger entry cannot be decoded at offset {offset} of {len(data)} bytes.",
                field="offset",
                value=offset,
            )
        available = len(data) - offset
        if available < LEDGER_ENTRY_HEADER_LENGTH:
            raise GrafxCorruptionDetected(
                f"A ledger entry needs {LEDGER_ENTRY_HEADER_LENGTH} header bytes and only "
                f"{available} remain.",
                field="header_len",
                offset=offset,
            )
        (
            magic,
            format_version,
            entry_type,
            total_length,
            entry_id,
            origin_class,
            reason,
            reserved,
            lsn_start,
            lsn_end,
            epoch,
            captured_at_wall,
            digest,
            payload_len,
        ) = _HEADER.unpack_from(data, offset)
        if magic != LEDGER_MAGIC:
            raise GrafxCorruptionDetected(
                f"A ledger entry starts with {magic:#010x} and not with the ledger magic.",
                field="magic",
                offset=offset,
                value=magic,
            )
        if format_version > LEDGER_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"This build reads ledger format {LEDGER_FORMAT_VERSION} and below; the entry at "
                f"byte {offset} declares {format_version}.",
                field="format_version",
                offset=offset,
                value=format_version,
            )
        if payload_len > MAX_LEDGER_PAYLOAD_BYTES:
            raise GrafxCorruptionDetected(
                f"A ledger entry declares a payload of {payload_len} bytes, past the "
                f"{MAX_LEDGER_PAYLOAD_BYTES} one entry carries.",
                field="payload_len",
                offset=offset,
                value=payload_len,
            )
        expected_total = LEDGER_ENTRY_HEADER_LENGTH + payload_len + _CHECKSUM.size
        if total_length != expected_total:
            raise GrafxCorruptionDetected(
                f"A ledger entry declares {total_length} total bytes but its payload length "
                f"describes {expected_total}.",
                field="total_length",
                offset=offset,
                value=total_length,
            )
        if available < total_length:
            # The field is deliberately NOT "total_length": the guard above answers the same
            # class for a declared length that disagrees with its own payload length, and two
            # guards that cannot be told apart make each other untestable (A62). A mutation
            # battery proved it -- deleting the guard above left the suite green because this
            # one answered in its place, wearing the same name.
            raise GrafxCorruptionDetected(
                f"A ledger entry declares {total_length} bytes and only {available} remain.",
                field="truncated_entry",
                offset=offset,
                value=total_length,
            )
        payload_at = offset + LEDGER_ENTRY_HEADER_LENGTH
        payload = data[payload_at : payload_at + payload_len]
        stored = _CHECKSUM.unpack_from(data, payload_at + payload_len)[0]
        computed = crc32c(data[offset : payload_at + payload_len])
        if stored != computed:
            raise GrafxCorruptionDetected(
                f"The checksum of the ledger entry at byte {offset} is {stored:#010x} and its "
                f"bytes compute {computed:#010x}.",
                field="crc32c",
                offset=offset,
                expected=stored,
                observed=computed,
            )
        if digest != digest_of(payload):
            raise GrafxCorruptionDetected(
                f"The digest of the ledger entry at byte {offset} does not match its payload.",
                field="digest",
                offset=offset,
            )
        if origin_class not in tuple(int(member) for member in LedgerOriginClass):
            raise GrafxCorruptionDetected(
                f"A ledger entry declares the unknown origin class {origin_class}.",
                field="origin_class",
                offset=offset,
                value=origin_class,
            )
        if reason not in tuple(int(member) for member in LedgerReason):
            raise GrafxCorruptionDetected(
                f"A ledger entry declares the unknown reason code {reason}.",
                field="reason_code",
                offset=offset,
                value=reason,
            )
        if entry_type not in tuple(int(member) for member in LedgerEntryType):
            raise GrafxCorruptionDetected(
                f"A ledger entry declares the unknown entry type {entry_type}.",
                field="entry_type",
                offset=offset,
                value=entry_type,
            )
        return cls(
            entry_id=entry_id,
            origin_class=LedgerOriginClass(origin_class),
            reason=LedgerReason(reason),
            payload=payload,
            entry_type=LedgerEntryType(entry_type),
            lsn_start=lsn_start,
            lsn_end=lsn_end,
            epoch=epoch,
            captured_at_wall=captured_at_wall,
            format_version=format_version,
            reserved=reserved,
        )


def _require_bytes(raw: object) -> bytes:
    """Return the argument as bytes, refusing anything that is not a byte buffer."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"A ledger entry is decoded from bytes; got {type(raw).__name__}.",
            field="raw",
            value=type(raw).__name__,
        )
    return bytes(raw)
