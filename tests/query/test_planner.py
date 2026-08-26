"""The planner: which operators, in which order, and which plans it refuses to build."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxEmbeddingSpaceMismatch,
    GrafxPlanError,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.ast import BinaryOperation, Direction, Literal, Parameter
from okto_grafx.domain.query.plan import (
    CreateNodeTable,
    CreateRelTable,
    CreateRelationships,
    CreateVectorSpace,
    DeleteEntities,
    FilterRows,
    IndexSeek,
    MergePattern,
    NodeScan,
    ProduceResults,
    SetProperties,
    SingleRow,
    TraverseRelationship,
    VectorSearch,
)
from okto_grafx.domain.query.planner import SCORE_COLUMN, build_plan
from okto_grafx.domain.query.parser import parse
from tests.query.conftest import build_catalog, find_operator, operators, plan_text


# --- shape ----------------------------------------------------------------------------------


def test_a_plan_is_rooted_at_the_result_it_produces() -> None:
    planned = plan_text("MATCH (p:Person) RETURN p.name")
    assert isinstance(planned.root, ProduceResults)
    assert planned.columns == ("p.name",)


def test_a_scan_is_driven_by_a_single_row_source() -> None:
    planned = plan_text("MATCH (p:Person) RETURN p.name")
    scan = find_operator(planned.root, "NodeScan")
    assert isinstance(scan, NodeScan)
    assert isinstance(scan.child, SingleRow)
    assert scan.table.name == "Person"


def test_two_patterns_nest_into_one_tree_rather_than_two_queries() -> None:
    planned = plan_text("MATCH (a:Person), (b:Doc) RETURN a.id, b.id")
    names = operators(planned.root)
    assert names.count("NodeScan") == 2
    assert "CartesianProduct" not in names


def test_a_traversal_hangs_off_the_operator_that_bound_its_source() -> None:
    planned = plan_text("MATCH (a:Person)-[:Knows]->(b:Person) RETURN b.name")
    traversal = find_operator(planned.root, "TraverseRelationship")
    assert isinstance(traversal, TraverseRelationship)
    assert traversal.source == "a"
    assert traversal.target == "b"
    assert traversal.table.name == "Knows"
    assert isinstance(traversal.child, NodeScan)


def test_a_hop_range_reaches_the_traversal_operator() -> None:
    planned = plan_text("MATCH (a:Person)-[:Knows*2..4]->(b:Person) RETURN b.name")
    traversal = find_operator(planned.root, "TraverseRelationship")
    assert isinstance(traversal, TraverseRelationship)
    assert (traversal.min_hops, traversal.max_hops) == (2, 4)


def test_a_traversal_records_that_its_target_was_already_bound() -> None:
    planned = plan_text("MATCH (a:Person), (b:Person) MATCH (a)-[:Knows]->(b) RETURN a.id")
    traversal = find_operator(planned.root, "TraverseRelationship")
    assert isinstance(traversal, TraverseRelationship)
    assert traversal.target_bound is True


def test_the_result_operators_are_stacked_in_the_order_the_language_defines() -> None:
    planned = plan_text(
        "MATCH (p:Person) RETURN DISTINCT p.age AS a ORDER BY a DESC SKIP 1 LIMIT 2"
    )
    names = operators(planned.root)
    order = [
        names.index(label)
        for label in ("LimitRows", "SkipRows", "SortRows", "DistinctRows", "ProjectRows")
    ]
    assert order == sorted(order)


def test_a_group_is_formed_before_the_projection_reads_it() -> None:
    planned = plan_text("MATCH (p:Person) RETURN p.city AS city, count(*) AS total")
    names = operators(planned.root)
    assert names.index("ProjectRows") < names.index("AggregateRows")


def test_a_residual_predicate_becomes_one_filter_over_the_pattern() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.age > 30 AND p.name <> 'x' RETURN p.id")
    predicate = find_operator(planned.root, "FilterRows")
    assert isinstance(predicate, FilterRows)
    assert predicate.predicate.describe() == "((p.age > 30) AND (p.name <> 'x'))"


def test_an_inline_property_map_becomes_an_equality_the_planner_can_use() -> None:
    planned = plan_text("MATCH (p:Person {id: 4}) RETURN p.name")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.key_values == (Literal(value=4),)


def test_a_query_with_no_pattern_reads_a_single_row() -> None:
    planned = plan_text("RETURN 1 AS one")
    assert operators(planned.root) == ("ProduceResults", "ProjectRows", "SingleRow")


# --- index selection ------------------------------------------------------------------------


def test_an_equality_on_an_indexed_column_becomes_a_seek() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = 7 RETURN p.name")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.index == "person_id"
    assert seek.key_columns == ("id",)


def test_the_seek_carries_the_visibility_the_index_declared() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = 7 RETURN p.name")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.visibility is IndexVisibility.EXACT


def test_a_proximity_index_is_recorded_as_proximity_and_not_as_exact() -> None:
    planned = plan_text("MATCH (n:Chunk) WHERE n.layer = 3 RETURN n.id")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.visibility is IndexVisibility.PROXIMITY


def test_the_conjunct_a_seek_consumed_does_not_stay_as_a_filter() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = 7 RETURN p.name")
    assert "FilterRows" not in operators(planned.root)


def test_a_conjunct_the_seek_did_not_consume_stays_as_a_filter() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = 7 AND p.age > 3 RETURN p.name")
    predicate = find_operator(planned.root, "FilterRows")
    assert isinstance(predicate, FilterRows)
    assert predicate.predicate.describe() == "(p.age > 3)"


def test_a_composite_index_is_keyed_in_the_order_it_declared() -> None:
    # The conditions are written age first, the index is declared city first, and the key has to
    # follow the index or it names bytes that were never written.
    planned = plan_text("MATCH (p:Person) WHERE p.age = 3 AND p.city = 'x' RETURN p.id")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.index == "person_city_age"
    assert seek.key_columns == ("city", "age")
    assert seek.key_values == (Literal(value="x"), Literal(value=3))


def test_a_composite_index_is_not_used_for_part_of_its_key() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.city = 'x' RETURN p.id")
    assert "IndexSeek" not in operators(planned.root)
    assert "NodeScan" in operators(planned.root)


def test_a_range_predicate_never_becomes_a_seek() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id > 7 RETURN p.name")
    assert "IndexSeek" not in operators(planned.root)


def test_an_equality_against_another_column_never_becomes_a_seek() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = p.age RETURN p.name")
    assert "IndexSeek" not in operators(planned.root)


def test_a_parameter_is_a_usable_seek_key() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = $wanted RETURN p.name")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.key_values == (Parameter(name="wanted"),)


def test_an_equality_written_the_other_way_round_is_still_a_seek() -> None:
    planned = plan_text("MATCH (p:Person) WHERE 7 = p.id RETURN p.name")
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.key_values == (Literal(value=7),)


def test_the_widest_index_wins_when_two_could_answer() -> None:
    planned = plan_text(
        "MATCH (p:Person) WHERE p.id = 1 AND p.city = 'x' AND p.age = 2 RETURN p.name"
    )
    seek = find_operator(planned.root, "IndexSeek")
    assert isinstance(seek, IndexSeek)
    assert seek.index == "person_city_age"


def test_the_choice_does_not_depend_on_the_order_indexes_were_registered() -> None:
    text = "MATCH (p:Person) WHERE p.id = 1 AND p.city = 'x' AND p.age = 2 RETURN p.name"
    forward = plan_text(text)
    from tests.query.conftest import build_indexes

    backward = plan_text(text, indexes=tuple(reversed(build_indexes())))
    assert forward.describe() == backward.describe()


def test_a_query_with_no_index_at_all_still_plans() -> None:
    planned = plan_text("MATCH (p:Person) WHERE p.id = 1 RETURN p.name", indexes=())
    assert "NodeScan" in operators(planned.root)
    assert "FilterRows" in operators(planned.root)


def test_an_index_on_another_table_is_never_chosen() -> None:
    other = (
        IndexDefinition(
            name="doc_id",
            table_id=3,
            table_name="Doc",
            positions=(0,),
            visibility=IndexVisibility.EXACT,
        ),
    )
    planned = plan_text("MATCH (p:Person) WHERE p.id = 1 RETURN p.name", indexes=other)
    assert "IndexSeek" not in operators(planned.root)


# --- similarity -----------------------------------------------------------------------------

HYBRID: str = (
    "MATCH (n:Chunk)-[:BELONGS_TO]->(d:Doc) "
    "WHERE n.layer = $layer AND d.active = true "
    "AND similarity(n.embedding, $q, space => 'minilm_v2') > 0.7 "
    "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 10"
)


def test_the_hybrid_query_is_one_tree_with_the_filters_below_the_search() -> None:
    planned = plan_text(HYBRID)
    names = operators(planned.root)
    assert names.count("VectorSearch") == 1
    search_at = names.index("VectorSearch")
    assert all(names.index(label) > search_at for label in ("TraverseRelationship",))
    filters = [index for index, label in enumerate(names) if label == "FilterRows"]
    assert filters and all(index > search_at for index in filters)


def test_the_search_reads_the_candidate_set_as_its_child() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert isinstance(search.child, FilterRows)


def test_the_threshold_is_pushed_into_the_search_rather_than_left_above_it() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.threshold_operator == ">"
    assert search.threshold == Literal(value=0.7)


def test_a_limit_over_a_score_sort_becomes_the_neighbour_count() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k == Literal(value=10)


def test_a_skip_is_added_to_the_neighbour_count_rather_than_dropped() -> None:
    planned = plan_text(HYBRID.replace("LIMIT 10", "SKIP 5 LIMIT 10"))
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k == BinaryOperation(
        operator="+", left=Literal(value=10), right=Literal(value=5)
    )


def test_a_search_with_no_limit_scores_every_candidate() -> None:
    planned = plan_text(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'minilm_v2') > 0.2 "
        "RETURN n.id, similarity_score() AS score"
    )
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k is None
    assert search.bounded is False


def test_a_limit_is_not_fused_when_distinct_could_drop_a_row() -> None:
    planned = plan_text(HYBRID.replace("RETURN n.id", "RETURN DISTINCT n.id"))
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k is None


def test_a_limit_is_not_fused_when_the_sort_is_not_the_score() -> None:
    planned = plan_text(HYBRID.replace("ORDER BY score DESC", "ORDER BY n.id DESC"))
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k is None


def test_a_limit_is_not_fused_when_the_score_sort_is_ascending() -> None:
    planned = plan_text(HYBRID.replace("ORDER BY score DESC", "ORDER BY score ASC"))
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k is None


def test_a_limit_is_not_fused_when_a_predicate_still_reads_the_score() -> None:
    text = (
        "MATCH (n:Chunk) "
        "WHERE similarity(n.embedding, $q, space => 'minilm_v2') * 2 > 1.0 "
        "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 10"
    )
    planned = plan_text(text)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.k is None
    assert search.threshold is None


def test_the_score_column_is_the_one_the_planner_publishes() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.score_column == SCORE_COLUMN


def test_the_column_space_travels_into_the_plan() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.column_space == "minilm_v2"


def test_a_search_over_a_column_of_another_space_is_refused_as_a_mismatch() -> None:
    with pytest.raises(GrafxEmbeddingSpaceMismatch) as failure:
        plan_text(
            "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'other_space') > 0.1 "
            "RETURN n.id"
        )
    assert failure.value.code == "embedding_space_mismatch"
    assert failure.value.details["column_space"] == "minilm_v2"


def test_a_search_naming_a_space_the_catalog_does_not_have_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text(
            "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'nope') > 0.1 "
            "RETURN n.id"
        )
    assert failure.value.details["field"] == "space"


def test_a_search_over_a_column_that_stores_no_vector_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text(
            "MATCH (p:Person) WHERE similarity(p.name, $q, space => 'minilm_v2') > 0.1 "
            "RETURN p.id"
        )
    assert failure.value.details["field"] == "column"


def test_a_search_whose_space_is_a_parameter_still_plans() -> None:
    planned = plan_text(
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => $space) > 0.1 RETURN n.id"
    )
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    assert search.space == Parameter(name="space")
    assert search.column_space == "minilm_v2"


# --- writes ---------------------------------------------------------------------------------


def test_a_create_clause_becomes_one_write_operator() -> None:
    planned = plan_text("CREATE (:Person {id: 1, name: 'Ada'})")
    write = find_operator(planned.root, "CreateRelationships")
    assert isinstance(write, CreateRelationships)
    assert write.nodes[0].table is not None
    assert write.nodes[0].table.name == "Person"
    assert planned.writes is True


def test_a_created_relationship_is_stored_from_its_declared_start() -> None:
    planned = plan_text("CREATE (a:Person)<-[:Knows]-(b:Person)")
    write = find_operator(planned.root, "CreateRelationships")
    assert isinstance(write, CreateRelationships)
    relationship = write.relationships[0]
    assert (relationship.source, relationship.target) == (
        write.nodes[1].variable,
        write.nodes[0].variable,
    )
    assert relationship.direction is Direction.OUTGOING


def test_a_created_node_that_reuses_a_binding_carries_no_table() -> None:
    planned = plan_text("MATCH (a:Person) CREATE (a)-[:Knows]->(b:Person)")
    write = find_operator(planned.root, "CreateRelationships")
    assert isinstance(write, CreateRelationships)
    assert write.nodes[0].table is None
    assert write.nodes[1].table is not None


def test_properties_may_not_be_given_to_a_node_an_earlier_clause_bound() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (a:Person) CREATE (a {age: 1})-[:Knows]->(b:Person)")
    assert failure.value.details["field"] == "properties"


def test_a_relationship_written_between_the_wrong_tables_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE (a:Person)-[:BELONGS_TO]->(b:Doc)")
    assert failure.value.details["field"] == "endpoint"


def test_a_set_clause_becomes_one_write_operator() -> None:
    planned = plan_text("MATCH (p:Person) SET p.age = 1")
    write = find_operator(planned.root, "SetProperties")
    assert isinstance(write, SetProperties)
    assert write.assignments[0].describe() == "p.age = 1"


def test_a_delete_clause_carries_the_detach_flag() -> None:
    planned = plan_text("MATCH (p:Person) DETACH DELETE p")
    write = find_operator(planned.root, "DeleteEntities")
    assert isinstance(write, DeleteEntities)
    assert write.variables == ("p",)
    assert write.detach is True


def test_a_merge_of_one_node_plans() -> None:
    planned = plan_text("MERGE (p:Person {id: 3})")
    merge = find_operator(planned.root, "MergePattern")
    assert isinstance(merge, MergePattern)
    assert merge.relationships == ()


def test_a_merge_of_a_relationship_between_bound_nodes_plans() -> None:
    planned = plan_text(
        "MATCH (a:Person), (b:Person) MERGE (a)-[:Knows]->(b)"
    )
    merge = find_operator(planned.root, "MergePattern")
    assert isinstance(merge, MergePattern)
    assert len(merge.relationships) == 1


def test_a_merge_of_a_relationship_with_an_unmatched_end_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MERGE (a:Person)-[:Knows]->(b:Person)")
    assert failure.value.details["field"] == "pattern"


def test_a_merge_of_a_longer_path_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text(
            "MATCH (a:Person), (b:Person), (c:Person) MERGE (a)-[:Knows]->(b)-[:Knows]->(c)"
        )
    assert failure.value.details["field"] == "pattern"


# --- schema ---------------------------------------------------------------------------------


def test_a_node_table_plan_resolves_its_column_types() -> None:
    planned = plan_text("CREATE NODE TABLE Team(id INT64, label STRING, PRIMARY KEY(id))")
    assert isinstance(planned.root, CreateNodeTable)
    assert [column.type for column in planned.root.columns] == [
        ValueType.INT64,
        ValueType.STRING,
    ]


def test_the_primary_key_column_is_not_nullable() -> None:
    planned = plan_text("CREATE NODE TABLE Team(id INT64, label STRING, PRIMARY KEY(id))")
    assert isinstance(planned.root, CreateNodeTable)
    assert planned.root.columns[0].nullable is False
    assert planned.root.columns[1].nullable is True


def test_a_vector_column_takes_its_precision_from_the_space_it_names() -> None:
    planned = plan_text("CREATE NODE TABLE Note(v VECTOR(minilm_v2))")
    assert isinstance(planned.root, CreateNodeTable)
    assert planned.root.columns[0].type is ValueType.VECTOR_F32
    assert planned.root.columns[0].vector_space == "minilm_v2"


def test_a_vector_column_naming_a_space_the_catalog_lacks_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE NODE TABLE Note(v VECTOR(nope))")
    assert failure.value.details["field"] == "space"


def test_a_primary_key_that_is_not_a_column_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE NODE TABLE Team(id INT64, PRIMARY KEY(other))")
    assert failure.value.details["field"] == "primary_key"


def test_a_column_declared_twice_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE NODE TABLE Team(id INT64, id STRING)")
    assert failure.value.details["value"] == "id"


def test_a_relationship_table_plan_names_both_endpoints() -> None:
    planned = plan_text("CREATE REL TABLE Wrote(FROM Person TO Doc, at INT64)")
    assert isinstance(planned.root, CreateRelTable)
    assert (planned.root.from_table, planned.root.to_table) == ("Person", "Doc")


def test_a_relationship_table_between_relationship_tables_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE REL TABLE Bad(FROM Knows TO Person)")
    assert failure.value.details["field"] == "from"


def test_a_relationship_table_naming_an_unknown_endpoint_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE REL TABLE Bad(FROM Nope TO Person)")
    assert failure.value.details["field"] == "from"


def test_a_vector_space_plan_carries_its_resolved_options() -> None:
    planned = plan_text(
        "CREATE VECTOR SPACE big {dimension: 8, metric: 'euclidean', "
        "normalized: true, storage_dtype: 'float64'}"
    )
    assert isinstance(planned.root, CreateVectorSpace)
    assert planned.root.dimension == 8
    assert planned.root.metric is DistanceMetric.EUCLIDEAN
    assert planned.root.normalized is True
    assert planned.root.storage_dtype == "float64"


def test_a_vector_space_takes_the_frozen_defaults_it_did_not_declare() -> None:
    planned = plan_text("CREATE VECTOR SPACE big {dimension: 8, metric: 'cosine'}")
    assert isinstance(planned.root, CreateVectorSpace)
    assert planned.root.normalized is False
    assert planned.root.storage_dtype == "float32"


def test_a_vector_space_with_an_unknown_option_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE VECTOR SPACE big {dimension: 8, metric: 'cosine', extra: 1}")
    assert failure.value.details["value"] == "extra"


def test_a_vector_space_with_no_dimension_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE VECTOR SPACE big {metric: 'cosine'}")
    assert failure.value.details["field"] == "dimension"


def test_a_vector_space_with_an_unknown_metric_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("CREATE VECTOR SPACE big {dimension: 8, metric: 'hamming'}")
    assert failure.value.details["field"] == "metric"


@pytest.mark.parametrize(
    "options",
    [
        "{dimension: 'eight', metric: 'cosine'}",
        "{dimension: 8, metric: 4}",
        "{dimension: 8, metric: 'cosine', normalized: 1}",
        "{dimension: true, metric: 'cosine'}",
    ],
)
def test_a_vector_space_option_of_the_wrong_kind_is_refused(options: str) -> None:
    with pytest.raises(GrafxPlanError):
        plan_text(f"CREATE VECTOR SPACE big {options}")


# --- catalog binding ------------------------------------------------------------------------


def test_a_node_pattern_with_no_label_is_refused_outside_its_one_shape() -> None:
    # A named label-free node on its own is the polymorphic scan; beside a second pattern it is
    # not, and the refusal says which shape it is missing rather than naming the label rule.
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (n), (m:Person) RETURN n.id")
    assert failure.value.details["field"] == "pattern"
    assert "exactly one shape" in str(failure.value)


def test_a_node_pattern_with_two_labels_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (n:Person:Doc) RETURN n.id")
    assert failure.value.details["field"] == "labels"


def test_a_relationship_pattern_with_no_type_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (a:Person)-[r]->(b:Person) RETURN b.id")
    assert failure.value.details["field"] == "types"


def test_a_label_that_names_a_relationship_table_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (n:Knows) RETURN n.since")
    assert failure.value.details["field"] == "label"


def test_a_type_that_names_a_node_table_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (a:Person)-[:Doc]->(b:Person) RETURN b.id")
    assert failure.value.details["field"] == "type"


def test_a_traversal_that_could_match_nothing_is_refused_rather_than_run() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (p:Person)-[:BELONGS_TO]->(d:Doc) RETURN d.id")
    assert failure.value.details["field"] == "from_table"


def test_an_incoming_traversal_checks_the_other_endpoint() -> None:
    planned = plan_text("MATCH (d:Doc)<-[:BELONGS_TO]-(n:Chunk) RETURN n.id")
    traversal = find_operator(planned.root, "TraverseRelationship")
    assert isinstance(traversal, TraverseRelationship)
    assert traversal.direction is Direction.INCOMING


def test_a_predicate_naming_a_column_the_table_lacks_is_refused() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        plan_text("MATCH (p:Person) WHERE p.nope = 1 RETURN p.id")
    assert failure.value.details["value"] == "nope"


def test_planning_needs_a_real_catalog() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        build_plan(parse("RETURN 1"), catalog=object())  # type: ignore[arg-type]
    assert failure.value.details["field"] == "catalog"


def test_planning_the_same_text_twice_gives_the_same_tree() -> None:
    # Determinism of the plan is what makes explain worth reading and a regression visible.
    catalog = build_catalog()
    first = plan_text(HYBRID, catalog=catalog)
    second = plan_text(HYBRID, catalog=catalog)
    assert first.describe() == second.describe()
    assert first.root.to_dict() == second.root.to_dict()
