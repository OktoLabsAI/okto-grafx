"""Closed planned column projection for KG-style node scans (KGRUN-1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine import query_engine as query_engine_module
from okto_grafx.engine.heap_store import HeapStore


@pytest.fixture
def database(tmp_path: Path) -> Iterator[okto_grafx.Database]:
    handle = okto_grafx.connect(tmp_path / "db")
    with handle.begin("write") as transaction:
        transaction.execute(
            "CREATE VECTOR SPACE emb {dimension: 4, metric: 'cosine'}"
        )
        for name in ("A", "B"):
            transaction.execute(
                f"CREATE NODE TABLE {name}("
                "id STRING, title STRING, payload STRING, embedding VECTOR(emb), "
                "PRIMARY KEY(id))"
            )
    with handle.begin("write") as transaction:
        transaction.execute(
            "CREATE (:A {id: 'a', title: 'alpha', payload: 'large-a', "
            "embedding: [1.0, 0.0, 0.0, 0.0]})"
        )
        transaction.execute(
            "CREATE (:B {id: 'b', title: 'beta', payload: 'large-b', "
            "embedding: [0.0, 1.0, 0.0, 0.0]})"
        )
    try:
        yield handle
    finally:
        handle.close()


def test_polymorphic_kg_scan_retains_only_properties_consumed_by_the_plan(
    database: okto_grafx.Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, frozenset[int]]] = []
    original = HeapStore.scan_projected

    def counted(
        self: HeapStore,
        table: TableDef,
        snapshot: Snapshot,
        materialized_positions: frozenset[int],
    ) -> Iterator[tuple[RecordRef, HeapVersion]]:
        if self is database._heap:  # noqa: SLF001
            observed.append((table.table_id, materialized_positions))
        yield from original(self, table, snapshot, materialized_positions)

    monkeypatch.setattr(HeapStore, "scan_projected", counted)
    result = database.execute(
        "MATCH (n) WHERE label(n) = 'A' AND n.title <> '' "
        "RETURN n.id, label(n) AS kind, n.title ORDER BY n.id LIMIT 10"
    )

    assert result.rows == (("a", "A", "alpha"),)
    tables = tuple(
        table
        for table in database._catalog.catalog.tables()  # noqa: SLF001
        if table.kind == "node"
    )
    assert len(observed) == 2
    assert dict(observed) == {
        table.table_id: frozenset(
            (table.column_positions["id"], table.column_positions["title"])
        )
        for table in tables
    }


def test_a_whole_entity_projection_stays_on_the_canonical_full_row_scan(
    database: okto_grafx.Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> object:
        pytest.fail("a whole entity may not leave a projected-row scan")

    monkeypatch.setattr(HeapStore, "scan_projected", unexpected)
    rows = database.execute("MATCH (n) WHERE label(n) = 'A' RETURN n").rows

    assert len(rows) == 1


def test_a_bad_internal_column_proof_fails_closed_instead_of_answering_false(
    database: okto_grafx.Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = database._catalog.catalog.table("A")  # noqa: SLF001
    monkeypatch.setattr(
        query_engine_module,
        "_closed_node_scan_projections",
        lambda root: {id(next(node for node in root.walk() if node.label == "NodeScan")): {table.table_id: frozenset()}},
    )

    with pytest.raises(GrafxPlanError) as caught:
        database.execute("MATCH (n:A) RETURN n.title")
    assert caught.value.details["field"] == "projection"
