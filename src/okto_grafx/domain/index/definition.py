"""What an index IS: the table it covers, the columns it keys on, and its visibility class.

The definition is a value, and a durable one. It is hashed into a digest that the index file
carries in its own header, so reopening a file under a definition that does not describe it is
refused rather than answered with entries derived from something else. That refusal is the
difference between an index that is stale -- repairable by rebuilding it -- and an index that is
about a different question entirely.

Column POSITIONS rather than column names are what the definition stores, resolved once against
the table at construction. A key must be re-derivable from a heap version alone when an exact hit
is validated, and a heap version carries positional values, not a mapping; resolving names at
validation time would need the catalog on the hot path and would give a renamed column two
answers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.fulltext import decode_options, entry_keys, is_fulltext
from okto_grafx.domain.index.keys import (
    DEFAULT_BUCKET_COUNT,
    index_key,
    record_id_key,
    validate_bucket_count,
)
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.ordered_keys import ordered_index_key
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import (
    MAX_IDENTIFIER_LENGTH,
    TableDef,
    is_identifier,
)
from okto_grafx.domain.model.value import Value

__all__ = [
    "automatic_index_definitions",
    "index_definition_matches_table",
    "COLUMN_KEY_DERIVATION",
    "DEFINITION_DIGEST_SIZE",
    "RECORD_ID_KEY_DERIVATION",
    "ORDERED_KEY_DERIVATION",
    "INDEX_DIRECTORY",
    "INDEX_FILE_SUFFIX",
    "IndexDefinition",
    "index_file",
    "index_generation_file",
    "require_index_name",
]

INDEX_DIRECTORY: str = "index"
"""The directory of a database that holds its secondary indexes (CONTRACT.md section 6.1)."""

INDEX_FILE_SUFFIX: str = ".idx"
"""The suffix of a paged secondary index file (CONTRACT.md section 6.1)."""

DEFINITION_DIGEST_SIZE: int = 16
"""Bytes of the digest an index file stores to prove which definition wrote it."""

COLUMN_KEY_DERIVATION: str = "columns"
"""The derivation this class implements: the key IS the encoding of the named columns.

It is a FORMAT tag, not a tuning knob. It travels in the definition digest, so an index file
written under one derivation cannot be opened under another -- which is the whole point, because
two derivations over the same columns produce different bytes for the same row and an index read
under the wrong one answers with nothing while looking perfectly healthy.
"""

RECORD_ID_KEY_DERIVATION: str = "record_id_u64_v1"
"""The built-in derivation whose key is the row's stable unsigned identity."""

ORDERED_KEY_DERIVATION: str = "ordered_timestamp_string_v1"
"""The order-preserving v1 derivation for one TIMESTAMP and one STRING column.

The codec is introduced with the ordered store. Naming the derivation in the durable definition
now prevents an ordered artifact from ever being opened under the hash key encoder.
"""


def index_file(name: str) -> str:
    """Return the file of an index, as CONTRACT.md section 6.1 names it.

    The separator is a forward slash because ``StorageDevice.list_files`` returns slash-separated
    names relative to the device root (amendment A15); the adapter, not the engine, knows what a
    path looks like on this platform.
    """
    return f"{INDEX_DIRECTORY}/{require_index_name(name)}{INDEX_FILE_SUFFIX}"


def index_generation_file(artifact_nonce: object) -> str:
    """Return the canonical physical path derived from one non-zero generation nonce."""

    if (
        isinstance(artifact_nonce, bool)
        or not isinstance(artifact_nonce, int)
        or not 1 <= artifact_nonce <= 0xFFFFFFFFFFFFFFFF
    ):
        raise GrafxIndexError(
            "An index generation needs a non-zero unsigned 64-bit artifact nonce.",
            field="artifact_nonce",
            value=repr(artifact_nonce),
        )
    return index_file(f"g_{artifact_nonce:016x}")


def require_index_name(name: object) -> str:
    """Return the name when it can be an index name, else refuse it.

    An index name becomes a FILE name, so it is held to the schema identifier rule: ASCII,
    starting with a letter or underscore, no separator and no character a platform treats
    specially. Case is preserved but never load-bearing -- two indexes whose names differ only in
    case would be one file on Windows and two on POSIX, so the registry compares them folded
    (definition of done item 9).
    """
    if not is_identifier(name):
        raise GrafxIndexError(
            "An index name must be an ASCII identifier of at most "
            f"{MAX_IDENTIFIER_LENGTH} characters; got {name!r}.",
            field="name",
            value=repr(name),
        )
    return str(name)


@dataclass(frozen=True, slots=True)
class IndexDefinition:
    """One secondary index: its name, the table it covers, its key columns and its contract.

    The definition also carries HOW its key is derived. For an ordinary column index the key is
    the encoding of the named columns and nothing else, which is what gives verification a free
    drift detector: re-deriving the key from the heap version and comparing it against the stored
    entry catches a row that changed without the index following.

    That rule cannot be universal, because it makes the key as large as the value. A vector
    column is the value, so a key-equals-value index would cap the embedding dimension at a
    function of ``page_size`` -- an entry is never split across a page, so at the 512-byte minimum
    the budget is 449 bytes, which refuses ``DOUBLE[384]`` outright and makes
    ``MAX_VECTOR_DIMENSION`` unreachable at every page size. Tying the supported dimension to a
    storage parameter, and leaving a declared constant unreachable, are both worse than letting
    the definition say what its key is.

    So an index whose values are large declares its own derivation: it names one in
    ``key_derivation`` and overrides :meth:`key_for` to produce something of constant size -- a
    digest of the vector, for instance. The drift detector is preserved exactly, because a
    changed vector digests differently; only the size of the key changes. The name travels in
    :meth:`digest`, so a file written under one derivation can never be opened under another.
    """

    name: str
    table_id: int
    table_name: str
    positions: tuple[int, ...]
    visibility: IndexVisibility
    bucket_count: int = DEFAULT_BUCKET_COUNT
    key_derivation: str = COLUMN_KEY_DERIVATION
    artifact_nonce: int = 0
    layout: IndexLayout = IndexLayout.HASH

    def __post_init__(self) -> None:
        """Refuse a definition that could not name a file, a table, or a key."""
        require_index_name(self.name)
        if not is_identifier(self.table_name):
            raise GrafxIndexError(
                f"An index must name the table it covers; got {self.table_name!r}.",
                field="table_name",
                value=repr(self.table_name),
            )
        if isinstance(self.table_id, bool) or not isinstance(self.table_id, int):
            raise GrafxIndexError(
                f"An index needs an integer table_id; got {type(self.table_id).__name__}.",
                field="table_id",
                value=repr(self.table_id),
            )
        if not 1 <= self.table_id <= 0xFFFFFFFF:
            raise GrafxIndexError(
                f"An index needs a table_id between 1 and 4294967295; got {self.table_id}.",
                field="table_id",
                value=self.table_id,
            )
        if not isinstance(self.positions, tuple) or (
            not self.positions and self.key_derivation != RECORD_ID_KEY_DERIVATION
        ):
            raise GrafxIndexError(
                f"Index {self.name!r} needs at least one key column, as a tuple of positions.",
                field="positions",
                value=repr(self.positions),
            )
        seen: set[int] = set()
        for position in self.positions:
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise GrafxIndexError(
                    f"Index {self.name!r} needs every key column position to be a non-negative "
                    f"integer; got {position!r}.",
                    field="positions",
                    value=repr(position),
                )
            if position in seen:
                raise GrafxIndexError(
                    f"Index {self.name!r} names the column at position {position} twice.",
                    field="positions",
                    value=position,
                )
            seen.add(position)
        if self.key_derivation == RECORD_ID_KEY_DERIVATION and self.positions:
            raise GrafxIndexError(
                f"Identity index {self.name!r} keys RecordId, not stored columns, so its "
                "positions tuple must be empty.",
                field="positions",
                value=repr(self.positions),
                index=self.name,
            )
        if not is_identifier(self.key_derivation):
            raise GrafxIndexError(
                f"Index {self.name!r} must name its key derivation as an ASCII identifier; got "
                f"{self.key_derivation!r}.",
                field="key_derivation",
                value=repr(self.key_derivation),
            )
        if (
            isinstance(self.artifact_nonce, bool)
            or not isinstance(self.artifact_nonce, int)
            or not 0 <= self.artifact_nonce <= 0xFFFFFFFFFFFFFFFF
        ):
            raise GrafxIndexError(
                f"Index {self.name!r} needs an unsigned 64-bit artifact nonce; got "
                f"{self.artifact_nonce!r}.",
                field="artifact_nonce",
                value=repr(self.artifact_nonce),
                index=self.name,
            )
        object.__setattr__(self, "visibility", IndexVisibility.parse(self.visibility))
        object.__setattr__(self, "layout", IndexLayout.parse(self.layout))
        object.__setattr__(
            self, "bucket_count", validate_bucket_count(self.bucket_count)
        )
        if self.layout is IndexLayout.SPARSE_HASH and (
            self.visibility is not IndexVisibility.EXACT
            or self.key_derivation != COLUMN_KEY_DERIVATION
        ):
            raise GrafxIndexError("Sparse hash requires an exact property index.", field="layout")
        if is_fulltext(self.key_derivation):
            options = decode_options(self.key_derivation)
            if len(options.field_weights) != len(self.positions) or self.layout is not IndexLayout.HASH or self.visibility is not IndexVisibility.EXACT:
                raise GrafxIndexError("Full-text fields/layout do not match their analyzer identity.", field="definition")
        if self.layout is IndexLayout.ORDERED:
            if self.visibility is not IndexVisibility.EXACT:
                raise GrafxIndexError(
                    "The ordered v1 layout is an exact access path.",
                    field="visibility",
                    value=self.visibility.value,
                    index=self.name,
                )
            if self.key_derivation != ORDERED_KEY_DERIVATION:
                raise GrafxIndexError(
                    "The ordered v1 layout requires the ordered_timestamp_string_v1 key "
                    "derivation.",
                    field="key_derivation",
                    value=self.key_derivation,
                    index=self.name,
                )
            if len(self.positions) != 2:
                raise GrafxIndexError(
                    "The ordered v1 layout keys exactly two column positions.",
                    field="positions",
                    value=repr(self.positions),
                    index=self.name,
                )
            if self.bucket_count != 1:
                raise GrafxIndexError(
                    "The ordered v1 layout reserves bucket_count=1 as a binary-stable "
                    "sentinel; it is not a sizing option.",
                    field="bucket_count",
                    value=self.bucket_count,
                    index=self.name,
                )
        elif self.key_derivation == ORDERED_KEY_DERIVATION:
            raise GrafxIndexError(
                "The ordered_timestamp_string_v1 derivation belongs only to the ordered "
                "layout.",
                field="layout",
                value=self.layout.value,
                index=self.name,
            )

    @classmethod
    def on(
        cls,
        table: TableDef,
        *,
        name: str,
        columns: Sequence[str],
        visibility: IndexVisibility | str,
        bucket_count: int = DEFAULT_BUCKET_COUNT,
        key_derivation: str = COLUMN_KEY_DERIVATION,
        artifact_nonce: int = 0,
        layout: IndexLayout | str = IndexLayout.HASH,
    ) -> IndexDefinition:
        """Return the definition of an index over these columns of this table.

        The column names are resolved to positions once, here, and a name the table does not have
        is refused with the names it does have -- a caller that mistyped a column needs the
        vocabulary, not merely a refusal.
        """
        if not isinstance(table, TableDef):
            raise GrafxIndexError(
                f"An index is defined on a TableDef; got {type(table).__name__}.",
                field="table",
                value=type(table).__name__,
            )
        if isinstance(columns, str) or not isinstance(columns, Sequence) or not columns:
            raise GrafxIndexError(
                f"Index {name!r} needs a non-empty sequence of column names.",
                field="columns",
                value=repr(columns),
            )
        order = {column.name: position for position, column in enumerate(table.columns)}
        positions: list[int] = []
        for column in columns:
            position = order.get(column) if isinstance(column, str) else None
            if position is None:
                available = ", ".join(repr(known) for known in order)
                raise GrafxIndexError(
                    f"Table {table.name!r} has no column {column!r}; it has {available}.",
                    field="columns",
                    value=repr(column),
                    table=table.name,
                )
            positions.append(position)
        return cls(
            name=name,
            table_id=table.table_id,
            table_name=table.name,
            positions=tuple(positions),
            visibility=IndexVisibility.parse(visibility),
            bucket_count=bucket_count,
            key_derivation=key_derivation,
            artifact_nonce=artifact_nonce,
            layout=IndexLayout.parse(layout),
        )

    @property
    def versioned(self) -> bool:
        """Return True when entries of this index carry their own birth stamp.

        There is exactly one place where the visibility class becomes a property of the stored
        bytes, and this is it. Every entry this index writes reads its ``versioned`` flag from
        here, so an index cannot declare one contract and store entries shaped for the other.
        """
        return self.visibility is IndexVisibility.PROXIMITY

    @property
    def registry_key(self) -> str:
        """Return the folded name the registry compares, so case never decides identity."""
        return self.name.lower()

    @property
    def file(self) -> str:
        """Return the paged file this index is stored in."""
        return (
            index_file(self.name)
            if self.artifact_nonce == 0
            else index_generation_file(self.artifact_nonce)
        )

    def key_for(self, values: Sequence[Value]) -> bytes:
        """Return the index key of a row of this table.

        This body implements ONE derivation, the one named by :data:`COLUMN_KEY_DERIVATION`, and
        it refuses to answer for any other. A definition that declares a derivation and does not
        override this method would otherwise be handed column keys silently: the index would be
        built and verified under a rule it never claimed, and every lookup under the declared
        rule would come back empty while the structure looked perfectly healthy. Refusing is the
        only answer that cannot be mistaken for a working index.
        """
        if self.key_derivation == ORDERED_KEY_DERIVATION:
            return ordered_index_key(values, self.positions)
        if self.key_derivation != COLUMN_KEY_DERIVATION:
            raise GrafxIndexError(
                f"Index {self.name!r} declares the {self.key_derivation!r} key derivation, which "
                f"this definition does not implement; a definition that names its own derivation "
                f"must override key_for.",
                field="key_derivation",
                value=self.key_derivation,
                index=self.name,
            )
        return index_key(values, self.positions)

    def key_for_record(self, record_id: object, values: Sequence[Value]) -> bytes:
        """Return this definition's key with the complete heap-version identity available.

        Column definitions retain their established value-only derivation.  The identity
        derivation deliberately ignores stored values and uses the unsigned logical RecordId,
        which is not representable by the public signed ``INT64`` value codec.
        """

        if self.key_derivation == RECORD_ID_KEY_DERIVATION:
            return record_id_key(record_id)
        return self.key_for(values)

    def entry_key_for(self, values: Sequence[Value]) -> bytes | None:
        """Return the stored key, or ``None`` when this row has no index entry.

        Ordinary exact indexes represent every row, including nullable scalar
        keys.  Sparse index definitions override this door so staging,
        rebuild and both verifiers make the same inclusion decision.
        """

        return self.key_for(values)

    def entry_key_for_record(
        self, record_id: object, values: Sequence[Value]
    ) -> bytes | None:
        """Return the stored key with RecordId available to non-column derivations."""

        if self.key_derivation == RECORD_ID_KEY_DERIVATION:
            return record_id_key(record_id)
        return self.entry_key_for(values)

    def owes_entry(self, values: Sequence[Value]) -> bool:
        """Say whether this row owes an entry without deriving its potentially large key.

        Exact indexes include nullable scalar keys, so the default is deliberately ``True``.
        Sparse definitions override this predicate; transaction WAL pre-counting uses it to
        avoid performing an expensive derivation a second time before staging.
        """

        return True

    def entry_keys_for_record(self, record_id: object, values: Sequence[Value]) -> tuple[bytes, ...]:
        """Return every owed key; scalar/sparse definitions keep their established rule."""
        if is_fulltext(self.key_derivation):
            return entry_keys(values, self.positions, self.key_derivation)
        key = self.entry_key_for_record(record_id, values)
        return () if key is None else (key,)

    def entry_matches(self, key: bytes, record_id: object, values: Sequence[Value]) -> bool:
        """Validate membership, including a full-text row's multiple distinct postings."""
        if is_fulltext(self.key_derivation):
            return key in self.entry_keys_for_record(record_id, values)
        return self.entry_key_for_record(record_id, values) == key

    def entry_count_for_record(self, record_id: object, values: Sequence[Value]) -> int:
        """Size logical WAL effects without pretending a multi-term row owes one entry."""
        if is_fulltext(self.key_derivation):
            return len(self.entry_keys_for_record(record_id, values))
        return int(self.owes_entry_for_record(record_id, values))

    def owes_entry_for_record(self, record_id: object, values: Sequence[Value]) -> bool:
        """Say whether the complete heap version owes an entry.

        Validating the identity here keeps WAL pre-counting and staging on the same domain.
        Sparse value-derived definitions retain their existing predicate.
        """

        if self.key_derivation == RECORD_ID_KEY_DERIVATION:
            record_id_key(record_id)
            return True
        return self.owes_entry(values)

    def digest(self) -> bytes:
        """Return the digest an index file stores to prove which definition wrote it.

        Every field that changes what the file MEANS is in the digest: the table, the key
        columns, the visibility class, the bucket count -- which decides placement, so an index
        reopened with a different count would look up the wrong chain and answer with nothing --
        and the key derivation, because two derivations over the same columns produce different
        bytes for the same row. The name is in it too, because a file renamed under another
        index's name is not that index.
        """
        fields = (
            self.name,
            str(self.table_id),
            self.table_name,
            ",".join(str(position) for position in self.positions),
            self.visibility.value,
            str(self.bucket_count),
            self.key_derivation,
        )
        # Preserve every established HASH digest byte-for-byte. Ordered artifacts append their
        # physical layout so no file can be adopted under the other placement contract.
        if self.layout is not IndexLayout.HASH:
            fields = (*fields, self.layout.value)
        material = "\n".join(fields)
        return hashlib.blake2b(
            material.encode("utf-8"), digest_size=DEFINITION_DIGEST_SIZE
        ).digest()


def _automatic_index_projection(
    table: TableDef,
) -> tuple[tuple[IndexDefinition, ...], Mapping[str, IndexDefinition]]:
    """Normalize one immutable table instance without retaining catalog authority.

    DDL validation asks the same question repeatedly while a transaction grows its speculative
    catalog.  ``TableDef`` is an immutable value and automatic definitions depend only on that
    complete value, never on a catalog generation, registered store or physical nonce.  The
    immutable derived projection can therefore live with that table instance without carrying
    authority across DDL replacements, commits or processes.
    """
    cached = table._automatic_index_projection
    if cached is not None:
        return cast(
            tuple[tuple[IndexDefinition, ...], Mapping[str, IndexDefinition]], cached
        )

    definitions: list[IndexDefinition] = []
    if table.kind == "rel":
        candidates = (
            (f"ef_{table.name}", (0,)),
            (f"et_{table.name}", (1,)),
        )
        for name, positions in candidates:
            try:
                definitions.append(
                    IndexDefinition(
                        name=name,
                        table_id=table.table_id,
                        table_name=table.name,
                        positions=positions,
                        visibility=IndexVisibility.EXACT,
                    )
                )
            except GrafxIndexError:
                continue
    elif table.primary_key is not None:
        try:
            definitions.append(
                IndexDefinition(
                    name=f"pk_{table.name}",
                    table_id=table.table_id,
                    table_name=table.name,
                    positions=(table.column_index(table.primary_key),),
                    visibility=IndexVisibility.EXACT,
                )
            )
        except GrafxIndexError:
            pass
    # Local import breaks the intentional definition -> vector-definition inheritance cycle.
    from okto_grafx.domain.vector.key import VectorIndexDefinition

    for position, column in enumerate(table.columns):
        if column.vector_space is None:
            continue
        try:
            definitions.append(
                VectorIndexDefinition(
                    name=f"vector_{table.name}_{column.vector_space}",
                    table_id=table.table_id,
                    table_name=table.name,
                    positions=(position,),
                    visibility=IndexVisibility.PROXIMITY,
                )
            )
        except GrafxIndexError:
            continue
    normalized = tuple(definitions)
    projection: tuple[tuple[IndexDefinition, ...], Mapping[str, IndexDefinition]] = (
        normalized,
        MappingProxyType(
            {definition.registry_key: definition for definition in normalized}
        ),
    )
    object.__setattr__(table, "_automatic_index_projection", projection)
    return projection


def automatic_index_definitions(table: TableDef) -> tuple[IndexDefinition, ...]:
    """Return the exact automatic definitions this table's committed schema declares.

    Concurrent speculative catalogs may reuse both a numeric id and a table name.  Positions,
    visibility and key derivation are therefore part of provenance too; returning value objects
    here lets planning, DML staging, public inventory and verification share that complete test.
    Each accelerator is optional independently: an inexpressible derived name may decline that
    one path, but can never hide a valid sibling or make the committed table unusable.
    """
    if not isinstance(table, TableDef):
        return ()
    return _automatic_index_projection(table)[0]


def index_definition_matches_table(
    definition: IndexDefinition, table: TableDef
) -> bool:
    """Match complete provenance for automatic names and positional semantics for custom ones."""
    if (
        not isinstance(definition, IndexDefinition)
        or not isinstance(table, TableDef)
        or definition.table_id != table.table_id
        or definition.table_name != table.name
    ):
        return False
    automatic = _automatic_index_projection(table)[1]
    expected = automatic.get(definition.registry_key)
    if expected is not None:
        # Bucket count and artifact nonce describe one physical generation, not the logical
        # schema provenance. Rehashed automatic exact indexes therefore remain the same access
        # path. The concrete type is still load-bearing for specialized derivations: accepting
        # a base IndexDefinition in place of VectorIndexDefinition would preserve the digest but
        # lose the vector key implementation on reopen.
        return (
            type(definition) is type(expected)
            and definition.name == expected.name
            and definition.table_id == expected.table_id
            and definition.table_name == expected.table_name
            and definition.positions == expected.positions
            and definition.visibility is expected.visibility
            and definition.key_derivation == expected.key_derivation
            and definition.layout is expected.layout
        )
    stored_arity = len(table.columns) + (2 if table.kind == "rel" else 0)
    return all(position < stored_arity for position in definition.positions)
