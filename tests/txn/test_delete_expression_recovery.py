"""Path/container deletion and ordinary effects share exactly one durable outcome."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_delete_expression_crash_recovery(tmp_path, codec, cut):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(a:N {id:1})-[:R]->(b:N {id:2})-[:R]->(a),(:Keep {v:1})")
        db.checkpoint()
    code = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path,codec,cut=sys.argv[1:]
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
        tx.execute('MATCH p=(n:N {id:1})-[:R*2..2]->(n) WITH {paths:collect(p)} AS m DELETE m.paths[0]')
        tx.execute('MATCH(n:Keep) SET n.v=2')
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable,"-c",code,str(path),codec,cut],
                             capture_output=True,text=True,timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path,codec=codec) as db:
            assert db.execute("MATCH(n:N) RETURN count(*)").rows == (((2 if cut == "before_commit" else 0),),)
            assert db.execute("MATCH()-[r:R]->() RETURN count(*)").rows == (((2 if cut == "before_commit" else 0),),)
            assert db.execute("MATCH(n:Keep) RETURN n.v").rows == (((1 if cut == "before_commit" else 2),),)
            assert db.verify("all").findings == ()
            db.checkpoint()
