"""Conflict granularity: hash partitions of a table (SPEC-M1 FR-4, decision SD-1).

Optimistic validation compares SETS, so the unit it compares has to be a number that two
processes derive identically from the same key. That rules out Python's own ``hash`` -- it is
randomised per interpreter for bytes and strings -- and it is why the bucket comes from CRC-32C,
which is already in the tree for the page and log formats and is a pure function of the bytes.

The BUCKET is what this module owns. The KEY that names a partition of a table --
``(table_id << 32) | partition_index`` -- belongs to the frozen COMMIT payload of CONTRACT.md
section 6.5, so ``partition_key`` and ``split_partition_key`` are imported from
``okto_grafx.domain.wal.commit`` rather than written a second time here (amendment A24).

``partitions_per_table`` is calibration, not format: the number travels in the descriptor of the
record that used it (SD-1), so changing it needs no migration. Its upper bound belongs to
``DatabaseConfig`` and is deliberately not repeated here either.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.wal.commit import (
    MAX_PARTITION_INDEX,
    MAX_TABLE_ID,
    PARTITION_INDEX_BITS,
    partition_key,
    split_partition_key,
)

__all__ = [
    "PAGE_PARTITION_TABLE_ID",
    "MAX_PARTITION_INDEX",
    "MAX_TABLE_ID",
    "PARTITION_INDEX_BITS",
    "page_partition",
    "partition_index_of",
    "partition_key",
    "partition_of",
    "split_partition_key",
    "validate_partitions_per_table",
]


def validate_partitions_per_table(value: int) -> int:
    """Return the partition count after refusing one no hash could be taken modulo.

    Only the lower bound is checked here. The upper bound is declared once, by
    ``DatabaseConfig`` in the composition root, and a second declaration of it in the domain is
    exactly the two-validators-one-name failure amendment A24 forbids.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"partitions_per_table must be an integer; got {type(value).__name__}.",
            field="partitions_per_table",
            value=repr(value),
        )
    if value < 1:
        raise GrafxConfigurationError(
            f"partitions_per_table must be at least 1; got {value}.",
            field="partitions_per_table",
            value=value,
        )
    return value


def partition_index_of(key: bytes, partitions_per_table: int) -> int:
    """Return the bucket a key falls into, from 0 to partitions_per_table - 1.

    The bucket is ``crc32c(key) % partitions_per_table``. Two processes computing this from the
    same bytes always get the same answer, which is the property optimistic validation rests on:
    a bucket that differed between processes would let two writers touch one row and both commit.
    """
    partitions = validate_partitions_per_table(partitions_per_table)
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"A partition key must be bytes; got {type(key).__name__}.",
            field="key",
            value=type(key).__name__,
        )
    return crc32c(bytes(key)) % partitions


def partition_of(table_id: int, key: bytes, partitions_per_table: int) -> int:
    """Return the partition key that a row of this table and this key belongs to."""
    return partition_key(table_id, partition_index_of(key, partitions_per_table))


PAGE_PARTITION_TABLE_ID: int = MAX_TABLE_ID
"""The table id reserved for partitions that name a PAGE rather than a row key.

A commit that writes a whole page image conflicts with any other commit that writes the same
page, whatever rows either of them thought it was touching: the second image replaces the first
and the first commit's work is gone, acknowledged and durable and lost. That is a real conflict
and it needs a name in the same u64 namespace the row partitions use, so the frozen COMMIT
payload of CONTRACT.md section 6.5 carries it unchanged and any participant reading the log can
compare against it.

The id is the largest a table can have. Catalog table ids are handed out from one upwards, so
reaching it takes four billion tables; a database that did would share this namespace and see
extra conflicts, never a missed one.
"""


def page_partition(file: str, page_index: int) -> int:
    """Return the partition key that names one page of one file.

    The index is a digest of the file name and the page number rather than the two packed
    together, because a partition index is 32 bits and a file name plus a page number is not.
    A digest collision therefore makes two different pages look like one, which costs a
    RETRYABLE refusal of a commit that would have been legal -- never a conflict that goes
    unnoticed. That direction is chosen deliberately: BR-6 pays for a false refusal with one
    retry, and the alternative pays for a missed one with a lost commit.
    """
    if not isinstance(file, str) or not file:
        raise GrafxConfigurationError(
            "A page partition must name a non-empty file.", field="file", value=repr(file)
        )
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise GrafxConfigurationError(
            f"A page partition needs a page index of zero or more; got {page_index!r}.",
            field="page_index",
            value=repr(page_index),
        )
    stamp = file.encode("utf-8") + (page_index & 0xFFFFFFFF).to_bytes(4, "little")
    return partition_key(PAGE_PARTITION_TABLE_ID, crc32c(stamp))
