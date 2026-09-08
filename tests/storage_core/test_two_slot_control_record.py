"""CE-1 unit contract for crash-safe two-slot control publications."""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.control_record_io import read_control_if_exists
from okto_grafx.adapters.coordination_local import (
    LeaseRecord,
    ReaderRecord,
    encode_lease_record,
    encode_reader_record,
)
from okto_grafx.domain.control_record import (
    CONTROL_FILE_PAGES,
    ControlRecordKind,
    ControlRecordReader,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.page.layout import MAX_U64, MIN_PAGE_SIZE, PageHeader
from okto_grafx.domain.txn.commit_state import CommitState

FILE = "control/unit.state"
TEMP = "control/unit.state.test.tmp"
DATABASE_UUID = bytes.fromhex("00112233445566778899aabbccddeeff")


class _FirstImageDamagedDevice:
    """Return one damaged exact image before exposing the wrapped bytes unchanged."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        self._inner = inner
        self._first_image = True

    @property
    def page_size(self) -> int:
        """Forward the immutable page size."""
        return self._inner.page_size

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Damage both slots on the first image read, then forward later reads."""
        image = self._inner.read_log(file, offset, length)
        if not self._first_image:
            return image
        self._first_image = False
        damaged = bytearray(image[: CONTROL_FILE_PAGES * self.page_size])
        damaged[2 * self.page_size - 1] ^= 1
        damaged[3 * self.page_size - 1] ^= 2
        return bytes(damaged)

    def __getattr__(self, name: str) -> object:
        """Forward the remaining storage port for this read-only regression fixture."""
        return getattr(self._inner, name)


class _FirstImageShortDevice:
    """Return one artificial short image while the wrapped file remains exact length."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        self._inner = inner
        self._first_image = True

    @property
    def page_size(self) -> int:
        """Forward the immutable page size."""
        return self._inner.page_size

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Shorten only the first read to reproduce an inconsistent adapter observation."""
        image = self._inner.read_log(file, offset, length)
        if not self._first_image:
            return image
        self._first_image = False
        return image[:-1]

    def __getattr__(self, name: str) -> object:
        """Forward the remaining storage port for this read-only regression fixture."""
        return getattr(self._inner, name)


class _FirstImageTinyDevice(_FirstImageShortDevice):
    """Return a tiny first image regardless of the wrapped file's larger length."""

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return six bytes once, then forward complete bounded reads."""
        if not self._first_image:
            return self._inner.read_log(file, offset, length)
        self._first_image = False
        return b"legacy"


class _FusedRecordingDevice:
    """A concrete adapter that records whether control reads select its fused door."""

    def __init__(self, inner: MemoryStorageDevice) -> None:
        self._inner = inner
        self.calls: list[str] = []

    @property
    def page_size(self) -> int:
        return self._inner.page_size

    def exists(self, file: str) -> bool:
        self.calls.append("exists")
        return self._inner.exists(file)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        self.calls.append("read_log")
        return self._inner.read_log(file, offset, length)

    def read_log_if_exists(
        self, file: str, offset: int, length: int
    ) -> bytes | None:
        self.calls.append("read_log_if_exists")
        if not self._inner.exists(file):
            return None
        return self._inner.read_log(file, offset, length)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _GetattrOnlyDevice:
    """A transparent-looking wrapper that deliberately declares no fused capability."""

    def __init__(self, inner: _FusedRecordingDevice) -> None:
        self._inner = inner

    @property
    def page_size(self) -> int:
        return self._inner.page_size

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _store(
    storage: object,
    *,
    file: str = FILE,
    temporary: str = TEMP,
    nonce: int = 11,
    database_uuid: bytes = DATABASE_UUID,
    read_if_exists: ControlRecordReader | None = read_control_if_exists,
) -> TwoSlotControlRecordStore:
    """Return one commit-state-kind store over the supplied test device."""
    return TwoSlotControlRecordStore(
        storage,  # type: ignore[arg-type]
        file=file,
        record_kind=ControlRecordKind.COMMIT_STATE,
        database_uuid=database_uuid,
        file_nonce=nonce,
        temporary=temporary,
        read_if_exists=read_if_exists,
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


def test_control_reads_use_only_a_capability_declared_by_the_concrete_type() -> None:
    inner = MemoryStorageDevice()
    _store(inner).publish(b"first")
    direct = _FusedRecordingDevice(inner)

    observed = _store(direct).read()

    assert observed is not None
    assert observed.payload == b"first"
    assert direct.calls == ["read_log_if_exists"]

    direct.calls.clear()
    wrapped = _GetattrOnlyDevice(direct)
    observed = _store(wrapped).read()

    assert observed is not None
    assert observed.payload == b"first"
    assert direct.calls == ["exists", "read_log"]


def test_manual_domain_composition_without_a_reader_uses_only_the_literal_port() -> None:
    inner = MemoryStorageDevice()
    _store(inner).publish(b"first")
    device = _FusedRecordingDevice(inner)
    observed = _store(device, read_if_exists=None).read()
    assert observed is not None and observed.payload == b"first"
    assert device.calls == ["exists", "read_log"]


def test_inherited_fused_methods_do_not_opt_in_a_new_concrete_type() -> None:
    class Inherited(_FusedRecordingDevice):
        pass

    inner = MemoryStorageDevice()
    _store(inner).publish(b"first")
    device = Inherited(inner)
    observed = _store(device).read()
    assert observed is not None and observed.payload == b"first"
    assert device.calls == ["exists", "read_log"]


def test_fused_capability_resolution_remains_fresh_and_instance_instrumented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = MemoryStorageDevice()
    _store(inner).publish(b"first")
    device = _FusedRecordingDevice(inner)
    store = _store(device)
    assert store.read() is not None
    original = device.read_log_if_exists
    instrumented: list[str] = []

    def observed(file: str, offset: int, length: int) -> bytes | None:
        instrumented.append(file)
        return original(file, offset, length)

    monkeypatch.setattr(device, "read_log_if_exists", observed)
    assert store.read() is not None
    assert instrumented == [FILE]

    # Removing the concrete declaration revokes opt-in even while the instance
    # instrumentation remains callable. The store must not cache this authority.
    monkeypatch.setattr(_FusedRecordingDevice, "read_log_if_exists", None)
    device.calls.clear()
    assert store.read() is not None
    assert instrumented == [FILE]
    assert device.calls == ["exists", "read_log"]


@pytest.mark.parametrize("reader", [False, 1, object()])
def test_malformed_injected_readers_are_refused(reader: object) -> None:
    with pytest.raises(GrafxConfigurationError) as refused:
        _store(MemoryStorageDevice(), read_if_exists=reader)  # type: ignore[arg-type]
    assert refused.value.details["field"] == "read_if_exists"


def test_injected_read_failure_is_not_reclassified_as_absence() -> None:
    failure = RuntimeError("control read failed")

    def reader(storage: object, file: str, offset: int, length: int) -> bytes | None:
        raise failure

    store = _store(MemoryStorageDevice(), read_if_exists=reader)
    with pytest.raises(RuntimeError) as caught:
        store.read()
    assert caught.value is failure


def test_a_warm_publication_is_exactly_one_page_write_and_one_barrier() -> None:
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    store = _store(device)
    store.publish(b"first")
    device.clear_trail()

    assert store.publish(b"second") == 2

    operations = [
        (call.method, call.file)
        for call in device.trail()
        if call.method
        in {
            "read_log",
            "read_page",
            "file_size",
            "write_page",
            "durable_barrier",
            "atomic_replace",
        }
    ]
    assert operations == [
        ("read_log", FILE),
        ("write_page", FILE),
        ("durable_barrier", FILE),
    ]
    device.clear_trail()
    observed = store.read()
    assert observed is not None
    assert (observed.payload, observed.generation) == (b"second", 2)
    reads = [
        (call.method, call.file)
        for call in device.trail()
        if call.method in {"read_log", "read_page", "file_size"}
    ]
    assert reads == [("read_log", FILE)]


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


def test_a_stable_legacy_record_uses_the_explicit_short_read_path() -> None:
    device = FaultInjectingStorageDevice(MemoryStorageDevice())
    device.create(FILE)
    device.append_log(FILE, b"legacy")
    device.clear_trail()

    observed = _store(device).read()

    assert observed is not None
    assert (observed.payload, observed.format_version) == (b"legacy", 1)
    reads = [
        (call.method, call.file)
        for call in device.trail()
        if call.method in {"read_log", "read_page", "file_size"}
    ]
    assert reads == [("read_log", FILE), ("file_size", FILE)]


def test_an_oversized_legacy_record_is_not_silently_decoded_as_a_v2_prefix() -> None:
    device = MemoryStorageDevice()
    payload = b"legacy" + bytes(CONTROL_FILE_PAGES * device.page_size)
    device.create(FILE)
    device.append_log(FILE, payload)

    observed = _store(device).read()

    assert observed is not None
    assert (observed.payload, observed.format_version) == (payload, 1)


def test_migrating_an_oversized_legacy_record_does_not_materialise_it_in_full() -> None:
    inner = MemoryStorageDevice()
    device = FaultInjectingStorageDevice(inner)
    payload = b"legacy" + bytes(CONTROL_FILE_PAGES * device.page_size)
    device.create(FILE)
    device.append_log(FILE, payload)
    device.clear_trail()

    assert _store(device).publish(b"migrated") == 1

    reads = [call for call in device.trail() if call.method == "read_log"]
    assert len(reads) == 1
    assert reads[0].args_summary.endswith(
        f"length={CONTROL_FILE_PAGES * device.page_size + 1}"
    )
    observed = _store(inner).read()
    assert observed is not None
    assert (observed.payload, observed.format_version) == (b"migrated", 2)


def test_a_v2_retry_refuses_an_oversized_replacement_with_a_valid_prefix() -> None:
    inner = MemoryStorageDevice()
    assert _store(inner).publish(b"valid-prefix") == 1
    inner.append_log(FILE, b"unexpected-suffix")
    device = _FirstImageDamagedDevice(inner)

    with pytest.raises(GrafxCorruptionDetected, match="changed length"):
        _store(device).read()


def test_an_initial_short_v2_read_fails_closed_when_the_file_is_exact_length() -> None:
    inner = MemoryStorageDevice()
    assert _store(inner).publish(b"exact") == 1

    with pytest.raises(GrafxCorruptionDetected, match="changed while it was read"):
        _store(_FirstImageShortDevice(inner)).read()


def test_publish_refuses_a_short_observation_whose_current_file_is_oversized() -> None:
    inner = MemoryStorageDevice()
    payload = b"oversized" + bytes(CONTROL_FILE_PAGES * inner.page_size)
    inner.create(FILE)
    inner.append_log(FILE, payload)

    with pytest.raises(GrafxCorruptionDetected, match="changed while it was read"):
        _store(_FirstImageTinyDevice(inner)).publish(b"must-not-replace")
    assert inner.read_log(FILE, 0, len(payload)) == payload


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
