"""Independent stdlib checks plus synthetic full-range/invalid rule files."""

from datetime import datetime, timedelta, timezone
from importlib import resources
from io import BytesIO
from struct import pack
from zoneinfo import ZoneInfo

import pytest

from okto_grafx.adapters.temporal_tzif import MAX_RULE_BYTES, parse_zone_rule
from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.model.temporal_values import DateValue, LocalDateTimeValue, LocalTimeValue

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NAMES = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8").splitlines()


def rule_file(transitions=(), indices=(), records=((0, 0, 0),), footer=b"UTC0", names=b"X\0"):
    def header(count, types, chars):
        return b"TZif3" + bytes(15) + pack(">6I", 0, 0, 0, count, types, chars)
    compat = header(0, 1, 2) + pack(">iBB", 0, 0, 0) + b"X\0"
    block = pack(f">{len(transitions)}q", *transitions) + bytes(indices)
    block += b"".join(pack(">iBB", *record) for record in records) + names
    return compat + header(len(transitions), len(records), len(names)) + block + b"\n" + footer + b"\n"


@pytest.mark.parametrize("name", _NAMES)
def test_every_package_zone_matches_independent_zoneinfo_history_and_transitions(name):
    data = resources.files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
    rule = parse_zone_rule(data, name)
    reference = ZoneInfo.from_file(BytesIO(data), key=name)
    points = {int((datetime(year, month, 17, 12, tzinfo=timezone.utc) - _EPOCH).total_seconds())
              for year in (1, 1600, 1818, 1900, 1970, 2020, 2400, 9999) for month in (1, 7)}
    points.update(second + delta for second in rule.transitions for delta in (-1, 0, 1))
    for seconds in points:
        host = (_EPOCH + timedelta(seconds=seconds)).astimezone(reference)
        actual = rule.offset_at(seconds)
        if host.tzname() == "-00":
            assert actual is None
        else:
            assert actual == int(host.utcoffset().total_seconds()), (name, host, seconds)
    resolver = ZoneInfoTemporalResolver()
    resolver._rules[name] = rule
    for seconds in rule.transitions:
        old, new = rule.offset_at(seconds - 1), rule.offset_at(seconds)
        if old is None or new is None or old == new:
            continue
        wall_second = seconds + min(old, new) + abs(new - old) // 2
        wall = LocalDateTimeValue.from_local_epoch_parts(wall_second, 987654321)
        value = resolver.at_local(wall, name)
        if new > old:
            assert value.local.local_epoch_seconds == wall_second + new - old, (name, wall)
            assert value.offset_seconds == new
        else:
            assert value.local == wall and value.offset_seconds == old
            later = resolver.at_local(wall, name, offset_seconds=new)
            assert later.local == wall and later.epoch_seconds - value.epoch_seconds == old - new
        assert value.nanosecond == 987654321


@pytest.mark.parametrize("name", ["Europe/London", "America/New_York", "Australia/Lord_Howe",
                                  "Pacific/Chatham", "Africa/Casablanca", "Europe/Dublin"])
def test_expanded_future_preserves_annual_calendar_and_exact_local_roundtrip(name):
    resolver = ZoneInfoTemporalResolver()
    for year in (10000, 10400, 20000, 999999999):
        for month in (1, 3, 7, 11):
            local = LocalDateTimeValue(DateValue(year, month, 17), LocalTimeValue.from_components(12, 30, 0, 123456789))
            value = resolver.at_local(local, name)
            surrogate = LocalDateTimeValue(DateValue(2400 + year % 400, month, 17), local.time)
            assert value.offset_seconds == resolver.at_local(surrogate, name).offset_seconds
            assert resolver.at_instant(value.epoch_seconds, value.nanosecond, name) == value
            assert value.local == local


def test_expanded_year_explicit_history_is_not_folded_into_annual_cycle():
    transition = DateValue(12000, 1, 1).epoch_day * 86400
    rule = parse_zone_rule(rule_file((transition,), (1,), ((0, 0, 0), (-18000, 0, 0)),
                                    b"EST5EDT,M3.2.0,M11.1.0"), "Synthetic")
    assert rule.offset_at(DateValue(11999, 7, 1).epoch_day * 86400) == 0
    assert rule.offset_at(transition) == -18000
    assert rule.offset_at(transition + 1) == -18000
    assert rule.offset_at(DateValue(20000, 7, 1).epoch_day * 86400) == -14400
    assert rule.offset_at(DateValue(-999999999, 7, 1).epoch_day * 86400) == 0


@pytest.mark.parametrize("footer", [b"ABC-1", b"ABC0DEF,J60/0,J300/0", b"ABC0DEF,59/0,300/0",
                                    b"ABC0DEF,M3.5.0/-2,M10.5.0/26", b"ABC0DEF-0:30,M3.2.0,M11.1.0",
                                    b"ABC0DEF,0/0,J365/25"])
def test_annual_rule_spellings_repeat_on_expanded_gregorian_cycles(footer):
    data = rule_file(footer=footer)
    rule = parse_zone_rule(data, "Synthetic")
    reference = ZoneInfo.from_file(BytesIO(data), key="Synthetic")
    for day in range(366):
        host = datetime(2400, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=day)
        seconds = int((host - _EPOCH).total_seconds())
        expected = int(host.astimezone(reference).utcoffset().total_seconds())
        assert rule.offset_at(seconds + 146097 * 86400 * 20) == expected
        assert rule.offset_at(seconds - 146097 * 86400 * 20) == expected


def test_unspecified_intervals_do_not_disable_later_known_rules():
    rule = parse_zone_rule(rule_file((0,), (1,), ((0, 0, 0), (3600, 0, 4)), b"ABC-1", b"-00\0ABC\0"), "Synthetic")
    assert rule.offset_at(-1) is None
    assert rule.offset_at(0) == rule.offset_at(1) == 3600
    resolver = ZoneInfoTemporalResolver()
    resolver._rules["Synthetic"] = rule
    with pytest.raises(GrafxUnsupportedOperation, match="does not specify"):
        resolver.at_instant(-1, 0, "Synthetic")
    assert resolver.at_instant(1, 999999999, "Synthetic").offset_seconds == 3600


@pytest.mark.parametrize("data", [b"", b"TZif", rule_file()[:-1], rule_file() + b"extra",
    rule_file((1, 0), (0, 0)), rule_file((1, 1), (0, 0)), rule_file((1,), (2,)),
    rule_file(records=((0, 2, 0),)), rule_file(records=((0, 0, 4),)),
    rule_file(names=b"no-null"), rule_file(records=((-(1 << 31), 0, 0),)),
    rule_file(footer=b"UTC0\ntrailing"), rule_file(footer=b"bad-offset"),
    rule_file(footer=b"ABC0DEF,invalid,rules"),
    b"TZif3" + bytes(15) + pack(">6I", 0, 0, 0, 0xffffffff, 256, 0xffffffff),
])
def test_malformed_rules_refuse(data):
    with pytest.raises(ValueError):
        parse_zone_rule(data, "Broken")


def test_rule_file_budget_refuses_before_decoding():
    with pytest.raises(GrafxUnsupportedOperation) as error:
        parse_zone_rule(bytes(MAX_RULE_BYTES + 1), "TooLarge")
    assert error.value.details["maximum"] == MAX_RULE_BYTES


@pytest.mark.parametrize("year,month,day,hour,minute,second,name", [
    (-999999999,1,1,0,0,0,"Asia/Tokyo"),
    (999999999,12,31,23,59,59,"America/New_York"),
    (1,1,1,0,0,0,"Asia/Tokyo"),
    (9999,12,31,23,59,59,"America/New_York"),
])
def test_named_roundtrip_does_not_require_utc_date_to_fit_host_or_local_year_bounds(
        year,month,day,hour,minute,second,name):
    wall = LocalDateTimeValue(DateValue(year,month,day),LocalTimeValue.from_components(hour,minute,second,999999999))
    resolver = ZoneInfoTemporalResolver()
    value = resolver.at_local(wall,name)
    assert resolver.at_instant(value.epoch_seconds,value.nanosecond,name) == value
    assert value.local == wall


def test_unknown_annual_interval_refuses_instead_of_inventing_utc():
    resolver = ZoneInfoTemporalResolver()
    resolver._rules["Synthetic"] = parse_zone_rule(rule_file(footer=b"<-00>0"),"Synthetic")
    with pytest.raises(GrafxUnsupportedOperation):
        resolver.at_instant(0,0,"Synthetic")
    with pytest.raises(GrafxUnsupportedOperation):
        resolver.at_local(LocalDateTimeValue(DateValue(1970),LocalTimeValue(0)),"Synthetic")


@pytest.mark.parametrize("data", [rule_file(records=((64801,0,0),)), rule_file(footer=b"ABC-19")])
def test_rule_offsets_cannot_bypass_native_offset_range(data):
    with pytest.raises(GrafxUnsupportedOperation):
        parse_zone_rule(data,"Synthetic")


def test_leap_adjusted_clock_is_not_silently_treated_as_posix_seconds():
    header = b"TZif\0" + bytes(15) + pack(">6I",0,0,1,0,1,2)
    data = header + pack(">iBB",0,0,0) + b"X\0" + pack(">ii",78796800,1)
    with pytest.raises(GrafxUnsupportedOperation,match="Leap-adjusted"):
        parse_zone_rule(data,"Synthetic")
