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
    built_through_lsn u64 | reconciled_through_lsn u64 | digest 16B | artifact_nonce u64
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, PROVISIONAL_CSN, Lsn
from okto_grafx.domain.index.definition import DEFINITION_DIGEST_SIZE
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.visibility import IndexVisibility

__all__ = [
    "INDEX_HEADER_FORMAT_VERSION",
    "INDEX_HEADER_SIZE",
    "INDEX_HEADER_SLOT",
    "ORDERED_INDEX_HEADER_FORMAT_VERSION",
    "ORDERED_INDEX_HEADER_SIZE",
    "IndexHeader",
]

INDEX_HEADER_SLOT: int = 1
"""Slot 1 of the reserved header page. Slot 0 belongs to the file header C1 writes."""

INDEX_HEADER_FORMAT_VERSION: int = 2
"""The version the established hash layout writes. Version 1 remains readable."""

ORDERED_INDEX_HEADER_FORMAT_VERSION: int = 3
"""The header version that carries an explicit ordered-layout discriminator."""

_HEADER_V1_STRUCT: struct.Struct = struct.Struct("<HBBIIQQ16s")
_HEADER_STRUCT: struct.Struct = struct.Struct("<HBBIIQQ16sQ")
_ORDERED_HEADER_STRUCT: struct.Struct = struct.Struct("<HBBB3xIIQQ16sQ")

INDEX_HEADER_SIZE: int = _HEADER_STRUCT.size
"""Bytes of the established format-2 hash index header record."""

ORDERED_INDEX_HEADER_SIZE: int = _ORDERED_HEADER_STRUCT.size
"""Bytes of the format-3 ordered index header record."""

_LAYOUT_CODES: dict[IndexLayout, int] = {
    IndexLayout.HASH: 1,
    IndexLayout.ORDERED: 2,
}
_LAYOUT_BY_CODE: dict[int, IndexLayout] = {
    code: layout for layout, code in _LAYOUT_CODES.items()
}

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
    artifact_nonce: int = 0
    format_version: int = INDEX_HEADER_FORMAT_VERSION
    flags: int = 0
    layout: IndexLayout = IndexLayout.HASH

    def __post_init__(self) -> None:
        """Refuse a header whose fields could not be encoded or could not be true."""
        if not isinstance(self.visibility, IndexVisibility):
            raise GrafxIndexError(
                f"An index header needs an IndexVisibility; got {self.visibility!r}.",
                field="visibility",
                value=repr(self.visibility),
            )
        object.__setattr__(self, "layout", IndexLayout.parse(self.layout))
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
            ("artifact_nonce", self.artifact_nonce, _MAX_U64),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= ceiling
            ):
                raise GrafxIndexError(
                    f"Index header field {field!r} is outside its width: {value!r}.",
                    field=field,
                    value=repr(value),
                )
        if self.format_version > ORDERED_INDEX_HEADER_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                "This build cannot encode a future index header format.",
                field="format_version",
                value=self.format_version,
                supported=ORDERED_INDEX_HEADER_FORMAT_VERSION,
            )
        if self.format_version == 0:
            raise GrafxIndexError(
                "An index header cannot use format version zero.",
                field="format_version",
                value=0,
            )
        for field, value in (
            ("built_through_lsn", self.built_through_lsn),
            ("reconciled_through_lsn", self.reconciled_through_lsn),
        ):
            if value == PROVISIONAL_CSN:
                raise GrafxIndexError(
                    "An index header cannot claim the log position reserved for provisional "
                    f"heap versions in {field}.",
                    field=field,
                    value=value,
                )
        if self.format_version < ORDERED_INDEX_HEADER_FORMAT_VERSION:
            if self.layout is not IndexLayout.HASH:
                raise GrafxIndexError(
                    "Index header formats 1 and 2 describe only the hash layout.",
                    field="layout",
                    value=self.layout.value,
                    format_version=self.format_version,
                )
        elif self.format_version == ORDERED_INDEX_HEADER_FORMAT_VERSION:
            if self.layout is not IndexLayout.ORDERED:
                raise GrafxIndexError(
                    "Index header format 3 is reserved for the ordered layout.",
                    field="layout",
                    value=self.layout.value,
                    format_version=self.format_version,
                )
            if self.visibility is not IndexVisibility.EXACT:
                raise GrafxIndexError(
                    "Index header format 3 describes only an exact ordered access path.",
                    field="visibility",
                    value=self.visibility.value,
                    format_version=self.format_version,
                )
            if self.bucket_count != 1:
                raise GrafxIndexError(
                    "Index header format 3 reserves bucket_count=1 as its layout sentinel.",
                    field="bucket_count",
                    value=self.bucket_count,
                    format_version=self.format_version,
                )

    def advanced_to(self, lsn: Lsn) -> IndexHeader:
        """Return the header with its build position raised to this log position.

        Raised, never set. The position only ever moves forward: a record applied out of order,
        or a caller declaring a position it has already passed, must not be able to make an index
        claim it covers less than it does -- that would turn a fresh index into a stale one and
        force a rebuild that nothing needed.
        """
        if (
            isinstance(lsn, bool)
            or not isinstance(lsn, int)
            or not NO_LSN <= lsn < PROVISIONAL_CSN
        ):
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
        if (
            isinstance(horizon, bool)
            or not isinstance(horizon, int)
            or not NO_LSN <= horizon < PROVISIONAL_CSN
        ):
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
        if self.format_version < 2:
            if self.artifact_nonce != 0:
                raise GrafxIndexError(
                    "Index header format 1 cannot encode an artifact nonce.",
                    field="artifact_nonce",
                    value=self.artifact_nonce,
                    format_version=self.format_version,
                )
            return _HEADER_V1_STRUCT.pack(
                self.format_version,
                _VISIBILITY_CODES[self.visibility],
                self.flags,
                self.table_id,
                self.bucket_count,
                self.built_through_lsn,
                self.reconciled_through_lsn,
                self.digest,
            )
        if self.format_version == ORDERED_INDEX_HEADER_FORMAT_VERSION:
            return _ORDERED_HEADER_STRUCT.pack(
                self.format_version,
                _VISIBILITY_CODES[self.visibility],
                _LAYOUT_CODES[self.layout],
                self.flags,
                self.table_id,
                self.bucket_count,
                self.built_through_lsn,
                self.reconciled_through_lsn,
                self.digest,
                self.artifact_nonce,
            )
        return _HEADER_STRUCT.pack(
            self.format_version,
            _VISIBILITY_CODES[self.visibility],
            self.flags,
            self.table_id,
            self.bucket_count,
            self.built_through_lsn,
            self.reconciled_through_lsn,
            self.digest,
            self.artifact_nonce,
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
        if len(image) < _HEADER_V1_STRUCT.size:
            raise GrafxCorruptionDetected(
                f"An index header needs at least {_HEADER_V1_STRUCT.size} bytes; got "
                f"{len(image)}.",
                field="index_header",
                value=len(image),
            )
        format_version = struct.unpack_from("<H", image, 0)[0]
        if format_version > ORDERED_INDEX_HEADER_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"This build reads index format {ORDERED_INDEX_HEADER_FORMAT_VERSION} and "
                "below; the "
                f"file declares {format_version}.",
                field="format_version",
                value=format_version,
            )
        if format_version == 0:
            raise GrafxCorruptionDetected(
                "An index header declares format version zero.",
                field="format_version",
                value=0,
            )
        if format_version == ORDERED_INDEX_HEADER_FORMAT_VERSION:
            if len(image) < ORDERED_INDEX_HEADER_SIZE:
                raise GrafxCorruptionDetected(
                    f"An ordered index header needs {ORDERED_INDEX_HEADER_SIZE} bytes; got "
                    f"{len(image)}.",
                    field="index_header",
                    value=len(image),
                )
            (
                format_version,
                visibility,
                layout_code,
                flags,
                table_id,
                bucket_count,
                built_through_lsn,
                reconciled_through_lsn,
                digest,
                artifact_nonce,
            ) = _ORDERED_HEADER_STRUCT.unpack_from(image, 0)
            layout = _LAYOUT_BY_CODE.get(layout_code)
            if layout is None:
                raise GrafxCorruptionDetected(
                    f"An index header declares unknown layout code {layout_code}.",
                    field="layout",
                    value=layout_code,
                )
        elif format_version >= 2:
            if len(image) < INDEX_HEADER_SIZE:
                raise GrafxCorruptionDetected(
                    f"An index header at format {format_version} needs {INDEX_HEADER_SIZE} "
                    f"bytes; got {len(image)}.",
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
                artifact_nonce,
            ) = _HEADER_STRUCT.unpack_from(image, 0)
            layout = IndexLayout.HASH
        else:
            (
                format_version,
                visibility,
                flags,
                table_id,
                bucket_count,
                built_through_lsn,
                reconciled_through_lsn,
                digest,
            ) = _HEADER_V1_STRUCT.unpack_from(image, 0)
            artifact_nonce = 0
            layout = IndexLayout.HASH
        known = _VISIBILITY_BY_CODE.get(visibility)
        if known is None:
            raise GrafxCorruptionDetected(
                f"An index header declares the unknown visibility class {visibility}.",
                field="visibility",
                value=visibility,
            )
        if (
            built_through_lsn == PROVISIONAL_CSN
            or reconciled_through_lsn == PROVISIONAL_CSN
        ):
            field = (
                "built_through_lsn"
                if built_through_lsn == PROVISIONAL_CSN
                else "reconciled_through_lsn"
            )
            raise GrafxCorruptionDetected(
                "A persisted index header claims the log position reserved for provisional "
                f"heap versions in {field}.",
                field=field,
                value=PROVISIONAL_CSN,
            )
        return cls(
            visibility=known,
            table_id=table_id,
            bucket_count=bucket_count,
            digest=digest,
            built_through_lsn=built_through_lsn,
            reconciled_through_lsn=reconciled_through_lsn,
            artifact_nonce=artifact_nonce,
            format_version=format_version,
            flags=flags,
            layout=layout,
        )
