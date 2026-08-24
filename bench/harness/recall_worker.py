"""Recall measurement worker: one fresh process, one profile, one JSON verdict (C13, v4 §3).

Run as ``python -m bench.harness.recall_worker --profile smoke --out <file>``. The PARENT
(:mod:`bench.harness.recall`) launches it as a subprocess with the BLAS thread variables set in
the child environment BEFORE the interpreter starts — a fresh process cannot have numpy
pre-imported, which is the whole reason this stage is a worker. The variables are also set
defensively at the top of :func:`main` for a direct invocation; numpy itself is imported lazily
and only on the paths that use it.

The worker owns the mathematics of one run: build the corpus (``uniform-int53-v1``), quantize
it to float32, feed it to a real :class:`VectorEngine` over a memory device, search every
held-out query, and score recall@k against the profile's oracle — pure ``math.fsum`` on the
smoke profile, versioned single-thread numpy brute force on the full profile, the latter only
after the frozen differential agreed with the canonical answers. The TR-4 dtype check runs on
the same oracle. Any fail-closed condition (dtype thresholds, differential disagreement,
missing numpy on a profile that requires it) exits non-zero after writing a diagnostic JSON;
the parent publishes nothing in that case.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bench.recall_corpus import (
    GENERATOR_NAME,
    DtypeCheck,
    compare_truths,
    generate_vectors,
    ground_truth,
    quantize_f32,
    recall_at_k,
    sha256_hex,
    vector_bytes_f32,
    vector_bytes_f64,
)

_BLAS_THREAD_VARIABLES: tuple[str, ...] = (
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


@dataclass(frozen=True, slots=True)
class Profile:
    """One frozen measurement profile of c13_design_v4 §3."""

    name: str
    corpus_size: int
    queries: int
    dimension: int
    k: int
    oracle: str


PROFILES: dict[str, Profile] = {
    "smoke": Profile(
        name="smoke",
        corpus_size=5120,
        queries=64,
        dimension=128,
        k=10,
        oracle="pure-fsum",
    ),
    "full": Profile(
        name="full",
        corpus_size=8192,
        queries=256,
        dimension=384,
        k=10,
        oracle="numpy-bruteforce-v1 (differential pure-fsum 16x1024)",
    ),
    "tiny": Profile(
        name="tiny", corpus_size=96, queries=8, dimension=16, k=4, oracle="pure-fsum"
    ),
}
"""``tiny`` exists for tests only: same code path as smoke, seconds instead of minutes.
Its corpus is deliberately BELOW the regime threshold, so it never freezes anything."""

CORPUS_SEED: int = 1337
QUERY_SEED: int = 4242
DIFFERENTIAL_QUERIES: int = 16
DIFFERENTIAL_SLICE: int = 1024
ACCEL_EPS_REL: float = 1e-9
DTYPE_MEAN_OVERLAP_MIN: float = 0.99
DTYPE_PER_QUERY_OVERLAP_MIN: float = 0.90
HNSW_FROZEN: dict[str, object] = {
    "neighbours": 16,
    "ef_construction": 200,
    "ef_search": 64,
    "index_seed": "0x0C701A11F0C0FFEE",
}


def _pure_truths(
    corpus: list[list[float]], queries: list[list[float]], k: int
) -> list[object]:
    """Return the canonical generous ground truth of every query (math.fsum path)."""
    return [ground_truth(corpus, query, k) for query in queries]


def _numpy_truths(
    corpus: list[list[float]], queries: list[list[float]], k: int
) -> tuple[list[object], str]:
    """Return generous ground truths computed by the versioned numpy brute-force oracle.

    Matrix f64 cosine distances per query; the generous membership and the total order are the
    SAME rules as the canonical oracle — only the arithmetic engine differs, which is exactly
    what the frozen differential validates before this path's answers may stand.
    """
    import numpy  # noqa: PLC0415 - lazily, after the thread environment is pinned

    matrix = numpy.array(corpus, dtype=numpy.float64)
    norms = numpy.sqrt(numpy.einsum("ij,ij->i", matrix, matrix))
    truths: list[object] = []
    for query in queries:
        query_vector = numpy.array(query, dtype=numpy.float64)
        query_norm = float(numpy.sqrt(numpy.dot(query_vector, query_vector)))
        dots = matrix @ query_vector
        with numpy.errstate(divide="ignore", invalid="ignore"):
            distances = 1.0 - dots / (norms * query_norm)
        cleaned = [
            2.0
            if (query_norm == 0.0 or norms[index] == 0.0)
            else float(distances[index])
            for index in range(len(corpus))
        ]
        scored = sorted((distance, index) for index, distance in enumerate(cleaned))
        cut = scored[min(k, len(scored)) - 1][0] if scored else 0.0
        members = frozenset(index for distance, index in scored if distance <= cut)
        truths.append(_FrozenTruth(tuple(scored), cut, members))
    return truths, str(numpy.__version__)


@dataclass(frozen=True, slots=True)
class _FrozenTruth:
    """The numpy oracle's answer in the same shape the canonical GroundTruth carries."""

    ordered: tuple[tuple[float, int], ...]
    cut_distance: float
    members: frozenset[int]


def _differential(corpus: list[list[float]], k: int) -> tuple[bool, str]:
    """Validate the numpy oracle against the canonical one on the frozen subset (v4 §3)."""
    slice_corpus = corpus[:DIFFERENTIAL_SLICE]
    slice_queries = generate_vectors(
        QUERY_SEED + 1, DIFFERENTIAL_QUERIES, len(corpus[0])
    )
    fast, _version = _numpy_truths(slice_corpus, slice_queries, k)
    for index, query in enumerate(slice_queries):
        canonical = ground_truth(slice_corpus, query, k)
        disagreement = compare_truths(canonical, fast[index], eps_rel=ACCEL_EPS_REL)
        if disagreement:
            return False, f"differential query {index}: {disagreement}"
    return True, ""


def _overlap(pre_truth: object, post_truth: object, k: int) -> float:
    """Return the TR-4 overlap of one query: BILATERALLY generous, the shared formula."""
    return min(len(pre_truth.members & post_truth.members), k) / k


def _build_engine(quantized: list[list[float]], dimension: int):
    """Assemble a real vector engine over a memory device and insert the whole corpus.

    This mirrors the most-external sanctioned composition the vector suite itself uses
    (tests/vector/conftest.VectorFixture): no public DDL for embedding spaces exists at the
    frozen M0 SHA, so the bench composes the engine exactly as that suite does — the honest
    fallback c13_design_v4 declares for this case.
    """
    from okto_grafx.adapters.codec_v1 import PageCodecV1
    from okto_grafx.adapters.storage_memory import MemoryStorageDevice
    from okto_grafx.adapters.vectormath_pure import PureVectorMath
    from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
    from okto_grafx.domain.model.value import ValueType, VectorValue
    from okto_grafx.domain.ports.vectormath import DistanceMetric
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.index_manager import IndexManager
    from okto_grafx.engine.vector_engine import VectorEngine

    page_size = 4096
    device = MemoryStorageDevice(page_size=page_size)
    codec = PageCodecV1(page_size)
    metrics = _SilentMetrics()
    pool = BufferPool(
        device, codec, metrics, budget_bytes=16384 * page_size, db_label="c13_recall"
    )
    catalog_store = CatalogStore(pool)
    catalog_store.bootstrap()
    heap = HeapStore(pool, catalog_store)
    heap.bootstrap()
    registry = IndexManager(pool, heap, metrics)
    engine = VectorEngine(
        catalog=catalog_store,
        heap=heap,
        pool=pool,
        indexes=registry,
        math=PureVectorMath(),
        metrics=metrics,
        clock=_StepClock(),
        exact_scan_threshold=4096,
        seed=int(str(HNSW_FROZEN["index_seed"]), 16),
        neighbours=int(HNSW_FROZEN["neighbours"]),  # type: ignore[call-overload]
        ef_construction=int(HNSW_FROZEN["ef_construction"]),  # type: ignore[call-overload]
        ef_search=int(HNSW_FROZEN["ef_search"]),  # type: ignore[call-overload]
    )
    space_definition = EmbeddingSpaceDef(
        space_id=catalog_store.catalog.next_space_id(),
        name="c13_recall",
        dimension=dimension,
        metric=DistanceMetric.COSINE,
        normalized=False,
        storage_dtype="float32",
        state="active",
    )
    engine.create_space(space_definition)
    space = catalog_store.catalog.space("c13_recall")
    table = TableDef(
        table_id=catalog_store.catalog.next_table_id(),
        name="c13_rows",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="layer", type=ValueType.INT64),
            ColumnDef(
                name="embedding", type=space.value_type, vector_space="c13_recall"
            ),
        ),
        primary_key="id",
    )
    catalog_store.catalog.add_table(table)
    catalog_store.save()
    engine.attach(table, "c13_recall")
    csn = 1
    for index, components in enumerate(quantized):
        record_id = index + 1
        stored = engine.validate_vector(space, components)
        ref = heap.insert(
            table,
            record_id,
            (record_id, 0, VectorValue(stored, space.space_id, space.storage_dtype)),
            csn,
        )
        txn = _TransactionStub(record_id)
        engine.stage_insert("c13_recall", record_id, ref, stored, csn, txn)
        engine.commit("c13_recall", txn, csn)
    return engine


class _SilentMetrics:
    """The disabled shape of the metrics port: the worker measures recall, not itself."""

    @property
    def enabled(self) -> bool:
        """Return False: nothing is collected."""
        return False

    def register(self, descriptor: object) -> None:
        """Accept a declaration and keep nothing."""

    def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
        """Discard a counter increment."""

    def set_gauge(self, name: str, value: float, labels: object = None) -> None:
        """Discard a gauge value."""

    def observe(self, name: str, value: float, labels: object = None) -> None:
        """Discard a histogram observation."""

    def time(self, name: str, labels: object = None):
        """Return a timing context that measures nothing."""
        from contextlib import nullcontext

        return nullcontext()


class _StepClock:
    """A deterministic clock: the worker's outputs must not depend on wall time."""

    def __init__(self) -> None:
        self._now = 0.0

    def monotonic(self) -> float:
        """Return a strictly increasing number."""
        self._now += 0.001
        return self._now

    def wall(self) -> float:
        """Return the same deterministic number for wall clock queries."""
        return self.monotonic()


class _TransactionStub:
    """The minimal staging transaction the vector engine's commit path needs."""

    def __init__(self, txn_id: int) -> None:
        self._txn_id = txn_id
        self.epoch = 1
        self.records: list[object] = []

    @property
    def txn_id(self) -> int:
        """Return the process-local transaction number."""
        return self._txn_id

    def stage_record(self, record: object) -> None:
        """Keep the record the engine would append at commit."""
        self.records.append(record)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """A snapshot that sees every committed version (the corpus is fully committed)."""

    read_lsn: int = 2

    def visible(self, xmin: int, xmax: int) -> bool:
        """CONTRACT.md §8.5 visibility, verbatim."""
        return (
            xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)
        )


def run_profile(profile_name: str, gt_mode: str = "auto") -> dict[str, object]:
    """Measure one profile end to end and return the worker verdict document.

    Fail-closed outcomes return ``ok=False`` with a ``failure`` string and publish nothing;
    the caller turns that into a non-zero exit. Deterministic given (profile, gt_mode).
    """
    profile = PROFILES[profile_name]
    corpus64 = generate_vectors(CORPUS_SEED, profile.corpus_size, profile.dimension)
    quantized = quantize_f32(corpus64)
    queries = generate_vectors(QUERY_SEED, profile.queries, profile.dimension)
    hashes = {
        "corpus_sha256_f64": sha256_hex(vector_bytes_f64(corpus64)),
        "corpus_sha256_f32": sha256_hex(vector_bytes_f32(corpus64)),
        "query_sha256_f64": sha256_hex(vector_bytes_f64(queries)),
        "query_sha256_f32": sha256_hex(vector_bytes_f32(queries)),
    }
    use_numpy = profile.oracle.startswith("numpy") and gt_mode != "pure"
    numpy_version = "absent"
    if use_numpy:
        try:
            agreed, disagreement = _differential(quantized, profile.k)
        except ModuleNotFoundError:
            return {
                "ok": False,
                "failure": (
                    f"profile {profile.name!r} requires numpy for its exhaustive oracle and "
                    "numpy is not importable; run --recall-gt pure on the smoke profile "
                    "instead."
                ),
                "gt_path_used": "none",
            }
        if not agreed:
            return {
                "ok": False,
                "failure": f"accelerated oracle failed the frozen differential: {disagreement}",
                "gt_path_used": "numpy-rejected",
            }
        truths, numpy_version = _numpy_truths(quantized, queries, profile.k)
        pre_truths, _ = _numpy_truths(corpus64, queries, profile.k)
        gt_path = "numpy"
    else:
        if profile.oracle.startswith("numpy"):
            return {
                "ok": False,
                "failure": (
                    f"profile {profile.name!r} demands the numpy oracle; --recall-gt pure "
                    "would take the pure pass v4 declares infeasible at this size. Use the "
                    "smoke profile for a pure oracle."
                ),
                "gt_path_used": "none",
            }
        truths = _pure_truths(quantized, queries, profile.k)
        pre_truths = _pure_truths(corpus64, queries, profile.k)
        gt_path = "pure"
    overlaps = [
        _overlap(pre_truths[index], truths[index], profile.k)
        for index in range(len(queries))
    ]
    dtype = DtypeCheck(
        mean_overlap=math.fsum(overlaps) / len(overlaps),
        min_overlap=min(overlaps),
        per_query=tuple(overlaps),
    )
    if not dtype.passes(
        mean_overlap_min=DTYPE_MEAN_OVERLAP_MIN,
        per_query_overlap_min=DTYPE_PER_QUERY_OVERLAP_MIN,
    ):
        return {
            "ok": False,
            "failure": (
                f"TR-4 dtype check failed closed: mean {dtype.mean_overlap:.4f} "
                f"(floor {DTYPE_MEAN_OVERLAP_MIN}), min {dtype.min_overlap:.4f} "
                f"(floor {DTYPE_PER_QUERY_OVERLAP_MIN}); nothing published."
            ),
            "gt_path_used": gt_path,
        }
    engine = _build_engine(quantized, profile.dimension)
    snapshot = _Snapshot()
    recalls: list[float] = []
    for index, query in enumerate(queries):
        result = engine.search(
            space="c13_recall", query=query, k=profile.k, snapshot=snapshot
        )
        answer = [hit.record_id - 1 for hit in result.hits]
        recalls.append(recall_at_k(answer, truths[index], profile.k))
    mean_recall = math.fsum(recalls) / len(recalls)
    below = sum(1 for value in recalls if value < 1.0)
    return {
        "ok": True,
        "failure": "",
        "profile": profile.name,
        "generator": GENERATOR_NAME,
        "oracle": profile.oracle if gt_path == "numpy" else "pure-fsum",
        "gt_path_used": gt_path,
        "numpy": numpy_version,
        "k": profile.k,
        "queries": profile.queries,
        "corpus_size": profile.corpus_size,
        "dimension": profile.dimension,
        "hashes": hashes,
        "gauge": mean_recall,
        "observed": {
            "mean_recall_at_k": mean_recall,
            "min_recall_at_k": min(recalls),
            "queries_below_target": below,
            "dtype_check": {
                "mean_overlap": dtype.mean_overlap,
                "min_overlap": dtype.min_overlap,
            },
        },
        "blas_environment": {
            name: os.environ.get(name, "") for name in _BLAS_THREAD_VARIABLES
        },
        "hnsw": dict(HNSW_FROZEN),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the worker arguments, run one profile, write the JSON verdict, exit by outcome."""
    for name in _BLAS_THREAD_VARIABLES:
        os.environ.setdefault(name, "1")
    parser = argparse.ArgumentParser(prog="python -m bench.harness.recall_worker")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--gt", default="auto", choices=("auto", "pure"))
    parser.add_argument(
        "--out", required=True, help="where the JSON verdict is written"
    )
    arguments = parser.parse_args(argv)
    verdict = run_profile(arguments.profile, arguments.gt)
    Path(arguments.out).write_text(
        json.dumps(verdict, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        encoding="utf-8",
    )
    if not verdict.get("ok", False):
        print(f"recall worker: FAIL-CLOSED -- {verdict.get('failure', 'unknown')}")
        return 3
    print(
        f"recall worker: profile {arguments.profile} mean recall@k "
        f"{verdict['gauge']:.4f} via {verdict['gt_path_used']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
