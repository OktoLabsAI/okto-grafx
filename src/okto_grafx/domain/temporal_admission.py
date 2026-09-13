"""Bounded temporal capability discovery in a pending row's native values.

Discovery does not itself publish a capability. It examines the entire submitted
shape (including MAP keys), rather than trusting an early positive match, an
inferred column type or a cached row encoding proof.
"""

from __future__ import annotations

from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_codec import encode_temporal_value, TEMPORAL_VALUES_CAPABILITY
from okto_grafx.domain.model.decimal_codec import encode_decimal_value, DECIMAL_VALUES_CAPABILITY
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
)
from okto_grafx.domain.model.value import MAX_VALUE_DEPTH, value_type_of

_TEMPORAL_CLASSES = frozenset((DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue))


def temporal_storage_required(values: tuple[object, ...]) -> bool:
    """Validate the native row and report whether it specifically needs temporal storage."""
    return TEMPORAL_VALUES_CAPABILITY in native_value_capabilities(values)


def native_value_capabilities(values: tuple[object, ...]) -> frozenset[str]:
    """Discover all native scalar format fences in one bounded validation walk.

    The outer tuple is a row, not an additional LIST nesting level. Schema, number
    finiteness, embedding authority and quotas remain the ordinary tuple encoder's
    responsibility. No arbitrary host conversion or temporal zone lookup is used.
    """
    if type(values) is not tuple:
        raise SchemaMismatchError("A native-value admission scan requires a row tuple.", field="values")

    required: set[str] = set()

    def visit(value: object, depth: int) -> None:
        """Validate every nested member even after finding a required capability."""
        if depth > MAX_VALUE_DEPTH:
            raise SchemaMismatchError("Stored value exceeds the nesting limit.", field="depth", limit=MAX_VALUE_DEPTH)
        kind = type(value)
        if kind in _TEMPORAL_CLASSES:
            encode_temporal_value(value)
            required.add(TEMPORAL_VALUES_CAPABILITY)
        elif kind is DecimalValue:
            encode_decimal_value(value)
            required.add(DECIMAL_VALUES_CAPABILITY)
        elif isinstance(value, dict):
            for key, item in dict.items(value):
                visit(key, depth + 1)
                visit(item, depth + 1)
        elif isinstance(value, (tuple, list)):
            iterator = tuple.__iter__(value) if isinstance(value, tuple) else list.__iter__(value)
            for item in iterator:
                visit(item, depth + 1)
        else:
            value_type_of(value)

    for value in values:
        visit(value, 0)
    return frozenset(required)


__all__ = [
    'temporal_storage_required',
    'native_value_capabilities',
]
