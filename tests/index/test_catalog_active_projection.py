"""Runtime index authority projected from catalog v1 and v2."""

from __future__ import annotations

from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.key import VectorIndexDefinition


def _catalog_with_vector_schema(*, include_relationship: bool = False) -> Catalog:
    catalog = Catalog()
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name="minilm",
            dimension=384,
            metric=DistanceMetric.COSINE,
            normalized=True,
        )
    )
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Person",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="email", type=ValueType.STRING),
                ColumnDef(name="tenant", type=ValueType.STRING),
                ColumnDef(
                    name="embedding",
                    type=ValueType.VECTOR_F32,
                    vector_space="minilm",
                ),
            ),
            primary_key="id",
        )
    )
    if include_relationship:
        catalog.add_table(
            TableDef(
                table_id=2,
                name="Knows",
                kind="rel",
                columns=(ColumnDef(name="weight", type=ValueType.DOUBLE),),
                from_table="Person",
                to_table="Person",
            )
        )
    return catalog


def _exact(
    name: str,
    position: int,
    nonce: int,
    state: str,
    *,
    buckets: int = 64,
) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name=name,
        table_id=1,
        table_name="Person",
        positions=(position,),
        visibility=IndexVisibility.EXACT,
        generations=(IndexGenerationDescriptor(nonce, buckets, state),),  # type: ignore[arg-type]
    )


def test_v1_projects_every_schema_derived_automatic_index() -> None:
    catalog = _catalog_with_vector_schema(include_relationship=True)

    definitions = catalog.active_index_definitions()

    assert tuple(definition.name for definition in definitions) == (
        "ef_Knows",
        "et_Knows",
        "pk_Person",
        "vector_Person_minilm",
    )
    assert all(type(definition) is IndexDefinition for definition in definitions[:3])
    assert isinstance(definitions[3], VectorIndexDefinition)
    assert all(definition.artifact_nonce == 0 for definition in definitions)


def test_v2_projects_only_active_persisted_exact_indexes_and_schema_vectors() -> None:
    catalog = _catalog_with_vector_schema()
    catalog.upgrade_index_catalog(
        (
            _exact("by_tenant", 2, 12, "building", buckets=256),
            _exact("by_email", 1, 11, "active", buckets=128),
            _exact("by_id", 0, 10, "stale"),
        )
    )

    definitions = catalog.active_index_definitions()

    assert tuple(definition.name for definition in definitions) == (
        "by_email",
        "vector_Person_minilm",
    )
    active = definitions[0]
    assert type(active) is IndexDefinition
    assert active.bucket_count == 128
    assert active.artifact_nonce == 11
    assert active.file == "index/g_000000000000000b.idx"
    assert isinstance(definitions[1], VectorIndexDefinition)
    assert "pk_Person" not in {definition.name for definition in definitions}


def test_v2_does_not_construct_an_inexpressible_legacy_exact_name() -> None:
    table_name = "T" * 128
    catalog = Catalog()
    catalog.add_table(
        TableDef(
            table_id=1,
            name=table_name,
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64),),
            primary_key="id",
        )
    )
    persisted = CatalogIndexDefinition(
        name="by_id",
        table_id=1,
        table_name=table_name,
        positions=(0,),
        visibility=IndexVisibility.EXACT,
        generations=(IndexGenerationDescriptor(1, 64, "active"),),  # type: ignore[arg-type]
    )
    catalog.upgrade_index_catalog((persisted,))

    definitions = catalog.active_index_definitions()

    assert tuple(definition.name for definition in definitions) == ("by_id",)
