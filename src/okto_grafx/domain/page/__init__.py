"""The page layer of Okto Grafx: layout, slotted page, checksum and overflow chains.

This package owns the on-disk shape of every paged file (CONTRACT.md section 6.3) and nothing
else: it knows how a page is laid out and how to prove that an image of one is self-consistent,
but it never reads or writes a byte of any device. ``Page`` is re-exported here because the
codec port forward-references it under that name (amendment A4).
"""

from __future__ import annotations

from okto_grafx.domain.page.checksum import (
    CRC32C_ACCEPTANCE_CORPUS,
    CRC32C_INITIAL,
    CRC32C_KNOWN_ANSWERS,
    CRC32C_POLYNOMIAL,
    CRC32C_POLYNOMIAL_REFLECTED,
    CRC32C_TABLE_SIZE,
    PURE_IMPLEMENTATION_NAME,
    crc32c,
    crc32c_implementation,
    crc32c_reference,
    crc32c_table,
    install_crc32c,
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
    is_unwritten_image,
    validate_page_size,
)
from okto_grafx.domain.page.overflow import chunk_capacity, join_chunks, split_payload
from okto_grafx.domain.page.slotted import FREE_SLOT, Page

__all__ = [
    "CRC32C_ACCEPTANCE_CORPUS",
    "CRC32C_INITIAL",
    "CRC32C_KNOWN_ANSWERS",
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
    "PURE_IMPLEMENTATION_NAME",
    "PageType",
    "chunk_capacity",
    "crc32c",
    "crc32c_implementation",
    "crc32c_reference",
    "crc32c_table",
    "decode_slot_entry",
    "encode_slot_entry",
    "install_crc32c",
    "is_unwritten_image",
    "join_chunks",
    "split_payload",
    "validate_page_size",
]
