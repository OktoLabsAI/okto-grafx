"""The near end of a typed hop, read from the relationship that declares it.

A relationship says what sits at each of its ends, and the engine already used the far half of
that: ``(a:X)-[r:T]->(b)`` binds b to T's TO table without b naming a label. Pulse writes the
same pattern with NEITHER end labelled, because it queries by edge type and does not care which
node table the edge happens to join. This file holds the near half of the rule -- and, just as
carefully, the shapes that must keep refusing, because reading a name from a schema is only
right where the schema answers unambiguously.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.analysis import Binding, QueryAnalysis, analyze
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
    SetClause,
    SetItem,
    Literal,
    Variable,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan

I01 = "MATCH (a)-[r:R]->(b) RETURN r.layer, r.rule_id"
"""The frozen I01 text; ``test_the_two_frozen_templates_are_the_ones_used`` pins it."""
CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "pulse_query_corpus_1_0.json"


def _frozen() -> dict:
    """Return the frozen corpus, which is the authority every gate below is read from."""
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _corpus_template(entry_id: str, rel_name: str) -> str:
    """Return one frozen internal template with its relationship hole filled.

    Read from the freeze rather than retyped. An abbreviation of I02 would exercise a query
    Pulse does not send, and what makes I02 worth its own gate is exactly the long chain of
    coalesce() over BOTH ends -- shortening it is how a test comes to pass for a shape nobody
    ships.
    """
    entry = next(item for item in _frozen()["entries"] if item["id"] == entry_id)
    return " ".join(entry["template"].split()).replace("<<rel_name>>", rel_name)


I02 = _corpus_template("I02", "R")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return two node tables joined by one typed relationship, with one edge in it."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE A(id STRING, kind_of STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE NODE TABLE B(id STRING, kind_of STRING, PRIMARY KEY(id))"
        )
        schema.execute("CREATE REL TABLE R(FROM A TO B, layer STRING, rule_id STRING)")
    with handle.begin("write") as seed:
        seed.execute("CREATE (n:A {id: 'a1', kind_of: 'decision'})")
        seed.execute("CREATE (n:B {id: 'b1', kind_of: 'note'})")
        seed.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'canonical', rule_id: 'rule-1'}]->(b)"
        )
    try:
        yield handle
    finally:
        handle.close()


def _operators(root: object) -> tuple[str, ...]:
    """Return the labels in one public plan, parents before children."""
    return tuple(node.label for node in root.walk())


def _scan_table(root: object) -> str:
    """Return the table the driving scan of a plan reads."""
    scan = next(node for node in root.walk() if node.label == "NodeScan")
    return str(scan.details()["table"])


# --- the frozen form ------------------------------------------------------------------------


def test_the_source_takes_the_table_the_relationship_declares(database: object) -> None:
    plan = database.explain(I01)

    # No new operator: the source is an ordinary scan of A, and the hop is the ordinary hop.
    assert _operators(plan) == (
        "ProduceResults",
        "ProjectRows",
        "TraverseRelationship",
        "NodeScan",
        "SingleRow",
    )
    assert _scan_table(plan) == "A"
    assert database.execute(I01).rows == (("canonical", "rule-1"),)


def test_no_edge_at_all_is_no_rows_rather_than_a_refusal(tmp_path: Path) -> None:
    handle = okto_grafx.connect(tmp_path / "empty")
    try:
        with handle.begin("write") as schema:
            schema.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            schema.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
            schema.execute(
                "CREATE REL TABLE R(FROM A TO B, layer STRING, rule_id STRING)"
            )
        with handle.begin("write") as seed:
            seed.execute("CREATE (n:A {id: 'a1'})")

        assert handle.execute(I01).rows == ()
        assert _scan_table(handle.explain(I01)) == "A"
    finally:
        handle.close()


def test_parallel_edges_between_one_pair_are_one_row_each(database: object) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'working', rule_id: 'rule-2'}]->(b)"
        )

    rows = database.execute(I01).rows
    assert sorted(rows) == [("canonical", "rule-1"), ("working", "rule-2")]


def test_a_property_the_edge_left_unset_reads_as_null(database: object) -> None:
    with database.begin("write") as transaction:
        transaction.execute("CREATE (n:A {id: 'a2'})")
        transaction.execute(
            "MATCH (a:A {id: 'a2'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'canonical'}]->(b)"
        )

    # A null and a string do not compare, so the unset one is ordered as the empty string.
    assert sorted(
        database.execute(I01).rows, key=lambda row: (row[0], row[1] or "")
    ) == [
        ("canonical", None),
        ("canonical", "rule-1"),
    ]


def test_both_ends_are_readable_and_a_typo_still_refuses(database: object) -> None:
    """The ends have TABLES here, so an unknown column is a mistake and still says so."""
    assert database.execute(
        "MATCH (a)-[r:R]->(b) RETURN a.kind_of, b.kind_of"
    ).rows == (("decision", "note"),)

    with pytest.raises(GrafxPlanError) as raised:
        database.execute("MATCH (a)-[r:R]->(b) RETURN a.no_such_column")
    assert raised.value.details["field"] == "column"


def test_the_code_traceability_filter_blocks_at_either_end(database: object) -> None:
    """The frozen I02 tests BOTH ends, so each end must be able to remove a row on its own."""
    with database.begin("write") as transaction:
        transaction.execute("CREATE (n:A {id: 'a-code', kind_of: 'code_evidence'})")
        transaction.execute("CREATE (n:B {id: 'b-plain', kind_of: 'note'})")
        transaction.execute(
            "CREATE (n:B {id: 'b-code', kind_of: 'implementation_target'})"
        )
        # b1 is the seeded target and is ordinary; the fixture's own edge is the clean one.
        transaction.execute(
            "MATCH (a:A {id: 'a-code'}), (b:B {id: 'b-plain'}) "
            "CREATE (a)-[:R {layer: 'canonical', rule_id: 'blocked-source'}]->(b)"
        )
        transaction.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b-code'}) "
            "CREATE (a)-[:R {layer: 'canonical', rule_id: 'blocked-target'}]->(b)"
        )

    allowed = database.execute(I02, {"include_code_traceability": True}).rows
    assert sorted(row[1] for row in allowed) == [
        "blocked-source",
        "blocked-target",
        "rule-1",
    ]

    filtered = database.execute(I02, {"include_code_traceability": False}).rows
    assert sorted(row[1] for row in filtered) == ["rule-1"]


def test_the_two_frozen_templates_are_the_ones_used() -> None:
    """The texts above are the corpus texts, not paraphrases that happen to pass."""
    assert _corpus_template("I01", "R") == I01
    assert I02.startswith("MATCH (a)-[r:R]->(b) WHERE ")
    assert I02.count("coalesce(a.kind_of, '')") == 3
    assert I02.count("coalesce(b.kind_of, '')") == 3


def test_the_owner_sees_the_edge_it_staged_and_a_rollback_removes_it(
    database: object,
) -> None:
    transaction = database.begin("write")
    try:
        transaction.execute("CREATE (n:A {id: 'a3'})")
        transaction.execute(
            "MATCH (a:A {id: 'a3'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {layer: 'staged', rule_id: 'rule-3'}]->(b)"
        )

        assert sorted(transaction.execute(I01).rows) == [
            ("canonical", "rule-1"),
            ("staged", "rule-3"),
        ]
        # Nobody outside the transaction sees it while it is only staged.
        assert database.execute(I01).rows == (("canonical", "rule-1"),)
    finally:
        transaction.rollback()

    assert database.execute(I01).rows == (("canonical", "rule-1"),)


# --- the shapes that keep the refusal they had ------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)<-[r:R]-(b) RETURN r.layer",
        "MATCH (a)-[r:R]-(b) RETURN r.layer",
        "MATCH (a)-[r:R]->() RETURN r.layer",
        "MATCH (a)-[r]->(b) RETURN a.id",
        "MATCH (a {id: 'a1'})-[r:R]->(b) RETURN r.layer",
        "MATCH (a)-[r:R]->(b {id: 'b1'}) RETURN r.layer",
        "MATCH (a)-[r:R*1..2]->(b) RETURN a.id",
        "MATCH (a)-[r:R]->(b) SET a.kind_of = 'z'",
        "MATCH (a)-[r:R]->(b) DELETE r",
        "MATCH (a)-[r:R]->(b) WITH r RETURN r.layer",
        "MATCH (a)-[r:R]->(b), (c:A) RETURN r.layer",
        "MATCH (a)-[r:R]->(b) MATCH (c:A) RETURN r.layer",
        "MATCH (a)-[:R]->(b) RETURN a.id",
        "MATCH (a)-[r:R]->(b:B) RETURN r.layer",
        "MATCH (a)-[r:R]->(b)-[q:R]->(c) RETURN r.layer",
        "MATCH (a)-[r:R*1..1]->(b) RETURN r.layer",
        "MATCH ()-[r:R]->(b) RETURN r.layer",
        "MATCH (a)-[r:R {layer: 'canonical'}]->(b) RETURN r.layer",
        "MATCH (a)-[r:R|Q]->(b) RETURN r.layer",
    ],
)
def test_every_shape_outside_the_frozen_one_keeps_its_refusal(
    database: object, query: str
) -> None:
    """The message is the one these shapes already had: this batch adds no new vocabulary."""
    with pytest.raises(GrafxPlanError) as raised:
        database.execute(query, {"rows": [1]})

    assert raised.value.details["field"] == "labels"
    assert "exactly one label" in str(raised.value)


def test_a_written_hop_range_is_outside_the_form_even_when_it_means_one_hop() -> None:
    """`*1..1` matches exactly one hop, and is still a range that the frozen text does not have.

    The counts cannot tell the two apart -- both are min 1, max 1 -- so the pattern carries
    whether a star was typed. Without that, a subset frozen without variable-length traversal
    would admit `*1..1` for being semantically equal to a form it never froze.
    """

    plain = parse("MATCH (a)-[r:R]->(b) RETURN r.layer")
    starred = parse("MATCH (a)-[r:R*1..1]->(b) RETURN r.layer")
    plain_hop = plain.match_clauses[0].patterns[0].relationships[0]
    starred_hop = starred.match_clauses[0].patterns[0].relationships[0]

    assert (plain_hop.min_hops, plain_hop.max_hops) == (
        starred_hop.min_hops,
        starred_hop.max_hops,
    )
    assert plain_hop.variable_length is starred_hop.variable_length is False
    assert plain_hop.hop_range_written is False
    assert starred_hop.hop_range_written is True
    assert starred_hop.describe() == "-[r:R*1..1]->"


def test_unwind_before_the_form_is_refused_for_the_clause_it_is(
    database: object,
) -> None:
    """UNWIND names its own tail rule, and that refusal arrives first; it is not widened here."""
    with pytest.raises(GrafxPlanError) as raised:
        database.execute(
            "UNWIND $rows AS x MATCH (a)-[r:R]->(b) RETURN r.layer", {"rows": [1]}
        )

    assert raised.value.details["field"] == "clause"


def test_a_labelled_source_keeps_planning_exactly_as_before(database: object) -> None:
    assert database.execute("MATCH (a:A)-[r:R]->(b) RETURN r.layer").rows == (
        ("canonical",),
    )
    assert _scan_table(database.explain("MATCH (a:A)-[r:R]->(b) RETURN r.layer")) == "A"


def _frozen_pattern(direction: Direction = Direction.OUTGOING) -> PatternPath:
    """Return the (a)-[r:R]->(b) pattern of the frozen form, as a tree."""
    return PatternPath(
        nodes=(NodePattern(variable="a"), NodePattern(variable="b")),
        relationships=(
            RelationshipPattern(variable="r", types=("R",), direction=direction),
        ),
    )


def _supplied_analysis(statement: Query) -> QueryAnalysis:
    """Return an analysis a caller could hand to build_plan for this statement."""
    return QueryAnalysis(
        statement=statement,
        bindings=(
            Binding(name="a", entity="node", labels=(), created=False),
            Binding(name="b", entity="node", labels=(), created=False),
            Binding(name="r", entity="relationship", labels=("R",), created=False),
        ),
        output_columns=("r.layer",),
    )


def _returns_layer() -> ReturnClause:
    """Return the RETURN clause the frozen form carries."""
    return ReturnClause(
        items=(
            ReturnItem(expression=Property(subject=Variable(name="r"), key="layer")),
        )
    )


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        (
            "a write nobody parsed",
            Query(
                match_clauses=(MatchClause(patterns=(_frozen_pattern(),)),),
                updating_clauses=(
                    SetClause(
                        items=(
                            SetItem(
                                target=Property(
                                    subject=Variable(name="a"), key="kind_of"
                                ),
                                value=Literal(value="z"),
                            ),
                        )
                    ),
                ),
                return_clause=_returns_layer(),
            ),
        ),
        (
            "an incoming hop nobody parsed",
            Query(
                match_clauses=(
                    MatchClause(patterns=(_frozen_pattern(Direction.INCOMING),)),
                ),
                return_clause=_returns_layer(),
            ),
        ),
        (
            "no RETURN at all",
            Query(match_clauses=(MatchClause(patterns=(_frozen_pattern(),)),)),
        ),
    ],
)
def test_a_caller_supplying_its_own_analysis_cannot_widen_the_form(
    catalog: object, indexes: tuple, name: str, statement: Query
) -> None:
    """The form is decided from the STATEMENT, so an analysis handed in cannot vouch for it."""
    with pytest.raises(GrafxPlanError) as raised:
        build_plan(
            statement,
            catalog=catalog,
            indexes=indexes,
            analysis=_supplied_analysis(statement),
        )

    assert raised.value.details["field"] == "labels", name


def test_the_analysis_admits_what_the_planner_refuses(catalog: object) -> None:
    """The planner is the door, and this is what proves it is load-bearing.

    A write over a label-free path is a perfectly meaningful statement to the analysis: the
    variables are bound, the target is a bound variable, nothing is aggregated where it may not
    be. So analyze() ACCEPTS it, and if the planner did not carry the shape rule the form would
    widen silently. Asserting the acceptance is the falsifiable half of the claim; asserting
    only the refusal below would leave it an assumption.
    """
    statement = Query(
        match_clauses=(MatchClause(patterns=(_frozen_pattern(),)),),
        updating_clauses=(
            SetClause(
                items=(
                    SetItem(
                        target=Property(subject=Variable(name="a"), key="kind_of"),
                        value=Literal(value="z"),
                    ),
                )
            ),
        ),
        return_clause=_returns_layer(),
    )

    analysed = analyze(statement)
    assert [binding.name for binding in analysed.bindings] == ["a", "b", "r"]

    with pytest.raises(GrafxPlanError) as raised:
        build_plan(statement, catalog=catalog, analysis=analysed)
    assert raised.value.details["field"] == "labels"


def test_a_type_that_names_a_node_table_is_refused_as_a_type(database: object) -> None:
    """The hop's own refusal, not the source's: the mistake is the type, and it says so."""
    with pytest.raises(GrafxPlanError) as raised:
        database.execute("MATCH (a)-[r:B]->(b) RETURN r.layer")

    assert raised.value.details == {"field": "type", "value": "B"}
    assert "cannot match a relationship" in str(raised.value)
    # A labelled source has always answered this way; the new form now answers the same.
    with pytest.raises(GrafxPlanError) as labelled:
        database.execute("MATCH (a:A)-[r:B]->(b) RETURN r.layer")
    assert labelled.value.details == raised.value.details


def test_a_type_no_table_declares_is_refused_by_name(database: object) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.execute("MATCH (a)-[r:NoSuchType]->(b) RETURN r.layer")

    assert raised.value.details == {"field": "type", "value": "NoSuchType"}


def test_the_frozen_form_itself_plans_from_a_tree_nobody_parsed(
    catalog: object, indexes: tuple
) -> None:
    """The control for the three above: the same door admits the shape it is meant to admit."""
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

    planned = build_plan(statement, catalog=catalog, indexes=indexes)
    assert "NodeScan" in tuple(node.label for node in planned.root.walk())


# --- every relationship type the corpus froze --------------------------------------------------


def _frozen_endpoints() -> dict[str, tuple[str, str]]:
    """Return one representative endpoint pair per relationship type, from the freeze itself.

    Read from the frozen corpus rather than written out here: the point of the check is that
    the engine agrees with what was frozen, and a copy of the list in this file could only ever
    disagree with it silently.
    """
    frozen = json.loads(CORPUS.read_text(encoding="utf-8"))
    behaviours = {
        entry["name"]: entry
        for entry in frozen["public_raw_contract"]["behaviour"]["behaviours"]
    }
    pairs: dict[str, tuple[str, str]] = {}
    for written in behaviours["endpoint pairs"]["value"]:
        edge, ends = written.split("(", 1)
        source, target = ends.rstrip(")").split("->")
        pairs.setdefault(edge, (source, target))
    declared = behaviours["relationship domain"]["value"]
    assert set(pairs) == set(declared), "the matrix and the domain disagree"
    return pairs


@pytest.mark.parametrize("edge_type", sorted(_frozen_endpoints()))
def test_each_frozen_relationship_type_names_the_table_of_its_near_end(
    tmp_path: Path, edge_type: str
) -> None:
    pairs = _frozen_endpoints()
    source, target = pairs[edge_type]
    handle = okto_grafx.connect(tmp_path / f"matrix-{edge_type}")
    try:
        with handle.begin("write") as schema:
            for label in dict.fromkeys((source, target)):
                schema.execute(
                    f"CREATE NODE TABLE {label}(id STRING, kind_of STRING, PRIMARY KEY(id))"
                )
            schema.execute(
                f"CREATE REL TABLE {edge_type}"
                f"(FROM {source} TO {target}, layer STRING, rule_id STRING)"
            )

        for entry_id in ("I01", "I02"):
            query = _corpus_template(entry_id, edge_type)
            assert _scan_table(handle.explain(query)) == source, entry_id
            parameters = (
                {} if entry_id == "I01" else {"include_code_traceability": True}
            )
            assert handle.execute(query, parameters).rows == (), entry_id
    finally:
        handle.close()
