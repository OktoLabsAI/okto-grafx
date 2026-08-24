"""The recall worker measures a real engine deterministically and fails closed (C13 v4 §3).

What these tests pin:

* the frozen profile table (smoke pure-fsum above the regime threshold; full numpy oracle with
  the 16x1024 differential; tiny as the tests-only miniature below the threshold);
* an end-to-end in-process run on the tiny profile: verdict ok, recall and dtype floors, and
  bit-stable determinism across two runs;
* every fail-closed door: full+pure refuses (the pure pass v4 declares infeasible), a missing
  numpy refuses with a diagnosis, a violated dtype threshold refuses publishing nothing;
* the REAL subprocess path: fresh interpreter, BLAS thread variables pinned to "1" in the
  child, verdict written and parsed, unknown profiles refused;
* the section shape of v4 §2: frozen/observed-per-family/provenance, with the deterministic
  projection excluding provenance so volatile values can never break same-machine equality;
* the worker CLI: exit 0 with a verdict file on success, non-zero on a fail-closed outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bench.harness.recall_worker as worker
from bench.harness.recall import (
    DEFAULT_TARGET,
    RECALL_METRIC,
    REGIME_THRESHOLD,
    RecallStageError,
    build_section,
    deterministic_projection,
    family,
    run_recall,
)
from bench.harness.recall_worker import PROFILES, main, run_profile


def _projection_of(verdict: dict[str, object]) -> str:
    """Serialize the deterministic slice of a worker verdict."""
    return json.dumps(
        {
            "gauge": verdict["gauge"],
            "observed": verdict["observed"],
            "hashes": verdict["hashes"],
        },
        sort_keys=True,
    )


def test_the_profile_table_is_frozen_exactly_as_v4_states() -> None:
    """Sizes, oracles and the regime relationship are the spec, not a convenience."""
    smoke = PROFILES["smoke"]
    full = PROFILES["full"]
    tiny = PROFILES["tiny"]
    assert (smoke.corpus_size, smoke.queries, smoke.dimension, smoke.k) == (
        5120,
        64,
        128,
        10,
    )
    assert smoke.oracle == "pure-fsum"
    assert (full.corpus_size, full.queries, full.dimension, full.k) == (
        8192,
        256,
        384,
        10,
    )
    assert full.oracle.startswith("numpy-bruteforce-v1")
    assert "16x1024" in full.oracle
    assert smoke.corpus_size > REGIME_THRESHOLD
    assert full.corpus_size > REGIME_THRESHOLD
    assert tiny.corpus_size < REGIME_THRESHOLD, "tiny is for tests and freezes nothing"


def test_the_tiny_profile_measures_a_real_engine_and_is_deterministic() -> None:
    """End to end in process: real HNSW inserts and searches, floors held, runs identical."""
    first = run_profile("tiny")
    assert first["ok"] is True
    assert first["gt_path_used"] == "pure"
    assert first["oracle"] == "pure-fsum"
    assert 0.9 <= first["gauge"] <= 1.0
    observed = first["observed"]
    assert observed["dtype_check"]["mean_overlap"] >= 0.99
    assert "queries_below_perfect" in observed, "the count is of non-perfect queries"
    assert "queries_below_target" not in observed, "the lying name must be gone"
    assert set(first["hashes"]) == {
        "corpus_sha256_f64",
        "corpus_sha256_f32",
        "query_sha256_f64",
        "query_sha256_f32",
    }
    second = run_profile("tiny")
    assert _projection_of(first) == _projection_of(second)


def test_the_full_profile_refuses_a_pure_oracle_as_infeasible() -> None:
    """v4 declares the 805M-op pure pass infeasible; asking for it refuses, publishing nothing."""
    verdict = run_profile("full", gt_mode="pure")
    assert verdict["ok"] is False
    assert "smoke" in verdict["failure"]


def test_a_missing_numpy_fails_closed_with_a_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full profile without its oracle refuses; it never degrades silently."""

    def absent(corpus: list[list[float]], k: int) -> tuple[bool, str]:
        raise ModuleNotFoundError("No module named 'numpy'")

    monkeypatch.setattr(worker, "_differential", absent)
    verdict = run_profile("full")
    assert verdict["ok"] is False
    assert "numpy" in verdict["failure"]
    assert verdict["gt_path_used"] == "none"


def test_a_violated_dtype_threshold_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic impossible thresholds prove the TR-4 door: ok=False, nothing measured further."""
    monkeypatch.setattr(worker, "DTYPE_MEAN_OVERLAP_MIN", 1.1)
    verdict = run_profile("tiny")
    assert verdict["ok"] is False
    assert "dtype" in verdict["failure"]


def test_the_real_subprocess_pins_blas_threads_and_returns_the_verdict(
    tmp_path: Path,
) -> None:
    """The parent path: fresh interpreter, env pinned before start, verdict parsed."""
    verdict = run_recall("tiny", scratch=tmp_path)
    assert verdict["ok"] is True
    assert verdict["exit_code"] == 0
    assert verdict["duration_seconds"] > 0.0
    assert verdict["blas_environment"] == {
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def test_an_unknown_profile_refuses_before_any_subprocess(tmp_path: Path) -> None:
    """The stage error names the valid profiles instead of spawning anything."""
    with pytest.raises(RecallStageError, match="unknown recall profile"):
        run_recall("gigantic", scratch=tmp_path)


def test_the_section_shape_matches_v4_and_the_projection_excludes_provenance(
    tmp_path: Path,
) -> None:
    """frozen/observed/provenance split; volatile values cannot break the projection."""
    verdict = run_recall("tiny", scratch=tmp_path)
    section = build_section(verdict)
    assert section["schema_version"] == 1
    frozen = section["frozen"]
    assert frozen["target"] == DEFAULT_TARGET
    assert frozen["regime_threshold"] == REGIME_THRESHOLD
    assert frozen["corpus"]["generator"] == "uniform-int53-v1"
    assert frozen["corpus"]["sha256_f64"] != frozen["corpus"]["sha256_f32"]
    assert frozen["hnsw"]["index_seed"] == "0x0C701A11F0C0FFEE"
    assert frozen["gt"]["canonical"] == "pure-python math.fsum"
    assert frozen["dtype_check"] == {
        "mean_overlap_min": 0.99,
        "per_query_overlap_min": 0.90,
    }
    home = family()
    assert set(section["observed"]) == {home}
    assert set(section["provenance"]) == {home}
    assert "duration_seconds" in section["provenance"][home]
    twin = build_section({**verdict, "duration_seconds": 999.0})
    assert deterministic_projection(section) == deterministic_projection(twin)
    assert RECALL_METRIC == "oktografx_vector_recall_ratio"


def test_the_worker_cli_writes_the_verdict_and_exits_by_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() is the subprocess contract: 0 with a verdict on success, 3 on fail-closed."""
    out = tmp_path / "verdict.json"
    assert main(["--profile", "tiny", "--out", str(out)]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["ok"] is True
    monkeypatch.setattr(worker, "DTYPE_MEAN_OVERLAP_MIN", 1.1)
    failed = tmp_path / "failed.json"
    assert main(["--profile", "tiny", "--out", str(failed)]) == 3
    diagnostic = json.loads(failed.read_text(encoding="utf-8"))
    assert diagnostic["ok"] is False


def test_the_worker_overlap_really_delegates_to_the_shared_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spy replaces generous_overlap in the worker namespace; _overlap must call it."""
    sentinel = 0.4242

    def spy(pre: object, post: object, k: int) -> float:
        return sentinel

    monkeypatch.setattr(worker, "generous_overlap", spy)
    assert worker._overlap(object(), object(), 3) == sentinel
