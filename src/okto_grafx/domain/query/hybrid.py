"""Versioned, bounded hybrid retrieval options and source-transparent results."""

from __future__ import annotations

from dataclasses import dataclass
from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = ["HybridSearchOptions", "HybridHit", "HybridSearchResult"]


@dataclass(frozen=True, slots=True)
class HybridSearchOptions:
    """RRF-v1 over bounded source candidates; graph work is explicit and optional."""

    lexical_weight: float = 1.0
    vector_weight: float = 1.0
    rrf_k: int = 60
    candidate_k: int = 100
    fusion: str = "union"
    allow_partial: bool = False
    graph_relations: tuple[str, ...] = ()
    graph_seeds: tuple[int, ...] = ()
    graph_direction: str = "out"
    graph_hops: int = 1
    graph_weight: float = 0.0
    graph_filter: bool = False
    max_graph_edges: int = 10_000
    max_memory_bytes: int = 32 * 1024 * 1024
    graph_access: str = "auto"

    def __post_init__(self) -> None:
        for name in ("lexical_weight", "vector_weight", "graph_weight"):
            value = getattr(self, name)
            if type(value) not in (float, int) or not 0 <= value <= 1000:
                raise GrafxConfigurationError("Invalid hybrid weight.", field=name)
        if not (self.lexical_weight or self.vector_weight):
            raise GrafxConfigurationError(
                "At least one retrieval source is required.", field="weights"
            )
        for name, maximum in (
            ("rrf_k", 100_000),
            ("candidate_k", 10_000),
            ("graph_hops", 8),
            ("max_graph_edges", 1_000_000),
            ("max_memory_bytes", 2**31),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise GrafxConfigurationError("Invalid hybrid bound.", field=name)
        for name, choices in (
            ("fusion", ("union", "intersection")),
            ("graph_direction", ("out", "in", "both")),
            ("graph_access", ("auto", "scan")),
        ):
            if (
                type(getattr(self, name)) is not str
                or getattr(self, name) not in choices
            ):
                raise GrafxConfigurationError("Invalid hybrid mode.", field=name)
        if type(self.allow_partial) is not bool or type(self.graph_filter) is not bool:
            raise GrafxConfigurationError(
                "Hybrid switches must be booleans.", field="switch"
            )
        if (
            type(self.graph_relations) is not tuple
            or len(self.graph_relations) > 16
            or any(type(v) is not str or not v for v in self.graph_relations)
            or len(set(self.graph_relations)) != len(self.graph_relations)
        ):
            raise GrafxConfigurationError(
                "Invalid relationship allowlist.", field="graph_relations"
            )
        if (
            type(self.graph_seeds) is not tuple
            or len(self.graph_seeds) > 10_000
            or any(
                type(v) is not int or not 0 < v < 2**64 - 1 for v in self.graph_seeds
            )
        ):
            raise GrafxConfigurationError("Invalid graph seeds.", field="graph_seeds")
        if (self.graph_weight or self.graph_filter) and not (
            self.graph_seeds and self.graph_relations
        ):
            raise GrafxConfigurationError(
                "Graph boost/filter needs seeds and relations.", field="graph_seeds"
            )


@dataclass(frozen=True, slots=True)
class HybridHit:
    """One table-qualified identity with source ranks, raw scores and fusion explanation."""

    table: str
    record_id: int
    score: float
    lexical_rank: int | None
    lexical_score: float | None
    vector_rank: int | None
    vector_score: float | None
    graph_distance: int | None


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """No hidden partial or approximate source: all dispositions are retained on empty hits."""

    hits: tuple[HybridHit, ...]
    snapshot_commit: int
    fusion: str
    regime: str
    lexical_regime: str
    vector_regime: str
    lexical_candidates: int
    vector_candidates: int
    graph_edges_visited: int
    source_errors: tuple[tuple[str, str], ...]
    lexical_index_built_through_commit: int | None = None
    graph_regime: str = "disabled"
    memory_peak_bytes: int = 0
    lexical_memory_peak_bytes: int = 0
    vector_memory_peak_bytes: int = 0
    graph_memory_peak_bytes: int = 0
