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

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from functools import lru_cache
from math import isfinite
from typing import TYPE_CHECKING, Any, cast, get_args, get_origin, get_type_hints

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxLedgerError,
    GrafxParseError,
    GrafxPlanError,
    GrafxQuarantineError,
    GrafxRecoveryRefused,
    GrafxVectorValidationError,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.ids import MAX_PAGE_INDEX, MAX_SLOT_ID, NULL_REF, RecordRef
from okto_grafx.domain.ledger.entry import (
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
)
from okto_grafx.domain.ledger.payload import LedgerPayload, decode_payload
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    MAX_VALUE_DEPTH,
    MAX_VECTOR_DIMENSION,
    VECTOR_DTYPES,
    Timestamp,
    Uuid,
    Value,
    ValueType,
    VectorValue,
)
from okto_grafx.domain.page.layout import MAX_U32, MAX_U64
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.analysis import Aggregation
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    CaseAlternative,
    CaseExpression,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    MapEntry,
    MapExpression,
    NamedArgument,
    NullCheck,
    Parameter,
    Property,
    ReturnItem,
    SortItem,
    Subscript,
    UnaryOperation,
    Variable,
)
from okto_grafx.domain.query.limits import (
    MAX_EXPRESSION_DEPTH,
    MAX_LIST_ELEMENTS,
    MAX_MAP_ENTRIES,
    MAX_NAME_CHARACTERS,
    MAX_PARAMETERS,
    MAX_PROJECTION_ITEMS,
    MAX_QUERY_CHARACTERS,
    MAX_RENDERED_QUERY_CHARACTERS,
    MAX_STRING_CHARACTERS,
)
from okto_grafx.domain.query.plan import (
    AllNodesScan,
    MAX_PLAN_DEPTH,
    AggregateRows,
    CreateNodeTable,
    CreatedNode,
    CreatedRelationship,
    CreateRelationships,
    CreateRelTable,
    CreateVectorSpace,
    DeleteEntities,
    DistinctRows,
    EagerRows,
    FilterRows,
    IndexSeek,
    LimitRows,
    MergePattern,
    NodeScan,
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    SetProperties,
    SingleRow,
    SkipRows,
    SortRows,
    TraverseRelationship,
    UnwindRows,
    VectorSearch,
    WithRows,
    validate_plan,
)
from okto_grafx.domain.recovery.manifest import (
    MANIFEST_FILE_NAME,
    RESTORE_RECEIPT_PREFIX,
    QuarantineManifest,
    stamp_of,
)
from okto_grafx.domain.recovery.report import RecoveryFinding, RecoveryReport
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.txn.partitions import partition_of
from okto_grafx.domain.vector.key import VectorIndexDefinition
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.vector.planner import REGIMES
from okto_grafx.domain.verify.findings import (
    NOT_APPLICABLE,
    SCOPE_ALL,
    SCOPE_INDEXES,
    SCOPE_PAGES,
    SCOPE_RECORDS,
    VERIFICATION_FINDING_KINDS,
    FindingLocation,
    VerificationFinding,
    VerificationReport,
)
from okto_grafx.domain.wal.codec import FailureReason
from okto_grafx.domain.wal.replay import RecycleReport, ScanFailure
from okto_grafx.domain.wal.segment import SegmentInfo
from okto_grafx.engine.ledger_store import DamagedTail
from okto_grafx.engine.quarantine import (
    QuarantineEntry,
    QuarantineInventoryItem,
    QuarantineInventoryState,
    _is_restore_receipt,
    _payload_file_name,
)
from okto_grafx.engine.vector_engine import VectorHit, VectorSearchResult

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from okto_grafx.engine.query_engine import QueryResult

_PEP604_UNION_TYPE = type(str | None)
"""Runtime origin returned by ``typing.get_origin`` for a PEP 604 union."""

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
    "MaintenanceStatus",
    "MetricsSnapshotView",
    "MetricsView",
    "QuarantineInventoryItem",
    "QuarantineInventoryState",
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
        wanted = _builtin_text(prefix, field="prefix")
        return tuple(item.name for item in self.files if item.name.startswith(wanted))

    def exists(self, file: str) -> bool:
        """Return whether ``file`` existed when this snapshot was built."""
        wanted = _require_text("file", file)
        return any(item.name == wanted for item in self.files)

    def file_size(self, file: str) -> int:
        """Return the captured byte length of ``file``."""
        return self._file(file).size_bytes

    def page_count(self, file: str) -> int:
        """Return the captured complete-page count of ``file``."""
        return self._file(file).page_count

    def _file(self, name: str) -> StorageFileView:
        """Return one captured file or refuse a name outside the snapshot."""
        wanted = _require_text("file", name)
        for item in self.files:
            if item.name == wanted:
                return item
        raise GrafxConfigurationError(
            f"No file named {wanted!r} belongs to this storage snapshot.",
            field="file",
            value=wanted,
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
class MaintenanceStatus:
    """Last-observed operational status without unavailable estimates or live capabilities."""

    wal_bytes: int
    checkpoint_lag_lsn: int | None
    recovery_required: bool
    stale_indexes: tuple[str, ...]
    heap_bloat_bytes: int | None
    oldest_reader_age: float | None


@dataclass(frozen=True, slots=True, eq=False)
class MetricsSnapshotView(Mapping[str, object]):
    """A detached immutable mapping used at every level of a metrics snapshot."""

    entries: tuple[tuple[str, object], ...]
    __hash__ = None

    def __getitem__(self, key: str) -> object:
        """Return one captured value without consulting the source metrics mapping."""
        wanted = _builtin_text(key, field="metrics.key", empty=False)
        for name, value in self.entries:
            if name == wanted:
                return value
        raise KeyError(wanted)

    def __iter__(self) -> Iterator[str]:
        """Iterate captured names in the order supplied by the sink."""
        return (name for name, _value in self.entries)

    def __len__(self) -> int:
        """Return the number of captured metric keys."""
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        """Compare by mapping contents, preserving the previous Mapping API behaviour."""
        if not isinstance(other, Mapping):
            return False
        return dict(self.entries) == dict(other.items())


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
        wanted = _require_text("table", name)
        return any(table.name == wanted for table in self.table_definitions)

    def has_space(self, name: str) -> bool:
        """Return whether a captured embedding space has ``name``."""
        wanted = _require_text("space", name)
        return any(space.name == wanted for space in self.space_definitions)

    def table(self, name: str) -> TableDef:
        """Return a captured table by name."""
        wanted = _require_text("table", name)
        for table in self.table_definitions:
            if table.name == wanted:
                return table
        raise GrafxConfigurationError(
            f"There is no table named {wanted!r} in this catalog snapshot.",
            field="table",
            value=wanted,
        )

    def table_by_id(self, table_id: int) -> TableDef:
        """Return a captured table by numeric identity."""
        wanted = _require_integer("table_id", table_id)
        for table in self.table_definitions:
            if table.table_id == wanted:
                return table
        raise GrafxConfigurationError(
            f"There is no table with id {wanted!r} in this catalog snapshot.",
            field="table_id",
            value=wanted,
        )

    def space(self, name: str) -> EmbeddingSpaceDef:
        """Return a captured embedding space by name."""
        wanted = _require_text("space", name)
        for space in self.space_definitions:
            if space.name == wanted:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space named {wanted!r} in this catalog snapshot.",
            field="space",
            value=wanted,
        )

    def space_by_id(self, space_id: int) -> EmbeddingSpaceDef:
        """Return a captured embedding space by numeric identity."""
        wanted = _require_integer("space_id", space_id)
        for space in self.space_definitions:
            if space.space_id == wanted:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space with id {wanted!r} in this catalog snapshot.",
            field="space_id",
            value=wanted,
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
        return partition_of(
            _builtin_int(table_id, field="table_id"),
            _builtin_bytes(key, field="key"),
            _builtin_int(self.partitions_per_table),
        )

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
    """Immutable public metadata for one committed secondary index.

    Header positions are ``None`` when page zero was not already resident.  A diagnostic
    property never faults that page in or evicts application data merely to fill these fields.
    """

    name: str
    file: str
    visibility: IndexVisibility
    definition: IndexDefinition
    stale: bool
    stale_reason: str | None
    built_through_lsn: int | None
    reconciled_through_lsn: int | None
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
        if not issubclass(type(name), str):
            observed = _builtin_type_name(name)
            raise GrafxIndexError(
                f"An index is named by a string; got {observed}.",
                field="name",
                value=observed,
            )
        wanted = _builtin_text(name, field="name")
        for index in self.registered:
            if index.name.lower() == wanted.lower():
                return index
        raise GrafxIndexError(
            f"No index named {wanted!r} belongs to this registry snapshot.",
            field="name",
            value=wanted,
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
        wanted_class = _as_origin_class(origin_class)
        wanted_reason = _as_reason(reason)
        page_size = _require_ledger_count("limit", limit)
        start = _require_ledger_count("offset", offset)
        selected = tuple(
            entry
            for entry in self.captured_entries
            if (wanted_class is None or entry.origin_class is wanted_class)
            and (wanted_reason is None or entry.reason is wanted_reason)
        )
        return selected[start : start + page_size]

    def inspect(self, entry_id: int) -> LedgerEntry:
        """Return one captured ledger entry by identity."""
        wanted = _require_ledger_identifier(entry_id)
        for entry in self.captured_entries:
            if entry.entry_id == wanted:
                return entry
        raise GrafxLedgerError(
            f"The ledger holds no entry {wanted}.",
            field="entry_id",
            entry_id=wanted,
        )

    def provenance(self, entry_id: int) -> LedgerPayload:
        """Decode the immutable provenance captured with one ledger entry."""
        return decode_payload(self.inspect(entry_id).payload)

    def export(self, entry_id: int) -> bytes:
        """Return preserved bytes from the captured, already-verified ledger entry."""
        entry = self.inspect(entry_id)
        if entry.reapplicable:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} is reapplicable, so it carries an operation "
                "rather than preserved bytes; reprocess it instead of exporting it.",
                field="origin_class",
                entry_id=entry.entry_id,
            )
        provenance = decode_payload(entry.payload)
        if not provenance.body and provenance.length:
            raise GrafxLedgerError(
                f"Ledger entry {entry.entry_id} preserves {provenance.length} bytes that were too "
                f"large to carry here; read them from quarantine entry "
                f"{provenance.quarantine!r} instead.",
                field="quarantine",
                entry_id=entry.entry_id,
                quarantine=provenance.quarantine,
            )
        return provenance.body


@dataclass(frozen=True, slots=True)
class QuarantineView:
    """Immutable inventory of quarantined evidence without restore or write capabilities."""

    directory: str
    captured_entries: tuple[QuarantineEntry, ...]
    captured_inventory: tuple[QuarantineInventoryItem, ...] | None = None

    def list(self) -> tuple[QuarantineEntry, ...]:
        """Return every quarantine entry captured with this view."""
        return self.captured_entries

    def count(self) -> int:
        """Return how many quarantine entries were captured with this view."""
        return len(self.captured_entries)

    def inventory(self) -> tuple[QuarantineInventoryItem, ...]:
        """Return the complete evidence inventory captured with this view.

        ``None`` is reserved for views built by older callers that supplied only the legacy
        complete-entry list.  Treating that state as an empty inventory would erase the
        distinction between "proved empty" and "not observed", so it refuses explicitly.
        """
        if self.captured_inventory is None:
            raise GrafxQuarantineError(
                "The quarantine inventory was not captured with this view, so absence cannot "
                "be concluded.",
                field="inventory",
                cause="not_captured",
                conclusive=False,
                inconclusive=True,
            )
        return self.captured_inventory

    def inspect(self, name: str) -> QuarantineEntry:
        """Return one captured quarantine entry by name."""
        wanted = _require_quarantine_name(name)
        for entry in self.captured_entries:
            if entry.name == wanted:
                return entry
        raise GrafxQuarantineError(
            f"The quarantine holds no readable entry {wanted!r}.",
            field="name",
            entry=wanted,
        )


@dataclass(frozen=True, slots=True)
class VectorIndexView:
    """Immutable identity and search configuration of one committed vector index.

    ``built_through_lsn`` is ``None`` when the header was cold at the observation point.
    """

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
    built_through_lsn: int | None


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
        wanted = _require_text("space", name)
        for space in self.space_definitions:
            if space.name == wanted:
                return space
        raise GrafxConfigurationError(
            f"There is no embedding space named {wanted!r} in this vector snapshot.",
            field="space",
            value=wanted,
        )

    def indexes(self) -> tuple[VectorIndexView, ...]:
        """Return captured vector indexes in stable space-name order."""
        return self.registered_indexes

    def index(self, space_name: str) -> VectorIndexView:
        """Return the captured vector index of one embedding space."""
        if not issubclass(type(space_name), str):
            observed = _builtin_type_name(space_name)
            raise GrafxIndexError(
                f"An embedding space is named by a string; got {observed}.",
                field="space",
                value=observed,
            )
        wanted = _builtin_text(space_name, field="space")
        for index in self.registered_indexes:
            if index.space_name == wanted:
                return index
        raise GrafxIndexError(
            f"Embedding space {wanted!r} has no index in this snapshot.",
            field="space",
            value=wanted,
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


def _builtin_type_name(value: object) -> str:
    """Return the exact built-in name of a value's class without metaclass dispatch."""
    # ``type.__getattribute__`` still honours a descriptor installed by a hostile metaclass.
    # Invoke the built-in ``type.__name__`` descriptor itself to bypass that door completely.
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _builtin_class_name(value: type[object]) -> str:
    """Return a class name through the built-in descriptor, bypassing its metaclass."""
    declared = type.__dict__["__name__"].__get__(value, type(value))
    return str.__str__(declared)


def _builtin_text(value: object, *, field: str, empty: bool = True) -> str:
    """Return an exact built-in string, never a capability-carrying ``str`` subclass.

    ``str(value)`` is deliberately not a canonicalizer: Python is allowed to return the same
    object when ``value`` already is a string, including a callable subclass with a ``__dict__``.
    Calling the base slot copies a subclass into an exact ``str`` without invoking its override.
    """
    if not issubclass(type(value), str):
        qualification = "a string" if empty else "a non-empty string"
        raise GrafxConfigurationError(
            f"The {field} of this public value must be {qualification}; got "
            f"{_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    plain = str.__str__(value)
    if not empty and str.__len__(plain) == 0:
        raise GrafxConfigurationError(
            f"The {field} of this public value must be a non-empty string.",
            field=field,
            value="",
        )
    return plain


def _builtin_optional_text(value: object, *, field: str) -> str | None:
    """Canonicalize an optional string leaf."""
    if value is None:
        return None
    return _builtin_text(value, field=field)


def _builtin_int(value: object, *, field: str = "integer") -> int:
    """Copy an integer into an exact built-in value without invoking ``value.__int__``."""
    if type(value) is bool or not issubclass(type(value), int):
        raise GrafxConfigurationError(
            f"The public {field} value must be an int; got {_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    return int.__int__(value)


def _builtin_float(value: object) -> float:
    """Copy an int/float into an exact float without invoking a host numeric override."""
    if type(value) is bool or not issubclass(type(value), (int, float)):
        raise GrafxConfigurationError(
            f"A public real value must be an int or float; got {_builtin_type_name(value)}.",
            field="real",
            value=_builtin_type_name(value),
        )
    if issubclass(type(value), float):
        return float.__float__(value)
    return float(int.__int__(value))


def _builtin_bool(value: object) -> bool:
    """Return an exact boolean without invoking an arbitrary ``__bool__`` method."""
    if type(value) is not bool:
        raise GrafxConfigurationError(
            f"A public boolean value must be a bool; got {_builtin_type_name(value)}.",
            field="boolean",
            value=_builtin_type_name(value),
        )
    return value


def _builtin_bytes(value: object, *, field: str = "bytes") -> bytes:
    """Copy one buffer into exact bytes without trusting a subclass's ``__bytes__`` door."""
    if not issubclass(type(value), (bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"The public {field} value must be bytes-like; got {_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    return memoryview(value).tobytes()


def _domain_value(value: object, expected: type[object], *, field: str) -> Any:
    """Validate one deep domain node before reading any of its attributes."""
    if not issubclass(type(value), expected):
        expected_name = _builtin_class_name(expected)
        raise GrafxConfigurationError(
            f"The {field} of this public value must be {expected_name}; got "
            f"{_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    return value


def _domain_field(value: object, expected: type[object], name: str) -> Any:
    """Read a base domain slot without dispatching through a subclass override.

    Domain subclasses remain accepted and are reconstructed into exact public values, but a
    subclass cannot turn a field read into a property callback or custom ``__getattribute__``.
    """
    for owner in expected.__mro__:
        descriptor = owner.__dict__.get(name)
        if descriptor is not None and hasattr(descriptor, "__get__"):
            try:
                return descriptor.__get__(value, expected)
            except AttributeError as failure:
                expected_name = _builtin_class_name(expected)
                raise GrafxConfigurationError(
                    f"The {expected_name} supplied to a public view has no initialized "
                    f"{name} field.",
                    field=name,
                    value="missing",
                ) from failure
    raise AssertionError(f"{_builtin_class_name(expected)} has no domain field {name}")


def _string_enum(value: object, expected: type[Any], *, field: str) -> Any:
    """Return an exact known string-enum member after validating the source class."""
    member = _domain_value(value, expected, field=field)
    return expected(_builtin_text(member.value, field=field, empty=False))


def _integer_enum(value: object, expected: type[Any], *, field: str) -> Any:
    """Return an exact known integer-enum member after validating the source class."""
    member = _domain_value(value, expected, field=field)
    return expected(_builtin_int(member))


def _tuple_items(value: object, *, field: str) -> tuple[object, ...]:
    """Copy a domain tuple through the base iterator, never a subclass override."""
    if not issubclass(type(value), tuple):
        raise GrafxConfigurationError(
            f"The {field} of this public value must be a tuple; got "
            f"{_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    return tuple(tuple.__iter__(value))


def _dictionary_values(value: object, *, field: str) -> tuple[object, ...]:
    """Copy values from a real dict without invoking subclass iteration methods."""
    if not issubclass(type(value), dict):
        raise GrafxConfigurationError(
            f"The {field} of this public value must be a dict; got "
            f"{_builtin_type_name(value)}.",
            field=field,
            value=_builtin_type_name(value),
        )
    return tuple(dict.values(value))


def _column_definition(value: Any) -> ColumnDef:
    """Rebuild one column with exact scalar and enum leaves."""
    value = _domain_value(value, ColumnDef, field="catalog.column")
    return ColumnDef(
        name=_builtin_text(
            _domain_field(value, ColumnDef, "name"), field="column.name", empty=False
        ),
        type=_integer_enum(
            _domain_field(value, ColumnDef, "type"), ValueType, field="column.type"
        ),
        nullable=_builtin_bool(_domain_field(value, ColumnDef, "nullable")),
        vector_space=_builtin_optional_text(
            _domain_field(value, ColumnDef, "vector_space"), field="column.vector_space"
        ),
    )


def _table_definition(value: Any) -> TableDef:
    """Rebuild a table and every nested column without retaining a domain subclass."""
    value = _domain_value(value, TableDef, field="catalog.table")
    return TableDef(
        table_id=_builtin_int(_domain_field(value, TableDef, "table_id")),
        name=_builtin_text(
            _domain_field(value, TableDef, "name"), field="table.name", empty=False
        ),
        kind=_builtin_text(
            _domain_field(value, TableDef, "kind"), field="table.kind", empty=False
        ),
        columns=tuple(
            _column_definition(column)
            for column in _tuple_items(
                _domain_field(value, TableDef, "columns"), field="table.columns"
            )
        ),
        primary_key=_builtin_optional_text(
            _domain_field(value, TableDef, "primary_key"), field="table.primary_key"
        ),
        from_table=_builtin_optional_text(
            _domain_field(value, TableDef, "from_table"), field="table.from_table"
        ),
        to_table=_builtin_optional_text(
            _domain_field(value, TableDef, "to_table"), field="table.to_table"
        ),
        schema_version=_builtin_int(_domain_field(value, TableDef, "schema_version")),
    )


def _space_definition(value: Any) -> EmbeddingSpaceDef:
    """Rebuild one embedding-space definition with exact leaves."""
    value = _domain_value(value, EmbeddingSpaceDef, field="catalog.space")
    return EmbeddingSpaceDef(
        space_id=_builtin_int(_domain_field(value, EmbeddingSpaceDef, "space_id")),
        name=_builtin_text(
            _domain_field(value, EmbeddingSpaceDef, "name"),
            field="space.name",
            empty=False,
        ),
        dimension=_builtin_int(_domain_field(value, EmbeddingSpaceDef, "dimension")),
        metric=_string_enum(
            _domain_field(value, EmbeddingSpaceDef, "metric"),
            DistanceMetric,
            field="space.metric",
        ),
        normalized=_builtin_bool(_domain_field(value, EmbeddingSpaceDef, "normalized")),
        storage_dtype=_builtin_text(
            _domain_field(value, EmbeddingSpaceDef, "storage_dtype"),
            field="space.storage_dtype",
            empty=False,
        ),
        state=_builtin_text(
            _domain_field(value, EmbeddingSpaceDef, "state"),
            field="space.state",
            empty=False,
        ),
        created_at_wall=_builtin_float(
            _domain_field(value, EmbeddingSpaceDef, "created_at_wall")
        ),
    )


def _index_definition(value: Any) -> IndexDefinition:
    """Rebuild a supported index definition, stripping arbitrary domain subclasses."""
    value = _domain_value(value, IndexDefinition, field="index.definition")
    definition_type = (
        VectorIndexDefinition
        if issubclass(type(value), VectorIndexDefinition)
        else IndexDefinition
    )
    return definition_type(
        name=_builtin_text(
            _domain_field(value, IndexDefinition, "name"),
            field="index.name",
            empty=False,
        ),
        table_id=_builtin_int(_domain_field(value, IndexDefinition, "table_id")),
        table_name=_builtin_text(
            _domain_field(value, IndexDefinition, "table_name"),
            field="index.table_name",
            empty=False,
        ),
        positions=tuple(
            _builtin_int(position)
            for position in _tuple_items(
                _domain_field(value, IndexDefinition, "positions"),
                field="index.positions",
            )
        ),
        visibility=_string_enum(
            _domain_field(value, IndexDefinition, "visibility"),
            IndexVisibility,
            field="index.visibility",
        ),
        bucket_count=_builtin_int(
            _domain_field(value, IndexDefinition, "bucket_count")
        ),
        key_derivation=_builtin_text(
            _domain_field(value, IndexDefinition, "key_derivation"),
            field="index.key_derivation",
            empty=False,
        ),
    )


def _scan_failure(value: Any) -> ScanFailure:
    """Rebuild WAL damage provenance with no host-owned leaf."""
    value = _domain_value(value, ScanFailure, field="wal.damage")
    return ScanFailure(
        reason=_string_enum(
            _domain_field(value, ScanFailure, "reason"),
            FailureReason,
            field="wal.damage.reason",
        ),
        segment=_builtin_text(
            _domain_field(value, ScanFailure, "segment"),
            field="wal.damage.segment",
            empty=False,
        ),
        offset=_builtin_int(_domain_field(value, ScanFailure, "offset")),
        length=_builtin_int(_domain_field(value, ScanFailure, "length")),
        expected_lsn=_builtin_int(_domain_field(value, ScanFailure, "expected_lsn")),
        detail=_builtin_text(
            _domain_field(value, ScanFailure, "detail"), field="wal.damage.detail"
        ),
        sample=_builtin_bytes(_domain_field(value, ScanFailure, "sample")),
    )


def _segment_info(value: Any) -> SegmentInfo:
    """Rebuild one WAL segment inventory record with exact leaves."""
    value = _domain_value(value, SegmentInfo, field="wal.segment")
    return SegmentInfo(
        number=_builtin_int(_domain_field(value, SegmentInfo, "number")),
        name=_builtin_text(
            _domain_field(value, SegmentInfo, "name"),
            field="wal.segment.name",
            empty=False,
        ),
        first_lsn=_builtin_int(_domain_field(value, SegmentInfo, "first_lsn")),
        last_lsn=_builtin_int(_domain_field(value, SegmentInfo, "last_lsn")),
        size_bytes=_builtin_int(_domain_field(value, SegmentInfo, "size_bytes")),
        record_count=_builtin_int(_domain_field(value, SegmentInfo, "record_count")),
    )


def _commit_state(value: Any) -> CommitState:
    """Rebuild a published-state record with exact integer leaves."""
    value = _domain_value(value, CommitState, field="transactions.state")
    return CommitState(
        last_committed_lsn=_builtin_int(
            _domain_field(value, CommitState, "last_committed_lsn")
        ),
        last_csn=_builtin_int(_domain_field(value, CommitState, "last_csn")),
        checkpoint_lsn=_builtin_int(
            _domain_field(value, CommitState, "checkpoint_lsn")
        ),
    )


def _damaged_tail(value: Any) -> DamagedTail:
    """Rebuild ledger tail diagnostics without retaining hostile text."""
    value = _domain_value(value, DamagedTail, field="ledger.damage")
    return DamagedTail(
        offset=_builtin_int(_domain_field(value, DamagedTail, "offset")),
        length=_builtin_int(_domain_field(value, DamagedTail, "length")),
        detail=_builtin_text(
            _domain_field(value, DamagedTail, "detail"), field="ledger.damage.detail"
        ),
    )


def _ledger_entry(value: Any) -> LedgerEntry:
    """Rebuild one ledger entry, including its byte payload and exact enum members."""
    value = _domain_value(value, LedgerEntry, field="ledger.entry")
    return LedgerEntry(
        entry_id=_builtin_int(_domain_field(value, LedgerEntry, "entry_id")),
        origin_class=_integer_enum(
            _domain_field(value, LedgerEntry, "origin_class"),
            LedgerOriginClass,
            field="ledger.origin_class",
        ),
        reason=_integer_enum(
            _domain_field(value, LedgerEntry, "reason"),
            LedgerReason,
            field="ledger.reason",
        ),
        payload=_builtin_bytes(_domain_field(value, LedgerEntry, "payload")),
        entry_type=_integer_enum(
            _domain_field(value, LedgerEntry, "entry_type"),
            LedgerEntryType,
            field="ledger.entry_type",
        ),
        lsn_start=_builtin_int(_domain_field(value, LedgerEntry, "lsn_start")),
        lsn_end=_builtin_int(_domain_field(value, LedgerEntry, "lsn_end")),
        epoch=_builtin_int(_domain_field(value, LedgerEntry, "epoch")),
        captured_at_wall=_builtin_float(
            _domain_field(value, LedgerEntry, "captured_at_wall")
        ),
        format_version=_builtin_int(
            _domain_field(value, LedgerEntry, "format_version")
        ),
        reserved=_builtin_int(_domain_field(value, LedgerEntry, "reserved")),
    )


def _quarantine_manifest(value: Any) -> QuarantineManifest:
    """Rebuild the complete quarantine manifest with exact scalar leaves."""
    value = _domain_value(value, QuarantineManifest, field="quarantine.manifest")
    return QuarantineManifest(
        origin=_builtin_text(
            _domain_field(value, QuarantineManifest, "origin"),
            field="quarantine.manifest.origin",
            empty=False,
        ),
        offset=_builtin_int(_domain_field(value, QuarantineManifest, "offset")),
        length=_builtin_int(_domain_field(value, QuarantineManifest, "length")),
        reason=_builtin_text(
            _domain_field(value, QuarantineManifest, "reason"),
            field="quarantine.manifest.reason",
            empty=False,
        ),
        detail=_builtin_text(
            _domain_field(value, QuarantineManifest, "detail"),
            field="quarantine.manifest.detail",
        ),
        captured_at_wall=_builtin_float(
            _domain_field(value, QuarantineManifest, "captured_at_wall")
        ),
        digest=_builtin_text(
            _domain_field(value, QuarantineManifest, "digest"),
            field="quarantine.manifest.digest",
            empty=False,
        ),
        payload_file=_builtin_text(
            _domain_field(value, QuarantineManifest, "payload_file"),
            field="quarantine.manifest.payload_file",
            empty=False,
        ),
        entry_name=_builtin_text(
            _domain_field(value, QuarantineManifest, "entry_name"),
            field="quarantine.manifest.entry_name",
            empty=False,
        ),
        expected_lsn=_builtin_int(
            _domain_field(value, QuarantineManifest, "expected_lsn")
        ),
        schema=_builtin_int(_domain_field(value, QuarantineManifest, "schema")),
    )


def _quarantine_entry(value: Any) -> QuarantineEntry:
    """Rebuild one quarantine listing entry and its nested manifest."""
    value = _domain_value(value, QuarantineEntry, field="quarantine.entry")
    return QuarantineEntry(
        name=_builtin_text(
            _domain_field(value, QuarantineEntry, "name"),
            field="quarantine.entry.name",
            empty=False,
        ),
        manifest=_quarantine_manifest(
            _domain_field(value, QuarantineEntry, "manifest")
        ),
        payload_file=_builtin_text(
            _domain_field(value, QuarantineEntry, "payload_file"),
            field="quarantine.entry.payload_file",
            empty=False,
        ),
        manifest_file=_builtin_text(
            _domain_field(value, QuarantineEntry, "manifest_file"),
            field="quarantine.entry.manifest_file",
            empty=False,
        ),
    )


_QUARANTINE_INVENTORY_STATES: tuple[str, ...] = (
    "complete",
    "incomplete",
    "corrupt_manifest",
    "unreadable_manifest",
    "missing_payload",
    "manifest_mismatch",
    "unexpected_layout",
)
"""Closed public spelling of every conclusive quarantine inventory state."""


# These constants deliberately mirror the portable logical-name grammar owned by the storage
# adapter.  Importing that adapter here would reverse the engine dependency boundary (G2).
_QUARANTINE_MAX_LOGICAL_NAME_LENGTH = 255
_QUARANTINE_MAX_PATH_COMPONENT_LENGTH = 128
_QUARANTINE_PENDING_DELETE_MARKER = ".pending-delete-"
_QUARANTINE_FORBIDDEN_PATH_CHARACTERS = frozenset('<>:"|?*\\')
_QUARANTINE_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _quarantine_path_component(value: str) -> bool:
    """Return whether an exact string is one canonical storage-path component."""
    return (
        bool(value)
        and len(value) <= _QUARANTINE_MAX_PATH_COMPONENT_LENGTH
        and value not in (".", "..")
        and "/" not in value
        and value == value.strip()
        and not value.endswith(".")
        and all(
            character not in _QUARANTINE_FORBIDDEN_PATH_CHARACTERS
            and 32 <= ord(character) < 127
            for character in value
        )
        and value.split(".", 1)[0].upper() not in _QUARANTINE_RESERVED_DEVICE_NAMES
        and _QUARANTINE_PENDING_DELETE_MARKER not in value.lower()
    )


def _quarantine_logical_name(value: str) -> bool:
    """Return whether an exact string is one canonical relative storage logical name."""
    return (
        bool(value)
        and len(value) <= _QUARANTINE_MAX_LOGICAL_NAME_LENGTH
        and not value.startswith("/")
        and not (len(value) >= 2 and value[1] == ":")
        and all(_quarantine_path_component(segment) for segment in value.split("/"))
    )


def _quarantine_restore_receipt(file: str, directory: str) -> bool:
    """Classify a receipt without letting an invalid numeric envelope escape untyped."""
    try:
        return _is_restore_receipt(file, directory)
    except (OverflowError, ValueError):
        return False


def _quarantine_inventory_item(
    value: Any, *, directory: str | None = None
) -> QuarantineInventoryItem:
    """Rebuild one full inventory item without retaining or dispatching its source."""
    value = _domain_value(
        value, QuarantineInventoryItem, field="quarantine.inventory.item"
    )
    state_text = _builtin_text(
        _domain_field(value, QuarantineInventoryItem, "state"),
        field="quarantine.inventory.state",
        empty=False,
    )
    if state_text not in _QUARANTINE_INVENTORY_STATES:
        raise GrafxConfigurationError(
            f"The quarantine inventory state {state_text!r} is not supported.",
            field="quarantine.inventory.state",
            value=state_text,
        )
    manifest_value = _domain_field(value, QuarantineInventoryItem, "manifest")
    item = QuarantineInventoryItem(
        name=_builtin_text(
            _domain_field(value, QuarantineInventoryItem, "name"),
            field="quarantine.inventory.name",
            empty=False,
        ),
        state=cast(QuarantineInventoryState, state_text),
        files=tuple(
            _builtin_text(
                file,
                field="quarantine.inventory.file",
                empty=False,
            )
            for file in _tuple_items(
                _domain_field(value, QuarantineInventoryItem, "files"),
                field="quarantine.inventory.files",
            )
        ),
        manifest_file=_builtin_optional_text(
            _domain_field(value, QuarantineInventoryItem, "manifest_file"),
            field="quarantine.inventory.manifest_file",
        ),
        payload_file=_builtin_optional_text(
            _domain_field(value, QuarantineInventoryItem, "payload_file"),
            field="quarantine.inventory.payload_file",
        ),
        manifest=(
            None if manifest_value is None else _quarantine_manifest(manifest_value)
        ),
        detail=_builtin_text(
            _domain_field(value, QuarantineInventoryItem, "detail"),
            field="quarantine.inventory.detail",
        ),
    )
    if item.state == "complete":
        manifest = item.manifest
        manifest_file = item.manifest_file
        payload_file = item.payload_file
        coherent = (
            manifest is not None
            and manifest_file is not None
            and payload_file is not None
        )
        if coherent:
            try:
                expected_name = (
                    f"{stamp_of(manifest.captured_at_wall)}-{manifest.suffix}"
                )
                expected_payload_leaf = _payload_file_name(manifest.origin)
            except (GrafxConfigurationError, OverflowError, ValueError):
                coherent = False
            else:
                manifest_parent, manifest_separator, manifest_leaf = (
                    manifest_file.rpartition("/")
                )
                payload_parent, payload_separator, payload_leaf = (
                    payload_file.rpartition("/")
                )
                if directory is None:
                    parent_directory, parent_separator, parent_name = (
                        manifest_parent.rpartition("/")
                    )
                    root_is_canonical = (
                        bool(parent_separator)
                        and parent_name == item.name
                        and _quarantine_logical_name(parent_directory)
                    )
                    expected_parent = manifest_parent
                else:
                    root_is_canonical = _quarantine_logical_name(directory)
                    expected_parent = f"{directory}/{item.name}"
                expected_manifest_file = f"{expected_parent}/{MANIFEST_FILE_NAME}"
                expected_payload_file = f"{expected_parent}/{expected_payload_leaf}"
                folded_payload_leaf = payload_leaf.casefold()
                folded_files = tuple(file.casefold() for file in item.files)
                required_files = (expected_manifest_file, expected_payload_file)
                coherent = (
                    root_is_canonical
                    and _quarantine_path_component(item.name)
                    and _quarantine_path_component(payload_leaf)
                    and all(_quarantine_logical_name(file) for file in item.files)
                    and bool(manifest_separator)
                    and bool(payload_separator)
                    and item.name == expected_name
                    and manifest_leaf == MANIFEST_FILE_NAME
                    and payload_leaf == expected_payload_leaf
                    and folded_payload_leaf != MANIFEST_FILE_NAME.casefold()
                    and not folded_payload_leaf.startswith(
                        RESTORE_RECEIPT_PREFIX.casefold()
                    )
                    and manifest_parent == expected_parent
                    and payload_parent == expected_parent
                    and manifest_file == expected_manifest_file
                    and payload_file == expected_payload_file
                    and manifest_file in item.files
                    and payload_file in item.files
                    and len(set(item.files)) == len(item.files)
                    and len(set(folded_files)) == len(folded_files)
                    and all(
                        file in required_files
                        or _quarantine_restore_receipt(file, expected_parent)
                        for file in item.files
                    )
                    and manifest.entry_name == item.name
                    and manifest.payload_file == payload_file
                    and item.detail == ""
                )
        if not coherent:
            raise GrafxConfigurationError(
                "A complete quarantine inventory item must carry a self-consistent "
                "manifest and both observed canonical files.",
                field="quarantine.inventory.item",
                value="incomplete_complete_item",
            )
    return item


def _recovery_finding(value: Any) -> RecoveryFinding:
    """Rebuild one recovery finding with exact public leaves."""
    value = _domain_value(value, RecoveryFinding, field="recovery.finding")
    return RecoveryFinding(
        kind=_builtin_text(
            _domain_field(value, RecoveryFinding, "kind"),
            field="recovery.finding.kind",
            empty=False,
        ),
        detail=_builtin_text(
            _domain_field(value, RecoveryFinding, "detail"),
            field="recovery.finding.detail",
            empty=False,
        ),
        file=_builtin_text(
            _domain_field(value, RecoveryFinding, "file"),
            field="recovery.finding.file",
        ),
        offset=_builtin_int(_domain_field(value, RecoveryFinding, "offset")),
        length=_builtin_int(_domain_field(value, RecoveryFinding, "length")),
        lsn=_builtin_int(_domain_field(value, RecoveryFinding, "lsn")),
        page=_builtin_int(_domain_field(value, RecoveryFinding, "page")),
        entry_id=_builtin_int(_domain_field(value, RecoveryFinding, "entry_id")),
        quarantine=_builtin_text(
            _domain_field(value, RecoveryFinding, "quarantine"),
            field="recovery.finding.quarantine",
        ),
    )


def _recovery_report_view(value: object) -> RecoveryReport | None:
    """Return a deeply canonical recovery report for the public facade."""
    if value is None:
        return None
    value = _domain_value(value, RecoveryReport, field="recovery_report")
    return RecoveryReport(
        outcome=_builtin_text(
            _domain_field(value, RecoveryReport, "outcome"),
            field="recovery.outcome",
            empty=False,
        ),
        records_replayed=_builtin_int(
            _domain_field(value, RecoveryReport, "records_replayed")
        ),
        records_discarded=_builtin_int(
            _domain_field(value, RecoveryReport, "records_discarded")
        ),
        ledger_entries_created=_builtin_int(
            _domain_field(value, RecoveryReport, "ledger_entries_created")
        ),
        last_good_lsn=_builtin_int(
            _domain_field(value, RecoveryReport, "last_good_lsn")
        ),
        findings=tuple(
            _recovery_finding(finding)
            for finding in _tuple_items(
                _domain_field(value, RecoveryReport, "findings"),
                field="recovery.findings",
            )
        ),
    )


def _metrics_snapshot_view(value: object) -> MetricsSnapshotView:
    """Detach and deeply freeze the JSON-like value returned by a metrics sink.

    ``MetricsSink.snapshot`` is a port and a caller-supplied implementation may return a live
    dictionary, a mapping proxy over one, scalar subclasses carrying callbacks, or an arbitrary
    object.  A shallow ``dict`` copy only severs the outer alias.  This walk rebuilds every
    supported container and scalar into exact built-ins and refuses cycles and capability
    leaves. The resulting views contain only tuples owned by the returned snapshot.
    """
    if not isinstance(value, Mapping):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"A metrics snapshot must be a mapping; got {observed}.",
            field="metrics.snapshot",
            value=observed,
        )
    return _metric_mapping(value, field="metrics.snapshot", active=set())


def _metric_mapping(
    value: Mapping[object, object], *, field: str, active: set[int]
) -> MetricsSnapshotView:
    """Copy one metrics mapping without retaining its source or any nested mutable value."""
    marker = id(value)
    if marker in active:
        raise GrafxConfigurationError(
            "A metrics snapshot cannot contain a recursive mapping or sequence.",
            field=field,
            value="cycle",
        )
    active.add(marker)
    try:
        value_type = type(value)
        if issubclass(value_type, dict):
            pairs = tuple(dict.items(value))
        else:
            # A custom Mapping is executable host code.  It is called only at this explicit
            # observation boundary; Database translates an ordinary failure and lets process
            # control signals pass unchanged.
            pairs = tuple(value.items())
        detached: dict[str, object] = {}
        for raw_key, raw_value in pairs:
            key = _builtin_text(raw_key, field=f"{field}.key", empty=False)
            detached[key] = _metric_value(
                raw_value,
                field=f"{field}.{key}",
                active=active,
            )
        return MetricsSnapshotView(tuple(detached.items()))
    finally:
        active.remove(marker)


def _metric_value(value: object, *, field: str, active: set[int]) -> object:
    """Return one deeply immutable metrics leaf or container."""
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if issubclass(value_type, str):
        return _builtin_text(value, field=field)
    if issubclass(value_type, int) and value_type is not bool:
        return _builtin_int(value, field=field)
    if issubclass(value_type, float):
        return _finite_float(value, field=field)
    if issubclass(value_type, (bytes, bytearray, memoryview)):
        return _builtin_bytes(value, field=field)
    if isinstance(value, Mapping):
        return _metric_mapping(value, field=field, active=active)
    if issubclass(value_type, (tuple, list)):
        marker = id(value)
        if marker in active:
            raise GrafxConfigurationError(
                "A metrics snapshot cannot contain a recursive mapping or sequence.",
                field=field,
                value="cycle",
            )
        active.add(marker)
        try:
            items = (
                tuple(tuple.__iter__(value))
                if issubclass(value_type, tuple)
                else tuple(list.__iter__(value))
            )
            return tuple(
                _metric_value(item, field=f"{field}[]", active=active) for item in items
            )
        finally:
            active.remove(marker)
    observed = _builtin_type_name(value)
    raise GrafxConfigurationError(
        f"The {field} value of a metrics snapshot is not an immutable scalar or container; "
        f"got {observed}.",
        field=field,
        value=observed,
    )


def _query_parameters_snapshot(
    value: Mapping[str, object] | None,
) -> dict[str, Value]:
    """Return one bounded, deeply owned parameter mapping before page access begins.

    A mapping is executable Python: ``items()``, iteration and value conversion may all call
    host code.  The facade invokes those doors while no page-access section is held, copies every
    supported value into an exact domain shape and then rechecks transaction liveness under the
    section.  Consequently a callback may roll back or close, but it cannot withdraw a reader pin
    halfway through a query engine operation.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"Query parameters must be a mapping; got {observed}.",
            field="parameters",
            value=observed,
        )
    marker = id(value)
    active = {marker}
    detached: dict[str, Value] = {}
    for position, (raw_name, raw_value) in enumerate(
        _bounded_mapping_pairs(value, limit=MAX_PARAMETERS, field="parameters")
    ):
        name = _builtin_text(raw_name, field=f"parameters[{position}].name", empty=False)
        if len(name) > MAX_NAME_CHARACTERS:
            raise GrafxConfigurationError(
                f"A parameter name may carry at most {MAX_NAME_CHARACTERS} characters.",
                field="parameters.name",
                value=len(name),
                limit=MAX_NAME_CHARACTERS,
            )
        if name in detached:
            raise GrafxConfigurationError(
                f"Two query parameters canonicalize to the same name {name!r}.",
                field="parameters",
                value=name,
                reason="duplicate",
            )
        detached[name] = _query_value_snapshot(
            raw_value,
            field=f"parameters.{name}",
            depth=0,
            active=active,
        )
    return detached


def _query_text_snapshot(value: object) -> str:
    """Return exact bounded query text before the page-access section is entered."""
    text = _builtin_text(value, field="statement", empty=False)
    if len(text) > MAX_QUERY_CHARACTERS:
        raise GrafxParseError(
            f"A query may carry at most {MAX_QUERY_CHARACTERS} characters; got {len(text)}.",
            field="text",
            value=len(text),
        )
    return text


def _query_value_snapshot(
    value: object,
    *,
    field: str,
    depth: int,
    active: set[int],
) -> Value:
    """Copy one query value into an exact, bounded and capability-free value graph."""
    if depth > MAX_VALUE_DEPTH:
        raise GrafxConfigurationError(
            f"A query value may nest at most {MAX_VALUE_DEPTH} levels deep.",
            field=field,
            value=depth,
            limit=MAX_VALUE_DEPTH,
        )
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if issubclass(value_type, int):
        plain_integer = _builtin_int(value, field=field)
        if not INT64_MIN <= plain_integer <= INT64_MAX:
            raise GrafxConfigurationError(
                "A query integer must fit in 64 signed bits.",
                field=field,
                value=plain_integer,
                minimum=INT64_MIN,
                maximum=INT64_MAX,
            )
        return plain_integer
    if issubclass(value_type, float):
        return float.__float__(value)
    if issubclass(value_type, str):
        plain_text = _builtin_text(value, field=field)
        if len(plain_text) > MAX_STRING_CHARACTERS:
            raise GrafxConfigurationError(
                f"A query string may carry at most {MAX_STRING_CHARACTERS} characters.",
                field=field,
                value=len(plain_text),
                limit=MAX_STRING_CHARACTERS,
            )
        return plain_text
    if issubclass(value_type, (bytes, bytearray, memoryview)):
        return _builtin_bytes(value, field=field)
    if issubclass(value_type, Timestamp):
        source = _domain_value(value, Timestamp, field=field)
        micros = _builtin_int(
            _domain_field(source, Timestamp, "micros"),
            field=f"{field}.micros",
        )
        if not INT64_MIN <= micros <= INT64_MAX:
            raise GrafxConfigurationError(
                "A query timestamp must fit in 64 signed bits.",
                field=field,
                value=micros,
                minimum=INT64_MIN,
                maximum=INT64_MAX,
            )
        return Timestamp(micros=micros)
    if issubclass(value_type, Uuid):
        source = _domain_value(value, Uuid, field=field)
        return Uuid(raw=_builtin_bytes(_domain_field(source, Uuid, "raw"), field=field))
    if issubclass(value_type, VectorValue):
        return _vector_query_snapshot(value)
    if isinstance(value, Mapping):
        return _query_mapping_snapshot(value, field=field, depth=depth, active=active)
    if isinstance(value, Sequence):
        return _query_sequence_snapshot(value, field=field, depth=depth, active=active)
    observed = _builtin_type_name(value)
    raise GrafxConfigurationError(
        f"The query value at {field} cannot retain a {observed} capability.",
        field=field,
        value=observed,
        reason="unsupported_value",
    )


def _query_mapping_snapshot(
    value: Mapping[object, object],
    *,
    field: str,
    depth: int,
    active: set[int],
) -> dict[Value, Value]:
    """Copy one bounded map, rejecting cycles and canonical-key collisions."""
    marker = id(value)
    if marker in active:
        raise GrafxConfigurationError(
            "A query value cannot contain a recursive mapping or sequence.",
            field=field,
            value="cycle",
        )
    active.add(marker)
    try:
        detached: dict[Value, Value] = {}
        for position, (raw_key, raw_value) in enumerate(
            _bounded_mapping_pairs(value, limit=MAX_MAP_ENTRIES, field=field)
        ):
            key = _query_value_snapshot(
                raw_key,
                field=f"{field}.key[{position}]",
                depth=depth + 1,
                active=active,
            )
            item = _query_value_snapshot(
                raw_value,
                field=f"{field}[{position}]",
                depth=depth + 1,
                active=active,
            )
            try:
                duplicate = key in detached
            except TypeError as failure:
                raise GrafxConfigurationError(
                    "A query map key must canonicalize to a hashable value.",
                    field=f"{field}.key[{position}]",
                    value=_builtin_type_name(key),
                ) from failure
            if duplicate:
                raise GrafxConfigurationError(
                    "Two query map keys become equal after canonicalization.",
                    field=f"{field}.key[{position}]",
                    value="collision",
                    reason="duplicate",
                )
            detached[key] = item
        return detached
    finally:
        active.remove(marker)


def _query_sequence_snapshot(
    value: Sequence[object],
    *,
    field: str,
    depth: int,
    active: set[int],
) -> tuple[Value, ...]:
    """Copy one bounded sequence without invoking list or tuple subclass overrides."""
    marker = id(value)
    if marker in active:
        raise GrafxConfigurationError(
            "A query value cannot contain a recursive mapping or sequence.",
            field=field,
            value="cycle",
        )
    active.add(marker)
    try:
        iterator: Iterator[object]
        if issubclass(type(value), tuple):
            iterator = tuple.__iter__(value)
        elif issubclass(type(value), list):
            iterator = list.__iter__(value)
        else:
            iterator = iter(value)
        detached: list[Value] = []
        for item in iterator:
            if len(detached) >= MAX_LIST_ELEMENTS:
                raise GrafxConfigurationError(
                    f"A query list may hold at most {MAX_LIST_ELEMENTS} elements.",
                    field=field,
                    value=len(detached) + 1,
                    limit=MAX_LIST_ELEMENTS,
                )
            detached.append(
                _query_value_snapshot(
                    item,
                    field=f"{field}[{len(detached)}]",
                    depth=depth + 1,
                    active=active,
                )
            )
        return tuple(detached)
    finally:
        active.remove(marker)


def _bounded_mapping_pairs(
    value: Mapping[object, object], *, limit: int, field: str
) -> Iterator[tuple[object, object]]:
    """Yield at most ``limit`` mapping pairs and refuse the first pair beyond it."""
    pairs = dict.items(value) if issubclass(type(value), dict) else value.items()
    for position, pair in enumerate(pairs):
        if position >= limit:
            raise GrafxConfigurationError(
                f"The {field} mapping may hold at most {limit} entries.",
                field=field,
                value=position + 1,
                limit=limit,
            )
        try:
            raw_key, raw_value = pair
        except (TypeError, ValueError) as failure:
            raise GrafxConfigurationError(
                f"The {field} mapping returned an entry that is not a key/value pair.",
                field=field,
                value=_builtin_type_name(pair),
            ) from failure
        yield raw_key, raw_value


_QUERY_PLAN_NODE_TYPES: frozenset[type[PlanNode]] = frozenset(
    {
        AggregateRows,
        AllNodesScan,
        CreateNodeTable,
        CreateRelationships,
        CreateRelTable,
        CreateVectorSpace,
        DeleteEntities,
        DistinctRows,
        EagerRows,
        FilterRows,
        IndexSeek,
        LimitRows,
        MergePattern,
        NodeScan,
        ProduceResults,
        ProjectRows,
        SetProperties,
        SingleRow,
        SkipRows,
        SortRows,
        TraverseRelationship,
        UnwindRows,
        VectorSearch,
        WithRows,
    }
)
"""Every exact operator implementation the frozen query planner may publish."""


_QUERY_PLAN_EXPRESSION_TYPES: frozenset[type[Expression]] = frozenset(
    {
        BinaryOperation,
        CaseExpression,
        FunctionCall,
        ListExpression,
        Literal,
        MapExpression,
        NullCheck,
        Parameter,
        Property,
        Subscript,
        UnaryOperation,
        Variable,
    }
)
"""Every exact expression implementation that can be embedded in a public plan."""


_QUERY_PLAN_AUXILIARY_TYPES: frozenset[type[object]] = frozenset(
    {
        Aggregation,
        CaseAlternative,
        ColumnDef,
        CreatedNode,
        CreatedRelationship,
        MapEntry,
        NamedArgument,
        PropertyAssignment,
        ReturnItem,
        SortItem,
        TableDef,
    }
)
"""Exact frozen dataclasses reachable from operator fields, excluding expressions."""


def _query_plan_view(value: object) -> PlanNode:
    """Rebuild a capability-free plan after checking its exact bounded grammar."""
    try:
        nodes = _query_plan_nodes(value)
        detached: dict[int, PlanNode] = {}
        for node in reversed(nodes):
            clone = _query_plan_dataclass_snapshot(
                node,
                expected=type(node),
                detached_nodes=detached,
                active=set(),
                expression_depth=0,
            )
            detached[id(node)] = clone  # type: ignore[assignment]
        root = detached[id(value)]
        return validate_plan(root)
    except GrafxPlanError:
        raise
    except Exception as failure:  # noqa: BLE001 - malformed collaborator output is a plan error
        observed = _builtin_type_name(failure)
        raise GrafxPlanError(
            f"The query collaborator returned a malformed plan field ({observed}).",
            field="plan",
            value="malformed",
            cause=observed,
        ) from failure


def _query_plan_nodes(value: object) -> tuple[PlanNode, ...]:
    """Return exact plan nodes parents-first without calling a subclass virtual door."""
    pending: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    nodes: list[PlanNode] = []
    while pending:
        node, depth = pending.pop()
        if type(node) not in _QUERY_PLAN_NODE_TYPES:
            observed = _builtin_type_name(node)
            raise GrafxPlanError(
                f"A public query plan may contain only built-in operators; got {observed}.",
                field="plan",
                value=observed,
            )
        if depth > MAX_PLAN_DEPTH:
            raise GrafxPlanError(
                f"An operator tree may be at most {MAX_PLAN_DEPTH} operators deep.",
                field="depth",
                value=MAX_PLAN_DEPTH,
            )
        marker = id(node)
        if marker in seen:
            raise GrafxPlanError(
                "A public query plan must be a tree without shared or cyclic operators.",
                field="plan",
                value="not_a_tree",
            )
        seen.add(marker)
        nodes.append(node)  # type: ignore[arg-type]
        # Exact-type validation above is deliberately before this virtual door. Every accepted
        # implementation is frozen in the static allowlist, so a collaborator subclass never
        # gets to run children(), label or details() during validation or escape to the caller.
        children = node.children()  # type: ignore[union-attr]
        if type(children) is not tuple:
            raise GrafxPlanError(
                "A query operator must publish its children as a tuple.",
                field="plan",
                value="invalid_children",
            )
        for child in reversed(tuple(tuple.__iter__(children))):
            pending.append((child, depth + 1))
    return tuple(nodes)


@lru_cache(maxsize=None)
def _query_plan_type_hints(expected: type[object]) -> Mapping[str, object]:
    """Resolve the frozen field grammar once for one exact allowlisted dataclass."""
    return get_type_hints(expected)


def _query_plan_dataclass_snapshot(
    value: object,
    *,
    expected: type[object],
    detached_nodes: Mapping[int, PlanNode],
    active: set[int],
    expression_depth: int,
) -> object:
    """Reconstruct one exact plan, expression, schema or auxiliary dataclass by base slots."""
    allowed = (
        expected in _QUERY_PLAN_NODE_TYPES
        or expected in _QUERY_PLAN_EXPRESSION_TYPES
        or expected in _QUERY_PLAN_AUXILIARY_TYPES
    )
    if not allowed or type(value) is not expected:
        observed = _builtin_type_name(value)
        raise GrafxPlanError(
            f"A query plan field expected exact {expected.__name__}; got {observed}.",
            field="plan",
            value=observed,
        )
    marker = id(value)
    if marker in active:
        raise GrafxPlanError(
            "A query plan field graph cannot contain a cycle.",
            field="plan",
            value="cycle",
        )
    if expected in _QUERY_PLAN_EXPRESSION_TYPES and expression_depth > MAX_EXPRESSION_DEPTH:
        raise GrafxPlanError(
            f"A plan expression may nest at most {MAX_EXPRESSION_DEPTH} levels deep.",
            field="expression",
            value=expression_depth,
            limit=MAX_EXPRESSION_DEPTH,
        )
    active.add(marker)
    try:
        if expected is Literal:
            raw_literal = _domain_field(value, Literal, "value")
            return Literal(
                value=_query_value_snapshot(
                    raw_literal,
                    field="plan.literal",
                    depth=0,
                    active=set(),
                )
            )
        hints = _query_plan_type_hints(expected)
        child_depth = (
            expression_depth + 1
            if expected in _QUERY_PLAN_EXPRESSION_TYPES
            else expression_depth
        )
        arguments: dict[str, object] = {}
        for declared in fields(expected):
            raw_field = _domain_field(value, expected, declared.name)
            annotation = hints[declared.name]
            arguments[declared.name] = _query_plan_field_snapshot(
                raw_field,
                annotation=annotation,
                detached_nodes=detached_nodes,
                active=active,
                expression_depth=child_depth,
                field=f"plan.{expected.__name__}.{declared.name}",
                string_limit=(
                    MAX_RENDERED_QUERY_CHARACTERS
                    if (
                        expected is ProduceResults and declared.name == "columns"
                    )
                    or (expected is ReturnItem and declared.name == "alias")
                    else None
                ),
            )
        if expected is ProduceResults:
            columns = arguments["columns"]
            if type(columns) is not tuple:  # pragma: no cover - grammar proves this above
                raise AssertionError("ProduceResults.columns did not clone to a tuple")
            seen_columns: set[str] = set()
            for column in columns:
                if column in seen_columns:
                    raise GrafxPlanError(
                        f"A public query plan cannot publish duplicate column {column!r}.",
                        field="plan.ProduceResults.columns",
                        value=column,
                        reason="duplicate",
                    )
                seen_columns.add(column)
        return expected(**arguments)
    finally:
        active.remove(marker)


def _query_plan_field_snapshot(
    value: object,
    *,
    annotation: object,
    detached_nodes: Mapping[int, PlanNode],
    active: set[int],
    expression_depth: int,
    field: str,
    string_limit: int | None = None,
) -> object:
    """Clone a field according to the closed type annotation of its exact owner class."""
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is _PEP604_UNION_TYPE:
        if value is None and type(None) in arguments:
            return None
        choices = tuple(item for item in arguments if item is not type(None))
        if len(choices) != 1:
            raise GrafxPlanError(
                "A query plan field has an unsupported union grammar.",
                field=field,
                value="union",
            )
        return _query_plan_field_snapshot(
            value,
            annotation=choices[0],
            detached_nodes=detached_nodes,
            active=active,
            expression_depth=expression_depth,
            field=field,
            string_limit=string_limit,
        )
    if origin is tuple:
        if not issubclass(type(value), tuple):
            raise GrafxPlanError(
                "A query plan collection must be a tuple.",
                field=field,
                value=_builtin_type_name(value),
            )
        marker = id(value)
        if marker in active:
            raise GrafxPlanError(
                "A query plan collection cannot contain a cycle.",
                field=field,
                value="cycle",
            )
        active.add(marker)
        try:
            raw_items = tuple(tuple.__iter__(value))
            if len(raw_items) > MAX_LIST_ELEMENTS:
                raise GrafxPlanError(
                    f"A plan collection may hold at most {MAX_LIST_ELEMENTS} entries.",
                    field=field,
                    value=len(raw_items),
                    limit=MAX_LIST_ELEMENTS,
                )
            if len(arguments) != 2 or arguments[1] is not Ellipsis:
                raise GrafxPlanError(
                    "A query plan tuple must declare one repeated item type.",
                    field=field,
                    value="tuple_grammar",
                )
            return tuple(
                _query_plan_field_snapshot(
                    item,
                    annotation=arguments[0],
                    detached_nodes=detached_nodes,
                    active=active,
                    expression_depth=expression_depth,
                    field=f"{field}[{position}]",
                    string_limit=string_limit,
                )
                for position, item in enumerate(raw_items)
            )
        finally:
            active.remove(marker)
    if annotation is PlanNode:
        if type(value) not in _QUERY_PLAN_NODE_TYPES or id(value) not in detached_nodes:
            raise GrafxPlanError(
                "A query operator child is outside the validated tree.",
                field=field,
                value=_builtin_type_name(value),
            )
        return detached_nodes[id(value)]
    if annotation is Expression:
        expression_type = type(value)
        if expression_type not in _QUERY_PLAN_EXPRESSION_TYPES:
            raise GrafxPlanError(
                "A query plan contains an unsupported expression implementation.",
                field=field,
                value=_builtin_type_name(value),
            )
        return _query_plan_dataclass_snapshot(
            value,
            expected=expression_type,
            detached_nodes=detached_nodes,
            active=active,
            expression_depth=expression_depth,
        )
    if annotation is str:
        text = _builtin_text(value, field=field)
        limit = MAX_NAME_CHARACTERS if string_limit is None else string_limit
        if len(text) > limit:
            raise GrafxPlanError(
                f"A query plan string at {field} may carry at most {limit} characters.",
                field=field,
                value=len(text),
                limit=limit,
            )
        return text
    if annotation is bool:
        return _builtin_bool(value)
    if annotation is int:
        integer = _builtin_int(value, field=field)
        if not INT64_MIN <= integer <= INT64_MAX:
            raise GrafxPlanError(
                "A query plan integer must fit in 64 signed bits.",
                field=field,
                value=integer,
            )
        return integer
    if annotation is float:
        return _builtin_float(value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not annotation:
            raise GrafxPlanError(
                f"A query plan field expected exact {annotation.__name__}.",
                field=field,
                value=_builtin_type_name(value),
            )
        return annotation(value.value)
    if annotation in _QUERY_PLAN_AUXILIARY_TYPES or annotation in _QUERY_PLAN_EXPRESSION_TYPES:
        return _query_plan_dataclass_snapshot(
            value,
            expected=annotation,  # type: ignore[arg-type]
            detached_nodes=detached_nodes,
            active=active,
            expression_depth=expression_depth,
        )
    raise GrafxPlanError(
        "A query plan field has a type outside the frozen public grammar.",
        field=field,
        value=repr(annotation),
    )


def _query_result_view(value: object) -> QueryResult:
    """Rebuild one result and normalize every malformed collaborator shape as a plan error."""
    try:
        return _query_result_snapshot(value)
    except GrafxPlanError:
        raise
    except Exception as failure:  # noqa: BLE001 - collaborator output is an untrusted plan
        observed = _builtin_type_name(failure)
        raise GrafxPlanError(
            f"The query collaborator returned a malformed result ({observed}).",
            field="result",
            value="malformed",
            cause=observed,
        ) from failure


def _query_result_snapshot(value: object) -> QueryResult:
    """Rebuild one fully materialised query result outside the page-access section."""
    # Local import avoids making the query engine depend on the public-view module that rebuilds
    # its output.  Database calls this only after composition has finished importing both modules.
    from okto_grafx.engine.query_engine import QueryResult

    source = _domain_value(value, QueryResult, field="query.result")
    raw_columns = _tuple_items(
        _domain_field(source, QueryResult, "columns"), field="query.result.columns"
    )
    if len(raw_columns) > MAX_PROJECTION_ITEMS:
        raise GrafxConfigurationError(
            f"A query result may carry at most {MAX_PROJECTION_ITEMS} columns.",
            field="query.result.columns",
            value=len(raw_columns),
            limit=MAX_PROJECTION_ITEMS,
        )
    columns: list[str] = []
    seen_columns: set[str] = set()
    for position, raw_column in enumerate(raw_columns):
        column = _builtin_text(
            raw_column, field=f"query.result.columns[{position}]", empty=False
        )
        if len(column) > MAX_RENDERED_QUERY_CHARACTERS:
            raise GrafxConfigurationError(
                f"A query result column may carry at most "
                f"{MAX_RENDERED_QUERY_CHARACTERS} characters.",
                field="query.result.columns",
                value=len(column),
                limit=MAX_RENDERED_QUERY_CHARACTERS,
            )
        if column in seen_columns:
            raise GrafxConfigurationError(
                f"A query result cannot publish duplicate column {column!r}.",
                field="query.result.columns",
                value=column,
                reason="duplicate",
            )
        seen_columns.add(column)
        columns.append(column)

    raw_rows = _tuple_items(
        _domain_field(source, QueryResult, "rows"), field="query.result.rows"
    )
    rows: list[tuple[Value, ...]] = []
    active: set[int] = set()
    for row_position, raw_row in enumerate(raw_rows):
        row_items = _tuple_items(raw_row, field=f"query.result.rows[{row_position}]")
        if len(row_items) != len(columns):
            raise GrafxConfigurationError(
                "Every query result row must have exactly one value per column.",
                field=f"query.result.rows[{row_position}]",
                value=len(row_items),
                expected=len(columns),
            )
        rows.append(
            tuple(
                _query_value_snapshot(
                    item,
                    field=f"query.result.rows[{row_position}][{column_position}]",
                    depth=0,
                    active=active,
                )
                for column_position, item in enumerate(row_items)
            )
        )

    raw_plan = _domain_field(source, QueryResult, "plan")
    plan = None if raw_plan is None else _query_plan_view(raw_plan)
    statistics = _query_statistics_snapshot(
        _domain_field(source, QueryResult, "statistics")
    )
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(rows),
        plan=plan,
        statistics=statistics,
    )


def _query_statistics_snapshot(value: object) -> dict[str, int]:
    """Return exact, owned and mutable non-negative statement counters."""
    if not isinstance(value, Mapping):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"Query result statistics must be a mapping; got {observed}.",
            field="query.result.statistics",
            value=observed,
        )
    detached: dict[str, int] = {}
    for position, (raw_name, raw_count) in enumerate(
        _bounded_mapping_pairs(
            value,
            limit=MAX_MAP_ENTRIES,
            field="query.result.statistics",
        )
    ):
        name = _builtin_text(
            raw_name,
            field=f"query.result.statistics[{position}].name",
            empty=False,
        )
        if len(name) > MAX_NAME_CHARACTERS:
            raise GrafxConfigurationError(
                f"A query result statistic name may carry at most {MAX_NAME_CHARACTERS} "
                "characters.",
                field="query.result.statistics",
                value=len(name),
                limit=MAX_NAME_CHARACTERS,
            )
        if name in detached:
            raise GrafxConfigurationError(
                f"Query result statistics contain duplicate counter {name!r}.",
                field="query.result.statistics",
                value=name,
                reason="duplicate",
            )
        count = _require_nonnegative_integer(
            f"query.result.statistics.{name}", raw_count
        )
        if count > INT64_MAX:
            raise GrafxConfigurationError(
                "A query result statistic must fit in a signed 64-bit integer.",
                field=f"query.result.statistics.{name}",
                value=count,
                limit=INT64_MAX,
            )
        detached[name] = count
    return detached


def _finite_float(value: object, *, field: str) -> float:
    """Copy one real scalar and refuse values that cannot participate in a ranking."""
    try:
        plain = _builtin_float(value)
    except OverflowError as failure:
        raise GrafxConfigurationError(
            f"The public {field} value is outside the finite float range.",
            field=field,
            value="overflow",
        ) from failure
    if not isfinite(plain):
        raise GrafxConfigurationError(
            f"The public {field} value must be finite.",
            field=field,
            value="non_finite",
        )
    return plain


def _require_nonnegative_integer(field: str, value: object) -> int:
    """Copy a non-negative counter into an exact integer."""
    plain = _builtin_int(value, field=field)
    if plain < 0:
        raise GrafxConfigurationError(
            f"The public {field} value cannot be negative.",
            field=field,
            value=plain,
        )
    return plain


def _require_positive_integer(field: str, value: object) -> int:
    """Copy a positive count into an exact integer."""
    plain = _builtin_int(value, field=field)
    if plain < 1:
        raise GrafxConfigurationError(
            f"The public {field} value must be at least 1.",
            field=field,
            value=plain,
        )
    return plain


def _record_ref_view(value: object, *, field: str) -> RecordRef:
    """Rebuild a record location with exact, encodable integer leaves."""
    value = _domain_value(value, RecordRef, field=field)
    page = _builtin_int(_domain_field(value, RecordRef, "page"), field=f"{field}.page")
    slot = _builtin_int(_domain_field(value, RecordRef, "slot"), field=f"{field}.slot")
    if not 0 <= page <= MAX_PAGE_INDEX or not 0 <= slot <= MAX_SLOT_ID:
        raise GrafxConfigurationError(
            f"The public {field} value is outside the encodable record-reference range.",
            field=field,
            value="out_of_range",
        )
    return RecordRef(page=page, slot=slot)


def _index_entry_view(value: object) -> IndexEntry:
    """Rebuild one secondary-index entry without retaining engine-owned DTO state."""
    value = _domain_value(value, IndexEntry, field="index.entry")
    page = _builtin_int(
        _domain_field(value, IndexEntry, "page"), field="index.entry.page"
    )
    slot = _builtin_int(
        _domain_field(value, IndexEntry, "slot"), field="index.entry.slot"
    )
    if not 0 <= page <= MAX_PAGE_INDEX or not 0 <= slot <= MAX_SLOT_ID:
        raise GrafxConfigurationError(
            "A public index entry location is outside the encodable page and slot range.",
            field="index.entry.location",
            value="out_of_range",
        )
    return IndexEntry(
        key=_builtin_bytes(
            _domain_field(value, IndexEntry, "key"), field="index.entry.key"
        ),
        ref=_record_ref_view(
            _domain_field(value, IndexEntry, "ref"), field="index.entry.ref"
        ),
        versioned=_builtin_bool(_domain_field(value, IndexEntry, "versioned")),
        born_csn=_builtin_int(
            _domain_field(value, IndexEntry, "born_csn"),
            field="index.entry.born_csn",
        ),
        dead_csn=_builtin_int(
            _domain_field(value, IndexEntry, "dead_csn"),
            field="index.entry.dead_csn",
        ),
        page=page,
        slot=slot,
    )


def _finding_location_view(value: object) -> FindingLocation:
    """Rebuild the complete location of a verification finding."""
    value = _domain_value(value, FindingLocation, field="verify.finding.location")
    location = FindingLocation(
        file=_builtin_text(
            _domain_field(value, FindingLocation, "file"),
            field="verify.finding.location.file",
        ),
        page=_builtin_int(
            _domain_field(value, FindingLocation, "page"),
            field="verify.finding.location.page",
        ),
        slot=_builtin_int(
            _domain_field(value, FindingLocation, "slot"),
            field="verify.finding.location.slot",
        ),
        lsn=_builtin_int(
            _domain_field(value, FindingLocation, "lsn"),
            field="verify.finding.location.lsn",
        ),
        index=_builtin_text(
            _domain_field(value, FindingLocation, "index"),
            field="verify.finding.location.index",
        ),
    )
    if location.page != NOT_APPLICABLE and not 0 <= location.page <= MAX_PAGE_INDEX:
        raise GrafxConfigurationError(
            "A verification finding page must be not-applicable or an encodable page index.",
            field="verify.finding.location.page",
            value="out_of_range",
        )
    if location.slot != NOT_APPLICABLE and not 0 <= location.slot <= MAX_SLOT_ID:
        raise GrafxConfigurationError(
            "A verification finding slot must be not-applicable or an encodable slot id.",
            field="verify.finding.location.slot",
            value="out_of_range",
        )
    if not 0 <= location.lsn <= MAX_U64:
        raise GrafxConfigurationError(
            "A verification finding sequence number must fit the unsigned 64-bit field.",
            field="verify.finding.location.lsn",
            value="out_of_range",
        )
    return location


def _verification_finding_view(value: object) -> VerificationFinding:
    """Rebuild one verification finding and its nested location."""
    value = _domain_value(value, VerificationFinding, field="verify.finding")
    kind = _builtin_text(
        _domain_field(value, VerificationFinding, "kind"),
        field="verify.finding.kind",
        empty=False,
    )
    if kind not in VERIFICATION_FINDING_KINDS:
        raise GrafxConfigurationError(
            "A verification finding must use the closed machine-readable vocabulary.",
            field="verify.finding.kind",
            value=kind,
        )
    return VerificationFinding(
        kind=kind,
        location=_finding_location_view(
            _domain_field(value, VerificationFinding, "location")
        ),
        detail=_builtin_text(
            _domain_field(value, VerificationFinding, "detail"),
            field="verify.finding.detail",
            empty=False,
        ),
    )


def _verification_report_view(
    value: object, *, requested_scope: str
) -> VerificationReport:
    """Return a deeply detached report that certifies exactly the requested walk."""
    value = _domain_value(value, VerificationReport, field="verify.report")
    scope = _builtin_text(
        _domain_field(value, VerificationReport, "scope"),
        field="verify.scope",
        empty=False,
    )
    if scope != requested_scope:
        raise GrafxConfigurationError(
            "A verifier report must name the scope requested by its caller.",
            field="verify.scope",
            value=scope,
            requested=requested_scope,
        )
    findings = tuple(
        _verification_finding_view(finding)
        for finding in _tuple_items(
            _domain_field(value, VerificationReport, "findings"),
            field="verify.findings",
        )
    )
    pages_checked = _require_nonnegative_integer(
        "verify.pages_checked",
        _domain_field(value, VerificationReport, "pages_checked"),
    )
    records_checked = _require_nonnegative_integer(
        "verify.records_checked",
        _domain_field(value, VerificationReport, "records_checked"),
    )
    index_entries_checked = _require_nonnegative_integer(
        "verify.index_entries_checked",
        _domain_field(value, VerificationReport, "index_entries_checked"),
    )
    files_checked = tuple(
        _builtin_text(file, field="verify.files_checked", empty=False)
        for file in _tuple_items(
            _domain_field(value, VerificationReport, "files_checked"),
            field="verify.files_checked",
        )
    )
    if len(frozenset(files_checked)) != len(files_checked):
        raise GrafxConfigurationError(
            "A verification report cannot name the same checked file more than once.",
            field="verify.files_checked",
            value="duplicate_file",
        )
    inactive_counts = {
        SCOPE_PAGES: (records_checked, index_entries_checked),
        SCOPE_RECORDS: (pages_checked, index_entries_checked),
        SCOPE_INDEXES: (pages_checked, records_checked),
        SCOPE_ALL: (),
    }[scope]
    if any(inactive_counts):
        raise GrafxConfigurationError(
            "A verification report must leave counters outside its requested scope at zero.",
            field="verify.report",
            value="inactive_counter",
        )
    if scope not in (SCOPE_PAGES, SCOPE_ALL) and files_checked:
        raise GrafxConfigurationError(
            "Only a verification page walk may report checked files.",
            field="verify.files_checked",
            value="scope_mismatch",
        )
    if scope in (SCOPE_PAGES, SCOPE_ALL) and (
        bool(files_checked) != bool(pages_checked) or len(files_checked) > pages_checked
    ):
        raise GrafxConfigurationError(
            "A verification page count and its unique checked-file inventory must agree.",
            field="verify.files_checked",
            value="count_mismatch",
        )
    return VerificationReport(
        scope=scope,
        findings=findings,
        pages_checked=pages_checked,
        records_checked=records_checked,
        index_entries_checked=index_entries_checked,
        files_checked=files_checked,
    )


def _recycle_report_view(value: object) -> RecycleReport:
    """Rebuild a checkpoint recycling report with no mutable or executable leaves."""
    value = _domain_value(value, RecycleReport, field="checkpoint.report")

    def names(field: str) -> tuple[str, ...]:
        """Copy one segment-name collection into exact, non-empty strings."""
        return tuple(
            _builtin_text(item, field=f"checkpoint.{field}", empty=False)
            for item in _tuple_items(
                _domain_field(value, RecycleReport, field),
                field=f"checkpoint.{field}",
            )
        )

    horizon_lsn = _require_nonnegative_integer(
        "checkpoint.horizon_lsn",
        _domain_field(value, RecycleReport, "horizon_lsn"),
    )
    if horizon_lsn > MAX_U64:
        raise GrafxConfigurationError(
            "A checkpoint horizon must fit the unsigned 64-bit log sequence field.",
            field="checkpoint.horizon_lsn",
            value="out_of_range",
        )
    recycled = names("recycled")
    deferred = names("deferred")
    retained = names("retained")
    for field, collection in (
        ("recycled", recycled),
        ("deferred", deferred),
        ("retained", retained),
    ):
        if len(frozenset(collection)) != len(collection):
            raise GrafxConfigurationError(
                "A checkpoint report cannot repeat a segment within one collection.",
                field=f"checkpoint.{field}",
                value="duplicate_segment",
            )
    recycled_names = frozenset(recycled)
    if recycled_names.intersection(deferred) or recycled_names.intersection(retained):
        raise GrafxConfigurationError(
            "A recycled checkpoint segment cannot also be deferred or retained.",
            field="checkpoint.recycled",
            value="contradictory_segment_state",
        )
    reclaimed_bytes = _require_nonnegative_integer(
        "checkpoint.reclaimed_bytes",
        _domain_field(value, RecycleReport, "reclaimed_bytes"),
    )
    lag_segments = _require_nonnegative_integer(
        "checkpoint.lag_segments",
        _domain_field(value, RecycleReport, "lag_segments"),
    )
    if bool(recycled) != bool(reclaimed_bytes):
        raise GrafxConfigurationError(
            "A checkpoint recycles segments exactly when it reclaims bytes.",
            field="checkpoint.reclaimed_bytes",
            value="recycled_bytes_mismatch",
        )
    expected_lag = max(len(retained) - 1, 0)
    if lag_segments != expected_lag:
        raise GrafxConfigurationError(
            "A checkpoint lag must count retained segments older than the newest one.",
            field="checkpoint.lag_segments",
            value=lag_segments,
            expected=expected_lag,
        )
    return RecycleReport(
        horizon_lsn=horizon_lsn,
        recycled=recycled,
        deferred=deferred,
        retained=retained,
        reclaimed_bytes=reclaimed_bytes,
        lag_segments=lag_segments,
        reader_present=_builtin_bool(
            _domain_field(value, RecycleReport, "reader_present")
        ),
    )


def _vector_component(value: object) -> float:
    """Copy one query component into an exact float before engine coordination begins."""
    value_type = type(value)
    if value_type is bool:
        return float(value)
    if issubclass(value_type, float):
        return float.__float__(value)
    if issubclass(value_type, int):
        return float(int.__int__(value))
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as failure:
        raise GrafxVectorValidationError(
            "A vector needs a sequence of numbers.",
            field="values",
            reason="not_a_sequence",
            value=_builtin_type_name(value),
        ) from failure


def _vector_query_snapshot(value: object) -> tuple[float, ...] | VectorValue:
    """Detach a query into an exact tuple or an identity-preserving exact VectorValue."""
    if issubclass(type(value), VectorValue):
        source = _domain_value(value, VectorValue, field="vector.query")
        values = tuple(
            _vector_component(component)
            for component in _tuple_items(
                _domain_field(source, VectorValue, "values"),
                field="vector.query.values",
            )
        )
        space_ref = _builtin_int(
            _domain_field(source, VectorValue, "space_ref"),
            field="vector.query.space_ref",
        )
        dtype = _builtin_text(
            _domain_field(source, VectorValue, "dtype"),
            field="vector.query.dtype",
            empty=False,
        )
        if not 1 <= space_ref <= MAX_U32:
            raise GrafxVectorValidationError(
                "A query vector space reference must be a real unsigned 32-bit identifier.",
                field="space_ref",
                value=space_ref,
            )
        if dtype not in VECTOR_DTYPES:
            raise GrafxVectorValidationError(
                "A query vector must name a supported storage dtype.",
                field="dtype",
                value=dtype,
            )
        if len(values) > MAX_VECTOR_DIMENSION:
            raise GrafxVectorValidationError(
                f"A query vector may have at most {MAX_VECTOR_DIMENSION} components.",
                field="values",
                reason="dimension_too_large",
                value=len(values),
            )
        return VectorValue(values=values, space_ref=space_ref, dtype=dtype)

    if issubclass(type(value), (str, bytes, bytearray)):
        raise GrafxVectorValidationError(
            f"A vector needs a sequence of numbers; got {_builtin_type_name(value)}.",
            field="values",
            reason="not_a_sequence",
            value=_builtin_type_name(value),
        )
    try:
        if issubclass(type(value), tuple):
            iterator = tuple.__iter__(value)
        elif issubclass(type(value), list):
            iterator = list.__iter__(value)
        else:
            iterator = iter(value)  # type: ignore[arg-type]
        components: list[float] = []
        for component in iterator:
            components.append(_vector_component(component))
            if len(components) > MAX_VECTOR_DIMENSION:
                raise GrafxVectorValidationError(
                    f"A query vector may have at most {MAX_VECTOR_DIMENSION} components.",
                    field="values",
                    reason="dimension_too_large",
                    value=len(components),
                )
    except GrafxVectorValidationError:
        raise
    except (TypeError, ValueError, OverflowError) as failure:
        raise GrafxVectorValidationError(
            f"A vector needs a sequence of numbers; got {_builtin_type_name(value)}.",
            field="values",
            reason="not_a_sequence",
            value=_builtin_type_name(value),
        ) from failure
    return tuple(components)


def _record_id_filter_snapshot(value: object | None) -> RecordIdFilter | None:
    """Rebuild an exact enumerated filter with exact, usable record identifiers."""
    if value is None:
        return None
    if type(value) is not RecordIdFilter:
        raise GrafxConfigurationError(
            "A public vector candidate filter must be an immutable RecordIdFilter or None.",
            field="candidate_filter",
            value=_builtin_type_name(value),
        )
    source = _domain_field(value, RecordIdFilter, "record_ids")
    if not issubclass(type(source), frozenset):
        raise GrafxConfigurationError(
            "A public vector candidate filter must contain a frozenset of record ids.",
            field="candidate_filter.record_ids",
            value=_builtin_type_name(source),
        )
    identifiers: list[int] = []
    for raw_identifier in frozenset.__iter__(source):
        identifier = _builtin_int(raw_identifier, field="candidate_filter.record_ids")
        if not 1 <= identifier < MAX_U64:
            raise GrafxConfigurationError(
                "A vector candidate record id must be within the usable unsigned 64-bit range.",
                field="candidate_filter.record_ids",
                value="out_of_range",
            )
        identifiers.append(identifier)
    return RecordIdFilter(frozenset(identifier for identifier in identifiers))


def _vector_hit_view(value: object) -> VectorHit:
    """Rebuild one vector hit and reject a score that cannot be ordered."""
    value = _domain_value(value, VectorHit, field="vector.hit")
    record_id = _require_nonnegative_integer(
        "vector.hit.record_id", _domain_field(value, VectorHit, "record_id")
    )
    if not 1 <= record_id < MAX_U64:
        raise GrafxConfigurationError(
            "A vector hit record id must be within the usable unsigned 64-bit identity field.",
            field="vector.hit.record_id",
            value="out_of_range",
        )
    ref = _record_ref_view(
        _domain_field(value, VectorHit, "ref"), field="vector.hit.ref"
    )
    if ref == NULL_REF:
        raise GrafxConfigurationError(
            "A vector hit must name a live record location, not the null reference.",
            field="vector.hit.ref",
            value="null_ref",
        )
    return VectorHit(
        record_id=record_id,
        score=_finite_float(
            _domain_field(value, VectorHit, "score"), field="vector.hit.score"
        ),
        ref=ref,
        retired=_builtin_bool(_domain_field(value, VectorHit, "retired")),
    )


def _vector_search_result_view(
    value: object,
    *,
    requested_k: int,
    requested_space: str,
    candidate_filter: RecordIdFilter | None,
) -> VectorSearchResult:
    """Rebuild and cross-check the complete result of one public vector search."""
    value = _domain_value(value, VectorSearchResult, field="vector.result")
    result_k = _require_positive_integer(
        "vector.requested_k",
        _domain_field(value, VectorSearchResult, "requested_k"),
    )
    result_space = _builtin_text(
        _domain_field(value, VectorSearchResult, "space"),
        field="vector.space",
        empty=False,
    )
    if result_k != requested_k or result_space != requested_space:
        raise GrafxConfigurationError(
            "A vector result must describe the space and neighbour count requested by its "
            "caller.",
            field="vector.result",
            value="request_mismatch",
        )
    regime = _builtin_text(
        _domain_field(value, VectorSearchResult, "regime"),
        field="vector.regime",
        empty=False,
    )
    if regime not in REGIMES:
        raise GrafxConfigurationError(
            "A vector result must name a supported search regime.",
            field="vector.regime",
            value=regime,
        )
    hits = tuple(
        _vector_hit_view(hit)
        for hit in _tuple_items(
            _domain_field(value, VectorSearchResult, "hits"), field="vector.hits"
        )
    )
    achieved_k = _require_nonnegative_integer(
        "vector.achieved_k",
        _domain_field(value, VectorSearchResult, "achieved_k"),
    )
    if achieved_k != len(hits) or achieved_k > requested_k:
        raise GrafxConfigurationError(
            "A vector result's achieved count must equal its hits and not exceed the requested "
            "count.",
            field="vector.achieved_k",
            value=achieved_k,
        )
    record_ids = tuple(hit.record_id for hit in hits)
    if len(frozenset(record_ids)) != len(record_ids):
        raise GrafxConfigurationError(
            "A vector result cannot name the same record more than once.",
            field="vector.hits",
            value="duplicate_record_id",
        )
    references = tuple(hit.ref for hit in hits)
    if len(frozenset(references)) != len(references):
        raise GrafxConfigurationError(
            "A vector result cannot name the same record reference more than once.",
            field="vector.hits",
            value="duplicate_record_ref",
        )
    if hits != tuple(sorted(hits, key=lambda hit: (-hit.score, hit.record_id))):
        raise GrafxConfigurationError(
            "A vector result must be ranked by descending score and then ascending record id.",
            field="vector.hits",
            value="ranking_mismatch",
        )
    if len(frozenset(hit.retired for hit in hits)) > 1:
        raise GrafxConfigurationError(
            "Every hit in one vector result must report the same space retirement state.",
            field="vector.hits",
            value="mixed_retired_state",
        )
    raw_cardinality = _domain_field(value, VectorSearchResult, "filter_cardinality")
    filter_cardinality = (
        None
        if raw_cardinality is None
        else _require_nonnegative_integer("vector.filter_cardinality", raw_cardinality)
    )
    if candidate_filter is None:
        if filter_cardinality is not None:
            raise GrafxConfigurationError(
                "An unfiltered vector result must not report a filter cardinality.",
                field="vector.filter_cardinality",
                value=filter_cardinality,
            )
    else:
        candidate_ids = _domain_field(candidate_filter, RecordIdFilter, "record_ids")
        expected_cardinality = frozenset.__len__(candidate_ids)
        if filter_cardinality != expected_cardinality:
            raise GrafxConfigurationError(
                "A filtered vector result must report the exact enumerated cardinality.",
                field="vector.filter_cardinality",
                value=filter_cardinality,
                expected=expected_cardinality,
            )
        if any(record_id not in candidate_ids for record_id in record_ids):
            raise GrafxConfigurationError(
                "Every vector hit must belong to the exact candidate filter.",
                field="vector.hits",
                value="outside_candidate_filter",
            )
    return VectorSearchResult(
        hits=hits,
        regime=regime,
        achieved_k=achieved_k,
        requested_k=result_k,
        space=result_space,
        filter_cardinality=filter_cardinality,
    )


def _require_text(field: str, value: object, *, empty: bool = False) -> str:
    """Return a public snapshot's string argument or raise the stable configuration type."""
    return _builtin_text(value, field=field, empty=empty)


def _require_integer(field: str, value: object) -> int:
    """Return one integer lookup key without allowing bool to alias identity one."""
    if type(value) is bool or not issubclass(type(value), int):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"The {field} of this snapshot lookup must be an integer; got {observed}.",
            field=field,
            value=observed,
        )
    return _builtin_int(value)


def _as_origin_class(value: object) -> LedgerOriginClass | None:
    """Match LedgerStore.list origin filters, including case-insensitive public names."""
    if value is None or issubclass(type(value), LedgerOriginClass):
        return value
    if issubclass(type(value), str):
        wanted = _builtin_text(value, field="origin_class")
        for member in LedgerOriginClass:
            if member.name.casefold() == wanted.casefold():
                return member
        observed = repr(wanted)
    else:
        observed = _builtin_type_name(value)
    raise GrafxConfigurationError(
        f"{observed} is not an origin class; expected one of "
        f"{tuple(member.name.lower() for member in LedgerOriginClass)}.",
        field="origin_class",
        value=observed,
    )


def _as_reason(value: object) -> LedgerReason | None:
    """Match LedgerStore.list reason filters, including case-insensitive public names."""
    if value is None or issubclass(type(value), LedgerReason):
        return value
    if issubclass(type(value), str):
        wanted = _builtin_text(value, field="reason")
        for member in LedgerReason:
            if member.name.casefold() == wanted.casefold():
                return member
        observed = repr(wanted)
    else:
        observed = _builtin_type_name(value)
    raise GrafxConfigurationError(
        f"{observed} is not a ledger reason; expected one of "
        f"{tuple(member.name.lower() for member in LedgerReason)}.",
        field="reason",
        value=observed,
    )


def _require_ledger_count(field: str, value: object) -> int:
    """Match LedgerStore.list validation for non-negative limit and offset values."""
    if type(value) is bool or not issubclass(type(value), int):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"The {field} of a ledger listing must be an integer; got {observed}.",
            field=field,
            value=observed,
        )
    plain = _builtin_int(value)
    if plain < 0:
        raise GrafxConfigurationError(
            f"The {field} of a ledger listing cannot be negative; got {plain}.",
            field=field,
            value=plain,
        )
    return plain


def _require_ledger_identifier(value: object) -> int:
    """Match LedgerStore.inspect validation for one positive entry identifier."""
    if type(value) is bool or not issubclass(type(value), int):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"A ledger entry_id must be an integer; got {observed}.",
            field="entry_id",
            value=observed,
        )
    plain = _builtin_int(value)
    if plain <= 0:
        raise GrafxConfigurationError(
            f"A ledger entry_id is positive; got {plain}.",
            field="entry_id",
            value=plain,
        )
    return plain


def _require_quarantine_name(value: object) -> str:
    """Match QuarantineStore.inspect text and one-directory-name validation."""
    name = _require_text("name", value)
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise GrafxConfigurationError(
            f"A quarantine entry name is one directory name; got {name!r}.",
            field="name",
            value=name,
        )
    return name


def _storage_view(storage: Any) -> StorageView:
    """Snapshot a storage device without retaining it."""
    page_size = _builtin_int(storage.page_size)
    files: list[StorageFileView] = []
    for observed_name in storage.list_files():
        # The name is a port result and is also sent back through another port door.  Strip a
        # callable ``str`` subclass before that round trip: a storage implementation may quite
        # reasonably validate truth/normalise the argument in ``file_size``, which would execute
        # the subclass's host hook before the public view had a chance to copy it.
        name = _builtin_text(observed_name, field="storage.file", empty=False)
        files.append(
            StorageFileView(
                name,
                _builtin_int(storage.file_size(name)),
                page_size,
            )
        )
    return StorageView(
        _builtin_text(storage.name, field="storage.name", empty=False),
        page_size,
        tuple(files),
    )


def _clock_view(clock: Any) -> ClockView:
    """Identify a clock without advancing an injected stateful test or virtual clock."""
    return ClockView(
        _builtin_text(
            _builtin_type_name(clock), field="clock.implementation", empty=False
        )
    )


def _codec_view(codec: Any, page_size: int) -> CodecView:
    """Snapshot codec identity without exposing encode or decode capabilities."""
    return CodecView(_builtin_int(codec.format_version), _builtin_int(page_size))


def _component_view(role: str, component: object) -> ComponentView:
    """Return identity-only metadata for a collaborator."""
    return ComponentView(
        _builtin_text(role, field="component.role", empty=False),
        _builtin_text(
            _builtin_type_name(component), field="component.implementation", empty=False
        ),
    )


def _queries_view(queries: Any) -> QueryEngineView:
    """Snapshot query diagnostics without retaining the planner or executor."""
    return QueryEngineView(
        _builtin_text(
            _builtin_type_name(queries), field="queries.implementation", empty=False
        ),
        tuple(
            _builtin_text(name, field="queries.skipped_index", empty=False)
            for name in queries.skipped_indexes
        ),
    )


def _coordinator_view(coordinator: Any) -> CoordinatorView:
    """Identify a coordinator without touching liveness, lease or retirement doors.

    ``reader_horizon`` is intentionally absent: LocalProcessCoordinator prunes dead, damaged and
    temporary registrations while answering it.  ``current_epoch`` also records a liveness
    observation used by owner-stall detection.  Merely evaluating a Database property must do
    neither, so this view captures only the protocol's stable participant identity.
    """
    return CoordinatorView(
        _builtin_text(
            coordinator.owner_id(), field="coordinator.participant", empty=False
        ),
        _builtin_text(
            _builtin_type_name(coordinator),
            field="coordinator.implementation",
            empty=False,
        ),
    )


def _pool_view(pool: Any) -> BufferPoolView:
    """Snapshot buffer-pool counters and limits without pinning or evicting a page."""
    return BufferPoolView(
        _builtin_int(pool.page_size),
        _builtin_int(pool.budget_bytes),
        _builtin_int(pool.capacity_pages),
        _builtin_int(pool.used_bytes()),
        _builtin_text(pool.db_label, field="pool.db_label", empty=False),
    )


def _catalog_view(store: Any) -> CatalogStoreView:
    """Copy the catalog already materialised by its store."""
    return _catalog_view_from(store, store._catalog)


def _catalog_view_from(store: Any, catalog: Any) -> CatalogStoreView:
    """Copy one caller-validated catalog value without retaining it or its store."""
    catalog = _domain_value(catalog, Catalog, field="catalog")
    tables = tuple(
        sorted(
            (
                _table_definition(table)
                for table in _dictionary_values(
                    _domain_field(catalog, Catalog, "_tables_by_id"),
                    field="catalog.tables_by_id",
                )
            ),
            key=lambda table: table.table_id,
        )
    )
    spaces = tuple(
        sorted(
            (
                _space_definition(space)
                for space in _dictionary_values(
                    _domain_field(catalog, Catalog, "_spaces_by_id"),
                    field="catalog.spaces_by_id",
                )
            ),
            key=lambda space: space.space_id,
        )
    )
    return CatalogStoreView(
        _builtin_text(store.file, field="catalog.file", empty=False),
        _builtin_int(store.chunk_capacity),
        CatalogView(tables, spaces),
    )


def _heap_view(heap: Any) -> HeapStoreView:
    """Snapshot safe heap layout metadata."""
    return HeapStoreView(
        _builtin_text(heap.file, field="heap.file", empty=False),
        _builtin_int(heap.max_tables),
        _builtin_int(heap.inline_capacity),
    )


def _wal_view(wal: Any) -> WalView:
    """Copy the WAL fields already observed by this handle, without storage or callbacks.

    The caller holds the transaction manager's participant section, which is also held across
    local append/barrier work.  Calling ``refresh`` here used to overwrite ``_unflushed`` while
    a commit was preparing its barrier and also published size metrics from a property getter.
    A pure copy is both coherent and side-effect free; cross-process publication remains
    observable through ``database.transactions``.
    """
    damage = wal._damage
    return WalView(
        _builtin_int(wal._last_lsn),
        _builtin_text(wal._directory, field="wal.directory", empty=False),
        _builtin_text(wal._descriptor, field="wal.descriptor", empty=False),
        _builtin_int(wal._segment_bytes),
        None if damage is None else _scan_failure(damage),
        _builtin_bool(wal._append_uncertain),
        tuple(_segment_info(segment) for segment in wal._segments),
        _builtin_int(wal._total_bytes),
    )


def _transactions_view(
    transactions: Any,
    *,
    recovery_required: bool,
    state: CommitState | None,
) -> TransactionManagerView:
    """Snapshot transaction limits and caller-linearized publication state."""
    stall_threshold = transactions.reader_stall_threshold
    return TransactionManagerView(
        _builtin_int(transactions.partitions_per_table),
        _builtin_float(transactions.commit_lock_timeout),
        _builtin_float(transactions.lease_timeout),
        (None if stall_threshold is None else _builtin_float(stall_threshold)),
        _builtin_float(transactions.refresh_interval),
        _builtin_int(transactions.open_transactions),
        _builtin_bool(recovery_required),
        None if state is None else _commit_state(state),
    )


def _index_view(index: Any, definition: IndexDefinition | None = None) -> IndexView:
    """Snapshot one registered index without retaining its store or faulting a page in."""
    definition = (
        _index_definition(index.definition) if definition is None else definition
    )
    file = _builtin_text(definition.file, field="index.file", empty=False)
    built_through, reconciled_through = _resident_index_positions(index, file)
    return IndexView(
        _builtin_text(definition.name, field="index.name", empty=False),
        file,
        _string_enum(definition.visibility, IndexVisibility, field="index.visibility"),
        definition,
        _builtin_bool(index.stale),
        _builtin_optional_text(index.stale_reason, field="index.stale_reason"),
        built_through,
        reconciled_through,
        _builtin_int(index.missing_targets),
    )


def _indexes_view(indexes: Any, table_ids: frozenset[int]) -> IndexRegistryView:
    """Snapshot registrations whose tables belong to the validated committed catalog."""
    captured: list[IndexView] = []
    for index in indexes.indexes():
        observed_definition = index.definition
        definition = _domain_value(
            observed_definition, IndexDefinition, field="index.definition"
        )
        table_id = _builtin_int(_domain_field(definition, IndexDefinition, "table_id"))
        if table_id in table_ids:
            captured.append(_index_view(index, _index_definition(observed_definition)))
    return IndexRegistryView(
        tuple(captured),
        _builtin_int(indexes.published_lsn),
    )


def _ledger_view(ledger: Any) -> LedgerView:
    """Snapshot forensic ledger metadata and immutable entries."""
    damage = ledger.damage
    return LedgerView(
        _builtin_text(ledger.file, field="ledger.file", empty=False),
        None if damage is None else _damaged_tail(damage),
        tuple(_ledger_entry(entry) for entry in ledger.entries()),
        tuple(
            sorted(
                (
                    _builtin_text(key, field="ledger.origin_class", empty=False),
                    _builtin_int(value),
                )
                for key, value in ledger.depth().items()
            )
        ),
    )


def _quarantine_view(quarantine: Any) -> QuarantineView:
    """Snapshot one complete inventory and derive the legacy view from that same instant."""
    directory = _builtin_text(
        quarantine.directory, field="quarantine.directory", empty=False
    )
    if not _quarantine_logical_name(directory):
        raise GrafxConfigurationError(
            "The quarantine directory must be a canonical relative logical name.",
            field="quarantine.directory",
            value="noncanonical",
        )
    captured_inventory = tuple(
        _quarantine_inventory_item(item, directory=directory)
        for item in _tuple_items(quarantine.inventory(), field="quarantine.inventory")
    )
    captured_entries: list[QuarantineEntry] = []
    for item in captured_inventory:
        entry = item.entry
        if entry is not None:
            captured_entries.append(_quarantine_entry(entry))
    return QuarantineView(
        directory,
        tuple(captured_entries),
        captured_inventory,
    )


def _vector_index_view(
    index: Any, definition: IndexDefinition | None = None
) -> VectorIndexView:
    """Snapshot one vector index without retaining its engine or faulting a page in."""
    definition = (
        _index_definition(index.definition) if definition is None else definition
    )
    file = _builtin_text(definition.file, field="vector.index.file", empty=False)
    built_through, _reconciled_through = _resident_index_positions(index, file)
    return VectorIndexView(
        _builtin_text(definition.name, field="vector.index.name", empty=False),
        file,
        _builtin_int(index.space_id),
        _builtin_text(index.space_name, field="vector.index.space_name", empty=False),
        _builtin_int(index.dimension),
        _string_enum(
            index.metric_of_space, DistanceMetric, field="vector.index.metric"
        ),
        _builtin_text(
            index.storage_dtype, field="vector.index.storage_dtype", empty=False
        ),
        _builtin_int(index.ef_search),
        _builtin_bool(index.stale),
        _builtin_optional_text(index.stale_reason, field="vector.index.stale_reason"),
        built_through,
    )


def _resident_index_positions(index: Any, file: str) -> tuple[int | None, int | None]:
    """Return header positions only when page zero is already resident.

    ``IndexStore.header`` calls storage even on a cache hit and a cold ``BufferPool.pin`` may
    evict a dirty page and publish metrics.  Observation builders must do neither while their
    caller holds the participant section.  The pool guard makes this private, read-only peek
    coherent with concurrent pin/eviction bookkeeping; absence means "not observed", not zero.
    No frame, page, guard or store is retained by the returned values.
    """
    pool = index._pool
    with pool._guard:
        frame = pool._frames.get((file, 0))
        if frame is None:
            return None, None
        page = frame.page
        if page.slot_count <= INDEX_HEADER_SLOT:
            return None, None
        header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
        return (
            _builtin_int(header.built_through_lsn),
            _builtin_int(header.reconciled_through_lsn),
        )


def _vectors_view(
    vectors: Any, spaces: Any, table_ids: frozenset[int]
) -> VectorEngineView:
    """Snapshot vector configuration against caller-validated catalog spaces."""
    captured_spaces = tuple(_space_definition(space) for space in spaces)
    space_ids = frozenset(space.space_id for space in captured_spaces)
    captured_indexes: list[VectorIndexView] = []
    for index in vectors.indexes():
        space_id = _builtin_int(index.space_id)
        observed_definition = index.definition
        definition = _domain_value(
            observed_definition, IndexDefinition, field="index.definition"
        )
        table_id = _builtin_int(_domain_field(definition, IndexDefinition, "table_id"))
        if space_id in space_ids and table_id in table_ids:
            captured_indexes.append(
                _vector_index_view(index, _index_definition(observed_definition))
            )
    return VectorEngineView(
        _builtin_int(vectors.exact_scan_threshold),
        captured_spaces,
        tuple(captured_indexes),
    )
