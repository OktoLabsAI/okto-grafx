"""Order-preserving keys for the finite TIMESTAMP/STRING ordered-index contract."""

from __future__ import annotations

from collections.abc import Sequence

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.value import Timestamp, Value

__all__ = [
    "ordered_entry_identity",
    "ordered_index_key",
    "ordered_timestamp_string_key",
]

_NON_NULL = b"\x00"
_NULL = b"\xff"
_STRING_ESCAPE = b"\x00\xff"
_STRING_TERMINATOR = b"\x00\x00"
_SIGN_BIT = 1 << 63


def _ordered_timestamp(value: object, *, position: int) -> bytes:
    """Encode one nullable timestamp so byte order equals the query engine's order."""

    if value is None:
        return _NULL
    if not isinstance(value, Timestamp):
        raise GrafxIndexError(
            "An ordered TIMESTAMP/STRING key requires a Timestamp in its first position.",
            field="values",
            value=type(value).__name__,
            position=position,
            expected="TIMESTAMP",
        )
    shifted = value.micros + _SIGN_BIT
    return _NON_NULL + shifted.to_bytes(8, "big", signed=False)


def _ordered_string(value: object, *, position: int) -> bytes:
    """Encode one nullable string with a prefix-free, UTF-8-order-preserving body."""

    if value is None:
        return _NULL
    if not isinstance(value, str):
        raise GrafxIndexError(
            "An ordered TIMESTAMP/STRING key requires a string in its second position.",
            field="values",
            value=type(value).__name__,
            position=position,
            expected="STRING",
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as failure:
        raise GrafxIndexError(
            "An ordered index string must be encodable as UTF-8.",
            field="values",
            value=repr(value),
            position=position,
            character=failure.start,
        ) from failure
    return _NON_NULL + encoded.replace(b"\x00", _STRING_ESCAPE) + _STRING_TERMINATOR


def ordered_timestamp_string_key(timestamp: object, text: object) -> bytes:
    """Return the composite order key for one TIMESTAMP followed by one STRING."""

    return _ordered_timestamp(timestamp, position=0) + _ordered_string(text, position=1)


def ordered_index_key(values: Sequence[Value], positions: Sequence[int]) -> bytes:
    """Derive the finite ordered key from two validated row positions."""

    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise GrafxIndexError(
            f"An ordered index key needs a sequence of values; got {type(values).__name__}.",
            field="values",
            value=type(values).__name__,
        )
    if (
        isinstance(positions, (str, bytes, bytearray))
        or not isinstance(positions, Sequence)
        or len(positions) != 2
    ):
        raise GrafxIndexError(
            "An ordered TIMESTAMP/STRING key needs exactly two column positions.",
            field="positions",
            value=repr(positions),
        )
    resolved: list[int] = []
    for position in positions:
        if isinstance(position, bool) or not isinstance(position, int):
            raise GrafxIndexError(
                "An ordered key column position must be an integer.",
                field="positions",
                value=repr(position),
            )
        if not 0 <= position < len(values):
            raise GrafxIndexError(
                f"An ordered key position must name one of {len(values)} row values.",
                field="positions",
                value=position,
                arity=len(values),
            )
        resolved.append(position)
    return _ordered_timestamp(values[resolved[0]], position=resolved[0]) + _ordered_string(
        values[resolved[1]], position=resolved[1]
    )


def ordered_entry_identity(key: bytes, ref: RecordRef) -> tuple[bytes, int]:
    """Return the strict physical sort identity of one historical exact entry."""

    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise GrafxIndexError(
            "An ordered entry identity needs key bytes.",
            field="key",
            value=type(key).__name__,
        )
    if not isinstance(ref, RecordRef):
        raise GrafxIndexError(
            "An ordered entry identity needs a RecordRef.",
            field="ref",
            value=type(ref).__name__,
        )
    return bytes(key), ref.encode()
