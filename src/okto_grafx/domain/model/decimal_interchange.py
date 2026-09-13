"""Canonical DECIMAL JSON components; never JSON numbers or implicit host casts."""

from __future__ import annotations

from .decimal_codec import encode_decimal_value
from .decimal_values import DecimalValue
from .errors import SchemaMismatchError

__all__ = ["decimal_json_value", "decimal_from_json_value"]


def decimal_json_value(value: object) -> dict[str, object]:
    """Return owned primitive fields after native validation, preserving p/s."""
    encode_decimal_value(value)
    return {"type": "decimal", "coefficient": str(value.coefficient),
            "precision": value.precision, "scale": value.scale}


def decimal_from_json_value(value: object) -> DecimalValue:
    """Decode only canonical tagged values with bounded exact coefficient text."""
    if (type(value) is not dict or set(value) != {"type", "coefficient", "precision", "scale"}
            or type(value["type"]) is not str or value["type"] != "decimal"
            or type(value["coefficient"]) is not str
            or not _canonical_coefficient(value["coefficient"])):
        raise SchemaMismatchError("Invalid tagged decimal value.", reason="decimal_transport")
    try:
        return DecimalValue(int(value["coefficient"]), value["precision"], value["scale"])
    except SchemaMismatchError as failure:
        raise SchemaMismatchError("Invalid decimal coordinates.", reason="decimal_transport") from failure


def _canonical_coefficient(text: str) -> bool:
    """Bounded ASCII integer grammar, independent of host parsing conveniences."""
    if not 1 <= len(text) <= 39:
        return False
    if text == "0":
        return True
    digits = text[1:] if text[0] == "-" else text
    return (1 <= len(digits) <= 38 and "1" <= digits[0] <= "9"
            and all("0" <= char <= "9" for char in digits[1:]))
