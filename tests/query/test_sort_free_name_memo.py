"""READ-4: the names an ORDER BY key reads are proved once per statement, not once per row.

``free_variables`` walks a whole expression tree and its answer depends only on that tree, so
asking it again for every scanned row repeats a plan-constant proof N times.  These tests count
the walk AT ITS PORT -- ``query_engine.free_variables``, the name the sort door calls -- and pin
that the memo changes nothing an alias, an aggregate or a refusal could observe.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    Expression,
    Literal,
    Property,
    Variable,
    free_variables,
)

ROWS = 200


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """An on-disk board with enough rows that a per-row walk is unmistakable in the count."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=4096)
    with handle.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Doc(id STRING, ts INT64, rank INT64, title STRING, "
            "PRIMARY KEY(id))"
        )
    with handle.begin("write") as transaction:
        payload = [
            {
                "i": f"doc-{index:05d}",
                "t": (index * 7919) % ROWS,
                "r": index % 13,
                "x": f"title {index}",
            }
            for index in range(ROWS)
        ]
        transaction.execute(
            "UNWIND $rows AS r CREATE (:Doc {id:r.i, ts:r.t, rank:r.r, title:r.x})",
            {"rows": payload},
        )
    try:
        yield handle
    finally:
        handle.close()


class _Counter:
    """Count every call that reaches the walk, and remember which expressions asked."""

    def __init__(self) -> None:
        self.calls = 0
        self.subjects: list[int] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def counted(expression: Expression) -> tuple[str, ...]:
            self.calls += 1
            self.subjects.append(id(expression))
            return free_variables(expression)

        monkeypatch.setattr(query_engine_module, "free_variables", counted)


def _bypass_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the pre-READ-4 behaviour: ask the walk again for every row."""

    def unmemoised(expression: Expression, context: object) -> tuple[str, ...]:
        return query_engine_module.free_variables(expression)

    monkeypatch.setattr(query_engine_module, "_free_names", unmemoised)


SHAPES = (
    # statement, number of ORDER BY keys that reach the walk
    ("MATCH (n:Doc) RETURN n ORDER BY n.ts DESC LIMIT 10", 1),
    (
        "MATCH (n:Doc) RETURN n.id, n.ts, n.rank ORDER BY n.ts DESC, n.id DESC LIMIT 10",
        2,
    ),
    ("MATCH (n:Doc) RETURN n.id, n.ts ORDER BY n.ts + 0 DESC", 1),
    ("MATCH (n:Doc) RETURN n.id, n.ts ORDER BY n.ts DESC SKIP 5 LIMIT 10", 1),
)


@pytest.mark.parametrize("statement,keys", SHAPES)
def test_the_walk_runs_once_per_sort_item_per_query_not_once_per_row(
    database: object, monkeypatch: pytest.MonkeyPatch, statement: str, keys: int
) -> None:
    """The count is the number of sort items, and it does not move with the row count."""
    counter = _Counter()
    counter.install(monkeypatch)
    rows = database.execute(statement).rows  # type: ignore[attr-defined]
    assert rows
    assert counter.calls == keys, (statement, counter.calls)
    assert len(set(counter.subjects)) == keys


@pytest.mark.parametrize("statement,keys", SHAPES)
def test_without_the_memo_the_same_port_is_called_once_per_scanned_row(
    database: object, monkeypatch: pytest.MonkeyPatch, statement: str, keys: int
) -> None:
    """The counter is discriminating: the canonical door pays the walk on every row."""
    counter = _Counter()
    counter.install(monkeypatch)
    _bypass_memo(monkeypatch)
    rows = database.execute(statement).rows  # type: ignore[attr-defined]
    assert rows
    assert counter.calls == ROWS * keys, (statement, counter.calls)


def test_the_count_is_flat_in_the_number_of_rows(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubling the scanned rows must not move the number of walks."""
    with database.begin("write") as transaction:  # type: ignore[attr-defined]
        payload = [
            {"i": f"more-{index:05d}", "t": index, "r": 0, "x": "x"}
            for index in range(ROWS)
        ]
        transaction.execute(
            "UNWIND $rows AS r CREATE (:Doc {id:r.i, ts:r.t, rank:r.r, title:r.x})",
            {"rows": payload},
        )
    statement = "MATCH (n:Doc) RETURN n.id, n.ts ORDER BY n.ts DESC, n.id DESC LIMIT 10"
    counter = _Counter()
    counter.install(monkeypatch)
    rows = database.execute(statement).rows  # type: ignore[attr-defined]
    assert len(rows) == 10
    assert counter.calls == 2


def test_each_statement_proves_the_names_for_itself(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No memo outlives a statement: the second execution of the same plan proves again."""
    statement = "MATCH (n:Doc) RETURN n.id, n.ts ORDER BY n.ts DESC LIMIT 10"
    counter = _Counter()
    counter.install(monkeypatch)
    for _ in range(3):
        database.execute(statement)  # type: ignore[attr-defined]
    assert counter.calls == 3


def test_an_alias_still_shadows_the_binding_a_compound_sort_key_reads() -> None:
    """Alias precedence, which the memoised set decides, on a multi-row sort."""
    with okto_grafx.connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3] AS n RETURN -n AS n ORDER BY n+1").rows == (
            (-3,),
            (-2,),
            (-1,),
        )
        assert db.execute("UNWIND [1,2,3] AS n RETURN n+10 AS x ORDER BY -x").rows == (
            (13,),
            (12,),
            (11,),
        )
        # Two keys, only one of them shadowed: the memo must answer per expression.
        assert db.execute(
            "UNWIND [1,2,3] AS n RETURN -n AS n, n AS k ORDER BY n+1, k+1"
        ).rows == ((-3, 3), (-2, 2), (-1, 1))


def test_an_aggregate_survives_a_shadowed_compound_key() -> None:
    """The second walk of the door -- over the computed items -- keeps its aggregate rule."""
    with okto_grafx.connect(":memory:") as db:
        assert db.execute(
            "UNWIND [1,1,2] AS n RETURN n, n+count(*) AS x ORDER BY n"
        ).rows == ((1, 3), (2, 3))


def test_the_memo_is_keyed_by_identity_and_holds_what_it_answered_for() -> None:
    """Two structurally equal expressions are two entries; each entry retains its own object."""
    context = SimpleNamespace(free_name_memos={})
    first = BinaryOperation(
        operator="+", left=Property(subject=Variable(name="n"), key="ts"), right=Literal(value=1)
    )
    twin = BinaryOperation(
        operator="+", left=Property(subject=Variable(name="n"), key="ts"), right=Literal(value=1)
    )
    assert first == twin and first is not twin
    assert query_engine_module._free_names(first, context) == ("n",)
    assert query_engine_module._free_names(twin, context) == ("n",)
    assert len(context.free_name_memos) == 2
    for key, (held, names) in context.free_name_memos.items():
        assert id(held) == key
        assert names == ("n",)
    assert any(held is first for held, _ in context.free_name_memos.values())
    assert any(held is twin for held, _ in context.free_name_memos.values())


class _NotAnExpression:
    """What the walk must refuse to accept as a node."""


class _HostileParent(Expression):
    """An expression whose child is outside the type boundary the walk enforces."""

    def children(self) -> tuple[Expression, ...]:
        return (_NotAnExpression(),)  # type: ignore[return-value]


def test_a_refusal_is_never_memoised_and_is_raised_again() -> None:
    """A walk that refuses must refuse on the next row too; nothing is stored."""
    context = SimpleNamespace(free_name_memos={})
    hostile = _HostileParent()
    for _ in range(2):
        with pytest.raises(GrafxPlanError) as failure:
            query_engine_module._free_names(hostile, context)
        assert failure.value.details["field"] == "depth"
    assert context.free_name_memos == {}
