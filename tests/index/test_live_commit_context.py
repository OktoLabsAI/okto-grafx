"""Physical live authority remains execution-local after moving context transport out."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.index_manager import HashIndex, IndexManager, IndexStore
from okto_grafx.runtime.scoped_value import ContextLocalValue

from .conftest import Database, TransactionDouble
from .test_live_hot_commit_batch import COMMIT_LSN, _colliding_keys, _stage_inserts

# ruff: noqa: SLF001


def test_transport_reference_does_not_carry_authority_to_another_thread(monkeypatch) -> None:
    database = Database(budget_pages=128)
    context = ContextLocalValue("live")
    txn = TransactionDouble(txn_id=701)
    refs = _stage_inserts(database.exact, txn, _colliding_keys(database.exact, 4))

    def refuse_hot(*args, **kwargs):
        raise AssertionError("a different thread borrowed physical write authority")

    monkeypatch.setattr(IndexStore, "_prepare_live_hot_buckets", refuse_hot)

    class Manager(IndexManager):
        def commit(self, txn, csn):
            authority = context.get()
            authority.store = database.exact
            assert authority.active

            def in_thread():
                assert context.get() is None
                return database.exact._commit_with_context(txn, csn, _live_context=context)

            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(in_thread).result(timeout=5)

    manager = Manager(database.pool, database.heap, database.metrics, live_commit_context=context)
    assert manager._commit_under_write_authority(txn, COMMIT_LSN) == 4
    assert {entry.ref for entry in database.exact.walk()} == set(refs)
    assert context.get() is None


def test_nested_manager_overrides_restore_and_revoke_their_own_scope() -> None:
    database = Database()
    context = ContextLocalValue("live")
    outer_txn = TransactionDouble(txn_id=702)
    inner_txn = TransactionDouble(txn_id=703)
    captured = []

    class Manager(IndexManager):
        def commit(self, txn, csn):
            scope = context.get()
            captured.append((scope, copy_context()))
            assert scope.manager is self and scope.txn is txn
            if txn is outer_txn:
                with pytest.raises(KeyboardInterrupt):
                    self._commit_under_write_authority(inner_txn, csn)
                assert context.get() is scope and scope.active
                return 0
            raise KeyboardInterrupt()

    manager = Manager(database.pool, database.heap, database.metrics, live_commit_context=context)
    assert manager._commit_under_write_authority(outer_txn, COMMIT_LSN) == 0
    for scope, copied in captured:
        assert copied.run(context.get) is scope
        assert scope.active is False and scope.store is None
    assert context.get() is None


@pytest.mark.parametrize("phase", ["bind", "enter", "exit"])
def test_transport_failure_revokes_physical_authority(phase) -> None:
    database = Database()
    captured = []
    failure = RuntimeError("physical context transport failed")

    class Broken:
        def get(self):
            return captured[-1] if captured else None

        def bind(self, value):
            captured.append(value)
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
                assert not captured[-1].active
                assert captured[-1].store is None
                raise failure

    class Manager(IndexManager):
        def commit(self, txn, csn):
            return 0

    manager = Manager(database.pool, database.heap, database.metrics, live_commit_context=Broken())
    with pytest.raises(RuntimeError) as caught:
        manager._commit_under_write_authority(TransactionDouble(txn_id=704), COMMIT_LSN)
    assert caught.value is failure
    assert not captured[-1].active and captured[-1].store is None


def test_custom_store_override_keeps_its_two_argument_contract_and_scalar_checks(monkeypatch) -> None:
    database = Database(budget_pages=128)
    calls = []

    class Custom(HashIndex):
        def commit(self, txn, csn):
            calls.append((txn, csn))
            return super().commit(txn, csn)

    store = Custom(database.exact.definition, database.pool, database.metrics)
    manager = IndexManager(
        database.pool, database.heap, database.metrics,
        live_commit_context=ContextLocalValue("live"),
    )
    manager.register(store)
    txn = TransactionDouble(txn_id=705)
    refs = _stage_inserts(store, txn, _colliding_keys(store, 4))

    def refuse_hot(*args, **kwargs):
        raise AssertionError("custom commit received native-only physical authority")

    monkeypatch.setattr(IndexStore, "_prepare_live_hot_buckets", refuse_hot)
    assert manager._commit_under_write_authority(txn, COMMIT_LSN) == 4
    assert calls == [(txn, COMMIT_LSN)]
    assert {entry.ref for entry in store.walk()} == set(refs)


def test_missing_transport_preserves_manual_scalar_commit(monkeypatch) -> None:
    database = Database(budget_pages=128)
    manager = IndexManager(database.pool, database.heap, database.metrics)
    manager.register(database.exact)
    txn = TransactionDouble(txn_id=706)
    refs = _stage_inserts(database.exact, txn, _colliding_keys(database.exact, 4))

    def refuse_hot(*args, **kwargs):
        raise AssertionError("omitted transport granted live authority")

    monkeypatch.setattr(IndexStore, "_prepare_live_hot_buckets", refuse_hot)
    assert manager._commit_under_write_authority(txn, COMMIT_LSN) == 4
    assert {entry.ref for entry in database.exact.walk()} == set(refs)


def test_callable_spoofing_method_metadata_is_not_a_native_commit(monkeypatch) -> None:
    database = Database(budget_pages=128)

    class Custom(HashIndex):
        pass

    store = Custom(database.exact.definition, database.pool, database.metrics)
    original = store.commit
    calls = []

    class PretendMethod:
        def __init__(self):
            self.__self__ = store
            self.__func__ = IndexStore.commit

        def __call__(self, *args, **kwargs):
            assert not kwargs, "metadata spoofing granted the private context argument"
            calls.append(args)
            return original(*args)

    store.commit = PretendMethod()
    manager = IndexManager(
        database.pool, database.heap, database.metrics,
        live_commit_context=ContextLocalValue("live"),
    )
    manager.register(store)
    txn = TransactionDouble(txn_id=707)
    _stage_inserts(store, txn, _colliding_keys(store, 4))

    def refuse_hot(*args, **kwargs):
        raise AssertionError("spoofed method received hot authority")

    monkeypatch.setattr(IndexStore, "_prepare_live_hot_buckets", refuse_hot)
    assert manager._commit_under_write_authority(txn, COMMIT_LSN) == 4
    assert calls == [(txn, COMMIT_LSN)]


def test_invalid_transport_is_refused() -> None:
    database = Database()
    with pytest.raises(GrafxConfigurationError) as refused:
        IndexManager(database.pool, database.heap, database.metrics, live_commit_context=False)
    assert refused.value.details["field"] == "live_commit_context"


def test_public_commit_still_prepares_native_live_batches(monkeypatch) -> None:
    prepared = []
    original = IndexStore._prepare_live_hot_buckets

    def observed(self, staged):
        prepared.append(self.name)
        return original(self, staged)

    monkeypatch.setattr(IndexStore, "_prepare_live_hot_buckets", observed)
    with connect(":memory:") as database:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        prepared.clear()
        with database.begin("write") as txn:
            for value in range(8):
                txn.execute("CREATE (:Person {id: $id})", {"id": value})
        assert prepared
        with database.begin("read") as txn:
            assert tuple(txn.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows) == tuple(
                (value,) for value in range(8)
            )
