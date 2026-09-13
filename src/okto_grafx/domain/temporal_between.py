"""Exact native differences in calendar or elapsed units, without ambient clocks."""

from __future__ import annotations

from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
    TemporalValue, NANOSECONDS_PER_SECOND,
)
from .ports.temporal_zone import TemporalZoneResolver
from .temporal_components import _zone, _at_zone
from .temporal_arithmetic import add_duration

_INSTANTS = (DateValue,LocalTimeValue,TimeValue,LocalDateTimeValue,DateTimeValue)
_MODES = frozenset({"between","inmonths","indays","inseconds"})


def _date(value):
    if type(value) is DateValue:
        return value
    if type(value) is LocalDateTimeValue:
        return value.date
    return value.local.date if type(value) is DateTimeValue else None


def _time(value):
    if type(value) is LocalTimeValue:
        return value
    if type(value) in (TimeValue,LocalDateTimeValue):
        return value.time
    return value.local.time if type(value) is DateTimeValue else LocalTimeValue(0)


def _zone_id(value):
    if type(value) is DateTimeValue:
        return value.zone if value.zone is not None else value.offset_seconds
    return value.offset_seconds if type(value) is TimeValue else None


def _coerce(left, right, resolver):
    """Fill missing components from the peer, retaining original zoned instants."""
    left_date, right_date = _date(left), _date(right)
    left_zone, right_zone = _zone_id(left), _zone_id(right)

    def fill(value: TemporalValue, date: DateValue | None, other_date: DateValue | None,
             zone: str | int | None, other_zone: str | int | None) -> TemporalValue:
        """Fill missing date and zone components from the peer operand before temporal subtraction."""
        actual_date = date if date is not None else other_date
        actual_zone = zone if zone is not None else other_zone
        if actual_date is None:
            return TimeValue(_time(value),actual_zone) if actual_zone is not None else _time(value)
        if type(value) is DateTimeValue:
            return value
        local = LocalDateTimeValue(actual_date,_time(value))
        return _zone(local,actual_zone,resolver) if actual_zone is not None else local

    return (fill(left,left_date,right_date,left_zone,right_zone),
            fill(right,right_date,left_date,right_zone,left_zone))


def _elapsed_nanos(left,right,resolver) -> int:
    first, second = _coerce(left,right,resolver)
    if type(first) is DateTimeValue:
        return ((second.epoch_seconds-first.epoch_seconds)*NANOSECONDS_PER_SECOND
                + second.nanosecond-first.nanosecond)
    if type(first) is LocalDateTimeValue:
        return ((second.local_epoch_seconds-first.local_epoch_seconds)*NANOSECONDS_PER_SECOND
                + second.time.nanosecond-first.time.nanosecond)
    if type(first) is TimeValue:
        return second.utc_nanoseconds-first.utc_nanoseconds
    return second.nanoseconds-first.nanoseconds


def _calendar_count(left,right,months: bool,resolver) -> int:
    first,second = _coerce(left,right,resolver)
    if type(first) not in (DateTimeValue,LocalDateTimeValue):
        raise SchemaMismatchError("Calendar differences need a date on at least one operand.",
                                  reason="temporal_difference_missing_date")
    if type(first) is DateTimeValue:
        if _zone_id(first) != _zone_id(second):
            second = _at_zone(second,_zone_id(first),resolver)
        first,second = first.local,second.local
    end_date = second.date
    day_delta = end_date.epoch_day-first.date.epoch_day
    # Count complete calendar boundaries in the first operand's zone/local
    # timeline. Fractional days must not turn into whole months or days.
    if day_delta > 0 and second.time < first.time:
        end_date = end_date.add_days(-1)
    elif day_delta < 0 and second.time > first.time:
        end_date = end_date.add_days(1)
    if not months:
        return end_date.epoch_day-first.date.epoch_day
    # A 32-day ordinal separates month/day components while preserving whole
    # calendar-month truncation, including reversed ranges and month-end dates.
    span = ((end_date.year-first.date.year)*12+end_date.month-first.date.month)*32 + end_date.day-first.date.day
    return (abs(span)//32)*(-1 if span < 0 else 1)


def temporal_between(left: object, right: object, mode: str = "between", *,
                     resolver: TemporalZoneResolver | None = None) -> DurationValue | None:
    """Compute duration.between/inMonths/inDays/inSeconds for native/NULL inputs.

    between splits complete months then complete days and a nanosecond residue;
    inSeconds uses exact elapsed coordinates. Dateless pairs do not acquire an
    invented date; their inMonths/inDays operations refuse. No storage admission
    or query overload dispatch is enabled by this component implementation.
    """
    if type(mode) is not str or not 1 <= len(mode) <= 16 or mode.lower() not in _MODES:
        raise SchemaMismatchError("Unknown temporal difference mode.",reason="temporal_difference_mode")
    mode = mode.lower()
    for value in (left,right):
        if value is not None and type(value) not in _INSTANTS:
            raise SchemaMismatchError("Temporal differences require native instants.",reason="temporal_difference_type")
    if left is None or right is None:
        return None
    if mode == "inmonths":
        return DurationValue(months=_calendar_count(left,right,True,resolver))
    if mode == "indays":
        return DurationValue(days=_calendar_count(left,right,False,resolver))
    months = days = 0
    if mode == "between" and _date(left) is not None and _date(right) is not None:
        months = _calendar_count(left,right,True,resolver)
        left = add_duration(left,DurationValue(months=months),resolver=resolver)
        days = _calendar_count(left,right,False,resolver)
        left = add_duration(left,DurationValue(days=days),resolver=resolver)
    seconds,nanos = divmod(_elapsed_nanos(left,right,resolver),NANOSECONDS_PER_SECOND)
    return DurationValue(months,days,seconds,nanos)


__all__ = [
    'temporal_between',
]
