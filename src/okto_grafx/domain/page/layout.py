"""The 32-byte page header and the constants of the slotted layout (CONTRACT.md section 6.3).

Every paged file of a database is a sequence of pages of the same size, and every page starts
with the same header at the same offsets, little-endian:

    0  u32 checksum    CRC-32C over bytes[4:page_size]
    4  u16 page_type   0 free, 1 meta, 2 heap, 3 catalog, 4 index_hash, 5 index_hnsw, 6 overflow
    6  u16 flags
    8  u64 page_lsn    LSN of the last WAL record applied to this page, for redo idempotence
    16 u32 seq         even is stable, odd is being written, which is the torn-read helper
    20 u16 slot_count
    22 u16 free_start
    24 u16 free_end
    26 u16 reserved
    28 u32 next_page   NO_PAGE means the chain ends here

The slot directory grows down from the end of the page and the payloads grow up from the header,
so free_end minus free_start is the space that is still contiguous. Both counters are 16-bit,
which is what bounds the page size from above.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_LSN, NO_PAGE, PageIndex

__all__ = [
    "PAGE_HEADER_SIZE",
    "SLOT_ENTRY_SIZE",
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "CHECKSUM_OFFSET",
    "CHECKSUM_SIZE",
    "PAGE_TYPE_OFFSET",
    "FLAGS_OFFSET",
    "PAGE_LSN_OFFSET",
    "SEQ_OFFSET",
    "SLOT_COUNT_OFFSET",
    "FREE_START_OFFSET",
    "FREE_END_OFFSET",
    "RESERVED_OFFSET",
    "NEXT_PAGE_OFFSET",
    "MAX_U16",
    "MAX_U32",
    "MAX_U64",
    "PageType",
    "PageHeader",
    "validate_page_size",
    "is_unwritten_image",
    "encode_slot_entry",
    "decode_slot_entry",
]

PAGE_HEADER_SIZE: int = 32
"""Bytes reserved at the start of every page for the header."""

SLOT_ENTRY_SIZE: int = 4
"""Bytes of one slot directory entry: a 16-bit offset followed by a 16-bit length."""

MIN_PAGE_SIZE: int = 512
"""Smallest page the layout accepts (amendment A20). DatabaseConfig imports this constant, so
the engine and the composition root cannot disagree about which sizes exist."""

MAX_PAGE_SIZE: int = 32768
"""Largest page the layout accepts, because free_start and free_end are 16-bit counters."""

DEFAULT_PAGE_SIZE: int = 8192
"""The page size of a database that does not choose one, matching DatabaseConfig.page_size."""

CHECKSUM_OFFSET: int = 0
CHECKSUM_SIZE: int = 4
PAGE_TYPE_OFFSET: int = 4
FLAGS_OFFSET: int = 6
PAGE_LSN_OFFSET: int = 8
SEQ_OFFSET: int = 16
SLOT_COUNT_OFFSET: int = 20
FREE_START_OFFSET: int = 22
FREE_END_OFFSET: int = 24
RESERVED_OFFSET: int = 26
NEXT_PAGE_OFFSET: int = 28

MAX_U16: int = 0xFFFF
MAX_U32: int = 0xFFFFFFFF
MAX_U64: int = 0xFFFFFFFFFFFFFFFF

_HEADER_STRUCT: struct.Struct = struct.Struct("<IHHQIHHHHI")
_SLOT_STRUCT: struct.Struct = struct.Struct("<HH")


class PageType(IntEnum):
    """The kind of content a page carries, as stored in the page_type header field."""

    FREE = 0
    META = 1
    HEAP = 2
    CATALOG = 3
    INDEX_HASH = 4
    INDEX_HNSW = 5
    OVERFLOW = 6


def is_unwritten_image(raw: bytes, page_size: int) -> bool:
    """Return True when these bytes are an allocated page that no write has ever reached.

    The length is part of the question, not a detail: a short buffer of zeros is a damaged read,
    not an untouched page, and the two must not be confused.

    ``StorageDevice.allocate`` grows a file with zero-filled pages, and the port has no
    positional write primitive, so the only way a page image can be nothing but zeros is that
    nobody has written it yet. That state is reachable in ordinary operation: a crash between the
    PAGE_ALLOC record and the WRITE_PAGE record that was to follow it leaves exactly this.

    A written page can never look like this. Its first four bytes hold the CRC-32C of everything
    after them, and for those to be zero the rest would have to checksum to zero while also being
    zero, which requires ``free_end == 0``; a valid header needs ``free_end == page_size -
    slot_count * 4`` with ``32 <= free_start <= free_end``, and no page size in the accepted range
    satisfies both. The two states are therefore disjoint, which is what makes reading zeros as
    "free" safe rather than a way of hiding damage.
    """
    return len(raw) == page_size > 0 and not any(raw)


def validate_page_size(page_size: int) -> int:
    """Return the page size after checking it against the bounds the layout can encode.

    The size must be a power of two between MIN_PAGE_SIZE and MAX_PAGE_SIZE. The upper bound is
    not a taste: free_start and free_end are 16-bit fields, so a larger page could not address
    its own tail.
    """
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise GrafxConfigurationError(
            f"Page size must be an integer; got {type(page_size).__name__}.",
            field="page_size",
            value=repr(page_size),
        )
    if not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE:
        raise GrafxConfigurationError(
            f"Page size must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}; got {page_size}.",
            field="page_size",
            value=page_size,
        )
    if page_size & (page_size - 1) != 0:
        raise GrafxConfigurationError(
            f"Page size must be a power of two; got {page_size}.",
            field="page_size",
            value=page_size,
        )
    return page_size


def _require_unsigned(field: str, value: int, maximum: int) -> int:
    """Return the value after checking that it is an unsigned integer within the field width."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxCorruptionDetected(
            f"Page header field {field!r} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= maximum:
        raise GrafxCorruptionDetected(
            f"Page header field {field!r} is outside its width: {value} exceeds {maximum}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class PageHeader:
    """The 32 bytes every page starts with, as a value that can be built and compared.

    The header is a description of the page, never its owner: the slotted page keeps the real
    state and produces a header on demand, so the two can never disagree about slot_count or
    about where the free space starts.
    """

    page_type: int = int(PageType.FREE)
    flags: int = 0
    page_lsn: int = NO_LSN
    seq: int = 0
    slot_count: int = 0
    free_start: int = PAGE_HEADER_SIZE
    free_end: int = 0
    reserved: int = 0
    next_page: PageIndex = NO_PAGE
    checksum: int = 0

    def encode(self) -> bytes:
        """Return the 32 header bytes, with the checksum field exactly as it is held here."""
        return _HEADER_STRUCT.pack(
            _require_unsigned("checksum", self.checksum, MAX_U32),
            _require_unsigned("page_type", self.page_type, MAX_U16),
            _require_unsigned("flags", self.flags, MAX_U16),
            _require_unsigned("page_lsn", self.page_lsn, MAX_U64),
            _require_unsigned("seq", self.seq, MAX_U32),
            _require_unsigned("slot_count", self.slot_count, MAX_U16),
            _require_unsigned("free_start", self.free_start, MAX_U16),
            _require_unsigned("free_end", self.free_end, MAX_U16),
            _require_unsigned("reserved", self.reserved, MAX_U16),
            _require_unsigned("next_page", self.next_page, MAX_U32),
        )

    @classmethod
    def decode(cls, raw: bytes) -> PageHeader:
        """Parse the first 32 bytes of a page image into a header value."""
        if len(raw) < PAGE_HEADER_SIZE:
            raise GrafxCorruptionDetected(
                f"A page header needs {PAGE_HEADER_SIZE} bytes; got {len(raw)}.",
                field="page_header",
                value=len(raw),
            )
        (
            checksum,
            page_type,
            flags,
            page_lsn,
            seq,
            slot_count,
            free_start,
            free_end,
            reserved,
            next_page,
        ) = _HEADER_STRUCT.unpack_from(raw, 0)
        return cls(
            page_type=page_type,
            flags=flags,
            page_lsn=page_lsn,
            seq=seq,
            slot_count=slot_count,
            free_start=free_start,
            free_end=free_end,
            reserved=reserved,
            next_page=next_page,
            checksum=checksum,
        )


def encode_slot_entry(offset: int, length: int) -> bytes:
    """Return the four bytes of one slot directory entry."""
    return _SLOT_STRUCT.pack(
        _require_unsigned("slot_offset", offset, MAX_U16),
        _require_unsigned("slot_length", length, MAX_U16),
    )


def decode_slot_entry(raw: bytes, position: int) -> tuple[int, int]:
    """Return the offset and length of the slot directory entry at the given byte position.

    The buffer is checked before it is unpacked. This function is public, so a caller can reach
    it with any bytes at all, and struct answers a short buffer with a raw struct.error that
    carries no code, no retry flag and no location.
    """
    if position < 0 or position + SLOT_ENTRY_SIZE > len(raw):
        raise GrafxCorruptionDetected(
            f"A slot directory entry needs {SLOT_ENTRY_SIZE} bytes at offset {position}, but "
            f"the buffer holds {len(raw)}.",
            field="slot_entry",
            offset=position,
            needed=SLOT_ENTRY_SIZE,
            available=max(len(raw) - max(position, 0), 0),
        )
    offset, length = _SLOT_STRUCT.unpack_from(raw, position)
    return int(offset), int(length)
