"""Parent side of the C13 recall stage: spawn the worker, shape the frozen section (v4 §2-3).

This module owns process isolation and document shaping, never the mathematics: it launches
:mod:`bench.harness.recall_worker` in a FRESH interpreter with the BLAS thread variables set in
the child environment before Python starts (the only placement that cannot arrive after a numpy
import), reads the worker's JSON verdict back from a temporary file, and turns it into the
``vector_recall`` calibration section — ``frozen`` (bit-comparable), ``observed`` per family
(deterministic same-machine), ``provenance`` per family (everything volatile, outside every
hash). Publication order — section first, gauge last — belongs to the harness wiring, which
calls :func:`run_recall` and appends; nothing here writes calibration or metrics documents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bench.harness.recall_worker import (
    ACCEL_EPS_REL,
    CORPUS_SEED,
    DIFFERENTIAL_QUERIES,
    DIFFERENTIAL_SLICE,
    DTYPE_MEAN_OVERLAP_MIN,
    DTYPE_PER_QUERY_OVERLAP_MIN,
    HNSW_FROZEN,
    PROFILES,
    QUERY_SEED,
    _BLAS_THREAD_VARIABLES,
)

RECALL_METRIC: str = "oktografx_vector_recall_ratio"
"""The SPEC-VEC FR-8 gauge the gate reads; published LAST by the harness wiring."""

DEFAULT_TARGET: float = 0.90
"""The frozen recall floor; the anti-drift test pins it equal to the gate's default."""

REGIME_THRESHOLD: int = 4096
"""``vector_exact_scan_threshold`` at the frozen SHA; smoke and full stay above it."""


class RecallStageError(RuntimeError):
    """A fail-closed recall outcome: the caller publishes nothing and exits non-zero."""


def family() -> str:
    """Return the calibration family of this platform, matching the CI matrix."""
    return "windows" if os.name == "nt" else "posix"


PROFILE_TIMEOUTS: dict[str, float] = {"tiny": 300.0, "smoke": 1800.0, "full": 9600.0}
"""Wall-clock ceilings per profile, measured rather than guessed: the full HNSW build
exceeded 3000s locally on a machine faster than the CI runners, so its inner ceiling is
160 minutes -- under the scheduled job's 180-minute budget with room for setup and the
gate -- while smoke keeps the original 30 and tiny stays test-sized. ``run_recall``
resolves these when the caller passes no explicit timeout; an explicit value always wins."""


def run_recall(
    profile: str,
    *,
    gt_mode: str = "auto",
    scratch: Path,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """Run the worker subprocess for one profile and return the parsed verdict.

    The child environment carries every BLAS thread variable pinned to ``1`` BEFORE the
    interpreter starts; a fresh process cannot have imported numpy earlier, so the pin can
    never be late. A worker that exits non-zero, times out, or writes no verdict raises
    :class:`RecallStageError` — the harness then publishes nothing vectorial and fails.
    """
    if profile not in PROFILES:
        raise RecallStageError(
            f"unknown recall profile {profile!r}; use one of {sorted(PROFILES)}"
        )
    if timeout_seconds is None:
        timeout_seconds = PROFILE_TIMEOUTS[profile]
    scratch.mkdir(parents=True, exist_ok=True)
    verdict_path = scratch / f"recall-{profile}.json"
    environment = dict(os.environ)
    for name in _BLAS_THREAD_VARIABLES:
        environment[name] = "1"
    command = [
        sys.executable,
        "-m",
        "bench.harness.recall_worker",
        "--profile",
        profile,
        "--gt",
        gt_mode,
        "--out",
        str(verdict_path),
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            env=environment,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as failure:
        raise RecallStageError(
            f"the recall worker exceeded {timeout_seconds:g}s on profile {profile!r}; "
            "nothing was published."
        ) from failure
    duration = time.monotonic() - started
    if not verdict_path.exists():
        raise RecallStageError(
            f"the recall worker wrote no verdict (exit {completed.returncode}); "
            f"stdout: {completed.stdout[-400:]!r} stderr: {completed.stderr[-400:]!r}"
        )
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    verdict["duration_seconds"] = duration
    verdict["exit_code"] = completed.returncode
    if completed.returncode != 0 or not verdict.get("ok", False):
        raise RecallStageError(
            f"recall stage failed closed on profile {profile!r}: "
            f"{verdict.get('failure', f'worker exit {completed.returncode}')}"
        )
    return verdict


def build_section(
    verdict: dict[str, object], *, target: float = DEFAULT_TARGET
) -> dict[str, object]:
    """Shape one worker verdict into the ``vector_recall`` calibration section (v4 §2).

    ``frozen`` and ``observed.<family>`` are the deterministic projection two same-machine
    runs must reproduce identically; ``provenance.<family>`` holds every volatile value —
    duration, versions, the oracle path actually taken — outside every hash and comparison.
    """
    home = family()
    hashes = dict(verdict["hashes"])  # type: ignore[arg-type]
    observed = dict(verdict["observed"])  # type: ignore[arg-type]
    return {
        "schema_version": 1,
        "frozen": {
            "target": target,
            "gate_required": True,
            "k": verdict["k"],
            "queries": verdict["queries"],
            "metric": "cosine",
            "storage_dtype": "float32",
            "dimension": verdict["dimension"],
            "regime_threshold": REGIME_THRESHOLD,
            "corpus": {
                "generator": verdict["generator"],
                "size": verdict["corpus_size"],
                "seed": CORPUS_SEED,
                "sha256_f64": hashes["corpus_sha256_f64"],
                "sha256_f32": hashes["corpus_sha256_f32"],
            },
            "query_set": {
                "held_out": True,
                "seed": QUERY_SEED,
                "sha256_f64": hashes["query_sha256_f64"],
                "sha256_f32": hashes["query_sha256_f32"],
            },
            "hnsw": dict(HNSW_FROZEN),
            "ties": "generous: GT admits every record at or under the k-th distance",
            "gt": {
                "canonical": "pure-python math.fsum",
                "oracle": verdict["oracle"],
                "differential": {
                    "queries": DIFFERENTIAL_QUERIES,
                    "corpus_slice": DIFFERENTIAL_SLICE,
                    "selection": "deterministic by seed",
                },
                "accel_equivalence_eps_rel": ACCEL_EPS_REL,
            },
            "dtype_check": {
                "mean_overlap_min": DTYPE_MEAN_OVERLAP_MIN,
                "per_query_overlap_min": DTYPE_PER_QUERY_OVERLAP_MIN,
            },
        },
        "observed": {home: observed},
        "provenance": {
            home: {
                "profile": verdict["profile"],
                "gt_path_used": verdict["gt_path_used"],
                "numpy": verdict.get("numpy", "absent"),
                "python": sys.version.split()[0],
                "duration_seconds": verdict["duration_seconds"],
                "blas_environment": verdict.get("blas_environment", {}),
            }
        },
    }


def deterministic_projection(section: dict[str, object]) -> str:
    """Serialize ``frozen`` plus ``observed`` canonically — the equality two runs must hold."""
    projection = {"frozen": section["frozen"], "observed": section["observed"]}
    return json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
