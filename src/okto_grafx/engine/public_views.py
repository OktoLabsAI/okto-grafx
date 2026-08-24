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
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.ledger.entry import (
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
)
from okto_grafx.domain.ledger.payload import LedgerPayload, decode_payload
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.recovery.manifest import QuarantineManifest
from okto_grafx.domain.recovery.report import RecoveryFinding, RecoveryReport
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.txn.partitions import partition_of
from okto_grafx.domain.vector.key import VectorIndexDefinition
from okto_grafx.domain.wal.codec import FailureReason
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

    def list(self) -> tuple[QuarantineEntry, ...]:
        """Return every quarantine entry captured with this view."""
        return self.captured_entries

    def count(self) -> int:
        """Return how many quarantine entries were captured with this view."""
        return len(self.captured_entries)

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
    """Snapshot quarantine inventory without exposing payload or restore doors."""
    return QuarantineView(
        _builtin_text(quarantine.directory, field="quarantine.directory", empty=False),
        tuple(_quarantine_entry(entry) for entry in quarantine.list()),
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
