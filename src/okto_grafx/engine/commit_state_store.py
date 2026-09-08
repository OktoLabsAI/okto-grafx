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
    ControlRecordReader,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_LEGACY_FORMAT_VERSION,
    CommitState,
)

__all__ = ["COMMIT_STATE_READ_ATTEMPTS", "CommitStateStore"]

COMMIT_STATE_READ_ATTEMPTS: int = 4
"""How many immediate attempts ride out a retryable device refusal while reading the state."""

_UNREAD: object = object()
_DAMAGED: object = object()


class CommitStateStore:
    """Read and atomically publish the durable commit-state record for one participant."""

    __slots__ = (
        "_observed_payload",
        "_observed_valid_payloads",
        "_owner_id",
        "_slots",
        "_storage",
    )

    def __init__(
        self,
        storage: StorageDevice,
        *,
        owner_id: str,
        database_uuid: bytes | None = None,
        file_nonce: int = 0,
        control_format_version: int = 1,
        control_read_if_exists: ControlRecordReader | None = None,
    ) -> None:
        """Bind a storage namespace and the owner identity used for the staging file.

        Owner identities are issued by the process coordinator, which already constrains them to
        portable logical-file characters.  Keeping the identity in the staging name prevents two
        participants preparing publications from sharing mutable temporary bytes.
        """
        self._storage: StorageDevice = storage
        self._owner_id: str = owner_id
        self._observed_payload: bytes | None | object = _UNREAD
        self._observed_valid_payloads: tuple[bytes, ...] | object = _UNREAD
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
                read_if_exists=control_read_if_exists,
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
                    try:
                        record = self._slots.read()
                    except GrafxCorruptionDetected as failure:
                        reconstructible = self._slots.damage_is_replaceable(failure)
                        valid_payloads = (
                            self._slots.valid_payloads_behind_replaceable_damage(
                                failure
                            )
                            if reconstructible
                            else ()
                        )
                        minimum_format = COMMIT_STATE_LEGACY_FORMAT_VERSION
                        for valid_payload in valid_payloads:
                            try:
                                valid_state = CommitState.decode(valid_payload)
                            except GrafxCorruptionDetected:
                                continue
                            minimum_format = max(
                                minimum_format, valid_state.format_version
                            )
                        self._observed_valid_payloads = valid_payloads
                        failure.details["commit_state_reconstructible"] = (
                            reconstructible
                        )
                        failure.details["commit_state_minimum_format_version"] = (
                            minimum_format
                        )
                        raise
                    valid_payloads = () if record is None else record.valid_payloads
                    payload = None if record is None else record.payload
                    try:
                        state = (
                            CommitState()
                            if record is None
                            else CommitState.decode(record.payload)
                        )
                    except GrafxCorruptionDetected as failure:
                        minimum_format = COMMIT_STATE_LEGACY_FORMAT_VERSION
                        for fallback in valid_payloads[1:]:
                            try:
                                fallback_state = CommitState.decode(fallback)
                            except GrafxCorruptionDetected:
                                pass
                            else:
                                minimum_format = max(
                                    minimum_format, fallback_state.format_version
                                )
                        self._observed_valid_payloads = valid_payloads
                        failure.details["commit_state_reconstructible"] = True
                        failure.details["commit_state_minimum_format_version"] = (
                            minimum_format
                        )
                        raise
                    for fallback in valid_payloads[1:]:
                        try:
                            fallback_state = CommitState.decode(fallback)
                        except GrafxCorruptionDetected:
                            pass
                        else:
                            if fallback_state.format_version > state.format_version:
                                raise GrafxCorruptionDetected(
                                    "An older physical commit-state slot carries a newer capability fence.",
                                    file=COMMIT_STATE_FILE,
                                    field="format_version",
                                    current=state.format_version,
                                    fallback=fallback_state.format_version,
                                    commit_state_reconstructible=False,
                                )
                    self._observed_payload = payload
                    self._observed_valid_payloads = valid_payloads
                    return state
                if not self._storage.exists(COMMIT_STATE_FILE):
                    self._observed_payload = None
                    self._observed_valid_payloads = ()
                    return CommitState()
                size = self._storage.log_size(COMMIT_STATE_FILE)
                payload = self._storage.read_log(COMMIT_STATE_FILE, 0, size)
                try:
                    state = CommitState.decode(payload)
                except GrafxCorruptionDetected as failure:
                    failure.details["commit_state_reconstructible"] = True
                    raise
                self._observed_payload = payload
                self._observed_valid_payloads = ()
                return state
            except GrafxError as failure:
                retryable = failure.details.get("retryable", failure.retryable)
                if attempts < COMMIT_STATE_READ_ATTEMPTS and retryable is True:
                    continue
                if (
                    isinstance(failure, GrafxCorruptionDetected)
                    and failure.details.get(
                        "commit_state_reconstructible", self._slots is None
                    )
                    is True
                ):
                    self._observed_payload = _DAMAGED
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

    def publish(
        self,
        state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        """Publish ``state`` after the authoritative predecessor, without fence rollback.

        ``previous`` is the state read while the caller owns the commit section. Requiring that
        proof keeps a fresh store instance from accidentally replacing a format-2 capability
        fence with format 1. In the two-slot protocol the first 1-to-2 publication makes the new
        fence authoritative, then a second publication replaces the remaining version-1
        fallback. A crash during that second write still leaves the first whole version-2 slot
        as the newest valid record.
        """
        if type(state) is not CommitState or type(previous) is not CommitState:
            raise GrafxConfigurationError(
                "Commit-state publication requires exact current and previous states.",
                field="commit_state",
                state_type=type(state).__name__,
                previous_type=type(previous).__name__,
            )
        if type(previous_was_damaged) is not bool:
            raise GrafxConfigurationError(
                "Commit-state damage provenance must be an exact bool.",
                field="previous_was_damaged",
                value=type(previous_was_damaged).__name__,
            )
        if state.format_version < previous.format_version:
            raise GrafxCorruptionDetected(
                "Commit-state publication cannot lower an established capability fence.",
                file=COMMIT_STATE_FILE,
                field="format_version",
                previous=previous.format_version,
                value=state.format_version,
            )
        if previous_was_damaged and self._observed_payload is not _DAMAGED:
            raise GrafxCorruptionDetected(
                "Commit-state recovery claimed damage without observing a damaged record.",
                file=COMMIT_STATE_FILE,
                field="previous_was_damaged",
            )
        payload = state.encode()
        predecessor = previous.encode()
        expected: tuple[bytes | None, ...] | None = (
            None
            if previous_was_damaged
            else (
                predecessor,
                payload,
                *((None,) if previous == CommitState() else ()),
            )
        )
        if self._slots is not None:
            observed_payloads = self._observed_valid_payloads
            if observed_payloads is not _UNREAD:
                for observed_payload in observed_payloads:
                    try:
                        observed_state = CommitState.decode(observed_payload)
                    except GrafxCorruptionDetected:
                        continue
                    if observed_state.format_version > state.format_version:
                        raise GrafxCorruptionDetected(
                            "Commit-state repair cannot overwrite a stronger capability fence.",
                            file=COMMIT_STATE_FILE,
                            field="format_version",
                            current=state.format_version,
                            observed=observed_state.format_version,
                            commit_state_reconstructible=False,
                        )
            if not previous_was_damaged and self._observed_valid_payloads is _UNREAD:
                self.read()
            self._slots.publish_checked(
                payload,
                expected_current=expected,
                publications=(
                    2
                    if previous_was_damaged
                    or state.format_version > previous.format_version
                    else 1
                ),
                replace_damaged=previous_was_damaged,
            )
            self._observed_payload = payload
            self._observed_valid_payloads = _UNREAD
            return
        observed = self._observed_payload
        if observed is _UNREAD:
            self.read()
            observed = self._observed_payload
        if previous_was_damaged:
            if observed is not _DAMAGED:
                raise GrafxCorruptionDetected(
                    "Commit-state recovery claimed damage after a valid predecessor was read.",
                    file=COMMIT_STATE_FILE,
                    field="previous_was_damaged",
                )
        elif observed is _DAMAGED or expected is None or observed not in expected:
            raise GrafxCorruptionDetected(
                "The commit state changed after its publisher read the predecessor.",
                file=COMMIT_STATE_FILE,
                field="current_payload",
            )
        temporary = self._temporary_file
        if self._storage.exists(temporary):
            self._storage.remove(temporary)
        self._storage.create(temporary, exclusive=True)
        self._storage.append_log(temporary, state.encode())
        self._storage.durable_barrier(temporary)
        self._storage.atomic_replace(temporary, COMMIT_STATE_FILE)
        self._storage.durable_barrier(COMMIT_STATE_FILE)
        self._observed_payload = payload
        self._observed_valid_payloads = ()

    def redundancy_needs_repair(self, state: CommitState) -> bool:
        """Return whether a format-2 fence lacks a second decodable format-2 slot."""
        if type(state) is not CommitState:
            raise GrafxConfigurationError(
                "Commit-state redundancy can be checked only against an exact state.",
                field="commit_state",
                value=type(state).__name__,
            )
        if self._slots is None:
            return False
        if self._observed_valid_payloads is _UNREAD:
            self.read()
        observed_payloads = self._observed_valid_payloads
        if observed_payloads is _UNREAD:  # pragma: no cover - read settles this value
            raise AssertionError("unreachable commit-state slot observation")
        payloads = observed_payloads
        if not payloads:
            if self._observed_payload is None and state == CommitState():
                return False
            raise GrafxCorruptionDetected(
                "The commit-state fence has no valid populated control slot.",
                file=COMMIT_STATE_FILE,
                field="slots",
            )
        decoded: list[CommitState] = []
        for position, payload in enumerate(payloads):
            try:
                decoded.append(CommitState.decode(payload))
            except GrafxCorruptionDetected:
                if position == 0:
                    raise
        if not decoded or decoded[0] != state:
            raise GrafxCorruptionDetected(
                "The newest valid commit-state slot differs from the state recovery observed.",
                file=COMMIT_STATE_FILE,
                field="current_payload",
            )
        if state.format_version < 2:
            return False
        return sum(candidate.format_version >= 2 for candidate in decoded) < 2

    def repair_redundancy(self, state: CommitState) -> None:
        """Convergently replace an old/empty fallback while preserving the current fence."""
        for _attempt in range(2):
            if not self.redundancy_needs_repair(state):
                return
            self.publish(state, previous=state)
        if self.redundancy_needs_repair(state):
            raise GrafxCorruptionDetected(
                "The commit-state format-2 fence could not establish two valid copies.",
                file=COMMIT_STATE_FILE,
                field="slots",
            )

    @property
    def _temporary_file(self) -> str:
        """Return the participant-exclusive staging name used for atomic publication."""
        return f"{COMMIT_STATE_FILE}.{self._owner_id}.tmp"
