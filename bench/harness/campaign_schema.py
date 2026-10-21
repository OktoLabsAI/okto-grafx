"""Versioned RF01 raw evidence and fail-closed campaign comparison.

Validation precedes all ratios. JSON duplicate keys, non-finite values, missing
samples and semantic mismatches are errors, never fast measurements. A report is
descriptive engineering evidence, not a production percentile/SLA estimate.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics

from bench.harness.campaign_fixtures import Scenario, contract, oracle, query_for
from bench.harness import fts_qualification, rf04_qualification

HISTORICAL_SCOPE = "rf01_historical_total_overhead"
HISTORICAL_REVISION = "3a2239a0bda80223c298a68d6d14f3c951f133d2"
HISTORICAL_TREE = "c2795181cda21041998cd13959afc8de303802bb"
HISTORICAL_QUERY_SHA256 = "8056d0625cf4764c3a160bb676b6cefdc71d680f7f52bba44b626a28e0fa7ad8"
LOGICAL_COVERAGE = "simultaneous_query_budget_and_handle_owner_endpoint_tariffs_v1"
WRITE_PHYSICAL_COUNTERS = ("physical_node_versions_examined", "physical_edge_versions_examined",
                           "index_probe_keys", "certificate_attempts", "certificate_retries")
WRITE_REQUIRED_COUNTERS = ("index_probe_keys", "certificate_attempts")
# Match the authenticated Pulse interpreter: ignore PYTHON* overrides and
# unsafe cwd/script prepend, preserve declared user-site dependencies, no pyc.
RUNNER_PYTHON_FLAGS = ("-E", "-P", "-B")
CALIBRATION_LAUNCHER = ("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
                        "runpy.run_module('bench.harness.campaign',run_name='__main__')")


def calibration_command_valid(command):
    """Recognize the real interpreter argv, including the isolated bridge."""
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        return False
    if command[1:4] == ["-m", "bench.harness.campaign", "calibrate"]:
        return True
    return (len(command) == 10 and command[1:4] == list(RUNNER_PYTHON_FLAGS)
            and command[4:6] == ["-c", CALIBRATION_LAUNCHER]
            and Path(command[6]).is_absolute() and command[7:9] == ["calibrate", "--output"])


def validate_candidate_memory(run, budgets):
    """Separate deterministic lifecycle evidence, bound to this worker/source.

    These are unsigned local receipts, not authenticated third-party claims.
    No per-operator peak is added to another; live simultaneous totals and each
    independent budget ceiling are checked separately.
    """
    evidence = run.get("candidate_memory_evidence")
    _fields(evidence, ("body", "sha256"), "candidate memory evidence")
    body = evidence["body"]
    _require(canonical_hash(body) == evidence["sha256"], "candidate memory evidence hash mismatch")
    _fields(body, ("schema_version", "revision", "process_id", "invocation_id", "engine_source_hashes", "procedure", "budgets"), "candidate memory proof")
    _require(body["schema_version"] == "grafx-candidate-memory/v1", "unknown memory proof schema")
    for key in ("revision", "process_id", "invocation_id", "engine_source_hashes"):
        _require(body[key] == run[key], f"candidate memory proof not bound to run {key}")
    producer = Path(__file__).with_name("campaign_worker.py")
    _require(body["procedure"] == {"id": "rf01-logical-lifecycle-v1", "source_sha256": hashlib.sha256(producer.read_bytes()).hexdigest()},
             "candidate memory procedure provenance mismatch")
    _require(isinstance(body["budgets"], list) and len(body["budgets"]) == len(budgets), "missing/duplicate memory budget evidence")
    seen = set()
    for group in body["budgets"]:
        limit = group["budget_bytes"]
        _require(type(limit) is int and limit in budgets and limit not in seen, "wrong memory proof budget")
        seen.add(limit)
        records = group["records"]
        _require(isinstance(records, list) and len(records) == 60, "memory proof requires twenty success/error/cancel executions each")
        initial = None
        for position, row in enumerate(records):
            mode = ("success", "error", "cancel")[position // 20]
            _require(row["mode"] == mode and row["index"] == position % 20, "memory proof missing/reordered lifecycle observation")
            for key in ("live_before_bytes", "live_after_bytes", "handle_before_bytes", "handle_after_bytes", "open_transactions_after"):
                _integer(row[key], key)
            if initial is None:
                initial = row["handle_before_bytes"]
            _require(row["live_before_bytes"] == row["live_after_bytes"] == row["handle_before_bytes"] == row["handle_after_bytes"] == initial,
                     "candidate memory retention grew or did not return to baseline")
            _require(row["open_transactions_after"] == 0, "memory proof left transaction open")
            expected_error = {"success": None, "error": "plan_error", "cancel": "query_cancelled"}[mode]
            _require(row["error_code"] == expected_error, "memory proof did not exercise expected outcome")
            memory = row["memory"]
            _require(memory["coverage"] == LOGICAL_COVERAGE and memory["unavailable_reason"] is None,
                     "candidate memory proof coverage unavailable")
            _integer(memory["logical_memory_peak_bytes"], "memory proof peak", 1)
            _require(memory["baseline_retained_bytes"] == initial, "memory proof baseline mismatch")
            _require(memory["logical_memory_peak_bytes"] >= initial, "memory proof peak below baseline")
            operators = row["operator_budgets"]
            _require(isinstance(operators, list) and bool(operators), "memory proof observed no operator budgets")
            for operator in operators:
                _require(isinstance(operator["operator"], str) and bool(operator["operator"]), "operator identity missing")
                _require(type(operator["limit_bytes"]) is int and operator["limit_bytes"] == limit,
                         "operator budget differs from configured limit")
                _integer(operator["peak_bytes"], "operator peak")
                _require(operator["peak_bytes"] <= limit, "operator exceeded memory budget")


def _historical_run(run, scenarios, unbudgeted=False, proof_budget=None):
    _fields(run, ("source_tree", "engine_source_hashes", "logical_observer_available"), "historical source evidence")
    _require(bool(re.fullmatch("[0-9a-f]{40}", run["source_tree"])), "invalid source tree")
    hashes = run["engine_source_hashes"]
    _fields(hashes, ("src/okto_grafx/engine/query_engine.py", "src/okto_grafx/domain/query/memory.py"), "engine source hashes")
    _require(all(isinstance(value, str) and bool(re.fullmatch("[0-9a-f]{64}", value)) for value in hashes.values()), "invalid engine source hash")
    if run["arm"] == "A":
        _require(run["revision"] == HISTORICAL_REVISION and run["source_tree"] == HISTORICAL_TREE
                 and hashes["src/okto_grafx/engine/query_engine.py"] == HISTORICAL_QUERY_SHA256
                 and run["logical_observer_available"] is False, "historical baseline identity or observer absence not established")
    else:
        _require(run["revision"] != HISTORICAL_REVISION and run["logical_observer_available"] is True,
                 "candidate logical observer unavailable")
        validate_candidate_memory(run, {proof_budget} if unbudgeted else {s.query_memory_budget_bytes for s in scenarios.values()})


class CampaignError(ValueError):
    """Evidence cannot support a comparison."""


def _typed(value):
    if type(value).__name__ == "Timestamp" and type(value).__module__ == "okto_grafx.domain.model.value":
        return ["timestamp_micros", str(value.micros)]
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise CampaignError("non-finite number")
        return ["float", value.hex()]
    if type(value) is str:
        return ["str", value]
    if isinstance(value, (list, tuple)):
        return ["sequence", [_typed(item) for item in value]]
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return ["map", [[key, _typed(value[key])] for key in sorted(value)]]
    raise CampaignError(f"unsupported canonical type: {type(value).__name__}")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(_typed(value), separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def load_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CampaignError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def invalid(value):
        raise CampaignError(f"non-finite JSON constant: {value}")
    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CampaignError(str(exc)) from exc


def _require(condition, message):
    if not condition:
        raise CampaignError(message)


def _fields(value, names, where):
    _require(isinstance(value, dict), f"{where}: expected object")
    missing = set(names) - value.keys()
    _require(not missing, f"{where}: missing {sorted(missing)}")


def _integer(value, name, minimum=0):
    _require(type(value) is int and value >= minimum, f"{name}: invalid integer")


def validate_campaign(data):
    """Validate completeness, metadata, independent oracles and contracts."""
    try:
        _validate(data)
    except CampaignError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CampaignError(f"malformed campaign: {exc}") from exc


def calibration_matches(data):
    """Validate an embedded executor control receipt, not a boolean assertion.

    Receipt schema is intentionally local to this harness. It is reproducible
    evidence, not an authentication/signature claim. Missing evidence is noisy;
    malformed supplied evidence is invalid. Age is checked against campaign time,
    so re-reading a valid historical artifact does not change its verdict.
    """
    calibration = data.get("calibration")
    if calibration is None:
        return False
    _fields(calibration, ("raw_json", "sha256", "path"), "calibration")
    raw = calibration["raw_json"]
    _require(isinstance(raw, str) and hashlib.sha256(raw.encode()).hexdigest() == calibration["sha256"], "calibration file hash mismatch")
    receipt = load_json(raw)
    _fields(receipt, ("schema_version", "observed_at", "environment_hash", "procedure", "elapsed_ns", "cpu_percent"), "calibration receipt")
    _require(receipt["schema_version"] == "grafx-executor-calibration/v1", "unsupported calibration schema")
    _fields(receipt["procedure"], ("command", "source_sha256"), "calibration procedure")
    _require(isinstance(receipt["procedure"]["command"], list) and bool(receipt["procedure"]["command"]), "calibration command missing")
    _require(bool(re.fullmatch("[0-9a-f]{64}", receipt["procedure"]["source_sha256"])), "calibration procedure digest invalid")
    procedure = receipt["procedure"]
    root = Path(__file__).resolve().parent
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
               for name in ("campaign.py", "campaign_schema.py", "campaign_worker.py")}
    _require(procedure.get("id") == "sha256-fixed-payload-v1"
             and procedure["source_sha256"] == sources["campaign.py"]
             and procedure.get("source_files") == sources
             and procedure.get("control") == {"algorithm": "sha256", "payload_bytes": 65536, "iterations_per_sample": 128}
             and calibration_command_valid(procedure["command"]),
             "calibration procedure provenance does not match this executable harness")
    _fields(receipt, ("environment", "warmup_ns", "producer_process_id", "clock"), "calibration receipt")
    _require(canonical_hash(receipt["environment"]) == receipt["environment_hash"], "calibration environment hash mismatch")
    _require(isinstance(receipt["warmup_ns"], list) and len(receipt["warmup_ns"]) == 5
             and all(type(v) is int and v > 0 for v in receipt["warmup_ns"]), "invalid calibration warmup")
    _integer(receipt["producer_process_id"], "calibration process_id", 1)
    _require(receipt["clock"].get("name") == "perf_counter_ns"
             and type(receipt["clock"].get("resolution_ns")) in (int, float)
             and receipt["clock"]["resolution_ns"] > 0, "invalid calibration clock")
    times, loads = receipt["elapsed_ns"], receipt["cpu_percent"]
    _require(isinstance(times, list) and len(times) >= 20, "calibration requires twenty retained control samples")
    _require(all(type(t) is int and t > 0 for t in times), "invalid calibration elapsed sample")
    _require(isinstance(loads, list) and len(loads) >= 5 and all(type(v) in (int, float) and 0 <= v <= 100 for v in loads), "invalid calibration CPU observations")
    observed = datetime.fromisoformat(receipt["observed_at"])
    campaign_time = datetime.fromisoformat(data["started_at"])
    _require(observed.tzinfo is not None and campaign_time.tzinfo is not None, "calibration timestamps require timezone")
    age = (campaign_time.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    env = data["runs"][0]["environment"]
    environment = {k: env[k] for k in ("python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "gc_enabled")}
    return (0 <= age <= 86400 and receipt["environment_hash"] == canonical_hash(environment)
            and max(loads) <= data["protocol"]["max_cpu_percent"]
            and statistics.stdev(times) / statistics.mean(times) <= .10)


def _validate(data):
    canonical_hash(data)  # Reject NaN/infinity anywhere, including optional fields.
    _fields(data, ("schema_version", "mode", "intent", "calibrated_executor", "command", "protocol", "scenarios", "runs"), "campaign")
    _require(data["schema_version"] == "grafx-campaign/v1", "unsupported schema version")
    _require(data["mode"] in ("noop", "optimization", "recipe"), "unknown comparison mode")
    _require(data["intent"] in ("exploratory", "qualification"), "unknown intent")
    scope = data.get("qualification_scope")
    _require(scope in (None, HISTORICAL_SCOPE), "unknown qualification scope")
    historical = scope == HISTORICAL_SCOPE
    regime = data.get("memory_regime", "budgeted")
    _require(regime in ("budgeted", "unbudgeted_landing"), "unknown memory regime")
    unbudgeted = regime == "unbudgeted_landing"
    proof_budget = data.get("candidate_memory_budget_bytes")
    if unbudgeted:
        _require(historical, "unbudgeted landing is only a historical noop exception")
        _integer(proof_budget, "separate candidate memory budget", 1)
    else:
        _require(proof_budget is None, "separate memory budget only applies to unbudgeted landing")
    if historical:
        _require(data["mode"] == "noop", "historical exception is only RF01 total noop overhead")
    _require(type(data["calibrated_executor"]) is bool, "calibrated_executor must be boolean")
    _require(isinstance(data["command"], list) and bool(data["command"]) and all(type(x) is str for x in data["command"]), "missing reproduction command")
    protocol = data["protocol"]
    _fields(protocol, ("pairs", "warmups", "retained", "orders", "attempt", "previous_campaign_sha256", "max_cpu_percent"), "protocol")
    _require((protocol["pairs"], protocol["warmups"], protocol["retained"], protocol["orders"]) == (3, 5, 20, ["AB", "BA", "AB"]), "protocol requires three AB/BA/AB pairs and 5+20 samples")
    _require(type(protocol["attempt"]) is int and protocol["attempt"] in (1, 2), "at most one complete rerun")
    previous = protocol["previous_campaign_sha256"]
    _require((previous is None and protocol["attempt"] == 1) or
             (protocol["attempt"] == 2 and isinstance(previous, str) and bool(re.fullmatch("[0-9a-f]{64}", previous))), "rerun must link complete previous campaign")
    _require(type(protocol["max_cpu_percent"]) in (int, float) and 0 < protocol["max_cpu_percent"] <= 100,
             "CPU gate must be an explicit percentage")
    if historical:
        _require(protocol["max_cpu_percent"] <= 20, "historical qualification CPU ceiling is 20 percent")
    scenarios = {}
    expectations = {}
    _require(isinstance(data["scenarios"], list) and bool(data["scenarios"]), "no scenarios")
    for value in data["scenarios"]:
        scenario = Scenario.from_dict(value)
        if scenario.family == "capacity":
            _require(data["mode"] == "recipe", "RF12 capacity is a local recipe, not a universal optimization")
        _require(scenario.name not in scenarios, "duplicate scenario")
        scenarios[scenario.name] = scenario
        if scenario.family == "fts_matrix":
            _require(data["mode"] == "recipe" and not historical,
                     "ranked FTS matrix requires a non-historical recipe comparison")
        if scenario.family == "unique_create":
            _require(data["mode"] == "optimization" and not historical,
                     "RF08 CREATE requires its own optimization matrix")
        if historical:
            if unbudgeted:
                _require(scenario.family == "landing" and scenario.query_memory_budget_bytes is None,
                         "unbudgeted landing requires landing family and no temporal quota in either arm")
            else:
                _require(type(scenario.query_memory_budget_bytes) is int and scenario.query_memory_budget_bytes > 0,
                         "historical candidate requires explicit logical budget in both arms")
            _require(scenario.family not in ("fts", "write") and scenario.writers == 1 and scenario.reader == "short",
                     "historical profile currently covers single-statement read controls only")
        query, params = query_for(scenario)
        expected_rows = oracle(scenario)
        expectations[scenario.name] = {"seed": scenario.seed, "config_hash": canonical_hash(scenario.to_dict()),
                                      "query_hash": canonical_hash(query), "parameters_hash": canonical_hash(params),
                                      "contract_hash": canonical_hash(contract(scenario)),
                                      "expected_result_hash": canonical_hash(expected_rows),
                                      "result_hash": canonical_hash(expected_rows), "rows": len(expected_rows)}
    rf08_qualification = (data["intent"] == "qualification"
                          and any(s.family == "unique_create" for s in scenarios.values()))
    if rf08_qualification:
        from bench.harness.uniqueness_qualification import matrix as rf08_matrix
        expected = {scenario.name: scenario.to_dict() for scenario in rf08_matrix(Scenario)}
        _require({name: scenario.to_dict() for name, scenario in scenarios.items()} == expected,
                 "RF08 qualification requires the exact 128/1/128-quota scenario matrix")
    fts_recipe = data["mode"] == "recipe" and all(s.family == "fts_matrix" for s in scenarios.values())
    if data["intent"] == "qualification" and any(s.family == "fts_matrix" for s in scenarios.values()):
        _require(fts_recipe, "qualified native FTS recipes cannot mix memory coverage with other families")
    if data["mode"] == "optimization":
        _require(any(s.role == "target" and s.work_counter for s in scenarios.values()), "optimization needs target physical-work counter")
        _require(any(s.role == "control" for s in scenarios.values()), "optimization needs controls")
    runs = data["runs"]
    _require(isinstance(runs, list) and len(runs) == 6, "six independent process runs required")
    observed, invocations, revisions = set(), set(), {"A": set(), "B": set()}
    environments = []
    sample_contracts = {}
    for position, run in enumerate(runs):
        _fields(run, ("arm", "pair", "run_order", "process_id", "invocation_id", "revision", "source_clean",
                      "package_file", "source_root", "environment", "cpu_before_percent", "cpu_after_percent", "metrics_sink", "samples"), "run")
        arm, pair = run["arm"], run["pair"]
        _require(arm in ("A", "B") and type(pair) is int and pair in (0, 1, 2), "invalid arm/pair")
        _require((pair, arm) not in observed, "duplicate arm/pair")
        observed.add((pair, arm))
        _require(run["run_order"] == protocol["orders"][pair], "wrong AB/BA order")
        _require(pair == position // 2 and arm == protocol["orders"][pair][position % 2], "actual process order differs from declaration")
        _integer(run["process_id"], "process_id", 1)
        _require(isinstance(run["invocation_id"], str) and bool(run["invocation_id"]) and run["invocation_id"] not in invocations, "duplicate invocation")
        invocations.add(run["invocation_id"])
        _require(isinstance(run["revision"], str) and bool(re.fullmatch("[0-9a-f]{40}", run["revision"])), "revision must be exact SHA")
        revisions[arm].add(run["revision"])
        _require(run["source_clean"] is True, "dirty source cannot establish revision")
        if historical:
            if unbudgeted:
                _require(run.get("memory_regime") == regime and run.get("candidate_memory_budget_bytes") == proof_budget,
                         "worker memory regime/proof budget differs from campaign")
            _historical_run(run, scenarios, unbudgeted, proof_budget)
        _require(bool(run["source_root"]) and bool(run["package_file"]), "missing import provenance")
        env = run["environment"]
        _fields(env, ("python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "disk", "gc_enabled"), "environment")
        _require(env["gc_enabled"] is True, "GC must remain enabled")
        _require(all(isinstance(env[k], str) and bool(env[k].strip()) for k in ("python", "os", "cpu")), "blank environment metadata")
        _integer(env["cpu_count"], "cpu_count", 1)
        _integer(env["ram_bytes"], "ram_bytes", 1)
        _require(isinstance(env["dependencies"], dict) and isinstance(env["disk"], dict), "invalid dependencies/disk")
        environments.append({k: env[k] for k in ("python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "gc_enabled")})
        for name in ("cpu_before_percent", "cpu_after_percent"):
            _require(type(run[name]) in (int, float) and 0 <= run[name] <= 100, f"invalid {name}")
        if data["mode"] == "noop":
            _require(run["metrics_sink"] == "NoOpMetricsSink", "noop requires default NoOpMetricsSink")
        samples = run["samples"]
        _require(isinstance(samples, list) and len(samples) == 25 * len(scenarios), "missing/extra samples")
        seen = set()
        for sample in samples:
            _fields(sample, ("scenario_id", "index", "kind", "seed", "config_hash", "query_hash", "parameters_hash",
                             "contract_hash", "cache_state", "result_hash", "expected_result_hash", "rows", "statistics",
                             "selected_plan", "effective_regime", "elapsed_ns", "cpu_ns", "prepare_ns", "prepare_boundary",
                             "first_row_ns", "consume_all_ns", "commit_ns", "memory", "rss_peak_bytes", "rss_scope",
                             "wal_delta_bytes", "disk_delta_bytes", "checkpoint"), "sample")
            name, index = sample["scenario_id"], sample["index"]
            _require(name in scenarios and type(index) is int and index in range(25), "invalid sample identity")
            _require((name, index) not in seen, "duplicate sample")
            seen.add((name, index))
            scenario = scenarios[name]
            _require(sample["kind"] == ("warmup" if index < 5 else "retained"), "wrong warmup classification")
            for key, expected in expectations[name].items():
                _require(sample[key] == expected, f"{name}: invalid {key} or semantic mismatch")
            _require(isinstance(sample["cache_state"], dict) and set(sample["cache_state"]) >= {"plan", "handle", "fts", "process", "os"}, "cache dimensions missing")
            sample_contract = {k: sample[k] for k in ("cache_state", "prepare_boundary", "rss_scope")}
            if name in sample_contracts:
                _require(sample_contract == sample_contracts[name], "measurement/cache contract changed between samples")
            else:
                sample_contracts[name] = sample_contract
            _require(isinstance(sample["selected_plan"], list) and bool(sample["selected_plan"]), "plan missing")
            _require(isinstance(sample["effective_regime"], dict), "effective runtime regime missing")
            _require(isinstance(sample["statistics"], dict), "statistics missing")
            for key, value in sample["statistics"].items():
                _integer(value, f"counter {key}")
            if scenario.rf04_case is not None:
                try:
                    rf04_qualification.validate_sample(sample, scenario)
                except ValueError as exc:
                    raise CampaignError(str(exc)) from exc
            if scenario.family == "fts_matrix":
                source = sample.get("fts_source")
                _fields(source, ("statistics_regime", "statistics_mode", "history_entries", "hits"), "FTS source")
                _require(source["statistics_mode"] == scenario.fts_statistics_mode
                         and source["history_entries"] == scenario.fts_history
                         and source["hits"] == sample["rows"]
                         and source["statistics_regime"] == fts_qualification.expected_source(scenario)
                         and sample["statistics"].get("corpus_documents") == scenario.nodes
                         and sample["statistics"].get("statistics_from_" + source["statistics_regime"]) == 1,
                         "FTS source or corpus evidence differs from scenario")
                _require((sample["statistics"].get("statistics_wal_records", 0) > 0)
                         == (scenario.fts_updates == 1), "FTS WAL-delta proof invalid")
                setup = sample.get("fts_setup")
                _fields(setup, ("elapsed_ns", "disk_allocated_bytes", "wal_allocated_bytes", "scope"), "FTS fixture setup")
                _integer(setup["elapsed_ns"], "FTS setup elapsed", 1)
                _integer(setup["disk_allocated_bytes"], "FTS setup disk", 1)
                _integer(setup["wal_allocated_bytes"], "FTS setup WAL")
                if scenario.fts_updates:
                    maintenance = sample.get("fts_maintenance")
                    _fields(maintenance, ("elapsed_ns", "durable_acks", "commit_ns", "disk_allocated_delta_bytes",
                                          "wal_allocated_delta_bytes", "scope"), "FTS maintenance")
                    _integer(maintenance["elapsed_ns"], "FTS update elapsed", 1)
                    _require(maintenance["durable_acks"] == scenario.fts_updates,
                             "FTS update ACK count differs from recipe")
                    _require(isinstance(maintenance["commit_ns"], list)
                             and len(maintenance["commit_ns"]) == maintenance["durable_acks"],
                             "FTS durable commit durations missing")
                    for duration in maintenance["commit_ns"]:
                        _integer(duration, "FTS commit ACK duration", 1)
                    _require(sum(maintenance["commit_ns"]) <= maintenance["elapsed_ns"],
                             "FTS commit ACK durations exceed update interval")
                    _require(type(maintenance["disk_allocated_delta_bytes"]) is int
                             and type(maintenance["wal_allocated_delta_bytes"]) is int,
                             "FTS allocated byte deltas must be signed integers")
                else:
                    _require("fts_maintenance" not in sample, "non-retained FTS sample has maintenance")
            for key in ("elapsed_ns", "cpu_ns", "prepare_ns", "consume_all_ns", "rss_peak_bytes"):
                _integer(sample[key], key)
            _require(sample["elapsed_ns"] > 0, "zero elapsed time")
            _require(sample["consume_all_ns"] <= sample["elapsed_ns"], "consume boundary exceeds total")
            if scenario.cursor_batch_size and sample["rows"]:
                _integer(sample["first_row_ns"], "first_row_ns")
                _require(sample["first_row_ns"] <= sample["consume_all_ns"], "first-row boundary exceeds consume")
            else:
                _require(sample["first_row_ns"] is None, "first row only applies to cursors")
            if scenario.family in ("write", "unique_create"):
                _integer(sample["commit_ns"], "commit_ns")
                _require(sample["commit_ns"] <= sample["elapsed_ns"], "ACK boundary exceeds total")
                if scenario.family == "unique_create":
                    from bench.harness.uniqueness_qualification import validate_sample as validate_unique_sample
                    try:
                        validate_unique_sample(sample, scenario, arm=arm)
                    except ValueError as exc:
                        raise CampaignError(str(exc)) from exc
                    _require(sample["unique_create"]["reopened"] is True,
                             "RF08 read-only reopen and verify proof missing")
                    if rf08_qualification:
                        _require(sample["rss_peak_bytes"] > 0 and sample["rss_scope"] in (
                            "process_lifetime_peak_working_set_bytes", "process_lifetime_ru_maxrss_bytes"),
                            "RF08 qualification requires real process RSS in both arms")
                        _require(sample.get("memory_budget_bytes") == scenario.query_memory_budget_bytes,
                                 "RF08 observed memory budget differs from scenario")
                        if scenario.query_memory_budget_bytes is None:
                            _require(sample["memory"]["logical_memory_peak_bytes"] is None
                                     and sample["memory"]["coverage"] == "unavailable"
                                     and sample["memory"]["unavailable_reason"] == "query_memory_budget_disabled",
                                     "RF08 unbudgeted diagnostic must report null logical peak")
                        else:
                            _require(sample["memory"]["coverage"] == LOGICAL_COVERAGE,
                                     "RF08 quota control requires covered logical peak")
                else:
                    validate_write_sample(sample, scenario, qualification=data["intent"] == "qualification")
                if data["intent"] == "qualification" and scenario.family == "write" and scenario.writers > 1:
                    key = name + ":writer_memory"
                    coverage = [w["memory"]["coverage"] for w in sample["writers"]]
                    _require(key not in sample_contracts or sample_contracts[key] == coverage,
                             "writer memory measurement changed between samples/arms")
                    sample_contracts[key] = coverage
            else:
                _require(sample["commit_ns"] is None, "read cannot claim durable commit timing")
            memory = sample["memory"]
            if scenario.family == "capacity":
                from bench.harness.capacity_qualification import validate_sample
                try:
                    validate_sample(sample, scenario)
                except ValueError as exc:
                    raise CampaignError(str(exc)) from exc
            _fields(memory, ("logical_memory_peak_bytes", "baseline_retained_bytes", "coverage", "unavailable_reason"), "memory")
            _integer(memory["baseline_retained_bytes"], "baseline_retained_bytes")
            if historical:
                _require(sample["rss_peak_bytes"] > 0 and sample["rss_scope"] in (
                    "process_lifetime_peak_working_set_bytes", "process_lifetime_ru_maxrss_bytes"),
                    "historical qualification requires real process RSS in both arms")
                _require(sample.get("memory_budget_bytes") == scenario.query_memory_budget_bytes,
                         "observed memory budget differs from common scenario config")
                if arm == "A":
                    _require(memory["logical_memory_peak_bytes"] is None
                             and memory["unavailable_reason"] == "revision_has_no_logical_memory_observer"
                             and memory["coverage"] == "unavailable", "historical memory must remain explicitly unavailable, never zero")
                elif unbudgeted:
                    _require(memory["logical_memory_peak_bytes"] is None and memory["coverage"] == "unavailable"
                             and memory["unavailable_reason"] == "query_memory_budget_disabled",
                             "unbudgeted candidate memory must remain null with disabled-budget reason")
                    _require(sample["statistics"].get("landing_batches", 0) > 0
                             and sample["statistics"].get("landing_candidates", 0) > 0,
                             "unbudgeted landing must demonstrate actual candidate batches and candidates")
                else:
                    _require(memory["logical_memory_peak_bytes"] is not None and memory["coverage"] == LOGICAL_COVERAGE,
                             "candidate memory must have real covered peak")
            if memory["logical_memory_peak_bytes"] is None:
                native_fts_unavailable = (fts_recipe and scenario.family == "fts_matrix" and
                                          memory["coverage"] == "unavailable" and
                                          memory["unavailable_reason"] == "native_fts_memory_not_covered_by_query_budget_observer" and
                                          sample["rss_peak_bytes"] > 0 and sample["rss_scope"] in (
                                              "process_lifetime_peak_working_set_bytes", "process_lifetime_ru_maxrss_bytes"))
                rf08_unbudgeted = rf08_qualification and scenario.family == "unique_create" and scenario.query_memory_budget_bytes is None
                _require((scenario.family == "write" and scenario.writers > 1) or (historical and (arm == "A" or unbudgeted)) or native_fts_unavailable or rf08_unbudgeted or (data["intent"] == "exploratory" and bool(memory["unavailable_reason"])), "memory unavailable: cannot qualify")
            else:
                _integer(memory["logical_memory_peak_bytes"], "logical_memory_peak_bytes")
                _require(memory["logical_memory_peak_bytes"] >= memory["baseline_retained_bytes"], "memory peak below baseline")
                _require(memory["unavailable_reason"] is None and bool(memory["coverage"]), "memory coverage invalid")
                if data["intent"] == "qualification":
                    coverage_key = name + ":memory"
                    if coverage_key in sample_contracts:
                        _require(memory["coverage"] == sample_contracts[coverage_key], "memory measurement method changed between arms")
                    else:
                        sample_contracts[coverage_key] = memory["coverage"]
    _require(all(len(values) == 1 for values in revisions.values()), "revision changed inside an arm")
    _require(all(value == environments[0] for value in environments), "environment changed between arms")
    if data.get("calibration") is not None:
        calibration_matches(data)


def _distribution(values, unit="ns"):
    return {f"median_{unit}": statistics.median(values), f"p95_{unit}": sorted(values)[math.ceil(.95 * len(values)) - 1],
            f"minimum_{unit}": min(values), f"maximum_{unit}": max(values),
            f"stdev_{unit}": statistics.stdev(values) if len(values) > 1 else 0,
            "coefficient_of_variation": statistics.stdev(values) / statistics.mean(values)
            if len(values) > 1 and statistics.mean(values) != 0 else 0}


def validate_write_sample(sample, scenario, *, qualification):
    """Validate returned durable reports and each process's real logical peak."""
    write = sample.get("write", {})
    from bench.harness.campaign_fixtures import contract
    _require(sample.get("memory_budget_bytes") == scenario.query_memory_budget_bytes, "write memory budget differs from scenario")
    for key in ("wal_delta_bytes", "disk_delta_bytes"):
        _require(type(sample.get(key)) is int, "write allocated-file cost must be a signed integer")
    if qualification:
        _require(all(key in sample["statistics"] for key in WRITE_REQUIRED_COUNTERS),
                 "write physical work counters unavailable: cannot qualify")
    _require(write.get("atomic_unit") == contract(scenario)["transaction"] and
             write.get("throughput_boundary") == "sample elapsed interval through all returned durable ACKs",
             "write timing/atomicity boundary invalid")
    count = (scenario.write_rows if scenario.write_shape == "transactions" else
             math.ceil(scenario.write_rows / scenario.chunk_rows) if scenario.write_shape == "chunks" else 1)
    expected = count * scenario.writers
    reports = write.get("commit_reports", [])
    _require(len(reports) == expected and write.get("commits") == expected
             and write.get("durable_acks") == expected, "write commits/ACKs inconsistent")
    for report in reports:
        _integer(report.get("csn"), "commit csn", 1)
        _require(report.get("durable") is True and report.get("wrote") is True, "write ACK not durable")
        for key in ("writer", "offset", "rows", "ack_duration_ns"):
            _integer(report.get(key), "write ACK " + key, 1 if key in ("rows", "ack_duration_ns") else 0)
    _require(len({r["csn"] for r in reports}) == expected, "duplicate commit report")
    _require(write.get("business_rows") == scenario.write_rows * scenario.writers, "write business rows inconsistent")
    _require(write.get("shape") == scenario.write_shape and write.get("partial_progress") is scenario.partial_progress,
             "write atomicity differs from scenario")
    _require(write.get("rows_per_second") == scenario.write_rows * scenario.writers * 1e9 / sample["elapsed_ns"]
             and write.get("durable_acks_per_second") == expected * 1e9 / sample["elapsed_ns"], "write throughput inconsistent")
    memories = [sample["memory"]]
    if scenario.writers > 1:
        writers = sample.get("writers", [])
        _require(len(writers) == scenario.writers and {w.get("writer") for w in writers} == set(range(scenario.writers)),
                 "missing or duplicate writer proof")
        _require(len({w.get("process_id") for w in writers}) == scenario.writers, "writers must be independent processes")
        for writer in writers:
            _integer(sample.get("barrier_release_ns"), "barrier release", 1)
            _integer(writer.get("started_ns"), "writer start", 1)
            _require(sample["barrier_release_ns"] <= writer["started_ns"] and
                     writer["started_ns"] + writer["elapsed_ns"] <= sample["barrier_release_ns"] + sample["elapsed_ns"],
                     "writer work falls outside common barrier interval")
            _integer(writer.get("rss_peak_bytes"), "writer RSS", 1)
            _require(writer.get("rss_scope") in ("process_lifetime_peak_working_set_bytes", "process_lifetime_ru_maxrss_bytes"),
                     "writer RSS scope invalid")
            _require(writer.get("memory_budget_bytes") == scenario.query_memory_budget_bytes, "writer memory budget differs from scenario")
            _integer(writer.get("process_id"), "writer process ID", 1)
            _require(len(writer.get("commit_reports", [])) == count, "writer ACK count differs from atomic units")
            _require(sum(r["ack_duration_ns"] for r in writer["commit_reports"]) == writer["commit_ns"],
                     "writer ACK durations differ from measured commit interval")
            for key in ("elapsed_ns", "cpu_ns", "commit_ns"):
                _integer(writer.get(key), "writer " + key)
            _require(writer["elapsed_ns"] > 0 and writer["commit_ns"] <= writer["elapsed_ns"], "writer ACK boundary invalid")
        _require([r for w in writers for r in w.get("commit_reports", [])] == reports, "writer reports differ from aggregate")
        _require(sample["memory"]["logical_memory_peak_bytes"] is None and sample["memory"]["unavailable_reason"] ==
                 "concurrent_process_peaks_reported_per_writer_not_aggregated", "concurrent memory cannot aggregate independent peaks")
        memories = [w["memory"] for w in writers]
    else:
        _require(sum(r["ack_duration_ns"] for r in reports) == sample["commit_ns"],
                 "write ACK durations differ from measured commit interval")
    for writer_id in range(scenario.writers):
        writer_reports = [r for r in reports if r["writer"] == writer_id]
        _require(len(writer_reports) == count, "writer atomic units missing or duplicated")
        expected_offset = 0
        for report in writer_reports:
            _require(report["offset"] == expected_offset, "write atomic unit offset differs from input")
            expected_rows = (1 if scenario.write_shape == "transactions" else
                             min(scenario.chunk_rows, scenario.write_rows - expected_offset)
                             if scenario.write_shape == "chunks" else scenario.write_rows)
            _require(report["rows"] == expected_rows, "write atomic unit row count differs from input")
            expected_offset += expected_rows
        _require(expected_offset == scenario.write_rows, "write atomic units do not cover input")
    for memory in memories:
        peak = memory.get("logical_memory_peak_bytes")
        if peak is None:
            _require(not qualification and bool(memory.get("unavailable_reason")), "writer memory unavailable: cannot qualify")
            continue
        _integer(peak, "writer memory peak")
        _integer(memory.get("baseline_retained_bytes"), "writer memory baseline")
        _require(peak >= memory["baseline_retained_bytes"] and memory.get("unavailable_reason") is None, "writer memory invalid")
        if qualification:
            _require(scenario.query_memory_budget_bytes is not None and memory.get("coverage") in (
                LOGICAL_COVERAGE, "simultaneous_sequence_query_budget_and_handle_owner_endpoint_tariffs_v1"), "writer memory coverage invalid")


def write_tradeoff(samples, scenario):
    """Aggregate recipe costs without attributing sample totals to an ACK."""
    by_arm = {}
    for arm, rows in samples.items():
        atomic_units = {}
        for sample in rows:
            for report in sample["write"]["commit_reports"]:
                key = (report["writer"], report["offset"], report["rows"])
                atomic_units.setdefault(key, []).append(report["ack_duration_ns"])
        physical = {key: _distribution([sample["statistics"][key] for sample in rows], "count")
                    for key in WRITE_PHYSICAL_COUNTERS if all(key in sample["statistics"] for sample in rows)}
        runtime_keys = sorted(set.intersection(*(set(sample["statistics"]) for sample in rows)))
        by_arm[arm] = {
            **{field: _distribution([sample["write"][field] for sample in rows],
                                    "per_second" if field.endswith("per_second") else "count")
               for field in ("commits", "durable_acks", "business_rows", "rows_per_second",
                             "durable_acks_per_second")},
            "ack_duration_ns": _distribution([report["ack_duration_ns"] for sample in rows
                                               for report in sample["write"]["commit_reports"]]),
            "ack_duration_by_atomic_unit": [
                {"writer": writer, "offset": offset, "rows": count,
                 "duration": _distribution(values)}
                for (writer, offset, count), values in sorted(atomic_units.items())],
            "physical_work_per_sample": physical,
            "physical_work_unavailable": [key for key in WRITE_PHYSICAL_COUNTERS if key not in physical],
            "runtime_statement_counters_per_sample": {
                key: _distribution([sample["statistics"][key] for sample in rows], "count")
                for key in runtime_keys},
            "allocated_file_bytes_per_sample": {
                "wal_delta_bytes": _distribution([sample["wal_delta_bytes"] for sample in rows], "bytes"),
                "disk_delta_bytes": _distribution([sample["disk_delta_bytes"] for sample in rows], "bytes")},
            "amortized_cost_per_atomic_unit": {
                "elapsed_ns": _distribution([sample["elapsed_ns"] / sample["write"]["durable_acks"]
                                             for sample in rows]),
                "cpu_ns": _distribution([sample["cpu_ns"] / sample["write"]["durable_acks"]
                                         for sample in rows]),
                "wal_allocated_delta_bytes": _distribution([
                    sample["wal_delta_bytes"] / sample["write"]["durable_acks"]
                    for sample in rows], "bytes"),
                "disk_allocated_delta_bytes": _distribution([
                    sample["disk_delta_bytes"] / sample["write"]["durable_acks"]
                    for sample in rows], "bytes")},
        }
    return {"atomic_unit": contract(scenario)["transaction"], "partial_progress": scenario.partial_progress,
            "comparison_limit": "ACK counts and atomic units remain explicit; different shapes are not equivalent contracts",
            "ack_duration_scope": "individual returned durable commit call; no replay of uncertain outcomes",
            "allocated_file_bytes_scope": "net allocated file length change per complete sample; not logical WAL bytes or fsync count",
            "amortized_cost_scope": "sample total divided by returned ACK count; concurrent wall time overlaps and these are not individual ACK latencies or attributable bytes",
            "physical_work_scope": "runtime statement counters per complete sample; unavailable counters are listed, never imputed as zero",
            "runtime_counter_scope": "returned statement statistics, including logical rows_created; these are not physical-write counters",
            "by_arm": by_arm}


def matched_write_loads(scenarios, comparisons):
    """Index same-load recipes without implying equivalent atomic contracts."""
    by_load = {}
    for scenario in scenarios:
        if scenario.family != "write":
            continue
        load = (scenario.write_rows, scenario.payload_bytes, scenario.writers)
        pairs = [item for item in comparisons if item["scenario_id"] == scenario.name]
        by_load.setdefault(load, []).append({
            "scenario_id": scenario.name, "shape": scenario.write_shape,
            "atomic_unit": contract(scenario)["transaction"],
            "partial_progress": scenario.partial_progress,
            "pairs": [{"pair": item["pair"], "by_arm": {
                arm: {"rows_per_second_median": item["write_tradeoff"]["by_arm"][arm]["rows_per_second"]["median_per_second"],
                      "durable_acks_per_sample": item["write_tradeoff"]["by_arm"][arm]["durable_acks"]["median_count"],
                      "ack_duration_median_ns": item["write_tradeoff"]["by_arm"][arm]["ack_duration_ns"]["median_ns"]}
                for arm in ("A", "B")}} for item in pairs]})
    return [{"write_rows_per_writer": rows, "payload_bytes": size, "writers": writers,
             "shapes": sorted(entries, key=lambda item: item["shape"])}
            for (rows, size, writers), entries in sorted(by_load.items())]


def compare_campaign(data):
    validate_campaign(data)
    comparisons, totals = [], []
    calibrated = calibration_matches(data)
    noisy = not calibrated or any(
        run[key] > data["protocol"]["max_cpu_percent"] for run in data["runs"]
        for key in ("cpu_before_percent", "cpu_after_percent"))
    for spec in data["scenarios"]:
        scenario = Scenario.from_dict(spec)
        all_values = {"A": [], "B": []}
        for pair in range(3):
            samples = {run["arm"]: [s for s in run["samples"] if s["scenario_id"] == scenario.name and s["kind"] == "retained"]
                       for run in data["runs"] if run["pair"] == pair}
            values = {arm: [s["elapsed_ns"] for s in rows] for arm, rows in samples.items()}
            distributions = {arm: _distribution(v) for arm, v in values.items()}
            for arm in ("A", "B"):
                all_values[arm].extend(values[arm])
            med = distributions["B"]["median_ns"] / distributions["A"]["median_ns"]
            tail = distributions["B"]["p95_ns"] / distributions["A"]["p95_ns"]
            limit, tail_limit = (1.03, 1.05) if data["mode"] == "noop" else (1.05, 1.10)
            work_reduced = None
            if data["mode"] == "optimization" and scenario.role == "target":
                limit = .90
                tail_limit = None
                key = scenario.work_counter
                _require(all(key in s["statistics"] for rows in samples.values() for s in rows), "target physical counter missing")
                work_reduced = all(b["statistics"][key] < a["statistics"][key]
                                   for a, b in zip(samples["A"], samples["B"]))
            measures = {}
            for field in ("prepare_ns", "cpu_ns", "first_row_ns", "consume_all_ns", "commit_ns", "rss_peak_bytes"):
                measures[field] = {arm: _distribution([s[field] for s in rows], "bytes" if field == "rss_peak_bytes" else "ns")
                                   if all(s[field] is not None for s in rows) else None
                                   for arm, rows in samples.items()}
            comparisons.append({"scenario_id": scenario.name, "pair": pair, "distributions": distributions,
                                "phase_distributions": measures,
                                "median_ratio": med, "p95_ratio": tail, "median_limit": limit,
                                "p95_limit": tail_limit, "physical_work_reduced": work_reduced,
                                "thresholds_met": med <= limit and (tail_limit is None or tail <= tail_limit) and work_reduced is not False})
            if scenario.rf04_case is not None:
                comparisons[-1]["rf04_tradeoff"] = {
                    "case": scenario.rf04_case,
                    "plan_scope": "EXPLAIN may include unexecuted alternatives; regimes come from runtime counters",
                    "rss_scope": "process-lifetime peak, not query-attributable allocation",
                    "by_arm": {arm: {
                        "edge_regimes": {regime: sum(s["rf04_access"]["edge_regime"] == regime for s in rows)
                                         for regime in ("endpoint", "grouped", "endpoint_then_grouped", "no_edge_access")},
                        "conditional_seek_in_plan": sum(s["rf04_access"]["conditional_seek_in_plan"] for s in rows),
                        "physical_node_versions_examined": _distribution([
                            s["statistics"]["physical_node_versions_examined"] for s in rows], "count"),
                        "physical_edge_versions_examined": _distribution([
                            s["statistics"]["physical_edge_versions_examined"] for s in rows], "count"),
                        "edge_lookups": _distribution([s["statistics"].get("edge_lookups", 0) for s in rows], "count"),
                        "edge_scans": _distribution([s["statistics"].get("edge_scans", 0) for s in rows], "count"),
                        "logical_memory_peak_bytes": _distribution([
                            s["memory"]["logical_memory_peak_bytes"] for s in rows], "bytes")
                        if all(type(s["memory"]["logical_memory_peak_bytes"]) is int for s in rows) else None,
                    } for arm, rows in samples.items()},
                }
            if scenario.family == "write":
                comparisons[-1]["write_tradeoff"] = write_tradeoff(samples, scenario)
            if scenario.family == "unique_create":
                comparisons[-1]["unique_create_diagnostic"] = {
                    "atomic_unit": "one transaction; one durable ACK",
                    "memory_limit": "unbudgeted batch has no logical peak; budgeted scalar control is separate",
                    "comparison_limit": ("qualified batch timing is unbudgeted; quota control proves scalar memory only"
                                         if data["intent"] == "qualification" else
                                         "exploratory RF08 diagnostic"),
                    "by_arm": {arm: {
                        "batch_groups": _distribution([s["statistics"].get("pk_uniqueness_batches", 0)
                                                       for s in rows], "count"),
                        "certificate_attempts": _distribution([s["statistics"].get("certificate_attempts", 0)
                                                               for s in rows], "count"),
                        "durable_acks": sum(s["unique_create"]["ack_count"] for s in rows),
                        "logical_memory_coverage": sorted({s["memory"]["coverage"] for s in rows}),
                    } for arm, rows in samples.items()},
                }
            if scenario.family == "fts_matrix":
                comparisons[-1]["fts_tradeoff"] = {
                    "expected_source": fts_qualification.expected_source(scenario),
                    "logical_memory": "unavailable; native search is outside the Cypher query-budget observer",
                    "wal_bytes_scope": "allocated WAL file bytes, not logical bytes appended or fsync count",
                    "setup_scope": "seed insert, index creation, checkpoint and close; not isolated index maintenance",
                    "by_arm": {arm: {
                        "source_counts": {source: sum(s["fts_source"]["statistics_regime"] == source for s in rows)
                                          for source in ("snapshot_cache", "wal_delta", "durable_summary", "full_census")},
                        "postings_visited": _distribution([s["statistics"]["postings_visited"] for s in rows], "count"),
                        "statistics_wal_records": _distribution([s["statistics"]["statistics_wal_records"] for s in rows], "count"),
                        "setup_elapsed_ns": _distribution([s["fts_setup"]["elapsed_ns"] for s in rows]),
                        "setup_disk_allocated_bytes": _distribution([s["fts_setup"]["disk_allocated_bytes"] for s in rows], "bytes"),
                        "setup_wal_allocated_bytes": _distribution([s["fts_setup"]["wal_allocated_bytes"] for s in rows], "bytes"),
                        "update_elapsed_ns": _distribution([s["fts_maintenance"]["elapsed_ns"] for s in rows])
                        if scenario.fts_updates else None,
                        "update_commit_ack_ns": _distribution([duration for s in rows
                                                                for duration in s["fts_maintenance"]["commit_ns"]])
                        if scenario.fts_updates else None,
                        "update_disk_allocated_delta_bytes": _distribution([
                            s["fts_maintenance"]["disk_allocated_delta_bytes"] for s in rows], "bytes")
                        if scenario.fts_updates else None,
                        "update_wal_allocated_delta_bytes": _distribution([
                            s["fts_maintenance"]["wal_allocated_delta_bytes"] for s in rows], "bytes")
                        if scenario.fts_updates else None,
                    } for arm, rows in samples.items()},
                }
            noisy |= any(d["coefficient_of_variation"] > .10 for d in distributions.values())
        totals.append({"scenario_id": scenario.name, "all_retained": {arm: _distribution(values) for arm, values in all_values.items()}})
    decision = "passed" if all(c["thresholds_met"] for c in comparisons) else "failed"
    if noisy:
        decision = "inconclusive"
    if data["intent"] == "exploratory":
        decision = "exploratory"
    elif data["mode"] == "recipe" and not noisy:
        decision = "tradeoff_reported"
    return {"schema_version": "grafx-campaign-report/v1", "campaign_hash": canonical_hash(data),
            "qualification_scope": data.get("qualification_scope", "comparable_observer"),
            "memory_regime": data.get("memory_regime", "budgeted"),
            "retry_scope": "single_campaign_only",
             "comparative_logical_memory": data.get("qualification_scope") != HISTORICAL_SCOPE and data["intent"] == "qualification" and not all(Scenario.from_dict(s).family == "fts_matrix" for s in data["scenarios"]),
            "candidate_memory_verified": data.get("qualification_scope") == HISTORICAL_SCOPE,
            "decision": decision, "comparisons": comparisons, "overall": totals,
            "matched_write_loads": matched_write_loads([Scenario.from_dict(s) for s in data["scenarios"]], comparisons)
            if data["mode"] == "recipe" else [],
            "calibration_valid": calibrated,
            "noisy": noisy, "noise_policy": "CPU > registered limit or within-process CV >10%; at most one complete rerun",
            "percentile_scope": "descriptive retained samples, not production SLA or p99",
            "raw_samples_retained": True}
