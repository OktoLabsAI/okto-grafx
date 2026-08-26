"""The key of a vector index entry: a digest of the stored vector (CF-9, SPEC-VEC FR-6).

The index framework derives a key from the indexed columns of a row, which makes the key equal to
the value. For every other column that is the right rule and it buys a drift detector for free:
re-derive the key from the heap version, compare it against the stored entry, and a row that
changed without the index following says so.

For a vector column the value IS the payload, so the same rule would make the key as large as the
embedding. An entry is never split across a page, so that caps the storable dimension at a
function of ``page_size`` -- 2027 float32 components on the default 8192-byte page and 107 on the
512-byte minimum, which refuses the ``DOUBLE[384]`` of the consumer of record outright and leaves
``MAX_VECTOR_DIMENSION`` unreachable at every page size.

A digest keeps the detector and drops the coupling. A changed vector digests differently, so drift
is still caught exactly; the key is a constant 32 bytes whatever the dimension, so no embedding
model can outgrow a page. What is given up is the ability to read a vector back OUT of a key, and
nothing wanted to: the components live in the heap, which is where a search reads them from.

The format is pinned
--------------------
:data:`VECTOR_DIGEST_DERIVATION` is a FORMAT tag, not a tuning knob. It travels in the definition
digest an index file stores, so a file written under one derivation can never be opened under
another -- and changing how the digest is computed means changing this name, which invalidates
every stored index by refusing to open it rather than by answering wrongly. A test pins the name,
the size and the digest of a fixed vector, because all three are part of what a stored file means.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.model.value import Value, VectorValue, encode_value

__all__ = [
    "VECTOR_DIGEST_DERIVATION",
    "VECTOR_KEY_SIZE",
    "vector_digest",
    "VectorIndexDefinition",
]

VECTOR_DIGEST_DERIVATION: str = "vector_digest_v1"
"""The name this derivation travels under, inside the digest of the definition."""

VECTOR_KEY_SIZE: int = 32
"""Bytes of every vector index key: a full SHA-256, whatever the dimension of the space.

The digest is not truncated. A shortened one would save a few bytes per entry and buy a
collision, and two vectors sharing a key is a lookup that answers with the wrong row -- the exact
failure the drift detector exists to catch, introduced by the detector's own key.
"""


def vector_digest(value: Value) -> bytes:
    """Return the key of one stored vector: the SHA-256 of the bytes a page holds for it.

    The input is the ENCODED value, so the digest covers the components, the dimension and the
    embedding space together. A row that changed any of the three digests differently, which is
    what preserves the drift detector the column derivation gave away for free.
    """
    if not isinstance(value, VectorValue):
        raise GrafxIndexError(
            f"A vector index keys an embedding; got {type(value).__name__}.",
            field="key",
            value=type(value).__name__,
        )
    return hashlib.sha256(encode_value(value)).digest()


@dataclass(frozen=True, slots=True)
class VectorIndexDefinition(IndexDefinition):
    """An index definition whose key is a digest of the vector rather than the vector.

    The derivation is declared rather than assumed, which is what lets the framework's verifier
    re-derive a key the same way this index wrote it. A definition that named a derivation and
    did not override ``key_for`` would be handed column keys silently, so the base class refuses
    that -- and this class is the override that answers.
    """

    key_derivation: str = VECTOR_DIGEST_DERIVATION

    def _value_from(self, values: Sequence[Value]) -> Value:
        """Return this definition's column value after checking row arity."""

        position = self.positions[0]
        if not 0 <= position < len(values):
            raise GrafxIndexError(
                f"Index {self.name!r} keys column {position} of its table; the row it was given "
                f"holds {len(values)} values.",
                field="positions",
                index=self.name,
                value=position,
                arity=len(values),
            )
        return values[position]

    def key_for(self, values: Sequence[Value]) -> bytes:
        """Return the digest of a non-null vector column."""

        return vector_digest(self._value_from(values))

    def entry_key_for(self, values: Sequence[Value]) -> bytes | None:
        """Return no entry for ``NULL`` and a digest for a stored vector."""

        value = self._value_from(values)
        return None if value is None else vector_digest(value)

    def owes_entry(self, values: Sequence[Value]) -> bool:
        """Say whether the vector column is populated, without hashing it."""

        return self._value_from(values) is not None
