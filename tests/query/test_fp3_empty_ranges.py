"""Empty relationship intervals have no paths, not syntax errors or zero-hop paths."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxParseError
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.analysis import analyze


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "empty-ranges") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64)")
            tx.execute("CREATE REL TABLE R(FROM N TO N,k INT64)")
            tx.execute("CREATE(a:N {id:1}), (b:N {id:2}) CREATE(a)-[:R {k:1}]->(b)")
        yield db


@pytest.mark.parametrize("bounds", ["2..1", "1..0", "..0", "30..0"])
@pytest.mark.parametrize("kind", ["R", "R|Missing", ""])
def test_empty_range_no_path_no_scan_and_optional_null(graph, monkeypatch, bounds, kind):
    import okto_grafx.engine.query_engine as execution
    def forbidden(*args, **kwargs):
        raise AssertionError("empty interval must not enumerate traversal candidates")
    monkeypatch.setattr(execution, "_traverse", forbidden)
    monkeypatch.setattr(execution, "_traverse_any", forbidden)
    type_text = ":" + kind if kind else ""
    pattern = f"p=(a:N)-[rs{type_text}*{bounds} {{k:1}}]->(b)"
    assert graph.execute("MATCH " + pattern + " RETURN p,rs,b").rows == ()
    assert graph.execute("MATCH " + pattern + " RETURN count(p)").rows == ((0,),)
    assert graph.execute("MATCH(a:N) OPTIONAL MATCH " + pattern +
                         " RETURN a.id,p,rs,b ORDER BY a.id").rows == ((1,None,None,None),(2,None,None,None))
    with graph.query("MATCH " + pattern + " RETURN p").cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ()


def test_empty_range_is_not_zero_length_and_composes_in_expressions(graph):
    assert graph.execute("MATCH p=(a:N)-[:R*0..0]->() RETURN a.id,length(p) ORDER BY a.id").rows == ((1,0),(2,0))
    assert graph.execute("MATCH(n:N) WHERE (n)-[:R*2..1]->() RETURN n").rows == ()
    assert graph.execute("MATCH(n:N) RETURN n.id, [p=(n)-[:R*2..1]->() | p] ORDER BY n.id").rows == ((1,()),(2,()))
    assert graph.execute("MATCH(n:N) WHERE NOT (n)-[:R*2..1]->() RETURN n.id ORDER BY n.id").rows == ((1,),(2,))


def test_empty_read_consumes_prior_writes_without_writing_unmatched_rows(graph, tmp_path):
    with graph.begin("write") as tx:
        assert tx.execute("CREATE(n:N {id:3}) WITH n MATCH(n)-[:R*2..1]->(b) SET b.id=99 RETURN b").rows == ()
        assert tx.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(3,))
        with pytest.raises(GrafxError):
            tx.execute("MATCH(n:N) CREATE(n)-[:R*2..1]->(n)")
    with connect(tmp_path / "empty-ranges") as db:
        assert db.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,),(3,))
        assert db.verify("all").findings == ()


def test_caller_ast_admits_empty_interval_but_retains_independent_bounds(graph):
    original = parse("MATCH(a:N)-[:R*1..2]->(b) RETURN b")
    pattern = original.match_clauses[0].patterns[0]
    for lower, upper, valid in ((2,1,True), (31,1,False), (2,-1,False)):
        edge = replace(pattern.relationships[0], min_hops=lower, max_hops=upper)
        clause = replace(original.match_clauses[0], patterns=(replace(pattern, relationships=(edge,)),))
        statement = replace(original, match_clauses=(clause,), clause_pipeline=tuple(
            clause if item is original.match_clauses[0] else item for item in original.clause_pipeline))
        if valid:
            analyze(statement)
        else:
            with pytest.raises(GrafxPlanError):
                analyze(statement)


def test_expression_local_empty_interval_cannot_bypass_hop_resource_admission():
    statement = parse("RETURN [(:N)-[:R*1..2]->() | 1]")
    item = statement.return_clause.items[0]
    expression = item.expression
    edge = replace(expression.pattern.relationships[0], min_hops=31, max_hops=0)
    expression = replace(expression, pattern=replace(expression.pattern, relationships=(edge,)))
    statement = replace(statement, return_clause=replace(statement.return_clause,
                        items=(replace(item, expression=expression),)))
    with pytest.raises(GrafxPlanError) as error:
        analyze(statement)
    assert error.value.details["field"] == "hops"


@pytest.mark.parametrize("spelling", ["*-2", "*1..-2", ".."])
def test_malformed_ranges_have_native_phase_metadata(spelling):
    with pytest.raises(GrafxParseError) as error:
        parse(f"MATCH()-[:R{spelling}]->() RETURN 1")
    assert error.value.details["reason"] == "invalid_relationship_pattern"
    assert error.value.details["query_phase"] == "planning"
