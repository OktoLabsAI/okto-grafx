"""The control-record formats: fixed layout, checksummed, and never readable half-updated.

C6 (recovery) and C13 (bench) read these files, so the layout is asserted here byte by byte
rather than only through the coordinator that writes it.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from conftest import CoordinatorFactory

from okto_grafx.adapters.coordination_local import (
    LEASE_FORMAT_VERSION,
    LEASE_MAGIC,
    READER_FORMAT_VERSION,
    READER_MAGIC,
    LeaseRecord,
    ReaderRecord,
    decode_lease_record,
    decode_reader_record,
    encode_lease_record,
    encode_reader_record,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)

LEASE = LeaseRecord(
    owner_id="p4242-abcdef012345",
    epoch=7,
    heartbeat_seq=19,
    ttl_seconds=5.0,
    wall_stamp=1_700_000_000.5,
    held=True,
    superseded_epoch=6,
)

READER = ReaderRecord(
    reader_id="p4242-abcdef012345-r0003",
    snapshot_lsn=12_345,
    heartbeat_seq=8,
    wall_stamp=1_700_000_000.5,
    active=True,
)


def test_a_lease_record_round_trips() -> None:
    assert decode_lease_record(encode_lease_record(LEASE)) == LEASE


def test_a_released_lease_record_round_trips() -> None:
    released = LeaseRecord(
        owner_id=LEASE.owner_id,
        epoch=LEASE.epoch,
        heartbeat_seq=LEASE.heartbeat_seq + 1,
        ttl_seconds=LEASE.ttl_seconds,
        wall_stamp=LEASE.wall_stamp,
        held=False,
        superseded_epoch=LEASE.superseded_epoch,
    )
    assert decode_lease_record(encode_lease_record(released)) == released


def test_a_reader_record_round_trips() -> None:
    assert decode_reader_record(encode_reader_record(READER)) == READER


def test_the_lease_layout_is_the_documented_one() -> None:
    raw = encode_lease_record(LEASE)
    owner = LEASE.owner_id.encode("ascii")
    assert raw[:8] == LEASE_MAGIC == b"OKTOLEAS"
    header = struct.unpack_from("<8sHHIQQddQII", raw, 0)
    assert header[1] == LEASE_FORMAT_VERSION == 1
    assert header[2] == 1  # flags: bit zero means held
    assert header[3] == len(owner)
    assert header[4] == LEASE.epoch
    assert header[5] == LEASE.heartbeat_seq
    assert header[6] == pytest.approx(LEASE.ttl_seconds)
    assert header[7] == pytest.approx(LEASE.wall_stamp)
    assert header[8] == LEASE.superseded_epoch
    assert header[9] == len(raw) == 64 + len(owner) + 4
    assert raw[64 : 64 + len(owner)] == owner
    assert struct.unpack_from("<I", raw, len(raw) - 4)[0] == zlib.crc32(raw[:-4]) & 0xFFFFFFFF


def test_the_reader_layout_is_the_documented_one() -> None:
    raw = encode_reader_record(READER)
    reader = READER.reader_id.encode("ascii")
    assert raw[:8] == READER_MAGIC == b"OKTORDER"
    header = struct.unpack_from("<8sHHIQQdII", raw, 0)
    assert header[1] == READER_FORMAT_VERSION == 1
    assert header[2] == 1
    assert header[3] == len(reader)
    assert header[4] == READER.snapshot_lsn
    assert header[5] == READER.heartbeat_seq
    assert header[7] == len(raw) == 48 + len(reader) + 4
    assert raw[48 : 48 + len(reader)] == reader
    assert struct.unpack_from("<I", raw, len(raw) - 4)[0] == zlib.crc32(raw[:-4]) & 0xFFFFFFFF


@pytest.mark.parametrize("position", [0, 8, 16, 24, 40, 64])
def test_a_flipped_bit_anywhere_fails_the_checksum(position: int) -> None:
    raw = bytearray(encode_lease_record(LEASE))
    raw[position] ^= 0xFF
    with pytest.raises((GrafxCorruptionDetected, GrafxSchemaVersionMismatch)):
        decode_lease_record(bytes(raw))


def test_a_truncated_record_is_refused_rather_than_guessed() -> None:
    raw = encode_lease_record(LEASE)
    for length in (0, 1, 32, 63, len(raw) - 1):
        with pytest.raises(GrafxCorruptionDetected):
            decode_lease_record(raw[:length])


def test_trailing_bytes_are_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        decode_lease_record(encode_lease_record(LEASE) + b"\x00")


def test_a_foreign_magic_is_refused() -> None:
    raw = bytearray(encode_lease_record(LEASE))
    raw[0:8] = b"NOTOURS!"
    with pytest.raises(GrafxCorruptionDetected):
        decode_lease_record(bytes(raw))
    reader = bytearray(encode_reader_record(READER))
    reader[0:8] = b"NOTOURS!"
    with pytest.raises(GrafxCorruptionDetected):
        decode_reader_record(bytes(reader))


def test_a_newer_format_version_is_a_typed_refusal_not_a_guess() -> None:
    raw = bytearray(encode_lease_record(LEASE))
    struct.pack_into("<H", raw, 8, LEASE_FORMAT_VERSION + 1)
    struct.pack_into("<I", raw, len(raw) - 4, zlib.crc32(bytes(raw[:-4])) & 0xFFFFFFFF)
    with pytest.raises(GrafxSchemaVersionMismatch):
        decode_lease_record(bytes(raw))


def test_an_identifier_that_is_unsafe_as_a_file_name_is_refused() -> None:
    for owner in ("With/Slash", "UPPER", "", "with space", "a" * 200, "tab\tstop"):
        with pytest.raises(GrafxConfigurationError):
            encode_lease_record(
                LeaseRecord(
                    owner_id=owner,
                    epoch=1,
                    heartbeat_seq=1,
                    ttl_seconds=5.0,
                    wall_stamp=0.0,
                    held=True,
                    superseded_epoch=0,
                )
            )


def test_the_liveness_key_is_what_an_observer_samples() -> None:
    assert LEASE.liveness_key() == (LEASE.owner_id, LEASE.epoch, LEASE.heartbeat_seq)
    assert READER.liveness_key() == (READER.reader_id, READER.heartbeat_seq)


def test_the_wall_stamp_is_not_part_of_the_liveness_key() -> None:
    # The stamp is diagnostic only (FR-7): two records that differ only in it describe the same
    # state of progress, and an observer must not read one as evidence about the other.
    moved = LeaseRecord(
        owner_id=LEASE.owner_id,
        epoch=LEASE.epoch,
        heartbeat_seq=LEASE.heartbeat_seq,
        ttl_seconds=LEASE.ttl_seconds,
        wall_stamp=LEASE.wall_stamp - 3_600.0,
        held=LEASE.held,
        superseded_epoch=LEASE.superseded_epoch,
    )
    assert moved.liveness_key() == LEASE.liveness_key()
    assert moved != LEASE


def test_a_record_ending_in_the_dos_end_of_file_byte_survives_the_device(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """A checksum whose last byte is 0x1A must not cost the record a byte on Windows.

    The C runtime opens a file in text mode unless O_BINARY is given, and committing a text-mode
    handle truncates the file at a trailing 0x1A. One record in every 256 ends that way by pure
    arithmetic, so a device that forgets the flag loses a lease at a predictable rate. This walks
    the heartbeat until such a record is published and proves it reads back whole.
    """
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    target = database_root / "control" / "writer.lease"
    seen_end_of_file_byte = False
    for _renewal in range(64):
        lease = coordinator.renew_lease(lease)
        raw = target.read_bytes()
        record = decode_lease_record(raw)
        assert record.heartbeat_seq == lease.heartbeat_seq
        assert len(raw) == 64 + len(record.owner_id) + 4
        seen_end_of_file_byte = seen_end_of_file_byte or raw[-1] == 0x1A
    assert seen_end_of_file_byte, "the walk never produced the byte this test is about"
