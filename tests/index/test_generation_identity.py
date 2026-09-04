"""Physical generation names and catalog-selected nonce checks for P2-ID."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.definition import (
    automatic_index_definitions,
    index_definition_matches_table,
    index_generation_file,
)
from okto_grafx.engine.index_manager import HashIndex
from okto_grafx.engine.public_views import _index_view

from .conftest import Database, exact_definition


def test_generation_file_is_canonical_and_independent_of_logical_name(
    database: Database,
) -> None:
    legacy = exact_definition(database.table)
    generated = replace(legacy, artifact_nonce=0xA17)

    assert generated.file == index_generation_file(0xA17)
    assert generated.file == "index/g_0000000000000a17.idx"
    assert generated.digest() == legacy.digest()
    assert generated != legacy


def test_public_index_view_preserves_the_selected_physical_generation(
    database: Database,
) -> None:
    nonce = 0xA17
    definition = replace(
        exact_definition(database.table, name="generated_view"),
        artifact_nonce=nonce,
    )
    index = database.manager.register(
        HashIndex(definition, database.pool, database.metrics)
    )

    view = _index_view(index)

    assert view.definition.artifact_nonce == nonce
    assert view.file == index_generation_file(nonce)
    assert view.file == index.file


@pytest.mark.parametrize("nonce", [0, -1, True, 1.0, 1 << 64])
def test_generation_file_refuses_non_generation_nonces(nonce: object) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        index_generation_file(nonce)

    assert refused.value.details["field"] == "artifact_nonce"


def test_catalog_selected_generation_requires_its_exact_header_nonce(
    database: Database,
) -> None:
    definition = replace(
        exact_definition(database.table, name="generated"), artifact_nonce=17
    )
    index = HashIndex(definition, database.pool, database.metrics)
    index._set_creation_nonce(18)

    with pytest.raises(GrafxIndexError) as refused:
        index.create()

    assert refused.value.details["field"] == "artifact_nonce"
    assert refused.value.details["expected"] == 17
    assert refused.value.details["observed"] == 18


def test_rehashed_automatic_exact_definition_keeps_logical_schema_provenance(
    database: Database,
) -> None:
    canonical = automatic_index_definitions(database.table)[0]
    generation = replace(canonical, bucket_count=256, artifact_nonce=91)

    assert index_definition_matches_table(generation, database.table)


def test_automatic_definition_normalization_is_bounded_to_the_table_value(
    database: Database,
) -> None:
    original = automatic_index_definitions(database.table)
    repeated = automatic_index_definitions(database.table)

    changed = replace(database.table, primary_key="name")
    changed_definitions = automatic_index_definitions(changed)

    assert repeated is original
    assert changed_definitions is not original
    assert original[0].positions == (0,)
    assert changed_definitions[0].positions == (1,)
    assert not index_definition_matches_table(original[0], changed)
    assert index_definition_matches_table(exact_definition(database.table), database.table)


def test_manager_definition_match_memo_does_not_cross_ddl_or_generation(
    database: Database,
) -> None:
    canonical = automatic_index_definitions(database.table)[0]

    assert database.manager._definition_matches_table_tolerantly(
        canonical, database.table
    )
    assert database.manager._definition_matches_table_tolerantly(
        canonical, database.table
    )
    after_repeat = database.manager._definition_match.cache_info()
    assert after_repeat.hits == 1
    assert after_repeat.misses == 1
    assert after_repeat.maxsize == 1024

    generation = replace(canonical, bucket_count=256, artifact_nonce=91)
    assert database.manager._definition_matches_table_tolerantly(
        generation, database.table
    )
    assert database.manager._definition_match.cache_info().misses == 2

    changed = replace(database.table, primary_key="name")
    assert not database.manager._definition_matches_table_tolerantly(generation, changed)
    assert database.manager._definition_match.cache_info().misses == 3

    custom = exact_definition(database.table)
    assert database.manager._definition_matches_table_tolerantly(
        custom, database.table
    )
    assert database.manager._definition_match.cache_info().misses == 4
