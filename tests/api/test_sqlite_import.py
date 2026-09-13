"""Real SQLite/Grafx import: source release, NULL/types, refusal and atomic rollback."""

import sqlite3
import pytest
from okto_grafx import connect, CancellationToken
from okto_grafx.errors import GrafxError, GrafxQueryBudgetExceeded, GrafxQueryCancelled
from okto_grafx.sqlite_import import read_sqlite_rows, import_sqlite, SQLiteImportLimits


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "input.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE items(id INTEGER, body TEXT, flag INTEGER, data BLOB)"
        )
        connection.executemany(
            "INSERT INTO items VALUES(?,?,?,?)",
            [(1, "a", 1, b"abc"), (2, None, 0, None)],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def options(source):
    return dict(
        allowed_root=source.parent,
        query="SELECT id, body FROM items ORDER BY id",
        columns=("id", "body"),
        types=("INT64", "STRING"),
    )


def test_types_and_closed_source(source):
    result = read_sqlite_rows(source, **options(source))
    assert result == ({"id": 1, "body": "a"}, {"id": 2, "body": None})
    result = read_sqlite_rows(
        source,
        allowed_root=source.parent,
        query="SELECT flag,data FROM items ORDER BY id",
        columns=("flag", "data"),
        types=("BOOL", "BYTES"),
    )
    assert result == ({"flag": True, "data": b"abc"}, {"flag": False, "data": None})
    with sqlite3.connect(source, timeout=0) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("INSERT INTO items(id) VALUES(3)")


def test_atomic_staging_and_failure(source, tmp_path):
    statement = "CREATE (:N {id:$id,body:$body})"
    with connect(tmp_path / "grafx") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,body STRING,PRIMARY KEY(id))")
        with db.begin() as tx:
            import_sqlite(tx, statement, source, **options(source))
        assert db.execute("MATCH (n:N) RETURN count(*)").rows == ((2,),)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:3})")
            bad = options(source)
            bad["query"] = "SELECT 4 AS id, body FROM items"  # duplicate second row
            with pytest.raises(GrafxError):
                import_sqlite(tx, statement, source, **bad)
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == (
            (1,),
            (2,),
            (3,),
        )


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM items",
        "PRAGMA user_version=9",
        "ATTACH DATABASE ':memory:' AS other",
        "SELECT load_extension(?)",
        "SELECT randomblob(100000000)",
        "SELECT id FROM items; DELETE FROM items",
    ],
)
def test_read_only_refusals(source, query):
    opts = options(source)
    opts["query"] = query
    with pytest.raises(GrafxError):
        read_sqlite_rows(source, **opts)
    assert len(read_sqlite_rows(source, **options(source))) == 2


def test_bounds_null_and_parameters(source):
    for limits in [
        SQLiteImportLimits(max_rows=1),
        SQLiteImportLimits(max_bytes=64),
        SQLiteImportLimits(max_work=1),
    ]:
        with pytest.raises(GrafxError):
            read_sqlite_rows(source, limits=limits, **options(source))
    opts = options(source)
    opts["query"] = "SELECT id,body FROM items WHERE id=?"
    assert read_sqlite_rows(source, parameters=(2,), **opts) == (
        {"id": 2, "body": None},
    )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        read_sqlite_rows(source, cancellation=token, **options(source))
    opts["query"] = (
        "WITH RECURSIVE x(id) AS (SELECT 1 UNION ALL SELECT id+1 FROM x WHERE id<10000000) SELECT sum(id) AS id, NULL AS body FROM x"
    )
    with pytest.raises(GrafxQueryBudgetExceeded):
        read_sqlite_rows(source, limits=SQLiteImportLimits(max_work=1000), **opts)


def test_missing_and_outside_root(source, tmp_path):
    with pytest.raises(GrafxError):
        read_sqlite_rows("missing.sqlite", **options(source))
    assert not (tmp_path / "missing.sqlite").exists()
    opts = options(source)
    opts["allowed_root"] = tmp_path / "different"
    opts["allowed_root"].mkdir()
    with pytest.raises(GrafxError):
        read_sqlite_rows(source, **opts)
