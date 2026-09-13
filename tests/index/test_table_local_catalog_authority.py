"""Discriminating tests for table-local catalog authority resolution.

These assertions use call-path sentinels instead of wall-clock timing: a per-row operation must
remain correct when the complete catalog projection is made unreachable.  That proves unrelated
indexes cannot silently return to the hot path as the graph grows.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.records import change_of
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
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
    return CatalogIndexDefinition(
        name=name,
        table_id=table.table_id,
        table_name=table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        generations=generations,
    )


def _generation(nonce: int, state: IndexGenerationState) -> IndexGenerationDescriptor:
    return IndexGenerationDescriptor(nonce, 4, state)


def _register(
    manager: IndexManager,
    logical: CatalogIndexDefinition,
    generation: IndexGenerationDescriptor,
    pool: BufferPool,
    metrics: RecordingMetrics,
) -> IndexStore:
    return manager.register(
        HashIndex(logical.runtime_definition(generation), pool, metrics)
    )


def test_row_hot_path_never_materializes_unrelated_catalog_authority(
    monkeypatch: pytest.MonkeyPatch,
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """Count, staging, and exact-name lookup avoid the complete v2 projection."""

    catalog = catalog_store.catalog
    active_generation = _generation(0x1000, IndexGenerationState.ACTIVE)
    target = _logical(
        person_table,
        name="target_by_name",
        generations=(active_generation,),
    )
    definitions = [target]
    for offset in range(1, 65):
        table = TableDef(
            table_id=catalog.next_table_id(),
            name=f"Unrelated{offset}",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="value", type=ValueType.STRING),
            ),
        )
        catalog.add_table(table)
        definitions.append(
            _logical(
                table,
                name=f"unrelated_by_value_{offset}",
                generations=(
                    _generation(0x1000 + offset, IndexGenerationState.ACTIVE),
                ),
            )
        )
    catalog.upgrade_index_catalog(tuple(definitions))
    target_store = _register(manager, target, active_generation, pool, metrics)

    full_projection_calls = 0
    table_projection_calls: list[tuple[int, str | None]] = []
    original_table_projection = Catalog.active_index_definitions_for

    def refuse_full_projection(_catalog: Catalog) -> tuple[object, ...]:
        nonlocal full_projection_calls
        full_projection_calls += 1
        raise AssertionError("the per-row path requested every catalog index")

    def track_table_projection(
        selected: Catalog, table_id: int, *, table_name: str | None = None
    ) -> tuple[object, ...]:
        table_projection_calls.append((table_id, table_name))
        return original_table_projection(selected, table_id, table_name=table_name)

    monkeypatch.setattr(Catalog, "active_index_definitions", refuse_full_projection)
    monkeypatch.setattr(Catalog, "active_index_definitions_for", track_table_projection)

    # A persisted exact name is resolved directly through Catalog.index_definition().
    assert manager.active_index("target_by_name") is target_store
    assert table_projection_calls == []

    values = (1, "Ada")
    assert (
        manager.row_entry_count(
            person_table.table_id,
            values,
            table_name=person_table.name,
            table=person_table,
        )
        == 1
    )
    records = manager.stage_row_insert(
        TransactionDouble(txn_id=19),
        person_table.table_id,
        RecordRef(1, 0),
        values,
        7,
        table_name=person_table.name,
        table=person_table,
    )

    assert tuple(change_of(record).index for record in records) == ("target_by_name",)
    assert table_projection_calls == [
        (person_table.table_id, person_table.name),
        (person_table.table_id, person_table.name),
    ]
    assert full_projection_calls == 0
    assert len(catalog.index_definitions()) == 65


def test_same_catalog_object_rebuilds_table_map_on_generation_activation(
    monkeypatch: pytest.MonkeyPatch,
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """An in-place authority replacement cannot leave an old ACTIVE nonce cached."""

    first = _generation(0x2001, IndexGenerationState.ACTIVE)
    shadow = _generation(0x2002, IndexGenerationState.BUILDING)
    logical = _logical(
        person_table,
        name="replace_in_place",
        generations=(first, shadow),
    )
    catalog = catalog_store.catalog
    catalog.upgrade_index_catalog((logical,))
    first_store = _register(manager, logical, first, pool, metrics)

    def refuse_full_projection(_catalog: Catalog) -> tuple[object, ...]:
        raise AssertionError("generation replacement fell back to global projection")

    monkeypatch.setattr(Catalog, "active_index_definitions", refuse_full_projection)
    assert manager.active_indexes_for(
        person_table.table_id, table_name=person_table.name, table=person_table
    ) == (first_store,)

    activated = logical.activate_generation(shadow.artifact_nonce)
    catalog.replace_index_definition(activated)

    # The old process-local object remains registered, but the same Catalog object now selects
    # another nonce.  Exact equality must make the interval before adoption fail closed.
    assert (
        manager.active_indexes_for(
            person_table.table_id, table_name=person_table.name, table=person_table
        )
        == ()
    )
    with pytest.raises(GrafxIndexError):
        manager.active_index(logical.name)

    assert manager.unregister(logical.name)
    active_shadow = activated.active_generation()
    assert active_shadow is not None
    replacement_store = _register(manager, activated, active_shadow, pool, metrics)
    assert manager.active_indexes_for(
        person_table.table_id, table_name=person_table.name, table=person_table
    ) == (replacement_store,)
    assert replacement_store.definition.artifact_nonce == shadow.artifact_nonce


def test_explicit_v1_catalog_keeps_valid_custom_handle_access_path(
    monkeypatch: pytest.MonkeyPatch,
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> None:
    """An explicit v1 photo is provenance, not nonexistent generation authority."""

    custom = manager.register(
        HashIndex(
            exact_definition(person_table, name="custom_v1_by_name"),
            pool,
            metrics,
        )
    )

    assert tuple(
        definition.name
        for definition in catalog_store.catalog.active_index_definitions_for(
            person_table.table_id, table_name=person_table.name
        )
    ) == ("pk_Person",)
    assert manager.active_indexes(catalog=catalog_store.catalog) == (custom,)

    def refuse_global_projection(
        _manager: IndexManager, *, catalog: object | None = None
    ) -> tuple[IndexStore, ...]:
        del catalog
        raise AssertionError("v1 row staging scanned the complete registry")

    def refuse_legacy_scan(
        _manager: IndexManager, catalog: object
    ) -> tuple[IndexStore, ...]:
        del catalog
        raise AssertionError("v1 exact-name lookup scanned the complete registry")

    monkeypatch.setattr(IndexManager, "active_indexes", refuse_global_projection)
    monkeypatch.setattr(IndexManager, "_legacy_active_indexes", refuse_legacy_scan)
    assert manager.active_indexes_for(
        person_table.table_id,
        table_name=person_table.name,
        table=person_table,
        catalog=catalog_store.catalog,
    ) == (custom,)
    assert manager.active_index(custom.name, catalog=catalog_store.catalog) is custom


def test_inexpressible_vector_name_does_not_hide_valid_v1_scalar_path(
    catalog_store: CatalogStore,
    manager: IndexManager,
    pool: BufferPool,
    metrics: RecordingMetrics,
) -> None:
    """A catalog round-trip keeps the valid scalar usable by every v1 facade."""

    catalog = catalog_store.catalog
    space_name = "vector_space"
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=catalog.next_space_id(),
            name=space_name,
            dimension=3,
            metric=DistanceMetric.COSINE,
            normalized=True,
        )
    )
    table_name = "T" * 120
    table = TableDef(
        table_id=catalog.next_table_id(),
        name=table_name,
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space=space_name),
        ),
        primary_key="id",
    )
    # New DDL must use the v2 bounded-name capability. This test instead models
    # a previously stored v1 catalog, whose scalar facade must remain readable.
    with pytest.raises(GrafxConfigurationError) as refused:
        catalog.add_table(table)
    assert refused.value.details["field"] == "format_version"
    assert not catalog.has_table(table_name)
    catalog._install_table(table)  # Same installation primitive as the legacy decoder.
    catalog_store.save()
    reopened = Catalog.deserialize(catalog.serialize())
    assert reopened.format_version == 1
    assert not reopened.table(table_name).vector_identity_names

    definitions = reopened.active_index_definitions_for(
        table.table_id, table_name=table.name
    )
    scalar = manager.register(HashIndex(definitions[0], pool, metrics))

    assert tuple(definition.name for definition in definitions) == (f"pk_{table_name}",)
    assert manager.active_indexes(catalog=reopened) == (scalar,)
    assert manager.active_index(scalar.name, catalog=reopened) is scalar
    assert manager.active_indexes_for(
        table.table_id,
        table_name=table.name,
        table=table,
        catalog=reopened,
    ) == (scalar,)

    records = manager.stage_row_insert(
        TransactionDouble(txn_id=29),
        table.table_id,
        RecordRef(2, 0),
        (1, None),
        9,
        table_name=table.name,
        table=table,
    )
    assert tuple(change_of(record).index for record in records) == (scalar.name,)
