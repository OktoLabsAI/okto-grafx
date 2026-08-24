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
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.page.file_header import HEADER_PAGE_INDEX, FileHeaderPage
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
from okto_grafx.engine.metrics_catalog import metric

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
            for file in self._paged_files():
                seen, found = self._verify_pages(file, reported)
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

    def _verify_pages(
        self, file: str, reported: set[tuple[str, PageIndex]]
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
            page = self._decode_page(file, index, findings, reported)
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
                findings.extend(self._verify_header_page(file, page))
        return checked, findings

    def _decode_page(
        self,
        file: str,
        index: PageIndex,
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
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

    def _verify_header_page(self, file: str, page: Page) -> list[VerificationFinding]:
        """Check that page 0 is the reserved header page amendment A2 requires it to be."""
        try:
            FileHeaderPage.read(page)
        except GrafxError as failure:
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
        if self._heap is None or self._catalog is None:
            return 0, findings
        try:
            catalog = self._catalog.read_from_pages()
        except GrafxError as failure:
            return 0, [
                VerificationFinding(
                    kind=FindingKind.CATALOG_UNREADABLE,
                    location=FindingLocation(file=_store_file(self._catalog)),
                    detail=f"The catalog could not be read from its pages: {failure}",
                )
            ]
        heap_file = _store_file(self._heap)
        owners = self._page_owners(heap_file, findings, reported)
        checked = 0
        for table in catalog.tables():
            counted, found = self._verify_table(table, heap_file, owners, reported)
            checked += counted
            findings.extend(found)
        return checked, findings

    def _page_owners(
        self,
        heap_file: str,
        findings: list[VerificationFinding],
        reported: set[tuple[str, PageIndex]],
    ) -> dict[int, set[PageIndex]]:
        """Return which pages each table claims, by scanning the file rather than by walking it.

        This is the independent half of the structural check. It follows no ``next_page`` link and
        consults no directory entry: it opens every page of the file and reads the table
        identifier the page descriptor of amendment A21 carries in slot 0. A chain that has been
        damaged cannot influence this answer, which is precisely why comparing the two catches the
        case where a replayed hint and a corrupt chain agree with each other.
        """
        owners: dict[int, set[PageIndex]] = {}
        try:
            total = self._pool.storage.page_count(heap_file)
        except GrafxError:
            return owners
        for index in range(total):
            if index == HEADER_PAGE_INDEX:
                continue
            page = self._decode_page(heap_file, index, findings, reported)
            if page is None or page.page_type != int(PageType.HEAP):
                continue
            if page.slot_count <= _HEAP_DESCRIPTOR_SLOT:
                continue
            try:
                descriptor = page.read_slot(_HEAP_DESCRIPTOR_SLOT)
            except GrafxError:
                continue
            if len(descriptor) < 4:
                continue
            table_id = int.from_bytes(descriptor[:4], "little")
            owners.setdefault(table_id, set()).add(index)
        return owners

    def _verify_table(
        self,
        table: TableDef,
        heap_file: str,
        owners: Mapping[int, set[PageIndex]],
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
        claimed = set(owners.get(table.table_id, ()))
        for orphan in sorted(claimed - set(chain)):
            findings.append(
                VerificationFinding(
                    kind=FindingKind.ORPHAN_PAGE,
                    location=FindingLocation(file=heap_file, page=orphan),
                    detail=(
                        f"This page carries the descriptor of table {table.name!r} and is not "
                        "reachable from the chain that table's directory entry starts, so every "
                        "record on it is invisible to a scan."
                    ),
                )
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
                    self._verify_record(table, heap_file, page, page_index, slot)
                )
        return checked, findings

    def _verify_record(
        self,
        table: TableDef,
        heap_file: str,
        page: Page,
        page_index: PageIndex,
        slot: int,
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
                self._heap.version_chain(RecordRef(page_index, slot))
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
        for index in self._indexes:
            counted, found = self._verify_index(index)
            checked += counted
            findings.extend(found)
        return checked, findings

    def _verify_index(self, index: object) -> tuple[int, list[VerificationFinding]]:
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
            try:
                self._heap.read(ref)
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
        findings.extend(self._verify_index_covers_the_heap(index, name, covered))
        return checked, findings

    def _verify_index_covers_the_heap(
        self, index: object, name: str, covered: set[tuple[bytes, int]]
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
        if not positions or not isinstance(table_id, int):
            return []
        try:
            catalog = self._catalog.read_from_pages()
            table = next(
                (found for found in catalog.tables() if found.table_id == table_id),
                None,
            )
            if table is None:
                return []
            versions = list(self._heap.scan_all(table))
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
        for ref, version in versions:
            if not version.live:
                continue
            try:
                key = _expected_key(definition, version.values, positions)
            except GrafxError:
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


def _expected_key(definition: object, values: object, positions: object) -> bytes:
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
