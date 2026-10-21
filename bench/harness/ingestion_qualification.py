"""Explicit RF09 runner bridge; operational configuration is never tracked.

LOCALAPPDATA/Okto/Grafx/rf09-runner.json selects immutable sources and a new
output below runs/. Both revisions must supply real sequence memory observation.
No missing proof, timeout or incomplete campaign can produce a passed receipt.
This bridge supports the first whole campaign; repeat authorization remains a
separate planning action and must not be achieved by selecting favorable raw.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from bench.harness.campaign_fixtures import ingestion_matrix
from bench.harness.pulse_qualification import canonical_path, inventory, verify_snapshot


def validate_manifest(manifest):
    if manifest.get("schema_version") != "grafx-rf09-runner/v1":
        raise ValueError("RF09 manifest version required")
    for key in ("baseline_revision", "candidate_revision"):
        value = manifest.get(key)
        if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("exact immutable SHA required")
    for key, limit in (("memory_budget_bytes", 2**31), ("timeout_seconds", 3600)):
        value = manifest.get(key)
        if type(value) is not int or not 0 < value <= limit:
            raise ValueError("invalid governed " + key)
    names = manifest.get("scenarios")
    catalog = {s.name: s for s in ingestion_matrix()}
    if not isinstance(names, list) or not names or len(names) != len(set(names)) or any(n not in catalog for n in names):
        raise ValueError("invalid representative scenario selection")
    selected = [catalog[n] for n in names]
    for field, values in (("write_shape", {"unwind", "statements", "executemany", "chunks", "transactions"}),
                          ("write_rows", {1, 32, 128, 512}), ("payload_bytes", {64, 4096}), ("writers", {1, 2, 4})):
        if {getattr(s, field) for s in selected} != values:
            raise ValueError("representative matrix does not cover " + field)


def _manifest():
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    raw = canonical_path(base / "rf09-runner.json").read_bytes()
    manifest = json.loads(raw)
    validate_manifest(manifest)
    return base, raw, manifest


def inspect_result(output, manifest):
    from bench.harness.campaign_schema import compare_campaign, load_json
    raw = load_json((output / "campaign.json").read_text(encoding="utf-8"))
    report = compare_campaign(raw)
    if report != load_json((output / "report.json").read_text(encoding="utf-8")):
        raise ValueError("stored report differs from complete raw")
    if raw["mode"] != "recipe" or raw["intent"] != "qualification":
        raise ValueError("recipe qualification required")
    if [s["name"] for s in raw["scenarios"]] != manifest["scenarios"] or any(
        s["query_memory_budget_bytes"] != manifest["memory_budget_bytes"] for s in raw["scenarios"]
    ):
        raise ValueError("matrix or budget differs from operational manifest")
    if raw["protocol"]["attempt"] != 1 or raw["protocol"]["previous_campaign_sha256"] is not None:
        raise ValueError("first whole campaign required")
    calibration = (output.parent / "calibration.json").read_text(encoding="utf-8")
    if raw["calibration"]["raw_json"] != calibration or raw["calibration"]["sha256"] != hashlib.sha256(calibration.encode()).hexdigest():
        raise ValueError("calibration differs from operational run")
    for run in raw["runs"]:
        arm = "baseline" if run["arm"] == "A" else "candidate"
        if run["revision"] != manifest[arm + "_revision"] or run["source_root"] != manifest[arm + "_root"]:
            raise ValueError("worker source differs from operational run")
        if canonical_path(run["package_file"]) != canonical_path(Path(run["source_root"]) / "src/okto_grafx/__init__.py"):
            raise ValueError("worker import differs from selected source")
        worker = load_json((output / f"raw-{run['pair']}-{run['arm']}.json").read_text(encoding="utf-8"))
        if worker != run:
            raise ValueError("worker raw differs from assembled campaign")
    return report


def _run(snapshot, expected_manifest_hash):
    from bench.harness import campaign, campaign_schema
    started = time.monotonic()
    snapshot = canonical_path(snapshot)
    for module in (campaign, campaign_schema):
        canonical_path(module.__file__).relative_to(snapshot)
    base, raw, manifest = _manifest()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_hash:
        raise ValueError("operational manifest changed")
    candidate, baseline = canonical_path(manifest["candidate_root"]), canonical_path(manifest["baseline_root"])
    python = canonical_path(manifest["python"])
    if python != canonical_path(sys.executable):
        raise ValueError("interpreter differs from authenticated runner")
    before = inventory(candidate, manifest["candidate_revision"])
    baseline_before = inventory(baseline, manifest["baseline_revision"])
    verify_snapshot(snapshot, before)
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    output.mkdir(parents=True, exist_ok=False)
    (output / "operational-manifest.json").write_bytes(raw)
    receipt = {"decision": "incomplete", "manifest_sha256": expected_manifest_hash, "commands": []}
    common = [str(python), *campaign_schema.RUNNER_PYTHON_FLAGS, "-c", campaign_schema.CALIBRATION_LAUNCHER, str(snapshot)]
    commands = [common + ["calibrate", "--output", str(output / "calibration.json")],
                common + ["run", "--mode", "recipe", "--intent", "qualification", "--calibrated-executor",
                          "--baseline", str(baseline), "--candidate", str(candidate),
                          "--memory-budget", str(manifest["memory_budget_bytes"]), "--max-cpu-percent", "20",
                          "--calibration", str(output / "calibration.json"), "--output", str(output / "campaign"),
                          *[part for name in manifest["scenarios"] for part in ("--scenario", name)]]]
    try:
        for index, command in enumerate(commands):
            remaining = manifest["timeout_seconds"] - (time.monotonic() - started) - 5
            if remaining <= 0:
                raise TimeoutError("RF09 deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = campaign.execute_worker(command, log, remaining)
            if code:
                raise RuntimeError(f"RF09 phase {index} exited {code}; artifacts preserved")
        receipt["report"] = inspect_result(output / "campaign", manifest)
        receipt["decision"] = receipt["report"]["decision"]
        if receipt["decision"] != "tradeoff_reported":
            raise RuntimeError("RF09 did not produce complete qualified tradeoff evidence")
    except BaseException as failure:
        receipt["decision"] = "failed"
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        try:
            if inventory(candidate, manifest["candidate_revision"]) != before or inventory(baseline, manifest["baseline_revision"]) != baseline_before:
                raise ValueError("source changed during campaign")
            verify_snapshot(snapshot, before)
        except BaseException:
            receipt["decision"] = "invalid_source"
            raise
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["artifact_sha256"] = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in output.rglob("*") if p.is_file()}
            (output / "runner-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def run(snapshot):
    """Bound the entire child tree, including preflight and postflight."""
    from bench.harness.campaign import execute_worker
    from bench.harness.campaign_schema import RUNNER_PYTHON_FLAGS
    base, raw, manifest = _manifest()
    snapshot = canonical_path(snapshot)
    audit = base / "runs" / ("rf09-supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    manifest_hash = hashlib.sha256(raw).hexdigest()
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.ingestion_qualification import _run;"
                "r=_run(pathlib.Path(sys.argv[1]),sys.argv[3]);"
                "pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')")
    command = [sys.executable, *RUNNER_PYTHON_FLAGS, "-c", launcher, str(snapshot), str(result), manifest_hash]
    receipt = {"decision": "failed", "command": command, "manifest_sha256": manifest_hash}
    try:
        with (audit / "supervisor.log").open("x", encoding="utf-8") as log:
            code = execute_worker(command, log, manifest["timeout_seconds"])
        if code:
            raise RuntimeError("RF09 child failed; artifacts preserved at " + str(audit))
        child = json.loads(result.read_text(encoding="utf-8"))
        if child.get("decision") != "tradeoff_reported":
            raise RuntimeError("RF09 child did not qualify")
        receipt["decision"] = "tradeoff_reported"
        return child
    finally:
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
