"""A whole-pattern MERGE's private phases share one durable COMMIT decision."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_general_merge_process_cut(tmp_path, codec, cut):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:A {v:1})-[:R]->(:B {v:2})")
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
        result=tx.execute('UNWIND [1,1] AS i MERGE p=(a:A {v:i})-[:R]->(:B {v:2})-[:S]->(:C) '
                          'ON CREATE SET a.hops=length(p) RETURN a.hops')
        assert result.rows==((2,),(2,))
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable, "-c", code, str(path), codec, cut],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path, codec=codec) as db:
            assert db.execute("MATCH(n) RETURN count(*)").rows == ((2 if cut == "before_commit" else 5,),)
            assert db.execute("MATCH()-[r]->() RETURN count(*)").rows == ((1 if cut == "before_commit" else 3,),)
            assert db.execute("MATCH(a:A) WHERE a.hops IS NOT NULL RETURN a.hops").rows == (() if cut == "before_commit" else ((2,),))
            assert db.verify("all").findings == ()
            db.checkpoint()
