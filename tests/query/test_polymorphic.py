"""A node written without a label: one scan over every node table, read as one set.

Pulse counts and pages its whole graph before it knows which kinds of node are in it, so its
templates open on ``MATCH (n)`` and let the WHERE decide. That makes one question the whole
file: is the union really ONE set? The filter, the label test, the count, the order and the
window all have to see it once -- and a property one table declares and another does not has to
read as null rather than as a refusal, because the same scan crosses both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.query.analysis import (
    ENTITY_NODE,
    Binding,
    QueryAnalysis,
    analyze,
    polymorphic_node,
)
from okto_grafx.domain.query.ast import (
    MatchClause,
    NodePattern,
    PatternPath,
    Query,
    ReturnClause,
    ReturnItem,
    SetClause,
    SetItem,
    Literal,
    Property,
    Variable,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan

PAGE = (
    "MATCH (n) WHERE n.relevance_score >= $min_relevance "
    "AND (coalesce(n.revocation_reason, '') <> 'source_deleted') "
    "RETURN n.id, label(n) AS node_type, n.title, "
    "coalesce(n.graph_layer, 'legacy_unknown') AS graph_layer "
    "ORDER BY n.created_at DESC, n.id DESC "
    "LIMIT $max_rows"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return two node tables that agree on some columns and differ on others."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, title STRING, content STRING, created_at STRING, "
            "relevance_score DOUBLE, graph_layer STRING, revocation_reason STRING, "
            "PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE NODE TABLE Bug("
            "id STRING, title STRING, created_at STRING, relevance_score DOUBLE, "
            "graph_layer STRING, PRIMARY KEY(id))"
        )
        schema.execute("CREATE REL TABLE Touches(FROM Decision TO Bug)")
    with handle.begin("write") as seed:
        seed.execute(
            "CREATE (:Decision {id: 'd1', title: 'first', content: 'body', "
            "created_at: '2026-01-01', relevance_score: 0.9, graph_layer: 'canonical'})"
        )
        seed.execute(
            "CREATE (:Decision {id: 'd2', title: 'second', content: 'body', "
            "created_at: '2026-01-03', relevance_score: 0.2, graph_layer: 'working'})"
        )
        seed.execute(
            "CREATE (:Bug {id: 'b1', title: 'bug', created_at: '2026-01-02', "
            "relevance_score: 0.8, graph_layer: 'canonical'})"
        )
    try:
        yield handle
    finally:
        handle.close()


def _operators(root: object) -> tuple[str, ...]:
    """Return the labels in one public plan, parents before children."""
    return tuple(node.label for node in root.walk())


def _ids(
    handle: object, query: str, parameters: dict | None = None
) -> tuple[object, ...]:
    """Return the first column of every row a query produces."""
    return tuple(row[0] for row in handle.execute(query, parameters or {}).rows)


# --- the union is one set ----------------------------------------------------------------------


def test_the_probe_reads_every_node_table_through_one_operator(
    database: object,
) -> None:
    plan = database.explain("MATCH (n) RETURN n")

    assert _operators(plan) == (
        "ProduceResults",
        "ProjectRows",
        "AllNodesScan",
        "SingleRow",
    )
    scan = next(node for node in plan.walk() if node.label == "AllNodesScan")
    # The tables are named in the plan and read in table_id order, so a plan says what it will
    # read and two runs of one plan read it in one order.
    assert scan.details()["tables"] == "Decision, Bug"
    assert _ids(database, "MATCH (n) RETURN n.id ORDER BY n.id") == ("b1", "d1", "d2")


def test_an_empty_catalog_still_plans_the_scan_and_answers_nothing(
    tmp_path: Path,
) -> None:
    handle = okto_grafx.connect(tmp_path / "empty")
    try:
        assert _operators(handle.explain("MATCH (n) RETURN n")) == (
            "ProduceResults",
            "ProjectRows",
            "AllNodesScan",
            "SingleRow",
        )
        assert handle.execute("MATCH (n) RETURN n").rows == ()
        assert handle.execute("MATCH (n) RETURN count(n) AS total").rows == ((0,),)
    finally:
        handle.close()


def test_the_order_and_the_window_apply_to_the_union_and_not_to_each_table(
    database: object,
) -> None:
    """b1 sits BETWEEN the two decisions by date; per-table paging could not produce that."""
    assert _ids(
        database, "MATCH (n) RETURN n.id ORDER BY n.created_at DESC LIMIT $k", {"k": 2}
    ) == ("d2", "b1")
    assert _ids(
        database, "MATCH (n) RETURN n.id ORDER BY n.created_at ASC LIMIT $k", {"k": 2}
    ) == ("d1", "b1")


def test_aggregation_and_distinct_summarise_the_union(database: object) -> None:
    assert database.execute("MATCH (n) RETURN count(n) AS total").rows == ((3,),)
    assert database.execute(
        "MATCH (n) WHERE n.relevance_score >= $m RETURN count(n) AS total", {"m": 0.5}
    ).rows == ((2,),)
    assert _ids(
        database, "MATCH (n) RETURN DISTINCT n.graph_layer AS layer ORDER BY layer"
    ) == ("canonical", "working")


def test_label_picks_one_table_out_of_the_union(database: object) -> None:
    assert _ids(
        database,
        "MATCH (n) WHERE label(n) = $t RETURN n.id ORDER BY n.id",
        {"t": "Bug"},
    ) == ("b1",)
    assert database.execute(
        "MATCH (n) WHERE label(n) = $t RETURN count(n) AS c", {"t": "Decision"}
    ).rows == ((2,),)


def test_the_pulse_paging_template_runs_over_the_whole_graph(database: object) -> None:
    result = database.execute(PAGE, {"min_relevance": 0.5, "max_rows": 10})

    assert result.columns == ("n.id", "node_type", "n.title", "graph_layer")
    # d2 is filtered by relevance; b1 has no `content` column at all and still answers.
    assert result.rows == (
        ("b1", "Bug", "bug", "canonical"),
        ("d1", "Decision", "first", "canonical"),
    )


# --- properties across tables ------------------------------------------------------------------


def test_a_property_only_one_table_declares_reads_as_null_on_the_other(
    database: object,
) -> None:
    assert database.execute("MATCH (n) RETURN n.id, n.content ORDER BY n.id").rows == (
        ("b1", None),
        ("d1", "body"),
        ("d2", "body"),
    )


def test_a_property_no_table_declares_reads_as_null_everywhere(
    database: object,
) -> None:
    assert _ids(database, "MATCH (n) RETURN n.nobody_declares_this") == (
        None,
        None,
        None,
    )


def test_tables_that_disagree_about_a_property_refuse_before_any_row(
    tmp_path: Path,
) -> None:
    """The refusal cannot depend on which table the scan happened to reach first."""
    handle = okto_grafx.connect(tmp_path / "conflict")
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE A(id STRING, tag STRING, PRIMARY KEY(id))"
            )
            schema.execute("CREATE NODE TABLE B(id STRING, tag INT64, PRIMARY KEY(id))")

        # No rows exist at all, so nothing could have refused per row.
        with pytest.raises(GrafxPlanError) as raised:
            handle.explain("MATCH (n) RETURN n.tag")
        assert raised.value.details == {"field": "property", "value": "tag"}
        assert "A.tag is STRING" in str(raised.value)
        assert "B.tag is INT64" in str(raised.value)

        with pytest.raises(GrafxPlanError):
            handle.execute("MATCH (n) RETURN coalesce(n.tag, '') AS t")
        # A property they DO agree on is unaffected.
        assert handle.execute("MATCH (n) RETURN n.id").rows == ()
    finally:
        handle.close()


def test_an_integer_and_a_double_column_promote_rather_than_disagree(
    tmp_path: Path,
) -> None:
    handle = okto_grafx.connect(tmp_path / "promote")
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE A(id STRING, weight DOUBLE, PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE NODE TABLE B(id STRING, weight INT64, PRIMARY KEY(id))"
            )
        with handle.begin("write") as seed:
            seed.execute("CREATE (:A {id: 'a', weight: 1.5})")
            seed.execute("CREATE (:B {id: 'b', weight: 2})")

        assert _ids(
            handle, "MATCH (n) RETURN coalesce(n.weight, 0.0) AS w ORDER BY w"
        ) == (1.5, 2.0)
    finally:
        handle.close()


# --- what the caller receives --------------------------------------------------------------------


def test_a_node_matched_without_a_label_detaches_as_a_map(database: object) -> None:
    """It carries the label and the properties, and nothing owner-private at all."""
    rows = database.execute("MATCH (n) WHERE label(n) = 'Bug' RETURN n").rows

    assert rows == (
        (
            {
                "label": "Bug",
                "properties": {
                    "id": "b1",
                    "title": "bug",
                    "created_at": "2026-01-02",
                    "relevance_score": 0.8,
                    "graph_layer": "canonical",
                },
            },
        ),
    )
    node = rows[0][0]
    assert set(node) == {"label", "properties"}
    for private in ("record_id", "ref", "version", "table_id", "id"):
        assert private not in node


def test_a_node_this_transaction_staged_detaches_the_same_way(database: object) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE (:Bug {id: 'b2', title: 'pending', created_at: '2026-01-04', "
            "relevance_score: 0.5, graph_layer: 'working'})"
        )
        staged = transaction.execute("MATCH (n) WHERE n.id = 'b2' RETURN n").rows

    assert staged == (
        (
            {
                "label": "Bug",
                "properties": {
                    "id": "b2",
                    "title": "pending",
                    "created_at": "2026-01-04",
                    "relevance_score": 0.5,
                    "graph_layer": "working",
                },
            },
        ),
    )
    # A row this statement staged has no identity yet, and none is invented for the caller.
    assert "record_id" not in staged[0][0]


def test_the_map_the_caller_receives_is_its_own(database: object) -> None:
    rows = database.execute("MATCH (n) WHERE label(n) = 'Bug' RETURN n").rows
    rows[0][0]["properties"]["title"] = "mutated"
    rows[0][0]["label"] = "NotATable"

    assert database.execute("MATCH (n) WHERE label(n) = 'Bug' RETURN n.title").rows == (
        ("bug",),
    )


# --- the owner's own view ------------------------------------------------------------------------


def test_the_scan_shows_the_owner_its_own_writes_and_shows_nobody_else(
    database: object,
) -> None:
    transaction = database.begin("write")
    try:
        transaction.execute(
            "CREATE (:Decision {id: 'd3', title: 'staged', created_at: '2026-01-05', "
            "relevance_score: 0.7, graph_layer: 'working'})"
        )
        transaction.execute("MATCH (n:Bug) WHERE n.id = 'b1' DELETE n")
        transaction.execute(
            "MATCH (n:Decision) WHERE n.id = 'd1' SET n.title = 'edited'"
        )

        owner = transaction.execute("MATCH (n) RETURN n.id, n.title ORDER BY n.id")
        # Its own insert is there, its own delete is gone, and its own update is the one it made.
        assert owner.rows == (
            ("d1", "edited"),
            ("d2", "second"),
            ("d3", "staged"),
        )

        outside = database.execute("MATCH (n) RETURN n.id, n.title ORDER BY n.id")
        assert outside.rows == (
            ("b1", "bug"),
            ("d1", "first"),
            ("d2", "second"),
        )
    finally:
        transaction.rollback()

    assert _ids(database, "MATCH (n) RETURN n.id ORDER BY n.id") == ("b1", "d1", "d2")


def test_the_budget_counts_the_union_and_not_each_table(tmp_path: Path) -> None:
    exact = okto_grafx.connect(tmp_path / "exact", max_intermediate_rows=3)
    refused = okto_grafx.connect(tmp_path / "refused", max_intermediate_rows=2)
    try:
        for handle in (exact, refused):
            with handle.begin("write") as schema:
                schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
                schema.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
            with handle.begin("write") as seed:
                seed.execute("CREATE (:A {id: 'a1'})")
                seed.execute("CREATE (:A {id: 'a2'})")
                seed.execute("CREATE (:B {id: 'b1'})")

        # Three rows over two tables is three rows, not two runs of at most two.
        assert _ids(exact, "MATCH (n) RETURN n.id ORDER BY n.id") == ("a1", "a2", "b1")
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            refused.execute("MATCH (n) RETURN n.id")
        assert raised.value.details == {
            "field": "max_intermediate_rows",
            "limit": 2,
            "observed": 3,
            "operator": "AllNodesScan",
        }
    finally:
        exact.close()
        refused.close()


# --- the one shape it is read in -------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "MATCH () RETURN 1",
        "MATCH (n), (m:Bug) RETURN n.id",
        "MATCH (n) MATCH (m:Bug) RETURN n.id",
        "MATCH (n) CREATE (:Bug {id: 'x'})",
        "MATCH (n) SET n.title = 'x'",
        "MATCH (n) DELETE n",
        "MATCH (n {id: 'd1'}) RETURN n.id",
        "UNWIND $rows AS r MATCH (n) RETURN n.id",
    ],
)
def test_every_shape_but_the_one_is_refused_before_a_table_is_read(
    database: object, query: str
) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.execute(query, {"rows": [1]})
    assert raised.value.details["field"] == "pattern"
    assert "exactly one shape" in str(raised.value)


def test_a_node_at_the_end_of_a_hop_keeps_the_refusal_it_already_had(
    database: object,
) -> None:
    """A path names the table at each end, so a label-free end is not this feature at all.

    The distinction matters beyond tidiness: were the shape rule to claim these too, every
    frozen path form in the corpus would change its recorded refusal without anything about it
    having changed.
    """

    with pytest.raises(GrafxPlanError) as raised:
        database.execute("MATCH (n)-[:Touches]->(b:Bug) RETURN n.id")

    assert raised.value.details["field"] == "labels"
    assert "exactly one label" in str(raised.value)
    assert polymorphic_node(parse("MATCH (n)-[:Touches]->(b:Bug) RETURN n.id")) is None


def test_the_shapes_that_are_admitted_stay_admitted(database: object) -> None:
    assert _ids(database, "MATCH (n) RETURN n.id ORDER BY n.id") == ("b1", "d1", "d2")
    # WITH shapes the rows the scan produced, which is reading and not writing.
    assert _ids(
        database,
        "MATCH (n) WITH n, n.id AS ref WHERE ref <> $skip RETURN ref ORDER BY ref",
        {"skip": "d2"},
    ) == ("b1", "d1")


def test_a_name_an_earlier_pattern_bound_is_not_a_second_polymorphic_scan(
    database: object,
) -> None:
    """The label-free mention reuses the table the first pattern gave it, so it is not one."""
    assert polymorphic_node(parse("MATCH (m:Bug) MATCH (m) RETURN m.id")) is None
    assert _ids(database, "MATCH (m:Bug) MATCH (m) RETURN m.id") == ("b1",)


def _polymorphic_write() -> Query:
    """Return MATCH (n) SET n.title = 'x' as a tree, which no parser here would hand on."""
    pattern = PatternPath(nodes=(NodePattern(variable="n"),), relationships=())
    return Query(
        match_clauses=(MatchClause(patterns=(pattern,)),),
        updating_clauses=(
            SetClause(
                items=(
                    SetItem(
                        target=Property(subject=Variable(name="n"), key="title"),
                        value=Literal(value="x"),
                    ),
                )
            ),
        ),
        return_clause=ReturnClause(
            items=(
                ReturnItem(expression=Property(subject=Variable(name="n"), key="id")),
            )
        ),
    )


def test_a_tree_nobody_parsed_is_refused_by_the_analysis() -> None:
    with pytest.raises(GrafxPlanError) as raised:
        analyze(_polymorphic_write())
    assert raised.value.details["field"] == "pattern"


def test_a_caller_supplying_its_own_analysis_is_refused_by_the_planner(
    catalog: object, indexes: tuple
) -> None:
    """build_plan takes an analysis, so the shape has to hold at that door too."""
    statement = _polymorphic_write()
    supplied = QueryAnalysis(
        statement=statement,
        bindings=(Binding(name="n", entity=ENTITY_NODE, labels=(), created=False),),
        output_columns=("n.id",),
    )

    with pytest.raises(GrafxPlanError) as raised:
        build_plan(statement, catalog=catalog, indexes=indexes, analysis=supplied)
    assert raised.value.details["field"] == "pattern"
