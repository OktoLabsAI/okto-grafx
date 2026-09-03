"""The in-memory catalog and the bytes it is persisted as (CONTRACT.md section 7.2).

The catalog is the answer to "what tables and what embedding spaces exist". It is a value with
rules rather than a container: a table is added once and never mutated, a space is added once and
the only change it ever accepts is from active to retired (SPEC-VEC TR-2), and a vector column
may only point at a space that already exists and still accepts writes.

Its serialised form is self-describing and checksummed, because the catalog is what every other
file is interpreted through: losing it silently would turn every heap page into an unreadable
blob. The common layout is

    magic 8B "GRFXCTLG" | format_version u16 | reserved u16 |
    table_count u32 | space_count u32 | next_table_id u32 | next_space_id u32 |
    [v2: required_capabilities u64 | index_count u32 | reserved u32] |
    tables | spaces | [v2: indexes] | crc32c u32

with tables ordered by table_id and spaces ordered by space_id, so the same catalog always
serialises to the same bytes.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from typing import NoReturn

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
)
from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    automatic_index_definitions,
)
from okto_grafx.domain.index.visibility import IndexVisibility
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
    "Catalog",
]

CATALOG_MAGIC: bytes = b"GRFXCTLG"
"""The eight bytes that open a serialised catalog."""

CATALOG_LEGACY_FORMAT_VERSION: int = 1
"""The format kept by ordinary databases until P2-ID is explicitly activated."""

CATALOG_FORMAT_VERSION: int = 2
"""The newest catalog format this build can read and write."""

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
_KNOWN_CAPABILITY_BITS = _IDENTITY_SECONDARY_INDEXES_V1_BIT
_CAPABILITY_TO_BIT = {
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY: _IDENTITY_SECONDARY_INDEXES_V1_BIT
}
_VISIBILITY_TO_TAG = {IndexVisibility.EXACT: 1}
_TAG_TO_VISIBILITY = {value: key for key, value in _VISIBILITY_TO_TAG.items()}
_DERIVATION_TO_TAG = {COLUMN_KEY_DERIVATION: 1, RECORD_ID_KEY_DERIVATION: 2}
_TAG_TO_DERIVATION = {value: key for key, value in _DERIVATION_TO_TAG.items()}
_STATE_TO_TAG = {
    IndexGenerationState.BUILDING: 1,
    IndexGenerationState.ACTIVE: 2,
    IndexGenerationState.STALE: 3,
}
_TAG_TO_STATE = {value: key for key, value in _STATE_TO_TAG.items()}


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
        "_indexes",
        "_indexes_by_key",
        "_index_definitions_by_table",
    )

    def __init__(self) -> None:
        """Build an empty catalog."""
        self._tables: dict[str, TableDef] = {}
        self._tables_by_id: dict[int, TableDef] = {}
        self._spaces: dict[str, EmbeddingSpaceDef] = {}
        self._spaces_by_id: dict[int, EmbeddingSpaceDef] = {}
        self._format_version: int = CATALOG_LEGACY_FORMAT_VERSION
        self._required_capabilities: frozenset[str] = frozenset()
        self._indexes: dict[str, CatalogIndexDefinition] = {}
        self._indexes_by_key: dict[str, CatalogIndexDefinition] = {}
        self._index_definitions_by_table: dict[
            tuple[int, str], tuple[CatalogIndexDefinition, ...]
        ] = {}

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

        definitions = (
            definition
            for table in self.tables()
            for definition in self.active_index_definitions_for(
                table.table_id, table_name=table.name
            )
        )
        return tuple(
            sorted(
                definitions,
                key=lambda definition: definition.registry_key,
            )
        )

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
        self._required_capabilities = frozenset(
            (IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,)
        )
        self._install_indexes(validated)
        return self

    def add_index_definition(
        self, definition: CatalogIndexDefinition
    ) -> CatalogIndexDefinition:
        """Add one v2-managed logical index after validating the complete authority."""

        self._require_index_catalog()
        proposed = (*self.index_definitions(), definition)
        validated = self._validated_index_authority(proposed, stored=False)
        self._install_indexes(validated)
        return definition

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
        self._install_indexes(validated)
        return definition

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
            if not capability_bits & _IDENTITY_SECONDARY_INDEXES_V1_BIT:
                raise GrafxConfigurationError(
                    "Catalog format 2 requires identity_secondary_indexes_v1.",
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
        for table in self.tables():
            parts.append(_encode_table(table))
        for space in self.spaces():
            parts.append(_encode_space(space))
        for definition in indexes:
            parts.append(_encode_catalog_index(definition))
        body = b"".join(parts)
        return body + _CHECKSUM.pack(crc32c(body))

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
            raise error_type(message, field=field, **details)

        automatic: dict[str, object] = {}
        for table in self.tables():
            for candidate in automatic_index_definitions(table):
                automatic[candidate.registry_key] = candidate

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

            is_identity = definition.key_derivation == RECORD_ID_KEY_DERIVATION
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
                candidate = automatic.get(definition.registry_key)
                if candidate is None or not _matches_automatic_exact(
                    definition, candidate
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

    def _install_space(self, space: EmbeddingSpaceDef) -> None:
        self._spaces[space.name] = space
        self._spaces_by_id[space.space_id] = space

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
    )


def _encode_capabilities(capabilities: frozenset[str]) -> int:
    """Encode every required capability, refusing one this build cannot uphold."""

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
    return frozenset(
        capability for capability, bit in _CAPABILITY_TO_BIT.items() if bits & bit
    )


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
        derivation_tag = _DERIVATION_TO_TAG[definition.key_derivation]
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
            0,
            len(definition.positions),
            len(definition.generations),
            definition.expected_cardinality or 0,
        ),
    ]
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
        reserved,
        position_count,
        generation_count,
        expected_cardinality,
    ) = _INDEX_META.unpack_from(raw, offset)
    offset += _INDEX_META.size
    if reserved != 0:
        raise GrafxCorruptionDetected(
            f"Index {name!r} has a non-zero reserved metadata byte.",
            field="reserved",
            value=reserved,
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
    if key_derivation is None:
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
