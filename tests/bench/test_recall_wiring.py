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
import os
import sys
from pathlib import Path

import pytest

import bench.harness.calibrate as calibrate_module
import bench.harness.recall_wiring as wiring
from bench.harness.recall import RECALL_METRIC, RecallStageError
from bench.harness.recall_wiring import append_vector_recall
from bench.harness.recall_worker import HNSW_FROZEN, _BLAS_THREAD_VARIABLES


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


def _tiny_hashes() -> dict[str, str]:
    """The REAL recomputed tiny-profile digests -- the validator refuses fakes now."""
    from bench.harness.recall_worker import CORPUS_SEED, PROFILES, QUERY_SEED
    from bench.recall_corpus import (
        generate_vectors,
        sha256_hex,
        vector_bytes_f32,
        vector_bytes_f64,
    )

    profile = PROFILES["tiny"]
    corpus = generate_vectors(CORPUS_SEED, profile.corpus_size, profile.dimension)
    queries = generate_vectors(QUERY_SEED, profile.queries, profile.dimension)
    return {
        "corpus_sha256_f64": sha256_hex(vector_bytes_f64(corpus)),
        "corpus_sha256_f32": sha256_hex(vector_bytes_f32(corpus)),
        "query_sha256_f64": sha256_hex(vector_bytes_f64(queries)),
        "query_sha256_f32": sha256_hex(vector_bytes_f32(queries)),
    }


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
        "hashes": _tiny_hashes(),
        # 31/32: seven perfect tiny queries and one at 3/4 -- ON the recall@4 grid and
        # jointly realizable with min=0.75, below=1 (the validator now proves it).
        "gauge": 0.96875,
        "observed": {
            "mean_recall_at_k": 0.96875,
            "min_recall_at_k": 0.75,
            # One query below perfect: coherent with a minimum under 1.0 -- the full
            # verdict validator refuses a zero count beside an imperfect minimum.
            "queries_below_perfect": 1,
            "dtype_check": {"mean_overlap": 1.0, "min_overlap": 1.0},
        },
        "blas_environment": {name: "1" for name in _BLAS_THREAD_VARIABLES},
        "hnsw": dict(HNSW_FROZEN),
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
    assert gauge["samples"] == [{"value": 0.96875}]


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


def test_a_stale_gauge_from_a_previous_run_never_survives_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: with an OLD gauge already published, a crash between section and gauge leaves
    the gauge ABSENT (UNMEASURED for a require gate) -- never the stale value."""
    out, metrics = _seed_documents(tmp_path)
    document = json.loads(metrics.read_text(encoding="utf-8"))
    document["metrics"].append({"name": RECALL_METRIC, "samples": [{"value": 0.42}]})
    metrics.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())

    def torn(path: Path, value: float) -> None:
        raise OSError("synthetic torn replace")

    monkeypatch.setattr(wiring, "_append_gauge", torn)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    survived = json.loads(metrics.read_text(encoding="utf-8"))
    assert all(entry["name"] != RECALL_METRIC for entry in survived["metrics"]), (
        "the stale gauge must be stripped before anything else"
    )
    assert "vector_recall" in json.loads(out.read_text(encoding="utf-8"))


def test_a_rerun_with_duplicate_old_gauges_ends_with_exactly_one_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: wholesale duplicate removal, then exactly one fresh gauge on success."""
    out, metrics = _seed_documents(tmp_path)
    document = json.loads(metrics.read_text(encoding="utf-8"))
    document["metrics"].append({"name": RECALL_METRIC, "samples": [{"value": 0.1}]})
    document["metrics"].append({"name": RECALL_METRIC, "samples": [{"value": 0.2}]})
    metrics.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 0
    final = json.loads(metrics.read_text(encoding="utf-8"))
    gauges = [entry for entry in final["metrics"] if entry["name"] == RECALL_METRIC]
    assert len(gauges) == 1
    assert gauges[0]["samples"] == [{"value": 0.96875}]


def test_a_crash_after_the_strip_before_the_section_leaves_no_gauge_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 window: the strip landed, the run then failed -- absent gauge, untouched out."""
    out, metrics = _seed_documents(tmp_path)
    document = json.loads(metrics.read_text(encoding="utf-8"))
    document["metrics"].append({"name": RECALL_METRIC, "samples": [{"value": 0.42}]})
    metrics.write_text(json.dumps(document), encoding="utf-8")
    out_before = out.read_text(encoding="utf-8")

    def refuse(*args: object, **kwargs: object) -> dict[str, object]:
        raise RecallStageError("synthetic fail-closed after strip")

    monkeypatch.setattr(wiring, "run_recall", refuse)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    survived = json.loads(metrics.read_text(encoding="utf-8"))
    assert all(entry["name"] != RECALL_METRIC for entry in survived["metrics"])
    assert out.read_text(encoding="utf-8") == out_before


def test_metrics_without_out_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a): the gauge may never be the ONLY artifact -- typed refusal, nothing touched,
    the worker never spawned."""
    _, metrics = _seed_documents(tmp_path)
    before = metrics.read_text(encoding="utf-8")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=None, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called, "refusal must precede the measurement, not follow it"
    assert metrics.read_text(encoding="utf-8") == before


def test_the_per_profile_timeout_resolves_and_an_explicit_value_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B1: each profile resolves its measured ceiling; an explicit timeout overrides."""
    from bench.harness.recall import PROFILE_TIMEOUTS, run_recall

    seen: list[object] = []

    def spy(command, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise RecallStageError("stop after recording the timeout")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", spy)
    for profile, expected in (("full", 9600.0), ("smoke", 1800.0), ("tiny", 300.0)):
        with pytest.raises(RecallStageError):
            run_recall(profile, scratch=tmp_path)
        assert seen[-1] == expected == PROFILE_TIMEOUTS[profile]
    with pytest.raises(RecallStageError):
        run_recall("tiny", scratch=tmp_path, timeout_seconds=77.0)
    assert seen[-1] == 77.0


def test_the_cli_timeout_flag_propagates_to_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: --timeout-seconds reaches run_recall through append_vector_recall."""
    out, metrics = _seed_documents(tmp_path)
    seen: dict[str, object] = {}

    def spy(profile, *, gt_mode, scratch, timeout_seconds=None):
        seen["timeout"] = timeout_seconds
        return _verdict_stub()

    monkeypatch.setattr(wiring, "run_recall", spy)
    code = wiring.main(
        [
            "--profile",
            "tiny",
            "--gt",
            "auto",
            "--out",
            str(out),
            "--metrics",
            str(metrics),
            "--workspace",
            str(tmp_path),
            "--timeout-seconds",
            "123.5",
        ]
    )
    assert code == 0
    assert seen["timeout"] == 123.5


def test_malformed_roots_are_refused_before_the_worker_ever_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4): non-dict roots and a non-list metrics key refuse typed, worker untouched."""
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    array_out = tmp_path / "calibration.json"
    array_out.write_text("[1, 2]", encoding="utf-8")
    good_metrics = tmp_path / "metrics.json"
    good_metrics.write_text(json.dumps({"metrics": []}), encoding="utf-8")
    assert (
        append_vector_recall(
            profile="tiny",
            gt_mode="auto",
            out=array_out,
            metrics=good_metrics,
            workspace=tmp_path,
        )
        == 3
    )
    good_out = tmp_path / "calibration2.json"
    good_out.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    dict_metrics = tmp_path / "metrics2.json"
    dict_metrics.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    assert (
        append_vector_recall(
            profile="tiny",
            gt_mode="auto",
            out=good_out,
            metrics=dict_metrics,
            workspace=tmp_path,
        )
        == 3
    )
    missing = tmp_path / "absent.json"
    assert (
        append_vector_recall(
            profile="tiny",
            gt_mode="auto",
            out=good_out,
            metrics=missing,
            workspace=tmp_path,
        )
        == 3
    )
    assert not called, "the worker must never spawn over a malformed document"


def test_a_strip_failure_aborts_before_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4): a bombing strip write exits 3 and the measurement never starts."""
    out, metrics = _seed_documents(tmp_path)
    document = json.loads(metrics.read_text(encoding="utf-8"))
    document["metrics"].append(
        {
            "name": RECALL_METRIC,
            "kind": "gauge",
            "unit": "ratio",
            "samples": [{"value": 0.42}],
        }
    )
    metrics.write_text(json.dumps(document), encoding="utf-8")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )

    def bomb(path: Path, mutate: object) -> None:
        raise OSError("synthetic strip bomb")

    monkeypatch.setattr(wiring, "_replace_json", bomb)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called, "strip failure must abort BEFORE the worker"


def test_an_ordinary_worker_exception_is_a_typed_exit_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4): RuntimeError from the stage surfaces as exit 3 with documents intact."""
    out, metrics = _seed_documents(tmp_path)
    out_before = out.read_text(encoding="utf-8")
    metrics_before = metrics.read_text(encoding="utf-8")

    def explode(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic engine explosion")

    monkeypatch.setattr(wiring, "run_recall", explode)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert out.read_text(encoding="utf-8") == out_before
    assert metrics.read_text(encoding="utf-8") == metrics_before


@pytest.mark.parametrize(
    "bad",
    [10**10000, "0.9", True, float("nan"), float("inf"), -0.1, 1.5, None],
    # Explicit ids: str(10**10000) in pytest's id generation exceeds the digit limit.
    ids=["huge-int", "string", "bool", "nan", "inf", "negative", "above-one", "absent"],
)
def test_an_invalid_worker_gauge_is_refused_before_any_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """The verdict's gauge is validated BEFORE the section append: huge int (float()
    itself raises OverflowError), string, bool, non-finite and out-of-range are all
    typed exit 3 with both documents exactly as the strip left them."""
    out, metrics = _seed_documents(tmp_path)
    out_before = out.read_text(encoding="utf-8")
    metrics_before = metrics.read_text(encoding="utf-8")
    verdict = _verdict_stub()
    verdict["gauge"] = bad
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: verdict)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert out.read_text(encoding="utf-8") == out_before, "no section may land"
    assert metrics.read_text(encoding="utf-8") == metrics_before, "no gauge may land"


def _broken(mutation) -> dict[str, object]:
    """A coherent verdict with exactly one adulteration applied."""
    verdict = _verdict_stub()
    mutation(verdict)
    return verdict


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.__setitem__("gauge", 0.99),
        lambda v: v["observed"].__setitem__("mean_recall_at_k", float("nan")),
        lambda v: v.__setitem__("hnsw", {}),
        lambda v: v.__setitem__("corpus_size", 97),
        lambda v: v["hashes"].__setitem__("corpus_sha256_f64", "short"),
        lambda v: v["observed"]["dtype_check"].__setitem__("mean_overlap", 0.5),
        lambda v: v["observed"].__setitem__("queries_below_perfect", 0),
        lambda v: v.__setitem__("blas_environment", {}),
        lambda v: v.__setitem__("gt_path_used", "numpy"),
        lambda v: v.__setitem__("k", 4.0),
        lambda v: v.__setitem__("hashes", {key: "0" * 64 for key in v["hashes"]}),
        lambda v: v["hnsw"].__setitem__("neighbours", 16.0),
        lambda v: v.__setitem__("failure", "x"),
        lambda v: v.__setitem__("exit_code", 1),
        lambda v: v.__setitem__("duration_seconds", -1.0),
        lambda v: v["blas_environment"].__setitem__("EXTRA_THREADS", "1"),
        # gauge follows the adulterated mean so the GAUGE rule stays satisfied and the
        # MEAN/BELOW coherence guard is what refuses -- the exact impossible verdict:
        # a perfect mean beside min 0.9 and one below-perfect query.
        lambda v: (
            v["observed"].__setitem__("mean_recall_at_k", 1.0),
            v.__setitem__("gauge", 1.0),
        ),
    ],
    ids=[
        "gauge-vs-mean",
        "nan-observed",
        "foreign-hnsw",
        "wrong-corpus",
        "bad-hash",
        "dtype-below-floor",
        "count-contradicts-min",
        "unpinned-blas",
        "oracle-gt-mismatch",
        "float-typed-k",
        "zeroed-hashes",
        "float-typed-hnsw",
        "nonempty-failure",
        "nonzero-exit",
        "negative-duration",
        "extra-blas-key",
        "perfect-mean-with-below-count",
    ],
)
def test_an_adulterated_verdict_is_refused_before_any_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation
) -> None:
    """Reaudit HIGH-1: gauge 0.99 beside observed 0.01 (and every sibling adulteration)
    was published with exit 0. Exit 3 typed now, both documents byte-identical."""
    out, metrics = _seed_documents(tmp_path)
    out_before = out.read_text(encoding="utf-8")
    metrics_before = metrics.read_text(encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _broken(mutation))
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert out.read_text(encoding="utf-8") == out_before
    assert metrics.read_text(encoding="utf-8") == metrics_before


def test_an_unrealizable_summary_is_refused_and_the_possible_one_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 HIGH-1, the auditor's exact probe: tiny (q=8, k=4) with min=0.75,
    below=1 and mean=gauge=0.99 is arithmetic fiction -- only 31/32 is realizable."""
    from bench.harness.recall import _validate_verdict

    impossible = _verdict_stub()
    impossible["observed"]["mean_recall_at_k"] = 0.99
    impossible["gauge"] = 0.99
    reason = _validate_verdict(impossible, "tiny")
    assert reason is not None and "grid" in reason, reason
    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: impossible)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8")) == (
        before
    )
    assert _validate_verdict(_verdict_stub(), "tiny") is None, "31/32 must pass"
    off_grid_min = _verdict_stub()
    off_grid_min["observed"]["min_recall_at_k"] = 0.9
    assert _validate_verdict(off_grid_min, "tiny") is not None, (
        "0.9 is off the k=4 grid"
    )


def test_a_held_publication_lock_refuses_a_second_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 B: competing stages serialize behind the O_EXCL lock file; the second
    refuses typed instead of interleaving, never spawns, never steals the lock --
    and after a release the stage succeeds and cleans its OWN lock."""
    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    lock = metrics.with_name(metrics.name + ".c13.lock")
    lock.write_text("12345", encoding="ascii")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called, "a contended stage must never spawn the worker"
    assert (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8")) == (
        before
    )
    assert lock.exists(), "a foreign lock must not be stolen or removed"
    lock.unlink()
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 0
    assert not lock.exists(), "the stage releases its own lock on every exit"


def test_a_zero_progress_writer_cannot_tear_a_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: a write() returning 0 used to replace valid JSON with an empty
    file and report success. The byte-verified loop refuses; the stage exits typed;
    both documents stay byte-identical."""
    import tempfile as tempfile_module

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    captured: dict[str, int] = {}
    real_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object):
        descriptor, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = descriptor
        return descriptor, name

    real_write = os.write

    def zero_write(descriptor: int, data: bytes) -> int:
        if descriptor == captured.get("fd"):
            return 0
        return real_write(descriptor, data)

    import os as os_module

    monkeypatch.setattr(tempfile_module, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(os_module, "write", zero_write)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    after = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    assert after == before, "no torn or empty document may replace a valid one"
    leftovers = [p.name for p in tmp_path.iterdir() if ".c13-" in p.name]
    assert leftovers == [], "the scratch is cleaned when the write is refused"


def test_a_hostile_exception_str_cannot_escape_the_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: printing {failure} re-executed a hostile __str__ inside the
    catch-all. The guarded describer keeps the exit typed."""

    class EvilError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("hostile str")

        def __repr__(self) -> str:
            raise RuntimeError("hostile repr")

    out, metrics = _seed_documents(tmp_path)

    def explode(*args: object, **kwargs: object) -> dict[str, object]:
        raise EvilError()

    monkeypatch.setattr(wiring, "run_recall", explode)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3


def test_hardlinked_documents_are_refused_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: os.replace breaks a hardlink relation, so no atomic publication
    coherent across the names exists -- typed refusal, worker untouched."""
    out, metrics = _seed_documents(tmp_path)
    alias = tmp_path / "metrics-alias.json"
    try:
        os.link(metrics, alias)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=alias, workspace=tmp_path
    )
    assert code == 3
    assert not called


def test_out_and_metrics_resolving_to_one_file_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: the section and the gauge need DISTINCT physical documents."""
    shared = tmp_path / "both.json"
    shared.write_text(json.dumps({"metrics": []}), encoding="utf-8")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=shared, metrics=shared, workspace=tmp_path
    )
    assert code == 3
    assert not called


def test_the_lock_identity_is_the_canonical_path_not_the_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: a lock held on the CANONICAL path refuses a run addressing the
    same file through a dotted alias spelling -- text identity was not enough."""
    out, metrics = _seed_documents(tmp_path)
    canonical_metrics = metrics.resolve(strict=True)
    lock = canonical_metrics.with_name(canonical_metrics.name + ".c13.lock")
    lock.write_text("12345", encoding="ascii")
    dotted = tmp_path / "." / "metrics.json"
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=dotted, workspace=tmp_path
    )
    assert code == 3
    assert not called
    lock.unlink()


def test_negative_zero_scalars_are_refused_as_worker_impossibilities() -> None:
    """Round-4 final: -0.0 == +0.0 and frozenset[float] collapse the signs; the
    worker only produces +0.0. Coherent-but-negative-zero verdicts are refused."""
    from bench.harness.recall import _validate_verdict

    negative_min = _verdict_stub()
    negative_min["observed"]["mean_recall_at_k"] = 0.875  # 28/32: one query at 0
    negative_min["gauge"] = 0.875
    negative_min["observed"]["min_recall_at_k"] = -0.0
    reason = _validate_verdict(negative_min, "tiny")
    assert reason is not None, "-0.0 min passed every pre-fix rule"
    positive_control = _verdict_stub()
    positive_control["observed"]["mean_recall_at_k"] = 0.875
    positive_control["gauge"] = 0.875
    positive_control["observed"]["min_recall_at_k"] = 0.0
    assert _validate_verdict(positive_control, "tiny") is None, _validate_verdict(
        positive_control, "tiny"
    )
    negative_gauge = _verdict_stub()
    negative_gauge["observed"]["mean_recall_at_k"] = 0.0
    negative_gauge["observed"]["min_recall_at_k"] = 0.0
    negative_gauge["observed"]["queries_below_perfect"] = 8
    negative_gauge["gauge"] = -0.0
    assert _validate_verdict(negative_gauge, "tiny") is not None
    negative_duration = _verdict_stub()
    negative_duration["duration_seconds"] = -0.0
    assert _validate_verdict(negative_duration, "tiny") is not None


def test_run_recall_refuses_success_over_temp_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 final: a parsed verdict whose fresh file cannot be removed used to
    return success with the temp left behind."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    scratch = tmp_path / "scratch"

    def writes_verdict(command, **kwargs):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    real_unlink = os.unlink

    def sticky(target: object, *args: object, **kwargs: object) -> None:
        # Round-7 (4): the cleanup runs on the BUILTIN name now, so this is where the
        # stickiness has to be injected -- patching Path.unlink would leave the real
        # mechanism untouched and the probe would pass without proving anything.
        if "recall-tiny-" in str(target):
            raise OSError("sticky temp")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", sticky)
    with pytest.raises(RecallStageError, match="residue"):
        run_recall("tiny", scratch=scratch)


def test_every_document_is_locked_not_just_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock blocker 1: a lock held on OUT alone must refuse a run that shares out
    with a different metrics -- the single-target lock only covered metrics. Both
    locks are released after a successful run."""
    out, metrics = _seed_documents(tmp_path)
    out_lock = out.with_name(out.name + ".c13.lock")
    out_lock.write_text("12345", encoding="ascii")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called, "a contended OUT lock must refuse before the worker"
    assert out_lock.exists(), "a foreign lock must not be stolen"
    out_lock.unlink()
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 0
    assert not out_lock.exists()
    assert not metrics.with_name(metrics.name + ".c13.lock").exists()


def test_a_failed_acquisition_unwinds_every_already_held_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock blocker 2: with two documents, the SECOND lock contended must release the
    first before the typed refusal -- no lock and no descriptor may leak."""
    out, metrics = _seed_documents(tmp_path)
    # Sorted order: calibration.json < metrics.json, so the metrics lock is second.
    metrics_lock = metrics.with_name(metrics.name + ".c13.lock")
    metrics_lock.write_text("12345", encoding="ascii")
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called
    out_lock = out.with_name(out.name + ".c13.lock")
    assert not out_lock.exists(), "the first-acquired lock must be unwound"
    assert metrics_lock.exists(), "the foreign lock stays"
    metrics_lock.unlink()


def test_metrics_mutated_underneath_fails_typed_not_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 B: 'metrics' turning into a dict between precheck and append used to
    return SUCCESS with section-without-gauge. Typed exit 3 now; the section stays as
    the honest partial a --require-recall gate reads as UNMEASURED."""
    out, metrics = _seed_documents(tmp_path)

    def sabotage(*args: object, **kwargs: object) -> dict[str, object]:
        metrics.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
        return _verdict_stub()

    monkeypatch.setattr(wiring, "run_recall", sabotage)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert "vector_recall" in json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(metrics.read_text(encoding="utf-8"))["metrics"] == {}


def test_a_hostile_metaclass_timeout_is_still_the_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 C, recall side: the refusal message must not touch the offender again."""
    from bench.harness.recall import run_recall

    class _HostileMeta(type):
        @property
        def __name__(cls) -> str:  # noqa: N804
            raise RuntimeError("hostile metaclass")

    class _Hostile(metaclass=_HostileMeta):
        def __repr__(self) -> str:
            raise RuntimeError("hostile repr")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", forbidden)
    with pytest.raises(RecallStageError, match="finite positive"):
        run_recall("tiny", scratch=tmp_path / "never", timeout_seconds=_Hostile())  # type: ignore[arg-type]


def test_a_non_utf8_verdict_is_a_typed_refusal_with_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 D: UnicodeDecodeError escaped run_recall as itself; now the typed
    RecallStageError, with the fresh per-run file still cleaned outcome-neutrally."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    scratch = tmp_path / "scratch"

    def writes_garbage(command, **kwargs):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_bytes(b"\xff\xfe\x00garbage")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_garbage)
    with pytest.raises(RecallStageError, match="no readable verdict"):
        run_recall("tiny", scratch=scratch)
    leftovers = [p.name for p in scratch.iterdir() if p.name.startswith("recall-tiny-")]
    assert leftovers == [], "cleanup must be outcome-neutral"


def test_the_mean_acceptance_set_is_exactly_the_fsum_reachable_one() -> None:
    """Round-3 final: at k=4 the pipeline is EXACT (dyadic grid), so even the 1-ulp
    neighbours of 31/32 are fiction and are refused; every fabricated delta dies; and
    a REAL k=10 histogram computed through the worker's own fsum pipeline is accepted
    -- the interval collapses to equality for dyadic k and spans real ulps for k=10."""
    import math as math_module

    from bench.harness.recall import _expected_hashes, _validate_verdict
    from bench.harness.recall_worker import PROFILES

    for direction in (0.0, 1.0):
        neighbour = _verdict_stub()
        nudged = math_module.nextafter(0.96875, direction)
        neighbour["observed"]["mean_recall_at_k"] = nudged
        neighbour["gauge"] = nudged
        reason = _validate_verdict(neighbour, "tiny")
        assert reason is not None, (direction, "dyadic k=4 admits ONLY exact 31/32")
    for delta in (1e-12, 1e-10, 1e-9, 1e-8):
        fabricated = _verdict_stub()
        fabricated["observed"]["mean_recall_at_k"] = 0.96875 + delta
        fabricated["gauge"] = 0.96875 + delta
        reason = _validate_verdict(fabricated, "tiny")
        assert reason is not None and "grid" in reason, (delta, reason)
    smoke = PROFILES["smoke"]
    recalls = [1.0] * (smoke.queries - 1) + [3 / 10]
    mean = math_module.fsum(recalls) / smoke.queries
    real_histogram = {
        "ok": True,
        "failure": "",
        "profile": "smoke",
        "generator": "uniform-int53-v1",
        "oracle": "pure-fsum",
        "gt_path_used": "pure",
        "numpy": "absent",
        "k": smoke.k,
        "queries": smoke.queries,
        "corpus_size": smoke.corpus_size,
        "dimension": smoke.dimension,
        "hashes": _expected_hashes(smoke),
        "gauge": mean,
        "observed": {
            "mean_recall_at_k": mean,
            "min_recall_at_k": 3 / 10,
            "queries_below_perfect": 1,
            "dtype_check": {"mean_overlap": 1.0, "min_overlap": 1.0},
        },
        "blas_environment": {name: "1" for name in _BLAS_THREAD_VARIABLES},
        "hnsw": dict(HNSW_FROZEN),
        "duration_seconds": 1.0,
        "exit_code": 0,
    }
    verdict = _validate_verdict(real_histogram, "smoke")
    assert verdict is None, verdict


def test_a_chameleon_mapping_cannot_split_validation_from_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 HIGH-2 (TOCTOU): coherent under get(), poisoned under __getitem__.
    The canonical snapshot refuses the subclass outright; nothing is published."""

    class Chameleon(dict):
        def __getitem__(self, key: object) -> object:
            if key == "observed":
                return {
                    "mean_recall_at_k": 0.0,
                    "min_recall_at_k": 0.0,
                    "queries_below_perfect": 8,
                    "dtype_check": {"mean_overlap": 1.0, "min_overlap": 1.0},
                }
            return dict.__getitem__(self, key)

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: Chameleon(_verdict_stub())
    )
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    after = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    assert after == before, "a chameleon must publish NOTHING, coherent or not"


def test_a_hostile_mapping_verdict_cannot_escape_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-review (f): a dict SUBCLASS passes isinstance and raises inside the
    validator; the boundary converts it into the typed exit 3, documents untouched."""

    class Hostile(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("hostile verdict mapping")

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: Hostile())
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    after = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    assert after == before


def test_a_worker_that_writes_nothing_cannot_resurrect_a_stale_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaudit HIGH-2: a deterministic path let returncode-0-without-writing hand back a
    PREVIOUS run's file. The fresh per-run file makes absence a typed refusal."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    stale = scratch / "recall-tiny.json"
    stale_content = json.dumps(_verdict_stub())
    stale.write_text(stale_content, encoding="utf-8")

    def silent_success(command, **kwargs):
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", silent_success)
    with pytest.raises(RecallStageError, match="fresh per-run"):
        run_recall("tiny", scratch=scratch)
    assert stale.read_text(encoding="utf-8") == stale_content, "never consumed"


def test_run_recall_cleans_its_fresh_file_and_returns_the_written_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-2 happy path: the worker's own write is read back; cleanup is outcome-neutral."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    scratch = tmp_path / "scratch"

    def writes_verdict(command, **kwargs):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    verdict = run_recall("tiny", scratch=scratch)
    assert verdict["ok"] is True
    assert verdict["exit_code"] == 0
    leftovers = [p.name for p in scratch.iterdir() if p.name.startswith("recall-tiny-")]
    assert leftovers == [], "the fresh per-run file must be cleaned outcome-neutrally"


def test_a_deeply_nested_document_is_refused_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM-3: json.loads raises RecursionError on ~5000 nesting levels; the
    pre-validation refuses it typed, and the worker never spawns."""
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    deep = tmp_path / "calibration.json"
    deep.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"metrics": []}), encoding="utf-8")
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=deep, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert not called, "no spawn over an unreadable document"


def test_a_malformed_ok_verdict_fails_typed_before_any_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ok=true verdict MISSING a key build_section needs (hashes) raises KeyError --
    a shape the old four-type catch missed. Exit 3 typed, both documents exactly as the
    strip left them, because build_section runs inside the boundary BEFORE any append."""
    out, metrics = _seed_documents(tmp_path)
    out_before = out.read_text(encoding="utf-8")
    metrics_before = metrics.read_text(encoding="utf-8")
    verdict = _verdict_stub()
    del verdict["hashes"]
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: verdict)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert out.read_text(encoding="utf-8") == out_before
    assert metrics.read_text(encoding="utf-8") == metrics_before


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), 0, -5, True, 10**10000, -(10**10000)],
    # Explicit ids: str(10**10000) in pytest's id generation exceeds the digit limit.
    ids=["nan", "inf", "zero", "negative", "bool", "huge-int", "huge-negative-int"],
)
def test_an_invalid_timeout_is_refused_before_mkdir_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """(5): nan/inf/zero/negative/bool never reach the filesystem or a subprocess."""
    from bench.harness.recall import run_recall

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", forbidden)
    scratch = tmp_path / "never-created"
    with pytest.raises(RecallStageError, match="finite positive"):
        run_recall("tiny", scratch=scratch, timeout_seconds=bad)  # type: ignore[arg-type]
    assert not scratch.exists(), "validation must precede mkdir"


# =====================================================================================
# Round-5 blocker 1: a write that LIES about its byte count, and the unguarded gap
# =====================================================================================


@pytest.mark.parametrize(
    "report",
    [lambda data: True, lambda data: len(data) + 1],
    ids=["bool-true-is-not-a-byte-count", "count-larger-than-the-remainder"],
)
def test_a_write_that_lies_about_its_byte_count_cannot_empty_a_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: object
) -> None:
    """Round-5 blocker 1a: ``isinstance(progress, int) and progress > 0`` accepted True
    -- bool IS an int and True > 0 -- and accepted any count LARGER than what remained.
    Either lie satisfied the loop with ZERO bytes on disk, and os.replace then swapped a
    VALID document for an empty one. The count must be an exact int within the
    remainder."""
    import tempfile as tempfile_module

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    captured: dict[str, int] = {}
    real_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object):
        descriptor, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = descriptor
        return descriptor, name

    real_write = os.write

    def lying_write(descriptor: int, data: bytes) -> object:
        if descriptor == captured.get("fd"):
            return report(data)  # type: ignore[operator]
        return real_write(descriptor, data)

    monkeypatch.setattr(tempfile_module, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(os, "write", lying_write)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    after = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    assert after == before, "an impossible byte count may not replace a valid document"
    assert [p.name for p in tmp_path.iterdir() if ".c13-" in p.name] == []


def test_a_partial_write_followed_by_a_stall_preserves_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 1: the honest torn write -- REAL progress and then a stall. The
    bytes that landed are in the scratch and never in the document, which stays exactly
    as it was."""
    import tempfile as tempfile_module

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    captured: dict[str, int] = {}
    real_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object):
        descriptor, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = descriptor
        return descriptor, name

    real_write = os.write
    calls: list[int] = []

    def partial_then_stall(descriptor: int, data: bytes) -> int:
        if descriptor != captured.get("fd"):
            return real_write(descriptor, data)
        calls.append(1)
        if len(calls) == 1:
            return real_write(descriptor, data[: max(1, len(data) // 2)])
        return 0

    monkeypatch.setattr(tempfile_module, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(os, "write", partial_then_stall)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    assert code == 3
    assert len(calls) >= 2, "the probe must exercise real progress before the stall"
    after = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    assert after == before, "a half-written scratch may never become the document"
    assert [p.name for p in tmp_path.iterdir() if ".c13-" in p.name] == []


def test_an_unserializable_document_never_acquires_a_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 1b: json.dumps sat BETWEEN the mkstemp and the try, so a
    serialization failure leaked the live descriptor AND the scratch file with nothing
    to clean either up. The bytes are produced first, so a failure there acquires
    nothing at all -- proved by forbidding mkstemp outright."""
    import tempfile as tempfile_module

    document = tmp_path / "document.json"
    document.write_text(json.dumps({"kept": True}), encoding="utf-8")
    before = document.read_text(encoding="utf-8")

    def forbidden_mkstemp(*args: object, **kwargs: object):
        raise AssertionError("no descriptor may be acquired before the bytes exist")

    monkeypatch.setattr(tempfile_module, "mkstemp", forbidden_mkstemp)

    def unserializable(payload: dict[str, object]) -> bool:
        payload["bad"] = object()
        return True

    with pytest.raises(TypeError):
        wiring._replace_json(document, unserializable)
    assert document.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if ".c13-" in p.name] == []


# =====================================================================================
# Round-5 blocker 2: the PRIMARY exception survives every cleanup shape
# =====================================================================================


@pytest.mark.parametrize(
    "primary", [KeyboardInterrupt, SystemExit], ids=["worker-KI", "worker-SE"]
)
@pytest.mark.parametrize(
    "cleanup",
    [KeyboardInterrupt, SystemExit, RuntimeError],
    ids=["cleanup-KI", "cleanup-SE", "cleanup-Exception"],
)
def test_the_primary_interrupt_survives_every_cleanup_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: type[BaseException],
    cleanup: type[BaseException],
) -> None:
    """Round-5 blocker 2, the full matrix: the reverse release ran under an
    Exception-only clause, so a KeyboardInterrupt/SystemExit raised BY THE UNLINK
    aborted the cleanup of the remaining locks and REPLACED the primary with a cleanup
    accident. The release now absorbs every shape, reaches every lock, and what the
    caller sees is always the primary."""
    out, metrics = _seed_documents(tmp_path)

    def interrupted_worker(*args: object, **kwargs: object) -> dict[str, object]:
        raise primary("PRIMARY")

    monkeypatch.setattr(wiring, "run_recall", interrupted_worker)
    real_unlink = os.unlink
    attempted: list[str] = []

    def hostile_unlink(path: object, *args: object, **kwargs: object) -> None:
        if str(path).endswith(".c13.lock"):
            attempted.append(str(path))
            raise cleanup("CLEANUP")
        return real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", hostile_unlink)
    with pytest.raises(primary) as caught:
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    assert "PRIMARY" in str(caught.value), "a cleanup accident may not become the story"
    assert len(attempted) == 2, "the reverse release must reach EVERY lock, any shape"


def test_an_interrupt_closing_the_second_lock_still_releases_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 2: the final close ran under an Exception-only clause, so a KI/SE
    closing the SECOND lock left the descriptor open AND both lock files on disk -- the
    worst residue of any path here. The certain lock is released; only the lock whose
    descriptor state is uncertain stays, and the interrupt propagates."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_open, real_close = os.open, os.close
    lock_descriptors: list[int] = []

    def spy_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if str(path).endswith(".c13.lock"):
            lock_descriptors.append(descriptor)
        return descriptor

    def interrupting_close(descriptor: int) -> None:
        real_close(descriptor)
        if len(lock_descriptors) == 2 and descriptor == lock_descriptors[-1]:
            raise KeyboardInterrupt("interrupted closing the second lock")

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", interrupting_close)
    with pytest.raises(KeyboardInterrupt):
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()
    leftover = sorted(
        p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")
    )
    assert leftover == ["metrics.json.c13.lock"], (
        "the certainly-closed lock is released; the uncertain one is left in place"
    )


def test_an_interrupt_preparing_the_verdict_file_removes_it_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 2: run_recall's initial close was Exception-only, so a KI/SE there
    left the fresh per-run file behind with no cleanup at all. Every shape now runs the
    same cleanup, and nothing is spawned on either path."""
    from bench.harness.recall import run_recall

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", forbidden)
    real_close = os.close

    def interrupting_close(descriptor: int) -> None:
        real_close(descriptor)
        raise KeyboardInterrupt("interrupted preparing the verdict file")

    scratch = tmp_path / "scratch"
    monkeypatch.setattr(os, "close", interrupting_close)
    with pytest.raises(KeyboardInterrupt):
        run_recall("tiny", scratch=scratch, timeout_seconds=30)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], (
        "the fresh temp file is removed on every shape"
    )


def test_an_interrupt_in_the_verdict_cleanup_cannot_replace_the_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 2: the finally's unlink was Exception-only, so a KI/SE raised
    THERE became the reported outcome and erased the worker's own typed failure. A
    leftover temp file is the lesser harm, and the residue check still speaks."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    def failing_worker(command: list[str], **kwargs: object):
        return subprocess_module.CompletedProcess(
            command, 7, stdout="", stderr="the worker died"
        )

    monkeypatch.setattr("bench.harness.recall.subprocess.run", failing_worker)
    real_unlink = Path.unlink

    def interrupting_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("recall-tiny-"):
            raise KeyboardInterrupt("interrupted during cleanup")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupting_unlink)
    with pytest.raises(RecallStageError) as caught:
        run_recall("tiny", scratch=tmp_path / "scratch")
    assert "exit 7" in str(caught.value), (
        "the worker's own failure -- its exit code -- is what is reported, not the "
        "KeyboardInterrupt the cleanup raised on top of it"
    )


def test_an_existence_check_that_cannot_answer_counts_as_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker 2: Path.exists() sat outside every guard, so a check that raises
    escaped run_recall untyped -- and an untyped escape from a stage whose contract is
    RecallStageError is exactly the traceback the harness must never produce. A doubt
    that cannot be resolved counts as residue."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    real_unlink, real_exists = os.unlink, os.path.exists

    def sticky(target: object, *args: object, **kwargs: object) -> None:
        if "recall-tiny-" in str(target):
            raise OSError("sticky temp")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    def unanswerable(target: object) -> bool:
        if "recall-tiny-" in str(target):
            raise OSError("the existence of this path cannot be determined")
        return real_exists(target)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", sticky)
    monkeypatch.setattr(os.path, "exists", unanswerable)
    with pytest.raises(RecallStageError, match="residue"):
        run_recall("tiny", scratch=tmp_path / "scratch")


@pytest.mark.parametrize("target", ["open", "write", "close", "unlink"])
def test_a_hostile_failure_in_the_lock_paths_refuses_without_re_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """Round-5 blocker 3: the acquisition and release reasons interpolated {failure}
    RAW, so a hostile __str__ ran again inside the very refusal that was describing it,
    and the refusal crashed. Every diagnosis goes through the guarded describer, so each
    injection ends as a typed exit 3."""

    class EvilError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("hostile str")

        def __repr__(self) -> str:
            raise RuntimeError("hostile repr")

    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real = {name: getattr(os, name) for name in ("open", "write", "close", "unlink")}
    lock_descriptors: list[int] = []

    def hostile_open(path: object, *args: object, **kwargs: object) -> int:
        if str(path).endswith(".c13.lock"):
            if target == "open":
                raise EvilError()
            descriptor = real["open"](path, *args, **kwargs)
            lock_descriptors.append(descriptor)
            return descriptor
        return real["open"](path, *args, **kwargs)

    def hostile_write(descriptor: int, data: bytes) -> int:
        if target == "write" and descriptor in lock_descriptors:
            raise EvilError()
        return real["write"](descriptor, data)

    def hostile_close(descriptor: int) -> None:
        if target == "close" and descriptor in lock_descriptors:
            real["close"](descriptor)
            raise EvilError()
        return real["close"](descriptor)

    def hostile_unlink(path: object, *args: object, **kwargs: object) -> None:
        if target == "unlink" and str(path).endswith(".c13.lock"):
            raise EvilError()
        return real["unlink"](path, *args, **kwargs)

    monkeypatch.setattr(os, "open", hostile_open)
    monkeypatch.setattr(os, "write", hostile_write)
    monkeypatch.setattr(os, "close", hostile_close)
    monkeypatch.setattr(os, "unlink", hostile_unlink)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "a hostile failure anywhere in the lock paths is a typed refusal"


def test_a_file_exists_error_while_stamping_is_not_a_contention_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5: the FileExistsError clause spanned the open AND the write, so this error
    coming out of os.write -- after WE created the lock, with OUR descriptor open -- was
    reported as another stage holding it. That refusal told the operator to delete a
    file this process had just created, and it leaked the descriptor, because the
    contention path has no descriptor to close. Creating and stamping are separate
    phases now: only the open can conclude contention."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_open, real_write = os.open, os.write
    lock_descriptors: list[int] = []

    def spy_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if str(path).endswith(".c13.lock"):
            lock_descriptors.append(descriptor)
        return descriptor

    def refusing_write(descriptor: int, data: bytes) -> int:
        if descriptor in lock_descriptors:
            raise FileExistsError("the stamp failed with the contention error shape")
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "write", refusing_write)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == [], (
        "a lock this process created and could not stamp is its own to remove"
    )


# =====================================================================================
# Round-6: a diagnostic never escapes a refusal, and never replaces a primary
# =====================================================================================


@pytest.mark.parametrize(
    "shape", [SystemExit, KeyboardInterrupt], ids=["SystemExit", "KeyboardInterrupt"]
)
def test_the_describer_absorbs_a_repr_that_raises_any_shape(
    shape: type[BaseException],
) -> None:
    """Round-6: _describe caught Exception only, so a __repr__ raising SystemExit or
    KeyboardInterrupt escaped the one helper whose entire purpose is to keep a refusal
    from crashing. Everywhere else KI/SE propagate because those places do WORK; here a
    value is formatted for a message and the shape is chosen by the object described, so
    an escaping SystemExit is not the process asking to exit -- it is hostile data
    walking through the guard."""
    from bench.harness.gate import _describe as gate_describe
    from bench.harness.recall import _describe as recall_describe

    class _Exploding:
        def __repr__(self) -> str:
            raise shape(71)

        def __str__(self) -> str:
            raise shape(71)

    for describe in (recall_describe, gate_describe):
        assert describe(_Exploding()) == "<value whose repr raises>"


def test_a_hostile_path_object_cannot_mask_its_own_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-6: the alias refusal interpolated the caller's object RAW, so a path-like
    whose resolve() fails and whose __str__ raises SystemExit escaped through the very
    message announcing the refusal. The refusal still names the offender -- safely."""

    class _HostilePath:
        def resolve(self, strict: bool = False) -> object:
            raise RuntimeError("this path cannot be resolved")

        def __str__(self) -> str:
            raise SystemExit(72)

        def __repr__(self) -> str:
            raise SystemExit(72)

    code = append_vector_recall(
        profile="tiny",
        gt_mode="auto",
        out=_HostilePath(),  # type: ignore[arg-type]
        metrics=None,
        workspace=tmp_path,
    )
    assert code == 3, "a path that cannot be resolved is a typed refusal, not an exit"
    assert "REFUSED" in capsys.readouterr().out


def test_a_diagnostic_print_in_the_finally_cannot_replace_the_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6: the residue warnings are printed from inside a finally. A print that
    raises did not merely lose the message -- it REPLACED the primary with the failure
    of the message about it. Here the worker is interrupted, both unlinks fail so there
    IS residue to report, and the reporting itself exits: the caller must still see the
    interrupt."""
    import builtins

    out, metrics = _seed_documents(tmp_path)

    def interrupted(*args: object, **kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt("PRIMARY")

    monkeypatch.setattr(wiring, "run_recall", interrupted)
    real_unlink, real_print = os.unlink, builtins.print

    def failing_unlink(path: object, *args: object, **kwargs: object) -> None:
        if str(path).endswith(".c13.lock"):
            raise OSError("this lock cannot be removed")
        return real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    def exiting_print(*args: object, **kwargs: object) -> None:
        if args and "WARNING" in str(args[0]):
            raise SystemExit(73)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    monkeypatch.setattr(builtins, "print", exiting_print)
    with pytest.raises(KeyboardInterrupt) as caught:
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()
    assert "PRIMARY" in str(caught.value), (
        "the interrupt is the outcome; the failure of a warning about residue is not"
    )


def test_a_broken_stdout_does_not_change_an_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6, structural: the exit code is this stage's contract and the text is the
    courtesy. With every print raising, a refusal is still a refusal and a success is
    still a success -- silent, but never a traceback whose status means something else."""
    import builtins

    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())

    def broken_print(*args: object, **kwargs: object) -> None:
        raise OSError("stdout is closed")

    monkeypatch.setattr(builtins, "print", broken_print)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 0, "a publication that succeeded is still a success when silent"
    assert json.loads(metrics.read_text(encoding="utf-8"))["metrics"][-1]["name"] == (
        RECALL_METRIC
    ), "and the gauge really landed"


def test_a_document_that_cannot_be_named_is_never_locked() -> None:
    """Round-6: the lock names come from str(document), which is the CALLER's code, and
    it ran outside every guard -- before a single lock existed. A document that cannot
    even be named cannot be locked, and that is a typed refusal."""

    class _Unnameable:
        def __str__(self) -> str:
            raise RuntimeError("this document has no name")

        def __repr__(self) -> str:
            raise RuntimeError("nor a representation")

    held, refusal = wiring._acquire_publication_locks([_Unnameable()])  # type: ignore[list-item]
    assert held == []
    assert refusal is not None and "named for locking" in refusal

    class _ExitingName:
        """Round-7 (C, code 121): the shape the pre-fix code RE-RAISED. str() on a
        caller-supplied object is the caller's code and nothing else, so a SystemExit
        arriving here was fabricated by the data, not by the process asking to exit --
        and handing it back to the caller ended the run from inside a refusal path. An
        ordinary Exception cannot tell these two versions apart; only this can."""

        def __str__(self) -> str:
            raise SystemExit(121)

        def __repr__(self) -> str:
            raise SystemExit(121)

    held, refusal = wiring._acquire_publication_locks([_ExitingName()])  # type: ignore[list-item]
    assert held == []
    assert refusal is not None and "named for locking" in refusal


def test_an_exiting_existence_check_cannot_report_success_over_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 item 5: the residue check caught Exception, so exists() raising SystemExit
    escaped run_recall -- and SystemExit(0) is the worst shape here, because the process
    would exit SUCCESS with the temp still on disk and nothing published. An
    unanswerable existence question counts as residue."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    real_unlink, real_exists = os.unlink, os.path.exists

    def sticky(target: object, *args: object, **kwargs: object) -> None:
        if "recall-tiny-" in str(target):
            raise OSError("sticky temp")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    def exiting_exists(target: object) -> bool:
        if "recall-tiny-" in str(target):
            raise SystemExit(0)
        return real_exists(target)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", sticky)
    monkeypatch.setattr(os.path, "exists", exiting_exists)
    with pytest.raises(RecallStageError, match="residue"):
        run_recall("tiny", scratch=tmp_path / "scratch")


def test_a_failure_before_the_spawn_never_orphans_the_verdict_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 item 7: the fresh file is ours from the moment mkstemp's descriptor
    closes, but the cleanup region began only after the environment copy, the command
    construction and time.monotonic(). A failure in that gap left the file orphaned,
    with no cleanup and no spawn. The clock is the cheapest way to stand in for the
    whole region."""
    from bench.harness import recall as recall_module
    from bench.harness.recall import run_recall

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be reached")

    def broken_clock() -> float:
        raise RuntimeError("the clock is unavailable")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", forbidden)
    monkeypatch.setattr(recall_module.time, "monotonic", broken_clock)
    scratch = tmp_path / "scratch"
    with pytest.raises(RuntimeError, match="clock"):
        run_recall("tiny", scratch=scratch, timeout_seconds=30)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], (
        "a failure anywhere after the acquisition still removes the fresh file"
    )


# =====================================================================================
# Round-7: hostile data may choose the SHAPE it raises; only WORK keeps its interrupts
# =====================================================================================


class _ExitingLen(str):
    """A str subclass whose length cannot be taken -- what repr() may hand back."""

    def __len__(self) -> int:
        raise SystemExit(122)


class _ExitingFormat(str):
    """The shape a GUARD cannot save you from.

    It measures and slices like any string, so every check inside the describer passes
    and the value is RETURNED. The explosion happens afterwards, in the caller's
    f-string -- which is why the fix has to be type-exactness at the boundary rather
    than another try/except around the length.
    """

    def __format__(self, spec: str) -> str:
        raise SystemExit(91)

    def __str__(self) -> str:
        raise SystemExit(91)


class _ReprReturnsUnmeasurable:
    def __repr__(self) -> str:
        return _ExitingLen("x" * 100)


class _ReprReturnsUnformattable:
    def __repr__(self) -> str:
        return _ExitingFormat("short")


@pytest.mark.parametrize(
    "hostile",
    [_ReprReturnsUnmeasurable, _ReprReturnsUnformattable],
    ids=["len-raises-122", "format-raises-91"],
)
def test_a_repr_returning_a_str_subclass_never_leaves_the_describer(
    hostile: type,
) -> None:
    """Round-7 (1): repr() is only CONVENTIONALLY a str, and the two cases fail
    differently. The one whose __len__ raises (122) is caught by the guard. The one
    whose __format__ raises (91) is NOT: it measures fine, so it is returned intact and
    detonates in the f-string of whoever formats the refusal -- a frame no guard here
    can reach. Type-exactness at the boundary is what closes both, which is why this
    probe asserts on the TYPE that leaves and not merely on not-raising."""
    from bench.harness.gate import _describe as gate_describe
    from bench.harness.recall import _describe as recall_describe

    for describe in (recall_describe, gate_describe):
        described = describe(hostile())
        assert type(described) is str, "an exact str, not a subclass, leaves here"
        assert f"{described}" == described, "and it survives being formatted"


def test_a_path_like_whose_resolve_exits_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 (3, code 103): resolve() is the ARGUMENT's code, so the shape it raises is
    chosen by the caller's object, not by an interrupt of our work. It becomes a typed
    refusal; the worker never runs."""
    called: list[int] = []

    class _ExitingResolve:
        def resolve(self, strict: bool = False) -> object:
            raise SystemExit(103)

        def __str__(self) -> str:
            return "<hostile path>"

        def __repr__(self) -> str:
            return "<hostile path>"

    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    code = append_vector_recall(
        profile="tiny",
        gt_mode="auto",
        out=_ExitingResolve(),  # type: ignore[arg-type]
        metrics=None,
        workspace=tmp_path,
    )
    assert code == 3
    assert not called, "nothing runs behind a document that cannot be resolved"


def test_a_document_whose_containment_exits_is_refused_before_the_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 (5, code 102): `"metrics" in document` runs the parsed object's
    __contains__, and a dict subclass reached that line before any lock existed."""

    class _ExitingContains(dict):
        def __contains__(self, key: object) -> bool:
            raise SystemExit(102)

    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    real_loads = json.loads

    def hostile_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        return _ExitingContains(parsed) if isinstance(parsed, dict) else parsed

    monkeypatch.setattr(wiring.json, "loads", hostile_loads)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3
    assert not called
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_stale_gauge_check_that_exits_fails_the_stage_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 (5, code 132): _strip_stale_gauge inspects a parsed document with the
    locks already held, and a subclass raising there escaped. Stripping is what makes
    every crash window read as gauge-ABSENT, so a document we cannot inspect is a typed
    stage failure -- never a silent skip that publishes over an unknown state."""

    class _ExitingGet(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise SystemExit(132)

    out, metrics = _seed_documents(tmp_path)
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    real_loads = json.loads
    seen: list[int] = []

    def hostile_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        if not isinstance(parsed, dict):
            return parsed
        seen.append(1)
        # The prevalidation reads both documents first; only the strip, which runs with
        # the locks held, gets the hostile mapping.
        return _ExitingGet(parsed) if len(seen) > 2 else parsed

    monkeypatch.setattr(wiring.json, "loads", hostile_loads)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3
    assert not called, "the worker never runs behind an uninspectable strip"
    assert (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8")) == (
        before
    )
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_verdict_dict_subclass_is_refused_before_anything_is_written_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 (6, code 131): a dict SUBCLASS can answer every read coherently and then
    raise from __setitem__, which is where duration_seconds and exit_code are written --
    after the temp file has already been cleaned up. json.loads yields an exact dict, so
    requiring one costs nothing and takes the object's code off those two writes."""
    import subprocess as subprocess_module

    from bench.harness import recall as recall_module
    from bench.harness.recall import run_recall

    class _ExitingSetItem(dict):
        def __setitem__(self, key: object, value: object) -> None:
            raise SystemExit(131)

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    real_loads = json.loads
    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    monkeypatch.setattr(
        recall_module.json,
        "loads",
        lambda text, *a, **k: _ExitingSetItem(real_loads(text, *a, **k)),
    )
    scratch = tmp_path / "scratch"
    with pytest.raises(RecallStageError, match="plain JSON-native data"):
        run_recall("tiny", scratch=scratch)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], "and the fresh file is still removed"


def test_a_real_interrupt_of_work_still_propagates_with_the_temp_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for every absorption above: a KeyboardInterrupt raised by the
    SUBPROCESS -- real work, not data choosing a shape -- stays the primary, and the
    fresh verdict file is still removed. If this ever turns into a typed refusal, the
    line between work and inspection has been crossed."""
    from bench.harness.recall import run_recall

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("the operator interrupted the worker")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", interrupted)
    scratch = tmp_path / "scratch"
    with pytest.raises(KeyboardInterrupt):
        run_recall("tiny", scratch=scratch, timeout_seconds=30)
    assert list(scratch.iterdir()) == [], (
        "work may be interrupted; residue may not stay"
    )


def test_a_real_system_exit_from_os_open_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second control: os.open is real I/O, so a SystemExit raised THERE is an
    interrupt of work and must reach the caller instead of becoming a typed refusal."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_open = os.open

    def exiting_open(path: object, *args: object, **kwargs: object) -> int:
        if str(path).endswith(".c13.lock"):
            raise SystemExit(93)
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", exiting_open)
    with pytest.raises(SystemExit):
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()


def test_a_path_that_refuses_to_be_built_still_removes_the_verdict_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 (4): Path(temp_name) was the LAST statement between the descriptor
    closing and the try, and a Path that refuses to be built left the fresh file
    orphaned with no cleanup and no spawn. This probe targets that statement
    specifically -- the earlier one breaks the clock, which stays inside the region
    either way and therefore cannot tell the two versions apart. The cleanup rides on
    the builtin name precisely so the object that failed cannot make the removal
    unreachable."""
    from bench.harness import recall as recall_module
    from bench.harness.recall import run_recall

    real_path = recall_module.Path

    class _RefusingPath:
        def __new__(cls, *args: object, **kwargs: object):
            if args and "recall-tiny-" in str(args[0]):
                raise RuntimeError("this path object cannot be built")
            return real_path(*args, **kwargs)  # type: ignore[arg-type]

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", forbidden)
    monkeypatch.setattr(recall_module, "Path", _RefusingPath)
    scratch = tmp_path / "scratch"
    with pytest.raises(RuntimeError, match="cannot be built"):
        run_recall("tiny", scratch=scratch, timeout_seconds=30)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], (
        "the file is ours from the moment the descriptor closed, so it goes even when "
        "the object naming it never existed"
    )


# =====================================================================================
# Round-8: parsing is not trusting -- and a cleanup interrupt is not a stage failure
# =====================================================================================


def _adds_a_key(document: dict[str, object]) -> bool:
    """A mutation that really does change the tree, and says so."""
    document["added"] = 1
    return True


class _IgnoreSet(dict):
    """A mapping that accepts every write and keeps none of them.

    The critical shape: nothing raises, so no guard anywhere can notice. Only rebuilding
    the document into exact builtins takes this object off the path.
    """

    def __setitem__(self, key: object, value: object) -> None:
        return None


def test_a_mutation_that_cannot_land_is_refused_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 CRITICAL: _replace_json re-read a SECOND object and mutated it exactly as
    parsed. A mapping whose __setitem__ quietly does nothing made the mutation a no-op
    while every other step succeeded -- so the caller was told the write landed when the
    document had not changed at all. Nothing raised, which is why no boundary caught
    it."""
    document = tmp_path / "document.json"
    document.write_text(json.dumps({"kept": True}), encoding="utf-8")
    before = document.read_text(encoding="utf-8")
    monkeypatch.setattr(
        wiring.json, "loads", lambda *a, **kw: _IgnoreSet({"kept": True})
    )
    with pytest.raises(RecallStageError, match="JSON-native"):
        wiring._replace_json(document, _adds_a_key)
    monkeypatch.undo()
    assert document.read_text(encoding="utf-8") == before


def test_the_gauge_is_never_published_without_its_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 CRITICAL, end to end: with the section's write silently dropped, the run
    returned 0 having published the GAUGE and not the SECTION -- the exact inversion of
    the invariant this module exists to hold, reported as success. The stage must fail
    instead, and the reachable partial state stays section-without-gauge, never the
    reverse."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_loads = json.loads
    seen: list[int] = []

    def dropping_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        if not isinstance(parsed, dict):
            return parsed
        seen.append(1)
        # The two prevalidation reads and the strip's read come first; the fourth is
        # the section's read-modify-write.
        return _IgnoreSet(parsed) if len(seen) >= 4 else parsed

    monkeypatch.setattr(wiring.json, "loads", dropping_loads)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code != 0, "a publication whose write cannot land is never a success"
    published = json.loads(metrics.read_text(encoding="utf-8"))
    names = [entry.get("name") for entry in published.get("metrics", [])]
    assert RECALL_METRIC not in names, "the gauge may never appear without its section"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []
    assert [p.name for p in tmp_path.iterdir() if ".c13-" in p.name] == []


@pytest.mark.parametrize("field", ["ok", "failure"], ids=["ok-141", "failure-142"])
def test_a_verdict_value_that_exits_cannot_escape_run_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """Round-8 (A): requiring an exact dict at the top left the VALUES inside it as
    whatever the parse produced -- ok with a __bool__ that raises (141) and failure with
    a __format__ that raises (142), both reached while reporting a failure. The whole
    tree is rebuilt before any access."""
    import subprocess as subprocess_module

    from bench.harness import recall as recall_module
    from bench.harness.recall import run_recall

    class _ExitingBool:
        def __bool__(self) -> bool:
            raise SystemExit(141)

    class _ExitingFormat:
        def __format__(self, spec: str) -> str:
            raise SystemExit(142)

        def __str__(self) -> str:
            raise SystemExit(142)

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    real_loads = json.loads

    def hostile_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        parsed[field] = _ExitingBool() if field == "ok" else _ExitingFormat()
        if field == "failure":
            parsed["ok"] = False
        return parsed

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    monkeypatch.setattr(recall_module.json, "loads", hostile_loads)
    scratch = tmp_path / "scratch"
    with pytest.raises(RecallStageError, match="JSON-native"):
        run_recall("tiny", scratch=scratch)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], "and the fresh file is still removed"


def test_a_second_lock_name_that_cannot_be_built_orphans_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 (C): the lock names were built inside the acquisition loop, so the SECOND
    Path could fail after the first lock was already held -- and the failure path for a
    name that does not exist yet had nothing to release it with. Every name is built
    before anything is acquired."""
    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    real_path = wiring.Path
    built: list[int] = []

    class _RefusingSecondPath:
        def __new__(cls, *args: object, **kwargs: object):
            if args and str(args[0]).endswith(".c13.lock"):
                built.append(1)
                if len(built) == 2:
                    raise RuntimeError("the second lock name cannot be built")
            return real_path(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wiring, "Path", _RefusingSecondPath)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3
    assert not called, "nothing runs when the field could not even be named"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == [], (
        "a name that failed to build may not leave an earlier lock on disk"
    )


def test_an_existence_answer_that_is_not_a_bool_counts_as_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 (D): the guard covered the exists() CALL but not the value it returned,
    so an object whose __bool__ raises (171) was evaluated for truth outside the try.
    Only an exact bool is an answer."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    class _ExitingTruth:
        def __bool__(self) -> bool:
            raise SystemExit(171)

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    real_unlink = os.unlink

    def sticky(target: object, *args: object, **kwargs: object) -> None:
        if "recall-tiny-" in str(target):
            raise OSError("sticky temp")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    monkeypatch.setattr(os, "unlink", sticky)
    monkeypatch.setattr(os.path, "exists", lambda *a, **kw: _ExitingTruth())
    with pytest.raises(RecallStageError, match="residue"):
        run_recall("tiny", scratch=tmp_path / "scratch")
    monkeypatch.undo()


def test_a_real_interrupt_of_the_resolve_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 (E): resolve() walks the filesystem, so a KeyboardInterrupt raised THERE
    (181) is an interrupt of work. Round-7 ran it in the same clause as the argument's
    own __fspath__ and absorbed both; the argument is reduced to a builtin string first,
    and the filesystem call keeps Exception."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_resolve = Path.resolve

    def interrupted_resolve(self: Path, *args: object, **kwargs: object):
        if self.name in ("calibration.json", "metrics.json"):
            raise KeyboardInterrupt("interrupted while resolving")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", interrupted_resolve)
    with pytest.raises(KeyboardInterrupt):
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()


def test_a_cleanup_interrupt_with_no_primary_is_not_swallowed_into_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 (F, code 182): on a SUCCESSFUL publication the lock release absorbed every
    shape, so a real KeyboardInterrupt raised by the unlink syscall came back as exit 3 --
    a stage failure that did not happen, and a lost interrupt that did. With no primary
    to protect, the interrupt reappears after the cleanup completed."""
    out, metrics = _seed_documents(tmp_path)
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_unlink = os.unlink

    def interrupted_unlink(target: object, *args: object, **kwargs: object) -> None:
        if str(target).endswith(".c13.lock"):
            raise KeyboardInterrupt("interrupted releasing the lock")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", interrupted_unlink)
    with pytest.raises(KeyboardInterrupt):
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()


def test_a_temp_cleanup_interrupt_with_no_primary_is_not_a_stage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 (F, code 183): the same rule for the verdict temp. A successful run whose
    unlink is interrupted used to report RecallStageError; the interrupt is what
    happened."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    def writes_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(json.dumps(_verdict_stub()), encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    real_unlink = os.unlink

    def interrupted_unlink(target: object, *args: object, **kwargs: object) -> None:
        if "recall-tiny-" in str(target):
            raise KeyboardInterrupt("interrupted removing the verdict file")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_verdict)
    monkeypatch.setattr(os, "unlink", interrupted_unlink)
    with pytest.raises(KeyboardInterrupt):
        run_recall("tiny", scratch=tmp_path / "scratch")
    monkeypatch.undo()


def test_a_cleanup_interrupt_never_replaces_an_existing_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for (F), and the reason the rule is conditional rather than absolute:
    when a primary IS propagating, an interrupt raised by the cleanup must still be
    dropped. Here the worker fails with SystemExit and the lock release is interrupted;
    the caller must see the worker's SystemExit, not the cleanup's KeyboardInterrupt."""
    out, metrics = _seed_documents(tmp_path)

    def failing_worker(*args: object, **kwargs: object) -> dict[str, object]:
        raise SystemExit("PRIMARY")

    monkeypatch.setattr(wiring, "run_recall", failing_worker)
    real_unlink = os.unlink

    def interrupted_unlink(target: object, *args: object, **kwargs: object) -> None:
        if str(target).endswith(".c13.lock"):
            raise KeyboardInterrupt("CLEANUP")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", interrupted_unlink)
    with pytest.raises(SystemExit) as caught:
        append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    monkeypatch.undo()
    assert "PRIMARY" in str(caught.value)


# =====================================================================================
# Round-9: a construction outside the boundary, and a tree too deep to walk
# =====================================================================================


def _deep_json(depth: int) -> str:
    """A type-exact JSON tree deep enough to exhaust the rebuild's recursion."""
    return "[" * depth + "1" + "]" * depth


def test_a_path_that_refuses_its_second_construction_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (1): `canonical = Path(canonical_name)` sat outside every boundary. A Path
    replacement that allows the first construction and the resolve, then refuses the
    second, escaped as a raw RuntimeError instead of the typed exit 3 -- from a function
    whose contract is an exit code."""
    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    real_path = wiring.Path
    built: list[int] = []

    class _RefusingSecondConstruction:
        def __new__(cls, *args: object, **kwargs: object):
            if args and str(args[0]).endswith(".json"):
                built.append(1)
                if len(built) == 2:
                    raise RuntimeError("this path cannot be constructed twice")
            return real_path(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wiring, "Path", _RefusingSecondConstruction)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "an ordinary failure building our own Path is a typed refusal"
    assert not called
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_tree_too_deep_to_rebuild_is_refused_not_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (2): the rebuild is RECURSIVE, so a type-exact tree deeper than the
    interpreter's limit raises RecursionError from inside it -- a shape the
    _NotCanonical clause never saw. Every public boundary that canonicalizes must still
    answer with its own verdict."""
    from bench.harness.recall import _UNCANONICAL, _canonical_json

    deep = json.loads(_deep_json(200))
    for _ in range(40):
        deep = [deep]
    monkeypatch.setattr(sys, "setrecursionlimit", sys.setrecursionlimit)
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(120)
    try:
        rebuilt = _canonical_json(deep)
    finally:
        sys.setrecursionlimit(original_limit)
    assert rebuilt is _UNCANONICAL, "a tree too deep to walk cannot be rebuilt"


def test_a_deeply_nested_verdict_fails_the_stage_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (2), run_recall's boundary: the canonicalization sat outside the parse's
    try, so anything it raised left this function without the RecallStageError its
    callers are promised -- and the fresh verdict file still has to go."""
    import subprocess as subprocess_module

    from bench.harness.recall import run_recall

    def writes_deep_verdict(command: list[str], **kwargs: object):
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text('{"ok": ' + _deep_json(400) + "}", encoding="utf-8")
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harness.recall.subprocess.run", writes_deep_verdict)
    scratch = tmp_path / "scratch"
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        with pytest.raises(RecallStageError):
            run_recall("tiny", scratch=scratch)
    finally:
        sys.setrecursionlimit(original_limit)
    monkeypatch.undo()
    assert list(scratch.iterdir()) == [], "and the fresh file is still removed"


def test_a_deeply_nested_document_refuses_the_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (2), the wiring's boundaries: a document too deep to rebuild cannot be
    read for a stale gauge nor mutated, so the stage refuses typed with both documents
    untouched and no locks left behind."""
    out, metrics = _seed_documents(tmp_path)
    out.write_text('{"ceilings": ' + _deep_json(400) + "}", encoding="utf-8")
    before = (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8"))
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        code = append_vector_recall(
            profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
        )
    finally:
        sys.setrecursionlimit(original_limit)
    monkeypatch.undo()
    assert code == 3
    assert (out.read_text(encoding="utf-8"), metrics.read_text(encoding="utf-8")) == (
        before
    )
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_strip_snapshot_of_the_wrong_shape_never_leaves_two_gauges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (4): the strip canonicalized but never required the TOPOLOGY it then
    inspects. A type-exact `[]` rebuilds perfectly and reads as "no gauge here", so the
    strip silently did nothing and the run published a new gauge beside the stale one --
    two gauges, and the invariant that every crash window reads as gauge-ABSENT quietly
    gone. The prevalidation's agreement on an EARLIER read is not evidence about this
    one."""
    out, metrics = _seed_documents(tmp_path)
    stale = json.loads(metrics.read_text(encoding="utf-8"))
    stale["metrics"].append(
        {
            "name": RECALL_METRIC,
            "kind": "gauge",
            "unit": "ratio",
            "samples": [{"value": 0.10}],
        }
    )
    metrics.write_text(json.dumps(stale), encoding="utf-8")
    before = metrics.read_text(encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_loads = json.loads
    seen: list[int] = []

    def wrong_shape_on_the_strip(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        if not isinstance(parsed, dict):
            return parsed
        seen.append(1)
        # The two prevalidation reads pass; the strip's own read gets a list.
        return [] if len(seen) == 3 else parsed

    monkeypatch.setattr(wiring.json, "loads", wrong_shape_on_the_strip)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "a strip that cannot see the document may not publish over it"
    assert metrics.read_text(encoding="utf-8") == before
    published = json.loads(metrics.read_text(encoding="utf-8"))
    gauges = [e for e in published["metrics"] if e.get("name") == RECALL_METRIC]
    assert len(gauges) == 1 and gauges[0]["samples"][0]["value"] == 0.10, (
        "the stale gauge is still the only one: no second gauge was appended beside it"
    )
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_resolve_result_whose_fspath_exits_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (5): resolve() is work and a real interrupt of it must propagate, but the
    object it RETURNS is not ours -- os.fspath on that result runs the returned object's
    __fspath__. Sharing one clause let a SystemExit from there escape as though the
    filesystem call had been interrupted."""
    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )

    class _ExitingFspath:
        def __fspath__(self) -> str:
            raise SystemExit(191)

    real_resolve = Path.resolve

    def resolve_to_hostile(self: Path, *args: object, **kwargs: object) -> object:
        if self.name in ("calibration.json", "metrics.json"):
            return _ExitingFspath()
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_to_hostile)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "normalizing a hostile result is a refusal, not an exit"
    assert not called


def test_an_established_refusal_survives_an_interrupted_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 (6): a non-zero outcome IS a primary -- the stage already decided, in a
    typed refusal the caller is entitled to. Re-raising a cleanup interrupt over it
    replaced that verdict with an interrupt raised by the cleanup OF it, which is the
    round-8 mistake in the opposite direction. Only a SUCCESSFUL publication leaves the
    interrupt as the sole event."""
    out, metrics = _seed_documents(tmp_path)

    def refusing_worker(*args: object, **kwargs: object) -> dict[str, object]:
        raise RecallStageError("the worker refused")

    monkeypatch.setattr(wiring, "run_recall", refusing_worker)
    real_unlink = os.unlink

    def interrupted_unlink(target: object, *args: object, **kwargs: object) -> None:
        if str(target).endswith(".c13.lock"):
            raise KeyboardInterrupt("interrupted releasing the lock")
        return real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", interrupted_unlink)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "the stage's own refusal stands; the cleanup's interrupt does not"


def test_a_stat_result_whose_link_count_exits_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 (B, code 194): the syscall is work and keeps its interrupts, but what it
    RETURNED is not ours. Reading st_nlink and comparing it runs the returned object's
    code, and an st_nlink whose __gt__ raises escaped with no lock taken."""

    class _ExitingCount:
        def __gt__(self, other: object) -> bool:
            raise SystemExit(194)

    class _HostileStat:
        st_nlink = _ExitingCount()

    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    monkeypatch.setattr(os, "stat", lambda *a, **kw: _HostileStat())
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3
    assert not called
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".c13.lock")] == []


def test_a_hostile_second_path_never_reaches_the_identity_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 (B, code 193): a factory answering with a real Path first and an object
    whose __eq__ raises second escaped through the out==metrics comparison, with no lock
    ever taken. Identity now lives as a builtin str and is compared as one, and a Path is
    only accepted when it reduces back to the exact name it was built from."""

    class _HostileEquality:
        def __init__(self, name: str) -> None:
            self._name = name

        def __eq__(self, other: object) -> bool:
            raise SystemExit(193)

        def __hash__(self) -> int:
            return 0

        def __fspath__(self) -> str:
            return self._name

    out, metrics = _seed_documents(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        wiring, "run_recall", lambda *a, **kw: called.append(1) or _verdict_stub()
    )
    real_path = wiring.Path
    built: dict[str, int] = {}

    def alternating(*args: object, **kwargs: object):
        if args and str(args[0]).endswith(".json"):
            key = str(args[0])
            built[key] = built.get(key, 0) + 1
            if built[key] == 2:
                return _HostileEquality(key)
        return real_path(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wiring, "Path", alternating)
    code = append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    assert code == 3, "a path object that is not the path we asked for is refused"
    assert not called


def test_a_divergent_reread_inside_the_replace_cannot_erase_the_legacy_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 (A), the worst of the four: the strip decided on one read and
    _replace_json wrote from ANOTHER, so a second read answering `{}` published that
    empty tree over the real one -- destroying oktografx_baseline_ceiling_multiple with
    exit 0, in a module whose whole premise is that legacy documents survive byte for
    byte. One read now decides and acts, so there is no second tree to publish."""
    out, metrics = _seed_documents(tmp_path)
    document = json.loads(metrics.read_text(encoding="utf-8"))
    document["metrics"].append(
        {
            "name": RECALL_METRIC,
            "kind": "gauge",
            "unit": "ratio",
            "samples": [{"value": 0.10}],
        }
    )
    metrics.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(wiring, "run_recall", lambda *a, **kw: _verdict_stub())
    real_loads = json.loads
    seen: list[int] = []

    def empty_after_the_prevalidations(text: str, *a: object, **kw: object) -> object:
        parsed = real_loads(text, *a, **kw)
        if not isinstance(parsed, dict):
            return parsed
        seen.append(1)
        return {} if len(seen) >= 3 else parsed

    monkeypatch.setattr(wiring.json, "loads", empty_after_the_prevalidations)
    append_vector_recall(
        profile="tiny", gt_mode="auto", out=out, metrics=metrics, workspace=tmp_path
    )
    monkeypatch.undo()
    published = json.loads(metrics.read_text(encoding="utf-8"))
    names = [entry.get("name") for entry in published.get("metrics", [])]
    assert "oktografx_baseline_ceiling_multiple" in names, (
        "a legacy metric may never be erased by a publication that only adds"
    )


def test_a_strip_with_nothing_to_remove_does_not_touch_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-10 (A): the no-op must stay a real no-op now that the decision moved inside
    the read-modify-write -- same bytes, same mtime, no format churn. The declaration is
    the only way not to write, so this pins that the declaration is made correctly."""
    out, metrics = _seed_documents(tmp_path)
    before_bytes = metrics.read_bytes()
    before_mtime = metrics.stat().st_mtime_ns
    wiring._strip_stale_gauge(metrics)
    assert metrics.read_bytes() == before_bytes
    assert metrics.stat().st_mtime_ns == before_mtime


def test_a_mutation_that_does_not_declare_its_outcome_is_refused(
    tmp_path: Path,
) -> None:
    """Round-10 (A): a mutation that merely happens to leave the tree alone must not
    lead to a write either -- the bytes would come from a snapshot nobody inspected,
    which is the same defect one level down. The declaration is mandatory."""
    document = tmp_path / "document.json"
    document.write_text(json.dumps({"kept": True}), encoding="utf-8")
    before = document.read_bytes()
    with pytest.raises(RecallStageError, match="declare"):
        wiring._replace_json(document, lambda doc: None)
    assert document.read_bytes() == before
