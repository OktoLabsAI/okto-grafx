"""Explicit native-runner bridge; operational inputs are never test fixtures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import time
import uuid

BASELINE = "3a2239a0bda80223c298a68d6d14f3c951f133d2"
MINIMUM_CONTROLS = frozenset({"constant", "reader-short-churn0", "edges-n1024-uniform-x1"})
PHYSICAL_COVERAGE = (
    "physical_node_versions_examined", "physical_edge_versions_examined",
    "index_probe_keys", "certificate_attempts", "landing_candidates", "landing_batches",
)
SEPARATE_LANDING_COVERAGE = ("landing_candidates", "landing_batches")


def validate_controls(manifest):
    """Require an explicit partition of the current catalog, never infer N/A.

    controls contains {scenario, rationale}; not_applicable contains
    {scenarios: [names], rationale}. Grouped omissions may share a rationale,
    but every omitted name must occur exactly once. Reasons are auditable
    author statements, not evidence that an instrumentation path is covered.
    """
    from bench.harness.campaign_fixtures import matrix
    catalog = {scenario.name for scenario in matrix()}
    controls = manifest.get("controls")
    omissions = manifest.get("not_applicable")
    if not isinstance(controls, list) or not controls or not isinstance(omissions, list):
        raise ValueError("explicit controls and not_applicable lists required")
    selected, excluded = [], []
    for records, keys, destination, field in (
        (controls, {"scenario", "rationale"}, selected, "scenario"),
        (omissions, {"scenarios", "rationale"}, excluded, "scenarios"),
    ):
        for record in records:
            if not isinstance(record, dict) or set(record) != keys:
                raise ValueError("invalid control selection record")
            if not isinstance(record["rationale"], str) or not record["rationale"].strip():
                raise ValueError("explicit control/omission rationale required")
            names = [record[field]] if field == "scenario" else record[field]
            if not isinstance(names, list) or not names:
                raise ValueError("nonempty omitted scenario list required")
            for name in names:
                if not isinstance(name, str) or name not in catalog or name in destination:
                    raise ValueError("unknown or duplicate scenario")
                destination.append(name)
    if not MINIMUM_CONTROLS <= set(selected):
        raise ValueError("minimum statement, cursor and edge controls required")
    if set(selected) & set(excluded) or set(selected) | set(excluded) != catalog:
        raise ValueError("selected and omitted scenarios must partition the catalog")
    return selected


def control_observations(raw):
    """Coverage comes from retained candidate work, not a fixture/plan name.

    These integer sums describe observed work only; they are not memory peaks
    or timing aggregates. Uncovered paths remain blockers even with N/A prose.
    """
    counts = dict.fromkeys(PHYSICAL_COVERAGE, 0)
    by_scenario = {}
    for run in raw["runs"]:
        if run["arm"] != "B":
            continue
        for sample in run["samples"]:
            if sample["kind"] != "retained":
                continue
            observed = by_scenario.setdefault(sample.get("scenario_id", "unknown"), dict.fromkeys(counts, 0))
            for name in counts:
                value = sample["statistics"].get(name, 0)
                if type(value) is not int or value < 0:
                    raise ValueError("physical coverage requires nonnegative integer counters")
                counts[name] += value
                observed[name] += value
    return {"counters": counts, "by_scenario": by_scenario,
            "uncovered_instrumentation": [name for name, count in counts.items()
                                          if count == 0 and name not in SEPARATE_LANDING_COVERAGE],
            "separate_evidence_required": list(SEPARATE_LANDING_COVERAGE),
            "separation_reason": "The historical campaign requires an explicit memory budget; "
                                 "the existing executor guard disables batched landings with that budget. "
                                 "Landing qualification requires a separately reviewed method; it is not N/A."}


def canonical_path(value):
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError("absolute canonical path required")
    for component in (path, *path.parents):
        if component.exists():
            info = component.lstat()
            if component.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse paths are not accepted")
    return path


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=10)


def inventory(root, revision):
    """Hash every committed blob, independent of checkout EOL conversion.

    ls-tree plus one cat-file batch includes export-ignore files (unlike archive)
    and avoids one subprocess or repeated ancestor resolution per tracked file.
    Clean status separately attests the checkout against Git's filter semantics.
    """
    root = canonical_path(root)
    if git(root, "rev-parse", "HEAD").decode().strip() != revision:
        raise ValueError("revision mismatch")
    if git(root, "status", "--porcelain"):
        raise ValueError("dirty source")
    entries = []
    checked = set()
    for record in git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("unsupported tracked entry (symlink/submodule)")
        name = raw_name.decode("utf-8")
        _tracked_path(root, name, checked)
        entries.append((name, oid))
    payload = subprocess.check_output(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=b"".join(oid + b"\n" for _, oid in entries), timeout=10,
    )
    result = {}
    offset = 0
    for name, oid in entries:
        end = payload.index(b"\n", offset)
        actual_oid, kind, size = payload[offset:end].split()
        if actual_oid != oid or kind != b"blob":
            raise ValueError("unexpected Git batch object")
        start = end + 1
        offset = start + int(size)
        if payload[offset:offset + 1] != b"\n":
            raise ValueError("truncated Git batch object")
        result[name] = hashlib.sha256(memoryview(payload)[start:offset]).hexdigest()
        offset += 1
    if offset != len(payload):
        raise ValueError("unexpected Git batch trailer")
    if not result:
        raise ValueError("empty tracked source")
    return result


def _tracked_path(root, name, checked):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise ValueError("invalid tracked path")
    path = root
    for part in relative.parts:
        path = path / part
        if path not in checked:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse paths are not accepted")
            checked.add(path)
    return path


def verify_snapshot(snapshot, tracked):
    snapshot = canonical_path(snapshot)
    checked = set()
    for name, expected in tracked.items():
        path = _tracked_path(snapshot, name, checked)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("runner snapshot differs from candidate: " + name)


def run(snapshot):
    """Supervise *all* qualification phases below the native 120-second cap.

    Inventories, filesystem hashing and final receipts run in the owned child,
    so they cannot outlive the 105-second deadline. Timeout leaves an explicit
    failed supervisor receipt and any partial child artifacts, never a pass.
    """
    from bench.harness.campaign import execute_worker
    started = time.monotonic()
    snapshot = canonical_path(snapshot)
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    audit = base / "runs" / ("supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.pulse_qualification import _run;"
                "r=_run(pathlib.Path(sys.argv[1]));"
                "pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')")
    command = [sys.executable, "-I", "-c", launcher, str(snapshot), str(result)]
    receipt = {"decision": "failed", "command": command, "timeout_seconds": 105}
    try:
        remaining = 105 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("supervisor setup exhausted deadline")
        with (audit / "supervisor.log").open("x", encoding="utf-8") as log:
            code = execute_worker(command, log, remaining)
        if code:
            raise RuntimeError(f"qualification child exited {code}; see {audit}")
        child = json.loads(result.read_text(encoding="utf-8"))
        if child.get("decision") != "passed":
            raise RuntimeError("qualification child did not pass")
        if time.monotonic() - started >= 110:
            raise TimeoutError("qualification exceeded supervisor deadline")
        receipt["decision"] = "passed"
        return child
    except BaseException as failure:
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def _run(snapshot):
    """Run calibration plus six real workers, or fail with preserved artifacts.

    A 90-second execution deadline reserves room below the native 120-second
    cap for process-tree cleanup and provenance checks. It is not a claim that
    any host can complete the campaign within that cap.
    """
    from bench.harness.campaign import execute_worker
    from bench.harness import campaign, campaign_schema

    snapshot = canonical_path(snapshot)
    for module in (campaign, campaign_schema):
        canonical_path(module.__file__).relative_to(snapshot)

    started = time.monotonic()
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    manifest_path = canonical_path(base / "rf01-qualification.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = campaign_schema.load_json(manifest_bytes.decode("utf-8"))
    if manifest.get("schema_version") != "grafx-pulse-qualification/v2":
        raise ValueError("unsupported operational manifest")
    if set(manifest) != {"schema_version", "candidate_root", "baseline_root", "python",
                         "candidate_revision", "output", "controls", "not_applicable"}:
        raise ValueError("unknown or missing operational manifest field")
    selected = validate_controls(manifest)
    candidate = canonical_path(manifest["candidate_root"])
    baseline = canonical_path(manifest["baseline_root"])
    python = canonical_path(manifest["python"])
    if python != canonical_path(sys.executable):
        raise ValueError("operational interpreter differs from authenticated runner")
    revision = manifest["candidate_revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("exact candidate SHA required")
    before = inventory(candidate, revision)
    baseline_before = inventory(baseline, BASELINE)
    verify_snapshot(snapshot, before)
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
               "candidate_revision": revision, "baseline_revision": BASELINE,
               "decision": "incomplete", "commands": [], "controls": manifest["controls"],
               "not_applicable": manifest["not_applicable"]}
    try:
        # Isolated interpreter imports the actual runner snapshot's harness;
        # each worker independently attests the requested checkout import.
        launcher = campaign_schema.CALIBRATION_LAUNCHER
        common = [str(python), "-I", "-c", launcher, str(snapshot)]
        calibration = output / "calibration.json"
        commands = [common + ["calibrate", "--output", str(calibration)],
                    common + ["run", "--historical-noop", "--mode", "noop", "--intent", "qualification",
                              "--baseline", str(baseline), "--candidate", str(candidate),
                              "--memory-budget", "4096", "--max-cpu-percent", "20",
                              *[part for name in selected for part in ("--scenario", name)],
                              "--calibrated-executor",
                              "--calibration", str(calibration), "--output", str(output / "campaign")]]
        for index, command in enumerate(commands):
            remaining = 90 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("qualification deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = execute_worker(command, log, remaining)
            if code:
                raise RuntimeError(f"qualification phase {index} exited {code}; artifacts preserved")
            if index == 0:
                actual = campaign_schema.load_json(calibration.read_text(encoding="utf-8"))
                if actual["procedure"]["command"] != command:
                    raise ValueError("calibration interpreter argv differs from launched command")
        from bench.harness.campaign_schema import compare_campaign, load_json
        raw = load_json((output / "campaign" / "campaign.json").read_text(encoding="utf-8"))
        report = compare_campaign(raw)
        receipt["temporal_decision"] = report.get("decision")
        if [scenario["name"] for scenario in raw["scenarios"]] != selected:
            raise ValueError("campaign controls differ from operational selection")
        receipt["physical_coverage"] = control_observations(raw)
        if receipt["physical_coverage"]["uncovered_instrumentation"]:
            raise ValueError("uncovered physical instrumentation: " + ", ".join(
                receipt["physical_coverage"]["uncovered_instrumentation"]))
        if report.get("decision") != "passed":
            raise RuntimeError("campaign did not qualify")
        # A passing budgeted comparison cannot exercise batched landings, whose
        # existing eligibility guard rejects a configured query memory budget.
        # Preserve the measured temporal verdict without granting full RF01
        # qualification until the separate verification method is resolved.
        receipt["decision"] = "incomplete"
        receipt["qualification_pending"] = receipt["physical_coverage"]["separate_evidence_required"]
    except BaseException as failure:
        receipt["decision"] = "failed"
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        try:
            if inventory(candidate, revision) != before or inventory(baseline, BASELINE) != baseline_before:
                raise ValueError("source changed during qualification")
            verify_snapshot(snapshot, before)
        except BaseException:
            receipt["decision"] = "invalid_source"
            raise
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["artifact_sha256"] = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in output.rglob("*") if p.is_file()}
            (output / "runner-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt
