"""Focused evidence for bounded byte-budget spill of blocking query operators."""

from __future__ import annotations

from math import isnan

import pytest

import okto_grafx
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
from okto_grafx.domain.errors import GrafxQueryBudgetExceeded
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import Timestamp, Uuid, ValueType, VectorValue
from okto_grafx.domain.query.memory import LogicalMemoryBudget
from okto_grafx.domain.query.plan import AggregateRows, plan_nodes
from okto_grafx.engine.query_engine import (
    _SpilledAggregateState,
    _spill_decode,
    _spill_encode,
)
from tests.query.stack import QueryStack, build_query_stack, vector

_ROWS = tuple(
    (
        index,
        f"name-{79 - index:03d}",
        None if index % 11 == 0 else 20 + index % 17,
        ("London", "New York", "Rotterdam", None)[index % 4],
    )
    for index in range(1, 81)
)


def _filled(*, budget: int | None) -> QueryStack:
    stack = build_query_stack(query_memory_budget_bytes=budget)
    for row in _ROWS:
        stack.insert("Person", row[0], row)
    return stack


def _run(stack: QueryStack, text: str):
    return stack.engine.execute(text, stack.transaction())


@pytest.mark.parametrize("query", (
    "UNWIND $xs AS x RETURN DISTINCT x",
    "UNWIND $xs AS x RETURN x, count(*) AS n",
    "UNWIND $xs AS x RETURN count(DISTINCT x)",
    "UNWIND $xs AS x RETURN x ORDER BY x",
    "UNWIND $xs AS x RETURN min(x), max(x)",
))
def test_numeric_equivalence_is_identical_in_memory_and_spill(query, monkeypatch):
    values = ([1, 1.0, 0, -0.0, True, False, 9007199254740992, 9007199254740993,
               9007199254740992.0, {"v": 1}, {"v": 1.0}, [1, None], [1.0, None]] * 10)
    values.extend(f"unique-{index}" for index in range(120))
    ordinary = build_query_stack(query_memory_budget_bytes=None)
    spilled = build_query_stack(query_memory_budget_bytes=4096)
    expected = ordinary.engine.execute(query, ordinary.transaction(), {"xs": values})
    opened = []
    original_open = LocalQuerySpillFactory.open
    def record_open(factory, budget):
        opened.append(True)
        return original_open(factory, budget)
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record_open)
    actual = spilled.engine.execute(query, spilled.transaction(), {"xs": values})
    assert actual.rows == expected.rows
    assert opened, "The equivalence test must actually exercise external spill."


def test_external_sort_matches_the_unbounded_stable_result() -> None:
    ordinary = _filled(budget=None)
    spilled = _filled(budget=2_048)
    text = (
        "MATCH (p:Person) RETURN p.id AS id, p.city AS city, p.name AS name "
        "ORDER BY city DESC, name, id DESC"
    )

    assert _run(spilled, text).rows == _run(ordinary, text).rows

    window = text + " SKIP 9 LIMIT 13"
    assert _run(spilled, window).rows == _run(ordinary, window).rows


def test_spilled_sort_keeps_the_canonical_stable_nan_order() -> None:
    stack = build_query_stack(query_memory_budget_bytes=1_024)
    stack.catalog_store.catalog.add_table(
        TableDef(
            table_id=4,
            name="Measurement",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="value", type=ValueType.DOUBLE),
            ),
            primary_key="id",
        )
    )
    stack.catalog_store.save()
    for record_id, value in ((1, float("nan")), (2, 0.0), (3, float("nan")), (4, -1.0)):
        stack.insert("Measurement", record_id, (record_id, value))

    ascending = _run(
        stack, "MATCH (m:Measurement) RETURN m.id, m.value ORDER BY m.value"
    )
    descending = _run(
        stack, "MATCH (m:Measurement) RETURN m.id, m.value ORDER BY m.value DESC"
    )

    assert tuple(row[0] for row in ascending.rows) == (4, 2, 1, 3)
    assert tuple(row[0] for row in descending.rows) == (1, 3, 2, 4)
    assert all(isnan(row[1]) for row in ascending.rows[-2:])


def test_spilled_grouping_and_distinct_aggregates_match_in_memory() -> None:
    ordinary = _filled(budget=None)
    spilled = _filled(budget=4_096)
    text = (
        "MATCH (p:Person) RETURN p.city AS city, count(*) AS rows, "
        "count(DISTINCT p.age) AS ages, sum(p.age) AS total, avg(p.age) AS mean, "
        "min(p.age) AS youngest, max(p.age) AS oldest, collect(p.name) AS names "
        "ORDER BY city"
    )

    assert _run(spilled, text).rows == _run(ordinary, text).rows

    encounter_order = "MATCH (p:Person) RETURN p.city AS city, count(*) AS rows"
    assert _run(spilled, encounter_order).rows == _run(ordinary, encounter_order).rows


def test_spilled_return_distinct_preserves_first_occurrence_and_sort_context() -> None:
    ordinary = _filled(budget=None)
    spilled = _filled(budget=2_048)

    for text in (
        "MATCH (p:Person) RETURN DISTINCT p.city AS city",
        "MATCH (p:Person) RETURN DISTINCT p.id AS id ORDER BY p.id DESC",
        "MATCH (p:Person) RETURN DISTINCT p AS person ORDER BY p DESC",
        "MATCH (p:Person) WHERE p.id <= 6 RETURN p.city AS city "
        "UNION MATCH (p:Person) WHERE p.id >= 4 AND p.id <= 10 RETURN p.city AS city",
    ):
        assert _run(spilled, text).rows == _run(ordinary, text).rows


def test_spilled_grouping_preserves_repeated_and_distinct_nan_identities() -> None:
    """Strong, budgeted identity tokens prevent allocator-address reuse collisions."""

    def grouped(budget: int | None) -> tuple[list[int], int, int, int, int, int]:
        stack = build_query_stack(query_memory_budget_bytes=budget)
        stack.catalog_store.catalog.add_table(
            TableDef(
                table_id=4,
                name="Measurement",
                kind="node",
                columns=(
                    ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                    ColumnDef(name="value", type=ValueType.DOUBLE),
                ),
                primary_key="id",
            )
        )
        stack.catalog_store.save()
        stack.insert("Measurement", 1, (1, float("nan")))
        stack.insert("Measurement", 2, (2, float("nan")))
        stack.insert("Measurement", 3, (3, float("nan")))
        groups = _run(
            stack,
            "MATCH (m:Measurement) RETURN m.value AS value, count(*) AS rows",
        )
        distinct = _run(
            stack,
            "MATCH (m:Measurement) RETURN count(DISTINCT m.value) AS values",
        )
        shared = float("nan")
        parameter_group = stack.engine.execute(
            "MATCH (m:Measurement) RETURN $value AS value, count(*) AS rows",
            stack.transaction(),
            {"value": shared},
        )
        parameter_distinct = stack.engine.execute(
            "MATCH (m:Measurement) RETURN count(DISTINCT $value) AS values",
            stack.transaction(),
            {"value": shared},
        )
        row_distinct = _run(
            stack,
            "MATCH (m:Measurement) RETURN DISTINCT m.value AS value",
        )
        parameter_row_distinct = stack.engine.execute(
            "MATCH (m:Measurement) RETURN DISTINCT $value AS value",
            stack.transaction(),
            {"value": shared},
        )
        return (
            sorted(row[1] for row in groups.rows),
            distinct.rows[0][0],
            parameter_group.rows[0][1],
            parameter_distinct.rows[0][0],
            len(row_distinct.rows),
            len(parameter_row_distinct.rows),
        )

    assert grouped(4_096) == grouped(None) == ([1, 1, 1], 3, 3, 1, 3, 1)


def test_spilled_identity_matches_python_for_signed_zero_and_vector_components() -> (
    None
):
    """Byte encodings must not split values which the historical frozen key considers equal."""

    def scalar_results(budget: int | None):
        stack = build_query_stack(query_memory_budget_bytes=budget)
        stack.catalog_store.catalog.add_table(
            TableDef(
                table_id=4,
                name="Measurement",
                kind="node",
                columns=(
                    ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                    ColumnDef(name="value", type=ValueType.DOUBLE),
                ),
                primary_key="id",
            )
        )
        stack.catalog_store.save()
        stack.insert("Measurement", 1, (1, 0.0))
        stack.insert("Measurement", 2, (2, -0.0))
        return (
            _run(
                stack,
                "MATCH (m:Measurement) RETURN m.value AS value, count(*) AS rows",
            ).rows,
            _run(
                stack,
                "MATCH (m:Measurement) RETURN count(DISTINCT m.value) AS values",
            ).rows,
            _run(stack, "MATCH (m:Measurement) RETURN DISTINCT m.value AS value").rows,
        )

    def vector_results(budget: int | None):
        stack = build_query_stack(query_memory_budget_bytes=budget)
        stack.insert("Chunk", 1, (1, 0, vector((0.0, 1.0, 0.0, 0.0))))
        stack.insert("Chunk", 2, (2, 0, vector((-0.0, 1.0, 0.0, 0.0))))
        return (
            _run(stack, "MATCH (c:Chunk) RETURN DISTINCT c.embedding AS value").rows,
            _run(
                stack,
                "MATCH (c:Chunk) RETURN count(DISTINCT c.embedding) AS values",
            ).rows,
        )

    assert (
        scalar_results(4_096)
        == scalar_results(None)
        == (
            ((0.0, 2),),
            ((1,),),
            ((0.0,),),
        )
    )
    assert vector_results(4_096) == vector_results(None)


def test_the_versioned_spill_codec_preserves_every_stored_value_kind() -> None:
    values = (
        None,
        False,
        7,
        -0.0,
        "text",
        b"bytes",
        Timestamp(123),
        Uuid(bytes(range(16))),
        VectorValue((0.25, -0.5), space_ref=1, dtype="float64"),
        (1, "nested", None),
        {"key": (True, b"value")},
    )

    encoded = _spill_encode(values, key=False)

    assert _spill_decode(encoded, key=False) == values


def test_closed_sorters_do_not_accumulate_in_a_long_lived_workspace(tmp_path) -> None:
    """One DISTINCT helper per group must not turn workspace metadata into O(groups)."""
    budget = LogicalMemoryBudget(1_024, operator="lifecycle-probe")
    workspace = LocalQuerySpillFactory(str(tmp_path)).open(budget)

    for _ in range(1_000):
        sorter = workspace.sorter(lambda left, right: (left > right) - (left < right))
        sorter.close()

    assert workspace._sorters == []
    assert budget.retained == 0
    workspace.close()


def test_cursor_close_removes_a_partially_consumed_sort_spill(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    database_root = tmp_path / "database"
    factory = LocalQuerySpillFactory
    monkeypatch.setattr(
        "okto_grafx.api.assembly.LocalQuerySpillFactory",
        lambda: factory(str(spill_root)),
    )
    database = okto_grafx.connect(database_root, query_memory_budget_bytes=1_024)
    with database.begin("write") as transaction:
        transaction.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")
        for value in range(24):
            transaction.execute(f"CREATE (:Item {{id: {value}}})")

    cursor = database.query("MATCH (i:Item) RETURN i.id AS id ORDER BY id DESC").cursor(
        batch_size=1
    )
    assert next(cursor) == (23,)
    assert tuple(spill_root.iterdir())

    cursor.close()
    database.close()

    assert tuple(spill_root.iterdir()) == ()


def test_cursor_close_removes_a_partially_consumed_distinct_spill(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    factory = LocalQuerySpillFactory
    monkeypatch.setattr(
        "okto_grafx.api.assembly.LocalQuerySpillFactory",
        lambda: factory(str(spill_root)),
    )
    database = okto_grafx.connect(
        tmp_path / "database", query_memory_budget_bytes=1_024
    )
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Item(id INT64, bucket INT64, PRIMARY KEY(id))"
        )
        for value in range(24):
            transaction.execute(f"CREATE (:Item {{id: {value}, bucket: {value % 6}}})")

    cursor = database.query("MATCH (i:Item) RETURN DISTINCT i.bucket AS bucket").cursor(
        batch_size=1
    )
    assert next(cursor) == (0,)
    assert tuple(spill_root.iterdir())

    cursor.close()
    database.close()

    assert tuple(spill_root.iterdir()) == ()


def test_collect_refuses_before_its_single_result_grows_past_the_budget(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    factory = LocalQuerySpillFactory
    monkeypatch.setattr(
        "okto_grafx.api.assembly.LocalQuerySpillFactory",
        lambda: factory(str(spill_root)),
    )
    database = okto_grafx.connect(
        tmp_path / "database", query_memory_budget_bytes=1_024
    )
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Item(id INT64, text STRING, PRIMARY KEY(id))"
        )
        for value in range(20):
            transaction.execute(
                f"CREATE (:Item {{id: {value}, text: '{'x' * 80}{value}'}})"
            )

    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        database.execute("MATCH (i:Item) RETURN collect(i.text) AS values")
    database.close()

    assert raised.value.details["field"] == "query_memory_budget_bytes"
    assert tuple(spill_root.iterdir()) == ()


@pytest.mark.parametrize("field", ["max_result_rows", "max_intermediate_rows"])
def test_row_budgets_remain_authoritative_when_spill_is_enabled(field: str) -> None:
    options = {field: 2, "query_memory_budget_bytes": 1_024}
    stack = build_query_stack(**options)
    for row in _ROWS[:6]:
        stack.insert("Person", row[0], row)

    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        _run(stack, "MATCH (p:Person) RETURN p.id ORDER BY p.id")

    assert raised.value.details["field"] == field


def test_distinct_aggregate_cleanup_never_replaces_the_primary_failure() -> None:
    stack = build_query_stack()
    planned = stack.engine.planned(
        stack.engine.parse("MATCH (p:Person) RETURN count(DISTINCT p.age) AS ages")
    )
    aggregate = next(
        node for node in plan_nodes(planned.root) if isinstance(node, AggregateRows)
    )

    class QuietSorter:
        def append(self, key: bytes, payload: bytes) -> None:
            del key, payload

        def records(self):
            return iter(())

        def close(self) -> None:
            return None

    class Workspace:
        def __init__(self) -> None:
            self.retained = 0

        def sorter(self, comparator):
            del comparator
            return QuietSorter()

        def reserve(self, amount: int, *, reason: str) -> None:
            del reason
            self.retained += amount

        def release(self, amount: int) -> None:
            self.retained -= amount

        def close(self) -> None:
            return None

    class PrimaryAndCloseFailure(QuietSorter):
        def records(self):
            def failing():
                raise ValueError("primary-record-failure")
                yield b"", b""  # pragma: no cover

            return failing()

        def close(self) -> None:
            raise RuntimeError("secondary-close-failure")

    workspace = Workspace()
    state = _SpilledAggregateState(aggregate, (), 0, workspace)  # type: ignore[arg-type]
    state._distinct = PrimaryAndCloseFailure()  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="primary-record-failure") as raised:
            state.finish()
        assert any(
            "secondary-close-failure" in note
            for note in getattr(raised.value, "__notes__", ())
        )
    finally:
        state.close()
    assert workspace.retained == 0
