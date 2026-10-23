"""Tamper checks for the RF04 non-temporal four-arm certificate."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from bench.harness.rf04_structural_evidence import CASES, PROFILES, REVISIONS, evaluate


def _four_arm_observations():
    arms = {}
    for arm, revision in REVISIONS.items():
        cases = {}
        for profile in PROFILES:
            for case in CASES:
                active = arm == "both" or (profile == "with" and arm == "rf04_only")
                lookups = (64 if case == "fan_limit" else 16) if active else 0
                scans = 1 if not active or case == "fan_limit" else 0
                stats = {"edge_lookups": lookups, "edge_scans": scans,
                         "physical_edge_versions_examined": 64 if active else 1151,
                         "physical_node_versions_examined": 32}
                if case == "fallback":
                    stats["fallback_reason_nonselective_probe_set"] = 1
                cases[f"rf04-{profile}-{case}"] = {
                    "rows": 32, "rows_sha256": "rows", "query_sha256": profile,
                    "fixture_sha256": case, "statistics": stats,
                    "plan": ["NodeMultiKeySeek"] if case == "fallback" else [],
                    "memory": {"logical_memory_peak_bytes": 40960,
                               "baseline_retained_bytes": 0, "coverage": "query_budget_v1",
                               "unavailable_reason": None},
                }
        arms[arm] = {"arm": arm, "revision": revision,
                     "query_memory_budget_bytes": 1 << 20, "cases": cases}
    return arms


def test_structural_certificate_accepts_equal_rows_and_bounded_access():
    report = evaluate(_four_arm_observations(), {"verified": True})
    assert report["decision"] == "structural_pass"
    assert report["latency_certified"] is False
    assert set(report["arm_hashes"]) == set(REVISIONS)


@pytest.mark.parametrize("arm,case,field,value,error", [
    ("rf03_only", "rf04-original-target", "rows_sha256", "changed", "semantic"),
    ("both", "rf04-with-target", "query_sha256", "changed", "semantic"),
    ("both", "rf04-original-target", "physical_edge_versions_examined", 2000,
     "endpoint contrast"),
    ("rf04_only", "rf04-with-fan_limit", "edge_lookups", 65, "fan-limit"),
    ("both", "rf04-original-hub", "edge_scans", 1, "skewed hub"),
    ("base", "rf04-with-fallback", "fallback_reason_nonselective_probe_set", 0,
     "broad conditional"),
])
def test_certificate_rejects_semantic_and_physical_tampering(arm, case, field, value, error):
    arms = _four_arm_observations()
    item = arms[arm]["cases"][case]
    (item if field in item else item["statistics"])[field] = value
    with pytest.raises(ValueError, match=error):
        evaluate(arms, {"verified": True})


def test_certificate_requires_memory_observation_within_shared_quota():
    arms = deepcopy(_four_arm_observations())
    arms["both"]["cases"]["rf04-original-target"]["memory"]["logical_memory_peak_bytes"] = (1 << 20) + 1
    with pytest.raises(ValueError, match="logical memory"):
        evaluate(arms, {"verified": True})


def test_certificate_requires_all_four_exact_revisions():
    arms = _four_arm_observations()
    arms["rf04_only"]["revision"] = "0" * 40
    with pytest.raises(ValueError, match="invalid source"):
        evaluate(arms, {"verified": True})


def test_recorded_four_arm_evidence_replays_and_keeps_candidate_guard_source():
    root = Path(__file__).resolve().parents[2]
    record = json.loads((root / "bench/evidence/rf04-structural-1b8e529.json").read_text(
        encoding="utf-8"))
    assert evaluate(record["observations"], record["report"]["source_isolation"]) == record["report"]
    guard = record["candidate_functional_guards"]
    module = root / guard["module"]
    assert hashlib.sha256(module.read_bytes()).hexdigest() == guard["module_sha256"]
    assert guard["revision"] == REVISIONS["both"]
    assert len(guard["passed_methods"]) == 3
    assert record["report"]["latency_certified"] is False
