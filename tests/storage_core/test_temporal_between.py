"""Native duration difference proofs, independent from public query integration."""

from datetime import datetime, timezone
import random

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue, MIN_YEAR, MAX_YEAR,
)
from okto_grafx.domain.temporal_between import temporal_between
from okto_grafx.domain.temporal_arithmetic import add_duration
from okto_grafx.domain.temporal_text import (
    parse_date, parse_localtime, parse_time, parse_localdatetime, parse_datetime,
)

_LEFT = [parse_date("1984-10-11"),parse_localtime("14:30"),parse_time("14:30"),
         parse_localdatetime("2015-07-21T21:40:32.142"),parse_datetime("2014-07-21T21:40:36.143+0200")]
_RIGHT = [parse_date("2015-06-24"),parse_localdatetime("2016-07-21T21:45:22.142"),
          parse_datetime("2015-07-21T21:40:32.142+0100"),parse_localtime("16:30"),parse_time("16:30+0100")]
_LOGICAL = [
    ["P30Y8M13D","P31Y9M10DT21H45M22.142S","P30Y9M10DT21H40M32.142S","PT16H30M","PT16H30M"],
    ["PT-14H-30M","PT7H15M22.142S","PT7H10M32.142S","PT2H","PT2H"],
    ["PT-14H-30M","PT7H15M22.142S","PT6H10M32.142S","PT2H","PT1H"],
    ["P-27DT-21H-40M-32.142S","P1YT4M50S","PT0S","PT-5H-10M-32.142S","PT-5H-10M-32.142S"],
    ["P11M2DT2H19M23.857S","P2YT4M45.999S","P1YT59M55.999S","PT-5H-10M-36.143S","PT-4H-10M-36.143S"],
]
_SECONDS = [
    ["PT269112H","PT278565H45M22.142S","PT269781H40M32.142S","PT16H30M","PT16H30M"],
    ["PT-14H-30M","PT7H15M22.142S","PT7H10M32.142S","PT2H","PT2H"],
    ["PT-14H-30M","PT7H15M22.142S","PT6H10M32.142S","PT2H","PT1H"],
    ["PT-669H-40M-32.142S","PT8784H4M50S","PT0S","PT-5H-10M-32.142S","PT-5H-10M-32.142S"],
    ["PT8090H19M23.857S","PT17544H4M45.999S","PT8760H59M55.999S","PT-5H-10M-36.143S","PT-4H-10M-36.143S"],
]


@pytest.mark.parametrize("i",range(5))
@pytest.mark.parametrize("j",range(5))
def test_original_logical_and_elapsed_family_cross_products(i,j):
    assert temporal_between(_LEFT[i],_RIGHT[j]).isoformat() == _LOGICAL[i][j]
    assert temporal_between(_LEFT[i],_RIGHT[j],"inSeconds").isoformat() == _SECONDS[i][j]


@pytest.mark.parametrize("i,j,months,days", [
    (0,0,368,11213),(0,1,381,11606),(0,2,369,11240),(0,3,0,0),(0,4,0,0),
    (1,0,0,0),(1,1,0,0),(1,2,0,0),(2,0,0,0),(2,1,0,0),(2,2,0,0),
    (3,0,0,-27),(3,1,12,366),(3,2,0,0),(3,3,0,0),(3,4,0,0),
    (4,0,11,337),(4,1,24,731),(4,2,12,365),(4,3,0,0),(4,4,0,0),
])
def test_original_single_calendar_unit_pairs(i,j,months,days):
    assert temporal_between(_LEFT[i],_RIGHT[j],"inMonths") == DurationValue(months=months)
    assert temporal_between(_LEFT[i],_RIGHT[j],"inDays") == DurationValue(days=days)


@pytest.mark.parametrize("source,target,expected", [
    ("2018-01-01T12:00","2018-01-02T10:00","PT22H"),
    ("2018-01-02T10:00","2018-01-01T12:00","PT-22H"),
    ("2018-01-01T10:00:00.2","2018-01-02T10:00:00.1","PT23H59M59.9S"),
    ("2018-01-02T10:00:00.1","2018-01-01T10:00:00.2","PT-23H-59M-59.9S"),
])
def test_complete_day_boundaries_include_nanosecond_comparison(source,target,expected):
    assert temporal_between(parse_localdatetime(source),parse_localdatetime(target)).isoformat() == expected


@pytest.mark.parametrize("source,target,expected", [
    ("12:34:54.7","12:34:54.3","PT-0.4S"),("12:34:54.3","12:34:54.7","PT0.4S"),
    ("12:34:54.7","12:34:55.3","PT0.6S"),("12:34:54.7","12:44:55.3","PT10M0.6S"),
    ("12:44:54.7","12:34:55.3","PT-9M-59.4S"),("12:34:56","12:34:55.7","PT-0.3S"),
    ("12:34:56","12:44:55.7","PT9M59.7S"),("12:44:56","12:34:55.7","PT-10M-0.3S"),
    ("12:34:56.3","12:34:54.7","PT-1.6S"),("12:34:54.7","12:34:56.3","PT1.6S"),
])
def test_original_signed_subsecond_pairs(source,target,expected):
    assert temporal_between(parse_localtime(source),parse_localtime(target),"inSeconds").isoformat() == expected


def test_daylight_saving_zone_inheritance_and_elapsed_seconds():
    resolver = ZoneInfoTemporalResolver()
    midnight = parse_datetime("2017-10-29T00:00[Europe/Stockholm]",resolver=resolver)
    four = parse_datetime("2017-10-29T04:00[Europe/Stockholm]",resolver=resolver)
    for left,right in [(midnight,parse_localdatetime("2017-10-29T04:00")),(midnight,parse_localtime("04:00")),
                       (parse_localdatetime("2017-10-29T00:00"),four),(parse_localtime("00:00"),four),
                       (DateValue(2017,10,29),four)]:
        assert temporal_between(left,right,"inSeconds",resolver=resolver) == DurationValue(seconds=5*3600)
        assert temporal_between(right,left,"inSeconds",resolver=resolver) == DurationValue(seconds=-5*3600)
    next_date = DateValue(2017,10,30)
    assert temporal_between(midnight,next_date,"inSeconds",resolver=resolver) == DurationValue(seconds=25*3600)
    assert temporal_between(midnight,next_date,resolver=resolver) == DurationValue(days=1)


@pytest.mark.parametrize("left,right,expected", [
    (parse_date("2018-03-11"),parse_date("2016-06-24"),-20),
    (parse_date("2018-07-21"),parse_datetime("2016-07-21T21:40:32.142+0100"),-23),
    (parse_localdatetime("2018-07-21T21:40:32.142"),parse_date("2016-07-21"),-24),
    (parse_datetime("2018-07-21T21:40:36.143+0200"),parse_localdatetime("2016-07-21T21:40:36.143"),-24),
    (parse_datetime("2018-07-21T21:40:36.143+0500"),parse_datetime("1984-07-21T22:40:36.143+0200"),-407),
])
def test_original_negative_month_boundaries(left,right,expected):
    assert temporal_between(left,right,"inMonths") == DurationValue(months=expected)


def test_full_calendar_range_and_month_end_noninvertibility():
    minimum,maximum = DateValue(MIN_YEAR),DateValue(MAX_YEAR,12,31)
    assert temporal_between(minimum,maximum).isoformat() == "P1999999998Y11M30D"
    first = LocalDateTimeValue(minimum,LocalTimeValue(0))
    last = LocalDateTimeValue(maximum,LocalTimeValue.from_components(23,59,59))
    assert temporal_between(first,last,"inSeconds").isoformat() == "PT17531639991215H59M59S"
    jan,feb = DateValue(2024,1,31),DateValue(2024,2,29)
    assert temporal_between(jan,feb) == DurationValue(days=29)
    assert temporal_between(feb,jan) == DurationValue(days=-29)
    assert temporal_between(jan,feb,"inMonths") == DurationValue()


@pytest.mark.parametrize("mode",["between","inMonths","inDays","inSeconds"])
def test_null_and_zero_difference(mode):
    assert temporal_between(None,None,mode) is None
    assert temporal_between(None,_LEFT[0],mode) is None
    assert temporal_between(_LEFT[0],None,mode) is None
    for value in (_LEFT[0],_LEFT[3],_LEFT[4]):
        assert temporal_between(value,value,mode) == DurationValue()


@pytest.mark.parametrize("mode",["inMonths","inDays"])
@pytest.mark.parametrize("left,right",[(LocalTimeValue(0),LocalTimeValue(1)),(TimeValue(LocalTimeValue(0),0),LocalTimeValue(1))])
def test_dateless_calendar_difference_refuses_without_inventing_a_date(mode,left,right):
    with pytest.raises(SchemaMismatchError,match="need a date"):
        temporal_between(left,right,mode)


def test_rules_are_required_only_when_a_zone_must_be_bound_or_shifted():
    source = DateTimeValue(_LEFT[3],3600,"Europe/Stockholm")
    target = DateTimeValue(_LEFT[3],0,"Europe/London")
    assert temporal_between(source,target,"inSeconds") == DurationValue(seconds=3600)
    with pytest.raises(GrafxUnsupportedOperation):
        temporal_between(source,_LEFT[3],"inSeconds")
    with pytest.raises(GrafxUnsupportedOperation):
        temporal_between(source,target,"inDays")
    assert temporal_between(source,source) == DurationValue()


def test_invalid_inputs_and_provider_failures_do_not_mutate_sources():
    class Host:
        def __getattr__(self,name):
            raise AssertionError("No host attributes")
    for left,right,mode in [(Host(),_LEFT[0],"between"),(_LEFT[0],DurationValue(),"between"),
                            (None,None,"years"),(None,None,Host()),(None,None,"x"*100)]:
        with pytest.raises(SchemaMismatchError):
            temporal_between(left,right,mode)
    class Provider:
        def at_local(self,*args,**kwargs):
            raise OSError("provider failure")
    named = DateTimeValue(_LEFT[3],3600,"Europe/Stockholm")
    before = named.isoformat()
    with pytest.raises(OSError,match="provider failure"):
        temporal_between(named,_LEFT[3],resolver=Provider())
    assert named.isoformat() == before


def test_independent_elapsed_reference_and_logical_roundtrips():
    rng = random.Random(0xAA0)
    for _ in range(1500):
        raw = [datetime(rng.randrange(100,9900),rng.randrange(1,13),rng.randrange(1,29),
                        rng.randrange(24),rng.randrange(60),rng.randrange(60),tzinfo=timezone.utc) for _ in range(2)]
        values = [LocalDateTimeValue(DateValue(v.year,v.month,v.day),LocalTimeValue.from_components(v.hour,v.minute,v.second,rng.randrange(1_000_000_000))) for v in raw]
        delta = raw[1]-raw[0]
        expected_nanos = (delta.days*86400+delta.seconds)*1_000_000_000 + values[1].time.nanosecond-values[0].time.nanosecond
        duration = temporal_between(*values,mode="inSeconds")
        assert duration.seconds*1_000_000_000+duration.nanoseconds == expected_nanos
        assert add_duration(values[0],temporal_between(*values)) == values[1]


def test_result_roundtrips_native_codec():
    from okto_grafx.domain.model.value import encode_value, decode_value
    result = temporal_between(_LEFT[0],_RIGHT[0])
    assert decode_value(encode_value(result))[0] == result
