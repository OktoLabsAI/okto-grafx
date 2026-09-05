"""A public result clones its plan only when someone reads it -- and never shares a node.

Publishing a result used to rebuild the whole operator tree on every ``execute`` even though the
memoised clone recipe already existed: a Pulse import that never looks at ``result.plan`` paid for
a tree per row.  The public ``plan`` is now a sealed door over the result's own slot.  A root the
exact engine proved it owns carries the compiled recipe; the first read runs it once, stores the
independent tree in that result alone and drops the door.  These tests pin the contract by COUNT
of recipe runs, prove that two results stay fully independent under adversarial mutation, keep the
hostile eager rebuild for every root the exact engine did not seal, and keep the door out of the
public constructor.
"""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.plan import PlanNode, ProduceResults, SingleRow
from okto_grafx.engine import public_views
from okto_grafx.engine.query_engine import (
    _QUERY_RESULT_PLAN_SLOT,
    QueryEngine,
    QueryResult,
    _OwnedPlanDoor,
    _owned_query_result,
)

STATEMENT = "RETURN 1 AS value"


class _RecipeSpy:
    """Count compilations of the owned clone recipe and every run of the recipes it produced."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.compiled = 0
        self.runs = 0
        original = public_views._query_owned_plan_clone_factory
        spy = self

        def counted_factory(value: PlanNode):  # type: ignore[no-untyped-def]
            spy.compiled += 1
            recipe = original(value)

            def counted_recipe() -> PlanNode:
                spy.runs += 1
                return recipe()

            return counted_recipe

        monkeypatch.setattr(
            public_views, "_query_owned_plan_clone_factory", counted_factory
        )


def _stored_plan(result: QueryResult) -> object:
    """Read the physical slot without going through the door."""
    return _QUERY_RESULT_PLAN_SLOT.__get__(result, QueryResult)


def test_repeated_executes_never_run_the_clone_recipe_until_a_plan_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RecipeSpy(monkeypatch)
    with connect(":memory:") as database:
        results = [database.execute(STATEMENT) for _ in range(5)]

    assert [result.rows for result in results] == [((1,),)] * 5
    assert spy.compiled == 1, "the root is compiled once and then served from the memo"
    assert spy.runs == 0, "no result read its plan, so no tree was cloned"
    assert all(type(_stored_plan(result)) is _OwnedPlanDoor for result in results)

    first = results[0].plan
    assert spy.runs == 1
    assert type(first) is ProduceResults
    assert results[0].plan is first, "a result materialises its tree exactly once"
    assert spy.runs == 1
    assert type(_stored_plan(results[0])) is not _OwnedPlanDoor, "the door is dropped"
    assert type(_stored_plan(results[1])) is _OwnedPlanDoor, "other results keep theirs"


def test_two_results_stay_fully_independent_under_adversarial_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RecipeSpy(monkeypatch)
    with connect(":memory:") as database:
        first, second, third = (database.execute(STATEMENT) for _ in range(3))

    first_plan = first.plan
    second_plan = second.plan
    assert spy.runs == 2
    assert first_plan == second_plan
    assert first_plan is not second_plan
    assert all(
        left is not right
        for left, right in zip(first_plan.walk(), second_plan.walk(), strict=True)
    )

    # Adversarial mutation of one materialised tree: bypass the frozen dataclass on the root and
    # on an inner node.  The sibling that already exists and the sibling that materialises later
    # must both still see the pristine tree.
    object.__setattr__(first_plan, "columns", ("changed",))
    object.__setattr__(first_plan.child, "items", ())
    assert second_plan.columns == ("value",)
    assert second_plan.child.items != ()
    third_plan = third.plan
    assert spy.runs == 3
    assert third_plan.columns == ("value",)
    assert third_plan.child.items == second_plan.child.items
    assert third_plan == second_plan
    assert first_plan != second_plan


def test_concurrent_reads_materialise_one_stable_tree_for_the_result() -> None:
    entered = Event()
    second_clone = Event()
    release = Event()
    counter_lock = Lock()
    runs = 0

    def clone() -> PlanNode:
        nonlocal runs
        with counter_lock:
            runs += 1
            if runs == 2:
                second_clone.set()
        entered.set()
        assert release.wait(timeout=2)
        return ProduceResults(child=SingleRow(), columns=("value",))

    result = _owned_query_result(plan=_OwnedPlanDoor(clone))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: result.plan)
        assert entered.wait(timeout=2)
        second = executor.submit(lambda: result.plan)
        assert not second_clone.wait(timeout=0.1), "the recipe ran concurrently twice"
        release.set()
        first_plan = first.result(timeout=2)
        second_plan = second.result(timeout=2)

    assert runs == 1
    assert first_plan is second_plan
    assert result.plan is first_plan


def test_equality_repr_and_replace_materialise_each_side_privately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RecipeSpy(monkeypatch)
    with connect(":memory:") as database:
        first = database.execute(STATEMENT)
        second = database.execute(STATEMENT)

    assert first == second
    assert spy.runs == 2, "dataclass equality read both plans, once each"
    assert "plan=" in repr(first)
    replaced = dataclasses.replace(first, rows=())
    assert replaced.rows == ()
    assert replaced.plan == first.plan
    assert replaced.plan is first.plan, (
        "replace() passes the already materialised tree through"
    )
    assert [f.name for f in dataclasses.fields(QueryResult)] == [
        "columns",
        "rows",
        "plan",
        "statistics",
    ]


def test_the_public_constructor_refuses_a_sealed_plan_door() -> None:
    raw = ProduceResults(child=SingleRow(), columns=("value",))
    with pytest.raises(GrafxPlanError) as refused:
        QueryResult(columns=("value",), rows=((1,),), plan=_OwnedPlanDoor(lambda: raw))  # type: ignore[arg-type]
    assert "sealed plan door is engine-private" in str(refused.value)

    # The frozen dataclass still refuses user assignment of the field.
    result = QueryResult(columns=("value",), rows=((1,),), plan=raw)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.plan = None  # type: ignore[misc]
    assert result.plan is raw


def test_a_root_the_exact_engine_did_not_seal_keeps_the_eager_hostile_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = ProduceResults(child=SingleRow(), columns=("value",))
    original_execute = QueryEngine.execute

    def foreign_root(self: QueryEngine, text: str, txn: object, parameters: object):  # type: ignore[no-untyped-def]
        # An exact engine answering with a root it does not own: the ownership proof fails.
        return QueryResult(columns=("value",), rows=((1,),), plan=raw)

    monkeypatch.setattr(QueryEngine, "execute", foreign_root)
    spy = _RecipeSpy(monkeypatch)
    with connect(":memory:") as database:
        observed = database.execute(STATEMENT)

    assert spy.compiled == 0, "an unproven root never reaches the trusted clone recipe"
    stored = _stored_plan(observed)
    assert type(stored) is ProduceResults, "the hostile path detached the tree eagerly"
    assert stored is not raw
    assert observed.plan == raw
    monkeypatch.setattr(QueryEngine, "execute", original_execute)


def test_a_malformed_owned_root_still_refuses_at_execute_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compilation stays eager: the door defers the clone, never the validation."""

    def malformed(value: object) -> PlanNode:
        raise ValueError("compile boom")

    monkeypatch.setattr(public_views, "_query_plan_rebuild", malformed)
    with connect(":memory:") as database:
        with pytest.raises(GrafxPlanError) as refused:
            database.execute(STATEMENT)
    assert "malformed plan field (ValueError)" in str(refused.value)


def test_explain_still_returns_an_eager_independent_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _RecipeSpy(monkeypatch)
    with connect(":memory:") as database:
        first = database.explain(STATEMENT)
        second = database.explain(STATEMENT)
        result = database.execute(STATEMENT)

    assert spy.compiled == 1
    assert spy.runs == 2, "explain has no result to hide a door in; it clones per call"
    assert first == second and first is not second
    assert type(_stored_plan(result)) is _OwnedPlanDoor
    assert result.plan == first
    assert spy.runs == 3
