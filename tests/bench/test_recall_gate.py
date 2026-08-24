"""The gate's recall floor has one precedence and one voice: flag > artifact > built-in (C13).

What these tests pin:

* an omitted ``--recall-target`` with a ``--calibration`` file takes the FROZEN target — the
  old masking default (flag default == built-in value) is gone, so the artifact actually
  governs;
* an explicit flag beats the artifact; nothing at all means the built-in default; every
  resolution prints its ORIGIN;
* an EXPLICITLY NAMED calibration file that is unreadable or holds no usable target
  REFUSES (UNMEASURED, exit 2) — the built-in default applies only when no source was given;
* the ROUND7 §3b anti-disconnection regression: a published gauge below the frozen target
  makes the gate exit non-zero — the knob is verifiably connected;
* the legacy exit codes survive: met is 0, EXCEEDED is 1, UNMEASURED (a required gauge
  absent included) is 2, an unreadable metrics document is 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.harness.gate import DEFAULT_RECALL_TARGET, _resolve_recall_target, main

RECALL_METRIC = "oktografx_vector_recall_ratio"


def _metrics_document(path: Path, *, recall: float | None) -> Path:
    """Write a minimal published metrics document, optionally carrying the recall gauge."""
    entries: list[dict[str, object]] = [
        {
            "name": "oktografx_baseline_ceiling_multiple",
            "samples": [
                {"value": 1.0, "labels": {"ceiling": "durable_commit"}},
                {"value": 1.0, "labels": {"ceiling": "point_read"}},
                {"value": 1.0, "labels": {"ceiling": "open_replay"}},
            ],
        }
    ]
    if recall is not None:
        entries.append({"name": RECALL_METRIC, "samples": [{"value": recall}]})
    document = path / "metrics.json"
    document.write_text(json.dumps({"metrics": entries}), encoding="utf-8")
    return document


def _calibration_document(path: Path, *, target: float) -> Path:
    """Write a calibration document whose vector_recall section freezes one target."""
    document = path / "calibration.json"
    document.write_text(
        json.dumps({"vector_recall": {"frozen": {"target": target}}}), encoding="utf-8"
    )
    return document


def test_resolution_precedence_is_flag_then_artifact_then_builtin(
    tmp_path: Path,
) -> None:
    """The three sources resolve in order, each naming its origin."""
    calibration = _calibration_document(tmp_path, target=0.95)
    explicit = _resolve_recall_target(0.88, str(calibration))
    assert explicit == (0.88, "explicit flag")
    frozen = _resolve_recall_target(None, str(calibration))
    assert frozen[0] == 0.95
    assert "frozen in" in frozen[1]
    built_in = _resolve_recall_target(None, None)
    assert built_in == (DEFAULT_RECALL_TARGET, "built-in default")


def test_an_explicitly_named_but_unusable_artifact_refuses(
    tmp_path: Path,
) -> None:
    """(b): the caller asked for THAT artifact to govern; a silent 0.90 is a floor nobody
    chose. Unreadable and target-less files raise; only calibration=None means built-in."""
    with pytest.raises(ValueError, match="unreadable"):
        _resolve_recall_target(None, str(tmp_path / "absent.json"))
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no usable frozen"):
        _resolve_recall_target(None, str(hollow))
    assert _resolve_recall_target(None, None) == (
        DEFAULT_RECALL_TARGET,
        "built-in default",
    )


def test_the_frozen_target_governs_the_verdict_anti_disconnection(
    tmp_path: Path,
) -> None:
    """ROUND7 §3b: a gauge below the frozen target exits non-zero; at or above passes."""
    calibration = _calibration_document(tmp_path, target=0.95)
    below = _metrics_document(tmp_path, recall=0.92)
    assert (
        main(
            [
                "--metrics",
                str(below),
                "--calibration",
                str(calibration),
                "--require-recall",
            ]
        )
        == 1
    )
    above = _metrics_document(tmp_path, recall=0.96)
    assert (
        main(
            [
                "--metrics",
                str(above),
                "--calibration",
                str(calibration),
                "--require-recall",
            ]
        )
        == 0
    )


def test_an_explicit_flag_beats_the_artifact_for_the_forced_violation(
    tmp_path: Path,
) -> None:
    """The VTS-10 forced violation uses the flag override, artifact present or not."""
    calibration = _calibration_document(tmp_path, target=0.90)
    document = _metrics_document(tmp_path, recall=0.95)
    assert (
        main(
            [
                "--metrics",
                str(document),
                "--calibration",
                str(calibration),
                "--recall-target",
                "0.999",
                "--require-recall",
            ]
        )
        == 1
    )


def test_legacy_exit_codes_survive(tmp_path: Path) -> None:
    """Met 0; UNMEASURED-with-require 2; unreadable metrics document 2; no-require passes."""
    document = _metrics_document(tmp_path, recall=None)
    assert main(["--metrics", str(document)]) == 0
    assert main(["--metrics", str(document), "--require-recall"]) == 2
    assert main(["--metrics", str(tmp_path / "missing.json")]) == 2


def _document_with_recall_entries(path: Path, entries: list[dict[str, object]]) -> Path:
    """A metrics document with all three ceilings met plus the given raw recall entries."""
    payload: list[dict[str, object]] = [
        {
            "name": "oktografx_baseline_ceiling_multiple",
            "samples": [
                {"value": 1.0, "labels": {"ceiling": "durable_commit"}},
                {"value": 1.0, "labels": {"ceiling": "point_read"}},
                {"value": 1.0, "labels": {"ceiling": "open_replay"}},
            ],
        }
    ]
    payload.extend(entries)
    document = path / "metrics.json"
    document.write_text(json.dumps({"metrics": payload}), encoding="utf-8")
    return document


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "1.5", "-0.5"])
def test_an_invalid_explicit_target_is_refused_as_unmeasured(
    tmp_path: Path, bad: str
) -> None:
    """A --recall-target outside (0,1] or non-finite cannot gate anything: exit 2."""
    document = _metrics_document(tmp_path, recall=0.95)
    # The = form is deliberate: argparse reads a bare "-inf" as an option, which is a
    # usage error (also exit 2) rather than the validation path this test pins.
    assert main(["--metrics", str(document), f"--recall-target={bad}"]) == 2


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -0.5, 1.5, "0.9", None]
)
def test_a_present_but_invalid_gauge_value_is_unmeasured_even_unrequired(
    tmp_path: Path, value: object
) -> None:
    """(c): bool, NaN, Inf, out-of-range and non-numeric are NOT measurements. A corrupt
    publication refuses even without --require-recall -- only ABSENCE is tolerable there."""
    document = _document_with_recall_entries(
        tmp_path, [{"name": RECALL_METRIC, "samples": [{"value": value}]}]
    )
    assert main(["--metrics", str(document)]) == 2


def test_duplicate_gauge_entries_or_samples_are_unmeasured(tmp_path: Path) -> None:
    """(c): exactly ONE measurement; two entries or two samples mean none is THE one."""
    twice = _document_with_recall_entries(
        tmp_path,
        [
            {"name": RECALL_METRIC, "samples": [{"value": 0.99}]},
            {"name": RECALL_METRIC, "samples": [{"value": 0.99}]},
        ],
    )
    assert main(["--metrics", str(twice)]) == 2
    multi = _document_with_recall_entries(
        tmp_path,
        [{"name": RECALL_METRIC, "samples": [{"value": 0.99}, {"value": 0.98}]}],
    )
    assert main(["--metrics", str(multi)]) == 2
    hollow = _document_with_recall_entries(
        tmp_path, [{"name": RECALL_METRIC, "samples": []}]
    )
    assert main(["--metrics", str(hollow)]) == 2


def test_zero_recall_is_a_measurement_and_fails_as_exceeded(tmp_path: Path) -> None:
    """0.0 is a legitimate (terrible) ratio: MEASURED, below every target -- exit 1."""
    document = _metrics_document(tmp_path, recall=0.0)
    assert main(["--metrics", str(document)]) == 1


def test_an_unusable_named_calibration_fails_the_cli_as_unmeasured(
    tmp_path: Path,
) -> None:
    """(b) end to end: a named-but-unusable --calibration exits 2; omitting it gates on
    the built-in default."""
    document = _metrics_document(tmp_path, recall=0.95)
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    assert main(["--metrics", str(document), "--calibration", str(hollow)]) == 2
    absent = tmp_path / "absent.json"
    assert main(["--metrics", str(document), "--calibration", str(absent)]) == 2
    assert main(["--metrics", str(document)]) == 0
