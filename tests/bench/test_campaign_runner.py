import pytest
from argparse import Namespace
import json
from pathlib import Path

from bench.harness.campaign import process_schedule, reserve_output
from bench.harness.campaign_schema import CampaignError


def test_calibration_records_actual_interpreter_argv(monkeypatch, tmp_path):
    import bench.harness.campaign as runner
    actual = ["python", "-E", "-P", "-B", "-c", "launcher", "snapshot", "calibrate", "--output", str(tmp_path / "receipt")]
    monkeypatch.setattr(runner.sys, "orig_argv", actual)
    captured = []
    monkeypatch.setattr(runner, "calibrate_executor", lambda output, command: captured.append(command) or {})
    assert runner.main(["calibrate", "--output", str(tmp_path / "receipt")]) == 0
    assert captured == [actual]


def test_process_order_is_three_independent_pairs():
    assert process_schedule() == [(0, "A", "AB"), (0, "B", "AB"),
                                  (1, "B", "BA"), (1, "A", "BA"),
                                  (2, "A", "AB"), (2, "B", "AB")]


@pytest.mark.parametrize("valid_baseline", [False, True])
def test_historical_cli_enforces_identity_and_propagates_scope(tmp_path, monkeypatch, valid_baseline):
    import bench.harness.campaign as runner
    from bench.harness.campaign_schema import HISTORICAL_REVISION, HISTORICAL_SCOPE
    baseline = tmp_path / "baseline"
    revisions = iter([HISTORICAL_REVISION if valid_baseline else "a" * 40, "b" * 40])
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **k: next(revisions))
    requests = []
    def fail(command, log, timeout):
        requests.append(json.loads(Path(command[command.index("--request") + 1]).read_text()))
        return 7
    monkeypatch.setattr(runner, "execute_worker", fail)
    args = Namespace(output=tmp_path / "out", baseline=baseline, candidate=tmp_path,
                     scenario=["constant"], memory_budget=4096, target=[], previous=None,
                     mode="noop", intent="qualification", calibrated_executor=False,
                     max_cpu_percent=20, historical_noop=True)
    with pytest.raises(CampaignError, match="worker failed" if valid_baseline else "exact original baseline"):
        runner.run_campaign(args)
    assert len(requests) == int(valid_baseline)
    if valid_baseline:
        assert requests[0]["qualification_scope"] == HISTORICAL_SCOPE
        assert requests[0]["scenarios"][0]["query_memory_budget_bytes"] == 4096


def test_existing_artifacts_are_never_overwritten(tmp_path):
    output = tmp_path / "campaign"
    reserve_output(output)
    with pytest.raises(FileExistsError):
        reserve_output(output)


def test_worker_failure_stops_without_fabricating_missing_pairs(tmp_path, monkeypatch):
    import bench.harness.campaign as runner
    calls = []
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **k: "a" * 40)
    def failed(command, log, timeout):
        calls.append(command)
        return 7
    monkeypatch.setattr(runner, "execute_worker", failed)
    args = Namespace(output=tmp_path / "out", baseline=tmp_path, candidate=tmp_path,
                     scenario=["constant"], memory_budget=None, target=[], previous=None,
                     mode="noop", intent="exploratory", calibrated_executor=False, max_cpu_percent=20)
    with pytest.raises(CampaignError, match="worker failed"):
        runner.run_campaign(args)
    assert len(calls) == 1
    assert json.loads((args.output / "report.json").read_text())["decision"] == "invalid"
    assert json.loads((args.output / "campaign.json").read_text())["runs"] == []
    assert Path(calls[0][4]).name == "campaign_worker.py"
    assert calls[0][1:4] == ["-E", "-P", "-B"]


def test_runner_preserves_six_complete_process_outputs(tmp_path, monkeypatch):
    import bench.harness.campaign as runner
    from test_campaign_schema import campaign
    from bench.harness.campaign_worker import sample_record
    source = campaign()
    calls = []
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **k: "a" * 40)
    def completed(command, log, timeout):
        request = json.loads(Path(command[command.index("--request") + 1]).read_text())
        result = source["runs"][len(calls)]
        result["revision"] = "a" * 40
        result["samples"] = [sample_record(request["scenarios"][0], i, ((1,),), {"rows_returned": 1},
            elapsed_ns=100, cpu_ns=10, prepare_ns=2, memory={"logical_memory_peak_bytes": 0,
            "baseline_retained_bytes": 0, "coverage": "test", "unavailable_reason": None},
            rss_peak_bytes=100) for i in range(25)]
        Path(command[command.index("--output") + 1]).write_text(json.dumps(result))
        calls.append((request["pair"], request["arm"], request["run_order"]))
        return 0
    monkeypatch.setattr(runner, "execute_worker", completed)
    args = Namespace(output=tmp_path / "out", baseline=tmp_path, candidate=tmp_path,
                     scenario=["constant"], memory_budget=None, target=[], previous=None,
                     mode="noop", intent="exploratory", calibrated_executor=False, max_cpu_percent=20)
    report = runner.run_campaign(args)
    assert report["decision"] == "exploratory"
    assert calls == process_schedule()
    raw = json.loads((args.output / "campaign.json").read_text())
    assert sum(len(r["samples"]) for r in raw["runs"]) == 150
    assert len(list(args.output.glob("raw-*.json"))) == 6


def test_timeout_preserves_incomplete_campaign_without_verdict(tmp_path, monkeypatch):
    import subprocess
    import bench.harness.campaign as runner
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **k: "a" * 40)
    def timeout(command, log, seconds):
        raise subprocess.TimeoutExpired(command, seconds)
    monkeypatch.setattr(runner, "execute_worker", timeout)
    args = Namespace(output=tmp_path / "out", baseline=tmp_path, candidate=tmp_path,
                     scenario=["constant"], memory_budget=None, target=[], previous=None,
                     mode="noop", intent="exploratory", calibrated_executor=False, max_cpu_percent=20)
    with pytest.raises(CampaignError, match="timeout"):
        runner.run_campaign(args)
    assert json.loads((args.output / "report.json").read_text())["decision"] == "invalid"


@pytest.mark.parametrize("targets,scenarios", [(["bad"], ["constant"]),
                                               (["missing:rows_returned"], ["constant"]),
                                               ([], ["missing"]), ([], ["constant", "constant"])])
def test_invalid_selection_raises_campaign_error(tmp_path, targets, scenarios):
    from bench.harness.campaign import run_campaign
    args = Namespace(output=tmp_path / "out", scenario=scenarios, target=targets,
                     max_cpu_percent=20, memory_budget=None)
    with pytest.raises(CampaignError):
        run_campaign(args)


def test_real_calibration_command_produces_bound_receipt(tmp_path, capsys, monkeypatch):
    import hashlib
    import bench.harness.campaign as runner
    output = tmp_path / "calibration.json"
    # Embedded API invocation must expose its real interpreter argv, not invent
    # a CLI launch. This fixture supplies the argv of the supported CLI producer.
    monkeypatch.setattr(runner.sys, "orig_argv", [runner.sys.executable, "-m", "bench.harness.campaign",
                                               "calibrate", "--output", str(output)])
    assert runner.main(["calibrate", "--output", str(output)]) == 0
    receipt = json.loads(output.read_text())
    assert len(receipt["elapsed_ns"]) == 20
    assert len(receipt["warmup_ns"]) == 5
    assert len(receipt["cpu_percent"]) == 5
    assert receipt["procedure"]["source_sha256"] == hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest()
    assert receipt["procedure"]["command"][-3:] == ["calibrate", "--output", str(output)]
    from bench.harness.campaign_worker import executor_environment
    from bench.harness.campaign_schema import calibration_matches
    from test_campaign_schema import campaign
    assert receipt["environment"] == executor_environment()
    data = campaign()
    data["started_at"] = receipt["finished_at"]
    for run in data["runs"]:
        run["environment"].update(receipt["environment"])
    raw = output.read_text()
    data["calibration"] = {"raw_json": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "path": str(output)}
    # A real noisy host may fail readiness; its receipt must still validate.
    assert isinstance(calibration_matches(data), bool)
    assert runner.main(["calibrate", "--output", str(output)]) == 2
