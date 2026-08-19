"""The heap record header and the version it describes (CONTRACT.md section 6.4).

Every version of every record starts with the same 40 bytes inside its slot:

    0  u8  flags          bit 0 deleted, bit 1 has_overflow
    1  u8  reserved
    2  u16 schema_version
    4  u32 payload_len
    8  u64 record_id
    16 u64 xmin_csn
    24 u64 xmax_csn       0 means this version is still live
    32 u64 prev_version   RecordRef.encode() of the previous version, 0 means none

The sentinel of prev_version is the literal 0 and never NULL_REF.encode(): page 0 of every paged
file is the reserved header page (amendment A2), so no real reference can encode to zero and the
sentinel is unambiguous.

The header says nothing about visibility. xmin and xmax are facts about when a version was
written and when it stopped being current; deciding whether a reader may see it belongs to the
snapshot of that reader, and the heap never applies that decision on its own.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import Csn, RecordId, RecordRef
from okto_grafx.domain.model.value import Value

__all__ = [
    "RECORD_HEADER_SIZE",
    "RECORD_FLAG_DELETED",
    "RECORD_FLAG_HAS_OVERFLOW",
    "NO_PREVIOUS_VERSION",
    "OVERFLOW_POINTER_SIZE",
    "RecordHeader",
    "HeapVersion",
    "encode_overflow_pointer",
    "decode_overflow_pointer",
]

RECORD_HEADER_SIZE: int = 40
"""Bytes of the fixed header that opens every heap record."""

RECORD_FLAG_DELETED: int = 0x01
"""Bit 0 of the flag byte: this version was deleted rather than superseded."""

RECORD_FLAG_HAS_OVERFLOW: int = 0x02
"""Bit 1 of the flag byte: the payload lives in an overflow chain, not in this slot."""

NO_PREVIOUS_VERSION: int = 0
"""The end of a version chain, written as the literal zero (amendment A2)."""

_HEADER_STRUCT = struct.Struct("<BBHIQQQQ")
_POINTER_STRUCT = struct.Struct("<I")

OVERFLOW_POINTER_SIZE: int = _POINTER_STRUCT.size
"""Bytes that follow the header of a record whose payload overflowed: the first chain page."""

_MAX_U8 = 0xFF
_MAX_U16 = 0xFFFF
_MAX_U32 = 0xFFFFFFFF
_MAX_U64 = 0xFFFFFFFFFFFFFFFF


def _require_unsigned(field: str, value: int, maximum: int) -> int:
    """Return the value after checking that it fits the unsigned field it is written to."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxCorruptionDetected(
            f"Record header field {field!r} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= maximum:
        raise GrafxCorruptionDetected(
            f"Record header field {field!r} is outside its width: {value} exceeds {maximum}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class RecordHeader:
    """The fixed part of one stored version of one record."""

    record_id: RecordId
    xmin: Csn
    xmax: Csn = 0
    prev_version: int = NO_PREVIOUS_VERSION
    payload_len: int = 0
    schema_version: int = 1
    flags: int = 0
    reserved: int = 0

    @property
    def deleted(self) -> bool:
        """Return True when this version was ended by a delete rather than by an update."""
        return bool(self.flags & RECORD_FLAG_DELETED)

    @property
    def has_overflow(self) -> bool:
        """Return True when the payload of this version lives in an overflow chain."""
        return bool(self.flags & RECORD_FLAG_HAS_OVERFLOW)

    @property
    def previous(self) -> RecordRef | None:
        """Return the reference to the previous version, or None at the end of the chain."""
        if self.prev_version == NO_PREVIOUS_VERSION:
            return None
        return RecordRef.decode(self.prev_version)

    def ended_at(self, xmax: Csn, *, deleted: bool) -> RecordHeader:
        """Return the same header with its xmax set, marking a delete when that is the reason."""
        flags = (self.flags | RECORD_FLAG_DELETED) if deleted else self.flags
        return replace(self, xmax=xmax, flags=flags)

    def encode(self) -> bytes:
        """Return the 40 header bytes."""
        return _HEADER_STRUCT.pack(
            _require_unsigned("flags", self.flags, _MAX_U8),
            _require_unsigned("reserved", self.reserved, _MAX_U8),
            _require_unsigned("schema_version", self.schema_version, _MAX_U16),
            _require_unsigned("payload_len", self.payload_len, _MAX_U32),
            _require_unsigned("record_id", self.record_id, _MAX_U64),
            _require_unsigned("xmin", self.xmin, _MAX_U64),
            _require_unsigned("xmax", self.xmax, _MAX_U64),
            _require_unsigned("prev_version", self.prev_version, _MAX_U64),
        )

    @classmethod
    def decode(cls, raw: bytes) -> RecordHeader:
        """Parse the first 40 bytes of a slot payload into a record header."""
        if len(raw) < RECORD_HEADER_SIZE:
            raise GrafxCorruptionDetected(
                f"A record header needs {RECORD_HEADER_SIZE} bytes; got {len(raw)}.",
                field="record_header",
                value=len(raw),
            )
        flags, reserved, schema_version, payload_len, record_id, xmin, xmax, prev = (
            _HEADER_STRUCT.unpack_from(raw, 0)
        )
        return cls(
            record_id=record_id,
            xmin=xmin,
            xmax=xmax,
            prev_version=prev,
            payload_len=payload_len,
            schema_version=schema_version,
            flags=flags,
            reserved=reserved,
        )


@dataclass(frozen=True, slots=True)
class HeapVersion:
    """One decoded version of one record, exactly as it is stored.

    Visibility is deliberately absent from this value: it carries xmin and xmax, and the caller
    decides with its own snapshot whether the version may be seen (CONTRACT.md section 8.2).
    """

    record_id: RecordId
    xmin: Csn
    xmax: Csn
    values: tuple[Value, ...]
    prev: RecordRef | None
    schema_version: int
    deleted: bool
    table_id: int

    @property
    def live(self) -> bool:
        """Return True when no later version and no delete has ended this one."""
        return self.xmax == 0


def encode_overflow_pointer(first_page: int) -> bytes:
    """Return the four bytes that follow the header of a record with an overflow chain."""
    return _POINTER_STRUCT.pack(_require_unsigned("overflow_page", first_page, _MAX_U32))


def decode_overflow_pointer(raw: bytes, offset: int = 0) -> int:
    """Return the first overflow page recorded after the header of a record."""
    if offset + _POINTER_STRUCT.size > len(raw):
        raise GrafxCorruptionDetected(
            f"A record with an overflow chain needs {_POINTER_STRUCT.size} bytes for its first "
            f"page at offset {offset}; the slot holds {len(raw)}.",
            field="overflow_page",
            offset=offset,
            length=len(raw),
        )
    return int(_POINTER_STRUCT.unpack_from(raw, offset)[0])
