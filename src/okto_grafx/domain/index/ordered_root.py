"""Independent root descriptors for the copy-on-write ordered index."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, NO_PAGE, PROVISIONAL_CSN, Lsn, PageIndex
from okto_grafx.domain.index.definition import DEFINITION_DIGEST_SIZE
from okto_grafx.domain.page import DEFAULT_PAGE_SIZE, Page, PageType
from okto_grafx.domain.page.checksum import crc32c

__all__ = [
    "FIRST_ORDERED_TREE_PAGE",
    "ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION",
    "ORDERED_ROOT_DESCRIPTOR_MAGIC",
    "ORDERED_ROOT_DESCRIPTOR_SIZE",
    "ORDERED_ROOT_PAGE_A",
    "ORDERED_ROOT_PAGE_B",
    "OrderedRootDescriptor",
    "OrderedRootSelection",
    "decode_ordered_root_page",
    "make_ordered_root_page",
    "select_ordered_root",
]

ORDERED_ROOT_DESCRIPTOR_MAGIC: bytes = b"GRFXORDR"
ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION: int = 1
ORDERED_ROOT_PAGE_A: PageIndex = 1
ORDERED_ROOT_PAGE_B: PageIndex = 2
FIRST_ORDERED_TREE_PAGE: PageIndex = 3

_ROOT_BODY = struct.Struct("<8sHHQQIHHQQQ16s")
_CHECKSUM = struct.Struct("<I")
ORDERED_ROOT_DESCRIPTOR_SIZE: int = _ROOT_BODY.size + _CHECKSUM.size
_MAX_U16 = 0xFFFF
_MAX_U64 = 0xFFFFFFFFFFFFFFFF


def _unsigned(field: str, value: object, maximum: int) -> int:
    """Return one exact unsigned integer or refuse a caller-built descriptor."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise GrafxIndexError(
            f"Ordered root field {field!r} must be an unsigned integer up to {maximum}; "
            f"got {value!r}.",
            field=field,
            value=repr(value),
        )
    return value


@dataclass(frozen=True, slots=True)
class OrderedRootDescriptor:
    """One complete publication point for an immutable ordered tree generation.

    Two distinct pages carry alternating instances. The generation and
    ``applied_through_lsn`` live in this same checksummed value so recovery can never select a
    root while using a watermark from another publication.
    """

    artifact_nonce: int
    generation: int
    root_page: PageIndex
    height: int
    entry_count: int
    applied_through_lsn: Lsn = NO_LSN
    reconciled_through_lsn: Lsn = NO_LSN
    definition_digest: bytes = bytes(DEFINITION_DIGEST_SIZE)
    format_version: int = ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION
    flags: int = 0

    def __post_init__(self) -> None:
        """Validate the complete root identity and empty/non-empty shape."""

        _unsigned("format_version", self.format_version, _MAX_U16)
        _unsigned("flags", self.flags, _MAX_U16)
        _unsigned("artifact_nonce", self.artifact_nonce, _MAX_U64)
        _unsigned("generation", self.generation, _MAX_U64)
        _unsigned("root_page", self.root_page, NO_PAGE)
        _unsigned("height", self.height, _MAX_U16)
        _unsigned("entry_count", self.entry_count, _MAX_U64)
        _unsigned("applied_through_lsn", self.applied_through_lsn, _MAX_U64)
        _unsigned("reconciled_through_lsn", self.reconciled_through_lsn, _MAX_U64)
        if self.format_version > ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                "This build cannot encode a future ordered-root descriptor format.",
                field="format_version",
                value=self.format_version,
                supported=ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION,
            )
        if self.format_version == 0:
            raise GrafxIndexError(
                "An ordered root descriptor cannot use format version zero.",
                field="format_version",
                value=0,
            )
        if self.artifact_nonce == 0:
            raise GrafxIndexError(
                "An ordered root belongs to one non-zero physical artifact nonce.",
                field="artifact_nonce",
                value=0,
            )
        if self.generation == 0:
            raise GrafxIndexError(
                "An ordered root generation starts at one.",
                field="generation",
                value=0,
            )
        if self.flags != 0:
            raise GrafxIndexError(
                "Ordered root format 1 defines no flag bits.",
                field="flags",
                value=self.flags,
            )
        if (
            self.applied_through_lsn == PROVISIONAL_CSN
            or self.reconciled_through_lsn == PROVISIONAL_CSN
        ):
            field = (
                "applied_through_lsn"
                if self.applied_through_lsn == PROVISIONAL_CSN
                else "reconciled_through_lsn"
            )
            raise GrafxIndexError(
                "An ordered root cannot carry the provisional heap stamp.",
                field=field,
                value=PROVISIONAL_CSN,
            )
        if self.reconciled_through_lsn > self.applied_through_lsn:
            raise GrafxIndexError(
                "An ordered root cannot be reconciled beyond the changes it has applied.",
                field="reconciled_through_lsn",
                value=self.reconciled_through_lsn,
                applied_through_lsn=self.applied_through_lsn,
            )
        digest = self.definition_digest
        if not isinstance(digest, (bytes, bytearray, memoryview)):
            raise GrafxIndexError(
                "An ordered root definition digest must be bytes.",
                field="definition_digest",
                value=type(digest).__name__,
            )
        if not isinstance(digest, bytes):
            digest = bytes(digest)
            object.__setattr__(self, "definition_digest", digest)
        if len(digest) != DEFINITION_DIGEST_SIZE:
            raise GrafxIndexError(
                f"An ordered root definition digest is {DEFINITION_DIGEST_SIZE} bytes; got "
                f"{len(digest)}.",
                field="definition_digest",
                value=len(digest),
            )
        empty = self.root_page == NO_PAGE
        if empty != (self.height == 0) or empty != (self.entry_count == 0):
            raise GrafxIndexError(
                "An empty ordered root has NO_PAGE, height zero and entry_count zero; a "
                "non-empty root has none of those empty markers.",
                field="root_page",
                value=self.root_page,
                height=self.height,
                entry_count=self.entry_count,
            )
        if not empty and self.root_page < FIRST_ORDERED_TREE_PAGE:
            raise GrafxIndexError(
                "An ordered tree root cannot point at the file header or either root slot.",
                field="root_page",
                value=self.root_page,
            )

    def encode(self) -> bytes:
        """Return the self-checksummed root descriptor bytes."""

        body = _ROOT_BODY.pack(
            ORDERED_ROOT_DESCRIPTOR_MAGIC,
            self.format_version,
            self.flags,
            self.artifact_nonce,
            self.generation,
            self.root_page,
            self.height,
            0,
            self.entry_count,
            self.applied_through_lsn,
            self.reconciled_through_lsn,
            self.definition_digest,
        )
        return body + _CHECKSUM.pack(crc32c(body))

    @classmethod
    def decode(cls, raw: bytes) -> OrderedRootDescriptor:
        """Decode one complete descriptor, classifying future format separately from damage."""

        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise GrafxCorruptionDetected(
                "An ordered root descriptor must be bytes.",
                field="ordered_root",
                value=type(raw).__name__,
            )
        image = bytes(raw)
        if len(image) != ORDERED_ROOT_DESCRIPTOR_SIZE:
            raise GrafxCorruptionDetected(
                f"An ordered root descriptor occupies {ORDERED_ROOT_DESCRIPTOR_SIZE} bytes; "
                f"got {len(image)}.",
                field="ordered_root",
                value=len(image),
            )
        body = image[: _ROOT_BODY.size]
        stored_checksum = _CHECKSUM.unpack_from(image, _ROOT_BODY.size)[0]
        computed_checksum = crc32c(body)
        if stored_checksum != computed_checksum:
            raise GrafxCorruptionDetected(
                "An ordered root descriptor failed its payload checksum.",
                field="checksum",
                stored_checksum=stored_checksum,
                computed_checksum=computed_checksum,
            )
        (
            magic,
            format_version,
            flags,
            artifact_nonce,
            generation,
            root_page,
            height,
            reserved,
            entry_count,
            applied_through_lsn,
            reconciled_through_lsn,
            definition_digest,
        ) = _ROOT_BODY.unpack(body)
        if magic != ORDERED_ROOT_DESCRIPTOR_MAGIC:
            raise GrafxCorruptionDetected(
                "These bytes do not carry the ordered-root magic.",
                field="magic",
                value=repr(magic),
            )
        if format_version > ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                "This build cannot read the ordered-root descriptor format.",
                field="format_version",
                value=format_version,
                supported=ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION,
            )
        if format_version == 0:
            raise GrafxCorruptionDetected(
                "An ordered root descriptor declares format version zero.",
                field="format_version",
                value=0,
            )
        if reserved != 0:
            raise GrafxCorruptionDetected(
                "An ordered root descriptor has a non-zero reserved word.",
                field="reserved",
                value=reserved,
            )
        try:
            return cls(
                artifact_nonce=artifact_nonce,
                generation=generation,
                root_page=root_page,
                height=height,
                entry_count=entry_count,
                applied_through_lsn=applied_through_lsn,
                reconciled_through_lsn=reconciled_through_lsn,
                definition_digest=definition_digest,
                format_version=format_version,
                flags=flags,
            )
        except (GrafxIndexError, GrafxSchemaVersionMismatch) as failure:
            raise GrafxCorruptionDetected(
                f"A stored ordered root is invalid: {failure.message}",
                field=str(failure.details.get("field", "ordered_root")),
                value=failure.details.get("value"),
            ) from failure


@dataclass(frozen=True, slots=True)
class OrderedRootSelection:
    """The newest valid root and the older page its successor must replace."""

    descriptor: OrderedRootDescriptor
    page_index: PageIndex
    publication_page: PageIndex
    damaged_pages: tuple[PageIndex, ...] = ()


def make_ordered_root_page(
    descriptor: OrderedRootDescriptor,
    page_index: PageIndex,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page:
    """Build one complete independent root page for a durable publication."""

    if page_index not in (ORDERED_ROOT_PAGE_A, ORDERED_ROOT_PAGE_B):
        raise GrafxIndexError(
            "An ordered root descriptor belongs only on physical page 1 or 2.",
            field="page_index",
            value=page_index,
        )
    if not isinstance(descriptor, OrderedRootDescriptor):
        raise GrafxIndexError(
            "An ordered root page requires an OrderedRootDescriptor.",
            field="descriptor",
            value=type(descriptor).__name__,
        )
    page = Page(
        int(PageType.INDEX_ORDERED_ROOT),
        page_size=page_size,
        page_index=page_index,
        page_lsn=descriptor.applied_through_lsn,
    )
    page.insert_slot(descriptor.encode())
    return page


def decode_ordered_root_page(page: Page) -> OrderedRootDescriptor:
    """Validate and decode one of the two independent root pages."""

    if not isinstance(page, Page):
        raise GrafxCorruptionDetected(
            "An ordered root page must be a Page value.",
            field="page",
            value=type(page).__name__,
        )
    if page.page_index not in (ORDERED_ROOT_PAGE_A, ORDERED_ROOT_PAGE_B):
        raise GrafxCorruptionDetected(
            "An ordered root was read from a physical page other than 1 or 2.",
            field="page_index",
            value=page.page_index,
        )
    if page.page_type != int(PageType.INDEX_ORDERED_ROOT):
        raise GrafxCorruptionDetected(
            f"Ordered root page {page.page_index} has the wrong page type.",
            field="page_type",
            value=page.page_type,
            page=page.page_index,
        )
    header = page.header()
    if page.flags != 0 or page.next_page != NO_PAGE or header.reserved != 0:
        raise GrafxCorruptionDetected(
            f"Ordered root page {page.page_index} carries unsupported header metadata.",
            field="page_header",
            page=page.page_index,
            flags=page.flags,
            next_page=page.next_page,
            reserved=header.reserved,
        )
    if page.seq & 1:
        raise GrafxCorruptionDetected(
            f"Ordered root page {page.page_index} carries an in-progress sequence.",
            field="seq",
            value=page.seq,
            page=page.page_index,
        )
    if page.slot_count != 1:
        raise GrafxCorruptionDetected(
            f"Ordered root page {page.page_index} must carry exactly one descriptor.",
            field="slot_count",
            value=page.slot_count,
            page=page.page_index,
        )
    descriptor = OrderedRootDescriptor.decode(page.read_slot(0))
    if page.page_lsn != descriptor.applied_through_lsn:
        raise GrafxCorruptionDetected(
            "An ordered root page LSN disagrees with the descriptor watermark.",
            field="page_lsn",
            value=page.page_lsn,
            applied_through_lsn=descriptor.applied_through_lsn,
            page=page.page_index,
        )
    return descriptor


def select_ordered_root(
    page_a: Page | None,
    page_b: Page | None,
) -> OrderedRootSelection:
    """Select the newest valid descriptor and name the older publication target."""

    candidates = (
        (ORDERED_ROOT_PAGE_A, page_a),
        (ORDERED_ROOT_PAGE_B, page_b),
    )
    valid: list[tuple[PageIndex, OrderedRootDescriptor]] = []
    damaged: list[PageIndex] = []
    for expected_page, page in candidates:
        if page is None:
            damaged.append(expected_page)
            continue
        if page.page_index != expected_page:
            raise GrafxCorruptionDetected(
                "Ordered root page arguments do not match their physical slots.",
                field="page_index",
                value=page.page_index,
                expected=expected_page,
            )
        try:
            valid.append((expected_page, decode_ordered_root_page(page)))
        except GrafxSchemaVersionMismatch:
            # A future descriptor may be the newest authority. Falling back would let this build
            # overwrite a format it cannot interpret.
            raise
        except GrafxCorruptionDetected:
            damaged.append(expected_page)
    if not valid:
        raise GrafxCorruptionDetected(
            "Both independent ordered-root descriptors are missing or invalid.",
            field="ordered_root",
            damaged_pages=tuple(damaged),
        )
    if len(valid) == 1:
        page_index, descriptor = valid[0]
        publication_page = (
            ORDERED_ROOT_PAGE_B
            if page_index == ORDERED_ROOT_PAGE_A
            else ORDERED_ROOT_PAGE_A
        )
        return OrderedRootSelection(
            descriptor,
            page_index,
            publication_page,
            tuple(damaged),
        )

    first_page, first = valid[0]
    second_page, second = valid[1]
    if first.artifact_nonce != second.artifact_nonce:
        raise GrafxCorruptionDetected(
            "The two ordered roots claim different physical artifact nonces.",
            field="artifact_nonce",
            first=first.artifact_nonce,
            second=second.artifact_nonce,
        )
    if first.definition_digest != second.definition_digest:
        raise GrafxCorruptionDetected(
            "The two ordered roots claim different index definitions.",
            field="definition_digest",
        )
    if first.generation == second.generation:
        if first != second:
            raise GrafxCorruptionDetected(
                "Equal ordered-root generations disagree on their publication state.",
                field="generation",
                value=first.generation,
            )
        return OrderedRootSelection(first, first_page, second_page)
    if abs(first.generation - second.generation) != 1:
        raise GrafxCorruptionDetected(
            "Valid ordered-root generations must be identical or adjacent.",
            field="generation",
            first=first.generation,
            second=second.generation,
        )
    if first.generation > second.generation:
        return OrderedRootSelection(first, first_page, second_page)
    return OrderedRootSelection(second, second_page, first_page)
