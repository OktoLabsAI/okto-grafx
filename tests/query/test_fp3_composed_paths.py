"""Composed named captures retain path identity, null extension and scope."""

import pytest

from okto_grafx import PathValue
from tests.query import test_fp3_native_path_results as native_paths

graph = native_paths.graph


@pytest.mark.parametrize("query", [
    "MATCH p=(a:A)-[r:R]->(b:B) WHERE a.id=1 RETURN p",
    "UNWIND [1,2] AS wanted MATCH p=(a:A)-[r:R]->(b:B) WHERE a.id=wanted RETURN p",
    "MATCH p=(a:A {id:1})-[r:R]->(b:B {id:1}) RETURN p",
    "MATCH p=(a:A)-[:R]->(b:B) WITH p AS journey RETURN journey AS p",
    "MATCH p=(a:A)-[r:R]->(b:B) WITH * RETURN p",
    "MATCH p=(a:A)-[r:R]->(b:B) RETURN DISTINCT p ORDER BY length(p) SKIP 0 LIMIT 2",
    "CALL () { MATCH p=(a:A)-[r:R]->(b:B) RETURN p } RETURN p",
])
def test_composed_capture_returns_the_same_native_path(graph, query):
    baseline = graph.execute("MATCH p=(a:A)-[r:R]->(b:B) RETURN p").rows
    assert graph.execute(query).rows == baseline


@pytest.mark.parametrize("query", [
    "MATCH p=(b:B)<-[r:R]-(a:A) RETURN p",
    "MATCH p=(b:B)<-[:R]-(a:A) RETURN p",
    "MATCH p=(b:B)-[r:R]-(a:A) RETURN p",
])
def test_reverse_walk_keeps_physical_relationship_endpoints(graph, query):
    forward = graph.execute("MATCH p=(a:A)-[r:R]->(b:B) RETURN p").rows[0][0]
    backward = graph.execute(query).rows[0][0]
    assert backward == PathValue(forward.nodes[::-1], forward.relationships)
    assert backward.relationships[0].source == backward.nodes[1].identity
    assert backward.relationships[0].target == backward.nodes[0].identity


@pytest.mark.parametrize("query,expected", [
    ("OPTIONAL MATCH p=(a:A)-[r:R]->(b:B) WHERE false RETURN p,nodes(p)", ((None,None),)),
    ("MATCH (a:A) OPTIONAL MATCH p=(a)-[r:R]->(b:B) WHERE b.id=99 RETURN a.id,p", ((1,None),)),
    ("MATCH p=(a:A)-[r:R]->(b:B) CALL (p) { RETURN length(p) AS n } RETURN n", ((1,),)),
    ("CALL () { MATCH p=(a:A)-[r:R]->(b:B) RETURN p } RETURN length(p)", ((1,),)),
    ("MATCH p=(a:A)-[r:R]->(b:B) WITH p AS journey RETURN length(journey)", ((1,),)),
])
def test_path_null_extension_functions_and_subquery_scope(graph, query, expected):
    assert graph.execute(query).rows == expected


def test_separate_matches_may_observe_the_same_edge_in_two_paths(graph):
    rows = graph.execute("MATCH p=(a:A)-[r:R]->(b:B) MATCH q=(a)-[s:R]->(c:B) RETURN p=q,p,q").rows
    assert len(rows) == 1 and rows[0][0] is True and rows[0][1] == rows[0][2]


@pytest.mark.parametrize("query,expected", [
    ("CALL () { MATCH p=(a:A)-[:R]->(b:B) RETURN p UNION RETURN null AS p } "
     "RETURN length(p) AS n ORDER BY n", ((1,), (None,))),
    ("MATCH p=(a:A)-[:R]->(b:B) CALL (p) { RETURN p AS q UNION RETURN p AS q } "
     "RETURN length(q)", ((1,),)),
    ("MATCH p=(a:A)-[:R]->(b:B) CALL (p) { RETURN length(p) AS n UNION ALL "
     "RETURN length(p) AS n } RETURN n", ((1,), (1,))),
])
def test_paths_survive_union_subquery_imports_exports_and_nulls(graph, query, expected):
    assert graph.execute(query).rows == expected


@pytest.mark.parametrize("query,ids", [
    ("MATCH p=(a)-[:R]->(b:B) RETURN p", ("alpha", "beta")),
    ("MATCH p=(b)<-[:R]-(a:A) RETURN p", ("beta", "alpha")),
    ("MATCH p=(b)-[:R]-(a:A) RETURN p", ("beta", "alpha")),
])
def test_captured_paths_infer_the_unlabelled_source_table(graph, query, ids):
    rows = graph.execute(query).rows
    assert len(rows) == 1
    assert tuple(node.properties["name"] for node in rows[0][0].nodes) == ids


def test_ordinary_undirected_hops_do_not_confuse_equal_ids_in_different_tables(graph):
    assert graph.execute("MATCH (b:B)-[r:R]-(a:A) RETURN b.name,a.name").rows == (("beta", "alpha"),)
    assert graph.execute("MATCH (b:B)-[r:R]-(a:B) RETURN b.name,a.name").rows == ()


@pytest.mark.parametrize("patterns", [
    "p=(a:A)-[r:R]->(b:B),q=(c:A)-[s:R]->(d:B)",
    "p=(a:A)-[:R]->(b:B),q=(c:A)-[:R]->(d:B)",
    "(a:A)-[:R]->(b:B)<-[:R]-(c:A)",
])
def test_same_match_cannot_reuse_a_relationship_even_when_anonymous(graph, patterns):
    assert graph.execute("MATCH " + patterns + " RETURN a.name").rows == ()
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:A),(b:B) CREATE (a)-[:R {weight:8}]->(b)")
    # Two edge choices, each followed only by the other edge: not four Cartesian rows.
    assert graph.execute("MATCH " + patterns + " RETURN a.name").rows == (("alpha",), ("alpha",))


def test_clause_trail_identity_is_qualified_by_relationship_table(graph):
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE S(FROM A TO B)")
        tx.execute("MATCH (a:A),(b:B) CREATE (a)-[:S]->(b)")
    assert graph.execute("MATCH (a:A)-[r:R]->(b:B),(c:A)-[s:S]->(d:B) "
                         "RETURN r=s").rows == ((False,),)


def test_variable_hop_segments_share_clause_trail_but_not_separate_match_scope(graph):
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE Loop(FROM A TO A)")
        tx.execute("MATCH (a:A) CREATE (a)-[:Loop]->(a)")
    assert graph.execute("MATCH (a:A)-[:Loop*1..2]->(b:A)-[:Loop]->(c:A) RETURN a.name").rows == ()
    assert graph.execute("MATCH (a:A)-[:Loop*1..2]->(b:A) "
                         "MATCH (b)-[:Loop]->(c:A) RETURN a.name").rows == (("alpha",),)
