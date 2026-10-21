"""Execute the published application recipe against real durable stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.errors import GrafxPlanError, GrafxTransactionBudgetExceeded, GrafxWriteConflict


INSERT = "CREATE (:Item {id: $id})"


def recipe():
    text = (Path(__file__).parents[2] / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    block = text.split("<!-- rf09-conflict-recipe -->", 1)[1].split("```python", 1)[1]
    namespace = {}
    exec(compile(block.split("```", 1)[0], "OPERATIONS.md", "exec"), namespace)
    return namespace["write_with_conflict_retry"]


def setup(db):
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")


def rows(db):
    return db.execute("MATCH (n:Item) RETURN n.id ORDER BY n.id").rows


def test_documented_retry_recomputes_from_fresh_snapshot(tmp_path):
    write = recipe()
    path = tmp_path / "store"
    with connect(path) as db:
        setup(db)
        with db.begin("write") as txn:
            txn.execute(INSERT, {"id": 0})
        seen = []

        def operation(txn):
            value = txn.execute("MATCH (n:Item) RETURN n.id ORDER BY n.id").rows[-1][0]
            seen.append(value)
            if len(seen) == 1:
                with db.begin("write") as competing:
                    competing.execute("MATCH (n:Item {id: 0}) SET n.id = 10")
            txn.execute("MATCH (n:Item) SET n.id = $id", {"id": value + 1})

        report = write(db, operation, pause=lambda _: None)
        assert report.durable and seen == [0, 10]
        assert db.transactions.open_transactions == 0
    with connect(path) as reopened:
        assert rows(reopened) == ((11,),)


@pytest.mark.parametrize("after_commit", [False, True])
def test_recipe_never_replays_explicit_refusal_or_acknowledged_commit(tmp_path, monkeypatch, after_commit):
    write = recipe()
    original = Transaction.commit
    calls = []

    def commit(txn):
        if after_commit:
            original(txn)
        # After a real durable commit, even a misleading retryable flag cannot permit replay.
        raise GrafxWriteConflict("injected outcome", retryable=after_commit)

    with connect(tmp_path / "store") as db:
        setup(db)
        with monkeypatch.context() as patch:
            patch.setattr(Transaction, "commit", commit)
            with pytest.raises(GrafxWriteConflict):
                write(db, lambda txn: (calls.append(txn.txn_id), txn.execute(INSERT, {"id": 1})),
                      pause=lambda _: pytest.fail("must not back off or replay"))
        assert len(calls) == 1
        assert rows(db) == (((1,),) if after_commit else ())
        assert db.transactions.open_transactions == 0


def test_caught_batch_failure_is_lazy_and_preserves_only_prior_work(tmp_path):
    path = tmp_path / "store"
    consumed = []

    def inputs():
        for item in ({"id": 1}, {}, {"id": 3}):
            consumed.append(item)
            yield item

    with connect(path) as db:
        setup(db)
        with db.begin("write") as txn:
            txn.execute(INSERT, {"id": 0})
            with pytest.raises(GrafxPlanError):
                txn.executemany(INSERT, inputs())
        assert len(consumed) == 2
    with connect(path) as reopened:
        assert rows(reopened) == ((0,),)


def test_chunks_have_explicit_durable_prefix_not_whole_import_atomicity(tmp_path):
    path = tmp_path / "store"
    with connect(path) as db:
        setup(db)
        with db.begin("write") as txn:
            txn.executemany(INSERT, ({"id": 1}, {"id": 2}))
        assert txn.report.durable
        with pytest.raises(GrafxPlanError), db.begin("write") as txn:
            txn.executemany(INSERT, ({"id": 3}, {}))
        assert db.transactions.open_transactions == 0
    with connect(path) as reopened:
        assert rows(reopened) == ((1,), (2,))


@pytest.mark.parametrize("shape", ["statements", "executemany", "unwind"])
def test_equal_atomic_contracts_survive_reopen(tmp_path, shape):
    path = tmp_path / "store"
    with connect(path) as db:
        setup(db)
        with db.begin("write") as txn:
            if shape == "statements":
                for value in (1, 2):
                    txn.execute(INSERT, {"id": value})
            elif shape == "executemany":
                txn.executemany(INSERT, ({"id": value} for value in (1, 2)))
            else:
                txn.execute("UNWIND $ids AS id CREATE (:Item {id: id})", {"ids": [1, 2]})
        assert txn.report.durable and txn.report.wrote
    with connect(path) as reopened:
        assert rows(reopened) == ((1,), (2,))


def test_budget_refusal_does_not_silently_chunk_or_retry(tmp_path):
    write = recipe()
    with connect(tmp_path / "store", max_transaction_rows=2) as db:
        setup(db)
        calls = []

        def operation(txn):
            calls.append(txn.txn_id)
            txn.executemany(INSERT, ({"id": i} for i in range(3)))

        with pytest.raises(GrafxTransactionBudgetExceeded):
            write(db, operation, pause=lambda _: pytest.fail("budget is not a retry"))
        assert len(calls) == 1 and rows(db) == ()
        assert db.transactions.open_transactions == 0
