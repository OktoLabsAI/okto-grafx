"""Regression for layout-sized query cycles and bounded optional retention."""

from concurrent.futures import ThreadPoolExecutor

import pytest

import okto_grafx.engine.query_engine as query_module
from tests.query.stack import build_query_stack


def test_160_statement_cycle_does_not_rebuild_every_plan(monkeypatch):
    stack = build_query_stack()
    builds = []
    original = query_module.build_plan

    def counted(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(query_module, "build_plan", counted)
    txn = stack.transaction()
    for repeat in range(2):
        for position in range(160):
            result = stack.engine.execute(
                f"RETURN $value AS field_{position}", txn, {"value": repeat}
            )
            assert result.rows == ((repeat,),)
    assert len(builds) == 160


def test_large_statement_executes_without_retaining_ast_plan_or_authority():
    stack = build_query_stack()
    text = "RETURN 7" + " " * query_module._PREPARED_MAX_TEXT_CHARS
    assert stack.engine.execute(text, stack.transaction()).rows == ((7,),)
    assert not stack.engine._parse_cache
    assert not stack.engine._plan_cache
    assert not stack.engine._statement_authority_memo
    assert not stack.engine._owned_prepared_plans


def test_plan_byte_admission_is_optional_and_eviction_releases_ownership(monkeypatch):
    stack = build_query_stack()
    txn = stack.transaction()
    first = stack.engine.execute("RETURN 0", txn).plan
    charge = stack.engine._plan_cache_bytes
    assert charge > 0
    monkeypatch.setattr(query_module, "_PLAN_CACHE_MAX_BYTES", charge * 2)
    for number in range(1, 10):
        assert stack.engine.execute(f"RETURN {number}", txn).rows == ((number,),)
        assert stack.engine._plan_cache_bytes <= charge * 2
    assert not stack.engine._owns_prepared_plan(first)
    assert len(stack.engine._owned_prepared_plans) == len(stack.engine._plan_cache)
    monkeypatch.setattr(query_module, "_PLAN_CACHE_MAX_BYTES", 1)
    retained = tuple(stack.engine._plan_cache)
    assert stack.engine.execute("RETURN 99", txn).rows == ((99,),)
    assert tuple(stack.engine._plan_cache) == retained


@pytest.mark.timeout(15, method="thread")
def test_concurrent_preparation_preserves_single_owned_winner_and_accounting():
    stack = build_query_stack()
    with ThreadPoolExecutor(max_workers=4) as workers:
        plans = list(workers.map(stack.engine.explain, ["RETURN 1"] * 40))
    assert all(plan is plans[0] for plan in plans)
    assert len(stack.engine._plan_cache) == 1
    assert stack.engine._owned_prepared_plans[id(plans[0])][1] == 1
    assert stack.engine._plan_cache_bytes == sum(stack.engine._plan_cache_charges.values())
