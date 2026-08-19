"""The no-op sink costs nothing (SPEC-M1 FR-14, OR-6, BR-12; CONTRACT.md section 11 item 6).

OR-6 asks for the zero-allocation claim to be verified by counting allocations rather than by
being asserted in a docstring, so the proof below drives a hundred thousand iterations of the hot
path and compares the allocator delta against an empty control loop of exactly the same length.
The control is what makes the number meaningful: it separates "the sink allocated nothing" from
"the interpreter allocated nothing", and only the first is a claim about this adapter.
"""

from __future__ import annotations

import gc
import sys
import tracemalloc
from collections.abc import Callable

import pytest

from okto_grafx.adapters.metrics_noop import (
    EMPTY_SNAPSHOT,
    NULL_TIMER,
    NoOpMetricsSink,
    NullTimer,
)
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.engine.metrics_catalog import metric, register_catalog

ITERATIONS: int = 100_000
"""Length of the measured loop, large enough that a per-call allocation could not hide."""

WARMUP: int = 5_000
"""Iterations run before measuring, so no first-call caching lands inside the measurement."""

ALLOCATION_TOLERANCE: int = 1
"""Blocks the measurement itself may cost.

The measured delta is compared against a control loop first, and equality with the control is
the real assertion. The tolerance exists only because the very first pair of readings in a fresh
interpreter can differ by a single block of allocator bookkeeping, which the control loop shows
is not attributable to the sink.
"""

COUNTER: str = "oktografx_write_conflicts_total"
GAUGE: str = "oktografx_wal_size_bytes"
HISTOGRAM: str = "oktografx_lease_wait_seconds"
LABELS: dict[str, str] = {"outcome": "granted"}


def _hot(sink: NoOpMetricsSink, iterations: int) -> None:
    """Drive the four emitting entry points, with the labels mapping hoisted out of the loop."""
    labels = LABELS
    for _ in range(iterations):
        sink.increment(COUNTER, 1.0, None)
        sink.set_gauge(GAUGE, 4096.0, None)
        sink.observe(HISTOGRAM, 0.5, labels)
        with sink.time(HISTOGRAM, labels):
            pass


def _control(iterations: int) -> None:
    """An empty loop of the same length, which is the baseline the hot loop is compared to."""
    for _ in range(iterations):
        pass


def _blocks(work: Callable[[int], None], iterations: int) -> int:
    """Return the change in allocated blocks caused by running the callable."""
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        before = sys.getallocatedblocks()
        work(iterations)
        after = sys.getallocatedblocks()
    finally:
        if enabled:
            gc.enable()
    return after - before


def _traced_bytes(work: Callable[[int], None], iterations: int) -> int:
    """Return the change in traced memory caused by running the callable."""
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        work(iterations)
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return after - before


def test_the_sink_satisfies_the_port_and_reports_itself_disabled() -> None:
    sink = NoOpMetricsSink()
    assert isinstance(sink, MetricsSink)
    assert sink.enabled is False


def test_registration_is_accepted_and_kept_nowhere() -> None:
    sink = NoOpMetricsSink()
    register_catalog(sink)
    sink.register(metric("oktografx_ledger_depth"))
    assert sink.snapshot() == {}


def test_emitting_an_unregistered_name_is_silently_ignored() -> None:
    # The no-op sink is the one place where an unknown name is not an error: it keeps no
    # registry, so there is nothing to check against and nothing to allocate to check with.
    sink = NoOpMetricsSink()
    sink.increment("oktografx_not_a_metric_total")
    sink.set_gauge("oktografx_not_a_metric_total", 1.0)
    sink.observe("oktografx_not_a_metric_total", 1.0)
    assert sink.snapshot() == {}


def test_the_timer_is_one_shared_reusable_object() -> None:
    sink = NoOpMetricsSink()
    first = sink.time(HISTOGRAM, LABELS)
    second = sink.time(GAUGE)
    assert first is second is NULL_TIMER
    assert isinstance(first, NullTimer)
    assert NoOpMetricsSink().time(GAUGE) is NULL_TIMER


def test_the_timer_nests_and_never_swallows_an_error() -> None:
    sink = NoOpMetricsSink()
    with sink.time(HISTOGRAM), sink.time(HISTOGRAM):
        pass
    with pytest.raises(ValueError):
        with sink.time(HISTOGRAM):
            raise ValueError("propagated")


def test_the_timer_yields_nothing() -> None:
    sink = NoOpMetricsSink()
    with sink.time(HISTOGRAM) as value:
        assert value is None


def test_the_snapshot_is_one_shared_empty_immutable_mapping() -> None:
    sink = NoOpMetricsSink()
    snapshot = sink.snapshot()
    assert snapshot == {}
    assert len(snapshot) == 0
    assert snapshot is EMPTY_SNAPSHOT
    assert sink.snapshot() is NoOpMetricsSink().snapshot()
    with pytest.raises(TypeError):
        snapshot["oktografx_ledger_depth"] = 1  # type: ignore[index]


def test_the_sink_carries_no_per_instance_state() -> None:
    # __slots__ with no members: two sinks cannot diverge and neither can grow a dictionary.
    sink = NoOpMetricsSink()
    assert NoOpMetricsSink.__slots__ == ()
    assert not hasattr(sink, "__dict__")
    with pytest.raises(AttributeError):
        sink.counter = 1  # type: ignore[attr-defined]


def test_the_hot_path_allocates_no_blocks_beyond_an_empty_loop() -> None:
    sink = NoOpMetricsSink()
    _hot(sink, WARMUP)
    _control(WARMUP)
    _blocks(_control, WARMUP)  # discard the first reading pair of the interpreter

    control_delta = _blocks(_control, ITERATIONS)
    hot_delta = _blocks(lambda iterations: _hot(sink, iterations), ITERATIONS)

    assert hot_delta == control_delta, (
        f"{ITERATIONS} iterations of the no-op hot path changed the allocated block count by "
        f"{hot_delta}, while an empty loop of the same length changed it by {control_delta}"
    )
    assert abs(hot_delta) <= ALLOCATION_TOLERANCE


def test_the_hot_path_traces_no_memory_beyond_an_empty_loop() -> None:
    # tracemalloc is the portable half of the proof: it works on any implementation that
    # supports the module, including one without sys.getallocatedblocks.
    sink = NoOpMetricsSink()
    _hot(sink, WARMUP)
    _control(WARMUP)

    control_bytes = _traced_bytes(_control, ITERATIONS)
    hot_bytes = _traced_bytes(lambda iterations: _hot(sink, iterations), ITERATIONS)

    assert hot_bytes <= control_bytes, (
        f"{ITERATIONS} iterations of the no-op hot path traced {hot_bytes} bytes, while an "
        f"empty loop of the same length traced {control_bytes} bytes"
    )


def test_the_measurement_can_fail_on_a_sink_that_does_allocate() -> None:
    # A proof that cannot fail proves nothing: the same harness must see a sink that keeps a
    # list of what it was told.
    class _AllocatingSink:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        @property
        def enabled(self) -> bool:
            return True

        def increment(self, name: str, value: float = 1.0, labels: object = None) -> None:
            self.calls.append((name, value))

        def set_gauge(self, name: str, value: float, labels: object = None) -> None:
            self.calls.append((name, value))

        def observe(self, name: str, value: float, labels: object = None) -> None:
            self.calls.append((name, value))

        def time(self, name: str, labels: object = None) -> object:
            return NULL_TIMER

    greedy = _AllocatingSink()
    _hot(greedy, WARMUP)  # type: ignore[arg-type]
    control_delta = _blocks(_control, ITERATIONS)
    greedy_delta = _blocks(lambda iterations: _hot(greedy, iterations), ITERATIONS)  # type: ignore[arg-type]
    assert greedy_delta > control_delta
