"""Node labels and relationship types have separate persistent identities."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("node_first", [False, True])
@pytest.mark.parametrize("typed", [False, True])
def test_same_name_native_queries_and_reopen(tmp_path, node_first, typed):
    path = tmp_path / "db"
    with connect(path) as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            if typed:
                tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
                node = "CREATE NODE TABLE R(v INT64)"
                rel = "CREATE REL TABLE R(FROM A TO A, v INT64)"
                for ddl in ((node, rel) if node_first else (rel, node)):
                    tx.execute(ddl)
                tx.execute("CREATE(a:A {id:1})-[r:R {v:2}]->(a),(:R {v:3})")
            else:
                statements = ("CREATE(:R {v:3})", "CREATE(a:A {id:1})-[:R {v:2}]->(a)")
                for statement in (statements if node_first else statements[::-1]):
                    tx.execute(statement)
            assert tx.execute("MATCH(n:R) RETURN n.v").rows == ((3,),)
            assert tx.execute("MATCH(a)-[r:R]->(b) RETURN r.v").rows == ((2,),)
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(n:R) RETURN n.v").rows == ((3,),)
        assert db.execute("MATCH(a)-[r:R]->(b) RETURN r.v").rows == ((2,),)
        assert db.execute("MATCH(a:A) OPTIONAL MATCH(a)-[r:R]->(b) RETURN r.v").rows == ((2,),)
        assert db.execute("MATCH p=(a:A)-[:R*1..2]->(b) RETURN length(p)").rows == ((1,),)
        assert db.verify("all").findings == ()


def test_original_graph5_type_label_fixture(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE ()-[:T1]->(), ()-[:T2]->(), ()-[:t2]->(), (:T2)-[:T3]->(), ()-[:T4]->(:T2)")
        rows = db.execute("MATCH ()-[r]->() RETURN r, r:T2 AS result").rows
        assert sorted((r.label, result) for r, result in rows) == [
            ("T1", False), ("T2", True), ("T3", False), ("T4", False), ("t2", False)]
        assert db.verify("all").findings == ()


def test_late_failure_rolls_back_overlapping_schema_and_data(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:A)-[:R]->(a)")
        before = db.catalog.catalog.tables()
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:R {v:3}) CREATE(:Broken {bad:0.0/0.0})")
            assert tx.execute("MATCH(n:R) RETURN count(n)").rows == ((0,),)
            assert tx.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((1,),)
        assert db.catalog.catalog.tables() == before
        assert db.verify("all").findings == ()


def test_independent_readers_and_writers_keep_kind_qualified_identity(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64, v INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R, v INT64)")
            tx.execute("CREATE(a:R {id:1,v:10})-[:R {v:20}]->(a)")
        with connect(path) as other, db.begin("read") as old:
            with db.begin("write") as nodes, other.begin("write") as edges:
                nodes.execute("MATCH(n:R) SET n.v=11")
                edges.execute("MATCH()-[r:R]->() SET r.v=21")
            assert old.execute("MATCH(n:R)-[r:R]->() RETURN n.v,r.v").rows == ((10,20),)
            assert other.execute("MATCH(n:R)-[r:R]->() RETURN n.v,r.v").rows == ((11,21),)
        assert db.verify("all").findings == ()


def test_scan_kind_qualification_and_cursor_cannot_switch_sibling(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R, v INT64)")
            tx.execute("UNWIND [1,2] AS i CREATE(a:R {id:i})-[:R {v:i+10}]->(a)")
        with db.begin("read") as tx:
            with pytest.raises(GrafxError) as error:
                tx.scan_rows_v1("R", limit=1)
            assert error.value.details["reason"] == "ambiguous_table_name"
            first = tx.scan_rows_v1("R", kind="node", limit=1)
            assert first.next_cursor is not None
            with pytest.raises(GrafxError):
                tx.scan_rows_v1("R", kind="rel", limit=1, cursor=first.next_cursor)
            second = tx.scan_rows_v1("R", kind="node", limit=1, cursor=first.next_cursor)
            assert {first.rows[0].values, second.rows[0].values} == {(1,), (2,)}
            assert {row.values[2] for row in tx.scan_rows_v1("R", kind="rel", limit=10).rows} == {11,12}
            for kind in (False, [], "relationship"):
                with pytest.raises(GrafxError):
                    tx.scan_rows_v1("R", kind=kind, limit=1)


@pytest.mark.parametrize("node_first", [False, True])
def test_legacy_namespace_admission_requires_explicit_upgrade_before_effects(tmp_path, node_first):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            first = "CREATE NODE TABLE R(v INT64)" if node_first else "CREATE REL TABLE R(FROM A TO A, v INT64)"
            second = "CREATE REL TABLE R(FROM A TO A, v INT64)" if node_first else "CREATE NODE TABLE R(v INT64)"
            tx.execute(first)
        before = db.catalog.catalog.tables()
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as error:
                tx.execute(second)
            assert error.value.details["remedy"] == "maintenance.ensure_identity_indexes"
            tx.execute("CREATE(:A {id:1})")
        assert db.catalog.catalog.tables() == before
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute(second)
        assert db.catalog.catalog.has_table("R", kind="node")
        assert db.catalog.catalog.has_table("R", kind="rel")
        assert db.execute("MATCH(n:A) RETURN n.id").rows == ((1,),)
        assert db.verify("all").findings == ()
