"""Temporal operators through the public query API, including spill and rollback."""

import pytest

from okto_grafx import connect, DateValue, DateTimeValue, LocalDateTimeValue, LocalTimeValue, TimeValue, DurationValue
from okto_grafx.errors import GrafxError
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory


@pytest.mark.parametrize("expression, expected", [
    ("date('2024-02-29').ordinalDay", 60),
    ("localtime('12:34:56.123456789').nanosecond", 123456789),
    ("time('12:34+01:00').offset", "+01:00"),
    ("datetime('2024-02-29T12:00[Europe/Paris]').timezone", "Europe/Paris"),
    ("duration('P1DT3.000000004S').nanosecondsOfSecond", 4),
    ("duration.inDays(date('2024-01-01'), date('2024-01-03')).days + 1", 3),
    ("date('2024-01-31') + duration('P1M')", DateValue(2024, 2, 29)),
    ("duration('P1M') + date('2024-01-31')", DateValue(2024, 2, 29)),
    ("date('2024-03-31') - duration('P1M')", DateValue(2024, 2, 29)),
    ("duration('P1D') + duration('PT1S')", DurationValue(days=1, seconds=1)),
    ("duration('P1D') - duration('PT1S')", DurationValue(days=1, seconds=-1)),
    ("duration('P1D') * 0.5", DurationValue(seconds=43200)),
    ("0.5 * duration('P1D')", DurationValue(seconds=43200)),
    ("duration('P1D') / 2", DurationValue(seconds=43200)),
    ("-duration('PT0.000000001S')", DurationValue(seconds=-1, nanoseconds=999999999)),
    ("+duration('P1D')", DurationValue(days=1)),
    ("date('2024-01-01') < date('2025-01-01')", True),
    ("date('2024-01-01') < localdatetime('2025-01-01T00:00')", None),
    ("duration('P1D') < duration('P1D')", None),
    ("duration('P1D') <= duration('P1D')", True),
    ("duration('P1D') = duration('PT24H')", False),
    ("[date('2024-01-01')] < [date('2025-01-01')]", True),
    ("date('2024-01-01') + [1]", (DateValue(2024), 1)),
    ("duration('P1D') * null", None),
])
def test_temporal_query_fields_arithmetic_and_predicates(expression, expected):
    with connect(":memory:") as db:
        assert db.execute("RETURN " + expression + " AS value").rows == ((expected,),)


@pytest.mark.parametrize("query", [
    "RETURN $value.year AS value",
    "WITH $value AS d RETURN d.year AS value",
    "UNWIND [$value] AS d RETURN d.year AS value",
    "RETURN (CASE WHEN true THEN $value ELSE null END).year AS value",
])
def test_temporal_fields_from_parameters_and_projected_values(query):
    with connect(":memory:") as db:
        assert db.execute(query, {"value": DateValue(2024, 2, 29)}).rows == ((2024,),)


@pytest.mark.parametrize("expression", [
    "date('2024-01-01').hour", "duration('P1D').timezone", "duration('P1D') / 0",
    "date('2024-01-01') + 1", "1 / duration('P1D')", "duration('P1D') * true",
    "duration('P1D') * (0.0 / 0.0)", "-date('2024-01-01')",
])
def test_invalid_temporal_operations_refuse_with_native_errors(expression):
    with connect(":memory:") as db:
        with pytest.raises(GrafxError):
            db.execute("RETURN " + expression)


def test_named_zone_calendar_day_differs_from_elapsed_hours():
    with connect(":memory:") as db:
        calendar, elapsed = db.execute("WITH datetime('2024-03-30T12:00[Europe/Paris]') AS d "
                                      "RETURN d + duration('P1D') AS calendar, d + duration('PT24H') AS elapsed").rows[0]
        assert calendar.local.time.hour == 12 and elapsed.local.time.hour == 13
        assert calendar.offset_seconds == elapsed.offset_seconds == 7200
        assert calendar.zone == elapsed.zone == "Europe/Paris"


def test_temporal_ordering_has_explicit_family_and_duration_ties():
    zoned = DateTimeValue.from_epoch_parts(0, 0)
    local = LocalDateTimeValue(DateValue(1970), LocalTimeValue(0))
    date = DateValue(1970)
    time = TimeValue(LocalTimeValue(0), 0)
    clock = LocalTimeValue(0)
    elapsed = DurationValue(seconds=86400)
    calendar = DurationValue(days=1)
    expected = [zoned, local, date, time, clock, elapsed, calendar]
    with connect(":memory:") as db:
        assert db.execute("UNWIND $xs AS x RETURN x ORDER BY x", {"xs": list(reversed(expected))}).rows == tuple((v,) for v in expected)
        assert db.execute("UNWIND $xs AS x RETURN min(x), max(x)", {"xs": expected}).rows == ((zoned, calendar),)


def test_unselected_temporal_field_does_not_probe_overflow_or_bad_family():
    with connect(":memory:") as db:
        for expression in ("duration({seconds:9223372036854775807}).milliseconds", "date('2024-01-01').hour"):
            assert db.execute(f"RETURN CASE WHEN false THEN {expression} ELSE 42 END AS value").rows == ((42,),)
            with pytest.raises(GrafxError):
                db.execute("RETURN " + expression)


def test_late_temporal_arithmetic_failure_rolls_back_statement_and_reopens(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE (:Event {id:1, day:date('2024-01-31')})")
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,0] AS factor CREATE (:Event {id:2, day:date('2024-01-31') + duration('P1M') / factor})")
            tx.execute("MATCH (n:Event {id:1}) SET n.day = n.day + duration('P1M')")
        assert db.execute("MATCH (n:Event) RETURN n.id, n.day.year, n.day").rows == ((1, 2024, DateValue(2024,2,29)),)
    with connect(path) as db:
        assert db.execute("MATCH (n:Event) RETURN n.id, n.day").rows == ((1, DateValue(2024,2,29)),)


@pytest.mark.parametrize("query", [
    "UNWIND $xs AS x RETURN x ORDER BY x",
    "UNWIND $xs AS x RETURN DISTINCT x",
    "UNWIND $xs AS x RETURN x, count(*) AS n",
    "UNWIND $xs AS x RETURN count(DISTINCT x)",
    "UNWIND $xs AS x RETURN min(x), max(x)",
])
def test_temporal_spill_and_in_memory_results_are_identical(query, monkeypatch):
    values = [DateValue(-999999999), DateValue(2024), DateValue(999999999),
              LocalTimeValue(0), TimeValue(LocalTimeValue(0), 3600),
              LocalDateTimeValue(DateValue(2024), LocalTimeValue(0)),
              DateTimeValue.from_epoch_parts(0, 1), DurationValue(months=2**63-1),
              DurationValue(days=1), DurationValue(seconds=86400), None] * 12
    values.extend(DateValue(2000 + year) for year in range(100))
    with connect(":memory:") as db:
        expected = db.execute(query, {"xs": values}).rows
    opened = []
    original = LocalQuerySpillFactory.open
    def record_open(factory, budget):
        opened.append(True)
        return original(factory, budget)
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record_open)
    with connect(":memory:", query_memory_budget_bytes=4096) as db:
        assert db.execute(query, {"xs": values}).rows == expected
    assert opened, "This test must actually spill temporal payloads and comparison keys."
