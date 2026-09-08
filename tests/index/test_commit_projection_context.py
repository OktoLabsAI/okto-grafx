"""Injected execution-local selection must retain all commit authority boundaries."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from threading import Barrier

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxIndexError
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.runtime.scoped_value import ContextLocalValue

from .conftest import Database, TransactionDouble

# These tests deliberately inspect lexical authority, not public result data.
# ruff: noqa: SLF001


def _manager(database: Database, context: object = None) -> IndexManager:
    return IndexManager(
        database.pool, database.heap, database.metrics,
        projection_context=context,  # type: ignore[arg-type]
    )


def test_nested_scopes_restore_identity_and_revoke_on_base_exception() -> None:
    database = Database()
    context = ContextLocalValue("projection")
    manager = _manager(database, context)
    outer_txn = TransactionDouble(txn_id=501)
    inner_txn = TransactionDouble(txn_id=502)
    with manager._commit_index_projection_scope(outer_txn) as outer:
        assert context.get() is outer
        with pytest.raises(KeyboardInterrupt):
            with manager._commit_index_projection_scope(inner_txn) as inner:
                assert manager._active_commit_index_projection(inner_txn) is inner
                assert manager._active_commit_index_projection(outer_txn) is None
                copied = copy_context()
                raise KeyboardInterrupt()
        assert inner.active is False
        assert copied.run(context.get) is inner
        assert copied.run(manager._active_commit_index_projection, inner_txn) is None
        assert manager._active_commit_index_projection(outer_txn) is outer
    assert outer.active is False
    assert context.get() is None


def test_two_threads_on_one_manager_cannot_borrow_each_others_projection() -> None:
    database = Database()
    context = ContextLocalValue("projection")
    manager = _manager(database, context)
    transactions = (TransactionDouble(txn_id=503), TransactionDouble(txn_id=504))
    barrier = Barrier(2, timeout=5)

    def run(index: int) -> object:
        txn = transactions[index]
        with manager._commit_index_projection_scope(txn) as projection:
            barrier.wait()
            assert context.get() is projection
            assert manager._active_commit_index_projection(txn) is projection
            assert manager._active_commit_index_projection(transactions[1 - index]) is None
            barrier.wait()
        assert context.get() is None
        return projection

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(run, (0, 1)))
    assert first is not second
    assert context.get() is None


def test_even_a_shared_transport_cannot_grant_another_manager_authority() -> None:
    database = Database()
    context = ContextLocalValue("shared")
    first = _manager(database, context)
    second = _manager(database, context)
    txn = TransactionDouble(txn_id=505)
    with first._commit_index_projection_scope(txn) as projection:
        assert first._active_commit_index_projection(txn) is projection
        assert second._active_commit_index_projection(txn) is None


def test_async_tasks_keep_independent_bindings_on_one_thread() -> None:
    context = ContextLocalValue("same-name")
    other = ContextLocalValue("same-name")

    async def run() -> None:
        first_ready = asyncio.Event()
        second_ready = asyncio.Event()

        async def task(value: object, ready, peer) -> None:
            with context.bind(value):
                ready.set()
                await asyncio.wait_for(peer.wait(), timeout=5)
                assert context.get() is value
                assert other.get() is None
                await asyncio.sleep(0)
                assert context.get() is value
            assert context.get() is None

        await asyncio.gather(
            task(object(), first_ready, second_ready),
            task(object(), second_ready, first_ready),
        )

    asyncio.run(run())
    assert context.get() is None


def test_manual_composition_without_transport_uses_canonical_selection() -> None:
    database = Database()
    manager = _manager(database)
    txn = TransactionDouble(txn_id=506)
    with manager._commit_index_projection_scope(txn) as projection:
        assert manager._active_commit_index_projection(txn) is None
    assert projection.active is False


def test_registry_drift_still_refuses_an_active_projection() -> None:
    database = Database()
    manager = _manager(database, ContextLocalValue("projection"))
    txn = TransactionDouble(txn_id=507)
    with manager._commit_index_projection_scope(txn):
        manager.register(database.exact)
        with pytest.raises(GrafxIndexError) as refused:
            manager._active_commit_index_projection(txn)
        assert refused.value.details["field"] == "index_registry"


@pytest.mark.parametrize("phase", ["bind", "enter", "exit"])
def test_failed_host_transport_cannot_retain_active_projection(phase: str) -> None:
    database = Database()
    failure = RuntimeError("transport failed")
    retained: list[object] = []

    class Broken:
        def get(self) -> object | None:
            return retained[-1] if retained else None

        def bind(self, value: object):
            retained.append(value)
            if phase == "bind":
                raise failure
            return self.scope()

        @contextmanager
        def scope(self):
            if phase == "enter":
                raise failure
            try:
                yield
            finally:
                # Revocation must happen before even a failing reset/exit.
                assert retained[-1].active is False
                raise failure

    manager = _manager(database, Broken())
    txn = TransactionDouble(txn_id=508)
    with pytest.raises(RuntimeError) as caught:
        with manager._commit_index_projection_scope(txn):
            pass
    assert caught.value is failure
    assert retained[-1].active is False
    assert manager._active_commit_index_projection(txn) is None


@pytest.mark.parametrize("transport", [False, object(), "context"])
def test_malformed_transport_is_refused_at_composition(transport: object) -> None:
    with pytest.raises(GrafxConfigurationError) as refused:
        _manager(Database(), transport)
    assert refused.value.details["field"] == "projection_context"


def test_public_commit_uses_the_injected_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager._active_commit_index_projection
    used: list[object] = []

    def observed(self, txn):
        projection = original(self, txn)
        if projection is not None:
            used.append(projection)
        return projection

    monkeypatch.setattr(IndexManager, "_active_commit_index_projection", observed)
    with connect(tmp_path / "db") as database:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        used.clear()
        with database.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 7})")
        assert used
        assert all(item is used[0] for item in used)
        assert all(item.active is False for item in used)
        with database.begin("read") as txn:
            assert tuple(txn.execute("MATCH (p:Person) RETURN p.id").rows) == ((7,),)
