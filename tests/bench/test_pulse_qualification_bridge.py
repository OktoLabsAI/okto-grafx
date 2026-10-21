"""Provenance boundary tests, deliberately not campaign qualification."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bench.harness.pulse_qualification import canonical_path, inventory, verify_snapshot


def control_selection():
    from bench.harness.campaign_fixtures import matrix
    names = ["constant", "reader-short-churn0", "edges-n1024-uniform-x1", "pk-seek-control", "landing-control"]
    return {
        "controls": [{"scenario": name, "rationale": "Dedicated statement, cursor or edge scan control."}
                     for name in names],
        "not_applicable": [{"scenarios": [s.name for s in matrix() if s.name not in names],
                            "rationale": "Excluded from this boundary-test selection, not a qualification claim."}],
    }


def test_explicit_controls_preserve_order_and_exhaustive_omission_reasons():
    from bench.harness.pulse_qualification import validate_controls
    selection = control_selection()
    assert validate_controls(selection) == ["constant", "reader-short-churn0", "edges-n1024-uniform-x1", "pk-seek-control", "landing-control"]


def operational_manifest(base, output=None):
    from bench.harness import pulse_qualification as bridge
    root = Path(bridge.__file__).resolve().parents[2]
    return {"schema_version": "grafx-pulse-qualification/v3", "candidate_root": str(root),
            "baseline_root": str(root), "python": str(Path(sys.executable).resolve()),
            "candidate_revision": "1" * 40, "output": str(output or base / "runs" / "current"),
            **control_selection(), "budgeted_memory_budget_bytes": 65536,
            "candidate_memory_proof_budget_bytes": 4096, "timeout_seconds": 180,
            "attempt": 1, "previous_aggregate": None}


def test_v3_manifest_partitions_controls_and_independent_positive_quotas(tmp_path):
    from bench.harness.pulse_qualification import load_operational_manifest
    manifest = operational_manifest(tmp_path)
    payload = json.dumps(manifest).encode()
    (tmp_path / "rf01-qualification.json").write_bytes(payload)
    original, loaded, budgeted, landing = load_operational_manifest(tmp_path)
    assert original == payload and loaded == manifest
    assert landing == ["landing-control"]
    assert budgeted == [r["scenario"] for r in manifest["controls"] if r["scenario"] != "landing-control"]
    assert loaded["budgeted_memory_budget_bytes"] != loaded["candidate_memory_proof_budget_bytes"]


@pytest.mark.parametrize("field,value", [
    ("schema_version", "grafx-pulse-qualification/v2"), ("budgeted_memory_budget_bytes", None),
    ("candidate_memory_proof_budget_bytes", 0), ("candidate_memory_proof_budget_bytes", True),
    ("candidate_memory_proof_budget_bytes", 67108865), ("attempt", 3), ("attempt", 2),
    ("previous_aggregate", "unexpected"), ("timeout_seconds", 119), ("timeout_seconds", 1801),
])
def test_v3_manifest_rejects_unsafe_regime_and_retry_configuration(tmp_path, field, value):
    from bench.harness.pulse_qualification import load_operational_manifest
    manifest = operational_manifest(tmp_path)
    manifest[field] = value
    (tmp_path / "rf01-qualification.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_operational_manifest(tmp_path)


def transport_raw(manifest, part, decision):
    from bench.harness import pulse_qualification as bridge
    names = [r["scenario"] for r in manifest["controls"] if (r["scenario"] == "landing-control") == (part == "landing")]
    return {"intent": "qualification", "mode": "noop", "transport_test_decision": decision,
            "memory_regime": "unbudgeted_landing" if part == "landing" else "budgeted",
            "candidate_memory_budget_bytes": manifest["candidate_memory_proof_budget_bytes"] if part == "landing" else None,
            "scenarios": [{"name": n, "query_memory_budget_bytes": None if part == "landing" else manifest["budgeted_memory_budget_bytes"]} for n in names],
            "runs": [{"arm": arm, "revision": bridge.BASELINE if arm == "A" else manifest["candidate_revision"],
                      "source_root": manifest["baseline_root"] if arm == "A" else manifest["candidate_root"],
                      "samples": [{"kind": "retained", "statistics": {k: 1 for k in bridge.PHYSICAL_COVERAGE}}]} for arm in ("A", "B")]}


def previous_pair_fixture(base, monkeypatch, decisions=("passed", "inconclusive")):
    """Synthetic transport only: no raw timing evidence or qualification claim."""
    from bench.harness import campaign_schema, pulse_qualification as bridge
    monkeypatch.setattr(campaign_schema, "compare_campaign", lambda raw: {"decision": raw["transport_test_decision"]})
    folder = base / "runs" / "previous"
    folder.mkdir(parents=True)
    manifest = operational_manifest(base, folder)
    (folder / "operational-manifest.json").write_text(json.dumps(manifest))
    (folder / "calibration.json").write_text("{}")
    for part, decision in zip(("budgeted", "landing"), decisions):
        (folder / part).mkdir()
        (folder / part / "campaign.json").write_text(json.dumps(transport_raw(manifest, part, decision)))
        (folder / part / "report.json").write_text(json.dumps({"decision": decision}))
    receipt = {**manifest, "baseline_revision": bridge.BASELINE, "decision": "inconclusive",
               "temporal_decisions": dict(zip(("budgeted", "landing"), decisions)),
               "manifest_sha256": hashlib.sha256((folder / "operational-manifest.json").read_bytes()).hexdigest(),
               "artifact_sha256": {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in folder.rglob("*") if p.is_file()}}
    path = folder / "runner-receipt.json"
    path.write_text(json.dumps(receipt))
    current = operational_manifest(base)
    current.update(attempt=2, previous_aggregate=str(path))
    return current, path, receipt


@pytest.mark.parametrize("decisions", [("passed", "inconclusive"), ("inconclusive", "passed"), ("inconclusive", "inconclusive")])
def test_retry_accepts_only_complete_inconclusive_pair(tmp_path, monkeypatch, decisions):
    from bench.harness.pulse_qualification import verify_previous_aggregate
    current, path, _ = previous_pair_fixture(tmp_path, monkeypatch, decisions)
    digest = verify_previous_aggregate(current, tmp_path, Path(current["output"]))
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("fault", ["passed_pair", "failed_part", "missing_part", "changed_artifact",
                                 "wrong_decision", "third_attempt", "different_budget", "different_controls"])
def test_retry_rejects_partial_cherrypicked_or_changed_evidence(tmp_path, monkeypatch, fault):
    from bench.harness.pulse_qualification import verify_previous_aggregate
    decisions = ("passed", "passed") if fault == "passed_pair" else ("failed", "inconclusive") if fault == "failed_part" else ("passed", "inconclusive")
    current, path, receipt = previous_pair_fixture(tmp_path, monkeypatch, decisions)
    if fault == "missing_part":
        del receipt["artifact_sha256"]["landing/campaign.json"]
    elif fault == "changed_artifact":
        (path.parent / "landing" / "campaign.json").write_text("tampered")
    elif fault == "wrong_decision":
        receipt["temporal_decisions"]["landing"] = "passed"
    elif fault == "third_attempt":
        receipt["attempt"] = 2
    elif fault == "different_budget":
        current["candidate_memory_proof_budget_bytes"] = 8192
    elif fault == "different_controls":
        current["controls"] = current["controls"][:-1]
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        verify_previous_aggregate(current, tmp_path, Path(current["output"]))


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "missing_minimum", "blank_reason",
                                 "missing_omission", "overlap", "duplicate_omission", "extra_field"])
def test_control_selection_fails_closed(fault):
    from bench.harness.pulse_qualification import validate_controls
    selection = control_selection()
    if fault == "unknown":
        selection["controls"][0]["scenario"] = "invented"
    elif fault == "duplicate":
        selection["controls"].append(selection["controls"][0])
    elif fault == "missing_minimum":
        selection["controls"].pop()
    elif fault == "blank_reason":
        selection["controls"][0]["rationale"] = " "
    elif fault == "missing_omission":
        selection["not_applicable"][0]["scenarios"].pop()
    elif fault == "overlap":
        selection["not_applicable"][0]["scenarios"].append("constant")
    elif fault == "duplicate_omission":
        selection["not_applicable"].append(selection["not_applicable"][0])
    else:
        selection["controls"][0]["skip_coverage"] = True
    with pytest.raises(ValueError):
        validate_controls(selection)


def test_names_do_not_prove_index_or_landing_runtime_coverage():
    from bench.harness.pulse_qualification import control_observations
    raw = {"scenarios": [{"name": n} for n in ("constant", "keys-n1024-k1", "fanout-64")],
           "runs": [{"arm": "B", "samples": [{"kind": "retained", "scenario_id": "fanout-64",
                      "statistics": {"physical_edge_versions_examined": 64}}]}]}
    observed = control_observations(raw)
    assert observed["counters"]["physical_edge_versions_examined"] == 64
    assert "index_probe_keys" in observed["uncovered_instrumentation"]
    assert "landing_candidates" in observed["uncovered_instrumentation"]
    assert "certificate_attempts" in observed["uncovered_instrumentation"]


def test_runtime_coverage_reports_all_counters_without_claiming_regime():
    from bench.harness.pulse_qualification import control_observations
    raw = {"runs": [{"arm": "B", "samples": [{"kind": "retained", "statistics": {
        "physical_node_versions_examined": 1, "physical_edge_versions_examined": 1,
        "index_probe_keys": 1, "certificate_attempts": 1, "landing_batches": 1,
        "landing_candidates": 1}}]}]}
    observed = control_observations(raw)
    assert observed["uncovered_instrumentation"] == []
    assert observed["counters"]["landing_batches"] == 1


def test_runtime_coverage_ignores_baseline_warmup_and_unexecuted_plan():
    from bench.harness.pulse_qualification import control_observations
    raw = {"runs": [{"arm": "A", "samples": [{"kind": "retained", "statistics": {"index_probe_keys": 2}}]},
                    {"arm": "B", "samples": [{"kind": "warmup", "statistics": {"index_probe_keys": 2}},
                        {"kind": "retained", "selected_plan": ["IndexSeek"], "statistics": {}}]}]}
    observed = control_observations(raw)
    assert observed["counters"]["index_probe_keys"] == 0


def test_optional_controls_are_explicit_without_claiming_their_plan():
    from bench.harness.pulse_qualification import validate_controls
    selection = control_selection()
    for name in ("keys-n1024-k1", "fanout-64"):
        selection["controls"].append({"scenario": name, "rationale": "Observe the actual runtime regime."})
        selection["not_applicable"][0]["scenarios"].remove(name)
    assert validate_controls(selection)[-2:] == ["keys-n1024-k1", "fanout-64"]


@pytest.mark.parametrize("fault", ["selection", "coverage", "none", "inconclusive", "incomplete_exit2", "forged_report"])
@pytest.mark.parametrize("aggregate_attempt", [1, 2])
def test_bridge_passes_all_selected_controls_and_rejects_incomplete_scope(tmp_path, monkeypatch, fault, aggregate_attempt):
    from bench.harness import pulse_qualification as bridge
    from bench.harness import campaign_schema
    root = Path(bridge.__file__).resolve().parents[2]
    base = tmp_path / "Okto" / "Grafx"
    base.mkdir(parents=True)
    output = base / "runs" / "scope-boundary"
    selection = control_selection()
    names = [record["scenario"] for record in selection["controls"]]
    manifest = operational_manifest(base, output)
    if aggregate_attempt == 2:
        manifest, previous_path, _ = previous_pair_fixture(base, monkeypatch)
        manifest["output"] = str(output)
    (base / "rf01-qualification.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(bridge, "inventory", lambda *args: {"fixture": "digest"})
    monkeypatch.setattr(bridge, "verify_snapshot", lambda *args: None)
    # Inject successful phase transport/schema decisions to isolate the scope
    # refusal. This is not a synthetic campaign offered as qualification.
    monkeypatch.setattr(campaign_schema, "compare_campaign", lambda raw: {"decision": raw["transport_test_decision"]})
    calls = []

    def phase(command, log, timeout):
        assert 0 < timeout <= 180
        calls.append(command)
        target = Path(command[-1])
        if "calibrate" in command:
            target.write_text(json.dumps({"procedure": {"command": command}}))
        else:
            landing = target.name == "landing"
            expected = ["landing-control"] if landing else names[:-1]
            assert [command[i + 1] for i, value in enumerate(command) if value == "--scenario"] == expected
            assert "--previous" not in command
            if landing:
                assert "--historical-unbudgeted-landing" in command and "--memory-budget" not in command
                assert command[command.index("--candidate-memory-budget") + 1] == "4096"
            else:
                assert "--historical-noop" in command
                assert command[command.index("--memory-budget") + 1] == "65536"
            assert command[command.index("--max-cpu-percent") + 1] == "20"
            assert command[command.index("--intent") + 1] == "qualification"
            target.mkdir()
            statistics = {} if fault == "coverage" else {key: 1 for key in bridge.PHYSICAL_COVERAGE}
            decision = "inconclusive" if not landing and fault in ("inconclusive", "incomplete_exit2", "forged_report") else "passed"
            raw = transport_raw(operational_manifest(base, output), target.name, decision)
            if fault == "selection":
                raw["scenarios"] = raw["scenarios"][:1]
            raw["runs"][1]["samples"][0]["statistics"] = statistics
            (target / "campaign.json").write_text(json.dumps(raw))
            if fault != "incomplete_exit2":
                (target / "report.json").write_text(json.dumps({"decision": "passed" if fault == "forged_report" else decision}))
            if decision == "inconclusive":
                return 2
        return 0

    monkeypatch.setattr("bench.harness.campaign.execute_worker", phase)
    if fault in ("none", "inconclusive"):
        assert bridge._run(root)["decision"] == ("passed" if fault == "none" else "inconclusive")
        assert len(calls) == 3
        assert calls[1][calls[1].index("--calibration") + 1] == calls[2][calls[2].index("--calibration") + 1]
    else:
        error = RuntimeError if fault in ("incomplete_exit2", "forged_report") else ValueError
        with pytest.raises(error):
            bridge._run(root)
    receipt = json.loads((output / "runner-receipt.json").read_text())
    assert receipt["decision"] == ({"none": "passed", "inconclusive": "inconclusive"}.get(fault, "failed"))
    assert receipt["controls"] == selection["controls"]
    assert receipt["not_applicable"] == selection["not_applicable"]
    assert receipt["attempt"] == aggregate_attempt
    if aggregate_attempt == 2:
        assert receipt["previous_aggregate_sha256"] == hashlib.sha256(previous_path.read_bytes()).hexdigest()
    if fault in ("incomplete_exit2", "forged_report"):
        assert len(calls) == 2  # Invalid first part cannot launch the second.


def test_snapshot_compares_actual_bytes_not_declared_revision(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_bytes(b"original\n")
    tracked = {"src/example.py": hashlib.sha256(b"original\n").hexdigest()}
    verify_snapshot(tmp_path, tracked)
    (source / "example.py").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="snapshot differs"):
        verify_snapshot(tmp_path, tracked)


def test_snapshot_refuses_inventory_escape(tmp_path):
    with pytest.raises(ValueError):
        verify_snapshot(tmp_path, {"../escaped": "not-a-hash"})


def test_relative_operational_paths_are_rejected():
    with pytest.raises(ValueError, match="absolute canonical"):
        canonical_path(Path("relative"))


def test_inventory_uses_git_blobs_with_crlf_checkout_and_export_ignored_files(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    def command(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])

    command("init", "--quiet")
    attributes = b"*.txt text eol=crlf\nsecret.txt export-ignore\n"
    (checkout / ".gitattributes").write_bytes(attributes)
    (checkout / "example.txt").write_bytes(b"one\r\ntwo\r\n")
    (checkout / "secret.txt").write_bytes(b"included\r\n")
    command("add", ".")
    command("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "CRLF fixture")
    revision = command("rev-parse", "HEAD").decode().strip()
    assert command("status", "--porcelain") == b""
    assert (checkout / "example.txt").read_bytes() == b"one\r\ntwo\r\n"
    tracked = inventory(checkout, revision)
    assert set(tracked) == {".gitattributes", "example.txt", "secret.txt"}
    for name in tracked:
        (snapshot / name).write_bytes(command("show", revision + ":" + name))
    assert (snapshot / "example.txt").read_bytes() == b"one\ntwo\n"
    verify_snapshot(snapshot, tracked)
    (snapshot / "secret.txt").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="snapshot differs"):
        verify_snapshot(snapshot, tracked)
    (checkout / "example.txt").write_bytes(b"tampered\r\n")
    with pytest.raises(ValueError, match="dirty source"):
        inventory(checkout, revision)


def test_inventory_checks_real_git_revision_and_dirty_files(tmp_path):
    def command(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args])

    command("init", "--quiet")
    (tmp_path / "example.py").write_bytes(b"original\n")
    command("add", "example.py")
    command("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "fixture")
    revision = command("rev-parse", "HEAD").decode().strip()
    assert inventory(tmp_path, revision) == {
        "example.py": hashlib.sha256(b"original\n").hexdigest(),
    }
    with pytest.raises(ValueError, match="revision mismatch"):
        inventory(tmp_path, "0" * 40)
    (tmp_path / "untracked").write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty source"):
        inventory(tmp_path, revision)


@pytest.mark.parametrize("failure", ["exit", "provenance"])
def test_worker_failure_preserves_receipt_and_logs_without_claiming_qualification(tmp_path, monkeypatch, failure):
    from bench.harness import pulse_qualification as bridge

    # Fault injection tests the bridge's failure boundary only. No timing sample
    # or campaign result is synthesized, and no qualification is asserted.
    root = Path(bridge.__file__).resolve().parents[2]
    app = tmp_path / "app"
    base = app / "Okto" / "Grafx"
    base.mkdir(parents=True)
    output = base / "runs" / "failed"
    (base / "rf01-qualification.json").write_text(json.dumps(operational_manifest(base, output)), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(app))
    monkeypatch.setattr(bridge, "inventory", lambda *args: {"fixture": "digest"})
    monkeypatch.setattr(bridge, "verify_snapshot", lambda *args: None)

    def failed(command, log, timeout):
        assert 0 < timeout <= 180
        log.write("injected child failure; no calibration executed")
        if failure == "provenance":
            Path(command[-1]).write_text(json.dumps({"procedure": {"command": ["invented"]}}))
            return 0
        return 9

    monkeypatch.setattr("bench.harness.campaign.execute_worker", failed)
    error, match = (RuntimeError, "phase 0 exited 9") if failure == "exit" else (ValueError, "interpreter argv differs")
    with pytest.raises(error, match=match):
        bridge._run(root)
    receipt = json.loads((output / "runner-receipt.json").read_text(encoding="utf-8"))
    assert receipt["decision"] == "failed"
    assert receipt["artifact_sha256"]["phase-0.log"] == hashlib.sha256(
        (output / "phase-0.log").read_bytes()
    ).hexdigest()
    assert not (output / "campaign").exists()


def test_supervisor_bounds_whole_child_and_preserves_timeout_receipt(tmp_path, monkeypatch):
    import subprocess
    from bench.harness import pulse_qualification as bridge
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Okto" / "Grafx"
    base.mkdir(parents=True)
    (base / "rf01-qualification.json").write_text(json.dumps(operational_manifest(base)))
    def timeout(command, log, seconds):
        assert 0 < seconds <= 180
        assert command[1:4] == ["-E", "-P", "-B"]
        assert "_run" in command[5]
        log.write("injected timeout during final inventory; no campaign executed")
        raise subprocess.TimeoutExpired(command, seconds)
    monkeypatch.setattr("bench.harness.campaign.execute_worker", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        bridge.run(Path(__file__).resolve().parents[2])
    receipts = list(tmp_path.rglob("supervisor-receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["decision"] == "failed"
    assert "TimeoutExpired" in receipt["failure"]
    assert not list(tmp_path.rglob("result.json"))


def test_supervisor_rejects_child_failure(tmp_path, monkeypatch):
    from bench.harness import pulse_qualification as bridge
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Okto" / "Grafx"
    base.mkdir(parents=True)
    (base / "rf01-qualification.json").write_text(json.dumps(operational_manifest(base)))
    monkeypatch.setattr("bench.harness.campaign.execute_worker", lambda *args: 7)
    with pytest.raises(RuntimeError, match="child exited 7"):
        bridge.run(Path(__file__).resolve().parents[2])
    receipt = json.loads(next(tmp_path.rglob("supervisor-receipt.json")).read_text())
    assert receipt["decision"] == "failed"
