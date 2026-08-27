"""An omitted upper bound, read the way the endpoint already reads it.

`*`, `*..` and `*n..` are legal at the public endpoint, which rewrites them to twenty hops from
its own MAX_TRAVERSAL_DEPTH before any engine sees the text. Refusing them here meant refusing
something a caller had been told was fine, and the refusal bought nothing: the rewrite had
already happened. So the engine reads the omission the same way, and the accepted form is
written back as the explicit range it became -- a canonicalisation, not a guess.

The bound itself is not softened anywhere. A traversal is finite in every spelling, an explicit
upper still reaches thirty and no further, and a hop range handed in by a caller rather than
written by the parser is checked before anything walks it: a forged `max_hops` is not a wrong
answer, it is unbounded work.
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
from okto_grafx.domain.query.analysis import Binding, QueryAnalysis, analyze
from okto_grafx.domain.query.ast import (
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
from okto_grafx.domain.query.limits import DEFAULT_TRAVERSAL_HOPS, MAX_TRAVERSAL_HOPS
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return a three-node cycle, which is where an unbounded walk would not terminate."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE R(FROM A TO A, layer STRING)")
    with handle.begin("write") as seed:
        for identity in ("a1", "a2", "a3"):
            seed.execute("CREATE (:A {id: $id})", {"id": identity})
        for source, target in (("a1", "a2"), ("a2", "a3"), ("a3", "a1")):
            seed.execute(
                "MATCH (x:A {id: $s}), (y:A {id: $t}) CREATE (x)-[:R {layer: 'l'}]->(y)",
                {"s": source, "t": target},
            )
    try:
        yield handle
    finally:
        handle.close()


def _hop(text: str) -> RelationshipPattern:
    """Return the one relationship pattern of a parsed query."""
    return parse(text).match_clauses[0].patterns[0].relationships[0]


def _reached(handle: object, spelling: str) -> list[str]:
    """Return the nodes one spelling reaches from a1, in a stable order."""
    rows = handle.execute(
        f"MATCH (x:A {{id: 'a1'}})-[r:R{spelling}]->(y:A) RETURN y.id"
    ).rows
    return sorted(row[0] for row in rows)


# --- the three spellings the endpoint normalises ------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "bounds"),
    [("*", (1, 20)), ("*..", (1, 20)), ("*3..", (3, 20)), ("*1..", (1, 20))],
)
def test_an_omitted_upper_bound_takes_the_default(
    spelling: str, bounds: tuple[int, int]
) -> None:
    hop = _hop(f"MATCH (a:A)-[r:R{spelling}]->(b:A) RETURN a.id")

    assert (hop.min_hops, hop.max_hops) == bounds
    assert hop.max_hops == DEFAULT_TRAVERSAL_HOPS
    assert hop.variable_length is True
    assert hop.hop_range_written is True


def test_the_accepted_form_is_written_back_as_the_range_it_became() -> None:
    """Canonicalisation, not a guess: what it means is what it says once it is read."""
    assert _hop("MATCH (a:A)-[r:R*]->(b:A) RETURN a.id").describe() == "-[r:R*1..20]->"
    assert (
        _hop("MATCH (a:A)-[r:R*..]->(b:A) RETURN a.id").describe() == "-[r:R*1..20]->"
    )
    assert (
        _hop("MATCH (a:A)-[r:R*3..]->(b:A) RETURN a.id").describe() == "-[r:R*3..20]->"
    )


@pytest.mark.parametrize(
    ("spelling", "bounds"),
    [("*2..7", (2, 7)), ("*..5", (1, 5)), ("*4", (4, 4)), ("*1..30", (1, 30))],
)
def test_a_range_that_writes_its_upper_bound_is_untouched(
    spelling: str, bounds: tuple[int, int]
) -> None:
    hop = _hop(f"MATCH (a:A)-[r:R{spelling}]->(b:A) RETURN a.id")

    assert (hop.min_hops, hop.max_hops) == bounds


def test_the_two_ceilings_are_different_numbers() -> None:
    """One is what a query may WRITE; the other is what it gets when it writes nothing."""
    assert DEFAULT_TRAVERSAL_HOPS == 20
    assert MAX_TRAVERSAL_HOPS == 30
    assert DEFAULT_TRAVERSAL_HOPS < MAX_TRAVERSAL_HOPS


# --- what it answers ------------------------------------------------------------------------------


def test_an_omitted_bound_answers_exactly_what_the_explicit_range_answers(
    database: object,
) -> None:
    """The equivalence is the claim; a cycle is where an unbounded walk would not return."""
    assert _reached(database, "*") == ["a1", "a2", "a3"]
    assert _reached(database, "*") == _reached(database, "*1..20")
    assert _reached(database, "*..") == _reached(database, "*1..20")


def test_the_written_lower_bound_survives_the_default(database: object) -> None:
    """`*2..` starts at two hops: a3 is two away and a1 is three, a2 at one is excluded."""
    assert _reached(database, "*2..") == ["a1", "a3"]
    assert _reached(database, "*2..") == _reached(database, "*2..20")


def test_an_explicit_range_still_stops_where_it_says(database: object) -> None:
    assert _reached(database, "*1..2") == ["a2", "a3"]
    assert _reached(database, "*1..1") == ["a2"]


def test_the_walk_terminates_because_an_edge_is_not_walked_twice(
    database: object,
) -> None:
    """Three nodes in a cycle answer three rows, not twenty and not forever.

    The bound is what makes the query finite in principle; relationship isomorphism is what
    makes it stop at three here. Both are load-bearing, and this asserts the COUNT so that a
    change to either shows up.
    """
    rows = database.execute("MATCH (x:A {id: 'a1'})-[r:R*]->(y:A) RETURN y.id").rows

    assert len(rows) == 3


def test_the_plan_is_the_plan_of_the_explicit_range(database: object) -> None:
    def shape(query: str) -> tuple[tuple[str, object], ...]:
        return tuple(
            (node.label, dict(node.details()))
            for node in database.explain(query).walk()
        )

    assert shape("MATCH (x:A)-[r:R*]->(y:A) RETURN y.id") == shape(
        "MATCH (x:A)-[r:R*1..20]->(y:A) RETURN y.id"
    )


def test_the_intermediate_budget_still_counts_what_the_walk_produces(
    tmp_path: Path,
) -> None:
    """A default bound is not an exemption: the rows it produces are admitted like any other."""
    # Seeded without a budget, so what the budget refuses below is the WALK and not the setup.
    builder = okto_grafx.connect(tmp_path / "budget")
    try:
        with builder.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE R(FROM A TO A, layer STRING)")
        with builder.begin("write") as seed:
            for identity in ("a1", "a2", "a3"):
                seed.execute("CREATE (:A {id: $id})", {"id": identity})
            for source, target in (("a1", "a2"), ("a2", "a3"), ("a3", "a1")):
                seed.execute(
                    "MATCH (x:A {id: $s}), (y:A {id: $t}) CREATE (x)-[:R {layer: 'l'}]->(y)",
                    {"s": source, "t": target},
                )
    finally:
        builder.close()

    handle = okto_grafx.connect(tmp_path / "budget", max_intermediate_rows=2)
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            handle.execute("MATCH (x:A {id: 'a1'})-[r:R*]->(y:A) RETURN y.id")
        assert raised.value.details["operator"] == "TraverseRelationship"
    finally:
        handle.close()


# --- what is still refused --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "field"),
    [
        ("*0..", "min_hops"),
        ("*0..3", "min_hops"),
        ("*25..", "min_hops"),
        ("*3..2", "min_hops"),
        ("*1..31", "hops"),
        ("*31", "hops"),
    ],
)
def test_the_shapes_that_were_refused_are_still_refused(
    spelling: str, field: str
) -> None:
    """`*25..` is the interesting one: the default makes it 25..20, which is an empty range."""
    with pytest.raises(GrafxParseError) as raised:
        parse(f"MATCH (a:A)-[r:R{spelling}]->(b:A) RETURN a.id")

    assert raised.value.details["field"] == field


# --- a range nobody parsed ----------------------------------------------------------------------


class _Hostile:
    """A value that raises whenever anything asks it to render or index itself."""

    def __repr__(self) -> str:  # noqa: D105 - the raising is the point
        raise RuntimeError("this value refuses to be reported")

    def __format__(self, spec: str) -> str:  # noqa: D105 - the raising is the point
        raise RuntimeError("this value refuses to be formatted")

    def __index__(self) -> int:  # noqa: D105 - the raising is the point
        raise RuntimeError("this value refuses to be an index")


def _forged(**fields: object) -> Query:
    """Return the frozen shape with one hop range swapped for whatever the caller passed."""
    written = fields.pop("hop_range_written", True)
    return Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=(
                            NodePattern(variable="a", labels=("Person",)),
                            NodePattern(variable="b", labels=("Person",)),
                        ),
                        relationships=(
                            RelationshipPattern(
                                variable="r",
                                types=("Knows",),
                                hop_range_written=written,  # type: ignore[arg-type]
                                **fields,  # type: ignore[arg-type]
                            ),
                        ),
                    ),
                )
            ),
        ),
        return_clause=ReturnClause(
            items=(
                ReturnItem(expression=Property(subject=Variable(name="a"), key="id")),
            )
        ),
    )


def _supplied_analysis(statement: Query) -> QueryAnalysis:
    """Return an analysis a caller could hand to build_plan for this statement.

    Its presence is the point: build_plan analyses the statement itself when none is given, so
    a test that omits it never reaches the planner's own check.
    """
    return QueryAnalysis(
        statement=statement,
        bindings=(
            Binding(name="a", entity="node", labels=("Person",), created=False),
            Binding(name="b", entity="node", labels=("Person",), created=False),
            Binding(name="r", entity="relationship", labels=("Knows",), created=False),
        ),
        output_columns=("a.id",),
    )


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("an upper bound that is a bool", {"min_hops": 1, "max_hops": True}),
        ("a lower bound that is a bool", {"min_hops": True, "max_hops": 3}),
        ("an upper bound that is a float", {"min_hops": 1, "max_hops": 2.5}),
        ("an upper bound past the ceiling", {"min_hops": 1, "max_hops": 10_000}),
        ("a lower bound below one", {"min_hops": 0, "max_hops": 3}),
        ("a lower bound above the upper", {"min_hops": 5, "max_hops": 2}),
        ("an object that refuses to be read", {"min_hops": 1, "max_hops": _Hostile()}),
        (
            "a written flag that is not a bool",
            {"min_hops": 1, "max_hops": 3, "hop_range_written": 1},
        ),
        (
            "a range that claims never to have been written",
            {"min_hops": 1, "max_hops": 20, "hop_range_written": False},
        ),
        (
            "two hops that claim never to have been written",
            {"min_hops": 2, "max_hops": 2, "hop_range_written": False},
        ),
    ],
)
def test_a_forged_hop_range_is_refused_at_both_doors(
    catalog: object, indexes: tuple, name: str, fields: dict
) -> None:
    """Both doors, because build_plan takes a caller's analysis and analyze takes its tree.

    The hostile case is the one that says why the refusal interpolates nothing: a value asked
    to render itself while the message is built can raise, and a refusal that raises reports
    nothing at all.
    """
    statement = _forged(**fields)

    with pytest.raises(GrafxPlanError) as analysed:
        analyze(statement)
    assert analysed.value.details["field"] == "hops", name

    # The analysis is SUPPLIED, so build_plan does not fall back to analyze() -- without this
    # the assertion below would be re-testing the door above and proving nothing about the
    # planner.
    with pytest.raises(GrafxPlanError) as planned:
        build_plan(
            statement,
            catalog=catalog,
            indexes=indexes,
            analysis=_supplied_analysis(statement),
        )
    assert planned.value.details["field"] == "hops", name


def test_a_legitimate_range_passes_both_doors(catalog: object, indexes: tuple) -> None:
    """The control: the guard refuses forged fields, not variable-length traversal."""
    statement = _forged(min_hops=1, max_hops=DEFAULT_TRAVERSAL_HOPS)

    assert analyze(statement) is not None
    planned = build_plan(
        statement,
        catalog=catalog,
        indexes=indexes,
        analysis=_supplied_analysis(statement),
    )
    assert "TraverseRelationship" in tuple(node.label for node in planned.root.walk())
