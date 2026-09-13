"""Named captures: decorative optimization, composed reads and unsafe AST refusal."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.analysis import (
    Binding,
    QueryAnalysis,
    analyze,
    named_path,
)
from okto_grafx.domain.query.ast import (
    Direction,
    MatchClause,
    NodePattern,
    PatternPath,
    Property,
    Query,
    RelationshipPattern,
    ReturnClause,
    ReturnItem,
    Variable,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan

NAMED = "MATCH path = (a:A)-[r:R]->(b:B) RETURN a.id, r.layer"
PLAIN = "MATCH (a:A)-[r:R]->(b:B) RETURN a.id, r.layer"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return one typed hop between two labelled tables, with one edge in it."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE R(FROM A TO B, layer STRING)")
    with handle.begin("write") as seed:
        seed.execute("CREATE (n:A {id: 'a1'})")
        seed.execute("CREATE (n:B {id: 'b1'})")
        seed.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'canonical'}]->(b)"
        )
    try:
        yield handle
    finally:
        handle.close()


def _plan(root: object) -> tuple[tuple[str, object], ...]:
    """Return the labels and details of a plan, parents before children."""
    return tuple((node.label, dict(node.details())) for node in root.walk())


# --- the name is written, kept, and changes nothing --------------------------------------------


def test_the_name_survives_the_round_trip_through_the_tree() -> None:
    statement = parse(NAMED)
    pattern = statement.match_clauses[0].patterns[0]

    assert pattern.variable == "path"
    assert pattern.describe() == "path = (a:A)-[r:R]->(b:B)"
    assert statement.describe() == NAMED
    assert named_path(statement) is pattern
    assert named_path(parse(PLAIN)) is None


def test_the_plan_is_the_same_plan_as_without_the_name(database: object) -> None:
    """The decoration claim, stated where it can fail: operators AND their details match."""
    assert _plan(database.explain(NAMED)) == _plan(database.explain(PLAIN))


def test_the_frozen_probe_runs_and_answers_what_the_unnamed_one_answers(
    database: object,
) -> None:
    assert database.execute(NAMED).rows == (("a1", "canonical"),)
    assert database.execute(NAMED).rows == database.execute(PLAIN).rows


def test_no_edge_is_no_rows_and_parallel_edges_are_one_row_each(
    database: object,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute("CREATE (n:A {id: 'a2'})")
        # a2 has no edge at all; a1 gains a second one.
        transaction.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'working'}]->(b)"
        )

    assert sorted(database.execute(NAMED).rows) == [
        ("a1", "canonical"),
        ("a1", "working"),
    ]


def test_the_owner_sees_its_own_edge_and_a_rollback_removes_it(
    database: object,
) -> None:
    transaction = database.begin("write")
    try:
        transaction.execute("CREATE (n:A {id: 'a3'})")
        transaction.execute(
            "MATCH (a:A {id: 'a3'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'staged'}]->(b)"
        )

        assert sorted(transaction.execute(NAMED).rows) == [
            ("a1", "canonical"),
            ("a3", "staged"),
        ]
        assert database.execute(NAMED).rows == (("a1", "canonical"),)
    finally:
        transaction.rollback()

    assert database.execute(NAMED).rows == (("a1", "canonical"),)


# --- native path operations and remaining explicit refusals -------------------------------


@pytest.mark.parametrize("query", [
    "MATCH path = (a:A)-[r:R]->(b:B) RETURN path.length",
    "MATCH path = (a:A)-[r:R]->(b:B) RETURN size(path) AS n",
    "MATCH path = (a:A)-[r:R]->(b:B) RETURN a.id LIMIT path",
])
def test_invalid_path_operations_are_refused_before_execution(database, query):
    with pytest.raises(GrafxPlanError):
        database.explain(query)


def test_path_property_refusal_identifies_the_invalid_operation(database):
    with pytest.raises(GrafxPlanError, match="no property map") as raised:
        database.execute("MATCH path = (a:A)-[r:R]->(b:B) RETURN path.length")
    assert raised.value.details["field"] == "property"


@pytest.mark.parametrize("query,expected", [
    ("MATCH path = (a:A)-[r:R]->(b:B) WHERE path IS NULL RETURN a.id", ()),
    ("MATCH path = (a:A)-[r:R]->(b:B) RETURN a.id ORDER BY path", (("a1",),)),
    ("MATCH path = (a)-[r:R]->(b:B) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[r:R]->(b) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[r:R]-(b:B) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[:R]->(b:B) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A {id:'a1'})-[r:R]->(b:B) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[r:R]->(b:B), (c:A) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[r:R]->(b:B) MATCH (c:A) RETURN a.id", (("a1",),)),
    ("MATCH path = (a:A)-[r:R]->(b:B) WITH a RETURN a.id", (("a1",),)),
    ("UNWIND $rows AS x MATCH path = (a:A)-[r:R]->(b:B) RETURN a.id", (("a1",),)),
    # One MATCH cannot reuse the only relationship across its two patterns.
    ("MATCH path = (a:A)-[r:R]->(b:B), p2 = (c:A)-[q:R]->(d:B) RETURN a.id", ()),
])
def test_composed_decorative_names_keep_exact_results(database, query, expected):
    assert database.execute(query, {"rows": [1]}).rows == expected


@pytest.mark.parametrize("query", [
    "MATCH a = (a:A)-[r:R]->(b:B) RETURN a.id",
    "MATCH r = (a:A)-[r:R]->(b:B) RETURN a.id",
    "MATCH b = (a:A)-[r:R]->(b:B) RETURN a.id",
])
def test_a_path_name_may_not_be_a_name_something_else_answers_to(query):
    with pytest.raises(GrafxPlanError, match="must differ") as raised:
        analyze(parse(query))
    assert raised.value.details["field"] == "pattern"


@pytest.mark.parametrize("query", [
    "MATCH path = (a:A)<-[r:R]-(b:B) RETURN a.id",
    "MATCH path = (a:A)-[r:R]->(b:B)-[q:R]->(c:B) RETURN a.id",
])
def test_wrong_endpoint_schema_remains_refused(database, query):
    with pytest.raises(GrafxPlanError):
        database.explain(query)


def test_named_path_inline_map_filters_instead_of_refusing(database):
    query = "MATCH path = (a:A)-[r:R {layer:$layer}]->(b:B) RETURN a.id,path"
    rows = database.execute(query, {"layer":"canonical"}).rows
    assert len(rows) == 1 and rows[0][0] == "a1"
    assert rows[0][1].relationships[0].properties["layer"] == "canonical"
    assert database.execute(query, {"layer":"missing"}).rows == ()


def test_decorative_path_name_allows_native_relationship_type_inference(database):
    query = "MATCH path = (a:A)-[r]->(b:B) RETURN a.id"
    assert database.execute(query).rows == (("a1",),)
    assert database.explain(query) is not None


@pytest.mark.parametrize("operation,probe,expected", [
    ("SET a.id = 'z'", "MATCH (a:A) RETURN a.id", (("z",),)),
    ("DELETE r", "MATCH (a:A)-[r:R]->(b:B) RETURN r.layer", ()),
])
def test_named_read_before_write_uses_original_transaction(database, operation, probe, expected):
    original = database.execute(probe).rows
    with database.begin("write") as tx:
        tx.execute("MATCH path=(a:A)-[r:R]->(b:B) " + operation)
        assert tx.execute(probe).rows == expected
        assert database.execute(probe).rows == original
    assert database.execute(probe).rows == expected


def test_written_pattern_path_names_reach_the_analysis() -> None:
    """CREATE and MERGE now preserve named captures; shape support is checked later."""
    assert parse("CREATE p = (a:A)-[:R]->(b:B)").updating_clauses[0].patterns[0].variable == "p"
    assert parse("MERGE p = (a:A)").updating_clauses[0].pattern.variable == "p"


# --- a tree nobody parsed ------------------------------------------------------------------------


def _named(
    direction: Direction = Direction.OUTGOING, **overrides: object
) -> PatternPath:
    """Return the frozen named pattern, with one part swapped out when asked."""
    nodes = overrides.get(
        "nodes",
        (
            NodePattern(variable="a", labels=("A",)),
            NodePattern(variable="b", labels=("B",)),
        ),
    )
    return PatternPath(
        nodes=nodes,  # type: ignore[arg-type]
        relationships=(
            RelationshipPattern(variable="r", types=("R",), direction=direction),
        ),
        variable="path",
    )


def _returns_id() -> ReturnClause:
    """Return the RETURN clause the frozen form carries."""
    return ReturnClause(
        items=(ReturnItem(expression=Property(subject=Variable(name="a"), key="id")),)
    )


def _analysis(statement: Query) -> QueryAnalysis:
    """Return an analysis a caller could hand to build_plan for this statement."""
    return QueryAnalysis(
        statement=statement,
        bindings=(
            Binding(name="a", entity="node", labels=("A",), created=False),
            Binding(name="b", entity="node", labels=("B",), created=False),
            Binding(name="r", entity="relationship", labels=("R",), created=False),
        ),
        output_columns=("a.id",),
    )


@pytest.mark.parametrize("direction", tuple(Direction))
def test_analysis_accepts_both_directions_and_typed_endpoint_inference(direction):
    statement = Query(
        match_clauses=(MatchClause(patterns=(_named(direction),)),),
        return_clause=_returns_id(),
    )
    assert analyze(statement).binding("path").entity == "path"


def test_supplied_analysis_cannot_smuggle_a_missing_return(catalog, indexes):
    statement = Query(match_clauses=(MatchClause(patterns=(_named(),)),))
    with pytest.raises(GrafxPlanError) as raised:
        build_plan(statement, catalog=catalog, indexes=indexes, analysis=_analysis(statement))
    assert raised.value.details["field"] == "clause"


@pytest.mark.parametrize("suffix,refused", [
    ("RETURN path.length", True),
    ("WHERE path IS NULL RETURN a.id", False),
    ("RETURN a.id ORDER BY path", False),
])
def test_supplied_analysis_is_recomputed_for_native_paths(catalog, indexes, suffix, refused):
    # Real catalog labels prevent absent A/R tables from hiding scope/type defects.
    statement = parse("MATCH path=(a:Person)-[r:Knows]->(b:Person) " + suffix)
    forged = QueryAnalysis(statement=statement, bindings=(), output_columns=("wrong",))
    if refused:
        with pytest.raises(GrafxPlanError) as raised:
            build_plan(statement, catalog=catalog, indexes=indexes, analysis=forged)
        assert raised.value.details["field"] == "property"
    else:
        planned = build_plan(statement, catalog=catalog, indexes=indexes, analysis=forged)
        assert planned.analysis.output_columns == ("a.id",)
        assert planned.analysis.binding("path").entity == "path"


@pytest.mark.parametrize("collides", ["a", "r", "b"])
def test_a_supplied_analysis_meets_the_collision_rule_too(
    catalog: object, indexes: tuple, collides: str
) -> None:
    """None of these reads the name, so only a structural comparison can catch them."""
    statement = parse(f"MATCH {collides} = (a:A)-[r:R]->(b:B) RETURN a.id")
    forged = QueryAnalysis(
        statement=statement,
        bindings=(
            Binding(name="a", entity="node", labels=("A",), created=False),
            Binding(name="b", entity="node", labels=("B",), created=False),
            Binding(name="r", entity="relationship", labels=("R",), created=False),
        ),
        output_columns=("a.id",),
    )

    with pytest.raises(GrafxPlanError) as raised:
        build_plan(statement, catalog=catalog, indexes=indexes, analysis=forged)
    assert raised.value.details["field"] == "pattern"


@pytest.mark.parametrize(
    ("name", "variable"),
    [
        ("empty", ""),
        ("an integer", 0),
        ("a boolean", True),
        ("a name with a space", "not safe"),
        ("a name past the lexer's ceiling", "x" * 200),
        ("a name carrying a back quote", "we`ird"),
        ("a name starting with a digit", "1path"),
        ("a name carrying punctuation", "path!"),
    ],
)
def test_a_name_the_parser_could_not_have_written_is_refused(
    name: str, variable: object
) -> None:
    """`describe()` writes a path name back bare, so a name needing quotes would not re-read.

    The question is put to the LEXER rather than answered again here, which is also what keeps
    the ceiling on a name's length in one place.
    """
    statement = Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=(
                            NodePattern(variable="a", labels=("A",)),
                            NodePattern(variable="b", labels=("B",)),
                        ),
                        relationships=(
                            RelationshipPattern(variable="r", types=("R",)),
                        ),
                        variable=variable,  # type: ignore[arg-type]
                    ),
                )
            ),
        ),
        return_clause=_returns_id(),
    )

    with pytest.raises(GrafxPlanError) as raised:
        analyze(statement)
    assert raised.value.details["field"] == ("pattern" if type(variable) is str else "ast"), name


class _HostileName(str):
    """A str subclass whose indexing raises, which is not what the parser ever produces."""

    def __getitem__(self, item: object) -> str:  # noqa: D105 - the raising is the point
        raise RuntimeError("this name refuses to be read")


def test_a_name_that_is_not_a_builtin_string_is_refused_before_it_is_touched() -> None:
    """Checked by exact type, not isinstance: a subclass could carry an exception out.

    A tokenizer handed a str whose __getitem__ raises would surface a RuntimeError where a
    typed refusal belongs, and a caller cannot act on that.
    """
    statement = Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=(
                            NodePattern(variable="a", labels=("A",)),
                            NodePattern(variable="b", labels=("B",)),
                        ),
                        relationships=(
                            RelationshipPattern(variable="r", types=("R",)),
                        ),
                        variable=_HostileName("path"),
                    ),
                )
            ),
        ),
        return_clause=_returns_id(),
    )

    with pytest.raises(GrafxPlanError) as raised:
        analyze(statement)
    assert raised.value.details["field"] == "ast"


@pytest.mark.parametrize(
    ("name", "nodes", "relationships"),
    [
        ("one node and one hop", 1, 1),
        ("two nodes and two hops", 2, 2),
        ("no nodes at all", 0, 0),
    ],
)
def test_a_malformed_path_is_refused_rather_than_crashing_its_own_message(
    catalog: object, name: str, nodes: int, relationships: int
) -> None:
    """The detail cannot walk a tree already judged malformed, so it does not try.

    `describe()` on a path with one node and one hop indexes past its own nodes. A refusal that
    raises while building its own message reports nothing and hides what it found.
    """
    node = NodePattern(variable="a", labels=("A",))
    hop = RelationshipPattern(variable="r", types=("R",))
    statement = Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=tuple(node for _ in range(nodes)),
                        relationships=tuple(hop for _ in range(relationships)),
                        variable="path",
                    ),
                )
            ),
        ),
        return_clause=_returns_id(),
    )

    with pytest.raises(GrafxPlanError) as raised:
        analyze(statement)
    assert raised.value.details["field"] == "pattern", name

    with pytest.raises(GrafxPlanError):
        build_plan(
            statement,
            catalog=catalog,
            analysis=QueryAnalysis(
                statement=statement,
                bindings=(
                    Binding(name="a", entity="node", labels=("A",), created=False),
                ),
                output_columns=("a.id",),
            ),
        )


def test_an_alias_may_carry_the_path_name_because_it_reads_nothing() -> None:
    """Adjudicated scope: an alias DEFINES a column, and defining is not reading.

    Refusing it would widen the batch past what the freeze pins, which is reads of the path
    and collisions with the nodes and relationships of the pattern.
    """
    analysis = analyze(parse("MATCH path = (a:A)-[r:R]->(b:B) RETURN a.id AS path"))

    assert analysis.output_columns == ("path",)


def test_the_valid_form_still_plans_under_a_supplied_analysis(
    catalog: object, indexes: tuple
) -> None:
    """The control for the four above: the door refuses reads, not the form itself."""
    statement = parse("MATCH path = (a:Person)-[r:Knows]->(b:Person) RETURN a.id")
    supplied = QueryAnalysis(
        statement=statement,
        bindings=(
            Binding(name="a", entity="node", labels=("Person",), created=False),
            Binding(name="b", entity="node", labels=("Person",), created=False),
            Binding(name="r", entity="relationship", labels=("Knows",), created=False),
        ),
        output_columns=("a.id",),
    )

    planned = build_plan(statement, catalog=catalog, indexes=indexes, analysis=supplied)
    # The decorative name changes neither semantics nor the selected access path. A large,
    # unbounded typed hop may therefore use the same endpoint-validating edge-first scan as its
    # unnamed twin; only a path value that is actually projected requires traversal material.
    assert "RelationshipScan" in tuple(node.label for node in planned.root.walk())


def test_a_named_path_infers_both_endpoints_from_its_relationship_type(
    catalog: object, indexes: tuple
) -> None:
    """A decorative path shares the typed source/target resolution of its unnamed twin."""
    statement = Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=(NodePattern(variable="a"), NodePattern(variable="b")),
                        relationships=(
                            RelationshipPattern(
                                variable="r",
                                types=("Knows",),
                                direction=Direction.OUTGOING,
                            ),
                        ),
                        variable="path",
                    ),
                )
            ),
        ),
        return_clause=_returns_id(),
    )

    assert build_plan(statement, catalog=catalog, indexes=indexes) is not None

    unnamed = parse("MATCH (a)-[r:Knows]->(b) RETURN a.id")
    assert build_plan(unnamed, catalog=catalog, indexes=indexes) is not None
