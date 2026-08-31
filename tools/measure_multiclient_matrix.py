"""F1/F4 multi-client matrix instrument (roadmap section 6.5), version 1.

The mono-client 17.72x number charges the full price of the multi-writer/multi-reader protocol
and collects none of its return, because the M7 harness runs one process. This tool measures the
return: N writer processes against M reader processes, over a real database, with a serial oracle
deciding whether the numbers describe a correct database at all.

WHAT IS FROZEN (these come from section 6.5 and are not tuning knobs):
  * writer families      create_node / create_edge / update_node / mark_superseded
  * writer counts        N in {1, 2, 4, 8}
  * reader counts        M in {0, 1, 3}
  * regimes              disjoint tables, and one hot table ("typical server", W6:49)
  * reader shapes        autocommit reads, one long read transaction, read-only traversal
  * foreign commit rate  {0, 1/s, 10/s} -- the key curve: reader latency vs foreign traffic
  * reader target        same-table vs unrelated-table -- the CE-3 discriminant
  * correctness          serial oracle, acknowledged-vs-stored, verify('all') live and cold

WHAT WAS A GAP IN THE REPORT AND IS ONLY PARAMETERISED HERE (the report never fixed these
volumes, so they are options with declared defaults, NOT frozen acceptance):
  --seconds, --rows-per-txn, --txns-per-writer, --long-reader-seconds, --warmup-seconds.
Whatever value a run uses is copied into the report, so a later reader is never left guessing
which numbers were policy and which were choices.

THE CE-3 DISCRIMINANT. A reader on `unrelated-table` reads a table (`Quiet`) that is seeded at
bootstrap and never written during the run, while the writer commits to its own tables. If a
foreign commit to an unrelated table still costs that reader -- dropped read views, higher
latency -- then WAL-directed read-view invalidation has a real target. If it costs nothing,
CE-3 loses its justification on this axis. `same-table` is the control: the reader reads exactly
what the writer writes.

THE RATE IS AN AGGREGATE, AND THE ZERO IS COMMANDED. `--foreign-commit-rate` names the rate
the READER sees; it is divided among the writers and they are staggered across one period, so
N=1 and N=8 are comparable at the same point instead of arriving N at a time. A rate of 0 means
every writer opens the database and stays idle: zero commits COMMANDED, marked `idle: true`,
with the case asserting the count really is zero, so a commanded zero is never confused with a
zero caused by a child that died. OMITTING the option sweeps the frozen curve; it does not mean
unpaced.

HONESTY RULES (a broken run must never read as a good one):
  * a child that dies, times out, or emits garbage fails the whole case; it never becomes 0
  * a case where nothing actually happened FAILS rather than passing every "nothing bad
    happened" check vacuously
  * a metric the product does not expose is reported as unavailable WITH ITS REASON and never
    as a number; the F1 items section 6.5 itself lists as non-existent stay unavailable
  * `verify` is judged by VerificationReport.clean, not by an empty findings tuple: a walk that
    checked nothing also has no findings, and calling that clean is how a verifier certifies a
    database it never looked at (A75.2)
  * every case carries provenance: commit, tree, source root, argv, Python, platform, script
    SHA, product cleanliness, and whether the operator asserted an idle machine

Not implemented here (declared, not silently missing): F2 phase timers, F3 takeover/fairness,
F5 Ladybug capacity comparison. This tool is F1 plus the finite F4/CE-3 two-process subcase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

CHILD_TIMEOUT = 900

FROZEN_WRITER_COUNTS = (1, 2, 4, 8)
FROZEN_READER_COUNTS = (0, 1, 3)
FROZEN_REGIMES = ("disjoint", "hot")
FROZEN_READER_SHAPES = ("autocommit", "long", "traversal")
FROZEN_READER_TARGETS = ("same-table", "unrelated-table")
FROZEN_FOREIGN_COMMIT_RATES = (0.0, 1.0, 10.0)
FROZEN_FAMILIES = ("create_node", "create_edge", "update_node", "mark_superseded")

QUIET_TABLE = "Quiet"
QUIET_SEED_ROWS = 200

# Counters section 6.5 names for F1 that the product does not emit today. Recording the reason
# beside the name is the whole point: a later reader must not mistake absence for zero.
UNAVAILABLE_METRICS = {
    "commit_hold_seconds": "not emitted by the product; roadmap 6.5 names it (V7 12.3) and "
    "nothing implements it",
    "writer_lease_publish_seconds": "not emitted by the product (roadmap 6.5)",
    "index_view_hold_seconds": "not emitted by the product (roadmap 6.5)",
    "commit_lock_wait_and_hold_histogram": "no per-section wait/hold timer exists; only "
    "oktografx_lease_wait_seconds is emitted, and it covers lease wait, not section hold",
    "wal_bytes_retained_between_checkpoints": "no public per-checkpoint retention series; "
    "oktografx_wal_size_bytes is a point gauge, not a between-checkpoint delta",
    "horizon_lag_seconds": "max(published LSN) - min(reader pin) has no public sampler",
    "per_op_read_page_and_certificate_counts": "_read_page / _fresh_certificate / _still_names "
    "are private and uninstrumented; counting them would require changing the product",
}

PIN_GUARD = """import pathlib as _pathlib
_pinned = _pathlib.Path(SRC).resolve()
if not (_pinned / "okto_grafx" / "__init__.py").is_file():
    raise SystemExit("A94-PIN-FAILED: no okto_grafx package under the pinned source root "
                     + str(_pinned))
import okto_grafx
# is_relative_to, not startswith: a sibling named "src-evil" starts with "src" and would sail
# through a prefix test while being a completely different tree.
if not _pathlib.Path(okto_grafx.__file__).resolve().is_relative_to(_pinned):
    raise SystemExit("A94-PIN-FAILED: child resolved okto_grafx at "
                     + str(_pathlib.Path(okto_grafx.__file__).resolve())
                     + " which is outside the pinned source root " + str(_pinned))
"""
"""The A94 source pin, in one place. Every child embeds this exact text."""

WRITER_CHILD = r'''
import json, pathlib, random, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

root, slot, seed = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
regime, seconds = sys.argv[5], float(sys.argv[6])
rows_per_txn, txns = int(sys.argv[7]), int(sys.argv[8])
rate_text, warmup = sys.argv[9], float(sys.argv[10])
reopen_on_stale, out = sys.argv[11] == "1", sys.argv[12]
metrics_out, ready_path, barrier_path = sys.argv[13], sys.argv[14], sys.argv[15]
phase_offset = float(sys.argv[16])
# "" = unpaced (flat out). "0" = COMMANDED IDLE, the zero of the 6.5 curve. ">0" = paced.
rate = None if rate_text == "" else float(rate_text)

# A refusal carrying this field is durable: it comes from INDEX_FLAG_STALE on the index
# header (index_manager.py:677), which lives on disk. The product marks it retryable=True,
# but nothing in THIS process clears it, so a retry loop spins instead of retrying. The
# instrument names that case rather than burning the measurement window on it.
DURABLE_FIELD = "index_view_unavailable"

rnd = random.Random(seed * 7919 + slot)
db = connect(root, metrics="json", metrics_destination=metrics_out)
base = slot * 1_000_000
# Disjoint: this writer owns its own tables. Hot: every writer hammers the shared pair. Keys
# stay disjoint per writer either way, so a conflict measures the protocol, not a duplicate key.
node_table = "Item" if regime == "hot" else "Item%d" % slot
edge_table = "Links" if regime == "hot" else "Links%d" % slot

latency = {family: [] for family in
           ("create_node", "create_edge", "update_node", "mark_superseded")}
conflicts = 0
retries = 0
outside_window = 0
escapes = []
durable_refusals = []
reopens = 0
acknowledged = []
commits = 0
# THE COMMON WINDOW. Every participant waits on a barrier outside the database and then runs
# the SAME absolute window. Spawning sequentially and letting each process time itself gives
# each one a different denominator, and the curve would carry that stagger as though it were
# an effect of the foreign rate.
pathlib.Path(ready_path).write_text(json.dumps({"role": "writer", "slot": slot}),
                                    encoding="utf-8")
waited = 0.0
while not pathlib.Path(barrier_path).is_file() and waited < 900.0:
    time.sleep(0.02)
    waited += 0.02
if not pathlib.Path(barrier_path).is_file():
    raise SystemExit("BARRIER-TIMEOUT: writer %d never saw the start barrier" % slot)
barrier = json.loads(pathlib.Path(barrier_path).read_text(encoding="utf-8"))
start_at, end_at = float(barrier["start_at"]), float(barrier["end_at"])
timing_starts_at = start_at + warmup
opening = start_at - time.time()
if opening > 0:
    time.sleep(opening)

started = time.monotonic()
timed_from = None
# STAGGERED. Every writer starting its pacing at the same instant makes N commits arrive
# together and then nothing for a whole period: N-at-a-time bursts, not the smooth aggregate
# rate the frozen curve names. Each writer is offset by its share of one aggregate period.
next_slot = start_at + phase_offset
idle = rate == 0.0
first_commit_at = None
last_commit_at = None


def window_is_open():
    """True while the shared measured window is still running."""
    return time.time() < end_at


def in_timed_window():
    """True once the discarded warmup is over, and only while the window is still open."""
    now = time.time()
    return timing_starts_at <= now < end_at


def committed(family, build):
    """Run one transaction to a successful commit, timing begin->commit INCLUSIVE of retries.

    A give-up carries the LAST refusal that caused it. "Gave up after 60 retries" on its own
    names the symptom and throws away the only evidence of the cause.
    """
    global conflicts, retries, commits, timed_from, next_slot, db, reopens, outside_window
    global first_commit_at, last_commit_at
    last_refusal = None
    if rate:
        # Pace to this writer's share of the aggregate rate. The deadline is rechecked AROUND
        # the sleep: at N=8 and an aggregate of 1/s a writer waits 8s between commits, so a
        # round entered just before the end would otherwise commit long after the readers
        # stopped and be counted into a window it never belonged to.
        if not window_is_open():
            return False, None
        gap = next_slot - time.time()
        if gap > 0:
            time.sleep(min(gap, max(0.0, end_at - time.time())))
        if not window_is_open():
            return False, None
        next_slot = max(next_slot + 1.0 / rate, time.time())
    elif not window_is_open():
        return False, None
    began = time.perf_counter()
    for attempt in range(60):
        try:
            with db.begin("write") as txn:
                made = build(txn)
            # Only commits that landed INSIDE the shared timed window are counted. One
            # that lands after the readers stopped is real, but it is not part of this
            # measurement, and counting it would inflate the rate the reader felt.
            if in_timed_window():
                if timed_from is None:
                    timed_from = time.monotonic()
                landed = time.time()
                if first_commit_at is None:
                    first_commit_at = landed
                last_commit_at = landed
                latency[family].append((time.perf_counter() - began) * 1000.0)
                commits += 1
            else:
                outside_window += 1
            return True, made
        except GrafxError as refused:
            if refused.details.get("field") == DURABLE_FIELD:
                # Marked retryable by the product, but backed by a DURABLE on-disk flag.
                # Retrying is a spin, not a retry: record it and stop.
                durable_refusals.append({"family": family, "attempt": attempt,
                                         "error": refused.to_dict()})
                if not reopen_on_stale:
                    return False, None
                # Opt-in workaround, always visible in the report: a fresh handle re-reads
                # the index state instead of spinning on this process's stale view.
                try:
                    db.close()
                    # The reopen keeps the SAME observable sink. Reconnecting with the
                    # default would silently drop this process back to the noop sink and
                    # every counter after the first refusal would go missing.
                    db = connect(root, metrics="json", metrics_destination=metrics_out)
                    reopens += 1
                except BaseException as failed:
                    escapes.append("reopen failed: %s: %s"
                                   % (type(failed).__name__, repr(failed)[:180]))
                    return False, None
                continue
            if refused.retryable:
                conflicts += 1
                retries += 1
                last_refusal = refused.to_dict()
                if not window_is_open():
                    # The phase is over. Stop retrying rather than overrunning the window
                    # the percentiles and the throughput denominator are drawn from.
                    escapes.append("%s: deadline reached after %d retries; last refusal: %s"
                                   % (family, attempt + 1, json.dumps(last_refusal)))
                    return False, None
                time.sleep(rnd.uniform(0.0, min(0.08 * (2 ** min(attempt, 4)), 0.35)))
                continue
            escapes.append("%s: %s" % (type(refused).__name__, repr(refused)[:220]))
            return False, None
        except BaseException as escaped:
            escapes.append("%s: %s" % (type(escaped).__name__, repr(escaped)[:220]))
            return False, None
    escapes.append("%s: gave up after 60 retries; last refusal: %s"
                   % (family, json.dumps(last_refusal)))
    return False, None


made_nodes = []
index = 0
# The effect ledger: what each of the four families actually COMMITTED, so the oracle can
# certify the effect of every family instead of only the existence of a node id. A row lands
# here only after the commit that produced it succeeded.
expected_owner = {}       # id -> owner value written by the last update that committed
expected_superseded = []  # ids whose live flag a committed mark_superseded set to false
expected_edges = []       # [source id, target id] pairs a committed create_edge made
# The ordered list of committed operations, in this writer's commit order. It is what the
# serial oracle replays: section 6 asks for a serial run of the SAME operation list, and a
# ledger of final values alone cannot answer that.
operations = []

if idle:
    # The zero of the curve: hold the database open, commit nothing, occupy a process slot.
    while window_is_open():
        time.sleep(0.05)
else:
    # A PACED writer runs to the end of the shared window; the txns cap applies only to the
    # unpaced case. Otherwise a fast rate exhausts the rounds early, the writer stops, and the
    # reader spends the rest of its window with no foreign traffic at all, so the point would
    # be labelled 10/s while the reader experienced about half of that.
    while (rate or index < txns) and window_is_open():
        def build_nodes(txn, index=index):
            keys = []
            for offset in range(rows_per_txn):
                key = base + index * rows_per_txn + offset + 1
                txn.execute(
                    "CREATE (:%s {id: $i, owner: $o, body: $b, live: true})" % node_table,
                    {"i": key, "o": slot, "b": "n" * 96},
                )
                keys.append(key)
            return keys
        ok, keys = committed("create_node", build_nodes)
        if ok:
            acknowledged.extend("%s:%s" % (node_table, key) for key in keys)
            made_nodes.extend(keys)
            for key in keys:
                expected_owner["%s:%s" % (node_table, key)] = [slot, "n" * 96]
            operations.append(["create_node", node_table, list(keys), slot])

        if window_is_open() and len(made_nodes) >= 2:
            pair = (made_nodes[-2], made_nodes[-1])

            def build_edge(txn, pair=pair):
                txn.execute(
                    "MATCH (a:%s), (b:%s) WHERE a.id = $a AND b.id = $b "
                    "CREATE (a)-[:%s {w: 1}]->(b)" % (node_table, node_table, edge_table),
                    {"a": pair[0], "b": pair[1]},
                )
                return pair
            ok, _made = committed("create_edge", build_edge)
            if ok:
                expected_edges.append([edge_table, pair[0], pair[1], 1])
                operations.append(["create_edge", node_table, edge_table,
                                   pair[0], pair[1]])

        if window_is_open() and made_nodes:
            update_key, update_owner = made_nodes[-1], slot * 1000 + index

            def build_update(txn, key=update_key, owner=update_owner):
                txn.execute(
                    "MATCH (n:%s) WHERE n.id = $k SET n.owner = $o" % node_table,
                    {"k": key, "o": owner},
                )
                return key
            ok, _made = committed("update_node", build_update)
            if ok:
                # Last committed write wins -- exactly what the oracle will read back.
                expected_owner["%s:%s" % (node_table, update_key)] = [update_owner,
                                                                        "n" * 96]
                operations.append(["update_node", node_table, update_key, update_owner])

            supersede_key = made_nodes[0]

            def build_supersede(txn, key=supersede_key):
                txn.execute(
                    "MATCH (n:%s) WHERE n.id = $k SET n.live = false" % node_table,
                    {"k": key},
                )
                return key
            if window_is_open():
                ok, _made = committed("mark_superseded", build_supersede)
            else:
                ok = False
            if ok:
                operations.append(["mark_superseded", node_table, supersede_key])
                marked = "%s:%s" % (node_table, supersede_key)
                if marked not in expected_superseded:
                    expected_superseded.append(marked)
        index += 1

ended = time.monotonic()
# The TIMED window, not the wall clock: dividing timed commits by an untimed elapsed would
# understate throughput by exactly the warmup.
elapsed = (ended - timed_from) if timed_from is not None else 0.0
# How long this writer was actually present after the warmup, whether or not it committed.
# This is the span the reader's window has to be compared against: a writer that went quiet
# halfway through did not deliver the foreign rate its label claims.
active_span = max(0.0, time.time() - max(start_at, timing_starts_at))
metrics = None
metrics_error = None
try:
    snapshot = db.snapshot_metrics()
    to_dict = getattr(snapshot, "as_dict", None)
    metrics = to_dict() if callable(to_dict) else json.loads(json.dumps(
        snapshot, default=lambda item: getattr(item, "__dict__", str(item))))
except BaseException as failure:
    metrics_error = "%s: %s" % (type(failure).__name__, repr(failure)[:200])
try:
    db.publish_metrics()
except BaseException as failure:
    metrics_error = (metrics_error or "") + " publish: %s" % repr(failure)[:120]
db.close()

pathlib.Path(out).write_text(json.dumps({
    "role": "writer", "slot": slot, "regime": regime, "idle": idle,
    "target_commit_rate": rate, "warmup_seconds": warmup,
    "node_table": node_table, "edge_table": edge_table,
    "latency_ms": latency, "conflicts": conflicts, "retries": retries,
    "commits": commits, "elapsed_seconds": elapsed,
    "active_span_seconds": active_span, "rounds_completed": index,
    "commits_outside_window": outside_window,
    "first_commit_at": first_commit_at, "last_commit_at": last_commit_at,
    "phase_offset_seconds": phase_offset,
    "window": {"start_at": start_at, "end_at": end_at, "warmup": warmup},
    "durable_refusals": durable_refusals, "reopens": reopens,
    "reopen_on_stale_index": reopen_on_stale,
    "acknowledged": acknowledged, "escapes": escapes,
    "expected_owner": {str(key): value for key, value in expected_owner.items()},
    "expected_superseded": expected_superseded,
    "expected_edges": expected_edges,
    "operations": operations,
    "metrics": metrics, "metrics_error": metrics_error,
    "metrics_document": metrics_out,
}, sort_keys=True), encoding="utf-8")
'''

READER_CHILD = r'''
import json, pathlib, random, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

root, slot, seed = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
shape, seconds, tables_json = sys.argv[5], float(sys.argv[6]), sys.argv[7]
edges_json, warmup, out = sys.argv[8], float(sys.argv[9]), sys.argv[10]
metrics_out, ready_path, barrier_path = sys.argv[11], sys.argv[12], sys.argv[13]
tables = json.loads(tables_json)
edges = json.loads(edges_json)
rnd = random.Random(seed * 104729 + slot)

db = connect(root, metrics="json", metrics_destination=metrics_out)
latency_ms = []
torn = []
escapes = []      # ILLEGAL: fails the case. Non-Grafx errors AND non-retryable refusals.
refusals = []     # LEGAL: a RETRYABLE refusal, which a reader may contractually receive.
finished_because = "window_closed"


def classify(refused):
    """Record a Grafx refusal, and say whether the reader was entitled to it.

    Treating every GrafxError as legal was a false pass: a corruption report or any
    non-retryable refusal is the product failing, not a read view moving under a reader.
    """
    detail = refused.to_dict()
    detail["at"] = time.time()
    if not refused.retryable:
        escapes.append({"kind": "non_retryable_refusal", "error": detail})
        return False
    refusals.append(detail)
    return True
statements = 0
lifecycle_ms = []   # begin -> rollback/commit, the whole read transaction
statement_ms = []   # the execute alone, so the two are never conflated

pathlib.Path(ready_path).write_text(json.dumps({"role": "reader", "slot": slot}),
                                    encoding="utf-8")
waited = 0.0
while not pathlib.Path(barrier_path).is_file() and waited < 900.0:
    time.sleep(0.02)
    waited += 0.02
if not pathlib.Path(barrier_path).is_file():
    raise SystemExit("BARRIER-TIMEOUT: reader %d never saw the start barrier" % slot)
barrier = json.loads(pathlib.Path(barrier_path).read_text(encoding="utf-8"))
start_at = float(barrier["start_at"])
end_at = start_at + seconds
timing_starts_at = start_at + warmup
opening = start_at - time.time()
if opening > 0:
    time.sleep(opening)

started = time.monotonic()
timed_from = None
long_first = None
long_scans = 0


def window_is_open():
    """True while the shared measured window is still running."""
    return time.time() < end_at


def in_timed_window():
    """True once the discarded warmup is over, and only while the window is still open."""
    now = time.time()
    return timing_starts_at <= now < end_at


def record(began, lifecycle_began=None):
    """Keep one latency sample, unless it fell inside the discarded warmup.

    Section 6.5 asks for the whole begin->commit lifecycle per operation, not only the time
    inside execute(). Both are kept, separately, because a reader that spends its cost opening
    and releasing a snapshot would look free if only the statement were timed.
    """
    global statements, timed_from
    if in_timed_window():
        if timed_from is None:
            timed_from = time.monotonic()
        elapsed_statement = (time.perf_counter() - began) * 1000.0
        latency_ms.append(elapsed_statement)
        statement_ms.append(elapsed_statement)
        if lifecycle_began is not None:
            lifecycle_ms.append((time.perf_counter() - lifecycle_began) * 1000.0)
        statements += 1


def one_read(execute, table, autocommit=False):
    """One statement, timed; returns its rows.

    For an autocommit read the call IS the whole lifecycle (begin, execute, commit), so the
    two timings coincide. Inside an explicit transaction the caller passes its own begin.
    """
    began = time.perf_counter()
    rows = execute(
        "MATCH (n:%s) RETURN n.id, n.owner ORDER BY n.id LIMIT 100" % table
    ).rows
    record(began, lifecycle_began=began if autocommit else None)
    return rows


if shape == "long":
    # ONE long read transaction: its snapshot must not move under it, however much the
    # writers commit. A moved snapshot is a torn read, and it fails the case.
    long_began = time.perf_counter()
    reader = db.begin("read")
    table = tables[0]
    long_first = sorted(one_read(reader.execute, table))
    while window_is_open():
        time.sleep(0.25)
        try:
            again = sorted(one_read(reader.execute, table))
            long_scans += 1
            if again != long_first:
                torn.append({"kind": "long_snapshot_moved",
                             "first_rows": len(long_first), "now_rows": len(again)})
                finished_because = "torn"
                break
        except GrafxError as refused:
            classify(refused)
            # A long reader that stops before its window closes has not run the scenario,
            # whatever the reason. Ending early used to look identical to finishing.
            finished_because = "refused"
            break
    reader.rollback()
    # The long transaction has exactly one lifecycle, and this is it: begin to rollback,
    # spanning the whole window. Its statements stay in their own series.
    lifecycle_ms.append((time.perf_counter() - long_began) * 1000.0)
else:
    while window_is_open():
        table = rnd.choice(tables)
        try:
            if shape == "autocommit":
                # Database.execute = begin+execute+commit, the shape measure_concurrency
                # leaves outside its timers (roadmap 6.5).
                one_read(db.execute, table, autocommit=True)
                began = time.perf_counter()
                first = db.execute("MATCH (n:%s) RETURN count(*)" % table).rows[0][0]
                record(began, lifecycle_began=began)
                began = time.perf_counter()
                second = db.execute("MATCH (n:%s) RETURN count(*)" % table).rows[0][0]
                record(began, lifecycle_began=began)
                # Rows are only ever added, never deleted, so a count that SHRANK between two
                # autocommit reads is a committed row that went missing.
                if second < first:
                    torn.append({"kind": "count_went_backwards",
                                 "first": first, "second": second})
            else:
                # Traversal, read-only. The edge table pairs with the node table by position,
                # so the disjoint regime traverses ITS writer's edges rather than a name that
                # does not exist in this database.
                edge = edges[tables.index(table)] if len(edges) == len(tables) else edges[0]
                lifecycle_began = time.perf_counter()
                reader = db.begin("read")
                try:
                    began = time.perf_counter()
                    # The read-only shape of delete_edges: select exactly the edges a delete
                    # would remove, by endpoint, and remove nothing. A bare one-hop MATCH
                    # would exercise a cheaper plan than the family it stands for.
                    rows = reader.execute(
                        "MATCH (a:%s)-[r:%s]->(b:%s) WHERE a.live = true "
                        "RETURN a.id, b.id, r.w LIMIT 100" % (table, edge, table)
                    ).rows
                    record(began)
                    if len(rows) != len(set(rows)):
                        torn.append({"kind": "duplicate_rows", "rows": len(rows)})
                finally:
                    reader.rollback()
                # AFTER the rollback. Timing up to the last row read leaves the cost of
                # releasing the snapshot outside the number, which is the half a reader
                # under a busy writer is most likely to pay.
                if in_timed_window():
                    lifecycle_ms.append((time.perf_counter() - lifecycle_began) * 1000.0)
        except GrafxError as refused:
            # A RETRYABLE refusal is legal for a reader whose index view moved: counted, not
            # charged against the case. Anything else is the product failing.
            classify(refused)
        except BaseException as escaped:
            escapes.append({"kind": "non_grafx_escape",
                            "error": "%s: %s" % (type(escaped).__name__, repr(escaped)[:180])})
            finished_because = "escaped"
            break

ended = time.monotonic()
elapsed = (ended - timed_from) if timed_from is not None else 0.0
metrics = None
metrics_error = None
try:
    snapshot = db.snapshot_metrics()
    to_dict = getattr(snapshot, "as_dict", None)
    metrics = to_dict() if callable(to_dict) else json.loads(json.dumps(
        snapshot, default=lambda item: getattr(item, "__dict__", str(item))))
except BaseException as failure:
    metrics_error = "%s: %s" % (type(failure).__name__, repr(failure)[:200])
try:
    db.publish_metrics()
except BaseException as failure:
    metrics_error = (metrics_error or "") + " publish: %s" % repr(failure)[:120]
db.close()

pathlib.Path(out).write_text(json.dumps({
    "role": "reader", "slot": slot, "shape": shape, "tables_read": tables,
    "latency_ms": latency_ms, "statements": statements, "torn": torn,
    "lifecycle_ms": lifecycle_ms, "statement_ms": statement_ms,
    "window": {"start_at": start_at, "end_at": end_at, "warmup": warmup},
    "elapsed_seconds": elapsed, "escapes": escapes, "refusals": refusals,
    "long_scans": long_scans, "warmup_seconds": warmup,
    "finished_because": finished_because,
    "refusals_per_thousand_statements": (
        round(len(refusals) * 1000.0 / statements, 3) if statements else None),
    "metrics": metrics, "metrics_error": metrics_error,
    "metrics_document": metrics_out,
}, sort_keys=True), encoding="utf-8")
'''

BOOTSTRAP_CHILD = r'''
import json, sys
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect

root = sys.argv[2]
tables = json.loads(sys.argv[3])
edges = json.loads(sys.argv[4])
quiet, quiet_rows = sys.argv[5], int(sys.argv[6])

db = connect(root)
with db.begin("write") as txn:
    for name in list(tables) + [quiet]:
        txn.execute(
            "CREATE NODE TABLE %s(id INT64, owner INT64, body STRING, live BOOL, "
            "PRIMARY KEY(id))" % name
        )
with db.begin("write") as txn:
    for position, name in enumerate(edges):
        source = tables[position] if position < len(tables) else tables[0]
        txn.execute("CREATE REL TABLE %s(FROM %s TO %s, w INT64)" % (name, source, source))
    txn.execute("CREATE REL TABLE %sLinks(FROM %s TO %s, w INT64)" % (quiet, quiet, quiet))

# The quiet table is seeded ONCE and never written during the run. Its keys are acknowledged
# rows like any other, so the oracle can hold the whole database to one ledger.
seeded = []
with db.begin("write") as txn:
    for offset in range(quiet_rows):
        key = 900_000_000 + offset
        txn.execute(
            "CREATE (:%s {id: $i, owner: 0, body: 'q', live: true})" % quiet, {"i": key}
        )
        seeded.append(key)
db.close()
print(json.dumps({"ok": True, "seeded": seeded}), flush=True)
'''

ORACLE_CHILD = r'''
import json, sys
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect

root, tables_json, mode = sys.argv[2], sys.argv[3], sys.argv[4]
edges_json = sys.argv[5]
tables = json.loads(tables_json)
edge_tables = json.loads(edges_json)
db = connect(root)
stored = []
owner = {}
superseded = []
edges = []
for table in tables:
    # Every column the four families touch, so the ledger can certify each family's EFFECT
    # and not merely that a node with that id exists.
    for row in db.execute(
        "MATCH (n:%s) RETURN n.id, n.owner, n.live, n.body" % table
    ).rows:
        # Keyed BY TABLE, and carrying the properties. Flattening ids across tables meant a
        # row appearing under a different table with the same id read as unchanged.
        key = "%s:%s" % (table, int(row[0]))
        stored.append(key)
        owner[key] = [row[1], row[3]]
        if row[2] is False:
            superseded.append(key)
for position, edge_table in enumerate(edge_tables):
    source = tables[position] if position < len(tables) else tables[0]
    for row in db.execute(
        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id, r.w" % (source, edge_table)
    ).rows:
        edges.append([edge_table, int(row[0]), int(row[1]), row[2]])
report = db.verify("all")
db.close()
# `clean` is the predicate, NOT `findings == 0`: a walk that checked NOTHING also has an empty
# findings tuple, and calling that clean is how a verifier certifies a database it never looked
# at (A75.2). The coverage counts travel too, so a caller can see what was actually examined.
print(json.dumps({
    "mode": mode,
    "stored": stored,
    "owner": owner,
    "superseded": superseded,
    "edges": edges,
    "findings": len(report.findings),
    "clean": report.clean,
    "pages_checked": report.pages_checked,
    "records_checked": report.records_checked,
    "index_entries_checked": report.index_entries_checked,
}), flush=True)
'''

SERIAL_ORACLE_CHILD = r'''
import json, sys
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect

root, tables_json, edges_json = sys.argv[2], sys.argv[3], sys.argv[4]
log_path = sys.argv[5]
tables = json.loads(tables_json)
edge_tables = json.loads(edges_json)
operations = json.loads(open(log_path, encoding="utf-8").read())

# THE SAME INITIAL STATE. This opens a copy of the database as it stood after bootstrap and
# before any participant touched it, then replays the acknowledged operations one at a time
# in one process. Rebuilding an empty database here would compare two runs that started from
# different places, which is not the serial oracle section 6 asks for.
db = connect(root)

replayed = 0
for entry in operations:
    kind = entry[0]
    with db.begin("write") as txn:
        if kind == "create_node":
            _kind, table, keys, owner = entry
            for key in keys:
                txn.execute(
                    "CREATE (:%s {id: $i, owner: $o, body: $b, live: true})" % table,
                    {"i": key, "o": owner, "b": "n" * 96},
                )
        elif kind == "create_edge":
            _kind, table, edge, source, target = entry
            txn.execute(
                "MATCH (a:%s), (b:%s) WHERE a.id = $a AND b.id = $b "
                "CREATE (a)-[:%s {w: 1}]->(b)" % (table, table, edge),
                {"a": source, "b": target},
            )
        elif kind == "update_node":
            _kind, table, key, owner = entry
            txn.execute("MATCH (n:%s) WHERE n.id = $k SET n.owner = $o" % table,
                        {"k": key, "o": owner})
        elif kind == "mark_superseded":
            _kind, table, key = entry
            txn.execute("MATCH (n:%s) WHERE n.id = $k SET n.live = false" % table,
                        {"k": key})
        else:
            raise SystemExit("unknown operation in the acknowledged list: " + repr(kind))
    replayed += 1

stored = []
owner = {}
superseded = []
edges = []
for table in tables:
    for row in db.execute(
        "MATCH (n:%s) RETURN n.id, n.owner, n.live, n.body" % table
    ).rows:
        # Keyed BY TABLE, and carrying the properties. Flattening ids across tables meant a
        # row appearing under a different table with the same id read as unchanged.
        key = "%s:%s" % (table, int(row[0]))
        stored.append(key)
        owner[key] = [row[1], row[3]]
        if row[2] is False:
            superseded.append(key)
for position, edge_table in enumerate(edge_tables):
    source = tables[position] if position < len(tables) else tables[0]
    for row in db.execute(
        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id, r.w" % (source, edge_table)
    ).rows:
        edges.append([edge_table, int(row[0]), int(row[1]), row[2]])
report = db.verify("all")
db.close()

print(json.dumps({
    "mode": "serial",
    "replayed": replayed,
    "pages_checked": report.pages_checked,
    "records_checked": report.records_checked,
    "index_entries_checked": report.index_entries_checked,
    "stored": stored,
    "owner": owner,
    "superseded": superseded,
    "edges": edges,
    "findings": len(report.findings),
    "clean": report.clean,
}), flush=True)
'''

AUDITOR_CHILD = r'''
import json, os, pathlib, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
__PIN_GUARD__
from okto_grafx import connect

root, tables_json, edges_json = sys.argv[2], sys.argv[3], sys.argv[4]
ready_path, done_path, out = sys.argv[5], sys.argv[6], sys.argv[7]
tables = json.loads(tables_json)
edge_tables = json.loads(edges_json)

# THE LIVE HANDLE. This process opens BEFORE the writers start and holds the same handle open
# across the whole run, so its verification is the one a live participant would get. The cold
# pass is a different process that opens only after everyone has closed. Two fresh opens after
# the run would not distinguish those two things at all.
opened_at = time.time()
db = connect(root)
pathlib.Path(ready_path).write_text(json.dumps({"opened_at": opened_at, "pid": os.getpid()}),
                                    encoding="utf-8")

waited = 0.0
while not pathlib.Path(done_path).exists() and waited < 900.0:
    time.sleep(0.05)
    waited += 0.05
saw_done = pathlib.Path(done_path).exists()

stored = []
owner = {}
superseded = []
edges = []
for table in tables:
    for row in db.execute(
        "MATCH (n:%s) RETURN n.id, n.owner, n.live, n.body" % table
    ).rows:
        # Keyed BY TABLE, and carrying the properties. Flattening ids across tables meant a
        # row appearing under a different table with the same id read as unchanged.
        key = "%s:%s" % (table, int(row[0]))
        stored.append(key)
        owner[key] = [row[1], row[3]]
        if row[2] is False:
            superseded.append(key)
for position, edge_table in enumerate(edge_tables):
    source = tables[position] if position < len(tables) else tables[0]
    for row in db.execute(
        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id, r.w" % (source, edge_table)
    ).rows:
        edges.append([edge_table, int(row[0]), int(row[1]), row[2]])
report = db.verify("all")
db.close()

pathlib.Path(out).write_text(json.dumps({
    "mode": "live",
    "held_open_through_run": True,
    "opened_at": opened_at,
    "saw_done_flag": saw_done,
    "waited_seconds": waited,
    "pid": os.getpid(),
    "stored": stored,
    "owner": owner,
    "superseded": superseded,
    "edges": edges,
    "findings": len(report.findings),
    "clean": report.clean,
    "pages_checked": report.pages_checked,
    "records_checked": report.records_checked,
    "index_entries_checked": report.index_entries_checked,
}), encoding="utf-8")
print(json.dumps({"ok": True}), flush=True)
'''


for _name in ("WRITER_CHILD", "READER_CHILD", "BOOTSTRAP_CHILD", "ORACLE_CHILD",
              "AUDITOR_CHILD", "SERIAL_ORACLE_CHILD"):
    globals()[_name] = globals()[_name].replace("__PIN_GUARD__", PIN_GUARD)
del _name


def percentile(samples: list[float], quantile: float) -> float | None:
    """Nearest-rank percentile, or None for an empty sample -- never a stand-in zero."""
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), int(quantile * len(ordered) + 0.9999999)))
    return ordered[rank - 1]


def profile(samples: list[float]) -> dict:
    """Latency profile, or an explicit empty marker. No sample is never reported as zero."""
    if not samples:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    return {
        "n": len(samples),
        "p50": round(statistics.median(samples), 3),
        "p90": round(percentile(samples, 0.90), 3),
        "p99": round(percentile(samples, 0.99), 3),
        "max": round(max(samples), 3),
    }


def spawn(template: str, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-B", "-c", template, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def run_child(template: str, args: list[str]) -> dict:
    """Run one child to completion; a bad exit or unparseable output is a FAILURE, not {}."""
    try:
        done = subprocess.run(
            [sys.executable, "-B", "-c", template, *args],
            capture_output=True, text=True, timeout=CHILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"child_failed": True, "reason": "timeout", "stderr": ""}
    if done.returncode != 0 or not done.stdout.strip():
        return {"child_failed": True, "code": done.returncode,
                "stderr": (done.stderr or "")[-400:]}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {"child_failed": True, "code": done.returncode,
                "stderr": "unparseable stdout: " + done.stdout[-200:]}


def collect(processes: list[tuple[str, subprocess.Popen]]) -> list[dict]:
    """Reap every child; a death or timeout is recorded as a failure, never as an empty result."""
    failures: list[dict] = []
    for label, process in processes:
        try:
            _out, err = process.communicate(timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                _out, err = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                err = ""
            failures.append({"child": label, "reason": "timeout"})
            continue
        if process.returncode != 0:
            failures.append({"child": label, "code": process.returncode,
                             "stderr": (err or "")[-400:]})
    return failures


def read_metrics_document(where: str | None) -> dict | None:
    """Return the product's own metrics document for one process, if it wrote one.

    The JSON sink APPENDS one document per publication, so the file is JSON Lines rather than
    a single object. Parsing the whole file as one document fails and would silently look
    exactly like a process that emitted nothing -- which is how a real capture becomes an
    absent one in the report.
    """
    if not where:
        return None
    target = pathlib.Path(where)
    if not target.is_file():
        return None
    documents = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.strip():
                documents.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as failure:
        return {"unreadable": f"{type(failure).__name__}: {failure}",
                "path": str(target), "bytes": target.stat().st_size}
    if not documents:
        return None
    # The last publication is the end-of-run state; the count says how many there were.
    return {"publications": len(documents), "final": documents[-1]}


def gather_metrics(reports: list[dict]) -> dict:
    """Fold what the product actually emitted; name what it does not, with the reason.

    Every process's snapshot is kept, labelled by role and slot. F1 asks for per-process
    counters, and keeping only the first one would answer a different question -- one writer's
    view standing in for the fleet.
    """
    errors = [{"role": report.get("role"), "slot": report.get("slot"),
               "error": report["metrics_error"]}
              for report in reports if report.get("metrics_error")]
    per_process = [{"role": report.get("role"), "slot": report.get("slot"),
                    "metrics": report["metrics"],
                    "document": read_metrics_document(report.get("metrics_document"))}
                   for report in reports if report.get("metrics")]
    return {
        "captured": {
            "per_process_snapshots": len(per_process),
            "processes_reporting_nothing": len(reports) - len(per_process) - len(errors),
            "by_process": per_process,
        },
        "capture_errors": errors or None,
        "unavailable": dict(UNAVAILABLE_METRICS),
    }


def official_shortfalls(opts: argparse.Namespace, board: dict | None = None) -> list[str]:
    """Return every reason this run is not the official one; empty means it is.

    official = bool(board_template) was too permissive. A run on a copied board is still not
    the official run if nobody vouched the machine was idle, if the digest was never checked
    against an expected value, or if the measured checkout was dirty -- each of those makes
    the number unattributable, which is the same as not having it.
    """
    unmet = []
    if not opts.board_template:
        unmet.append(
            "synthetic board: section 6 requires the official run to start from a relocated "
            "copy of a real board (--board-template)"
        )
    elif not opts.board_digest:
        unmet.append(
            "--board-digest was not given, so the template was never authenticated against "
            "an expected content digest"
        )
    elif board is not None:
        # Requiring the option to EXIST authenticated nothing. A wrong digest, or a copy that
        # came out different from its template, has to be fail-closed.
        if board.get("matches_expected_digest") is not True:
            unmet.append(
                f"the template digest {board.get('template_digest', {}).get('digest')} does "
                f"not match the expected {opts.board_digest}"
            )
        if board.get("copy_is_identical") is not True:
            unmet.append("the relocated copy is not byte-identical to its template")
    if not opts.machine_idle_asserted:
        unmet.append("--machine-idle-asserted was not given (H5)")
    if opts.case != "matrix":
        unmet.append(f"--case {opts.case} is a subcase; the official run is the full frozen "
                     "matrix")
    overridden = [name for name, value in (
        ("--writers", opts.writers), ("--readers", opts.readers),
        ("--regime", opts.regime), ("--reader-shape", opts.reader_shape),
        ("--reader-target", opts.reader_target),
        ("--foreign-commit-rate", opts.foreign_commit_rate),
    ) if value is not None]
    if overridden:
        unmet.append("frozen dimensions were overridden: " + ", ".join(overridden))
    if opts.reopen_on_stale_index:
        unmet.append("--reopen-on-stale-index works around a product refusal, so the run "
                     "does not measure the product as it stands")
    source = describe_repository(pathlib.Path(opts.src).resolve())
    if not source.get("src_tests_clean"):
        unmet.append("the measured checkout has uncommitted changes under src/ or tests/")
    if not source.get("commit"):
        unmet.append("the measured source root is not inside a git checkout, so it has no pin")
    return unmet


def plan_tables(writers: int, regime: str, reader_target: str) -> tuple[list, list, list]:
    """Return (writer node tables, writer edge tables, tables the readers read)."""
    if regime == "hot":
        tables, edges = ["Item"], ["Links"]
    else:
        tables = [f"Item{slot}" for slot in range(1, writers + 1)]
        edges = [f"Links{slot}" for slot in range(1, writers + 1)]
    # The CE-3 discriminant: read a table nobody writes, or read exactly what is being written.
    read_tables = [QUIET_TABLE] if reader_target == "unrelated-table" else list(tables)
    read_edges = [f"{QUIET_TABLE}Links"] if reader_target == "unrelated-table" else list(edges)
    return tables, edges, [read_tables, read_edges]


def evaluate(*, readers: int, rate: float | None, reopen_workaround: bool,
             child_failures: list, missing: list, live: dict, cold: dict,
             acknowledged, stored_list: list, torn: list, escapes: list,
             durable: list, reopens: int, commits: int, statements: int,
             expected_owner: dict | None = None,
             expected_superseded: list | None = None,
             expected_edges: list | None = None,
             writer_span: float = 0.0, reader_window: float = 0.0,
             commits_outside_window: int = 0, serial: dict | None = None,
             serial_operations: int = 0, board: dict | None = None,
             readers_stopped_early: list | None = None, writers: int = 0,
             participants_never_ready: list | None = None, metrics: dict | None = None,
             family_gaps: list | None = None,
             commit_span: tuple | None = None) -> list[dict]:
    """Judge one cell from its collected observations, and return the criteria list.

    This is deliberately a PURE function of the observations. The scenarios that matter most
    -- a dead child, a torn read, a durable index refusal -- are the ones a short run produces
    only intermittently, so the judgement has to be drivable from synthetic inputs or it can
    never be proven to work. A criterion that has never once been seen to fail is not evidence.
    """
    oracle_ok = not live.get("child_failed")
    acknowledged = set(acknowledged)
    stored = set(stored_list) if oracle_ok else set()
    lost = sorted(acknowledged - stored) if oracle_ok else []
    phantom = sorted(stored - acknowledged) if oracle_ok else []
    duplicates = (len(stored_list) - len(stored)) if oracle_ok else None

    criteria: list[dict] = [
        {"name": "every_participant_reached_the_barrier",
         "pass": not (participants_never_ready or []),
         "observed": participants_never_ready or [],
         "bound": "every writer and reader signalled ready before the window opened; one "
                  "that did not was measured over a window it never fully saw"},
        {"name": "every_child_completed",
         "pass": not child_failures and not missing,
         "observed": {"failures": child_failures, "missing": missing},
         "bound": "no death, timeout or missing report"},
        {"name": "oracle_ran",
         "pass": oracle_ok and not cold.get("child_failed"),
         "observed": {"live_failed": bool(live.get("child_failed")),
                      "cold_failed": bool(cold.get("child_failed"))},
         "bound": "both oracle passes produced a report"},
    ]
    # A case in which nothing happened would otherwise pass every "nothing bad happened"
    # check vacuously. These two criteria are what refuse to call that a success.
    if rate == 0.0:
        criteria.append({"name": "commanded_idle_held", "pass": commits == 0,
                         "observed": commits,
                         "bound": "0 commits, because rate 0 commands an idle writer"})
    else:
        criteria.append({"name": "writers_did_work", "pass": commits > 0,
                         "observed": commits, "bound": "> 0 timed commits"})
    criteria.append({"name": "readers_did_work",
                     "pass": statements > 0 if readers else statements == 0,
                     "observed": statements,
                     "bound": "> 0 timed statements" if readers else "no readers, so 0"})
    # The curve only means something if the foreign traffic was present for the whole window
    # the reader was measured over. A writer that exhausted its rounds at the halfway mark
    # leaves the reader alone for the rest, and the point carries a rate nobody experienced.
    if rate and reader_window > 0:
        expected_commits = rate * reader_window
        # One slot of quantisation per writer, and not a percentage anybody invented: a
        # writer can be at most one pacing period short or long inside the window.
        allowed = max(1, writers)
        delivered = abs(commits - expected_commits) <= allowed
        criteria.append({
            "name": "the_labelled_rate_was_delivered",
            "pass": delivered,
            "observed": {"labelled_rate": rate, "commits": commits,
                         "expected_commits": round(expected_commits, 3),
                         "allowed_slack_commits": allowed,
                         "effective_rate": round(commits / reader_window, 3)},
            "bound": "commits within one pacing slot per writer of rate * window; a point "
                     "that delivered a different rate carries a label nobody experienced",
        })
    if rate and readers and reader_window > 0:
        # Coverage from the REAL first and last commit, not from a nominal span: staggered
        # writers start at different instants and the nominal figure hides a gap.
        if commit_span and commit_span[0] is not None and commit_span[1] is not None:
            observed_span = commit_span[1] - commit_span[0]
            measured_from = "first and last aggregate commit"
        else:
            observed_span = writer_span
            measured_from = "nominal writer span"
        # A paced writer cannot commit at the very first and very last instant of the window:
        # at 1/s over 5s the commits land at 0,1,2,3,4 and the span is 4s however healthy the
        # run. One aggregate period is the quantisation the pacing itself creates, so the
        # window it is measured against is shortened by exactly that -- not by a percentage.
        period = 1.0 / rate
        reachable = max(period, reader_window - period)
        coverage = min(1.0, observed_span / reachable)
        criteria.append({
            "name": "foreign_traffic_covered_the_reader_window",
            "pass": coverage >= 0.9,
            "observed": {
                "coverage": round(coverage, 3),
                "measured_from": measured_from,
                "reachable_span_seconds": round(reachable, 3),
                "one_pacing_period_seconds": round(period, 3),
                "first_commit_at": commit_span[0] if commit_span else None,
                "last_commit_at": commit_span[1] if commit_span else None,
                "writer_span_seconds": round(writer_span, 3),
                "reader_window_seconds": round(reader_window, 3),
                "labelled_rate": rate,
                "effective_rate_over_reader_window": round(commits / reader_window, 3),
            },
            "bound": ">= 0.9 of the REACHABLE window had a writer present, where the "
                     "reachable window is the reader window less one pacing period; below "
                     "that there is a real gap, not an edge effect",
        })
    # A commit that landed after the readers stopped is a real commit, but it is not part
    # of this measurement. Counting it would inflate the rate the reader is said to have felt,
    # and the fix is to refuse the point rather than to invent a tolerance for it.
    criteria.append({
        "name": "no_commit_landed_outside_the_window",
        "pass": commits_outside_window == 0,
        "observed": commits_outside_window,
        "bound": "0 commits outside the shared measured window",
    })
    if board is not None and not board.get("synthetic"):
        criteria.append({
            "name": "board_copy_is_authentic",
            "pass": (board.get("matches_expected_digest") is True
                     and board.get("copy_is_identical") is True),
            "observed": {
                "expected_digest": board.get("expected_digest"),
                "template_digest": (board.get("template_digest") or {}).get("digest"),
                "matches_expected_digest": board.get("matches_expected_digest"),
                "copy_is_identical": board.get("copy_is_identical"),
            },
            "bound": "the template matches the digest recorded beforehand AND the relocated "
                     "copy is byte-identical to it",
        })
    if rate and family_gaps is not None:
        criteria.append({
            "name": "every_writer_sampled_every_family",
            "pass": not family_gaps,
            "observed": family_gaps[:6],
            "bound": "each writer produced at least one timed sample per family; an empty "
                     "profile cannot certify the dimension it is named after",
        })
    criteria.append({"name": "no_acknowledged_row_lost", "pass": oracle_ok and not lost,
                     "observed": lost[:8], "bound": "acknowledged is a subset of stored"})
    criteria.append({"name": "no_phantom_row", "pass": oracle_ok and not phantom,
                     "observed": phantom[:8], "bound": "stored is a subset of acknowledged"})
    criteria.append({"name": "no_duplicate_row", "pass": duplicates == 0,
                     "observed": duplicates, "bound": "0 duplicate ids"})
    # A reader that ended before its window closed did not run the scenario. Ending early
    # used to be indistinguishable from finishing, so a refusal on the second scan of a long
    # read could end the reader and still pass.
    criteria.append({
        "name": "every_reader_ran_its_whole_window",
        "pass": not (readers_stopped_early or []),
        "observed": (readers_stopped_early or [])[:4],
        "bound": "every reader finished because its window closed, not because it was "
                 "refused, torn or thrown out",
    })
    if metrics is not None:
        captured = metrics.get("captured", {})
        by_process = captured.get("by_process", [])
        documents = [entry for entry in by_process if entry.get("document")]
        expected_processes = writers + readers
        criteria.append({
            "name": "every_process_published_real_metrics",
            "pass": (not metrics.get("capture_errors")
                     and len(by_process) >= expected_processes
                     and len(documents) >= expected_processes
                     and all(entry["document"].get("final", {}).get("metrics")
                             for entry in documents)),
            "observed": {"expected_processes": expected_processes,
                         "snapshots": len(by_process),
                         "documents": len(documents),
                         "capture_errors": metrics.get("capture_errors")},
            "bound": "every writer and reader published a real, non-empty metrics document; "
                     "counters the product does not emit stay explicitly unavailable",
        })
    criteria.append({"name": "no_torn_read", "pass": not torn, "observed": torn[:4],
                     "bound": "0 torn observations"})
    criteria.append({"name": "no_non_grafx_escape", "pass": not escapes,
                     "observed": escapes[:4],
                     "bound": "0 escapes (Grafx refusals are counted separately)"})
    # A durable index refusal is not a flaky conflict: INDEX_FLAG_STALE lives on disk, so a
    # writer that hits it cannot make progress by retrying. It fails the case BY NAME unless
    # the operator explicitly asked for the reopen workaround.
    criteria.append({"name": "no_durable_index_refusal",
                     "pass": not durable or reopen_workaround,
                     "observed": {"count": len(durable), "sample": durable[:2],
                                  "reopens": reopens,
                                  "workaround_enabled": reopen_workaround},
                     "bound": "0 refusals with field=index_view_unavailable, unless "
                              "--reopen-on-stale-index was explicitly requested"})
    for label, report in (("live", live), ("cold", cold)):
        criteria.append({
            "name": f"verify_clean_{label}",
            "pass": report.get("clean") is True,
            "observed": {key: report.get(key) for key in
                         ("clean", "findings", "pages_checked", "records_checked",
                          "index_entries_checked")},
            "bound": "report.clean is True -- a walk that checked NOTHING also has no "
                     "findings, and calling that clean certifies a database nobody read "
                     "(A75.2)",
        })

    # The live pass has to have been genuinely live. Two fresh opens after everyone closed
    # would be the same measurement under two names, and the criterion below is what stops
    # that from ever passing silently.
    criteria.append({
        "name": "live_pass_used_a_handle_held_through_the_run",
        "pass": bool(live.get("held_open_through_run")) and bool(
            live.get("opened_before_writers")) and bool(live.get("saw_done_flag")),
        "observed": {key: live.get(key) for key in
                     ("held_open_through_run", "opened_before_writers",
                      "opened_before_end_of_run", "saw_done_flag", "pid", "opened_at",
                      "writers_started_at", "done_flag_written_at", "waited_seconds")},
        "bound": "the live auditor opened BEFORE THE FIRST WRITER (not merely before the end "
                 "of the run, which any reopen satisfies), held one handle across the run, "
                 "and was released by the done flag rather than by a timeout",
    })

    # --- the effect of each of the four writer families, not merely a node id -------------
    if expected_owner is not None:
        observed_owner = live.get("owner", {}) if oracle_ok else {}
        wrong = {key: {"expected": value, "observed": observed_owner.get(key)}
                 for key, value in expected_owner.items()
                 if observed_owner.get(key) != value}
        criteria.append({
            "name": "update_node_effect_stored",
            "pass": oracle_ok and not wrong,
            "observed": dict(list(wrong.items())[:4]) or {"checked": len(expected_owner)},
            "bound": "every committed owner value is the one stored",
        })
    if expected_superseded is not None:
        observed_superseded = set(live.get("superseded", [])) if oracle_ok else set()
        missing_flags = sorted(set(expected_superseded) - observed_superseded)
        surprise_flags = sorted(observed_superseded - set(expected_superseded))
        criteria.append({
            "name": "mark_superseded_effect_stored",
            "pass": oracle_ok and not missing_flags and not surprise_flags,
            "observed": {"not_superseded": missing_flags[:4],
                         "superseded_without_a_commit": surprise_flags[:4],
                         "checked": len(expected_superseded)},
            "bound": "live is false for exactly the rows a committed mark_superseded named",
        })
    if expected_edges is not None:
        observed_edges = sorted(map(list, live.get("edges", []))) if oracle_ok else []
        wanted = sorted(map(list, expected_edges))
        criteria.append({
            "name": "create_edge_effect_stored",
            "pass": oracle_ok and observed_edges == wanted,
            "observed": {"expected": len(wanted), "stored": len(observed_edges),
                         "missing": [pair for pair in wanted
                                     if pair not in observed_edges][:4],
                         "unexpected": [pair for pair in observed_edges
                                        if pair not in wanted][:4]},
            "bound": "the stored edges are exactly the pairs a committed create_edge made",
        })

    # THE SERIAL ORACLE: the same acknowledged operations, run one at a time in one process.
    # If the concurrent database disagrees with that, the protocol produced an answer no
    # serial execution could have produced, which is the definition of a wrong answer.
    if serial is not None:
        ran = not serial.get("child_failed")
        differences = {}
        if ran and oracle_ok:
            for field in ("stored", "superseded"):
                concurrent = sorted(live.get(field, []))
                replayed = sorted(serial.get(field, []))
                if concurrent != replayed:
                    differences[field] = {
                        "only_concurrent": sorted(set(concurrent) - set(replayed))[:6],
                        "only_serial": sorted(set(replayed) - set(concurrent))[:6],
                    }
            if live.get("owner") != serial.get("owner"):
                wrong_owner = {key: {"concurrent": value,
                                     "serial": serial.get("owner", {}).get(key)}
                               for key, value in (live.get("owner") or {}).items()
                               if serial.get("owner", {}).get(key) != value}
                differences["owner"] = dict(list(wrong_owner.items())[:6])
            concurrent_edges = sorted(map(list, live.get("edges", [])))
            replayed_edges = sorted(map(list, serial.get("edges", [])))
            if concurrent_edges != replayed_edges:
                differences["edges"] = {"concurrent": len(concurrent_edges),
                                        "serial": len(replayed_edges)}
        criteria.append({
            "name": "serial_oracle_agrees_on_every_answer",
            "pass": ran and oracle_ok and not differences,
            "observed": {"replayed_operations": serial.get("replayed"),
                         "operations_supplied": serial_operations,
                         "child_failed": bool(serial.get("child_failed")),
                         "differences": differences},
            "bound": "0 wrong answers: replaying the acknowledged operation list serially "
                     "reproduces the concurrent database exactly",
        })
        # At rate 0 the writer is COMMANDED idle, so an empty list is the right answer and
        # not an empty measurement. Everywhere else an empty list would mean the oracle
        # certified nothing while appearing to agree with everything.
        expected_some = rate != 0.0
        criteria.append({
            "name": "serial_oracle_replayed_every_acknowledged_operation",
            "pass": (ran and serial.get("replayed") == serial_operations
                     and (serial_operations > 0 if expected_some else serial_operations == 0)),
            "observed": {"replayed": serial.get("replayed"),
                         "supplied": serial_operations,
                         "operations_expected": "some" if expected_some
                         else "none, the writer was commanded idle"},
            "bound": ("every acknowledged operation was replayed, and there was at least one"
                      if expected_some else
                      "no operations, because rate 0 commands an idle writer"),
        })

    if serial is not None:
        criteria.append({
            "name": "verify_clean_serial",
            "pass": serial.get("clean") is True,
            "observed": {key: serial.get(key) for key in
                         ("clean", "findings", "pages_checked", "records_checked",
                          "index_entries_checked")},
            "bound": "the serially replayed database verifies clean with real coverage "
                     "(A75.2), so it is fit to be compared against",
        })
        baseline = serial.get("initial_state") or {}
        if baseline:
            criteria.append({
                "name": "serial_started_from_the_same_state",
                "pass": baseline.get("identical") is True,
                "observed": {"identical": baseline.get("identical"),
                             "source_digest": (baseline.get("source") or {}).get("digest"),
                             "copy_digest": (baseline.get("copy") or {}).get("digest")},
                "bound": "the serial replay began from a byte-identical copy of the state "
                         "the concurrent run began from",
            })

    # The two passes look at the same database from two different vantage points. If they
    # disagree, one of them is wrong and the run cannot certify anything.
    if oracle_ok and not cold.get("child_failed"):
        agree = (sorted(live.get("stored", [])) == sorted(cold.get("stored", []))
                 and live.get("owner") == cold.get("owner")
                 and sorted(live.get("superseded", [])) == sorted(
                     cold.get("superseded", []))
                 and sorted(map(list, live.get("edges", []))) == sorted(
                     map(list, cold.get("edges", []))))
        criteria.append({
            "name": "live_and_cold_ledgers_agree",
            "pass": agree,
            "observed": {"live_rows": len(live.get("stored", [])),
                         "cold_rows": len(cold.get("stored", [])),
                         "live_edges": len(live.get("edges", [])),
                         "cold_edges": len(cold.get("edges", []))},
            "bound": "the live handle and a fresh reopen report the same ledger",
        })
    return criteria


def per_writer_rate(rate: float | None, writers: int) -> float | None:
    """Split an AGGREGATE target rate across the writers that will produce it.

    The frozen curve {0, 1/s, 10/s} is the foreign commit rate the READER experiences. Handing
    the same figure to each of N writers would deliver N x rate and label it rate, so the
    N=1 and N=8 rows of the matrix would not be comparable at the same point of the curve.
    """
    if rate is None:
        return None
    if writers <= 0:
        return 0.0
    return rate / writers


def run_case(opts: argparse.Namespace, writers: int, readers: int, regime: str, shape: str,
             reader_target: str, rate: float | None) -> dict:
    """One matrix cell: N writers x M readers, one regime, shape, reader target and rate."""
    if opts.workspace:
        parent = pathlib.Path(opts.workspace)
        parent.mkdir(parents=True, exist_ok=True)
        container = pathlib.Path(tempfile.mkdtemp(prefix=f"grafx-f1-{regime}-",
                                                  dir=str(parent)))
    else:
        container = pathlib.Path(tempfile.mkdtemp(prefix=f"grafx-f1-{regime}-"))
    # The database gets a directory of its own. Writing the instrument's own reports, ready
    # files and flags INSIDE it would put foreign entries in the storage namespace under
    # measurement -- and directory listing is one of the things being measured.
    root = container / "db"
    reports_dir = container / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    board_manifest = None
    if opts.board_template:
        # Section 6, imported by 6.5: the official run works on a RELOCATED COPY of a real
        # board, never on the original. The copy is hashed so it can be proven identical.
        template = pathlib.Path(opts.board_template).resolve()
        refuse_a_certified_board(template)
        refuse_a_certified_board(container)
        source_digest = hash_board(template)
        shutil.copytree(template, root)
        copy_digest = hash_board(root)
        board_manifest = {
            "template": str(template),
            "template_digest": source_digest,
            "copy_digest": copy_digest,
            "copy_is_identical": copy_digest["digest"] == source_digest["digest"],
            "expected_digest": opts.board_digest,
            "matches_expected_digest": (None if not opts.board_digest
                                        else source_digest["digest"] == opts.board_digest),
        }
    else:
        root.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[str, subprocess.Popen]] = []
    criteria: list[dict] = []
    done_path = reports_dir / "auditor-done.flag"
    barrier_path = reports_dir / "start-barrier.json"
    unmet = official_shortfalls(opts, board_manifest)
    identity = {
        "official": not unmet,
        "not_official_because": unmet or None,
        "writers": writers, "readers": readers, "regime": regime, "reader_shape": shape,
        "reader_target": reader_target,
        "foreign_commit_rate": rate,
        "foreign_commit_rate_is_aggregate": True,
        "foreign_commit_rate_per_writer": per_writer_rate(rate, writers),
        "foreign_commit_rate_meaning": (
            "unpaced (flat out)" if rate is None
            else "commanded idle: zero commits" if rate == 0.0
            else f"{rate}/s AGGREGATE across {writers} writer(s), so "
                 f"{per_writer_rate(rate, writers)}/s each"
        ),
    }
    try:
        tables, edges, (read_tables, read_edges) = plan_tables(writers, regime, reader_target)
        made = run_child(BOOTSTRAP_CHILD, [
            opts.src, str(root), json.dumps(tables), json.dumps(edges),
            QUIET_TABLE, str(QUIET_SEED_ROWS),
        ])
        if made.get("child_failed"):
            criteria.append({"name": "bootstrap", "pass": False, "observed": made,
                             "bound": "schema created and quiet table seeded"})
            return {**identity, "criteria": criteria, "pass": False}
        seeded = made.get("seeded", [])
        # Copied while every bootstrap handle is closed and before any participant opens the
        # database, so it IS the state the concurrent run began from. Hashed both ways so the
        # claim is checkable rather than asserted.
        baseline_root = container / "serial-db"
        shutil.copytree(root, baseline_root)
        baseline_manifest = {
            "source": hash_board(root),
            "copy": hash_board(baseline_root),
        }
        baseline_manifest["identical"] = (
            baseline_manifest["source"]["digest"] == baseline_manifest["copy"]["digest"]
        )

        # The LIVE auditor opens before any writer starts and holds that one handle across the
        # whole run. Without it, "live" and "cold" would both be fresh opens after everybody
        # closed -- two names for the same measurement.
        ready_path = reports_dir / "auditor-ready.json"
        auditor_report = reports_dir / "auditor.json"
        auditor = spawn(AUDITOR_CHILD, [
            opts.src, str(root), json.dumps(tables + [QUIET_TABLE]),
            json.dumps(edges + [f"{QUIET_TABLE}Links"]),
            str(ready_path), str(done_path), str(auditor_report),
        ])
        processes.append(("auditor", auditor))
        opened = None
        for _attempt in range(int(CHILD_TIMEOUT / 0.05)):
            if ready_path.exists():
                opened = json.loads(ready_path.read_text(encoding="utf-8"))
                break
            if auditor.poll() is not None:
                break
            time.sleep(0.05)

        # Stamped immediately before the FIRST writer is spawned. Comparing the auditor's open
        # against the end of the run would only prove it opened before the end, which every
        # post-hoc reopen also satisfies.
        # The writers have to be present for the WHOLE window the readers are measured over.
        # With shape=long the reader runs for long_reader_seconds, so writers bounded by
        # --seconds would leave the tail of the official cell with no foreign traffic.
        # EXACTLY the reader's window, not max(): with --seconds greater than
        # --long-reader-seconds the writers would outlive the readers and the denominators
        # would part company again.
        writer_seconds = (opts.long_reader_seconds if (readers and shape == "long")
                          else opts.seconds)
        writers_started_at = time.time()
        for slot in range(1, writers + 1):
            processes.append((f"writer-{slot}", spawn(WRITER_CHILD, [
                opts.src, str(root), str(slot), str(opts.seed), regime,
                str(writer_seconds), str(opts.rows_per_txn), str(opts.txns_per_writer),
                "" if rate is None else str(per_writer_rate(rate, writers)),
                str(opts.warmup_seconds),
                "1" if opts.reopen_on_stale_index else "0",
                str(reports_dir / f"writer-{slot}.json"),
                str(reports_dir / f"metrics-writer-{slot}.json"),
                str(reports_dir / f"ready-writer-{slot}.json"), str(barrier_path),
                # One aggregate period spread across the writers, so the commits arrive
                # evenly instead of N at a time followed by silence.
                str(0.0 if not rate else (slot - 1) / rate),
            ])))
        for slot in range(1, readers + 1):
            duration = opts.long_reader_seconds if shape == "long" else opts.seconds
            processes.append((f"reader-{slot}", spawn(READER_CHILD, [
                opts.src, str(root), str(slot), str(opts.seed), shape, str(duration),
                json.dumps(read_tables), json.dumps(read_edges), str(opts.warmup_seconds),
                str(reports_dir / f"reader-{slot}.json"),
                str(reports_dir / f"metrics-reader-{slot}.json"),
                str(reports_dir / f"ready-reader-{slot}.json"), str(barrier_path),
            ])))

        # Every participant is up and waiting. Opening the window now, from one place, is
        # what makes the per-process numbers comparable: spawning is sequential, and a
        # process that started measuring at spawn would carry that stagger into the curve.
        expected_ready = ([f"ready-writer-{slot}.json" for slot in range(1, writers + 1)]
                          + [f"ready-reader-{slot}.json" for slot in range(1, readers + 1)])
        barrier_deadline = time.monotonic() + CHILD_TIMEOUT
        missing_ready = list(expected_ready)
        while missing_ready and time.monotonic() < barrier_deadline:
            missing_ready = [name for name in expected_ready
                             if not (reports_dir / name).is_file()]
            if not missing_ready:
                break
            if any(process.poll() is not None for label, process in processes
                   if label != "auditor"):
                break
            time.sleep(0.02)
        window_starts_at = time.time() + 0.25
        window_ends_at = window_starts_at + writer_seconds
        barrier_path.write_text(json.dumps({
            "start_at": window_starts_at, "end_at": window_ends_at,
            "warmup": opts.warmup_seconds, "participants": len(expected_ready),
            "ready_before_start": not missing_ready,
        }), encoding="utf-8")

        # Reap the writers and readers first; the auditor is still holding its handle open.
        participants = [entry for entry in processes if entry[0] != "auditor"]
        child_failures = collect(participants)
        done_written_at = time.time()
        done_path.write_text("done", encoding="utf-8")
        child_failures.extend(collect([("auditor", auditor)]))
        processes.clear()

        writer_reports, reader_reports, missing = [], [], []
        for slot in range(1, writers + 1):
            target = reports_dir / f"writer-{slot}.json"
            if target.exists():
                writer_reports.append(json.loads(target.read_text(encoding="utf-8")))
            else:
                missing.append(f"writer-{slot}")
        for slot in range(1, readers + 1):
            target = reports_dir / f"reader-{slot}.json"
            if target.exists():
                reader_reports.append(json.loads(target.read_text(encoding="utf-8")))
            else:
                missing.append(f"reader-{slot}")

        # --- the two oracles, and they are genuinely different -----------------------------
        # live: the auditor's handle, opened before the writers and never reopened.
        # cold: a brand new process, opened only after every participant has closed.
        every_table = list(tables) + [QUIET_TABLE]
        every_edge = list(edges) + [f"{QUIET_TABLE}Links"]
        if auditor_report.exists():
            live = json.loads(auditor_report.read_text(encoding="utf-8"))
            live["writers_started_at"] = writers_started_at
            live["done_flag_written_at"] = done_written_at
            # BOTH comparisons are recorded, and the criterion demands the strict one. The
            # loose one is satisfied by any post-hoc reopen, so keeping it visible is what
            # lets a reader see which question was actually answered.
            live["opened_before_writers"] = bool(
                opened and opened.get("opened_at", 0) < writers_started_at
            )
            live["opened_before_end_of_run"] = bool(
                opened and opened.get("opened_at", 0) < done_written_at
            )
        else:
            live = {"child_failed": True, "reason": "the live auditor produced no report"}
        cold = run_child(ORACLE_CHILD, [opts.src, str(root), json.dumps(every_table),
                                        "cold", json.dumps(every_edge)])

        # THE SERIAL ORACLE. Section 6 asks for a serial run of the same operation list, not
        # a comparison of final ids. The acknowledged operations are replayed one at a time
        # into a fresh database and the whole resulting state is compared.
        #
        # Concatenating the per-writer logs in slot order IS a valid serialisation: every
        # writer owns a disjoint key range (base = slot * 1_000_000) and only ever touches its
        # own keys, even in the hot-table regime where the tables are shared. Operations of
        # different writers therefore commute, while each writer's own order is preserved by
        # the concatenation. test_the_serial_replay_order_is_a_valid_serialisation pins that
        # premise so it cannot quietly stop being true.
        serial_log = []
        for report in sorted(writer_reports, key=lambda item: item["slot"]):
            serial_log.extend(report.get("operations", []))
        log_path = reports_dir / "serial-operations.json"
        log_path.write_text(json.dumps(serial_log), encoding="utf-8")
        # The serial replay has to START WHERE THE CONCURRENT RUN STARTED. Rebuilding an
        # empty database and recreating the schema compares two runs that began from
        # different states, which is not the serial oracle section 6 asks for. The
        # post-bootstrap state was copied aside before any participant opened it, and the
        # serial child merely opens that copy and replays the log.
        serial_root = container / "serial-db"
        serial = run_child(SERIAL_ORACLE_CHILD, [
            opts.src, str(serial_root), json.dumps(every_table),
            json.dumps(every_edge), str(log_path),
        ])
        serial["initial_state"] = baseline_manifest

        acknowledged = {f"{QUIET_TABLE}:{key}" for key in seeded}
        expected_owner: dict[str, object] = {f"{QUIET_TABLE}:{key}": [0, "q"]
                                             for key in seeded}
        expected_superseded: list = []
        expected_edges: list = []
        for report in writer_reports:
            acknowledged.update(report.get("acknowledged", []))
            expected_owner.update(report.get("expected_owner", {}))
            expected_superseded.extend(report.get("expected_superseded", []))
            expected_edges.extend(report.get("expected_edges", []))
        stored_list = live.get("stored", []) if not live.get("child_failed") else []
        torn = [entry for report in reader_reports for entry in report.get("torn", [])]
        escapes = [entry for report in writer_reports + reader_reports
                   for entry in report.get("escapes", [])]
        refusals = [entry for report in reader_reports for entry in report.get("refusals", [])]
        stopped_early = [{"slot": report["slot"], "why": report.get("finished_because")}
                         for report in reader_reports
                         if report.get("finished_because") != "window_closed"]
        durable = [entry for report in writer_reports
                   for entry in report.get("durable_refusals", [])]
        reopens = sum(report.get("reopens", 0) for report in writer_reports)

        commits = sum(report.get("commits", 0) for report in writer_reports)
        statements = sum(report.get("statements", 0) for report in reader_reports)

        # ONE denominator for everybody. Per-process elapsed times differ by scheduling
        # noise, and dividing each process by its own would let a slow starter report a
        # higher rate than it delivered.
        global_window = max(0.0, window_ends_at - (window_starts_at + opts.warmup_seconds))
        writer_window = global_window
        reader_window = global_window
        writer_span = max([report.get("active_span_seconds", 0.0)
                           for report in writer_reports] or [0.0])
        folded_metrics = gather_metrics(writer_reports + reader_reports)
        # A writer that produced no sample for a family cannot certify that family, so the
        # gap is named per writer rather than shown as an empty profile.
        family_gaps = [
            {"writer": report["slot"], "family": family}
            for report in writer_reports
            for family in FROZEN_FAMILIES
            if not report["latency_ms"].get(family)
        ] if rate else []
        first_commits = [report["first_commit_at"] for report in writer_reports
                         if report.get("first_commit_at") is not None]
        last_commits = [report["last_commit_at"] for report in writer_reports
                        if report.get("last_commit_at") is not None]
        commit_span = (min(first_commits) if first_commits else None,
                       max(last_commits) if last_commits else None)
        criteria = evaluate(
            readers=readers, rate=rate,
            reopen_workaround=bool(opts.reopen_on_stale_index),
            child_failures=child_failures, missing=missing, live=live, cold=cold,
            acknowledged=acknowledged, stored_list=stored_list, torn=torn,
            escapes=escapes, durable=durable, reopens=reopens,
            commits=commits, statements=statements,
            expected_owner=expected_owner, expected_superseded=expected_superseded,
            expected_edges=expected_edges, serial=serial,
            serial_operations=len(serial_log), board=board_manifest,
            writer_span=writer_span, reader_window=reader_window,
            commits_outside_window=sum(report.get("commits_outside_window", 0)
                                       for report in writer_reports),
            readers_stopped_early=stopped_early, writers=writers,
            participants_never_ready=missing_ready, metrics=folded_metrics,
            family_gaps=family_gaps, commit_span=commit_span,
        )

        return {
            **identity,
            "throughput": {
                "commits_per_second": round(commits / writer_window, 3)
                if writer_window else None,
                "reader_statements_per_second": round(statements / reader_window, 3)
                if reader_window else None,
                "shared_timed_window_seconds": round(global_window, 3),
                "writer_timed_window_seconds": round(writer_window, 3),
                "reader_timed_window_seconds": round(reader_window, 3),
                "commits_landing_outside_the_window": sum(
                    report.get("commits_outside_window", 0) for report in writer_reports),
                "writer_active_span_seconds": round(writer_span, 3),
                # The rate the READER actually experienced, over the reader's own window --
                # not the writer's self-reported rate over whatever window it happened to use.
                "effective_foreign_commits_per_second_over_reader_window":
                    round(commits / reader_window, 3) if reader_window else None,
            },
            "writer_latency_ms_by_family": {
                family: profile([sample for report in writer_reports
                                 for sample in report["latency_ms"].get(family, [])])
                for family in FROZEN_FAMILIES
            },
            "writer_latency_ms_per_process": {
                report["slot"]: {family: profile(samples)
                                 for family, samples in report["latency_ms"].items()}
                for report in writer_reports
            },
            "reader_latency_ms_per_process": {
                report["slot"]: profile(report.get("latency_ms", []))
                for report in reader_reports
            },
            # Section 6.5 asks for the whole begin->commit lifecycle per operation. A reader
            # whose cost sits in opening and releasing the snapshot would look free if only
            # the statement were timed, so the two are reported side by side.
            "reader_lifecycle_ms_per_process": {
                report["slot"]: profile(report.get("lifecycle_ms", []))
                for report in reader_reports
            },
            "reader_statement_ms_per_process": {
                report["slot"]: profile(report.get("statement_ms", []))
                for report in reader_reports
            },
            "tables": {"written": tables, "read": read_tables, "quiet": QUIET_TABLE},
            "board": board_manifest or {"synthetic": True},
            "window": {
                "start_at": window_starts_at, "end_at": window_ends_at,
                "warmup_seconds": opts.warmup_seconds,
                "every_participant_was_ready_before_the_start": not missing_ready,
                "participants_never_ready": missing_ready,
            },
            "layout": {
                "database_root": str(root),
                "reports_root": str(reports_dir),
                # The instrument's own files must live OUTSIDE the measured directory.
                "reports_outside_database": not str(reports_dir).startswith(str(root)),
                "database_entries_at_end": sorted(entry.name for entry in root.iterdir())
                if root.is_dir() else None,
            },
            "conflicts": sum(report.get("conflicts", 0) for report in writer_reports),
            "retries": sum(report.get("retries", 0) for report in writer_reports),
            "durable_index_refusals": len(durable),
            "durable_index_refusal_sample": durable[:2],
            "writer_reopens": reopens,
            "legal_reader_refusals": len(refusals),
            "legal_reader_refusal_sample": refusals[:4],
            "reader_refusals_per_thousand_statements": {
                report["slot"]: report.get("refusals_per_thousand_statements")
                for report in reader_reports},
            "readers_stopped_early": stopped_early,
            "metrics": folded_metrics,
            "criteria": criteria,
            "pass": all(item["pass"] for item in criteria),
        }
    finally:
        # Release the auditor BEFORE reaping. On the exception path it is still blocked on a
        # done flag nobody wrote, and collect() would sit on it for the full child timeout.
        try:
            if not done_path.exists():
                done_path.write_text("aborted", encoding="utf-8")
        except BaseException:
            pass
        for _label, process in processes:
            if process.poll() is None:
                process.terminate()
        collect(processes)
        shutil.rmtree(container, ignore_errors=True)


def describe_repository(where: pathlib.Path) -> dict:
    """Return commit, tree and working-tree state for the repository holding `where`."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(where), *args],
                              capture_output=True, text=True, check=False)

    top = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain")
    # `:(top)` anchors the pathspec at the repository root. A bare "src tests" is resolved
    # relative to the -C directory, so running this from a src/ directory would ask about
    # src/src and src/tests -- two paths that do not exist, and therefore always "clean".
    product = git("status", "--porcelain", "--", ":(top)src", ":(top)tests")
    return {
        "path": str(where),
        "toplevel": top.stdout.strip() if top.returncode == 0 else None,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "tree": tree.stdout.strip() if tree.returncode == 0 else None,
        "git_status_porcelain": status.stdout.splitlines() if status.returncode == 0 else None,
        "src_tests_clean": product.returncode == 0 and not product.stdout.strip(),
    }


# Section 6 preconditions, imported by 6.5. A certified board is EVIDENCE: opening one can
# replay its WAL and destroy the very state it certifies, so the instrument refuses by name
# rather than trusting the operator to remember.
FORBIDDEN_BOARD_MARKERS = ("m7-cert-", "m7-gate-")


def refuse_a_certified_board(where: pathlib.Path) -> None:
    """Refuse any path that names a certification or gate board, at any depth."""
    parts = [part.lower() for part in pathlib.Path(where).resolve().parts]
    for marker in FORBIDDEN_BOARD_MARKERS:
        if any(part.startswith(marker) for part in parts):
            raise SystemExit(
                f"REFUSED: {where} lies under a '{marker}*' board. Those are forensic "
                "evidence: opening one replays its WAL and destroys what it certifies. "
                "Copy it elsewhere first and point --board-template at the copy."
            )


def hash_board(where: pathlib.Path) -> dict:
    """Return one digest over a whole board, plus its file count and byte total.

    The digest is sha256 over ``relpath\0size\0sha256(bytes)\n`` per file in sorted order,
    which is the algorithm the existing recorded digests were produced with, so a value here
    can be compared against one recorded earlier instead of merely against itself.
    """
    running = hashlib.sha256()
    files = 0
    total = 0
    for item in sorted(pathlib.Path(where).rglob("*")):
        if not item.is_file():
            continue
        payload = item.read_bytes()
        relative = str(item.relative_to(where)).replace("\\", "/")
        running.update(
            f"{relative}\0{len(payload)}\0{hashlib.sha256(payload).hexdigest()}\n".encode()
        )
        files += 1
        total += len(payload)
    return {"digest": running.hexdigest(), "files": files, "bytes": total}


def cpu_percent(sample_seconds: float = 2.0) -> object:
    """Return CPU utilisation as a percentage, or an explicit reason it could not be read.

    A load average is not what H5 asks for and is not even the same quantity on Windows, so
    this samples utilisation directly: psutil when it is installed, typeperf otherwise. An
    invented figure would be worse than an absent one, so failure is reported, never guessed.
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return {"percent": psutil.cpu_percent(interval=sample_seconds),
                    "source": "psutil.cpu_percent"}
        except BaseException as failure:
            return {"unavailable": f"psutil failed: {type(failure).__name__}"}
    if sys.platform == "win32":
        try:
            sampled = subprocess.run(
                ["typeperf", r"\Processor(_Total)\% Processor Time", "-sc", "2"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            readings = []
            for line in sampled.stdout.splitlines():
                fields = [field.strip('"') for field in line.strip().split('","')]
                if len(fields) >= 2:
                    try:
                        readings.append(float(fields[-1].strip('"')))
                    except ValueError:
                        continue
            if readings:
                return {"percent": round(readings[-1], 2), "source": "typeperf"}
            return {"unavailable": "typeperf produced no numeric reading"}
        except BaseException as failure:
            return {"unavailable": f"typeperf failed: {type(failure).__name__}"}
    return {"unavailable": "no CPU utilisation source on this platform "
                           "(psutil is not installed and typeperf is Windows-only)"}


def machine_evidence() -> dict:
    """H5: what the machine was doing, not merely the operator's word that it was idle.

    Anything this cannot observe is recorded as unavailable with its reason. A load figure
    invented for the report would be worse than an absent one.
    """
    evidence: dict[str, object] = {"cpu_count": os.cpu_count()}
    evidence["cpu_percent"] = cpu_percent()
    try:
        if sys.platform == "win32":
            listed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            running = [line for line in listed.stdout.splitlines()
                       if line.strip().startswith('"python.exe"')]
        else:
            listed = subprocess.run(["pgrep", "-c", "python"], capture_output=True,
                                    text=True, timeout=30, check=False)
            running = [listed.stdout.strip()]
        evidence["python_processes"] = (len(running) if sys.platform == "win32"
                                        else int(running[0] or 0))
    except BaseException as failure:
        evidence["python_processes"] = {
            "unavailable": f"could not enumerate processes: {type(failure).__name__}"
        }
    return evidence


def package_versions() -> dict:
    """Record the versions that change the numbers: the accelerators and numpy."""
    from importlib import metadata

    found = {}
    for name in ("numpy", "google-crc32c", "okto-grafx-accel", "psutil"):
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


def provenance(opts: argparse.Namespace) -> dict:
    """Describe BOTH repositories: the one holding this script and the one holding --src.

    They are usually the same checkout, but nothing enforces it: --src can point at another
    worktree entirely, and then a single commit field would attribute the measurement to code
    that never ran. Both are recorded, and whether they match is stated rather than assumed.
    """
    script_path = pathlib.Path(__file__).resolve()
    script_repo = describe_repository(script_path.parent.parent)
    source_repo = describe_repository(pathlib.Path(opts.src).resolve())
    same = (script_repo["toplevel"] is not None
            and script_repo["toplevel"] == source_repo["toplevel"]
            and script_repo["commit"] == source_repo["commit"])
    return {
        "command": [sys.executable, str(script_path), *sys.argv[1:]],
        # The measured tree is the one --src names, so it leads.
        "grafx_commit": source_repo["commit"],
        "grafx_tree": source_repo["tree"],
        "git_status_porcelain": source_repo["git_status_porcelain"],
        "product_src_tests_clean": source_repo["src_tests_clean"],
        "source_repository": source_repo,
        "script_repository": script_repo,
        "script_and_source_are_the_same_checkout": same,
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "packages": package_versions(),
        "repository": script_repo["path"],
        "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "source_root": opts.src,
        "system": platform.platform(),
        "machine_idle_asserted": bool(opts.machine_idle_asserted),
        # The boolean is the operator's word. These are the observations that can contradict it.
        "machine_before": machine_evidence(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_multiclient_matrix",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "FROZEN BY ROADMAP SECTION 6.5 -- defaults, not knobs:\n"
            f"  writer families      {' / '.join(FROZEN_FAMILIES)}\n"
            f"  writer counts N      {list(FROZEN_WRITER_COUNTS)}\n"
            f"  reader counts M      {list(FROZEN_READER_COUNTS)}\n"
            f"  regimes              {list(FROZEN_REGIMES)}\n"
            f"  reader shapes        {list(FROZEN_READER_SHAPES)}\n"
            f"  foreign commit rate  {list(FROZEN_FOREIGN_COMMIT_RATES)} (the key curve)\n"
            f"  reader target        {list(FROZEN_READER_TARGETS)} (the CE-3 discriminant)\n"
            "  correctness          serial oracle + verify('all') live and cold\n"
            "\n"
            "PARAMETERISED BECAUSE THE REPORT LEFT THE VOLUME OPEN -- declared, not frozen:\n"
            "  --seconds --rows-per-txn --txns-per-writer --long-reader-seconds\n"
            "  --warmup-seconds\n"
            "Each value used is copied into the report, so nobody has to guess later which\n"
            "numbers were policy and which were choices.\n"
            "\n"
            "Every metric the product does not emit is reported as unavailable WITH ITS\n"
            "REASON. None is ever reported as zero, and a case in which nothing happened\n"
            "fails rather than passing every 'nothing bad happened' check vacuously."
        ),
    )
    parser.add_argument("--src", required=True,
                        help="src/ root every child must import okto_grafx from (A94 pin)")
    parser.add_argument("--case", default="f1-curve-2proc",
                        choices=("matrix", "f1-curve-2proc", "ce3-2proc"),
                        help="f1-curve-2proc (default): the finite 2-process subcase -- the "
                             "frozen rate curve crossed with the same-table / "
                             "unrelated-table discriminant. It INFORMS the CE-3 decision; it "
                             "is NOT the literal section 5b CE-3 gate, which needs the twelve "
                             "warm families on an M7 copy and is not implemented here. "
                             "'ce3-2proc' is accepted as an alias for the same thing and is "
                             "deprecated, because the name overclaimed. "
                             "matrix: the full frozen N x M sweep (LONG; not a smoke run)")
    parser.add_argument("--writers", type=int, default=None,
                        help="override N for a short run; the matrix uses the frozen set")
    parser.add_argument("--readers", type=int, default=None, help="override M for a short run")
    parser.add_argument("--regime", default=None, choices=FROZEN_REGIMES)
    parser.add_argument("--reader-shape", default=None, choices=FROZEN_READER_SHAPES)
    parser.add_argument("--reader-target", default=None, choices=FROZEN_READER_TARGETS,
                        help="override the CE-3 discriminant for a short run")
    parser.add_argument("--foreign-commit-rate", type=float, default=None,
                        help="run ONE rate instead of sweeping the frozen curve. The "
                             "value is the AGGREGATE rate the reader sees; it is divided "
                             "among the writers, so N=1 and N=8 are comparable at the same "
                             "point. 0 means every writer opens the database and commits "
                             "nothing -- the zero of the curve, recorded as a commanded idle "
                             "rather than a failure. OMITTING this option sweeps the frozen "
                             f"curve {list(FROZEN_FOREIGN_COMMIT_RATES)} in both cases; only "
                             "an explicit value pins a single rate, and a run recorded as "
                             "null (unpaced, flat out) happens only when a rate cannot be "
                             "determined")
    parser.add_argument("--seconds", type=float, default=20.0,
                        help="[gap-parameterised] writer/reader phase duration")
    parser.add_argument("--long-reader-seconds", type=float, default=60.0,
                        help="[gap-parameterised] duration of the 6.5 long read transaction")
    parser.add_argument("--rows-per-txn", type=int, default=5,
                        help="[gap-parameterised] rows per create_node transaction")
    parser.add_argument("--txns-per-writer", type=int, default=25,
                        help="[gap-parameterised] transaction rounds per writer")
    parser.add_argument("--warmup-seconds", type=float, default=0.0,
                        help="[gap-parameterised] discarded warmup, excluded from BOTH the "
                             "percentiles and the throughput denominator")
    parser.add_argument("--seed", type=int, default=7,
                        help="deterministic seed; every child derives its stream from it")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="also write the report to this path")
    parser.add_argument("--workspace", default=None,
                        help="directory to build the databases in (default: a temp dir)")
    parser.add_argument("--board-template", default=None,
                        help="directory holding a real board to copy into the workspace. "
                             "Section 6 (imported by 6.5) requires an OFFICIAL run to work "
                             "on a relocated COPY, never the original, and the copy is "
                             "hashed file by file so it can be proven identical. Without "
                             "this the run builds a synthetic board and every cell is "
                             "labelled official=false. Any path under an m7-cert-* or "
                             "m7-gate-* board is REFUSED: those are forensic evidence and "
                             "opening one replays its WAL over the state it certifies")
    parser.add_argument("--board-digest", default=None,
                        help="the digest --board-template is expected to have, as sha256 over "
                             "relpath\\0size\\0sha256(bytes) per file in sorted order. An "
                             "official run must authenticate its template against a value "
                             "recorded beforehand, not merely against itself")
    parser.add_argument("--machine-idle-asserted", action="store_true",
                        help="operator asserts the machine was idle; recorded in provenance")
    parser.add_argument("--reopen-on-stale-index", action="store_true",
                        help="OPT-IN WORKAROUND, off by default. A writer attached to a "
                             "board another process created can be refused with "
                             "GrafxIndexError field=index_view_unavailable -- the index "
                             "header carries INDEX_FLAG_STALE, which is DURABLE on disk. The "
                             "product marks it retryable, but nothing in that process clears "
                             "it, so retrying spins. Off: the case FAILS and names the "
                             "refusal. On: the writer reopens its handle and continues, and "
                             "the reopen count appears in the report so a worked-around run "
                             "can never be mistaken for a clean one")
    return parser


def select_cells(opts: argparse.Namespace) -> list[tuple]:
    """Return (writers, readers, regime, shape, reader_target, rate) for every cell to run."""
    rates = ([opts.foreign_commit_rate] if opts.foreign_commit_rate is not None
             else list(FROZEN_FOREIGN_COMMIT_RATES))
    if opts.case in ("f1-curve-2proc", "ce3-2proc"):
        # Two processes exactly: one writer, one reader. The pair that decides CE-3 is
        # unrelated-table against same-table, swept across the frozen rate curve.
        targets = [opts.reader_target] if opts.reader_target else list(FROZEN_READER_TARGETS)
        writers = opts.writers if opts.writers is not None else 1
        readers = opts.readers if opts.readers is not None else 1
        regime = opts.regime or "disjoint"
        shape = opts.reader_shape or "autocommit"
        return [(writers, readers, regime, shape, target, rate)
                for target in targets for rate in rates]
    writers = [opts.writers] if opts.writers else list(FROZEN_WRITER_COUNTS)
    readers = [opts.readers] if opts.readers is not None else list(FROZEN_READER_COUNTS)
    regimes = [opts.regime] if opts.regime else list(FROZEN_REGIMES)
    shapes = [opts.reader_shape] if opts.reader_shape else list(FROZEN_READER_SHAPES)
    targets = [opts.reader_target] if opts.reader_target else list(FROZEN_READER_TARGETS)
    # The frozen rate curve is part of the matrix, not an extra the CE-3 subcase owns alone.
    # Running the matrix at a single unpaced rate and calling it the full sweep would claim
    # coverage of an axis section 6.5 freezes.
    matrix_rates = ([opts.foreign_commit_rate]
                    if opts.foreign_commit_rate is not None
                    else list(FROZEN_FOREIGN_COMMIT_RATES))
    cells = []
    for count in writers:
        for readers_count in readers:
            # With no readers the reader shape and target are not observable, so enumerating
            # them would multiply identical cells and inflate the run for nothing.
            shape_axis = shapes if readers_count else shapes[:1]
            target_axis = targets if readers_count else targets[:1]
            for regime in regimes:
                for shape in shape_axis:
                    for target in target_axis:
                        for rate in matrix_rates:
                            cells.append(
                                (count, readers_count, regime, shape, target, rate)
                            )
    return cells


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(argv)
    positive = {"--seconds": opts.seconds, "--long-reader-seconds": opts.long_reader_seconds,
                "--rows-per-txn": opts.rows_per_txn,
                "--txns-per-writer": opts.txns_per_writer}
    invalid = sorted(name for name, value in positive.items() if value <= 0)
    if invalid:
        parser.error(f"these options must be greater than zero: {', '.join(invalid)}")
    if opts.warmup_seconds < 0:
        parser.error("--warmup-seconds must not be negative")
    if opts.warmup_seconds >= opts.seconds:
        parser.error("--warmup-seconds must be smaller than --seconds, or every sample "
                     "would be discarded and the run would report nothing")
    # The long reader runs for its own duration, so a warmup that swallows THAT phase would
    # leave the long shape with zero samples while every other shape still reported.
    if opts.reader_shape in (None, "long") and opts.warmup_seconds >= opts.long_reader_seconds:
        parser.error("--warmup-seconds must be smaller than --long-reader-seconds, or the "
                     "long reader would discard every sample it took")
    if opts.writers is not None and opts.writers <= 0:
        parser.error("--writers must be greater than zero")
    if opts.readers is not None and opts.readers < 0:
        parser.error("--readers must not be negative")
    if opts.foreign_commit_rate is not None and opts.foreign_commit_rate < 0:
        parser.error("--foreign-commit-rate must not be negative")
    if not pathlib.Path(opts.src).is_dir():
        parser.error(f"--src is not a directory: {opts.src}")
    if opts.board_template:
        template = pathlib.Path(opts.board_template)
        if not template.is_dir():
            parser.error(f"--board-template is not a directory: {opts.board_template}")
        refuse_a_certified_board(template)
    if opts.workspace:
        refuse_a_certified_board(pathlib.Path(opts.workspace))

    cells = select_cells(opts)
    report = {
        "tool": "measure_multiclient_matrix",
        "version": 1,
        "provenance": provenance(opts),
        "frozen": {
            "writer_families": list(FROZEN_FAMILIES),
            "writer_counts": list(FROZEN_WRITER_COUNTS),
            "reader_counts": list(FROZEN_READER_COUNTS),
            "regimes": list(FROZEN_REGIMES),
            "reader_shapes": list(FROZEN_READER_SHAPES),
            "reader_targets": list(FROZEN_READER_TARGETS),
            "foreign_commit_rates": list(FROZEN_FOREIGN_COMMIT_RATES),
        },
        "parameterised_gaps": {
            "seconds": opts.seconds,
            "note_writers_follow_the_long_reader": "with reader shape 'long' the writers run "
            "max(seconds, long_reader_seconds) so the foreign load covers the whole measured "
            "window rather than stopping partway through it",
            "long_reader_seconds": opts.long_reader_seconds,
            "rows_per_txn": opts.rows_per_txn,
            "txns_per_writer": opts.txns_per_writer,
            "warmup_seconds": opts.warmup_seconds,
            "seed": opts.seed,
        },
        "not_implemented_here": {
            "F2": "phase timers",
            "F3": "takeover and fairness",
            "F4": "PARTIAL. The multi-process scenarios this tool runs are writers and "
                  "readers over one database with a serial oracle. The full F4 scenario list "
                  "is not covered and no claim is made that it is.",
            "F5": "Ladybug capacity comparison",
            "CE-3 literal (section 5b)": "NOT IMPLEMENTED. The literal form is participant A "
                  "running the twelve warm M7 families against a copy of the M7 board while "
                  "participant B commits a small write to the same or an unrelated table, "
                  "reported as RAW per operation with the _read_page / _still_names hooks. "
                  "The two-process case here is the F1 rate curve plus ONE CE-3 "
                  "discriminant (same-table vs unrelated-table); it is not the CE-3 gate and "
                  "must not be read as one.",
        },
        "case": opts.case,
        "cells": [run_case(opts, *cell) for cell in cells],
    }
    report["machine_after"] = machine_evidence()

    # The full matrix runs for hours. If the checkout moved, went dirty, or the source tree
    # changed under it, the numbers can no longer be attributed to the commit named at the
    # start -- so the run is re-described at the end and any drift is fatal to official.
    after = describe_repository(pathlib.Path(opts.src).resolve())
    before = report["provenance"]["source_repository"]
    drift = [field for field in ("commit", "tree", "src_tests_clean")
             if before.get(field) != after.get(field)]
    report["provenance_after"] = after
    report["source_tree_drifted_during_the_run"] = drift or None

    # official is the CELLS' verdict, not a re-derivation from the options: a cell that found
    # an inauthentic board is false, and the report must not be true above it.
    cell_reasons = sorted({reason for cell in report["cells"]
                           for reason in (cell.get("not_official_because") or [])})
    if drift:
        cell_reasons.append("the source tree changed during the run: " + ", ".join(drift))
    report["official"] = bool(report["cells"]) and all(
        cell.get("official") for cell in report["cells"]) and not drift
    report["not_official_because"] = cell_reasons or None
    report["pass"] = (bool(report["cells"])
                      and all(cell["pass"] for cell in report["cells"])
                      and not drift)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if opts.json_out:
        pathlib.Path(opts.json_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
