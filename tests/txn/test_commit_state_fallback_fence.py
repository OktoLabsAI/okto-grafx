"""Fail-closed tests for capability fences hidden in the older control slot."""

from __future__ import annotations

from okto_grafx.runtime.capability_probe import port_has_attribute

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.control_record import CONTROL_SLOT_PAGES
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.page.layout import CHECKSUM_SIZE, PAGE_HEADER_SIZE
from okto_grafx.domain.txn.commit_record import CommitPayload
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_FORMAT_VERSION,
    CommitState,
)
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.ledger_store import LedgerStore
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.wal_manager import WalManager

from txn_support import ManualClock, RecordingMetricsSink

_DATABASE_UUID = bytes.fromhex("8aac82d7a7e94aa4bca84113820dca52")
_FILE_NONCE = 0xFA11BAC
_DESCRIPTOR = "hash-v1;partitions_per_table=8"


def _store(
    storage: MemoryStorageDevice, *, owner_id: str = "fallback-fence"
) -> CommitStateStore:
    """Bind the production two-slot store to the fixed test identity."""
    return CommitStateStore(
        storage,
        owner_id=owner_id,
        database_uuid=_DATABASE_UUID,
        file_nonce=_FILE_NONCE,
        control_format_version=2,
    )


def _future_payload(state: CommitState) -> bytes:
    """Return an intact same-size CommitState carrying the next unknown version."""
    raw = bytearray(state.encode())
    raw[4:6] = (COMMIT_STATE_FORMAT_VERSION + 1).to_bytes(2, "little")
    raw[-4:] = crc32c(bytes(raw[:-4])).to_bytes(4, "little")
    return bytes(raw)


def _corrupt_inner_payload(state: CommitState) -> bytes:
    """Damage only the inner CommitState checksum; the enclosing slot remains valid."""
    raw = bytearray(state.encode())
    raw[-1] ^= 0xFF
    return bytes(raw)


def _seed_slots(
    storage: MemoryStorageDevice, *, newest: bytes, fallback: bytes
) -> None:
    """Install two outer-valid slots with generations three and two, respectively."""
    from okto_grafx.domain.control_record import _encode_slot  # noqa: PLC2701

    seed = _store(storage, owner_id="slot-seed")
    legacy = CommitState(last_committed_lsn=1, last_csn=1)
    fenced = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    seed.publish(legacy, previous=CommitState())
    seed.publish(fenced, previous=legacy)
    slots = seed._slots  # noqa: SLF001 - exact physical-slot fixture
    assert slots is not None
    header = slots._read_header()  # noqa: SLF001 - exact physical-slot fixture
    page_size = storage.page_size
    storage.write_page(
        COMMIT_STATE_FILE,
        CONTROL_SLOT_PAGES[0],
        _encode_slot(
            header=header,
            generation=3,
            payload=newest,
            page_size=page_size,
        ),
    )
    storage.write_page(
        COMMIT_STATE_FILE,
        CONTROL_SLOT_PAGES[1],
        _encode_slot(
            header=header,
            generation=2,
            payload=fallback,
            page_size=page_size,
        ),
    )


def _snapshot(storage: MemoryStorageDevice) -> dict[str, bytes]:
    """Capture every logical file so a refusal proves complete non-interference."""
    return {
        file: storage.read_log(file, 0, storage.file_size(file))
        for file in storage.list_files()
    }


def _damage_external_header_checksum(storage: MemoryStorageDevice) -> None:
    """Corrupt page zero's outer checksum without changing its body or inner CRC."""
    raw = bytearray(storage.read_page(COMMIT_STATE_FILE, 0))
    raw[0] ^= 0xFF
    storage.write_page(COMMIT_STATE_FILE, 0, bytes(raw))


def _damage_inner_header_checksum(storage: MemoryStorageDevice) -> None:
    """Damage the inner header CRC while leaving its outer page checksum intact."""
    from okto_grafx.domain.control_record import _HEADER_BODY  # noqa: PLC2701

    raw = bytearray(storage.read_page(COMMIT_STATE_FILE, 0))
    raw[PAGE_HEADER_SIZE + _HEADER_BODY.size] ^= 0xFF
    raw[:CHECKSUM_SIZE] = crc32c(bytes(raw[CHECKSUM_SIZE:])).to_bytes(
        CHECKSUM_SIZE, "little"
    )
    storage.write_page(COMMIT_STATE_FILE, 0, bytes(raw))


class _Coordinator:
    """Minimal complete recovery fence for a deterministic single-process test."""

    @contextmanager
    def exclusive(self, _name: str, *, timeout: float) -> Iterator[None]:
        del timeout
        yield

    def reader_horizon(self) -> None:
        return None


def _recovery(
    storage: MemoryStorageDevice,
    state_store: CommitStateStore,
    *,
    catalog_format: int | None = None,
    seed_complete_commit: bool = False,
) -> RecoveryManager:
    """Assemble only the real components reached by the recovery entry point."""
    clock = ManualClock()
    metrics = RecordingMetricsSink()
    codec = PageCodecV1(page_size=storage.page_size)
    pool = BufferPool(
        storage,
        codec,
        metrics,
        budget_bytes=512 * 1024,
        db_label="fallback-fence",
    )
    catalog = None
    if catalog_format is not None:
        catalog = CatalogStore(pool)
        catalog.bootstrap()
        if catalog_format == CATALOG_FORMAT_VERSION:
            candidate = catalog.read_from_pages()
            candidate.upgrade_index_catalog()
            catalog.adopt(candidate)
            catalog.save()
        else:
            assert catalog_format == CATALOG_LEGACY_FORMAT_VERSION
        pool.flush(catalog.file)
    wal = WalManager(
        storage,
        clock,
        metrics,
        segment_bytes=4096,
        descriptor=_DESCRIPTOR,
    )
    wal.open()
    if seed_complete_commit:
        wal.append(
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                epoch=1,
                txn_id=1,
                payload=CommitPayload.build(
                    snapshot_lsn=0,
                    read_partitions=(),
                    write_partitions=(),
                ).encode(),
                descriptor=_DESCRIPTOR,
            )
        )
        wal.barrier()
    quarantine = QuarantineStore(storage, clock, metrics)
    ledger = LedgerStore(storage, clock, metrics, quarantine=quarantine)
    return RecoveryManager(
        storage,
        wal,
        ledger,
        quarantine,
        pool,
        metrics,
        attribute_probe=port_has_attribute,
        catalog=catalog,
        commit_state_store=state_store,
        coordinator=_Coordinator(),
    )


def test_read_refuses_a_future_fence_in_the_fallback_without_mutation() -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    _seed_slots(storage, newest=fenced.encode(), fallback=_future_payload(fenced))
    before = _snapshot(storage)

    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        _store(storage, owner_id="strict-reader").read()

    assert refused.value.details["field"] == "format_version"
    assert _snapshot(storage) == before


def test_publish_refuses_a_future_fallback_before_any_mutation() -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    _seed_slots(storage, newest=fenced.encode(), fallback=_future_payload(fenced))
    before = _snapshot(storage)

    with pytest.raises(GrafxSchemaVersionMismatch):
        _store(storage, owner_id="strict-publisher").publish(
            CommitState(
                last_committed_lsn=3,
                last_csn=3,
                format_version=COMMIT_STATE_FORMAT_VERSION,
            ),
            previous=fenced,
        )

    assert _snapshot(storage) == before


def test_recovery_refuses_a_future_fallback_before_mutating_any_file() -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    _seed_slots(storage, newest=fenced.encode(), fallback=_future_payload(fenced))
    recovery = _recovery(storage, _store(storage, owner_id="strict-recovery"))
    before = _snapshot(storage)

    with pytest.raises(GrafxSchemaVersionMismatch):
        recovery.run()

    assert _snapshot(storage) == before


def test_newest_v1_over_supported_v2_fallback_refuses_downgrade_without_mutation() -> (
    None
):
    storage = MemoryStorageDevice(page_size=512)
    legacy = CommitState(last_committed_lsn=3, last_csn=3)
    fallback = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    _seed_slots(storage, newest=legacy.encode(), fallback=fallback.encode())
    before = _snapshot(storage)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        _store(storage, owner_id="downgrade-reader").read()

    assert refused.value.details["file"] == COMMIT_STATE_FILE
    assert refused.value.details["field"] == "format_version"
    assert refused.value.details["current"] == 1
    assert refused.value.details["fallback"] == COMMIT_STATE_FORMAT_VERSION
    assert refused.value.details["commit_state_reconstructible"] is False
    assert _snapshot(storage) == before

    with pytest.raises(GrafxCorruptionDetected):
        _store(storage, owner_id="downgrade-publisher").publish(
            CommitState(last_committed_lsn=4, last_csn=4),
            previous=legacy,
        )

    assert _snapshot(storage) == before

    recovery = _recovery(storage, _store(storage, owner_id="downgrade-recovery"))
    before_recovery = _snapshot(storage)
    with pytest.raises(GrafxCorruptionDetected) as recovery_refused:
        recovery.run()

    assert recovery_refused.value.details["field"] == "format_version"
    assert recovery_refused.value.details["commit_state_reconstructible"] is False
    assert _snapshot(storage) == before_recovery


def test_absent_default_state_remains_valid_and_publishable() -> None:
    storage = MemoryStorageDevice(page_size=512)
    store = _store(storage)
    default = CommitState()

    assert store.read() == default
    assert store.redundancy_needs_repair(default) is False
    assert not storage.exists(COMMIT_STATE_FILE)

    store.publish(default, previous=default)

    assert _store(storage, owner_id="default-reader").read() == default


def test_recovery_refuses_corrupt_newest_over_v2_fallback_with_v1_catalog() -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    _seed_slots(
        storage,
        newest=_corrupt_inner_payload(fenced),
        fallback=fenced.encode(),
    )
    recovery = _recovery(
        storage,
        _store(storage, owner_id="v1-catalog-recovery"),
        catalog_format=CATALOG_LEGACY_FORMAT_VERSION,
    )
    before = _snapshot(storage)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        recovery.run()

    assert refused.value.details["field"] == "format_version"
    assert refused.value.details["commit_state_format"] == COMMIT_STATE_FORMAT_VERSION
    assert refused.value.details["catalog_format"] == CATALOG_LEGACY_FORMAT_VERSION
    assert _snapshot(storage) == before


def test_recovery_rebuilds_corrupt_newest_from_v2_catalog_and_v2_fallback() -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    _seed_slots(
        storage,
        newest=_corrupt_inner_payload(fenced),
        fallback=fenced.encode(),
    )
    recovery = _recovery(
        storage,
        _store(storage, owner_id="v2-catalog-recovery"),
        catalog_format=CATALOG_FORMAT_VERSION,
        seed_complete_commit=True,
    )

    report = recovery.run()

    assert report.records_replayed == 0
    healed = _store(storage, owner_id="healed-reader")
    state = healed.read()
    assert state.format_version == COMMIT_STATE_FORMAT_VERSION
    assert state.last_committed_lsn > 0
    assert healed.redundancy_needs_repair(state) is False
    slots = healed._slots  # noqa: SLF001 - verify both repaired physical copies
    assert slots is not None
    physical = slots.read()
    assert physical is not None
    payloads = physical.valid_payloads
    assert len(payloads) == 2
    assert all(
        CommitState.decode(payload).format_version == COMMIT_STATE_FORMAT_VERSION
        for payload in payloads
    )


@pytest.mark.parametrize("inner_checksum", [False, True], ids=["outer", "inner"])
def test_damaged_header_cannot_hide_future_slots_from_read_publish_or_recovery(
    inner_checksum: bool,
) -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    future = _future_payload(fenced)
    _seed_slots(storage, newest=future, fallback=future)
    damage = (
        _damage_inner_header_checksum
        if inner_checksum
        else _damage_external_header_checksum
    )
    damage(storage)
    before = _snapshot(storage)
    inspector = _store(storage, owner_id="future-header-reader")

    with pytest.raises(GrafxSchemaVersionMismatch) as read_refused:
        inspector.read()

    assert read_refused.value.details["field"] == "format_version"
    assert _snapshot(storage) == before

    with pytest.raises(GrafxCorruptionDetected) as publish_refused:
        inspector.publish(
            fenced,
            previous=fenced,
            previous_was_damaged=True,
        )

    assert publish_refused.value.details["field"] == "previous_was_damaged"
    assert _snapshot(storage) == before

    recovery = _recovery(
        storage,
        _store(storage, owner_id="future-header-recovery"),
        catalog_format=CATALOG_FORMAT_VERSION,
        seed_complete_commit=True,
    )
    before_recovery = _snapshot(storage)

    with pytest.raises(GrafxSchemaVersionMismatch) as recovery_refused:
        recovery.run()

    assert recovery_refused.value.details["field"] == "format_version"
    assert _snapshot(storage) == before_recovery


@pytest.mark.parametrize("inner_checksum", [False, True], ids=["outer", "inner"])
def test_damaged_header_with_v2_slots_and_v2_catalog_is_reconstructed(
    inner_checksum: bool,
) -> None:
    storage = MemoryStorageDevice(page_size=512)
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    _seed_slots(storage, newest=fenced.encode(), fallback=fenced.encode())
    damage = (
        _damage_inner_header_checksum
        if inner_checksum
        else _damage_external_header_checksum
    )
    damage(storage)
    repairing = _store(storage, owner_id="known-header-recovery")
    recovery = _recovery(
        storage,
        repairing,
        catalog_format=CATALOG_FORMAT_VERSION,
        seed_complete_commit=True,
    )
    before = _snapshot(storage)

    with pytest.raises(GrafxCorruptionDetected) as observed:
        repairing.read()

    expected_field = "header_crc32c" if inner_checksum else "checksum"
    assert observed.value.details["field"] == expected_field
    assert observed.value.details["commit_state_reconstructible"] is True
    assert (
        observed.value.details["commit_state_minimum_format_version"]
        == COMMIT_STATE_FORMAT_VERSION
    )
    assert _snapshot(storage) == before

    report = recovery.run()

    assert report.records_replayed == 0
    healed = _store(storage, owner_id="known-header-reader")
    state = healed.read()
    assert state.format_version == COMMIT_STATE_FORMAT_VERSION
    assert state.last_committed_lsn > 0
    assert healed.redundancy_needs_repair(state) is False
    slots = healed._slots  # noqa: SLF001 - verify both repaired physical copies
    assert slots is not None
    physical = slots.read()
    assert physical is not None
    assert len(physical.valid_payloads) == 2
    assert all(
        CommitState.decode(payload).format_version == COMMIT_STATE_FORMAT_VERSION
        for payload in physical.valid_payloads
    )
