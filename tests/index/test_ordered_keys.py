"""Order equivalence and hostile boundaries of ordered TIMESTAMP/STRING keys."""

from __future__ import annotations

import itertools

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.ordered_keys import (
    ordered_entry_identity,
    ordered_index_key,
    ordered_timestamp_string_key,
)
from okto_grafx.domain.model.value import INT64_MAX, INT64_MIN, Timestamp
from okto_grafx.engine.query_engine import _sort_key


_TIMESTAMPS = (
    None,
    Timestamp(INT64_MIN),
    Timestamp(-1),
    Timestamp(0),
    Timestamp(1),
    Timestamp(INT64_MAX),
)
_STRINGS = (None, "", "\x00", "a", "a\x00", "a\x00b", "aa", "é", "東京", "😀")


def _canonical(item: tuple[object, object]) -> tuple[object, object]:
    return _sort_key(item[0]), _sort_key(item[1])


def test_encoded_order_matches_the_canonical_query_sort_in_both_directions() -> None:
    corpus = tuple(itertools.product(_TIMESTAMPS, _STRINGS))

    encoded = sorted(corpus, key=lambda item: ordered_timestamp_string_key(*item))
    canonical = sorted(corpus, key=_canonical)

    assert encoded == canonical
    assert sorted(
        corpus,
        key=lambda item: ordered_timestamp_string_key(*item),
        reverse=True,
    ) == sorted(corpus, key=_canonical, reverse=True)


def test_row_positions_and_runtime_types_are_validated_exactly() -> None:
    values = ("event-1", Timestamp(9), "ignored")

    assert ordered_index_key(values, (1, 0)) == ordered_timestamp_string_key(
        Timestamp(9), "event-1"
    )

    with pytest.raises(GrafxIndexError) as wrong_type:
        ordered_index_key(values, (0, 1))
    assert wrong_type.value.details["expected"] == "TIMESTAMP"

    with pytest.raises(GrafxIndexError) as outside:
        ordered_index_key(values, (1, 4))
    assert outside.value.details["field"] == "positions"


def test_strings_are_prefix_free_and_invalid_unicode_is_refused_typed() -> None:
    keys = {
        value: ordered_timestamp_string_key(Timestamp(1), value)
        for value in ("", "\x00", "a", "a\x00", "a\x00b", "aa")
    }

    assert len(set(keys.values())) == len(keys)
    assert sorted(keys, key=keys.__getitem__) == ["", "\x00", "a", "a\x00", "a\x00b", "aa"]

    with pytest.raises(GrafxIndexError) as refused:
        ordered_timestamp_string_key(Timestamp(1), "\ud800")
    assert refused.value.details["field"] == "values"


def test_record_reference_is_the_strict_physical_tie_breaker() -> None:
    key = ordered_timestamp_string_key(Timestamp(3), "same")
    earlier = RecordRef(2, 7)
    later = RecordRef(3, 0)

    assert ordered_entry_identity(key, earlier) < ordered_entry_identity(key, later)
