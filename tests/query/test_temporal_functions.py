"""Native temporal function evaluation, query clocks and persisted query results."""

import pytest

from okto_grafx import connect, DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue
from okto_grafx.errors import GrafxError
from okto_grafx.adapters.temporal_clock import SystemTemporalClock
from okto_grafx.domain.model.temporal_values import TemporalInstant


@pytest.mark.parametrize("expression, expected", [
    ("date('2024-02-29')", DateValue(2024, 2, 29)),
    ("date({year:2024, ordinalDay:60})", DateValue(2024, 2, 29)),
    ("localtime('12:34:56.123456789')", LocalTimeValue.from_components(12, 34, 56, 123456789)),
    ("time({hour:12, minute:34, timezone:'+01:00'})", TimeValue(LocalTimeValue.from_components(12, 34), 3600)),
    ("localdatetime('2024-02-29T12:34')", LocalDateTimeValue(DateValue(2024, 2, 29), LocalTimeValue.from_components(12, 34))),
    ("datetime('2024-02-29T12:34+01:00')", DateTimeValue(LocalDateTimeValue(DateValue(2024, 2, 29), LocalTimeValue.from_components(12, 34)), 3600)),
    ("duration('P1M2DT3.000000004S')", DurationValue(1, 2, 3, 4)),
    ("duration({months:1, days:2, nanoseconds:4})", DurationValue(1, 2, 0, 4)),
    ("datetime.fromepoch(-1, 123)", DateTimeValue.from_epoch_parts(-1, 123)),
    ("datetime.fromepochmillis(-1)", DateTimeValue.from_epoch_parts(-1, 999000000)),
    ("date.truncate('month', date('2024-02-29'))", DateValue(2024, 2)),
    ("duration.inDays(date('2024-02-28'), date('2024-03-01'))", DurationValue(days=2)),
    ("duration.between(date('2024-01-01'), date('2024-03-02'))", DurationValue(months=2, days=1)),
    ("toString(time('12:34:56.123456789+01:00'))", "12:34:56.123456789+01:00"),
    ("toString(duration('P1M'))", "P1M"),
])
def test_native_constructor_and_dotted_function_results(expression, expected):
    with connect(":memory:") as db:
        assert db.execute("RETURN " + expression + " AS value").rows == ((expected,),)


@pytest.mark.parametrize("name", ["date", "localtime", "time", "localdatetime", "datetime", "duration"])
def test_constructor_null_propagation_and_case_insensitive_name(name):
    with connect(":memory:") as db:
        assert db.execute(f"RETURN {name.upper()}(null)").rows == ((None,),)


@pytest.mark.parametrize("expression", ["date(42)", "time(true)", "duration()", "date(1,2)",
                                        "date('2023-02-29')", "datetime.unknown()",
                                        "datetime.statement({other:1})", "arbitrary.procedure()"])
def test_invalid_function_inputs_refuse_natively(expression):
    with connect(":memory:") as db:
        with pytest.raises(GrafxError):
            db.execute("RETURN " + expression)


def test_default_statement_transaction_and_realtime_clocks(monkeypatch):
    counter = [0]
    def now(self):
        counter[0] += 1
        return TemporalInstant(counter[0], 123)
    monkeypatch.setattr(SystemTemporalClock, "now", now)
    with connect(":memory:") as db:
        with db.begin("read") as tx:
            first = tx.execute("RETURN datetime() AS a, datetime.statement() AS b, datetime.transaction() AS c, datetime.realtime() AS d, datetime.realtime() AS e").rows[0]
            second = tx.execute("RETURN datetime(), datetime.transaction()").rows[0]
            assert first[0] == first[1]
            assert first[2] == second[1]
            assert first[0].epoch_seconds < second[0].epoch_seconds
            assert first[2].epoch_seconds < first[0].epoch_seconds < first[3].epoch_seconds < first[4].epoch_seconds
            assert all(value.nanosecond == 123 for value in (*first, *second))


def test_named_zone_constructor_and_selection_are_native():
    with connect(":memory:") as db:
        row = db.execute("RETURN datetime('2024-01-01T12:00[Europe/Paris]'), date(datetime('2024-01-01T12:00Z'))").rows[0]
        assert row[0].zone == "Europe/Paris" and row[0].offset_seconds == 3600
        assert row[1] == DateValue(2024)


def test_query_constructed_values_persist_and_failed_statement_rolls_back(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val DATE, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, val:date('2024-02-29')})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND ['2024-01-01', '2023-02-29'] AS d CREATE (:N {id:2, val:date(d)})")
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((DateValue(2024,2,29),),)
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH (n:N) RETURN toString(n.val)").rows == (("2024-02-29",),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("name, expected", [
    ("date", DateValue), ("localtime", LocalTimeValue), ("time", TimeValue),
    ("localdatetime", LocalDateTimeValue), ("datetime", DateTimeValue),
])
@pytest.mark.parametrize("mode", ["transaction", "statement", "realtime"])
def test_all_clock_families_and_variants_accept_timezone(name, expected, mode, monkeypatch):
    monkeypatch.setattr(SystemTemporalClock, "now", lambda self: TemporalInstant(0, 123))
    with connect(":memory:") as db:
        value = db.execute(f"RETURN {name}.{mode}({{timeZone:'+01:00'}})").rows[0][0]
        assert type(value) is expected
        if name in ("time", "datetime"):
            assert value.offset_seconds == 3600


@pytest.mark.parametrize("name", ["date", "localtime", "time", "localdatetime", "datetime"])
def test_default_clock_and_current_timezone_map(name, monkeypatch):
    monkeypatch.setattr(SystemTemporalClock, "now", lambda self: TemporalInstant(0))
    with connect(":memory:") as db:
        ordinary, selected = db.execute(f"RETURN {name}() AS a, {name}({{timezone:'Z'}}) AS b").rows[0]
        assert ordinary == selected
        with pytest.raises(GrafxError):
            db.execute(f"RETURN {name}.statement({{timezone:null}})")


def test_streamed_statement_clock_remains_fixed_across_fetches(monkeypatch):
    counter = [0]
    def now(self):
        counter[0] += 1
        return TemporalInstant(counter[0])
    monkeypatch.setattr(SystemTemporalClock, "now", now)
    with connect(":memory:") as db:
        with db.query("UNWIND [1,2,3] AS i RETURN datetime.statement() AS t").cursor(batch_size=1) as cursor:
            rows = list(cursor)
        assert len(rows) == 3 and rows[0] == rows[1] == rows[2]


def test_invalid_transaction_clock_factory_does_not_register_partial_transaction():
    with connect(":memory:") as db:
        manager = db._transactions
        factory = manager._temporal_context_factory
        before = manager.open_transactions
        manager._temporal_context_factory = lambda: None
        try:
            with pytest.raises(GrafxError):
                db.begin("read")
            assert manager.open_transactions == before
        finally:
            manager._temporal_context_factory = factory
        with db.begin("read") as tx:
            assert type(tx.execute("RETURN date()").rows[0][0]) is DateValue
