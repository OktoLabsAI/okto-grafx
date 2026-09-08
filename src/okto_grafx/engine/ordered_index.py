"""Persistent exact ordered-index storage and heap-validated reverse reads.

This module owns the engine half of the ordered format.  The tree pages are immutable, page 0
identifies one nonced artifact, and pages 1/2 alternate complete root publications.  A root is
only an exact-index candidate authority: every result is read again from the heap under the
caller's snapshot before it can leave this module.

Transactional copy-on-write maintenance shares the ordinary secondary-index staging/WAL
contract, while this module retains the layout-specific publication and read certificates.  The
query planner still cannot select the ordered format merely because its domain codecs exist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TypeVar

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import (
    NO_LSN,
    NO_PAGE,
    PROVISIONAL_CSN,
    Csn,
    Lsn,
    PageIndex,
    RecordRef,
)
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
    ReconcileReport,
    SnapshotLike,
    StagingTransaction,
    build_ordered_tree,
    make_ordered_root_page,
    mutate_ordered_tree,
    seek_ordered_exact,
    decode_ordered_root_page,
    select_ordered_root,
    verify_ordered_tree,
    walk_ordered_desc,
)
from okto_grafx.domain.index.records import change_of, lsn_of
from okto_grafx.domain.index.visibility import is_reclaimable
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
)
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexStore, _IndexReadCertificate

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

    @property
    def visited(self) -> frozenset[PageIndex]:
        """Return every page this loader has read so far, decodable or not."""
        return frozenset(self._seen)

    def __iter__(self) -> Iterator[PageIndex]:
        return iter(range(3, self._page_count))

    def __len__(self) -> int:
        return max(0, self._page_count - 3)


class OrderedIndex(IndexStore):
    """One persistent append-only exact ordered artifact.

    Reads are useful independently of query planning: callers can bulk-create an unreachable
    nonced generation, verify it, and obtain reverse candidates or heap-validated rows.  The
    normal registry and commit path use the shared staging contract, while all physical mutation
    remains a single root-certified COW batch.
    """

    __slots__ = ()

    def __init__(
        self,
        definition: IndexDefinition,
        pool: BufferPool,
        metrics: MetricsSink,
    ) -> None:
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
        if not isinstance(metrics, MetricsSink):
            raise GrafxIndexError(
                "An OrderedIndex needs the metrics port used by the index framework.",
                field="metrics",
                value=type(metrics).__name__,
                index=definition.name,
            )
        super().__init__(definition, pool, metrics)
        self._page_type = int(PageType.INDEX_ORDERED_LEAF)

    def is_created(self, *, proved_present: bool = False) -> bool:
        """Say whether page 0 is a complete header for this exact artifact."""

        if (
            (not proved_present and not self.exists())
            or self._pool.storage.page_count(self._file) == 0
        ):
            return False
        try:
            self._read_header_fresh()
        except GrafxCorruptionDetected as failure:
            if failure.details.get("field") in {"page_type", "slot_count"}:
                return False
            raise
        return True

    def create_bulk(
        self,
        entries: Iterable[IndexEntry],
        *,
        applied_through_lsn: Lsn,
        reconciled_through_lsn: Lsn = NO_LSN,
        _precreated: bool = False,
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
        if not _precreated:
            try:
                storage.create(self._file)
            except GrafxUnsupportedOperation as failure:
                # Narrow test/storage collaborators predating the normalized reason still expose
                # the decisive postcondition: the exact logical name now exists.  Never
                # reinterpret another refusal as a create race when the name remains absent.
                if (
                    failure.details.get("reason") != "file_exists"
                    and not storage.exists(self._file)
                ):
                    raise
                if self.is_created():
                    current = self.open_root()
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

        selected = self.open_root()
        if selected != descriptor:
            raise GrafxCorruptionDetected(
                "The ordered artifact did not reopen at the root it published.",
                field="ordered_root",
                file=self.file,
            )
        return selected

    def create(self, *, proved_present: bool = False) -> IndexHeader:
        """Create an empty ordered artifact or open the existing complete generation."""

        header, _created = self._create_with_provenance(
            proved_present=proved_present
        )
        return header

    def _create_with_provenance(
        self, *, proved_present: bool = False
    ) -> tuple[IndexHeader, bool]:
        if self.is_created(proved_present=proved_present):
            return self.open(proved_present=proved_present), False
        if proved_present or self.exists():
            raise GrafxCorruptionDetected(
                "The ordered artifact name exists but its publication header is incomplete.",
                field="ordered_artifact_incomplete",
                index=self.name,
                file=self.file,
            )
        try:
            self._pool.storage.create(self.file)
        except GrafxUnsupportedOperation as failure:
            if (
                failure.details.get("reason") != "file_exists"
                and not self.exists()
            ):
                raise
            if self.is_created():
                return self.open(), False
            raise GrafxCorruptionDetected(
                "The ordered artifact name was concurrently reserved but is incomplete.",
                field="ordered_artifact_incomplete",
                index=self.name,
                file=self.file,
                retryable=True,
            ) from failure
        self.create_bulk(
            (), applied_through_lsn=NO_LSN, _precreated=True
        )
        return self.open(), True

    def open_root(self) -> OrderedRootDescriptor:
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

    def open(self, *, proved_present: bool = False) -> IndexHeader:
        """Open and fully verify the artifact, returning its current logical header."""

        if proved_present and self._pool.storage.page_count(self.file) == 0:
            raise GrafxIndexError(
                "The proved ordered artifact is empty.",
                field="file",
                index=self.name,
                file=self.file,
            )
        descriptor = self.open_root()
        return self._header_at(self._read_header_fresh()[1], descriptor)

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

    @staticmethod
    def _header_at(
        header: IndexHeader, descriptor: OrderedRootDescriptor
    ) -> IndexHeader:
        """Project mutable root watermarks onto the immutable artifact identity header."""

        return replace(
            header,
            built_through_lsn=descriptor.applied_through_lsn,
            reconciled_through_lsn=descriptor.reconciled_through_lsn,
        )

    def _fresh_certificate(self) -> _IndexReadCertificate:
        """Return the manager certificate shape, backed by one fresh dual-root proof."""

        certificate = self._read_certificate()
        descriptor = certificate.selection.descriptor
        return _IndexReadCertificate(
            seq=descriptor.generation,
            header=self._header_at(certificate.header, descriptor),
        )

    def _remember_local_certificate(self) -> _IndexReadCertificate:
        """Bind manager cache state to the root generation this pool just published."""

        certificate = self._fresh_certificate()
        self._carried_certificate = None
        self._cache_certificate = certificate
        self._local_certificate = certificate
        return certificate

    @property
    def built_through_lsn(self) -> Lsn:
        return self._read_certificate().selection.descriptor.applied_through_lsn

    @property
    def reconciled_through_lsn(self) -> Lsn:
        return self._read_certificate().selection.descriptor.reconciled_through_lsn

    def check_freshness(
        self,
        published_lsn: Lsn,
        *,
        required_lsn: Lsn | None = None,
        persist: bool = True,
        allow_ahead: bool = False,
    ) -> bool:
        """Compare the selected root with the table floor; an exact root may be ahead safely."""

        del persist  # Every read proves the root watermark again; no mutable stale bit is needed.
        for field, value in (("published_lsn", published_lsn), ("required_lsn", required_lsn)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < NO_LSN
            ):
                raise GrafxIndexError(
                    f"An ordered index needs a non-negative integer {field}.",
                    field=field,
                    value=repr(value),
                    index=self.name,
                )
        required = published_lsn if required_lsn is None else required_lsn
        if required > published_lsn and not allow_ahead:
            raise GrafxIndexError(
                "The ordered index table floor is ahead of the published database position.",
                field="required_lsn",
                value=required,
                published_lsn=published_lsn,
                index=self.name,
            )
        certificate = self._fresh_certificate()
        if certificate.header.built_through_lsn < required:
            self._stale_reason = (
                f"Ordered index {self.name!r} covers position "
                f"{certificate.header.built_through_lsn}, before its table floor {required}."
            )
            self._stale_device_seq = certificate.seq
            return True
        if (
            self._stale_reason is not None
            and self._stale_device_seq is not None
            and certificate.seq != self._stale_device_seq
        ):
            self._stale_reason = None
            self._stale_device_seq = None
        return self._stale_reason is not None

    def mark_stale(self, reason: str, *, persist: bool = True) -> None:
        """Refuse this handle; root freshness makes the same omission fail closed elsewhere."""

        del persist
        if not isinstance(reason, str) or not reason:
            raise GrafxIndexError(
                "Marking an ordered index stale needs a reason.",
                field="reason",
                value=repr(reason),
                index=self.name,
            )
        self._stale_reason = reason
        self._stale_device_seq = self._fresh_certificate().seq
        self._carried_certificate = None

    def advance_built_through(self, lsn: Lsn) -> None:
        """Publish a completed-replay/empty-observation watermark without rebuilding entries."""

        current = self._read_certificate().selection.descriptor
        if current.applied_through_lsn < lsn:
            self.publish_committed_batch((), applied_through_lsn=lsn)
        self._stale_reason = None
        self._stale_device_seq = None
        self._remember_local_certificate()

    def complete_built_through(self, lsn: Lsn, table_high_water: Lsn | None) -> None:
        """Refuse to certify a damaged surviving root past data it may have lost.

        A hash header can safely advance after complete logical replay.  An ordered watermark is
        the root authority itself: advancing an older surviving root after the newer copy was
        damaged would certify a tree that omits committed rows once the corresponding WAL was
        checkpointed away.  A root that is merely behind because intervening commits did not
        touch this index retains the established completed-replay advance.  Recovery therefore
        leaves only an actually damaged-and-behind generation stale and available for rebuild,
        while the database and canonical heap remain usable.
        """

        if isinstance(lsn, bool) or not isinstance(lsn, int) or lsn < NO_LSN:
            raise GrafxIndexError(
                "An ordered replay completion needs a non-negative integer position.",
                field="lsn",
                value=repr(lsn),
                index=self.name,
            )
        if table_high_water is not None and (
            isinstance(table_high_water, bool)
            or not isinstance(table_high_water, int)
            or table_high_water < NO_LSN
        ):
            raise GrafxIndexError(
                "An ordered replay completion needs a non-negative table high-water.",
                field="table_high_water",
                value=repr(table_high_water),
                index=self.name,
            )
        certificate = self._read_certificate()
        covered = certificate.selection.descriptor.applied_through_lsn
        if certificate.selection.damaged_pages and (
            table_high_water is None or covered < table_high_water
        ):
            self._stale_reason = (
                f"Ordered index {self.name!r} survived recovery through {covered}, before "
                f"its table high-water {table_high_water!r}; a root copy was damaged and "
                "an explicit rebuild is required."
            )
            self._stale_device_seq = certificate.selection.descriptor.generation
            self._carried_certificate = None
            return
        super().complete_built_through(lsn, table_high_water)

    def stage_reset(
        self,
        txn: StagingTransaction,
        built_through: Lsn,
        *,
        rebuild_token: int = 0,
        defer_clear: bool = False,
    ) -> WalRecord:
        """Refuse in-place RESET; ordered compaction publishes a fresh nonced generation."""

        del txn, built_through, rebuild_token, defer_clear
        raise GrafxUnsupportedOperation(
            "An ordered index is rebuilt by publishing a fresh compact generation.",
            field="ordered_rebuild_generation",
            index=self.name,
            file=self.file,
        )

    def commit(self, txn: StagingTransaction, csn: Csn) -> int:
        """Publish this transaction's complete staged set as one COW root generation."""

        txn_id = self._require_txn(txn)
        stamp = self._require_csn("csn", csn)
        staged = self._staged.get(txn_id)
        if staged is None:
            return 0
        if any(change.operation is IndexOperation.RESET for change in staged.changes):
            raise GrafxUnsupportedOperation(
                "An ordered commit cannot apply an in-place RESET.",
                field="ordered_rebuild_generation",
                index=self.name,
                file=self.file,
            )
        report = self.publish_committed_batch(
            staged.changes, applied_through_lsn=stamp
        )
        self._missing_targets += report.missing_targets
        self._staged.pop(txn_id, None)
        self._stale_reason = None
        self._stale_device_seq = None
        self._remember_local_certificate()
        return len(staged.changes)

    def apply(self, record: WalRecord) -> None:
        """Replay one logical record idempotently through the ordered root watermark."""

        change = change_of(record)
        if change.index != self.name:
            raise GrafxIndexError(
                "A WAL record was offered to a different ordered index.",
                field="index",
                value=change.index,
                index=self.name,
            )
        if change.versioned or change.operation is IndexOperation.RESET:
            raise GrafxCorruptionDetected(
                "An ordered exact artifact cannot replay this index-record shape.",
                field=(
                    "versioned"
                    if change.versioned
                    else "ordered_rebuild_generation"
                ),
                index=self.name,
                operation=change.operation.name,
            )
        self.apply_replay_batch((change,), through_lsn=lsn_of(record))

    def apply_replay_batch(
        self, changes: Iterable[IndexChange], *, through_lsn: Lsn
    ) -> OrderedBatchPublication:
        """Replay one prevalidated WAL subsequence as a single idempotent COW publication."""

        materialized = tuple(changes)
        if any(
            change.index != self.name
            or change.versioned
            or change.operation is IndexOperation.RESET
            for change in materialized
        ):
            raise GrafxCorruptionDetected(
                "An ordered replay batch contains an incompatible logical change.",
                field="ordered_replay_batch",
                index=self.name,
            )
        report = self.publish_committed_batch(
            materialized, applied_through_lsn=through_lsn
        )
        self._missing_targets += report.missing_targets
        self._stale_reason = None
        self._stale_device_seq = None
        self._remember_local_certificate()
        return report

    def lookup(self, key: bytes, snapshot: SnapshotLike) -> tuple[RecordRef, ...]:
        """Return exact candidates; IndexManager performs the mandatory heap validation."""

        self._require_readable()
        read_lsn = self._require_exact_read_lsn(snapshot)
        wanted = self._require_key(key)
        return tuple(
            entry.ref for entry in self._stable_candidates(wanted, read_lsn)
        )

    def _candidates_unchecked(self, wanted: bytes) -> tuple[IndexEntry, ...]:
        descriptor = self._read_certificate().selection.descriptor
        return seek_ordered_exact(
            descriptor.root_page,
            descriptor.height,
            _OrderedPageLoader(self._pool, self.file, fresh=False),
            wanted,
        )

    def reachable_pages(self) -> frozenset[PageIndex] | None:
        """Return the pages both root copies reach, or ``None`` without a complete proof.

        A door for the verifier, read fresh from the device and never from a resident frame.
        The proof is complete or it is nothing: the dual-root certificate must decode with no
        damaged root page; the selected tree and, when the alternate root page decodes, the
        alternate tree are structurally verified in full (shape, order, height, entry count);
        and the certificate must be unchanged afterwards.  Any refusal anywhere returns
        ``None`` and the caller keeps every verdict for every page of the file -- a partial
        set would let a page an aborted traversal never reached pass for an orphan.  Nothing
        is cached and nothing is repaired.
        """
        try:
            before = self._read_certificate()
        except GrafxError:
            return None
        if before.selection.damaged_pages:
            return None
        descriptors = [before.selection.descriptor]
        alternate_page = self._read_root_fresh(before.selection.publication_page)
        if alternate_page is not None:
            try:
                alternate = decode_ordered_root_page(alternate_page)
            except GrafxError:
                alternate = None
            if (
                alternate is not None
                and alternate.artifact_nonce == self._definition.artifact_nonce
                and alternate.root_page != NO_PAGE
            ):
                descriptors.append(alternate)
        loader = _OrderedPageLoader(self._pool, self._file, fresh=True)
        try:
            for descriptor in descriptors:
                if descriptor.root_page == NO_PAGE:
                    continue
                verify_ordered_tree(
                    descriptor.root_page,
                    descriptor.height,
                    loader,
                    expected_entry_count=descriptor.entry_count,
                )
            after = self._read_certificate()
        except (GrafxError, KeyError):
            return None
        if after != before:
            return None
        return loader.visited

    def walk(self) -> tuple[IndexEntry, ...]:
        """Return every reachable entry in deterministic ascending physical identity order."""

        def materialize(descriptor: OrderedRootDescriptor) -> tuple[IndexEntry, ...]:
            return tuple(
                reversed(
                    tuple(
                        walk_ordered_desc(
                            descriptor.root_page,
                            descriptor.height,
                            _OrderedPageLoader(self._pool, self.file, fresh=False),
                        )
                    )
                )
            )

        return self._stable_read(NO_LSN, materialize)

    def reconcile(
        self, horizon: Lsn, txn: StagingTransaction | None = None
    ) -> ReconcileReport:
        """Measure or stage reclaimable ordered tombstones through logical WAL records."""

        if txn is not None:
            self._require_txn(txn)
        scanned = reclaimable = removed = retained = 0
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
        """Advance an empty reconciliation horizon through the same root publication."""

        current = self._read_certificate().selection.descriptor
        if current.reconciled_through_lsn >= horizon:
            return
        self.publish_committed_batch(
            (),
            applied_through_lsn=current.applied_through_lsn,
            reconciled_through_lsn=horizon,
        )
        self._remember_local_certificate()

    def publish_committed_batch(
        self,
        changes: Iterable[IndexChange],
        *,
        applied_through_lsn: Lsn,
        reconciled_through_lsn: Lsn | None = None,
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
        if reconciled_through_lsn is not None:
            self._root_descriptor(
                generation=1,
                root_page=NO_PAGE,
                height=0,
                entry_count=0,
                applied_through_lsn=applied_through_lsn,
                reconciled_through_lsn=reconciled_through_lsn,
            )
        publication_page: PageIndex | None = None
        allocated_pages: tuple[PageIndex, ...] = ()
        with self._pool.page_write_fence(self.file, HEADER_PAGE_INDEX):
            before = self._read_certificate()
            current = before.selection.descriptor
            requested_reconciled = (
                current.reconciled_through_lsn
                if reconciled_through_lsn is None
                else reconciled_through_lsn
            )
            if (
                current.applied_through_lsn >= applied_through_lsn
                and current.reconciled_through_lsn >= requested_reconciled
            ):
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
            reconciled = max(
                current.reconciled_through_lsn, requested_reconciled
            )
            for change in materialized:
                if change.operation is IndexOperation.REMOVE:
                    reconciled = max(reconciled, change.csn)
            descriptor = self._root_descriptor(
                generation=current.generation + 1,
                root_page=mutation.root_page,
                height=mutation.height,
                entry_count=mutation.entry_count,
                applied_through_lsn=max(
                    current.applied_through_lsn, applied_through_lsn
                ),
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

        self._require_readable()
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

        self._require_readable()
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

    def iter_visible_desc(
        self,
        heap: HeapStore,
        table: TableDef,
        snapshot: SnapshotLike,
        *,
        upper_key: bytes | None = None,
    ) -> Iterator[tuple[bytes, RecordRef, HeapVersion]]:
        """Yield heap-validated rows lazily below one certified descending root.

        Unlike :meth:`visible_desc`, this door does not retain a candidate prefix per table.
        It is intended for a bounded k-way query merge: the caller holds at most one current
        row from each table and closes every iterator before exposing any result.  Closing is a
        semantic part of the read.  A fresh certificate is compared even when the caller stops
        early, so a root replacement can never bless a mixed or partially certified prefix.

        The immutable pages selected by ``before`` remain safe to traverse while another writer
        publishes a new root.  Drift still produces a retryable refusal because the query layer
        deliberately has no permission to replay after consuming part of this stream.
        """

        self._require_readable()
        read_lsn = _required_read_lsn(snapshot)
        self._require_table(table)
        if not isinstance(heap, HeapStore):
            raise GrafxIndexError(
                "An ordered exact read needs the canonical HeapStore for revalidation.",
                field="heap",
                value=type(heap).__name__,
                index=self.name,
            )

        required_lsn = self._required_table_position(read_lsn)
        before = self._read_certificate()
        descriptor = before.selection.descriptor
        if descriptor.applied_through_lsn < required_lsn:
            raise GrafxIndexError(
                "The ordered index does not cover the requested snapshot.",
                field="index_view_unavailable",
                index=self.name,
                file=self.file,
                applied_through_lsn=descriptor.applied_through_lsn,
                required_lsn=required_lsn,
                retryable=True,
            )

        failure: BaseException | None = None
        try:
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
                if (
                    self._definition.entry_key_for_record(
                        version.record_id, version.values
                    )
                    != entry.key
                ):
                    continue
                yield entry.key, entry.ref, version
        except GeneratorExit:
            # ``close()`` is an early but successful consumption boundary.  Let the certificate
            # check below replace GeneratorExit when the root drifted so callers observe it.
            raise
        except BaseException as caught:
            failure = caught
            raise
        finally:
            try:
                after = self._read_certificate()
                if after != before:
                    raise GrafxIndexError(
                        "The ordered index changed during a lazy certified read.",
                        field="index_view_changed",
                        index=self.name,
                        file=self.file,
                        generation=after.selection.descriptor.generation,
                        retryable=True,
                    )
            except BaseException as certificate_failure:
                if failure is None:
                    raise
                failure.add_note(
                    "Closing the ordered read certificate also failed with "
                    f"{type(certificate_failure).__name__}: {certificate_failure}"
                )

    def _stable_read(
        self,
        read_lsn: Lsn,
        operation: Callable[[OrderedRootDescriptor], _ReadResult],
    ) -> _ReadResult:
        """Run one complete read between equal fresh header/root certificates."""
        required_lsn = self._required_table_position(read_lsn)
        last: OrderedIndexReadCertificate | None = None
        for attempt in range(ORDERED_READ_RETRY_BUDGET + 1):
            before = self._read_certificate()
            descriptor = before.selection.descriptor
            if descriptor.applied_through_lsn < required_lsn:
                raise GrafxIndexError(
                    "The ordered index does not cover the requested snapshot.",
                    field="index_view_unavailable",
                    index=self.name,
                    file=self.file,
                    applied_through_lsn=descriptor.applied_through_lsn,
                    required_lsn=required_lsn,
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
        if descriptor.definition_digest != self._definition_digest:
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
            or header.digest != self._definition_digest
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
        if page_index in (ORDERED_ROOT_PAGE_A, ORDERED_ROOT_PAGE_B):
            self._pool.replace_clean_page_without_read(
                self._file, page_index, image
            )
            return
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
                digest=self._definition_digest,
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
            definition_digest=self._definition_digest,
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
