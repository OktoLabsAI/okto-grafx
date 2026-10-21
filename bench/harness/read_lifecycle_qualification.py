"""Read-cycle recipes and untimed ownership diagnostics for RF10.

These are consistency choices, not interchangeable freshness promises. The
campaign compares each recipe with itself across revisions, never equates
sixteen autocommit snapshots with one application-owned snapshot.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

FAMILIES = frozenset({"read_autocommit", "read_shared", "read_materialized", "read_cursor"})
PK = "MATCH (n:N {id:$id}) RETURN n.id,n.value"
SCAN = "MATCH (n:N) RETURN n.id,n.value ORDER BY n.id"


def validate(scenario):
    if scenario.nodes < 16 or scenario.churn_percent or scenario.reader != "short":
        raise ValueError("read lifecycle requires at least16nodes and a private unchanged fixture")
    if scenario.keys != tuple(range(16)):
        raise ValueError("read lifecycle compares exactly16ordered PKs")


def query_for(scenario):
    return (PK, {"id": 0}) if scenario.family in {"read_autocommit", "read_shared"} else (SCAN, {})


def oracle(scenario):
    count = 16 if scenario.family in {"read_autocommit", "read_shared"} else scenario.nodes
    return tuple((i, i * 2) for i in range(count))


def contract(scenario):
    return {
        "snapshot": "one snapshot shared by16statements" if scenario.family == "read_shared" else "independent snapshot per execute/cursor",
        "transaction": "read only; no durable write ACK measured",
        "statement_sequence": [{"query": PK, "parameters": {"id": i}} for i in range(16)]
        if scenario.family in {"read_autocommit", "read_shared"} else [{"query": SCAN, "parameters": {}}],
        "diagnostic_scope": "after timing and memory observation; separate repeat records actual manager begin/commit/rollback counts; not counts observed inside measured interval",
        "partial_cursor_scope": "separate post-timing prefix diagnostic records first-row/close; temporal cursor sample consumes all rows",
        "ownership": "owned snapshots must end; physical standing pins may remain until handle close",
        "rss_limit": "process-lifetime RSS peak includes post-timing diagnostic work; logical-memory observation covers timed recipe only",
        "qualification_limit": "recipe tradeoff only; no universal gain or freshness equivalence; no concurrent writer in timing fixture",
    }


def execute(db, scenario, start_ns=None):
    """Run the real recipe without transaction-manager wrappers."""
    start = time.perf_counter_ns() if start_ns is None else start_ns
    rows, statistics = [], {}
    first = None
    close_ns = 0

    def append(result):
        nonlocal first
        if first is None and result.rows:
            first = time.perf_counter_ns() - start
        rows.extend(result.rows)
        for key, value in result.statistics.items():
            statistics[key] = statistics.get(key, 0) + value

    if scenario.family == "read_shared":
        with db.begin("read") as tx:
            for key in scenario.keys:
                append(tx.execute(PK, {"id": key}))
            close_start = time.perf_counter_ns()
        close_ns = time.perf_counter_ns() - close_start
    elif scenario.family == "read_autocommit":
        for key in scenario.keys:
            append(db.execute(PK, {"id": key}))
    elif scenario.family == "read_materialized":
        append(db.execute(SCAN))
    else:
        with db.query(SCAN).cursor(batch_size=1) as cursor:
            head = next(cursor, None)
            if head is not None:
                first = time.perf_counter_ns() - start
                rows.append(head)
                rows.extend(cursor)
            close_start = time.perf_counter_ns()
        close_ns = time.perf_counter_ns() - close_start
        statistics.update(cursor.statistics)
    if db.transactions.open_transactions:
        raise RuntimeError("read recipe retained an owned transaction")
    if scenario.family in {"read_shared", "read_cursor"}:
        statistics["lifecycle_explicit_close_ns"] = close_ns
    return tuple(rows), SimpleNamespace(statistics=statistics), first


def diagnose(db, scenario):
    """A second, explicitly untimed execution; never substitute for timed work."""
    manager = db._transactions
    cls = type(manager)
    counts = {"begin": 0, "commit": 0, "rollback": 0}
    originals = {name: getattr(cls, name) for name in counts}

    def wrap(name):
        def call(self, *args, **kwargs):
            result = originals[name](self, *args, **kwargs)
            if self is manager:
                counts[name] += 1
            return result
        return call

    with patch.object(cls, "begin", wrap("begin")), patch.object(cls, "commit", wrap("commit")), patch.object(cls, "rollback", wrap("rollback")):
        rows, _, _ = execute(db, scenario)
    if rows != oracle(scenario):
        raise RuntimeError("read lifecycle diagnostic oracle mismatch")
    result = {"diagnostic_transactions_" + key: value for key, value in counts.items()}
    cursor = db.query(SCAN).cursor(batch_size=1)
    start = time.perf_counter_ns()
    with cursor:
        head = next(cursor)
        result["diagnostic_partial_first_row_ns"] = time.perf_counter_ns() - start
        result["diagnostic_partial_owned_transactions"] = db.transactions.open_transactions
        result["diagnostic_partial_resident_physical_pins"] = sum(frame.pins for frame in db._pool._frames.values())
        if head != (0, 0):
            raise RuntimeError("partial cursor oracle mismatch")
        close_start = time.perf_counter_ns()
    result["diagnostic_partial_close_ns"] = time.perf_counter_ns() - close_start
    cursor.close()
    result["diagnostic_owned_transactions_after_close"] = db.transactions.open_transactions
    result["diagnostic_physical_pins_after_close"] = sum(frame.pins for frame in db._pool._frames.values())
    result["diagnostic_pool_retained_estimate_bytes"] = db._pool.retained_bytes_estimate()
    result["diagnostic_standing_reader_horizon_present"] = int(manager._coordinator.reader_horizon() is not None)
    if result["diagnostic_owned_transactions_after_close"] or not cursor.closed or cursor._buffer:
        raise RuntimeError("partial cursor retained ownership")
    return result


def matrix():
    from bench.harness.campaign_fixtures import Scenario
    return [Scenario("rf10-" + family + "-" + cache, family, nodes=128,
                     edges_per_node=0, keys=tuple(range(16)), cache=cache)
            for family in sorted(FAMILIES) for cache in ("warm", "reopen")]
