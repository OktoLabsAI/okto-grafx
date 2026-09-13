"""Durable publication and recovery of a second vector owner in one space."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("kind,peer", [("node","Other"), ("rel","Other"), ("rel","R")])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_second_vector_owner_crash_recovery(tmp_path, kind, peer, codec, cut):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
            tx.execute("CREATE(:R {id:1,embedding:$v})", {"v":[1.0,0.0]})
        db.checkpoint()
    code = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path,kind,peer,codec,cut=sys.argv[1:]
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
        if kind=='node':
            tx.execute(f'CREATE NODE TABLE {peer}(id INT64,embedding VECTOR(s),PRIMARY KEY(id))')
            tx.execute(f'CREATE(:{peer} {{id:2,embedding:$v}})',{'v':[0.0,1.0]})
        else:
            tx.execute(f'CREATE REL TABLE {peer}(FROM R TO R,id INT64,embedding VECTOR(s))')
            tx.execute(f'MATCH(n:R) CREATE(n)-[:{peer} {{id:2,embedding:$v}}]->(n)',{'v':[0.0,1.0]})
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable, "-c", code, str(path), kind, peer, codec, cut],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    committed = cut == "before_apply"
    for _ in range(2):
        with connect(path, codec=codec) as db:
            assert db.catalog.catalog.has_table(peer, kind=kind) is committed
            if peer == "R":
                assert db._catalog.catalog.requires_capability("vector_owner_names_v1") is committed
            assert len(db.vectors.indexes()) == (2 if committed else 1)
            with db.begin("read") as tx:
                assert db.search_vectors(tx, table=("node","R"), space="s", query=[1.0,0.0], k=1).hits[0].score == 1.0
                if committed:
                    assert db.search_vectors(tx, table=(kind,peer), space="s", query=[1.0,0.0], k=1).hits[0].score == 0.0
            assert db.verify("all").findings == ()
            db.checkpoint()
