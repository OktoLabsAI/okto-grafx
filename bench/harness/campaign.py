"""RF01 campaigns, independent of the existing D5 calibration entry point.

Examples: ``python -m bench.harness.campaign list``; ``... compare raw.json``;
``... run --baseline PATH --candidate PATH --output NEW_PATH --scenario constant``.
Default intent is exploratory. Qualification additionally requires a declared
calibrated executor, complete logical-memory observations and clean revisions.
"""
from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from bench.harness.campaign_fixtures import matrix
from bench.harness.campaign_schema import CampaignError, canonical_hash, compare_campaign, load_json, validate_campaign


def process_schedule():
    return [(pair, arm, order) for pair, order in enumerate(("AB", "BA", "AB")) for arm in order]


def reserve_output(path):
    Path(path).mkdir(parents=True, exist_ok=False)


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def calibrate_executor(output, command):
    """Measure the fixed local control procedure; never declare the host quiet.

    The receipt carries noisy observations too. Qualification decides whether
    they meet its registered policy. This is unsigned local evidence, not proof
    against deliberate fabrication by somebody controlling the workspace.
    """
    import psutil
    from bench.harness.campaign_worker import executor_environment
    if output.exists():
        raise FileExistsError(str(output))
    source = Path(__file__).resolve()
    sources = {name: hashlib.sha256(source.with_name(name).read_bytes()).hexdigest()
               for name in ("campaign.py", "campaign_schema.py", "campaign_worker.py")}
    environment = executor_environment()
    started_at = datetime.now(timezone.utc).isoformat()
    payload = bytes(range(256)) * 256
    expected = hashlib.sha256(payload).digest()
    warmup, retained, cpu = [], [], []
    for index in range(25):
        start = time.perf_counter_ns()
        for _ in range(128):
            result = hashlib.sha256(payload).digest()
        elapsed = time.perf_counter_ns() - start
        if result != expected:
            raise CampaignError("executor calibration control result mismatch")
        (warmup if index < 5 else retained).append(elapsed)
        if index % 5 == 4:
            cpu.append(psutil.cpu_percent(interval=.2))
    if any(hashlib.sha256(source.with_name(name).read_bytes()).hexdigest() != digest
           for name, digest in sources.items()):
        raise CampaignError("calibration procedure source changed during execution")
    receipt = {"schema_version": "grafx-executor-calibration/v1", "observed_at": started_at,
               "finished_at": datetime.now(timezone.utc).isoformat(),
               "environment": environment, "environment_hash": canonical_hash(environment),
               "producer_process_id": psutil.Process().pid,
               "procedure": {"id": "sha256-fixed-payload-v1", "command": command,
                             "source_sha256": sources["campaign.py"], "source_files": sources,
                             "control": {"algorithm": "sha256", "payload_bytes": 65536, "iterations_per_sample": 128}},
               "elapsed_ns": retained, "warmup_ns": warmup, "cpu_percent": cpu,
               "clock": {"name": "perf_counter_ns", "resolution_ns": time.get_clock_info("perf_counter").resolution * 1e9},
               "scope": "fixed CPU control and global CPU observations; not disk or Grafx throughput calibration",
               "authenticity": "unsigned local reproducible evidence"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
    return {"calibration_file": str(output.resolve()), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "retained": len(retained), "warmups": len(warmup), "cpu_observations": len(cpu)}


def execute_worker(command, log, timeout):
    """Bound the owned process tree, including spawned writer children."""
    import psutil
    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                owned = psutil.Process(process.pid)
                owned.suspend()  # Prevent spawning another child during cleanup.
                descendants = owned.children(recursive=True)
            except psutil.NoSuchProcess:
                descendants = []
            for child in descendants:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.wait()
            psutil.wait_procs(descendants, timeout=5)
            raise


def run_campaign(args):
    output = args.output.resolve()
    reserve_output(output)
    catalog = {scenario.name: scenario for scenario in matrix()}
    if len(args.scenario) != len(set(args.scenario)):
        raise CampaignError("duplicate scenario selection")
    if not 0 < args.max_cpu_percent <= 100 or getattr(args, "worker_timeout", 3600) <= 0:
        raise CampaignError("CPU threshold/worker timeout must be positive and bounded")
    if args.memory_budget is not None and args.memory_budget <= 0:
        raise CampaignError("memory budget must be positive")
    targets = {}
    for item in args.target:
        parts = item.split(":")
        if len(parts) != 2 or parts[0] not in args.scenario or parts[0] in targets:
            raise CampaignError("target must be a unique selected SCENARIO:PHYSICAL_COUNTER")
        if parts[1] not in {"physical_node_versions_examined", "physical_edge_versions_examined",
                            "index_probe_keys", "certificate_attempts", "certificate_retries",
                            "landing_candidates", "landing_batches"}:
            raise CampaignError("target must name a physical work counter")
        targets[parts[0]] = parts[1]
    selected = []
    for name in args.scenario:
        if name not in catalog:
            raise CampaignError(f"unknown scenario: {name}")
        scenario = catalog[name]
        if args.memory_budget is not None:
            scenario = dataclasses.replace(scenario, query_memory_budget_bytes=args.memory_budget)
        if name in targets:
            scenario = dataclasses.replace(scenario, role="target", work_counter=targets[name])
        selected.append(scenario.to_dict())
    sources = {"A": args.baseline.resolve(), "B": args.candidate.resolve()}
    revisions = {arm: subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
                 for arm, root in sources.items()}
    previous = None
    if args.previous is not None:
        previous_data = load_json(args.previous.read_text(encoding="utf-8"))
        if previous_data["protocol"]["attempt"] != 1:
            raise CampaignError("only one entire rerun is allowed")
        previous = hashlib.sha256(args.previous.read_bytes()).hexdigest()
        if previous_data["scenarios"] != selected or previous_data["mode"] != args.mode:
            raise CampaignError("rerun must preserve scenarios and comparison mode")
        previous_revisions = {run["arm"]: run["revision"] for run in previous_data["runs"]}
        if previous_revisions != revisions or previous_data["protocol"]["max_cpu_percent"] != args.max_cpu_percent:
            raise CampaignError("rerun must preserve revisions and registered noise policy")
    data = {"schema_version": "grafx-campaign/v1", "intent": args.intent, "mode": args.mode,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "calibrated_executor": args.calibrated_executor,
            "command": [sys.executable, "-m", "bench.harness.campaign", *sys.argv[1:]],
            "protocol": {"pairs": 3, "warmups": 5, "retained": 20, "orders": ["AB", "BA", "AB"],
                         "attempt": 2 if previous else 1, "previous_campaign_sha256": previous,
                         "max_cpu_percent": args.max_cpu_percent},
            "scenarios": selected, "runs": [],
            "not_applicable": [name for name in catalog if name not in args.scenario],
            "harness_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in Path(__file__).parent.glob("campaign*.py")}}
    calibration_path = getattr(args, "calibration", None)
    if calibration_path is not None:
        raw = calibration_path.read_text(encoding="utf-8")
        load_json(raw)
        data["calibration"] = {"path": str(calibration_path.resolve()), "raw_json": raw,
                               "sha256": hashlib.sha256(raw.encode()).hexdigest()}
    _write(output / "manifest.json", data)
    worker = Path(__file__).with_name("campaign_worker.py").resolve()
    for pair, arm, order in process_schedule():
        request = {"source_root": str(sources[arm]), "revision": revisions[arm], "arm": arm,
                   "pair": pair, "run_order": order, "scenarios": selected}
        request_path = output / f"request-{pair}-{arm}.json"
        raw_path = output / f"raw-{pair}-{arm}.json"
        _write(request_path, request)
        command = [sys.executable, "-I", str(worker), "--request", str(request_path), "--output", str(raw_path)]
        try:
            with (output / f"worker-{pair}-{arm}.log").open("w", encoding="utf-8") as log:
                returncode = execute_worker(command, log, getattr(args, "worker_timeout", 3600))
        except subprocess.TimeoutExpired:
            data["worker_failure"] = {"pair": pair, "arm": arm, "reason": "worker timeout", "command": command}
            _write(output / "campaign.json", data)
            _write(output / "report.json", {"decision": "invalid", "reason": "worker timeout; raw/log retained"})
            raise CampaignError("worker timeout; preserved artifacts") from None
        if returncode:
            data["worker_failure"] = {"pair": pair, "arm": arm, "exitcode": returncode, "command": command}
            _write(output / "campaign.json", data)
            _write(output / "report.json", {"decision": "invalid", "reason": "worker failure; raw/log retained"})
            raise CampaignError("worker failed; preserved raw artifacts, no ratios calculated")
        data["runs"].append(load_json(raw_path.read_text(encoding="utf-8")))
        _write(output / "campaign.json", data)
    try:
        report = compare_campaign(data)
    except CampaignError as exc:
        _write(output / "report.json", {"decision": "invalid", "reason": str(exc)})
        raise
    _write(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list")
    commands.add_parser("calibrate", help="execute the fixed local CPU control procedure").add_argument("--output", type=Path, required=True)
    for name in ("validate", "compare"):
        commands.add_parser(name).add_argument("raw", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--baseline", type=Path, required=True)
    run.add_argument("--candidate", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--scenario", action="append", required=True)
    run.add_argument("--mode", choices=("noop", "optimization", "recipe"), default="noop")
    run.add_argument("--intent", choices=("exploratory", "qualification"), default="exploratory")
    run.add_argument("--calibrated-executor", action="store_true")
    run.add_argument("--calibration", type=Path, help="hashed executor control receipt; a boolean alone cannot qualify")
    run.add_argument("--worker-timeout", type=float, default=3600)
    run.add_argument("--max-cpu-percent", type=float, default=20)
    run.add_argument("--memory-budget", type=int)
    run.add_argument("--target", action="append", default=[], metavar="SCENARIO:PHYSICAL_COUNTER",
                     help="optimization target and its physical work counter; other scenarios remain controls")
    run.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            result = [scenario.to_dict() for scenario in matrix()]
        elif args.action == "calibrate":
            command = [sys.executable, "-m", "bench.harness.campaign", *(sys.argv[1:] if argv is None else argv)]
            result = calibrate_executor(args.output, command)
        elif args.action == "run":
            result = run_campaign(args)
        else:
            data = load_json(args.raw.read_text(encoding="utf-8"))
            if args.action == "validate":
                validate_campaign(data)
                result = {"valid": True}
            else:
                result = compare_campaign(data)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if not isinstance(result, dict) or result.get("decision") not in ("failed", "invalid", "inconclusive") else 2
    except (CampaignError, FileExistsError) as exc:
        print(json.dumps({"decision": "invalid", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
