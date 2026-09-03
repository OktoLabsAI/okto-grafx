"""Differential and bounded-retention tests for ORDER BY with a result window."""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import INT64_MAX
from okto_grafx.domain.query.ast import Literal, Parameter, SortItem, Variable
from okto_grafx.domain.query.plan import (
    LimitRows,
    PlanNode,
    ProduceResults,
    SingleRow,
    SkipRows,
    SortRows,
    validate_plan,
)
from okto_grafx.engine import query_engine
from okto_grafx.engine.query_engine import (
    QueryEngine,
    QueryResult,
    _Context,
    _Row,
    _sort_rows,
)
from tests.query.conftest import find_operator, plan_text
from tests.query.stack import QueryStack, build_query_stack


ROWS: tuple[tuple[int, str, int | None, str | None], ...] = (
    (1, "alpha", 10, "x"),
    (2, "bravo", None, "x"),
    (3, "charlie", 30, None),
    (4, "delta", 20, "y"),
    (5, "echo", 20, "y"),
    (6, "foxtrot", 30, None),
    (7, "golf", 10, "x"),
    (8, "hotel", None, "z"),
)


@pytest.fixture
def stack() -> QueryStack:
    """Return a stack with ties and nulls on each sort-key position."""
    built = build_query_stack()
    for identity, name, age, city in ROWS:
        built.insert("Person", identity, (identity, name, age, city), csn=1)
    return built


def _run(
    stack: QueryStack, text: str, parameters: dict[str, object] | None = None
) -> QueryResult:
    """Execute against a snapshot containing every fixture row."""
    return stack.engine.execute(text, stack.transaction(read_lsn=1000), parameters)


def _assert_window_matches_full_sort(
    stack: QueryStack,
    ordered: str,
    *,
    skip: int,
    limit: int,
) -> None:
    """Compare the bounded physical sort with the canonical full-sort-and-slice path."""
    complete = _run(stack, ordered).rows
    bounded = _run(
        stack,
        f"{ordered} SKIP $top_n_skip LIMIT $top_n_limit",
        {"top_n_skip": skip, "top_n_limit": limit},
    ).rows
    assert bounded == complete[skip : skip + limit]


def test_the_plan_marks_only_an_order_with_limit_as_physically_bounded() -> None:
    bounded = find_operator(
        plan_text(
            "MATCH (p:Person) RETURN p.id ORDER BY p.id SKIP $start LIMIT $count"
        ).root,
        "SortRows",
    )
    assert isinstance(bounded, SortRows)
    assert bounded.retained_skip == Parameter(name="start")
    assert bounded.retained_limit == Parameter(name="count")
    assert bounded.details()["retains"] == "$start + $count"

    unbounded = find_operator(
        plan_text("MATCH (p:Person) RETURN p.id ORDER BY p.id SKIP 2").root,
        "SortRows",
    )
    assert isinstance(unbounded, SortRows)
    assert unbounded.retained_skip is None
    assert unbounded.retained_limit is None
    assert "retains" not in unbounded.details()


@pytest.mark.parametrize(
    ("ordered", "skip", "limit"),
    [
        pytest.param(
            "MATCH (p:Person) RETURN p.id AS identity, p.city AS city, p.age AS age "
            "ORDER BY city, age DESC, identity DESC",
            1,
            4,
            id="aliases-multi-key-directions-and-nulls",
        ),
        pytest.param(
            "MATCH (p:Person) RETURN DISTINCT p.city AS city ORDER BY city DESC",
            1,
            2,
            id="distinct-before-order",
        ),
        pytest.param(
            "MATCH (p:Person) RETURN p.city AS city, count(*) AS total "
            "ORDER BY total DESC, city",
            1,
            2,
            id="aggregation-before-order",
        ),
        pytest.param(
            "MATCH (p:Person) RETURN p.id, p.city ORDER BY p.city",
            2,
            3,
            id="stable-ties",
        ),
    ],
)
def test_top_n_is_differentially_equal_to_full_stable_sort_and_slice(
    stack: QueryStack, ordered: str, skip: int, limit: int
) -> None:
    _assert_window_matches_full_sort(stack, ordered, skip=skip, limit=limit)


def test_top_n_preserves_the_total_order_across_mixed_runtime_kinds() -> None:
    """A collaborator row of heterogeneous values still follows the executor's total order."""
    child = SingleRow()
    rows = tuple(
        _Row(bindings={}, columns={"identity": identity, "value": value})
        for identity, value in enumerate(
            ("text", None, 7, b"bytes", False, 3.5), start=1
        )
    )

    class Rows:
        def _rows(self, node: PlanNode, context: _Context) -> Iterator[_Row]:
            assert node is child
            del context
            yield from rows

    keys = (
        SortItem(expression=Variable(name="value"), descending=True),
        SortItem(expression=Variable(name="identity")),
    )
    context = cast(_Context, SimpleNamespace(parameters={}))
    engine = cast(QueryEngine, Rows())
    complete = tuple(_sort_rows(engine, SortRows(child=child, keys=keys), context))
    retained = tuple(
        _sort_rows(
            engine,
            SortRows(
                child=child,
                keys=keys,
                retained_skip=Literal(value=1),
                retained_limit=Literal(value=3),
            ),
            context,
        )
    )
    assert retained[1:] == complete[1:4]


def test_top_n_handles_a_window_at_least_as_large_as_the_input(
    stack: QueryStack,
) -> None:
    _assert_window_matches_full_sort(
        stack,
        "MATCH (p:Person) RETURN p.id ORDER BY p.id DESC",
        skip=0,
        limit=len(ROWS) + 100,
    )


def test_zero_limit_keeps_the_existing_empty_window_semantics(
    stack: QueryStack,
) -> None:
    assert (
        _run(
            stack,
            "MATCH (p:Person) RETURN p.id ORDER BY p.id SKIP $start LIMIT $count",
            {"start": 0, "count": 0},
        ).rows
        == ()
    )


def test_retention_addition_does_not_introduce_query_integer_overflow(
    stack: QueryStack,
) -> None:
    """SKIP and LIMIT remain separate valid values even when their physical sum exceeds INT64."""
    assert (
        _run(
            stack,
            "MATCH (p:Person) RETURN p.id ORDER BY p.id SKIP $start LIMIT $count",
            {"start": INT64_MAX, "count": INT64_MAX},
        ).rows
        == ()
    )


def test_top_n_never_retains_more_than_skip_plus_limit(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The physical heap is bounded by K even though every child row is consumed."""
    real_push = heapq.heappush
    real_replace = heapq.heapreplace
    observed_sizes: list[int] = []

    def recording_push(heap: list[object], item: object) -> None:
        real_push(heap, item)
        observed_sizes.append(len(heap))

    def recording_replace(heap: list[object], item: object) -> object:
        replaced = real_replace(heap, item)
        observed_sizes.append(len(heap))
        return replaced

    monkeypatch.setattr(query_engine, "heappush", recording_push)
    monkeypatch.setattr(query_engine, "heapreplace", recording_replace)

    found = _run(
        stack,
        "MATCH (p:Person) RETURN p.id ORDER BY p.id DESC SKIP 2 LIMIT 3",
    )
    assert found.rows == ((6,), (5,), (4,))
    assert observed_sizes
    assert max(observed_sizes) == 5


def test_an_unbounded_sort_does_not_use_the_top_n_heap(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDER BY without LIMIT keeps the canonical stable-sort implementation."""

    def unexpected(*_args: object) -> None:
        raise AssertionError("an unbounded sort entered the top-N heap")

    monkeypatch.setattr(query_engine, "heappush", unexpected)
    assert len(_run(stack, "MATCH (p:Person) RETURN p.id ORDER BY p.id").rows) == len(
        ROWS
    )


def test_literal_retention_is_visible_in_the_plan() -> None:
    sort = find_operator(
        plan_text("MATCH (p:Person) RETURN p.id ORDER BY p.id LIMIT 3").root,
        "SortRows",
    )
    assert isinstance(sort, SortRows)
    assert sort.retained_limit == Literal(value=3)
    assert sort.retained_skip is None
    assert sort.details()["retains"] == "3"


def test_a_physical_retention_without_its_exact_window_is_refused() -> None:
    """A forged optimization hint cannot silently truncate a public plan."""
    count = Literal(value=3)
    bounded = SortRows(
        child=SingleRow(),
        keys=(SortItem(expression=Literal(value=1)),),
        retained_limit=count,
    )
    with pytest.raises(GrafxPlanError) as missing:
        validate_plan(ProduceResults(child=bounded))
    assert missing.value.details["reason"] == "unmatched_retention"

    wrong = ProduceResults(
        child=LimitRows(child=bounded, count=Literal(value=2)),
    )
    with pytest.raises(GrafxPlanError) as mismatched:
        validate_plan(wrong)
    assert mismatched.value.details["reason"] == "unmatched_retention"


def test_an_exact_skip_and_limit_window_validates() -> None:
    skip = Literal(value=2)
    limit = Literal(value=3)
    bounded = SortRows(
        child=SingleRow(),
        keys=(SortItem(expression=Literal(value=1)),),
        retained_skip=skip,
        retained_limit=limit,
    )
    root = ProduceResults(
        child=LimitRows(child=SkipRows(child=bounded, count=skip), count=limit),
    )
    assert validate_plan(root) is root
