"""Text/value tests, not native TCK query qualification or durable admission."""

import random

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue, TimeValue, days_in_month,
)
from okto_grafx.domain import temporal_text as text


# Input/output pairs from the pinned Temporal2 examples. These only qualify the
# native text/value layer; the original queries remain mandatory and unexecuted.
_EXAMPLES = {
    "date": [
        ("2015-07-21","2015-07-21"),("20150721","2015-07-21"),
        ("2015-07","2015-07-01"),("201507","2015-07-01"),
        ("2015-W30-2","2015-07-21"),("2015W302","2015-07-21"),
        ("2015-W30","2015-07-20"),("2015W30","2015-07-20"),
        ("2015-202","2015-07-21"),("2015202","2015-07-21"),("2015","2015-01-01")],
    "localtime": [("21:40:32.142","21:40:32.142"),("214032.142","21:40:32.142"),
        ("21:40:32","21:40:32"),("214032","21:40:32"),("21:40","21:40"),("2140","21:40"),("21","21:00")],
    "time": [("21:40:32.142+0100","21:40:32.142+01:00"),("214032.142Z","21:40:32.142Z"),
        ("21:40:32+01:00","21:40:32+01:00"),("214032-0100","21:40:32-01:00"),
        ("21:40-01:30","21:40-01:30"),("2140-00:00","21:40Z"),("2140-02","21:40-02:00"),("22+18:00","22:00+18:00")],
    "localdatetime": [("2015-07-21T21:40:32.142","2015-07-21T21:40:32.142"),
        ("2015-W30-2T214032.142","2015-07-21T21:40:32.142"),("2015-202T21:40:32","2015-07-21T21:40:32"),
        ("2015T214032","2015-01-01T21:40:32"),("20150721T21:40","2015-07-21T21:40"),
        ("2015-W30T2140","2015-07-20T21:40"),("2015202T21","2015-07-21T21:00")],
    "datetime": [("2015-07-21T21:40:32.142+0100","2015-07-21T21:40:32.142+01:00"),
        ("2015-W30-2T214032.142Z","2015-07-21T21:40:32.142Z"),("2015-202T21:40:32+01:00","2015-07-21T21:40:32+01:00"),
        ("2015T214032-0100","2015-01-01T21:40:32-01:00"),("20150721T21:40-01:30","2015-07-21T21:40-01:30"),
        ("2015-W30T2140-00:00","2015-07-20T21:40Z"),("2015-W30T2140-02","2015-07-20T21:40-02:00"),
        ("2015202T21+18:00","2015-07-21T21:00+18:00"),
        ("2015-07-21T21:40:32.142+02:00[Europe/Stockholm]","2015-07-21T21:40:32.142+02:00[Europe/Stockholm]"),
        ("2015-07-21T21:40:32.142+0845[Australia/Eucla]","2015-07-21T21:40:32.142+08:45[Australia/Eucla]"),
        ("2015-07-21T21:40:32.142-04[America/New_York]","2015-07-21T21:40:32.142-04:00[America/New_York]"),
        ("2015-07-21T21:40:32.142[Europe/London]","2015-07-21T21:40:32.142+01:00[Europe/London]"),
        ("1818-07-21T21:40:32.142[Europe/Stockholm]","1818-07-21T21:40:32.142+00:53:28[Europe/Stockholm]")],
    "duration": [("P14DT16H12M","P14DT16H12M"),("P5M1.5D","P5M1DT12H"),("P0.75M","P22DT19H51M49.5S"),
        ("PT0.75M","PT45S"),("P2.5W","P17DT12H"),("P12Y5M14DT16H12M70S","P12Y5M14DT16H13M10S"),
        ("P2012-02-02T14:37:21.545","P2012Y2M2DT14H37M21.545S")],
}


@pytest.mark.parametrize("kind,source,expected", [(kind,source,expected) for kind,examples in _EXAMPLES.items()
                                                for source,expected in examples])
def test_all_53_temporal2_text_examples_and_roundtrip(kind,source,expected):
    parse = getattr(text,"parse_" + kind)
    kwargs = {"resolver":ZoneInfoTemporalResolver()} if kind == "datetime" else {}
    value = parse(source,**kwargs)
    assert value.isoformat() == expected
    assert parse(value.isoformat(),**kwargs) == value


@pytest.mark.parametrize("source,expected", [("2015-Q2-60","2015-05-30"),("2015Q260","2015-05-30"),
    ("2015-Q2","2015-04-01"),("2015Q2","2015-04-01"),("2024-Q1-91","2024-03-31"),
    ("0000-02-29","0000-02-29"),("-1-01-01","-0001-01-01"),("+999999999-12-31","+999999999-12-31")])
def test_calendar_extended_forms(source,expected):
    assert text.parse_date(source).isoformat() == expected


@pytest.mark.parametrize("value,expected", [
    (DurationValue(months=149,days=14,seconds=16*3600+12*60+70,nanoseconds=1),"P12Y5M14DT16H13M10.000000001S"),
    (DurationValue(months=149,days=-14,seconds=16*3600),"P12Y5M-14DT16H"),
    (DurationValue(seconds=660),"PT11M"),(DurationValue(seconds=2,nanoseconds=-1000000),"PT1.999S"),
    (DurationValue(seconds=-2,nanoseconds=1000000),"PT-1.999S"),(DurationValue(seconds=-2,nanoseconds=-1000000),"PT-2.001S"),
    (DurationValue(days=1,nanoseconds=1000000),"P1DT0.001S"),(DurationValue(days=1,nanoseconds=-1000000),"P1DT-0.001S"),
    (DurationValue(seconds=60,nanoseconds=-1000000),"PT59.999S"),(DurationValue(seconds=-60,nanoseconds=1000000),"PT-59.999S"),
    (DurationValue(seconds=-60,nanoseconds=-1000000),"PT-1M-0.001S"),
])
def test_temporal6_duration_rendering_pairs(value,expected):
    assert value.isoformat() == expected
    assert text.parse_duration(expected) == value


def test_deterministic_full_range_duration_roundtrips():
    randomizer = random.Random(504)
    for _ in range(2000):
        value = DurationValue(months=randomizer.randrange(-(1<<63),1<<63),days=randomizer.randrange(-(1<<63),1<<63),
                              seconds=randomizer.randrange(-(1<<63),1<<63),nanoseconds=randomizer.randrange(1000000000))
        assert text.parse_duration(value.isoformat()) == value


def test_deterministic_full_range_calendar_clock_and_offset_roundtrips():
    randomizer = random.Random(506)
    for _ in range(2000):
        year, month = randomizer.randint(-999999999,999999999), randomizer.randint(1,12)
        date = DateValue(year,month,randomizer.randint(1,days_in_month(year,month)))
        clock = LocalTimeValue(randomizer.randrange(86400*1000000000))
        local = LocalDateTimeValue(date,clock)
        offset = randomizer.randint(-64800,64800)
        for kind,value in (("date",date),("localtime",clock),("localdatetime",local),
                           ("time",TimeValue(clock,offset)),("datetime",DateTimeValue(local,offset))):
            assert getattr(text,"parse_" + kind)(value.isoformat()) == value


@pytest.mark.parametrize("source,expected", [("-P1Y2M3DT4H","P-1Y-2M-3DT-4H"),("-P-1D","P1D"),
    ("PT-0.000000001S","PT-0.000000001S"),("P1,5D","P1DT12H"),("P0D","PT0S"),
    ("P0.000000000001D","PT0.000000086S"),("P0000-00-00T24:60:60","PT25H1M")])
def test_duration_signs_decimal_fraction_policy_and_quantity_form(source,expected):
    assert text.parse_duration(source).isoformat() == expected


@pytest.mark.parametrize("kind,source", [
    ("date","2023-02-29"),("date","2015-Q1-91"),("date","2015-W54"),("date","2015-2"),
    ("date","2015-Q2-"),("date","2015-W30-22"),("date","2015-07-21junk"),("date","２０１５"),
    ("localtime","24:00"),("localtime","12:60"),("localtime","12:00:60"),("localtime","12:00Z"),
    ("localtime","12:00:00.1234567890"),("localtime","12:00.5"),("time","12:00+18:01"),
    ("time","12:00+01:60"),("time","12:00+0100:00"),("datetime","2015T12:00[../UTC]"),
    ("time","12:00Z\n"),("time","12:00+01\n"),("datetime","2015T12:00Z\n"),
    ("duration","P"),("duration","PT"),("duration","P1DT"),("duration","--P1D"),("duration","P1.5DT0S"),
    ("duration","P1M1Y"),("duration","P1Y1Y"),("duration","P1.0Y0M"),("duration","PT1.0H0M"),
    ("duration","PNaND"),("duration","PT1.0000000001S"),("duration","P2012-13-01"),
    ("duration","P2012-01-32"),("duration","P2012-01-01T25"),
])
def test_invalid_text_never_coerces_or_discards_components(kind,source):
    with pytest.raises(SchemaMismatchError):
        getattr(text,"parse_" + kind)(source)


def test_named_construction_uses_only_explicit_context_and_checks_offset():
    resolver = ZoneInfoTemporalResolver()
    with pytest.raises(GrafxUnsupportedOperation):
        text.parse_datetime("2015T12[Europe/London]")
    with pytest.raises(SchemaMismatchError):
        text.parse_datetime("2015-07-21T12+00[Europe/London]",resolver=resolver)
    assert text.parse_datetime("2015-07-21[Europe/London]",resolver=resolver).offset_seconds == 3600
    with pytest.raises(GrafxUnsupportedOperation):
        text.parse_time("12[Europe/London]",resolver=resolver)
    assert text.parse_time("12[Europe/London]",resolver=resolver,reference_date=DateValue(2015,7,21)).offset_seconds == 3600
    assert text.parse_time("12").offset_seconds == 0


@pytest.mark.parametrize("nano,fraction", [(100000000,"100"),(123400000,"123400"),(123456700,"123456700"),(1,"000000001")])
def test_local_time_fraction_uses_lossless_three_six_nine_digit_groups(nano,fraction):
    value = LocalTimeValue.from_components(12,0,0,nano)
    assert value.isoformat() == "12:00:00." + fraction
    assert text.parse_localtime(value.isoformat()) == value


@pytest.mark.parametrize("kind", _EXAMPLES)
def test_type_budget_and_storage_admission_are_not_bypassed(kind):
    from okto_grafx.domain.model.value import encode_value, decode_value
    parse = getattr(text,"parse_" + kind)
    class Host:
        def __str__(self):
            raise AssertionError("host callback")
    for invalid in (Host(),None,True,1,1.0," " * (text.MAX_TEMPORAL_TEXT + 1)):
        with pytest.raises(SchemaMismatchError):
            parse(invalid)
    value = parse(_EXAMPLES[kind][0][0])
    assert decode_value(encode_value(value))[0] == value
