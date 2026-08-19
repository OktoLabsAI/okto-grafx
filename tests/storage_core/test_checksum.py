"""CRC-32C known answer tests (CONTRACT.md section 6.3).

A checksum implementation that is merely self-consistent proves nothing: it would still agree
with itself after a wrong polynomial or a missing reflection, and every page ever written under
it would then be unreadable by anything else. These vectors come from the published CRC-32C
test set, so the implementation is pinned to the algorithm and not to itself.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.page.checksum import (
    CRC32C_POLYNOMIAL,
    CRC32C_POLYNOMIAL_REFLECTED,
    CRC32C_TABLE_SIZE,
    crc32c,
    crc32c_table,
)

KNOWN_VECTORS: tuple[tuple[bytes, int], ...] = (
    (b"", 0x00000000),
    (b"123456789", 0xE3069283),
    (b"\x00" * 32, 0x8A9136AA),
    (b"\xff" * 32, 0x62A8AB43),
    (bytes(range(32)), 0x46DD794E),
    (bytes(range(31, -1, -1)), 0x113FDB5C),
    (b"a", 0xC1D04330),
    (b"The quick brown fox jumps over the lazy dog", 0x22620404),
)
"""Published CRC-32C answers, including the two 32-byte patterns from RFC 3720 appendix B."""


@pytest.mark.parametrize(
    ("payload", "expected"), KNOWN_VECTORS, ids=[f"{index}" for index in range(len(KNOWN_VECTORS))]
)
def test_the_known_vectors_are_reproduced(payload: bytes, expected: int) -> None:
    assert crc32c(payload) == expected


def test_the_polynomial_constants_are_reflections_of_one_another() -> None:
    reflected = 0
    value = CRC32C_POLYNOMIAL
    for _ in range(32):
        reflected = (reflected << 1) | (value & 1)
        value >>= 1
    assert reflected == CRC32C_POLYNOMIAL_REFLECTED


def test_the_table_is_built_once_and_has_one_entry_per_byte() -> None:
    table = crc32c_table()
    assert len(table) == CRC32C_TABLE_SIZE
    assert table is crc32c_table()
    assert all(0 <= entry <= 0xFFFFFFFF for entry in table)
    assert table[0] == 0


def test_the_table_is_the_bit_by_bit_reduction_of_the_reflected_polynomial() -> None:
    # The table is an optimisation of the one-bit-at-a-time reduction; recomputing it the slow
    # way here is what proves the optimisation did not change the function.
    expected = []
    for index in range(CRC32C_TABLE_SIZE):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (CRC32C_POLYNOMIAL_REFLECTED if value & 1 else 0)
        expected.append(value)
    assert list(crc32c_table()) == expected


def test_chaining_a_computation_checksums_the_concatenation() -> None:
    whole = crc32c(b"123456789")
    chained = crc32c(b"9", crc32c(b"12345678"))
    assert chained == whole


def test_a_single_bit_flip_changes_the_checksum() -> None:
    payload = bytearray(b"okto grafx page payload")
    original = crc32c(bytes(payload))
    for position in range(len(payload)):
        for bit in (0, 3, 7):
            flipped = bytearray(payload)
            flipped[position] ^= 1 << bit
            assert crc32c(bytes(flipped)) != original


def test_the_checksum_is_always_an_unsigned_32_bit_integer() -> None:
    for length in range(0, 64):
        value = crc32c(bytes(range(length % 256)) * 3)
        assert 0 <= value <= 0xFFFFFFFF


def test_a_seed_outside_32_bits_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        crc32c(b"x", 1 << 32)
    with pytest.raises(GrafxCorruptionDetected):
        crc32c(b"x", -1)


def test_the_codec_exposes_the_same_checksum() -> None:
    codec = PageCodecV1(512)
    assert codec.checksum(b"123456789") == 0xE3069283
    assert codec.checksum(b"") == 0
    assert codec.format_version == 1
