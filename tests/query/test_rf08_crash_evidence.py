"""Real process termination around RF08 commit; also a native unittest entrypoint."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from okto_grafx import connect  # noqa: E402
from okto_grafx.domain.errors import GrafxError  # noqa: E402

CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
path, cut = sys.argv[2:]
with connect(path, codec="pure") as db:
    active = None
    original_commit = Transaction.commit
    original_apply = TransactionManager._apply_images
    def stop_commit(tx):
        global active
        active = tx
        if cut == "before_commit":
            os._exit(71)
        return original_commit(tx)
    def stop_apply(manager, images):
        if active is not None and active._context.state.value == "committed":
            os._exit(73)
        return original_apply(manager, images)
    Transaction.commit = stop_commit
    TransactionManager._apply_images = stop_apply
    with db.begin("write") as tx:
        result = tx.execute("UNWIND $ids AS id CREATE (n:N {id:id}) RETURN n.id",
                            {"ids": list(range(128))})
        assert result.rows == tuple((i,) for i in range(128))
        assert result.statistics["pk_uniqueness_batches"] == 2
        assert result.statistics["certificate_attempts"] == 2
    print("ACK", flush=True)
raise AssertionError("Crash boundary was not reached")
'''


class RF08CrashEvidence(unittest.TestCase):
    def test_rollback_and_lazy_consumption_preserve_prior_work(self):
        seen = []

        def parameters():
            for values in ([1, 2], [3, 1], [4, 5]):
                seen.append(values)
                yield {"ids": values}

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db"
            with connect(path, codec="pure") as db:
                with db.begin("write") as tx:
                    tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
                    tx.execute("CREATE (:N {id:-1})")
                with db.begin("write") as tx:
                    tx.execute("CREATE (:N {id:-2})")
                    with self.assertRaises(GrafxError):
                        tx.executemany("UNWIND $ids AS id CREATE (n:N {id:id})", parameters())
                    self.assertEqual(seen, [[1, 2], [3, 1]])
                    self.assertEqual(tx.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows,
                                     ((-2,), (-1,)))
                    tx.rollback()
            with connect(path, codec="pure") as db:
                self.assertEqual(db.execute("MATCH(n:N) RETURN n.id").rows, ((-1,),))
                self.assertEqual(db.verify("all").findings, ())

    def _check(self, cut, expected_code, committed):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db"
            with connect(path, codec="pure") as db:
                with db.begin("write") as tx:
                    tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
                    tx.execute("CREATE (:N {id:-1})")
                db.checkpoint()
            process = subprocess.run(
                [sys.executable, "-E", "-P", "-B", "-c", CHILD,
                 str(ROOT / "src"), str(path), cut],
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(process.returncode, expected_code, process.stderr)
            self.assertNotIn("ACK", process.stdout)
            expected = ((-1,),) + (tuple((i,) for i in range(128)) if committed else ())
            # Reopen twice: recovery is idempotent, preserves the old committed row,
            # and never publishes a prefix or duplicates the recovered batch.
            for _ in range(2):
                with connect(path, codec="pure") as db:
                    self.assertEqual(db.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows,
                                     expected)
                    self.assertEqual(db.verify("all").findings, ())
                    db.checkpoint()

    def test_before_commit_has_no_batch_or_ack(self):
        self._check("before_commit", 71, False)

    def test_after_barrier_recovers_entire_batch_without_ack(self):
        self._check("before_apply", 73, True)


if __name__ == "__main__":
    unittest.main()
