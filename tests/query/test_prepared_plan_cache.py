"""Bounded parse/prepared-plan reuse without weakening catalog or index authority."""

from __future__ import annotations

import okto_grafx.engine.query_engine as query_module
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.plan import IndexSeek, NodeScan
from tests.query.stack import build_query_stack


def test_repeated_exact_text_reuses_parse_analysis_and_plan(
    monkeypatch,
) -> None:
    stack = build_query_stack()
    counts = {"parse": 0, "analyze": 0, "plan": 0}
    original_parse = query_module.parse_text
    original_analyze = query_module.analyze
    original_plan = query_module.build_plan

    def counted_parse(text):
        counts["parse"] += 1
        return original_parse(text)

    def counted_analyze(statement):
        counts["analyze"] += 1
        return original_analyze(statement)

    def counted_plan(*args, **kwargs):
        counts["plan"] += 1
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(query_module, "parse_text", counted_parse)
    monkeypatch.setattr(query_module, "analyze", counted_analyze)
    monkeypatch.setattr(query_module, "build_plan", counted_plan)
    transaction = stack.transaction()
    text = "RETURN 1 AS value"

    first = stack.engine.execute(text, transaction)
    second = stack.engine.execute(text, transaction)

    assert counts == {"parse": 1, "analyze": 1, "plan": 1}
    assert first.plan is second.plan
    assert first.rows == second.rows == ((1,),)


def test_equivalent_committed_catalog_objects_reuse_the_prepared_plan(
    monkeypatch,
) -> None:
    stack = build_query_stack()
    counts = {"analyze": 0, "plan": 0}
    original_analyze = query_module.analyze
    original_plan = query_module.build_plan

    def counted_analyze(statement):
        counts["analyze"] += 1
        return original_analyze(statement)

    def counted_plan(*args, **kwargs):
        counts["plan"] += 1
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(query_module, "analyze", counted_analyze)
    monkeypatch.setattr(query_module, "build_plan", counted_plan)
    text = "MATCH (n:Person) WHERE n.id = 7 RETURN n.id"

    first_catalog = stack.catalog_store.catalog
    first = stack.engine.execute(text, stack.transaction())
    stack.catalog_store.adopt(stack.catalog_store.read_from_pages())
    second_catalog = stack.catalog_store.catalog
    second = stack.engine.execute(text, stack.transaction())

    assert second_catalog is not first_catalog
    assert stack.catalog_store.persisted_image() == first_catalog.serialize()
    assert counts == {"analyze": 1, "plan": 1}
    assert second.plan is first.plan


def test_exact_text_catalog_stale_and_dirty_pictures_split_entries(
    monkeypatch,
) -> None:
    stack = build_query_stack()
    builds = 0
    original_plan = query_module.build_plan

    def counted_plan(*args, **kwargs):
        nonlocal builds
        builds += 1
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(query_module, "build_plan", counted_plan)
    text = "MATCH (n:Person) WHERE n.id = 7 RETURN n.id"
    clean = stack.transaction()
    first = stack.engine.execute(text, clean)
    assert any(isinstance(node, IndexSeek) for node in first.plan.walk())
    assert stack.engine.execute(text, clean).plan is first.plan

    index = stack.indexes.index("person_id")
    index.mark_stale("test cache invalidation", persist=False)
    stale = stack.engine.execute(text, clean)
    assert any(isinstance(node, NodeScan) for node in stale.plan.walk())

    stack.catalog_store.catalog.add_table(
        TableDef(
            table_id=4,
            name="Other",
            kind="node",
            columns=(ColumnDef("id", ValueType.INT64, nullable=False),),
            primary_key="id",
        )
    )
    stack.catalog_store.save()
    after_catalog = stack.engine.execute(text, clean)
    assert after_catalog.plan is not stale.plan

    dirty = stack.transaction()
    clean_dirty_owner = stack.engine.execute(text, dirty)
    stack.engine.execute("CREATE (n:Person {id: 9})", dirty)
    changed_dirty_owner = stack.engine.execute(text, dirty)
    assert clean_dirty_owner.plan is not changed_dirty_owner.plan
    assert any(isinstance(node, NodeScan) for node in changed_dirty_owner.plan.walk())
    assert builds == 5


def test_cache_is_bounded_and_keys_retain_no_runtime_store_or_projection() -> None:
    stack = build_query_stack()
    for position in range(query_module._PARSE_CACHE_MAX_ENTRIES + 1):
        stack.engine.parse(f"RETURN {position} AS value")
    assert len(stack.engine._parse_cache) == query_module._PARSE_CACHE_MAX_ENTRIES
    assert "RETURN 0 AS value" not in stack.engine._parse_cache

    transaction = stack.transaction()
    for position in range(query_module._PLAN_CACHE_MAX_ENTRIES + 1):
        stack.engine.execute(f"RETURN {position} AS value", transaction)
    assert len(stack.engine._plan_cache) == query_module._PLAN_CACHE_MAX_ENTRIES
    for key in stack.engine._plan_cache:
        assert isinstance(key.catalog_image, bytes)
        assert all(type(stale) is bool for _definition, stale in key.index_picture)
        assert not any(
            isinstance(part, query_module._IndexAuthorityProjection)
            for part in (
                key.text,
                key.catalog_image,
                key.index_picture,
                key.dirty_tables,
                key.version,
            )
        )
