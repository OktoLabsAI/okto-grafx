"""C3 against the real MetricsSink, not a permissive double.

The double this suite uses elsewhere accepts any name. The sink the default install ships
refuses one it has never seen (A51), so a component that emits without registering works in its
own tests and fails the moment somebody assembles the stack -- which is exactly how this was
found, by C11 rather than here. One path against the real thing is enough to give that gap
somewhere to show; this covers all three outcomes, because the takeover and timeout paths emit
from different call sites than the grant.
"""

from __future__ import annotations

import pytest

from conftest import CoordinatorFactory
from coordination_support import ManualClock
from okto_grafx.adapters.coordination_local import EMITTED_METRICS, LEASE_WAIT_METRIC
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxLeaseTimeout
from okto_grafx.engine.metrics_catalog import metric


def _outcomes(sink: OpenMetricsSink) -> dict[str, float]:
    """Return the observation count of each outcome series the real sink is holding."""
    state = sink.snapshot()[LEASE_WAIT_METRIC]
    return {
        sample["labels"]["outcome"]: sample["count"]  # type: ignore[index]
        for sample in state["samples"]  # type: ignore[index]
    }


def test_every_emitted_metric_is_registered_before_it_is_emitted(
    make_coordinator: CoordinatorFactory
) -> None:
    """Constructing the coordinator is what registers; emitting never finds an unknown name.

    The real sink refuses an unregistered name, so reaching the assertion at all is most of the
    proof. Relying on the composition root to register a blanket catalog would work only until
    the next component emits something the root does not know it emits.
    """
    sink = OpenMetricsSink()
    coordinator = make_coordinator(owner_id="p1-aaaa", metrics=sink)
    assert set(EMITTED_METRICS) <= set(sink.snapshot())

    coordinator.acquire_writer_lease(timeout=1.0)
    assert _outcomes(sink)["granted"] == 1.0


def test_the_real_sink_records_every_outcome_this_component_emits(
    make_coordinator: CoordinatorFactory
) -> None:
    # A66.1: the label domain is bounded at registration, so an outcome this component emits and
    # the catalog does not allow would be refused here rather than accepted as a new series.
    sink = OpenMetricsSink()
    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0, metrics=sink)
    zombie.acquire_writer_lease(timeout=1.0)

    contender_clock = ManualClock(monotonic=3.0)
    contender = make_coordinator(owner_id="p1-slow", clock=contender_clock, metrics=sink)
    with pytest.raises(GrafxLeaseTimeout):
        contender.acquire_writer_lease(timeout=1.0)

    survivor_clock = ManualClock(monotonic=8_000.0)
    survivor = make_coordinator(owner_id="p2-alive", clock=survivor_clock, metrics=sink)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    survivor_clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    assert survivor.takeover().epoch == 2

    counts = _outcomes(sink)
    assert counts == {"granted": 1.0, "timeout": 1.0, "takeover": 1.0}, counts


def test_registering_twice_is_what_lets_a_shared_sink_work(
    make_coordinator: CoordinatorFactory
) -> None:
    # The composition root registers the whole catalog as well, so this component registering the
    # same descriptor has to be a no-op rather than a conflict. It is a no-op only because the
    # descriptor comes from the catalog rather than being restated here.
    sink = OpenMetricsSink()
    sink.register(metric(LEASE_WAIT_METRIC))
    first = make_coordinator(owner_id="p1-aaaa", metrics=sink)
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=40.0, metrics=sink)
    first.acquire_writer_lease(timeout=1.0)
    assert _outcomes(sink)["granted"] == 1.0
    assert second.owner_id() != first.owner_id()


def test_an_unregistered_name_is_what_the_real_sink_refuses(
    make_coordinator: CoordinatorFactory
) -> None:
    # The failure C11 met, stated directly: without registration the emit path raises, and it
    # raises from the sink rather than from anything this component could catch.
    sink = OpenMetricsSink()
    with pytest.raises(GrafxConfigurationError) as refusal:
        sink.observe(LEASE_WAIT_METRIC, 0.1, {"outcome": "granted"})
    assert "never registered" in str(refusal.value)
