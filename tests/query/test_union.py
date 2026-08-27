"""Two reading queries answered as one result, and everything that must not be one.

The pair is deliberately narrow: two branches, both read-only, both ending in RETURN, the same
arity, and column names taken from the left branch alone. What makes it worth its own file is
not the happy path but the edges around it -- a word that only looks like the keyword, a tree
nobody parsed, a type the two sides cannot agree about, and a budget that has to count the rows
BEFORE the duplicates are removed rather than after.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine import query_engine
from okto_grafx.domain.errors import (
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.query.analysis import QueryAnalysis, analyze
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    MatchClause,
    NodePattern,
    PatternPath,
    Property,
    Query,
    ReturnClause,
    ReturnItem,
    UnionQuery,
    Variable,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.limits import MAX_EXPRESSION_DEPTH
from okto_grafx.domain.query.plan import UnionRows
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.engine.heap_store import HeapStore
from tests.query.conftest import build_catalog


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return two node tables whose rows overlap, so a duplicate can cross the branches."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id STRING, n INT64, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id STRING, n INT64, PRIMARY KEY(id))")
    with handle.begin("write") as seed:
        for table, identity, number in (("A", "x", 1), ("A", "y", 2), ("B", "x", 3)):
            seed.execute(
                f"CREATE (:{table} {{id: $id, n: $n}})", {"id": identity, "n": number}
            )
    try:
        yield handle
    finally:
        handle.close()


# --- the shape of the plan ----------------------------------------------------------------------


def test_the_plan_is_the_pair_of_pipelines_under_one_distinct() -> None:
    """The union is one tree: the branches lose their own results and gain a shared one."""
    plan = build_plan(
        parse("MATCH (n:Person) RETURN n.id UNION MATCH (m:Doc) RETURN m.id"),
        catalog=build_catalog(),
    )
    assert [line.split("(")[0].strip() for line in plan.root.render()[:3]] == [
        "ProduceResults",
        "DistinctRows",
        "UnionRows",
    ]
    union = next(node for node in plan.root.walk() if isinstance(node, UnionRows))
    assert len(union.children()) == 2
    assert union.children()[0] is not union.children()[1]
    assert union.columns == ("n.id",)


def test_the_admitted_pair_round_trips_through_its_description() -> None:
    statement = parse(
        "MATCH (n:Person) RETURN n.id AS left UNION MATCH (m:Doc) RETURN m.id AS right"
    )
    assert type(statement) is UnionQuery
    assert parse(statement.describe()) == statement


def test_public_names_come_from_the_left_branch_alone(database: object) -> None:
    """The right branch may spell its aliases differently; a reader never sees them."""
    found = database.execute(
        "MATCH (n:A) RETURN n.id AS left_name UNION MATCH (m:B) RETURN m.id AS other"
    )
    assert found.columns == ("left_name",)
    assert sorted(row[0] for row in found.rows) == ["x", "y"]


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "MATCH (n:A) WHERE false RETURN n.id UNION "
            "MATCH (m:B) WHERE false RETURN m.id",
            (),
        ),
        (
            "RETURN 'one' AS id UNION MATCH (m:B) WHERE false RETURN m.id",
            (("one",),),
        ),
        (
            "MATCH (n:A) RETURN n.id UNION MATCH (m:B) RETURN m.id",
            (("x",), ("y",)),
        ),
    ),
)
def test_zero_one_and_many_rows_cross_the_pair_once(
    database: object,
    text: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    """Empty branches do not invent rows and a non-empty pair preserves first occurrence."""
    assert database.execute(text).rows == expected


def test_duplicates_inside_and_across_branches_are_removed_once(
    database: object,
) -> None:
    """The global distinct sees repetitions from either source, in branch order."""
    with database.begin("write") as writer:
        writer.execute("CREATE (:A {id: 'z', n: 1})")
        writer.execute("CREATE (:B {id: 'w', n: 1})")

    found = database.execute("MATCH (n:A) RETURN n.n UNION MATCH (m:B) RETURN m.n")
    assert found.rows == ((1,), (2,), (3,))


# --- the rule the pair has to agree about -------------------------------------------------------


def test_an_int_and_a_double_widen_before_the_duplicates_are_removed(
    database: object,
) -> None:
    """One row, not two: 1 and 1.0 are one row of a widened column.

    This single case proves three things at once -- the coercion happens, it happens BEFORE the
    distinct, and the published name is the left branch's.
    """
    found = database.execute("RETURN 1 AS left UNION RETURN 1.0 AS right")
    assert found.columns == ("left",)
    assert found.rows == ((1.0,),)


@pytest.mark.parametrize(
    ("text", "left_type", "right_type"),
    (
        ("RETURN true AS a UNION RETURN 1 AS b", "BOOL", "INT64"),
        ("RETURN 'a' AS a UNION RETURN 1 AS b", "STRING", "INT64"),
        ("RETURN 1 AS a UNION RETURN 'a' AS b", "INT64", "STRING"),
    ),
)
def test_families_this_engine_does_not_read_as_one_are_refused(
    text: str, left_type: str, right_type: str
) -> None:
    """BOOL beside INT64 is the one some dialects merge; this one names both and refuses."""
    with pytest.raises(GrafxPlanError) as failure:
        build_plan(parse(text), catalog=build_catalog())
    assert left_type in str(failure.value)
    assert right_type in str(failure.value)
    assert failure.value.details["field"] == "union"


def test_a_provable_disagreement_is_refused_before_anything_runs() -> None:
    """build_plan answers it, so explain refuses it too and no row is ever read."""
    with pytest.raises(GrafxPlanError):
        build_plan(
            parse("MATCH (n:Person) RETURN n.id UNION MATCH (m:Person) RETURN m.name"),
            catalog=build_catalog(),
        )


def test_null_takes_the_other_side_and_two_nulls_are_one_row(database: object) -> None:
    """A column that is null on one side says nothing about what the column IS."""
    assert database.execute("RETURN null AS a UNION RETURN 1 AS b").rows == (
        (None,),
        (1,),
    )
    assert database.execute("RETURN null AS a UNION RETURN null AS b").rows == (
        (None,),
    )


def test_aggregates_and_labels_are_typed_by_the_engine_that_already_types_them(
    database: object,
) -> None:
    """count and label carry a type, so a pair of them is a pair the engine can judge."""
    counted = database.execute(
        "MATCH (n:A) RETURN count(n) AS c UNION MATCH (m:B) RETURN count(m) AS d"
    )
    assert counted.rows == ((2,), (1,))
    labelled = database.execute(
        "MATCH (n:A) RETURN label(n) AS l UNION MATCH (m:B) RETURN label(m) AS k"
    )
    assert sorted(row[0] for row in labelled.rows) == ["A", "B"]
    with pytest.raises(GrafxPlanError):
        database.execute(
            "MATCH (n:A) RETURN count(n) AS c UNION MATCH (m:B) RETURN label(m) AS k"
        )


def test_sum_widens_an_integer_peer_before_global_distinct(database: object) -> None:
    """SUM already returns DOUBLE, so its logical type must make the integer peer widen."""
    found = database.execute(
        "MATCH (n:A) RETURN sum(n.n) AS total UNION RETURN 3 AS other"
    )
    assert found.rows == ((3.0,),)


def test_a_logically_double_column_normalises_runtime_ints_before_distinct(
    database: object,
) -> None:
    """A mixed numeric list is DOUBLE even when the selected element happens to be int."""
    found = database.execute(
        "MATCH (n:A) RETURN [1, 2.0][n.n] AS value UNION RETURN 1.0 AS other"
    )
    assert found.rows == ((1.0,), (2.0,))


@pytest.mark.parametrize(
    ("aggregate", "expected"),
    (("sum", 1.0), ("min", 1), ("max", 1)),
)
def test_parameter_aggregate_types_are_resolved_at_the_bind(
    database: object,
    aggregate: str,
    expected: object,
) -> None:
    found = database.execute(
        f"RETURN {aggregate}($p) AS value UNION RETURN 1 AS other",
        {"p": 1},
    )
    assert found.rows == ((expected,),)


def test_parameters_behind_branch_local_with_aliases_resolve_at_one_bind(
    database: object,
) -> None:
    """Typing follows each branch alias while execution still reads its projected value."""
    found = database.execute(
        "WITH $left AS first WITH first + 1 AS x RETURN x AS value "
        "UNION WITH $right AS x RETURN x AS other",
        {"left": 1, "right": 2},
    )
    assert found.rows == ((2,),)


def test_a_literal_map_remains_typed_behind_a_with_alias(database: object) -> None:
    """Typing expands the alias while execution still reads the WITH-projected map."""
    found = database.execute(
        "WITH {x: 2} AS m RETURN m.x AS value UNION RETURN 2 AS other"
    )
    assert found.rows == ((2,),)


def test_a_parameter_map_postfix_behind_with_is_resolved_at_the_bind(
    database: object,
) -> None:
    """The one pre-stream bind sees through the alias before either branch is read."""
    found = database.execute(
        "WITH $m AS m RETURN m.x AS value UNION RETURN 2 AS other",
        {"m": {"x": 2}},
    )
    assert found.rows == ((2,),)


@pytest.mark.parametrize(
    "left",
    (
        "RETURN {v: $p + 1}.v AS value",
        "WITH $p + 1 AS computed RETURN {v: computed}.v AS value",
        "RETURN {outer: {v: $p + 1}}.outer.v AS value",
    ),
)
def test_union_types_only_the_selected_computed_map_entry_at_bind_time(
    database: object, left: str
) -> None:
    """A map selector does not require the postfix binder to execute its scalar entry."""
    found = database.execute(f"{left} UNION RETURN 2 AS other", {"p": 1})
    assert found.rows == ((2,),)


def test_a_shared_alias_dag_never_expands_into_a_rendered_bind_error(
    database: object,
) -> None:
    stages = ["WITH $p AS a0"]
    stages.extend(f"WITH a{i - 1} + a{i - 1} AS a{i}" for i in range(1, 21))
    text = " ".join((*stages, "RETURN {v: a20}.v AS value UNION RETURN 1 AS other"))

    with pytest.raises(GrafxPlanError) as failure:
        database.execute(text, {"p": []})
    assert len(str(failure.value)) < 1024


@pytest.mark.parametrize(
    "source",
    (
        "coalesce($p, 1)",
        "CASE WHEN true THEN $p ELSE 1 END",
    ),
)
def test_bound_alias_type_replaces_its_provisional_static_type(
    database: object,
    source: str,
) -> None:
    """A parameter can widen an alias whose non-parameter arm looked statically integral."""
    found = database.execute(
        f"WITH {source} AS x RETURN x AS value UNION RETURN 1 AS other",
        {"p": 2.5},
    )
    assert found.rows == ((2.5,), (1.0,))


def test_an_alias_inside_a_case_remains_typed_for_union(database: object) -> None:
    """The typing-only CASE clone is inferred independently of executable metadata ids."""
    found = database.execute(
        "WITH 2 AS x RETURN CASE WHEN true THEN x ELSE 1 END AS value "
        "UNION RETURN 2 AS other"
    )
    assert found.rows == ((2,),)


def test_typing_aliases_form_a_linear_dag_instead_of_an_exponential_tree() -> None:
    """Repeated alias operands share their immutable typing node at every WITH stage."""
    stages = ["WITH $p AS a0"]
    stages.extend(f"WITH a{i - 1} + a{i - 1} AS a{i}" for i in range(1, 25))
    statement = parse(
        " ".join((*stages, "RETURN a24 AS value UNION RETURN 1 AS other"))
    )
    plan = build_plan(statement, catalog=build_catalog())
    expression = plan.union_columns[0][1]

    depth = 0
    while isinstance(expression, BinaryOperation):
        assert expression.left is expression.right
        expression = expression.left
        depth += 1
    assert depth == 24


def test_bound_typing_visits_each_shared_alias_node_once(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bind keeps the planner's DAG sharing instead of walking both arms repeatedly."""
    stages = ["WITH $p AS a0"]
    stages.extend(f"WITH a{i - 1} + a{i - 1} AS a{i}" for i in range(1, 21))
    text = " ".join((*stages, "RETURN a20 AS value UNION RETURN 1 AS other"))
    calls = 0
    original = query_engine._infer_bound_pulse_expression_type

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(query_engine, "_infer_bound_pulse_expression_type", counted)
    assert database.execute(text, {"p": 1}).rows == ((1 << 20,), (1,))
    assert calls < 50


def test_combined_alias_depth_is_refused_by_a_typed_budget_before_the_stack() -> None:
    """Many individually shallow WITH stages cannot assemble a recursive typing bomb."""
    unary = "- " * 10
    stages = ["WITH $p AS a0"]
    stages.extend(f"WITH {unary}a{i - 1} AS a{i}" for i in range(1, 56))
    statement = parse(
        " ".join((*stages, "RETURN a55 AS value UNION RETURN 1 AS other"))
    )

    with pytest.raises(GrafxPlanError) as failure:
        build_plan(statement, catalog=build_catalog())
    assert failure.value.details["field"] == "depth"


def test_union_typing_accepts_the_same_live_expression_depth_as_an_ordinary_query() -> (
    None
):
    """UNION cannot move the inclusive depth boundary one level closer to the caller."""
    expression = "- " * (MAX_EXPRESSION_DEPTH + 1) + "1"

    build_plan(parse(f"RETURN {expression} AS value"), catalog=build_catalog())
    build_plan(
        parse(f"RETURN {expression} AS value UNION RETURN 1 AS other"),
        catalog=build_catalog(),
    )


def test_alias_expansion_accepts_the_ceiling_and_refuses_the_next_level() -> None:
    """The non-executable typing DAG uses the same inclusive forty-eight-edge limit."""

    def query(depth: int) -> str:
        stages = ["WITH 1 AS a0"]
        stages.extend(f"WITH -a{i - 1} AS a{i}" for i in range(1, depth + 1))
        return " ".join((*stages, f"RETURN a{depth} AS value UNION RETURN 1 AS other"))

    build_plan(parse(query(MAX_EXPRESSION_DEPTH)), catalog=build_catalog())
    with pytest.raises(GrafxPlanError) as failure:
        build_plan(parse(query(MAX_EXPRESSION_DEPTH + 1)), catalog=build_catalog())
    assert failure.value.details["field"] == "depth"


def test_union_types_a_literal_unwind_source_from_all_its_elements(
    database: object,
) -> None:
    found = database.execute(
        "UNWIND [1, 2] AS x RETURN x AS value UNION RETURN 1 AS other"
    )
    assert found.rows == ((1,), (2,))


def test_union_types_a_parameter_unwind_source_at_the_bind(database: object) -> None:
    found = database.execute(
        "UNWIND $items AS x RETURN x AS value UNION RETURN 1.0 AS other",
        {"items": [1, 2.0]},
    )
    assert found.rows == ((1.0,), (2.0,))


@pytest.mark.parametrize("items", ([1, "bad"], "not-a-list"))
def test_invalid_bound_unwind_types_are_refused_before_any_rows(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
    items: object,
) -> None:
    calls = 0
    original = query_engine.QueryEngine._rows

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(query_engine.QueryEngine, "_rows", counted)
    with pytest.raises(GrafxPlanError):
        database.execute(
            "UNWIND $items AS x RETURN x AS value UNION RETURN 1 AS other",
            {"items": items},
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("source", "parameters"),
    (("[]", None), ("$items", {"items": []})),
)
def test_an_empty_unwind_source_remains_fail_closed_without_type_evidence(
    database: object,
    source: str,
    parameters: dict[str, object] | None,
) -> None:
    with pytest.raises(GrafxPlanError):
        database.execute(
            f"UNWIND {source} AS x RETURN x AS value UNION RETURN 1 AS other",
            parameters,
        )


def test_an_all_null_unwind_source_has_the_null_type(database: object) -> None:
    found = database.execute(
        "UNWIND $items AS x RETURN x AS value UNION RETURN 1 AS other",
        {"items": [None, None]},
    )
    assert found.rows == ((None,), (1,))


@pytest.mark.parametrize(
    ("source", "parameters"),
    (
        ("string_split('a,b', ',')", None),
        ("string_split($text, ',')", {"text": "a,b"}),
    ),
)
def test_string_split_unwind_elements_keep_their_known_string_type(
    database: object,
    source: str,
    parameters: dict[str, object] | None,
) -> None:
    found = database.execute(
        f"UNWIND {source} AS x RETURN x AS value UNION RETURN 'a' AS other",
        parameters,
    )
    assert found.rows == (("a",), ("b",))


@pytest.mark.parametrize(
    ("source", "parameters"),
    (
        ("[{id: 1}, {id: 2}]", None),
        ("$items", {"items": [{"id": 1}, {"id": 2}]}),
    ),
)
def test_unwind_map_property_types_are_proven_across_every_element(
    database: object,
    source: str,
    parameters: dict[str, object] | None,
) -> None:
    found = database.execute(
        f"UNWIND {source} AS x RETURN x.id AS value UNION RETURN 1 AS other",
        parameters,
    )
    assert found.rows == ((1,), (2,))


def test_a_statically_typed_unwind_subscript_keeps_its_dynamic_index(
    database: object,
) -> None:
    """UNION typing does not eagerly evaluate an index whose result family is already known."""
    found = database.execute(
        "UNWIND [[10, 20]] AS x RETURN x[$i + 1] AS value UNION RETURN 20 AS other",
        {"i": 1},
    )
    assert found.rows == ((20,),)


def test_an_invariant_outer_type_does_not_materialise_its_unwind_postfix(
    database: object,
) -> None:
    """IS NULL is BOOL without asking UNION to evaluate the row's dynamic subscript."""
    found = database.execute(
        "UNWIND [[10, 20]] AS x RETURN x[$i + 1] IS NULL AS value "
        "UNION RETURN false AS other",
        {"i": 1},
    )
    assert found.rows == ((False,),)


@pytest.mark.parametrize(
    ("projection", "peer", "expected"),
    (
        (
            "CASE WHEN x[$i + 1] IS NULL THEN 1 ELSE 2 END",
            "2",
            ((2,),),
        ),
        ("[x[$i + 1]]", "[20]", (((20,),),)),
        ("{v: x[$i + 1]}", "{v: 20}", (({"v": 20},),)),
    ),
)
def test_structurally_fixed_outputs_do_not_materialise_dynamic_unwind_postfixes(
    database: object,
    projection: str,
    peer: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    found = database.execute(
        f"UNWIND [[10, 20]] AS x RETURN {projection} AS value "
        f"UNION RETURN {peer} AS other",
        {"i": 1},
    )
    assert found.rows == expected


@pytest.mark.parametrize("projection", ("[x][1]", "{v: x}.v"))
def test_postfix_over_a_container_around_unwind_is_typed_per_element(
    database: object,
    projection: str,
) -> None:
    found = database.execute(
        f"UNWIND $items AS x RETURN {projection} AS value UNION RETURN 1 AS other",
        {"items": [1, 2]},
    )
    assert found.rows == ((1,), (2,))


def test_dynamic_postfix_over_a_container_around_unwind_keeps_element_type(
    database: object,
) -> None:
    found = database.execute(
        "UNWIND $items AS x RETURN [x][$i + 1] AS value UNION RETURN 2 AS other",
        {"items": [1, 2], "i": 0},
    )
    assert found.rows == ((1,), (2,))


def test_direct_unwind_subscript_is_typed_from_every_list_element(
    database: object,
) -> None:
    found = database.execute(
        "UNWIND [[1], [2]] AS x RETURN x[1] AS value UNION RETURN 1 AS other"
    )
    assert found.rows == ((1,), (2,))


@pytest.mark.parametrize(
    ("projection", "expected"),
    (
        ("1", ((1,),)),
        ("x IS NULL", ((False,),)),
        ("size(x)", ((1,),)),
    ),
)
def test_heterogeneous_unwind_values_are_allowed_when_the_output_type_is_invariant(
    database: object,
    projection: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    items: list[object] = ["a", [1]] if projection == "size(x)" else [1, "bad"]
    peer = "false" if projection == "x IS NULL" else "1"
    found = database.execute(
        f"UNWIND $items AS x RETURN {projection} AS value UNION RETURN {peer} AS other",
        {"items": items},
    )
    assert found.rows == expected


def test_coalesce_metadata_does_not_cross_branch_schema_contexts(
    tmp_path: Path,
) -> None:
    """Equal-looking calls in two branches still belong to different AST occurrences."""
    handle = okto_grafx.connect(tmp_path / "coalesce-identity", page_size=512)
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Number(id STRING, x INT64, PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE NODE TABLE Word(id STRING, x STRING, PRIMARY KEY(id))"
            )
        with handle.begin("write") as seed:
            seed.execute("CREATE (:Number {id: 'n', x: 1})")
            seed.execute("CREATE (:Word {id: 'w', x: 'text'})")

        with pytest.raises(GrafxPlanError):
            handle.execute(
                "MATCH (n:Number) WHERE coalesce(n.x, $p) = $p RETURN 1 AS v "
                "UNION "
                "MATCH (n:Word) WHERE coalesce(n.x, $p) = $p RETURN 2 AS v",
                {"p": "fallback"},
            )
    finally:
        handle.close()


def test_a_column_with_no_type_at_all_is_refused_before_the_stream(
    database: object,
) -> None:
    """An entity has no ValueType, so no rule could ever show the two branches agree."""
    with pytest.raises(GrafxPlanError) as failure:
        database.execute("MATCH (n:A) RETURN n UNION MATCH (m:B) RETURN m")
    assert failure.value.details["field"] == "union"


@pytest.mark.parametrize(
    "projection",
    (
        "[$v]",
        "{x: $v}",
        "collect($v)",
        "[$v, 1][1]",
        "{bad: $v}.bad",
        "{outer: {bad: $v}}.outer.bad",
    ),
)
def test_an_entity_cannot_hide_inside_a_union_value(
    database: object, projection: str
) -> None:
    """Private identity cannot drive DISTINCT and then disappear during detachment."""
    left = projection.replace("$v", "n")
    right = projection.replace("$v", "m")

    with pytest.raises(GrafxPlanError) as failure:
        database.execute(
            f"MATCH (n:A) RETURN {left} AS value "
            f"UNION MATCH (m:B) RETURN {right} AS other"
        )
    assert failure.value.details["field"] == "union"


@pytest.mark.parametrize(
    ("projection", "expected"),
    (
        ("[$v, 1][2]", ((1,),)),
        ("{bad: $v, safe: $v.id}.safe", (("x",), ("y",))),
        ("{outer: {bad: $v, safe: $v.id}}.outer.safe", (("x",), ("y",))),
        ("[[$v, $v.id]][1][2]", (("x",), ("y",))),
        ("{outer: [$v, $v.id]}.outer[2]", (("x",), ("y",))),
        ("size([$v])", ((1,),)),
    ),
)
def test_a_scalar_derived_from_an_entity_remains_a_valid_union_value(
    database: object,
    projection: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    left = projection.replace("$v", "n")
    right = projection.replace("$v", "m")

    found = database.execute(
        f"MATCH (n:A) RETURN {left} AS value UNION MATCH (m:B) RETURN {right} AS other"
    )
    assert found.rows == expected


# --- parameters ---------------------------------------------------------------------------------


def test_a_parameter_is_judged_at_the_bind_and_before_any_row_is_read(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The type nobody could prove at planning is proven once, by the call that supplies it."""
    widened = database.execute("RETURN 1 AS a UNION RETURN $p AS b", {"p": 2.5})
    assert widened.rows == ((1.0,), (2.5,))

    scans = 0
    original_scan = HeapStore.scan

    def recording_scan(self: HeapStore, *args: object, **kwargs: object) -> object:
        nonlocal scans
        scans += 1
        return original_scan(self, *args, **kwargs)

    monkeypatch.setattr(HeapStore, "scan", recording_scan)
    with pytest.raises(GrafxPlanError):
        database.execute(
            "MATCH (n:A) RETURN n.n AS a UNION RETURN $p AS b",
            {"p": "text"},
        )
    assert scans == 0


def test_a_parameter_the_call_did_not_supply_is_refused(database: object) -> None:
    """One call binds the pair, so a name either branch reads must arrive with it."""
    with pytest.raises(GrafxPlanError):
        database.execute("RETURN 1 AS a UNION RETURN $p AS b", {})


@pytest.mark.parametrize(("per_branch", "accepted"), ((128, True), (129, False)))
def test_the_parameter_limit_applies_to_the_combined_statement(
    per_branch: int,
    accepted: bool,
) -> None:
    """Two individually bounded branches cannot double the one-statement bind ceiling."""
    left = ", ".join(f"$left_{index} AS c{index}" for index in range(per_branch))
    right = ", ".join(f"$right_{index} AS d{index}" for index in range(per_branch))
    statement = parse(f"RETURN {left} UNION RETURN {right}")

    if accepted:
        assert len(analyze(statement).parameters) == 256
        return
    with pytest.raises(GrafxPlanError):
        analyze(statement)


# --- the words that only look like the keyword ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        pytest.param("RETURN 'UNION' AS a", ("UNION",), id="single-quoted"),
        pytest.param('RETURN "UNION" AS a', ("UNION",), id="double-quoted"),
        pytest.param("RETURN 1 AS a // UNION", (1,), id="line-comment"),
    ),
)
def test_the_word_inside_text_is_not_the_keyword(
    database: object, text: str, expected: tuple[object, ...]
) -> None:
    """A textual splitter would cut these in half; the parser reads tokens, so it does not."""
    assert database.execute(text).rows == (expected,)


def test_a_quoted_name_spelled_like_the_keyword_stays_a_name(database: object) -> None:
    """Back quotes make a name out of anything, including a word the grammar uses."""
    found = database.execute("MATCH (n:A) RETURN n.id AS `UNION` ORDER BY n.id")
    assert found.columns == ("UNION",)
    assert found.rows == (("x",), ("y",))


# --- the shapes that are not a union -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        "MATCH (n:A) RETURN n.id UNION ALL MATCH (m:B) RETURN m.id",
        "MATCH (a:A) RETURN a.id UNION MATCH (b:B) RETURN b.id UNION MATCH (c:A) RETURN c.id",
        "MATCH (n:A) RETURN n.id UNION",
        "UNION MATCH (n:A) RETURN n.id",
        "MATCH (n:A) RETURN n.id UNION CREATE (:B {id: 'z'})",
        "MATCH (n:A) RETURN n.id UNION MATCH (m:B) SET m.n = 1 RETURN m.id",
    ),
)
def test_the_parser_refuses_every_shape_that_is_not_the_admitted_pair(
    text: str,
) -> None:
    """Each of these is refused where the text is read, with a message that names the word."""
    with pytest.raises(GrafxParseError):
        parse(text)


def test_branches_may_reuse_one_name_for_different_tables(database: object) -> None:
    """Nothing crosses the boundary, so the two n's are not the same n."""
    found = database.execute("MATCH (n:A) RETURN n.id UNION MATCH (n:B) RETURN n.id")
    assert sorted(row[0] for row in found.rows) == ["x", "y"]


def test_each_branch_keeps_its_own_window(database: object) -> None:
    """A LIMIT belongs to the branch that wrote it; the pair adds no ordering of its own."""
    found = database.execute(
        "MATCH (n:A) RETURN n.id ORDER BY n.id LIMIT 1 UNION MATCH (m:B) RETURN m.id"
    )
    assert found.rows == (("x",),)


# --- a tree nobody parsed --------------------------------------------------------------------------


def _branch(table: str, variable: str) -> Query:
    """Return a minimal reading branch over one table."""
    return Query(
        match_clauses=(
            MatchClause(
                patterns=(
                    PatternPath(
                        nodes=(NodePattern(variable=variable, labels=(table,)),)
                    ),
                ),
            ),
        ),
        return_clause=ReturnClause(
            items=(
                ReturnItem(
                    expression=Property(subject=Variable(name=variable), key="id")
                ),
            )
        ),
    )


FORGED: dict[str, UnionQuery] = {
    "a branch that is not a query": UnionQuery(
        left=_branch("Person", "n"),
        right="MATCH (m:Doc) RETURN m.id",  # type: ignore[arg-type]
    ),
    "a branch with no RETURN": UnionQuery(
        left=_branch("Person", "n"),
        right=Query(
            match_clauses=(
                MatchClause(
                    patterns=(
                        PatternPath(
                            nodes=(NodePattern(variable="m", labels=("Doc",)),)
                        ),
                    ),
                ),
            )
        ),
    ),
    "branches of different arity": UnionQuery(
        left=Query(
            match_clauses=_branch("Person", "n").match_clauses,
            return_clause=ReturnClause(
                items=(
                    ReturnItem(
                        expression=Property(subject=Variable(name="n"), key="id")
                    ),
                    ReturnItem(
                        expression=Property(subject=Variable(name="n"), key="name")
                    ),
                )
            ),
        ),
        right=_branch("Doc", "m"),
    ),
    "a branch that writes": UnionQuery(
        left=_branch("Person", "n"),
        right=parse("CREATE (:Doc {id: 'x'}) RETURN 1"),  # type: ignore[arg-type]
    ),
    "a nested union": UnionQuery(
        left=_branch("Person", "n"),
        right=UnionQuery(
            left=_branch("Person", "p"),
            right=_branch("Doc", "m"),
        ),  # type: ignore[arg-type]
    ),
    "an optional branch": UnionQuery(
        left=parse("OPTIONAL MATCH (p:Person) RETURN p.id"),  # type: ignore[arg-type]
        right=_branch("Doc", "m"),
    ),
}


@pytest.mark.parametrize("name", sorted(FORGED))
@pytest.mark.parametrize("door", ("analysis", "planner"))
def test_a_forged_union_is_refused_at_both_doors(name: str, door: str) -> None:
    """analyze sees a hand-built tree and build_plan sees a caller's own analysis."""
    statement = FORGED[name]
    if door == "analysis":
        with pytest.raises(GrafxPlanError):
            analyze(statement)
        return
    supplied = QueryAnalysis(statement=statement)
    with pytest.raises(GrafxPlanError):
        build_plan(statement, catalog=build_catalog(), analysis=supplied)


def test_supplied_incomplete_analysis_cannot_hide_either_branch_parameters() -> None:
    """The optional analysis argument is a cache, not authority over the statement."""
    statement = parse("RETURN $left AS a UNION RETURN $right AS b")
    assert type(statement) is UnionQuery

    planned = build_plan(
        statement,
        catalog=build_catalog(),
        analysis=QueryAnalysis(statement=statement),
    )

    assert planned.analysis.parameters == ("left", "right")
    assert planned.analysis.output_columns == ("a",)


# --- budgets ---------------------------------------------------------------------------------------


def test_the_intermediate_budget_counts_the_rows_before_the_duplicates_go(
    tmp_path: Path,
) -> None:
    """Two rows reach the union operator even though one row leaves the query."""
    handle = okto_grafx.connect(
        tmp_path / "narrow", page_size=512, max_intermediate_rows=1
    )
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as exceeded:
            handle.execute("RETURN 1 AS left UNION RETURN 1.0 AS right")
        assert "UnionRows" in str(exceeded.value)
    finally:
        handle.close()


def test_the_result_budget_counts_what_the_caller_receives(tmp_path: Path) -> None:
    """One row survives the distinct, so a limit of one is enough for the pair."""
    handle = okto_grafx.connect(tmp_path / "one", page_size=512, max_result_rows=1)
    try:
        assert handle.execute("RETURN 1 AS left UNION RETURN 1.0 AS right").rows == (
            (1.0,),
        )
    finally:
        handle.close()


# --- one execution -----------------------------------------------------------------------------------


def test_the_pair_writes_nothing_and_reports_no_statistics(database: object) -> None:
    """A union only reads, so there is nothing for it to count."""
    found = database.execute("RETURN 1 AS a UNION RETURN 2 AS b")
    assert dict(found.statistics) == {}


def test_a_reader_inside_a_write_transaction_sees_its_own_rows(
    database: object,
) -> None:
    """The pair runs in the transaction that asked, so RYOW holds across both branches."""
    with database.begin("write") as txn:
        txn.execute("CREATE (:B {id: 'z', n: 9})")
        found = txn.execute("MATCH (n:A) RETURN n.id UNION MATCH (m:B) RETURN m.id")
        assert sorted(row[0] for row in found.rows) == ["x", "y", "z"]


def test_an_outsider_keeps_one_old_snapshot_for_both_branches(database: object) -> None:
    """A commit between reads cannot make only one half of the pair see a newer graph."""
    outsider = database.begin("read")
    with database.begin("write") as writer:
        writer.execute("CREATE (:A {id: 'z', n: 9})")

    text = "MATCH (n:A) RETURN n.id UNION MATCH (m:B) RETURN m.id"
    assert outsider.execute(text).rows == (("x",), ("y",))
    outsider.commit()
    assert database.execute(text).rows == (("x",), ("y",), ("z",))


def test_a_failure_in_the_right_branch_returns_no_partial_result(
    database: object,
) -> None:
    """Rows already pulled from the left never escape when the second pipeline refuses."""
    reader = database.begin("read")
    with pytest.raises(GrafxPlanError):
        reader.execute("RETURN 1 AS value UNION RETURN 1 / 0 AS other")
    assert reader.active
    assert reader.execute("RETURN 7 AS value").rows == ((7,),)
    reader.rollback()


def test_a_rolled_back_write_leaves_the_pair_as_it_was(database: object) -> None:
    """What the transaction undid is not in the result the next reader gets."""
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE (:B {id: 'z', n: 9})")
            raise RuntimeError("undo this")
    except RuntimeError:
        pass
    found = database.execute("MATCH (n:A) RETURN n.id UNION MATCH (m:B) RETURN m.id")
    assert sorted(row[0] for row in found.rows) == ["x", "y"]
