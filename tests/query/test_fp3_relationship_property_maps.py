"""Relationship maps are native predicates, including every edge of a range."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "maps") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, k INT64, name STRING)")
            tx.execute("CREATE REL TABLE S(FROM N TO N, k STRING)")
            tx.execute("CREATE(a:N {id:1}), (b:N {id:2}), (c:N {id:3}), (d:N {id:4}) "
                       "CREATE(a)-[:R {k:1,name:'x'}]->(b), (b)-[:R {k:1,name:'y'}]->(c), "
                       "(c)-[:R {k:2,name:'z'}]->(d), (a)-[:S {k:'one'}]->(b)")
        yield db


@pytest.mark.parametrize("edge", ["[r:R {k:1}]", "[r {k:$k}]", "[:R|S {k:1}]", "[{k:1}]"])
def test_single_hop_typed_polymorphic_anonymous_and_parameters(graph, edge):
    query = f"MATCH(a)-{edge}->(b) RETURN a.id,b.id ORDER BY a.id"
    assert graph.execute(query, {"k":1}).rows == ((1,2),(2,3))
    with graph.query(query, {"k":1}).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ((1,2),(2,3))


@pytest.mark.parametrize("condition,count", [("{k:1,name:'x'}",1), ("{k:null}",0),
                                             ("{missing:1}",0), ("{}",3)])
def test_conjunction_null_missing_and_empty_maps(graph, condition, count):
    assert graph.execute(f"MATCH()-[:R {condition}]->() RETURN count(*)").rows == ((count,),)


@pytest.mark.parametrize("range_,expected", [("0..3", (0,1,2)), ("1..3",(1,2)),
                                            ("1..1",(1,)), ("0..0",(0,))])
def test_range_maps_require_every_edge_not_only_the_last(graph, range_, expected):
    query = f"MATCH p=(a:N {{id:1}})-[rs:R*{range_} {{k:1}}]->(b) RETURN length(p),rs ORDER BY length(p)"
    rows = graph.execute(query).rows
    assert tuple(row[0] for row in rows) == expected
    assert all(edge.properties["k"] == 1 for _, edges in rows for edge in edges)


def test_optional_predicates_run_before_null_extension_and_bound_identity(graph):
    rows = graph.execute("MATCH(a:N) OPTIONAL MATCH(a)-[r:R {k:1}]->(b) "
                         "RETURN a.id,r.k,b.id ORDER BY a.id").rows
    assert rows == ((1,1,2),(2,1,3),(3,None,None),(4,None,None))
    rows = graph.execute("MATCH()-[r:R]->() OPTIONAL MATCH(a)-[r {name:'x'}]->(b) "
                         "RETURN r.name,a.id,b.id ORDER BY r.name").rows
    assert rows == (('x',1,2),('y',None,None),('z',None,None))


def test_path_reverse_comprehension_and_predicate_composition(graph):
    assert graph.execute("MATCH p=(a)<-[r:R {name:'x'}]-(b) RETURN a.id,b.id,length(p)").rows == ((2,1,1),)
    assert graph.execute("MATCH(n:N) WHERE (n)-[:R {k:1}]->() RETURN n.id ORDER BY n.id").rows == ((1,),(2,))
    rows = graph.execute("MATCH(n:N) RETURN n.id, [(n)-[:R {k:1}]->(m) | m.id] ORDER BY n.id").rows
    assert rows == ((1,(2,)),(2,(3,)),(3,()),(4,()))
    assert graph.execute("MATCH(n:N {id:1}) RETURN [p=(n)-[:R*0..3 {k:1}]->() | length(p)]").rows == (((0,1,2),),)


def test_row_dependent_values_and_multiple_segments(graph):
    assert graph.execute("UNWIND [1,2] AS wanted MATCH(a)-[:R {k:wanted}]->(b) "
                         "RETURN wanted,a.id ORDER BY wanted,a.id").rows == ((1,1),(1,2),(2,3))
    assert graph.execute("MATCH p=()-[:R {name:'x'}]->()-[:R {name:'y'}]->() RETURN length(p)").rows == ((2,),)


def test_late_type_failure_rolls_back_prior_matched_writes(graph, tmp_path):
    with graph.begin("write") as tx:
        tx.execute("MATCH()-[r:R {name:'z'}]->() SET r.k=9")
        with pytest.raises(GrafxError):
            tx.execute("UNWIND [1,'bad'] AS wanted MATCH()-[r:R {k:abs(wanted)}]->() SET r.k=7")
        assert tx.execute("MATCH()-[r:R]->() RETURN r.name,r.k ORDER BY r.name").rows == (('x',1),('y',1),('z',9))
    with connect(tmp_path / "maps") as reader:
        assert reader.execute("MATCH()-[r:R]->() RETURN r.k ORDER BY r.k").rows == ((1,),(1,),(9,))
        assert reader.verify("all").findings == ()
