"""Offline harness checks, not Neo4j engine qualification."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("runner", Path(__file__).with_name("fp_neo4j_reference.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ReferenceHarnessTests(unittest.TestCase):
    def test_original_queries_and_expected_answers_are_unchanged(self):
        original = runner.shared.scenarios()
        adapted = runner.manifest()["cases"]
        self.assertEqual(len(original), 16)
        self.assertEqual(len(adapted), len(original))
        for case, result in zip(original, adapted, strict=True):
            self.assertEqual(case["id"], result["id"])
            self.assertEqual(case["query"], result["query"])
            if "error_contains" in case:
                self.assertEqual(result["expected"], {"error_contains": case["error_contains"]})
            else:
                self.assertEqual(result["expected"], {"columns": case["columns"],
                                                     "rows": runner.shared.tagged(case["rows"])})
            if "after" in case:
                self.assertEqual(result["after_query"], case["after"][0])
                self.assertEqual(result["after_expected"]["rows"], runner.shared.tagged(case["after"][2]))

    def test_schema_difference_does_not_rewrite_null_key_expected_failure(self):
        case = next(item for item in runner.manifest()["cases"] if item["id"] == "null-primary-key-atomicity")
        self.assertIn("REQUIRE n.id IS UNIQUE", case["schema"][0])
        self.assertEqual(case["expected"], {"error_contains": "null"})
        self.assertIn("not equivalent", " ".join(case["adaptations"]))

    def test_unknown_schema_never_silently_drops_a_constraint(self):
        with self.assertRaisesRegex(AssertionError, "Unreviewed schema"):
            runner.normalized_case({"id": "new", "schema": ["UNKNOWN SCHEMA"],
                                    "query": "RETURN 1", "columns": ["1"], "rows": [[1]]})

    def test_nested_and_decimal_not_coerced_for_reference(self):
        for case in runner.manifest()["cases"]:
            if case["id"] == "decimal-storage":
                self.assertIn("decimal('1.2500',12,4)", case["setup"][0])
                self.assertEqual(case["expected"]["rows"]["value"][0]["value"][0],
                                 {"type": "decimal", "value": "1.2500"})
            if case["id"] == "nested-storage":
                self.assertIn("items:[{num:1},{num:2}]", case["setup"][0])

    def test_native_date_preserves_type_and_coordinates(self):
        from neo4j.time import Date
        self.assertEqual(runner.tagged([[Date(2024, 2, 29)]]),
                         runner.shared.tagged([[runner.shared.date(2024, 2, 29)]]))
        self.assertNotEqual(runner.tagged(Date(2024, 2, 29)), runner.tagged("2024-02-29"))

    def test_bool_integer_and_duplicate_row_identity_not_normalized_away(self):
        self.assertNotEqual(runner.tagged([[True]]), runner.tagged([[1]]))
        self.assertNotEqual(runner.tagged([[1], [1]]), runner.tagged([[1]]))
        self.assertFalse(runner.equal({"columns": ["+(1,2)"], "rows": runner.tagged([[3]])},
                                      {"columns": ["1 + 2"], "rows": runner.tagged([[3]])}))

    def test_nonsemantic_failures_are_not_differences(self):
        from neo4j.exceptions import Neo4jError, ServiceUnavailable
        for exc in (ServiceUnavailable("offline"), ValueError("decoder"),
                    Neo4jError.hydrate(code="Neo.ClientError.Security.Unauthorized", message="denied"),
                    Neo4jError.hydrate(code="Neo.TransientError.General.MemoryPoolOutOfMemoryError", message="OOM")):
            self.assertFalse(runner.semantic_error(exc))
        exc = Neo4jError.hydrate(code="Neo.ClientError.Statement.SyntaxError", message="syntax")
        self.assertTrue(runner.semantic_error(exc))

    def test_setup_semantic_refusal_is_observed_not_counted_unavailable(self):
        from neo4j.exceptions import Neo4jError
        case = next(item for item in runner.manifest()["cases"] if item["id"] == "nested-storage")
        exc = Neo4jError.hydrate(code="Neo.ClientError.Statement.TypeError", message="cannot store map")
        with patch.object(runner, "execute", side_effect=[{}, exc,
                          {"columns": ["total"], "rows": runner.tagged([[0]])}]):
            result = runner.observe(None, case)
        self.assertEqual(result["comparison"], "differs")
        self.assertEqual(result["failed_phase"], "setup")
        self.assertEqual(result["setup_nodes"]["rows"], runner.tagged([[0]]))
        self.assertNotIn("observed", result)

    def test_transport_failure_is_unavailable_not_semantic_refusal(self):
        from neo4j.exceptions import ServiceUnavailable
        case = runner.manifest()["cases"][0]
        with patch.object(runner, "execute", side_effect=ServiceUnavailable("offline")):
            result = runner.observe(None, case)
        self.assertEqual(result["comparison"], "unavailable")

    def test_success_does_not_skip_post_statement_effects(self):
        case = next(item for item in runner.manifest()["cases"] if item["id"] == "multiple-labels")
        responses = [{}] * (len(case["schema"]) + len(case["setup"]))
        responses.extend([case["expected"], {"columns": ["id"], "rows": runner.tagged([[1], [2]])}])
        with patch.object(runner, "execute", side_effect=responses):
            result = runner.observe(None, case)
        self.assertEqual(result["comparison"], "differs")
        self.assertIn("after", result)

    def test_container_rejects_unowned_bind_remote_and_unpinned(self):
        cid = "a" * 64
        run_id = "fp-" + "b" * 32
        valid = {"Id": cid, "State": {"Running": True}, "Created": "test", "Image": "sha256:abc",
                 "Config": {"Labels": {runner.LABEL: run_id}, "Image": runner.IMAGE,
                            "Env": ["NEO4J_AUTH=none"]}, "Mounts": [],
                 "HostConfig": {"NetworkMode": "bridge", "Memory": 1024**3, "NanoCpus": 10**9},
                 "NetworkSettings": {"Ports": {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17687"}],
                                                "7474/tcp": None}}}
        image = {"Id": "sha256:abc", "Os": "linux", "Architecture": "amd64",
                 "RepoDigests": [runner.IMAGE]}
        with patch.object(runner, "docker_json", side_effect=[[valid], [image]]):
            self.assertEqual(runner.guard_container(cid, run_id)["container_id"], cid)
        for mutation in ("owner", "bind", "remote", "image", "stopped"):
            changed = json.loads(json.dumps(valid))
            if mutation == "owner":
                changed["Config"]["Labels"][runner.LABEL] = "somebody-else"
            elif mutation == "bind":
                changed["Mounts"] = [{"Type": "bind", "Source": "C:/production"}]
            elif mutation == "remote":
                changed["NetworkSettings"]["Ports"]["7687/tcp"][0]["HostIp"] = "0.0.0.0"
            elif mutation == "image":
                changed["Config"]["Image"] = "neo4j:latest"
            else:
                changed["State"]["Running"] = False
            with patch.object(runner, "docker_json", return_value=[changed]):
                with self.assertRaises(AssertionError, msg=mutation):
                    runner.guard_container(cid, run_id)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReferenceHarnessTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
