"""Closed native scalar contracts shared by planning and row evaluation."""

from __future__ import annotations

import math

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import INT64_MAX, ValueType

NATIVE_SCALARS = frozenset(("LOWER", "UPPER", "TRIM", "ABS"))
__all__ = ["NATIVE_SCALARS", "scalar_type", "scalar_value"]


def scalar_type(name: str, argument: ValueType | None) -> ValueType | None:
    """Infer a closed native scalar's result type or refuse an incompatible input."""
    allowed = (
        (ValueType.INT64, ValueType.DOUBLE) if name == "ABS" else (ValueType.STRING,)
    )
    if argument not in (None, ValueType.NULL, *allowed):
        raise GrafxPlanError(
            "Incompatible native scalar argument.", field="function", value=name
        )
    if argument is ValueType.NULL:
        return ValueType.NULL
    return argument if name == "ABS" else ValueType.STRING


def scalar_value(name: str, value: object) -> object:
    """Evaluate a validated native scalar, preserving null and numeric bounds."""
    if value is None:
        return None
    if name == "ABS":
        if type(value) not in (int, float):
            raise GrafxPlanError(
                "abs requires INT64 or DOUBLE.", field="function", value=name
            )
        result = abs(value)
        if (type(result) is int and result > INT64_MAX) or (
            type(result) is float and not math.isfinite(result)
        ):
            raise GrafxPlanError(
                "abs result is outside its finite numeric domain.",
                field="function",
                value=name,
            )
        return result
    if type(value) is not str:
        raise GrafxPlanError(
            "String scalar requires STRING.", field="function", value=name
        )
    return {"LOWER": str.lower, "UPPER": str.upper, "TRIM": str.strip}[name](value)
