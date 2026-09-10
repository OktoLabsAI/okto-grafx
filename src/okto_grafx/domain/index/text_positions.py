"""Canonical bounded positional chunks; exact token routing remains CRC-32C."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import struct

from okto_grafx.domain.errors import GrafxIndexError

_TAIL = struct.Struct("<BHHB")
CHUNK = 32
__all__ = ["CHUNK", "position_keys", "decode_position", "position_bucket_key"]


def position_keys(fields: Sequence[Sequence[str]]) -> Iterator[bytes]:
    """One bounded chunk per field/term/32 occurrences, in deterministic order."""
    for field, tokens in enumerate(fields):
        locations = {}
        for index, term in enumerate(tokens):
            locations.setdefault(term, []).append(index)
        for term, positions in sorted(locations.items()):
            raw = term.encode("utf-8")
            prefix = b"\x03" + struct.pack("<H", len(raw)) + raw
            for start in range(0, len(positions), CHUNK):
                selected = positions[start:start + CHUNK]
                yield prefix + _TAIL.pack(field, start // CHUNK, len(tokens) - 1, len(selected)) + struct.pack("<" + "H" * len(selected), *selected)


def decode_position(key: bytes) -> tuple[str, int, int, int, tuple[int, ...]]:
    """Reject malformed/noncanonical scalar bounds before using any position."""
    try:
        if type(key) is not bytes or key[:1] != b"\x03":
            raise ValueError
        size, = struct.unpack_from("<H", key, 1)
        if not 1 <= size <= 128:
            raise ValueError
        term = key[3:3 + size].decode("utf-8")
        field, chunk, last, count = _TAIL.unpack_from(key, 3 + size)
        positions = struct.unpack("<" + "H" * count, key[3 + size + _TAIL.size:])
        if not 0 <= field < 4 or not 1 <= count <= CHUNK or chunk > 2047 or tuple(sorted(set(positions))) != positions or positions[-1] > last:
            raise ValueError
        return term, field, chunk, last + 1, positions
    except (ValueError, struct.error, UnicodeError) as failure:
        raise GrafxIndexError("Invalid full-text positional posting.", field="text_positions") from failure


def position_bucket_key(key: bytes) -> bytes:
    """Co-locate positional chunks with the ordinary exact term posting."""
    if key[:1] == b"\x03":
        term, *_ = decode_position(key)
        return b"\x01" + term.encode("utf-8")
    return key
