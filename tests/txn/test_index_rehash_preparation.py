"""Focused manager-boundary proofs for growth-only exact-index rehash."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)
from okto_grafx.domain.index.catalog import IndexGenerationState

PAGE_SIZE = 512


def _seed_rehashable_index(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, email STRING, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'one@example.test'})")
        rows.execute("CREATE (:Person {id: 2, email: 'two@example.test'})")
    with database.begin("write") as creation:
        database._transactions.prepare_custom_exact_index(
            creation._context,
            name="person_email",
            table_name="Person",
            positions=(1,),
            bucket_count=64,
            expected_cardinality=None,
        )


def _generation_files(database: object) -> frozenset[str]:
    return frozenset(
        name
        for name in database._storage.list_files("index/")
        if name.startswith("index/g_")
    )


def _prepare_email_rehash(database: object, transaction: object) -> object:
    return database._transactions.prepare_index_rehash(
        transaction._context,
        name="person_email",
        bucket_count=128,
        expected_cardinality=None,
    )


def test_rehash_prepare_is_non_publishing_and_rollback_discards_its_plan(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_rehashable_index(database)
        persisted = database._catalog.catalog.index_definition("person_email")
        previous = persisted.active_generation()
        assert previous is not None
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        before_files = _generation_files(database)

        transaction = database.begin("write")
        planned = _prepare_email_rehash(database, transaction)
        active = planned.active_generation()
        assert active is not None
        assert active.state is IndexGenerationState.ACTIVE
        assert active.bucket_count == 128
        assert active.artifact_nonce != previous.artifact_nonce
        assert (
            planned.generation(previous.artifact_nonce).state
            is IndexGenerationState.STALE
        )

        # Preparation may stage catalog pages only inside this transaction.  It cannot make
        # either catalog or physical-generation authority externally reachable before commit.
        assert transaction._context.page_images
        assert transaction._context.txn_id in (
            database._transactions._index_catalog_activation_plans
        )
        assert database._catalog.read_from_pages().serialize() == before_catalog
        assert database.wal.last_lsn == before_lsn
        assert _generation_files(database) == before_files

        transaction.rollback()
        assert transaction._context.txn_id not in (
            database._transactions._index_catalog_activation_plans
        )
        assert database._catalog.read_from_pages().serialize() == before_catalog
        assert database.wal.last_lsn == before_lsn
        assert _generation_files(database) == before_files


def test_rehash_plan_seal_refuses_later_row_mutation_without_building_shadow(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_rehashable_index(database)
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        before_files = _generation_files(database)

        transaction = database.begin("write")
        try:
            _prepare_email_rehash(database, transaction)
            transaction.execute("CREATE (:Person {id: 3, email: 'three@example.test'})")
            with pytest.raises(GrafxTransactionStateError) as caught:
                transaction.commit()
            assert caught.value.details["field"] == "activation_transaction"
            assert transaction._context.active is True
            assert database._catalog.read_from_pages().serialize() == before_catalog
            assert database.wal.last_lsn == before_lsn
            assert _generation_files(database) == before_files
        finally:
            transaction.rollback()


def test_concurrent_target_writer_wins_occ_before_rehash_shadow_construction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as first:
        _seed_rehashable_index(first)
        before_catalog = first._catalog.read_from_pages().serialize()
        before_files = _generation_files(first)

        with connect(root, page_size=PAGE_SIZE) as second:
            transaction = first.begin("write")
            try:
                _prepare_email_rehash(first, transaction)

                with second.begin("write") as writer:
                    writer.execute(
                        "CREATE (:Person {id: 3, email: 'three@example.test'})"
                    )

                with pytest.raises(GrafxWriteConflict):
                    transaction.commit()
                assert transaction._context.active is True
                assert first._catalog.read_from_pages().serialize() == before_catalog
                assert _generation_files(first) == before_files
            finally:
                transaction.rollback()


def test_rehash_quota_refuses_before_catalog_staging_or_generation_creation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as setup:
        _seed_rehashable_index(setup)

    with connect(
        root,
        page_size=PAGE_SIZE,
        max_index_build_entries=1,
    ) as database:
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        before_files = _generation_files(database)
        transaction = database.begin("write")
        try:
            with pytest.raises(GrafxTransactionBudgetExceeded) as caught:
                _prepare_email_rehash(database, transaction)
            assert caught.value.details["field"] == "max_index_build_entries"
            assert caught.value.details["limit"] == 1
            assert caught.value.details["observed"] == 2
            assert transaction._context.page_images == {}
            assert transaction._context.txn_id not in (
                database._transactions._index_catalog_activation_plans
            )
            assert database._catalog.read_from_pages().serialize() == before_catalog
            assert database.wal.last_lsn == before_lsn
            assert _generation_files(database) == before_files
        finally:
            transaction.rollback()
