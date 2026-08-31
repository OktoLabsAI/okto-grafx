"""Shared persistence for the published commit state.

``control/commit.state`` is a small control-plane record, but its ordering is part of the
durability contract.  Format 1 atomically replaces the whole record.  Format 2 alternates two
checksummed pages after the WAL commit is already durable, then barriers the updated page before
acknowledgement.  This store keeps both protocols in one place so commit and recovery cannot
drift apart when they publish or interpret the record.
"""

from __future__ import annotations

from okto_grafx.domain.control_record import (
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE, CommitState

__all__ = ["COMMIT_STATE_READ_ATTEMPTS", "CommitStateStore"]

COMMIT_STATE_READ_ATTEMPTS: int = 4
"""How many immediate attempts ride out a retryable device refusal while reading the state."""


class CommitStateStore:
    """Read and atomically publish the durable commit-state record for one participant."""

    __slots__ = ("_owner_id", "_slots", "_storage")

    def __init__(
        self,
        storage: StorageDevice,
        *,
        owner_id: str,
        database_uuid: bytes | None = None,
        file_nonce: int = 0,
        control_format_version: int = 1,
    ) -> None:
        """Bind a storage namespace and the owner identity used for the staging file.

        Owner identities are issued by the process coordinator, which already constrains them to
        portable logical-file characters.  Keeping the identity in the staging name prevents two
        participants preparing publications from sharing mutable temporary bytes.
        """
        self._storage: StorageDevice = storage
        self._owner_id: str = owner_id
        if type(control_format_version) is not int or control_format_version not in {
            1,
            2,
        }:
            raise GrafxConfigurationError(
                "The commit-state control format must be version 1 or 2.",
                field="control_format_version",
                value=repr(control_format_version),
            )
        if control_format_version == 2:
            if database_uuid is None:
                raise GrafxConfigurationError(
                    "Commit-state slot format 2 needs the database UUID.",
                    field="database_uuid",
                )
            self._slots: TwoSlotControlRecordStore | None = TwoSlotControlRecordStore(
                storage,
                file=COMMIT_STATE_FILE,
                record_kind=ControlRecordKind.COMMIT_STATE,
                database_uuid=database_uuid,
                file_nonce=file_nonce,
                temporary=self._temporary_file,
            )
            pin = getattr(storage, "pin_descriptor", None)
            if callable(pin):
                pin(COMMIT_STATE_FILE)
        else:
            self._slots = None

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
                if self._slots is not None:
                    record = self._slots.read()
                    return (
                        CommitState()
                        if record is None
                        else CommitState.decode(record.payload)
                    )
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
        """Publish ``state`` with the database's declared control-record protocol."""
        if self._slots is not None:
            self._slots.publish(state.encode())
            return
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
