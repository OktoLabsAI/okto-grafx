"""RF02 native snapshot assertions; no timing or campaign approval."""

from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

_SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
errors = import_module("okto_grafx.domain.errors")
Path(grafx.__file__).resolve().relative_to(_SOURCE.resolve())
Path(errors.__file__).resolve().relative_to(_SOURCE.resolve())


class UnwindScalarEvidence(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="grafx-rf02-")
        self.addCleanup(temporary.cleanup)
        self.db = grafx.connect(Path(temporary.name) / "db", codec="pure", vector_math="pure")
        self.addCleanup(self.db.close)
        with self.db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id STRING, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Number(id INT64, PRIMARY KEY(id))")
        with self.db.begin("write") as tx:
            tx.execute("UNWIND $rows AS r CREATE (:Item {id:r.id,value:r.value})",
                       {"rows": [{"id": str(i), "value": i} for i in range(1023)]
                        + [{"id": "雪", "value": 1023}]})
            tx.execute("UNWIND $ids AS i CREATE (:Number {id:i})",
                       {"ids": [-(2**63), 1, 2**63 - 1]})

    def _labels(self, query):
        return tuple(n.label for n in self.db.explain(query).walk())

    @staticmethod
    def _query_outcome(handle, query, parameters):
        try:
            result = handle.execute(query, parameters)
            return "rows", result.columns, result.rows
        except errors.GrafxError as error:
            return "error", type(error), error.code, error.details

    def _compare(self, candidate, oracle, parameters, handle=None):
        handle = handle or self.db
        self.assertEqual(self._query_outcome(handle, candidate, parameters),
                         self._query_outcome(handle, oracle, parameters))

    def test_1024_nodes_sixteen_occurrences(self):
        """ts_5344a5f5: logical occurrences and physical memo are distinct."""
        candidate = "UNWIND $ids AS i MATCH (n:Item {id:i}) RETURN n.id"
        oracle = "UNWIND $ids AS i MATCH (n:Item) WHERE n.id=i OR false RETURN n.id"
        self.assertIn("IndexSeek", self._labels(candidate))
        self.assertNotIn("IndexSeek", self._labels(oracle))
        self.assertEqual(self.db.execute("MATCH (n:Item) RETURN count(n)").rows, ((1024,),))
        for distinct in (8, 16):
            ids = [str(i % distinct) for i in range(16)]
            with self.subTest(distinct=distinct), self.db.begin("read") as tx:
                result = tx.execute(candidate, {"ids": ids})
                reference = tx.execute(oracle, {"ids": ids})
                self.assertEqual(result.rows, tuple((key,) for key in ids))
                self.assertEqual(result.rows, reference.rows)
                self.assertEqual(result.statistics.get("rows_seeked"), 16)
                self.assertEqual(result.statistics.get("rows_scanned", 0), 0)
                self.assertEqual(result.statistics.get("index_probe_keys"), distinct)
                self.assertEqual(result.statistics.get("physical_node_versions_examined"), distinct)

    def test_null_types_unicode_and_int64_limits(self):
        """ts_6e8e6b31: compare exact values/types and error code/details."""
        for table, values in (("Item", [[], None, [None], ["missing"], ["雪", "雪"],
                                        [True, 1, 1.0], "bad", 1]),
                              ("Number", [[key, key] for key in
                                          (-(2**63), 2**63 - 1, 1, 1.0, True, False, None, "1")])):
            candidate = f"UNWIND $ids AS i MATCH (n:{table} {{id:i}}) RETURN n.id"
            oracle = f"UNWIND $ids AS i MATCH (n:{table}) WHERE n.id=i OR false RETURN n.id"
            for ids in values:
                with self.subTest(table=table, ids=ids):
                    self._compare(candidate, oracle, {"ids": ids})

    def test_binding_optional_and_pending_write(self):
        """ts_db6bdc45: binding identity, canonical writes and owner visibility."""
        for prefix, key, eligible in (
            ("UNWIND $ids AS i WITH i", "i", True),
            ("UNWIND $ids AS i WITH i AS k", "k", True),
            ("UNWIND $ids AS i WITH $replacement AS i", "i", False),
            ("UNWIND $ids AS i WITH toString(i) AS i", "i", False),
            ("UNWIND $ids AS i WITH count(i) AS i", "i", False),
            ("UNWIND ['0'] AS i WITH i AS old UNWIND $ids AS i", "i", True),
        ):
            candidate = prefix + f" MATCH (n:Item {{id:{key}}}) RETURN n.id"
            oracle = prefix + f" MATCH (n:Item) WHERE n.id={key} OR false RETURN n.id"
            with self.subTest(prefix=prefix):
                self.assertEqual("IndexSeek" in self._labels(candidate), eligible)
                self._compare(candidate, oracle, {"ids": ["0", "雪", "0"], "replacement": "1"})
        candidate = "UNWIND $ids AS i OPTIONAL MATCH (n:Item {id:i}) RETURN i,n.id"
        oracle = "UNWIND $ids AS i OPTIONAL MATCH (n:Item) WHERE n.id=i OR false RETURN i,n.id"
        self._compare(candidate, oracle, {"ids": ["0", "missing", None, "0"]})
        for operation in ("SET n.value=n.value+1 RETURN n.value",
                          "CREATE (:Number {id:n.value+100}) RETURN n.id"):
            candidate = "UNWIND $ids AS i MATCH (n:Item {id:i}) " + operation
            oracle = "UNWIND $ids AS i MATCH (n:Item) WHERE n.id=i OR false " + operation
            self.assertNotIn("IndexSeek", self._labels(candidate))
            observations = []
            for query in (candidate, oracle):
                tx = self.db.begin("write")
                try:
                    observations.append((self._query_outcome(tx, query, {"ids": ["0", "0"]}),
                                         tx.execute("MATCH (n:Item {id:'0'}) RETURN n.value").rows,
                                         tx.execute("MATCH (n:Number) RETURN n.id ORDER BY n.id").rows))
                finally:
                    tx.rollback()
            self.assertEqual(*observations)
        with self.db.begin("write") as tx:
            tx.execute("CREATE (:Item {id:'owned',value:4})")
            tx.execute("MATCH (n:Item {id:'0'}) SET n.value=9")
            self._compare("UNWIND $ids AS i MATCH (n:Item {id:i}) RETURN n.id,n.value",
                          "UNWIND $ids AS i MATCH (n:Item) WHERE n.id=i OR false RETURN n.id,n.value",
                          {"ids": ["owned", "0", "owned"]}, tx)


if __name__ == "__main__":
    unittest.main()
