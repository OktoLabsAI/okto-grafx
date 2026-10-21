"""RF08 campaign fixture proves physical work, ACKs and memory coverage."""
from dataclasses import replace

import pytest

from bench.harness import campaign_fixtures, campaign_worker, uniqueness_qualification as rf08
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
