"""Detached exact-generation construction before catalog publication."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from okto_grafx.domain.errors import GrafxIndexError, GrafxUnsupportedOperation
from okto_grafx.domain.index.definition import (
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    index_generation_file,
)
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager

from .conftest import (
    TEST_BUCKET_COUNT,
    MemoryDevice,
    RecordingMetrics,
    SnapshotDouble,
    header_on_device,
    make_pool,
)


HORIZON = 23
NONCE = 0xD37AC4ED
UNSIGNED_RECORD_ID = (1 << 63) + 73


def _identity_definition(table: TableDef, *, nonce: int = NONCE) -> IndexDefinition:
    return IndexDefinition(
        name=f"rid_t_{table.table_id:08x}",
        table_id=table.table_id,
        table_name=table.name,
        positions=(),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
        artifact_nonce=nonce,
    )


def test_detached_build_preserves_history_tombstones_u64_and_global_horizon(
    manager: IndexManager,
    pool: BufferPool,
    device: MemoryDevice,
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    old = heap_store.insert(
        person_table,
        UNSIGNED_RECORD_ID,
        (1, "before"),
        xmin=3,
    )
    new = heap_store.update(person_table, old, (1, "after"), xmin=11)
    heap_store.delete(person_table, new, xmax=17)
    live_record_id = UNSIGNED_RECORD_ID + 1
    live = heap_store.insert(person_table, live_record_id, (2, "live"), xmin=19)
    pool.flush(heap_store.file)
    definition = _identity_definition(person_table)

    detached = manager._build_detached_exact_generation(definition, HORIZON)

    entries = {entry.ref: entry for entry in detached.walk()}
    assert set(entries) == {old, new, live}
    assert entries[old].key == record_id_key(UNSIGNED_RECORD_ID)
    assert entries[old].dead_csn == 11
    assert entries[new].key == record_id_key(UNSIGNED_RECORD_ID)
    assert entries[new].dead_csn == 17
    assert entries[live].key == record_id_key(live_record_id)
    assert entries[live].live
    assert all(
        not entry.versioned and entry.born_csn == 0 for entry in entries.values()
    )

    at_old = manager.validated_versions(
        detached,
        record_id_key(UNSIGNED_RECORD_ID),
        SnapshotDouble(7),
    )
    at_new = manager.validated_versions(
        detached,
        record_id_key(UNSIGNED_RECORD_ID),
        SnapshotDouble(13),
    )
    after_delete = manager.validated_versions(
        detached,
        record_id_key(UNSIGNED_RECORD_ID),
        SnapshotDouble(HORIZON),
    )
    assert tuple(ref for ref, _version in at_old) == (old,)
    assert tuple(ref for ref, _version in at_new) == (new,)
    assert after_delete == ()

    durable = header_on_device(device, detached.file)
    assert durable.artifact_nonce == NONCE
    assert durable.built_through_lsn == HORIZON
    assert device.barriers == [detached.file]
    cold_metrics = RecordingMetrics()
    cold = HashIndex(definition, make_pool(device, cold_metrics), cold_metrics)
    assert cold.open().built_through_lsn == HORIZON
    assert {(entry.key, entry.ref, entry.dead_csn) for entry in cold.walk()} == {
        (entry.key, entry.ref, entry.dead_csn) for entry in entries.values()
    }
    assert manager._verify_entries(detached) == ()
    assert manager._verify_coverage(detached) == ()
    assert manager.indexes() == ()
    with pytest.raises(GrafxIndexError):
        manager.index(detached.name)


def test_existing_generation_file_is_refused_without_adoption_or_mutation(
    manager: IndexManager,
    pool: BufferPool,
    device: MemoryDevice,
    person_table: TableDef,
) -> None:
    definition = _identity_definition(person_table)
    detached = manager._build_detached_exact_generation(definition, through_lsn=0)
    before = tuple(
        device.raw_page(detached.file, page_index)
        for page_index in range(device.page_count(detached.file))
    )
    barriers = tuple(device.barriers)

    with pytest.raises(GrafxUnsupportedOperation):
        manager._build_detached_exact_generation(definition, through_lsn=0)

    assert (
        tuple(
            device.raw_page(detached.file, page_index)
            for page_index in range(device.page_count(detached.file))
        )
        == before
    )
    assert tuple(device.barriers) == barriers
    assert detached.walk() == ()
    assert not pool.has_dirty_pages(detached.file)
    assert manager.indexes() == ()


def test_preexisting_empty_generation_file_is_not_repaired_or_adopted(
    manager: IndexManager,
    device: MemoryDevice,
    person_table: TableDef,
) -> None:
    definition = _identity_definition(person_table)
    device.create(definition.file, exclusive=True)

    with pytest.raises(GrafxUnsupportedOperation):
        manager._build_detached_exact_generation(definition, through_lsn=0)

    assert device.page_count(definition.file) == 0
    assert device.barriers == []
    assert manager.indexes() == ()


def test_detached_build_requires_an_exact_preallocated_generation(
    manager: IndexManager,
    device: MemoryDevice,
    person_table: TableDef,
) -> None:
    without_nonce = _identity_definition(person_table, nonce=0)
    proximity = IndexDefinition.on(
        person_table,
        name="detached_proximity",
        columns=("name",),
        visibility=IndexVisibility.PROXIMITY,
        bucket_count=TEST_BUCKET_COUNT,
        artifact_nonce=NONCE + 1,
    )

    with pytest.raises(GrafxIndexError) as missing_nonce:
        manager._build_detached_exact_generation(without_nonce, through_lsn=0)
    with pytest.raises(GrafxIndexError) as wrong_visibility:
        manager._build_detached_exact_generation(proximity, through_lsn=0)

    assert missing_nonce.value.details["field"] == "artifact_nonce"
    assert wrong_visibility.value.details["field"] == "visibility"
    assert not device.exists(index_generation_file(NONCE + 1))
    assert manager.indexes() == ()


def test_horizon_older_than_the_fenced_heap_view_refuses_publication(
    manager: IndexManager,
    pool: BufferPool,
    device: MemoryDevice,
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    heap_store.insert(person_table, 1, (1, "later"), xmin=HORIZON + 1)
    pool.flush(heap_store.file)
    definition = _identity_definition(person_table)

    with pytest.raises(GrafxIndexError) as refused:
        manager._build_detached_exact_generation(definition, HORIZON)

    assert refused.value.details["field"] == "through_lsn"
    assert device.exists(definition.file), (
        "the exclusive but unreachable path is an orphan"
    )
    assert not pool.has_dirty_pages(definition.file)
    assert device.barriers == []
    assert manager.indexes() == ()


def test_failed_build_leaves_only_an_unregistered_orphan_and_drops_dirty_frames(
    manager: IndexManager,
    pool: BufferPool,
    device: MemoryDevice,
    heap_store: HeapStore,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heap_store.insert(person_table, 1, (1, "Ada"), xmin=3)
    pool.flush(heap_store.file)
    definition = _identity_definition(person_table)
    original = HashIndex._apply_empty_build_change
    calls = 0

    def fail_after_first_write(
        index: HashIndex,
        build: object,
        change: object,
        lsn: int,
    ) -> bool | None:
        nonlocal calls
        moved = original(index, build, change, lsn)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            raise GrafxIndexError("injected detached-build failure", field="injected")
        return moved

    monkeypatch.setattr(
        HashIndex, "_apply_empty_build_change", fail_after_first_write
    )

    with pytest.raises(GrafxIndexError, match="injected detached-build failure"):
        manager._build_detached_exact_generation(definition, HORIZON)

    assert device.exists(definition.file), "G6 preserves the inaccessible orphan"
    assert manager.indexes() == ()
    assert not pool.has_dirty_pages(definition.file)
    assert device.barriers == []


def _manager_with_nonce_provider(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: RecordingMetrics,
    provider: Callable[[], int],
) -> IndexManager:
    return IndexManager(pool, heap_store, metrics, artifact_nonce=provider)


def test_nonce_allocation_skips_catalog_and_orphan_collisions_without_reserving(
    pool: BufferPool,
    device: MemoryDevice,
    heap_store: HeapStore,
    metrics: RecordingMetrics,
) -> None:
    offered = iter((11, 12, 13))
    manager = _manager_with_nonce_provider(
        pool, heap_store, metrics, lambda: next(offered)
    )
    device.create(index_generation_file(12), exclusive=True)

    assert manager._allocate_detached_generation_nonce({11}) == 13
    assert not device.exists(index_generation_file(13)), "allocation is not reservation"


@pytest.mark.parametrize("invalid", [False, 0, -1, 1 << 64, None, 1.5])
def test_nonce_provider_values_outside_nonzero_u64_are_refused(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: RecordingMetrics,
    invalid: object,
) -> None:
    manager = _manager_with_nonce_provider(
        pool,
        heap_store,
        metrics,
        lambda: invalid,  # type: ignore[return-value]
    )

    with pytest.raises(GrafxIndexError) as refused:
        manager._allocate_detached_generation_nonce(set())

    assert refused.value.details["field"] == "artifact_nonce"


def test_nonce_collisions_stop_at_the_fixed_retry_bound(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: RecordingMetrics,
) -> None:
    calls = 0

    def occupied() -> int:
        nonlocal calls
        calls += 1
        return 7

    manager = _manager_with_nonce_provider(pool, heap_store, metrics, occupied)

    with pytest.raises(GrafxIndexError) as refused:
        manager._allocate_detached_generation_nonce({7})

    assert refused.value.details["field"] == "artifact_nonce"
    assert refused.value.details["attempts"] == calls == 64
