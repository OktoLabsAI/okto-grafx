"""Runtime write targets retain real schema, identity, rollback and OCC proofs."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxError, GrafxWriteConflict


def seed(db):
    with db.begin("write") as tx:
        for label in ("A", "B", "C"):
            tx.execute(f"CREATE NODE TABLE {label}(id INT64, v STRING, PRIMARY KEY(id))")
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO A, FROM B TO C, FROM C TO B, w INT64)")
        tx.execute("CREATE REL TABLE GROUP BAD(FROM B TO A, FROM C TO A, w INT64)")
        tx.execute("CREATE(a:A {id:1,v:'a'})-[:R {w:10}]->(b:B {id:1,v:'b'})-[:R {w:20}]->(:C {id:1,v:'c'})")


@pytest.fixture
def graph(tmp_path):
    with okto_grafx.connect(tmp_path / "graph") as db:
        seed(db)
        yield db


def edges(db):
    return db.execute("MATCH(a)-[r]->(b) RETURN labels(a),type(r),labels(b),r.w ORDER BY r.w").rows


def test_reverse_group_edges_preserves_qualified_identity_and_old_reader(graph):
    before = edges(graph)
    with graph.begin("read") as old:
        old_rows = old.execute("MATCH(a)-[r]->(b) RETURN a,r,b").rows
        with graph.begin("write") as tx:
            result = tx.execute("MATCH(a)-[r:R]->(b) WITH a,b,r,r.w AS weight DELETE r CREATE(b)-[s:R {w:weight}]->(a) RETURN b,s,a")
            assert len(result.rows) == 2
            for source, relation, target in result.rows:
                assert relation.source == source.identity and relation.target == target.identity
                assert source.identity != target.identity
            assert old.execute("MATCH(a)-[r]->(b) RETURN a,r,b").rows == old_rows
        assert old.execute("MATCH(a)-[r]->(b) RETURN a,r,b").rows == old_rows
    assert edges(graph) == tuple((target, label, source, weight) for source, label, target, weight in before)
    assert graph.verify("all").findings == ()


def test_late_missing_pair_rolls_back_deletions_and_insertions(graph):
    before = edges(graph)
    with graph.begin("write") as tx:
        tx.execute("CREATE(:A {id:99,v:'earlier'})")
        with pytest.raises(GrafxError) as raised:
            tx.execute("MATCH(a)-[r:R]->(b) WITH a,b,r ORDER BY r.w DELETE r CREATE(b)-[:BAD]->(a)")
        assert raised.value.details["field"] == "endpoint"
        assert tx.execute("MATCH(n:A {id:99}) RETURN n.v").rows == (("earlier",),)
    assert edges(graph) == before
    assert graph.verify("all").findings == ()


def test_new_group_binding_can_be_updated_and_returned(graph):
    with graph.begin("write") as tx:
        result = tx.execute("MATCH(a)-[r:R]->(b) CREATE(b)-[s:R {w:1}]->(a) SET s.w=r.w+1 RETURN type(s),s.w ORDER BY s.w")
        assert result.rows == (("R", 11), ("R", 21))
    assert len(edges(graph)) == 4


def test_relationship_merge_resolves_each_runtime_pair(graph):
    with graph.begin("write") as tx:
        assert tx.execute("MATCH(a)-[r:R]->(b) MERGE(b)-[s:R {w:r.w}]->(a) RETURN s.w ORDER BY s.w").rows == ((10,), (20,))
        assert tx.execute("MATCH(a)-[r:R]->(b) MERGE(b)-[s:R {w:r.w}]->(a) RETURN count(s)").rows == ((4,),)
    assert len(edges(graph)) == 4


def test_polymorphic_node_update_delete_and_typed_creation(graph):
    with graph.begin("write") as tx:
        assert tx.execute("MATCH(n) SET n.v='updated' RETURN count(n)").rows == ((3,),)
        tx.execute("MATCH(n) WHERE n:A CREATE(:C {id:9,v:n.v})")
        tx.execute("MATCH(n) WHERE n.id=9 DELETE n")
        assert tx.execute("MATCH(n) RETURN n.v").rows == (("updated",),) * 3
        tx.execute("MATCH(n) DETACH DELETE n")
    assert edges(graph) == ()
    assert graph.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
    assert graph.verify("all").findings == ()


def test_actual_schema_mismatch_in_late_table_preserves_all_prior_rows(tmp_path):
    path = tmp_path / "types"
    with okto_grafx.connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, v STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, v INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(:A {id:1,v:'old'}),(:B {id:2,v:7})")
            with pytest.raises(GrafxError):
                tx.execute("MATCH(n) WITH n ORDER BY n.id SET n.v='bad'")
            assert tx.execute("MATCH(n) RETURN n.v ORDER BY n.id").rows == (("old",), (7,))
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH(n) RETURN n.v ORDER BY n.id").rows == (("old",), (7,))
        assert db.verify("all").findings == ()


def test_runtime_endpoint_fences_conflict_with_concurrent_node_delete(tmp_path):
    path = tmp_path / "fence"
    with okto_grafx.connect(path) as db:
        seed(db)
        with okto_grafx.connect(path) as other:
            writer = db.begin("write")
            try:
                writer.execute("MATCH(a)-[:R]->(b) CREATE(b)-[:R {w:99}]->(a)")
                with other.begin("write") as remover:
                    remover.execute("MATCH(n:B) DETACH DELETE n")
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                if writer.active:
                    writer.rollback()
        assert edges(db) == ()
        assert db.verify("all").findings == ()


def test_pending_endpoint_identities_survive_reopen(tmp_path):
    path = tmp_path / "pending"
    with okto_grafx.connect(path) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE(:A {id:9}),(:B {id:9}),(:C {id:9})")
            tx.execute("MATCH(a),(b) WHERE a.id=9 AND b.id=9 AND (a:A AND b:B OR a:B AND b:C) CREATE(a)-[:R {w:90}]->(b)")
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH(a)-[r:R]->(b) WHERE a.id=9 RETURN r.w").rows == ((90,), (90,))
        assert db.verify("all").findings == ()


def test_detached_and_foreign_results_cannot_supply_write_authority(graph):
    before = edges(graph)
    local = graph.execute("MATCH(n:A) RETURN n").rows[0][0]
    with okto_grafx.connect(":memory:") as other:
        seed(other)
        foreign = other.execute("MATCH(n:A) RETURN n").rows[0][0]
    with graph.begin("write") as tx:
        tx.execute("CREATE(:A {id:99,v:'earlier'})")
        for value in (local, foreign):
            for query in ("WITH $n AS a MATCH(b:B) CREATE(a)-[:R]->(b)",
                          "WITH $n AS a SET a.v='bad'", "WITH $n AS a DELETE a"):
                with pytest.raises(GrafxError):
                    tx.execute(query, {"n": value})
        assert tx.execute("MATCH(n:A {id:99}) RETURN n.v").rows == (("earlier",),)
    assert edges(graph) == before


def test_null_polymorphic_endpoint_refuses_without_effects(graph):
    before = edges(graph)
    with graph.begin("write") as tx:
        with pytest.raises(GrafxError):
            tx.execute("OPTIONAL MATCH(a) WHERE a.id=404 MATCH(b:B) CREATE(a)-[:R]->(b)")
    assert edges(graph) == before
    assert graph.verify("all").findings == ()
