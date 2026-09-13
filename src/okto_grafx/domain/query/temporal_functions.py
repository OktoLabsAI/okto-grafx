"""Native temporal function signatures and provider-explicit execution."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import TEMPORAL_VALUE_TYPES, ValueType
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue, TemporalValue,
)
from okto_grafx.domain.ports.temporal_zone import TemporalZoneResolver
from okto_grafx.domain.temporal_runtime import TemporalStatementContext
from okto_grafx.domain import temporal_components as component, temporal_text as text
from okto_grafx.domain.temporal_between import temporal_between
from okto_grafx.domain.temporal_truncation import truncate_temporal

TEMPORAL_CONSTRUCTORS = {
    "DATE": ValueType.DATE, "LOCALTIME": ValueType.LOCALTIME, "TIME": ValueType.TIME,
    "LOCALDATETIME": ValueType.LOCALDATETIME, "DATETIME": ValueType.DATETIME,
    "DURATION": ValueType.DURATION,
}
TEMPORAL_CLASSES = (DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue)
TEMPORAL_ARITIES = {
    **{name: (0 if name != "DURATION" else 1, 1) for name in TEMPORAL_CONSTRUCTORS},
    **{f"{name}.{mode}": (0, 1) for name in TEMPORAL_CONSTRUCTORS if name != "DURATION"
       for mode in ("TRANSACTION", "STATEMENT", "REALTIME")},
    **{f"{name}.TRUNCATE": (2, 3) for name in TEMPORAL_CONSTRUCTORS if name != "DURATION"},
    **{f"DURATION.{mode}": (2, 2) for mode in ("BETWEEN", "INMONTHS", "INDAYS", "INSECONDS")},
    "DATETIME.FROMEPOCH": (2, 2), "DATETIME.FROMEPOCHMILLIS": (1, 1),
}


def temporal_result_type(name: str) -> ValueType:
    """Return the native type declared by a temporal constructor family."""
    return TEMPORAL_CONSTRUCTORS[name.split(".", 1)[0]]


def temporal_property_type(subject_type: ValueType | None, field: str) -> ValueType | None:
    """Infer a field's scalar family without evaluating its subject.

    Availability and component overflow are validated at the actual access;
    planning must not execute clocks or unselected expressions as probes.
    """
    if subject_type not in TEMPORAL_VALUE_TYPES:
        return None
    if subject_type in (ValueType.TIME, ValueType.DATETIME) and field.lower() in ("timezone", "offset"):
        return ValueType.STRING
    return ValueType.INT64


def temporal_arithmetic_type(operator: str, left: ValueType | None, right: ValueType | None) -> ValueType | None:
    """Return a supported temporal result type, or None for other operand pairs."""
    if ValueType.NULL in (left, right) and (left in TEMPORAL_VALUE_TYPES or right in TEMPORAL_VALUE_TYPES):
        return ValueType.NULL
    if operator in ("+", "-") and left in TEMPORAL_VALUE_TYPES and right is ValueType.DURATION:
        return left
    if operator == "+" and left is ValueType.DURATION and right in TEMPORAL_VALUE_TYPES:
        return right
    if operator in ("*", "/") and left is ValueType.DURATION and right in (ValueType.INT64, ValueType.DOUBLE):
        return ValueType.DURATION
    if operator == "*" and right is ValueType.DURATION and left in (ValueType.INT64, ValueType.DOUBLE):
        return ValueType.DURATION
    return None


def temporal_function(name: str, arguments: Sequence[object], *,
                      context: TemporalStatementContext | None = None,
                      resolver: TemporalZoneResolver | None = None) -> TemporalValue | None:
    """Execute with an explicit captured statement clock and zone resolver.

    A missing source refuses only an operation that needs it; there is no ambient
    clock or timezone fallback in this domain module.
    """
    name = name.upper()
    minimum, maximum = TEMPORAL_ARITIES[name]
    if not minimum <= len(arguments) <= maximum:
        raise GrafxPlanError("Incorrect temporal function argument count.", field="function", value=name)
    if any(value is None for value in arguments):
        return None
    family, _, operation = name.partition(".")
    kind = family.lower()
    resolver = context.resolver if context is not None else resolver

    def current(timezone: str | None = None, mode: str = "statement", selected: str = kind) -> TemporalValue:
        """Construct a current temporal value from the already captured query clock."""
        if context is None:
            raise GrafxPlanError("Temporal current values require a captured query clock.", field="function", value=name)
        return context.current(selected, mode=mode, timezone=timezone)

    def timezone_map(value: object) -> str | None:
        """Extract an exact single-entry timezone map without coercing its value."""
        if type(value) is dict and len(value) == 1:
            key = next(iter(value))
            if type(key) is str and key.lower() == "timezone":
                if type(value[key]) is not str:
                    raise GrafxPlanError("A timezone must be a string.", field="function", value=name)
                return value[key]
        return None

    def reference_date() -> DateValue | None:
        """Use the captured current date when named time resolution needs a reference date."""
        return cast(DateValue, current(selected="date")) if context is not None else None

    if operation in ("TRANSACTION", "STATEMENT", "REALTIME"):
        if arguments:
            arg = arguments[0]
            zone = timezone_map(arg)
            if zone is None:
                raise GrafxPlanError("Clock variants accept only a timezone map.", field="function", value=name)
            return current(zone, operation.lower())
        return current(mode=operation.lower())
    if operation == "TRUNCATE":
        return truncate_temporal(kind, *arguments, resolver=resolver, reference_date=reference_date())
    if family == "DURATION" and operation:
        return temporal_between(*arguments, mode=operation.lower(), resolver=resolver)
    if operation == "FROMEPOCH":
        return component.datetime_from_epoch(*arguments)
    if operation == "FROMEPOCHMILLIS":
        return component.datetime_from_epoch_millis(*arguments)
    if not arguments:
        return current()
    value = arguments[0]
    if type(value) is str:
        parser = getattr(text, "parse_" + kind)
        options = {"resolver": resolver} if family in ("TIME", "DATETIME") else {}
        if family == "TIME":
            options["reference_date"] = reference_date()
        return parser(value, **options)
    if type(value) in TEMPORAL_CLASSES:
        if family == "DURATION" and type(value) is DurationValue:
            return DurationValue(value.months, value.days, value.seconds, value.nanoseconds)
        selector = "datetime" if family in ("DATETIME", "LOCALDATETIME") else "date" if family == "DATE" else "time"
        if family in ("DATETIME", "LOCALDATETIME") and type(value) is DateValue:
            selector = "date"
        value = {selector: value}
    if family != "DURATION":
        zone = timezone_map(value)
        if zone is not None:
            return current(zone)
    builder = getattr(component, "build_" + kind)
    options = {"resolver": resolver} if family in ("TIME", "DATETIME") else {}
    if family == "TIME":
        options["reference_date"] = reference_date()
    return builder(value, **options)


__all__ = [
    'TEMPORAL_CONSTRUCTORS',
    'TEMPORAL_CLASSES',
    'TEMPORAL_ARITIES',
    'temporal_result_type',
    'temporal_property_type',
    'temporal_arithmetic_type',
    'temporal_function',
]
