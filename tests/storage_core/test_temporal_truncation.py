"""Native truncation contracts; no public query/storage admission implied."""

from datetime import date, timedelta
import random
from types import MappingProxyType

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.model.temporal_values import (
    DateValue, DateTimeValue, TimeValue, DurationValue, MIN_YEAR, MAX_YEAR,
)
from okto_grafx.domain.temporal_truncation import truncate_temporal
from okto_grafx.domain.temporal_text import parse_datetime, parse_localdatetime, parse_localtime

_LOCAL = parse_localdatetime("1984-10-11T12:31:14.645876123")
_SOURCES = [_LOCAL.date,_LOCAL,DateTimeValue(_LOCAL,3600)]
_CLOCK_SOURCES = [_LOCAL.time,TimeValue(_LOCAL.time,3600),_LOCAL,DateTimeValue(_LOCAL,3600)]


@pytest.mark.parametrize("unit,expected", [
    ("millennium","1000-01-01"),("century","1900-01-01"),("decade","1980-01-01"),
    ("year","1984-01-01"),("weekYear","1984-01-02"),("quarter","1984-10-01"),
    ("month","1984-10-01"),("week","1984-10-08"),("day","1984-10-11"),
])
@pytest.mark.parametrize("source",_SOURCES)
@pytest.mark.parametrize("kind",["date","localdatetime","datetime"])
def test_calendar_unit_source_and_result_family_cross_product(unit,expected,source,kind):
    suffix = "" if kind == "date" else "T00:00" if kind == "localdatetime" else (
        "T00:00+01:00" if type(source) is DateTimeValue else "T00:00Z")
    assert truncate_temporal(kind,unit,source).isoformat() == expected+suffix


@pytest.mark.parametrize("unit,base,override", [
    ("day","00:00","00:00:00.000000002"),
    ("hour","12:00","12:00:00.000000002"),
    ("minute","12:31","12:31:00.000000002"),
    ("second","12:31:14","12:31:14.000000002"),
    ("millisecond","12:31:14.645","12:31:14.645000002"),
    ("microsecond","12:31:14.645876","12:31:14.645876002"),
])
@pytest.mark.parametrize("source",_CLOCK_SOURCES)
@pytest.mark.parametrize("kind",["time","localtime"])
def test_clock_unit_source_and_precision_override_cross_product(unit,base,override,source,kind):
    suffix = "" if kind == "localtime" else "+01:00" if type(source) in (DateTimeValue,TimeValue) else "Z"
    assert truncate_temporal(kind,unit,source,{}).isoformat() == base+suffix
    assert truncate_temporal(kind,unit,source,{"nanosecond":2}).isoformat() == override+suffix


@pytest.mark.parametrize("unit,base,override", [
    ("hour","12:00","12:00:00.000000002"),("minute","12:31","12:31:00.000000002"),
    ("second","12:31:14","12:31:14.000000002"),("millisecond","12:31:14.645","12:31:14.645000002"),
    ("microsecond","12:31:14.645876","12:31:14.645876002"),
])
@pytest.mark.parametrize("source",_SOURCES[1:])
@pytest.mark.parametrize("kind",["datetime","localdatetime"])
def test_datetime_clock_units_keep_date(unit,base,override,source,kind):
    suffix = "" if kind == "localdatetime" else "+01:00" if type(source) is DateTimeValue else "Z"
    assert truncate_temporal(kind,unit,source).isoformat() == "1984-10-11T"+base+suffix
    assert truncate_temporal(kind,unit,source,{"nanosecond":2}).isoformat() == "1984-10-11T"+override+suffix


def test_timezone_override_is_wall_assignment_not_instant_conversion():
    resolver = ZoneInfoTemporalResolver()
    source = DateTimeValue(_LOCAL,-3600)
    result = truncate_temporal("datetime","hour",source,{"timezone":"Europe/Stockholm"},resolver=resolver)
    assert result.isoformat() == "1984-10-11T12:00+01:00[Europe/Stockholm]"
    result = truncate_temporal("time","hour",source,{"timezone":"+03:00"})
    assert result.isoformat() == "12:00+03:00"


def test_smaller_date_fields_and_normalized_subsecond_combination():
    assert truncate_temporal("date","year",_LOCAL,{"day":5}) == DateValue(1984,1,5)
    assert truncate_temporal("date","week",_LOCAL,{"dayOfWeek":2}) == DateValue(1984,10,9)
    assert truncate_temporal("date","weekYear",DateValue(1984,1,1),{"day":5}) == DateValue(1983,1,5)
    assert truncate_temporal("localtime","millisecond",_LOCAL,{"microsecond":7,"nanosecond":2}).isoformat() == "12:31:14.645007002"
    assert truncate_temporal("localtime","microsecond",_LOCAL,{"nanosecond":999}).isoformat() == "12:31:14.645876999"
    assert truncate_temporal("LOCALTIME","MILLISECOND",_LOCAL,MappingProxyType({"NANOSECOND":2})).isoformat() == "12:31:14.645000002"


def test_gap_overlap_and_current_rule_rebinding():
    resolver = ZoneInfoTemporalResolver()
    # This zone's historical change skipped 00:00. Day truncation resolves forward.
    source = parse_datetime("2018-11-04T12:00[America/Sao_Paulo]",resolver=resolver)
    assert truncate_temporal("datetime","day",source,resolver=resolver).isoformat() == "2018-11-04T01:00-02:00[America/Sao_Paulo]"
    # Unlike duration arithmetic, truncation resolves the wall boundary anew.
    later = parse_datetime("2024-11-03T01:30-05:00[America/New_York]",resolver=resolver)
    assert truncate_temporal("datetime","hour",later,resolver=resolver).isoformat() == "2024-11-03T01:00-04:00[America/New_York]"


def test_named_time_override_requires_explicit_reference_without_a_source_date():
    resolver = ZoneInfoTemporalResolver()
    with pytest.raises(GrafxUnsupportedOperation):
        truncate_temporal("time","hour",_LOCAL.time,{"timezone":"Europe/Stockholm"},resolver=resolver)
    for month,offset in [(1,"+01:00"),(7,"+02:00")]:
        result = truncate_temporal("time","hour",_LOCAL.time,{"timezone":"Europe/Stockholm"},
                                   resolver=resolver,reference_date=DateValue(2024,month,1))
        assert result.isoformat() == "12:00"+offset
    result = truncate_temporal("time","hour",parse_localtime("02:45"),{"timezone":"America/New_York"},
                               resolver=resolver,reference_date=DateValue(2024,3,10))
    assert result.isoformat() == "02:00-04:00"


@pytest.mark.parametrize("kind,unit,value",[
    ("date","hour",_LOCAL),("time","week",_LOCAL),("datetime","hour",_LOCAL.date),
    ("localdatetime","minute",_LOCAL.date),("localtime","day",_LOCAL.date),
    ("date","day",_LOCAL.time),("datetime","day",DurationValue()),
    ("duration","day",_LOCAL),("date","nanosecond",_LOCAL),("date",None,_LOCAL),
])
def test_invalid_selectors_and_missing_source_components_refuse(kind,unit,value):
    with pytest.raises(SchemaMismatchError):
        truncate_temporal(kind,unit,value)


@pytest.mark.parametrize("kind,unit,fields",[
    ("date","year",{"year":2000}),("date","day",{"month":1}),
    ("localtime","second",{"second":2}),("datetime","month",{"month":3}),
    ("localtime","microsecond",{"millisecond":5}),("date","year",{"timezone":"Z"}),
    ("localtime","hour",{"time":_LOCAL.time}),("datetime","year",{"date":_LOCAL.date}),
    ("date","year",{"day":1,"DAY":2}),("date","year",{"day":"2"}),
    ("localtime","millisecond",{"microsecond":1000}),
    ("localtime","microsecond",{"nanosecond":1000}),
])
def test_invalid_or_non_smaller_fields_refuse(kind,unit,fields):
    with pytest.raises(SchemaMismatchError):
        truncate_temporal(kind,unit,_LOCAL,fields)


def test_bad_configuration_and_map_shape_refuse_before_provider_calls():
    class Provider:
        def at_local(self,*args,**kwargs):
            raise AssertionError("invalid arguments must not reach provider")
    named = DateTimeValue(_LOCAL,3600,"Europe/Stockholm")
    for kwargs in ({"default_offset":True},{"reference_date":object()},{"fields":{"year":0}},
                   {"fields":[]},{"fields":{"timezone":None}},{"fields":{"day":1,"DAY":2}}):
        with pytest.raises(SchemaMismatchError):
            truncate_temporal("datetime","year",named,resolver=Provider(),**kwargs)
    with pytest.raises(GrafxUnsupportedOperation):
        truncate_temporal("datetime","day",named)


def test_negative_year_groups_and_calendar_bounds():
    # Explicit reference semantics: year grouping truncates signed division.
    assert truncate_temporal("date","century",DateValue(-1984,10,11)) == DateValue(-1900,1,1)
    assert truncate_temporal("date","decade",DateValue(-1,10,11)) == DateValue(0,1,1)
    assert truncate_temporal("date","day",DateValue(MIN_YEAR)) == DateValue(MIN_YEAR)
    assert truncate_temporal("date","month",DateValue(MAX_YEAR,12,31)) == DateValue(MAX_YEAR,12,1)


def test_calendar_boundaries_against_independent_stdlib_and_idempotence():
    rng = random.Random(0xA99)
    for _ in range(1500):
        source = date(rng.randrange(2,9999),rng.randrange(1,13),rng.randrange(1,29))
        native = DateValue(source.year,source.month,source.day)
        week_start = source-timedelta(days=source.isoweekday()-1)
        week_year_start = date.fromisocalendar(source.isocalendar().year,1,1)
        for unit,expected in [("week",week_start),("weekYear",week_year_start)]:
            result = truncate_temporal("date",unit,native)
            assert result == DateValue(expected.year,expected.month,expected.day)
            assert truncate_temporal("date",unit,result) == result


def test_truncation_results_roundtrip_native_codec():
    from okto_grafx.domain.model.value import encode_value, decode_value
    for kind in ("date","time","localtime","datetime","localdatetime"):
        result = truncate_temporal(kind,"day",_LOCAL)
        assert decode_value(encode_value(result))[0] == result
