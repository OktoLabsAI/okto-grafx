"""Finite acceptance gate for typed, directed one-hop path projection.

The capability is deliberately bounded. It is not a general path value implementation:
``MATCH path = (a:Label)-[r:Type]->(b:Label) RETURN path`` admits caller-defined identifiers.
It may carry only a terminal literal non-negative ``LIMIT``. These tests freeze both halves of
that statement: the narrow parser/analyser/planner gate and the Kuzu/Ladybug-compatible public
value produced for each matching edge.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.analysis import (
    ENTITY_PATH,
    Aggregation,
    Binding,
    QueryAnalysis,
    analyze,
    exact_path_projection,
)
from okto_grafx.domain.query.ast import (
    Direction,
    FunctionCall,
    Literal,
    MatchClause,
    NodePattern,
    PatternPath,
    Query,
    RelationshipPattern,
    ReturnClause,
    ReturnItem,
    Variable,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.plan import (
    AggregateRows,
    LimitRows,
    NodeScan,
    ProduceResults,
    ProjectRows,
    RelationshipScan,
    SingleRow,
    TraverseRelationship,
)
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.engine.query_engine import QueryEngine

ADMITTED = "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path"


def _catalog() -> Catalog:
    """Return exactly the endpoint pair and property order the frozen path needs."""
    catalog = Catalog()
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Decision",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.STRING, nullable=False),
                ColumnDef(name="title", type=ValueType.STRING),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="supersedes",
            kind="rel",
            columns=(
                ColumnDef(name="layer", type=ValueType.STRING),
                ColumnDef(name="note", type=ValueType.STRING),
            ),
            from_table="Decision",
            to_table="Decision",
        )
    )
    return catalog


def _catalog_with_path_reserved_property(
    table_name: str, property_name: str
) -> Catalog:
    """Return the frozen schema with one otherwise-legal property that shadows path metadata."""
    node_columns = [
        ColumnDef(name="id", type=ValueType.STRING, nullable=False),
    ]
    relationship_columns = [ColumnDef(name="layer", type=ValueType.STRING)]
    destination = node_columns if table_name == "Decision" else relationship_columns
    destination.append(ColumnDef(name=property_name, type=ValueType.STRING))

    catalog = Catalog()
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Decision",
            kind="node",
            columns=tuple(node_columns),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="supersedes",
            kind="rel",
            columns=tuple(relationship_columns),
            from_table="Decision",
            to_table="Decision",
        )
    )
    return catalog


def _install_schema(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision(id STRING, title STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE REL TABLE supersedes("
            "FROM Decision TO Decision, layer STRING, note STRING)"
        )


def _seed_canonical(database: object) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Decision {id: 'd1', title: 'source'})")
        seed.execute("CREATE (:Decision {id: 'd2', title: 'target'})")
        seed.execute(
            "MATCH (a:Decision {id: 'd1'}), (b:Decision {id: 'd2'}) "
            "CREATE (a)-[:supersedes "
            "{layer: 'canonical', note: 'primary'}]->(b)"
        )


def _open_database(root: Path, *, seeded: bool = True, **options: object) -> object:
    database = okto_grafx.connect(root, page_size=512, **options)
    _install_schema(database)
    if seeded:
        _seed_canonical(database)
    return database


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = _open_database(tmp_path / "db")
    try:
        yield handle
    finally:
        handle.close()


def _identity(value: object) -> dict[str, int]:
    """Check an opaque Kuzu-shaped identity without pinning physical numbers."""
    assert type(value) is dict
    assert tuple(value) == ("offset", "table")
    assert type(value["offset"]) is int
    assert type(value["table"]) is int
    return value


def _assert_path(
    value: object,
    *,
    source: tuple[str, str],
    target: tuple[str, str],
    relationship: tuple[str, str],
) -> dict[str, object]:
    """Assert the exact public shape, order and endpoint correlations of one path."""
    assert type(value) is dict
    assert tuple(value) == ("_NODES", "_RELS")
    nodes = value["_NODES"]
    relationships = value["_RELS"]
    assert type(nodes) is tuple
    assert type(relationships) is tuple
    assert len(nodes) == 2
    assert len(relationships) == 1

    source_node, target_node = nodes
    assert type(source_node) is dict
    assert type(target_node) is dict
    assert tuple(source_node) == ("_ID", "_LABEL", "id", "title")
    assert tuple(target_node) == ("_ID", "_LABEL", "id", "title")
    source_identity = _identity(source_node["_ID"])
    target_identity = _identity(target_node["_ID"])
    assert source_node["_LABEL"] == "Decision"
    assert target_node["_LABEL"] == "Decision"
    assert (source_node["id"], source_node["title"]) == source
    assert (target_node["id"], target_node["title"]) == target

    edge = relationships[0]
    assert type(edge) is dict
    assert tuple(edge) == (
        "_SRC",
        "_DST",
        "_LABEL",
        "_ID",
        "layer",
        "note",
    )
    assert _identity(edge["_SRC"]) == source_identity
    assert _identity(edge["_DST"]) == target_identity
    assert edge["_LABEL"] == "supersedes"
    _identity(edge["_ID"])
    assert (edge["layer"], edge["note"]) == relationship
    return value


def _paths(owner: object) -> tuple[dict[str, object], ...]:
    result = owner.execute(ADMITTED)
    assert result.columns == ("path",)
    assert all(type(row) is tuple and len(row) == 1 for row in result.rows)
    return tuple(row[0] for row in result.rows)


def _by_layer(paths: tuple[dict[str, object], ...]) -> dict[str, dict[str, object]]:
    return {path["_RELS"][0]["layer"]: path for path in paths}


# --- exact syntax, analysis and plan -------------------------------------------------------------


def test_exact_statement_round_trips_analyses_and_plans_one_path_binding() -> None:
    statement = parse(ADMITTED)
    pattern = exact_path_projection(statement)
    assert pattern is statement.match_clauses[0].patterns[0]
    assert statement.describe() == ADMITTED

    analysis = analyze(statement)
    assert analysis.bindings == (
        Binding(name="path", entity=ENTITY_PATH, labels=(), created=False),
        Binding(name="a", entity="node", labels=("Decision",), created=False),
        Binding(name="b", entity="node", labels=("Decision",), created=False),
        Binding(
            name="r",
            entity="relationship",
            labels=("supersedes",),
            created=False,
        ),
    )
    assert analysis.parameters == ()
    assert analysis.output_columns == ("path",)
    assert analysis.aggregations == ()

    planned = build_plan(statement, catalog=_catalog())
    assert [type(node) for node in planned.root.walk()] == [
        ProduceResults,
        ProjectRows,
        TraverseRelationship,
        NodeScan,
        SingleRow,
    ]
    traversal = next(
        node for node in planned.root.walk() if type(node) is TraverseRelationship
    )
    assert traversal.source == "a"
    assert traversal.target == "b"
    assert traversal.relationship == "r"
    assert traversal.table.name == "supersedes"
    assert traversal.direction is Direction.OUTGOING
    assert (traversal.min_hops, traversal.max_hops) == (1, 1)
    assert traversal.path_variable == "path"
    assert dict(traversal.details())["path"] == "path"


@pytest.mark.parametrize(
    "text",
    (
        "match path = (a:Decision)-[r:supersedes]->(b:Decision) return path;",
        "MATCH path=(a:Decision)-[r:supersedes]->(b:Decision)\nRETURN path",
    ),
)
def test_lexical_trivia_does_not_change_the_exact_ast_gate(text: str) -> None:
    statement = parse(text)
    assert statement.describe() == ADMITTED
    assert exact_path_projection(statement) is not None


def test_one_edge_returns_one_correlated_kuzu_shaped_path(database: object) -> None:
    paths = _paths(database)
    assert len(paths) == 1
    _assert_path(
        paths[0],
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )


def test_native_path_functions_return_owned_components(database: object) -> None:
    prefix = "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN "
    path = database.execute(prefix + "path").rows[0][0]
    assert database.execute(prefix + "length(path), nodes(path), relationships(path)").rows == (
        (1, path["_NODES"], path["_RELS"]),
    )
    assert database.execute(prefix + "[n IN nodes(path) | n.id]").rows == ((("d1", "d2"),),)
    assert database.execute(prefix + "head(relationships(path)).note").rows == (("primary",),)
    with database.query(prefix + "length(path) AS length").cursor(batch_size=1) as cursor:
        assert tuple(cursor) == ((1,),)


@pytest.mark.parametrize("name", ("length", "nodes", "relationships"))
def test_path_functions_refuse_forged_values_even_before_empty_stream(database: object, name: str) -> None:
    assert database.execute(f"RETURN {name}(NULL)").rows == ((None,),)
    with pytest.raises(GrafxPlanError):
        database.execute(f"UNWIND [] AS x RETURN {name}($fake)", {"fake": {"_NODES": [], "_RELS": []}})


def test_projected_path_refuses_a_catalog_with_the_wrong_target_endpoint() -> None:
    catalog = Catalog()
    for table_id, name in ((1, "Decision"), (2, "Bug")):
        catalog.add_table(
            TableDef(
                table_id=table_id,
                name=name,
                kind="node",
                columns=(ColumnDef(name="id", type=ValueType.STRING, nullable=False),),
                primary_key="id",
            )
        )
    catalog.add_table(
        TableDef(
            table_id=3,
            name="supersedes",
            kind="rel",
            columns=(ColumnDef(name="layer", type=ValueType.STRING),),
            from_table="Decision",
            to_table="Bug",
        )
    )

    with pytest.raises(GrafxPlanError) as raised:
        build_plan(parse(ADMITTED), catalog=catalog)
    assert raised.value.details == {
        "field": "endpoint",
        "value": "supersedes",
        "from_table": "Decision",
        "to_table": "Bug",
    }


@pytest.mark.parametrize(
    ("table_name", "property_name"),
    (
        ("Decision", "_ID"),
        ("Decision", "_LABEL"),
        ("supersedes", "_SRC"),
        ("supersedes", "_DST"),
        ("supersedes", "_LABEL"),
        ("supersedes", "_ID"),
    ),
)
def test_projected_path_refuses_properties_that_shadow_structural_keys_before_stream(
    table_name: str, property_name: str
) -> None:
    catalog = _catalog_with_path_reserved_property(table_name, property_name)

    with pytest.raises(GrafxPlanError) as raised:
        build_plan(parse(ADMITTED), catalog=catalog)

    assert raised.value.details == {
        "field": "column",
        "value": property_name,
        "table": table_name,
    }
    assert property_name in str(raised.value)


def test_physical_relationship_endpoints_do_not_shadow_path_keys() -> None:
    catalog = _catalog()
    relationship = catalog.table("supersedes")

    assert tuple(column.name for column in relationship.endpoint_columns) == (
        "_from",
        "_to",
    )
    planned = build_plan(parse(ADMITTED), catalog=catalog)
    assert any(
        type(node) is TraverseRelationship and node.path_variable == "path"
        for node in planned.root.walk()
    )


def test_public_execute_reports_a_reserved_path_property_as_a_plan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = okto_grafx.connect(tmp_path / "reserved-path-property", page_size=512)
    try:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Decision(id STRING, _ID STRING, PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE REL TABLE supersedes(FROM Decision TO Decision, reason STRING)"
            )
        with database.begin("write") as seed:
            seed.execute("CREATE (:Decision {id: 'd1', _ID: 'user-one'})")
            seed.execute("CREATE (:Decision {id: 'd2', _ID: 'user-two'})")
            seed.execute(
                "MATCH (a:Decision {id: 'd1'}), (b:Decision {id: 'd2'}) "
                "CREATE (a)-[:supersedes {reason: 'newer'}]->(b)"
            )

        def fail_if_run(*_args: object, **_kwargs: object) -> object:
            pytest.fail("query execution was reached after the planning refusal")

        monkeypatch.setattr(QueryEngine, "_run", fail_if_run)
        with pytest.raises(GrafxPlanError) as raised:
            database.execute(ADMITTED)
        assert raised.value.details == {
            "field": "column",
            "value": "_ID",
            "table": "Decision",
        }
    finally:
        database.close()


def test_no_edge_is_an_empty_result_not_a_null_path(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "empty", seeded=False)
    try:
        with database.begin("write") as seed:
            seed.execute("CREATE (:Decision {id: 'd1', title: 'one'})")
            seed.execute("CREATE (:Decision {id: 'd2', title: 'two'})")
        result = database.execute(ADMITTED)
        assert result.columns == ("path",)
        assert result.rows == ()
    finally:
        database.close()


def test_parallel_edges_are_distinct_paths_with_shared_endpoint_identities(
    database: object,
) -> None:
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (a:Decision {id: 'd1'}), (b:Decision {id: 'd2'}) "
            "CREATE (a)-[:supersedes "
            "{layer: 'working', note: 'parallel'}]->(b)"
        )

    paths = _by_layer(_paths(database))
    assert tuple(sorted(paths)) == ("canonical", "working")
    canonical = _assert_path(
        paths["canonical"],
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )
    working = _assert_path(
        paths["working"],
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("working", "parallel"),
    )
    assert canonical["_NODES"][0]["_ID"] == working["_NODES"][0]["_ID"]
    assert canonical["_NODES"][1]["_ID"] == working["_NODES"][1]["_ID"]
    assert canonical["_RELS"][0]["_ID"] != working["_RELS"][0]["_ID"]


def test_direction_and_self_loop_correlations_follow_the_stored_edge(
    database: object,
) -> None:
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (a:Decision {id: 'd2'}), (b:Decision {id: 'd1'}) "
            "CREATE (a)-[:supersedes {layer: 'reverse', note: 'direction'}]->(b)"
        )
        writer.execute(
            "MATCH (a:Decision {id: 'd1'}) "
            "CREATE (a)-[:supersedes {layer: 'loop', note: 'self'}]->(a)"
        )

    paths = _by_layer(_paths(database))
    reverse = _assert_path(
        paths["reverse"],
        source=("d2", "target"),
        target=("d1", "source"),
        relationship=("reverse", "direction"),
    )
    loop = _assert_path(
        paths["loop"],
        source=("d1", "source"),
        target=("d1", "source"),
        relationship=("loop", "self"),
    )
    assert reverse["_RELS"][0]["_SRC"] == reverse["_NODES"][0]["_ID"]
    assert reverse["_RELS"][0]["_DST"] == reverse["_NODES"][1]["_ID"]
    assert loop["_NODES"][0]["_ID"] == loop["_NODES"][1]["_ID"]
    assert loop["_RELS"][0]["_SRC"] == loop["_RELS"][0]["_DST"]


# --- transactional visibility ------------------------------------------------------------------


def test_owner_reads_pending_path_outsider_does_not_and_rollback_removes_it(
    database: object,
) -> None:
    writer = database.begin("write")
    try:
        writer.execute("CREATE (:Decision {id: 'd3', title: 'pending source'})")
        writer.execute("CREATE (:Decision {id: 'd4', title: 'pending target'})")
        writer.execute(
            "MATCH (a:Decision {id: 'd3'}), (b:Decision {id: 'd4'}) "
            "CREATE (a)-[:supersedes {layer: 'staged', note: 'rollback'}]->(b)"
        )

        owned = _by_layer(_paths(writer))
        assert set(owned) == {"canonical", "staged"}
        _assert_path(
            owned["staged"],
            source=("d3", "pending source"),
            target=("d4", "pending target"),
            relationship=("staged", "rollback"),
        )
        assert set(_by_layer(_paths(database))) == {"canonical"}
    finally:
        writer.rollback()

    assert set(_by_layer(_paths(database))) == {"canonical"}


def test_reader_snapshot_does_not_move_when_a_pending_path_commits(
    database: object,
) -> None:
    old_reader = database.begin("read")
    try:
        assert set(_by_layer(_paths(old_reader))) == {"canonical"}
        writer = database.begin("write")
        writer.execute("CREATE (:Decision {id: 'd3', title: 'later source'})")
        writer.execute("CREATE (:Decision {id: 'd4', title: 'later target'})")
        writer.execute(
            "MATCH (a:Decision {id: 'd3'}), (b:Decision {id: 'd4'}) "
            "CREATE (a)-[:supersedes {layer: 'later', note: 'committed'}]->(b)"
        )
        assert set(_by_layer(_paths(writer))) == {"canonical", "later"}
        writer.commit()

        assert set(_by_layer(_paths(old_reader))) == {"canonical"}
        assert set(_by_layer(_paths(database))) == {"canonical", "later"}
    finally:
        old_reader.commit()


def test_relationship_update_is_owner_visible_and_rollback_restores_path(
    database: object,
) -> None:
    writer = database.begin("write")
    try:
        writer.execute(
            "MATCH (a:Decision)-[r:supersedes]->(b:Decision) "
            "SET r.layer = 'staged', r.note = 'owner update'"
        )
        staged = _paths(writer)
        assert len(staged) == 1
        _assert_path(
            staged[0],
            source=("d1", "source"),
            target=("d2", "target"),
            relationship=("staged", "owner update"),
        )
        assert set(_by_layer(_paths(database))) == {"canonical"}
    finally:
        writer.rollback()

    canonical = _paths(database)
    assert len(canonical) == 1
    _assert_path(
        canonical[0],
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )


def test_relationship_delete_is_owner_visible_and_rollback_restores_path(
    database: object,
) -> None:
    writer = database.begin("write")
    try:
        deleted = writer.execute(
            "MATCH (a:Decision)-[r:supersedes]->(b:Decision) DELETE r"
        )
        assert deleted.statistics["rows_deleted"] == 1
        assert _paths(writer) == ()
        assert len(_paths(database)) == 1
    finally:
        writer.rollback()

    assert len(_paths(database)) == 1


def test_old_snapshot_keeps_path_after_committed_edge_delete(database: object) -> None:
    old_reader = database.begin("read")
    try:
        assert len(_paths(old_reader)) == 1
        with database.begin("write") as writer:
            writer.execute("MATCH (a:Decision)-[r:supersedes]->(b:Decision) DELETE r")
        assert len(_paths(old_reader)) == 1
        assert _paths(database) == ()
    finally:
        old_reader.commit()


@pytest.mark.parametrize(
    ("identity", "delete"),
    (
        ("d1", "DETACH DELETE p"),
        ("d2", "DETACH DELETE p"),
    ),
)
def test_deleted_endpoint_removes_path_from_a_new_snapshot(
    database: object, identity: str, delete: str
) -> None:
    with database.begin("write") as writer:
        writer.execute(f"MATCH (p:Decision) WHERE p.id = '{identity}' {delete}")
    assert _paths(database) == ()


# --- budgets and public detachment ---------------------------------------------------------------


def _seed_self_loops(root: Path, count: int) -> None:
    database = _open_database(root, seeded=False)
    try:
        with database.begin("write") as writer:
            writer.execute("CREATE (:Decision {id: 'd1', title: 'loop'})")
            for position in range(count):
                writer.execute(
                    "MATCH (a:Decision {id: 'd1'}) "
                    "CREATE (a)-[:supersedes "
                    f"{{layer: 'l{position}', note: 'n{position}'}}]->(a)"
                )
    finally:
        database.close()


def test_one_path_fits_one_intermediate_and_one_result_row(tmp_path: Path) -> None:
    root = tmp_path / "exact-budget"
    _seed_self_loops(root, 1)
    database = okto_grafx.connect(
        root,
        page_size=512,
        max_intermediate_rows=1,
        max_result_rows=1,
    )
    try:
        assert len(_paths(database)) == 1
    finally:
        database.close()


def test_parallel_paths_trip_the_traversal_budget_at_limit_plus_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "intermediate-budget"
    _seed_self_loops(root, 2)
    narrow = okto_grafx.connect(root, page_size=512, max_intermediate_rows=1)
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            narrow.execute(ADMITTED)
        assert raised.value.details == {
            "field": "max_intermediate_rows",
            "limit": 1,
            "observed": 2,
            "operator": "TraverseRelationship",
        }
    finally:
        narrow.close()

    exact = okto_grafx.connect(root, page_size=512, max_intermediate_rows=2)
    try:
        assert len(_paths(exact)) == 2
    finally:
        exact.close()


def test_parallel_paths_trip_only_the_result_budget_when_work_fits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "result-budget"
    _seed_self_loops(root, 2)
    database = okto_grafx.connect(
        root,
        page_size=512,
        max_intermediate_rows=2,
        max_result_rows=1,
    )
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            database.execute(ADMITTED)
        assert raised.value.details == {
            "field": "max_result_rows",
            "limit": 1,
            "observed": 2,
        }
    finally:
        database.close()


def test_public_path_is_json_serialisable_deeply_owned_and_capability_free(
    database: object,
) -> None:
    first = _paths(database)[0]
    _assert_path(
        first,
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )
    assert json.loads(json.dumps(first))["_RELS"][0]["layer"] == "canonical"
    assert "_Path" not in repr(first)
    assert "RowBinding" not in repr(first)
    committed_identities = (
        dict(first["_NODES"][0]["_ID"]),
        dict(first["_NODES"][1]["_ID"]),
        dict(first["_RELS"][0]["_ID"]),
    )

    first["_NODES"][0]["id"] = "poisoned"
    first["_NODES"][0]["_ID"]["offset"] = 999
    first["_RELS"][0]["layer"] = "poisoned"

    second = _paths(database)[0]
    _assert_path(
        second,
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )
    assert second is not first
    assert second["_NODES"][0] is not first["_NODES"][0]
    assert second["_NODES"][0]["_ID"] is not first["_NODES"][0]["_ID"]
    assert second["_RELS"][0] is not first["_RELS"][0]
    assert (
        second["_NODES"][0]["_ID"],
        second["_NODES"][1]["_ID"],
        second["_RELS"][0]["_ID"],
    ) == committed_identities


def test_public_plan_is_owned_and_keeps_only_the_explicit_path_marker(
    database: object,
) -> None:
    first = database.explain(ADMITTED)
    second = database.explain(ADMITTED)
    assert first is not second
    traversal = next(
        node for node in first.walk() if type(node) is TraverseRelationship
    )
    assert traversal.path_variable == "path"
    assert traversal.details()["path"] == "path"
    assert "_Path" not in repr(first)


# --- supplied summaries, composition and the negative exact gate --------------------------------


def test_supplied_analysis_cannot_add_aggregation_columns_or_parameters() -> None:
    statement = parse(ADMITTED)
    forged = QueryAnalysis(
        statement=statement,
        bindings=(Binding("ghost", "node", ("Ghost",), False),),
        parameters=("ghost",),
        output_columns=("wrong",),
        aggregations=(
            Aggregation(
                position=0,
                call=FunctionCall(name="count", arguments=(), star=True),
            ),
        ),
    )
    planned = build_plan(statement, catalog=_catalog(), analysis=forged)
    assert not any(type(node) is AggregateRows for node in planned.root.walk())
    assert planned.columns == ("path",)
    assert tuple(binding.name for binding in planned.analysis.bindings) == (
        "path",
        "a",
        "b",
        "r",
    )
    assert planned.analysis.parameters == ()
    assert planned.analysis.output_columns == ("path",)
    assert planned.analysis.aggregations == ()


class _HostileAnalysis(QueryAnalysis):
    @property
    def aggregated(self) -> bool:
        raise AssertionError("a supplied analysis was trusted")


def test_planner_recomputes_before_touching_a_hostile_supplied_analysis() -> None:
    statement = parse(ADMITTED)
    supplied = _HostileAnalysis(statement=statement)
    planned = build_plan(
        statement,
        catalog=_catalog(),
        analysis=supplied,
    )
    assert planned.analysis is not supplied
    assert planned.analysis.output_columns == ("path",)
    assert any(
        type(node) is TraverseRelationship and node.path_variable == "path"
        for node in planned.root.walk()
    )


@pytest.mark.parametrize(
    "text",
    (
        f"{ADMITTED} UNION MATCH (n:Decision) RETURN n.id",
        f"MATCH (n:Decision) RETURN n.id UNION {ADMITTED}",
    ),
)
def test_path_projection_is_refused_in_either_union_branch_even_with_analysis(
    text: str,
) -> None:
    statement = parse(text)
    with pytest.raises(GrafxPlanError) as analysed:
        analyze(statement)
    assert analysed.value.details["field"] == "union"

    with pytest.raises(GrafxPlanError) as planned:
        build_plan(
            statement,
            catalog=_catalog(),
            analysis=QueryAnalysis(statement=statement),
        )
    assert planned.value.details["field"] == "union"


NEAR_MISSES = (
    "MATCH path = (a)-[r:supersedes]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision:Bug)-[r:supersedes]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision:Bug) RETURN path",
    "MATCH path = (a:Decision {id: 'd1'})-[r:supersedes]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes {layer: 'canonical'}]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes|supports]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)<-[r:supersedes]-(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]-(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes*1..1]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes*1..2]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision), (c:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) MATCH (c:Decision) RETURN path",
    "UNWIND $rows AS x MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) WHERE a.id = 'd1' RETURN path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN DISTINCT path",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path ORDER BY a.id",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path SKIP 0",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) SET a.title = 'changed'",
)

# A row bound written as a literal non-negative integer is part of the admitted
# shape. It is the only thing about this projection a caller may vary, and
# refusing it made the projection unreachable for any client that appends LIMIT
# to every read.
ADMITTED_ROW_BOUNDS = (
    f"{ADMITTED} LIMIT 0",
    f"{ADMITTED} LIMIT 1",
    f"{ADMITTED} LIMIT 1000",
    f"{ADMITTED} limit 7",
    f"{ADMITTED} LIMIT 2;",
)

# Everything a row bound could be written as that this recogniser cannot read as
# a number, plus every clause the bound does not drag in with it.
REFUSED_ROW_BOUNDS = (
    f"{ADMITTED} LIMIT 1.5",
    f"{ADMITTED} LIMIT true",
    f"{ADMITTED} LIMIT false",
    f"{ADMITTED} LIMIT -1",
    f"{ADMITTED} LIMIT $n",
    f"{ADMITTED} LIMIT 2+3",
    f"{ADMITTED} LIMIT '2'",
    f"{ADMITTED} LIMIT NULL",
    f"{ADMITTED} SKIP 1 LIMIT 2",
    f"{ADMITTED} SKIP 0 LIMIT 1",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) "
    "RETURN DISTINCT path LIMIT 1",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) "
    "RETURN path ORDER BY a.id LIMIT 1",
    "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) "
    "WHERE a.id = 'd1' RETURN path LIMIT 1",
)


@pytest.mark.parametrize("text", ADMITTED_ROW_BOUNDS)
def test_a_literal_row_bound_is_part_of_the_admitted_shape(text: str) -> None:
    statement = parse(text)
    pattern = exact_path_projection(statement)

    assert pattern is statement.match_clauses[0].patterns[0]
    # The bound reaches the plan rather than being recognised and dropped.
    assert statement.return_clause.limit is not None


@pytest.mark.parametrize("text", REFUSED_ROW_BOUNDS)
def test_a_bound_this_subset_cannot_read_stays_refused(text: str) -> None:
    try:
        statement = parse(text)
    except GrafxParseError:
        return
    assert exact_path_projection(statement) is None


def test_the_engine_applies_the_bound_rather_than_the_caller(
    database: object,
) -> None:
    """The point of admitting LIMIT: the engine stops, nothing trims afterwards."""

    with database.begin("write") as writer:
        writer.execute("CREATE (:Decision {id: 'd3', title: 'third'})")
        writer.execute("CREATE (:Decision {id: 'd4', title: 'fourth'})")
        for source, target in (("d2", "d3"), ("d3", "d4")):
            writer.execute(
                f"MATCH (a:Decision {{id: '{source}'}}), "
                f"(b:Decision {{id: '{target}'}}) "
                "CREATE (a)-[:supersedes "
                "{layer: 'canonical', note: 'chain'}]->(b)"
            )

    def count(text: str) -> int:
        result = database.execute(text)
        assert result.columns == ("path",)
        return len(tuple(result.rows))

    assert count(ADMITTED) == 3
    assert count(f"{ADMITTED} LIMIT 1000") == 3
    assert count(f"{ADMITTED} LIMIT 2") == 2
    assert count(f"{ADMITTED} LIMIT 1") == 1
    # Zero is a bound a caller can mean, so it answers no rows instead of all.
    assert count(f"{ADMITTED} LIMIT 0") == 0


def test_limit_one_is_applied_before_the_public_result_budget(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "bounded-db", max_result_rows=1)
    try:
        with database.begin("write") as writer:
            writer.execute("CREATE (:Decision {id: 'd3', title: 'third'})")
            writer.execute("CREATE (:Decision {id: 'd4', title: 'fourth'})")
            for source, target in (("d2", "d3"), ("d3", "d4")):
                writer.execute(
                    f"MATCH (a:Decision {{id: '{source}'}}), "
                    f"(b:Decision {{id: '{target}'}}) "
                    "CREATE (a)-[:supersedes "
                    "{layer: 'canonical', note: 'chain'}]->(b)"
                )

        with pytest.raises(GrafxQueryBudgetExceeded):
            database.execute(ADMITTED)
        result = database.execute(f"{ADMITTED} LIMIT 1")

        assert len(result.rows) == 1
        assert result.columns == ("path",)
    finally:
        database.close()


def test_a_bounded_path_is_the_same_value_as_an_unbounded_one(
    database: object,
) -> None:
    unbounded = _paths(database)
    bounded = tuple(row[0] for row in database.execute(f"{ADMITTED} LIMIT 1").rows)

    assert len(bounded) == 1
    # The bound decides how many rows arrive and nothing about what a row is.
    assert bounded[0] == unbounded[0]
    _assert_path(
        bounded[0],
        source=("d1", "source"),
        target=("d2", "target"),
        relationship=("canonical", "primary"),
    )


def test_the_bound_becomes_a_limit_in_the_plan(database: object) -> None:
    """The bound is planned, so the engine stops rather than the caller trimming."""

    bounded = database.explain(f"{ADMITTED} LIMIT 2")
    unbounded = database.explain(ADMITTED)

    assert any(type(node) is LimitRows for node in bounded.walk())
    assert not any(type(node) is LimitRows for node in unbounded.walk())
    # The rest of the plan is the one this milestone froze, bound or not.
    assert any(type(node) is TraverseRelationship for node in bounded.walk())


@pytest.mark.parametrize("text", NEAR_MISSES)
def test_every_near_miss_stays_outside_the_literal_recogniser(text: str) -> None:
    assert exact_path_projection(parse(text)) is None


@pytest.mark.parametrize(
    "text",
    (
        "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path.id",
        "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN size(path)",
    ),
)
def test_a_near_miss_that_reads_its_path_keeps_the_typed_refusal(text: str) -> None:
    with pytest.raises(GrafxPlanError):
        build_plan(parse(text), catalog=_catalog())


def test_decorative_path_remains_accepted_without_materialising_a_path() -> None:
    text = "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN a.id"
    statement = parse(text)
    assert exact_path_projection(statement) is None
    planned = build_plan(statement, catalog=_catalog())
    assert any(type(node) is RelationshipScan for node in planned.root.walk())
    assert not any(
        type(node) is TraverseRelationship and node.path_variable is not None
        for node in planned.root.walk()
    )


# --- hostile trees no parser could have built ---------------------------------------------------


class _HostileText(str):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("hostile equality was dispatched")

    def __hash__(self) -> int:
        return str.__hash__(self)


class _SubQuery(Query):
    pass


class _SubMatch(MatchClause):
    pass


class _SubPath(PatternPath):
    pass


class _SubNode(NodePattern):
    pass


class _SubRelationship(RelationshipPattern):
    pass


class _SubReturn(ReturnClause):
    pass


class _SubItem(ReturnItem):
    pass


class _SubVariable(Variable):
    pass


def _as_subclass(cls: type, value: object) -> object:
    return cls(
        **{
            field.name: getattr(value, field.name)
            for field in dataclasses.fields(value)
        }
    )


def _hostile_trees() -> dict[str, Query]:
    statement = parse(ADMITTED)
    clause = statement.match_clauses[0]
    pattern = clause.patterns[0]
    source, target = pattern.nodes
    relationship = pattern.relationships[0]
    returned = statement.return_clause
    assert returned is not None
    item = returned.items[0]
    replace = dataclasses.replace

    def with_pattern(changed: PatternPath) -> Query:
        return replace(statement, match_clauses=(replace(clause, patterns=(changed,)),))

    def with_nodes(first: NodePattern, second: NodePattern = target) -> Query:
        return with_pattern(replace(pattern, nodes=(first, second)))

    def with_relationship(changed: RelationshipPattern) -> Query:
        return with_pattern(replace(pattern, relationships=(changed,)))

    return {
        "query subclass": _as_subclass(_SubQuery, statement),
        "match_clauses list": replace(statement, match_clauses=[clause]),
        "with_clauses list": replace(statement, with_clauses=[]),
        "updating_clauses list": replace(statement, updating_clauses=[]),
        "match subclass": replace(
            statement, match_clauses=(_as_subclass(_SubMatch, clause),)
        ),
        "optional int": replace(
            statement, match_clauses=(replace(clause, optional=0),)
        ),
        "patterns list": replace(
            statement, match_clauses=(replace(clause, patterns=[pattern]),)
        ),
        "path subclass": with_pattern(_as_subclass(_SubPath, pattern)),
        "nodes list": with_pattern(replace(pattern, nodes=[source, target])),
        "relationships list": with_pattern(
            replace(pattern, relationships=[relationship])
        ),
        "source subclass": with_nodes(_as_subclass(_SubNode, source)),
        "target subclass": with_nodes(source, _as_subclass(_SubNode, target)),
        "relationship subclass": with_relationship(
            _as_subclass(_SubRelationship, relationship)
        ),
        "source labels list": with_nodes(replace(source, labels=["Decision"])),
        "target labels list": with_nodes(source, replace(target, labels=["Decision"])),
        "relationship types list": with_relationship(
            replace(relationship, types=["supersedes"])
        ),
        "hop-range bool impostor": with_relationship(
            replace(relationship, hop_range_written=0)
        ),
        "minimum bool": with_relationship(replace(relationship, min_hops=True)),
        "maximum bool": with_relationship(replace(relationship, max_hops=True)),
        "direction string": with_relationship(
            replace(relationship, direction="outgoing")
        ),
        "path hostile text": with_pattern(
            replace(pattern, variable=_HostileText("path"))
        ),
        "source hostile text": with_nodes(replace(source, variable=_HostileText("a"))),
        "source label hostile text": with_nodes(
            replace(source, labels=(_HostileText("Decision"),))
        ),
        "target hostile text": with_nodes(
            source, replace(target, variable=_HostileText("b"))
        ),
        "target label hostile text": with_nodes(
            source, replace(target, labels=(_HostileText("Decision"),))
        ),
        "relationship hostile text": with_relationship(
            replace(relationship, variable=_HostileText("r"))
        ),
        "relationship type hostile text": with_relationship(
            replace(relationship, types=(_HostileText("supersedes"),))
        ),
        "return subclass": replace(
            statement, return_clause=_as_subclass(_SubReturn, returned)
        ),
        "return items list": replace(
            statement, return_clause=replace(returned, items=[item])
        ),
        "sort items list": replace(
            statement, return_clause=replace(returned, sort_items=[])
        ),
        "distinct int": replace(statement, return_clause=replace(returned, distinct=0)),
        "limit bool literal": replace(
            statement,
            return_clause=replace(returned, limit=Literal(value=True)),
        ),
        "limit variable": replace(
            statement,
            return_clause=replace(returned, limit=Variable(name="n")),
        ),
        "return item subclass": replace(
            statement,
            return_clause=replace(returned, items=(_as_subclass(_SubItem, item),)),
        ),
        "return variable subclass": replace(
            statement,
            return_clause=replace(
                returned,
                items=(
                    replace(
                        item,
                        expression=_as_subclass(_SubVariable, item.expression),
                    ),
                ),
            ),
        ),
        "return hostile text": replace(
            statement,
            return_clause=replace(
                returned,
                items=(
                    replace(
                        item,
                        expression=Variable(name=_HostileText("path")),
                    ),
                ),
            ),
        ),
    }


HOSTILE_TREES = _hostile_trees()


@pytest.mark.parametrize("name", sorted(HOSTILE_TREES))
def test_hostile_ast_is_not_recognised_or_allowed_to_escape_an_untyped_failure(
    name: str,
) -> None:
    statement = HOSTILE_TREES[name]
    assert exact_path_projection(statement) is None
    with pytest.raises(GrafxPlanError):
        analyze(statement)


@pytest.mark.parametrize("name", sorted(HOSTILE_TREES))
def test_hostile_ast_with_supplied_analysis_reaches_no_projected_path(
    name: str,
) -> None:
    statement = HOSTILE_TREES[name]
    try:
        planned = build_plan(
            statement,
            catalog=_catalog(),
            analysis=QueryAnalysis(statement=statement),
        )
    except GrafxPlanError:
        return
    assert not any(
        type(node) is TraverseRelationship and node.path_variable is not None
        for node in planned.root.walk()
    )
