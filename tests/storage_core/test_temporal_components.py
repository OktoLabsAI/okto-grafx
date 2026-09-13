"""Native component/selection semantics; public query/storage admission is separate."""

from datetime import date
import random
from types import MappingProxyType

import pytest

from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain import temporal_components as component
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue, TimeValue
from okto_grafx.domain.temporal_text import parse_date, parse_datetime, parse_localtime

_DATES = [({"year":1984,"month":10,"day":11},"1984-10-11",3600),
          ({"year":1984,"week":10,"dayOfWeek":3},"1984-03-07",3600),
          ({"year":1984,"ordinalDay":202},"1984-07-20",7200),
          ({"year":1984,"quarter":3,"dayOfQuarter":45},"1984-08-14",7200)]
_CLOCKS = [({},"00:00"),({"hour":12},"12:00"),({"hour":12,"minute":31},"12:31"),
           ({"hour":12,"minute":31,"second":14},"12:31:14")]
_CLOCKS += [({"hour":12,"minute":31,"second":14,**parts},"12:31:14."+fraction)
            for parts,fraction in [({"millisecond":645},"645"),({"microsecond":645876},"645876"),
                                   ({"nanosecond":645876123},"645876123"),({"nanosecond":3},"000000003"),
                                   ({"millisecond":123,"microsecond":456,"nanosecond":789},"123456789")]]


@pytest.mark.parametrize("fields,expected,_",_DATES)
@pytest.mark.parametrize("clock,clock_text",_CLOCKS)
def test_calendar_clock_cross_product_and_explicit_zone_composition(fields,expected,_,clock,clock_text):
    assert component.build_localdatetime({**fields,**clock}).isoformat() == expected + "T" + clock_text
    assert component.build_datetime({**fields,**clock}).isoformat() == expected + "T" + clock_text + "Z"
    assert component.build_datetime({**fields,**clock,"timezone":"+01:00"}).isoformat() == expected + "T" + clock_text + "+01:00"
    named = component.build_datetime({**fields,**clock,"timezone":"Europe/Stockholm"},resolver=ZoneInfoTemporalResolver())
    assert named.local.isoformat() == expected + "T" + clock_text
    assert named.offset_seconds == _ and named.zone == "Europe/Stockholm"


@pytest.mark.parametrize("fields,expected",_CLOCKS[1:])
def test_local_and_offset_time_components(fields,expected):
    assert component.build_localtime(fields).isoformat() == expected
    assert component.build_time(fields).isoformat() == expected + "Z"
    assert component.build_time({**fields,"timezone":"-02:05:07"}).isoformat() == expected + "-02:05:07"


@pytest.mark.parametrize("fields,expected",[
    ({"year":1816,"week":1},"1816-01-01"),({"year":1816,"week":52},"1816-12-23"),
    ({"year":1817,"week":1},"1816-12-30"),({"year":1817,"week":10},"1817-03-03"),
    ({"year":1817,"week":30},"1817-07-21"),({"year":1817,"week":52},"1817-12-22"),
    ({"year":1818,"week":1},"1817-12-29"),({"year":1818,"week":52},"1818-12-21"),
    ({"year":1818,"week":53},"1818-12-28"),({"year":1819,"week":1},"1819-01-04"),
    ({"year":1819,"week":52},"1819-12-27"),({"year":1817,"week":1,"dayOfWeek":2},"1816-12-31"),
    ({"date":parse_date("1816-12-30"),"week":2,"dayOfWeek":3},"1817-01-08"),
    ({"date":parse_date("1816-12-31"),"week":2},"1817-01-07"),
    ({"date":parse_date("1816-12-31"),"year":1817,"week":2},"1817-01-07"),
])
def test_week_year_is_preserved_when_calendar_year_differs(fields,expected):
    assert component.build_date(fields).isoformat() == expected
    assert component.build_localdatetime(fields).isoformat() == expected + "T00:00"
    assert component.build_datetime(fields).isoformat() == expected + "T00:00Z"


@pytest.mark.parametrize("fields,expected",[
    ({"days":14,"hours":16,"minutes":12},"P14DT16H12M"),({"months":5,"days":1.5},"P5M1DT12H"),
    ({"months":0.75},"P22DT19H51M49.5S"),({"weeks":2.5},"P17DT12H"),
    ({"years":12,"months":5,"days":14,"hours":16,"minutes":12,"seconds":70},"P12Y5M14DT16H13M10S"),
    ({"days":14,"seconds":70,"milliseconds":1},"P14DT1M10.001S"),
    ({"days":14,"seconds":70,"microseconds":1},"P14DT1M10.000001S"),
    ({"days":14,"seconds":70,"nanoseconds":1},"P14DT1M10.000000001S"),
    ({"minutes":1.5,"seconds":1},"PT1M31S"),({"seconds":-60,"milliseconds":-1},"PT-1M-0.001S"),
    ({"seconds":0.1,"nanoseconds":1},"PT0.100000001S"),({"nanoseconds":-0.9},"PT0S"),
])
def test_duration_map_retains_calendar_and_subsecond_units(fields,expected):
    assert component.build_duration(fields).isoformat() == expected


def test_epoch_seconds_millis_and_zoned_maps_are_exact():
    assert component.datetime_from_epoch(416779,999999999).isoformat() == "1970-01-05T19:46:19.999999999Z"
    assert component.datetime_from_epoch_millis(237821673987).isoformat() == "1977-07-15T13:34:33.987Z"
    assert component.datetime_from_epoch_millis(-1).isoformat() == "1969-12-31T23:59:59.999Z"
    assert component.build_datetime({"epochSeconds":-1,"nanosecond":999999999,"timezone":"+01:00"}).isoformat() == "1970-01-01T00:59:59.999999999+01:00"
    assert component.build_datetime({"epochMillis":-1}) == component.datetime_from_epoch_millis(-1)


def test_selected_date_fields_preserve_calendar_position_and_clamp_implicit_day():
    base = DateValue(1984,11,11)
    for fields,expected in [({"year":28},"0028-11-11"),({"day":28},"1984-11-28"),
                            ({"week":1},"1984-01-08"),({"ordinalDay":28},"1984-01-28"),({"quarter":3},"1984-08-11")]:
        assert component.build_date({"date":base,**fields}).isoformat() == expected
    assert component.build_date({"date":DateValue(2024,2,29),"year":2023}) == DateValue(2023,2,28)
    assert component.build_date({"date":DateValue(2024,3,31),"quarter":2}) == DateValue(2024,6,30)


def test_zone_selection_distinguishes_wall_assignment_from_instant_conversion():
    resolver = ZoneInfoTemporalResolver()
    source = parse_datetime("1984-10-11T12[Europe/Stockholm]",resolver=resolver)
    value = component.build_datetime({"datetime":source,"day":28,"second":42,"timezone":"Pacific/Honolulu"},resolver=resolver)
    assert value.isoformat() == "1984-10-28T01:00:42-10:00[Pacific/Honolulu]"
    local = component.build_datetime({"datetime":source.local,"day":28,"second":42,"timezone":"Pacific/Honolulu"},resolver=resolver)
    assert local.isoformat() == "1984-10-28T12:00:42-10:00[Pacific/Honolulu]"
    combined = component.build_datetime({"date":DateValue(1984,3,28),"time":source,"timezone":"Pacific/Honolulu"},resolver=resolver)
    assert combined.isoformat() == "1984-03-28T00:00-10:00[Pacific/Honolulu]"
    # Date-source timezone does not leak into a separately selected local clock.
    assert component.build_datetime({"date":source,"time":LocalTimeValue(0)}).offset_seconds == 0


def test_selected_time_conversion_wraps_clock_without_inventing_calendar_days():
    source = TimeValue(parse_localtime("23:31:14.645876"),3600)
    assert component.build_time({"time":source,"second":42,"timezone":"+05:00"}).isoformat() == "03:31:42.645876+05:00"
    assert component.build_localtime({"time":source,"second":42}).isoformat() == "23:31:42.645876"


def test_noop_selection_preserves_recorded_instant_without_reinterpreting_zone_data():
    source = DateTimeValue(LocalDateTimeValue(DateValue(1818),LocalTimeValue(1)),123,"Europe/London")
    class NoRules:
        def at_local(self,*args,**kwargs):
            raise AssertionError("unchanged stored value must not be reinterpreted")
    assert component.build_datetime({"datetime":source},resolver=NoRules()) is source
    converted = component.build_datetime({"datetime":source,"timezone":"Z"},resolver=NoRules())
    assert converted.epoch_seconds == source.epoch_seconds and converted.nanosecond == 1


@pytest.mark.parametrize("kind,fields",[
    ("date",{}),("date",{"month":1}),("date",{"year":2024,"day":1}),
    ("date",{"year":2024,"month":1,"week":1}),("date",{"year":2024,"week":1,"dayOfQuarter":1}),
    ("date",{"year":True}),("date",{"year":1.0}),("date",{"year":None}),("date",{"year":1,"YEAR":2}),
    ("date",{"date":DateValue(2024,2,29),"year":2023,"day":29}),
    ("localtime",{"hour":12,"second":1}),("localtime",{"minute":1}),("localtime",{"hour":12,"nanosecond":1}),
    ("localtime",{"hour":12,"minute":1,"second":1,"millisecond":1,"microsecond":1000}),
    ("localtime",{"hour":12,"minute":1,"second":1,"microsecond":1,"nanosecond":1000}),
    ("datetime",{"year":2024,"hour":1}),("datetime",{"year":2024,"timezone":3600}),
    ("datetime",{"year":2024,"datetime":DateValue(2024)}),
    ("datetime",{"epochSeconds":0,"epochMillis":0}),("datetime",{"epochMillis":0,"year":2024}),
    ("datetime",{"epochSeconds":0,"nanosecond":1000000000}),
    ("duration",{"days":float("nan")}),("duration",{"seconds":float("inf")}),
    ("duration",{"years":True}),("duration",{"seconds":"1"}),("duration",{"unknown":0}),
])
def test_invalid_components_refuse_without_silent_coercion(kind,fields):
    with pytest.raises(SchemaMismatchError):
        getattr(component,"build_" + kind)(fields)


def test_owned_map_inputs_are_not_mutated_and_custom_mapping_is_not_called():
    fields = {"YEAR":2024,"month":2,"day":29}
    assert component.build_date(MappingProxyType(fields)) == DateValue(2024,2,29)
    assert fields == {"YEAR":2024,"month":2,"day":29}
    class Host(dict):
        def items(self):
            raise AssertionError("host callback")
    with pytest.raises(SchemaMismatchError):
        component.build_date(Host(year=2024))


def test_week_fields_match_independent_calendar_across_host_range():
    randomizer = random.Random(510)
    for _ in range(2000):
        host = date.fromordinal(randomizer.randint(1,date.max.toordinal()))
        value = DateValue(host.year,host.month,host.day)
        assert (value.week_year,value.week,value.day_of_week) == tuple(host.isocalendar())


def test_map_constructor_values_roundtrip_native_codec():
    from okto_grafx.domain.model.value import encode_value, decode_value
    for value in (component.build_date({"year":2024}),component.build_localtime({"hour":0}),
                  component.build_time({"hour":0}),component.build_localdatetime({"year":2024}),
                  component.build_datetime({"year":2024}),component.build_duration({"seconds":0})):
        assert decode_value(encode_value(value))[0] == value
    assert component.build_duration({"days":1}) != DurationValue(seconds=86400)


@pytest.mark.parametrize("offset", [True,1.0,64801,-64801,None])
def test_default_offset_is_validated_even_when_a_source_zone_is_inherited(offset):
    with pytest.raises(SchemaMismatchError):
        component.build_time({"time":TimeValue(LocalTimeValue(0),0)},default_offset=offset)
    with pytest.raises(SchemaMismatchError):
        component.build_datetime({"year":2024},default_offset=offset)


def test_bad_fields_refuse_before_zone_provider_work():
    class NoRules:
        def at_local(self,*args,**kwargs):
            raise AssertionError("invalid fields reached provider")
    for fields in ({"year":2024,"hour":1,"timezone":"Europe/London"},
                   {"year":2024,"timezone":"Europe/London","bad-key":1},
                   {"year":2024,"timezone":"Europe/London","a"*10000:1}):
        with pytest.raises(SchemaMismatchError):
            component.build_datetime(fields,resolver=NoRules())
