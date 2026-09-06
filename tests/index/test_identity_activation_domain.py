from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index import (
    DEFAULT_BUCKET_COUNT,
    MAX_EXPECTED_CARDINALITY,
    custom_index_sizing,
    identity_index_name,
    identity_index_sizing,
    rehash_index_sizing,
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
    assert raised.value.details["max_expected_cardinality"] == MAX_EXPECTED_CARDINALITY


def test_automatic_identity_sizing_uses_the_hint_only_as_a_floor() -> None:
    assert identity_index_sizing(10, expected_cardinality=16_000) == (16_000, 256)
    assert identity_index_sizing(10_000, expected_cardinality=16_000) == (
        20_000,
        512,
    )


@pytest.mark.parametrize("visible_rows", [-1, True, 1.5, None])
def test_automatic_identity_sizing_refuses_invalid_counts(
    visible_rows: object,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        identity_index_sizing(visible_rows)

    assert raised.value.details["field"] == "visible_rows"


@pytest.mark.parametrize(
    ("expected_cardinality", "bucket_count"),
    [
        (1, 1),
        (64, 1),
        (65, 2),
        (4096, DEFAULT_BUCKET_COUNT),
        (4097, 128),
        (MAX_EXPECTED_CARDINALITY, 4096),
    ],
)
def test_custom_sizing_derives_the_next_power_of_two_directory(
    expected_cardinality: int,
    bucket_count: int,
) -> None:
    assert custom_index_sizing(expected_cardinality=expected_cardinality) == (
        bucket_count,
        expected_cardinality,
    )


def test_custom_sizing_preserves_default_and_explicit_bucket_intent() -> None:
    assert custom_index_sizing() == (DEFAULT_BUCKET_COUNT, None)
    assert custom_index_sizing(bucket_count=17) == (17, None)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"bucket_count": 64, "expected_cardinality": 4096}, "sizing"),
        ({"bucket_count": 0}, "bucket_count"),
        ({"expected_cardinality": 0}, "expected_cardinality"),
        (
            {"expected_cardinality": MAX_EXPECTED_CARDINALITY + 1},
            "expected_cardinality",
        ),
        ({"expected_cardinality": True}, "expected_cardinality"),
    ],
)
def test_custom_sizing_refuses_ambiguous_or_out_of_domain_hints(
    kwargs: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        custom_index_sizing(**kwargs)

    assert raised.value.details["field"] == field


@pytest.mark.parametrize(
    ("current_bucket_count", "kwargs", "expected"),
    [
        (1, {"bucket_count": 2}, (2, None)),
        (64, {"bucket_count": 65}, (65, None)),
        (4095, {"bucket_count": 4096}, (4096, None)),
        (1, {"expected_cardinality": 65}, (2, 65)),
        (64, {"expected_cardinality": 4097}, (128, 4097)),
        (
            2048,
            {"expected_cardinality": MAX_EXPECTED_CARDINALITY},
            (4096, MAX_EXPECTED_CARDINALITY),
        ),
    ],
)
def test_rehash_sizing_accepts_every_kind_of_strict_growth_boundary(
    current_bucket_count: int,
    kwargs: dict[str, object],
    expected: tuple[int, int | None],
) -> None:
    assert rehash_index_sizing(current_bucket_count, **kwargs) == expected


@pytest.mark.parametrize(
    ("current_bucket_count", "kwargs", "field"),
    [
        (64, {}, "sizing"),
        (
            64,
            {"bucket_count": 128, "expected_cardinality": 4097},
            "sizing",
        ),
        (64, {"bucket_count": 64}, "bucket_count"),
        (64, {"bucket_count": 63}, "bucket_count"),
        (64, {"expected_cardinality": 4096}, "bucket_count"),
        (64, {"expected_cardinality": 1}, "bucket_count"),
        (4096, {"expected_cardinality": MAX_EXPECTED_CARDINALITY}, "bucket_count"),
        (64, {"bucket_count": 4097}, "bucket_count"),
        (
            64,
            {"expected_cardinality": MAX_EXPECTED_CARDINALITY + 1},
            "expected_cardinality",
        ),
        (64, {"bucket_count": True}, "bucket_count"),
        (64, {"expected_cardinality": True}, "expected_cardinality"),
    ],
)
def test_rehash_sizing_refuses_missing_ambiguous_non_growing_or_invalid_hints(
    current_bucket_count: int,
    kwargs: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        rehash_index_sizing(current_bucket_count, **kwargs)

    assert raised.value.details["field"] == field


@pytest.mark.parametrize("current_bucket_count", [0, 4097, True, 1.5, None])
def test_rehash_sizing_refuses_an_invalid_active_directory(
    current_bucket_count: object,
) -> None:
    with pytest.raises(GrafxIndexError) as raised:
        rehash_index_sizing(current_bucket_count, bucket_count=128)

    assert raised.value.details["field"] == "current_bucket_count"


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
