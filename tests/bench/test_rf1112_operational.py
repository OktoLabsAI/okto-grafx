"""Static fences for the authenticated RF11/RF12 temporal partitions."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from bench.harness.campaign_fixtures import matrix
from bench.harness import rf1112_operational as operational
from bench.harness.rf1112_operational import PARTITIONS, SCHEMA, validate_manifest


BASELINE = "f0dbce09c2c4796473c93281123682520b213710"
CANDIDATE = "fc4654612135731ac6777ac248042a648204ac3c"


def manifest(partition):
    return {
        "schema_version": SCHEMA, "partition": partition,
        "runner_root": "C:/runner", "runner_revision": "a" * 40,
        "baseline_root": "C:/baseline", "baseline_revision": BASELINE,
        "candidate_root": "C:/candidate", "candidate_revision": CANDIDATE,
        "python": "C:/python.exe", "output": "C:/runs/one",
        "timeout_seconds": 3500, "scenarios": PARTITIONS[partition].copy(),
        "attempt": 1, "previous_output": None, "previous_campaign_sha256": None,
    }


def test_partitions_match_closed_spec_and_catalog():
    catalog = {s.name for s in matrix()}
    assert [len(PARTITIONS[f"P{i}"]) for i in range(1, 6)] == [9, 4, 2, 2, 8]
    assert [len(PARTITIONS[f"Q{i}"]) for i in range(1, 6)] == [3, 3, 3, 5, 4]
    assert len(set().union(*(set(PARTITIONS[f"P{i}"]) for i in range(1, 6)))) == 25
    assert len(set().union(*(set(PARTITIONS[f"Q{i}"]) for i in range(1, 6)))) == 15
    assert all(set(partition) <= catalog for partition in PARTITIONS.values())
    for name in PARTITIONS:
        validate_manifest(manifest(name), name)


@pytest.mark.parametrize("field,value", [
    ("scenarios", ["fts-matrix-n256-wal-h0-rare-warm"]),
    ("timeout_seconds", 3600),
    ("attempt", 2),
    ("candidate_revision", BASELINE),
    ("runner_revision", "short"),
])
def test_manifest_refuses_partial_or_unbounded_profile(field, value):
    candidate = deepcopy(manifest("P1"))
    candidate[field] = value
    with pytest.raises(ValueError):
        validate_manifest(candidate, "P1")


def test_partition_cannot_be_credited_as_another():
    with pytest.raises(ValueError):
        validate_manifest(manifest("Q1"), "Q2")


def test_retry_requires_frozen_lineage():
    candidate = manifest("Q5")
    candidate.update(attempt=2, previous_output="C:/runs/old",
                     previous_campaign_sha256="b" * 64)
    validate_manifest(candidate, "Q5")
    candidate["previous_campaign_sha256"] = "not a hash"
    with pytest.raises(ValueError):
        validate_manifest(candidate, "Q5")


def test_retry_requires_complete_receipt_inventory_and_manifest_binding(tmp_path, monkeypatch):
    previous = tmp_path / "runs" / "first"
    campaign = previous / "campaign" / "campaign.json"
    campaign.parent.mkdir(parents=True)
    campaign.write_text('{"synthetic_unit_test_only":true}', encoding="utf-8")
    old = manifest("Q5")
    old["output"] = str(previous)
    old_manifest = previous / "operational-manifest.json"
    old_manifest.write_text(json.dumps(old), encoding="utf-8")
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    artifacts = {"campaign/campaign.json": digest(campaign),
                 "operational-manifest.json": digest(old_manifest)}
    receipt_path = previous / "runner-receipt.json"
    receipt = {"partition": "Q5", "decision": "inconclusive",
               "manifest_sha256": digest(old_manifest), "artifact_sha256": artifacts}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    retry = manifest("Q5")
    retry.update(attempt=2, output=str(tmp_path / "runs" / "second"),
                 previous_output=str(previous),
                 previous_campaign_sha256=digest(campaign))
    monkeypatch.setattr(operational, "inspect_result", lambda *_: {"decision": "inconclusive"})
    assert operational._previous(tmp_path, retry) == campaign

    receipt["artifact_sha256"] = {"campaign/campaign.json": digest(campaign)}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact inventory"):
        operational._previous(tmp_path, retry)
    receipt["artifact_sha256"] = artifacts
    receipt["manifest_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        operational._previous(tmp_path, retry)
