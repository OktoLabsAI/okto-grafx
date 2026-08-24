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
    real_unlink = Path.unlink

    def sticky(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("recall-tiny-"):
            raise OSError("sticky temp")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", sticky)
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

    def unserializable(payload: dict[str, object]) -> None:
        payload["bad"] = object()

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
    real_unlink, real_exists = Path.unlink, Path.exists

    def sticky(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("recall-tiny-"):
            raise OSError("sticky temp")
        return real_unlink(self, *args, **kwargs)

    def unanswerable(self: Path) -> bool:
        if self.name.startswith("recall-tiny-"):
            raise OSError("the existence of this path cannot be determined")
        return real_exists(self)

    monkeypatch.setattr(Path, "unlink", sticky)
    monkeypatch.setattr(Path, "exists", unanswerable)
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
    real_unlink, real_exists = Path.unlink, Path.exists

    def sticky(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith("recall-tiny-"):
            raise OSError("sticky temp")
        return real_unlink(self, *args, **kwargs)

    def exiting_exists(self: Path) -> bool:
        if self.name.startswith("recall-tiny-"):
            raise SystemExit(0)
        return real_exists(self)

    monkeypatch.setattr(Path, "unlink", sticky)
    monkeypatch.setattr(Path, "exists", exiting_exists)
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
