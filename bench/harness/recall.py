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
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bench.recall_corpus import (
    generate_vectors,
    sha256_hex,
    vector_bytes_f32,
    vector_bytes_f64,
)
from bench.harness.recall_worker import (
    ACCEL_EPS_REL,
    CORPUS_SEED,
    DIFFERENTIAL_QUERIES,
    DIFFERENTIAL_SLICE,
    DTYPE_MEAN_OVERLAP_MIN,
    DTYPE_PER_QUERY_OVERLAP_MIN,
    GENERATOR_NAME,
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
"""Wall-clock ceilings per profile, measured rather than guessed. The full profile run
took ~53 minutes end to end on the freeze machine (faster than the CI runners), so its
ceiling is 160 minutes -- whole-run, with margin, under the scheduled job's 180-minute
budget. What IS separately measured about the phases: a 2048x384 build takes ~3
seconds, and per-query searches at the frozen ef visit most of that graph (the ACORN
pruning weakens while the result set stays unfilled). The phase breakdown of the full
8192x384 run was NOT instrumented separately, so no phase is blamed here -- the
ceiling simply covers all of them. Smoke keeps the original 30 minutes and tiny stays
test-sized. ``run_recall`` resolves these when the caller passes no explicit timeout;
an explicit value always wins after validation."""


def _describe(value: object) -> str:
    """repr(), guarded and bounded: ``repr(10**10000)`` raises past CPython's digit
    limit, and the refusal message must not crash the refusal (mirrors gate._describe).
    """
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 -- a hostile __repr__ may raise anything ordinary
        return f"<{type(value).__name__} whose repr raises>"
    return text if len(text) <= 80 else text[:77] + "..."


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
    invalid = isinstance(timeout_seconds, bool) or type(timeout_seconds) not in (
        int,
        float,
    )
    if not invalid:
        try:
            # float(10**10000) raises OverflowError BEFORE isfinite could refuse it, so
            # the conversion itself is guarded -- the reaudit's huge-integer probe.
            as_float = float(timeout_seconds)
        except OverflowError:
            invalid = True
        else:
            invalid = not math.isfinite(as_float) or not as_float > 0.0
    if invalid:
        raise RecallStageError(
            "timeout_seconds must be a finite positive number; "
            f"got {_describe(timeout_seconds)} "
            "-- refused before any directory or process exists."
        )
    timeout_seconds = as_float
    scratch.mkdir(parents=True, exist_ok=True)
    # Reaudit HIGH-2: a DETERMINISTIC verdict path let a worker that exited 0 without
    # writing hand back a PREVIOUS run's file as this run's result. Every run now gets a
    # unique fresh file (mkstemp; the handle closes at once so the Windows child can open
    # it), an empty fresh file is a typed refusal -- absence is never acceptance -- and
    # the cleanup in the finally below is outcome-neutral.
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f"recall-{profile}-", suffix=".json", dir=str(scratch)
    )
    os.close(descriptor)
    verdict_path = Path(temp_name)
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
                f"the recall worker exceeded {timeout_seconds:g}s on profile "
                f"{profile!r}; nothing was published."
            ) from failure
        duration = time.monotonic() - started
        try:
            raw = verdict_path.read_text(encoding="utf-8")
        except OSError as failure:
            raise RecallStageError(
                f"the recall worker left no readable verdict "
                f"(exit {completed.returncode}); stdout: {completed.stdout[-400:]!r} "
                f"stderr: {completed.stderr[-400:]!r}"
            ) from failure
        if not raw.strip():
            raise RecallStageError(
                "the recall worker wrote nothing into its fresh per-run file "
                f"(exit {completed.returncode}); a stale file can never be mistaken "
                f"for this run's result. stdout: {completed.stdout[-400:]!r} "
                f"stderr: {completed.stderr[-400:]!r}"
            )
        try:
            verdict = json.loads(raw)
        except (ValueError, RecursionError) as failure:
            raise RecallStageError(
                f"the recall worker's verdict is not readable JSON "
                f"(exit {completed.returncode}): {failure}"
            ) from failure
        if not isinstance(verdict, dict):
            raise RecallStageError(
                "the recall worker's verdict is not a JSON object; refusing it."
            )
    finally:
        try:
            verdict_path.unlink()
        except OSError:
            pass
    verdict["duration_seconds"] = duration
    verdict["exit_code"] = completed.returncode
    if completed.returncode != 0 or not verdict.get("ok", False):
        raise RecallStageError(
            f"recall stage failed closed on profile {profile!r}: "
            f"{verdict.get('failure', f'worker exit {completed.returncode}')}"
        )
    return verdict


_EXPECTED_HASHES: dict[str, dict[str, str]] = {}


def _expected_hashes(profile) -> dict[str, str]:
    """The recomputed corpus/query digests for one profile, cached per process.

    Pre-review (b): 64 well-formed hex characters are not IDENTITY -- four zeroed
    digests still froze. The only honest comparison is a fresh recomputation from the
    frozen recipe, the same recomputation the freeze test pins.
    """
    cached = _EXPECTED_HASHES.get(profile.name)
    if cached is None:
        corpus = generate_vectors(CORPUS_SEED, profile.corpus_size, profile.dimension)
        queries = generate_vectors(QUERY_SEED, profile.queries, profile.dimension)
        cached = {
            "corpus_sha256_f64": sha256_hex(vector_bytes_f64(corpus)),
            "corpus_sha256_f32": sha256_hex(vector_bytes_f32(corpus)),
            "query_sha256_f64": sha256_hex(vector_bytes_f64(queries)),
            "query_sha256_f32": sha256_hex(vector_bytes_f32(queries)),
        }
        _EXPECTED_HASHES[profile.name] = cached
    return cached


def _validate_verdict(verdict: dict[str, object], profile_name: str) -> str | None:
    """The reason this verdict cannot be published, or None when it is fully coherent.

    Reaudit HIGH-1: the wiring validated only the gauge, so an adulterated verdict --
    gauge 0.99 beside observed 0.01, a foreign hnsw block -- was published while
    ``build_section`` stamped the FROZEN constants over whatever the verdict claimed.
    Everything the section will freeze or the gate will read is therefore checked against
    the declared contract here, and a contradiction is REFUSED, never normalized.
    """
    profile = PROFILES.get(profile_name)
    if profile is None:
        return f"unknown profile {profile_name!r}"
    if verdict.get("ok") is not True:
        return "ok is not True"
    if verdict.get("profile") != profile_name:
        return f"profile {_describe(verdict.get('profile'))} is not {profile_name!r}"
    if verdict.get("generator") != GENERATOR_NAME:
        return (
            f"generator {_describe(verdict.get('generator'))} is not {GENERATOR_NAME!r}"
        )
    if verdict.get("failure") != "":
        return "failure is not the empty string on a success verdict"
    gt_path = verdict.get("gt_path_used")
    oracle = verdict.get("oracle")
    if gt_path not in ("numpy", "pure"):
        return f"gt_path_used {_describe(gt_path)} is neither 'numpy' nor 'pure'"
    if gt_path == "numpy":
        if not profile.oracle.startswith("numpy"):
            return "a pure-oracle profile cannot claim the numpy GT path"
        if oracle != profile.oracle:
            return (
                f"oracle {_describe(oracle)} does not match the profile's declaration"
            )
    if gt_path == "pure":
        if profile.oracle.startswith("numpy"):
            return "a numpy-oracle profile cannot have taken the pure GT path"
        if oracle != "pure-fsum":
            return f"oracle {_describe(oracle)} does not match the pure GT path"
    numpy_field = verdict.get("numpy")
    if gt_path == "numpy":
        if (
            not isinstance(numpy_field, str)
            or not numpy_field
            or numpy_field == "absent"
        ):
            return "the numpy GT path requires a real numpy version string"
    elif numpy_field != "absent":
        return "the pure GT path must record numpy as 'absent'"
    for field, expected in (
        ("k", profile.k),
        ("queries", profile.queries),
        ("corpus_size", profile.corpus_size),
        ("dimension", profile.dimension),
    ):
        value = verdict.get(field)
        # type(value) is int: float equality (10.0 == 10) is a MUTATION, not the profile.
        if isinstance(value, bool) or type(value) is not int or value != expected:
            return f"{field} {_describe(value)} is not the profile's exact {expected}"
    hashes = verdict.get("hashes")
    declared_hashes = {
        "corpus_sha256_f64",
        "corpus_sha256_f32",
        "query_sha256_f64",
        "query_sha256_f32",
    }
    if not isinstance(hashes, dict) or set(hashes) != declared_hashes:
        return "hashes are not exactly the four declared digests"
    for key, digest in hashes.items():
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(ch in "0123456789abcdef" for ch in digest)
        ):
            return f"{key} is not a 64-character lowercase hex digest"
    if hashes != _expected_hashes(profile):
        return "hashes do not equal the profile's recomputed corpus/query digests"
    hnsw = verdict.get("hnsw")
    if not isinstance(hnsw, dict) or set(hnsw) != set(HNSW_FROZEN):
        return "hnsw does not carry exactly the frozen parameter names"
    for key, frozen in HNSW_FROZEN.items():
        value = hnsw[key]
        # Type-exact: dict equality accepts 16.0 == 16, and a float that merely equals
        # the frozen integer is a mutation the section must never launder.
        if type(value) is not type(frozen) or value != frozen:
            return f"hnsw.{key} {_describe(value)} is not exactly the frozen {frozen!r}"
    observed = verdict.get("observed")
    declared_observed = {
        "mean_recall_at_k",
        "min_recall_at_k",
        "queries_below_perfect",
        "dtype_check",
    }
    if not isinstance(observed, dict) or set(observed) != declared_observed:
        return "observed is not exactly its declared keys"
    mean = observed["mean_recall_at_k"]
    minimum = observed["min_recall_at_k"]
    for label, value in (("mean_recall_at_k", mean), ("min_recall_at_k", minimum)):
        if (
            isinstance(value, bool)
            or type(value) is not float
            or not math.isfinite(value)
        ):
            return f"observed.{label} {_describe(value)} is not a finite float"
        if not 0.0 <= value <= 1.0:
            return f"observed.{label} {_describe(value)} is outside [0, 1]"
    if minimum > mean:
        return "observed.min_recall_at_k exceeds the mean"
    below = observed["queries_below_perfect"]
    if (
        isinstance(below, bool)
        or type(below) is not int
        or not 0 <= below <= profile.queries
    ):
        return (
            f"observed.queries_below_perfect {_describe(below)} is not an int "
            "within the query count"
        )
    if (below == 0) != (minimum >= 1.0):
        return "queries_below_perfect and min_recall_at_k contradict each other"
    if (below == 0) != (mean >= 1.0):
        return "queries_below_perfect and mean_recall_at_k contradict each other"
    dtype = observed["dtype_check"]
    if not isinstance(dtype, dict) or set(dtype) != {"mean_overlap", "min_overlap"}:
        return "dtype_check is not exactly its declared keys"
    mean_overlap = dtype["mean_overlap"]
    min_overlap = dtype["min_overlap"]
    for label, value, floor in (
        ("mean_overlap", mean_overlap, DTYPE_MEAN_OVERLAP_MIN),
        ("min_overlap", min_overlap, DTYPE_PER_QUERY_OVERLAP_MIN),
    ):
        if (
            isinstance(value, bool)
            or type(value) is not float
            or not math.isfinite(value)
        ):
            return f"dtype_check.{label} {_describe(value)} is not a finite float"
        if not floor <= value <= 1.0:
            return f"dtype_check.{label} {_describe(value)} is outside [{floor}, 1]"
    if min_overlap > mean_overlap:
        return "dtype_check.min_overlap exceeds the mean overlap"
    gauge = verdict.get("gauge")
    if isinstance(gauge, bool) or type(gauge) is not float or gauge != mean:
        return "gauge does not EXACTLY equal observed.mean_recall_at_k"
    blas = verdict.get("blas_environment")
    if (
        not isinstance(blas, dict)
        or set(blas) != set(_BLAS_THREAD_VARIABLES)
        or any(blas[name] != "1" for name in _BLAS_THREAD_VARIABLES)
    ):
        return "blas_environment is not exactly the thread variables pinned to '1'"
    duration = verdict.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or type(duration) is not float
        or not math.isfinite(duration)
        or duration < 0.0
    ):
        return (
            f"duration_seconds {_describe(duration)} is not a finite non-negative float"
        )
    exit_code = verdict.get("exit_code")
    if isinstance(exit_code, bool) or type(exit_code) is not int or exit_code != 0:
        return f"exit_code {_describe(exit_code)} is not exactly 0"
    return None


def build_section(
    verdict: dict[str, object], *, target: float = DEFAULT_TARGET
) -> dict[str, object]:
    """Shape one worker verdict into the ``vector_recall`` calibration section (v4 §2).

    ``frozen`` and ``observed.<family>`` are the deterministic projection two same-machine
    runs must reproduce identically; ``provenance.<family>`` holds every volatile value —
    duration, versions, the oracle path actually taken — outside every hash and comparison.

    Schema note, recorded pre-freeze: the observed counter is ``queries_below_perfect``
    (renamed from ``queries_below_target`` before any calibration was ever frozen — the
    count is of NON-PERFECT queries, ``recall < 1.0``, and the old name lied).
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
