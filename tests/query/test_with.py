"""The Pulse projection stage: a non-aggregating WITH that really replaces the scope.

Two frozen mutations drive this file. Both open on a MATCH, narrow the row twice through a
WITH, and only then write -- and each WHERE belongs to the stage above it rather than to the
pattern, so it reads names the projection has just created. What the stage does NOT carry is
as much of the contract as what it does: a variable a stage drops is out of reach below it,
which is what stops the second stage from reading a list the first one deliberately filtered.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.query.analysis import (
    ENTITY_NODE,
    ENTITY_PROJECTED,
    ENTITY_UNWOUND,
    Binding,
    QueryAnalysis,
    analyze,
)
from okto_grafx.domain.query.ast import (
    Parameter,
    Query,
    ReturnClause,
    ReturnItem,
    UnwindClause,
    Variable,
    WithClause,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan

I06 = (
    "MATCH (n:Decision) WHERE n.source_artifact_ref IS NOT NULL "
    "AND n.revocation_reason IS NULL "
    "WITH n, string_split(n.source_artifact_ref, ':') AS parts "
    "WHERE size(parts) >= 2 "
    "WITH n, CASE parts[0] WHEN 'card' THEN 'card' "
    "WHEN 'card_relationship_target' THEN 'card' WHEN 'task' THEN 'card' "
    "WHEN 'test' THEN 'card' WHEN 'bug' THEN 'card' ELSE parts[0] END AS owner_type, "
    "parts[1] AS owner_id "
    "WHERE owner_type = $owner_type AND owner_id = $owner_id "
    "SET n.pre_cancellation_relevance_score = n.relevance_score, "
    "n.relevance_score = CASE WHEN n.relevance_score - $penalty < 0.0 THEN 0.0 "
    "ELSE n.relevance_score - $penalty END, "
    "n.revocation_reason = $reason, n.superseded_by = $reason, n.superseded_at = $now "
    "RETURN n.id"
)
I07 = (
    "MATCH (n:Decision) WHERE n.source_artifact_ref IS NOT NULL "
    "AND n.revocation_reason = $reason "
    "AND (n.superseded_by IS NULL OR n.superseded_by = $reason) "
    "WITH n, string_split(n.source_artifact_ref, ':') AS parts "
    "WHERE size(parts) >= 2 "
    "WITH n, CASE parts[0] WHEN 'card' THEN 'card' "
    "WHEN 'card_relationship_target' THEN 'card' WHEN 'task' THEN 'card' "
    "WHEN 'test' THEN 'card' WHEN 'bug' THEN 'card' ELSE parts[0] END AS owner_type, "
    "parts[1] AS owner_id "
    "WHERE owner_type = $owner_type AND owner_id = $owner_id "
    "SET n.relevance_score = CASE WHEN n.pre_cancellation_relevance_score IS NULL "
    "THEN n.relevance_score + $penalty ELSE n.pre_cancellation_relevance_score END, "
    "n.pre_cancellation_relevance_score = NULL, n.revocation_reason = NULL, "
    "n.superseded_by = NULL, n.superseded_at = NULL "
    "RETURN n.id"
)
# owner_type and owner_id reach the statement through **owner_params rather than through the
# frozen parameter map, so a test that leaves them out is testing a different statement.
CANCEL = {
    "owner_type": "card",
    "owner_id": "c1",
    "penalty": 0.25,
    "reason": "cancelled",
    "now": "2026-08-26T12:00:00+00:00",
}
RESTORE = {
    "owner_type": "card",
    "owner_id": "c1",
    "penalty": 0.25,
    "reason": "cancelled",
}
# The same two stages with the guard removed: every row reaches parts[1], including the one
# whose reference carries no colon at all.
UNGUARDED = (
    "MATCH (n:Decision) "
    "WITH n, string_split(n.source_artifact_ref, ':') AS parts "
    "WITH n, parts[1] AS owner_id "
    "SET n.superseded_by = owner_id "
    "RETURN n.id"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return a database seeded with the reference shapes the two mutations sort through."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, source_artifact_ref STRING, relevance_score DOUBLE, "
            "pre_cancellation_relevance_score DOUBLE, revocation_reason STRING, "
            "superseded_by STRING, superseded_at STRING, PRIMARY KEY(id))"
        )
    with handle.begin("write") as seed:
        for identity, reference in (
            ("d1", "task:c1:9"),
            ("d2", "card:c1:8"),
            ("d3", "note:c1:7"),
            ("d4", "card:c2:6"),
            ("d5", "nocolon"),
        ):
            seed.execute(
                "CREATE (:Decision {id: $id, source_artifact_ref: $ref, "
                "relevance_score: 0.8})",
                {"id": identity, "ref": reference},
            )
    try:
        yield handle
    finally:
        handle.close()


def _operators(root: object) -> tuple[str, ...]:
    """Return the labels in one public plan, parents before children."""
    return tuple(node.label for node in root.walk())


def _scores(handle: object) -> tuple[tuple[object, ...], ...]:
    """Return what the two mutations read and write, for every row, in a stable order."""
    return handle.execute(
        "MATCH (n:Decision) RETURN n.id, n.relevance_score, "
        "n.pre_cancellation_relevance_score, n.revocation_reason, n.superseded_by "
        "ORDER BY n.id"
    ).rows


# --- the parser ------------------------------------------------------------------------------


def test_a_leading_with_is_a_query_of_its_own() -> None:
    statement = parse("WITH 1 AS value RETURN value")

    assert statement.describe() == "WITH 1 AS value RETURN value"
    assert len(statement.with_clauses) == 1
    assert statement.with_clauses[0].column_names() == ("value",)
    assert statement.with_clauses[0].predicate is None


def test_each_where_belongs_to_the_stage_it_was_written_under() -> None:
    statement = parse(
        "MATCH (n:Decision) WHERE n.id = $id "
        "WITH n, n.relevance_score AS score WHERE score > 0.5 "
        "WITH n, score AS kept "
        "RETURN kept"
    )

    first, second = statement.with_clauses
    assert statement.match_clauses[0].predicate.describe() == "(n.id = $id)"
    assert first.predicate.describe() == "(score > 0.5)"
    assert second.predicate is None
    assert statement.describe().count("WHERE") == 2


@pytest.mark.parametrize(
    ("query", "value"),
    [
        ("MATCH (n:Decision) WITH n WHERE n.id = 'd1' LIMIT 1 RETURN n.id", "LIMIT"),
        ("MATCH (n:Decision) WITH n MATCH (m:Decision) RETURN n.id", "MATCH"),
        ("MATCH (n:Decision) SET n.relevance_score = 1.0 WITH n RETURN n.id", "WITH"),
        ("UNWIND $rows AS r WITH r RETURN r", "WITH"),
    ],
)
def test_clause_order_admits_composition_but_not_misplaced_windows(
    database: object, query: str, value: str
) -> None:
    if value == "LIMIT":
        with pytest.raises(GrafxParseError, match="LIMIT"):
            parse(query)
        return
    tx = database.begin("write")
    try:
        result = tx.execute(query, {"rows": [1]} if query.startswith("UNWIND") else None)
        if query.startswith("UNWIND"):
            assert result.rows == ((1,),)
        else:
            assert result.columns == ("n.id",)
            assert len(result.rows) == (25 if value == "MATCH" else 5)
    finally:
        tx.rollback()


# --- the scope -------------------------------------------------------------------------------


def test_a_stage_replaces_the_scope_rather_than_adding_to_it() -> None:
    analysis = analyze(
        parse(
            "MATCH (n:Decision), (m:Decision) "
            "WITH n, n.relevance_score AS score "
            "RETURN score"
        )
    )

    assert [(b.name, b.entity) for b in analysis.bindings] == [
        ("n", ENTITY_NODE),
        ("score", ENTITY_PROJECTED),
    ]
    assert analysis.binding("n").labels == ("Decision",)


def test_a_variable_a_stage_dropped_is_refused_for_being_dropped() -> None:
    with pytest.raises(GrafxPlanError) as raised:
        analyze(parse("MATCH (n:Decision) WITH n.id AS kept RETURN n.id"))

    assert raised.value.details == {"field": "variable", "value": "n",
                                    "reason": "undefined_variable", "query_phase": "planning"}
    assert "dropped by a WITH clause" in str(raised.value)


@pytest.mark.parametrize(
    ("query", "field", "value", "reason"),
    [
        ("MATCH (n:Decision) WITH n, n.id RETURN n.id", "item", "n.id", "no_expression_alias"),
        ("WITH 1 AS a, 2 AS a RETURN a", "item", "a", "column_name_conflict"),
        ("MATCH (n:Decision) WITH n, n.id AS n RETURN n", "item", "n", "column_name_conflict"),
        ("WITH 1 AS a, a + 1 AS b RETURN b", "variable", "a", "undefined_variable"),
        (
            "MATCH (n:Decision) WITH n WHERE count(n) > 1 RETURN n.id",
            "expression",
            "(count(n) > 1)",
            "invalid_aggregation_context",
        ),
    ],
)
def test_a_stage_refuses_the_projections_that_have_no_single_meaning(
    query: str, field: str, value: str, reason: str
) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        analyze(parse(query))
    expected = {"field": field, "value": value, "reason": reason, "query_phase": "planning"}
    assert raised.value.details == expected


def test_a_projected_scalar_is_not_a_property_set_target() -> None:
    with pytest.raises(GrafxPlanError) as raised:
        analyze(parse("MATCH (n:Decision) WITH n.id AS ref SET ref.x = 1"))

    assert raised.value.details == {"field": "variable", "value": "ref"}
    assert "expression alias" in str(raised.value)


def test_a_projected_scalar_is_not_a_delete_target() -> None:
    # DELETE admits native entity-valued expressions; an alias's scalar type is
    # established by catalog-aware planning, not by its syntactic alias category.
    with okto_grafx.connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(:Decision {id:1})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH(n:Decision) WITH n.id AS ref DELETE ref")
            assert failure.value.details["reason"] == "delete_argument_type"
            assert failure.value.details["query_phase"] == "planning"
            assert tx.execute("MATCH(n:Decision) RETURN n.id").rows == ((1,),)


def test_a_later_stage_reads_what_an_earlier_stage_created() -> None:
    analysis = analyze(parse("WITH 1 AS a WITH a + 1 AS b RETURN b"))

    assert [binding.name for binding in analysis.bindings] == ["b"]


# --- the plan --------------------------------------------------------------------------------


@pytest.mark.parametrize("statement", [I06, I07])
def test_each_stage_plans_a_projection_with_its_own_filter_above_it(
    database: object, statement: str
) -> None:
    assert _operators(database.explain(statement)) == (
        "ProduceResults",
        "ProjectRows",
        "EagerRows",
        "SetProperties",
        "FilterRows",
        "WithRows",
        "FilterRows",
        "WithRows",
        "FilterRows",
        "NodeScan",
        "SingleRow",
    )


def test_a_stage_without_a_where_plans_no_filter_of_its_own(database: object) -> None:
    assert _operators(database.explain("WITH 1 AS value RETURN value")) == (
        "ProduceResults",
        "ProjectRows",
        "WithRows",
        "SingleRow",
    )


# --- the two mutations -----------------------------------------------------------------------


def test_the_public_probe_answers_from_a_single_row() -> None:
    with okto_grafx.connect(":memory:") as handle:
        assert handle.execute("WITH 1 AS value RETURN value").rows == ((1,),)


def test_i06_cancels_only_the_rows_both_stages_kept(database: object) -> None:
    with database.begin("write") as transaction:
        result = transaction.execute(I06, CANCEL)

    # d1 and d2 map to the card owner c1; d3 keeps its own owner type, d4 belongs to another
    # card and d5 never reaches the second stage at all.
    assert result.rows == (("d1",), ("d2",))
    assert result.statistics["rows_updated"] == 2
    assert _scores(database) == (
        ("d1", 0.55, 0.8, "cancelled", "cancelled"),
        ("d2", 0.55, 0.8, "cancelled", "cancelled"),
        ("d3", 0.8, None, None, None),
        ("d4", 0.8, None, None, None),
        ("d5", 0.8, None, None, None),
    )


def test_i07_restores_what_i06_saved_and_leaves_the_rest_alone(
    database: object,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute(I06, CANCEL)
    with database.begin("write") as transaction:
        result = transaction.execute(I07, RESTORE)

    assert result.rows == (("d1",), ("d2",))
    assert _scores(database) == (
        ("d1", 0.8, None, None, None),
        ("d2", 0.8, None, None, None),
        ("d3", 0.8, None, None, None),
        ("d4", 0.8, None, None, None),
        ("d5", 0.8, None, None, None),
    )


def test_every_set_value_reads_the_row_as_the_statement_matched_it(
    database: object,
) -> None:
    """The saved score is the score BEFORE the penalty, not the penalised one beside it."""
    with database.begin("write") as transaction:
        transaction.execute(I06, CANCEL)

    saved, penalised = database.execute(
        "MATCH (n:Decision {id: 'd1'}) RETURN n.pre_cancellation_relevance_score, "
        "n.relevance_score"
    ).rows[0]
    assert saved == 0.8
    assert penalised == 0.55


def test_a_reference_the_first_stage_filtered_never_reaches_the_second(
    database: object,
) -> None:
    """The guard preserves a short source reference; an unguarded lookup returns null."""
    with database.begin("write") as transaction:
        transaction.execute(I06, CANCEL)
    assert _scores(database)[4] == ("d5", 0.8, None, None, None)

    with database.begin("write") as transaction:
        transaction.execute(UNGUARDED)
    assert database.execute("MATCH (n:Decision {id: 'd5'}) RETURN n.superseded_by").rows == ((None,),)

    # The same statement over the rows that DO carry an owner is accepted, so what the guard
    # protects against is the one short reference and not the shape of the query.
    with database.begin("write") as transaction:
        transaction.execute(
            UNGUARDED.replace(
                "MATCH (n:Decision) ", "MATCH (n:Decision) WHERE n.id <> 'd5' "
            )
        )
    assert database.execute(
        "MATCH (n:Decision {id: 'd1'}) RETURN n.superseded_by"
    ).rows == (("c1",),)


def test_a_late_refusal_releases_the_rows_the_stages_had_already_written(
    database: object,
) -> None:
    transaction = database.begin("write")
    transaction.execute("MATCH (n:Decision {id: 'd3'}) SET n.relevance_score = 0.4")
    accepted = tuple(transaction._context.row_intents)

    with pytest.raises(GrafxPlanError):
        transaction.execute(UNGUARDED.replace(
            "SET n.superseded_by = owner_id ",
            "SET n.superseded_by = owner_id, "
            "n.relevance_score = 1 / (size(string_split(n.source_artifact_ref, ':')) - 1) ",
        ))

    assert tuple(transaction._context.row_intents) == accepted
    assert transaction.commit().wrote is True
    assert database.execute(
        "MATCH (n:Decision) RETURN n.id, n.superseded_by, n.relevance_score ORDER BY n.id"
    ).rows == (
        ("d1", None, 0.8),
        ("d2", None, 0.8),
        ("d3", None, 0.4),
        ("d4", None, 0.8),
        ("d5", None, 0.8),
    )


BUDGETED = (
    "MATCH (n:Decision) "
    "WITH n, string_split(n.source_artifact_ref, ':') AS parts "
    "WITH n, parts[1] AS owner_id "
    "SET n.superseded_by = owner_id"
)


def _budget_database(path: Path, **options: object) -> object:
    """Return three rows the stages will write, under whatever budget the caller set."""
    handle = okto_grafx.connect(path, **options)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, source_artifact_ref STRING, superseded_by STRING, "
            "PRIMARY KEY(id))"
        )
    for identity in ("d1", "d2", "d3"):
        with handle.begin("write") as seed:
            seed.execute(
                "CREATE (:Decision {id: $id, source_artifact_ref: 'card:c1:1'})",
                {"id": identity},
            )
    return handle


def test_a_budget_refusal_mid_stage_releases_no_partial_write(tmp_path: Path) -> None:
    generous = _budget_database(tmp_path / "generous", max_intermediate_rows=4)
    try:
        with generous.begin("write") as transaction:
            transaction.execute(BUDGETED)
        # The same statement over the same three rows writes all three when the budget allows
        # it, so the refusal below arrives with two of them already built.
        assert generous.execute(
            "MATCH (n:Decision) RETURN n.superseded_by ORDER BY n.id"
        ).rows == (("c1",), ("c1",), ("c1",))
    finally:
        generous.close()

    handle = _budget_database(tmp_path / "budget", max_intermediate_rows=2)
    try:
        transaction = handle.begin("write")
        transaction.execute(
            "MATCH (n:Decision {id: 'd3'}) SET n.superseded_by = 'kept'"
        )
        accepted = tuple(transaction._context.row_intents)
        assert accepted

        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            transaction.execute(BUDGETED)
        assert raised.value.details == {
            "field": "max_intermediate_rows",
            "limit": 2,
            "observed": 3,
            "operator": "NodeScan",
        }

        assert tuple(transaction._context.row_intents) == accepted
        assert transaction.commit().wrote is True
    finally:
        handle.close()

    reopened = okto_grafx.connect(tmp_path / "budget")
    try:
        assert reopened.execute(
            "MATCH (n:Decision) RETURN n.id, n.superseded_by ORDER BY n.id"
        ).rows == (("d1", None), ("d2", None), ("d3", "kept"))
    finally:
        reopened.close()


def test_a_carried_variable_is_still_the_row_it_was_matched_from(
    database: object,
) -> None:
    """WITH n hands on the binding itself, so the stages below still read and write the row."""
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (n:Decision) WHERE n.id = 'd4' "
            "WITH n, n.relevance_score AS before "
            "WHERE before > 0.5 "
            "SET n.superseded_by = 'carried'"
        )

    assert database.execute(
        "MATCH (n:Decision {id: 'd4'}) RETURN n.superseded_by, n.relevance_score"
    ).rows == (("carried", 0.8),)


# --- the doors the parser is not ---------------------------------------------------------------


def _unwound_then_projected() -> Query:
    """Construct a valid composed query independently of the text parser."""
    return Query(
        unwind_clause=UnwindClause(expression=Parameter(name="rows"), alias="r"),
        with_clauses=(WithClause(items=(ReturnItem(expression=Variable(name="r")),)),),
        return_clause=ReturnClause(items=(ReturnItem(expression=Variable(name="r")),)),
    )


def test_a_query_built_positionally_still_means_what_it_meant() -> None:
    """The new field is declared last, so the four that shipped before keep their positions."""
    clause = ReturnClause(items=(ReturnItem(expression=Variable(name="x"), alias="x"),))
    statement = Query(None, (), (), clause)

    assert statement.unwind_clause is None
    assert statement.match_clauses == ()
    assert statement.updating_clauses == ()
    assert statement.return_clause is clause
    assert statement.with_clauses == ()


def test_unwind_beside_with_is_analyzed_from_a_tree_nobody_parsed() -> (
    None
):
    result = analyze(_unwound_then_projected())
    assert result.parameters == ("rows",)
    assert result.output_columns == ("r",)


def test_unwind_beside_with_is_revalidated_despite_supplied_analysis(
    catalog: object, indexes: tuple
) -> None:
    """build_plan accepts a caller's analysis, so the shape has to hold at that door too."""
    statement = _unwound_then_projected()
    supplied = QueryAnalysis(
        statement=statement,
        bindings=(Binding(name="r", entity=ENTITY_UNWOUND, labels=(), created=False),),
        parameters=(),
        output_columns=("r",),
    )

    planned = build_plan(statement, catalog=catalog, indexes=indexes, analysis=supplied)
    assert planned.analysis.parameters == ("rows",)
    assert planned.columns == ("r",)


# --- a name means one thing per query -----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "WITH ['x'] AS a WITH a[1] AS x WITH x WITH x, 1 AS a RETURN x",
        "WITH ['x'] AS a WITH a AS b WITH b AS a RETURN a[1]",
    ],
)
def test_a_dropped_name_can_be_reused_without_a_type_resolution_cycle(
    database: object, query: str
) -> None:
    """Lexical lowering gives each occurrence its own identity; index one is out of range."""
    analyze(parse(query))
    assert database.execute(query).rows == ((None,),)


def test_a_dropped_name_may_return_when_nothing_else_claims_it() -> None:
    """The rule is about REUSE: carrying a name forward through every stage is still fine."""
    analysis = analyze(parse("WITH ['x'] AS a WITH a AS a WITH a RETURN a[1]"))

    assert [binding.name for binding in analysis.bindings] == ["a"]


# --- the branches of the two mutations ----------------------------------------------------------


def test_i06_floors_the_penalised_score_and_still_saves_the_one_it_read(
    database: object,
) -> None:
    """A score below the penalty lands on 0.0, and the saved score is the one before it."""
    with database.begin("write") as transaction:
        transaction.execute("MATCH (n:Decision {id: 'd1'}) SET n.relevance_score = 0.1")
    with database.begin("write") as transaction:
        transaction.execute(I06, CANCEL)

    assert _scores(database)[:2] == (
        ("d1", 0.0, 0.1, "cancelled", "cancelled"),
        ("d2", 0.55, 0.8, "cancelled", "cancelled"),
    )


def test_i07_adds_the_penalty_back_only_when_nothing_was_saved(
    database: object,
) -> None:
    """The two arms are told apart by making the sum differ from the saved value."""
    with database.begin("write") as transaction:
        transaction.execute(I06, CANCEL)
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (n:Decision {id: 'd1'}) "
            "SET n.pre_cancellation_relevance_score = NULL, n.relevance_score = 0.5"
        )

    with database.begin("write") as transaction:
        result = transaction.execute(I07, RESTORE)

    # Both rows come back, in whichever order the scan reaches them: the preparation above
    # rewrote d1, and a RETURN with no ORDER BY promises nothing about where a rewritten row
    # lands. What the two arms produced is asserted below, and that is not order-dependent.
    assert sorted(result.rows) == [("d1",), ("d2",)]
    # d1 had nothing saved, so it gets its penalty back: 0.5 + 0.25. d2 gets the 0.8 it saved,
    # which is a different number on purpose -- otherwise both arms would look alike.
    assert _scores(database)[:2] == (
        ("d1", 0.75, None, None, None),
        ("d2", 0.8, None, None, None),
    )
