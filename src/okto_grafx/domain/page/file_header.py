"""Page 0 of every paged file: the reserved header page (CONTRACT.md section 6.3, amendment A2).

No heap, catalog or index record ever lives on page 0. The consequence is what the heap record
format depends on: a real RecordRef can never encode to zero, so zero is an unambiguous "there is
no previous version" at the end of a version chain.

The header page is an ordinary slotted page of type META whose slot 0 holds the fields below.
Everything else on the page belongs to whoever owns the file: the heap keeps its table directory
in the slots that follow, and the catalog keeps nothing there at all.

    magic 8B "OKTOGRFX" | format_version u16 | file_kind u16 | page_size u32 |
    root_page u32 | payload_length u64
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.ids import NO_PAGE, PageIndex, SlotId
from okto_grafx.domain.page.layout import MAX_U32, MAX_U64, PageType, validate_page_size
from okto_grafx.domain.page.slotted import Page

__all__ = [
    "FILE_HEADER_MAGIC",
    "FILE_HEADER_FORMAT_VERSION",
    "FILE_HEADER_SIZE",
    "HEADER_PAGE_INDEX",
    "HEADER_SLOT",
    "FileKind",
    "FileHeader",
    "FileHeaderPage",
]

FILE_HEADER_MAGIC: bytes = b"OKTOGRFX"
"""The eight bytes that say this file belongs to Okto Grafx."""

FILE_HEADER_FORMAT_VERSION: int = 1
"""The version this build writes. Every earlier version stays readable."""

HEADER_PAGE_INDEX: PageIndex = 0
"""Page 0 of every paged file is reserved for the header page."""

HEADER_SLOT: SlotId = 0
"""Slot 0 of the header page carries the fields of the file header."""

_HEADER_STRUCT: struct.Struct = struct.Struct("<8sHHIIQ")

FILE_HEADER_SIZE: int = _HEADER_STRUCT.size
"""Bytes of the encoded file header record."""


class FileKind(IntEnum):
    """What a paged file holds, recorded in its header page so the file is self-describing."""

    META = 1
    HEAP = 2
    CATALOG = 3
    INDEX = 4


@dataclass(frozen=True, slots=True)
class FileHeader:
    """The identity of one paged file plus the two fields a chained payload needs.

    root_page and payload_length are what the catalog uses to find the chain that holds its
    serialised form; a file that keeps no chained payload leaves them at NO_PAGE and zero.
    """

    kind: FileKind
    page_size: int
    format_version: int = FILE_HEADER_FORMAT_VERSION
    root_page: PageIndex = NO_PAGE
    payload_length: int = 0

    def __post_init__(self) -> None:
        """Reject a header whose fields could not be encoded or could not be true."""
        if not isinstance(self.kind, FileKind):
            raise GrafxCorruptionDetected(
                f"A file header needs a FileKind; got {self.kind!r}.",
                field="kind",
                value=repr(self.kind),
            )
        validate_page_size(self.page_size)
        for field, value, maximum in (
            ("format_version", self.format_version, 0xFFFF),
            ("root_page", self.root_page, MAX_U32),
            ("payload_length", self.payload_length, MAX_U64),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise GrafxCorruptionDetected(
                    f"File header field {field!r} is outside its width: {value!r}.",
                    field=field,
                    value=repr(value),
                )

    def encode(self) -> bytes:
        """Return the encoded file header record."""
        return _HEADER_STRUCT.pack(
            FILE_HEADER_MAGIC,
            self.format_version,
            int(self.kind),
            self.page_size,
            self.root_page,
            self.payload_length,
        )

    @classmethod
    def decode(cls, raw: bytes) -> FileHeader:
        """Parse a file header record, refusing a foreign file or a future format."""
        if len(raw) < FILE_HEADER_SIZE:
            raise GrafxCorruptionDetected(
                f"A file header needs {FILE_HEADER_SIZE} bytes; got {len(raw)}.",
                field="file_header",
                value=len(raw),
            )
        magic, format_version, kind, page_size, root_page, payload_length = (
            _HEADER_STRUCT.unpack_from(raw, 0)
        )
        if magic != FILE_HEADER_MAGIC:
            raise GrafxCorruptionDetected(
                f"This file does not start with the Okto Grafx magic; got {magic!r}.",
                field="magic",
                value=repr(magic),
            )
        if format_version > FILE_HEADER_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"This build reads file format {FILE_HEADER_FORMAT_VERSION} and below; the file "
                f"declares {format_version}.",
                field="format_version",
                value=format_version,
            )
        if kind not in tuple(int(member) for member in FileKind):
            raise GrafxCorruptionDetected(
                f"A file header declares the unknown file kind {kind}.",
                field="kind",
                value=kind,
            )
        return cls(
            kind=FileKind(kind),
            page_size=page_size,
            format_version=format_version,
            root_page=root_page,
            payload_length=payload_length,
        )


class FileHeaderPage:
    """Read and write the file header held in slot 0 of the reserved page 0 of a paged file."""

    @staticmethod
    def initialize(page: Page, header: FileHeader) -> None:
        """Turn a fresh page into the header page of its file, discarding whatever it held."""
        if page.page_size != header.page_size:
            raise GrafxCorruptionDetected(
                f"A header page of {page.page_size} bytes cannot describe a file whose pages are "
                f"{header.page_size} bytes.",
                field="page_size",
                value=page.page_size,
                page=page.page_index,
            )
        page.clear()
        page.page_type = int(PageType.META)
        page.insert_slot(header.encode())

    @staticmethod
    def read(page: Page) -> FileHeader:
        """Return the file header stored on this page."""
        if page.page_type != int(PageType.META):
            raise GrafxCorruptionDetected(
                f"Page {page.page_index} is of type {page.page_type} and is not a header page.",
                field="page_type",
                value=page.page_type,
                page=page.page_index,
            )
        if page.slot_count <= HEADER_SLOT:
            raise GrafxCorruptionDetected(
                f"Header page {page.page_index} carries no file header record.",
                field="slot_count",
                value=page.slot_count,
                page=page.page_index,
            )
        return FileHeader.decode(page.read_slot(HEADER_SLOT))

    @staticmethod
    def write(page: Page, header: FileHeader) -> None:
        """Replace the file header stored on this page, keeping every other slot."""
        if page.slot_count <= HEADER_SLOT:
            FileHeaderPage.initialize(page, header)
            return
        page.update_slot(HEADER_SLOT, header.encode())
