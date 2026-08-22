"""The in-memory catalog and the bytes it is persisted as (CONTRACT.md section 7.2).

The catalog is the answer to "what tables and what embedding spaces exist". It is a value with
rules rather than a container: a table is added once and never mutated, a space is added once and
the only change it ever accepts is from active to retired (SPEC-VEC TR-2), and a vector column
may only point at a space that already exists and still accepts writes.

Its serialised form is self-describing and checksummed, because the catalog is what every other
file is interpreted through: losing it silently would turn every heap page into an unreadable
blob. The layout is

    magic 8B "GRFXCTLG" | format_version u16 | reserved u16 |
    table_count u32 | space_count u32 | next_table_id u32 | next_space_id u32 |
    tables | spaces | crc32c u32

with tables ordered by table_id and spaces ordered by space_id, so the same catalog always
serialises to the same bytes.
"""

from __future__ import annotations

import struct

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
)
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
    "CATALOG_MAGIC",
    "CATALOG_FORMAT_VERSION",
    "Catalog",
]

CATALOG_MAGIC: bytes = b"GRFXCTLG"
"""The eight bytes that open a serialised catalog."""

CATALOG_FORMAT_VERSION: int = 1
"""The catalog format this build writes. Every earlier version stays readable."""

_PREAMBLE = struct.Struct("<8sHHIIII")
_U8 = struct.Struct("<B")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_F64 = struct.Struct("<d")
_CHECKSUM = struct.Struct("<I")
_MAX_TEXT = 0xFFFF


class Catalog:
    """The set of tables and embedding spaces of one database.

    Identity is immutable by construction: a table or a space is installed once, and the only
    sanctioned change to either is retiring a space. Everything that would mutate an existing
    definition is refused with a typed error rather than applied.
    """

    __slots__ = ("_tables", "_tables_by_id", "_spaces", "_spaces_by_id")

    def __init__(self) -> None:
        """Build an empty catalog."""
        self._tables: dict[str, TableDef] = {}
        self._tables_by_id: dict[int, TableDef] = {}
        self._spaces: dict[str, EmbeddingSpaceDef] = {}
        self._spaces_by_id: dict[int, EmbeddingSpaceDef] = {}

    # --- reading ---------------------------------------------------------------------------

    def tables(self) -> tuple[TableDef, ...]:
        """Return every table, ordered by table_id."""
        return tuple(self._tables_by_id[key] for key in sorted(self._tables_by_id))

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        """Return every embedding space, ordered by space_id."""
        return tuple(self._spaces_by_id[key] for key in sorted(self._spaces_by_id))

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
        parts: list[bytes] = [
            _PREAMBLE.pack(
                CATALOG_MAGIC,
                CATALOG_FORMAT_VERSION,
                0,
                len(self._tables_by_id),
                len(self._spaces_by_id),
                self.next_table_id(),
                self.next_space_id(),
            )
        ]
        for table in self.tables():
            parts.append(_encode_table(table))
        for space in self.spaces():
            parts.append(_encode_space(space))
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
        magic, format_version, _reserved, table_count, space_count, next_table, next_space = (
            _PREAMBLE.unpack_from(raw, 0)
        )
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
        tables: list[TableDef] = []
        spaces: list[EmbeddingSpaceDef] = []
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
        except GrafxConfigurationError as invalid:
            raise GrafxCorruptionDetected(
                f"A stored catalog describes something this build cannot serve: "
                f"{invalid.message}",
                field=str(invalid.details.get("field", "catalog")),
                value=invalid.details.get("value"),
            ) from invalid
        catalog._install_loaded(tables, spaces)
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
        return self._tables == other._tables and self._spaces == other._spaces

    def __repr__(self) -> str:
        return f"Catalog(tables={len(self._tables)}, spaces={len(self._spaces)})"

    # --- internals -------------------------------------------------------------------------

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
