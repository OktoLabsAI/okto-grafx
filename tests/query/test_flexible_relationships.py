"""Automatic relationship types use real endpoint identities and native atomic DDL."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxWriteConflict


def test_native_flexible_edge_creation_properties_and_reopen(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(a {v:'a'})-[r:R {weight:2}]->(b {v:'b'}) RETURN a,r,b,type(r),properties(r)")
            a, r, b, kind, props = result.rows[0]
            assert a.labels == b.labels == ()
            assert r.source == a.identity and r.target == b.identity
            assert kind == "R" and props == {"weight":2} and dict(r.properties) == props
        assert db.execute("MATCH(a)-[r:R]->(b) RETURN a.v,r.weight,b.v").rows == (("a",2,"b"),)
        with db.begin("write") as tx:
            tx.execute("MATCH()-[r:R]->() SET r.weight='text',r.extra=[1,2]")
        assert db.execute("MATCH()-[r:R]->() RETURN r.weight,r.extra[1]").rows == (("text",2),)
        assert db.verify("all").findings == ()
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH()-[r:R]->() RETURN type(r),r.weight").rows == (("R","text"),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("suffix", [
    "WITH a,b MATCH(a)-[r:R]->(b) RETURN r.weight",
    "WITH a,b MATCH p=(a)-[:R*1..2]->(b) RETURN length(p)",
    "WITH a,b MATCH(a)-[r]->(b) RETURN r.weight",
    "WITH a OPTIONAL MATCH(a)-[r:R]->(c) RETURN r.weight",
    "WITH a RETURN size([(a)-[:R]->(c) | c])",
    "WITH a WHERE (a)-[:R]->() RETURN 1",
    "WITH a CALL (a) { MATCH(a)-[r:R]->() RETURN r } RETURN r.weight",
])
def test_new_type_is_readable_later_in_same_statement(tmp_path, suffix):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE(a {v:1})-[:R {weight:1}]->(b {v:2}) " + suffix)
            assert result.rows == ((1,),)
        assert db.verify("all").findings == ()


def test_one_flexible_type_grows_endpoint_pairs_without_retyping_declared_nodes(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(a:A {id:1})-[r:R {v:1}]->(b:B {id:2})")
            tx.execute("MATCH(a:A),(b:B) CREATE(b)-[:R {v:'reverse'}]->(a)")
        assert set(db.execute("MATCH()-[r:R]->() RETURN r.v").rows) == {(1,), ("reverse",)}
        assert len(db.catalog.catalog.relationship_tables("R")) == 2
        assert db.verify("all").findings == ()


def test_late_invalid_edge_rolls_back_implicit_types_nodes_and_edges(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError, match="nonfinite"):
                tx.execute("UNWIND [1,2] AS i CREATE(a)-[:R {v:CASE WHEN i=1 THEN i ELSE 0.0/0.0 END}]->(b)")
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_entity_list_concatenation_and_subscript_preserve_native_node_authority(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (a {var:'start'}),(b {var:'end'}) WITH * "
                       "UNWIND range(1,20) AS i CREATE(n {var:i}) "
                       "WITH a,b,[a]+collect(n)+[b] AS nodes "
                       "UNWIND range(0,size(nodes)-2) AS i "
                       "WITH nodes[i] AS a,nodes[i+1] AS b CREATE(a)-[:T]->(b)")
        result = db.execute("MATCH(n {var:'start'})-[:T*]->(m {var:'end'}) RETURN m")
        assert len(result.rows) == 1
        assert result.rows[0][0].labels == ()
        assert dict(result.rows[0][0].properties) == {"var":"end"}
        assert db.execute("MATCH()-[r:T]->() RETURN count(r)").rows == ((21,),)
        assert db.verify("all").findings == ()


def test_failed_pair_expansion_retains_previous_group_and_data_after_reopen(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(a:A {id:1})-[:R {v:1}]->(b:B {id:2})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError, match="nonfinite"):
                tx.execute("MATCH(a:A),(b:B) CREATE(b)-[:R {v:0.0/0.0}]->(a)")
        assert len(db.catalog.catalog.relationship_tables("R")) == 1
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH()-[r:R]->() RETURN r.v").rows == ((1,),)
        assert len(db.catalog.catalog.relationship_tables("R")) == 1
        assert db.verify("all").findings == ()


def test_flexible_edges_preserve_snapshot_and_conflicting_writer_isolation(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        reader = db.begin("read")
        try:
            assert reader.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
            with db.begin("write") as tx:
                tx.execute("CREATE(a)-[:R {v:1}]->(b)")
            assert reader.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
        finally:
            reader.rollback()
        with connect(path) as other:
            writer = db.begin("write")
            try:
                writer.execute("MATCH()-[r:R]->() SET r.v='loser'")
                with other.begin("write") as tx:
                    tx.execute("MATCH()-[r:R]->() SET r.v={winner:true}")
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                if writer.active:
                    writer.rollback()
        assert db.execute("MATCH()-[r:R]->() RETURN r.v.winner").rows == ((True,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("expression", ["[a, 1][0]", "$nodes[0]", "[a]", "[a,r][0]"])
def test_ambiguous_or_external_lists_do_not_grant_endpoint_authority(tmp_path, expression):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(a)-[r:R]->(b) WITH b," + expression + " AS n CREATE(n)-[:T]->(b)",
                           {"nodes":[{"id":1}]})
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_flexible_relationship_merge_uses_named_keys_and_sees_owner_updates(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a {v:1}),(b {v:2})")
            for _ in range(2):
                tx.execute("MATCH(a {v:1}),(b {v:2}) MERGE(a)-[:R {v:1}]->(b)")
            tx.execute("MATCH()-[r:R]->() SET r.extra='keep'")
            tx.execute("MATCH(a {v:1}),(b {v:2}) MERGE(a)-[:R {v:1}]->(b)")
        with db.begin("write") as tx:
            tx.execute("MATCH(a {v:1}),(b {v:2}) MERGE(a)-[:R]->(b)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH(a {v:1}),(b {v:2}) MERGE(a)-[:R {v:null}]->(b)")
            assert failure.value.details["reason"] == "merge_null_property"
            assert failure.value.details["query_phase"] == "execution"
        assert db.execute("MATCH()-[r:R]->() RETURN r.v,r.extra").rows == ((1,"keep"),)
        assert db.verify("all").findings == ()
