"""Re-matching edges preserves incoming qualified identity and path multiplicity."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM N TO N, id INT64)")
            tx.execute("CREATE REL TABLE Other(FROM N TO N, id INT64)")
            tx.execute("CREATE(a:N {id:0}), (b:N {id:1}), (c:N {id:2}), (d:N {id:3}) "
                       "CREATE(a)-[:E {id:10}]->(b), (b)-[:E {id:11}]->(c), (c)-[:E {id:12}]->(d)")
        yield db


@pytest.mark.parametrize("second", ["MATCH(a)-[r:E]->(b)", "MATCH(a)-[r]->(b)",
                                    "MATCH(a)<-[r]-(b)", "MATCH(a)-[r]-(b)"])
def test_bound_edge_is_not_replaced_by_another_scan_candidate(graph, second):
    rows = graph.execute("MATCH()-[r:E]->() " + second + " RETURN r.id,a.id,b.id ORDER BY r.id,a.id").rows
    forward = tuple((10 + n, n, n + 1) for n in range(3))
    reverse = tuple((10 + n, n + 1, n) for n in range(3))
    expected = reverse if "<-[r]" in second else (
        tuple(sorted((*forward, *reverse))) if "-[r]-(" in second else forward)
    assert rows == expected


def test_bound_edge_variable_length_path_count_and_identity(graph):
    query = "MATCH()-[r:E]-() MATCH p=(n)-[*0..1]-()-[r]-()-[*0..1]-(m) "
    assert graph.execute(query + "RETURN count(p)").rows == ((32,),)
    rows = graph.execute(query + "RETURN r,p").rows
    assert len(rows) == 32
    for edge, path in rows:
        assert sum(candidate.identity == edge.identity for candidate in path.relationships) == 1
        assert len({candidate.identity for candidate in path.relationships}) == len(path.relationships)


def test_other_table_parallel_edges_and_optional_retain_identity(graph):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:N {id:0}), (b:N {id:1}) "
                   "CREATE(a)-[:Other {id:10}]->(b), (a)-[:E {id:99}]->(b)")
    assert graph.execute("MATCH()-[r:E]->() MATCH()-[r:Other]->() RETURN r").rows == ()
    rows = graph.execute("MATCH()-[r:E]->() OPTIONAL MATCH(a)-[r:Other]->(b) "
                         "RETURN r.id,a,b ORDER BY r.id").rows
    assert rows == ((10, None, None), (11, None, None), (12, None, None), (99, None, None))
    assert graph.execute("MATCH()-[r:E]->() MATCH()-[r:E]->() RETURN count(r)").rows == ((4,),)
    assert graph.execute("MATCH()-[r:Other]->() MATCH()-[r]->() RETURN type(r),r.id").rows == (("Other", 10),)


def test_alias_cursor_same_clause_trail_and_null_anchor(graph):
    query = "MATCH()-[r:E]->() WITH r AS carried MATCH(a)-[carried]->(b) RETURN carried.id ORDER BY carried.id"
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ((10,), (11,), (12,))
    assert graph.execute("MATCH()-[r:E]->(), ()-[r:E]->() RETURN r").rows == ()
    assert graph.execute("OPTIONAL MATCH()-[r:Missing]->() MATCH()-[r]->() RETURN r").rows == ()


def test_optional_fast_path_cannot_overwrite_prior_optional_edge(graph):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:N {id:0}), (b:N {id:1}) CREATE(a)-[:E {id:99}]->(b)")
    rows = graph.execute("MATCH(a:N) OPTIONAL MATCH(a)-[r:E]->(b) "
                         "OPTIONAL MATCH(a)-[r:E]->(c) "
                         "RETURN a.id,r.id,b.id,c.id ORDER BY a.id,r.id").rows
    assert rows == ((0,10,1,1), (0,99,1,1), (1,11,2,2), (2,12,3,3), (3,None,None,None))


@pytest.mark.parametrize("query", [
    "MATCH(a:N)-[r:E]->() WHERE (a)-[r]->()-[r]->() RETURN a",
    "MATCH(a:N) RETURN [(a)-[r]->()-[r]->() | r]",
])
def test_repeated_relationship_expression_patterns_also_refuse(graph, query):
    with pytest.raises(GrafxPlanError) as error:
        graph.execute(query)
    assert error.value.details["reason"] == "relationship_uniqueness_violation"


@pytest.mark.parametrize("pattern", ["(a)-[r]->()-[r]->(a)", "()-[r*1..2]->()-[r*1..2]->()"])
def test_repeated_relationship_in_one_pattern_refuses_statically(graph, pattern):
    with pytest.raises(GrafxPlanError) as error:
        graph.execute("MATCH " + pattern + " RETURN r")
    assert error.value.details["reason"] == "relationship_uniqueness_violation"
    assert error.value.details["query_phase"] == "planning"
    assert graph.execute("MATCH()-[r:E]->() RETURN count(r)").rows == ((3,),)


def test_owner_overlay_and_rollback_preserve_bound_relationship(graph, tmp_path):
    with graph.begin("write") as tx:
        tx.execute("MATCH(a:N {id:0}), (b:N {id:3}) CREATE(a)-[:E {id:90}]->(b)")
        assert tx.execute("MATCH()-[r:E]->() MATCH(a)-[r]->(b) WHERE r.id=90 "
                          "RETURN a.id,b.id").rows == ((0, 3),)
        with pytest.raises(GrafxPlanError):
            tx.execute("MATCH()-[r:E]->()-[r]->() DELETE r")
        assert tx.execute("MATCH()-[r:E]->() RETURN count(r)").rows == ((4,),)
    with connect(tmp_path / "graph") as reader:
        assert reader.execute("MATCH()-[r:E]->() MATCH()-[r]->() RETURN count(r)").rows == ((4,),)
        assert reader.verify("all").findings == ()
