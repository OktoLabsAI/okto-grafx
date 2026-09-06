"""Crash boundaries for the catalog-v2 and commit-state-v2 capability fence."""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice, FaultPlan
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.control_record import CONTROL_SLOT_PAGES
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxRecoveryRefused,
)
from okto_grafx.domain.model.catalog import CATALOG_FORMAT_VERSION
from okto_grafx.domain.page.layout import MAX_U64
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_FORMAT_VERSION,
    COMMIT_STATE_LEGACY_FORMAT_VERSION,
    CommitState,
)
from okto_grafx.engine.catalog_store import CATALOG_FILE
from okto_grafx.engine.commit_state_store import CommitStateStore

from .conftest import HEAP_FILE, Stack, commit_pages, make_page_image

_DATABASE_UUID = bytes.fromhex("a8e1ce4b4533404091e31b375d473c89")
_FILE_NONCE = 0xC0AC71


def _state_store(
    storage: object,
    *,
    owner_id: str = "coactivation-test",
    control_format_version: int = 2,
) -> CommitStateStore:
    """Bind the production two-slot commit-state store to this test database."""
    return CommitStateStore(
        storage,  # type: ignore[arg-type]
        owner_id=owner_id,
        database_uuid=_DATABASE_UUID,
        file_nonce=_FILE_NONCE,
        control_format_version=control_format_version,
    )


def _persist_catalog_v2(stack: Stack) -> None:
    """Install catalog v2 in pages without adding a catalog record to the WAL."""
    catalog = stack.catalog.read_from_pages()
    catalog.upgrade_index_catalog()
    stack.catalog.adopt(catalog)
    stack.catalog.save()
    stack.pool.flush(CATALOG_FILE)
    assert stack.catalog.read_from_pages().format_version == CATALOG_FORMAT_VERSION


def _damage_slot(storage: object, page_index: int) -> bytes:
    """Invalidate one physical slot and return its exact prior image for restoration."""
    original = storage.read_page(COMMIT_STATE_FILE, page_index)  # type: ignore[attr-defined]
    damaged = bytearray(original)
    damaged[-1] ^= 0xFF
    storage.write_page(  # type: ignore[attr-defined]
        COMMIT_STATE_FILE, page_index, bytes(damaged)
    )
    return original


def _commit_state_bytes(storage: object) -> bytes:
    """Return the complete physical commit-state image, whatever its outer format."""
    size = storage.file_size(COMMIT_STATE_FILE)  # type: ignore[attr-defined]
    return storage.read_log(COMMIT_STATE_FILE, 0, size)  # type: ignore[attr-defined]


class _HostileCommitState(CommitState):
    """A subclass whose codec must never run through an exact-state boundary."""

    def encode(self) -> bytes:
        """Make a missed exact-type guard unmistakable."""
        raise AssertionError("a CommitState subclass reached the durable codec")


def test_recovery_promotes_state_from_durable_catalog_v2_without_catalog_wal(
    stack: Stack,
) -> None:
    """Durable catalog authority closes the fence even when replay has no catalog effect."""
    _persist_catalog_v2(stack)
    store = _state_store(stack.storage)

    assert stack.wal.last_lsn == 0
    assert store.read().format_version == COMMIT_STATE_LEGACY_FORMAT_VERSION

    report = stack.recovery(commit_state_store=store).run()

    assert report.records_replayed == 0
    assert stack.wal.last_lsn == 0
    assert store.read().format_version == COMMIT_STATE_FORMAT_VERSION


def test_recovery_heals_the_v1_fallback_left_by_an_interrupted_promotion(
    stack: Stack,
) -> None:
    """A restart replaces the residual v1 fallback without rewriting state forever."""
    _persist_catalog_v2(stack)
    legacy = CommitState()
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    _state_store(stack.storage, owner_id="legacy").publish(legacy, previous=legacy)

    fault = FaultInjectingStorageDevice(
        stack.storage,  # type: ignore[arg-type]
        plan=FaultPlan(device_full_method="write_page", device_full_occurrence=2),
    )
    with pytest.raises(GrafxDeviceFull):
        _state_store(fault, owner_id="interrupted").publish(fenced, previous=legacy)

    # The first publication made v2 authoritative, but corrupting that sole slot exposes the
    # v1 fallback and demonstrates why merely reading the newest payload is not enough.
    first_v2 = _damage_slot(stack.storage, CONTROL_SLOT_PAGES[1])
    assert (
        _state_store(stack.storage, owner_id="window-probe").read().format_version
        == COMMIT_STATE_LEGACY_FORMAT_VERSION
    )
    stack.storage.write_page(  # type: ignore[attr-defined]
        COMMIT_STATE_FILE, CONTROL_SLOT_PAGES[1], first_v2
    )

    healer = _state_store(stack.storage, owner_id="recovery")
    report = stack.recovery(commit_state_store=healer).run()
    assert report.records_replayed == 0

    # Recovery must have replaced the old page-1 fallback with v2. Damage the original sole-v2
    # page: without healing this would expose v1 again; with healing page 1 still reads as v2.
    _damage_slot(stack.storage, CONTROL_SLOT_PAGES[1])
    assert (
        _state_store(stack.storage, owner_id="released-reader").read().format_version
        == COMMIT_STATE_FORMAT_VERSION
    )


def test_recovery_refuses_state_v2_over_catalog_v1_without_touching_state_bytes(
    stack: Stack,
) -> None:
    """The monotonic fence cannot legitimize a catalog that has moved backwards."""
    stack.pool.flush(CATALOG_FILE)
    assert (
        stack.catalog.read_from_pages().format_version
        == COMMIT_STATE_LEGACY_FORMAT_VERSION
    )
    store = _state_store(stack.storage, owner_id="seed-v2")
    legacy = CommitState()
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    store.publish(legacy, previous=legacy)
    store.publish(fenced, previous=legacy)
    size = stack.storage.file_size(COMMIT_STATE_FILE)  # type: ignore[attr-defined]
    before = stack.storage.read_log(  # type: ignore[attr-defined]
        COMMIT_STATE_FILE, 0, size
    )

    with pytest.raises(GrafxRecoveryRefused) as refused:
        stack.recovery(
            commit_state_store=_state_store(stack.storage, owner_id="refusal")
        ).run()

    assert refused.value.details["field"] == "format_version"
    assert (
        stack.storage.read_log(COMMIT_STATE_FILE, 0, size)  # type: ignore[attr-defined]
        == before
    )


@pytest.mark.parametrize("control_format_version", [1, 2])
def test_a_stale_predecessor_cannot_lower_an_existing_fence(
    control_format_version: int,
) -> None:
    """The claimed predecessor is checked against storage in both physical protocols."""
    storage = MemoryStorageDevice()
    writer = _state_store(
        storage,
        owner_id="first-writer",
        control_format_version=control_format_version,
    )
    legacy = CommitState(last_committed_lsn=1, last_csn=1)
    fenced = CommitState(
        last_committed_lsn=2,
        last_csn=2,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    writer.publish(legacy, previous=CommitState())
    writer.publish(fenced, previous=legacy)
    before = _commit_state_bytes(storage)

    stale_writer = _state_store(
        storage,
        owner_id="stale-writer",
        control_format_version=control_format_version,
    )
    with pytest.raises(GrafxCorruptionDetected):
        stale_writer.publish(
            CommitState(last_committed_lsn=3, last_csn=3),
            previous=legacy,
        )

    assert _commit_state_bytes(storage) == before
    assert stale_writer.read() == fenced


def test_commit_state_subclasses_are_refused_before_any_storage_mutation() -> None:
    """Two physical writes may never call an attacker-controlled ``encode`` override."""
    storage = MemoryStorageDevice()
    store = _state_store(storage)

    with pytest.raises(GrafxConfigurationError):
        store.publish(_HostileCommitState(), previous=CommitState())

    assert not storage.exists(COMMIT_STATE_FILE)


def test_v1_to_v2_promotion_at_generation_max_minus_one_refuses_before_write() -> None:
    """The two-write transition reserves both generations before changing either slot."""
    from okto_grafx.domain.control_record import _encode_slot  # noqa: PLC2701

    storage = FaultInjectingStorageDevice(MemoryStorageDevice())
    store = _state_store(storage)
    legacy = CommitState()
    store.publish(legacy, previous=legacy)
    slots = store._slots  # noqa: SLF001 - exact generation-boundary fixture
    assert slots is not None
    header = slots._read_header()  # noqa: SLF001 - exact generation-boundary fixture
    storage.write_page(
        COMMIT_STATE_FILE,
        CONTROL_SLOT_PAGES[0],
        _encode_slot(
            header=header,
            generation=MAX_U64 - 1,
            payload=legacy.encode(),
            page_size=storage.page_size,
        ),
    )
    before = _commit_state_bytes(storage)
    storage.clear_trail()

    with pytest.raises(GrafxCorruptionDetected, match="without wrapping"):
        store.publish(
            CommitState(format_version=COMMIT_STATE_FORMAT_VERSION),
            previous=legacy,
        )

    assert _commit_state_bytes(storage) == before
    assert all(call.method != "write_page" for call in storage.trail())


@pytest.mark.parametrize("damage", ["header", "both_slots"])
def test_recovery_rebuilds_replaceable_outer_damage_with_two_v2_copies(
    stack: Stack,
    damage: str,
) -> None:
    """Complete WAL authority may rebuild torn local bytes, then restores redundancy."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [b"lineage"], page_index=3))],
    )
    _persist_catalog_v2(stack)
    store = _state_store(stack.storage, owner_id="damage-seed")
    legacy = store.read()
    fenced = CommitState(
        last_committed_lsn=legacy.last_committed_lsn,
        last_csn=legacy.last_csn,
        checkpoint_lsn=legacy.checkpoint_lsn,
        format_version=COMMIT_STATE_FORMAT_VERSION,
    )
    store.publish(fenced, previous=legacy)

    pages = (0,) if damage == "header" else CONTROL_SLOT_PAGES
    for page_index in pages:
        _damage_slot(stack.storage, page_index)

    recovering = _state_store(stack.storage, owner_id=f"recover-{damage}")
    with pytest.raises(GrafxCorruptionDetected):
        recovering.read()

    stack.recovery(commit_state_store=recovering).run()

    rebuilt = recovering.read()
    assert rebuilt.format_version == COMMIT_STATE_FORMAT_VERSION
    assert recovering.redundancy_needs_repair(rebuilt) is False


def test_a_foreign_control_binding_is_never_reclassified_as_rebuildable_damage(
    stack: Stack,
) -> None:
    """A valid record bound to another UUID is preserved byte-for-byte and fails closed."""
    _persist_catalog_v2(stack)
    owner = _state_store(stack.storage, owner_id="binding-owner")
    legacy = CommitState()
    fenced = CommitState(format_version=COMMIT_STATE_FORMAT_VERSION)
    owner.publish(legacy, previous=legacy)
    owner.publish(fenced, previous=legacy)
    before = _commit_state_bytes(stack.storage)
    foreign = CommitStateStore(
        stack.storage,  # type: ignore[arg-type]
        owner_id="foreign-binding",
        database_uuid=b"x" * 16,
        file_nonce=_FILE_NONCE,
        control_format_version=2,
    )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        stack.recovery(commit_state_store=foreign).run()

    assert refused.value.details["field"] == "control_binding"
    assert refused.value.details["commit_state_reconstructible"] is False
    assert _commit_state_bytes(stack.storage) == before
