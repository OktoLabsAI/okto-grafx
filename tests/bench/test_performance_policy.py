"""Informational timings must never forgive missing evidence or incorrect recall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.harness.gate import check
from tests.bench.test_ceiling_gate import CALIBRATION_FILE, _document, _run_gate


@pytest.mark.parametrize("ceiling", [None, "durable_commit", "point_read", "open_replay"])
def test_informational_timings_preserve_measurements_and_strict_comparison(
    tmp_path: Path, ceiling: str | None,
) -> None:
    """The real CLI reports an overflow without calling it met or changing the input."""
    multiples = {"durable_commit": 1.0, "point_read": 1.0, "open_replay": 1.0}
    if ceiling is not None:
        multiples[ceiling] = 123.5
    path = tmp_path / "metrics.json"
    path.write_text(_document(multiples, {"oktografx_vector_recall_ratio": 0.9}))
    original = path.read_bytes()
    args = ["--metrics", str(path), "--require-recall", "--calibration", str(CALIBRATION_FILE)]
    strict = _run_gate(args)
    assert strict.returncode == (0 if ceiling is None else 1), strict.stdout + strict.stderr
    reported = _run_gate([*args, "--informational-ceilings"])
    assert reported.returncode == 0, reported.stdout + reported.stderr
    if ceiling is None:
        assert "CEILINGS_MET" in reported.stdout
    else:
        assert "INFORMATIONAL_CEILINGS_EXCEEDED" in reported.stdout
        assert f"{ceiling}: 123.50x" in reported.stdout
        assert "EXCEEDED" in reported.stdout and "CEILINGS_MET" not in reported.stdout
        assert "consult JP" not in reported.stdout
    assert path.read_bytes() == original


@pytest.mark.parametrize("fault,expected", [
    ("recall-low", 1), ("recall-missing", 2), ("recall-nan", 2),
    ("recall-duplicate", 2), ("d5-missing", 2), ("d5-nan", 2),
    ("d5-negative", 2), ("d5-duplicate", 2), ("invalid-json", 2),
    ("missing-metrics", 2), ("missing-calibration", 2), ("invalid-floor", 2),
])
def test_informational_timings_still_reject_quality_and_evidence_failures(
    tmp_path: Path, fault: str, expected: int,
) -> None:
    """Every plant accompanies three overflowing timings, so none can hide behind them."""
    payload = json.loads(_document(
        {"durable_commit": 20.0, "point_read": 10.0, "open_replay": 6.0},
        {"oktografx_vector_recall_ratio": 0.9953},
    ))
    metrics = payload["metrics"]
    if fault == "recall-low":
        metrics[1]["samples"][0]["value"] = 0.89
    elif fault == "recall-missing":
        metrics.pop()
    elif fault == "recall-nan":
        metrics[1]["samples"][0]["value"] = float("nan")
    elif fault == "recall-duplicate":
        metrics.append(metrics[1])
    elif fault == "d5-missing":
        metrics[0]["samples"].pop()
    elif fault in ("d5-nan", "d5-negative"):
        metrics[0]["samples"][0]["value"] = float("nan") if fault == "d5-nan" else -1
    elif fault == "d5-duplicate":
        metrics[0]["samples"].append(metrics[0]["samples"][0])
    path = tmp_path / "metrics.json"
    if fault != "missing-metrics":
        path.write_text("{" if fault == "invalid-json" else json.dumps(payload))
    calibration = CALIBRATION_FILE
    if fault in ("missing-calibration", "invalid-floor"):
        calibration = tmp_path / "calibration.json"
        if fault == "invalid-floor":
            calibration.write_text('{"vector_recall":{"frozen":{"target":0}}}')
    result = _run_gate([
        "--metrics", str(path), "--require-recall", "--calibration", str(calibration),
        "--informational-ceilings",
    ])
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("BELOW" if expected == 1 else "UNMEASURED") in result.stdout
    assert "INFORMATIONAL_CEILINGS_EXCEEDED" not in result.stdout
    if fault in ("d5-negative", "d5-duplicate"):
        strict = _run_gate(["--metrics", str(path), "--require-recall"])
        assert strict.returncode == 2 and "UNMEASURED" in strict.stdout


@pytest.mark.parametrize("policy", [None, 1, "true", []])
def test_informational_policy_requires_an_explicit_boolean(policy: object) -> None:
    """An accidental truthy object cannot select a more permissive timing policy."""
    document = _document({"durable_commit": 20.0, "point_read": 1.0, "open_replay": 1.0})
    assert check(document, informational_ceilings=policy).exit_code == 2
