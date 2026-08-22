"""The frozen ledger entry format of CONTRACT.md section 6.6, and its text form (TR-5)."""

from __future__ import annotations

import hashlib
import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ledger.entry import (
    DIGEST_LENGTH,
    LEDGER_ENTRY_HEADER_LENGTH,
    LEDGER_FORMAT_VERSION,
    LEDGER_MAGIC,
    MAX_LEDGER_PAYLOAD_BYTES,
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
    digest_of,
)
from okto_grafx.domain.ledger.payload import LedgerPayload, decode_payload, encode_payload
from okto_grafx.domain.ledger.textform import decode_fields, encode_fields
from okto_grafx.domain.page.checksum import crc32c


def _entry(**overrides: object) -> LedgerEntry:
    """Return one ordinary entry with the fields a test usually wants to vary."""
    fields: dict[str, object] = {
        "entry_id": 7,
        "origin_class": LedgerOriginClass.FORENSIC,
        "reason": LedgerReason.CHECKSUM_FAILURE,
        "payload": b"the damaged bytes",
        "lsn_start": 40,
        "lsn_end": 41,
        "epoch": 3,
        "captured_at_wall": 1_700_000_000.5,
    }
    fields.update(overrides)
    return LedgerEntry(**fields)  # type: ignore[arg-type]


def test_the_header_is_the_ninety_two_bytes_section_six_six_freezes() -> None:
    assert LEDGER_ENTRY_HEADER_LENGTH == 92
    assert DIGEST_LENGTH == 32
    assert LEDGER_MAGIC == 0x4C475258
    assert LEDGER_FORMAT_VERSION == 1


def test_an_entry_round_trips_through_its_encoding() -> None:
    original = _entry()
    decoded = LedgerEntry.decode(original.encode())
    assert decoded == original


def test_the_encoded_entry_lays_its_fields_out_in_the_frozen_order() -> None:
    entry = _entry()
    raw = entry.encode()
    (
        magic,
        version,
        entry_type,
        total_length,
        entry_id,
        origin_class,
        reason,
        reserved,
        lsn_start,
        lsn_end,
        epoch,
        captured,
        digest,
        payload_len,
    ) = struct.unpack_from("<IHHIQBBHQQQd32sI", raw, 0)
    assert magic == LEDGER_MAGIC
    assert version == LEDGER_FORMAT_VERSION
    assert entry_type == int(LedgerEntryType.DISCARD)
    assert total_length == len(raw)
    assert entry_id == 7
    assert origin_class == int(LedgerOriginClass.FORENSIC)
    assert reason == int(LedgerReason.CHECKSUM_FAILURE)
    assert reserved == 0
    assert (lsn_start, lsn_end, epoch) == (40, 41, 3)
    assert captured == pytest.approx(1_700_000_000.5)
    assert digest == hashlib.sha256(b"the damaged bytes").digest()
    assert payload_len == len(b"the damaged bytes")


def test_the_trailing_checksum_covers_everything_before_it() -> None:
    raw = _entry().encode()
    stored = struct.unpack_from("<I", raw, len(raw) - 4)[0]
    assert stored == crc32c(raw[: len(raw) - 4])


def test_a_flipped_payload_byte_is_caught_by_the_checksum() -> None:
    raw = bytearray(_entry().encode())
    raw[LEDGER_ENTRY_HEADER_LENGTH] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "crc32c"


def test_a_rewritten_digest_is_caught_even_when_the_checksum_agrees() -> None:
    entry = _entry()
    raw = bytearray(entry.encode())
    raw[52:84] = bytes(32)
    raw[len(raw) - 4 :] = struct.pack("<I", crc32c(bytes(raw[: len(raw) - 4])))
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "digest"


def test_a_wrong_magic_is_refused_before_anything_else() -> None:
    raw = bytearray(_entry().encode())
    raw[0:4] = b"NOPE"
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "magic"


def test_a_newer_format_version_is_a_version_question_and_not_damage() -> None:
    raw = bytearray(_entry().encode())
    raw[4:6] = struct.pack("<H", LEDGER_FORMAT_VERSION + 1)
    with pytest.raises(GrafxSchemaVersionMismatch):
        LedgerEntry.decode(bytes(raw))


def test_a_declared_length_that_disagrees_with_the_payload_is_refused() -> None:
    raw = bytearray(_entry().encode())
    raw[8:12] = struct.pack("<I", len(raw) + 8)
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "total_length"


def test_a_truncated_entry_is_refused_rather_than_partially_read() -> None:
    raw = _entry().encode()
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(raw[:-1])
    # A62: the two length guards must be tellable apart. This one says the bytes ran out; the
    # one in test_a_declared_length_that_disagrees_with_the_payload_is_refused says the header
    # contradicts itself. Asserting only the exception class would let either answer for both.
    assert caught.value.details["field"] == "truncated_entry"


def test_a_header_shorter_than_the_frozen_length_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(LEDGER_ENTRY_HEADER_LENGTH - 1))
    assert caught.value.details["field"] == "header_len"


def test_an_unknown_origin_class_is_refused() -> None:
    entry = _entry()
    raw = bytearray(entry.encode())
    raw[20] = 9
    raw[len(raw) - 4 :] = struct.pack("<I", crc32c(bytes(raw[: len(raw) - 4])))
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "origin_class"


def test_an_unknown_reason_code_is_refused() -> None:
    entry = _entry()
    raw = bytearray(entry.encode())
    raw[21] = 200
    raw[len(raw) - 4 :] = struct.pack("<I", crc32c(bytes(raw[: len(raw) - 4])))
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "reason_code"


def test_an_unknown_entry_type_is_refused() -> None:
    entry = _entry()
    raw = bytearray(entry.encode())
    raw[6:8] = struct.pack("<H", 40)
    raw[len(raw) - 4 :] = struct.pack("<I", crc32c(bytes(raw[: len(raw) - 4])))
    with pytest.raises(GrafxCorruptionDetected) as caught:
        LedgerEntry.decode(bytes(raw))
    assert caught.value.details["field"] == "entry_type"


def test_a_caller_argument_of_the_wrong_shape_is_never_reported_as_corruption() -> None:
    with pytest.raises(GrafxConfigurationError):
        LedgerEntry(
            entry_id=-1,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.TRUNCATED_TAIL,
        )
    with pytest.raises(GrafxConfigurationError):
        LedgerEntry(
            entry_id=1,
            origin_class="forensic",  # type: ignore[arg-type]
            reason=LedgerReason.TRUNCATED_TAIL,
        )
    with pytest.raises(GrafxConfigurationError):
        LedgerEntry(
            entry_id=1,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.TRUNCATED_TAIL,
            payload="not bytes",  # type: ignore[arg-type]
        )


def test_a_span_that_runs_backwards_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        _entry(lsn_start=40, lsn_end=39)
    assert caught.value.details["field"] == "lsn_end"


def test_a_payload_past_the_ceiling_is_refused_before_it_is_packed() -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        LedgerEntry(
            entry_id=1,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.TRUNCATED_TAIL,
            payload=b"x",
        ).__class__(
            entry_id=1,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.TRUNCATED_TAIL,
            payload=bytes(MAX_LEDGER_PAYLOAD_BYTES + 1),
        )
    assert caught.value.details["field"] == "payload_len"


def test_the_digest_is_the_sha256_of_the_payload() -> None:
    assert digest_of(b"abc") == hashlib.sha256(b"abc").digest()
    assert _entry(payload=b"abc").digest == hashlib.sha256(b"abc").digest()


# --- the payload envelope ---------------------------------------------------------------------


def test_the_envelope_round_trips_provenance_and_bytes() -> None:
    payload = LedgerPayload(
        origin="wal/000000000001.wal",
        offset=128,
        length=64,
        expected_lsn=9,
        record_type=2,
        failure="checksum_failure",
        detail="The checksum did not match.",
        quarantine="00000000001700000000-wal_000000000001.wal-128-64",
        body=b"\x00" * 64,
    )
    decoded = decode_payload(encode_payload(payload))
    assert decoded == payload


def test_the_body_of_an_envelope_is_exactly_the_bytes_that_went_in() -> None:
    body = bytes(range(256))
    decoded = decode_payload(encode_payload(LedgerPayload(origin="heap.dat", body=body)))
    assert decoded.body == body


def test_an_envelope_whose_declared_header_runs_past_its_bytes_is_refused() -> None:
    raw = bytearray(encode_payload(LedgerPayload(origin="heap.dat", body=b"x")))
    raw[0:2] = struct.pack("<H", len(raw) + 10)
    with pytest.raises(GrafxCorruptionDetected):
        decode_payload(bytes(raw))


def test_an_envelope_must_name_its_origin() -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        LedgerPayload(origin="")
    assert caught.value.details["field"] == "origin"


# --- the text form ----------------------------------------------------------------------------


def test_the_text_form_is_a_function_of_the_mapping_and_not_of_its_order() -> None:
    first = encode_fields({"b": 2, "a": 1, "c": "x"})
    second = encode_fields({"c": "x", "a": 1, "b": 2})
    assert first == second == b'{"a":1,"b":2,"c":"x"}'


def test_the_text_form_round_trips_every_scalar_it_carries() -> None:
    fields = {"text": 'a "quoted" \\ back\nslash', "int": -12, "real": 1.5, "flag": True, "none": None}
    assert decode_fields(encode_fields(fields)) == fields


def test_the_text_form_stays_ascii_even_for_text_that_is_not() -> None:
    raw = encode_fields({"name": "café"})
    assert raw.decode("ascii") == '{"name":"caf\\u00e9"}'
    assert decode_fields(raw) == {"name": "café"}


def test_text_that_is_not_a_record_of_this_subset_is_damage_and_not_a_bad_argument() -> None:
    for broken in (b"", b"{", b'{"a"}', b'{"a":}', b'{"a":1,}}', b'[]', b'{"a":1} trailing'):
        with pytest.raises(GrafxCorruptionDetected):
            decode_fields(broken)


def test_a_repeated_field_is_refused_rather_than_silently_resolved() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        decode_fields(b'{"a":1,"a":2}')


def test_a_value_the_writer_cannot_carry_is_a_caller_error() -> None:
    with pytest.raises(GrafxConfigurationError):
        encode_fields({"a": object()})
    with pytest.raises(GrafxConfigurationError):
        encode_fields({"a": float("inf")})
    with pytest.raises(GrafxConfigurationError):
        encode_fields({"a": [1, 2]})


# --- the text form outside the basic multilingual plane (LESSONS L5's second lesson) --------------


def test_a_character_outside_the_basic_plane_survives_the_text_form() -> None:
    r"""``\uXXXX`` carries four hexadecimal digits, and an astral code point does not fit in one.

    ``format(ord(c), "04x")`` is a units question that ``format`` answers wrongly without
    complaining: for U+1F600 it produced five digits, and the reader -- correctly consuming four
    -- read ``ὠ0`` back as U+1F60 followed by the character ``0``. So ``"a.wal"`` with an
    emoji in front of it came back as a different string, silently, and a manifest is where the
    ORIGIN NAME of preserved bytes lives.

    The corpus deliberately leaves the ASCII plane: a corpus that shares an alphabet with its
    assertions cannot find this class at all.
    """
    astral = {
        "emoji": "\U0001f600.wal",
        "gothic": "\U00010330-\U0001033f",
        "supplementary_plane_edges": "\U00010000\U0010ffff",
        "mixed": "wal/\U0001f4a9-é-plain.wal",
    }
    written = encode_fields(astral)
    assert written.decode("ascii") == written.decode("utf-8")  # the writer stays ASCII-only
    assert decode_fields(written) == astral


def test_an_astral_character_is_written_as_a_surrogate_pair_and_not_as_five_digits() -> None:
    r"""The escape is the pair the format defines, not a longer escape the reader cannot parse."""
    written = encode_fields({"origin": "\U0001f600"}).decode("ascii")
    assert "\\ud83d\\ude00" in written
    assert "\\u1f600" not in written


def test_two_different_astral_origins_do_not_collapse_into_one_after_a_round_trip() -> None:
    """The consequence at the door: a lossy escape maps distinct names onto one name.

    Under the five-digit escape both of these came back as the same string, so a manifest written
    for one named the other, and two ranges that must stay apart became one.
    """
    first = "\U0001f600.wal"
    second = "ὠ0.wal"
    assert first != second
    assert decode_fields(encode_fields({"origin": first}))["origin"] == first
    assert decode_fields(encode_fields({"origin": second}))["origin"] == second


@pytest.mark.parametrize(
    "text",
    [
        '{"origin":"\\ud83d"}',
        '{"origin":"\\ud83dtail"}',
        '{"origin":"\\ude00"}',
        '{"origin":"\\ud83d\\u0041"}',
        '{"origin":"\\ud83d\\ud83d"}',
    ],
    ids=["lone-lead", "lead-then-text", "lone-trail", "lead-then-plain", "lead-then-lead"],
)
def test_a_lone_surrogate_escape_is_damage_rather_than_a_character(text: str) -> None:
    """LESSONS L14: the guard belongs on DECODE, because bytes are a door of their own.

    These bytes only ever arrive off a device, so a surrogate that is not part of a pair is
    damage rather than a caller's mistake -- and it is not merely unusual: a lone surrogate
    cannot be encoded back to UTF-8, so accepting it here would plant a ``UnicodeEncodeError``,
    a non-``Grafx*`` escape, in whatever later re-serialised or logged the manifest.
    """
    with pytest.raises(GrafxCorruptionDetected) as caught:
        decode_fields(text.encode("ascii"))
    assert caught.value.details["field"] == "text_form"
    assert "surrogate" in caught.value.message


@pytest.mark.parametrize(
    "text",
    ["\ud83d", "\ude00", "lead\ud83d", "\ud83dtail", "a\ud83db\ude00c"],
    ids=["lone-lead", "lone-trail", "after-text", "before-text", "both-embedded"],
)
def test_the_writer_never_produces_what_the_reader_refuses(text: str) -> None:
    """The other half of the decode guard: an encoder that emits a refused escape is a trap.

    Decode is right to treat a lone surrogate as damage, and the guard belongs there because
    bytes are a door of their own (LESSONS L14). But the two halves have to agree about what the
    format can carry. While the writer emitted ``\\ud83d`` for a lone surrogate -- it is at or
    below ``0xFFFF``, so the pair branch never ran -- a caller holding an unpaired code point
    could write an entry and then have ``LedgerStore.export``, a FROZEN section 8.6 door, report
    the component's own freshly written bytes as corruption. That is a false integrity incident,
    which is exactly what A11-revised exists to prevent.
    """
    with pytest.raises(GrafxConfigurationError) as caught:
        encode_fields({"origin": text})
    assert caught.value.details["field"] == "text"
    assert "surrogate" in caught.value.message


def test_a_manifest_carrying_an_astral_origin_names_the_same_file_when_it_is_read_back() -> None:
    """The whole point of the escape, seen from the record that uses it."""
    from okto_grafx.domain.recovery.manifest import QuarantineManifest

    origin = "wal/\U0001f600-000000000001.wal"
    manifest = QuarantineManifest(
        origin=origin,
        offset=0,
        length=16,
        reason="truncated_tail",
        detail="a tail that would not decode",
        captured_at_wall=1_700_000_000.0,
        digest="0" * 64,
        payload_file="quarantine/e/payload",
        entry_name="e",
    )
    assert QuarantineManifest.parse(manifest.serialize()).origin == origin
