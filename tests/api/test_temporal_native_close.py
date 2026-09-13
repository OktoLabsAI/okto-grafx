"""The temporal admission hook preserves commit-versus-close lifecycle ownership."""

import threading
import time

from okto_grafx import connect, DateValue
from okto_grafx.engine.txn_manager import TransactionManager


def test_first_temporal_update_finishes_when_commit_wins_before_close(tmp_path, monkeypatch):
    path = tmp_path / "db"
    db = connect(path, page_size=512)
    db.ensure_identity_indexes()
    with db.begin() as schema:
        schema.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        schema.execute("CREATE (:N {id:1, val:'before'})")
    tx = db.begin()
    tx.execute("MATCH (n:N) SET n.val=$v", {"v": DateValue(2000)})
    inside, release = threading.Event(), threading.Event()
    failures, outcomes = [], []
    original = TransactionManager._validate_staged_inputs
    def paused(manager, context):
        original(manager, context)
        if manager is db._transactions:
            inside.set()
            assert release.wait(10), "test never released the commit winner"
    def run(operation):
        try:
            outcomes.append(operation())
        except BaseException as error:
            failures.append(error)
    with monkeypatch.context() as patch:
        patch.setattr(TransactionManager, "_validate_staged_inputs", paused)
        writer = threading.Thread(target=run, args=(tx.commit,))
        closer = threading.Thread(target=run, args=(db.close,))
        writer.start()
        try:
            assert inside.wait(10)
            closer.start()
            deadline = time.monotonic() + 10
            while not db._transactions.closed and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert db._transactions.closed
            assert closer.is_alive()
        finally:
            release.set()
            writer.join(10)
            if closer.ident is not None:
                closer.join(10)
        assert not writer.is_alive() and not closer.is_alive()
    try:
        assert failures == [] and len(outcomes) == 2
    finally:
        db.close()
    with connect(path, page_size=512) as reopened:
        assert reopened.execute("MATCH (n:N) RETURN n.val").rows == ((DateValue(2000),),)
        assert not reopened.verify("all").findings
