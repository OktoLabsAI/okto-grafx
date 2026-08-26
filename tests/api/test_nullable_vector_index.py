"""A nullable vector column is represented by a sparse, durable vector index."""

from __future__ import annotations

from pathlib import Path

from okto_grafx import Database, Transaction, VectorValue, connect
from okto_grafx.engine.index_manager import IndexStore


def _create_schema(database: Database) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        txn.execute(
            "CREATE NODE TABLE Chunk("
            "id INT64, body STRING, embedding VECTOR(s), PRIMARY KEY(id))"
        )


def _vector_index(database: Database) -> IndexStore:
    return next(
        index
        for index in database.indexes.indexes()
        if index.name.startswith("vector_Chunk_")
    )


def _assert_vector_coverage_is_current(database: Database) -> None:
    """A NULL-only table commit still belongs to the vector index's covered history."""
    assert (
        _vector_index(database).built_through_lsn
        == database.transactions.published_lsn()
    )


def _search_ids(
    database: Database, *, expected_regime: str | None = None
) -> tuple[int, ...]:
    reader = database.begin("read")
    try:
        return _search_ids_in(database, reader, expected_regime=expected_regime)
    finally:
        reader.rollback()


def _search_ids_in(
    database: Database,
    reader: Transaction,
    *,
    expected_regime: str | None = None,
) -> tuple[int, ...]:
    result = database.search_vectors(
        reader,
        space="s",
        query=(1.0, 0.0, 0.0, 0.0),
        k=10,
    )
    if expected_regime is not None:
        assert result.regime == expected_regime
    assert result.achieved_k == len(result.hits)
    return tuple(sorted(hit.record_id for hit in result.hits))


def _vector(database: Database, values: tuple[float, ...]) -> VectorValue:
    return VectorValue(
        values=values,
        space_ref=database.catalog.catalog.space("s").space_id,
    )


def test_nullable_vector_transitions_are_sparse_durable_and_rebuildable(
    tmp_path: Path,
) -> None:
    """Exercise every NULL transition, rollback, verification and a cold reopen.

    Each NULL-only commit is separate so a non-null sibling cannot accidentally advance the
    vector index on its behalf.  That makes the coverage acknowledgement observable rather than
    merely proving that key derivation stopped raising.
    """
    root = tmp_path / "database"
    with connect(root, vector_exact_scan_threshold=4096) as database:
        _create_schema(database)

        with database.begin("write") as txn:
            txn.execute(
                "CREATE (:Chunk {id: 1, body: 'vector', "
                "embedding: [1.0, 0.0, 0.0, 0.0]})"
            )
        _assert_vector_coverage_is_current(database)

        # INSERT with an omitted vector and with an explicit NULL both create heap rows and no
        # vector entries.  They nevertheless advance this index through their commits.
        with database.begin("write") as txn:
            txn.execute("CREATE (:Chunk {id: 2, body: 'omitted'})")
        _assert_vector_coverage_is_current(database)
        with database.begin("write") as txn:
            txn.execute(
                "CREATE (:Chunk {id: 3, body: 'explicit-null', embedding: $value})",
                {"value": None},
            )
        _assert_vector_coverage_is_current(database)
        assert _search_ids(database, expected_regime="exact") == (1,)

        # vector -> NULL and NULL -> vector have one real effect each and may share a commit.
        old_reader = database.begin("read")
        try:
            with database.begin("write") as txn:
                txn.execute(
                    "MATCH (c:Chunk {id: 1}) SET c.embedding = $value",
                    {"value": None},
                )
                txn.execute(
                    "MATCH (c:Chunk {id: 2}) SET c.embedding = $value",
                    {"value": _vector(database, (0.0, 1.0, 0.0, 0.0))},
                )
            _assert_vector_coverage_is_current(database)
            assert _search_ids(database) == (2,)
            assert _search_ids_in(database, old_reader) == (1,)
        finally:
            old_reader.rollback()

        # NULL -> NULL and DELETE of a NULL row are true empty sparse batches.
        with database.begin("write") as txn:
            txn.execute(
                "MATCH (c:Chunk {id: 3}) SET c.embedding = $value", {"value": None}
            )
        _assert_vector_coverage_is_current(database)
        with database.begin("write") as txn:
            txn.execute("MATCH (c:Chunk {id: 3}) DELETE c")
        _assert_vector_coverage_is_current(database)

        # A rolled-back vector -> NULL must retain the old entry and discard its staged end.
        writer = database.begin("write")
        writer.execute(
            "MATCH (c:Chunk {id: 2}) SET c.embedding = $value", {"value": None}
        )
        writer.rollback()
        assert _search_ids(database) == (2,)
        assert database.verify("all").findings == ()

    with connect(root, vector_exact_scan_threshold=0) as reopened:
        assert _search_ids(reopened, expected_regime="approximate") == (2,)
        rows = reopened.begin("read")
        try:
            assert rows.execute(
                "MATCH (c:Chunk) RETURN c.id, c.embedding ORDER BY c.id"
            ).rows == (
                (1, None),
                (2, _vector(reopened, (0.0, 1.0, 0.0, 0.0))),
            )
        finally:
            rows.rollback()
        assert reopened.verify("all").findings == ()
