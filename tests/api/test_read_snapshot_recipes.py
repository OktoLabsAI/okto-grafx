"""RF10 executable reading recipes: consistency choice and owned cleanup, not timing."""

import pytest

from okto_grafx import CancellationToken, connect
from okto_grafx.errors import GrafxQueryCancelled


PK = "MATCH (n:Item {id: $id}) RETURN n.value"
PAGE = "MATCH (n:Item) WHERE n.id > $after RETURN n.id ORDER BY n.id LIMIT 4"


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "recipes"
    with connect(path, codec="pure") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id INT64, value INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            for identity in range(16):
                tx.execute(
                    "CREATE (:Item {id: $id, value: $value})",
                    {"id": identity, "value": identity * 10},
                )
        yield db, path


def test_related_reads_keep_the_snapshot_and_new_transaction_sees_publication(graph):
    db, path = graph
    with connect(path, codec="pure") as writer:
        with db.begin("read") as reader:
            assert reader.execute(PK, {"id": 7}).rows == ((70,),)
            with writer.begin("write") as change:
                change.execute("MATCH (n:Item {id: 7}) SET n.value = 700")
            assert reader.execute(PK, {"id": 7}).rows == ((70,),)
        assert db.transactions.open_transactions == 0
        assert db.execute(PK, {"id": 7}).rows == ((700,),)


@pytest.mark.parametrize("shared", [False, True])
def test_sixteen_primary_keys_explicitly_choose_one_or_sixteen_transactions(
    graph,
    monkeypatch,
    shared,
):
    db, _ = graph
    manager = db._transactions
    began, ended = [], []
    manager_type = type(manager)
    original_begin = manager_type.begin
    original_commit = manager_type.commit

    def begin(self, *args, **kwargs):
        transaction = original_begin(self, *args, **kwargs)
        began.append(transaction)
        return transaction

    def commit(self, transaction, *args, **kwargs):
        result = original_commit(self, transaction, *args, **kwargs)
        ended.append(transaction)
        return result

    monkeypatch.setattr(manager_type, "begin", begin)
    monkeypatch.setattr(manager_type, "commit", commit)
    if shared:
        with db.begin("read") as tx:
            results = [tx.execute(PK, {"id": key}).rows for key in range(16)]
    else:
        results = [db.execute(PK, {"id": key}).rows for key in range(16)]
    assert results == [((key * 10,),) for key in range(16)]
    assert len(began) == len(ended) == (1 if shared else 16)
    assert all(a is b for a, b in zip(began, ended, strict=True))
    assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("shared", [False, True])
def test_keyset_pages_declare_snapshot_scope_instead_of_implicit_freshness(
    graph, shared
):
    db, path = graph
    with connect(path, codec="pure") as writer:
        tx = db.begin("read") if shared else None
        try:
            executor = tx if tx is not None else db
            first = executor.execute(PAGE, {"after": -1}).rows
            assert first == ((0,), (1,), (2,), (3,))
            with writer.begin("write") as change:
                change.execute("MATCH (n:Item {id: 4}) DELETE n")
                change.execute("CREATE (:Item {id: 16, value: 160})")
            found = list(first)
            while page := executor.execute(PAGE, {"after": found[-1][0]}).rows:
                found.extend(page)
            expected = list(range(16)) if shared else [*range(4), *range(5, 17)]
            assert found == [(identity,) for identity in expected]
        finally:
            if tx is not None:
                tx.rollback()
    assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("context_manager", [False, True])
def test_materialized_rows_survive_handle_close_and_partial_cursor_releases_owner(
    graph,
    context_manager,
):
    db, path = graph
    materialized = db.execute(PK, {"id": 7})
    cursor = db.query("MATCH (n:Item) RETURN n.id").cursor(batch_size=1)
    if context_manager:
        with cursor:
            assert cursor.fetchone() == (0,)
            assert db.transactions.open_transactions == 1
    else:
        assert cursor.fetchone() == (0,)
        cursor.close()
    cursor.close()
    assert cursor.closed and cursor.fetchone() is None
    assert db.transactions.open_transactions == 0
    # The standing participant registration is deliberately not per-query ownership.
    with connect(path, codec="pure") as observer:
        coordinator = observer._transactions._coordinator
        assert coordinator.reader_horizon() is not None
        db.close()
        assert coordinator.reader_horizon() is None
    assert materialized.rows == ((70,),)


@pytest.mark.parametrize("ending", ["consumer_error", "cancel"])
def test_cleanup_preserves_primary_failure_and_releases_stream(
    graph, monkeypatch, ending
):
    db, _ = graph
    token = CancellationToken()
    cursor = db.query("MATCH (n:Item) RETURN n.id").cursor(
        batch_size=1,
        cancellation=token,
    )
    assert next(cursor) == (0,)
    raw_type = type(cursor._raw)
    original_close = raw_type.close
    closed = []

    def close_then_fail(raw):
        original_close(raw)
        closed.append(raw)
        raise RuntimeError("secondary cleanup failure")

    monkeypatch.setattr(raw_type, "close", close_then_fail)
    primary = ValueError("consumer rejected row")
    error_type = ValueError if ending == "consumer_error" else GrafxQueryCancelled
    with pytest.raises(error_type) as raised:
        with cursor:
            if ending == "consumer_error":
                raise primary
            token.cancel()
            next(cursor)
    if ending == "consumer_error":
        assert raised.value is primary
    else:
        assert raised.value.code == "query_cancelled"
    assert closed == [cursor._raw]
    assert cursor.closed and not cursor._buffer
    assert db.transactions.open_transactions == 0
    assert db.execute("RETURN 42").rows == ((42,),)
