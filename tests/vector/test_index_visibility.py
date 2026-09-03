"""The PROXIMITY contract of the vector index (SPEC-VEC FR-6, FR-9, BR-3, VTS-8).

The vector index is a ``ProximityIndex``, so the rules tested here are the framework's rules seen
through the graph that sits on top of them: a versioned entry, a tombstone that keeps an older
snapshot answering, a horizon-bounded reconciliation whose every removal is a log record, and --
the one this component used not to have -- a durable staleness flag that makes the index REFUSE
rather than answer short.

That last one is the sharpest rule in the module. A proximity index answers from its own entries
without consulting the heap, so an index behind the heap returns a plausible, confidently ordered
top-k that silently omits rows. An omission looks exactly like an empty neighbourhood, which is
why the refusal has to be the index's own and not a caller's judgement.
"""

from __future__ import annotations

import threading

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
)
from okto_grafx.domain.index.records import IndexOperation, change_of
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.value import VectorValue

from .conftest import (
    RecordingMetrics,
    SnapshotDouble,
    StepClock,
    TransactionDouble,
    VectorFixture,
)


def _populate(database: VectorFixture, count: int = 6, *, dimension: int = 4):
    """Return the space, the table and the refs of a small indexed corpus."""
    space = database.create_space("space", dimension)
    table = database.create_table("Chunk", "space")
    refs = []
    for index in range(count):
        values = tuple(float(index + position) for position in range(dimension))
        refs.append(
            database.insert_row(table, index + 1, index % 2, space, values, csn=10 + index)
        )
    return space, table, refs


# --- the contract the index declares ---------------------------------------------------------


def test_the_vector_index_declares_the_frameworks_proximity_contract(
    database: VectorFixture,
) -> None:
    """The enumeration belongs to the index framework and this index reports its member."""
    _populate(database, 1)
    assert database.engine.index("space").visibility is IndexVisibility.PROXIMITY


def test_the_index_is_registered_with_the_index_manager(database: VectorFixture) -> None:
    """A vector index is one of the database's indexes, not a structure beside them."""
    _populate(database, 1)
    index = database.engine.index("space")
    assert database.registry.index(index.name) is index
    assert index in database.registry.indexes()


def test_the_index_owns_a_paged_file_of_its_own(database: VectorFixture) -> None:
    """Durability is the point of the adoption, so the file is asserted to exist."""
    _populate(database, 2)
    index = database.engine.index("space")
    assert index.exists()
    assert index.file.startswith("index/")
    assert database.device.exists(index.file)


# --- visibility -------------------------------------------------------------------------------


def test_an_entry_is_invisible_to_a_snapshot_that_opened_before_it_was_written(
    database: VectorFixture,
) -> None:
    """FR-9: no version a snapshot cannot see appears in a result, whatever the regime."""
    _populate(database, 6)
    result = database.engine.search(
        space="space", query=(0.0, 1.0, 2.0, 3.0), k=10, snapshot=SnapshotDouble(12)
    )
    assert sorted(hit.record_id for hit in result.hits) == [1, 2, 3]


def test_a_tombstoned_entry_leaves_the_result_at_the_commit_that_ended_it(
    database: VectorFixture,
) -> None:
    """A snapshot at or after the delete cannot see the version; one before it still can."""
    space, table, refs = _populate(database, 4)
    values = (2.0, 3.0, 4.0, 5.0)
    database.delete_row(table, refs[2], 3, space, values, csn=50)
    before = database.engine.search(
        space="space", query=values, k=10, snapshot=SnapshotDouble(49)
    )
    after = database.engine.search(
        space="space", query=values, k=10, snapshot=SnapshotDouble(50)
    )
    assert 3 in {hit.record_id for hit in before.hits}
    assert 3 not in {hit.record_id for hit in after.hits}


def test_a_tombstoned_entry_stays_in_the_index_as_a_bridge(database: VectorFixture) -> None:
    """The version leaves the results and stays traversable, which is what keeps the graph whole."""
    space, table, refs = _populate(database, 6)
    index = database.engine.index("space")
    before = len(index.walk())
    database.delete_row(table, refs[3], 4, space, (3.0, 4.0, 5.0, 6.0), csn=50)
    assert len(index.walk()) == before
    assert index.live_count() == before - 1


def test_a_key_mismatched_tombstone_keeps_the_warm_count_without_guessing(
    database: VectorFixture,
) -> None:
    """A missing durable target is a no-op, not permission to decrement by ref."""
    space, _table, refs = _populate(database, 1)
    index = database.engine.index("space")
    assert len(index.graph()) == 1
    txn = TransactionDouble()
    index.stage_delete(txn, index.key_for((90.0, 91.0, 92.0, 93.0)), refs[0], 80)
    database.engine.commit("space", txn, 80)

    assert index.missing_targets == 1
    assert index._snapshot is not None  # noqa: SLF001 - no-op kept the warm picture
    assert index.live_count() == 1


def test_an_insert_with_the_same_ref_and_another_key_is_a_distinct_warm_entry(
    database: VectorFixture,
) -> None:
    """An entry identity is ``(key, ref)``; ref alone cannot decide a delta."""
    space, _table, refs = _populate(database, 1)
    index = database.engine.index("space")
    assert len(index.graph()) == 1
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for((90.0, 91.0, 92.0, 93.0)), refs[0], 80)
    database.engine.commit("space", txn, 80)

    assert index._snapshot is not None  # noqa: SLF001 - second entry patched by full identity
    assert index.live_count() == 2
    assert len(index.graph()) == 2


def test_the_visible_count_is_what_the_snapshot_can_see(database: VectorFixture) -> None:
    """The size of a space is a snapshot question, because an old reader lives in a smaller one."""
    _populate(database, 6)
    index = database.engine.index("space")
    assert index.visible_count(SnapshotDouble(1000)) == 6
    assert index.visible_count(SnapshotDouble(11)) == 2
    assert index.visible_count(SnapshotDouble(0)) == 0


# --- staleness: the rule this component used not to have ---------------------------------------


def test_an_index_behind_the_heap_refuses_to_answer_rather_than_answering_short(
    database: VectorFixture,
) -> None:
    """The sharpest rule of the module: an omission looks exactly like an empty neighbourhood."""
    _populate(database, 5)
    index = database.engine.index("space")
    assert index.check_freshness(index.built_through_lsn) is False
    assert index.check_freshness(index.built_through_lsn + 100) is True
    assert index.stale
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.search(
            space="space", query=(0.0, 1.0, 2.0, 3.0), k=3, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["field"] == "stale"
    assert failure.value.details["index"] == index.name


def test_the_staleness_flag_is_durable(database: VectorFixture) -> None:
    """A flag only in memory would clear itself on the reopen that most needs it."""
    _populate(database, 3)
    index = database.engine.index("space")
    index.mark_stale("a test marked this index stale")
    database.pool.invalidate(index.file)
    assert index.check_freshness(0) is True


def test_a_rebuilt_index_answers_again(database: VectorFixture) -> None:
    """The refusal has to be recoverable, or a stale index would be a dead database."""
    space, table, refs = _populate(database, 4)
    index = database.engine.index("space")
    index.mark_stale("a test marked this index stale")
    index.clear_stale(index.built_through_lsn)
    database.pool.flush(database.heap.file)
    result = database.engine.search(
        space="space", query=(0.0, 1.0, 2.0, 3.0), k=4, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k == 4
    assert space.name == "space" and len(refs) == 4 and table.name == "Chunk"


def test_marking_an_index_stale_drops_the_graph_derived_from_it(
    database: VectorFixture,
) -> None:
    """Derived state must not outlive the entries it was derived from (amendment A63)."""
    _populate(database, 4)
    index = database.engine.index("space")
    index.graph()
    index.mark_stale("a test marked this index stale")
    assert index._snapshot is None  # noqa: SLF001 - the derived state under test


# --- reconciliation ----------------------------------------------------------------------------


def test_a_pass_without_a_transaction_measures_and_removes_nothing(
    database: VectorFixture,
) -> None:
    """BR-3: a cleanup the log never saw is the one thing reconciliation may not do."""
    space, table, refs = _populate(database, 4)
    database.delete_row(table, refs[1], 2, space, (1.0, 2.0, 3.0, 4.0), csn=50)
    index = database.engine.index("space")
    report = database.engine.reconcile("space", 50)
    assert report.reclaimable == 1
    assert report.removed == 0
    assert len(index.walk()) == 4


def test_a_pass_with_a_transaction_removes_and_logs_every_removal(
    database: VectorFixture,
) -> None:
    """Every removal is a log record, so the pass is replayable and reversible by replay."""
    space, table, refs = _populate(database, 4)
    database.delete_row(table, refs[1], 2, space, (1.0, 2.0, 3.0, 4.0), csn=50)
    txn = TransactionDouble()
    report = database.engine.reconcile("space", 50, txn)
    assert report.removed == 1
    assert len(txn.records) == 1
    assert change_of(txn.records[0]).operation is IndexOperation.REMOVE
    database.engine.commit("space", txn, 60)
    index = database.engine.index("space")
    assert len(index.walk()) == 3
    assert len(index.walk()) == 3


def test_reconciliation_keeps_a_tombstone_the_horizon_has_not_passed(
    database: VectorFixture,
) -> None:
    """The boundary is what keeps a live reader answering; both sides of it are pinned."""
    space, table, refs = _populate(database, 4)
    database.delete_row(table, refs[1], 2, space, (1.0, 2.0, 3.0, 4.0), csn=51)
    assert database.engine.reconcile("space", 50).reclaimable == 0
    assert database.engine.reconcile("space", 51).reclaimable == 1


def test_a_row_a_live_snapshot_still_needs_survives_reconciliation(
    database: VectorFixture,
) -> None:
    """VTS-8: the pass is bounded by the horizon, so the old reader keeps its answer."""
    space, table, refs = _populate(database, 5)
    reader = SnapshotDouble(30)
    values = (1.0, 2.0, 3.0, 4.0)
    database.delete_row(table, refs[1], 2, space, values, csn=40)
    before = database.engine.search(space="space", query=values, k=5, snapshot=reader)
    txn = TransactionDouble()
    database.engine.reconcile("space", 30, txn)
    database.engine.commit("space", txn, 45)
    after = database.engine.search(space="space", query=values, k=5, snapshot=reader)
    assert [hit.record_id for hit in before.hits] == [hit.record_id for hit in after.hits]
    assert 2 in {hit.record_id for hit in after.hits}


def test_a_removed_entry_leaves_the_graph_too(database: VectorFixture) -> None:
    """The graph is derived, so a removal that the store applied must reach it as well."""
    space, table, refs = _populate(database, 4)
    database.delete_row(table, refs[0], 1, space, (0.0, 1.0, 2.0, 3.0), csn=40)
    index = database.engine.index("space")
    index.graph()
    txn = TransactionDouble()
    database.engine.reconcile("space", 40, txn)
    database.engine.commit("space", txn, 50)
    assert len(index.graph()) == 3


# --- staging, commit and rollback ----------------------------------------------------------------


def test_staging_changes_nothing_until_the_transaction_commits(
    database: VectorFixture,
) -> None:
    """A transaction that never commits leaves the index exactly as it found it."""
    space, table, refs = _populate(database, 2)
    index = database.engine.index("space")
    before = len(index.walk())
    pending = database.insert_row(
        table, 77, 0, space, (7.0, 7.0, 7.0, 7.0), csn=70, apply_index=False
    )
    txn = TransactionDouble()
    database.engine.stage_insert("space", 77, pending, (7.0, 7.0, 7.0, 7.0), 70, txn)
    assert len(index.walk()) == before
    assert len(txn.records) == 1
    assert database.engine.rollback("space", txn) == 1
    assert len(index.walk()) == before


def test_a_staged_record_carries_the_transaction_that_produced_it(
    database: VectorFixture,
) -> None:
    """CONTRACT 6.5: a record names the epoch and the transaction that wrote it."""
    _populate(database, 1)
    txn = TransactionDouble(txn_id=4242, epoch=7)
    record = database.engine.stage_insert(
        "space", 9, RecordRef(30, 2), (1.0, 0.0, 0.0, 0.0), 55, txn
    )
    assert txn.records
    assert record.txn_id == 4242
    assert record.epoch == 7
    assert change_of(record).operation is IndexOperation.INSERT


def test_replaying_a_record_is_idempotent(database: VectorFixture) -> None:
    """Redo walks the same records through the same door; a second pass changes nothing."""
    space, table, _refs = _populate(database, 3)
    fresh = database.insert_row(
        table, 8, 0, space, (8.0, 8.0, 8.0, 8.0), csn=80, apply_index=False
    )
    index = database.engine.index("space")
    txn = TransactionDouble()
    record = database.engine.stage_insert("space", 8, fresh, (8.0, 8.0, 8.0, 8.0), 80, txn)
    database.engine.commit("space", txn, 80)
    before = len(index.walk())
    index.apply(record)
    index.apply(record)
    assert len(index.walk()) == before
    assert len(index.graph()) == before


def test_a_record_of_another_index_is_refused(database: VectorFixture) -> None:
    """Every secondary index writes into one log, so a record names the index it belongs to."""
    from okto_grafx.domain.model.schema import ColumnDef, TableDef
    from okto_grafx.domain.model.value import ValueType

    first = database.create_space("space", 4)
    second = database.create_space("second", 4)
    table = TableDef(
        table_id=database.catalog_store.catalog.next_table_id(),
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="a", type=first.value_type, vector_space="space"),
            ColumnDef(name="b", type=second.value_type, vector_space="second"),
        ),
        primary_key="id",
    )
    database.catalog_store.catalog.add_table(table)
    database.catalog_store.save()
    here = database.engine.attach(table, "space")
    there = database.engine.attach(table, "second")
    txn = TransactionDouble()
    foreign = there.stage_insert(txn, there.key_for((1.0, 0.0, 0.0, 0.0)), RecordRef(5, 1), 90)
    with pytest.raises(GrafxIndexError) as failure:
        here.apply(foreign)
    assert failure.value.details["field"] == "index"


def test_a_snapshot_that_cannot_answer_visibility_is_refused(
    database: VectorFixture,
) -> None:
    """The visibility rule is taken structurally, so the argument must be able to answer it."""
    _populate(database, 1)
    index = database.engine.index("space")
    with pytest.raises(GrafxConfigurationError) as failure:
        index.search((1.0, 0.0, 0.0, 0.0), 1, object())  # type: ignore[arg-type]
    assert failure.value.details["field"] == "snapshot"


def test_the_index_refuses_a_stale_search_at_its_own_door(database: VectorFixture) -> None:
    """The index is a public object of the framework, so it guards itself as well as the engine.

    The engine checks staleness before it plans, because both regimes enumerate their candidates
    from the index. That does not cover a caller holding the index directly -- a verifier, a
    maintenance pass, or the query planner asking for a traversal -- so the traversal door has
    the same refusal. The battery found this by reverting it and watching the engine's check
    answer instead (amendment A67: each layer must be independently observable).
    """
    _populate(database, 4)
    index = database.engine.index("space")
    index.mark_stale("a test marked this index stale")
    with pytest.raises(GrafxIndexError) as failure:
        index.search((0.0, 1.0, 2.0, 3.0), 2, SnapshotDouble(1000))
    assert failure.value.details["field"] == "stale"
    assert failure.value.details["index"] == index.name


def test_a_redo_record_reaches_a_graph_that_is_already_built(
    database: VectorFixture,
) -> None:
    """Redo has to update the derived state, not only the pages under it.

    A graph that was never built is rebuilt from the store on demand and would look correct
    however ``apply`` behaved -- which is why the earlier idempotence test could not see this.
    The graph is therefore built FIRST, so the assertion is about what the redo did to it.
    """
    space, table, _refs = _populate(database, 3)
    index = database.engine.index("space")
    assert len(index.graph()) == 3
    fresh = database.insert_row(
        table, 9, 0, space, (9.0, 9.0, 9.0, 9.0), csn=90, apply_index=False
    )
    txn = TransactionDouble()
    record = index.stage_insert(txn, index.key_for((9.0, 9.0, 9.0, 9.0)), fresh, 90)
    index.rollback(txn)
    index.apply(record)
    assert len(index.walk()) == 4
    assert len(index.graph()) == 4
    database.pool.flush(index.file)
    database.pool.flush(database.heap.file)
    scored, _stats = index.search((9.0, 9.0, 9.0, 9.0), 1, SnapshotDouble(1000))
    assert [item.record_id for item in scored] == [9]


def test_a_replayed_removal_retires_the_warm_count_before_the_canonical_read(
    database: VectorFixture,
) -> None:
    """REMOVE replay also has no inferred delta, even when it lands exactly once."""
    space, table, refs = _populate(database, 1)
    index = database.engine.index("space")
    database.delete_row(table, refs[0], 1, space, (0.0, 1.0, 2.0, 3.0), csn=40)
    assert len(index.graph()) == 1
    txn = TransactionDouble()
    database.engine.reconcile("space", 40, txn)
    record = txn.records[0]
    index.rollback(txn)
    index.apply(record)  # type: ignore[arg-type]
    database.pool.flush(index.file)

    assert index._snapshot is None  # noqa: SLF001 - replay retired derived state
    assert index.live_count() == 0


def test_a_tombstone_reaches_a_warm_graph_and_the_traversal_drops_the_row(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The sharpest rule of the component, in the one path that could still get it wrong.

    The traversal decides visibility from the entry it holds, so a tombstone that reached the
    store but not the derived graph would leave a DELETED row rankable and returnable -- the
    wrong-result shape CONTRACT 13 names first. Every other test of this rule runs in the exact
    regime, which reads the heap and would exclude the row however the graph behaved; the
    battery found the gap by reverting the tombstone update and watching the suite stay green.

    Three things therefore have to be true of this test and are asserted: the regime is
    approximate, the graph was warm before the delete, and the row is gone from the answer.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=32)
    space = database.create_space("space", 4)
    table = database.create_table("Chunk", "space")
    refs = [
        database.insert_row(
            table,
            index + 1,
            0,
            space,
            tuple(float(index + position) for position in range(4)),
            csn=10 + index,
        )
        for index in range(6)
    ]
    index = database.engine.index("space")
    assert len(index.graph()) == 6

    doomed = (2.0, 3.0, 4.0, 5.0)
    before = database.engine.search(
        space="space", query=doomed, k=6, snapshot=SnapshotDouble(1000)
    )
    assert before.regime == "approximate"
    assert before.hits[0].record_id == 3

    database.delete_row(table, refs[2], 3, space, doomed, csn=50)
    after = database.engine.search(
        space="space", query=doomed, k=6, snapshot=SnapshotDouble(1000)
    )
    assert after.regime == "approximate"
    assert 3 not in {hit.record_id for hit in after.hits}
    assert after.achieved_k == 5

    # And the version is still THERE, as a bridge, for the snapshot that may still see it.
    assert len(index.graph()) == 6
    older = database.engine.search(
        space="space", query=doomed, k=6, snapshot=SnapshotDouble(49)
    )
    assert 3 in {hit.record_id for hit in older.hits}


def test_a_reopened_database_finds_its_vectors_without_replaying_anything(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The durability the adoption bought, asserted end to end.

    A memory-resident index cannot do this: its entries live only in the process that wrote them,
    so a reopen either replays the whole log or answers short. Here the entries are on pages, the
    second engine attaches to the same file, and the graph is rebuilt from what the store already
    holds -- so the answer is the same one the first engine gave.
    """
    from okto_grafx.adapters.codec_v1 import PageCodecV1
    from okto_grafx.adapters.vectormath_pure import PureVectorMath
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.index_manager import IndexManager
    from okto_grafx.engine.vector_engine import VectorEngine

    first = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=32)
    space = first.create_space("space", 4)
    table = first.create_table("Chunk", "space")
    corpus = [tuple(float(index + position) for position in range(4)) for index in range(6)]
    for index, values in enumerate(corpus):
        first.insert_row(table, index + 1, 0, space, values, csn=10 + index)
    snapshot = SnapshotDouble(1000)
    before = first.engine.search(space="space", query=corpus[2], k=4, snapshot=snapshot)
    first.pool.flush()

    # A second composition over the SAME device: new pool, new stores, new registry, new engine.
    pool = BufferPool(
        first.device,
        PageCodecV1(first.device.page_size),
        metrics,
        budget_bytes=256 * first.device.page_size,
        db_label="reopened",
    )
    catalog = CatalogStore(pool)
    catalog.load()
    heap = HeapStore(pool, catalog)
    registry = IndexManager(pool, heap, metrics)
    engine = VectorEngine(
        catalog=catalog,
        heap=heap,
        pool=pool,
        indexes=registry,
        math=PureVectorMath(),
        metrics=metrics,
        clock=clock,
        exact_scan_threshold=0,
        ef_search=32,
    )
    reopened = engine.attach(catalog.catalog.table("Chunk"), "space")

    assert len(reopened.walk()) == 6
    assert reopened.stale is False
    after = engine.search(space="space", query=corpus[2], k=4, snapshot=snapshot)
    assert [hit.record_id for hit in after.hits] == [hit.record_id for hit in before.hits]
    assert [hit.score for hit in after.hits] == [hit.score for hit in before.hits]


def test_a_warm_vector_graph_rebases_after_a_foreign_rebuild(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A second participant's healthy generation replaces both warm pages and the warm graph."""
    from okto_grafx.adapters.codec_v1 import PageCodecV1
    from okto_grafx.adapters.vectormath_pure import PureVectorMath
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.index_manager import IndexManager
    from okto_grafx.engine.vector_engine import VectorEngine

    reader = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=32)
    space = reader.create_space("space", 4)
    table = reader.create_table("Chunk", "space")
    corpus = [tuple(float(index + position) for position in range(4)) for index in range(4)]
    for record_id, values in enumerate(corpus, start=1):
        reader.insert_row(table, record_id, 0, space, values, csn=10 + record_id)
    before = reader.engine.search(
        space="space", query=corpus[0], k=5, snapshot=SnapshotDouble(1000)
    )
    assert before.achieved_k == 4
    assert reader.engine.index("space")._snapshot is not None  # noqa: SLF001
    reader.pool.flush()

    pool = BufferPool(
        reader.device,
        PageCodecV1(reader.device.page_size),
        metrics,
        budget_bytes=256 * reader.device.page_size,
        db_label="foreign_rebuilder",
    )
    catalog = CatalogStore(pool)
    catalog.load()
    heap = HeapStore(pool, catalog)
    registry = IndexManager(pool, heap, metrics)
    writer = VectorEngine(
        catalog=catalog,
        heap=heap,
        pool=pool,
        indexes=registry,
        math=PureVectorMath(),
        metrics=metrics,
        clock=clock,
        exact_scan_threshold=0,
        ef_search=32,
    )
    writer_index = writer.attach(catalog.catalog.table("Chunk"), "space")
    writer_space = catalog.catalog.space("space")
    writer_table = catalog.catalog.table("Chunk")
    new_values = (20.0, 21.0, 22.0, 23.0)
    stored = writer.validate_vector(writer_space, new_values)
    heap.insert(
        writer_table,
        5,
        (
            5,
            0,
            VectorValue(stored, writer_space.space_id, writer_space.storage_dtype),
        ),
        20,
    )
    pool.flush(heap.file)
    through = reader.engine.index("space").built_through_lsn
    rebuild = TransactionDouble(txn_id=901)
    registry.rebuild(writer_index.name, rebuild, through)
    registry.commit(rebuild, through)
    registry.clear_stale(writer_index.name, through)

    after = reader.engine.search(
        space="space", query=new_values, k=5, snapshot=SnapshotDouble(1000)
    )
    assert after.achieved_k == 5
    assert {hit.record_id for hit in after.hits} == {1, 2, 3, 4, 5}


def test_an_inflight_graph_build_cannot_publish_across_a_foreign_rebuild(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A build from generation A is discarded when generation B lands at the same LSN."""
    from okto_grafx.adapters.codec_v1 import PageCodecV1
    from okto_grafx.adapters.graph_guard import ConditionGuard
    from okto_grafx.adapters.vectormath_pure import PureVectorMath
    from okto_grafx.engine.buffer_pool import BufferPool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.index_manager import IndexManager
    from okto_grafx.engine.vector_engine import VectorEngine

    reader = VectorFixture(
        metrics=metrics,
        clock=clock,
        exact_scan_threshold=0,
        ef_search=32,
        guard=ConditionGuard(),
    )
    space = reader.create_space("space", 4)
    table = reader.create_table("Chunk", "space")
    corpus = [tuple(float(index + position) for position in range(4)) for index in range(4)]
    for record_id, values in enumerate(corpus, start=1):
        reader.insert_row(table, record_id, 0, space, values, csn=10 + record_id)
    reader.pool.flush()
    index = reader.engine.index("space")

    parked = threading.Event()
    released = threading.Event()
    original_resolve = index._resolve  # noqa: SLF001 - deterministic build interleaving
    first_resolve = True

    def park_first_resolve(ref: RecordRef) -> object:
        nonlocal first_resolve
        if first_resolve:
            first_resolve = False
            parked.set()
            assert released.wait(5.0), "the foreign rebuild never released the old builder"
        return original_resolve(ref)

    index._resolve = park_first_resolve  # type: ignore[assignment]  # noqa: SLF001
    failures: list[BaseException] = []

    def build_old_generation() -> None:
        try:
            index.snapshot()
        except BaseException as failure:  # pragma: no cover - asserted below
            failures.append(failure)

    builder = threading.Thread(target=build_old_generation)
    builder.start()
    try:
        assert parked.wait(5.0), "the generation-A graph build never parked"

        pool = BufferPool(
            reader.device,
            PageCodecV1(reader.device.page_size),
            metrics,
            budget_bytes=256 * reader.device.page_size,
            db_label="inflight_foreign_rebuilder",
        )
        catalog = CatalogStore(pool)
        catalog.load()
        heap = HeapStore(pool, catalog)
        registry = IndexManager(pool, heap, metrics)
        writer = VectorEngine(
            catalog=catalog,
            heap=heap,
            pool=pool,
            indexes=registry,
            math=PureVectorMath(),
            metrics=metrics,
            clock=clock,
            exact_scan_threshold=0,
            ef_search=32,
        )
        writer_index = writer.attach(catalog.catalog.table("Chunk"), "space")
        writer_space = catalog.catalog.space("space")
        writer_table = catalog.catalog.table("Chunk")
        new_values = (20.0, 21.0, 22.0, 23.0)
        stored = writer.validate_vector(writer_space, new_values)
        heap.insert(
            writer_table,
            5,
            (
                5,
                0,
                VectorValue(stored, writer_space.space_id, writer_space.storage_dtype),
            ),
            20,
        )
        pool.flush(heap.file)
        through = index.built_through_lsn
        rebuild = TransactionDouble(txn_id=902)
        registry.rebuild(writer_index.name, rebuild, through)
        registry.commit(rebuild, through)
        registry.clear_stale(writer_index.name, through)

        certificate = index.begin_exact_read(1000)
        index._refresh_companion(certificate)  # noqa: SLF001 - same composed read fence
    finally:
        released.set()
        builder.join(5.0)

    assert not builder.is_alive(), "the superseded graph builder did not finish"
    assert failures == []
    after = reader.engine.search(
        space="space", query=(20.0, 21.0, 22.0, 23.0), k=5, snapshot=SnapshotDouble(1000)
    )
    assert after.achieved_k == 5
    assert {hit.record_id for hit in after.hits} == {1, 2, 3, 4, 5}


def test_a_reset_clears_the_store_and_the_graph_derived_from_it(
    database: VectorFixture,
) -> None:
    """The rebuild path: clearing every bucket must clear the derived state with it.

    ``stage_reset`` is how a rebuild starts, and it is the one change that removes everything at
    once. A graph that survived it would rank versions the store no longer holds -- every hit a
    row that is not there. The battery found this uncovered because nothing in the suite had
    reason to reset an index.
    """
    space, table, _refs = _populate(database, 4)
    index = database.engine.index("space")
    assert len(index.graph()) == 4
    txn = TransactionDouble()
    index.mark_stale("the reset is a rebuild generation")
    record = index.stage_reset(txn, index.built_through_lsn)
    assert txn.records == [record]
    database.engine.commit("space", txn, 100)
    assert index.walk() == ()
    assert index._snapshot is None  # noqa: SLF001 - RESET retires every derived value
    assert index.live_count() == 0
    assert len(index.graph()) == 0
    result = database.engine.search(
        space="space", query=(0.0, 1.0, 2.0, 3.0), k=4, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k == 0
    assert space.name == "space" and table.name == "Chunk"
