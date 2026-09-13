"""Bounded native temporal field access, independent of clocks and zone lookups."""

from __future__ import annotations

from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, TimeValue,
    DurationValue, _integer, _offset_text, _MIN_I64, _MAX_I64,
)

_DATE_FIELDS = {"year":"year", "quarter":"quarter", "month":"month", "week":"week",
                "weekyear":"week_year", "day":"day", "ordinalday":"ordinal_day",
                "weekday":"day_of_week", "dayofweek":"day_of_week", "dayofquarter":"day_of_quarter"}
_TIME_FIELDS = {"hour":"hour", "minute":"minute", "second":"second", "nanosecond":"nanosecond"}
_DURATION_UNITS = {"years":("months",12), "quarters":("months",3), "months":("months",1),
                   "weeks":("days",7), "days":("days",1), "hours":("seconds",3600),
                   "minutes":("seconds",60), "seconds":("seconds",1)}
_DURATION_PARTS = {"quartersofyear":("months",3,4), "monthsofquarter":("months",1,3),
                   "monthsofyear":("months",1,12), "daysofweek":("days",1,7),
                   "minutesofhour":("seconds",60,60), "secondsofminute":("seconds",1,60)}
_PRECISION = {"milliseconds":(1000,1_000_000), "microseconds":(1_000_000,1000),
              "nanoseconds":(1_000_000_000,1)}
_TYPES = (DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, TimeValue, DurationValue)


def _trunc(value: int, divisor: int) -> int:
    return (abs(value) // divisor) * (-1 if value < 0 else 1)


def _remainder(value: int, divisor: int) -> int:
    return value - _trunc(value, divisor) * divisor


def _unknown() -> None:
    raise SchemaMismatchError("Temporal field is not available for this value family.",
                              reason="temporal_field_unavailable")


def _duration_field(value: DurationValue, field: str) -> int:
    if field in _DURATION_UNITS:
        attribute, divisor = _DURATION_UNITS[field]
        return _trunc(getattr(value, attribute), divisor)
    if field in _DURATION_PARTS:
        attribute, divisor, modulus = _DURATION_PARTS[field]
        return _remainder(_trunc(getattr(value, attribute), divisor), modulus)
    if field in _PRECISION:
        scale, divisor = _PRECISION[field]
        return value.seconds * scale + value.nanoseconds // divisor
    if field.endswith("ofsecond") and field[:-8] in _PRECISION:
        return value.nanoseconds // _PRECISION[field[:-8]][1]
    _unknown()


def temporal_field(value: object, field: str) -> int | str:
    """Read a supported case-insensitive ASCII field; refuse unknowns and overflow.

    A field valid on another family does not invent a date, timezone or clock.
    Query-level NULL propagation remains the query executor's responsibility.
    """
    if type(value) not in _TYPES:
        raise SchemaMismatchError("Temporal access requires a native value.", reason="temporal_component_type")
    if type(field) is not str or not 1 <= len(field) <= 32 or not field.isascii():
        raise SchemaMismatchError("Temporal field requires a bounded ASCII name.", reason="temporal_field_name")
    name = field.lower()
    if type(value) is DurationValue:
        result = _duration_field(value,name)
    else:
        date = value if type(value) is DateValue else (
            value.date if type(value) is LocalDateTimeValue else value.local.date if type(value) is DateTimeValue else None)
        time = value if type(value) is LocalTimeValue else (
            value.time if type(value) in (TimeValue,LocalDateTimeValue) else value.local.time if type(value) is DateTimeValue else None)
        if name in _DATE_FIELDS and date is not None:
            result = getattr(date,_DATE_FIELDS[name])
        elif name in _TIME_FIELDS and time is not None:
            result = getattr(time,_TIME_FIELDS[name])
        elif name in ("millisecond","microsecond") and time is not None:
            result = time.nanosecond // (1_000_000 if name == "millisecond" else 1000)
        elif name in ("timezone","offset","offsetminutes","offsetseconds") and type(value) in (TimeValue,DateTimeValue):
            if name == "timezone" and type(value) is DateTimeValue and value.zone is not None:
                result = value.zone
            elif name in ("timezone","offset"):
                result = _offset_text(value.offset_seconds)
            else:
                result = _trunc(value.offset_seconds,60) if name == "offsetminutes" else value.offset_seconds
        elif name in ("epochseconds","epochmillis") and type(value) is DateTimeValue:
            result = value.epoch_seconds if name == "epochseconds" else value.epoch_seconds * 1000 + value.nanosecond // 1_000_000
        else:
            _unknown()
    return _integer(result,"temporal_field_result",_MIN_I64,_MAX_I64) if type(result) is int else result


__all__ = [
    'temporal_field',
]
