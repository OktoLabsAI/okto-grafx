"""RF09 operational gate tests; the explicit unittest selector runs real work."""
from pathlib import Path
import json
import sys
import unittest

import pytest


def test_operational_selection_requires_all_shapes_dimensions_and_fixed_budget():
    from bench.harness.ingestion_qualification import validate_manifest
    from bench.harness.campaign_fixtures import ingestion_matrix
    manifest = {"schema_version": "grafx-rf09-runner/v1", "candidate_revision": "b" * 40,
                "baseline_revision": "a" * 40, "memory_budget_bytes": 1048576,
                "timeout_seconds": 3500, "scenarios": [s.name for s in ingestion_matrix()]}
    validate_manifest(manifest)
    for change in ({"scenarios": manifest["scenarios"][:2]}, {"memory_budget_bytes": 0},
                   {"timeout_seconds": 3601}, {"candidate_revision": "HEAD"},
                   {"scenarios": manifest["scenarios"] + [manifest["scenarios"][0]]}):
        with pytest.raises(ValueError):
            validate_manifest(dict(manifest, **change))
    # This selection covers every dimension and shape, but each shape has a
    # different workload: no meaningful recipe comparison can be made.
    shapes = ("unwind", "statements", "executemany", "chunks", "transactions", "unwind")
    loads = ((1, 64, 1), (32, 64, 1), (128, 4096, 1),
             (512, 4096, 1), (32, 64, 2), (32, 64, 4))
    unpaired = [next(s.name for s in ingestion_matrix() if
                     (s.write_rows, s.payload_bytes, s.writers, s.write_shape) == (*load, shape))
                for load, shape in zip(loads, shapes)]
    with pytest.raises(ValueError, match="paired write shapes"):
        validate_manifest(dict(manifest, scenarios=unpaired))
    # A bounded subset can still cover every dimension while comparing at
    # least two shapes under identical records/payload/writer count.
    paired = [s.name for s in ingestion_matrix() if
              (s.write_rows, s.payload_bytes, s.writers) == (32, 64, 1) or
              s.write_shape in ("unwind", "executemany")]
    validate_manifest(dict(manifest, scenarios=paired))


def test_missing_manifest_fails_before_launch(tmp_path, monkeypatch):
    from bench.harness import ingestion_qualification
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        ingestion_qualification.run(Path(__file__).resolve().parents[2])


def test_exit_two_requires_complete_recomputed_inconclusive(monkeypatch, tmp_path):
    from bench.harness import ingestion_qualification as q
    monkeypatch.setattr(q, "inspect_result", lambda *_: {"decision": "inconclusive"})
    q.phase_result(2, 1, tmp_path, {})
    for code, index in ((1, 1), (2, 0), (3, 1)):
        with pytest.raises(RuntimeError):
            q.phase_result(code, index, tmp_path, {})
    monkeypatch.setattr(q, "inspect_result", lambda *_: {"decision": "failed"})
    with pytest.raises(RuntimeError):
        q.phase_result(2, 1, tmp_path, {})
    def incomplete(*_):
        raise ValueError("incomplete raw")
    monkeypatch.setattr(q, "inspect_result", incomplete)
    with pytest.raises(ValueError, match="incomplete"):
        q.phase_result(2, 1, tmp_path, {})


def test_repeat_binds_all_raw_and_rejects_third_attempt(tmp_path, monkeypatch):
    import hashlib
    from bench.harness import ingestion_qualification as q
    from bench.harness.campaign_fixtures import ingestion_matrix
    previous = tmp_path / "runs/first"
    (previous / "campaign").mkdir(parents=True)
    raw = b'{"synthetic_unit_test_only":true}'
    (previous / "campaign/campaign.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    old = {"schema_version": "grafx-rf09-runner/v1", "candidate_revision": "b" * 40,
           "baseline_revision": "a" * 40, "candidate_root": "candidate", "baseline_root": "baseline", "python": "python",
           "memory_budget_bytes": 1048576, "timeout_seconds": 3500,
           "scenarios": [s.name for s in ingestion_matrix()]}
    (previous / "operational-manifest.json").write_text(json.dumps(old), encoding="utf-8")
    (previous / "runner-receipt.json").write_text(json.dumps({"decision": "inconclusive",
            "artifact_sha256": {"campaign/campaign.json": digest}}), encoding="utf-8")
    manifest = dict(old, attempt=2, previous_output=str(previous), previous_campaign_sha256=digest)
    monkeypatch.setattr(q, "inspect_result", lambda *_: {"decision": "inconclusive"})
    q.validate_manifest(manifest)
    assert q.verify_previous(tmp_path, manifest) == previous / "campaign/campaign.json"
    with pytest.raises(ValueError, match="at most"):
        q.validate_manifest(dict(manifest, attempt=3))
    with pytest.raises(ValueError, match="changed scenarios"):
        q.verify_previous(tmp_path, dict(manifest, scenarios=list(reversed(old["scenarios"]))))
    (previous / "campaign/campaign.json").write_bytes(raw + b' ')
    with pytest.raises(ValueError, match="artifact changed"):
        q.verify_previous(tmp_path, manifest)


def test_supervisor_failure_preserves_failed_audit_without_qualification(tmp_path, monkeypatch):
    from bench.harness import campaign, ingestion_qualification
    from bench.harness.campaign_fixtures import ingestion_matrix
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    base = tmp_path / "Okto/Grafx"
    base.mkdir(parents=True)
    manifest = {"schema_version": "grafx-rf09-runner/v1", "candidate_revision": "b" * 40,
                "baseline_revision": "a" * 40, "memory_budget_bytes": 1048576,
                "timeout_seconds": 3500, "scenarios": [s.name for s in ingestion_matrix()]}
    (base / "rf09-runner.json").write_text(json.dumps(manifest), encoding="utf-8")
    def refused_child(command, log, timeout):
        assert timeout == 3500
        assert "ingestion_qualification" in command[5]
        log.write("partial child output retained")
        return 1
    monkeypatch.setattr(campaign, "execute_worker", refused_child)
    with pytest.raises(RuntimeError, match="child failed"):
        ingestion_qualification.run(Path(__file__).resolve().parents[2])
    audit, = (base / "runs").iterdir()
    assert json.loads((audit / "supervisor-receipt.json").read_text())["decision"] == "failed"
    assert (audit / "supervisor.log").read_text() == "partial child output retained"
    assert not (audit / "result.json").exists()


class RF09CampaignRunner(unittest.TestCase):
    """Explicit native runner selector; never an automatic pytest campaign."""
    __test__ = False

    def test_ingestion_qualification(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from bench.harness.ingestion_qualification import run
        self.assertEqual(run(Path(__file__).resolve().parents[2])["decision"], "tradeoff_reported")
