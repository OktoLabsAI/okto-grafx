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
import zlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import Lsn, PageIndex
from okto_grafx.domain.index.definition import (
    INDEX_DIRECTORY,
    INDEX_FILE_SUFFIX,
    require_index_name,
)
from okto_grafx.domain.page.layout import MAX_PAGE_SIZE
from okto_grafx.domain.wal.record import (
    WAL_FORMAT_VERSION,
    WAL_LEGACY_FORMAT_VERSION,
    WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
    WAL_V2_FLAG_COMMIT_CATALOG_V1,
    WAL_V2_FLAG_SYSTEM_HISTORY_V1,
    WAL_V2_FLAG_REQUIRED,
    WalRecord,
    WalRecordType,
    v2_record_semantics_error,
)

__all__ = [
    "MAX_FILE_NAME_BYTES",
    "WAL_FORMAT_VERSION",
    "EncodedPageWrite",
    "PageWrite",
    "PageWriteLocation",
    "WalRecord",
    "WalRecordLike",
    "WalRecordType",
    "decode_page_write",
    "decode_page_write_location",
    "encode_page_write",
    "encode_page_write_record",
    "is_redoable_page_file",
    "COMMIT_DIRECTORY_FILE",
    "COMMIT_STREAM_FILE",
    "COMMIT_CATALOG_PAGE_FILES",
]

MAX_FILE_NAME_BYTES: int = 0xFFFF
"""Longest file name a page-write payload can carry: the length prefix is 16 bits."""

_NAME_LENGTH: struct.Struct = struct.Struct("<H")
_PAGE_INDEX: struct.Struct = struct.Struct("<I")
_UNCOMPRESSED_LENGTH: struct.Struct = struct.Struct("<I")
_MAX_PAGE_INDEX_FIELD: int = 0xFFFFFFFF
_REDOABLE_PAGE_FILES: frozenset[str] = frozenset({"heap.dat", "catalog.dat"})
_PAGE_IMAGE_ZLIB1_FLAGS: int = WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1
COMMIT_DIRECTORY_FILE: str = "commits.dir"
COMMIT_STREAM_FILE: str = "commits.dat"
COMMIT_CATALOG_PAGE_FILES: frozenset[str] = frozenset({COMMIT_DIRECTORY_FILE, COMMIT_STREAM_FILE})
_COMMIT_CATALOG_FLAGS: int = WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_COMMIT_CATALOG_V1
SYSTEM_HISTORY_FILE: str = "system-history.dat"
_SYSTEM_HISTORY_FLAGS: int = WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_SYSTEM_HISTORY_V1


def is_redoable_page_file(file: object) -> bool:
    """Return whether ``file`` is a canonical paged-data path.

    Storage names are slash-separated logical names on every host. WAL and live commit inputs
    share this predicate so neither path can target a control record, escape through traversal,
    or interpret a backslash differently on Windows and POSIX.
    """
    if not isinstance(file, str) or not file or "\\" in file:
        return False
    parts = file.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if file in _REDOABLE_PAGE_FILES:
        return True
    if len(parts) != 2 or parts[0] != INDEX_DIRECTORY:
        return False
    basename = parts[1]
    if not basename.endswith(INDEX_FILE_SUFFIX):
        return False
    try:
        require_index_name(basename[: -len(INDEX_FILE_SUFFIX)])
    except GrafxIndexError:
        return False
    return True


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
class EncodedPageWrite:
    """A complete WRITE_PAGE payload plus the WAL envelope semantics it requires."""

    payload: bytes
    format_version: int
    flags: int

    @property
    def compressed(self) -> bool:
        """Return whether this encoding uses the v2 zlib page-image grammar."""

        return self.format_version == WAL_FORMAT_VERSION and bool(
            self.flags & WAL_V2_FLAG_PAGE_IMAGE_ZLIB1
        )


def encode_page_write_record(
    file: str,
    page_index: PageIndex,
    image: bytes,
    *,
    compress: bool,
) -> EncodedPageWrite:
    """Encode a page, retaining mandatory journal semantics even without compression.

    This codec does not authorize journal publication or register a replay target.
    Ordinary data pages keep legacy framing unless compression is strictly smaller.
    """

    if not isinstance(compress, bool):
        raise GrafxConfigurationError(
            "Page-write compression selection must be boolean.",
            field="compress",
            value=type(compress).__name__,
        )
    legacy = encode_page_write(file, page_index, image)
    journal = file in COMMIT_CATALOG_PAGE_FILES
    history = file == SYSTEM_HISTORY_FILE
    version = WAL_FORMAT_VERSION if journal or history else WAL_LEGACY_FORMAT_VERSION
    flags = _COMMIT_CATALOG_FLAGS if journal else _SYSTEM_HISTORY_FLAGS if history else 0
    if not compress:
        return EncodedPageWrite(legacy, version, flags)
    raw_image = bytes(image)
    compressed = zlib.compress(raw_image, level=1)
    if len(compressed) + _UNCOMPRESSED_LENGTH.size >= len(raw_image):
        return EncodedPageWrite(legacy, version, flags)
    prefix_length = len(legacy) - len(raw_image)
    payload = b"".join(
        (
            legacy[:prefix_length],
            _UNCOMPRESSED_LENGTH.pack(len(raw_image)),
            compressed,
        )
    )
    return EncodedPageWrite(payload, WAL_FORMAT_VERSION, _PAGE_IMAGE_ZLIB1_FLAGS | flags)


@dataclass(frozen=True, slots=True)
class PageWrite:
    """What a WRITE_PAGE payload says: which page of which file, and what it should hold."""

    file: str
    page_index: PageIndex
    image: bytes


@dataclass(frozen=True, slots=True)
class PageWriteLocation:
    """The cleartext target prefix shared by legacy and compressed page writes."""

    file: str
    page_index: PageIndex


def _decode_page_write_prefix(payload: bytes) -> tuple[bytes, str, PageIndex, int]:
    """Validate and return the common target prefix plus its image offset."""

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
    return raw, file, page_index, minimum


def decode_page_write_location(payload: bytes) -> PageWriteLocation:
    """Decode only the cleartext file/page target without inflating a compressed image."""

    _raw, file, page_index, _image_offset = _decode_page_write_prefix(payload)
    return PageWriteLocation(file=file, page_index=page_index)


def decode_page_write(
    payload: bytes,
    *,
    format_version: int = WAL_LEGACY_FORMAT_VERSION,
    flags: int = 0,
) -> PageWrite:
    """Parse a WRITE_PAGE payload back into the file, the page and the image it carries."""
    raw, file, page_index, minimum = _decode_page_write_prefix(payload)
    if format_version == WAL_LEGACY_FORMAT_VERSION:
        # V1 flags were always opaque and must not acquire retrospective meaning.
        if file in COMMIT_CATALOG_PAGE_FILES or file == SYSTEM_HISTORY_FILE:
            raise GrafxSchemaVersionMismatch(
                "Commit catalog pages require explicit WAL-v2 journal semantics.",
                field="commit_catalog_grammar",
            )
        return PageWrite(file=file, page_index=page_index, image=raw[minimum:])
    if format_version != WAL_FORMAT_VERSION:
        raise GrafxSchemaVersionMismatch(
            f"This build cannot decode WRITE_PAGE format version {format_version!r}.",
            field="format_version",
            value=format_version,
            supported=WAL_FORMAT_VERSION,
        )
    semantic_error = v2_record_semantics_error(WalRecordType.WRITE_PAGE, flags)
    if semantic_error is not None:
        raise GrafxSchemaVersionMismatch(
            semantic_error,
            field="flags",
            value=flags,
        )
    journal = bool(flags & WAL_V2_FLAG_COMMIT_CATALOG_V1)
    history = bool(flags & WAL_V2_FLAG_SYSTEM_HISTORY_V1)
    if history != (file == SYSTEM_HISTORY_FILE):
        raise GrafxSchemaVersionMismatch("System history WAL target and required semantics differ.",
                                         field="system_history_grammar")
    if journal and file not in COMMIT_CATALOG_PAGE_FILES:
        raise GrafxCorruptionDetected(
            "Commit catalog WAL semantics name a non-journal target.",
            field="commit_catalog_target",
        )
    if not journal and file in COMMIT_CATALOG_PAGE_FILES:
        raise GrafxSchemaVersionMismatch(
            "Commit catalog pages require explicit WAL-v2 journal semantics.",
            field="commit_catalog_grammar",
        )
    if not flags & WAL_V2_FLAG_PAGE_IMAGE_ZLIB1:
        return PageWrite(file=file, page_index=page_index, image=raw[minimum:])
    compressed_offset = minimum + _UNCOMPRESSED_LENGTH.size
    if len(raw) < compressed_offset:
        raise GrafxCorruptionDetected(
            "A compressed page-write payload is missing its uncompressed length.",
            field="payload",
            value=len(raw),
        )
    (declared_length,) = _UNCOMPRESSED_LENGTH.unpack_from(raw, minimum)
    if not 0 < declared_length <= MAX_PAGE_SIZE:
        raise GrafxCorruptionDetected(
            f"A compressed page image declares invalid length {declared_length}.",
            field="image_length",
            value=declared_length,
            limit=MAX_PAGE_SIZE,
        )
    compressed = raw[compressed_offset:]
    try:
        inflater = zlib.decompressobj()
        image = inflater.decompress(compressed, declared_length + 1)
    except zlib.error as damaged:
        raise GrafxCorruptionDetected(
            "A compressed page image is not a valid zlib stream.",
            field="compression",
            value="zlib1",
        ) from damaged
    if (
        len(image) != declared_length
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise GrafxCorruptionDetected(
            "A compressed page image did not expand to exactly its declared bounded length.",
            field="image_length",
            value=len(image),
            declared=declared_length,
        )
    return PageWrite(file=file, page_index=page_index, image=image)
