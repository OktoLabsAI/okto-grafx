"""The gate's recall floor has one precedence and one voice: flag > artifact > built-in (C13).

What these tests pin:

* an omitted ``--recall-target`` with a ``--calibration`` file takes the FROZEN target — the
  old masking default (flag default == built-in value) is gone, so the artifact actually
  governs;
* an explicit flag beats the artifact; nothing at all means the built-in default; every
  resolution prints its ORIGIN;
* an unreadable or sectionless calibration file falls through to the built-in default with
  the origin saying so — never a silent guess;
* the ROUND7 §3b anti-disconnection regression: a published gauge below the frozen target
  makes the gate exit non-zero — the knob is verifiably connected;
* the legacy exit codes survive: met is 0, EXCEEDED is 1, UNMEASURED (a required gauge
  absent included) is 2, an unreadable metrics document is 2.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_an_unreadable_or_sectionless_artifact_falls_through_loudly(
    tmp_path: Path,
) -> None:
    """The gate never guesses silently: the origin names the fallback and its reason."""
    missing = _resolve_recall_target(None, str(tmp_path / "absent.json"))
    assert missing[0] == DEFAULT_RECALL_TARGET
    assert "unreadable" in missing[1]
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    sectionless = _resolve_recall_target(None, str(hollow))
    assert sectionless[0] == DEFAULT_RECALL_TARGET
    assert "no usable frozen target" in sectionless[1]
    bad_value = tmp_path / "bad.json"
    bad_value.write_text(
        json.dumps({"vector_recall": {"frozen": {"target": 7.5}}}), encoding="utf-8"
    )
    out_of_range = _resolve_recall_target(None, str(bad_value))
    assert out_of_range[0] == DEFAULT_RECALL_TARGET


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
