"""Bounded page-image plans and reads for the append-only commit catalog.

NOT an independently writable store. The caller must provide a stable, physically
proved page view; publication must stage these images through both OCC checks and
ordinary full-image WAL. No file targets/capability are registered by this module.
It neither opens files nor acquires locks nor acknowledges a durable outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from struct import Struct
from types import MappingProxyType

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxTransactionBudgetExceeded,
)
from okto_grafx.domain.ids import NO_PAGE, PROVISIONAL_CSN
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.page import FileHeader, FileKind, Page, PageType
from okto_grafx.domain.page.layout import validate_page_size
from okto_grafx.domain.recovery.decision import CommittedReplay, validate_commit_boundaries
from okto_grafx.domain.txn.commit_catalog import (
    MAX_COMMIT_RECORD_BYTES,
    CommitCatalogEntry,
    CommitKind,
    decode_commit_catalog_entry,
)
from okto_grafx.domain.txn.commit_identity import CommitId, assign_commit_time
from okto_grafx.domain.txn.records import (
    COMMIT_DIRECTORY_FILE as COMMIT_DIRECTORY_FILE,
    COMMIT_STREAM_FILE as COMMIT_STREAM_FILE,
    decode_page_write,
    decode_page_write_location,
)
from okto_grafx.domain.wal.record import WAL_V2_FLAG_COMMIT_CATALOG_V1, WalRecordType


_HEAD = Struct("<8sHH16sQQQQq")
_BLOCK = Struct("<8sHH16sQ")
_ITEM = Struct("<QQIIq")
_HEAD_MAGIC = b"GXCMHEAD"
_BLOCK_MAGIC = b"GXCMBLK\0"
_DIRECTORY = 1
_STREAM = 2
_MIN_RECORD_BYTES = 60


def _corrupt(field_name: str) -> GrafxCorruptionDetected:
    return GrafxCorruptionDetected(
        "Invalid commit catalog page or coverage.", component="commit_catalog",
        field=field_name,
    )


def _invalid(field_name: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid commit catalog input.", field=field_name)


@dataclass(frozen=True, slots=True)
class CommitCatalogHead:
    """Decoded coverage, not a physical certificate or a published commit receipt."""

    activation_sequence: int
    entry_count: int
    stream_bytes: int
    last_sequence: int
    last_ordered_micros: int


@dataclass(frozen=True, slots=True)
class CommitCatalogPageImage:
    file: str
    page_index: int
    raw: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommitCatalogPlan:
    """Private, unsealed images. Transaction staging still owns stamps and authority."""

    head: CommitCatalogHead
    images: tuple[CommitCatalogPageImage, ...]


@dataclass(frozen=True, slots=True)
class PreparedCommitCatalogAppend:
    """One commit attempt's detached inputs, not reusable physical authority.

    The transaction coordinator must prepare under its current publication fence,
    stage through normal private provenance and include all locations in the second
    OCC pass. This value ONLY closes the batch-size/terminal-LSN dependency; it does
    not permit reuse after leaving that attempt or grant permission to publish.
    """

    previous_sequence: int
    image_count: int
    _database_uuid: bytes = field(repr=False)
    _page_size: int = field(repr=False)
    _record: bytes = field(repr=False)
    _pages: Mapping[tuple[str, int], bytes] = field(repr=False)

    def bind(self, sequence: int) -> CommitCatalogPlan:
        """Regenerate complete images at the final COMMIT LSN, with zero host I/O.

        Raw page lengths and record cardinality are invariant under retargeting.
        Compression must still follow the existing raw-batch roll decision so its
        compressed-size feedback cannot move the chosen terminal LSN a second time.
        """
        identity = CommitId(self._database_uuid, sequence)
        if sequence <= self.previous_sequence:
            raise _invalid("commit_order")
        captured = decode_commit_catalog_entry(
            self._record, expected_store_uuid=self._database_uuid,
        )
        candidate = CommitCatalogEntry(identity, captured.timing, captured.metadata_bytes, captured.kind)

        def read(file: str, index: int) -> bytes:
            try:
                return self._pages[file, index]
            except KeyError:
                raise _corrupt("prepared_page_coverage") from None

        store = CommitCatalogStore(read, database_uuid=self._database_uuid, page_size=self._page_size)
        if store.read_head().last_sequence != self.previous_sequence:
            raise _corrupt("prepared_baseline")
        plan = store.plan_append(candidate)
        if len(plan.images) != self.image_count:
            raise _corrupt("prepared_cardinality")
        return plan


@dataclass(frozen=True, slots=True)
class _DirectoryItem:
    sequence: int
    offset: int
    size: int
    ordered: int

    def encode(self) -> bytes:
        return _ITEM.pack(self.sequence, self.offset, self.size, 0, self.ordered)


class CommitCatalogStore:
    """Two arithmetic-addressed files; reads are scoped to the caller's stable view.

    Logical bytes/ordinals only grow. A later tail image preserves its old prefix,
    so lookup can filter by an older read_lsn without retaining a copy of every head.
    The surrounding engine must still prove a consistent view before AND after the
    operation. A CRC or this object alone cannot make a concurrent raw read safe.
    """

    def __init__(
        self, read_page: Callable[[str, int], bytes], *, database_uuid: bytes,
        page_size: int,
    ) -> None:
        self._uuid = CommitId(database_uuid, 1).database_uuid
        if type(page_size) is not int:
            raise _invalid("page_size")
        self._page_size = validate_page_size(page_size)
        self._read_page = read_page
        # Common page header, two slot directory entries, fixed block descriptor.
        self._capacity = page_size - 32 - 8 - _BLOCK.size
        self._per_page = self._capacity // _ITEM.size

    def _page(self, file: str, index: int, page_type: PageType) -> Page:
        raw = self._read_page(file, index)
        if type(raw) is not bytes or len(raw) != self._page_size:
            raise _corrupt("page_size")
        page = Page.from_bytes(raw, page_index=index)
        if (
            page.page_type != page_type or page.flags or page.seq % 2
            or page.next_page != NO_PAGE or page.slot_count != 2
            or page.header().reserved
        ):
            raise _corrupt("page_header")
        if page.is_slot_free(0) or page.is_slot_free(1):
            raise _corrupt("freed_slot")
        return page

    def _header_image(self, kind: int, head: CommitCatalogHead) -> CommitCatalogPageImage:
        page = Page(PageType.META, page_size=self._page_size)
        page.insert_slot(FileHeader(FileKind.CATALOG, self._page_size).encode())
        page.insert_slot(_HEAD.pack(
            _HEAD_MAGIC, 1, kind, self._uuid, head.activation_sequence,
            head.entry_count, head.stream_bytes, head.last_sequence,
            head.last_ordered_micros,
        ))
        return CommitCatalogPageImage(self._file(kind), 0, page.to_bytes())

    @staticmethod
    def _file(kind: int) -> str:
        return COMMIT_DIRECTORY_FILE if kind == _DIRECTORY else COMMIT_STREAM_FILE

    def _head(self, kind: int) -> CommitCatalogHead:
        page = self._page(self._file(kind), 0, PageType.META)
        # The generic reserved header is exact: neither a linked schema catalog nor
        # an older/newer generic file-header format is interchangeable with this file.
        header_raw = page.read_slot(0)
        try:
            FileHeader.decode(header_raw)
        except GrafxCorruptionDetected:
            raise _corrupt("file_header") from None
        if header_raw != FileHeader(FileKind.CATALOG, self._page_size).encode():
            raise _corrupt("file_header")
        raw = page.read_slot(1)
        if len(raw) != _HEAD.size:
            raise _corrupt("head_size")
        magic, version, actual_kind, uuid, activation, count, size, last, ordered = _HEAD.unpack(raw)
        self._discriminator(magic, _HEAD_MAGIC, version, actual_kind, kind, uuid)
        head = CommitCatalogHead(activation, count, size, last, ordered)
        self._validate_head(head)
        if kind == _STREAM and (count or size or last != activation or ordered):
            raise _corrupt("stream_header")
        return head

    def _validate_head(self, head: CommitCatalogHead) -> None:
        if not 0 < head.activation_sequence <= head.last_sequence < PROVISIONAL_CSN:
            raise _corrupt("head_sequence")
        if head.entry_count == 0:
            if head.stream_bytes or head.last_sequence != head.activation_sequence or head.last_ordered_micros:
                raise _corrupt("empty_head")
        elif (
            head.last_sequence - head.activation_sequence < head.entry_count
            or not head.entry_count * _MIN_RECORD_BYTES <= head.stream_bytes
            <= head.entry_count * MAX_COMMIT_RECORD_BYTES
        ):
            raise _corrupt("head_coverage")
        if (
            head.entry_count > (NO_PAGE - 1) * self._per_page
            or head.stream_bytes > (NO_PAGE - 1) * self._capacity
        ):
            raise _corrupt("page_address_space")

    def _discriminator(
        self, magic: bytes, expected_magic: bytes, version: int, kind: int,
        expected_kind: int, uuid: bytes,
    ) -> None:
        if magic != expected_magic:
            raise _corrupt("magic")
        if version != 1:
            raise GrafxSchemaVersionMismatch(
                "Unsupported commit catalog page version.", component="commit_catalog",
                field="version", version=version,
            )
        if kind != expected_kind:
            raise _corrupt("file_kind")
        if uuid != self._uuid:
            raise _corrupt("database_uuid")

    def read_head(self) -> CommitCatalogHead:
        """Check both file identities and the declared legacy boundary, not all records."""
        head = self._head(_DIRECTORY)
        stream = self._head(_STREAM)
        if stream.activation_sequence != head.activation_sequence:
            raise _corrupt("activation_sequence")
        return head

    def _block_image(self, kind: int, base: int, payload: bytes) -> CommitCatalogPageImage:
        stride = self._per_page if kind == _DIRECTORY else self._capacity
        page = Page(PageType.CATALOG, page_size=self._page_size)
        page.insert_slot(_BLOCK.pack(_BLOCK_MAGIC, 1, kind, self._uuid, base))
        page.insert_slot(payload)
        return CommitCatalogPageImage(self._file(kind), 1 + base // stride, page.to_bytes())

    def _block(self, kind: int, index: int, head: CommitCatalogHead) -> bytes:
        stride = self._per_page if kind == _DIRECTORY else self._capacity
        extent = head.entry_count if kind == _DIRECTORY else head.stream_bytes
        base = (index - 1) * stride
        if not 1 <= index < NO_PAGE or not 0 <= base < extent:
            raise _corrupt("block_address")
        page = self._page(self._file(kind), index, PageType.CATALOG)
        raw = page.read_slot(0)
        if len(raw) != _BLOCK.size:
            raise _corrupt("block_size")
        magic, version, actual_kind, uuid, actual_base = _BLOCK.unpack(raw)
        self._discriminator(magic, _BLOCK_MAGIC, version, actual_kind, kind, uuid)
        if uuid != self._uuid or actual_base != base:
            raise _corrupt("block_identity")
        payload = page.read_slot(1)
        expected = min(stride, extent - base) * (_ITEM.size if kind == _DIRECTORY else 1)
        if len(payload) != expected:
            raise _corrupt("block_coverage")
        return payload

    def _directory_page(self, index: int, head: CommitCatalogHead) -> tuple[_DirectoryItem, ...]:
        payload = self._block(_DIRECTORY, index, head)
        result: list[_DirectoryItem] = []
        for sequence, offset, size, reserved, ordered in _ITEM.iter_unpack(payload):
            if (
                reserved or not head.activation_sequence < sequence <= head.last_sequence
                or not _MIN_RECORD_BYTES <= size <= MAX_COMMIT_RECORD_BYTES
                or offset + size > head.stream_bytes or ordered > head.last_ordered_micros
            ):
                raise _corrupt("directory_item")
            item = _DirectoryItem(sequence, offset, size, ordered)
            if result:
                self._adjacent(result[-1], item)
            result.append(item)
        if index == 1 and result[0].offset:
            raise _corrupt("stream_start")
        if index * self._per_page >= head.entry_count:
            tail = result[-1]
            if (tail.sequence, tail.offset + tail.size, tail.ordered) != (
                head.last_sequence, head.stream_bytes, head.last_ordered_micros,
            ):
                raise _corrupt("head_tail")
        return tuple(result)

    @staticmethod
    def _adjacent(left: _DirectoryItem, right: _DirectoryItem) -> None:
        if (
            left.sequence >= right.sequence or left.ordered >= right.ordered
            or left.offset + left.size != right.offset
        ):
            raise _corrupt("directory_order")

    def _item(self, ordinal: int, head: CommitCatalogHead) -> _DirectoryItem:
        return self._directory_page(1 + ordinal // self._per_page, head)[ordinal % self._per_page]

    def _record(self, item: _DirectoryItem, head: CommitCatalogHead) -> CommitCatalogEntry:
        offset, remaining = item.offset, item.size
        parts: list[bytes] = []
        while remaining:
            payload = self._block(_STREAM, 1 + offset // self._capacity, head)
            start = offset % self._capacity
            size = min(remaining, len(payload) - start)
            if size <= 0:
                raise _corrupt("record_fragment")
            parts.append(payload[start:start + size])
            offset += size
            remaining -= size
        record = decode_commit_catalog_entry(
            b"".join(parts), expected_store_uuid=self._uuid, expected_sequence=item.sequence,
        )
        if record.timing.ordered_at.micros != item.ordered:
            raise _corrupt("record_time")
        return record

    def plan_initialize(self, *, activation_sequence: int) -> CommitCatalogPlan:
        """Produce empty images ONLY; the activation caller must prove unused targets."""
        CommitId(self._uuid, activation_sequence)
        head = CommitCatalogHead(activation_sequence, 0, 0, activation_sequence, 0)
        return CommitCatalogPlan(head, (
            self._header_image(_DIRECTORY, head), self._header_image(_STREAM, head),
        ))

    def prepare_append(
        self, *, expected_last_sequence: int, observed_at: Timestamp,
        metadata_bytes: bytes | None = None, kind: CommitKind = CommitKind.DATA,
    ) -> PreparedCommitCatalogAppend:
        """Capture one bounded attempt before WAL sizing, with a mandatory control baseline.

        Admission precedes reads. The caller samples observed_at exactly once via
        its Clock port; this module never samples time. The head must cover the
        current durable COMMIT supplied by the coordinator, not merely be valid.
        Reads remain inside that caller's established physical-view/fence protocol.
        The bounded capture is used only to size/rebind this attempt, not to cache
        authority for future transactions or bypass descriptor revalidation.
        """
        CommitId(self._uuid, expected_last_sequence)
        # Validate and detach the entire payload before invoking the page provider.
        admitted = CommitCatalogEntry(
            CommitId(self._uuid, 1), assign_commit_time(observed_at, None), metadata_bytes, kind,
        )
        admitted = decode_commit_catalog_entry(admitted.encode())
        pages: dict[tuple[str, int], bytes] = {}

        def read(file: str, index: int) -> bytes:
            location = (file, index)
            if location not in pages:
                pages[location] = self._read_page(file, index)
            return pages[location]

        captured_store = CommitCatalogStore(read, database_uuid=self._uuid, page_size=self._page_size)
        head = captured_store.read_head()
        if head.last_sequence != expected_last_sequence:
            raise _corrupt("published_coverage")
        timing = assign_commit_time(
            admitted.timing.observed_at,
            Timestamp(head.last_ordered_micros) if head.entry_count else None,
        )
        prototype = CommitCatalogEntry(
            CommitId(self._uuid, head.last_sequence + 1), timing, admitted.metadata_bytes, admitted.kind,
        )
        plan = captured_store.plan_append(prototype)
        return PreparedCommitCatalogAppend(
            head.last_sequence, len(plan.images), self._uuid, self._page_size,
            prototype.encode(), MappingProxyType(dict(pages)),
        )

    def plan_append(self, entry: CommitCatalogEntry) -> CommitCatalogPlan:
        """Plan bounded full images without allocating pages or changing input pages."""
        if type(entry) is not CommitCatalogEntry:
            raise _invalid("entry")
        # Capture/revalidate even host-mutated frozen fields before trusting sizes/IDs.
        raw = entry.encode()
        captured = decode_commit_catalog_entry(raw)
        if captured.identity.database_uuid != self._uuid:
            raise _invalid("database_uuid")
        head = self.read_head()
        sequence = captured.identity.sequence
        ordered = captured.timing.ordered_at.micros
        if sequence <= head.last_sequence or (head.entry_count and ordered <= head.last_ordered_micros):
            raise _invalid("commit_order")
        new_head = CommitCatalogHead(
            head.activation_sequence, head.entry_count + 1, head.stream_bytes + len(raw),
            sequence, ordered,
        )
        required_pages = max(
            (new_head.entry_count + self._per_page - 1) // self._per_page,
            (new_head.stream_bytes + self._capacity - 1) // self._capacity,
        )
        if required_pages >= NO_PAGE:
            raise GrafxTransactionBudgetExceeded(
                "Commit catalog page address space exhausted.",
                field="commit_catalog_pages", limit=NO_PAGE - 1, observed=required_pages,
            )
        self._validate_head(new_head)
        if head.entry_count:
            self._record(self._item(head.entry_count - 1, head), head)
        images: list[CommitCatalogPageImage] = []
        used = head.stream_bytes % self._capacity
        base = head.stream_bytes - used
        prefix = self._block(_STREAM, 1 + base // self._capacity, head) if used else b""
        pending = prefix + raw
        for start in range(0, len(pending), self._capacity):
            images.append(self._block_image(_STREAM, base + start, pending[start:start + self._capacity]))
        ordinal_base = head.entry_count - head.entry_count % self._per_page
        directory_prefix = (
            self._block(_DIRECTORY, 1 + ordinal_base // self._per_page, head)
            if head.entry_count % self._per_page else b""
        )
        item = _DirectoryItem(sequence, head.stream_bytes, len(raw), ordered)
        images.append(self._block_image(_DIRECTORY, ordinal_base, directory_prefix + item.encode()))
        images.append(self._header_image(_DIRECTORY, new_head))
        return CommitCatalogPlan(new_head, tuple(images))

    def _admit_append_image_count(self, count: int) -> None:
        maximum = (MAX_COMMIT_RECORD_BYTES + self._capacity - 1) // self._capacity + 3
        if count > maximum:
            raise GrafxTransactionBudgetExceeded(
                "Commit catalog append image count exceeds its bounded format.",
                field="append_images", limit=maximum, observed=count,
            )

    def _capture_append_images(
        self, images: tuple[CommitCatalogPageImage, ...], sequence: int,
    ) -> tuple[dict[tuple[str, int], bytes], dict[tuple[str, int], int]]:
        """Bound and detach after-images before any provider can run callbacks."""
        if type(images) is not tuple:
            raise _invalid("append_images")
        self._admit_append_image_count(len(images))
        if len(images) < 3:
            raise _corrupt("append_images")
        incoming: dict[tuple[str, int], bytes] = {}
        sequences: dict[tuple[str, int], int] = {}
        for image in images:
            if (
                type(image) is not CommitCatalogPageImage or type(image.file) is not str
                or image.file not in {COMMIT_DIRECTORY_FILE, COMMIT_STREAM_FILE}
                or type(image.page_index) is not int or not 0 <= image.page_index < NO_PAGE
                or type(image.raw) is not bytes or len(image.raw) != self._page_size
            ):
                raise _corrupt("append_image")
            location = (image.file, image.page_index)
            if location in incoming:
                raise _corrupt("append_duplicate")
            page = Page.from_bytes(image.raw, page_index=image.page_index)
            if page.page_lsn != sequence or page.seq % 2:
                raise _corrupt("append_page_stamp")
            incoming[location] = image.raw
            sequences[location] = page.seq
        return incoming, sequences

    def validate_append_images(
        self, images: tuple[CommitCatalogPageImage, ...], *,
        previous_sequence: int, sequence: int, activation_sequence: int,
    ) -> CommitCatalogEntry:
        """Validate one complete stamped append against a proved predecessor view.

        The provider MUST represent the predecessor, not a partially applied or
        newer head. Recovery must establish that view before using this validator;
        this method neither reconstructs it nor grants physical authority. No
        image is applied. Caller-owned fields are captured before provider calls.
        Work is bounded by the old/new last record sizes, never retained history.
        """
        for value in (previous_sequence, sequence, activation_sequence):
            CommitId(self._uuid, value)
        if not activation_sequence <= previous_sequence < sequence:
            raise _invalid("append_sequence")
        incoming, sequences = self._capture_append_images(images, sequence)

        predecessor_pages: dict[tuple[str, int], bytes] = {}

        def before_read(file: str, index: int) -> bytes:
            key = (file, index)
            if key not in predecessor_pages:
                predecessor_pages[key] = self._read_page(file, index)
            return predecessor_pages[key]

        before = CommitCatalogStore(before_read, database_uuid=self._uuid, page_size=self._page_size)
        previous = before.read_head()
        if previous.last_sequence != previous_sequence or previous.activation_sequence != activation_sequence:
            raise _corrupt("published_coverage")

        def after_read(file: str, index: int) -> bytes:
            raw = incoming.get((file, index))
            return before_read(file, index) if raw is None else raw

        after = CommitCatalogStore(after_read, database_uuid=self._uuid, page_size=self._page_size)
        head = after.read_head()
        if (
            head.activation_sequence != activation_sequence or head.last_sequence != sequence
            or head.entry_count != previous.entry_count + 1
            or not _MIN_RECORD_BYTES <= head.stream_bytes - previous.stream_bytes <= MAX_COMMIT_RECORD_BYTES
        ):
            raise _corrupt("append_coverage")
        locations = {
            (COMMIT_DIRECTORY_FILE, 0),
            (COMMIT_DIRECTORY_FILE, 1 + previous.entry_count // self._per_page),
        }
        locations.update(
            (COMMIT_STREAM_FILE, 1 + index)
            for index in range(previous.stream_bytes // self._capacity, (head.stream_bytes - 1) // self._capacity + 1)
        )
        if set(incoming) != locations:
            raise _corrupt("append_locations")
        item = after._item(head.entry_count - 1, head)
        if item.offset != previous.stream_bytes or item.sequence != sequence:
            raise _corrupt("append_offset")
        record = after._record(item, head)
        if previous.entry_count and record.timing.ordered_at.micros <= previous.last_ordered_micros:
            raise _corrupt("append_time")
        expected_time = assign_commit_time(
            record.timing.observed_at,
            Timestamp(previous.last_ordered_micros) if previous.entry_count else None,
        )
        if record.timing != expected_time:
            raise _corrupt("append_time")
        expected = before.plan_append(record)
        if expected.head != head:
            raise _corrupt("append_coverage")
        for image in expected.images:
            location = (image.file, image.page_index)
            canonical = Page.from_bytes(image.raw, page_index=image.page_index)
            canonical.page_lsn = sequence
            canonical.seq = sequences[location]
            if canonical.to_bytes() != incoming[location]:
                raise _corrupt("append_prefix")
        return record

    def validate_redo_images(
        self, images: tuple[CommitCatalogPageImage, ...], *,
        previous_sequence: int, sequence: int, activation_sequence: int,
    ) -> CommitCatalogEntry:
        """Validate complete durable after-images despite a partially applied live tail.

        The caller must prove WAL COMMIT lineage/barrier, activation and physical
        authority. Covered targets are NEVER read from the device. Only immutable
        full prefix blocks and the existing stream header may come from storage.
        First append must carry the stream header itself; absence is not bootstrap.
        Reconstructed prefixes inherit WAL authority, not an independent before-
        image proof. Live publication must still use validate_append_images.
        This returns a checked record value only and does not apply or acknowledge.
        """
        for value in (previous_sequence, sequence, activation_sequence):
            CommitId(self._uuid, value)
        if not activation_sequence <= previous_sequence < sequence:
            raise _invalid("append_sequence")
        incoming, _sequences = self._capture_append_images(images, sequence)
        directory_head = (COMMIT_DIRECTORY_FILE, 0)
        stream_head = (COMMIT_STREAM_FILE, 0)
        if directory_head not in incoming:
            raise _corrupt("redo_head")
        saved: dict[tuple[str, int], bytes] = {}

        def read(file: str, index: int) -> bytes:
            key = (file, index)
            if key in incoming:
                return incoming[key]
            if key not in saved:
                raw = self._read_page(file, index)
                if type(raw) is not bytes or len(raw) != self._page_size:
                    raise _corrupt("page_size")
                page = Page.from_bytes(raw, page_index=index)
                if not activation_sequence < page.page_lsn <= previous_sequence:
                    raise _corrupt("redo_prefix_stamp")
                saved[key] = raw
            return saved[key]

        after = CommitCatalogStore(read, database_uuid=self._uuid, page_size=self._page_size)
        head = after._head(_DIRECTORY)
        if head.entry_count < 1 or head.last_sequence != sequence or head.activation_sequence != activation_sequence:
            raise _corrupt("redo_coverage")
        first = head.entry_count == 1
        if first != (previous_sequence == activation_sequence) or first != (stream_head in incoming):
            raise _corrupt("redo_initialization")
        tail_directory = (COMMIT_DIRECTORY_FILE, 1 + (head.entry_count - 1) // self._per_page)
        if tail_directory not in incoming:
            raise _corrupt("redo_directory")
        # Validates the immutable header's role, UUID and activation horizon. A
        # missing existing file is never synthesized from the latest head.
        after.read_head()
        current = after._item(head.entry_count - 1, head)
        locations = {directory_head, tail_directory}
        locations.update(
            (COMMIT_STREAM_FILE, 1 + index)
            for index in range(current.offset // self._capacity, (head.stream_bytes - 1) // self._capacity + 1)
        )
        if first:
            locations.add(stream_head)
        if set(incoming) != locations:
            raise _corrupt("redo_locations")
        if first:
            if current.offset:
                raise _corrupt("redo_offset")
            previous = CommitCatalogHead(activation_sequence, 0, 0, activation_sequence, 0)
        else:
            prior = after._item(head.entry_count - 2, head)
            after._adjacent(prior, current)
            if prior.sequence != previous_sequence:
                raise _corrupt("redo_previous_commit")
            previous = CommitCatalogHead(
                activation_sequence, head.entry_count - 1, current.offset,
                previous_sequence, prior.ordered,
            )
        after._validate_head(previous)

        # Full older blocks never change. Only the two partial tails need a
        # virtual predecessor encoding, obtained by removing this WAL append.
        virtual: dict[tuple[str, int], bytes] = {
            directory_head: after._header_image(_DIRECTORY, previous).raw,
            stream_head: read(*stream_head),
        }
        for kind, extent, stride, width in (
            (_DIRECTORY, previous.entry_count, self._per_page, _ITEM.size),
            (_STREAM, previous.stream_bytes, self._capacity, 1),
        ):
            used = extent % stride
            if used:
                base = extent - used
                index = 1 + base // stride
                body = after._block(kind, index, head)[:used * width]
                old = after._block_image(kind, base, body)
                virtual[old.file, old.page_index] = old.raw

        def before_read(file: str, index: int) -> bytes:
            raw = virtual.get((file, index))
            return read(file, index) if raw is None else raw

        before = CommitCatalogStore(before_read, database_uuid=self._uuid, page_size=self._page_size)
        append_images = tuple(
            CommitCatalogPageImage(file, index, raw)
            for (file, index), raw in incoming.items() if (file, index) != stream_head
        )
        return before.validate_append_images(
            append_images, previous_sequence=previous_sequence, sequence=sequence,
            activation_sequence=activation_sequence,
        )

    def validate_redo(
        self, replay: CommittedReplay, *, previous_sequence: int, activation_sequence: int,
    ) -> tuple[CommitCatalogEntry, ...]:
        """Check every writing COMMIT in an already proved WAL range without applying.

        Only the first append needs after-image reconstruction. Thereafter each
        validated WAL overlay is the predecessor for the next append, so prefix,
        count and clock continuity are checked against independent earlier WAL
        images. The local overlay lasts only for this selected range, not as a
        physical-authority cache. Ordinary effects still require native preflight.
        """
        if not isinstance(replay, CommittedReplay):
            raise _invalid("replay")
        CommitId(self._uuid, activation_sequence)
        if type(previous_sequence) is not int or not 0 <= previous_sequence < PROVISIONAL_CSN:
            raise _invalid("previous_sequence")
        if type(replay.last_committed_lsn) is not int or not 0 <= replay.last_committed_lsn < PROVISIONAL_CSN:
            raise _corrupt("redo_commit_boundaries")
        validate_commit_boundaries(replay)
        if not replay.commit_records:
            if replay.effects or replay.last_committed_lsn not in {0, previous_sequence}:
                raise _corrupt("redo_commit_boundaries")
            return ()
        commits = tuple((record.epoch, record.txn_id, record.lsn) for record in replay.commit_records)
        if commits[0][2] <= previous_sequence:
            raise _corrupt("redo_commit_boundaries")
        if (
            previous_sequence < activation_sequence
            and any(lsn > activation_sequence for _epoch, _txn, lsn in commits)
            and not any(lsn == activation_sequence for _epoch, _txn, lsn in commits)
        ):
            raise _corrupt("redo_activation")
        offered: dict[tuple[int, int], list[tuple[bytes, int, int]]] = {}
        # Admit every transaction's cardinality before decoding any page images.
        # Capture all supplied effect fields/bytes before any provider callback.
        for effect in replay.effects:
            if effect.record_type != int(WalRecordType.WRITE_PAGE):
                continue
            location = decode_page_write_location(effect.payload)
            journal = location.file in {COMMIT_DIRECTORY_FILE, COMMIT_STREAM_FILE}
            if not journal and not (effect.format_version == 2 and effect.flags & WAL_V2_FLAG_COMMIT_CATALOG_V1):
                continue
            batch = offered.setdefault((effect.epoch, effect.txn_id), [])
            self._admit_append_image_count(len(batch) + 1)
            batch.append((effect.payload, effect.format_version, effect.flags))
        grouped: dict[tuple[int, int], tuple[CommitCatalogPageImage, ...]] = {}
        for owner, batch in offered.items():
            decoded_images: list[CommitCatalogPageImage] = []
            for payload, version, flags in batch:
                decoded = decode_page_write(payload, format_version=version, flags=flags)
                if len(decoded.image) != self._page_size:
                    raise _corrupt("page_size")
                decoded_images.append(CommitCatalogPageImage(decoded.file, decoded.page_index, decoded.image))
            grouped[owner] = tuple(decoded_images)
        overlay: dict[tuple[str, int], bytes] = {}

        def read(file: str, index: int) -> bytes:
            raw = overlay.get((file, index))
            return self._read_page(file, index) if raw is None else raw

        view = CommitCatalogStore(read, database_uuid=self._uuid, page_size=self._page_size)
        results: list[CommitCatalogEntry] = []
        previous = previous_sequence
        for epoch, txn, sequence in commits:
            images = tuple(grouped.get((epoch, txn), ()))
            if sequence <= activation_sequence:
                if images:
                    raise _corrupt("redo_before_activation")
                previous = sequence
                continue
            if not images:
                raise _corrupt("redo_missing_commit")
            if not results:
                result = view.validate_redo_images(images, previous_sequence=previous, sequence=sequence, activation_sequence=activation_sequence)
            else:
                result = view.validate_append_images(images, previous_sequence=previous, sequence=sequence, activation_sequence=activation_sequence)
            overlay.update({(image.file, image.page_index): image.raw for image in images})
            results.append(result)
            previous = sequence
        return tuple(results)

    def lookup(self, identity: CommitId, *, read_lsn: int) -> CommitCatalogEntry | None:
        """Exact O(log N) ordinal lookup, bounded to read_lsn and the retained horizon.

        None means absent in the tracked interval/snapshot, NOT an assertion about
        earlier untracked legacy history. The public result must expose that boundary.
        Full historical integrity is the verifier's job, not inferred from this seek.
        """
        if type(identity) is not CommitId:
            raise _invalid("identity")
        captured = CommitId(identity.database_uuid, identity.sequence)
        if captured.database_uuid != self._uuid:
            raise _invalid("database_uuid")
        if type(read_lsn) is not int or not 0 <= read_lsn < PROVISIONAL_CSN:
            raise _invalid("read_lsn")
        head = self.read_head()
        target = captured.sequence
        if not head.activation_sequence < target <= min(read_lsn, head.last_sequence):
            return None
        lo, hi = 0, head.entry_count
        lower, upper = head.activation_sequence, head.last_sequence + 1
        while lo < hi:
            middle = (lo + hi) // 2
            item = self._item(middle, head)
            if not lower < item.sequence < upper:
                raise _corrupt("search_order")
            if item.sequence == target:
                return self._record(item, head)
            if item.sequence < target:
                lo, lower = middle + 1, item.sequence
            else:
                hi, upper = middle, item.sequence
        return None

    def _items(self, head: CommitCatalogHead) -> Iterator[_DirectoryItem]:
        for index in range(1, 1 + (head.entry_count + self._per_page - 1) // self._per_page):
            yield from self._directory_page(index, head)

    def verify(self) -> CommitCatalogHead:
        """Stream the entire advertised catalog with bounded memory and no repair.

        Covers all records and cross-page ordering/coverage; unreferenced physical
        pages, WAL correspondence and publication authority belong to engine verify.
        """
        head = self.read_head()
        previous: _DirectoryItem | None = None
        for item in self._items(head):
            if previous is not None:
                self._adjacent(previous, item)
            self._record(item, head)
            previous = item
        return head
