"""One statement can create nodes/edges while keeping a single atomic commit boundary."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxTransactionBudgetExceeded


def schema(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T(id INT64, n INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM T TO T, weight INT64)")


@pytest.mark.parametrize("query", [
    "CREATE (a:T {id:1,n:10})-[r:R {weight:7}]->(b:T {id:2,n:20}) RETURN a.id,b.id,r.weight",
    "CREATE (a:T {id:1,n:10}), (b:T {id:2,n:20}) CREATE (a)-[r:R {weight:7}]->(b) RETURN a.id,b.id,r.weight",
    "CREATE (a:T {id:1,n:10}) WITH a CREATE (b:T {id:2,n:20}) CREATE (a)-[r:R {weight:7}]->(b) RETURN a.id,b.id,r.weight",
])
def test_fresh_endpoints_owner_reader_and_reopen(tmp_path, query):
    path = tmp_path / "fresh"
    with connect(path) as db:
        schema(db)
        with connect(path) as peer, peer.begin("read") as reader:
            with db.begin("write") as tx:
                assert tx.execute(query).rows == ((1, 2, 7),)
                assert tx.execute("MATCH (a:T)-[r:R]->(b:T) RETURN a.id,b.id,r.weight").rows == ((1, 2, 7),)
                assert reader.execute("MATCH (a:T) RETURN count(*)").rows == ((0,),)
            assert reader.execute("MATCH (a:T) RETURN count(*)").rows == ((0,),)
    with connect(path) as db:
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN a.id,b.id,r.weight").rows == ((1, 2, 7),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("suffix", ["SET b.n = 'bad'", "RETURN 1 / 0", "DELETE a"])
def test_later_failure_discards_early_endpoint_intents_but_preserves_prior_work(tmp_path, suffix):
    with connect(tmp_path / "rollback") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:9})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE (a:T {id:1})-[r:R]->(b:T {id:2}) " + suffix)
            tx.execute("CREATE (:T {id:10})")
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((9,), (10,))
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN count(r)").rows == ((0,),)
        assert db.verify("all").findings == ()


def test_create_detach_and_create_delete_edge_same_statement(tmp_path):
    with connect(tmp_path / "detach") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (a:T {id:1})-[r:R]->(b:T {id:2}) DETACH DELETE a")
            tx.execute("CREATE (a:T {id:3})-[r:R]->(b:T {id:4}) DELETE r, a")
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((2,), (4,))
        assert db.verify("all").findings == ()


def test_set_return_sees_own_change_and_retains_local_identity(tmp_path):
    with connect(tmp_path / "set") as db:
        schema(db)
        with db.begin("write") as tx:
            assert tx.execute("CREATE (a:T {id:1,n:0}) SET a.n=1 SET a.n=a.n+1 RETURN a.n").rows == ((2,),)
            assert tx.execute("MATCH (a:T) SET a.n=3 RETURN a.n").rows == ((3,),)
            assert tx.execute("MATCH (a:T),(b:T) WHERE a.id=b.id SET a.n=4 RETURN a.n,b.n").rows == ((4,4),)


def test_statement_quota_spans_early_publication(tmp_path):
    with connect(tmp_path / "quota", max_statement_writes=2) as db:
        schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxTransactionBudgetExceeded):
                tx.execute("CREATE (a:T {id:1})-[r:R]->(b:T {id:2})")
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((0,),)


def test_merge_existing_relationship_binds_latest_owner_version(tmp_path):
    with connect(tmp_path / "merge-edge") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (a:T {id:1})-[r:R {weight:7}]->(b:T {id:2})")
        with db.begin("write") as tx:
            query = "MATCH (a:T {id:1}), (b:T {id:2}) MERGE (a)-[r:R]->(b)"
            assert tx.execute(query + " RETURN r.weight").rows == ((7,),)
            assert tx.execute(query + " SET r.weight=8 RETURN r.weight").rows == ((8,),)
            assert tx.execute(query + " RETURN r.weight").rows == ((8,),)
            tx.execute("MATCH (a:T)-[r:R]->(b:T) DELETE r")
            assert tx.execute(query + " RETURN r.weight").rows == ((None,),)
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN r.weight").rows == ((None,),)


def test_fresh_write_then_read_phase_traverses_owner_edges(tmp_path):
    with connect(tmp_path / "phase-read") as db:
        schema(db)
        with db.begin("write") as tx:
            result = tx.execute(
                "CREATE (a:T {id:1})-[r:R {weight:7}]->(b:T {id:2}) "
                "WITH a MATCH (a)-[s:R]->(c:T) RETURN c.id,s.weight"
            )
            assert result.rows == ((2,7),)


def test_repeat_merge_then_set_and_delete_in_one_statement(tmp_path):
    with connect(tmp_path / "repeated-merge") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:1}), (:T {id:2})")
        with db.begin("write") as tx:
            result = tx.execute(
                "MATCH (a:T {id:1}), (b:T {id:2}) "
                "MERGE (a)-[r:R {weight:7}]->(b) "
                "MERGE (a)-[s:R {weight:7}]->(b) SET s.weight=8 RETURN s.weight"
            )
            assert result.rows == ((8,),)
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN r.weight").rows == ((8,),)


def test_merging_held_nodes_and_anonymous_edges_preserves_write_identity(tmp_path):
    with connect(tmp_path / "held-identity") as db:
        schema(db)
        with db.begin("write") as tx:
            assert tx.execute(
                "MERGE (a:T {id:1}) MERGE (b:T {id:1}) SET b.n=5 RETURN a.n,b.n"
            ).rows == ((5,5),)
            assert tx.execute(
                "MATCH (a:T {id:1}) CREATE (a)-[:R {weight:7}]->(a) "
                "MERGE (a)-[s:R]->(a) SET s.weight=8 RETURN s.weight"
            ).rows == ((8,),)
        assert db.execute("MATCH (a:T) RETURN a.id,a.n").rows == ((1,5),)
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN r.weight").rows == ((8,),)


def test_fresh_public_result_failure_rolls_back_all_phases(tmp_path, monkeypatch):
    import okto_grafx.engine.database as public
    from okto_grafx.errors import GrafxPlanError
    with connect(tmp_path / "public-failure") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:9})")
            with monkeypatch.context() as patch:
                def refuse(*args, **kwargs):
                    raise GrafxPlanError("forced public boundary failure")
                patch.setattr(public, "_query_result_view", refuse)
                with pytest.raises(GrafxPlanError, match="forced public"):
                    tx.execute("CREATE (a:T {id:1})-[r:R]->(b:T {id:2}) WITH a RETURN a.id")
            tx.execute("CREATE (:T {id:10})")
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((9,), (10,))
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN count(*)").rows == ((0,),)
        assert db.verify("all").findings == ()


def test_unprovable_statement_rollback_prevents_a_later_commit(tmp_path, monkeypatch):
    from okto_grafx.domain.txn.context import TransactionContext
    from okto_grafx.errors import GrafxPlanError, GrafxTransactionStateError
    import okto_grafx.engine.database as public
    with connect(tmp_path / "rollback-fault") as db:
        schema(db)
        tx = db.begin("write")
        try:
            tx.execute("CREATE (:T {id:9})")
            with monkeypatch.context() as patch:
                def output_failure(*args, **kwargs):
                    raise GrafxPlanError("forced public failure")
                def rollback_failure(*args, **kwargs):
                    raise GrafxTransactionStateError("forced rollback failure")
                patch.setattr(public, "_query_result_view", output_failure)
                patch.setattr(TransactionContext, "discard_since", rollback_failure)
                with pytest.raises(GrafxPlanError, match="forced public"):
                    tx.execute("CREATE (a:T {id:1})-[r:R]->(b:T {id:2}) RETURN a.id")
            assert not tx.active
            with pytest.raises(GrafxTransactionStateError):
                tx.commit()
        finally:
            if tx.active:
                tx.rollback()
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((0,),)
        assert db.verify("all").findings == ()
