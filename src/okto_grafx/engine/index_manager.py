"""The secondary-index framework (CONTRACT.md section 8.7; SPEC-M1 FR-12, BR-11, SD-3).

What a caller may rely on, stated as two rules and nothing else:

**An EXACT index answers with candidates.** ``lookup`` returns a superset of the heap locations
whose row carries the key and is visible to the snapshot. It may return a location whose row was
deleted, superseded, or committed after the snapshot opened. Every hit MUST be validated against
the heap under the caller's own snapshot, and :meth:`IndexManager.lookup` is the door that does
it, so the obligation is discharged by the framework rather than left to a caller's discipline.

**A PROXIMITY index answers with results.** ``lookup`` returns exactly the heap locations the
snapshot may see, decided from the entry's own birth stamp and tombstone. Nothing is validated
against the heap, because validating every candidate is precisely what a proximity structure
exists to avoid. The price is that the index must be maintained exactly: a missing tombstone is a
deleted row in a result set, so tombstones are written inside the same commit as the heap change
and are removed only once the snapshot horizon has passed them.

Neither rule can be applied to the other kind by accident. An exact entry physically carries no
birth stamp, so :func:`okto_grafx.domain.index.visibility.entry_visible` refuses it rather than
guessing, and the mistake that would produce wrong results is unrepresentable rather than
guarded.

Three further properties hold for both kinds.

*Every index change is covered by the log* (BR-11). ``stage_insert`` and ``stage_delete`` touch no
page at all: they stage an ``INDEX_WRITE`` record on the transaction, and the pages move only in
:meth:`IndexManager.commit`, after the commit is durable and its commit number is known. An
aborted or refused transaction therefore leaves nothing behind -- which matters most for a
proximity index, where an entry from a transaction that never committed would be returned to a
caller as a result.

*A stale index refuses rather than omits.* The index file records the log position through which
it is known to reflect the heap. An index behind that position is marked stale, durably, and its
lookups raise instead of answering with a set that might be missing a row. The repair is
:meth:`IndexManager.rebuild`, which re-derives every entry from the heap inside a transaction, so
the repair is itself covered by the log.

The position moves only on a commit that had something for THAT index, which makes
:meth:`IndexManager.open` an OPEN-TIME check rather than a running one: a database that has been
committing to one table would otherwise find every other index behind the published position and
call it stale. Recovery closes the gap with :meth:`IndexManager.mark_built_through`, which is the
one thing an index cannot know for itself -- that the replay just finished was complete. The
conservative direction is deliberate: an index never claims to cover a position it cannot show it
covers, and the cost of being wrong that way is a rebuild rather than a missing row.

*Every walk over a derived structure terminates.* A bucket is a chain of pages, and every walk
carries a visited set and a bound taken from the file's own page count, so damaged links fail
with a located error instead of hanging (amendment A42).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
)
from okto_grafx.domain.ids import (
    NO_CSN,
    NO_LSN,
    NO_PAGE,
    Csn,
    Lsn,
    PageIndex,
    RecordRef,
    SlotId,
)
from okto_grafx.domain.index.contract import SecondaryIndex, StagingTransaction
from okto_grafx.domain.index.definition import (
    INDEX_DIRECTORY,
    IndexDefinition,
    index_file,
)
from okto_grafx.domain.index.entry import INDEX_ENTRY_HEADER_SIZE, IndexEntry
from okto_grafx.domain.index.header import (
    INDEX_HEADER_SLOT,
    IndexHeader,
)
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.index.records import (
    IndexChange,
    IndexOperation,
    change_of,
    lsn_of,
    wal_record_for,
)
from okto_grafx.domain.index.visibility import (
    IndexVisibility,
    ReconcileReport,
    SnapshotLike,
    entry_visible,
    is_reclaimable,
)
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.page import (
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
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.buffer_pool import BufferPool, refuse_endless_chain, visited_pages
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "INDEX_DIRECTORY",
    "INDEX_FLAG_STALE",
    "INDEX_METRICS",
    "RECONCILIATION_TOTAL",
    "TOMBSTONE_BACKLOG",
    "HashIndex",
    "IndexDefinition",
    "IndexEntry",
    "IndexFinding",
    "IndexHeader",
    "IndexManager",
    "IndexStore",
    "IndexVisibility",
    "ProximityIndex",
    "ReconcileReport",
    "SecondaryIndex",
    "StagingTransaction",
    "index_file",
]

INDEX_FLAG_STALE: int = 0x01
"""Bit 0 of the index header flags: this index is behind the heap and may not be read.

The flag is durable on purpose. Staleness is discovered by comparing the position the file claims
against the position the database has published, and that comparison needs a number an index does
not carry; remembering the verdict where the file can be reopened means the answer survives a
restart that never asks the question again.
"""

TOMBSTONE_BACKLOG: str = "oktografx_vector_tombstone_backlog"
RECONCILIATION_TOTAL: str = "oktografx_vector_reconciliation_total"

INDEX_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name) for name in (TOMBSTONE_BACKLOG, RECONCILIATION_TOTAL)
)
"""The descriptors this component registers and emits, taken from the frozen catalog.

The names carry ``vector`` because CONTRACT.md section 9 has no generic secondary-index metric
and G7 forbids inventing one. They describe exactly what the proximity path here does -- entries
awaiting reconciliation, and reconciliation passes completed -- and SPEC-VEC FR-6 makes the
vector index the proximity index of this build, so C9 inherits the two series from this framework
instead of publishing a second set. Only a PROXIMITY index emits them; an exact index reconciles
as space reclamation and reports it in its return value alone. The gap is recorded as a contract
conflict rather than closed by a name nobody froze.
"""


@dataclass(frozen=True, slots=True)
class IndexFinding:
    """One disagreement between an index and the heap, located precisely (SPEC-M1 FR-11, AC-12).

    The fields are the ones CONTRACT.md section 8.6 gives a verification finding -- a kind, a
    location and an en-US detail. C6 owns the report these travel in; this is the value it
    collects from the index scope of ``verify()``.
    """

    kind: str
    index: str
    detail: str
    file: str
    page: PageIndex = NO_PAGE
    slot: SlotId = 0
    lsn: Lsn = NO_LSN
    ref: RecordRef | None = None

    def location(self) -> Mapping[str, object]:
        """Return the location of this finding as the mapping a report serialises."""
        return {
            "file": self.file,
            "page": self.page,
            "slot": self.slot,
            "lsn": self.lsn,
            "index": self.index,
        }


@dataclass(slots=True)
class _Staged:
    """The changes one transaction has staged into one index, in the order it staged them."""

    txn_id: int
    changes: list[IndexChange] = field(default_factory=list)


class IndexStore:
    """The paged store both kinds of index are built on: a file of bucket chains.

    It is deliberately NOT an index: it has no ``lookup``, because looking up is exactly the
    operation the two visibility contracts disagree about, and a shared implementation of it
    would be the place where one contract quietly answers for the other. Everything the two kinds
    genuinely share -- the file, the header, placement, the log records, reconciliation and the
    verification walk -- lives here and is written once.

    Layout of the file, following CONTRACT.md section 6.1 and amendment A2:

    * page 0 is the reserved header page. Slot 0 holds the file header C1 owns; slot 1 holds the
      index header of :mod:`okto_grafx.domain.index.header`.
    * pages 1 to ``bucket_count`` are the head pages of the buckets, in order, so the head of a
      bucket needs no lookup table to find.
    * every further page is an overflow page of some bucket, linked by ``next_page``.

    The store caches nothing derived from those links. That is a decision rather than an
    omission: a cached header or a cached tail would be a second answer to a question the page
    already answers, and it would need an invalidation proof of its own (amendments A63, A67).
    The one derived fact that IS kept is durable and self-describing -- the log position the
    header claims -- and the rule for trusting it is stated where it is read.
    """

    __slots__ = (
        "_definition",
        "_pool",
        "_metrics",
        "_staged",
        "_stale_reason",
        "_missing_targets",
        "_short_commit",
    )

    def __init__(
        self, definition: IndexDefinition, pool: BufferPool, metrics: MetricsSink
    ) -> None:
        """Build the store of one index over one buffer pool."""
        if not isinstance(definition, IndexDefinition):
            raise GrafxIndexError(
                f"An index store needs an IndexDefinition; got {type(definition).__name__}.",
                field="definition",
                value=type(definition).__name__,
            )
        self._definition: IndexDefinition = definition
        self._pool: BufferPool = pool
        self._metrics: MetricsSink = metrics
        self._staged: dict[int, _Staged] = {}
        self._stale_reason: str | None = None
        self._missing_targets: int = 0
        # The transaction whose part-applied commit is the ONLY reason this index is stale, or
        # None. It is what lets a retry of that same transaction lift the mark its own failure
        # set, and it is deliberately not a boolean: lifting a mark on the strength of "some
        # commit succeeded" would let an unrelated transaction clear a refusal it knows nothing
        # about.
        self._short_commit: int | None = None
        if metrics.enabled:
            for descriptor in INDEX_METRICS:
                metrics.register(descriptor)

    # --- identity ---------------------------------------------------------------------------

    @property
    def definition(self) -> IndexDefinition:
        """Return the definition this store was built for."""
        return self._definition

    @property
    def name(self) -> str:
        """Return the name of this index, which is also the name of its file."""
        return self._definition.name

    @property
    def visibility(self) -> IndexVisibility:
        """Return the visibility contract this index offers its callers."""
        return self._definition.visibility

    @property
    def file(self) -> str:
        """Return the paged file this index is stored in."""
        return self._definition.file

    @property
    def page_type(self) -> int:
        """Return the page type the pages of this index carry.

        The two codes of CONTRACT.md section 6.3 follow the visibility class, so a page says
        which contract wrote it and a verifier reading raw pages needs nothing else to tell them
        apart.
        """
        if self._definition.versioned:
            return int(PageType.INDEX_HNSW)
        return int(PageType.INDEX_HASH)

    @property
    def stale_reason(self) -> str | None:
        """Return why this index is stale, or None when it is not."""
        return self._stale_reason

    @property
    def missing_targets(self) -> int:
        """Return how many changes named an entry this index does not hold.

        Ending or removing an entry that is not there changes nothing and cannot produce a wrong
        answer, so it is not refused -- but it is never nothing either. A non-zero count on the
        live path means a caller is ending entries it never created, and on the redo path it
        means a replay met a tombstone whose insert did not survive. Both are worth seeing before
        a verification pass finds them.
        """
        return self._missing_targets

    def __repr__(self) -> str:
        """Return a representation naming the index, its class and its file."""
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"visibility={self.visibility.value!r}, file={self.file!r})"
        )

    # --- the file ---------------------------------------------------------------------------

    def exists(self) -> bool:
        """Return True when the file of this index has been created."""
        return self._pool.storage.exists(self.file)

    def is_created(self) -> bool:
        """Return True when page 0 of the file really is the header page of THIS index.

        A file that a redo grew before anything reserved page 0 exists, has pages, and is not
        created (amendment A22); saying so here is what lets :meth:`create` repair it instead of
        refusing it for good.
        """
        storage = self._pool.storage
        if not storage.exists(self.file) or storage.page_count(self.file) == 0:
            return False
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            if page.is_pristine():
                return False
            if page.page_type != int(PageType.META) or page.slot_count <= INDEX_HEADER_SLOT:
                return False
            self._require_file_header(page)
        return True

    def create(self) -> IndexHeader:
        """Create the file of this index, or open the one that is already there.

        Creating is safe to run on every open: an index whose file already carries this
        definition is opened rather than replaced, because G6 forbids a sanctioned operation
        destroying what is under ``index/``.
        """
        storage = self._pool.storage
        if not storage.exists(self.file):
            storage.create(self.file)
        if self.is_created():
            return self.open()
        self._reserve_header_page()
        self._grow_buckets()
        header = self.open()
        # The pages reach the DEVICE before this returns, and that is not a performance choice.
        # Creating an index is the one write this component makes that no log record covers:
        # nothing says "this file now has a header page and its buckets", so a crash before those
        # pages are written leaves a file whose every page is zero-filled. Nothing reading the
        # device can tell that state from damage -- a cold reader refuses the bucket walk, and
        # verification reports one finding per bucket on an index that is perfectly clean, which
        # is the cries-wolf failure that teaches an operator to ignore the verifier. Worse, C6
        # meets a zero page it can only classify as corruption and routes an intact database to
        # quarantine. It is a flush and NOT a barrier, for the reason section 8.5 step 6 gives:
        # durability of the ENTRIES is the log's business; this only has to make the structure
        # that holds them visible to every other process.
        self._pool.flush(self.file)
        return header

    def _reserve_header_page(self) -> None:
        """Turn page 0 into the reserved header page of this index file."""
        storage = self._pool.storage
        header = FileHeader(kind=FileKind.INDEX, page_size=self._pool.page_size)
        index_header = IndexHeader(
            visibility=self._definition.visibility,
            table_id=self._definition.table_id,
            bucket_count=self._definition.bucket_count,
            digest=self._definition.digest(),
        )
        if storage.page_count(self.file) == 0:
            page = self._pool.allocate(self.file, int(PageType.META))
            try:
                if page.page_index != HEADER_PAGE_INDEX:
                    raise GrafxCorruptionDetected(
                        f"The header page of {self.file!r} must be page {HEADER_PAGE_INDEX}; "
                        f"the device handed out {page.page_index}.",
                        file=self.file,
                        page=page.page_index,
                    )
                FileHeaderPage.initialize(page, header)
                page.insert_slot(index_header.encode())
            finally:
                self._pool.unpin(self.file, HEADER_PAGE_INDEX, dirty=True)
            return
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            if not page.is_pristine():
                raise GrafxCorruptionDetected(
                    f"Page {HEADER_PAGE_INDEX} of {self.file!r} has been written and is not the "
                    f"header page of this index, so it is not reserved over.",
                    file=self.file,
                    page=HEADER_PAGE_INDEX,
                    page_type=page.page_type,
                )
            FileHeaderPage.initialize(page, header)
            page.insert_slot(index_header.encode())

    def _grow_buckets(self) -> None:
        """Give every bucket a head page, repairing a file a redo grew before this ran."""
        storage = self._pool.storage
        wanted = 1 + self._definition.bucket_count
        while storage.page_count(self.file) < wanted:
            # reuse=False: the loop waits on the file's length, so a hand-out that does not
            # lengthen it just goes round again and spends an abandoned page on the way.
            page = self._pool.allocate(self.file, self.page_type, reuse=False)
            self._pool.unpin(self.file, page.page_index, dirty=True)
        for bucket in range(self._definition.bucket_count):
            index = self._bucket_head(bucket)
            with self._pool.pinned(self.file, index) as page:
                if page.is_pristine():
                    page.page_type = self.page_type
                    page.dirty = True

    def open(self) -> IndexHeader:
        """Return the header of this index file, refusing a file that is not its own.

        A digest that disagrees is NOT staleness and must not be repaired by rebuilding: a file
        written under a different definition answers a different question, and the honest reply
        is to refuse to open it.
        """
        header = self._read_header()
        definition = self._definition
        if header.digest != definition.digest():
            raise GrafxIndexError(
                f"The file {self.file!r} was written under a different definition of index "
                f"{definition.name!r}, so its entries do not answer this index's question.",
                field="digest",
                index=definition.name,
                file=self.file,
            )
        if header.visibility is not definition.visibility:
            raise GrafxIndexError(
                f"The file {self.file!r} holds a {header.visibility.value} index and this "
                f"definition declares a {definition.visibility.value} one.",
                field="visibility",
                index=definition.name,
                file=self.file,
            )
        wanted = 1 + header.bucket_count
        if self._pool.storage.page_count(self.file) < wanted:
            raise GrafxCorruptionDetected(
                f"Index {definition.name!r} declares {header.bucket_count} buckets, which needs "
                f"{wanted} pages; the file holds "
                f"{self._pool.storage.page_count(self.file)}.",
                file=self.file,
                field="bucket_count",
                value=header.bucket_count,
            )
        if header.flags & INDEX_FLAG_STALE and self._stale_reason is None:
            self._stale_reason = (
                f"Index {definition.name!r} was recorded as stale and has not been rebuilt."
            )
        return header

    def _require_file_header(self, page: Page) -> FileHeader:
        """Return the file header of page 0, refusing a page that is not an index header page."""
        header = FileHeaderPage.read(page)
        if header.kind is not FileKind.INDEX:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self.file!r} carries the header of a "
                f"{header.kind.name.lower()} file, not of an index.",
                file=self.file,
                page=HEADER_PAGE_INDEX,
                kind=header.kind.name,
            )
        if header.page_size != self._pool.page_size:
            raise GrafxCorruptionDetected(
                f"The index file {self.file!r} was written with pages of {header.page_size} "
                f"bytes, and this database uses {self._pool.page_size}.",
                file=self.file,
                page=HEADER_PAGE_INDEX,
                page_size=header.page_size,
            )
        return header

    def _read_header(self) -> IndexHeader:
        """Return the index header stored in slot 1 of the reserved header page."""
        storage = self._pool.storage
        if not storage.exists(self.file) or storage.page_count(self.file) == 0:
            raise GrafxIndexError(
                f"Index {self.name!r} has no file yet; create it before using it.",
                field="file",
                index=self.name,
                file=self.file,
            )
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            self._require_file_header(page)
            if page.slot_count <= INDEX_HEADER_SLOT:
                raise GrafxCorruptionDetected(
                    f"The header page of {self.file!r} carries no index header.",
                    file=self.file,
                    page=HEADER_PAGE_INDEX,
                    field="slot_count",
                    value=page.slot_count,
                )
            return IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))

    def _write_header(self, header: IndexHeader) -> None:
        """Replace the index header stored in slot 1 of the reserved header page."""
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            self._require_file_header(page)
            page.update_slot(INDEX_HEADER_SLOT, header.encode())

    @property
    def header(self) -> IndexHeader:
        """Return the header this file currently holds, read from the page every time."""
        return self._read_header()

    @property
    def built_through_lsn(self) -> Lsn:
        """Return the log position through which this index is known to reflect the heap."""
        return self._read_header().built_through_lsn

    @property
    def reconciled_through_lsn(self) -> Lsn:
        """Return the horizon the last reconciliation pass applied."""
        return self._read_header().reconciled_through_lsn

    # --- staleness --------------------------------------------------------------------------

    @property
    def stale(self) -> bool:
        """Return True while this index may not be read."""
        return self._stale_reason is not None

    def mark_stale(self, reason: str) -> None:
        """Record, durably, that this index is behind the heap and may not answer a lookup."""
        if not isinstance(reason, str) or not reason:
            raise GrafxIndexError(
                "Marking an index stale needs a reason a reader can act on.",
                field="reason",
                value=repr(reason),
                index=self.name,
            )
        self._stale_reason = reason
        header = self._read_header()
        if not header.flags & INDEX_FLAG_STALE:
            self._write_header(_with_flags(header, header.flags | INDEX_FLAG_STALE))
        # This is the SECOND write this component makes that no log record covers, and it is the
        # one whose loss is a wrong answer rather than wasted work. Nothing in the log says "this
        # index is behind the heap": the verdict is DERIVED, by comparing the position the file
        # claims against the position the database published, and redo cannot restore a
        # conclusion it never carried. Left in the page cache the mark dies with the process
        # while the file goes on saying the index is complete -- so recovery replays a log
        # holding no record for this index, declares the replay complete through
        # mark_built_through, and the guard that should refuse (an index already marked stale
        # refuses to move the position it claims) is VOID, because the mark never reached the
        # device. The index then opens fresh and answers a SHORT set, which is the one failure
        # this component exists to prevent.
        # Flushed whether or not the bit was just turned on: a header carrying the bit in the
        # cache but not on the device is exactly what a refused flush leaves behind, and skipping
        # the retry would make the second call weaker than the first. It is a flush and NOT a
        # barrier, for the reason section 8.5 step 6 gives -- what this owes is that every other
        # process, and this one after a restart, READS the refusal.
        self._pool.flush(self.file)

    def _mark_stale_after_failure(self, reason: str) -> None:
        """Mark this index stale after an operation left it part-applied, without masking why.

        The failure that brings us here is usually a device that stopped taking writes, so the
        DURABLE half of the mark can fail for the very same reason. The in-memory half cannot,
        and it is the half that stops this process answering; the durable half is attempted and
        its own failure is dropped, because the caller has to see the failure that broke the
        operation rather than the one that happened while recording it -- and a second exception
        raised from an except block would replace the first (L17: an uncertain signal resolves
        toward refusing to answer, never toward answering).

        Dropping it is safe to the extent that matters. The position this index claims never
        moved, so the file still says it covers less than the database has published, and the
        next freshness check calls it stale from the file alone. What the durable mark adds is
        the case where the position happens to agree -- a commit at a position the index already
        claimed -- and that is worth attempting even though it cannot be guaranteed here.
        """
        try:
            self.mark_stale(reason)
        except GrafxError:
            self._stale_reason = reason

    def _lift_short_commit_mark(self) -> None:
        """Take back the mark a part-applied commit set, once its retry has completed.

        Narrower than :meth:`clear_stale` in every way, and deliberately so. It moves no
        position -- the caller's own ``_advance`` does that, under the ordinary rule -- and it
        runs only when the transaction that set the mark is the transaction that just finished
        applying every one of its changes. A retry that converges is the only evidence a short
        index can offer that it is whole again, and refusing to read that evidence would send
        every transient budget refusal to a full rebuild of a structure that is already correct.
        """
        self._short_commit = None
        self._stale_reason = None
        header = self._read_header()
        if header.flags & INDEX_FLAG_STALE:
            self._write_header(_with_flags(header, header.flags & ~INDEX_FLAG_STALE))
            # The mark was flushed when it was set, so taking it back has to reach the device
            # too. Leaving the bit on the platter while this process believes it cleared would be
            # the durability defect of mark_stale in the mirror: every OTHER participant, and
            # this one after a restart, would go on refusing an index that is whole.
            self._pool.flush(self.file)

    def check_freshness(self, published_lsn: Lsn) -> bool:
        """Compare the position this index claims against the one the database published.

        This is the only detector of staleness in the component, and the flag it sets is the only
        gate the read path consults. Keeping detection and memory apart is what makes each of
        them testable on its own (amendment A67): a test can set the flag without a published
        position, and can move the published position without a flag already set.
        """
        if isinstance(published_lsn, bool) or not isinstance(published_lsn, int):
            raise GrafxIndexError(
                f"A published log position must be an integer; got "
                f"{type(published_lsn).__name__}.",
                field="published_lsn",
                value=repr(published_lsn),
                index=self.name,
            )
        if published_lsn < NO_LSN:
            raise GrafxIndexError(
                f"A published log position must not be negative; got {published_lsn}.",
                field="published_lsn",
                value=published_lsn,
                index=self.name,
            )
        header = self._read_header()
        if header.flags & INDEX_FLAG_STALE:
            if self._stale_reason is None:
                self._stale_reason = (
                    f"Index {self.name!r} was recorded as stale and has not been rebuilt."
                )
            return True
        if header.built_through_lsn < published_lsn:
            self.mark_stale(
                f"Index {self.name!r} covers the log through position "
                f"{header.built_through_lsn} and the database has published "
                f"{published_lsn}, so a lookup could omit a row."
            )
            return True
        return False

    def _require_readable(self) -> None:
        """Refuse a read of an index that is behind the heap.

        Refusing is the whole point. An index that answers while it is missing entries produces
        a result set that omits rows the caller is entitled to, and an omission is a wrong answer
        that looks exactly like an empty one.
        """
        if self._stale_reason is not None:
            raise GrafxIndexError(
                self._stale_reason,
                field="stale",
                index=self.name,
                file=self.file,
            )

    # --- staging ----------------------------------------------------------------------------

    def stage_insert(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage the entry a row insertion owes this index and return the record for the log.

        Nothing on any page moves here. The record goes into the transaction, and the entry
        appears only in :meth:`commit`, once the log has made the change durable and the real
        commit number is known. A transaction that aborts, or that optimistic validation refuses,
        therefore leaves the index exactly as it found it -- which is what keeps a proximity
        index from returning a row that was never committed.
        """
        return self._stage(
            txn,
            IndexChange(
                index=self.name,
                operation=IndexOperation.INSERT,
                key=self._require_key(key),
                ref=self._require_ref(ref),
                csn=self._require_csn("csn", csn) if self._definition.versioned else NO_CSN,
                versioned=self._definition.versioned,
            ),
        )

    def stage_delete(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage the end of an entry and return the record for the log.

        The entry is never dropped here, whichever kind of index this is. A snapshot older than
        ``csn`` still sees the row, so an entry removed at delete time would take that row out of
        the answer for every reader that is entitled to it. What the change records is WHEN the
        entry stopped applying: for a proximity index that stamp is the tombstone the snapshot
        rule reads, and for an exact index it is the stamp that tells a later reconciliation pass
        when no live snapshot could still want the entry.
        """
        stamp = self._require_csn("csn", csn)
        if stamp == NO_CSN:
            raise GrafxIndexError(
                "Ending an index entry needs the commit number at which it stopped applying, "
                "and zero is the value that means it never did.",
                field="csn",
                value=stamp,
                index=self.name,
            )
        return self._stage(
            txn,
            IndexChange(
                index=self.name,
                operation=IndexOperation.TOMBSTONE,
                key=self._require_key(key),
                ref=self._require_ref(ref),
                csn=stamp,
                versioned=self._definition.versioned,
            ),
        )

    def stage_reset(self, txn: StagingTransaction, built_through: Lsn) -> WalRecord:
        """Stage the clearing of every bucket, which is how a rebuild starts."""
        return self._stage(
            txn,
            IndexChange(
                index=self.name,
                operation=IndexOperation.RESET,
                csn=self._require_csn("built_through", built_through),
                versioned=self._definition.versioned,
            ),
        )

    def _stage(self, txn: StagingTransaction, change: IndexChange) -> WalRecord:
        """Put a change in this transaction's staging area and hand its record to the log."""
        txn_id = self._require_txn(txn)
        record = wal_record_for(
            change,
            epoch=int(getattr(txn, "epoch", 0) or 0),
            txn_id=txn_id,
        )
        staged = self._staged.get(txn_id)
        if staged is None:
            staged = _Staged(txn_id=txn_id)
            self._staged[txn_id] = staged
        staged.changes.append(change)
        txn.stage_record(record)
        return record

    def pending(self, txn: StagingTransaction) -> tuple[IndexChange, ...]:
        """Return the changes this transaction has staged into this index, in order."""
        staged = self._staged.get(self._require_txn(txn))
        return () if staged is None else tuple(staged.changes)

    def rollback(self, txn: StagingTransaction) -> int:
        """Drop everything this transaction staged, and return how many changes were dropped."""
        txn_id = self._require_txn(txn)
        staged = self._staged.pop(txn_id, None)
        if self._short_commit == txn_id:
            # This transaction's part-applied commit is why the index is stale, and abandoning it
            # is the caller saying the retry is not coming. The MARK stays -- the entries that
            # did land are still there and the ones that did not never will -- but the claim that
            # a retry could lift it is dropped, so a later transaction that happens to carry the
            # same number cannot inherit the right to clear a refusal it did not cause.
            self._short_commit = None
        return 0 if staged is None else len(staged.changes)

    def commit(self, txn: StagingTransaction, csn: Csn) -> int:
        """Apply everything this transaction staged, at the log position the commit received.

        The stamps inside the entries are the ones the caller declared when it staged them, and
        this method does not rewrite them. That is deliberate and it is what keeps the live path
        and the redo path identical: the log record carries the stamp the caller gave, so a
        replay of that record produces exactly the entry this call produces. An index that
        re-stamped here would answer one way when it was written and another way after a crash,
        which is a wrong result that only ever appears in recovery. The same number the caller
        passes here it passed to ``HeapStore.insert`` as the version's ``xmin``, so a
        disagreement between the two is a disagreement between the index and the heap -- and
        ``verify`` reports it rather than one side silently correcting the other.

        What the log position IS used for is the page stamp of CONTRACT.md section 6.3 and the
        position this index claims to cover. The claim moves only after EVERY staged change has
        landed, and a failure part of the way through -- a budget refusal, a device that stopped
        taking writes -- MARKS THE INDEX STALE before it re-raises.

        Leaving the position alone is not enough on its own, and the difference is measurable. A
        failed commit leaves entries on the pages and the position where it was, so the next
        freshness check does call the index stale -- but that check runs at ``open()``, and a
        caller that catches the failure and carries on meets a structure that is SHORT and says
        it is fine. Measured with a two-page budget and a device refusing its first write-back:
        1 of 24 entries applied, ``stale`` False, and 23 lookups answering EMPTY for rows the
        heap holds. Marking here closes the gap between the failure and the next open, which is
        the only window in which a short index answers.

        The mark a failure sets here is the ONE mark a retry can lift, and only the retry of the
        same transaction can lift it. The staging survives a refusal on purpose, so a retry
        re-applies every change from the start -- safe because applying a change twice is a
        no-op -- and a retry that gets all the way through is the evidence, and the only
        evidence, that the index is whole again. Anything else that made it stale stands: a
        freshness verdict is about the log position and no commit answers it, and a rebuild is
        the only thing that clears one. That asymmetry is why the transaction is remembered by
        NUMBER rather than by a flag.
        """
        txn_id = self._require_txn(txn)
        stamp = self._require_csn("csn", csn)
        staged = self._staged.get(txn_id)
        if staged is None:
            return 0
        applied = 0
        for change in staged.changes:
            try:
                self._apply_change(change, stamp)
            except GrafxError as failure:
                if self._stale_reason is None:
                    # Only a commit that found the index HEALTHY may claim to be the reason it is
                    # stale, because only then is a completed retry proof that nothing else is
                    # wrong. Failing while the index was already stale leaves that older verdict
                    # owning the mark, and it needs a rebuild rather than a retry.
                    self._short_commit = txn_id
                self._mark_stale_after_failure(
                    f"Applying a commit to index {self.name!r} failed after {applied} of "
                    f"{len(staged.changes)} changes, so it is missing entries the heap holds: "
                    f"{failure.message}"
                )
                raise
            applied += 1
        self._staged.pop(txn_id, None)
        if self._short_commit == txn_id:
            self._lift_short_commit_mark()
        self._advance(stamp)
        if self._metrics.enabled and self._definition.versioned:
            self._metrics.set_gauge(TOMBSTONE_BACKLOG, float(self._tombstone_backlog()))
        return applied

    def advance_built_through(self, lsn: Lsn) -> None:
        """Raise the position this index claims to cover, unless it is known to be stale.

        The caller must have put every record up to that position into this index, or know that
        something else did. Recovery is the case that needs it: after a complete replay every
        index has seen everything, and without a way to say so an index that the final commits
        happened to miss would be rebuilt for no reason on every open after a crash.
        """
        self._advance(_require_position("lsn", lsn))
        # The conservative half of the family mark_stale belongs to. Recovery's declaration that
        # a replay was complete is not in the log either -- the log records what CHANGED and this
        # records that nothing more will -- so losing it costs a rebuild nobody needed and never
        # a wrong answer, because the position falls BACK and a position that is behind is
        # precisely what marks an index stale. Flushed anyway: this is the door recovery calls
        # once per index per open, the page is dirty already, and leaving one of the four writers
        # of this header un-flushed would be a rule the next reader has to reconstruct from a
        # silence.
        # The flush belongs HERE and not in _advance, which is the same write on the commit and
        # redo paths. There the position IS derivable from the log -- replaying the records
        # re-advances it -- and flushing per record would turn a replay into one device write per
        # record for a number redo reconstructs for free (see the note on apply()).
        self._pool.flush(self.file)

    def clear_stale(self, built_through: Lsn) -> None:
        """Declare this index rebuilt through a position, so it may answer lookups again.

        The position is not optional and it is not derived. A stale index refuses to move the
        position it claims -- that is what keeps it from quietly looking fresh again -- so
        clearing the flag without saying what the rebuild covered would leave the file claiming
        the position it had while it was broken, and the very next freshness check would call it
        stale again. The repair would not stick, and nothing would say why.
        """
        position = _require_position("built_through", built_through)
        self._stale_reason = None
        header = self._read_header()
        cleared = _with_flags(header, header.flags & ~INDEX_FLAG_STALE)
        self._write_header(cleared.advanced_to(position))
        # Clearing the mark is unlogged for the same reason setting it is, and it errs the other
        # way: a repair that never reached the device leaves the file still saying stale, so the
        # next open rebuilds an index that was already rebuilt. Wasted work, never a wrong
        # answer. It is flushed because the pair must be symmetrical -- a durable mark that only
        # a cached clear can undo would mean an index this process believes repaired and every
        # other process still refuses, and two participants disagreeing about whether an index
        # may answer is worse than either verdict.
        self._pool.flush(self.file)

    def _advance(self, lsn: Lsn) -> None:
        """Raise the position this index claims to cover, unless it is known to be stale."""
        if self._stale_reason is not None:
            # An index that is missing entries does not become complete by seeing a newer commit.
            # Letting the position move here is how a stale index would quietly stop looking
            # stale while still omitting every row it never received.
            return
        header = self._read_header()
        advanced = header.advanced_to(lsn)
        if advanced is not header:
            self._write_header(advanced)

    # --- redo -------------------------------------------------------------------------------

    def apply(self, record: WalRecord) -> None:
        """Redo one log record against this index, idempotently.

        Idempotence is on the ENTRY and not on the page: inserting a key and location the index
        already holds changes nothing, ending an entry already ended changes nothing, and
        removing an entry already gone changes nothing. That property is what makes replaying the
        same log twice produce the same index however the entries happen to be laid out, which a
        page-position rule could not promise once a reconciliation pass has moved them.

        Two things about the record are checked, and the NAME is only the first of them. The
        staging path stamps every change with this index's own visibility class, so a change can
        only carry the wrong one if it came off a device -- a damaged flag byte, or a record
        written when this name meant a different index. Storing it would put an entry of the
        wrong shape in the file, and the shape is what the whole read path is built on: an
        unversioned entry in a proximity index carries no birth stamp, so every snapshot
        comparison over that bucket raises instead of answering. Measured on the pristine build,
        one such record made ``lookup`` on that key AND ``visible_entries`` for the whole index
        raise, while ``verify()`` reported nothing -- an index that cannot be read and cannot be
        diagnosed. Refusing here keeps the damage in the record where C6 can classify it, rather
        than in the file where nothing can.
        """
        change = change_of(record)
        if change.index != self.name:
            raise GrafxIndexError(
                f"Record for index {change.index!r} was offered to index {self.name!r}.",
                field="index",
                value=change.index,
                index=self.name,
            )
        if change.versioned != self._definition.versioned:
            raise GrafxCorruptionDetected(
                f"A record for index {self.name!r} describes a "
                f"{'versioned' if change.versioned else 'unversioned'} entry and this index "
                f"stores {'versioned' if self._definition.versioned else 'unversioned'} ones, "
                f"so the entry it asks for could not be read back.",
                field="versioned",
                value=change.versioned,
                index=self.name,
                file=self.file,
                operation=change.operation.name,
            )
        position = lsn_of(record)
        self._apply_change(change, position)
        self._advance(position)

    # --- reading ----------------------------------------------------------------------------

    def candidates(self, key: bytes) -> tuple[IndexEntry, ...]:
        """Return every stored entry under this key, in the order the bucket holds them.

        The order is a property of the walk -- pages in chain order, slots in slot order -- so
        two runs over the same file answer identically and a caller may rely on the sequence.
        """
        self._require_readable()
        wanted = self._require_key(key)
        found: list[IndexEntry] = []
        for page_index in self._bucket_pages(bucket_of(wanted, self._definition.bucket_count)):
            for entry in self._entries_on(page_index):
                if entry.key == wanted:
                    found.append(entry)
        return tuple(found)

    def walk(self) -> tuple[IndexEntry, ...]:
        """Return every stored entry of this index, bucket by bucket.

        This door deliberately does NOT refuse a stale index, unlike every read that answers a
        question about rows. A stale index is exactly the one a verifier most needs to look at,
        and a walk makes no claim of completeness: it reports what is stored, which is the input
        to deciding what is missing.

        The whole index is materialised rather than yielded lazily, so no page stays pinned while
        a caller decides what to do with an entry. A generator that a caller abandons half way
        would leave a pin behind, and a pinned page can never be evicted (FR-13).
        """
        entries: list[IndexEntry] = []
        for bucket in range(self._definition.bucket_count):
            for page_index in self._bucket_pages(bucket):
                entries.extend(self._entries_on(page_index))
        return tuple(entries)

    def _entries_on(self, page_index: PageIndex) -> tuple[IndexEntry, ...]:
        """Return the entries stored on one page, tagged with where each one lives."""
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            return tuple(
                IndexEntry.decode(payload).located_at(page_index, slot)
                for slot, payload in page.iter_slots()
            )

    # --- reconciliation ---------------------------------------------------------------------

    def reconcile(
        self, horizon: Lsn, txn: StagingTransaction | None = None
    ) -> ReconcileReport:
        """Remove the entries no live snapshot can still want, and log every removal.

        For a PROXIMITY index this is the mechanism SD-3 names: a tombstoned entry is what keeps
        an older snapshot answering correctly, and it may only be dropped once the horizon has
        passed it. For an EXACT index the same pass is space reclamation and nothing more --
        exact lookups never consult a stamp -- but it is offered rather than refused, because the
        protocol gives every index this method and an exact index with no way to reclaim would
        grow for as long as the database lives with no sanctioned remedy. The safety argument is
        identical in both cases and is stated once, in
        :func:`okto_grafx.domain.index.visibility.is_reclaimable`.

        The transaction is what makes a removal legal. Every removal is staged as an
        ``INDEX_RECONCILE`` record and applied when that transaction commits, so the pass is
        replayable and reversible by replay exactly as SPEC-VEC BR-3 requires. Called with no
        transaction -- which is how CONTRACT.md section 8.7 spells the signature -- the pass
        MEASURES instead: it reports how much the horizon has released and how much it holds
        back, and removes nothing, because a cleanup the log never saw is the one thing BR-3
        forbids outright.
        """
        if txn is not None:
            self._require_txn(txn)
        scanned = 0
        reclaimable = 0
        removed = 0
        retained = 0
        pages: set[PageIndex] = set()
        for entry in self.walk():
            scanned += 1
            if entry.live:
                continue
            if not is_reclaimable(entry, horizon):
                retained += 1
                continue
            reclaimable += 1
            pages.add(entry.page)
            if txn is None:
                continue
            self._stage(
                txn,
                IndexChange(
                    index=self.name,
                    operation=IndexOperation.REMOVE,
                    key=entry.key,
                    ref=entry.ref,
                    csn=horizon,
                    versioned=entry.versioned,
                ),
            )
            removed += 1
        return ReconcileReport(
            index=self.name,
            horizon=horizon,
            scanned=scanned,
            reclaimable=reclaimable,
            removed=removed,
            retained=retained,
            pages_touched=len(pages),
        )

    def note_reconciled(self, horizon: Lsn) -> None:
        """Record the horizon a completed reconciliation pass applied.

        A verification walk needs this number to tell an entry that was correctly reclaimed from
        one that went missing: below the recorded horizon an absent entry is the pass working,
        and above it the same absence is a divergence worth reporting.
        """
        header = self._read_header()
        reconciled = header.reconciled_to(horizon)
        if reconciled is not header:
            self._write_header(reconciled)
        # The fourth unlogged write, and it is NOT in the conservative half. The REMOVALS a pass
        # made are covered -- every one travels as an INDEX_RECONCILE record and reaches the
        # device when the transaction carrying it commits -- but the horizon itself is covered by
        # nothing, and replaying those records does not restore it: apply() erases entries, it
        # never calls reconciled_to. Lose the horizon while keeping the removals and the two
        # halves disagree in the dangerous direction: _verify_coverage reads this number to tell
        # an entry a pass correctly reclaimed from one that went missing, so with the number back
        # at zero every reclaimed row becomes a missing_entry finding. Measured on a cold pool
        # over the same device: verify() reports missing_entry on an index that is perfectly
        # clean. That is the cries-wolf failure create() flushes to avoid, arriving through a
        # different door, and it teaches an operator to ignore the verifier.
        self._pool.flush(self.file)
        if self._metrics.enabled and self._definition.versioned:
            self._metrics.increment(RECONCILIATION_TOTAL)
            self._metrics.set_gauge(TOMBSTONE_BACKLOG, float(self._tombstone_backlog()))

    def _tombstone_backlog(self) -> int:
        """Return how many entries carry a tombstone that has not been reclaimed yet."""
        return sum(1 for entry in self.walk() if not entry.live)

    # --- applying -----------------------------------------------------------------------------

    def _apply_change(self, change: IndexChange, lsn: Lsn) -> bool:
        """Apply one change to the pages and say whether anything moved."""
        if change.operation is IndexOperation.RESET:
            return self._reset(lsn)
        bucket = bucket_of(change.key, self._definition.bucket_count)
        pages = self._bucket_pages(bucket)
        located = self._find_entry(pages, change.key, change.ref)
        if change.operation is IndexOperation.INSERT:
            # The redo path never went through staging, so the key it carries is checked here as
            # well. Both sites call the same helper: a size rule written twice is a size rule
            # that can disagree with itself (A66.1).
            self._require_key(change.key)
            if located is not None:
                return False
            return self._place(
                pages,
                IndexEntry(
                    key=change.key,
                    ref=change.ref,
                    versioned=change.versioned,
                    born_csn=change.csn if change.versioned else NO_CSN,
                ),
                lsn,
            )
        if located is None:
            # The entry this change is about is not here. On the redo path that means the insert
            # it followed was discarded with a broken tail, and on the live path it means the
            # caller ended an entry it never created -- by naming the wrong row version, say.
            # Neither can produce a wrong answer, because an entry that is absent returns
            # nothing, and raising would let a replay wedge a database that recovery is in the
            # middle of repairing. It is COUNTED rather than merely ignored: a change that did
            # nothing is otherwise indistinguishable from one that worked, and a caller ending
            # entries that do not exist should be able to see that it is doing so without
            # waiting for a verification pass to tell it.
            self._missing_targets += 1
            return False
        page_index, slot, entry = located
        if change.operation is IndexOperation.TOMBSTONE:
            if not entry.live:
                return False
            return self._rewrite(page_index, slot, entry.ended_at(change.csn), lsn)
        return self._erase(page_index, slot, lsn)

    def _reset(self, lsn: Lsn) -> bool:
        """Clear every entry of every bucket, keeping the pages and the chains they form."""
        for bucket in range(self._definition.bucket_count):
            for page_index in self._bucket_pages(bucket):
                with self._pool.pinned(self.file, page_index) as page:
                    self._require_index_page(page, page_index)
                    page.clear()
                    self._stamp(page, lsn)
        return True

    def _place(self, pages: Sequence[PageIndex], entry: IndexEntry, lsn: Lsn) -> bool:
        """Store a new entry in the first page of the chain that can hold it.

        Whether a page can hold the entry is decided by the page itself, by attempting the
        insertion and catching its refusal, rather than by a second copy of its space arithmetic
        here. A predicate written twice is a predicate that can disagree with itself, and the
        page already accounts for the directory entry and for what compacting would recover.
        """
        payload = entry.encode()
        for page_index in pages:
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                try:
                    page.insert_slot(payload)
                except PageFullError:
                    continue
                self._stamp(page, lsn)
                return True
        return self._place_on_new_page(pages, payload, lsn)

    def _place_on_new_page(
        self, pages: Sequence[PageIndex], payload: bytes, lsn: Lsn
    ) -> bool:
        """Add one page to the end of the chain and store the entry on it."""
        if not pages:
            raise GrafxCorruptionDetected(
                f"A bucket of {self.file!r} has no head page, so an entry has nowhere to go.",
                file=self.file,
                field="bucket",
            )
        fresh = self._pool.allocate(self.file, self.page_type)
        index = fresh.page_index
        try:
            # An entry too large for an empty page was refused before it was ever staged, and
            # again before this change was applied, so this insertion cannot fail. A third check
            # here would be one no input could reach, which is dead code wearing a guard (A34).
            fresh.insert_slot(payload)
            self._stamp(fresh, lsn)
        finally:
            self._pool.unpin(self.file, index, dirty=True)
        # The page is filled BEFORE it is linked. A failure between the two leaves a page that no
        # chain reaches, which costs space G6 forbids reclaiming here but loses nothing and
        # returns nothing; linking first and failing after would put an unreadable page in the
        # middle of a bucket, and every later walk of that bucket would refuse.
        with self._pool.pinned(self.file, pages[-1]) as tail:
            self._require_index_page(tail, pages[-1])
            tail.next_page = index
            tail.dirty = True
        return True

    def _rewrite(self, page_index: PageIndex, slot: SlotId, entry: IndexEntry, lsn: Lsn) -> bool:
        """Replace an entry in place, which a stamp change always fits because it is fixed width."""
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            page.update_slot(slot, entry.encode())
            self._stamp(page, lsn)
        return True

    def _erase(self, page_index: PageIndex, slot: SlotId, lsn: Lsn) -> bool:
        """Free the slot an entry occupied, and reclaim the directory when the page empties."""
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            page.free_slot(slot)
            if not page.live_slots():
                # clear() drops the slot directory as well as the payloads, and keeps the header
                # fields -- the page type and the next_page link -- so the page stays in its
                # chain and can be filled again. Freeing slots alone would leave the directory
                # growing forever, and a page whose directory has eaten its payload area cannot
                # hold anything even after compacting.
                page.clear()
            self._stamp(page, lsn)
        return True

    def _stamp(self, page: Page, lsn: Lsn) -> None:
        """Record on the page the log position of the last record applied to it."""
        if lsn > page.page_lsn:
            page.page_lsn = lsn
        page.dirty = True

    # --- walking ------------------------------------------------------------------------------

    def _bucket_head(self, bucket: int) -> PageIndex:
        """Return the head page of a bucket: buckets follow the header page, in order."""
        return bucket + 1

    def _bucket_pages(self, bucket: int) -> tuple[PageIndex, ...]:
        """Return the pages of a bucket, in chain order, refusing a chain that does not end.

        Termination rests on two independent guards, as every chain walk in this build does: a
        visited set, and a bound taken from the number of pages the file actually has. A chain
        cannot legitimately be longer than the file, so the bound can never refuse a walk that is
        merely unusual -- and it is what turns a damaged link into a located failure rather than
        into a process that never returns (amendments A34, A42).
        """
        if isinstance(bucket, bool) or not isinstance(bucket, int):
            raise GrafxIndexError(
                f"A bucket must be named by an integer; got {type(bucket).__name__}.",
                field="bucket",
                value=repr(bucket),
                index=self.name,
            )
        if not 0 <= bucket < self._definition.bucket_count:
            raise GrafxIndexError(
                f"Index {self.name!r} has {self._definition.bucket_count} buckets; got "
                f"{bucket}.",
                field="bucket",
                value=bucket,
                index=self.name,
            )
        pages: list[PageIndex] = []
        seen: set[PageIndex] = visited_pages()
        limit = self._pool.storage.page_count(self.file) + 1
        index: PageIndex = self._bucket_head(bucket)
        while index != NO_PAGE:
            refuse_endless_chain(self.file, len(pages) + 1, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The bucket chain of {self.file!r} returns to page {index}, so it is a "
                    f"cycle.",
                    file=self.file,
                    page=index,
                    field="cycle",
                )
            seen.add(index)
            with self._pool.pinned(self.file, index) as page:
                self._require_index_page(page, index)
                following = page.next_page
            pages.append(index)
            index = following
        return tuple(pages)

    def _find_entry(
        self, pages: Sequence[PageIndex], key: bytes, ref: RecordRef
    ) -> tuple[PageIndex, SlotId, IndexEntry] | None:
        """Return where the entry for this key and heap location lives, or None when it does not."""
        for page_index in pages:
            for entry in self._entries_on(page_index):
                if entry.matches(key, ref):
                    return page_index, entry.slot, entry
        return None

    def _require_index_page(self, page: Page, page_index: PageIndex) -> None:
        """Refuse a page of a bucket chain that is not a page of this index."""
        if page.page_type != self.page_type:
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {self.file!r} is of type {page.page_type} and this index "
                f"stores pages of type {self.page_type}.",
                file=self.file,
                page=page_index,
                page_type=page.page_type,
                field="page_type",
            )

    # --- argument checks ------------------------------------------------------------------------

    @property
    def max_key_bytes(self) -> int:
        """Return the longest key this index can store, which one page decides.

        An entry lives in a single slot: it is never split across pages, because an entry that
        spanned a chain could be half-read by a walk that meets a damaged link. So the longest
        key is what remains of an empty page once the page header, one directory entry and the
        fixed part of an entry have been taken out of it.
        """
        return (
            self._pool.page_size
            - PAGE_HEADER_SIZE
            - SLOT_ENTRY_SIZE
            - INDEX_ENTRY_HEADER_SIZE
        )

    def _require_key(self, key: object) -> bytes:
        """Return the key when it is one this index can store, else refuse it.

        The size is checked here rather than where the entry is placed, so an entry that could
        never be stored is refused before a log record for it exists. A record that no replay
        could apply is worse than a refusal: it is a log that cannot be replayed.
        """
        if not isinstance(key, (bytes, bytearray, memoryview)):
            raise GrafxIndexError(
                f"An index key must be bytes; got {type(key).__name__}.",
                field="key",
                value=type(key).__name__,
                index=self.name,
            )
        material = bytes(key)
        if len(material) > self.max_key_bytes:
            raise GrafxIndexError(
                f"An index entry lives in one slot, so index {self.name!r} stores a key of at "
                f"most {self.max_key_bytes} bytes on a page of {self._pool.page_size}; got "
                f"{len(material)}.",
                field="key",
                value=len(material),
                index=self.name,
                limit=self.max_key_bytes,
            )
        return material

    def _require_ref(self, ref: object) -> RecordRef:
        """Return the heap location when it is one, else refuse it."""
        if not isinstance(ref, RecordRef):
            raise GrafxIndexError(
                f"An index entry points at a RecordRef; got {type(ref).__name__}.",
                field="ref",
                value=type(ref).__name__,
                index=self.name,
            )
        return ref

    def _require_csn(self, field_name: str, csn: object) -> Csn:
        """Return the commit number when it is one, else refuse it."""
        if isinstance(csn, bool) or not isinstance(csn, int):
            raise GrafxIndexError(
                f"An index change needs an integer {field_name}; got {type(csn).__name__}.",
                field=field_name,
                value=repr(csn),
                index=self.name,
            )
        if csn < NO_CSN:
            raise GrafxIndexError(
                f"An index change needs a non-negative {field_name}; got {csn}.",
                field=field_name,
                value=csn,
                index=self.name,
            )
        return csn

    def _require_txn(self, txn: object) -> int:
        """Return the transaction number, refusing anything that cannot stage a record."""
        if not isinstance(txn, StagingTransaction):
            raise GrafxIndexError(
                "An index change is staged on a transaction that carries a txn_id and can stage "
                f"a record; got {type(txn).__name__}.",
                field="txn",
                value=type(txn).__name__,
                index=self.name,
            )
        txn_id = txn.txn_id
        if isinstance(txn_id, bool) or not isinstance(txn_id, int) or txn_id < 0:
            raise GrafxIndexError(
                f"A transaction number must be a non-negative integer; got {txn_id!r}.",
                field="txn_id",
                value=repr(txn_id),
                index=self.name,
            )
        return txn_id


class HashIndex(IndexStore):
    """The reference EXACT index of M1: a hash index whose hits are candidates.

    ``lookup`` deliberately takes the snapshot and does not use it. That is not an oversight and
    it must not be "fixed": an exact entry carries no birth stamp, so this index has no way to
    decide visibility, and any filter applied here would be a guess. The snapshot is in the
    signature because the protocol puts it there for both kinds, and the parameter is what tells
    a reader of this class that the question was asked and answered.
    """

    __slots__ = ()

    def __init__(
        self, definition: IndexDefinition, pool: BufferPool, metrics: MetricsSink
    ) -> None:
        """Build the index, refusing a definition that does not declare the exact contract."""
        if IndexVisibility.parse(definition.visibility) is not IndexVisibility.EXACT:
            raise GrafxIndexError(
                f"A HashIndex realises the exact contract; definition {definition.name!r} "
                f"declares {definition.visibility.value}.",
                field="visibility",
                value=definition.visibility.value,
                index=definition.name,
            )
        super().__init__(definition, pool, metrics)

    def lookup(self, key: bytes, snapshot: SnapshotLike) -> tuple[RecordRef, ...]:
        """Return the CANDIDATE heap locations stored under this key.

        The result is a superset: it may name a row that was deleted, superseded, or committed
        after the snapshot opened. Validating each hit against the heap under the caller's own
        snapshot is mandatory, and :meth:`IndexManager.lookup` is where that happens.
        """
        if not isinstance(snapshot, SnapshotLike):
            raise GrafxIndexError(
                f"A lookup needs a snapshot; got {type(snapshot).__name__}.",
                field="snapshot",
                value=type(snapshot).__name__,
                index=self.name,
            )
        return tuple(entry.ref for entry in self.candidates(key))


class ProximityIndex(IndexStore):
    """The PROXIMITY contract realised over the same paged store: versioned entries, tombstones.

    This is the seam SPEC-VEC FR-6 and TR-3 describe, and it is deliberately a working index
    rather than an interface: the visibility rule, the tombstone, the horizon-bounded
    reconciliation and the log coverage are all here, so a vector index adds a graph and inherits
    the part that decides correctness instead of building a second lifetime of its own.

    ``lookup`` is authoritative. Nothing it returns is validated against the heap, so an entry
    whose stamps disagree with its row is a wrong result rather than a slow one -- which is why
    tombstones travel inside the same commit as the heap change and why ``verify`` compares every
    stamp against the version it points at.
    """

    __slots__ = ()

    def __init__(
        self, definition: IndexDefinition, pool: BufferPool, metrics: MetricsSink
    ) -> None:
        """Build the index, refusing a definition that does not declare the proximity contract."""
        if IndexVisibility.parse(definition.visibility) is not IndexVisibility.PROXIMITY:
            raise GrafxIndexError(
                f"A ProximityIndex realises the proximity contract; definition "
                f"{definition.name!r} declares {definition.visibility.value}.",
                field="visibility",
                value=definition.visibility.value,
                index=definition.name,
            )
        super().__init__(definition, pool, metrics)

    def lookup(self, key: bytes, snapshot: SnapshotLike) -> tuple[RecordRef, ...]:
        """Return exactly the heap locations this snapshot may see under this key."""
        return tuple(
            entry.ref
            for entry in self.candidates(key)
            if entry_visible(entry, snapshot)
        )

    def visible_entries(self, snapshot: SnapshotLike) -> tuple[IndexEntry, ...]:
        """Return every entry of the whole index this snapshot may see.

        A proximity structure rarely looks a key up: it navigates. This is the door that gives a
        navigator the same visibility answer the keyed lookup gives, so a graph built on top of
        this store never has to re-derive the rule -- and never has to consult the heap.
        """
        self._require_readable()
        return tuple(
            entry for entry in self.walk() if entry_visible(entry, snapshot)
        )


def _tables_written_by(txn: object) -> frozenset[int] | None:
    """Return the ids of the tables this transaction wrote rows of, or None if it cannot say.

    None is not "no tables". It means the transaction does not describe its row intents in a shape
    this can read, and the caller must then assume the worst -- that any index may have been owed
    an entry -- rather than granting freshness it cannot justify.
    """
    intents = getattr(txn, "row_intents", None)
    if intents is None:
        return None
    tables: set[int] = set()
    for intent in intents:
        table_id = getattr(getattr(intent, "table", None), "table_id", None)
        if not isinstance(table_id, int) or isinstance(table_id, bool):
            return None  # an intent whose table cannot be named makes the whole answer unsafe
        tables.add(table_id)
    return frozenset(tables)


PRIMARY_KEY_INDEX_PREFIX: str = "pk_"
"""What the index covering a table's declared PRIMARY KEY is named after."""


def primary_key_index_name(table_name: str) -> str:
    """Return the name of the index covering that table's primary key.

    One function rather than an f-string at each site, because the name is a FILE name: the DDL
    that creates the index and the composition that re-adopts it on the next open must produce
    the same string or the second one creates a second, empty index beside the first.
    """
    return f"{PRIMARY_KEY_INDEX_PREFIX}{table_name}"


def primary_key_index(
    table: object, pool: BufferPool, metrics: MetricsSink
) -> HashIndex | None:
    """Return the exact index covering that table's primary key, or None if it declares none.

    A table's primary key is the column every keyed read names, so without this the engine had a
    complete index framework and no index: `CREATE NODE TABLE ... PRIMARY KEY(id)` registered
    nothing, `_index_for` searched an empty list, and every `WHERE id = $k` planned a full heap
    scan. Measured before this existed, a point read cost 5.97 ms at 200 rows, 25.4 ms at 800 and
    57.4 ms at 3200 -- linear in the table, against a D5 ceiling of five times a reference engine's
    ~0.9 ms. Insertion carried the same cost through its uniqueness check, which made a bulk load
    quadratic, and a two-pattern MATCH multiplied two scans.

    EXACT visibility, so the hit is a candidate and `IndexManager.lookup` validates it against the
    heap under the caller's snapshot (CONTRACT.md section 8.7). That is what lets the index be a
    superset without being a wrong answer, and it is why a primary key can be indexed at all
    without the index having to know about visibility.

    A relationship table has no primary key and gets none.
    """
    primary = getattr(table, "primary_key", None)
    if not primary:
        return None
    return HashIndex(
        IndexDefinition(
            name=primary_key_index_name(table.name),
            table_id=table.table_id,
            table_name=table.name,
            positions=(table.column_index(primary),),
            visibility=IndexVisibility.EXACT,
        ),
        pool,
        metrics,
    )


class IndexManager:
    """The registry of the indexes of one database, and the door callers use (SPEC-M1 FR-12).

    It owns three things no single index can: which indexes cover which table, the validation an
    exact lookup must not skip, and the comparison against the heap that ``verify`` reports.
    """

    __slots__ = ("_pool", "_heap", "_metrics", "_indexes", "_published_lsn")

    def __init__(self, pool: BufferPool, heap: HeapStore, metrics: MetricsSink) -> None:
        """Build the registry over the pool and heap of one database."""
        self._pool: BufferPool = pool
        self._heap: HeapStore = heap
        self._metrics: MetricsSink = metrics
        self._indexes: dict[str, IndexStore] = {}
        self._published_lsn: Lsn = NO_LSN

    # --- registry ---------------------------------------------------------------------------

    def register(
        self, index: IndexStore, *, complete_through: Lsn | None = None
    ) -> IndexStore:
        """Register an index, create its file if it has none, and check that it is fresh.

        ``complete_through`` is for the one caller that KNOWS the index it is registering has
        nothing to catch up on: the statement that creates a table declares its primary key and
        the table is empty, so the index covers everything there is to cover at the position the
        database has published. It is applied BEFORE the freshness check, and the order is the
        whole point -- `_advance` refuses to move an index that is already marked stale, so an
        advance after the check is a no-op and the index stays stale for ever. That is what
        happened: every table declared in a session after the first got an index that was marked
        stale on the spot and that no amount of loading could lift.
        """
        if not isinstance(index, IndexStore):
            raise GrafxIndexError(
                f"An index registered here is built on the paged store; got "
                f"{type(index).__name__}.",
                field="index",
                value=type(index).__name__,
            )
        if not isinstance(index, SecondaryIndex):
            raise GrafxIndexError(
                f"An index must answer the whole contract of CONTRACT.md section 8.7; "
                f"{type(index).__name__} does not.",
                field="contract",
                value=type(index).__name__,
                index=index.name,
            )
        key = index.definition.registry_key
        existing = self._indexes.get(key)
        if existing is not None:
            raise GrafxIndexError(
                f"Index {existing.name!r} is already registered; names are compared without "
                f"case because they become file names.",
                field="name",
                value=index.name,
                index=existing.name,
            )
        index.create()
        self._indexes[key] = index
        if complete_through is not None:
            index.advance_built_through(complete_through)
        index.check_freshness(self._published_lsn)
        return index

    def indexes(self) -> tuple[IndexStore, ...]:
        """Return every registered index, in the order names sort, so a report is reproducible."""
        return tuple(self._indexes[key] for key in sorted(self._indexes))

    def index(self, name: str) -> IndexStore:
        """Return the index of that name, refusing a name nothing was registered under."""
        if not isinstance(name, str):
            raise GrafxIndexError(
                f"An index is named by a string; got {type(name).__name__}.",
                field="name",
                value=type(name).__name__,
            )
        found = self._indexes.get(name.lower())
        if found is None:
            known = ", ".join(repr(index.name) for index in self.indexes()) or "none"
            raise GrafxIndexError(
                f"No index named {name!r} is registered; registered indexes are {known}.",
                field="name",
                value=name,
            )
        return found

    def indexes_for(self, table_id: int) -> tuple[IndexStore, ...]:
        """Return every registered index that covers this table."""
        return tuple(
            index for index in self.indexes() if index.definition.table_id == table_id
        )

    # --- freshness --------------------------------------------------------------------------

    @property
    def published_lsn(self) -> Lsn:
        """Return the log position this manager was last told the database had published."""
        return self._published_lsn

    def open(self, published_lsn: Lsn) -> tuple[IndexStore, ...]:
        """Check every registered index against the position the database has published.

        Returns the indexes that are stale, which is what a caller needs in order to decide
        between rebuilding them and running without them. Nothing is repaired here: a repair
        writes to the log and therefore belongs inside a transaction the caller owns.
        """
        self._published_lsn = _require_position("published_lsn", published_lsn)
        return tuple(
            index for index in self.indexes() if index.check_freshness(self._published_lsn)
        )

    def mark_built_through(self, lsn: Lsn) -> None:
        """Declare that every log record up to this position has reached every index.

        The caller must have replayed the whole log, or written every record itself. It exists
        because recovery knows something no index can: that the replay it just finished was
        complete. Without it, an index that no record of the final commits happened to touch
        would be indistinguishable from one that missed them, and every open after a crash would
        rebuild indexes that were never behind.

        Call it BEFORE :meth:`open`. An index already marked stale refuses to move the position
        it claims -- that is what stops a broken index looking fresh again -- so declaring a
        replay complete afterwards does not clear the mark, and the index goes to a rebuild it
        did not need. That ordering costs work and never a wrong answer, and it is the right way
        round: only :meth:`clear_stale`, which names the rebuild that repaired it, takes an index
        out of the stale state.
        """
        position = _require_position("lsn", lsn)
        for index in self.indexes():
            index.advance_built_through(position)
        self._published_lsn = max(self._published_lsn, position)

    # --- staging ----------------------------------------------------------------------------

    def stage_row_insert(
        self,
        txn: StagingTransaction,
        table_id: int,
        ref: RecordRef,
        values: Sequence[object],
        csn: Csn,
    ) -> tuple[WalRecord, ...]:
        """Stage, on every index of the table, the entry this new row version owes it."""
        return tuple(
            index.stage_insert(txn, index.definition.key_for(values), ref, csn)
            for index in self.indexes_for(table_id)
        )

    def stage_row_delete(
        self,
        txn: StagingTransaction,
        table_id: int,
        ref: RecordRef,
        values: Sequence[object],
        csn: Csn,
    ) -> tuple[WalRecord, ...]:
        """Stage, on every index of the table, the end of the entry this row version had."""
        return tuple(
            index.stage_delete(txn, index.definition.key_for(values), ref, csn)
            for index in self.indexes_for(table_id)
        )

    def stage_row_update(
        self,
        txn: StagingTransaction,
        table_id: int,
        old_ref: RecordRef,
        old_values: Sequence[object],
        new_ref: RecordRef,
        new_values: Sequence[object],
        csn: Csn,
    ) -> tuple[WalRecord, ...]:
        """Stage both halves of an update on every index of the table.

        Both halves, always, even when the key did not change. An update writes a NEW version at
        a NEW location and ends the old one, so the entry that pointed at the old version has to
        be ended and an entry for the new one created; leaving the old entry alone because the
        key looks the same would leave the index pointing at a version the row no longer has.
        """
        records: list[WalRecord] = []
        for index in self.indexes_for(table_id):
            definition = index.definition
            records.append(
                index.stage_delete(txn, definition.key_for(old_values), old_ref, csn)
            )
            records.append(
                index.stage_insert(txn, definition.key_for(new_values), new_ref, csn)
            )
        return tuple(records)

    def commit(self, txn: StagingTransaction, csn: Csn) -> int:
        """Apply, on every index, what this transaction staged, and return how many changes moved.

        This is the one call the transaction manager owes the index framework, and it belongs
        AFTER the log barrier of CONTRACT.md section 8.5 step 3.5 and beside the page images of
        step 6: the commit number it is given is the number the log assigned, and nothing has
        touched an index page before it.
        """
        applied = 0
        touched: list[str] = []
        written = _tables_written_by(txn)
        for index in self.indexes():
            moved = index.commit(txn, csn)
            if moved:
                applied += moved
                touched.append(index.file)
            elif written is not None and index.definition.table_id not in written:
                # This commit staged nothing for this index BECAUSE it wrote no row of the table
                # the index covers. The index has therefore seen everything there was to see
                # through this position, and saying so is what keeps it from going stale for a
                # commit that had nothing to do with it.
                #
                # Without this, an index went stale the moment ANY commit happened after it was
                # created and before it received its first entry -- so "create the schema in one
                # session, load the data in the next" left every index of that database
                # permanently stale, and permanently is the right word: `_advance` refuses to move
                # a stale index, so the loads that followed could never lift it and only a rebuild
                # could. Reproduced on the vector index before any of this existed: create a table
                # with a vector column, close, reopen -> stale, and inserting rows did not clear
                # it.
                #
                # The condition is narrow ON PURPOSE. An index whose table WAS written and which
                # staged nothing is exactly the shape of defect E3 -- a commit that populated no
                # index at all -- and that case still goes stale, which is the alarm that found
                # E3 in the first place. Advancing unconditionally here would have silenced it.
                index.advance_built_through(csn)
        for file in touched:
            # A page applied into this process's pool is invisible to every other process until
            # it reaches the device, and the commit is about to publish a position that says the
            # entry is there. It is a flush and not a barrier, for the reason section 8.5 step 6
            # gives: the log is the authority on durability and the redo is idempotent.
            self._pool.flush(file)
        return applied

    def rollback(self, txn: StagingTransaction) -> int:
        """Drop what this transaction staged into every index, and return how many were dropped."""
        return sum(index.rollback(txn) for index in self.indexes())

    def apply(self, record: WalRecord) -> bool:
        """Redo one index record against the index it names, and say whether it was dispatched.

        A record for an index this database has not registered is NOT an error: recovery replays
        the whole log, and an index may have been dropped since the record was written. The
        answer says so rather than raising, and the caller decides.
        """
        change = change_of(record)
        found = self._indexes.get(change.index.lower())
        if found is None:
            return False
        found.apply(record)
        return True

    # --- reading ----------------------------------------------------------------------------

    def lookup(
        self, name: str, key: bytes, snapshot: SnapshotLike
    ) -> tuple[RecordRef, ...]:
        """Return the heap locations this index offers, under the contract it declares.

        For an EXACT index every candidate is validated against the heap here, so the obligation
        SD-3 puts on the caller is discharged once, in the framework, rather than repeated at
        every call site with a chance of being forgotten. For a PROXIMITY index the entries are
        already the answer and the heap is deliberately not consulted.
        """
        index = self.index(name)
        if index.visibility is IndexVisibility.PROXIMITY:
            # Every registered index satisfies the protocol -- register() checks it -- so the
            # index decides its own answer here and the heap is deliberately not consulted.
            reader: SecondaryIndex = index
            return tuple(reader.lookup(key, snapshot))
        return self.validated(index, key, snapshot)

    def validated(
        self, index: IndexStore, key: bytes, snapshot: SnapshotLike
    ) -> tuple[RecordRef, ...]:
        """Return the candidates of an exact index that the heap confirms under this snapshot.

        Three things are checked against the heap, and each of them is a way an exact index is
        allowed to be stale: the version may not be visible to this snapshot, the row may no
        longer carry the key the entry filed it under, and the location may belong to another
        table. A candidate that fails any of them is dropped silently, because being a superset
        is the contract rather than a defect. A candidate that cannot be READ is a different
        matter and is raised: the heap is the truth, and a truth that will not decode is damage.
        """
        if not isinstance(snapshot, SnapshotLike):
            raise GrafxIndexError(
                f"A lookup needs a snapshot; got {type(snapshot).__name__}.",
                field="snapshot",
                value=type(snapshot).__name__,
                index=index.name,
            )
        definition = index.definition
        confirmed: list[RecordRef] = []
        for entry in index.candidates(key):
            version = self._heap.read(entry.ref)
            if version.table_id != definition.table_id:
                raise GrafxCorruptionDetected(
                    f"Index {definition.name!r} points at a row of table {version.table_id} and "
                    f"covers table {definition.table_id}.",
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    index=definition.name,
                    field="table_id",
                )
            if not snapshot.visible(version.xmin, version.xmax):
                continue
            if definition.key_for(version.values) != entry.key:
                continue
            confirmed.append(entry.ref)
        return tuple(confirmed)

    # --- maintenance ---------------------------------------------------------------------------

    def reconcile(
        self, horizon: Lsn, txn: StagingTransaction | None = None
    ) -> tuple[ReconcileReport, ...]:
        """Run a reconciliation pass over every registered index at this horizon.

        With a transaction the pass removes and logs; without one it measures. The argument order
        follows the index method it delegates to, which follows CONTRACT.md section 8.7.
        """
        return tuple(index.reconcile(horizon, txn) for index in self.indexes())

    def note_reconciled(self, horizon: Lsn) -> None:
        """Record on every index the horizon a completed reconciliation pass applied."""
        for index in self.indexes():
            index.note_reconciled(horizon)

    def rebuild(self, name: str, txn: StagingTransaction, through_lsn: Lsn) -> int:
        """Re-derive every entry of an index from the heap, and return how many changes it staged.

        This is the repair of a stale index, and it is covered by the log like every other index
        change: the pass stages a reset followed by one insert per stored version and one
        tombstone per version that has ended, so a crash in the middle of it replays to the same
        place rather than leaving a half-built structure. The index stops being stale only when
        the transaction that carries these records commits.

        The cost is proportional to the table and the whole pass is one transaction, which is the
        honest bound for a reference implementation: a rebuild that spanned several commits would
        publish a partially-built index in between, and that is the state this method exists to
        get out of.
        """
        index = self.index(name)
        definition = index.definition
        table = self._heap.catalog.catalog.table_by_id(definition.table_id)
        position = _require_position("through_lsn", through_lsn)
        staged = 1
        index.stage_reset(txn, position)
        for ref, version in self._heap.scan_all(table):
            key = definition.key_for(version.values)
            index.stage_insert(txn, key, ref, version.xmin)
            staged += 1
            if version.xmax != NO_CSN:
                index.stage_delete(txn, key, ref, version.xmax)
                staged += 1
        return staged

    def clear_stale(self, name: str, through_lsn: Lsn) -> None:
        """Declare an index rebuilt through a position, so it may answer lookups again.

        Called by the caller that committed the transaction a rebuild staged, with the same
        position it passed to :meth:`rebuild`. It is separate from that method because a rebuild
        that was staged and never committed changed nothing, and an index that started answering
        at staging time would answer from a structure the log had not yet accepted.
        """
        self.index(name).clear_stale(through_lsn)

    # --- verification --------------------------------------------------------------------------

    def verify(self, name: str | None = None) -> tuple[IndexFinding, ...]:
        """Walk the indexes against the heap and report every divergence (SPEC-M1 FR-11, AC-12).

        Damage found while walking an INDEX becomes a finding rather than an exception: a
        verification that stopped at the first damaged page would tell an operator about one
        problem and hide the rest, and CONTRACT.md section 8.6 says a clean database returns an
        empty tuple. Damage found in the HEAP is deliberately left to propagate -- classifying
        heap damage is C6's scope, and an index reporting it as an index finding would name the
        wrong file.

        Both directions are checked, and only one of them is a wrong answer. An entry the heap
        does not confirm is a STALE entry, which the exact contract absorbs by validating and the
        proximity contract does not -- so the kind reported differs. A live row with no entry is
        an OMISSION, which is a wrong answer under either contract, and it is reported as such
        whichever kind of index left it out.
        """
        findings: list[IndexFinding] = []
        for index in self.indexes() if name is None else (self.index(name),):
            findings.extend(self._verify_entries(index))
            findings.extend(self._verify_coverage(index))
        return tuple(findings)

    def _verify_entries(self, index: IndexStore) -> tuple[IndexFinding, ...]:
        """Check every stored entry against the heap version it points at."""
        findings: list[IndexFinding] = []
        entries: tuple[IndexEntry, ...]
        try:
            entries = index.walk()
        except GrafxCorruptionDetected as damaged:
            return (
                IndexFinding(
                    kind="index_page_damaged",
                    index=index.name,
                    detail=damaged.message,
                    file=index.file,
                    page=_page_of(damaged),
                ),
            )
        for entry in entries:
            try:
                version = self._heap.read(entry.ref)
            except GrafxCorruptionDetected as damaged:
                findings.append(
                    IndexFinding(
                        kind="unreadable_reference",
                        index=index.name,
                        detail=(
                            f"Entry at page {entry.page} slot {entry.slot} points at page "
                            f"{entry.ref.page} slot {entry.ref.slot} of the heap, which does not "
                            f"read back: {damaged.message}"
                        ),
                        file=index.file,
                        page=entry.page,
                        slot=entry.slot,
                        ref=entry.ref,
                    )
                )
                continue
            findings.extend(self._compare(index, entry, version))
        return tuple(findings)

    def _compare(
        self, index: IndexStore, entry: IndexEntry, version: HeapVersion
    ) -> tuple[IndexFinding, ...]:
        """Compare one entry against the heap version it points at."""
        definition = index.definition
        findings: list[IndexFinding] = []
        if version.table_id != definition.table_id:
            return (
                IndexFinding(
                    kind="foreign_row",
                    index=index.name,
                    detail=(
                        f"Entry at page {entry.page} slot {entry.slot} points at a row of table "
                        f"{version.table_id}; this index covers table {definition.table_id}."
                    ),
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    ref=entry.ref,
                ),
            )
        if definition.key_for(version.values) != entry.key:
            findings.append(
                IndexFinding(
                    kind="stale_entry"
                    if definition.visibility is IndexVisibility.EXACT
                    else "index_heap_divergence",
                    index=index.name,
                    detail=(
                        f"Entry at page {entry.page} slot {entry.slot} is filed under a key the "
                        f"row it points at no longer carries."
                    ),
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    ref=entry.ref,
                )
            )
        if entry.versioned and entry.born_csn != version.xmin:
            findings.append(
                IndexFinding(
                    kind="index_heap_divergence",
                    index=index.name,
                    detail=(
                        f"Entry at page {entry.page} slot {entry.slot} was born at "
                        f"{entry.born_csn} and the version it points at at {version.xmin}, so a "
                        f"snapshot decides the two differently."
                    ),
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    lsn=entry.born_csn,
                    ref=entry.ref,
                )
            )
        if entry.dead_csn != version.xmax:
            missing = version.xmax != NO_CSN and entry.live
            findings.append(
                IndexFinding(
                    kind="missing_tombstone"
                    if missing and entry.versioned
                    else "stale_stamp",
                    index=index.name,
                    detail=(
                        f"Entry at page {entry.page} slot {entry.slot} ends at {entry.dead_csn} "
                        f"and the version it points at ends at {version.xmax}."
                    ),
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    lsn=version.xmax,
                    ref=entry.ref,
                )
            )
        return tuple(findings)

    def _verify_coverage(self, index: IndexStore) -> tuple[IndexFinding, ...]:
        """Check that every heap version the index owes an entry actually has one."""
        definition = index.definition
        try:
            table = self._heap.catalog.catalog.table_by_id(definition.table_id)
        except GrafxCorruptionDetected as unknown:
            return (
                IndexFinding(
                    kind="unknown_table",
                    index=definition.name,
                    detail=(
                        f"This index covers table {definition.table_id}, which the catalog does "
                        f"not know: {unknown.message}"
                    ),
                    file=index.file,
                ),
            )
        try:
            reconciled = index.reconciled_through_lsn
            stored = {(entry.key, entry.ref) for entry in index.walk()}
        except GrafxCorruptionDetected:
            # The damage was already reported by the entry walk, which runs first. Reporting it
            # twice would make one broken page look like two problems.
            return ()
        findings: list[IndexFinding] = []
        for ref, version in self._heap.scan_all(table):
            key = definition.key_for(version.values)
            if (key, ref) in stored:
                continue
            if version.xmax != NO_CSN and version.xmax <= reconciled:
                # The entry was released by a reconciliation pass this index has recorded, so its
                # absence is the pass working rather than a row going missing.
                continue
            findings.append(
                IndexFinding(
                    kind="missing_entry",
                    index=definition.name,
                    detail=(
                        f"The version at page {ref.page} slot {ref.slot} of the heap carries a "
                        f"key this index has no entry for, so a lookup would omit it."
                    ),
                    file=index.file,
                    page=ref.page,
                    slot=ref.slot,
                    lsn=version.xmin,
                    ref=ref,
                )
            )
        return tuple(findings)


def _with_flags(header: IndexHeader, flags: int) -> IndexHeader:
    """Return the header carrying different flag bits and nothing else changed.

    Rebuilding the value field by field is written once, here, because the two callers that turn
    the stale bit on and off would otherwise each enumerate every other field -- and a field one
    of them forgot would be silently reset by the very operation that says nothing else changed.
    """
    return IndexHeader(
        visibility=header.visibility,
        table_id=header.table_id,
        bucket_count=header.bucket_count,
        digest=header.digest,
        built_through_lsn=header.built_through_lsn,
        reconciled_through_lsn=header.reconciled_through_lsn,
        format_version=header.format_version,
        flags=flags,
    )


def _page_of(failure: GrafxCorruptionDetected) -> PageIndex:
    """Return the page a corruption failure named, or NO_PAGE when it named none."""
    page = failure.details.get("page")
    if isinstance(page, int) and not isinstance(page, bool) and page >= 0:
        return page
    return NO_PAGE


def _require_position(field_name: str, value: object) -> Lsn:
    """Return a log position when it is one, else refuse it."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxIndexError(
            f"A log position must be an integer; got {type(value).__name__}.",
            field=field_name,
            value=repr(value),
        )
    if value < NO_LSN:
        raise GrafxIndexError(
            f"A log position must not be negative; got {value}.",
            field=field_name,
            value=value,
        )
    return value
