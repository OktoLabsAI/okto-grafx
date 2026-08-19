"""The page layer of Okto Grafx: layout, slotted page, checksum and overflow chains.

This package owns the on-disk shape of every paged file (CONTRACT.md section 6.3) and nothing
else: it knows how a page is laid out and how to prove that an image of one is self-consistent,
but it never reads or writes a byte of any device. ``Page`` is re-exported here because the
codec port forward-references it under that name (amendment A4).
"""

from __future__ import annotations

from okto_grafx.domain.page.checksum import (
    CRC32C_INITIAL,
    CRC32C_POLYNOMIAL,
    CRC32C_POLYNOMIAL_REFLECTED,
    CRC32C_TABLE_SIZE,
    crc32c,
    crc32c_table,
)
from okto_grafx.domain.page.errors import PageFullError
from okto_grafx.domain.page.file_header import (
    FILE_HEADER_FORMAT_VERSION,
    FILE_HEADER_MAGIC,
    FILE_HEADER_SIZE,
    HEADER_PAGE_INDEX,
    HEADER_SLOT,
    FileHeader,
    FileHeaderPage,
    FileKind,
)
from okto_grafx.domain.page.layout import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_U16,
    MAX_U32,
    MAX_U64,
    MIN_PAGE_SIZE,
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    PageHeader,
    PageType,
    decode_slot_entry,
    encode_slot_entry,
    validate_page_size,
)
from okto_grafx.domain.page.overflow import chunk_capacity, join_chunks, split_payload
from okto_grafx.domain.page.slotted import FREE_SLOT, Page

__all__ = [
    "CRC32C_INITIAL",
    "CRC32C_POLYNOMIAL",
    "CRC32C_POLYNOMIAL_REFLECTED",
    "CRC32C_TABLE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "FILE_HEADER_FORMAT_VERSION",
    "FILE_HEADER_MAGIC",
    "FILE_HEADER_SIZE",
    "FREE_SLOT",
    "HEADER_PAGE_INDEX",
    "HEADER_SLOT",
    "MAX_PAGE_SIZE",
    "MAX_U16",
    "MAX_U32",
    "MAX_U64",
    "MIN_PAGE_SIZE",
    "PAGE_HEADER_SIZE",
    "SLOT_ENTRY_SIZE",
    "FileHeader",
    "FileHeaderPage",
    "FileKind",
    "Page",
    "PageFullError",
    "PageHeader",
    "PageType",
    "chunk_capacity",
    "crc32c",
    "crc32c_table",
    "decode_slot_entry",
    "encode_slot_entry",
    "join_chunks",
    "split_payload",
    "validate_page_size",
]
