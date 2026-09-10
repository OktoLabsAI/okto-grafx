"""Backward-compatible format checks for the version-2 index artifact identity."""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import GrafxSchemaVersionMismatch
from okto_grafx.domain.index.header import (
    INDEX_HEADER_FORMAT_VERSION,
    INDEX_HEADER_SIZE,
    IndexHeader,
)
from okto_grafx.domain.index.visibility import IndexVisibility


_V1 = struct.Struct("<HBBIIQQ16s")
_V2 = struct.Struct("<HBBIIQQ16sQ")
_DIGEST = bytes.fromhex("00112233445566778899aabbccddeeff")


def test_real_v1_bytes_decode_with_an_unclaimed_artifact_nonce() -> None:
    image = _V1.pack(1, 1, 0, 7, 32, 19, 11, _DIGEST)

    decoded = IndexHeader.decode(image)

    assert decoded.format_version == 1
    assert decoded.visibility is IndexVisibility.EXACT
    assert decoded.table_id == 7
    assert decoded.bucket_count == 32
    assert decoded.built_through_lsn == 19
    assert decoded.reconciled_through_lsn == 11
    assert decoded.digest == _DIGEST
    assert decoded.artifact_nonce == 0


def test_v2_round_trip_preserves_the_artifact_nonce() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.PROXIMITY,
        table_id=23,
        bucket_count=64,
        digest=_DIGEST,
        built_through_lsn=101,
        reconciled_through_lsn=89,
        artifact_nonce=0xFEDCBA9876543210,
    )

    encoded = header.encode()

    assert INDEX_HEADER_FORMAT_VERSION == 2
    assert INDEX_HEADER_SIZE == _V2.size
    assert len(encoded) == _V2.size
    assert IndexHeader.decode(encoded) == header


def test_position_transitions_preserve_the_artifact_nonce() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=5,
        bucket_count=16,
        digest=_DIGEST,
        artifact_nonce=0x1020304050607080,
    )

    advanced = header.advanced_to(41)
    reconciled = advanced.reconciled_to(37)

    assert advanced.artifact_nonce == header.artifact_nonce
    assert reconciled.artifact_nonce == header.artifact_nonce
    assert IndexHeader.decode(reconciled.encode()).artifact_nonce == header.artifact_nonce


def test_a_future_v2_shaped_header_is_refused_before_its_payload_is_trusted() -> None:
    image = _V2.pack(5, 1, 0, 1, 4, 0, 0, _DIGEST, 73)

    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        IndexHeader.decode(image)

    assert refused.value.details["field"] == "format_version"
    assert refused.value.details["value"] == 5


def test_explicit_v1_writes_the_exact_legacy_layout() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=13,
        bucket_count=8,
        digest=_DIGEST,
        built_through_lsn=17,
        reconciled_through_lsn=9,
        artifact_nonce=0,
        format_version=1,
    )

    encoded = header.encode()

    assert len(encoded) == _V1.size
    assert _V1.unpack(encoded) == (1, 1, 0, 13, 8, 17, 9, _DIGEST)
    assert IndexHeader.decode(encoded) == header
