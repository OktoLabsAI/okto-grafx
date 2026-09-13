"""Native temporal truncation and bounded smaller-field overrides.

Explicit sources only: clock overloads belong to query lifecycle integration.
"""

from __future__ import annotations

from types import MappingProxyType

from . import temporal_components as component
from .errors import GrafxUnsupportedOperation
from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    TemporalValue,
    DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, TimeValue,
    NANOSECONDS_PER_DAY, _component, _offset,
)
from .ports.temporal_zone import TemporalZoneResolver

_CALENDAR = {"millennium":0,"century":1,"decade":2,"year":3,"weekyear":3,
             "quarter":4,"month":5,"week":6,"day":7}
_CLOCK = {"hour":3600_000_000_000,"minute":60_000_000_000,"second":1_000_000_000,
          "millisecond":1_000_000,"microsecond":1000}
_FIELD_RANK = {"year":3,"quarter":4,"month":5,"week":6,"day":7,"dayofweek":7,
               "dayofquarter":7,"ordinalday":7,"hour":8,"minute":9,"second":10,
               "millisecond":11,"microsecond":12,"nanosecond":13}
_KINDS = frozenset({"date","datetime","localdatetime","time","localtime"})


def _name(value: str, allowed, field: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 16 or not value.isascii() or value.lower() not in allowed:
        raise SchemaMismatchError("Unknown temporal truncation selector.",field=field,reason="temporal_truncation_selector")
    return value.lower()


def _fields(value, kind: str, unit: str) -> dict:
    if value is None:
        return {}
    if type(value) not in (dict,MappingProxyType):
        raise SchemaMismatchError("Truncation fields require a native map.",reason="temporal_component_type")
    allowed = (component._DATE if kind == "date" else component._TIME if kind in ("time","localtime")
               else component._DATE | component._TIME)
    if kind in ("time","datetime"):
        allowed = allowed | {"timezone"}
    result = component._fields(value,allowed) if value else {}
    rank = _CALENDAR[unit] if unit in _CALENDAR else _FIELD_RANK[unit]
    if any(name != "timezone" and _FIELD_RANK[name] <= rank for name in result):
        raise SchemaMismatchError("Truncation overrides must be smaller than the unit.",reason="temporal_truncation_field_order")
    return result


def _date(date: DateValue, unit: str) -> DateValue:
    if unit in ("millennium","century","decade"):
        scale = {"millennium":1000,"century":100,"decade":10}[unit]
        # Match the pinned reference's signed year-group arithmetic, including
        # negative years; do not silently apply Python's floor division here.
        year = (abs(date.year)//scale)*scale*(-1 if date.year < 0 else 1)
        return DateValue(year)
    if unit == "year":
        return DateValue(date.year)
    if unit == "weekyear":
        return DateValue.from_week_date(date.week_year,1)
    if unit == "quarter":
        return DateValue(date.year,date.quarter*3-2)
    if unit == "month":
        return DateValue(date.year,date.month)
    if unit == "week":
        return date.add_days(1-date.day_of_week)
    return date


def _clock(time: LocalTimeValue, unit: str) -> LocalTimeValue:
    quantum = NANOSECONDS_PER_DAY if unit == "day" else _CLOCK[unit]
    return LocalTimeValue(time.nanoseconds//quantum*quantum)


def _overrides(time: LocalTimeValue, unit: str, fields: dict) -> dict:
    result = dict(fields)
    if unit == "millisecond" and fields.keys() & {"microsecond","nanosecond"}:
        result["millisecond"] = time.nanosecond//1_000_000
    elif unit == "microsecond" and "nanosecond" in fields:
        result["microsecond"] = time.nanosecond//1000
    return result


def truncate_temporal(kind: str, unit: str, value: object, fields: object = None, *,
                      resolver: TemporalZoneResolver | None = None,
                      reference_date: DateValue | None = None, default_offset: int = 0) -> TemporalValue:
    """Truncate an explicit native source, then apply smaller field overrides.

    Timezone overrides are same-local assignments, not instant conversions.
    Invalid configuration/field shape is rejected before provider interaction.
    """
    kind = _name(kind,_KINDS,"temporal_type")
    units = _CALENDAR if kind == "date" else ({"day",*_CLOCK} if kind in ("time","localtime") else {*_CALENDAR,*_CLOCK})
    unit = _name(unit,units,"unit")
    _offset(default_offset)
    if reference_date is not None:
        _component(reference_date,DateValue,"reference_date")
    fields = _fields(fields,kind,unit)
    if kind == "date":
        truncated = _date(component._date(value),unit)
        return component.build_date({"date":truncated,**fields}) if fields else truncated
    if kind in ("time","localtime"):
        time = _clock(component._time(value),unit)
        overrides = _overrides(time,unit,fields)
        zone = overrides.pop("timezone",None)
        time = component._clock(overrides,time)
        if kind == "localtime":
            return time
        offset = value.offset_seconds if type(value) in (TimeValue,DateTimeValue) else default_offset
        if zone is None:
            return TimeValue(time,offset)
        date = reference_date if reference_date is not None else value.local.date if type(value) is DateTimeValue else None
        selected = component.build_time({"time":time,"timezone":zone},resolver=resolver,
                                        reference_date=date,default_offset=default_offset)
        # The reference date selects rules, but an offset-time result has no
        # date/gap coordinate: a timezone override keeps the truncated clock.
        return TimeValue(time,selected.offset_seconds)
    date = component._date(value)
    if unit in _CALENDAR:
        local = LocalDateTimeValue(_date(date,unit),LocalTimeValue(0))
    else:
        if type(value) not in (LocalDateTimeValue,DateTimeValue):
            raise SchemaMismatchError("Time-unit datetime truncation requires a datetime source.",reason="temporal_component_type")
        local = LocalDateTimeValue(date,_clock(component._time(value),unit))
    overrides = _overrides(local.time,unit,fields)
    if kind == "localdatetime":
        return component.build_localdatetime({"datetime":local,**overrides}) if overrides else local
    # A named source is deliberately resolved at the truncated wall boundary;
    # unlike duration arithmetic, this does not prefer its old overlap offset.
    zone = value.zone if type(value) is DateTimeValue and value.zone is not None else (
        value.offset_seconds if type(value) is DateTimeValue else default_offset)
    if type(zone) is str and resolver is None:
        raise GrafxUnsupportedOperation("Named truncation requires a timezone provider.",field="temporal_timezone_provider")
    zoned = component._zone(local,zone,resolver)
    if "timezone" in overrides:
        zoned = component._zone(zoned.local,overrides.pop("timezone"),resolver)
    return component.build_datetime({"datetime":zoned,**overrides},resolver=resolver) if overrides else zoned


__all__ = [
    'truncate_temporal',
]
