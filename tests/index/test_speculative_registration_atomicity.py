"""Atomicity regressions for speculative index registry publication."""

from __future__ import annotations

import pytest

from okto_grafx.engine.index_manager import HashIndex, IndexManager, IndexStore

from .conftest import Database, TransactionDouble, exact_definition


def test_failed_post_registration_advance_leaves_no_unjournaled_registry_claim(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A physical file may remain orphaned, but no process-local owner may escape failure."""

    manager = IndexManager(database.pool, database.heap, database.metrics)
    candidate = HashIndex(
        exact_definition(database.table, name="post_registration_failure"),
        database.pool,
        database.metrics,
    )
    failure = RuntimeError("advance coverage sentinel")
    original_advance = IndexStore.advance_built_through

    def fail_candidate_advance(self: IndexStore, lsn: int) -> None:
        if self is candidate:
            raise failure
        original_advance(self, lsn)

    monkeypatch.setattr(IndexStore, "advance_built_through", fail_candidate_advance)

    with pytest.raises(RuntimeError) as raised:
        manager.register_speculative(candidate, complete_through=7)

    assert raised.value is failure
    assert manager.indexes() == ()
    assert manager._artifact_claims == {}
    assert manager._heap_cache_certificates == {}
    assert database.device.exists(candidate.file)
    assert manager.rollback(TransactionDouble(txn_id=91)) == 0
    assert manager.indexes() == ()
    assert manager._artifact_claims == {}


def test_failed_post_register_generation_proof_discards_the_unclaimed_publication(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between successful registration and claim creation owns no local state."""

    manager = IndexManager(database.pool, database.heap, database.metrics)
    candidate = HashIndex(
        exact_definition(database.table, name="post_register_generation_failure"),
        database.pool,
        database.metrics,
    )
    failure = RuntimeError("generation proof sentinel")
    original_register = IndexManager.register
    original_fresh = IndexStore._fresh_certificate
    armed = False
    fired = False
    published_pages: tuple[bytes, ...] | None = None

    def arm_after_successful_register(
        self: IndexManager, index: IndexStore, **options: object
    ) -> IndexStore:
        nonlocal armed, published_pages
        registered = original_register(self, index, **options)
        if self is manager and index is candidate:
            published_pages = tuple(
                database.device.raw_page(candidate.file, page_index)
                for page_index in range(database.device.page_count(candidate.file))
            )
            armed = True
        return registered

    def fail_first_post_register_proof(self: IndexStore):
        nonlocal fired
        if self is candidate and armed and not fired:
            fired = True
            raise failure
        return original_fresh(self)

    monkeypatch.setattr(IndexManager, "register", arm_after_successful_register)
    monkeypatch.setattr(
        IndexStore, "_fresh_certificate", fail_first_post_register_proof
    )

    with pytest.raises(RuntimeError) as raised:
        manager.register_speculative(candidate, complete_through=7)

    assert raised.value is failure
    assert fired is True
    assert published_pages is not None
    assert manager.indexes() == ()
    assert manager._artifact_claims == {}
    assert manager._heap_cache_certificates == {}
    assert (
        tuple(
            database.device.raw_page(candidate.file, page_index)
            for page_index in range(database.device.page_count(candidate.file))
        )
        == published_pages
    )

    assert manager.rollback(TransactionDouble(txn_id=92)) == 0
    assert manager.indexes() == ()
    assert manager._artifact_claims == {}
    assert manager._heap_cache_certificates == {}
    assert (
        tuple(
            database.device.raw_page(candidate.file, page_index)
            for page_index in range(database.device.page_count(candidate.file))
        )
        == published_pages
    )
