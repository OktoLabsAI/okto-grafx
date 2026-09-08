"""Read cancellation/deadline lifecycle, including buffered rows and blocking operators."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path

import pytest

from okto_grafx import CancellationToken, connect
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxQueryCancelled,
    GrafxQueryDeadlineExceeded,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.query_engine import _Context
from okto_grafx.domain.query.control import _ReadControl
import okto_grafx.engine.database as facade
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory


@pytest.fixture
def database():
    with connect(":memory:", page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.executemany("CREATE (:N {id: $id})", ({"id": i} for i in range(180)))
        yield db


def _no_readers(db):
    assert not db._transactions._open
    assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((180,),)


@pytest.mark.parametrize("door", ["execute", "cursor"])
def test_precancelled_does_not_open_snapshot(database, door):
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.cancelled
    with pytest.raises(GrafxQueryCancelled) as failure:
        if door == "execute":
            database.execute("MATCH (n:N) RETURN n.id", cancellation=token)
        else:
            database.query("MATCH (n:N) RETURN n.id").cursor(cancellation=token)
    assert failure.value.code == "query_cancelled"
    assert not failure.value.retryable
    _no_readers(database)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:N) RETURN n.id",
        "MATCH (n:N) WHERE n.id < 0 RETURN n.id",
        "MATCH (n:N) RETURN n.id ORDER BY n.id DESC",
        "MATCH (n:N) RETURN count(n)",
        "MATCH (n:N), (m:N) RETURN n.id, m.id",
    ],
)
@pytest.mark.parametrize("cursor", [False, True])
def test_cancel_during_operator_work_cleans_up(database, monkeypatch, query, cursor):
    token = CancellationToken()
    original = _Context.count
    calls = 0

    def count(self, name, amount=1):
        nonlocal calls
        calls += 1
        if calls == 5:
            token.cancel()
        return original(self, name, amount)

    monkeypatch.setattr(_Context, "count", count)
    with pytest.raises(GrafxQueryCancelled):
        if cursor:
            stream = database.query(query).cursor(cancellation=token)
            tuple(stream)
        else:
            database.execute(query, cancellation=token)
    if cursor:
        assert stream.closed
    _no_readers(database)


@pytest.mark.parametrize("consume", ["next", "fetchmany", "fetchone"])
def test_cancellation_discards_already_buffered_rows(database, consume):
    token = CancellationToken()
    cursor = database.query("MATCH (n:N) RETURN n.id").cursor(
        batch_size=20, cancellation=token
    )
    next(cursor)
    assert cursor._buffer
    token.cancel()
    with pytest.raises(GrafxQueryCancelled):
        next(cursor) if consume == "next" else getattr(cursor, consume)()
    assert cursor.closed and not cursor._buffer
    _no_readers(database)


class ManualClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


def test_cursor_deadline_includes_consumer_idle_time(database):
    clock = ManualClock()
    database._clock = clock
    cursor = database.query("MATCH (n:N) RETURN n.id").cursor(timeout_seconds=2)
    next(cursor)
    clock.now += 2
    with pytest.raises(GrafxQueryDeadlineExceeded) as failure:
        next(cursor)
    assert failure.value.code == "query_deadline_exceeded"
    assert cursor.closed
    _no_readers(database)


def test_deadline_expires_inside_scan(database, monkeypatch):
    clock = ManualClock()
    database._clock = clock
    original = _Context.count

    def count(self, name, amount=1):
        clock.now += 0.1
        return original(self, name, amount)

    monkeypatch.setattr(_Context, "count", count)
    with pytest.raises(GrafxQueryDeadlineExceeded):
        database.execute("MATCH (n:N) WHERE n.id < 0 RETURN n.id", timeout_seconds=1)
    _no_readers(database)


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, float("nan"), float("inf"), "1", object(), 10**1000]
)
def test_bad_deadline_rejected_before_begin(database, value):
    with pytest.raises(GrafxConfigurationError):
        database.execute("RETURN 1", timeout_seconds=value)
    assert not database._transactions._open


def test_explicit_read_transaction_remains_owned_by_caller(database):
    token = CancellationToken()
    with database.begin("read") as tx:
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            tx.execute("RETURN 1", cancellation=token)
        assert tx.active
        assert tx.execute("RETURN 2").rows == ((2,),)
    _no_readers(database)


def test_write_transaction_refuses_controls_before_any_write(database):
    token = CancellationToken()
    with database.begin("write") as tx:
        with pytest.raises(GrafxUnsupportedOperation):
            tx.execute("CREATE (:N {id: 900})", cancellation=token)
        with pytest.raises(GrafxUnsupportedOperation):
            tx.execute("RETURN 1", timeout_seconds=1)
        tx.execute("CREATE (:N {id: 900})")
        token.cancel()
    assert database.execute("MATCH (n:N {id: 900}) RETURN n.id").rows == ((900,),)


def test_native_engine_also_refuses_write_control(database):
    with database.begin("write") as tx:
        control = _ReadControl(database._clock, 10, None)
        with pytest.raises(GrafxUnsupportedOperation):
            database._queries.execute("CREATE (:N {id: 900})", tx._context, read_control=control)
    _no_readers(database)


@pytest.mark.parametrize("door", ["execute", "transaction", "cursor"])
def test_deadline_checked_after_public_result_copy(database, monkeypatch, door):
    clock = ManualClock()
    database._clock = clock
    original = facade._query_result_view
    if door == "cursor":
        cursor = database.query("RETURN 1").cursor(timeout_seconds=1)

    def detach(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now += 2
        return result

    monkeypatch.setattr(facade, "_query_result_view", detach)
    with pytest.raises(GrafxQueryDeadlineExceeded):
        if door == "cursor":
            cursor.fetchone()
        elif door == "transaction":
            with database.begin("read") as tx:
                tx.execute("RETURN 1", timeout_seconds=1)
        else:
            database.execute("RETURN 1", timeout_seconds=1)
    if door == "cursor":
        assert cursor.closed
    assert not database._transactions._open


def test_cancellation_from_other_thread_is_observed_by_consumer(database, monkeypatch):
    token = CancellationToken()
    reached, resume = Event(), Event()
    original = _Context.count

    def count(self, name, amount=1):
        if self.read_control is not None and not reached.is_set():
            reached.set()
            assert resume.wait(5)
        return original(self, name, amount)

    monkeypatch.setattr(_Context, "count", count)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(
            database.execute, "MATCH (n:N) RETURN n.id", cancellation=token
        )
        try:
            assert reached.wait(5)
            token.cancel()
        finally:
            resume.set()
        with pytest.raises(GrafxQueryCancelled):
            future.result(timeout=5)
    _no_readers(database)


def test_default_read_never_reads_control_clock(database):
    class RefusingClock:
        def monotonic(self):
            raise AssertionError("default read consulted control clock")

    database._clock = RefusingClock()
    _no_readers(database)


def test_reusable_query_gets_fresh_deadline_per_cursor(database):
    clock = ManualClock()
    database._clock = clock
    query = database.query("RETURN 1")
    with query.cursor(timeout_seconds=1) as cursor:
        assert tuple(cursor) == ((1,),)
    clock.now += 100
    with query.cursor(timeout_seconds=1) as cursor:
        assert tuple(cursor) == ((1,),)


@pytest.mark.parametrize("cursor", [False, True])
def test_cancel_removes_real_spill_files(database, monkeypatch, tmp_path, cursor):
    database._queries._query_memory_budget_bytes = 2048
    database._queries._query_spill = LocalQuerySpillFactory(str(tmp_path))
    token = CancellationToken()
    original = _Context.count
    workspaces = []
    open_workspace = LocalQuerySpillFactory.open

    def opened(self, budget):
        workspace = open_workspace(self, budget)
        workspaces.append(workspace)
        return workspace

    calls = 0

    def count(self, name, amount=1):
        nonlocal calls
        calls += 1
        if calls == 80:
            token.cancel()
        return original(self, name, amount)

    monkeypatch.setattr(LocalQuerySpillFactory, "open", opened)
    monkeypatch.setattr(_Context, "count", count)
    with pytest.raises(GrafxQueryCancelled):
        text = "MATCH (n:N) RETURN n.id ORDER BY n.id DESC"
        if cursor:
            with database.query(text).cursor(cancellation=token) as rows:
                tuple(rows)
        else:
            database.execute(text, cancellation=token)
    assert workspaces and any(workspace._next_run > 0 for workspace in workspaces)
    assert all(
        workspace._closed and not Path(workspace._temporary.name).exists()
        for workspace in workspaces
    )
    _no_readers(database)


@pytest.mark.parametrize("token", [True, object(), lambda: True])
def test_arbitrary_cancellation_callbacks_are_not_executed(database, token):
    with pytest.raises(GrafxConfigurationError):
        database.execute("RETURN 1", cancellation=token)
    _no_readers(database)


def test_controlled_reader_does_not_block_independent_writer(tmp_path, monkeypatch):
    path = str(tmp_path / "graph")
    with connect(path) as reader:
        with reader.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id: 1})")
        with connect(path) as writer:
            reached, resume = Event(), Event()
            token = CancellationToken()
            original = _Context.count

            def count(self, name, amount=1):
                if self.read_control is not None and not reached.is_set():
                    reached.set()
                    assert resume.wait(10)
                return original(self, name, amount)

            monkeypatch.setattr(_Context, "count", count)
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(
                    reader.execute, "MATCH (n:N) RETURN n.id", cancellation=token
                )
                try:
                    assert reached.wait(10)
                    with writer.begin("write") as tx:
                        tx.execute("CREATE (:N {id: 2})")
                    token.cancel()
                finally:
                    resume.set()
                with pytest.raises(GrafxQueryCancelled):
                    future.result(timeout=10)
            assert reader.execute("MATCH (n:N) RETURN count(n)").rows == ((2,),)
