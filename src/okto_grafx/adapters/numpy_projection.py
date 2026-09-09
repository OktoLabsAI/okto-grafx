"""Opt-in NumPy power iteration for detached graph projections."""

from __future__ import annotations

from collections.abc import Callable

from okto_grafx.errors import GrafxUnsupportedOperation

__all__ = ["pagerank_numpy"]


def pagerank_numpy(node_count: int, sources: tuple[int, ...], targets: tuple[int, ...],
                   shares: tuple[float, ...], teleport: tuple[float, ...],
                   dangling: tuple[int, ...], damping: float, tolerance: float,
                   max_iterations: int, check: Callable[[int], None]) -> tuple[tuple[float, ...], int, bool, float]:
    """Run bounded float64 kernels; the caller reserves buffers and owns read controls."""
    try:
        import numpy as np
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[accel] for NumPy PageRank.", field="backend") from failure
    check(node_count + len(sources))
    if not node_count:
        return (), 0, True, 0.0
    src, dst = np.asarray(sources, dtype=np.int64), np.asarray(targets, dtype=np.int64)
    weights = np.asarray(shares, dtype=np.float64)
    seed = np.asarray(teleport, dtype=np.float64)
    empty = np.asarray(dangling, dtype=np.int64)
    ranks = np.full(node_count, 1.0 / node_count, dtype=np.float64)
    for iteration in range(1, max_iterations + 1):
        check(3 * node_count + len(sources))
        mass = float(ranks[empty].sum())
        updated = (1.0 - damping + damping * mass) * seed
        updated += np.bincount(dst, weights=damping * ranks[src] * weights, minlength=node_count)
        residual = float(np.abs(updated - ranks).sum())
        ranks = updated
        check(0)
        if residual <= tolerance:
            return tuple(map(float, ranks)), iteration, True, residual
    return tuple(map(float, ranks)), max_iterations, False, residual
