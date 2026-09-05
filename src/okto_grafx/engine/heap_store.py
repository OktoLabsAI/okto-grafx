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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    NO_CSN,
    NO_LSN,
    NO_PAGE,
    PROVISIONAL_CSN,
    Csn,
    Lsn,
    PageIndex,
    RecordId,
    RecordRef,
    SlotId,
    is_committed_csn,
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
    decode_relationship_endpoints,
    decode_tuple,
    encode_tuple,
)
from okto_grafx.domain.model.catalog import HEAP_RECLAIM_V1_CAPABILITY
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
    "RecordIdFloorAdvance",
    "RecordIdFloorPlan",
    "HeapReclaimFloorPlan",
    "HeapVacuumPlan",
    "HeapVacuumTablePlan",
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


def _require_record_id_floor(value: RecordId) -> RecordId:
    """Return a requested exclusive identity floor after validating the v1 field width."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"A record identity floor must be an integer; got a {type(value).__name__}.",
            field="next_record_id",
            value=repr(value),
        )
    if not FIRST_RECORD_ID <= value <= MAX_U64:
        raise GrafxConfigurationError(
            f"A record identity floor must be between {FIRST_RECORD_ID} and {MAX_U64}; "
            f"got {value}.",
            field="next_record_id",
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
class _HeapScanPosition:
    """Internal physical continuation for one bounded heap scan.

    The public API wraps this value in an opaque transaction-owned token.  Keeping the physical
    location here lets a page resume without sorting, replaying earlier pages or retaining a
    generator (and therefore engine capabilities) across calls.  ``chain_limit`` is captured at
    the first page.  It both bounds a corrupt cycle across calls and fixes the physical end of
    this scan: pages appended by a writer after the first result cannot contain a version visible
    to the reader's older snapshot, so the continuation must not follow them.
    """

    page: PageIndex
    slot: SlotId
    pages_walked: int
    chain_limit: int


class _VisibleRecordCursor:
    """Resume one canonical, snapshot-filtered header walk without retaining a page pin.

    The cursor is deliberately narrower than :meth:`HeapStore.scan_page`: endpoint validation
    needs only the identity and physical reference of rows the snapshot can see.  Payloads stay
    on the heap until the one requested candidate is revalidated and fully decoded.  One page is
    inspected into a compact tuple before its pin is released, so pausing the cursor never keeps
    a frame resident by capability.

    ``admit_page`` lets the query engine charge the growing visited-page proof before it grows.
    Keeping that proof is not optional: it preserves the canonical walk's immediate cycle
    refusal instead of replacing it with a later chain-length failure.
    """

    __slots__ = (
        "_store",
        "_table",
        "_snapshot",
        "_admit_page",
        "_next_page",
        "_pending",
        "_pending_at",
        "_seen",
        "_limit",
        "_steps",
        "_closed",
    )

    def __init__(
        self,
        store: HeapStore,
        table: TableDef,
        snapshot: SnapshotLike,
        admit_page: Callable[[], None],
    ) -> None:
        """Capture the physical end and first page of this canonical walk."""
        extent = store._find_extent(table.table_id)
        self._store = store
        self._table = table
        self._snapshot = snapshot
        self._admit_page = admit_page
        self._next_page = NO_PAGE if extent is None else extent.first_page
        self._pending: tuple[tuple[RecordRef, RecordId], ...] = ()
        self._pending_at = 0
        self._seen: set[PageIndex] = visited_pages()
        self._limit = store._chain_limit()
        self._steps = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether this cursor has released all of its derived state."""
        return self._closed

    def close(self) -> None:
        """Release every retained header/reference and make the cursor terminal."""
        self._pending = ()
        self._pending_at = 0
        self._seen.clear()
        self._next_page = NO_PAGE
        self._closed = True

    def next_visible(self) -> tuple[RecordRef, RecordId] | None:
        """Return the next visible identity in storage order, or None at the captured end."""
        if self._closed:
            return None
        while True:
            if self._pending_at < len(self._pending):
                item = self._pending[self._pending_at]
                self._pending_at += 1
                return item
            self._pending = ()
            self._pending_at = 0
            index = self._next_page
            if index == NO_PAGE:
                self.close()
                return None

            self._steps += 1
            self._store._refuse_endless_chain(self._table, self._steps, self._limit)
            if index in self._seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {self._table.name!r} returns to page {index}.",
                    file=self._store._file,
                    page=index,
                    field="cycle",
                )
            # Admission precedes the set growth.  A caller that refuses it can close this cursor
            # and fall back to an ordinary canonical lookup without one unaccounted page entry.
            self._admit_page()
            self._seen.add(index)
            selected: list[tuple[RecordRef, RecordId]] = []
            with self._store._pool.pinned(self._store._file, index) as page:
                self._store._require_table_page(page, self._table)
                for slot in page.live_slots():
                    if slot < FIRST_RECORD_SLOT:
                        continue
                    fields = RecordHeader.peek(page.slot_view(slot))
                    (
                        _flags,
                        _reserved,
                        _schema_version,
                        _payload_len,
                        record_id,
                        xmin,
                        xmax,
                        _previous,
                    ) = fields
                    if self._snapshot.visible(xmin, xmax):
                        selected.append((RecordRef(page=index, slot=slot), record_id))
                following = page.next_page
            if following != NO_PAGE and following >= self._limit - 1:
                current_page_ceiling = self._store._chain_limit() - 1
                if following >= current_page_ceiling:
                    raise GrafxCorruptionDetected(
                        f"The page chain of table {self._table.name!r} in "
                        f"{self._store._file!r} points to page {following}, outside a file "
                        f"with {current_page_ceiling} pages.",
                        file=self._store._file,
                        table=self._table.name,
                        page=following,
                        field="next_page",
                        page_count=current_page_ceiling,
                    )
                # A concurrently appended page cannot contain a version visible to this older
                # snapshot. Validate its type/owner, then keep the physical horizon captured at
                # cursor creation instead of drifting into an unbounded stream of new pages.
                with self._store._pool.pinned(self._store._file, following) as appended:
                    self._store._require_table_page(appended, self._table)
                following = NO_PAGE
            self._next_page = following
            self._pending = tuple(selected)


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
    def decode(cls, raw: bytes | memoryview) -> TableExtent:
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


@dataclass(frozen=True, slots=True)
class _ExtentAuthority:
    """Frozen reservation authority carried by one opaque commit-local proof."""

    owner: object
    seal: object
    table_id: int
    first_page: PageIndex
    durable_floor: RecordId


@dataclass(slots=True)
class _ExtentCursor:
    """Mutable physical hint whose authority remains in its frozen sibling."""

    extent: TableExtent
    derived_epoch: int


@dataclass(frozen=True, slots=True)
class _ExtentProof:
    """One sealed reservation authority plus its revocable settled-tail cursor."""

    authority: _ExtentAuthority
    cursor: _ExtentCursor


@dataclass(frozen=True, slots=True)
class RecordIdFloorAdvance:
    """One table's durable identity floor before and after a staged reservation."""

    table_id: int
    old_floor: RecordId
    new_floor: RecordId


@dataclass(frozen=True, slots=True)
class RecordIdFloorPlan:
    """The detached page-zero image and the identity ranges it reserves.

    ``advances`` is ordered by ``table_id``.  Each item reserves the half-open range
    ``[old_floor, new_floor)`` once ``image`` has been committed through the ordinary page-image
    WAL path.  Constructing this value changes no resident page.
    """

    page_index: PageIndex
    image: bytes
    advances: tuple[RecordIdFloorAdvance, ...]


@dataclass(frozen=True, slots=True)
class HeapReclaimFloorPlan:
    """Detached heap page-zero image advancing the global retained-snapshot floor."""

    page_index: PageIndex
    image: bytes
    old_floor: Lsn
    new_floor: Lsn


@dataclass(frozen=True, slots=True)
class HeapVacuumTablePlan:
    """Deterministic physical effects selected for one table."""

    table_id: int
    pages_scanned: int
    eligible_inline_versions: int
    reclaimed_versions: int
    reclaimed_slot_bytes: int
    relinked_versions: int
    skipped_overflow_versions: int


@dataclass(frozen=True, slots=True)
class HeapVacuumPlan:
    """Detached data-page images and accounting for one bounded heap reclaim pass."""

    horizon_lsn: Lsn
    page_images: tuple[tuple[PageIndex, bytes], ...]
    tables: tuple[HeapVacuumTablePlan, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class _HeapBloatSample:
    """Header-only physical counts for one table at one conservative horizon."""

    table_id: int
    data_pages: int
    slot_directory_entries: int
    free_slots: int
    stored_versions: int
    ended_versions: int
    horizon_eligible_versions: int
    horizon_retained_versions: int
    horizon_eligible_slot_bytes: int
    horizon_retained_slot_bytes: int
    overflow_versions: int
    horizon_eligible_overflow_versions: int


class HeapStore:
    """Insert, update, delete, read and scan record versions over a paged heap file."""

    __slots__ = (
        "_pool",
        "_catalog",
        "_file",
        "_tail_cache",
        "_extent_slots",
        "_extent_slots_epoch",
        "_bootstrapped_epoch",
        "_extent_proof_seal",
    )

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
        # Directory slots are stable within one header image.  Remembering the slot removes
        # the three page-zero directory walks an inserted row otherwise pays (find, identity
        # update and tail-hint update).  The entry remains only a hint: every use reads the
        # current slot view and verifies its table-id prefix before decoding it.
        self._extent_slots: dict[int, SlotId] = {}
        self._extent_slots_epoch: tuple[int | None, int] | None = None
        # Internal operations may reuse a successful header proof only while the exact page
        # view it proved remains current. The public predicate never trusts this memo.
        self._bootstrapped_epoch: int | None = None
        self._extent_proof_seal = object()
        # TransactionManager and recovery use the shared page-image door directly. Registering
        # the heap format classifier here lets those paths make the same structural distinction
        # as HeapStore.apply_page_image without teaching either component the heap layout.
        self._pool._register_structure_signature(
            self._file, HeapStore._page_structure_signature
        )

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
            self._set_bootstrapped_epoch(None)
            return False
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            if page.is_pristine():
                self._set_bootstrapped_epoch(None)
                return False
            self._require_header_page(page)
        self._set_bootstrapped_epoch(self._pool.derived_epoch(self._file))
        return True

    def _set_bootstrapped_epoch(self, epoch: int | None) -> None:
        """Move the header proof and invalidate hints derived under another proof."""
        if self._bootstrapped_epoch != epoch:
            self._invalidate_extent_slots()
        self._bootstrapped_epoch = epoch

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

        The heap supplies its structural signature to the shared redo door.  Page type, flags,
        chain links, data-page ownership and page-zero directory authority move the structure
        epoch; record payload, MVCC fields, durable hints and LSN/sequence stamps do not.  This
        keeps a same-handle commit from discarding the tail, extent-slot and locator work it just
        established while retaining the conservative epoch boundary for every relink or change
        of ownership.
        """
        before = (
            self._pool.structure_epoch(self._file),
            self._pool.cache_drop_epoch(self._file),
        )
        applied = apply_page_image(
            self._pool,
            self._file,
            page_index,
            image,
            structure_signature=self._page_structure_signature,
        )
        after = (
            self._pool.structure_epoch(self._file),
            self._pool.cache_drop_epoch(self._file),
        )
        if applied and before != after:
            # Structural images and concurrent cache/view drops revoke the slot proof eagerly.
            # Pure content images preserve it; its epoch is still revalidated on every use.
            self._invalidate_extent_slots()
        return applied

    @staticmethod
    def _page_structure_signature(page: Page) -> object:
        """Return only the heap page fields that authorize derived physical locations.

        Data-page rows are intentionally absent.  A record append or MVCC stamp cannot change
        the chain or page owner, and an older snapshot cannot observe a version appended after
        its horizon.  Reclamation likewise cannot remove a version admitted by a live snapshot.
        The record slot directory is therefore content for this purpose, while slot zero is the
        page-owner authority and remains structural.

        Page zero is narrower than a byte-for-byte signature.  The file header, directory slot
        topology, table id and first page are authority.  ``last_page``, ``page_count`` and
        ``next_record_id`` are durable hints/counters updated by ordinary DML and are verified or
        repaired by their existing readers.  A malformed entry is represented by its complete
        payload, making the classifier conservative without moving validation out of the
        canonical heap doors.
        """

        common = (
            page.page_type,
            page.flags,
            page.header().reserved,
            page.next_page,
        )
        if page.page_type == int(PageType.HEAP):
            return (*common, "heap", HeapStore._slot_authority(page, DESCRIPTOR_SLOT))
        if page.page_type != int(PageType.META) or page.page_index != HEADER_PAGE_INDEX:
            return common

        entries: list[object] = []
        for slot in range(EXTENT_FIRST_SLOT, page.slot_count):
            if page.is_slot_free(slot):
                entries.append((slot, "free"))
                continue
            payload = page.slot_view(slot)
            if len(payload) != DIRECTORY_ENTRY_SIZE:
                entries.append((slot, "malformed", bytes(payload)))
                continue
            table_id, first_page = struct.unpack_from("<II", payload)
            entries.append((slot, "extent", table_id, first_page))
        return (
            *common,
            "meta",
            page.slot_count,
            HeapStore._slot_authority(page, 0),
            tuple(entries),
        )

    @staticmethod
    def _slot_authority(page: Page, slot: SlotId) -> object:
        """Return a total signature for one authoritative slot, including absence/free state."""
        if slot >= page.slot_count:
            return ("missing",)
        if page.is_slot_free(slot):
            return ("free",)
        return ("value", bytes(page.slot_view(slot)))

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

    def plan_record_id_floors(
        self, floors: Mapping[TableDef, RecordId]
    ) -> RecordIdFloorPlan:
        """Plan atomic durable reservations on a detached image of heap page 0.

        ``floors`` maps each existing table to the exclusive upper bound of the range being
        reserved.  One call may advance several extents because every extent lives on the same
        physical page: returning one image lets the caller commit all of those advances through
        one ordinary ``WRITE_PAGE`` plus ``COMMIT`` pair.  The half-open range granted to a table
        is reported as ``[old_floor, new_floor)``.

        This is deliberately a copy-on-write door.  It reads one checksum-verified, detached
        header page directly from the device, validates the complete request, and changes only
        that private copy.  Bypassing the resident frame matters when another process has just
        published a newer floor.  A caller that loses OCC or abandons the plan discards the
        value; neither the live frame nor the durable counter moved.  Missing extents are refused
        rather than created, because first use still belongs to the legacy insert/allocation path
        and may need a new data page as well as a directory slot.
        """
        if not isinstance(floors, Mapping):
            raise GrafxConfigurationError(
                f"Identity floors must be supplied as a mapping; got {type(floors).__name__}.",
                field="identity_floors",
                value=type(floors).__name__,
            )
        requested = tuple(floors.items())
        if not requested:
            raise GrafxConfigurationError(
                "At least one table is required to plan an identity-floor advance.",
                field="identity_floors",
                value=0,
            )
        table_ids: set[int] = set()
        validated: list[tuple[TableDef, RecordId]] = []
        for table, floor in requested:
            if not isinstance(table, TableDef):
                raise GrafxConfigurationError(
                    f"An identity-floor key must be a TableDef; got {type(table).__name__}.",
                    field="identity_floors",
                    value=type(table).__name__,
                )
            if table.table_id in table_ids:
                raise GrafxConfigurationError(
                    f"Identity floors name table id {table.table_id} more than once.",
                    field="table_id",
                    value=table.table_id,
                )
            table_ids.add(table.table_id)
            validated.append((table, _require_record_id_floor(floor)))
        validated.sort(key=lambda item: item[0].table_id)

        self._require_bootstrapped()
        # A resident clean frame can precede another participant's just-published image, so the
        # reservation must be based on the device image and must not replace or mutate that
        # resident object.  The caller stages this detached image under the global commit
        # ordering; if the device changes after this read, page-zero interest makes the caller
        # rebuild the reservation from the new durable image.
        image = self._pool.read_fresh_page(self._file, HEADER_PAGE_INDEX)
        self._require_header_page(image)
        located: dict[int, tuple[SlotId, TableExtent]] = {}
        for slot, payload in image.iter_slots():
            if slot < EXTENT_FIRST_SLOT:
                continue
            extent = TableExtent.decode(payload)
            if extent.table_id not in table_ids:
                continue
            if extent.table_id in located:
                raise GrafxCorruptionDetected(
                    f"Table {extent.table_id} has more than one directory entry on the "
                    f"header page of {self._file!r}.",
                    file=self._file,
                    page=HEADER_PAGE_INDEX,
                    table_id=extent.table_id,
                    field="directory_entry",
                )
            located[extent.table_id] = (slot, self._require_first_page(extent))

        advances: list[RecordIdFloorAdvance] = []
        for table, new_floor in validated:
            found = located.get(table.table_id)
            if found is None:
                raise GrafxTransactionStateError(
                    f"Table {table.name!r} has no heap extent, so an identity range cannot "
                    "be reserved for it yet.",
                    file=self._file,
                    table=table.name,
                    table_id=table.table_id,
                    field="table_extent",
                )
            old_floor = found[1].next_record_id
            if new_floor <= old_floor:
                raise GrafxTransactionStateError(
                    f"Table {table.name!r} already has durable identity floor {old_floor}; "
                    f"a reservation must advance it, not request {new_floor}.",
                    file=self._file,
                    table=table.name,
                    table_id=table.table_id,
                    field="next_record_id",
                    old_floor=old_floor,
                    value=new_floor,
                )
            advances.append(
                RecordIdFloorAdvance(
                    table_id=table.table_id,
                    old_floor=old_floor,
                    new_floor=new_floor,
                )
            )

        # Only after every extent and floor passed validation does the detached page change.  No
        # partial image can escape if a later table is absent or its requested floor is stale.
        for advance in advances:
            slot, extent = located[advance.table_id]
            image.update_slot(
                slot,
                replace(extent, next_record_id=advance.new_floor).encode(),
            )
        return RecordIdFloorPlan(
            page_index=HEADER_PAGE_INDEX,
            image=self._pool.codec.encode_page(image),
            advances=tuple(advances),
        )

    def reclaim_floor(self) -> Lsn:
        """Return and validate the global minimum snapshot retained by this heap.

        Heap files predating vacuum use ``(root_page=NO_PAGE, payload_length=0)``.  Vacuum v1
        reuses these otherwise-unused heap-header fields as ``(HEADER_PAGE_INDEX, floor_lsn)``;
        the catalog capability makes that interpretation unambiguous across binary versions.
        """

        with self._pinned_validated_header() as (_page, header):
            return self._decode_reclaim_floor(header)

    def plan_reclaim_floor(self, floor: Lsn) -> HeapReclaimFloorPlan:
        """Plan a monotonic durable floor advance without changing resident or durable pages."""

        if (
            isinstance(floor, bool)
            or not isinstance(floor, int)
            or not 1 <= floor < PROVISIONAL_CSN
        ):
            raise GrafxConfigurationError(
                "A heap reclaim floor must be a positive committed LSN.",
                field="reclaim_floor_lsn",
                value=repr(floor),
            )
        capabilities = self._catalog.catalog.required_capabilities()
        if HEAP_RECLAIM_V1_CAPABILITY not in capabilities:
            raise GrafxSchemaVersionMismatch(
                "A heap reclaim floor requires catalog capability heap_reclaim_v1 to be "
                "published first.",
                field="required_capabilities",
                value=HEAP_RECLAIM_V1_CAPABILITY,
            )

        self._require_bootstrapped()
        page = self._pool.read_fresh_page(self._file, HEADER_PAGE_INDEX)
        header = self._require_header_page(page)
        old_floor = self._decode_reclaim_floor(header)
        if floor < old_floor:
            raise GrafxTransactionStateError(
                f"The heap reclaim floor is already {old_floor}; it cannot move backward to "
                f"{floor}.",
                file=self._file,
                field="reclaim_floor_lsn",
                old_floor=old_floor,
                value=floor,
            )
        FileHeaderPage.write(
            page,
            replace(
                header,
                root_page=HEADER_PAGE_INDEX,
                payload_length=floor,
            ),
        )
        return HeapReclaimFloorPlan(
            page_index=HEADER_PAGE_INDEX,
            image=self._pool.codec.encode_page(page),
            old_floor=old_floor,
            new_floor=floor,
        )

    def _decode_reclaim_floor(self, header: FileHeader) -> Lsn:
        """Interpret the guarded heap-header floor and reject cross-file disagreement."""

        capabilities = self._catalog.catalog.required_capabilities()
        capable = HEAP_RECLAIM_V1_CAPABILITY in capabilities
        if header.root_page == NO_PAGE and header.payload_length == 0:
            return NO_LSN
        if header.root_page == HEADER_PAGE_INDEX and header.payload_length > 0:
            if capable:
                return header.payload_length
            raise GrafxCorruptionDetected(
                f"Heap {self._file!r} declares a reclaim floor without the required catalog "
                "capability.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                field="required_capabilities",
                value=HEAP_RECLAIM_V1_CAPABILITY,
            )
        raise GrafxCorruptionDetected(
            f"Heap {self._file!r} carries an invalid reclaim-floor marker.",
            file=self._file,
            page=HEADER_PAGE_INDEX,
            field="reclaim_floor",
            root_page=header.root_page,
            payload_length=header.payload_length,
        )

    def plan_vacuum(
        self,
        tables: Sequence[TableDef],
        horizon: Lsn,
        *,
        max_versions: int | None = None,
    ) -> HeapVacuumPlan:
        """Build copy-on-write images that reclaim eligible inline MVCC versions.

        The plan never mutates a resident frame.  Candidate discovery is header-only; a second
        deterministic pass rewrites retained chain links, frees selected slots, compacts each
        touched page, and emits full page images for the ordinary WAL path.  Overflow versions
        are counted but retained by vacuum v1.
        """

        if (
            isinstance(horizon, bool)
            or not isinstance(horizon, int)
            or not 1 <= horizon < PROVISIONAL_CSN
        ):
            raise GrafxConfigurationError(
                "A heap vacuum horizon must be a positive committed LSN.",
                field="horizon_lsn",
                value=repr(horizon),
            )
        if max_versions is not None and (
            isinstance(max_versions, bool)
            or not isinstance(max_versions, int)
            or max_versions <= 0
        ):
            raise GrafxConfigurationError(
                "A heap vacuum max_versions limit must be a positive integer or None.",
                field="max_versions",
                value=repr(max_versions),
            )
        requested = tuple(tables)
        if any(not isinstance(table, TableDef) for table in requested):
            raise GrafxConfigurationError(
                "A heap vacuum plan requires TableDef values.",
                field="tables",
            )
        ordered = tuple(sorted(requested, key=lambda table: table.table_id))
        if len({table.table_id for table in ordered}) != len(ordered):
            raise GrafxConfigurationError(
                "A heap vacuum plan cannot name one table id more than once.",
                field="tables",
            )
        if (
            HEAP_RECLAIM_V1_CAPABILITY
            not in self._catalog.catalog.required_capabilities()
        ):
            raise GrafxSchemaVersionMismatch(
                "Physical heap reclaim requires catalog capability heap_reclaim_v1.",
                field="required_capabilities",
                value=HEAP_RECLAIM_V1_CAPABILITY,
            )

        selected: dict[RecordRef, RecordHeader] = {}
        selected_table: dict[RecordRef, int] = {}
        selected_by_page: dict[PageIndex, list[RecordRef]] = {}
        eligible_by_table: dict[int, int] = {}
        skipped_by_table: dict[int, int] = {}
        pages_by_table: dict[int, tuple[PageIndex, ...]] = {}
        reclaimed_bytes_by_table: dict[int, int] = {}
        selected_by_table: dict[int, int] = {}
        remaining = max_versions
        for table in ordered:
            pages = self.pages_of(table)
            pages_by_table[table.table_id] = pages
            eligible = 0
            skipped = 0
            for ref, header, _content in self._walk(table, copy_content=False):
                if not (
                    is_committed_csn(header.xmin)
                    and is_committed_csn(header.xmax)
                    and header.xmin <= header.xmax <= horizon
                ):
                    continue
                if header.has_overflow:
                    skipped += 1
                    continue
                eligible += 1
                if remaining is None or remaining > 0:
                    selected[ref] = header
                    selected_table[ref] = table.table_id
                    selected_by_page.setdefault(ref.page, []).append(ref)
                    selected_by_table[table.table_id] = (
                        selected_by_table.get(table.table_id, 0) + 1
                    )
                    if remaining is not None:
                        remaining -= 1
            eligible_by_table[table.table_id] = eligible
            skipped_by_table[table.table_id] = skipped

        relinked_by_table: dict[int, int] = {}
        page_images: list[tuple[PageIndex, bytes]] = []
        found: set[RecordRef] = set()
        for table in ordered:
            for page_index in pages_by_table[table.table_id]:
                page = self._pool.read_fresh_page(self._file, page_index)
                self._require_table_page(page, table)
                changed = False
                for slot in page.live_slots():
                    if slot < FIRST_RECORD_SLOT:
                        continue
                    ref = RecordRef(page=page_index, slot=slot)
                    content = page.read_slot(slot)
                    header = RecordHeader.decode(content)
                    candidate = selected.get(ref)
                    if candidate is not None:
                        if header != candidate:
                            raise GrafxTransactionStateError(
                                "A heap vacuum candidate changed while its detached plan was "
                                "being built.",
                                file=self._file,
                                table=table.name,
                                page=page_index,
                                slot=slot,
                                field="vacuum_candidate",
                            )
                        found.add(ref)
                        continue
                    previous = header.previous
                    seen: set[RecordRef] = set()
                    while previous in selected:
                        if previous in seen:
                            raise GrafxCorruptionDetected(
                                f"The selected version chain of record {header.record_id} in "
                                f"table {table.name!r} is cyclic.",
                                file=self._file,
                                table=table.name,
                                record_id=header.record_id,
                                field="cycle",
                            )
                        seen.add(previous)
                        prior = selected[previous]
                        if (
                            selected_table[previous] != table.table_id
                            or prior.record_id != header.record_id
                        ):
                            raise GrafxCorruptionDetected(
                                f"Version {page_index}:{slot} of table {table.name!r} record "
                                f"{header.record_id} points into unrelated reclaimed history.",
                                file=self._file,
                                table=table.name,
                                page=page_index,
                                slot=slot,
                                field="record_id",
                                expected_record_id=header.record_id,
                                observed_record_id=prior.record_id,
                                expected_table_id=table.table_id,
                                observed_table_id=selected_table[previous],
                            )
                        previous = prior.previous
                    encoded_previous = (
                        NO_PREVIOUS_VERSION if previous is None else previous.encode()
                    )
                    if encoded_previous != header.prev_version:
                        page.update_slot(
                            slot,
                            replace(header, prev_version=encoded_previous).encode()
                            + content[RECORD_HEADER_SIZE:],
                        )
                        relinked_by_table[table.table_id] = (
                            relinked_by_table.get(table.table_id, 0) + 1
                        )
                        changed = True

                for ref in sorted(
                    selected_by_page.get(page_index, ()),
                    key=lambda candidate: candidate.slot,
                ):
                    reclaimed_bytes_by_table[table.table_id] = (
                        reclaimed_bytes_by_table.get(table.table_id, 0)
                        + page.free_slot(ref.slot)
                    )
                    changed = True
                if changed:
                    page.compact()
                    page_images.append((page_index, self._pool.codec.encode_page(page)))

        if found != set(selected):
            missing = tuple(
                (ref.page, ref.slot)
                for ref in sorted(set(selected) - found, key=lambda ref: ref.encode())
            )
            raise GrafxTransactionStateError(
                "Heap vacuum candidates disappeared while their detached plan was built.",
                file=self._file,
                field="vacuum_candidate",
                missing=missing,
            )
        table_plans = tuple(
            HeapVacuumTablePlan(
                table_id=table.table_id,
                pages_scanned=len(pages_by_table[table.table_id]),
                eligible_inline_versions=eligible_by_table[table.table_id],
                reclaimed_versions=selected_by_table.get(table.table_id, 0),
                reclaimed_slot_bytes=reclaimed_bytes_by_table.get(table.table_id, 0),
                relinked_versions=relinked_by_table.get(table.table_id, 0),
                skipped_overflow_versions=skipped_by_table[table.table_id],
            )
            for table in ordered
        )
        return HeapVacuumPlan(
            horizon_lsn=horizon,
            page_images=tuple(sorted(page_images)),
            tables=table_plans,
            complete=sum(plan.reclaimed_versions for plan in table_plans)
            == sum(plan.eligible_inline_versions for plan in table_plans),
        )

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
        _, changed = self._observe_record_id_extent(table, record_id)
        return changed

    def _observe_record_id_extent(
        self, table: TableDef, record_id: RecordId
    ) -> tuple[TableExtent, bool]:
        """Raise the identity floor and retain the exact extent that was just settled.

        ``insert`` needs both results: the public boolean and the extent whose identity floor
        was proved before the row can become reachable.  Returning that already-read value
        avoids a second page-zero directory lookup.  It is only a local hint: ``_store_version``
        rejects its proof after any derived-view epoch change, while ``_resolve_tail`` still
        validates and repairs stale physical tail hints before appending.
        """
        _require_record_id(record_id)
        extent = self._extent_for(table)
        if record_id < extent.next_record_id:
            return extent, False
        if record_id >= MAX_U64:
            raise GrafxUnsupportedOperation(
                f"Row identity {record_id} of table {table.name!r} leaves no room for the next "
                f"one in a 64-bit field.",
                table=table.name,
                table_id=table.table_id,
                field="next_record_id",
                value=record_id,
            )
        updated = replace(extent, next_record_id=record_id + 1)
        self._write_extent(updated)
        return updated, True

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
        extent_epoch = self._derived_read_epoch()
        extent, _ = self._observe_record_id_extent(table, record_id)
        extent_proof = self._new_extent_proof(extent, derived_epoch=extent_epoch)
        header = RecordHeader(
            record_id=record_id,
            xmin=xmin,
            xmax=0,
            prev_version=NO_PREVIOUS_VERSION,
            payload_len=len(payload),
            schema_version=table.schema_version,
        )
        return self._store_version(table, header, payload, extent_proof=extent_proof)

    def insert_reserved(
        self,
        table: TableDef,
        record_id: RecordId,
        values: tuple[Value, ...],
        xmin: Csn,
        *,
        extent_proof: object | None = None,
    ) -> RecordRef:
        """Store a row whose identity is already below this table's durable floor.

        The ordinary :meth:`insert` must raise page 0 before it writes a supplied identity,
        because that identity may have come from replay or from an explicit caller.  A leased
        identity has already been burned by a committed floor image, so raising the counter again
        would manufacture the page-zero false sharing the lease exists to remove.  This narrower
        door therefore requires an existing extent and proves ``record_id < next_record_id``
        before storing the version, then leaves the floor untouched.  Tail repair or growth may
        still update the other fields of the extent; only the identity floor is bypassed.
        """
        _require_commit_number("xmin", xmin)
        _require_record_id(record_id)
        if record_id < FIRST_RECORD_ID:
            raise GrafxConfigurationError(
                f"A reserved record id starts at {FIRST_RECORD_ID}; got {record_id}.",
                field="record_id",
                value=record_id,
            )
        extent_epoch = self._derived_read_epoch()
        cursor = self._current_extent_cursor(
            extent_proof, table.table_id, derived_epoch=extent_epoch
        )
        if cursor is not None:
            assert type(extent_proof) is _ExtentProof
            proof = extent_proof
            extent = cursor.extent
            durable_floor = proof.authority.durable_floor
        else:
            extent = self._find_extent(table.table_id)
            proof = (
                None
                if extent is None
                else self._new_extent_proof(extent, derived_epoch=extent_epoch)
            )
            durable_floor = None if extent is None else extent.next_record_id
        if extent is None:
            raise GrafxTransactionStateError(
                f"Table {table.name!r} has no heap extent, so record id {record_id} cannot "
                "belong to a durable reservation.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="table_extent",
                record_id=record_id,
            )
        assert durable_floor is not None
        if record_id >= durable_floor:
            raise GrafxTransactionStateError(
                f"Record id {record_id} of table {table.name!r} is not below its durable "
                f"identity floor {durable_floor}.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="record_id",
                record_id=record_id,
                durable_floor=durable_floor,
            )
        payload = encode_tuple(table, values)
        header = RecordHeader(
            record_id=record_id,
            xmin=xmin,
            xmax=0,
            prev_version=NO_PREVIOUS_VERSION,
            payload_len=len(payload),
            schema_version=table.schema_version,
        )
        return self._store_version(table, header, payload, extent_proof=proof)

    def insert_initial_reserved(
        self,
        table: TableDef,
        record_id: RecordId,
        values: tuple[Value, ...],
        xmin: Csn,
        *,
        next_record_id: RecordId,
    ) -> RecordRef:
        """Create a table's first extent with one batch-wide identity floor.

        A transaction that materialises the first rows of a table already planned every identity
        while holding the global commit section.  Creating the extent at the exclusive upper
        bound lets the remaining rows use :meth:`insert_reserved` instead of rewriting heap page
        zero once per row.  The floor is part of the surrounding user transaction's ordinary page
        image and WAL batch; this door never publishes a metadata subcommit.

        The extent must still be absent.  An existing one means the caller's locked plan no longer
        describes the materialisation point and is refused before any new page is allocated.
        Encoding is also completed before allocation so malformed values cannot leave an empty
        extent behind.
        """
        _require_commit_number("xmin", xmin)
        _require_record_id(record_id)
        floor = _require_record_id_floor(next_record_id)
        if record_id < FIRST_RECORD_ID or record_id >= floor:
            raise GrafxConfigurationError(
                f"Initial record id {record_id} of table {table.name!r} must be at least "
                f"{FIRST_RECORD_ID} and below its batch identity floor {floor}.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="record_id",
                record_id=record_id,
                next_record_id=floor,
            )
        if self._find_extent(table.table_id) is not None:
            raise GrafxTransactionStateError(
                f"Table {table.name!r} already has a heap extent, so its initial batch floor "
                "cannot be installed.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="table_extent",
                record_id=record_id,
                next_record_id=floor,
            )
        payload = encode_tuple(table, values)
        extent = self._create_extent(table, next_record_id=floor)
        extent_proof = self._new_extent_proof(extent)
        header = RecordHeader(
            record_id=record_id,
            xmin=xmin,
            xmax=0,
            prev_version=NO_PREVIOUS_VERSION,
            payload_len=len(payload),
            schema_version=table.schema_version,
        )
        return self._store_version(table, header, payload, extent_proof=extent_proof)

    def reserved_extent_proof(self, table: TableDef, record_id: RecordId) -> object:
        """Prove one durable reserved id and return a revocable, store-bound extent hint.

        The caller may reuse the hint for later ids only through :meth:`insert_reserved`, which
        rechecks the owner, table and current derived epoch and validates every id against the
        captured exclusive floor.  A cache drop or foreign read view therefore falls back to the
        ordinary extent lookup; the proof is never durable authority and never leaves this store.
        """
        _require_record_id(record_id)
        if record_id < FIRST_RECORD_ID:
            raise GrafxConfigurationError(
                f"A reserved record id starts at {FIRST_RECORD_ID}; got {record_id}.",
                field="record_id",
                value=record_id,
            )
        extent_epoch = self._derived_read_epoch()
        extent = self._find_extent(table.table_id)
        if extent is None:
            raise GrafxTransactionStateError(
                f"Table {table.name!r} has no heap extent, so record id {record_id} cannot "
                "belong to a durable reservation.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="table_extent",
                record_id=record_id,
            )
        if record_id >= extent.next_record_id:
            raise GrafxTransactionStateError(
                f"Record id {record_id} of table {table.name!r} is not below its durable "
                f"identity floor {extent.next_record_id}.",
                file=self._file,
                table=table.name,
                table_id=table.table_id,
                field="record_id",
                record_id=record_id,
                durable_floor=extent.next_record_id,
            )
        return self._new_extent_proof(extent, derived_epoch=extent_epoch)

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

    def committed_high_water(self, table: TableDef) -> Lsn:
        """Return the highest committed birth or end stamp stored for ``table``.

        Index freshness is a property of the table an index covers, not of unrelated commits
        elsewhere in the database. This deliberately walks only record headers: payloads and
        overflow chains are irrelevant to the watermark and decoding them would turn an
        open-time integrity check into a full logical table scan.

        ``NO_CSN`` means that no committed version or end is present. Provisional stamps are
        abandoned, unpublished attempts and therefore cannot raise the committed watermark.
        """
        high_water: Lsn = NO_LSN

        def observe(record_id: RecordId, xmin: Csn, xmax: Csn) -> bool:
            nonlocal high_water
            if is_committed_csn(xmin):
                high_water = max(high_water, xmin)
            elif xmin != NO_CSN and not is_provisional_csn(xmin):
                raise GrafxCorruptionDetected(
                    f"Record {record_id} of table {table.name!r} has invalid birth "
                    f"stamp {xmin}.",
                    file=self._file,
                    table=table.name,
                    table_id=table.table_id,
                    record_id=record_id,
                    field="xmin",
                    value=xmin,
                )
            if is_committed_csn(xmax):
                high_water = max(high_water, xmax)
            elif xmax != NO_CSN and not is_provisional_csn(xmax):
                raise GrafxCorruptionDetected(
                    f"Record {record_id} of table {table.name!r} has invalid end "
                    f"stamp {xmax}.",
                    file=self._file,
                    table=table.name,
                    table_id=table.table_id,
                    record_id=record_id,
                    field="xmax",
                    value=xmax,
                )
            # ``_walk`` performs the same fixed-header unpack before invoking this predicate.
            # Rejecting every row avoids constructing RecordHeader values and per-page result
            # lists while still walking and validating every page and both MVCC stamps.
            return False

        for _unused in self._walk(table, accept=observe, copy_content=False):
            raise AssertionError(
                "the high-water observer must not materialize heap versions"
            )
        return high_water

    def read(self, ref: RecordRef) -> HeapVersion:
        """Return the version stored at that location, whichever table it belongs to."""
        table_id, content = self._read_slot(ref)
        table = self._catalog.catalog.table_by_id(table_id)
        return self._decode_version(table, content)

    def read_if(
        self,
        ref: RecordRef,
        accept: Callable[[RecordId, Csn, Csn], bool],
    ) -> HeapVersion | None:
        """Return the version at that location if the predicate accepts its header, else None.

        The header-first sibling of :meth:`read` (VEC-2). The predicate sees ``record_id``,
        ``xmin`` and ``xmax`` from one struct unpack of the raw slot -- the same three fields,
        from the same unpack, that :meth:`_walk` offers its predicate -- and only a version it
        accepts is decoded: the payload, and the vector inside it, are never materialised for a
        row a snapshot cannot see or a filter refuses. Everything :meth:`read` checks before it
        decodes is checked here in the same order: the location names a record slot of a data
        page, and the table of that page is the table the version is decoded with.
        """
        table_id, content = self._read_slot(ref)
        table = self._catalog.catalog.table_by_id(table_id)
        fields = RecordHeader.peek(content)
        if not accept(fields[4], fields[5], fields[6]):
            return None
        return self._decode_version_with_header(
            table, RecordHeader._from_peek(fields), content
        )

    def _revalidate_visible_ref(
        self,
        table: TableDef,
        ref: RecordRef,
        record_id: RecordId,
        snapshot: SnapshotLike,
    ) -> HeapVersion | None:
        """Fully decode one expected physical row and reapply the caller's snapshot.

        This is an internal proof door, not an identity lookup.  The caller already has a
        :class:`RecordRef`; accepting a different table or identity at that location would turn
        a stale/malformed proof into a different row.  Those mismatches are corruption and are
        never candidates for fallback.  Ordinary invisibility remains ``None``, matching
        :meth:`lookup`.
        """
        _require_record_id(record_id)
        table_id, content = self._read_slot(ref)
        if table_id != table.table_id:
            raise GrafxCorruptionDetected(
                f"Endpoint reference {ref.page}:{ref.slot} belongs to table {table_id}, not to "
                f"{table.name!r} with id {table.table_id}.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                table=table.name,
                table_id=table_id,
                expected_table_id=table.table_id,
                field="table_id",
            )
        version = self._decode_version(table, content)
        if version.record_id != record_id:
            raise GrafxCorruptionDetected(
                f"Endpoint reference {ref.page}:{ref.slot} names record {version.record_id}, "
                f"not record {record_id} of table {table.name!r}.",
                file=self._file,
                page=ref.page,
                slot=ref.slot,
                table=table.name,
                table_id=table.table_id,
                field="record_id",
                expected_record_id=record_id,
                observed_record_id=version.record_id,
            )
        if not snapshot.visible(version.xmin, version.xmax):
            return None
        return version

    def _visible_record_cursor(
        self,
        table: TableDef,
        snapshot: SnapshotLike,
        *,
        admit_page: Callable[[], None],
    ) -> _VisibleRecordCursor:
        """Return a resumable canonical header cursor for one table and snapshot."""
        return _VisibleRecordCursor(self, table, snapshot, admit_page)

    def _derived_read_epoch(self) -> int:
        """Return the conservative epoch that vouches for derived heap walks."""
        return self._pool.derived_epoch(self._file)

    def scan(
        self, table: TableDef, snapshot: SnapshotLike
    ) -> Iterator[tuple[RecordRef, HeapVersion]]:
        """Yield every version of the table the snapshot can see, in storage order."""

        def visible(_record_id: RecordId, xmin: Csn, xmax: Csn) -> bool:
            """Return whether this snapshot may observe the record header."""
            return snapshot.visible(xmin, xmax)

        for ref, header, content in self._walk(table, accept=visible):
            yield ref, self._decode_version_with_header(table, header, content)

    def scan_relationship_endpoints(
        self, table: TableDef, snapshot: SnapshotLike
    ) -> Iterator[tuple[RecordRef, tuple[RecordId, RecordId]]]:
        """Yield visible relationship references and endpoints in storage order.

        The complete payload is reconstructed and validated against every declared column.  The
        only difference from :meth:`scan` is materialisation: properties and ``HeapVersion`` are
        not retained when the caller needs only the relationship partition.  Rejected headers
        stay header-only, so an invisible version still incurs no payload or overflow read.
        """
        if table.kind != "rel":
            raise GrafxConfigurationError(
                f"Table {table.name!r} is a {table.kind} table and has no relationship endpoints.",
                field="kind",
                value=table.kind,
                table=table.name,
                table_id=table.table_id,
            )

        def visible(_record_id: RecordId, xmin: Csn, xmax: Csn) -> bool:
            """Return whether this snapshot may observe the relationship header."""
            return snapshot.visible(xmin, xmax)

        for ref, header, content in self._walk(table, accept=visible):
            payload = self._validated_payload(table, header, content)
            endpoints = decode_relationship_endpoints(table, payload)
            # HeapVersion construction evaluates the tuple before the previous-version
            # reference.  Preserve both that validation and its order without retaining either
            # object: a u64 header field is wider than RecordRef's durable 48-bit encoding.
            _previous = header.previous
            yield ref, endpoints

    def scan_page(
        self,
        table: TableDef,
        snapshot: SnapshotLike,
        *,
        limit: int,
        position: _HeapScanPosition | None = None,
    ) -> tuple[
        tuple[tuple[RecordRef, HeapVersion], ...],
        _HeapScanPosition | None,
    ]:
        """Return at most ``limit`` visible rows and a physical continuation.

        Unlike :meth:`scan`, this door retains no generator and no growing visited-page set
        between calls.  It decodes at most ``limit`` row payloads.  Headers beyond the boundary
        may be inspected to locate the next visible row, so a non-terminal page never requires an
        empty follow-up call, but that look-ahead does not decode the row's values.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise GrafxConfigurationError(
                "A heap scan page limit must be a positive integer.",
                field="limit",
                value=repr(limit),
            )
        if position is not None and type(position) is not _HeapScanPosition:
            raise GrafxConfigurationError(
                "A heap scan continuation must be an internal scan position.",
                field="position",
                value=type(position).__name__,
            )

        extent = self._find_extent(table.table_id)
        if extent is None:
            return (), None

        if position is None:
            index = extent.first_page
            start_slot = FIRST_RECORD_SLOT
            pages_walked = 1
            chain_limit = self._chain_limit()
        else:
            index = position.page
            start_slot = position.slot
            pages_walked = position.pages_walked
            chain_limit = position.chain_limit
            current_chain_limit = self._chain_limit()
            if (
                type(index) is not int
                or type(start_slot) is not int
                or type(pages_walked) is not int
                or type(chain_limit) is not int
                or index == NO_PAGE
                or index <= HEADER_PAGE_INDEX
                or start_slot < FIRST_RECORD_SLOT
                or pages_walked <= 0
                or chain_limit <= 0
                or pages_walked > chain_limit
                or chain_limit > current_chain_limit
                or index >= chain_limit - 1
                or index >= current_chain_limit - 1
            ):
                raise GrafxConfigurationError(
                    "A heap scan continuation carries an invalid physical position.",
                    field="position",
                    page=index,
                    slot=start_slot,
                    pages_walked=pages_walked,
                    chain_limit=chain_limit,
                )

        selected: list[tuple[RecordRef, RecordHeader, bytes]] = []
        next_position: _HeapScanPosition | None = None
        while index != NO_PAGE:
            self._refuse_endless_chain(table, pages_walked, chain_limit)
            with self._pool.pinned(self._file, index) as page:
                self._require_table_page(page, table)
                following = page.next_page
                for slot in page.live_slots():
                    if slot < max(start_slot, FIRST_RECORD_SLOT):
                        continue
                    view = page.slot_view(slot)
                    fields = RecordHeader.peek(view)
                    (
                        _flags,
                        _reserved,
                        _schema_version,
                        _payload_len,
                        _record_id,
                        xmin,
                        xmax,
                        _previous,
                    ) = fields
                    if not snapshot.visible(xmin, xmax):
                        continue
                    header = RecordHeader._from_peek(fields)
                    if len(selected) == limit:
                        next_position = _HeapScanPosition(
                            page=index,
                            slot=slot,
                            pages_walked=pages_walked,
                            chain_limit=chain_limit,
                        )
                        break
                    selected.append(
                        (RecordRef(page=index, slot=slot), header, bytes(view))
                    )

            if next_position is not None:
                break

            if following == NO_PAGE:
                break
            if following >= chain_limit - 1:
                current_page_ceiling = self._chain_limit() - 1
                if following >= current_page_ceiling:
                    raise GrafxCorruptionDetected(
                        f"The page chain of table {table.name!r} in {self._file!r} points to "
                        f"page {following}, outside a file with {current_page_ceiling} pages.",
                        file=self._file,
                        table=table.name,
                        page=following,
                        field="next_page",
                        page_count=current_page_ceiling,
                    )
                # The allocator is append-only: an index beyond the ceiling captured by the
                # first page was linked by a later writer.  Such a page cannot hold a version
                # visible to this scan's older snapshot, so this is its stable physical end.  It
                # must still be a real page of this table; growth never excuses a corrupt link.
                with self._pool.pinned(self._file, following) as appended_page:
                    self._require_table_page(appended_page, table)
                break
            index = following
            start_slot = FIRST_RECORD_SLOT
            pages_walked += 1

        # Decoding can follow overflow chains, so it happens only after every data-page pin above
        # has been released. ``selected`` contains at most ``limit`` payloads.
        rows = tuple(
            (ref, self._decode_version_with_header(table, header, content))
            for ref, header, content in selected
        )
        return rows, next_position

    def scan_all(self, table: TableDef) -> Iterator[tuple[RecordRef, HeapVersion]]:
        """Yield every stored version of the table, visible or not.

        Recovery and verification need the whole truth of what is on the pages, which is exactly
        what a snapshot is designed to hide.
        """
        for ref, header, content in self._walk(table):
            yield ref, self._decode_version_with_header(table, header, content)

    def lookup(
        self, table: TableDef, record_id: RecordId, snapshot: SnapshotLike
    ) -> HeapVersion | None:
        """Return the version of that record the snapshot can see, or None when there is none."""
        found = self._lookup_with_ref(table, record_id, snapshot)
        return None if found is None else found[1]

    def _lookup_with_ref(
        self, table: TableDef, record_id: RecordId, snapshot: SnapshotLike
    ) -> tuple[RecordRef, HeapVersion] | None:
        """Return the first canonical visible version and its physical reference.

        This internal sibling deliberately shares the public lookup's head-to-tail walk.  It is
        reusable by any executor path that already needs an identity and cannot afford to throw
        away the reference; it neither changes scan order nor adds a second visibility rule.
        """

        def wanted(candidate: RecordId, xmin: Csn, xmax: Csn) -> bool:
            """Return whether this version is the requested row visible to the snapshot."""
            return candidate == record_id and snapshot.visible(xmin, xmax)

        for ref, header, content in self._walk(table, accept=wanted):
            return ref, self._decode_version_with_header(table, header, content)
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

    def _measure_bloat(self, table: TableDef, horizon: Lsn) -> _HeapBloatSample:
        """Measure conservative slot bloat without decoding tuples or following overflow.

        The horizon is the caller's already-derived recyclable horizon.  Only a version with a
        structurally plausible committed lifetime (committed ``xmin`` and ``xmax`` with
        ``xmin <= xmax``) can be called horizon-eligible.  Everything else remains merely stored:
        this diagnostic is not a verifier and must never certify malformed or provisional
        residue for destructive maintenance.

        Byte counts are the lengths of record slots.  For an overflow-backed version that means
        the fixed header and pointer only; the separately allocated overflow pages are counted
        nowhere, intentionally.  Eligible and retained are a partition of ended versions only;
        live and provisional versions stay in the stored total but are never described as bloat.
        Slot directory entries are likewise reported but never priced as horizon-eligible because
        their identifiers cannot currently be reused safely.
        """
        if (
            isinstance(horizon, bool)
            or not isinstance(horizon, int)
            or not NO_LSN <= horizon < PROVISIONAL_CSN
        ):
            raise GrafxConfigurationError(
                "A heap-bloat horizon must be a non-negative committed LSN.",
                field="horizon_lsn",
                value=repr(horizon),
            )

        extent = self._find_extent(table.table_id)
        if extent is None:
            return _HeapBloatSample(table.table_id, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

        data_pages = 0
        directory_entries = 0
        free_slots = 0
        stored_versions = 0
        ended_versions = 0
        eligible_versions = 0
        retained_versions = 0
        eligible_bytes = 0
        retained_bytes = 0
        overflow_versions = 0
        eligible_overflow_versions = 0
        seen: set[PageIndex] = visited_pages()
        limit = self._chain_limit()
        index = extent.first_page

        while index != NO_PAGE:
            data_pages += 1
            self._refuse_endless_chain(table, data_pages, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The page chain of table {table.name!r} in {self._file!r} returns to "
                    f"page {index}.",
                    file=self._file,
                    table=table.name,
                    page=index,
                    field="cycle",
                )
            seen.add(index)
            with self._pool.pinned(self._file, index) as page:
                self._require_table_page(page, table)
                directory_entries += max(page.slot_count - FIRST_RECORD_SLOT, 0)
                for slot in range(FIRST_RECORD_SLOT, page.slot_count):
                    if page.is_slot_free(slot):
                        free_slots += 1
                        continue
                    view = page.slot_view(slot)
                    (
                        flags,
                        _reserved,
                        _schema_version,
                        _payload_len,
                        _record_id,
                        xmin,
                        xmax,
                        _previous,
                    ) = RecordHeader.peek(view)
                    slot_bytes = page.slot_length(slot)
                    stored_versions += 1
                    has_overflow = bool(flags & RECORD_FLAG_HAS_OVERFLOW)
                    if has_overflow:
                        overflow_versions += 1
                    if not is_committed_csn(xmax):
                        continue
                    ended_versions += 1
                    horizon_eligible = (
                        is_committed_csn(xmin) and xmin <= xmax and xmax <= horizon
                    )
                    if horizon_eligible:
                        eligible_versions += 1
                        eligible_bytes += slot_bytes
                        if has_overflow:
                            eligible_overflow_versions += 1
                    else:
                        retained_versions += 1
                        retained_bytes += slot_bytes
                following = page.next_page
            index = following

        return _HeapBloatSample(
            table_id=table.table_id,
            data_pages=data_pages,
            slot_directory_entries=directory_entries,
            free_slots=free_slots,
            stored_versions=stored_versions,
            ended_versions=ended_versions,
            horizon_eligible_versions=eligible_versions,
            horizon_retained_versions=retained_versions,
            horizon_eligible_slot_bytes=eligible_bytes,
            horizon_retained_slot_bytes=retained_bytes,
            overflow_versions=overflow_versions,
            horizon_eligible_overflow_versions=eligible_overflow_versions,
        )

    # --- internals ---------------------------------------------------------------------------

    def _new_extent_proof(
        self,
        extent: TableExtent,
        *,
        derived_epoch: int | None = None,
    ) -> _ExtentProof:
        """Seal a frozen reservation boundary around one mutable physical cursor."""
        epoch = self._derived_read_epoch() if derived_epoch is None else derived_epoch
        return _ExtentProof(
            authority=_ExtentAuthority(
                owner=self,
                seal=self._extent_proof_seal,
                table_id=extent.table_id,
                first_page=extent.first_page,
                durable_floor=extent.next_record_id,
            ),
            cursor=_ExtentCursor(extent, epoch),
        )

    def _current_extent_cursor(
        self,
        proof: object | None,
        table_id: int,
        *,
        derived_epoch: int | None = None,
    ) -> _ExtentCursor | None:
        """Return only an exact, sealed and still-current cursor for one table."""
        if type(proof) is not _ExtentProof:
            return None
        authority = proof.authority
        cursor = proof.cursor
        epoch = self._derived_read_epoch() if derived_epoch is None else derived_epoch
        if (
            type(authority) is not _ExtentAuthority
            or type(cursor) is not _ExtentCursor
            or authority.owner is not self
            or authority.seal is not self._extent_proof_seal
            or authority.table_id != table_id
            or cursor.derived_epoch != epoch
            or cursor.extent.table_id != table_id
            or cursor.extent.first_page != authority.first_page
            or cursor.extent.next_record_id < authority.durable_floor
        ):
            return None
        return cursor

    def _store_version(
        self,
        table: TableDef,
        header: RecordHeader,
        payload: bytes,
        *,
        extent_proof: _ExtentProof | None = None,
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
        cursor = self._current_extent_cursor(extent_proof, table.table_id)
        proof_is_current = cursor is not None
        if cursor is None:
            extent = self._extent_for(table)
        else:
            authority = extent_proof.authority
            # Only last_page/page_count are cursor hints.  The table root and reservation floor
            # remain frozen authority and are restored before any directory rewrite can occur.
            extent = replace(
                cursor.extent,
                table_id=authority.table_id,
                first_page=authority.first_page,
                next_record_id=authority.durable_floor,
            )
        tail, length = self._resolve_tail(table, extent)
        if RECORD_HEADER_SIZE + len(payload) <= self.inline_capacity:
            content = header.encode() + payload
        else:
            chain = write_chain(
                self._pool, self._file, payload, page_type=int(PageType.OVERFLOW)
            )
            overflowed = replace(header, flags=header.flags | RECORD_FLAG_HAS_OVERFLOW)
            content = overflowed.encode() + encode_overflow_pointer(chain[0])
        reference, settled_extent = self._append(table, extent, tail, length, content)
        if proof_is_current:
            # Only this store mutates its private proof.  The exclusive identity floor never
            # changes here; carrying the tail/count just settled by _append prevents a hot batch
            # from mistaking its own older hint for directory drift on the next row.
            assert extent_proof is not None
            assert cursor is not None
            cursor.extent = settled_extent
            cursor.derived_epoch = self._derived_read_epoch()
        return reference

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
    ) -> tuple[RecordRef, TableExtent]:
        """Append one slot to the tail page of the table, growing the chain when it is full.

        The tail arrives resolved and checked, because the caller has to settle every refusal
        before it writes an overflow chain. What goes back into the directory is the length the
        walk counted, never the stored count plus the distance travelled: the stored count
        describes wherever the hint happened to be, so adding to it is permanently wrong whenever
        the hint was not where the count said. A count that is never right is worse than no count,
        because the next append walks zero hops and never revisits it (A40.2).

        The common settled-hint cost is proven by
        test_a_settled_inline_append_reuses_its_capacity_pin. The drift path's frame cost is
        proven by test_the_extent_hint_is_written_with_the_tail_released and its consequence by
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
            if fits and extent.last_page == tail and extent.page_count == length:
                # The capacity decision and insertion concern the same validated resident page.
                # With no directory hint to repair, retaining this pin removes a second page
                # acquisition and also removes its otherwise unnecessary refusal window.
                return RecordRef(page=tail, slot=page.insert_slot(content)), extent
        if fits:
            if extent.last_page != tail or extent.page_count != length:
                # The length the walk counted, never the stored count plus the distance
                # travelled: adding to a count that describes wherever the hint happened to be
                # is permanently wrong the moment the hint was not where the count said (A40.2).
                extent = replace(extent, last_page=tail, page_count=length)
                extent = self._write_extent(extent)
            # Re-taken only after repairing a stale hint. This pin can still refuse -- it can
            # evict a dirty page, and the write-back is where the device speaks -- and it refuses
            # with nothing of this row on any page. Once held, insert_slot touches only the
            # pinned frame, so the row cannot half-arrive.
            with self._pool.pinned(self._file, tail) as page:
                return RecordRef(page=tail, slot=page.insert_slot(content)), extent
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
        extent = replace(extent, last_page=new_index, page_count=length + 1)
        extent = self._write_extent(extent)
        with self._pool.pinned(self._file, tail) as page:
            page.next_page = new_index
        self._tail_cache[table.table_id] = (
            new_index,
            length + 1,
            self._pool.derived_epoch(self._file),
        )
        return RecordRef(page=new_index, slot=slot), extent

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
        return self._create_extent(table, next_record_id=FIRST_RECORD_ID)

    def _create_extent(
        self, table: TableDef, *, next_record_id: RecordId
    ) -> TableExtent:
        """Create one absent table extent with its already-validated exclusive id floor."""
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
            table_id=table.table_id,
            first_page=index,
            last_page=index,
            page_count=1,
            next_record_id=next_record_id,
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
        slot = header_page.insert_slot(extent.encode())
        self._sync_extent_slots_epoch()
        self._extent_slots[extent.table_id] = slot

    def _find_extent(self, table_id: int) -> TableExtent | None:
        """Return the directory entry of the table, or None when the table has no page yet."""
        with self._pinned_validated_header() as (header_page, _header):
            self._sync_extent_slots_epoch()
            cached = self._extent_slots.get(table_id)
            if cached is not None:
                payload = self._extent_payload_at(header_page, cached, table_id)
                if payload is not None:
                    return self._require_first_page(TableExtent.decode(payload))
                self._extent_slots.pop(table_id, None)
            for slot, payload in header_page.iter_slot_views():
                if slot < EXTENT_FIRST_SLOT:
                    continue
                if self._extent_table_id(payload) != table_id:
                    continue
                extent = self._require_first_page(TableExtent.decode(payload))
                self._extent_slots[table_id] = slot
                return extent
        return None

    @staticmethod
    def _extent_table_id(payload: bytes | memoryview) -> int:
        """Read only an extent's table-id prefix, while retaining the format length guard."""
        if len(payload) != DIRECTORY_ENTRY_SIZE:
            raise GrafxCorruptionDetected(
                f"A heap directory entry is {DIRECTORY_ENTRY_SIZE} bytes; got {len(payload)}.",
                field="directory_entry",
                value=len(payload),
            )
        return _DESCRIPTOR.unpack_from(payload)[0]

    @classmethod
    def _extent_payload_at(
        cls, header_page: Page, slot: SlotId, table_id: int
    ) -> memoryview | None:
        """Return a verified cached slot view, or None when the hint became stale."""
        if (
            slot < EXTENT_FIRST_SLOT
            or slot >= header_page.slot_count
            or header_page.is_slot_free(slot)
        ):
            return None
        payload = header_page.slot_view(slot)
        if cls._extent_table_id(payload) != table_id:
            return None
        return payload

    def _sync_extent_slots_epoch(self) -> None:
        """Invalidate directory hints whenever their page view generation changes."""
        identity = (
            self._bootstrapped_epoch,
            self._pool.derived_epoch(self._file),
        )
        if self._extent_slots_epoch != identity:
            self._extent_slots.clear()
            self._extent_slots_epoch = identity

    def _invalidate_extent_slots(self) -> None:
        """Forget every directory hint and its page-view identity."""
        self._extent_slots.clear()
        self._extent_slots_epoch = None

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

    def _write_extent(self, extent: TableExtent) -> TableExtent:
        """Replace one extent while preserving a newer durable identity floor."""
        with self._pinned_validated_header() as (header_page, _header):
            self._sync_extent_slots_epoch()
            cached = self._extent_slots.get(extent.table_id)
            if cached is not None:
                payload = self._extent_payload_at(header_page, cached, extent.table_id)
                if payload is not None:
                    current = TableExtent.decode(payload)
                    extent = replace(
                        extent,
                        next_record_id=max(
                            extent.next_record_id, current.next_record_id
                        ),
                    )
                    header_page.update_slot(cached, extent.encode())
                    return extent
                self._extent_slots.pop(extent.table_id, None)
            for slot, payload in header_page.iter_slot_views():
                if slot < EXTENT_FIRST_SLOT:
                    continue
                if self._extent_table_id(payload) != extent.table_id:
                    continue
                current = TableExtent.decode(payload)
                extent = replace(
                    extent,
                    next_record_id=max(extent.next_record_id, current.next_record_id),
                )
                header_page.update_slot(slot, extent.encode())
                self._extent_slots[extent.table_id] = slot
                return extent
            raise GrafxCorruptionDetected(
                f"Table {extent.table_id} has no directory entry on the header page of "
                f"{self._file!r}, so its extent cannot be updated.",
                file=self._file,
                table_id=extent.table_id,
            )

    @contextmanager
    def _pinned_validated_header(self) -> Iterator[tuple[Page, FileHeader]]:
        """Yield page 0 after the bootstrap and page-integrity checks, pinning it once hot.

        A moved derived epoch retains the canonical ``is_bootstrapped`` probe and its cache
        invalidation before the caller acquires the operational pin. Under a stable epoch the
        operational pin itself performs the required resident header check, so a directory
        lookup or rewrite need not acquire the same page once merely to validate it and again to
        use it. The yielded pin is never storage authority beyond this context. Detached planners
        that require the current device image keep the separate ``_require_bootstrapped`` plus
        ``read_fresh_page`` protocol; this resident helper must not replace that authority path.
        """
        if self._bootstrapped_epoch != self._pool.derived_epoch(self._file):
            self._invalidate_extent_slots()
            if not self.is_bootstrapped():
                raise GrafxCorruptionDetected(
                    f"The heap file {self._file!r} has no header page; call bootstrap() first.",
                    file=self._file,
                )
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            header = self._require_header_page(page)
            yield page, header

    def _require_bootstrapped(self) -> None:
        """Refuse to work against a heap file that has not been created yet."""
        if self._bootstrapped_epoch == self._pool.derived_epoch(self._file):
            # The memo removes the repeated device-level ``exists`` and ``page_count`` probes,
            # not the page-level integrity check.  The header may have been changed through a
            # resident writable page without moving the pool's derived epoch; pinning that hot
            # frame is cheap and preserves the rule that a damaged in-memory header is refused
            # before any heap operation proceeds.
            with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
                self._require_header_page(page)
            return
        self._invalidate_extent_slots()
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

    def _walk(
        self,
        table: TableDef,
        *,
        accept: Callable[[RecordId, Csn, Csn], bool] | None = None,
        copy_content: bool = True,
    ) -> Iterator[tuple[RecordRef, RecordHeader, bytes]]:
        """Yield every stored version of the table with its location and its raw content.

        One page at a time is pinned. A predicate sees ``record_id``, ``xmin`` and ``xmax`` from
        one struct unpack of the read-only slot view. Rejected rows never materialize a
        :class:`RecordHeader`; accepted rows materialize the complete header from those same
        unpacked fields. Only accepted content is copied before the pin is released. Following an
        overflow chain never needs a second frame while a data page is still held. Header-only
        callers may also suppress every content copy.
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
                items: list[tuple[SlotId, RecordHeader, bytes]] = []
                for slot, view in page.iter_slot_views():
                    if slot < FIRST_RECORD_SLOT:
                        continue
                    if accept is None:
                        header = RecordHeader.decode(view)
                    else:
                        fields = RecordHeader.peek(view)
                        (
                            _flags,
                            _reserved,
                            _schema_version,
                            _payload_len,
                            record_id,
                            xmin,
                            xmax,
                            _previous,
                        ) = fields
                        if not accept(record_id, xmin, xmax):
                            continue
                        header = RecordHeader._from_peek(fields)
                    items.append((slot, header, bytes(view) if copy_content else b""))
                following = page.next_page
            for slot, header, content in items:
                yield (
                    RecordRef(page=index, slot=slot),
                    header,
                    content,
                )
            index = following

    def _decode_version(self, table: TableDef, content: bytes) -> HeapVersion:
        """Turn the raw content of a slot into a decoded version of the table."""
        return self._decode_version_with_header(
            table, RecordHeader.decode(content), content
        )

    def _decode_version_with_header(
        self, table: TableDef, header: RecordHeader, content: bytes
    ) -> HeapVersion:
        """Decode a version whose header the page walk has already validated."""
        payload = self._validated_payload(table, header, content)
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

    def _validated_payload(
        self, table: TableDef, header: RecordHeader, content: bytes
    ) -> bytes:
        """Return one payload after the checks shared by full and projected decoders."""
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
        return payload

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
