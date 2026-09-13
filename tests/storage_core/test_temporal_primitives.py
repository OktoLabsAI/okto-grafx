"""FP-5 value foundation, not yet a query/persistence conformance claim."""

from datetime import date
from random import Random

import pytest

from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import DateValue, DurationValue, LocalTimeValue, MIN_YEAR, MAX_YEAR


def test_calendar_matches_independent_standard_library_across_supported_host_years():
    rng = Random(731)
    epoch = date(1970, 1, 1).toordinal()
    for _ in range(5000):
        original = date.fromordinal(rng.randrange(1, date.max.toordinal() + 1))
        value = DateValue(original.year, original.month, original.day)
        assert value.epoch_day == original.toordinal() - epoch
        assert value.day_of_week == original.isoweekday()
        assert value.ordinal_day == original.timetuple().tm_yday
        assert DateValue.from_epoch_day(value.epoch_day) == value
        week_year, week, weekday = original.isocalendar()
        assert DateValue.from_week_date(week_year, week, weekday) == value


@pytest.mark.parametrize("year", [MIN_YEAR,-10000,-400,-100,-4,-1,0,1,4,100,400,9999,10000,MAX_YEAR])
def test_expanded_negative_years_and_zero_follow_400_year_cycles(year):
    for month, day in [(1,1),(2,28),(3,1),(6,30),(12,31)]:
        value = DateValue(year,month,day)
        assert DateValue.from_epoch_day(value.epoch_day) == value
        neighbor = year + 400 if year + 400 <= MAX_YEAR else year - 400
        assert abs(DateValue(neighbor,month,day).epoch_day - value.epoch_day) == 146097
        assert DateValue.from_ordinal_day(year,value.ordinal_day) == value


@pytest.mark.parametrize("before,months,after", [
    (DateValue(2024,1,31),1,DateValue(2024,2,29)),
    (DateValue(2023,3,31),-1,DateValue(2023,2,28)),
    (DateValue(0,1,31),-1,DateValue(-1,12,31)),
])
def test_calendar_month_shift_clamps_and_preserves_year_zero(before, months, after):
    assert before.add_months(months) == after
    assert before.add_days(1).add_days(-1) == before


@pytest.mark.parametrize("value,expected", [
    (DateValue(0),"0000-01-01"),(DateValue(-1),"-0001-01-01"),
    (DateValue(10000),"+10000-01-01"),
    (LocalTimeValue.from_components(1,2),"01:02"),
    (LocalTimeValue.from_components(1,2,3,1),"01:02:03.000000001"),
    (LocalTimeValue.from_components(23,59,59,999999999),"23:59:59.999999999"),
])
def test_exact_canonical_spelling(value, expected):
    assert value.isoformat() == expected


@pytest.mark.parametrize("constructor,args", [
    (DateValue,(True,)),(DateValue,(2023,2,29)),(DateValue,(MIN_YEAR-1,)),
    (DateValue.from_epoch_day,(True,)),(DateValue.from_week_date,(2023,53)),
    (LocalTimeValue,(-1,)),(LocalTimeValue,(86400000000000,)),
    (LocalTimeValue.from_components,(1,2,60)),(LocalTimeValue,(1.0,)),
    (DurationValue,(False,)),(DurationValue,(0,0,(1<<63)-1,1000000000)),
])
def test_invalid_components_are_typed_refusals(constructor, args):
    with pytest.raises(SchemaMismatchError):
        constructor(*args)


def test_duration_normalizes_signed_subseconds_without_folding_calendar_components():
    assert DurationValue(seconds=1,nanoseconds=-1) == DurationValue(seconds=0,nanoseconds=999999999)
    assert DurationValue(nanoseconds=-1) == DurationValue(seconds=-1,nanoseconds=999999999)
    assert DurationValue(months=1) != DurationValue(days=30)
    assert DurationValue(days=1) != DurationValue(seconds=86400)
    assert hash(DurationValue(nanoseconds=1000000001)) == hash(DurationValue(seconds=1,nanoseconds=1))


@pytest.mark.parametrize("value", [DateValue(2024,2,29),LocalTimeValue(1),DurationValue(nanoseconds=1)])
def test_temporal_values_have_native_codec_tags(value):
    from okto_grafx.domain.model.value import encode_value, decode_value, value_type_of
    raw = encode_value(value)
    assert raw[0] == value_type_of(value)
    assert decode_value(raw) == (value, len(raw))
