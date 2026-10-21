"""RF01: physical scan work is distinct from visible rows and logical scans.

Fixtures write directly through the existing real-store query stack. Timing
qualification is a separate campaign; these tests establish counting semantics.
"""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.control import _ReadControl
from okto_grafx.engine.heap_store import HeapStore
from tests.query.stack import TransactionDouble, build_query_stack
from tests.query import test_node_in_seek as node_in_fixture
from tests.query import test_batched_landing_without_vectors as landing_fixture
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.index_manager import HashIndex, IndexManager

database = node_in_fixture.database
graph = landing_fixture.graph


def _fallback(statistics):
    return {key: value for key, value in statistics.items()
            if key.startswith("fallback_reason_")}


def test_fallback_is_actual_execution_not_an_alternative_in_plan(database, monkeypatch):
    assert any(type(node).__name__ == "NodeScan"
               for node in database.explain(node_in_fixture.QUERY).walk())
    fast = database.execute(node_in_fixture.QUERY, {"ids": ["a1"]})
    assert _fallback(fast.statistics) == {}
    monkeypatch.setattr(IndexManager, "validated_versions_many", None)
    scan = database.execute(node_in_fixture.QUERY, {"ids": ["a1"]})
    assert scan.rows == fast.rows
    assert _fallback(scan.statistics) == {"fallback_reason_index_unavailable": 1}


@pytest.mark.parametrize("ids,reason", [
    (["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10"], "nonselective_probe_set"),
    (["\ud800"], "unproven_totality"),
])
def test_multi_key_scan_fallback_reports_its_executed_guard(database, ids, reason):
    result = database.execute(node_in_fixture.QUERY, {"ids": ids})
    assert _fallback(result.statistics) == {f"fallback_reason_{reason}": 1}


def test_stale_authority_fallback_reports_staleness_without_probe(database, monkeypatch):
    original = qe._IndexAuthorityProjection.named

    class Stale:
        stale = True

        def __init__(self, store):
            self.store = store

        def __getattr__(self, name):
            return getattr(self.store, name)

    def named(self, name):
        store = original(self, name)
        return None if store is None else Stale(store)

    monkeypatch.setattr(qe._IndexAuthorityProjection, "named", named)
    result = database.execute(node_in_fixture.QUERY, {"ids": ["a1"]})
    assert result.rows == (("a1", 1),)
    assert _fallback(result.statistics) == {"fallback_reason_index_stale": 1}


@pytest.mark.parametrize("suffix", ["RETURN b.id LIMIT 0", "RETURN b.id"])
def test_unconsumed_landing_decisions_do_not_report_fallback(graph, suffix):
    result = graph.execute(landing_fixture.PREFIX.replace("a.id='a'", "a.id='absent'") + suffix)
    assert result.rows == ()
    assert _fallback(result.statistics) == {}


@pytest.mark.parametrize("option,reason", [
    (None, "streaming_guard"),
    ("_max_intermediate_rows", "streaming_guard"),
    ("_query_memory_budget_bytes", "memory_guard"),
])
def test_landing_fallback_records_first_effective_guard(graph, monkeypatch, option, reason):
    if option is not None:
        monkeypatch.setattr(graph._queries, option, 65536)
    with graph.query(landing_fixture.PREFIX + "RETURN b.id").cursor(batch_size=1) as cursor:
        assert _fallback(cursor.statistics) == {}
        assert cursor.fetchone() == ("b0",)
        assert _fallback(cursor.statistics) == {f"fallback_reason_{reason}": 1}
    assert graph.transactions.open_transactions == 0


def test_fallback_diagnostic_does_not_add_cooperative_steps(graph, monkeypatch):
    original_step = _ReadControl.step
    steps = []

    def step(self):
        steps.append(1)
        return original_step(self)

    monkeypatch.setattr(_ReadControl, "step", step)
    from okto_grafx import CancellationToken
    result = graph.execute(landing_fixture.PREFIX + "RETURN b.id", cancellation=CancellationToken())
    measured = len(steps)
    assert _fallback(result.statistics) == {"fallback_reason_streaming_guard": 1}
    monkeypatch.setattr(qe, "_record_fallback", lambda *_: None)
    steps.clear()
    reference = graph.execute(landing_fixture.PREFIX + "RETURN b.id", cancellation=CancellationToken())
    assert result.rows == reference.rows
    assert len(steps) == measured
    assert {key: value for key, value in result.statistics.items()
            if not key.startswith("fallback_reason_")} == reference.statistics


def test_first_consumed_edge_fallback_wins_over_earlier_unused_landing_decision(graph, monkeypatch):
    # The memory decision is evaluated first, but the edge scan is consumed first.
    monkeypatch.setattr(graph._queries, "_query_memory_budget_bytes", 65536)
    monkeypatch.setattr(qe, "_EDGE_LOOKUP_FAN_LIMIT", 0)
    result = graph.execute(landing_fixture.PREFIX + "RETURN b.id")
    assert result.rows == (("b0",), ("b0",), ("b1",), ("b1",), ("b2",), ("b2",))
    assert result.statistics["edge_scans"] == 1
    assert _fallback(result.statistics) == {"fallback_reason_fan_limit": 1}


def test_owner_edge_overlay_reports_only_one_reason(graph):
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        result = tx.execute(landing_fixture.PREFIX + "RETURN b.id")
        assert len(result.rows) == 7
        assert _fallback(result.statistics) == {"fallback_reason_owner_overlay": 1}


def test_empty_multi_key_table_does_not_claim_a_scan_fallback(database):
    result = database.execute("MATCH (n:D) WHERE n.id IN $ids RETURN n.id", {"ids": ["absent"]})
    assert result.rows == ()
    assert _fallback(result.statistics) == {}


def test_specialized_landing_port_is_not_misreported_as_totality(graph, monkeypatch):
    original = HeapStore.read

    def specialized(heap, ref):
        return original(heap, ref)

    monkeypatch.setattr(HeapStore, "read", specialized)
    result = graph.execute(landing_fixture.PREFIX + "RETURN b.id")
    assert len(result.rows) == 6
    assert _fallback(result.statistics) == {"fallback_reason_noncanonical_port": 1}


def test_certificate_retry_that_succeeds_is_not_a_fallback(database, monkeypatch):
    original = HashIndex.finish_exact_read
    rejected = []

    def changed_once(store, before, lsn):
        stable = original(store, before, lsn)
        if not rejected:
            rejected.append(1)
            return False
        return stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed_once)
    result = database.execute(node_in_fixture.QUERY, {"ids": ["a1"]})
    assert result.rows == (("a1", 1),)
    assert result.statistics["certificate_retries"] == 1
    assert _fallback(result.statistics) == {}


class _ReadTransaction(TransactionDouble):
    mode = "read"


def _people(count=3):
    stack = build_query_stack()
    refs = [stack.insert("Person", i, (i, f"p{i}", 20 + i, "SP"))
            for i in range(1, count + 1)]
    return stack, refs


@pytest.mark.parametrize("projection", ["p.id", "p"])
def test_node_scan_counts_invisible_versions_before_projection(projection):
    stack, refs = _people()
    stack.end("Person", refs[0], (1, "p1", 21, "SP"), csn=2)
    result = stack.engine.execute(
        f"MATCH (p:Person) RETURN {projection}", stack.transaction()
    )
    assert len(result.rows) == 2
    assert result.statistics["rows_scanned"] == 2
    assert result.statistics["physical_node_versions_examined"] == 3


def test_grouped_edge_scan_counts_history_once_for_all_frontiers():
    stack, _ = _people()
    ended = stack.insert("Knows", 1, (1, 2, 2001))
    stack.insert("Knows", 2, (1, 3, 2002))
    stack.insert("Knows", 3, (2, 3, 2003))
    stack.end("Knows", ended, (1, 2, 2001), csn=2)
    result = stack.engine.execute(
        "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id, b.id",
        stack.transaction(),
    )
    assert sorted(result.rows) == [(1, 3), (2, 3)]
    assert result.statistics["edge_scans"] == 1
    assert result.statistics["physical_edge_versions_examined"] == 3


def test_scan_counters_are_statement_local_and_do_not_charge_control(monkeypatch):
    stack, _ = _people(8)
    text = "MATCH (p:Person) RETURN p.id"
    observed = []
    original_step = _ReadControl.step

    def step(self):
        observed.append(1)
        return original_step(self)

    monkeypatch.setattr(_ReadControl, "step", step)
    control = _ReadControl(stack.clock, None, None)
    result = stack.engine.execute(
        text, _ReadTransaction(stack.snapshot()), read_control=control
    )
    measured_steps = len(observed)
    assert result.statistics["physical_node_versions_examined"] == 8

    # The unchanged public scan is the control path. An override must still be
    # called and must not acquire a made-up physical count from its output rows.
    original_projected = HeapStore.scan_projected

    def delegated(self, table, snapshot, positions):
        return original_projected(self, table, snapshot, positions)

    monkeypatch.setattr(HeapStore, "scan_projected", delegated)
    observed.clear()
    control = _ReadControl(stack.clock, None, None)
    canonical = stack.engine.execute(
        text, _ReadTransaction(stack.snapshot()), read_control=control
    )
    assert canonical.rows == result.rows
    assert len(observed) == measured_steps
    assert "physical_node_versions_examined" not in canonical.statistics
    assert result.statistics["physical_node_versions_examined"] == 8


def test_snapshots_change_visibility_but_not_the_physical_versions_examined():
    stack, refs = _people()
    stack.end("Person", refs[0], (1, "p1", 21, "SP"), csn=2)
    before = stack.engine.execute("MATCH (p:Person) RETURN p.id", stack.transaction(1))
    after = stack.engine.execute("MATCH (p:Person) RETURN p.id", stack.transaction())
    assert before.rows == ((1,), (2,), (3,))
    assert after.rows == ((2,), (3,))
    assert before.statistics["physical_node_versions_examined"] == 3
    assert after.statistics["physical_node_versions_examined"] == 3


def test_early_close_counts_inspected_page_but_does_not_walk_remaining_pages(monkeypatch):
    stack, _ = _people(40)
    statistics = {}
    decoded = []
    original = HeapStore._decode_version_with_header

    def decode(self, *args, **kwargs):
        decoded.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HeapStore, "_decode_version_with_header", decode)
    rows = stack.heap._scan_with_statistics(stack.table("Person"), stack.snapshot(), statistics)
    next(rows)
    inspected = statistics["physical_node_versions_examined"]
    assert 1 < inspected < 40
    assert len(decoded) == 1
    rows.close()
    assert statistics["physical_node_versions_examined"] == inspected
    assert len(decoded) == 1


def test_pending_owner_rows_are_logical_work_not_stored_versions():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 2})")
            result = tx.execute("MATCH (n:N) RETURN n.id")
            assert result.rows == ((1,), (2,))
            assert result.statistics["rows_scanned"] == 2
            assert result.statistics["physical_node_versions_examined"] == 1


def test_limit_zero_does_not_report_unexecuted_scan():
    stack, _ = _people()
    result = stack.engine.execute(
        "MATCH (p:Person) RETURN p.id LIMIT 0", stack.transaction()
    )
    assert result.rows == ()
    assert "physical_node_versions_examined" not in result.statistics


def test_empty_executed_scan_reports_zero():
    stack = build_query_stack()
    result = stack.engine.execute(
        "MATCH (p:Person) RETURN p.id", stack.transaction()
    )
    assert result.rows == ()
    assert result.statistics["physical_node_versions_examined"] == 0
