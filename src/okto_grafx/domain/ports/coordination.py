"""The process coordination port (CONTRACT.md section 4.3, SPEC-M1 FR-7 and BR-7).

The coordinator says who holds the current epoch and which readers are alive. It does not
serialise transactions: two writers touching disjoint partitions must both be able to commit,
so the lease decides the epoch, never the order of the work.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from okto_grafx.domain.ids import Epoch, Lsn

__all__ = [
    "Lease",
    "ReaderHandle",
    "DeadOwnerReport",
    "ProcessCoordinator",
]


@dataclass(frozen=True, slots=True)
class Lease:
    """A durable claim on the writer role, valid for one epoch while its heartbeat keeps moving."""

    owner_id: str
    epoch: Epoch
    acquired_monotonic: float
    heartbeat_seq: int
    ttl_seconds: float


@dataclass(frozen=True, slots=True)
class ReaderHandle:
    """The registration of one live reader together with the snapshot LSN it pins."""

    reader_id: str
    snapshot_lsn: Lsn


@dataclass(frozen=True, slots=True)
class DeadOwnerReport:
    """Evidence that the current lease owner stopped making progress."""

    owner_id: str
    last_heartbeat_seq: int
    observed_stall_seconds: float   # measured with the OBSERVER's own monotonic clock


@runtime_checkable
class ProcessCoordinator(Protocol):
    """Cross-process agreement on the writer epoch, on live readers and on short critical sections."""

    def owner_id(self) -> str:
        """Return the stable identity of this participant."""
        ...

    def current_epoch(self) -> Epoch:
        """Return the epoch currently published by the lease."""
        ...

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        """Raise GrafxLeaseTimeout on expiry. Does NOT serialize whole transactions --
        it identifies the epoch holder."""
        ...

    def renew_lease(self, lease: Lease) -> Lease:
        """Extend the lease. Raise GrafxLeaseStolen when another owner took over."""
        ...

    def release_lease(self, lease: Lease) -> None:
        """Give up the writer role so another participant can take it without waiting for the stall threshold."""
        ...

    def validate_epoch(self, epoch: Epoch) -> None:
        """Raise GrafxStaleEpoch BEFORE any byte can reach the device."""
        ...

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None:
        """Return a report when the owner heartbeat has been still for longer than the threshold."""
        ...

    def takeover(self) -> Lease:
        """Increment epoch and become the owner. Must be atomic against concurrent takeovers."""
        ...

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle:
        """Publish a live reader and the snapshot it pins, so no segment it still needs is recycled."""
        ...

    def refresh_reader(self, handle: ReaderHandle) -> None:
        """Prove that a registered reader is still alive, republishing at the handle's pin.

        E-CE2-2: the handle names the pin the registration holds FROM NOW ON, so a caller may
        advance a deferred pin -- forward only, the engine refuses regressions -- with the same
        single publication that proves liveness. A registration a foreign observer pruned is
        recreated by this call rather than lost (BR-10 forbids evicting a live reader).
        """
        ...

    def unregister_reader(self, handle: ReaderHandle) -> None:
        """Remove a reader registration and release the snapshot it pinned."""
        ...

    def reader_horizon(self) -> Lsn | None:
        """Minimum snapshot_lsn over LIVE readers; None when there is no live reader.
        Dead readers (stalled heartbeat, observer-local monotonic) are pruned, never evicted."""
        ...

    def exclusive(self, name: str, *, timeout: float) -> AbstractContextManager[None]:
        """Short cross-process critical section. Used for the commit window and for takeover."""
        ...
