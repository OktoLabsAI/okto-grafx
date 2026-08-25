"""The public Database is an API, not a route around its own safety protocols."""

from __future__ import annotations

from collections.abc import Mapping, MutableSequence, MutableSet
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from enum import Enum
import threading
from typing import Iterator

import pytest

from okto_grafx import Database, Transaction, connect
from okto_grafx.api import assembly as assembly_module
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxLedgerError,
    GrafxQuarantineError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.ledger.entry import (
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
)
from okto_grafx.domain.ledger.payload import LedgerPayload, encode_payload
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.recovery.manifest import (
    QuarantineManifest,
    entry_suffix,
    stamp_of,
)
from okto_grafx.domain.recovery.report import RecoveryFinding, RecoveryReport
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.txn.context import CommitReport, TransactionContext
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.vector.key import VectorIndexDefinition
from okto_grafx.domain.wal.codec import FailureReason
from okto_grafx.domain.wal.replay import ScanFailure
from okto_grafx.domain.wal.segment import SegmentInfo
from okto_grafx.engine import database as database_module
from okto_grafx.engine import public_views as public_views_module
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.engine.ledger_store import DamagedTail, LedgerStore
from okto_grafx.engine.public_views import (
    PUBLIC_DATABASE_VIEW_ALLOWLIST,
    CatalogView,
    IndexRegistryView,
    IndexView,
    LedgerView,
    QuarantineInventoryItem,
    QuarantineView,
    StorageFileView,
    StorageView,
    VectorEngineView,
    VectorIndexView,
)
from okto_grafx.engine.quarantine import QuarantineEntry, QuarantineStore
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.vector_engine import VectorEngine
from okto_grafx.engine.wal_manager import WalManager
from okto_grafx.runtime.bootstrap import (
    build_default_registry,
    release_ports,
)
from okto_grafx.runtime.config import DatabaseConfig

_SCALAR_DATABASE_PROPERTIES: frozenset[str] = frozenset(
    {
        "attached_indexes",
        "close_complete",
        "closed",
        "identity",
        "label",
        "metrics_endpoint",
        "path",
        "read_only",
        "recovery_report",
        "stale_indexes",
        "unindexed_tables",
    }
)
"""Public database values that are already immutable scalars or frozen domain reports."""

_TRANSACTION_PROPERTIES: frozenset[str] = frozenset(
    {"active", "mode", "report", "snapshot", "txn_id"}
)
"""The complete observational surface of a public Transaction."""

_RAW_ENGINE_TYPES: tuple[type[object], ...] = (
    BufferPool,
    CatalogStore,
    ContainedMetricsSink,
    HeapStore,
    IndexManager,
    IndexStore,
    LedgerStore,
    LocalProcessCoordinator,
    LoggingEventSink,
    MemoryStorageDevice,
    NoOpMetricsSink,
    PageCodecV1,
    PureVectorMath,
    QuarantineStore,
    QueryEngine,
    SystemClock,
    TransactionContext,
    TransactionManager,
    VectorEngine,
    WalManager,
)
"""Concrete mutable collaborators that no public observation graph may contain."""

_BACKDOOR_NAMES: frozenset[str] = frozenset(
    {
        "callback",
        "delegate",
        "inner",
        "source",
        "_callback",
        "_delegate",
        "_inner",
        "_source",
    }
)
"""Conventional wrapper attributes that would make a view a collaborator proxy."""

_EXACT_SCALAR_LEAF_TYPES: frozenset[type[object]] = frozenset(
    {str, int, float, bool, bytes, type(None)}
)
"""Scalar leaves must be exact built-ins, never a subclass carrying host state."""

_EXACT_ENUM_LEAF_TYPES: frozenset[type[Enum]] = frozenset(
    {
        DistanceMetric,
        FailureReason,
        IndexVisibility,
        LedgerEntryType,
        LedgerOriginClass,
        LedgerReason,
        ValueType,
    }
)
"""The closed set of exact domain enums reachable from public observations."""

_EXACT_DATACLASS_TYPES: frozenset[type[object]] = frozenset(
    {
        *(kind for _name, kind in PUBLIC_DATABASE_VIEW_ALLOWLIST),
        CatalogView,
        ColumnDef,
        CommitState,
        DamagedTail,
        database_module.DatabaseIdentity,
        EmbeddingSpaceDef,
        IndexDefinition,
        IndexView,
        LedgerEntry,
        LedgerPayload,
        QuarantineEntry,
        QuarantineInventoryItem,
        QuarantineManifest,
        RecoveryFinding,
        RecoveryReport,
        ScanFailure,
        SegmentInfo,
        Snapshot,
        StorageFileView,
        TableDef,
        VectorIndexDefinition,
        VectorIndexView,
    }
)
"""Every exact frozen value class the public property graph is allowed to contain."""

_VIEW_BUILDER_HOOKS: tuple[tuple[str, str], ...] = (
    ("storage", "_storage_view"),
    ("clock", "_clock_view"),
    ("codec", "_codec_view"),
    ("metrics", "MetricsView"),
    ("events", "_component_view"),
    ("vector_math", "VectorMathView"),
    ("coordinator", "_coordinator_view"),
    ("pool", "_pool_view"),
    ("catalog", "_catalog_view_from"),
    ("heap", "_heap_view"),
    ("wal", "_wal_view"),
    ("transactions", "_transactions_view"),
    ("indexes", "_indexes_view"),
    ("ledger", "_ledger_view"),
    ("quarantine", "_quarantine_view"),
    ("vectors", "_vectors_view"),
    ("queries", "_queries_view"),
)
"""Every public composition property and the final builder call it must contain."""


def _hook_counts() -> dict[str, int]:
    """Return counters for every scalar hook a canonicalizer must bypass."""
    return {
        "__str__": 0,
        "__bool__": 0,
        "__repr__": 0,
        "__int__": 0,
        "__float__": 0,
        "__bytes__": 0,
        "__iter__": 0,
        "__class__": 0,
        "__call__": 0,
        "comparison": 0,
        "attribute": 0,
    }


class _CapabilityMixin:
    """Count and explode if public canonicalization dispatches through host code."""

    _capability_counts: dict[str, int]

    def _explode(self, hook: str) -> None:
        self._capability_counts[hook] += 1
        raise AssertionError(f"public canonicalization invoked hostile {hook}")

    def __call__(self) -> None:
        self._explode("__call__")


class _CapabilityText(_CapabilityMixin, str):
    """A valid string that is itself a callable capability with hostile scalar hooks."""

    def __new__(cls, value: str, counts: dict[str, int]) -> _CapabilityText:
        instance = str.__new__(cls, value)
        instance._capability_counts = counts
        return instance

    def __str__(self) -> str:
        self._explode("__str__")

    def __bool__(self) -> bool:
        self._explode("__bool__")

    def __repr__(self) -> str:
        self._explode("__repr__")


class _CapabilityInt(_CapabilityMixin, int):
    """A valid integer whose conversion and truth hooks are forbidden."""

    def __new__(cls, value: int, counts: dict[str, int]) -> _CapabilityInt:
        instance = int.__new__(cls, value)
        instance._capability_counts = counts
        return instance

    def __int__(self) -> int:
        self._explode("__int__")

    def __bool__(self) -> bool:
        self._explode("__bool__")

    def __repr__(self) -> str:
        self._explode("__repr__")

    def __lt__(self, _other: object) -> bool:
        self._explode("comparison")

    def __ne__(self, _other: object) -> bool:
        self._explode("comparison")


class _CapabilityFloat(_CapabilityMixin, float):
    """A valid float whose conversion and truth hooks are forbidden."""

    def __new__(cls, value: float, counts: dict[str, int]) -> _CapabilityFloat:
        instance = float.__new__(cls, value)
        instance._capability_counts = counts
        return instance

    def __float__(self) -> float:
        self._explode("__float__")

    def __bool__(self) -> bool:
        self._explode("__bool__")

    def __repr__(self) -> str:
        self._explode("__repr__")


class _CapabilityBytes(_CapabilityMixin, bytes):
    """Valid bytes whose ``__bytes__`` hook must not be trusted."""

    def __new__(cls, value: bytes, counts: dict[str, int]) -> _CapabilityBytes:
        instance = bytes.__new__(cls, value)
        instance._capability_counts = counts
        return instance

    def __bytes__(self) -> bytes:
        self._explode("__bytes__")

    def __bool__(self) -> bool:
        self._explode("__bool__")

    def __repr__(self) -> str:
        self._explode("__repr__")


class _CapabilityTuple(_CapabilityMixin, tuple):
    """A real tuple whose public iterator is an executable host capability."""

    def __new__(
        cls, value: tuple[object, ...], counts: dict[str, int]
    ) -> _CapabilityTuple:
        instance = tuple.__new__(cls, value)
        instance._capability_counts = counts
        return instance

    def __iter__(self) -> Iterator[object]:
        self._explode("__iter__")


class _AttributeCapability:
    """An invalid deep node that records any attribute read before type validation."""

    def __init__(self, counts: dict[str, int]) -> None:
        object.__setattr__(self, "_capability_counts", counts)

    def __getattribute__(self, name: str) -> object:
        if name in {"_capability_counts", "__class__"}:
            return object.__getattribute__(self, name)
        counts = object.__getattribute__(self, "_capability_counts")
        counts["attribute"] += 1
        raise AssertionError(f"deep canonicalizer read hostile attribute {name}")

    def __repr__(self) -> str:
        counts = object.__getattribute__(self, "_capability_counts")
        counts["__repr__"] += 1
        raise AssertionError("deep canonicalizer rendered a hostile object")


class _ClassSpoofCapability:
    """An invalid object whose apparent ``__class__`` is itself an executable door."""

    def __init__(self, counts: dict[str, int]) -> None:
        object.__setattr__(self, "_capability_counts", counts)

    def __getattribute__(self, name: str) -> object:
        if name == "_capability_counts":
            return object.__getattribute__(self, name)
        if name == "__class__":
            counts = object.__getattribute__(self, "_capability_counts")
            counts["__class__"] += 1
            raise AssertionError("a boundary consulted hostile __class__")
        return object.__getattribute__(self, name)


class _HostileMemoryStorage(MemoryStorageDevice):
    """Return capability-carrying names only after assembly has completed."""

    _capability_counts: dict[str, int]
    _hostile_observations: bool

    @property
    def name(self) -> str:
        if self._hostile_observations:
            return _CapabilityText("memory", self._capability_counts)
        return "memory"

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        files = super().list_files(prefix)
        if not self._hostile_observations:
            return files
        return tuple(_CapabilityText(name, self._capability_counts) for name in files)

    def file_size(self, file: str) -> int:
        size = super().file_size(file)
        if not self._hostile_observations:
            return size
        return _CapabilityInt(size, self._capability_counts)


class _HostileCatalog(Catalog):
    """A layout-compatible catalog subclass whose public readers are callback doors."""

    __slots__ = ()

    def tables(self) -> tuple[TableDef, ...]:
        raise AssertionError("a public view dispatched through Catalog.tables")

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        raise AssertionError("a public view dispatched through Catalog.spaces")


class _HostileDatabaseIdentity(database_module.DatabaseIdentity):
    """Expose identity fields as callback doors after ordinary construction completes."""

    __slots__ = ("_armed", "_capability_counts")

    def __getattribute__(self, name: str) -> object:
        if name == "page_size":
            try:
                armed = object.__getattribute__(self, "_armed")
            except AttributeError:
                armed = False
            if armed:
                counts = object.__getattribute__(self, "_capability_counts")
                counts["attribute"] += 1
                raise AssertionError(
                    "a public property dispatched through identity fields"
                )
        return object.__getattribute__(self, name)


def _replace_leaves(value: object, **replacements: object) -> object:
    """Replace frozen/slotted test DTO leaves without invoking their public validators again."""
    for name, replacement in replacements.items():
        object.__setattr__(value, name, replacement)
    return value


def _hostile_recovery_report(
    source: RecoveryReport, counts: dict[str, int]
) -> RecoveryReport:
    """Put capability-carrying leaves into the report returned by a recovery component."""
    finding = RecoveryFinding(kind="catalog_adopted", detail="catalog was adopted")
    _replace_leaves(
        finding,
        kind=_CapabilityText("catalog_adopted", counts),
        detail=_CapabilityText("catalog was adopted", counts),
        file=_CapabilityText("catalog.dat", counts),
        offset=_CapabilityInt(1, counts),
        length=_CapabilityInt(2, counts),
        lsn=_CapabilityInt(3, counts),
        page=_CapabilityInt(4, counts),
        entry_id=_CapabilityInt(5, counts),
        quarantine=_CapabilityText("quarantine/evidence", counts),
    )
    _replace_leaves(
        source,
        outcome=_CapabilityText(source.outcome, counts),
        records_replayed=_CapabilityInt(source.records_replayed, counts),
        records_discarded=_CapabilityInt(source.records_discarded, counts),
        ledger_entries_created=_CapabilityInt(source.ledger_entries_created, counts),
        last_good_lsn=_CapabilityInt(source.last_good_lsn, counts),
        findings=(finding,),
    )
    return source


def _hostile_catalog_values(
    table: TableDef,
    space: EmbeddingSpaceDef,
    counts: dict[str, int],
) -> tuple[TableDef, EmbeddingSpaceDef]:
    """Clone committed schema values and replace every scalar family with hostile subclasses."""
    columns: list[ColumnDef] = []
    for source in table.columns:
        column = ColumnDef(
            name=str.__str__(source.name),
            type=source.type,
            nullable=source.nullable,
            vector_space=(
                None
                if source.vector_space is None
                else str.__str__(source.vector_space)
            ),
        )
        _replace_leaves(
            column,
            name=_CapabilityText(source.name, counts),
            vector_space=(
                None
                if source.vector_space is None
                else _CapabilityText(source.vector_space, counts)
            ),
        )
        columns.append(column)
    captured_table = TableDef(
        table_id=int.__int__(table.table_id),
        name=str.__str__(table.name),
        kind=str.__str__(table.kind),
        columns=tuple(columns),
        primary_key=(
            None if table.primary_key is None else str.__str__(table.primary_key)
        ),
        from_table=(
            None if table.from_table is None else str.__str__(table.from_table)
        ),
        to_table=(None if table.to_table is None else str.__str__(table.to_table)),
        schema_version=int.__int__(table.schema_version),
    )
    _replace_leaves(
        captured_table,
        table_id=_CapabilityInt(table.table_id, counts),
        name=_CapabilityText(table.name, counts),
        kind=_CapabilityText(table.kind, counts),
        primary_key=(
            None
            if table.primary_key is None
            else _CapabilityText(table.primary_key, counts)
        ),
        from_table=(
            None
            if table.from_table is None
            else _CapabilityText(table.from_table, counts)
        ),
        to_table=(
            None if table.to_table is None else _CapabilityText(table.to_table, counts)
        ),
        schema_version=_CapabilityInt(table.schema_version, counts),
    )
    captured_space = EmbeddingSpaceDef(
        space_id=int.__int__(space.space_id),
        name=str.__str__(space.name),
        dimension=int.__int__(space.dimension),
        metric=space.metric,
        normalized=space.normalized,
        storage_dtype=str.__str__(space.storage_dtype),
        state=str.__str__(space.state),
        created_at_wall=float.__float__(space.created_at_wall),
    )
    _replace_leaves(
        captured_space,
        space_id=_CapabilityInt(space.space_id, counts),
        name=_CapabilityText(space.name, counts),
        dimension=_CapabilityInt(space.dimension, counts),
        storage_dtype=_CapabilityText(space.storage_dtype, counts),
        state=_CapabilityText(space.state, counts),
        created_at_wall=_CapabilityFloat(space.created_at_wall, counts),
    )
    return captured_table, captured_space


def _hostile_index_definition(
    source: IndexDefinition, counts: dict[str, int]
) -> IndexDefinition:
    """Clone an index definition, then turn every non-enum leaf into a capability subclass."""
    definition = type(source)(
        name=str.__str__(source.name),
        table_id=int.__int__(source.table_id),
        table_name=str.__str__(source.table_name),
        positions=tuple(int.__int__(position) for position in source.positions),
        visibility=source.visibility,
        bucket_count=int.__int__(source.bucket_count),
        key_derivation=str.__str__(source.key_derivation),
    )
    _replace_leaves(
        definition,
        name=_CapabilityText(source.name, counts),
        table_id=_CapabilityInt(source.table_id, counts),
        table_name=_CapabilityText(source.table_name, counts),
        positions=tuple(
            _CapabilityInt(position, counts) for position in source.positions
        ),
        bucket_count=_CapabilityInt(source.bucket_count, counts),
        key_derivation=_CapabilityText(source.key_derivation, counts),
    )
    return definition


def _hostile_ledger_values(
    counts: dict[str, int],
) -> tuple[DamagedTail, LedgerEntry]:
    """Build forensic values carrying hostile text, numeric and byte leaves."""
    damage = DamagedTail(offset=1, length=2, detail="damaged tail")
    _replace_leaves(
        damage,
        offset=_CapabilityInt(1, counts),
        length=_CapabilityInt(2, counts),
        detail=_CapabilityText("damaged tail", counts),
    )
    entry = LedgerEntry(
        entry_id=1,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=b"evidence",
        entry_type=LedgerEntryType.DISCARD,
        lsn_start=2,
        lsn_end=3,
        epoch=4,
        captured_at_wall=5.5,
    )
    _replace_leaves(
        entry,
        entry_id=_CapabilityInt(1, counts),
        payload=_CapabilityBytes(b"evidence", counts),
        lsn_start=_CapabilityInt(2, counts),
        lsn_end=_CapabilityInt(3, counts),
        epoch=_CapabilityInt(4, counts),
        captured_at_wall=_CapabilityFloat(5.5, counts),
        format_version=_CapabilityInt(entry.format_version, counts),
        reserved=_CapabilityInt(entry.reserved, counts),
    )
    return damage, entry


def _hostile_quarantine_entry(counts: dict[str, int]) -> QuarantineEntry:
    """Build one complete evidence DTO whose nested leaves are all host-owned subclasses."""
    origin = "wal/000000000001.wal"
    captured_at_wall = 3.5
    name = f"{stamp_of(captured_at_wall)}-{entry_suffix(origin, 1, 2)}"
    payload_file = f"quarantine/{name}/000000000001.wal"
    manifest_file = f"quarantine/{name}/manifest.json"
    manifest = QuarantineManifest(
        origin=origin,
        offset=1,
        length=2,
        reason="checksum_failure",
        detail="bad checksum",
        captured_at_wall=captured_at_wall,
        digest="0" * 64,
        payload_file=payload_file,
        entry_name=name,
        expected_lsn=4,
    )
    _replace_leaves(
        manifest,
        origin=_CapabilityText(manifest.origin, counts),
        offset=_CapabilityInt(manifest.offset, counts),
        length=_CapabilityInt(manifest.length, counts),
        reason=_CapabilityText(manifest.reason, counts),
        detail=_CapabilityText(manifest.detail, counts),
        captured_at_wall=_CapabilityFloat(manifest.captured_at_wall, counts),
        digest=_CapabilityText(manifest.digest, counts),
        payload_file=_CapabilityText(manifest.payload_file, counts),
        entry_name=_CapabilityText(manifest.entry_name, counts),
        expected_lsn=_CapabilityInt(manifest.expected_lsn, counts),
        schema=_CapabilityInt(manifest.schema, counts),
    )
    entry = QuarantineEntry(
        name=name,
        manifest=manifest,
        payload_file=payload_file,
        manifest_file=manifest_file,
    )
    _replace_leaves(
        entry,
        name=_CapabilityText(entry.name, counts),
        payload_file=_CapabilityText(entry.payload_file, counts),
        manifest_file=_CapabilityText(entry.manifest_file, counts),
    )
    return entry


def _hostile_quarantine_inventory_item(
    counts: dict[str, int],
) -> QuarantineInventoryItem:
    """Build one inventory DTO whose tuple and every nested leaf are host subclasses."""
    entry = _hostile_quarantine_entry(counts)
    item = QuarantineInventoryItem(
        name=entry.name,
        state="complete",
        files=(entry.manifest_file, entry.payload_file),
        manifest_file=entry.manifest_file,
        payload_file=entry.payload_file,
        manifest=entry.manifest,
        detail="",
    )
    _replace_leaves(
        item,
        name=entry.name,
        state=_CapabilityText("complete", counts),
        files=_CapabilityTuple(item.files, counts),
        manifest_file=entry.manifest_file,
        payload_file=entry.payload_file,
        detail=_CapabilityText("", counts),
    )
    return item


@contextmanager
def _hostile_class_names(
    components: tuple[object, ...], counts: dict[str, int]
) -> Iterator[None]:
    """Temporarily install callable class names and restore them through ``type`` itself."""
    restored: list[tuple[type[object], str]] = []
    try:
        for component in components:
            component_type = type(component)
            original = vars(type)["__name__"].__get__(
                component_type, type(component_type)
            )
            restored.append((component_type, str.__str__(original)))
            type.__setattr__(
                component_type,
                "__name__",
                _CapabilityText(str.__str__(original), counts),
            )
        yield
    finally:
        for component_type, original in reversed(restored):
            type.__setattr__(component_type, "__name__", original)


def _public_properties(kind: type[object]) -> frozenset[str]:
    """Return the non-private properties declared directly by ``kind``."""
    return frozenset(
        name
        for name, member in vars(kind).items()
        if not name.startswith("_") and isinstance(member, property)
    )


def _walk_values(root: object) -> tuple[object, ...]:
    """Walk the finite value graph exposed by frozen dataclasses and immutable containers."""
    pending = [root]
    seen: set[int] = set()
    walked: list[object] = []
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        walked.append(value)
        if is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in fields(value))
        elif type(value) is CommitReport:
            pending.extend((value.csn, value.durable, value.wrote))
        elif isinstance(value, (tuple, frozenset)):
            pending.extend(value)
    return tuple(walked)


def _assert_capability_free(root: object, *, surface: str) -> None:
    """Prove one finite observation graph contains only exact approved value types."""
    for value in _walk_values(root):
        value_type = type(value)
        if is_dataclass(value) and not isinstance(value, type):
            assert value_type in _EXACT_DATACLASS_TYPES, (surface, value_type.__name__)
            assert not hasattr(value, "__dict__"), (surface, value_type.__name__)
            with pytest.raises(FrozenInstanceError):
                setattr(value, fields(value)[0].name, object())
        elif value_type is CommitReport:
            assert not hasattr(value, "__dict__"), (surface, value_type.__name__)
        else:
            assert (
                value_type in _EXACT_SCALAR_LEAF_TYPES
                or value_type in _EXACT_ENUM_LEAF_TYPES
                or value_type in {tuple, frozenset}
            ), (surface, value_type.__name__)
        assert not isinstance(value, _RAW_ENGINE_TYPES), (surface, value_type.__name__)
        assert not isinstance(
            value,
            (Mapping, MutableSequence, MutableSet, bytearray, memoryview),
        ), (surface, value_type.__name__)
        assert not callable(value), (surface, value_type.__name__)
        assert _BACKDOOR_NAMES.isdisjoint(dir(value)), (surface, value_type.__name__)


def test_database_and_transaction_have_complete_static_public_property_allowlists() -> (
    None
):
    """A newly added property cannot silently publish another mutable collaborator."""
    component_names = frozenset(name for name, _kind in PUBLIC_DATABASE_VIEW_ALLOWLIST)
    assert _public_properties(Database) == _SCALAR_DATABASE_PROPERTIES | component_names
    assert frozenset(name for name, _hook in _VIEW_BUILDER_HOOKS) == component_names
    assert _public_properties(Transaction) == _TRANSACTION_PROPERTIES
    assert not hasattr(Transaction, "context")


def test_scalar_canonicalizers_bypass_every_host_override_and_return_exact_builtins() -> (
    None
):
    """Valid scalar subclasses are copied through base slots, never their executable hooks."""
    calls = _hook_counts()

    text = public_views_module._builtin_text(
        _CapabilityText("safe", calls), field="probe", empty=False
    )
    integer = public_views_module._builtin_int(_CapabilityInt(7, calls))
    real_from_float = public_views_module._builtin_float(_CapabilityFloat(1.5, calls))
    real_from_int = public_views_module._builtin_float(_CapabilityInt(2, calls))
    payload = public_views_module._builtin_bytes(_CapabilityBytes(b"safe", calls))

    assert type(text) is str and text == "safe"
    assert type(integer) is int and integer == 7
    assert type(real_from_float) is float and real_from_float == 1.5
    assert type(real_from_int) is float and real_from_int == 2.0
    assert type(payload) is bytes and payload == b"safe"
    assert calls == _hook_counts()


def test_commit_report_copies_detach_manager_wrapper_and_caller_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating any mutable report instance cannot rewrite another public observation."""
    captured: list[CommitReport] = []
    original_commit = TransactionManager.commit

    def capture_raw(
        manager: TransactionManager, context: TransactionContext
    ) -> CommitReport:
        report = original_commit(manager, context)
        captured.append(report)
        return report

    with connect(":memory:") as database:
        transaction = database.begin("read")
        with monkeypatch.context() as boundary:
            boundary.setattr(TransactionManager, "commit", capture_raw)
            returned = transaction.commit()

        raw = captured[0]
        observed = transaction.report
        assert observed is not None
        assert returned == observed == raw
        assert returned is not raw
        assert observed is not raw
        assert observed is not returned

        raw._csn = 101
        returned._csn = 202
        observed._csn = 303
        fresh = transaction.report
        assert fresh is not None
        assert fresh.csn not in {101, 202, 303}
        assert fresh is not observed


def test_invalid_scalar_inputs_refuse_without_repr_truth_or_conversion_callbacks() -> (
    None
):
    """Malformed ports fail through Grafx taxonomy before any host conversion hook runs."""
    calls = _hook_counts()
    invalid = _AttributeCapability(calls)

    for operation in (
        lambda: public_views_module._builtin_text(invalid, field="probe"),
        lambda: public_views_module._builtin_int(invalid),
        lambda: public_views_module._builtin_float(invalid),
        lambda: public_views_module._builtin_bool(invalid),
        lambda: public_views_module._builtin_bytes(invalid),
    ):
        with pytest.raises(GrafxConfigurationError):
            operation()

    # An int subclass is still not a boolean. Refusal must not ask its hostile truth predicate.
    with pytest.raises(GrafxConfigurationError):
        public_views_module._builtin_bool(_CapabilityInt(1, calls))

    assert calls == _hook_counts()


def test_type_names_bypass_hostile_metaclass_descriptors_in_views_and_path_errors() -> (
    None
):
    """Even a metaclass ``__name__`` property is not an executable publication capability."""
    calls = _hook_counts()

    class HostileMeta(type):
        @property
        def __name__(cls) -> str:  # noqa: N805 - this is a metaclass descriptor
            calls["attribute"] += 1
            raise AssertionError(
                "public type naming invoked a hostile metaclass descriptor"
            )

    class HostilePath(metaclass=HostileMeta):
        pass

    candidate = HostilePath()
    assert public_views_module._builtin_type_name(candidate) == "HostilePath"
    with pytest.raises(GrafxConfigurationError):
        connect(candidate)  # type: ignore[arg-type]
    assert calls == _hook_counts()


def test_boundary_type_checks_never_consult_a_spoofed_class_descriptor() -> None:
    """Runtime class checks use ``type(value)`` and cannot be forged by ``value.__class__``."""
    calls = _hook_counts()
    candidate = _ClassSpoofCapability(calls)

    for operation in (
        lambda: public_views_module._builtin_text(candidate, field="probe"),
        lambda: public_views_module._builtin_bool(candidate),
        lambda: public_views_module._builtin_bytes(candidate),
        lambda: public_views_module._domain_value(
            candidate, RecoveryFinding, field="finding"
        ),
        lambda: database_module._public_identity(candidate),
        lambda: database_module._public_commit_report(candidate),
        lambda: database_module._public_snapshot(candidate),
        lambda: connect(candidate),
    ):
        with pytest.raises(GrafxConfigurationError):
            operation()

    with pytest.raises(GrafxCorruptionDetected):
        database_module.DatabaseIdentity.decode(candidate)  # type: ignore[arg-type]

    assert calls == _hook_counts()


def test_domain_subclasses_are_read_through_base_slots_and_rebuilt_exactly() -> None:
    """Accepted DTO subclasses cannot run overridden attributes while being reconstructed."""
    calls = _hook_counts()

    class HostileFinding(RecoveryFinding):
        __slots__ = ("_armed", "_counts")

        def __getattribute__(self, name: str) -> object:
            try:
                armed = object.__getattribute__(self, "_armed")
            except AttributeError:
                armed = False
            if armed and name in {field.name for field in fields(RecoveryFinding)}:
                counts = object.__getattribute__(self, "_counts")
                counts["attribute"] += 1
                raise AssertionError("public finding copy dispatched through subclass")
            return object.__getattribute__(self, name)

    class HostileReport(CommitReport):
        __slots__ = ("_armed", "_counts")

        def __getattribute__(self, name: str) -> object:
            try:
                armed = object.__getattribute__(self, "_armed")
            except AttributeError:
                armed = False
            if armed and name in {"csn", "durable", "wrote"}:
                counts = object.__getattribute__(self, "_counts")
                counts["attribute"] += 1
                raise AssertionError("public commit copy dispatched through subclass")
            return object.__getattribute__(self, name)

    finding = HostileFinding(kind="catalog_adopted", detail="safe")
    object.__setattr__(finding, "_counts", calls)
    object.__setattr__(finding, "_armed", True)
    copied_finding = public_views_module._recovery_finding(finding)

    report = HostileReport(csn=7, durable=True, wrote=False)
    object.__setattr__(report, "_counts", calls)
    object.__setattr__(report, "_armed", True)
    copied_report = database_module._public_commit_report(report)

    missing = object.__new__(HostileFinding)
    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._recovery_finding(missing)

    assert type(copied_finding) is RecoveryFinding
    assert type(copied_report) is CommitReport
    assert raised.value.details["value"] == "missing"
    assert calls == _hook_counts()


def test_transaction_partition_helper_canonicalizes_numeric_and_byte_subclasses() -> (
    None
):
    """A DTO operation strips its public arguments before entering domain hash arithmetic."""
    calls = _hook_counts()
    view = public_views_module.TransactionManagerView(
        partitions_per_table=8,
        commit_lock_timeout=1.0,
        lease_timeout=1.0,
        reader_stall_threshold=None,
        refresh_interval=0.1,
        open_transactions=0,
        recovery_required=False,
        state=CommitState(0, 0, 0),
    )

    observed = view.partition_of(
        _CapabilityInt(3, calls), _CapabilityBytes(b"record", calls)
    )

    with pytest.raises(GrafxConfigurationError) as bad_table:
        view.partition_of(True, b"record")
    with pytest.raises(GrafxConfigurationError) as bad_key:
        view.partition_of(3, object())  # type: ignore[arg-type]

    assert type(observed) is int
    assert bad_table.value.details["field"] == "table_id"
    assert bad_key.value.details["field"] == "key"
    assert calls == _hook_counts()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_column_definition",
        "_table_definition",
        "_space_definition",
        "_index_definition",
        "_scan_failure",
        "_segment_info",
        "_commit_state",
        "_damaged_tail",
        "_ledger_entry",
        "_quarantine_manifest",
        "_quarantine_entry",
        "_quarantine_inventory_item",
        "_recovery_finding",
        "_recovery_report_view",
    ),
)
def test_every_deep_canonicalizer_validates_type_before_reading_attributes(
    helper_name: str,
) -> None:
    """A callable in a malformed deep DTO never becomes AttributeError or callback execution."""
    calls = _hook_counts()
    hostile = _AttributeCapability(calls)

    with pytest.raises(GrafxConfigurationError):
        getattr(public_views_module, helper_name)(hostile)

    assert calls == _hook_counts()


def test_connect_canonicalizes_a_callable_path_before_configuration_truth_checks() -> (
    None
):
    """EvilPath is stripped at the front door and cannot survive as ``database.path``."""
    calls = _hook_counts()
    path = _CapabilityText(":memory:", calls)

    with connect(path) as database:
        assert type(database.path) is str
        assert database.path == ":memory:"
        for name in _SCALAR_DATABASE_PROPERTIES:
            _assert_capability_free(getattr(database, name), surface=f"database.{name}")

        transaction = database.begin("read")
        for name in _TRANSACTION_PROPERTIES:
            _assert_capability_free(
                getattr(transaction, name), surface=f"transaction.{name}"
            )
        report = transaction.commit()
        _assert_capability_free(report, surface="transaction.commit")
        _assert_capability_free(transaction.report, surface="transaction.report")

    assert calls == _hook_counts()


def test_hostile_registry_and_deep_builder_outputs_publish_only_exact_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the complete facade with callable scalar subclasses returned by real ports."""
    calls = _hook_counts()
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    storage = registry.get("storage")
    assert type(storage) is MemoryStorageDevice
    storage.__class__ = _HostileMemoryStorage
    storage._capability_counts = calls
    storage._hostile_observations = False
    original_recovery = RecoveryManager.run

    def hostile_recovery(manager: RecoveryManager) -> RecoveryReport:
        report = original_recovery(manager)
        return _hostile_recovery_report(report, calls)

    def hostile_publisher(
        _config: object,
        _metrics: object,
        _events: object,
        _owns_ports: bool,
    ) -> tuple[str, object]:
        return _CapabilityText("http://127.0.0.1:43210/metrics", calls), lambda: None

    database: Database | None = None
    try:
        # Recovery and publisher are genuine assembly outputs. Database must strip their scalar
        # subclasses at ingress; the custom storage remains caller-owned and survives close.
        with monkeypatch.context() as assembly_boundary:
            assembly_boundary.setattr(RecoveryManager, "run", hostile_recovery)
            assembly_boundary.setattr(
                assembly_module, "_start_publisher", hostile_publisher
            )
            database = connect(
                _CapabilityText(":memory:", calls),
                registry=registry,
            )

        assert database.metrics_endpoint == "http://127.0.0.1:43210/metrics"
        assert type(database.metrics_endpoint) is str
        assert database.recovery_report is not None
        _assert_capability_free(database.recovery_report, surface="recovery_report")

        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
            schema.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")

        # A public commit report is rebuilt even when a manager-compatible collaborator returns
        # a report whose csn leaf carries state and executable hooks.
        original_commit = TransactionManager.commit

        def hostile_commit(
            manager: TransactionManager, context: TransactionContext
        ) -> CommitReport:
            report = original_commit(manager, context)
            return CommitReport(
                csn=_CapabilityInt(report.csn, calls),
                durable=report.durable,
                wrote=report.wrote,
            )

        with monkeypatch.context() as transaction_boundary:
            transaction_boundary.setattr(TransactionManager, "commit", hostile_commit)
            transaction = database.begin("read")
            report = transaction.commit()
        _assert_capability_free(report, surface="transaction.commit")
        _assert_capability_free(transaction.report, surface="transaction.report")

        # The observational properties also copy an active context rather than retaining any
        # of its domain values. Restore the manager-owned keys before rolling the context back.
        transaction = database.begin("read")
        hostile_snapshot = Snapshot(1)
        object.__setattr__(hostile_snapshot, "read_lsn", _CapabilityInt(1, calls))
        hostile_report = CommitReport(
            csn=_CapabilityInt(7, calls), durable=True, wrote=False
        )
        with monkeypatch.context() as transaction_boundary:
            transaction_boundary.setattr(
                transaction._context,
                "_txn_id",
                _CapabilityInt(transaction.txn_id, calls),
            )
            transaction_boundary.setattr(
                transaction._context, "_snapshot", hostile_snapshot
            )
            transaction_boundary.setattr(transaction, "_report", hostile_report)
            for name in _TRANSACTION_PROPERTIES:
                _assert_capability_free(
                    getattr(transaction, name), surface=f"transaction.{name}"
                )
        transaction.rollback()

        # Explicit operational reads canonicalize both their inputs and their scalar return
        # before crossing back into engine code or the caller's object graph.
        original_index_lookup = IndexManager.index
        index_name = str.__str__(database._indexes.indexes()[0].name)

        def canonical_index_lookup(candidate: IndexManager, name: str) -> IndexStore:
            if candidate is database._indexes:
                assert type(name) is str
            return original_index_lookup(candidate, name)

        original_flush = BufferPool.flush

        def hostile_flush(candidate: BufferPool) -> int:
            flushed = original_flush(candidate)
            if candidate is database._pool:
                return _CapabilityInt(flushed, calls)
            return flushed

        with monkeypatch.context() as operation_boundary:
            operation_boundary.setattr(IndexManager, "index", canonical_index_lookup)
            operation_boundary.setattr(BufferPool, "flush", hostile_flush)
            inspected = database.inspect_index(_CapabilityText(index_name, calls))
            assert type(inspected) is tuple
            flushed = database.flush()
            assert type(flushed) is int

        # The catalog store adopts committed page images lazily at the observational boundary.
        # Take that boundary once before replacing its domain leaves for the adversarial pass.
        committed_catalog = database.catalog.catalog
        source_table = committed_catalog.tables()[0]
        source_space = committed_catalog.spaces()[0]
        catalog = database._catalog._catalog
        catalog_tables = tuple(dict.values(catalog._tables_by_id))
        catalog_spaces = tuple(dict.values(catalog._spaces_by_id))
        table, space = _hostile_catalog_values(source_table, source_space, calls)
        damage, ledger_entry = _hostile_ledger_values(calls)
        quarantine_inventory_item = _hostile_quarantine_inventory_item(calls)
        state = CommitState(1, 2, 3)
        _replace_leaves(
            state,
            last_committed_lsn=_CapabilityInt(1, calls),
            last_csn=_CapabilityInt(2, calls),
            checkpoint_lsn=_CapabilityInt(3, calls),
        )
        scan_failure = ScanFailure(
            reason=FailureReason.CHECKSUM_FAILURE,
            segment="wal/000000000001.wal",
            offset=1,
            length=2,
            expected_lsn=3,
            detail="bad checksum",
            sample=b"bad",
        )
        _replace_leaves(
            scan_failure,
            segment=_CapabilityText(scan_failure.segment, calls),
            offset=_CapabilityInt(scan_failure.offset, calls),
            length=_CapabilityInt(scan_failure.length, calls),
            expected_lsn=_CapabilityInt(scan_failure.expected_lsn, calls),
            detail=_CapabilityText(scan_failure.detail, calls),
            sample=_CapabilityBytes(scan_failure.sample, calls),
        )

        original_damage = LedgerStore.damage
        original_entries = LedgerStore.entries
        original_depth = LedgerStore.depth
        original_quarantine_inventory = QuarantineStore.inventory
        original_quarantine_read = QuarantineStore.read
        original_state = TransactionManager._published_state_in_section
        original_view_epoch = CatalogStore._view_epoch
        original_owner = type(database._coordinator).owner_id

        def ledger_damage(candidate: LedgerStore) -> DamagedTail | None:
            if candidate is database._ledger:
                return damage
            return original_damage.__get__(candidate, type(candidate))

        def ledger_entries(candidate: LedgerStore) -> tuple[LedgerEntry, ...]:
            if candidate is database._ledger:
                return (ledger_entry,)
            return original_entries(candidate)

        def ledger_depth(candidate: LedgerStore) -> Mapping[str, int]:
            if candidate is database._ledger:
                return {
                    _CapabilityText("forensic", calls): _CapabilityInt(1, calls),
                    _CapabilityText("reapplicable", calls): _CapabilityInt(0, calls),
                }
            return original_depth(candidate)

        def quarantine_inventory(
            candidate: QuarantineStore,
        ) -> tuple[QuarantineInventoryItem, ...]:
            if candidate is database._quarantine:
                return (quarantine_inventory_item,)
            return original_quarantine_inventory(candidate)

        def quarantine_payload(candidate: QuarantineStore, name: str) -> bytes:
            if candidate is database._quarantine:
                assert type(name) is str
                assert name == "evidence"
                return _CapabilityBytes(b"verified evidence", calls)
            return original_quarantine_read(candidate, name)

        def published_state(candidate: TransactionManager) -> CommitState:
            if candidate is database._transactions:
                return state
            return original_state(candidate)

        def hostile_view_epoch(candidate: CatalogStore) -> int:
            epoch = original_view_epoch(candidate)
            if candidate is database._catalog:
                return _CapabilityInt(epoch, calls)
            return epoch

        def hostile_owner(candidate: object) -> str:
            if candidate is database._coordinator:
                return _CapabilityText("participant-hostile", calls)
            return original_owner(candidate)

        with monkeypatch.context() as boundary:
            storage._hostile_observations = True
            boundary.setattr(
                storage, "_page_size", _CapabilityInt(storage.page_size, calls)
            )
            boundary.setattr(
                PageCodecV1,
                "format_version",
                property(lambda _self: _CapabilityInt(1, calls)),
            )
            boundary.setattr(type(database._coordinator), "owner_id", hostile_owner)
            boundary.setattr(
                database._pool,
                "_page_size",
                _CapabilityInt(database._pool.page_size, calls),
            )
            boundary.setattr(
                database._pool,
                "_budget_bytes",
                _CapabilityInt(database._pool.budget_bytes, calls),
            )
            boundary.setattr(
                database._pool,
                "_db_label",
                _CapabilityText(database._pool.db_label, calls),
            )
            boundary.setattr(
                database._catalog,
                "_file",
                _CapabilityText(database._catalog.file, calls),
            )
            boundary.setattr(
                database._catalog,
                "_loaded_epoch",
                _CapabilityInt(database._catalog._loaded_epoch, calls),
            )
            boundary.setattr(CatalogStore, "_view_epoch", hostile_view_epoch)
            boundary.setattr(
                database._heap,
                "_file",
                _CapabilityText(database._heap.file, calls),
            )

            # Read trusted Catalog slots directly: a subclass cannot replace its readers with
            # callbacks, and hostile numeric keys are never sorted. Values are rebuilt first and
            # ordered only by their canonical identities.
            boundary.setattr(
                catalog,
                "_tables",
                {
                    item.name: table if item.table_id == source_table.table_id else item
                    for item in catalog_tables
                },
            )
            boundary.setattr(
                catalog,
                "_tables_by_id",
                {
                    _CapabilityInt(item.table_id, calls): (
                        table if item.table_id == source_table.table_id else item
                    )
                    for item in catalog_tables
                },
            )
            boundary.setattr(
                catalog,
                "_spaces",
                {
                    item.name: space if item.space_id == source_space.space_id else item
                    for item in catalog_spaces
                },
            )
            boundary.setattr(
                catalog,
                "_spaces_by_id",
                {
                    _CapabilityInt(item.space_id, calls): (
                        space if item.space_id == source_space.space_id else item
                    )
                    for item in catalog_spaces
                },
            )
            boundary.setattr(catalog, "__class__", _HostileCatalog)

            for index in database._indexes.indexes():
                boundary.setattr(
                    index,
                    "_definition",
                    _hostile_index_definition(index.definition, calls),
                )
                boundary.setattr(
                    index,
                    "_stale_reason",
                    _CapabilityText("audit-only", calls),
                )
                boundary.setattr(
                    index,
                    "_missing_targets",
                    _CapabilityInt(index.missing_targets, calls),
                )
                if hasattr(index, "_space_id"):
                    boundary.setattr(
                        index, "_space_id", _CapabilityInt(index.space_id, calls)
                    )
                    boundary.setattr(
                        index,
                        "_space_name",
                        _CapabilityText(index.space_name, calls),
                    )
                    boundary.setattr(
                        index, "_dimension", _CapabilityInt(index.dimension, calls)
                    )
                    boundary.setattr(
                        index,
                        "_storage_dtype",
                        _CapabilityText(index.storage_dtype, calls),
                    )
                    boundary.setattr(
                        index, "_ef_search", _CapabilityInt(index.ef_search, calls)
                    )

            port_wal_names = tuple(
                name
                for name in storage.list_files("wal/")
                if str.startswith(name, "wal/")
            )
            assert port_wal_names and type(port_wal_names[0]) is _CapabilityText
            source_segment = database._wal._segments[0]
            segment = SegmentInfo(
                number=source_segment.number,
                name=str.__str__(port_wal_names[0]),
                first_lsn=source_segment.first_lsn,
                last_lsn=source_segment.last_lsn,
                size_bytes=source_segment.size_bytes,
                record_count=source_segment.record_count,
            )
            _replace_leaves(
                segment,
                number=_CapabilityInt(segment.number, calls),
                name=port_wal_names[0],
                first_lsn=_CapabilityInt(segment.first_lsn, calls),
                last_lsn=_CapabilityInt(segment.last_lsn, calls),
                size_bytes=_CapabilityInt(segment.size_bytes, calls),
                record_count=_CapabilityInt(segment.record_count, calls),
            )
            boundary.setattr(database._wal, "_segments", [segment])
            boundary.setattr(database._wal, "_damage", scan_failure)
            boundary.setattr(
                database._wal,
                "_last_lsn",
                _CapabilityInt(database._wal._last_lsn, calls),
            )
            boundary.setattr(
                database._wal,
                "_directory",
                _CapabilityText(database._wal._directory, calls),
            )
            boundary.setattr(
                database._wal,
                "_descriptor",
                _CapabilityText(database._wal._descriptor, calls),
            )
            boundary.setattr(
                database._wal,
                "_segment_bytes",
                _CapabilityInt(database._wal._segment_bytes, calls),
            )
            boundary.setattr(
                database._wal,
                "_total_bytes",
                _CapabilityInt(database._wal._total_bytes, calls),
            )

            boundary.setattr(
                database._transactions,
                "_partitions_per_table",
                _CapabilityInt(database._transactions.partitions_per_table, calls),
            )
            boundary.setattr(
                TransactionManager,
                "_published_state_in_section",
                published_state,
            )
            boundary.setattr(LedgerStore, "damage", property(ledger_damage))
            boundary.setattr(LedgerStore, "entries", ledger_entries)
            boundary.setattr(LedgerStore, "depth", ledger_depth)
            boundary.setattr(QuarantineStore, "inventory", quarantine_inventory)
            boundary.setattr(QuarantineStore, "read", quarantine_payload)
            boundary.setattr(
                database._ledger,
                "_file",
                _CapabilityText(database._ledger.file, calls),
            )
            boundary.setattr(
                database._quarantine,
                "_directory",
                _CapabilityText(database._quarantine.directory, calls),
            )
            boundary.setattr(
                database._vectors,
                "_threshold",
                _CapabilityInt(database._vectors.exact_scan_threshold, calls),
            )
            database._queries._skipped_indexes.add(
                _CapabilityText("skipped_hostile", calls)
            )

            # Class names are writable string attributes in Python, and therefore another port
            # can supply a callable ``str`` subclass without any descriptor being involved.
            with _hostile_class_names(
                (
                    database._clock,
                    database._events,
                    database._vector_math,
                    database._coordinator,
                    database._queries,
                ),
                calls,
            ):
                identity = _HostileDatabaseIdentity(
                    database_uuid=b"0" * 16,
                    page_size=512,
                    partitions_per_table=8,
                    created_at_wall=1.5,
                    granularity_descriptor="hostile-identity",
                )
                _replace_leaves(
                    identity,
                    database_uuid=_CapabilityBytes(b"0" * 16, calls),
                    page_size=_CapabilityInt(512, calls),
                    partitions_per_table=_CapabilityInt(8, calls),
                    created_at_wall=_CapabilityFloat(1.5, calls),
                    granularity_descriptor=_CapabilityText("hostile-identity", calls),
                    format_version=_CapabilityInt(1, calls),
                )
                object.__setattr__(identity, "_capability_counts", calls)
                object.__setattr__(identity, "_armed", True)
                boundary.setattr(database, "_identity", identity)

                observations: list[tuple[str, object]] = [
                    (name, getattr(database, name))
                    for name in _SCALAR_DATABASE_PROPERTIES
                ]
                observations.extend(
                    (name, getattr(database, name))
                    for name, _expected_type in PUBLIC_DATABASE_VIEW_ALLOWLIST
                )
                for surface, observation in observations:
                    _assert_capability_free(observation, surface=f"database.{surface}")

                assert database.wal.segment_inventory
                assert type(database.wal.segment_inventory[0].name) is str
                assert database.quarantine.captured_entries
                assert type(database.quarantine.captured_entries[0].name) is str
                assert database.quarantine.captured_inventory
                assert type(database.quarantine.captured_inventory[0].name) is str
                payload = database.read_quarantine(_CapabilityText("evidence", calls))
                assert type(payload) is bytes
                assert payload == b"verified evidence"

        storage._hostile_observations = False
        database._queries._skipped_indexes.discard("skipped_hostile")
        assert calls == _hook_counts()
    finally:
        storage._hostile_observations = False
        if database is not None:
            database.close()
        release_ports(registry)


@pytest.mark.parametrize(("property_name", "builder_name"), _VIEW_BUILDER_HOOKS)
def test_every_view_builder_defers_reentrant_close_until_its_snapshot_is_complete(
    property_name: str,
    builder_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A builder callback seals immediately but cannot release collaborators under itself."""
    expected_types = dict(PUBLIC_DATABASE_VIEW_ALLOWLIST)
    database = connect(":memory:")
    original = getattr(database_module, builder_name)
    calls: list[str] = []

    def close_then_build(*args: object, **kwargs: object) -> object:
        database.close()
        assert database.closed
        assert not database.close_complete, property_name
        answer = original(*args, **kwargs)
        calls.append(property_name)
        return answer

    with monkeypatch.context() as boundary:
        boundary.setattr(database_module, builder_name, close_then_build)
        view = getattr(database, property_name)

    assert type(view) is expected_types[property_name]
    assert calls == [property_name]
    assert database.close_complete


@pytest.mark.parametrize(("property_name", "builder_name"), _VIEW_BUILDER_HOOKS)
def test_close_winning_before_view_transition_refuses_before_any_builder(
    property_name: str,
    builder_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inner open check seals the precheck-to-transition race for every public view."""
    database = connect(":memory:")
    before_transition = threading.Event()
    release_view = threading.Event()
    builder_calls: list[str] = []
    failures: list[BaseException] = []
    original_transition = Database._public_transition

    def pause_before_transition(self: Database):  # noqa: ANN202
        inner = original_transition(self)

        @contextmanager
        def paused() -> Iterator[None]:
            if self is database and threading.current_thread().name == "late-view":
                before_transition.set()
                assert release_view.wait(5.0), "close did not release the late view"
            with inner:
                yield

        return paused()

    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        builder_calls.append(builder_name)
        raise AssertionError("a closed view reached its collaborator builder")

    def observe() -> None:
        try:
            getattr(database, property_name)
        except BaseException as failure:  # noqa: BLE001 - exact lifecycle evidence
            failures.append(failure)

    with monkeypatch.context() as boundary:
        boundary.setattr(Database, "_public_transition", pause_before_transition)
        boundary.setattr(database_module, builder_name, forbidden_builder)
        worker = threading.Thread(target=observe, name="late-view")
        try:
            worker.start()
            assert before_transition.wait(5.0), (
                "view never reached its transition boundary"
            )
            database.close()
            assert database.close_complete
        finally:
            release_view.set()
            worker.join(5.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], GrafxUnsupportedOperation)
    assert builder_calls == []


def test_storage_builder_close_callback_cannot_release_between_inventory_and_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reproduced list_files-to-file_size reentry returns one complete immutable view."""
    database = connect(":memory:")
    storage = database._storage
    storage_type = type(storage)
    original_list = storage_type.list_files
    reentered: list[tuple[str, ...]] = []

    def close_after_inventory(candidate: object, prefix: str = "") -> tuple[str, ...]:
        files = original_list(candidate, prefix)
        if candidate is storage and not reentered:
            reentered.append(files)
            database.close()
            assert database.closed and not database.close_complete
        return files

    with monkeypatch.context() as boundary:
        boundary.setattr(storage_type, "list_files", close_after_inventory)
        view = database.storage

    assert reentered
    assert view.list_files() == reentered[0]
    assert all(view.file_size(name) >= 0 for name in view.list_files())
    assert database.close_complete


def test_ledger_view_preserves_store_filtering_pagination_and_case_insensitivity() -> (
    None
):
    """The immutable inventory keeps every documented LedgerStore.list read spelling."""
    forensic = LedgerEntry(
        entry_id=1,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
    )
    reapplicable = LedgerEntry(
        entry_id=2,
        origin_class=LedgerOriginClass.REAPPLICABLE,
        reason=LedgerReason.STALE_EPOCH,
    )
    later_forensic = LedgerEntry(
        entry_id=3,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.TRUNCATED_TAIL,
    )
    view = LedgerView(
        "ledger/unapplied.log",
        None,
        (forensic, reapplicable, later_forensic),
        (("forensic", 2), ("reapplicable", 1)),
    )

    assert view.list(origin_class="forensic") == (forensic, later_forensic)
    assert view.list(origin_class="FoReNsIc") == (forensic, later_forensic)
    assert view.list(origin_class=LedgerOriginClass.REAPPLICABLE) == (reapplicable,)
    assert view.list(reason="checksum_failure") == (forensic,)
    assert view.list(reason="CHECKSUM_FAILURE") == (forensic,)
    assert view.list(reason=LedgerReason.STALE_EPOCH) == (reapplicable,)
    assert view.list(limit=1, offset=1) == (reapplicable,)
    assert view.list(limit=0) == ()
    assert view.inspect(2) is reapplicable


def test_ledger_view_decodes_and_exports_only_its_immutable_captured_entry() -> None:
    """Forensic compatibility reads need no callback or retained ledger capability."""
    payload = LedgerPayload(
        origin="wal/000000000001.wal",
        offset=512,
        length=8,
        expected_lsn=7,
        record_type=2,
        failure="checksum_failure",
        detail="bad checksum",
        quarantine="evidence",
        body=b"evidence",
    )
    entry = LedgerEntry(
        entry_id=1,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=encode_payload(payload),
    )
    view = LedgerView("ledger/unapplied.log", None, (entry,), (("forensic", 1),))

    observed = view.provenance(1)

    assert observed == payload
    _assert_capability_free(observed, surface="ledger.provenance")
    assert view.export(1) == b"evidence"


def test_ledger_view_export_preserves_store_refusal_taxonomy() -> None:
    """Operations and quarantine-backed large ranges are never mislabeled as exported bytes."""
    operation = LedgerEntry(
        entry_id=1,
        origin_class=LedgerOriginClass.REAPPLICABLE,
        reason=LedgerReason.STALE_EPOCH,
        payload=encode_payload(LedgerPayload(origin="wal/operation.wal")),
    )
    redirected = LedgerEntry(
        entry_id=2,
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.TRUNCATED_TAIL,
        payload=encode_payload(
            LedgerPayload(
                origin="wal/large.wal",
                length=4096,
                quarantine="large-evidence",
            )
        ),
    )
    view = LedgerView("ledger/unapplied.log", None, (operation, redirected), ())

    with pytest.raises(GrafxLedgerError) as operation_failure:
        view.export(1)
    with pytest.raises(GrafxLedgerError) as redirected_failure:
        view.export(2)

    assert operation_failure.value.details == {"field": "origin_class", "entry_id": 1}
    assert redirected_failure.value.details == {
        "field": "quarantine",
        "entry_id": 2,
        "quarantine": "large-evidence",
    }


@pytest.mark.parametrize(
    ("case", "field"),
    (
        ("origin", "origin_class"),
        ("reason", "reason"),
        ("limit_type", "limit"),
        ("limit_negative", "limit"),
        ("offset_type", "offset"),
        ("offset_negative", "offset"),
        ("inspect_type", "entry_id"),
        ("inspect_nonpositive", "entry_id"),
    ),
)
def test_ledger_view_invalid_arguments_use_store_configuration_taxonomy(
    case: str,
    field: str,
) -> None:
    """Bad public filters/counts/identifiers never become empty pages or raw slice errors."""
    view = LedgerView("ledger/unapplied.log", None, (), ())

    with pytest.raises(GrafxConfigurationError) as raised:
        if case == "origin":
            view.list(origin_class="unclassifiable")
        elif case == "reason":
            view.list(reason="something_else")
        elif case == "limit_type":
            view.list(limit=True)
        elif case == "limit_negative":
            view.list(limit=-1)
        elif case == "offset_type":
            view.list(offset=1.5)  # type: ignore[arg-type]
        elif case == "offset_negative":
            view.list(offset=-1)
        elif case == "inspect_type":
            view.inspect([])  # type: ignore[arg-type]
        else:
            view.inspect(0)

    assert raised.value.details["field"] == field


def test_ledger_view_missing_identifier_keeps_ledger_error_taxonomy() -> None:
    """A valid absent identifier is a ledger lookup failure, not a configuration refusal."""
    view = LedgerView("ledger/unapplied.log", None, (), ())

    with pytest.raises(GrafxLedgerError) as raised:
        view.inspect(7)

    assert raised.value.details == {"field": "entry_id", "entry_id": 7}


@pytest.mark.parametrize(
    ("operation", "error_type", "field"),
    (
        (
            lambda: StorageView("memory", 512, ()).list_files(1),
            GrafxConfigurationError,
            "prefix",
        ),
        (
            lambda: StorageView("memory", 512, ()).exists([]),
            GrafxConfigurationError,
            "file",
        ),
        (
            lambda: StorageView("memory", 512, ()).file_size(None),
            GrafxConfigurationError,
            "file",
        ),
        (
            lambda: StorageView("memory", 512, ()).page_count({}),
            GrafxConfigurationError,
            "file",
        ),
        (lambda: CatalogView((), ()).has_table([]), GrafxConfigurationError, "table"),
        (lambda: CatalogView((), ()).has_space({}), GrafxConfigurationError, "space"),
        (lambda: CatalogView((), ()).table(None), GrafxConfigurationError, "table"),
        (
            lambda: CatalogView((), ()).table_by_id(True),
            GrafxConfigurationError,
            "table_id",
        ),
        (lambda: CatalogView((), ()).space([]), GrafxConfigurationError, "space"),
        (
            lambda: CatalogView((), ()).space_by_id("1"),
            GrafxConfigurationError,
            "space_id",
        ),
        (
            lambda: QuarantineView("quarantine", ()).inspect([]),
            GrafxConfigurationError,
            "name",
        ),
        (
            lambda: QuarantineView("quarantine", ()).inspect("../x"),
            GrafxConfigurationError,
            "name",
        ),
        (
            lambda: VectorEngineView(0, (), ()).space({}),
            GrafxConfigurationError,
            "space",
        ),
        (lambda: VectorEngineView(0, (), ()).index([]), GrafxIndexError, "space"),
        (lambda: IndexRegistryView((), 0).index([]), GrafxIndexError, "name"),
    ),
)
def test_snapshot_lookup_invalid_inputs_never_escape_raw_python_errors(
    operation: object,
    error_type: type[BaseException],
    field: str,
) -> None:
    """Every DTO lookup refuses hostile/unhashable inputs through a Grafx taxonomy."""
    with pytest.raises(error_type) as raised:
        operation()  # type: ignore[operator]

    assert getattr(raised.value, "details")["field"] == field


def test_snapshot_valid_missing_lookups_keep_component_taxonomy() -> None:
    """Validation does not collapse valid absence into the wrong component error."""
    with pytest.raises(GrafxQuarantineError):
        QuarantineView("quarantine", ()).inspect("missing")
    with pytest.raises(GrafxIndexError):
        VectorEngineView(0, (), ()).index("missing")
    with pytest.raises(GrafxIndexError):
        IndexRegistryView((), 0).index("missing")


def test_quarantine_view_count_is_an_exact_observational_integer() -> None:
    """The compatibility count never traverses or retains a store capability."""
    calls: dict[str, int] = {}
    view = QuarantineView(
        "quarantine",
        (_hostile_quarantine_entry(calls),),
    )

    observed = view.count()

    assert type(observed) is int
    assert observed == 1
    assert calls == {}


def test_legacy_quarantine_view_refuses_to_invent_an_empty_inventory() -> None:
    """A legacy two-field view means "not captured", never "proved empty"."""
    view = QuarantineView("quarantine", ())

    with pytest.raises(GrafxQuarantineError) as raised:
        view.inventory()

    assert view.captured_inventory is None
    assert raised.value.details == {
        "field": "inventory",
        "cause": "not_captured",
        "conclusive": False,
        "inconclusive": True,
    }


def _complete_quarantine_inventory_item(
    root: str = "quarantine", *, extra_files: tuple[str, ...] = ()
) -> QuarantineInventoryItem:
    """Build one physically coherent complete item below an arbitrary candidate root."""
    origin = "wal/000000000001.wal"
    captured_at_wall = 3.5
    name = f"{stamp_of(captured_at_wall)}-{entry_suffix(origin, 1, 2)}"
    item_directory = f"{root}/{name}"
    manifest_file = f"{item_directory}/manifest.json"
    payload_file = f"{item_directory}/000000000001.wal"
    manifest = QuarantineManifest(
        origin=origin,
        offset=1,
        length=2,
        reason="checksum_failure",
        detail="bad checksum",
        captured_at_wall=captured_at_wall,
        digest="0" * 64,
        payload_file=payload_file,
        entry_name=name,
    )
    return QuarantineInventoryItem(
        name=name,
        state="complete",
        files=(manifest_file, payload_file, *extra_files),
        manifest_file=manifest_file,
        payload_file=payload_file,
        manifest=manifest,
    )


@pytest.mark.parametrize(
    "contradiction",
    (
        "missing_manifest",
        "empty_files",
        "missing_payload_file",
        "manifest_entry_name",
        "manifest_payload_file",
        "crossed_manifest_directory",
        "crossed_payload_directory",
        "dot_item_name",
        "parent_item_name",
        "backslash_item_name",
        "parent_payload_leaf",
        "backslash_payload_leaf",
        "manifest_case_alias",
        "restore_receipt_case_alias",
        "unexpected_extra_file",
        "foreign_extra_file",
        "payload_not_derived_from_origin",
        "duplicate_file",
        "casefold_file_collision",
    ),
)
@pytest.mark.parametrize(
    "directory", ("quarantine", None), ids=("known-root", "derived-root")
)
def test_quarantine_view_refuses_an_incoherent_complete_inventory_item(
    contradiction: str, directory: str | None
) -> None:
    """A store cannot label contradictory physical evidence as legacy-complete."""
    origin = "wal/000000000001.wal"
    captured_at_wall = 3.5
    name = f"{stamp_of(captured_at_wall)}-{entry_suffix(origin, 1, 2)}"
    item_directory = f"quarantine/{name}"
    manifest_file = f"{item_directory}/manifest.json"
    payload_file = f"{item_directory}/000000000001.wal"
    manifest = QuarantineManifest(
        origin=origin,
        offset=1,
        length=2,
        reason="checksum_failure",
        detail="bad checksum",
        captured_at_wall=captured_at_wall,
        digest="0" * 64,
        payload_file=payload_file,
        entry_name=name,
    )
    complete = QuarantineInventoryItem(
        name=name,
        state="complete",
        files=(manifest_file, payload_file),
        manifest_file=manifest_file,
        payload_file=payload_file,
        manifest=manifest,
    )
    if contradiction == "missing_manifest":
        incoherent = replace(complete, manifest=None)
    elif contradiction == "empty_files":
        incoherent = replace(complete, files=())
    elif contradiction == "missing_payload_file":
        incoherent = replace(complete, files=(complete.manifest_file,))
    elif contradiction == "manifest_entry_name":
        incoherent = replace(
            complete, manifest=replace(manifest, entry_name="another-entry")
        )
    elif contradiction == "manifest_payload_file":
        incoherent = replace(
            complete,
            manifest=replace(
                manifest, payload_file=f"{item_directory}/another-payload.wal"
            ),
        )
    elif contradiction == "crossed_manifest_directory":
        crossed = "quarantine/another-entry/manifest.json"
        incoherent = replace(
            complete,
            manifest_file=crossed,
            files=(crossed, complete.payload_file),
        )
    elif contradiction == "crossed_payload_directory":
        crossed = "quarantine/another-entry/payload.wal"
        incoherent = replace(
            complete,
            payload_file=crossed,
            manifest=replace(manifest, payload_file=crossed),
            files=(complete.manifest_file, crossed),
        )
    elif contradiction in ("dot_item_name", "parent_item_name", "backslash_item_name"):
        crossed_name = {
            "dot_item_name": ".",
            "parent_item_name": "..",
            "backslash_item_name": "nested\\entry",
        }[contradiction]
        manifest_file = f"quarantine/{crossed_name}/manifest.json"
        payload_file = f"quarantine/{crossed_name}/payload.wal"
        incoherent = replace(
            complete,
            name=crossed_name,
            files=(manifest_file, payload_file),
            manifest_file=manifest_file,
            payload_file=payload_file,
            manifest=replace(
                manifest, entry_name=crossed_name, payload_file=payload_file
            ),
        )
    else:
        if contradiction in (
            "parent_payload_leaf",
            "backslash_payload_leaf",
            "manifest_case_alias",
            "restore_receipt_case_alias",
            "payload_not_derived_from_origin",
        ):
            payload_leaf = {
                "parent_payload_leaf": "..",
                "backslash_payload_leaf": "nested\\payload.wal",
                "manifest_case_alias": "MANIFEST.JSON",
                "restore_receipt_case_alias": "RESTORE-1.JSON",
                "payload_not_derived_from_origin": "another-payload.wal",
            }[contradiction]
            contradictory_payload = f"{item_directory}/{payload_leaf}"
            incoherent = replace(
                complete,
                files=(complete.manifest_file, contradictory_payload),
                payload_file=contradictory_payload,
                manifest=replace(manifest, payload_file=contradictory_payload),
            )
        elif contradiction == "unexpected_extra_file":
            incoherent = replace(
                complete, files=(*complete.files, f"{item_directory}/unexpected.bin")
            )
        elif contradiction == "foreign_extra_file":
            incoherent = replace(
                complete, files=(*complete.files, "quarantine/other/foreign.bin")
            )
        elif contradiction == "duplicate_file":
            incoherent = replace(complete, files=(*complete.files, manifest_file))
        else:
            incoherent = replace(
                complete, files=(*complete.files, f"{item_directory}/MANIFEST.JSON")
            )

    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._quarantine_inventory_item(incoherent, directory=directory)

    assert raised.value.details == {
        "field": "quarantine.inventory.item",
        "value": "incomplete_complete_item",
    }


@pytest.mark.parametrize(
    "mode", ("explicit", "derived"), ids=("known-root", "derived-root")
)
@pytest.mark.parametrize("root", ("quarantine", "forensics/quarantine"))
def test_quarantine_view_accepts_only_canonical_restore_receipts_as_extra_files(
    mode: str, root: str
) -> None:
    """A numbered receipt is part of a complete layout; arbitrary neighbours are not."""
    plain = _complete_quarantine_inventory_item(root)
    receipt = f"{root}/{plain.name}/restore-1.json"
    complete = replace(
        plain,
        files=(*plain.files, receipt),
    )
    captured_directory = root if mode == "explicit" else None

    observed = public_views_module._quarantine_inventory_item(
        complete, directory=captured_directory
    )

    assert observed == complete
    assert observed.entry is not None


@pytest.mark.parametrize(
    "root",
    (
        "",
        ".",
        "..",
        "nested/./quarantine",
        "nested/../quarantine",
        "quarantine//nested",
        "nested\\quarantine",
        "nul\x00root",
        "/absolute",
        "C:/quarantine",
        "quarantine/",
        " padded",
        "quarantine.",
        "CON",
        "quarantine/NUL",
    ),
)
@pytest.mark.parametrize(
    "captured_directory", ("explicit", "derived"), ids=("known-root", "derived-root")
)
def test_quarantine_view_refuses_noncanonical_inventory_roots(
    root: str, captured_directory: str
) -> None:
    """Neither a supplied nor path-derived root may escape the storage namespace grammar."""
    item = _complete_quarantine_inventory_item(root)
    directory = root if captured_directory == "explicit" else None

    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._quarantine_inventory_item(item, directory=directory)

    assert raised.value.details == {
        "field": "quarantine.inventory.item",
        "value": "incomplete_complete_item",
    }


@pytest.mark.parametrize(
    "captured_directory", ("quarantine", None), ids=("known-root", "derived-root")
)
def test_quarantine_view_translates_an_oversized_restore_receipt(
    captured_directory: str | None,
) -> None:
    """A hostile decimal envelope remains an incoherent item, never a raw int failure."""
    plain = _complete_quarantine_inventory_item()
    receipt = f"quarantine/{plain.name}/restore-{'9' * 4_301}.json"
    item = replace(plain, files=(*plain.files, receipt))

    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._quarantine_inventory_item(
            item, directory=captured_directory
        )

    assert raised.value.details == {
        "field": "quarantine.inventory.item",
        "value": "incomplete_complete_item",
    }


@pytest.mark.parametrize("failure_type", (ValueError, OverflowError))
def test_quarantine_view_translates_restore_receipt_classifier_failures(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[Exception]
) -> None:
    """Numeric classifier failures are closed into the public incoherence taxonomy."""
    plain = _complete_quarantine_inventory_item()
    receipt = f"quarantine/{plain.name}/restore-1.json"
    item = replace(plain, files=(*plain.files, receipt))

    def fail(_file: str, _directory: str) -> bool:
        raise failure_type("hostile numeric envelope")

    monkeypatch.setattr(public_views_module, "_is_restore_receipt", fail)

    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._quarantine_inventory_item(item, directory="quarantine")

    assert raised.value.details == {
        "field": "quarantine.inventory.item",
        "value": "incomplete_complete_item",
    }


@pytest.mark.parametrize("failure_type", (KeyboardInterrupt, SystemExit))
def test_quarantine_view_preserves_base_exceptions_from_receipt_classifier(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    """Process-control failures retain their identity at the receipt classifier boundary."""
    plain = _complete_quarantine_inventory_item()
    receipt = f"quarantine/{plain.name}/restore-1.json"
    item = replace(plain, files=(*plain.files, receipt))
    primary = failure_type()

    def fail(_file: str, _directory: str) -> bool:
        raise primary

    monkeypatch.setattr(public_views_module, "_is_restore_receipt", fail)

    with pytest.raises(failure_type) as raised:
        public_views_module._quarantine_inventory_item(item, directory="quarantine")

    assert raised.value is primary


@pytest.mark.parametrize(
    "directory", (".", "../quarantine", "C:/quarantine", "nested\\quarantine")
)
def test_quarantine_view_refuses_a_noncanonical_captured_directory_before_inventory(
    directory: str,
) -> None:
    """Even an empty snapshot cannot publish an invalid namespace root."""
    calls: list[str] = []

    class Candidate:
        def __init__(self) -> None:
            self.directory = directory

        def inventory(self) -> tuple[()]:
            calls.append("inventory")
            return ()

    with pytest.raises(GrafxConfigurationError) as raised:
        public_views_module._quarantine_view(Candidate())

    assert raised.value.details == {
        "field": "quarantine.directory",
        "value": "noncanonical",
    }
    assert calls == []


def test_database_quarantine_view_captures_inventory_once_and_derives_legacy_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete evidence is public while the legacy list comes from the same snapshot."""
    capability_calls = _hook_counts()
    complete = _hostile_quarantine_inventory_item(capability_calls)
    incomplete = QuarantineInventoryItem(
        name="orphan",
        state="incomplete",
        files=("quarantine/orphan/manifest.json",),
        manifest_file="quarantine/orphan/manifest.json",
        detail="The payload has not been published.",
    )
    inventory_calls: list[int] = []

    with connect(":memory:") as database:
        quarantine = database._quarantine
        original_inventory = QuarantineStore.inventory

        def captured_once(
            candidate: QuarantineStore,
        ) -> tuple[QuarantineInventoryItem, ...]:
            if candidate is quarantine:
                inventory_calls.append(1)
                return _CapabilityTuple((complete, incomplete), capability_calls)
            return original_inventory(candidate)

        def legacy_scan_forbidden(
            _candidate: QuarantineStore,
        ) -> tuple[QuarantineEntry, ...]:
            raise AssertionError("the public view must derive entries from inventory")

        monkeypatch.setattr(QuarantineStore, "inventory", captured_once)
        monkeypatch.setattr(QuarantineStore, "list", legacy_scan_forbidden)
        view = database.quarantine

    observed = view.inventory()
    assert inventory_calls == [1]
    assert [item.state for item in observed] == ["complete", "incomplete"]
    assert [item.name for item in observed] == [complete.name, "orphan"]
    assert all(type(item.name) is str for item in observed)
    assert type(observed[0].files) is tuple
    assert all(type(file) is str for item in observed for file in item.files)
    assert view.count() == 1
    assert view.list() == (observed[0].entry,)
    _assert_capability_free(view, surface="database.quarantine")
    assert capability_calls == _hook_counts()


def test_database_quarantine_view_distinguishes_a_captured_empty_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory always supplies a tuple, including a conclusively empty one."""
    with connect(":memory:") as database:
        quarantine = database._quarantine
        calls: list[int] = []

        def empty(candidate: QuarantineStore) -> tuple[QuarantineInventoryItem, ...]:
            if candidate is quarantine:
                calls.append(1)
                return ()
            raise AssertionError("an unrelated quarantine store was inspected")

        monkeypatch.setattr(QuarantineStore, "inventory", empty)
        view = database.quarantine

    assert calls == [1]
    assert view.captured_inventory == ()
    assert view.inventory() == ()
    assert view.list() == ()


def test_database_quarantine_receipts_cross_as_exact_immutable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit receipt read strips host subclasses without exposing its store."""
    calls = _hook_counts()
    with connect(":memory:") as database:
        quarantine = database._quarantine
        original = QuarantineStore.receipts

        def hostile_receipts(candidate: QuarantineStore, name: str) -> tuple[str, ...]:
            if candidate is quarantine:
                assert type(name) is str
                assert name == "evidence"
                return _CapabilityTuple(
                    (_CapabilityText("quarantine/evidence/restore-1.json", calls),),
                    calls,
                )
            return original(candidate, name)

        monkeypatch.setattr(QuarantineStore, "receipts", hostile_receipts)
        observed = database.quarantine_receipts(_CapabilityText("evidence", calls))

    assert type(observed) is tuple
    assert observed == ("quarantine/evidence/restore-1.json",)
    assert type(observed[0]) is str
    assert calls == _hook_counts()


@pytest.mark.parametrize("operation", ("read", "receipts"))
def test_explicit_quarantine_reads_defer_reentrant_close_until_the_result_is_safe(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback may seal the facade but cannot release the store beneath an active read."""
    database = connect(":memory:")
    quarantine = database._quarantine
    original = getattr(QuarantineStore, operation)
    callbacks: list[str] = []

    def close_then_return(candidate: QuarantineStore, name: str) -> object:
        if candidate is quarantine:
            assert type(name) is str
            database.close()
            assert database.closed
            assert not database.close_complete
            callbacks.append(operation)
            if operation == "read":
                return b"verified evidence"
            return ("quarantine/evidence/restore-1.json",)
        return original(candidate, name)

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(QuarantineStore, operation, close_then_return)
            if operation == "read":
                observed = database.read_quarantine("evidence")
                assert observed == b"verified evidence"
            else:
                observed = database.quarantine_receipts("evidence")
                assert observed == ("quarantine/evidence/restore-1.json",)
    finally:
        database.close()

    assert callbacks == [operation]
    assert database.close_complete


def test_every_component_property_returns_the_allowlisted_immutable_value_graph() -> (
    None
):
    """No view retains a raw engine, mutable container, callback or conventional backdoor."""
    with connect(":memory:") as database:
        # Non-empty schema and index inventories make the walk cover the nested domain DTOs,
        # rather than proving only that an empty tuple is harmless.
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
        for name, expected_type in PUBLIC_DATABASE_VIEW_ALLOWLIST:
            view = getattr(database, name)
            assert type(view) is expected_type, name
            assert is_dataclass(view), name
            _assert_capability_free(view, surface=name)


def test_coordinator_view_never_enters_liveness_lease_or_retirement_doors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the view cannot prune reader pins or perturb lease/liveness bookkeeping."""
    with connect(":memory:") as database:
        coordinator = database._coordinator
        expected_owner = coordinator.owner_id()
        called: list[str] = []

        def forbidden(name: str):
            def refuse(*_args: object, **_kwargs: object) -> object:
                called.append(name)
                raise AssertionError(
                    f"coordinator operation {name} was called by its view"
                )

            return refuse

        operational = (
            "current_epoch",
            "acquire_writer_lease",
            "renew_lease",
            "release_lease",
            "validate_epoch",
            "detect_dead_owner",
            "takeover",
            "register_reader",
            "refresh_reader",
            "unregister_reader",
            "reader_horizon",
            "exclusive",
        )
        # Restore the real doors before Database.close releases its own resources.
        with monkeypatch.context() as boundary:
            for name in operational:
                boundary.setattr(coordinator, name, forbidden(name))
            view = database.coordinator

        assert called == []
        assert view.owner_id() == expected_owner
        assert view.implementation == type(coordinator).__name__


def test_clock_view_does_not_advance_an_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harmless property read cannot move liveness time in a stateful clock adapter."""
    with connect(":memory:") as database:
        clock = database._clock

        def refuse(_self: object) -> float:
            raise AssertionError(
                "a clock source was advanced while building its identity view"
            )

        # Database.close may legitimately stamp or measure cleanup, so the sentinels live only
        # across construction of the public view.
        with monkeypatch.context() as boundary:
            boundary.setattr(type(clock), "monotonic", refuse)
            boundary.setattr(type(clock), "wall", refuse)
            view = database.clock

        assert view.implementation == type(clock).__name__


@pytest.mark.parametrize("invalid", ["foreign", "finished", "not_refused"])
def test_retry_rejects_invalid_transactions_before_schema_bookkeeping(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership, state and conflict validation all precede query-schema settlement."""
    settled: list[tuple[int, bool]] = []
    original = Database._settle_schema

    def observe(
        self: Database, context: TransactionContext, *, committed: bool
    ) -> None:
        settled.append((context.txn_id, committed))
        original(self, context, committed=committed)

    monkeypatch.setattr(Database, "_settle_schema", observe)
    first = connect(":memory:")
    second = connect(":memory:")
    try:
        transaction = (
            second.begin("write") if invalid == "foreign" else first.begin("write")
        )
        if invalid == "finished":
            transaction.rollback()
        settled.clear()
        with pytest.raises(GrafxTransactionStateError):
            first.retry(transaction)
        assert settled == []
    finally:
        # Restore first so ordinary close/rollback bookkeeping does not pollute this assertion.
        monkeypatch.setattr(Database, "_settle_schema", original)
        first.close()
        second.close()


def test_vector_search_accepts_only_an_active_transaction_of_the_same_database() -> (
    None
):
    """The safe vector read door never accepts a raw context, snapshot or foreign transaction."""
    first = connect(":memory:")
    second = connect(":memory:")
    try:
        foreign = second.begin("read")
        with pytest.raises(GrafxTransactionStateError) as refused:
            first.search_vectors(foreign, space="missing", query=(1.0,), k=1)
        assert refused.value.details["field"] == "transaction_owner"

        finished = first.begin("read")
        finished.rollback()
        with pytest.raises(GrafxTransactionStateError):
            first.search_vectors(finished, space="missing", query=(1.0,), k=1)
    finally:
        first.close()
        second.close()


def test_retry_still_returns_a_fresh_public_transaction_after_a_real_conflict() -> None:
    """Sealing TransactionContext does not cost the documented optimistic retry loop."""
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))"
            )
        with database.begin("write") as seed:
            seed.execute("CREATE (:P {id: 1, name: 'seed'})")

        winner = database.begin("write")
        loser = database.begin("write")
        winner.execute("MATCH (p:P {id: 1}) SET p.name = 'winner'")
        loser.execute("MATCH (p:P {id: 1}) SET p.name = 'loser'")
        winner.commit()
        with pytest.raises(GrafxWriteConflict):
            loser.commit()

        successor = database.retry(loser)
        assert loser.active is False
        assert successor.active is True
        assert successor.snapshot.read_lsn > loser.snapshot.read_lsn
        successor.execute("MATCH (p:P {id: 1}) SET p.name = 'retried'")
        successor.commit()
        assert database.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (
            ("retried",),
        )
