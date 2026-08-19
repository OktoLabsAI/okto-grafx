"""Acquire, renew, release and time out on the writer lease (FR-7, CONTRACT.md section 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordination_support import ManualClock, RecordingMetricsSink
from conftest import CoordinatorFactory
from okto_grafx.adapters.coordination_local import (
    LEASE_WAIT_METRIC,
    LocalProcessCoordinator,
    decode_lease_record,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxStaleEpoch,
)
from okto_grafx.domain.ports.coordination import Lease, ProcessCoordinator


def published(database_root: Path) -> object:
    """Return the lease record as it stands on disk, decoded from the raw bytes."""
    return decode_lease_record((database_root / "control" / "writer.lease").read_bytes())


def test_the_adapter_satisfies_the_port(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    assert isinstance(coordinator, ProcessCoordinator)
    assert isinstance(coordinator, LocalProcessCoordinator)


def test_the_first_acquisition_publishes_epoch_one(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    assert coordinator.current_epoch() == 0
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    assert lease.owner_id == coordinator.owner_id() == "p1-aaaa"
    assert lease.epoch == 1
    assert lease.heartbeat_seq == 1
    assert lease.ttl_seconds == 5.0
    assert coordinator.current_epoch() == 1
    record = published(database_root)
    assert record.owner_id == "p1-aaaa"
    assert record.epoch == 1
    assert record.held is True
    assert record.superseded_epoch == 0


def test_renewal_advances_the_heartbeat_and_keeps_the_epoch(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    for expected in (2, 3, 4):
        lease = coordinator.renew_lease(lease)
        assert lease.heartbeat_seq == expected
        assert lease.epoch == 1
        assert published(database_root).heartbeat_seq == expected
    assert lease.acquired_monotonic == pytest.approx(1_000.0)


def test_releasing_makes_the_lease_vacant_without_dropping_the_epoch(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.release_lease(lease)
    record = published(database_root)
    assert record.held is False
    assert record.epoch == 1
    assert record.owner_id == "p1-aaaa"


def test_a_second_owner_takes_a_released_lease_at_the_next_epoch(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=9_000_000.0)
    lease = first.acquire_writer_lease(timeout=1.0)
    first.release_lease(lease)
    taken = second.acquire_writer_lease(timeout=1.0)
    assert taken.owner_id == "p2-bbbb"
    assert taken.epoch == 2
    assert second.current_epoch() == 2
    # The previous holder is refused at once, with no byte of its own reaching the device.
    with pytest.raises(GrafxStaleEpoch):
        first.validate_epoch(lease.epoch)


def test_re_acquiring_a_lease_this_participant_already_holds_is_idempotent(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    first = coordinator.acquire_writer_lease(timeout=1.0)
    second = coordinator.acquire_writer_lease(timeout=1.0)
    assert second == first
    assert coordinator.current_epoch() == 1


def test_a_second_owner_times_out_while_the_first_is_alive(
    make_coordinator: CoordinatorFactory, metrics: RecordingMetricsSink
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=42.0, metrics=metrics)
    first.acquire_writer_lease(timeout=1.0)
    with pytest.raises(GrafxLeaseTimeout) as failure:
        second.acquire_writer_lease(timeout=1.0)
    assert failure.value.retryable is True
    assert failure.value.code == "lease_timeout"
    assert metrics.outcomes(LEASE_WAIT_METRIC) == ["timeout"]
    assert second.current_epoch() == 1


def test_a_zero_timeout_still_makes_one_attempt(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    assert coordinator.acquire_writer_lease(timeout=0.0).epoch == 1


def test_renewing_after_a_takeover_reports_the_lease_as_stolen(
    make_coordinator: CoordinatorFactory, clock: ManualClock
) -> None:
    first = make_coordinator(owner_id="p1-aaaa", clock=clock)
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=77.0)
    lease = first.acquire_writer_lease(timeout=1.0)
    second.detect_dead_owner(stall_threshold=5.0)
    second_clock_advance(second)
    second.takeover()
    with pytest.raises(GrafxLeaseStolen) as failure:
        first.renew_lease(lease)
    assert failure.value.retryable is False
    assert failure.value.details["published_epoch"] == 2


def second_clock_advance(coordinator: LocalProcessCoordinator) -> None:
    """Advance the clock of a coordinator past the stall threshold, then re-observe."""
    clock = coordinator._clock  # noqa: SLF001 - the test drives the injected fake on purpose
    clock.advance(6.0)
    coordinator.detect_dead_owner(stall_threshold=5.0)


def test_releasing_a_stolen_lease_is_quiet(make_coordinator: CoordinatorFactory) -> None:
    # Closing a database must never raise because somebody else already won the epoch.
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=77.0)
    lease = first.acquire_writer_lease(timeout=1.0)
    second.detect_dead_owner(stall_threshold=5.0)
    second_clock_advance(second)
    taken = second.takeover()
    first.release_lease(lease)
    assert second.current_epoch() == taken.epoch == 2
    assert first.current_epoch() == 2


def test_the_metric_reports_a_grant_and_a_takeover(
    make_coordinator: CoordinatorFactory, metrics: RecordingMetricsSink
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    second = make_coordinator(owner_id="p2-bbbb", monotonic_origin=5.0, metrics=metrics)
    first.acquire_writer_lease(timeout=1.0)
    taken = second.acquire_writer_lease(timeout=30.0)
    assert taken.epoch == 2
    assert metrics.outcomes(LEASE_WAIT_METRIC) == ["takeover"]
    observed = [value for name, value, _labels in metrics.observations if name == LEASE_WAIT_METRIC]
    assert observed[0] > 5.0


def test_a_disabled_sink_costs_the_hot_path_nothing(make_coordinator: CoordinatorFactory) -> None:
    sink = RecordingMetricsSink(enabled=False)
    coordinator = make_coordinator(owner_id="p1-aaaa", metrics=sink)
    coordinator.acquire_writer_lease(timeout=1.0)
    assert sink.observations == []


def test_the_lease_survives_a_fresh_coordinator_over_the_same_directory(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-aaaa")
    lease = first.acquire_writer_lease(timeout=1.0)
    lease = first.renew_lease(lease)
    observer = make_coordinator(owner_id="p3-cccc", monotonic_origin=1.0)
    assert observer.current_epoch() == 1
    assert observer.detect_dead_owner(stall_threshold=5.0) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ttl_seconds", 0.0),
        ("ttl_seconds", -1.0),
        ("owner_stall_threshold", 0.0),
        ("reader_stall_threshold", -0.5),
        ("poll_interval", 0.0),
        ("section_timeout", -1.0),
        ("epoch_cache_seconds", -0.1),
        ("owner_id", "UPPER"),
        ("owner_id", "with/slash"),
        ("control_directory", ""),
    ],
)
def test_a_refused_setting_fails_closed(
    make_coordinator: CoordinatorFactory, field: str, value: object
) -> None:
    settings: dict[str, object] = {"owner_id": "p1-aaaa"}
    settings[field] = value
    with pytest.raises(GrafxConfigurationError):
        make_coordinator(**settings)


def test_a_negative_timeout_is_refused(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    with pytest.raises(GrafxConfigurationError):
        coordinator.acquire_writer_lease(timeout=-1.0)


def test_the_lease_type_is_the_frozen_one(make_coordinator: CoordinatorFactory) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    lease = coordinator.acquire_writer_lease(timeout=1.0)
    assert isinstance(lease, Lease)
    with pytest.raises(Exception):
        lease.epoch = 9  # type: ignore[misc]


def test_an_acquisition_never_outlasts_the_timeout_it_was_given(
    make_coordinator: CoordinatorFactory
) -> None:
    # The section has a budget of its own, and it must not be allowed to overrun the budget the
    # caller gave to the acquisition: a caller that asked for one second waits one second.
    contender_clock = ManualClock(monotonic=1_000.0)
    holder = make_coordinator(owner_id="p1-aaaa")
    contender = make_coordinator(
        owner_id="p2-bbbb", clock=contender_clock, poll_interval=0.25, section_timeout=30.0
    )
    with holder.exclusive("writer.lease", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            contender.acquire_writer_lease(timeout=1.0)
    assert contender_clock.monotonic() - 1_000.0 <= 1.5
