"""Shared fixtures for the query tests: a catalog, its indexes and a planning helper."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.plan import PlanNode
from okto_grafx.domain.query.planner import PlannedQuery, build_plan

SPACE_NAME: str = "minilm_v2"
SPACE_DIMENSION: int = 4


def build_catalog() -> Catalog:
    """Return the catalog every planning test is written against.

    Two node tables with a relationship between them, one node table carrying an embedding, and
    one self-referencing relationship so a variable-length traversal has somewhere to go.
    """
    catalog = Catalog()
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name=SPACE_NAME,
            dimension=SPACE_DIMENSION,
            metric=DistanceMetric.COSINE,
            normalized=True,
        )
    )
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=2,
            name="other_space",
            dimension=SPACE_DIMENSION,
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
                ColumnDef(name="name", type=ValueType.STRING),
                ColumnDef(name="age", type=ValueType.INT64),
                ColumnDef(name="city", type=ValueType.STRING),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="Chunk",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="layer", type=ValueType.INT64),
                ColumnDef(
                    name="embedding", type=ValueType.VECTOR_F32, vector_space=SPACE_NAME
                ),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=3,
            name="Doc",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="active", type=ValueType.BOOL),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=4,
            name="BELONGS_TO",
            kind="rel",
            columns=(ColumnDef(name="weight", type=ValueType.DOUBLE),),
            from_table="Chunk",
            to_table="Doc",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=5,
            name="Knows",
            kind="rel",
            columns=(ColumnDef(name="since", type=ValueType.INT64),),
            from_table="Person",
            to_table="Person",
        )
    )
    return catalog


def build_indexes() -> tuple[IndexDefinition, ...]:
    """Return the index definitions the planning tests offer the planner."""
    return (
        IndexDefinition(
            name="person_id",
            table_id=1,
            table_name="Person",
            positions=(0,),
            visibility=IndexVisibility.EXACT,
        ),
        IndexDefinition(
            name="person_city_age",
            table_id=1,
            table_name="Person",
            positions=(3, 2),
            visibility=IndexVisibility.EXACT,
        ),
        IndexDefinition(
            name="chunk_layer",
            table_id=2,
            table_name="Chunk",
            positions=(1,),
            visibility=IndexVisibility.PROXIMITY,
        ),
    )


@pytest.fixture
def catalog() -> Catalog:
    """Return a fresh catalog for one test."""
    return build_catalog()


@pytest.fixture
def indexes() -> tuple[IndexDefinition, ...]:
    """Return the index definitions for one test."""
    return build_indexes()


def plan_text(
    text: str,
    *,
    catalog: Catalog | None = None,
    indexes: Sequence[IndexDefinition] | None = None,
) -> PlannedQuery:
    """Parse and plan one query against the shared catalog."""
    return build_plan(
        parse(text),
        catalog=catalog if catalog is not None else build_catalog(),
        indexes=build_indexes() if indexes is None else indexes,
    )


def operators(root: PlanNode) -> tuple[str, ...]:
    """Return the operator names of a plan, parents before children."""
    return tuple(node.label for node in root.walk())


def find_operator(root: PlanNode, label: str) -> PlanNode:
    """Return the single operator of that name in a plan, refusing zero or several."""
    found = [node for node in root.walk() if node.label == label]
    assert len(found) == 1, f"expected exactly one {label}, found {len(found)}"
    return found[0]
