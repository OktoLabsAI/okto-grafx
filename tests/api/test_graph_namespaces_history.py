"""Historical rows, graph endpoints and retention controls keep kind identity."""

import subprocess
import sys

import pytest

from okto_grafx import connect, CommitId, TemporalLimits
from okto_grafx.errors import GrafxError, GrafxHistoryExpired


TABLES = (("node", "R"), ("rel", "R"))


def prepare(db, indexed=False):
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE R(id INT64, v INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM R TO R, v INT64)")
        tx.execute("CREATE(a:R {id:1,v:10})-[:R {v:20}]->(a)")
    db.enable_commit_history()
    db.enable_system_history(TABLES)
    before = db.commit_history().entries[-1].identity
    if indexed:
        db.enable_system_history_index()
    return before


@pytest.mark.parametrize("indexed", [False, True])
def test_history_versions_diff_pins_retention_compaction_and_reopen(tmp_path, indexed):
    path = tmp_path / "db"
    limits = TemporalLimits(access_path="index" if indexed else "scan")
    with connect(path) as db:
        before = prepare(db, indexed)
        old = db.system_as_of(before,tables=TABLES,limits=limits)
        assert len(old.rows) == 2
        ids = {s.kind:s.table_id for s in old.schemas}
        edge = next(r for r in old.rows if r.table_id == ids["rel"])
        node = next(r for r in old.rows if r.table_id == ids["node"])
        assert edge.values[:2] == (node.record_id,node.record_id)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:R)-[r:R]->() SET n.v=11,r.v=21")
        after = CommitId(db.identity.database_uuid,tx.report.csn)
        new = db.system_as_of(after,tables=TABLES,limits=limits)
        assert sorted(r.values[-1] for r in new.rows) == [11,21]
        assert db.system_as_of(before,tables=TABLES,limits=limits).rows == old.rows
        assert len(db.system_versions(("rel","R"),edge.record_id,limits=limits).versions) == 2
        assert len(db.system_diff(before,after,tables=TABLES,limits=limits).rows) == 2
        with pytest.raises(GrafxError):
            db.system_as_of(before,tables=("R",))
        with pytest.raises(GrafxError):
            db.system_as_of(before,tables=(("rel","R"),))
        db.pin_system_history("keep",before,tables=TABLES)
        assert set(db.system_history_pins()[0].tables) == set(TABLES)
        with pytest.raises(GrafxError):
            db.prune_system_history(after,tables=TABLES)
        db.unpin_system_history("keep")
        report = db.prune_system_history(after,tables=TABLES)
        assert report.redacted_versions == 2
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(before,tables=TABLES,limits=limits)
        db.compact_system_history(confirm_quiescent=True)
        assert db.system_as_of(after,tables=TABLES,limits=limits).rows == new.rows
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path,read_only=True) as db:
        assert db.system_as_of(after,tables=TABLES,limits=limits).rows == new.rows
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("selectors", [("R",), (("node","R"),("node","R")), (("relationship","R"),), ((False,"R"),)])
def test_activation_and_control_refuse_bad_selection_without_publication(tmp_path, selectors):
    with connect(tmp_path / "db") as db:
        before = prepare(db)
        sequence = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxError):
            db.enable_system_history(selectors)
        with pytest.raises(GrafxError):
            db.pin_system_history("bad",before,tables=selectors)
        assert db.system_history_pins() == ()
        assert db.transactions.published_state().last_committed_lsn == sequence


@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
@pytest.mark.parametrize("indexed", [False, True])
def test_crash_keeps_current_rows_and_history_at_same_outcome(tmp_path, cut, indexed):
    path = tmp_path / "db"
    with connect(path) as db:
        before = prepare(db,indexed)
        db.checkpoint()
    code = r'''
import os,sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
with connect(sys.argv[1]) as db:
    active = None
    commit, apply = Transaction.commit, TransactionManager._apply_images
    def stop_commit(tx):
        global active
        active = tx
        if sys.argv[2] == 'before_commit': os._exit(71)
        return commit(tx)
    def stop_apply(manager, images):
        if active is not None and active._context.state.value == 'committed': os._exit(73)
        return apply(manager,images)
    Transaction.commit, TransactionManager._apply_images = stop_commit, stop_apply
    with db.begin('write') as tx:
        tx.execute('MATCH(n:R)-[r:R]->() SET n.v=11,r.v=21')
raise AssertionError('Cut not reached')
'''
    proc = subprocess.run([sys.executable,"-c",code,str(path),cut],capture_output=True,text=True,timeout=60)
    assert proc.returncode == (71 if cut == "before_commit" else 73), proc.stderr
    for _ in range(2):
        with connect(path) as db:
            values = [10,20] if cut == "before_commit" else [11,21]
            latest = db.commit_history().entries[-1].identity
            history = db.system_as_of(latest,tables=TABLES)
            assert sorted(r.values[-1] for r in history.rows) == values
            assert db.execute("MATCH(n:R)-[r:R]->() RETURN n.v,r.v").rows == (tuple(values),)
            assert sorted(r.values[-1] for r in db.system_as_of(before,tables=TABLES).rows) == [10,20]
            assert db.verify("all").findings == ()
            db.checkpoint()
