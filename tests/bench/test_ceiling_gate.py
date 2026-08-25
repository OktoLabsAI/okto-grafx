"""The D5 ceiling gate and the measurement primitives behind it (SPEC-M1 FR-15, AC-14, TS-14).

TS-14 asks for the gate to be proved able to fail "by forcing an artificial overflow", so that is
what happens here: a published metrics document is written with a durable-commit multiple above
10x and the real command line is run on it, asserting the exit code and the 'consult JP' state
FR-15 requires. The green direction is asserted with the same machinery, so neither result is a
property of the harness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from bench.harness.calibrate import (
    CEILINGS,
    METRIC_NAME,
    STATUS_CONSULT_JP,
    STATUS_EXCEEDED,
    STATUS_MET,
    STATUS_UNMEASURED,
    CalibrationResult,
    publish,
)
from bench.harness.gate import RECALL_METRIC, check
from bench.harness.measure import Measurement, from_samples, measure, ratio

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, from which the gate is run exactly as CI runs it."""

CALIBRATION_FILE: Path = PROJECT_ROOT / "bench" / "calibration.json"
"""The frozen calibration artefact this component commits."""


def _document(
    multiples: dict[str, float], gauges: dict[str, float] | None = None
) -> str:
    """Return a published metrics document holding these multiples."""
    metrics: list[dict[str, object]] = [
        {
            "name": METRIC_NAME,
            "kind": "gauge",
            "unit": "multiple",
            "description": "Measured multiple of the calibrated baseline ceiling, by ceiling.",
            "samples": [
                {"labels": {"ceiling": ceiling}, "value": value}
                for ceiling, value in sorted(multiples.items())
            ],
        }
    ]
    for name, value in sorted((gauges or {}).items()):
        if name == RECALL_METRIC:
            # The strict C13 matcher demands the DECLARED publication shape for the
            # recall gauge -- exactly {name, kind, unit, samples} with one sample of
            # exactly {"value"} -- which is what the wiring actually publishes. The
            # legacy extras (a description on the entry, labels inside the sample)
            # described a shape the pipeline never emits.
            metrics.append(
                {
                    "name": name,
                    "kind": "gauge",
                    "unit": "ratio",
                    "samples": [{"value": value}],
                }
            )
            continue
        metrics.append(
            {
                "name": name,
                "kind": "gauge",
                "unit": "ratio",
                "description": "A gauge.",
                "samples": [{"labels": {}, "value": value}],
            }
        )
    return json.dumps(
        {"format": "okto-grafx-metrics", "version": 1, "metrics": metrics}
    )


def _run_gate(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the real gate command line, resolving imports from this tree only (A94)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT), str(PROJECT_ROOT / "src")]
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "bench.harness.gate", *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        cwd=str(PROJECT_ROOT),
    )


# --- the gate, proved able to fail on a forced overflow (TS-14) --------------------------------


def test_a_forced_overflow_of_the_commit_ceiling_stops_the_pipeline(
    tmp_path: Path,
) -> None:
    """FR-15: above 10x on durable commit the gate does not merely fail, it says consult JP."""
    document = tmp_path / "metrics.json"
    document.write_text(
        _document({"durable_commit": 12.5, "point_read": 1.0, "open_replay": 1.0}),
        encoding="utf-8",
    )
    result = _run_gate(["--metrics", str(document)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CONSULT_JP" in result.stdout
    assert "12.50x of 10x" in result.stdout
    assert "changes D1" in result.stdout or "change D1" in result.stdout


def test_the_same_gate_is_green_when_every_ceiling_is_met(tmp_path: Path) -> None:
    """The control: the red above came from the numbers, not from the gate."""
    document = tmp_path / "metrics.json"
    document.write_text(
        _document({"durable_commit": 9.9, "point_read": 0.02, "open_replay": 2.9}),
        encoding="utf-8",
    )
    result = _run_gate(["--metrics", str(document)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CEILINGS_MET" in result.stdout


def test_a_ceiling_with_no_published_sample_is_unmeasured(tmp_path: Path) -> None:
    """A75.2: a ceiling nobody measured is not a ceiling that was met."""
    document = tmp_path / "metrics.json"
    document.write_text(_document({"durable_commit": 1.0}), encoding="utf-8")
    result = _run_gate(["--metrics", str(document)])
    assert result.returncode == 2, result.stdout
    assert "UNMEASURED" in result.stdout
    assert "point_read" in result.stdout


def test_a_metrics_file_that_is_absent_is_unmeasured(tmp_path: Path) -> None:
    """The gate reads a file; a missing file is a failure to measure, not a pass."""
    result = _run_gate(["--metrics", str(tmp_path / "nothing.json")])
    assert result.returncode == 2, result.stdout
    assert "Traceback" not in result.stderr


def test_an_unreadable_metrics_document_is_unmeasured() -> None:
    """A truncated document must never read as an empty set of violations."""
    result = check("{not json")
    assert result.status == STATUS_UNMEASURED
    assert result.exit_code == 2


@pytest.mark.parametrize("document", ["[]", "null", "3", '"metrics"'])
def test_valid_json_that_is_not_an_object_is_unmeasured_and_not_a_ceiling_failure(
    tmp_path: Path, document: str
) -> None:
    """Exit 2, never exit 1: a document that could not be read is not a build that regressed.

    ``json.loads`` accepts all of these, so only the SHAPE refuses them, and the refusal has to
    happen before ``payload.get``. Without it the gate dies with an ``AttributeError`` -- the
    docstring of ``read_multiples`` says it never raises -- and an unhandled exception leaves the
    interpreter with status 1, which is this gate's code for CEILING EXCEEDED. Read through the
    REAL command line, because the exit code is the whole claim: a caller that sees 1 believes a
    ceiling was measured and missed, which is the A75.2 conflation this module exists to forbid.
    """
    metrics = tmp_path / "metrics.json"
    metrics.write_text(document, encoding="utf-8")
    result = _run_gate(["--metrics", str(metrics)])
    assert result.returncode == 2, (
        f"a metrics document of {document!r} was reported with exit {result.returncode}; "
        f"1 means a ceiling was exceeded and this one was never measured.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert "UNMEASURED" in result.stdout, result.stdout
    # And the same refusal in process, so the reason travels rather than only the exit code.
    verdict = check(document)
    assert verdict.status == STATUS_UNMEASURED
    assert verdict.lines and "not an object" in verdict.lines[0]


def test_a_ceiling_exactly_at_its_limit_is_met() -> None:
    """The boundary is inclusive, and it is pinned so nobody has to guess."""
    document = _document(
        {"durable_commit": 10.0, "point_read": 5.0, "open_replay": 3.0}
    )
    assert check(document).status == STATUS_MET


def test_a_point_read_over_its_ceiling_fails_without_stopping_the_pipeline() -> None:
    """Only durable commit carries the 'consult JP' consequence (FR-15)."""
    document = _document({"durable_commit": 1.0, "point_read": 5.1, "open_replay": 1.0})
    result = check(document)
    assert result.status == STATUS_EXCEEDED
    assert result.exit_code == 1


def test_a_recall_below_the_frozen_target_fails_the_gate() -> None:
    """SPEC-VEC FR-8/D8d: recall is a floor, not a ceiling, and the gate reads it as one."""
    document = _document(
        {"durable_commit": 1.0, "point_read": 1.0, "open_replay": 1.0},
        {RECALL_METRIC: 0.80},
    )
    result = check(document, recall_target=0.90)
    assert result.status == STATUS_EXCEEDED
    assert any("BELOW" in line for line in result.lines)


def test_a_recall_at_the_target_is_met() -> None:
    """The other direction of the same rule."""
    document = _document(
        {"durable_commit": 1.0, "point_read": 1.0, "open_replay": 1.0},
        {RECALL_METRIC: 0.90},
    )
    assert check(document, recall_target=0.90).status == STATUS_MET


def test_an_absent_recall_is_reported_and_can_be_demanded() -> None:
    """Until the vector wave publishes, the gate says so; a flag makes it blocking."""
    document = _document({"durable_commit": 1.0, "point_read": 1.0, "open_replay": 1.0})
    assert check(document).status == STATUS_MET
    assert check(document, require_recall=True).status == STATUS_UNMEASURED


def test_the_ceilings_are_the_three_of_d5() -> None:
    """A56: widening this mapping switches a ceiling off in one token, so it is pinned."""
    assert CEILINGS == {"durable_commit": 10.0, "point_read": 5.0, "open_replay": 3.0}


# --- publication goes through the real sink and the frozen catalog -----------------------------


def test_a_published_multiple_carries_the_frozen_name_and_label(tmp_path: Path) -> None:
    """OR-4: the gate reads this metric, so publication must produce exactly it."""
    result = CalibrationResult(
        status=STATUS_MET,
        ratios=(
            ratio(
                "durable_commit",
                10.0,
                from_samples("s", [0.010]),
                from_samples("b", [0.002]),
            ),
            ratio(
                "point_read",
                5.0,
                from_samples("s", [0.001]),
                from_samples("b", [0.002]),
            ),
            ratio(
                "open_replay",
                3.0,
                from_samples("s", [0.100]),
                from_samples("b", [0.050]),
            ),
        ),
        partitions_per_table=64,
        environment={},
    )
    document = publish(result, tmp_path / "metrics.json")
    payload = json.loads(document)
    names = {entry["name"] for entry in payload["metrics"]}
    assert METRIC_NAME in names
    gate = check(document)
    assert gate.status == STATUS_MET
    assert (tmp_path / "metrics.json").is_file()


def test_the_catalog_refuses_a_ceiling_label_the_contract_does_not_allow(
    tmp_path: Path,
) -> None:
    """G7: the label domain is bounded at registration, so an invented ceiling cannot publish."""
    from okto_grafx.adapters.metrics_json import JsonMetricsSink
    from okto_grafx.domain.errors import GrafxError
    from okto_grafx.engine.metrics_catalog import metric

    written: list[str] = []
    sink = JsonMetricsSink(written.append)
    sink.register(metric(METRIC_NAME))
    with pytest.raises(GrafxError):
        sink.set_gauge(METRIC_NAME, 1.0, {"ceiling": "an_invented_ceiling"})


# --- the measurement primitives ----------------------------------------------------------------


def test_warm_up_is_discarded_and_kept() -> None:
    """The discard must be visible, so a reader can judge it rather than trust it."""
    seen: list[int] = []

    def operation(index: int) -> None:
        seen.append(index)

    result = measure(operation, name="counting", iterations=4, warmup=2)
    assert result.ok
    assert result.iterations == 4
    assert len(result.warmup) == 2
    assert seen == [0, 1, 2, 3, 4, 5], "the operation is told which iteration it is on"


def test_an_operation_that_raises_is_unmeasured_and_not_a_fast_result() -> None:
    """A75.2 at the level of one measurement: a partial sample list must not be reported."""

    def operation(index: int) -> None:
        if index == 3:
            raise RuntimeError("planted")

    result = measure(operation, name="failing", iterations=5, warmup=1)
    assert not result.ok
    assert result.samples == ()
    assert "planted" in result.unmeasured


def test_the_statistics_are_the_ones_the_report_claims() -> None:
    """Median, p95 by nearest rank, spread: computed here so a reader can check them by hand."""
    result = from_samples("fixed", [0.001, 0.002, 0.003, 0.004, 0.005])
    assert result.median == pytest.approx(0.003)
    assert result.minimum == pytest.approx(0.001)
    assert result.maximum == pytest.approx(0.005)
    assert result.p95 == pytest.approx(0.005)
    assert result.relative_spread > 0


def test_a_ratio_with_an_unmeasured_side_reports_no_multiple() -> None:
    """A missing baseline must not become a ceiling that was met."""
    unmeasured = Measurement(
        name="b", samples=(), warmup=(), unmeasured="the engine is absent"
    )
    item = ratio("durable_commit", 10.0, from_samples("s", [0.01]), unmeasured)
    assert not item.ok
    assert not item.met
    assert "absent" in item.unmeasured
    assert "unmeasured" in item.to_dict()


def test_a_ratio_reports_the_median_the_tail_and_the_best_case() -> None:
    """A single number with no spread is not a measurement."""
    subject = from_samples("s", [0.010, 0.011, 0.012])
    baseline = from_samples("b", [0.001, 0.002, 0.003])
    item = ratio("durable_commit", 10.0, subject, baseline)
    assert item.multiple == pytest.approx(0.011 / 0.002)
    assert item.multiple_best == pytest.approx(0.010 / 0.001)
    assert item.multiple_p95 > 0


# --- the committed calibration artefact ---------------------------------------------------------


def test_the_frozen_calibration_artefact_is_readable_and_says_what_it_froze() -> None:
    """FR-15 freezes partitions_per_table with a recorded value; this is that record."""
    payload = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    assert payload["format"] == "okto-grafx-calibration"
    assert payload["partitions_per_table"] == 64
    assert payload["status"] in {
        STATUS_MET,
        STATUS_EXCEEDED,
        STATUS_CONSULT_JP,
        STATUS_UNMEASURED,
    }
    ceilings = {entry["ceiling"] for entry in payload["ceilings"]}
    assert ceilings == set(CEILINGS)
    for entry in payload["ceilings"]:
        if "multiple" in entry:
            # Every reported multiple must carry the samples it was computed from.
            assert entry["subject"]["samples_seconds"]
            assert entry["baseline"]["samples_seconds"]


def test_the_frozen_default_matches_the_one_the_harness_builds_with() -> None:
    """The calibrated number and the number the bench stack uses must not drift apart."""
    from bench.harness.grafx_ops import DEFAULT_PARTITIONS_PER_TABLE

    payload = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    assert payload["partitions_per_table"] == DEFAULT_PARTITIONS_PER_TABLE
