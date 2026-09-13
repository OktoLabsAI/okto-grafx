"""Exact internal temporal arithmetic; public query/storage wiring is separate."""

from __future__ import annotations

from fractions import Fraction
import math

from .errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue,
    TimeValue, TemporalValue, NANOSECONDS_PER_DAY, NANOSECONDS_PER_SECOND, _component,
    _integer, _MIN_I64, _MAX_I64,
)
from .ports.temporal_zone import TemporalZoneResolver

_TEMPORALS = (DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue)


def _duration(months: int, days: int, nanos: int) -> DurationValue:
    # Normalize before checking the final signed-second bound: cancelling terms
    # may have intermediate values larger than int64 without overflowing a value.
    seconds, remainder = divmod(nanos, NANOSECONDS_PER_SECOND)
    return DurationValue(months, days, seconds, remainder)


def _elapsed(value: DurationValue) -> int:
    return value.seconds * NANOSECONDS_PER_SECOND + value.nanoseconds


def _at_instant(seconds: int, nanos: int, zone: str, resolver: TemporalZoneResolver) -> DateTimeValue:
    result = resolver.at_instant(seconds, nanos, zone)
    _component(result, DateTimeValue, "temporal_zone_result")
    if (result.epoch_seconds, result.nanosecond, result.zone) != (seconds, nanos, zone):
        raise GrafxCorruptionDetected("Temporal zone provider changed the requested instant or zone.",
                                     field="temporal_zone_result")
    return result


def _calendar_step(value: DateTimeValue, amount: int, *, months: bool,
                   resolver: TemporalZoneResolver | None) -> DateTimeValue:
    if not amount:
        return value
    date = value.local.date.add_months(amount) if months else value.local.date.add_days(amount)
    local = LocalDateTimeValue(date, value.local.time)
    if value.zone is None:
        return DateTimeValue(local, value.offset_seconds)
    assert resolver is not None
    # Retain the original offset in an overlap if it is still valid. An explicit
    # offset passed to at_local would reject a normal DST change, not prefer it.
    candidate = _at_instant(local.local_epoch_seconds - value.offset_seconds,
                            local.time.nanosecond, value.zone, resolver)
    if candidate.local == local:
        return candidate
    result = resolver.at_local(local, value.zone)
    _component(result, DateTimeValue, "temporal_zone_result")
    if result.zone != value.zone or result.nanosecond != local.time.nanosecond:
        raise GrafxCorruptionDetected("Temporal zone provider changed calendar zone or precision.",
                                     field="temporal_zone_result")
    return result


def _shift(value: TemporalValue, duration: DurationValue, sign: int,
           resolver: TemporalZoneResolver | None) -> TemporalValue:
    _component(duration, DurationValue, "duration")
    if type(value) not in _TEMPORALS:
        raise SchemaMismatchError("Temporal arithmetic requires native operands.",
                                  reason="temporal_arithmetic_type")
    months, days, nanos = sign * duration.months, sign * duration.days, sign * _elapsed(duration)
    if type(value) is DurationValue:
        return _duration(value.months + months, value.days + days, _elapsed(value) + nanos)
    if type(value) is DateValue:
        # The pinned semantics include whole days in normalized seconds (signed
        # truncation, not Python floor division); discard the remaining clock.
        whole_days = abs(duration.seconds) // 86400
        if duration.seconds < 0:
            whole_days = -whole_days
        return value.add_months(months).add_days(days + sign * whole_days)
    if type(value) in (LocalTimeValue, TimeValue):
        time = value if type(value) is LocalTimeValue else value.time
        shifted = LocalTimeValue((time.nanoseconds + nanos) % NANOSECONDS_PER_DAY)
        return shifted if type(value) is LocalTimeValue else TimeValue(shifted, value.offset_seconds)
    if type(value) is LocalDateTimeValue:
        local = LocalDateTimeValue(value.date.add_months(months).add_days(days), value.time)
        seconds, remainder = divmod(local.time.nanosecond + nanos, NANOSECONDS_PER_SECOND)
        return LocalDateTimeValue.from_local_epoch_parts(local.local_epoch_seconds + seconds, remainder)
    if value.zone is not None and resolver is None and (months or days or nanos):
        raise GrafxUnsupportedOperation("Named temporal arithmetic needs an explicit rule provider.",
                                        field="temporal_timezone_provider")
    # Each calendar step may cross a gap/overlap. Elapsed seconds follow the
    # resulting instant, not a second calendar/wall-time interpretation.
    shifted = _calendar_step(value, months, months=True, resolver=resolver)
    shifted = _calendar_step(shifted, days, months=False, resolver=resolver)
    if not nanos:
        return shifted
    seconds, remainder = divmod(shifted.nanosecond + nanos, NANOSECONDS_PER_SECOND)
    seconds += shifted.epoch_seconds
    if value.zone is not None:
        assert resolver is not None
        return _at_instant(seconds, remainder, value.zone, resolver)
    return DateTimeValue.from_epoch_parts(seconds, remainder, offset_seconds=value.offset_seconds)


def add_duration(value: TemporalValue, duration: DurationValue, *,
                 resolver: TemporalZoneResolver | None = None) -> TemporalValue:
    """Apply months, calendar days, then elapsed seconds/nanos in that order."""
    return _shift(value, duration, 1, resolver)


def subtract_duration(value: TemporalValue, duration: DurationValue, *,
                      resolver: TemporalZoneResolver | None = None) -> TemporalValue:
    """Subtract components in the same order; month-end clamping is not invertible."""
    return _shift(value, duration, -1, resolver)


def scale_duration(value: DurationValue, number: int | float, *, divide: bool = False) -> DurationValue:
    """Exact rational scaling, carrying fractional months/days toward smaller units."""
    _component(value, DurationValue, "duration")
    if type(divide) is not bool or type(number) not in (int, float):
        raise SchemaMismatchError("Duration scaling needs a native number and boolean mode.",
                                  reason="temporal_arithmetic_type")
    if type(number) is int:
        _integer(number, "factor", _MIN_I64, _MAX_I64)
    elif not math.isfinite(number):
        raise SchemaMismatchError("Duration scaling requires a finite factor.",
                                  reason="temporal_arithmetic_nonfinite")
    factor = Fraction(str(number)) if type(number) is float else Fraction(number)
    if divide:
        if not factor:
            raise SchemaMismatchError("A duration cannot be divided by zero.",
                                      reason="temporal_arithmetic_zero_divisor")
        factor = 1 / factor
    months = value.months * factor
    days = value.days * factor + (months - int(months)) * Fraction(2629746, 86400)
    nanos = _elapsed(value) * factor + (days - int(days)) * NANOSECONDS_PER_DAY
    return _duration(int(months), int(days), int(nanos))


__all__ = [
    'TemporalValue',
    'add_duration',
    'subtract_duration',
    'scale_duration',
]
