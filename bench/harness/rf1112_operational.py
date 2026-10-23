"""Closed, bounded Pulse bridge for RF11 P1-P5 and RF12 Q1-Q5.

The authenticated runner snapshot contains this supervisor. The measured engine
sources are separate, clean, immutable checkouts declared in a private manifest.
One invocation proves one partition; no partition can credit another.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from bench.harness.campaign_fixtures import matrix
from bench.harness.campaign_schema import RUNNER_PYTHON_FLAGS, CALIBRATION_LAUNCHER
from bench.harness.campaign_schema import compare_campaign, load_json
from bench.harness.pulse_qualification import canonical_path, inventory, verify_snapshot


P1 = [f"fts-matrix-n256-{mode}-h0-{term}-{cache}"
      for mode in ("wal", "durable") for term in ("rare", "common")
      for cache in ("warm", "reopen")] + ["fts-matrix-wal-delta-n256"]
P2 = [f"fts-matrix-retained-h{history}" for history in (0, 1, 8, 32)]
P3 = ["fts-matrix-n4096-wal-h0-rare-reopen",
      "fts-matrix-n4096-durable-h8-rare-reopen"]
P4 = ["fts-matrix-n65536-wal-h0-rare-reopen",
      "fts-matrix-n65536-durable-h32-rare-reopen"]
P5 = [f"fts-matrix-{mode}-{case}-n256" for mode in ("wal", "durable")
      for case in ("insert", "update", "delete", "external_mixed")]
Q1 = [f"rf12-{case}-buf64-cp512-lease64-batch32"
      for case in ("retained", "released", "external")]
Q2 = [f"rf12-released-buf{buffer}-cp512-lease64-batch32"
      for buffer in (16, 64, 256)]
Q3 = [f"rf12-released-buf64-cp{interval}-lease64-batch32"
      for interval in (128, 512, 2048)]
Q4 = [f"rf12-external-buf64-cp512-lease{lease}-batch32"
      for lease in (1, 32, 64, 128, 512)]
Q5 = [f"rf12-external-total512-lease64-txbatch{batch}"
      for batch in (1, 32, 128, 512)]
PARTITIONS = {"P1": P1, "P2": P2, "P3": P3, "P4": P4, "P5": P5,
              "Q1": Q1, "Q2": Q2, "Q3": Q3, "Q4": Q4, "Q5": Q5}
SCHEMA = "grafx-rf1112-runner/v1"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest(manifest, partition):
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "partition", "runner_root", "runner_revision",
        "baseline_root", "baseline_revision", "candidate_root",
        "candidate_revision", "python", "output", "timeout_seconds",
        "scenarios", "attempt", "previous_output", "previous_campaign_sha256",
    }:
        raise ValueError("closed RF11/RF12 operational manifest required")
    if manifest["schema_version"] != SCHEMA or partition not in PARTITIONS:
        raise ValueError("RF11/RF12 partition or schema mismatch")
    if manifest["partition"] != partition or manifest["scenarios"] != PARTITIONS[partition]:
        raise ValueError("partition must select exact canonical scenarios in order")
    catalog = {s.name: s for s in matrix()}
    if any(name not in catalog for name in PARTITIONS[partition]):
        raise ValueError("closed partition differs from fixture catalog")
    for key in ("runner_revision", "baseline_revision", "candidate_revision"):
        value = manifest[key]
        if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("immutable full SHA required: " + key)
    if manifest["baseline_revision"] == manifest["candidate_revision"]:
        raise ValueError("baseline and candidate must differ")
    timeout = manifest["timeout_seconds"]
    # Leave 20 s for Pulse to collect and validate the unittest process receipt.
    if type(timeout) is not int or not 120 <= timeout <= 3580:
        raise ValueError("partition supervisor timeout must be 120..3580 seconds")
    if manifest["attempt"] not in (1, 2) or type(manifest["attempt"]) is not int:
        raise ValueError("only one complete retry is allowed")
    previous = manifest["previous_campaign_sha256"]
    if manifest["attempt"] == 1:
        if manifest["previous_output"] is not None or previous is not None:
            raise ValueError("first attempt cannot claim prior evidence")
    elif (not isinstance(previous, str) or len(previous) != 64
          or any(c not in "0123456789abcdef" for c in previous)
          or not isinstance(manifest["previous_output"], str)):
        raise ValueError("retry requires complete previous evidence hash/path")
    if partition.startswith("P"):
        if any(catalog[name].query_memory_budget_bytes is not None for name in manifest["scenarios"]):
            raise ValueError("native FTS logical memory must remain unavailable")
    elif any(catalog[name].query_memory_budget_bytes != 1 << 20 for name in manifest["scenarios"]):
        raise ValueError("RF12 requires each 1 MiB query quota")


def _load_manifest(partition):
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    raw = canonical_path(base / f"rf1112-{partition.lower()}-runner.json").read_bytes()
    manifest = load_json(raw.decode("utf-8"))
    validate_manifest(manifest, partition)
    return base, raw, manifest


def inspect_result(output, manifest):
    raw_path = output / "campaign.json"
    raw = load_json(raw_path.read_text(encoding="utf-8"))
    report = compare_campaign(raw)
    if report != load_json((output / "report.json").read_text(encoding="utf-8")):
        raise ValueError("stored report differs from complete raw")
    if raw["mode"] != "recipe" or raw["intent"] != "qualification":
        raise ValueError("recipe qualification required")
    catalog = {s.name: s.to_dict() for s in matrix()}
    if raw["scenarios"] != [catalog[name] for name in manifest["scenarios"]]:
        raise ValueError("scenario/configuration differs from closed partition")
    protocol = raw["protocol"]
    if (protocol["attempt"] != manifest["attempt"]
            or protocol["previous_campaign_sha256"] != manifest["previous_campaign_sha256"]
            or protocol["max_cpu_percent"] != 20):
        raise ValueError("campaign retry or CPU policy differs from manifest")
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
    old_manifest = previous / "operational-manifest.json"
    old = load_json(old_manifest.read_text(encoding="utf-8"))
    validate_manifest(old, manifest["partition"])
    if old["attempt"] != 1:
        raise ValueError("cannot retry a prior retry")
    for key in ("partition", "runner_root", "runner_revision", "baseline_root", "baseline_revision",
                "candidate_root", "candidate_revision", "python", "timeout_seconds", "scenarios"):
        if old[key] != manifest[key]:
            raise ValueError("retry changed " + key)
    receipt = load_json((previous / "runner-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("manifest_sha256") != _digest(old_manifest):
        raise ValueError("previous manifest digest differs")
    if (receipt.get("partition") != manifest["partition"]
            or receipt.get("decision") != "inconclusive"
            or inspect_result(previous / "campaign", old)["decision"] != "inconclusive"):
        raise ValueError("retry requires complete inconclusive partition")
    expected_artifacts = receipt.get("artifact_sha256")
    actual_artifacts = {path.relative_to(previous).as_posix() for path in previous.rglob("*")
                        if path.is_file() and path != previous / "runner-receipt.json"}
    if (not isinstance(expected_artifacts, dict) or not expected_artifacts
            or set(expected_artifacts) != actual_artifacts):
        raise ValueError("previous artifact inventory differs")
    for name, digest in expected_artifacts.items():
        artifact = canonical_path(previous / name)
        artifact.relative_to(previous)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or _digest(artifact) != digest):
            raise ValueError("previous artifact changed")
    path = previous / "campaign/campaign.json"
    if _digest(path) != manifest["previous_campaign_sha256"]:
        raise ValueError("previous raw digest differs")
    return path


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
    python = canonical_path(manifest["python"])
    if python != canonical_path(Path(sys.executable).resolve()):
        raise ValueError("interpreter differs from registered runner")
    runner_before = inventory(runner, manifest["runner_revision"])
    baseline_before = inventory(baseline, manifest["baseline_revision"])
    candidate_before = inventory(candidate, manifest["candidate_revision"])
    verify_snapshot(snapshot, runner_before)
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    previous = _previous(base, manifest)
    output.mkdir(parents=True, exist_ok=False)
    (output / "operational-manifest.json").write_bytes(raw)
    receipt = {"decision": "incomplete", "partition": partition,
               "manifest_sha256": expected_manifest_hash, "commands": []}
    common = [str(python), *RUNNER_PYTHON_FLAGS, "-c", CALIBRATION_LAUNCHER, str(snapshot)]
    commands = [common + ["calibrate", "--output", str(output / "calibration.json")],
                common + ["run", "--mode", "recipe", "--intent", "qualification",
                          "--calibrated-executor", "--baseline", str(baseline),
                          "--candidate", str(candidate), "--max-cpu-percent", "20",
                          "--calibration", str(output / "calibration.json"),
                          "--output", str(output / "campaign"),
                          *[part for name in manifest["scenarios"] for part in ("--scenario", name)]]]
    if previous is not None:
        commands[1].extend(("--previous", str(previous)))
    try:
        for index, command in enumerate(commands):
            remaining = manifest["timeout_seconds"] - (time.monotonic() - started) - 5
            if remaining <= 0:
                raise TimeoutError("partition deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = execute_worker(command, log, remaining)
            if code and not (index == 1 and code == 2
                             and inspect_result(output / "campaign", manifest)["decision"] == "inconclusive"):
                raise RuntimeError(f"RF11/RF12 phase {index} failed; artifacts preserved")
        receipt["report"] = inspect_result(output / "campaign", manifest)
        receipt["decision"] = receipt["report"]["decision"]
        if receipt["decision"] not in ("tradeoff_reported", "inconclusive"):
            raise RuntimeError("partition produced no complete qualified report")
    except BaseException as failure:
        receipt["decision"] = "failed"
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        try:
            if (inventory(runner, manifest["runner_revision"]) != runner_before
                    or inventory(baseline, manifest["baseline_revision"]) != baseline_before
                    or inventory(candidate, manifest["candidate_revision"]) != candidate_before):
                raise ValueError("source changed during partition")
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


def run(partition):
    """Run one partition under an outer process-tree deadline."""
    from bench.harness.campaign import execute_worker

    base, raw, manifest = _load_manifest(partition)
    audit = base / "runs" / ("rf1112-" + partition.lower() + "-supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    manifest_hash = hashlib.sha256(raw).hexdigest()
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.rf1112_operational import _run;"
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
            raise RuntimeError("partition child failed; artifacts preserved at " + str(audit))
        child = load_json(result.read_text(encoding="utf-8"))
        if child.get("decision") != "tradeoff_reported":
            raise RuntimeError("partition remains inconclusive; artifacts preserved at " + str(audit))
        receipt["decision"] = child["decision"]
        return child
    finally:
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
