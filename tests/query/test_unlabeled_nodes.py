"""Native unlabeled entities are not typed fixture labels or a sidecar graph."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError, GrafxWriteConflict
from okto_grafx.domain.query.entity_values import NodeValue
from okto_grafx.transfer import export_graph, import_graph


def test_create_unlabeled_properties_and_reopen(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE (n {var:'start'}), (m {var:2}), (z) RETURN n, m, z, labels(n), properties(n)")
            n, m, z, labels, props = result.rows[0]
            assert all(type(v) is NodeValue and v.labels == () and v.label is None for v in (n, m, z))
            assert dict(n.properties) == {"var": "start"}
            assert dict(m.properties) == {"var": 2}
            assert dict(z.properties) == {}
            assert labels == () and props == {"var": "start"}
            assert n.to_dict()["label"] is None
        assert db.execute("MATCH (n {var:'start'}) RETURN n.var").rows == (("start",),)
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH (n) RETURN count(n)").rows == ((3,),)
        assert db.execute("MATCH (n) RETURN labels(n)").rows == (((),), ((),), ((),))
        assert db.verify("all").findings == ()


def test_unlabeled_set_grows_and_removes_properties_atomically(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (n {id:1, a:1})")
            result = tx.execute("MATCH (n {id:1}) SET n.a='text', n.b=[1,'two'], n.id=null RETURN n, n.b[1]")
            assert result.rows[0][1] == "two"
            assert dict(result.rows[0][0].properties) == {"a":"text", "b":(1,"two")}
        assert db.execute("MATCH (n) WHERE n.id IS NULL RETURN n.a").rows == (("text",),)
        assert db.verify("all").findings == ()


def test_late_failure_rolls_back_native_schema_and_rows(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i CREATE (n {v:CASE WHEN i=1 THEN i ELSE 0.0/0.0 END})")
            assert tx.execute("MATCH(n) RETURN count(n)").rows == ((0,),)
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_zero_input_does_not_create_hidden_schema(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND [] AS x CREATE (n {v:x})")
        assert db.catalog.catalog.tables() == ()


def test_unlabeled_native_merge_and_logical_transfer(tmp_path):
    with connect(tmp_path / "source") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,1,2] AS i MERGE (n {v:i}) RETURN n")
            assert len(result.rows) == 3
            assert result.rows[0][0] == result.rows[1][0] != result.rows[2][0]
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((2,),)
        physical = db.catalog.catalog.tables()[0].name
        assert db.execute(f"MATCH(n:{physical}) RETURN count(n)").rows == ((0,),)
        assert db.execute(f"MATCH(n) RETURN n:{physical}").rows == ((False,), (False,))
        with db.begin("write") as tx:
            with pytest.raises(GrafxError, match="not a node label"):
                tx.execute(f"CREATE(n:{physical})")
        export_graph(db, tmp_path / "artifact")
    import_graph(tmp_path / "artifact", tmp_path / "target")
    with connect(tmp_path / "target") as db:
        assert db.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((1,), (2,))
        assert all(row[0].labels == () for row in db.execute("MATCH(n) RETURN n").rows)
        assert db.verify("all").findings == ()


def test_properties_of_several_pending_typed_nodes_never_share_none_identity(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
            result = tx.execute("CREATE(a:T {id:1}),(b:T {id:2}) RETURN properties(a), properties(b)")
            assert result.rows == (({"id":1}, {"id":2}),)


def test_schema_and_values_are_snapshot_isolated_between_participants(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        reader = db.begin("read")
        assert reader.execute("MATCH(n) RETURN count(n)").rows == ((0,),)
        with db.begin("write") as tx:
            tx.execute("CREATE(n {v:'old'})")
        assert reader.execute("MATCH(n) RETURN count(n)").rows == ((0,),)
        reader.rollback()
        observed = db.execute("MATCH(n) RETURN n").rows[0][0]
        with connect(path) as other:
            writer = db.begin("write")
            try:
                writer.execute("MATCH(n) SET n.v=2")
                with other.begin("write") as concurrent:
                    concurrent.execute("MATCH(n) SET n.v={winner:true}")
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                if writer.active:
                    writer.rollback()
        assert dict(observed.properties) == {"v":"old"}
        assert db.execute("MATCH(n) RETURN n.v.winner").rows == ((True,),)
        with db.begin("write") as tx:
            tx.execute("MATCH(n) DELETE n")
            replacement = tx.execute("CREATE(n {v:'new'}) RETURN n").rows[0][0]
        assert replacement != observed and replacement.labels == ()
        assert db.verify("all").findings == ()


def test_late_expression_failure_discards_created_schema_and_prior_pipeline_writes(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(n {v:'staged'}) RETURN 1/0")
        assert db.catalog.catalog.tables() == ()
        assert db.verify("all").findings == ()


def test_unlabeled_values_compose_without_leaking_physical_labels(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND range(1,3) AS i CREATE(n {i:i, `_properties`:i+10})")
        rows = db.execute("MATCH(n) WITH n ORDER BY n.i RETURN n, n._properties, properties(n)").rows
        assert [row[1] for row in rows] == [11,12,13]
        assert all(row[0].labels == () and set(row[2]) == {"i","_properties"} for row in rows)
        union = db.execute("MATCH(n) RETURN n UNION MATCH(n) RETURN n").rows
        assert len(union) == 3 and {r[0] for r in union} == {r[0] for r in rows}
        assert db.execute("MATCH(n) OPTIONAL MATCH(m {i:99}) RETURN labels(n), m").rows == (((),None),)*3
