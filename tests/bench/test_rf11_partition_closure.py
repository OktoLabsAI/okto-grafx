"""A partial or substituted RF11 partition cannot produce a global decision."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bench.harness import rf11_partition_closure as closure
from bench.harness.rf1112_operational import PARTITIONS, SCHEMA as RUNNER_SCHEMA


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _outputs(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for partition in closure.PARTITION_NAMES:
        output = tmp_path / "runs" / partition
        manifest = {
            "schema_version": RUNNER_SCHEMA, "partition": partition,
            "runner_root": "runner", "runner_revision": "1" * 40,
            "baseline_root": "baseline", "baseline_revision": "2" * 40,
            "candidate_root": "candidate", "candidate_revision": "3" * 40,
            "python": "python", "output": str(output), "timeout_seconds": 3580,
            "scenarios": list(PARTITIONS[partition]), "attempt": 1,
            "previous_output": None, "previous_campaign_sha256": None,
        }
        _write(output / "operational-manifest.json", manifest)
        _write(output / "campaign" / "campaign.json", {"partition": partition})
        _write(output / "campaign" / "report.json", {"decision": "tradeoff_reported"})
        artifacts = {
            p.relative_to(output).as_posix(): _sha(p.read_bytes())
            for p in output.rglob("*") if p.is_file()
        }
        _write(output / "runner-receipt.json", {
            "partition": partition,
            "manifest_sha256": artifacts["operational-manifest.json"],
            "decision": "tradeoff_reported", "artifact_sha256": artifacts,
        })
        result[partition] = output
    return result


def test_closure_requires_five_matching_receipts_and_25_unique_scenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(closure, "inspect_result", lambda output, manifest: {
        "decision": "tradeoff_reported"})
    outputs = _outputs(tmp_path)
    report = closure.close(outputs)
    assert report["scenario_count"] == 25
    assert [item["partition"] for item in report["partitions"]] == list(closure.PARTITION_NAMES)
    assert report["source"]["candidate_revision"] == "3" * 40

    with pytest.raises(ValueError, match="P1-P5"):
        closure.close({name: path for name, path in outputs.items() if name != "P4"})
    (outputs["P4"] / "campaign" / "campaign.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash"):
        closure.close(outputs)


def test_closure_rejects_inconclusive_and_mixed_source(tmp_path, monkeypatch):
    outputs = _outputs(tmp_path)
    monkeypatch.setattr(closure, "inspect_result", lambda output, manifest: {
        "decision": "inconclusive" if manifest["partition"] == "P4" else "tradeoff_reported"})
    with pytest.raises(ValueError, match="incomplete or inconclusive"):
        closure.close(outputs)

    monkeypatch.setattr(closure, "inspect_result", lambda output, manifest: {
        "decision": "tradeoff_reported"})
    p4 = outputs["P4"]
    manifest_path = p4 / "operational-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_revision"] = "4" * 40
    _write(manifest_path, manifest)
    receipt_path = p4 / "runner-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    digest = _sha(manifest_path.read_bytes())
    receipt["manifest_sha256"] = digest
    receipt["artifact_sha256"]["operational-manifest.json"] = digest
    _write(receipt_path, receipt)
    with pytest.raises(ValueError, match="different sources"):
        closure.close(outputs)


def test_cli_refuses_to_write_into_sealed_partition(tmp_path):
    outputs = _outputs(tmp_path)
    args = [part for name, path in outputs.items()
            for part in ("--" + name.lower(), str(path))]
    args += ["--output", str(outputs["P1"] / "closure.json")]
    with pytest.raises(ValueError, match="sealed artifacts"):
        closure.main(args)
    assert not (outputs["P1"] / "closure.json").exists()
