"""Endpoint functions return transaction-qualified nodes, not detached maps."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxWriteConflict


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("memory", [None, 8192])
def test_endpoint_composition_owner_updates_and_snapshot(tmp_path, codec, memory, monkeypatch):
    from okto_grafx.engine import query_engine
    restored = []
    decode = query_engine._decode_sort_row
    def observe(payload, codec):
        row = decode(payload, codec)
        restored.append(row)
        return row
    monkeypatch.setattr(query_engine, "_decode_sort_row", observe)
    path = tmp_path / "db"
    with connect(path, codec=codec, query_memory_budget_bytes=memory) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:R {id:1,v:10})-[r:R]->(b:R {id:2,v:20})")
        with connect(path, codec=codec) as reader, reader.begin("read") as old:
            query = "MATCH()-[r:R]->() RETURN startNode(r).v,endNode(r).v"
            assert old.execute(query).rows == ((10, 20),)
            with db.begin("write") as tx:
                assert tx.execute("MATCH()-[r:R]->() "
                                  "WITH [endNode(r),startNode(r)] AS endpoints "
                                  "UNWIND endpoints AS n WITH n ORDER BY n.id "
                                  "SET n.v=n.v+1 RETURN n.id,n.v").rows == ((1, 11), (2, 21))
                assert tx.execute(query).rows == ((11, 21),)
                assert old.execute(query).rows == ((10, 20),)
            assert old.execute(query).rows == ((10, 20),)
        assert db.execute(query).rows == ((11, 21),)
        assert db.verify("all").findings == ()
        if memory is not None:
            assert restored, "Exercise the actual spill decoder, not just a configured limit"


def test_endpoint_typed_nodes_with_different_property_types(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(v INT64)")
            tx.execute("CREATE NODE TABLE B(v STRING)")
            tx.execute("CREATE REL TABLE R(FROM A TO B)")
            tx.execute("CREATE(a:A {v:1})-[:R]->(b:B {v:'text'})")
        assert db.execute("MATCH()-[r:R]->() WITH startNode(r) AS a,endNode(r) AS b "
                          "RETURN a.v+1,upper(b.v)").rows == ((2, "TEXT"),)


@pytest.mark.parametrize("function", ["startNode", "endNode"])
def test_null_type_and_deleted_content_contract(tmp_path, function):
    with connect(tmp_path / "db") as db:
        assert db.execute(f"RETURN {function}(null)").rows == ((None,),)
        for arguments in ("", "null,null", "*", "DISTINCT null", "x => null"):
            with pytest.raises(GrafxError):
                db.execute(f"RETURN {function}({arguments})")
        for argument in ("1", "{}", "[]"):
            with pytest.raises(GrafxError) as failure:
                db.execute(f"RETURN {function}({argument})")
            assert failure.value.details["reason"] == "entity_function_argument_type"
            assert failure.value.details["query_phase"] == "planning"
        with db.begin("write") as tx:
            tx.execute("CREATE(a)-[:R]->(b)")
            with pytest.raises(GrafxError) as failure:
                tx.execute(f"MATCH()-[r:R]->() DELETE r RETURN {function}(r)")
            assert failure.value.details["query_phase"] == "execution"
            assert tx.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((1,),)
            with pytest.raises(GrafxError):
                tx.execute(f"MATCH(n) RETURN {function}(n)")
        with pytest.raises(GrafxError) as failure:
            db.execute(f"RETURN {function}($x)", {"x": 1})
        assert failure.value.details["query_phase"] == "execution"


def test_runtime_failure_after_endpoint_update_rolls_back_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a {v:1})-[:R]->(b {v:2})")
            with pytest.raises(GrafxError):
                tx.execute("MATCH()-[r:R]->() WITH startNode(r) AS n,r "
                           "SET n.v=9 UNWIND [r,1] AS item RETURN endNode(item)")
            assert tx.execute("MATCH(n) RETURN n.v ORDER BY n.v").rows == ((1,), (2,))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("memory", [None, 8192])
def test_endpoint_write_conflicts_with_direct_write(tmp_path, memory):
    path = tmp_path / "db"
    with connect(path, query_memory_budget_bytes=memory) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:N {id:1,v:10})-[:R]->(b:N {id:2,v:20})")
        with connect(path) as other:
            first, second = db.begin("write"), other.begin("write")
            try:
                first.execute("MATCH()-[r:R]->() WITH startNode(r) AS n ORDER BY n.id SET n.v=11")
                second.execute("MATCH(n:N {id:1}) SET n.v=12")
                first.commit()
                with pytest.raises(GrafxWriteConflict):
                    second.commit()
            finally:
                first.rollback()
                second.rollback()
        assert db.execute("MATCH(n:N {id:1}) RETURN n.v").rows == ((11,),)
        assert db.verify("all").findings == ()
