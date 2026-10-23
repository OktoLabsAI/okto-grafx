"""Native, immutable-source RF05 functional scenario entry points."""
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


_SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
values = import_module("okto_grafx.domain.model.value")
for module in (grafx, values):
    Path(module.__file__).resolve().relative_to(_SOURCE.resolve())


PAGE = (
    "MATCH (n:Item) RETURN n.id,n.created_at "
    "ORDER BY n.created_at DESC,n.id DESC LIMIT $maximum"
)


def canonical(query):
    return query.replace(" LIMIT $maximum", " SKIP 0 LIMIT $maximum")


class LabelKeysetEvidence(unittest.TestCase):
    """One native method per RF05 functional scenario; no timing assertions."""

    def setUp(self):
        temp = TemporaryDirectory(prefix="grafx-rf05-")
        self.addCleanup(temp.cleanup)
        self.db = grafx.connect(Path(temp.name) / "db", codec="pure", vector_math="pure")
        self.addCleanup(self.db.close)

    def _seed(self, count, *, index=True):
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id STRING, created_at TIMESTAMP, PRIMARY KEY(id))")
        rows = [{"id": f"n-{i:04d}", "ts": values.Timestamp(1000 + i // 3)}
                for i in range(count)]
        with self.db.begin("write") as tx:
            tx.execute("UNWIND $rows AS r CREATE (:Item {id:r.id, created_at:r.ts})", {"rows": rows})
        if index:
            self.db.create_index("item_page", "Item", ("created_at", "id"), layout="ordered")

    def _compare(self, query, params, handle=None):
        handle = handle or self.db
        result = handle.execute(query, params)
        reference = handle.execute(canonical(query), params)
        self.assertEqual(result.columns, reference.columns)
        self.assertEqual(result.rows, reference.rows)
        return result

    def test_1024_nodes_bounded_sixteen_candidates(self):
        """ts_bdc18a98: label path reads 16 candidates for a 16-row page."""
        self._seed(1024)
        result = self._compare(PAGE, {"maximum": 16})
        self.assertEqual(result.rows, tuple(
            (f"n-{i:04d}", values.Timestamp(1000 + i // 3))
            for i in range(1023, 1007, -1)
        ))
        self.assertEqual(result.statistics.get("ordered_candidates_examined"), 16)
        self.assertEqual(result.statistics.get("ordered_merge_rows"), 16)
        self.assertEqual(result.statistics.get("physical_node_versions_examined"), 16)
        self.assertTrue(any(type(node).__name__ == "OrderedNodeMerge" for node in result.plan.walk()))

    def test_membership_ties_null_and_keyset_same_snapshot(self):
        """ts_6c645353: rejected candidates do not spend LIMIT; strict cursor."""
        self._seed(40)
        with self.db.begin("write") as tx:
            tx.execute("MATCH(n:Item) SET n:Selected:Other")
            tx.execute("MATCH(n:Item) WHERE n.id >= 'n-0024' REMOVE n:Item")
        for label in ("Item", "Selected", "Selected:Other"):
            with self.subTest(label=label):
                query = PAGE.replace(":Item", ":" + label)
                with self.db.begin("read") as reader:
                    first = self._compare(query, {"maximum": 7}, reader)
                    seen = list(first.rows)
                    while seen and seen[-1][1] is not None:
                        cursor = query.replace(
                            " RETURN", " WHERE n.created_at < $ts OR "
                            "(n.created_at = $ts AND n.id < $pk) RETURN",
                        )
                        next_page = self._compare(cursor, {
                            "maximum": 7, "ts": seen[-1][1], "pk": seen[-1][0]
                        }, reader)
                        if not next_page.rows:
                            break
                        seen.extend(next_page.rows)
                    expected = reader.execute(canonical(query), {"maximum": 100}).rows
                    self.assertEqual(tuple(seen), expected)
                    self.assertEqual(len(seen), len(set(seen)))
        item = self._compare(PAGE, {"maximum": 16})
        self.assertEqual(item.rows[0][0], "n-0023")
        self.assertEqual(item.statistics.get("ordered_candidates_examined"), 32)
        with self.db.begin("write") as tx:
            tx.execute("CREATE (:Item {id:'null-time', created_at:NULL})")
        self.assertEqual(self._compare(PAGE, {"maximum": 16}).rows[0][0], "null-time")
        null_cursor = PAGE.replace(" RETURN", " WHERE n.created_at < $ts RETURN")
        self.assertEqual(self._compare(null_cursor, {"ts": None, "maximum": 16}).rows, ())

    def test_fallback_shapes_authority_and_owner_writes(self):
        """ts_23bd1309: unsafe shapes and stale/owned states retain canonical path."""
        self._seed(40)
        for query in (
            PAGE.replace("n.id,n.created_at", "n"),
            canonical(PAGE),
            PAGE.replace("n.id DESC", "n.id ASC"),
        ):
            with self.subTest(query=query):
                result = self.db.execute(query, {"maximum": 16})
                self.assertFalse(any(type(node).__name__ == "OrderedNodeMerge" for node in result.plan.walk()))
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Other(id STRING, created_at TIMESTAMP, PRIMARY KEY(id))")
        with self.db.begin("write") as tx:
            tx.execute("CREATE (:Other {id:'foreign', created_at:$ts})", {"ts": values.Timestamp(9000)})
            tx.execute("MATCH(n) SET n:Shared")
        self.db.create_index("other_page", "Other", ("created_at", "id"), layout="ordered")
        shared = PAGE.replace(":Item", ":Shared")
        self.assertEqual(self._compare(shared, {"maximum": 16}).rows[0][0], "foreign")
        self.assertFalse(any(type(node).__name__ == "OrderedNodeMerge" for node in
                             self.db.explain(shared).walk()))
        tx = self.db.begin("write")
        try:
            tx.execute("MATCH(n:Item {id:'n-0039'}) REMOVE n:Item")
            own = self._compare(PAGE, {"maximum": 16}, tx)
            self.assertEqual(own.statistics.get("ordered_merge_rows", 0), 0)
        finally:
            if tx.active:
                tx.rollback()
        store = next(index for index in self.db._indexes._indexes.values()
                     if index.definition.name == "item_page")
        store.mark_stale("RF05 native fallback evidence")
        stale = self._compare(PAGE, {"maximum": 16})
        self.assertEqual(stale.statistics.get("ordered_merge_rows", 0), 0)


if __name__ == "__main__":
    unittest.main()
