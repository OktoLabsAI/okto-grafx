"""Package-backed named zones preserve precision and explicit DST resolution."""

from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from struct import error as StructError

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import DateValue,LocalDateTimeValue,LocalTimeValue


def local(year,month,day,hour=0,minute=0,nano=1):
    return LocalDateTimeValue(DateValue(year,month,day),LocalTimeValue.from_components(hour,minute,0,nano))


def test_named_zone_historical_seconds_and_nanoseconds_are_preserved():
    resolver = ZoneInfoTemporalResolver()
    value = resolver.at_local(local(1818,7,21,21,40,142000001),"Europe/Stockholm")
    assert value.offset_seconds == 3208
    assert value.nanosecond == 142000001
    assert resolver.at_instant(value.epoch_seconds,value.nanosecond,value.zone) == value
    assert resolver.data_version.startswith("tzdata:") and ";iana:" in resolver.data_version


def test_overlap_defaults_to_earlier_instant_and_explicit_offset_selects_later():
    resolver = ZoneInfoTemporalResolver()
    wall = local(2020,11,1,1,30)
    early = resolver.at_local(wall,"America/New_York")
    late = resolver.at_local(wall,"America/New_York",offset_seconds=-18000)
    assert (early.offset_seconds,late.offset_seconds) == (-14400,-18000)
    assert late.epoch_seconds - early.epoch_seconds == 3600
    assert resolver.at_instant(late.epoch_seconds,late.nanosecond,late.zone) == late
    with pytest.raises(SchemaMismatchError) as failure:
        resolver.at_local(wall,"America/New_York",offset_seconds=0)
    assert failure.value.details["reason"] == "temporal_offset_mismatch"


def test_gap_shifts_by_transition_length_and_rejects_explicit_invalid_offset():
    resolver = ZoneInfoTemporalResolver()
    wall = local(2020,3,8,2,30,999999999)
    shifted = resolver.at_local(wall,"America/New_York")
    assert shifted.local == local(2020,3,8,3,30,999999999)
    assert shifted.offset_seconds == -14400
    with pytest.raises(SchemaMismatchError):
        resolver.at_local(wall,"America/New_York",offset_seconds=-18000)


def test_half_hour_gap_is_not_assumed_to_be_one_hour():
    value = ZoneInfoTemporalResolver().at_local(local(2020,10,4,2,15),"Australia/Lord_Howe")
    assert value.local == local(2020,10,4,2,45)
    assert value.offset_seconds == 39600


@pytest.mark.parametrize("zone", ["../UTC","/UTC","C:/UTC","Europe\\London","Europe//London","NoSuch/Zone"])
def test_unknown_or_path_like_zones_never_fall_back_to_utc(zone):
    with pytest.raises(SchemaMismatchError):
        ZoneInfoTemporalResolver().at_local(local(2020,1,1),zone)


@pytest.mark.parametrize("year", [-999999999,-10000,-1,0,10000,999999999])
def test_named_zone_expanded_years_roundtrip_without_clipping(year):
    resolver = ZoneInfoTemporalResolver()
    wall = local(year,1,1,nano=999999999)
    value = resolver.at_local(wall,"Europe/London")
    assert value.local == wall
    assert resolver.at_instant(value.epoch_seconds,value.nanosecond,value.zone) == value
    assert value.offset_seconds == (-75 if year <= 0 else 0)


def test_rule_cache_is_bounded_and_shared_calls_are_consistent():
    resolver = ZoneInfoTemporalResolver()
    names = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8").splitlines()[:140]
    assert len(names) == 140
    for name in names:
        resolver.at_local(local(2020,1,1),name)
    assert len(resolver._rules) == 128
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _:resolver.at_local(local(2020,11,1,1,30),"America/New_York"),range(40)))
    assert all(value == values[0] for value in values)


def test_named_rules_use_declared_package_files_not_default_zoneinfo_search(monkeypatch):
    import okto_grafx.adapters.temporal_zoneinfo as module
    original = module.ZoneInfo
    called = []
    class ExplicitFileOnly:
        @staticmethod
        def from_file(stream, *, key):
            assert stream.read(4) == b"TZif"
            stream.seek(0)
            called.append(key)
            return original.from_file(stream,key=key)
    monkeypatch.setattr(module,"ZoneInfo",ExplicitFileOnly)
    assert ZoneInfoTemporalResolver().at_local(local(2020,1,1),"UTC").offset_seconds == 0
    assert called == ["UTC"]


@pytest.mark.parametrize("error,expected", [
    (ValueError("malformed"), GrafxCorruptionDetected),
    (EOFError("truncated"), GrafxCorruptionDetected),
    (StructError("truncated header"), GrafxCorruptionDetected),
    (PermissionError("unreadable"), GrafxUnsupportedOperation),
    (OSError("read failure"), GrafxUnsupportedOperation),
])
def test_failed_rule_load_is_typed_uncached_and_retryable(monkeypatch,error,expected):
    import okto_grafx.adapters.temporal_zoneinfo as module
    original = module.ZoneInfo

    class BrokenRule:
        @staticmethod
        def from_file(stream, *, key):
            raise error

    resolver = ZoneInfoTemporalResolver()
    monkeypatch.setattr(module,"ZoneInfo",BrokenRule)
    with pytest.raises(expected) as failure:
        resolver.at_local(local(2020,1,1),"UTC")
    assert failure.value.details["field"] == "temporal_timezone_data"
    assert failure.value.details["source"] == resolver.data_version
    assert not resolver._rules
    monkeypatch.setattr(module,"ZoneInfo",original)
    assert resolver.at_local(local(2020,1,1),"UTC").offset_seconds == 0


def test_full_day_calendar_gap_preserves_nanoseconds():
    shifted = ZoneInfoTemporalResolver().at_local(local(2011,12,30,12,30,999999999),"Pacific/Apia")
    assert shifted.local == local(2011,12,31,12,30,999999999)
    assert shifted.offset_seconds == 50400
