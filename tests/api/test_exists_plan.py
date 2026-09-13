"""EXISTS syntax belongs to the closed, deeply owned public plan grammar."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.query import analyze, parse
from okto_grafx.domain.query.ast import ExistsSubquery, Literal, Query, ReturnClause, ReturnItem
from okto_grafx.domain.query.plan import ProjectRows, SingleRow
from okto_grafx.engine.public_views import _query_plan_rebuild
from okto_grafx.errors import GrafxError, GrafxPlanError


def test_owned_nested_query_and_cache_are_independent():
    with connect(":memory:") as db:
        query = "RETURN EXISTS{RETURN 1 AS value} AS found"
        first = db.execute(query)
        second = db.execute(query)
        plan = first.plan
        expression = plan.child.items[0].expression
        assert isinstance(expression, ExistsSubquery)
        object.__setattr__(expression.query.return_clause.items[0].expression, "value", 99)
        assert second.plan.child.items[0].expression.query.return_clause.items[0].expression.value == 1
        assert db.execute(query).rows == ((True,),)
        assert db.execute(query).plan.child.items[0].expression.query.return_clause.items[0].expression.value == 1


@pytest.mark.parametrize("corrupt", ["query", "imports", "cycle", "mutable"])
def test_hostile_nested_syntax_refuses(corrupt):
    expression = ExistsSubquery(Query(return_clause=ReturnClause(items=(ReturnItem(Literal(1)),))))
    if corrupt == "query":
        expression = replace(expression, query=object())
    elif corrupt == "imports":
        expression = replace(expression, imports=(("x", object()),))
    elif corrupt == "mutable":
        expression = replace(expression, query=replace(expression.query, match_clauses=[]))
    else:
        object.__setattr__(expression.query.return_clause.items[0], "expression", expression)
    raw = ProjectRows(SingleRow(), (ReturnItem(expression, "found"),))
    with pytest.raises(GrafxError):
        _query_plan_rebuild(raw)
    query = Query(return_clause=ReturnClause(items=(ReturnItem(expression),)))
    with pytest.raises(GrafxPlanError):
        analyze(query)


def test_ast_round_trip_and_inner_parameters():
    text = "WITH 1 AS x RETURN EXISTS{MATCH(n {v:$value}) WITH n WHERE n.v=x RETURN n} AS present"
    query = parse(text)
    assert parse(query.describe()).describe() == query.describe()
    assert analyze(query).parameters == ("value",)
