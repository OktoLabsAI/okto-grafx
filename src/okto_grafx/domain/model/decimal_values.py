"""FP-6 exact finite decimals, conversions, arithmetic and aggregate finalization."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from math import gcd, isfinite

from .errors import SchemaMismatchError

MAX_PRECISION = 38
MAX_DECIMAL_TEXT_CHARACTERS = 1024
_ROUNDING = frozenset({"EXACT", "HALF_EVEN", "HALF_UP", "DOWN", "FLOOR", "CEILING"})


def _error(reason: str) -> SchemaMismatchError:
    return SchemaMismatchError("Decimal value cannot satisfy the declared contract.",
                               field="decimal", reason=reason)


def _shape(precision: int, scale: int) -> None:
    if type(precision) is not int or type(scale) is not int:
        raise _error("decimal_component_type")
    if not 1 <= precision <= MAX_PRECISION or not 0 <= scale <= precision:
        raise _error("decimal_type_bounds")


def _rounded(numerator: int, denominator: int, mode: str) -> int:
    if type(mode) is not str or mode not in _ROUNDING:
        raise _error("decimal_rounding_mode")
    if denominator == 0:
        raise _error("decimal_division_by_zero")
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    quotient, remainder = divmod(abs(numerator), abs(denominator))
    if remainder:
        if mode == "EXACT":
            raise _error("decimal_inexact")
        increment = ((mode == "FLOOR" and sign < 0)
                     or (mode == "CEILING" and sign > 0)
                     or (mode in ("HALF_EVEN", "HALF_UP") and
                         (2 * remainder > abs(denominator) or
                          (2 * remainder == abs(denominator) and
                           (mode == "HALF_UP" or quotient % 2 == 1)))))
        quotient += int(increment)
    return sign * quotient


@dataclass(frozen=True, slots=True)
class DecimalValue:
    """Exact coefficient/type snapshot; numeric equality uses numeric_key instead."""

    coefficient: int
    precision: int
    scale: int

    def __post_init__(self) -> None:
        _shape(self.precision, self.scale)
        if type(self.coefficient) is not int:
            raise _error("decimal_component_type")
        if abs(self.coefficient) >= 10 ** self.precision:
            raise _error("decimal_overflow")

    def as_integer_ratio(self) -> tuple[int, int]:
        """Return the reduced exact numerator and positive denominator of this decimal."""
        denominator = 10 ** self.scale
        factor = gcd(abs(self.coefficient), denominator)
        return self.coefficient // factor, denominator // factor

    def numeric_key(self) -> tuple[int, int]:
        """Canonical finite rational key; equality/hash callers share this proof."""
        return self.as_integer_ratio()

    def to_string(self) -> str:
        """Format the exact value while preserving its declared fractional scale."""
        digits = str(abs(self.coefficient)).zfill(self.scale + 1)
        if self.scale:
            digits = digits[:-self.scale] + "." + digits[-self.scale:]
        return ("-" if self.coefficient < 0 else "") + digits

    def rescale(self, precision: int, scale: int, *, rounding: str = "EXACT") -> DecimalValue:
        """Convert to a requested precision and scale using the explicit rounding policy."""
        _shape(precision, scale)
        coefficient = _rounded(self.coefficient * 10 ** scale, 10 ** self.scale, rounding)
        return DecimalValue(coefficient, precision, scale)


def _native(value: DecimalValue) -> DecimalValue:
    if type(value) is not DecimalValue:
        raise _error("decimal_operand_type")
    return value


def decimal_from_text(text: str, precision: int, scale: int, *, rounding: str = "EXACT") -> DecimalValue:
    """Parse bounded ASCII decimal text, refusing implicit whitespace/coercion.

    Exponent magnitude cannot trigger an enormous power allocation: impossible
    large results refuse first, and sub-unit tiny results round from sign alone.
    """
    _shape(precision, scale)
    if type(text) is not str or not 1 <= len(text) <= MAX_DECIMAL_TEXT_CHARACTERS:
        raise _error("decimal_text_bounds")
    if not text.isascii():
        raise _error("decimal_text_syntax")
    mantissa, has_exponent, exponent = text.lower().partition("e")
    exponent_digits = exponent[1:] if exponent.startswith(("+", "-")) else exponent
    if has_exponent and not exponent_digits.isdigit():
        raise _error("decimal_text_syntax")
    sign = mantissa[:1] if mantissa.startswith(("+", "-")) else ""
    if sign:
        mantissa = mantissa[1:]
    integer, separator, fraction = mantissa.partition(".")
    if (not (integer or fraction) or (integer and not integer.isdigit())
            or (fraction and not fraction.isdigit())):
        raise _error("decimal_text_syntax")
    coefficient = int((integer or "0") + fraction) * (-1 if sign == "-" else 1)
    shift = int(exponent or "0") - (len(fraction) if separator else 0) + scale
    if coefficient == 0:
        # Validate the mode even when no rounding is needed.
        return DecimalValue(_rounded(0, 1, rounding), precision, scale)
    digits = len(str(abs(coefficient)))
    if shift >= 0:
        if digits + shift > precision:
            raise _error("decimal_overflow")
        result = _rounded(coefficient * 10 ** shift, 1, rounding)
    elif -shift > digits:
        # Strictly below one tenth of a target unit; ties cannot occur.
        result = _rounded(-1 if coefficient < 0 else 1, 10, rounding)
    else:
        result = _rounded(coefficient, 10 ** (-shift), rounding)
    return DecimalValue(result, precision, scale)


def decimal_from_number(value: int | float, precision: int, scale: int, *,
                        rounding: str = "EXACT") -> DecimalValue:
    """Explicit finite numeric conversion; a DOUBLE contributes its exact ratio."""
    _shape(precision, scale)
    if type(value) is int:
        numerator, denominator = value, 1
    elif type(value) is float and isfinite(value):
        numerator, denominator = value.as_integer_ratio()
    else:
        raise _error("decimal_operand_type")
    coefficient = _rounded(numerator * 10 ** scale, denominator, rounding)
    return DecimalValue(coefficient, precision, scale)


def decimal_compare(left: DecimalValue, right: DecimalValue | int | float) -> int:
    """Compare a native decimal with a finite numeric operand without float casts."""
    left_n, left_d = _native(left).as_integer_ratio()
    if type(right) is DecimalValue:
        right_n, right_d = right.as_integer_ratio()
    elif type(right) is int:
        right_n, right_d = right, 1
    elif type(right) is float and isfinite(right):
        right_n, right_d = right.as_integer_ratio()
    else:
        raise _error("decimal_operand_type")
    delta = left_n * right_d - right_n * left_d
    return (delta > 0) - (delta < 0)


def decimal_negate(value: DecimalValue) -> DecimalValue:
    """Negate a native decimal without changing its precision or scale."""
    _native(value)
    return DecimalValue(-value.coefficient, value.precision, value.scale)


def decimal_absolute(value: DecimalValue) -> DecimalValue:
    """Return the absolute native decimal with its original precision and scale."""
    _native(value)
    return DecimalValue(abs(value.coefficient), value.precision, value.scale)


def _exact_result(coefficient: int, scale: int) -> DecimalValue:
    while scale > 0 and (scale > MAX_PRECISION or abs(coefficient) >= 10 ** MAX_PRECISION):
        if coefficient % 10:
            raise _error("decimal_overflow")
        coefficient //= 10
        scale -= 1
    return DecimalValue(coefficient, MAX_PRECISION, scale)


def decimal_add(left: DecimalValue, right: DecimalValue, *, subtract: bool = False) -> DecimalValue:
    """Add or subtract exactly, fitting the result without losing nonzero digits."""
    _native(left)
    _native(right)
    if type(subtract) is not bool:
        raise _error("decimal_operand_type")
    scale = max(left.scale, right.scale)
    a = left.coefficient * 10 ** (scale - left.scale)
    b = right.coefficient * 10 ** (scale - right.scale)
    return _exact_result(a - b if subtract else a + b, scale)


def decimal_multiply(left: DecimalValue, right: DecimalValue) -> DecimalValue:
    """Multiply exact coefficients and fit the combined scale without lossy truncation."""
    _native(left)
    _native(right)
    return _exact_result(left.coefficient * right.coefficient, left.scale + right.scale)


def decimal_divide(left: DecimalValue, right: DecimalValue) -> DecimalValue:
    """Divide with one half-even rounding at the largest scale fitting precision 38."""
    _native(left)
    _native(right)
    if right.coefficient == 0:
        raise _error("decimal_division_by_zero")
    numerator = left.coefficient * 10 ** right.scale
    denominator = right.coefficient * 10 ** left.scale
    return _quotient(numerator, denominator)


def _quotient(numerator: int, denominator: int) -> DecimalValue:
    integral = abs(numerator) // abs(denominator)
    integer_digits = len(str(integral)) if integral else 0
    if integer_digits > MAX_PRECISION:
        raise _error("decimal_overflow")
    scale = MAX_PRECISION - integer_digits
    while scale >= 0:
        coefficient = _rounded(numerator * 10 ** scale, denominator, "HALF_EVEN")
        if abs(coefficient) < 10 ** MAX_PRECISION:
            return DecimalValue(coefficient, MAX_PRECISION, scale)
        scale -= 1
    raise _error("decimal_overflow")


class DecimalTotal:
    """Streaming exact coefficients; query callers retain row/work/memory budgets."""

    __slots__ = ("coefficient", "scale")

    def __init__(self, integer_total: int = 0) -> None:
        """Seed from an already accumulated exact integer total, never a float."""
        if type(integer_total) is not int:
            raise _error("decimal_operand_type")
        self.coefficient = integer_total
        self.scale = 0

    def add(self, value: DecimalValue | int) -> None:
        """Align and add without imposing the stored envelope on intermediate totals."""
        if type(value) is int:
            self.coefficient += value * 10 ** self.scale
            return
        _native(value)
        scale = max(self.scale, value.scale)
        self.coefficient = self.coefficient * 10 ** (scale - self.scale) + value.coefficient * 10 ** (scale - value.scale)
        self.scale = scale

    def observe_scale(self, scale: int) -> None:
        """Retain declared input scale even when DISTINCT drops its representative."""
        _shape(MAX_PRECISION, scale)
        if scale > self.scale:
            self.coefficient *= 10 ** (scale - self.scale)
            self.scale = scale

    def result(self, *, count: int | None = None) -> DecimalValue:
        """Fit SUM once, or divide exact AVG once without materializing the sum."""
        if count is None:
            return _exact_result(self.coefficient, self.scale)
        if type(count) is not int or count <= 0:
            raise _error("decimal_operand_type")
        return _quotient(self.coefficient, count * 10 ** self.scale)


def _totals(values: Iterable[DecimalValue | None]) -> tuple[int, int, int]:
    coefficient = scale = count = 0
    for value in values:
        if value is None:
            continue
        _native(value)
        target_scale = max(scale, value.scale)
        coefficient = (coefficient * 10 ** (target_scale - scale)
                       + value.coefficient * 10 ** (target_scale - value.scale))
        scale = target_scale
        count += 1
    return coefficient, scale, count


def decimal_sum(values: Iterable[DecimalValue | None]) -> DecimalValue:
    """Accumulate exactly, fitting only the final value; caller owns row budgets."""
    coefficient, scale, _count = _totals(values)
    return _exact_result(coefficient, scale)


def decimal_average(values: Iterable[DecimalValue | None]) -> DecimalValue | None:
    """Divide the exact total once; a temporary sum outside precision 38 is legal."""
    coefficient, scale, count = _totals(values)
    return None if count == 0 else _quotient(coefficient, count * 10 ** scale)


__all__ = ["MAX_PRECISION", "DecimalValue", "DecimalTotal", "decimal_compare", "decimal_add",
           "decimal_multiply", "decimal_divide", "decimal_negate", "decimal_absolute",
           "MAX_DECIMAL_TEXT_CHARACTERS", "decimal_from_text", "decimal_from_number",
           "decimal_sum", "decimal_average"]
