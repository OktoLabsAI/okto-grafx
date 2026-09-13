"""Overlapping names activate only with the schema/data transaction's COMMIT."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
@pytest.mark.parametrize("typed", [False, True])
def test_namespace_activation_crash_is_atomic(tmp_path, cut, typed):
    path = tmp_path / "db"
    with connect(path) as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(:R {id:1})")
        db.checkpoint()
    worker = r'''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path, cut, typed = sys.argv[1:]
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
        if typed == 'True':
            tx.execute('CREATE REL TABLE R(FROM R TO R, v INT64)')
        tx.execute('MATCH(n:R) CREATE(n)-[:R {v:7}]->(n)')
raise AssertionError('Expected cut not reached')
'''
    completed = subprocess.run([sys.executable, "-c", worker, str(path), cut, str(typed)],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == (71 if cut == "before_commit" else 73), completed.stderr
    for _ in range(2):
        with connect(path) as db:
            assert db.execute("MATCH(n:R) RETURN n.id").rows == ((1,),)
            assert db.execute("MATCH()-[r:R]->() RETURN r.v").rows == (((7,),) if cut == "before_apply" else ())
            assert bool(db.catalog.catalog.relationship_tables("R")) == (cut == "before_apply")
            assert db.verify("all").findings == ()
            db.checkpoint()
