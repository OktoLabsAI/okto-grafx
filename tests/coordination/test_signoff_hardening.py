"""The four points raised after sign-off: no unguarded device call, no unowned release,
no renewal that leaves its own schedule in the past, and no way to configure the epoch guard off.

Each test here failed before its fix. Together they close the gap between what the docstrings of
this component promise and what its code does, which is the only gap a passing suite cannot see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, HookStorageDevice, ManualClock, owned_by
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator, decode_lease_record
from okto_grafx.domain.errors import (
    GrafxLeaseStolen,
    GrafxStaleEpoch,
    GrafxStorageError,
)
from okto_grafx.engine.coordination import LeaseGuard

# --- H1: every device call is inside the guard, and inside the retry ---------------------------


def _prepared(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> tuple[LocalProcessCoordinator, HookStorageDevice, ManualClock]:
    """Return a coordinator holding a lease, over a device that can be made to fail on demand."""
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-aaaa", clock=clock, storage=device)
    coordinator.acquire_writer_lease(timeout=1.0)
    coordinator.register_reader(42)
    return coordinator, device, clock


@pytest.mark.parametrize(
    "method",
    [
        "current_epoch",
        "reader_horizon",
    ],
)
def test_a_transient_failure_of_the_existence_probe_is_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path, method: str
) -> None:
    # The probe that opens every read is one device call like any other, and a sharing violation
    # on it is exactly as transient as one on the read that follows it (TR-3).
    coordinator, device, _clock = _prepared(make_coordinator, database_root)
    device.fail_next["exists"] = 1
    assert getattr(coordinator, method)() is not None


def test_a_permanent_failure_of_the_existence_probe_is_typed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator, device, _clock = _prepared(make_coordinator, database_root)
    device.fail_next["exists"] = 10_000
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.current_epoch()
    assert failure.value.details["attempts"] >= 2
    assert failure.value.details["file"].endswith("writer.lease")


@pytest.mark.parametrize(
    "method",
    ["current_epoch", "detect_dead_owner", "acquire_writer_lease", "validate_epoch"],
)
def test_no_public_method_lets_a_device_failure_escape_untyped(
    make_coordinator: CoordinatorFactory, database_root: Path, method: str
) -> None:
    coordinator, device, _clock = _prepared(make_coordinator, database_root)
    arguments: dict[str, dict[str, object]] = {
        "detect_dead_owner": {"stall_threshold": 5.0},
        "acquire_writer_lease": {"timeout": 0.0},
    }
    device.fail_next["exists"] = 10_000
    with pytest.raises(GrafxStorageError):
        if method == "validate_epoch":
            coordinator.validate_epoch(1)
        else:
            getattr(coordinator, method)(**arguments.get(method, {}))


def test_a_takeover_that_has_to_read_first_reports_a_device_failure_typed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A takeover with no observation of its own reads the lease to establish one, and that read
    # is the first device call of the method.
    _holder, device, _clock = _prepared(make_coordinator, database_root)
    fresh = make_coordinator(owner_id="p2-fresh", monotonic_origin=50.0, storage=device)
    device.fail_next["exists"] = 10_000
    with pytest.raises(GrafxStorageError):
        fresh.takeover()


def test_a_transient_failure_of_the_reader_listing_is_ridden_out(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator, device, _clock = _prepared(make_coordinator, database_root)
    device.fail_next["list_files"] = 1
    assert coordinator.reader_horizon() == 42


def test_a_permanent_failure_of_the_reader_listing_is_typed(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    coordinator, device, _clock = _prepared(make_coordinator, database_root)
    device.fail_next["list_files"] = 10_000
    with pytest.raises(GrafxStorageError) as failure:
        coordinator.reader_horizon()
    assert failure.value.details["attempts"] >= 2


# --- H2: a release is as owned as a renewal ----------------------------------------------------


def test_a_participant_that_never_held_the_lease_cannot_vacate_it(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Two participants sharing an owner identifier is a composition mistake, and the guard has to
    # fail closed on it in both directions: the renewal already did, the release did not.
    holder = make_coordinator(owner_id="p1-same")
    impostor = make_coordinator(owner_id="p1-same", monotonic_origin=40.0)
    lease = holder.acquire_writer_lease(timeout=1.0)

    with pytest.raises(GrafxLeaseStolen):
        impostor.renew_lease(lease)
    with pytest.raises(GrafxLeaseStolen):
        impostor.release_lease(lease)

    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.held is True
    assert owned_by(published.owner_id, "p1-same")
    holder.validate_epoch(lease.epoch)


def test_the_true_holder_can_still_release_quietly_after_being_taken_over(
    make_coordinator: CoordinatorFactory
) -> None:
    # The other half of the rule: a participant that DID hold this lease and lost it must still
    # be able to close without raising, which is what release_lease promises.
    holder = make_coordinator(owner_id="p1-holder")
    lease = holder.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=7_000.0)
    successor = make_coordinator(owner_id="p2-next", clock=clock)
    assert successor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert successor.detect_dead_owner(stall_threshold=5.0) is not None
    successor.takeover()
    with pytest.raises(GrafxLeaseStolen):
        holder.renew_lease(lease)
    holder.release_lease(lease)
    holder.release_lease(lease)
    assert successor.current_epoch() == 2


def test_a_released_lease_stays_releasable_by_its_own_holder(
    make_coordinator: CoordinatorFactory
) -> None:
    holder = make_coordinator(owner_id="p1-holder")
    lease = holder.acquire_writer_lease(timeout=1.0)
    holder.release_lease(lease)
    holder.release_lease(lease)
    with pytest.raises(GrafxStaleEpoch):
        holder.validate_epoch(lease.epoch)


# --- H4: the epoch guard is not configurable -------------------------------------------------


def test_the_epoch_guard_cannot_be_configured_off(
    database_root: Path, lock_directory: str
) -> None:
    # CONTRACT section 4.3 states the guarantee unconditionally, and section 8.5 step 2 puts a
    # validate_epoch OUTSIDE the commit section precisely as the before-any-device-call guard.
    # A knob that turns that step into a no-op is a switch for the central safety property.
    with pytest.raises(TypeError):
        LocalProcessCoordinator(
            DirectoryStorageDevice(database_root),
            ManualClock(),
            owner_id="p1-aaaa",
            lock_directory=lock_directory,
            epoch_cache_seconds=60.0,  # type: ignore[call-arg]
        )


def test_a_stale_holder_is_refused_outside_any_section(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = HookStorageDevice(DirectoryStorageDevice(database_root))
    stale = make_coordinator(owner_id="p1-stale", storage=device)
    lease = stale.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=6_000_000.0)
    successor = make_coordinator(owner_id="p2-next", clock=clock)
    assert successor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert successor.detect_dead_owner(stall_threshold=5.0) is not None
    successor.takeover()

    device.reset()
    device.forbid_writes = True
    for _attempt in range(5):
        with pytest.raises(GrafxStaleEpoch):
            stale.validate_epoch(lease.epoch)
    with pytest.raises(GrafxStaleEpoch):
        with stale.exclusive("commit", timeout=1.0):
            stale.validate_epoch(lease.epoch)
    assert device.forbid_writes is True, "the device was not armed, so nothing was proved"
    assert device.calls, "the guard did read the published record"


# --- H3: a renewal never leaves its own schedule in the past ----------------------------------


class ScheduleCoordinator:
    """The smallest coordinator a lease guard needs, recording every renewal it is asked for."""

    def __init__(self) -> None:
        self.renewals = 0

    def acquire_writer_lease(self, *, timeout: float) -> object:
        """Grant a lease acquired at monotonic reading 100 with a time to live of six seconds."""
        from okto_grafx.domain.ports.coordination import Lease

        return Lease(
            owner_id="spy",
            epoch=1,
            acquired_monotonic=100.0,
            heartbeat_seq=1,
            ttl_seconds=6.0,
        )

    def renew_lease(self, lease: object) -> object:
        """Advance the heartbeat, leaving acquired_monotonic where the adapter leaves it."""
        from okto_grafx.domain.ports.coordination import Lease

        self.renewals += 1
        return Lease(
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            acquired_monotonic=lease.acquired_monotonic,
            heartbeat_seq=lease.heartbeat_seq + 1,
            ttl_seconds=lease.ttl_seconds,
        )

    def release_lease(self, lease: object) -> None:
        """Accept the release without recording anything."""

    def validate_epoch(self, epoch: int) -> None:
        """Accept every epoch; this stand-in is about scheduling only."""


def test_a_renewal_must_say_when_it_happened() -> None:
    # The adapter carries acquired_monotonic forward unchanged, by design, so a renewal that does
    # not say when it happened has nothing to move the schedule with: it would leave due_at
    # pinned to the original acquisition and make every later tick due.
    guard = LeaseGuard.acquire(ScheduleCoordinator(), timeout=1.0)
    with pytest.raises(TypeError):
        guard.renew()  # type: ignore[call-arg]


def test_an_out_of_band_renewal_advances_the_schedule() -> None:
    coordinator = ScheduleCoordinator()
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    assert guard.due_at() == pytest.approx(102.0)
    guard.renew(105.0)
    assert guard.due_at() == pytest.approx(107.0)
    assert [guard.renew_if_due(tick) for tick in (105.1, 106.0, 106.9)] == [False, False, False]
    assert guard.renew_if_due(107.0) is True
    assert coordinator.renewals == 2
    assert guard.due_at() == pytest.approx(109.0)


# --- the two notes: namespace identity, and whose stall threshold decides a takeover -----------


def test_a_shared_namespace_token_makes_two_devices_one_namespace(
    make_coordinator: CoordinatorFactory, database_root: Path, tmp_path: Path
) -> None:
    # The token is matched by identity, so passing the same OBJECT is what joins two coordinators
    # that reach one store through different device objects.
    token = object()
    inner = DirectoryStorageDevice(database_root)
    first = make_coordinator(
        owner_id="p1-aaaa",
        storage=HookStorageDevice(inner),
        use_lock_directory=False,
        namespace=token,
    )
    second = make_coordinator(
        owner_id="p2-bbbb",
        storage=HookStorageDevice(inner),
        use_lock_directory=False,
        namespace=token,
        monotonic_origin=50.0,
    )
    from okto_grafx.domain.errors import GrafxLeaseTimeout

    with first.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with second.exclusive("commit", timeout=0.2):
                pytest.fail("the section was granted twice at once")

    apart = make_coordinator(
        owner_id="p3-cccc",
        storage=DirectoryStorageDevice(tmp_path / "other"),
        use_lock_directory=False,
        namespace=object(),
        monotonic_origin=90.0,
    )
    with first.exclusive("commit", timeout=1.0):
        with apart.exclusive("commit", timeout=0.2):
            pass


def test_a_shorter_detection_threshold_cannot_lower_the_bar_for_a_takeover(
    make_coordinator: CoordinatorFactory
) -> None:
    # detect_dead_owner answers the question the caller asked; takeover applies its own
    # configured threshold, so a caller can be more cautious than the configuration, never less.
    from okto_grafx.domain.errors import GrafxLeaseTimeout

    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(
        owner_id="p1-alive", clock=clock, owner_stall_threshold=10.0, ttl_seconds=10.0
    )
    assert survivor.detect_dead_owner(stall_threshold=1.0) is None
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=1.0) is not None
    with pytest.raises(GrafxLeaseTimeout) as refusal:
        survivor.takeover()
    assert refusal.value.details["stall_threshold_seconds"] == 10.0
    clock.advance(5.0)
    assert survivor.detect_dead_owner(stall_threshold=1.0) is not None
    assert survivor.takeover().epoch == 2


# --- the notes from the final pass ------------------------------------------------------------


def test_a_backwards_reading_cannot_pull_the_renewal_schedule_forwards() -> None:
    from okto_grafx.engine.coordination import LeaseGuard as Guard

    guard = Guard.acquire(ScheduleCoordinator(), timeout=1.0)
    guard.renew(105.0)
    assert guard.due_at() == pytest.approx(107.0)
    guard.renew(90.0)
    assert guard.due_at() == pytest.approx(107.0)
    assert guard.renew_if_due(106.9) is False


def test_a_wait_on_a_frozen_clock_gives_up_quickly(
    make_coordinator: CoordinatorFactory
) -> None:
    # The bound exists for an injected clock that never advances. It has to stop such a wait in
    # milliseconds, and it must be unreachable by a wait whose clock does move.
    frozen = ManualClock(monotonic=1_000.0)
    holder = make_coordinator(owner_id="p1-aaaa")
    stuck = make_coordinator(
        owner_id="p2-bbbb", clock=frozen, poll_interval=0.001, sleeper=lambda _seconds: None
    )
    from okto_grafx.domain.errors import GrafxLeaseTimeout

    with holder.exclusive("commit", timeout=1.0):
        with pytest.raises(GrafxLeaseTimeout):
            with stuck.exclusive("commit", timeout=1.0):
                pass
    assert frozen.monotonic() == 1_000.0


def test_releasing_a_superseded_lease_this_participant_held_is_quiet(
    make_coordinator: CoordinatorFactory
) -> None:
    # release_lease promises that closing never raises for a lease this participant held, and a
    # guard wrapping an older epoch has to be able to close a clean block.
    from okto_grafx.engine.coordination import LeaseGuard as Guard

    zombie = make_coordinator(owner_id="p0-dead", monotonic_origin=500.0)
    zombie.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=1_000.0)
    survivor = make_coordinator(owner_id="p1-alive", clock=clock)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    older = survivor.takeover()

    # The same participant is later handed the role again at a higher epoch.
    other = make_coordinator(owner_id="p2-other", monotonic_origin=20.0)
    other.detect_dead_owner(stall_threshold=5.0)
    clock.advance(6.0)
    assert survivor.detect_dead_owner(stall_threshold=5.0) is not None
    newer = survivor.takeover()
    assert newer.epoch == older.epoch + 1

    survivor.release_lease(older)
    with Guard(survivor, older):
        pass
    assert survivor.current_epoch() == newer.epoch
    survivor.validate_epoch(newer.epoch)
