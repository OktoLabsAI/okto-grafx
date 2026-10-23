"""Native unittest entry points for RF01 immutable-snapshot runners.

These four methods establish functional contracts. Synthetic campaign records
exercise validation only; passing them is NOT temporal overhead qualification.
Native fixtures explicitly use pure codec/vector adapters in every metrics mode and reopen,
so the Pulse Python runner does not require NumPy. Production defaults remain
unchanged; this wrapper does not qualify the NumPy codec's performance.
"""
import copy
import hashlib
from importlib import import_module
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "src"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SOURCE))
grafx = import_module("okto_grafx")
Path(grafx.__file__).resolve().relative_to(_SOURCE.resolve())
fixtures = import_module("bench.harness.campaign_fixtures")
worker = import_module("bench.harness.campaign_worker")
schema = import_module("bench.harness.campaign_schema")
for _module in (fixtures, worker, schema):
    Path(_module.__file__).resolve().relative_to(_ROOT.resolve())
qe = import_module("okto_grafx.engine.query_engine")
IndexManager = import_module("okto_grafx.engine.index_manager").IndexManager


class _CompatibilitySink:
    """Exercise the real active adapter, with failures at recording boundaries."""

    enabled = True

    def __init__(self, inject=False):
        self.inner = import_module("okto_grafx.adapters.metrics_openmetrics").OpenMetricsSink()
        self.inject = inject
        self.armed = False
        self.calls = []
        self.failures = []
        self.adapter_errors = []

    def register(self, descriptor):
        self.inner.register(descriptor)

    def _record(self, door, name, value, labels):
        self.calls.append((door, name))
        # First validate against the genuine frozen-catalog adapter. Its errors
        # must not be mistaken for the deliberately injected host exception.
        try:
            getattr(self.inner, door)(name, value, labels)
        except BaseException as exc:
            self.adapter_errors.append(type(exc).__name__)
            raise
        if self.armed and self.inject and (
            name.startswith("oktografx_query_")
            or (door == "set_gauge" and name == "oktografx_active_transactions" and value == 0)
        ):
            self.failures.append((door, name))
            raise RuntimeError("RF01 injected recording failure")

    def increment(self, name, value=1.0, labels=None):
        self._record("increment", name, value, labels)

    def observe(self, name, value, labels=None):
        self._record("observe", name, value, labels)

    def set_gauge(self, name, value, labels=None):
        self._record("set_gauge", name, value, labels)

    def time(self, name, labels=None):
        return self.inner.time(name, labels)

    def snapshot(self):
        return self.inner.snapshot()


def _synthetic_campaign():
    """Arithmetic/schema fixture; never a measurement of this machine or revision."""
    scenario = fixtures.Scenario(name="constant", family="constant", nodes=4).to_dict()
    runs = []
    for pair, order in enumerate(("AB", "BA", "AB")):
        for arm in order:
            samples = [worker.sample_record(
                scenario, index, ((1,),), {"rows_returned": 1},
                elapsed_ns=100, cpu_ns=10, prepare_ns=2,
                memory={"logical_memory_peak_bytes": 0, "baseline_retained_bytes": 0,
                        "coverage": "instrumented budgets", "unavailable_reason": None},
                rss_peak_bytes=100) for index in range(25)]
            runs.append({"arm": arm, "pair": pair, "run_order": order,
                         "process_id": 100 + len(runs), "invocation_id": str(len(runs)),
                         "revision": ("a" if arm == "A" else "b") * 40,
                         "source_clean": True, "package_file": "/snapshot/src/okto_grafx/__init__.py",
                         "source_root": "/snapshot", "environment": {
                             "python": "3.11", "dependencies": {}, "os": "test", "cpu": "test",
                             "cpu_count": 1, "ram_bytes": 1, "disk": {"free_bytes": 1}, "gc_enabled": True},
                         "cpu_before_percent": 0, "cpu_after_percent": 0,
                         "metrics_sink": "NoOpMetricsSink", "samples": samples})
    data = {"schema_version": "grafx-campaign/v1", "mode": "noop", "intent": "qualification",
            "started_at": "2026-10-21T10:00:00+00:00", "calibrated_executor": True,
            "command": ["python", "-m", "bench.harness.campaign"],
            "protocol": {"pairs": 3, "warmups": 5, "retained": 20, "orders": ["AB", "BA", "AB"],
                         "attempt": 1, "previous_campaign_sha256": None, "max_cpu_percent": 20},
            "scenarios": [scenario], "runs": runs}
    environment = {key: runs[0]["environment"][key] for key in
                   ("python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "gc_enabled")}
    sources = {name: hashlib.sha256((_ROOT / "bench" / "harness" / name).read_bytes()).hexdigest()
               for name in ("campaign.py", "campaign_schema.py", "campaign_worker.py")}
    raw = json.dumps({"schema_version": "grafx-executor-calibration/v1", "observed_at": data["started_at"],
                      "environment_hash": schema.canonical_hash(environment),
                      "environment": environment, "warmup_ns": [100] * 5, "producer_process_id": 123,
                      "clock": {"name": "perf_counter_ns", "resolution_ns": 100},
                      "procedure": {"id": "sha256-fixed-payload-v1",
                                    "command": ["synthetic-test-python", "-m", "bench.harness.campaign", "calibrate"],
                                    "source_sha256": sources["campaign.py"], "source_files": sources,
                                    "control": {"algorithm": "sha256", "payload_bytes": 65536, "iterations_per_sample": 128}},
                      "elapsed_ns": [100] * 20, "cpu_percent": [0] * 5})
    data["calibration"] = {"raw_json": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "path": "test-only"}
    return data


class RF01Evidence(unittest.TestCase):
    def test_physical_scan(self):
        """Logical scan count differs from physical versions, including invisible history."""
        from tests.query.stack import build_query_stack

        stack = build_query_stack()
        for i in range(1, 4):
            stack.insert("Person", i, (i, f"p{i}", 20 + i, "SP"))
        ended = stack.insert("Knows", 1, (1, 2, 2001))
        stack.insert("Knows", 2, (1, 3, 2002))
        stack.insert("Knows", 3, (2, 3, 2003))
        stack.end("Knows", ended, (1, 2, 2001), csn=2)
        query = "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id, b.id"
        for _ in range(2):
            result = stack.engine.execute(query, stack.transaction())
            self.assertEqual(sorted(result.rows), [(1, 3), (2, 3)])
            self.assertEqual(result.statistics["edge_scans"], 1)
            self.assertEqual(result.statistics["physical_edge_versions_examined"], 3)
        zero = stack.engine.execute("MATCH (p:Person) RETURN p.id LIMIT 0", stack.transaction())
        self.assertEqual(zero.rows, ())
        self.assertNotIn("physical_node_versions_examined", zero.statistics)

    def test_fallback_regimes(self):
        """Absent/stale authorities preserve results; unused decisions remain absent."""
        query = "MATCH (n:A) WHERE n.id IN $ids RETURN n.id, n.v"
        def reasons(result):
            return {k: v for k, v in result.statistics.items() if k.startswith("fallback_reason_")}
        with TemporaryDirectory(prefix="grafx-rf01-fallback-") as root, grafx.connect(Path(root) / "db", codec="pure", vector_math="pure") as db:
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE A(id STRING, v INT64, PRIMARY KEY(id))")
                for number in range(1, 6):
                    tx.execute("CREATE (:A {id:$id, v:$v})", {"id": f"a{number}", "v": number})
            db.ensure_identity_indexes()
            self.assertTrue(any(type(n).__name__ == "NodeScan" for n in db.explain(query).walk()))
            fast = db.execute(query, {"ids": ["a1"]})
            self.assertEqual(fast.rows, (("a1", 1),))
            self.assertEqual(reasons(fast), {})
            with patch.object(IndexManager, "validated_versions_many", None):
                scan = db.execute(query, {"ids": ["a1"]})
                self.assertEqual(scan.rows, fast.rows)
                self.assertEqual(reasons(scan), {"fallback_reason_index_unavailable": 1})
                self.assertEqual(reasons(db.execute(query + " LIMIT 0", {"ids": ["a1"]})), {})
            original = qe._IndexAuthorityProjection.named
            class Stale:
                stale = True
                def __init__(self, store):
                    self.store = store
                def __getattr__(self, name):
                    return getattr(self.store, name)
            def named(projection, name):
                store = original(projection, name)
                return None if store is None else Stale(store)
            with patch.object(qe._IndexAuthorityProjection, "named", named):
                stale = db.execute(query, {"ids": ["a1"]})
                self.assertEqual(stale.rows, fast.rows)
                self.assertEqual(reasons(stale), {"fallback_reason_index_stale": 1})
            self.assertEqual(reasons(db.execute(query, {"ids": ["a1"]})), {})
            self.assertEqual(db.transactions.open_transactions, 0)

    def test_compatibility_functional(self):
        """Functional-only entry: overhead qualification remains pending."""
        # Identical operations through the supported composition boundary. This
        # is semantic evidence for ts_16741f1a, never timing qualification.
        catalog = import_module("okto_grafx.engine.metrics_catalog")
        catalog_before = catalog.METRIC_CATALOG
        with TemporaryDirectory(prefix="grafx-rf01-sink-matrix-") as directory:
            outcomes = {}
            for mode in ("noop", "active", "injected"):
                with self.subTest(metrics=mode):
                    outcomes[mode] = self._metrics_compatibility_case(Path(directory) / mode, mode)
            self.assertEqual(outcomes["active"], outcomes["noop"])
            self.assertEqual(outcomes["injected"], outcomes["noop"])
        self.assertEqual(catalog.METRIC_CATALOG, catalog_before)
        scenario = fixtures.Scenario(name="cursor", family="node_scan", nodes=8, cursor_batch_size=1)
        with TemporaryDirectory(prefix="grafx-rf01-functional-") as root:
            with grafx.connect(Path(root) / "read", codec="pure", vector_math="pure") as db:
                fixtures.build_fixture(db, scenario)
                sample = worker.measure_statement(db, scenario, 0)
                self.assertEqual(sample["result_hash"], sample["expected_result_hash"])
                self.assertEqual(sample["rows"], len(fixtures.oracle(scenario)))
                self.assertLessEqual(sample["first_row_ns"], sample["consume_all_ns"])
                token = grafx.CancellationToken()
                with db.query("MATCH (n:N) RETURN n.id").cursor(batch_size=1, cancellation=token) as cursor:
                    self.assertIsNotNone(cursor.fetchone())
                    token.cancel()
                    with self.assertRaises(grafx.errors.GrafxQueryCancelled):
                        cursor.fetchone()
                    self.assertTrue(cursor.closed)
                self.assertEqual(db.transactions.open_transactions, 0)
            write = fixtures.Scenario(name="write", family="write", nodes=4, write_rows=2)
            with grafx.connect(Path(root) / "write", codec="pure", vector_math="pure") as db:
                fixtures.build_fixture(db, write)
                sample = worker.measure_statement(db, write, 0)
                self.assertEqual(sample["result_hash"], sample["expected_result_hash"])
                self.assertIsNotNone(sample["commit_ns"])
            with grafx.connect(Path(root) / "write", codec="pure", vector_math="pure") as db:
                self.assertEqual(db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows, fixtures.oracle(write))
        # Even complete modeled timings cannot qualify without executor proof.
        # Real query success above supplies functionality, never overhead proof.
        pending = _synthetic_campaign()
        del pending["calibration"]
        report = schema.compare_campaign(pending)
        self.assertEqual(report["decision"], "inconclusive", "Overhead must remain pending")
        self.assertNotEqual(report["decision"], "passed")

    def _metrics_compatibility_case(self, path, mode):
        sink = None if mode == "noop" else _CompatibilitySink(inject=mode == "injected")
        kwargs = {"codec": "pure", "vector_math": "pure"}
        if sink is not None:
            bootstrap = import_module("okto_grafx.runtime.bootstrap")
            registry = bootstrap.build_default_registry(grafx.DatabaseConfig(path=str(path), codec="pure", vector_math="pure"))
            registry.bind("metrics", sink)
            kwargs["registry"] = registry
        outcome = {"reads": [], "errors": []}
        with grafx.connect(path, **kwargs) as db:
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(id INT64,value INT64,PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE E(FROM N TO N)")
                tx.execute("UNWIND [1,2,3] AS i CREATE (:N {id:i,value:i})")
                tx.execute("MATCH (a:N {id:1}),(b:N {id:2}) CREATE (a)-[:E]->(b)")
                tx.execute("MATCH (a:N {id:1}),(b:N {id:3}) CREATE (a)-[:E]->(b)")
            if sink is not None:
                sink.armed = True
            for query, parameters in (
                ("MATCH (n:N {id:$id}) RETURN n.id,n.value", {"id": 2}),
                ("MATCH (a:N)-[:E]->(b:N) RETURN a.id,b.id ORDER BY a.id,b.id", {}),
                ("UNWIND [1,1,3] AS i MATCH (n:N {id:i}) RETURN n.id ORDER BY n.id", {}),
            ):
                result = db.execute(query, parameters)
                outcome["reads"].append((result.rows, dict(result.statistics)))
            self.assertEqual(outcome["reads"][0][0], ((2, 2),))
            self.assertEqual(outcome["reads"][1][0], ((1, 2), (1, 3)))
            self.assertEqual(outcome["reads"][2][0], ((1,), (1,), (3,)))
            self.assertEqual(outcome["reads"][1][1]["edge_scans"], 1)
            self.assertEqual(outcome["reads"][1][1]["physical_edge_versions_examined"], 2)
            with self.assertRaises(grafx.errors.GrafxError) as read_error:
                db.execute("MATCH (n:N) RETURN 1 / $zero", {"zero": 0})
            outcome["errors"].append((type(read_error.exception).__name__, read_error.exception.code))
            with self.assertRaises(grafx.errors.GrafxError) as write_error:
                with db.begin("write") as tx:
                    tx.execute("UNWIND [10,10] AS i CREATE (:N {id:i,value:10})")
            outcome["errors"].append((type(write_error.exception).__name__, write_error.exception.code))
            self.assertEqual(db.execute("MATCH (n:N {id:10}) RETURN n.id").rows, ())
            token = grafx.CancellationToken()
            with db.query("MATCH (n:N) RETURN n.id ORDER BY n.id").cursor(batch_size=1, cancellation=token) as cursor:
                self.assertEqual(next(cursor), (1,))
                token.cancel()
                with self.assertRaises(grafx.errors.GrafxQueryCancelled) as cancelled:
                    next(cursor)
                self.assertTrue(cursor.closed)
                outcome["cancelled"] = (cancelled.exception.code, dict(cursor.statistics))
            self.assertEqual(db.transactions.open_transactions, 0)
            before_commit_failures = len(sink.failures) if sink else 0
            with db.begin("write") as tx:
                result = tx.execute("CREATE (:N {id:4,value:40}) RETURN 4")
                outcome["write"] = (result.rows, dict(result.statistics))
            # The application received successful commit ACK despite recording
            # exceptions. Recovery below independently checks durable visibility.
            self.assertEqual(db.transactions.open_transactions, 0)
            if sink is not None:
                self.assertTrue(sink.calls)
                self.assertEqual(sink.adapter_errors, [])
                self.assertLessEqual({name for _, name in sink.calls},
                                     import_module("okto_grafx.engine.metrics_catalog").metric_names())
                if mode == "injected":
                    self.assertGreater(len(sink.failures), before_commit_failures)
                    self.assertTrue(any(name.startswith("oktografx_query_") for _, name in sink.failures))
                    self.assertIn(("set_gauge", "oktografx_active_transactions"),
                                  sink.failures[before_commit_failures:])
                else:
                    self.assertEqual(sink.failures, [])
            self.assertIsNone(db._queries._statement_work.current_statistics())
        with grafx.connect(path, codec="pure", vector_math="pure") as reopened:
            outcome["durable"] = reopened.execute("MATCH (n:N) RETURN n.id,n.value ORDER BY n.id").rows
            self.assertEqual(outcome["durable"], ((1, 1), (2, 2), (3, 3), (4, 40)))
            self.assertFalse(reopened.verify("all").findings)
        return outcome

    def test_campaign_validation(self):
        """Synthetic malformed evidence fails closed before ratio publication."""
        data = _synthetic_campaign()
        report = schema.compare_campaign(data)
        self.assertEqual(report["decision"], "passed")  # schema fixture, NOT measured performance
        self.assertEqual(len(report["comparisons"]), 3)
        self.assertEqual(report["comparisons"][0]["median_ratio"], 1)
        changes = {
            "missing_run": lambda d: d["runs"].pop(),
            "missing_sample": lambda d: d["runs"][0]["samples"].pop(),
            "nan": lambda d: d["runs"][0]["samples"][0].update(elapsed_ns=float("nan")),
            "result_mismatch": lambda d: d["runs"][0]["samples"][0].update(result_hash="0" * 64),
            "query_mismatch": lambda d: d["runs"][0]["samples"][0].update(query_hash="0" * 64),
            "dirty_source": lambda d: d["runs"][0].update(source_clean=False),
            "missing_environment": lambda d: d["runs"][0].pop("environment"),
            "third_attempt": lambda d: d["protocol"].update(attempt=3),
            "calibration_hash": lambda d: d["calibration"].update(sha256="0" * 64),
        }
        for name, change in changes.items():
            with self.subTest(invalid=name):
                invalid = copy.deepcopy(data)
                change(invalid)
                with self.assertRaises(schema.CampaignError):
                    schema.compare_campaign(invalid)
        with self.assertRaises(schema.CampaignError):
            schema.load_json('{"elapsed_ns":1,"elapsed_ns":2}')
        self.assertEqual(len({schema.canonical_hash(x) for x in
                              [((1,),), ((True,),), ((1.0,),), ((1,), (1,))]}), 4)
        self.assertNotEqual(schema.canonical_hash(((1,), (2,))), schema.canonical_hash(((2,), (1,))))
        noisy = copy.deepcopy(data)
        noisy["runs"][0]["cpu_before_percent"] = 21
        self.assertEqual(schema.compare_campaign(noisy)["decision"], "inconclusive")
        noisy["intent"] = "exploratory"
        self.assertEqual(schema.compare_campaign(noisy)["decision"], "exploratory")
        slower = copy.deepcopy(data)
        for sample in slower["runs"][1]["samples"]:
            sample.update(elapsed_ns=104, consume_all_ns=104)
        self.assertEqual(schema.compare_campaign(slower)["decision"], "failed")


if __name__ == "__main__":
    unittest.main()
