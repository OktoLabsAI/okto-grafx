"""Native percentile arithmetic shared by in-memory and external aggregation."""

from __future__ import annotations

import math

from okto_grafx.errors import GrafxPlanError

PERCENTILE_FUNCTIONS = frozenset({"PERCENTILEDISC", "PERCENTILECONT"})


def percentile_argument(value: object) -> float:
    """Validate a numeric percentile fraction in the inclusive range zero to one."""
    if type(value) not in (int, float):
        raise GrafxPlanError("A percentile must be numeric.", field="percentile", reason="percentile_argument_type", query_phase="execution")
    if not 0 <= value <= 1:
        raise GrafxPlanError("A percentile must be in [0,1].", field="percentile", reason="percentile_argument_bounds", query_phase="execution")
    return float(value)


def percentile_sample(value: object) -> None:
    """Refuse nonnumeric non-NULL percentile samples at evaluation."""
    if type(value) not in (int, float):
        raise GrafxPlanError("Percentile samples must be numeric or NULL.", field="percentile", reason="percentile_sample_type", query_phase="execution")


def percentile_positions(function: str, count: int, fraction: float) -> tuple[int, int, float]:
    """Compute discrete or interpolated sample positions in an ordered population."""
    if function == "PERCENTILEDISC":
        index = max(0, min(count - 1, math.ceil(fraction * count) - 1))
        return index, index, 0.0
    position = fraction * (count - 1)
    lower = min(count - 1, math.floor(position))
    return lower, min(count - 1, lower + 1), position - lower


def percentile_interpolate(function: str, low: int | float, high: int | float, weight: float) -> int | float:
    """Select a discrete sample or interpolate continuous neighboring samples."""
    if function == "PERCENTILEDISC":
        return low
    if weight == 0.0 or low == high:
        return float(low)
    # Avoid overflowing high-low for finite opposite-sign extremes.
    return float(low) * (1.0 - weight) + float(high) * weight


__all__ = [
    'PERCENTILE_FUNCTIONS',
    'percentile_argument',
    'percentile_sample',
    'percentile_positions',
    'percentile_interpolate',
]
