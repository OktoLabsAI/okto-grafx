"""Native function signatures and bounded evaluation shared by planner and executor."""

from __future__ import annotations

import math
from collections.abc import Mapping

from okto_grafx.domain.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.value import INT64_MAX, ValueType
from okto_grafx.domain.query.limits import MAX_QUERY_VALUE_CHARACTERS

MAX_GENERATED_LIST_ELEMENTS = 100_000
_STRING_UNARY = {"LOWER": str.lower, "UPPER": str.upper, "TRIM": str.strip,
                 "LTRIM": str.lstrip, "RTRIM": str.rstrip, "TOLOWER": str.lower,
                 "TOUPPER": str.upper}
_MATH_UNARY = {"ABS": abs, "CEIL": math.ceil, "CEILING": math.ceil, "FLOOR": math.floor,
               "SQRT": math.sqrt, "EXP": math.exp, "LOG": math.log, "LOG10": math.log10,
               "SIN": math.sin, "COS": math.cos, "TAN": math.tan, "ASIN": math.asin,
               "ACOS": math.acos, "ATAN": math.atan, "DEGREES": math.degrees,
               "RADIANS": math.radians, "SIGN": lambda x: (x > 0) - (x < 0)}
_CONVERSIONS = {"TOINTEGER", "TOFLOAT", "TOBOOLEAN", "TOSTRING"}
_ARITIES = {**{name: (1, 1) for name in (*_STRING_UNARY, *_MATH_UNARY, *_CONVERSIONS,
                                      "HEAD", "LAST", "TAIL", "REVERSE", "KEYS")},
            "RANGE": (2, 3), "SUBSTRING": (2, 3), "LEFT": (2, 2), "RIGHT": (2, 2),
            "REPLACE": (3, 3), "ROUND": (1, 1), "ATAN2": (2, 2), "PI": (0, 0), "E": (0, 0),
            "RAND": (0, 0)}
NATIVE_SCALARS = frozenset(_ARITIES)
NONDETERMINISTIC_SCALARS = frozenset({"RAND"})
__all__ = ["NATIVE_SCALARS", "NONDETERMINISTIC_SCALARS", "MAX_GENERATED_LIST_ELEMENTS", "scalar_arity", "scalar_type", "scalar_value"]


def _bad(name: str, message: str = "Incompatible native function argument.") -> GrafxPlanError:
    return GrafxPlanError(message, field="function", value=name)


def scalar_arity(name: str, count: int) -> None:
    """Validate positional cardinality at analysis and at the direct evaluation boundary."""
    minimum, maximum = _ARITIES[name]
    if not minimum <= count <= maximum:
        raise _bad(name, f"{name} requires {minimum}..{maximum} positional arguments.")


def scalar_type(name: str, *arguments: ValueType | None) -> ValueType | None:
    """Infer types without forcing heterogeneous list elements into one scalar family."""
    scalar_arity(name, len(arguments))
    number = (ValueType.INT64, ValueType.DOUBLE)
    string = (ValueType.STRING,)
    integer = (ValueType.INT64,)
    any_value = tuple(ValueType)
    if name in _STRING_UNARY:
        allowed, result = (string,), ValueType.STRING
    elif name in _MATH_UNARY or name in {"ROUND", "ATAN2"}:
        allowed = (number,) * len(arguments)
        result = arguments[0] if name == "ABS" else ValueType.INT64 if name == "SIGN" else ValueType.DOUBLE
    elif name in {"PI", "E", "RAND"}:
        allowed, result = (), ValueType.DOUBLE
    elif name == "RANGE":
        # RANGE validates operand values when evaluated, including statically
        # known invalid literals. Inference must not move runtime errors ahead
        # of CASE/empty-row evaluation or a statement's rollback boundary.
        return ValueType.NULL if ValueType.NULL in arguments else ValueType.LIST
    elif name in {"HEAD", "LAST", "TAIL"}:
        allowed, result = ((ValueType.LIST,),), ValueType.LIST if name == "TAIL" else None
    elif name == "REVERSE":
        allowed, result = ((ValueType.LIST, ValueType.STRING),), arguments[0]
    elif name == "KEYS":
        allowed, result = ((ValueType.MAP,),), ValueType.LIST
    elif name in {"SUBSTRING", "LEFT", "RIGHT"}:
        allowed, result = (string, *((integer,) * (len(arguments) - 1))), ValueType.STRING
    elif name == "REPLACE":
        allowed, result = (string,) * 3, ValueType.STRING
    else:
        allowed = (any_value,)
        result = {"TOINTEGER": ValueType.INT64, "TOFLOAT": ValueType.DOUBLE,
                  "TOBOOLEAN": ValueType.BOOL, "TOSTRING": ValueType.STRING}[name]
    for actual, accepted in zip(arguments, allowed, strict=True):
        if actual not in (None, ValueType.NULL, *accepted):
            raise _bad(name)
    return ValueType.NULL if ValueType.NULL in arguments else result


def _kind(value: object) -> ValueType | None:
    """Classify language values, never invoking arbitrary host conversion callbacks."""
    return {bool: ValueType.BOOL, int: ValueType.INT64, float: ValueType.DOUBLE,
            str: ValueType.STRING, list: ValueType.LIST, tuple: ValueType.LIST,
            dict: ValueType.MAP, type(None): ValueType.NULL}.get(type(value))


def _decimal_int64(text: str) -> int | None:
    """Truncate an ASCII decimal exactly, without ambient contexts or huge integers."""
    text = text.strip()
    negative = text.startswith("-")
    if text[:1] in ("+", "-"):
        text = text[1:]
    parts = text.lower().split("e")
    if len(parts) > 2:
        return None
    exponent = 0
    if len(parts) == 2:
        written = parts[1]
        reverse = written.startswith("-")
        if written[:1] in ("+", "-"):
            written = written[1:]
        if not written or any(not "0" <= digit <= "9" for digit in written):
            return None
        # Beyond the input length plus INT64 digits, only overflow/underflow is
        # possible. Saturation prevents an attacker-sized exponent allocation.
        cap = len(text) + 20
        for digit in written:
            exponent = min(cap, exponent * 10 + ord(digit) - ord("0"))
        if reverse:
            exponent = -exponent
    mantissa = parts[0].split(".")
    digits = "".join(mantissa)
    if len(mantissa) > 2 or not digits or any(not "0" <= digit <= "9" for digit in digits):
        return None
    shift = exponent - (len(mantissa[1]) if len(mantissa) == 2 else 0)
    digits = digits.lstrip("0")
    if not digits:
        return 0
    size = len(digits) + shift
    if size > 19:
        return None
    if size <= 0:
        return 0
    integral = int(digits[:size] + "0" * max(0, shift))
    bound = INT64_MAX + int(negative)
    if integral > bound or integral == bound and any(digit != "0" for digit in digits[size:]):
        return None
    return -integral if negative else integral


def range_values(*arguments: object) -> range | None:
    """Validate range operands and expose an allocation-free inclusive sequence.

    Internal consumers must bound what they materialize or enumerate. This helper
    never exposes a new stored/public value type or changes the scalar list cap.
    """
    scalar_arity("RANGE", len(arguments))
    if any(item is not None and type(item) is not int for item in arguments):
        raise GrafxPlanError("range operands must be INT64 or NULL.", field="function", value="RANGE",
                             reason="range_argument_type", query_phase="execution")
    if any(value is None for value in arguments):
        return None
    start, end = arguments[:2]
    step = arguments[2] if len(arguments) == 3 else 1
    if step == 0:
        raise GrafxPlanError("range step must not be zero.", field="function", value="RANGE",
                             reason="range_argument_bounds", query_phase="execution")
    if any(not -INT64_MAX - 1 <= item <= INT64_MAX for item in arguments):
        raise GrafxPlanError("range operands must fit INT64.", field="function", value="RANGE",
                             reason="range_argument_bounds", query_phase="execution")
    return range(start, end + (1 if step > 0 else -1), step)


def scalar_value(name: str, *arguments: object) -> object:
    """Evaluate a closed function, with output admission before large allocations."""
    scalar_arity(name, len(arguments))
    if name == "RANGE":
        values = range_values(*arguments)
        if values is None:
            return None
        distance, step = values.stop - values.start, values.step
        count = max(0, (abs(distance) + abs(step) - 1) // abs(step)) if distance * step > 0 else 0
        if count > MAX_GENERATED_LIST_ELEMENTS:
            raise GrafxQueryBudgetExceeded("Generated list exceeds its element budget.", resource="generated_list")
        return tuple(values)
    scalar_type(name, *(_kind(value) for value in arguments))
    if name in NONDETERMINISTIC_SCALARS:
        raise _bad(name, "Nondeterministic scalars require an execution source, not pure evaluation.")
    if any(value is None for value in arguments):
        return None
    value = arguments[0] if arguments else None
    if name in _STRING_UNARY:
        if type(value) is not str:
            raise _bad(name)
        return _STRING_UNARY[name](value)
    if name in _MATH_UNARY or name in {"ROUND", "ATAN2"}:
        if any(type(item) not in (int, float) for item in arguments):
            raise _bad(name)
        try:
            result = (math.floor(value + 0.5) if name == "ROUND" else
                      math.atan2(*arguments) if name == "ATAN2" else _MATH_UNARY[name](value))
            if name not in {"ABS", "SIGN"}:
                result = float(result)
        except (ValueError, OverflowError) as error:
            raise _bad(name, "Function arguments are outside its finite numeric domain.") from error
        if (type(result) is int and not -INT64_MAX - 1 <= result <= INT64_MAX
                or type(result) is float and not math.isfinite(result)):
            raise _bad(name, "Function result is outside its finite numeric domain.")
        return result
    if name in {"PI", "E"}:
        return math.pi if name == "PI" else math.e
    if name in {"HEAD", "LAST", "TAIL", "REVERSE"}:
        if not isinstance(value, (list, tuple)) and not (name == "REVERSE" and type(value) is str):
            raise _bad(name)
        if name == "TAIL":
            return tuple(value[1:])
        if name == "REVERSE":
            return value[::-1] if type(value) is str else tuple(reversed(value))
        return (value[0] if name == "HEAD" else value[-1]) if value else None
    if name == "KEYS":
        if not isinstance(value, Mapping):
            raise _bad(name)
        return tuple(value)
    if name in {"SUBSTRING", "LEFT", "RIGHT"}:
        if type(value) is not str or any(type(item) is not int or item < 0 for item in arguments[1:]):
            raise _bad(name, "String positions and lengths must be non-negative integers.")
        start = arguments[1]
        if name == "LEFT":
            return value[:start]
        if name == "RIGHT":
            return value[-start:] if start else ""
        return value[start:] if len(arguments) == 2 else value[start:start + arguments[2]]
    if name == "REPLACE":
        if any(type(item) is not str for item in arguments):
            raise _bad(name)
        text, search, replacement = arguments
        count = text.count(search)
        length = len(text) + count * (len(replacement) - len(search))
        if length > MAX_QUERY_VALUE_CHARACTERS:
            raise GrafxQueryBudgetExceeded("Replacement exceeds its string budget.", resource="query_value")
        return text.replace(search, replacement)
    if type(value) not in (str, bool, int, float):
        raise _bad(name, "Conversion requires a scalar string, boolean or number.")
    if name == "TOSTRING":
        return ("true" if value else "false") if type(value) is bool else str(value)
    if name == "TOBOOLEAN":
        if type(value) is bool:
            return value
        if type(value) is str:
            return {"true": True, "false": False}.get(value.strip().lower())
        raise _bad(name)
    if type(value) is bool and name == "TOFLOAT":
        raise _bad(name)
    try:
        if name == "TOINTEGER" and type(value) is str:
            return _decimal_int64(value)
        else:
            result = int(value) if name == "TOINTEGER" else float(value)
    except (ValueError, OverflowError):
        return None
    if (type(result) is int and not -INT64_MAX - 1 <= result <= INT64_MAX
            or type(result) is float and not math.isfinite(result)):
        return None
    return result
