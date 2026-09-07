"""Persistent exact ordered-index storage and heap-validated reverse reads.

This module owns the engine half of the ordered format.  The tree pages are immutable, page 0
identifies one nonced artifact, and pages 1/2 alternate complete root publications.  A root is
only an exact-index candidate authority: every result is read again from the heap under the
caller's snapshot before it can leave this module.

Transactional copy-on-write maintenance is intentionally a later layer.  Keeping the bulk
artifact and its read protocol here means the query planner cannot accidentally select the
ordered format merely because its domain codecs exist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TypeVar

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_LSN, NO_PAGE, PROVISIONAL_CSN, Lsn, PageIndex, RecordRef
from okto_grafx.domain.index import (
    INDEX_HEADER_SLOT,
    ORDERED_INDEX_HEADER_FORMAT_VERSION,
    ORDERED_ROOT_PAGE_A,
    ORDERED_ROOT_PAGE_B,
    IndexDefinition,
    IndexChange,
    IndexEntry,
    IndexHeader,
    IndexLayout,
    IndexOperation,
    IndexVisibility,
    OrderedRootDescriptor,
    OrderedRootSelection,
    OrderedTreeVerification,
    SnapshotLike,
    build_ordered_tree,
    make_ordered_root_page,
    mutate_ordered_tree,
    select_ordered_root,
    verify_ordered_tree,
    walk_ordered_desc,
)
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
)
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore

__all__ = [
    "ORDERED_READ_RETRY_BUDGET",
    "OrderedIndex",
    "OrderedBatchPublication",
    "OrderedIndexReadCertificate",
]


ORDERED_READ_RETRY_BUDGET: int = 2
"""How many complete statement-local walks may be retried after root drift."""

_ReadResult = TypeVar("_ReadResult")


def _required_read_lsn(snapshot: object) -> Lsn:
    """Return the snapshot position an ordered root must cover."""

    if not isinstance(snapshot, SnapshotLike):
        raise GrafxIndexError(
            "An ordered-index read needs a snapshot visibility predicate.",
            field="snapshot",
            value=type(snapshot).__name__,
        )
    read_lsn = getattr(snapshot, "read_lsn", None)
    if (
        isinstance(read_lsn, bool)
        or not isinstance(read_lsn, int)
        or not NO_LSN <= read_lsn < PROVISIONAL_CSN
    ):
        raise GrafxIndexError(
            "An ordered-index read needs a non-provisional integer snapshot.read_lsn.",
            field="snapshot.read_lsn",
            value=repr(read_lsn),
        )
    return read_lsn


def _required_limit(limit: object) -> int:
    """Return the bounded result count, refusing bool and negative values."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise GrafxIndexError(
            "An ordered-index read limit must be a non-negative integer.",
            field="limit",
            value=repr(limit),
        )
    return limit


@dataclass(frozen=True, slots=True)
class OrderedIndexReadCertificate:
    """One static-header and dual-root observation surrounding a tree walk."""

    header_seq: int
    header: IndexHeader
    root_a_seq: int | None
    root_b_seq: int | None
    selection: OrderedRootSelection


@dataclass(frozen=True, slots=True)
class OrderedBatchPublication:
    """The durable ordered-root transition produced by one committed logical batch."""

    descriptor: OrderedRootDescriptor
    changes: int
    pages_written: int
    missing_targets: int
    replay_skipped: bool = False


class _OrderedPageLoader(Mapping[PageIndex, Page]):
    """Resolve only the immutable pages a tree walk actually visits."""

    __slots__ = ("_file", "_fresh", "_page_count", "_pool", "_seen")

    def __init__(self, pool: BufferPool, file: str, *, fresh: bool) -> None:
        self._pool = pool
        self._file = file
        self._fresh = fresh
        self._page_count = pool.storage.page_count(file)
        self._seen: dict[PageIndex, Page] = {}

    def __getitem__(self, page_index: PageIndex) -> Page:
        cached = self._seen.get(page_index)
        if cached is not None:
            return cached
        if (
            isinstance(page_index, bool)
            or not isinstance(page_index, int)
            or page_index < 3
            or page_index >= self._page_count
        ):
            raise KeyError(page_index)
        if self._fresh:
            page = self._pool.read_fresh_page(self._file, page_index)
        else:
            with self._pool.pinned(self._file, page_index) as resident:
                page = resident.copy()
        self._seen[page_index] = page
        return page

    def __iter__(self) -> Iterator[PageIndex]:
        return iter(range(3, self._page_count))

    def __len__(self) -> int:
        return max(0, self._page_count - 3)


class OrderedIndex:
    """One persistent append-only exact ordered artifact.

    Reads are useful independently of query planning: callers can bulk-create an unreachable
    nonced generation, verify it, and obtain reverse candidates or heap-validated rows.  The
    normal registry and commit path start using this type only once OIX-2 can maintain it.
    """

    __slots__ = ("_definition", "_digest", "_file", "_pool")

    def __init__(self, definition: IndexDefinition, pool: BufferPool) -> None:
        if not isinstance(definition, IndexDefinition):
            raise GrafxIndexError(
                "An OrderedIndex needs an IndexDefinition.",
                field="definition",
                value=type(definition).__name__,
            )
        if definition.layout is not IndexLayout.ORDERED:
            raise GrafxIndexError(
                "An OrderedIndex can open only the ordered layout.",
                field="layout",
                value=definition.layout.value,
                index=definition.name,
            )
        if definition.visibility is not IndexVisibility.EXACT:
            raise GrafxIndexError(
                "An OrderedIndex implements only the exact candidate contract.",
                field="visibility",
                value=definition.visibility.value,
                index=definition.name,
            )
        if definition.artifact_nonce == 0:
            raise GrafxIndexError(
                "An ordered artifact needs a non-zero physical generation nonce.",
                field="artifact_nonce",
                value=0,
                index=definition.name,
            )
        if not isinstance(pool, BufferPool):
            raise GrafxIndexError(
                "An OrderedIndex needs the canonical BufferPool.",
                field="pool",
                value=type(pool).__name__,
                index=definition.name,
            )
        self._definition = definition
        self._digest = definition.digest()
        self._file = definition.file
        self._pool = pool

    @property
    def definition(self) -> IndexDefinition:
        return self._definition

    @property
    def file(self) -> str:
        return self._file

    @property
    def name(self) -> str:
        return self._definition.name

    def exists(self) -> bool:
        return self._pool.storage.exists(self._file)

    def is_created(self) -> bool:
        """Say whether page 0 is a complete header for this exact artifact."""

        if not self.exists() or self._pool.storage.page_count(self._file) == 0:
            return False
        try:
            self._read_header_fresh()
        except GrafxCorruptionDetected as failure:
            if failure.details.get("field") in {"page_type", "slot_count"}:
                return False
            raise
        return True

    def create(
        self,
        entries: Iterable[IndexEntry],
        *,
        applied_through_lsn: Lsn,
        reconciled_through_lsn: Lsn = NO_LSN,
    ) -> OrderedRootDescriptor:
        """Exclusively create and durably publish one verified bulk generation.

        The physical file is nonced and remains unreachable until catalog activation.  Tree
        pages, both independent roots, and finally the static header cross separate durability
        barriers.  Consequently an observed page 0 is proof that the complete artifact landed.
        """

        descriptor = self._root_descriptor(
            generation=1,
            root_page=NO_PAGE,
            height=0,
            entry_count=0,
            applied_through_lsn=applied_through_lsn,
            reconciled_through_lsn=reconciled_through_lsn,
        )
        build = build_ordered_tree(
            entries,
            page_size=self._pool.page_size,
            page_lsn=applied_through_lsn,
        )
        descriptor = self._root_descriptor(
            generation=1,
            root_page=build.root_page,
            height=build.height,
            entry_count=build.entry_count,
            applied_through_lsn=applied_through_lsn,
            reconciled_through_lsn=reconciled_through_lsn,
        )
        storage = self._pool.storage
        try:
            storage.create(self._file)
        except GrafxUnsupportedOperation as failure:
            # Narrow test/storage collaborators predating the normalized reason still expose
            # the decisive postcondition: the exact logical name now exists.  Never reinterpret
            # another refusal as a create race when the name remains absent.
            if (
                failure.details.get("reason") != "file_exists"
                and not storage.exists(self._file)
            ):
                raise
            if self.is_created():
                current = self.open()
                if current != descriptor:
                    raise GrafxIndexError(
                        "The ordered artifact already exists with a different root.",
                        field="artifact_generation",
                        index=self.name,
                        file=self.file,
                        expected=descriptor.generation,
                        observed=current.generation,
                        retryable=True,
                    )
                return current
            raise GrafxCorruptionDetected(
                "The ordered artifact name exists but its publication header is incomplete.",
                field="ordered_artifact_incomplete",
                index=self.name,
                file=self.file,
            ) from failure

        total_pages = 3 + len(build.pages)
        first = storage.allocate(self._file, total_pages)
        if first != HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                "A new ordered artifact did not allocate its header at page zero.",
                field="page_index",
                value=first,
                file=self.file,
            )

        # Phase 1: immutable tree pages.  Root and header pages remain zero-filled and cannot
        # publish the tree while this phase is incomplete.
        for image in build.pages:
            self._replace_page(image.page_index, image)
        self._pool.flush(self._file)
        storage.durable_barrier(self._file)

        # Phase 2: two physically independent complete roots.
        self._replace_page(
            ORDERED_ROOT_PAGE_A,
            make_ordered_root_page(
                descriptor, ORDERED_ROOT_PAGE_A, page_size=self._pool.page_size
            ),
        )
        self._replace_page(
            ORDERED_ROOT_PAGE_B,
            make_ordered_root_page(
                descriptor, ORDERED_ROOT_PAGE_B, page_size=self._pool.page_size
            ),
        )
        self._pool.flush(self._file)
        storage.durable_barrier(self._file)

        # Phase 3 is the new-file publication point.  Generic BufferPool flush ordering also
        # keeps page 0 last if a future caller happens to dirty more than this page.
        self._replace_page(HEADER_PAGE_INDEX, self._header_page(descriptor))
        self._pool.flush(self._file)
        storage.durable_barrier(self._file)

        selected = self.open()
        if selected != descriptor:
            raise GrafxCorruptionDetected(
                "The ordered artifact did not reopen at the root it published.",
                field="ordered_root",
                file=self.file,
            )
        return selected

    def open(self) -> OrderedRootDescriptor:
        """Open, identify and completely verify the currently selected tree."""

        certificate = self._read_certificate()
        descriptor = certificate.selection.descriptor
        loader = _OrderedPageLoader(self._pool, self._file, fresh=True)
        verify_ordered_tree(
            descriptor.root_page,
            descriptor.height,
            loader,
            expected_entry_count=descriptor.entry_count,
        )
        return descriptor

    def verify(self) -> OrderedTreeVerification:
        """Return the complete structural verification report for the selected root."""

        certificate = self._read_certificate()
        descriptor = certificate.selection.descriptor
        return verify_ordered_tree(
            descriptor.root_page,
            descriptor.height,
            _OrderedPageLoader(self._pool, self._file, fresh=True),
            expected_entry_count=descriptor.entry_count,
        )

    def publish_committed_batch(
        self,
        changes: Iterable[IndexChange],
        *,
        applied_through_lsn: Lsn,
    ) -> OrderedBatchPublication:
        """Publish one already-WAL-durable exact batch through append-only COW pages.

        Every conforming ordered writer enters the same page-0 write section.  Within it, the
        current dual-root certificate is read once, the batch is planned against immutable
        pages, one contiguous run is allocated, and the new tree pages cross a grouped data
        barrier before the alternate root page crosses its grouped root barrier.  Commit-state
        publication remains the transaction manager's next step; this door never performs it.

        Recovery may call this again.  A selected root already at or beyond the batch LSN is the
        idempotence proof; the file is barriered again before the skip is acknowledged so an
        earlier write-that-landed-then-raised cannot turn an uncertain root into false success.
        """

        try:
            materialized = tuple(changes)
        except TypeError as failure:
            raise GrafxIndexError(
                "An ordered publication needs an iterable of IndexChange values.",
                field="changes",
                value=type(changes).__name__,
                index=self.name,
            ) from failure
        for change in materialized:
            if not isinstance(change, IndexChange):
                raise GrafxIndexError(
                    "An ordered publication accepts only IndexChange values.",
                    field="changes",
                    value=type(change).__name__,
                    index=self.name,
                )
            if change.index.casefold() != self.name.casefold():
                raise GrafxIndexError(
                    "An ordered publication received a change for another index.",
                    field="index",
                    value=change.index,
                    index=self.name,
                )

        # OrderedRootDescriptor owns the exact position-domain validation.  Constructing a
        # provisional descriptor before any file mutation keeps malformed positions pre-write.
        self._root_descriptor(
            generation=1,
            root_page=NO_PAGE,
            height=0,
            entry_count=0,
            applied_through_lsn=applied_through_lsn,
            reconciled_through_lsn=NO_LSN,
        )
        publication_page: PageIndex | None = None
        allocated_pages: tuple[PageIndex, ...] = ()
        with self._pool.page_write_fence(self.file, HEADER_PAGE_INDEX):
            before = self._read_certificate()
            current = before.selection.descriptor
            if current.applied_through_lsn >= applied_through_lsn:
                self._pool.storage.durable_barrier(self.file)
                return OrderedBatchPublication(
                    current,
                    len(materialized),
                    0,
                    0,
                    replay_skipped=True,
                )
            start_page = self._pool.storage.page_count(self.file)
            mutation = mutate_ordered_tree(
                current.root_page,
                current.height,
                current.entry_count,
                _OrderedPageLoader(self._pool, self.file, fresh=False),
                materialized,
                page_size=self._pool.page_size,
                start_page=start_page,
                page_lsn=applied_through_lsn,
            )
            reconciled = current.reconciled_through_lsn
            for change in materialized:
                if change.operation is IndexOperation.REMOVE:
                    reconciled = max(reconciled, change.csn)
            descriptor = self._root_descriptor(
                generation=current.generation + 1,
                root_page=mutation.root_page,
                height=mutation.height,
                entry_count=mutation.entry_count,
                applied_through_lsn=applied_through_lsn,
                reconciled_through_lsn=reconciled,
            )
            publication_page = before.selection.publication_page
            try:
                if mutation.pages:
                    first = self._pool.storage.allocate(
                        self.file, len(mutation.pages)
                    )
                    if first != start_page:
                        raise GrafxCorruptionDetected(
                            "Ordered COW allocation did not begin at the planned append point.",
                            field="page_index",
                            value=first,
                            expected=start_page,
                            file=self.file,
                        )
                    allocated_pages = tuple(
                        range(first, first + len(mutation.pages))
                    )
                    for image in mutation.pages:
                        self._replace_page(image.page_index, image)
                    self._pool.flush(self.file)
                    self._pool.storage.durable_barrier(self.file)

                # A clean resident root may predate the fresh certificate.  Drop it without
                # write-back before installing the alternate complete descriptor.
                self._pool.discard_clean_page(self.file, publication_page)
                self._replace_page(
                    publication_page,
                    make_ordered_root_page(
                        descriptor,
                        publication_page,
                        page_size=self._pool.page_size,
                    ),
                )
                self._pool.flush(self.file)
                self._pool.storage.durable_barrier(self.file)
                after = self._read_certificate()
                if (
                    after.selection.descriptor != descriptor
                    or after.selection.page_index != publication_page
                ):
                    raise GrafxCorruptionDetected(
                        "The ordered root publication did not become the selected generation.",
                        field="ordered_root",
                        file=self.file,
                        expected_generation=descriptor.generation,
                        observed_generation=after.selection.descriptor.generation,
                    )
            except BaseException as failure:
                # No published root reaches a failed data prefix.  Dirty frames must not be
                # allowed to escape and write themselves during an unrelated later flush.
                for page_index in allocated_pages:
                    try:
                        self._pool.discard(self.file, page_index)
                    except BaseException as cleanup_failure:  # pragma: no cover - note only
                        failure.add_note(
                            f"Discarding failed ordered COW page {page_index} also failed: "
                            f"{cleanup_failure!r}"
                        )
                if publication_page is not None:
                    try:
                        self._pool.discard(self.file, publication_page)
                    except BaseException as cleanup_failure:  # pragma: no cover - note only
                        failure.add_note(
                            "Discarding the failed ordered root frame also failed: "
                            f"{cleanup_failure!r}"
                        )
                raise
            return OrderedBatchPublication(
                descriptor=descriptor,
                changes=len(materialized),
                pages_written=len(mutation.pages),
                missing_targets=mutation.missing_targets,
            )

    def candidates_desc(
        self,
        snapshot: SnapshotLike,
        *,
        upper_key: bytes | None = None,
        limit: int,
    ) -> tuple[IndexEntry, ...]:
        """Return a stable descending candidate prefix below an exclusive logical key."""

        wanted = _required_limit(limit)
        read_lsn = _required_read_lsn(snapshot)

        def materialize(descriptor: OrderedRootDescriptor) -> tuple[IndexEntry, ...]:
            return tuple(
                walk_ordered_desc(
                    descriptor.root_page,
                    descriptor.height,
                    _OrderedPageLoader(self._pool, self._file, fresh=False),
                    upper_key=upper_key,
                    limit=wanted,
                )
            )

        return self._stable_read(read_lsn, materialize)

    def visible_desc(
        self,
        heap: HeapStore,
        table: TableDef,
        snapshot: SnapshotLike,
        *,
        upper_key: bytes | None = None,
        limit: int,
    ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
        """Return heap-validated visible rows in descending ordered-key order.

        The tree walk is deliberately unbounded by the requested row count: stale and invisible
        exact candidates may be skipped, so stopping after ``limit`` candidates could omit a
        valid row that follows them.
        """

        wanted = _required_limit(limit)
        read_lsn = _required_read_lsn(snapshot)
        self._require_table(table)
        if not isinstance(heap, HeapStore):
            raise GrafxIndexError(
                "An ordered exact read needs the canonical HeapStore for revalidation.",
                field="heap",
                value=type(heap).__name__,
                index=self.name,
            )

        def materialize(
            descriptor: OrderedRootDescriptor,
        ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
            selected: list[tuple[RecordRef, HeapVersion]] = []
            candidates = walk_ordered_desc(
                descriptor.root_page,
                descriptor.height,
                _OrderedPageLoader(self._pool, self._file, fresh=False),
                upper_key=upper_key,
            )
            for entry in candidates:
                version = heap.read_if(
                    entry.ref,
                    lambda _record_id, xmin, xmax: snapshot.visible(xmin, xmax),
                )
                if version is None:
                    continue
                if version.table_id != table.table_id:
                    raise GrafxCorruptionDetected(
                        "An ordered index candidate points into a different heap table.",
                        field="table_id",
                        index=self.name,
                        file=self.file,
                        page=entry.ref.page,
                        slot=entry.ref.slot,
                        expected_table_id=table.table_id,
                        observed_table_id=version.table_id,
                    )
                # Exact entries are supersets.  A committed update may legitimately leave its
                # former key behind until maintenance reclaims it, so drift means skip, not
                # damage.  The physical reference/table mismatch above is still corruption.
                if (
                    self._definition.entry_key_for_record(
                        version.record_id, version.values
                    )
                    != entry.key
                ):
                    continue
                selected.append((entry.ref, version))
                if len(selected) == wanted:
                    break
            return tuple(selected)

        return self._stable_read(read_lsn, materialize)

    def _stable_read(
        self,
        read_lsn: Lsn,
        operation: Callable[[OrderedRootDescriptor], _ReadResult],
    ) -> _ReadResult:
        """Run one complete read between equal fresh header/root certificates."""
        last: OrderedIndexReadCertificate | None = None
        for attempt in range(ORDERED_READ_RETRY_BUDGET + 1):
            before = self._read_certificate()
            descriptor = before.selection.descriptor
            if descriptor.applied_through_lsn < read_lsn:
                raise GrafxIndexError(
                    "The ordered index does not cover the requested snapshot.",
                    field="index_view_unavailable",
                    index=self.name,
                    file=self.file,
                    applied_through_lsn=descriptor.applied_through_lsn,
                    required_lsn=read_lsn,
                    retryable=True,
                )
            try:
                result = operation(descriptor)
            except GrafxCorruptionDetected:
                # A root may legitimately move while its old pages are being inspected.  Retry
                # only when the complete durable certificate proves that concurrent drift;
                # stable damage remains an integrity failure.
                after_failure = self._read_certificate()
                if after_failure != before and attempt < ORDERED_READ_RETRY_BUDGET:
                    last = after_failure
                    continue
                raise
            after = self._read_certificate()
            if after == before:
                return result
            last = after
        raise GrafxIndexError(
            "The ordered index changed during every bounded read attempt.",
            field="index_view_changed",
            index=self.name,
            file=self.file,
            generation=(
                None if last is None else last.selection.descriptor.generation
            ),
            retryable=True,
        )

    def _read_certificate(self) -> OrderedIndexReadCertificate:
        """Read the static identity and both root pages directly from the device."""

        header_page, header = self._read_header_fresh()
        page_a = self._read_root_fresh(ORDERED_ROOT_PAGE_A)
        page_b = self._read_root_fresh(ORDERED_ROOT_PAGE_B)
        selection = select_ordered_root(page_a, page_b)
        descriptor = selection.descriptor
        if descriptor.artifact_nonce != self._definition.artifact_nonce:
            raise GrafxCorruptionDetected(
                "The selected ordered root belongs to a different artifact nonce.",
                field="artifact_nonce",
                index=self.name,
                file=self.file,
                expected=self._definition.artifact_nonce,
                observed=descriptor.artifact_nonce,
            )
        if descriptor.definition_digest != self._digest:
            raise GrafxCorruptionDetected(
                "The selected ordered root belongs to a different index definition.",
                field="definition_digest",
                index=self.name,
                file=self.file,
            )
        if (
            descriptor.applied_through_lsn < header.built_through_lsn
            or descriptor.reconciled_through_lsn < header.reconciled_through_lsn
        ):
            raise GrafxCorruptionDetected(
                "The selected ordered root regresses behind the artifact's creation horizon.",
                field="applied_through_lsn",
                index=self.name,
                file=self.file,
                header_built_through_lsn=header.built_through_lsn,
                root_applied_through_lsn=descriptor.applied_through_lsn,
                header_reconciled_through_lsn=header.reconciled_through_lsn,
                root_reconciled_through_lsn=descriptor.reconciled_through_lsn,
            )
        return OrderedIndexReadCertificate(
            header_seq=header_page.seq,
            header=header,
            root_a_seq=None if page_a is None else page_a.seq,
            root_b_seq=None if page_b is None else page_b.seq,
            selection=selection,
        )

    def _read_header_fresh(self) -> tuple[Page, IndexHeader]:
        page = self._pool.read_fresh_page(self._file, HEADER_PAGE_INDEX)
        if page.flags != 0 or page.next_page != NO_PAGE or page.header().reserved != 0:
            raise GrafxCorruptionDetected(
                "The ordered index header page carries unsupported page metadata.",
                field="page_header",
                file=self.file,
                page=HEADER_PAGE_INDEX,
            )
        if page.seq & 1:
            raise GrafxCorruptionDetected(
                "The ordered index header page carries an in-progress sequence.",
                field="seq",
                value=page.seq,
                file=self.file,
                page=HEADER_PAGE_INDEX,
            )
        file_header = FileHeaderPage.read(page)
        if file_header.kind is not FileKind.INDEX:
            raise GrafxCorruptionDetected(
                "The ordered artifact does not carry an index file header.",
                field="kind",
                value=file_header.kind.name,
                file=self.file,
            )
        if file_header.page_size != self._pool.page_size:
            raise GrafxCorruptionDetected(
                "The ordered artifact page size differs from this database.",
                field="page_size",
                value=file_header.page_size,
                expected=self._pool.page_size,
                file=self.file,
            )
        if file_header.root_page != NO_PAGE or file_header.payload_length != 0:
            raise GrafxCorruptionDetected(
                "An ordered index file header cannot name a chained payload.",
                field="file_header",
                file=self.file,
            )
        if page.slot_count != INDEX_HEADER_SLOT + 1:
            raise GrafxCorruptionDetected(
                "An ordered index header page must carry exactly two records.",
                field="slot_count",
                value=page.slot_count,
                file=self.file,
                page=HEADER_PAGE_INDEX,
            )
        header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
        if (
            header.format_version != ORDERED_INDEX_HEADER_FORMAT_VERSION
            or header.layout is not IndexLayout.ORDERED
            or header.visibility is not IndexVisibility.EXACT
            or header.table_id != self._definition.table_id
            or header.bucket_count != 1
            or header.digest != self._digest
            or header.artifact_nonce != self._definition.artifact_nonce
            or header.flags != 0
        ):
            raise GrafxIndexError(
                "The ordered artifact header does not identify this index definition.",
                field="index_header_identity",
                index=self.name,
                file=self.file,
            )
        return page, header

    def _read_root_fresh(self, page_index: PageIndex) -> Page | None:
        try:
            return self._pool.read_fresh_page(self._file, page_index)
        except GrafxSchemaVersionMismatch:
            raise
        except GrafxCorruptionDetected:
            return None

    def _replace_page(self, page_index: PageIndex, image: Page) -> None:
        if image.page_index != page_index:
            raise GrafxCorruptionDetected(
                "An ordered page image was offered at a different physical index.",
                field="page_index",
                value=image.page_index,
                expected=page_index,
                file=self.file,
            )
        with self._pool.pinned(self._file, page_index) as resident:
            resident.replace_with(image)

    def _header_page(self, descriptor: OrderedRootDescriptor) -> Page:
        page = Page(
            int(PageType.META),
            page_size=self._pool.page_size,
            page_index=HEADER_PAGE_INDEX,
        )
        FileHeaderPage.initialize(
            page, FileHeader(kind=FileKind.INDEX, page_size=self._pool.page_size)
        )
        page.insert_slot(
            IndexHeader(
                visibility=IndexVisibility.EXACT,
                table_id=self._definition.table_id,
                bucket_count=1,
                digest=self._digest,
                built_through_lsn=descriptor.applied_through_lsn,
                reconciled_through_lsn=descriptor.reconciled_through_lsn,
                artifact_nonce=self._definition.artifact_nonce,
                format_version=ORDERED_INDEX_HEADER_FORMAT_VERSION,
                layout=IndexLayout.ORDERED,
            ).encode()
        )
        return page

    def _root_descriptor(
        self,
        *,
        generation: int,
        root_page: PageIndex,
        height: int,
        entry_count: int,
        applied_through_lsn: Lsn,
        reconciled_through_lsn: Lsn,
    ) -> OrderedRootDescriptor:
        return OrderedRootDescriptor(
            artifact_nonce=self._definition.artifact_nonce,
            generation=generation,
            root_page=root_page,
            height=height,
            entry_count=entry_count,
            applied_through_lsn=applied_through_lsn,
            reconciled_through_lsn=reconciled_through_lsn,
            definition_digest=self._digest,
        )

    def _require_table(self, table: object) -> None:
        if not isinstance(table, TableDef):
            raise GrafxIndexError(
                "An ordered exact read needs a TableDef.",
                field="table",
                value=type(table).__name__,
                index=self.name,
            )
        if (
            table.table_id != self._definition.table_id
            or table.name != self._definition.table_name
        ):
            raise GrafxIndexError(
                "The ordered index was asked to validate a different table.",
                field="table",
                index=self.name,
                expected_table_id=self._definition.table_id,
                observed_table_id=table.table_id,
                expected_table=self._definition.table_name,
                observed_table=table.name,
            )
