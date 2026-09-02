"""Every bound is pinned to its literal value (amendments A56 and A68).

A limit that a test derives from the limit itself slides with it, so widening it in one token
leaves the suite green -- which is exactly how a bound stops bounding anything. Each assertion
here names the number, and the last two assert an independent consequence instead.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain import query
from okto_grafx.domain.model.schema import MAX_IDENTIFIER_LENGTH
from okto_grafx.domain.model.value import INT64_MAX
from okto_grafx.domain.query import limits
from okto_grafx.domain.query.lexer import INTEGER_MAGNITUDE_LIMIT
from okto_grafx.domain.query.plan import MAX_PLAN_DEPTH

EXPECTED: dict[str, int] = {
    "DEFAULT_MAX_QUERY_VALUE_CHARACTERS": 65536,
    "MAX_QUERY_CHARACTERS": 65536,
    "MAX_QUERY_VALUE_CHARACTERS": 1048576,
    "MAX_RENDERED_QUERY_CHARACTERS": 1048576,
    "MAX_TOKENS": 8192,
    "MAX_EXPRESSION_DEPTH": 48,
    "MAX_CLAUSES": 64,
    "MAX_PATTERNS_PER_CLAUSE": 32,
    "MAX_PATTERN_ELEMENTS": 64,
    "MAX_TRAVERSAL_HOPS": 30,
    "MAX_PROJECTION_ITEMS": 256,
    "MAX_SORT_KEYS": 32,
    "MAX_LIST_ELEMENTS": 1024,
    "MAX_MAP_ENTRIES": 256,
    "MAX_PARAMETERS": 256,
    "MAX_COLUMN_DEFINITIONS": 512,
    "MAX_NAME_CHARACTERS": 128,
    "MAX_NUMBER_CHARACTERS": 40,
    "MAX_STRING_CHARACTERS": 16384,
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_bound_holds_the_value_it_was_chosen_at(name: str) -> None:
    assert getattr(limits, name) == EXPECTED[name]


def test_the_declared_bounds_are_exactly_the_ones_pinned_here() -> None:
    # A new bound that nothing pins is a bound that can be widened silently, so the set itself
    # is the thing under test rather than the individual numbers.
    assert set(limits.__all__) == set(EXPECTED)


def test_the_rendered_query_bound_is_reexported_by_the_query_package() -> None:
    assert query.MAX_RENDERED_QUERY_CHARACTERS == 1_048_576
    assert "MAX_RENDERED_QUERY_CHARACTERS" in query.__all__


def test_the_name_bound_matches_the_schema_identifier_rule() -> None:
    # An identifier longer than the domain model accepts could never name a real table, so the
    # two bounds have to agree or the parser would admit names the catalog must then refuse.
    assert limits.MAX_NAME_CHARACTERS == MAX_IDENTIFIER_LENGTH


def test_the_integer_magnitude_limit_is_one_past_the_positive_range() -> None:
    # The consequence, not the constant: the most negative 64-bit integer is a sign applied to a
    # magnitude one above the positive maximum, so the lexer has to admit exactly that magnitude.
    assert INTEGER_MAGNITUDE_LIMIT == INT64_MAX + 1


def test_the_plan_depth_ceiling_is_above_the_expression_ceiling() -> None:
    # A plan grows several operators per nesting level, so a ceiling at or below the expression
    # bound would refuse trees the parser is willing to produce.
    assert MAX_PLAN_DEPTH > limits.MAX_EXPRESSION_DEPTH
