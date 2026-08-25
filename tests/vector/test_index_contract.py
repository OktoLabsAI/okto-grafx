"""The vector index against the frozen secondary-index contract (CONTRACT.md section 8.7).

The index framework of C7 owns the contract, the store, the entry format and the registry. This
module asserts that the vector index really is one of its indexes rather than something shaped
like one: it satisfies the protocol, it registers, it creates a file, it declares a definition,
and every member the registry demands answers.

That list is the point. Registration was deliberately refused for a bare protocol object, because
an index that registered without ``commit`` would raise ``AttributeError`` out of a public door
the first time a transaction ended. Passing these tests is what says the seam is real.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxIndexError
from okto_grafx.domain.index.contract import SecondaryIndex, StagingTransaction
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.visibility import IndexVisibility, SnapshotLike
from okto_grafx.engine.index_manager import IndexStore, ProximityIndex
from okto_grafx.engine.vector_engine import VectorHnswIndex

from .conftest import SnapshotDouble, TransactionDouble, VectorFixture, seeded_vectors

REGISTRY_SURFACE: tuple[str, ...] = (
    "definition",
    "create",
    "check_freshness",
    "commit",
    "rollback",
    "name",
    "visibility",
    "apply",
    "stage_insert",
    "stage_delete",
    "lookup",
    "reconcile",
    "walk",
)
"""Every member ``IndexManager.register`` and CONTRACT 8.7 between them demand."""


def _index(database: VectorFixture) -> VectorHnswIndex:
    """Return a registered index over a small space, with a few versions already in it."""
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    for index, values in enumerate(seeded_vectors(5, 4, seed=0x77)):
        database.insert_row(table, index + 1, 0, space, values, csn=10 + index)
    return database.engine.index("space")


def _cold_index_bytes(database: VectorFixture, index: VectorHnswIndex) -> bytes:
    """Read one index straight from the device, bypassing the pool that owns it."""
    return b"".join(
        database.device.read_page(index.file, page_index)
        for page_index in range(database.device.page_count(index.file))
    )


def test_the_vector_index_is_built_on_the_frameworks_store(
    database: VectorFixture,
) -> None:
    """Not shaped like a proximity index: it IS one, by inheritance."""
    index = _index(database)
    assert isinstance(index, ProximityIndex)
    assert isinstance(index, IndexStore)
    assert isinstance(index, SecondaryIndex)


def test_registry_poisoning_can_exclude_a_vector_index_without_persisting(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read-only redo poisons every accelerator without creating another write.

    ``IndexManager.mark_all_stale`` calls the concrete indexes with ``persist=False``.  The
    vector override must preserve that keyword: losing it either raises ``TypeError`` before the
    vector is excluded or takes the base method's persistent default and writes the stale bit.
    A cold device reading proves the latter did not happen, while a published graph makes its
    invalidation independently observable.
    """
    index = _index(database)
    published = index.snapshot()
    assert index._snapshot is published  # noqa: SLF001 - proves a graph exists to invalidate
    before = _cold_index_bytes(database, index)

    def forbidden_flush(_pool: object, _file: str | None = None) -> int:
        raise AssertionError("persist=False reached the buffer-pool write path")

    monkeypatch.setattr(type(database.pool), "flush", forbidden_flush)

    database.registry.mark_all_stale("committed redo did not complete", persist=False)

    assert _cold_index_bytes(database, index) == before
    assert index.stale
    assert index.stale_reason == "committed redo did not complete"
    assert index._snapshot is None  # noqa: SLF001 - the graph invalidation is the contract here
    with pytest.raises(GrafxIndexError) as refused:
        index.search((1.0, 0.0, 0.0, 0.0), 1, SnapshotDouble(1_000))
    assert refused.value.details["field"] == "stale"


def test_the_index_answers_every_member_the_registry_demands(
    database: VectorFixture,
) -> None:
    """A missing member would surface as an AttributeError out of a public door."""
    index = _index(database)
    for member in REGISTRY_SURFACE:
        assert hasattr(index, member), member


def test_the_index_declares_a_definition_naming_its_table_and_column(
    database: VectorFixture,
) -> None:
    """TR-2: one index per property and space, and the property is a column of a table."""
    index = _index(database)
    definition = index.definition
    assert isinstance(definition, IndexDefinition)
    assert definition.visibility is IndexVisibility.PROXIMITY
    assert definition.table_name == "Chunk"
    assert definition.positions == (2,)
    assert index.name == "vector_Chunk_space"


def test_two_spaces_over_one_table_are_two_indexes(database: VectorFixture) -> None:
    """Coexisting spaces each get their own index, which is what FR-3 means by segregated."""
    from okto_grafx.domain.model.schema import ColumnDef, TableDef
    from okto_grafx.domain.model.value import ValueType

    first = database.create_space("minilm", 4)
    second = database.create_space("bge", 4)
    table = TableDef(
        table_id=database.catalog_store.catalog.next_table_id(),
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="a", type=first.value_type, vector_space="minilm"),
            ColumnDef(name="b", type=second.value_type, vector_space="bge"),
        ),
        primary_key="id",
    )
    database.catalog_store.catalog.add_table(table)
    database.catalog_store.save()
    left = database.engine.attach(table, "minilm")
    right = database.engine.attach(table, "bge")
    assert left is not right
    assert left.name != right.name
    assert {left.name, right.name} <= {index.name for index in database.registry.indexes()}


def test_the_walk_yields_entries_of_the_framework_shape(database: VectorFixture) -> None:
    """A verifier written against any secondary index must be able to walk this one."""
    index = _index(database)
    walked = index.walk()
    assert walked
    assert all(isinstance(entry, IndexEntry) for entry in walked)
    assert all(entry.versioned and entry.born_csn != 0 for entry in walked)


def test_a_lookup_returns_the_visible_locations_of_one_record(
    database: VectorFixture,
) -> None:
    """The keyed door of the contract, answered under the proximity rule."""
    index = _index(database)
    key = index.key_for(seeded_vectors(5, 4, seed=0x77)[1])
    assert len(index.lookup(key, SnapshotDouble(1000))) == 1
    assert index.lookup(key, SnapshotDouble(5)) == ()


def test_the_registry_verifies_the_vector_index_with_every_other_one(
    database: VectorFixture,
) -> None:
    """FR-6 asks for index-heap divergence detection; the framework's walk provides it."""
    _index(database)
    assert database.registry.verify() == ()


def test_a_divergence_is_reported_by_the_registry(database: VectorFixture) -> None:
    """A row the index never received is an omission, which is a wrong answer under either rule."""
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    database.insert_row(
        table, 2, 0, space, (0.0, 1.0, 0.0, 0.0), csn=11, apply_index=False
    )
    findings = database.registry.verify()
    assert findings
    assert any("omission" in finding.kind or "missing" in finding.kind for finding in findings)


def test_the_transaction_protocol_is_the_frameworks_own(database: VectorFixture) -> None:
    """The staging door is C7's, so the double a test uses must satisfy C7's protocol."""
    assert isinstance(TransactionDouble(), StagingTransaction)
    assert isinstance(SnapshotDouble(5), SnapshotLike)


def test_staging_needs_a_transaction(database: VectorFixture) -> None:
    """Every index write travels inside a transaction, so there is no door without one."""
    index = _index(database)
    with pytest.raises(GrafxIndexError):
        index.stage_insert(None, index.walk()[0].key, index.walk()[0].ref, 50)  # type: ignore[arg-type]


def test_the_engine_refuses_to_build_an_index_without_a_registry(
    metrics: object, clock: object
) -> None:
    """Fail-closed: the alternative is the memory-resident second implementation, not a fallback."""
    from okto_grafx.adapters.codec_v1 import PageCodecV1
    from okto_grafx.adapters.storage_memory import MemoryStorageDevice
    from okto_grafx.adapters.vectormath_pure import PureVectorMath
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.vector_engine import VectorEngine

    device = MemoryStorageDevice(page_size=4096)
    pool = BufferPool(
        device, PageCodecV1(4096), metrics, budget_bytes=64 * 4096, db_label="bare"
    )
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    engine = VectorEngine(
        catalog=catalog,
        heap=heap,
        math=PureVectorMath(),
        metrics=metrics,
        clock=clock,
    )
    from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
    from okto_grafx.domain.model.value import ValueType
    from okto_grafx.domain.ports.vectormath import DistanceMetric

    space = EmbeddingSpaceDef(
        space_id=1,
        name="space",
        dimension=2,
        metric=DistanceMetric.COSINE,
        normalized=False,
    )
    engine.create_space(space)
    table = TableDef(
        table_id=1,
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="v", type=ValueType.VECTOR_F32, vector_space="space"),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()
    with pytest.raises(GrafxConfigurationError) as failure:
        engine.attach(table, "space")
    assert failure.value.details["field"] == "indexes"


def test_searching_a_space_with_no_index_is_refused(database: VectorFixture) -> None:
    """An index is created when a table declares the column, so a bare space cannot be searched."""
    database.create_space("orphan", 3)
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.search(
            space="orphan", query=(1.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["field"] == "space"


def test_the_beam_is_never_narrower_than_the_requested_neighbour_count(
    database: VectorFixture,
) -> None:
    """A beam narrower than k would silently cap the answer below what was asked for.

    Restored after the battery reported the guard surviving: this test existed and was lost when
    the suite was rewritten onto the index framework (amendment A82).
    """
    index = _index(database)
    scored, _stats = index.search((1.0, 0.0, 0.0, 0.0), 5, SnapshotDouble(1000), ef=1)
    assert len(scored) == 5


def test_a_row_whose_vector_has_the_wrong_dimension_is_refused_by_name(
    database: VectorFixture,
) -> None:
    """The graph refuses a row it cannot rank, naming the index and the location.

    The arithmetic would refuse this too, one layer down, as a length mismatch between two
    vectors -- but that error names neither the index nor the page, so an operator reading it
    could not tell which entry to look at. The guard therefore carries a ``field`` and an
    ``index`` that only it produces (amendment A62).
    """
    from okto_grafx.domain.model.value import VectorValue

    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0, 0.0), csn=10)
    index = database.engine.index("space")
    index.graph()
    short = VectorValue((1.0,), space.space_id, space.storage_dtype)
    ref = database.heap.insert(table, 2, (2, 0, short), 11)
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for((1.0,)), ref, 11)
    with pytest.raises(GrafxIndexError) as failure:
        index.commit(txn, 11)
    assert failure.value.details["field"] == "dimension"
    assert failure.value.details["index"] == index.name
    assert failure.value.details["expected"] == 4
    assert failure.value.details["value"] == 1
