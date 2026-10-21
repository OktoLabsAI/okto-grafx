"""RF08 campaign fixture proves physical work, ACKs and memory coverage."""
from dataclasses import replace
from argparse import Namespace

import pytest

from bench.harness import campaign_fixtures, campaign_schema, campaign_worker, uniqueness_qualification as rf08
from okto_grafx import connect
from okto_grafx.engine import query_engine as qe


def _sample(tmp_path, scenario, *, scalar=False):
    path = tmp_path / ("scalar" if scalar else scenario.name)
    with connect(path, query_memory_budget_bytes=scenario.query_memory_budget_bytes) as db:
        rf08.build_fixture(db, scenario)
        if scalar:
            original = qe._create_primary_key_batch_index
            qe._create_primary_key_batch_index = lambda *_: None
        try:
            sample = campaign_worker.measure_unique_create(db, scenario, 5)
        finally:
            if scalar:
                qe._create_primary_key_batch_index = original
        db.checkpoint()
    with connect(path, read_only=True) as reopened:
        assert reopened.execute(rf08.READBACK).rows == rf08.oracle(scenario)
        assert reopened.verify("all").findings == ()
    sample["unique_create"]["reopened"] = True
    return sample


def test_target_and_scalar_oracle_share_rows_but_not_certificate_work(tmp_path):
    target, _, quota = rf08.matrix(campaign_fixtures.Scenario)
    candidate = _sample(tmp_path, target)
    baseline = _sample(tmp_path, target, scalar=True)
    guarded = _sample(tmp_path, quota)
    assert candidate["result_hash"] == baseline["result_hash"]
    assert candidate["unique_create"]["ack_count"] == baseline["unique_create"]["ack_count"] == 1
    assert candidate["statistics"]["pk_uniqueness_batches"] == 2
    assert candidate["statistics"]["certificate_attempts"] == 2
    assert candidate["statistics"]["index_probe_keys"] == 128
    assert baseline["statistics"].get("pk_uniqueness_batches", 0) == 0
    assert baseline["statistics"]["certificate_attempts"] > candidate["statistics"]["certificate_attempts"]
    assert guarded["statistics"].get("pk_uniqueness_batches", 0) == 0
    assert candidate["memory"]["logical_memory_peak_bytes"] is None
    assert candidate["memory"]["unavailable_reason"] == "query_memory_budget_disabled"
    assert guarded["memory"]["logical_memory_peak_bytes"] is not None
    assert guarded["memory"]["logical_memory_peak_bytes"] <= rf08.QUOTA_BYTES
    assert guarded["unique_create"]["quota_retention"] == {
        "retained_before_bytes": guarded["memory"]["baseline_retained_bytes"],
        "retained_after_bytes": guarded["memory"]["baseline_retained_bytes"],
        "open_transactions_after": 0,
    }
    rf08.validate_sample(candidate, target, arm="B")
    rf08.validate_sample(baseline, target, arm="A")
    rf08.validate_sample(guarded, quota, arm="B")


def test_control_and_catalog_are_explicit(tmp_path):
    target, one, quota = rf08.matrix(campaign_fixtures.Scenario)
    catalog = {scenario.name for scenario in campaign_fixtures.matrix()}
    assert {target.name, one.name, quota.name} <= catalog
    sample = _sample(tmp_path, one)
    assert sample["rows"] == 1
    assert sample["statistics"].get("pk_uniqueness_batches", 0) == 0
    rf08.validate_sample(sample, one, arm="B")
    with pytest.raises(ValueError, match="role"):
        replace(target, role="control")
    with pytest.raises(ValueError, match="budget"):
        replace(quota, query_memory_budget_bytes=4096)


def test_metadata_tampering_cannot_claim_missing_ack_or_logical_peak(tmp_path):
    target = rf08.matrix(campaign_fixtures.Scenario)[0]
    sample = _sample(tmp_path, target)
    sample["unique_create"]["ack"]["durable"] = False
    with pytest.raises(ValueError, match="durably"):
        rf08.validate_sample(sample, target, arm="B")
    sample["unique_create"]["ack"]["durable"] = True
    sample["memory"]["logical_memory_peak_bytes"] = 0
    with pytest.raises(ValueError, match="cannot claim logical memory"):
        rf08.validate_sample(sample, target, arm="B")


def test_quota_control_rejects_retention_and_overbudget_peak(tmp_path):
    quota = rf08.matrix(campaign_fixtures.Scenario)[2]
    sample = _sample(tmp_path, quota)
    sample["unique_create"]["quota_retention"]["retained_after_bytes"] += 1
    with pytest.raises(ValueError, match="retained logical memory"):
        rf08.validate_sample(sample, quota, arm="B")
    sample["unique_create"]["quota_retention"]["retained_after_bytes"] -= 1
    sample["memory"]["logical_memory_peak_bytes"] = rf08.QUOTA_BYTES + 1
    with pytest.raises(ValueError, match="exceeds budget"):
        rf08.validate_sample(sample, quota, arm="B")


def test_qualification_requires_the_complete_rf08_matrix():
    target, one, quota = rf08.matrix(campaign_fixtures.Scenario)
    data = {
        "schema_version": "grafx-campaign/v1", "mode": "optimization",
        "intent": "qualification", "calibrated_executor": False,
        "command": ["python", "-m", "bench.harness.campaign"],
        "protocol": {"pairs": 3, "warmups": 5, "retained": 20,
                     "orders": ["AB", "BA", "AB"], "attempt": 1,
                     "previous_campaign_sha256": None, "max_cpu_percent": 20},
        "scenarios": [target.to_dict(), one.to_dict()], "runs": [],
    }
    with pytest.raises(campaign_schema.CampaignError, match="exact 128/1/128-quota"):
        campaign_schema.validate_campaign(data)
    data["scenarios"].append(quota.to_dict())
    with pytest.raises(campaign_schema.CampaignError, match="six independent process runs"):
        campaign_schema.validate_campaign(data)


def test_incomplete_rf08_selection_refused_before_campaign_workers(tmp_path):
    from bench.harness.campaign import run_campaign

    args = Namespace(output=tmp_path / "out", scenario=["rf08-create-128",
                     "rf08-create-1-control"], target=[], max_cpu_percent=20,
                     memory_budget=None, mode="optimization", intent="qualification")
    with pytest.raises(campaign_schema.CampaignError, match="exact 128/1/128-quota"):
        run_campaign(args)
    assert not (args.output / "manifest.json").exists()


def test_failed_duplicate_has_no_ack_or_persisted_prefix(tmp_path):
    target = rf08.matrix(campaign_fixtures.Scenario)[0]
    path = tmp_path / "duplicate"
    with connect(path) as db:
        rf08.build_fixture(db, target)
        with db.begin("write") as tx:
            with pytest.raises(Exception):
                tx.execute(rf08.INSERT, {"ids": [*range(65), 0]})
        assert db.execute(rf08.READBACK).rows == ()
        db.checkpoint()
    with connect(path, read_only=True) as reopened:
        assert reopened.execute(rf08.READBACK).rows == ()
        assert reopened.verify("all").findings == ()
