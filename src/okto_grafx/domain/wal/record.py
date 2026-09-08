"""The write-ahead log record (CONTRACT.md section 6.5, SPEC-M1 TR-4 and FR-5).

One record is 48 bytes of fixed header, then a descriptor, then a payload, then a CRC-32C over
everything before it. Nothing outside the record is needed to read it: the magic says a record
starts here, the format version says how to read it, the descriptor says under which granularity
the payload was produced, and the length says where the next one begins. That is what
self-describing buys -- a record found in isolation, in a segment whose neighbours are gone,
still decodes.

The consequence the spec cares about is that changing ``partitions_per_table`` changes the
descriptor string and nothing else. No format version moves, no migration runs, and a log written
under one granularity stays readable under another.

The checksum is CRC-32C, and it is the one C1 already owns: a second implementation of the same
polynomial is a second thing that can drift.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from enum import IntEnum

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import NO_LSN, Epoch, Lsn, TxnId
from okto_grafx.domain.page.checksum import crc32c

__all__ = [
    "WAL_MAGIC",
    "WAL_FORMAT_VERSION",
    "WAL_LEGACY_FORMAT_VERSION",
    "WAL_V2_FLAG_REQUIRED",
    "WAL_V2_FLAG_SKIPPABLE",
    "WAL_V2_FLAG_PAGE_IMAGE_ZLIB1",
    "WAL_V2_FLAG_COMMIT_CATALOG_V1",
    "WAL_HEADER_LENGTH",
    "CHECKSUM_LENGTH",
    "SUPPORTED_FORMAT_VERSIONS",
    "HEADER_LENGTHS",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_TOTAL_LENGTH",
    "MAX_U16",
    "MAX_U32",
    "MAX_U64",
    "WalRecordType",
    "WalRecord",
    "header_length_of",
    "is_known_record_type",
    "v2_record_semantics_error",
]

WAL_MAGIC: int = 0x5852474F
"""First four bytes of every record, little-endian, so a scanner can find one inside a hole."""

WAL_LEGACY_FORMAT_VERSION: int = 1
"""The legacy format retained for records whose payload grammar did not change."""

WAL_FORMAT_VERSION: int = 2
"""The highest record version this build can emit, not a global automatic selection."""

WAL_V2_FLAG_REQUIRED: int = 0x0001
"""The v2 record carries semantics that a reader must understand before continuing."""

WAL_V2_FLAG_SKIPPABLE: int = 0x0008
"""An unknown v2 record is explicitly safe for an older decoder to skip."""

WAL_V2_FLAG_PAGE_IMAGE_ZLIB1: int = 0x0004
"""The WRITE_PAGE image body uses bounded zlib level-1 compression."""

WAL_V2_FLAG_COMMIT_CATALOG_V1: int = 0x0010
"""Required WRITE_PAGE semantics for commit history, independent of compression."""

WAL_HEADER_LENGTH: int = 48
"""Bytes of fixed header in a version 1 record."""

CHECKSUM_LENGTH: int = 4
"""Bytes of CRC-32C that close every record."""

SUPPORTED_FORMAT_VERSIONS: tuple[int, ...] = (
    WAL_LEGACY_FORMAT_VERSION,
    WAL_FORMAT_VERSION,
)
"""Every version this build can decode, oldest first.

The decoder rule of CONTRACT.md section 6.5 is that a reader accepts every version at or below
its own, so this tuple is the closed set the round-trip suite walks, and a version outside it is
refused rather than guessed at.
"""

HEADER_LENGTHS: dict[int, int] = {
    WAL_LEGACY_FORMAT_VERSION: WAL_HEADER_LENGTH,
    WAL_FORMAT_VERSION: WAL_HEADER_LENGTH,
}
"""Header length declared by each supported version.

The decoder reads the header length out of the record rather than assuming it, so a later
version may grow its header without this one having to guess where the descriptor starts.
"""

MAX_U16: int = 0xFFFF
"""Ceiling of every 16-bit field of the record header."""

MAX_U32: int = 0xFFFFFFFF
"""Ceiling of every 32-bit field of the record header."""

MAX_U64: int = 0xFFFFFFFFFFFFFFFF
"""Ceiling of every 64-bit field of the record header."""

MAX_DESCRIPTOR_BYTES: int = 4096
"""Bytes a granularity descriptor may occupy once encoded as UTF-8."""

MAX_TOTAL_LENGTH: int = MAX_U32
"""The total length field is a u32, so no record may encode to more than this."""

_HEADER = struct.Struct("<IHHHHIQQQII")
_CHECKSUM = struct.Struct("<I")


class WalRecordType(IntEnum):
    """The record types of CONTRACT.md section 6.5, with their frozen numeric codes."""

    BEGIN = 1
    WRITE_PAGE = 2
    COMMIT = 3
    ABORT = 4
    CHECKPOINT = 5
    INDEX_WRITE = 6
    INDEX_RECONCILE = 7
    LEDGER_APPEND = 8
    CATALOG_WRITE = 9
    SPACE_DDL = 10
    VECTOR_WRITE = 11
    SEGMENT_HEADER = 12
    PAGE_ALLOC = 13


_KNOWN_TYPES: frozenset[int] = frozenset(int(member) for member in WalRecordType)


def is_known_record_type(record_type: int) -> bool:
    """Return True when this build knows what the record type means."""
    return record_type in _KNOWN_TYPES


def v2_record_semantics_error(record_type: int, flags: int) -> str | None:
    """Return why a v2 type/flags pair is unsupported, or ``None`` when it is closed-safe."""

    compressed_page_flags = WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1
    journal_flags = WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_COMMIT_CATALOG_V1
    if record_type == int(WalRecordType.WRITE_PAGE):
        if flags in {compressed_page_flags, journal_flags, journal_flags | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1}:
            return None
        return (
            "WRITE_PAGE v2 requires exactly compressed-page (0x0005), "
            "commit-catalog (0x0011), or compressed commit-catalog (0x0015) flags; got "
            f"0x{flags:04x}."
        )
    if not is_known_record_type(record_type):
        if flags == WAL_V2_FLAG_SKIPPABLE:
            return None
        return (
            f"Unknown WAL-v2 record type {record_type} must carry exactly the explicit "
            f"SKIPPABLE flag 0x{WAL_V2_FLAG_SKIPPABLE:04x}; got 0x{flags:04x}."
        )
    return (
        f"Known WAL record type {record_type} has no format-v2 grammar in this build."
    )


def header_length_of(format_version: int) -> int:
    """Return the header length a format version declares, refusing an unknown version."""
    length = HEADER_LENGTHS.get(format_version)
    if length is None:
        raise GrafxConfigurationError(
            f"Format version {format_version!r} is not one this build writes or reads.",
            field="format_version",
            value=format_version,
        )
    return length


def _require_unsigned(field: str, value: object, ceiling: int) -> int:
    """Return the value as an unsigned integer inside its field width, or refuse it.

    A bool is refused with everything else: True would silently mean one, and a caller that
    passed a flag where a counter belongs would write a record nobody asked for. The refusal is
    a configuration error and never a corruption, because A11-revised reserves
    ``corruption_detected`` for damaged bytes and these are caller arguments.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a log record must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= ceiling:
        raise GrafxConfigurationError(
            f"The {field} of a log record must be between 0 and {ceiling}; got {value}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class WalRecord:
    """One self-describing write-ahead log record.

    The log sequence number is zero until the manager assigns one, because the order of records
    is the log's property and not the caller's. Everything else belongs to the caller: what kind
    of record it is, which epoch and which transaction produced it, and the bytes it carries.
    """

    record_type: int
    payload: bytes = b""
    descriptor: str = ""
    lsn: Lsn = NO_LSN
    epoch: Epoch = 0
    txn_id: TxnId = 0
    flags: int = 0
    format_version: int = WAL_LEGACY_FORMAT_VERSION
    # One private slot per record, reserved for the codec that proves this record's payload
    # (today: the index change codec in ``okto_grafx.domain.index.records``).  It is not a field
    # of the value -- never compared, printed, replaced or written to the device -- and this
    # class offers no door to it: the owning codec seals and reads it under its own private
    # proof protocol, so an ordinary caller cannot plant a decoded value in a record.
    _decoded: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Check every field against the width the format gives it, before any packing.

        Range checking here rather than at ``struct.pack`` is what keeps a raw ``struct.error``
        out of a public door (amendment A41): a field that does not fit is a typed refusal
        naming the field, never an exception from the standard library.
        """
        _require_unsigned("record_type", self.record_type, MAX_U16)
        _require_unsigned("format_version", self.format_version, MAX_U16)
        _require_unsigned("flags", self.flags, MAX_U16)
        _require_unsigned("lsn", self.lsn, MAX_U64)
        _require_unsigned("epoch", self.epoch, MAX_U64)
        _require_unsigned("txn_id", self.txn_id, MAX_U64)
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise GrafxConfigurationError(
                f"The payload of a log record must be bytes; got {type(self.payload).__name__}.",
                field="payload",
                value=type(self.payload).__name__,
            )
        if not isinstance(self.payload, bytes):
            object.__setattr__(self, "payload", bytes(self.payload))
        if not isinstance(self.descriptor, str):
            raise GrafxConfigurationError(
                "The descriptor of a log record must be a string; got "
                f"{type(self.descriptor).__name__}.",
                field="descriptor",
                value=type(self.descriptor).__name__,
            )
        encoded = self._encoded_descriptor()
        if len(encoded) > MAX_DESCRIPTOR_BYTES:
            raise GrafxConfigurationError(
                f"A descriptor holds at most {MAX_DESCRIPTOR_BYTES} bytes; got {len(encoded)}.",
                field="descriptor",
                value=len(encoded),
            )
        if len(self.payload) > MAX_U32:
            raise GrafxConfigurationError(
                f"A payload holds at most {MAX_U32} bytes; got {len(self.payload)}.",
                field="payload",
                value=len(self.payload),
            )

    def _encoded_descriptor(self) -> bytes:
        """Return the descriptor as UTF-8, refusing a string that cannot be encoded at all."""
        try:
            return self.descriptor.encode("utf-8")
        except UnicodeEncodeError as failure:
            raise GrafxConfigurationError(
                "The descriptor of a log record must be encodable as UTF-8.",
                field="descriptor",
                value=repr(self.descriptor),
            ) from failure

    @property
    def is_known_type(self) -> bool:
        """Return True when this build knows what to do with this record type.

        An unknown type is decodable on purpose: the format is self-describing, so a reader that
        meets a record from a later build skips it by its own declared length instead of
        declaring the log corrupt. Writing one is refused, because that would be this build
        inventing a meaning it does not have.
        """
        return is_known_record_type(self.record_type)

    def encoded_length(self) -> int:
        """Return how many bytes this record occupies on disk."""
        return (
            header_length_of(self.format_version)
            + len(self._encoded_descriptor())
            + len(self.payload)
            + CHECKSUM_LENGTH
        )

    def with_lsn(self, lsn: Lsn) -> WalRecord:
        """Return the same record carrying the log sequence number the manager assigned."""
        return replace(self, lsn=lsn)

    def with_descriptor(self, descriptor: str) -> WalRecord:
        """Return the same record carrying the granularity descriptor of the log it enters."""
        return replace(self, descriptor=descriptor)

    def encode(self) -> bytes:
        """Return the on-disk image of this record, checksum included.

        Only a version this build writes and a record type it understands may be encoded. The
        decoder is deliberately more tolerant than this: reading is where forward compatibility
        belongs, writing is where a mistake becomes permanent.

        The version is refused by :func:`header_length_of` below rather than by a check of its
        own. An explicit check here stood in front of it and refused the same inputs with the
        same error class and the same ``field``, so no test could tell which of the two answered
        and a mutation battery found the pair mutually untestable (A62, A67). One door, one
        refusal, one message.
        """
        header_length = header_length_of(self.format_version)
        if not self.is_known_type:
            raise GrafxConfigurationError(
                f"Record type {self.record_type} is not one this build knows how to write.",
                field="record_type",
                value=self.record_type,
            )
        if self.format_version == WAL_FORMAT_VERSION:
            semantic_error = v2_record_semantics_error(self.record_type, self.flags)
            if semantic_error is not None:
                raise GrafxConfigurationError(
                    semantic_error,
                    field="flags",
                    value=self.flags,
                    record_type=self.record_type,
                    format_version=self.format_version,
                )
        descriptor = self._encoded_descriptor()
        total = header_length + len(descriptor) + len(self.payload) + CHECKSUM_LENGTH
        if total > MAX_TOTAL_LENGTH:
            raise GrafxConfigurationError(
                f"A log record encodes to at most {MAX_TOTAL_LENGTH} bytes; got {total}.",
                field="total_length",
                value=total,
            )
        head = _HEADER.pack(
            WAL_MAGIC,
            self.format_version,
            self.record_type,
            header_length,
            self.flags,
            total,
            self.lsn,
            self.epoch,
            self.txn_id,
            len(descriptor),
            len(self.payload),
        )
        body = b"".join((head, descriptor, self.payload))
        return b"".join((body, _CHECKSUM.pack(crc32c(body))))
