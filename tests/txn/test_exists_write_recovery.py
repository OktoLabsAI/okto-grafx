"""Existential private read phases never publish an uncommitted write."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_exists_after_create_and_set_has_one_commit_boundary(tmp_path, codec, cut):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Saved {v:1})")
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
        result=tx.execute('CREATE(a:N {v:1})-[:R]->(b:N {v:2}) WITH a,b '
                          'SET a.seen=EXISTS{(a)-->(b)} '
                          'RETURN EXISTS{MATCH(n:N {seen:true})}')
        assert result.rows==((True,),),result.rows
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable,"-c",code,str(path),codec,cut],
                             capture_output=True,text=True,timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path,codec=codec) as db:
            assert db.execute("MATCH(n:Saved) RETURN n.v").rows == ((1,),)
            assert db.execute("MATCH(n:N) RETURN count(*)").rows == ((0 if cut == "before_commit" else 2,),)
            assert db.execute("RETURN EXISTS{MATCH(n:N {seen:true})}").rows == ((cut == "before_apply",),)
            assert db.execute("MATCH()-[r:R]->() RETURN count(*)").rows == ((0 if cut == "before_commit" else 1,),)
            assert db.verify("all").findings == ()
            db.checkpoint()
