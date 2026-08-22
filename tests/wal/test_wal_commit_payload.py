"""The canonical COMMIT payload (CONTRACT.md section 6.5, and the validator of section 8.5).

The optimistic validator reads these bytes to answer one question -- do two transactions touch a
common partition -- so the layout has to be exact and the encoding has to be canonical. Canonical
matters because the transaction manager holds SETS: without a fixed order the same transaction
would produce different bytes on different runs, and a crash run could not be compared with its
replay.

A short or self-contradicting payload came off a device, so it is damage. A caller passing the
wrong type is a configuration error. Both directions are asserted here.
"""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.wal.commit import (
    MAX_FILE_ID,
    MAX_PARTITION_INDEX,
    MAX_TABLE_ID,
    MAX_TOUCHED_PAGE_INDEX,
    PARTITION_INDEX_BITS,
    CommitPayload,
    PageTouch,
    partition_key,
    split_partition_key,
)


def test_a_partition_key_packs_the_table_above_the_partition() -> None:
    """The frozen rule is (table_id << 32) | partition_index, so both halves are asserted."""
    assert PARTITION_INDEX_BITS == 32
    assert partition_key(0, 0) == 0
    assert partition_key(1, 0) == 1 << 32
    assert partition_key(7, 5) == (7 << 32) | 5
    assert partition_key(MAX_TABLE_ID, MAX_PARTITION_INDEX) == 0xFFFFFFFFFFFFFFFF


@pytest.mark.parametrize(
    ("table_id", "partition_index"),
    [(0, 0), (1, 63), (12345, 7), (MAX_TABLE_ID, MAX_PARTITION_INDEX)],
)
def test_a_partition_key_splits_back_into_what_built_it(
    table_id: int, partition_index: int
) -> None:
    """A key that cannot be taken apart again cannot name the partition it locked."""
    assert split_partition_key(partition_key(table_id, partition_index)) == (
        table_id,
        partition_index,
    )


@pytest.mark.parametrize(
    ("field", "arguments"),
    [
        ("table_id", (MAX_TABLE_ID + 1, 0)),
        ("table_id", (-1, 0)),
        ("partition_index", (0, MAX_PARTITION_INDEX + 1)),
        ("partition_index", (0, True)),
    ],
)
def test_a_partition_key_refuses_a_component_that_does_not_fit(
    field: str, arguments: tuple[object, object]
) -> None:
    """An out-of-range half would silently overwrite the other one."""
    with pytest.raises(GrafxConfigurationError) as caught:
        partition_key(*arguments)  # type: ignore[arg-type]
    assert caught.value.details["field"] == field


def test_build_puts_both_partition_sets_in_canonical_order() -> None:
    """Two runs of the same transaction must produce the same bytes."""
    payload = CommitPayload.build(
        snapshot_lsn=11,
        read_partitions=[9, 1, 5, 1],
        write_partitions=(4, 4, 2),
        page_touches=[PageTouch(2, 9), PageTouch(1, 3), PageTouch(2, 9)],
    )
    assert payload.read_partitions == (1, 5, 9)
    assert payload.write_partitions == (2, 4)
    assert payload.page_touches == (PageTouch(1, 3), PageTouch(2, 9))
    assert payload.encode() == CommitPayload.build(
        snapshot_lsn=11,
        read_partitions=[1, 5, 9, 9],
        write_partitions=[2, 4],
        page_touches=[PageTouch(2, 9), PageTouch(1, 3)],
    ).encode()


def test_the_payload_round_trips_through_its_bytes() -> None:
    """What the validator reads has to be exactly what the committer wrote."""
    payload = CommitPayload.build(
        snapshot_lsn=1234,
        read_partitions=[partition_key(1, 2), partition_key(3, 4)],
        write_partitions=[partition_key(5, 6)],
        page_touches=[PageTouch(1, 0), PageTouch(1, 4096)],
    )
    assert CommitPayload.decode(payload.encode()) == payload
    assert payload.encoded_length() == len(payload.encode())


def test_an_empty_payload_still_round_trips() -> None:
    """A read-only-looking commit carries no partitions and must not be a special case."""
    payload = CommitPayload.build(snapshot_lsn=0)
    encoded = payload.encode()
    assert len(encoded) == payload.encoded_length()
    assert CommitPayload.decode(encoded) == payload


def test_the_layout_is_the_one_the_contract_writes_down() -> None:
    """The field order is on disk; spelling it out again keeps a silent reorder impossible."""
    payload = CommitPayload(
        snapshot_lsn=7,
        read_partitions=(11,),
        write_partitions=(22, 33),
        page_touches=(PageTouch(1, 2),),
    )
    encoded = payload.encode()
    assert struct.unpack_from("<Q", encoded, 0)[0] == 7
    assert struct.unpack_from("<I", encoded, 8)[0] == 1
    assert struct.unpack_from("<I", encoded, 12)[0] == 2
    assert struct.unpack_from("<Q", encoded, 16)[0] == 11
    assert struct.unpack_from("<Q", encoded, 24)[0] == 22
    assert struct.unpack_from("<Q", encoded, 32)[0] == 33
    assert struct.unpack_from("<I", encoded, 40)[0] == 1
    assert struct.unpack_from("<HI", encoded, 44) == (1, 2)


def test_the_decoder_keeps_an_order_another_build_chose() -> None:
    """Decoding is evidence about what was written, so it must not quietly canonicalise."""
    payload = CommitPayload(snapshot_lsn=1, read_partitions=(9, 1))
    assert CommitPayload.decode(payload.encode()).read_partitions == (9, 1)


@pytest.mark.parametrize("cut", [1, 8, 15, 20, 40])
def test_a_payload_that_stops_early_is_damage(cut: int) -> None:
    """These bytes came off a device, so the refusal is corruption and never a caller error."""
    payload = CommitPayload.build(
        snapshot_lsn=3, read_partitions=[1], write_partitions=[2], page_touches=[PageTouch(0, 0)]
    )
    with pytest.raises(GrafxCorruptionDetected) as caught:
        CommitPayload.decode(payload.encode()[:cut])
    assert caught.value.code == "corruption_detected"
    assert caught.value.details["reason"] == "short_commit_payload"


def test_a_payload_with_bytes_left_over_is_damage() -> None:
    """Trailing bytes mean the counts and the buffer disagree about what is here."""
    payload = CommitPayload.build(snapshot_lsn=3, read_partitions=[1])
    with pytest.raises(GrafxCorruptionDetected) as caught:
        CommitPayload.decode(payload.encode() + b"\x00\x00")
    assert caught.value.details["reason"] == "trailing_bytes"


def test_decoding_something_that_is_not_bytes_is_a_caller_error() -> None:
    """A11-revised: a caller's wrong argument must not manufacture an integrity incident."""
    with pytest.raises(GrafxConfigurationError) as caught:
        CommitPayload.decode("not bytes")  # type: ignore[arg-type]
    assert caught.value.details["field"] == "payload"


@pytest.mark.parametrize(
    ("field", "touch"),
    [("file_id", (MAX_FILE_ID + 1, 0)), ("page_index", (0, MAX_TOUCHED_PAGE_INDEX + 1))],
)
def test_a_page_touch_refuses_a_field_that_does_not_fit(
    field: str, touch: tuple[int, int]
) -> None:
    """The file id is a u16 and the page index a u32; neither may be packed by luck."""
    with pytest.raises(GrafxConfigurationError) as caught:
        PageTouch(*touch)
    assert caught.value.details["field"] == field


def test_a_payload_refuses_a_partition_key_that_is_not_an_integer() -> None:
    """A key that is not a number would fail inside struct, after other fields were packed."""
    with pytest.raises(GrafxConfigurationError) as caught:
        CommitPayload(snapshot_lsn=1, read_partitions=("nine",))  # type: ignore[arg-type]
    assert caught.value.details["field"] == "read_partitions"


def test_a_payload_refuses_partition_sets_that_are_not_tuples() -> None:
    """A list would still encode, and then two payloads built the same way would not compare."""
    with pytest.raises(GrafxConfigurationError) as caught:
        CommitPayload(snapshot_lsn=1, write_partitions=[1, 2])  # type: ignore[arg-type]
    assert caught.value.details["field"] == "write_partitions"


def test_a_payload_refuses_a_page_touch_of_the_wrong_shape() -> None:
    """Every touch has to be the frozen pair, or the encoder would pack whatever it found."""
    with pytest.raises(GrafxConfigurationError) as caught:
        CommitPayload(snapshot_lsn=1, page_touches=((1, 2),))  # type: ignore[arg-type]
    assert caught.value.details["field"] == "page_touches"


def test_a_payload_refuses_a_snapshot_that_is_not_a_sequence_number() -> None:
    """The snapshot is the number the validator compares against; a bool is not one."""
    with pytest.raises(GrafxConfigurationError) as caught:
        CommitPayload(snapshot_lsn=True)
    assert caught.value.details["field"] == "snapshot_lsn"


def test_two_disjoint_commits_share_no_partition_key() -> None:
    """BR-6 in the shape the validator sees it: disjoint sets intersect in nothing."""
    first = CommitPayload.build(
        snapshot_lsn=1, write_partitions=[partition_key(1, index) for index in range(8)]
    )
    second = CommitPayload.build(
        snapshot_lsn=1, write_partitions=[partition_key(1, index) for index in range(8, 16)]
    )
    assert set(first.write_partitions) & set(second.write_partitions) == set()
    overlapping = CommitPayload.build(snapshot_lsn=1, read_partitions=[partition_key(1, 7)])
    assert set(first.write_partitions) & set(overlapping.read_partitions) != set()
