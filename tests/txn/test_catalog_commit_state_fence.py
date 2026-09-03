"""Transactional activation tests for the catalog-v2 capability fence."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FORMAT_VERSION,
    COMMIT_STATE_LEGACY_FORMAT_VERSION,
    CommitState,
)
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.wal_manager import WalManager

from txn_support import Stack, build_stack, make_page_image


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build the production WAL needed by checkpoint replay."""
    manager = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=1 << 20,
        descriptor="hash-v1;partitions_per_table=8",
    )
    manager.open()
    return manager


def _stage_catalog_v2(stack: Stack) -> object:
    """Stage, without installing, the exact v1-to-v2 catalog transition."""
    candidate = stack.catalog.read_from_pages()
    candidate.upgrade_index_catalog()
    transaction = stack.manager.begin("write")
    for page_index, image in stack.catalog.stage(candidate):
        transaction.owner._stage_page_image(
            transaction, stack.catalog.file, page_index, image
        )
    return transaction


def _commit_heap_page(stack: Stack, page_index: int) -> None:
    """Commit one unrelated physical heap image through the production protocol."""
    transaction = stack.manager.begin("write")
    transaction.owner._stage_page_image(
        transaction,
        stack.heap.file,
        page_index,
        make_page_image(stack.codec, [b"row"], page_index=page_index),
    )
    transaction.note_write(stack.manager.partition_of(1, b"row"))
    stack.manager.commit(transaction)


def test_an_unrelated_v1_commit_does_not_raise_the_capability_fence(
    stack: Stack,
) -> None:
    _commit_heap_page(stack, 3)

    assert (
        stack.catalog.read_from_pages().format_version == CATALOG_LEGACY_FORMAT_VERSION
    )
    assert (
        stack.manager.published_state().format_version
        == COMMIT_STATE_LEGACY_FORMAT_VERSION
    )


def test_catalog_v2_and_commit_state_v2_become_visible_in_one_commit(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _stage_catalog_v2(stack)
    events: list[str] = []
    wal_type = type(stack.wal)
    original_barrier = wal_type.barrier
    original_flush = BufferPool.flush
    original_catalog_read = CatalogStore.read_from_pages
    original_publish = CommitStateStore.publish

    def barrier(wal: object) -> object:
        events.append("wal_barrier")
        return original_barrier(wal)

    def flush(pool: BufferPool, file: str) -> None:
        original_flush(pool, file)
        if file == stack.catalog.file:
            events.append("catalog_flush")

    def catalog_read(store: CatalogStore) -> object:
        events.append("durable_catalog_read")
        return original_catalog_read(store)

    def publish(
        store: CommitStateStore,
        state: CommitState,
        *,
        previous: CommitState,
        previous_was_damaged: bool = False,
    ) -> None:
        events.append("state_publish")
        original_publish(
            store,
            state,
            previous=previous,
            previous_was_damaged=previous_was_damaged,
        )

    monkeypatch.setattr(wal_type, "barrier", barrier)
    monkeypatch.setattr(BufferPool, "flush", flush)
    monkeypatch.setattr(CatalogStore, "read_from_pages", catalog_read)
    monkeypatch.setattr(CommitStateStore, "publish", publish)
    events.clear()

    report = stack.manager.commit(transaction)

    durable_catalog = stack.catalog.read_from_pages()
    durable_state = stack.manager.published_state()
    assert durable_catalog.format_version == CATALOG_FORMAT_VERSION
    assert durable_state.format_version == COMMIT_STATE_FORMAT_VERSION
    assert durable_state.last_committed_lsn == report.csn
    assert events.index("wal_barrier") < events.index("catalog_flush")
    assert events.index("catalog_flush") < events.index("durable_catalog_read")
    assert events.index("durable_catalog_read") < events.index("state_publish")


def test_an_unsaved_live_v2_catalog_cannot_promote_an_unrelated_heap_commit(
    stack: Stack,
) -> None:
    stack.catalog.catalog.upgrade_index_catalog()
    assert stack.catalog.has_unsaved_changes() is True

    _commit_heap_page(stack, 3)

    assert (
        stack.catalog.read_from_pages().format_version == CATALOG_LEGACY_FORMAT_VERSION
    )
    assert (
        stack.manager.published_state().format_version
        == COMMIT_STATE_LEGACY_FORMAT_VERSION
    )
    assert stack.manager.recovery_required is False


def test_checkpoint_and_later_data_commit_preserve_the_v2_fence(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, wal_factory=_real_wal)
    stack.manager.commit(_stage_catalog_v2(stack))
    activated = stack.manager.published_state()

    stack.manager.checkpoint()
    checkpointed = stack.manager.published_state()
    assert checkpointed.format_version == COMMIT_STATE_FORMAT_VERSION
    assert checkpointed.checkpoint_lsn == activated.last_committed_lsn

    _commit_heap_page(stack, 3)
    assert stack.manager.published_state().format_version == COMMIT_STATE_FORMAT_VERSION
    assert stack.catalog.read_from_pages().format_version == CATALOG_FORMAT_VERSION
