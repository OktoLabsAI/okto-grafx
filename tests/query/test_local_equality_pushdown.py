"""The canonical Cartesian filter order, including its observable refusal surface."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
)
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


def _create_short_circuit_schema(database) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(k INT64, name STRING)")
        txn.execute("CREATE NODE TABLE Q(tag STRING)")


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


def test_unique_endpoint_equalities_keep_the_canonical_cartesian_plan(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unindexed equality stays above both scans to preserve their error surface."""

    size = 12
    _create_schema(database)
    _insert_nodes(database, size)
    statement = "MATCH (p:P {id: $p}), (q:Q {id: $q}) CREATE (p)-[:E {w: 1}]->(q)"
    plan = database.explain(statement)
    p_scan = _scan(plan, "p")
    q_scan = _scan(plan, "q")
    assert _direct_filter(plan, p_scan) is None
    assert _direct_filter(plan, q_scan) is not None
    assert q_scan.child is p_scan

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
        assert result.statistics["rows_scanned"] == size + size * size
        assert result.statistics["relationships_created"] == 1


def test_canonical_filter_preserves_duplicate_multiplicity_and_scan_order(
    database,
) -> None:
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
def test_wider_predicates_keep_the_canonical_filter(database, predicate: str) -> None:
    _create_schema(database)
    plan = database.explain(f"MATCH (p:P), (q:Q) WHERE {predicate} RETURN p.id, q.id")
    p_scan = _scan(plan, "p")
    q_scan = _scan(plan, "q")

    assert _direct_filter(plan, p_scan) is None
    assert _direct_filter(plan, q_scan) is not None
    assert q_scan.child is p_scan


def test_primary_key_seek_and_unkeyed_canonical_filter_compose(database) -> None:
    _create_schema(database, keyed_q=True)
    plan = database.explain("MATCH (p:P {id: $p}), (q:Q {id: $q}) RETURN p.id, q.id")
    p_scan = _scan(plan, "p")
    seeks = [
        node
        for node in plan.walk()
        if isinstance(node, IndexSeek) and node.variable == "q"
    ]

    assert _direct_filter(plan, p_scan) is None
    assert len(seeks) == 1
    assert seeks[0].child is p_scan
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


def test_canonical_filter_reads_pending_nodes_owned_by_the_transaction(
    database,
) -> None:
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


def test_canonical_filter_reads_the_complete_owner_overlay_after_update_and_delete(
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


def test_optional_short_circuit_keeps_the_non_boolean_term_unobserved(
    database,
) -> None:
    """A false equality retains OPTIONAL's null row without evaluating its right term."""

    _create_short_circuit_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {k: 2, name: 'x'})")

    assert database.execute(
        "OPTIONAL MATCH (p:P) WHERE p.k = 1 AND p.name RETURN p.k, p.name"
    ).rows == ((None, None),)


def test_cartesian_short_circuit_keeps_later_non_boolean_term_unobserved(
    database,
) -> None:
    """A false outer equality is evaluated before a non-boolean inner property."""

    _create_short_circuit_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {k: 2, name: 'x'})")
        txn.execute("CREATE (:Q {tag: 'x'})")

    assert (
        database.execute("MATCH (p:P), (q:Q) WHERE p.k = 1 AND q.tag RETURN p.k").rows
        == ()
    )


def test_a_refusing_term_before_an_equality_cannot_be_optimised_away(
    database,
) -> None:
    """Predicate evaluation order is observable and a write may not suppress its refusal."""

    _create_short_circuit_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {k: 2, name: 'x'})")

    with pytest.raises(GrafxPlanError) as raised:
        with database.begin("write") as txn:
            txn.execute(
                "MATCH (p:P) WHERE size(p.k) = 1 AND p.k = 1 "
                "CREATE (:P {k: 3, name: 'must-not-exist'})"
            )

    assert raised.value.details["field"] == "function"
    assert database.execute("MATCH (p:P) RETURN p.k").rows == ((2,),)


def test_an_outer_miss_does_not_suppress_a_persistent_inner_scan_failure(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical product opens Q before filtering a non-matching P row."""

    _create_short_circuit_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {k: 2, name: 'outer'})")
        txn.execute("CREATE (:Q {tag: 'inner'})")
    original = HeapStore.scan
    expected = GrafxCorruptionDetected("injected inner scan refusal", table="Q")

    def refusing(self, table, snapshot):
        if table.name == "Q":
            raise expected
        return original(self, table, snapshot)

    monkeypatch.setattr(HeapStore, "scan", refusing)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        database.execute("MATCH (p:P), (q:Q) WHERE p.k = 1 RETURN p.k, q.tag")
    assert raised.value is expected


def test_inner_scan_replay_is_owned_by_one_statement(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_schema(database)
    _insert_nodes(database, 3)
    scans: Counter[str] = Counter()
    original = HeapStore.scan

    def counted(self, table, snapshot):
        if table.name in {"P", "Q"}:
            scans[table.name] += 1
        return original(self, table, snapshot)

    monkeypatch.setattr(HeapStore, "scan", counted)
    statement = "MATCH (p:P), (q:Q) RETURN p.id, q.id"

    assert len(database.execute(statement).rows) == 9
    assert len(database.execute(statement).rows) == 9
    assert scans == {"P": 2, "Q": 2}


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    (
        ("_NODE_SCAN_REPLAY_MAX_ENTRIES", 1),
        ("_NODE_SCAN_REPLAY_MAX_BYTES", 1),
    ),
    ids=("entry-cap", "byte-cap"),
)
def test_replay_capacity_exhaustion_returns_to_canonical_scans(
    database,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    _create_schema(database)
    _insert_nodes(database, 3)
    scans: Counter[str] = Counter()
    original = HeapStore.scan

    def counted(self, table, snapshot):
        if table.name in {"P", "Q"}:
            scans[table.name] += 1
        return original(self, table, snapshot)

    monkeypatch.setattr(HeapStore, "scan", counted)
    monkeypatch.setattr(query_engine_module, limit_name, limit)

    result = database.execute("MATCH (p:P), (q:Q) RETURN p.id, q.id")

    assert len(result.rows) == 9
    assert result.statistics["rows_scanned"] == 12
    assert scans == {"P": 1, "Q": 3}


def test_first_inner_scan_corruption_is_not_hidden_by_replay(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_schema(database)
    _insert_nodes(database, 3)
    original = HeapStore.scan
    expected = GrafxCorruptionDetected("injected mid-scan refusal", table="Q")

    def refusing(self, table, snapshot):
        rows = original(self, table, snapshot)
        if table.name != "Q":
            return rows

        def partial():
            yield next(rows)
            yield next(rows)
            raise expected

        return partial()

    monkeypatch.setattr(HeapStore, "scan", refusing)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        database.execute("MATCH (p:P), (q:Q) RETURN p.id, q.id")
    assert raised.value is expected


def test_limit_does_not_eagerly_complete_the_inner_replay(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_schema(database)
    _insert_nodes(database, 3)
    original = HeapStore.scan
    expected = GrafxCorruptionDetected("must remain unread", table="Q")

    def refusing(self, table, snapshot):
        rows = original(self, table, snapshot)
        if table.name != "Q":
            return rows

        def partial():
            yield next(rows)
            # LIMIT's canonical iterator reads one row beyond its result before stopping.  A
            # replay cache must not widen that established lookahead into a full table read.
            yield next(rows)
            raise expected

        return partial()

    monkeypatch.setattr(HeapStore, "scan", refusing)

    assert database.execute("MATCH (p:P), (q:Q) RETURN p.id, q.id LIMIT 1").rows == (
        (0, 0),
    )


def test_replay_preserves_pending_owner_rows_and_order(database) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, ordinal: 'p1'})")
        txn.execute("CREATE (:P {id: 2, ordinal: 'p2'})")
        txn.execute("CREATE (:Q {id: 8, ordinal: 'q1'})")
        txn.execute("CREATE (:Q {id: 9, ordinal: 'q2'})")

        result = txn.execute("MATCH (p:P), (q:Q) RETURN p.ordinal, q.ordinal")

    assert result.rows == (
        ("p1", "q1"),
        ("p1", "q2"),
        ("p2", "q1"),
        ("p2", "q2"),
    )
    assert result.statistics["rows_scanned"] == 6


def test_replay_preserves_owner_updates_and_deletes(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, ordinal: 'p1'})")
        txn.execute("CREATE (:P {id: 2, ordinal: 'p2'})")
        txn.execute("CREATE (:Q {id: 8, ordinal: 'before'})")
        txn.execute("CREATE (:Q {id: 9, ordinal: 'deleted'})")

    with database.begin("write") as txn:
        txn.execute("MATCH (q:Q {id: 8}) SET q.ordinal = 'after'")
        txn.execute("MATCH (q:Q {id: 9}) DELETE q")
        scans: Counter[str] = Counter()
        original = HeapStore.scan

        def counted(self, table, snapshot):
            if table.name in {"P", "Q"}:
                scans[table.name] += 1
            return original(self, table, snapshot)

        monkeypatch.setattr(HeapStore, "scan", counted)
        result = txn.execute("MATCH (p:P), (q:Q) RETURN p.ordinal, q.ordinal")

    assert result.rows == (("p1", "after"), ("p2", "after"))
    assert result.statistics["rows_scanned"] == 4
    assert scans == {"P": 1, "Q": 1}


def test_explicit_query_memory_budget_keeps_canonical_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "memory-budget"
    setup = okto_grafx.connect(root, page_size=512)
    try:
        _create_schema(setup)
        _insert_nodes(setup, 3)
    finally:
        setup.close()

    database = okto_grafx.connect(root, page_size=512, query_memory_budget_bytes=1_024)
    scans: Counter[str] = Counter()
    original = HeapStore.scan

    def counted(self, table, snapshot):
        if table.name in {"P", "Q"}:
            scans[table.name] += 1
        return original(self, table, snapshot)

    monkeypatch.setattr(HeapStore, "scan", counted)
    try:
        result = database.execute("MATCH (p:P), (q:Q) RETURN p.id, q.id")
    finally:
        database.close()

    assert len(result.rows) == 9
    assert result.statistics["rows_scanned"] == 12
    assert scans == {"P": 1, "Q": 3}


def test_canonical_cartesian_work_exceeds_a_scan_sized_intermediate_budget(
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
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            with database.begin("write") as txn:
                txn.execute(
                    "MATCH (p:P {id: 2}), (q:Q {id: 4}) CREATE (p)-[:E {w: 1}]->(q)"
                )
        assert raised.value.details["field"] == "max_intermediate_rows"
        assert raised.value.details["limit"] == size
        assert raised.value.details["observed"] == size + 1
        assert raised.value.details["operator"] == "NodeScan"
        assert database.execute("MATCH (p:P)-[e:E]->(q:Q) RETURN e.w").rows == ()
    finally:
        database.close()
