"""RF03 native snapshot assertions; no timing or campaign approval."""

from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

_SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
errors = import_module("okto_grafx.domain.errors")
indexes = import_module("okto_grafx.engine.index_manager")
plan = import_module("okto_grafx.domain.query.plan")
for module in (grafx, errors, indexes, plan):
    Path(module.__file__).resolve().relative_to(_SOURCE.resolve())


class ConnectedAnchorEvidence(unittest.TestCase):
    def setUp(self):
        # AC1 fixes the large fixture. Boundary cases use a smaller table so a
        # broad probe set can exceed 2*allocated without breaking the API list cap.
        self.node_count = (1024 if self._testMethodName ==
                           "test_connected_1024_128_1151_eight_keys" else 16)
        temporary = TemporaryDirectory(prefix="grafx-rf03-")
        self.addCleanup(temporary.cleanup)
        self.db = grafx.connect(Path(temporary.name) / "db", codec="pure", vector_math="pure")
        self.addCleanup(self.db.close)
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, v INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO B, weight INT64)")
        with self.db.begin("write") as tx:
            tx.execute("UNWIND $rows AS r CREATE (:A {id:r.id,v:r.v})",
                       {"rows": [{"id": f"a{i}", "v": i} for i in range(self.node_count)]})
            tx.execute("UNWIND $ids AS id CREATE (:B {id:id})",
                       {"ids": [f"b{i}" for i in range(128)]})
        self.edges = [(i, i % 128, i) for i in range(self.node_count)]
        self.edges += [(i, i % 128, 1024 + i) for i in range(min(127, self.node_count))]
        with self.db.begin("write") as tx:
            for a, b, weight in self.edges:
                tx.execute("MATCH (a:A {id:$a}),(b:B {id:$b}) "
                           "CREATE (a)-[:R {weight:$w}]->(b)",
                           {"a": f"a{a}", "b": f"b{b}", "w": weight})
        self.db.ensure_identity_indexes()

    @staticmethod
    def _query(predicate="a.id IN $ids", optional=False):
        return ("OPTIONAL " if optional else "") + (
            "MATCH (a:A)-[r:R]->(b:B) WHERE " + predicate +
            " RETURN a.id,b.id,r.weight")

    @staticmethod
    def _query_outcome(handle, query, parameters):
        try:
            result = handle.execute(query, parameters)
            return "rows", result.columns, result.rows
        except errors.GrafxError as error:
            return "error", type(error), error.code, error.details

    def _compare(self, query, ids):
        with self.db.begin("read") as tx:
            candidate = self._query_outcome(tx, query, {"ids": ids})
            with patch.object(indexes.IndexManager, "validated_versions_many", None):
                oracle = self._query_outcome(tx, query, {"ids": ids})
        self.assertEqual(candidate, oracle)
        return candidate

    def _seeks(self, query):
        return [n for n in self.db.explain(query).walk() if type(n) is plan.NodeMultiKeySeek]

    def test_connected_1024_128_1151_eight_keys(self):
        """ts_35673f7d: exact fixture, membership and edge multiplicity."""
        for query, size in (("MATCH (a:A) RETURN count(a)", 1024),
                            ("MATCH (b:B) RETURN count(b)", 128),
                            ("MATCH (:A)-[r:R]->(:B) RETURN count(r)", 1151)):
            self.assertEqual(self.db.execute(query).rows, ((size,),))
        query = self._query()
        self.assertEqual(len(self._seeks(query)), 1)
        ids = [f"a{i}" for i in range(8)]
        with self.db.begin("read") as tx:
            candidate = tx.execute(query, {"ids": ids})
            with patch.object(indexes.IndexManager, "validated_versions_many", None):
                oracle = tx.execute(query, {"ids": ids})
        self.assertEqual(candidate.rows, oracle.rows)
        self.assertCountEqual(candidate.rows,
                              [(f"a{a}", f"b{b}", w) for a, b, w in self.edges if a < 8])
        # rows_scanned also counts destination landings, not only anchor scans.
        self.assertLess(candidate.statistics.get("rows_scanned", 0), 1024)
        self.assertGreater(candidate.statistics.get("rows_seeked", 0), 0)
        self.assertGreaterEqual(oracle.statistics.get("rows_scanned", 0), 1024)
        # RF04 may change edge strategy; this assertion qualifies only the anchor.
        self.assertGreater(candidate.statistics.get("edge_scans", 0)
                           + candidate.statistics.get("edge_lookups", 0), 0)

    def test_membership_types_and_nonselective_threshold(self):
        """ts_f78cabcb: actual broad-list fallback, canonical errors and sequence."""
        for ids in ([], None, [None], ["a0", "a0"], [True, 1, "a0"], 1, "a0"):
            with self.subTest(ids=ids):
                self._compare(self._query(), ids)
        broad = [f"a{i}" for i in range(1024)]
        result = self.db.execute(self._query(), {"ids": broad})
        self.assertEqual(result.statistics.get("fallback_reason_nonselective_probe_set"), 1)
        self.assertGreaterEqual(result.statistics.get("rows_scanned", 0), self.node_count)
        self.assertEqual(len(result.rows), len(self.edges))
        self._compare(self._query(), broad)

    def test_refusal_order_optional_and_empty_destination(self):
        """ts_5aefe82d: residual reachability and canonical refusal phase."""
        predicates = ("a.id IN $ids AND a.v=1", "a.v=1 AND a.id IN $ids",
                      "a.id IN $ids OR a.v=1", "a.v/0>1 AND a.id IN $ids",
                      "a.id IN $ids AND a.v/0>1")
        for predicate in predicates:
            query = self._query(predicate)
            if predicate != predicates[0]:
                self.assertFalse(self._seeks(query))
            for ids in ([], None, ["a0"], ["missing"]):
                with self.subTest(predicate=predicate, ids=ids):
                    self._compare(query, ids)
        optional = self._query(optional=True)
        self.assertFalse(self._seeks(optional))
        self._compare(optional, ["missing"])
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Empty(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE EmptyRel(FROM A TO Empty)")
        for ids in (1, [], ["a0"]):
            query = "MATCH (a:A)-[:EmptyRel]->(b:Empty) WHERE a.id IN $ids RETURN a.id"
            self._compare(query, ids)


if __name__ == "__main__":
    unittest.main()
