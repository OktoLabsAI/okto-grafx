"""Existential patterns use native reads, lexical references and statement budgets."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.ast import PatternPredicate, free_variables, walk


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,k INT64)")
            tx.execute("CREATE (a:N {id:1})-[:R {k:1}]->(b:N {id:2})-[:R {k:2}]->(c:N {id:3}), (:N {id:4})")
            tx.execute("MATCH(a:N {id:1}),(b:N {id:2}) CREATE (a)-[:R {k:3}]->(b)")
        yield db


@pytest.mark.parametrize("predicate,expected", [
    ("(n)-->()", (1,2)), ("(n)<--()", (2,3)), ("(n)--()", (1,2,3)),
    ("(n)-[:R*2]->()", (1,)), ("(n)-[:R*0..0]->()", (1,2,3,4)),
    ("NOT (n)-[:R]->()", (3,4)), ("(n)-[:Missing]->()", ()),
    ("(n)-[:Missing|R]->()", (1,2)),
    ("(n)-[:R]->() AND (n)<-[:R]-()", (2,)),
    ("(n)-[:R]->() OR (n)<-[:R]-()", (1,2,3)),
    ("(n)-[:R]->({id:$target})", (2,)),
    ("(n)-[:R]->()-[:R]->()", (1,)),
])
def test_native_boolean_existence_does_not_multiply_outer_rows(graph, predicate, expected):
    result = graph.execute(f"MATCH(n:N) WHERE {predicate} RETURN n.id ORDER BY n.id", {"target":3})
    assert result.rows == tuple((number,) for number in expected)
    assert result.plan is not None


def test_correlated_endpoints_aliases_and_null_do_not_scan_as_unbound(graph):
    assert graph.execute("MATCH(n:N),(m:N) WHERE (n)-->(m) RETURN n.id,m.id ORDER BY n.id,m.id").rows == ((1,2),(2,3))
    assert graph.execute("MATCH(n:N) WITH n AS a WHERE (a)-->() RETURN a.id ORDER BY a.id").rows == ((1,),(2,))
    assert graph.execute("MATCH(n:N {id:4}) OPTIONAL MATCH(n)-->(m) WITH m WHERE NOT (m)-->() RETURN m").rows == ()


def test_optional_clause_predicate_filters_before_null_extension(graph):
    result = graph.execute("MATCH(n:N) OPTIONAL MATCH(n)-[:R]->(m:N) "
        "WHERE (m)-[:R]->() RETURN n.id,m.id ORDER BY n.id,m.id")
    assert result.rows == ((1,2),(1,2),(2,None),(3,None),(4,None))


def test_pending_writes_rollback_and_other_reader(graph):
    query = "MATCH(n:N {id:4}) WHERE (n)-->() RETURN n.id"
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:N {id:4}),(b:N {id:1}) CREATE(a)-[:R]->(b)")
        assert tx.execute(query).rows == ((4,),)
        assert graph.execute(query).rows == ()
        tx.rollback()
    assert graph.execute(query).rows == ()


def test_bound_relationship_is_identity_constraint_not_a_fresh_binding(graph):
    result = graph.execute("MATCH(a:N)-[r:R]->(b:N),(c:N) "
        "WHERE (c)-[r:R]->(b) RETURN a.id,c.id ORDER BY a.id,c.id")
    assert result.rows == ((1,1),(1,1),(2,2))


def test_predicate_existence_short_circuits_and_shares_expansion_budget(graph, tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    with connect(tmp_path / "db", max_traversal_expansions=1) as limited:
        # Materialize one outer anchor before the predicate. This does not rely
        # on an optional index reordering WHERE and inline node-property checks.
        anchor = "MATCH(n:N) WITH n ORDER BY n.id LIMIT 1 WHERE "
        assert limited.execute(anchor + "(n)-->() RETURN n.id").rows == ((1,),)
        # A skipped boolean branch must not inspect any relationship.
        assert limited.execute(anchor + "true OR (n)-[:R*2]->() RETURN n.id").rows == ((1,),)
        with pytest.raises(GrafxQueryBudgetExceeded):
            limited.execute(anchor + "(n)-[:R*2]->() RETURN n.id")
        assert limited.execute(anchor + "(n)-->() RETURN n.id").rows == ((1,),)


def test_subquery_predicate_and_prepared_reuse_observe_current_commits(graph):
    query = "MATCH(n:N) CALL(n) { WITH n WHERE (n)-->() RETURN n.id AS id } RETURN id ORDER BY id"
    assert graph.execute(query).rows == ((1,),(2,))
    with graph.begin("write") as tx:
        tx.execute("MATCH(n:N {id:4}),(m:N {id:1}) CREATE(n)-[:R]->(m)")
    assert graph.execute(query).rows == ((1,),(2,),(4,))


def test_structural_admission_rejects_mutable_pattern_inventory():
    from dataclasses import replace
    from okto_grafx.domain.query.ast import PatternPath, NodePattern
    statement = parse("MATCH(n) WHERE (n)-->() RETURN n")
    clause = statement.match_clauses[0]
    bad = PatternPredicate(PatternPath(nodes=[NodePattern(variable="n")]))
    with pytest.raises(GrafxError):
        analyze(replace(statement, clause_pipeline=(replace(clause, predicate=bad),)))


def test_invalid_pattern_reference_is_rejected_before_prior_work_is_changed(graph):
    with graph.begin("write") as tx:
        tx.execute("CREATE(:N {id:99})")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(n:N) WHERE (n)-[undeclared]->() DELETE n")
        assert tx.execute("MATCH(n:N {id:99}) RETURN n.id").rows == ((99,),)


@pytest.mark.parametrize("text", [
    "MATCH(n) WHERE (n)-[r]->() RETURN n",
    "MATCH(n) WHERE (n)-->(unknown) RETURN n",
    "MATCH(n) RETURN (n)-->()",
    "MATCH(n) WITH (n)-->() AS x RETURN x",
    "MATCH(n) SET n.x=(n)-->()",
])
def test_pattern_scope_and_value_position_refusals(text):
    with pytest.raises(GrafxError):
        analyze(parse(text))


def test_pattern_ast_preserves_parameters_reference_names_and_boolean_precedence():
    statement = parse("MATCH(n),(m) WHERE NOT (n)-[:R*1..2]->(m {id:$id}) OR n.id=2 RETURN n")
    predicate = statement.match_clauses[0].predicate
    patterns = [node for node in walk(predicate) if isinstance(node, PatternPredicate)]
    assert len(patterns) == 1 and free_variables(patterns[0]) == ("n", "m")
    assert analyze(statement).parameters == ("id",)
    assert parse(statement.describe()).match_clauses[0].predicate == predicate


@pytest.mark.parametrize("expression", ["(n.id)-1=1", "(n.id)<-1", "((n.id)+1)=2"])
def test_parenthesized_arithmetic_is_not_a_pattern(graph, expression):
    graph.execute(f"MATCH(n:N) WHERE {expression} RETURN n.id")
