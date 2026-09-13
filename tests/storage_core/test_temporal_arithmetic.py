"""Arithmetic component proofs, not native TCK query/storage qualification."""

from datetime import datetime, timedelta, timezone
import random

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue,
    TimeValue, MAX_YEAR, MIN_YEAR,
)
from okto_grafx.domain.temporal_arithmetic import add_duration, subtract_duration, scale_duration
from okto_grafx.domain.temporal_components import build_duration
from okto_grafx.domain.temporal_text import parse_datetime, parse_duration

_DURATIONS = [
    build_duration(dict(years=12, months=5, days=14, hours=16, minutes=12, seconds=70, nanoseconds=2)),
    build_duration(dict(months=1, days=-14, hours=16, minutes=-12, seconds=70)),
    build_duration(dict(years=12.5, months=5.5, days=14.5, hours=16.5, minutes=12.5, seconds=70.5, nanoseconds=3)),
]
_DATE = DateValue(1984, 10, 11)
_TIME = LocalTimeValue.from_components(12, 31, 14, 1)


@pytest.mark.parametrize("index,date_sum,date_diff,time_sum,time_diff,datetime_sum,datetime_diff", [
    (0, "1997-03-25", "1972-04-27", "04:44:24.000000003", "20:18:03.999999999", "1997-03-26", "1972-04-26"),
    (1, "1984-10-28", "1984-09-25", "04:20:24.000000001", "20:42:04.000000001", "1984-10-29", "1984-09-24"),
    (2, "1997-10-11", "1971-10-12", "22:29:27.500000004", "02:33:00.499999998", "1997-10-11", "1971-10-12"),
])
@pytest.mark.parametrize("kind", ["date", "localtime", "time", "localdatetime", "datetime"])
def test_original_temporal8_instant_arithmetic_pairs(index, date_sum, date_diff, time_sum, time_diff, datetime_sum, datetime_diff, kind):
    local = LocalDateTimeValue(_DATE, _TIME)
    value = {"date":_DATE, "localtime":_TIME, "time":TimeValue(_TIME,3600),
             "localdatetime":local, "datetime":DateTimeValue(local,3600)}[kind]
    expected = {"date":(date_sum,date_diff), "localtime":(time_sum,time_diff),
                "time":(time_sum+"+01:00",time_diff+"+01:00"),
                "localdatetime":(datetime_sum+"T"+time_sum,datetime_diff+"T"+time_diff),
                "datetime":(datetime_sum+"T"+time_sum+"+01:00",datetime_diff+"T"+time_diff+"+01:00")}[kind]
    assert add_duration(value,_DURATIONS[index]).isoformat() == expected[0]
    assert subtract_duration(value,_DURATIONS[index]).isoformat() == expected[1]


@pytest.mark.parametrize("i,j,total,difference", [
    (0,0,"P24Y10M28DT32H26M20.000000002S","PT0S"),
    (0,1,"P12Y6MT32H2M20.000000001S","P12Y4M28DT24M0.000000001S"),
    (0,2,"P25Y4M43DT50H11M23.500000004S","P-6M-15DT-17H-45M-3.500000002S"),
    (1,0,"P12Y6MT32H2M20.000000001S","P-12Y-4M-28DT-24M-0.000000001S"),
    (1,1,"P2M-28DT31H38M20S","PT0S"),
    (1,2,"P13Y15DT49H47M23.500000003S","P-12Y-10M-43DT-18H-9M-3.500000003S"),
    (2,0,"P25Y4M43DT50H11M23.500000004S","P6M15DT17H45M3.500000002S"),
    (2,1,"P13Y15DT49H47M23.500000003S","P12Y10M43DT18H9M3.500000003S"),
    (2,2,"P25Y10M58DT67H56M27.000000006S","PT0S"),
])
def test_original_temporal8_duration_pairs(i,j,total,difference):
    durations = [DurationValue(149,14,58390,1),*_DURATIONS[1:]]
    assert add_duration(durations[i],durations[j]).isoformat() == total
    assert subtract_duration(durations[i],durations[j]).isoformat() == difference


@pytest.mark.parametrize("factor,product,quotient", [
    (1,"P12Y5M14DT16H13M10.000000001S","P12Y5M14DT16H13M10.000000001S"),
    (2,"P24Y10M28DT32H26M20.000000002S","P6Y2M22DT13H21M8S"),
    (0.5,"P6Y2M22DT13H21M8S","P24Y10M28DT32H26M20.000000002S"),
])
def test_original_temporal8_scale_pairs(factor,product,quotient):
    value = DurationValue(149,14,58390,1)
    assert scale_duration(value,factor).isoformat() == product
    assert scale_duration(value,factor,divide=True).isoformat() == quotient


def test_clamping_order_noninvertibility_and_ignored_components():
    original = DateValue(2011,1,31)
    month = DurationValue(months=1)
    assert add_duration(original,month) == DateValue(2011,2,28)
    assert subtract_duration(add_duration(original,month),month) == DateValue(2011,1,28)
    assert add_duration(add_duration(original,month),DurationValue(months=12)) == DateValue(2012,2,28)
    assert add_duration(original,DurationValue(months=13)) == DateValue(2012,2,29)
    assert add_duration(original,DurationValue(seconds=86399,nanoseconds=999999999)) == original
    assert add_duration(original,DurationValue(seconds=86400*100)) == original.add_days(100)
    assert add_duration(_TIME,DurationValue(months=100,days=100)) == _TIME


@pytest.mark.parametrize("source,day,hours", [
    ("2024-03-09T12:00-05:00[America/New_York]","2024-03-10T12:00-04:00[America/New_York]","2024-03-10T13:00-04:00[America/New_York]"),
    ("2024-11-02T12:00-04:00[America/New_York]","2024-11-03T12:00-05:00[America/New_York]","2024-11-03T11:00-05:00[America/New_York]"),
    ("2024-10-05T12:00+10:30[Australia/Lord_Howe]","2024-10-06T12:00+11:00[Australia/Lord_Howe]","2024-10-06T12:30+11:00[Australia/Lord_Howe]"),
])
def test_calendar_days_are_not_elapsed_24_hours(source,day,hours):
    resolver = ZoneInfoTemporalResolver()
    value = parse_datetime(source,resolver=resolver)
    assert add_duration(value,DurationValue(days=1),resolver=resolver).isoformat() == day
    assert add_duration(value,DurationValue(seconds=86400),resolver=resolver).isoformat() == hours


def test_gap_shift_and_overlap_offset_retention():
    resolver = ZoneInfoTemporalResolver()
    gap = parse_datetime("2024-03-09T02:30:00.000000007[America/New_York]",resolver=resolver)
    assert add_duration(gap,DurationValue(days=1),resolver=resolver).isoformat() == "2024-03-10T03:30:00.000000007-04:00[America/New_York]"
    earlier = parse_datetime("2024-11-02T01:30-04:00[America/New_York]",resolver=resolver)
    later = parse_datetime("2024-11-04T01:30-05:00[America/New_York]",resolver=resolver)
    assert add_duration(earlier,DurationValue(days=1),resolver=resolver).offset_seconds == -4*3600
    assert subtract_duration(later,DurationValue(days=1),resolver=resolver).offset_seconds == -5*3600
    # Resolve month first: its gap correction remains in the subsequent day step.
    feb = parse_datetime("2024-02-10T02:30[America/New_York]",resolver=resolver)
    assert add_duration(feb,DurationValue(months=1,days=1),resolver=resolver).isoformat() == "2024-03-11T03:30-04:00[America/New_York]"


def test_exact_negative_fraction_scaling_and_int64_cancellation():
    assert scale_duration(DurationValue(nanoseconds=-1),0.5) == DurationValue()
    assert scale_duration(DurationValue(nanoseconds=-3),0.5) == DurationValue(nanoseconds=-1)
    assert scale_duration(DurationValue(months=1),-0.5) == parse_duration("P-15DT-5H-14M-33S")
    maximum = DurationValue(seconds=(1<<63)-1,nanoseconds=999999999)
    assert scale_duration(maximum,1) == maximum
    assert subtract_duration(maximum,maximum) == DurationValue()
    minimum = DurationValue(seconds=-(1<<63))
    assert subtract_duration(minimum,minimum) == DurationValue()


@pytest.mark.parametrize("seconds,nanos,days", [
    (86399,999999999,0),(86400,0,1),(-86399,0,0),(-86400,999999999,-1),(-86401,0,-1),
])
def test_date_normalized_seconds_use_signed_truncation(seconds,nanos,days):
    duration = DurationValue(seconds=seconds,nanoseconds=nanos)
    assert add_duration(_DATE,duration) == _DATE.add_days(days)
    assert subtract_duration(_DATE,duration) == _DATE.add_days(-days)


@pytest.mark.parametrize("factor",[True,None,"2",float("nan"),float("inf"),float("-inf"),1<<63])
def test_invalid_scale_operands_refuse(factor):
    with pytest.raises(SchemaMismatchError):
        scale_duration(DurationValue(seconds=1),factor)


@pytest.mark.parametrize("zero",[0,0.0,-0.0])
def test_zero_divisor_refuses(zero):
    with pytest.raises(SchemaMismatchError,match="divided by zero"):
        scale_duration(DurationValue(),zero,divide=True)


@pytest.mark.parametrize("value,duration",[(object(),DurationValue()),(_DATE,object()),(_TIME,None)])
def test_unknown_operands_refuse_without_coercion(value,duration):
    with pytest.raises(SchemaMismatchError):
        add_duration(value,duration)


@pytest.mark.parametrize("value,duration",[
    (DateValue(MAX_YEAR,12,31),DurationValue(days=1)),
    (DateValue(MIN_YEAR,1,1),DurationValue(days=-1)),
    (DurationValue(months=(1<<63)-1),DurationValue(months=1)),
    (DurationValue(seconds=(1<<63)-1,nanoseconds=999999999),DurationValue(nanoseconds=1)),
])
def test_result_bounds_refuse_without_mutating_inputs(value,duration):
    before = value.isoformat()
    with pytest.raises(SchemaMismatchError):
        add_duration(value,duration)
    assert value.isoformat() == before


def test_named_arithmetic_requires_rules_but_zero_preserves_recorded_identity():
    value = DateTimeValue(LocalDateTimeValue(_DATE,_TIME),3600,"Europe/Stockholm")
    assert add_duration(value,DurationValue()) is value
    with pytest.raises(GrafxUnsupportedOperation):
        add_duration(value,DurationValue(seconds=1))


def test_bad_zone_provider_cannot_change_elapsed_instant():
    class BadProvider:
        def at_instant(self,seconds,nano,zone):
            return DateTimeValue.from_epoch_parts(seconds+1,nano,zone=zone)
    value = DateTimeValue(LocalDateTimeValue(_DATE,_TIME),3600,"Europe/Stockholm")
    with pytest.raises(GrafxCorruptionDetected):
        add_duration(value,DurationValue(seconds=1),resolver=BadProvider())


def test_skipped_calendar_day_and_expanded_years():
    resolver = ZoneInfoTemporalResolver()
    value = parse_datetime("2011-12-29T12:00[Pacific/Apia]",resolver=resolver)
    assert add_duration(value,DurationValue(days=1),resolver=resolver).isoformat() == "2011-12-31T12:00+14:00[Pacific/Apia]"
    assert add_duration(DateValue(-1,12,31),DurationValue(days=1)) == DateValue(0,1,1)
    expanded = parse_datetime("+12000-02-29T12:00[Europe/Stockholm]",resolver=resolver)
    assert add_duration(expanded,DurationValue(months=1),resolver=resolver).local.date == DateValue(12000,3,29)


@pytest.mark.parametrize("factor,divide", [(1e308,False),(5e-324,True),(1,1)])
def test_scale_overflow_and_nonboolean_mode_refuse(factor,divide):
    with pytest.raises(SchemaMismatchError):
        scale_duration(DurationValue(months=1,seconds=1),factor,divide=divide)


def test_arithmetic_results_roundtrip_native_codec():
    from okto_grafx.domain.model.value import encode_value, decode_value
    local = LocalDateTimeValue(_DATE,_TIME)
    for value in (_DATE,_TIME,TimeValue(_TIME,0),local,DateTimeValue(local,0),DurationValue()):
        result = add_duration(value,DurationValue(seconds=1))
        assert decode_value(encode_value(result))[0] == result


def test_independent_fixed_offset_elapsed_reference_and_inverse():
    rng = random.Random(0xA81)
    for _ in range(1500):
        year = rng.randrange(100,9900)
        source = datetime(year,rng.randrange(1,13),rng.randrange(1,29),rng.randrange(24),rng.randrange(60),rng.randrange(60),tzinfo=timezone.utc)
        nanos = rng.randrange(1_000_000_000)
        value = DateTimeValue(LocalDateTimeValue(DateValue(source.year,source.month,source.day),
                             LocalTimeValue.from_components(source.hour,source.minute,source.second,nanos)),rng.randrange(-64800,64801))
        duration = DurationValue(seconds=rng.randrange(-1_000_000,1_000_000),nanoseconds=rng.randrange(1_000_000_000))
        result = add_duration(value,duration)
        expected = source + timedelta(seconds=duration.seconds+(nanos+duration.nanoseconds)//1_000_000_000)
        assert result.local.date == DateValue(expected.year,expected.month,expected.day)
        assert (result.local.time.hour,result.local.time.minute,result.local.time.second) == (expected.hour,expected.minute,expected.second)
        assert result.nanosecond == (nanos+duration.nanoseconds)%1_000_000_000
        assert subtract_duration(result,duration) == value
