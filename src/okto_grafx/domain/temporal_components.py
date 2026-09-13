"""Explicit map composition for native temporal values, independent of query I/O."""

from __future__ import annotations

from fractions import Fraction
import math
from types import MappingProxyType

from .errors import GrafxUnsupportedOperation
from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue, TimeValue,
    NANOSECONDS_PER_DAY, _component, _integer, _offset, _MIN_I64, _MAX_I64, days_in_month,
)
from .ports.temporal_zone import TemporalZoneResolver
from .temporal_text import parse_offset

_DATE = {"year", "month", "day", "week", "dayofweek", "quarter", "dayofquarter", "ordinalday"}
_TIME = {"hour", "minute", "second", "millisecond", "microsecond", "nanosecond"}
_DURATION = {"years", "months", "weeks", "days", "hours", "minutes", "seconds",
             "milliseconds", "microseconds", "nanoseconds"}


def _refuse(reason: str, **details):
    raise SchemaMismatchError("Invalid temporal component combination.", reason=reason, **details)


def _fields(value, allowed: set[str]) -> dict:
    if type(value) not in (dict, MappingProxyType):
        _refuse("temporal_component_type", field="components")
    if not value or len(value) > len(allowed):
        _refuse("temporal_component_fields", field="components")
    result = {}
    for key, component in value.items():
        if type(key) is not str:
            _refuse("temporal_component_type", field="component_name")
        if not 1 <= len(key) <= 12 or not key.isascii():
            _refuse("temporal_component_fields", field="component_name")
        normalized = key.lower()
        if normalized not in allowed or normalized in result:
            _refuse("temporal_component_fields", field=key)
        if normalized == "timezone" and type(component) is not str:
            _refuse("temporal_component_type", field="timezone")
        result[normalized] = component
    return result


def _date(value) -> DateValue:
    if type(value) is DateValue:
        return value
    if type(value) is LocalDateTimeValue:
        return value.date
    if type(value) is DateTimeValue:
        return value.local.date
    _refuse("temporal_component_type", field="date")


def _time(value) -> LocalTimeValue:
    if type(value) is LocalTimeValue:
        return value
    if type(value) is TimeValue:
        return value.time
    if type(value) is LocalDateTimeValue:
        return value.time
    if type(value) is DateTimeValue:
        return value.local.time
    _refuse("temporal_component_type", field="time")


def _prefix(fields: dict, names: tuple[str, ...]) -> None:
    missing = None
    for name in names:
        if name not in fields:
            missing = missing or name
        elif missing is not None:
            _refuse("temporal_missing_component", field=missing)


def _calendar(fields: dict, base: DateValue | None = None) -> DateValue:
    if not fields.keys() & _DATE and base is not None:
        return base
    families = [names for names in (("month","day"),("week","dayofweek"),
                                    ("quarter","dayofquarter"),("ordinalday",)) if fields.keys() & set(names)]
    if len(families) > 1:
        _refuse("temporal_conflicting_components", field="date")
    family = families[0] if families else ("month","day")
    if base is None:
        if "year" not in fields:
            _refuse("temporal_missing_component", field="year")
        _prefix(fields, ("year", *family))
    year = fields.get("year", base.week_year if base and family[0] == "week" else base.year if base else 0)
    if family[0] == "week":
        return DateValue.from_week_date(year, fields.get("week", base.week if base else 1),
                                        fields.get("dayofweek", base.day_of_week if base else 1))
    if family[0] == "ordinalday":
        return DateValue.from_ordinal_day(year, fields["ordinalday"])
    if family[0] == "quarter":
        quarter = _integer(fields.get("quarter", base.quarter if base else 1), "quarter", 1, 4)
        month = 3 * quarter - 2
        if "dayofquarter" in fields:
            maximum = sum(days_in_month(year, m) for m in range(month, month + 3))
            day = _integer(fields["dayofquarter"], "dayOfQuarter", 1, maximum)
            return DateValue(year, month).add_days(day - 1)
        if base:
            month += (base.month - 1) % 3
        return DateValue(year, month, min(base.day, days_in_month(year, month)) if base else 1)
    month = fields.get("month", base.month if base else 1)
    day = fields.get("day", min(base.day, days_in_month(year, month)) if base else 1)
    return DateValue(year, month, day)


def _clock(fields: dict, base: LocalTimeValue | None = None) -> LocalTimeValue:
    if base is None:
        _prefix(fields, ("hour","minute","second"))
        if fields.keys() & {"millisecond","microsecond","nanosecond"} and "second" not in fields:
            _refuse("temporal_missing_component", field="second")
    fractions = ("millisecond","microsecond","nanosecond")
    present = [name for name in fractions if name in fields]
    nanos = base.nanosecond if base else 0
    if present:
        nanos, preceding_unit = 0, 1_000_000_000
        for name, unit in zip(fractions, (1_000_000, 1000, 1)):
            if name in fields:
                nanos += _integer(fields[name], name, 0, preceding_unit // unit - 1) * unit
                preceding_unit = unit
    return LocalTimeValue.from_components(fields.get("hour", base.hour if base else 0),
        fields.get("minute", base.minute if base else 0), fields.get("second", base.second if base else 0), nanos)


def build_date(value: object) -> DateValue:
    """Construct a native date from validated calendar components or a source date."""
    fields = _fields(value, _DATE | {"date"})
    return _calendar(fields, _date(fields["date"]) if "date" in fields else None)


def build_localtime(value: object) -> LocalTimeValue:
    """Construct a native local time from validated clock components or a source time."""
    fields = _fields(value, _TIME | {"time"})
    return _clock(fields, _time(fields["time"]) if "time" in fields else None)


def _local(fields: dict) -> LocalDateTimeValue:
    if "datetime" in fields:
        if "date" in fields or "time" in fields:
            _refuse("temporal_conflicting_components", field="datetime")
        date_base, time_base = _date(fields["datetime"]), _time(fields["datetime"])
    else:
        date_base = _date(fields["date"]) if "date" in fields else None
        time_base = _time(fields["time"]) if "time" in fields else None
    date = _calendar(fields, date_base)
    if date_base is None and time_base is None and fields.keys() & _TIME:
        family = (("week","dayofweek") if "week" in fields else
                  ("quarter","dayofquarter") if "quarter" in fields else
                  ("ordinalday",) if "ordinalday" in fields else ("month","day"))
        _prefix(fields, ("year", *family, "hour","minute","second"))
    return LocalDateTimeValue(date, _clock(fields, time_base))


def build_localdatetime(value: object) -> LocalDateTimeValue:
    """Construct an unzoned datetime from validated date and time components."""
    return _local(_fields(value, _DATE | _TIME | {"date","time","datetime"}))


def _zone(local: LocalDateTimeValue, zone, resolver: TemporalZoneResolver | None) -> DateTimeValue:
    if type(zone) is int:
        return DateTimeValue(local, zone)
    if type(zone) is not str:
        _refuse("temporal_component_type", field="timezone")
    if zone == "Z" or zone.startswith(("+","-")):
        return DateTimeValue(local, parse_offset(zone))
    if resolver is None:
        raise GrafxUnsupportedOperation("Named zone construction requires an explicit provider.", field="temporal_timezone_provider")
    return resolver.at_local(local, zone)


def _at_zone(instant: DateTimeValue, zone, resolver: TemporalZoneResolver | None) -> DateTimeValue:
    if type(zone) is int or (type(zone) is str and (zone == "Z" or zone.startswith(("+","-")))):
        offset = zone if type(zone) is int else parse_offset(zone)
        return DateTimeValue.from_epoch_parts(instant.epoch_seconds, instant.nanosecond, offset_seconds=offset)
    if type(zone) is not str:
        _refuse("temporal_component_type", field="timezone")
    if resolver is None:
        raise GrafxUnsupportedOperation("Named zone conversion requires an explicit provider.", field="temporal_timezone_provider")
    return resolver.at_instant(instant.epoch_seconds, instant.nanosecond, zone)


def build_datetime(value: object, *, resolver: TemporalZoneResolver | None = None, default_offset: int = 0) -> DateTimeValue:
    """Construct an offset or named-zone datetime from component or epoch fields."""
    _offset(default_offset)
    fields = _fields(value, _DATE | _TIME | {"date","time","datetime","timezone","epochseconds","epochmillis"})
    if "epochseconds" in fields or "epochmillis" in fields:
        seconds_mode = "epochseconds" in fields
        allowed = {"epochseconds","nanosecond","timezone"} if seconds_mode else {"epochmillis","timezone"}
        if fields.keys() - allowed:
            _refuse("temporal_conflicting_components", field="epoch")
        instant = (datetime_from_epoch(fields["epochseconds"], fields.get("nanosecond", 0)) if seconds_mode
                   else datetime_from_epoch_millis(fields["epochmillis"]))
        return _at_zone(instant, fields.get("timezone", default_offset), resolver)
    local = _local(fields)
    source = fields.get("time", fields.get("datetime"))
    inherited = source.zone or source.offset_seconds if type(source) is DateTimeValue else (
        source.offset_seconds if type(source) is TimeValue else None)
    if inherited is not None:
        origin = source if type(source) is DateTimeValue and local == source.local else _zone(local, inherited, resolver)
        return _at_zone(origin, fields["timezone"], resolver) if "timezone" in fields else origin
    return _zone(local, fields.get("timezone", default_offset), resolver)


def build_time(value: object, *, resolver: TemporalZoneResolver | None = None,
               reference_date: DateValue | None = None, default_offset: int = 0) -> TimeValue:
    """Construct an offset time, using an explicit reference date for named-zone resolution."""
    _offset(default_offset)
    if reference_date is not None:
        _component(reference_date, DateValue, "reference_date")
    fields = _fields(value, _TIME | {"time","timezone"})
    source = fields.get("time")
    local = _clock(fields, _time(source) if "time" in fields else None)
    origin = source.offset_seconds if type(source) in (DateTimeValue,TimeValue) else None
    if "timezone" not in fields and origin is not None:
        return TimeValue(local, origin)
    zone = fields.get("timezone", default_offset)
    if type(zone) is int or (type(zone) is str and (zone == "Z" or zone.startswith(("+","-")))):
        offset = zone if type(zone) is int else parse_offset(zone)
        shifted = (local.nanoseconds + (offset - origin) * 1_000_000_000) % NANOSECONDS_PER_DAY if origin is not None else local.nanoseconds
        return TimeValue(LocalTimeValue(shifted), offset)
    date = source.local.date if type(source) is DateTimeValue else reference_date
    if date is None:
        raise GrafxUnsupportedOperation("Named time needs an explicit reference date.", field="temporal_timezone_provider")
    combined = LocalDateTimeValue(date, local)
    result = _at_zone(DateTimeValue(combined, origin), zone, resolver) if origin is not None else _zone(combined, zone, resolver)
    return TimeValue(result.local.time, result.offset_seconds)


def datetime_from_epoch(seconds: int, nanosecond: int = 0) -> DateTimeValue:
    """Construct a UTC datetime from whole epoch seconds and a nanosecond remainder."""
    return DateTimeValue.from_epoch_parts(seconds, nanosecond)


def datetime_from_epoch_millis(milliseconds: int) -> DateTimeValue:
    """Convert signed epoch milliseconds to exact UTC seconds and nanoseconds."""
    _integer(milliseconds, "epochMillis", _MIN_I64, _MAX_I64)
    seconds, remainder = divmod(milliseconds, 1000)
    return DateTimeValue.from_epoch_parts(seconds, remainder * 1_000_000)


def build_duration(value: object) -> DurationValue:
    """Construct a duration from bounded numeric components, rejecting nonfinite values."""
    fields = _fields(value, _DURATION)
    numbers = {}
    for name, number in fields.items():
        if type(number) not in (int, float) or (type(number) is float and not math.isfinite(number)):
            _refuse("temporal_component_type", field=name)
        if type(number) is int:
            _integer(number, name, _MIN_I64, _MAX_I64)
        numbers[name] = Fraction(str(number)) if type(number) is float else Fraction(number)
    months = numbers.get("years", 0) * 12 + numbers.get("months", 0)
    days = numbers.get("weeks", 0) * 7 + numbers.get("days", 0) + (months - int(months)) * Fraction(2629746, 86400)
    seconds = (numbers.get("hours", 0) * 3600 + numbers.get("minutes", 0) * 60 + numbers.get("seconds", 0)
               + (days - int(days)) * 86400)
    nanos = int(seconds * 1_000_000_000 + numbers.get("milliseconds", 0) * 1_000_000
                + numbers.get("microseconds", 0) * 1000 + numbers.get("nanoseconds", 0))
    whole, remainder = divmod(nanos, 1_000_000_000)
    return DurationValue(int(months), int(days), whole, remainder)


__all__ = [
    'build_date',
    'build_localtime',
    'build_localdatetime',
    'build_datetime',
    'build_time',
    'datetime_from_epoch',
    'datetime_from_epoch_millis',
    'build_duration',
]
