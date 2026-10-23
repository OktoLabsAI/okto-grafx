"""Fast contract tests; the actual campaigns run only through explicit Pulse selectors."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from bench.harness.campaign_fixtures import matrix
from bench.harness.campaign_schema import validate_fixed_optimization_matrix
from bench.harness.rf0208_operational import (DEFAULT_BUDGET, PARTITIONS, REPORT_SCHEMA,
                                              RF04_PHASES, SCHEMA, _command,
                                              expected_scenarios, validate_manifest,
                                              validate_report_manifest)


def manifest(partition):
    budget = None if partition in ("RF06", "RF08") else DEFAULT_BUDGET
    return {"schema_version": SCHEMA, "partition": partition,
            "runner_root": "C:/runner", "runner_revision": "b" * 40,
            "baseline_root": "C:/baseline", "baseline_revision": "a" * 40,
            "candidate_root": "C:/candidate", "candidate_revision": "b" * 40,
            "python": "C:/python.exe", "output": "C:/runs/one",
            "timeout_seconds": 3500, "memory_budget_bytes": budget,
            "scenarios": [s.name for s in expected_scenarios(partition, budget)],
            "attempt": 1, "previous_output": None, "previous_campaign_sha256": None}


def test_all_partitions_have_fixed_unique_matrix_and_closed_manifest():
    assert [len(expected_scenarios(name, None if name in ("RF06", "RF08") else DEFAULT_BUDGET))
            for name in PARTITIONS] == [10, 11, 4, 4, 4, 4, 6, 6, 3]
    for partition in PARTITIONS:
        candidate = manifest(partition)
        assert len(candidate["scenarios"]) == len(set(candidate["scenarios"]))
        validate_manifest(candidate, partition)


@pytest.mark.parametrize("field,value", [
    ("timeout_seconds", 3600), ("attempt", 2),
    ("candidate_revision", "a" * 40), ("scenarios", ["rf02-empty"]),
    ("memory_budget_bytes", None), ("runner_revision", "short"),
])
def test_manifest_rejects_unbounded_or_partial_qualification(field, value):
    candidate = deepcopy(manifest("RF02"))
    candidate[field] = value
    with pytest.raises(ValueError):
        validate_manifest(candidate, "RF02")


def test_rf04_requires_current_combined_arm_but_allows_isolated_source():
    isolated = manifest("RF04-RF03")
    isolated["candidate_revision"] = "c" * 40
    validate_manifest(isolated, "RF04-RF03")
    combined = manifest("RF04-COMBINED")
    combined["candidate_revision"] = "c" * 40
    with pytest.raises(ValueError, match="combined arm"):
        validate_manifest(combined, "RF04-COMBINED")


def test_cli_contains_exact_regime_and_target_overrides(tmp_path):
    flags = {name: _command(tmp_path, tmp_path, manifest(name), None)[1] for name in PARTITIONS}
    assert "--rf06-split-memory" in flags["RF06"]
    assert "--memory-budget" not in flags["RF06"]
    assert "--memory-budget" not in flags["RF08"]
    assert [flags["RF04-RF03"][i + 1] for i, part in enumerate(flags["RF04-RF03"][:-1])
            if part == "--target"] == ["rf04-original-target:physical_node_versions_examined"]
    assert [flags["RF05"][i + 1] for i, part in enumerate(flags["RF05"][:-1])
            if part == "--target"] == ["rf05-first-n1024-p16:physical_node_versions_examined"]
    assert all("--calibrated-executor" in command and "--max-cpu-percent" in command
               for command in flags.values())


def test_ordinary_cli_selection_reaches_the_existing_matrix_gate(tmp_path):
    catalog = {scenario.name: scenario for scenario in matrix()}
    for partition in ("RF02", "RF03", "RF05"):
        candidate = manifest(partition)
        command = _command(tmp_path, tmp_path, candidate, None)[1]
        overrides = {}
        for index, part in enumerate(command[:-1]):
            if part == "--target":
                name, counter = command[index + 1].split(":")
                overrides[name] = counter
        selected = []
        for name in candidate["scenarios"]:
            scenario = replace(catalog[name], query_memory_budget_bytes=DEFAULT_BUDGET)
            if name in overrides:
                scenario = replace(scenario, role="target", work_counter=overrides[name])
            selected.append(scenario.to_dict())
        validate_fixed_optimization_matrix(selected, mode="optimization", intent="qualification")
        assert selected == [s.to_dict() for s in expected_scenarios(partition, DEFAULT_BUDGET)]


def test_retry_requires_explicit_frozen_lineage():
    candidate = manifest("RF08")
    candidate.update(attempt=2, previous_output="C:/runs/old",
                     previous_campaign_sha256="c" * 64)
    validate_manifest(candidate, "RF08")
    candidate["previous_campaign_sha256"] = "not a digest"
    with pytest.raises(ValueError):
        validate_manifest(candidate, "RF08")


def test_rf04_report_requires_four_phase_paths_hashes_and_budget():
    report = {"schema_version": REPORT_SCHEMA, "runner_root": "C:/runner",
              "runner_revision": "b" * 40, "python": "C:/python.exe",
              "output": "C:/runs/report", "timeout_seconds": 3500,
              "memory_budget_bytes": DEFAULT_BUDGET,
              "phase_outputs": {phase: "C:/runs/" + phase for phase in RF04_PHASES},
              "phase_campaign_sha256": {phase: "c" * 64 for phase in RF04_PHASES}}
    validate_report_manifest(report)
    report["phase_outputs"].pop("combined")
    with pytest.raises(ValueError, match="all four"):
        validate_report_manifest(report)


def test_outer_supervisor_never_credits_inconclusive_child(tmp_path, monkeypatch):
    from bench.harness import campaign, rf0208_operational

    base = tmp_path / "Okto/Grafx"
    (base / "runs").mkdir(parents=True)
    selected = manifest("RF08")
    monkeypatch.setattr(rf0208_operational, "_load_manifest",
                        lambda partition: (base, b"frozen manifest", selected))

    def inconclusive(command, log, timeout):
        assert timeout == 3500
        assert "rf0208_operational" in command[5]
        Path(command[-3]).write_text(
            json.dumps({"partition": "RF08", "decision": "inconclusive"}), encoding="utf-8")
        return 0

    monkeypatch.setattr(campaign, "execute_worker", inconclusive)
    with pytest.raises(RuntimeError, match="incomplete or inconclusive"):
        rf0208_operational.run("RF08")
    audit, = (base / "runs").iterdir()
    assert json.loads((audit / "supervisor-receipt.json").read_text())["decision"] == "failed"
