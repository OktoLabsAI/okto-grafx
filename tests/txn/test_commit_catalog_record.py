"""CAP-1B record grammar and hostile decoding, not integrated crash certification."""

from __future__ import annotations

from dataclasses import replace
from random import Random
import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.model import Timestamp
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_identity import CommitId, assign_commit_time
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits, decode_commit_metadata
from okto_grafx.domain.txn.commit_catalog import (
    CommitCatalogEntry, CommitKind, decode_commit_catalog_entry,
)

STORE = bytes(range(16))
BODY_PREFIX = b"GXCM\x01nnnn"


def _text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"s" + struct.pack("<I", len(raw)) + raw


def _root(value: bytes) -> bytes:
    return BODY_PREFIX + b"m\x01\0\0\0" + _text("x") + value


def _entry(metadata: CommitMetadata | None = None, *, maintenance: bool = False) -> CommitCatalogEntry:
    return CommitCatalogEntry(
        identity=CommitId(STORE, 17),
        timing=assign_commit_time(Timestamp(10), Timestamp(10)),
        metadata_bytes=None if metadata is None else metadata.canonical_bytes,
        kind=CommitKind.MAINTENANCE if maintenance else CommitKind.DATA,
    )


def _resign(raw: bytes, offset: int, patch: bytes) -> bytes:
    body = bytearray(raw[:-4])
    body[offset:offset + len(patch)] = patch
    return bytes(body) + struct.pack("<I", crc32c(body))


@pytest.mark.parametrize("metadata", [CommitMetadata(), CommitMetadata(actor="worker"),
    CommitMetadata(origin="test", correlation_id="123", reason="explicit", attributes={
        "a": [None, True, False, 1, -1, 0.0, -0.0, "é\0🙂"], "b": {"": "v"},
    }),
    CommitMetadata(actor="x" * 10_000, limits=MetadataLimits(max_string_bytes=16_384)),
])
def test_metadata_round_trip_uses_format_limits_not_default_write_limits(metadata: CommitMetadata) -> None:
    decoded = decode_commit_metadata(metadata.canonical_bytes)
    assert decoded == metadata
    assert decoded.canonical_bytes == metadata.canonical_bytes
    with pytest.raises(TypeError):
        decoded.attributes["new"] = "x"


@pytest.mark.parametrize("raw", [b"", b"GXCM", b"BAD!\x01nnnnm\0\0\0\0",
    BODY_PREFIX + b"m\0\0\0\0n", BODY_PREFIX + b"a\0\0\0\0",
    b"GXCM\x01tnnnm\0\0\0\0", BODY_PREFIX + b"m\xff\xff\xff\xff",
    _root(b"?"), _root(b"s\xff\xff\xff\xff"), _root(b"s\x01\0\0\0\xff"),
    _root(b"s\x03\0\0\0\xed\xa0\x80"), _root(b"i\x01"),
    _root(b"d" + struct.pack("<d", float("nan"))),
    _root(b"d" + struct.pack("<d", float("inf"))),
    _root(b"a\xff\xff\xff\xff"),
    BODY_PREFIX + b"m\x01\0\0\0n" + b"n",
    BODY_PREFIX + b"m\x02\0\0\0" + _text("a") + b"n" + _text("a") + b"t",
    BODY_PREFIX + b"m\x02\0\0\0" + _text("z") + b"n" + _text("a") + b"t",
    b"x" * 65_537,
], ids=lambda raw: f"bytes_{len(raw)}")
def test_metadata_refuses_malformed_or_noncanonical_bytes(raw: bytes) -> None:
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_metadata(raw)


def test_unknown_metadata_version_is_not_interpreted_as_current() -> None:
    with pytest.raises(GrafxSchemaVersionMismatch):
        decode_commit_metadata(b"GXCM\x02nnnnm\0\0\0\0")


def test_decoder_depth_and_global_value_bounds_precede_large_expansion() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_metadata(_root((b"a\x01\0\0\0" * 17) + b"n"))
    # Each local list fits its declared count, but the complete tree exceeds 4096.
    branch = b"a" + struct.pack("<I", 2048) + b"n" * 2048
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_metadata(_root(b"a\x02\0\0\0" + branch * 2))


@pytest.mark.parametrize("metadata", [None, CommitMetadata(), CommitMetadata(actor="DO-NOT-LOG")])
@pytest.mark.parametrize("maintenance", [False, True])
def test_catalog_entry_round_trip_and_expected_identity(metadata: CommitMetadata | None, maintenance: bool) -> None:
    entry = _entry(metadata, maintenance=maintenance)
    raw = entry.encode()
    assert len(raw) == 60 + (0 if metadata is None else len(metadata.canonical_bytes))
    assert raw[:8] == b"GXCMREC\0"
    decoded = decode_commit_catalog_entry(raw, expected_store_uuid=STORE, expected_sequence=17)
    assert decoded == entry
    assert decoded.metadata == metadata
    assert "DO-NOT-LOG" not in repr(decoded)
    assert decoded.encode() == raw


@pytest.mark.parametrize("offset,patch", [
    (0, b"badmagic"), (28, b"\0" * 8), (28, b"\xff" * 8),
    (36, struct.pack("<q", 12)), (52, struct.pack("<I", 1)),
    (10, struct.pack("<H", 0)), (10, struct.pack("<H", 3)),
])
def test_checksum_valid_semantic_corruption_still_refuses(offset: int, patch: bytes) -> None:
    raw = _resign(_entry().encode(), offset, patch)
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_catalog_entry(raw)


@pytest.mark.parametrize("offset,patch", [(8, struct.pack("<H", 2)), (10, struct.pack("<H", 0x8001))])
def test_unknown_record_semantics_refuse_with_version_error(offset: int, patch: bytes) -> None:
    with pytest.raises(GrafxSchemaVersionMismatch):
        decode_commit_catalog_entry(_resign(_entry().encode(), offset, patch))


def test_wrong_store_sequence_and_record_checksum_refuse() -> None:
    raw = _entry().encode()
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_catalog_entry(raw, expected_store_uuid=b"x" * 16)
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_catalog_entry(raw, expected_sequence=18)
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_catalog_entry(raw[:-1] + bytes([raw[-1] ^ 1]))
    for truncated in (raw[:4], raw[:59], raw[:-1]):
        with pytest.raises(GrafxCorruptionDetected):
            decode_commit_catalog_entry(truncated)


def test_valid_envelope_does_not_hide_damaged_metadata_or_expose_it() -> None:
    raw = _entry(CommitMetadata(actor="DO-NOT-LOG")).encode()
    damaged = _resign(raw, 56 + 5, b"?")
    with pytest.raises(GrafxCorruptionDetected) as error:
        decode_commit_catalog_entry(damaged)
    assert "DO-NOT-LOG" not in str(error.value.to_dict())


@pytest.mark.parametrize("value", [None, "text", bytearray(b"x"), memoryview(b"x")])
def test_wrong_argument_types_are_configuration_not_media_corruption(value: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        decode_commit_metadata(value)
    with pytest.raises(GrafxConfigurationError):
        decode_commit_catalog_entry(value)


def test_encoding_revalidates_captured_fields_before_packing() -> None:
    entry = _entry()
    object.__setattr__(entry, "kind", "data")
    with pytest.raises(GrafxConfigurationError):
        entry.encode()
    with pytest.raises(GrafxConfigurationError):
        replace(_entry(), identity="not-an-identity")


def test_largest_metadata_body_and_record_fit_exact_format_ceiling() -> None:
    limits = MetadataLimits(max_bytes=65_536, max_string_bytes=16_384)
    metadata = CommitMetadata(
        actor="a" * 16_384, origin="o" * 16_384, correlation_id="c" * 16_384,
        reason="r" * 16_354, limits=limits,
    )
    assert len(metadata.canonical_bytes) == 65_536
    raw = _entry(metadata).encode()
    assert len(raw) == 65_596
    assert decode_commit_catalog_entry(raw).metadata == metadata
    with pytest.raises(GrafxCorruptionDetected):
        decode_commit_catalog_entry(raw + b"x")


def test_entry_detaches_supplied_identity_and_time_values() -> None:
    identity = CommitId(STORE, 17)
    timing = assign_commit_time(Timestamp(10), Timestamp(10))
    entry = CommitCatalogEntry(identity, timing)
    object.__setattr__(identity, "sequence", 99)
    object.__setattr__(timing.observed_at, "micros", -100)
    assert entry.identity.sequence == 17
    assert entry.timing.observed_at.micros == 10
    assert decode_commit_catalog_entry(entry.encode(), expected_sequence=17) == entry


def test_bounded_mutation_corpus_never_accepts_noncanonical_data() -> None:
    rng = Random(20260908)
    metadata = CommitMetadata(actor="writer", attributes={"a": [1, -0.0, None], "b": "é"})
    record = _entry(metadata).encode()
    accepted_body = accepted_record = refused = 0
    for iteration in range(1500):
        body = bytearray(metadata.canonical_bytes)
        position = rng.randrange(len(body))
        body[position] ^= 1 << rng.randrange(8)
        try:
            decoded = decode_commit_metadata(bytes(body))
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch):
            refused += 1
        else:
            assert decoded.canonical_bytes == body
            accepted_body += 1
        candidate = bytearray(record)
        position = rng.randrange(len(candidate) - 4)
        candidate[position] ^= 1 << rng.randrange(8)
        if iteration % 2:
            candidate[-4:] = struct.pack("<I", crc32c(candidate[:-4]))
        try:
            decoded_record = decode_commit_catalog_entry(
                bytes(candidate), expected_store_uuid=STORE, expected_sequence=17,
            )
        except (GrafxCorruptionDetected, GrafxSchemaVersionMismatch):
            refused += 1
        else:
            assert decoded_record.encode() == candidate
            accepted_record += 1
    # A wholly rejecting parser must not make the corpus appear successful.
    assert accepted_body > 0 and accepted_record > 0 and refused > 0


@pytest.mark.parametrize("expectation", [{"expected_store_uuid": bytearray(16)},
    {"expected_store_uuid": b"short"}, {"expected_sequence": True}, {"expected_sequence": 0}])
def test_invalid_directory_expectations_are_caller_errors(expectation: dict) -> None:
    with pytest.raises(GrafxConfigurationError):
        decode_commit_catalog_entry(_entry().encode(), **expectation)


def test_codec_does_not_register_unsupported_runtime_capability() -> None:
    from okto_grafx.domain.model.catalog import Catalog
    import okto_grafx

    assert "commit_catalog_v1" not in Catalog().required_capabilities()
    assert not hasattr(okto_grafx.Database, "enable_commit_catalog")
