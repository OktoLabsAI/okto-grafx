"""The in-memory catalog and the bytes it is persisted as (CONTRACT.md section 7.2).

The catalog is the answer to "what tables and what embedding spaces exist". It is a value with
rules rather than a container: table definitions are immutable values (the explicit
nullable-column capability may install an append-only replacement), a space is added once and
the only change it ever accepts is from active to retired (SPEC-VEC TR-2), and a vector column
may only point at a space that already exists and still accepts writes.

Its serialised form is self-describing and checksummed, because the catalog is what every other
file is interpreted through: losing it silently would turn every heap page into an unreadable
blob. The common layout is

    magic 8B "GRFXCTLG" | format_version u16 | reserved u16 |
    table_count u32 | space_count u32 | next_table_id u32 | next_space_id u32 |
    [v2: required_capabilities u64 | index_count u32 | reserved u32] |
    [commit_catalog_v1: activation_commit_lsn u64] |
    tables | spaces | [v2: indexes] | crc32c u32

with tables ordered by table_id and spaces ordered by space_id, so the same catalog always
serialises to the same bytes.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from types import MappingProxyType
from typing import NoReturn
from dataclasses import replace

from okto_grafx.domain.ids import PROVISIONAL_CSN

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
)
from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    ORDERED_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    ORDERED_KEY_DERIVATION,
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    automatic_index_definitions,
)
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.fulltext import (
    FULLTEXT_CAPABILITY, is_fulltext, FULLTEXT_STATISTICS_CAPABILITY, has_durable_statistics,
    FULLTEXT_HISTORY_CAPABILITY, has_historical_statistics,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.index.keys import LARGE_HASH_CAPABILITY, LEGACY_MAX_BUCKET_COUNT
from okto_grafx.domain.model.schema import (
    SPACE_STATE_ACTIVE,
    ColumnDef,
    EmbeddingSpaceDef,
    TableDef,
)
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.ports.vectormath import DistanceMetric

__all__ = [
    "CATALOG_LEGACY_FORMAT_VERSION",
    "CATALOG_MAGIC",
    "CATALOG_FORMAT_VERSION",
    "HEAP_RECLAIM_V1_CAPABILITY",
    "ORDERED_SECONDARY_INDEXES_V1_CAPABILITY",
    "WAL_RECORD_V2_CAPABILITY",
    "COMMIT_CATALOG_V1_CAPABILITY",
    "Catalog",
]

CATALOG_MAGIC: bytes = b"GRFXCTLG"
"""The eight bytes that open a serialised catalog."""

CATALOG_LEGACY_FORMAT_VERSION: int = 1
"""The format kept by ordinary databases until P2-ID is explicitly activated."""

CATALOG_FORMAT_VERSION: int = 2
"""The newest catalog format this build can read and write."""

HEAP_RECLAIM_V1_CAPABILITY: str = "heap_reclaim_v1"
"""Required capability guarding the durable heap snapshot floor and physical reclamation."""

WAL_RECORD_V2_CAPABILITY: str = "wal_record_v2"
"""Required capability allowing compressed WRITE_PAGE records in retained WAL."""

COMMIT_CATALOG_V1_CAPABILITY: str = "commit_catalog_v1"
"""Required capability guarding the persisted commit-history activation horizon."""

HEAP_FREE_INDEX_CAPABILITY: str = "heap_free_page_index_v1"
"""Required capability for the heap-header linked retired-page directory."""

SPARSE_HASH_CAPABILITY: str = "sparse_hash_directories_v1"
"""Required capability for exact sparse-hash bucket directories."""

_PREAMBLE = struct.Struct("<8sHHIIII")
_V2_EXTENSION = struct.Struct("<QII")
_INDEX_META = struct.Struct("<BBBBHHQ")
_INDEX_GENERATION = struct.Struct("<QIB3x")
_U8 = struct.Struct("<B")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_F64 = struct.Struct("<d")
_CHECKSUM = struct.Struct("<I")
_MAX_TEXT = 0xFFFF

_IDENTITY_SECONDARY_INDEXES_V1_BIT = 1 << 0
_HEAP_RECLAIM_V1_BIT = 1 << 1
_WAL_RECORD_V2_BIT = 1 << 2
_ORDERED_SECONDARY_INDEXES_V1_BIT = 1 << 3
_COMMIT_CATALOG_V1_BIT = 1 << 4
_FULLTEXT_V1_BIT = 1 << 5
_FULLTEXT_STATISTICS_V1_BIT = 1 << 6
_LARGE_HASH_V1_BIT = 1 << 7
_FULLTEXT_HISTORY_V1_BIT = 1 << 8
_HEAP_FREE_INDEX_BIT = 1 << 9
_SPARSE_HASH_BIT = 1 << 10
_FULLTEXT_PREFIX_BIT = 1 << 11
_FULLTEXT_RELATIONSHIPS_BIT = 1 << 12
_NULLABLE_COLUMNS_BIT = 1 << 13
_POSTING_HASH_BIT = 1 << 14
_SYSTEM_HISTORY_BIT = 1 << 15
_FULLTEXT_POSITIONS_BIT = 1 << 16
SYSTEM_HISTORY_CAPABILITY = "system_history_v1"


_KNOWN_CAPABILITY_BITS = (
    _IDENTITY_SECONDARY_INDEXES_V1_BIT
    | _HEAP_RECLAIM_V1_BIT
    | _WAL_RECORD_V2_BIT
    | _ORDERED_SECONDARY_INDEXES_V1_BIT
    | _COMMIT_CATALOG_V1_BIT
    | _FULLTEXT_V1_BIT
    | _FULLTEXT_STATISTICS_V1_BIT
    | _LARGE_HASH_V1_BIT
    | _FULLTEXT_HISTORY_V1_BIT
    | _HEAP_FREE_INDEX_BIT
    | _SPARSE_HASH_BIT
    | _FULLTEXT_PREFIX_BIT
    | _FULLTEXT_RELATIONSHIPS_BIT
    | _NULLABLE_COLUMNS_BIT
    | _POSTING_HASH_BIT
    | _SYSTEM_HISTORY_BIT
    | _FULLTEXT_POSITIONS_BIT
)
_CAPABILITY_TO_BIT = MappingProxyType(
    {
        IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY: _IDENTITY_SECONDARY_INDEXES_V1_BIT,
        HEAP_RECLAIM_V1_CAPABILITY: _HEAP_RECLAIM_V1_BIT,
        WAL_RECORD_V2_CAPABILITY: _WAL_RECORD_V2_BIT,
        ORDERED_SECONDARY_INDEXES_V1_CAPABILITY: (
            _ORDERED_SECONDARY_INDEXES_V1_BIT
        ),
        COMMIT_CATALOG_V1_CAPABILITY: _COMMIT_CATALOG_V1_BIT,
        FULLTEXT_CAPABILITY: _FULLTEXT_V1_BIT,
        FULLTEXT_STATISTICS_CAPABILITY: _FULLTEXT_STATISTICS_V1_BIT,
        LARGE_HASH_CAPABILITY: _LARGE_HASH_V1_BIT,
        FULLTEXT_HISTORY_CAPABILITY: _FULLTEXT_HISTORY_V1_BIT,
        HEAP_FREE_INDEX_CAPABILITY: _HEAP_FREE_INDEX_BIT,
        SPARSE_HASH_CAPABILITY: _SPARSE_HASH_BIT,
        "fulltext_prefixes_v1": _FULLTEXT_PREFIX_BIT,
        "fulltext_relationships_v1": _FULLTEXT_RELATIONSHIPS_BIT,
        "nullable_columns_v1": _NULLABLE_COLUMNS_BIT,
        "posting_hash_v1": _POSTING_HASH_BIT,
        SYSTEM_HISTORY_CAPABILITY: _SYSTEM_HISTORY_BIT,
        "fulltext_positions_v1": _FULLTEXT_POSITIONS_BIT,
    }
)
_VISIBILITY_TO_TAG = MappingProxyType({IndexVisibility.EXACT: 1})
_TAG_TO_VISIBILITY = MappingProxyType(
    {value: key for key, value in _VISIBILITY_TO_TAG.items()}
)
_DERIVATION_TO_TAG = MappingProxyType(
    {
        COLUMN_KEY_DERIVATION: 1,
        RECORD_ID_KEY_DERIVATION: 2,
        ORDERED_KEY_DERIVATION: 3,
    }
)
_TAG_TO_DERIVATION = MappingProxyType(
    {value: key for key, value in _DERIVATION_TO_TAG.items()}
)
_STATE_TO_TAG = MappingProxyType(
    {
        IndexGenerationState.BUILDING: 1,
        IndexGenerationState.ACTIVE: 2,
        IndexGenerationState.STALE: 3,
    }
)
_TAG_TO_STATE = MappingProxyType({value: key for key, value in _STATE_TO_TAG.items()})
_LAYOUT_TO_TAG = MappingProxyType(
    {IndexLayout.HASH: 0, IndexLayout.ORDERED: 1, IndexLayout.SPARSE_HASH: 2, IndexLayout.POSTING_HASH: 3}
)
_TAG_TO_LAYOUT = MappingProxyType(
    {value: key for key, value in _LAYOUT_TO_TAG.items()}
)


def _require_commit_catalog_sequence(sequence: int) -> None:
    if type(sequence) is not int or not 0 < sequence < PROVISIONAL_CSN:
        raise GrafxConfigurationError(
            "Invalid commit catalog activation sequence.", field="commit_catalog_activation"
        )


def _large_hash_generations(definitions: Iterable[CatalogIndexDefinition]) -> bool:
    return any(g.bucket_count > LEGACY_MAX_BUCKET_COUNT for d in definitions for g in d.generations)


class Catalog:
    """The set of tables and embedding spaces of one database.

    Identity is immutable by construction: a table or a space is installed once, and the only
    sanctioned change to either is retiring a space. Everything that would mutate an existing
    definition is refused with a typed error rather than applied.
    """

    __slots__ = (
        "_tables",
        "_tables_by_id",
        "_spaces",
        "_spaces_by_id",
        "_format_version",
        "_required_capabilities",
        "_commit_catalog_activation",
        "_system_history",
        "_system_history_revision",
        "_system_history_pins",
        "_indexes",
        "_indexes_by_key",
        "_index_definitions_by_table",
        "_active_index_definitions_memo",
        "_serialized_memo",
    )

    def __init__(self) -> None:
        """Build an empty catalog."""
        self._tables: dict[str, TableDef] = {}
        self._tables_by_id: dict[int, TableDef] = {}
        self._spaces: dict[str, EmbeddingSpaceDef] = {}
        self._spaces_by_id: dict[int, EmbeddingSpaceDef] = {}
        self._format_version: int = CATALOG_LEGACY_FORMAT_VERSION
        self._required_capabilities: frozenset[str] = frozenset()
        self._commit_catalog_activation: int | None = None
        self._system_history: dict[int, tuple[int, int]] = {}
        self._system_history_revision = 0
        self._system_history_pins: dict[str, tuple[int, tuple[int, ...]]] = {}
        self._indexes: dict[str, CatalogIndexDefinition] = {}
        self._indexes_by_key: dict[str, CatalogIndexDefinition] = {}
        self._index_definitions_by_table: dict[
            tuple[int, str], tuple[CatalogIndexDefinition, ...]
        ] = {}
        self._active_index_definitions_memo: tuple[IndexDefinition, ...] | None = None
        self._serialized_memo: bytes | None = None

    # --- reading ---------------------------------------------------------------------------

    def tables(self) -> tuple[TableDef, ...]:
        """Return every table, ordered by table_id."""
        return tuple(self._tables_by_id[key] for key in sorted(self._tables_by_id))

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        """Return every embedding space, ordered by space_id."""
        return tuple(self._spaces_by_id[key] for key in sorted(self._spaces_by_id))

    @property
    def format_version(self) -> int:
        """Return the exact format this value will preserve when serialized."""

        return self._format_version

    def required_capabilities(self) -> tuple[str, ...]:
        """Return required feature capabilities in deterministic spelling order."""

        return tuple(sorted(self._required_capabilities))

    def requires_capability(self, capability: str) -> bool:
        """Test one required capability without allocating the public ordered snapshot."""

        return capability in self._required_capabilities

    @property
    def commit_catalog_activation(self) -> int | None:
        """The persisted legacy horizon, not inferred from missing history files."""
        return self._commit_catalog_activation

    def enable_commit_catalog(self, activation_sequence: int) -> Catalog:
        """Set a one-way horizon on a detached value; this does not enable a public API."""
        self._require_index_catalog()
        _require_commit_catalog_sequence(activation_sequence)
        if self._commit_catalog_activation is not None:
            if self._commit_catalog_activation != activation_sequence:
                raise GrafxConfigurationError(
                    "An activated commit catalog cannot move its legacy boundary.",
                    field="commit_catalog_activation",
                )
            return self
        self._commit_catalog_activation = activation_sequence
        self._required_capabilities = frozenset((*self._required_capabilities, COMMIT_CATALOG_V1_CAPABILITY))
        self._invalidate_derived()
        return self

    def _retarget_commit_catalog_activation(self, old_sequence: int, new_sequence: int) -> None:
        """Rebind a detached, unpublished activation image during exact WAL sizing."""
        _require_commit_catalog_sequence(old_sequence)
        _require_commit_catalog_sequence(new_sequence)
        if self._commit_catalog_activation != old_sequence or not self.requires_capability(COMMIT_CATALOG_V1_CAPABILITY):
            raise GrafxConfigurationError("Activation retarget baseline differs.", field="commit_catalog_activation")
        self._commit_catalog_activation = new_sequence
        self._invalidate_derived()

    def system_history_tables(self) -> tuple[tuple[int, int, int], ...]:
        """Return table ID, activation COMMIT and retained horizon, ordered by ID."""
        return tuple((key, *self._system_history[key]) for key in sorted(self._system_history))

    def enable_system_history(self, table_ids: tuple[int, ...], sequence: int) -> Catalog:
        """Opt detached table definitions into one-way temporal publication."""
        self._require_index_catalog()
        _require_commit_catalog_sequence(sequence)
        if self._commit_catalog_activation is None:
            raise GrafxConfigurationError("System history requires commit history.", field="commit_catalog_activation")
        if type(table_ids) is not tuple or not table_ids or len(set(table_ids)) != len(table_ids):
            raise GrafxConfigurationError("Specify distinct history tables.", field="tables")
        for key in table_ids:
            if type(key) is not int or key not in self._tables_by_id:
                raise GrafxConfigurationError("Unknown history table.", field="tables")
        for key in table_ids:
            self._system_history.setdefault(key, (sequence, sequence))
        self._required_capabilities = frozenset((*self._required_capabilities, SYSTEM_HISTORY_CAPABILITY))
        self._invalidate_derived()
        return self

    def _retarget_system_history(self, table_ids: tuple[int, ...], old: int, new: int) -> None:
        _require_commit_catalog_sequence(new)
        if any(self._system_history.get(key) != (old, old) for key in table_ids):
            raise GrafxConfigurationError("History activation retarget differs.", field="system_history")
        for key in table_ids:
            self._system_history[key] = (new, new)
        self._invalidate_derived()

    @property
    def system_history_revision(self) -> int:
        """Return the COMMIT of the latest native retention rewrite, or zero."""
        return self._system_history_revision

    def system_history_pins(self) -> tuple[tuple[str, int, tuple[int, ...]], ...]:
        """Return durable explicit pins; they survive crashes until explicitly released."""
        return tuple((name, *self._system_history_pins[name]) for name in sorted(self._system_history_pins))

    def set_system_history_pin(self, name: str, sequence: int, tables: tuple[int, ...]) -> bool:
        """Pin a detached catalog's table horizons without involving MVCC or WAL recycling."""
        _require_commit_catalog_sequence(sequence)
        if (type(name) is not str or not name or len(name.encode("utf-8")) > 128
                or type(tables) is not tuple or not tables or any(type(key) is not int for key in tables)
                or tuple(sorted(set(tables))) != tables
                or any(key not in self._system_history or self._system_history[key][1] > sequence for key in tables)):
            raise GrafxConfigurationError("Invalid history pin.", field="system_history_pin")
        previous = self._system_history_pins.get(name)
        if previous is not None:
            if previous != (sequence, tables):
                raise GrafxConfigurationError("History pin name is already bound.", field="system_history_pin")
            return False
        if len(self._system_history_pins) >= 1024:
            raise GrafxConfigurationError("History pin limit is 1024.", field="system_history_pin")
        self._system_history_pins[name] = (sequence, tables)
        self._invalidate_derived()
        return True

    def remove_system_history_pin(self, name: str) -> bool:
        """Explicitly release a detached durable pin; a missing name is a no-op."""
        if type(name) is not str or not name:
            raise GrafxConfigurationError("Invalid history pin name.", field="system_history_pin")
        if self._system_history_pins.pop(name, None) is None:
            return False
        self._invalidate_derived()
        return True

    def advance_system_history(self, tables: tuple[int, ...], before: int, revision: int) -> bool:
        """Advance retained horizons after proving all explicit pins permit pruning."""
        _require_commit_catalog_sequence(before)
        _require_commit_catalog_sequence(revision)
        if (type(tables) is not tuple or not tables or any(type(key) is not int for key in tables)
                or tuple(sorted(set(tables))) != tables or any(key not in self._system_history for key in tables)):
            raise GrafxConfigurationError("Enable history on the selected tables first.", field="tables")
        if any(before < self._system_history[key][1] for key in tables):
            raise GrafxConfigurationError("History retention cannot move backwards.", field="before")
        if any(sequence < before and set(tables).intersection(pinned)
               for sequence, pinned in self._system_history_pins.values()):
            raise GrafxConfigurationError("A durable history pin protects this interval.", field="system_history_pin")
        if all(before == self._system_history[key][1] for key in tables):
            return False
        for key in tables:
            self._system_history[key] = (self._system_history[key][0], before)
        self._system_history_revision = revision
        self._invalidate_derived()
        return True

    def _retarget_history_revision(self, old: int, new: int) -> None:
        _require_commit_catalog_sequence(new)
        if self._system_history_revision != old:
            raise GrafxConfigurationError("Retention revision retarget differs.", field="system_history_revision")
        self._system_history_revision = new
        self._invalidate_derived()

    def index_definitions(self) -> tuple[CatalogIndexDefinition, ...]:
        """Return catalog-managed exact indexes in canonical registry order."""

        return tuple(self._indexes_by_key[key] for key in sorted(self._indexes_by_key))

    def active_index_definitions(self) -> tuple[IndexDefinition, ...]:
        """Project the complete runtime index authority for this catalog version.

        A legacy v1 catalog has no persisted logical index records, so all of its automatic
        definitions continue to come from the committed table schema.  Once v2 is active, exact
        indexes come only from persisted logical definitions whose physical generation is
        ``ACTIVE``; building and stale generations are deliberately invisible.  Specialized
        proximity/vector definitions remain schema-derived in both formats because P2-ID does
        not persist them as generic exact definitions.

        The result is ordered by the registry's case-insensitive key so every runtime consumer
        can install the same authority deterministically.
        """

        memo = self._active_index_definitions_memo
        if memo is not None:
            return memo
        definitions = (
            definition
            for table in self.tables()
            for definition in self.active_index_definitions_for(
                table.table_id, table_name=table.name
            )
        )
        projected = tuple(
            sorted(
                definitions,
                key=lambda definition: definition.registry_key,
            )
        )
        self._active_index_definitions_memo = projected
        return projected

    def active_index_definitions_for(
        self, table_id: int, *, table_name: str | None = None
    ) -> tuple[IndexDefinition, ...]:
        """Project runtime authority for one complete table identity.

        Catalog v2 keeps a structural secondary map of its immutable logical definitions, so a
        row write pays only for indexes of its own table.  The map is rebuilt with every
        authority installation; an in-place ACTIVE-generation replacement therefore cannot
        leave a stale cached projection behind.  Legacy automatic and specialized vector
        definitions are likewise derived only from the requested table.

        A table absent from this durable catalog has no committed authority.  Returning an empty
        tuple is intentional: a transaction may still maintain a table/index pair from its own
        speculative schema observation without granting that pair to any other transaction.
        """

        table = self._tables_by_id.get(table_id)
        if table is None or (table_name is not None and table.name != table_name):
            return ()
        if self._format_version == CATALOG_LEGACY_FORMAT_VERSION:
            return tuple(
                sorted(
                    automatic_index_definitions(table),
                    key=lambda definition: definition.registry_key,
                )
            )

        exact_definitions: list[IndexDefinition] = []
        for logical_definition in self._index_definitions_by_table.get(
            (table.table_id, table.name), ()
        ):
            generation = logical_definition.active_generation()
            if generation is not None:
                exact_definitions.append(
                    logical_definition.runtime_definition(generation)
                )

        # Do not compose the legacy PK/endpoint half here: v2 owns those paths through exact
        # persisted generations.  P2-ID leaves only proximity/vector definitions schema-derived.
        specialized_definitions = tuple(
            definition
            for definition in automatic_index_definitions(table)
            if definition.visibility is IndexVisibility.PROXIMITY
        )
        return tuple(
            sorted(
                (*exact_definitions, *specialized_definitions),
                key=lambda definition: definition.registry_key,
            )
        )

    def has_index_definition(self, name: str) -> bool:
        """Return whether a catalog-managed logical index has this folded name."""

        return isinstance(name, str) and name.lower() in self._indexes_by_key

    def index_definition(self, name: str) -> CatalogIndexDefinition:
        """Return one catalog-managed definition by case-insensitive logical name."""

        key = name.lower() if isinstance(name, str) else ""
        try:
            return self._indexes_by_key[key]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"There is no catalog-managed index named {name!r}.",
                field="index",
                value=repr(name),
            ) from failure

    def has_table(self, name: str) -> bool:
        """Return True when a table with that name exists."""
        return name in self._tables

    def has_space(self, name: str) -> bool:
        """Return True when an embedding space with that name exists."""
        return name in self._spaces

    def table(self, name: str) -> TableDef:
        """Return the table with that name."""
        try:
            return self._tables[name]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"There is no table named {name!r} in this catalog.",
                field="table",
                value=name,
            ) from failure

    def table_by_id(self, table_id: int) -> TableDef:
        """Return the table with that numeric id, which is what a heap page records."""
        try:
            return self._tables_by_id[table_id]
        except KeyError as failure:
            raise GrafxCorruptionDetected(
                f"There is no table with id {table_id} in this catalog.",
                field="table_id",
                value=table_id,
            ) from failure

    def space(self, name: str) -> EmbeddingSpaceDef:
        """Return the embedding space with that name."""
        try:
            return self._spaces[name]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"There is no embedding space named {name!r} in this catalog.",
                field="space",
                value=name,
            ) from failure

    def space_by_id(self, space_id: int) -> EmbeddingSpaceDef:
        """Return the embedding space with that numeric id, which is what a vector records."""
        try:
            return self._spaces_by_id[space_id]
        except KeyError as failure:
            raise GrafxCorruptionDetected(
                f"There is no embedding space with id {space_id} in this catalog.",
                field="space_ref",
                value=space_id,
            ) from failure

    def next_table_id(self) -> int:
        """Return the id the next table should take, which is one past the highest in use."""
        return max(self._tables_by_id, default=0) + 1

    def next_space_id(self) -> int:
        """Return the id the next embedding space should take, one past the highest in use."""
        return max(self._spaces_by_id, default=0) + 1

    def is_empty(self) -> bool:
        """Return True when the catalog holds neither a table nor an embedding space."""
        return not self._tables and not self._spaces

    # --- writing ---------------------------------------------------------------------------

    def upgrade_index_catalog(
        self, definitions: Iterable[CatalogIndexDefinition] = ()
    ) -> Catalog:
        """Explicitly cross the one-way v1-to-v2 capability boundary.

        Validation is complete before this object changes. Repeating the exact activation is a
        no-op; changing an already-active authority uses the dedicated add/replace operations.
        """

        try:
            proposed = tuple(definitions)
        except TypeError as failure:
            raise GrafxConfigurationError(
                "Index-catalog activation needs an iterable of definitions.",
                field="definitions",
                value=type(definitions).__name__,
            ) from failure
        validated = self._validated_index_authority(
            proposed,
            stored=False,
            require_endpoint_identity=True,
            require_active_identity=True,
        )
        if self._format_version == CATALOG_FORMAT_VERSION:
            if self.index_definitions() == tuple(
                validated[key] for key in sorted(validated)
            ):
                return self
            raise GrafxConfigurationError(
                "Catalog format 2 is already active; use add_index_definition or "
                "replace_index_definition to change its authority.",
                field="format_version",
                value=self._format_version,
            )
        if self._format_version != CATALOG_LEGACY_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"Catalog format {self._format_version} cannot be upgraded by this build.",
                field="format_version",
                value=self._format_version,
                supported=CATALOG_FORMAT_VERSION,
            )
        self._format_version = CATALOG_FORMAT_VERSION
        capabilities = {IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY}
        if any(
            definition.layout is IndexLayout.ORDERED
            for definition in validated.values()
        ):
            capabilities.add(ORDERED_SECONDARY_INDEXES_V1_CAPABILITY)
        if any(is_fulltext(definition.key_derivation) for definition in validated.values()):
            capabilities.add(FULLTEXT_CAPABILITY)
        if any(d.key_derivation.startswith("fulltext_v4_") for d in validated.values()):
            capabilities.add("fulltext_prefixes_v1")
        if any(d.key_derivation.startswith("fulltext_v5_") for d in validated.values()):
            capabilities.add("fulltext_positions_v1")
        if any(is_fulltext(d.key_derivation) and self.table_by_id(d.table_id).kind == "rel" for d in validated.values()):
            capabilities.add("fulltext_relationships_v1")
        if any(has_durable_statistics(d.key_derivation) for d in validated.values()):
            capabilities.add(FULLTEXT_STATISTICS_CAPABILITY)
        if any(has_historical_statistics(d.key_derivation) for d in validated.values()):
            capabilities.add(FULLTEXT_HISTORY_CAPABILITY)
        if _large_hash_generations(validated.values()):
            capabilities.add(LARGE_HASH_CAPABILITY)
        if any(d.layout is IndexLayout.SPARSE_HASH for d in validated.values()):
            capabilities.add(SPARSE_HASH_CAPABILITY)
        if any(d.layout is IndexLayout.POSTING_HASH for d in validated.values()):
            capabilities.add("posting_hash_v1")
        self._required_capabilities = frozenset(capabilities)
        self._install_indexes(validated)
        return self

    def add_index_definition(
        self, definition: CatalogIndexDefinition
    ) -> CatalogIndexDefinition:
        """Add one v2-managed logical index after validating the complete authority."""

        self._require_index_catalog()
        proposed = (*self.index_definitions(), definition)
        validated = self._validated_index_authority(proposed, stored=False)
        if is_fulltext(definition.key_derivation):
            self._required_capabilities = frozenset((*self._required_capabilities, FULLTEXT_CAPABILITY))
        if definition.key_derivation.startswith("fulltext_v4_"):
            self._required_capabilities = frozenset((*self._required_capabilities, "fulltext_prefixes_v1"))
        if definition.key_derivation.startswith("fulltext_v5_"):
            self._required_capabilities = frozenset((*self._required_capabilities, "fulltext_positions_v1"))
        if is_fulltext(definition.key_derivation) and self.table_by_id(definition.table_id).kind == "rel":
            self._required_capabilities = frozenset((*self._required_capabilities, "fulltext_relationships_v1"))
        if has_durable_statistics(definition.key_derivation):
            self._required_capabilities = frozenset((*self._required_capabilities, FULLTEXT_STATISTICS_CAPABILITY))
        if has_historical_statistics(definition.key_derivation):
            self._required_capabilities = frozenset((*self._required_capabilities, FULLTEXT_HISTORY_CAPABILITY))
        if _large_hash_generations(validated.values()):
            self._required_capabilities = frozenset((*self._required_capabilities, LARGE_HASH_CAPABILITY))
        if any(d.layout is IndexLayout.SPARSE_HASH for d in validated.values()):
            self._required_capabilities = frozenset((*self._required_capabilities, SPARSE_HASH_CAPABILITY))
        if any(d.layout is IndexLayout.POSTING_HASH for d in validated.values()):
            self._required_capabilities = frozenset((*self._required_capabilities, "posting_hash_v1"))
        if definition.layout is IndexLayout.ORDERED:
            self._required_capabilities = frozenset(
                (
                    *self._required_capabilities,
                    ORDERED_SECONDARY_INDEXES_V1_CAPABILITY,
                )
            )
        self._install_indexes(validated)
        return definition

    def enable_heap_reclaim(self, *, index_free_pages: bool = False) -> Catalog:
        """Add the one-way heap-reclaim capability to an already-active v2 catalog.

        The capability is published before any heap floor or reclaimed slot.  Older builds then
        reject the catalog's unknown required bit instead of opening bytes whose MVCC history
        they do not understand.  Repeating the activation is an in-memory no-op.
        """

        self._require_index_catalog()
        capabilities = frozenset(
            (*self._required_capabilities, HEAP_RECLAIM_V1_CAPABILITY)
        )
        if type(index_free_pages) is not bool:
            raise GrafxConfigurationError("index_free_pages must be a bool.", field="index_free_pages")
        if index_free_pages:
            capabilities = frozenset((*capabilities, HEAP_FREE_INDEX_CAPABILITY))
        if capabilities != self._required_capabilities:
            self._required_capabilities = capabilities
            self._invalidate_derived()
        return self

    def enable_wal_record_v2(self) -> Catalog:
        """Add the one-way capability required before emitting any WAL-v2 record."""

        self._require_index_catalog()
        capabilities = frozenset(
            (*self._required_capabilities, WAL_RECORD_V2_CAPABILITY)
        )
        if capabilities != self._required_capabilities:
            self._required_capabilities = capabilities
            self._invalidate_derived()
        return self

    def replace_index_definition(
        self, definition: CatalogIndexDefinition
    ) -> CatalogIndexDefinition:
        """Replace only physical generations or sizing of an existing logical definition."""

        self._require_index_catalog()
        if not isinstance(definition, CatalogIndexDefinition):
            raise GrafxConfigurationError(
                "A catalog index replacement needs a CatalogIndexDefinition.",
                field="definition",
                value=type(definition).__name__,
            )
        existing = self._indexes_by_key.get(definition.registry_key)
        if existing is None:
            raise GrafxConfigurationError(
                f"Index {definition.name!r} is not present and cannot be replaced.",
                field="name",
                value=definition.name,
            )
        if _logical_index_identity(existing) != _logical_index_identity(definition):
            raise GrafxConfigurationError(
                "Replacing an index may change only expected_cardinality and physical "
                "generations, never its logical meaning.",
                field="definition",
                index=existing.name,
            )
        proposed = tuple(
            definition if item.registry_key == definition.registry_key else item
            for item in self.index_definitions()
        )
        validated = self._validated_index_authority(proposed, stored=False)
        if _large_hash_generations(validated.values()):
            self._required_capabilities = frozenset((*self._required_capabilities, LARGE_HASH_CAPABILITY))
        self._install_indexes(validated)
        return definition

    def add_nullable_column(self, name: str, column: ColumnDef) -> TableDef:
        """Append one nullable non-vector column, retaining exact prior decode layouts."""
        self._require_index_catalog()
        table = self.table(name)
        if (type(column) is not ColumnDef or not column.nullable or column.vector_space is not None
                or column.type in (ValueType.VECTOR_F32, ValueType.VECTOR_F64)):
            raise GrafxConfigurationError("Only nullable non-vector columns can be appended.", field="column")
        if len(table.columns) >= 65535:
            raise GrafxConfigurationError("Table column limit reached.", field="columns")
        updated = replace(table, columns=(*table.columns, column), schema_version=table.schema_version + 1,
                          schema_layouts=(*table.schema_layouts, (table.schema_version, len(table.columns))))
        self._required_capabilities = self._required_capabilities | {"nullable_columns_v1"}
        self._install_table(updated)
        return updated

    def add_table(self, table: TableDef) -> TableDef:
        """Install a table, refusing a duplicate name or id and an unusable vector column."""
        if not isinstance(table, TableDef):
            raise GrafxConfigurationError(
                f"A catalog holds TableDef values; got {type(table).__name__}.",
                field="table",
                value=type(table).__name__,
            )
        if table.name in self._tables:
            raise GrafxConfigurationError(
                f"This catalog already has a table named {table.name!r}.",
                field="name",
                value=table.name,
            )
        if table.table_id in self._tables_by_id:
            raise GrafxConfigurationError(
                f"This catalog already has a table with id {table.table_id}, named "
                f"{self._tables_by_id[table.table_id].name!r}.",
                field="table_id",
                value=table.table_id,
            )
        for column in table.columns:
            if not column.is_vector:
                continue
            space_name = str(column.vector_space)
            if space_name not in self._spaces:
                raise GrafxConfigurationError(
                    f"Column {column.name!r} of table {table.name!r} points at the embedding "
                    f"space {space_name!r}, which this catalog does not have.",
                    field="vector_space",
                    value=space_name,
                )
            space = self._spaces[space_name]
            if not space.is_active:
                raise GrafxSpaceRetired(
                    f"Column {column.name!r} of table {table.name!r} points at the retired "
                    f"embedding space {space_name!r}, which accepts no writes.",
                    field="vector_space",
                    value=space_name,
                )
            if space.value_type is not column.type:
                raise GrafxConfigurationError(
                    f"Column {column.name!r} of table {table.name!r} is a "
                    f"{column.type.name} column, but the space {space_name!r} stores "
                    f"{space.value_type.name}.",
                    field="storage_dtype",
                    value=space.storage_dtype,
                )
        self._install_table(table)
        return table

    def add_space(self, space: EmbeddingSpaceDef) -> EmbeddingSpaceDef:
        """Install an embedding space, which is born active and keeps its identity forever."""
        if not isinstance(space, EmbeddingSpaceDef):
            raise GrafxConfigurationError(
                f"A catalog holds EmbeddingSpaceDef values; got {type(space).__name__}.",
                field="space",
                value=type(space).__name__,
            )
        if space.state != SPACE_STATE_ACTIVE:
            raise GrafxConfigurationError(
                f"Embedding space {space.name!r} must be created active; retiring it is a "
                f"separate act.",
                field="state",
                value=space.state,
            )
        if space.name in self._spaces:
            raise GrafxConfigurationError(
                f"This catalog already has an embedding space named {space.name!r}, and space "
                f"identity is immutable after creation.",
                field="name",
                value=space.name,
            )
        if space.space_id in self._spaces_by_id:
            raise GrafxConfigurationError(
                f"This catalog already has an embedding space with id {space.space_id}, named "
                f"{self._spaces_by_id[space.space_id].name!r}.",
                field="space_id",
                value=space.space_id,
            )
        self._install_space(space)
        return space

    def retire_space(self, name: str) -> EmbeddingSpaceDef:
        """Retire an embedding space and return it, refusing a second retirement.

        Retiring is the only change an installed space accepts, and it is one way: a retired
        space stays readable forever and never accepts a write again (SPEC-VEC FR-3).
        """
        space = self.space(name)
        if not space.is_active:
            raise GrafxSpaceRetired(
                f"Embedding space {name!r} is already retired.",
                field="state",
                value=space.state,
                space=name,
            )
        retired = space.retired()
        self._install_space(retired)
        return retired

    # --- serialisation ---------------------------------------------------------------------

    def serialize(self) -> bytes:
        """Return the checksummed bytes of this catalog, in a stable order."""
        # Catalog definitions are immutable values and every sanctioned authority change goes
        # through one of the installers below.  Reusing this exact state's already-validated
        # image therefore removes repeated O(tables + spaces + indexes) encoding from DDL and
        # prepared-plan keys without sharing mutable authority between catalog snapshots.
        # Subclasses retain the historical protocol because they may observe serialization.
        if type(self) is Catalog and self._serialized_memo is not None:
            return self._serialized_memo
        if self._format_version not in {
            CATALOG_LEGACY_FORMAT_VERSION,
            CATALOG_FORMAT_VERSION,
        }:
            raise GrafxSchemaVersionMismatch(
                f"This build cannot serialize catalog format {self._format_version}.",
                field="format_version",
                value=self._format_version,
                supported=CATALOG_FORMAT_VERSION,
            )
        indexes: tuple[CatalogIndexDefinition, ...] = ()
        capability_bits = 0
        if self._format_version == CATALOG_FORMAT_VERSION:
            validated = self._validated_index_authority(
                self.index_definitions(),
                stored=False,
                require_endpoint_identity=True,
            )
            indexes = tuple(validated[key] for key in sorted(validated))
            capability_bits = _encode_capabilities(self._required_capabilities)
            if any(is_fulltext(d.key_derivation) and self.table_by_id(d.table_id).kind == "rel" for d in indexes) and "fulltext_relationships_v1" not in self._required_capabilities:
                raise GrafxConfigurationError("Relationship FTS requires its capability.", field="required_capabilities")
            if any(d.key_derivation.startswith("fulltext_v4_") for d in indexes) and "fulltext_prefixes_v1" not in self._required_capabilities:
                raise GrafxConfigurationError("Prefix postings require their capability.", field="required_capabilities")
            if any(d.key_derivation.startswith("fulltext_v5_") for d in indexes) and "fulltext_positions_v1" not in self._required_capabilities:
                raise GrafxConfigurationError("Positional postings require their capability.", field="required_capabilities")
            if any(is_fulltext(d.key_derivation) for d in indexes) and FULLTEXT_CAPABILITY not in self._required_capabilities:
                raise GrafxConfigurationError("Full-text indexes require their capability.", field="required_capabilities")
            if any(has_durable_statistics(d.key_derivation) for d in indexes) and FULLTEXT_STATISTICS_CAPABILITY not in self._required_capabilities:
                raise GrafxConfigurationError("Durable text statistics require their capability.", field="required_capabilities")
            if any(has_historical_statistics(d.key_derivation) for d in indexes) and FULLTEXT_HISTORY_CAPABILITY not in self._required_capabilities:
                raise GrafxConfigurationError("Historical text statistics require their capability.", field="required_capabilities")
            if _large_hash_generations(indexes) and LARGE_HASH_CAPABILITY not in self._required_capabilities:
                raise GrafxConfigurationError("Large hash directories require their capability.", field="required_capabilities")
            if any(d.layout is IndexLayout.SPARSE_HASH for d in indexes) and SPARSE_HASH_CAPABILITY not in self._required_capabilities:
                raise GrafxConfigurationError("Sparse hash requires its capability.", field="required_capabilities")
            if any(d.layout is IndexLayout.POSTING_HASH for d in indexes) and "posting_hash_v1" not in self._required_capabilities:
                raise GrafxConfigurationError("Posting hash requires its capability.", field="required_capabilities")
            if not capability_bits & _IDENTITY_SECONDARY_INDEXES_V1_BIT:
                raise GrafxConfigurationError(
                    "Catalog format 2 requires identity_secondary_indexes_v1.",
                    field="required_capabilities",
                )
            if any(
                definition.layout is IndexLayout.ORDERED for definition in indexes
            ) and not capability_bits & _ORDERED_SECONDARY_INDEXES_V1_BIT:
                raise GrafxConfigurationError(
                    "An ordered index requires ordered_secondary_indexes_v1.",
                    field="required_capabilities",
                )
        parts: list[bytes] = [
            _PREAMBLE.pack(
                CATALOG_MAGIC,
                self._format_version,
                0,
                len(self._tables_by_id),
                len(self._spaces_by_id),
                self.next_table_id(),
                self.next_space_id(),
            )
        ]
        if self._format_version == CATALOG_FORMAT_VERSION:
            parts.append(_V2_EXTENSION.pack(capability_bits, len(indexes), 0))
        if bool(capability_bits & _COMMIT_CATALOG_V1_BIT) != (self._commit_catalog_activation is not None):
            raise GrafxConfigurationError("Commit catalog horizon and capability differ.", field="commit_catalog_activation")
        if self._commit_catalog_activation is not None:
            _require_commit_catalog_sequence(self._commit_catalog_activation)
            parts.append(_U64.pack(self._commit_catalog_activation))
        if bool(capability_bits & _SYSTEM_HISTORY_BIT) != bool(self._system_history):
            raise GrafxConfigurationError("History tables and capability differ.", field="system_history")
        if self._system_history:
            if self._commit_catalog_activation is None:
                raise GrafxConfigurationError("History requires commit provenance.", field="system_history")
            parts.append(_U32.pack(len(self._system_history)))
            for key, activation, horizon in self.system_history_tables():
                if key not in self._tables_by_id or not 0 < activation <= horizon < PROVISIONAL_CSN:
                    raise GrafxConfigurationError("Invalid history table horizon.", field="system_history")
                parts.append(struct.pack("<IQQ", key, activation, horizon))
            if not 0 <= self._system_history_revision < PROVISIONAL_CSN or len(self._system_history_pins) > 1024:
                raise GrafxConfigurationError("Invalid history retention metadata.", field="system_history")
            parts.append(struct.pack("<QH", self._system_history_revision, len(self._system_history_pins)))
            for name, sequence, pinned in self.system_history_pins():
                parts.extend((_encode_text(name), struct.pack("<QI", sequence, len(pinned))))
                parts.extend(_U32.pack(key) for key in pinned)
        for table in self.tables():
            parts.append(_encode_table(table))
            if "nullable_columns_v1" in self._required_capabilities:
                parts.append(_U16.pack(len(table.schema_layouts)))
                for version, count in table.schema_layouts:
                    parts.append(_U16.pack(version) + _U16.pack(count))
            elif table.schema_layouts:
                raise GrafxConfigurationError("Prior layouts require nullable_columns_v1.", field="required_capabilities")
        for space in self.spaces():
            parts.append(_encode_space(space))
        for definition in indexes:
            parts.append(_encode_catalog_index(definition))
        body = b"".join(parts)
        encoded = body + _CHECKSUM.pack(crc32c(body))
        if type(self) is Catalog:
            self._serialized_memo = encoded
        return encoded

    def copy(self) -> Catalog:
        """Return an independent catalog holding exactly this state.

        Every definition is an immutable value and every sanctioned change installs a
        replacement through one of the installers above, so an exact catalog is cloned by
        copying its dictionaries: the clone shares the ``TableDef``, ``EmbeddingSpaceDef`` and
        ``CatalogIndexDefinition`` instances and the already-validated serialized image, and a
        mutation of either side clears only that side's derived values.  Decoding the serialized
        bytes again -- the previous way to obtain a working copy -- validated nothing the
        installers had not already validated, and cost O(tables x columns) for every DDL
        statement of a transaction.  Whatever the serializer refuses about this state is still
        refused at the next serialization of either copy, because a refused state never earns
        an image.  A subclass keeps the serialized round trip: it may observe serialization.
        """
        if type(self) is not Catalog:
            return Catalog.deserialize(self.serialize())
        clone = Catalog()
        clone._tables = dict(self._tables)
        clone._tables_by_id = dict(self._tables_by_id)
        clone._spaces = dict(self._spaces)
        clone._spaces_by_id = dict(self._spaces_by_id)
        clone._format_version = self._format_version
        clone._required_capabilities = self._required_capabilities
        clone._commit_catalog_activation = self._commit_catalog_activation
        clone._system_history = dict(self._system_history)
        clone._system_history_revision = self._system_history_revision
        clone._system_history_pins = dict(self._system_history_pins)
        clone._indexes = dict(self._indexes)
        clone._indexes_by_key = dict(self._indexes_by_key)
        clone._index_definitions_by_table = dict(self._index_definitions_by_table)
        clone._active_index_definitions_memo = self._active_index_definitions_memo
        clone._serialized_memo = self._serialized_memo
        return clone

    @classmethod
    def deserialize(cls, raw: bytes) -> Catalog:
        """Parse a serialised catalog, refusing foreign bytes, a future format or a bad sum."""
        if len(raw) < _PREAMBLE.size + _CHECKSUM.size:
            raise GrafxCorruptionDetected(
                f"A serialised catalog needs at least "
                f"{_PREAMBLE.size + _CHECKSUM.size} bytes; got {len(raw)}.",
                field="catalog",
                value=len(raw),
            )
        (
            magic,
            format_version,
            reserved,
            table_count,
            space_count,
            next_table,
            next_space,
        ) = _PREAMBLE.unpack_from(raw, 0)
        if magic != CATALOG_MAGIC:
            raise GrafxCorruptionDetected(
                f"These bytes do not start with the catalog magic; got {magic!r}.",
                field="magic",
                value=repr(magic),
            )
        if format_version > CATALOG_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                f"This build reads catalog format {CATALOG_FORMAT_VERSION} and below; the "
                f"stored catalog declares {format_version}.",
                field="format_version",
                value=format_version,
            )
        if format_version < CATALOG_LEGACY_FORMAT_VERSION:
            raise GrafxCorruptionDetected(
                f"The catalog declares invalid format version {format_version}.",
                field="format_version",
                value=format_version,
            )
        body_end = len(raw) - _CHECKSUM.size
        stored = _CHECKSUM.unpack_from(raw, body_end)[0]
        computed = crc32c(bytes(raw[:body_end]))
        if stored != computed:
            raise GrafxCorruptionDetected(
                f"The stored catalog failed its checksum: stored 0x{stored:08x}, computed "
                f"0x{computed:08x}.",
                field="checksum",
                stored_checksum=stored,
                computed_checksum=computed,
            )
        catalog = cls()
        offset = _PREAMBLE.size
        required_capabilities: frozenset[str] = frozenset()
        index_count = 0
        if format_version == CATALOG_FORMAT_VERSION:
            if reserved != 0:
                raise GrafxCorruptionDetected(
                    "Catalog format 2 requires the common reserved word to be zero.",
                    field="reserved",
                    value=reserved,
                    offset=10,
                )
            _require(raw, offset, _V2_EXTENSION.size, "catalog-v2 extension")
            capability_bits, index_count, extension_reserved = (
                _V2_EXTENSION.unpack_from(raw, offset)
            )
            offset += _V2_EXTENSION.size
            required_capabilities = _decode_capabilities(capability_bits)
            if IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected(
                    "Catalog format 2 omits its required identity-index capability.",
                    field="required_capabilities",
                    value=capability_bits,
                )
            if extension_reserved != 0:
                raise GrafxCorruptionDetected(
                    "The catalog-v2 extension reserved word must be zero.",
                    field="reserved",
                    value=extension_reserved,
                    offset=offset - _U32.size,
                )
            if COMMIT_CATALOG_V1_CAPABILITY in required_capabilities:
                if offset + _U64.size > body_end:
                    raise GrafxCorruptionDetected("Missing commit catalog horizon.", field="commit_catalog_activation")
                sequence = _U64.unpack_from(raw, offset)[0]
                try:
                    _require_commit_catalog_sequence(sequence)
                except GrafxConfigurationError:
                    raise GrafxCorruptionDetected("Invalid commit catalog horizon.", field="commit_catalog_activation") from None
                catalog._commit_catalog_activation = sequence
                offset += _U64.size
            if SYSTEM_HISTORY_CAPABILITY in required_capabilities:
                _require(raw, offset, 4, "history tables")
                count = _U32.unpack_from(raw, offset)[0]
                offset += 4
                if not 0 < count <= table_count or catalog._commit_catalog_activation is None:
                    raise GrafxCorruptionDetected("Invalid history table count.", field="system_history")
                _require(raw, offset, count * 20, "history table horizons")
                previous_id = 0
                for _ in range(count):
                    key, activation, horizon = struct.unpack_from("<IQQ", raw, offset)
                    offset += 20
                    if not previous_id < key or not 0 < activation <= horizon < PROVISIONAL_CSN:
                        raise GrafxCorruptionDetected("Invalid history table horizon.", field="system_history")
                    catalog._system_history[key] = (activation, horizon)
                    previous_id = key
                _require(raw, offset, 10, "history retention metadata")
                revision, pin_count = struct.unpack_from("<QH", raw, offset)
                offset += 10
                if revision >= PROVISIONAL_CSN or pin_count > 1024:
                    raise GrafxCorruptionDetected("Invalid history retention metadata.", field="system_history")
                catalog._system_history_revision = revision
                last_name = ""
                for _ in range(pin_count):
                    name, offset = _decode_text(raw, offset)
                    _require(raw, offset, 12, "history pin")
                    sequence, pinned_count = struct.unpack_from("<QI", raw, offset)
                    offset += 12
                    if name <= last_name or not 0 < pinned_count <= count:
                        raise GrafxCorruptionDetected("Invalid history pin order/count.", field="system_history_pin")
                    _require(raw, offset, pinned_count * 4, "history pin tables")
                    pinned = tuple(_U32.unpack_from(raw, offset + index * 4)[0] for index in range(pinned_count))
                    offset += pinned_count * 4
                    try:
                        catalog.set_system_history_pin(name, sequence, pinned)
                    except GrafxConfigurationError as failure:
                        raise GrafxCorruptionDetected("Invalid persisted history pin.", field="system_history_pin") from failure
                    last_name = name
        tables: list[TableDef] = []
        spaces: list[EmbeddingSpaceDef] = []
        indexes: list[CatalogIndexDefinition] = []
        # A schema value validates itself when it is built, and on the CREATE path a refusal is a
        # configuration mistake: a caller named an impossible table. Here the caller is a stored
        # file, and the same refusal means something else entirely -- the bytes do not describe a
        # catalog this build can serve. It is reported as what it is, so that C6 can classify it
        # and record it, and so that this path speaks with one voice: the cross-record invariants
        # below already raise corruption for the same class of damage (A11-revised, G8).
        try:
            for _ in range(table_count):
                table, offset = _decode_table(raw, offset)
                if "nullable_columns_v1" in required_capabilities:
                    _require(raw, offset, _U16.size, "schema layout count")
                    layout_count = _U16.unpack_from(raw, offset)[0]
                    offset += _U16.size
                    if layout_count > 64:
                        raise GrafxCorruptionDetected("Too many prior schema layouts.", field="schema_layouts")
                    _require(raw, offset, layout_count * 4, "schema layouts")
                    layouts = tuple((_U16.unpack_from(raw, offset + i * 4)[0], _U16.unpack_from(raw, offset + i * 4 + 2)[0]) for i in range(layout_count))
                    offset += layout_count * 4
                    table = replace(table, schema_layouts=layouts)
                tables.append(table)
            for _ in range(space_count):
                space, offset = _decode_space(raw, offset)
                spaces.append(space)
            for _ in range(index_count):
                definition, offset = _decode_catalog_index(raw, offset)
                indexes.append(definition)
        except (GrafxConfigurationError, GrafxIndexError) as invalid:
            raise GrafxCorruptionDetected(
                f"A stored catalog describes something this build cannot serve: "
                f"{invalid.message}",
                field=str(invalid.details.get("field", "catalog")),
                value=invalid.details.get("value"),
            ) from invalid
        if format_version == CATALOG_FORMAT_VERSION:
            _require_canonical_order(tables, spaces, indexes)
        catalog._install_loaded(tables, spaces)
        if format_version == CATALOG_FORMAT_VERSION:
            if any(is_fulltext(d.key_derivation) for d in indexes) and FULLTEXT_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected("Full-text indexes lack their required capability.", field="required_capabilities")
            if any(is_fulltext(d.key_derivation) and catalog.table_by_id(d.table_id).kind == "rel" for d in indexes) and "fulltext_relationships_v1" not in required_capabilities:
                raise GrafxCorruptionDetected("Relationship FTS lacks its capability.", field="required_capabilities")
            if any(d.key_derivation.startswith("fulltext_v4_") for d in indexes) and "fulltext_prefixes_v1" not in required_capabilities:
                raise GrafxCorruptionDetected("Prefix postings lack their capability.", field="required_capabilities")
            if any(d.key_derivation.startswith("fulltext_v5_") for d in indexes) and "fulltext_positions_v1" not in required_capabilities:
                raise GrafxCorruptionDetected("Positional postings lack their capability.", field="required_capabilities")
            if any(has_durable_statistics(d.key_derivation) for d in indexes) and FULLTEXT_STATISTICS_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected("Durable text statistics lack their capability.", field="required_capabilities")
            if any(has_historical_statistics(d.key_derivation) for d in indexes) and FULLTEXT_HISTORY_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected("Historical text statistics lack their capability.", field="required_capabilities")
            if _large_hash_generations(indexes) and LARGE_HASH_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected("Large hash directories lack their capability.", field="required_capabilities")
            if any(d.layout is IndexLayout.SPARSE_HASH for d in indexes) and SPARSE_HASH_CAPABILITY not in required_capabilities:
                raise GrafxCorruptionDetected("Sparse hash lacks its capability.", field="required_capabilities")
            if any(d.layout is IndexLayout.POSTING_HASH for d in indexes) and "posting_hash_v1" not in required_capabilities:
                raise GrafxCorruptionDetected("Posting hash lacks its capability.", field="required_capabilities")
            if any(
                definition.layout is IndexLayout.ORDERED for definition in indexes
            ) and (
                ORDERED_SECONDARY_INDEXES_V1_CAPABILITY
                not in required_capabilities
            ):
                raise GrafxCorruptionDetected(
                    "The catalog carries an ordered index without its required capability.",
                    field="required_capabilities",
                    value=ORDERED_SECONDARY_INDEXES_V1_CAPABILITY,
                )
            validated = catalog._validated_index_authority(
                indexes,
                stored=True,
                require_endpoint_identity=True,
            )
            catalog._format_version = format_version
            catalog._required_capabilities = required_capabilities
            catalog._install_indexes(validated)
        if offset != body_end:
            raise GrafxCorruptionDetected(
                f"A serialised catalog decoded {offset} of {body_end} body bytes.",
                field="catalog",
                consumed=offset,
                length=body_end,
            )
        if next_table < catalog.next_table_id() or next_space < catalog.next_space_id():
            raise GrafxCorruptionDetected(
                "A serialised catalog declares an identifier counter below the identifiers it "
                "already holds.",
                field="next_table_id",
                next_table_id=next_table,
                next_space_id=next_space,
            )
        if any(key not in catalog._tables_by_id for key in catalog._system_history):
            raise GrafxCorruptionDetected("History references an unknown table.", field="system_history")
        return catalog

    # --- protocol --------------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Catalog):
            return NotImplemented
        return (
            self._tables == other._tables
            and self._spaces == other._spaces
            and self._format_version == other._format_version
            and self._required_capabilities == other._required_capabilities
            and self._commit_catalog_activation == other._commit_catalog_activation
            and self._system_history == other._system_history
            and self._system_history_revision == other._system_history_revision
            and self._system_history_pins == other._system_history_pins
            and self._indexes_by_key == other._indexes_by_key
        )

    def __repr__(self) -> str:
        return f"Catalog(tables={len(self._tables)}, spaces={len(self._spaces)})"

    # --- internals -------------------------------------------------------------------------

    def _require_index_catalog(self) -> None:
        """Refuse an index-authority mutation before explicit v2 activation."""

        if self._format_version != CATALOG_FORMAT_VERSION or (
            IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY not in self._required_capabilities
        ):
            raise GrafxConfigurationError(
                "Persistent index definitions require explicit catalog-v2 activation.",
                field="format_version",
                value=self._format_version,
            )

    def _install_indexes(self, definitions: dict[str, CatalogIndexDefinition]) -> None:
        """Adopt one already-validated logical index authority."""

        by_key = dict(definitions)
        by_name = {definition.name: definition for definition in definitions.values()}
        mutable_by_table: dict[tuple[int, str], list[CatalogIndexDefinition]] = {}
        for key in sorted(definitions):
            definition = definitions[key]
            identity = (definition.table_id, definition.table_name)
            mutable_by_table.setdefault(identity, []).append(definition)
        by_table = {
            identity: tuple(table_definitions)
            for identity, table_definitions in mutable_by_table.items()
        }

        # Build all three projections before publishing any of them.  Catalog mutations are
        # serialized by their owner, and readers never observe a partly constructed table map.
        self._indexes_by_key = by_key
        self._indexes = by_name
        self._index_definitions_by_table = by_table
        self._invalidate_derived()

    def _validated_index_authority(
        self,
        definitions: Iterable[CatalogIndexDefinition],
        *,
        stored: bool,
        require_endpoint_identity: bool = False,
        require_active_identity: bool = False,
    ) -> dict[str, CatalogIndexDefinition]:
        """Validate cross-record, namespace and global-generation invariants."""

        error_type = GrafxCorruptionDetected if stored else GrafxConfigurationError

        def refuse(message: str, *, field: str, **details: object) -> NoReturn:
            """Report invalid persisted state through the owning error taxonomy."""
            raise error_type(message, field=field, **details)

        automatic: dict[str, list[object]] = {}
        for table in self.tables():
            for candidate in automatic_index_definitions(table):
                automatic.setdefault(candidate.registry_key, []).append(candidate)

        endpoints: set[str] = set()
        for relation in self.tables():
            if relation.kind != "rel":
                continue
            for endpoint_name in (relation.from_table, relation.to_table):
                endpoint = self._tables.get(str(endpoint_name))
                if endpoint is None or endpoint.kind != "node":
                    refuse(
                        f"Relationship {relation.name!r} points at missing or non-node "
                        f"endpoint {endpoint_name!r}.",
                        field="endpoint",
                        value=endpoint_name,
                        table=relation.name,
                    )
                endpoints.add(endpoint.name)

        by_key: dict[str, CatalogIndexDefinition] = {}
        nonces: dict[int, str] = {}
        for definition in definitions:
            if not isinstance(definition, CatalogIndexDefinition):
                refuse(
                    "Catalog index authority contains a value of the wrong type.",
                    field="definition",
                    value=type(definition).__name__,
                )
            if definition.registry_key in by_key:
                refuse(
                    f"Catalog index name {definition.name!r} is duplicated without regard "
                    "to case.",
                    field="name",
                    value=definition.name,
                )
            table = self._tables_by_id.get(definition.table_id)
            if table is None or table.name != definition.table_name:
                refuse(
                    f"Index {definition.name!r} does not name one committed table identity.",
                    field="table_id",
                    value=definition.table_id,
                    table_name=definition.table_name,
                )
            stored_arity = len(table.columns) + (2 if table.kind == "rel" else 0)
            for position in definition.positions:
                if position >= stored_arity:
                    refuse(
                        f"Index {definition.name!r} position {position} is outside table "
                        f"{table.name!r}, whose stored arity is {stored_arity}.",
                        field="positions",
                        value=position,
                        index=definition.name,
                    )
            if definition.layout is IndexLayout.ORDERED:
                if table.kind != "node":
                    refuse(
                        f"Ordered index {definition.name!r} belongs to a node table.",
                        field="table_id",
                        value=definition.table_id,
                        index=definition.name,
                    )
                first, second = (
                    table.columns[position] for position in definition.positions
                )
                if first.type is not ValueType.TIMESTAMP or second.type is not ValueType.STRING:
                    refuse(
                        f"Ordered index {definition.name!r} requires TIMESTAMP then STRING; "
                        f"got {first.type.name} then {second.type.name}.",
                        field="positions",
                        value=definition.positions,
                        index=definition.name,
                    )

            is_identity = definition.key_derivation == RECORD_ID_KEY_DERIVATION
            text_offset = 2 if table.kind == "rel" else 0
            if is_fulltext(definition.key_derivation) and (
                any(p < text_offset or p >= len(table.columns) or table.columns[p].type is not ValueType.STRING for p in definition.positions)
            ):
                refuse("Full-text indexes require declared STRING properties, not endpoints.", field="positions", index=definition.name)
            if is_identity:
                if table.kind != "node" or table.name not in endpoints:
                    refuse(
                        f"Identity index {definition.name!r} belongs only to a node used as "
                        "a relationship endpoint.",
                        field="table_id",
                        value=definition.table_id,
                        index=definition.name,
                    )
            elif definition.automatic:
                candidates = automatic.get(definition.registry_key, ())
                if not any(
                    _matches_automatic_exact(definition, candidate)
                    for candidate in candidates
                ):
                    refuse(
                        f"Automatic index {definition.name!r} does not match the table's "
                        "schema-derived exact definition.",
                        field="definition",
                        index=definition.name,
                    )
            elif definition.registry_key in automatic:
                refuse(
                    f"Custom index {definition.name!r} collides with a schema-derived "
                    "automatic index name.",
                    field="name",
                    value=definition.name,
                )

            for generation in definition.generations:
                owner = nonces.get(generation.artifact_nonce)
                if owner is not None:
                    refuse(
                        f"Physical generation nonce {generation.artifact_nonce} is owned by "
                        f"both {owner!r} and {definition.name!r}.",
                        field="artifact_nonce",
                        value=generation.artifact_nonce,
                    )
                nonces[generation.artifact_nonce] = definition.name
            by_key[definition.registry_key] = definition

        if require_endpoint_identity or require_active_identity:
            for endpoint_name in sorted(endpoints):
                table = self._tables[endpoint_name]
                key = identity_index_name(table.table_id)
                identity = by_key.get(key)
                if identity is None:
                    refuse(
                        f"Endpoint table {endpoint_name!r} has no durable identity index "
                        "definition.",
                        field="identity_index",
                        table=endpoint_name,
                    )
                if require_active_identity and identity.active_generation() is None:
                    refuse(
                        f"Endpoint table {endpoint_name!r} has no active identity-index "
                        "generation.",
                        field="generations",
                        table=endpoint_name,
                        index=identity.name,
                    )
        return by_key

    def _install_table(self, table: TableDef) -> None:
        self._tables[table.name] = table
        self._tables_by_id[table.table_id] = table
        self._invalidate_derived()

    def _install_space(self, space: EmbeddingSpaceDef) -> None:
        self._spaces[space.name] = space
        self._spaces_by_id[space.space_id] = space
        self._invalidate_derived()

    def _invalidate_derived(self) -> None:
        """Drop values derived from the current catalog authority after one mutation."""

        self._active_index_definitions_memo = None
        self._serialized_memo = None

    def _install_loaded(
        self, tables: list[TableDef], spaces: list[EmbeddingSpaceDef]
    ) -> None:
        """Install a decoded catalog, holding it to the invariants the write path enforces.

        The checksum only proves the bytes are the ones that were written; it says nothing about
        whether they were written by a build that agreed with this one. A stored catalog with two
        tables of the same name, or with a vector column pointing at a space that is not there,
        describes a database this engine cannot serve, and accepting it would push the failure
        into the first query instead of the load.

        The rules are the invariants, not the creation rules: a space is allowed to arrive
        retired, and a table is allowed to point at a retired space, because retiring a space
        after its columns were created is the sanctioned path (SPEC-VEC FR-3).
        """
        for space in spaces:
            if space.name in self._spaces:
                raise GrafxCorruptionDetected(
                    f"The stored catalog declares the embedding space {space.name!r} more than "
                    f"once.",
                    field="name",
                    value=space.name,
                )
            if space.space_id in self._spaces_by_id:
                raise GrafxCorruptionDetected(
                    f"The stored catalog gives the id {space.space_id} to both "
                    f"{self._spaces_by_id[space.space_id].name!r} and {space.name!r}.",
                    field="space_id",
                    value=space.space_id,
                )
            self._install_space(space)
        for table in tables:
            if table.name in self._tables:
                raise GrafxCorruptionDetected(
                    f"The stored catalog declares the table {table.name!r} more than once.",
                    field="name",
                    value=table.name,
                )
            if table.table_id in self._tables_by_id:
                raise GrafxCorruptionDetected(
                    f"The stored catalog gives the id {table.table_id} to both "
                    f"{self._tables_by_id[table.table_id].name!r} and {table.name!r}.",
                    field="table_id",
                    value=table.table_id,
                )
            for column in table.columns:
                if not column.is_vector:
                    continue
                space_name = str(column.vector_space)
                stored = self._spaces.get(space_name)
                if stored is None:
                    raise GrafxCorruptionDetected(
                        f"Column {column.name!r} of the stored table {table.name!r} points at "
                        f"the embedding space {space_name!r}, which the catalog does not hold.",
                        field="vector_space",
                        value=space_name,
                        table=table.name,
                    )
                if stored.value_type is not column.type:
                    raise GrafxCorruptionDetected(
                        f"Column {column.name!r} of the stored table {table.name!r} is a "
                        f"{column.type.name} column, but the space {space_name!r} stores "
                        f"{stored.value_type.name}.",
                        field="storage_dtype",
                        value=stored.storage_dtype,
                        table=table.name,
                    )
            self._install_table(table)


def _logical_index_identity(definition: CatalogIndexDefinition) -> tuple[object, ...]:
    """Return fields a physical generation or sizing hint may never redefine."""

    return (
        definition.name,
        definition.table_id,
        definition.table_name,
        definition.positions,
        definition.visibility,
        definition.key_derivation,
        definition.layout,
        definition.automatic,
    )


def _matches_automatic_exact(
    definition: CatalogIndexDefinition, candidate: object
) -> bool:
    """Match logical schema provenance while deliberately ignoring physical generation fields."""

    return (
        isinstance(candidate, IndexDefinition)
        and candidate.visibility is IndexVisibility.EXACT
        and candidate.key_derivation == COLUMN_KEY_DERIVATION
        and definition.name == candidate.name
        and definition.table_id == candidate.table_id
        and definition.table_name == candidate.table_name
        and definition.positions == candidate.positions
        and definition.visibility is candidate.visibility
        and definition.key_derivation == candidate.key_derivation
        and definition.layout is candidate.layout
    )


def _encode_capabilities(capabilities: frozenset[str]) -> int:
    """Encode every required capability, refusing one this build cannot uphold."""

    for dependent, required in (
        ("fulltext_positions_v1", {FULLTEXT_CAPABILITY}),
        ("fulltext_relationships_v1", {FULLTEXT_CAPABILITY}),
        ("fulltext_prefixes_v1", {FULLTEXT_CAPABILITY}),
        (FULLTEXT_HISTORY_CAPABILITY, {FULLTEXT_CAPABILITY, FULLTEXT_STATISTICS_CAPABILITY}),
        (HEAP_FREE_INDEX_CAPABILITY, {HEAP_RECLAIM_V1_CAPABILITY}),
    ):
        if dependent in capabilities and not required <= capabilities:
            raise GrafxConfigurationError("Required capability dependencies are missing.", field="required_capabilities")
    bits = 0
    for capability in capabilities:
        bit = _CAPABILITY_TO_BIT.get(capability)
        if bit is None:
            raise GrafxSchemaVersionMismatch(
                f"This build cannot write required catalog capability {capability!r}.",
                field="required_capabilities",
                value=capability,
            )
        bits |= bit
    return bits


def _decode_capabilities(bits: int) -> frozenset[str]:
    """Decode a required-capability bitset, never ignoring an unknown required bit."""

    unknown = bits & ~_KNOWN_CAPABILITY_BITS
    if unknown:
        raise GrafxSchemaVersionMismatch(
            f"The catalog requires unsupported capability bits 0x{unknown:016x}.",
            field="required_capabilities",
            value=bits,
            unsupported=unknown,
        )
    capabilities = frozenset(
        capability for capability, bit in _CAPABILITY_TO_BIT.items() if bits & bit
    )
    for dependent, required in (
        ("fulltext_positions_v1", {FULLTEXT_CAPABILITY}),
        ("fulltext_relationships_v1", {FULLTEXT_CAPABILITY}),
        ("fulltext_prefixes_v1", {FULLTEXT_CAPABILITY}),
        (FULLTEXT_HISTORY_CAPABILITY, {FULLTEXT_CAPABILITY, FULLTEXT_STATISTICS_CAPABILITY}),
        (HEAP_FREE_INDEX_CAPABILITY, {HEAP_RECLAIM_V1_CAPABILITY}),
    ):
        if dependent in capabilities and not required <= capabilities:
            raise GrafxCorruptionDetected("Required capability dependencies are missing.", field="required_capabilities")
    return capabilities


def _encode_catalog_index(definition: CatalogIndexDefinition) -> bytes:
    """Return one deterministic catalog-v2 logical definition and its generations."""

    if len(definition.positions) > 0xFFFF:
        raise GrafxConfigurationError(
            "A catalog index may contain at most 65535 positions.",
            field="positions",
            value=len(definition.positions),
            index=definition.name,
        )
    if len(definition.generations) > 0xFFFF:
        raise GrafxConfigurationError(
            "A catalog index may retain at most 65535 physical generations.",
            field="generations",
            value=len(definition.generations),
            index=definition.name,
        )
    try:
        visibility_tag = _VISIBILITY_TO_TAG[definition.visibility]
        derivation_tag = 4 if is_fulltext(definition.key_derivation) else _DERIVATION_TO_TAG[definition.key_derivation]
        layout_tag = _LAYOUT_TO_TAG[definition.layout]
    except KeyError as failure:
        raise GrafxConfigurationError(
            f"Index {definition.name!r} uses a contract catalog format 2 cannot encode.",
            field="definition",
            index=definition.name,
        ) from failure
    parts = [
        _encode_text(definition.name),
        _U32.pack(definition.table_id),
        _encode_text(definition.table_name),
        _INDEX_META.pack(
            visibility_tag,
            derivation_tag,
            1 if definition.automatic else 0,
            layout_tag,
            len(definition.positions),
            len(definition.generations),
            definition.expected_cardinality or 0,
        ),
    ]
    if derivation_tag == 4:
        parts.append(_encode_text(definition.key_derivation))
    parts.extend(_U32.pack(position) for position in definition.positions)
    parts.extend(
        _INDEX_GENERATION.pack(
            generation.artifact_nonce,
            generation.bucket_count,
            _STATE_TO_TAG[generation.state],
        )
        for generation in definition.generations
    )
    return b"".join(parts)


def _decode_catalog_index(
    raw: bytes, offset: int
) -> tuple[CatalogIndexDefinition, int]:
    """Decode one catalog-v2 logical index without normalizing stored order or tags."""

    name, offset = _decode_text(raw, offset)
    _require(raw, offset, _U32.size, "index table_id")
    table_id = _U32.unpack_from(raw, offset)[0]
    offset += _U32.size
    table_name, offset = _decode_text(raw, offset)
    _require(raw, offset, _INDEX_META.size, "index metadata")
    (
        visibility_tag,
        derivation_tag,
        automatic,
        layout_tag,
        position_count,
        generation_count,
        expected_cardinality,
    ) = _INDEX_META.unpack_from(raw, offset)
    offset += _INDEX_META.size
    layout = _TAG_TO_LAYOUT.get(layout_tag)
    if layout is None:
        raise GrafxCorruptionDetected(
            f"Index {name!r} declares unknown layout tag {layout_tag}.",
            field="layout",
            value=layout_tag,
            index=name,
        )
    visibility = _TAG_TO_VISIBILITY.get(visibility_tag)
    if visibility is None:
        raise GrafxCorruptionDetected(
            f"Index {name!r} declares unknown visibility tag {visibility_tag}.",
            field="visibility",
            value=visibility_tag,
            index=name,
        )
    key_derivation = _TAG_TO_DERIVATION.get(derivation_tag)
    if key_derivation is None and derivation_tag != 4:
        raise GrafxCorruptionDetected(
            f"Index {name!r} declares unknown key-derivation tag {derivation_tag}.",
            field="key_derivation",
            value=derivation_tag,
            index=name,
        )
    if automatic not in (0, 1):
        raise GrafxCorruptionDetected(
            f"Index {name!r} automatic flag must be zero or one; got {automatic}.",
            field="automatic",
            value=automatic,
            index=name,
        )
    if derivation_tag == 4:
        key_derivation, offset = _decode_text(raw, offset)
        if not is_fulltext(key_derivation):
            raise GrafxCorruptionDetected("Invalid full-text derivation family.", field="key_derivation")
    positions: list[int] = []
    for _ in range(position_count):
        _require(raw, offset, _U32.size, "index position")
        positions.append(_U32.unpack_from(raw, offset)[0])
        offset += _U32.size
    generations: list[IndexGenerationDescriptor] = []
    for _ in range(generation_count):
        _require(raw, offset, _INDEX_GENERATION.size, "index generation")
        artifact_nonce, bucket_count, state_tag = _INDEX_GENERATION.unpack_from(
            raw, offset
        )
        if bytes(raw[offset + 13 : offset + 16]) != b"\x00\x00\x00":
            raise GrafxCorruptionDetected(
                f"Index {name!r} generation {artifact_nonce} has non-zero reserved bytes.",
                field="reserved",
                index=name,
                artifact_nonce=artifact_nonce,
            )
        offset += _INDEX_GENERATION.size
        state = _TAG_TO_STATE.get(state_tag)
        if state is None:
            raise GrafxCorruptionDetected(
                f"Index {name!r} generation {artifact_nonce} declares unknown state tag "
                f"{state_tag}.",
                field="state",
                value=state_tag,
                index=name,
                artifact_nonce=artifact_nonce,
            )
        generations.append(
            IndexGenerationDescriptor(
                artifact_nonce=artifact_nonce,
                bucket_count=bucket_count,
                state=state,
            )
        )
    return (
        CatalogIndexDefinition(
            name=name,
            table_id=table_id,
            table_name=table_name,
            positions=tuple(positions),
            visibility=visibility,
            key_derivation=key_derivation,
            layout=layout,
            automatic=automatic == 1,
            expected_cardinality=expected_cardinality or None,
            generations=tuple(generations),
        ),
        offset,
    )


def _require_canonical_order(
    tables: list[TableDef],
    spaces: list[EmbeddingSpaceDef],
    indexes: list[CatalogIndexDefinition],
) -> None:
    """Refuse a v2 writer that did not emit the one canonical record order."""

    table_ids = [table.table_id for table in tables]
    space_ids = [space.space_id for space in spaces]
    index_keys = [definition.registry_key for definition in indexes]
    if table_ids != sorted(table_ids):
        raise GrafxCorruptionDetected(
            "Catalog-v2 tables are not ordered by table_id.",
            field="table_order",
            value=table_ids,
        )
    if space_ids != sorted(space_ids):
        raise GrafxCorruptionDetected(
            "Catalog-v2 spaces are not ordered by space_id.",
            field="space_order",
            value=space_ids,
        )
    if index_keys != sorted(index_keys):
        raise GrafxCorruptionDetected(
            "Catalog-v2 indexes are not ordered by case-folded logical name.",
            field="index_order",
            value=index_keys,
        )


def _encode_text(text: str) -> bytes:
    """Return a length-prefixed UTF-8 string as the catalog stores it.

    Schema names are validated identifiers and are therefore always encodable, but the guard
    stays: a string with no UTF-8 encoding must leave as a Grafx error and never as a raw
    UnicodeEncodeError (CONTRACT.md section 11 item 5).
    """
    try:
        body = text.encode("utf-8")
    except UnicodeEncodeError as failure:
        raise GrafxConfigurationError(
            f"A catalog string must be encodable as UTF-8; the character at position "
            f"{failure.start} cannot be: {failure.reason}.",
            field="text",
            position=failure.start,
        ) from failure
    if len(body) > _MAX_TEXT:
        raise GrafxConfigurationError(
            f"A catalog string may be at most {_MAX_TEXT} bytes; got {len(body)}.",
            field="text",
            value=len(body),
        )
    return _U16.pack(len(body)) + body


def _decode_text(raw: bytes, offset: int) -> tuple[str, int]:
    """Return the length-prefixed string at the offset and the offset that follows it."""
    _require(raw, offset, _U16.size, "text length")
    length = _U16.unpack_from(raw, offset)[0]
    offset += _U16.size
    _require(raw, offset, length, "text")
    try:
        text = bytes(raw[offset : offset + length]).decode("utf-8")
    except UnicodeDecodeError as failure:
        raise GrafxCorruptionDetected(
            "A catalog string is not valid UTF-8.",
            field="text",
            offset=offset,
            length=length,
        ) from failure
    return text, offset + length


def _encode_optional_text(text: str | None) -> bytes:
    """Return an optional string as a presence byte followed, when present, by the string."""
    if text is None:
        return _U8.pack(0)
    return _U8.pack(1) + _encode_text(text)


def _decode_optional_text(raw: bytes, offset: int) -> tuple[str | None, int]:
    """Return the optional string at the offset and the offset that follows it."""
    _require(raw, offset, _U8.size, "presence")
    present = _U8.unpack_from(raw, offset)[0]
    offset += _U8.size
    if present == 0:
        return None, offset
    if present != 1:
        raise GrafxCorruptionDetected(
            f"A catalog presence byte must be 0 or 1; got {present}.",
            field="presence",
            value=present,
            offset=offset - 1,
        )
    return _decode_text(raw, offset)


def _encode_table(table: TableDef) -> bytes:
    """Return the stored form of one table definition."""
    parts = [
        _U32.pack(table.table_id),
        _encode_text(table.name),
        _encode_text(table.kind),
        _U16.pack(table.schema_version),
        _encode_optional_text(table.primary_key),
        _encode_optional_text(table.from_table),
        _encode_optional_text(table.to_table),
        _U16.pack(len(table.columns)),
    ]
    for column in table.columns:
        parts.append(_encode_text(column.name))
        parts.append(_U8.pack(int(column.type)))
        parts.append(_U8.pack(1 if column.nullable else 0))
        parts.append(_encode_optional_text(column.vector_space))
    return b"".join(parts)


def _decode_table(raw: bytes, offset: int) -> tuple[TableDef, int]:
    """Return the table definition at the offset and the offset that follows it."""
    _require(raw, offset, _U32.size, "table_id")
    table_id = _U32.unpack_from(raw, offset)[0]
    offset += _U32.size
    name, offset = _decode_text(raw, offset)
    kind, offset = _decode_text(raw, offset)
    _require(raw, offset, _U16.size, "schema_version")
    schema_version = _U16.unpack_from(raw, offset)[0]
    offset += _U16.size
    primary_key, offset = _decode_optional_text(raw, offset)
    from_table, offset = _decode_optional_text(raw, offset)
    to_table, offset = _decode_optional_text(raw, offset)
    _require(raw, offset, _U16.size, "column count")
    column_count = _U16.unpack_from(raw, offset)[0]
    offset += _U16.size
    columns: list[ColumnDef] = []
    for _ in range(column_count):
        column_name, offset = _decode_text(raw, offset)
        _require(raw, offset, _U8.size * 2, "column type")
        type_tag = _U8.unpack_from(raw, offset)[0]
        nullable = _U8.unpack_from(raw, offset + _U8.size)[0]
        offset += _U8.size * 2
        vector_space, offset = _decode_optional_text(raw, offset)
        try:
            column_type = ValueType(type_tag)
        except ValueError as failure:
            raise GrafxCorruptionDetected(
                f"Column {column_name!r} of table {name!r} declares the unknown type tag "
                f"{type_tag}.",
                field="type",
                value=type_tag,
            ) from failure
        if nullable > 1:
            # Its neighbour, the optional-string presence byte, refuses anything but 0 or 1;
            # this one silently read every other value as False.
            raise GrafxCorruptionDetected(
                f"Column {column_name!r} of table {name!r} declares the nullable byte "
                f"{nullable}, which is neither 0 nor 1.",
                field="nullable",
                value=nullable,
                table=name,
            )
        columns.append(
            ColumnDef(
                name=column_name,
                type=column_type,
                nullable=nullable == 1,
                vector_space=vector_space,
            )
        )
    table = TableDef(
        table_id=table_id,
        name=name,
        kind=kind,
        columns=tuple(columns),
        primary_key=primary_key,
        from_table=from_table,
        to_table=to_table,
        schema_version=schema_version,
    )
    return table, offset


def _encode_space(space: EmbeddingSpaceDef) -> bytes:
    """Return the stored form of one embedding space definition."""
    return b"".join(
        (
            _U32.pack(space.space_id),
            _encode_text(space.name),
            _U32.pack(space.dimension),
            _encode_text(space.metric.value),
            _U8.pack(1 if space.normalized else 0),
            _encode_text(space.storage_dtype),
            _encode_text(space.state),
            _F64.pack(space.created_at_wall),
        )
    )


def _decode_space(raw: bytes, offset: int) -> tuple[EmbeddingSpaceDef, int]:
    """Return the embedding space at the offset and the offset that follows it."""
    _require(raw, offset, _U32.size, "space_id")
    space_id = _U32.unpack_from(raw, offset)[0]
    offset += _U32.size
    name, offset = _decode_text(raw, offset)
    _require(raw, offset, _U32.size, "dimension")
    dimension = _U32.unpack_from(raw, offset)[0]
    offset += _U32.size
    metric_name, offset = _decode_text(raw, offset)
    _require(raw, offset, _U8.size, "normalized")
    normalized = _U8.unpack_from(raw, offset)[0]
    offset += _U8.size
    storage_dtype, offset = _decode_text(raw, offset)
    state, offset = _decode_text(raw, offset)
    _require(raw, offset, _F64.size, "created_at_wall")
    created_at_wall = _F64.unpack_from(raw, offset)[0]
    offset += _F64.size
    try:
        metric = DistanceMetric(metric_name)
    except ValueError as failure:
        raise GrafxCorruptionDetected(
            f"Embedding space {name!r} declares the unknown metric {metric_name!r}.",
            field="metric",
            value=metric_name,
        ) from failure
    space = EmbeddingSpaceDef(
        space_id=space_id,
        name=name,
        dimension=dimension,
        metric=metric,
        normalized=normalized == 1,
        storage_dtype=storage_dtype,
        state=state,
        created_at_wall=created_at_wall,
    )
    return space, offset


def _require(raw: bytes, offset: int, size: int, what: str) -> None:
    """Refuse to read past the end of the serialised catalog, naming what was being read."""
    if offset < 0 or offset + size > len(raw):
        raise GrafxCorruptionDetected(
            f"A serialised catalog needs {size} bytes for its {what} at offset {offset}, but "
            f"the buffer holds {len(raw)}.",
            field=what,
            offset=offset,
            needed=size,
            available=max(len(raw) - max(offset, 0), 0),
        )
