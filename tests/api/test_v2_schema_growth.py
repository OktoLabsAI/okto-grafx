"""Schema growth must extend catalog-v2 exact-index authority atomically.

Once ``ensure_identity_indexes()`` has selected catalog v2, later DDL cannot fall back to
process-local legacy registrations.  A new table must publish its automatic exact indexes in
the same commit that makes the table visible, and those generations must survive a cold reopen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxDurabilityBarrierFailed
from okto_grafx.domain.index import IndexGenerationState, identity_index_name
from okto_grafx.engine.buffer_pool import BufferPool

PAGE_SIZE = 512


def _active_exact_index_names(database: object) -> set[str]:
    definitions = database._catalog.catalog.index_definitions()
    assert all(
        definition.active_generation() is not None
        and definition.active_generation().state is IndexGenerationState.ACTIVE
        for definition in definitions
    )
    return {definition.name for definition in definitions}


def test_v2_create_node_table_persists_primary_key_across_reopen(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Seed(id INT64, PRIMARY KEY(id))")
        database.ensure_identity_indexes()

        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Fresh(id INT64, name STRING, PRIMARY KEY(id))"
            )
        with database.begin("write") as rows:
            rows.execute("CREATE (:Fresh {id: 7, name: 'Ada'})")

        assert "pk_Fresh" in _active_exact_index_names(database)
        assert database.execute(
            "MATCH (n:Fresh) WHERE n.id = 7 RETURN n.name"
        ).rows == (("Ada",),)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert "pk_Fresh" in _active_exact_index_names(reopened)
        assert reopened.execute(
            "MATCH (n:Fresh) WHERE n.id = 7 RETURN n.name"
        ).rows == (("Ada",),)


def test_v2_new_generation_barrier_failure_never_publishes_catalog_or_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Seed(id INT64, PRIMARY KEY(id))")
        database.ensure_identity_indexes()

        catalog_before = database._catalog.read_from_pages().serialize()
        state_before = database._transactions.published_state()
        wal_before = database.wal.last_lsn
        failed_files: list[str] = []
        original_barrier = BufferPool.durability_barrier

        def fail_new_generation_barrier(
            pool: BufferPool,
            file: str | None = None,
        ) -> None:
            if isinstance(file, str) and file.startswith("index/g_"):
                failed_files.append(file)
                raise GrafxDurabilityBarrierFailed(
                    "The new generation did not reach stable storage.",
                    file=file,
                )
            original_barrier(pool, file)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                BufferPool,
                "durability_barrier",
                fail_new_generation_barrier,
            )
            with pytest.raises(GrafxDurabilityBarrierFailed):
                with database.begin("write") as schema:
                    schema.execute("CREATE NODE TABLE Fresh(id INT64, PRIMARY KEY(id))")

        assert len(failed_files) == 1
        assert database.wal.last_lsn == wal_before
        assert database._transactions.published_state() == state_before
        assert database._catalog.read_from_pages().serialize() == catalog_before
        assert database._catalog.catalog.serialize() == catalog_before
        assert not database._catalog.catalog.has_table("Fresh")

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened._catalog.catalog.serialize() == catalog_before
        assert reopened._catalog.catalog.has_table("Seed")
        assert not reopened._catalog.catalog.has_table("Fresh")
        assert reopened.verify("all").findings == ()


def test_v2_create_rel_table_persists_endpoint_and_identity_indexes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as rows:
            rows.execute("CREATE (:A {id: 1})")
            rows.execute("CREATE (:B {id: 2})")
        database.ensure_identity_indexes()

        with database.begin("write") as schema:
            schema.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")

        catalog = database._catalog.catalog
        expected = {
            "ef_E",
            "et_E",
            identity_index_name(catalog.table("A").table_id),
            identity_index_name(catalog.table("B").table_id),
        }
        assert expected <= _active_exact_index_names(database)

        with database.begin("write") as rows:
            rows.execute(
                "MATCH (a:A {id: 1}), (b:B {id: 2}) CREATE (a)-[:E {w: 12}]->(b)"
            )
        assert database.execute(
            "MATCH (a:A)-[e:E]->(b:B) RETURN a.id, b.id, e.w"
        ).rows == ((1, 2, 12),)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        catalog = reopened._catalog.catalog
        expected = {
            "ef_E",
            "et_E",
            identity_index_name(catalog.table("A").table_id),
            identity_index_name(catalog.table("B").table_id),
        }
        assert expected <= _active_exact_index_names(reopened)
        assert reopened.execute(
            "MATCH (a:A)-[e:E]->(b:B) RETURN a.id, b.id, e.w"
        ).rows == ((1, 2, 12),)


def test_v2_relationship_rollback_releases_detached_process_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as rows:
            rows.execute("CREATE (:A {id: 1})")
            rows.execute("CREATE (:B {id: 2})")
        database.ensure_identity_indexes()

        catalog_before = database._catalog.catalog.serialize()
        registry_before = {
            index.definition.registry_key: index.definition
            for index in database._indexes.indexes()
        }
        cache_before = dict(database._indexes._heap_cache_certificates)

        schema = database.begin("write")
        schema.execute("CREATE REL TABLE E(FROM A TO B)")
        detached_files = {
            index.file for index in database._indexes._detached_speculative_indexes
        }
        assert detached_files
        assert detached_files.isdisjoint(
            index.file for index in database._indexes.indexes()
        )
        schema.rollback()

        assert database._catalog.catalog.serialize() == catalog_before
        assert {
            index.definition.registry_key: index.definition
            for index in database._indexes.indexes()
        } == registry_before
        assert database._indexes._detached_speculative_indexes == set()
        assert database._indexes._heap_cache_certificates == cache_before
        assert detached_files.isdisjoint(database._indexes._heap_cache_certificates)


def test_v2_multi_ddl_transaction_persists_every_active_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as rows:
            rows.execute("CREATE (:A {id: 1})")
            rows.execute("CREATE (:B {id: 2})")
        database.ensure_identity_indexes()

        with database.begin("write") as combined:
            combined.execute(
                "CREATE NODE TABLE Fresh(id INT64, name STRING, PRIMARY KEY(id))"
            )
            combined.execute("CREATE (:Fresh {id: 7, name: 'Ada'})")
            combined.execute("CREATE REL TABLE E(FROM A TO B)")
            combined.execute("CREATE REL TABLE F(FROM A TO B)")

        catalog = database._catalog.catalog
        expected = {
            "pk_Fresh",
            "ef_E",
            "et_E",
            "ef_F",
            "et_F",
            identity_index_name(catalog.table("A").table_id),
            identity_index_name(catalog.table("B").table_id),
        }
        assert expected <= _active_exact_index_names(database)
        assert database.execute(
            "MATCH (n:Fresh) WHERE n.id = 7 RETURN n.name"
        ).rows == (("Ada",),)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        catalog = reopened._catalog.catalog
        expected = {
            "pk_Fresh",
            "ef_E",
            "et_E",
            "ef_F",
            "et_F",
            identity_index_name(catalog.table("A").table_id),
            identity_index_name(catalog.table("B").table_id),
        }
        assert expected <= _active_exact_index_names(reopened)
        assert reopened.execute(
            "MATCH (n:Fresh) WHERE n.id = 7 RETURN n.name"
        ).rows == (("Ada",),)
