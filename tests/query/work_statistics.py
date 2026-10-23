"""Separate intentional access-regime diagnostics in differential query tests."""

_REGIME_COUNTERS = frozenset({
    "index_probe_keys", "certificate_attempts", "certificate_retries",
    "landing_candidates", "landing_batches",
    "fallback_reason_ineligible_shape", "fallback_reason_unproven_binding",
    "fallback_reason_unproven_totality", "fallback_reason_nonselective_probe_set",
    "fallback_reason_fan_limit", "fallback_reason_index_unavailable",
    "fallback_reason_index_stale", "fallback_reason_owner_overlay",
    "fallback_reason_noncanonical_port", "fallback_reason_quota_frontier",
    "fallback_reason_memory_guard", "fallback_reason_streaming_guard",
})


def comparable_statistics(statistics):
    """Keep every logical/scan counter; certification and landing work may differ.

    RF01 deliberately measures different physical work in batched and scalar
    implementations. Its dedicated tests check those units and attribution;
    differential traversal tests retain all other statistics, including physical
    heap scan counts. Unknown future counters are compared, never hidden.
    """
    return {key: value for key, value in statistics.items()
            if key not in _REGIME_COUNTERS}
