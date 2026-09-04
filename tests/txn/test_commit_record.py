"""What C5 owns around a commit record: file numbers, the page-write payload, the commit state.

The COMMIT payload layout itself is C4's (``okto_grafx.domain.wal.commit``) and is tested there;
what is checked here is the part this component adds and the part it depends on being canonical.
"""

from __future__ import annotations

import struct
from random import Random

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.page.layout import MAX_PAGE_SIZE
from okto_grafx.domain.txn import (
    CATALOG_FILE_ID,
    COMMIT_STATE_SIZE,
    FIRST_DERIVED_FILE_ID,
    HEAP_FILE_ID,
    MAX_FILE_ID,
    CommitPayload,
    CommitState,
    FileIdMap,
    PageWrite,
    decode_page_write,
    decode_page_write_location,
    encode_page_write,
    encode_page_write_record,
)
from okto_grafx.domain.wal import (
    WAL_FORMAT_VERSION,
    WAL_LEGACY_FORMAT_VERSION,
    WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
    WAL_V2_FLAG_REQUIRED,
)


# --- the file numbers a page touch carries ------------------------------------------------------


def test_the_two_well_known_files_have_fixed_numbers() -> None:
    mapping = FileIdMap()
    assert mapping.id_of("heap.dat") == HEAP_FILE_ID
    assert mapping.id_of("catalog.dat") == CATALOG_FILE_ID


def test_another_file_gets_a_stable_number_above_the_well_known_ones() -> None:
    """Two processes reading one log must agree without consulting a table being recovered."""
    mapping = FileIdMap()
    first = mapping.id_of("index/by_name.idx")
    assert FIRST_DERIVED_FILE_ID <= first <= MAX_FILE_ID
    assert mapping.id_of("index/by_name.idx") == first
    assert FileIdMap().id_of("index/by_name.idx") == first


def test_every_derived_number_fits_the_field_the_payload_stores_it_in() -> None:
    mapping = FileIdMap()
    numbers = {mapping.id_of(f"index/{index:04d}.idx") for index in range(500)}
    assert all(FIRST_DERIVED_FILE_ID <= number <= MAX_FILE_ID for number in numbers)


def test_a_store_that_uses_other_names_moves_the_well_known_numbers_with_it() -> None:
    mapping = FileIdMap(heap_file="rows.dat", catalog_file="schema.dat")
    assert mapping.id_of("rows.dat") == HEAP_FILE_ID
    assert mapping.id_of("heap.dat") >= FIRST_DERIVED_FILE_ID


def test_a_page_touch_for_an_unnamed_file_is_a_caller_mistake() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        FileIdMap().id_of("")
    assert raised.value.details["field"] == "file"


# --- the payload this component builds ----------------------------------------------------------


def test_the_payload_this_component_builds_is_canonical() -> None:
    """A set has no order, so the record must impose one or two runs differ (determinism)."""
    candidates = [7, 4_294_967_299, 8_589_934_647, 3, 12_884_901_890, 19]
    assert list(set(candidates)) != sorted(candidates), (
        "this bench needs a set whose own iteration order is not ascending"
    )
    payload = CommitPayload.build(snapshot_lsn=1, write_partitions=set(candidates))
    assert list(payload.write_partitions) == sorted(candidates)
    assert CommitPayload.decode(payload.encode()) == payload


def test_the_same_partitions_in_a_different_order_encode_to_the_same_bytes() -> None:
    forward = CommitPayload.build(snapshot_lsn=3, write_partitions=[1, 2, 3, 4, 5])
    backward = CommitPayload.build(snapshot_lsn=3, write_partitions=[5, 4, 3, 2, 1])
    assert forward.encode() == backward.encode()


# --- the page-write payload ---------------------------------------------------------------------


def test_a_page_write_payload_names_its_own_file() -> None:
    """TR-4: a replay must be able to redo the record with nothing but the record."""
    payload = encode_page_write("index/by_name.idx", 12, b"page bytes")
    assert decode_page_write(payload) == PageWrite(
        file="index/by_name.idx", page_index=12, image=b"page bytes"
    )


def test_a_page_write_payload_round_trips_an_empty_image() -> None:
    assert decode_page_write(encode_page_write("heap.dat", 0, b"")).image == b""


def test_a_compressible_page_uses_bounded_wal_v2_and_round_trips() -> None:
    image = bytes(8192)

    encoded = encode_page_write_record("heap.dat", 17, image, compress=True)

    assert encoded.format_version == WAL_FORMAT_VERSION
    assert encoded.flags == WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1
    assert encoded.compressed is True
    assert len(encoded.payload) < len(encode_page_write("heap.dat", 17, image))
    assert decode_page_write(
        encoded.payload,
        format_version=encoded.format_version,
        flags=encoded.flags,
    ) == PageWrite(file="heap.dat", page_index=17, image=image)


def test_an_incompressible_page_falls_back_to_the_exact_legacy_grammar() -> None:
    image = Random(731).randbytes(8192)

    encoded = encode_page_write_record("heap.dat", 9, image, compress=True)

    assert encoded.format_version == WAL_LEGACY_FORMAT_VERSION
    assert encoded.flags == 0
    assert encoded.compressed is False
    assert encoded.payload == encode_page_write("heap.dat", 9, image)


def test_the_cleartext_target_is_available_without_inflating_the_page() -> None:
    encoded = encode_page_write_record("catalog.dat", 3, bytes(8192), compress=True)
    damaged_body = encoded.payload[:-4] + b"xxxx"

    assert decode_page_write_location(damaged_body).file == "catalog.dat"
    assert decode_page_write_location(damaged_body).page_index == 3
    with pytest.raises(GrafxCorruptionDetected):
        decode_page_write(
            damaged_body,
            format_version=encoded.format_version,
            flags=encoded.flags,
        )


def test_wal_v2_page_flags_are_required_and_closed() -> None:
    encoded = encode_page_write_record("heap.dat", 1, bytes(8192), compress=True)

    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        decode_page_write(
            encoded.payload,
            format_version=encoded.format_version,
            flags=WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
        )

    assert raised.value.details["field"] == "flags"


@pytest.mark.parametrize("damage", ["oversize", "undersize", "trailing"])
def test_compressed_page_inflate_is_bounded_and_exact(damage: str) -> None:
    encoded = encode_page_write_record("heap.dat", 1, bytes(8192), compress=True)
    raw = bytearray(encoded.payload)
    length_offset = 2 + len("heap.dat".encode("utf-8")) + 4
    if damage == "oversize":
        struct.pack_into("<I", raw, length_offset, MAX_PAGE_SIZE + 1)
    elif damage == "undersize":
        struct.pack_into("<I", raw, length_offset, 16)
    else:
        raw.extend(b"trailing")

    with pytest.raises(GrafxCorruptionDetected):
        decode_page_write(
            bytes(raw),
            format_version=encoded.format_version,
            flags=encoded.flags,
        )


def test_a_page_write_payload_that_is_truncated_is_refused() -> None:
    payload = encode_page_write("heap.dat", 3, b"abc")
    with pytest.raises(GrafxCorruptionDetected):
        decode_page_write(payload[:4])


def test_a_page_write_payload_with_a_name_length_past_the_end_is_refused() -> None:
    raw = struct.pack("<H", 400) + b"heap.dat"
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_page_write(raw)
    assert raised.value.details["declared"] == 400


def test_a_page_write_payload_whose_name_is_not_utf8_is_refused() -> None:
    raw = struct.pack("<H", 2) + b"\xff\xfe" + struct.pack("<I", 1)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_page_write(raw)
    assert raised.value.details["field"] == "file"


def test_a_page_write_for_an_unnamed_file_is_a_caller_mistake() -> None:
    """A caller's bad argument is never corruption_detected (amendment A11-revised)."""
    with pytest.raises(GrafxConfigurationError) as raised:
        encode_page_write("", 1, b"x")
    assert raised.value.details["field"] == "file"


def test_a_page_index_the_payload_cannot_store_is_a_caller_mistake() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        encode_page_write("heap.dat", 1 << 32, b"x")
    assert raised.value.details["field"] == "page_index"


def test_a_page_image_that_is_not_bytes_is_a_caller_mistake() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        encode_page_write("heap.dat", 1, "not bytes")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "image"


# --- the published commit state -------------------------------------------------------------------


def test_the_commit_state_round_trips_with_its_checksum() -> None:
    state = CommitState(last_committed_lsn=9, last_csn=9, checkpoint_lsn=4)
    raw = state.encode()
    assert len(raw) == COMMIT_STATE_SIZE
    assert CommitState.decode(raw) == state


def test_a_commit_state_with_a_flipped_byte_is_refused() -> None:
    raw = bytearray(
        CommitState(last_committed_lsn=9, last_csn=9, checkpoint_lsn=4).encode()
    )
    raw[10] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as raised:
        CommitState.decode(bytes(raw))
    assert raised.value.details["field"] == "checksum"


def test_a_commit_state_without_its_magic_is_refused() -> None:
    raw = bytearray(CommitState().encode())
    raw[0:4] = b"XXXX"
    # The checksum still has to be right, or the magic guard would never be the one that answers.
    from okto_grafx.domain.page import crc32c

    body = bytes(raw[: COMMIT_STATE_SIZE - 4])
    raw[COMMIT_STATE_SIZE - 4 :] = struct.pack("<I", crc32c(body))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        CommitState.decode(bytes(raw))
    assert raised.value.details["field"] == "magic"


def test_a_commit_state_of_a_newer_format_is_a_version_mismatch_not_damage() -> None:
    """Intact bytes from a newer build are not corruption (amendment A11-revised)."""
    from okto_grafx.domain.page import crc32c

    body = bytearray(CommitState().encode()[:-4])
    struct.pack_into("<H", body, 4, 99)
    raw = bytes(body) + struct.pack("<I", crc32c(bytes(body)))
    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        CommitState.decode(raw)
    assert raised.value.details["value"] == 99


def test_a_commit_state_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        CommitState.decode(CommitState().encode()[:-1])
    assert raised.value.details["field"] == "length"


def test_a_commit_state_that_is_not_bytes_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        CommitState.decode("not bytes")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "payload"


def test_a_commit_state_number_that_cannot_be_stored_is_a_caller_mistake() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        CommitState(last_committed_lsn=1 << 64)
    assert raised.value.details["field"] == "last_committed_lsn"


def test_an_empty_commit_state_is_the_one_a_database_starts_from() -> None:
    state = CommitState()
    assert (state.last_committed_lsn, state.last_csn, state.checkpoint_lsn) == (0, 0, 0)
    assert CommitState.decode(state.encode()) == state
