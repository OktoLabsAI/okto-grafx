"""Connection-scoped sizing for newly materialized automatic exact indexes."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.index import identity_index_name
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)

PAGE_SIZE = 512


def _automatic_exact(database: object) -> dict[str, object]:
    return {
        definition.name: definition
        for definition in database._catalog.catalog.index_definitions()
        if definition.automatic
    }


def _create_graph_schema(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE E(FROM A TO B)")


def test_v1_activation_sizes_new_generations_from_the_connection_hint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as legacy:
        _create_graph_schema(legacy)

    with connect(
        root,
        page_size=PAGE_SIZE,
        automatic_index_expected_cardinality=16_000,
    ) as database:
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        database.ensure_identity_indexes()

        catalog = database._catalog.catalog
        definitions = _automatic_exact(database)
        expected_names = {
            "pk_A",
            "pk_B",
            "ef_E",
            "et_E",
            identity_index_name(catalog.table("A").table_id),
            identity_index_name(catalog.table("B").table_id),
        }
        assert definitions.keys() == expected_names
        assert all(
            definition.expected_cardinality == 16_000
            and definition.active_generation().bucket_count == 256
            for definition in definitions.values()
        )


def test_nonempty_v1_refuses_to_ignore_the_hint_on_new_table_ddl(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as legacy:
        with legacy.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Seed(id INT64, PRIMARY KEY(id))")

    with connect(
        root,
        page_size=PAGE_SIZE,
        automatic_index_expected_cardinality=16_000,
    ) as database:
        assert database._catalog.catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
        with database.begin("write") as schema:
            with pytest.raises(GrafxUnsupportedOperation) as caught:
                schema.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        assert caught.value.details["remedy"] == "maintenance.ensure_identity_indexes"
        assert not database._catalog.catalog.has_table("T")


def test_reopen_preserves_existing_sizing_and_uses_current_hint_for_new_ddl(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(
        root,
        page_size=PAGE_SIZE,
        automatic_index_expected_cardinality=16_000,
    ) as database:
        assert database._catalog.catalog.format_version == CATALOG_FORMAT_VERSION
        database.ensure_identity_indexes()
        _create_graph_schema(database)
        original = {
            name: (
                definition.expected_cardinality,
                definition.active_generation().bucket_count,
                definition.active_generation().artifact_nonce,
            )
            for name, definition in _automatic_exact(database).items()
        }

    with connect(
        root,
        page_size=PAGE_SIZE,
        automatic_index_expected_cardinality=65_000,
    ) as reopened:
        assert {
            name: (
                definition.expected_cardinality,
                definition.active_generation().bucket_count,
                definition.active_generation().artifact_nonce,
            )
            for name, definition in _automatic_exact(reopened).items()
        } == original

        with reopened.begin("write") as schema:
            schema.execute("CREATE NODE TABLE C(id INT64, PRIMARY KEY(id))")

        created = _automatic_exact(reopened)["pk_C"]
        assert created.expected_cardinality == 65_000
        assert created.active_generation().bucket_count == 1_024
