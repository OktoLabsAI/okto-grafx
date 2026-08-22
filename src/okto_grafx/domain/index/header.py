"""The index header: what an index file says about itself (SPEC-M1 FR-12, BR-11).

It sits in slot 1 of the reserved header page of the index file, directly after the file header
C1 owns (amendment A2), and it carries the three facts a reader needs before it may trust a
single entry:

* the DEFINITION digest -- which index this file belongs to. A file opened under a definition
  whose digest disagrees is not a stale index, it is a different index, and the difference
  matters: one is repaired by rebuilding, the other by not opening it;
* ``built_through_lsn`` -- the log position through which this file is known to reflect the heap.
  An index behind that position is STALE, and a stale index must refuse to answer rather than
  omit a row, because an omission is a wrong result and a refusal is not;
* ``reconciled_through_lsn`` -- the horizon the last reconciliation pass applied. A verification
  walk needs it to tell an entry that was correctly reclaimed from one that went missing.

Layout, little-endian::

    format_version u16 | visibility u8 | flags u8 | table_id u32 | bucket_count u32 |
    built_through_lsn u64 | reconciled_through_lsn u64 | digest 16B
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.index.definition import DEFINITION_DIGEST_SIZE
from okto_grafx.domain.index.visibility import IndexVisibility

__all__ = [
    "INDEX_HEADER_FORMAT_VERSION",
    "INDEX_HEADER_SIZE",
    "INDEX_HEADER_SLOT",
    "IndexHeader",
]

INDEX_HEADER_SLOT: int = 1
"""Slot 1 of the reserved header page. Slot 0 belongs to the file header C1 writes."""

INDEX_HEADER_FORMAT_VERSION: int = 1
"""The version this build writes. Every earlier version stays readable."""

_HEADER_STRUCT: struct.Struct = struct.Struct("<HBBIIQQ16s")

INDEX_HEADER_SIZE: int = _HEADER_STRUCT.size
"""Bytes of the encoded index header record."""

_VISIBILITY_CODES: dict[IndexVisibility, int] = {
    IndexVisibility.EXACT: 1,
    IndexVisibility.PROXIMITY: 2,
}
"""The numeric code each visibility class is stored under, frozen with the format.

The enum VALUE is a word and would be a second spelling to keep in step; a code is what the file
carries and the mapping is stated once, here, in both directions.
"""

_VISIBILITY_BY_CODE: dict[int, IndexVisibility] = {
    code: visibility for visibility, code in _VISIBILITY_CODES.items()
}

_MAX_U32: int = 0xFFFFFFFF
_MAX_U64: int = 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True, slots=True)
class IndexHeader:
    """The self-description of one index file."""

    visibility: IndexVisibility
    table_id: int
    bucket_count: int
    digest: bytes
    built_through_lsn: Lsn = NO_LSN
    reconciled_through_lsn: Lsn = NO_LSN
    format_version: int = INDEX_HEADER_FORMAT_VERSION
    flags: int = 0

    def __post_init__(self) -> None:
        """Refuse a header whose fields could not be encoded or could not be true."""
        if not isinstance(self.visibility, IndexVisibility):
            raise GrafxIndexError(
                f"An index header needs an IndexVisibility; got {self.visibility!r}.",
                field="visibility",
                value=repr(self.visibility),
            )
        if not isinstance(self.digest, (bytes, bytearray, memoryview)):
            raise GrafxIndexError(
                f"An index header needs a digest of bytes; got {type(self.digest).__name__}.",
                field="digest",
                value=type(self.digest).__name__,
            )
        if not isinstance(self.digest, bytes):
            object.__setattr__(self, "digest", bytes(self.digest))
        if len(self.digest) != DEFINITION_DIGEST_SIZE:
            raise GrafxIndexError(
                f"An index header digest is {DEFINITION_DIGEST_SIZE} bytes; got "
                f"{len(self.digest)}.",
                field="digest",
                value=len(self.digest),
            )
        for field, value, ceiling in (
            ("format_version", self.format_version, 0xFFFF),
            ("flags", self.flags, 0xFF),
            ("table_id", self.table_id, _MAX_U32),
            ("bucket_count", self.bucket_count, _MAX_U32),
            ("built_through_lsn", self.built_through_lsn, _MAX_U64),
            ("reconciled_through_lsn", self.reconciled_through_lsn, _MAX_U64),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ceiling:
                raise GrafxIndexError(
                    f"Index header field {field!r} is outside its width: {value!r}.",
                    field=field,
                    value=repr(value),
                )

    def advanced_to(self, lsn: Lsn) -> IndexHeader:
        """Return the header with its build position raised to this log position.

        Raised, never set. The position only ever moves forward: a record applied out of order,
        or a caller declaring a position it has already passed, must not be able to make an index
        claim it covers less than it does -- that would turn a fresh index into a stale one and
        force a rebuild that nothing needed.
        """
        if isinstance(lsn, bool) or not isinstance(lsn, int) or lsn < NO_LSN:
            raise GrafxIndexError(
                f"A build position must be a non-negative integer; got {lsn!r}.",
                field="built_through_lsn",
                value=repr(lsn),
            )
        if lsn <= self.built_through_lsn:
            return self
        return replace(self, built_through_lsn=lsn)

    def reconciled_to(self, horizon: Lsn) -> IndexHeader:
        """Return the header with its reconciliation horizon raised to this position."""
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < NO_LSN:
            raise GrafxIndexError(
                f"A reconciliation horizon must be a non-negative integer; got {horizon!r}.",
                field="reconciled_through_lsn",
                value=repr(horizon),
            )
        if horizon <= self.reconciled_through_lsn:
            return self
        return replace(self, reconciled_through_lsn=horizon)

    def encode(self) -> bytes:
        """Return the encoded index header record."""
        return _HEADER_STRUCT.pack(
            self.format_version,
            _VISIBILITY_CODES[self.visibility],
            self.flags,
            self.table_id,
            self.bucket_count,
            self.built_through_lsn,
            self.reconciled_through_lsn,
            self.digest,
        )

    @classmethod
    def decode(cls, raw: bytes) -> IndexHeader:
        """Parse an index header record, refusing a future format or an unknown class."""
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise GrafxCorruptionDetected(
                f"An index header must be bytes; got {type(raw).__name__}.",
                field="index_header",
                value=type(raw).__name__,
            )
        image = bytes(raw)
        if len(image) < INDEX_HEADER_SIZE:
            raise GrafxCorruptionDetected(
                f"An index header needs {INDEX_HEADER_SIZE} bytes; got {len(image)}.",
                field="index_header",
                value=len(image),
            )
        (
            format_version,
            visibility,
            flags,
            table_id,
            bucket_count,
            built_through_lsn,
            reconciled_through_lsn,
            digest,
        ) = _HEADER_STRUCT.unpack_from(image, 0)
        if format_version > INDEX_HEADER_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"This build reads index format {INDEX_HEADER_FORMAT_VERSION} and below; the "
                f"file declares {format_version}.",
                field="format_version",
                value=format_version,
            )
        known = _VISIBILITY_BY_CODE.get(visibility)
        if known is None:
            raise GrafxCorruptionDetected(
                f"An index header declares the unknown visibility class {visibility}.",
                field="visibility",
                value=visibility,
            )
        return cls(
            visibility=known,
            table_id=table_id,
            bucket_count=bucket_count,
            digest=digest,
            built_through_lsn=built_through_lsn,
            reconciled_through_lsn=reconciled_through_lsn,
            format_version=format_version,
            flags=flags,
        )
