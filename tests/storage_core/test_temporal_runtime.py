"""Scope capture proofs; no engine transaction lifecycle or public query claim."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from okto_grafx.adapters.temporal_clock import SystemTemporalClock
from okto_grafx.adapters.temporal_zoneinfo import ZoneInfoTemporalResolver
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import DateTimeValue, DateValue, LocalDateTimeValue, LocalTimeValue, TemporalInstant
from okto_grafx.domain.temporal_runtime import TemporalTransactionContext


class Clock:
    def __init__(self, *instants):
        self.values = iter(instants)
        self.reads = 0

    def now(self):
        self.reads += 1
        return next(self.values)

    def wall(self):
        raise AssertionError("Legacy float wall clock is not the temporal source")

    def monotonic(self):
        raise AssertionError("Liveness clock is not the temporal source")


def test_capture_is_once_per_transaction_statement_and_realtime_invocation():
    source = Clock(*(TemporalInstant(0,n) for n in range(1,6)))
    transaction = TemporalTransactionContext.begin(source)
    first = transaction.begin_statement()
    assert source.reads == 2
    for _ in range(100):
        assert first.instant("transaction") is transaction.transaction_instant
        assert first.instant() is first.statement_instant
        assert first.current("datetime").nanosecond == 2
        assert first.current("datetime",mode="transaction").nanosecond == 1
    assert source.reads == 2
    assert first.current("datetime",mode="realtime").nanosecond == 3
    second = transaction.begin_statement()
    assert second.instant().nanosecond == 4
    assert second.instant("transaction") is first.instant("transaction")
    assert first.instant().nanosecond == 2
    other = TemporalTransactionContext.begin(source)
    assert other.transaction_instant.nanosecond == 5
    assert source.reads == 5


def test_wall_clock_can_move_backwards_without_rewriting_any_capture():
    source = Clock(*(TemporalInstant(n) for n in (20,10,5,30)))
    transaction = TemporalTransactionContext.begin(source)
    statement = transaction.begin_statement()
    assert statement.instant("realtime").seconds == 5
    assert statement.instant().seconds == 10
    assert statement.instant("transaction").seconds == 20
    later = transaction.begin_statement()
    assert later.instant().seconds == 30
    assert statement.instant().seconds == 10


@pytest.mark.parametrize("kind,expected", [
    ("date","1969-12-31"),("localtime","23:00:00.123456789"),("time","23:00:00.123456789-01:00"),
    ("localdatetime","1969-12-31T23:00:00.123456789"),("datetime","1969-12-31T23:00:00.123456789-01:00"),
])
def test_current_families_apply_explicit_zone_before_extracting_components(kind,expected):
    source = Clock(TemporalInstant(0),TemporalInstant(0,123456789))
    statement = TemporalTransactionContext.begin(source,timezone="-01:00").begin_statement()
    assert statement.current(kind).isoformat() == expected
    assert source.reads == 2
    assert statement.current("datetime",timezone="+00:00:30").isoformat() == "1970-01-01T00:00:30.123456789+00:00:30"


def test_named_zone_overlap_preserves_each_captured_instant_and_offset():
    resolver = ZoneInfoTemporalResolver()
    wall = LocalDateTimeValue(DateValue(2020,11,1),LocalTimeValue.from_components(1,30,0,1))
    early = resolver.at_local(wall,"America/New_York")
    late = resolver.at_local(wall,"America/New_York",offset_seconds=-18000)
    source = Clock(TemporalInstant(early.epoch_seconds,1),TemporalInstant(late.epoch_seconds,1))
    statement = TemporalTransactionContext.begin(source,resolver=resolver,timezone="America/New_York").begin_statement()
    assert statement.current("datetime",mode="transaction") == early
    assert statement.current("datetime") == late
    assert statement.current("time").offset_seconds == -18000
    assert statement.current("localdatetime") == wall
    assert source.reads == 2


def test_fixed_scope_values_are_immutable_and_shared_readers_do_not_advance_clock():
    source = Clock(TemporalInstant(0,1),TemporalInstant(0,2))
    statement = TemporalTransactionContext.begin(source).begin_statement()
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _:statement.current("datetime"),range(400)))
    assert all(value == values[0] for value in values) and source.reads == 2
    with pytest.raises(FrozenInstanceError):
        statement.statement_instant = TemporalInstant(4)
    with pytest.raises(FrozenInstanceError):
        statement.statement_instant.nanosecond = 4


@pytest.mark.parametrize("kind,mode", [("duration","realtime"),("datetime","bad"),
    (True,"statement"),("datetime",False),("x"*10000,"realtime"),("datetime","x"*10000)])
def test_invalid_selector_never_reads_live_clock(kind,mode):
    source = Clock(TemporalInstant(0),TemporalInstant(1))
    statement = TemporalTransactionContext.begin(source).begin_statement()
    with pytest.raises(SchemaMismatchError):
        statement.current(kind,mode=mode)
    assert source.reads == 2


@pytest.mark.parametrize("zone,error", [(True,SchemaMismatchError),("+18:01",SchemaMismatchError),
    ("../UTC",SchemaMismatchError),("",SchemaMismatchError),("Europe/London",GrafxUnsupportedOperation)])
def test_bad_or_unconfigured_timezone_refuses_before_clock_capture(zone,error):
    source = Clock()
    with pytest.raises(error):
        TemporalTransactionContext.begin(source,timezone=zone)
    assert source.reads == 0


@pytest.mark.parametrize("bad", [True,1,1.0,None,(0,0),DateTimeValue.from_epoch_parts(0)])
def test_temporal_port_must_return_native_exact_instant(bad):
    with pytest.raises(SchemaMismatchError):
        TemporalTransactionContext.begin(Clock(bad))


@pytest.mark.parametrize("raw,expected", [(-1,TemporalInstant(-1,999999999)),(0,TemporalInstant(0)),
    (1234567890123456789,TemporalInstant(1234567890,123456789)),
    ((1<<63)*1000000000-1,TemporalInstant((1<<63)-1,999999999))])
def test_system_adapter_uses_exact_integer_nanoseconds(monkeypatch,raw,expected):
    import okto_grafx.adapters.temporal_clock as module
    monkeypatch.setattr(module,"time_ns",lambda:raw)
    assert SystemTemporalClock().now() == expected


@pytest.mark.parametrize("raw", [True,1.0,None,1<<100])
def test_system_adapter_refuses_invalid_clock_values(monkeypatch,raw):
    import okto_grafx.adapters.temporal_clock as module
    monkeypatch.setattr(module,"time_ns",lambda:raw)
    with pytest.raises(SchemaMismatchError):
        SystemTemporalClock().now()


def test_system_clock_read_failure_is_typed(monkeypatch):
    import okto_grafx.adapters.temporal_clock as module
    def broken():
        raise OSError("clock unavailable")
    monkeypatch.setattr(module,"time_ns",broken)
    with pytest.raises(GrafxUnsupportedOperation):
        SystemTemporalClock().now()


@pytest.mark.parametrize("bad", [DateTimeValue.from_epoch_parts(1,zone="UTC"),
    DateTimeValue.from_epoch_parts(0,1,zone="UTC"),DateTimeValue.from_epoch_parts(0,zone="Etc/UTC")])
def test_zone_provider_cannot_change_captured_instant_or_zone_identity(bad):
    class BadZone:
        def at_instant(self,*args):
            return bad
    statement = TemporalTransactionContext.begin(Clock(TemporalInstant(0),TemporalInstant(0)),
                                                 resolver=BadZone(),timezone="UTC").begin_statement()
    with pytest.raises(GrafxCorruptionDetected):
        statement.current("datetime")


def test_clock_instant_is_not_a_stored_type_but_current_values_are():
    from okto_grafx.domain.model.value import encode_value, decode_value
    statement = TemporalTransactionContext.begin(Clock(TemporalInstant(0),TemporalInstant(1))).begin_statement()
    with pytest.raises(SchemaMismatchError):
        encode_value(statement.instant())
    for value in (statement.current("datetime"),statement.current("date")):
        assert decode_value(encode_value(value))[0] == value


def test_failed_statement_capture_does_not_replace_transaction_clock():
    clock = Clock(TemporalInstant(1),None,TemporalInstant(3))
    transaction = TemporalTransactionContext.begin(clock)
    with pytest.raises(SchemaMismatchError):
        transaction.begin_statement()
    statement = transaction.begin_statement()
    assert statement.instant("transaction") == TemporalInstant(1)
    assert statement.instant() == TemporalInstant(3)


def test_timezone_defaults_and_overrides_are_context_local():
    west = TemporalTransactionContext.begin(Clock(TemporalInstant(0),TemporalInstant(0)),timezone="-01:00").begin_statement()
    east = TemporalTransactionContext.begin(Clock(TemporalInstant(0),TemporalInstant(0)),timezone="+01:00").begin_statement()
    assert west.current("date") == DateValue(1969,12,31)
    assert east.current("date") == DateValue(1970,1,1)
    assert west.current("date",timezone="Z") == DateValue(1970,1,1)
    assert west.current("date") == DateValue(1969,12,31)


def test_bad_zone_result_type_refuses_before_accessing_host_properties():
    class Host:
        @property
        def epoch_seconds(self):
            raise AssertionError("host value properties must not be invoked")
    class BadZone:
        def at_instant(self,*args):
            return Host()
    statement = TemporalTransactionContext.begin(Clock(TemporalInstant(0),TemporalInstant(0)),
        resolver=BadZone(),timezone="UTC").begin_statement()
    with pytest.raises(SchemaMismatchError):
        statement.current("datetime")
