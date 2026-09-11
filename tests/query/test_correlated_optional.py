"""Native optional incident joins: semantics, direction, overlays and read isolation."""
from pathlib import Path

import pytest

import okto_grafx


@pytest.fixture
def graph(tmp_path: Path):
    with okto_grafx.connect(tmp_path / "graph") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Person(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Topic(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Knows(FROM Person TO Person, weight INT64)")
            tx.execute("CREATE REL TABLE Likes(FROM Person TO Topic)")
            tx.execute("CREATE REL TABLE Attracts(FROM Topic TO Person)")
        with db.begin("write") as tx:
            for key in ("a", "b", "isolated"):
                tx.execute("CREATE (:Person {id:$id, title:$id})", {"id": key})
            tx.execute("CREATE (:Topic {id:'t'})")
            for weight in (1, 2):
                tx.execute("MATCH (a:Person {id:'a'}), (b:Person {id:'b'}) "
                           "CREATE (a)-[:Knows {weight:$weight}]->(b)", {"weight": weight})
            tx.execute("MATCH (a:Person {id:'a'}), (t:Topic {id:'t'}) CREATE (a)-[:Likes]->(t)")
            tx.execute("MATCH (b:Person {id:'b'}), (t:Topic {id:'t'}) CREATE (t)-[:Attracts]->(b)")
        yield db


@pytest.mark.parametrize("hop, expected", [
    ("-[edge]-", {"a": 3, "b": 3, "isolated": 0}),
    ("-[edge]->", {"a": 3, "b": 0, "isolated": 0}),
    ("<-[edge]-", {"a": 0, "b": 3, "isolated": 0}),
    ("-[edge:Knows]-", {"a": 2, "b": 2, "isolated": 0}),
])
def test_degree_is_generic_and_preserves_parallel_edges(graph, hop, expected):
    result = graph.execute(f"MATCH (person:Person) OPTIONAL MATCH (person){hop}() "
                           "RETURN person.id, count(edge) ORDER BY person.id")
    assert dict(result.rows) == expected
    assert result.statistics.get("edge_scans", 0) <= 3


def test_optional_filter_null_extends_after_matching(graph):
    result = graph.execute("MATCH (a:Person) OPTIONAL MATCH (a)-[r:Knows]->(b:Person) "
                           "WHERE r.weight > 1 RETURN a.id, count(r), count(*), count(b) ORDER BY a.id")
    assert result.rows == (("a", 1, 1, 1), ("b", 0, 1, 0), ("isolated", 0, 1, 0))
    assert graph.execute("MATCH (a:Person) WHERE a.id = 'missing' "
                         "OPTIONAL MATCH (a)-[r]-() RETURN a.id, count(r)").rows == ()


def test_target_label_and_returned_nulls(graph):
    assert graph.execute("MATCH (a:Person) OPTIONAL MATCH (a)-[r]->(b:Topic) "
                         "RETURN a.id, b.id ORDER BY a.id").rows == (
                             ("a", "t"), ("b", None), ("isolated", None))


def test_owner_rollback_and_independent_read_snapshot(graph):
    query = "MATCH (a:Person) OPTIONAL MATCH (a)-[r]-() RETURN a.id, count(r) ORDER BY a.id"
    before = graph.execute(query).rows
    with graph.begin("read") as reader:
        assert reader.execute(query).rows == before
        with graph.begin("write") as writer:
            writer.execute("MATCH (a:Person {id:'isolated'}), (b:Person {id:'b'}) "
                           "CREATE (a)-[:Knows {weight:3}]->(b)")
            assert dict(writer.execute(query).rows)["isolated"] == 1
        assert reader.execute(query).rows == before
    assert dict(graph.execute(query).rows)["isolated"] == 1
    writer = graph.begin("write")
    writer.execute("MATCH (a:Person)-[r:Knows]->(b:Person) DELETE r")
    assert dict(writer.execute(query).rows)["isolated"] == 0
    writer.rollback()
    assert dict(graph.execute(query).rows)["isolated"] == 1


def test_optional_projection_order_and_window(graph):
    result = graph.execute("MATCH (x:Person) OPTIONAL MATCH (x)-[link]-() "
                           "RETURN x.id AS name, count(link) AS degree "
                           "ORDER BY degree DESC, name SKIP 1 LIMIT 1")
    assert result.rows == (("b", 3),)


def test_polymorphic_target_and_relationship_properties(graph):
    result = graph.execute(
        "MATCH (p:Person {id:'a'}) OPTIONAL MATCH (p)-[r]->(x) "
        "RETURN x.id, x.title, coalesce(r.weight,0) ORDER BY x.id, r.weight"
    )
    assert result.rows == (("b", "b", 1), ("b", "b", 2), ("t", None, 0))
    result = graph.execute(
        "MATCH (p:Person) OPTIONAL MATCH (p)-[r]->(x) "
        "WHERE x.title IS NULL AND r.weight IS NULL "
        "RETURN p.id, x.id, count(r) ORDER BY p.id"
    )
    assert result.rows == (("a", "t", 1), ("b", None, 0), ("isolated", None, 0))


def test_incompatible_untyped_relationship_properties_refused_before_streaming(graph):
    from okto_grafx.errors import GrafxPlanError

    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE Conflict(FROM Person TO Topic, weight STRING)")
    with pytest.raises(GrafxPlanError, match="tables do not agree"):
        graph.execute("MATCH (p:Person) OPTIONAL MATCH (p)-[r]->() RETURN r.weight")


def test_anchor_without_relationship_tables_and_empty_root(tmp_path):
    with okto_grafx.connect(tmp_path / "empty") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id STRING, PRIMARY KEY(id))")
        query = "MATCH (p:P) OPTIONAL MATCH (p)-[r]-() RETURN p.id, count(r)"
        assert db.execute(query).rows == ()
        assert db.execute("MATCH (p:P) OPTIONAL MATCH (p)-[r]-() RETURN count(r),count(*)").rows == ((0, 0),)
        with db.begin("write") as tx:
            tx.execute("CREATE (:P {id:'alone'})")
        assert db.execute(query).rows == (("alone", 0),)


def test_self_loop_is_one_physical_match_not_two_directions(graph):
    # FP-3 trail identity counts a physical self-loop once, as in typed traversal.
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:Person {id:'isolated'}) CREATE (a)-[:Knows {weight:5}]->(a)")
    result = graph.execute("MATCH (a:Person {id:'isolated'}) OPTIONAL MATCH (a)-[r]-() "
                           "RETURN count(r), count(DISTINCT r)")
    assert result.rows == ((1, 1),)


def test_optional_expansion_keeps_query_budgets(tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded

    with okto_grafx.connect(tmp_path / "budget", max_traversal_expansions=1) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM P TO P)")
        with db.begin("write") as tx:
            tx.execute("CREATE (:P {id:'a'})")
            tx.execute("MATCH (a:P) CREATE (a)-[:R]->(a)")
            # Two distinct physical edges, not a duplicate orientation of one loop.
            tx.execute("MATCH (a:P) CREATE (a)-[:R]->(a)")
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("MATCH (p:P) OPTIONAL MATCH (p)-[r]-() RETURN count(r)")
