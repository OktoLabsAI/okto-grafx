"""Alternative type delimiters and bidirectional read arrows execute natively."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxParseError, GrafxPlanError
from okto_grafx.domain.query.parser import parse


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "spelling") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64)")
            tx.execute("CREATE NODE TABLE B(id INT64)")
            tx.execute("CREATE REL TABLE R(FROM A TO B, k INT64)")
            tx.execute("CREATE REL TABLE S(FROM B TO A, k INT64)")
            tx.execute("CREATE REL TABLE Loop(FROM A TO A, k INT64)")
            tx.execute("CREATE(a:A {id:1}), (b:B {id:2}) CREATE(a)-[:R {k:7}]->(b), (b)-[:S {k:8}]->(a)")
        yield db


@pytest.mark.parametrize("types", ["R|:S", "R|S|:R", "`R`|:`S`|:`R`", "R|:Missing|S"])
def test_colon_alternatives_preserve_qualified_edges_without_duplicates(graph, types):
    rows = graph.execute(f"MATCH()-[r:{types}]->() RETURN type(r),r.k ORDER BY r.k").rows
    assert rows == (("R",7),("S",8))
    assert parse(f"MATCH()-[:{types}]->() RETURN 1").describe().count("|:") == 0


@pytest.mark.parametrize("suffix,count", [("<-->(n)",4), ("<--(n)",2)])
def test_original_cycle_multiplicity_and_physical_directions(graph, suffix, count):
    rows = graph.execute(f"MATCH p=(n)<-->(k){suffix} RETURN p").rows
    assert len(rows) == count
    assert len({tuple(edge.identity for edge in row[0].relationships) + (row[0].nodes[0].identity,) for row in rows}) == count
    for (path,) in rows:
        assert path.nodes[0].identity == path.nodes[-1].identity
        assert len({edge.identity for edge in path.relationships}) == 2
        assert {edge.label for edge in path.relationships} == {"R","S"}
        for edge in path.relationships:
            expected = ("A","B") if edge.label == "R" else ("B","A")
            by_identity = {node.identity:node.label for node in path.nodes}
            assert (by_identity[edge.source],by_identity[edge.target]) == expected


def test_both_arrows_range_maps_optional_predicate_and_comprehension(graph):
    assert graph.execute("MATCH p=(:A)<-[:R|:S*2]->() RETURN length(p)").rows == ((2,),(2,))
    assert graph.execute("MATCH p=(:A)<-[:R*0..0 {k:999}]->() RETURN length(p)").rows == ((0,),)
    assert graph.execute("MATCH(n:A) WHERE (n)<-[:R {k:7}]->() RETURN n.id").rows == ((1,),)
    assert graph.execute("MATCH(n:A) RETURN [(n)<-[:R|:S]->(m) | m.id]").rows == (((2,2),),)
    assert graph.execute("MATCH(n:A) OPTIONAL MATCH p=(n)<-[:Missing]->(m) RETURN n.id,p,m").rows == ((1,None,None),)
    query = "MATCH p=(a)<-[:R]->(b) RETURN a.id,b.id ORDER BY a.id"
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ((1,2),(2,1))


def test_self_loop_is_not_doubled_by_two_arrowheads(graph):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:A) CREATE(a)-[:Loop {k:9}]->(a)")
    assert graph.execute("MATCH p=()<-[r:Loop]->() RETURN length(p),r.k").rows == ((1,9),)


@pytest.mark.parametrize("query", ["CREATE(a:A)<-[:Loop]->(b:A)",
                                   "MATCH(a:A) MERGE(a)<-[:Loop]->(a)",
                                   "MATCH(a:A) CREATE(a)-[:R|:S]->(a)"])
def test_new_read_spellings_do_not_authorize_ambiguous_writes(graph, query):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:A) SET a.id=10")
        with pytest.raises(GrafxPlanError):
            tx.execute(query)
        assert tx.execute("MATCH(a:A) RETURN a.id").rows == ((10,),)
        assert tx.execute("MATCH()-[r]->() RETURN count(r)").rows == ((2,),)
    assert graph.verify("all").findings == ()


@pytest.mark.parametrize("types", ["R|:", "R|::S", "R||S", "R|:|S"])
def test_malformed_type_alternatives_still_refuse(types):
    with pytest.raises(GrafxParseError):
        parse(f"MATCH()-[:{types}]->() RETURN 1")
