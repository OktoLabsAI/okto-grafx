"""The pure engine-side helpers: BR-10 arithmetic, lease guard, reader registration.

Everything here is exercised against a recording stand-in for the port, which is the point: the
engine layer must work through the port alone, with no clock, no thread and no file of its own.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ports.coordination import Lease, ProcessCoordinator, ReaderHandle
from okto_grafx.engine.coordination import (
    DEFAULT_RENEWAL_FRACTION,
    LeaseGuard,
    ReaderRegistration,
    recyclable_horizon,
)


class SpyCoordinator:
    """A ProcessCoordinator that records what the engine asked of it and nothing more."""

    def __init__(self, *, epoch: int = 4, ttl: float = 6.0) -> None:
        self.calls: list[tuple[str, object]] = []
        self.epoch = epoch
        self.ttl = ttl
        self.heartbeat = 1
        self.readers: dict[str, int] = {}
        self.steal_on_renew = False
        self.grant_timeout = False
        self.reader_counter = 0

    def owner_id(self) -> str:
        """Return the identity of this stand-in."""
        return "spy"

    def current_epoch(self) -> int:
        """Return the published epoch."""
        return self.epoch

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        """Grant a lease, or refuse when the test asked for a refusal."""
        self.calls.append(("acquire", timeout))
        if self.grant_timeout:
            raise GrafxLeaseTimeout("No lease for you.", timeout_seconds=timeout)
        return Lease(
            owner_id="spy",
            epoch=self.epoch,
            acquired_monotonic=100.0,
            heartbeat_seq=self.heartbeat,
            ttl_seconds=self.ttl,
        )

    def renew_lease(self, lease: Lease) -> Lease:
        """Advance the heartbeat, or report the lease as stolen."""
        self.calls.append(("renew", lease.heartbeat_seq))
        if self.steal_on_renew:
            raise GrafxLeaseStolen("Another owner took over.", epoch=lease.epoch)
        self.heartbeat = lease.heartbeat_seq + 1
        return Lease(
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            acquired_monotonic=lease.acquired_monotonic,
            heartbeat_seq=self.heartbeat,
            ttl_seconds=lease.ttl_seconds,
        )

    def release_lease(self, lease: Lease) -> None:
        """Record the release."""
        self.calls.append(("release", lease.epoch))

    def validate_epoch(self, epoch: int) -> None:
        """Record the validation."""
        self.calls.append(("validate", epoch))

    def detect_dead_owner(self, *, stall_threshold: float) -> None:
        """Report nobody dead."""
        self.calls.append(("detect", stall_threshold))
        return None

    def takeover(self) -> Lease:
        """Take the next epoch."""
        self.epoch += 1
        return self.acquire_writer_lease(timeout=0.0)

    def register_reader(self, snapshot_lsn: int) -> ReaderHandle:
        """Register a reader and remember its snapshot."""
        self.reader_counter += 1
        reader_id = f"spy-r{self.reader_counter}"
        self.readers[reader_id] = snapshot_lsn
        self.calls.append(("register", snapshot_lsn))
        return ReaderHandle(reader_id=reader_id, snapshot_lsn=snapshot_lsn)

    def refresh_reader(self, handle: ReaderHandle) -> None:
        """Record the refresh."""
        self.readers[handle.reader_id] = handle.snapshot_lsn
        self.calls.append(("refresh", handle.reader_id))

    def unregister_reader(self, handle: ReaderHandle) -> None:
        """Forget a reader."""
        self.readers.pop(handle.reader_id, None)
        self.calls.append(("unregister", handle.reader_id))

    def reader_horizon(self) -> int | None:
        """Return the minimum snapshot over the registered readers."""
        return min(self.readers.values()) if self.readers else None

    @contextmanager
    def exclusive(self, name: str, *, timeout: float) -> Iterator[None]:
        """Enter a section that does nothing but record itself."""
        self.calls.append(("exclusive", name))
        yield


def test_the_stand_in_satisfies_the_port() -> None:
    assert isinstance(SpyCoordinator(), ProcessCoordinator)


# --- recyclable_horizon (BR-10) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("reader_horizon", "checkpoint_lsn", "expected"),
    [
        (None, 0, 0),
        (None, 1, 1),
        (None, 5_000, 5_000),
        (1, 5_000, 1),
        (4_999, 5_000, 4_999),
        (5_000, 5_000, 5_000),
        (5_001, 5_000, 5_000),
        (9_999_999, 5_000, 5_000),
        (0, 5_000, 0),
        (0, 0, 0),
        (7, 0, 0),
    ],
)
def test_the_recyclable_horizon_truth_table(
    reader_horizon: int | None, checkpoint_lsn: int, expected: int
) -> None:
    assert recyclable_horizon(reader_horizon, checkpoint_lsn) == expected


def test_no_live_reader_does_not_mean_recycle_everything() -> None:
    # The trap BR-10 names: with no reader the horizon is the checkpoint, never the end of the
    # log, and certainly never "whatever is there".
    assert recyclable_horizon(None, 900) == 900
    assert recyclable_horizon(None, 0) == 0


def test_a_reader_behind_the_checkpoint_decides_the_horizon() -> None:
    assert recyclable_horizon(10, 900) == 10


def test_a_reader_ahead_of_the_checkpoint_cannot_push_the_horizon_past_it() -> None:
    assert recyclable_horizon(1_000, 900) == 900


@pytest.mark.parametrize(
    ("reader_horizon", "checkpoint_lsn"),
    [(-1, 10), (10, -1), ("10", 10), (10, "10"), (1.5, 10), (10, None)],
)
def test_an_impossible_horizon_is_refused(reader_horizon: object, checkpoint_lsn: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        recyclable_horizon(reader_horizon, checkpoint_lsn)  # type: ignore[arg-type]


def test_a_boolean_is_not_an_lsn() -> None:
    with pytest.raises(GrafxConfigurationError):
        recyclable_horizon(True, 10)  # type: ignore[arg-type]


# --- LeaseGuard --------------------------------------------------------------------------------


def test_the_guard_renews_when_the_caller_says_the_interval_elapsed() -> None:
    coordinator = SpyCoordinator(ttl=6.0)
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    assert guard.renew_interval == pytest.approx(6.0 * DEFAULT_RENEWAL_FRACTION)
    assert guard.due_at() == pytest.approx(102.0)
    assert guard.renew_if_due(101.9) is False
    assert guard.renew_if_due(102.0) is True
    assert guard.lease.heartbeat_seq == 2
    assert guard.renew_if_due(102.0) is False
    assert guard.renew_if_due(104.5) is True
    assert [call for call in coordinator.calls if call[0] == "renew"] == [("renew", 1), ("renew", 2)]


def test_the_guard_takes_an_explicit_interval() -> None:
    coordinator = SpyCoordinator(ttl=6.0)
    guard = LeaseGuard.acquire(coordinator, timeout=1.0, renew_interval=0.5)
    assert guard.renew_interval == 0.5
    assert guard.renew_if_due(100.4) is False
    assert guard.renew_if_due(100.5) is True


@pytest.mark.parametrize("interval", [0.0, -1.0, 6.0, 7.0])
def test_an_interval_that_cannot_keep_the_lease_alive_is_refused(interval: float) -> None:
    coordinator = SpyCoordinator(ttl=6.0)
    with pytest.raises(GrafxConfigurationError):
        LeaseGuard.acquire(coordinator, timeout=1.0, renew_interval=interval)


def test_a_lease_without_a_time_to_live_is_refused() -> None:
    coordinator = SpyCoordinator(ttl=0.0)
    with pytest.raises(GrafxConfigurationError):
        LeaseGuard.acquire(coordinator, timeout=1.0)


def test_the_guard_validates_through_the_port() -> None:
    coordinator = SpyCoordinator(epoch=9)
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    assert guard.epoch == 9
    assert guard.owner_id == "spy"
    guard.validate()
    assert ("validate", 9) in coordinator.calls


def test_the_guard_releases_once_and_only_once() -> None:
    coordinator = SpyCoordinator()
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    assert guard.released is False
    guard.release()
    guard.release()
    assert guard.released is True
    assert [call for call in coordinator.calls if call[0] == "release"] == [("release", 4)]


def test_the_guard_releases_on_the_way_out_of_its_block() -> None:
    coordinator = SpyCoordinator()
    with LeaseGuard.acquire(coordinator, timeout=1.0) as guard:
        assert guard.epoch == 4
    assert guard.released is True
    assert [call for call in coordinator.calls if call[0] == "release"] == [("release", 4)]


def test_the_guard_releases_when_the_block_raises() -> None:
    coordinator = SpyCoordinator()
    with pytest.raises(RuntimeError):
        with LeaseGuard.acquire(coordinator, timeout=1.0):
            raise RuntimeError("the work failed")
    assert [call for call in coordinator.calls if call[0] == "release"] == [("release", 4)]


def test_a_stolen_lease_ends_the_guard_rather_than_pretending() -> None:
    coordinator = SpyCoordinator()
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    coordinator.steal_on_renew = True
    with pytest.raises(GrafxLeaseStolen):
        guard.renew_if_due(1_000.0)
    assert guard.released is True
    for use in (guard.validate, lambda: guard.renew_if_due(2_000.0)):
        with pytest.raises(GrafxUnsupportedOperation):
            use()
    guard.release()
    assert [call for call in coordinator.calls if call[0] == "release"] == []


def test_a_released_guard_refuses_to_be_used_again() -> None:
    coordinator = SpyCoordinator()
    guard = LeaseGuard.acquire(coordinator, timeout=1.0)
    guard.release()
    with pytest.raises(GrafxUnsupportedOperation):
        guard.validate()
    with pytest.raises(GrafxUnsupportedOperation):
        guard.renew(200.0)


def test_the_guard_reports_the_failure_to_acquire() -> None:
    coordinator = SpyCoordinator()
    coordinator.grant_timeout = True
    with pytest.raises(GrafxLeaseTimeout):
        LeaseGuard.acquire(coordinator, timeout=0.25)


def test_the_guard_can_adopt_a_lease_the_caller_already_holds() -> None:
    coordinator = SpyCoordinator()
    lease = coordinator.acquire_writer_lease(timeout=0.0)
    guard = LeaseGuard(coordinator, lease)
    assert guard.lease is lease
    assert repr(guard).startswith("LeaseGuard(owner_id='spy'")


# --- ReaderRegistration ------------------------------------------------------------------------


def test_the_registration_opens_refreshes_and_closes_through_the_port() -> None:
    coordinator = SpyCoordinator()
    registration = ReaderRegistration.open(coordinator, 512)
    assert registration.snapshot_lsn == 512
    assert registration.reader_id == "spy-r1"
    assert registration.closed is False
    registration.refresh()
    registration.refresh()
    registration.close()
    registration.close()
    assert coordinator.calls == [
        ("register", 512),
        ("refresh", "spy-r1"),
        ("refresh", "spy-r1"),
        ("unregister", "spy-r1"),
    ]


def test_the_registration_closes_on_the_way_out_of_its_block() -> None:
    coordinator = SpyCoordinator()
    with ReaderRegistration.open(coordinator, 7) as registration:
        assert coordinator.reader_horizon() == 7
        assert isinstance(registration.handle, ReaderHandle)
    assert registration.closed is True
    assert coordinator.reader_horizon() is None


def test_the_registration_closes_when_the_block_raises() -> None:
    coordinator = SpyCoordinator()
    with pytest.raises(RuntimeError):
        with ReaderRegistration.open(coordinator, 7):
            raise RuntimeError("the read failed")
    assert coordinator.reader_horizon() is None


def test_a_closed_registration_refuses_to_be_refreshed() -> None:
    coordinator = SpyCoordinator()
    registration = ReaderRegistration.open(coordinator, 7)
    registration.close()
    with pytest.raises(GrafxUnsupportedOperation):
        registration.refresh()
    assert repr(registration).startswith("ReaderRegistration(reader_id='spy-r1'")


def test_a_registration_advances_its_pin_forward_in_the_refresh_publication() -> None:
    coordinator = SpyCoordinator()
    registration = ReaderRegistration.open(coordinator, 7)
    registration.advance(11)
    assert registration.snapshot_lsn == 11
    assert coordinator.reader_horizon() == 11
    assert coordinator.calls == [("register", 7), ("refresh", "spy-r1")]


def test_a_registration_refuses_to_regress_or_advance_after_close() -> None:
    coordinator = SpyCoordinator()
    registration = ReaderRegistration.open(coordinator, 7)
    with pytest.raises(GrafxUnsupportedOperation):
        registration.advance(6)
    assert registration.snapshot_lsn == 7
    registration.close()
    with pytest.raises(GrafxUnsupportedOperation):
        registration.advance(8)


def test_a_snapshot_that_is_not_an_lsn_is_refused() -> None:
    coordinator = SpyCoordinator()
    with pytest.raises(GrafxConfigurationError):
        ReaderRegistration.open(coordinator, -1)


def test_the_horizon_of_several_registrations_is_the_minimum() -> None:
    coordinator = SpyCoordinator()
    first = ReaderRegistration.open(coordinator, 900)
    second = ReaderRegistration.open(coordinator, 120)
    assert recyclable_horizon(coordinator.reader_horizon(), 5_000) == 120
    second.close()
    assert recyclable_horizon(coordinator.reader_horizon(), 5_000) == 900
    first.close()
    assert recyclable_horizon(coordinator.reader_horizon(), 5_000) == 5_000


# --- the closing path never masks the failure it is unwinding ---------------------------------


class BrittleCoordinator(SpyCoordinator):
    """A coordinator whose control plane has become unreachable at exactly the wrong moment."""

    def release_lease(self, lease: Lease) -> None:
        """Fail the way a device does when the control file cannot be written."""
        self.calls.append(("release", lease.epoch))
        raise GrafxStorageError("The control plane is unreachable.", attempts=5)

    def unregister_reader(self, handle: ReaderHandle) -> None:
        """Fail the same way for a reader registration."""
        self.calls.append(("unregister", handle.reader_id))
        raise GrafxStorageError("The control plane is unreachable.", attempts=5)


def test_the_guard_does_not_mask_the_exception_it_is_unwinding() -> None:
    coordinator = BrittleCoordinator()
    with pytest.raises(RuntimeError, match="the work failed"):
        with LeaseGuard.acquire(coordinator, timeout=1.0):
            raise RuntimeError("the work failed")
    assert [call for call in coordinator.calls if call[0] == "release"] == [("release", 4)]


def test_the_guard_reports_a_release_failure_of_a_clean_block() -> None:
    coordinator = BrittleCoordinator()
    with pytest.raises(GrafxStorageError):
        with LeaseGuard.acquire(coordinator, timeout=1.0):
            pass


def test_the_registration_does_not_mask_the_exception_it_is_unwinding() -> None:
    coordinator = BrittleCoordinator()
    with pytest.raises(RuntimeError, match="the read failed"):
        with ReaderRegistration.open(coordinator, 5):
            raise RuntimeError("the read failed")


def test_the_registration_reports_a_close_failure_of_a_clean_block() -> None:
    coordinator = BrittleCoordinator()
    with pytest.raises(GrafxStorageError):
        with ReaderRegistration.open(coordinator, 5):
            pass
