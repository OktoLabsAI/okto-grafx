"""Registered generation nonce inventory avoids redundant v2 index opens."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.index import IndexDefinition, IndexVisibility
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.txn_manager import TransactionManager


def _v2_index(
    manager: IndexManager,
    pool: BufferPool,
    metrics: Any,
    table: TableDef,
    *,
    nonce: int,
) -> HashIndex:
    definition = IndexDefinition.on(
        table,
        name=f"person_by_name_{nonce}",
        columns=("name",),
        visibility=IndexVisibility.EXACT,
        artifact_nonce=nonce,
    )
    return manager.register(HashIndex(definition, pool, metrics))


def _catalog_with(*nonces: int) -> object:
    generations = tuple(SimpleNamespace(artifact_nonce=nonce) for nonce in nonces)
    definition = SimpleNamespace(generations=generations)
    return SimpleNamespace(index_definitions=lambda: (definition,))


def test_v2_registered_nonce_projection_does_not_reopen_indexes(
    manager: IndexManager,
    pool: BufferPool,
    metrics: Any,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _v2_index(manager, pool, metrics, person_table, nonce=41)

    def unexpected_open(self, *args, **kwargs):
        del self, args, kwargs
        raise AssertionError("catalog-v2 nonce projection reopened the index")

    monkeypatch.setattr(HashIndex, "open", unexpected_open)

    assert manager._registered_artifact_nonces() == frozenset({41})


def test_legacy_registered_definition_opens_header_for_its_nonce(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: Any,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = IndexManager(pool, heap_store, metrics, artifact_nonce=lambda: 73)
    index = manager.register(
        HashIndex(
            IndexDefinition.on(
                person_table,
                name="person_by_name",
                columns=("name",),
                visibility=IndexVisibility.EXACT,
            ),
            pool,
            metrics,
        )
    )
    calls = 0
    original = HashIndex.open

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HashIndex, "open", counted)

    assert index.definition.artifact_nonce == 0
    assert manager._registered_artifact_nonces() == frozenset({73})
    assert calls == 1


def test_legacy_header_refusal_remains_fail_closed(
    manager: IndexManager,
    pool: BufferPool,
    metrics: Any,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager.register(
        HashIndex(
            IndexDefinition.on(
                person_table,
                name="person_by_name",
                columns=("name",),
                visibility=IndexVisibility.EXACT,
            ),
            pool,
            metrics,
        )
    )
    expected = GrafxCorruptionDetected("injected legacy header refusal", field="header")

    def refuse(self, *args, **kwargs):
        del self, args, kwargs
        raise expected

    monkeypatch.setattr(HashIndex, "open", refuse)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        manager._registered_artifact_nonces()
    assert raised.value is expected


def test_transaction_inventory_preserves_catalog_and_registered_collisions(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: Any,
    person_table: TableDef,
) -> None:
    offered = iter((11, 12, 13))
    manager = IndexManager(
        pool, heap_store, metrics, artifact_nonce=lambda: next(offered)
    )
    _v2_index(manager, pool, metrics, person_table, nonce=12)
    owner = SimpleNamespace(_index_manager=manager)

    occupied = TransactionManager._index_generation_nonces(owner, _catalog_with(11))

    assert occupied == {11, 12}
    assert manager._allocate_detached_generation_nonce(occupied) == 13


def test_query_ddl_allocator_consumes_the_v2_projection_without_index_open(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: Any,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offered = iter((11, 12, 13))
    manager = IndexManager(
        pool, heap_store, metrics, artifact_nonce=lambda: next(offered)
    )
    _v2_index(manager, pool, metrics, person_table, nonce=12)
    owner = SimpleNamespace(require_indexes=lambda: manager)

    def unexpected_open(self, *args, **kwargs):
        del self, args, kwargs
        raise AssertionError("the query DDL allocator reopened a v2 index")

    monkeypatch.setattr(HashIndex, "open", unexpected_open)

    assert (
        QueryEngine._allocate_catalog_generation_nonce(owner, _catalog_with(11)) == 13
    )


def test_transaction_inventory_keeps_collision_exhaustion_refusal(
    pool: BufferPool,
    heap_store: HeapStore,
    metrics: Any,
    person_table: TableDef,
) -> None:
    calls = 0

    def occupied_nonce() -> int:
        nonlocal calls
        calls += 1
        return 12

    manager = IndexManager(pool, heap_store, metrics, artifact_nonce=occupied_nonce)
    _v2_index(manager, pool, metrics, person_table, nonce=12)
    owner = SimpleNamespace(_index_manager=manager)
    occupied = TransactionManager._index_generation_nonces(owner, _catalog_with())

    with pytest.raises(GrafxIndexError) as raised:
        manager._allocate_detached_generation_nonce(occupied)

    assert raised.value.details["field"] == "artifact_nonce"
    assert raised.value.details["attempts"] == calls == 64
