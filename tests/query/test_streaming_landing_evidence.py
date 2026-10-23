"""RF07 unittest entry points for immutable-snapshot evidence runners.

The runner imports this tracked file directly and only adds the snapshot root
to sys.path. Resolve src from this file, and refuse a previously installed Grafx
module from outside that snapshot. No pytest plugin or receipt fabrication is
involved: each registered method performs real queries and native assertions.
"""
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


_SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
Path(grafx.__file__).resolve().relative_to(_SOURCE.resolve())
qe = import_module("okto_grafx.engine.query_engine")
HeapStore = import_module("okto_grafx.engine.heap_store").HeapStore
indexes = import_module("okto_grafx.engine.index_manager")
errors = import_module("okto_grafx.errors")
RECORD_ID_KEY_DERIVATION = import_module("okto_grafx.domain.index.definition").RECORD_ID_KEY_DERIVATION
PREFIX = "MATCH (a:A)-[r:E]->(b:B) WHERE a.id='a' "


class StreamingLandingEvidence(unittest.TestCase):
    """One explicit method per automated RF07 scenario; no skipped cases."""

    def setUp(self):
        temporary = TemporaryDirectory(prefix="grafx-rf07-")
        self.addCleanup(temporary.cleanup)
        self.db = grafx.connect(Path(temporary.name) / "db", page_size=8192)
        self.addCleanup(self.db.close)
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title})",
                           {"id": f"b{i}", "title": f"Title {i}"})
                for rank in range(2):
                    tx.execute("MATCH (a:A {id:'a'}),(b:B {id:$id}) "
                               "CREATE (a)-[:E {rank:$rank}]->(b)",
                               {"id": f"b{i}", "rank": rank})
        self.db.ensure_identity_indexes()

    def _index(self):
        return next(index for index in self.db._indexes.active_indexes()
                    if index.definition.table_name == "B"
                    and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)

    def _assert_clean(self, cursor=None):
        if cursor is not None:
            self.assertTrue(cursor.closed)
            self.assertFalse(cursor._buffer)
        self.assertEqual(self.db.transactions.open_transactions, 0)
        self.assertEqual(self.db._queries._owner_budget._used_bytes, 0)

    @contextmanager
    def _destination_fault(self):
        table_id = self._index().definition.table_id
        seen = []

        def observe(original):
            def read(heap, ref):
                version = original(heap, ref)
                if version.table_id == table_id:
                    seen.append(version.values[0])
                    if version.values[0] != "b0":
                        raise errors.GrafxCorruptionDetected(
                            "Undemanded destination fault", field="rf07.destination")
                return version
            return read

        read = observe(HeapStore.read)
        landing = observe(HeapStore.read_landing)
        with (patch.object(HeapStore, "read", read),
              patch.object(HeapStore, "read_landing", landing),
              patch.object(qe, "_VECTOR_FREE_CANONICAL_READ", read)):
            yield seen

    @contextmanager
    def _batch_observer(self):
        original = indexes.IndexManager.validated_identity_landings_many
        keys = []

        def observed(manager, index, values, snapshot):
            keys.append(len(values))
            return original(manager, index, values, snapshot)

        with (patch.object(indexes.IndexManager, "validated_identity_landings_many", observed),
              patch.object(qe, "_BATCH_LANDING_CANONICAL_MANY", observed)):
            yield keys

    def test_demand_prefix(self):
        """ts_890c4832: LIMIT, filter, DISTINCT, close, corruption and duplicates."""
        cases = (
            ("RETURN b.id LIMIT 0", ()),
            ("RETURN b.id LIMIT 1", (("b0",),)),
            ("AND r.rank=1 RETURN b.id LIMIT 1", (("b0",),)),
            ("RETURN DISTINCT b.id LIMIT 1", (("b0",),)),
        )
        for suffix, expected in cases:
            with self.subTest(suffix=suffix), self._destination_fault() as seen, self._batch_observer() as batches:
                self.assertEqual(self.db.execute(PREFIX + suffix).rows, expected)
                self.assertEqual(set(seen), {"b0"} if expected else set())
                self.assertEqual(batches, [])
                self._assert_clean()
        with self._destination_fault() as seen:
            with self.db.query(PREFIX + "RETURN b.id").cursor(batch_size=1) as cursor:
                self.assertEqual(next(cursor), ("b0",))
            cursor.close()
            self.assertEqual(seen, ["b0"])
            self.assertIsNone(cursor.fetchone())
            self._assert_clean(cursor)

        target = self._index()
        original = indexes.IndexStore._candidates_unchecked
        keys = []

        def duplicate_later(store, key):
            entries = original(store, key)
            if store.file == target.file:
                keys.append(key)
                if key != keys[0]:
                    return entries + entries[:1]
            return entries

        with patch.object(indexes.IndexStore, "_candidates_unchecked", duplicate_later):
            self.assertEqual(self.db.execute(PREFIX + "RETURN b.id LIMIT 1").rows, (("b0",),))
            self.assertEqual(len(keys), 1)
            with self.assertRaises(errors.GrafxCorruptionDetected):
                self.db.execute(PREFIX + "RETURN b.id")
            self.assertEqual(len(set(keys)), 2)
            self._assert_clean()

        # Prove the protection is meaningful: only unsafe admission changes.
        with (self._destination_fault() as seen, self._batch_observer() as batches,
              patch.object(qe, "_admits_batched_landings", lambda *_: True)):
            with self.assertRaises(errors.GrafxCorruptionDetected) as caught:
                self.db.execute(PREFIX + "RETURN b.id LIMIT 1")
            self.assertEqual(caught.exception.details["field"], "rf07.destination")
            self.assertEqual(batches, [3])
            self.assertIn("b1", seen)
            self._assert_clean()

    def test_generation_failure_no_replay(self):
        """ts_d2740500: changed certificates after a published prefix terminate."""
        target = self._index()
        with self.db.query(PREFIX + "RETURN DISTINCT b.id").cursor(batch_size=1) as cursor:
            self.assertEqual(cursor.fetchone(), ("b0",))
            original = indexes.HashIndex.finish_exact_read
            attempts = []

            def changed(store, before, lsn):
                stable = original(store, before, lsn)
                if store.file == target.file:
                    attempts.append(1)
                    return False
                return stable

            with patch.object(indexes.HashIndex, "finish_exact_read", changed):
                with self.assertRaises(errors.GrafxIndexError) as caught:
                    cursor.fetchone()
            self.assertEqual(caught.exception.details["field"], "index_view_changed")
            self.assertEqual(len(attempts), indexes.INDEX_READ_RETRY_BUDGET + 1)
            self.assertIsNone(cursor.fetchone())
            self._assert_clean(cursor)

    def test_controls_cleanup_and_blocking(self):
        """ts_51f302b8: control errors win; cleanup and blocking batches survive."""
        class Clock:
            now = 10.0

            def monotonic(self):
                return self.now

        for control in ("cancel", "deadline"):
            clock = Clock()
            token = grafx.CancellationToken()
            with self.subTest(control=control), patch.object(self.db, "_clock", clock), self._destination_fault() as seen:
                with self.db.query(PREFIX + "RETURN b.id").cursor(
                    batch_size=1, cancellation=token, timeout_seconds=1,
                ) as cursor:
                    self.assertEqual(next(cursor), ("b0",))
                    if control == "cancel":
                        token.cancel()
                    else:
                        clock.now += 2
                    expected = errors.GrafxQueryCancelled if control == "cancel" else errors.GrafxQueryDeadlineExceeded
                    with self.assertRaises(expected):
                        cursor.fetchone()
                    self.assertEqual(seen, ["b0"])
                    self._assert_clean(cursor)
        with self._batch_observer() as batches:
            result = self.db.execute(PREFIX + "RETURN b.id, count(r) ORDER BY b.id")
            self.assertEqual(result.rows, (("b0", 2), ("b1", 2), ("b2", 2)))
            self.assertEqual(batches, [3])
            self._assert_clean()


if __name__ == "__main__":
    unittest.main()
