"""Exact local/zoned native containers; no implicit clock or storage admission."""

from datetime import datetime, timedelta, timezone
from random import Random

import pytest

from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, LocalDateTimeValue, TimeValue, DateTimeValue,
    MIN_YEAR, MAX_YEAR,
)


def test_offset_instant_round_trip_matches_independent_host_arithmetic():
    random = Random(3117)
    epoch = datetime(1970,1,1,tzinfo=timezone.utc)
    for _ in range(1500):
        seconds = random.randrange(-2_000_000_000,2_000_000_000)
        nano = random.randrange(1_000_000_000)
        offset = random.randrange(-64800,64801)
        value = DateTimeValue.from_epoch_parts(seconds,nano,offset_seconds=offset)
        expected = (epoch + timedelta(seconds=seconds)).astimezone(timezone(timedelta(seconds=offset)))
        assert (value.local.date.year,value.local.date.month,value.local.date.day) == (expected.year,expected.month,expected.day)
        assert (value.local.time.hour,value.local.time.minute,value.local.time.second) == (expected.hour,expected.minute,expected.second)
        assert (value.epoch_seconds,value.nanosecond) == (seconds,nano)


@pytest.mark.parametrize("year", [MIN_YEAR,-10000,-1,0,10000,MAX_YEAR])
@pytest.mark.parametrize("offset", [-64800,-3208,0,30,64800])
def test_expanded_year_fixed_offset_instant_preserves_exact_local_fields(year, offset):
    local = LocalDateTimeValue(DateValue(year,1,1),LocalTimeValue(1))
    value = DateTimeValue(local,offset)
    assert DateTimeValue.from_epoch_parts(value.epoch_seconds,value.nanosecond,offset_seconds=offset) == value


def test_same_instant_keeps_distinct_offsets_and_zone_names_in_equality_and_order():
    west = DateTimeValue.from_epoch_parts(0,1,offset_seconds=-3600)
    zero = DateTimeValue.from_epoch_parts(0,1)
    east = DateTimeValue.from_epoch_parts(0,1,offset_seconds=3600)
    assert west != zero != east
    assert west.sort_key < zero.sort_key < east.sort_key
    named = DateTimeValue(zero.local,0,"Etc/UTC")
    assert named != zero and named.sort_key > zero.sort_key
    assert len({west,zero,east,named}) == 4
    assert west.isoformat() == "1969-12-31T23:00:00.000000001-01:00"
    assert named.isoformat() == "1970-01-01T00:00:00.000000001Z[Etc/UTC]"


def test_offset_time_preserves_second_precision_and_midnight_displacement():
    east = TimeValue(LocalTimeValue(0),30)
    zero = TimeValue(LocalTimeValue(0),0)
    assert east.isoformat() == "00:00+00:00:30"
    assert east.utc_nanoseconds == -30_000_000_000
    assert east.sort_key < zero.sort_key


@pytest.mark.parametrize("constructor,args", [
    (LocalDateTimeValue,(DateValue(2020),0)),
    (TimeValue,(LocalTimeValue(0),True)),
    (TimeValue,(LocalTimeValue(0),64801)),
    (DateTimeValue,(LocalDateTimeValue(DateValue(2020),LocalTimeValue(0)),0,"../UTC")),
    (DateTimeValue,(LocalDateTimeValue(DateValue(2020),LocalTimeValue(0)),0,"C:/UTC")),
    (DateTimeValue.from_epoch_parts,(0,1_000_000_000)),
])
def test_bad_component_and_zone_keys_refuse_without_host_coercion(constructor, args):
    with pytest.raises(SchemaMismatchError):
        constructor(*args)


@pytest.mark.parametrize("value", [
    LocalDateTimeValue(DateValue(2020),LocalTimeValue(1)),
    TimeValue(LocalTimeValue(1),0),DateTimeValue.from_epoch_parts(0,1),
])
def test_zoned_types_have_native_codec_tags(value):
    from okto_grafx.domain.model.value import encode_value,decode_value,value_type_of
    raw = encode_value(value)
    assert raw[0] == value_type_of(value)
    assert decode_value(raw) == (value, len(raw))
