"""The exact-type evaluator table answers exactly what the isinstance walk answers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    CaseExpression,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    MapExpression,
    NullCheck,
    Parameter,
    Property,
    Subscript,
    UnaryOperation,
    Variable,
)
from okto_grafx.engine import query_engine as engine_module
from okto_grafx.engine.query_engine import _evaluate, _evaluate_by_kind

QUERIES = [
    ("RETURN 1 AS v", {}),
    ("RETURN $p AS v", {"p": "x"}),
    ("MATCH (n:T) RETURN n.id AS v ORDER BY v", {}),
    ("MATCH (n:T) WHERE n.n IS NOT NULL RETURN n.id AS v ORDER BY v", {}),
    ("MATCH (n:T) WHERE NOT (n.n > 1) RETURN n.id AS v ORDER BY v", {}),
    (
        "MATCH (n:T) WHERE n.n + 1 = 2 OR n.id IN $ids RETURN n.id AS v ORDER BY v",
        {"ids": ["t-0"]},
    ),
    ("RETURN [1, $p, 'a'] AS v", {"p": 2}),
    ("RETURN {a: 1, b: $p} AS v", {"p": "z"}),
    (
        "MATCH (n:T) RETURN CASE WHEN n.n > 1 THEN 'big' ELSE 'small' END AS v ORDER BY n.id",
        {},
    ),
    (
        "MATCH (n:T) RETURN CASE n.n WHEN 0 THEN 'zero' ELSE 'other' END AS v ORDER BY n.id",
        {},
    ),
    ("RETURN [10, 20, 30][2] AS v", {}),
    ("MATCH (n:T) RETURN coalesce(n.n, 0) AS v ORDER BY v", {}),
    ("MATCH (n:T) RETURN label(n) AS v, size([n.id, n.n]) AS s ORDER BY n.id", {}),
    ("MATCH (n:T) WITH n.n AS k, n.id AS i RETURN k * 2 AS v, i ORDER BY i", {}),
]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(str(tmp_path / "db"), page_size=8192)
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE NODE TABLE T(id STRING, n INT64, PRIMARY KEY(id))")
        with handle.begin("write") as txn:
            for position in range(5):
                txn.execute(
                    "CREATE (:T {id: $id, n: $n})",
                    {"id": f"t-{position}", "n": position},
                )
        yield handle
    finally:
        handle.close()


def _rows(database: object, text: str, parameters: dict[str, object]) -> list[tuple]:
    return [tuple(row) for row in database.execute(text, parameters).rows]  # type: ignore[attr-defined]


@pytest.mark.parametrize(("text", "parameters"), QUERIES, ids=[q[0] for q in QUERIES])
def test_every_expression_kind_answers_the_same_through_both_doors(
    database: object,
    text: str,
    parameters: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    through_table = _rows(database, text, parameters)
    monkeypatch.setattr(engine_module, "_EXACT_EVALUATORS", {})
    through_walk = _rows(database, text, parameters)
    assert through_table == through_walk


def test_table_holds_exactly_the_walked_kinds_and_their_walk_functions() -> None:
    table = engine_module._EXACT_EVALUATORS
    assert set(table) == {
        Literal,
        Parameter,
        Variable,
        Property,
        NullCheck,
        UnaryOperation,
        BinaryOperation,
        ListExpression,
        MapExpression,
        CaseExpression,
        Subscript,
        FunctionCall,
    }
    row = SimpleNamespace(computed=None, bindings={})
    context = SimpleNamespace(parameters={"p": 7})
    literal = Literal(value=3)
    parameter = Parameter(name="p")
    assert table[Literal](literal, row, context, None) == 3
    assert _evaluate_by_kind(literal, row, context, None) == 3  # type: ignore[arg-type]
    assert table[Parameter](parameter, row, context, None) == 7
    assert _evaluate_by_kind(parameter, row, context, None) == 7  # type: ignore[arg-type]


def test_subclasses_take_the_walk_and_answer_like_their_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass(frozen=True, slots=True)
    class Verbatim(Literal):
        pass

    @dataclass(frozen=True, slots=True)
    class Named(Parameter):
        pass

    walked: list[type] = []
    original = engine_module._evaluate_by_kind

    def counting(expression, row, context, computed):  # type: ignore[no-untyped-def]
        walked.append(type(expression))
        return original(expression, row, context, computed)

    monkeypatch.setattr(engine_module, "_evaluate_by_kind", counting)
    row = SimpleNamespace(computed=None, bindings={})
    context = SimpleNamespace(parameters={"p": "v"})
    assert Verbatim not in engine_module._EXACT_EVALUATORS
    assert _evaluate(Verbatim(value=5), row, context) == 5  # type: ignore[arg-type]
    assert _evaluate(Named(name="p"), row, context) == "v"  # type: ignore[arg-type]
    assert _evaluate(Literal(value=5), row, context) == 5  # type: ignore[arg-type]
    assert walked == [Verbatim, Named]


def test_missing_parameter_and_unknown_node_refuse_identically() -> None:
    @dataclass(frozen=True, slots=True)
    class Alien(Expression):
        pass

    row = SimpleNamespace(computed=None, bindings={})
    context = SimpleNamespace(parameters={})
    with pytest.raises(GrafxPlanError, match=r"needs the parameter \$q"):
        _evaluate(Parameter(name="q"), row, context)  # type: ignore[arg-type]
    with pytest.raises(GrafxPlanError, match="Alien cannot be evaluated"):
        _evaluate(Alien(), row, context)  # type: ignore[arg-type]
    with pytest.raises(GrafxPlanError, match="Alien cannot be evaluated"):
        _evaluate_by_kind(Alien(), row, context, None)  # type: ignore[arg-type]


def test_computed_values_still_win_over_both_doors() -> None:
    literal = Literal(value=1)
    row = SimpleNamespace(computed={literal: "memoized"}, bindings={})
    context = SimpleNamespace(parameters={})
    assert _evaluate(literal, row, context) == "memoized"  # type: ignore[arg-type]
