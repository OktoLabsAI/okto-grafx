"""Native unittest entry points for RF04 immutable-snapshot evidence.

Three native unittest methods for immutable-snapshot RF04 evidence. No timing,
pytest dependency, external checkout import, or assertion of campaign approval.
Local execution is not an authenticated Pulse scenario result.
"""

from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

_SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
qe = import_module("okto_grafx.engine.query_engine")
indexes = import_module("okto_grafx.engine.index_manager")
plan = import_module("okto_grafx.domain.query.plan")
for module in (grafx, qe, indexes, plan):
    Path(module.__file__).resolve().relative_to(_SOURCE.resolve())


class ConditionalEndpointEvidence(unittest.TestCase):
    """One method per RF04 functional scenario; assertions execute real storage."""

    def setUp(self):
        temporary = TemporaryDirectory(prefix="grafx-rf04-")
        self.addCleanup(temporary.cleanup)
        self.db = grafx.connect(
            Path(temporary.name) / "db",
            page_size=8192,
            codec="pure",
            vector_math="pure",
        )
        self.addCleanup(self.db.close)
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM N TO N, weight INT64)")
        with self.db.begin("write") as tx:
            tx.execute(
                "UNWIND $ids AS id CREATE (:N {id:id})", {"ids": list(range(80))}
            )
        self.edges = [(i, (i + 1) % 80, i) for i in range(80)]
        with self.db.begin("write") as tx:
            for source, target, weight in self.edges:
                self._insert(tx, source, target, weight)
        self.db.ensure_identity_indexes()

    @staticmethod
    def _insert(tx, source, target, weight):
        tx.execute(
            "MATCH (a:N {id:$source}),(b:N {id:$target}) "
            "CREATE (a)-[:E {weight:$weight}]->(b)",
            {"source": source, "target": target, "weight": weight},
        )

    @staticmethod
    def _query(direction="outgoing"):
        pattern = {
            "outgoing": "(a)-[r:E]->(b:N)",
            "incoming": "(a)<-[r:E]-(b:N)",
            "undirected": "(a)-[r:E]-(b:N)",
        }[direction]
        return (
            "MATCH (a:N) WHERE a.id IN $ids WITH a MATCH "
            + pattern
            + " RETURN a.id,b.id,r.weight ORDER BY r.weight,a.id,b.id"
        )

    @staticmethod
    def _expected(edges, ids, direction="outgoing"):
        starts = set(ids)
        rows = []
        for source, target, weight in edges:
            if direction != "incoming" and source in starts:
                rows.append((source, target, weight))
            if direction != "outgoing" and target in starts:
                if direction != "undirected" or source != target:
                    rows.append((target, source, weight))
        return tuple(sorted(rows, key=lambda row: (row[2], row[0], row[1])))

    def _compare(self, handle, ids, edges, direction="outgoing"):
        query = self._query(direction)
        result = handle.execute(query, {"ids": ids})
        # Disable the actual endpoint strategy, not merely the conditional node door.
        with patch.object(qe, "_frontier_is_bounded", lambda *_: False):
            oracle = handle.execute(query, {"ids": ids})
        self.assertEqual(result.columns, oracle.columns)
        self.assertEqual(result.rows, oracle.rows)
        self.assertEqual(result.rows, self._expected(edges, ids, direction))
        self.assertEqual(oracle.statistics.get("edge_scans"), 1)
        self.assertEqual(oracle.statistics.get("edge_lookups", 0), 0)
        return result, oracle

    def test_sixteen_starts_without_global_edge_census(self):
        """ts_e5ac4444: physical and semantic oracle for sixteen indexed starts."""
        with self.db.begin("read") as tx:
            result, oracle = self._compare(tx, list(range(16)), self.edges)
        self.assertTrue(
            any(type(node) is plan.NodeMultiKeySeek for node in result.plan.walk())
        )
        self.assertEqual(result.statistics.get("edge_lookups"), 16)
        self.assertEqual(result.statistics.get("edge_scans", 0), 0)
        self.assertGreater(
            result.statistics.get("physical_edge_versions_examined", 0), 0
        )
        self.assertGreaterEqual(
            oracle.statistics.get("physical_edge_versions_examined", 0), 80
        )
        self.assertLess(
            result.statistics["physical_edge_versions_examined"],
            oracle.statistics["physical_edge_versions_examined"],
        )

    def test_fan_limit_and_actual_conditional_node_fallback(self):
        """ts_5fddacca: boundaries, no replay, duplicate input, actual node scan."""
        for size in (16, 64, 65, 75):
            with self.subTest(size=size), self.db.begin("read") as tx:
                result, _ = self._compare(tx, list(range(size)) + [0, 0], self.edges)
                self.assertEqual(len(result.rows), size)
                self.assertEqual(result.statistics.get("edge_lookups"), min(size, 64))
                self.assertEqual(result.statistics.get("edge_scans", 0), int(size > 64))
        with self.db.begin("read") as tx:
            with patch.object(indexes.IndexManager, "validated_versions_many", None):
                result = tx.execute(self._query(), {"ids": list(range(16))})
        self.assertEqual(result.rows, self._expected(self.edges, range(16)))
        self.assertEqual(result.statistics.get("rows_seeked", 0), 0)
        self.assertGreaterEqual(result.statistics.get("rows_scanned", 0), 80)
        self.assertEqual(result.statistics.get("fallback_reason_index_unavailable"), 1)
        # A real node scan can still feed the bounded endpoint hybrid: its cost is explicit.
        self.assertEqual(result.statistics.get("edge_lookups"), 16)

    def test_directions_parallel_self_loop_and_owned_overlay(self):
        """ts_c4cd7b4c: stable snapshot and owner versions, including rollback."""
        self.edges.extend([(1, 1, 100), (1, 2, 101), (2, 1, 102)])
        with self.db.begin("write") as tx:
            for edge in self.edges[-3:]:
                self._insert(tx, *edge)
        for direction in ("outgoing", "incoming", "undirected"):
            with self.subTest(direction=direction), self.db.begin("read") as tx:
                self._compare(tx, [1, 1, 999], self.edges, direction)
            for operation in ("create", "delete", "update"):
                with self.subTest(direction=direction, operation=operation):
                    expected = list(self.edges)
                    tx = self.db.begin("write")
                    try:
                        selected = 102 if direction == "incoming" else 101
                        if operation == "create":
                            edge = (
                                (3, 1, 103) if direction == "incoming" else (1, 3, 103)
                            )
                            self._insert(tx, *edge)
                            expected.append(edge)
                        elif operation == "delete":
                            tx.execute(
                                "MATCH (:N)-[r:E]->(:N) WHERE r.weight=$w DELETE r",
                                {"w": selected},
                            )
                            expected = [
                                edge for edge in expected if edge[2] != selected
                            ]
                        else:
                            tx.execute(
                                "MATCH (:N)-[r:E]->(:N) WHERE r.weight=$w SET r.weight=104",
                                {"w": selected},
                            )
                            expected = [
                                (a, b, 104 if w == selected else w)
                                for a, b, w in expected
                            ]
                        self._compare(tx, [1, 1, 999], expected, direction)
                    finally:
                        if tx.active:
                            tx.rollback()
                    self.assertEqual(
                        self.db.execute(self._query(direction), {"ids": [1]}).rows,
                        self._expected(self.edges, [1], direction),
                    )


if __name__ == "__main__":
    unittest.main()
