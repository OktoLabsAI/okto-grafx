"""CE-1 unit contract for crash-safe two-slot control publications."""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.coordination_local import (
    LeaseRecord,
    ReaderRecord,
    encode_lease_record,
    encode_reader_record,
)
from okto_grafx.domain.control_record import (
    CONTROL_FILE_PAGES,
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.page.layout import MAX_U64, MIN_PAGE_SIZE, PageHeader
from okto_grafx.domain.txn.commit_state import CommitState

FILE = "control/unit.state"
TEMP = "control/unit.state.test.tmp"
DATABASE_UUID = bytes.fromhex("00112233445566778899aabbccddeeff")


def _store(
    storage: object,
    *,
    file: str = FILE,
    temporary: str = TEMP,
    nonce: int = 11,
    database_uuid: bytes = DATABASE_UUID,
) -> TwoSlotControlRecordStore:
    """Return one commit-state-kind store over the supplied test device."""
    return TwoSlotControlRecordStore(
        storage,  # type: ignore[arg-type]
        file=file,
        record_kind=ControlRecordKind.COMMIT_STATE,
        database_uuid=database_uuid,
        file_nonce=nonce,
        temporary=temporary,
    )


def test_bootstrap_installs_three_complete_pages_and_a_canonical_empty_slot() -> None:
    device = MemoryStorageDevice()
    store = _store(device)

    assert store.publish(b"first") == 1

    assert device.file_size(FILE) == CONTROL_FILE_PAGES * device.page_size
    assert not device.exists(TEMP)
    observed = _store(device).read()
    assert observed is not None
    assert (observed.payload, observed.format_version, observed.generation) == (
        b"first",
        2,
        1,
    )


def test_a_warm_publication_is_exactly_one_page_write_and_one_barrier() -> None:
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    store = _store(device)
    store.publish(b"first")
    device.clear_trail()

    assert store.publish(b"second") == 2

    writes = [
        (call.method, call.file)
        for call in device.trail()
        if call.method in {"write_page", "durable_barrier", "atomic_replace"}
    ]
    assert writes == [("write_page", FILE), ("durable_barrier", FILE)]
    observed = store.read()
    assert observed is not None
    assert (observed.payload, observed.generation) == (b"second", 2)


def test_a_legacy_record_is_read_without_guessing_and_migrated_on_publish() -> None:
    device = MemoryStorageDevice()
    device.create(FILE)
    device.append_log(FILE, b"legacy")
    store = _store(device)

    legacy = store.read()
    assert legacy is not None
    assert (legacy.payload, legacy.format_version, legacy.generation) == (
        b"legacy",
        1,
        0,
    )

    assert store.publish(b"migrated") == 1
    migrated = store.read()
    assert migrated is not None
    assert (migrated.payload, migrated.format_version, migrated.generation) == (
        b"migrated",
        2,
        1,
    )


def test_a_torn_older_slot_is_ignored_and_repaired_by_the_next_publication() -> None:
    device = MemoryStorageDevice()
    store = _store(device)
    store.publish(b"one")
    store.publish(b"two")
    old = bytearray(device.read_page(FILE, 1))
    old[-1] ^= 0xFF
    device.write_page(FILE, 1, bytes(old))

    observed = _store(device).read()
    assert observed is not None
    assert (observed.payload, observed.generation) == (b"two", 2)

    assert _store(device).publish(b"three") == 3
    repaired = _store(device).read()
    assert repaired is not None
    assert (repaired.payload, repaired.generation) == (b"three", 3)


def test_both_torn_slots_fail_closed() -> None:
    device = MemoryStorageDevice()
    _store(device).publish(b"one")
    for page in (1, 2):
        raw = bytearray(device.read_page(FILE, page))
        raw[-1] ^= page
        device.write_page(FILE, page, bytes(raw))

    with pytest.raises(GrafxCorruptionDetected, match="Both slots"):
        _store(device).read()


def test_an_intact_slot_moved_from_another_file_is_rejected_by_its_nonce() -> None:
    device = MemoryStorageDevice()
    left = _store(device, nonce=11)
    right = _store(
        device,
        file="control/other.state",
        temporary="control/other.state.test.tmp",
        nonce=22,
    )
    left.publish(b"left")
    right.publish(b"right")
    device.write_page(FILE, 1, device.read_page("control/other.state", 1))

    with pytest.raises(GrafxCorruptionDetected, match="Both slots"):
        _store(device, nonce=99).read()


def test_two_slots_may_not_claim_the_same_nonempty_generation() -> None:
    device = MemoryStorageDevice()
    _store(device).publish(b"one")
    device.write_page(FILE, 2, device.read_page(FILE, 1))

    with pytest.raises(GrafxCorruptionDetected, match="same non-empty generation"):
        _store(device).read()


def test_a_slot_file_is_bound_to_the_database_uuid() -> None:
    device = MemoryStorageDevice()
    _store(device).publish(b"one")

    with pytest.raises(GrafxCorruptionDetected, match="not bound"):
        _store(device, database_uuid=b"x" * 16).read()


def test_generation_at_u64_max_refuses_to_publish() -> None:
    from okto_grafx.domain.control_record import _encode_slot  # noqa: PLC2701

    device = MemoryStorageDevice()
    store = _store(device)
    store.publish(b"one")
    header = store._read_header()  # noqa: SLF001 - exact overflow fixture
    device.write_page(
        FILE,
        1,
        _encode_slot(
            header=header,
            generation=MAX_U64,
            payload=b"last",
            page_size=device.page_size,
        ),
    )

    with pytest.raises(GrafxCorruptionDetected, match="without wrapping"):
        _store(device).publish(b"never")


def test_page_seq_wraparound_does_not_affect_slot_selection() -> None:
    from okto_grafx.domain.control_record import _encode_slot  # noqa: PLC2701

    device = MemoryStorageDevice()
    store = _store(device)
    store.publish(b"one")
    header = store._read_header()  # noqa: SLF001 - exact wrap fixture
    before_wrap = (1 << 31) - 1
    after_wrap = 1 << 31
    device.write_page(
        FILE,
        1,
        _encode_slot(
            header=header,
            generation=before_wrap,
            payload=b"before",
            page_size=device.page_size,
        ),
    )
    wrapped = _encode_slot(
        header=header,
        generation=after_wrap,
        payload=b"after",
        page_size=device.page_size,
    )
    device.write_page(FILE, 2, wrapped)

    assert PageHeader.decode(wrapped).seq == 0
    observed = _store(device).read()
    assert observed is not None
    assert (observed.payload, observed.generation) == (b"after", after_wrap)


def test_every_logical_v1_record_fits_in_the_smallest_control_page() -> None:
    device = MemoryStorageDevice(page_size=MIN_PAGE_SIZE)
    payloads = (
        encode_lease_record(
            LeaseRecord(
                owner_id="o" * 88,
                epoch=1,
                heartbeat_seq=1,
                ttl_seconds=30.0,
                wall_stamp=1.0,
                held=True,
                superseded_epoch=0,
            )
        ),
        encode_reader_record(
            ReaderRecord(
                reader_id="r" * 96,
                snapshot_lsn=1,
                heartbeat_seq=1,
                wall_stamp=1.0,
                active=True,
            )
        ),
        CommitState(last_committed_lsn=1, last_csn=1, checkpoint_lsn=1).encode(),
    )
    for index, payload in enumerate(payloads, start=1):
        store = _store(
            device,
            file=f"control/minimum-{index}.state",
            temporary=f"control/minimum-{index}.tmp",
            nonce=index,
        )
        assert store.publish(payload) == 1
        assert store.read() is not None
