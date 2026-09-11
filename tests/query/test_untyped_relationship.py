"""Generic one-hop traversal, historic access-path guarantees and forged AST refusal."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.analysis import (
    Aggregation,
    QueryAnalysis,
    analyze,
)
from okto_grafx.domain.query.ast import FunctionCall, Property, Query, Variable
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.plan import TraverseAnyRelationship
from okto_grafx.domain.query.planner import build_plan

ADMITTED = "MATCH (a:Decision)-[r]->(b) RETURN a.id"


def _catalog(*, broken: bool = False, barren: bool = False) -> Catalog:
    """Return a catalog with two relationship tables leaving Decision, in id order."""
    catalog = Catalog()
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Decision",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.STRING, nullable=False),),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="Bug",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.STRING, nullable=False),),
            primary_key="id",
        )
    )
    if not barren:
        catalog.add_table(
            TableDef(
                table_id=3,
                name="supports",
                kind="rel",
                columns=(ColumnDef(name="w", type=ValueType.DOUBLE),),
                from_table="Decision",
                to_table="Decision",
            )
        )
        catalog.add_table(
            TableDef(
                table_id=4,
                name="mentions",
                kind="rel",
                columns=(ColumnDef(name="w", type=ValueType.DOUBLE),),
                from_table="Decision",
                to_table="Bug",
            )
        )
    if broken:
        catalog.add_table(
            TableDef(
                table_id=5,
                name="dangling",
                kind="rel",
                columns=(ColumnDef(name="w", type=ValueType.DOUBLE),),
                from_table="Decision",
                to_table="Ghost",
            )
        )
    return catalog


def _seed(handle: object) -> None:
    """Create two tables leaving Decision, two parallel edges and one incoming edge."""
    with handle.begin("write") as schema:
        schema.execute("CREATE NODE TABLE Decision(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE Bug(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE supports(FROM Decision TO Decision, w DOUBLE)")
        schema.execute("CREATE REL TABLE mentions(FROM Decision TO Bug, w DOUBLE)")
        schema.execute("CREATE REL TABLE reported(FROM Bug TO Decision, w DOUBLE)")
    with handle.begin("write") as rows:
        rows.execute("CREATE (:Decision {id: 'd1'})")
        rows.execute("CREATE (:Decision {id: 'd2'})")
        rows.execute("CREATE (:Bug {id: 'b1'})")
        rows.execute(
            "MATCH (x:Decision {id: 'd1'}), (y:Decision {id: 'd2'}) "
            "CREATE (x)-[:supports {w: 1.0}]->(y)"
        )
        rows.execute(
            "MATCH (x:Decision {id: 'd1'}), (y:Decision {id: 'd2'}) "
            "CREATE (x)-[:supports {w: 2.0}]->(y)"
        )
        rows.execute(
            "MATCH (x:Decision {id: 'd1'}), (y:Bug {id: 'b1'}) "
            "CREATE (x)-[:mentions {w: 3.0}]->(y)"
        )
        rows.execute(
            "MATCH (x:Bug {id: 'b1'}), (y:Decision {id: 'd1'}) "
            "CREATE (x)-[:reported {w: 4.0}]->(y)"
        )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return a seeded database whose Decision label has two relationship tables."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    _seed(handle)
    try:
        yield handle
    finally:
        handle.close()


# --- what the hop answers ------------------------------------------------------------------------


def test_the_hop_walks_every_table_that_leaves_the_label(database: object) -> None:
    """Two parallel edges in one table and one edge in another are three separate rows.

    The pair a caller asked for is the EDGE, not the neighbour, so a second edge between the
    same two nodes is a second row -- and a table nobody named is still walked.
    """
    found = database.execute(ADMITTED)
    assert found.columns == ("a.id",)
    assert found.rows == (("d1",), ("d1",), ("d1",))


def test_a_node_with_nothing_leaving_it_contributes_nothing(database: object) -> None:
    """d2 is reachable but leads nowhere, so it never appears as a source."""
    assert all(row[0] == "d1" for row in database.execute(ADMITTED).rows)


def test_an_incoming_edge_is_not_followed(database: object) -> None:
    """The hop is written outgoing, and an untyped hop does not become undirected."""
    # b1 -[:reported]-> d1 exists; if direction were ignored, d1 would gain a fourth row and
    # b1 would appear as a source, which it cannot because it is not a Decision.
    assert len(database.execute(ADMITTED).rows) == 3


def test_the_tables_are_walked_in_catalog_order() -> None:
    """table_id, not name: the order the catalog assigned, so two runs agree."""
    plan = build_plan(parse(ADMITTED), catalog=_catalog())
    traversal = next(
        node for node in plan.root.walk() if isinstance(node, TraverseAnyRelationship)
    )
    assert [table.name for table in traversal.tables] == ["supports", "mentions"]
    assert [table.table_id for table in traversal.tables] == sorted(
        table.table_id for table in traversal.tables
    )


def test_the_plan_is_one_operator_over_one_scan() -> None:
    """One operator, so the rows meet the intermediate budget at one admission point."""
    plan = build_plan(parse(ADMITTED), catalog=_catalog())
    assert [line.strip().split("(")[0] for line in plan.root.render()] == [
        "ProduceResults",
        "ProjectRows",
        "TraverseAnyRelationship",
        "NodeScan",
        "SingleRow",
    ]


def test_a_label_with_no_relationship_tables_answers_no_rows() -> None:
    """An admitted shape ANSWERS. A schema with nothing to walk is an empty answer, not a fault."""
    plan = build_plan(parse(ADMITTED), catalog=_catalog(barren=True))
    traversal = next(
        node for node in plan.root.walk() if isinstance(node, TraverseAnyRelationship)
    )
    assert traversal.tables == ()


def test_an_endpoint_the_catalog_does_not_hold_is_refused_like_a_typed_hop() -> None:
    """Filtering the broken table would answer with the sound ones and call that the result."""
    with pytest.raises(GrafxPlanError) as untyped:
        build_plan(parse(ADMITTED), catalog=_catalog(broken=True))
    with pytest.raises(GrafxPlanError) as typed:
        build_plan(
            parse("MATCH (a:Decision)-[r:dangling]->(b) RETURN a.id"),
            catalog=_catalog(broken=True),
        )
    assert "Ghost" in str(untyped.value)
    assert str(untyped.value) == str(typed.value)


# --- budgets ---------------------------------------------------------------------------------------


def test_the_intermediate_budget_is_charged_once_at_the_traversal(
    tmp_path: Path,
) -> None:
    """One source and one edge in each of two tables is two rows at one operator.

    The budget belongs to the READING handle on purpose: seeding with it in force trips on the
    seed's own NodeScan, and the test would then pass for a reason that has nothing to do with
    the traversal.
    """
    where = tmp_path / "db"
    seeded = okto_grafx.connect(where, page_size=512)
    with seeded.begin("write") as schema:
        schema.execute("CREATE NODE TABLE Decision(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE Bug(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE supports(FROM Decision TO Decision, w DOUBLE)")
        schema.execute("CREATE REL TABLE mentions(FROM Decision TO Bug, w DOUBLE)")
    with seeded.begin("write") as rows:
        rows.execute("CREATE (:Decision {id: 'd1'})")
        rows.execute("CREATE (:Decision {id: 'd2'})")
        rows.execute("CREATE (:Bug {id: 'b1'})")
        rows.execute(
            "MATCH (x:Decision {id: 'd1'}), (y:Decision {id: 'd2'}) "
            "CREATE (x)-[:supports {w: 1.0}]->(y)"
        )
        rows.execute(
            "MATCH (x:Decision {id: 'd1'}), (y:Bug {id: 'b1'}) "
            "CREATE (x)-[:mentions {w: 2.0}]->(y)"
        )
    seeded.close()

    narrow = okto_grafx.connect(where, page_size=512, max_intermediate_rows=1)
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as exceeded:
            narrow.execute(ADMITTED)
        assert "TraverseAnyRelationship" in str(exceeded.value)
        assert "observed 2" in str(exceeded.value)
    finally:
        narrow.close()

    wide = okto_grafx.connect(where, page_size=512, max_intermediate_rows=2)
    try:
        assert wide.execute(ADMITTED).rows == (("d1",), ("d1",))
    finally:
        wide.close()


def test_untyped_traversal_uses_the_same_cumulative_work_budgets(tmp_path: Path) -> None:
    """Candidates from distinct relationship tables share one query-wide counter."""
    where = tmp_path / "untyped-budget"
    seeded = okto_grafx.connect(where, page_size=512)
    _seed(seeded)
    seeded.close()

    narrow = okto_grafx.connect(
        where,
        page_size=512,
        max_traversal_expansions=2,
        max_traversal_paths=3,
    )
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            narrow.execute(ADMITTED)
    finally:
        narrow.close()
    assert raised.value.details == {
        "field": "max_traversal_expansions",
        "limit": 2,
        "observed": 3,
    }

    exact = okto_grafx.connect(
        where,
        page_size=512,
        max_traversal_expansions=3,
        max_traversal_paths=3,
    )
    try:
        found = exact.execute(ADMITTED)
    finally:
        exact.close()
    assert found.rows == (("d1",), ("d1",), ("d1",))
    assert found.statistics["traversal_expansions"] == 3
    assert found.statistics["traversal_paths"] == 3


# --- the statements that only resemble the admitted one -------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        pytest.param("MATCH (x:Decision)-[r]->(b) RETURN x.id", id="other-source-name"),
        pytest.param("MATCH (a:Decision)-[q]->(b) RETURN a.id", id="other-hop-name"),
        pytest.param("MATCH (a:Decision)-[r]->(c) RETURN a.id", id="other-target-name"),
        pytest.param("MATCH (a:Bug)-[r]->(b) RETURN a.id", id="other-label"),
        pytest.param("MATCH (a:Decision:Bug)-[r]->(b) RETURN a.id", id="two-labels"),
        pytest.param("MATCH (a)-[r]->(b) RETURN a.id", id="no-label"),
        pytest.param("MATCH (a:Decision)-[r]->(b:Bug) RETURN a.id", id="target-label"),
        pytest.param("MATCH (a:Decision)-[]->(b) RETURN a.id", id="anonymous-hop"),
        pytest.param("MATCH (a:Decision)<-[r]-(b) RETURN a.id", id="incoming"),
        pytest.param("MATCH (a:Decision)-[r]-(b) RETURN a.id", id="undirected"),
        pytest.param(
            "MATCH (a:Decision)-[r*1..2]->(b) RETURN a.id", id="written-range"
        ),
        pytest.param(
            "MATCH (a:Decision {id: 'd1'})-[r]->(b) RETURN a.id", id="inline-map"
        ),
        pytest.param(
            "MATCH (a:Decision)-[r]->(b) WHERE a.id = 'd1' RETURN a.id", id="where"
        ),
        pytest.param("MATCH (a:Decision)-[r]->(b) RETURN a.title", id="other-property"),
        pytest.param("MATCH (a:Decision)-[r]->(b) RETURN a.id AS ident", id="aliased"),
        pytest.param("MATCH (a:Decision)-[r]->(b) RETURN a.id, b.id", id="extra-item"),
        pytest.param("MATCH (a:Decision)-[r]->(b) RETURN DISTINCT a.id", id="distinct"),
        pytest.param("MATCH (a:Decision)-[r]->(b) RETURN a.id LIMIT 1", id="limit"),
        pytest.param(
            "MATCH (a:Decision)-[r]->(b) RETURN a.id ORDER BY a.id", id="order-by"
        ),
        pytest.param(
            "MATCH (a:Decision)-[r]->(b), (c:Bug) RETURN a.id", id="second-pattern"
        ),
        pytest.param(
            "MATCH (a:Decision)-[r]->(b) MATCH (c:Bug) RETURN a.id", id="second-match"
        ),
        pytest.param("MATCH p = (a:Decision)-[r]->(b) RETURN a.id", id="named-path"),
    ),
)
def test_general_one_hop_forms_are_not_limited_to_literal_names(
    text: str,
) -> None:
    """FP-3 admits generic bounded forms while retaining typed-model refusals."""
    if ":Decision:Bug" in text:
        with pytest.raises(GrafxPlanError):
            build_plan(parse(text), catalog=_catalog())
    else:
        assert build_plan(parse(text), catalog=_catalog()).columns


def test_a_different_source_label_uses_its_own_incident_tables() -> None:
    plan = build_plan(parse("MATCH (a:Bug)-[r]->(b) RETURN a.id"), catalog=_catalog())
    assert any(isinstance(node, TraverseAnyRelationship) for node in plan.root.walk())


def test_a_typed_hop_with_a_small_frontier_keeps_its_traversal() -> None:
    """The untyped route does not widen the typed hop's small-frontier cost rule."""
    plan = build_plan(
        parse("MATCH (a:Decision)-[r:supports]->(b) RETURN a.id LIMIT 64"),
        catalog=_catalog(),
    )
    assert any("TraverseRelationship(" in line for line in plan.root.render())
    assert not any("TraverseAnyRelationship" in line for line in plan.root.render())


# --- trees nobody parsed ---------------------------------------------------------------------------


class _Sneaky(str):
    """A string that claims to equal anything, which is what an equality check cannot see."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash(str(self))


class _SubQuery(Query):
    """A subclass of the statement, which dispatch and equality would both accept."""


def _forged() -> dict[str, Query]:
    """Return trees that carry the right shape in the wrong types."""
    statement = parse(ADMITTED)
    clause = statement.match_clauses[0]
    pattern = clause.patterns[0]
    source, target = pattern.nodes
    hop = pattern.relationships[0]
    returned = statement.return_clause
    assert returned is not None
    item = returned.items[0]
    replace = dataclasses.replace
    return {
        "match_clauses is a list": replace(statement, match_clauses=[clause]),
        "with_clauses is a list": replace(statement, with_clauses=[]),
        "updating_clauses is a list": replace(statement, updating_clauses=[]),
        "patterns is a list": replace(
            statement, match_clauses=(replace(clause, patterns=[pattern]),)
        ),
        "nodes is a list": replace(
            statement,
            match_clauses=(
                replace(clause, patterns=(replace(pattern, nodes=[source, target]),)),
            ),
        ),
        "relationships is a list": replace(
            statement,
            match_clauses=(
                replace(clause, patterns=(replace(pattern, relationships=[hop]),)),
            ),
        ),
        "labels is a list": replace(
            statement,
            match_clauses=(
                replace(
                    clause,
                    patterns=(
                        replace(
                            pattern,
                            nodes=(replace(source, labels=["Decision"]), target),
                        ),
                    ),
                ),
            ),
        ),
        "hop types is a list": replace(
            statement,
            match_clauses=(
                replace(
                    clause,
                    patterns=(
                        replace(pattern, relationships=(replace(hop, types=[]),)),
                    ),
                ),
            ),
        ),
        "items is a list": replace(
            statement, return_clause=replace(returned, items=[item])
        ),
        "sort_items is a list": replace(
            statement, return_clause=replace(returned, sort_items=[])
        ),
        "distinct is an int": replace(
            statement, return_clause=replace(returned, distinct=0)
        ),
        "optional is an int": replace(
            statement, match_clauses=(replace(clause, optional=0),)
        ),
        "the statement is a subclass": _SubQuery(
            unwind_clause=statement.unwind_clause,
            match_clauses=statement.match_clauses,
            updating_clauses=statement.updating_clauses,
            return_clause=statement.return_clause,
            with_clauses=statement.with_clauses,
        ),
        "the label is a str subclass": replace(
            statement,
            match_clauses=(
                replace(
                    clause,
                    patterns=(
                        replace(
                            pattern,
                            nodes=(
                                replace(source, labels=(_Sneaky("Whatever"),)),
                                target,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        "the property key is a str subclass": replace(
            statement,
            return_clause=replace(
                returned,
                items=(
                    replace(
                        item,
                        expression=Property(
                            subject=item.expression.subject, key=_Sneaky("nope")
                        ),
                    ),
                ),
            ),
        ),
        "the variable name is a str subclass": replace(
            statement,
            return_clause=replace(
                returned,
                items=(
                    replace(
                        item,
                        expression=Property(
                            subject=Variable(name=_Sneaky("zzz")), key="id"
                        ),
                    ),
                ),
            ),
        ),
    }


FORGED = _forged()


@pytest.mark.parametrize("name", sorted(FORGED))
def test_a_tree_that_carries_the_shape_in_the_wrong_types_is_rejected(
    name: str,
) -> None:
    """A list is not a tuple and ``0`` is not ``False``, whatever ``len`` and truthiness say."""
    with pytest.raises(GrafxPlanError):
        analyze(FORGED[name])


@pytest.mark.parametrize("name", sorted(FORGED))
def test_a_forged_tree_reaches_no_fan_out_at_the_planner(name: str) -> None:
    """The planner asks the recogniser itself rather than trusting anything handed to it."""
    statement = FORGED[name]
    try:
        plan = build_plan(
            statement, catalog=_catalog(), analysis=QueryAnalysis(statement=statement)
        )
    except GrafxPlanError:
        return
    assert not any(
        isinstance(node, TraverseAnyRelationship) for node in plan.root.walk()
    )


def test_a_supplied_analysis_cannot_add_an_aggregation_the_statement_never_asked_for() -> (
    None
):
    """``_result`` reads the analysis, so the analysis this plan is built from is recomputed."""
    statement = parse(ADMITTED)
    forged = QueryAnalysis(
        statement=statement,
        aggregations=(
            Aggregation(
                position=0, call=FunctionCall(name="count", arguments=(), star=True)
            ),
        ),
        output_columns=("wrong",),
        parameters=("ghost",),
    )
    plan = build_plan(statement, catalog=_catalog(), analysis=forged)
    operators = [line.strip().split("(")[0] for line in plan.root.render()]
    assert "AggregateRows" not in operators
    assert plan.columns == ("a.id",)
    assert plan.analysis.output_columns == ("a.id",)
    assert plan.analysis.parameters == ()
    assert plan.analysis.aggregations == ()


# --- composition ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        pytest.param(
            "MATCH (a:Decision)-[r]->(b) RETURN a.id UNION MATCH (n:Bug) RETURN n.id",
            id="untyped-on-the-left",
        ),
        pytest.param(
            "MATCH (n:Bug) RETURN n.id UNION MATCH (a:Decision)-[r]->(b) RETURN a.id",
            id="untyped-on-the-right",
        ),
    ),
)
def test_untyped_union_still_requires_matching_column_names(text: str) -> None:
    """Generic hops are legal branches, but a.id and n.id are different output names."""
    statement = parse(text)
    with pytest.raises(GrafxPlanError) as raised:
        analyze(statement)
    assert raised.value.details["reason"] == "different_columns_in_union"
    with pytest.raises(GrafxPlanError):
        build_plan(
            statement, catalog=_catalog(), analysis=QueryAnalysis(statement=statement)
        )


def test_an_ordinary_union_is_untouched() -> None:
    """The pair still works when neither branch reaches for an untyped hop."""
    statement = parse("MATCH (a:Decision) RETURN a.id AS id UNION MATCH (b:Bug) RETURN b.id AS id")
    assert analyze(statement) is not None
    assert build_plan(statement, catalog=_catalog()) is not None


# --- one execution -------------------------------------------------------------------------------------


def test_the_hop_reads_inside_the_transaction_that_asked(database: object) -> None:
    """One transaction and one snapshot, so a writer sees its own edge through the fan-out."""
    with database.begin("write") as txn:
        txn.execute(
            "MATCH (x:Decision {id: 'd2'}), (y:Bug {id: 'b1'}) "
            "CREATE (x)-[:mentions {w: 9.0}]->(y)"
        )
        found = txn.execute(ADMITTED)
        assert sorted(row[0] for row in found.rows) == ["d1", "d1", "d1", "d2"]


def test_a_rolled_back_edge_is_not_walked(database: object) -> None:
    """What the transaction undid is not reachable to the next reader."""
    try:
        with database.begin("write") as txn:
            txn.execute(
                "MATCH (x:Decision {id: 'd2'}), (y:Bug {id: 'b1'}) "
                "CREATE (x)-[:mentions {w: 9.0}]->(y)"
            )
            raise RuntimeError("undo this")
    except RuntimeError:
        pass
    assert len(database.execute(ADMITTED).rows) == 3


def test_a_staged_edge_is_the_writers_alone_until_the_reader_moves_on(
    database: object,
) -> None:
    """Owner-only, snapshot and publication, proved in one sequence.

    The fan-out reads the same three views a typed hop reads -- the writer's staged edges, the
    committed heap, and the snapshot a reader opened at -- so an edge that only the writer can
    see must stay invisible to a reader that opened before it, and must stay invisible even
    after the commit, because the reader's snapshot is older than the commit rather than
    unaware of it.
    """
    writer = database.begin("write")
    writer.execute(
        "MATCH (x:Decision {id: 'd2'}), (y:Bug {id: 'b1'}) "
        "CREATE (x)-[:mentions {w: 9.0}]->(y)"
    )
    assert len(writer.execute(ADMITTED).rows) == 4

    outsider = database.begin("read")
    try:
        assert len(outsider.execute(ADMITTED).rows) == 3
        writer.commit()
        # The commit happened, and this reader still answers from where it began.
        assert len(outsider.execute(ADMITTED).rows) == 3
    finally:
        outsider.commit()

    assert len(database.execute(ADMITTED).rows) == 4
