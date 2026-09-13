"""All UNION branches require the same durable COMMIT proof after a process cut."""

import subprocess
import sys

import pytest

from okto_grafx import connect


QUERIES = {
    "returning": ("CREATE(a:A {v:1}) RETURN 1 AS x UNION ALL MATCH(a:A) "
                  "CREATE(b:B {v:11}), (a)-[:R]->(b) RETURN 2 AS x"),
    "unit": "CREATE(a:A {v:1}) UNION ALL MATCH(a:A) CREATE(b:B {v:11}), (a)-[:R]->(b)",
    "correlated": ("UNWIND [1,2] AS i CALL(i){ CREATE(a:A {v:i}) RETURN 1 AS x UNION ALL "
                   "MATCH(a:A {v:i}) CREATE(b:B {v:i+10}), (a)-[:R]->(b) RETURN 2 AS x } RETURN x"),
}


@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
@pytest.mark.parametrize("mode", QUERIES)
def test_branch_schema_nodes_edges_recover_as_one_commit(tmp_path, cut, mode):
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
path, cut, query = sys.argv[1:]
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
        tx.execute(query)
raise AssertionError('Expected process cut not reached')
'''
    completed = subprocess.run([sys.executable, "-c", worker, str(path), cut, QUERIES[mode]],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == (71 if cut == "before_commit" else 73), completed.stderr
    pairs = ((1,11), (2,12)) if mode == "correlated" else ((1,11),)
    expected = pairs if cut == "before_apply" else ()
    for _ in range(2):
        with connect(path) as db:
            assert db.execute("MATCH(n:Prior) RETURN n.v").rows == ((7,),)
            assert db.execute("MATCH(a:A)-[:R]->(b:B) RETURN a.v,b.v ORDER BY a.v").rows == expected
            assert db.execute("MATCH(n) RETURN count(*)").rows == ((1+2*len(expected),),)
            if not expected:
                assert {table.name for table in db.catalog.catalog.tables()} == {"Prior"}
            assert db.verify("all").findings == ()
            db.checkpoint()
