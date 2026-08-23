"""The executor: the rows a statement really produces, against the real stores."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxEmbeddingSpaceMismatch,
    GrafxParseError,
    GrafxPlanError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.query.plan import ProduceResults, VectorSearch
from okto_grafx.engine.query_engine import (
    PHASE_EXECUTE,
    PHASE_PARSE,
    PHASE_PLAN,
    QueryEngine,
    QueryResult,
)
from tests.query.stack import QueryStack, build_query_stack, vector

PEOPLE: tuple[tuple[int, str, int | None, str | None], ...] = (
    (1, "Ada", 36, "London"),
    (2, "Grace", 45, "New York"),
    (3, "Alan", 41, "London"),
    (4, "Edsger", None, "Rotterdam"),
    (5, "Barbara", 52, None),
)


@pytest.fixture
def stack() -> QueryStack:
    """Return a database carrying five people."""
    built = build_query_stack()
    for record_id, name, age, city in PEOPLE:
        built.insert("Person", record_id, (record_id, name, age, city), csn=1)
    return built


def run(stack: QueryStack, text: str, parameters: dict[str, object] | None = None) -> QueryResult:
    """Run one statement against the stack under a snapshot that sees everything committed."""
    return stack.engine.execute(text, stack.transaction(read_lsn=1000), parameters)


def names(result: QueryResult) -> list[object]:
    """Return the first column of every row."""
    return [row[0] for row in result.rows]


# --- reading ----------------------------------------------------------------------------------


def test_a_scan_returns_every_visible_row(stack: QueryStack) -> None:
    assert sorted(names(run(stack, "MATCH (p:Person) RETURN p.name"))) == sorted(
        person[1] for person in PEOPLE
    )


def test_the_result_carries_the_column_names_the_query_gave(stack: QueryStack) -> None:
    result = run(stack, "MATCH (p:Person) RETURN p.name AS who, p.age")
    assert result.columns == ("who", "p.age")


def test_a_snapshot_hides_a_row_committed_after_it_opened(stack: QueryStack) -> None:
    stack.insert("Person", 9, (9, "Later", 20, "Paris"), csn=500)
    early = stack.engine.execute(
        "MATCH (p:Person) RETURN p.name", stack.transaction(read_lsn=100)
    )
    late = stack.engine.execute(
        "MATCH (p:Person) RETURN p.name", stack.transaction(read_lsn=1000)
    )
    assert "Later" not in names(early)
    assert "Later" in names(late)


def test_a_snapshot_still_sees_a_row_a_later_commit_deleted(stack: QueryStack) -> None:
    ref = stack.insert("Person", 8, (8, "Doomed", 30, "Paris"), csn=10)
    stack.end("Person", ref, (8, "Doomed", 30, "Paris"), csn=500)
    before = stack.engine.execute(
        "MATCH (p:Person) RETURN p.name", stack.transaction(read_lsn=100)
    )
    after = stack.engine.execute(
        "MATCH (p:Person) RETURN p.name", stack.transaction(read_lsn=1000)
    )
    assert "Doomed" in names(before)
    assert "Doomed" not in names(after)


def test_a_filter_keeps_only_the_rows_whose_predicate_is_true(stack: QueryStack) -> None:
    assert sorted(names(run(stack, "MATCH (p:Person) WHERE p.age > 40 RETURN p.name"))) == [
        "Alan",
        "Barbara",
        "Grace",
    ]


def test_a_predicate_that_is_unknown_drops_the_row(stack: QueryStack) -> None:
    # Edsger's age is null, so "age > 40" is unknown rather than false, and unknown must not
    # keep the row any more than false does.
    assert "Edsger" not in names(run(stack, "MATCH (p:Person) WHERE p.age > 40 RETURN p.name"))
    assert "Edsger" not in names(
        run(stack, "MATCH (p:Person) WHERE NOT p.age > 40 RETURN p.name")
    )


def test_is_null_finds_what_a_comparison_with_null_cannot(stack: QueryStack) -> None:
    assert names(run(stack, "MATCH (p:Person) WHERE p.age IS NULL RETURN p.name")) == ["Edsger"]
    assert names(run(stack, "MATCH (p:Person) WHERE p.age = null RETURN p.name")) == []


def test_and_is_false_as_soon_as_either_side_is(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) WHERE p.age > 40 AND p.city = 'Nowhere' RETURN p.name")
    assert found.rows == ()


def test_or_is_true_as_soon_as_either_side_is(stack: QueryStack) -> None:
    found = run(
        stack, "MATCH (p:Person) WHERE p.name = 'Edsger' OR p.age > 100 RETURN p.name"
    )
    assert names(found) == ["Edsger"]


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ("p.name STARTS WITH 'A'", ["Ada", "Alan"]),
        ("p.name ENDS WITH 'a'", ["Ada", "Barbara"]),
        ("p.name CONTAINS 'ra'", ["Barbara", "Grace"]),
        ("p.id IN [1, 3]", ["Ada", "Alan"]),
        ("p.age <> 36", ["Alan", "Barbara", "Grace"]),
        ("p.city = 'London'", ["Ada", "Alan"]),
    ],
)
def test_each_comparison_selects_the_rows_it_names(
    stack: QueryStack, predicate: str, expected: list[str]
) -> None:
    found = run(stack, f"MATCH (p:Person) WHERE {predicate} RETURN p.name ORDER BY p.name")
    assert names(found) == expected


def test_arithmetic_is_evaluated_over_the_row(stack: QueryStack) -> None:
    found = run(
        stack, "MATCH (p:Person) WHERE p.id = 1 RETURN p.age + 1 AS next, p.age * 2 AS twice"
    )
    assert found.rows == ((37, 72),)


def test_integer_division_truncates_towards_zero() -> None:
    built = build_query_stack()
    found = built.engine.execute(
        "RETURN 7 / 2 AS positive, -7 / 2 AS negative, -7 % 2 AS remainder",
        built.transaction(),
    )
    assert found.rows == ((3, -3, -1),)


def test_dividing_by_zero_is_a_typed_refusal(stack: QueryStack) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        run(stack, "RETURN 1 / 0")
    assert failure.value.details["field"] == "operator"


# --- ordering ---------------------------------------------------------------------------------


def test_order_by_sorts_ascending_by_default(stack: QueryStack) -> None:
    assert names(run(stack, "MATCH (p:Person) RETURN p.name ORDER BY p.name")) == [
        "Ada",
        "Alan",
        "Barbara",
        "Edsger",
        "Grace",
    ]


def test_null_sorts_last_ascending_and_first_descending(stack: QueryStack) -> None:
    ascending = names(run(stack, "MATCH (p:Person) RETURN p.age ORDER BY p.age"))
    descending = names(run(stack, "MATCH (p:Person) RETURN p.age ORDER BY p.age DESC"))
    assert ascending[-1] is None
    assert descending[0] is None


def test_two_sort_keys_are_applied_most_significant_first(stack: QueryStack) -> None:
    found = run(
        stack,
        "MATCH (p:Person) RETURN p.city AS city, p.name AS who ORDER BY city, who DESC",
    )
    assert found.rows[:2] == (("London", "Alan"), ("London", "Ada"))


def test_an_order_by_key_may_name_an_alias(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN p.name AS who ORDER BY who DESC")
    assert names(found)[0] == "Grace"


def test_sorting_never_raises_on_a_column_of_mixed_kinds() -> None:
    # A total order across kinds is what keeps a heterogeneous column from raising TypeError in
    # the middle of a sort, which would be a crash out of a public door.
    built = build_query_stack()
    built.insert("Person", 1, (1, "text", 1, None))
    built.insert("Person", 2, (2, None, 2, None))
    found = built.engine.execute(
        "MATCH (p:Person) RETURN p.name ORDER BY p.name", built.transaction()
    )
    assert len(found.rows) == 2


def test_the_same_query_answers_in_the_same_order_every_time(stack: QueryStack) -> None:
    text = "MATCH (p:Person) RETURN p.city AS city, p.name AS who ORDER BY city"
    answers = {run(stack, text).rows for _ in range(8)}
    assert len(answers) == 1


# --- windows and duplicates -------------------------------------------------------------------


def test_distinct_removes_duplicate_projections(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN DISTINCT p.city AS city ORDER BY city")
    assert names(found) == ["London", "New York", "Rotterdam", None]


def test_skip_and_limit_take_a_window_of_the_ordered_rows(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN p.name ORDER BY p.name SKIP 1 LIMIT 2")
    assert names(found) == ["Alan", "Barbara"]


def test_a_limit_of_zero_returns_nothing(stack: QueryStack) -> None:
    assert run(stack, "MATCH (p:Person) RETURN p.name LIMIT 0").rows == ()


def test_a_row_window_may_arrive_as_a_parameter(stack: QueryStack) -> None:
    found = run(
        stack,
        "MATCH (p:Person) RETURN p.name ORDER BY p.name SKIP $from LIMIT $count",
        {"from": 3, "count": 1},
    )
    assert names(found) == ["Edsger"]


# --- aggregation ------------------------------------------------------------------------------


def test_an_aggregate_groups_by_everything_else(stack: QueryStack) -> None:
    found = run(
        stack, "MATCH (p:Person) RETURN p.city AS city, count(*) AS total ORDER BY city"
    )
    assert found.rows[:3] == (("London", 2), ("New York", 1), ("Rotterdam", 1))


def test_counting_a_column_skips_the_rows_where_it_is_null(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN count(p.age) AS counted, count(*) AS rows")
    assert found.rows == ((4, 5),)


def test_an_aggregate_over_no_rows_still_answers() -> None:
    built = build_query_stack()
    found = built.engine.execute(
        "MATCH (p:Person) RETURN count(*) AS total", built.transaction()
    )
    assert found.rows == ((0,),)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("sum(p.age)", 174.0),
        ("avg(p.age)", 43.5),
        ("min(p.age)", 36),
        ("max(p.age)", 52),
    ],
)
def test_each_aggregate_reports_what_it_names(
    stack: QueryStack, expression: str, expected: object
) -> None:
    found = run(stack, f"MATCH (p:Person) RETURN {expression} AS value")
    assert found.rows[0][0] == pytest.approx(expected)


def test_collect_gathers_the_values_of_its_group(stack: QueryStack) -> None:
    found = run(
        stack,
        "MATCH (p:Person) WHERE p.city = 'London' RETURN collect(p.name) AS everyone",
    )
    assert sorted(found.rows[0][0]) == ["Ada", "Alan"]


def test_a_distinct_aggregate_counts_each_value_once(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN count(DISTINCT p.city) AS cities")
    assert found.rows == ((3,),)


def test_an_aggregate_may_be_ordered_by_its_alias(stack: QueryStack) -> None:
    found = run(
        stack,
        "MATCH (p:Person) RETURN p.city AS city, count(*) AS total ORDER BY total DESC",
    )
    assert found.rows[0] == ("London", 2)


# --- index seeks ------------------------------------------------------------------------------


def test_an_exact_seek_returns_what_a_scan_and_a_filter_return(stack: QueryStack) -> None:
    seeking = run(stack, "MATCH (p:Person) WHERE p.id = 3 RETURN p.name")
    scanning = build_query_stack(with_indexes=False)
    for record_id, name, age, city in PEOPLE:
        scanning.insert("Person", record_id, (record_id, name, age, city), csn=1)
    scanned = scanning.engine.execute(
        "MATCH (p:Person) WHERE p.id = 3 RETURN p.name", scanning.transaction(read_lsn=1000)
    )
    assert seeking.rows == scanned.rows == (("Alan",),)


def test_an_exact_seek_drops_a_candidate_the_snapshot_cannot_see() -> None:
    # The heart of the dual visibility rule: an exact index answers with candidates, and a hit
    # whose row the snapshot cannot see must not reach the caller. If the seek returned the
    # entry without validating it, this query would answer with a row that does not exist yet.
    built = build_query_stack()
    built.insert("Person", 7, (7, "Future", 30, "Paris"), csn=900)
    early = built.engine.execute(
        "MATCH (p:Person) WHERE p.id = 7 RETURN p.name", built.transaction(read_lsn=100)
    )
    late = built.engine.execute(
        "MATCH (p:Person) WHERE p.id = 7 RETURN p.name", built.transaction(read_lsn=1000)
    )
    assert early.rows == ()
    assert late.rows == (("Future",),)


def test_an_exact_seek_drops_a_candidate_whose_row_was_deleted() -> None:
    built = build_query_stack()
    ref = built.insert("Person", 7, (7, "Gone", 30, "Paris"), csn=10)
    built.end("Person", ref, (7, "Gone", 30, "Paris"), csn=100)
    found = built.engine.execute(
        "MATCH (p:Person) WHERE p.id = 7 RETURN p.name", built.transaction(read_lsn=1000)
    )
    assert found.rows == ()


def test_a_proximity_seek_answers_from_its_own_entries(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) WHERE p.city = 'London' RETURN p.name ORDER BY p.name")
    assert names(found) == ["Ada", "Alan"]


def test_a_proximity_seek_drops_a_row_a_later_commit_ended() -> None:
    built = build_query_stack()
    built.insert("Person", 1, (1, "Stays", 30, "Rome"), csn=10)
    ref = built.insert("Person", 2, (2, "Goes", 31, "Rome"), csn=10)
    built.end("Person", ref, (2, "Goes", 31, "Rome"), csn=100)
    found = built.engine.execute(
        "MATCH (p:Person) WHERE p.city = 'Rome' RETURN p.name", built.transaction(read_lsn=1000)
    )
    assert names(found) == ["Stays"]


def test_a_seek_and_a_scan_agree_over_every_key(stack: QueryStack) -> None:
    # The differential is the point: whatever the index answers, it has to be the set the query
    # means, which is what a scan with the same predicate produces.
    scanning = build_query_stack(with_indexes=False)
    for record_id, name, age, city in PEOPLE:
        scanning.insert("Person", record_id, (record_id, name, age, city), csn=1)
    for record_id, _name, _age, _city in PEOPLE:
        text = f"MATCH (p:Person) WHERE p.id = {record_id} RETURN p.name"
        assert run(stack, text).rows == scanning.engine.execute(
            text, scanning.transaction(read_lsn=1000)
        ).rows


def test_a_seek_for_a_key_nothing_carries_returns_nothing(stack: QueryStack) -> None:
    assert run(stack, "MATCH (p:Person) WHERE p.id = 999 RETURN p.name").rows == ()


def test_a_seek_without_an_index_framework_refuses_rather_than_scanning() -> None:
    built = build_query_stack()
    engine = QueryEngine(
        catalog=built.catalog_store,
        heap=built.heap,
        pool=built.pool,
        metrics=built.metrics,
        clock=built.clock,
        indexes=None,
        vectors=built.vectors,
    )
    # Without the framework the planner sees no index at all, so this is a scan and it works.
    built.insert("Person", 1, (1, "Ada", 36, "London"))
    found = engine.execute(
        "MATCH (p:Person) WHERE p.id = 1 RETURN p.name", built.transaction()
    )
    assert found.rows == (("Ada",),)


# --- similarity -------------------------------------------------------------------------------


HYBRID: str = (
    "MATCH (n:Chunk) WHERE n.layer = $layer "
    "AND similarity(n.embedding, $q, space => 'minilm_v2') > 0.0 "
    "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 2"
)


@pytest.fixture
def chunks() -> QueryStack:
    """Return a database carrying four chunks with vectors in one embedding space."""
    built = build_query_stack()
    rows = (
        (10, 1, (1.0, 0.0, 0.0, 0.0)),
        (11, 1, (0.9, 0.1, 0.0, 0.0)),
        (12, 1, (0.0, 1.0, 0.0, 0.0)),
        (13, 2, (0.95, 0.05, 0.0, 0.0)),
    )
    for record_id, layer, components in rows:
        ref = built.insert("Chunk", record_id, (record_id, layer, vector(components)), csn=1)
        built.add_vector(record_id, ref, components, csn=1)
    return built


def test_the_hybrid_query_ranks_the_filtered_set_by_score(chunks: QueryStack) -> None:
    found = chunks.engine.execute(
        HYBRID, chunks.transaction(read_lsn=1000), {"layer": 1, "q": [1.0, 0.0, 0.0, 0.0]}
    )
    assert [row[0] for row in found.rows] == [10, 11]


def test_the_filter_really_excludes_the_rows_it_names(chunks: QueryStack) -> None:
    # Chunk 13 is the second-best match in the whole space and belongs to another layer, so a
    # plan that searched first and filtered afterwards would either return it or return fewer
    # rows than asked. Neither happens.
    found = chunks.engine.execute(
        HYBRID, chunks.transaction(read_lsn=1000), {"layer": 1, "q": [1.0, 0.0, 0.0, 0.0]}
    )
    assert 13 not in [row[0] for row in found.rows]
    assert len(found.rows) == 2


def test_a_threshold_removes_the_neighbours_below_it(chunks: QueryStack) -> None:
    text = (
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'minilm_v2') > 0.5 "
        "RETURN n.id, similarity_score() AS score ORDER BY score DESC"
    )
    found = chunks.engine.execute(
        text, chunks.transaction(read_lsn=1000), {"q": [1.0, 0.0, 0.0, 0.0]}
    )
    assert 12 not in [row[0] for row in found.rows]


def test_the_score_reaches_the_projection(chunks: QueryStack) -> None:
    found = chunks.engine.execute(
        HYBRID, chunks.transaction(read_lsn=1000), {"layer": 1, "q": [1.0, 0.0, 0.0, 0.0]}
    )
    assert found.rows[0][1] == pytest.approx(1.0)


def test_the_search_is_one_operator_over_the_filtered_child(chunks: QueryStack) -> None:
    plan = chunks.engine.explain(HYBRID)
    searches = [node for node in plan.walk() if isinstance(node, VectorSearch)]
    assert len(searches) == 1
    below = [node.label for node in searches[0].child.walk()]
    assert "FilterRows" in below


def test_a_space_named_by_a_parameter_is_checked_when_it_is_bound(chunks: QueryStack) -> None:
    text = (
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => $space) > 0.0 "
        "RETURN n.id"
    )
    with pytest.raises(GrafxEmbeddingSpaceMismatch) as failure:
        chunks.engine.execute(
            text,
            chunks.transaction(read_lsn=1000),
            {"q": [1.0, 0.0, 0.0, 0.0], "space": "other_space"},
        )
    assert failure.value.details["column_space"] == "minilm_v2"


def test_a_search_over_no_candidates_answers_with_no_rows(chunks: QueryStack) -> None:
    text = (
        "MATCH (n:Chunk) WHERE n.layer = 99 "
        "AND similarity(n.embedding, $q, space => 'minilm_v2') > 0.0 RETURN n.id"
    )
    found = chunks.engine.execute(
        text, chunks.transaction(read_lsn=1000), {"q": [1.0, 0.0, 0.0, 0.0]}
    )
    assert found.rows == ()


def test_a_search_without_the_vector_subsystem_refuses(chunks: QueryStack) -> None:
    engine = QueryEngine(
        catalog=chunks.catalog_store,
        heap=chunks.heap,
        pool=chunks.pool,
        metrics=chunks.metrics,
        clock=chunks.clock,
        indexes=chunks.indexes,
        vectors=None,
    )
    with pytest.raises(GrafxUnsupportedOperation) as failure:
        engine.execute(
            HYBRID, chunks.transaction(read_lsn=1000), {"layer": 1, "q": [1.0, 0.0, 0.0, 0.0]}
        )
    assert failure.value.details["value"] == "vectors"


def test_a_reference_vector_that_is_not_numbers_is_refused(chunks: QueryStack) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        chunks.engine.execute(
            HYBRID, chunks.transaction(read_lsn=1000), {"layer": 1, "q": "not a vector"}
        )
    assert failure.value.details["field"] == "query"


# --- schema -----------------------------------------------------------------------------------


def test_a_node_table_statement_installs_the_table_and_stages_its_pages() -> None:
    built = build_query_stack()
    transaction = built.transaction()
    result = built.engine.execute(
        "CREATE NODE TABLE Team(id INT64, label STRING, PRIMARY KEY(id))", transaction
    )
    assert built.catalog_store.catalog.has_table("Team")
    assert result.statistics["tables_created"] == 1
    assert transaction.staged_pages


def test_a_vector_space_statement_installs_the_space() -> None:
    built = build_query_stack()
    built.engine.execute(
        "CREATE VECTOR SPACE second {dimension: 8, metric: 'dot', normalized: true}",
        built.transaction(),
    )
    space = built.catalog_store.catalog.space("second")
    assert (space.dimension, space.metric.value, space.normalized) == (8, "dot", True)


def test_a_relationship_table_statement_installs_the_table() -> None:
    built = build_query_stack()
    built.engine.execute(
        "CREATE REL TABLE Wrote(FROM Person TO Chunk, at INT64)", built.transaction()
    )
    table = built.catalog_store.catalog.table("Wrote")
    assert (table.kind, table.from_table, table.to_table) == ("rel", "Person", "Chunk")


def test_a_table_with_a_vector_column_gets_the_index_that_makes_it_searchable() -> None:
    # An index covers a (table, space) pair, so the vector subsystem creates it when a table
    # declares the column rather than when the space is declared. This statement is the moment
    # the pair exists, so it is the statement that has to attach it; nothing else does, and a
    # column with no index is a column no query can ever search.
    built = build_query_stack()
    before = {index.name for index in built.indexes.indexes()}
    result = built.engine.execute(
        "CREATE NODE TABLE Note(id INT64, body VECTOR(minilm_v2), PRIMARY KEY(id))",
        built.transaction(),
    )
    after = {index.name for index in built.indexes.indexes()}
    assert result.statistics["indexes_attached"] == 1
    # Two indexes appear, and naming both is deliberate: the statement attaches the vector index
    # AND the index of the primary key it declares, and a test that only counted would not notice
    # if one of them stopped appearing.
    assert after - before == {"pk_Note", "vector_Note_minilm_v2"}


def test_a_table_with_no_vector_column_attaches_nothing() -> None:
    built = build_query_stack()
    before = {index.name for index in built.indexes.indexes()}
    result = built.engine.execute(
        "CREATE NODE TABLE Plain(id INT64, PRIMARY KEY(id))", built.transaction()
    )
    after = {index.name for index in built.indexes.indexes()}
    assert "indexes_attached" not in result.statistics
    # No VECTOR index -- which is what "attaches nothing" meant -- and exactly the primary-key
    # index the table declares, so an unexpected third one still fails this.
    assert after - before == {"pk_Plain"}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("true < 1", None),
        ("1 < true", None),
        ("true >= 0", None),
    ],
)
def test_ordering_a_boolean_against_a_number_is_unknown(
    stack: QueryStack, expression: str, expected: object
) -> None:
    # Python would answer these; the language does not, because a boolean is a condition rather
    # than a number. Equality has a second guard of its own, so this is pinned through the
    # ordering path, which the equality guard cannot mask (amendment A34).
    assert run(stack, f"RETURN {expression} AS answer").rows == ((expected,),)


@pytest.mark.parametrize("expression", ["true + 1", "1 * true", "false - 1", "true / 2"])
def test_arithmetic_over_a_boolean_is_refused(stack: QueryStack, expression: str) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        run(stack, f"RETURN {expression} AS answer")
    assert failure.value.details["field"] == "operator"


def test_a_vector_subsystem_that_cannot_attach_is_told_apart_from_a_missing_one() -> None:
    # Two refusals, two fields: without this the test below would pass whichever guard fired.
    class _WithoutAttach:
        """A vector subsystem that offers no attach door."""

    built = build_query_stack()
    engine = QueryEngine(
        catalog=built.catalog_store,
        heap=built.heap,
        pool=built.pool,
        metrics=built.metrics,
        clock=built.clock,
        indexes=built.indexes,
        vectors=_WithoutAttach(),
    )
    with pytest.raises(GrafxUnsupportedOperation) as failure:
        engine.execute(
            "CREATE NODE TABLE Note(id INT64, body VECTOR(minilm_v2))", built.transaction()
        )
    assert failure.value.details["field"] == "attach"


def test_a_vector_column_without_the_vector_subsystem_is_refused() -> None:
    built = build_query_stack()
    engine = QueryEngine(
        catalog=built.catalog_store,
        heap=built.heap,
        pool=built.pool,
        metrics=built.metrics,
        clock=built.clock,
        indexes=built.indexes,
        vectors=None,
    )
    with pytest.raises(GrafxUnsupportedOperation) as failure:
        engine.execute(
            "CREATE NODE TABLE Note(id INT64, body VECTOR(minilm_v2))", built.transaction()
        )
    assert failure.value.details["field"] == "component"
    assert failure.value.details["value"] == "vectors"


def test_a_column_attached_by_the_statement_is_searchable_at_once() -> None:
    # End to end for the attach: declare the table through the language, write a vector into the
    # index the statement created, and search it. If the attach were missing this would refuse.
    built = build_query_stack()
    built.engine.execute(
        "CREATE NODE TABLE Note(id INT64, body VECTOR(minilm_v2), PRIMARY KEY(id))",
        built.transaction(),
    )
    table = built.catalog_store.catalog.table("Note")
    ref = built.heap.insert(table, 1, (1, vector((1.0, 0.0, 0.0, 0.0))), 1)
    transaction = built.transaction()
    built.vectors.stage_insert("minilm_v2", 1, ref, (1.0, 0.0, 0.0, 0.0), 1, transaction)
    built.indexes.commit(transaction, 1)
    found = built.engine.execute(
        "MATCH (n:Note) WHERE similarity(n.body, $q, space => 'minilm_v2') > 0.5 "
        "RETURN n.id, similarity_score() AS score",
        built.transaction(read_lsn=1000),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert found.rows == ((1, pytest.approx(1.0)),)


def test_a_schema_statement_needs_a_transaction_to_stage_its_pages() -> None:
    built = build_query_stack()
    with pytest.raises(GrafxTransactionStateError) as failure:
        built.engine.execute("CREATE NODE TABLE Team(id INT64)", object())
    assert failure.value.details["field"] == "transaction"


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "operator"),
    [
        ("MATCH (p:Person) SET p.nickname = 'x'", "column"),
        ("MATCH (p:Person) SET q.age = 1", "variable"),
        ("MATCH (p:Person) DELETE q", "variable"),
    ],
)
def test_a_write_operator_naming_something_the_rows_do_not_carry_refuses(
    stack: QueryStack, text: str, operator: str
) -> None:
    # SET and DELETE run now, so what is worth pinning is what they still refuse: a column the
    # table does not declare, and a variable the rows reaching the operator never bound.
    with pytest.raises(GrafxPlanError) as failure:
        run(stack, text)
    assert failure.value.details["field"] == operator


def test_a_statement_without_a_snapshot_refuses(stack: QueryStack) -> None:
    with pytest.raises(GrafxTransactionStateError) as failure:
        stack.engine.execute("MATCH (p:Person) RETURN p.name", object())
    assert failure.value.details["field"] == "transaction"


def test_a_missing_parameter_is_refused_before_anything_runs(stack: QueryStack) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        run(stack, "MATCH (p:Person) WHERE p.id = $wanted RETURN p.name")
    assert failure.value.details["value"] == "wanted"


def test_parameters_that_are_not_a_mapping_are_refused(stack: QueryStack) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        stack.engine.execute(
            "MATCH (p:Person) RETURN p.name", stack.transaction(), ["not", "a", "mapping"]
        )
    assert failure.value.details["field"] == "parameters"


def test_a_query_that_does_not_parse_never_reaches_the_stores(stack: QueryStack) -> None:
    with pytest.raises(GrafxParseError):
        run(stack, "MATCH (")


# --- the doors --------------------------------------------------------------------------------


def test_explain_returns_the_tree_execute_walks(stack: QueryStack) -> None:
    text = "MATCH (p:Person) WHERE p.age > 40 RETURN p.name ORDER BY p.name LIMIT 2"
    explained = stack.engine.explain(text)
    executed = run(stack, text).plan
    assert isinstance(explained, ProduceResults)
    assert executed is not None
    assert explained.to_dict() == executed.to_dict()


def test_plan_accepts_the_snapshot_the_contract_names(stack: QueryStack) -> None:
    statement = stack.engine.parse("MATCH (p:Person) RETURN p.name")
    with_snapshot = stack.engine.plan(statement, stack.snapshot())
    without = stack.engine.plan(statement)
    assert with_snapshot.to_dict() == without.to_dict()


def test_the_result_can_be_read_as_mappings(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) WHERE p.id = 1 RETURN p.name AS who")
    assert found.dictionaries() == ({"who": "Ada"},)


def test_the_result_reports_its_own_size(stack: QueryStack) -> None:
    found = run(stack, "MATCH (p:Person) RETURN p.name")
    assert len(found) == len(PEOPLE)
    assert len(list(found)) == len(PEOPLE)


def test_the_engine_says_which_components_it_was_given(stack: QueryStack) -> None:
    assert "indexes" in repr(stack.engine)
    assert "vectors" in repr(stack.engine)


# --- metrics ----------------------------------------------------------------------------------


def test_each_phase_publishes_a_duration(stack: QueryStack) -> None:
    stack.metrics.emissions.clear()
    run(stack, "MATCH (p:Person) RETURN p.name")
    phases = {
        labels["phase"]
        for _kind, _value, labels in stack.metrics.named(
            "oktografx_query_phase_duration_seconds"
        )
    }
    assert phases == {PHASE_PARSE, PHASE_PLAN, PHASE_EXECUTE}


def test_the_row_count_is_published(stack: QueryStack) -> None:
    stack.metrics.emissions.clear()
    run(stack, "MATCH (p:Person) RETURN p.name")
    observed = stack.metrics.named("oktografx_query_rows_returned_count")
    assert [value for _kind, value, _labels in observed] == [float(len(PEOPLE))]


def test_a_refused_query_is_counted_under_its_code(stack: QueryStack) -> None:
    stack.metrics.emissions.clear()
    with pytest.raises(GrafxParseError):
        run(stack, "MATCH (")
    counted = stack.metrics.named("oktografx_query_errors_total")
    assert [labels["code"] for _kind, _value, labels in counted] == ["parse_error"]


def test_every_metric_this_engine_emits_is_in_the_frozen_catalog(stack: QueryStack) -> None:
    from okto_grafx.engine.metrics_catalog import metric_names

    stack.metrics.emissions.clear()
    run(stack, "MATCH (p:Person) RETURN p.name")
    emitted = {name for _kind, name, _value, _labels in stack.metrics.emissions}
    assert emitted <= metric_names()


def test_a_disabled_sink_costs_the_engine_no_emission() -> None:
    built = build_query_stack()
    built.metrics._enabled = False
    built.metrics.emissions.clear()
    built.insert("Person", 1, (1, "Ada", 36, "London"))
    built.engine.execute("MATCH (p:Person) RETURN p.name", built.transaction())
    assert built.metrics.emissions == []

# --- what a value IS, and what a condition IS -------------------------------------------------


@pytest.mark.parametrize("predicate", ["p.name", "p.age", "p.age + 1", "'text'", "1"])
def test_a_predicate_that_is_not_a_condition_is_refused(
    stack: QueryStack, predicate: str
) -> None:
    # Reading a predicate as Python truthiness would keep every non-empty string and every
    # non-zero number, which is a different query; dropping the row silently would answer a
    # mistyped predicate with an empty result and no reason. Neither is acceptable, so it is a
    # typed refusal that names what the predicate produced.
    with pytest.raises(GrafxPlanError) as failure:
        run(stack, f"MATCH (p:Person) WHERE {predicate} RETURN p.name")
    assert failure.value.details["field"] == "predicate"


def test_a_null_predicate_is_unknown_rather_than_an_error(stack: QueryStack) -> None:
    # Null is the third value of the logic, not a mistyped condition: the row goes, quietly.
    found = run(stack, "MATCH (p:Person) WHERE p.age > 1000 RETURN p.name")
    assert found.rows == ()


def test_a_boolean_column_is_a_condition_like_any_other() -> None:
    built = build_query_stack()
    built.engine.execute(
        "CREATE NODE TABLE Flagged(id INT64, live BOOL, PRIMARY KEY(id))",
        built.transaction(),
    )
    built.insert("Flagged", 1, (1, True))
    built.insert("Flagged", 2, (2, False))
    found = built.engine.execute(
        "MATCH (f:Flagged) WHERE f.live RETURN f.id", built.transaction(read_lsn=1000)
    )
    assert found.rows == ((1,),)


def test_two_carriers_of_one_map_are_the_same_value(stack: QueryStack) -> None:
    from collections import OrderedDict

    found = run(stack, "RETURN {a: 1, b: 2} = $other AS same", {"other": OrderedDict(a=1, b=2)})
    assert found.rows == ((True,),)


def test_a_list_literal_and_a_list_parameter_are_the_same_value(stack: QueryStack) -> None:
    found = run(stack, "RETURN [1, 2] = $other AS same", {"other": [1, 2]})
    assert found.rows == ((True,),)


def test_two_spellings_of_one_byte_string_are_the_same_value(stack: QueryStack) -> None:
    found = run(stack, "RETURN $left = $right AS same", {"left": b"x", "right": bytearray(b"x")})
    assert found.rows == ((True,),)


@pytest.mark.parametrize(
    ("expression", "arguments", "expected"),
    [
        ("1 = '1'", {}, False),
        ("true = 1", {}, False),
        ("1 = 1.0", {}, True),
        ("'a' = 'a'", {}, True),
        ("[1] = $other", {"other": [1, 2]}, False),
        ("{a: 1} = $other", {"other": {"a": 2}}, False),
        ("{a: 1} = $other", {"other": [1]}, False),
    ],
)
def test_equality_never_coerces_across_kinds(
    stack: QueryStack, expression: str, arguments: dict[str, object], expected: bool
) -> None:
    assert run(stack, f"RETURN {expression} AS same", arguments).rows == ((expected,),)


def test_distinct_still_tells_an_integer_from_a_double(stack: QueryStack) -> None:
    # Equality treats 1 and 1.0 as one number; DISTINCT is a different question and keeps them
    # apart, which is what the scalar tag in the freeze is for.
    found = run(stack, "RETURN [1, 1.0] AS pair")
    assert found.rows[0][0] == (1, 1.0)
    assert [type(item).__name__ for item in found.rows[0][0]] == ["int", "float"]


def test_the_freeze_keeps_an_integer_apart_from_a_double(stack: QueryStack) -> None:
    # Equality treats 1 and 1.0 as one number, and DISTINCT must not: they are two values of the
    # value system. The scalar tag is what keeps them apart, so it is pinned directly.
    from okto_grafx.engine.query_engine import _equal, _freeze

    assert _equal(1, 1.0) is True
    assert _freeze(1) != _freeze(1.0)
    assert _freeze(1) == _freeze(1)


def test_equality_and_distinct_agree_about_what_one_value_is(stack: QueryStack) -> None:
    # The two rules share one definition, so a pair that compares equal is a pair DISTINCT
    # collapses. Two definitions of "the same value" is how they drift.
    from okto_grafx.engine.query_engine import _equal, _freeze

    pairs = [
        ({"a": 1}, {"a": 1}),
        ((1, 2), [1, 2]),
        (b"x", bytearray(b"x")),
        ("a", "a"),
        (1, "1"),
        ({"a": 1}, {"a": 2}),
    ]
    for left, right in pairs:
        assert _equal(left, right) is (_freeze(left) == _freeze(right)), (left, right)
