"""Public activation of catalog-v2 identity indexes.

These tests keep the activation door honest at its two durable boundaries: the catalog must
publish one complete generation authority, and the heap rows that authority was derived from
must keep answering through both the live handle and a cold reopen.  Activation is explicit so
ordinary legacy databases remain byte-compatible until their owner chooses this transition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxIndexError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.domain.index import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_LEGACY_FORMAT_VERSION

PAGE_SIZE = 512


def _seed_two_endpoint_graph(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with database.begin("write") as rows:
        rows.execute("CREATE (:A {id: 1})")
        rows.execute("CREATE (:A {id: 2})")
        rows.execute("CREATE (:B {id: 7})")
        rows.execute("CREATE (:B {id: 8})")
        rows.execute("MATCH (a:A {id: 1}), (b:B {id: 7}) CREATE (a)-[:E {w: 17}]->(b)")
        rows.execute("MATCH (a:A {id: 2}), (b:B {id: 8}) CREATE (a)-[:E {w: 28}]->(b)")


def _graph_rows(database: object) -> tuple[tuple[object, ...], ...]:
    result = database.execute("MATCH (a:A)-[e:E]->(b:B) RETURN a.id, b.id, e.w")
    return tuple(sorted(result.rows))


def _storage_fingerprint(database: object) -> tuple[tuple[str, int], ...]:
    snapshot = database.storage
    return tuple((item.name, item.size_bytes) for item in snapshot.files)


def _assert_complete_v2_authority(database: object) -> None:
    catalog = database._catalog.catalog
    table_a = catalog.table("A")
    table_b = catalog.table("B")
    expected_names = {
        "pk_A",
        "pk_B",
        "ef_E",
        "et_E",
        identity_index_name(table_a.table_id),
        identity_index_name(table_b.table_id),
    }

    assert catalog.format_version == CATALOG_FORMAT_VERSION
    assert catalog.required_capabilities() == (
        IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    )
    definitions = catalog.index_definitions()
    assert {definition.name for definition in definitions} == expected_names

    generations = tuple(
        generation
        for definition in definitions
        for generation in definition.generations
    )
    assert len(generations) == len(definitions)
    assert all(
        generation.state is IndexGenerationState.ACTIVE for generation in generations
    )
    nonces = tuple(generation.artifact_nonce for generation in generations)
    assert len(set(nonces)) == len(nonces)
    assert all(database.storage.exists(generation.file) for generation in generations)


def test_v1_activation_preserves_graph_and_is_idempotent_across_reopen(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    expected_rows = ((1, 7, 17), (2, 8, 28))

    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        assert _graph_rows(database) == expected_rows

        assert database.ensure_identity_indexes() is None
        _assert_complete_v2_authority(database)
        assert _graph_rows(database) == expected_rows

        same_handle_before = (
            database.wal.last_lsn,
            _storage_fingerprint(database),
        )
        assert database.ensure_identity_indexes() is None
        assert (
            database.wal.last_lsn,
            _storage_fingerprint(database),
        ) == same_handle_before

    with connect(root, page_size=PAGE_SIZE) as reopened:
        _assert_complete_v2_authority(reopened)
        assert _graph_rows(reopened) == expected_rows
        cold_before = (reopened.wal.last_lsn, _storage_fingerprint(reopened))
        assert reopened.ensure_identity_indexes() is None
        assert (reopened.wal.last_lsn, _storage_fingerprint(reopened)) == cold_before


def test_self_loop_schema_builds_one_identity_index_for_the_shared_endpoint(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE E(FROM A TO A, w INT64)")
        with database.begin("write") as rows:
            rows.execute("CREATE (:A {id: 1})")
            rows.execute("CREATE (:A {id: 2})")
            rows.execute(
                "MATCH (a:A {id: 1}), (b:A {id: 2}) CREATE (a)-[:E {w: 12}]->(b)"
            )

        database.ensure_identity_indexes()

        catalog = database._catalog.catalog
        rid_name = identity_index_name(catalog.table("A").table_id)
        definitions = catalog.index_definitions()
        assert tuple(
            definition.name for definition in definitions if definition.name == rid_name
        ) == (rid_name,)
        assert {definition.name for definition in definitions} == {
            "pk_A",
            "ef_E",
            "et_E",
            rid_name,
        }
        assert database.execute(
            "MATCH (a:A)-[e:E]->(b:A) RETURN a.id, b.id, e.w"
        ).rows == ((1, 2, 12),)


def test_read_only_refuses_activation_even_when_v2_is_already_complete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        database.ensure_identity_indexes()
        database.checkpoint()

    with connect(root, page_size=PAGE_SIZE, read_only=True) as reader:
        before = (
            reader.wal.last_lsn,
            reader.transactions.published_state(),
            reader._catalog.catalog.serialize(),
            _storage_fingerprint(reader),
        )
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            reader.ensure_identity_indexes()
        assert refused.value.details["field"] == "read_only"
        assert (
            reader.wal.last_lsn,
            reader.transactions.published_state(),
            reader._catalog.catalog.serialize(),
            _storage_fingerprint(reader),
        ) == before


def test_failed_first_shadow_build_keeps_v1_and_retry_uses_new_nonces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        before_catalog = database._catalog.read_from_pages().serialize()
        before_state = database._transactions.published_state()
        before_lsn = database.wal.last_lsn
        manager_type = type(database._indexes)
        original_build = manager_type._build_detached_exact_generation
        failed_generation: list[tuple[int, str]] = []

        def fail_after_first_complete_shadow(
            manager: object,
            definition: object,
            through_lsn: int,
        ) -> object:
            built = original_build(manager, definition, through_lsn)
            if not failed_generation:
                failed_generation.append(
                    (
                        int(getattr(definition, "artifact_nonce")),
                        str(getattr(definition, "file")),
                    )
                )
                raise GrafxIndexError("injected failure after first detached build")
            return built

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            fail_after_first_complete_shadow,
        )

        with pytest.raises(GrafxIndexError, match="injected failure"):
            database.ensure_identity_indexes()

        failed_nonce, failed_file = failed_generation[0]
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        assert database._catalog.read_from_pages().serialize() == before_catalog
        assert database._transactions.published_state() == before_state
        assert before_state.format_version == COMMIT_STATE_LEGACY_FORMAT_VERSION
        assert database.wal.last_lsn == before_lsn
        assert database._transactions.open_transactions == 0
        assert database._storage.exists(failed_file), (
            "the unreachable shadow may remain orphaned"
        )

        database.ensure_identity_indexes()

        _assert_complete_v2_authority(database)
        active_nonces = {
            generation.artifact_nonce
            for logical in database._catalog.catalog.index_definitions()
            for generation in logical.generations
            if generation.state is IndexGenerationState.ACTIVE
        }
        assert failed_nonce not in active_nonces
        assert database._storage.exists(failed_file)
        assert database._transactions.open_transactions == 0


def test_missing_active_identity_file_is_not_recreated_by_ensure(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        database.ensure_identity_indexes()
        catalog = database._catalog.catalog
        identity_name = identity_index_name(catalog.table("A").table_id)
        active = catalog.index_definition(identity_name).active_generation()
        assert active is not None
        missing_file = active.file
        missing_nonce = active.artifact_nonce

        database._pool.invalidate(missing_file)
        database._storage.remove(missing_file)
        assert not database._storage.exists(missing_file)
        before_catalog = catalog.serialize()
        before_state = database._transactions.published_state()
        before_lsn = database.wal.last_lsn

        with pytest.raises(GrafxIndexError) as refused:
            database.ensure_identity_indexes()
        assert refused.value.details["field"] == "file"
        assert refused.value.details["file"] == missing_file
        assert refused.value.details["artifact_nonce"] == missing_nonce

        persisted = database._catalog.read_from_pages()
        persisted_active = persisted.index_definition(identity_name).active_generation()
        assert persisted_active is not None
        assert persisted_active.artifact_nonce == missing_nonce
        assert persisted.serialize() == before_catalog
        assert database._transactions.published_state() == before_state
        assert database.wal.last_lsn == before_lsn
        assert not database._storage.exists(missing_file)
        assert database._transactions.open_transactions == 0


def test_stale_identity_gets_same_size_replacement_and_retires_old_generation(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        database.ensure_identity_indexes()
        catalog = database._catalog.catalog
        identity_name = identity_index_name(catalog.table("A").table_id)
        original = catalog.index_definition(identity_name).active_generation()
        assert original is not None
        old_file = original.file

        index = database._indexes.active_index(identity_name, catalog=catalog)
        index.mark_stale("injected stale identity generation")
        assert index.stale is True

        database.ensure_identity_indexes()

        replacement = database._catalog.catalog.index_definition(identity_name)
        active = replacement.active_generation()
        assert active is not None
        assert active.artifact_nonce != original.artifact_nonce
        assert active.bucket_count == original.bucket_count
        assert (
            replacement.generation(original.artifact_nonce).state
            is IndexGenerationState.STALE
        )
        assert database._storage.exists(old_file)
        assert database._storage.exists(active.file)
        assert _graph_rows(database) == ((1, 7, 17), (2, 8, 28))


def test_activation_plan_refuses_later_dml_before_wal_or_catalog_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_two_endpoint_graph(database)
        before_catalog = database._catalog.read_from_pages().serialize()
        before_lsn = database.wal.last_lsn
        transaction = database.begin("write")
        try:
            assert database._transactions.prepare_identity_index_activation(
                transaction._context
            )
            transaction.execute("CREATE (:A {id: 99})")

            with pytest.raises(GrafxTransactionStateError) as refused:
                transaction.commit()
            assert refused.value.details["field"] == "activation_transaction"
            assert transaction._context.active is True
            assert database.wal.last_lsn == before_lsn
            assert database._catalog.read_from_pages().serialize() == before_catalog
            assert (
                database._catalog.catalog.format_version
                == CATALOG_LEGACY_FORMAT_VERSION
            )
        finally:
            transaction.rollback()

        assert database.execute("MATCH (a:A) RETURN a.id").rows == ((1,), (2,))

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        assert reopened.execute("MATCH (a:A) RETURN a.id").rows == ((1,), (2,))


def test_activation_preserves_the_registered_winner_of_casefold_collisions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE person(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as rows:
            rows.execute("CREATE (:Person {id: 1})")
            rows.execute("CREATE (:person {id: 2})")

        registered_winner = database._indexes.index("pk_person").definition.table_name
        database.ensure_identity_indexes()

        colliding = tuple(
            definition
            for definition in database._catalog.catalog.index_definitions()
            if definition.registry_key == "pk_person"
        )
        assert len(colliding) == 1
        assert colliding[0].table_name == registered_winner
        assert database.execute("MATCH (n:Person) RETURN n.id").rows == ((1,),)
        assert database.execute("MATCH (n:person) RETURN n.id").rows == ((2,),)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        colliding = tuple(
            definition
            for definition in reopened._catalog.catalog.index_definitions()
            if definition.registry_key == "pk_person"
        )
        assert len(colliding) == 1
        assert colliding[0].table_name == registered_winner
        assert reopened.execute("MATCH (n:Person) RETURN n.id").rows == ((1,),)
        assert reopened.execute("MATCH (n:person) RETURN n.id").rows == ((2,),)


def test_identity_index_is_not_a_generic_property_equality_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(name STRING)")
            schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE E(FROM A TO B)")
        with database.begin("write") as rows:
            rows.execute("CREATE (:A {name: 'Ada'})")
            rows.execute("CREATE (:B {id: 1})")

        database.ensure_identity_indexes()

        assert database.execute(
            "MATCH (a:A) WHERE a.name = 'Ada' RETURN a.name"
        ).rows == (("Ada",),)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.execute(
            "MATCH (a:A) WHERE a.name = 'Ada' RETURN a.name"
        ).rows == (("Ada",),)


def test_concurrent_endpoint_write_refuses_activation_before_shadow_build(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as first:
        _seed_two_endpoint_graph(first)
        with connect(root, page_size=PAGE_SIZE) as second:
            activation = first.begin("write")
            try:
                assert first._transactions.prepare_identity_index_activation(
                    activation._context
                )
                before_generations = {
                    name
                    for name in first._storage.list_files("index/")
                    if name.startswith("index/g_")
                }

                with second.begin("write") as writer:
                    writer.execute("CREATE (:A {id: 3})")

                with pytest.raises(GrafxWriteConflict):
                    activation.commit()
                assert activation._context.active is True
                assert first._catalog.read_from_pages().format_version == (
                    CATALOG_LEGACY_FORMAT_VERSION
                )
                assert {
                    name
                    for name in first._storage.list_files("index/")
                    if name.startswith("index/g_")
                } == before_generations
            finally:
                activation.rollback()

            first.ensure_identity_indexes()
            assert first.execute("MATCH (a:A) RETURN a.id").rows == (
                (1,),
                (2,),
                (3,),
            )
