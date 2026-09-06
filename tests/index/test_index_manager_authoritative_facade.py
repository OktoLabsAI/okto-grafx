"""Adversarial boundaries for the catalog-authoritative index facade.

The process-local registry deliberately remains broader than the committed catalog.  These
tests prove that consumers can ask for the ACTIVE projection without accidentally admitting a
BUILDING, STALE, rogue, or physically different generation, while a schema transaction can
still maintain the speculative index it owns.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
from okto_grafx.domain.index.records import change_of
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.page import HEADER_PAGE_INDEX
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager, IndexStore

from .conftest import RecordingMetrics, TransactionDouble, exact_definition


def _logical(
    table: TableDef,
    *,
    name: str,
    generations: tuple[IndexGenerationDescriptor, ...],
) -> CatalogIndexDefinition:
    """Describe one catalog-v2 exact index over ``Person.name``."""

    return CatalogIndexDefinition(
        name=name,
        table_id=table.table_id,
        table_name=table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        generations=generations,
    )


def _generation(
    nonce: int, state: IndexGenerationState
) -> IndexGenerationDescriptor:
    return IndexGenerationDescriptor(nonce, 4, state)


def _register_generation(
    manager: IndexManager,
    logical: CatalogIndexDefinition,
    generation: IndexGenerationDescriptor,
    pool: BufferPool,
    metrics: RecordingMetrics,
) -> IndexStore:
    return manager.register(
        HashIndex(logical.runtime_definition(generation), pool, metrics)
    )


def test_authoritative_facade_intersects_registry_with_only_catalog_active_generation(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """BUILDING, STALE, and unpersisted stores stay raw-registry state only."""

    active_generation = _generation(0xA001, IndexGenerationState.ACTIVE)
    building_generation = _generation(0xB001, IndexGenerationState.BUILDING)
    stale_generation = _generation(0xC001, IndexGenerationState.STALE)
    active = _logical(
        person_table,
        name="catalog_active",
        generations=(active_generation,),
    )
    building = _logical(
        person_table,
        name="catalog_building",
        generations=(building_generation,),
    )
    stale = _logical(
        person_table,
        name="catalog_stale",
        generations=(stale_generation,),
    )
    catalog_store.catalog.upgrade_index_catalog((active, building, stale))
    catalog_store.save()

    active_store = _register_generation(
        manager, active, active_generation, pool, metrics
    )
    _register_generation(manager, building, building_generation, pool, metrics)
    _register_generation(manager, stale, stale_generation, pool, metrics)
    rogue_definition = replace(
        exact_definition(person_table, name="process_local_rogue"),
        artifact_nonce=0xBAD,
    )
    manager.register(HashIndex(rogue_definition, pool, metrics))

    assert {index.name for index in manager.indexes()} == {
        "catalog_active",
        "catalog_building",
        "catalog_stale",
        "process_local_rogue",
    }
    assert manager.active_indexes() == (active_store,)
    assert manager.active_indexes(catalog=catalog_store.catalog) == (active_store,)
    assert manager.active_index(
        "catalog_active", catalog=catalog_store.catalog
    ) is active_store
    for forbidden in ("catalog_building", "catalog_stale", "process_local_rogue"):
        with pytest.raises(GrafxIndexError):
            manager.active_index(forbidden, catalog=catalog_store.catalog)
        with pytest.raises(GrafxIndexError):
            manager.verify(forbidden)


def test_authoritative_facade_requires_complete_definition_equality_including_nonce(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """A same-named store from another physical generation cannot satisfy authority."""

    active_generation = _generation(0xD001, IndexGenerationState.ACTIVE)
    logical = _logical(
        person_table,
        name="same_semantics_different_generation",
        generations=(active_generation,),
    )
    catalog_store.catalog.upgrade_index_catalog((logical,))
    catalog_store.save()
    expected = logical.runtime_definition(active_generation)
    wrong_generation = replace(expected, artifact_nonce=0xD002)
    wrong_store = manager.register(HashIndex(wrong_generation, pool, metrics))

    # The raw registry owns the object, but the ACTIVE projection owns a different file.
    assert manager.index(expected.name) is wrong_store
    assert wrong_store.definition != expected
    assert manager.active_indexes(catalog=catalog_store.catalog) == ()
    with pytest.raises(GrafxIndexError):
        manager.active_index(expected.name, catalog=catalog_store.catalog)


def test_registered_active_generations_skip_the_persistent_inventory(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact registered target leaves physical proof to the later fresh certificate."""
    generation = _generation(0xD101, IndexGenerationState.ACTIVE)
    logical = _logical(
        person_table, name="steady_active", generations=(generation,)
    )
    catalog_store.catalog.upgrade_index_catalog((logical,))
    _register_generation(manager, logical, generation, pool, metrics)

    def forbidden(_prefix: str = "") -> tuple[str, ...]:
        pytest.fail("a resolved ACTIVE registry must not inventory index/")

    monkeypatch.setattr(pool.storage, "list_files", forbidden)

    assert manager.unregistered_persistent_indexes_for(
        (person_table,), catalog=catalog_store.catalog
    ) == ()


def test_unresolved_foreign_generation_materializes_one_inventory(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog generation absent from the local registry is classified with one listing."""
    generation = _generation(0xD102, IndexGenerationState.ACTIVE)
    logical = _logical(
        person_table, name="foreign_active", generations=(generation,)
    )
    catalog_store.catalog.upgrade_index_catalog((logical,))
    store = _register_generation(manager, logical, generation, pool, metrics)
    pool.flush(store.file)
    assert manager.unregister(store.name)
    original = pool.storage.list_files
    calls: list[str] = []

    def counted(prefix: str = "") -> tuple[str, ...]:
        calls.append(prefix)
        return original(prefix)

    monkeypatch.setattr(pool.storage, "list_files", counted)

    assert manager.unregistered_persistent_indexes_for(
        (person_table,), catalog=catalog_store.catalog
    ) == (store.name,)
    assert calls == ["index/"]


def test_missing_v2_and_optional_v1_keep_their_inventory_semantics(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pool.storage.list_files
    calls: list[str] = []

    def counted(prefix: str = "") -> tuple[str, ...]:
        calls.append(prefix)
        return original(prefix)

    monkeypatch.setattr(pool.storage, "list_files", counted)
    legacy = next(
        definition
        for definition in catalog_store.catalog.active_index_definitions()
        if definition.table_id == person_table.table_id
    )
    assert manager.unregistered_persistent_indexes_for(
        (person_table,), catalog=catalog_store.catalog
    ) == ()
    assert calls == ["index/"]

    generation = _generation(0xD103, IndexGenerationState.ACTIVE)
    logical = _logical(person_table, name="missing_v2", generations=(generation,))
    catalog_store.catalog.upgrade_index_catalog((logical,))
    assert manager.unregistered_persistent_indexes_for(
        (person_table,), catalog=catalog_store.catalog
    ) == (logical.name,)
    assert calls == ["index/", "index/"]
    assert legacy.artifact_nonce == 0


@pytest.mark.parametrize("mismatch", ("definition", "nonce"))
def test_unresolved_v2_header_mismatch_remains_an_obligation(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    generation = _generation(0xD104, IndexGenerationState.ACTIVE)
    logical = _logical(person_table, name="mismatched_v2", generations=(generation,))
    expected = logical.runtime_definition(generation)
    physical = replace(expected, positions=(0,)) if mismatch == "definition" else expected
    store = manager.register(HashIndex(physical, pool, metrics))
    pool.flush(store.file)
    if mismatch == "nonce":
        with pool.pinned(store.file, HEADER_PAGE_INDEX) as page:
            header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
            page.update_slot(
                INDEX_HEADER_SLOT,
                replace(header, artifact_nonce=generation.artifact_nonce + 1).encode(),
            )
        pool.flush(store.file)
        assert manager.unregister(store.name)
    catalog_store.catalog.upgrade_index_catalog((logical,))
    original = pool.storage.list_files
    calls = 0

    def counted(prefix: str = "") -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original(prefix)

    monkeypatch.setattr(pool.storage, "list_files", counted)

    assert manager.unregistered_persistent_indexes_for(
        (person_table,), catalog=catalog_store.catalog
    ) == (logical.name,)
    assert calls == 1


def test_row_maintenance_adds_only_the_calling_transactions_speculative_index(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """Committed ACTIVE plus own DDL speculation is the complete staging/counting set."""

    active_generation = _generation(0xE001, IndexGenerationState.ACTIVE)
    logical = _logical(
        person_table,
        name="committed_active",
        generations=(active_generation,),
    )
    catalog_store.catalog.upgrade_index_catalog((logical,))
    catalog_store.save()
    active_store = _register_generation(
        manager, logical, active_generation, pool, metrics
    )

    speculative_definition = replace(
        exact_definition(person_table, name="own_speculative"),
        artifact_nonce=0xE002,
    )
    speculative, artifact = manager.register_speculative(
        HashIndex(speculative_definition, pool, metrics), complete_through=0
    )
    assert artifact is not None
    owner = TransactionDouble(txn_id=71)
    stranger = TransactionDouble(txn_id=72)
    manager.stage_schema_observation(speculative, owner)
    values = (1, "Ada")

    assert manager.active_indexes_for(
        person_table.table_id,
        table_name=person_table.name,
        table=person_table,
    ) == (active_store,)
    assert manager.active_indexes_for(
        person_table.table_id,
        table_name=person_table.name,
        table=person_table,
        txn=stranger,
    ) == (active_store,)
    assert set(
        manager.active_indexes_for(
            person_table.table_id,
            table_name=person_table.name,
            table=person_table,
            txn=owner,
        )
    ) == {active_store, speculative}
    assert manager.row_entry_count(
        person_table.table_id,
        values,
        table_name=person_table.name,
        table=person_table,
    ) == 1
    assert manager.row_entry_count(
        person_table.table_id,
        values,
        table_name=person_table.name,
        table=person_table,
        txn=stranger,
    ) == 1
    assert manager.row_entry_count(
        person_table.table_id,
        values,
        table_name=person_table.name,
        table=person_table,
        txn=owner,
    ) == 2

    records = manager.stage_row_insert(
        owner,
        person_table.table_id,
        RecordRef(1, 0),
        values,
        7,
        table_name=person_table.name,
        table=person_table,
    )
    assert {change_of(record).index for record in records} == {
        "committed_active",
        "own_speculative",
    }


def test_physical_replacement_guard_protects_every_catalog_v2_generation(
    catalog_store: CatalogStore,
    manager: IndexManager,
    person_table: TableDef,
) -> None:
    """A shadow or retired generation remains declared physical state until catalog GC."""

    generations = (
        _generation(0xF001, IndexGenerationState.STALE),
        _generation(0xF002, IndexGenerationState.ACTIVE),
        _generation(0xF003, IndexGenerationState.BUILDING),
    )
    logical = _logical(
        person_table,
        name="multi_generation",
        generations=generations,
    )
    catalog_store.catalog.upgrade_index_catalog((logical,))
    catalog_store.save()

    for generation in generations:
        assert manager._canonical_file_is_declared(generation.file)
        assert manager._canonical_file_is_declared(generation.file.upper())
    undeclared = _generation(0xF004, IndexGenerationState.STALE)
    assert not manager._canonical_file_is_declared(undeclared.file)
