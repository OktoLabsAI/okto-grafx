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

THE ZERO OF THE CURVE. `--foreign-commit-rate 0` means the writer opens the database and stays
idle: zero commits COMMANDED. Its report carries `idle: true` and the case asserts that the
commit count really is zero, so a commanded zero can never be confused with a zero caused by a
child that died. Omitting the option entirely means unpaced (flat out), which is a different
thing again and is recorded as `null`.

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
# "" = unpaced (flat out). "0" = COMMANDED IDLE, the zero of the 6.5 curve. ">0" = paced.
rate = None if rate_text == "" else float(rate_text)

# A refusal carrying this field is durable: it comes from INDEX_FLAG_STALE on the index
# header (index_manager.py:677), which lives on disk. The product marks it retryable=True,
# but nothing in THIS process clears it, so a retry loop spins instead of retrying. The
# instrument names that case rather than burning the measurement window on it.
DURABLE_FIELD = "index_view_unavailable"

rnd = random.Random(seed * 7919 + slot)
db = connect(root)
base = slot * 1_000_000
# Disjoint: this writer owns its own tables. Hot: every writer hammers the shared pair. Keys
# stay disjoint per writer either way, so a conflict measures the protocol, not a duplicate key.
node_table = "Item" if regime == "hot" else "Item%d" % slot
edge_table = "Links" if regime == "hot" else "Links%d" % slot

latency = {family: [] for family in
           ("create_node", "create_edge", "update_node", "mark_superseded")}
conflicts = 0
retries = 0
escapes = []
durable_refusals = []
reopens = 0
acknowledged = []
commits = 0
started = time.monotonic()
deadline = started + seconds
# Warmup is discarded from BOTH the samples and the commit count, so the reported commits/s
# divides by the very window the percentiles were drawn from.
timing_starts = started + warmup
timed_from = None
next_slot = started
idle = rate == 0.0


def committed(family, build):
    """Run one transaction to a successful commit, timing begin->commit INCLUSIVE of retries.

    A give-up carries the LAST refusal that caused it. "Gave up after 60 retries" on its own
    names the symptom and throws away the only evidence of the cause.
    """
    global conflicts, retries, commits, timed_from, next_slot, db, reopens
    last_refusal = None
    if rate:
        # Pace to the target rate: this writer IS the foreign traffic whose effect on reader
        # latency is the key curve of section 6.5.
        pause = next_slot - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        next_slot = max(next_slot + 1.0 / rate, time.monotonic())
    began = time.perf_counter()
    for attempt in range(60):
        try:
            with db.begin("write") as txn:
                made = build(txn)
            if time.monotonic() >= timing_starts:
                if timed_from is None:
                    timed_from = time.monotonic()
                latency[family].append((time.perf_counter() - began) * 1000.0)
                commits += 1
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
                    db = connect(root)
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
                if time.monotonic() >= deadline:
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

if idle:
    # The zero of the curve: hold the database open, commit nothing, occupy a process slot.
    while time.monotonic() < deadline:
        time.sleep(0.05)
else:
    # A PACED writer runs to the deadline; the txns cap applies only to the unpaced case.
    # Otherwise a fast rate exhausts the rounds early, the writer stops, and the reader spends
    # the rest of its window with no foreign traffic at all -- so the curve point would be
    # labelled 10/s while the reader actually experienced about half of that.
    while (rate or index < txns) and time.monotonic() < deadline:
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
            acknowledged.extend(keys)
            made_nodes.extend(keys)
            for key in keys:
                expected_owner[key] = slot

        if len(made_nodes) >= 2:
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
                expected_edges.append([pair[0], pair[1]])

        if made_nodes:
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
                expected_owner[update_key] = update_owner

            supersede_key = made_nodes[0]

            def build_supersede(txn, key=supersede_key):
                txn.execute(
                    "MATCH (n:%s) WHERE n.id = $k SET n.live = false" % node_table,
                    {"k": key},
                )
                return key
            ok, _made = committed("mark_superseded", build_supersede)
            if ok and supersede_key not in expected_superseded:
                expected_superseded.append(supersede_key)
        index += 1

ended = time.monotonic()
# The TIMED window, not the wall clock: dividing timed commits by an untimed elapsed would
# understate throughput by exactly the warmup.
elapsed = (ended - timed_from) if timed_from is not None else 0.0
# How long this writer was actually present after the warmup, whether or not it committed.
# This is the span the reader's window has to be compared against: a writer that went quiet
# halfway through did not deliver the foreign rate its label claims.
active_span = max(0.0, ended - max(started, timing_starts))
metrics = None
metrics_error = None
try:
    snapshot = db.snapshot_metrics()
    to_dict = getattr(snapshot, "as_dict", None)
    metrics = to_dict() if callable(to_dict) else json.loads(json.dumps(
        snapshot, default=lambda item: getattr(item, "__dict__", str(item))))
except BaseException as failure:
    metrics_error = "%s: %s" % (type(failure).__name__, repr(failure)[:200])
db.close()

pathlib.Path(out).write_text(json.dumps({
    "role": "writer", "slot": slot, "regime": regime, "idle": idle,
    "target_commit_rate": rate, "warmup_seconds": warmup,
    "node_table": node_table, "edge_table": edge_table,
    "latency_ms": latency, "conflicts": conflicts, "retries": retries,
    "commits": commits, "elapsed_seconds": elapsed,
    "active_span_seconds": active_span, "rounds_completed": index,
    "durable_refusals": durable_refusals, "reopens": reopens,
    "reopen_on_stale_index": reopen_on_stale,
    "acknowledged": acknowledged, "escapes": escapes,
    "expected_owner": {str(key): value for key, value in expected_owner.items()},
    "expected_superseded": expected_superseded,
    "expected_edges": expected_edges,
    "metrics": metrics, "metrics_error": metrics_error,
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
tables = json.loads(tables_json)
edges = json.loads(edges_json)
rnd = random.Random(seed * 104729 + slot)

db = connect(root)
latency_ms = []
torn = []
escapes = []      # ILLEGAL: anything that is not a Grafx refusal. Any entry fails the case.
refusals = []     # LEGAL: a Grafx refusal a reader is contractually allowed to receive.
statements = 0
started = time.monotonic()
deadline = started + seconds
timing_starts = started + warmup
timed_from = None
long_first = None
long_scans = 0


def record(began):
    """Keep one latency sample, unless it fell inside the discarded warmup."""
    global statements, timed_from
    if time.monotonic() >= timing_starts:
        if timed_from is None:
            timed_from = time.monotonic()
        latency_ms.append((time.perf_counter() - began) * 1000.0)
        statements += 1


def one_read(execute, table):
    """One statement, timed; returns its rows."""
    began = time.perf_counter()
    rows = execute(
        "MATCH (n:%s) RETURN n.id, n.owner ORDER BY n.id LIMIT 100" % table
    ).rows
    record(began)
    return rows


if shape == "long":
    # ONE long read transaction: its snapshot must not move under it, however much the
    # writers commit. A moved snapshot is a torn read, and it fails the case.
    reader = db.begin("read")
    table = tables[0]
    long_first = sorted(one_read(reader.execute, table))
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            again = sorted(one_read(reader.execute, table))
            long_scans += 1
            if again != long_first:
                torn.append({"kind": "long_snapshot_moved",
                             "first_rows": len(long_first), "now_rows": len(again)})
                break
        except GrafxError as refused:
            refusals.append("%s: %s" % (type(refused).__name__, repr(refused)[:160]))
            break
    reader.rollback()
else:
    while time.monotonic() < deadline:
        table = rnd.choice(tables)
        try:
            if shape == "autocommit":
                # Database.execute = begin+execute+commit, the shape measure_concurrency
                # leaves outside its timers (roadmap 6.5).
                one_read(db.execute, table)
                began = time.perf_counter()
                first = db.execute("MATCH (n:%s) RETURN count(*)" % table).rows[0][0]
                record(began)
                began = time.perf_counter()
                second = db.execute("MATCH (n:%s) RETURN count(*)" % table).rows[0][0]
                record(began)
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
                reader = db.begin("read")
                try:
                    began = time.perf_counter()
                    rows = reader.execute(
                        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id LIMIT 100"
                        % (table, edge)
                    ).rows
                    record(began)
                    if len(rows) != len(set(rows)):
                        torn.append({"kind": "duplicate_rows", "rows": len(rows)})
                finally:
                    reader.rollback()
        except GrafxError as refused:
            # A refusal is LEGAL for a reader whose index view moved: it is counted, not
            # charged against the case. Only a non-Grafx escape means the product broke.
            refusals.append("%s(retryable=%s): %s"
                            % (type(refused).__name__, refused.retryable, repr(refused)[:120]))
        except BaseException as escaped:
            escapes.append("%s: %s" % (type(escaped).__name__, repr(escaped)[:160]))
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
db.close()

pathlib.Path(out).write_text(json.dumps({
    "role": "reader", "slot": slot, "shape": shape, "tables_read": tables,
    "latency_ms": latency_ms, "statements": statements, "torn": torn,
    "elapsed_seconds": elapsed, "escapes": escapes, "refusals": refusals,
    "long_scans": long_scans, "warmup_seconds": warmup,
    "metrics": metrics, "metrics_error": metrics_error,
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
    for row in db.execute("MATCH (n:%s) RETURN n.id, n.owner, n.live" % table).rows:
        key = int(row[0])
        stored.append(key)
        owner[str(key)] = row[1]
        if row[2] is False:
            superseded.append(key)
for position, edge_table in enumerate(edge_tables):
    source = tables[position] if position < len(tables) else tables[0]
    for row in db.execute(
        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id" % (source, edge_table)
    ).rows:
        edges.append([int(row[0]), int(row[1])])
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
    for row in db.execute("MATCH (n:%s) RETURN n.id, n.owner, n.live" % table).rows:
        key = int(row[0])
        stored.append(key)
        owner[str(key)] = row[1]
        if row[2] is False:
            superseded.append(key)
for position, edge_table in enumerate(edge_tables):
    source = tables[position] if position < len(tables) else tables[0]
    for row in db.execute(
        "MATCH (a:%s)-[r:%s]->(b) RETURN a.id, b.id" % (source, edge_table)
    ).rows:
        edges.append([int(row[0]), int(row[1])])
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
              "AUDITOR_CHILD"):
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
                    "metrics": report["metrics"]}
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
             writer_span: float = 0.0, reader_window: float = 0.0) -> list[dict]:
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
    if rate and readers and reader_window > 0:
        coverage = writer_span / reader_window
        criteria.append({
            "name": "foreign_traffic_covered_the_reader_window",
            "pass": coverage >= 0.9,
            "observed": {
                "coverage": round(coverage, 3),
                "writer_span_seconds": round(writer_span, 3),
                "reader_window_seconds": round(reader_window, 3),
                "labelled_rate": rate,
                "effective_rate_over_reader_window": round(commits / reader_window, 3),
            },
            "bound": ">= 0.9 of the reader window had a writer present; below that, the "
                     "labelled rate is not the rate the reader felt",
        })
    criteria.append({"name": "no_acknowledged_row_lost", "pass": oracle_ok and not lost,
                     "observed": lost[:8], "bound": "acknowledged is a subset of stored"})
    criteria.append({"name": "no_phantom_row", "pass": oracle_ok and not phantom,
                     "observed": phantom[:8], "bound": "stored is a subset of acknowledged"})
    criteria.append({"name": "no_duplicate_row", "pass": duplicates == 0,
                     "observed": duplicates, "bound": "0 duplicate ids"})
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
    root.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[str, subprocess.Popen]] = []
    criteria: list[dict] = []
    identity = {
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

        # The LIVE auditor opens before any writer starts and holds that one handle across the
        # whole run. Without it, "live" and "cold" would both be fresh opens after everybody
        # closed -- two names for the same measurement.
        ready_path = reports_dir / "auditor-ready.json"
        done_path = reports_dir / "auditor-done.flag"
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
        writers_started_at = time.time()
        for slot in range(1, writers + 1):
            processes.append((f"writer-{slot}", spawn(WRITER_CHILD, [
                opts.src, str(root), str(slot), str(opts.seed), regime,
                str(opts.seconds), str(opts.rows_per_txn), str(opts.txns_per_writer),
                "" if rate is None else str(per_writer_rate(rate, writers)),
                str(opts.warmup_seconds),
                "1" if opts.reopen_on_stale_index else "0",
                str(reports_dir / f"writer-{slot}.json"),
            ])))
        for slot in range(1, readers + 1):
            duration = opts.long_reader_seconds if shape == "long" else opts.seconds
            processes.append((f"reader-{slot}", spawn(READER_CHILD, [
                opts.src, str(root), str(slot), str(opts.seed), shape, str(duration),
                json.dumps(read_tables), json.dumps(read_edges), str(opts.warmup_seconds),
                str(reports_dir / f"reader-{slot}.json"),
            ])))

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

        acknowledged = set(seeded)
        expected_owner: dict[str, object] = {str(key): 0 for key in seeded}
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
        durable = [entry for report in writer_reports
                   for entry in report.get("durable_refusals", [])]
        reopens = sum(report.get("reopens", 0) for report in writer_reports)

        commits = sum(report.get("commits", 0) for report in writer_reports)
        statements = sum(report.get("statements", 0) for report in reader_reports)

        writer_window = max([report.get("elapsed_seconds", 0.0)
                             for report in writer_reports] or [0.0])
        reader_window = max([report.get("elapsed_seconds", 0.0)
                             for report in reader_reports] or [0.0])
        writer_span = max([report.get("active_span_seconds", 0.0)
                           for report in writer_reports] or [0.0])
        criteria = evaluate(
            readers=readers, rate=rate,
            reopen_workaround=bool(opts.reopen_on_stale_index),
            child_failures=child_failures, missing=missing, live=live, cold=cold,
            acknowledged=acknowledged, stored_list=stored_list, torn=torn,
            escapes=escapes, durable=durable, reopens=reopens,
            commits=commits, statements=statements,
            expected_owner=expected_owner, expected_superseded=expected_superseded,
            expected_edges=expected_edges,
            writer_span=writer_span, reader_window=reader_window,
        )

        return {
            **identity,
            "throughput": {
                "commits_per_second": round(commits / writer_window, 3)
                if writer_window else None,
                "reader_statements_per_second": round(statements / reader_window, 3)
                if reader_window else None,
                "writer_timed_window_seconds": round(writer_window, 3),
                "reader_timed_window_seconds": round(reader_window, 3),
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
            "tables": {"written": tables, "read": read_tables, "quiet": QUIET_TABLE},
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
            "metrics": gather_metrics(writer_reports + reader_reports),
            "criteria": criteria,
            "pass": all(item["pass"] for item in criteria),
        }
    finally:
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
        "repository": script_repo["path"],
        "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "source_root": opts.src,
        "system": platform.platform(),
        "machine_idle_asserted": bool(opts.machine_idle_asserted),
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
    parser.add_argument("--case", default="ce3-2proc", choices=("matrix", "ce3-2proc"),
                        help="ce3-2proc (default): the finite 2-process subcase that decides "
                             "CE-3 -- 2 reader targets x 3 frozen rates. "
                             "matrix: the full frozen N x M sweep (LONG; not for a smoke run)")
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
    if opts.case == "ce3-2proc":
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
            "long_reader_seconds": opts.long_reader_seconds,
            "rows_per_txn": opts.rows_per_txn,
            "txns_per_writer": opts.txns_per_writer,
            "warmup_seconds": opts.warmup_seconds,
            "seed": opts.seed,
        },
        "not_implemented_here": {
            "F2": "phase timers", "F3": "takeover and fairness",
            "F5": "Ladybug capacity comparison",
        },
        "case": opts.case,
        "cells": [run_case(opts, *cell) for cell in cells],
    }
    report["pass"] = bool(report["cells"]) and all(cell["pass"] for cell in report["cells"])
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if opts.json_out:
        pathlib.Path(opts.json_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
