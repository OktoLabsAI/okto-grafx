"""CREATE references an earlier node declaration, including in the same path."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "graph"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO A,id INT64)")
            tx.execute("CREATE REL TABLE AB(FROM A TO B)")
            tx.execute("CREATE REL TABLE BA(FROM B TO A)")
        yield db, path


@pytest.mark.parametrize("pattern", [
    "(n:A {id:1})-[r:R {id:7}]->(n)",
    "(n:A {id:1})<-[r:R {id:7}]-(n)",
    "(n:A {id:1}), (n)-[r:R {id:7}]->(n)",
])
def test_single_node_single_edge_identity_is_durable(graph, pattern):
    db, path = graph
    with db.begin("write") as tx:
        node, edge = tx.execute("CREATE " + pattern + " RETURN n,r").rows[0]
        assert node.properties["id"] == 1 and edge.properties["id"] == 7
        assert edge.source == node.identity == edge.target
        assert tx.execute("MATCH(n:A) RETURN count(n)").rows == ((1,),)
        assert db.execute("MATCH(n:A) RETURN count(n)").rows == ((0,),)
    with connect(path) as reader:
        assert reader.execute("MATCH(n:A) RETURN count(n)").rows == ((1,),)
        rows = reader.execute("MATCH p1=(:A)-[:R]->() MATCH p2=(:A)<-[:R]-() RETURN p1=p2").rows
        assert rows == ((True,),)
        assert reader.execute("MATCH p=(:A)-[:R]-() RETURN length(p)").rows == ((1,),)
        assert not reader.verify("all").findings


def test_nonadjacent_reference_creates_a_cycle_without_recreating_the_node(graph):
    db, _ = graph
    with db.begin("write") as tx:
        tx.execute("CREATE (a:A {id:1})-[:AB]->(b:B {id:2})-[:BA]->(a)")
        assert tx.execute("MATCH(a:A) RETURN count(a)").rows == ((1,),)
        assert tx.execute("MATCH(b:B) RETURN count(b)").rows == ((1,),)
        assert tx.execute("MATCH p=(:A)-[:AB]->(:B)-[:BA]->(:A) RETURN length(p)").rows == ((2,),)


@pytest.mark.parametrize("query", [
    "CREATE (n)-[:R]->(n:A {id:1})",  # No schema-free forward declaration.
    "CREATE (n:A {id:1})-[:R]->(n:B)",
    "CREATE (n:A {id:1})-[:R]->(n {id:2})",
    "CREATE (n:A {id:1})-[n:R]->(n)",
    "CREATE (n:A {id:1})-[:R]->(m)",
])
def test_invalid_references_refuse_without_losing_prior_statement(graph, query):
    db, path = graph
    with db.begin("write") as tx:
        tx.execute("CREATE (:A {id:99})")
        with pytest.raises(GrafxError):
            tx.execute(query)
        assert tx.execute("MATCH(n:A) RETURN n.id").rows == ((99,),)
        assert tx.execute("MATCH(:A)-[r:R]->(:A) RETURN count(r)").rows == ((0,),)
    with connect(path) as reader:
        assert reader.execute("MATCH(n:A) RETURN n.id").rows == ((99,),)
        assert not reader.verify("all").findings


def test_late_edge_failure_rolls_back_created_node_and_self_loop(graph):
    db, path = graph
    with db.begin("write") as tx:
        tx.execute("CREATE (:A {id:99})")
        with pytest.raises(GrafxError):
            tx.execute("UNWIND [1,2] AS i CREATE (n:A {id:i})-[:R {id:1/(2-i)}]->(n)")
        assert tx.execute("MATCH(n:A) RETURN n.id").rows == ((99,),)
        assert tx.execute("MATCH(:A)-[r:R]->(:A) RETURN count(r)").rows == ((0,),)
    with connect(path) as reader:
        assert reader.execute("MATCH(n:A) RETURN n.id").rows == ((99,),)
        assert not reader.verify("all").findings
