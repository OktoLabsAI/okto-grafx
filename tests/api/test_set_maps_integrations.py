"""Map updates flow through ordinary indexes/history rather than a side store."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_namespace_map_updates_preserve_indexes_history_and_endpoints(tmp_path, codec):
    path = tmp_path / "db"
    tables = (("node","R"),("rel","R"))
    with connect(path,codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,body STRING,v INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R,body STRING,v INT64)")
            tx.execute("CREATE(n:R {id:1,body:'before',v:10})-[:R {body:'before',v:20}]->(n)")
        db.create_index("by_v","R",("v",))
        db.create_text_index("node_text","R",("body",),kind="node")
        db.create_text_index("edge_text","R",("body",),kind="rel")
        db.enable_commit_history()
        db.enable_system_history(tables)
        before = db.commit_history().entries[-1].identity
        old = db.system_as_of(before,tables=tables)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:R)-[r:R]->() SET n={id:1,body:'after',v:11},r+={body:'after',v:null}")
        assert db.execute("MATCH(n:R) WHERE n.v=11 RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH(n:R) WHERE n.v=10 RETURN n.id").rows == ()
        assert db.system_as_of(before,tables=tables).rows == old.rows
        assert len(db.system_diff(before,db.commit_history().entries[-1].identity,tables=tables).rows) == 2
        for index in ("node_text","edge_text"):
            assert len(db.search_text(index=index,query="after").hits) == 1
            assert db.search_text(index=index,query="before").hits == ()
        with pytest.raises(GrafxError):
            with db.begin("write") as tx:
                tx.execute("MATCH()-[r:R]->() SET r += {_from:999}")
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path,codec=codec) as db:
        assert db.execute("MATCH(n:R)-[r:R]->(m:R) RETURN n.id,r.body,r.v,m.id").rows == ((1,"after",None,1),)
        assert db.system_as_of(before,tables=tables).rows == old.rows
        assert db.verify("all").findings == ()
