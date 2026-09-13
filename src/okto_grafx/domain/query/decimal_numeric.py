"""Exact DECIMAL expression semantics without changing native DTO equality."""

from __future__ import annotations

from math import isfinite, isnan

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.decimal_values import (
    DecimalValue, decimal_compare, decimal_add, decimal_multiply, decimal_divide,
)
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import ValueType


def decimal_numeric_compare(left: object, right: object) -> int | None:
    """Compare decimal/numeric operands exactly; NaN remains unordered."""
    if type(left) not in (DecimalValue, int, float) or type(right) not in (DecimalValue, int, float) or (
        type(left) is not DecimalValue and type(right) is not DecimalValue
    ):
        raise TypeError("Decimal comparison requires a decimal and a numeric operand")
    if type(left) is not DecimalValue:
        result = decimal_numeric_compare(right, left)
        return None if result is None else -result
    if type(right) is float and not isfinite(right):
        return None if isnan(right) else -1 if right > 0 else 1
    return decimal_compare(left, right)


def decimal_group_key(value: DecimalValue) -> tuple[object, ...]:
    """Use existing numeric keys only with proof of identical mathematical value.

    A float is only a candidate encoding for a grouping key: its exact binary
    ratio must equal the decimal ratio. No arithmetic, rounding or cast uses it.
    """
    numerator, denominator = value.as_integer_ratio()
    if denominator == 1:
        return ("number", numerator)
    candidate = numerator / denominator
    if candidate.as_integer_ratio() == (numerator, denominator):
        return ("number", candidate)
    return ("decimal", numerator, denominator)


class DecimalOrderKey:
    """Internal numeric ordering adapter; never a public or durable property type."""

    __slots__ = ("value",)

    def __init__(self, value: DecimalValue) -> None:
        """Accept only an exact native value, not an arbitrary comparator hook."""
        if type(value) is not DecimalValue:
            raise TypeError("Decimal ordering requires a native decimal")
        self.value = value

    def _compare(self, other: object) -> int | None:
        """Unwrap one internal key and compare only the closed numeric families."""
        value = other.value if type(other) is DecimalOrderKey else other
        if type(value) not in (DecimalValue, int, float):
            raise TypeError("Decimal ordering cannot compare another family")
        return decimal_numeric_compare(self.value, value)

    def __eq__(self, other: object) -> bool:
        """Equality of order keys is numeric, independently of declared scale."""
        if type(other) not in (DecimalOrderKey, DecimalValue, int, float):
            return False
        return self._compare(other) == 0

    def __lt__(self, other: object) -> bool:
        """Compare ascending, including the reflected float/int comparison path."""
        result = self._compare(other)
        return result is not None and result < 0

    def __gt__(self, other: object) -> bool:
        """Compare descending without converting the coefficient to binary float."""
        result = self._compare(other)
        return result is not None and result > 0


def decimal_arithmetic_type(operator: str, left: ValueType | None, right: ValueType | None) -> ValueType | None:
    """Infer decimal arithmetic without implicit DOUBLE promotion or evaluation."""
    if ValueType.DECIMAL not in (left, right):
        return None
    if left not in (ValueType.INT64, ValueType.DECIMAL, ValueType.NULL, None) or right not in (
        ValueType.INT64, ValueType.DECIMAL, ValueType.NULL, None
    ) or operator not in ("+", "-", "*", "/"):
        raise GrafxPlanError("Decimal arithmetic requires DECIMAL/INT64 and +, -, *, /.",
                             field="operator", value=operator, reason="decimal_operand_type")
    return ValueType.DECIMAL


def decimal_arithmetic(operator: str, left: object, right: object) -> DecimalValue:
    """Promote INT64 exactly and dispatch the fixed native decimal result policy."""
    if type(left) not in (DecimalValue, int) or type(right) not in (DecimalValue, int):
        raise GrafxPlanError("Decimal arithmetic never implicitly promotes DOUBLE.",
                             field="operator", value=operator, reason="decimal_operand_type", query_phase="execution")
    try:
        lhs = DecimalValue(left, 38, 0) if type(left) is int else left
        rhs = DecimalValue(right, 38, 0) if type(right) is int else right
        if operator in ("+", "-"):
            return decimal_add(lhs, rhs, subtract=operator == "-")
        if operator == "*":
            return decimal_multiply(lhs, rhs)
        if operator == "/":
            return decimal_divide(lhs, rhs)
    except SchemaMismatchError as failure:
        raise GrafxPlanError("Decimal arithmetic cannot satisfy its exact result contract.",
                             field="operator", value=operator, reason=failure.details.get("reason"),
                             query_phase="execution") from failure
    raise GrafxPlanError("Unsupported decimal arithmetic operator.", field="operator", value=operator,
                         reason="decimal_operator", query_phase="execution")


__all__ = ["decimal_numeric_compare", "decimal_group_key", "DecimalOrderKey", "decimal_arithmetic_type", "decimal_arithmetic"]
