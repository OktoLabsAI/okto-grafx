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

import struct
from collections.abc import Sequence

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.model.value import Value, encode_value
from okto_grafx.domain.page.checksum import crc32c

__all__ = [
    "DEFAULT_BUCKET_COUNT",
    "MAX_BUCKET_COUNT",
    "MAX_EXPECTED_CARDINALITY",
    "MIN_BUCKET_COUNT",
    "RECORD_ID_KEY_FORMAT_VERSION",
    "TARGET_ENTRIES_PER_BUCKET",
    "bucket_of",
    "custom_index_sizing",
    "identity_index_sizing",
    "index_key",
    "record_id_key",
    "rehash_index_sizing",
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

TARGET_ENTRIES_PER_BUCKET: int = 64
"""Canonical average occupancy target used by automatic sizing and assisted growth."""

_AUTOMATIC_IDENTITY_MIN_EXPECTED: int = (
    DEFAULT_BUCKET_COUNT * TARGET_ENTRIES_PER_BUCKET
)

MAX_EXPECTED_CARDINALITY: int = MAX_BUCKET_COUNT * TARGET_ENTRIES_PER_BUCKET
"""Largest sizing hint representable by the eager hash directory."""

RECORD_ID_KEY_FORMAT_VERSION: int = 1
"""Version tag prefixed to the canonical unsigned row-identity key."""

_RECORD_ID = struct.Struct("<BQ")
_FIRST_RECORD_ID: int = 1
_EXHAUSTED_RECORD_ID: int = 0xFFFFFFFFFFFFFFFF


def _bucket_count_for_expected(expected_cardinality: int) -> int:
    """Resolve one already-validated expected count by the single P2-ID formula."""
    required = (
        expected_cardinality + TARGET_ENTRIES_PER_BUCKET - 1
    ) // TARGET_ENTRIES_PER_BUCKET
    return validate_bucket_count(1 << (required - 1).bit_length())


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


def custom_index_sizing(
    *,
    bucket_count: object | None = None,
    expected_cardinality: object | None = None,
) -> tuple[int, int | None]:
    """Return ``(bucket_count, expected_cardinality)`` for a custom exact index.

    The two hints are alternatives: a bucket count chooses the physical directory exactly,
    while an expected cardinality records the caller's sizing intent and deterministically
    derives the next power-of-two directory at sixty-four expected entries per bucket.  The
    eager directory has a finite bound, so no value is silently capped.
    """

    if bucket_count is not None and expected_cardinality is not None:
        raise GrafxIndexError(
            "Index sizing accepts either bucket_count or expected_cardinality, not both.",
            field="sizing",
            bucket_count=repr(bucket_count),
            expected_cardinality=repr(expected_cardinality),
        )
    if bucket_count is not None:
        return validate_bucket_count(bucket_count), None
    if expected_cardinality is None:
        return DEFAULT_BUCKET_COUNT, None
    if isinstance(expected_cardinality, bool) or not isinstance(
        expected_cardinality, int
    ):
        raise GrafxIndexError(
            "An expected cardinality must be a positive integer; "
            f"got {type(expected_cardinality).__name__}.",
            field="expected_cardinality",
            value=repr(expected_cardinality),
        )
    if not 1 <= expected_cardinality <= MAX_EXPECTED_CARDINALITY:
        raise GrafxIndexError(
            "An expected cardinality must fit the eager hash-directory limit "
            f"1..{MAX_EXPECTED_CARDINALITY}; got {expected_cardinality}.",
            field="expected_cardinality",
            value=expected_cardinality,
            max_expected_cardinality=MAX_EXPECTED_CARDINALITY,
        )
    return _bucket_count_for_expected(expected_cardinality), expected_cardinality


def rehash_index_sizing(
    current_bucket_count: object,
    *,
    bucket_count: object | None = None,
    expected_cardinality: object | None = None,
) -> tuple[int, int | None]:
    """Resolve one strictly growing exact-index directory request.

    Rehash has no implicit default: exactly one physical count or cardinality hint is required.
    The selected hint is resolved by :func:`custom_index_sizing`, so creation and maintenance
    share the same bounds and power-of-two formula.  An explicit bucket count has no persisted
    cardinality hint, while a derived count retains the caller's cardinality intent.
    """

    if (
        isinstance(current_bucket_count, bool)
        or not isinstance(current_bucket_count, int)
        or not MIN_BUCKET_COUNT <= current_bucket_count <= MAX_BUCKET_COUNT
    ):
        raise GrafxIndexError(
            "Rehash needs the active generation's legal bucket count; "
            f"got {current_bucket_count!r}.",
            field="current_bucket_count",
            value=repr(current_bucket_count),
            min_bucket_count=MIN_BUCKET_COUNT,
            max_bucket_count=MAX_BUCKET_COUNT,
        )

    supplied = int(bucket_count is not None) + int(expected_cardinality is not None)
    if supplied != 1:
        raise GrafxIndexError(
            "Rehash sizing requires exactly one of bucket_count or expected_cardinality.",
            field="sizing",
            bucket_count=repr(bucket_count),
            expected_cardinality=repr(expected_cardinality),
        )

    resolved, retained_expected = custom_index_sizing(
        bucket_count=bucket_count,
        expected_cardinality=expected_cardinality,
    )
    if resolved <= current_bucket_count:
        raise GrafxIndexError(
            "Rehash is growth-only: the resolved bucket count must be strictly greater than "
            f"the active generation's {current_bucket_count}; got {resolved}.",
            field="bucket_count",
            value=resolved,
            current_bucket_count=current_bucket_count,
            expected_cardinality=retained_expected,
        )
    return resolved, retained_expected


def identity_index_sizing(
    visible_rows: object,
    *,
    expected_cardinality: object | None = None,
) -> tuple[int, int]:
    """Return ``(expected_cardinality, bucket_count)`` for an automatic identity index.

    The P2-ID v1 policy reserves one growth interval by doubling the rows visible in the fenced
    build view, with the established 4096-entry floor.  A configured expected cardinality is an
    additional floor for a load whose future size is known; it never replaces the fenced count.
    Sixty-four expected entries share each eagerly allocated bucket and the directory rounds
    upward to a power of two.  A request beyond the finite eager-directory bound is refused
    rather than silently capped.
    """

    if isinstance(visible_rows, bool) or not isinstance(visible_rows, int):
        raise GrafxIndexError(
            "Automatic identity-index sizing needs an integer visible-row count; "
            f"got {type(visible_rows).__name__}.",
            field="visible_rows",
            value=repr(visible_rows),
        )
    if visible_rows < 0:
        raise GrafxIndexError(
            "Automatic identity-index sizing needs a non-negative visible-row count; "
            f"got {visible_rows}.",
            field="visible_rows",
            value=visible_rows,
        )

    expected = max(_AUTOMATIC_IDENTITY_MIN_EXPECTED, 2 * visible_rows)
    if expected_cardinality is not None:
        _hinted_bucket_count, hinted_expected = custom_index_sizing(
            expected_cardinality=expected_cardinality
        )
        assert hinted_expected is not None
        expected = max(expected, hinted_expected)
    if expected > MAX_EXPECTED_CARDINALITY:
        raise GrafxIndexError(
            "Automatic identity-index sizing exceeds the eager hash-directory limit: "
            f"the largest representable expected cardinality is {MAX_EXPECTED_CARDINALITY}; "
            f"{visible_rows} visible rows require {expected}.",
            field="visible_rows",
            value=visible_rows,
            expected_cardinality=expected,
            max_expected_cardinality=MAX_EXPECTED_CARDINALITY,
        )

    return expected, _bucket_count_for_expected(expected)


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


def record_id_key(record_id: object) -> bytes:
    """Return the versioned canonical key of one usable unsigned 64-bit ``RecordId``.

    ``ValueType.INT64`` is signed and therefore cannot encode half of the physical identity
    domain.  Identity indexes use this private format instead: one format byte followed by the
    little-endian unsigned value.  Zero and ``2**64 - 1`` are not usable row identities; the
    latter is the durable exhausted marker.
    """

    if isinstance(record_id, bool) or not isinstance(record_id, int):
        raise GrafxIndexError(
            "An identity index key needs an integer RecordId; "
            f"got {type(record_id).__name__}.",
            field="record_id",
            value=repr(record_id),
        )
    if not _FIRST_RECORD_ID <= record_id < _EXHAUSTED_RECORD_ID:
        raise GrafxIndexError(
            "An identity index key needs a RecordId from 1 up to but not including "
            f"{_EXHAUSTED_RECORD_ID}; got {record_id}.",
            field="record_id",
            value=record_id,
        )
    return _RECORD_ID.pack(RECORD_ID_KEY_FORMAT_VERSION, record_id)


def bucket_of(key: bytes, bucket_count: int) -> int:
    """Return the bucket a key belongs to, the same way in every process and on every platform."""
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise GrafxIndexError(
            f"An index key must be bytes; got {type(key).__name__}.",
            field="key",
            value=type(key).__name__,
        )
    return crc32c(bytes(key)) % validate_bucket_count(bucket_count)
