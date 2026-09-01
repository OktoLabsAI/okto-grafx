from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxWriteConflict
from tools import measure_m7_ce3 as ce3


class GateFailure(RuntimeError):
    pass


class GraphLockContention(RuntimeError):
    code = "graph_lock_contention"

    def __init__(
        self,
        message: str = "typed contention",
        *,
        retryable: bool = True,
        commit_durable: object | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.details: dict[str, object] = {
            "backend": "okto_grafx",
            "operation": "commit",
            "backend_error_type": "GrafxWriteConflict",
            "backend_error_code": "write_conflict",
            "backend_retryable": True,
        }
        if commit_durable is not None:
            self.details["commit_durable"] = commit_durable


class GraphIndexUnavailable(RuntimeError):
    code = "graph_index_unavailable"
    retryable = True

    def __init__(self, message: str = "durable index refusal") -> None:
        super().__init__(message)
        self.details = {"field": "durable_index_state"}


class _RandomizerDouble:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def uniform(self, lower: float, upper: float) -> float:
        self.calls.append((lower, upper))
        return 0.0


class _RunnerDouble:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[object, object, object]] = []
        self.scope_tokens: list[object] = []

    async def _execute_operation(
        self, backend: object, context: object, operation: object
    ) -> None:
        # The real harness opens and settles a transaction scope inside every call.
        self.scope_tokens.append(object())
        self.calls.append((backend, context, operation))
        outcome = self.outcomes[len(self.calls) - 1]
        if outcome is not None:
            raise outcome


class _WarmBackendDouble:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _WarmRunnerDouble:
    def __init__(
        self,
        identity: dict[str, object] | None = None,
        *,
        identity_failure: BaseException | None = None,
    ) -> None:
        self.identity = dict(identity or {})
        self.identity_failure = identity_failure
        self.identity_calls: list[tuple[object, object]] = []
        self.close_calls: list[object] = []

    async def _backend_identity(
        self, backend: object, context: object
    ) -> dict[str, object]:
        self.identity_calls.append((backend, context))
        if self.identity_failure is not None:
            raise self.identity_failure
        return dict(self.identity)

    async def _close_backend(self, backend: _WarmBackendDouble) -> None:
        self.close_calls.append(backend)
        await backend.close()


class _WarmHarnessDouble:
    def __init__(self, root: Path, backend: _WarmBackendDouble) -> None:
        self.GRAFX = root / "grafx"
        self.COMMUNITY = root / "community"
        self.CORE = root / "core"
        self.backend = backend
        self.context = object()
        self.open_calls: list[tuple[object, str, Path]] = []
        self.read_calls: list[tuple[object, str, dict[str, object]]] = []

    async def _open_backend(
        self, runner: object, backends: object, name: str, workspace: Path
    ) -> tuple[object, object]:
        self.open_calls.append((backends, name, workspace))
        return self.backend, self.context

    async def _read_rows(
        self, backend: object, statement: str, params: dict[str, object]
    ) -> list[list[object]]:
        self.read_calls.append((backend, statement, params))
        return [["board"]]


def _install_graph_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = ce3.importlib.import_module

    def import_module(name: str, package: str | None = None) -> Any:
        if name == "okto_pulse.core.kg.interfaces.graph_errors":
            return SimpleNamespace(GraphLockContention=GraphLockContention)
        return real_import(name, package)

    monkeypatch.setattr(ce3.importlib, "import_module", import_module)


def _from_cause(cause: BaseException) -> GateFailure:
    failure = GateFailure("adapter boundary")
    failure.__cause__ = cause
    return failure


def _typed_contention(
    *,
    core_retryable: bool = True,
    backend_retryable: bool = True,
    declared_backend_retryable: bool | None = None,
    commit_durable: object | None = None,
    field: str | None = None,
    include_backend_cause: bool = True,
    rollback_note: bool = False,
) -> GateFailure:
    backend = GrafxWriteConflict("optimistic conflict", retryable=backend_retryable)
    contention = GraphLockContention(
        retryable=core_retryable, commit_durable=commit_durable
    )
    contention.details["backend_retryable"] = (
        backend_retryable
        if declared_backend_retryable is None
        else declared_backend_retryable
    )
    if field is not None:
        contention.details["field"] = field
    if include_backend_cause:
        contention.__cause__ = backend
    if rollback_note:
        contention.add_note("rollback also failed: synthetic release failure")
    return _from_cause(contention)


def _retry_refusal(attempt: int = 1) -> dict[str, object]:
    core_details = {
        "backend": "okto_grafx",
        "operation": "commit",
        "backend_error_type": "GrafxWriteConflict",
        "backend_error_code": "write_conflict",
        "backend_retryable": True,
        "commit_durable": False,
    }
    return {
        "attempt": attempt,
        "attempt_scope": "fresh_transaction",
        "type": "GraphLockContention",
        "code": "graph_lock_contention",
        "retryable": True,
        "details": core_details,
        "message": "typed contention",
        "classification": "typed_pre_durable_grafx_write_conflict",
        "chain": [
            {
                "type": "GateFailure",
                "code": None,
                "retryable": None,
                "details": None,
                "notes": [],
                "message": "adapter boundary",
            },
            {
                "type": "GraphLockContention",
                "code": "graph_lock_contention",
                "retryable": True,
                "details": core_details,
                "notes": [],
                "message": "typed contention",
            },
            {
                "type": "GrafxWriteConflict",
                "code": "write_conflict",
                "retryable": True,
                "details": {},
                "notes": [],
                "message": "optimistic conflict",
            },
        ],
        "backoff_seconds": 0.0,
    }


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


def _phase_capture(started: int, ended: int, section_id: int) -> dict[str, object]:
    child_started = started + max(1, (ended - started) // 10)
    child_ended = min(ended, child_started + max(1, (ended - started) // 5))
    context_started = started + 1
    context_ended = ended - 1
    root_ms = (ended - started) / 1e6
    context_ms = (context_ended - context_started) / 1e6
    child_ms = (child_ended - child_started) / 1e6
    covered_ms = child_ms
    residual_ms = root_ms - covered_ms
    residual_ratio = residual_ms / root_ms
    body_residual_ms = context_ms - child_ms
    body_residual_ratio = body_residual_ms / context_ms
    root_phase = "TransactionManager._commit_with_writing"
    context_phase = "TransactionManager._coordinator_section"
    child_phase = "TransactionManager._complete_committed_gap"
    return {
        "capture_status": "captured_after_reset",
        "events": [
            {
                "scope": "commit",
                "section_id": section_id,
                "phase": root_phase,
                "root": True,
                "started_at_ns": started,
                "ended_at_ns": ended,
                "inclusive_ms": root_ms,
                "outcome": "returned",
            },
            {
                "scope": "commit",
                "section_id": section_id,
                "phase": context_phase,
                "root": False,
                "started_at_ns": context_started,
                "ended_at_ns": context_ended,
                "inclusive_ms": context_ms,
                "outcome": "returned",
                "detail": "commit",
            },
            {
                "scope": "commit",
                "section_id": section_id,
                "phase": child_phase,
                "root": False,
                "started_at_ns": child_started,
                "ended_at_ns": child_ended,
                "inclusive_ms": child_ms,
                "outcome": "returned",
            },
        ],
        "inclusive_totals": {
            root_phase: {"calls": 1, "inclusive_ms": root_ms},
            context_phase: {"calls": 1, "inclusive_ms": context_ms},
            child_phase: {"calls": 1, "inclusive_ms": child_ms},
        },
        "sections": [
            {
                "scope": "commit",
                "section_id": section_id,
                "root_ms": root_ms,
                "covered_ms": covered_ms,
                "residual_ms": residual_ms,
                "residual_ratio": residual_ratio,
                "reconciliation_limit": 0.15,
                "reconciliation_conclusive": residual_ratio <= 0.15,
                "commit_section": {
                    "root_ms": context_ms,
                    "covered_ms": child_ms,
                    "residual_ms": body_residual_ms,
                    "residual_ratio": body_residual_ratio,
                    "reconciliation_limit": 0.15,
                    "reconciliation_conclusive": body_residual_ratio <= 0.15,
                },
            }
        ],
        "inclusive_not_additive": True,
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
                "attempts": 1,
                "conflicts": 0,
                "retries": 0,
                "retryable_refusal_count": 0,
                "retryable_refusals": [],
                "last_refusal": None,
            }
            if instrumented:
                sample["hooks"] = {
                    "capture_status": "captured_after_reset",
                    "calls": {name: 1 for name in ce3.REQUIRED_HOOKS},
                    "inclusive_ms": {name: 0.1 for name in ce3.REQUIRED_HOOKS},
                    "observed_hooks": list(ce3.REQUIRED_HOOKS),
                }
                sample["phase_probe"] = _phase_capture(
                    int(sample["started_at_ns"]),
                    int(sample["ended_at_ns"]),
                    len(samples) + 1,
                )
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
                    "attempts": 1,
                    "conflicts": 0,
                    "retries": 0,
                    "retryable_refusal_count": 0,
                    "retryable_refusals": [],
                    "last_refusal": None,
                }
            )
            if pass_name == "instrumented":
                commits[-1]["phase_probe"] = _phase_capture(
                    int(commits[-1]["started_at_ns"]),
                    int(commits[-1]["ended_at_ns"]),
                    index + 1,
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
            "descriptor_revalidation": ce3.REQUIRED_DESCRIPTOR_REVALIDATION,
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
            "installed": list(ce3.REQUIRED_HOOKS)
            if pass_name == "instrumented"
            else [],
            "phase_probe_installed": (
                list(ce3.PHASE_PROBE_HOOKS) if pass_name == "instrumented" else []
            ),
            "raw_contaminated": False,
        },
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "families": list(ce3.EXPECTED_FAMILIES),
        "samples": _samples(instrumented=pass_name == "instrumented"),
        "postconditions_passed": 60,
        "retry_policy": dict(ce3.RETRY_POLICY),
        "retryable_conflicts": 0,
        "retries": 0,
        "retryable_refusal_count": 0,
        "retryable_refusals": [],
        "durable_refusals": [],
        "nonretryable_refusals": [],
        "retry_exhaustions": [],
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
            "descriptor_revalidation": ce3.REQUIRED_DESCRIPTOR_REVALIDATION,
            "environment": copy.deepcopy(environment),
        },
        "active_start_ns": active_start,
        "active_end_ns": active_end,
        "reopens_during_measured_window": 0,
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "scenario": scenario.as_dict(),
        "instrumentation": {
            "enabled": pass_name == "instrumented",
            "phase_probe_installed": (
                list(ce3.PHASE_PROBE_HOOKS) if pass_name == "instrumented" else []
            ),
            "raw_contaminated": False,
        },
        "commits": commits,
        "effects": {
            "expected_nodes": len(commits),
            "observed_nodes": len(commits),
            "status": "passed",
        },
        "retry_policy": dict(ce3.RETRY_POLICY),
        "retryable_conflicts": 0,
        "retries": 0,
        "retryable_refusal_count": 0,
        "retryable_refusals": [],
        "durable_refusals": [],
        "nonretryable_refusals": [],
        "retry_exhaustions": [],
        "refusals": [],
    }
    profile_completion = ce3._profile_completion(a, 5)
    verifier = {
        "status": "passed",
        "operation_set_sha256": ce3.EXPECTED_OPERATION_SET_SHA256,
        "verify_scope": "all",
        "cold_open": True,
        "handle": {
            "checksum_implementation": "native",
            "descriptor_revalidation": ce3.REQUIRED_DESCRIPTOR_REVALIDATION,
            "environment": copy.deepcopy(environment),
        },
        "verification": dict(verification),
        "profile_completion": copy.deepcopy(profile_completion),
        "whole_profile_observation": {
            "status": "passed",
            "fingerprints": {"logical": "fingerprint"},
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
        "profile_completion": profile_completion,
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
        "schema": ce3.SCHEMA,
        "official_requested": True,
        "check_only": False,
        "inputs": {
            "per_family": 5,
            "descriptor_revalidation": ce3.REQUIRED_DESCRIPTOR_REVALIDATION,
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
        "finalization": {"status": "passed", "errors": []},
        "results": results,
    }


def _install_official_run_doubles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_scenario: Any,
    scratch_name: str,
) -> tuple[SimpleNamespace, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    harness_repo = tmp_path / "community"
    grafx_repo = tmp_path / "grafx"
    core_repo = tmp_path / "core"
    for repo in (harness_repo, grafx_repo, core_repo):
        repo.mkdir()
    provenance = tmp_path / "source-provenance.json"
    provenance.write_text("{}", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    scratch = work_root / scratch_name
    scratch.mkdir()
    base_workspace = scratch / "m7profile-grafx-base"
    base_workspace.mkdir()
    out = tmp_path / "ce3-report.json"
    good_report = _official_report()
    identity = good_report["provenance"]["identity"]["start"]
    environment = good_report["provenance"]["environment"]

    async def build_base(*_args: object) -> Path:
        return base_workspace

    harness = SimpleNamespace(_build_base=build_base)
    plan_info = {
        "digest": ce3.EXPECTED_OPERATION_SET_SHA256,
        "families": list(ce3.EXPECTED_FAMILIES),
    }

    monkeypatch.setattr(ce3, "_assert_output_outside_inputs", lambda *_args: None)
    monkeypatch.setattr(
        ce3, "_capture_identity", lambda **_kwargs: copy.deepcopy(identity)
    )
    monkeypatch.setattr(ce3, "_git", lambda *_args: ce3.PINNED_HARNESS_BLOB)
    monkeypatch.setattr(
        ce3,
        "content_digest",
        lambda path: (
            {"sha256": "source", "files": 2, "bytes": 3}
            if Path(path).resolve() == source.resolve()
            else {"sha256": "a", "files": 1, "bytes": 2}
        ),
    )
    monkeypatch.setattr(ce3, "_source_binding", lambda _source: {"backend": "grafx"})
    monkeypatch.setattr(
        ce3,
        "source_provenance_evidence",
        lambda *_args, **_kwargs: copy.deepcopy(
            good_report["provenance"]["source_provenance"]
        ),
    )
    monkeypatch.setattr(ce3, "_prepare_import_paths", lambda _config: None)
    monkeypatch.setattr(ce3, "_environment", lambda: copy.deepcopy(environment))
    monkeypatch.setattr(
        ce3,
        "_runtime",
        lambda _config: (harness, object(), object(), object(), plan_info),
    )
    machine_samples = iter(
        [
            {"cpu_percent": 2.0, "sample": "before"},
            {"cpu_percent": 3.0, "sample": "after"},
        ]
    )
    monkeypatch.setattr(ce3, "_machine_state", lambda: next(machine_samples))
    monkeypatch.setattr(ce3.tempfile, "mkdtemp", lambda **_kwargs: str(scratch))
    monkeypatch.setattr(ce3, "_run_scenario", run_scenario)

    args = SimpleNamespace(
        source_workspace=source,
        source_workspace_sha256="source",
        source_provenance=provenance,
        source_provenance_sha256="receipt",
        harness_repo=harness_repo,
        grafx=grafx_repo,
        grafx_sha="grafx",
        tool_commit="tool-commit",
        tool_blob="tool-blob",
        core=core_repo,
        core_sha="core",
        work_root=work_root,
        out=out,
        official=True,
        per_family=5,
        scenario="all",
        machine_idle_asserted=True,
        maximum_initial_cpu_percent=20.0,
        barrier_timeout_seconds=1.0,
        child_timeout_seconds=1.0,
        check_only=False,
        fail_fast=True,
    )
    return args, out, scratch


def test_fail_fast_finalizes_report_provenance_without_reclassifying_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failed_result = _scenario_result("raw", ce3.SCENARIOS[0])
    failed_result["process_b"]["status"] = "failed"
    failed_result["process_exitcodes"]["b"] = 1
    failed_result["shortfalls"] = ["process_b_failed"]
    failed_result["status"] = "failed"
    args, out, _scratch = _install_official_run_doubles(
        monkeypatch,
        tmp_path,
        run_scenario=lambda *_args: failed_result,
        scratch_name="grafx-ce3-failfast",
    )

    with pytest.raises(ce3.MeasurementRefused, match="raw/idle-0 failed"):
        ce3.run(args)

    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert persisted["official"] is False
    assert persisted["run_failure"]["type"] == "MeasurementRefused"
    assert persisted["finalization"]["status"] == "passed"
    assert persisted["finalization"]["errors"] == []
    assert persisted["results"][0]["status"] == "failed"
    assert persisted["source_unchanged_after_run"] is True
    assert persisted["provenance"]["source_workspace"]["after"] == {
        "sha256": "source",
        "files": 2,
        "bytes": 3,
    }
    assert persisted["machine"]["after"]["sample"] == "after"
    assert persisted["provenance"]["identity"]["end"] is not None
    assert persisted["provenance"]["identity"]["stable"] is True
    assert persisted["official_shortfalls"]
    assert "measurement_run_failed" in persisted["official_shortfalls"]
    assert any("process_b_failed" in item for item in persisted["official_shortfalls"])


def test_failed_post_cleanup_write_cannot_leave_an_official_pending_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def passed_scenario(
        _config: object,
        _base_workspace: object,
        _base_digest: object,
        pass_name: str,
        scenario: ce3.Scenario,
    ) -> dict[str, object]:
        result = _scenario_result(pass_name, scenario)
        result["shortfalls"] = []
        result["status"] = "passed"
        return result

    args, out, _scratch = _install_official_run_doubles(
        monkeypatch,
        tmp_path,
        run_scenario=passed_scenario,
        scratch_name="grafx-ce3-cleanup-write",
    )
    real_atomic_json = ce3._atomic_json

    def fail_post_cleanup_write(path: Path, payload: dict[str, object]) -> None:
        finalization = payload.get("finalization", {})
        if finalization.get("scratch_retained") is False:
            raise OSError("synthetic post-cleanup artifact failure")
        real_atomic_json(path, payload)

    monkeypatch.setattr(ce3, "_atomic_json", fail_post_cleanup_write)

    with pytest.raises(
        (OSError, ce3.MeasurementRefused), match="post-cleanup artifact failure"
    ):
        ce3.run(args)

    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert persisted["official"] is False
    assert not (
        persisted["official"] is True
        and persisted["finalization"]["scratch_retained"] == "cleanup_pending"
    )


def test_frozen_matrix_and_pins_are_literal() -> None:
    assert ce3.SCHEMA == "okto-grafx.ce3-m7-multiprocess.v7"
    assert ce3.REQUIRED_DESCRIPTOR_REVALIDATION == "generation"
    assert ce3.MAX_OPERATION_ATTEMPTS == 60
    assert ce3.EXPECTED_OPERATION_SET_SHA256 == (
        "c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81"
    )
    assert ce3.PINNED_HARNESS_HEAD == "b07bf3ef8cdd05bc1365a46c2411bca857ab2bb0"
    assert ce3.PINNED_HARNESS_BLOB == "a02b86dce098ceec3fdbd10a820a4dd6f9e2a7b1"
    assert [
        (scenario.relation, scenario.table, scenario.target_rate_per_second)
        for scenario in ce3.SCENARIOS
    ] == [
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


def test_open_warm_authenticates_and_records_generation_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _WarmBackendDouble()
    runner = _WarmRunnerDouble(
        {
            "backend": "grafx",
            "descriptor_revalidation": ce3.REQUIRED_DESCRIPTOR_REVALIDATION,
        }
    )
    harness = _WarmHarnessDouble(tmp_path, backend)
    monkeypatch.setattr(
        ce3,
        "_require_module_origin",
        lambda module_name, repo: str(Path(repo) / module_name),
    )
    monkeypatch.setattr(ce3, "_environment", lambda: {"status": "authenticated"})
    from okto_grafx.domain.page import checksum as checksum_module

    monkeypatch.setattr(checksum_module, "crc32c_implementation", lambda: "native")

    opened, context, handle = asyncio.run(
        ce3._open_warm(harness, runner, object(), tmp_path / "workspace")
    )

    assert opened is backend
    assert context is harness.context
    assert handle["descriptor_revalidation"] == "generation"
    assert runner.identity_calls == [(backend, harness.context)]
    assert len(harness.read_calls) == 1
    assert runner.close_calls == []
    assert backend.close_calls == 0


@pytest.mark.parametrize(
    ("identity", "identity_failure"),
    [
        ({"backend": "grafx"}, None),
        ({"backend": "grafx", "descriptor_revalidation": "strict"}, None),
        (None, GateFailure("provider identity refused")),
    ],
    ids=("missing", "strict", "provider-refusal"),
)
def test_open_warm_fails_closed_and_closes_unauthenticated_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: dict[str, object] | None,
    identity_failure: BaseException | None,
) -> None:
    backend = _WarmBackendDouble()
    runner = _WarmRunnerDouble(identity, identity_failure=identity_failure)
    harness = _WarmHarnessDouble(tmp_path, backend)

    with pytest.raises((ce3.MeasurementRefused, GateFailure)):
        asyncio.run(ce3._open_warm(harness, runner, object(), tmp_path / "workspace"))

    assert harness.read_calls == []
    assert runner.close_calls == [backend]
    assert backend.close_calls == 1


def test_typed_pre_durable_contention_retries_whole_operation_on_same_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_graph_errors(monkeypatch)
    first = _typed_contention()
    second = _typed_contention(commit_durable=False)
    runner = _RunnerDouble([first, second, None])
    randomizer = _RandomizerDouble()
    backend = object()
    context = object()
    operation = {"operation_id": "logical-1", "payload": {"id": "n-1"}}

    outcome = asyncio.run(
        ce3._execute_with_retry(
            runner,
            backend,
            context,
            operation,
            randomizer=randomizer,
        )
    )

    assert outcome["status"] == "committed"
    assert outcome["attempts"] == 3
    assert outcome["conflicts"] == outcome["retries"] == 2
    assert outcome["retryable_refusal_count"] == 2
    assert [item["attempt"] for item in outcome["retryable_refusals"]] == [1, 2]
    assert {item["type"] for item in outcome["retryable_refusals"]} == {
        "GraphLockContention"
    }
    assert {item["code"] for item in outcome["retryable_refusals"]} == {
        "graph_lock_contention"
    }
    assert {item["classification"] for item in outcome["retryable_refusals"]} == {
        "typed_pre_durable_grafx_write_conflict"
    }
    assert {item["attempt_scope"] for item in outcome["retryable_refusals"]} == {
        "fresh_transaction"
    }
    assert all(
        [entry["type"] for entry in item["chain"]]
        == ["GateFailure", "GraphLockContention", "GrafxWriteConflict"]
        for item in outcome["retryable_refusals"]
    )
    assert outcome["last_refusal"] == outcome["retryable_refusals"][-1]
    assert len(runner.calls) == 3
    assert all(call[0] is backend and call[1] is context for call in runner.calls)
    assert all(call[2] is operation for call in runner.calls)
    assert len({id(scope) for scope in runner.scope_tokens}) == 3
    assert len(randomizer.calls) == 2


@pytest.mark.parametrize("commit_durable", [None, False], ids=("absent", "false"))
def test_absent_or_false_commit_durable_remains_retryable(
    monkeypatch: pytest.MonkeyPatch, commit_durable: object | None
) -> None:
    _install_graph_errors(monkeypatch)
    failure = _typed_contention(commit_durable=commit_durable)

    assert isinstance(ce3._retryable_contention(failure), GraphLockContention)


@pytest.mark.parametrize(
    "commit_durable",
    ["true", 1, object()],
    ids=("string", "integer", "object"),
)
def test_malformed_commit_durable_is_never_retryable(
    monkeypatch: pytest.MonkeyPatch, commit_durable: object
) -> None:
    _install_graph_errors(monkeypatch)
    failure = _typed_contention(commit_durable=commit_durable)

    assert ce3._retryable_contention(failure) is None


@pytest.mark.parametrize(
    "failure",
    [
        _typed_contention(core_retryable=False),
        _typed_contention(commit_durable=True),
        _typed_contention(field="index_view_unavailable"),
        _typed_contention(rollback_note=True),
        _typed_contention(declared_backend_retryable=False),
        _typed_contention(backend_retryable=False),
        _typed_contention(include_backend_cause=False),
        _from_cause(GraphIndexUnavailable()),
        _from_cause(
            RuntimeError(
                "GraphLockContention GrafxWriteConflict retryable=True "
                "commit_durable=False"
            )
        ),
    ],
    ids=(
        "non-retryable-core",
        "commit-durable",
        "durable-index-field",
        "rollback-failed",
        "declared-flag-diverges",
        "backend-non-retryable",
        "missing-typed-backend-cause",
        "durable-index-type",
        "text-forgery",
    ),
)
def test_only_typed_retryable_pre_durable_contention_is_retried(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    _install_graph_errors(monkeypatch)
    runner = _RunnerDouble([failure])
    randomizer = _RandomizerDouble()

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(
            ce3._execute_with_retry(
                runner,
                object(),
                object(),
                {"operation_id": "terminal"},
                randomizer=randomizer,
            )
        )

    assert raised.value is failure
    assert len(runner.calls) == 1
    assert randomizer.calls == []


def test_retry_budget_exhaustion_remains_a_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_graph_errors(monkeypatch)
    runner = _RunnerDouble(
        [_typed_contention() for index in range(ce3.MAX_OPERATION_ATTEMPTS)]
    )
    randomizer = _RandomizerDouble()
    backend = object()
    context = object()
    operation = {"operation_id": "exhausted"}

    with pytest.raises(ce3.MeasurementRefused, match="exhausted 60"):
        asyncio.run(
            ce3._execute_with_retry(
                runner,
                backend,
                context,
                operation,
                randomizer=randomizer,
            )
        )

    assert len(runner.calls) == ce3.MAX_OPERATION_ATTEMPTS
    assert len(randomizer.calls) == ce3.MAX_OPERATION_ATTEMPTS - 1
    assert all(call == (backend, context, operation) for call in runner.calls)
    assert (
        len({id(scope) for scope in runner.scope_tokens}) == ce3.MAX_OPERATION_ATTEMPTS
    )


def test_b_can_close_its_window_after_a_pre_durable_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_graph_errors(monkeypatch)
    runner = _RunnerDouble([_typed_contention()])
    stop_event = SimpleNamespace(is_set=lambda: True)

    outcome = asyncio.run(
        ce3._execute_with_retry(
            runner,
            object(),
            object(),
            {"operation_id": "window-closes"},
            randomizer=_RandomizerDouble(),
            stop_event=stop_event,
        )
    )

    assert outcome["status"] == "window_closed"
    assert outcome["attempts"] == outcome["conflicts"] == outcome["retries"] == 1
    assert outcome["retryable_refusal_count"] == 1
    assert len(outcome["retryable_refusals"]) == 1


@pytest.mark.parametrize(
    "worker",
    [ce3._run_process_a_async, ce3._run_process_b_async],
    ids=("process-a", "process-b"),
)
def test_operation_latency_includes_every_retry(worker: Any) -> None:
    source = inspect.getsource(worker)
    execute = source.index("retry = await _execute_with_retry(")
    started = source.rfind("started = time.perf_counter_ns()", 0, execute)
    ended = source.index("ended = time.perf_counter_ns()", execute)

    assert 0 <= started < execute < ended


def test_rate_is_recomputed_from_the_real_intersection() -> None:
    scenario = ce3.SCENARIOS[1]
    result = _scenario_result("raw", scenario)

    assert result["rate"]["intersection_seconds"] == 10.0
    assert result["rate"]["effective_rate_per_second"] == 1.0
    assert ce3._scenario_shortfalls(result) == []


def test_consistent_retry_evidence_for_both_processes_can_pass() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    refusal_a = _retry_refusal()
    sample = result["process_a"]["samples"][0]
    sample.update(
        attempts=2,
        conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal_a],
        last_refusal=refusal_a,
    )
    result["process_a"].update(
        retryable_conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal_a],
    )
    refusal_b = _retry_refusal()
    commit = result["process_b"]["commits"][0]
    commit.update(
        attempts=2,
        conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal_b],
        last_refusal=refusal_b,
    )
    result["process_b"].update(
        retryable_conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal_b],
    )

    assert ce3._scenario_shortfalls(result) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["process_a"]["samples"][0].update(attempts=2),
        lambda value: value["process_a"]["samples"][0].update(conflicts=True),
        lambda value: value["process_a"]["samples"][0].update(
            retryable_refusal_count=1
        ),
        lambda value: value["process_a"].update(retryable_conflicts=1),
        lambda value: value["process_a"].update(retryable_refusal_count=1),
        lambda value: value["process_a"].update(retries=1),
        lambda value: value["process_a"].update(retry_policy={}),
        lambda value: value["process_b"]["commits"][0].update(attempts=2),
        lambda value: value["process_b"].update(retryable_refusals=[_retry_refusal()]),
    ],
    ids=(
        "attempt-count",
        "bool-conflict-count",
        "record-refusal-count",
        "a-conflict-total",
        "participant-refusal-count",
        "a-retry-total",
        "policy",
        "b-attempt-count",
        "b-refusal-total",
    ),
)
def test_gate_rejects_incoherent_retry_evidence(mutation: Any) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])

    mutation(result)

    assert "retry_evidence_inconsistent" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("record_owner", ["process_a", "process_b"])
@pytest.mark.parametrize(
    ("counter", "malformed"),
    [
        ("attempts", True),
        ("attempts", 1.0),
        ("conflicts", False),
        ("conflicts", 0.0),
        ("retries", False),
        ("retries", 0.0),
        ("retryable_refusal_count", False),
        ("retryable_refusal_count", 0.0),
    ],
)
def test_record_counters_reject_bool_and_float(
    record_owner: str, counter: str, malformed: object
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    records = (
        result[record_owner]["samples"]
        if record_owner == "process_a"
        else result[record_owner]["commits"]
    )
    records[0][counter] = malformed

    assert "retry_evidence_inconsistent" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("participant", ["process_a", "process_b"])
@pytest.mark.parametrize(
    ("counter", "malformed"),
    [
        ("retryable_conflicts", False),
        ("retryable_conflicts", 0.0),
        ("retries", False),
        ("retries", 0.0),
        ("retryable_refusal_count", False),
        ("retryable_refusal_count", 0.0),
    ],
)
def test_participant_counters_reject_bool_and_float(
    participant: str, counter: str, malformed: object
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    result[participant][counter] = malformed

    assert "retry_evidence_inconsistent" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize(
    "ledger",
    ["durable_refusals", "nonretryable_refusals", "retry_exhaustions", "refusals"],
)
@pytest.mark.parametrize("participant", ["process_a", "process_b"])
def test_any_terminal_refusal_ledger_keeps_the_scenario_failed(
    participant: str, ledger: str
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    result[participant][ledger] = [{"type": "terminal"}]

    assert set(ce3._scenario_shortfalls(result)) & {
        "refusal_was_not_fail_closed",
        "retry_evidence_inconsistent",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda refusal: refusal.update(type="GraphIndexUnavailable"),
        lambda refusal: refusal.update(code="graph_index_unavailable"),
        lambda refusal: refusal.update(retryable=False),
        lambda refusal: refusal.update(details={"commit_durable": True}),
        lambda refusal: refusal.update(attempt=60),
        lambda refusal: refusal.update(classification="message_pattern"),
        lambda refusal: refusal.update(attempt_scope="same_transaction"),
        lambda refusal: refusal["chain"].pop(),
        lambda refusal: refusal["chain"][1]["details"].update(
            backend_error_code="forged"
        ),
        lambda refusal: refusal["chain"][1].update(notes=["rollback also failed"]),
        lambda refusal: refusal["chain"][2].update(retryable=False),
    ],
    ids=(
        "wrong-type",
        "wrong-code",
        "not-retryable",
        "post-durable",
        "budget-exhausted",
        "wrong-classification",
        "same-scope",
        "missing-backend-chain",
        "mapping-diverges",
        "rollback-note",
        "backend-non-retryable",
    ),
)
def test_gate_rejects_a_refusal_that_was_not_eligible_for_retry(mutation: Any) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    refusal = _retry_refusal()
    mutation(refusal)
    sample = result["process_a"]["samples"][0]
    sample.update(
        attempts=2,
        conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
        last_refusal=refusal,
    )
    result["process_a"].update(
        retryable_conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
    )

    assert "retry_evidence_inconsistent" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("commit_durable", ["true", 1, object()])
def test_gate_rejects_malformed_commit_durable_evidence(
    commit_durable: object,
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    refusal = _retry_refusal()
    refusal["details"]["commit_durable"] = commit_durable
    sample = result["process_a"]["samples"][0]
    sample.update(
        attempts=2,
        conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
        last_refusal=refusal,
    )
    result["process_a"].update(
        retryable_conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
    )

    assert "retry_evidence_inconsistent" in ce3._scenario_shortfalls(result)


def test_gate_accepts_absent_commit_durable_evidence() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    refusal = _retry_refusal()
    refusal["details"].pop("commit_durable")
    sample = result["process_a"]["samples"][0]
    sample.update(
        attempts=2,
        conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
        last_refusal=refusal,
    )
    result["process_a"].update(
        retryable_conflicts=1,
        retries=1,
        retryable_refusal_count=1,
        retryable_refusals=[refusal],
    )

    assert ce3._scenario_shortfalls(result) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["copy_authentication"].update(
                initial_matches_base=False
            ),
            "scenario_copy_not_authenticated",
        ),
        (
            lambda value: value["barrier"].update(parent_participated=False),
            "spawn_barrier_parent_not_proved",
        ),
        (
            lambda value: value["process_b"].update(pid=101),
            "distinct_real_processes_not_proved",
        ),
        (
            lambda value: value["process_a"].update(reopens_during_measured_window=1),
            "measured_handle_reopened",
        ),
        (lambda value: value["process_a"]["samples"].pop(), "a_did_not_run_exact_pf5"),
        (
            lambda value: value["process_a"]["samples"][0].update(
                postcondition_status="skipped"
            ),
            "a_postcondition_status_not_passed",
        ),
        (
            lambda value: value["process_a"].update(refusals=["retryable"]),
            "refusal_was_not_fail_closed",
        ),
        (
            lambda value: value["process_exitcodes"].update(a=1),
            "child_process_exit_nonzero",
        ),
        (
            lambda value: value["process_b"].update(operation_set_sha256="wrong"),
            "child_operation_set_digest_disagrees",
        ),
        (
            lambda value: value["scenario"].update(table="Assumption"),
            "result_scenario_not_canonical",
        ),
        (
            lambda value: value["process_b"]["effects"].update(observed_nodes=0),
            "b_effects_not_proved",
        ),
        (
            lambda value: value["process_a"]["instrumentation"].update(enabled=True),
            "raw_pass_contaminated_by_hooks",
        ),
        (
            lambda value: value["verifier"]["verification"].update(
                pages_checked=0, records_checked=0, index_entries_checked=0
            ),
            "cold_verify_all_clean_coverage_not_proved",
        ),
        (
            lambda value: value["rate"].update(effective_rate_per_second=99.0),
            "effective_rate_not_reproducible_from_timestamps",
        ),
        (
            lambda value: value["process_b"]["commits"].clear(),
            "foreign_commit_not_observed",
        ),
        (
            lambda value: value["process_b"]["handle"].update(closed_at_ns=1),
            "b_handle_did_not_cover_a_window",
        ),
    ],
)
def test_each_critical_false_pass_mutation_is_rejected(mutation, expected: str) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    mutation(result)

    assert expected in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("participant", ("process_a", "process_b", "verifier"))
@pytest.mark.parametrize("mode", (None, "strict"), ids=("missing", "strict"))
def test_each_participant_must_authenticate_generation_mode(
    participant: str,
    mode: str | None,
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    handle = result[participant]["handle"]
    if mode is None:
        handle.pop("descriptor_revalidation")
    else:
        handle["descriptor_revalidation"] = mode

    assert "child_descriptor_revalidation_not_generation" in (
        ce3._scenario_shortfalls(result)
    )


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


def test_phase_probe_records_only_nested_commit_work_and_reconciles_it(
    monkeypatch,
) -> None:
    boundary: dict[str, int] = {}

    class Section:
        def __enter__(self) -> None:
            boundary["entered_at_ns"] = time.perf_counter_ns()

        def __exit__(self, *_failure: object) -> None:
            boundary["exited_at_ns"] = time.perf_counter_ns()

    class Fake:
        def root(self) -> None:
            with self.section("commit", timeout=1.0):
                self.phase()

        def phase(self) -> None:
            return None

        def section(self, _name: str, *, timeout: float):
            assert timeout == 1.0
            return Section()

    module = SimpleNamespace(Fake=Fake)
    monkeypatch.setattr(
        ce3,
        "PHASE_PROBE_ROOTS",
        (("fake", "Fake", "root", "commit"),),
    )
    monkeypatch.setattr(
        ce3,
        "PHASE_PROBE_TARGETS",
        (("fake", "Fake", "phase"),),
    )
    monkeypatch.setattr(
        ce3,
        "PHASE_PROBE_CONTEXT_TARGET",
        ("fake", "Fake", "section"),
    )
    monkeypatch.setattr(ce3.importlib, "import_module", lambda _name: module)
    original_root = Fake.root
    probe = ce3._SectionPhaseProbe()

    probe.install()
    try:
        Fake().phase()
        assert probe.snapshot()["events"] == []
        probe.reset()
        Fake().root()
        captured = probe.snapshot()
    finally:
        probe.uninstall()

    assert Fake.root is original_root
    assert [event["phase"] for event in captured["events"]] == [
        "Fake.root",
        "Fake.section",
        "Fake.phase",
    ]
    assert {event["section_id"] for event in captured["events"]} == {1}
    assert captured["sections"][0]["scope"] == "commit"
    assert "commit_section" in captured["sections"][0]
    assert captured["inclusive_not_additive"] is True
    context_event = next(
        event for event in captured["events"] if event["phase"] == "Fake.section"
    )
    phase_event = next(
        event for event in captured["events"] if event["phase"] == "Fake.phase"
    )
    assert context_event["started_at_ns"] >= boundary["entered_at_ns"]
    assert context_event["ended_at_ns"] <= boundary["exited_at_ns"]
    assert captured["sections"][0]["covered_ms"] == pytest.approx(
        phase_event["inclusive_ms"]
    )
    assert captured["sections"][0]["commit_section"]["covered_ms"] == pytest.approx(
        phase_event["inclusive_ms"]
    )


def test_phase_probe_installs_on_the_real_engine_and_captures_auto_checkpoint() -> None:
    with connect(":memory:", checkpoint_interval_records=1) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE PhaseProbe(id INT64, PRIMARY KEY(id))"
            )
        probe = ce3._SectionPhaseProbe()
        probe.install()
        try:
            probe.reset()
            started = time.perf_counter_ns()
            with database.begin("write") as transaction:
                transaction.execute("CREATE (:PhaseProbe {id: 1})")
            ended = time.perf_counter_ns()
            captured = probe.snapshot()
        finally:
            probe.uninstall()

    assert ce3._phase_probe_capture_valid(
        captured, started_at_ns=started, ended_at_ns=ended
    )
    roots = {event["scope"] for event in captured["events"] if event["root"]}
    assert roots == {"commit", "checkpoint"}

    checkpoint_root = next(
        event
        for event in captured["events"]
        if event["root"] and event["scope"] == "checkpoint"
    )
    checkpoint_children = [
        event
        for event in captured["events"]
        if not event["root"]
        and event["scope"] == "checkpoint"
        and event["section_id"] == checkpoint_root["section_id"]
    ]
    commit_contexts = [
        event
        for event in checkpoint_children
        if event["phase"] == "TransactionManager._coordinator_section"
        and event.get("detail") == "commit"
    ]
    data_barriers = [
        event
        for event in checkpoint_children
        if event["phase"] == "BufferPool.durability_barrier"
    ]
    assert len(commit_contexts) == 2
    assert data_barriers
    assert all(
        barrier["ended_at_ns"] <= section["started_at_ns"]
        or barrier["started_at_ns"] >= section["ended_at_ns"]
        for barrier in data_barriers
        for section in commit_contexts
    )

    reconciled = next(
        section for section in captured["sections"] if "commit_section" in section
    )
    reconciled["commit_section"]["residual_ms"] += 1.0
    assert not ce3._phase_probe_capture_valid(
        captured, started_at_ns=started, ended_at_ns=ended
    )


def test_v6_phase_validation_rejects_the_single_section_v5_checkpoint_shape() -> None:
    started = 10_000
    ended = 20_000
    legacy = _phase_capture(started, ended, 1)
    old_root = "TransactionManager._commit_with_writing"
    old_child = "TransactionManager._complete_committed_gap"
    new_root = "TransactionManager._checkpoint_in_section"
    new_child = "BufferPool.checkpoint"
    for event in legacy["events"]:
        event["scope"] = "checkpoint"
        if event["phase"] == old_root:
            event["phase"] = new_root
        elif event["phase"] == old_child:
            event["phase"] = new_child
    legacy["inclusive_totals"][new_root] = legacy["inclusive_totals"].pop(old_root)
    legacy["inclusive_totals"][new_child] = legacy["inclusive_totals"].pop(old_child)
    legacy["sections"][0]["scope"] = "checkpoint"

    assert not ce3._phase_probe_capture_valid(
        legacy, started_at_ns=started, ended_at_ns=ended
    )


def test_phase_probe_rejects_returned_commit_without_commit_section() -> None:
    started = 10_000
    ended = 20_000
    root_ms = (ended - started) / 1e6
    root_phase = "TransactionManager._commit_with_writing"
    capture = {
        "capture_status": "captured_after_reset",
        "events": [
            {
                "scope": "commit",
                "section_id": 1,
                "phase": root_phase,
                "root": True,
                "started_at_ns": started,
                "ended_at_ns": ended,
                "inclusive_ms": root_ms,
                "outcome": "returned",
            }
        ],
        "inclusive_totals": {root_phase: {"calls": 1, "inclusive_ms": root_ms}},
        "sections": [
            {
                "scope": "commit",
                "section_id": 1,
                "root_ms": root_ms,
                "covered_ms": 0.0,
                "residual_ms": root_ms,
                "residual_ratio": 1.0,
                "reconciliation_limit": 0.15,
                "reconciliation_conclusive": False,
            }
        ],
        "inclusive_not_additive": True,
    }

    assert not ce3._phase_probe_capture_valid(
        capture, started_at_ns=started, ended_at_ns=ended
    )


def test_phase_probe_evidence_is_required_in_both_instrumented_processes() -> None:
    result = _scenario_result("instrumented", ce3.SCENARIOS[2])
    assert ce3._scenario_shortfalls(result) == []

    result["process_b"]["instrumentation"]["phase_probe_installed"].pop()
    assert (
        "instrumented_phase_probe_installation_not_proved"
        in ce3._scenario_shortfalls(result)
    )

    malformed = _scenario_result("instrumented", ce3.SCENARIOS[2])
    malformed["process_a"]["samples"][0]["phase_probe"]["events"][0]["phase"] = (
        "unknown.phase"
    )
    assert "instrumented_phase_probe_evidence_incomplete" in ce3._scenario_shortfalls(
        malformed
    )

    missing_totals = _scenario_result("instrumented", ce3.SCENARIOS[2])
    missing_totals["process_a"]["samples"][0]["phase_probe"]["inclusive_totals"] = {}
    assert "instrumented_phase_probe_evidence_incomplete" in ce3._scenario_shortfalls(
        missing_totals
    )

    forged_reconciliation = _scenario_result("instrumented", ce3.SCENARIOS[2])
    forged_reconciliation["process_b"]["commits"][0]["phase_probe"]["sections"][0][
        "root_ms"
    ] = -1.0
    assert "instrumented_phase_probe_evidence_incomplete" in ce3._scenario_shortfalls(
        forged_reconciliation
    )

    raw = _scenario_result("raw", ce3.SCENARIOS[2])
    raw["process_b"]["commits"][0]["phase_probe"] = _phase_capture(1, 2, 1)
    assert "raw_samples_contain_phase_probe_output" in ce3._scenario_shortfalls(raw)


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

    assert (
        "instrumented_per_operation_hook_evidence_incomplete"
        in ce3._scenario_shortfalls(result)
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


def test_hook_gate_is_stable_across_sorted_json_object_keys() -> None:
    result = _scenario_result("instrumented", ce3.SCENARIOS[0])
    round_tripped = json.loads(json.dumps(result, sort_keys=True))

    assert ce3._scenario_shortfalls(round_tripped) == []


@pytest.mark.parametrize("field", ["id", "relation", "table", "target_rate_per_second"])
@pytest.mark.parametrize("location", ["result", "process_b"])
def test_every_scenario_coordinate_is_bound_to_the_canonical_id(
    field: str, location: str
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    target = (
        result["scenario"] if location == "result" else result["process_b"]["scenario"]
    )
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
    target = (
        result["scenario"] if location == "result" else result["process_b"]["scenario"]
    )
    target["meaning"] = ce3.SCENARIOS[1].meaning

    assert f"{location}_scenario_not_canonical" in ce3._scenario_shortfalls(result)


@pytest.mark.parametrize("location", ["result", "process_b"])
def test_canonical_scenario_rejects_any_other_extra_field(location: str) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[1])
    target = (
        result["scenario"] if location == "result" else result["process_b"]["scenario"]
    )
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


@pytest.mark.parametrize("complete", [False, True])
def test_cold_verifier_applies_whole_profile_observer_only_after_complete_pf5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, complete: bool
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.verify_calls = 0
            self.observe_calls = 0
            self.closed = False

        async def _verify_all(self) -> dict[str, object]:
            self.verify_calls += 1
            return {
                "engine": "okto-grafx",
                "pages_checked": 1,
                "records_checked": 1,
                "index_entries_checked": 1,
            }

        def observe_fingerprints(self) -> dict[str, str]:
            self.observe_calls += 1
            return {"nodes": "n", "edges": "e"}

        async def close(self) -> None:
            self.closed = True

    backend = Backend()

    async def open_warm(*_args: object) -> tuple[Backend, object, dict[str, int]]:
        return backend, object(), {"opened_at_ns": 1}

    monkeypatch.setattr(
        ce3,
        "_runtime",
        lambda _config: (
            object(),
            object(),
            object(),
            object(),
            {"digest": ce3.EXPECTED_OPERATION_SET_SHA256},
        ),
    )
    monkeypatch.setattr(ce3, "_open_warm", open_warm)
    completion = {
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "expected_operations": 60,
        "completed_operations": 60 if complete else 18,
        "reasons": [] if complete else ["process_a_not_passed"],
    }

    payload = asyncio.run(ce3._verify_async({}, tmp_path, completion))

    assert backend.verify_calls == 1
    assert backend.observe_calls == int(complete)
    assert backend.closed is True
    assert payload["verification"]["clean"] is True
    assert payload["profile_completion"] == completion
    observation = payload["whole_profile_observation"]
    if complete:
        assert payload["status"] == "passed"
        assert observation == {
            "status": "passed",
            "fingerprints": {"nodes": "n", "edges": "e"},
        }
    else:
        assert payload["status"] == "passed"
        assert observation == {
            "status": "not_applicable",
            "reason": "process_a_profile_incomplete",
            "expected_operations": 60,
            "completed_operations": 18,
        }


def test_cold_verifier_preserves_structural_success_when_applicable_observer_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Backend:
        async def _verify_all(self) -> dict[str, object]:
            return {
                "engine": "okto-grafx",
                "pages_checked": 1,
                "records_checked": 1,
                "index_entries_checked": 1,
            }

        def observe_fingerprints(self) -> dict[str, str]:
            raise GateFailure("logical observer refused")

        async def close(self) -> None:
            return None

    async def open_warm(*_args: object) -> tuple[Backend, object, dict[str, int]]:
        return Backend(), object(), {"opened_at_ns": 1}

    monkeypatch.setattr(
        ce3,
        "_runtime",
        lambda _config: (
            object(),
            object(),
            object(),
            object(),
            {"digest": ce3.EXPECTED_OPERATION_SET_SHA256},
        ),
    )
    monkeypatch.setattr(ce3, "_open_warm", open_warm)
    completion = {
        "status": "complete",
        "complete": True,
        "expected_operations": 60,
        "completed_operations": 60,
        "reasons": [],
    }

    payload = asyncio.run(ce3._verify_async({}, tmp_path, completion))

    assert payload["status"] == "failed"
    assert payload["verification"]["clean"] is True
    assert payload["whole_profile_observation"]["status"] == "failed"
    assert payload["whole_profile_observation"]["failure"]["type"] == "GateFailure"


def test_incomplete_profile_observation_is_not_applicable_but_never_passes_gate() -> (
    None
):
    result = _scenario_result("raw", ce3.SCENARIOS[2])
    result["process_a"].update(status="failed", postconditions_passed=0)
    del result["process_a"]["samples"]
    completion = ce3._profile_completion(result["process_a"], result["per_family"])
    result["profile_completion"] = completion
    result["verifier"]["profile_completion"] = copy.deepcopy(completion)
    result["verifier"]["whole_profile_observation"] = {
        "status": "not_applicable",
        "reason": "process_a_profile_incomplete",
        "expected_operations": completion["expected_operations"],
        "completed_operations": completion["completed_operations"],
    }

    shortfalls = ce3._scenario_shortfalls(result)

    assert "process_a_failed" in shortfalls
    assert "a_did_not_run_exact_pf5" in shortfalls
    assert "post_run_verifier_failed" not in shortfalls
    assert "whole_profile_observation_failed" not in shortfalls
    assert "incomplete_profile_observation_not_marked_not_applicable" not in shortfalls
    assert completion["completed_operations"] is None


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["profile_completion"].update(complete=False),
            "profile_completion_evidence_inconsistent",
        ),
        (
            lambda value: value["verifier"]["profile_completion"].update(
                completed_operations=59
            ),
            "profile_completion_evidence_inconsistent",
        ),
        (
            lambda value: value["verifier"]["whole_profile_observation"].pop(
                "fingerprints"
            ),
            "whole_profile_observation_failed",
        ),
    ],
)
def test_whole_profile_applicability_evidence_is_fail_closed(
    mutation: Any, expected: str
) -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])

    mutation(result)

    assert expected in ce3._scenario_shortfalls(result)


def test_missing_operation_digest_has_an_independent_completion_shortfall() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_a"]["operation_set_sha256"] = None
    result["process_b"]["operation_set_sha256"] = None
    result["verifier"]["operation_set_sha256"] = None
    completion = ce3._profile_completion(result["process_a"], result["per_family"])
    result["profile_completion"] = completion
    result["verifier"]["profile_completion"] = copy.deepcopy(completion)
    result["verifier"]["whole_profile_observation"] = {
        "status": "not_applicable",
        "reason": "process_a_profile_incomplete",
        "expected_operations": completion["expected_operations"],
        "completed_operations": completion["completed_operations"],
    }

    assert "profile_completion_digest_invalid" in ce3._scenario_shortfalls(result)


def test_live_verify_must_finish_before_both_measured_handles_close() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_b"]["handle"]["closed_at_ns"] = 20_150_000_000

    assert "live_verify_not_inside_open_handle_window" in ce3._scenario_shortfalls(
        result
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report["finalization"].update(errors=None),
        lambda report: report["provenance"]["identity"]["start"].update(tool=[]),
        lambda report: report["provenance"]["source_provenance"].update(
            semantic_checks=None
        ),
        lambda report: report.update(machine=None),
        lambda report: report["results"].__setitem__(0, None),
    ],
    ids=(
        "finalization-errors-none",
        "nested-tool-list",
        "semantic-checks-none",
        "machine-none",
        "result-none",
    ),
)
def test_official_shortfalls_is_total_over_malformed_nested_evidence(
    mutation: Any,
) -> None:
    report = _official_report()
    mutation(report)

    shortfalls = ce3.official_shortfalls(report)

    assert isinstance(shortfalls, list)
    assert shortfalls


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value.update(schema="okto-grafx.ce3-m7-multiprocess.v5"),
            "report_schema_mismatch",
        ),
        (
            lambda value: value["provenance"].update(operation_set_sha256="wrong"),
            "logical_pf5_digest_mismatch",
        ),
        (
            lambda value: value["inputs"].update(descriptor_revalidation="strict"),
            "descriptor_revalidation_input_mismatch",
        ),
        (
            lambda value: value["inputs"].pop("descriptor_revalidation"),
            "descriptor_revalidation_input_mismatch",
        ),
        (
            lambda value: value["provenance"]["harness"].update(profile_blob="wrong"),
            "harness_pin_mismatch",
        ),
        (
            lambda value: value["provenance"]["source_workspace"].update(
                expected_sha256="wrong"
            ),
            "source_workspace_not_authenticated",
        ),
        (
            lambda value: value["provenance"]["source_provenance"].update(
                expected_sha256="wrong"
            ),
            "source_provenance_not_authenticated",
        ),
        (
            lambda value: value.update(source_unchanged_after_run=False),
            "source_workspace_changed",
        ),
        (
            lambda value: value["machine"].update(machine_idle_asserted=False),
            "machine_idle_not_asserted",
        ),
        (
            lambda value: value["machine"]["before"].update(cpu_percent=99.0),
            "machine_idle_sample_failed",
        ),
        (
            lambda value: value["provenance"]["environment"].update(accel_ready=False),
            "accel_environment_not_proved",
        ),
        (lambda value: value["results"].pop(), "raw_instrumented_matrix_incomplete"),
        (
            lambda value: value["results"][1].update(
                copy_id=value["results"][0]["copy_id"]
            ),
            "raw_and_instrumented_copies_not_distinct",
        ),
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
            lambda value: value["provenance"]["environment"][
                "official_baseline_checks"
            ].update(python_3_13_1=False),
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
            lambda value: value["provenance"]["environment"][
                "official_baseline"
            ].update(numpy="2.5.0"),
            "official_environment_baseline_mismatch",
        ),
        (
            lambda value: value["provenance"]["environment"][
                "official_baseline"
            ].update(ladybug="0.15.0"),
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
            lambda value: value["results"][0]["process_a"]["handle"][
                "environment"
            ].update(python_executable_sha256="other-executor"),
            "child_executor_differs_from_launcher",
        ),
    ],
)
def test_environment_and_end_identity_mutations_are_fail_closed(
    mutation, expected: str
) -> None:
    report = _official_report()
    assert ce3.official_shortfalls(report) == []

    mutation(report)

    assert expected in ce3.official_shortfalls(report)


def test_child_environment_observed_versions_cannot_disagree_with_asserted_checks() -> (
    None
):
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_a"]["handle"]["environment"]["modules"]["ladybug"]["version"] = (
        "0.15.0"
    )

    assert "child_official_environment_mismatch" in ce3._scenario_shortfalls(result)


def test_child_module_origins_must_agree_with_each_other() -> None:
    result = _scenario_result("raw", ce3.SCENARIOS[0])
    result["process_a"]["handle"]["environment"]["modules"]["numpy"]["origin"] = (
        "forged-origin"
    )

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


def test_source_provenance_is_semantically_bound_to_board_and_pf5(
    tmp_path: Path,
) -> None:
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
        "semantic": (
            "_run_process_a_async\nif-body\nhooks is not None\nhooks.install()\n"
            "_run_process_a_async\nif-body\nhooks is not None\nphase_probe.install()\n"
            "_run_process_b_async\nif-body\ninstrumented\nphase_probe.install()"
        ),
        "sha256": ce3.RAW_HOOK_GUARD_SHA256,
        "expected_sha256": ce3.RAW_HOOK_GUARD_SHA256,
    }


def test_mutant_moving_install_outside_the_raw_guard_is_rejected(
    tmp_path: Path,
) -> None:
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


def test_mutant_installing_hooks_in_the_guard_else_branch_is_rejected(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize(
    ("guard", "replacement"),
    (
        (
            "        if hooks is not None:\n"
            "            hooks.install()\n"
            "            installed_hooks = _installed_required_hooks(hooks)\n",
            "        phase_probe = _SectionPhaseProbe()\n"
            "        phase_probe.install()\n"
            "        if hooks is not None:\n"
            "            hooks.install()\n"
            "            installed_hooks = _installed_required_hooks(hooks)\n",
        ),
        (
            "        if instrumented:\n"
            "            phase_probe = _SectionPhaseProbe()\n"
            "            phase_probe.install()\n",
            "        phase_probe = _SectionPhaseProbe()\n"
            "        phase_probe.install()\n"
            "        if instrumented:\n",
        ),
    ),
)
def test_mutant_moving_phase_probe_outside_raw_guard_is_rejected(
    tmp_path: Path, guard: str, replacement: str
) -> None:
    source = Path(ce3.__file__).read_text(encoding="utf-8")
    assert source.count(guard) == 1
    mutated = tmp_path / "measure_m7_ce3_phase_mutant.py"
    mutated.write_text(source.replace(guard, replacement), encoding="utf-8")

    with pytest.raises(
        ce3.MeasurementRefused, match="not nested under an if guard|needs exactly one"
    ):
        ce3.raw_hook_guard_ast_evidence(mutated)
