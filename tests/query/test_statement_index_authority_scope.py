"""Structural checks for statement-local index authority projection."""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.query.ast import Query
from okto_grafx.domain.query.parser import parse
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.query_engine import _closed_statement_tables

from .conftest import build_catalog
from .stack import build_query_stack


def _names(text: str) -> tuple[str, ...] | None:
    tables = _closed_statement_tables(parse(text), build_catalog())
    return None if tables is None else tuple(table.name for table in tables)


def test_closed_footprint_covers_pulse_node_and_relationship_inserts() -> None:
    assert _names("CREATE (n:Person {id: $id}) RETURN n.id") == ("Person",)
    assert _names(
        "MATCH (source:Chunk {id: $source}), (target:Doc {id: $target}) "
        "CREATE (source)-[:BELONGS_TO {weight: $weight}]->(target) "
        "RETURN source.id, target.id"
    ) == ("Chunk", "Doc", "BELONGS_TO")


def test_unclosed_or_noncanonical_statement_keeps_global_fallback() -> None:
    assert _names("MATCH (n) RETURN n") is None
    assert _names("MATCH (a:Person)-[r]->(b) RETURN r") is None

    class QuerySubclass(Query):
        __slots__ = ()

    assert _closed_statement_tables(QuerySubclass(), build_catalog()) is None


def test_catalog_corruption_is_not_converted_into_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = build_catalog()

    def corrupt(_catalog: Catalog, _name: str) -> object:
        raise GrafxCorruptionDetected("stored catalog is corrupt", field="catalog")

    monkeypatch.setattr(Catalog, "table", corrupt)
    with pytest.raises(GrafxCorruptionDetected):
        _closed_statement_tables(parse("MATCH (n:Person) RETURN n.id"), catalog)


def test_index_manager_subclass_keeps_its_global_authority_policy() -> None:
    stack = build_query_stack()
    calls = 0

    class PolicyManager(IndexManager):
        def _statement_indexes(
            self, *, txn: object | None = None, catalog: object | None = None
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            nonlocal calls
            del txn, catalog
            calls += 1
            raise GrafxIndexError("custom authority refusal", field="index_authority")

    stack.engine._indexes = PolicyManager(stack.pool, stack.heap, stack.metrics)
    with pytest.raises(GrafxIndexError, match="custom authority refusal"):
        stack.engine._statement_index_authority(
            stack.catalog_store.catalog,
            txn=stack.transaction(),
            statement=parse("MATCH (p:Person) RETURN p.id"),
        )
    assert calls == 1


@pytest.mark.parametrize("catalog_v2", [False, True], ids=("v1", "v2"))
def test_query_execution_never_enumerates_unrelated_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_v2: bool,
) -> None:
    """Node and edge writes remain executable when the global projection is unreachable."""
    options = {"automatic_index_expected_cardinality": 1_000} if catalog_v2 else {}
    with okto_grafx.connect(tmp_path / f"scope-{catalog_v2}", page_size=512, **options) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE Unrelated(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE E(FROM A TO B, weight INT64)")
        with db.begin("write") as txn:
            txn.execute("CREATE (:B {id: 2})")

        calls: list[str | None] = []
        original = IndexManager.active_indexes_for

        def counted(
            manager: IndexManager,
            table_id: int,
            *,
            table_name: str | None = None,
            table: object | None = None,
            txn: object | None = None,
            catalog: object | None = None,
        ) -> tuple[object, ...]:
            calls.append(table_name)
            return original(
                manager,
                table_id,
                table_name=table_name,
                table=table,
                txn=txn,
                catalog=catalog,
            )

        def refuse_global(
            _manager: IndexManager,
            *,
            txn: object | None = None,
            catalog: object | None = None,
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            del txn, catalog
            raise AssertionError("statement enumerated every registered index")

        with db.begin("write") as txn:
            with monkeypatch.context() as patch:
                patch.setattr(IndexManager, "active_indexes_for", counted)
                patch.setattr(IndexManager, "_statement_indexes", refuse_global)
                result = txn.execute("CREATE (a:A {id: 1}) RETURN a.id")
            assert result.rows == ((1,),)
        assert calls == ["A"]

        calls.clear()
        with db.begin("write") as txn:
            with monkeypatch.context() as patch:
                patch.setattr(IndexManager, "active_indexes_for", counted)
                patch.setattr(IndexManager, "_statement_indexes", refuse_global)
                result = txn.execute(
                    "MATCH (a:A {id: 1}), (b:B {id: 2}) "
                    "CREATE (a)-[:E {weight: 3}]->(b) RETURN a.id, b.id"
                )
            assert result.rows == ((1, 2),)
        assert calls == ["A", "B", "E"]
        assert "Unrelated" not in calls


@pytest.mark.parametrize("catalog_v2", [False, True], ids=("v1", "v2"))
def test_pulse_style_commit_never_enumerates_the_global_index_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_v2: bool,
) -> None:
    """Statement planning and the complete durable commit stay table-local."""
    options = {"automatic_index_expected_cardinality": 1_000} if catalog_v2 else {}
    path = tmp_path / f"commit-scope-{catalog_v2}"
    with okto_grafx.connect(path, page_size=512, **options) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            for table_id in range(12):
                txn.execute(
                    f"CREATE NODE TABLE Unrelated{table_id}(id INT64, PRIMARY KEY(id))"
                )

        def refuse_global(
            _manager: IndexManager,
            *,
            txn: object | None = None,
            catalog: object | None = None,
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            del txn, catalog
            raise AssertionError("durable commit enumerated every registered index")

        def refuse_catalog_tables(_catalog: Catalog) -> tuple[object, ...]:
            raise AssertionError("durable commit enumerated every catalog table")

        def refuse_catalog_indexes(
            _manager: IndexManager, catalog: object | None = None
        ) -> tuple[object, ...] | None:
            del catalog
            raise AssertionError("durable commit projected every catalog index")

        with monkeypatch.context() as patch:
            patch.setattr(IndexManager, "_statement_indexes", refuse_global)
            patch.setattr(IndexManager, "_catalog_active_definitions", refuse_catalog_indexes)
            patch.setattr(Catalog, "tables", refuse_catalog_tables)
            with db.begin("write") as txn:
                result = txn.execute("CREATE (p:Person {id: 7}) RETURN p.id")
                assert result.rows == ((7,),)

    with okto_grafx.connect(path, page_size=512, **options) as reopened:
        assert reopened.execute("MATCH (p:Person {id: 7}) RETURN p.id").rows == ((7,),)


@pytest.mark.parametrize("catalog_v2", [False, True], ids=("v1", "v2"))
def test_schema_only_commit_uses_its_observed_indexes_without_a_global_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_v2: bool,
) -> None:
    options = {"automatic_index_expected_cardinality": 1_000} if catalog_v2 else {}
    path = tmp_path / f"schema-commit-scope-{catalog_v2}"
    with okto_grafx.connect(path, page_size=512, **options) as db:
        def refuse_global(
            _manager: IndexManager,
            *,
            txn: object | None = None,
            catalog: object | None = None,
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            del txn, catalog
            raise AssertionError("schema commit enumerated every registered index")

        txn = db.begin("write")
        txn.execute("CREATE NODE TABLE Fresh(id INT64, PRIMARY KEY(id))")
        with monkeypatch.context() as patch:
            patch.setattr(IndexManager, "_statement_indexes", refuse_global)
            txn.commit()

    with okto_grafx.connect(path, page_size=512, **options) as reopened:
        assert reopened.catalog.catalog.table("Fresh").primary_key == "id"
