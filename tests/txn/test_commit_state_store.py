"""Focused contract tests for the shared commit-state persistence component."""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxStorageError,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_FORMAT_VERSION,
    CommitState,
)
from okto_grafx.engine.commit_state_store import (
    COMMIT_STATE_READ_ATTEMPTS,
    CommitStateStore,
)

OWNER = "commit-state-test-owner"
TEMPORARY = f"{COMMIT_STATE_FILE}.{OWNER}.tmp"


def _write(device: object, file: str, payload: bytes) -> None:
    device.create(file, exclusive=True)  # type: ignore[attr-defined]
    device.append_log(file, payload)  # type: ignore[attr-defined]


def test_an_absent_state_is_empty_but_an_existing_empty_file_is_corrupt() -> None:
    device = MemoryStorageDevice()
    store = CommitStateStore(device, owner_id=OWNER)

    assert store.read() == CommitState()

    device.create(COMMIT_STATE_FILE, exclusive=True)
    with pytest.raises(GrafxCorruptionDetected):
        store.read()


def test_the_strict_reader_propagates_intact_bytes_from_a_newer_format() -> None:
    device = MemoryStorageDevice()
    raw = bytearray(CommitState(last_committed_lsn=9, last_csn=9).encode())
    raw[4:6] = (COMMIT_STATE_FORMAT_VERSION + 1).to_bytes(2, "little")
    raw[-4:] = crc32c(bytes(raw[:-4])).to_bytes(4, "little")
    _write(device, COMMIT_STATE_FILE, bytes(raw))

    with pytest.raises(GrafxSchemaVersionMismatch):
        CommitStateStore(device, owner_id=OWNER).read()


class _RetryingReadDevice:
    """Refuse a configured number of commit-state reads as explicitly retryable."""

    def __init__(self, inner: MemoryStorageDevice, *, refusals: int) -> None:
        self._inner = inner
        self._remaining = refusals
        self.reads = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def page_size(self) -> int:
        return self._inner.page_size

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        self.reads += 1
        if file == COMMIT_STATE_FILE and self._remaining > 0:
            self._remaining -= 1
            raise GrafxStorageError(
                "The test device temporarily refused the commit-state read.",
                file=file,
                retryable=True,
            )
        return self._inner.read_log(file, offset, length)


def test_a_retryable_read_can_succeed_on_the_fourth_attempt() -> None:
    inner = MemoryStorageDevice()
    state = CommitState(last_committed_lsn=7, last_csn=7, checkpoint_lsn=4)
    _write(inner, COMMIT_STATE_FILE, state.encode())
    device = _RetryingReadDevice(inner, refusals=COMMIT_STATE_READ_ATTEMPTS - 1)

    assert CommitStateStore(device, owner_id=OWNER).read() == state  # type: ignore[arg-type]
    assert device.reads == COMMIT_STATE_READ_ATTEMPTS


def test_a_retryable_read_is_reported_after_four_failed_attempts() -> None:
    inner = MemoryStorageDevice()
    _write(inner, COMMIT_STATE_FILE, CommitState().encode())
    device = _RetryingReadDevice(inner, refusals=COMMIT_STATE_READ_ATTEMPTS)

    with pytest.raises(GrafxStorageError):
        CommitStateStore(device, owner_id=OWNER).read()  # type: ignore[arg-type]
    assert device.reads == COMMIT_STATE_READ_ATTEMPTS


def test_checkpoint_hint_is_explicitly_lenient_without_weakening_strict_read() -> None:
    device = MemoryStorageDevice()
    _write(device, COMMIT_STATE_FILE, b"damaged")
    store = CommitStateStore(device, owner_id=OWNER)

    assert store.checkpoint_hint() == 0
    with pytest.raises(GrafxCorruptionDetected):
        store.read()


def test_publish_uses_an_owner_exclusive_temporary_and_the_frozen_durability_order() -> None:
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    stale = b"stale temporary bytes"
    _write(device, TEMPORARY, stale)
    device.clear_trail()
    state = CommitState(last_committed_lsn=13, last_csn=13, checkpoint_lsn=8)

    CommitStateStore(device, owner_id=OWNER).publish(state)

    relevant = [
        (call.method, call.file, call.args_summary)
        for call in device.trail()
        if call.method
        in {"remove", "create", "append_log", "durable_barrier", "atomic_replace"}
    ]
    assert relevant == [
        ("remove", TEMPORARY, ""),
        ("create", TEMPORARY, "exclusive=True"),
        ("append_log", TEMPORARY, f"bytes={len(state.encode())}"),
        ("durable_barrier", TEMPORARY, ""),
        ("atomic_replace", TEMPORARY, f"target='{COMMIT_STATE_FILE}'"),
        ("durable_barrier", COMMIT_STATE_FILE, ""),
    ]
    assert not device.exists(TEMPORARY)
    assert CommitStateStore(device, owner_id="reader").read() == state
