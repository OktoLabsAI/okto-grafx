"""Deletion consumes native identity from expressions, never arbitrary user data."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxWriteConflict


def write(db, query, parameters=None):
    with db.begin("write") as tx:
        return tx.execute(query, parameters)


@pytest.mark.parametrize("selector", ["{key:n}.key", "[n][0]", "CASE WHEN n.id=1 THEN n ELSE null END", "coalesce(null,n)"])
def test_native_node_selection(tmp_path, selector):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {id:1}),(:N {id:2})")
        write(db,f"MATCH(n:N) WHERE n.id=1 DELETE {selector}")
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((2,),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("detach", [False, True])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_path_delete_repeated_nodes_edges_and_reopen(tmp_path, detach, codec):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        write(db,"CREATE(a:N {id:1})-[r:R]->(b:N {id:2})-[:R]->(a)")
        keyword = "DETACH DELETE" if detach else "DELETE"
        result = write(db,f"MATCH p=(a:N {{id:1}})-[:R*2..2]->(a) {keyword} p,p RETURN 1")
        assert result.rows == ((1,),)
        assert result.statistics["rows_deleted"] == 4
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
        assert db.verify("all").findings == ()
    with connect(path,codec=codec) as db:
        assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((0,),)
        assert db.verify("all").findings == ()


def test_bidirectional_predicate_and_conditional_target_freeze_before_delete(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE()-[:R {v:1}]->()")
        result = write(db,"MATCH()-[r:R]-() WHERE r.v=1 DELETE CASE WHEN r.v=1 THEN r ELSE null END RETURN 1")
        assert result.rows == ((1,),(1,))
        assert result.statistics["rows_deleted"] == 1


def test_missing_null_and_empty_path_selection(tmp_path):
    with connect(tmp_path / "db") as db:
        assert write(db,"OPTIONAL MATCH p=()-[:Absent]->() DELETE p RETURN p").rows == ((None,),)
        write(db,"CREATE(:N)")
        write(db,"MATCH(n:N) DELETE {key:null}.missing,[][0],null")
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((1,),)
        write(db,"MATCH p=(n:N)-[*0..0]->(n) DELETE p")
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((0,),)


@pytest.mark.parametrize("expression", ["1+1", "[n]", "{key:n}", "$value", "n:Label"])
def test_nonentity_targets_refuse_without_effects(tmp_path, expression):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {id:1})")
        with pytest.raises(GrafxError):
            write(db,f"MATCH(n:N) DELETE {expression}", {"value":{"id":1}})
        assert db.execute("MATCH(n:N) RETURN n.id").rows == ((1,),)


def test_late_nested_failure_restores_prior_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {id:1}),(:N {id:2})")
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep)")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i CALL(i){ MATCH(n:N {id:i}) DELETE [n][0] RETURN 1/(2-i) AS v } RETURN v")
            assert tx.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,),(2,))
        assert db.execute("MATCH(n:Keep) RETURN count(*)").rows == ((1,),)
        assert db.verify("all").findings == ()


def test_path_connected_constraint_rollback(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:N)-[:R]->(b:N)-[:Other]->(:Extra)")
        with pytest.raises(GrafxError) as failure:
            write(db,"MATCH p=()-[:R]->() DELETE p")
        assert failure.value.details["reason"] == "connected_node_delete"
        assert failure.value.details["query_phase"] == "execution"
        assert db.execute("MATCH(n) RETURN count(*)").rows == ((3,),)
        assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((2,),)
        assert db.verify("all").findings == ()


def test_delete_expression_snapshot_read_only_and_occ(tmp_path):
    path = tmp_path / "db"
    with connect(path) as left, connect(path) as right:
        write(left,"CREATE(:N {id:1})")
        with left.begin("read") as reader:
            with pytest.raises(GrafxError):
                reader.execute("MATCH(n:N) DELETE [n][0]")
            first, second = left.begin("write"), right.begin("write")
            try:
                first.execute("MATCH(n:N) DELETE [n][0]")
                second.execute("MATCH(n:N) SET n.id=2")
                first.commit()
                with pytest.raises(GrafxWriteConflict):
                    second.commit()
            finally:
                if first.active:
                    first.rollback()
                if second.active:
                    second.rollback()
            assert reader.execute("MATCH(n:N) RETURN n.id").rows == ((1,),)
        assert left.execute("MATCH(n) RETURN count(*)").rows == ((0,),)


def test_delete_input_budget_refuses_before_effects_and_transaction_remains_usable(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        write(db,"UNWIND range(1,20) AS i CREATE(:N {id:i})")
    with connect(path,query_memory_budget_bytes=1024) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as failure:
                tx.execute("MATCH(n:N) DELETE [n][0]")
            assert failure.value.details["operator"] == "delete_inputs"
            assert tx.execute("MATCH(n:N) RETURN count(*)").rows == ((20,),)
            tx.execute("MATCH(n:N {id:1}) SET n.id=21")
    with connect(path) as db:
        assert db.execute("MATCH(n:N) WHERE n.id=21 RETURN count(*)").rows == ((1,),)
        assert db.execute("MATCH(n:N) RETURN count(*)").rows == ((20,),)
        assert db.verify("all").findings == ()
