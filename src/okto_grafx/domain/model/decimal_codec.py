"""Bounded native DECIMAL frame; durable admission separately requires catalog bit 26."""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxCorruptionDetected
from .decimal_values import DecimalValue
from .errors import SchemaMismatchError

DECIMAL_VALUES_CAPABILITY = "decimal_values_v1"
DECIMAL_VALUES_CAPABILITY_BIT = 1 << 26
DECIMAL_VALUE_TAG = 18
DECIMAL_VALUE_BYTES = 19


def encode_decimal_value(value: object) -> bytes:
    """Encode tag, precision, scale and a signed little-endian 128-bit coefficient."""
    if type(value) is not DecimalValue:
        raise SchemaMismatchError("Decimal encoding requires an exact native value.", field="decimal", reason="decimal_codec_type")
    checked = DecimalValue(value.coefficient, value.precision, value.scale)
    return bytes((DECIMAL_VALUE_TAG, checked.precision, checked.scale)) + checked.coefficient.to_bytes(16, "little", signed=True)


def decode_decimal_value(raw: bytes, offset: int = 0) -> tuple[DecimalValue, int]:
    """Validate the complete frame before constructing a native immutable value."""
    if type(raw) is not bytes or type(offset) is not int or offset < 0 or offset > len(raw) - DECIMAL_VALUE_BYTES:
        raise GrafxCorruptionDetected("Truncated decimal frame or invalid offset.", field="decimal", offset=offset)
    if raw[offset] != DECIMAL_VALUE_TAG:
        raise GrafxCorruptionDetected("Unknown decimal value tag.", field="decimal", offset=offset)
    try:
        value = DecimalValue(int.from_bytes(raw[offset + 3:offset + DECIMAL_VALUE_BYTES], "little", signed=True),
                             raw[offset + 1], raw[offset + 2])
    except SchemaMismatchError as failure:
        raise GrafxCorruptionDetected("Invalid stored decimal components.", field="decimal", offset=offset,
                                      reason=failure.details.get("reason")) from failure
    return value, offset + DECIMAL_VALUE_BYTES


__all__ = ["DECIMAL_VALUES_CAPABILITY", "DECIMAL_VALUES_CAPABILITY_BIT", "DECIMAL_VALUE_TAG",
           "DECIMAL_VALUE_BYTES", "encode_decimal_value", "decode_decimal_value"]
