"""CRC-32C, the integrity check under every page and every log record (CONTRACT.md section 6).

The engine writes one checksum algorithm and only one: Castagnoli's CRC-32C, the polynomial
0x1EDC6F41, evaluated in its reflected form 0x82F63B78 so the message is consumed low bit first.
It is the same function the hardware instruction on modern processors computes, which keeps the
door open for an accelerated adapter later without changing a single stored byte.

The implementation is table driven and pure Python: the 256-entry table is built once at import
and the loop is a byte-at-a-time reduction. Nothing here touches a mechanism, so the domain can
verify its own bytes without asking an adapter for permission.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxCorruptionDetected

__all__ = [
    "CRC32C_POLYNOMIAL",
    "CRC32C_POLYNOMIAL_REFLECTED",
    "CRC32C_INITIAL",
    "CRC32C_TABLE_SIZE",
    "crc32c",
    "crc32c_table",
]

CRC32C_POLYNOMIAL: int = 0x1EDC6F41
"""The Castagnoli polynomial in its normal, most significant bit first form."""

CRC32C_POLYNOMIAL_REFLECTED: int = 0x82F63B78
"""The same polynomial reflected, which is the form a least significant bit first loop uses."""

CRC32C_INITIAL: int = 0
"""The seed of a fresh checksum. Chaining a computation passes the previous result instead."""

CRC32C_TABLE_SIZE: int = 256
"""One table entry per possible byte value."""

_MASK_32: int = 0xFFFFFFFF


def _build_table() -> tuple[int, ...]:
    """Build the byte-at-a-time reduction table for the reflected polynomial."""
    entries: list[int] = []
    for index in range(CRC32C_TABLE_SIZE):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (CRC32C_POLYNOMIAL_REFLECTED if value & 1 else 0)
        entries.append(value & _MASK_32)
    return tuple(entries)


_TABLE: tuple[int, ...] = _build_table()


def crc32c_table() -> tuple[int, ...]:
    """Return the precomputed reduction table, so a test can inspect it without rebuilding it."""
    return _TABLE


def crc32c(data: bytes, crc: int = CRC32C_INITIAL) -> int:
    """Return the CRC-32C of the data, optionally continuing a previous computation.

    Passing the result of an earlier call as crc checksums the concatenation of the two byte
    ranges, which is what lets a caller checksum a page without first joining its parts.
    """
    if not isinstance(crc, int) or isinstance(crc, bool) or not 0 <= crc <= _MASK_32:
        raise GrafxCorruptionDetected(
            f"A CRC-32C seed must be an unsigned 32-bit integer; got {crc!r}.",
            field="crc",
            value=repr(crc),
        )
    table = _TABLE
    value = (crc ^ _MASK_32) & _MASK_32
    for byte in data:
        value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
    return (value ^ _MASK_32) & _MASK_32
