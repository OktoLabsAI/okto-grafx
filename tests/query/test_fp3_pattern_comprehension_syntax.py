"""Scoped comprehension syntax foundation; execution is explicitly still pending."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.ast import PatternComprehension, free_variables
from okto_grafx.domain.query.scopes import lower_scopes


@pytest.mark.parametrize("value", [
    "[p=(n)-->() | p]", "[(n)-->(m) | m.id]", "[(n)-[r:R]->() | r.k]",
    "[p=(n)<-[:R*0..2]-(m) WHERE m.id=$id | {node:m,path:p}]",
    "[(n)-[:R|S]->(m) WHERE (m)-->() | m]",
    "[(n)-->(m) | [(m)-->(other) | other.id]]",
    "[x IN nodes(p) | [(x)-->(y) | y]]",
])
def test_parse_analyze_and_roundtrip_local_projection(value):
    prefix = "MATCH p=(n:N)-->() " if "nodes(p)" in value else "MATCH(n:N) "
    statement = parse(prefix + "RETURN " + value + " AS result")
    analysis = analyze(statement)
    assert analysis.output_columns == ("result",)
    reparsed = parse(statement.describe())
    assert reparsed.return_clause.items[0].expression == statement.return_clause.items[0].expression
    assert {binding.name for binding in analysis.bindings} <= {"n", "p"}


def test_free_body_names_parameters_and_correlations_are_distinct():
    statement = parse("WITH 1 AS external MATCH(n:N) "
                      "RETURN [(n)-->(m {id:$id}) WHERE m.id=external | m.id] AS values")
    expression = statement.return_clause.items[0].expression
    assert isinstance(expression, PatternComprehension)
    assert expression.local_names() == ("n", "m")
    assert free_variables(expression) == ("external",)
    assert analyze(statement).parameters == ("id",)


def test_list_local_graph_references_follow_the_lowered_binder_not_outer_spelling():
    original = parse("MATCH p=(x:N)-->() RETURN [x IN nodes(p) | [(x)-->(y) | y]] AS values")
    analyze(original)
    lowered = lower_scopes(original)
    analyze(lowered)
    iteration = lowered.return_clause.items[0].expression
    assert iteration.variable != "x"
    assert iteration.body.pattern.nodes[0].variable == iteration.variable
    assert lowered.match_clauses[0].patterns[0].nodes[0].variable == "x"


def test_with_alias_correlation_is_rewritten_without_leaking_comprehension_names():
    original = parse("MATCH(n:N) WITH n AS n RETURN [(n)-->(m) | m] AS values")
    analyze(original)
    lowered = lower_scopes(original)
    analysis = analyze(lowered)
    expression = lowered.return_clause.items[0].expression
    assert expression.pattern.nodes[0].variable == lowered.with_clauses[0].items[0].name
    assert analysis.binding("m") is None


@pytest.mark.parametrize("query", [
    "MATCH(n:N) RETURN [(n)-->(m) | absent] AS values",
    "MATCH(n:N) RETURN [(n)-->(m) WHERE missing.id=1 | m] AS values",
    "MATCH(n:N) RETURN [(n)-->(m) | m] AS values,m",
    "MATCH(n:N) WITH [(n)-->(m) | m] AS values RETURN m",
    "MATCH(n:N) RETURN [(n)-[m:R]->(m) | m] AS values",
    "MATCH(n:N) RETURN [(n)-->(m) | count(m)] AS values",
    "MATCH(n:N) RETURN [(n)-->(m) WHERE count(m)>0 | m] AS values",
])
def test_local_names_do_not_leak_and_invalid_local_bodies_refuse(query):
    with pytest.raises(GrafxError):
        analyze(parse(query))


def test_correlated_list_node_rematch_uses_label_membership(tmp_path):
    query = ("MATCH p=(n:N)-[:R]->(n) "
             "RETURN [x IN nodes(p) | [(x:N)-[:R]->(x:Other) | x.id]] AS values "
             "ORDER BY n.id")
    analyze(parse(query))
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (a:N:Other {id:1}), (b:N {id:2}) CREATE (a)-[:R]->(a), (b)-[:R]->(b)")
        rows = db.execute(query).rows
        assert len(rows) == 2
        assert rows[0][0] == ((1,), (1,))
        assert rows[1][0] == ((), ())


@pytest.mark.parametrize("value", ["[x=1]", "[x=(1)]", "[(x)-1]", "[(x)<-1]", "[x IN [1,2] | x+1]"])
def test_nonpattern_list_grammar_remains_available(value):
    statement = parse("WITH 1 AS x RETURN " + value + " AS values")
    analyze(statement)
    assert not isinstance(statement.return_clause.items[0].expression, PatternComprehension)


@pytest.mark.parametrize("value", ["[p=(n) | p]", "[(n)-->() | ]", "[(n)-->() WHERE | 1]", "[(n)-->() 1]"])
def test_incomplete_pattern_comprehensions_refuse(value):
    with pytest.raises(GrafxError):
        parse("MATCH(n:N) RETURN " + value)


def test_hostile_mutable_pattern_inventory_refuses_structurally():
    statement = parse("MATCH(n:N) RETURN [(n)-->(m) | m] AS values")
    expression = statement.return_clause.items[0].expression
    bad = replace(expression, pattern=replace(expression.pattern, nodes=list(expression.pattern.nodes)))
    changed = replace(statement, return_clause=replace(statement.return_clause,
                      items=(replace(statement.return_clause.items[0], expression=bad),)))
    with pytest.raises(GrafxPlanError) as error:
        analyze(changed)
    assert error.value.details["reason"] == "invalid_ast_structure"


def test_nested_comprehensions_cannot_bypass_expression_depth_limit():
    value = "1"
    for _ in range(50):
        value = "[(n)-->() | " + value + "]"
    with pytest.raises(GrafxError):
        parse("MATCH(n:N) RETURN " + value)


def test_projection_failure_preserves_prior_work(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N)")
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE(n:N {id:1}), (m:N {id:2}) CREATE(n)-[:R]->(m) "
                           "RETURN [(n)-[:R]->(m) | 1 / 0] AS values")
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
    with connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings
