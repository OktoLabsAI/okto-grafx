"""Shared persistence for the published commit state.

``control/commit.state`` is a small control-plane record, but its ordering is part of the
durability contract: readers may observe the new state only after its complete payload has been
made durable and atomically installed.  This store keeps that protocol in one place so commit
and recovery cannot drift apart when they publish or interpret the record.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState

__all__ = ["COMMIT_STATE_READ_ATTEMPTS", "CommitStateStore"]

COMMIT_STATE_READ_ATTEMPTS: int = 4
"""How many immediate attempts ride out a retryable device refusal while reading the state."""


class CommitStateStore:
    """Read and atomically publish the durable commit-state record for one participant."""

    __slots__ = ("_owner_id", "_storage")

    def __init__(self, storage: StorageDevice, *, owner_id: str) -> None:
        """Bind a storage namespace and the owner identity used for the staging file.

        Owner identities are issued by the process coordinator, which already constrains them to
        portable logical-file characters.  Keeping the identity in the staging name prevents two
        participants preparing publications from sharing mutable temporary bytes.
        """
        self._storage: StorageDevice = storage
        self._owner_id: str = owner_id

    def read(self) -> CommitState:
        """Return the published state, or an empty state only when the file is absent.

        Retryable device refusals are attempted four times without sleeping: this engine layer
        owns no clock and the adapter below has already applied its own backoff.  Existing but
        malformed bytes are never interpreted as absence; ``CommitState.decode`` therefore
        propagates corruption and schema-version errors unchanged.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                if not self._storage.exists(COMMIT_STATE_FILE):
                    return CommitState()
                size = self._storage.log_size(COMMIT_STATE_FILE)
                payload = self._storage.read_log(COMMIT_STATE_FILE, 0, size)
                return CommitState.decode(payload)
            except GrafxError as failure:
                retryable = failure.details.get("retryable", failure.retryable)
                if attempts < COMMIT_STATE_READ_ATTEMPTS and retryable is True:
                    continue
                raise

    def checkpoint_hint(self) -> Lsn:
        """Return the checkpoint hint, or zero when no trustworthy hint can be read.

        This is deliberately the one lenient door.  Recovery may safely redo more work when the
        control-plane hint is absent, damaged, from a newer build, or temporarily inaccessible;
        callers that need the authoritative published state must use :meth:`read`, which never
        hides those conditions.
        """
        try:
            return self.read().checkpoint_lsn
        except GrafxError:
            return NO_LSN

    def publish(self, state: CommitState) -> None:
        """Durably stage ``state`` and atomically replace the published record with it."""
        temporary = self._temporary_file
        if self._storage.exists(temporary):
            self._storage.remove(temporary)
        self._storage.create(temporary, exclusive=True)
        self._storage.append_log(temporary, state.encode())
        self._storage.durable_barrier(temporary)
        self._storage.atomic_replace(temporary, COMMIT_STATE_FILE)
        self._storage.durable_barrier(COMMIT_STATE_FILE)

    @property
    def _temporary_file(self) -> str:
        """Return the participant-exclusive staging name used for atomic publication."""
        return f"{COMMIT_STATE_FILE}.{self._owner_id}.tmp"
