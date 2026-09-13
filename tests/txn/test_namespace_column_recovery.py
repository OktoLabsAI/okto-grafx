"""Qualified append-only DDL retains the exact physical owner across COMMIT replay."""

import subprocess
import sys

import pytest

from okto_grafx import connect


@pytest.mark.parametrize("kind", ["node", "rel"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_qualified_column_crash_recovery(tmp_path, kind, codec, cut):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R,weight INT64)")
            tx.execute("CREATE(a:R {id:1})-[:R {weight:2}]->(a)")
        db.checkpoint()
    code = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path,kind,codec,cut=sys.argv[1:]
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
    db.add_nullable_column((kind,'R'),ColumnDef('extra',ValueType.STRING))
raise AssertionError('Cut not reached')
'''
    process = subprocess.run([sys.executable,"-c",code,str(path),kind,codec,cut],
                             capture_output=True,text=True,timeout=60)
    assert process.returncode == (71 if cut == "before_commit" else 73), process.stderr
    for _ in range(2):
        with connect(path,codec=codec) as db:
            for actual in ("node", "rel"):
                table = db.catalog.catalog.table("R",kind=actual)
                expected = 2 if actual == kind and cut == "before_apply" else 1
                assert table.schema_version == expected
                assert ("extra" in {column.name for column in table.columns}) == (expected == 2)
            assert db.execute("MATCH(n:R) RETURN n.id").rows == ((1,),)
            assert db.execute("MATCH()-[r:R]->() RETURN r.weight").rows == ((2,),)
            assert db.verify("all").findings == ()
            db.checkpoint()
