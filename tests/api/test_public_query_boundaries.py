"""Public query values cross page access only as bounded, detached domain values."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import get_type_hints

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxParseError,
    GrafxPlanError,
    GrafxTransactionStateError,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    MAX_VALUE_DEPTH,
    ValueType,
)
from okto_grafx.domain.query.limits import (
    DEFAULT_MAX_QUERY_VALUE_CHARACTERS,
    MAX_LIST_ELEMENTS,
    MAX_MAP_ENTRIES,
    MAX_NAME_CHARACTERS,
    MAX_PARAMETERS,
    MAX_PROJECTION_ITEMS,
    MAX_QUERY_CHARACTERS,
    MAX_RENDERED_QUERY_CHARACTERS,
    MAX_STRING_CHARACTERS,
)
from okto_grafx.domain.query.ast import Literal, ReturnItem
from okto_grafx.domain.query.plan import (
    DistinctRows,
    FilterRows,
    NodeScan,
    PlanNode,
    ProduceResults,
    ProjectRows,
    SingleRow,
    UnionRows,
    VectorSearch,
)
from okto_grafx.engine.database import Database, Transaction
from okto_grafx.engine.query_engine import QueryEngine, QueryResult


class _ObservedMapping(Mapping[str, object]):
    """A mapping whose protocol methods report whether page access is active."""

    def __init__(
        self,
        values: Mapping[str, object],
        observe: Callable[[str], None],
    ) -> None:
        self._values = dict(values)
        self._observe = observe

    def __iter__(self) -> Iterator[str]:
        self._observe("mapping.__iter__")
        return iter(self._values)

    def __len__(self) -> int:
        self._observe("mapping.__len__")
        return len(self._values)

    def __getitem__(self, key: str) -> object:
        self._observe("mapping.__getitem__")
        return self._values[key]


class _ObservedSequence(Sequence[object]):
    """A sequence whose iterator is executable host code."""

    def __init__(
        self, values: tuple[object, ...], observe: Callable[[str], None]
    ) -> None:
        self._values = values
        self._observe = observe

    def __len__(self) -> int:
        self._observe("sequence.__len__")
        return len(self._values)

    def __getitem__(self, position: int) -> object:
        self._observe("sequence.__getitem__")
        return self._values[position]

    def __iter__(self) -> Iterator[object]:
        self._observe("sequence.__iter__")
        return iter(self._values)


def test_input_callbacks_finish_before_page_access_and_the_engine_gets_exact_values() -> (
    None
):
    events: list[tuple[str, bool]] = []

    class HostileText(str):
        def __len__(self) -> int:
            events.append(("text.__len__", database._metrics.page_access_active))
            return str.__len__(self)

    with connect(":memory:") as database:
        observe = lambda name: events.append(  # noqa: E731 - captures the current facade
            (name, database._metrics.page_access_active)
        )
        values = _ObservedSequence((1,), observe)
        parameters = _ObservedMapping({"values": values}, observe)
        reader = database.begin("read")
        result = reader.execute(HostileText("RETURN 1 IN $values AS found"), parameters)
        reader.rollback()

    assert result.rows == ((True,),)
    assert events
    assert all(not active for _name, active in events)
    assert all(name != "text.__len__" for name, _active in events)


def test_parameter_callback_can_rollback_but_liveness_is_rechecked_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached = False

    def should_not_execute(
        _engine: QueryEngine,
        _text: str,
        _context: object,
        _parameters: Mapping[str, object] | None = None,
    ) -> QueryResult:
        nonlocal reached
        reached = True
        raise AssertionError("an inactive transaction reached the query engine")

    monkeypatch.setattr(QueryEngine, "execute", should_not_execute)
    with connect(":memory:") as database:
        reader = database.begin("read")
        rolled_back = False

        def rollback_once(_name: str) -> None:
            nonlocal rolled_back
            if not rolled_back:
                rolled_back = True
                reader.rollback()

        parameters = _ObservedMapping({"x": 7}, rollback_once)
        with pytest.raises(GrafxTransactionStateError):
            reader.execute("RETURN $x AS x", parameters)

    assert not reader.active
    assert reached is False


def test_query_text_size_is_refused_before_page_access_and_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached = False

    def should_not_execute(*_args: object, **_kwargs: object) -> QueryResult:
        nonlocal reached
        reached = True
        raise AssertionError("oversized query text reached the engine")

    monkeypatch.setattr(QueryEngine, "execute", should_not_execute)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxParseError):
            reader.execute("x" * (MAX_QUERY_CHARACTERS + 1))
        assert reader.active
        reader.rollback()

    assert reached is False


def test_maximum_string_literal_keeps_its_query_derived_result_column() -> None:
    literal = "x" * MAX_STRING_CHARACTERS
    text = f"RETURN {literal!r}"
    assert len(text) < MAX_QUERY_CHARACTERS

    with connect(":memory:") as database:
        plan = database.explain(text)
        result = database.execute(text)

    assert type(plan) is ProduceResults
    assert plan.columns == (repr(literal),)
    assert result.columns == (repr(literal),)
    assert result.rows == ((literal,),)


def test_case_and_subscript_survive_the_detached_public_plan_boundary() -> None:
    text = "RETURN CASE WHEN true THEN [10, 20][2] ELSE 0 END AS selected"
    with connect(":memory:") as database:
        plan = database.explain(text)
        result = database.execute(text)

    assert type(plan) is ProduceResults
    assert result.columns == ("selected",)
    assert result.rows == ((20,),)


def test_repeated_internal_plan_validation_is_memoized_but_public_trees_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_grafx.engine.public_views as public_views

    walks = 0
    original = public_views._query_plan_nodes

    def counted_nodes(value: object):
        nonlocal walks
        walks += 1
        return original(value)

    monkeypatch.setattr(public_views, "_query_plan_nodes", counted_nodes)
    with connect(":memory:") as database:
        first = database.explain("RETURN 1 AS value")
        second = database.explain("RETURN 1 AS value")

    assert walks == 1
    assert first == second
    assert first is not second
    assert all(left is not right for left, right in zip(first.walk(), second.walk()))
    object.__setattr__(first, "columns", ("changed",))
    assert second.columns == ("value",)


@pytest.mark.parametrize("character", ["\x00", "\U000e0001"])
def test_maximum_nonprintable_string_literal_fits_rendered_query_bound(
    character: str,
) -> None:
    assert not character.isprintable()
    literal = character * MAX_STRING_CHARACTERS
    text = "RETURN '" + literal + "'"
    rendered = repr(literal)
    assert len(text) == MAX_STRING_CHARACTERS + 9
    assert MAX_QUERY_CHARACTERS < len(rendered) <= MAX_RENDERED_QUERY_CHARACTERS

    with connect(":memory:") as database:
        plan = database.explain(text)
        result = database.execute(text)

    assert type(plan) is ProduceResults
    assert plan.columns == (rendered,)
    assert result.columns == (rendered,)
    assert result.rows == ((literal,),)


@pytest.mark.parametrize("signal", [RuntimeError("boom"), KeyboardInterrupt()])
def test_parameter_callback_failures_are_contained_but_process_signals_pass(
    signal: BaseException,
) -> None:
    class ExplodingParameters(Mapping[str, object]):
        def __iter__(self) -> Iterator[str]:
            raise signal

        def __len__(self) -> int:
            return 1

        def __getitem__(self, _key: str) -> object:
            return 1

    expected = (
        GrafxConfigurationError if isinstance(signal, Exception) else KeyboardInterrupt
    )
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(expected):
            reader.execute("RETURN $x AS x", ExplodingParameters())
        assert reader.active
        reader.rollback()


def test_parameter_and_result_maps_are_mutable_but_owned_and_lists_are_tuples() -> None:
    source = {"nested": [1]}
    with connect(":memory:") as database:
        reader = database.begin("read")
        result = reader.execute("RETURN $x AS x", {"x": source})
        reader.rollback()

    observed = result.rows[0][0]
    assert type(observed) is dict
    assert observed is not source
    assert observed == {"nested": (1,)}
    source["nested"].append(2)
    assert observed == {"nested": (1,)}
    observed["owned"] = True
    assert "owned" not in source


def test_shared_acyclic_values_are_accepted_and_detached_per_occurrence() -> None:
    shared = [1]
    source = {"left": shared, "right": shared}
    with connect(":memory:") as database:
        reader = database.begin("read")
        result = reader.execute("RETURN $x AS x", {"x": source})
        reader.rollback()

    observed = result.rows[0][0]
    assert observed == {"left": (1,), "right": (1,)}
    assert observed is not source


@pytest.mark.parametrize("value", [INT64_MIN, INT64_MAX])
def test_int64_boundaries_are_accepted(value: int) -> None:
    with connect(":memory:") as database:
        assert database.execute("RETURN $x AS x", {"x": value}).rows == ((value,),)


@pytest.mark.parametrize("value", [INT64_MIN - 1, INT64_MAX + 1])
def test_integers_outside_int64_are_refused(value: int) -> None:
    with connect(":memory:") as database:
        with pytest.raises(GrafxConfigurationError):
            database.execute("RETURN $x AS x", {"x": value})


def test_list_map_string_and_parameter_limits_have_live_edges() -> None:
    accepted_list = list(range(MAX_LIST_ELEMENTS))
    accepted_map = {str(index): index for index in range(MAX_MAP_ENTRIES)}
    accepted_parameters = {
        **{f"p{index}": index for index in range(MAX_PARAMETERS - 1)},
        "x": 1,
    }
    with connect(":memory:") as database:
        assert (
            len(database.execute("RETURN $x AS x", {"x": accepted_list}).rows[0][0])
            == 1024
        )
        assert (
            len(database.execute("RETURN $x AS x", {"x": accepted_map}).rows[0][0])
            == 256
        )
        assert database.execute(
            "RETURN $x AS x", {"x": "s" * DEFAULT_MAX_QUERY_VALUE_CHARACTERS}
        ).rows == (("s" * DEFAULT_MAX_QUERY_VALUE_CHARACTERS,),)
        assert database.execute("RETURN $x AS x", accepted_parameters).rows == ((1,),)

        refused = (
            list(range(MAX_LIST_ELEMENTS + 1)),
            {str(index): index for index in range(MAX_MAP_ENTRIES + 1)},
            "s" * (DEFAULT_MAX_QUERY_VALUE_CHARACTERS + 1),
        )
        for value in refused:
            with pytest.raises(GrafxConfigurationError):
                database.execute("RETURN $x AS x", {"x": value})
        excessive_parameters = dict(accepted_parameters)
        excessive_parameters["overflow"] = 1
        with pytest.raises(GrafxConfigurationError):
            database.execute("RETURN $x AS x", excessive_parameters)


def test_query_value_string_limit_is_configurable_without_becoming_unbounded() -> None:
    with connect(":memory:", max_query_value_characters=17_000) as database:
        accepted = "x" * 17_000
        assert database.execute("RETURN $x AS x", {"x": accepted}).rows == (
            (accepted,),
        )
        with pytest.raises(GrafxConfigurationError) as caught:
            database.execute("RETURN $x AS x", {"x": "x" * 17_001})
        assert caught.value.details == {
            "field": "parameters.x",
            "value": 17_001,
            "limit": 17_000,
        }

    expanded = "x" * (DEFAULT_MAX_QUERY_VALUE_CHARACTERS + 1)
    with connect(
        ":memory:", max_query_value_characters=len(expanded)
    ) as database:
        assert database.execute("RETURN $x AS x", {"x": expanded}).rows == (
            (expanded,),
        )


def test_query_value_string_limit_applies_inside_nested_parameter_values() -> None:
    with connect(":memory:", max_query_value_characters=20_000) as database:
        accepted = "x" * 20_000
        assert database.execute("RETURN $x AS x", {"x": [{"body": accepted}]}).rows
        with pytest.raises(GrafxConfigurationError) as caught:
            database.execute(
                "RETURN $x AS x", {"x": [{"body": "x" * 20_001}]}
            )
        assert caught.value.details["limit"] == 20_000


def test_pulse_sized_string_parameter_crosses_the_create_boundary() -> None:
    content = "x" * 27_825
    with connect(":memory:") as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Artifact(id STRING, content STRING, PRIMARY KEY(id))"
            )
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE (n:Artifact {id: $id, content: $content})",
                {"id": "refinement", "content": content},
            )
        assert database.execute(
            "MATCH (n:Artifact {id: $id}) RETURN n.content",
            {"id": "refinement"},
        ).rows == ((content,),)


def _nested_list(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def test_value_depth_limit_has_a_live_edge() -> None:
    with connect(":memory:") as database:
        accepted = database.execute(
            "RETURN $x AS x", {"x": _nested_list(MAX_VALUE_DEPTH)}
        )
        assert accepted.rows
        with pytest.raises(GrafxConfigurationError):
            database.execute("RETURN $x AS x", {"x": _nested_list(MAX_VALUE_DEPTH + 1)})


def test_cycles_capabilities_and_canonical_key_collisions_are_refused() -> None:
    cycle: list[object] = []
    cycle.append(cycle)

    class CollidingMap(Mapping[object, object]):
        def __iter__(self) -> Iterator[object]:
            return iter((1, 1.0))

        def __len__(self) -> int:
            return 2

        def __getitem__(self, key: object) -> object:
            return "int" if type(key) is int else "float"

        def items(self):  # noqa: ANN201 - adversarial Mapping protocol
            return ((1, "int"), (1.0, "float"))

    with connect(":memory:") as database:
        for value in (cycle, lambda: None, CollidingMap()):
            with pytest.raises(GrafxConfigurationError):
                database.execute("RETURN $x AS x", {"x": value})


@pytest.mark.parametrize(
    "arguments",
    [
        {"columns": ["x"]},
        {"columns": ("x", "x")},
        {"columns": ("x",), "rows": [[1]]},
        {"columns": ("x",), "rows": ((1, 2),)},
        {"statistics": MappingProxyType({"rows": 1})},
        {"statistics": {"rows": True}},
        {"statistics": {"rows": -1}},
        {"plan": object()},
        {"columns": tuple(f"c{index}" for index in range(MAX_PROJECTION_ITEMS + 1))},
    ],
)
def test_query_result_constructor_refuses_structural_ambiguity(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(GrafxPlanError):
        QueryResult(**arguments)  # type: ignore[arg-type]


def test_query_result_statistics_are_exact_owned_and_mutable() -> None:
    statistics = {"rows": 1}
    result = QueryResult(statistics=statistics)
    assert type(result.statistics) is dict
    assert result.statistics is not statistics
    result.statistics["owned"] = 2
    assert statistics == {"rows": 1}


def test_query_result_column_and_statistic_bounds_have_live_edges() -> None:
    column = "c" * MAX_RENDERED_QUERY_CHARACTERS
    statistic = "s" * MAX_NAME_CHARACTERS
    result = QueryResult(
        columns=(column,),
        rows=((None,),),
        statistics={statistic: INT64_MAX},
    )
    assert result.columns == (column,)
    assert result.statistics == {statistic: INT64_MAX}

    invalid = (
        {"columns": (column + "x",)},
        {"statistics": {statistic + "x": 1}},
        {"statistics": {"rows": INT64_MAX + 1}},
    )
    for arguments in invalid:
        with pytest.raises(GrafxPlanError):
            QueryResult(**arguments)  # type: ignore[arg-type]


def test_query_result_keeps_only_outer_ownership_before_the_public_facade() -> None:
    payload = {"nested": [1]}
    rows = ((payload,),)
    result = QueryResult(columns=("payload",), rows=rows)  # type: ignore[arg-type]
    assert result.rows is not rows
    assert result.rows[0][0] is payload


def test_query_result_constructor_validates_the_plan_tree() -> None:
    cyclic = ProduceResults(child=SingleRow())
    object.__setattr__(cyclic, "child", cyclic)
    with pytest.raises(GrafxPlanError):
        QueryResult(plan=cyclic)

    bounded_search = VectorSearch(
        child=SingleRow(),
        variable="n",
        space=Literal("s"),
        query_vector=Literal((1.0,)),
        property_key="embedding",
        score_column="score",
        k=Literal(1),
    )
    post_filtered = FilterRows(child=bounded_search, predicate=Literal(True))
    with pytest.raises(GrafxPlanError):
        QueryResult(plan=post_filtered)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ordinary"),
        GrafxPlanError("typed", field="plan", value="typed"),
        KeyboardInterrupt(),
        SystemExit(7),
    ],
)
def test_query_result_plan_callback_failures_have_stable_taxonomy(
    failure: BaseException,
) -> None:
    class HostilePlan(PlanNode):
        def children(self) -> tuple[PlanNode, ...]:
            raise failure

    expected = GrafxPlanError if isinstance(failure, Exception) else type(failure)
    with pytest.raises(expected) as caught:
        QueryResult(plan=HostilePlan())
    if isinstance(failure, GrafxPlanError) or not isinstance(failure, Exception):
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure


@pytest.mark.parametrize("text", ["RETURN 1, 1", "RETURN 1, 2 AS `1`"])
def test_duplicate_output_columns_are_refused_before_execution_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    reached = False

    def should_not_run(*_args: object, **_kwargs: object) -> QueryResult:
        nonlocal reached
        reached = True
        raise AssertionError("a duplicate result shape reached execution dispatch")

    monkeypatch.setattr(QueryEngine, "_run", should_not_run)
    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain(text)
        reader = database.begin("read")
        with pytest.raises(GrafxPlanError):
            reader.execute(text)
        assert reader.active
        reader.rollback()

    assert reached is False


@pytest.mark.parametrize("finalizer", ["commit", "rollback"])
def test_duplicate_write_columns_leave_zero_mutation_and_a_usable_transaction(
    finalizer: str,
) -> None:
    with connect(":memory:") as database:
        schema = database.begin("write")
        schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        schema.commit()

        writer = database.begin("write")
        with pytest.raises(GrafxPlanError):
            writer.execute("CREATE (p:Person {id: 1}) RETURN p.id, p.id")
        assert writer.active
        assert writer.execute("MATCH (p:Person) RETURN p.id").rows == ()
        getattr(writer, finalizer)()
        assert not writer.active

        assert database.execute("MATCH (p:Person) RETURN p.id").rows == ()


def test_malformed_collaborator_result_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(QueryEngine, "execute", lambda *_args, **_kwargs: object())
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxPlanError):
            reader.execute("RETURN 1 AS x")
        assert reader.active
        reader.rollback()


def test_forged_result_arity_is_revalidated_outside_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = QueryResult(columns=("x",), rows=((1,),))
    object.__setattr__(forged, "rows", ((1, 2),))
    monkeypatch.setattr(QueryEngine, "execute", lambda *_args, **_kwargs: forged)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxPlanError):
            reader.execute("RETURN 1 AS x")
        reader.rollback()


def test_hostile_oversized_result_columns_and_statistics_are_refused_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            events.append("text.__str__")
            return str.__str__(self)

        def __len__(self) -> int:
            events.append("text.__len__")
            return str.__len__(self)

        def __hash__(self) -> int:
            events.append("text.__hash__")
            return str.__hash__(self)

    oversized_column = HostileText("c" * (MAX_RENDERED_QUERY_CHARACTERS + 1))
    oversized_statistic = HostileText("s" * (MAX_NAME_CHARACTERS + 1))
    forged = QueryResult(columns=("x",), rows=((1,),))
    candidates = (
        ((oversized_column,), ((1,),), {}),
        ((), (), {oversized_statistic: 1}),
        ((), (), {"rows": INT64_MAX + 1}),
    )
    events.clear()

    with connect(":memory:") as database:
        reader = database.begin("read")
        for columns, rows, statistics in candidates:
            object.__setattr__(forged, "columns", columns)
            object.__setattr__(forged, "rows", rows)
            object.__setattr__(forged, "statistics", statistics)
            monkeypatch.setattr(
                QueryEngine, "execute", lambda *_args, **_kwargs: forged
            )
            with pytest.raises(GrafxPlanError):
                reader.execute("RETURN 1 AS x")
        reader.rollback()

    assert events == []


def test_collaborator_result_is_detached_only_after_page_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[bool] = []
    source = {"value": 1}

    class ResultMap(Mapping[str, object]):
        def __iter__(self) -> Iterator[str]:
            events.append(database._metrics.page_access_active)
            return iter(source)

        def __len__(self) -> int:
            events.append(database._metrics.page_access_active)
            return len(source)

        def __getitem__(self, key: str) -> object:
            events.append(database._metrics.page_access_active)
            return source[key]

    raw = QueryResult(columns=("x",), rows=((ResultMap(),),))  # type: ignore[arg-type]
    monkeypatch.setattr(QueryEngine, "execute", lambda *_args, **_kwargs: raw)
    with connect(":memory:") as database:
        reader = database.begin("read")
        result = reader.execute("RETURN 1 AS x")
        reader.rollback()

    assert events and not any(events)
    assert result.rows == (({"value": 1},),)
    assert result.rows[0][0] is not source
    source["value"] = 2
    assert result.rows == (({"value": 1},),)


def test_explain_rejects_an_invalid_plan_outside_page_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, bool]] = []

    class HostilePlan(PlanNode):
        @property
        def label(self) -> str:
            events.append(("label", database._metrics.page_access_active))
            return "hostile"

        def children(self) -> tuple[PlanNode, ...]:
            events.append(("children", database._metrics.page_access_active))
            return ()

        def details(self) -> Mapping[str, object]:
            events.append(("details", database._metrics.page_access_active))
            return {}

    hostile = HostilePlan()
    raw = ProduceResults(child=hostile)

    def hostile_explain(_engine: QueryEngine, _text: str) -> PlanNode:
        events.append(("engine", database._metrics.page_access_active))
        return raw

    monkeypatch.setattr(QueryEngine, "explain", hostile_explain)
    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1")

    assert events == [("engine", True)]


def test_exact_plan_nodes_are_rebuilt_with_exact_scalar_tuple_and_schema_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            events.append("text.__str__")
            return str.__str__(self)

        def __len__(self) -> int:
            events.append("text.__len__")
            return str.__len__(self)

    class HostileTuple(tuple):
        def __iter__(self):  # noqa: ANN204 - hostile tuple protocol
            events.append("tuple.__iter__")
            return tuple.__iter__(self)

    column = ColumnDef(name=HostileText("id"), type=ValueType.INT64, nullable=False)
    table = TableDef(
        table_id=1,
        name=HostileText("Person"),
        kind=HostileText("node"),
        columns=HostileTuple((column,)),
        primary_key=HostileText("id"),
    )
    raw = ProduceResults(
        child=NodeScan(
            child=SingleRow(),
            variable=HostileText("p"),
            table=table,
        ),
        columns=HostileTuple((HostileText("p.id"),)),
    )
    events.clear()
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        observed = database.explain("RETURN 1")

    assert events == []
    assert observed is not raw
    assert type(observed) is ProduceResults
    assert type(observed.columns) is tuple
    assert type(observed.columns[0]) is str
    scan = observed.child
    assert type(scan) is NodeScan
    assert type(scan.variable) is str
    assert scan.table is not table
    assert type(scan.table) is TableDef
    assert type(scan.table.name) is str
    assert type(scan.table.columns) is tuple
    assert type(scan.table.columns[0]) is ColumnDef
    assert type(scan.table.columns[0].name) is str


def test_union_operator_is_rebuilt_as_an_owned_exact_public_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_union = UnionRows(
        left=SingleRow(),
        right=SingleRow(),
        columns=("value",),
    )
    raw = ProduceResults(
        child=DistinctRows(child=raw_union),
        columns=("value",),
    )
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        observed = database.explain("RETURN 1 AS value")

    assert type(observed) is ProduceResults
    assert observed is not raw
    distinct = observed.child
    assert type(distinct) is DistinctRows
    union = distinct.child
    assert type(union) is UnionRows
    assert union is not raw_union
    assert union.left is not raw_union.left
    assert union.right is not raw_union.right
    assert union.columns == ("value",)


@pytest.mark.parametrize("shape", ("shared", "cyclic"))
def test_public_union_refuses_a_plan_graph(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    shared = SingleRow()
    union = UnionRows(left=shared, right=shared, columns=("value",))
    if shape == "cyclic":
        object.__setattr__(union, "left", union)
    raw = ProduceResults(child=DistinctRows(child=union), columns=("value",))
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1 AS value")


def test_hostile_oversized_union_column_is_refused_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            events.append("text.__str__")
            return str.__str__(self)

        def __len__(self) -> int:
            events.append("text.__len__")
            return str.__len__(self)

    raw = ProduceResults(
        child=DistinctRows(
            child=UnionRows(
                left=SingleRow(),
                right=SingleRow(),
                columns=(HostileText("c" * (MAX_RENDERED_QUERY_CHARACTERS + 1)),),
            )
        ),
        columns=("value",),
    )
    events.clear()
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1 AS value")

    assert events == []


def test_hostile_oversized_plan_column_is_refused_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class HostileText(str):
        def __str__(self) -> str:
            events.append("text.__str__")
            return str.__str__(self)

        def __len__(self) -> int:
            events.append("text.__len__")
            return str.__len__(self)

    raw = ProduceResults(
        child=SingleRow(),
        columns=(HostileText("c" * (MAX_RENDERED_QUERY_CHARACTERS + 1)),),
    )
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1")

    assert events == []


def test_collaborator_plan_with_duplicate_columns_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = ProduceResults(child=SingleRow(), columns=("x", "x"))
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1 AS x")


def test_literal_value_graph_in_an_exact_plan_is_deeply_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"nested": [1]}
    raw = ProduceResults(
        child=ProjectRows(
            child=SingleRow(),
            items=(ReturnItem(expression=Literal(source), alias="payload"),),
        ),
        columns=("payload",),
    )
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        observed = database.explain("RETURN 1")

    projected = observed.child
    assert type(projected) is ProjectRows
    literal = projected.items[0].expression
    assert type(literal) is Literal
    assert literal.value == {"nested": (1,)}
    assert literal.value is not source
    source["nested"].append(2)
    assert literal.value == {"nested": (1,)}
    literal.value["owned"] = True
    assert "owned" not in source


def test_expression_subclass_inside_an_exact_operator_is_refused_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[bool] = []

    class HostileLiteral(Literal):
        def describe(self) -> str:
            events.append(database._metrics.page_access_active)
            return "hostile"

    raw = ProduceResults(
        child=ProjectRows(
            child=SingleRow(),
            items=(ReturnItem(expression=HostileLiteral(1), alias="x"),),
        ),
        columns=("x",),
    )
    monkeypatch.setattr(QueryEngine, "explain", lambda *_args: raw)

    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError):
            database.explain("RETURN 1")

    assert events == []


def test_query_result_plan_is_validated_after_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = QueryResult(columns=("x",), rows=((1,),), plan=SingleRow())
    object.__setattr__(forged, "plan", object())
    monkeypatch.setattr(QueryEngine, "execute", lambda *_args, **_kwargs: forged)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxPlanError):
            reader.execute("RETURN 1 AS x")
        reader.rollback()


def test_public_query_annotations_name_the_values_the_doors_return() -> None:
    assert get_type_hints(Transaction.execute)["return"] is QueryResult
    assert get_type_hints(Database.execute)["return"] is QueryResult
    assert get_type_hints(Database.explain)["return"] is PlanNode
