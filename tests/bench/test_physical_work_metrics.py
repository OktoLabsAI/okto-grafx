"""RF01: physical scan work is distinct from visible rows and logical scans.

Fixtures write directly through the existing real-store query stack. Timing
qualification is a separate campaign; these tests establish counting semantics.
"""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.control import _ReadControl
from okto_grafx.engine.heap_store import HeapStore
from tests.query.stack import TransactionDouble, build_query_stack


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
