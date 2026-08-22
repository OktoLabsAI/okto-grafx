"""The two-regime planner of a similarity search (SPEC-VEC FR-5, decision ``dec_fc3351c3``).

One number decides how a similarity query is answered: how many rows the filter is expected to
leave. Below the calibrated threshold the engine scans that set exactly, which costs a distance
per surviving row and returns a recall of 1.0 by construction. Above it the engine traverses the
filter-aware graph, which costs a beam instead of a scan and returns a labelled approximation.

Neither regime is a fallback for the other, and the label is never inferred by the caller: every
result carries ``regime`` and the neighbour count actually achieved, so an approximation is
always visible as one (BR-2).

The threshold is a frozen decision
----------------------------------
:data:`DEFAULT_EXACT_SCAN_THRESHOLD` is 4096, which is the value CONTRACT.md section 5 freezes
into ``DatabaseConfig.vector_exact_scan_threshold``. It is the calibration output of SPEC-VEC
FR-8, owned by the benchmark harness, and this module consumes it rather than deriving it: a
threshold computed from the data would move under the feet of the two-regime agreement test.
The literal is asserted by a test, not compared against itself (amendments A56 and A68), and a
second test compares it against the composition root so the two cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = [
    "REGIME_EXACT",
    "REGIME_APPROXIMATE",
    "REGIMES",
    "DEFAULT_EXACT_SCAN_THRESHOLD",
    "RegimePlan",
    "plan_regime",
]

REGIME_EXACT: str = "exact"
"""The label of a result produced by scanning the filtered set: recall 1.0 by construction."""

REGIME_APPROXIMATE: str = "approximate"
"""The label of a result produced by a filter-aware graph traversal."""

REGIMES: frozenset[str] = frozenset({REGIME_EXACT, REGIME_APPROXIMATE})
"""The only two labels a search result may carry, and the bounded domain of the metric label."""

DEFAULT_EXACT_SCAN_THRESHOLD: int = 4096
"""Filtered rows at or below which a search scans exactly instead of traversing the graph."""


def _require_count(field: str, value: int) -> int:
    """Return a non-negative integer count, refusing a caller argument that is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a vector search plan must be an integer; got "
            f"{type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value < 0:
        raise GrafxConfigurationError(
            f"The {field} of a vector search plan must not be negative; got {value}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class RegimePlan:
    """The decision, and everything that went into it, so a caller can see why.

    ``estimate`` is the number the decision was taken on: the cardinality the filter declared,
    or the size of the space when there is no filter to narrow it. ``selectivity`` is that
    estimate as a fraction of the space, which is the number the metric publishes.
    """

    regime: str
    estimate: int
    space_size: int
    threshold: int
    filter_cardinality: int | None

    @property
    def is_exact(self) -> bool:
        """Return True when this plan scans the filtered set exactly."""
        return self.regime == REGIME_EXACT

    @property
    def selectivity(self) -> float:
        """Return the fraction of the space the filter is expected to leave.

        An empty space has no fraction to report and answers 0.0, which is the value the ratio
        histogram already reserves for a filter that excluded everything.
        """
        if self.space_size <= 0:
            return 0.0
        return self.estimate / self.space_size


def plan_regime(
    *, space_size: int, filter_cardinality: int | None, threshold: int
) -> RegimePlan:
    """Choose the regime for one search from the expected size of the filtered set.

    A filter that does not know its own cardinality is estimated at the size of the space, which
    is the true upper bound of any subset of it: the estimate is never smaller than the set it
    describes, so an unknown filter cannot talk the planner into the exact regime by claiming to
    be small.
    """
    _require_count("space_size", space_size)
    _require_count("threshold", threshold)
    if filter_cardinality is not None:
        _require_count("filter_cardinality", filter_cardinality)
    estimate = space_size if filter_cardinality is None else min(filter_cardinality, space_size)
    regime = REGIME_EXACT if estimate <= threshold else REGIME_APPROXIMATE
    return RegimePlan(
        regime=regime,
        estimate=estimate,
        space_size=space_size,
        threshold=threshold,
        filter_cardinality=filter_cardinality,
    )
