"""The vector stage is additive, ordered and fail-safe in the calibrate CLI (C13 v4 §3).

What these tests pin:

* without ``--vector-recall`` the harness writes exactly what it always wrote — no section,
  no gauge, byte-compatible behavior;
* with the flag, the calibration document gains its ``vector_recall`` section FIRST and the
  metrics document gains the ``oktografx_vector_recall_ratio`` gauge LAST, in the shape the
  gate parses;
* the three failure windows: a recall failure before any append leaves both documents
  untouched and exits non-zero; a crash between the section append and the gauge append
  leaves section-without-gauge (which a ``--require-recall`` gate reads as UNMEASURED and
  fails) — the reverse state, a gauge without its section, is unreachable; a torn gauge
  replace also exits non-zero with the legacy content intact;
* every append is a read-modify-write through a temporary file and ``os.replace``, so legacy
  keys survive byte-for-byte in all outcomes.

The CLI-level tests stub the D5 measurement itself (``calibrate``/``publish``): this file
audits the WIRING, not the ceilings — those have their own suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bench.harness.calibrate as calibrate_module
import bench.harness.recall_wiring as wiring
from bench.harness.recall import RECALL_METRIC, RecallStageError
from bench.harness.recall_wiring import append_vector_recall


def _seed_documents(tmp_path: Path) -> tuple[Path, Path]:
    """Write minimal legacy calibration and metrics documents, as the harness would."""
    out = tmp_path / "calibration.json"
    out.write_text(
        json.dumps(
            {"ceilings": [{"name": "durable_commit"}], "partitions_per_table": 64}
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "name": "oktografx_baseline_ceiling_multiple",
                        "samples": [
                            {"value": 1.0, "labels": {"ceiling": "durable_commit"}}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return out, metrics


def _verdict_stub() -> dict[str, object]:
    """A successful worker verdict, minimal but shape-complete for build_section."""
    return {
        "ok": True,
        "failure": "",
        "profile": "tiny",
        "generator": "uniform-int53-v1",
        "oracle": "pure-fsum",
        "gt_path_used": "pure",
        "numpy": "absent",
        "k": 4,
        "queries": 8,
        "corpus_size": 96,
        "dimension": 16,
        "hashes": {
            "corpus_sha256_f64": "a" * 64,
            "corpus_sha256_f32": "b" * 64,
            "query_sha256_f64": "c" * 64,
            "query_sha256_f32": "d" * 64,
        },
        "gauge": 0.975,
        "observed": {
            "mean_recall_at_k": 0.975,
            "min_recall_at_k": 0.9,
            "queries_below_target": 0,
            "dtype_check": {"mean_overlap": 1.0, "min_overlap": 1.0},
        },
        "blas_environment": {},
        "hnsw": {},
        "duration_seconds": 0.5,
        "exit_code": 0,
    }


def test_success_appends_section_first_and_gauge_last_in_the_gate_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both appends land; legacy keys survive; the gauge entry is exactly what the gate reads."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 0
    calibration = json.loads(out.read_text(encoding="utf-8"))
    assert calibration["ceilings"] == [{"name": "durable_commit"}]
    assert (
        calibration["vector_recall"]["frozen"]["corpus"]["generator"]
        == "uniform-int53-v1"
    )
    document = json.loads(metrics.read_text(encoding="utf-8"))
    names = [entry["name"] for entry in document["metrics"]]
    assert names == ["oktografx_baseline_ceiling_multiple", RECALL_METRIC]
    gauge = document["metrics"][-1]
    assert gauge["samples"] == [{"value": 0.975}]


def test_a_recall_failure_before_any_append_touches_neither_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Window 1: the worker refused — exit non-zero, both documents byte-identical."""
    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise RecallStageError("synthetic fail-closed")

    monkeypatch.setattr(wiring, "run_recall", refuse)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert (
        out.read_text(encoding="utf-8"),
        metrics.read_text(encoding="utf-8"),
    ) == before


def test_a_crash_between_section_and_gauge_leaves_section_without_gauge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Window 2: gauge-orphan is impossible; the reachable partial state is UNMEASURED."""
    out, metrics = _seed_documents(tmp_path)
    metrics_before = metrics.read_text(encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())

    def torn(path: Path, value: float) -> None:
        raise OSError("synthetic torn replace")

    monkeypatch.setattr(wiring, "_append_gauge", torn)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    calibration = json.loads(out.read_text(encoding="utf-8"))
    assert "vector_recall" in calibration, "the section append already landed"
    assert metrics.read_text(encoding="utf-8") == metrics_before, "no gauge was written"
    document = json.loads(metrics_before)
    assert all(entry["name"] != RECALL_METRIC for entry in document["metrics"])


def test_missing_documents_are_skipped_without_inventing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None paths mean the caller asked for no document; the stage still succeeds."""
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=None, metrics=None, workspace=tmp_path
    )
    assert code == 0
    assert list(tmp_path.glob("*.json")) == []


def _stub_result() -> object:
    """The minimal CalibrationResult stand-in the CLI tail consumes."""

    class Result:
        exit_code = 0

        def to_dict(self) -> dict[str, object]:
            return {"ceilings": [], "partitions_per_table": 64}

    return Result()


def test_the_cli_without_the_flag_writes_no_vector_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-flag pin: byte-compatible legacy behavior, no section, no gauge."""
    out = tmp_path / "calibration.json"
    metrics = tmp_path / "metrics.json"
    monkeypatch.setattr(calibrate_module, "calibrate", lambda **kw: _stub_result())
    monkeypatch.setattr(calibrate_module, "format_result", lambda result: "stubbed")
    monkeypatch.setattr(
        calibrate_module,
        "publish",
        lambda result, path: path.write_text(
            json.dumps({"metrics": []}), encoding="utf-8"
        ),
    )
    code = calibrate_module.main(
        ["--out", str(out), "--metrics", str(metrics), "--workspace", str(tmp_path)]
    )
    assert code == 0
    assert "vector_recall" not in json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"metrics": []}


def test_the_cli_with_the_flag_runs_the_stage_after_the_legacy_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flags flow end to end: profile and gt mode reach the stage; appends land in order."""
    out = tmp_path / "calibration.json"
    metrics = tmp_path / "metrics.json"
    seen: dict[str, object] = {}
    monkeypatch.setattr(calibrate_module, "calibrate", lambda **kw: _stub_result())
    monkeypatch.setattr(calibrate_module, "format_result", lambda result: "stubbed")
    monkeypatch.setattr(
        calibrate_module,
        "publish",
        lambda result, path: path.write_text(
            json.dumps({"metrics": []}), encoding="utf-8"
        ),
    )

    def stage(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr("bench.harness.recall_wiring.append_vector_recall", stage)
    code = calibrate_module.main(
        [
            "--out",
            str(out),
            "--metrics",
            str(metrics),
            "--workspace",
            str(tmp_path),
            "--vector-recall",
            "--recall-profile",
            "tiny",
            "--recall-gt",
            "pure",
        ]
    )
    assert code == 0
    assert seen["profile"] == "tiny"
    assert seen["gt_mode"] == "pure"
    assert seen["out"] == out
    assert seen["metrics"] == metrics
    assert out.exists(), "the legacy calibration write happened before the stage"


def test_a_failing_stage_fails_the_cli_with_legacy_outputs_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI contract: harness exit != 0 stops the job before the gate ever runs."""
    out = tmp_path / "calibration.json"
    metrics = tmp_path / "metrics.json"
    monkeypatch.setattr(calibrate_module, "calibrate", lambda **kw: _stub_result())
    monkeypatch.setattr(calibrate_module, "format_result", lambda result: "stubbed")
    monkeypatch.setattr(
        calibrate_module,
        "publish",
        lambda result, path: path.write_text(
            json.dumps({"metrics": []}), encoding="utf-8"
        ),
    )
    monkeypatch.setattr(
        "bench.harness.recall_wiring.append_vector_recall", lambda **kw: 3
    )
    code = calibrate_module.main(
        [
            "--out",
            str(out),
            "--metrics",
            str(metrics),
            "--workspace",
            str(tmp_path),
            "--vector-recall",
        ]
    )
    assert code == 3
    assert out.exists() and metrics.exists(), (
        "legacy outputs survive the vector failure"
    )
