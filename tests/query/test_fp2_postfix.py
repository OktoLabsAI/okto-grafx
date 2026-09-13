"""General postfix composition on expression results, not just variable bindings."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize("expression, expected", [
    ("head([{a: [10,20]}]).a[-1]", 20),
    ("coalesce(null, {a: [{b: 7}]}).a[0].b", 7),
    ("(CASE WHEN true THEN {a: [9]} ELSE null END).a[0]", 9),
    ("([{a: 1}] + [{a: 2}])[1].a", 2),
    ("[x IN [{a: 4}] | x][0].a", 4),
    ("head([{a: null}]).a.missing", None),
    ("head([{a: 1}]).missing[0]", None),
    ("coalesce(null, {a: [1,2,3]}).a[1..][-1]", 3),
    ("head([{`odd key`: 5}]).`odd key`", 5),
])
def test_postfix_after_functions_groups_case_and_lists(expression, expected):
    with connect(":memory:") as db:
        result = db.execute("RETURN " + expression)
        assert result.columns == (expression,)
        assert result.rows == ((expected,),)


def test_postfix_consumes_parameters_without_mutating_them():
    parameter = {"a": [{"b": [3, 7]}]}
    with connect(":memory:") as db:
        assert db.execute("RETURN coalesce(null,$p).a[0].b[-1]", {"p": parameter}).rows == ((7,),)
    assert parameter == {"a": [{"b": [3, 7]}]}


def test_invalid_postfix_write_is_statement_atomic():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:N {id:2}) RETURN head([{a: [1]}]).a[true]")
            assert tx.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_postfix_calls_are_not_executed_by_planner_or_binder(monkeypatch):
    from okto_grafx.engine import query_engine

    calls = []
    original = query_engine._call

    def observed(expression, row, context):
        calls.append(expression.name.upper())
        return original(expression, row, context)

    monkeypatch.setattr(query_engine, "_call", observed)
    with connect(":memory:") as db:
        query = "RETURN coalesce(null, $p).a[0] AS v"
        db.explain(query)
        assert calls == []
        for _ in range(2):
            assert db.execute(query, {"p": {"a": [9]}}).rows == ((9,),)
        assert calls == ["COALESCE", "COALESCE"]
        calls.clear()
        assert db.execute("UNWIND [] AS n RETURN coalesce(null, $p).a[0]", {"p": {"a": [9]}}).rows == ()
        assert calls == []


@pytest.mark.parametrize("query, parameters", [
    ("UNWIND [] AS n RETURN abs(1).a[0]", {}),
    ("UNWIND [] AS n RETURN abs($p).a[0]", {"p": 1}),
    ("UNWIND [] AS n RETURN coalesce(null, $p).a[0]", {"p": 1}),
    ("UNWIND [] AS n RETURN coalesce(null, {a: [1]}).a[true]", {}),
])
def test_provably_bad_postfix_types_refuse_even_without_rows(query, parameters):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError):
            db.execute(query, parameters)
