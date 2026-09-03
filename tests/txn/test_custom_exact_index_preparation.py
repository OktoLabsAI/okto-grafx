"""Focused manager-boundary proofs for transactional custom exact indexes."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.index.catalog import IndexGenerationState
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)

PAGE_SIZE = 512


def _seed_node_table(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, email STRING, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'one@example.test'})")
        rows.execute("CREATE (:Person {id: 2, email: 'two@example.test'})")


def _generation_files(database: object) -> frozenset[str]:
    return frozenset(
        name
        for name in database._storage.list_files("index/")
        if name.startswith("index/g_")
    )


def _prepare_email_index(database: object, transaction: object) -> object:
    return database._transactions.prepare_custom_exact_index(
        transaction._context,
        name="person_email",
        table_name="Person",
        positions=(1,),
        bucket_count=64,
        expected_cardinality=4096,
    )


def test_custom_prepare_coactivates_v1_and_cold_reopens_the_full_shadow(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_node_table(database)
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION

        transaction = database.begin("write")
        planned = _prepare_email_index(database, transaction)
        active = planned.active_generation()
        assert planned.automatic is False
        assert planned.positions == (1,)
        assert planned.expected_cardinality == 4096
        assert active is not None
        assert active.state is IndexGenerationState.ACTIVE
        assert active.bucket_count == 64
        transaction.commit()

        catalog = database._catalog.catalog
        persisted = catalog.index_definition("PERSON_EMAIL")
        assert persisted == planned
        assert catalog.format_version == CATALOG_FORMAT_VERSION
        assert {definition.name for definition in catalog.index_definitions()} >= {
            "pk_Person",
            "person_email",
        }
        assert database._storage.exists(active.file)
        assert database.verify("all").findings == ()

        # The same port remains a real catalog-v2 operation after migration becomes a no-op;
        # its second plan receives a distinct physical identity and joins existing authority.
        second = database.begin("write")
        compound = database._transactions.prepare_custom_exact_index(
            second._context,
            name="person_id_email",
            table_name="Person",
            positions=(0, 1),
            bucket_count=128,
            expected_cardinality=None,
        )
        second.commit()
        compound_active = compound.active_generation()
        assert compound_active is not None
        assert compound_active.artifact_nonce != active.artifact_nonce
        assert database._catalog.catalog.index_definition(
            "person_id_email"
        ) == compound

        duplicate = database.begin("write")
        try:
            with pytest.raises(GrafxConfigurationError) as caught:
                database._transactions.prepare_custom_exact_index(
                    duplicate._context,
                    name="PERSON_EMAIL",
                    table_name="Person",
                    positions=(0,),
                    bucket_count=64,
                    expected_cardinality=None,
                )
            assert caught.value.details["field"] == "name"
            assert duplicate._context.page_images == {}
            assert duplicate._context.read_partitions == set()
        finally:
            duplicate.rollback()

    with connect(root, page_size=PAGE_SIZE) as reopened:
        persisted = reopened._catalog.catalog.index_definition("person_email")
        generation = persisted.active_generation()
        assert generation is not None
        assert generation.artifact_nonce == active.artifact_nonce
        assert reopened._indexes.active_index(
            "person_email", catalog=reopened._catalog.catalog
        ).definition == persisted.runtime_definition(generation)
        reopened_compound = reopened._catalog.catalog.index_definition(
            "person_id_email"
        )
        reopened_compound_generation = reopened_compound.active_generation()
        assert reopened_compound_generation is not None
        assert reopened._indexes.active_index(
            "person_id_email", catalog=reopened._catalog.catalog
        ).definition == reopened_compound.runtime_definition(
            reopened_compound_generation
        )
        assert reopened.execute(
            "MATCH (person:Person) RETURN person.id, person.email"
        ).rows == (
            (1, "one@example.test"),
            (2, "two@example.test"),
        )
        assert reopened.verify("all").findings == ()


def test_custom_batch_quota_counts_migration_and_custom_before_any_staging(
    tmp_path: Path,
) -> None:
    with connect(
        tmp_path / "db",
        page_size=PAGE_SIZE,
        max_index_build_entries=3,
    ) as database:
        _seed_node_table(database)
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        before_files = _generation_files(database)
        transaction = database.begin("write")
        try:
            # Two committed rows owe one entry to the migrating PK and one to the custom
            # generation: the complete four-entry batch, not either index alone, is admitted.
            with pytest.raises(GrafxTransactionBudgetExceeded) as caught:
                _prepare_email_index(database, transaction)
            assert caught.value.details["field"] == "max_index_build_entries"
            assert caught.value.details["limit"] == 3
            assert caught.value.details["observed"] == 4
            assert transaction._context.page_images == {}
            assert transaction._context.txn_id not in (
                database._transactions._index_catalog_activation_plans
            )
            assert database._catalog.read_from_pages().serialize() == before_catalog
            assert database.wal.last_lsn == before_lsn
            assert _generation_files(database) == before_files
        finally:
            transaction.rollback()


def test_custom_request_refusals_precede_transaction_or_artifact_mutation(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE A(id INT64, value STRING, PRIMARY KEY(id))"
            )
            schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE E(FROM A TO B)")

        before_catalog = database._catalog.read_from_pages().serialize()
        before_files = _generation_files(database)
        for request, error_type, field in (
            (
                {
                    "name": "rel_value",
                    "table_name": "E",
                    "positions": (0,),
                },
                GrafxUnsupportedOperation,
                "table",
            ),
            (
                {"name": "PK_a", "table_name": "A", "positions": (1,)},
                GrafxConfigurationError,
                "name",
            ),
        ):
            transaction = database.begin("write")
            try:
                with pytest.raises(error_type) as caught:
                    database._transactions.prepare_custom_exact_index(
                        transaction._context,
                        bucket_count=64,
                        expected_cardinality=None,
                        **request,
                    )
                assert caught.value.details["field"] == field
                assert transaction._context.read_partitions == set()
                assert transaction._context.write_partitions == set()
                assert transaction._context.page_images == {}
                assert transaction._context.txn_id not in (
                    database._transactions._index_catalog_activation_plans
                )
                assert database._catalog.read_from_pages().serialize() == before_catalog
                assert _generation_files(database) == before_files
            finally:
                transaction.rollback()

        nonfresh = database.begin("write")
        try:
            nonfresh._context.note_read(7)
            with pytest.raises(GrafxTransactionStateError) as caught:
                database._transactions.prepare_custom_exact_index(
                    nonfresh._context,
                    name="by_value",
                    table_name="A",
                    positions=(1,),
                    bucket_count=64,
                    expected_cardinality=None,
                )
            assert caught.value.details["field"] == "activation_transaction"
            assert nonfresh._context.read_partitions == {7}
            assert nonfresh._context.page_images == {}
            assert _generation_files(database) == before_files
        finally:
            nonfresh.rollback()


def test_custom_plan_rollback_and_post_prepare_dml_never_publish_a_shadow(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_node_table(database)
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        before_files = _generation_files(database)

        rolled_back = database.begin("write")
        _prepare_email_index(database, rolled_back)
        assert _generation_files(database) == before_files
        rolled_back.rollback()
        assert database._catalog.read_from_pages().serialize() == before_catalog
        assert database.wal.last_lsn == before_lsn
        assert _generation_files(database) == before_files

        modified = database.begin("write")
        try:
            _prepare_email_index(database, modified)
            modified.execute(
                "CREATE (:Person {id: 3, email: 'three@example.test'})"
            )
            with pytest.raises(GrafxTransactionStateError) as caught:
                modified.commit()
            assert caught.value.details["field"] == "activation_transaction"
            assert modified._context.active is True
            assert database._catalog.read_from_pages().serialize() == before_catalog
            assert database.wal.last_lsn == before_lsn
            assert _generation_files(database) == before_files
        finally:
            modified.rollback()


def test_concurrent_target_write_wins_occ_before_custom_shadow_construction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as first:
        _seed_node_table(first)
        with connect(root, page_size=PAGE_SIZE) as second:
            transaction = first.begin("write")
            try:
                _prepare_email_index(first, transaction)
                before_files = _generation_files(first)

                with second.begin("write") as writer:
                    writer.execute(
                        "CREATE (:Person {id: 3, email: 'three@example.test'})"
                    )

                with pytest.raises(GrafxWriteConflict):
                    transaction.commit()
                assert transaction._context.active is True
                assert (
                    first._catalog.read_from_pages().format_version
                    == CATALOG_LEGACY_FORMAT_VERSION
                )
                assert _generation_files(first) == before_files
            finally:
                transaction.rollback()
