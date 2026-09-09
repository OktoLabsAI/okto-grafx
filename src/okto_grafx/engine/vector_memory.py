"""Deterministic per-picture HNSW tariffs, separate from query buffers and RSS."""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded
from okto_grafx.domain.vector.hnsw import MAX_LEVEL

__all__ = ["VectorMemoryUsage"]


@dataclass(frozen=True, slots=True)
class VectorMemoryUsage:
    """Local derived-cache observations; no storage read or freshness proof.

    Bytes are conservative logical tariffs, not measured Python allocations. Peaks
    include refused reservations. Each independently held picture has its own
    limit; these values do not cap all participants or RSS.
    """

    space: str
    limit_bytes: int | None
    cached_entries: int
    cached_logical_bytes: int
    peak_requested_bytes: int
    budget_refusals: int
    warm_retirements: int


def require_picture_budget(value: int | None) -> int | None:
    """Validate an optional positive native logical-byte ceiling."""
    if value is not None and (type(value) is not int or value < 1):
        raise GrafxConfigurationError(
            "vector_hnsw_memory_budget_bytes must be None or a positive integer.",
            field="vector_hnsw_memory_budget_bytes",
        )
    return value


def picture_tariff(entries: int, dimension: int, neighbours: int) -> int:
    """Charge vectors, identity maps and maximum-height adjacency capacity.

    Float components cost 32 logical bytes even for compact adapters. Each tower
    reserves all supported levels; this deliberately overestimates short towers.
    """
    return 4096 + entries * (1024 + dimension * 32 + 32 * neighbours * (MAX_LEVEL + 2))


def work_tariff(entries: int, dimension: int, neighbours: int, ef: int, *, cold: bool) -> int:
    """Include insertion frontier/visited state and cold headers/score caches.

    Construction can visit the complete picture, not merely ef nodes. Header sort
    slots and link-score caches are charged for the entire cold-build entry set.
    One decoder or custom math provider's private allocations remain out of scope.
    """
    scratch = entries * 128 + ef * 128 + dimension * 64
    if cold:
        scratch += entries * (256 + 64 * neighbours * (MAX_LEVEL + 2))
    return picture_tariff(entries, dimension, neighbours) + scratch


def require_reservation(amount: int, limit: int | None) -> None:
    """Refuse before retaining a picture whose declared tariff exceeds its limit."""
    if limit is not None and amount > limit:
        raise GrafxQueryBudgetExceeded(
            "HNSW derived-picture logical-memory budget exceeded.",
            resource="vector_hnsw_memory", requested_bytes=amount, limit_bytes=limit,
        )
