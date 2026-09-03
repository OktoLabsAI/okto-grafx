from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index import (
    MAX_EXPECTED_CARDINALITY,
    identity_index_name,
    identity_index_sizing,
)


@pytest.mark.parametrize(
    ("visible_rows", "expected_cardinality", "bucket_count"),
    [
        (0, 4096, 64),
        (2048, 4096, 64),
        (2049, 4098, 128),
        (131_072, MAX_EXPECTED_CARDINALITY, 4096),
    ],
)
def test_automatic_identity_sizing_follows_the_adr_boundaries(
    visible_rows: int,
    expected_cardinality: int,
    bucket_count: int,
) -> None:
    assert identity_index_sizing(visible_rows) == (
        expected_cardinality,
        bucket_count,
    )


def test_automatic_identity_sizing_refuses_the_first_unsupported_row_count() -> None:
    with pytest.raises(GrafxIndexError) as raised:
        identity_index_sizing(131_073)

    assert raised.value.details["field"] == "visible_rows"
    assert raised.value.details["expected_cardinality"] == 262_146
    assert (
        raised.value.details["max_expected_cardinality"]
        == MAX_EXPECTED_CARDINALITY
    )


@pytest.mark.parametrize("visible_rows", [-1, True, 1.5, None])
def test_automatic_identity_sizing_refuses_invalid_counts(
    visible_rows: object,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        identity_index_sizing(visible_rows)

    assert raised.value.details["field"] == "visible_rows"


@pytest.mark.parametrize(
    ("table_id", "name"),
    [
        (1, "rid_t_00000001"),
        (0xFFFFFFFF, "rid_t_ffffffff"),
    ],
)
def test_identity_index_name_is_canonical(table_id: int, name: str) -> None:
    assert identity_index_name(table_id) == name


@pytest.mark.parametrize("table_id", [0, -1, 0x1_0000_0000, True, 1.0, None])
def test_identity_index_name_refuses_values_outside_the_catalog_domain(
    table_id: object,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        identity_index_name(table_id)

    assert raised.value.details["field"] == "table_id"
