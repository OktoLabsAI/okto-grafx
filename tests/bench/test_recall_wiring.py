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
