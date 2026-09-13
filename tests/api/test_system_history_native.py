"""Native atomic temporal publication; isolated stores only."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.engine.system_history_store import SystemHistoryStore


def batches(db):
    with db._transactions.page_access_section(allow_writeback=False):
        sequence = db._transactions._published_state_in_section().last_committed_lsn
        store = SystemHistoryStore(db._storage.read_page, database_uuid=db.identity.database_uuid,
                                   page_size=db._pool.page_size)
        return store.read_batches(expected_sequence=sequence, page_count=db._storage.page_count("system-history.dat"))


def test_native_activation_baseline_update_delete_reopen(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, value:'before'})")
        db.enable_system_history(("N",))
        initial = batches(db)
        assert [change.operation for change in initial[0][1]] == [4, 1]
        assert initial[0][1][1].values == (1, "before")
        for query in ("MATCH (n:N {id:1}) SET n.value = 'after'", "MATCH (n:N {id:1}) DELETE n"):
            with db.begin() as tx:
                tx.execute(query)
        captured = batches(db)
        assert [change.operation for _, changes in captured for change in changes] == [4, 1, 2, 3]
        assert db.verify().clean
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert batches(db) == captured


@pytest.mark.parametrize("operation", ["activate", "update"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply", "after_current", "after_history_root", "after_history_chunk", "after_commit"])
def test_native_history_process_cuts_and_repeated_recovery(tmp_path, operation, cut):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, value:'before'})")
        if operation == "update":
            db.enable_system_history(("N",))
        db.checkpoint()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    worker = Path(__file__).with_name("system_history_worker.py")
    process = subprocess.run([sys.executable, str(worker), str(root), operation, cut],
                             env=env, capture_output=True, text=True, timeout=60)
    assert process.returncode in (71, 72, 73, 74), process.stdout + process.stderr
    original = None
    for _ in range(2):
        with connect(root, page_size=512) as db:
            active = bool(db._catalog.catalog.system_history_tables())
            assert active == (operation == "update" or cut != "before_commit")
            if active:
                current = batches(db)
                if original is not None:
                    assert current == original
                original = current
                values = [change.values for _, changes in current for change in changes if change.operation in (1, 2)]
                assert values[0] == (1, "before")
                assert len(values) == (2 if operation == "update" and cut != "before_commit" else 1)
            with db.begin("read") as tx:
                answer = tx.execute("MATCH (n:N) RETURN n.value").rows
            expected = "changed" * 200 if operation == "update" and cut != "before_commit" else "before"
            assert answer == ((expected,),)
            assert db.verify().clean
            db.checkpoint()


def test_old_writer_and_independent_reader_preserve_temporal_publication(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, value:'before'})")
        with connect(root, page_size=512) as other:
            with other.begin() as staged:
                staged.execute("MATCH (n:N {id:1}) SET n.value = 'old-writer'")
                db.enable_system_history(("N",))
            assert [change.operation for _, changes in batches(other) for change in changes] == [4, 1, 2]
        db.checkpoint()
