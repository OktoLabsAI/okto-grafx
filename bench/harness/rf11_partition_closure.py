"""Close RF11 only after all five authenticated temporal partitions agree.

This consumes immutable runner outputs; it never starts a benchmark or changes
the qualified per-partition reports. An incomplete or inconclusive partition
cannot be converted into a successful overall decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from bench.harness.campaign_schema import load_json
from bench.harness.rf1112_operational import (
    PARTITIONS, _previous, inspect_result, validate_manifest,
)


PARTITION_NAMES = ("P1", "P2", "P3", "P4", "P5")
SCHEMA = "grafx-rf11-partition-closure/v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(output: Path, expected: dict[str, str]) -> None:
    if not isinstance(expected, dict) or not expected:
        raise ValueError("partition artifact inventory missing")
    observed = {p.relative_to(output).as_posix() for p in output.rglob("*")
                if p.is_file() and p.relative_to(output).as_posix() != "runner-receipt.json"}
    if set(expected) != observed:
        raise ValueError("partition artifact inventory differs from disk")
    for name, digest in expected.items():
        path = (output / name).resolve(strict=True)
        path.relative_to(output)
        if (not isinstance(digest, str) or len(digest) != 64
                or _sha256(path) != digest):
            raise ValueError("partition artifact hash differs: " + name)


def close(partition_outputs: dict[str, Path]) -> dict:
    """Verify five selected attempts and return a bounded cross-partition report."""
    if set(partition_outputs) != set(PARTITION_NAMES):
        raise ValueError("RF11 closure requires P1-P5 exactly once")
    shared_identity = None
    used_outputs: set[Path] = set()
    sections = []
    for partition in PARTITION_NAMES:
        output = Path(partition_outputs[partition]).resolve(strict=True)
        if output in used_outputs or not output.is_dir():
            raise ValueError("partition output must be a distinct directory")
        used_outputs.add(output)
        manifest_path = output / "operational-manifest.json"
        manifest = load_json(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(manifest, partition)
        if Path(manifest["output"]).resolve() != output:
            raise ValueError("manifest output differs from selected partition")
        receipt = load_json((output / "runner-receipt.json").read_text(encoding="utf-8"))
        if (receipt.get("partition") != partition
                or receipt.get("manifest_sha256") != _sha256(manifest_path)
                or receipt.get("decision") != "tradeoff_reported"):
            raise ValueError("partition lacks a successful matching runner receipt")
        _artifacts(output, receipt.get("artifact_sha256"))
        if manifest["attempt"] == 2:
            _previous(output.parent.parent, manifest)
        report_path = output / "campaign" / "report.json"
        report = inspect_result(output / "campaign", manifest)
        if report.get("decision") != "tradeoff_reported":
            raise ValueError("partition comparison is incomplete or inconclusive")
        identity = tuple(manifest[key] for key in (
            "runner_root", "runner_revision", "baseline_root", "baseline_revision",
            "candidate_root", "candidate_revision", "python"))
        if shared_identity is None:
            shared_identity = identity
        elif identity != shared_identity:
            raise ValueError("RF11 partitions used different sources or interpreter")
        sections.append({
            "partition": partition,
            "attempt": manifest["attempt"],
            "scenario_names": list(PARTITIONS[partition]),
            "output": str(output),
            "manifest_sha256": _sha256(manifest_path),
            "campaign_sha256": _sha256(output / "campaign" / "campaign.json"),
            "report_sha256": _sha256(report_path),
            "decision": report["decision"],
        })
    names = [name for section in sections for name in section["scenario_names"]]
    if len(names) != 25 or len(set(names)) != 25:
        raise ValueError("RF11 closure requires 25 distinct canonical scenarios")
    return {
        "schema_version": SCHEMA,
        "decision": "tradeoff_reported",
        "qualification_scope": "all_five_partitions_only",
        "scenario_count": len(names),
        "source": dict(zip(("runner_root", "runner_revision", "baseline_root",
                            "baseline_revision", "candidate_root", "candidate_revision",
                            "python"), shared_identity)),
        "partitions": sections,
        "limits": [
            "Each partition report retains its own raw samples and tradeoff decision.",
            "This closure does not establish a universal speedup or zero overhead.",
            "Warm/reopen does not control the operating-system file cache.",
            "Changing the engine, defaults, or format requires a separate specification.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for partition in PARTITION_NAMES:
        parser.add_argument("--" + partition.lower(), type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = {p: getattr(args, p.lower()) for p in PARTITION_NAMES}
    destination = args.output.resolve()
    for path in inputs.values():
        if destination.is_relative_to(path.resolve()):
            raise ValueError("closure output must not alter a partition's sealed artifacts")
    report = close(inputs)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
