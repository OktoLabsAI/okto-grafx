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

from collections.abc import Sequence
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import (
    MAX_VECTOR_DIMENSION,
    VECTOR_DTYPES,
    VECTOR_VALUE_TYPES,
    Value,
    ValueType,
    decode_value,
    encode_value,
    value_type_of,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric

__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "TABLE_KINDS",
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
"""A table describes either the nodes of a label or the relationships of a type."""

SPACE_STATE_ACTIVE: str = "active"
SPACE_STATE_RETIRED: str = "retired"

SPACE_STATES: tuple[str, ...] = (SPACE_STATE_ACTIVE, SPACE_STATE_RETIRED)
"""The only two states of an embedding space, and the only transition is from the first."""

STORAGE_DTYPES: tuple[str, ...] = ("float32", "float64")
"""The precisions an embedding space may store its vectors in."""


def is_identifier(name: object) -> bool:
    """Return True when the name is a usable ASCII identifier for a schema object."""
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


@dataclass(frozen=True, slots=True)
class TableDef:
    """A node table or a relationship table, with its columns in their stored order."""

    table_id: int
    name: str
    kind: str
    columns: tuple[ColumnDef, ...]
    primary_key: str | None = None
    from_table: str | None = None
    to_table: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Refuse a table whose shape contradicts the kind it declares."""
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
        """Return the number of columns a tuple of this table must carry."""
        return len(self.columns)

    def column(self, name: str) -> ColumnDef:
        """Return the column with that name."""
        for column in self.columns:
            if column.name == name:
                return column
        raise GrafxConfigurationError(
            f"Table {self.name!r} has no column named {name!r}.",
            field="column",
            value=name,
        )

    def column_index(self, name: str) -> int:
        """Return the position of the column with that name in the stored tuple."""
        for position, column in enumerate(self.columns):
            if column.name == name:
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


def _check_column_value(table: TableDef, position: int, column: ColumnDef, value: Value) -> None:
    """Refuse a value that the column does not declare, instead of coercing it."""
    if value is None:
        if column.nullable:
            return
        raise _reject(table, column, position, "a null is not allowed in this column.")
    observed = value_type_of(value)
    if observed is not column.type:
        raise _reject(
            table,
            column,
            position,
            f"a {observed.name} value cannot be stored in a {column.type.name} column.",
        )


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
    parts: list[bytes] = []
    for position, (column, value) in enumerate(zip(table.columns, values)):
        _check_column_value(table, position, column, value)
        parts.append(encode_value(value))
    return b"".join(parts)


def decode_tuple(table: TableDef, buf: bytes) -> tuple[Value, ...]:
    """Return the row stored in the payload, checked against the schema of the table.

    Trailing bytes are corruption, not a mismatch: a payload that decodes into the declared
    number of values and then continues did not come from this encoder.
    """
    values: list[Value] = []
    offset = 0
    for position, column in enumerate(table.columns):
        value, offset = decode_value(buf, offset)
        if value is None:
            if not column.nullable:
                raise _reject(table, column, position, "a null is not allowed in this column.")
            values.append(value)
            continue
        observed = value_type_of(value)
        if observed is not column.type:
            raise _reject(
                table,
                column,
                position,
                f"a stored {observed.name} value does not belong to a {column.type.name} column.",
            )
        values.append(value)
    if offset != len(buf):
        raise GrafxCorruptionDetected(
            f"A row of table {table.name!r} decoded {offset} of {len(buf)} payload bytes.",
            field="payload",
            table=table.name,
            consumed=offset,
            length=len(buf),
        )
    return tuple(values)
