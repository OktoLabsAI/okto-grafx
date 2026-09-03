"""Focused contract tests for the shared commit-state persistence component."""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxStorageError,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_FORMAT_VERSION,
    COMMIT_STATE_LEGACY_FORMAT_VERSION,
    COMMIT_STATE_SIZE,
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


def test_feature_fence_keeps_the_legacy_commit_state_layout() -> None:
    legacy = CommitState(last_committed_lsn=9, last_csn=9, checkpoint_lsn=4)
    fenced = CommitState(
        last_committed_lsn=9,
        last_csn=9,
        checkpoint_lsn=4,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )

    assert legacy.format_version == COMMIT_STATE_LEGACY_FORMAT_VERSION == 1
    assert fenced.format_version == COMMIT_STATE_FORMAT_VERSION == 2
    assert len(legacy.encode()) == len(fenced.encode()) == COMMIT_STATE_SIZE == 36
    assert int.from_bytes(legacy.encode()[4:6], "little") == 1
    assert int.from_bytes(fenced.encode()[4:6], "little") == 2
    assert CommitState.decode(legacy.encode()) == legacy
    assert CommitState.decode(fenced.encode()) == fenced


def test_released_v1_decoder_classifies_the_same_size_fence_as_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_grafx.domain.txn import commit_state as module

    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION).encode()
    monkeypatch.setattr(module, "COMMIT_STATE_FORMAT_VERSION", 1)

    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        CommitState.decode(fenced)

    assert refused.value.details["field"] == "format_version"
    assert refused.value.details["value"] == 2


def test_commit_state_refuses_an_invalid_writable_format() -> None:
    for version in (0, COMMIT_STATE_FORMAT_VERSION + 1, True):
        with pytest.raises(GrafxConfigurationError) as refused:
            CommitState(format_version=version)  # type: ignore[arg-type]

        assert getattr(refused.value, "details", {}).get("field") == "format_version"


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


def test_format_two_checkpoint_hint_hides_length_damage_but_strict_read_refuses() -> (
    None
):
    device = MemoryStorageDevice()
    store = CommitStateStore(
        device,
        owner_id=OWNER,
        database_uuid=b"d" * 16,
        file_nonce=17,
        control_format_version=2,
    )
    store.publish(
        CommitState(last_committed_lsn=7, last_csn=7, checkpoint_lsn=4),
        previous=CommitState(),
    )
    device.append_log(COMMIT_STATE_FILE, b"unexpected-suffix")

    assert store.checkpoint_hint() == 0
    with pytest.raises(GrafxCorruptionDetected):
        store.read()


def test_publish_uses_an_owner_exclusive_temporary_and_the_frozen_durability_order() -> (
    None
):
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    stale = b"stale temporary bytes"
    _write(device, TEMPORARY, stale)
    device.clear_trail()
    state = CommitState(last_committed_lsn=13, last_csn=13, checkpoint_lsn=8)

    CommitStateStore(device, owner_id=OWNER).publish(state, previous=CommitState())

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


def test_format_two_migrates_the_legacy_state_then_uses_one_page_write() -> None:
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    original = CommitState(last_committed_lsn=7, last_csn=7, checkpoint_lsn=4)
    _write(device, COMMIT_STATE_FILE, original.encode())
    store = CommitStateStore(
        device,
        owner_id=OWNER,
        database_uuid=b"d" * 16,
        file_nonce=17,
        control_format_version=2,
    )

    assert store.read() == original
    intermediate = CommitState(last_committed_lsn=9, last_csn=9, checkpoint_lsn=4)
    store.publish(intermediate, previous=original)
    device.clear_trail()
    final = CommitState(last_committed_lsn=12, last_csn=12, checkpoint_lsn=4)
    store.publish(final, previous=intermediate)

    relevant = [
        (call.method, call.file)
        for call in device.trail()
        if call.method in {"write_page", "durable_barrier", "atomic_replace"}
    ]
    assert relevant == [
        ("write_page", COMMIT_STATE_FILE),
        ("durable_barrier", COMMIT_STATE_FILE),
    ]
    assert store.read() == final
