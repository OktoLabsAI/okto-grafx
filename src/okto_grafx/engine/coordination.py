"""Engine-side coordination helpers (CONTRACT.md section 8.4, SPEC-M1 FR-7, BR-7, BR-10).

Pure orchestration over the coordination port: no clock, no threads, no timers. Everything that
needs to know the time takes the reading from its caller, which is what lets the transaction
manager, the API layer and a test drive renewal on their own schedule and get the same answer.

Three pieces:

* :class:`LeaseGuard` owns one lease, renews it when the caller says enough monotonic time has
  passed, and gives the epoch back to whoever is about to write.
* :class:`ReaderRegistration` owns one reader registration and its snapshot.
* :func:`recyclable_horizon` turns the reader horizon and the checkpoint into the single LSN
  below which WAL segments may be recycled. It is the whole of BR-10 in one expression, and C4
  calls exactly this function: recycling follows the horizon, never the absence of readers.
"""

from __future__ import annotations

from typing import Self

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxLeaseStolen,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import Epoch, Lsn
from okto_grafx.domain.ports.coordination import Lease, ProcessCoordinator, ReaderHandle

__all__ = [
    "COMMIT_SECTION",
    "DEFAULT_RENEWAL_FRACTION",
    "LeaseGuard",
    "ReaderRegistration",
    "recyclable_horizon",
]

COMMIT_SECTION: str = "commit"
"""Cross-process section shared by commit, checkpoint and recovery.

Every operation that decides, appends, replays, truncates or recycles WAL records uses this
same name.  Keeping the name beside the other coordination primitives prevents two callers from
silently serialising on different sections while believing they exclude one another.
"""

DEFAULT_RENEWAL_FRACTION: float = 1.0 / 3.0
"""Fraction of the lease TTL after which a renewal is due.

At one third, two renewals may fail before the owner looks stalled to anybody else.
"""


def _require_lsn(label: str, value: Lsn) -> Lsn:
    """Return the value when it is a usable LSN, else raise a typed configuration error."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {label} must be an integer.", field=label, value=repr(value)
        )
    if value < 0:
        raise GrafxConfigurationError(
            f"The {label} must not be negative.", field=label, value=value
        )
    return value


def recyclable_horizon(reader_horizon: Lsn | None, checkpoint_lsn: Lsn) -> Lsn:
    """Return the LSN below which a WAL segment may be recycled (BR-10, CONTRACT.md section 8.3).

    With a live reader the horizon is ``min(reader_horizon, checkpoint_lsn)``: neither the work a
    reader still needs nor the work the checkpoint has not covered may go away. With no live
    reader the horizon is the checkpoint and nothing more, because the absence of readers is not
    a licence to recycle everything: it only removes the reader constraint.

    ``reader_horizon`` of None means exactly that, no live reader, and never "horizon zero".
    """
    checkpoint = _require_lsn("checkpoint_lsn", checkpoint_lsn)
    if reader_horizon is None:
        return checkpoint
    pinned = _require_lsn("reader_horizon", reader_horizon)
    return pinned if pinned < checkpoint else checkpoint


class LeaseGuard:
    """One held writer lease, renewed on a schedule the caller drives with its own clock."""

    __slots__ = ("_coordinator", "_lease", "_renew_interval", "_last_renewal", "_released")

    def __init__(
        self,
        coordinator: ProcessCoordinator,
        lease: Lease,
        *,
        renew_interval: float | None = None,
    ) -> None:
        """Adopt a lease that the caller already acquired and fix its renewal interval."""
        ttl = float(lease.ttl_seconds)
        if not ttl > 0.0 or ttl != ttl:
            raise GrafxConfigurationError(
                "A lease with no positive time to live cannot be renewed on a schedule.",
                ttl_seconds=lease.ttl_seconds,
            )
        interval = (
            ttl * DEFAULT_RENEWAL_FRACTION if renew_interval is None else float(renew_interval)
        )
        if not interval > 0.0 or interval != interval:
            raise GrafxConfigurationError(
                "The renewal interval must be greater than zero.", renew_interval=renew_interval
            )
        if interval >= ttl:
            raise GrafxConfigurationError(
                "The renewal interval must be shorter than the lease time to live, or the lease "
                "expires before it is renewed.",
                renew_interval=interval,
                ttl_seconds=ttl,
            )
        self._coordinator: ProcessCoordinator = coordinator
        self._lease: Lease = lease
        self._renew_interval: float = interval
        self._last_renewal: float = float(lease.acquired_monotonic)
        self._released: bool = False

    @classmethod
    def acquire(
        cls,
        coordinator: ProcessCoordinator,
        *,
        timeout: float,
        renew_interval: float | None = None,
    ) -> Self:
        """Acquire the writer lease through the port and wrap it in a guard."""
        lease = coordinator.acquire_writer_lease(timeout=timeout)
        return cls(coordinator, lease, renew_interval=renew_interval)

    @property
    def lease(self) -> Lease:
        """Return the lease as it stands after the last successful renewal."""
        return self._lease

    @property
    def epoch(self) -> Epoch:
        """Return the epoch this guard authorises writes under."""
        return self._lease.epoch

    @property
    def owner_id(self) -> str:
        """Return the owner identity carried by the lease."""
        return self._lease.owner_id

    @property
    def renew_interval(self) -> float:
        """Return how much monotonic time may pass between two renewals."""
        return self._renew_interval

    @property
    def released(self) -> bool:
        """Return True once the lease has been released or found to be stolen."""
        return self._released

    def due_at(self) -> float:
        """Return the caller-clock reading at which the next renewal becomes due."""
        return self._last_renewal + self._renew_interval

    def renew_if_due(self, now_monotonic: float) -> bool:
        """Renew when the caller monotonic reading says the interval elapsed; return whether it did.

        The reading comes from the caller because this layer owns no clock. Passing a reading
        from a different process would be meaningless, which is precisely why the port keeps the
        monotonic source local.
        """
        self._require_live()
        now = float(now_monotonic)
        if now < self.due_at():
            return False
        self.renew(now)
        return True

    def renew(self, now_monotonic: float) -> Lease:
        """Renew the lease at the caller monotonic reading, and reschedule from that reading.

        The reading is required rather than optional. A lease carries the moment it was
        ACQUIRED, which the port keeps unchanged across renewals on purpose, so a renewal with
        nothing else to go on would reschedule from the original acquisition and leave every
        later tick due -- turning a heartbeat into a full control-record replacement per tick.
        This layer owns no clock, so the caller is the only place the answer can come from.

        The guard is marked released when the renewal reports that another owner took over, so
        no later call pretends the lease is still held.

        A reading that goes backwards breaks the contract of the port, which is why it can only
        ever move the schedule forwards here: honouring it would bring the next renewal closer
        and turn a caller mistake into extra load on the control plane.
        """
        self._require_live()
        try:
            self._lease = self._coordinator.renew_lease(self._lease)
        except GrafxLeaseStolen:
            self._released = True
            raise
        reading = float(now_monotonic)
        self._last_renewal = reading if reading > self._last_renewal else self._last_renewal
        return self._lease

    def validate(self) -> None:
        """Refuse the write path unless this epoch is still the published one (BR-7)."""
        self._require_live()
        self._coordinator.validate_epoch(self._lease.epoch)

    def release(self) -> None:
        """Release the lease through the port. Repeating it is a no-op, as is a stolen lease."""
        if self._released:
            return
        self._released = True
        self._coordinator.release_lease(self._lease)

    def _require_live(self) -> None:
        """Refuse to use a guard whose lease was released or taken over."""
        if self._released:
            raise GrafxUnsupportedOperation(
                "This lease guard was released and cannot be used again.",
                owner_id=self._lease.owner_id,
                epoch=self._lease.epoch,
            )

    def __enter__(self) -> Self:
        """Return the guard so a ``with`` block can use the lease it holds."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Release the lease on the way out, whether the block succeeded or raised.

        A release that fails while the block is already unwinding is swallowed: replacing the
        failure the caller is handling with a failure of the closing path hides the reason the
        block is being left at all. On a clean exit the failure is reported, because then it is
        the only thing that went wrong.
        """
        if exc_type is None:
            self.release()
            return
        try:
            self.release()
        except GrafxError:
            return

    def __repr__(self) -> str:
        """Return a representation naming the owner, the epoch and whether the guard is live."""
        return (
            f"LeaseGuard(owner_id={self._lease.owner_id!r}, epoch={self._lease.epoch}, "
            f"released={self._released})"
        )


class ReaderRegistration:
    """One live reader registration: the snapshot it pins and the heartbeat that keeps it alive."""

    __slots__ = ("_coordinator", "_handle", "_closed")

    def __init__(self, coordinator: ProcessCoordinator, handle: ReaderHandle) -> None:
        """Adopt a registration the caller already opened through the port."""
        self._coordinator: ProcessCoordinator = coordinator
        self._handle: ReaderHandle = handle
        self._closed: bool = False

    @classmethod
    def open(cls, coordinator: ProcessCoordinator, snapshot_lsn: Lsn) -> Self:
        """Register a reader at the given snapshot and wrap the handle."""
        pinned = _require_lsn("snapshot_lsn", snapshot_lsn)
        return cls(coordinator, coordinator.register_reader(pinned))

    @property
    def handle(self) -> ReaderHandle:
        """Return the handle this registration owns."""
        return self._handle

    @property
    def reader_id(self) -> str:
        """Return the identity of the registered reader."""
        return self._handle.reader_id

    @property
    def snapshot_lsn(self) -> Lsn:
        """Return the snapshot LSN this reader pins against recycling."""
        return self._handle.snapshot_lsn

    @property
    def closed(self) -> bool:
        """Return True once the registration has been withdrawn."""
        return self._closed

    def refresh(self) -> None:
        """Prove that this reader is still alive, re-publishing the registration when needed."""
        if self._closed:
            raise GrafxUnsupportedOperation(
                "This reader registration was closed and cannot be refreshed.",
                reader_id=self._handle.reader_id,
            )
        self._coordinator.refresh_reader(self._handle)

    def advance(self, snapshot_lsn: Lsn) -> None:
        """Move this registration's pin forward and prove it alive in the same publication.

        CE-2's deferred participant pin: the registration outlives any one transaction, so the
        position it holds against recycling must be able to FOLLOW the oldest open snapshot --
        forward only. A regression is refused here rather than published, because a pin that
        moves backward could re-cover segments the horizon already released (E-CE2-2; the C3
        port's ``refresh_reader`` republishes at the handle's pin, which is what makes this one
        write both the liveness proof and the new position).
        """
        if self._closed:
            raise GrafxUnsupportedOperation(
                "This reader registration was closed and cannot advance.",
                reader_id=self._handle.reader_id,
            )
        pinned = _require_lsn("snapshot_lsn", snapshot_lsn)
        if pinned < self._handle.snapshot_lsn:
            raise GrafxUnsupportedOperation(
                "A reader pin only ever advances; moving it backward could re-cover "
                "segments the horizon already released.",
                reader_id=self._handle.reader_id,
                pinned=self._handle.snapshot_lsn,
                requested=pinned,
            )
        advanced = ReaderHandle(reader_id=self._handle.reader_id, snapshot_lsn=pinned)
        # Publish can fail after the coordinator has replaced the durable record. Retaining the
        # old local handle in that uncertain outcome would let the next refresh regress a pin
        # that may already be visible at ``pinned``. Move the local monotone state first; a retry
        # can only republish the same safe position or advance it again.
        self._handle = advanced
        self._coordinator.refresh_reader(advanced)

    def close(self) -> None:
        """Withdraw the registration and release the snapshot it pinned. Repeating it is a no-op."""
        if self._closed:
            return
        self._closed = True
        self._coordinator.unregister_reader(self._handle)

    def __enter__(self) -> Self:
        """Return the registration so a ``with`` block can read under its snapshot."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Withdraw the registration on the way out, whether the block succeeded or raised.

        As with the lease guard, a failure to withdraw never replaces the failure that is already
        unwinding through this block.
        """
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except GrafxError:
            return

    def __repr__(self) -> str:
        """Return a representation naming the reader, its snapshot and whether it is closed."""
        return (
            f"ReaderRegistration(reader_id={self._handle.reader_id!r}, "
            f"snapshot_lsn={self._handle.snapshot_lsn}, closed={self._closed})"
        )
