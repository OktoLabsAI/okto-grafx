"""The record format and its decoder (CONTRACT.md section 6.5, SPEC-M1 TR-4, FR-5).

TR-4 asks for a self-describing record and a versioned decoder with round-trip tests per version.
The round trip is parametrized over :data:`SUPPORTED_FORMAT_VERSIONS`, which is the closed set the
build declares, so adding a version without adding its round trip is impossible rather than
merely discouraged.

The other half of this file is the decoder's classification, in both directions. A field a caller
got wrong is a configuration error naming the field; a byte on disk that is wrong is damage, and
the one exception is a record from a LATER build, which is a version question and must never be
reported as corruption -- corruption sends an operator to quarantine, and the answer there is an
upgrade (A11-revised).
"""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.wal.codec import MAGIC_BYTES, FailureReason, decode_record
from okto_grafx.domain.wal.record import (
    CHECKSUM_LENGTH,
    HEADER_LENGTHS,
    MAX_DESCRIPTOR_BYTES,
    MAX_U16,
    MAX_U64,
    SUPPORTED_FORMAT_VERSIONS,
    WAL_FORMAT_VERSION,
    WAL_HEADER_LENGTH,
    WAL_LEGACY_FORMAT_VERSION,
    WAL_MAGIC,
    WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
    WAL_V2_FLAG_REQUIRED,
    WAL_V2_FLAG_SKIPPABLE,
    WalRecord,
    WalRecordType,
    header_length_of,
    is_known_record_type,
)
from okto_grafx.domain.wal.replay import ScanFailure

HEADER = struct.Struct("<IHHHHIQQQII")
"""The header layout of CONTRACT.md section 6.5, spelled out again so the test is independent."""


def _record(**overrides: object) -> WalRecord:
    """Return a record with sensible fields, overridden field by field."""
    fields: dict[str, object] = {
        "record_type": WalRecordType.COMMIT,
        "payload": b"payload-bytes",
        "descriptor": "hash-v1;partitions_per_table=64",
        "lsn": 42,
        "epoch": 3,
        "txn_id": 77,
        "flags": 0,
    }
    fields.update(overrides)
    return WalRecord(**fields)  # type: ignore[arg-type]


# --- the frozen shape ---------------------------------------------------------------------


def test_the_magic_and_the_header_length_are_the_frozen_ones() -> None:
    """CONTRACT.md section 6.5 fixes both numbers; a change here changes every stored log."""
    assert WAL_MAGIC == 0x5852474F
    assert WAL_HEADER_LENGTH == 48
    assert HEADER.size == 48
    assert MAGIC_BYTES == struct.pack("<I", 0x5852474F)
    assert CHECKSUM_LENGTH == 4


def test_every_record_type_carries_the_number_the_contract_gives_it() -> None:
    """The numeric codes are on disk, so they are frozen and spelled out one by one."""
    assert {member.name: int(member) for member in WalRecordType} == {
        "BEGIN": 1,
        "WRITE_PAGE": 2,
        "COMMIT": 3,
        "ABORT": 4,
        "CHECKPOINT": 5,
        "INDEX_WRITE": 6,
        "INDEX_RECONCILE": 7,
        "LEDGER_APPEND": 8,
        "CATALOG_WRITE": 9,
        "SPACE_DDL": 10,
        "VECTOR_WRITE": 11,
        "SEGMENT_HEADER": 12,
        "PAGE_ALLOC": 13,
    }


def test_each_header_field_sits_at_the_offset_the_contract_names() -> None:
    """A field that moves is a format change, so the offsets are asserted against the table."""
    raw = _record(flags=0x1234).encode()
    assert struct.unpack_from("<I", raw, 0)[0] == WAL_MAGIC
    assert struct.unpack_from("<H", raw, 4)[0] == WAL_LEGACY_FORMAT_VERSION
    assert struct.unpack_from("<H", raw, 6)[0] == int(WalRecordType.COMMIT)
    assert struct.unpack_from("<H", raw, 8)[0] == WAL_HEADER_LENGTH
    assert struct.unpack_from("<H", raw, 10)[0] == 0x1234
    assert struct.unpack_from("<I", raw, 12)[0] == len(raw)
    assert struct.unpack_from("<Q", raw, 16)[0] == 42
    assert struct.unpack_from("<Q", raw, 24)[0] == 3
    assert struct.unpack_from("<Q", raw, 32)[0] == 77
    assert struct.unpack_from("<I", raw, 40)[0] == len(
        "hash-v1;partitions_per_table=64"
    )
    assert struct.unpack_from("<I", raw, 44)[0] == len(b"payload-bytes")


def test_the_checksum_is_crc32c_over_everything_before_it() -> None:
    """The trailing four bytes are the CRC-32C of the record, and that is C1's function."""
    raw = _record().encode()
    assert struct.unpack_from("<I", raw, len(raw) - 4)[0] == crc32c(raw[:-4])


# --- round trip, per version (TR-4) --------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_FORMAT_VERSIONS)
def test_a_record_round_trips_in_every_supported_version(version: int) -> None:
    """TR-4 asks for a round trip per version, and this is the closed set of them."""
    overrides: dict[str, object] = {"format_version": version}
    if version == WAL_FORMAT_VERSION:
        overrides.update(
            record_type=WalRecordType.WRITE_PAGE,
            flags=WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
        )
    record = _record(**overrides)
    outcome = decode_record(record.encode())
    assert outcome.record == record
    assert outcome.consumed == record.encoded_length()
    assert outcome.checked is True
    assert header_length_of(version) == HEADER_LENGTHS[version]


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00", bytes(range(256)), b"\x1a" * 300, MAGIC_BYTES * 8],
    ids=[
        "empty",
        "one-zero",
        "every-byte",
        "windows-eof-byte",
        "payload-looks-like-a-header",
    ],
)
def test_a_payload_survives_whatever_bytes_it_holds(payload: bytes) -> None:
    """A payload is opaque: a run of the magic inside it must not confuse the decoder."""
    record = _record(payload=payload)
    outcome = decode_record(record.encode())
    assert outcome.record is not None
    assert outcome.record.payload == payload


def test_a_record_decodes_at_an_offset_inside_a_larger_buffer() -> None:
    """A segment is a run of records, so decoding starts wherever the caller says."""
    first = _record(lsn=1).encode()
    second = _record(lsn=2, payload=b"second").encode()
    outcome = decode_record(first + second, len(first))
    assert outcome.record is not None
    assert outcome.record.lsn == 2
    assert outcome.consumed == len(second)


def test_the_descriptor_round_trips_at_its_declared_ceiling() -> None:
    """Changing partitions_per_table changes only this string, so its full range must work."""
    descriptor = "p" * MAX_DESCRIPTOR_BYTES
    record = _record(descriptor=descriptor)
    outcome = decode_record(record.encode())
    assert outcome.record is not None
    assert outcome.record.descriptor == descriptor


# --- what a caller gets wrong is a configuration error ---------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_type", -1),
        ("record_type", MAX_U16 + 1),
        ("record_type", True),
        ("record_type", "commit"),
        ("format_version", MAX_U16 + 1),
        ("flags", MAX_U16 + 1),
        ("lsn", -1),
        ("lsn", MAX_U64 + 1),
        ("epoch", -5),
        ("txn_id", MAX_U64 + 1),
    ],
)
def test_a_field_outside_its_width_is_refused_by_name(
    field: str, value: object
) -> None:
    """A41: the refusal names the field, and no raw struct error reaches a public door."""
    with pytest.raises(GrafxConfigurationError) as caught:
        _record(**{field: value})
    assert caught.value.details["field"] == field
    assert caught.value.code == "configuration_error"


def test_a_payload_that_is_not_bytes_is_refused() -> None:
    """The payload is bytes; a string would silently encode under some codec or other."""
    with pytest.raises(GrafxConfigurationError) as caught:
        _record(payload="text")
    assert caught.value.details["field"] == "payload"


def test_a_descriptor_that_is_not_a_string_is_refused() -> None:
    """The descriptor is text, and the refusal says so rather than failing inside struct."""
    with pytest.raises(GrafxConfigurationError) as caught:
        _record(descriptor=b"bytes")
    assert caught.value.details["field"] == "descriptor"


def test_a_descriptor_past_the_ceiling_is_refused_with_its_length() -> None:
    """The ceiling exists so one record cannot carry an unbounded string."""
    with pytest.raises(GrafxConfigurationError) as caught:
        _record(descriptor="p" * (MAX_DESCRIPTOR_BYTES + 1))
    assert caught.value.details["value"] == MAX_DESCRIPTOR_BYTES + 1


def test_writing_a_record_type_this_build_does_not_know_is_refused() -> None:
    """Writing an unknown type would be this build inventing a meaning it does not have."""
    record = WalRecord(record_type=250, payload=b"x")
    assert record.is_known_type is False
    with pytest.raises(GrafxConfigurationError) as caught:
        record.encode()
    assert caught.value.details["field"] == "record_type"


def test_writing_a_format_version_this_build_does_not_write_is_refused() -> None:
    """A build writes exactly the versions it declares, and the refusal names the field.

    The message is asserted as well as the class, because the layout lookup is the ONE door that
    refuses this and a test that only names the class could not tell it from any other refusal
    that carries the same field (A62).
    """
    with pytest.raises(GrafxConfigurationError) as caught:
        _record(format_version=WAL_FORMAT_VERSION + 1).encode()
    assert caught.value.details["field"] == "format_version"
    assert "not one this build writes or reads" in caught.value.message


def test_header_length_of_refuses_a_version_it_has_no_layout_for() -> None:
    """The header length is looked up, never assumed, so an unknown version stops here."""
    with pytest.raises(GrafxConfigurationError) as caught:
        header_length_of(WAL_FORMAT_VERSION + 1)
    assert caught.value.details["field"] == "format_version"


def test_a_known_type_is_reported_as_known() -> None:
    """The predicate the encoder and the decoder share answers for the frozen set."""
    assert is_known_record_type(int(WalRecordType.SEGMENT_HEADER)) is True
    assert is_known_record_type(0) is False
    assert is_known_record_type(250) is False


# --- what the disk gets wrong is a decode failure --------------------------------------------


def test_a_buffer_too_short_for_a_header_is_a_truncated_tail() -> None:
    """The commonest crash signature: the record simply is not all there."""
    raw = _record().encode()[: WAL_HEADER_LENGTH - 1]
    outcome = decode_record(raw)
    assert outcome.reason is FailureReason.TRUNCATED_TAIL
    assert outcome.consumed == len(raw)
    assert outcome.record is None
    assert outcome.checked is False


def test_a_record_cut_short_after_its_header_is_a_truncated_tail() -> None:
    """The header is whole and says more bytes follow, and they do not."""
    raw = _record().encode()[:-3]
    outcome = decode_record(raw)
    assert outcome.reason is FailureReason.TRUNCATED_TAIL
    assert outcome.consumed == len(raw)


def test_an_empty_buffer_is_a_truncated_tail_and_never_a_loop() -> None:
    """Nothing at all is the boundary case a scanning loop meets at the end of every segment."""
    outcome = decode_record(b"")
    assert outcome.reason is FailureReason.TRUNCATED_TAIL
    assert outcome.consumed == 0


def test_bytes_that_do_not_start_a_record_advance_nothing() -> None:
    """A caller that cannot trust the length must resynchronise, never add a claimed length."""
    outcome = decode_record(bytes(WAL_HEADER_LENGTH + 8))
    assert outcome.reason is FailureReason.BAD_MAGIC
    assert outcome.consumed == 0
    assert outcome.checked is False


def test_a_version_above_this_build_is_a_version_question_not_damage() -> None:
    """Reporting a newer record as corrupt would send an operator to quarantine, not upgrade."""
    raw = bytearray(_record().encode())
    struct.pack_into("<H", raw, 4, WAL_FORMAT_VERSION + 1)
    outcome = decode_record(bytes(raw))
    assert outcome.reason is FailureReason.UNSUPPORTED_VERSION
    assert outcome.consumed == 0
    failure = ScanFailure(
        reason=outcome.reason,
        segment="wal/000000000001.wal",
        offset=0,
        length=len(raw),
        detail=outcome.detail,
    )
    error = failure.as_error()
    assert isinstance(error, GrafxSchemaVersionMismatch)
    assert error.code == "schema_version_mismatch"
    assert error.retryable is False


def test_a_total_length_that_does_not_add_up_is_a_bad_header() -> None:
    """The three length fields have to agree before a single payload byte is believed.

    The message is asserted, not only the class: two guards in this decoder refuse with
    ``bad_header`` and a test that names only the class cannot say which one answered (A62).
    """
    raw = bytearray(_record().encode())
    struct.pack_into("<I", raw, 12, 999)
    outcome = decode_record(bytes(raw))
    assert outcome.reason is FailureReason.BAD_HEADER
    assert "claims 999" in outcome.detail
    assert outcome.consumed == 0
    assert outcome.checked is False


def test_a_header_length_the_version_never_declared_is_refused() -> None:
    """A header length nobody declared moves where the descriptor begins.

    Every other field is made to agree with the false claim and the checksum is recomputed, so
    the length arithmetic passes and the bytes are genuinely intact. Only the guard that knows
    what length THIS version declares can refuse it -- and without that guard the record decodes
    into a different record than the one that was written, with its descriptor read out of the
    middle of its own header.
    """
    record = _record(descriptor="abcdefgh", payload=b"payload")
    raw = bytearray(record.encode())
    descriptor_length = len("abcdefgh")
    payload_length = len(b"payload")
    struct.pack_into("<H", raw, 8, WAL_HEADER_LENGTH - 8)
    struct.pack_into("<I", raw, 40, descriptor_length + 8)
    struct.pack_into("<I", raw, 44, payload_length)
    body = bytes(raw[:-4])
    raw[-4:] = struct.pack("<I", crc32c(body))
    outcome = decode_record(bytes(raw))
    assert outcome.record is None
    assert outcome.reason is FailureReason.BAD_HEADER
    assert f"declares a {WAL_HEADER_LENGTH} byte header" in outcome.detail
    assert outcome.consumed == 0


def test_a_declared_length_past_the_end_of_the_buffer_is_a_truncated_tail() -> None:
    """A length the buffer cannot satisfy is missing bytes, not a contradiction."""
    payload = b"x" * 32
    record = _record(payload=payload)
    raw = bytearray(record.encode())
    struct.pack_into("<I", raw, 44, len(payload) + 64)
    struct.pack_into("<I", raw, 12, len(raw) + 64)
    outcome = decode_record(bytes(raw))
    assert outcome.reason is FailureReason.TRUNCATED_TAIL


@pytest.mark.parametrize("position", [0, 20, 48, 55])
def test_flipping_any_byte_of_a_record_fails_its_checksum(position: int) -> None:
    """The checksum covers everything before it, so no field is outside its protection."""
    raw = bytearray(_record().encode())
    raw[position] ^= 0xFF
    outcome = decode_record(bytes(raw))
    assert outcome.record is None
    assert outcome.reason in {
        FailureReason.CHECKSUM_FAILURE,
        FailureReason.BAD_MAGIC,
        FailureReason.BAD_HEADER,
        FailureReason.UNSUPPORTED_VERSION,
    }


def test_a_checksum_that_does_not_match_says_so_and_advances_nothing() -> None:
    """A checksum failure is the one damage the format can prove, and it names both values."""
    raw = bytearray(_record().encode())
    raw[-1] ^= 0xFF
    outcome = decode_record(bytes(raw))
    assert outcome.reason is FailureReason.CHECKSUM_FAILURE
    assert outcome.consumed == 0
    assert outcome.checked is True
    assert "checksum" in outcome.detail


def test_a_descriptor_that_is_not_utf8_is_stepped_over_rather_than_read() -> None:
    """The checksum already proved the length, so the scan may pass it; it may not read it."""
    record = _record(descriptor="ok")
    raw = bytearray(record.encode())
    start = WAL_HEADER_LENGTH
    raw[start : start + 2] = b"\xff\xfe"
    body = bytes(raw[:-4])
    raw[-4:] = struct.pack("<I", crc32c(body))
    outcome = decode_record(bytes(raw))
    assert outcome.reason is FailureReason.UNREADABLE_DESCRIPTOR
    assert outcome.consumed == len(raw)
    assert outcome.checked is True


def _planted_record(*, descriptor: bytes, payload: bytes = b"", lsn: int = 1) -> bytes:
    """Return the bytes of a checksum-valid record, built without the record model's limits.

    The point of the format is that a record found on disk decodes on its own terms. Building
    one here by hand is the only way to plant a record the WRITE side would have refused.
    """
    total = WAL_HEADER_LENGTH + len(descriptor) + len(payload) + CHECKSUM_LENGTH
    body = (
        HEADER.pack(
            WAL_MAGIC,
            WAL_LEGACY_FORMAT_VERSION,
            int(WalRecordType.WRITE_PAGE),
            WAL_HEADER_LENGTH,
            0,
            total,
            lsn,
            1,
            1,
            len(descriptor),
            len(payload),
        )
        + descriptor
        + payload
    )
    return body + struct.pack("<I", crc32c(body))


def test_a_descriptor_longer_than_this_build_holds_is_reported_never_raised() -> None:
    """The decoder promises never to raise, and the record model has limits it must not honour.

    The write side refuses a descriptor past MAX_DESCRIPTOR_BYTES because a CALLER must not ask
    for one. Bytes already on disk are not a caller: this record's checksum proves it was
    written exactly as it stands. So the scan steps over it and says why. A decoder that raised
    here would take the whole log with it -- the manager could not finish opening, and
    recovery's own repair door sits behind that open.
    """
    raw = _planted_record(descriptor=b"d" * (MAX_DESCRIPTOR_BYTES + 1))
    outcome = decode_record(raw)
    assert outcome.record is None
    assert outcome.reason is FailureReason.UNREPRESENTABLE_RECORD
    assert outcome.consumed == len(raw)
    assert outcome.checked is True
    assert str(MAX_DESCRIPTOR_BYTES) in outcome.detail


def test_a_record_at_the_descriptor_ceiling_still_decodes() -> None:
    """The refusal is at the boundary and not before it, so the ceiling is pinned from both sides."""
    raw = _planted_record(descriptor=b"d" * MAX_DESCRIPTOR_BYTES)
    outcome = decode_record(raw)
    assert outcome.record is not None
    assert len(outcome.record.descriptor) == MAX_DESCRIPTOR_BYTES


def test_a_record_this_build_cannot_hold_is_damage_and_not_a_caller_error() -> None:
    """A11-revised in both directions: the write side is configuration, the disk side is damage."""
    with pytest.raises(GrafxConfigurationError):
        _record(descriptor="d" * (MAX_DESCRIPTOR_BYTES + 1))
    failure = ScanFailure(
        reason=FailureReason.UNREPRESENTABLE_RECORD,
        segment="wal/000000000001.wal",
        offset=0,
        length=64,
        detail="The record is outside what this build can hold.",
    )
    error = failure.as_error()
    assert isinstance(error, GrafxCorruptionDetected)
    assert error.code == "corruption_detected"


def test_an_unknown_record_type_still_decodes() -> None:
    """Forward compatibility: a later build's record is stepped over, not declared corrupt."""
    raw = bytearray(_record().encode())
    struct.pack_into("<H", raw, 6, 250)
    body = bytes(raw[:-4])
    raw[-4:] = struct.pack("<I", crc32c(body))
    outcome = decode_record(bytes(raw))
    assert outcome.record is not None
    assert outcome.record.record_type == 250
    assert outcome.record.is_known_type is False


def test_an_unknown_explicitly_skippable_v2_record_can_be_stepped_over() -> None:
    """Only an unknown v2 record with the exact SKIPPABLE flag is safe to skip."""
    raw = bytearray(_record().encode())
    struct.pack_into("<H", raw, 4, WAL_FORMAT_VERSION)
    struct.pack_into("<H", raw, 6, 250)
    struct.pack_into("<H", raw, 10, WAL_V2_FLAG_SKIPPABLE)
    body = bytes(raw[:-CHECKSUM_LENGTH])
    raw[-CHECKSUM_LENGTH:] = struct.pack("<I", crc32c(body))

    outcome = decode_record(bytes(raw))

    assert outcome.record is not None
    assert outcome.record.record_type == 250
    assert outcome.record.format_version == WAL_FORMAT_VERSION


@pytest.mark.parametrize(
    ("record_type", "flags"),
    [
        (250, 0),
        (250, WAL_V2_FLAG_REQUIRED),
        (int(WalRecordType.WRITE_PAGE), 0),
        (int(WalRecordType.COMMIT), 0),
    ],
)
def test_unsupported_required_v2_semantics_are_a_checked_upgrade_refusal(
    record_type: int,
    flags: int,
) -> None:
    raw = bytearray(_record().encode())
    struct.pack_into("<H", raw, 4, WAL_FORMAT_VERSION)
    struct.pack_into("<H", raw, 6, record_type)
    struct.pack_into("<H", raw, 10, flags)
    body = bytes(raw[:-CHECKSUM_LENGTH])
    raw[-CHECKSUM_LENGTH:] = struct.pack("<I", crc32c(body))

    outcome = decode_record(bytes(raw))

    assert outcome.record is None
    assert outcome.reason is FailureReason.UNSUPPORTED_REQUIRED_RECORD
    assert outcome.consumed == len(raw)
    assert outcome.checked is True
    failure = ScanFailure(
        reason=outcome.reason,
        segment="wal/000000000001.wal",
        offset=0,
        length=len(raw),
        detail=outcome.detail,
    )
    assert isinstance(failure.as_error(), GrafxSchemaVersionMismatch)


def test_the_decoder_refuses_a_non_buffer_without_raising() -> None:
    """Decoding never raises, so even a caller mistake comes back as an outcome."""
    outcome = decode_record("not bytes")  # type: ignore[arg-type]
    assert outcome.reason is FailureReason.BAD_HEADER
    assert outcome.consumed == 0


@pytest.mark.parametrize("offset", [-1, True, "0"])
def test_the_decoder_refuses_an_offset_that_is_not_one(offset: object) -> None:
    """A negative offset would silently read from the end of the buffer."""
    outcome = decode_record(_record().encode(), offset)  # type: ignore[arg-type]
    assert outcome.reason is FailureReason.BAD_HEADER
    assert outcome.consumed == 0


def test_damage_other_than_a_version_is_reported_as_corruption() -> None:
    """CONTRACT.md section 2: damaged bytes are corruption_detected, and it is not retryable."""
    failure = ScanFailure(
        reason=FailureReason.CHECKSUM_FAILURE,
        segment="wal/000000000007.wal",
        offset=128,
        length=64,
        expected_lsn=9,
        detail="The record checksum does not match.",
    )
    error = failure.as_error()
    assert isinstance(error, GrafxCorruptionDetected)
    assert error.code == "corruption_detected"
    assert error.retryable is False
    assert error.details["file"] == "wal/000000000007.wal"
    assert error.details["offset"] == 128
    assert error.details["expected_lsn"] == 9
    assert error.details["reason"] == "checksum_failure"


def test_with_lsn_and_with_descriptor_change_one_field_each() -> None:
    """The manager stamps both, so the two helpers must leave everything else alone."""
    record = _record(lsn=0, descriptor="")
    stamped = record.with_lsn(9).with_descriptor("d;p=8")
    assert stamped.lsn == 9
    assert stamped.descriptor == "d;p=8"
    assert stamped.payload == record.payload
    assert stamped.txn_id == record.txn_id
    assert record.lsn == 0 and record.descriptor == ""


def test_encoded_length_matches_what_encode_produces() -> None:
    """The manager sizes a batch before writing it, so the two must never disagree."""
    for payload in (b"", b"x", bytes(1000)):
        record = _record(payload=payload)
        assert record.encoded_length() == len(record.encode())
