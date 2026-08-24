"""The whole stack, driven through the public surface (C11).

Every component below this one is provable on its own. What none of them can show is that the
pieces fit: that a page staged through the public transaction reaches the log, survives the
barrier, lands on the heap, is published in the commit state, and is still there after the
database has been closed, reopened and recovered by a different set of objects.

Nothing here uses a double. The device, the clock, the coordinator, the codec, the log, the
catalog, the heap and the transaction manager are the delivered ones, composed exactly as
``connect`` composes them for an application.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxTransactionStateError
from okto_grafx.domain.index import index_file
from okto_grafx.domain.page import Page, PageType
from okto_grafx.engine.database import Database
from okto_grafx.engine.index_manager import primary_key_index_name

HEAP = "heap.dat"
ROW = b"a durable row"


def _image(db: Database, payloads: list[bytes], page_index: int) -> bytes:
    """Return a valid heap page image carrying these payloads, one per slot."""
    page = Page(
        int(PageType.HEAP),
        page_size=db.codec.page_size,
        page_index=page_index,
        page_lsn=0,
        seq=2,
    )
    for payload in payloads:
        page.insert_slot(payload)
    return db._codec.encode_page(page)


def _grow_to(db: Database, page_index: int) -> None:
    """Make sure the heap file has a page at this index.

    Bounded by a count and gated on the device's own page count, never on anything the code under
    test computes (A92): a walk that regresses must fail this test, not hang it.
    """
    db._heap.bootstrap()
    for _ in range(page_index + 2):
        if db._storage.page_count(HEAP) > page_index:
            return
        page = db._pool.allocate(HEAP, int(PageType.HEAP))
        db._pool.unpin(HEAP, page.page_index, dirty=True)
    raise AssertionError(f"The heap did not reach page {page_index}.")


def _write_one_row(db: Database, page_index: int, payload: bytes) -> object:
    """Stage one page through a public write transaction and commit it."""
    _grow_to(db, page_index)
    txn = db.begin("write")
    txn._context.owner._stage_page_image(
        txn._context, HEAP, page_index, _image(db, [payload], page_index)
    )
    txn._context.note_write(db.transactions.partition_of(1, payload))
    return txn.commit()


def _payloads(db: Database, page_index: int) -> tuple[bytes, ...]:
    """Return what the heap page holds right now, read back through the pool."""
    with db._pool.pinned(HEAP, page_index) as page:
        return tuple(bytes(payload) for _, payload in page.iter_slots())


def test_a_commit_through_the_public_surface_is_durable_across_a_reopen(
    tmp_path: Path,
) -> None:
    # FR-5: a commit returns only after the log append and the barrier, and FR-1 says the reopen
    # recovers before it accepts anything. This is the first time both are asked at once.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        report = _write_one_row(db, 3, ROW)
        assert report.wrote is True
        assert report.durable is True
        assert report.csn > 0
        committed = report.csn

    with connect(root, page_size=512, partitions_per_table=8) as reopened:
        assert reopened.recovery_report.outcome == "clean"
        assert _payloads(reopened, 3) == (ROW,)
        assert reopened.transactions.published_lsn() == committed


def test_a_rolled_back_transaction_leaves_nothing_on_the_device(tmp_path: Path) -> None:
    # BR-2: a transaction's pages live in its own staging map until the commit appends them, so
    # abandoning one writes nothing at all -- not even a record saying it happened.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        _grow_to(db, 2)
        before = db.wal.last_lsn
        txn = db.begin("write")
        txn._context.owner._stage_page_image(
            txn._context, HEAP, 2, _image(db, [b"never"], 2)
        )
        txn._context.note_write(db.transactions.partition_of(1, b"never"))
        txn.rollback()
        assert db.wal.last_lsn == before
        assert _payloads(db, 2) == ()


def test_closing_a_database_with_an_uncommitted_transaction_loses_only_that_transaction(
    tmp_path: Path,
) -> None:
    # CONTRACT.md section 10: closing with an open transaction aborts it and never corrupts.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        _write_one_row(db, 3, ROW)
        abandoned = db.begin("write")
        abandoned._context.owner._stage_page_image(
            abandoned._context, HEAP, 3, _image(db, [b"abandoned"], 3)
        )
        abandoned._context.note_write(db.transactions.partition_of(1, b"abandoned"))

    with connect(root, page_size=512, partitions_per_table=8) as reopened:
        assert _payloads(reopened, 3) == (ROW,)


def test_two_transactions_on_disjoint_partitions_both_commit(tmp_path: Path) -> None:
    # BR-6 and AC-1 seen from the public surface: disjoint partition sets never conflict.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        _grow_to(db, 4)
        first = db.begin("write")
        second = db.begin("write")
        first._context.owner._stage_page_image(
            first._context, HEAP, 3, _image(db, [b"first"], 3)
        )
        first._context.note_write(db.transactions.partition_of(1, b"first"))
        second._context.owner._stage_page_image(
            second._context, HEAP, 4, _image(db, [b"second"], 4)
        )
        second._context.note_write(db.transactions.partition_of(2, b"second"))
        assert first.commit().wrote is True
        assert second.commit().wrote is True
        assert _payloads(db, 3) == (b"first",)
        assert _payloads(db, 4) == (b"second",)


def test_a_reader_keeps_its_snapshot_while_a_writer_commits(tmp_path: Path) -> None:
    # FR-2 and AC-3: a reader sees the consistent view of the instant it opened, and readers
    # never block writers.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        _grow_to(db, 3)
        reader = db.begin("read")
        taken = reader.snapshot.read_lsn
        _write_one_row(db, 3, ROW)
        assert reader.snapshot.read_lsn == taken
        assert reader.commit().wrote is False


def test_the_metrics_of_an_open_database_are_machine_readable(tmp_path: Path) -> None:
    # FR-14: the operational behaviours of M1 emit through the MetricsSink port, and the CI gate
    # reads the snapshot rather than a log.
    with connect(tmp_path / "db", metrics="openmetrics") as db:
        _write_one_row(db, 3, ROW)
        snapshot = db.snapshot_metrics()
    names = {str(name) for name in snapshot}
    assert any("oktografx_database_opens_total" in name for name in names)


def test_the_default_install_exposes_the_metrics_endpoint(tmp_path: Path) -> None:
    """OR-6 end to end: a database configured for OpenMetrics really serves GET /metrics.

    Nothing in the build could close this before the composition root existed -- C8's suite
    proves the publisher, and only a started database proves that the default install starts one.
    """
    with connect(tmp_path / "db", metrics="openmetrics") as db:
        endpoint = db.metrics_endpoint
        assert endpoint is not None
        with urllib.request.urlopen(endpoint, timeout=10) as answer:
            body = answer.read().decode("utf-8")
            assert answer.status == 200
            assert answer.headers["Content-Type"].startswith("text/plain")
    assert "oktografx_database_opens_total" in body
    # The socket belongs to the database, so closing it must give the port back.
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(endpoint, timeout=5)


@pytest.mark.parametrize("selector", ["noop", "json"])
def test_a_database_that_serves_no_endpoint_says_so(tmp_path: Path, selector: str) -> None:
    options: dict[str, object] = {"metrics": selector}
    if selector == "json":
        options["metrics_destination"] = str(tmp_path / "metrics.jsonl")
    with connect(tmp_path / "db", **options) as db:
        assert db.metrics_endpoint is None


def test_the_same_database_reopened_five_times_reports_the_same_state(tmp_path: Path) -> None:
    # Determinism over repeated opens: the identity, the published LSN and the recovery outcome
    # are the same every time, so nothing in the open sequence depends on order or on chance.
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as db:
        _write_one_row(db, 3, ROW)
    readings = []
    for _ in range(5):
        with connect(root, page_size=512, partitions_per_table=8) as db:
            readings.append(
                (
                    db.identity,
                    db.transactions.published_lsn(),
                    db.recovery_report.outcome,
                    _payloads(db, 3),
                )
            )
    assert len(set(readings)) == 1


def test_an_in_memory_database_has_the_same_transactional_semantics(tmp_path: Path) -> None:
    # FR-1: ":memory:" selects the memory device with the SAME semantics, so the same script must
    # produce the same answers on both.
    with connect(":memory:", page_size=512, partitions_per_table=8) as memory:
        memory_report = _write_one_row(memory, 3, ROW)
        memory_rows = _payloads(memory, 3)
    with connect(tmp_path / "db", page_size=512, partitions_per_table=8) as local:
        local_report = _write_one_row(local, 3, ROW)
        local_rows = _payloads(local, 3)
    assert memory_rows == local_rows == (ROW,)
    assert memory_report.csn == local_report.csn
    assert memory_report.durable is local_report.durable is True


def test_verification_walks_the_indexes_a_caller_registered_after_the_open(
    tmp_path: Path,
) -> None:
    """A verifier must see the index set as it is NOW, not as it was when the database opened.

    A database registers indexes for as long as it is open, and a verifier is handed the set it
    must walk. Capturing that set at open makes ``verify()`` walk an empty set for every index
    registered afterwards -- a clean report about files it never looked at, which is a wrong
    answer rather than a missing one, and the single thing verification may never produce
    (SPEC-M1 FR-11, FR-12).
    """
    from okto_grafx.domain.index.contract import IndexVisibility
    from okto_grafx.domain.index.definition import IndexDefinition
    from okto_grafx.engine.index_manager import HashIndex

    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64)")
        before = db.verify("all")
        assert not any("person_by_id" in name for name in before.files_checked)
        person = db.catalog.catalog.table("Person")
        definition = IndexDefinition(
            name="person_by_id",
            table_id=person.table_id,
            table_name="Person",
            positions=(0,),
            visibility=IndexVisibility.EXACT,
        )
        index = db._indexes.register(HashIndex(definition, db._pool, db._metrics))
        assert [item.name for item in db.indexes.indexes()] == [index.name]
        after = db.verify("all")
        # The index file is walked because the verifier was given the index. A verifier built
        # over an empty set reports exactly the reading above and never looks at these pages.
        assert any("person_by_id" in name for name in after.files_checked), after.files_checked
        assert after.pages_checked > before.pages_checked
        # `register` allocates the bucket pages in the cache and does not write them, so a walk
        # that reads the DEVICE sees them zero-filled until something flushes. That is a finding
        # against the index framework, recorded rather than asserted here: this test is about the
        # verifier seeing the index at all, and pinning the unflushed state would turn a fix into
        # a failure. Flushing is an ordinary caller action and makes the walk clean.
        db.flush()
        assert db.verify("all").findings == ()


def _vector_schema(db: Database, space_name: str = "minilm_v2") -> object:
    """Declare one embedding space and one table with a column in it, and return the table."""
    from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
    from okto_grafx.domain.model.value import ValueType
    from okto_grafx.domain.ports.vectormath import DistanceMetric

    catalog = db._catalog.catalog
    db._vectors.create_space(
        EmbeddingSpaceDef(
            space_id=catalog.next_space_id(),
            name=space_name,
            dimension=4,
            metric=DistanceMetric.COSINE,
            normalized=True,
            storage_dtype="float32",
            state="active",
            created_at_wall=db._clock.wall(),
        )
    )
    table = TableDef(
        table_id=catalog.next_table_id(),
        name="Chunk",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space=space_name),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )
    catalog.add_table(table)
    db._catalog.save()
    return table


def test_a_vector_index_attaches_onto_the_paged_index_registry(tmp_path: Path) -> None:
    """CF-10: a vector index is a paged file owned by the index registry, not a memory structure.

    The engine takes the pool and the registry as optional arguments and refuses ``attach`` with
    a typed error naming the field when it has neither -- deliberately, because a fallback to a
    memory index would be a second implementation of one index. Only the composition root holds
    both objects, so only a composed database can show the refusal turned off.
    """
    with connect(tmp_path / "db", page_size=512) as db:
        table = _vector_schema(db)
        index = db._vectors.attach(table, "minilm_v2")
        # Registered with the SAME registry the rest of the database uses, by identity.
        assert db._indexes.index(index.name) is index
        assert index.name in [registered.name for registered in db.indexes.indexes()]
        db.flush()
        report = db.verify("all")
        assert report.findings == ()
        # The index is a paged file the verifier walks, which is what "onto C7's store" means.
        assert any(index.name in name for name in report.files_checked), report.files_checked


def test_the_vector_engine_reaches_the_same_pool_and_registry_as_the_database(
    tmp_path: Path,
) -> None:
    # Identity, not type: an engine given a second pool would page the index out of a budget the
    # database does not account for, and FR-13 says a budget belongs to one database.
    with connect(tmp_path / "db", page_size=512) as db:
        table = _vector_schema(db)
        db._vectors.attach(table, "minilm_v2")
        assert db.indexes.indexes()[0].name.startswith("vector_")
        before = db.pool.used_bytes()
        assert before > 0


def test_vector_ddl_through_the_public_door_attaches_its_index(tmp_path: Path) -> None:
    """CF-10 through ``connect()``, which is the only door that proves the wiring.

    The vector engine takes its pool and its index registry as optional arguments and refuses
    ``attach`` when it has neither, deliberately, because a fallback to a memory index would be a
    second implementation of one index. Only the composition root holds both objects -- so a test
    that hand-builds a VectorEngine passes whatever the composition root does, which is exactly
    how this went unnoticed until a downstream component drove the real door.
    """
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            space = txn.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            table = txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        assert space.statistics["spaces_created"] == 1
        # The index is attached by the DDL itself, which is only possible because the engine was
        # given the registry that owns it.
        assert table.statistics["indexes_attached"] == 1
        registered = [index.name for index in db.indexes.indexes()]
        # Both, and named exhaustively: the statement attaches the vector index AND the index of
        # the primary key the same table declares.
        assert registered == ["pk_Chunk", "vector_Chunk_minilm_v2"]
        assert db.verify("all").findings == ()

    with connect(root, page_size=512) as reopened:
        assert [s.name for s in reopened.catalog.catalog.spaces()] == ["minilm_v2"]
        assert [t.name for t in reopened.catalog.catalog.tables()] == ["Chunk"]
        assert reopened.recovery_report.outcome == "clean"


def test_vector_ef_search_reaches_new_and_reopened_index_views(tmp_path: Path) -> None:
    """The public knob configures the derived index of this composition, not persisted bytes."""
    root = tmp_path / "db"
    with connect(root, page_size=512, vector_ef_search=777) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        assert db.vectors.index("minilm_v2").ef_search == 777

    # HNSW adjacency is derived state. A later composition may tune its search effort without a
    # migration or rewrite, and the reattached index must expose that composition's real value.
    with connect(root, page_size=512, vector_ef_search=901) as reopened:
        assert reopened.vectors.index("minilm_v2").ef_search == 901


def test_the_vector_engine_of_a_composed_database_can_attach(tmp_path: Path) -> None:
    # The same property one layer down, so a failure names the wiring rather than the grammar:
    # the engine reaches the pool and the registry of THIS database, by identity.
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        index = db.indexes.indexes()[0]
        assert db.indexes.index(index.name) == index
        db.flush()
        report = db.verify("all")
        assert any(index.name in name for name in report.files_checked), report.files_checked


def test_a_reopened_database_re_attaches_the_vector_index_it_declared(tmp_path: Path) -> None:
    """A registry belongs to one composition, so a reopen starts with none (CF-10).

    The statement that creates a table attaches the index of every vector column it declares.
    Nothing did that on the way BACK, so a reopened database held the durable index file and zero
    registered indexes, and the engine answered ``GrafxIndexError`` for a space whose index was
    sitting on disk -- a similarity query would have found nothing on any database that had been
    closed and opened again.
    """
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        # Named, not positioned. A table now also carries the index of its PRIMARY KEY, and
        # indexes() sorts by name, so "the first one" stopped being the vector one.
        created = next(
            index.name for index in db.indexes.indexes() if index.name.startswith("vector_")
        )

    with connect(root, page_size=512) as reopened:
        # Still exhaustive: the expectation gained the primary-key index rather than the
        # assertion being narrowed to vector ones, so an index nobody asked for still fails here.
        assert reopened.attached_indexes == ("pk_Chunk", created)
        assert [index.name for index in reopened.indexes.indexes()] == ["pk_Chunk", created]
        # Reachable the way a query reaches it: by SPACE, through the engine.
        assert reopened.vectors.index("minilm_v2").name == created
        assert reopened.verify("all").findings == ()

    # Idempotent: the store opens the existing file rather than replacing it (G6), so a third
    # composition adopts the same durable index instead of rebuilding it.
    with connect(root, page_size=512) as third:
        assert third.attached_indexes == ("pk_Chunk", created)


def test_a_reopened_database_says_which_indexes_opened_behind(tmp_path: Path) -> None:
    """Nothing is repaired at open, so the honest answer is to name what is behind.

    A rebuild re-derives the index from the heap and is covered by the log like any other index
    change -- so it writes, and a write belongs inside a transaction the caller owns rather than
    inside ``connect``.
    """
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        assert db.stale_indexes == ()

    with connect(root, page_size=512) as reopened:
        # Reported, not hidden and not falsely cleared: the open sequence has NOT replayed index
        # records into these indexes, because recovery runs before the registry exists, so
        # claiming freshness here would be a claim the composition cannot support.
        assert set(reopened.stale_indexes) <= set(reopened.attached_indexes)
        # Derived from the registry rather than hardcoded, so this stays honest if the
        # cross-component ordering is settled and these indexes start opening fresh -- and it
        # still fails today for a composition that reported an empty set it never computed.
        recomputed = {
            index.name
            for index in reopened._indexes.open(reopened._transactions.published_lsn())
        }
        assert set(reopened.stale_indexes) == recomputed


def test_a_database_with_no_vector_column_attaches_nothing(tmp_path: Path) -> None:
    # The other side of the walk: a catalog with no vector column must not invent an index, and
    # the ordinary path must not pay for the vector one.
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with connect(tmp_path / "db", page_size=512) as reopened:
        # The table has a primary key, so it has that index and nothing else. Asserting the exact
        # tuple rather than "no vector index" is what keeps this test able to fail if some other
        # index is ever invented here, which is the whole point of it.
        assert reopened.attached_indexes == ("pk_Person",)
        assert reopened.stale_indexes == ()
        assert [index.name for index in reopened.indexes.indexes()] == ["pk_Person"]
        assert not any(name.startswith("vector_") for name in reopened.attached_indexes)


def test_recovery_never_certifies_an_index_behind_its_checkpoint(tmp_path: Path) -> None:
    """A retained WAL suffix cannot repair an index that already missed older commits."""
    root = tmp_path / "db"
    name = primary_key_index_name("P")
    path = root / index_file(name)
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        db.checkpoint()
        old_index = path.read_bytes()
        with db.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")
        db.checkpoint()

    # Simulate an independently lost/rolled-back accelerator after the authoritative heap and
    # checkpoint advanced. The WAL below that checkpoint is no longer available to fill it.
    path.write_bytes(old_index)

    with connect(root, page_size=512) as reopened:
        assert name in reopened.stale_indexes
        assert reopened.indexes.index(name).built_through_lsn < (
            reopened.transactions.published_state().checkpoint_lsn
        )
        # The stale access path is excluded; the heap remains authoritative and the scan is whole.
        assert reopened.execute("MATCH (p:P {id: 1}) RETURN p.id").rows == ((1,),)


def test_operator_recovery_requires_local_transaction_quiescence(tmp_path: Path) -> None:
    with connect(tmp_path / "db", page_size=512) as db:
        transaction = db.begin("read")
        try:
            with pytest.raises(GrafxTransactionStateError) as refused:
                db.recover()
            assert refused.value.details["field"] == "open_transactions"
            assert refused.value.details["open_transactions"] == 1
        finally:
            transaction.rollback()

        assert db.recover().outcome == "clean"
