"""Closed Pulse qualification runners for RF02–RF06 and RF08.

Each invocation measures one immutable A/B comparison. RF04 uses four separate
comparisons and a final, untimed interaction report so no profile must fit all
four campaigns inside one external-runner deadline.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from bench.harness.campaign_fixtures import Scenario
from bench.harness.campaign_schema import (CALIBRATION_LAUNCHER, RUNNER_PYTHON_FLAGS,
                                           compare_campaign, load_json)
from bench.harness.pulse_qualification import canonical_path, inventory, verify_snapshot


SCHEMA = "grafx-rf0208-runner/v1"
REPORT_SCHEMA = "grafx-rf04-report/v1"
RF04_PHASES = ("rf03", "rf04", "incremental", "combined")
PARTITIONS = ("RF02", "RF03", "RF04-RF03", "RF04-RF04", "RF04-INCREMENTAL",
              "RF04-COMBINED", "RF05", "RF06", "RF08")
DEFAULT_BUDGET = 64 << 20


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(value, label):
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("immutable full SHA required: " + label)


def expected_scenarios(partition, budget):
    from bench.harness.connected_qualification import matrix as rf03_matrix
    from bench.harness.pagination_qualification import matrix as rf05_matrix
    from bench.harness.quota_qualification import qualification_matrix
    from bench.harness.rf02_qualification import matrix as rf02_matrix
    from bench.harness.rf04_qualification import phase_scenarios
    from bench.harness.uniqueness_qualification import matrix as rf08_matrix

    if partition not in PARTITIONS:
        raise ValueError("unknown RF02–RF08 qualification partition")
    if partition in ("RF06", "RF08"):
        if budget is not None:
            raise ValueError("RF06/RF08 memory regimes are fixed by their matrices")
        return qualification_matrix(Scenario) if partition == "RF06" else rf08_matrix(Scenario)
    if type(budget) is not int or not 0 < budget <= 2**31:
        raise ValueError("positive common logical budget required")
    if partition == "RF02":
        return [replace(s, query_memory_budget_bytes=budget) for s in rf02_matrix()]
    if partition == "RF03":
        return [replace(s, query_memory_budget_bytes=budget) for s in rf03_matrix()]
    if partition == "RF05":
        scenarios = rf05_matrix(Scenario)
        scenarios[0] = replace(scenarios[0], role="target",
                               work_counter="physical_node_versions_examined")
        return [replace(s, query_memory_budget_bytes=budget) for s in scenarios]
    return phase_scenarios(partition.removeprefix("RF04-").lower(), Scenario, budget=budget)


def validate_manifest(manifest, partition):
    required = {"schema_version", "partition", "runner_root", "runner_revision",
                "baseline_root", "baseline_revision", "candidate_root", "candidate_revision",
                "python", "output", "timeout_seconds", "memory_budget_bytes", "scenarios",
                "attempt", "previous_output", "previous_campaign_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != SCHEMA:
        raise ValueError("closed RF02–RF08 operational manifest required")
    if partition not in PARTITIONS or manifest["partition"] != partition:
        raise ValueError("manifest partition mismatch")
    for key in ("runner_revision", "baseline_revision", "candidate_revision"):
        _sha(manifest[key], key)
    if manifest["baseline_revision"] == manifest["candidate_revision"]:
        raise ValueError("baseline and candidate must differ")
    if not partition.startswith("RF04-") and manifest["candidate_revision"] != manifest["runner_revision"]:
        raise ValueError("candidate must be the authenticated runner revision")
    if partition in ("RF04-INCREMENTAL", "RF04-COMBINED") and manifest["candidate_revision"] != manifest["runner_revision"]:
        raise ValueError("RF04 combined arm must be the authenticated runner revision")
    if manifest["scenarios"] != [s.name for s in expected_scenarios(partition, manifest["memory_budget_bytes"])]:
        raise ValueError("partition requires exact canonical scenario matrix in order")
    timeout = manifest["timeout_seconds"]
    if type(timeout) is not int or not 120 <= timeout <= 3580:
        raise ValueError("Pulse leaves 20 seconds after the bounded child tree")
    attempt = manifest["attempt"]
    previous = manifest["previous_campaign_sha256"]
    if type(attempt) is not int or attempt not in (1, 2):
        raise ValueError("at most one complete retry")
    if attempt == 1:
        if manifest["previous_output"] is not None or previous is not None:
            raise ValueError("first attempt cannot claim prior evidence")
    elif (not isinstance(manifest["previous_output"], str) or not isinstance(previous, str)
          or len(previous) != 64 or any(c not in "0123456789abcdef" for c in previous)):
        raise ValueError("retry requires prior output and exact raw digest")


def _base():
    return canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")


def _load_manifest(partition):
    base = _base()
    raw = canonical_path(base / f"rf0208-{partition.lower()}-runner.json").read_bytes()
    manifest = load_json(raw.decode("utf-8"))
    validate_manifest(manifest, partition)
    return base, raw, manifest


def inspect_result(output, manifest):
    raw = load_json((output / "campaign.json").read_text(encoding="utf-8"))
    report = compare_campaign(raw)
    if report != load_json((output / "report.json").read_text(encoding="utf-8")):
        raise ValueError("stored report differs from complete campaign")
    partition = manifest["partition"]
    expected = [s.to_dict() for s in expected_scenarios(partition, manifest["memory_budget_bytes"])]
    if raw["mode"] != "optimization" or raw["intent"] != "qualification" or raw["scenarios"] != expected:
        raise ValueError("campaign mode, intent or matrix differs from manifest")
    protocol = raw["protocol"]
    if (protocol["attempt"] != manifest["attempt"]
            or protocol["previous_campaign_sha256"] != manifest["previous_campaign_sha256"]
            or protocol["max_cpu_percent"] != 20):
        raise ValueError("campaign retry or CPU policy differs from manifest")
    if partition == "RF06" and raw.get("memory_regime") != "rf06_split_memory_landing":
        raise ValueError("RF06 requires split memory regime")
    calibration = (output.parent / "calibration.json").read_text(encoding="utf-8")
    if (raw["calibration"]["raw_json"] != calibration
            or raw["calibration"]["sha256"] != hashlib.sha256(calibration.encode()).hexdigest()):
        raise ValueError("calibration differs from campaign")
    for run in raw["runs"]:
        arm = "baseline" if run["arm"] == "A" else "candidate"
        if run["revision"] != manifest[arm + "_revision"] or run["source_root"] != manifest[arm + "_root"]:
            raise ValueError("worker source differs from manifest")
        root = canonical_path(run["source_root"])
        if canonical_path(run["package_file"]) != canonical_path(root / "src/okto_grafx/__init__.py"):
            raise ValueError("worker imported another engine")
        worker = load_json((output / f"raw-{run['pair']}-{run['arm']}.json").read_text(encoding="utf-8"))
        if worker != run:
            raise ValueError("worker raw differs from aggregate")
    return report


def _previous(base, manifest):
    if manifest["attempt"] == 1:
        return None
    previous = canonical_path(manifest["previous_output"])
    previous.relative_to(base / "runs")
    old = load_json((previous / "operational-manifest.json").read_text(encoding="utf-8"))
    validate_manifest(old, manifest["partition"])
    if old["attempt"] != 1:
        raise ValueError("cannot retry a prior retry")
    for key in ("partition", "runner_root", "runner_revision", "baseline_root", "baseline_revision",
                "candidate_root", "candidate_revision", "python", "timeout_seconds",
                "memory_budget_bytes", "scenarios"):
        if old[key] != manifest[key]:
            raise ValueError("retry changed " + key)
    receipt = load_json((previous / "runner-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("decision") != "inconclusive" or inspect_result(previous / "campaign", old)["decision"] != "inconclusive":
        raise ValueError("retry requires complete inconclusive campaign")
    for name, digest in receipt["artifact_sha256"].items():
        artifact = canonical_path(previous / name)
        artifact.relative_to(previous)
        if _digest(artifact) != digest:
            raise ValueError("previous artifact changed")
    path = previous / "campaign/campaign.json"
    if _digest(path) != manifest["previous_campaign_sha256"]:
        raise ValueError("previous raw digest differs")
    return path


def _command(snapshot, output, manifest, previous):
    python = canonical_path(manifest["python"])
    common = [str(python), *RUNNER_PYTHON_FLAGS, "-c", CALIBRATION_LAUNCHER, str(snapshot)]
    command = common + ["run", "--mode", "optimization", "--intent", "qualification",
                        "--calibrated-executor", "--baseline", manifest["baseline_root"],
                        "--candidate", manifest["candidate_root"], "--max-cpu-percent", "20",
                        "--calibration", str(output / "calibration.json"),
                        "--output", str(output / "campaign")]
    if manifest["memory_budget_bytes"] is not None:
        command += ["--memory-budget", str(manifest["memory_budget_bytes"])]
    if manifest["partition"] == "RF06":
        command.append("--rf06-split-memory")
    if manifest["partition"] == "RF04-RF03":
        command += ["--target", "rf04-original-target:physical_node_versions_examined"]
    if manifest["partition"] == "RF05":
        command += ["--target", "rf05-first-n1024-p16:physical_node_versions_examined"]
    command += [part for name in manifest["scenarios"] for part in ("--scenario", name)]
    if previous is not None:
        command += ["--previous", str(previous)]
    return [common + ["calibrate", "--output", str(output / "calibration.json")], command]


def _run(snapshot, partition, expected_manifest_hash):
    from bench.harness.campaign import execute_worker

    started = time.monotonic()
    base, raw, manifest = _load_manifest(partition)
    if hashlib.sha256(raw).hexdigest() != expected_manifest_hash:
        raise ValueError("operational manifest changed")
    snapshot = canonical_path(snapshot)
    runner = canonical_path(manifest["runner_root"])
    baseline = canonical_path(manifest["baseline_root"])
    candidate = canonical_path(manifest["candidate_root"])
    if canonical_path(manifest["python"]) != canonical_path(sys.executable):
        raise ValueError("interpreter differs from registered runner")
    before = {"runner": inventory(runner, manifest["runner_revision"]),
              "baseline": inventory(baseline, manifest["baseline_revision"]),
              "candidate": inventory(candidate, manifest["candidate_revision"])}
    verify_snapshot(snapshot, before["runner"])
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    previous = _previous(base, manifest)
    output.mkdir(parents=True, exist_ok=False)
    (output / "operational-manifest.json").write_bytes(raw)
    receipt = {"decision": "incomplete", "partition": partition,
               "manifest_sha256": expected_manifest_hash, "commands": []}
    try:
        for index, command in enumerate(_command(snapshot, output, manifest, previous)):
            remaining = manifest["timeout_seconds"] - (time.monotonic() - started) - 5
            if remaining <= 0:
                raise TimeoutError("qualification deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = execute_worker(command, log, remaining)
            if code and not (index == 1 and code == 2
                             and inspect_result(output / "campaign", manifest)["decision"] == "inconclusive"):
                raise RuntimeError(f"qualification phase {index} failed; artifacts preserved")
        receipt["report"] = inspect_result(output / "campaign", manifest)
        receipt["decision"] = receipt["report"]["decision"]
        if receipt["decision"] not in ("passed", "inconclusive"):
            raise RuntimeError("no complete qualified campaign report")
    except BaseException as failure:
        receipt["decision"] = "failed"
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        try:
            if (inventory(runner, manifest["runner_revision"]) != before["runner"]
                    or inventory(baseline, manifest["baseline_revision"]) != before["baseline"]
                    or inventory(candidate, manifest["candidate_revision"]) != before["candidate"]):
                raise ValueError("source changed during qualification")
            verify_snapshot(snapshot, before["runner"])
        except BaseException:
            receipt["decision"] = "invalid_source"
            raise
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["artifact_sha256"] = {p.relative_to(output).as_posix(): _digest(p)
                                          for p in output.rglob("*") if p.is_file()}
            (output / "runner-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def run(partition):
    """Bound the entire child tree; only a complete passed report satisfies unittest."""
    from bench.harness.campaign import execute_worker

    base, raw, manifest = _load_manifest(partition)
    audit = base / "runs" / ("rf0208-" + partition.lower() + "-supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    manifest_hash = hashlib.sha256(raw).hexdigest()
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.rf0208_operational import _run;"
                "r=_run(pathlib.Path(sys.argv[1]),sys.argv[3],sys.argv[4]);"
                "pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')")
    command = [sys.executable, *RUNNER_PYTHON_FLAGS, "-c", launcher,
               str(Path(__file__).resolve().parents[2]), str(result), partition, manifest_hash]
    receipt = {"decision": "failed", "partition": partition, "command": command,
               "manifest_sha256": manifest_hash}
    try:
        with (audit / "supervisor.log").open("x", encoding="utf-8") as log:
            code = execute_worker(command, log, manifest["timeout_seconds"])
        if code:
            raise RuntimeError("qualification child failed; artifacts preserved at " + str(audit))
        child = load_json(result.read_text(encoding="utf-8"))
        if child.get("decision") != "passed" or child.get("partition") != partition:
            raise RuntimeError("qualification incomplete or inconclusive; artifacts preserved at " + str(audit))
        receipt["decision"] = "passed"
        return child
    finally:
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def validate_report_manifest(manifest):
    required = {"schema_version", "runner_root", "runner_revision", "python", "output",
                "timeout_seconds", "memory_budget_bytes", "phase_outputs", "phase_campaign_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != REPORT_SCHEMA:
        raise ValueError("closed RF04 interaction report manifest required")
    _sha(manifest["runner_revision"], "runner_revision")
    if type(manifest["memory_budget_bytes"]) is not int or not 0 < manifest["memory_budget_bytes"] <= 2**31:
        raise ValueError("RF04 report requires one positive common memory budget")
    if type(manifest["timeout_seconds"]) is not int or not 120 <= manifest["timeout_seconds"] <= 3580:
        raise ValueError("RF04 report deadline must leave Pulse receipt time")
    if set(manifest["phase_outputs"]) != set(RF04_PHASES) or set(manifest["phase_campaign_sha256"]) != set(RF04_PHASES):
        raise ValueError("RF04 report requires all four exact phase outputs and hashes")
    for phase in RF04_PHASES:
        digest = manifest["phase_campaign_sha256"][phase]
        if not isinstance(manifest["phase_outputs"][phase], str) or not isinstance(digest, str) or len(digest) != 64 or any(
            c not in "0123456789abcdef" for c in digest
        ):
            raise ValueError("RF04 phase path/hash invalid")


def _load_report_manifest():
    base = _base()
    raw = canonical_path(base / "rf0208-rf04-report.json").read_bytes()
    manifest = load_json(raw.decode("utf-8"))
    validate_report_manifest(manifest)
    return base, raw, manifest


def _run_report(snapshot, expected_manifest_hash):
    from bench.harness.rf04_qualification import interaction_report

    started = time.monotonic()
    base, raw, manifest = _load_report_manifest()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_hash:
        raise ValueError("RF04 report manifest changed")
    snapshot = canonical_path(snapshot)
    runner = canonical_path(manifest["runner_root"])
    if canonical_path(manifest["python"]) != canonical_path(sys.executable):
        raise ValueError("interpreter differs from authenticated report runner")
    runner_before = inventory(runner, manifest["runner_revision"])
    verify_snapshot(snapshot, runner_before)
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    output.mkdir(parents=True, exist_ok=False)
    (output / "operational-manifest.json").write_bytes(raw)
    receipt = {"decision": "incomplete", "manifest_sha256": expected_manifest_hash,
               "phase_campaign_sha256": manifest["phase_campaign_sha256"]}
    try:
        campaigns = {}
        for phase in RF04_PHASES:
            phase_output = canonical_path(manifest["phase_outputs"][phase])
            phase_output.relative_to(base / "runs")
            prior = load_json((phase_output / "operational-manifest.json").read_text(encoding="utf-8"))
            validate_manifest(prior, "RF04-" + phase.upper())
            if (prior["runner_revision"] != manifest["runner_revision"]
                    or prior["runner_root"] != manifest["runner_root"]
                    or prior["memory_budget_bytes"] != manifest["memory_budget_bytes"]):
                raise ValueError("RF04 phase differs from report runner or budget")
            prior_receipt = load_json((phase_output / "runner-receipt.json").read_text(encoding="utf-8"))
            campaign_path = phase_output / "campaign/campaign.json"
            if (prior_receipt.get("decision") != "passed"
                    or _digest(campaign_path) != manifest["phase_campaign_sha256"][phase]
                    or inspect_result(phase_output / "campaign", prior)["decision"] != "passed"):
                raise ValueError("RF04 phase lacks complete passing campaign")
            for name, digest in prior_receipt["artifact_sha256"].items():
                artifact = canonical_path(phase_output / name)
                artifact.relative_to(phase_output)
                if _digest(artifact) != digest:
                    raise ValueError("RF04 phase artifact changed")
            campaigns[phase] = load_json(campaign_path.read_text(encoding="utf-8"))
        joined = interaction_report(campaigns)
        if joined["source_revisions"]["both"] != manifest["runner_revision"]:
            raise ValueError("RF04 combined source is not current authenticated revision")
        if joined["decision"] != "qualified":
            raise ValueError("RF04 four-phase interaction is not qualified")
        (output / "interaction-report.json").write_text(json.dumps(joined, indent=2), encoding="utf-8")
        receipt["decision"] = "qualified"
        receipt["report"] = joined
    except BaseException as failure:
        receipt["decision"] = "failed"
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        try:
            if inventory(runner, manifest["runner_revision"]) != runner_before:
                raise ValueError("RF04 report runner source changed")
            verify_snapshot(snapshot, runner_before)
        except BaseException:
            receipt["decision"] = "invalid_source"
            raise
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["artifact_sha256"] = {p.relative_to(output).as_posix(): _digest(p)
                                          for p in output.rglob("*") if p.is_file()}
            (output / "runner-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def run_report():
    """Authenticate the complete four-source interaction without rerunning timing."""
    from bench.harness.campaign import execute_worker

    base, raw, manifest = _load_report_manifest()
    audit = base / "runs" / ("rf0208-rf04-report-supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    manifest_hash = hashlib.sha256(raw).hexdigest()
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.rf0208_operational import _run_report;"
                "r=_run_report(pathlib.Path(sys.argv[1]),sys.argv[3]);"
                "pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')")
    command = [sys.executable, *RUNNER_PYTHON_FLAGS, "-c", launcher,
               str(Path(__file__).resolve().parents[2]), str(result), manifest_hash]
    receipt = {"decision": "failed", "command": command, "manifest_sha256": manifest_hash}
    try:
        with (audit / "supervisor.log").open("x", encoding="utf-8") as log:
            code = execute_worker(command, log, manifest["timeout_seconds"])
        if code:
            raise RuntimeError("RF04 interaction child failed; artifacts preserved at " + str(audit))
        child = load_json(result.read_text(encoding="utf-8"))
        if child.get("decision") != "qualified":
            raise RuntimeError("RF04 interaction not qualified; artifacts preserved at " + str(audit))
        receipt["decision"] = "qualified"
        return child
    finally:
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
