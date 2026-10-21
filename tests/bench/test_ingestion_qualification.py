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


def test_missing_manifest_fails_before_launch(tmp_path, monkeypatch):
    from bench.harness import ingestion_qualification
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        ingestion_qualification.run(Path(__file__).resolve().parents[2])


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
