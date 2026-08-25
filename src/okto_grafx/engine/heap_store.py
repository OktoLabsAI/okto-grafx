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
from okto_grafx.domain.ids import (
    NO_PAGE,
    PROVISIONAL_CSN,
    Csn,
    PageIndex,
    RecordId,
    RecordRef,
    SlotId,
    is_open_end_csn,
    is_provisional_csn,
)
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
from okto_grafx.domain.model.schema import (
    SOURCE_COLUMN,
    TARGET_COLUMN,
    TableDef,
    decode_tuple,
    encode_tuple,
)
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
    PageType,
)
from okto_grafx.engine.buffer_pool import (
    BufferPool,
    apply_page_image,
    read_chain,
    refuse_endless_chain,
    visited_pages,
    write_chain,
)

if TYPE_CHECKING:
    from okto_grafx.engine.catalog_store import CatalogStore

__all__ = [
    "HEAP_FILE",
    "DESCRIPTOR_SLOT",
    "FIRST_RECORD_SLOT",
    "EXTENT_FIRST_SLOT",
    "DESCRIPTOR_SIZE",
    "DIRECTORY_ENTRY_SIZE",
    "MAX_DIRECTORY_FIELD",
    "MAX_U64",
    "FIRST_RECORD_ID",
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

EXTENT_FIRST_SLOT: SlotId = 1
"""Table extents start at slot 1 of the header page, because slot 0 holds the file header."""

MINIMUM_FRAMES: int = 2
"""Frames the heap needs at once: one page being written and one page being linked to it."""

_DESCRIPTOR = struct.Struct("<I")
_DIRECTORY_ENTRY = struct.Struct("<IIIIQ")

DESCRIPTOR_SIZE: int = _DESCRIPTOR.size
"""Bytes of the page descriptor that opens every heap data page."""

MAX_DIRECTORY_FIELD: int = 0xFFFFFFFF
"""Largest value any field of a directory entry can hold, since all four are 32-bit."""

DIRECTORY_ENTRY_SIZE: int = _DIRECTORY_ENTRY.size
"""Bytes of one table directory entry on the header page."""


def _require_directory_field(field: str, value: int) -> int:
    """Return a directory entry field after checking it fits the 32 bits it is stored in."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxCorruptionDetected(
            f"A heap directory entry needs an integer {field}; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= MAX_DIRECTORY_FIELD:
        raise GrafxCorruptionDetected(
            f"A heap directory entry field must fit in 32 bits; {field} is {value}.",
            field=field,
            value=value,
        )
    return value


MAX_U64: int = 0xFFFFFFFFFFFFFFFF
"""The widest value the record header can store in any of its 64-bit fields."""

FIRST_RECORD_ID: RecordId = 1
"""The id the first row of a table takes.

One rather than zero, so that zero stays available to every caller as "no such row", which is
what section 3 already means by it for an Lsn and an Epoch.
"""


def _require_wide_field(field: str, value: int) -> int:
    """Return a directory field that is stored as a u64, refusing one that cannot be packed.

    The u32 checker beside this one cannot answer for a record id: a record id is a 64-bit
    identity, and passing it through a u32 bound would refuse three quarters of the space the
    format reserves for it while claiming the entry was corrupt.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxCorruptionDetected(
            f"A heap directory field {field!r} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= MAX_U64:
        raise GrafxCorruptionDetected(
            f"A heap directory field {field!r} is outside its width: {value} exceeds {MAX_U64}.",
            field=field,
            value=value,
        )
    return value


def _require_commit_number(field: str, value: Csn) -> Csn:
    """Return a commit sequence number after refusing one no snapshot could ever see.

    Zero is not a small number here, it is the sentinel for "no such commit": a version written
    with xmin of zero is invisible to every snapshot for as long as it exists, so persisting one
    and refusing afterwards leaves a row nobody can read and nobody can find. The refusal has to
    come before anything is written.

    Both ends are guarded. Only the low one was, so a number too WIDE for the field walked past
    here and was refused further in by the header encoder, which speaks corruption_detected --
    FR-8 and FR-10 turn that into truncation, quarantine and a forensic entry, manufactured out
    of a caller's arithmetic (A11-revised, D5 round 8). One guard, one classification, both ends.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxTransactionStateError(
            f"A commit sequence number must be an integer; {field} is a "
            f"{type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value <= 0:
        raise GrafxTransactionStateError(
            f"A commit sequence number starts at one, because zero means no commit; {field} is "
            f"{value}.",
            field=field,
            value=value,
        )
    if value > MAX_U64:
        raise GrafxTransactionStateError(
            f"A commit sequence number is a 64-bit field; {field} is {value}, which does not "
            f"fit in it.",
            field=field,
            value=value,
        )
    return value


def _require_record_id(value: RecordId) -> RecordId:
    """Return a record id after refusing one the header could not carry.

    The id arrives from a caller, so a wrong one is a caller error however wrong it is. Left
    unguarded it reached the header encoder, which is written for bytes off a page and answers
    corruption_detected -- and a typed id, a negative id and a bool all became integrity
    incidents (A11-revised, D5 round 8).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"A record id must be an integer; got a {type(value).__name__}.",
            field="record_id",
            value=repr(value),
        )
    if not 0 <= value <= MAX_U64:
        raise GrafxConfigurationError(
            f"A record id is an unsigned 64-bit field; {value} does not fit in it.",
            field="record_id",
            value=value,
        )
    return value


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
    next_record_id: RecordId = FIRST_RECORD_ID
    """The id the next row of this table takes, one past the highest ever allocated.

    It lives here rather than in the catalog because of what each page costs to write. The
    catalog is a chain of its own pages that a save rewrites whole, so a counter there would turn
    every insert into a catalog rewrite; this entry is on page 0 of the heap file, which an
    append already reaches whenever the tail hint has moved, and which the pool holds resident
    for as long as the table is being written. Section 6 freezes the page header, the record
    header, the WAL record and the ledger entry -- it does not freeze this directory entry, so
    widening it needs no amendment.

    Unlike next_table_id in the catalog, this counter cannot be DERIVED from what is in use: the
    ids in use are spread over every page of the table, and deriving one would cost a full scan
    on every open. So it is stored, and the invariant is the weaker one A40 uses for the page
    count -- strictly above every id in use, never equal to or below it
    (observe_record_id is what enforces it).
    """

    def encode(self) -> bytes:
        """Return the stored form of this directory entry, refusing a field it cannot pack.

        Every field here was read back from a page, so any of them can be damaged bytes. An
        unchecked pack throws a raw struct.error out of the public insert door -- after the row
        has already been written and linked -- and a raw struct.error is not a Grafx error, so no
        caller can classify it and no ledger can record it (A41, CONTRACT.md section 11 item 5).
        """
        return _DIRECTORY_ENTRY.pack(
            _require_directory_field("table_id", self.table_id),
            _require_directory_field("first_page", self.first_page),
            _require_directory_field("last_page", self.last_page),
            _require_directory_field("page_count", self.page_count),
            _require_wide_field("next_record_id", self.next_record_id),
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
        table_id, first_page, last_page, page_count, next_record_id = (
            _DIRECTORY_ENTRY.unpack(raw)
        )
        if next_record_id < FIRST_RECORD_ID:
            # The catalog makes the same refusal about its own counters: a stored counter below
            # the first id that can exist is not a small number, it is a number that would hand
            # out an id some row may already be using, and reuse is the one failure this counter
            # exists to prevent (catalog.py, A11-revised on the class: these are page bytes).
            raise GrafxCorruptionDetected(
                f"A heap directory entry declares next_record_id {next_record_id}, below the "
                f"first id that can exist ({FIRST_RECORD_ID}).",
                field="next_record_id",
                value=next_record_id,
                table_id=table_id,
            )
        return cls(
            table_id=table_id,
            first_page=first_page,
            last_page=last_page,
            page_count=page_count,
            next_record_id=next_record_id,
        )


class HeapStore:
    """Insert, update, delete, read and scan record versions over a paged heap file."""

    __slots__ = ("_pool", "_catalog", "_file", "_tail_cache")

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
        # The resolved tail of each table, so an append stays O(1) after the first walk. It is a
        # cache of this instance and of nothing else: the durable hint on page 0 is what a cold
        # start and another process read (A40.3).
        self._tail_cache: dict[int, tuple[PageIndex, int, int]] = {}

    @property
    def catalog(self) -> CatalogStore:
        """Return the catalog store this heap resolves its tables through."""
        return self._catalog

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
        """Return True when page 0 of the heap file really is its reserved header page.

        The question is deliberately not "does the file have any pages". Amendment A22 lets
        recovery grow the file before anything has reserved page 0, so a heap file can exist,
        have pages, and still have no header; answering yes there would let the table directory
        be written into the page that is supposed to hold the file header.

        A page 0 that is pristine has never been written and is therefore reservable. Anything
        that is not a heap header page is damage, and damage is raised rather than reported as
        "not bootstrapped", because the caller of this predicate is bootstrap(), and bootstrap()
        must never overwrite a header page that merely failed to be understood (G6).
        """
        storage = self._pool.storage
        if not storage.exists(self._file) or storage.page_count(self._file) == 0:
            return False
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            if page.is_pristine():
                return False
            self._require_header_page(page)
        return True

    def bootstrap(self) -> None:
        """Create the heap file and reserve its header page if that has not happened yet."""
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        if self.is_bootstrapped():
            return
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
                self._reserve_header_page(page)
            finally:
                self._pool.unpin(self._file, page.page_index, dirty=True)
            return
        # The file already reaches past page 0 without anyone having reserved it, which is what
        # a redo of a later page leaves behind. The page is free, so it is reserved in place.
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            self._reserve_header_page(page)

    def _reserve_header_page(self, page: Page) -> None:
        """Turn page 0 into the reserved header page of this heap file."""
        FileHeaderPage.initialize(
            page, FileHeader(kind=FileKind.HEAP, page_size=self._pool.page_size)
        )

    def _require_header_page(self, page: Page) -> FileHeader:
        """Return the file header of page 0, refusing a page that is not one.

        Reading a header from a page that is not a header page is how a zeroed or repurposed
        page 0 turns into "this table has no extents" instead of into an error.
        """
        if page.page_type != int(PageType.META):
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} is of type {page.page_type} and "
                f"is not the reserved header page.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                page_type=page.page_type,
                expected_page_type=int(PageType.META),
            )
        header = FileHeaderPage.read(page)
        if header.kind is not FileKind.HEAP:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} carries the header of a "
                f"{header.kind.name.lower()} file, not of a heap.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                kind=header.kind.name,
            )
        if header.page_size != self._pool.page_size:
            raise GrafxCorruptionDetected(
                f"The heap file {self._file!r} was written with pages of "
                f"{header.page_size} bytes, and this database uses {self._pool.page_size}.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                page_size=header.page_size,
            )
        return header

    def apply_page_image(self, page_index: PageIndex, image: bytes) -> bool:
        """Install a page image if it is newer than the page, and say whether it was applied.

        This is the redo rule of CONTRACT.md section 8.5 step 6 and amendment A22, applied to the
        heap file. It covers every page of the file, the reserved header page and the overflow
        pages included, because recovery replays what the log recorded and the log records page
        images without caring what the page is for.

        Whatever this store walked to and remembered stops being trusted here, though not by
        this method: the image carries its own next_page, so a redo can take pages out of a chain
        without touching the page a walk stopped at, and the pool records that by advancing the
        structure epoch of the file. Every holder of a derived walk reads that epoch before it
        trusts what it derived. Clearing the cache here as well would answer the same question a
        second time and make the first answer impossible to test (A34).
        """
        return apply_page_image(self._pool, self._file, page_index, image)

    # --- writing ---------------------------------------------------------------------------

    def require_endpoints(
        self, table: TableDef, values: Sequence[Value], snapshot: SnapshotLike
    ) -> tuple[RecordId, RecordId]:
        """Refuse an edge whose endpoints name rows this snapshot cannot see, and return them.

        BEFORE anything is staged, not after. The edge tuple has not been encoded, no page has
        been pinned and no identity has been spent when this answers, so a refusal here leaves
        nothing behind -- which is the same rule the row itself is written under, applied to the
        thing the row points at.

        Visibility is the caller's snapshot, not this store's opinion: an edge must not be able to
        point at a row that the transaction writing it cannot see, because that row may be a
        version another transaction has not committed, or one an older snapshot has already had
        deleted out from under it. The store still holds no opinion of its own -- it asks the
        snapshot it was handed, which is what every other read door here does.

        The cost is a scan of each endpoint table, because C1 has no index: an index by record id
        is C7's, and when one exists this door is where it plugs in. It is stated rather than
        hidden, so a caller inserting many edges knows to expect it.
        """
        source = table.source_of(values)
        target = table.target_of(values)
        for name, identity, end in (
            (table.from_table, source, SOURCE_COLUMN),
            (table.to_table, target, TARGET_COLUMN),
        ):
            if name is None:
                raise GrafxConfigurationError(
                    f"Relationship table {table.name!r} does not say which table its {end!r} "
                    f"endpoint belongs to.",
                    table=table.name,
                    field=end,
                )
            endpoint_table = self._catalog.catalog.table(name)
            _require_record_id(identity)
            if self.lookup(endpoint_table, identity, snapshot) is None:
                raise GrafxConfigurationError(
                    f"An edge in {table.name!r} names row {identity} of {name!r} as its {end!r} "
                    f"endpoint, and this snapshot has no such row.",
                    table=table.name,
                    table_id=table.table_id,
                    field=end,
                    endpoint_table=name,
                    value=identity,
                )
        return source, target

    # --- record identity ----------------------------------------------------------------------

    def allocate_record_id(self, table: TableDef) -> RecordId:
        """Take the next row identity of this table and record that it is spent.

        Section 3 calls a RecordId "a stable logical identity across versions", and three things
        have to hold for that to be true:

        1. ALLOCATED ONCE, NEVER REUSED, across a crash. The counter is advanced and written to
           the directory entry BEFORE this returns, so the id is spent on a page before any
           caller can act on it. A crash between the two leaves the counter ahead of the rows --
           a gap in the sequence, which costs nothing -- and never behind them, which would hand
           the same identity to a second row.
        2. STABLE ACROSS VERSIONS. Nothing here touches a version: update and delete carry the id
           the insert was given, and this door is never on their path.
        3. REPLAY MUST NOT RE-ALLOCATE. Recovery does not call this at all. A WAL insert record
           carries the id it was written with, and insert() feeds that id to observe_record_id,
           which only ever raises the counter. So the counter ends strictly above every id in
           use, which is stronger than A40's page-count hint for a reason: a stored number that
           is merely ahead is valid, and one equal to or behind an existing id would reuse it.

        Allocating is a WRITE, so it can refuse -- the pin can evict a dirty page and the device
        speaks there. It refuses before the counter moves, so a refused allocation spends no id.
        """
        extent = self._extent_for(table)
        identity = extent.next_record_id
        # MAX_U64 is the exhausted marker, not the last usable id: handing it out would leave the
        # counter with nowhere to move to, and a counter that cannot advance is a counter that
        # hands the same identity out twice. So the last usable id is one below it.
        if identity >= MAX_U64:
            # Not corruption: nothing is damaged, the table has simply used every identity a
            # 64-bit field can carry. Naming it as damage would send a caller that has inserted
            # 18 quintillion rows into truncation and quarantine (A11-revised).
            raise GrafxUnsupportedOperation(
                f"Table {table.name!r} has allocated every row identity a 64-bit field can "
                f"carry; the counter has reached its exhausted marker {identity}.",
                table=table.name,
                table_id=table.table_id,
                field="next_record_id",
                value=identity,
            )
        self._write_extent(replace(extent, next_record_id=identity + 1))
        return identity

    def observe_record_id(self, table: TableDef, record_id: RecordId) -> bool:
        """Make the counter cover an id that already exists, and say whether it had to move.

        This is the door recovery needs. A replayed insert carries the id it was written with, so
        replaying must not allocate a new one -- but the counter it was allocated from may not
        have reached the device, and a counter behind an id in use would hand that identity out
        again. Raising it here is O(1) per replayed row and needs no scan.

        Idempotent by construction: it only ever moves the counter up, so replaying the same
        record twice is the same as replaying it once, which is what A22 asks of every redo.
        """
        _require_record_id(record_id)
        extent = self._extent_for(table)
        if record_id < extent.next_record_id:
            return False
        if record_id >= MAX_U64:
            raise GrafxUnsupportedOperation(
                f"Row identity {record_id} of table {table.name!r} leaves no room for the next "
                f"one in a 64-bit field.",
                table=table.name,
                table_id=table.table_id,
                field="next_record_id",
                value=record_id,
            )
        self._write_extent(replace(extent, next_record_id=record_id + 1))
        return True

    def next_record_id(self, table: TableDef) -> RecordId:
        """Return the id the next row of this table would take, without spending it."""
        extent = self._find_extent(table.table_id)
        return FIRST_RECORD_ID if extent is None else extent.next_record_id

    def insert(
        self,
        table: TableDef,
        record_id: RecordId,
        values: tuple[Value, ...],
        xmin: Csn,
    ) -> RecordRef:
        """Store the first version of a record and return where it was placed.

        The counter is made to cover this id before the row is written, never after. The order is
        the same rule that governs the row itself: nothing becomes reachable before every step
        that can still refuse has succeeded, and here the step that can refuse is the directory
        write that spends the identity. Written the other way round, a crash between the row and
        the counter leaves an id in use that the counter does not cover, and the next allocation
        hands that identity to a second row -- a reused id, which is the one outcome the counter
        exists to prevent. An id allocated and then abandoned is a gap, which costs nothing.
        """
        _require_commit_number("xmin", xmin)
        _require_record_id(record_id)
        payload = encode_tuple(table, values)
        self.observe_record_id(table, record_id)
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

        An update is two writes, and the second one is not allowed to fail after the first has
        succeeded. Writing the new version and only then reaching for the old one leaves the
        record DOUBLY visible when the second write is refused: two live versions, one of them
        carrying the value the caller was told had not been written, and lookup answering with
        whichever it meets first. A reported failure that changed what a reader sees is worse
        than either outcome the operation could have had.

        So the old page is pinned before the new version exists and stays pinned until the old
        version has been ended. Once that pin is held, nothing between here and the end can take
        it away, and the replacement header is the same length as the one it replaces, so the
        write into it cannot need room it does not have. Everything else that could refuse --
        the commit number, the reference, the page, the tuple against its schema, the version
        being live -- is settled before the first write, the way _store_version settles its own.

        Atomicity across the two pages is not C1's to give: the log covers them and the commit
        applies them together (CONTRACT.md section 8.5). What is C1's to give is that a refusal
        here leaves exactly one live version.

        The ordering here is NOT what buys that, and saying it was is what let the guarantee be
        false for two rounds. This method holds the old page and ends it last; the new version
        is created inside _store_version, and for as long as _store_version could make a version
        REACHABLE and then refuse, the exception left this method before old_page.update_slot
        ever ran -- two live versions, one of them the value the caller was told had not been
        written. The guarantee is bought by _append, which now settles every step that can refuse
        before anything becomes reachable, and this method's part is only to keep the old page
        pinned so that ending it cannot fail afterwards (D1, round 8; A85).

        Proven by test_a_refused_update_leaves_exactly_one_live_version,
        test_a_refused_update_does_not_leave_the_failed_value_readable and
        test_a_device_refusal_inside_the_directory_write_leaves_one_live_version, the last of
        which provokes the refusal from the device rather than from the budget: the budget route
        is the one round 7 closed, and closing it is what made the test that used to provoke it
        stop provoking anything at all (A83.1).
        """
        _require_commit_number("xmin", xmin)
        self._require_bootstrapped()
        if ref.page == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} is the reserved header page and "
                f"holds no record.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                # Masked by _require_table_page, which refuses the same page a moment later
                # because a META page is not a HEAP page. That refusal is about a page whose
                # TYPE is wrong; this one is about the one page index that can never hold a
                # record whatever it says about itself (A2). Only the field tells them apart,
                # and without it the guard could be deleted with the suite still green (A34,
                # A62, D4 round 8).
                field="reserved_header_page",
            )
        # The tuple is encoded before anything is pinned: a row that does not match its schema
        # must not reach a page, and it must not hold a pin while it finds that out.
        payload = encode_tuple(table, tuple(values))
        with self._pool.pinned(self._file, ref.page) as old_page:
            self._require_table_page(old_page, table)
            if ref.slot < FIRST_RECORD_SLOT:
                raise GrafxCorruptionDetected(
                    f"Slot {ref.slot} of page {ref.page} in {self._file!r} is the page "
                    f"descriptor, not a record.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    field="page_descriptor_slot",
                )
            content = old_page.read_slot(ref.slot)
            previous = RecordHeader.decode(content)
            if is_provisional_csn(previous.xmin):
                raise GrafxTransactionStateError(
                    f"Version {ref.page}:{ref.slot} of record {previous.record_id} is an "
                    "abandoned provisional birth and cannot be updated.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    xmin=PROVISIONAL_CSN,
                    field="provisional_birth",
                )
            if not is_open_end_csn(previous.xmax):
                raise GrafxTransactionStateError(
                    f"Version {ref.page}:{ref.slot} of record {previous.record_id} already "
                    f"ended at {previous.xmax} and cannot be updated.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    xmax=previous.xmax,
                    field="already_ended",
                )
            header = RecordHeader(
                record_id=previous.record_id,
                xmin=xmin,
                xmax=0,
                prev_version=ref.encode(),
                payload_len=len(payload),
                schema_version=table.schema_version,
            )
            new_ref = self._store_version(table, header, payload)
            ended = previous.ended_at(xmin, deleted=False)
            old_page.update_slot(
                ref.slot, ended.encode() + content[RECORD_HEADER_SIZE:]
            )
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
        seen: set[int] = visited_pages()
        # A version chain visits distinct slots, so the pages of the file times the slots a page
        # can hold is an upper bound it can never legitimately reach.
        limit = (
            self._pool.storage.page_count(self._file)
            * (self._pool.page_size // SLOT_ENTRY_SIZE)
            + 1
        )
        current: RecordRef | None = ref
        while current is not None:
            refuse_endless_chain(self._file, len(chain) + 1, limit)
            encoded = current.encode()
            if encoded in seen:
                raise GrafxCorruptionDetected(
                    f"The version chain of {self._file!r} returns to page {current.page} slot "
                    f"{current.slot}, so it is a cycle.",
                    file=self._file,
                    page=current.page,
                    slot=current.slot,
                    field="cycle",
                )
            seen.add(encoded)
            chain.append(current)
            _table_id, content = self._read_slot(current)
            current = RecordHeader.decode(content).previous
        return tuple(chain)

    def extent_of(self, table: TableDef) -> TableExtent | None:
        """Return the directory entry of the table, or None when it has no page yet.

        The entry is a hint about the chain, not the chain itself: an append links a new page in
        before it rewrites the entry, so a failure between the two leaves the two disagreeing
        without anything being damaged. The heap repairs the hint on its next append and never
        reads a row through it, so it needs no opinion about a disagreement. A verifier does:
        this is how C6 gets both numbers and decides whether the difference is drift it can
        reconcile or damage it must report.
        """
        return self._find_extent(table.table_id)

    def pages_of(self, table: TableDef) -> tuple[PageIndex, ...]:
        """Return the data pages of the table, in chain order.

        Every hop is checked to be a data page of this table, which is what A40 asks of a walk
        and what the other two walks here already did. Listing pages without asking put the
        reserved header page and pages of other tables into the answer -- and worse, when the
        directory hint had been replayed to agree with a corrupt chain, this walk and extent_of
        agreed with each other, so the drift comparison A33 gives C6 confirmed a damaged table as
        clean while scan_all refused it. A consistency check that certifies damage is worse than
        no check at all.

        Proven by test_listing_the_pages_refuses_a_hop_into_another_tables_page,
        test_listing_the_pages_refuses_a_hop_into_the_reserved_header_page and
        test_the_two_halves_of_the_drift_comparison_do_not_agree_on_a_damaged_chain, the last of
        which is the failure this paragraph is really about (A85).
        """
        extent = self._find_extent(table.table_id)
        if extent is None:
            return ()
        pages: list[PageIndex] = []
        seen: set[PageIndex] = visited_pages()
        limit = self._chain_limit()
        index = extent.first_page
        while index != NO_PAGE:
            self._refuse_endless_chain(table, len(pages) + 1, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                    field="cycle",
                )
            seen.add(index)
            with self._pool.pinned(self._file, index) as page:
                self._require_table_page(page, table)
                following = page.next_page
            pages.append(index)
            index = following
        return tuple(pages)

    # --- internals ---------------------------------------------------------------------------

    def _store_version(
        self, table: TableDef, header: RecordHeader, payload: bytes
    ) -> RecordRef:
        """Place one encoded version, using an overflow chain when it does not fit inline.

        The questions that can be settled before anything is allocated are: the directory has to
        have room for the table, and the page the extent names has to be a usable tail of this
        table. A chain written before those are settled is a chain nobody can reach and, under
        G6, nobody can take back -- once per attempt, for as long as the caller retries.

        Not everything can be settled that early, and claiming it was is what hid D1 for a round.
        The device can still refuse while _append writes the directory entry, because a pin can
        evict a dirty page and the write-back is where the device speaks. What _append guarantees
        instead is the property that actually matters to a caller: no version becomes reachable
        until every remaining step has succeeded, so a refusal leaves nothing to meet twice.
        """
        extent = self._extent_for(table)
        tail, length = self._resolve_tail(table, extent)
        if RECORD_HEADER_SIZE + len(payload) <= self.inline_capacity:
            content = header.encode() + payload
        else:
            chain = write_chain(
                self._pool, self._file, payload, page_type=int(PageType.OVERFLOW)
            )
            overflowed = replace(header, flags=header.flags | RECORD_FLAG_HAS_OVERFLOW)
            content = overflowed.encode() + encode_overflow_pointer(chain[0])
        return self._append(table, extent, tail, length, content)

    def _chain_limit(self) -> int:
        """Return the most hops any chain in this file can take before it must be a cycle.

        A chain visits distinct pages, so it can never be longer than the file. The bound comes
        from the device rather than from the directory entry, so it cannot be stale and it can
        never refuse a walk that is merely drifting (A40); what it guarantees is that a walk
        stops even if the visited set stops working, which is what keeps a broken guard a test
        failure instead of a hung machine.
        """
        return self._pool.storage.page_count(self._file) + 1

    def _refuse_endless_chain(self, table: TableDef, steps: int, limit: int) -> None:
        """Refuse a walk that has taken more steps than the file could possibly justify."""
        if steps > limit:
            raise GrafxCorruptionDetected(
                f"The page chain of table {table.name!r} in {self._file!r} passed {steps} pages "
                f"in a file that holds {limit - 1}, so it does not end.",
                file=self._file,
                table=table.name,
                field="chain_length",
                steps=steps,
            )

    def _resolve_tail(
        self, table: TableDef, extent: TableExtent
    ) -> tuple[PageIndex, int]:
        """Return the last page of the table and the true length of its chain (A40).

        The chain is the truth and the extent is a hint about it. The two disagree without
        anything being damaged: a new page is linked into the chain first and the directory entry
        is rewritten second, so any failure between the two -- and a buffer budget refusal is
        retryable, so a caller is meant to hit it -- leaves the hint behind the chain. An ordinary
        redo of a heap page leaves the same state with no failure at all.

        So the hint is a starting guess and nothing more. The walk begins at first_page, which is
        the only field the chain itself cannot contradict, and follows next_page to the end. That
        answers two questions one walk from the hint cannot: whether last_page is REACHABLE at all
        (an orphan page of the same table passes every per-page check and still takes the append
        out of scan()), and what the chain actually measures. What terminates the walk is the set
        of pages already visited, not a counter: it is bounded by the pages the file has, so it
        needs no bound from the hint -- and a bound from the hint would refuse exactly the drift
        this method exists to tolerate.

        Only a page that cannot be read, is not a heap page, belongs to another table, or is
        revisited is corruption. Everything else about the hint is repaired.
        """
        cached = self._tail_cache.get(table.table_id)
        if cached is not None and self._cache_is_usable(table, extent, cached):
            return cached[0], cached[1]
        self._tail_cache.pop(table.table_id, None)
        index = extent.first_page
        seen: set[PageIndex] = visited_pages()
        limit = self._chain_limit()
        length = 0
        while True:
            self._refuse_endless_chain(table, length + 1, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} in {self._file!r} returns to page "
                    f"{index}.",
                    file=self._file,
                    page=index,
                    table=table.name,
                    field="cycle",
                )
            seen.add(index)
            length += 1
            with self._pool.pinned(self._file, index) as page:
                self._require_table_page(page, table)
                following = page.next_page
            if following == NO_PAGE:
                self._tail_cache[table.table_id] = (
                    index,
                    length,
                    self._pool.derived_epoch(self._file),
                )
                return index, length
            index = following

    def _cache_is_usable(
        self, table: TableDef, extent: TableExtent, cached: tuple[PageIndex, int, int]
    ) -> bool:
        """Return True when the remembered tail can still be trusted without walking to it.

        A remembered tail is a claim about two things: that the page is a tail page of this
        table, and that the chain from first_page still reaches it. The first is a property of
        the page and can simply be re-read. The second is a property of no page at all -- a redo
        that shortens a chain leaves the old tail entirely intact -- so it is carried by the pool
        epoch instead: the walk recorded the epoch at which it established reachability, and
        every door that could have relinked pages of this file has bumped it since.

        The first page is re-checked as well. Skipping it lets a warm cache serve appends for a
        table whose chain now starts at the reserved header page or inside another table, and
        those are precisely the refusals the walk exists to make. An optimisation that answers
        where the authority would refuse is not an optimisation.

        Only GrafxCorruptionDetected means "this cache entry no longer describes the file". A
        wholesale except GrafxError also swallowed the RETRYABLE conditions the device raises --
        a full volume, a sharing violation, an exhausted budget -- and turned each of them into a
        silent re-walk that then met the same condition with a pin already spent. A caller that
        should have been told to retry was told nothing at all (D8, round 8).
        """
        tail, length, epoch = cached
        if epoch != self._pool.derived_epoch(self._file):
            return False
        try:
            with self._pool.pinned(self._file, extent.first_page) as page:
                self._require_table_page(page, table)
            with self._pool.pinned(self._file, tail) as page:
                self._require_table_page(page, table)
                return page.next_page == NO_PAGE
        except GrafxCorruptionDetected:
            return False

    def _append(
        self,
        table: TableDef,
        extent: TableExtent,
        tail: PageIndex,
        length: int,
        content: bytes,
    ) -> RecordRef:
        """Append one slot to the tail page of the table, growing the chain when it is full.

        The tail arrives resolved and checked, because the caller has to settle every refusal
        before it writes an overflow chain. What goes back into the directory is the length the
        walk counted, never the stored count plus the distance travelled: the stored count
        describes wherever the hint happened to be, so adding to it is permanently wrong whenever
        the hint was not where the count said. A count that is never right is worse than no count,
        because the next append walks zero hops and never revisits it (A40.2).

        The frame cost of this method is proven by
        test_the_extent_hint_is_written_with_the_tail_released and its consequence by
        test_a_retryable_refusal_never_multiplies_a_row, which reaches the three-frame shape the
        way a caller does: an update whose old version is not on the tail, over a hint an
        ordinary redo has left behind its chain (A85).
        """
        # THE RULE: no version may become reachable before every step that can still refuse has
        # succeeded. Not "before every step that can refuse for the reason we last fixed" -- the
        # window is defined by what a caller is told, not by which door produced the refusal.
        # Round 7 moved the directory write out of the tail pin, which closed the BUDGET route
        # into this window and left the window itself open: _write_extent pins page 0, an
        # ordinary eviction write-back inside that pin reaches the device, and a device that
        # answers GrafxDeviceFull or GrafxStorageError answers RETRYABLE. The caller is told to
        # try again while the row it was told about is already in the chain, so the retry writes
        # it a second time and scan() yields the record twice (D1, round 8).
        #
        # The hint is settleable first and the row is not. A40 already tolerates a last_page
        # AHEAD of its chain and repairs it: the next append walks from first_page, finds the
        # real end and rewrites the count. A row ahead of its directory entry has no such
        # repair, because the caller was told the write did not happen.
        fits = False
        with self._pool.pinned(self._file, tail) as page:
            fits = page.can_fit(len(content))
        if fits:
            if extent.last_page != tail or extent.page_count != length:
                # The length the walk counted, never the stored count plus the distance
                # travelled: adding to a count that describes wherever the hint happened to be
                # is permanently wrong the moment the hint was not where the count said (A40.2).
                self._write_extent(replace(extent, last_page=tail, page_count=length))
            # Re-taken with the hint settled. This pin can still refuse -- it can evict a dirty
            # page, and the write-back is where the device speaks -- and it refuses with nothing
            # of this row on any page. Once it is held, insert_slot touches only the pinned
            # frame, so the row cannot half-arrive.
            with self._pool.pinned(self._file, tail) as page:
                return RecordRef(page=tail, slot=page.insert_slot(content))
        fresh = self._pool.allocate(self._file, int(PageType.HEAP))
        new_index = fresh.page_index
        try:
            self._initialize_data_page(fresh, table.table_id)
            slot = fresh.insert_slot(content)
        finally:
            self._pool.unpin(self._file, new_index, dirty=True)
        # Written, and pointed at by nothing: the relink below is what makes it reachable. So the
        # hint is settled while the row is still invisible. A refusal here leaves an unreferenced
        # page, which G6 forbids reclaiming and which every walk skips because every walk starts
        # at first_page -- a leak, and not a row a reader can meet twice.
        self._write_extent(replace(extent, last_page=new_index, page_count=length + 1))
        with self._pool.pinned(self._file, tail) as page:
            page.next_page = new_index
        self._tail_cache[table.table_id] = (
            new_index,
            length + 1,
            self._pool.derived_epoch(self._file),
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
        # The directory is settled before the device is asked to grow. The other order leaves a
        # page behind on every refusal at the table bound, and no sanctioned operation can take
        # it back (G6), which is the same reasoning BufferPool.allocate states for itself.
        self._require_directory_room(table)
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

    def _require_directory_room(self, table: TableDef) -> None:
        """Refuse a new table before anything is allocated when the directory is full."""
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            if not header_page.can_fit(DIRECTORY_ENTRY_SIZE):
                raise self._directory_full(table)

    def _directory_full(self, table: TableDef) -> GrafxUnsupportedOperation:
        """Build the refusal for a header page that cannot name one more table.

        The directory lives on the single reserved header page, so the number of tables one heap
        file can hold is bounded by the page size. Reaching that bound is a real limit of the
        format rather than damage, and the caller is told so by name.
        """
        return GrafxUnsupportedOperation(
            f"The header page of {self._file!r} is full, so it cannot hold the directory "
            f"entry of table {table.name!r}; this heap file is limited to "
            f"{self.max_tables} tables.",
            file=self._file,
            table=table.name,
            max_tables=self.max_tables,
        )

    def _add_directory_entry(
        self, header_page: Page, table: TableDef, extent: TableExtent
    ) -> None:
        """Add one table to the directory, never over the slot that holds the file header."""
        # Two structural facts, neither of them checked here because no input can reach the
        # check. Slot ids are handed out in order and never reused, and the header page always
        # carries its file header in slot 0, so an extent cannot land in the slot the readers
        # skip. And the caller has already settled the room with the identical can_fit predicate
        # this insert uses, on a page that never accumulates compactable gaps, so PageFullError
        # cannot come back from here either.
        header_page.insert_slot(extent.encode())

    def _find_extent(self, table_id: int) -> TableExtent | None:
        """Return the directory entry of the table, or None when the table has no page yet."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            for slot, payload in header_page.iter_slots():
                if slot < EXTENT_FIRST_SLOT:
                    continue
                extent = TableExtent.decode(payload)
                if extent.table_id == table_id:
                    return self._require_first_page(extent)
        return None

    def _require_first_page(self, extent: TableExtent) -> TableExtent:
        """Refuse a directory entry whose chain would end before it starts.

        Both walks stop at NO_PAGE, so an entry that names it as the FIRST page reads as an empty
        table: scan, scan_all, lookup and pages_of all answer nothing, and they answer it without
        raising, while the rows sit on pages the walk never reached. scan_all is what C6 verifies
        against, and the drift check agrees with a page count of zero, so the verifier reports a
        clean, empty table over a table that is not empty.

        A table gets its directory entry only after its first page has been allocated, so this
        value is never a state the heap can be in -- it is four damaged bytes, exactly as
        CatalogStore says of a chain root that names the reserved page, and it is refused here
        for the same reason.
        """
        if extent.first_page == NO_PAGE:
            raise GrafxCorruptionDetected(
                f"Table {extent.table_id} in {self._file!r} names no first page, so its chain "
                f"would end before it starts.",
                file=self._file,
                table_id=extent.table_id,
                field="first_page",
                value=extent.first_page,
            )
        if extent.first_page == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"Table {extent.table_id} in {self._file!r} starts its chain at page "
                f"{HEADER_PAGE_INDEX}, which is the reserved header page.",
                file=self._file,
                table_id=extent.table_id,
                field="first_page",
                value=extent.first_page,
            )
        return extent

    def _write_extent(self, extent: TableExtent) -> None:
        """Replace the directory entry of a table on the header page."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as header_page:
            for slot, payload in header_page.iter_slots():
                if slot < EXTENT_FIRST_SLOT:
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
                # Masked by _require_table_page, which refuses the same page a moment later
                # because a META page is not a HEAP page. That refusal is about a page whose
                # TYPE is wrong; this one is about the one page index that can never hold a
                # record whatever it says about itself (A2). Only the field tells them apart,
                # and without it the guard could be deleted with the suite still green (A34,
                # A62, D4 round 8).
                field="reserved_header_page",
            )
        with self._pool.pinned(self._file, ref.page) as page:
            self._require_data_page(page)
            table_id = self._page_table_id(page)
            if ref.slot < FIRST_RECORD_SLOT:
                raise GrafxCorruptionDetected(
                    f"Slot {ref.slot} of page {ref.page} in {self._file!r} is the page "
                    f"descriptor, not a record.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    field="page_descriptor_slot",
                )
            return int(table_id), page.read_slot(ref.slot)

    def _page_table_id(self, page: Page) -> int:
        """Return the table this data page belongs to, from the descriptor in its slot 0.

        The descriptor is four bytes that came off a page, so its width is checked before it is
        unpacked. A22 lets recovery install any checksum-valid image the log carried, and nothing
        in that image constrains how wide slot 0 is: unpacking it blind throws a raw struct.error
        out of read(), scan() and insert(), and a raw struct.error carries no code, no retry flag
        and no location, so C6 cannot classify it and no ledger entry is possible. A41 required
        exactly this check on the encode side; the decode side is the same invariant.
        """
        descriptor = page.read_slot(DESCRIPTOR_SLOT)
        if len(descriptor) != DESCRIPTOR_SIZE:
            raise GrafxCorruptionDetected(
                f"The page descriptor of page {page.page_index} in {self._file!r} is "
                f"{len(descriptor)} bytes; a heap data page carries {DESCRIPTOR_SIZE}.",
                file=self._file,
                page=page.page_index,
                field="page_descriptor",
                value=len(descriptor),
            )
        return int(_DESCRIPTOR.unpack(descriptor)[0])

    def _require_table_page(self, page: Page, table: TableDef) -> None:
        """Refuse a page that is not a data page of this table.

        Every door into a page, read or write, asks this question. A page that answers for the
        wrong table is not a smaller problem than a page that cannot be parsed: it is the one
        that produces a wrong answer instead of an error.
        """
        self._require_data_page(page)
        owner = self._page_table_id(page)
        if owner != table.table_id:
            raise GrafxCorruptionDetected(
                f"Page {page.page_index} of {self._file!r} belongs to table {owner}, not to "
                f"{table.name!r} with id {table.table_id}.",
                file=self._file,
                page=page.page_index,
                table=table.name,
                table_id=owner,
            )

    def _require_data_page(self, page: Page) -> None:
        """Refuse a page that is not a heap data page, saying which of the two refusals it is.

        The settlement of D6 (round 8). Two halves of C1 met one state -- a chain hop into a page
        that has been ALLOCATED and never written -- and answered it oppositely: the pool called
        it free and unremarkable (is_unwritten_image argues at length that a page of zeros is not
        damaged bytes, and it is right: nothing checksummed wrong, nothing was overwritten), the
        heap called it corruption_detected, which is the code FR-8 and FR-10 route to truncation
        and quarantine.

        Which half is right: BOTH, about different questions, and the heap was wrong to answer
        with the same code for both. The bytes are not damaged, so the pool must not raise. The
        chain is not readable, so the heap must not walk on -- it cannot invent the rows of a
        page nobody wrote, and A19 asks scan_all for the unfiltered truth, not for a guess.

        What was actually wrong is that C6 could not tell the two apart. A page that was written
        and says the wrong thing needs a verifier; a page that was never written needs the redo
        that was already coming for it -- the crash between the PAGE_ALLOC record and the
        WRITE_PAGE record is the ordinary shape here, and the log covers it. So the refusal now
        carries the distinction: field="unwritten_page" for the second, and the ordinary type
        refusal for the first.
        """
        if page.page_type == int(PageType.FREE) and page.slot_count == 0:
            raise GrafxCorruptionDetected(
                f"Page {page.page_index} of {self._file!r} is reachable from a chain but has "
                f"never been written, which is the state a crash between allocating a page and "
                f"writing it leaves; replaying the log covers it.",
                file=self._file,
                page=page.page_index,
                page_type=page.page_type,
                field="unwritten_page",
            )
        if page.page_type != int(PageType.HEAP):
            raise GrafxCorruptionDetected(
                f"Page {page.page_index} of {self._file!r} is of type {page.page_type} and is "
                f"not a heap data page.",
                file=self._file,
                page=page.page_index,
                page_type=page.page_type,
                field="page_type",
            )
        if page.slot_count < 1 or page.is_slot_free(DESCRIPTOR_SLOT):
            # The third page state, and the one C6 asked to be able to route. Its two neighbours
            # above already say which they are; this one said nothing, so a verifier could not
            # tell it from them and had to fall back on guessing. It is WRITTEN -- the page type
            # is HEAP, so something wrote it -- and structurally incomplete, which means a
            # verifier and a quarantine, never a redo: replaying cannot invent a descriptor that
            # was never logged.
            #
            # Both spellings are the same state. A page whose descriptor slot was FREED is not a
            # page with a different problem from one that never had a slot 0 at all: neither can
            # say which table it belongs to, and that is the whole content of the finding. This
            # is also the boundary where provenance is known: Page refuses a freed slot as an
            # ordinary state because a slot id is an argument, but slot 0 of a heap data page is
            # a structural slot THIS component wrote, so here the same state is damage (A66.1).
            raise GrafxCorruptionDetected(
                f"Heap page {page.page_index} of {self._file!r} carries no page descriptor in "
                f"slot {DESCRIPTOR_SLOT}, so it cannot say which table it belongs to.",
                file=self._file,
                page=page.page_index,
                page_type=page.page_type,
                slot_count=page.slot_count,
                field="missing_page_descriptor",
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
        # A store that has just refused every read of this file may not go on writing to it.
        self._require_bootstrapped()
        # The same door insert() and update() use. A caller that passes a number no snapshot
        # could see has made a caller error, and it must not come back as corruption_detected:
        # FR-8 and FR-10 turn that code into truncation, quarantine and a forensic ledger entry,
        # so misclassifying it manufactures an integrity incident out of a typo (A11-revised).
        _require_commit_number("xmax", xmax)
        if ref.page == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} is the reserved header page and "
                f"holds no record.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                # Masked by _require_table_page, which refuses the same page a moment later
                # because a META page is not a HEAP page. That refusal is about a page whose
                # TYPE is wrong; this one is about the one page index that can never hold a
                # record whatever it says about itself (A2). Only the field tells them apart,
                # and without it the guard could be deleted with the suite still green (A34,
                # A62, D4 round 8).
                field="reserved_header_page",
            )
        with self._pool.pinned(self._file, ref.page) as page:
            self._require_table_page(page, table)
            if ref.slot < FIRST_RECORD_SLOT:
                # Masked by the record header decoder, which refuses the same read because four
                # bytes are not forty. Only this one says what is actually wrong with it (A62).
                raise GrafxCorruptionDetected(
                    f"Slot {ref.slot} of page {ref.page} in {self._file!r} is the page "
                    f"descriptor, not a record.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    field="page_descriptor_slot",
                )
            content = page.read_slot(ref.slot)
            header = RecordHeader.decode(content)
            if is_provisional_csn(header.xmin):
                raise GrafxTransactionStateError(
                    f"Version {ref.page}:{ref.slot} of record {header.record_id} is an "
                    "abandoned provisional birth and cannot be ended.",
                    file=self._file,
                    page=ref.page,
                    slot=ref.slot,
                    xmin=PROVISIONAL_CSN,
                    field="provisional_birth",
                )
            if not is_open_end_csn(header.xmax):
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

    def _walk(self, table: TableDef) -> Iterator[tuple[RecordRef, RecordHeader, bytes]]:
        """Yield every stored version of the table with its location and its raw content.

        One page at a time is pinned and its slots are copied out before anything is decoded, so
        following an overflow chain never needs a second frame while a data page is still held.
        """
        extent = self._find_extent(table.table_id)
        if extent is None:
            return
        index = extent.first_page
        seen: set[PageIndex] = visited_pages()
        limit = self._chain_limit()
        steps = 0
        while index != NO_PAGE:
            steps += 1
            self._refuse_endless_chain(table, steps, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                    field="cycle",
                )
            seen.add(index)
            with self._pool.pinned(self._file, index) as page:
                self._require_table_page(page, table)
                items = tuple(
                    (slot, payload)
                    for slot, payload in page.iter_slots()
                    if slot >= FIRST_RECORD_SLOT
                )
                following = page.next_page
            for slot, content in items:
                yield (
                    RecordRef(page=index, slot=slot),
                    RecordHeader.decode(content),
                    content,
                )
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
