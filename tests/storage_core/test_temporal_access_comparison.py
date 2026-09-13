"""Temporal field/predicate/order component proofs; native query wiring is pending."""

from itertools import product
import random

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, DurationValue, MAX_YEAR,
)
from okto_grafx.domain.temporal_access import temporal_field
from okto_grafx.domain.temporal_comparison import temporal_order_key, temporal_predicate
from okto_grafx.domain.temporal_text import (
    parse_date, parse_localtime, parse_time, parse_datetime, parse_localdatetime, parse_duration,
)

_DATE_FIELDS = "year quarter month week weekYear day ordinalDay weekDay dayOfQuarter"
_TIME_FIELDS = "hour minute second millisecond microsecond nanosecond"
_ZONE_FIELDS = "timezone offset offsetMinutes offsetSeconds"
_DURATION_FIELDS = ("years quarters months weeks days hours minutes seconds milliseconds microseconds nanoseconds "
                    "quartersOfYear monthsOfQuarter monthsOfYear daysOfWeek minutesOfHour secondsOfMinute "
                    "millisecondsOfSecond microsecondsOfSecond nanosecondsOfSecond")
_DAY = [1984,4,11,45,1984,11,316,7,42]
_CLOCK = [12,31,14,645,645876,645876123]


@pytest.mark.parametrize("value,fields,expected", [
    (parse_date("1984-10-11"),_DATE_FIELDS,[1984,4,10,41,1984,11,285,4,11]),
    (parse_date("1984-01-01"),"year weekYear week weekDay",[1984,1983,52,7]),
    (parse_localtime("12:31:14.645876123"),_TIME_FIELDS,_CLOCK),
    (parse_time("12:31:14.645876123+01:00"),_TIME_FIELDS+" "+_ZONE_FIELDS,_CLOCK+["+01:00","+01:00",60,3600]),
    (parse_localdatetime("1984-11-11T12:31:14.645876123"),_DATE_FIELDS+" "+_TIME_FIELDS,_DAY+_CLOCK),
    (parse_datetime("1984-11-11T12:31:14.645876123[Europe/Stockholm]",resolver=ZoneInfoTemporalResolver()),
     _DATE_FIELDS+" "+_TIME_FIELDS+" "+_ZONE_FIELDS+" epochSeconds epochMillis",_DAY+_CLOCK+["Europe/Stockholm","+01:00",60,3600,469020674,469020674645]),
    (parse_duration("P1Y4M10DT1H1M1.111111111S"),_DURATION_FIELDS,
     [1,5,16,1,10,1,61,3661,3661111,3661111111,3661111111111,1,1,4,3,1,1,111,111111,111111111]),
])
def test_all_seven_original_temporal5_accessor_result_vectors(value,fields,expected):
    assert [temporal_field(value,name) for name in fields.split()] == expected
    assert [temporal_field(value,name.upper()) for name in fields.split()] == expected


@pytest.mark.parametrize("parse,left,right", [
    (parse_date,"1980-12-24","1984-10-11"),
    (parse_localtime,"10:35","12:31:14.645876123"),
    (parse_time,"10:00+01:00","09:35:14.645876123Z"),
    (parse_localdatetime,"1980-12-11T12:31:14","1984-10-11T12:31:14.645876123"),
    (parse_datetime,"1980-12-11T12:31:14Z","1984-10-11T12:31:14+05:00"),
])
@pytest.mark.parametrize("equal",[False,True])
def test_original_temporal7_instant_comparison_vectors(parse,left,right,equal):
    left, right = parse(right if equal else left),parse(right)
    expected = [False,False,True,True,True] if equal else [False,True,False,True,False]
    assert [temporal_predicate(left,right,op) for op in (">","<",">=","<=","=")] == expected


@pytest.mark.parametrize("other,expected", [
    (parse_date("1984-10-11"),False),
    (parse_localtime("12:31:14.645876123"),False),
    (parse_time("09:35:14.645876123Z"),False),
    (parse_localdatetime("1984-10-11T12:31:14.645876123"),False),
    (parse_datetime("1984-10-11T12:31:14+05:00"),False),
    (parse_duration("P12Y5M14DT16H12M70S"),True),
    (parse_duration("P12Y5M14DT16H13M10S"),True),
    (parse_duration("P12Y5M13DT40H13M10S"),False),
])
def test_original_temporal7_duration_equality_cases(other,expected):
    assert temporal_predicate(parse_duration("P12Y5M14DT16H12M70S"),other,"=") is expected


def test_negative_duration_truncation_normalized_subseconds_and_component_isolation():
    value = DurationValue(-16,-10,-3662,888888889)
    assert [temporal_field(value,f) for f in _DURATION_FIELDS.split()] == [
        -1,-5,-16,-1,-10,-1,-61,-3662,-3661112,-3661111112,-3661111111111,
        -1,-1,-4,-3,-1,-2,888,888888,888888889]
    value = DurationValue(months=12,days=100,seconds=3661)
    assert temporal_field(value,"hours") == 1
    assert temporal_field(value,"days") == 100
    assert temporal_field(value,"months") == 12


def test_calendar_aliases_recorded_offset_and_negative_epoch_fields():
    assert temporal_field(parse_date("1984-01-01"),"dayOfWeek") == 7
    value = DateTimeValue.from_epoch_parts(-1,999999999,offset_seconds=-75,zone="Europe/London")
    assert temporal_field(value,"epochSeconds") == -1
    assert temporal_field(value,"epochMillis") == -1
    assert temporal_field(value,"offsetMinutes") == -1
    assert temporal_field(value,"offsetSeconds") == -75
    assert temporal_field(value,"offset") == "-00:01:15"
    assert temporal_field(value,"timezone") == "Europe/London"
    assert temporal_field(parse_time("00:00Z"),"timezone") == "Z"


@pytest.mark.parametrize("value,field", [
    (DateValue(2000),"hour"),(LocalTimeValue(0),"year"),(LocalTimeValue(0),"offset"),
    (parse_localdatetime("2000-01-01T00:00"),"epochSeconds"),(parse_time("00:00Z"),"epochMillis"),
    (DurationValue(),"year"),(DateValue(2000),"__class__"),(DateValue(2000),"time"),
])
def test_unknown_or_cross_family_fields_refuse(value,field):
    with pytest.raises(SchemaMismatchError,match="not available"):
        temporal_field(value,field)


@pytest.mark.parametrize("field",[None,1,"","a"*33,"yéar"])
def test_bad_field_names_refuse(field):
    with pytest.raises(SchemaMismatchError):
        temporal_field(DateValue(2000),field)


@pytest.mark.parametrize("value,field", [
    (DurationValue(seconds=(1<<63)-1),"milliseconds"),
    (DurationValue(seconds=-(1<<63)),"nanoseconds"),
    (DateTimeValue(LocalDateTimeValue(DateValue(MAX_YEAR,12,31),LocalTimeValue(0)),0),"epochMillis"),
])
def test_large_field_results_refuse_instead_of_wrapping(value,field):
    with pytest.raises(SchemaMismatchError,match="outside"):
        temporal_field(value,field)
    assert type(temporal_field(value,"seconds" if type(value) is DurationValue else "year")) is int


def test_duration_order_and_predicates_are_not_interchangeable():
    month = DurationValue(months=1)
    same_average = DurationValue(seconds=2629746)
    assert temporal_order_key(same_average) < temporal_order_key(month)
    for op in ("<",">","<=",">="):
        assert temporal_predicate(month,same_average,op) is None
    for op in ("<",">"):
        assert temporal_predicate(month,month,op) is None
    for op in ("<=",">=","="):
        assert temporal_predicate(month,month,op) is True
    day = DurationValue(days=1)
    seconds = DurationValue(seconds=86400)
    assert temporal_order_key(seconds) < temporal_order_key(day)
    assert temporal_predicate(day,seconds,"=") is False
    assert temporal_predicate(day,seconds,"<>") is True


def test_same_instant_different_offset_and_name_remain_distinct():
    values = [DateTimeValue.from_epoch_parts(0,offset_seconds=-3600),
              DateTimeValue.from_epoch_parts(0),DateTimeValue.from_epoch_parts(0,zone="Etc/UTC"),
              DateTimeValue.from_epoch_parts(0,zone="UTC"),DateTimeValue.from_epoch_parts(0,offset_seconds=3600)]
    for first,second in zip(values,values[1:]):
        assert temporal_predicate(first,second,"=") is False
        assert temporal_predicate(first,second,"<") is True
    assert temporal_order_key(parse_time("00:00+01:00")) < temporal_order_key(parse_time("00:00Z"))
    assert temporal_predicate(parse_time("00:00Z"),parse_time("01:00+01:00"),"<") is True


def test_type_order_cross_family_predicates_and_nulls():
    values = [parse_datetime("2000-01-01T00:00Z"),parse_localdatetime("2000-01-01T00:00"),
              DateValue(2000),parse_time("00:00Z"),LocalTimeValue(0),DurationValue()]
    assert sorted(reversed(values),key=temporal_order_key) == values
    for i,j in product(range(6),repeat=2):
        if i != j:
            assert temporal_predicate(values[i],values[j],"=") is False
            assert temporal_predicate(values[i],values[j],"<=") is None
    for op in ("=","<>","<",">","<=",">="):
        assert temporal_predicate(None,None,op) is None
        assert temporal_predicate(None,values[0],op) is None
        assert temporal_predicate(values[0],None,op) is None


def test_no_host_callback_coercion_or_comparison():
    class Host:
        def __eq__(self,other):
            raise AssertionError("No host equality")
        def __str__(self):
            raise AssertionError("No host string conversion")
        def __getattr__(self,name):
            raise AssertionError("No host attribute access")
    value = Host()
    operations = [lambda:temporal_order_key(value),lambda:temporal_predicate(value,DateValue(2000),"="),
                  lambda:temporal_field(value,"year"),lambda:temporal_field(DateValue(2000),value),
                  lambda:temporal_predicate(None,None,value)]
    for operation in operations:
        with pytest.raises(SchemaMismatchError):
            operation()


@pytest.mark.parametrize("operator",["!=","==","lt","",None,"<"*100])
def test_unrecognized_operators_refuse_even_with_null_operands(operator):
    with pytest.raises(SchemaMismatchError):
        temporal_predicate(None,None,operator)


def test_no_integer_overflow_in_internal_duration_sort_keys():
    maximum = (1<<63)-1
    values = [DurationValue(months=maximum),DurationValue(days=maximum),DurationValue(seconds=maximum)]
    assert sorted(values,key=temporal_order_key) == list(reversed(values))
    assert temporal_field(values[0],"months") == maximum


def test_component_semantics_survive_codec_roundtrip():
    from okto_grafx.domain.model.value import encode_value, decode_value
    value = DateValue(2000)
    assert temporal_field(value,"year") == 2000
    assert temporal_predicate(value,value,"=") is True
    assert temporal_order_key(value)[1] == (value.epoch_day,)
    assert decode_value(encode_value(value))[0] == value


def test_duration_order_is_total_exact_and_equality_consistent():
    rng = random.Random(0xA77)
    values = [DurationValue(rng.randrange(-100000,100000),rng.randrange(-100000,100000),
                            rng.randrange(-(1<<63),(1<<63)-1),rng.randrange(1_000_000_000)) for _ in range(1500)]
    values.extend([DurationValue(days=1),DurationValue(seconds=86400),DurationValue(months=1),DurationValue(seconds=2629746)])
    values.sort(key=temporal_order_key)
    for first,second in zip(values,values[1:]):
        expected_a = first.months*2629746*1_000_000_000+first.days*86400*1_000_000_000+first.seconds*1_000_000_000+first.nanoseconds
        expected_b = second.months*2629746*1_000_000_000+second.days*86400*1_000_000_000+second.seconds*1_000_000_000+second.nanoseconds
        assert expected_a <= expected_b
        assert (temporal_order_key(first) == temporal_order_key(second)) == (first == second)
