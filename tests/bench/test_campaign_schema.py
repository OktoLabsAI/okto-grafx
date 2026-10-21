"""Campaign decisions fail closed before any performance arithmetic."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from bench.harness.campaign_schema import (
    CampaignError, canonical_hash, compare_campaign, load_json, validate_campaign,
)


def campaign():
    from bench.harness.campaign_fixtures import Scenario
    from bench.harness.campaign_worker import sample_record

    scenario = Scenario(name="constant", family="constant", nodes=4).to_dict()
    runs = []
    for pair, order in enumerate(("AB", "BA", "AB")):
        for arm in order:
            samples = []
            for index in range(25):
                sample = sample_record(scenario, index, ((1,),), {"rows_returned": 1},
                                       elapsed_ns=100, cpu_ns=10, prepare_ns=2,
                                       memory={"logical_memory_peak_bytes": 0,
                                               "baseline_retained_bytes": 0,
                                               "coverage": "instrumented budgets",
                                               "unavailable_reason": None}, rss_peak_bytes=100)
                samples.append(sample)
            runs.append({"arm": arm, "pair": pair, "run_order": order,
                         "process_id": 100 + len(runs), "invocation_id": str(len(runs)),
                         "revision": ("a" if arm == "A" else "b") * 40,
                         "source_clean": True, "package_file": "/snapshot/src/okto_grafx/__init__.py",
                         "source_root": "/snapshot", "environment": {"python": "3.11", "dependencies": {},
                         "os": "test", "cpu": "test", "cpu_count": 1, "ram_bytes": 1,
                         "disk": {"free_bytes": 1}, "gc_enabled": True},
                         "cpu_before_percent": 0, "cpu_after_percent": 0,
                         "metrics_sink": "NoOpMetricsSink", "samples": samples})
    data = {"schema_version": "grafx-campaign/v1", "mode": "noop", "intent": "qualification",
            "started_at": "2026-10-21T10:00:00+00:00",
            "calibrated_executor": True, "command": ["python", "-m", "bench.harness.campaign"],
            "protocol": {"pairs": 3, "warmups": 5, "retained": 20, "orders": ["AB", "BA", "AB"],
                         "attempt": 1, "previous_campaign_sha256": None, "max_cpu_percent": 20},
            "scenarios": [scenario], "runs": runs}
    environment = {k: runs[0]["environment"][k] for k in
                   ("python", "dependencies", "os", "cpu", "cpu_count", "ram_bytes", "gc_enabled")}
    root = Path(__file__).resolve().parents[2] / "bench" / "harness"
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
               for name in ("campaign.py", "campaign_schema.py", "campaign_worker.py")}
    raw = json.dumps({"schema_version": "grafx-executor-calibration/v1", "observed_at": data["started_at"],
                      "environment_hash": canonical_hash(environment),
                      "environment": environment, "warmup_ns": [100] * 5, "producer_process_id": 123,
                      "clock": {"name": "perf_counter_ns", "resolution_ns": 100},
                      "procedure": {"id": "sha256-fixed-payload-v1",
                                    "command": ["synthetic-test-python", "-m", "bench.harness.campaign", "calibrate"],
                                    "source_sha256": sources["campaign.py"], "source_files": sources,
                                    "control": {"algorithm": "sha256", "payload_bytes": 65536, "iterations_per_sample": 128}},
                      "elapsed_ns": [100] * 20, "cpu_percent": [0] * 5})
    data["calibration"] = {"raw_json": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "path": "test-only"}
    return data


def test_complete_campaign_and_descriptive_report():
    data = campaign()
    validate_campaign(data)
    report = compare_campaign(data)
    assert report["decision"] == "passed"
    assert len(report["comparisons"]) == 3
    assert report["comparisons"][0]["median_ratio"] == 1


def test_calibration_accepts_exact_isolated_launcher_but_rejects_other_code(tmp_path):
    from bench.harness.campaign_schema import CALIBRATION_LAUNCHER, calibration_command_valid
    command = ["python", "-E", "-P", "-B", "-c", CALIBRATION_LAUNCHER, str(tmp_path), "calibrate", "--output", "receipt"]
    assert calibration_command_valid(command)
    command[5] += ";print('unregistered code')"
    assert not calibration_command_valid(command)


@pytest.mark.parametrize("change", [
    lambda d: d["runs"].pop(),
    lambda d: d["runs"][0]["samples"].pop(),
    lambda d: d["runs"][0]["samples"][0].update(elapsed_ns=float("nan")),
    lambda d: d["runs"][0]["samples"][0].pop("result_hash"),
    lambda d: d["runs"][0]["samples"][0].update(result_hash="0" * 64),
    lambda d: d["runs"][0]["samples"][0].update(query_hash="0" * 64),
    lambda d: d["runs"][0]["samples"].append(copy.deepcopy(d["runs"][0]["samples"][0])),
    lambda d: d["runs"][0].update(source_clean=False),
    lambda d: d["runs"][0].pop("environment"),
    lambda d: d["protocol"].update(attempt=3),
])
def test_invalid_evidence_never_receives_ratios(change):
    data = campaign()
    change(data)
    with pytest.raises(CampaignError):
        compare_campaign(data)


def test_types_multiplicity_and_order_participate_in_hash():
    assert len({canonical_hash(x) for x in [((1,),), ((True,),), ((1.0,),), ((1,), (1,))]}) == 4
    assert canonical_hash(((1,), (2,))) != canonical_hash(((2,), (1,)))


def test_duplicate_json_keys_rejected():
    with pytest.raises(CampaignError, match="duplicate"):
        load_json('{"elapsed_ns":1,"elapsed_ns":2}')


def test_noise_and_exploratory_never_pass():
    data = campaign()
    data["runs"][0]["cpu_before_percent"] = 21
    assert compare_campaign(data)["decision"] == "inconclusive"
    data["runs"][0]["cpu_before_percent"] = 0
    for sample in data["runs"][1]["samples"]:
        sample.update(elapsed_ns=104, consume_all_ns=104)
    assert compare_campaign(data)["decision"] == "failed"
    del data["calibration"]
    assert compare_campaign(data)["decision"] == "inconclusive"
    data["intent"] = "exploratory"
    assert compare_campaign(data)["decision"] == "exploratory"


def test_noop_thresholds_each_pair_and_tail():
    data = campaign()
    for sample in data["runs"][1]["samples"]:
        sample["elapsed_ns"] = 104
        sample["consume_all_ns"] = 104
    assert compare_campaign(data)["decision"] == "failed"


def test_missing_memory_cannot_qualify():
    data = campaign()
    data["runs"][0]["samples"][0]["memory"].update(logical_memory_peak_bytes=None,
        unavailable_reason="baseline lacks observer")
    with pytest.raises(CampaignError, match="memory"):
        compare_campaign(data)
    data["intent"] = "exploratory"
    assert compare_campaign(data)["decision"] == "exploratory"


def test_order_is_observed_not_only_declared():
    data = campaign()
    data["runs"][0], data["runs"][1] = data["runs"][1], data["runs"][0]
    with pytest.raises(CampaignError, match="actual process order"):
        compare_campaign(data)


def test_registered_cpu_policy_is_not_a_universal_twenty_percent_limit():
    data = campaign()
    data["protocol"]["max_cpu_percent"] = 30
    data["runs"][0]["cpu_before_percent"] = 25
    assert compare_campaign(data)["decision"] == "passed"


def test_boolean_calibration_claim_is_insufficient():
    data = campaign()
    del data["calibration"]
    assert compare_campaign(data)["decision"] == "inconclusive"


def test_calibration_hash_is_verified():
    data = campaign()
    data["calibration"]["sha256"] = "0" * 64
    with pytest.raises(CampaignError, match="calibration file hash"):
        compare_campaign(data)


def test_quantized_cpu_clock_zero_samples_are_not_divided_by_zero():
    data = campaign()
    for run in data["runs"]:
        for sample in run["samples"]:
            sample["cpu_ns"] = 0
    assert compare_campaign(data)["decision"] == "passed"


def test_calibration_procedure_digest_must_identify_real_producer():
    data = campaign()
    receipt = json.loads(data["calibration"]["raw_json"])
    receipt["procedure"]["source_sha256"] = "0" * 64
    raw = json.dumps(receipt)
    data["calibration"].update(raw_json=raw, sha256=hashlib.sha256(raw.encode()).hexdigest())
    with pytest.raises(CampaignError, match="procedure provenance"):
        compare_campaign(data)


def historical_campaign():
    """Synthetic validator fixture only; these numbers are not benchmark proof."""
    from bench.harness.campaign_fixtures import Scenario
    from bench.harness.campaign_worker import sample_record
    data = campaign()
    data["qualification_scope"] = "rf01_historical_total_overhead"
    spec = Scenario(name="constant", family="constant", nodes=4, query_memory_budget_bytes=4096).to_dict()
    data["scenarios"] = [spec]
    producer = Path(__file__).resolve().parents[2] / "bench/harness/campaign_worker.py"
    for run in data["runs"]:
        baseline = run["arm"] == "A"
        if baseline:
            run["revision"] = "3a2239a0bda80223c298a68d6d14f3c951f133d2"
        run["source_tree"] = "c2795181cda21041998cd13959afc8de303802bb" if baseline else "b" * 40
        run["engine_source_hashes"] = {
            "src/okto_grafx/engine/query_engine.py": "8056d0625cf4764c3a160bb676b6cefdc71d680f7f52bba44b626a28e0fa7ad8" if baseline else "b" * 64,
            "src/okto_grafx/domain/query/memory.py": "c" * 64}
        run["logical_observer_available"] = not baseline
        memory = {"logical_memory_peak_bytes": None if baseline else 0,
                  "baseline_retained_bytes": 0, "coverage": "unavailable" if baseline else "simultaneous_query_budget_and_handle_owner_endpoint_tariffs_v1",
                  "unavailable_reason": "revision_has_no_logical_memory_observer" if baseline else None}
        run["samples"] = [sample_record(spec, i, ((1,),), {"rows_returned": 1}, elapsed_ns=100,
                            cpu_ns=10, prepare_ns=2, memory=memory.copy(), rss_peak_bytes=100,
                            rss_scope="process_lifetime_peak_working_set_bytes") for i in range(25)]
        if not baseline:
            rows = []
            for mode in ("success", "error", "cancel"):
                for index in range(20):
                    rows.append({"mode": mode, "index": index, "live_before_bytes": 0,
                                 "live_after_bytes": 0, "handle_before_bytes": 0, "handle_after_bytes": 0,
                                 "memory": dict(memory, logical_memory_peak_bytes=100),
                                 "operator_budgets": [{"operator": "sort", "limit_bytes": 4096, "peak_bytes": 100}],
                                 "error_code": {"success": None, "error": "plan_error", "cancel": "query_cancelled"}[mode],
                                 "open_transactions_after": 0})
            body = {"schema_version": "grafx-candidate-memory/v1", "revision": run["revision"],
                    "process_id": run["process_id"], "invocation_id": run["invocation_id"],
                    "engine_source_hashes": run["engine_source_hashes"],
                    "procedure": {"id": "rf01-logical-lifecycle-v1", "source_sha256": hashlib.sha256(producer.read_bytes()).hexdigest()},
                    "budgets": [{"budget_bytes": 4096, "records": rows}]}
            run["candidate_memory_evidence"] = {"body": body, "sha256": canonical_hash(body)}
    return data


def test_historical_null_is_a_narrow_temporal_exception():
    data = historical_campaign()
    report = compare_campaign(data)
    assert report["decision"] == "passed"
    assert report["qualification_scope"] == "rf01_historical_total_overhead"
    assert report["comparative_logical_memory"] is False
    assert report["candidate_memory_verified"] is True


def unbudgeted_landing_campaign():
    from bench.harness.campaign_fixtures import matrix, oracle
    from bench.harness.campaign_worker import sample_record
    data = historical_campaign()
    data.update(memory_regime="unbudgeted_landing", candidate_memory_budget_bytes=4096)
    scenario = next(s for s in matrix() if s.name == "landing-control")
    data["scenarios"] = [scenario.to_dict()]
    for run in data["runs"]:
        run.update(memory_regime="unbudgeted_landing", candidate_memory_budget_bytes=4096)
        memory = {"logical_memory_peak_bytes": None, "baseline_retained_bytes": 0,
                  "coverage": "unavailable", "unavailable_reason": "revision_has_no_logical_memory_observer"
                  if run["arm"] == "A" else "query_memory_budget_disabled"}
        run["samples"] = [sample_record(scenario.to_dict(), i, oracle(scenario),
            {"landing_batches": 1, "landing_candidates": 64}, elapsed_ns=100, cpu_ns=10, prepare_ns=2,
            memory=memory.copy(), rss_peak_bytes=100, rss_scope="process_lifetime_peak_working_set_bytes") for i in range(25)]
    return data


def test_unbudgeted_landing_keeps_missing_memory_and_separate_proof():
    report = compare_campaign(unbudgeted_landing_campaign())
    assert report["decision"] == "passed"
    assert report["memory_regime"] == "unbudgeted_landing"
    assert report["retry_scope"] == "single_campaign_only"


@pytest.mark.parametrize("change", [
    lambda d: d.update(candidate_memory_budget_bytes=None),
    lambda d: d.update(candidate_memory_budget_bytes=0),
    lambda d: d.update(memory_regime="unknown"),
    lambda d: d.pop("qualification_scope"),
    lambda d: d["runs"][1].update(candidate_memory_budget_bytes=8192),
    lambda d: d["runs"][1]["samples"][0]["memory"].update(logical_memory_peak_bytes=0),
    lambda d: d["runs"][1]["samples"][0]["memory"].update(unavailable_reason="revision_has_no_logical_memory_observer"),
    lambda d: d["runs"][1]["samples"][0]["statistics"].update(landing_batches=0),
    lambda d: d["runs"][1].pop("candidate_memory_evidence"),
])
def test_unbudgeted_landing_fails_closed(change):
    data = unbudgeted_landing_campaign()
    change(data)
    with pytest.raises(CampaignError):
        compare_campaign(data)


@pytest.mark.parametrize("change", [
    lambda d: d.pop("qualification_scope"),
    lambda d: d.update(mode="recipe"),
    lambda d: d["runs"][0].update(revision="a" * 40),
    lambda d: d["runs"][0].update(source_tree="0" * 40),
    lambda d: d["runs"][0].update(logical_observer_available=True),
    lambda d: d["runs"][0]["samples"][0]["memory"].update(unavailable_reason="query_memory_budget_disabled"),
    lambda d: d["runs"][0]["samples"][0]["memory"].update(logical_memory_peak_bytes=0),
    lambda d: d["runs"][1]["samples"][0]["memory"].update(logical_memory_peak_bytes=None, unavailable_reason="revision_has_no_logical_memory_observer"),
    lambda d: d["runs"][1].pop("candidate_memory_evidence"),
    lambda d: d["runs"][1]["candidate_memory_evidence"].update(sha256="0" * 64),
    lambda d: d["runs"][0]["samples"][0].update(rss_peak_bytes=0),
    lambda d: d["protocol"].update(max_cpu_percent=30),
])
def test_historical_exemption_does_not_spread(change):
    data = historical_campaign()
    change(data)
    with pytest.raises(CampaignError):
        compare_campaign(data)


@pytest.mark.parametrize("field,value", [("live_after_bytes", 1), ("handle_after_bytes", 1),
                                        ("open_transactions_after", 1), ("error_code", None)])
def test_historical_memory_proof_cannot_claim_cleanup(field, value):
    data = historical_campaign()
    evidence = data["runs"][1]["candidate_memory_evidence"]
    evidence["body"]["budgets"][0]["records"][20][field] = value
    evidence["sha256"] = canonical_hash(evidence["body"])
    with pytest.raises(CampaignError):
        compare_campaign(data)


def test_historical_timing_still_requires_calibration_and_noise_and_thresholds():
    data = historical_campaign()
    data["runs"][0]["cpu_before_percent"] = 21
    assert compare_campaign(data)["decision"] == "inconclusive"


@pytest.mark.parametrize("change", [
    lambda b: b.update(revision="c" * 40),
    lambda b: b["procedure"].update(source_sha256="0" * 64),
    lambda b: b["budgets"][0]["records"].pop(),
    lambda b: b["budgets"][0]["records"][0]["operator_budgets"][0].update(peak_bytes=4097),
    lambda b: b["budgets"][0]["records"][0]["memory"].update(coverage="unknown"),
])
def test_historical_memory_proof_validates_content_not_only_digest(change):
    data = historical_campaign()
    evidence = data["runs"][1]["candidate_memory_evidence"]
    change(evidence["body"])
    evidence["sha256"] = canonical_hash(evidence["body"])
    with pytest.raises(CampaignError):
        compare_campaign(data)
