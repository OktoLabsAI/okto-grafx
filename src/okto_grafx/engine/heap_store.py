"""The heap: where every version of every record is stored (CONTRACT.md sections 6.4 and 8.2).

The heap file is a sequence of pages. Page 0 is the reserved header page: it carries the file
header in slot 0 and, in the slots that follow, one directory entry per table saying where the
pages of that table begin and end. Every data page belongs to exactly one table, records that in
its own slot 0, and points at the next page of the same table through the next_page header
field. A payload too large for one page is written to a chain of overflow pages instead, and the
record keeps only the first page of that chain.

Two rules decide everything else:

* an update never overwrites a version. It writes a new one, chains prev_version to the old one
  and sets the xmax of the old one, so a reader under an older snapshot still finds what it saw.
* the store never hides a row. scan and lookup take a snapshot and ask it, through its own
  visible predicate, whether a version may be seen; the rule itself belongs to the transaction
  manager, and the heap only applies the answer.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_PAGE, Csn, PageIndex, RecordId, RecordRef, SlotId
from okto_grafx.domain.model.record import (
    NO_PREVIOUS_VERSION,
    RECORD_FLAG_DELETED,
    RECORD_FLAG_HAS_OVERFLOW,
    RECORD_HEADER_SIZE,
    HeapVersion,
    RecordHeader,
    decode_overflow_pointer,
    encode_overflow_pointer,
)
from okto_grafx.domain.model.schema import TableDef, decode_tuple, encode_tuple
from okto_grafx.domain.model.value import Value
from okto_grafx.domain.page import (
    FILE_HEADER_SIZE,
    HEADER_PAGE_INDEX,
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageFullError,
    PageType,
)
from okto_grafx.engine.buffer_pool import BufferPool, read_chain, write_chain

if TYPE_CHECKING:
    from okto_grafx.engine.catalog_store import CatalogStore

__all__ = [
    "HEAP_FILE",
    "DESCRIPTOR_SLOT",
    "FIRST_RECORD_SLOT",
    "DESCRIPTOR_SIZE",
    "DIRECTORY_ENTRY_SIZE",
    "MINIMUM_FRAMES",
    "SnapshotLike",
    "TableExtent",
    "HeapVersion",
    "HeapStore",
]

HEAP_FILE: str = "heap.dat"
"""The default name of the heap file inside a database directory."""

DESCRIPTOR_SLOT: SlotId = 0
"""Slot 0 of a heap data page says which table the page belongs to."""

FIRST_RECORD_SLOT: SlotId = 1
"""Records start at slot 1, because slot 0 of a data page is the page descriptor."""

MINIMUM_FRAMES: int = 2
"""Frames the heap needs at once: one page being written and one page being linked to it."""

_DESCRIPTOR = struct.Struct("<I")
_DIRECTORY_ENTRY = struct.Struct("<IIII")

DESCRIPTOR_SIZE: int = _DESCRIPTOR.size
"""Bytes of the page descriptor that opens every heap data page."""

DIRECTORY_ENTRY_SIZE: int = _DIRECTORY_ENTRY.size
"""Bytes of one table directory entry on the header page."""


class SnapshotLike(Protocol):
    """The only thing the heap needs from a snapshot: whether a version may be seen.

    The real Snapshot is a frozen value of the transaction manager (CONTRACT.md section 8.5).
    The heap deliberately does not import it: taking the predicate structurally is what keeps the
    visibility rule in one place and keeps the store from growing an opinion of its own.
    """

    def visible(self, xmin: int, xmax: int) -> bool:
        """Return True when a version written at xmin and ended at xmax is visible."""
        ...


@dataclass(frozen=True, slots=True)
class TableExtent:
    """Where the pages of one table are: the first, the last, and how many there are."""

    table_id: int
    first_page: PageIndex
    last_page: PageIndex
    page_count: int

    def encode(self) -> bytes:
        """Return the stored form of this directory entry."""
        return _DIRECTORY_ENTRY.pack(
            self.table_id, self.first_page, self.last_page, self.page_count
        )

    @classmethod
    def decode(cls, raw: bytes) -> TableExtent:
        """Parse a directory entry from the header page of the heap."""
        if len(raw) != DIRECTORY_ENTRY_SIZE:
            raise GrafxCorruptionDetected(
                f"A heap directory entry is {DIRECTORY_ENTRY_SIZE} bytes; got {len(raw)}.",
                field="directory_entry",
                value=len(raw),
            )
        table_id, first_page, last_page, page_count = _DIRECTORY_ENTRY.unpack(raw)
        return cls(
            table_id=table_id,
            first_page=first_page,
            last_page=last_page,
            page_count=page_count,
        )


class HeapStore:
    """Insert, update, delete, read and scan record versions over a paged heap file."""

    __slots__ = ("_pool", "_catalog", "_file")

    def __init__(
        self, pool: BufferPool, catalog: CatalogStore, *, file: str = HEAP_FILE
    ) -> None:
        """Bind the heap to a buffer pool, the catalog that names its tables, and its file."""
        if pool.capacity_pages < MINIMUM_FRAMES:
            raise GrafxConfigurationError(
                f"The heap needs a buffer budget of at least {MINIMUM_FRAMES} pages of "
                f"{pool.page_size} bytes; the budget of {pool.budget_bytes} bytes holds "
                f"{pool.capacity_pages}.",
                field="budget_bytes",
                value=pool.budget_bytes,
            )
        self._pool: BufferPool = pool
        self._catalog: CatalogStore = catalog
        self._file: str = file

    @property
    def file(self) -> str:
        """Return the name of the heap file this store reads and writes."""
        return self._file

    @property
    def max_tables(self) -> int:
        """Return how many tables the directory on the reserved header page can name."""
        available = (
            self._pool.page_size - PAGE_HEADER_SIZE - FILE_HEADER_SIZE - SLOT_ENTRY_SIZE
        )
        return available // (DIRECTORY_ENTRY_SIZE + SLOT_ENTRY_SIZE)

    @property
    def inline_capacity(self) -> int:
        """Return the largest record that fits in a page without an overflow chain."""
        return (
            self._pool.page_size
            - PAGE_HEADER_SIZE
            - DESCRIPTOR_SIZE
            - SLOT_ENTRY_SIZE * 2
        )

    # --- lifecycle -------------------------------------------------------------------------

    def is_bootstrapped(self) -> bool:
        """Return True when the heap file exists and carries its header page."""
        storage = self._pool.storage
        return storage.exists(self._file) and storage.page_count(self._file) > 0

    def bootstrap(self) -> None:
        """Create the heap file and its reserved header page if they do not exist yet."""
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        if storage.page_count(self._file) == 0:
            page = self._pool.allocate(self._file, int(PageType.META))
            try:
                if page.page_index != HEADER_PAGE_INDEX:
                    raise GrafxCorruptionDetected(
                        f"The header page of {self._file!r} must be page "
                        f"{HEADER_PAGE_INDEX}; the device handed out {page.page_index}.",
                        file=self._file,
                        page=page.page_index,
                    )
                FileHeaderPage.initialize(
                    page, FileHeader(kind=FileKind.HEAP, page_size=self._pool.page_size)
                )
            finally:
                self._pool.unpin(self._file, page.page_index, dirty=True)

    # --- writing ---------------------------------------------------------------------------

    def insert(
        self,
        table: TableDef,
        record_id: RecordId,
        values: tuple[Value, ...],
        xmin: Csn,
    ) -> RecordRef:
        """Store the first version of a record and return where it was placed."""
        payload = encode_tuple(table, values)
        header = RecordHeader(
            record_id=record_id,
            xmin=xmin,
            xmax=0,
            prev_version=NO_PREVIOUS_VERSION,
            payload_len=len(payload),
            schema_version=table.schema_version,
        )
        return self._store_version(table, header, payload)

    def update(
        self,
        table: TableDef,
        ref: RecordRef,
        values: Sequence[Value],
        xmin: Csn,
    ) -> RecordRef:
        """Write a new version of a record, chained to the old one, and end the old one.

        The old version is not touched until the new one exists: its xmax is set afterwards, to
        the same commit sequence number that begins the new version, so the two are contiguous
        for every snapshot and no instant exists in which the record is invisible.
        """
        previous = self._read_header(table, ref)
        if previous.xmax != 0:
            raise GrafxTransactionStateError(
                f"Version {ref.page}:{ref.slot} of record {previous.record_id} already ended at "
                f"{previous.xmax} and cannot be updated.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                xmax=previous.xmax,
            )
        payload = encode_tuple(table, tuple(values))
        header = RecordHeader(
            record_id=previous.record_id,
            xmin=xmin,
            xmax=0,
            prev_version=ref.encode(),
            payload_len=len(payload),
            schema_version=table.schema_version,
        )
        new_ref = self._store_version(table, header, payload)
        self._end_version(table, ref, xmin, deleted=False)
        return new_ref

    def delete(self, table: TableDef, ref: RecordRef, xmax: Csn) -> None:
        """End a version with a delete, which leaves the bytes in place and marks the header."""
        self._end_version(table, ref, xmax, deleted=True)

    # --- reading ---------------------------------------------------------------------------

    def read(self, ref: RecordRef) -> HeapVersion:
        """Return the version stored at that location, whichever table it belongs to."""
        table_id, content = self._read_slot(ref)
        table = self._catalog.catalog.table_by_id(table_id)
        return self._decode_version(table, content)

    def scan(
        self, table: TableDef, snapshot: SnapshotLike
    ) -> Iterator[tuple[RecordRef, HeapVersion]]:
        """Yield every version of the table the snapshot can see, in storage order."""
        for ref, header, content in self._walk(table):
            if not snapshot.visible(header.xmin, header.xmax):
                continue
            yield ref, self._decode_version(table, content)

    def scan_all(self, table: TableDef) -> Iterator[tuple[RecordRef, HeapVersion]]:
        """Yield every stored version of the table, visible or not.

        Recovery and verification need the whole truth of what is on the pages, which is exactly
        what a snapshot is designed to hide.
        """
        for ref, _header, content in self._walk(table):
            yield ref, self._decode_version(table, content)

    def lookup(
        self, table: TableDef, record_id: RecordId, snapshot: SnapshotLike
    ) -> HeapVersion | None:
        """Return the version of that record the snapshot can see, or None when there is none."""
        for _ref, header, content in self._walk(table):
            if header.record_id != record_id:
                continue
            if not snapshot.visible(header.xmin, header.xmax):
                continue
            return self._decode_version(table, content)
        return None

    def version_chain(self, ref: RecordRef) -> tuple[RecordRef, ...]:
        """Return the chain of versions that ends at this one, newest first.

        A chain that returns to a version it already visited is corruption, not a loop to walk.
        """
        chain: list[RecordRef] = []
        seen: set[int] = set()
        current: RecordRef | None = ref
        while current is not None:
            encoded = current.encode()
            if encoded in seen:
                raise GrafxCorruptionDetected(
                    f"The version chain of {self._file!r} returns to page {current.page} slot "
                    f"{current.slot}, so it is a cycle.",
                    file=self._file,
                    page=current.page,
                    slot=current.slot,
                )
            seen.add(encoded)
            chain.append(current)
            _table_id, content = self._read_slot(current)
            current = RecordHeader.decode(content).previous
        return tuple(chain)

    def pages_of(self, table: TableDef) -> tuple[PageIndex, ...]:
        """Return the data pages of the table, in chain order."""
        extent = self._find_extent(table.table_id)
        if extent is None:
            return ()
        pages: list[PageIndex] = []
        seen: set[PageIndex] = set()
        index = extent.first_page
        while index != NO_PAGE:
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                )
            seen.add(index)
            pages.append(index)
            with self._pool.pinned(self._file, index) as page:
                index = page.next_page
        return tuple(pages)

    # --- internals ---------------------------------------------------------------------------

    def _store_version(
        self, table: TableDef, header: RecordHeader, payload: bytes
    ) -> RecordRef:
        """Place one encoded version, using an overflow chain when it does not fit inline."""
        if RECORD_HEADER_SIZE + len(payload) <= self.inline_capacity:
            content = header.encode() + payload
        else:
            chain = write_chain(
                self._pool, self._file, payload, page_type=int(PageType.OVERFLOW)
            )
            overflowed = replace(header, flags=header.flags | RECORD_FLAG_HAS_OVERFLOW)
            content = overflowed.encode() + encode_overflow_pointer(chain[0])
        return self._append(table, content)

    def _append(self, table: TableDef, content: bytes) -> RecordRef:
        """Append one slot to the last page of the table, growing the chain when it is full."""
        extent = self._extent_for(table)
        with self._pool.pinned(self._file, extent.last_page) as page:
            if page.can_fit(len(content)):
                return RecordRef(page=extent.last_page, slot=page.insert_slot(content))
        fresh = self._pool.allocate(self._file, int(PageType.HEAP))
        new_index = fresh.page_index
        try:
            self._initialize_data_page(fresh, table.table_id)
            slot = fresh.insert_slot(content)
        finally:
            self._pool.unpin(self._file, new_index, dirty=True)
        with self._pool.pinned(self._file, extent.last_page) as page:
            page.next_page = new_index
        self._write_extent(
            replace(extent, last_page=new_index, page_count=extent.page_count + 1)
        )
        return RecordRef(page=new_index, slot=slot)

    def _initialize_data_page(self, page: Page, table_id: int) -> None:
        """Turn a fresh page into a data page of one table, with its descriptor in slot 0."""
        page.clear()
        page.page_type = int(PageType.HEAP)
        page.next_page = NO_PAGE
        page.insert_slot(_DESCRIPTOR.pack(table_id))

    def _extent_for(self, table: TableDef) -> TableExtent:
        """Return the directory entry of the table, creating its first page when needed."""
        extent = self._find_extent(table.table_id)
        if extent is not None:
            return extent
        fresh = self._pool.allocate(self._file, int(PageType.HEAP))
        index = fresh.page_index
        try:
            self._initialize_data_page(fresh, table.table_id)
        finally:
            self._pool.unpin(self._file, index, dirty=True)
        extent = TableExtent(
            table_id=table.table_id, first_page=index, last_page=index, page_count=1
        )
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            self._add_directory_entry(header_page, table, extent)
        return extent

    def _add_directory_entry(
        self, header_page: Page, table: TableDef, extent: TableExtent
    ) -> None:
        """Add one table to the directory, turning a full header page into a clear refusal.

        The directory lives on the single reserved header page, so the number of tables one heap
        file can hold is bounded by the page size. Reaching that bound is a real limit of the
        format rather than damage, and the caller is told so by name.
        """
        try:
            header_page.insert_slot(extent.encode())
        except PageFullError as full:
            raise GrafxUnsupportedOperation(
                f"The header page of {self._file!r} is full, so it cannot hold the directory "
                f"entry of table {table.name!r}; this heap file is limited to "
                f"{self.max_tables} tables.",
                file=self._file,
                table=table.name,
                max_tables=self.max_tables,
            ) from full

    def _find_extent(self, table_id: int) -> TableExtent | None:
        """Return the directory entry of the table, or None when the table has no page yet."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            for slot, payload in header_page.iter_slots():
                if slot == 0:
                    continue
                extent = TableExtent.decode(payload)
                if extent.table_id == table_id:
                    return extent
        return None

    def _write_extent(self, extent: TableExtent) -> None:
        """Replace the directory entry of a table on the header page."""
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            for slot, payload in header_page.iter_slots():
                if slot == 0:
                    continue
                if TableExtent.decode(payload).table_id == extent.table_id:
                    header_page.update_slot(slot, extent.encode())
                    return
            raise GrafxCorruptionDetected(
                f"Table {extent.table_id} has no directory entry on the header page of "
                f"{self._file!r}, so its extent cannot be updated.",
                file=self._file,
                table_id=extent.table_id,
            )

    def _require_bootstrapped(self) -> None:
        """Refuse to work against a heap file that has not been created yet."""
        if not self.is_bootstrapped():
            raise GrafxCorruptionDetected(
                f"The heap file {self._file!r} has no header page; call bootstrap() first.",
                file=self._file,
            )

    def _read_slot(self, ref: RecordRef) -> tuple[int, bytes]:
        """Return the table id of the page and the raw slot content at a reference."""
        if ref.page == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} is the reserved header page and "
                f"holds no record.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
            )
        with self._pool.pinned(self._file, ref.page) as page:
            self._require_data_page(page)
            table_id = _DESCRIPTOR.unpack(page.read_slot(DESCRIPTOR_SLOT))[0]
            if ref.slot < FIRST_RECORD_SLOT:
                raise GrafxCorruptionDetected(
                    f"Slot {ref.slot} of page {ref.page} in {self._file!r} is the page "
                    f"descriptor, not a record.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                )
            return int(table_id), page.read_slot(ref.slot)

    def _require_data_page(self, page: Page) -> None:
        """Refuse a page that is not a heap data page."""
        if page.page_type != int(PageType.HEAP):
            raise GrafxCorruptionDetected(
                f"Page {page.page_index} of {self._file!r} is of type {page.page_type} and is "
                f"not a heap data page.",
                file=self._file,
                page=page.page_index,
                page_type=page.page_type,
            )
        if page.slot_count < 1:
            raise GrafxCorruptionDetected(
                f"Heap page {page.page_index} of {self._file!r} carries no page descriptor.",
                file=self._file,
                page=page.page_index,
            )

    def _read_header(self, table: TableDef, ref: RecordRef) -> RecordHeader:
        """Return the record header at a reference, checking it belongs to the table."""
        table_id, content = self._read_slot(ref)
        if table_id != table.table_id:
            raise GrafxCorruptionDetected(
                f"Page {ref.page} of {self._file!r} belongs to table {table_id}, not to "
                f"{table.name!r} with id {table.table_id}.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                table=table.name,
                table_id=table_id,
            )
        return RecordHeader.decode(content)

    def _end_version(
        self, table: TableDef, ref: RecordRef, xmax: Csn, *, deleted: bool
    ) -> None:
        """Set the xmax of one version in place, without touching its payload."""
        if xmax == 0:
            raise GrafxTransactionStateError(
                "A version must end at a commit sequence number above zero, because zero means "
                "the version is still live.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
            )
        with self._pool.pinned(self._file, ref.page) as page:
            self._require_data_page(page)
            table_id = int(_DESCRIPTOR.unpack(page.read_slot(DESCRIPTOR_SLOT))[0])
            if table_id != table.table_id:
                raise GrafxCorruptionDetected(
                    f"Page {ref.page} of {self._file!r} belongs to table {table_id}, not to "
                    f"{table.name!r} with id {table.table_id}.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    table=table.name,
                    table_id=table_id,
                )
            content = page.read_slot(ref.slot)
            header = RecordHeader.decode(content)
            if header.xmax != 0:
                raise GrafxTransactionStateError(
                    f"Version {ref.page}:{ref.slot} of record {header.record_id} already ended "
                    f"at {header.xmax}.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    xmax=header.xmax,
                )
            ended = header.ended_at(xmax, deleted=deleted)
            page.update_slot(ref.slot, ended.encode() + content[RECORD_HEADER_SIZE:])

    def _walk(
        self, table: TableDef
    ) -> Iterator[tuple[RecordRef, RecordHeader, bytes]]:
        """Yield every stored version of the table with its location and its raw content.

        One page at a time is pinned and its slots are copied out before anything is decoded, so
        following an overflow chain never needs a second frame while a data page is still held.
        """
        extent = self._find_extent(table.table_id)
        if extent is None:
            return
        index = extent.first_page
        seen: set[PageIndex] = set()
        while index != NO_PAGE:
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                )
            seen.add(index)
            with self._pool.pinned(self._file, index) as page:
                self._require_data_page(page)
                page_table_id = int(_DESCRIPTOR.unpack(page.read_slot(DESCRIPTOR_SLOT))[0])
                if page_table_id != table.table_id:
                    raise GrafxCorruptionDetected(
                        f"Page {index} of {self._file!r} is chained to table "
                        f"{table.name!r} but declares table {page_table_id}.",
                        file=self._file,
                        page=index,
                        table=table.name,
                        table_id=page_table_id,
                    )
                items = tuple(
                    (slot, payload)
                    for slot, payload in page.iter_slots()
                    if slot >= FIRST_RECORD_SLOT
                )
                following = page.next_page
            for slot, content in items:
                yield RecordRef(page=index, slot=slot), RecordHeader.decode(content), content
            index = following

    def _decode_version(self, table: TableDef, content: bytes) -> HeapVersion:
        """Turn the raw content of a slot into a decoded version of the table."""
        header = RecordHeader.decode(content)
        if header.schema_version != table.schema_version:
            raise GrafxSchemaVersionMismatch(
                f"A stored version of table {table.name!r} was written under schema version "
                f"{header.schema_version}, and the catalog now declares "
                f"{table.schema_version}.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                stored_schema_version=header.schema_version,
                current_schema_version=table.schema_version,
            )
        payload = self._payload_of(header, content)
        if len(payload) != header.payload_len:
            raise GrafxCorruptionDetected(
                f"A record of table {table.name!r} declares {header.payload_len} payload bytes "
                f"but carries {len(payload)}.",
                file=self._file,
                table=table.name,
                declared=header.payload_len,
                observed=len(payload),
            )
        return HeapVersion(
            record_id=header.record_id,
            xmin=header.xmin,
            xmax=header.xmax,
            values=decode_tuple(table, payload),
            prev=header.previous,
            schema_version=header.schema_version,
            deleted=bool(header.flags & RECORD_FLAG_DELETED),
            table_id=table.table_id,
        )

    def _payload_of(self, header: RecordHeader, content: bytes) -> bytes:
        """Return the payload of a version, following its overflow chain when it has one."""
        if not header.has_overflow:
            return content[RECORD_HEADER_SIZE:]
        first = decode_overflow_pointer(content, RECORD_HEADER_SIZE)
        payload = read_chain(
            self._pool, self._file, first, page_type=int(PageType.OVERFLOW)
        )
        return payload[: header.payload_len]

    def __repr__(self) -> str:
        return f"HeapStore(file={self._file!r}, inline_capacity={self.inline_capacity})"
