from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from tools import measure_m7_ce3 as ce3


def _samples(*, instrumented: bool, per_family: int = 5) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    stamp = 10_000_000_000
    for family in ce3.EXPECTED_FAMILIES:
        for index in range(per_family):
            sample: dict[str, object] = {
                "family": family,
                "index": index,
                "operation_id": f"{family}-{index}",
                "started_at_ns": stamp,
                "ended_at_ns": stamp + 10_000_000,
                "wall_ms": 10.0,
                "postcondition": "node_exists",
                "postcondition_status": "passed",
                "foreign_commits_completed_during_operation": 0,
            }
            if instrumented:
                sample["hooks"] = {
                    "calls": {name: 1 for name in ce3.REQUIRED_HOOKS},
                    "inclusive_ms": {name: 0.1 for name in ce3.REQUIRED_HOOKS},
                }
            samples.append(sample)
            stamp += 20_000_000
    return samples


def _scenario_result(pass_name: str, scenario: ce3.Scenario) -> dict[str, object]:
    active_start = 10_000_000_000
    active_end = 20_000_000_000
    commits: list[dict[str, object]] = []
    if scenario.target_rate_per_second:
        count = int(scenario.target_rate_per_second * 10)
        spacing = int(1e9 / scenario.target_rate_per_second)
        for index in range(count):
            ended = active_start + (index + 1) * spacing
            commits.append(
                {
                    "node_id": f"b-{index}",
                    "started_at_ns": ended - 1_000_000,
                    "ended_at_ns": ended,
                    "wall_ms": 1.0,
                }
            )
    a: dict[str, object] = {
        "status": "passed",
        "pid": 101,
        "barrier_passed": True,
        "handle": {
            "opened_at_ns": 1,
            "closed_at_ns": 21_000_000_000,
            "checksum_implementation": "native",
        },
        "active_start_ns": active_start,
        "active_end_ns": active_end,
        "reopens_during_measured_window": 0,
        "instrumentation": {
            "enabled": pass_name == "instrumented",
            "installed": list(ce3.REQUIRED_HOOKS) if pass_name == "instrumented" else [],
            "raw_contaminated": False,
        },
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "families": list(ce3.EXPECTED_FAMILIES),
        "samples": _samples(instrumented=pass_name == "instrumented"),
        "postconditions_passed": 60,
        "refusals": [],
    }
    b: dict[str, object] = {
        "status": "passed",
        "pid": 202,
        "barrier_passed": True,
        "handle": {
            "opened_at_ns": 2,
            "closed_at_ns": 22_000_000_000,
            "checksum_implementation": "native",
        },
        "active_start_ns": active_start,
        "active_end_ns": active_end,
        "reopens_during_measured_window": 0,
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "scenario": scenario.as_dict(),
        "commits": commits,
        "effects": {
            "expected_nodes": len(commits),
            "observed_nodes": len(commits),
            "status": "passed",
        },
        "refusals": [],
    }
    verifier = {
        "status": "passed",
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "verify_scope": "all",
        "handle": {"checksum_implementation": "native"},
        "verification": {
            "engine": "okto-grafx",
            "pages_checked": 1,
            "records_checked": 1,
            "index_entries_checked": 1,
        },
    }
    result: dict[str, object] = {
        "copy_id": f"{pass_name}-{scenario.identifier}",
        "per_family": 5,
        "pass": pass_name,
        "scenario": scenario.as_dict(),
        "barrier": {"parent_participated": True},
        "copy_authentication": {
            "base": {"sha256": "a", "files": 1, "bytes": 2},
            "initial": {"sha256": "a", "files": 1, "bytes": 2},
            "initial_matches_base": True,
        },
        "process_a": a,
        "process_b": b,
        "verifier": verifier,
        "process_exitcodes": {"a": 0, "b": 0, "verifier": 0},
    }
    result["rate"] = ce3._rate_evidence(a, b)
    return result


def _official_report() -> dict[str, object]:
    results = [
        _scenario_result(pass_name, scenario)
        for pass_name in ("raw", "instrumented")
        for scenario in ce3.SCENARIOS
    ]
    return {
        "official_requested": True,
        "check_only": False,
        "inputs": {"per_family": 5, "maximum_initial_cpu_percent": 20.0},
        "provenance": {
            "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
            "harness": {
                "head": ce3.PINNED_HARNESS_HEAD,
                "profile_blob": ce3.PINNED_HARNESS_BLOB,
            },
            "source_workspace": {
                "sha256": "source",
                "files": 2,
                "bytes": 3,
                "expected_sha256": "source",
                "after": {"sha256": "source", "files": 2, "bytes": 3},
            },
            "source_provenance": {
                "sha256": "receipt",
                "expected_sha256": "receipt",
                "semantic_status": "passed",
                "semantic_checks": {"board": True, "pf5": True},
            },
            "checkouts": {
                name: {"status": "clean", "head": name, "expected_head": name}
                for name in ("grafx", "community", "core")
            },
            "environment": {"accel_ready": True},
            "fixture_base": {"sha256": "a", "files": 1, "bytes": 2},
        },
        "source_unchanged_after_run": True,
        "machine": {"machine_idle_asserted": True, "before": {"cpu_percent": 2.0}},
        "results": results,
    }


def test_frozen_matrix_and_pins_are_literal() -> None:
    assert ce3.EXPECTED_OPERATION_SET_SHA256 == (
        "c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81"
    )
    assert ce3.PINNED_HARNESS_HEAD == "0dfb5269dd8531fd4db2679fc80b649c64bd9b09"
    assert ce3.PINNED_HARNESS_BLOB == "a02b86dce098ceec3fdbd10a820a4dd6f9e2a7b1"
    assert [(scenario.relation, scenario.table, scenario.target_rate_per_second) for scenario in ce3.SCENARIOS] == [
        ("idle", None, 0.0),
        ("same", "Decision", 1.0),
        ("same", "Decision", 10.0),
        ("unrelated", "Assumption", 1.0),
        ("unrelated", "Assumption", 10.0),
    ]


def test_rate_is_recomputed_from_the_real_intersection() -> None:
    scenario = ce3.SCENARIOS[1]
    result = _scenario_result("raw", scenario)

    assert result["rate"]["intersection_seconds"] == 10.0
    assert result["rate"]["effective_rate_per_second"] == 1.0
    assert ce3._scenario_shortfalls(result) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["copy_authentication"].update(initial_matches_base=False), "scenario_copy_not_authenticated"),
        (lambda value: value["barrier"].update(parent_participated=False), "spawn_barrier_parent_not_proved"),
        (lambda value: value["process_b"].update(pid=101), "distinct_real_processes_not_proved"),
        (lambda value: value["process_a"].update(reopens_during_measured_window=1), "measured_handle_reopened"),
        (lambda value: value["process_a"]["samples"].pop(), "a_did_not_run_exact_pf5"),
        (lambda value: value["process_a"]["samples"][0].update(postcondition_status="skipped"), "a_postcondition_status_not_passed"),
        (lambda value: value["process_a"].update(refusals=["retryable"]), "refusal_was_not_fail_closed"),
        (lambda value: value["process_exitcodes"].update(a=1), "child_process_exit_nonzero"),
        (lambda value: value["process_b"].update(operation_set_sha256="wrong"), "child_operation_set_digest_disagrees"),
        (lambda value: value["scenario"].update(table="Assumption"), "same_or_unrelated_table_changed"),
        (lambda value: value["process_b"]["effects"].update(observed_nodes=0), "b_effects_not_proved"),
        (lambda value: value["process_a"]["instrumentation"].update(enabled=True), "raw_pass_contaminated_by_hooks"),
        (lambda value: value["verifier"]["verification"].update(pages_checked=0, records_checked=0, index_entries_checked=0), "verify_all_coverage_not_proved"),
        (lambda value: value["rate"].update(effective_rate_per_second=99.0), "effective_rate_not_reproducible_from_timestamps"),
        (lambda value: value["process_b"]["commits"].clear(), "foreign_commit_not_observed"),
        (lambda value: value["process_b"]["handle"].update(closed_at_ns=1), "b_handle_did_not_cover_a_window"),
    ],
)
def test_each_critical_false_pass_mutation_is_rejected(mutation, expected: str) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    mutation(result)

    assert expected in ce3._scenario_shortfalls(result)


def test_instrumented_requires_all_three_ce3_hooks_and_raw_has_none() -> None:
    raw = _scenario_result("raw", ce3.SCENARIOS[0])
    instrumented = _scenario_result("instrumented", ce3.SCENARIOS[0])

    assert ce3._scenario_shortfalls(raw) == []
    assert ce3._scenario_shortfalls(instrumented) == []
    del instrumented["process_a"]["samples"][0]["hooks"]
    for sample in instrumented["process_a"]["samples"]:
        sample.get("hooks", {}).get("calls", {}).pop("BufferPool._invalidate", None)
    assert "required_ce3_hooks_missing" in ce3._scenario_shortfalls(instrumented)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["provenance"].update(operation_set_sha256="wrong"), "logical_pf5_digest_mismatch"),
        (lambda value: value["provenance"]["harness"].update(profile_blob="wrong"), "harness_pin_mismatch"),
        (lambda value: value["provenance"]["source_workspace"].update(expected_sha256="wrong"), "source_workspace_not_authenticated"),
        (lambda value: value["provenance"]["source_provenance"].update(expected_sha256="wrong"), "source_provenance_not_authenticated"),
        (lambda value: value.update(source_unchanged_after_run=False), "source_workspace_changed"),
        (lambda value: value["machine"].update(machine_idle_asserted=False), "machine_idle_not_asserted"),
        (lambda value: value["machine"]["before"].update(cpu_percent=99.0), "machine_idle_sample_failed"),
        (lambda value: value["provenance"]["environment"].update(accel_ready=False), "accel_environment_not_proved"),
        (lambda value: value["results"].pop(), "raw_instrumented_matrix_incomplete"),
        (lambda value: value["results"][1].update(copy_id=value["results"][0]["copy_id"]), "raw_and_instrumented_copies_not_distinct"),
    ],
)
def test_official_report_mutations_cannot_false_pass(mutation, expected: str) -> None:
    report = _official_report()
    assert ce3.official_shortfalls(report) == []

    mutation(report)

    assert expected in ce3.official_shortfalls(report)


def test_content_digest_authenticates_names_sizes_and_bytes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    first = root / "one.bin"
    first.write_bytes(b"abc")
    before = ce3.content_digest(root)

    first.write_bytes(b"abd")
    changed_bytes = ce3.content_digest(root)
    first.rename(root / "two.bin")
    changed_name = ce3.content_digest(root)

    assert before["sha256"] != changed_bytes["sha256"]
    assert changed_bytes["sha256"] != changed_name["sha256"]
    assert before["files"] == changed_name["files"] == 1


def test_source_provenance_is_semantically_bound_to_board_and_pf5(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "board.bin").write_bytes(b"board")
    digest = ce3.content_digest(workspace)
    provenance = tmp_path / "pf5.json"
    payload = {
        "backend": "grafx",
        "mode": "continuous",
        "method_path": "scope",
        "per_family": 5,
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "forensic": {
            "workspace": str(workspace),
            "content_sha256": digest["sha256"],
            "files": digest["files"],
            "bytes": digest["bytes"],
        },
        "checkouts": {"grafx": {"head": "abc"}},
    }
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    expected = ce3._sha256_file(provenance)

    evidence = ce3.source_provenance_evidence(
        provenance, workspace, digest, expected_file_sha256=expected
    )
    assert evidence["semantic_status"] == "passed"
    assert all(evidence["semantic_checks"].values())

    payload["operation_set_sha256"] = "wrong"
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ce3.MeasurementRefused, match="logical_digest_is_pf5"):
        ce3.source_provenance_evidence(
            provenance,
            workspace,
            digest,
            expected_file_sha256=ce3._sha256_file(provenance),
        )


def test_source_parses_and_raw_branch_has_no_hook_install_call() -> None:
    source = Path(ce3.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert isinstance(tree, ast.Module)
    # A source-level tripwire complements the behavioural mutation tests: the only install call
    # in process A must remain guarded by ``hooks is not None``.
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_process_a_async"
    )
    installs = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "install"
    ]
    assert len(installs) == 1
    assert hashlib.sha256(source.encode()).hexdigest()
