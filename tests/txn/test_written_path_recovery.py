"""Read phases before MERGE do not become independent durable commits."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
@pytest.mark.parametrize("direction", ["->", "-"])
def test_written_path_phase_recovery(tmp_path,codec,cut,direction):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:A {v:1}),(:A {v:2}),(:B)")
        db.checkpoint()
    code = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path,codec,cut,direction=sys.argv[1:]
with connect(path,codec=codec) as db:
    active=None
    commit,apply=Transaction.commit,TransactionManager._apply_images
    def stop_commit(tx):
        global active
        active=tx
        if cut=='before_commit': os._exit(71)
        return commit(tx)
    def stop_apply(manager,images):
        if active is not None and active._context.state.value=='committed': os._exit(73)
        return apply(manager,images)
    Transaction.commit,TransactionManager._apply_images=stop_commit,stop_apply
    with db.begin('write') as tx:
        result=tx.execute('MATCH(old:A) DELETE old MERGE p=(a:A {v:3}) RETURN p')
        assert len(result.rows)==2
        tx.execute(f'MATCH(a:A),(b:B) MERGE p=(a)-[r:R]{direction}(b) ON CREATE SET r.hops=length(p) RETURN p')
        tx.execute('MATCH()-[r:R]->() WITH endNode(r) AS b SET b.endpoint=true')
raise AssertionError('Cut not reached')
'''
    process=subprocess.run([sys.executable,"-c",code,str(path),codec,cut,direction],capture_output=True,text=True,timeout=60)
    assert process.returncode == (71 if cut=="before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path,codec=codec) as db:
            assert db.execute("MATCH(a:A) RETURN a.v ORDER BY a.v").rows == (((1,),(2,)) if cut=="before_commit" else ((3,),))
            assert db.execute("MATCH()-[r:R]->() RETURN r.hops").rows == (() if cut=="before_commit" else ((1,),))
            assert db.execute("MATCH(b:B) RETURN b.endpoint").rows == (((None,),) if cut=="before_commit" else ((True,),))
            assert db.verify("all").findings == ()
            db.checkpoint()
