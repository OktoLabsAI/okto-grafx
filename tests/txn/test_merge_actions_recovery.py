"""Conditional action effects never survive independently of their outer COMMIT."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("maps", [False, True])
def test_mixed_created_matched_actions_recover_as_one_commit(tmp_path, cut, codec, maps):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:1,v:10}),(:N {id:2,v:20})")
        db.checkpoint()
    worker = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path, cut, codec, maps = sys.argv[1:]
with connect(path,codec=codec) as db:
    active = None
    commit, apply = Transaction.commit, TransactionManager._apply_images
    def stop_commit(tx):
        global active
        active = tx
        if cut == 'before_commit': os._exit(71)
        return commit(tx)
    def stop_apply(manager,images):
        if active is not None and active._context.state.value == 'committed': os._exit(73)
        return apply(manager,images)
    Transaction.commit, TransactionManager._apply_images = stop_commit, stop_apply
    with db.begin('write') as tx:
        if maps == 'True':
            tx.execute('UNWIND [1,2,3] AS i MERGE(n:N {id:i}) ON CREATE SET n={id:i,v:30} ON MATCH SET n+={v:n.v+1}')
            tx.execute('MATCH(a:N {id:1}),(b:N {id:3}) MERGE(a)-[r:R]->(b) ON CREATE SET r={v:1}')
            tx.execute('MATCH(a:N {id:1}),(b:N {id:3}) MERGE(a)-[r:R]->(b) ON MATCH SET r+={v:r.v+10}')
        else:
            tx.execute('UNWIND [1,2,3] AS i MERGE(n:N {id:i}) ON CREATE SET n.v=30 ON MATCH SET n.v=n.v+1')
            tx.execute('MATCH(a:N {id:1}),(b:N {id:3}) MERGE(a)-[r:R]->(b) ON CREATE SET r.v=1')
            tx.execute('MATCH(a:N {id:1}),(b:N {id:3}) MERGE(a)-[r:R]->(b) ON MATCH SET r.v=r.v+10')
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable,"-c",worker,str(path),cut,codec,str(maps)],
                             capture_output=True,text=True,timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path,codec=codec) as db:
            assert db.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == (
                ((1,10),(2,20)) if cut == "before_commit" else ((1,11),(2,21),(3,30)))
            assert db.execute("MATCH(a)-[r:R]->(b) RETURN a.id,r.v,b.id").rows == (
                () if cut == "before_commit" else ((1,11,3),))
            assert db.verify("all").findings == ()
            db.checkpoint()
