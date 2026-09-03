"""Bounded admission for detached catalog-v2 index construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxTransactionBudgetExceeded
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)

PAGE_SIZE = 512
INDEX_BUILD_ENTRIES = 6


def _seed_one_edge(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with database.begin("write") as rows:
        rows.execute("CREATE (:A {id: 1})")
        rows.execute("CREATE (:B {id: 2})")
        rows.execute("MATCH (a:A {id: 1}), (b:B {id: 2}) CREATE (a)-[:E {w: 3}]->(b)")


def _storage_fingerprint(database: object) -> tuple[tuple[str, int], ...]:
    return tuple((item.name, item.size_bytes) for item in database.storage.files)


def _graph_rows(database: object) -> tuple[tuple[object, ...], ...]:
    return database.execute("MATCH (a:A)-[e:E]->(b:B) RETURN a.id, b.id, e.w").rows


def test_index_build_budget_refuses_before_catalog_wal_or_generation_mutation(
    tmp_path: Path,
) -> None:
    with connect(
        tmp_path / "db",
        page_size=PAGE_SIZE,
        max_index_build_entries=INDEX_BUILD_ENTRIES - 1,
    ) as database:
        _seed_one_edge(database)
        before = (
            database._catalog.read_from_pages().serialize(),
            database.transactions.published_state(),
            database.wal.last_lsn,
            _storage_fingerprint(database),
        )

        with pytest.raises(GrafxTransactionBudgetExceeded) as caught:
            database.ensure_identity_indexes()

        assert caught.value.details["field"] == "max_index_build_entries"
        assert caught.value.details["limit"] == INDEX_BUILD_ENTRIES - 1
        assert caught.value.details["observed"] == INDEX_BUILD_ENTRIES
        assert isinstance(caught.value.details["txn_id"], int)
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        assert (
            database._catalog.read_from_pages().serialize(),
            database.transactions.published_state(),
            database.wal.last_lsn,
            _storage_fingerprint(database),
        ) == before
        assert not any(
            item.name.startswith("index/g_") for item in database.storage.files
        )
        assert database.transactions.open_transactions == 0
        assert _graph_rows(database) == ((1, 2, 3),)


def test_exact_index_build_budget_succeeds_and_cold_verifies(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(
        root,
        page_size=PAGE_SIZE,
        max_index_build_entries=INDEX_BUILD_ENTRIES,
    ) as database:
        _seed_one_edge(database)
        database.ensure_identity_indexes()
        assert database._catalog.catalog.format_version == CATALOG_FORMAT_VERSION
        assert _graph_rows(database) == ((1, 2, 3),)

    with connect(
        root,
        page_size=PAGE_SIZE,
        max_index_build_entries=INDEX_BUILD_ENTRIES,
    ) as reopened:
        assert reopened._catalog.catalog.format_version == CATALOG_FORMAT_VERSION
        assert _graph_rows(reopened) == ((1, 2, 3),)
        assert reopened.verify("all").findings == ()
