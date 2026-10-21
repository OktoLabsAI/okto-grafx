"""Explicit RF09 runner bridge; operational configuration is never tracked.

LOCALAPPDATA/Okto/Grafx/rf09-runner.json selects immutable sources and a new
output below runs/. Both revisions must supply real sequence memory observation.
No missing proof, timeout or incomplete campaign can produce a passed receipt.
One explicit whole repeat may follow a complete inconclusive campaign, preserving
the previous raw and matrix. Failed/incomplete evidence cannot authorize a repeat.
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
    attempt = manifest.get("attempt", 1)
    if type(attempt) is not int or attempt not in (1, 2):
        raise ValueError("at most one whole repeat is allowed")
    if attempt == 2:
        digest = manifest.get("previous_campaign_sha256", "")
        if not manifest.get("previous_output") or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("repeat requires previous output and exact raw hash")
    elif manifest.get("previous_output") is not None or manifest.get("previous_campaign_sha256") is not None:
        raise ValueError("first attempt cannot select previous evidence")
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
    # A comparison of recipes needs matched records, payload and contention.
    # Cover every shape at the central workload and compare at least two shapes
    # at every other selected workload. Do not accept a list whose five shapes
    # each occur under a different load.
    by_load = {}
    for scenario in selected:
        load = (scenario.write_rows, scenario.payload_bytes, scenario.writers)
        by_load.setdefault(load, set()).add(scenario.write_shape)
    shapes = {"unwind", "statements", "executemany", "chunks", "transactions"}
    if by_load.get((32, 64, 1)) != shapes or any(len(group) < 2 for group in by_load.values()):
        raise ValueError("RF09 requires paired write shapes at every load and all shapes at 32/64/1")


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
    if raw["protocol"]["attempt"] != manifest.get("attempt", 1) or raw["protocol"]["previous_campaign_sha256"] != manifest.get("previous_campaign_sha256"):
        raise ValueError("whole campaign retry lineage differs from manifest")
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


def verify_previous(base, manifest):
    if manifest.get("attempt", 1) == 1:
        return None
    previous = canonical_path(manifest["previous_output"])
    previous.relative_to(base / "runs")
    old = json.loads((previous / "operational-manifest.json").read_text(encoding="utf-8"))
    validate_manifest(old)
    if old.get("attempt", 1) != 1:
        raise ValueError("cannot repeat a second attempt")
    for field in ("candidate_revision", "baseline_revision", "candidate_root", "baseline_root", "python", "memory_budget_bytes", "scenarios"):
        if manifest[field] != old[field]:
            raise ValueError("repeat changed " + field)
    receipt = json.loads((previous / "runner-receipt.json").read_text(encoding="utf-8"))
    if receipt.get("decision") != "inconclusive" or inspect_result(previous / "campaign", old)["decision"] != "inconclusive":
        raise ValueError("repeat requires complete inconclusive evidence")
    for name, digest in receipt["artifact_sha256"].items():
        artifact = canonical_path(previous / name)
        artifact.relative_to(previous)
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError("previous artifact changed")
    raw = canonical_path(previous / "campaign/campaign.json")
    if hashlib.sha256(raw.read_bytes()).hexdigest() != manifest["previous_campaign_sha256"]:
        raise ValueError("previous campaign hash differs")
    return raw


def phase_result(code, index, output, manifest):
    if code == 0:
        return
    if index == 1 and code == 2 and inspect_result(output / "campaign", manifest)["decision"] == "inconclusive":
        return
    raise RuntimeError(f"RF09 phase {index} exited {code}; artifacts preserved")


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
    previous = verify_previous(base, manifest)
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
    if previous is not None:
        commands[1].extend(("--previous", str(previous)))
    try:
        for index, command in enumerate(commands):
            remaining = manifest["timeout_seconds"] - (time.monotonic() - started) - 5
            if remaining <= 0:
                raise TimeoutError("RF09 deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = campaign.execute_worker(command, log, remaining)
            phase_result(code, index, output, manifest)
        receipt["report"] = inspect_result(output / "campaign", manifest)
        receipt["decision"] = receipt["report"]["decision"]
        if receipt["decision"] not in ("tradeoff_reported", "inconclusive"):
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
        if child.get("decision") not in ("tradeoff_reported", "inconclusive"):
            raise RuntimeError("RF09 child did not qualify")
        receipt["decision"] = child["decision"]
        return child
    finally:
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
