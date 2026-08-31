from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools import measure_m7_ce3 as ce3


def _environment_evidence(*, executor: str = "executor") -> dict[str, object]:
    return {
        "python_executable_sha256": executor,
        "python_release": ce3.OFFICIAL_PYTHON_VERSION,
        "modules": {
            "numpy": {"version": ce3.OFFICIAL_NUMPY_VERSION, "origin": "numpy"},
            "ladybug": {"version": ce3.OFFICIAL_LADYBUG_VERSION, "origin": "ladybug"},
            "google_crc32c": {"version": "1.8.0", "origin": "google_crc32c"},
        },
        "accel_ready": True,
        "official_baseline": {
            "python": ce3.OFFICIAL_PYTHON_VERSION,
            "numpy": ce3.OFFICIAL_NUMPY_VERSION,
            "ladybug": ce3.OFFICIAL_LADYBUG_VERSION,
            "google_crc32c": "available",
        },
        "official_baseline_checks": {
            "python_3_13_1": True,
            "numpy_2_5_1": True,
            "ladybug_0_16_0": True,
            "google_crc32c_available": True,
        },
        "official_baseline_matches": True,
    }


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
                    "capture_status": "captured_after_reset",
                    "calls": {name: 1 for name in ce3.REQUIRED_HOOKS},
                    "inclusive_ms": {name: 0.1 for name in ce3.REQUIRED_HOOKS},
                    "observed_hooks": list(ce3.REQUIRED_HOOKS),
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
    environment = _environment_evidence()
    verification = {
        "engine": "okto-grafx",
        "clean": True,
        "pages_checked": 1,
        "records_checked": 1,
        "index_entries_checked": 1,
    }
    a: dict[str, object] = {
        "status": "passed",
        "pid": 101,
        "barrier_passed": True,
        "handle": {
            "opened_at_ns": 1,
            "closed_at_ns": 21_000_000_000,
            "checksum_implementation": "native",
            "environment": copy.deepcopy(environment),
        },
        "active_start_ns": active_start,
        "active_end_ns": active_end,
        "reopens_during_measured_window": 0,
        "live_verifier": {
            "status": "passed",
            "cold_open": False,
            "handle_was_open": True,
            "verify_scope": "all",
            "started_at_ns": 20_100_000_000,
            "ended_at_ns": 20_200_000_000,
            "verification": dict(verification),
        },
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
            "environment": copy.deepcopy(environment),
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
        "cold_open": True,
        "handle": {
            "checksum_implementation": "native",
            "environment": copy.deepcopy(environment),
        },
        "verification": dict(verification),
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
    guard = {
        "status": "passed",
        "sha256": ce3.RAW_HOOK_GUARD_SHA256,
        "expected_sha256": ce3.RAW_HOOK_GUARD_SHA256,
    }
    checkouts = {
        name: {"status": "clean", "head": name, "expected_head": name}
        for name in ("grafx", "community", "core")
    }
    identity_capture = {
        "checkouts": checkouts,
        "tool": {
            "commit": "tool-commit",
            "expected_commit": "tool-commit",
            "blob": "tool-blob",
            "committed_blob": "tool-blob",
            "expected_blob": "tool-blob",
            "raw_hook_guard": guard,
        },
        "executor": {
            "sha256": "executor",
            "python_release": ce3.OFFICIAL_PYTHON_VERSION,
        },
    }
    identity_capture["fingerprint_sha256"] = ce3._identity_fingerprint(identity_capture)
    environment = _environment_evidence()
    return {
        "official_requested": True,
        "check_only": False,
        "inputs": {
            "per_family": 5,
            "maximum_initial_cpu_percent": 20.0,
            "tool_commit": "tool-commit",
            "tool_blob": "tool-blob",
        },
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
            "checkouts": checkouts,
            "environment": environment,
            "fixture_base": {"sha256": "a", "files": 1, "bytes": 2},
            "identity": {
                "start": copy.deepcopy(identity_capture),
                "end": copy.deepcopy(identity_capture),
                "stable": True,
            },
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
    assert all(
        tuple(scenario.as_dict())
        == ("id", "relation", "table", "target_rate_per_second")
        for scenario in ce3.SCENARIOS
    )
    assert all(scenario.meaning for scenario in ce3.SCENARIOS)


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
        (lambda value: value["scenario"].update(table="Assumption"), "result_scenario_not_canonical"),
        (lambda value: value["process_b"]["effects"].update(observed_nodes=0), "b_effects_not_proved"),
        (lambda value: value["process_a"]["instrumentation"].update(enabled=True), "raw_pass_contaminated_by_hooks"),
        (lambda value: value["verifier"]["verification"].update(pages_checked=0, records_checked=0, index_entries_checked=0), "cold_verify_all_clean_coverage_not_proved"),
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
    shortfalls = ce3._scenario_shortfalls(instrumented)
    assert "instrumented_per_operation_hook_evidence_incomplete" in shortfalls
    assert "instrumented_required_hook_never_observed" in shortfalls

    installed_mutant = _scenario_result("instrumented", ce3.SCENARIOS[0])
    installed_mutant["process_a"]["instrumentation"]["installed"].pop()
    assert "instrumented_hook_installation_not_proved" in ce3._scenario_shortfalls(
        installed_mutant
    )
    duplicated_install = _scenario_result("instrumented", ce3.SCENARIOS[0])
    duplicated_install["process_a"]["instrumentation"]["installed"].append(
        ce3.REQUIRED_HOOKS[-1]
    )
    assert "instrumented_hook_installation_not_proved" in ce3._scenario_shortfalls(
        duplicated_install
    )


def test_hook_extraction_does_not_fabricate_zero_valued_presence() -> None:
    extracted = ce3._extract_hooks(
        {
            "calls": {"LocalStorageDevice._still_names": 7},
            "inclusive_ms": {"LocalStorageDevice._still_names": 1.25},
            "read_view_drops": {"all": 0, "file": 0, "doom_pinned": 0},
        }
    )

    assert extracted["calls"] == {"LocalStorageDevice._still_names": 7}
    assert "BufferPool._read_page" not in extracted["calls"]
    assert "BufferPool._invalidate" not in extracted["calls"]


def test_one_good_hook_sample_and_fifty_nine_missing_or_zero_cannot_pass() -> None:
    result = _scenario_result("instrumented", ce3.SCENARIOS[0])
    samples = result["process_a"]["samples"]
    for index, sample in enumerate(samples[1:], start=1):
        if index % 2:
            sample.pop("hooks")
        else:
            sample["hooks"] = {
                "capture_status": "captured_after_reset",
                "calls": {},
                "inclusive_ms": {},
                "observed_hooks": [],
            }

    assert "instrumented_per_operation_hook_evidence_incomplete" in ce3._scenario_shortfalls(
        result
    )


def test_hook_gate_does_not_require_an_event_impossible_for_every_operation() -> None:
    result = _scenario_result("instrumented", ce3.SCENARIOS[0])
    samples = result["process_a"]["samples"]
    for sample in samples:
        sample["hooks"]["calls"] = {"LocalStorageDevice._still_names": 1}
        sample["hooks"]["inclusive_ms"] = {"LocalStorageDevice._still_names": 0.1}
        sample["hooks"]["observed_hooks"] = ["LocalStorageDevice._still_names"]
    samples[0]["hooks"]["calls"] = {"BufferPool._read_page": 1}
    samples[0]["hooks"]["inclusive_ms"] = {"BufferPool._read_page": 0.1}
    samples[0]["hooks"]["observed_hooks"] = ["BufferPool._read_page"]
    samples[1]["hooks"]["calls"] = {"BufferPool._invalidate": 1}
    samples[1]["hooks"]["inclusive_ms"] = {"BufferPool._invalidate": 0.1}
    samples[1]["hooks"]["observed_hooks"] = ["BufferPool._invalidate"]

    assert ce3._scenario_shortfalls(result) == []


@pytest.mark.parametrize("field", ["id", "relation", "table", "target_rate_per_second"])
@pytest.mark.parametrize("location", ["result", "process_b"])
def test_every_scenario_coordinate_is_bound_to_the_canonical_id(
    field: str, location: str
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    target = result["scenario"] if location == "result" else result["process_b"]["scenario"]
    target[field] = "mutated" if field != "target_rate_per_second" else 7.0

    shortfalls = ce3._scenario_shortfalls(result)
    expected = (
        "scenario_id_not_canonical"
        if location == "result" and field == "id"
        else f"{location}_scenario_not_canonical"
    )
    assert expected in shortfalls


def test_scenario_target_rate_type_is_part_of_exact_canonical_equality() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    result["scenario"]["target_rate_per_second"] = 1

    assert "result_scenario_not_canonical" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("location", ["result", "process_b"])
def test_canonical_scenario_rejects_meaning_as_an_extra_field(location: str) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    target = result["scenario"] if location == "result" else result["process_b"]["scenario"]
    target["meaning"] = ce3.SCENARIOS[1].meaning

    assert f"{location}_scenario_not_canonical" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("location", ["result", "process_b"])
def test_canonical_scenario_rejects_any_other_extra_field(location: str) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    target = result["scenario"] if location == "result" else result["process_b"]["scenario"]
    target["extra"] = "forged"

    assert f"{location}_scenario_not_canonical" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("which", ["live", "cold"])
@pytest.mark.parametrize("mutation", ["not_clean", "no_coverage"])
def test_both_live_and_cold_verify_all_clean_coverage_govern_pass(
    which: str, mutation: str
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    verifier = (
        result["process_a"]["live_verifier"] if which == "live" else result["verifier"]
    )
    if mutation == "not_clean":
        verifier["verification"]["clean"] = False
    else:
        verifier["verification"].update(
            pages_checked=0, records_checked=0, index_entries_checked=0
        )

    expected = (
        "live_verify_all_clean_coverage_not_proved"
        if which == "live"
        else "cold_verify_all_clean_coverage_not_proved"
    )
    assert expected in ce3._scenario_shortfalls(result)


def test_live_verify_must_finish_before_both_measured_handles_close() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_b"]["handle"]["closed_at_ns"] = 20_150_000_000

    assert "live_verify_not_inside_open_handle_window" in ce3._scenario_shortfalls(result)


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


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["provenance"]["environment"]["official_baseline_checks"].update(
                python_3_13_1=False
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"].update(
                official_baseline_checks={}
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"]["modules"]["numpy"].update(
                version="2.5.0"
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"]["modules"]["numpy"].update(
                error="ImportError: failed"
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"]["modules"]["numpy"].update(
                origin="forged-origin"
            ),
            "child_environment_differs_from_launcher",
        ),
        (
            lambda value: value["provenance"]["environment"]["official_baseline"].update(
                numpy="2.5.0"
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"]["official_baseline"].update(
                ladybug="0.15.0"
            ),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["identity"]["end"].update(
                fingerprint_sha256="drift"
            ),
            "tool_executor_or_checkout_identity_drift",
        ),
        (
            lambda value: value["provenance"]["identity"]["end"]["checkouts"][
                "grafx"
            ].update(head="drift"),
            "tool_executor_or_checkout_identity_drift",
        ),
        (
            lambda value: value["provenance"]["identity"]["end"]["executor"].update(
                sha256="drift"
            ),
            "tool_executor_or_checkout_identity_drift",
        ),
        (
            lambda value: value["provenance"]["identity"]["end"]["tool"].update(
                blob="other"
            ),
            "launcher_blob_commit_or_raw_guard_not_pinned",
        ),
        (
            lambda value: value["provenance"]["identity"]["end"]["executor"].update(
                python_release="3.13.2"
            ),
            "executor_identity_not_pinned",
        ),
        (
            lambda value: value["results"][0]["process_a"]["handle"]["environment"].update(
                python_executable_sha256="other-executor"
            ),
            "child_executor_differs_from_launcher",
        ),
    ],
)
def test_environment_and_end_identity_mutations_are_fail_closed(mutation, expected: str) -> None:
    report = _official_report()
    assert ce3.official_shortfalls(report) == []

    mutation(report)

    assert expected in ce3.official_shortfalls(report)


def test_child_environment_observed_versions_cannot_disagree_with_asserted_checks() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_a"]["handle"]["environment"]["modules"]["ladybug"][
        "version"
    ] = "0.15.0"

    assert "child_official_environment_mismatch" in ce3._scenario_shortfalls(result)


def test_child_module_origins_must_agree_with_each_other() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_a"]["handle"]["environment"]["modules"]["numpy"][
        "origin"
    ] = "forged-origin"

    assert "child_environment_identity_disagrees" in ce3._scenario_shortfalls(result)


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


def test_raw_hook_guard_has_a_pinned_semantic_ast_digest() -> None:
    evidence = ce3.raw_hook_guard_ast_evidence(Path(ce3.__file__))

    assert evidence == {
        "status": "passed",
        "semantic": "if-body\nhooks is not None\nhooks.install()",
        "sha256": ce3.RAW_HOOK_GUARD_SHA256,
        "expected_sha256": ce3.RAW_HOOK_GUARD_SHA256,
    }


def test_mutant_moving_install_outside_the_raw_guard_is_rejected(tmp_path: Path) -> None:
    source = Path(ce3.__file__).read_text(encoding="utf-8")
    guarded = (
        "        if hooks is not None:\n"
        "            hooks.install()\n"
        "            installed_hooks"
    )
    mutant = (
        "        hooks.install()\n"
        "        if hooks is not None:\n"
        "            installed_hooks"
    )
    assert source.count(guarded) == 1
    mutated = tmp_path / "measure_m7_ce3_mutant.py"
    mutated.write_text(source.replace(guarded, mutant), encoding="utf-8")

    with pytest.raises(ce3.MeasurementRefused, match="not nested under an if guard"):
        ce3.raw_hook_guard_ast_evidence(mutated)


def test_mutant_installing_hooks_in_the_guard_else_branch_is_rejected(tmp_path: Path) -> None:
    source = Path(ce3.__file__).read_text(encoding="utf-8")
    guarded = (
        "        if hooks is not None:\n"
        "            hooks.install()\n"
        "            installed_hooks"
    )
    mutant = (
        "        if hooks is not None:\n"
        "            pass\n"
        "        else:\n"
        "            hooks.install()\n"
        "        if hooks is not None:\n"
        "            installed_hooks"
    )
    assert source.count(guarded) == 1
    mutated = tmp_path / "measure_m7_ce3_else_mutant.py"
    mutated.write_text(source.replace(guarded, mutant), encoding="utf-8")

    with pytest.raises(ce3.MeasurementRefused, match="positive body"):
        ce3.raw_hook_guard_ast_evidence(mutated)
