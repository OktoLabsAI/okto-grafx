"""Two embedding spaces are never comparable (SPEC-VEC FR-2, BR-1, AC-2, VTS-2).

A number computed across two spaces is not imprecise, it is meaningless: the coordinates come
from different geometries and nothing about the result says so. The rule is therefore an error
and not a warning, and it fires BEFORE any distance is computed, so no partial ranking exists to
be handed back by mistake.

Three doors can reach a cross-space comparison and all three are tested here: a query that
carries the identity of another space, a candidate whose stored row belongs to another space,
and a staged index record built for another space.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import (
    GrafxEmbeddingSpaceMismatch,
    GrafxIndexError,
    GrafxVectorValidationError,
)
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.domain.vector.space import require_space_identity

from .conftest import SnapshotDouble, TransactionDouble, VectorFixture


class CountingMath:
    """A VectorMath that records every distance it is asked for, so a claim about ORDER holds.

    BR-1 says the refusal happens before any distance is computed. Asserting that needs a way to
    see the distances, because an implementation that refused after scoring the first candidate
    would raise exactly the same error at exactly the same call.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        """Return the label of the adapter this one wraps."""
        return self._inner.name  # type: ignore[attr-defined]

    def dot(self, a: object, b: object) -> float:
        """Record and forward."""
        self.calls.append("dot")
        return self._inner.dot(a, b)  # type: ignore[attr-defined]

    def cosine(self, a: object, b: object) -> float:
        """Record and forward."""
        self.calls.append("cosine")
        return self._inner.cosine(a, b)  # type: ignore[attr-defined]

    def euclidean(self, a: object, b: object) -> float:
        """Record and forward."""
        self.calls.append("euclidean")
        return self._inner.euclidean(a, b)  # type: ignore[attr-defined]

    def norm(self, a: object) -> float:
        """Record and forward."""
        self.calls.append("norm")
        return self._inner.norm(a)  # type: ignore[attr-defined]

    def normalize(self, a: object) -> tuple[float, ...]:
        """Record and forward."""
        self.calls.append("normalize")
        return self._inner.normalize(a)  # type: ignore[attr-defined]

    def score(self, a: object, b: object, metric: object) -> float:
        """Record and forward."""
        self.calls.append("score")
        return self._inner.score(a, b, metric)  # type: ignore[attr-defined]

    def top_k(self, query: object, candidates: object, k: int, metric: object) -> list:
        """Record and forward."""
        self.calls.append("top_k")
        return self._inner.top_k(query, candidates, k, metric)  # type: ignore[attr-defined]


def test_a_query_that_declares_another_space_is_refused(database: VectorFixture) -> None:
    """A query carrying its own identity is checked against the space being searched."""
    first = database.create_space("alpha", 3)
    second = database.create_space("beta", 3)
    table = database.create_table("Chunk", "alpha")
    database.insert_row(table, 1, 0, first, (1.0, 0.0, 0.0), csn=10)
    foreign = VectorValue((1.0, 0.0, 0.0), second.space_id, second.storage_dtype)
    with pytest.raises(GrafxEmbeddingSpaceMismatch) as failure:
        database.engine.search(
            space="alpha", query=foreign, k=3, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.code == "embedding_space_mismatch"
    assert failure.value.details["expected"] == first.space_id
    assert failure.value.details["value"] == second.space_id
    assert failure.value.details["origin"] == "query vector"


def test_a_matching_query_identity_is_accepted(database: VectorFixture) -> None:
    """The check refuses a mismatch and nothing else; the same query in its own space works."""
    space = database.create_space("alpha", 3)
    table = database.create_table("Chunk", "alpha")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    own = VectorValue((1.0, 0.0, 0.0), space.space_id, space.storage_dtype)
    result = database.engine.search(
        space="alpha", query=own, k=3, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k == 1


def test_no_distance_is_computed_before_a_query_mismatch_is_refused(
    metrics: object, clock: object
) -> None:
    """AC-2: the refusal precedes every distance, so no partial ranking can exist."""
    counting = CountingMath(PureVectorMath())
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=4096, math=counting
    )
    first = database.create_space("alpha", 3)
    second = database.create_space("beta", 3)
    table = database.create_table("Chunk", "alpha")
    database.insert_row(table, 1, 0, first, (1.0, 0.0, 0.0), csn=10)
    counting.calls.clear()
    with pytest.raises(GrafxEmbeddingSpaceMismatch):
        database.engine.search(
            space="alpha",
            query=VectorValue((1.0, 0.0, 0.0), second.space_id, second.storage_dtype),
            k=3,
            snapshot=SnapshotDouble(1000),
        )
    assert counting.calls == []


def test_a_stored_row_of_another_space_is_refused_before_any_distance(
    metrics: object, clock: object
) -> None:
    """AC-2 in its own words: the property holds a vector of space B and the query declares A."""
    counting = CountingMath(PureVectorMath())
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=4096, math=counting
    )
    first = database.create_space("alpha", 3)
    second = database.create_space("beta", 3)
    table = database.create_table("Chunk", "alpha")
    good = database.insert_row(table, 1, 0, first, (1.0, 0.0, 0.0), csn=10)
    assert good is not None
    foreign_ref = database.heap.insert(
        table,
        2,
        (2, 0, VectorValue((0.0, 1.0, 0.0), second.space_id, second.storage_dtype)),
        11,
    )
    txn = TransactionDouble()
    database.engine.stage_insert("alpha", 2, foreign_ref, (0.0, 1.0, 0.0), 11, txn)
    database.engine.commit("alpha", txn, 11)
    counting.calls.clear()
    with pytest.raises(GrafxEmbeddingSpaceMismatch) as failure:
        database.engine.search(
            space="alpha", query=(1.0, 0.0, 0.0), k=3, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["origin"] == "stored vector"
    assert counting.calls == []


def test_a_staged_record_for_another_space_is_refused_by_the_index(
    database: VectorFixture,
) -> None:
    """An index covers one space, so a record naming another one never enters it."""
    from okto_grafx.domain.model.schema import ColumnDef, TableDef
    from okto_grafx.domain.model.value import ValueType

    first = database.create_space("alpha", 3)
    second = database.create_space("beta", 3)
    table = TableDef(
        table_id=database.catalog_store.catalog.next_table_id(),
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="layer", type=ValueType.INT64),
            ColumnDef(name="a", type=first.value_type, vector_space="alpha"),
            ColumnDef(name="b", type=second.value_type, vector_space="beta"),
        ),
        primary_key="id",
    )
    database.catalog_store.catalog.add_table(table)
    database.catalog_store.save()
    database.attach(table, "alpha")
    database.attach(table, "beta")
    ref = database.heap.insert(
        table,
        1,
        (
            1,
            0,
            _stored(database, first, (1.0, 0.0, 0.0)),
            _stored(database, second, (1.0, 0.0, 0.0)),
        ),
        10,
    )
    txn = TransactionDouble()
    foreign = database.engine.index("beta").stage_insert(
        txn, database.engine.index("beta").key_for((1.0, 0.0, 0.0)), ref, 12
    )
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.index("alpha").apply(foreign)
    assert failure.value.details["field"] == "index"
    assert first.space_id != second.space_id


def test_a_query_of_the_wrong_dimension_is_a_validation_error_not_a_space_mismatch(
    database: VectorFixture,
) -> None:
    """Two different rules, two different errors: a length is not an identity."""
    database.create_space("alpha", 3)
    table = database.create_table("Chunk", "alpha")
    database.insert_row(table, 1, 0, database.engine.space("alpha"), (1.0, 0.0, 0.0), csn=10)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.search(
            space="alpha", query=(1.0, 0.0), k=3, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["reason"] == "dimension_mismatch"


def test_the_identity_check_precedes_the_dimension_check(database: VectorFixture) -> None:
    """A query that is wrong on both counts is refused as a mismatch, which is the graver one."""
    database.create_space("alpha", 3)
    second = database.create_space("beta", 5)
    with pytest.raises(GrafxEmbeddingSpaceMismatch):
        database.engine.search(
            space="alpha",
            query=VectorValue((1.0,) * 5, second.space_id, second.storage_dtype),
            k=3,
            snapshot=SnapshotDouble(1000),
        )


def test_a_non_finite_query_is_refused_before_any_search_happens(
    database: VectorFixture,
) -> None:
    """A NaN query would poison every score; the query door refuses it like the write door."""
    space = database.create_space("alpha", 3)
    table = database.create_table("Chunk", "alpha")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.search(
            space="alpha",
            query=(1.0, float("nan"), 0.0),
            k=3,
            snapshot=SnapshotDouble(1000),
        )
    assert failure.value.details["reason"] == "non_finite"
    assert failure.value.details["position"] == 1


def test_the_identity_rule_names_where_the_foreign_vector_came_from() -> None:
    """The origin detail is what lets an operator tell a bad query from a bad row."""
    from okto_grafx.domain.model.schema import EmbeddingSpaceDef
    from okto_grafx.domain.ports.vectormath import DistanceMetric

    space = EmbeddingSpaceDef(
        space_id=7,
        name="alpha",
        dimension=3,
        metric=DistanceMetric.COSINE,
        normalized=False,
    )
    require_space_identity(space, 7, origin="query vector")
    with pytest.raises(GrafxEmbeddingSpaceMismatch) as failure:
        require_space_identity(space, 8, origin="stored vector")
    assert failure.value.details["origin"] == "stored vector"
    assert failure.value.details["space"] == "alpha"


def _stored(database: VectorFixture, space, values):
    """Return the storable vector value of these components in one space."""
    from okto_grafx.domain.model.value import VectorValue

    return VectorValue(
        database.engine.validate_vector(space, values), space.space_id, space.storage_dtype
    )
