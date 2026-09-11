"""Composed named captures retain path identity, null extension and scope."""

import pytest

from okto_grafx import PathValue
from tests.query.test_fp3_native_path_results import graph as graph


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
