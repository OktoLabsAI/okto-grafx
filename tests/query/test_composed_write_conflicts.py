"""Committed integrity of composed writes under competing native transaction snapshots."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxWriteConflict


def schema(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE U(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM T TO T)")
        tx.execute("CREATE (:T {id: 1}), (:T {id: 2})")


@pytest.mark.parametrize("first", ("delete", "edge"))
def test_delete_and_edge_creation_cannot_both_commit_from_conflicting_snapshots(tmp_path, first):
    path = tmp_path / "conflict"
    with connect(path) as db:
        schema(db)
        with connect(path) as peer:
            deleting, creating = db.begin("write"), peer.begin("write")
            reader = peer.begin("read")
            try:
                assert reader.execute("MATCH (n:T) RETURN count(*)").rows == ((2,),)
                deleting.execute("MATCH (n:T) WHERE n.id=1 DELETE n")
                creating.execute("MATCH (a:T), (b:T) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R]->(b)")
                winner, loser = (deleting, creating) if first == "delete" else (creating, deleting)
                assert winner.commit().durable
                with pytest.raises(GrafxWriteConflict):
                    loser.commit()
                # A reader registered before either writer still observes its own complete state.
                assert reader.execute("MATCH (n:T) RETURN count(*)").rows == ((2,),)
                assert reader.execute("MATCH (a:T)-[r:R]->(b:T) RETURN count(r)").rows == ((0,),)
            finally:
                for tx in (deleting, creating, reader):
                    if tx.active:
                        tx.rollback()
        expected = ((2,),) if first == "delete" else ((1,), (2,))
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == expected
        assert db.verify("all").findings == ()
    with connect(path) as reopened:
        assert reopened.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == expected
        assert reopened.execute("MATCH (a:T)-[r:R]->(b:T) RETURN count(r)").rows == ((0 if first == "delete" else 1,),)


def test_unrelated_writer_and_old_reader_continue_across_composed_write(tmp_path):
    path = tmp_path / "independent"
    with connect(path) as db:
        schema(db)
        with connect(path) as peer, peer.begin("read") as old:
            first, second = db.begin("write"), peer.begin("write")
            try:
                result = first.execute("CREATE (:T {id:3}) WITH count(*) AS added MATCH (n:T) "
                                       "RETURN added,n.id ORDER BY n.id")
                assert result.rows == ((1,1), (1,2), (1,3))
                second.execute("CREATE (:U {id:9})")
                assert first.commit().durable
                assert second.commit().durable
                assert old.execute("MATCH (n:T) RETURN count(*)").rows == ((2,),)
                assert old.execute("MATCH (n:U) RETURN count(*)").rows == ((0,),)
            finally:
                for tx in (first, second):
                    if tx.active:
                        tx.rollback()
        assert db.execute("MATCH (n:U) RETURN n.id").rows == ((9,),)
        assert db.verify("all").findings == ()


def test_competing_fresh_endpoint_statements_cannot_commit_duplicate_keys(tmp_path):
    path = tmp_path / "fresh-conflict"
    with connect(path) as db:
        schema(db)
        with connect(path) as peer:
            first, second = db.begin("write"), peer.begin("write")
            try:
                first.execute("CREATE (a:T {id:3})-[:R]->(b:T {id:4})")
                second.execute("CREATE (a:T {id:3})-[:R]->(b:T {id:5})")
                assert first.commit().durable
                with pytest.raises(GrafxWriteConflict):
                    second.commit()
            finally:
                for tx in (first, second):
                    if tx.active:
                        tx.rollback()
    with connect(path) as db:
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (3,), (4,))
        assert db.execute("MATCH (a:T)-[r:R]->(b:T) RETURN a.id,b.id").rows == ((3,4),)
        assert db.verify("all").findings == ()
