"""Conflict granularity (SPEC-M1 FR-4, decision SD-1).

The property that matters is not which bucket a key lands in, it is that two PROCESSES land it
in the same one. A bucket derived from Python's own ``hash`` would not, which is why the last
test here spends a subprocess to prove the derivation does not move with the interpreter's hash
seed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.txn import (
    MAX_TABLE_ID,
    PARTITION_INDEX_BITS,
    partition_index_of,
    partition_key,
    partition_of,
    split_partition_key,
    validate_partitions_per_table,
)

PROJECT_SOURCE: str = str(Path(__file__).resolve().parents[2] / "src")


def test_a_partition_key_carries_the_table_in_its_upper_half() -> None:
    """CONTRACT.md section 6.5: the key is ``(table_id << 32) | partition_index``."""
    assert partition_key(7, 3) == (7 << PARTITION_INDEX_BITS) | 3
    assert split_partition_key(partition_key(7, 3)) == (7, 3)


def test_two_tables_never_share_a_partition_key() -> None:
    """Without the table id in the key, disjoint tables would look like the same partition."""
    assert partition_key(1, 5) != partition_key(2, 5)


def test_a_bucket_is_inside_the_configured_range() -> None:
    buckets = {partition_index_of(bytes([value]), 8) for value in range(256)}
    assert buckets <= set(range(8))
    assert len(buckets) > 1


def test_the_same_key_always_lands_in_the_same_bucket() -> None:
    first = [partition_index_of(b"row-1", 64) for _ in range(20)]
    assert len(set(first)) == 1


def test_one_partition_puts_everything_together() -> None:
    """The degenerate calibration still has to work: every key is partition zero."""
    assert {partition_index_of(bytes([value]), 1) for value in range(64)} == {0}


@pytest.mark.parametrize("value", [0, -1, "8", 8.0, None, True])
def test_an_unusable_partition_count_is_refused(value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        validate_partitions_per_table(value)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "partitions_per_table"


def test_a_table_id_wider_than_the_key_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        partition_key(MAX_TABLE_ID + 1, 0)
    assert raised.value.details["field"] == "table_id"


def test_a_partition_index_wider_than_the_key_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        partition_key(1, 1 << 32)
    assert raised.value.details["field"] == "partition_index"


def test_a_key_that_is_not_bytes_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        partition_index_of("row-1", 8)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "key"


def test_a_bytearray_key_answers_like_the_bytes_it_holds() -> None:
    assert partition_index_of(bytearray(b"row-1"), 8) == partition_index_of(b"row-1", 8)


def test_partition_of_composes_the_bucket_and_the_table() -> None:
    assert partition_of(4, b"row-1", 16) == partition_key(4, partition_index_of(b"row-1", 16))


@pytest.mark.multiprocess
def test_the_bucket_is_the_same_in_a_process_with_a_different_hash_seed() -> None:
    """FR-4 rests on two processes agreeing, so the derivation must not read the hash seed.

    ``PYTHONHASHSEED`` randomises ``hash(bytes)`` per interpreter. A bucket built on it would
    differ between the two writers of AC-1 and AC-2, and every conflict decision with it.
    """
    program = (
        "import sys; sys.path.insert(0, %r)\n"
        "from okto_grafx.domain.txn import partition_of\n"
        "print(','.join(str(partition_of(3, bytes([n]), 64)) for n in range(16)))\n"
    ) % PROJECT_SOURCE
    here = ",".join(str(partition_of(3, bytes([n]), 64)) for n in range(16))
    seen = set()
    for seed in ("0", "1", "12345"):
        finished = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PYTHONHASHSEED": seed, "SYSTEMROOT": _system_root(), "PATH": _path()},
        )
        assert finished.returncode == 0, finished.stderr
        seen.add(finished.stdout.strip())
    assert seen == {here}


def _system_root() -> str:
    """Return the value Windows needs in a scrubbed environment, empty elsewhere."""
    import os

    return os.environ.get("SYSTEMROOT", "")


def _path() -> str:
    """Return the search path, which a spawned interpreter still needs."""
    import os

    return os.environ.get("PATH", "")
