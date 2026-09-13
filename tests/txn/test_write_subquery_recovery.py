"""Real process cuts prove all CALL invocations belong to one durable commit."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_subquery_schema_and_data_recover_as_one_transaction(tmp_path, cut):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n:Prior {v:7})")
        db.checkpoint()
    worker = r'''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path, cut = sys.argv[1:]
with connect(path) as db:
    commit, apply = Transaction.commit, TransactionManager._apply_images
    active = None
    def interrupted_commit(tx):
        global active
        active = tx
        if cut == 'before_commit':
            os._exit(71)
        return commit(tx)
    def interrupted_apply(manager, images):
        if active is not None and active._context.state.value == 'committed':
            os._exit(73)
        return apply(manager, images)
    Transaction.commit = interrupted_commit
    TransactionManager._apply_images = interrupted_apply
    with db.begin('write') as tx:
        tx.execute('UNWIND [1,2] AS i CALL(i) { CREATE(a:A {v:i}) RETURN a } '
                   'CALL(a) { CREATE(b:B {v:a.v+10}), (a)-[:R]->(b) } RETURN a.v')
raise AssertionError('Expected durable cut was not reached')
'''
    completed = subprocess.run([sys.executable, "-c", worker, str(path), cut],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == (71 if cut == "before_commit" else 73), completed.stderr
    for _ in range(2):
        with connect(path) as db:
            assert db.execute("MATCH(n:Prior) RETURN n.v").rows == ((7,),)
            expected = ((1,11), (2,12)) if cut == "before_apply" else ()
            assert db.execute("MATCH(a:A)-[:R]->(b:B) RETURN a.v,b.v ORDER BY a.v").rows == expected
            assert db.execute("MATCH(n) RETURN count(n)").rows == ((5 if cut == "before_apply" else 1,),)
            assert db.verify("all").findings == ()
            db.checkpoint()
