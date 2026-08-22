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
from collections.abc import Sequence
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.keys import (
    DEFAULT_BUCKET_COUNT,
    index_key,
    validate_bucket_count,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import MAX_IDENTIFIER_LENGTH, TableDef, is_identifier
from okto_grafx.domain.model.value import Value

__all__ = [
    "COLUMN_KEY_DERIVATION",
    "DEFINITION_DIGEST_SIZE",
    "INDEX_DIRECTORY",
    "INDEX_FILE_SUFFIX",
    "IndexDefinition",
    "index_file",
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


def index_file(name: str) -> str:
    """Return the file of an index, as CONTRACT.md section 6.1 names it.

    The separator is a forward slash because ``StorageDevice.list_files`` returns slash-separated
    names relative to the device root (amendment A15); the adapter, not the engine, knows what a
    path looks like on this platform.
    """
    return f"{INDEX_DIRECTORY}/{require_index_name(name)}{INDEX_FILE_SUFFIX}"


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
        if not isinstance(self.positions, tuple) or not self.positions:
            raise GrafxIndexError(
                f"Index {self.name!r} needs at least one key column, as a tuple of positions.",
                field="positions",
                value=repr(self.positions),
            )
        seen: set[int] = set()
        for position in self.positions:
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
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
        if not is_identifier(self.key_derivation):
            raise GrafxIndexError(
                f"Index {self.name!r} must name its key derivation as an ASCII identifier; got "
                f"{self.key_derivation!r}.",
                field="key_derivation",
                value=repr(self.key_derivation),
            )
        object.__setattr__(self, "visibility", IndexVisibility.parse(self.visibility))
        object.__setattr__(self, "bucket_count", validate_bucket_count(self.bucket_count))

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
        return index_file(self.name)

    def key_for(self, values: Sequence[Value]) -> bytes:
        """Return the index key of a row of this table.

        This body implements ONE derivation, the one named by :data:`COLUMN_KEY_DERIVATION`, and
        it refuses to answer for any other. A definition that declares a derivation and does not
        override this method would otherwise be handed column keys silently: the index would be
        built and verified under a rule it never claimed, and every lookup under the declared
        rule would come back empty while the structure looked perfectly healthy. Refusing is the
        only answer that cannot be mistaken for a working index.
        """
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

    def digest(self) -> bytes:
        """Return the digest an index file stores to prove which definition wrote it.

        Every field that changes what the file MEANS is in the digest: the table, the key
        columns, the visibility class, the bucket count -- which decides placement, so an index
        reopened with a different count would look up the wrong chain and answer with nothing --
        and the key derivation, because two derivations over the same columns produce different
        bytes for the same row. The name is in it too, because a file renamed under another
        index's name is not that index.
        """
        material = "\n".join(
            (
                self.name,
                str(self.table_id),
                self.table_name,
                ",".join(str(position) for position in self.positions),
                self.visibility.value,
                str(self.bucket_count),
                self.key_derivation,
            )
        )
        return hashlib.blake2b(
            material.encode("utf-8"), digest_size=DEFINITION_DIGEST_SIZE
        ).digest()
