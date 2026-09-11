"""Optional null extension, composed syntax, budgets and untrusted AST validation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.query.analysis import QueryAnalysis, analyze
from okto_grafx.domain.query.ast import MatchClause, Query
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan
from tests.query.stack import build_query_stack, vector


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return an empty labelled table; tests opt in to the rows they need."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id STRING, name STRING, PRIMARY KEY(id))"
        )
    try:
        yield handle
    finally:
        handle.close()


def _query(text: str) -> Query:
    """Parse a query and narrow the statement union for the AST tests."""
    statement = parse(text)
    assert isinstance(statement, Query)
    return statement


def _force_optional(text: str) -> Query:
    """Return a tree the ordinary parser wrote, with its first MATCH forged optional."""
    statement = _query(text)
    assert statement.match_clauses
    first = replace(statement.match_clauses[0], optional=True)
    pipeline = tuple(first if clause is statement.match_clauses[0] else clause
                     for clause in statement.clause_pipeline)
    return replace(statement, match_clauses=(first, *statement.match_clauses[1:]),
                   clause_pipeline=pipeline)


def _forged_analysis(statement: Query) -> QueryAnalysis:
    """Return plausible analysis metadata without asking it to approve ``statement``."""
    valid = _query("OPTIONAL MATCH (p:Person) RETURN p.id")
    return replace(analyze(valid), statement=statement)


# --- the one admitted form --------------------------------------------------------------------


def test_optional_flag_round_trips_and_keeps_match_clause_positionals() -> None:
    statement = _query("OPTIONAL MATCH (p:Person) WHERE p.id = 'p1' RETURN p.id")
    clause = statement.match_clauses[0]

    assert clause.optional is True
    assert statement.describe() == (
        "OPTIONAL MATCH (p:Person) WHERE (p.id = 'p1') RETURN p.id"
    )
    # The field was appended with a default: old positional construction still means MATCH.
    assert MatchClause(clause.patterns, clause.predicate).optional is False


def test_plan_places_null_extension_above_the_complete_match(database: object) -> None:
    plan = database.explain("OPTIONAL MATCH (p:Person) RETURN p.id")
    operators = tuple(node.label for node in plan.walk())

    assert operators == (
        "ProduceResults",
        "ProjectRows",
        "ApplyRows",
        "SingleRow",
        "NodeScan",
        "ArgumentRows",
    )
    optional = next(node for node in plan.walk() if node.label == "ApplyRows")
    assert optional.null_variables == ("p",)


def test_empty_match_extends_one_null_row_for_properties_label_and_counts(
    database: object,
) -> None:
    projected = database.execute(
        "OPTIONAL MATCH (p:Person) RETURN p.id, p.name, label(p)"
    )
    counted = database.execute("OPTIONAL MATCH (p:Person) RETURN count(p), count(*)")

    assert projected.rows == ((None, None, None),)
    assert counted.rows == ((0, 1),)


def test_real_matches_never_gain_a_synthetic_row(database: object) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 'p1', name: 'Ada'})")
        seed.execute("CREATE (:Person {id: 'p2', name: 'Grace'})")

    rows = database.execute("OPTIONAL MATCH (p:Person) RETURN p.id ORDER BY p.id").rows
    counted = database.execute(
        "OPTIONAL MATCH (p:Person) RETURN count(p), count(*)"
    ).rows

    assert rows == (("p1",), ("p2",))
    assert counted == ((2, 2),)


@pytest.mark.parametrize(
    "predicate",
    ["false", "p.id = 'missing'"],
)
def test_where_that_removes_every_candidate_is_inside_the_optional_extension(
    database: object, predicate: str
) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 'p1', name: 'Ada'})")

    assert database.execute(
        f"OPTIONAL MATCH (p:Person) WHERE {predicate} RETURN p.id"
    ).rows == ((None,),)


def test_deferred_similarity_filter_is_inside_the_optional_extension() -> None:
    stack = build_query_stack()
    reference = stack.insert("Chunk", 10, (10, 1, vector((0.0, 1.0, 0.0, 0.0))), csn=1)
    stack.add_vector(10, reference, (0.0, 1.0, 0.0, 0.0), csn=1)

    result = stack.engine.execute(
        "OPTIONAL MATCH (n:Chunk) "
        "WHERE similarity(n.embedding, $q, space => 'minilm_v2') > 0.5 "
        "RETURN n.id",
        stack.transaction(read_lsn=1000),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )

    assert result.rows == ((None,),)
    labels = tuple(node.label for node in result.plan.walk())
    assert labels.index("ApplyRows") < labels.index("VectorSearch")


# --- isolation and budgets --------------------------------------------------------------------


def test_pending_node_is_null_to_an_old_snapshot_and_real_to_its_owner(
    database: object,
) -> None:
    writer = database.begin("write")
    writer.execute("CREATE (:Person {id: 'pending', name: 'owner'})")
    outsider = database.begin("read")
    try:
        assert writer.execute(
            "OPTIONAL MATCH (p:Person) WHERE p.id = 'pending' RETURN p.name"
        ).rows == (("owner",),)
        assert outsider.execute(
            "OPTIONAL MATCH (p:Person) WHERE p.id = 'pending' RETURN p.name"
        ).rows == ((None,),)

        writer.commit()
        # Its snapshot does not move merely because the writer committed.
        assert outsider.execute(
            "OPTIONAL MATCH (p:Person) WHERE p.id = 'pending' RETURN p.name"
        ).rows == ((None,),)
    finally:
        outsider.commit()

    assert database.execute(
        "OPTIONAL MATCH (p:Person) WHERE p.id = 'pending' RETURN p.name"
    ).rows == (("owner",),)


def test_rollback_removes_pending_match_and_restores_null_extension(
    database: object,
) -> None:
    writer = database.begin("write")
    try:
        writer.execute("CREATE (:Person {id: 'rolled-back', name: 'temporary'})")
        assert writer.execute(
            "OPTIONAL MATCH (p:Person) WHERE p.id = 'rolled-back' RETURN p.name"
        ).rows == (("temporary",),)
    finally:
        writer.rollback()

    assert database.execute(
        "OPTIONAL MATCH (p:Person) WHERE p.id = 'rolled-back' RETURN p.name"
    ).rows == ((None,),)


@pytest.mark.parametrize("field", ["max_result_rows", "max_intermediate_rows"])
def test_existing_query_budgets_apply_to_optional_rows(
    tmp_path: Path, field: str
) -> None:
    with okto_grafx.connect(tmp_path / field, **{field: 1}) as handle:
        with handle.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id STRING, PRIMARY KEY(id))")

        # The single synthetic row is neither hidden from nor double-counted by a budget.
        assert handle.execute("OPTIONAL MATCH (p:Person) RETURN p.id").rows == (
            (None,),
        )

        for identity in ("p1", "p2"):
            with handle.begin("write") as seed:
                seed.execute(f"CREATE (:Person {{id: '{identity}'}})")

        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            handle.execute("OPTIONAL MATCH (p:Person) RETURN p.id")

    assert raised.value.details["field"] == field
    assert raised.value.details["limit"] == 1
    assert raised.value.details["observed"] == 2


# --- everything outside the frozen shape ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "OPTIONAL MATCH (:Person) RETURN 1",
        "OPTIONAL MATCH (p) RETURN p",
        "OPTIONAL MATCH (p:Person:Other) RETURN p",
        "OPTIONAL MATCH (p:Person {id: 'p1'}) RETURN p",
        "OPTIONAL MATCH (p:Person)-[:Knows]->(q:Person) RETURN p",
        "OPTIONAL MATCH (p:Person), (q:Person) RETURN p",
        "OPTIONAL MATCH path = (p:Person) RETURN p",
        "OPTIONAL MATCH (p:Person) MATCH (q:Person) RETURN p",
        "OPTIONAL MATCH (p:Person) WITH p RETURN p",
        "OPTIONAL MATCH (p:Person) SET p.name = 'changed' RETURN p",
        "OPTIONAL MATCH (p:Person) RETURN p UNION MATCH (q:Person) RETURN q",
        "OPTIONAL MATCH (p:Person) CALL db.index() YIELD value RETURN value",
        "MATCH (p:Person) OPTIONAL MATCH (q:Person) RETURN p",
        "WITH 1 AS x OPTIONAL MATCH (p:Person) RETURN p",
        "UNWIND [] AS x OPTIONAL MATCH (p:Person) RETURN p",
        "ＭＡＴＣＨ (p:Person) RETURN p",
    ],
)
def test_composable_optional_syntax_is_distinct_from_semantic_admission(text: str) -> None:
    if text.startswith("ＭＡＴＣＨ"):
        with pytest.raises(GrafxParseError):
            parse(text)
    else:
        statement = parse(text)
        assert parse(statement.describe()) == statement


FORGED_TEXTS: tuple[str, ...] = (
    "MATCH (:Person) RETURN 1",
    "MATCH (p) RETURN p",
    "MATCH (p:Person:Chunk) RETURN p",
    "MATCH (p:Person {id: 'p1'}) RETURN p",
    "MATCH (p:Chunk)-[r:BELONGS_TO]->(d:Doc) RETURN p.id",
    "MATCH (p:Person), (q:Person) RETURN p.id",
    "MATCH path = (p:Person) RETURN p.id",
    "MATCH (p:Person) MATCH (q:Person) RETURN p.id",
    "MATCH (p:Person) WITH p RETURN p.id",
    "MATCH (p:Person) SET p.name = 'changed' RETURN p.id",
    "UNWIND [1] AS x MATCH (p:Person) RETURN p.id",
)


@pytest.mark.parametrize("text", FORGED_TEXTS)
def test_analysis_and_planner_revalidate_composed_optional_trees(
    catalog: object, indexes: tuple, text: str
) -> None:
    statement = _force_optional(text)

    if "MATCH path" in text:
        with pytest.raises(GrafxPlanError):
            analyze(statement)
        with pytest.raises(GrafxPlanError):
            build_plan(statement, catalog=catalog, indexes=indexes,
                       analysis=_forged_analysis(statement))
        return
    actual = analyze(statement)
    if ":Person:Chunk" in text:
        with pytest.raises(GrafxPlanError):
            build_plan(statement, catalog=catalog, indexes=indexes,
                       analysis=_forged_analysis(statement))
    else:
        planned = build_plan(statement, catalog=catalog, indexes=indexes,
                             analysis=_forged_analysis(statement))
        assert planned.writes == actual.statement.writes
        assert planned.columns == actual.output_columns


@pytest.mark.parametrize("door", ["analysis", "planner"])
def test_optional_without_return_is_refused_at_both_ast_doors(
    catalog: object, indexes: tuple, door: str
) -> None:
    statement = replace(
        _query("OPTIONAL MATCH (p:Person) RETURN p.id"), return_clause=None
    )

    with pytest.raises(GrafxPlanError):
        if door == "analysis":
            analyze(statement)
        else:
            build_plan(
                statement,
                catalog=catalog,  # type: ignore[arg-type]
                indexes=indexes,
                analysis=_forged_analysis(statement),
            )


@pytest.mark.parametrize("door", ["analysis", "planner"])
def test_optional_flag_must_be_an_exact_boolean(
    catalog: object, indexes: tuple, door: str
) -> None:
    valid = _query("OPTIONAL MATCH (p:Person) RETURN p.id")
    forged = replace(valid.match_clauses[0], optional=1)  # type: ignore[arg-type]
    statement = replace(valid, match_clauses=(forged,))

    with pytest.raises(GrafxPlanError) as raised:
        if door == "analysis":
            analyze(statement)
        else:
            build_plan(
                statement,
                catalog=catalog,  # type: ignore[arg-type]
                indexes=indexes,
                analysis=_forged_analysis(statement),
            )

    assert raised.value.details["value"] == "optional"
