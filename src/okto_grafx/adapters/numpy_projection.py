"""Opt-in NumPy power iteration for detached graph projections."""

from __future__ import annotations

from collections.abc import Callable

from okto_grafx.errors import GrafxUnsupportedOperation

__all__ = ["pagerank_numpy"]


def prepare_numpy(sources: tuple[int, ...], targets: tuple[int, ...], shares: tuple[float, ...],
                  dangling: tuple[int, ...]) -> tuple[bytes, ...]:
    """Copy numeric transition arrays into immutable bytes, never writable retained arrays."""
    try:
        import numpy as np
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[accel] for NumPy PageRank.", field="backend") from failure
    return tuple(np.asarray(values, dtype=dtype).tobytes() for values, dtype in
                 ((sources, "<i8"), (targets, "<i8"), (shares, "<f8"), (dangling, "<i8")))


def pagerank_numpy(node_count: int, sources: tuple[int, ...], targets: tuple[int, ...],
                   shares: tuple[float, ...], teleport: tuple[float, ...],
                   dangling: tuple[int, ...], damping: float, tolerance: float,
                   max_iterations: int, check: Callable[[int], None], *,
                   prepared: tuple[bytes, ...] | None = None) -> tuple[tuple[float, ...], int, bool, float]:
    """Run bounded float64 kernels; the caller reserves buffers and owns read controls."""
    try:
        import numpy as np
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[accel] for NumPy PageRank.", field="backend") from failure
    check(node_count + len(sources))
    if not node_count:
        return (), 0, True, 0.0
    if prepared is None:
        src, dst = np.asarray(sources, dtype=np.int64), np.asarray(targets, dtype=np.int64)
        weights = np.asarray(shares, dtype=np.float64)
        empty = np.asarray(dangling, dtype=np.int64)
    else:
        src, dst, weights, empty = (np.frombuffer(buf, dtype=dtype) for buf, dtype in
                                   zip(prepared, ("<i8", "<i8", "<f8", "<i8")))
    seed = np.asarray(teleport, dtype=np.float64)
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
