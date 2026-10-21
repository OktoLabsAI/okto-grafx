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
    names = ["constant", "reader-short-churn0", "edges-n1024-uniform-x1"]
    return {
        "controls": [{"scenario": name, "rationale": "Dedicated statement, cursor or edge scan control."}
                     for name in names],
        "not_applicable": [{"scenarios": [s.name for s in matrix() if s.name not in names],
                            "rationale": "Excluded from this boundary-test selection, not a qualification claim."}],
    }


def test_explicit_controls_preserve_order_and_exhaustive_omission_reasons():
    from bench.harness.pulse_qualification import validate_controls
    selection = control_selection()
    assert validate_controls(selection) == ["constant", "reader-short-churn0", "edges-n1024-uniform-x1"]


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
    assert observed["separate_evidence_required"] == ["landing_candidates", "landing_batches"]
    assert "landing_candidates" not in observed["uncovered_instrumentation"]
    assert "certificate_attempts" in observed["uncovered_instrumentation"]


def test_budgeted_coverage_keeps_landing_pending_even_with_reported_counts():
    from bench.harness.pulse_qualification import control_observations
    raw = {"runs": [{"arm": "B", "samples": [{"kind": "retained", "statistics": {
        "physical_node_versions_examined": 1, "physical_edge_versions_examined": 1,
        "index_probe_keys": 1, "certificate_attempts": 1, "landing_batches": 1,
        "landing_candidates": 1}}]}]}
    observed = control_observations(raw)
    assert observed["uncovered_instrumentation"] == []
    assert observed["separate_evidence_required"] == ["landing_candidates", "landing_batches"]
    assert "not N/A" in observed["separation_reason"]


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


@pytest.mark.parametrize("fault", ["selection", "coverage", "landing_pending"])
def test_bridge_passes_all_selected_controls_and_rejects_incomplete_scope(tmp_path, monkeypatch, fault):
    from bench.harness import pulse_qualification as bridge
    from bench.harness import campaign_schema
    root = Path(__file__).resolve().parents[2]
    base = tmp_path / "Okto" / "Grafx"
    base.mkdir(parents=True)
    output = base / "runs" / "scope-boundary"
    selection = control_selection()
    names = [record["scenario"] for record in selection["controls"]]
    (base / "rf01-qualification.json").write_text(json.dumps({
        "schema_version": "grafx-pulse-qualification/v2", "candidate_root": str(root),
        "baseline_root": str(root), "python": str(Path(sys.executable).resolve()),
        "candidate_revision": "1" * 40, "output": str(output), **selection,
    }), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(bridge, "inventory", lambda *args: {"fixture": "digest"})
    monkeypatch.setattr(bridge, "verify_snapshot", lambda *args: None)
    # Inject successful phase transport/schema decisions to isolate the scope
    # refusal. This is not a synthetic campaign offered as qualification.
    monkeypatch.setattr(campaign_schema, "compare_campaign", lambda raw: {"decision": "passed"})

    def phase(command, log, timeout):
        assert 0 < timeout <= 90
        target = Path(command[-1])
        if "calibrate" in command:
            target.write_text(json.dumps({"procedure": {"command": command}}))
        else:
            assert [command[i + 1] for i, value in enumerate(command) if value == "--scenario"] == names
            assert command[command.index("--max-cpu-percent") + 1] == "20"
            assert command[command.index("--intent") + 1] == "qualification"
            target.mkdir()
            statistics = {key: 1 for key in bridge.PHYSICAL_COVERAGE} if fault == "landing_pending" else {}
            raw = {"scenarios": [{"name": name} for name in (names[:1] if fault == "selection" else names)],
                   "runs": [{"arm": "B", "samples": [{"kind": "retained", "statistics": statistics}]}]}
            (target / "campaign.json").write_text(json.dumps(raw))
        return 0

    monkeypatch.setattr("bench.harness.campaign.execute_worker", phase)
    if fault == "landing_pending":
        assert bridge._run(root)["decision"] == "incomplete"
    else:
        message = "controls differ" if fault == "selection" else "uncovered physical instrumentation"
        with pytest.raises(ValueError, match=message):
            bridge._run(root)
    receipt = json.loads((output / "runner-receipt.json").read_text())
    assert receipt["decision"] == ("incomplete" if fault == "landing_pending" else "failed")
    assert receipt["controls"] == selection["controls"]
    assert receipt["not_applicable"] == selection["not_applicable"]
    if fault == "coverage":
        assert "index_probe_keys" in receipt["physical_coverage"]["uncovered_instrumentation"]
    if fault == "landing_pending":
        assert receipt["temporal_decision"] == "passed"
        assert receipt["qualification_pending"] == ["landing_candidates", "landing_batches"]


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
    root = Path(__file__).resolve().parents[2]
    app = tmp_path / "app"
    base = app / "Okto" / "Grafx"
    base.mkdir(parents=True)
    output = base / "runs" / "failed"
    (base / "rf01-qualification.json").write_text(json.dumps({
        "schema_version": "grafx-pulse-qualification/v2", "candidate_root": str(root),
        "baseline_root": str(root), "python": str(Path(sys.executable).resolve()),
        "candidate_revision": "1" * 40, "output": str(output), **control_selection(),
    }), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(app))
    monkeypatch.setattr(bridge, "inventory", lambda *args: {"fixture": "digest"})
    monkeypatch.setattr(bridge, "verify_snapshot", lambda *args: None)

    def failed(command, log, timeout):
        assert 0 < timeout <= 90
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
    def timeout(command, log, seconds):
        assert 0 < seconds <= 105
        assert "_run" in command[3]
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
    monkeypatch.setattr("bench.harness.campaign.execute_worker", lambda *args: 7)
    with pytest.raises(RuntimeError, match="child exited 7"):
        bridge.run(Path(__file__).resolve().parents[2])
    receipt = json.loads(next(tmp_path.rglob("supervisor-receipt.json")).read_text())
    assert receipt["decision"] == "failed"
