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
MINIMUM_CONTROLS = frozenset({"constant", "reader-short-churn0", "edges-n1024-uniform-x1",
                              "pk-seek-control", "landing-control"})
LANDING_CONTROL = "landing-control"
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
        raise ValueError("minimum statement, cursor, edge, seek and landing controls required")
    if set(selected) & set(excluded) or set(selected) | set(excluded) != catalog:
        raise ValueError("selected and omitted scenarios must partition the catalog")
    return selected


def partition_controls(selected):
    """Landing has no query quota; every other selected control keeps one."""
    if selected.count(LANDING_CONTROL) != 1:
        raise ValueError("exactly one landing control required")
    return [name for name in selected if name != LANDING_CONTROL], [LANDING_CONTROL]


def _positive_budget(value, label):
    if type(value) is not int or not 1 <= value <= 64 * 1024 * 1024:
        raise ValueError(f"{label} must be a positive bounded integer")
    return value


def _deadline(value):
    # The host profile is separately registered by a local operator. This cap
    # only narrows that authority; the manifest cannot enlarge a Pulse profile.
    if type(value) is not int or not 120 <= value <= 1800:
        raise ValueError("qualification deadline must be 120..1800 seconds")
    return value


def load_operational_manifest(base):
    from bench.harness.campaign_schema import load_json

    path = canonical_path(base / "rf01-qualification.json")
    payload = path.read_bytes()
    manifest = load_json(payload.decode("utf-8"))
    expected = {"schema_version", "candidate_root", "baseline_root", "python",
                "candidate_revision", "output", "controls", "not_applicable",
                "budgeted_memory_budget_bytes", "candidate_memory_proof_budget_bytes",
                "timeout_seconds", "attempt", "previous_aggregate"}
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise ValueError("unknown or missing operational manifest field")
    if manifest["schema_version"] != "grafx-pulse-qualification/v3":
        raise ValueError("unsupported operational manifest")
    selected = validate_controls(manifest)
    budgeted, landing = partition_controls(selected)
    _positive_budget(manifest["budgeted_memory_budget_bytes"], "budgeted query quota")
    _positive_budget(manifest["candidate_memory_proof_budget_bytes"], "candidate proof quota")
    _deadline(manifest["timeout_seconds"])
    if type(manifest["attempt"]) is not int or manifest["attempt"] not in (1, 2):
        raise ValueError("only one complete repeat is allowed")
    if (manifest["attempt"] == 1) != (manifest["previous_aggregate"] is None):
        raise ValueError("second attempt requires the first aggregate")
    return payload, manifest, budgeted, landing


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
            "uncovered_instrumentation": [name for name, count in counts.items() if count == 0]}


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


def inspect_component(root, regime, names, manifest):
    """Recompute one complete component and bind it to the aggregate inputs."""
    from bench.harness.campaign_schema import compare_campaign, load_json

    raw = load_json((root / "campaign.json").read_text(encoding="utf-8"))
    stored = load_json((root / "report.json").read_text(encoding="utf-8"))
    # Retry belongs to the closed aggregate. Both components are freshly run
    # with attempt 1 even when their enclosing aggregate is attempt 2.
    protocol = raw.get("protocol")
    if (not isinstance(protocol, dict) or type(protocol.get("attempt")) is not int
            or protocol["attempt"] != 1 or protocol.get("previous_campaign_sha256", "missing") is not None):
        raise ValueError(f"{regime} component attempt must be fresh within its aggregate")
    # Resolve against this component's own aggregate, including when checking
    # a prior aggregate for permission to repeat. Text normalization matches
    # campaign.run's embedded receipt on Windows; outer hashes bind raw bytes.
    calibration_path = canonical_path((root.parent / "calibration.json").resolve())
    calibration_raw = calibration_path.read_text(encoding="utf-8")
    calibration = raw.get("calibration")
    if (not isinstance(calibration, dict)
            or calibration.get("path") != str(calibration_path)
            or calibration.get("raw_json") != calibration_raw
            or calibration.get("sha256") != hashlib.sha256(calibration_raw.encode()).hexdigest()):
        raise ValueError(f"{regime} component differs from its aggregate calibration")
    report = compare_campaign(raw)
    if stored != report:
        raise ValueError(f"{regime} retained report differs from raw campaign")
    if (raw["intent"] != "qualification" or raw["mode"] != "noop"
            or [scenario["name"] for scenario in raw["scenarios"]] != names):
        raise ValueError(f"{regime} controls differ from operational selection")
    unbudgeted = regime == "landing"
    if (raw.get("memory_regime", "budgeted") != ("unbudgeted_landing" if unbudgeted else "budgeted")
            or raw.get("candidate_memory_budget_bytes") != (manifest["candidate_memory_proof_budget_bytes"] if unbudgeted else None)):
        raise ValueError(f"{regime} memory regime differs from protocol")
    expected_budget = None if unbudgeted else manifest["budgeted_memory_budget_bytes"]
    if any(scenario["query_memory_budget_bytes"] != expected_budget for scenario in raw["scenarios"]):
        raise ValueError(f"{regime} temporal memory quota differs from protocol")
    if ({run["revision"] for run in raw["runs"] if run["arm"] == "A"} != {BASELINE}
            or {run["revision"] for run in raw["runs"] if run["arm"] == "B"} != {manifest["candidate_revision"]}):
        raise ValueError(f"{regime} source revisions differ from protocol")
    for run in raw["runs"]:
        expected_root = manifest["baseline_root"] if run["arm"] == "A" else manifest["candidate_root"]
        if run["source_root"] != expected_root:
            raise ValueError(f"{regime} source root differs from protocol")
    observed = control_observations(raw)
    required = SEPARATE_LANDING_COVERAGE if unbudgeted else tuple(
        name for name in PHYSICAL_COVERAGE if name not in SEPARATE_LANDING_COVERAGE)
    uncovered = [name for name in required if observed["counters"][name] == 0]
    if uncovered:
        raise ValueError(f"{regime} lacks observed physical work: " + ", ".join(uncovered))
    return report, observed


def verify_previous_aggregate(manifest, base, output):
    """An entire completed inconclusive pair permits one entire repeat."""
    if manifest["attempt"] == 1:
        return None
    from bench.harness.campaign_schema import load_json

    path = canonical_path(manifest["previous_aggregate"])
    if path.name != "runner-receipt.json" or path.parent == output:
        raise ValueError("previous aggregate receipt path invalid")
    path.parent.relative_to(base / "runs")
    payload = path.read_bytes()
    previous = load_json(payload.decode("utf-8"))
    matching = ("candidate_revision", "baseline_revision", "controls", "not_applicable",
                "budgeted_memory_budget_bytes", "candidate_memory_proof_budget_bytes")
    expected = dict(manifest, baseline_revision=BASELINE)
    if (previous.get("attempt") != 1 or previous.get("decision") != "inconclusive"
            or any(previous.get(key) != expected[key] for key in matching)):
        raise ValueError("previous aggregate is not the same complete inconclusive protocol")
    artifacts = previous.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("previous aggregate artifact inventory absent")
    required = {"operational-manifest.json", "calibration.json",
                "budgeted/campaign.json", "landing/campaign.json",
                "budgeted/report.json", "landing/report.json"}
    if not required <= set(artifacts):
        raise ValueError("previous aggregate is incomplete")
    checked = set()
    for name, digest in artifacts.items():
        if (not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid prior artifact inventory")
        artifact = _tracked_path(path.parent, name, checked)
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError("previous aggregate artifact changed: " + name)
    prior_manifest = load_json((path.parent / "operational-manifest.json").read_text(encoding="utf-8"))
    if (hashlib.sha256((path.parent / "operational-manifest.json").read_bytes()).hexdigest() != previous.get("manifest_sha256")
            or prior_manifest.get("schema_version") != "grafx-pulse-qualification/v3"
            or prior_manifest.get("output") != str(path.parent)
            or prior_manifest.get("attempt") != 1 or prior_manifest.get("previous_aggregate") is not None
            or any(prior_manifest.get(key) != manifest[key] for key in
                   ("candidate_root", "baseline_root", "python", "candidate_revision", "controls",
                    "not_applicable", "budgeted_memory_budget_bytes", "candidate_memory_proof_budget_bytes"))):
        raise ValueError("previous operational manifest changed")
    budgeted, landing = partition_controls(validate_controls(manifest))
    decisions = [inspect_component(path.parent / part, part, names, manifest)[0]["decision"]
                 for part, names in (("budgeted", budgeted), ("landing", landing))]
    if set(decisions) - {"passed", "inconclusive"} or "inconclusive" not in decisions:
        raise ValueError("previous aggregate is not a complete inconclusive pair")
    if previous.get("temporal_decisions") != dict(zip(("budgeted", "landing"), decisions)):
        raise ValueError("previous aggregate decisions differ from retained raw campaigns")
    return hashlib.sha256(payload).hexdigest()


def run(snapshot):
    """Supervise the complete protocol under a locally authorized profile.

    The manifest deadline only narrows the registered native runner deadline.
    Timeout leaves an explicit failed receipt and any partial child artifacts.
    """
    from bench.harness.campaign import execute_worker
    started = time.monotonic()
    snapshot = canonical_path(snapshot)
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    manifest_bytes, manifest, _, _ = load_operational_manifest(base)
    deadline = manifest["timeout_seconds"]
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    audit = base / "runs" / ("supervisor-" + uuid.uuid4().hex)
    audit.mkdir(parents=True, exist_ok=False)
    result = audit / "result.json"
    launcher = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                "from bench.harness.pulse_qualification import _run;"
                 "r=_run(pathlib.Path(sys.argv[1]),sys.argv[3]);"
                 "pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding='utf-8')")
    from bench.harness.campaign_schema import RUNNER_PYTHON_FLAGS
    command = [sys.executable, *RUNNER_PYTHON_FLAGS, "-c", launcher,
               str(snapshot), str(result), manifest_hash]
    receipt = {"decision": "failed", "command": command,
               "timeout_seconds": deadline, "manifest_sha256": manifest_hash,
               "aggregate_attempt": manifest["attempt"]}
    try:
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("supervisor setup exhausted deadline")
        with (audit / "supervisor.log").open("x", encoding="utf-8") as log:
            code = execute_worker(command, log, remaining)
        if code:
            raise RuntimeError(f"qualification child exited {code}; see {audit}")
        child = json.loads(result.read_text(encoding="utf-8"))
        receipt["aggregate_decision"] = child.get("decision")
        receipt["aggregate_receipt"] = str(canonical_path(manifest["output"]) / "runner-receipt.json")
        if child.get("decision") != "passed":
            raise RuntimeError("qualification child did not pass")
        if time.monotonic() - started >= deadline:
            raise TimeoutError("qualification exceeded supervisor deadline")
        receipt["decision"] = "passed"
        return child
    except BaseException as failure:
        receipt["failure"] = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (audit / "supervisor-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def _run(snapshot, expected_manifest_sha256=None):
    """Run calibration and both complete historical campaigns or fail closed."""
    from bench.harness.campaign import execute_worker
    from bench.harness import campaign, campaign_schema

    snapshot = canonical_path(snapshot)
    for module in (campaign, campaign_schema):
        canonical_path(module.__file__).relative_to(snapshot)

    started = time.monotonic()
    base = canonical_path(Path(os.environ["LOCALAPPDATA"]) / "Okto" / "Grafx")
    manifest_bytes, manifest, budgeted, landing = load_operational_manifest(base)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is not None and manifest_hash != expected_manifest_sha256:
        raise ValueError("operational manifest changed after supervision began")
    candidate = canonical_path(manifest["candidate_root"])
    baseline = canonical_path(manifest["baseline_root"])
    python = canonical_path(manifest["python"])
    # Hosted Python installations may expose the active interpreter through a
    # symlink (for example, bin/python -> bin/python3). Authenticate the real
    # executable while keeping manifest paths subject to canonical_path.
    if python != canonical_path(Path(sys.executable).resolve()):
        raise ValueError("operational interpreter differs from authenticated runner")
    revision = manifest["candidate_revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("exact candidate SHA required")
    before = inventory(candidate, revision)
    baseline_before = inventory(baseline, BASELINE)
    verify_snapshot(snapshot, before)
    output = canonical_path(manifest["output"])
    output.relative_to(base / "runs")
    previous_hash = verify_previous_aggregate(manifest, base, output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "operational-manifest.json").write_bytes(manifest_bytes)
    receipt = {"manifest_sha256": manifest_hash,
               "candidate_revision": revision, "baseline_revision": BASELINE,
               "decision": "incomplete", "commands": [], "controls": manifest["controls"],
                "not_applicable": manifest["not_applicable"], "attempt": manifest["attempt"],
                "previous_aggregate_sha256": previous_hash,
                "budgeted_memory_budget_bytes": manifest["budgeted_memory_budget_bytes"],
                "candidate_memory_proof_budget_bytes": manifest["candidate_memory_proof_budget_bytes"],
                "temporal_decisions": {}}
    try:
        # Pulse-compatible safe flags import the actual snapshot's harness;
        # each worker independently attests the requested checkout import.
        launcher = campaign_schema.CALIBRATION_LAUNCHER
        common = [str(python), *campaign_schema.RUNNER_PYTHON_FLAGS, "-c", launcher, str(snapshot)]
        calibration = output / "calibration.json"
        common_run = ["--mode", "noop", "--intent", "qualification",
                      "--baseline", str(baseline), "--candidate", str(candidate),
                      "--max-cpu-percent", "20", "--calibrated-executor",
                      "--calibration", str(calibration)]
        commands = [common + ["calibrate", "--output", str(calibration)],
                    common + ["run", "--historical-noop", *common_run,
                              "--memory-budget", str(manifest["budgeted_memory_budget_bytes"]),
                              *[part for name in budgeted for part in ("--scenario", name)],
                              "--output", str(output / "budgeted")],
                    common + ["run", "--historical-unbudgeted-landing", *common_run,
                              "--candidate-memory-budget", str(manifest["candidate_memory_proof_budget_bytes"]),
                              *[part for name in landing for part in ("--scenario", name)],
                              "--output", str(output / "landing")]]
        for index, command in enumerate(commands):
            remaining = manifest["timeout_seconds"] - (time.monotonic() - started) - 5
            if remaining <= 0:
                raise TimeoutError("qualification deadline exhausted")
            receipt["commands"].append(command)
            with (output / f"phase-{index}.log").open("x", encoding="utf-8") as log:
                code = execute_worker(command, log, remaining)
            if code:
                # The campaign CLI returns 2 for an intact but inconclusive
                # comparison as well as for invalid/failing campaigns. Only a
                # complete, recomputed inconclusive result may proceed to the
                # other campaign and the aggregate decision.
                if index == 0 or code != 2:
                    raise RuntimeError(f"qualification phase {index} exited {code}; artifacts preserved")
                regime = "budgeted" if index == 1 else "landing"
                phase_output = output / regime
                try:
                    expected = budgeted if regime == "budgeted" else landing
                    recomputed, _ = inspect_component(phase_output, regime, expected, manifest)
                except (OSError, ValueError, KeyError) as exc:
                    raise RuntimeError(f"qualification phase {index} has no complete inconclusive campaign") from exc
                if recomputed["decision"] != "inconclusive":
                    raise RuntimeError(f"qualification phase {index} exited {code}; artifacts preserved")
            if index == 0:
                actual = campaign_schema.load_json(calibration.read_text(encoding="utf-8"))
                if actual["procedure"]["command"] != command:
                    raise ValueError("calibration interpreter argv differs from launched command")
        for regime, expected in (("budgeted", budgeted), ("landing", landing)):
            report, observed = inspect_component(output / regime, regime, expected, manifest)
            receipt.setdefault("physical_coverage", {})[regime] = observed
            receipt["temporal_decisions"][regime] = report["decision"]
        decisions = set(receipt["temporal_decisions"].values())
        if decisions <= {"passed"}:
            receipt["decision"] = "passed"
        elif decisions <= {"passed", "inconclusive"}:
            receipt["decision"] = "inconclusive"
        else:
            receipt["decision"] = "failed"
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
