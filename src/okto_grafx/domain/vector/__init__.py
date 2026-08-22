"""The vector subsystem of the pure core: spaces, the graph, the planner and the key.

Nothing here reads a clock, opens a file or imports a numerical library, and nothing here is an
index: the index is an engine object, because it owns pages, and it is built on the store the
index framework of C7 provides (CONTRACT.md section 8.7, amendment A12). What lives here is the
part that is genuinely the vector subsystem's own -- the rules a vector must satisfy to be
stored, the graph that answers a nearest-neighbour question, and the planner that chooses between the
two regimes. The KEY of an index entry belongs to the index framework, which derives it from the
indexed column of the row, and is not restated here.
"""

from __future__ import annotations

from okto_grafx.domain.vector.filter import CandidateFilter, RecordIdFilter, admits_everything
from okto_grafx.domain.vector.hnsw import HnswGraph, TraversalStats
from okto_grafx.domain.vector.key import (
    VECTOR_DIGEST_DERIVATION,
    VECTOR_KEY_SIZE,
    VectorIndexDefinition,
    vector_digest,
)
from okto_grafx.domain.vector.planner import (
    DEFAULT_EXACT_SCAN_THRESHOLD,
    REGIME_APPROXIMATE,
    REGIME_EXACT,
    REGIMES,
    RegimePlan,
    plan_regime,
)
from okto_grafx.domain.vector.space import (
    NORMALIZED_NORM_TOLERANCE,
    STORAGE_DTYPE_FLOAT32,
    STORAGE_DTYPE_FLOAT64,
    require_active,
    require_space_identity,
    round_to_storage_dtype,
    validate_components,
    validate_query_components,
    vector_of,
)

__all__ = [
    "DEFAULT_EXACT_SCAN_THRESHOLD",
    "NORMALIZED_NORM_TOLERANCE",
    "REGIMES",
    "REGIME_APPROXIMATE",
    "REGIME_EXACT",
    "STORAGE_DTYPE_FLOAT32",
    "STORAGE_DTYPE_FLOAT64",
    "VECTOR_DIGEST_DERIVATION",
    "VECTOR_KEY_SIZE",
    "CandidateFilter",
    "HnswGraph",
    "RecordIdFilter",
    "RegimePlan",
    "TraversalStats",
    "VectorIndexDefinition",
    "admits_everything",
    "plan_regime",
    "require_active",
    "require_space_identity",
    "round_to_storage_dtype",
    "validate_components",
    "validate_query_components",
    "vector_digest",
    "vector_of",
]
