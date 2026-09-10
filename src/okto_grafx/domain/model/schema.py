"""Tables, columns, embedding spaces and the positional tuple encoding (CONTRACT.md section 7.2).

A table schema is a frozen value that validates itself the moment it is built, so an impossible
table cannot exist long enough to be persisted: a vector column that names no embedding space, a
relationship table with no endpoints, an embedding space of dimension zero. The same rule keeps
the catalog honest without a second validation pass at load time.

A record payload is the positional encoding of the values of one row, in column order, with no
names and no padding. The schema_version that says how to read it back is not part of the
payload: it lives in the heap record header (CONTRACT.md section 6.4), which is what lets a
reader detect that the bytes were written under a different schema before it decodes them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from math import isfinite
from types import MappingProxyType

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordId
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import (
    MAX_VECTOR_DIMENSION,
    VECTOR_DTYPES,
    VECTOR_VALUE_TYPES,
    Value,
    ValueType,
    Timestamp,
    Uuid,
    VectorValue,
    _U32,
    _append_encoded_value,
    _decode_expected_value_body,
    _decode_vector_mode,
    _require,
    _validate_value,
    decode_value,
    value_type_of,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric

__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "TABLE_KINDS",
    "relationship_row",
    "relationship_columns",
    "endpoint_column_defs",
    "ENDPOINT_COLUMN_COUNT",
    "ENDPOINT_COLUMNS",
    "TARGET_COLUMN",
    "SOURCE_COLUMN",
    "SPACE_STATES",
    "SPACE_STATE_ACTIVE",
    "SPACE_STATE_RETIRED",
    "STORAGE_DTYPES",
    "ColumnDef",
    "TableDef",
    "EmbeddingSpaceDef",
    "is_identifier",
    "encode_tuple",
    "decode_tuple",
]

MAX_IDENTIFIER_LENGTH: int = 128
"""Characters a table, column or space name may have."""

TABLE_KINDS: tuple[str, ...] = ("node", "rel")

SOURCE_COLUMN: str = "_from"
"""The reserved column that carries the RecordId of the row an edge starts at."""

TARGET_COLUMN: str = "_to"
"""The reserved column that carries the RecordId of the row an edge ends at."""

ENDPOINT_COLUMNS: tuple[str, str] = (SOURCE_COLUMN, TARGET_COLUMN)
"""The two reserved columns of every relationship table, in their stored order.

They are RecordIds and not RecordRefs, and that is the whole point. Section 3 calls a RecordId
"a stable logical identity across versions", which is exactly what an endpoint needs: an edge
must still name the same node after that node has been updated, and an update writes a NEW
version at a new page and slot. A RecordRef names a page and a slot, and both move, so an edge
holding one would silently come to point at whatever later occupied that slot -- or at nothing.

They lead the stored tuple rather than trailing it so that their position never depends on how
many properties the user declared: source is always value 0 and target is always value 1, in
every relationship table of every schema version.
"""

ENDPOINT_COLUMN_COUNT: int = len(ENDPOINT_COLUMNS)
"""How many values of a relationship tuple belong to the layout rather than to the user."""

"""A table describes either the nodes of a label or the relationships of a type."""

SPACE_STATE_ACTIVE: str = "active"
SPACE_STATE_RETIRED: str = "retired"

SPACE_STATES: tuple[str, ...] = (SPACE_STATE_ACTIVE, SPACE_STATE_RETIRED)
"""The only two states of an embedding space, and the only transition is from the first."""

STORAGE_DTYPES: tuple[str, ...] = ("float32", "float64")
"""The precisions an embedding space may store its vectors in."""


def is_identifier(name: object) -> bool:
    """Return True when the name is a usable ASCII identifier for a schema object."""
    if type(name) is str:
        # On ASCII, Python's identifier predicate is exactly [A-Za-z_][A-Za-z0-9_]*.
        # Keep the existing subclass path and its hooks; no schema/authority cache.
        return len(name) <= MAX_IDENTIFIER_LENGTH and name.isascii() and name.isidentifier()
    if not isinstance(name, str) or not name:
        return False
    if len(name) > MAX_IDENTIFIER_LENGTH:
        return False
    if not name.isascii():
        return False
    first = name[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(character.isalnum() or character == "_" for character in name)


def _require_identifier(field: str, name: object) -> str:
    """Return the name after checking that it can be a schema identifier."""
    if not is_identifier(name):
        raise GrafxConfigurationError(
            f"Schema field {field!r} needs a non-empty ASCII identifier of at most "
            f"{MAX_IDENTIFIER_LENGTH} characters; got {name!r}.",
            field=field,
            value=repr(name),
        )
    return str(name)


@dataclass(frozen=True, slots=True)
class ColumnDef:
    """One column of a table: a name, a stored type, nullability and, for vectors, its space."""

    name: str
    type: ValueType
    nullable: bool = True
    vector_space: str | None = None

    def __post_init__(self) -> None:
        """Refuse a column that could not be stored or read back."""
        _require_identifier("name", self.name)
        if not isinstance(self.type, ValueType):
            raise GrafxConfigurationError(
                f"Column {self.name!r} needs a ValueType; got {self.type!r}.",
                field="type",
                value=repr(self.type),
            )
        if not isinstance(self.nullable, bool):
            raise GrafxConfigurationError(
                f"Column {self.name!r} needs a boolean nullable flag; got {self.nullable!r}.",
                field="nullable",
                value=repr(self.nullable),
            )
        if self.is_vector:
            if self.vector_space is None:
                raise GrafxConfigurationError(
                    f"Vector column {self.name!r} must name the embedding space it belongs to.",
                    field="vector_space",
                    value=None,
                )
            _require_identifier("vector_space", self.vector_space)
        elif self.vector_space is not None:
            raise GrafxConfigurationError(
                f"Column {self.name!r} is not a vector column, so it may not name the embedding "
                f"space {self.vector_space!r}.",
                field="vector_space",
                value=repr(self.vector_space),
            )

    @property
    def is_vector(self) -> bool:
        """Return True when this column stores an embedding."""
        return self.type in VECTOR_VALUE_TYPES


class _TableDefColumnCache:
    """Reserve non-domain slots for immutable, derived column plans."""

    __slots__ = ("_automatic_index_projection", "_column_positions", "_decode_plan")
    _automatic_index_projection: object | None
    _column_positions: Mapping[str, int]
    _decode_plan: tuple[tuple[int, ValueType, bool, ColumnDef], ...]


@dataclass(frozen=True, slots=True)
class TableDef(_TableDefColumnCache):
    """A node table or a relationship table, with its columns in their stored order."""

    table_id: int
    name: str
    kind: str
    columns: tuple[ColumnDef, ...]
    primary_key: str | None = None
    from_table: str | None = None
    to_table: str | None = None
    schema_version: int = 1
    schema_layouts: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        """Refuse a table whose shape contradicts the kind it declares.

        A relationship table has its two endpoint columns put in front of its properties here,
        rather than being refused for not having them. Refusing would be the stricter rule and
        the worse one: every caller that builds a relationship table from DDL would have to
        remember the layout, and the first one to forget would produce a table that stores edges
        with nowhere to say what they join. Normalising makes the layout impossible to get wrong
        and impossible to disagree about, which is what C5 and C10 need from it.

        It is idempotent, so a table read back out of the catalog -- which already carries the
        columns -- comes back unchanged.
        """
        object.__setattr__(self, "_automatic_index_projection", None)
        if self.kind == "rel" and isinstance(self.columns, tuple):
            object.__setattr__(self, "columns", relationship_columns(self.columns))
        if isinstance(self.table_id, bool) or not isinstance(self.table_id, int):
            raise GrafxConfigurationError(
                f"Table {self.name!r} needs an integer table_id; got {self.table_id!r}.",
                field="table_id",
                value=repr(self.table_id),
            )
        if not 1 <= self.table_id <= 0xFFFFFFFF:
            raise GrafxConfigurationError(
                f"Table {self.name!r} needs a table_id between 1 and 4294967295; got "
                f"{self.table_id}.",
                field="table_id",
                value=self.table_id,
            )
        _require_identifier("name", self.name)
        if self.kind not in TABLE_KINDS:
            raise GrafxConfigurationError(
                f"Table {self.name!r} must be one of {TABLE_KINDS}; got {self.kind!r}.",
                field="kind",
                value=repr(self.kind),
            )
        if not isinstance(self.columns, tuple) or not self.columns:
            raise GrafxConfigurationError(
                f"Table {self.name!r} needs at least one column, as a tuple.",
                field="columns",
                value=type(self.columns).__name__,
            )
        seen: set[str] = set()
        for column in self.columns:
            if not isinstance(column, ColumnDef):
                raise GrafxConfigurationError(
                    f"Table {self.name!r} needs every column to be a ColumnDef; got {column!r}.",
                    field="columns",
                    value=repr(column),
                )
            if column.name in seen:
                raise GrafxConfigurationError(
                    f"Table {self.name!r} declares the column {column.name!r} more than once.",
                    field="columns",
                    value=column.name,
                )
            seen.add(column.name)
        object.__setattr__(
            self,
            "_column_positions",
            MappingProxyType(
                {column.name: position for position, column in enumerate(self.columns)}
            ),
        )
        object.__setattr__(
            self,
            "_decode_plan",
            tuple(
                (int(column.type), column.type, column.nullable, column)
                for column in self.columns
            ),
        )
        reserved_positions = {
            position
            for position, column in enumerate(self.columns)
            if column.name in ENDPOINT_COLUMNS
        }
        if self.kind == "rel":
            if reserved_positions != set(range(ENDPOINT_COLUMN_COUNT)):
                # Normalisation put them at 0 and 1, so reaching here means the caller supplied
                # a column of a reserved NAME somewhere else -- a property called _to, three
                # columns in. Its values would be read as an endpoint by every consumer of this
                # layout, so it is refused rather than shadowed.
                raise GrafxConfigurationError(
                    f"Relationship table {self.name!r} uses a reserved endpoint column name "
                    f"outside positions 0 and 1; {ENDPOINT_COLUMNS} are the layout's own.",
                    field="columns",
                    value=sorted(reserved_positions),
                )
            for position, column in enumerate(self.columns[:ENDPOINT_COLUMN_COUNT]):
                expected = endpoint_column_defs()[position]
                if column != expected:
                    raise GrafxConfigurationError(
                        f"Relationship table {self.name!r} declares {column.name!r} as "
                        f"{column.type.name}"
                        f"{'' if column.nullable else ' not null'}, but the layout stores it as "
                        f"{expected.type.name} not null.",
                        field="columns",
                        value=column.name,
                    )
        elif reserved_positions:
            raise GrafxConfigurationError(
                f"Node table {self.name!r} uses {ENDPOINT_COLUMNS}, which are reserved for the "
                f"endpoints of a relationship table.",
                field="columns",
                value=sorted(reserved_positions),
            )
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise GrafxConfigurationError(
                f"Table {self.name!r} needs an integer schema_version; got "
                f"{self.schema_version!r}.",
                field="schema_version",
                value=repr(self.schema_version),
            )
        if not 1 <= self.schema_version <= 0xFFFF:
            raise GrafxConfigurationError(
                f"Table {self.name!r} needs a schema_version between 1 and 65535; got "
                f"{self.schema_version}.",
                field="schema_version",
                value=self.schema_version,
            )
        if type(self.schema_layouts) is not tuple or len(self.schema_layouts) > 64:
            raise GrafxConfigurationError("At most 64 prior append-only layouts are supported.", field="schema_layouts")
        for position, layout in enumerate(self.schema_layouts):
            if (type(layout) is not tuple or len(layout) != 2
                    or any(type(n) is not int for n in layout)):
                raise GrafxConfigurationError("Invalid prior schema layout.", field="schema_layouts")
            version, count = layout
            remaining = len(self.schema_layouts) - position
            if (version != self.schema_version - remaining or version < 1
                    or count != len(self.columns) - remaining or count < (2 if self.kind == "rel" else 1)):
                raise GrafxConfigurationError("Prior layouts must be contiguous one-column additions.", field="schema_layouts")
        if self.schema_layouts and any(not c.nullable for c in self.columns[self.schema_layouts[0][1]:]):
            raise GrafxConfigurationError("Appended columns must be nullable.", field="schema_layouts")
        if self.schema_layouts:
            base_count = self.schema_layouts[0][1]
            if (self.primary_key is not None and self.primary_key not in {c.name for c in self.columns[:base_count]}
                    or any(c.type in VECTOR_VALUE_TYPES for c in self.columns[base_count:])):
                raise GrafxConfigurationError("Prior layouts cannot imply a new PK or vector column.", field="schema_layouts")
        if self.primary_key is not None:
            _require_identifier("primary_key", self.primary_key)
            if self.primary_key not in seen:
                raise GrafxConfigurationError(
                    f"Table {self.name!r} names {self.primary_key!r} as its primary key, which "
                    f"is not one of its columns.",
                    field="primary_key",
                    value=self.primary_key,
                )
        if self.kind == "rel":
            if self.from_table is None or self.to_table is None:
                raise GrafxConfigurationError(
                    f"Relationship table {self.name!r} must declare both endpoints.",
                    field="from_table",
                    value=repr(self.from_table),
                )
            _require_identifier("from_table", self.from_table)
            _require_identifier("to_table", self.to_table)
            if self.primary_key is not None:
                raise GrafxConfigurationError(
                    f"Relationship table {self.name!r} may not declare a primary key; the "
                    f"endpoints are its identity.",
                    field="primary_key",
                    value=self.primary_key,
                )
        elif self.from_table is not None or self.to_table is not None:
            raise GrafxConfigurationError(
                f"Node table {self.name!r} may not declare endpoints.",
                field="from_table",
                value=repr(self.from_table),
            )

    @property
    def arity(self) -> int:
        """Return the number of columns a tuple of this table must carry.

        For a relationship table that is the two endpoints PLUS the properties, so a caller
        staging an edge passes relationship_row(source, target, properties) and the arity check
        in encode_tuple catches anyone who forgot an end.
        """
        return len(self.columns)

    @property
    def endpoint_columns(self) -> tuple[ColumnDef, ...]:
        """Return the reserved endpoint columns, which only a relationship table has."""
        if self.kind != "rel":
            return ()
        return tuple(self.columns[:ENDPOINT_COLUMN_COUNT])

    @property
    def property_columns(self) -> tuple[ColumnDef, ...]:
        """Return the columns the user declared, without the layout's own."""
        if self.kind != "rel":
            return tuple(self.columns)
        return tuple(self.columns[ENDPOINT_COLUMN_COUNT:])

    def source_of(self, values: Sequence[Value]) -> RecordId:
        """Return the row identity an edge tuple starts at."""
        return self._endpoint_of(values, 0, SOURCE_COLUMN)

    def target_of(self, values: Sequence[Value]) -> RecordId:
        """Return the row identity an edge tuple ends at."""
        return self._endpoint_of(values, 1, TARGET_COLUMN)

    def _endpoint_of(self, values: Sequence[Value], position: int, name: str) -> RecordId:
        """Return one endpoint of an edge tuple, refusing a tuple that cannot carry one."""
        if self.kind != "rel":
            raise GrafxConfigurationError(
                f"Table {self.name!r} is a {self.kind} table and has no endpoints.",
                field="kind",
                value=self.kind,
            )
        if len(values) != self.arity:
            raise SchemaMismatchError(
                f"Table {self.name!r} has {self.arity} columns, but the tuple carries "
                f"{len(values)} values.",
                table=self.name,
                table_id=self.table_id,
                expected_arity=self.arity,
                observed_arity=len(values),
            )
        endpoint = values[position]
        if isinstance(endpoint, bool) or not isinstance(endpoint, int):
            raise SchemaMismatchError(
                f"The {name!r} endpoint of an edge in {self.name!r} must be a row identity; got "
                f"{type(endpoint).__name__}.",
                table=self.name,
                table_id=self.table_id,
                column=name,
                value=repr(endpoint),
            )
        return endpoint

    def column(self, name: str) -> ColumnDef:
        """Return the column with that name."""
        position = self.column_positions.get(name)
        if position is not None:
            return self.columns[position]
        raise GrafxConfigurationError(
            f"Table {self.name!r} has no column named {name!r}.",
            field="column",
            value=name,
        )

    @property
    def column_positions(self) -> Mapping[str, int]:
        """Return the immutable, precomputed position of each stored column."""
        return self._column_positions

    def column_index(self, name: str) -> int:
        """Return the position of the column with that name in the stored tuple."""
        position = self.column_positions.get(name)
        if position is not None:
            return position
        raise GrafxConfigurationError(
            f"Table {self.name!r} has no column named {name!r}.",
            field="column",
            value=name,
        )


@dataclass(frozen=True, slots=True)
class EmbeddingSpaceDef:
    """One embedding space: the identity a vector column points at (SPEC-VEC TR-2).

    Everything here except the state is immutable for the life of the space. The only sanctioned
    change is from active to retired, which the catalog performs through retire_space and which
    this class exposes as a value transformation.
    """

    space_id: int
    name: str
    dimension: int
    metric: DistanceMetric
    normalized: bool
    storage_dtype: str = "float32"
    state: str = SPACE_STATE_ACTIVE
    created_at_wall: float = 0.0

    def __post_init__(self) -> None:
        """Refuse a space that could not be searched or stored."""
        if isinstance(self.space_id, bool) or not isinstance(self.space_id, int):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs an integer space_id; got "
                f"{self.space_id!r}.",
                field="space_id",
                value=repr(self.space_id),
            )
        if not 1 <= self.space_id <= 0xFFFFFFFF:
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a space_id between 1 and 4294967295, "
                f"because 0 marks a vector with no space; got {self.space_id}.",
                field="space_id",
                value=self.space_id,
            )
        _require_identifier("name", self.name)
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs an integer dimension; got "
                f"{self.dimension!r}.",
                field="dimension",
                value=repr(self.dimension),
            )
        if not 1 <= self.dimension <= MAX_VECTOR_DIMENSION:
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a dimension between 1 and "
                f"{MAX_VECTOR_DIMENSION}; got {self.dimension}.",
                field="dimension",
                value=self.dimension,
            )
        if not isinstance(self.metric, DistanceMetric):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a DistanceMetric; got {self.metric!r}.",
                field="metric",
                value=repr(self.metric),
            )
        if not isinstance(self.normalized, bool):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a boolean normalized flag; got "
                f"{self.normalized!r}.",
                field="normalized",
                value=repr(self.normalized),
            )
        if self.storage_dtype not in STORAGE_DTYPES:
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} must store {STORAGE_DTYPES}; got "
                f"{self.storage_dtype!r}.",
                field="storage_dtype",
                value=repr(self.storage_dtype),
            )
        if self.state not in SPACE_STATES:
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} must be in one of {SPACE_STATES}; got "
                f"{self.state!r}.",
                field="state",
                value=repr(self.state),
            )
        if isinstance(self.created_at_wall, bool) or not isinstance(
            self.created_at_wall, (int, float)
        ):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a numeric creation reading; got "
                f"{self.created_at_wall!r}.",
                field="created_at_wall",
                value=repr(self.created_at_wall),
            )
        if not isfinite(float(self.created_at_wall)):
            raise GrafxConfigurationError(
                f"Embedding space {self.name!r} needs a finite creation reading; got "
                f"{self.created_at_wall!r}.",
                field="created_at_wall",
                value=repr(self.created_at_wall),
            )
        object.__setattr__(self, "created_at_wall", float(self.created_at_wall))

    @property
    def is_active(self) -> bool:
        """Return True while the space still accepts writes."""
        return self.state == SPACE_STATE_ACTIVE

    @property
    def value_type(self) -> ValueType:
        """Return the value type vectors of this space encode to."""
        return VECTOR_DTYPES[self.storage_dtype]

    def retired(self) -> EmbeddingSpaceDef:
        """Return the same space in the retired state, which is the only change it allows."""
        return replace(self, state=SPACE_STATE_RETIRED)


def _reject(table: TableDef, column: ColumnDef, position: int, detail: str) -> SchemaMismatchError:
    """Build the mismatch error for one column, naming the table, the column and the reason."""
    return SchemaMismatchError(
        f"Table {table.name!r} column {column.name!r} at position {position}: {detail}",
        table=table.name,
        table_id=table.table_id,
        column=column.name,
        position=position,
        declared_type=column.type.name,
    )


def _check_column_value(
    table: TableDef, position: int, column: ColumnDef, value: Value
) -> ValueType:
    """Return the checked value type, refusing a value the column does not declare."""
    if value is None:
        if column.nullable:
            return ValueType.NULL
        raise _reject(table, column, position, "a null is not allowed in this column.")
    observed = value_type_of(value)
    if observed is not column.type:
        raise _reject(
            table,
            column,
            position,
            f"a {observed.name} value cannot be stored in a {column.type.name} column.",
        )
    return observed


def endpoint_column_defs() -> tuple[ColumnDef, ColumnDef]:
    """Return the two reserved endpoint columns, exactly as a relationship table stores them.

    INT64 because section 7.1 fixes the value taxonomy and INT64 is the only 64-bit integer in
    it. That makes the reachable endpoint space 1..INT64_MAX rather than the allocator's full
    unsigned range; the difference begins at 2**63 rows in one table, which is past the point at
    which the counter's own exhausted marker becomes reachable, and HeapStore refuses an endpoint
    outside it by name rather than letting a raw struct error out (A41).

    Not nullable: an edge with no source is not an edge. The refusal for a null endpoint is
    therefore the ordinary column refusal, raised while the tuple is being encoded -- which is
    before anything is pinned, staged or made reachable.
    """
    return (
        ColumnDef(name=SOURCE_COLUMN, type=ValueType.INT64, nullable=False),
        ColumnDef(name=TARGET_COLUMN, type=ValueType.INT64, nullable=False),
    )


def relationship_columns(properties: Sequence[ColumnDef]) -> tuple[ColumnDef, ...]:
    """Return the full column tuple of a relationship table: the endpoints, then the properties.

    Idempotent, so a caller may pass a tuple that already carries them -- which is what a table
    read back out of the catalog does.
    """
    columns = tuple(properties)
    if _leads_with_endpoints(columns):
        return columns
    return (*endpoint_column_defs(), *columns)


def relationship_row(
    source: RecordId, target: RecordId, properties: Sequence[Value]
) -> tuple[Value, ...]:
    """Return the stored tuple of one edge: source, target, then the user's property values.

    The one place the order is written down for callers. C5 stages the result like any other row
    and C10 encodes against it, and neither has to remember which end comes first.
    """
    return (source, target, *tuple(properties))


def _leads_with_endpoints(columns: Sequence[ColumnDef]) -> bool:
    """Return True when these columns already start with the reserved endpoint pair."""
    if len(columns) < ENDPOINT_COLUMN_COUNT:
        return False
    return tuple(column.name for column in columns[:ENDPOINT_COLUMN_COUNT]) == ENDPOINT_COLUMNS


def encode_tuple(table: TableDef, values: Sequence[Value]) -> bytes:
    """Return the positional encoding of one row of the table.

    Arity and column types are checked first and separately, so a caller that passes a tuple in
    the wrong order is told which column disagreed rather than handed a coerced row.
    """
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise SchemaMismatchError(
            f"Table {table.name!r} needs a sequence of values; got {type(values).__name__}.",
            table=table.name,
            table_id=table.table_id,
            value=type(values).__name__,
        )
    if len(values) != table.arity:
        raise SchemaMismatchError(
            f"Table {table.name!r} has {table.arity} columns, but the tuple carries "
            f"{len(values)} values.",
            table=table.name,
            table_id=table.table_id,
            expected_arity=table.arity,
            observed_arity=len(values),
        )
    encoded = bytearray()
    for position, (column, value) in enumerate(zip(table.columns, values)):
        kind = _check_column_value(table, position, column, value)
        _append_encoded_value(encoded, value, kind=kind)
    return bytes(encoded)


@dataclass(frozen=True, slots=True)
class TupleEncodingProofs:
    """Private composed protocol; opaque proofs belong to their minting registry."""

    encode: Callable[[TableDef, Sequence[Value]], tuple[bytes, object | None]]
    payload: Callable[[TableDef, Sequence[Value], object], bytes | None]
    forget: Callable[[object], None]


def _tuple_encoding_proof_protocol(
    entries: MutableMapping[object, tuple[object, object, bytes]],
    guard: AbstractContextManager[object],
) -> TupleEncodingProofs:
    """Build the private proof that lets one exact immutable row reuse its encoding.

    The registry and guard are injected by composition; the opaque proof type lives
    only in this closure. A proof never authenticates an
    ``id()`` by itself: its registry entry retains the exact table, values tuple and bytes object
    produced by :func:`encode_tuple`.  A copied/replaced intent, another tuple with equal values,
    or a caller-authored object therefore misses and must take the canonical encoder again.

    Weak keys bound retention to the proof's own lifetime.  The lock protects only registry
    publication/lookup/removal; encoding itself and every heap operation remain concurrent.
    """

    class Proof:
        """Opaque weak-referenceable token identifying one tuple encoding proof."""
        __slots__ = ("__weakref__",)

    def proof_safe(value: object) -> bool:
        """Return whether normal Python code cannot mutate this encoded value in place."""
        value_type = type(value)
        if value is None or value_type in (bool, int, float, str, bytes):
            return True
        if value_type in (Timestamp, Uuid, VectorValue):
            return True
        if value_type is tuple:
            return all(proof_safe(item) for item in value)
        # LIST/MAP also accept list/dict and BYTES accepts bytearray.  Their outer row tuple can
        # retain its identity while a nested value changes, so identity alone cannot authorize
        # reuse.  Unknown subclasses stay on the canonical path for the same reason.
        return False

    def encode_with_proof(
        table: TableDef, values: Sequence[Value]
    ) -> tuple[bytes, object | None]:
        """Encode the tuple and issue a proof only for stable exact values."""
        payload = encode_tuple(table, values)
        if type(values) is not tuple or not all(proof_safe(value) for value in values):
            return payload, None
        proof = Proof()
        with guard:
            entries[proof] = (table, values, payload)
        return payload, proof

    def proved_payload(
        table: TableDef, values: Sequence[Value], proof: object
    ) -> bytes | None:
        """Return bytes only when the proof still names this exact table and value tuple."""
        if type(proof) is not Proof:
            return None
        with guard:
            entry = entries.get(proof)
        if entry is None:
            return None
        proved_table, proved_values, payload = entry
        if proved_table is table and proved_values is values:
            return payload
        return None

    def forget(proof: object) -> None:
        """Withdraw the owned tuple encoding proof if it remains registered."""
        if type(proof) is not Proof:
            return
        with guard:
            entries.pop(proof, None)

    return TupleEncodingProofs(encode_with_proof, proved_payload, forget)


def _encode_tuple_with_proof(
    table: TableDef, values: Sequence[Value], *, protocol: TupleEncodingProofs | None = None
) -> tuple[bytes, object | None]:
    """Encode canonically; retain a proof only with an explicitly composed protocol."""
    return (encode_tuple(table, values), None) if protocol is None else protocol.encode(table, values)


def _proved_tuple_payload(
    table: TableDef, values: Sequence[Value], proof: object,
    *, protocol: TupleEncodingProofs | None = None,
) -> bytes | None:
    """Resolve only through the participant's own proof registry."""
    return None if protocol is None else protocol.payload(table, values, proof)


def _forget_tuple_encoding_proof(
    proof: object, *, protocol: TupleEncodingProofs | None = None
) -> None:
    """Revoke a local proof; another participant's proof is never recognized here."""
    if protocol is not None:
        protocol.forget(proof)


def decode_tuple(table: TableDef, buf: bytes) -> tuple[Value, ...]:
    """Return the row stored in the payload, checked against the schema of the table.

    Trailing bytes are corruption, not a mismatch: a payload that decodes into the declared
    number of values and then continues did not come from this encoder.
    """
    return _decode_tuple(table, buf, materialized_positions=None)


def decode_tuple_landing(table: TableDef, buf: bytes) -> tuple[Value, ...]:
    """Return the row for an identity landing: every check of ``decode_tuple``, no vector object.

    An identity landing consumes the header of the version it validates (its record id and its
    physical reference) and never a vector component, so the vector body is validated exactly as
    ``decode_tuple`` validates it (tag, header, dimension, space reference, body length, trailing
    bytes) and the 384-float object is simply not built.  The vector positions carry the shared
    decoder's ``_ValidatedValue`` sentinel, which is deliberately NOT a stored value: any attempt
    to type, encode or publish it is refused by ``value_type_of`` (RELSEEK-M4).  Every scalar
    column is decoded and judged exactly as ``decode_tuple`` does.
    """
    return _decode_tuple(
        table, buf, materialized_positions=None, materialize_vectors=False
    )


class _UnmaterializedColumn:
    """Private proof that a projected column was validated but not retained."""

    __slots__ = ()


_UNMATERIALIZED_COLUMN = _UnmaterializedColumn()


def _is_unmaterialized_column(value: object) -> bool:
    """Return whether ``value`` is the private projected-row sentinel."""
    return value is _UNMATERIALIZED_COLUMN


def _decode_tuple_projection(
    table: TableDef,
    buf: bytes,
    materialized_positions: frozenset[int],
) -> tuple[Value, ...]:
    """Validate a complete row while retaining only the selected positional values.

    This is an internal execution proof, not a public row decoder. The returned tuple preserves
    the table's full positional shape so trusted query consumers can use their ordinary column
    offsets; positions outside the closed plan carry an unforgeable sentinel. Every omitted
    value still passes the canonical non-materialising parser, including UTF-8, recursive MAP
    key, vector-boundary and trailing-payload validation.
    """
    return _decode_tuple(
        table,
        buf,
        materialized_positions=materialized_positions,
        preserve_positions=True,
    )


def decode_relationship_endpoints(
    table: TableDef, buf: bytes
) -> tuple[RecordId, RecordId]:
    """Return a relationship's endpoints after validating its complete stored tuple.

    This is an internal projection for heap consumers that need only the fixed leading pair.
    Every later value is still parsed by the same decoder and checked against its column, but
    values outside positions 0 and 1 are not retained as Python objects.
    """
    if table.kind != "rel":
        raise GrafxConfigurationError(
            f"Table {table.name!r} is a {table.kind} table and has no relationship endpoints.",
            field="kind",
            value=table.kind,
            table=table.name,
            table_id=table.table_id,
        )
    values = _decode_tuple(
        table,
        buf,
        materialized_positions=frozenset(range(ENDPOINT_COLUMN_COUNT)),
    )
    source, target = values
    # TableDef fixes both positions as non-null INT64 columns.  _decode_tuple has just proved
    # that the bytes satisfy those definitions, so these are RecordIds without another parser.
    return int(source), int(target)  # type: ignore[arg-type]


def _decode_tuple(
    table: TableDef,
    buf: bytes,
    *,
    materialized_positions: frozenset[int] | None,
    materialize_vectors: bool = True,
    preserve_positions: bool = False,
) -> tuple[Value, ...]:
    """Validate one tuple and retain either every value or selected positions.

    ``materialize_vectors=False`` is the identity-landing form (RELSEEK-M4): a vector column
    whose stored tag matches the plan is validated by ``_decode_vector_mode`` exactly as the
    materialising branch validates it, and its position carries the decoder's private sentinel
    instead of a ``VectorValue``.  A mismatched tag still takes the generic decoder, so the
    corruption-before-mismatch rule below is untouched.
    """
    values: list[Value] = (
        [_UNMATERIALIZED_COLUMN] * len(table.columns)  # type: ignore[list-item]
        if preserve_positions
        else []
    )
    offset = 0
    for position, (expected_tag, expected_type, nullable, column) in enumerate(
        table._decode_plan
    ):
        tag_offset = offset
        materialize = (
            materialized_positions is None or position in materialized_positions
        )
        if offset >= len(buf):
            # Keep the generic oracle's classified short-tag refusal verbatim.
            value, offset = decode_value(buf, offset)
        else:
            stored_tag = buf[offset]
            if stored_tag == expected_tag:
                if (
                    expected_type in VECTOR_VALUE_TYPES
                    and (not materialize or not materialize_vectors)
                ):
                    value, offset = _decode_vector_mode(
                        buf, offset + 1, expected_type, materialize=False
                    )
                elif expected_type is ValueType.STRING:
                    # STRING dominates wide graph rows.  Keep the generic decoder as the single
                    # oracle for mismatched tags and compound values, but execute this planned
                    # body in the table loop. A skipped string is decoded and discarded: UTF-8
                    # validation is part of the durable format and projection cannot remove it.
                    offset += 1
                    _require(buf, offset, _U32.size, "length")
                    length = _U32.unpack_from(buf, offset)[0]
                    offset += _U32.size
                    _require(buf, offset, length, "string")
                    following = offset + length
                    try:
                        value = bytes(buf[offset:following]).decode("utf-8")
                    except UnicodeDecodeError as failure:
                        raise GrafxCorruptionDetected(
                            "A stored STRING is not valid UTF-8.",
                            field="string",
                            offset=offset,
                            length=length,
                        ) from failure
                    offset = following
                elif materialize:
                    value, offset = _decode_expected_value_body(
                        buf, offset + 1, expected_type
                    )
                else:
                    offset = _validate_value(buf, offset)
                    value = None
            elif stored_tag == int(ValueType.NULL):
                value = None
                offset += 1
            elif materialize:
                # A mismatched value is still decoded completely before schema rejection. This
                # preserves the rule that malformed stored bytes are corruption rather than
                # being hidden by the schema mismatch they would otherwise reach first.
                value, offset = decode_value(buf, offset)
            else:
                offset = _validate_value(buf, offset)
                value = None
        if buf[tag_offset] == int(ValueType.NULL):
            if not nullable:
                raise _reject(
                    table,
                    column,
                    position,
                    "a null is not allowed in this column.",
                )
            if materialize:
                if preserve_positions:
                    values[position] = None
                else:
                    values.append(None)
            continue
        if buf[tag_offset] != expected_tag:
            observed = (
                value_type_of(value)
                if materialize
                else ValueType(buf[tag_offset])
            )
            raise _reject(
                table,
                column,
                position,
                f"a stored {observed.name} value does not belong to "
                f"a {column.type.name} column.",
            )
        if materialize:
            if preserve_positions:
                values[position] = value  # type: ignore[assignment]
            else:
                values.append(value)  # type: ignore[arg-type]
    if offset != len(buf):
        raise GrafxCorruptionDetected(
            f"A row of table {table.name!r} decoded {offset} of {len(buf)} payload bytes.",
            field="payload",
            table=table.name,
            consumed=offset,
            length=len(buf),
        )
    return tuple(values)
