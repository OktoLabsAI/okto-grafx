"""The page-write payload a transaction produces (SPEC-M1 FR-5, TR-4).

C5 creates two of the thirteen record types of CONTRACT.md section 6.5: the ``WRITE_PAGE``
records that carry the pages a transaction changed, and the ``COMMIT`` record that makes them
visible. The record ENVELOPE and the COMMIT payload are the log format, owned by C4 in
``domain/wal/**``; ``WalRecord`` and ``WalRecordType`` are imported from there rather than
declared again (amendment A24).

What section 6.5 leaves open, and what this module fixes, is what a ``WRITE_PAGE`` record
CARRIES. It is deliberately SELF-DESCRIBING (TR-4): it names its file rather than referring to a
table of file numbers, so a replay can redo it with nothing but the record in front of it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import Lsn, PageIndex
from okto_grafx.domain.wal.record import WAL_FORMAT_VERSION, WalRecord, WalRecordType

__all__ = [
    "MAX_FILE_NAME_BYTES",
    "WAL_FORMAT_VERSION",
    "PageWrite",
    "WalRecord",
    "WalRecordLike",
    "WalRecordType",
    "decode_page_write",
    "encode_page_write",
]

MAX_FILE_NAME_BYTES: int = 0xFFFF
"""Longest file name a page-write payload can carry: the length prefix is 16 bits."""

_NAME_LENGTH: struct.Struct = struct.Struct("<H")
_PAGE_INDEX: struct.Struct = struct.Struct("<I")
_MAX_PAGE_INDEX_FIELD: int = 0xFFFFFFFF


@runtime_checkable
class WalRecordLike(Protocol):
    """The part of a log record C5 reads back when it validates a commit.

    Taken structurally on purpose. The record format and its framing belong to C4; what C5 needs
    is a type, an LSN and a payload, and asking for those by shape rather than by class lets a
    test stand a log in without either component having to know about the other.
    """

    @property
    def record_type(self) -> int:
        """Return the record type of CONTRACT.md section 6.5."""
        ...

    @property
    def lsn(self) -> Lsn:
        """Return the log sequence number the log assigned to this record."""
        ...

    @property
    def payload(self) -> bytes:
        """Return the payload bytes of this record."""
        ...


def encode_page_write(file: str, page_index: PageIndex, image: bytes) -> bytes:
    """Return the payload of a WRITE_PAGE record: the file it belongs to, its index, its bytes.

    The name travels with the record because TR-4 asks every record to describe itself. A number
    standing for a file would need a table to interpret, and a replay that has to consult a table
    it may not have recovered yet is a replay that can silently redo the wrong file.
    """
    if not isinstance(file, str) or not file:
        raise GrafxConfigurationError(
            "A page-write record must name a non-empty file.",
            field="file",
            value=repr(file),
        )
    encoded = file.encode("utf-8")
    if len(encoded) > MAX_FILE_NAME_BYTES:
        raise GrafxConfigurationError(
            f"A page-write record can name a file of at most {MAX_FILE_NAME_BYTES} bytes; "
            f"this one is {len(encoded)}.",
            field="file",
            value=len(encoded),
        )
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise GrafxConfigurationError(
            f"A page index must be an integer; got {type(page_index).__name__}.",
            field="page_index",
            value=repr(page_index),
        )
    if not 0 <= page_index <= _MAX_PAGE_INDEX_FIELD:
        raise GrafxConfigurationError(
            f"A page index must fit in 32 bits; got {page_index}.",
            field="page_index",
            value=page_index,
        )
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"A page image must be bytes; got {type(image).__name__}.",
            field="image",
            value=type(image).__name__,
        )
    return b"".join(
        (
            _NAME_LENGTH.pack(len(encoded)),
            encoded,
            _PAGE_INDEX.pack(page_index),
            bytes(image),
        )
    )


@dataclass(frozen=True, slots=True)
class PageWrite:
    """What a WRITE_PAGE payload says: which page of which file, and what it should hold."""

    file: str
    page_index: PageIndex
    image: bytes


def decode_page_write(payload: bytes) -> PageWrite:
    """Parse a WRITE_PAGE payload back into the file, the page and the image it carries."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise GrafxCorruptionDetected(
            f"A page-write payload must be bytes; got {type(payload).__name__}.",
            field="payload",
            value=type(payload).__name__,
        )
    raw = bytes(payload)
    head = _NAME_LENGTH.size
    if len(raw) < head:
        raise GrafxCorruptionDetected(
            f"A page-write payload needs at least {head} bytes for its name length; "
            f"this one has {len(raw)}.",
            field="payload",
            value=len(raw),
        )
    (name_length,) = _NAME_LENGTH.unpack_from(raw, 0)
    minimum = head + name_length + _PAGE_INDEX.size
    if len(raw) < minimum:
        raise GrafxCorruptionDetected(
            f"A page-write payload declaring a name of {name_length} bytes needs at least "
            f"{minimum} bytes; this one has {len(raw)}.",
            field="payload",
            value=len(raw),
            declared=name_length,
        )
    try:
        file = raw[head : head + name_length].decode("utf-8")
    except UnicodeDecodeError as damaged:
        raise GrafxCorruptionDetected(
            "The file name of a page-write payload is not valid UTF-8.",
            field="file",
            value=name_length,
        ) from damaged
    (page_index,) = _PAGE_INDEX.unpack_from(raw, head + name_length)
    return PageWrite(file=file, page_index=page_index, image=raw[minimum:])
