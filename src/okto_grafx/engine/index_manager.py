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

The position moves only on a commit that had something for THAT index. At open time the claimed
position is compared with the highest committed physical version of THAT index's table, while the
database-wide published position remains the upper bound. A commit to another table therefore
does not rewrite this index merely to keep a global clock level. Recovery closes a retained-WAL
gap with :meth:`IndexManager.mark_built_through`, which is the one thing an index cannot know for
itself -- that the replay just finished was complete. The conservative direction is deliberate:
an index never claims to cover a position it cannot show it covers, and the cost of being wrong
that way is a rebuild rather than a missing row.

*Every walk over a derived structure terminates.* A bucket is a chain of pages, and every walk
carries a visited set and a bound taken from the file's own page count, so damaged links fail
with a located error instead of hanging (amendment A42).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import NamedTuple, TypeVar

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
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
    RecordRef,
    SlotId,
    _require_decodable_ref,
    is_committed_csn,
    is_open_end_csn,
    is_provisional_csn,
)
from okto_grafx.domain.index.contract import SecondaryIndex, StagingTransaction
from okto_grafx.domain.index.definition import (
    INDEX_DIRECTORY,
    IndexDefinition,
    automatic_index_definitions,
    index_definition_matches_table,
    index_file,
    index_generation_file,
)
from okto_grafx.domain.index.entry import (
    INDEX_ENTRY_HEADER_SIZE,
    IndexEntry,
    _validated_image,
)
from okto_grafx.domain.index.header import (
    INDEX_HEADER_FORMAT_VERSION,
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
from okto_grafx.domain.model.catalog import CATALOG_LEGACY_FORMAT_VERSION
from okto_grafx.domain.model.schema import TableDef
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
from okto_grafx.domain.txn.context import RowIntent, TransactionContext
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.buffer_pool import (
    BufferPool,
    refuse_endless_chain,
    visited_pages,
)
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "INDEX_DIRECTORY",
    "INDEX_FLAG_STALE",
    "INDEX_READ_RETRY_BUDGET",
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

INDEX_READ_RETRY_BUDGET: int = 2
"""Fresh exact-index views retried after a concurrent header transition before refusing."""

_DETACHED_GENERATION_NONCE_ATTEMPTS: int = 64
"""Bounded provider draws used to find one unowned physical-generation name."""

_DEFINITION_MATCH_MEMO_LIMIT: int = 1024
"""Per-manager ceiling for immutable schema-provenance comparisons."""

_COMMON_REPLAY_HOT_BUCKET_MIN_EFFECTS: int = 8
"""Smallest per-bucket replay run worth pre-indexing for this one recovery call."""

_COMMON_REPLAY_HOT_TARGET_LIMIT: int = 65_536
"""Hard ceiling on target identities retained by all hot directories in one replay."""

_COMMON_REPLAY_HOT_PAGE_LIMIT: int = 16_384
"""Hard ceiling on bucket pages retained by all hot directories in one replay."""

_COMMON_REPLAY_HOT_BUCKET_LIMIT: int = 16_384
"""Hard ceiling on distinct bucket identities considered by the replay accelerator."""

_LIVE_COMMIT_HOT_BUCKET_MIN_EFFECTS: int = 2
"""Smallest fenced live-commit run that saves a second bucket scan."""

_LIVE_COMMIT_AUTHORITY_SEAL: object = object()
"""Module-private proof installed only by the transaction manager's fenced commit door."""

_LIVE_COMMIT_AUTHORITY: ContextVar[object | None] = ContextVar(
    "okto_grafx_live_commit_authority", default=None
)
"""Call-local authority whose mutable scope is revoked before its context is reset."""

_LIVE_HOT_HOOK_NAMES: tuple[str, ...] = (
    "_apply_change",
    "_bucket_pages",
    "_find_entry",
    "_place",
    "_rewrite",
    "_erase",
)
"""Physical hooks a live batch replaces and therefore requires in canonical form."""

_CANONICAL_LIVE_HOT_HOOKS: tuple[object, ...]
"""Original hook objects, bound after ``IndexStore`` is fully defined."""

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


class _IndexEntryHeader(NamedTuple):
    """Validated index metadata materialised without constructing an ``IndexEntry``."""

    page: PageIndex
    slot: SlotId
    encoded_ref: int
    born_csn: Csn
    dead_csn: Csn
    versioned: bool
    key: bytes


@dataclass(slots=True)
class _Staged:
    """One transaction's index effects, including a proved empty sparse observation.

    An empty ``changes`` list is meaningful: the transaction wrote the covered table, but a
    sparse definition decided that the written row owed this index no entry.  Keeping that
    observation lets commit advance the index's coverage without inventing a WAL effect.
    """

    txn_id: int
    artifact_nonce: int
    changes: list[IndexChange] = field(default_factory=list)
    defer_clear: bool = False
    """Whether this transaction's RESET leaves the stale refusal standing for its caller.

    Process-local on purpose. The record format reserves ``ref.slot`` for a reset, and replay
    never completes a rebuild -- only ``commit`` and ``clear_stale`` do -- so a recovered
    database already ends stale without being told. A durable flag would change a frozen record
    to express something the recovery path never has to read.
    """


@dataclass(slots=True)
class _LiveCommitAuthority:
    """Revocable scope for one manager/transaction/store inside the fenced commit call."""

    seal: object
    manager: IndexManager
    txn: object
    store: IndexStore | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class _IndexReadCertificate:
    """The durable page-0 state to which this process's bucket cache is attached."""

    seq: int
    header: IndexHeader


@dataclass(frozen=True, slots=True)
class _SpeculativeIndexArtifact:
    """Exact registry object and durable generation installed by one DDL effect.

    The token is deliberately opaque outside this module.  A rollback may release the
    process-local registration only while it still names this exact object; it may regard the
    canonical file as private only while page zero still has this exact durable generation.
    """

    index: IndexStore
    generation: _IndexReadCertificate
    created_file: bool
    owner: object


@dataclass(frozen=True, slots=True)
class _RebuildAuthority:
    """The stale page-0 generation one RESET may replace, and no other."""

    token: int
    header_seq: int
    through_lsn: Lsn


class _FirstFitPages:
    """Ephemeral first-fit directory for pages proved empty at the start of one build.

    The tree stores the greatest payload capacity below each node, so choosing the leftmost page
    that can hold an entry is logarithmic and byte-for-byte equivalent to ``_place`` walking the
    chain from its head.  It is never retained across a commit, recovery call or certificate.
    """

    __slots__ = ("pages", "_capacities", "_leaf_count", "_positions", "_tree")

    def __init__(self, pages: Sequence[PageIndex], capacities: Sequence[int]) -> None:
        self.pages: list[PageIndex] = list(pages)
        self._capacities: list[int] = list(capacities)
        self._positions: dict[PageIndex, int] = {
            page: position for position, page in enumerate(self.pages)
        }
        self._leaf_count = 1
        while self._leaf_count < len(self.pages):
            self._leaf_count *= 2
        self._tree: list[int] = [-1] * (2 * self._leaf_count)
        self._rebuild_tree()

    def first_fit(self, payload_size: int) -> int | None:
        """Return the first chain position that can hold ``payload_size``, if one exists."""
        if not self.pages or self._tree[1] < payload_size:
            return None
        node = 1
        while node < self._leaf_count:
            left = node * 2
            node = left if self._tree[left] >= payload_size else left + 1
        position = node - self._leaf_count
        return position if position < len(self.pages) else None

    def update(self, position: int, capacity: int) -> None:
        """Replace one page's capacity after a successful mutation."""
        self._capacities[position] = capacity
        node = self._leaf_count + position
        self._tree[node] = capacity
        node //= 2
        while node:
            self._tree[node] = max(self._tree[node * 2], self._tree[node * 2 + 1])
            node //= 2

    def append(self, page: PageIndex, capacity: int) -> None:
        """Add a new tail page, growing the tree geometrically."""
        self._positions[page] = len(self.pages)
        self.pages.append(page)
        self._capacities.append(capacity)
        if len(self.pages) > self._leaf_count:
            self._leaf_count *= 2
            self._tree = [-1] * (2 * self._leaf_count)
            self._rebuild_tree()
            return
        self.update(len(self.pages) - 1, capacity)

    def position(self, page: PageIndex) -> int | None:
        """Return one page's chain position without rescanning a replay-hot chain."""
        return self._positions.get(page)

    def _rebuild_tree(self) -> None:
        """Recreate the max tree after geometric growth."""
        start = self._leaf_count
        self._tree[start : start + len(self._capacities)] = self._capacities
        for node in range(self._leaf_count - 1, 0, -1):
            self._tree[node] = max(self._tree[node * 2], self._tree[node * 2 + 1])


@dataclass(slots=True)
class _EmptyIndexBuild:
    """State derived only after every bucket page was proved empty for this one batch."""

    buckets: dict[int, _FirstFitPages] = field(default_factory=dict)
    entries: dict[tuple[bytes, RecordRef], tuple[PageIndex, SlotId, IndexEntry]] = (
        field(default_factory=dict)
    )
    valid: bool = True


@dataclass(frozen=True, slots=True)
class _CommonReplayItem:
    """One fully decoded item in an ephemeral common index-only replay plan."""

    store: IndexStore
    change: IndexChange
    position: Lsn
    bucket: int


@dataclass(slots=True)
class _CommonReplayHotBucket:
    """One fully validated, call-local directory over a replay-hot bucket."""

    pages: _FirstFitPages
    entries: dict[
        tuple[bytes, RecordRef],
        tuple[PageIndex, SlotId, IndexEntry] | None,
    ]


@dataclass(frozen=True, slots=True)
class _CommonReplayStore:
    """One store's single seeded header and monotonically composed final image."""

    store: IndexStore
    initial: IndexHeader
    final: IndexHeader


@dataclass(frozen=True, slots=True)
class _CommonReplayBatch:
    """Passage-local proof for the narrow non-vector, non-rebuild replay fast path."""

    owner: IndexManager
    items: tuple[_CommonReplayItem, ...]
    stores: tuple[_CommonReplayStore, ...]
    hot_buckets: Mapping[tuple[IndexStore, int], _CommonReplayHotBucket]


_ReadResult = TypeVar("_ReadResult")


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
        "_definition_digest",
        "_file",
        "_page_type",
        "_creation_nonce",
        "_pool",
        "_metrics",
        "_staged",
        "_stale_reason",
        "_stale_device_seq",
        "_missing_targets",
        "_short_commit",
        "_cache_certificate",
        "_local_certificate",
        "_carried_certificate",
        "_rebuild_authority",
        "_completed_rebuild_through",
        "_replaying",
        "_table_high_water",
        "_tombstone_backlog_count",
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
        # IndexDefinition is frozen.  These values sit on every lookup/header-validation path
        # and deriving them again cannot observe catalog or cross-process state.
        self._definition_digest: bytes = definition.digest()
        self._file: str = definition.file
        self._page_type: int = int(
            PageType.INDEX_HNSW if definition.versioned else PageType.INDEX_HASH
        )
        self._creation_nonce: int = 0
        self._pool: BufferPool = pool
        self._metrics: MetricsSink = metrics
        self._staged: dict[int, _Staged] = {}
        self._stale_reason: str | None = None
        # The durable generation that justified ``_stale_reason``.  ``None`` while a reason is
        # present means the current handle was poisoned by process-local uncertainty (for
        # example a partial recovery) and may not heal itself merely because page 0 looks
        # healthy.  A concrete sequence may be released after a different, healthy generation
        # is observed and all clean derived frames have been rebased to it.
        self._stale_device_seq: int | None = None
        self._missing_targets: int = 0
        # The transaction whose part-applied commit is the ONLY reason this index is stale, or
        # None. It is what lets a retry of that same transaction lift the mark its own failure
        # set, and it is deliberately not a boolean: lifting a mark on the strength of "some
        # commit succeeded" would let an unrelated transaction clear a refusal it knows nothing
        # about.
        self._short_commit: int | None = None
        # None means the resident bucket frames have not been proved to belong to any current
        # durable page-0 state. The first exact read establishes it by dropping clean frames.
        self._cache_certificate: _IndexReadCertificate | None = None
        # The last certificate THIS handle published. Unlike ``_cache_certificate`` it is never
        # replaced by a foreign read rebase, so the manager can distinguish local dirty/current
        # companion heap frames from a genuinely foreign generation.
        self._local_certificate: _IndexReadCertificate | None = None
        # The certificate the last successful exact read collected from the device AFTER its
        # traversal (CQ-3/QW-2). The next lookup may start from it instead of a fresh pre-read;
        # it is consumed one-shot, dropped by every local page-0 write and by every refusal,
        # and it is never what certifies a traversal -- the fresh post-read still is.
        self._carried_certificate: _IndexReadCertificate | None = None
        # Set only after RESET has re-proved the durable stale generation while holding the
        # file's page-0 write section. It is the authority required to publish healthy again.
        self._rebuild_authority: _RebuildAuthority | None = None
        # Remembers an automatically completed local rebuild so its legacy follow-up
        # ``clear_stale`` call cannot accidentally clear a newer foreign participant's mark.
        self._completed_rebuild_through: Lsn | None = None
        self._replaying: bool = False
        # Bound by IndexManager after a table-aware open and advanced by local commits. ``None``
        # preserves the standalone IndexStore contract, whose caller supplies the whole floor.
        self._table_high_water: Lsn | None = None
        # Derived process-local metric state only. ``None`` means that no exact count is proved
        # for the current paged generation; the next gauge emission seeds it with one verified
        # walk. Thereafter successful logical changes maintain it in O(1), without adding a byte
        # to the index format or making this diagnostic state an authority for reads.
        self._tombstone_backlog_count: int | None = None
        if metrics.enabled:
            for descriptor in INDEX_METRICS:
                metrics.register(descriptor)

    # --- identity ---------------------------------------------------------------------------

    @property
    def definition(self) -> IndexDefinition:
        """Return the definition this store was built for."""
        return self._definition

    def _set_creation_nonce(self, nonce: int) -> None:
        """Set the nonce used only if this handle wins exclusive file creation."""
        if (
            isinstance(nonce, bool)
            or not isinstance(nonce, int)
            or not 0 <= nonce <= 0xFFFFFFFFFFFFFFFF
        ):
            raise GrafxIndexError(
                "An index artifact nonce must be an unsigned 64-bit integer.",
                field="artifact_nonce",
                value=repr(nonce),
            )
        self._creation_nonce = nonce

    def _ensure_artifact_nonce(self, nonce: int) -> _IndexReadCertificate:
        """Claim a legacy v1 artifact once and return its fresh persisted identity.

        The manager calls this only from a create-capable registration under
        ``COMMIT_SECTION``.  Version-1 files remain readable, but nonce zero never authorises a
        speculative owner or pre-WAL validation because recreate-to-identical would be ABA.
        """
        if (
            isinstance(nonce, bool)
            or not isinstance(nonce, int)
            or not 1 <= nonce <= 0xFFFFFFFFFFFFFFFF
        ):
            raise GrafxIndexError(
                "Claiming an index artifact needs a non-zero unsigned 64-bit nonce.",
                field="artifact_nonce",
                value=repr(nonce),
            )
        self._publish_header_transition(
            lambda header: (
                header
                if header.artifact_nonce != 0
                else replace(
                    header,
                    artifact_nonce=nonce,
                    format_version=INDEX_HEADER_FORMAT_VERSION,
                )
            )
        )
        certificate = self._fresh_certificate()
        if certificate.header.artifact_nonce == 0:
            raise GrafxIndexError(
                f"Index {self.name!r} did not persist its physical artifact nonce.",
                field="artifact_nonce",
                index=self.name,
                file=self.file,
                retryable=True,
            )
        return certificate

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
        return self._file

    @property
    def page_type(self) -> int:
        """Return the page type the pages of this index carry.

        The two codes of CONTRACT.md section 6.3 follow the visibility class, so a page says
        which contract wrote it and a verifier reading raw pages needs nothing else to tell them
        apart.
        """
        return self._page_type

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

    def is_created(self, *, proved_present: bool = False) -> bool:
        """Return True when page 0 of the file really is the header page of THIS index.

        A file that a redo grew before anything reserved page 0 exists, has pages, and is not
        created (amendment A22); saying so here is what lets :meth:`create` repair it instead of
        refusing it for good.

        ``proved_present`` carries an exact entry from the composition root's immediately
        preceding ``list_files('index/')``. It skips only the duplicate name lookup; page count,
        header identity, definition digest and every corruption refusal remain mandatory.
        """
        storage = self._pool.storage
        if (not proved_present and not storage.exists(self.file)) or storage.page_count(
            self.file
        ) == 0:
            return False
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            if page.is_pristine():
                return False
            if (
                page.page_type != int(PageType.META)
                or page.slot_count <= INDEX_HEADER_SLOT
            ):
                return False
            self._require_file_header(page)
        return True

    def create(self, *, proved_present: bool = False) -> IndexHeader:
        """Create the file of this index, or open the one that is already there.

        Creating is safe to run on every open: an index whose file already carries this
        definition is opened rather than replaced, because G6 forbids a sanctioned operation
        destroying what is under ``index/``.

        A true ``proved_present`` is the same short-lived directory proof accepted by
        :meth:`is_created`; it never turns a missing/torn structure into a created one.
        """
        header, _created = self._create_with_provenance(proved_present=proved_present)
        return header

    def _create_with_provenance(
        self, *, proved_present: bool = False
    ) -> tuple[IndexHeader, bool]:
        """Create/open this file and say whether this call won exclusive name creation.

        A preceding ``exists`` observation is not ownership: another participant can create the
        canonical name between that observation and ``create``.  The exclusive create outcome is
        the only proof a schema journal may retain.  A participant that loses the race adopts the
        resulting complete file; it never records the other participant's bytes as its own.
        """
        storage = self._pool.storage
        created = False
        if not proved_present and not storage.exists(self.file):
            try:
                storage.create(self.file)
                created = True
            except GrafxUnsupportedOperation as failure:
                if failure.details.get("reason") != "file_exists":
                    raise
        if self.is_created(proved_present=proved_present):
            return self.open(proved_present=proved_present), created
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
        return header, created

    def _reserve_header_page(self) -> None:
        """Turn page 0 into the reserved header page of this index file."""
        storage = self._pool.storage
        header = FileHeader(kind=FileKind.INDEX, page_size=self._pool.page_size)
        index_header = IndexHeader(
            visibility=self._definition.visibility,
            table_id=self._definition.table_id,
            bucket_count=self._definition.bucket_count,
            digest=self._definition_digest,
            artifact_nonce=self._creation_nonce,
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

    def open(self, *, proved_present: bool = False) -> IndexHeader:
        """Return the header of this index file, refusing a file that is not its own.

        A digest that disagrees is NOT staleness and must not be repaired by rebuilding: a file
        written under a different definition answers a different question, and the honest reply
        is to refuse to open it.
        """
        # Opening is also a sanctioned re-adoption door for a handle that may have been retained
        # while another participant replaced its durable generation. Never carry a derived
        # diagnostic count across that boundary.
        self._invalidate_tombstone_backlog()
        header = self._read_header(proved_present=proved_present)
        definition = self._definition
        if header.digest != self._definition_digest:
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
        if (
            definition.artifact_nonce != 0
            and header.artifact_nonce != definition.artifact_nonce
        ):
            raise GrafxIndexError(
                f"The file {self.file!r} carries artifact nonce "
                f"{header.artifact_nonce}, but catalog authority selected "
                f"{definition.artifact_nonce} for index {definition.name!r}.",
                field="artifact_nonce",
                index=definition.name,
                file=self.file,
                expected=definition.artifact_nonce,
                observed=header.artifact_nonce,
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
        if header.flags & INDEX_FLAG_STALE:
            # ``_read_header`` may have returned a resident page 0 from before another process
            # completed a rebuild.  Bind the refusal to one detached durable generation, never
            # to an unversioned process-local impression.  If that generation is already gone,
            # return the fresh healthy header and let the read-view fence rebase bucket frames.
            certificate = self._fresh_certificate()
            header = certificate.header
            if header.flags & INDEX_FLAG_STALE:
                if self._stale_reason is None:
                    self._stale_reason = f"Index {definition.name!r} was recorded as stale and has not been rebuilt."
                    self._stale_device_seq = certificate.seq
                elif self._stale_device_seq is not None:
                    self._stale_device_seq = certificate.seq
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

    def _read_header(self, *, proved_present: bool = False) -> IndexHeader:
        """Return the index header stored in slot 1 of the reserved header page."""
        storage = self._pool.storage
        if (not proved_present and not storage.exists(self.file)) or storage.page_count(
            self.file
        ) == 0:
            raise GrafxIndexError(
                f"Index {self.name!r} has no file yet; create it before using it.",
                field="file",
                index=self.name,
                file=self.file,
            )
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            return self._decode_header_page(page)

    def _decode_header_page(self, page: Page) -> IndexHeader:
        """Decode and identify an index header from a resident or detached page 0."""
        self._require_file_header(page)
        if page.slot_count <= INDEX_HEADER_SLOT:
            raise GrafxCorruptionDetected(
                f"The header page of {self.file!r} carries no index header.",
                file=self.file,
                page=HEADER_PAGE_INDEX,
                field="slot_count",
                value=page.slot_count,
            )
        header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
        definition = self._definition
        if header.digest != self._definition_digest:
            raise GrafxIndexError(
                f"The file {self.file!r} was written under a different definition of index "
                f"{definition.name!r}.",
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
        if (
            definition.artifact_nonce != 0
            and header.artifact_nonce != definition.artifact_nonce
        ):
            raise GrafxIndexError(
                f"The file {self.file!r} carries artifact nonce "
                f"{header.artifact_nonce}, but catalog authority selected "
                f"{definition.artifact_nonce} for index {definition.name!r}.",
                field="artifact_nonce",
                index=definition.name,
                file=self.file,
                expected=definition.artifact_nonce,
                observed=header.artifact_nonce,
            )
        return header

    def _fresh_certificate(self) -> _IndexReadCertificate:
        """Collect one detached, checksum-verified certificate directly from the device."""
        page = self._pool.read_fresh_page(self.file, HEADER_PAGE_INDEX)
        return _IndexReadCertificate(
            seq=page.seq, header=self._decode_header_page(page)
        )

    def _remember_local_certificate(self) -> _IndexReadCertificate:
        """Bind resident derived state to the page-0 image this handle just published.

        This intentionally reads the resident clean frame rather than the device again. Another
        participant may publish immediately after our flush; remembering that participant's
        certificate would falsely bless our older heap/index cache as its generation. Remembering
        our own image makes the next fresh read detect that race and take the rebase path.
        """
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            certificate = _IndexReadCertificate(
                seq=page.seq, header=self._decode_header_page(page)
            )
        self._carried_certificate = None
        self._cache_certificate = certificate
        self._local_certificate = certificate
        return certificate

    def _require_safe_certificate(
        self,
        certificate: _IndexReadCertificate,
        required_lsn: Lsn,
        *,
        rebuild_required_lsn: Lsn | None = None,
    ) -> None:
        """Refuse a durable state that cannot answer the requested snapshot completely.

        Ordinary header coverage is table-local, but a rebuild completed by this live handle
        keeps a stronger process-local fence against the original database snapshot.  A later
        index-only commit can advance the transaction clock without moving the heap high-water;
        capping that fence to the table would then certify a generation the rebuild never read.
        """
        header = certificate.header
        if header.flags & INDEX_FLAG_STALE:
            raise GrafxIndexError(
                f"Index {self.name!r} is durably unavailable while it is stale or rebuilding.",
                field="index_view_unavailable",
                index=self.name,
                file=self.file,
                seq=certificate.seq,
                retryable=True,
            )
        fenced_through = self._completed_rebuild_through
        rebuild_required = (
            required_lsn if rebuild_required_lsn is None else rebuild_required_lsn
        )
        if fenced_through is not None and rebuild_required > fenced_through:
            # A rebuild this handle completed derived its entries at ``fenced_through``. The
            # header may legitimately record a later position -- the checkpoint that finished
            # the rebuild proved the device that far -- but those later effects were not in the
            # scan, so answering a newer snapshot from these entries could omit a row. The
            # marker is process-local and a kept live commit clears it; a reopen never sees it
            # and reads the header the checkpoint proved.
            raise GrafxIndexError(
                f"Index {self.name!r} was rebuilt through position {fenced_through}, before "
                f"the snapshot at {rebuild_required}; a lookup could omit a row.",
                field="index_view_unavailable",
                index=self.name,
                file=self.file,
                built_through_lsn=fenced_through,
                required_lsn=rebuild_required,
                seq=certificate.seq,
                retryable=True,
            )
        if header.built_through_lsn < required_lsn:
            raise GrafxIndexError(
                f"Index {self.name!r} covers position {header.built_through_lsn}, before the "
                f"snapshot at {required_lsn}; a lookup could omit a row.",
                field="index_view_unavailable",
                index=self.name,
                file=self.file,
                built_through_lsn=header.built_through_lsn,
                required_lsn=required_lsn,
                seq=certificate.seq,
                retryable=True,
            )

    def _foreign_healthy_replaces_stale(
        self,
        certificate: _IndexReadCertificate,
        required_lsn: Lsn,
        *,
        rebuild_required_lsn: Lsn,
    ) -> bool:
        """Say whether a different healthy generation may release this handle's refusal.

        A process-local poison has no durable sequence and is intentionally permanent for this
        handle: page 0 cannot prove that its dirty or partially-applied frames are harmless.  A
        refusal learned from a durable generation is different.  Once another generation is
        healthy and covers the requested view, dropping every clean derived frame makes the
        handle equivalent to a cold open and it may resume without a restart.
        """
        if self._stale_reason is None:
            return False
        stale_seq = self._stale_device_seq
        if stale_seq is None:
            self._require_readable()
        if certificate.header.flags & INDEX_FLAG_STALE:
            # Follow a foreign claimant while keeping the refusal retryable.  A later healthy
            # sequence may release it; this one cannot.
            if certificate.seq == stale_seq:
                self._require_readable()
            self._stale_device_seq = certificate.seq
            self._require_safe_certificate(
                certificate,
                required_lsn,
                rebuild_required_lsn=rebuild_required_lsn,
            )
        if certificate.seq == stale_seq:
            self._require_readable()
        try:
            self._require_safe_certificate(
                certificate,
                required_lsn,
                rebuild_required_lsn=rebuild_required_lsn,
            )
        except GrafxIndexError:
            # The stale flag was cleared but coverage is not sufficient yet. Bind to that
            # intermediate generation so a later page-0 advance can release the refusal.
            self._stale_device_seq = certificate.seq
            raise
        return True

    def _cache_rebased(self) -> None:
        """Notify derived in-memory structures that their paged source was discarded."""
        self._invalidate_tombstone_backlog()

    def _required_table_position(self, requested_lsn: Lsn) -> Lsn:
        """Restrict a database snapshot to the covered table's committed history."""
        high_water = self._table_high_water
        return requested_lsn if high_water is None else min(requested_lsn, high_water)

    def begin_exact_read(self, required_lsn: Lsn) -> _IndexReadCertificate:
        """Attach cached derived state to one fresh healthy durable certificate.

        The certificate carried from the previous lookup's post-read may stand in for the fresh
        pre-read: it was collected from the device, it proved that whole view, and the resident
        frames are still bound to it. It is consumed one-shot and reused ONLY while it still
        names the cached generation, this handle holds no refusal, and it covers the requested
        snapshot. Anything else drops it and takes the fresh path below -- the carry never turns
        into a refusal of its own. What certifies the traversal is unchanged: the fresh post-read
        of :meth:`finish_exact_read`. A foreign page-0 transition between two lookups is seen
        there, costs one of the bounded retries, and the retry re-proves from the device.
        """
        rebuild_required_lsn = required_lsn
        required_lsn = self._required_table_position(required_lsn)
        carried = self._carried_certificate
        if carried is not None:
            self._carried_certificate = None
            if self._stale_reason is None and carried == self._cache_certificate:
                try:
                    self._require_safe_certificate(
                        carried,
                        required_lsn,
                        rebuild_required_lsn=rebuild_required_lsn,
                    )
                except GrafxIndexError:
                    # Fallback, never a verdict: the device decides below.
                    pass
                else:
                    return carried
        certificate = self._fresh_certificate()
        recovering = self._foreign_healthy_replaces_stale(
            certificate,
            required_lsn,
            rebuild_required_lsn=rebuild_required_lsn,
        )
        self._require_safe_certificate(
            certificate,
            required_lsn,
            rebuild_required_lsn=rebuild_required_lsn,
        )
        if certificate != self._cache_certificate or recovering:
            # The mismatch includes first use. Pre/post equality alone cannot detect a rebuild
            # that finished before this lookup while old bucket frames remained resident.
            self._pool.discard_clean_file(self.file)
            certificate = self._fresh_certificate()
            recovering = self._foreign_healthy_replaces_stale(
                certificate,
                required_lsn,
                rebuild_required_lsn=rebuild_required_lsn,
            )
            self._require_safe_certificate(
                certificate,
                required_lsn,
                rebuild_required_lsn=rebuild_required_lsn,
            )
            if recovering:
                self._stale_reason = None
                self._stale_device_seq = None
                self._rebuild_authority = None
            self._cache_certificate = certificate
            self._cache_rebased()
        return certificate

    def finish_exact_read(
        self, before: _IndexReadCertificate, required_lsn: Lsn
    ) -> bool:
        """Say whether traversal+heap validation stayed inside one durable index view."""
        rebuild_required_lsn = required_lsn
        required_lsn = self._required_table_position(required_lsn)
        after = self._fresh_certificate()
        if after == before:
            self._require_safe_certificate(
                after,
                required_lsn,
                rebuild_required_lsn=rebuild_required_lsn,
            )
            # The device-observed certificate that proved this view is the next lookup's
            # pre-certificate; it stays bound to the resident frames it just certified.
            self._carried_certificate = after
            return True
        # Do not carry any frame from the losing attempt into its retry. The discard is
        # zero-write and refuses dirty/pinned uncertainty rather than writing stale state back.
        self._carried_certificate = None
        self._cache_certificate = None
        self._pool.discard_clean_file(self.file)
        self._cache_rebased()
        return False

    def _write_header(self, header: IndexHeader) -> None:
        """Replace the index header stored in slot 1 of the reserved header page."""
        # Every local page-0 write goes through here, including the ones that leave
        # ``_cache_certificate`` alone (position advance, reconciliation watermark); any of
        # them makes a carried certificate a stale impression of the device.
        self._carried_certificate = None
        with self._pool.pinned(self.file, HEADER_PAGE_INDEX) as page:
            self._require_file_header(page)
            page.update_slot(INDEX_HEADER_SLOT, header.encode())

    def _publish_header_transition(
        self,
        transition: Callable[[IndexHeader], IndexHeader],
        *,
        force_write: bool = False,
    ) -> IndexHeader:
        """Fresh-read/CAS one dangerous page-0 transition with bounded conflict retry."""
        for attempt in range(INDEX_READ_RETRY_BUDGET + 1):
            self._pool.discard_clean_page(self.file, HEADER_PAGE_INDEX)
            current = self._read_header()
            updated = transition(current)
            if (updated is current or updated == current) and not force_write:
                return current
            self._write_header(updated)
            try:
                self._pool.write_back(self.file, HEADER_PAGE_INDEX)
            except BaseException as failure:
                # The attempted image is never a safe cache base after an unconfirmed publish.
                # This includes the device-completed-then-raised case: a retry must re-read the
                # device to discover which side won, rather than carrying dirty bytes forward.
                try:
                    self._pool.discard(self.file, HEADER_PAGE_INDEX)
                except (
                    BaseException
                ) as cleanup_failure:  # pragma: no cover - defensive note
                    failure.add_note(
                        "Discarding the unconfirmed page-0 attempt also failed: "
                        f"{cleanup_failure!r}"
                    )
                if (
                    not isinstance(failure, GrafxUnsupportedOperation)
                    or failure.details.get("field") != "page_sequence_conflict"
                    or attempt >= INDEX_READ_RETRY_BUDGET
                ):
                    raise
                continue
            self._cache_certificate = None
            return updated
        raise AssertionError("bounded header transition loop did not return or raise")

    def _touch_header(self) -> None:
        """Make the current header the final dirty certificate for a bucket publication."""
        current = self._read_header()
        self._write_header(current)
        self._cache_certificate = None

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

    def mark_stale(self, reason: str, *, persist: bool = True) -> None:
        """Exclude this index from reads, optionally recording the verdict on the device."""
        if not isinstance(reason, str) or not reason:
            raise GrafxIndexError(
                "Marking an index stale needs a reason a reader can act on.",
                field="reason",
                value=repr(reason),
                index=self.name,
            )
        self._completed_rebuild_through = None
        self._stale_reason = reason
        self._carried_certificate = None
        if not persist:
            # A failed recovery may have changed an unknown prefix and must poison the current
            # handle without turning an otherwise non-mutating refusal into another device
            # write. The retained WAL lets the next open retry and derive a durable verdict.
            self._stale_device_seq = None
            return
        self._publish_header_transition(
            lambda header: _with_flags(header, header.flags | INDEX_FLAG_STALE)
        )
        self._stale_device_seq = self._fresh_certificate().seq
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
        # process, and this one after a restart, READS the refusal. The transition helper
        # publishes page 0 before releasing its cross-process section.

    def _claim_rebuild(self, reason: str) -> int:
        """Publish a unique stale generation and return its page-0 sequence token."""
        if not isinstance(reason, str) or not reason:
            raise GrafxIndexError(
                "Claiming an index rebuild needs a reason a reader can act on.",
                field="reason",
                value=repr(reason),
                index=self.name,
            )
        self._completed_rebuild_through = None
        self._stale_reason = reason
        self._publish_header_transition(
            lambda header: _with_flags(header, header.flags | INDEX_FLAG_STALE),
            force_write=True,
        )
        certificate = self._fresh_certificate()
        if not certificate.header.flags & INDEX_FLAG_STALE:
            raise GrafxIndexError(
                f"Index {self.name!r} did not retain the stale mark that claimed its rebuild.",
                field="rebuild_authority",
                index=self.name,
                file=self.file,
                seq=certificate.seq,
                retryable=True,
            )
        self._stale_device_seq = certificate.seq
        return certificate.seq

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
        self._invalidate_tombstone_backlog()
        try:
            self.mark_stale(reason)
        except GrafxError:
            self._completed_rebuild_through = None
            self._stale_reason = reason
            self._stale_device_seq = None

    def _lift_short_commit_mark(self, built_through: Lsn) -> None:
        """Take back the mark a part-applied commit set, once its retry has completed.

        It runs only after the transaction that set the mark has finished every change AND its
        bucket pages have reached the device. The clear and build advance then publish together
        as page 0's final certificate; clearing earlier would expose a healthy old header while
        the repaired buckets were still process-local.
        """
        self._short_commit = None
        self._publish_header_transition(
            lambda header: _with_flags(
                header, header.flags & ~INDEX_FLAG_STALE
            ).advanced_to(built_through)
        )
        self._stale_reason = None
        self._stale_device_seq = None

    def check_freshness(
        self,
        published_lsn: Lsn,
        *,
        required_lsn: Lsn | None = None,
        persist: bool = True,
        allow_ahead: bool = False,
    ) -> bool:
        """Check this index between its table's required floor and the published ceiling.

        This is the only detector of staleness in the component, and the flag it sets is the only
        gate the read path consults. Keeping detection and memory apart is what makes each of
        them testable on its own (amendment A67): a test can set the flag without a published
        position, and can move the published position without a flag already set.

        Direct index callers that omit ``required_lsn`` retain the original global comparison.
        The manager supplies the covered table's physical high-water mark so unrelated commits
        neither stale this index nor force a page-0 rewrite.
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
        required = published_lsn if required_lsn is None else required_lsn
        if isinstance(required, bool) or not isinstance(required, int):
            raise GrafxIndexError(
                f"A required index position must be an integer; got "
                f"{type(required).__name__}.",
                field="required_lsn",
                value=repr(required),
                index=self.name,
            )
        if required < NO_LSN:
            raise GrafxIndexError(
                f"A required index position must not be negative; got {required}.",
                field="required_lsn",
                value=required,
                index=self.name,
            )
        if required > published_lsn and not allow_ahead:
            raise GrafxIndexError(
                f"Index {self.name!r} was given required position {required}, ahead of the "
                f"database's published position {published_lsn}.",
                field="required_lsn",
                value=required,
                published_lsn=published_lsn,
                index=self.name,
            )
        certificate = self._fresh_certificate()
        header = certificate.header
        if certificate != self._cache_certificate:
            # The device moved since the carried certificate was collected; let the next
            # lookup take the fresh path instead of spending a retry to learn the same thing.
            self._carried_certificate = None
        if header.flags & INDEX_FLAG_STALE:
            if self._stale_reason is None:
                self._stale_reason = f"Index {self.name!r} was recorded as stale and has not been rebuilt."
                self._stale_device_seq = certificate.seq
            elif self._stale_device_seq is not None:
                self._stale_device_seq = certificate.seq
            return True
        if header.built_through_lsn < required:
            reason = (
                f"Index {self.name!r} covers the log through position "
                f"{header.built_through_lsn}, but its table requires coverage through "
                f"{required} under database published position {published_lsn}, so a lookup "
                "could omit a row."
            )
            if persist:
                self.mark_stale(reason)
            else:
                # A read-only open still refuses the unsafe access path, but records that verdict
                # only in this participant. Persisting it would make inspection modify the DB.
                self._stale_reason = reason
                self._stale_device_seq = certificate.seq
            return True
        if header.built_through_lsn > published_lsn and not allow_ahead:
            reason = (
                f"Index {self.name!r} claims to cover log position "
                f"{header.built_through_lsn}, ahead of the database's published position "
                f"{published_lsn}; the file may belong to another database state."
            )
            if persist:
                self.mark_stale(reason)
            else:
                self._stale_reason = reason
                self._stale_device_seq = certificate.seq
            return True
        if self._stale_reason is None:
            return False
        stale_seq = self._stale_device_seq
        if stale_seq is None or certificate.seq == stale_seq:
            return True
        # A different durable generation is now healthy and agrees with this published view.
        # Rebase before lifting the in-memory refusal; a sticky bucket frame from the stale
        # generation would otherwise make a recovered handle less safe than a cold one.
        self._pool.discard_clean_file(self.file)
        after = self._fresh_certificate()
        if after != certificate:
            self._stale_device_seq = after.seq
            return True
        self._carried_certificate = None
        self._cache_certificate = after
        self._stale_reason = None
        self._stale_device_seq = None
        self._rebuild_authority = None
        self._cache_rebased()
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
                csn=self._require_csn("csn", csn)
                if self._definition.versioned
                else NO_CSN,
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

    def stage_reset(
        self,
        txn: StagingTransaction,
        built_through: Lsn,
        *,
        rebuild_token: int = 0,
        defer_clear: bool = False,
    ) -> WalRecord:
        """Stage a bucket reset bound to the durable stale generation that authorised it.

        ``defer_clear`` asks the commit to publish the rebuilt buckets and STOP there,
        leaving the stale refusal and its generation exactly as the claim left them. It
        travels inside the record rather than beside the call because replay has to reach
        the same conclusion as the live path; a deferral only the caller knew about would
        make a crash between the commit and the clear recover into a different state.
        """
        certificate = self._require_durable_stale_for_reset()
        if rebuild_token and certificate.seq != rebuild_token:
            raise GrafxIndexError(
                f"Index {self.name!r} changed after rebuild generation {rebuild_token} was "
                f"claimed; the durable generation is now {certificate.seq}.",
                field="rebuild_superseded",
                index=self.name,
                file=self.file,
                rebuild_token=rebuild_token,
                device_seq=certificate.seq,
                retryable=True,
            )
        token = certificate.seq if rebuild_token == 0 else rebuild_token
        if defer_clear:
            txn_id = self._require_txn(txn)
            staged = self._staged.get(txn_id)
            if staged is None:
                staged = self._new_staged(txn_id)
                self._staged[txn_id] = staged
            staged.defer_clear = True
        return self._stage(
            txn,
            IndexChange(
                index=self.name,
                operation=IndexOperation.RESET,
                ref=RecordRef(token, 0),
                csn=self._require_csn("built_through", built_through),
                versioned=self._definition.versioned,
            ),
        )

    def _require_durable_stale_for_reset(self) -> _IndexReadCertificate:
        """Prove from the device that RESET cannot make a healthy index temporarily short."""
        certificate = self._fresh_certificate()
        if not certificate.header.flags & INDEX_FLAG_STALE:
            raise GrafxIndexError(
                f"Index {self.name!r} is durably healthy; RESET is permitted only after a "
                "stale mark has reached page 0.",
                field="reset_requires_stale",
                index=self.name,
                file=self.file,
                seq=certificate.seq,
                retryable=True,
            )
        return certificate

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
            staged = self._new_staged(txn_id)
            self._staged[txn_id] = staged
        staged.changes.append(change)
        txn.stage_record(record)
        return record

    def stage_empty_observation(self, txn: StagingTransaction) -> bool:
        """Record that a covered row was examined but owed this sparse index no entry.

        The log records changes, so an omitted sparse entry has no logical index record.  The
        live index still has to advance through the table's commit: otherwise its durable
        ``built_through`` position remains behind and the next reader correctly, but needlessly,
        marks it stale.  An empty staging batch supplies exactly that acknowledgement and is
        discarded by rollback like an ordinary batch.
        """

        txn_id = self._require_txn(txn)
        if txn_id not in self._staged:
            self._staged[txn_id] = self._new_staged(txn_id)
            return True
        return False

    def _new_staged(self, txn_id: int) -> _Staged:
        """Bind new private staging to this file's non-repeating physical identity."""
        certificate = self._fresh_certificate()
        return _Staged(
            txn_id=txn_id,
            artifact_nonce=certificate.header.artifact_nonce,
        )

    def discard_empty_observation(self, txn: StagingTransaction) -> bool:
        """Undo an empty observation created by one refused schema statement only."""
        txn_id = self._require_txn(txn)
        staged = self._staged.get(txn_id)
        if staged is None or staged.changes:
            return False
        return self._staged.pop(txn_id, None) is staged

    def validate_staged_artifact(self, txn: StagingTransaction) -> None:
        """Prove this transaction still stages against the same physical index artifact."""
        txn_id = self._require_txn(txn)
        staged = self._staged.get(txn_id)
        if staged is None:
            return
        certificate = self._fresh_certificate()
        if certificate.header.artifact_nonce != staged.artifact_nonce:
            raise GrafxIndexError(
                f"Index {self.name!r} was replaced after transaction {txn_id} staged it.",
                field="artifact_nonce",
                index=self.name,
                file=self.file,
                expected=staged.artifact_nonce,
                observed=certificate.header.artifact_nonce,
                retryable=True,
            )

    def pending(self, txn: StagingTransaction) -> tuple[IndexChange, ...]:
        """Return the changes this transaction has staged into this index, in order."""
        staged = self._staged.get(self._require_txn(txn))
        return () if staged is None else tuple(staged.changes)

    def observed(self, txn: StagingTransaction) -> bool:
        """Return whether this index observed its covered table in the transaction."""
        return self._require_txn(txn) in self._staged

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
        resets = [
            change
            for change in staged.changes
            if change.operation is IndexOperation.RESET
        ]
        if len(resets) > 1:
            raise GrafxIndexError(
                f"Transaction {txn_id} staged {len(resets)} resets for index {self.name!r}; "
                "one rebuild has exactly one generation authority.",
                field="reset_count",
                index=self.name,
                txn_id=txn_id,
                value=len(resets),
            )
        reset = resets[0] if resets else None
        authority = _LIVE_COMMIT_AUTHORITY.get()
        live_hot = bool(
            isinstance(authority, _LiveCommitAuthority)
            and authority.active
            and authority.seal is _LIVE_COMMIT_AUTHORITY_SEAL
            and authority.txn is txn
            and authority.store is self
        )
        if reset is None:
            applied = self._commit_staged(
                txn_id, staged, stamp, reset=None, live_hot=live_hot
            )
            self._remember_local_certificate()
            return applied
        # The same cross-process section that serialises page-0 CAS covers the dangerous
        # interval from RESET's generation proof through bucket publication and the final
        # healthy certificate. Readers remain optimistic and never acquire it.
        with self._pool.page_write_fence(self.file, HEADER_PAGE_INDEX):
            applied = self._commit_staged(
                txn_id, staged, stamp, reset=reset, live_hot=live_hot
            )
            self._remember_local_certificate()
            return applied

    def _commit_staged(
        self,
        txn_id: int,
        staged: _Staged,
        stamp: Csn,
        *,
        reset: IndexChange | None,
        live_hot: bool,
    ) -> int:
        """Apply one staged batch; a RESET caller already holds the whole-file fence."""
        applied = 0
        moved_any = False
        empty_build: _EmptyIndexBuild | None = None
        try:
            hot_buckets = (
                self._prepare_live_hot_buckets(staged)
                if live_hot and reset is None
                else {}
            )
        except GrafxError as failure:
            self._note_commit_failure(txn_id, staged, applied, failure)
            raise
        for change in staged.changes:
            try:
                moved: bool
                hot_bucket = hot_buckets.get(
                    bucket_of(change.key, self._definition.bucket_count)
                )
                if hot_bucket is not None:
                    moved = self._apply_common_replay_hot_change(
                        hot_bucket, change, stamp
                    )
                else:
                    accelerated = (
                        None
                        if empty_build is None
                        else self._apply_empty_build_change(
                            empty_build, change, stamp
                        )
                    )
                    if accelerated is None:
                        moved = self._apply_change(change, stamp)
                        if empty_build is not None:
                            empty_build = None
                    else:
                        moved = accelerated
                moved_any = moved or moved_any
                if change.operation is IndexOperation.RESET and moved and applied == 0:
                    # RESET just validated and cleared every reachable bucket page while this
                    # rebuild holds the whole-file fence.  The remaining changes may therefore
                    # use an ephemeral directory without trusting state from another generation.
                    empty_build = _EmptyIndexBuild()
            except GrafxError as failure:
                self._note_commit_failure(txn_id, staged, applied, failure)
                raise
            applied += 1
        self._staged.pop(txn_id, None)
        if reset is not None:
            self._complete_rebuild(reset.csn, defer=staged.defer_clear)
        elif self._short_commit == txn_id:
            # Keep the durable refusal in place while the repaired buckets are published, then
            # clear+advance page 0 as the final certificate.
            self._pool.flush(self.file)
            self._lift_short_commit_mark(stamp)
        else:
            # A commit that lands while a rebuild is deferred must not move page 0. This handle
            # can believe the index is healthy and still be wrong: the claim may have been made
            # by another handle, or after this one opened. Asking the device settles it, and a
            # durable STALE mark means the entries go to their buckets while the header keeps
            # the generation the retained RESET is bound to -- otherwise the clock moves, the
            # token no longer matches, and the final checkpoint refuses a reset it must replay.
            durably_stale = self._stale_reason is not None
            if not durably_stale:
                certificate = self._fresh_certificate()
                if certificate.header.flags & INDEX_FLAG_STALE:
                    durably_stale = True
                    self._stale_reason = (
                        f"Index {self.name!r} is durably stale under a rebuild this handle "
                        "did not claim."
                    )
                    self._stale_device_seq = certificate.seq
                elif certificate != self._cache_certificate:
                    # Page 0 moved on the device outside any commit this handle applied: a
                    # cold open's replay advances a proximity header without a log record
                    # (ST-7), so no foreign-record sync ever brought it here, and nothing
                    # before this point re-reads page 0 into the pool. A clean resident page-0
                    # frame is still bound to the older generation; advancing the header on it
                    # would meet the page-0 sequence fence in the flush below -- AFTER the
                    # barrier -- and leave this handle in recovery_required over a conflict
                    # that was never about this commit. Drop that one clean frame so the
                    # advance reads the device generation -- through the door header
                    # transitions already use for a foreign refresh, which dooms a PINNED
                    # clean frame rather than leaving it resident for the advance to reuse.
                    # Only page 0, and only when clean: this commit's dirty buckets stay, and
                    # a dirty page 0 keeps meeting the fence, which is the refusal this must
                    # not relax.
                    self._invalidate_tombstone_backlog()
                    dirty = {
                        page_index
                        for file, page_index in self._pool.modified_pages(self.file)
                        if file == self.file
                    }
                    if HEADER_PAGE_INDEX not in dirty:
                        self._pool.discard_clean_page(self.file, HEADER_PAGE_INDEX)
            if not durably_stale:
                self._advance(stamp)
                if moved_any:
                    # Even when the payload position was already at ``stamp``, changed buckets
                    # need a new durable clock. Touching page 0 after them puts it at the LRU
                    # tail so the existing flush boundary publishes the certificate last.
                    self._touch_header()
            # The exact read fence certifies the durable header, not this process's dirty frame.
            # Publish bucket changes before page 0 (the _advance read moved it to the LRU tail)
            # so a fresh certificate never advertises a position whose entries are process-local.
            self._pool.flush(self.file)
            if not durably_stale:
                # Only now: a commit that reached the device is later work this index really
                # took, so the rebuild fence no longer describes what it can answer. Releasing
                # it before the flush would let any failure along the way lift the fence too.
                self._completed_rebuild_through = None
        self._publish_tombstone_backlog()
        return applied

    def _note_commit_failure(
        self,
        txn_id: int,
        staged: _Staged,
        applied: int,
        failure: GrafxError,
    ) -> None:
        """Preserve the live commit's durable stale/retry protocol after one refusal."""
        if applied == 0 and failure.details.get("field") in {
            "rebuild_superseded",
            "reset_requires_stale",
        }:
            # A generation proof refused before the first bucket moved. The durable header
            # already belongs to the winning rebuild; poisoning it would turn a clean
            # arbitration result into needless global unavailability.
            return
        if self._stale_reason is None:
            # Only a commit that found the index HEALTHY may claim to be the reason it is stale,
            # because only then is a completed retry proof that nothing else is wrong.
            self._short_commit = txn_id
        self._mark_stale_after_failure(
            f"Applying a commit to index {self.name!r} failed after {applied} of "
            f"{len(staged.changes)} changes, so it is missing entries the heap holds: "
            f"{failure.message}"
        )

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
        self._remember_local_certificate()

    def complete_built_through(self, lsn: Lsn, table_high_water: Lsn | None) -> None:
        """Advance for a completed replay, unless the header already covers its table.

        Eligibility to skip is proved by THIS call's own fresh device certificate and the
        caller's current watermark photo, never by a remembered flag: a foreign participant may
        persist STALE outside any commit section at any moment, and page 0 is the only place
        that verdict lives (ST-7). Four proofs make a skip: the fresh certificate is healthy,
        no page of this index holds unpublished local work, its built-through position agrees
        with the resident header, and it covers the table's committed high water -- which is the
        strongest position
        :meth:`IndexManager.open` will ever require of it. The skip never writes; the worst a
        wrong photo can cost is a rebuild nobody needed, never a wrong answer. Every other
        state -- no photo for this table, dirty work, a stale mark in either home, a rebuild in
        flight, or a disagreement between device and resident -- takes
        :meth:`advance_built_through` whole.

        Proximity-class indexes never skip. Their freshness is consumed OUTSIDE this repo
        against the GLOBAL replay declaration -- the Pulse ANN contract reads
        ``built_through_lsn`` versus the declared position, not versus the table's high water
        (regression msg_179ada60) -- so for them the completion mark keeps its pre-ST-7
        semantics whole. The open-time saving this door exists for is almost entirely
        exact-class: they keep the table-local skip.
        """
        position = _require_position("lsn", lsn)
        if self.visibility is IndexVisibility.PROXIMITY:
            self.advance_built_through(position)
            return
        if (
            table_high_water is None
            or self._stale_reason is not None
            or self._rebuild_authority is not None
            or self._completed_rebuild_through is not None
            or self._pool.has_dirty_pages(self.file)
        ):
            self.advance_built_through(position)
            return
        certificate = self._fresh_certificate()
        resident = self._read_header()
        if (
            not certificate.header.flags & INDEX_FLAG_STALE
            and certificate.header.built_through_lsn == resident.built_through_lsn
            and certificate.header.built_through_lsn >= table_high_water
        ):
            return
        self.advance_built_through(position)

    def clear_stale(
        self,
        built_through: Lsn,
        *,
        advance_to: Lsn | None = None,
        rebuild_token: int = 0,
    ) -> None:
        """Declare this index rebuilt through a position, so it may answer lookups again.

        The position is not optional and it is not derived. A stale index refuses to move the
        position it claims -- that is what keeps it from quietly looking fresh again -- so
        clearing the flag without saying what the rebuild covered would leave the file claiming
        the position it had while it was broken, and the very next freshness check would call it
        stale again. The repair would not stick, and nothing would say why.
        """
        position = _require_position("built_through", built_through)
        with self._pool.page_write_fence(self.file, HEADER_PAGE_INDEX):
            certificate = self._fresh_certificate()
            if not certificate.header.flags & INDEX_FLAG_STALE:
                if rebuild_token:
                    # A tokenised caller is finishing ITS rebuild, and a healthy header means
                    # the refusal it was going to lift has already been consumed by someone
                    # else's completion. Returning here would report that completion as this
                    # caller's success. The untokenised operator/recovery door keeps the
                    # idempotent fast path below, because it is asserting rather than claiming.
                    raise GrafxIndexError(
                        f"Index {self.name!r} is already healthy, so rebuild generation "
                        f"{rebuild_token} was completed by something else.",
                        field="rebuild_superseded",
                        index=self.name,
                        file=self.file,
                        rebuild_token=rebuild_token,
                        device_seq=certificate.seq,
                        retryable=True,
                    )
                if certificate.header.built_through_lsn < position:
                    raise GrafxIndexError(
                        f"Index {self.name!r} is healthy only through "
                        f"{certificate.header.built_through_lsn}; clearing it through {position} "
                        "without a stale rebuild would invent coverage.",
                        field="rebuild_authority",
                        index=self.name,
                        file=self.file,
                        built_through_lsn=certificate.header.built_through_lsn,
                        required_lsn=position,
                        retryable=True,
                    )
                if self._rebuild_authority is not None:
                    self._completed_rebuild_through = position
                self._rebuild_authority = None
                self._stale_reason = None
                self._stale_device_seq = None
                self._carried_certificate = None
                self._cache_certificate = certificate
                return
            if (
                self._rebuild_authority is None
                and self._completed_rebuild_through is not None
            ):
                raise GrafxIndexError(
                    f"Index {self.name!r} acquired a new stale generation after this handle "
                    "completed its rebuild; a late clear cannot consume another participant's "
                    "mark.",
                    field="rebuild_superseded",
                    index=self.name,
                    file=self.file,
                    completed_through=self._completed_rebuild_through,
                    device_seq=certificate.seq,
                    retryable=True,
                )
            if self._rebuild_authority is not None:
                if rebuild_token and self._rebuild_authority.token != rebuild_token:
                    # The generation being finished is not the one the caller claimed. Names
                    # are not identity: another rebuild of the SAME index can hold a perfectly
                    # valid authority, and clearing it here would certify somebody else's work
                    # as this call's result.
                    raise GrafxIndexError(
                        f"Index {self.name!r} holds rebuild generation "
                        f"{self._rebuild_authority.token}, not the {rebuild_token} this "
                        "caller claimed.",
                        field="rebuild_superseded",
                        index=self.name,
                        file=self.file,
                        rebuild_token=rebuild_token,
                        device_seq=self._rebuild_authority.token,
                        retryable=True,
                    )
                self._complete_rebuild(position, advance_to=advance_to)
                return
            # Backward-compatible operator/recovery door. With no RESET authority this is an
            # explicit assertion that repair happened outside this store. Holding the page-0
            # section and flushing first still prevents it from interleaving with a rebuild.
            self._pool.flush(self.file)
            before_clear = self._fresh_certificate()
            if before_clear != certificate:
                raise GrafxIndexError(
                    f"Index {self.name!r} changed while its manual stale clear was prepared.",
                    field="rebuild_superseded",
                    index=self.name,
                    file=self.file,
                    expected_seq=certificate.seq,
                    device_seq=before_clear.seq,
                    retryable=True,
                )
            self._publish_header_transition(
                lambda header: _with_flags(
                    header, header.flags & ~INDEX_FLAG_STALE
                ).advanced_to(position)
            )
            self._stale_reason = None
            self._stale_device_seq = None
            self._remember_local_certificate()
        # Clearing the mark is unlogged for the same reason setting it is, and it errs the other
        # way: a repair that never reached the device leaves the file still saying stale, so the
        # next open rebuilds an index that was already rebuilt. Wasted work, never a wrong
        # answer. It is flushed because the pair must be symmetrical -- a durable mark that only
        # a cached clear can undo would mean an index this process believes repaired and every
        # other process still refuses, and two participants disagreeing about whether an index
        # may answer is worse than either verdict.
        # The transition helper makes the clear durable before this process lifts its refusal.

    def _complete_rebuild(
        self,
        built_through: Lsn,
        *,
        defer: bool = False,
        advance_to: Lsn | None = None,
    ) -> None:
        """Publish rebuilt buckets, then consume exactly their stale-generation authority.

        With ``defer`` the second half does not happen here. The buckets reach the device
        and the authority is kept, so the index stays durably stale on exactly the
        generation the claim published: page 0 is not written, the retained RESET stays
        replayable under its own token, and a crash before the clear recovers to a stale
        index rather than to one that answers from a rebuild nobody proved.

        The clear is then somebody else's last act. Anything that fails between the two
        halves needs no repair at all, because the refusal was never lifted -- which is
        the whole reason the halves are split.
        """
        position = _require_position("built_through", built_through)
        authority = self._rebuild_authority
        if authority is None or authority.through_lsn != position:
            raise GrafxIndexError(
                f"Index {self.name!r} has no matching RESET authority through {position}.",
                field="rebuild_authority",
                index=self.name,
                file=self.file,
                required_lsn=position,
                authority_lsn=None if authority is None else authority.through_lsn,
                retryable=True,
            )
        before_flush = self._fresh_certificate()
        if (
            not before_flush.header.flags & INDEX_FLAG_STALE
            or before_flush.seq != authority.header_seq
        ):
            for file, page_index in self._pool.modified_pages(self.file):
                if file == self.file:
                    self._pool.discard(file, page_index)
            self._rebuild_authority = None
            raise GrafxIndexError(
                f"Index {self.name!r} rebuild generation {authority.header_seq} was superseded "
                f"by durable generation {before_flush.seq} before bucket publication.",
                field="rebuild_superseded",
                index=self.name,
                file=self.file,
                rebuild_token=authority.token,
                expected_seq=authority.header_seq,
                device_seq=before_flush.seq,
                retryable=True,
            )
        # Bucket pages reach the device while the stale refusal remains durable. Page 0 is clean
        # here, so it cannot be emitted before them by this flush.
        self._pool.flush(self.file)
        after_flush = self._fresh_certificate()
        if after_flush != before_flush:
            self._rebuild_authority = None
            raise GrafxIndexError(
                f"Index {self.name!r} changed while rebuilt buckets were being published.",
                field="rebuild_superseded",
                index=self.name,
                file=self.file,
                expected_seq=before_flush.seq,
                device_seq=after_flush.seq,
                retryable=True,
            )
        if defer:
            # Buckets are durable and the refusal still stands. The authority is retained
            # on purpose: the clear that consumes it revalidates this same generation, so
            # a claim arriving in between makes that clear refuse instead of certify.
            return
        # One page-0 write, at the position the index may honestly claim. A caller finishing a
        # deferred rebuild has just replayed the log onto this index, so that is the replayed
        # position rather than the older one its scan read at -- and writing it inside this same
        # transition keeps the clear a single act instead of a clear plus a second write that
        # would leave page 0 dirty behind it.
        covered = (
            position
            if advance_to is None
            else _require_position("advance_to", advance_to)
        )
        try:
            self._publish_header_transition(
                lambda header: _with_flags(
                    header, header.flags & ~INDEX_FLAG_STALE
                ).advanced_to(max(position, covered))
            )
        except BaseException as failure:
            # A device is allowed to complete a write and then report interruption. Preserve
            # that original evidence, but do not retain an authority that durable page 0 has
            # already consumed: it would suppress the header clock on later same-LSN repairs.
            try:
                completed = self._fresh_certificate()
            except (
                BaseException
            ) as observation_failure:  # pragma: no cover - diagnostic only
                failure.add_note(
                    "Observing page 0 after the final rebuild publication also failed: "
                    f"{observation_failure!r}"
                )
            else:
                if (
                    not completed.header.flags & INDEX_FLAG_STALE
                    and completed.header.built_through_lsn >= max(position, covered)
                ):
                    self._rebuild_authority = None
                    self._completed_rebuild_through = position
                    self._stale_reason = None
                    self._stale_device_seq = None
                    self._carried_certificate = None
                    self._cache_certificate = completed
                    self._local_certificate = completed
                    # The device completed the write and then reported interruption. A fresh
                    # certificate says the healthy header landed with the coverage this clear
                    # meant to publish, so this IS the success it looks like, and re-raising
                    # would tell a caller the rebuild failed while leaving an index that
                    # answers from it -- fail-open by way of a false refusal.
                    #
                    # Only for an ordinary failure, though. KeyboardInterrupt and SystemExit
                    # are not the device disagreeing with itself, they are the process being
                    # told to stop, and a store that swallowed them because a page happened to
                    # land would be deciding something that was never its call.
                    if isinstance(failure, Exception):
                        return
            raise
        self._rebuild_authority = None
        self._completed_rebuild_through = position
        self._stale_reason = None
        self._stale_device_seq = None
        try:
            self._remember_local_certificate()
        except Exception:  # noqa: BLE001 - the header is already healthy on the device
            # Refreshing the cached certificate is the last thing here and it is not a
            # mutation: the clear already landed. Failing the call for it would report a
            # refusal for an index that is durably healthy. The cache is simply left for the
            # next read to rebuild. Process-control signals are not caught.
            self._carried_certificate = None
            self._cache_certificate = None

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
        fenced = (
            change.operation is IndexOperation.RESET
            or self._rebuild_authority is not None
        )
        try:
            if fenced:
                # A replay API offers one logical record at a time, so it cannot retain a lock
                # across calls.  Instead every rebuild record is a small complete-or-refuse
                # publication: validate the generation, mutate, and flush while holding the same
                # cross-process section. No stale dirty frame may survive the section boundary.
                with self._pool.page_write_fence(self.file, HEADER_PAGE_INDEX):
                    if change.operation is not IndexOperation.RESET:
                        self._require_active_replay_generation()
                    self._apply_replay_change(change, position)
                    self._publish_active_replay_prefix()
            else:
                self._apply_replay_change(change, position)
        except GrafxError as failure:
            if fenced:
                self._discard_replay_frames(failure)
            if failure.details.get("field") not in {
                "rebuild_superseded",
                "reset_requires_stale",
            }:
                self._mark_stale_after_failure(
                    f"Replaying log position {position} into index {self.name!r} failed, so the "
                    f"current handle cannot prove the index complete: {failure.message}"
                )
            raise
        except BaseException as failure:
            if fenced:
                self._discard_replay_frames(failure)
            raise

    def _apply_replay_change(self, change: IndexChange, position: Lsn) -> None:
        """Apply one already-validated logical record without choosing its outer fence."""
        self._replaying = True
        try:
            moved = self._apply_change(change, position)
        finally:
            self._replaying = False
        self._advance(position)
        if change.operation is IndexOperation.REMOVE:
            # A reconciliation record carries both the erasure and the horizon that made it
            # legal. Restoring only the erasure makes verification report a cleanly reclaimed
            # entry as missing after a crash.
            self._record_reconciled(change.csn)
        if moved and self._rebuild_authority is None:
            # Logical redo may repair a missing bucket at an LSN the header already covers. The
            # payload then stays byte-identical, but the durable view did not: make page 0 the
            # final dirty clock at the caller's existing recovery flush boundary.
            self._touch_header()

    def _require_active_replay_generation(self) -> None:
        """Refuse a replay prefix whose RESET generation another participant superseded."""
        authority = self._rebuild_authority
        if authority is None:
            return
        certificate = self._fresh_certificate()
        if (
            certificate.header.flags & INDEX_FLAG_STALE
            and certificate.seq == authority.header_seq
        ):
            return
        failure = GrafxIndexError(
            f"Index {self.name!r} rebuild generation {authority.header_seq} was superseded by "
            f"durable generation {certificate.seq} before replay completed.",
            field="rebuild_superseded",
            index=self.name,
            file=self.file,
            rebuild_token=authority.token,
            expected_seq=authority.header_seq,
            device_seq=certificate.seq,
            retryable=True,
        )
        self._rebuild_authority = None
        self._discard_replay_frames(failure)
        raise failure

    def _publish_active_replay_prefix(self) -> None:
        """Flush one rebuild prefix while stale and rebind authority to any own header write."""
        authority = self._rebuild_authority
        if authority is None:
            return
        self._pool.flush(self.file)
        certificate = self._fresh_certificate()
        if not certificate.header.flags & INDEX_FLAG_STALE:
            raise GrafxIndexError(
                f"Index {self.name!r} became healthy before its replay prefix completed.",
                field="rebuild_superseded",
                index=self.name,
                file=self.file,
                expected_seq=authority.header_seq,
                device_seq=certificate.seq,
                retryable=True,
            )
        self._rebuild_authority = replace(authority, header_seq=certificate.seq)
        self._stale_device_seq = certificate.seq

    def _discard_replay_frames(self, failure: BaseException) -> None:
        """Drop every local frame a refused rebuild replay could otherwise write later."""
        self._carried_certificate = None
        self._invalidate_tombstone_backlog()
        for file, page_index in self._pool.modified_pages(self.file):
            if file != self.file:
                continue
            try:
                self._pool.discard(file, page_index)
            except (
                BaseException
            ) as cleanup_failure:  # pragma: no cover - diagnostic only
                failure.add_note(
                    f"Discarding replay frame {file!r}:{page_index} also failed: "
                    f"{cleanup_failure!r}"
                )
        try:
            self._pool.discard_clean_file(self.file)
        except BaseException as cleanup_failure:  # pragma: no cover - diagnostic only
            failure.add_note(
                "Discarding clean replay frames after refusal also failed: "
                f"{cleanup_failure!r}"
            )

    # --- reading ----------------------------------------------------------------------------

    def _require_exact_read_lsn(self, snapshot: object) -> Lsn:
        """Return the durable position an authoritative public lookup must prove."""
        if type(snapshot) is not Snapshot and not isinstance(snapshot, SnapshotLike):
            raise GrafxIndexError(
                f"A lookup needs a snapshot; got {type(snapshot).__name__}.",
                field="snapshot",
                value=type(snapshot).__name__,
                index=self.name,
            )
        read_lsn = getattr(snapshot, "read_lsn", None)
        if (
            isinstance(read_lsn, bool)
            or not isinstance(read_lsn, int)
            or not NO_LSN <= read_lsn < PROVISIONAL_CSN
        ):
            raise GrafxIndexError(
                "An index lookup needs an integer snapshot.read_lsn so freshness can be "
                "proved against the durable build position.",
                field="snapshot.read_lsn",
                value=repr(read_lsn),
                index=self.name,
            )
        return read_lsn

    def _stable_view(
        self,
        required_lsn: Lsn,
        operation: Callable[[_IndexReadCertificate], _ReadResult],
    ) -> _ReadResult:
        """Run one materialising read wholly inside a durable page-0 generation."""
        for attempt in range(INDEX_READ_RETRY_BUDGET + 1):
            try:
                certificate = self.begin_exact_read(required_lsn)
            except GrafxIndexError as failure:
                if (
                    failure.details.get("field") == "index_view_unavailable"
                    and attempt < INDEX_READ_RETRY_BUDGET
                ):
                    continue
                raise
            result = operation(certificate)
            if self.finish_exact_read(certificate, required_lsn):
                return result
        raise GrafxIndexError(
            f"Index {self.name!r} changed during every durable read attempt.",
            field="index_view_changed",
            index=self.name,
            file=self.file,
            attempts=INDEX_READ_RETRY_BUDGET + 1,
            retryable=True,
        )

    def _stable_candidates(
        self, wanted: bytes, required_lsn: Lsn
    ) -> tuple[IndexEntry, ...]:
        """Return candidates traversed wholly inside one durable index view."""
        return self._stable_view(
            required_lsn, lambda _certificate: self._candidates_unchecked(wanted)
        )

    def _stable_entries(self, required_lsn: Lsn) -> tuple[IndexEntry, ...]:
        """Return the complete stored entry set from one durable index view."""
        return self._stable_view(required_lsn, lambda _certificate: self.walk())

    def candidates(self, key: bytes) -> tuple[IndexEntry, ...]:
        """Return every stored entry under this key, in the order the bucket holds them.

        The order is a property of the walk -- pages in chain order, slots in slot order -- so
        two runs over the same file answer identically and a caller may rely on the sequence.
        """
        self._require_readable()
        wanted = self._require_key(key)
        return self._candidates_unchecked(wanted)

    def _candidates_unchecked(self, wanted: bytes) -> tuple[IndexEntry, ...]:
        """Walk one already-validated key; the manager surrounds this with its view fence."""
        _pages, found = self._scan_bucket(
            bucket_of(wanted, self._definition.bucket_count), wanted
        )
        return found

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
                for slot, payload in page.iter_slot_views()
            )

    def _entry_headers(self) -> tuple[_IndexEntryHeader, ...]:
        """Materialise every validated entry header in canonical walk order.

        This is the build-only middle ground between scalar/ref-only scans and the public full
        :meth:`walk`.  It validates the same page and entry images in bucket/chain/slot order,
        copies only the key which must outlive the page pin, and retains the encoded reference
        until the consumer actually needs an ``IndexEntry``.  Returning a tuple keeps every pin
        inside this method and preserves the full-walk rule that all index images are validated
        before a caller starts fallible heap or vector work.
        """
        headers: list[_IndexEntryHeader] = []
        for bucket in range(self._definition.bucket_count):
            for page_index in self._bucket_pages(bucket):
                with self._pool.pinned(self.file, page_index) as page:
                    self._require_index_page(page, page_index)
                    for slot, image in page.iter_slot_views():
                        (
                            validated,
                            encoded_ref,
                            born_csn,
                            dead_csn,
                            versioned,
                        ) = _validated_image(image)
                        _require_decodable_ref(encoded_ref)
                        headers.append(
                            _IndexEntryHeader(
                                page=page_index,
                                slot=slot,
                                encoded_ref=encoded_ref,
                                born_csn=born_csn,
                                dead_csn=dead_csn,
                                versioned=versioned,
                                key=bytes(validated[INDEX_ENTRY_HEADER_SIZE:]),
                            )
                        )
        return tuple(headers)

    def _entry_counts_from_headers(self) -> tuple[int, int]:
        """Return ``(stored, live)`` after validating every entry without DTOs.

        This is a private cost path, not a weaker walk.  It visits buckets, chains and slots in
        exactly the order :meth:`walk` does and runs the shared entry-image validator on every
        live slot.  Only the validated ``dead_csn`` field is retained, so callers which need an
        entry, its key or its physical index location must continue to use :meth:`walk`.
        """
        stored = 0
        live = 0
        for bucket in range(self._definition.bucket_count):
            for page_index in self._bucket_pages(bucket):
                with self._pool.pinned(self.file, page_index) as page:
                    self._require_index_page(page, page_index)
                    for _slot, image in page.iter_slot_views():
                        _image, _ref, _born_csn, dead_csn, _versioned = (
                            _validated_image(image)
                        )
                        _require_decodable_ref(_ref)
                        stored += 1
                        if dead_csn == NO_CSN:
                            live += 1
        return stored, live

    def _entry_refs_from_headers(self) -> tuple[RecordRef, ...]:
        """Materialise every validated heap reference without constructing index DTOs.

        The tuple is complete before the method returns, so no page pin or memoryview escapes to
        the caller.  This door is intentionally insufficient for verification and maintenance:
        those consumers need keys and index locations and therefore retain the canonical full
        :meth:`walk`.
        """
        refs: list[RecordRef] = []
        for bucket in range(self._definition.bucket_count):
            for page_index in self._bucket_pages(bucket):
                with self._pool.pinned(self.file, page_index) as page:
                    self._require_index_page(page, page_index)
                    for _slot, image in page.iter_slot_views():
                        _image, encoded_ref, _born_csn, _dead_csn, _versioned = (
                            _validated_image(image)
                        )
                        refs.append(RecordRef.decode(encoded_ref))
        return tuple(refs)

    def _matching_entries_on(
        self, page_index: PageIndex, key: bytes, ref: RecordRef | None = None
    ) -> tuple[IndexEntry, ...]:
        """Validate every slot on a page but materialize only entries matching this probe."""
        found: list[IndexEntry] = []
        with self._pool.pinned(self.file, page_index) as page:
            self._require_index_page(page, page_index)
            for slot in page.live_slots():
                entry = IndexEntry.decode_if_matches(page.slot_view(slot), key, ref)
                if entry is not None:
                    found.append(entry.located_at(page_index, slot))
        return tuple(found)

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
        self._record_reconciled(horizon)
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
            # Bind the derived count to this handle's own page-0 publication. Otherwise the next
            # commit mistakes this unlogged local horizon advance for a foreign generation and
            # pays a needless full backlog walk. Disabled/exact metrics keep their former path.
            self._remember_local_certificate()
            self._metrics.increment(RECONCILIATION_TOTAL)
            self._publish_tombstone_backlog()

    def _record_reconciled(self, horizon: Lsn) -> None:
        """Advance the reconciliation watermark without choosing a flush boundary."""
        header = self._read_header()
        reconciled = header.reconciled_to(horizon)
        if reconciled is not header:
            self._write_header(reconciled)

    def _tombstone_backlog(self) -> int:
        """Return how many entries carry a tombstone that has not been reclaimed yet."""
        count = self._tombstone_backlog_count
        if count is None:
            count = sum(1 for entry in self.walk() if not entry.live)
            self._tombstone_backlog_count = count
        return count

    def _publish_tombstone_backlog(self) -> None:
        """Publish the derived backlog, seeding it once when its generation is unknown."""
        if self._metrics.enabled and self._definition.versioned:
            self._metrics.set_gauge(TOMBSTONE_BACKLOG, float(self._tombstone_backlog()))

    def _invalidate_tombstone_backlog(self) -> None:
        """Forget the derived count when this handle cannot prove the paged generation."""
        self._tombstone_backlog_count = None

    def _adjust_tombstone_backlog(self, delta: int) -> None:
        """Apply a proved live/dead transition to an already-seeded derived count."""
        if not self._metrics.enabled or not self._definition.versioned:
            return
        count = self._tombstone_backlog_count
        if count is None:
            return
        adjusted = count + delta
        # A negative diagnostic count proves that some state transition escaped this handle.
        # Re-seeding on the next gauge is safer than publishing an invented correction.
        self._tombstone_backlog_count = adjusted if adjusted >= 0 else None

    def _prepare_live_hot_buckets(
        self, staged: _Staged
    ) -> dict[int, _CommonReplayHotBucket]:
        """Build bounded call-local directories for one fully fenced live commit.

        This door is reached only through ``IndexManager._commit_under_write_authority``.  It
        deliberately declines stale/rebuild/retry state and non-canonical physical hooks; those
        cases retain the scalar protocol that established their recovery semantics.  Every
        accepted bucket is completely validated before this store's first mutation, and every
        page acquisition still passes through the ordinary buffer budget.
        """
        if (
            self._stale_reason is not None
            or self._rebuild_authority is not None
            or self._short_commit is not None
            or staged.defer_clear
            or any(
                change.operation is IndexOperation.RESET for change in staged.changes
            )
            or not self._uses_canonical_live_hot_hooks()
        ):
            return {}

        counts: dict[int, int] = {}
        for change in staged.changes:
            bucket = bucket_of(change.key, self._definition.bucket_count)
            if bucket not in counts and len(counts) >= _COMMON_REPLAY_HOT_BUCKET_LIMIT:
                return {}
            counts[bucket] = counts.get(bucket, 0) + 1

        targets_by_bucket: dict[int, set[tuple[bytes, RecordRef]]] = {
            bucket: set()
            for bucket, count in counts.items()
            if count >= _LIVE_COMMIT_HOT_BUCKET_MIN_EFFECTS
        }
        if not targets_by_bucket:
            return {}
        retained_targets = 0
        for change in staged.changes:
            bucket = bucket_of(change.key, self._definition.bucket_count)
            targets = targets_by_bucket.get(bucket)
            if targets is None:
                continue
            identity = (change.key, change.ref)
            if identity in targets:
                continue
            retained_targets += 1
            if retained_targets > _COMMON_REPLAY_HOT_TARGET_LIMIT:
                return {}
            targets.add(identity)

        prepared: dict[int, _CommonReplayHotBucket] = {}
        remaining_pages = _COMMON_REPLAY_HOT_PAGE_LIMIT
        for bucket, targets in targets_by_bucket.items():
            hot = self._prepare_common_replay_hot_bucket(
                bucket, targets, page_limit=remaining_pages
            )
            if hot is None:
                continue
            prepared[bucket] = hot
            remaining_pages -= len(hot.pages.pages)
        return prepared

    def _uses_canonical_live_hot_hooks(self) -> bool:
        """Return whether this store retains every physical hook the fast path replaces.

        Unlike replay, this path does not replace ``apply`` or ``commit``.  A vector
        store may therefore keep its outer commit semantics while reusing these
        inherited canonical physical hooks.
        """
        resolved: list[object] = []
        for name in _LIVE_HOT_HOOK_NAMES:
            hook = getattr(self, name)
            resolved.append(getattr(hook, "__func__", hook))
        return tuple(resolved) == _CANONICAL_LIVE_HOT_HOOKS

    @staticmethod
    def _page_insert_capacity(page: Page) -> int:
        """Return the largest payload one additional slot can hold after compaction."""
        return page.compactable_space() - SLOT_ENTRY_SIZE

    def _prepare_common_replay_hot_bucket(
        self,
        bucket: int,
        targets: Collection[tuple[bytes, RecordRef]],
        *,
        page_limit: int,
    ) -> _CommonReplayHotBucket | None:
        """Validate one hot bucket and retain locations only for this replay's targets.

        A duplicate target makes scalar lookup semantics significant (the legacy path chooses
        the first match), so that bucket declines acceleration.  Malformed pages or entries are
        not hidden behind a decline: preparation propagates their typed refusal while the batch
        is still mutation-free.
        """
        if page_limit < 1:
            return None
        pages: list[PageIndex] = []
        entries: dict[
            tuple[bytes, RecordRef],
            tuple[PageIndex, SlotId, IndexEntry] | None,
        ] = dict.fromkeys(targets)
        targets_by_ref: dict[
            int,
            dict[bytes, tuple[bytes, RecordRef]],
        ] = {}
        for target in targets:
            targets_by_ref.setdefault(target[1].encode(), {})[target[0]] = target
        capacities: list[int] = []
        duplicate_target = False
        seen: set[PageIndex] = visited_pages()
        lazy_bound_after = 8
        chain_limit: int | None = None
        page_index: PageIndex = self._bucket_head(bucket)
        while page_index != NO_PAGE:
            # A larger chain is not an error, but it is outside this bounded accelerator.  The
            # scalar path remains authoritative and will validate the rest as it applies.
            if len(pages) >= page_limit:
                return None
            if chain_limit is None and len(pages) >= lazy_bound_after:
                chain_limit = self._pool.storage.page_count(self.file) + 1
            if chain_limit is not None:
                refuse_endless_chain(self.file, len(pages) + 1, chain_limit)
            if page_index in seen:
                raise GrafxCorruptionDetected(
                    f"The bucket chain of {self.file!r} returns to page {page_index}, so it "
                    "is a cycle.",
                    file=self.file,
                    page=page_index,
                    field="cycle",
                )
            seen.add(page_index)
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                capacities.append(self._page_insert_capacity(page))
                for slot, image in page.iter_slot_views():
                    (
                        validated,
                        encoded_ref,
                        born_csn,
                        dead_csn,
                        versioned,
                    ) = _validated_image(image)
                    _require_decodable_ref(encoded_ref)
                    candidates = targets_by_ref.get(encoded_ref)
                    if candidates is None:
                        continue
                    key_view = validated[INDEX_ENTRY_HEADER_SIZE:]
                    # ``key_view`` is backed by the mutable page image and is
                    # therefore not hashable, even when exposed read-only.  Copy
                    # only slots whose encoded ref matches a target; this keeps
                    # the common non-target path allocation-free while avoiding
                    # an O(targets-per-ref) identity scan.
                    identity = candidates.get(bytes(key_view))
                    if identity is None:
                        continue
                    if entries[identity] is not None:
                        duplicate_target = True
                        continue
                    key, ref = identity
                    entry = IndexEntry(
                        key=key,
                        ref=ref,
                        versioned=versioned,
                        born_csn=born_csn,
                        dead_csn=dead_csn,
                        page=page_index,
                        slot=slot,
                    )
                    entries[identity] = (
                        page_index,
                        slot,
                        entry,
                    )
                following = page.next_page
            pages.append(page_index)
            page_index = following
        if duplicate_target:
            return None
        return _CommonReplayHotBucket(_FirstFitPages(pages, capacities), entries)

    def _place_during_common_replay(
        self,
        bucket: _CommonReplayHotBucket,
        entry: IndexEntry,
        lsn: Lsn,
    ) -> tuple[PageIndex, SlotId, IndexEntry]:
        """Place through one accepted hot-bucket directory without an ambiguous fallback."""
        payload = entry.encode()
        position = bucket.pages.first_fit(len(payload))
        while position is not None:
            page_index = bucket.pages.pages[position]
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                try:
                    slot = page.insert_slot(payload)
                except PageFullError:
                    # The tree is only a selection hint. Page.insert_slot remains the authority
                    # and promises a refused insertion is mutation-free, so excluding this hint
                    # and trying the next candidate preserves _place's ordering and semantics.
                    bucket.pages.update(
                        position,
                        min(self._page_insert_capacity(page), len(payload) - 1),
                    )
                else:
                    self._stamp(page, lsn)
                    bucket.pages.update(position, self._page_insert_capacity(page))
                    return page_index, slot, entry
            position = bucket.pages.first_fit(len(payload))

        if not bucket.pages.pages:
            raise GrafxCorruptionDetected(
                f"A bucket of {self.file!r} has no head page, so an entry has nowhere to go.",
                file=self.file,
                field="bucket",
            )
        fresh = self._pool.allocate(self.file, self.page_type)
        page_index = fresh.page_index
        try:
            slot = fresh.insert_slot(payload)
            self._stamp(fresh, lsn)
            capacity = self._page_insert_capacity(fresh)
        finally:
            self._pool.unpin(self.file, page_index, dirty=True)
        # Preserve _place_on_new_page's failure ordering: fill the new page before linking it.
        tail_index = bucket.pages.pages[-1]
        with self._pool.pinned(self.file, tail_index) as tail:
            self._require_index_page(tail, tail_index)
            tail.next_page = page_index
            tail.dirty = True
        bucket.pages.append(page_index, capacity)
        return page_index, slot, entry

    def _apply_common_replay_hot_change(
        self,
        bucket: _CommonReplayHotBucket,
        change: IndexChange,
        lsn: Lsn,
    ) -> bool:
        """Apply one ordered replay effect through a fully prepared hot-bucket directory."""
        identity = (change.key, change.ref)
        if identity not in bucket.entries:
            raise GrafxCorruptionDetected(
                f"The ephemeral replay directory for index {self.name!r} does not contain an "
                "identity from its prepared batch.",
                field="replay_bucket_identity",
                index=self.name,
                file=self.file,
            )
        located = bucket.entries[identity]
        if change.operation is IndexOperation.INSERT:
            self._require_key(change.key)
            if located is not None:
                return False
            entry = IndexEntry(
                key=change.key,
                ref=change.ref,
                versioned=change.versioned,
                born_csn=change.csn if change.versioned else NO_CSN,
            )
            bucket.entries[identity] = self._place_during_common_replay(
                bucket, entry, lsn
            )
            return True
        if located is None:
            self._missing_targets += 1
            return False
        page_index, slot, entry = located
        if change.operation is IndexOperation.TOMBSTONE:
            if not entry.live:
                return False
            ended = entry.ended_at(change.csn)
            try:
                moved = self._rewrite(page_index, slot, ended, lsn)
            except BaseException:
                self._invalidate_tombstone_backlog()
                raise
            if moved:
                bucket.entries[identity] = (
                    page_index,
                    slot,
                    ended,
                )
                self._adjust_tombstone_backlog(1)
            return moved

        position = bucket.pages.position(page_index)
        if position is None:
            raise GrafxCorruptionDetected(
                f"The ephemeral replay directory for index {self.name!r} lost page "
                f"{page_index} before removing its target.",
                field="replay_bucket_page",
                index=self.name,
                file=self.file,
                page=page_index,
            )
        try:
            moved = self._erase(page_index, slot, lsn)
        except BaseException:
            self._invalidate_tombstone_backlog()
            raise
        if moved:
            bucket.entries[identity] = None
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                bucket.pages.update(position, self._page_insert_capacity(page))
            if not entry.live:
                self._adjust_tombstone_backlog(-1)
        return moved

    def _empty_build_bucket(
        self, build: _EmptyIndexBuild, bucket: int
    ) -> _FirstFitPages | None:
        """Return one batch-local bucket directory after proving every page is still empty."""
        known = build.buckets.get(bucket)
        if known is not None:
            return known
        pages = self._bucket_pages(bucket)
        capacities: list[int] = []
        for page_index in pages:
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                if page.live_slots():
                    build.valid = False
                    return None
                capacities.append(self._page_insert_capacity(page))
        state = _FirstFitPages(pages, capacities)
        build.buckets[bucket] = state
        return state

    def _place_during_empty_build(
        self,
        build: _EmptyIndexBuild,
        bucket: int,
        entry: IndexEntry,
        lsn: Lsn,
    ) -> tuple[PageIndex, SlotId, IndexEntry] | None:
        """Place one entry through the batch first-fit directory, or decline conservatively."""
        pages = self._empty_build_bucket(build, bucket)
        if pages is None:
            return None
        payload = entry.encode()
        position = pages.first_fit(len(payload))
        if position is not None:
            page_index = pages.pages[position]
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                try:
                    slot = page.insert_slot(payload)
                except PageFullError:
                    # A cooperating build owns the surrounding publication fence, so this can
                    # only mean the ephemeral view no longer describes the page.  The canonical
                    # walk re-establishes truth; no page was changed by the refused insertion.
                    build.valid = False
                    return None
                self._stamp(page, lsn)
                pages.update(position, self._page_insert_capacity(page))
            return page_index, slot, entry.located_at(page_index, slot)

        if not pages.pages:
            build.valid = False
            return None
        fresh = self._pool.allocate(self.file, self.page_type)
        page_index = fresh.page_index
        try:
            slot = fresh.insert_slot(payload)
            self._stamp(fresh, lsn)
            capacity = self._page_insert_capacity(fresh)
        finally:
            self._pool.unpin(self.file, page_index, dirty=True)
        # Match _place_on_new_page's failure ordering: the new page is complete before the old
        # tail links it, so an interrupted link leaves unreachable space rather than a bad chain.
        tail_index = pages.pages[-1]
        with self._pool.pinned(self.file, tail_index) as tail:
            self._require_index_page(tail, tail_index)
            tail.next_page = page_index
            tail.dirty = True
        pages.append(page_index, capacity)
        return page_index, slot, entry.located_at(page_index, slot)

    def _apply_empty_build_change(
        self, build: _EmptyIndexBuild, change: IndexChange, lsn: Lsn
    ) -> bool | None:
        """Apply against a batch proved empty, returning None when canonical fallback is needed."""
        if not build.valid or change.operation is IndexOperation.RESET:
            return None
        bucket = bucket_of(change.key, self._definition.bucket_count)
        identity = (change.key, change.ref)
        located = build.entries.get(identity)
        if change.operation is IndexOperation.INSERT:
            self._require_key(change.key)
            if located is not None:
                return False
            entry = IndexEntry(
                key=change.key,
                ref=change.ref,
                versioned=change.versioned,
                born_csn=change.csn if change.versioned else NO_CSN,
            )
            placed = self._place_during_empty_build(build, bucket, entry, lsn)
            if placed is None:
                return None
            build.entries[identity] = placed
            return True
        if located is None:
            self._missing_targets += 1
            return False
        page_index, slot, entry = located
        if change.operation is IndexOperation.TOMBSTONE:
            if not entry.live:
                return False
            ended = entry.ended_at(change.csn)
            try:
                moved = self._rewrite(page_index, slot, ended, lsn)
            except BaseException:
                build.valid = False
                self._invalidate_tombstone_backlog()
                raise
            if moved:
                build.entries[identity] = (
                    page_index,
                    slot,
                    ended.located_at(page_index, slot),
                )
                self._adjust_tombstone_backlog(1)
            return moved
        try:
            moved = self._erase(page_index, slot, lsn)
        except BaseException:
            build.valid = False
            self._invalidate_tombstone_backlog()
            raise
        if moved:
            build.entries.pop(identity, None)
            pages = build.buckets.get(bucket)
            if pages is None:
                build.valid = False
                return moved
            position = pages.position(page_index)
            if position is None:
                build.valid = False
                return moved
            with self._pool.pinned(self.file, page_index) as page:
                self._require_index_page(page, page_index)
                pages.update(position, self._page_insert_capacity(page))
            if not entry.live:
                self._adjust_tombstone_backlog(-1)
        return moved

    # --- applying -----------------------------------------------------------------------------

    def _apply_change(self, change: IndexChange, lsn: Lsn) -> bool:
        """Apply one change to the pages and say whether anything moved."""
        if change.operation is IndexOperation.RESET:
            try:
                moved = self._reset(change, lsn)
            except BaseException:
                self._invalidate_tombstone_backlog()
                raise
            if moved and self._metrics.enabled and self._definition.versioned:
                self._tombstone_backlog_count = 0
            return moved
        bucket = bucket_of(change.key, self._definition.bucket_count)
        pages, matches = self._scan_bucket(
            bucket, change.key, change.ref, first_matching_page=True
        )
        located = (
            None
            if not matches
            else (matches[0].page, matches[0].slot, matches[0])
        )
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
            try:
                moved = self._rewrite(page_index, slot, entry.ended_at(change.csn), lsn)
            except BaseException:
                # A storage/page implementation may mutate before reporting interruption. The
                # diagnostic count cannot decide which side landed, so it becomes unknown.
                self._invalidate_tombstone_backlog()
                raise
            if moved:
                self._adjust_tombstone_backlog(1)
            return moved
        try:
            moved = self._erase(page_index, slot, lsn)
        except BaseException:
            self._invalidate_tombstone_backlog()
            raise
        if moved and not entry.live:
            self._adjust_tombstone_backlog(-1)
        return moved

    def _reset(self, change: IndexChange, lsn: Lsn) -> bool:
        """Clear every entry of every bucket, keeping the pages and the chains they form."""
        # Re-check immediately before the first bucket mutation. The RESET record's ref.page is
        # the exact stale generation claimed at staging; a later claimant supersedes it.
        certificate = self._fresh_certificate()
        if not certificate.header.flags & INDEX_FLAG_STALE:
            if self._replaying and certificate.header.built_through_lsn >= change.csn:
                # A retained WAL may replay after this rebuild (or a later one) was already
                # published healthy. Re-clearing now would erase subsequent entries; the
                # following logical inserts/tombstones remain idempotent repair operations.
                self._rebuild_authority = None
                return False
            raise GrafxIndexError(
                f"Index {self.name!r} is durably healthy only through "
                f"{certificate.header.built_through_lsn}; RESET through {change.csn} needs a "
                "durable stale authority.",
                field=(
                    "rebuild_superseded" if change.ref.page else "reset_requires_stale"
                ),
                index=self.name,
                file=self.file,
                seq=certificate.seq,
                retryable=True,
            )
        token = change.ref.page
        if token and certificate.seq != token:
            raise GrafxIndexError(
                f"Index {self.name!r} rebuild generation {token} was superseded by durable "
                f"generation {certificate.seq}.",
                field="rebuild_superseded",
                index=self.name,
                file=self.file,
                rebuild_token=token,
                device_seq=certificate.seq,
                retryable=True,
            )
        self._completed_rebuild_through = None
        self._rebuild_authority = _RebuildAuthority(
            token=token,
            header_seq=certificate.seq,
            through_lsn=change.csn,
        )
        self._stale_device_seq = certificate.seq
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

    def _rewrite(
        self, page_index: PageIndex, slot: SlotId, entry: IndexEntry, lsn: Lsn
    ) -> bool:
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
        pages, _matches = self._scan_bucket(bucket)
        return pages

    def _scan_bucket(
        self,
        bucket: int,
        key: bytes | None = None,
        ref: RecordRef | None = None,
        *,
        first_matching_page: bool = False,
    ) -> tuple[tuple[PageIndex, ...], tuple[IndexEntry, ...]]:
        """Validate one chain and optionally collect matches during that same page pass.

        ``_bucket_pages`` remains the structure-only door used by full walks. Point reads and
        scalar writes already know their key, so paying a second pin pass over every page cannot
        reveal a fresher authority: their surrounding exact-view/commit fences decide freshness.
        This fused pass retains chain order, slot order and both termination guards.  Validation
        is deliberately interleaved: if an early entry and a later page structure are both
        corrupt, the entry is reported first instead of the later structure; both outcomes remain
        fail-closed and precede mutation.  ``first_matching_page`` then keeps later pages
        structure-only, matching the former ``_find_entry`` decode boundary.
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
        matches: list[IndexEntry] = []
        matching_complete = False
        seen: set[PageIndex] = visited_pages()
        # Almost every bucket is one or two pages long. Asking the device for the file size on
        # every such walk is pure fixed cost; defer that second, independent termination guard
        # until a chain is unusually long. Once observed, the bound is frozen for this walk so
        # concurrent growth cannot turn a corrupt chain into an unbounded one.
        lazy_bound_after = 8
        limit: int | None = None
        index: PageIndex = self._bucket_head(bucket)
        while index != NO_PAGE:
            if limit is None and len(pages) >= lazy_bound_after:
                limit = self._pool.storage.page_count(self.file) + 1
            if limit is not None:
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
                if key is not None and not matching_complete:
                    for slot, image in page.iter_slot_views():
                        entry = IndexEntry.decode_if_matches(image, key, ref)
                        if entry is not None:
                            matches.append(entry.located_at(index, slot))
                    if first_matching_page and matches:
                        # Scalar mutation historically stopped decoding after the first page
                        # with a match, while its preceding chain walk still validated every
                        # page type/link. Preserve that bounded work and error surface exactly.
                        matching_complete = True
                following = page.next_page
            pages.append(index)
            index = following
        return tuple(pages), tuple(matches)

    def _find_entry(
        self, pages: Sequence[PageIndex], key: bytes, ref: RecordRef
    ) -> tuple[PageIndex, SlotId, IndexEntry] | None:
        """Return where the entry for this key and heap location lives, or None when it does not."""
        for page_index in pages:
            entries = self._matching_entries_on(page_index, key, ref)
            if entries:
                entry = entries[0]
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
        if csn < NO_CSN or csn >= PROVISIONAL_CSN:
            raise GrafxIndexError(
                f"An index change needs a {field_name} between {NO_CSN} and "
                f"{PROVISIONAL_CSN - 1}; got {csn}.",
                field=field_name,
                value=csn,
                index=self.name,
            )
        return csn

    def _require_txn(self, txn: object) -> int:
        """Return the transaction number, refusing anything that cannot stage a record."""
        if type(txn) is not TransactionContext and not isinstance(txn, StagingTransaction):
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
        read_lsn = self._require_exact_read_lsn(snapshot)
        wanted = self._require_key(key)
        return tuple(entry.ref for entry in self._stable_candidates(wanted, read_lsn))

    def candidates(self, key: bytes) -> tuple[IndexEntry, ...]:
        """Return a stable candidate superset at the index's current durable frontier.

        This lower-level door has no caller snapshot and therefore cannot promise coverage of a
        later position. It does prove that the returned bucket traversal belongs to one healthy
        durable certificate; callers needing a snapshot answer use :meth:`lookup` or the manager.
        """
        wanted = self._require_key(key)
        return self._stable_candidates(wanted, NO_LSN)


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
        if (
            IndexVisibility.parse(definition.visibility)
            is not IndexVisibility.PROXIMITY
        ):
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
        read_lsn = self._require_exact_read_lsn(snapshot)
        wanted = self._require_key(key)
        return tuple(
            entry.ref
            for entry in self._stable_candidates(wanted, read_lsn)
            if entry_visible(entry, snapshot)
        )

    def candidates(self, key: bytes) -> tuple[IndexEntry, ...]:
        """Return candidates from one healthy durable generation at its current frontier."""
        wanted = self._require_key(key)
        return self._stable_candidates(wanted, NO_LSN)

    def visible_entries(self, snapshot: SnapshotLike) -> tuple[IndexEntry, ...]:
        """Return every entry of the whole index this snapshot may see.

        A proximity structure rarely looks a key up: it navigates. This is the door that gives a
        navigator the same visibility answer the keyed lookup gives, so a graph built on top of
        this store never has to re-derive the rule -- and never has to consult the heap.
        """
        read_lsn = self._require_exact_read_lsn(snapshot)
        return tuple(
            entry
            for entry in self._stable_entries(read_lsn)
            if entry_visible(entry, snapshot)
        )


_CANONICAL_LIVE_HOT_HOOKS = tuple(
    getattr(IndexStore, name) for name in _LIVE_HOT_HOOK_NAMES
)


_TableIdentity = tuple[int, str]


def _table_identity(table: object) -> _TableIdentity | None:
    """Return the complete catalog identity needed to distinguish reused table ids."""
    table_id = getattr(table, "table_id", None)
    table_name = getattr(table, "name", None)
    if (
        isinstance(table_id, bool)
        or not isinstance(table_id, int)
        or table_id < 1
        or not isinstance(table_name, str)
        or not table_name
    ):
        return None
    return table_id, table_name


def _tables_written_by(txn: object) -> frozenset[_TableIdentity] | None:
    """Return effective row-intent table identities, or None when they cannot be proved.

    The heap writer caches the id-and-name pairs from the already-reduced intents it actually
    materialized. Consume that certificate first: reducing the public intent history again here
    would add an avoidable O(n) pass inside the cross-process commit section, after the WAL
    barrier. Direct index callers and lightweight doubles have no certificate, so retain the
    conservative reducer/raw-intent fallback for those compatibility shapes.

    Real transactions retain their raw intent history until commit. A pending INSERT followed
    by its DELETE is therefore present in that history even though the shared reducer correctly
    makes both disappear before any heap or index work. Deriving table writes from the raw list
    would call the missing-observation guard below for a mutation that does not exist and poison
    the unchanged index as stale. Use the same reduced outcome as the heap writer. Lightweight
    index test doubles predate ``RowIntent`` and deliberately expose only ``.table``; preserve
    their conservative raw-table evidence instead of inventing reducer fields for them.
    """
    certified = getattr(txn, "_effective_row_tables", None)
    if isinstance(certified, frozenset) and all(
        isinstance(identity, tuple)
        and len(identity) == 2
        and isinstance(identity[0], int)
        and not isinstance(identity[0], bool)
        and isinstance(identity[1], str)
        and bool(identity[1])
        for identity in certified
    ):
        return certified
    intents = getattr(txn, "row_intents", None)
    if intents is None:
        return None
    captured = tuple(intents)
    effective = (
        reduce_row_intents(captured)
        if all(isinstance(intent, RowIntent) for intent in captured)
        else captured
    )
    tables: set[_TableIdentity] = set()
    for intent in effective:
        identity = _table_identity(getattr(intent, "table", None))
        if identity is None:
            return None
        tables.add(identity)
    return frozenset(tables)


EDGE_FROM_INDEX_PREFIX: str = "ef_"
"""What the index over a relationship table's SOURCE endpoint is named after."""

EDGE_TO_INDEX_PREFIX: str = "et_"
"""What the index over a relationship table's TARGET endpoint is named after."""


def edge_from_index_name(table_name: str) -> str:
    """Return the name of the index over that relationship table's source endpoint."""
    return f"{EDGE_FROM_INDEX_PREFIX}{table_name}"


def edge_to_index_name(table_name: str) -> str:
    """Return the name of the index over that relationship table's target endpoint."""
    return f"{EDGE_TO_INDEX_PREFIX}{table_name}"


def relationship_endpoint_indexes(
    table: object, pool: BufferPool, metrics: MetricsSink
) -> tuple[HashIndex, ...]:
    """Return the two exact indexes covering a relationship table's endpoints, or none.

    A stored relationship row leads with the two endpoints it connects (W5c): ``_from`` and
    ``_to`` at positions 0 and 1 of the STORED tuple, each a record identity, ahead of the
    properties. Traversal is a walk over those two columns, and without an index it was a scan
    of every edge per frontier node -- measured on a 2500-node knowledge graph, a reverse hop
    into a well-referenced entity read all 3600 edges and cost 1.46 s.

    The positions deliberately index the stored tuple, not ``table.columns``: a relationship
    table's declared columns are its properties, which sit AFTER the endpoints, and the one
    thing every consumer of an IndexDefinition agrees on -- ``key_for``, the commit's staging,
    the verifier's drift detector -- is that positions index the stored values. The PLANNER
    maps WHERE clauses through column names and therefore never chooses these; the traversal
    operator is their one reader, by name.

    EXACT visibility, for the same reason the primary key's index is exact: a hit is a
    candidate, and ``IndexManager.lookup`` validates every one against the heap under the
    caller's snapshot, so the index may be a superset and can never be a wrong answer.
    """
    if getattr(table, "kind", None) != "rel":
        return ()
    return (
        HashIndex(
            IndexDefinition(
                name=edge_from_index_name(table.name),
                table_id=table.table_id,
                table_name=table.name,
                positions=(0,),
                visibility=IndexVisibility.EXACT,
            ),
            pool,
            metrics,
        ),
        HashIndex(
            IndexDefinition(
                name=edge_to_index_name(table.name),
                table_id=table.table_id,
                table_name=table.name,
                positions=(1,),
                visibility=IndexVisibility.EXACT,
            ),
            pool,
            metrics,
        ),
    )


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

    __slots__ = (
        "_pool",
        "_heap",
        "_metrics",
        "_indexes",
        "_index_keys_by_table",
        "_published_lsn",
        "_table_watermarks",
        "_heap_cache_certificates",
        "_artifact_nonce",
        "_artifact_claims",
        "_detached_speculative_indexes",
        "_schema_observed",
        "_schema_new_table_observed",
        "_definition_match",
    )

    def __init__(
        self,
        pool: BufferPool,
        heap: HeapStore,
        metrics: MetricsSink,
        *,
        artifact_nonce: Callable[[], int] | None = None,
    ) -> None:
        """Build the registry over the pool and heap of one database."""
        self._pool: BufferPool = pool
        self._heap: HeapStore = heap
        self._metrics: MetricsSink = metrics
        self._indexes: dict[str, IndexStore] = {}
        self._index_keys_by_table: dict[tuple[int, str], set[str]] = {}
        self._published_lsn: Lsn = NO_LSN
        self._table_watermarks: dict[int, Lsn] = {}
        # Exact answers validate index candidates against heap pages.  The index certificate is
        # therefore also the clock for the heap cache used by that validation: every committed
        # row change of the covered table advances its maintained index page 0 before becoming
        # visible.  A new certificate drops clean heap frames before they can confirm a deleted
        # or superseded version from another process.
        self._heap_cache_certificates: dict[str, _IndexReadCertificate] = {}
        self._artifact_nonce = artifact_nonce
        self._artifact_claims: dict[IndexStore, set[object]] = {}
        self._detached_speculative_indexes: set[IndexStore] = set()
        self._schema_observed: dict[int, dict[IndexStore, int]] = {}
        self._schema_new_table_observed: dict[int, set[IndexStore]] = {}
        # This is schema normalization, not index authority.  The complete immutable physical
        # definition (including generation nonce/bucket count) and complete immutable TableDef
        # form the key, so DDL or rehash cannot inherit a prior answer.  The manager lifetime and
        # finite ceiling avoid process-global retention while a large schema transaction asks the
        # same comparison quadratically often.
        self._definition_match = lru_cache(
            maxsize=_DEFINITION_MATCH_MEMO_LIMIT, typed=True
        )(index_definition_matches_table)

    # --- registry ---------------------------------------------------------------------------

    def _publish_registered_index(self, index: IndexStore) -> None:
        """Publish one raw ownership entry and its table-local structural key."""

        key = index.definition.registry_key
        previous = self._indexes.get(key)
        if previous is not None and previous is not index:
            self._remove_registered_index(key, expected=previous)
        self._indexes[key] = index
        identity = (index.definition.table_id, index.definition.table_name)
        self._index_keys_by_table.setdefault(identity, set()).add(key)

    def _remove_registered_index(
        self, key: str, *, expected: IndexStore | None = None
    ) -> IndexStore | None:
        """Remove one raw ownership entry without leaving its table bucket stale."""

        current = self._indexes.get(key)
        if current is None or (expected is not None and current is not expected):
            return None
        removed = self._indexes.pop(key)
        identity = (removed.definition.table_id, removed.definition.table_name)
        table_keys = self._index_keys_by_table.get(identity)
        if table_keys is not None:
            table_keys.discard(key)
            if not table_keys:
                self._index_keys_by_table.pop(identity, None)
        return removed

    def _registered_indexes_for(
        self, table_id: int, *, table_name: str | None = None
    ) -> tuple[IndexStore, ...]:
        """Return raw registry ownership from only the requested table bucket."""

        if table_name is not None:
            keys = self._index_keys_by_table.get((table_id, table_name), ())
        else:
            keys = {
                key
                for (
                    owned_id,
                    _owned_name,
                ), table_keys in self._index_keys_by_table.items()
                if owned_id == table_id
                for key in table_keys
            }
        return tuple(
            index
            for key in sorted(keys)
            if (index := self._indexes.get(key)) is not None
        )

    def register(
        self,
        index: IndexStore,
        *,
        complete_through: Lsn | None = None,
        existing_only: bool = False,
        persist_stale: bool = True,
        proved_present: bool = False,
        _creation: list[bool] | None = None,
    ) -> IndexStore:
        """Register an index, optionally requiring a complete existing file, and check freshness.

        ``complete_through`` is for the one caller that KNOWS the index it is registering has
        nothing to catch up on: the statement that creates a table declares its primary key and
        the table is empty, so the index covers everything there is to cover at the position the
        database has published. It is applied BEFORE the freshness check, and the order is the
        whole point -- `_advance` refuses to move an index that is already marked stale, so an
        advance after the check is a no-op and the index stays stale for ever. That is what
        happened: every table declared in a session after the first got an index that was marked
        stale on the spot and that no amount of loading could lift.

        ``proved_present`` is an internal startup optimization: the composition root already
        proved this exact logical file in one index-directory listing. It suppresses repeated
        existence walks only; opening and validating the persisted structure is unchanged.
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
        created_file = False
        nonce = index.definition.artifact_nonce
        if nonce == 0 and self._artifact_nonce is not None:
            nonce = self._artifact_nonce()
        index._set_creation_nonce(nonce)
        header: IndexHeader
        if existing_only:
            # A read-only composition may inspect an existing accelerator, but it must never
            # repair a zero-length/torn one as a side effect of opening the database. ``create``
            # deliberately repairs that shape, so the strict route proves the structure first
            # and then calls the read-only ``open`` door directly.
            if (not proved_present and not index.exists()) or not index.is_created(
                proved_present=proved_present
            ):
                raise GrafxIndexError(
                    f"Index {index.name!r} has no complete existing file to register without "
                    "creating or repairing one.",
                    field="file",
                    file=index.file,
                    index=index.name,
                )
            header = index.open(proved_present=proved_present)
        else:
            header, created_file = index._create_with_provenance(
                proved_present=proved_present
            )
            if header.artifact_nonce == 0 and nonce != 0:
                header = index._ensure_artifact_nonce(nonce).header
        self._publish_registered_index(index)
        try:
            self._finish_registration(
                index,
                complete_through=complete_through,
                persist_stale=persist_stale,
            )
        except BaseException:
            # Physical creation precedes registry publication and is intentionally preserved:
            # without a cross-process delete CAS those bytes may already have an adopter.  The
            # process-local publication is different.  Until every post-registration proof has
            # succeeded no DDL journal owns it, so compensate only this exact object and the
            # companion cache it may have published.
            self._discard_unclaimed_registration(index)
            raise
        if _creation is not None:
            _creation.append(created_file)
        return index

    def _discard_unclaimed_registration(self, index: IndexStore) -> bool:
        """Compensate one exact provisional registry publication and its cache binding."""
        key = index.definition.registry_key
        if self._indexes.get(key) is not index:
            # A re-entrant host callback may have installed a replacement.  This failure owns
            # neither that object nor the companion certificate it published.
            return False
        self._remove_registered_index(key, expected=index)
        self._heap_cache_certificates.pop(index.file, None)
        return True

    def _finish_registration(
        self,
        index: IndexStore,
        *,
        complete_through: Lsn | None,
        persist_stale: bool,
    ) -> None:
        """Complete freshness and heap-view publication for one provisional registry entry."""
        if complete_through is not None:
            index.advance_built_through(complete_through)
        # Startup registers durable files before recovery has loaded the authoritative published
        # ceiling into the manager. Being ahead of this provisional value is therefore allowed;
        # the final ``open`` checks both sides after recovery.
        cached_required = self._table_watermarks.get(index.definition.table_id)
        if cached_required is not None:
            index._table_high_water = cached_required
            index.check_freshness(
                max(self._published_lsn, cached_required),
                required_lsn=cached_required,
                persist=persist_stale,
                allow_ahead=False,
            )
        elif self._published_lsn == NO_LSN:
            # Composition registers durable files before recovery and DDL registers a new
            # table's indexes before that table is installed in the transaction's catalog
            # picture. At this provisional position the heap cannot be used as a final floor:
            # retained WAL may still repair it, or the table may not be visible here yet. The
            # replay-floor check and final open perform the authoritative table-aware checks.
            index.check_freshness(
                NO_LSN,
                required_lsn=NO_LSN,
                persist=persist_stale,
                allow_ahead=True,
            )
        else:
            try:
                table = self._heap.catalog.catalog.table_by_id(
                    index.definition.table_id
                )
            except GrafxCorruptionDetected as failure:
                if failure.details.get("field") != "table_id":
                    raise
                # A schema transaction can attach the accelerator before publishing the table.
                # Its explicit complete_through certificate is the only floor available here;
                # final open still refuses a genuinely orphaned definition.
                required = NO_LSN
            else:
                required = self._heap.committed_high_water(table)
                self._record_table_watermark(index.definition.table_id, required)
            index.check_freshness(
                self._published_lsn,
                required_lsn=required,
                persist=persist_stale,
                allow_ahead=False,
            )
        if not index.stale:
            try:
                index._stable_view(
                    NO_LSN,
                    lambda certificate: self._prepare_heap_view(
                        index.file, certificate
                    ),
                )
            except GrafxUnsupportedOperation as failure:
                if failure.details.get("field") != "dirty":
                    raise
                # Registration may share a pool with local heap work that is not yet publishable.
                # Leave the companion generation unbound; the first authoritative lookup will
                # fail closed unless a successful local commit binds it first.
                self._heap_cache_certificates.pop(index.file, None)

    def equivalent_registered(self, candidate: IndexStore) -> IndexStore | None:
        """Return the exact equivalent already registered, or refuse a name collision.

        Schema DDL is idempotent only for a complete durable definition.  Numeric table ids and
        names can both be reused by concurrent speculative catalogs, so neither is sufficient;
        dataclass equality includes positions, visibility, bucket layout and key derivation.
        """
        existing = self._indexes.get(candidate.definition.registry_key)
        if existing is None:
            return None
        if existing.definition == candidate.definition:
            return existing
        same_table = (
            existing.definition.table_id == candidate.definition.table_id
            and existing.definition.table_name == candidate.definition.table_name
        )
        raise GrafxIndexError(
            f"Index {existing.name!r} is already registered under a different definition.",
            field="definition" if same_table else "name",
            value=candidate.name,
            index=existing.name,
        )

    def register_speculative(
        self,
        index: IndexStore,
        *,
        complete_through: Lsn | None = None,
        equivalent: Callable[[IndexStore], bool] | None = None,
    ) -> tuple[IndexStore, _SpeculativeIndexArtifact | None]:
        """Register one DDL index and return its exact rollback authority.

        Every equivalent process-local adoption acquires its own claim.  The final rollback may
        release the registration only if no claimant committed it and its physical generation is
        unchanged.  A newly registered object also carries the exclusive-create result and its
        initial durable page-zero generation, never an ``exists`` guess made outside the creation
        door.
        """
        key = index.definition.registry_key
        existing = self._indexes.get(key)
        if existing is not None:
            same_definition = existing.definition == index.definition
            same_semantics = equivalent is None or equivalent(existing)
            if same_definition and same_semantics:
                return existing, self._claim_speculative(
                    existing,
                    generation=existing._fresh_certificate(),
                    created_file=False,
                )

            # Two schema transactions may allocate the same table id/name from the same durable
            # snapshot yet declare different key positions or vector-space semantics.  Neither
            # has won catalog OCC, so the later statement must be allowed to install its own
            # candidate.  Preserve the first candidate's bytes outside the canonical namespace;
            # its transaction retains the old object+nonce and will receive a retryable conflict
            # if it later attempts to publish.  A case-fold collision belonging to another table
            # is the supported unindexed-table regime and is never displaced here.
            same_table = (
                existing.definition.table_id == index.definition.table_id
                and existing.definition.table_name == index.definition.table_name
            )
            if not same_table or not self._preserve_unclaimed_canonical(index.file):
                raise GrafxIndexError(
                    f"Index {existing.name!r} is already registered under a different "
                    "definition.",
                    field="definition" if same_table else "name",
                    value=index.name,
                    index=existing.name,
                )
            if self._indexes.get(key) is existing:
                self._remove_registered_index(key, expected=existing)
                self._heap_cache_certificates.pop(existing.file, None)
        creation: list[bool] = []
        try:
            registered = self.register(
                index,
                complete_through=complete_through,
                _creation=creation,
            )
        except GrafxIndexError as failure:
            if failure.details.get("field") not in {"digest", "visibility"}:
                raise
            if not self._preserve_unclaimed_canonical(index.file):
                raise
            creation.clear()
            registered = self.register(
                index,
                complete_through=complete_through,
                _creation=creation,
            )
        try:
            generation = registered._fresh_certificate()
            artifact = self._claim_speculative(
                registered,
                generation=generation,
                created_file=bool(creation and creation[0]),
            )
        except BaseException:
            # ``register`` has published process-local state but QueryEngine still has no
            # journal token.  A failed fresh generation proof must therefore unwind the exact
            # candidate just like a failed registration finalizer, while preserving a
            # re-entrant replacement and all canonical physical bytes.
            self._discard_unclaimed_registration(registered)
            raise
        return registered, artifact

    def register_detached_speculative(
        self,
        index: IndexStore,
        *,
        complete_through: Lsn | None = None,
    ) -> tuple[IndexStore, _SpeculativeIndexArtifact]:
        """Observe a prebuilt nonced shadow without replacing committed registry authority.

        A detached generation has already won exclusive ownership of its immutable physical
        filename and completed verification.  Publishing it in ``_indexes`` before the catalog
        commit would displace the old ACTIVE generation for every other transaction; routing it
        through :meth:`register_speculative` would additionally treat its own nonced file as a
        canonical collision and move the verified bytes aside.  This door validates the file,
        claims it for rollback/provenance, and leaves it reachable only through the creating
        transaction's explicit schema observation.
        """
        if not isinstance(index, IndexStore) or not isinstance(index, SecondaryIndex):
            raise GrafxIndexError(
                "A detached speculative index must implement the paged secondary-index "
                "contract.",
                field="index",
                value=type(index).__name__,
            )
        if index.definition.artifact_nonce == 0:
            raise GrafxIndexError(
                f"Detached speculative index {index.name!r} needs a non-zero generation "
                "nonce.",
                field="artifact_nonce",
                value=0,
                index=index.name,
            )
        collision = next(
            (
                current
                for current in self._indexes.values()
                if current.file.casefold() == index.file.casefold()
            ),
            None,
        )
        if collision is not None:
            raise GrafxIndexError(
                f"Detached generation {index.file!r} is already registered as "
                f"{collision.name!r}.",
                field="file",
                file=index.file,
                index=index.name,
                registered=collision.name,
                retryable=True,
            )
        index._set_creation_nonce(index.definition.artifact_nonce)
        if not index.exists() or not index.is_created():
            raise GrafxIndexError(
                f"Detached generation {index.file!r} is absent or incomplete.",
                field="file",
                file=index.file,
                index=index.name,
            )
        index.open(proved_present=True)
        self._finish_registration(
            index,
            complete_through=complete_through,
            persist_stale=False,
        )
        generation = index._fresh_certificate()
        artifact = self._claim_speculative(
            index,
            generation=generation,
            created_file=False,
            detached=True,
        )
        return index, artifact

    def _claim_speculative(
        self,
        index: IndexStore,
        *,
        generation: _IndexReadCertificate,
        created_file: bool,
        detached: bool = False,
    ) -> _SpeculativeIndexArtifact:
        """Acquire one process-local DDL claim over an exact registry object."""
        owner = object()
        owners = self._artifact_claims.setdefault(index, set())
        try:
            owners.add(owner)
            if detached:
                self._detached_speculative_indexes.add(index)
            return _SpeculativeIndexArtifact(
                index=index,
                generation=generation,
                created_file=created_file,
                owner=owner,
            )
        except BaseException:
            owners.discard(owner)
            if self._artifact_claims.get(index) is owners and not owners:
                self._artifact_claims.pop(index, None)
                self._detached_speculative_indexes.discard(index)
            raise

    def adopt_committed(
        self,
        index: IndexStore,
        *,
        persist_stale: bool = False,
        proved_present: bool = False,
        replace_equivalent: bool = False,
    ) -> IndexStore:
        """Replace a process-local speculative registration with one proved by the catalog.

        The composition builds ``index`` from the freshly rebased durable catalog and calls this
        only through its existing-only sync route.  Opening the candidate proves the canonical
        bytes before the registry changes.  If validation fails, the former object is restored;
        no file is created or repaired.  ``replace_equivalent`` lets the vector layer rebind an
        equal physical definition to different committed runtime space semantics.
        """
        if not isinstance(index, IndexStore):
            raise GrafxIndexError(
                "A committed index adoption needs a paged IndexStore candidate.",
                field="index",
                value=type(index).__name__,
            )
        key = index.definition.registry_key
        existing = self._indexes.get(key)
        if (
            existing is not None
            and existing.definition == index.definition
            and not replace_equivalent
        ):
            # Re-read the canonical identity even for an equivalent wrapper.  A foreign DDL may
            # have recreated the same definition with another physical nonce.
            existing.open(proved_present=proved_present)
            return existing
        if existing is not None:
            self._remove_registered_index(key, expected=existing)
            self._heap_cache_certificates.pop(existing.file, None)
        try:
            return self.register(
                index,
                existing_only=True,
                persist_stale=persist_stale,
                proved_present=proved_present,
            )
        except BaseException:
            if self._indexes.get(key) is index:
                self._remove_registered_index(key, expected=index)
            if existing is not None:
                self._publish_registered_index(existing)
            raise

    def ensure_artifact_identities(self) -> None:
        """Upgrade every registered legacy artifact to a non-repeatable physical identity.

        The writable composition calls this only inside ``schema_artifact_section``.  Read-only
        opens continue to decode v1 with nonce zero and never mutate it.
        """
        if self._artifact_nonce is None:
            return
        for index in self.active_indexes():
            # The immediately preceding sync proved one complete directory inventory.  Reuse
            # that proof instead of doubling every per-file existence probe merely to inspect
            # the nonce.
            header = index.open(proved_present=True)
            if header.artifact_nonce == 0:
                index._ensure_artifact_nonce(self._artifact_nonce())

    def discard_speculative(self, artifact: object) -> bool:
        """Release only the unchanged process-local object named by one DDL journal.

        This never deletes the canonical file.  Cross-process speculative claimants are not
        enumerable through this process-local registry, so even an unchanged page-zero token is
        not a delete-CAS proof.  Canonical orphan reclamation is a separate attach-time operation
        under ``COMMIT_SECTION`` that first proves no committed catalog definition owns the name.

        If another participant durably advanced the same definition, the object has been adopted
        and is retained.  If the canonical name now contains a different definition, the local
        object is obsolete and is unregistered, but those foreign bytes remain untouched.
        """
        return self.settle_speculative(artifact, committed=False)

    def settle_speculative(self, artifact: object, *, committed: bool) -> bool:
        """Release one exact DDL claim and conditionally retire its unowned registration."""
        if not isinstance(artifact, _SpeculativeIndexArtifact):
            return False
        expected = artifact.index
        claims = self._artifact_claims.get(expected)
        if claims is None or artifact.owner not in claims:
            return False
        detached = expected in self._detached_speculative_indexes
        claims.remove(artifact.owner)
        if not claims:
            self._artifact_claims.pop(expected, None)
            self._detached_speculative_indexes.discard(expected)
        if committed:
            return True
        if claims:
            return False
        key = expected.definition.registry_key
        current = self._indexes.get(key)
        if current is not expected:
            if detached:
                # A detached shadow deliberately never entered the global registry, but
                # registration may still have bound a companion heap certificate while proving
                # it.  Once its final rollback claim is gone, that cache key is unreachable too;
                # retaining it would leak one entry for every refused schema statement.
                self._heap_cache_certificates.pop(expected.file, None)
            return False
        if self._definition_is_durable(expected):
            return False
        try:
            observed = expected._fresh_certificate()
        except GrafxIndexError as failure:
            if failure.details.get("field") not in {"digest", "visibility"}:
                return False
            self._remove_registered_index(key, expected=expected)
            self._heap_cache_certificates.pop(expected.file, None)
            return True
        except GrafxError:
            return False
        if observed != artifact.generation:
            # Same definition, newer durable generation: another completed transaction adopted
            # the object/file.  It is no longer this rollback's state to release.
            return False
        removed = self._remove_registered_index(key)
        if (
            removed is not expected
        ):  # pragma: no cover - participant section serialises locals
            if removed is not None:
                self._publish_registered_index(removed)
            return False
        self._heap_cache_certificates.pop(expected.file, None)
        return True

    def _definition_is_durable(self, index: IndexStore) -> bool:
        """Prove that the refreshed committed catalog selects this physical definition."""
        try:
            definitions = self._catalog_active_definitions()
        except GrafxError:
            return False
        return definitions is not None and index.definition in definitions

    def stage_schema_observation(
        self, index: IndexStore, txn: StagingTransaction
    ) -> bool:
        """Stage and track one DDL empty observation even when the object was adopted."""
        txn_id = index._require_txn(txn)
        created = index.stage_empty_observation(txn)
        observed = self._schema_observed.setdefault(txn_id, {})
        observed[index] = observed.get(index, 0) + 1
        try:
            committed_table = self._heap.catalog.catalog.table_by_id(
                index.definition.table_id
            )
        except GrafxCorruptionDetected as failure:
            if failure.details.get("field") != "table_id":
                raise
            committed_table = None
        if committed_table is None or (
            committed_table.name != index.definition.table_name
            or not self._definition_matches_table_tolerantly(
                index.definition,
                committed_table,
            )
        ):
            # A CREATE TABLE observation establishes the initial table watermark because every
            # sibling index is born at this same schema commit.  An index added to an already
            # committed table is different: schema bytes did not move that table's heap floor.
            self._schema_new_table_observed.setdefault(txn_id, set()).add(index)
        return created

    def discard_schema_observation(
        self,
        index: object,
        txn: StagingTransaction,
        *,
        created: bool,
    ) -> bool:
        """Release exactly one statement's observation claim during schema unwind."""
        if not isinstance(index, IndexStore):
            return False
        txn_id = index._require_txn(txn)
        observed = self._schema_observed.get(txn_id)
        if observed is None or observed.get(index, 0) <= 0:
            return False
        remaining = observed[index] - 1
        if remaining:
            observed[index] = remaining
        else:
            observed.pop(index, None)
            new_table = self._schema_new_table_observed.get(txn_id)
            if new_table is not None:
                new_table.discard(index)
                if not new_table:
                    self._schema_new_table_observed.pop(txn_id, None)
            if not observed:
                self._schema_observed.pop(txn_id, None)
        if created:
            index.discard_empty_observation(txn)
        return True

    def validate_staged_artifacts(
        self,
        txn: StagingTransaction,
        *,
        row_tables: Sequence[object] = (),
    ) -> None:
        """Prove every schema/row staging object still owns its persisted physical nonce.

        The transaction manager calls this under the same ``COMMIT_SECTION`` it retains through
        WAL append and publication.  An ABA replacement can repeat a definition and page
        sequence, but it cannot repeat the composition-root nonce.
        """
        # Concrete stores validate the full staging protocol below.  The manager owns only the
        # registry, so it validates the identifier without pretending to be an IndexStore.
        txn_id = getattr(txn, "txn_id", None)
        if isinstance(txn_id, bool) or not isinstance(txn_id, int) or txn_id < 0:
            raise GrafxIndexError(
                "Index artifact validation needs a transaction with a non-negative txn_id.",
                field="txn_id",
                value=repr(txn_id),
            )
        schema_indexes = tuple(self._schema_observed.get(txn_id, ()))
        staged_indexes = tuple(
            index for index in self._transaction_indexes(txn) if index.observed(txn)
        )
        row_indexes = tuple(
            index
            for table in row_tables
            for index in self.active_indexes_for(
                getattr(table, "table_id", -1),
                table_name=getattr(table, "name", None),
                table=table,
                txn=txn,
            )
        )
        for index in dict.fromkeys((*schema_indexes, *staged_indexes, *row_indexes)):
            detached = (
                index in schema_indexes
                and index in self._detached_speculative_indexes
                and bool(self._artifact_claims.get(index))
            )
            if (
                self._indexes.get(index.definition.registry_key) is not index
                and not detached
            ):
                raise GrafxIndexError(
                    f"Index {index.name!r} is no longer the registered artifact staged by "
                    f"transaction {txn_id}.",
                    field="index_registry",
                    index=index.name,
                    file=index.file,
                    txn_id=txn_id,
                    retryable=True,
                )
            index.validate_staged_artifact(txn)
            if index in row_indexes and self._artifact_nonce is not None:
                certificate = index._fresh_certificate()
                if certificate.header.artifact_nonce == 0:
                    raise GrafxIndexError(
                        f"Index {index.name!r} has no non-repeatable physical identity for "
                        f"transaction {txn_id}.",
                        field="artifact_nonce",
                        index=index.name,
                        file=index.file,
                        observed=0,
                        retryable=True,
                    )

    def _preserve_unclaimed_canonical(self, file: str) -> bool:
        """Move an undeclared canonical index aside for a new speculative definition.

        The caller is ``register_speculative`` while QueryEngine owns ``COMMIT_SECTION``.  No
        commit can publish between the committed-catalog proof and the rename.  The bytes are
        preserved outside ``index/`` because active speculative claimants in other processes are
        not visible here; their later pre-WAL artifact validation will refuse rather than append
        a record naming the displaced canonical file.
        """
        if self._canonical_file_is_declared(file):
            return False
        storage = self._pool.storage
        if not storage.exists(file):
            return True
        try:
            page_zero = storage.read_page(file, HEADER_PAGE_INDEX)
        except GrafxError:
            return False
        fingerprint = hashlib.blake2b(
            file.encode("utf-8") + page_zero, digest_size=16
        ).hexdigest()
        orphan: str | None = None
        for ordinal in range(0x10000):
            suffix = "" if ordinal == 0 else f".{ordinal:04x}"
            orphan = f"index_orphan/{fingerprint}{suffix}.idx"
            if not storage.exists(orphan):
                break
        else:
            # Fixed-width names keep the quarantine namespace bounded. Exhaustion refuses the
            # replacement and preserves the canonical bytes rather than inventing an overwrite.
            return False
        assert (
            orphan is not None
        )  # the bounded loop always assigns before its first check
        for page_index in range(storage.page_count(file)):
            self._pool.discard(file, page_index)
        storage.atomic_replace(file, orphan)
        self._heap_cache_certificates.pop(file, None)
        return True

    def _canonical_file_is_declared(self, file: str) -> bool:
        """Say whether any committed catalog generation owns this physical file.

        Runtime eligibility and physical ownership are deliberately different questions.  Only
        the ACTIVE generation may answer queries or receive DML, but BUILDING and STALE files
        are still catalog-owned bytes and may never be displaced as speculative orphans.
        """
        wanted = file.casefold()
        try:
            catalog = self._heap.catalog.catalog
            definitions = list(self._catalog_active_definitions(catalog) or ())
            logical_definitions = getattr(catalog, "index_definitions", None)
            if callable(logical_definitions):
                for logical in logical_definitions():
                    for generation in logical.generations:
                        definitions.append(logical.runtime_definition(generation))
        except GrafxError:
            return True
        return any(definition.file.casefold() == wanted for definition in definitions)

    def unregister(self, name: str) -> bool:
        """Forget one registered index, leaving its file alone, and say whether one was held.

        The caller is the rollback of a schema transaction: the DDL registered the index the
        moment it ran, and the transaction that asked for it is now not going to happen. The
        FILE stays -- removing it is a device operation inside an unwind, with its own failure
        mode -- and an orphan index file is harmless: the next registration under the same name
        either adopts it (same definition digest) or declines, and nothing else ever reads it.

        A name nothing is registered under answers False rather than raising, because this runs
        while a rollback is already unwinding and must not replace its reason.
        """
        if not isinstance(name, str):
            return False
        removed = self._remove_registered_index(name.lower())
        if removed is None:
            return False
        self._heap_cache_certificates.pop(removed.file, None)
        return True

    def indexes(self) -> tuple[IndexStore, ...]:
        """Return every registered index, in the order names sort, so a report is reproducible."""
        return tuple(self._indexes[key] for key in sorted(self._indexes))

    def _registered_artifact_nonces(self) -> frozenset[int]:
        """Project physical identities without reopening catalog-v2 generations.

        A non-zero nonce in the registered definition is the physical identity already proved
        when that registry object was admitted.  Legacy definitions do not carry that identity,
        so their header remains the only conservative source and is opened through the existing
        validating door.  Zero is never an occupied generation identity.
        """

        occupied: set[int] = set()
        for index in self.indexes():
            nonce = index.definition.artifact_nonce
            if nonce == 0:
                nonce = index.open().artifact_nonce
            if nonce != 0:
                occupied.add(nonce)
        return frozenset(occupied)

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

    def _catalog_active_definitions(
        self, catalog: object | None = None
    ) -> tuple[IndexDefinition, ...] | None:
        """Return the catalog's runtime projection, or ``None`` for a legacy test double.

        Production heaps always expose a concrete :class:`Catalog`, whose projection is the
        authority.  A few component collaborators intentionally implement only the old heap
        surface; retaining the raw-registry fallback for those doubles does not weaken a real
        database because the fallback is unreachable once a catalog object is present.
        """
        authority = catalog
        if authority is None:
            try:
                authority = self._heap.catalog.catalog
            except AttributeError:
                return None
        projection = getattr(authority, "active_index_definitions", None)
        if callable(projection):
            return tuple(projection())
        tables = getattr(authority, "tables", None)
        if not callable(tables):
            return None
        return tuple(
            definition
            for table in tables()
            for definition in automatic_index_definitions(table)
        )

    def _catalog_authority(self, catalog: object | None = None) -> object | None:
        """Return the selected catalog object without inventing one for narrow doubles."""
        if catalog is not None:
            return catalog
        try:
            return self._heap.catalog.catalog
        except AttributeError:
            return None

    def _definition_matches_table_tolerantly(
        self,
        definition: IndexDefinition,
        table: object,
        *,
        catalog: object | None = None,
    ) -> bool:
        """Validate one access path without deriving an invalid sibling accelerator."""

        if definition.table_id != getattr(
            table, "table_id", None
        ) or definition.table_name != getattr(table, "name", None):
            return False
        try:
            if isinstance(table, TableDef):
                return self._definition_match(definition, table)
            return index_definition_matches_table(  # type: ignore[arg-type]
                definition, table
            )
        except GrafxIndexError:
            # The legacy compositor constructs scalar and vector siblings together.  A legal
            # scalar access path must remain usable when only a derived vector name is too long,
            # so ask Catalog's per-path tolerant projection for the one expected name.
            authority = self._catalog_authority(catalog)
            projection = getattr(authority, "active_index_definitions_for", None)
            if callable(projection):
                expected = next(
                    (
                        candidate
                        for candidate in projection(
                            definition.table_id,
                            table_name=definition.table_name,
                        )
                        if candidate.registry_key == definition.registry_key
                    ),
                    None,
                )
                if expected is not None:
                    return (
                        type(definition) is type(expected)
                        and definition.name == expected.name
                        and definition.table_id == expected.table_id
                        and definition.table_name == expected.table_name
                        and definition.positions == expected.positions
                        and definition.visibility is expected.visibility
                        and definition.key_derivation == expected.key_derivation
                    )
            columns = getattr(table, "columns", None)
            if columns is None:
                return False
            stored_arity = len(columns) + (
                2 if getattr(table, "kind", None) == "rel" else 0
            )
            return all(position < stored_arity for position in definition.positions)

    def _legacy_active_indexes(self, catalog: object) -> tuple[IndexStore, ...]:
        """Preserve v1's valid process-local registry semantics.

        Catalog v1 predates persisted logical definitions.  Its schema can prove provenance but
        cannot distinguish an automatic index from a legitimate low-level custom registration.
        Consequently every registered definition that still matches a committed table remains
        eligible in v1; catalog v2 replaces this compatibility rule with exact generation
        authority.
        """
        try:
            tables = {(table.table_id, table.name): table for table in catalog.tables()}
        except (AttributeError, GrafxError):
            return ()
        return tuple(
            index
            for index in self.indexes()
            if (
                table := tables.get(
                    (index.definition.table_id, index.definition.table_name)
                )
            )
            is not None
            and self._definition_matches_table_tolerantly(
                index.definition, table, catalog=catalog
            )
        )

    def active_indexes(
        self, *, catalog: object | None = None
    ) -> tuple[IndexStore, ...]:
        """Return registered stores selected by the catalog's complete ACTIVE definitions.

        :meth:`indexes` remains the raw ownership registry used by DDL compensation.  This
        sibling is the committed-data facade: a same-name process-local object, an old physical
        nonce and BUILDING/STALE generations all fail the exact value comparison and therefore
        cannot become query, redo, verification or DML authority.  Passing an explicit catalog
        requests that exact snapshot.  Catalog v1 preserves its historical valid process-local
        registrations because it has no logical-definition records; strict generation authority
        begins only with v2.
        """
        authority = self._catalog_authority(catalog)
        if authority is not None and (
            getattr(authority, "format_version", None) == CATALOG_LEGACY_FORMAT_VERSION
        ):
            return self._legacy_active_indexes(authority)
        definitions = self._catalog_active_definitions(authority)
        if definitions is None:
            return self.indexes()
        selected: list[IndexStore] = []
        for definition in definitions:
            current = self._indexes.get(definition.registry_key)
            if current is not None and current.definition == definition:
                selected.append(current)
        return tuple(selected)

    def active_index(self, name: str, *, catalog: object | None = None) -> IndexStore:
        """Resolve one name only when its registered store is the catalog-selected generation."""
        if not isinstance(name, str):
            raise GrafxIndexError(
                f"An index is named by a string; got {type(name).__name__}.",
                field="name",
                value=type(name).__name__,
            )
        authority = self._catalog_authority(catalog)
        if authority is not None and (
            getattr(authority, "format_version", None) == CATALOG_LEGACY_FORMAT_VERSION
        ):
            current = self._indexes.get(name.lower())
            if current is not None:
                try:
                    table = authority.table_by_id(current.definition.table_id)
                    matches = self._definition_matches_table_tolerantly(
                        current.definition, table, catalog=authority
                    )
                except (AttributeError, GrafxError):
                    matches = False
                if matches:
                    return current
            raise GrafxIndexError(
                f"No committed-table index named {name!r} is registered in this v1 catalog.",
                field="index_authority",
                value=name,
                index=name,
                registered=current is not None,
            )
        key = name.lower()
        if authority is not None and (
            getattr(authority, "format_version", None) != CATALOG_LEGACY_FORMAT_VERSION
        ):
            logical_lookup = getattr(authority, "index_definition", None)
            table_projection = getattr(authority, "active_index_definitions_for", None)
            if callable(logical_lookup) and callable(table_projection):
                current = self._indexes.get(key)
                expected: IndexDefinition | None = None
                try:
                    logical = logical_lookup(name)
                except GrafxConfigurationError:
                    # Specialized proximity indexes are deliberately schema-derived in v2.
                    # The registered definition identifies the sole table bucket worth asking;
                    # exact process-local impostors still fail complete value equality below.
                    if current is not None:
                        definitions = tuple(
                            table_projection(
                                current.definition.table_id,
                                table_name=current.definition.table_name,
                            )
                        )
                        expected = next(
                            (
                                definition
                                for definition in definitions
                                if definition.registry_key == key
                            ),
                            None,
                        )
                else:
                    generation = logical.active_generation()
                    if generation is not None:
                        expected = logical.runtime_definition(generation)
                if (
                    expected is None
                    or current is None
                    or current.definition != expected
                ):
                    raise GrafxIndexError(
                        f"No catalog-selected ACTIVE index named {name!r} is registered with "
                        "its exact physical definition.",
                        field="index_authority",
                        value=name,
                        index=name,
                        registered=current is not None,
                    )
                return current
        definitions = self._catalog_active_definitions(authority)
        if definitions is None:
            return self.index(name)
        expected = next(
            (
                definition
                for definition in definitions
                if definition.registry_key == key
            ),
            None,
        )
        current = self._indexes.get(key)
        if expected is None or current is None or current.definition != expected:
            raise GrafxIndexError(
                f"No catalog-selected ACTIVE index named {name!r} is registered with its "
                "exact physical definition.",
                field="index_authority",
                value=name,
                index=name,
                registered=current is not None,
            )
        return current

    def _transaction_indexes(
        self, txn: StagingTransaction, *, catalog: object | None = None
    ) -> tuple[IndexStore, ...]:
        """Return ACTIVE stores plus speculative stores explicitly observed by the transaction."""
        _committed, selected = self._statement_indexes(txn=txn, catalog=catalog)
        return selected

    def _statement_indexes(
        self,
        *,
        txn: StagingTransaction | None = None,
        catalog: object | None = None,
    ) -> tuple[tuple[IndexStore, ...], tuple[IndexStore, ...]]:
        """Project committed and transaction-visible stores with one authority walk.

        Query planning consumes only the first tuple. Runtime DML also sees stores this
        transaction created through its schema journal, supplied by the second tuple.
        """
        active = self.active_indexes(catalog=catalog)
        if txn is None:
            return active, active
        txn_id = getattr(txn, "txn_id", None)
        if isinstance(txn_id, bool) or not isinstance(txn_id, int) or txn_id < 0:
            raise GrafxIndexError(
                "Transaction-scoped index authority needs a non-negative integer txn_id.",
                field="txn_id",
                value=repr(txn_id),
            )
        observed = tuple(self._schema_observed.get(txn_id, ()))
        return active, tuple(dict.fromkeys((*active, *observed)))

    def indexes_for(
        self,
        table_id: int,
        *,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[IndexStore, ...]:
        """Return registered indexes owned by this complete table identity.

        ``table_id`` alone remains the compatibility shape for component callers.  The commit
        path also supplies the case-sensitive catalog name because concurrent speculative and
        durable DDL can legitimately allocate the same numeric id from the same snapshot.
        """
        return tuple(
            index
            for index in self._registered_indexes_for(table_id, table_name=table_name)
            if (
                table is None
                or not hasattr(table, "columns")
                or self._definition_matches_table_tolerantly(index.definition, table)
            )
        )

    def active_indexes_for(
        self,
        table_id: int,
        *,
        table_name: str | None = None,
        table: object | None = None,
        txn: StagingTransaction | None = None,
        catalog: object | None = None,
    ) -> tuple[IndexStore, ...]:
        """Return authoritative indexes owned by one complete table identity.

        Supplying ``txn`` adds only stores that transaction previously observed through the DDL
        journal.  This narrow exception preserves CREATE-TABLE-plus-DML in one transaction
        without granting another process-local registration committed authority.
        """

        def belongs_to_requested_table(index: IndexStore) -> bool:
            definition = index.definition
            if definition.table_id != table_id or (
                table_name is not None and definition.table_name != table_name
            ):
                return False
            return (
                table is None
                or not hasattr(table, "columns")
                or self._definition_matches_table_tolerantly(
                    definition, table, catalog=authority
                )
            )

        authority = self._catalog_authority(catalog)
        is_v1 = authority is not None and (
            getattr(authority, "format_version", None) == CATALOG_LEGACY_FORMAT_VERSION
        )
        table_projection = getattr(authority, "active_index_definitions_for", None)
        if not is_v1 and callable(table_projection):
            definitions = tuple(table_projection(table_id, table_name=table_name))
            committed = tuple(
                current
                for definition in definitions
                if (current := self._indexes.get(definition.registry_key)) is not None
                and current.definition == definition
                and belongs_to_requested_table(current)
            )
        elif is_v1:
            # v1 has no persisted logical authority: every valid process-local access path for
            # the committed table remains eligible, including with an explicit catalog photo.
            # Resolve only that table's raw ownership bucket; calling active_indexes() here
            # would reintroduce an O(total_indexes) projection for every row.
            try:
                catalog_table = authority.table_by_id(table_id)
            except (AttributeError, GrafxError):
                committed = ()
            else:
                if table_name is not None and catalog_table.name != table_name:
                    committed = ()
                else:
                    committed = tuple(
                        index
                        for index in self._registered_indexes_for(
                            table_id, table_name=catalog_table.name
                        )
                        if belongs_to_requested_table(index)
                        and self._definition_matches_table_tolerantly(
                            index.definition, catalog_table, catalog=authority
                        )
                    )
        else:
            # Narrow component doubles have no persisted authority surface.  Their historical
            # contract is the raw registry, now reached through the same table-local structure.
            committed = tuple(
                index
                for index in self._registered_indexes_for(
                    table_id, table_name=table_name
                )
                if belongs_to_requested_table(index)
            )

        if txn is None:
            return committed
        txn_id = getattr(txn, "txn_id", None)
        if isinstance(txn_id, bool) or not isinstance(txn_id, int) or txn_id < 0:
            raise GrafxIndexError(
                "Transaction-scoped index authority needs a non-negative integer txn_id.",
                field="txn_id",
                value=repr(txn_id),
            )
        observed = tuple(
            index
            for index in self._schema_observed.get(txn_id, ())
            if belongs_to_requested_table(index)
        )
        return tuple(dict.fromkeys((*committed, *observed)))

    def unregistered_persistent_indexes_for(
        self,
        tables: Sequence[object],
        *,
        catalog: object | None = None,
    ) -> tuple[str, ...]:
        """Name catalog-selected indexes of ``tables`` absent or physically mismatched.

        A long-lived participant can adopt a table committed by another process while its
        process-local index registry still predates that DDL.  In that state row materialisation
        is able to update the heap, but index staging has no object on which to record either the
        logical change or a durable stale verdict.  Recovery can then mistake the resulting WAL
        silence for a complete replay and certify a short index.

        This is the pre-WAL proof used by the transaction manager after its existing-only
        registry synchronisation.  In v1 only legacy automatic files that actually exist are
        obligations, preserving the established scan fallback.  In v2 every ACTIVE generation
        is an explicit catalog promise, so absence, unreadable bytes or any header/nonce mismatch
        is an obligation and blocks the write before WAL publication.
        """

        authority = catalog
        if authority is None:
            try:
                authority = self._heap.catalog.catalog
            except AttributeError:
                authority = None
        definitions = self._catalog_active_definitions(authority)
        if definitions is None:
            definitions = tuple(index.definition for index in self.indexes())
        identities = {
            (getattr(table, "table_id", None), getattr(table, "name", None))
            for table in tables
        }
        is_v2 = (
            getattr(authority, "format_version", CATALOG_LEGACY_FORMAT_VERSION)
            != CATALOG_LEGACY_FORMAT_VERSION
        )
        relevant: list[IndexDefinition] = []
        seen: set[str] = set()
        for definition in definitions:
            if (definition.table_id, definition.table_name) not in identities:
                continue
            file = definition.file
            if file in seen:
                continue
            seen.add(file)
            relevant.append(definition)

        unresolved = tuple(
            definition
            for definition in relevant
            if (
                (current := self._indexes.get(definition.registry_key)) is None
                or current.definition != definition
            )
        )
        if not unresolved:
            # Registry equality proves the logical staging target. Physical ownership is still
            # revalidated through the fresh certificate in validate_staged_artifacts before WAL;
            # repeating a directory inventory here adds no authority to that stronger proof.
            return ()

        # Materialise at most once, and only when an unresolved catalog promise requires the
        # legacy existence/mismatch classification below. Per-definition exists() calls would
        # retain the same directory-scan cost on local storage.
        persisted = frozenset(self._pool.storage.list_files(f"{INDEX_DIRECTORY}/"))
        missing: list[str] = []
        for definition in unresolved:
            file = definition.file
            strict_generation = is_v2 and definition.artifact_nonce != 0
            exists = file in persisted
            if not exists:
                if strict_generation:
                    missing.append(definition.name)
                continue
            try:
                page = self._pool.read_fresh_page(file, HEADER_PAGE_INDEX)
                header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
            except GrafxError:
                if strict_generation:
                    missing.append(definition.name)
                continue
            matches = (
                header.digest == definition.digest()
                and header.visibility is definition.visibility
                and header.table_id == definition.table_id
                and header.bucket_count == definition.bucket_count
                and (
                    not strict_generation
                    or header.artifact_nonce == definition.artifact_nonce
                )
            )
            if matches or strict_generation:
                missing.append(definition.name)
        return tuple(missing)

    # --- freshness --------------------------------------------------------------------------

    def _table_high_waters(self, indexes: Sequence[IndexStore]) -> dict[int, Lsn]:
        """Read each covered table's committed physical watermark exactly once."""
        high_waters: dict[int, Lsn] = {}
        for index in indexes:
            table_id = index.definition.table_id
            if table_id in high_waters:
                continue
            table = self._heap.catalog.catalog.table_by_id(table_id)
            high_waters[table_id] = self._heap.committed_high_water(table)
        return high_waters

    def table_watermark_photo(self) -> dict[int, Lsn]:
        """Photograph each covered table's committed watermark once, for one recovery holder.

        Valid only while the photographer holds the section and applies nothing: a caller that
        replays pages or adopts a catalog re-photographs before asking again, and the boot-time
        :meth:`open`, which runs after the section is released into the regime where foreign
        commits move the heap, always takes its own (ST-7).
        """
        return self._table_high_waters(self.active_indexes())

    def _replace_table_watermarks(self, high_waters: Mapping[int, Lsn]) -> None:
        """Bind every index to one open-time physical table-watermark picture."""
        self._table_watermarks = dict(high_waters)
        for index in self.active_indexes():
            index._table_high_water = high_waters.get(index.definition.table_id)

    def _record_table_watermark(
        self, table_id: int, lsn: Lsn, *, table_name: str | None = None
    ) -> None:
        """Advance one locally observed table floor and bind all of its indexes to it."""
        position = max(self._table_watermarks.get(table_id, NO_LSN), lsn)
        self._table_watermarks[table_id] = position
        for index in self.active_indexes_for(table_id, table_name=table_name):
            index._table_high_water = position

    @property
    def published_lsn(self) -> Lsn:
        """Return the log position this manager was last told the database had published."""
        return self._published_lsn

    def observe_published_lsn(self, published_lsn: Lsn) -> None:
        """Advance the registry's publication ceiling without certifying any index.

        Dynamic registration happens after a transaction manager has selected a newer durable
        cross-process view.  ``register`` must judge a newly adopted file against that view, not
        the position this registry happened to see when the process opened.  Merely recording the
        ceiling grants no freshness: each registration still reads its table watermark and index
        header, and :meth:`open` remains the door that validates the complete inventory.
        """

        published = _require_position("published_lsn", published_lsn)
        if published < self._published_lsn:
            raise GrafxIndexError(
                f"The index registry has already observed published position "
                f"{self._published_lsn}, so it cannot move back to {published}.",
                field="published_lsn",
                value=published,
                observed=self._published_lsn,
            )
        self._published_lsn = published

    def open(
        self,
        published_lsn: Lsn,
        *,
        persist_stale: bool = True,
        allow_ahead: bool = False,
        catalog: object | None = None,
    ) -> tuple[IndexStore, ...]:
        """Check every registered index against the position the database has published.

        Returns the indexes that are stale, which is what a caller needs in order to decide
        between rebuilding them and running without them. Nothing is repaired here: a repair
        writes to the log and therefore belongs inside a transaction the caller owns.
        """
        published = _require_position("published_lsn", published_lsn)
        indexes = self.active_indexes(catalog=catalog)
        high_waters = self._table_high_waters(indexes)
        for index in indexes:
            required = high_waters[index.definition.table_id]
            if required > published:
                table = self._heap.catalog.catalog.table_by_id(
                    index.definition.table_id
                )
                raise GrafxCorruptionDetected(
                    f"Table {table.name!r} contains committed physical state through "
                    f"position {required}, ahead of the database's published position "
                    f"{published}.",
                    file=self._heap.file,
                    table=table.name,
                    table_id=table.table_id,
                    index=index.name,
                    field="table_high_water",
                    value=required,
                    published_lsn=published,
                )
        self._replace_table_watermarks(high_waters)
        self._published_lsn = published
        return tuple(
            index
            for index in indexes
            if index.check_freshness(
                self._published_lsn,
                required_lsn=high_waters[index.definition.table_id],
                persist=persist_stale,
                allow_ahead=allow_ahead,
            )
        )

    def check_replay_floor(
        self,
        checkpoint_lsn: Lsn,
        *,
        persist_stale: bool = True,
        watermarks: Mapping[int, Lsn] | None = None,
    ) -> tuple[IndexStore, ...]:
        """Flag indexes behind a WAL replay floor without changing the published ceiling.

        Recovery may only replay records above ``checkpoint_lsn``. An index below that floor is
        irreparable from the retained suffix, while one above it is expected and must not be
        called stale yet. Unlike :meth:`open`, this diagnostic deliberately leaves
        ``published_lsn`` untouched so a refused operator recovery cannot regress the state of a
        live handle.

        ``watermarks`` lets one holder of the recovery section reuse a photo it took itself
        (:meth:`table_watermark_photo`) instead of walking the heap again for the same still
        picture. A table the photo does not name -- newly adopted between the photo and this
        pass -- is read fresh here, never guessed at (ST-7).
        """
        floor = _require_position("checkpoint_lsn", checkpoint_lsn)
        indexes = self.active_indexes()
        if watermarks is None:
            high_waters = self._table_high_waters(indexes)
        else:
            high_waters = dict(watermarks)
            for index in indexes:
                table_id = index.definition.table_id
                if table_id not in high_waters:
                    table = self._heap.catalog.catalog.table_by_id(table_id)
                    high_waters[table_id] = self._heap.committed_high_water(table)
        return tuple(
            index
            for index in indexes
            if index.check_freshness(
                floor,
                required_lsn=min(high_waters[index.definition.table_id], floor),
                persist=persist_stale,
                allow_ahead=True,
            )
        )

    def mark_built_through(
        self, lsn: Lsn, *, watermarks: Mapping[int, Lsn] | None = None
    ) -> None:
        """Declare a completed replay, writing only the headers that need the declaration.

        The caller must have replayed the whole log, or written every record itself. It exists
        because recovery knows something no index can: that the replay it just finished was
        complete. Without it, an index that no record of the final commits happened to touch
        would be indistinguishable from one that missed them, and every open after a crash would
        rebuild indexes that were never behind.

        What the declaration certifies is completeness FOR EACH TABLE: :meth:`open` requires an
        index to cover its table's committed high water, never a global number. So an index whose
        fresh page-0 certificate already covers that high water, with nothing unflushed behind it
        and no stale, claim or rebuild transition in play, is left exactly as it stands -- the
        skip never writes, so it can never invent freshness (ST-7, reopening R3-do-plano; the
        earlier semantics rewrote and flushed every header to the global position on every
        open). Every state this door cannot vouch for takes the full advance as before. The
        published position still rises for the manager as a whole.

        Call it BEFORE :meth:`open`. An index already marked stale refuses to move the position
        it claims -- that is what stops a broken index looking fresh again -- so declaring a
        replay complete afterwards does not clear the mark, and the index goes to a rebuild it
        did not need. That ordering costs work and never a wrong answer, and it is the right way
        round: only :meth:`clear_stale`, which names the rebuild that repaired it, takes an index
        out of the stale state.
        """
        position = _require_position("lsn", lsn)
        indexes = self.active_indexes()
        photo = (
            dict(watermarks)
            if watermarks is not None
            else self._table_high_waters(indexes)
        )
        # The same photograph that justifies leaving a per-table-complete header alone is the
        # read floor future snapshots must use.  Without this binding the header remains at the
        # table high-water while reads still demand the global clock, falsely refusing a complete
        # index after an index-only commit (ST-7 integration regression).
        self._replace_table_watermarks(photo)
        for index in indexes:
            index.complete_built_through(position, photo.get(index.definition.table_id))
            self._bind_local_heap_view(index)
        self._published_lsn = max(self._published_lsn, position)

    def mark_all_stale(self, reason: str, *, persist: bool = False) -> None:
        """Exclude every registered index after a redo whose completed prefix is unknown.

        A page replay can fail before logical-index replay begins. In that shape no individual
        index operation gets the chance to mark itself, yet a later unrelated commit could
        flush the recovered heap pages and advance every untouched index past the missing
        entries. Excluding the registry as one unit prevents that false certification. The
        default is in-memory only: the WAL is retained and the failed pass may have refused
        before its first mutation, so poisoning this handle must not invent another disk write.
        """
        for index in self.active_indexes():
            index.mark_stale(reason, persist=persist)

    def validate_staged_records(
        self, txn: StagingTransaction, records: Sequence[object]
    ) -> None:
        """Prove that every staged WAL effect is owned by this registry's staging state.

        ``TransactionContext.pending_records`` is reachable to callers because indexes stage
        through that protocol. A caller must not be able to inject an ABORT/outcome record or an
        extra logical change that is replayed after restart but was never applied on the live
        commit path. The decoded multiset must exactly match the changes held by the registered
        indexes for this transaction.
        """
        authorised = {
            index.definition.registry_key: index
            for index in self._transaction_indexes(txn)
        }
        actual: list[IndexChange] = []
        for position, record in enumerate(records):
            if not isinstance(record, WalRecord):
                raise GrafxIndexError(
                    "A staged index effect must be a concrete WalRecord.",
                    field="pending_records",
                    position=position,
                    value=type(record).__name__,
                )
            if record.lsn != NO_LSN or record.txn_id != txn.txn_id:
                raise GrafxIndexError(
                    "A staged index effect must be unnumbered and belong to its transaction.",
                    field="pending_records",
                    position=position,
                    lsn=record.lsn,
                    record_txn_id=record.txn_id,
                    txn_id=txn.txn_id,
                )
            try:
                change = change_of(record)
            except GrafxError as failure:
                raise GrafxIndexError(
                    "A staged index effect does not carry a valid logical index change.",
                    field="pending_records",
                    position=position,
                ) from failure
            if change.index.lower() not in authorised:
                raise GrafxIndexError(
                    f"A staged effect names index {change.index!r} without ACTIVE catalog "
                    "authority for this transaction.",
                    field="index",
                    index=change.index,
                    position=position,
                )
            actual.append(change)

        expected = [
            change
            for index in self._transaction_indexes(txn)
            for change in index.pending(txn)
        ]
        expected_counts = Counter(expected)
        actual_counts = Counter(actual)
        unexpected = actual_counts - expected_counts
        if unexpected:
            change = next(iter(unexpected))
            raise GrafxIndexError(
                "A staged WAL effect has no matching change in the index registry.",
                field="pending_records",
                index=change.index,
                operation=change.operation.name,
            )
        missing = expected_counts - actual_counts
        if missing:
            raise GrafxIndexError(
                "The transaction's staged WAL effects do not exactly match the index registry.",
                field="pending_records",
                expected=len(expected),
                actual=len(actual),
                missing=missing.total(),
            )

    def retarget_staged(
        self, txn: StagingTransaction, old_csn: Csn, new_csn: Csn
    ) -> tuple[WalRecord, ...]:
        """Atomically replace a predicted CSN in this transaction's staged index effects.

        Segment creation can add a WAL header between a transaction's first prediction and the
        exact terminal LSN planned for its batch.  Index effects are logical WAL records, so
        their stamps must move with the heap page images before append.  Every old record and
        every replacement is proved first; only then are the registry staging lists and the
        transaction's public record list replaced in place.  Unversioned inserts keep
        ``NO_CSN`` because zero never matches a usable ``old_csn``.
        """
        old = _require_retarget_csn("old_csn", old_csn)
        new = _require_retarget_csn("new_csn", new_csn)
        pending = getattr(txn, "pending_records", None)
        if type(pending) is not list:
            raise GrafxIndexError(
                "Retargeting staged index effects needs the transaction's concrete mutable "
                "pending_records list.",
                field="pending_records",
                value=type(pending).__name__,
            )

        # This validates the complete multiset before any staging state moves.  In particular,
        # a caller-replaced record cannot be blessed merely because its index name is known.
        self.validate_staged_records(txn, tuple(pending))

        staged_replacements: list[tuple[_Staged, list[IndexChange]]] = []
        expected: list[IndexChange] = []
        txn_id = int(txn.txn_id)
        for index in self._transaction_indexes(txn):
            staged = index._staged.get(txn_id)
            if staged is None:
                continue
            changes = [_retarget_change(change, old, new) for change in staged.changes]
            staged_replacements.append((staged, changes))
            expected.extend(changes)

        records: list[WalRecord] = []
        actual: list[IndexChange] = []
        for record in pending:
            # validate_staged_records already established this concrete type; retaining the
            # guard here keeps this method locally total if that validator is ever generalized.
            if not isinstance(record, WalRecord):  # pragma: no cover - guarded above
                raise GrafxIndexError(
                    "A retargeted index effect must be a concrete WalRecord.",
                    field="pending_records",
                    value=type(record).__name__,
                )
            original_change = change_of(record)
            change = _retarget_change(original_change, old, new)
            actual.append(change)
            records.append(
                record
                if change is original_change
                else replace(record, payload=change.encode())
            )

        if Counter(actual) != Counter(expected):  # pragma: no cover - guarded above
            raise GrafxIndexError(
                "Retargeting changed the transaction and registry into different effects.",
                field="pending_records",
                expected=len(expected),
                actual=len(actual),
            )

        original_records = list(pending)
        original_staging = [
            (staged, list(staged.changes)) for staged, _ in staged_replacements
        ]
        try:
            for staged, changes in staged_replacements:
                staged.changes[:] = changes
            pending[:] = records
        except BaseException:
            for staged, changes in original_staging:
                staged.changes[:] = changes
            pending[:] = original_records
            raise
        return tuple(records)

    # --- staging ----------------------------------------------------------------------------

    def row_entry_count(
        self,
        table_id: int,
        values: Sequence[object],
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
        txn: StagingTransaction | None = None,
    ) -> int:
        """Count entries this row owes without deriving or hashing value keys.

        ``record_id`` is optional only for compatibility with column-derived indexes, whose
        inclusion predicate ignores it.  A RecordId-derived index validates that the durable
        logical identity is present here, keeping WAL quota pre-counting on exactly the same
        domain as staging without trying to encode an empty DELETE value tuple.
        """

        return sum(
            1
            for index in self.active_indexes_for(
                table_id,
                table_name=table_name,
                table=table,
                txn=txn,
            )
            if index.definition.owes_entry_for_record(record_id, values)
        )

    def stage_row_insert(
        self,
        txn: StagingTransaction,
        table_id: int,
        ref: RecordRef,
        values: Sequence[object],
        csn: Csn,
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[WalRecord, ...]:
        """Stage, on every index of the table, the entry this new row version owes it."""
        records: list[WalRecord] = []
        for index in self.active_indexes_for(
            table_id, table_name=table_name, table=table, txn=txn
        ):
            definition = index.definition
            if not definition.owes_entry_for_record(record_id, values):
                index.stage_empty_observation(txn)
            else:
                records.append(
                    index.stage_insert(
                        txn,
                        definition.key_for_record(record_id, values),
                        ref,
                        csn,
                    )
                )
        return tuple(records)

    def stage_row_delete(
        self,
        txn: StagingTransaction,
        table_id: int,
        ref: RecordRef,
        values: Sequence[object],
        csn: Csn,
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[WalRecord, ...]:
        """Stage, on every index of the table, the end of the entry this row version had."""
        records: list[WalRecord] = []
        for index in self.active_indexes_for(
            table_id, table_name=table_name, table=table, txn=txn
        ):
            definition = index.definition
            if not definition.owes_entry_for_record(record_id, values):
                index.stage_empty_observation(txn)
            else:
                records.append(
                    index.stage_delete(
                        txn,
                        definition.key_for_record(record_id, values),
                        ref,
                        csn,
                    )
                )
        return tuple(records)

    def stage_row_update(
        self,
        txn: StagingTransaction,
        table_id: int,
        old_ref: RecordRef,
        old_values: Sequence[object],
        new_ref: RecordRef,
        new_values: Sequence[object],
        csn: Csn,
        *,
        record_id: object | None = None,
        table_name: str | None = None,
        table: object | None = None,
    ) -> tuple[WalRecord, ...]:
        """Stage both halves of an update on every index of the table.

        Both halves, always, even when the key did not change. An update writes a NEW version at
        a NEW location and ends the old one, so the entry that pointed at the old version has to
        be ended and an entry for the new one created; leaving the old entry alone because the
        key looks the same would leave the index pointing at a version the row no longer has.
        """
        records: list[WalRecord] = []
        for index in self.active_indexes_for(
            table_id, table_name=table_name, table=table, txn=txn
        ):
            definition = index.definition
            owes_old = definition.owes_entry_for_record(record_id, old_values)
            owes_new = definition.owes_entry_for_record(record_id, new_values)
            if not owes_old and not owes_new:
                index.stage_empty_observation(txn)
            elif owes_old:
                records.append(
                    index.stage_delete(
                        txn,
                        definition.key_for_record(record_id, old_values),
                        old_ref,
                        csn,
                    )
                )
            if owes_new:
                records.append(
                    index.stage_insert(
                        txn,
                        definition.key_for_record(record_id, new_values),
                        new_ref,
                        csn,
                    )
                )
        return tuple(records)

    def _commit_under_write_authority(
        self, txn: StagingTransaction, csn: Csn
    ) -> int:
        """Commit through the private door owned by the fully fenced transaction path.

        The surrounding transaction manager already holds its participant section, writer lease
        and cross-process ``COMMIT_SECTION``. A context-local seal carries only that authority
        through ordinary/custom ``commit`` overrides; it cannot leak to a direct low-level call
        in another thread or survive success/failure.
        """
        authority = _LiveCommitAuthority(
            seal=_LIVE_COMMIT_AUTHORITY_SEAL,
            manager=self,
            txn=txn,
        )
        token = _LIVE_COMMIT_AUTHORITY.set(authority)
        try:
            return self.commit(txn, csn)
        finally:
            # Context copies retain this same scope object. Revoke it before resetting the
            # current context so no delayed task can inherit a still-valid capability.
            authority.active = False
            authority.store = None
            _LIVE_COMMIT_AUTHORITY.reset(token)

    def commit(self, txn: StagingTransaction, csn: Csn) -> int:
        """Apply, on every index, what this transaction staged, and return how many changes moved.

        This is the one call the transaction manager owes the index framework, and it belongs
        AFTER the log barrier of CONTRACT.md section 8.5 step 3.5 and beside the page images of
        step 6: the commit number it is given is the number the log assigned, and nothing has
        touched an index page before it.
        """
        applied = 0
        touched: list[str] = []
        written_tables = _tables_written_by(txn)
        observed_tables: set[_TableIdentity] = set(written_tables or ())
        indexes = self._transaction_indexes(txn)
        new_table_observations = self._schema_new_table_observed.get(
            int(txn.txn_id), set()
        )
        # One transaction can hold the old committed generation and its detached replacement
        # under the same logical name.  Object identity, not name, keeps their observations
        # distinct until the catalog atomically selects the shadow.
        observations = {index: index.observed(txn) for index in indexes}
        staged_tables = {
            index.definition.table_id for index in indexes if observations[index]
        }
        authority = _LIVE_COMMIT_AUTHORITY.get()
        authorised_scope = authority if (
            isinstance(authority, _LiveCommitAuthority)
            and authority.active
            and authority.seal is _LIVE_COMMIT_AUTHORITY_SEAL
            and authority.manager is self
            and authority.txn is txn
        ) else None
        for index in indexes:
            observed = observations[index]
            if authorised_scope is not None:
                authorised_scope.store = index
            try:
                moved = index.commit(txn, csn)
            finally:
                if authorised_scope is not None:
                    authorised_scope.store = None
            identity = (
                index.definition.table_id,
                index.definition.table_name,
            )
            if observed and (
                written_tables is None
                or identity in written_tables
                or index in new_table_observations
            ):
                # A schema-only observation proves the new artifact, not a heap mutation.
                # Treating it as a row-table high-water advanced every older sibling index to
                # the DDL commit without advancing its header, making a healthy PK unreadable
                # immediately after adding an identity generation to that table.  Concrete
                # transactions expose their exact written-table set; compatibility doubles
                # without it retain the former observation-based inference.
                observed_tables.add(identity)
            elif (written_tables is not None and identity in written_tables) or (
                written_tables is None and index.definition.table_id in staged_tables
            ):
                # A row intent without even an empty observation is a short index, not an
                # unrelated commit.  The table watermark below protects this handle and an
                # open-time heap scan protects a reopened one; the durable mark is what protects
                # a participant that already cached the older table watermark.  Every read takes
                # a fresh page-0 certificate, so it observes this refusal without IPC or a
                # database-wide page-0 fanout.
                # A generic staging double may not expose row intents. In that compatibility
                # shape, an observation on a sibling index is evidence that THIS table was
                # processed; no observation anywhere is evidence of no index work, not of a
                # hidden heap mutation.
                index.mark_stale(
                    f"Commit {csn} wrote table {index.definition.table_id}, but index "
                    f"{index.name!r} staged no row observation and may omit a visible row."
                )
            if moved:
                applied += moved
                touched.append(index.file)
        for table_id, table_name in observed_tables:
            self._record_table_watermark(table_id, csn, table_name=table_name)
        for file in touched:
            # A page applied into this process's pool is invisible to every other process until
            # it reaches the device, and the commit is about to publish a position that says the
            # entry is there. It is a flush and not a barrier, for the reason section 8.5 step 6
            # gives: the log is the authority on durability and the redo is idempotent.
            self._pool.flush(file)
        # These certificates name images published by THIS pool. Binding them does not discard
        # the dirty/current heap frames that supplied the same commit. A later foreign page-0
        # generation differs and takes the zero-write rebase path before validation.
        for index in indexes:
            self._bind_local_heap_view(index)
        self._schema_observed.pop(int(txn.txn_id), None)
        self._schema_new_table_observed.pop(int(txn.txn_id), None)
        return applied

    def rollback(self, txn: StagingTransaction) -> int:
        """Drop what this transaction staged into every index, and return how many were dropped."""
        dropped = sum(index.rollback(txn) for index in self.indexes())
        self._schema_observed.pop(int(txn.txn_id), None)
        self._schema_new_table_observed.pop(int(txn.txn_id), None)
        return dropped

    def apply(self, record: WalRecord) -> bool:
        """Redo one index record against the index it names, and say whether it was dispatched.

        A record for an index this database has not registered is NOT an error: recovery replays
        the whole log, and an index may have been dropped since the record was written. The
        answer says so rather than raising, and the caller decides.
        """
        change = change_of(record)
        try:
            found = self.active_index(change.index)
        except GrafxIndexError:
            return False
        found.apply(record)
        return True

    def _prepare_common_replay_batch(
        self, records: Sequence[WalRecord]
    ) -> _CommonReplayBatch | None:
        """Preflight the narrow common index-only replay path without mutating an index.

        The returned value is deliberately opaque outside this module and belongs to exactly
        this call.  It is never cached on the manager or a store.  A caller gets ``None`` for any
        shape whose existing per-record protocol carries extra semantics: RESET, an active
        rebuild, or a subclass such as ``VectorHnswIndex`` that overrides :meth:`IndexStore.apply`.

        Every record is decoded and resolved before page 0 of the first store is seeded.  Header
        transitions are then composed in WAL order with the same monotonic value objects used by
        the legacy path.  Consequently a bad position or horizon discovered at the end of the
        batch refuses before the first bucket mutation.
        """
        if not records:
            return None
        items: list[_CommonReplayItem] = []
        ordered_stores: list[IndexStore] = []
        seen: set[IndexStore] = set()
        for record in records:
            change = change_of(record)
            if change.operation is IndexOperation.RESET:
                return None
            try:
                store = self.active_index(change.index)
            except GrafxIndexError:
                # CommitRedo's mandatory preflight normally turns this into its more useful
                # recovery-level refusal.  Retain the legacy dispatch result if a custom caller
                # invokes the capability directly.
                return None
            if (
                type(store).apply is not IndexStore.apply
                or store._rebuild_authority is not None
                or store._replaying
            ):
                return None
            if change.versioned != store.definition.versioned:
                raise GrafxCorruptionDetected(
                    f"A record for index {change.index!r} describes a "
                    f"{'versioned' if change.versioned else 'unversioned'} entry and this index "
                    f"stores {'versioned' if store.definition.versioned else 'unversioned'} "
                    "ones, so batched replay was refused before any effect was applied.",
                    field="versioned",
                    value=change.versioned,
                    index=change.index,
                    file=store.file,
                    operation=change.operation.name,
                    lsn=record.lsn,
                )
            if len(change.key) > store.max_key_bytes:
                raise GrafxCorruptionDetected(
                    f"A record for index {change.index!r} carries a key of "
                    f"{len(change.key)} bytes, but this index stores at most "
                    f"{store.max_key_bytes}; batched replay was refused before any effect was "
                    "applied.",
                    field="key",
                    value=len(change.key),
                    limit=store.max_key_bytes,
                    index=change.index,
                    file=store.file,
                    operation=change.operation.name,
                    lsn=record.lsn,
                )
            item = _CommonReplayItem(
                store,
                change,
                lsn_of(record),
                bucket_of(change.key, store.definition.bucket_count),
            )
            items.append(item)
            if store not in seen:
                seen.add(store)
                ordered_stores.append(store)

        initial_by_store: dict[IndexStore, IndexHeader] = {}
        final_by_store: dict[IndexStore, IndexHeader] = {}
        for store in ordered_stores:
            # A process-local stale verdict makes legacy _advance return before reading page 0;
            # preserve that ordering and do not introduce a new fallible read. A durable STALE
            # bit not reflected in this handle is still an ordinary immutable header value here:
            # legacy replay advances/reconciles it without clearing the bit, which composition
            # below reproduces exactly. Active rebuild authority was rejected above.
            if store._stale_reason is not None:
                return None
            header = store._read_header()
            initial_by_store[store] = header
            final_by_store[store] = header

        for item in items:
            final = final_by_store[item.store].advanced_to(item.position)
            if item.change.operation is IndexOperation.REMOVE:
                final = final.reconciled_to(item.change.csn)
            final_by_store[item.store] = final

        bucket_counts: Counter[tuple[IndexStore, int]] = Counter()
        bounded_bucket_census = True
        for item in items:
            identity = (item.store, item.bucket)
            if (
                identity not in bucket_counts
                and len(bucket_counts) >= _COMMON_REPLAY_HOT_BUCKET_LIMIT
            ):
                # Counting an unbounded number of cold buckets would make the accelerator's
                # metadata grow with the WAL.  Decline the whole directory optimization while
                # retaining the already-prepared common header batch and its scalar semantics.
                bucket_counts.clear()
                bounded_bucket_census = False
                break
            bucket_counts[identity] += 1
        bucket_targets: dict[tuple[IndexStore, int], set[tuple[bytes, RecordRef]]] = {}
        declined_buckets: set[tuple[IndexStore, int]] = set()
        retained_targets = 0
        for item in items if bounded_bucket_census else ():
            identity = (item.store, item.bucket)
            if (
                bucket_counts[identity] < _COMMON_REPLAY_HOT_BUCKET_MIN_EFFECTS
                or identity in declined_buckets
            ):
                continue
            targets = bucket_targets.setdefault(identity, set())
            target = (item.change.key, item.change.ref)
            if target in targets:
                continue
            if retained_targets >= _COMMON_REPLAY_HOT_TARGET_LIMIT:
                retained_targets -= len(targets)
                bucket_targets.pop(identity)
                declined_buckets.add(identity)
                continue
            targets.add(target)
            retained_targets += 1

        hot_buckets: dict[
            tuple[IndexStore, int], _CommonReplayHotBucket
        ] = {}
        remaining_pages = _COMMON_REPLAY_HOT_PAGE_LIMIT
        for identity, targets in bucket_targets.items():
            store, bucket = identity
            prepared_bucket = store._prepare_common_replay_hot_bucket(
                bucket,
                targets,
                page_limit=remaining_pages,
            )
            if prepared_bucket is not None:
                hot_buckets[identity] = prepared_bucket
                remaining_pages -= len(prepared_bucket.pages.pages)
        stores = tuple(
            _CommonReplayStore(store, initial_by_store[store], final_by_store[store])
            for store in ordered_stores
        )
        return _CommonReplayBatch(self, tuple(items), stores, hot_buckets)

    def apply_common_replay_batch(
        self, records: Sequence[WalRecord]
    ) -> tuple[str, ...] | None:
        """Try one common replay as a private, single-use plan.

        ``None`` asks the caller to dispatch the *whole* replay through the legacy per-record
        protocol.  Keeping preparation and application inside one call prevents a captured
        header image from being retained and replayed after a later generation or STALE mark.

        The production caller reaches this door during committed redo while holding its local
        participant section and the cross-process ``COMMIT_SECTION``.  Readers only pin these
        pages; every local or foreign page mutation needs those writer sections.  A prepared
        capacity/location therefore cannot change before this call finishes, and the ephemeral
        directory needs neither persistence nor another concurrency premise.
        """
        prepared = self._prepare_common_replay_batch(records)
        if prepared is None:
            return None
        moved_by_store: dict[IndexStore, bool] = {
            state.store: False for state in prepared.stores
        }
        touched: list[IndexStore] = []
        touched_set: set[IndexStore] = set()
        try:
            for item in prepared.items:
                store = item.store
                if store not in touched_set:
                    touched_set.add(store)
                    touched.append(store)
                store._replaying = True
                try:
                    hot_bucket = prepared.hot_buckets.get((store, item.bucket))
                    item_moved = (
                        store._apply_change(item.change, item.position)
                        if hot_bucket is None
                        else store._apply_common_replay_hot_change(
                            hot_bucket, item.change, item.position
                        )
                    )
                finally:
                    store._replaying = False
                moved_by_store[store] = moved_by_store[store] or item_moved

            for state in prepared.stores:
                store_moved = moved_by_store[state.store]
                if state.final != state.initial or store_moved:
                    # One composed header image is both the monotonic built/reconciled advance
                    # and, when a bucket moved, the final page-0 clock the caller's existing
                    # flush boundary publishes after those buckets.
                    state.store._write_header(state.final)
                    if store_moved:
                        state.store._cache_certificate = None
            return tuple(store.file for store in touched)
        except Exception as failure:
            for store in touched:
                try:
                    store._mark_stale_after_failure(
                        f"Batched logical replay into index {store.name!r} failed after a "
                        "possibly partial prefix, so the current handle cannot prove the index "
                        f"complete: {failure!r}"
                    )
                except Exception as stale_failure:  # noqa: BLE001 - preserve root failure
                    failure.add_note(
                        f"Marking touched index {store.name!r} stale also failed: "
                        f"{stale_failure!r}"
                    )
            raise

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
        index = self.active_index(name)
        if index.visibility is IndexVisibility.PROXIMITY:
            # Every registered index satisfies the protocol -- register() checks it -- so the
            # index decides its own answer here and the heap is deliberately not consulted.
            reader: SecondaryIndex = index
            return tuple(reader.lookup(key, snapshot))
        return self.validated(index, key, snapshot)

    def lookup_versions(
        self, name: str, key: bytes, snapshot: SnapshotLike
    ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
        """Return exact-index locations together with the heap versions that validated them.

        This is an engine-internal sibling of :meth:`lookup`, not a third visibility contract.
        An EXACT lookup already has to read each candidate while the index certificate is stable;
        returning that immutable version lets the query executor consume the proof instead of
        reading and decoding the same heap slot again outside the stable view.  A PROXIMITY index
        deliberately does not consult the heap, so it keeps using :meth:`lookup` and this door
        refuses that contract rather than silently changing it.
        """
        index = self.active_index(name)
        if index.visibility is IndexVisibility.PROXIMITY:
            raise GrafxIndexError(
                f"Index {index.name!r} has proximity visibility and does not validate heap "
                "versions during lookup.",
                field="visibility",
                value=index.visibility.value,
                index=index.name,
                file=index.file,
            )
        return self.validated_versions(index, key, snapshot)

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
        return self._validated_items(
            index,
            key,
            snapshot,
            project=lambda ref, _version: ref,
        )

    def validated_versions(
        self, index: IndexStore, key: bytes, snapshot: SnapshotLike
    ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
        """Return the exact candidates and the versions read while validating them.

        Keeping the pair inside one stable-view callback is the important part: the page-0
        certificate still brackets both index traversal and heap validation, and callers cannot
        accidentally turn one exact hit into two heap decodes.
        """
        return self._validated_items(
            index,
            key,
            snapshot,
            project=lambda ref, version: (ref, version),
        )

    def _validated_items(
        self,
        index: IndexStore,
        key: bytes,
        snapshot: SnapshotLike,
        *,
        project: Callable[[RecordRef, HeapVersion], _ReadResult],
    ) -> tuple[_ReadResult, ...]:
        """Validate exact candidates once and project each accepted heap proof."""
        read_lsn = index._require_exact_read_lsn(snapshot)
        definition = index.definition
        wanted = index._require_key(key)

        def confirm(
            certificate: _IndexReadCertificate,
        ) -> tuple[_ReadResult, ...]:
            """Validate candidates against heap frames bound to this index generation."""
            self._prepare_heap_view(index.file, certificate)
            confirmed: list[_ReadResult] = []
            for entry in index._candidates_unchecked(wanted):
                version = self._heap.read(entry.ref)
                if version.table_id != definition.table_id:
                    raise GrafxCorruptionDetected(
                        f"Index {definition.name!r} points at a row of table "
                        f"{version.table_id} and covers table {definition.table_id}.",
                        file=index.file,
                        page=entry.page,
                        slot=entry.slot,
                        index=definition.name,
                        field="table_id",
                    )
                if not snapshot.visible(version.xmin, version.xmax):
                    continue
                if (
                    definition.entry_key_for_record(version.record_id, version.values)
                    != entry.key
                ):
                    continue
                confirmed.append(project(entry.ref, version))
            return tuple(confirmed)

        return index._stable_view(read_lsn, confirm)

    def _prepare_heap_view(self, index_file: str, certificate: object) -> None:
        """Attach clean heap frames to the durable index generation validating them."""
        if not isinstance(certificate, _IndexReadCertificate):
            raise GrafxIndexError(
                "A companion heap view needs a durable index certificate.",
                field="certificate",
                value=type(certificate).__name__,
                file=index_file,
            )
        if self._heap_cache_certificates.get(index_file) == certificate:
            return
        local = next(
            (index for index in self._indexes.values() if index.file == index_file),
            None,
        )
        if local is not None and local._local_certificate == certificate:
            # The index generation and the heap frames were produced by this same pool. Dropping
            # dirty heap pages here would either lose local work or reject a valid direct index
            # commit; a later foreign sequence cannot equal this non-wrapping certificate.
            self._heap_cache_certificates[index_file] = certificate
            return
        # The heap participates in the same answer as the index traversal. Rebase it while the
        # surrounding pre/post certificate can still catch a commit that lands during the read.
        self._pool.discard_clean_file(self._heap.file)
        self._heap_cache_certificates[index_file] = certificate

    def _bind_local_heap_view(self, index: IndexStore) -> None:
        """Bind companion heap frames to an index image published by this same pool."""
        certificate = index._cache_certificate
        if certificate is not None:
            self._heap_cache_certificates[index.file] = certificate

    # --- maintenance ---------------------------------------------------------------------------

    def reconcile(
        self, horizon: Lsn, txn: StagingTransaction | None = None
    ) -> tuple[ReconcileReport, ...]:
        """Run a reconciliation pass over every registered index at this horizon.

        With a transaction the pass removes and logs; without one it measures. The argument order
        follows the index method it delegates to, which follows CONTRACT.md section 8.7.
        """
        return tuple(index.reconcile(horizon, txn) for index in self.active_indexes())

    def note_reconciled(self, horizon: Lsn) -> None:
        """Record on every index the horizon a completed reconciliation pass applied."""
        for index in self.active_indexes():
            index.note_reconciled(horizon)

    def rebuild(
        self,
        name: str,
        txn: StagingTransaction,
        through_lsn: Lsn,
        *,
        rebuild_token: int = 0,
        defer_clear: bool = False,
    ) -> int:
        """Re-derive every entry of an index from the heap, and return how many changes it staged.

        This is the repair of a stale index, and it is covered by the log like every other index
        change: the pass stages a reset followed by one insert per committed stored version and
        one tombstone per version that has a committed end. Abandoned provisional births are
        heap residue, not rows, and a provisional xmax is an abandoned end rather than a
        tombstone. A crash in the middle replays to the same place rather than leaving a
        half-built structure. The index stops being stale only when the transaction that carries
        these records commits.

        The cost is proportional to the table and the whole pass is one transaction, which is the
        honest bound for a reference implementation: a rebuild that spanned several commits would
        publish a partially-built index in between, and that is the state this method exists to
        get out of.
        """
        index = self.active_index(name)
        definition = index.definition
        table = self._heap.catalog.catalog.table_by_id(definition.table_id)
        position = _require_position("through_lsn", through_lsn)
        # RESET can expose an empty/partial index while its replacement entries are staged and
        # applied. Publish the refusal first, even when an operator proactively rebuilds a
        # healthy index; a crash then leaves a durable stale verdict, never a short answer.
        if not rebuild_token:
            # No token supplied: this call owns the claim, as it always did. A caller that
            # claimed under the commit section passes its token instead, because claiming
            # again here would move the generation a second time -- outside that section.
            rebuild_token = index._claim_rebuild(
                f"Index {index.name!r} is being rebuilt through position {position}."
            )
        staged = 1
        index.stage_reset(
            txn, position, rebuild_token=rebuild_token, defer_clear=defer_clear
        )
        for ref, version in self._heap.scan_all(table):
            if is_provisional_csn(version.xmin):
                continue
            key = definition.entry_key_for_record(version.record_id, version.values)
            if key is None:
                continue
            index.stage_insert(txn, key, ref, version.xmin)
            staged += 1
            if not is_open_end_csn(version.xmax):
                index.stage_delete(txn, key, ref, version.xmax)
                staged += 1
        return staged

    def _allocate_detached_generation_nonce(self, occupied: Collection[int]) -> int:
        """Return one provider nonce absent from catalog inventory and physical storage.

        This is discovery, not reservation.  The later detached build's exclusive create is the
        sole ownership proof; checking storage here only avoids predictably losing that race on
        an orphan already present.  A provider contract violation refuses immediately, while
        well-formed collisions are retried under a fixed bound.
        """
        if self._artifact_nonce is None:
            raise GrafxIndexError(
                "Allocating a detached index generation needs an artifact-nonce provider.",
                field="artifact_nonce",
                value=None,
            )
        try:
            occupied_nonces = frozenset(occupied)
        except TypeError as failure:
            raise GrafxIndexError(
                "Occupied artifact nonces must be a finite collection of unsigned integers.",
                field="occupied",
                value=type(occupied).__name__,
            ) from failure
        for nonce in occupied_nonces:
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or not 1 <= nonce <= PROVISIONAL_CSN
            ):
                raise GrafxIndexError(
                    "Occupied artifact nonces must be non-zero unsigned 64-bit integers.",
                    field="occupied",
                    value=repr(nonce),
                )

        for _attempt in range(_DETACHED_GENERATION_NONCE_ATTEMPTS):
            try:
                nonce = self._artifact_nonce()
            except StopIteration as failure:
                raise GrafxIndexError(
                    "The artifact-nonce provider was exhausted before producing an unused "
                    "generation.",
                    field="artifact_nonce",
                    retryable=True,
                ) from failure
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or not 1 <= nonce <= PROVISIONAL_CSN
            ):
                raise GrafxIndexError(
                    "The artifact-nonce provider returned a value outside non-zero u64.",
                    field="artifact_nonce",
                    value=repr(nonce),
                )
            if nonce in occupied_nonces:
                continue
            if self._pool.storage.exists(index_generation_file(nonce)):
                continue
            return nonce
        raise GrafxIndexError(
            "The artifact-nonce provider did not produce an unused generation within the "
            f"bounded {_DETACHED_GENERATION_NONCE_ATTEMPTS} attempts.",
            field="artifact_nonce",
            attempts=_DETACHED_GENERATION_NONCE_ATTEMPTS,
            retryable=True,
        )

    def _detached_exact_generation_source(
        self,
        definition: IndexDefinition,
        through_lsn: Lsn,
    ) -> tuple[Lsn, TableDef]:
        """Validate one detached build request and return its fenced table source."""

        if not isinstance(definition, IndexDefinition):
            raise GrafxIndexError(
                "A detached index generation needs an IndexDefinition.",
                field="definition",
                value=type(definition).__name__,
            )
        if definition.visibility is not IndexVisibility.EXACT:
            raise GrafxIndexError(
                f"Detached generation {definition.name!r} must be an exact index.",
                field="visibility",
                value=definition.visibility.value,
                index=definition.name,
            )
        if definition.artifact_nonce == 0:
            raise GrafxIndexError(
                f"Detached generation {definition.name!r} needs its catalog-assigned, "
                "non-zero artifact nonce before construction.",
                field="artifact_nonce",
                value=definition.artifact_nonce,
                index=definition.name,
            )
        position = _require_position("through_lsn", through_lsn)
        table = self._heap.catalog.catalog.table_by_id(definition.table_id)
        if not self._definition_matches_table_tolerantly(definition, table):
            raise GrafxIndexError(
                f"Detached generation {definition.name!r} does not describe committed table "
                f"{table.name!r}.",
                field="definition",
                index=definition.name,
                table=table.name,
                table_id=table.table_id,
            )
        return position, table

    def _detached_exact_generation_entries(
        self,
        definition: IndexDefinition,
        position: Lsn,
        table: TableDef,
    ) -> Iterator[tuple[RecordRef, bytes, Csn | None]]:
        """Yield exactly the durable entry images one detached generation will retain."""

        for ref, version in self._heap.scan_all(table):
            if is_provisional_csn(version.xmin):
                continue
            if not is_committed_csn(version.xmin):
                raise GrafxCorruptionDetected(
                    f"Record {version.record_id} of table {table.name!r} has invalid birth "
                    f"stamp {version.xmin} during detached index construction.",
                    file=self._heap.file,
                    table=table.name,
                    table_id=table.table_id,
                    record_id=version.record_id,
                    field="xmin",
                    value=version.xmin,
                )
            if version.xmin > position:
                raise GrafxIndexError(
                    f"Detached generation {definition.name!r} is fenced through {position}, "
                    f"but record {version.record_id} was committed at {version.xmin}.",
                    field="through_lsn",
                    value=position,
                    observed=version.xmin,
                    index=definition.name,
                    table=table.name,
                    record_id=version.record_id,
                )

            ended_at: Csn | None = None
            if not is_open_end_csn(version.xmax):
                if not is_committed_csn(version.xmax):
                    raise GrafxCorruptionDetected(
                        f"Record {version.record_id} of table {table.name!r} has invalid "
                        f"end stamp {version.xmax} during detached index construction.",
                        file=self._heap.file,
                        table=table.name,
                        table_id=table.table_id,
                        record_id=version.record_id,
                        field="xmax",
                        value=version.xmax,
                    )
                if version.xmax > position:
                    raise GrafxIndexError(
                        f"Detached generation {definition.name!r} is fenced through "
                        f"{position}, but record {version.record_id} ended at "
                        f"{version.xmax}.",
                        field="through_lsn",
                        value=position,
                        observed=version.xmax,
                        index=definition.name,
                        table=table.name,
                        record_id=version.record_id,
                    )
                ended_at = version.xmax

            key = definition.entry_key_for_record(version.record_id, version.values)
            if key is not None:
                yield ref, key, ended_at

    def _count_detached_exact_generation_entries(
        self,
        definition: IndexDefinition,
        through_lsn: Lsn,
        *,
        remaining: int | None = None,
    ) -> int:
        """Count final entries, stopping at ``remaining + 1`` when admission is bounded."""

        if remaining is not None and (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or remaining < 0
        ):
            raise GrafxIndexError(
                "A detached-generation entry remainder must be zero or more.",
                field="remaining",
                value=remaining,
            )

        position, table = self._detached_exact_generation_source(
            definition, through_lsn
        )
        observed = 0
        for _ref, _key, _ended_at in self._detached_exact_generation_entries(
            definition, position, table
        ):
            observed += 1
            if remaining is not None and observed > remaining:
                return observed
        return observed

    def _build_detached_exact_generation(
        self,
        definition: IndexDefinition,
        through_lsn: Lsn,
    ) -> IndexStore:
        """Build one complete, durable exact generation without publishing it.

        The caller has already allocated the catalog-v2 generation nonce and fenced writers at
        the global durable horizon supplied here.  This door deliberately does neither: it does
        not allocate an identity, touch the registry, stage logical WAL or publish catalog
        authority.  Exclusive physical-file creation is the ownership proof, so a competing or
        orphaned path is refused rather than adopted.

        Every committed heap version is retained, including historical versions, and every
        committed end becomes a tombstone.  That makes the detached generation usable by
        snapshots on either side of an update once a later catalog transaction publishes it.
        Both verification directions run before the final data checkpoint establishes the
        durability barrier.  A failed attempt leaves its uniquely nonced file unreachable and
        drops this process's frames so a later unrelated flush cannot continue the orphan.
        """
        position, table = self._detached_exact_generation_source(
            definition, through_lsn
        )

        index = HashIndex(definition, self._pool, self._metrics)
        index._set_creation_nonce(definition.artifact_nonce)
        collision = next(
            (
                current
                for current in self._indexes.values()
                if current.file.casefold() == index.file.casefold()
            ),
            None,
        )
        if collision is not None:
            raise GrafxIndexError(
                f"Detached generation file {index.file!r} is already owned by registered "
                f"index {collision.name!r}.",
                field="file",
                file=index.file,
                index=definition.name,
                registered=collision.name,
            )

        created = False
        try:
            # No preceding exists() observation grants ownership.  Only this exclusive create
            # distinguishes our new orphan-safe generation from another participant's bytes.
            self._pool.storage.create(index.file, exclusive=True)
            created = True
            index.create(proved_present=True)

            empty_build = _EmptyIndexBuild()
            for ref, key, ended_at in self._detached_exact_generation_entries(
                definition, position, table
            ):
                insert = IndexChange(
                    index=definition.name,
                    operation=IndexOperation.INSERT,
                    key=key,
                    ref=ref,
                )
                accelerated = index._apply_empty_build_change(
                    empty_build, insert, position
                )
                if accelerated is None:
                    index._apply_change(insert, position)
                if ended_at is not None:
                    tombstone = IndexChange(
                        index=definition.name,
                        operation=IndexOperation.TOMBSTONE,
                        key=key,
                        ref=ref,
                        csn=ended_at,
                    )
                    accelerated = index._apply_empty_build_change(
                        empty_build, tombstone, position
                    )
                    if accelerated is None:
                        index._apply_change(tombstone, position)

            # The header claim is flushed before verification, and the final checkpoint below
            # then barriers the complete verified generation as one unreachable shadow.
            index.advance_built_through(position)
            entry_findings = self._verify_entries(index)
            coverage_findings = self._verify_coverage(index)
            findings = (*entry_findings, *coverage_findings)
            if findings:
                raise GrafxIndexError(
                    f"Detached generation {definition.name!r} failed bidirectional "
                    f"verification with {len(findings)} finding(s).",
                    field="verification",
                    index=definition.name,
                    file=index.file,
                    count=len(findings),
                    kinds=tuple(finding.kind for finding in findings),
                )
            self._pool.checkpoint(index.file)
            return index
        except BaseException as failure:
            if created:
                self._discard_detached_generation_frames(index.file, failure)
            raise

    def _discard_detached_generation_frames(
        self, file: str, failure: BaseException
    ) -> None:
        """Drop every local frame owned by one failed, never-published generation."""
        self._heap_cache_certificates.pop(file, None)
        try:
            page_count = self._pool.storage.page_count(file)
            for page_index in range(page_count):
                self._pool.discard(file, page_index)
        except BaseException as cleanup_failure:  # pragma: no cover - defensive note
            failure.add_note(
                "Discarding frames of the failed detached generation also failed: "
                f"{cleanup_failure!r}"
            )

    def validate_staged_rebuild_generations(self, txn: StagingTransaction) -> None:
        """Refuse a staged RESET whose generation another claim has already superseded.

        A RESET carries the rebuild generation that authorised it. If something claimed a
        newer one while this transaction was staging, the record can still be written and
        will still be durable -- and will then refuse to apply, on this handle and on every
        reopen after it. Catching it here costs a retryable refusal; not catching it costs
        a database that will not open.
        """
        for index in tuple(self._indexes.values()):
            for change in index.pending(txn):
                if change.operation is not IndexOperation.RESET:
                    continue
                token = change.ref.page
                if not token:
                    continue
                certificate = index._fresh_certificate()
                if certificate.seq != token:
                    raise GrafxIndexError(
                        f"Index {index.name!r} staged a rebuild under generation {token}, "
                        f"and the durable generation is now {certificate.seq}; the reset "
                        "is refused before it can become an unreplayable record.",
                        field="rebuild_superseded",
                        index=index.name,
                        file=index.file,
                        rebuild_token=token,
                        device_seq=certificate.seq,
                        retryable=True,
                    )

    def clear_stale(
        self,
        name: str,
        through_lsn: Lsn,
        *,
        advance_to: Lsn | None = None,
        rebuild_token: int = 0,
    ) -> None:
        """Declare an index rebuilt through a position, so it may answer lookups again.

        Called by the caller that committed the transaction a rebuild staged, with the same
        position it passed to :meth:`rebuild`. It is separate from that method because a rebuild
        that was staged and never committed changed nothing, and an index that started answering
        at staging time would answer from a structure the log had not yet accepted.
        """
        index = self.active_index(name)
        index.clear_stale(
            through_lsn, advance_to=advance_to, rebuild_token=rebuild_token
        )
        try:
            self._bind_local_heap_view(index)
        except Exception:  # noqa: BLE001 - the clear already landed on the device
            # Rebinding the local view is derived state, not a mutation the caller is waiting
            # on. Failing here would report a refusal for an index that is durably healthy.
            pass

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
        authority = self._catalog_authority()
        legacy = authority is not None and (
            getattr(authority, "format_version", None) == CATALOG_LEGACY_FORMAT_VERSION
        )
        # Catalog v1 had no persistent access-path authority.  Its verifier historically
        # inspected the raw registry so that it could diagnose, among other things, an index
        # bound to a table the catalog no longer knows.  Catalog v2 is different: only its exact
        # ACTIVE generations may be interpreted as database state, including by diagnostics.
        if legacy:
            indexes = self.indexes() if name is None else (self.index(name),)
        else:
            indexes = (
                self.active_indexes() if name is None else (self.active_index(name),)
            )
        findings: list[IndexFinding] = []
        for index in indexes:
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
        if is_provisional_csn(version.xmin):
            # A frame written before WAL refusal is an abandoned allocation, not an index row.
            # Exact indexes may legally retain a candidate for it, and proximity indexes must
            # never have persisted its reserved birth stamp.
            return ()
        if (
            definition.entry_key_for_record(version.record_id, version.values)
            != entry.key
        ):
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
        version_end = NO_CSN if is_open_end_csn(version.xmax) else version.xmax
        if entry.dead_csn != version_end:
            missing = version_end != NO_CSN and entry.live
            findings.append(
                IndexFinding(
                    kind="missing_tombstone"
                    if missing and entry.versioned
                    else "stale_stamp",
                    index=index.name,
                    detail=(
                        f"Entry at page {entry.page} slot {entry.slot} ends at {entry.dead_csn} "
                        f"and the version it points at ends at {version_end}."
                    ),
                    file=index.file,
                    page=entry.page,
                    slot=entry.slot,
                    lsn=version_end,
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
            if is_provisional_csn(version.xmin):
                continue
            key = definition.entry_key_for_record(version.record_id, version.values)
            if key is None:
                continue
            if (key, ref) in stored:
                continue
            if not is_open_end_csn(version.xmax) and version.xmax <= reconciled:
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
    return replace(header, flags=flags)


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
    if value < NO_LSN or value >= PROVISIONAL_CSN:
        raise GrafxIndexError(
            f"A log position must be between {NO_LSN} and {PROVISIONAL_CSN - 1}; got {value}.",
            field=field_name,
            value=value,
        )
    return value


def _require_retarget_csn(field_name: str, value: object) -> Csn:
    """Return a real commit stamp suitable for staged-record retargeting."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not NO_CSN < value < PROVISIONAL_CSN
    ):
        raise GrafxIndexError(
            f"A retargeted commit number must be between 1 and {PROVISIONAL_CSN - 1}; "
            f"{field_name} is {value!r}.",
            field=field_name,
            value=repr(value),
        )
    return value


def _retarget_change(change: IndexChange, old_csn: Csn, new_csn: Csn) -> IndexChange:
    """Return ``change`` with one predicted non-zero stamp replaced."""
    return replace(change, csn=new_csn) if change.csn == old_csn else change
