"""Immutable observations exposed by :mod:`okto_grafx.engine.database`.

The composition behind a database contains write-capable devices, page caches, logs and
transaction managers.  Returning any of those objects from a public property turns an
observational convenience into a second write API: a caller can bypass transaction ownership,
the recovery latch and the WAL-before-data protocol.  This module defines value snapshots of
that composition instead.  A snapshot contains only immutable domain values and scalars; it
never retains a collaborator, bound method, callback, ``inner`` attribute or capability token.

The views deliberately preserve familiar read spellings such as ``database.wal.last_lsn`` and
``database.catalog.catalog.tables()`` where doing so is cheap and honest.  Operations that can
change bytes or engine bookkeeping are absent rather than hidden behind an ``unsafe`` switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxLedgerError,
    GrafxQuarantineError,
    GrafxRecoveryRefused,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.ledger.entry import LedgerEntry
from okto_grafx.domain.model.schema import EmbeddingSpaceDef, TableDef
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.txn.partitions import partition_of
from okto_grafx.domain.wal.replay import ScanFailure
from okto_grafx.domain.wal.segment import SegmentInfo
from okto_grafx.engine.ledger_store import DamagedTail
from okto_grafx.engine.quarantine import QuarantineEntry

__all__ = [
    "PUBLIC_DATABASE_VIEW_ALLOWLIST",
    "BufferPoolView",
    "CatalogStoreView",
    "CatalogView",
    "ClockView",
    "CodecView",
    "ComponentView",
    "CoordinatorView",
    "HeapStoreView",
    "IndexRegistryView",
    "IndexView",
    "LedgerView",
    "MetricsView",
    "QuarantineView",
    "QueryEngineView",
    "StorageFileView",
    "StorageView",
    "TransactionManagerView",
    "VectorEngineView",
    "VectorIndexView",
    "VectorMathView",
    "WalView",
]


@dataclass(frozen=True, slots=True)
class StorageFileView:
    """Immutable size metadata for one file in the storage namespace."""

    name: str
    size_bytes: int
    page_size: int

    @property
    def page_count(self) -> int:
        """Return how many complete configured pages fit in this file."""
        return self.size_bytes // self.page_size


@dataclass(frozen=True, slots=True)
class StorageView:
    """Read-only inventory of the storage device at the instant the view was requested."""

    name: str
    page_size: int
    files: tuple[StorageFileView, ...]

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return captured file names starting with ``prefix`` in stable order."""
        if not isinstance(prefix, str):
            raise GrafxConfigurationError(
                f"A storage prefix must be a string; got {type(prefix).__name__}.",
                field="prefix",
                value=type(prefix).__name__,
            )
        return tuple(item.name for item in self.files if item.name.startswith(prefix))

    def exists(self, file: str) -> bool:
        """Return whether ``file`` existed when this snapshot was built."""
        return any(item.name == file for item in self.files)

    def file_size(self, file: str) -> int:
        """Return the captured byte length of ``file``."""
        return self._file(file).size_bytes

    def page_count(self, file: str) -> int:
        """Return the captured complete-page count of ``file``."""
        return self._file(file).page_count

    def _file(self, name: str) -> StorageFileView:
        """Return one captured file or refuse a name outside the snapshot."""
        for item in self.files:
            if item.name == name:
                return item
        raise GrafxConfigurationError(
            f"No file named {name!r} belongs to this storage snapshot.",
            field="file",
            value=name,
        )


@dataclass(frozen=True, slots=True)
class ClockView:
    """Immutable identity of the clock implementation, without advancing either source."""

    implementation: str


@dataclass(frozen=True, slots=True)
class CodecView:
    """Immutable public identity of the configured page codec."""

    format_version: int
    page_size: int


@dataclass(frozen=True, slots=True)
class MetricsView:
    """Immutable metric capability summary; values remain available on ``Database`` itself."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class ComponentView:
    """Identity-only view for a component with no safe collaborator-level read API."""

    role: str
    implementation: str


@dataclass(frozen=True, slots=True)
class QueryEngineView:
    """Immutable query-planning diagnostics that do not retain the query engine."""

    implementation: str
    skipped_indexes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VectorMathView:
    """Immutable identity of the selected vector arithmetic implementation."""

    name: str


@dataclass(frozen=True, slots=True)
class CoordinatorView:
    """Participant identity without reading or maintaining coordination records."""

    participant: str
    implementation: str

    def owner_id(self) -> str:
        """Return the participant identity captured with this view."""
        return self.participant


@dataclass(frozen=True, slots=True)
class BufferPoolView:
    """Captured buffer-pool capacity and residency counters."""

    page_size: int
    budget_bytes: int
    capacity_pages: int
    used_bytes_value: int
    db_label: str

    def used_bytes(self) -> int:
        """Return the resident byte count captured with this view."""
        return self.used_bytes_value


@dataclass(frozen=True, slots=True)
class CatalogView:
    """Immutable catalog definition snapshot with read-compatible lookup helpers."""

    table_definitions: tuple[TableDef, ...]
    space_definitions: tuple[EmbeddingSpaceDef, ...]

    def tables(self) -> tuple[TableDef, ...]:
        """Return captured tables in numeric identity order."""
        return self.table_definitions

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        """Return captured embedding spaces in numeric identity order."""
        return self.space_definitions

    def has_table(self, name: str) -> bool:
        """Return whether a captured table has ``name``."""
        return any(table.name == name for table in self.table_definitions)

    def has_space(self, name: str) -> bool:
        """Return whether a captured embedding space has ``name``."""
        return any(space.name == name for space in self.space_definitions)

    def table(self, name: str) -> TableDef:
        """Return a captured table by name."""
        for table in self.table_definitions:
            if table.name == name:
                return table
        raise GrafxConfigurationError(
            f"There is no table named {name!r} in this catalog snapshot.",
            field="table",
            value=name,
        )

    def table_by_id(self, table_id: int) -> TableDef:
        """Return a captured table by numeric identity."""
        for table in self.table_definitions:
            if table.table_id == table_id:
                return table
        raise GrafxConfigurationError(
            f"There is no table with id {table_id!r} in this catalog snapshot.",
            field="table_id",
            value=table_id,
        )

    def space(self, name: str) -> EmbeddingSpaceDef:
        """Return a captured embedding space by name."""
        for space in self.space_definitions:
            if space.name == name:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space named {name!r} in this catalog snapshot.",
            field="space",
            value=name,
        )

    def space_by_id(self, space_id: int) -> EmbeddingSpaceDef:
        """Return a captured embedding space by numeric identity."""
        for space in self.space_definitions:
            if space.space_id == space_id:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space with id {space_id!r} in this catalog snapshot.",
            field="space_id",
            value=space_id,
        )

    def is_empty(self) -> bool:
        """Return whether no table or embedding space was captured."""
        return not self.table_definitions and not self.space_definitions


@dataclass(frozen=True, slots=True)
class CatalogStoreView:
    """Immutable metadata and catalog snapshot for the catalog store."""

    file: str
    chunk_capacity: int
    catalog: CatalogView


@dataclass(frozen=True, slots=True)
class HeapStoreView:
    """Immutable layout metadata for the heap store."""

    file: str
    max_tables: int
    inline_capacity: int


@dataclass(frozen=True, slots=True)
class WalView:
    """Immutable state snapshot of the write-ahead log."""

    last_lsn: int
    directory: str
    descriptor: str
    segment_bytes: int
    damage: ScanFailure | None
    append_uncertain: bool
    segment_inventory: tuple[SegmentInfo, ...]
    total_bytes_value: int

    def segments(self) -> tuple[SegmentInfo, ...]:
        """Return the segment inventory captured with this view."""
        return self.segment_inventory

    def total_bytes(self) -> int:
        """Return the live log size captured with this view."""
        return self.total_bytes_value


@dataclass(frozen=True, slots=True)
class TransactionManagerView:
    """Immutable transaction configuration and publication snapshot."""

    partitions_per_table: int
    commit_lock_timeout: float
    lease_timeout: float
    reader_stall_threshold: float | None
    refresh_interval: float
    open_transactions: int
    recovery_required: bool
    state: CommitState | None

    def partition_of(self, table_id: int, key: bytes) -> int:
        """Return the deterministic conflict partition for ``table_id`` and ``key``."""
        return partition_of(table_id, key, self.partitions_per_table)

    def published_state(self) -> CommitState:
        """Return the commit state captured with this view."""
        if self.state is None:
            raise GrafxRecoveryRefused(
                "Publication state is unavailable while this handle requires recovery.",
                field="recovery_required",
            )
        return self.state

    def published_lsn(self) -> int:
        """Return the published commit LSN captured with this view."""
        return self.published_state().last_committed_lsn


@dataclass(frozen=True, slots=True)
class IndexView:
    """Immutable public metadata for one registered secondary index."""

    name: str
    file: str
    visibility: IndexVisibility
    definition: IndexDefinition
    stale: bool
    stale_reason: str | None
    built_through_lsn: int
    reconciled_through_lsn: int
    missing_targets: int


@dataclass(frozen=True, slots=True)
class IndexRegistryView:
    """Immutable inventory of registered secondary indexes."""

    registered: tuple[IndexView, ...]
    published_lsn: int

    def indexes(self) -> tuple[IndexView, ...]:
        """Return the captured indexes in stable name order."""
        return self.registered

    def index(self, name: str) -> IndexView:
        """Return one captured index by case-insensitive name."""
        if not isinstance(name, str):
            raise GrafxIndexError(
                f"An index is named by a string; got {type(name).__name__}.",
                field="name",
                value=type(name).__name__,
            )
        for index in self.registered:
            if index.name.lower() == name.lower():
                return index
        raise GrafxIndexError(
            f"No index named {name!r} belongs to this registry snapshot.",
            field="name",
            value=name,
        )


@dataclass(frozen=True, slots=True)
class LedgerView:
    """Immutable inventory of forensic ledger entries."""

    file: str
    damage: DamagedTail | None
    captured_entries: tuple[LedgerEntry, ...]
    depth_by_origin: tuple[tuple[str, int], ...]

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Return every captured entry, oldest first."""
        return self.captured_entries

    def depth(self) -> tuple[tuple[str, int], ...]:
        """Return captured pending counts as immutable key-value pairs."""
        return self.depth_by_origin

    def list(
        self,
        *,
        origin_class: object = None,
        reason: object = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[LedgerEntry, ...]:
        """Filter the captured entries without touching the ledger store."""
        origin_word = _enum_word(origin_class)
        reason_word = _enum_word(reason)
        selected = tuple(
            entry
            for entry in self.captured_entries
            if (origin_word is None or _enum_word(entry.origin_class) == origin_word)
            and (reason_word is None or _enum_word(entry.reason) == reason_word)
        )
        return selected[offset : offset + limit]

    def inspect(self, entry_id: int) -> LedgerEntry:
        """Return one captured ledger entry by identity."""
        for entry in self.captured_entries:
            if entry.entry_id == entry_id:
                return entry
        raise GrafxLedgerError(
            f"No ledger entry with id {entry_id!r} belongs to this snapshot.",
            field="entry_id",
            value=entry_id,
        )


@dataclass(frozen=True, slots=True)
class QuarantineView:
    """Immutable inventory of quarantined evidence without restore or write capabilities."""

    directory: str
    captured_entries: tuple[QuarantineEntry, ...]

    def list(self) -> tuple[QuarantineEntry, ...]:
        """Return every quarantine entry captured with this view."""
        return self.captured_entries

    def inspect(self, name: str) -> QuarantineEntry:
        """Return one captured quarantine entry by name."""
        for entry in self.captured_entries:
            if entry.name == name:
                return entry
        raise GrafxQuarantineError(
            f"No quarantine entry named {name!r} belongs to this snapshot.",
            field="name",
            value=name,
        )


@dataclass(frozen=True, slots=True)
class VectorIndexView:
    """Immutable identity and search configuration of one vector index."""

    name: str
    file: str
    space_id: int
    space_name: str
    dimension: int
    metric_of_space: DistanceMetric
    storage_dtype: str
    ef_search: int
    stale: bool
    stale_reason: str | None
    built_through_lsn: int


@dataclass(frozen=True, slots=True)
class VectorEngineView:
    """Immutable inventory and configuration of the vector subsystem."""

    exact_scan_threshold: int
    space_definitions: tuple[EmbeddingSpaceDef, ...]
    registered_indexes: tuple[VectorIndexView, ...]

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        """Return captured embedding-space definitions."""
        return self.space_definitions

    def space(self, name: str) -> EmbeddingSpaceDef:
        """Return one captured embedding-space definition by name."""
        for space in self.space_definitions:
            if space.name == name:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space named {name!r} in this vector snapshot.",
            field="space",
            value=name,
        )

    def indexes(self) -> tuple[VectorIndexView, ...]:
        """Return captured vector indexes in stable space-name order."""
        return self.registered_indexes

    def index(self, space_name: str) -> VectorIndexView:
        """Return the captured vector index of one embedding space."""
        for index in self.registered_indexes:
            if index.space_name == space_name:
                return index
        raise GrafxIndexError(
            f"Embedding space {space_name!r} has no index in this snapshot.",
            field="space",
            value=space_name,
        )


PUBLIC_DATABASE_VIEW_ALLOWLIST: tuple[tuple[str, type[object]], ...] = (
    ("storage", StorageView),
    ("clock", ClockView),
    ("codec", CodecView),
    ("metrics", MetricsView),
    ("events", ComponentView),
    ("vector_math", VectorMathView),
    ("coordinator", CoordinatorView),
    ("pool", BufferPoolView),
    ("catalog", CatalogStoreView),
    ("heap", HeapStoreView),
    ("wal", WalView),
    ("transactions", TransactionManagerView),
    ("indexes", IndexRegistryView),
    ("ledger", LedgerView),
    ("quarantine", QuarantineView),
    ("vectors", VectorEngineView),
    ("queries", QueryEngineView),
)
"""Static allowlist of every public composition property and its safe result type."""


def _enum_word(value: object) -> str | None:
    """Return an enum-like value's public word, preserving None as no filter."""
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    return str(candidate)


def _storage_view(storage: Any) -> StorageView:
    """Snapshot a storage device without retaining it."""
    page_size = int(storage.page_size)
    files = tuple(
        StorageFileView(name, int(storage.file_size(name)), page_size)
        for name in storage.list_files()
    )
    return StorageView(str(storage.name), page_size, files)


def _clock_view(clock: Any) -> ClockView:
    """Identify a clock without advancing an injected stateful test or virtual clock."""
    return ClockView(type(clock).__name__)


def _codec_view(codec: Any, page_size: int) -> CodecView:
    """Snapshot codec identity without exposing encode or decode capabilities."""
    return CodecView(int(codec.format_version), page_size)


def _component_view(role: str, component: object) -> ComponentView:
    """Return identity-only metadata for a collaborator."""
    return ComponentView(role, type(component).__name__)


def _queries_view(queries: Any) -> QueryEngineView:
    """Snapshot query diagnostics without retaining the planner or executor."""
    return QueryEngineView(type(queries).__name__, tuple(queries.skipped_indexes))


def _coordinator_view(coordinator: Any) -> CoordinatorView:
    """Identify a coordinator without touching liveness, lease or retirement doors.

    ``reader_horizon`` is intentionally absent: LocalProcessCoordinator prunes dead, damaged and
    temporary registrations while answering it.  ``current_epoch`` also records a liveness
    observation used by owner-stall detection.  Merely evaluating a Database property must do
    neither, so this view captures only the protocol's stable participant identity.
    """
    return CoordinatorView(str(coordinator.owner_id()), type(coordinator).__name__)


def _pool_view(pool: Any) -> BufferPoolView:
    """Snapshot buffer-pool counters and limits."""
    return BufferPoolView(
        int(pool.page_size),
        int(pool.budget_bytes),
        int(pool.capacity_pages),
        int(pool.used_bytes()),
        str(pool.db_label),
    )


def _catalog_view(store: Any) -> CatalogStoreView:
    """Snapshot catalog metadata and immutable schema definitions."""
    catalog = store.catalog
    return CatalogStoreView(
        str(store.file),
        int(store.chunk_capacity),
        CatalogView(tuple(catalog.tables()), tuple(catalog.spaces())),
    )


def _heap_view(heap: Any) -> HeapStoreView:
    """Snapshot safe heap layout metadata."""
    return HeapStoreView(str(heap.file), int(heap.max_tables), int(heap.inline_capacity))


def _wal_view(wal: Any) -> WalView:
    """Snapshot WAL state and its immutable segment metadata."""
    # Each of last_lsn/damage/segments/total_bytes ordinarily refreshes the same tail. Refresh
    # once, then copy the cached values produced by that pass; a diagnostic snapshot must not do
    # four directory surveys and four metric publications for one answer.
    wal.refresh()
    return WalView(
        int(wal._last_lsn),
        str(wal._directory),
        str(wal._descriptor),
        int(wal._segment_bytes),
        wal._damage,
        bool(wal._append_uncertain),
        tuple(wal._segments),
        int(wal._total_bytes),
    )


def _transactions_view(transactions: Any) -> TransactionManagerView:
    """Snapshot transaction limits, counts and published state."""
    recovery_required = bool(transactions.recovery_required)
    return TransactionManagerView(
        int(transactions.partitions_per_table),
        float(transactions.commit_lock_timeout),
        float(transactions.lease_timeout),
        transactions.reader_stall_threshold,
        float(transactions.refresh_interval),
        int(transactions.open_transactions),
        recovery_required,
        None if recovery_required else transactions.published_state(),
    )


def _index_view(index: Any) -> IndexView:
    """Snapshot one registered index without retaining its store."""
    # Both positions live in the same header. Read it once so the snapshot is coherent and does
    # one checksum/page-cache access per index instead of two.
    header = index.header
    return IndexView(
        str(index.name),
        str(index.file),
        index.visibility,
        index.definition,
        bool(index.stale),
        index.stale_reason,
        int(header.built_through_lsn),
        int(header.reconciled_through_lsn),
        int(index.missing_targets),
    )


def _indexes_view(indexes: Any) -> IndexRegistryView:
    """Snapshot a complete registered-index inventory."""
    return IndexRegistryView(
        tuple(_index_view(index) for index in indexes.indexes()),
        int(indexes.published_lsn),
    )


def _ledger_view(ledger: Any) -> LedgerView:
    """Snapshot forensic ledger metadata and immutable entries."""
    return LedgerView(
        str(ledger.file),
        ledger.damage,
        tuple(ledger.entries()),
        tuple(sorted((str(key), int(value)) for key, value in ledger.depth().items())),
    )


def _quarantine_view(quarantine: Any) -> QuarantineView:
    """Snapshot quarantine inventory without exposing payload or restore doors."""
    return QuarantineView(str(quarantine.directory), tuple(quarantine.list()))


def _vector_index_view(index: Any) -> VectorIndexView:
    """Snapshot one vector index without retaining its engine or store."""
    return VectorIndexView(
        str(index.name),
        str(index.file),
        int(index.space_id),
        str(index.space_name),
        int(index.dimension),
        index.metric_of_space,
        str(index.storage_dtype),
        int(index.ef_search),
        bool(index.stale),
        index.stale_reason,
        int(index.built_through_lsn),
    )


def _vectors_view(vectors: Any) -> VectorEngineView:
    """Snapshot vector configuration, spaces and registered indexes."""
    return VectorEngineView(
        int(vectors.exact_scan_threshold),
        tuple(vectors.spaces()),
        tuple(_vector_index_view(index) for index in vectors.indexes()),
    )
