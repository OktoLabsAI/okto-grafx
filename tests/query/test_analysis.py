"""Semantic analysis: what a statement means, decided with no catalog in reach."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxParseError, GrafxPlanError
from okto_grafx.domain.query import (
    LABEL_FUNCTION,
    SIZE_FUNCTION,
    STRING_SPLIT_FUNCTION,
    TIMESTAMP_FUNCTION,
)
from okto_grafx.domain.query.analysis import (
    ENTITY_NODE,
    ENTITY_RELATIONSHIP,
    analyze,
    contains_aggregate,
    is_aggregate,
)
from okto_grafx.domain.query.ast import (
    CaseExpression,
    FunctionCall,
    Literal,
    Parameter,
    Query,
    ReturnClause,
    ReturnItem,
)
from okto_grafx.domain.query.limits import MAX_PARAMETERS
from okto_grafx.domain.query.parser import parse


def analysis_of(text: str):
    """Return the analysis of one query text."""
    return analyze(parse(text))


def test_pulse_scalar_function_names_are_exported() -> None:
    assert LABEL_FUNCTION == "LABEL"
    assert SIZE_FUNCTION == "SIZE"
    assert STRING_SPLIT_FUNCTION == "STRING_SPLIT"
    assert TIMESTAMP_FUNCTION == "TIMESTAMP"


# --- bindings -------------------------------------------------------------------------------


def test_a_pattern_binds_its_nodes_and_relationships_by_kind() -> None:
    found = analysis_of("MATCH (a:Person)-[r:Knows]->(b:Person) RETURN a.id")
    assert found.variables(ENTITY_NODE) == ("a", "b")
    assert found.variables(ENTITY_RELATIONSHIP) == ("r",)


def test_bindings_keep_the_order_they_were_written_in() -> None:
    found = analysis_of("MATCH (z:Person), (a:Person) RETURN z.id, a.id")
    assert [binding.name for binding in found.bindings] == ["z", "a"]


def test_a_variable_used_by_no_pattern_is_refused_and_names_what_is_bound() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN q.name")
    assert failure.value.details["value"] == "q"
    assert "p" in failure.value.message


def test_a_variable_cannot_be_a_node_and_a_relationship() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (a:Person)-[a:Knows]->(b:Person) RETURN b.id")
    assert failure.value.details["field"] == "variable"


def test_a_variable_cannot_carry_two_different_labels() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (a:Person), (a:Doc) RETURN a.id")
    assert failure.value.details["value"] == "a"


def test_a_second_mention_may_add_the_label_the_first_left_out() -> None:
    found = analysis_of("MATCH (a:Person)-[:Knows]->(b) MATCH (b:Person) RETURN b.id")
    binding = found.binding("b")
    assert binding is not None
    assert binding.labels == ("Person",)


# --- parameters -----------------------------------------------------------------------------


def test_parameters_are_collected_once_each_in_first_appearance_order() -> None:
    found = analysis_of(
        "MATCH (p:Person) WHERE p.id = $id AND p.age = $age OR p.city = $id RETURN p.name"
    )
    assert found.parameters == ("id", "age")


def test_case_and_subscript_parameters_follow_written_order() -> None:
    found = analysis_of(
        "RETURN CASE $kind WHEN $wanted THEN $items[$position] ELSE $fallback END"
    )
    assert found.parameters == ("kind", "wanted", "items", "position", "fallback")


def test_a_prebuilt_case_still_needs_one_when_alternative() -> None:
    statement = Query(
        return_clause=ReturnClause(
            items=(
                ReturnItem(
                    expression=CaseExpression(
                        operand=None,
                        alternatives=(),
                        fallback=Literal(value=1),
                    )
                ),
            )
        )
    )
    with pytest.raises(GrafxPlanError) as failure:
        analyze(statement)
    assert failure.value.details["field"] == "case"


def test_a_row_window_may_be_a_parameter() -> None:
    found = analysis_of("MATCH (p:Person) RETURN p.id SKIP $offset LIMIT $count")
    assert found.parameters == ("offset", "count")


def test_too_many_parameters_are_refused() -> None:
    # The parameters go inside ONE list literal on purpose: a RETURN clause is bounded at the
    # same 256 items, so projecting them one per column would be refused by the projection rule
    # and this guard would never fire (amendment A34).
    items = ", ".join(f"$p{index}" for index in range(MAX_PARAMETERS + 1))
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"RETURN [{items}]")
    assert failure.value.details["field"] == "parameters"


def test_a_schema_statement_may_not_carry_a_parameter() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("CREATE VECTOR SPACE s {dimension: $d, metric: 'cosine'}")
    assert failure.value.details["field"] == "parameter"


# --- aggregation ----------------------------------------------------------------------------


def test_a_clause_with_an_aggregate_groups_by_everything_else() -> None:
    found = analysis_of("MATCH (p:Person) RETURN p.city AS city, count(*) AS total")
    assert found.aggregated is True
    assert found.grouping_positions == (0,)
    assert [item.function for item in found.aggregations] == ["COUNT"]


def test_a_clause_with_no_aggregate_does_not_group() -> None:
    found = analysis_of("MATCH (p:Person) RETURN p.city")
    assert found.aggregated is False
    assert found.grouping_positions == (0,)


def test_an_aggregate_in_where_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) WHERE count(p.id) > 1 RETURN p.id")
    assert failure.value.details["field"] == "predicate"


def test_an_aggregate_inside_an_aggregate_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN sum(count(p.id))")
    assert failure.value.details["field"] == "function"


def test_an_aggregate_in_a_written_property_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) CREATE (q:Person {age: count(p.id)})")
    assert failure.value.details["field"] == "expression"


def test_an_aggregate_in_a_set_value_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) SET p.age = count(p.id)")
    assert failure.value.details["field"] == "expression"


def test_an_aggregate_takes_exactly_one_argument() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN sum(p.age, p.id)")
    assert failure.value.details["field"] == "function"


def test_only_count_is_written_with_a_star() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN sum(*)")
    assert failure.value.details["value"] == "sum"


def test_the_aggregate_predicates_agree_with_each_other() -> None:
    call = parse("MATCH (p:Person) RETURN count(p.id)").return_clause.items[0].expression
    assert is_aggregate(call) is True
    assert contains_aggregate(call) is True
    assert is_aggregate(Literal(value=1)) is False
    assert contains_aggregate(Literal(value=1)) is False


# --- ordering -------------------------------------------------------------------------------


def test_an_order_by_key_may_name_an_alias() -> None:
    found = analysis_of("MATCH (p:Person) RETURN p.age AS a ORDER BY a DESC")
    assert found.output_columns == ("a",)


def test_an_order_by_key_may_repeat_a_projected_expression() -> None:
    found = analysis_of("MATCH (p:Person) RETURN DISTINCT p.age ORDER BY p.age")
    assert found.output_columns == ("p.age",)


def test_an_order_by_key_that_distinct_dropped_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN DISTINCT p.age ORDER BY p.name")
    assert failure.value.details["field"] == "sort_item"


def test_an_order_by_key_that_a_group_dropped_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN count(*) AS total ORDER BY p.name")
    assert failure.value.details["field"] == "sort_item"


def test_an_aggregate_written_directly_in_order_by_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN p.city ORDER BY count(*)")
    assert failure.value.details["field"] == "sort_item"


def test_an_order_by_key_over_a_bound_variable_is_allowed_without_aggregation() -> None:
    found = analysis_of("MATCH (p:Person) RETURN p.age ORDER BY p.name")
    assert found.output_columns == ("p.age",)


def test_two_items_may_not_be_given_the_same_name() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN p.age AS x, p.name AS x")
    assert failure.value.details == {"field": "alias", "value": "x"}


@pytest.mark.parametrize("text", ["RETURN 1, 1", "RETURN 1, 2 AS `1`"])
def test_two_items_may_not_share_a_derived_or_explicit_output_name(text: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(text)
    assert failure.value.details == {"field": "column", "value": "1"}


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
def test_a_row_window_that_is_not_a_count_is_refused(keyword: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"MATCH (p:Person) RETURN p.id {keyword} 'two'")
    assert failure.value.details["field"] == keyword.lower()


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
def test_a_negative_row_window_is_refused(keyword: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"MATCH (p:Person) RETURN p.id {keyword} -1")
    assert failure.value.details["field"] == keyword.lower()


# --- similarity -----------------------------------------------------------------------------


def test_the_similarity_call_is_taken_apart_for_the_planner() -> None:
    found = analysis_of(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'minilm_v2') > 0.7 "
        "RETURN n.id"
    )
    assert found.similarity is not None
    assert found.similarity.variable == "n"
    assert found.similarity.property_key == "embedding"
    assert found.similarity.space == Literal(value="minilm_v2")
    assert found.similarity.query_vector == Parameter(name="q")


def test_the_space_may_be_named_by_a_parameter() -> None:
    found = analysis_of(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => $space) > 0.7 RETURN n.id"
    )
    assert found.similarity is not None
    assert found.similarity.space == Parameter(name="space")


def test_the_same_call_written_twice_is_one_search() -> None:
    found = analysis_of(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 's') > 0.1 "
        "RETURN similarity(n.embedding, $q, space => 's') AS score"
    )
    assert found.similarity is not None


def test_two_different_searches_in_one_query_are_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(
            "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'a') > 0.1 "
            "AND similarity(n.embedding, $q, space => 'b') > 0.1 RETURN n.id"
        )
    assert failure.value.details["field"] == "function"


def test_a_search_that_names_no_space_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (n:Chunk) WHERE similarity(n.embedding, $q) > 0.1 RETURN n.id")
    assert failure.value.details["field"] == "space"


def test_a_search_whose_space_is_computed_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(
            "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => n.layer) > 0.1 "
            "RETURN n.id"
        )
    assert failure.value.details["field"] == "space"


def test_a_search_with_an_unknown_named_argument_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(
            "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 's', k => 3) > 0.1 "
            "RETURN n.id"
        )
    assert failure.value.details["field"] == "argument"


def test_a_search_whose_first_argument_is_not_a_property_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (n:Chunk) WHERE similarity($a, $q, space => 's') > 0.1 RETURN n.id")
    assert failure.value.details["field"] == "argument"


def test_a_search_with_the_wrong_number_of_arguments_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (n:Chunk) WHERE similarity(n.embedding, space => 's') > 0.1 RETURN n.id")
    assert failure.value.details["field"] == "function"


def test_the_score_projection_needs_a_search_to_report() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (n:Chunk) RETURN similarity_score()")
    assert failure.value.details["value"] == "similarity_score"


def test_the_score_projection_takes_no_arguments() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (n:Chunk) RETURN similarity_score(n.embedding)")
    assert failure.value.details["field"] == "function"


def test_the_score_projection_is_recorded_when_a_search_exists() -> None:
    found = analysis_of(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 's') > 0.1 "
        "RETURN similarity_score() AS score"
    )
    assert found.scores_similarity is True


# --- writes ---------------------------------------------------------------------------------


def test_a_written_node_needs_exactly_one_label() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("CREATE (n)")
    assert failure.value.details["field"] == "labels"


def test_a_written_relationship_needs_exactly_one_type() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("CREATE (a:Person)-[:Knows|Likes]->(b:Person)")
    assert failure.value.details["field"] == "types"


def test_a_written_relationship_needs_a_direction() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("CREATE (a:Person)-[:Knows]-(b:Person)")
    assert failure.value.details["field"] == "direction"


def test_a_written_relationship_may_not_span_a_hop_range() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("CREATE (a:Person)-[:Knows*1..2]->(b:Person)")
    assert failure.value.details["field"] == "hops"


def test_a_written_node_that_reuses_a_binding_needs_no_label() -> None:
    found = analysis_of("MATCH (a:Person) CREATE (a)-[:Knows]->(b:Person)")
    assert found.variables(ENTITY_NODE) == ("a", "b")


def test_delete_needs_a_bound_variable() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) DELETE q")
    assert failure.value.details["value"] == "q"


def test_set_needs_a_bound_subject() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) SET q.age = 1")
    assert failure.value.details["value"] == "q"


def test_an_unknown_function_names_the_ones_that_exist() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of("MATCH (p:Person) RETURN nosuch(p.id)")
    assert failure.value.details["value"] == "nosuch"
    assert "coalesce" in failure.value.message
    assert "similarity" in failure.value.message


def test_coalesce_is_case_insensitive_and_takes_positional_arguments() -> None:
    found = analysis_of(
        "MATCH (p:Person) RETURN CoAlEsCe(p.name, $fallback) AS name"
    )
    assert found.parameters == ("fallback",)


@pytest.mark.parametrize(
    "expression",
    (
        "coalesce()",
        "coalesce(fallback => 'Ada')",
        "coalesce(null, fallback => 'Ada')",
    ),
)
def test_coalesce_refuses_an_empty_or_named_argument_list(expression: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"MATCH (p:Person) RETURN {expression}")
    assert failure.value.details["field"] == "function"
    assert failure.value.details["value"].lower() == "coalesce"


def test_timestamp_is_case_insensitive_and_collects_its_parameter() -> None:
    found = analysis_of("RETURN TiMeStAmP($moment) AS at")
    assert found.parameters == ("moment",)


@pytest.mark.parametrize(
    "expression",
    ("timestamp()", "timestamp('a', 'b')", "timestamp(value => 'a')"),
)
def test_timestamp_refuses_wrong_or_named_arguments(expression: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"RETURN {expression}")
    assert failure.value.details == {"field": "function", "value": "timestamp"}


@pytest.mark.parametrize("expression", ("timestamp(DISTINCT 'a')", "timestamp(*)"))
def test_timestamp_takes_neither_distinct_nor_a_star(expression: str) -> None:
    with pytest.raises(GrafxParseError):
        analysis_of(f"RETURN {expression}")


def test_label_is_case_insensitive_and_analysed_like_the_other_scalars() -> None:
    found = analysis_of("MATCH (p:Person) RETURN LaBeL(p) AS kind")
    assert found.parameters == ()


@pytest.mark.parametrize(
    "expression",
    ("label()", "label(p, p)", "label(value => p)"),
)
def test_label_refuses_wrong_or_named_arguments(expression: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"MATCH (p:Person) RETURN {expression}")
    assert failure.value.details == {"field": "function", "value": "label"}


@pytest.mark.parametrize("expression", ("label(DISTINCT p)", "label(*)"))
def test_label_takes_neither_distinct_nor_a_star(expression: str) -> None:
    with pytest.raises(GrafxParseError):
        analysis_of(f"MATCH (p:Person) RETURN {expression}")


def test_string_split_and_size_are_case_insensitive_and_collect_parameters() -> None:
    found = analysis_of(
        "RETURN SiZe(StRiNg_SpLiT($reference, $separator)) AS pieces"
    )
    assert found.parameters == ("reference", "separator")


@pytest.mark.parametrize(
    ("expression", "function"),
    (
        ("string_split()", "string_split"),
        ("string_split('a')", "string_split"),
        ("string_split('a', ':', 'extra')", "string_split"),
        (
            "string_split(text => 'a', separator => ':')",
            "string_split",
        ),
        ("size()", "size"),
        ("size('a', 'extra')", "size"),
        ("size(value => 'a')", "size"),
    ),
)
def test_string_split_and_size_refuse_wrong_or_named_arguments(
    expression: str, function: str
) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        analysis_of(f"RETURN {expression}")
    assert failure.value.details == {"field": "function", "value": function}


@pytest.mark.parametrize(
    "expression",
    (
        "size(DISTINCT 'abc')",
        "string_split(DISTINCT 'abc', ':')",
        "size(*)",
        "string_split(*)",
    ),
)
def test_scalar_distinct_and_star_text_are_typed_parse_refusals(
    expression: str,
) -> None:
    with pytest.raises(GrafxParseError) as failure:
        parse(f"RETURN {expression}")
    assert failure.value.details["field"] == "function"


@pytest.mark.parametrize("modifier", ("distinct", "star"))
@pytest.mark.parametrize(
    ("function", "arguments"),
    (
        ("size", (Literal(value="abc"),)),
        (
            "string_split",
            (Literal(value="abc"), Literal(value=":")),
        ),
    ),
)
def test_prebuilt_scalar_ast_refuses_distinct_and_star(
    modifier: str, function: str, arguments: tuple[Literal, ...]
) -> None:
    call = FunctionCall(
        name=function,
        arguments=arguments,
        distinct=modifier == "distinct",
        star=modifier == "star",
    )
    statement = Query(
        return_clause=ReturnClause(items=(ReturnItem(expression=call),))
    )
    with pytest.raises(GrafxPlanError) as failure:
        analyze(statement)
    assert failure.value.details == {"field": "function", "value": function}


def test_a_schema_statement_analyses_to_an_empty_analysis() -> None:
    found = analysis_of("CREATE NODE TABLE T(a INT64)")
    assert found.bindings == ()
    assert found.parameters == ()
    assert found.aggregated is False
