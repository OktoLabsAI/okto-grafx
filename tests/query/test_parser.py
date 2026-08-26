"""The parser: the grammar it reads, and the shapes it refuses (CONTRACT.md section 8.9, D3)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxParseError, GrafxQueryError
from okto_grafx.domain.model.value import INT64_MAX, INT64_MIN
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    CaseExpression,
    CreateClause,
    CreateNodeTableStatement,
    CreateRelTableStatement,
    CreateVectorSpaceStatement,
    DeleteClause,
    Direction,
    Literal,
    MergeClause,
    NullCheck,
    Parameter,
    Property,
    Query,
    SetClause,
    Subscript,
    UnaryOperation,
    Variable,
)
from okto_grafx.domain.query.limits import (
    MAX_CLAUSES,
    MAX_EXPRESSION_DEPTH,
    MAX_LIST_ELEMENTS,
    MAX_MAP_ENTRIES,
    MAX_PATTERNS_PER_CLAUSE,
    MAX_PROJECTION_ITEMS,
    MAX_SORT_KEYS,
    MAX_TRAVERSAL_HOPS,
)
from okto_grafx.domain.query.parser import parse


def only_item(text: str) -> object:
    """Return the single projected expression of a one-item RETURN clause."""
    statement = parse(text)
    assert isinstance(statement, Query)
    assert statement.return_clause is not None
    return statement.return_clause.items[0].expression


# --- statements -----------------------------------------------------------------------------


def test_a_node_table_statement_carries_its_columns_and_key() -> None:
    statement = parse("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    assert isinstance(statement, CreateNodeTableStatement)
    assert statement.name == "Person"
    assert [column.name for column in statement.columns] == ["id", "name"]
    assert statement.primary_key == "id"


def test_a_relationship_table_statement_carries_both_endpoints() -> None:
    statement = parse("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")
    assert isinstance(statement, CreateRelTableStatement)
    assert (statement.from_table, statement.to_table) == ("Person", "Person")
    assert [column.name for column in statement.columns] == ["since"]


def test_a_vector_column_names_the_space_it_belongs_to() -> None:
    statement = parse("CREATE NODE TABLE Chunk(v VECTOR(minilm_v2))")
    assert isinstance(statement, CreateNodeTableStatement)
    assert statement.columns[0].vector_space == "minilm_v2"


def test_a_vector_space_statement_keeps_its_options_unresolved() -> None:
    statement = parse("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
    assert isinstance(statement, CreateVectorSpaceStatement)
    assert statement.options.keys() == ("dimension", "metric")


def test_a_trailing_semicolon_is_accepted() -> None:
    assert isinstance(parse("RETURN 1;"), Query)


def test_a_query_may_be_only_a_return_clause() -> None:
    statement = parse("RETURN 1 AS one")
    assert isinstance(statement, Query)
    assert statement.match_clauses == ()


def test_keywords_are_read_without_regard_to_case() -> None:
    assert parse("match (p:Person) return p.name").describe() == parse(
        "MATCH (p:Person) RETURN p.name"
    ).describe()


def test_a_label_keeps_the_case_it_was_written_in() -> None:
    statement = parse("MATCH (p:Person) RETURN p.name")
    assert isinstance(statement, Query)
    assert statement.match_clauses[0].patterns[0].nodes[0].labels == ("Person",)


# --- clauses --------------------------------------------------------------------------------


def test_each_updating_clause_reaches_the_tree() -> None:
    statement = parse(
        "MATCH (p:Person) CREATE (p)-[:Knows]->(q:Person) SET p.age = 1 DELETE p"
    )
    assert isinstance(statement, Query)
    kinds = [type(clause) for clause in statement.updating_clauses]
    assert kinds == [CreateClause, SetClause, DeleteClause]


def test_a_merge_clause_carries_one_pattern() -> None:
    statement = parse("MERGE (p:Person {id: 1})")
    assert isinstance(statement, Query)
    assert isinstance(statement.updating_clauses[0], MergeClause)


def test_detach_delete_is_told_apart_from_delete() -> None:
    plain = parse("MATCH (p:Person) DELETE p")
    detaching = parse("MATCH (p:Person) DETACH DELETE p")
    assert isinstance(plain, Query) and isinstance(detaching, Query)
    assert isinstance(plain.updating_clauses[0], DeleteClause)
    assert plain.updating_clauses[0].detach is False
    assert isinstance(detaching.updating_clauses[0], DeleteClause)
    assert detaching.updating_clauses[0].detach is True


def test_a_set_target_is_read_at_property_precedence() -> None:
    statement = parse("MATCH (p:Person) SET p.age = p.age + 1")
    assert isinstance(statement, Query)
    clause = statement.updating_clauses[0]
    assert isinstance(clause, SetClause)
    assert isinstance(clause.items[0].target, Property)
    assert isinstance(clause.items[0].value, BinaryOperation)


def test_return_carries_distinct_order_skip_and_limit() -> None:
    statement = parse("MATCH (p:Person) RETURN DISTINCT p.age AS a ORDER BY a DESC SKIP 2 LIMIT 3")
    assert isinstance(statement, Query)
    clause = statement.return_clause
    assert clause is not None
    assert clause.distinct is True
    assert clause.sort_items[0].descending is True
    assert clause.skip == Literal(value=2)
    assert clause.limit == Literal(value=3)


def test_ascending_is_the_default_sort_direction() -> None:
    statement = parse("MATCH (p:Person) RETURN p.age AS a ORDER BY a")
    assert isinstance(statement, Query)
    assert statement.return_clause is not None
    assert statement.return_clause.sort_items[0].descending is False


# --- patterns -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id", Direction.OUTGOING),
        ("MATCH (a:Person)<-[:Knows]-(b:Person) RETURN a.id", Direction.INCOMING),
        ("MATCH (a:Person)-[:Knows]-(b:Person) RETURN a.id", Direction.UNDIRECTED),
        ("MATCH (a:Person)-->(b:Person) RETURN a.id", Direction.OUTGOING),
        ("MATCH (a:Person)<--(b:Person) RETURN a.id", Direction.INCOMING),
        ("MATCH (a:Person)--(b:Person) RETURN a.id", Direction.UNDIRECTED),
    ],
)
def test_every_arrow_form_reads_its_direction(text: str, expected: Direction) -> None:
    statement = parse(text)
    assert isinstance(statement, Query)
    assert statement.match_clauses[0].patterns[0].relationships[0].direction is expected


@pytest.mark.parametrize(
    ("written", "expected"),
    [("*3", (3, 3)), ("*1..3", (1, 3)), ("*..4", (1, 4))],
)
def test_every_hop_range_form_reads_its_bounds(
    written: str, expected: tuple[int, int]
) -> None:
    statement = parse(f"MATCH (a:Person)-[:Knows{written}]->(b:Person) RETURN a.id")
    assert isinstance(statement, Query)
    relationship = statement.match_clauses[0].patterns[0].relationships[0]
    assert (relationship.min_hops, relationship.max_hops) == expected


@pytest.mark.parametrize("written", ["*0", "*0..2", "*0..0"])
def test_a_zero_length_path_is_refused_rather_than_answered_as_one_hop(written: str) -> None:
    """openCypher gives ``*0..k`` the start node itself; this dialect starts at one hop.

    An accepted zero used to be executed from depth 1 -- ``*0..1`` answered as ``*1..1`` -- a
    third answer that is neither dialect's. Refusing at the door is the honest outcome.
    """
    with pytest.raises(GrafxParseError) as refusal:
        parse(f"MATCH (a:Person)-[:Knows{written}]->(b:Person) RETURN b.name")
    assert refusal.value.details["field"] == "min_hops"


def test_a_pattern_may_carry_inline_properties() -> None:
    statement = parse("MATCH (p:Person {id: 1, name: 'Ada'}) RETURN p.id")
    assert isinstance(statement, Query)
    properties = statement.match_clauses[0].patterns[0].nodes[0].properties
    assert properties is not None
    assert properties.keys() == ("id", "name")


def test_a_relationship_may_name_several_types() -> None:
    statement = parse("MATCH (a:Person)-[:Knows|Likes]->(b:Person) RETURN a.id")
    assert isinstance(statement, Query)
    assert statement.match_clauses[0].patterns[0].relationships[0].types == (
        "Knows",
        "Likes",
    )


# --- expressions ----------------------------------------------------------------------------


def test_and_binds_more_tightly_than_or() -> None:
    expression = only_item("RETURN true OR false AND true")
    assert expression.describe() == "(true OR (false AND true))"


def test_comparison_binds_more_tightly_than_and() -> None:
    expression = only_item("RETURN 1 < 2 AND 3 < 4")
    assert expression.describe() == "((1 < 2) AND (3 < 4))"


def test_arithmetic_binds_more_tightly_than_comparison() -> None:
    expression = only_item("RETURN 1 + 2 < 4")
    assert expression.describe() == "((1 + 2) < 4)"


def test_multiplication_binds_more_tightly_than_addition() -> None:
    expression = only_item("RETURN 1 + 2 * 3")
    assert expression.describe() == "(1 + (2 * 3))"


def test_exponentiation_is_right_associative() -> None:
    expression = only_item("RETURN 2 ^ 3 ^ 2")
    assert expression.describe() == "(2 ^ (3 ^ 2))"


def test_addition_is_left_associative() -> None:
    expression = only_item("RETURN 1 - 2 - 3")
    assert expression.describe() == "((1 - 2) - 3)"


def test_not_binds_more_loosely_than_comparison() -> None:
    expression = only_item("RETURN NOT 1 = 2")
    assert expression.describe() == "NOT (1 = 2)"


def test_not_binds_more_tightly_than_and() -> None:
    expression = only_item("RETURN NOT true AND false")
    assert expression.describe() == "(NOT true AND false)"


def test_a_sign_in_front_of_a_number_folds_into_the_literal() -> None:
    assert only_item("RETURN -5") == Literal(value=-5)


def test_exponentiation_binds_more_tightly_than_a_sign() -> None:
    # Folding "-2" first would make this the square of minus two, which is a different number.
    expression = only_item("RETURN -2 ^ 2")
    assert isinstance(expression, UnaryOperation)
    assert expression.describe() == "-(2 ^ 2)"


def test_the_most_negative_integer_is_writable() -> None:
    assert only_item(f"RETURN {INT64_MIN}") == Literal(value=INT64_MIN)


def test_the_largest_positive_integer_is_writable() -> None:
    assert only_item(f"RETURN {INT64_MAX}") == Literal(value=INT64_MAX)


def test_is_null_and_is_not_null_are_their_own_node() -> None:
    assert only_item("MATCH (p:Person) RETURN p.name IS NULL") == NullCheck(
        operand=Property(subject=Variable(name="p"), key="name"), negated=False
    )
    assert only_item("MATCH (p:Person) RETURN p.name IS NOT NULL") == NullCheck(
        operand=Property(subject=Variable(name="p"), key="name"), negated=True
    )


@pytest.mark.parametrize(
    "operator", ["STARTS WITH", "ENDS WITH", "CONTAINS", "IN", "<>", "!="]
)
def test_every_extra_comparison_reads_as_one_operator(operator: str) -> None:
    expression = only_item(f"RETURN 'a' {operator} 'b'")
    assert isinstance(expression, BinaryOperation)


def test_the_two_spellings_of_inequality_produce_one_operator() -> None:
    assert only_item("RETURN 1 <> 2") == only_item("RETURN 1 != 2")


def test_a_parameter_is_its_own_node() -> None:
    assert only_item("RETURN $value") == Parameter(name="value")


def test_a_constant_reads_as_a_literal_whatever_its_case() -> None:
    assert only_item("RETURN TrUe") == Literal(value=True)
    assert only_item("RETURN nULl") == Literal(value=None)


def test_a_named_argument_is_kept_apart_from_a_positional_one() -> None:
    expression = only_item("MATCH (n:Chunk) RETURN similarity(n.embedding, $q, space => 's')")
    assert expression.describe() == "similarity(n.embedding, $q, space => 's')"


def test_an_aggregate_may_take_distinct_or_a_star() -> None:
    assert only_item("MATCH (p:Person) RETURN count(*)").describe() == "count(*)"
    assert (
        only_item("MATCH (p:Person) RETURN count(DISTINCT p.id)").describe()
        == "count(DISTINCT p.id)"
    )


def test_a_list_and_a_map_read_as_their_own_nodes() -> None:
    assert only_item("RETURN [1, 2]").describe() == "[1, 2]"
    assert only_item("RETURN {a: 1}").describe() == "{a: 1}"


def test_searched_and_simple_case_are_distinct_immutable_nodes() -> None:
    searched = only_item("RETURN CASE WHEN true THEN 1 ELSE 2 END")
    simple = only_item("RETURN CASE $kind WHEN 'a' THEN 1 END")
    assert isinstance(searched, CaseExpression)
    assert searched.operand is None
    assert searched.alternatives[0].condition == Literal(value=True)
    assert searched.fallback == Literal(value=2)
    assert isinstance(simple, CaseExpression)
    assert simple.operand == Parameter(name="kind")
    assert simple.fallback is None


def test_property_and_subscript_postfixes_may_be_interleaved() -> None:
    expression = only_item("RETURN {parts: [[10, 20]]}.parts[1][2]")
    assert isinstance(expression, Subscript)
    assert expression.describe() == "{parts: [[10, 20]]}.parts[1][2]"


@pytest.mark.parametrize(
    "text",
    (
        "RETURN CASE END",
        "RETURN CASE WHEN true END",
        "RETURN CASE WHEN true THEN 1",
        "RETURN [1][]",
    ),
)
def test_incomplete_case_and_subscript_forms_are_located_refusals(text: str) -> None:
    with pytest.raises(GrafxParseError):
        parse(text)


# --- refusals -------------------------------------------------------------------------------


def test_a_parse_refusal_is_a_query_error_with_the_parse_code() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH")
    assert isinstance(failure.value, GrafxQueryError)
    assert failure.value.code == "parse_error"
    assert failure.value.retryable is False


def test_an_empty_query_is_refused() -> None:
    with pytest.raises(GrafxParseError):
        parse("   ")


def test_a_query_that_only_reads_must_return_something() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (p:Person)")
    assert failure.value.details["value"] == "RETURN"


def test_a_clause_after_return_is_refused() -> None:
    with pytest.raises(GrafxParseError):
        parse("MATCH (a:Person) RETURN a.id MATCH (b:Person) RETURN b.id")


def test_a_match_after_a_write_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("CREATE (a:Person) MATCH (b:Person) RETURN b.id")
    assert failure.value.details["value"] == "MATCH"


def test_the_unsupported_clause_is_named_rather_than_puzzled_over() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (a:Person) WITH a RETURN a.id")
    assert failure.value.details["value"] == "WITH"


def test_comparisons_do_not_chain() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("RETURN 1 = 2 = 3")
    assert failure.value.details["field"] == "operator"


def test_an_integer_one_past_the_range_is_refused_when_it_carries_no_sign() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse(f"RETURN {INT64_MAX + 1}")
    assert failure.value.details["field"] == "integer"


def test_an_unbounded_hop_range_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (a:Person)-[:Knows*]->(b:Person) RETURN a.id")
    assert failure.value.details["field"] == "max_hops"


def test_a_hop_range_with_no_upper_bound_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (a:Person)-[:Knows*2..]->(b:Person) RETURN a.id")
    assert failure.value.details["field"] == "max_hops"


def test_a_hop_range_beyond_the_ceiling_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse(
            f"MATCH (a:Person)-[:Knows*1..{MAX_TRAVERSAL_HOPS + 1}]->(b:Person) RETURN a.id"
        )
    assert failure.value.details["value"] == MAX_TRAVERSAL_HOPS + 1


def test_a_hop_range_at_the_ceiling_is_accepted() -> None:
    statement = parse(
        f"MATCH (a:Person)-[:Knows*1..{MAX_TRAVERSAL_HOPS}]->(b:Person) RETURN a.id"
    )
    assert isinstance(statement, Query)


def test_a_backwards_hop_range_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (a:Person)-[:Knows*3..1]->(b:Person) RETURN a.id")
    assert failure.value.details["field"] == "min_hops"


def test_an_arrow_pointing_both_ways_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (a:Person)<-[:Knows]->(b:Person) RETURN a.id")
    assert failure.value.details["field"] == "direction"


def test_a_star_on_something_that_is_not_an_aggregate_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (n:Chunk) RETURN similarity(*)")
    assert failure.value.details["field"] == "function"


def test_distinct_on_something_that_is_not_an_aggregate_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (n:Chunk) RETURN similarity(DISTINCT n.embedding)")
    assert failure.value.details["field"] == "function"


def test_a_positional_argument_after_a_named_one_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (n:Chunk) RETURN similarity(space => 's', n.embedding)")
    assert failure.value.details["field"] == "arguments"


def test_a_map_key_written_twice_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("RETURN {a: 1, a: 2}")
    assert failure.value.details["field"] == "key"


def test_an_unknown_column_type_names_the_ones_that_exist() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("CREATE NODE TABLE T(x FLOAT)")
    assert failure.value.details["value"] == "FLOAT"


def test_a_table_with_no_column_is_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("CREATE NODE TABLE T(PRIMARY KEY(id))")
    assert failure.value.details["field"] == "columns"


def test_two_primary_keys_are_refused() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("CREATE NODE TABLE T(a INT64, PRIMARY KEY(a), PRIMARY KEY(a))")
    assert failure.value.details["field"] == "primary_key"


def test_a_vector_space_statement_without_options_is_refused() -> None:
    with pytest.raises(GrafxParseError):
        parse("CREATE VECTOR SPACE s")


def test_set_must_assign_to_a_property() -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse("MATCH (p:Person) SET p = 1")
    assert failure.value.details["field"] == "target"


# --- the bounds fire on hostile input -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "RETURN " + "(" * 400 + "1" + ")" * 400,
        "RETURN " + "NOT " * 400 + "true",
        "RETURN " + "2^" * 400 + "2",
        "RETURN " + "-" * 400 + "2",
        "RETURN " + "[" * 400 + "1" + "]" * 400,
        "RETURN " + "{a: " * 400 + "1" + "}" * 400,
        "MATCH (n:Chunk) RETURN " + "count(" * 400 + "1" + ")" * 400,
    ],
)
def test_deep_nesting_is_refused_before_the_interpreter_runs_out_of_stack(text: str) -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse(text)
    assert failure.value.details["field"] == "depth"
    assert failure.value.details["value"] == MAX_EXPRESSION_DEPTH


def test_nesting_at_the_ceiling_still_parses() -> None:
    depth = MAX_EXPRESSION_DEPTH
    assert isinstance(parse("RETURN " + "(" * depth + "1" + ")" * depth), Query)


def test_a_long_flat_chain_costs_no_depth_at_all() -> None:
    # Precedence climbing consumes same-precedence operators in a loop, so this is the property
    # that makes an ordinary long predicate parse at all.
    assert isinstance(parse("RETURN " + " + ".join(["1"] * 2000)), Query)


def test_too_many_clauses_are_refused() -> None:
    text = " ".join(["MATCH (p:Person)"] * (MAX_CLAUSES + 1)) + " RETURN p.id"
    with pytest.raises(GrafxParseError) as failure:
        parse(text)
    assert failure.value.details["field"] == "clauses"


def test_too_many_patterns_in_one_clause_are_refused() -> None:
    patterns = ", ".join([f"(p{index}:Person)" for index in range(MAX_PATTERNS_PER_CLAUSE + 1)])
    with pytest.raises(GrafxParseError) as failure:
        parse(f"MATCH {patterns} RETURN p0.id")
    assert failure.value.details["field"] == "patterns"


def test_too_many_projected_items_are_refused() -> None:
    items = ", ".join(["1"] * (MAX_PROJECTION_ITEMS + 1))
    with pytest.raises(GrafxParseError) as failure:
        parse(f"RETURN {items}")
    assert failure.value.details["field"] == "items"


def test_too_many_sort_keys_are_refused() -> None:
    keys = ", ".join(["1"] * (MAX_SORT_KEYS + 1))
    with pytest.raises(GrafxParseError) as failure:
        parse(f"RETURN 1 AS a ORDER BY {keys}")
    assert failure.value.details["field"] == "sort_items"


def test_too_many_list_elements_are_refused() -> None:
    elements = ", ".join(["1"] * (MAX_LIST_ELEMENTS + 1))
    with pytest.raises(GrafxParseError) as failure:
        parse(f"RETURN [{elements}]")
    assert failure.value.details["field"] == "elements"


def test_too_many_map_entries_are_refused() -> None:
    entries = ", ".join(f"k{index}: 1" for index in range(MAX_MAP_ENTRIES + 1))
    with pytest.raises(GrafxParseError) as failure:
        parse("RETURN {" + entries + "}")
    assert failure.value.details["field"] == "entries"


def test_too_many_pattern_elements_are_refused() -> None:
    text = "MATCH (a:Person)" + "-[:Knows]->(b:Person)" * 40 + " RETURN a.id"
    with pytest.raises(GrafxParseError) as failure:
        parse(text)
    assert failure.value.details["field"] == "pattern"


@pytest.mark.parametrize(
    "text",
    [
        "MATCH",
        "MATCH (",
        "MATCH (p",
        "MATCH (p:",
        "MATCH (p:Person",
        "MATCH (p:Person)-",
        "MATCH (p:Person)-[",
        "MATCH (p:Person)-[:",
        "MATCH (p:Person)-[:Knows",
        "MATCH (p:Person)-[:Knows]",
        "MATCH (p:Person)-[:Knows]-",
        "MATCH (p:Person)-[:Knows]->",
        "MATCH (p:Person) RETURN",
        "MATCH (p:Person) RETURN p.",
        "MATCH (p:Person) RETURN p.name AS",
        "MATCH (p:Person) RETURN p.name ORDER",
        "MATCH (p:Person) RETURN p.name ORDER BY",
        "MATCH (p:Person) RETURN p.name LIMIT",
        "MATCH (p:Person) WHERE",
        "MATCH (p:Person) SET",
        "MATCH (p:Person) SET p.age =",
        "MATCH (p:Person) DELETE",
        "CREATE",
        "CREATE NODE",
        "CREATE NODE TABLE",
        "CREATE NODE TABLE T",
        "CREATE NODE TABLE T(",
        "CREATE NODE TABLE T(a",
        "CREATE REL TABLE R(FROM",
        "CREATE REL TABLE R(FROM A",
        "CREATE REL TABLE R(FROM A TO",
        "CREATE VECTOR",
        "CREATE VECTOR SPACE",
        "CREATE VECTOR SPACE s {",
        "CREATE VECTOR SPACE s {a",
        "CREATE VECTOR SPACE s {a:",
        "RETURN",
        "RETURN 1 +",
        "RETURN (",
        "RETURN [",
        "RETURN {",
        "RETURN 1 IS",
        "RETURN 1 IS NOT",
        "RETURN count(",
        "RETURN count(1",
        "RETURN 'a' STARTS",
        "MERGE",
    ],
)
def test_every_truncation_of_the_grammar_is_a_typed_refusal(text: str) -> None:
    # A hand-written parser meets truncated input by reading past the end of its token list;
    # this asserts that every prefix of the grammar refuses instead of raising IndexError.
    with pytest.raises(GrafxQueryError):
        parse(text)
