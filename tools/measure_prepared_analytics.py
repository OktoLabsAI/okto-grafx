"""Short fixed detached-analytics observation; no timing threshold or Pulse claim."""

from __future__ import annotations

from dataclasses import replace
import json
import platform
import random
from statistics import median
from time import perf_counter

from okto_grafx.projections import GraphProjection, ProjectionNode, ProjectionEdge, ProjectionLimits


def main() -> None:
    """Separate one-time preparation from five seeded rank calls and repeated peeling."""
    import numpy
    rng = random.Random(970)
    n, m = 5000, 30000
    graph = GraphProjection(b"b" * 16, 1, tuple(ProjectionNode("N", i) for i in range(n)),
        tuple(ProjectionEdge("R", i, rng.randrange(n), rng.randrange(n)) for i in range(m)),
        ProjectionLimits(), 4096 + 1024 * n + 544 * m)
    graph = replace(graph, weights=tuple(1. + i % 11 for i in range(m))).with_adjacency()
    graph.pagerank(backend="numpy", weighted=True, max_iterations=1)
    start = perf_counter()
    prepared = graph.with_pagerank(backend="numpy", weighted=True)
    rank_setup = (perf_counter() - start) * 1000
    start = perf_counter()
    simple = graph.with_simple_topology()
    simple_setup = (perf_counter() - start) * 1000
    samples = {"rank_unprepared": [], "rank_prepared": [], "core_unprepared": [], "core_prepared": []}
    for seed in range(5):
        ranks = []
        for name, candidate in (("rank_unprepared", graph), ("rank_prepared", prepared)):
            start = perf_counter()
            result = candidate.pagerank(backend="numpy", weighted=True, personalization={graph.nodes[seed]: 1.},
                                       tolerance=1e-10, max_iterations=100)
            samples[name].append((perf_counter() - start) * 1000)
            assert result.converged
            ranks.append(result.scores)
        assert max(abs(a - b) for a, b in zip(*ranks)) <= 1e-11
        cores = []
        for name, candidate in (("core_unprepared", graph), ("core_prepared", simple)):
            start = perf_counter()
            cores.append(candidate.k_core())
            samples[name].append((perf_counter() - start) * 1000)
        assert cores[0] == cores[1]
    print(json.dumps({"python": platform.python_version(), "numpy": numpy.__version__, "nodes": n,
        "edges": m, "seed": 970, "repeats": 5, "rank_preparation_ms": rank_setup,
        "simple_preparation_ms": simple_setup, "prepared_rank_logical_bytes": prepared.logical_bytes,
        "prepared_simple_logical_bytes": simple.logical_bytes,
        "measurements": {name: {"median_ms": median(values), "samples_ms": values} for name, values in samples.items()}}, indent=2))


if __name__ == "__main__":
    main()
