"""Heterogeneous properties retain their concrete types across transactional boundaries."""

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError, GrafxWriteConflict
from okto_grafx.domain.model.schema import SchemaType
from okto_grafx.transfer import export_graph, import_graph


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_authorized_set1_list_of_maps_persists_without_rewriting_its_query(tmp_path: Path, codec: str) -> None:
    path = tmp_path / "nested"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (a)\nSET a.maplist = [{num: 1}]")
        assert db.execute("MATCH (a) RETURN a.maplist").rows == ((({"num": 1},),),)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("MATCH (a) SET a.maplist = [{num: 0.0 / 0.0}]")
            assert tx.execute("MATCH (a) RETURN a.maplist[0].num").rows == ((1,),)
        db.checkpoint()
    with connect(path, codec="pure") as db:
        assert db.execute("MATCH (a) RETURN a.maplist").rows == ((({"num": 1},),),)
        assert not db.verify("all").findings


def test_native_any_values_update_rollback_and_reopen(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:Flex {id:1,val:'start'}), (:Flex {id:2,val:3})")
        assert db.catalog.catalog.table("Flex").columns[1].type is SchemaType.ANY
        old = db.begin("read")
        assert old.execute("MATCH (n:Flex) RETURN n.val ORDER BY n.id").rows == (("start",), (3,))
        with db.begin("write") as tx:
            tx.execute("MATCH (n:Flex {id:1}) SET n.val = 4.5")
            tx.execute("MATCH (n:Flex {id:2}) SET n.val = {nested: [true, 2, 'text']}")
        assert old.execute("MATCH (n:Flex) RETURN n.val ORDER BY n.id").rows == (("start",), (3,))
        old.rollback()
        tx = db.begin("write")
        tx.execute("MATCH (n:Flex) SET n.val = null")
        tx.rollback()
        assert db.execute("MATCH (n:Flex {id:1}) RETURN n.val + 1").rows == ((5.5,),)
        assert db.execute("MATCH (n:Flex {id:2}) RETURN n.val.nested[2]").rows == (("text",),)
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (n:Flex {id:1}) RETURN n.val").rows == ((4.5,),)
        assert db.execute("MATCH (n:Flex {id:2}) RETURN n.val.nested[0]").rows == ((True,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("expression", ["0.0 / 0.0", "[0.0 / 0.0]", "{a:[0.0 / 0.0]}"])
def test_nonfinite_any_statement_rollback_keeps_prior_statement(tmp_path: Path, expression: str) -> None:
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:Flex {id:1,val:'safe'})")
            with pytest.raises(GrafxError):
                tx.execute(f"MATCH (n:Flex) SET n.val = {expression}")
            assert tx.execute("MATCH (n:Flex) RETURN n.val").rows == (("safe",),)
        assert db.verify("all").findings == ()


def test_any_ddl_rollback_does_not_publish_schema_or_capability(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        before = db.catalog.catalog.tables()
        tx = db.begin("write")
        tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
        tx.execute("CREATE (:Flex {id:1,val:'uncommitted'})")
        tx.rollback()
        assert db.catalog.catalog.tables() == before
    with connect(tmp_path / "db") as db:
        assert not db.catalog.catalog.has_table("Flex")
        assert db.verify("all").findings == ()


def test_any_relationship_and_logical_transfer_preserve_native_values(tmp_path: Path) -> None:
    with connect(tmp_path / "source") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM Flex TO Flex, val ANY)")
            tx.execute("CREATE (a:Flex {id:1,val:'a'}), (b:Flex {id:2,val:2}), (a)-[:R {val:{n:[1,'b']}}]->(b)")
        assert db.execute("MATCH ()-[r:R]->() RETURN r.val.n[1]").rows == (("b",),)
        export_graph(db, tmp_path / "artifact")
    import_graph(tmp_path / "artifact", tmp_path / "target")
    with connect(tmp_path / "target") as db:
        assert db.catalog.catalog.table("Flex").columns[1].type is SchemaType.ANY
        assert db.execute("MATCH (n:Flex) RETURN n.val ORDER BY n.id").rows == (("a",), (2,))
        assert db.execute("MATCH ()-[r:R]->() RETURN r.val.n[1]").rows == (("b",),)
        assert db.verify("all").findings == ()


def test_any_create_late_invalid_row_rolls_back_whole_statement(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:Flex {id:0,val:'prior'})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i CREATE (:Flex {id:i,val:CASE WHEN i=1 THEN 'ok' ELSE 0.0/0.0 END})")
            assert tx.execute("MATCH (n:Flex) RETURN n.id, n.val").rows == ((0, "prior"),)
        assert db.verify("all").findings == ()


def test_any_property_index_refuses_without_schema_or_data_loss(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:Flex {id:0,val:'prior'})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError, match="ANY properties"):
                tx.execute("CREATE INDEX flex_val FOR (n:Flex) ON (n.val)")
            assert tx.execute("MATCH (n:Flex) RETURN n.val").rows == (("prior",),)
        assert db.verify("all").findings == ()


def test_independent_participants_still_conflict_on_same_any_property(tmp_path: Path) -> None:
    path = tmp_path / "db"
    with connect(path) as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Flex(id INT64, val ANY, PRIMARY KEY(id))")
            tx.execute("CREATE (:Flex {id:1,val:'original'})")
        with connect(path) as other:
            writer = db.begin("write")
            try:
                writer.execute("MATCH (n:Flex {id:1}) SET n.val = 7")
                with other.begin("write") as concurrent:
                    concurrent.execute("MATCH (n:Flex {id:1}) SET n.val = {winner:true}")
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                if writer.active:
                    writer.rollback()
        assert db.execute("MATCH (n:Flex) RETURN n.val.winner").rows == ((True,),)
        assert db.verify("all").findings == ()
