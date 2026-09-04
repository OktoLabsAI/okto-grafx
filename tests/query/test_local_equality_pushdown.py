"""Local equality filters stop Cartesian work before later node scans."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.query.plan import FilterRows, IndexSeek, NodeScan
from okto_grafx.engine.heap_store import HeapStore


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        yield handle
    finally:
        handle.close()


def _create_schema(database, *, keyed_q: bool = False) -> None:
    primary_key = ", PRIMARY KEY(id)" if keyed_q else ""
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(id INT64, ordinal STRING)")
        txn.execute(f"CREATE NODE TABLE Q(id INT64, ordinal STRING{primary_key})")
        txn.execute("CREATE REL TABLE E(FROM P TO Q, w INT64)")


def _insert_nodes(database, size: int) -> None:
    with database.begin("write") as txn:
        for identity in range(size):
            txn.execute(
                "CREATE (:P {id: $id, ordinal: $ordinal})",
                {"id": identity, "ordinal": f"p{identity}"},
            )
            txn.execute(
                "CREATE (:Q {id: $id, ordinal: $ordinal})",
                {"id": identity, "ordinal": f"q{identity}"},
            )


def _scan(root, variable: str) -> NodeScan:
    found = [
        node
        for node in root.walk()
        if isinstance(node, NodeScan) and node.variable == variable
    ]
    assert len(found) == 1
    return found[0]


def _direct_filter(root, scan: NodeScan) -> FilterRows | None:
    return next(
        (
            node
            for node in root.walk()
            if isinstance(node, FilterRows) and node.child is scan
        ),
        None,
    )


def test_unique_endpoint_equalities_scan_each_table_once(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unique matches cost two scans and 2N rows, not N scans and N squared rows."""

    size = 12
    _create_schema(database)
    _insert_nodes(database, size)
    statement = "MATCH (p:P {id: $p}), (q:Q {id: $q}) CREATE (p)-[:E {w: 1}]->(q)"
    plan = database.explain(statement)
    p_scan = _scan(plan, "p")
    q_scan = _scan(plan, "q")
    assert _direct_filter(plan, p_scan) is not None
    assert _direct_filter(plan, q_scan) is not None

    scans: Counter[str] = Counter()
    original = HeapStore.scan

    def counted(self, table, snapshot):
        if table.name in {"P", "Q"}:
            scans[table.name] += 1
        return original(self, table, snapshot)

    monkeypatch.setattr(HeapStore, "scan", counted)
    with database.begin("write") as txn:
        result = txn.execute(statement, {"p": 3, "q": 7})
        assert scans == {"P": 1, "Q": 1}
        assert result.statistics["rows_scanned"] == 2 * size
        assert result.statistics["relationships_created"] == 1


def test_pushdown_preserves_duplicate_multiplicity_and_scan_order(database) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        for ordinal in ("p1", "p2"):
            txn.execute("CREATE (:P {id: 1, ordinal: $ordinal})", {"ordinal": ordinal})
        for ordinal in ("q1", "q2"):
            txn.execute("CREATE (:Q {id: 9, ordinal: $ordinal})", {"ordinal": ordinal})

    with database.begin("write") as txn:
        result = txn.execute(
            "MATCH (p:P {id: 1}), (q:Q {id: 9}) "
            "CREATE (p)-[:E {w: 1}]->(q) RETURN p.ordinal, q.ordinal"
        )

    assert result.rows == (
        ("p1", "q1"),
        ("p1", "q2"),
        ("p2", "q1"),
        ("p2", "q2"),
    )
    assert result.statistics["relationships_created"] == 4


@pytest.mark.parametrize(
    "predicate",
    (
        "q.id = p.id",
        "p.id > 1",
        "p.id = 1 OR q.id = 2",
    ),
    ids=("correlated", "non-equality", "or"),
)
def test_only_proven_local_equalities_are_pushed(database, predicate: str) -> None:
    _create_schema(database)
    plan = database.explain(f"MATCH (p:P), (q:Q) WHERE {predicate} RETURN p.id, q.id")
    p_scan = _scan(plan, "p")
    q_scan = _scan(plan, "q")

    assert _direct_filter(plan, p_scan) is None
    assert _direct_filter(plan, q_scan) is not None
    assert q_scan.child is p_scan


def test_primary_key_seek_and_unkeyed_pushdown_compose(database) -> None:
    _create_schema(database, keyed_q=True)
    plan = database.explain("MATCH (p:P {id: $p}), (q:Q {id: $q}) RETURN p.id, q.id")
    p_scan = _scan(plan, "p")
    seeks = [
        node
        for node in plan.walk()
        if isinstance(node, IndexSeek) and node.variable == "q"
    ]

    assert _direct_filter(plan, p_scan) is not None
    assert len(seeks) == 1
    assert seeks[0].child is _direct_filter(plan, p_scan)
    assert not any(
        isinstance(node, NodeScan) and node.variable == "q" for node in plan.walk()
    )


def test_parameter_equality_keeps_null_and_numeric_semantics(database) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, ordinal: 'one'})")
        txn.execute("CREATE (:P {id: 2, ordinal: 'two'})")

    statement = "MATCH (p:P {id: $key}) RETURN p.ordinal"
    assert database.execute(statement, {"key": 1.0}).rows == (("one",),)
    assert database.execute(statement, {"key": None}).rows == ()


def test_pushdown_reads_pending_nodes_owned_by_the_transaction(database) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 3, ordinal: 'pending-p'})")
        txn.execute("CREATE (:Q {id: 7, ordinal: 'pending-q'})")
        result = txn.execute(
            "MATCH (p:P {id: 3}), (q:Q {id: 7}) "
            "CREATE (p)-[:E {w: 1}]->(q) RETURN p.ordinal, q.ordinal"
        )
        assert result.rows == (("pending-p", "pending-q"),)
        assert result.statistics["relationships_created"] == 1

    assert database.execute(
        "MATCH (p:P)-[:E]->(q:Q) RETURN p.ordinal, q.ordinal"
    ).rows == (("pending-p", "pending-q"),)


def test_pushdown_filters_the_complete_owner_overlay_after_update_and_delete(
    database,
) -> None:
    """A local filter sees updated values and omits rows deleted by its own transaction."""

    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, ordinal: 'before'})")
        txn.execute("CREATE (:Q {id: 9, ordinal: 'target'})")

    with database.begin("write") as txn:
        txn.execute("MATCH (p:P {id: 1}) SET p.id = 2, p.ordinal = 'after'")
        assert txn.execute("MATCH (p:P {id: 2}) RETURN p.ordinal").rows == (("after",),)
        assert txn.execute("MATCH (p:P {id: 1}) RETURN p.ordinal").rows == ()
        txn.execute("MATCH (q:Q {id: 9}) DELETE q")
        assert (
            txn.execute(
                "MATCH (p:P {id: 2}), (q:Q {id: 9}) RETURN p.ordinal, q.ordinal"
            ).rows
            == ()
        )


def test_optional_local_equality_keeps_one_null_extended_row_on_a_miss(
    database,
) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, ordinal: 'one'})")

    statement = "OPTIONAL MATCH (p:P) WHERE p.id = $key RETURN p.ordinal"
    assert database.execute(statement, {"key": 1}).rows == (("one",),)
    assert database.execute(statement, {"key": 99}).rows == ((None,),)


def test_selective_endpoints_fit_the_scan_sized_intermediate_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "budget"
    size = 6
    setup = okto_grafx.connect(root, page_size=512)
    try:
        _create_schema(setup)
        _insert_nodes(setup, size)
    finally:
        setup.close()

    database = okto_grafx.connect(root, page_size=512, max_intermediate_rows=size)
    try:
        with database.begin("write") as txn:
            result = txn.execute(
                "MATCH (p:P {id: 2}), (q:Q {id: 4}) CREATE (p)-[:E {w: 1}]->(q)"
            )
        assert result.statistics["rows_scanned"] == 2 * size
        assert result.statistics["relationships_created"] == 1
    finally:
        database.close()
