"""Short reproducible detached-kernel observation, never a performance gate or UI benchmark."""

from __future__ import annotations

import json
import platform
import random
from statistics import median
from time import perf_counter

from okto_grafx.projections import GraphProjection, ProjectionNode, ProjectionEdge, ProjectionLimits


def main() -> None:
    """Measure one seeded prepared fixture; exclude construction/import warm-up from kernels."""
    import numpy
    rng = random.Random(970)
    n, m = 5000, 30000
    # Synthetic DTOs are only a benchmark fixture, not the consumer capture door.
    graph = GraphProjection(b"b" * 16, 1, tuple(ProjectionNode("N", i) for i in range(n)),
        tuple(ProjectionEdge("R", i, rng.randrange(n), rng.randrange(n)) for i in range(m)),
        ProjectionLimits(), 4096 + 1024 * n + 512 * m).with_adjacency()
    graph.pagerank(backend="numpy", max_iterations=1)
    report: dict[str, object] = {"python": platform.python_version(), "platform": platform.system(),
        "numpy": numpy.__version__, "nodes": n, "physical_edges": m, "seed": 970,
        "repeats": 3, "prepared_logical_bytes": graph.logical_bytes, "measurements": {}}
    scores = {}
    for backend in ("python", "numpy"):
        samples = []
        for _ in range(3):
            start = perf_counter()
            result = graph.pagerank(backend=backend, tolerance=1e-10, max_iterations=100)
            samples.append((perf_counter() - start) * 1000)
            assert result.converged
        scores[backend] = result.scores
        report["measurements"][f"pagerank_{backend}"] = {
            "median_ms": median(samples), "samples_ms": samples,
            "iterations": result.iterations, "residual": result.residual}
    assert max(abs(a - b) for a, b in zip(scores["python"], scores["numpy"])) <= 1e-11
    samples = []
    for _ in range(3):
        start = perf_counter()
        cores = graph.k_core()
        samples.append((perf_counter() - start) * 1000)
    report["measurements"]["bucket_kcore"] = {"median_ms": median(samples), "samples_ms": samples,
                                               "max_core": max(cores)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
