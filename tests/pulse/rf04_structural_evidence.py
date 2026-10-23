"""Pulse unittest selectors for RF04's structural, non-temporal qualification.

Each invocation reconstructs four arms from a committed catalog of exact Git
blobs inside Pulse's immutable source snapshot, then runs the complete eight
case matrix in separate interpreters. The methods deliberately do not certify
latency or the historical temporal thresholds.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bench.harness.rf04_structural_evidence import REVISIONS, materialize_bundle, run_four_arm


_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "bench/evidence/rf04-source-bundle-1b8e529.json"


def run_immutable_rf04() -> dict:
    """Construct temporary source arms from the verified raw blob catalog."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="grafx-rf04-pulse-") as temporary:
        boundary = Path(temporary).resolve(strict=True)
        roots = materialize_bundle(_ROOT, boundary, manifest)
        return run_four_arm(roots, source_manifest=_MANIFEST)


class RF04StructuralEvidence(unittest.TestCase):
    """One observation shared when both selectors run in the same process."""

    @classmethod
    def setUpClass(cls):
        cls.evidence = run_immutable_rf04()

    def test_rf04_reproducible_structural_qualification(self):
        """ts_0acc8871: equal source oracles, physical work and common quota."""
        report = self.evidence["report"]
        self.assertEqual(report["decision"], "structural_pass")
        self.assertFalse(report["latency_certified"])
        self.assertEqual(report["source_isolation"]["revisions"], REVISIONS)
        self.assertEqual(len(self.evidence["observations"]), 4)
        for arm, observation in self.evidence["observations"].items():
            with self.subTest(arm=arm):
                self.assertEqual(observation["revision"], REVISIONS[arm])
                self.assertEqual(observation["query_memory_budget_bytes"], 1 << 20)
                self.assertEqual(len(observation["cases"]), 8)
                for case in observation["cases"].values():
                    self.assertLessEqual(case["memory"]["logical_memory_peak_bytes"], 1 << 20)
        for profile, inactive, active in (("with", "base", "rf04_only"),
                                          ("original", "rf03_only", "both")):
            cases = self.evidence["observations"]
            target = f"rf04-{profile}-target"
            self.assertEqual(cases[inactive]["cases"][target]["rows_sha256"],
                             cases[active]["cases"][target]["rows_sha256"])
            self.assertEqual(report["contrasts"][target][active]["edge_lookups"], 16)
            self.assertEqual(report["contrasts"][target][active]["edge_scans"], 0)

    def test_rf04_four_arm_authority_and_hub_tradeoff(self):
        """ts_263e138f: independent switches, skew and real fallback."""
        report = self.evidence["report"]
        self.assertEqual(report["decision"], "structural_pass")
        self.assertFalse(report["latency_certified"])
        self.assertEqual(report["source_isolation"]["changed_files"], {
            "base_to_rf03_only": ["src/okto_grafx/domain/query/planner.py"],
            "base_to_rf04_only": ["src/okto_grafx/engine/query_engine.py"],
            "rf03_only_to_both": ["src/okto_grafx/engine/query_engine.py"],
            "rf04_only_to_both": ["src/okto_grafx/domain/query/planner.py"],
        })
        for profile in ("with", "original"):
            hub = self.evidence["observations"]["both"]["cases"][f"rf04-{profile}-hub"]
            topology = hub["fixture_topology"]
            self.assertLessEqual(topology["hot_starts"] * 100, topology["starts"])
            self.assertGreaterEqual(topology["edges_from_hot_starts"] * 5,
                                    topology["edges"] * 4)
            tradeoff = report["hub_tradeoffs"][profile]
            self.assertEqual(tradeoff["rf04_increases_physical_edge_work"],
                             tradeoff["with_rf04_edge_versions"]
                             > tradeoff["without_rf04_edge_versions"])
            self.assertTrue(tradeoff["rf04_increases_physical_edge_work"])
        self.assertEqual(len(self.evidence["candidate_functional_guards"]["passed_methods"]), 3)


if __name__ == "__main__":
    unittest.main()
