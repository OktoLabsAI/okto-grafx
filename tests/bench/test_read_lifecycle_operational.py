"""RF10 operational bridge contract; no timed campaign runs in ordinary pytest."""
import json
from pathlib import Path
import sys
import unittest

import pytest

from bench.harness import read_lifecycle_operational as operational
from bench.harness.read_lifecycle_qualification import matrix


def manifest():
    return {"schema_version": "grafx-rf10-runner/v1", "candidate_revision": "b" * 40,
            "baseline_revision": "a" * 40, "memory_budget_bytes": 1048576,
            "timeout_seconds": 3500, "scenarios": [scenario.name for scenario in matrix()]}


def test_manifest_requires_exact_eight_slices_one_budget_and_immutable_revisions():
    valid = manifest()
    operational.validate_manifest(valid)
    for change in ({"scenarios": valid["scenarios"][:-1]},
                   {"scenarios": list(reversed(valid["scenarios"]))},
                   {"scenarios": valid["scenarios"] + valid["scenarios"][:1]},
                   {"memory_budget_bytes": 0}, {"timeout_seconds": 3601},
                   {"candidate_revision": "HEAD"}, {"attempt": 3}):
        with pytest.raises(ValueError):
            operational.validate_manifest(dict(valid, **change))


def test_missing_manifest_fails_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        operational.run(Path(__file__).resolve().parents[2])


def test_nonzero_phase_requires_complete_inconclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(operational, "inspect_result", lambda *_: {"decision": "inconclusive"})
    operational.phase_result(2, 1, tmp_path, {})
    for code, phase in ((1, 1), (2, 0), (3, 1)):
        with pytest.raises(RuntimeError):
            operational.phase_result(code, phase, tmp_path, {})
    monkeypatch.setattr(operational, "inspect_result", lambda *_: {"decision": "failed"})
    with pytest.raises(RuntimeError):
        operational.phase_result(2, 1, tmp_path, {})


def test_repeat_binds_complete_first_attempt_and_raw_hash(tmp_path, monkeypatch):
    import hashlib

    previous = tmp_path / "runs/first"
    (previous / "campaign").mkdir(parents=True)
    raw = b'{"unit_test_only":true}'
    (previous / "campaign/campaign.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    old = dict(manifest(), candidate_root="candidate", baseline_root="baseline", python="python")
    old_manifest = previous / "operational-manifest.json"
    old_manifest.write_text(json.dumps(old), encoding="utf-8")
    manifest_digest = hashlib.sha256(old_manifest.read_bytes()).hexdigest()
    artifacts = {path.relative_to(previous).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in previous.rglob("*") if path.is_file()}
    (previous / "runner-receipt.json").write_text(json.dumps({
        "decision": "inconclusive", "manifest_sha256": manifest_digest,
        "artifact_sha256": artifacts,
    }), encoding="utf-8")
    repeat = dict(old, attempt=2, previous_output=str(previous), previous_campaign_sha256=digest)
    monkeypatch.setattr(operational, "inspect_result", lambda *_: {"decision": "inconclusive"})
    assert operational.verify_previous(tmp_path, repeat) == previous / "campaign/campaign.json"
    with pytest.raises(ValueError, match="changed scenarios"):
        operational.verify_previous(tmp_path, dict(repeat, scenarios=list(reversed(old["scenarios"]))))
    (previous / "campaign/campaign.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="artifact changed"):
        operational.verify_previous(tmp_path, repeat)


def test_repeat_rejects_unsealed_or_substituted_previous_artifacts(tmp_path, monkeypatch):
    import hashlib

    previous = tmp_path / "runs/first"
    (previous / "campaign").mkdir(parents=True)
    (previous / "campaign/campaign.json").write_text("{}", encoding="utf-8")
    old = dict(manifest(), candidate_root="candidate", baseline_root="baseline", python="python")
    old_manifest = previous / "operational-manifest.json"
    old_manifest.write_text(json.dumps(old), encoding="utf-8")
    artifacts = {path.relative_to(previous).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in previous.rglob("*") if path.is_file()}
    receipt_path = previous / "runner-receipt.json"
    receipt = {"decision": "inconclusive",
               "manifest_sha256": hashlib.sha256(old_manifest.read_bytes()).hexdigest(),
               "artifact_sha256": artifacts}
    repeat = dict(old, attempt=2, previous_output=str(previous),
                  previous_campaign_sha256=artifacts["campaign/campaign.json"])
    monkeypatch.setattr(operational, "inspect_result", lambda *_: {"decision": "inconclusive"})

    def check(candidate):
        receipt_path.write_text(json.dumps(candidate), encoding="utf-8")
        operational.verify_previous(tmp_path, repeat)

    check(receipt)
    with pytest.raises(ValueError, match="complete inconclusive"):
        check(dict(receipt, manifest_sha256="0" * 64))
    with pytest.raises(ValueError, match="inventory differs"):
        check(dict(receipt, artifact_sha256={"campaign/campaign.json": artifacts["campaign/campaign.json"]}))
    with pytest.raises(ValueError, match="artifact changed"):
        check(dict(receipt, artifact_sha256=dict(artifacts, **{"campaign/campaign.json": "not-a-hash"})))
    check(receipt)
    (previous / "campaign/extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory differs"):
        operational.verify_previous(tmp_path, repeat)


def test_supervisor_failure_preserves_audit(tmp_path, monkeypatch):
    from bench.harness import campaign

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Okto/Grafx"
    base.mkdir(parents=True)
    (base / "rf10-runner.json").write_text(json.dumps(manifest()), encoding="utf-8")

    def fail(command, log, timeout):
        assert timeout == 3500
        assert "read_lifecycle_operational" in command[5]
        log.write("child refused")
        return 1

    monkeypatch.setattr(campaign, "execute_worker", fail)
    with pytest.raises(RuntimeError, match="child failed"):
        operational.run(Path(__file__).resolve().parents[2])
    audit, = (base / "runs").iterdir()
    assert json.loads((audit / "supervisor-receipt.json").read_text())["decision"] == "failed"
    assert (audit / "supervisor.log").read_text() == "child refused"
    assert not (audit / "result.json").exists()


class RF10CampaignRunner(unittest.TestCase):
    """Explicit native runner selector, never an automatic pytest campaign."""
    __test__ = False

    def test_read_lifecycle_qualification(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        self.assertEqual(
            operational.run(Path(__file__).resolve().parents[2])["decision"],
            "tradeoff_reported",
        )
