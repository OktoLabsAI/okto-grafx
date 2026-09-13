"""Native temporal predicates and order keys are deliberately separate contracts."""

from __future__ import annotations

from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, TimeValue, DurationValue,
)

_FAMILIES = (DateTimeValue, LocalDateTimeValue, DateValue, TimeValue, LocalTimeValue, DurationValue)
_RANK = {family:rank for rank,family in enumerate(_FAMILIES)}
_OPERATORS = frozenset({"=","<>","<","<=",">",">="})


def _validate(value: object) -> None:
    if type(value) not in _RANK:
        raise SchemaMismatchError("Temporal comparison requires native values.", reason="temporal_comparison_type")


def temporal_order_key(value: object) -> tuple[int, tuple]:
    """Total order among the six native families; not a relational predicate.

    Duration keys use exact average seconds, then nanos and component ties.
    Keys may contain wide *internal* integers, never wrapped int64 intermediates.
    """
    _validate(value)
    if type(value) is DateValue:
        key = (value.epoch_day,)
    elif type(value) is LocalTimeValue:
        key = (value.nanoseconds,)
    elif type(value) is LocalDateTimeValue:
        key = (value.date.epoch_day,value.time.nanoseconds)
    elif type(value) in (TimeValue,DateTimeValue):
        key = value.sort_key
    else:
        key = (value.months*2629746 + value.days*86400 + value.seconds,
               value.nanoseconds,value.months,value.days,value.seconds)
    return _RANK[type(value)],key


def temporal_predicate(left: object, right: object, operator: str) -> bool | None:
    """Evaluate native/null operands without borrowing ORDER BY's total order.

    Different families are unequal and relationally incomparable. Durations
    allow equality; strict inequalities are undefined even for equal durations,
    while <= and >= are true for equal components and undefined otherwise.
    """
    if type(operator) is not str or operator not in _OPERATORS:
        raise SchemaMismatchError("Unknown temporal predicate operator.", reason="temporal_comparison_operator")
    for value in (left,right):
        if value is not None:
            _validate(value)
    if left is None or right is None:
        return None
    equal = type(left) is type(right) and left == right
    if operator in ("=","<>"):
        return equal if operator == "=" else not equal
    if type(left) is not type(right):
        return None
    if type(left) is DurationValue:
        return True if equal and operator in ("<=",">=") else None
    first, second = temporal_order_key(left), temporal_order_key(right)
    if operator == "<":
        return first < second
    if operator == "<=":
        return first <= second
    return first > second if operator == ">" else first >= second


__all__ = [
    'temporal_order_key',
    'temporal_predicate',
]
