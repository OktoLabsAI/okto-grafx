"""Deriving an index key from a row, and placing that key in a bucket (SPEC-M1 FR-12).

Two properties matter here and nothing else does.

**The derivation is total and deterministic.** The key of a row is the encoding of its indexed
columns, produced by the one value encoder C1 owns. Two processes, two runs and two platforms
therefore agree on the bytes, and the same function that produced the entry re-derives the key
when an exact hit is validated against the heap -- which is what makes "the heap is the truth"
a check rather than a hope. A second encoding of the same values would be a second thing that
can drift.

**The placement is deterministic too.** The bucket of a key is CRC-32C of the key modulo the
bucket count. Python's built-in ``hash`` is salted per process, so a durable structure that used
it would place the same key in different buckets in two processes and lose every entry written by
the other -- silently, and only under multi-process use, which is the one thing SPEC-M1 exists to
prove correct. CRC-32C is already in the tree, already used for pages, the WAL and the ledger, and
it does not need to be cryptographic: it decides placement, never identity.
"""

from __future__ import annotations

from collections.abc import Sequence

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.model.value import Value, encode_value
from okto_grafx.domain.page.checksum import crc32c

__all__ = [
    "DEFAULT_BUCKET_COUNT",
    "MAX_BUCKET_COUNT",
    "MIN_BUCKET_COUNT",
    "bucket_of",
    "index_key",
    "validate_bucket_count",
]

MIN_BUCKET_COUNT: int = 1
"""The smallest legal bucket count: one bucket is a single chain and is still a correct index."""

MAX_BUCKET_COUNT: int = 4096
"""The largest legal bucket count.

Every bucket owns a head page from the moment the index is created, so the count is also the
minimum size of the file in pages. Four thousand pages is 32 MiB at the default page size, which
is a generous ceiling for a reference index and a bound that keeps a mistyped configuration from
allocating a file the engine may never shrink (G6).
"""

DEFAULT_BUCKET_COUNT: int = 64
"""Buckets an index gets when its definition does not say. Small enough to stay cheap on a tiny
database, large enough that the reference index really does spread keys across chains."""


def validate_bucket_count(bucket_count: object) -> int:
    """Return the bucket count when it is a usable one, else refuse it."""
    if isinstance(bucket_count, bool) or not isinstance(bucket_count, int):
        raise GrafxIndexError(
            f"A bucket count must be an integer; got {type(bucket_count).__name__}.",
            field="bucket_count",
            value=repr(bucket_count),
        )
    if not MIN_BUCKET_COUNT <= bucket_count <= MAX_BUCKET_COUNT:
        raise GrafxIndexError(
            f"A bucket count must be between {MIN_BUCKET_COUNT} and {MAX_BUCKET_COUNT}; got "
            f"{bucket_count}.",
            field="bucket_count",
            value=bucket_count,
        )
    return bucket_count


def index_key(values: Sequence[Value], positions: Sequence[int]) -> bytes:
    """Return the index key of a row: the encoding of the columns at these positions, in order.

    The encoding of every value is self-delimiting -- a tag byte, then a body whose length the
    tag or an explicit prefix decides -- so concatenating several of them is unambiguous and a
    two-column key can never be confused with a one-column key that happens to share its bytes.
    """
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise GrafxIndexError(
            f"An index key needs a sequence of values; got {type(values).__name__}.",
            field="values",
            value=type(values).__name__,
        )
    if not isinstance(positions, Sequence) or not positions:
        raise GrafxIndexError(
            "An index key needs at least one column position.",
            field="positions",
            value=repr(positions),
        )
    parts: list[bytes] = []
    for position in positions:
        if isinstance(position, bool) or not isinstance(position, int):
            raise GrafxIndexError(
                f"A column position must be an integer; got {type(position).__name__}.",
                field="positions",
                value=repr(position),
            )
        if not 0 <= position < len(values):
            raise GrafxIndexError(
                f"A column position must name one of the {len(values)} values of the row; got "
                f"{position}.",
                field="positions",
                value=position,
                arity=len(values),
            )
        parts.append(encode_value(values[position]))
    return b"".join(parts)


def bucket_of(key: bytes, bucket_count: int) -> int:
    """Return the bucket a key belongs to, the same way in every process and on every platform."""
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise GrafxIndexError(
            f"An index key must be bytes; got {type(key).__name__}.",
            field="key",
            value=type(key).__name__,
        )
    return crc32c(bytes(key)) % validate_bucket_count(bucket_count)
