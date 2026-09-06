"""Composition adopts catalog-v2 exact generations without inventing artifacts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.api.assembly import _attach_primary_key_indexes, _verifier_factory
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager

from .conftest import MemoryDevice, RecordingMetrics


def _active_definition(
    table: TableDef,
    *,
    nonce: int = 0xA11CE,
) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name="by_name",
        table_id=table.table_id,
        table_name=table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        generations=(IndexGenerationDescriptor(nonce, 4, "active"),),  # type: ignore[arg-type]
    )


def _attach(
    catalog: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    device: MemoryDevice,
    *,
    existing_only: bool,
) -> tuple[str, ...]:
    return _attach_primary_key_indexes(
        catalog,
        manager,
        pool,
        metrics,
        definitions=catalog.catalog.active_index_definitions(),
        existing_only=existing_only,
        existing_files=frozenset(device.list_files("index/")),
    )


def test_v2_adopts_the_exact_active_generation_named_by_the_catalog(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    device: MemoryDevice,
    person_table: TableDef,
) -> None:
    logical = _active_definition(person_table)
    expected = logical.runtime_definition()
    manager.register(HashIndex(expected, pool, metrics))
    pool.flush(expected.file)
    assert manager.unregister(expected.name)
    catalog_store.catalog.upgrade_index_catalog((logical,))

    attached = _attach(
        catalog_store,
        manager,
        pool,
        metrics,
        device,
        existing_only=True,
    )

    assert attached == (expected.name,)
    assert manager.index(expected.name).definition == expected
    assert manager.index(expected.name).file == "index/g_00000000000a11ce.idx"


@pytest.mark.parametrize("existing_only", [True, False])
def test_v2_never_creates_a_missing_active_generation_during_composition(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    device: MemoryDevice,
    person_table: TableDef,
    existing_only: bool,
) -> None:
    logical = _active_definition(person_table)
    catalog_store.catalog.upgrade_index_catalog((logical,))
    expected_file = logical.runtime_definition().file
    before = device.list_files()

    with pytest.raises(GrafxIndexError) as refused:
        _attach(
            catalog_store,
            manager,
            pool,
            metrics,
            device,
            existing_only=existing_only,
        )

    assert refused.value.details["field"] == "file"
    assert refused.value.details["artifact_nonce"] == 0xA11CE
    assert not device.exists(expected_file)
    assert device.list_files() == before


def test_v2_refuses_a_generation_whose_header_has_another_definition(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    device: MemoryDevice,
    person_table: TableDef,
) -> None:
    logical = _active_definition(person_table)
    expected = logical.runtime_definition()
    foreign = replace(expected, positions=(0,))
    manager.register(HashIndex(foreign, pool, metrics))
    pool.flush(foreign.file)
    assert manager.unregister(foreign.name)
    catalog_store.catalog.upgrade_index_catalog((logical,))
    before = tuple(
        device.raw_page(foreign.file, page)
        for page in range(device.page_count(foreign.file))
    )

    with pytest.raises(GrafxIndexError) as refused:
        _attach(
            catalog_store,
            manager,
            pool,
            metrics,
            device,
            existing_only=True,
        )

    assert refused.value.details["field"] == "digest"
    assert tuple(
        device.raw_page(foreign.file, page)
        for page in range(device.page_count(foreign.file))
    ) == before


def test_verifier_factory_receives_only_catalog_selected_registrations(
    catalog_store: CatalogStore,
    heap_store: HeapStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    active = _active_definition(person_table)
    building = CatalogIndexDefinition(
        name="building_by_name",
        table_id=person_table.table_id,
        table_name=person_table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        generations=(IndexGenerationDescriptor(0xB001D, 4, "building"),),  # type: ignore[arg-type]
    )
    catalog_store.catalog.upgrade_index_catalog((active, building))
    for logical in (active, building):
        manager.register(
            HashIndex(
                logical.runtime_definition(logical.generations[0]),
                pool,
                metrics,
            )
        )

    verifier = _verifier_factory(
        pool,
        metrics,
        heap_store,
        catalog_store,
        manager,
    )()

    assert tuple(index.name for index in verifier._indexes) == ("by_name",)


def test_verifier_factory_refuses_incomplete_v2_active_coverage(
    catalog_store: CatalogStore,
    heap_store: HeapStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """A missing ACTIVE store cannot turn into a falsely clean verification report."""

    active = _active_definition(person_table)
    catalog_store.catalog.upgrade_index_catalog((active,))

    with pytest.raises(GrafxIndexError) as refused:
        _verifier_factory(
            pool,
            metrics,
            heap_store,
            catalog_store,
            manager,
        )()

    assert refused.value.details["field"] == "index_authority"
    assert refused.value.details["missing"] == ("by_name",)
