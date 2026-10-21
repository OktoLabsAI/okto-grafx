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
