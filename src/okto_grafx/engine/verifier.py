"""On-demand verification (CONTRACT.md section 8.6, SPEC-M1 FR-11, AC-12).

``verify(scope)`` walks the database and reports what disagrees, with a location precise enough to
act on. It is written under one rule, which this build has already been bitten by twice: **a
consistency check that agrees with the corruption it was meant to catch is worse than no check**.
Everything below follows from that.

**Pages are read from the DEVICE, never through the pool.** A cached page is the image this
process last wrote, so verifying through the buffer pool would check memory against itself and
certify a file whose bytes on disk are damaged. The page walk therefore calls ``read_page`` and
decodes with the codec directly.

**The structural oracle is independent of the structure it checks.** C1's punch list records the
failure this avoids: when a directory hint had been replayed to agree with a corrupt chain, the
chain walk and the hint agreed with each other and the damaged table was certified clean. So the
table check compares the chain reachable from the directory against a FULL SCAN of the file --
every page that claims to belong to that table, found without following a single link. A page
that claims the table and is not reachable is an orphan and is reported; a chain that reaches a
page claiming another table is reported too. The two answers cannot be made to agree by damaging
one structure, because only one of them follows links.

**A refusal from a store is a finding, not an exception.** The walk is the product. Where a store
refuses -- a damaged chain, an unreadable record -- the refusal is recorded with its location and
the walk continues with the next subject, so one damaged table cannot hide the state of the rest.

**An allocated page nobody wrote is not damage, and this walk asks before it decodes.** C1's
``is_unwritten_image`` proves an all-zero image and a written page are DISJOINT states, and that
the all-zero one is what an ordinary crash between the ``PAGE_ALLOC`` record and the
``WRITE_PAGE`` that was to follow it leaves. Handing those bytes to the codec produces
``corruption_detected`` -- the verdict FR-8/FR-10 route to truncation, quarantine and a forensic
ledger entry -- for a page that is intact, which is precisely the false integrity incident
A11-revised exists to prevent. ``domain/verify/routing.py`` already holds this position for a
refusal that arrives from the heap, and ``recovery_manager`` already holds it for the redo path;
the page walk asks the same question in the same words. So the state is reported as
``page_unwritten``, and no checksum counter moves, because no checksum was verified.

**With exactly one exception, and it is page 0.** The reserved file header page of amendment A2
is written when the file is created, never from the log, so "a redo is already coming for it" is
false there and only there. An all-zero page 0 is reachable -- create a paged file, grow it,
crash before the header write -- and it keeps the ``file_header`` verdict, so narrowing the
verdict for data pages does not quietly narrow it for the one page nothing repairs.

**One page image is one finding, however many walks read it.** The page walk and the structural
scan behind ``records`` both decode every page of the heap, so under ``scope="all"`` the same
damaged image was reported twice -- once by each -- and a caller counting findings counted the
damage twice. A single ``verify()`` call therefore carries a ledger of the page images it has
already reported on, and the second walk to meet one stays silent. The ledger lives for one call
and is never an attribute, because a verifier is reused and a set that outlived the call would
silence a finding on the NEXT run, which is the opposite failure and a worse one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxConfigurationError,
    GrafxError,
    GrafxPortNotConfigured,
)
from okto_grafx.domain.ids import (
    NO_CSN,
    NO_PAGE,
    PageIndex,
    RecordRef,
    is_provisional_csn,
)
from okto_grafx.domain.index.definition import index_definition_matches_table
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.ordered_root import ORDERED_ROOT_PAGE_B
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, HeapVersion, RecordHeader
from okto_grafx.domain.model.catalog import HEAP_RECLAIM_V1_CAPABILITY
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.page.file_header import (
    HEADER_PAGE_INDEX,
    FileHeaderPage,
    FileKind,
)
from okto_grafx.domain.page.layout import PageType, is_unwritten_image
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.verify.findings import (
    NOT_APPLICABLE,
    SCOPE_ALL,
    SCOPE_INDEXES,
    SCOPE_PAGES,
    SCOPE_RECORDS,
    VERIFICATION_SCOPES,
    FindingKind,
    FindingLocation,
    VerificationFinding,
    VerificationReport,
    scope_covers,
)
from okto_grafx.domain.verify.routing import UNCLASSIFIED, route_page_refusal
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import (
    DESCRIPTOR_SIZE,
    EXTENT_FIRST_SLOT,
    HeapStore,
    TableExtent,
)
from okto_grafx.engine.index_manager import HashIndex, ProximityIndex
from okto_grafx.engine.metrics_catalog import metric
from okto_grafx.engine.vector_engine import VectorHnswIndex

__all__ = [
    "CHECKSUM_FAILURES_TOTAL",
    "CHECKSUM_VERIFICATIONS_TOTAL",
    "DEFAULT_VERIFIED_FILES",
    "PAGE_KIND_LABELS",
    "VERIFIER_METRICS",
    "Verifier",
]

CHECKSUM_VERIFICATIONS_TOTAL: str = "oktografx_checksum_verifications_total"
"""Counter of checksum verifications performed, by verified object (CONTRACT.md section 9)."""

CHECKSUM_FAILURES_TOTAL: str = "oktografx_checksum_failures_total"
"""Counter of checksum verifications that failed, by verified object (section 9)."""

VERIFIER_METRICS: tuple[MetricDescriptor, ...] = (
    metric(CHECKSUM_VERIFICATIONS_TOTAL),
    metric(CHECKSUM_FAILURES_TOTAL),
)
"""Every metric this verifier emits, taken from the frozen catalogue by name and never invented."""

PAGE_KIND_LABELS: Mapping[str, str] = {"kind": "page"}
"""The label a page checksum is counted under. A heap record carries no checksum of its own, so
nothing here is ever counted as ``kind=record`` -- that series belongs to the log, whose records
do carry one, and populating it from something else would make the number mean two things."""

DEFAULT_VERIFIED_FILES: tuple[str, ...] = ("heap.dat", "catalog.dat")
"""The paged files verified when the caller names none. Index files are added by their manager."""

_HEAP_DESCRIPTOR_SLOT: int = 0
_FIRST_RECORD_SLOT: int = 1

_CANONICAL_VERSION_CHAIN = HeapStore.version_chain
_CANONICAL_READ_SLOT = HeapStore._read_slot

_CANONICAL_INDEX_TYPES: tuple[type[object], ...] = (
    HashIndex,
    ProximityIndex,
    VectorHnswIndex,
)


class _CanonicalIndexVerification:
    """One-call immutable authorities and bounded memo for built-in index verification."""

    __slots__ = (
        "catalog_failure",
        "remaining_indexes",
        "resolved_refs",
        "scan_failures",
        "scanned_refs",
        "seeded",
        "tables",
        "versions",
    )

    def __init__(self) -> None:
        self.catalog_failure: GrafxError | None = None
        self.tables: dict[tuple[int, str], TableDef] = {}
        self.versions: dict[
            tuple[int, str], tuple[tuple[RecordRef, HeapVersion], ...]
        ] = {}
        self.scan_failures: dict[tuple[int, str], GrafxError] = {}
        self.scanned_refs: dict[tuple[int, str], set[int]] = {}
        self.resolved_refs: dict[tuple[int, str], set[int]] = {}
        self.seeded: set[tuple[int, str]] = set()
        self.remaining_indexes: dict[tuple[int, str], int] = {}

    def register(self, index: object) -> None:
        """Count one canonical consumer so table-sized state can be released promptly."""
        identity = _index_table_identity(index)
        if identity is not None:
            self.remaining_indexes[identity] = self.remaining_indexes.get(identity, 0) + 1

    def release(self, index: object) -> None:
        """Discard table-sized state immediately after its final index was verified."""
        identity = _index_table_identity(index)
        if identity is None:
            return
        remaining = self.remaining_indexes.get(identity, 0) - 1
        if remaining > 0:
            self.remaining_indexes[identity] = remaining
            return
        self.remaining_indexes.pop(identity, None)
        self.versions.pop(identity, None)
        self.scan_failures.pop(identity, None)
        self.scanned_refs.pop(identity, None)
        self.resolved_refs.pop(identity, None)
        self.seeded.discard(identity)


def _index_table_identity(index: object) -> tuple[int, str] | None:
    """Return the exact catalog identity published by one built-in index."""
    definition = getattr(index, "definition", None)
    table_id = getattr(definition, "table_id", None)
    table_name = getattr(definition, "table_name", None)
    if not isinstance(table_id, int) or not isinstance(table_name, str):
        return None
    return table_id, table_name


class Verifier:
    """Walks a database and reports every disagreement it can prove."""

    __slots__ = ("_pool", "_metrics", "_heap", "_catalog", "_indexes", "_files")

    def __init__(
        self,
        pool: BufferPool,
        metrics: MetricsSink,
        *,
        heap: object = None,
        catalog: object = None,
        indexes: Sequence[object] = (),
        files: Sequence[str] = DEFAULT_VERIFIED_FILES,
    ) -> None:
        """Build the verifier over the pool it reads through and the stores it interrogates.

        Every collaborator is optional, and an absent one narrows the walk rather than failing it:
        a database with no catalog store still has pages to verify, and reporting a smaller walk
        honestly is better than refusing to walk at all. The counts in the report say what was
        actually covered.
        """
        if not isinstance(pool, BufferPool):
            raise GrafxConfigurationError(
                f"A verifier reads through a BufferPool; got {type(pool).__name__}.",
                field="pool",
                value=type(pool).__name__,
            )
        for name in ("enabled", "register", "increment"):
            if not hasattr(metrics, name):
                raise GrafxPortNotConfigured(
                    f"The metrics port of the verifier is missing {name}.",
                    slot="metrics",
                    missing=(name,),
                )
        self._pool: BufferPool = pool
        self._metrics: MetricsSink = metrics
        self._heap = heap
        self._catalog = catalog
        self._indexes: tuple[object, ...] = tuple(indexes)
        self._files: tuple[str, ...] = tuple(files)
        if self._metrics.enabled:
            for declared in VERIFIER_METRICS:
                self._metrics.register(declared)

    def verify(self, scope: str = SCOPE_ALL) -> VerificationReport:
        """Walk the database and return everything that disagrees (CONTRACT.md section 8.6).

        A clean database returns an empty findings tuple with non-zero counts. Nothing is
        repaired, moved or removed: verification is a read, and the one thing it may never do is
        change what it is describing.
        """
        if scope not in VERIFICATION_SCOPES:
            raise GrafxConfigurationError(
                f"A verification scope is one of {VERIFICATION_SCOPES}; got {scope!r}.",
                field="scope",
                value=repr(scope),
            )
        findings: list[VerificationFinding] = []
        pages_checked = 0
        records_checked = 0
        entries_checked = 0
        files_checked: list[str] = []
        # The page images this call has already reported on. Local to the call, never an
        # attribute: a verifier is reused, and a set that survived the call would suppress a
        # finding on a later run of the same verifier.
        reported: set[tuple[str, PageIndex]] = set()
        if scope_covers(scope, SCOPE_PAGES):
            reachable_by_file = self._ordered_reachable_pages()
            for file in self._paged_files():
                seen, found = self._verify_pages(
                    file, reported, reachable=reachable_by_file.get(file)
                )
                pages_checked += seen
                findings.extend(found)
                if seen:
                    files_checked.append(file)
        if scope_covers(scope, SCOPE_RECORDS):
            counted, found = self._verify_records(reported)
            records_checked += counted
            findings.extend(found)
        if scope_covers(scope, SCOPE_INDEXES):
            counted, found = self._verify_indexes()
            entries_checked += counted
            findings.extend(found)
        return VerificationReport(
            scope=scope,
            findings=tuple(findings),
            pages_checked=pages_checked,
            records_checked=records_checked,
            index_entries_checked=entries_checked,
            files_checked=tuple(files_checked),
        )

    # --- pages -------------------------------------------------------------------------------

    def _paged_files(self) -> tuple[str, ...]:
        """Return every paged file this verifier walks, including the index files it was given."""
        names = list(self._files)
        for index in self._indexes:
            name = _index_file(index)
            if name and name not in names:
                names.append(name)
        return tuple(name for name in names if self._pool.storage.exists(name))

    def _ordered_reachable_pages(self) -> dict[str, frozenset[PageIndex]]:
        """Return, per ORDERED artifact file, the tree pages its selected root reaches.

        The layout comes from the store's own definition, the same authority the catalog
        resolved it through, and the reachable set from a fresh, complete proof: the store
        reads its certificate and traverses the whole tree.  A file no store names, a store
        without the door, and a store whose proof is absent or failed anywhere are absent
        from the mapping: every page of such a file keeps every verdict.
        """
        reachable: dict[str, frozenset[PageIndex]] = {}
        for index in self._indexes:
            definition = getattr(index, "definition", None)
            if getattr(definition, "layout", None) is not IndexLayout.ORDERED:
                continue
            name = _index_file(index)
            door = getattr(index, "reachable_pages", None)
            if not name or not callable(door):
                continue
            pages = door()
            if pages is not None:
                reachable[name] = frozenset(pages)
        return reachable

    def _verify_pages(
        self,
        file: str,
        reported: set[tuple[str, PageIndex]],
        *,
        reachable: frozenset[PageIndex] | None = None,
    ) -> tuple[int, list[VerificationFinding]]:
        """Verify every page of one file, straight off the device.

        Four things are checked per page, and each is a state the page itself can prove wrong:
        the checksum, whether the sequence counter says an interrupted write (A21: a durable
        image always carries an even one), whether the type is a value the layout defines, and --
        for page 0 only -- whether it is the reserved header page A2 requires it to be.
        """
        findings: list[VerificationFinding] = []
        checked = 0
        try:
            total = self._pool.storage.page_count(file)
        except GrafxError as failure:
            return 0, [
                VerificationFinding(
                    kind=FindingKind.FILE_UNREADABLE,
                    location=FindingLocation(file=file),
                    detail=f"The size of this file could not be read: {failure}",
                )
            ]
        for index in range(total):
            page = self._decode_page(file, index, findings, reported, reachable=reachable)
            checked += 1
            if page is None:
                continue
            if page.seq % 2:
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.PAGE_TORN,
                        location=FindingLocation(file=file, page=index),
                        detail=(
                            f"The sequence counter of this page is {page.seq}, which is odd, so "
                            "the write that produced it was interrupted."
                        ),
                    )
                )
            if page.page_type not in _KNOWN_PAGE_TYPES:
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.PAGE_TYPE,
                        location=FindingLocation(file=file, page=index),
                        detail=(
                            f"This page declares type {page.page_type}, which is not one the "
                            "page layout defines."
                        ),
                    )
                )
            if index == HEADER_PAGE_INDEX:
                findings.extend(self._verify_header_page(file, page, reported))
        return checked, findings

    def _decode_page(
        self,
        file: str,
        index: PageIndex,
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
        *,
        reachable: frozenset[PageIndex] | None = None,
    ) -> Page | None:
        """Read and decode one page, counting the checksum and reporting a failure as a finding.

        Three answers, and the order between the first two is the whole point. An ALLOCATED page
        that no write has reached is all zeros, and ``is_unwritten_image`` proves that state
        disjoint from every written page: the first four bytes of a written page hold the CRC-32C
        of the rest, and for those to be zero the rest would have to checksum to zero while also
        being zero, which no page size in the accepted range permits. Asking the codec first
        turns that intact page into ``corruption_detected`` -- the verdict FR-8/FR-10 route to
        truncation, quarantine and a forensic ledger entry -- so the question is asked BEFORE the
        decode, and the answer is ``page_unwritten``: the redo that was already coming for it is
        the repair. Neither checksum counter moves, because nothing verified a checksum.

        ``reported`` is the ledger of page images this ``verify()`` call has already spoken about.
        Both the page walk and the structural scan behind ``records`` decode every page of the
        heap, and one image that two walks read is still one image and one finding.
        """
        try:
            raw = self._pool.storage.read_page(file, index)
        except GrafxError as failure:
            self._report_page(
                findings,
                reported,
                file,
                index,
                FindingKind.FILE_UNREADABLE,
                f"This page could not be read from the device: {failure}",
            )
            return None
        if is_unwritten_image(raw, self._pool.page_size):
            if index == HEADER_PAGE_INDEX:
                # Page 0 is the reserved file header of amendment A2, and it is the ONE page
                # where "a redo is already coming for it" is false: a file header is written
                # when the file is created, not from the log, so nothing replays it. The state
                # is reachable through ordinary device doors -- create the file, grow it, and
                # crash before the header write -- and calling it awaiting-redo would promise a
                # repair that never arrives. It keeps the verdict the header check would give.
                self._report_page(
                    findings,
                    reported,
                    file,
                    index,
                    FindingKind.FILE_HEADER,
                    "Page 0 is the reserved file header page and holds nothing but zeros, so "
                    "this file was grown before its header was written. No replay repairs a "
                    "file header, because a header is written when the file is created.",
                )
                return None
            if (
                reachable is not None
                and index > ORDERED_ROOT_PAGE_B
                and index not in reachable
            ):
                # OIX-2B/O2.  An ORDERED artifact is append-only copy-on-write: a publication
                # allocates its new pages, writes them, barriers them, and only then writes a
                # root that references them.  A page allocated but never written and reached
                # by no valid root is the orphan of a publication interrupted between the
                # allocation and the write; no replay will ever fill it, because a COW page
                # carries no log image, and only a compacting rebuild reclaims it.  It is not
                # a loss and not a finding.  A page the selected root does reach keeps this
                # verdict whatever the scope -- zeroed under a live handle it is damage, and
                # the page walk names it before the tree walk refuses it.  The two root pages
                # and a file whose certificate cannot be read keep every verdict.
                return None
            self._report_page(
                findings,
                reported,
                file,
                index,
                FindingKind.PAGE_UNWRITTEN,
                "This page is allocated and has never been written, which is the state a crash "
                "between allocating a page and writing it leaves; replaying the log repairs it, "
                "so it is reported rather than treated as loss.",
            )
            return None
        if self._metrics.enabled:
            self._metrics.increment(CHECKSUM_VERIFICATIONS_TOTAL, 1.0, PAGE_KIND_LABELS)
        try:
            return self._pool.codec.decode_page(raw, verify=True)
        except GrafxError as failure:
            if self._metrics.enabled:
                self._metrics.increment(CHECKSUM_FAILURES_TOTAL, 1.0, PAGE_KIND_LABELS)
            self._report_page(
                findings,
                reported,
                file,
                index,
                FindingKind.PAGE_CHECKSUM,
                f"This page did not survive its own checksum: {failure}",
            )
            return None

    @staticmethod
    def _report_page(
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
        file: str,
        index: PageIndex,
        kind: str,
        detail: str,
    ) -> None:
        """Record one statement about one page image, at most once per ``verify()`` call."""
        if (file, index) in reported:
            return
        reported.add((file, index))
        findings.append(
            VerificationFinding(
                kind=kind,
                location=FindingLocation(file=file, page=index),
                detail=detail,
            )
        )

    def _verify_header_page(
        self,
        file: str,
        page: Page,
        reported: set[tuple[str, PageIndex]],
    ) -> list[VerificationFinding]:
        """Check that page 0 is the reserved header page amendment A2 requires it to be."""
        try:
            FileHeaderPage.read(page)
        except GrafxError as failure:
            if (file, HEADER_PAGE_INDEX) in reported:
                return []
            reported.add((file, HEADER_PAGE_INDEX))
            return [
                VerificationFinding(
                    kind=FindingKind.FILE_HEADER,
                    location=FindingLocation(file=file, page=HEADER_PAGE_INDEX),
                    detail=f"Page 0 is not a readable file header page: {failure}",
                )
            ]
        return []

    # --- records -----------------------------------------------------------------------------

    def _verify_records(
        self, reported: set[tuple[str, PageIndex]]
    ) -> tuple[int, list[VerificationFinding]]:
        """Verify the heap: its tables, their chains, their records and their version chains."""
        findings: list[VerificationFinding] = []
        if self._heap is None:
            return 0, findings
        heap_file = _store_file(self._heap)
        inventory = self._physical_heap_inventory(heap_file, findings, reported)
        if inventory is None:
            owners: dict[int, set[PageIndex]] | None = None
            physical_maxima: dict[int, int] | None = None
        else:
            owners, physical_maxima = inventory
        counter_findings, extent_slots = self._verify_record_id_counters(
            heap_file, physical_maxima, findings, reported
        )
        findings.extend(counter_findings)
        if extent_slots is not None and owners is not None:
            findings.extend(
                self._verify_unowned_heap_pages(
                    heap_file, owners, frozenset(extent_slots), reported
                )
            )
        if self._catalog is None:
            return 0, findings
        try:
            catalog = self._catalog.read_from_pages()
        except GrafxError as failure:
            findings.append(
                VerificationFinding(
                    kind=FindingKind.CATALOG_UNREADABLE,
                    location=FindingLocation(file=_store_file(self._catalog)),
                    detail=f"The catalog could not be read from its pages: {failure}",
                )
            )
            return 0, findings
        # Use the same freshly decoded catalog whose failure is reported above;
        # a second cached-catalog access must not escape the diagnostic boundary.
        from okto_grafx.engine.free_page_index import CAPABILITY, census
        from okto_grafx.engine.heap_store import HeapStore
        if isinstance(self._heap, HeapStore) and CAPABILITY in catalog.required_capabilities():
            try:
                horizon = getattr(self._pool.read_view_token(), "last_committed_lsn", None)
                if type(horizon) is not int:
                    raise GrafxCorruptionDetected("Free-page verification lacks a durable view.")
                census(self._heap, horizon)
            except GrafxError as failure:
                findings.append(VerificationFinding(
                    kind=FindingKind.TABLE_UNREADABLE,
                    location=FindingLocation(file=self._heap.file, page=0),
                    detail=f"Free-page index could not be verified: {failure}",
                ))
        tables = tuple(catalog.tables())
        if extent_slots is not None:
            findings.extend(
                self._verify_catalog_ownership(
                    heap_file,
                    owners,
                    extent_slots,
                    frozenset(table.table_id for table in tables),
                    reported,
                )
            )
        checked = 0
        for table in tables:
            counted, found = self._verify_table(table, heap_file, owners, reported)
            checked += counted
            findings.extend(found)
        return checked, findings

    def _physical_heap_inventory(
        self,
        heap_file: str,
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
    ) -> tuple[dict[int, set[PageIndex]], dict[int, int]] | None:
        """Return physical page owners and the greatest decodable record id of each table.

        This is the independent half of the structural check. It follows no ``next_page`` link and
        consults no directory entry: it opens every page of the file and reads the table
        identifier the page descriptor of amendment A21 carries in slot 0. A chain that has been
        damaged cannot influence this answer, which is precisely why comparing the two catches the
        case where a replayed hint and a corrupt chain agree with each other.

        The record-id high water follows the same independence rule. Every physically decodable
        header participates, including an ended, deleted, provisional, no-CSN or orphaned version.
        Visibility is a reader's opinion and a directory counter cannot use it: handing out an id
        already present in any persisted header would make the answer depend on whether that old
        header happened to be visible. Pages and slots are read from the device, never through the
        heap or its cache. A page without an exact readable descriptor is located and excluded:
        assigning its headers to any table would fabricate the association the damaged bytes lost.
        ``None`` means the file could not be enumerated at all; it is deliberately distinct from
        two complete empty maps, because an unavailable oracle cannot certify an empty heap.
        """
        owners: dict[int, set[PageIndex]] = {}
        maxima: dict[int, int] = {}
        try:
            total = self._pool.storage.page_count(heap_file)
        except GrafxError as failure:
            findings.append(
                VerificationFinding(
                    kind=FindingKind.FILE_UNREADABLE,
                    location=FindingLocation(file=heap_file),
                    detail=(
                        "The physical heap inventory could not read the file's page count: "
                        f"{failure}. Record ownership and identity high-water were not certified."
                    ),
                )
            )
            return None
        for index in range(total):
            if index == HEADER_PAGE_INDEX:
                continue
            page = self._decode_page(heap_file, index, findings, reported)
            if page is None or page.page_type != int(PageType.HEAP):
                continue
            if page.slot_count <= _HEAP_DESCRIPTOR_SLOT:
                self._report_page(
                    findings,
                    reported,
                    heap_file,
                    index,
                    FindingKind.PAGE_DESCRIPTOR_MISSING,
                    "This physical heap page has no table descriptor in slot 0, so its owner "
                    "cannot be established.",
                )
                continue
            try:
                descriptor = page.read_slot(_HEAP_DESCRIPTOR_SLOT)
            except GrafxError as failure:
                self._report_page(
                    findings,
                    reported,
                    heap_file,
                    index,
                    FindingKind.PAGE_DESCRIPTOR_MISSING,
                    "This physical heap page has no readable table descriptor in slot 0: "
                    f"{failure}",
                )
                continue
            if len(descriptor) != DESCRIPTOR_SIZE:
                self._report_page(
                    findings,
                    reported,
                    heap_file,
                    index,
                    FindingKind.PAGE_DESCRIPTOR_MISSING,
                    "This physical heap page carries a "
                    f"{len(descriptor)}-byte table descriptor in slot 0; exactly "
                    f"{DESCRIPTOR_SIZE} bytes are required before an owner can be trusted.",
                )
                continue
            table_id = int.from_bytes(descriptor[:4], "little")
            owners.setdefault(table_id, set()).add(index)
            for slot in range(_FIRST_RECORD_SLOT, page.slot_count):
                if page.is_slot_free(slot):
                    continue
                try:
                    header = RecordHeader.decode(page.read_slot(slot))
                except GrafxError:
                    # This pass is the independent counter oracle. The ordinary record walk owns
                    # the located RECORD_HEADER finding for a malformed reachable slot; an orphan
                    # page is already a located ORPHAN_PAGE. With no decodable id there is no
                    # number this pass may honestly compare against the counter.
                    continue
                previous = maxima.get(table_id)
                if previous is None or header.record_id > previous:
                    maxima[table_id] = header.record_id
        return owners, maxima

    def _verify_record_id_counters(
        self,
        heap_file: str,
        physical_maxima: Mapping[int, int] | None,
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
    ) -> tuple[list[VerificationFinding], dict[int, tuple[int, ...]] | None]:
        """Compare device-resident directory counters with every physical record header.

        ``next_record_id`` is the identity the next insert will take, so equality is already
        behind: the stored counter must be STRICTLY greater than the largest id in any decodable
        header. A counter ahead of the records is legal -- allocations and range leases burn gaps
        deliberately -- and an empty table imposes no lower bound beyond TableExtent's own format
        validation.

        Page zero is decoded directly from ``storage``. Calling ``HeapStore.extent_of`` here would
        let a resident old image certify lower bytes on the device, the precise failure a verifier
        is meant to expose. Duplicate extents are an authority failure of their own, so no counter
        is selected between them; the returned physical slot map lets the catalog comparison make
        the same choice without re-reading page zero through a cache.
        """
        page = self._decode_page(heap_file, HEADER_PAGE_INDEX, findings, reported)
        if page is None:
            return [], None
        try:
            header = FileHeaderPage.read(page)
        except GrafxError as failure:
            self._report_page(
                findings,
                reported,
                heap_file,
                HEADER_PAGE_INDEX,
                FindingKind.FILE_HEADER,
                f"Page 0 is not a readable heap file header page: {failure}",
            )
            return [], None
        if header.kind is not FileKind.HEAP:
            self._report_page(
                findings,
                reported,
                heap_file,
                HEADER_PAGE_INDEX,
                FindingKind.FILE_HEADER,
                f"Page 0 carries a {header.kind.name.lower()} file header, not a heap header.",
            )
            return [], None
        if header.page_size != self._pool.page_size:
            self._report_page(
                findings,
                reported,
                heap_file,
                HEADER_PAGE_INDEX,
                FindingKind.FILE_HEADER,
                f"The heap header declares {header.page_size}-byte pages and this verifier uses "
                f"{self._pool.page_size}-byte pages.",
            )
            return [], None
        legacy_floor = header.root_page == NO_PAGE and header.payload_length == 0
        guarded_floor = (
            header.root_page == HEADER_PAGE_INDEX and header.payload_length > 0
        )
        reclaim_capable = False
        if self._catalog is not None:
            try:
                reclaim_capable = (
                    HEAP_RECLAIM_V1_CAPABILITY
                    in self._catalog.read_from_pages().required_capabilities()
                )
            except GrafxError:
                # The catalog walk owns its own located finding.  It cannot authorize a heap
                # marker when it is unreadable, so this remains conservatively false.
                reclaim_capable = False
        if not legacy_floor and not (guarded_floor and reclaim_capable):
            self._report_page(
                findings,
                reported,
                heap_file,
                HEADER_PAGE_INDEX,
                FindingKind.FILE_HEADER,
                "The heap reclaim-floor marker is invalid or lacks catalog capability "
                "heap_reclaim_v1.",
            )
            return [], None

        found: list[VerificationFinding] = []
        extents: dict[int, list[tuple[int, TableExtent]]] = {}
        for slot, payload in page.iter_slots():
            if slot < EXTENT_FIRST_SLOT:
                continue
            try:
                extent = TableExtent.decode(payload)
            except GrafxError as failure:
                found.append(
                    VerificationFinding(
                        kind=FindingKind.TABLE_UNREADABLE,
                        location=FindingLocation(
                            file=heap_file, page=HEADER_PAGE_INDEX, slot=slot
                        ),
                        detail=f"This heap directory entry could not be decoded: {failure}",
                    )
                )
                continue
            extents.setdefault(extent.table_id, []).append((slot, extent))

        for table_id in sorted(extents):
            entries = extents[table_id]
            if len(entries) > 1:
                first_slot = entries[0][0]
                for slot, _extent in entries[1:]:
                    found.append(
                        VerificationFinding(
                            kind=FindingKind.TABLE_UNREADABLE,
                            location=FindingLocation(
                                file=heap_file, page=HEADER_PAGE_INDEX, slot=slot
                            ),
                            detail=(
                                f"Table {table_id} has duplicate heap directory entries in "
                                f"slots {first_slot} and {slot}; no counter or chain can be "
                                "chosen as its authority."
                            ),
                        )
                    )
                continue
            slot, extent = entries[0]
            if physical_maxima is None:
                continue
            highest = physical_maxima.get(table_id)
            if highest is None or extent.next_record_id > highest:
                continue
            found.append(
                VerificationFinding(
                    kind=FindingKind.RECORD_ID_COUNTER,
                    location=FindingLocation(
                        file=heap_file, page=HEADER_PAGE_INDEX, slot=slot
                    ),
                    detail=(
                        f"The directory entry of table {extent.table_id} declares next_record_id "
                        f"{extent.next_record_id}, but a physically stored record header carries "
                        f"id {highest}. The next id must be greater than every persisted id or a "
                        "reopen can hand the same identity to another row."
                    ),
                )
            )
        return found, {
            table_id: tuple(slot for slot, _extent in entries)
            for table_id, entries in extents.items()
        }

    def _verify_unowned_heap_pages(
        self,
        heap_file: str,
        owners: Mapping[int, set[PageIndex]],
        known_extent_ids: frozenset[int],
        reported: set[tuple[str, PageIndex]],
    ) -> list[VerificationFinding]:
        """Locate physical heap pages whose trustworthy descriptor has no directory owner.

        A descriptor is admitted to ``owners`` only after slot 0 decoded to exactly
        ``DESCRIPTOR_SIZE`` bytes.  Page zero is the ownership authority; if it cannot be read,
        the caller skips this comparison rather than guessing that every page is an orphan.
        """
        found: list[VerificationFinding] = []
        for table_id in sorted(set(owners) - known_extent_ids):
            for page_index in sorted(owners[table_id]):
                self._report_page(
                    found,
                    reported,
                    heap_file,
                    page_index,
                    FindingKind.ORPHAN_PAGE,
                    f"This physical heap page declares table {table_id}, but page 0 has no "
                    "decodable directory extent that owns it.",
                )
        return found

    def _verify_catalog_ownership(
        self,
        heap_file: str,
        owners: Mapping[int, set[PageIndex]] | None,
        extent_slots: Mapping[int, tuple[int, ...]],
        catalog_table_ids: frozenset[int],
        reported: set[tuple[str, PageIndex]],
    ) -> list[VerificationFinding]:
        """Report directory owners that have no table definition in the decoded catalog.

        The caller supplies the immutable ids returned by one ``read_from_pages`` result. An
        intentionally unwired catalog never reaches this method, and a catalog read failure stops
        before it, so neither absence of a collaborator nor unreadable authority is guessed to
        mean that every extent is orphaned.
        """
        found: list[VerificationFinding] = []
        for table_id in sorted(set(extent_slots) - catalog_table_ids):
            slots = extent_slots[table_id]
            if slots:
                found.append(
                    VerificationFinding(
                        kind=FindingKind.TABLE_UNREADABLE,
                        location=FindingLocation(
                            file=heap_file,
                            page=HEADER_PAGE_INDEX,
                            slot=slots[0],
                        ),
                        detail=(
                            f"The heap directory declares table {table_id}, but the catalog "
                            "snapshot has no table definition with that id."
                        ),
                    )
                )
            for page_index in sorted(
                owners.get(table_id, ()) if owners is not None else ()
            ):
                self._report_page(
                    found,
                    reported,
                    heap_file,
                    page_index,
                    FindingKind.ORPHAN_PAGE,
                    f"This physical heap page declares table {table_id}, but the catalog "
                    "snapshot has no table definition that can own it.",
                )
        return found

    def _verify_table(
        self,
        table: TableDef,
        heap_file: str,
        owners: Mapping[int, set[PageIndex]] | None,
        reported: set[tuple[str, PageIndex]],
    ) -> tuple[int, list[VerificationFinding]]:
        """Verify one table: its chain against the file, its records, and its version chains."""
        findings: list[VerificationFinding] = []
        try:
            chain = self._heap.pages_of(table)
        except GrafxError as failure:
            # C1 distinguishes four states behind one exception class, at this component's
            # request, and this is where that distinction is spent. Collapsing them back into
            # one finding would have made the ask pointless: an allocated page nobody wrote is
            # repaired by the redo already coming for it, while a page written with the wrong
            # type or with no descriptor is damage no replay repairs, and an operator does
            # different things about the two.
            kind, is_damage = route_page_refusal(failure)
            page = failure.details.get("page")
            if isinstance(page, int) and (heap_file, page) in reported:
                return 0, []
            return 0, [
                VerificationFinding(
                    # Compared by VALUE, never by identity: both sides are ordinary strings, and
                    # `is` on a string is an interning accident rather than a comparison.
                    kind=kind if kind != UNCLASSIFIED else FindingKind.TABLE_UNREADABLE,
                    location=FindingLocation(
                        file=heap_file,
                        page=page if isinstance(page, int) else NOT_APPLICABLE,
                    ),
                    detail=(
                        f"The page chain of table {table.name!r} could not be walked: {failure}"
                        + (
                            ""
                            if is_damage
                            else " This is the state a crash between allocating a page and "
                            "writing it leaves; replaying the log repairs it, so it is reported "
                            "rather than treated as loss."
                        )
                    ),
                )
            ]
        if owners is not None:
            claimed = set(owners.get(table.table_id, ()))
            for orphan in sorted(claimed - set(chain)):
                self._report_page(
                    findings,
                    reported,
                    heap_file,
                    orphan,
                    FindingKind.ORPHAN_PAGE,
                    f"This page carries the descriptor of table {table.name!r} and is not "
                    "reachable from the chain that table's directory entry starts, so every "
                    "record on it is invisible to a scan.",
                )
        findings.extend(self._verify_extent(table, heap_file, chain))
        checked, found = self._verify_versions(table, heap_file, chain, reported)
        findings.extend(found)
        return checked, findings

    def _verify_extent(
        self, table: TableDef, heap_file: str, chain: Sequence[PageIndex]
    ) -> list[VerificationFinding]:
        """Compare the directory hint against the chain the walk actually found (A33, A40).

        A disagreement here is DRIFT, not necessarily damage: the append path links a page in
        before it rewrites the entry, so an ordinary retryable refusal between the two leaves them
        disagreeing with nothing broken, and the heap repairs the hint on its next append. It is
        reported as drift and named as such, because an operator reading "damage" for a state the
        engine self-heals learns to distrust the report.
        """
        try:
            extent = self._heap.extent_of(table)
        except GrafxError as failure:
            return [
                VerificationFinding(
                    kind=FindingKind.TABLE_UNREADABLE,
                    location=FindingLocation(file=heap_file),
                    detail=f"The directory entry of table {table.name!r} is unreadable: {failure}",
                )
            ]
        if extent is None or not chain:
            return []
        findings: list[VerificationFinding] = []
        if extent.last_page != chain[-1]:
            findings.append(
                VerificationFinding(
                    kind=FindingKind.EXTENT_DRIFT,
                    location=FindingLocation(file=heap_file, page=extent.last_page),
                    detail=(
                        f"The directory entry of table {table.name!r} names page "
                        f"{extent.last_page} as the tail and the chain ends at page "
                        f"{chain[-1]}. This is drift the next append repairs, not damage."
                    ),
                )
            )
        if extent.page_count != len(chain):
            findings.append(
                VerificationFinding(
                    kind=FindingKind.EXTENT_DRIFT,
                    location=FindingLocation(file=heap_file),
                    detail=(
                        f"The directory entry of table {table.name!r} counts "
                        f"{extent.page_count} pages and the chain walks {len(chain)}. This is "
                        "drift the next append repairs, not damage."
                    ),
                )
            )
        return findings

    def _verify_versions(
        self,
        table: TableDef,
        heap_file: str,
        chain: Sequence[PageIndex],
        reported: set[tuple[str, PageIndex]],
    ) -> tuple[int, list[VerificationFinding]]:
        """Verify every stored version of one table, visible or not (A19's unfiltered truth).

        The slots are read and decoded HERE rather than through ``HeapStore.scan_all``, and that
        is the point rather than a convenience. The store's own decoder refuses a record whose
        declared length disagrees with its slot -- correctly -- so a verifier built on top of it
        can only ever report "this table would not read", never which record is wrong and how.
        Worse, it would be checking the store against itself: two answers from one mechanism
        (A34, A67). Reading the page off the device and decoding the 40-byte header of section
        6.4 directly gives an answer the heap cannot influence.

        Three properties are checked per version, and none of them is a checksum: a heap record
        has no checksum of its own -- the page it lives on carries one, and the page walk already
        took it. What can still be wrong is the RECORD: a header that will not decode, a declared
        payload length that disagrees with the slot it sits in, or a lifetime that ends before it
        starts. All three survive a correct page checksum, which is why they are checked here.
        """
        findings: list[VerificationFinding] = []
        checked = 0
        # A long history used to rewalk every older suffix for every stored version:
        # quadratic work despite checking the same stable physical graph. Keep only
        # successful suffix lengths, local to this table/call, never across admission.
        # Custom heap collaborators retain their observable per-record protocol.
        verified_chains: dict[int, int] | None = (
            {}
            if type(self._heap) is HeapStore
            and HeapStore.version_chain is _CANONICAL_VERSION_CHAIN
            and HeapStore._read_slot is _CANONICAL_READ_SLOT
            else None
        )
        for page_index in chain:
            page = self._decode_page(heap_file, page_index, findings, reported)
            if page is None:
                continue
            for slot in range(_FIRST_RECORD_SLOT, page.slot_count):
                if page.is_slot_free(slot):
                    # A freed slot holds no version, so there is nothing to check and nothing to
                    # count. Reading it would raise, and reporting that as a damaged record would
                    # make a clean database produce findings -- the mirror of certifying damage,
                    # and just as corrosive to a report an operator has to believe.
                    continue
                checked += 1
                findings.extend(
                    self._verify_record(
                        table,
                        heap_file,
                        page,
                        page_index,
                        slot,
                        verified_chains=verified_chains,
                    )
                )
        return checked, findings

    def _verify_record(
        self,
        table: TableDef,
        heap_file: str,
        page: Page,
        page_index: PageIndex,
        slot: int,
        *,
        verified_chains: dict[int, int] | None = None,
    ) -> list[VerificationFinding]:
        """Check one stored version against the slot that holds it.

        The page index is passed in rather than read off the page. ``PageCodec.decode_page``
        takes no location -- section 4.5 gives it only the bytes -- so a decoded image reports
        index zero whatever page it came from, and a finding built from it would name the wrong
        page every time.
        """
        location = FindingLocation(file=heap_file, page=page_index, slot=slot)
        try:
            content = page.read_slot(slot)
        except GrafxError as failure:
            return [
                VerificationFinding(
                    kind=FindingKind.RECORD_HEADER,
                    location=location,
                    detail=f"This slot could not be read: {failure}",
                )
            ]
        try:
            header = RecordHeader.decode(content)
        except GrafxError as failure:
            return [
                VerificationFinding(
                    kind=FindingKind.RECORD_HEADER,
                    location=location,
                    detail=f"This record header did not decode: {failure}",
                )
            ]
        findings: list[VerificationFinding] = []
        if (
            not is_provisional_csn(header.xmin)
            and header.xmax
            and header.xmin
            and header.xmax < header.xmin
        ):
            findings.append(
                VerificationFinding(
                    kind=FindingKind.RECORD_LIFETIME,
                    location=location,
                    detail=(
                        f"Record {header.record_id} of table {table.name!r} was born at commit "
                        f"number {header.xmin} and ended at {header.xmax}, so it ends before it "
                        "begins and no snapshot can ever see it."
                    ),
                )
            )
        if not header.has_overflow:
            stored = len(content) - RECORD_HEADER_SIZE
            if header.payload_len != stored:
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.RECORD_LENGTH,
                        location=location,
                        detail=(
                            f"Record {header.record_id} of table {table.name!r} declares a "
                            f"payload of {header.payload_len} bytes and its slot holds {stored}."
                        ),
                    )
                )
        if header.previous is not None and self._heap is not None:
            try:
                ref = RecordRef(page_index, slot)
                if verified_chains is None:
                    self._heap.version_chain(ref)
                else:
                    self._heap._walk_version_chain(ref, verified_chains)
            except GrafxError as failure:
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.VERSION_CHAIN,
                        location=location,
                        detail=f"The version chain that ends here could not be walked: {failure}",
                    )
                )
        return findings

    # --- indexes -----------------------------------------------------------------------------

    def _verify_indexes(self) -> tuple[int, list[VerificationFinding]]:
        """Verify every secondary index against the heap it points at (AC-12)."""
        findings: list[VerificationFinding] = []
        checked = 0
        shared = self._canonical_index_verification()
        for index in self._indexes:
            try:
                counted, found = self._verify_index(index, shared=shared)
            finally:
                if shared is not None:
                    shared.release(index)
            checked += counted
            findings.extend(found)
        return checked, findings

    def _canonical_index_verification(
        self,
    ) -> _CanonicalIndexVerification | None:
        """Capture one-call catalog authority only for the exact built-in collaboration.

        A custom catalog, heap or index may attach observable behaviour to each call.  It keeps
        the former per-index protocol.  The built-in database runs verification inside one fresh
        page-access section, so one physical catalog image and one table scan can serve every
        built-in index without broadening their authority or surviving the public call.
        """
        if (
            type(self._catalog) is not CatalogStore
            or type(self._heap) is not HeapStore
            or any(type(index) not in _CANONICAL_INDEX_TYPES for index in self._indexes)
        ):
            return None
        shared = _CanonicalIndexVerification()
        try:
            catalog = self._catalog.read_from_pages()
            for table in catalog.tables():
                shared.tables.setdefault((table.table_id, table.name), table)
        except GrafxError as failure:
            shared.catalog_failure = failure
        for index in self._indexes:
            shared.register(index)
        return shared

    def _verify_index(
        self,
        index: object,
        *,
        shared: _CanonicalIndexVerification | None = None,
    ) -> tuple[int, list[VerificationFinding]]:
        """Verify one index: every entry must resolve to a heap version that exists.

        An EXACT index returns a superset by contract (SD-3), so an entry pointing at a version
        no snapshot can see is legal and is NOT reported. What is never legal is an entry pointing
        at a location the heap cannot resolve at all: that is index-heap divergence, and it is the
        class AC-12 names.
        """
        name = _index_name(index)
        findings: list[VerificationFinding] = []
        checked = 0
        walk = getattr(index, "walk", None)
        if walk is None:
            return 0, [
                VerificationFinding(
                    kind=FindingKind.INDEX_UNREADABLE,
                    location=FindingLocation(index=name),
                    detail="This index cannot be walked, so its entries were not verified.",
                )
            ]
        try:
            entries = list(walk())
        except GrafxError as failure:
            return 0, [
                VerificationFinding(
                    kind=FindingKind.INDEX_UNREADABLE,
                    location=FindingLocation(index=name),
                    detail=f"The entries of this index could not be walked: {failure}",
                )
            ]
        covered: set[tuple[bytes, int]] = set()
        table_identity = _index_table_identity(index)
        resolved_refs = (
            shared.resolved_refs.setdefault(table_identity, set())
            if shared is not None and table_identity is not None
            else None
        )
        if resolved_refs is not None and self._heap is not None:
            self._seed_resolved_refs(
                index,
                table_identity,  # type: ignore[arg-type]
                shared,  # type: ignore[arg-type]
                resolved_refs,
            )
        for entry in entries:
            checked += 1
            location = FindingLocation(
                index=name,
                page=getattr(entry, "page", NOT_APPLICABLE),
                slot=getattr(entry, "slot", NOT_APPLICABLE),
            )
            ref = getattr(entry, "ref", None)
            if ref is None or ref.page == NO_PAGE:
                # A distinct kind, not a distinct message: an entry carrying no reference is a
                # MALFORMED entry, while an entry whose reference does not resolve is index-heap
                # divergence, and an operator acts on the two differently. Sharing one kind also
                # made the guard untestable -- a null reference simply failed to resolve a moment
                # later and produced the same finding, so deleting this branch changed nothing a
                # test could see (A62).
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.INDEX_ENTRY_MALFORMED,
                        location=location,
                        detail="This index entry points at no heap location at all.",
                    )
                )
                continue
            if not getattr(entry, "dead_csn", NO_CSN):
                covered.add((bytes(getattr(entry, "key", b"")), ref.encode()))
            if self._heap is None:
                continue
            ref_identity = ref.encode() if type(ref) is RecordRef else None
            try:
                if resolved_refs is None or ref_identity not in resolved_refs:
                    self._heap.read(ref)
                    if ref_identity is not None and resolved_refs is not None:
                        resolved_refs.add(ref_identity)
            except GrafxError as failure:
                findings.append(
                    VerificationFinding(
                        kind=FindingKind.INDEX_ENTRY_UNRESOLVED,
                        location=location,
                        detail=(
                            f"This index entry points at page {ref.page} slot {ref.slot} of the "
                            f"heap, which does not resolve: {failure}"
                        ),
                    )
                )
        findings.extend(
            self._verify_index_covers_the_heap(
                index,
                name,
                covered,
                shared=shared,
            )
        )
        return checked, findings

    def _canonical_versions(
        self,
        identity: tuple[int, str],
        table: TableDef,
        shared: _CanonicalIndexVerification,
    ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
        """Fully decode one table, retaining only values that coverage can need.

        The scan runs at most once per table per verification; a failure is kept and raised
        again to every later asker, so each index reports it where it always did.
        Ended built-in versions still pass the complete decoder, including overflow and tuple
        validation. Their payloads are then discarded: coverage never requires their keys.
        Exact physical references are retained separately for resolution seeding, published
        only after the entire scan succeeds. Foreign version objects keep their former path.
        """
        failure = shared.scan_failures.get(identity)
        if failure is not None:
            raise failure
        versions = shared.versions.get(identity)
        if versions is None:
            try:
                retained: list[tuple[RecordRef, HeapVersion]] = []
                scanned_refs: set[int] = set()
                for ref, version in self._heap.scan_all(table):  # type: ignore[union-attr]
                    if type(ref) is RecordRef:
                        scanned_refs.add(ref.encode())
                    if type(version) is not HeapVersion or version.live:
                        retained.append((ref, version))
                versions = tuple(retained)
            except GrafxError as caught:
                shared.scan_failures[identity] = caught
                raise
            shared.versions[identity] = versions
            shared.scanned_refs[identity] = scanned_refs
        return versions

    def _seed_resolved_refs(
        self,
        index: object,
        identity: tuple[int, str],
        shared: _CanonicalIndexVerification,
        resolved_refs: set[int],
    ) -> None:
        """Seed the references the canonical scan of the table already proved resolvable.

        CKPTCERT-1.  ``HeapStore.read`` and ``HeapStore.scan_all`` open the same doors to a
        slot: the walk accepts a page only as a data page owned by this table, skips the
        descriptor slot, and decodes the same bytes with the same decoder.  The one thing
        ``read`` does on its own is resolve the table through the catalog the heap holds, so
        the scan is trusted for a reference only while that catalog names a definition equal to
        the one the scan decoded with; otherwise every entry keeps its own read and its own
        finding.  The scan runs only where the coverage check would run it, and a scan that
        fails seeds nothing and is reported where it always was, by that check, while the
        entries are still resolved one by one -- a broken heap never hides a broken index and a
        broken index never hides a broken heap.  Nothing here outlives the verification of the
        table's last index.
        """
        if identity in shared.seeded:
            return
        if shared.catalog_failure is not None:
            return
        definition = getattr(index, "definition", None)
        if not getattr(definition, "positions", None):
            return
        table = shared.tables.get(identity)
        if table is None or not index_definition_matches_table(definition, table):
            return
        try:
            held = self._heap.catalog.catalog.table_by_id(table.table_id)  # type: ignore[union-attr]
        except GrafxError:
            return
        if held != table:
            return
        try:
            self._canonical_versions(identity, table, shared)
        except GrafxError:
            return
        # Publish the seeded marker only after this particular index proved that it exposes a
        # valid built-in definition and the heap/catalog pair agreed.  An earlier malformed or
        # non-covering index for the same table must not suppress the optimization for a later
        # valid one; all failure paths above remain canonical fallbacks.
        shared.seeded.add(identity)
        resolved_refs.update(shared.scanned_refs[identity])

    def _verify_index_covers_the_heap(
        self,
        index: object,
        name: str,
        covered: set[tuple[bytes, int]],
        *,
        shared: _CanonicalIndexVerification | None = None,
    ) -> list[VerificationFinding]:
        """Report a live heap row this index has no entry for (AC-12, BR-11).

        This is the other direction of index-heap divergence, and it is the dangerous one: an
        entry pointing at nothing makes a lookup return a row that has to be validated away, while
        a MISSING entry makes a row that exists disappear from every lookup. BR-11 says the index
        is never less safe than the heap, so a live row with no live entry is a finding.

        The check runs only when the index publishes the definition C7 gives it -- the table it
        covers and the key positions inside a row -- because the key is derived from the row by
        ``index_key``, and guessing it would be inventing an oracle rather than using one. An
        index that publishes no definition is left with the entry-side check alone, and the
        report says nothing it cannot prove.

        A DEAD row is deliberately not required to have an entry: an exact index returns a
        superset (SD-3) and a proximity index reconciles tombstones at the horizon, so an entry
        for a dead row is legal in both contracts and its absence is legal too.
        """
        definition = getattr(index, "definition", None)
        if definition is None or self._heap is None or self._catalog is None:
            return []
        positions = getattr(definition, "positions", None)
        table_id = getattr(definition, "table_id", None)
        table_name = getattr(definition, "table_name", None)
        if (
            not positions
            or not isinstance(table_id, int)
            or not isinstance(table_name, str)
        ):
            return []
        identity = (table_id, table_name)
        try:
            if shared is None:
                catalog = self._catalog.read_from_pages()
                table = next(
                    (
                        found
                        for found in catalog.tables()
                        if found.table_id == table_id and found.name == table_name
                    ),
                    None,
                )
            else:
                if shared.catalog_failure is not None:
                    raise shared.catalog_failure
                table = shared.tables.get(identity)
            if table is None:
                return []
            if not index_definition_matches_table(definition, table):
                return []
            if shared is None:
                versions: Sequence[tuple[RecordRef, HeapVersion]] = tuple(
                    self._heap.scan_all(table)
                )
            else:
                versions = self._canonical_versions(identity, table, shared)
        except GrafxError as failure:
            return [
                VerificationFinding(
                    kind=FindingKind.INDEX_UNREADABLE,
                    location=FindingLocation(index=name),
                    detail=(
                        f"The rows this index covers could not be read, so its coverage was not "
                        f"verified: {failure}"
                    ),
                )
            ]
        findings: list[VerificationFinding] = []
        from okto_grafx.domain.index.fulltext import has_durable_statistics, decode_options, field_tokens
        if has_durable_statistics(getattr(definition, "key_derivation", "")):
            from okto_grafx.engine.fulltext_durable import read_history_from_device
            from okto_grafx.domain.txn.snapshot import Snapshot
            try:
                series = read_history_from_device(index)
                _, actual_count, actual_totals = series[-1]
                options = decode_options(definition.key_derivation)
                count = 0
                totals = [0] * len(positions)
                for _, version in versions:
                    if version.live:
                        count += 1
                        fields = field_tokens(version.values, positions, options)
                        totals = [a + len(b) for a, b in zip(totals, fields, strict=True)]
                if actual_count != count or actual_totals != tuple(totals):
                    raise GrafxCorruptionDetected("Durable text statistics differ from the heap census.")
                floor = self._heap.reclaim_floor() if len(series) > 1 else 0
                retained = [record for record in series[:-1] if record[0] >= floor]
                snapshots = [Snapshot(record[0]) for record in retained]
                counts = [0] * len(retained)
                lengths = [[0] * len(positions) for _ in retained]
                # The shared coverage cache intentionally discards ended payloads.
                # Stream canonical history once; never mistake that live-only cache
                # for a complete historical oracle or retain every old document.
                if retained:
                    markers = {record[0] for record in series}
                    earliest = retained[0][0]
                    latest = series[-1][0]
                    for _, version in self._heap.scan_all(table):
                        if any(earliest < stamp <= latest and stamp not in markers
                               for stamp in (version.xmin, version.xmax)):
                            raise GrafxCorruptionDetected("Historical text statistics omit a retained heap transition.")
                        visible = [i for i, snapshot in enumerate(snapshots)
                                   if snapshot.visible(version.xmin, version.xmax)]
                        if visible:
                            sizes = [len(tokens) for tokens in field_tokens(version.values, positions, options)]
                            for i in visible:
                                counts[i] += 1
                                lengths[i] = [a + b for a, b in zip(lengths[i], sizes, strict=True)]
                for (_, historical_count, historical_totals), count, totals in zip(retained, counts, lengths, strict=True):
                    if historical_count != count or historical_totals != tuple(totals):
                        raise GrafxCorruptionDetected("Historical text statistics differ from retained heap visibility.")
            except GrafxError as failure:
                findings.append(VerificationFinding(
                    kind=FindingKind.INDEX_UNREADABLE,
                    location=FindingLocation(index=name, file=_index_file(index), page=0),
                    detail=f"Durable text statistics could not be verified: {failure}",
                ))
        for ref, version in versions:
            if not version.live:
                continue
            try:
                key = _expected_key(definition, version.values, positions)
            except GrafxError:
                continue
            if key is None:
                continue
            if (key, ref.encode()) in covered:
                continue
            findings.append(
                VerificationFinding(
                    kind=FindingKind.INDEX_ENTRY_MISSING,
                    location=FindingLocation(
                        index=name,
                        file=_store_file(self._heap),
                        page=ref.page,
                        slot=ref.slot,
                    ),
                    detail=(
                        f"Record {version.record_id} of table {table.name!r} is live at this "
                        "location and this index holds no live entry for it, so a lookup by its "
                        "key cannot find it."
                    ),
                )
            )
        return findings

    def __repr__(self) -> str:
        """Return a short description naming what this verifier was given to walk."""
        return f"Verifier(files={self._files!r}, indexes={len(self._indexes)})"


_KNOWN_PAGE_TYPES: frozenset[int] = frozenset(int(member) for member in PageType)


def _expected_key(
    definition: object,
    values: object,
    positions: object,
) -> bytes | None:
    """Return the key this index would store for a row, asking the DEFINITION how.

    The derivation belongs to the index, not to this walk. A vector index keys on a digest of the
    embedding (``vector_digest_v1``), not on the column bytes, so computing ``index_key`` here
    produced a key no entry could ever match and reported every live row of the table as missing
    an entry it in fact had -- the false ``corruption`` verdict A11-revised exists to prevent, on
    a database that is perfectly correct.

    This walk's own docstring says guessing the key would be "inventing an oracle rather than
    using one". Calling ``index_key`` directly WAS the guess: it is one derivation of several,
    and it happened to be right for the only kind of index that had entries while nothing
    populated the others. ``key_for`` is the oracle, and it is the same call the staging path
    makes, so the two cannot drift.
    """
    entry_key_for = getattr(definition, "entry_key_for", None)
    if callable(entry_key_for):
        key = entry_key_for(values)
        return None if key is None else bytes(key)
    key_for = getattr(definition, "key_for", None)
    if callable(key_for):
        return bytes(key_for(values))
    return index_key(
        values, positions
    )  # pragma: no cover - every definition carries key_for


def _store_file(store: object) -> str:
    """Return the file a store works over, or an empty name when it does not say."""
    file = getattr(store, "file", "")
    return file if isinstance(file, str) else ""


def _index_name(index: object) -> str:
    """Return the name of an index, or a placeholder when it does not carry one."""
    name = getattr(index, "name", "")
    return name if isinstance(name, str) and name else "unnamed"


def _index_file(index: object) -> str:
    """Return the paged file an index lives in, when it says which one."""
    file = getattr(index, "file", "")
    return file if isinstance(file, str) else ""
