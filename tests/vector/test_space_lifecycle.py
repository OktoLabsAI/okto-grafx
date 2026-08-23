"""Spaces coexist, and retirement is observable (SPEC-VEC FR-3, BR-4, AC-3, AC-4, VTS-3, VTS-4).

An embedding model upgrade is a migration the caller drives, not a rebuild the engine performs.
Two spaces therefore live side by side over one property, each with its own index and its own
identity; a search names the space it wants and sees nothing else; and retiring the old space
closes its write door while leaving every vector in it readable, with ``retired`` on every hit
and a coverage gauge that falls as the caller moves rows across.

The engine never generates an embedding (BR-4, D6). That is asserted the only way a negative can
be: the surface is enumerated and nothing on it produces vector components from anything but the
caller's own argument.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxSpaceRetired,
)
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType, VectorValue
from okto_grafx.domain.ports.vectormath import DistanceMetric

from .conftest import (
    RecordingEvents,
    RecordingMetrics,
    SnapshotDouble,
    TransactionDouble,
    VectorFixture,
)


def _two_space_table(database: VectorFixture) -> TableDef:
    """Return a table whose two vector columns belong to two coexisting spaces."""
    first = database.catalog_store.catalog.space("minilm")
    second = database.catalog_store.catalog.space("bge")
    table = TableDef(
        table_id=database.catalog_store.catalog.next_table_id(),
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="layer", type=ValueType.INT64),
            ColumnDef(
                name="embedding", type=first.value_type, vector_space="minilm"
            ),
            ColumnDef(name="upgraded", type=second.value_type, vector_space="bge"),
        ),
        primary_key="id",
    )
    database.catalog_store.catalog.add_table(table)
    database.catalog_store.save()
    database.engine.attach(table, 'minilm')
    database.engine.attach(table, 'bge')
    return table


def test_two_spaces_coexist_over_one_property_with_separate_indexes(
    database: VectorFixture,
) -> None:
    """VTS-3: each space has its own index and its own identity, at the same time."""
    first = database.create_space("minilm", 4)
    second = database.create_space("bge", 4)
    _two_space_table(database)
    assert first.space_id != second.space_id
    assert database.engine.index("minilm") is not database.engine.index("bge")
    assert database.engine.index("minilm").space_id == first.space_id
    assert database.engine.index("bge").space_id == second.space_id
    assert [space.name for space in database.engine.spaces()] == ["minilm", "bge"]


def test_a_search_of_one_space_never_sees_the_vectors_of_the_other(
    database: VectorFixture,
) -> None:
    """AC-3: a query names its target space and the other one is not in reach at all."""
    first = database.create_space("minilm", 4)
    second = database.create_space("bge", 4)
    table = _two_space_table(database)
    snapshot = SnapshotDouble(1000)
    for index in range(6):
        values = tuple(float(index + position) for position in range(4))
        ref = database.heap.insert(
            table,
            index + 1,
            (
                index + 1,
                0,
                VectorValue(values, first.space_id, first.storage_dtype),
                VectorValue(values, second.space_id, second.storage_dtype),
            ),
            10 + index,
        )
        target = "minilm" if index < 4 else "bge"
        space = first if index < 4 else second
        txn = TransactionDouble()
        database.engine.stage_insert(
            target,
            index + 1,
            ref,
            database.engine.validate_vector(space, values),
            10 + index,
            txn,
        )
        database.engine.commit(target, txn, 10 + index)
    minilm = database.engine.search(
        space="minilm", query=(0.0, 1.0, 2.0, 3.0), k=10, snapshot=snapshot
    )
    bge = database.engine.search(
        space="bge", query=(0.0, 1.0, 2.0, 3.0), k=10, snapshot=snapshot
    )
    assert sorted(hit.record_id for hit in minilm.hits) == [1, 2, 3, 4]
    assert sorted(hit.record_id for hit in bge.hits) == [5, 6]


def test_the_coverage_of_the_spaces_sums_to_the_whole(
    database: VectorFixture, metrics: RecordingMetrics
) -> None:
    """AC-3: coverage is a share of every indexed vector, so the shares sum to one."""
    first = database.create_space("minilm", 4)
    second = database.create_space("bge", 4)
    table = _two_space_table(database)
    for index in range(10):
        values = tuple(float(index + position) for position in range(4))
        ref = database.heap.insert(
            table,
            index + 1,
            (
                index + 1,
                0,
                VectorValue(values, first.space_id, first.storage_dtype),
                VectorValue(values, second.space_id, second.storage_dtype),
            ),
            10 + index,
        )
        target, space = ("minilm", first) if index < 6 else ("bge", second)
        assert space is not None
        txn = TransactionDouble()
        database.engine.stage_insert(target, index + 1, ref, values, 10 + index, txn)
        database.engine.commit(target, txn, 10 + index)
    coverage = database.engine.coverage()
    assert coverage == {"minilm": 0.6, "bge": 0.4}
    assert sum(coverage.values()) == pytest.approx(1.0)
    published = metrics.snapshot()
    assert published["oktografx_vector_space_coverage_ratio{space=minilm}"] == 0.6
    assert published["oktografx_vector_space_coverage_ratio{space=bge}"] == 0.4
    assert published["oktografx_vector_index_entries{space=minilm}"] == 6.0
    assert published["oktografx_vector_index_entries{space=bge}"] == 4.0


def test_coverage_of_an_empty_engine_is_zero_and_not_a_division_by_zero(
    database: VectorFixture,
) -> None:
    """A space with an index and no vectors has no share, and asking is not an error."""
    space = database.create_space("empty", 3)
    database.create_table("Chunk", "empty")
    assert space.name == "empty"
    assert database.engine.coverage() == {"empty": 0.0}


def test_retiring_a_space_closes_the_write_door_and_leaves_the_read_door_open(
    database: VectorFixture, metrics: RecordingMetrics
) -> None:
    """VTS-4 and AC-4: writes fail, searches work, and every hit says the space is retired."""
    space = database.create_space("legacy", 3)
    table = database.create_table("Chunk", "legacy")
    last_ref = None
    for index in range(4):
        last_ref = database.insert_row(
            table, index + 1, 0, space, (float(index), 1.0, 0.0), csn=10 + index
        )
    assert last_ref is not None
    database.engine.retire_space("legacy")
    with pytest.raises(GrafxSpaceRetired):
        database.engine.stage_insert(
            "legacy", 99, last_ref, (0.0, 1.0, 0.0), 50, TransactionDouble()
        )
    result = database.engine.search(
        space="legacy", query=(0.0, 1.0, 0.0), k=4, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k == 4
    assert all(hit.retired for hit in result.hits)
    assert metrics.snapshot()["oktografx_vector_space_retired_total"] == 1.0


def test_a_hit_of_an_active_space_is_not_marked_retired(database: VectorFixture) -> None:
    """The flag is a property of the space, so an active one must never set it."""
    space = database.create_space("current", 3)
    table = database.create_table("Chunk", "current")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    result = database.engine.search(
        space="current", query=(1.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert result.hits[0].retired is False


def test_the_residual_coverage_of_a_retired_space_falls_as_the_caller_migrates(
    database: VectorFixture,
) -> None:
    """AC-4: the migration is the caller's, and its progress is visible as a falling share."""
    old = database.create_space("minilm", 4)
    new = database.create_space("bge", 4)
    table = _two_space_table(database)
    rows: list[tuple[int, object, tuple[float, ...]]] = []
    for index in range(10):
        values = tuple(float(index + position) for position in range(4))
        ref = database.heap.insert(
            table,
            index + 1,
            (
                index + 1,
                0,
                VectorValue(values, old.space_id, old.storage_dtype),
                VectorValue(values, new.space_id, new.storage_dtype),
            ),
            10 + index,
        )
        txn = TransactionDouble()
        database.engine.stage_insert("minilm", index + 1, ref, values, 10 + index, txn)
        database.engine.commit("minilm", txn, 10 + index)
        rows.append((index + 1, ref, values))
    database.engine.retire_space("minilm")
    assert database.engine.coverage()["minilm"] == 1.0
    shares: list[float] = []
    for position, (record_id, ref, values) in enumerate(rows):
        txn = TransactionDouble()
        database.engine.stage_delete(
            "minilm", record_id, ref, values, 100 + position, txn
        )
        database.engine.commit("minilm", txn, 100 + position)
        second_txn = TransactionDouble()
        database.engine.stage_insert(
            "bge", record_id, ref, values, 100 + position, second_txn
        )
        database.engine.commit("bge", second_txn, 100 + position)
        shares.append(database.engine.coverage()["minilm"])
    assert shares == sorted(shares, reverse=True)
    assert shares[-1] == 0.0
    assert database.engine.coverage()["bge"] == 1.0


def test_a_retired_space_still_accepts_a_delete_so_a_migration_can_finish(
    database: VectorFixture,
) -> None:
    """Retirement closes the door on NEW vectors; closing it on removals would trap the caller."""
    space = database.create_space("legacy", 3)
    table = database.create_table("Chunk", "legacy")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    database.engine.retire_space("legacy")
    txn = TransactionDouble()
    database.engine.stage_delete("legacy", 1, ref, (1.0, 0.0, 0.0), 20, txn)
    database.engine.commit("legacy", txn, 20)
    assert database.engine.index("legacy").live_count() == 0


def test_retiring_a_space_twice_is_refused(database: VectorFixture) -> None:
    """One way, once: a second retirement is a caller mistake with a typed answer."""
    database.create_space("legacy", 3)
    database.engine.retire_space("legacy")
    with pytest.raises(GrafxSpaceRetired):
        database.engine.retire_space("legacy")


def test_retiring_a_space_that_does_not_exist_is_refused(database: VectorFixture) -> None:
    """A name the catalog does not know is a configuration error, never a silent no-op."""
    with pytest.raises(GrafxConfigurationError):
        database.engine.retire_space("never_existed")


def test_creating_a_space_persists_it_in_the_catalog(database: VectorFixture) -> None:
    """TR-2: the space registry lives in the catalog, so a reopened database still has it."""
    definition = database.create_space("durable", 5, metric=DistanceMetric.EUCLIDEAN)
    reloaded = database.catalog_store.read_from_pages()
    stored = reloaded.space("durable")
    assert stored == definition
    assert stored.metric is DistanceMetric.EUCLIDEAN
    assert stored.state == "active"


def test_retiring_a_space_persists_the_new_state(database: VectorFixture) -> None:
    """The state change is durable too, or a reopen would resurrect a closed write door."""
    database.create_space("durable", 5)
    database.engine.retire_space("durable")
    assert database.catalog_store.read_from_pages().space("durable").state == "retired"


def test_a_space_with_no_table_attached_has_no_index_yet(
    database: VectorFixture,
) -> None:
    """An index covers a (table, space) pair, so a bare space has none until one attaches.

    The refusal is what makes the ordering visible. A space is declared before any table
    declares a column in it, and an index that appeared at space creation would have no table
    to name in its definition and no column position to key on.
    """
    definition = database.create_space("cold", 3)
    with pytest.raises(GrafxIndexError):
        database.engine.index("cold")
    table = database.create_table("Chunk", "cold")
    index = database.engine.index("cold")
    assert index.space_id == definition.space_id
    assert index.definition.table_name == table.name
    assert index.walk() == ()


def test_creating_a_space_that_is_not_a_definition_is_refused(
    database: VectorFixture,
) -> None:
    """A caller argument of the wrong type is a configuration error at the door."""
    with pytest.raises(GrafxConfigurationError) as failure:
        database.engine.create_space("minilm")  # type: ignore[arg-type]
    assert failure.value.details["field"] == "definition"


def test_creating_a_space_that_is_born_retired_is_refused(database: VectorFixture) -> None:
    """Retiring is a separate act, so a space cannot be created already closed."""
    definition = EmbeddingSpaceDef(
        space_id=1,
        name="stillborn",
        dimension=3,
        metric=DistanceMetric.COSINE,
        normalized=False,
        state="retired",
    )
    with pytest.raises(GrafxConfigurationError):
        database.engine.create_space(definition)


def test_the_lifecycle_notices_name_the_space(
    database: VectorFixture, events: RecordingEvents
) -> None:
    """The host hears about a creation and a retirement, with the space named in the payload."""
    database.create_space("watched", 3)
    database.engine.retire_space("watched")
    assert events.names() == ["vector.space_created", "vector.space_retired"]
    assert events.events[0][1]["space"] == "watched"
    assert events.events[0][1]["dimension"] == 3
    assert events.events[1][1]["space"] == "watched"


def test_the_engine_exposes_nothing_that_could_generate_an_embedding(
    database: VectorFixture,
) -> None:
    """BR-4 and D6: every door takes components from the caller and none produces any.

    The assertion is an enumeration of the public surface, because "the engine never generates
    an embedding" is a claim about what does NOT exist. A door added later that synthesises
    components would have to appear in this list first.
    """
    public = sorted(
        name for name in dir(database.engine) if not name.startswith("_")
    )
    assert public == [
        "attach",
        "commit",
        "coverage",
        "create_space",
        # Forgets per-space state for ONE space, by name -- the undo of a refused schema
        # STATEMENT is the caller, and by-name is the point: pruning by catalog would also drop
        # another open transaction's attachments. Produces nothing, generates nothing (BR-4).
        "detach",
        # Takes a catalog and drops per-space state for spaces it does not know -- the rollback
        # of a schema transaction is the caller. Produces nothing, generates nothing (BR-4).
        "discard_unknown",
        "exact_scan_threshold",
        "index",
        "indexes",
        "reconcile",
        "retire_space",
        "rollback",
        "search",
        "space",
        "spaces",
        "stage_delete",
        "stage_insert",
        "validate_vector",
    ]
