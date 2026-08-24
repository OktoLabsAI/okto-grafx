"""The instrument behind docs/PERFORMANCE.md's concurrency figures, kept in the tree.

Concurrency smoke with LATENCY measurement: several OS processes writing and reading at once.

Two phases for the writers, because they are different regimes and mixing them would average away
the answer: phase 1 writes DISJOINT key ranges (the protocol at rest -- every commit should land,
conflicts near zero), phase 2 hammers a SHARED set of rows (the protocol at work -- conflicts are
expected and retried, and the number that matters is end-to-end latency INCLUDING retries).

Readers run the whole time, timing three shapes separately: an indexed point read, an aggregate,
and a bounded scan. Correctness stays asserted at the end -- latency numbers from a run that lost
rows would be numbers about a broken database.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import pathlib
SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)
from okto_grafx import connect  # noqa: E402

WRITERS, READERS = 4, 3
DISJOINT_TXNS, ROWS_PER_TXN = 25, 5
CONTENDED_TXNS = 15
CONTENDED_KEYS = list(range(900_001, 900_011))


def pct(samples, q):
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def profile(samples):
    if not samples:
        return "n=0"
    return (f"n={len(samples):<5} med={statistics.median(samples):7.2f}ms "
            f"p90={pct(samples, 0.90):7.2f}ms p99={pct(samples, 0.99):8.2f}ms "
            f"max={max(samples):8.2f}ms")


WRITER = r'''
import json, sys, time, random
sys.path.insert(0, sys.argv[1])
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

root, slot = sys.argv[2], int(sys.argv[3])
disjoint_txns, rows_per, contended_txns = int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
contended = json.loads(sys.argv[7])
rnd = random.Random(slot * 7919)
db = connect(root)
acknowledged, escapes = [], []
disjoint_ms, contended_ms = [], []
conflicts = 0


def committed(build, bucket):
    """Run one transaction to a successful commit, timing END TO END including retries."""
    global conflicts
    started = time.perf_counter()
    for _ in range(60):
        try:
            with db.begin("write") as txn:
                made = build(txn)
            bucket.append((time.perf_counter() - started) * 1000)
            return made
        except GrafxError as refused:
            if getattr(refused, "retryable", False):
                conflicts += 1
                time.sleep(rnd.uniform(0.002, 0.02))
                continue
            raise
        except BaseException as escaped:
            escapes.append(f"{type(escaped).__name__}: {repr(escaped)[:120]}")
            return []
    escapes.append("gave up after 60 retries")
    return []


# Phase 1: disjoint ranges. Every one of these must land.
for index in range(disjoint_txns):
    base = slot * 1_000_000 + index * rows_per
    def build(txn, base=base):
        made = []
        for offset in range(rows_per):
            identity = base + offset + 1
            txn.execute("CREATE (:Item {id: $i, owner: $o, body: $b})",
                        {"i": identity, "o": slot, "b": "y" * 120})
            made.append(identity)
        return made
    acknowledged.extend(committed(build, disjoint_ms))

# Phase 2: everybody updates the same small row set. Conflicts are the point.
for index in range(contended_txns):
    key = contended[index % len(contended)]
    def build(txn, key=key, index=index):
        txn.execute("MATCH (i:Item) WHERE i.id = $k SET i.owner = $o",
                    {"k": key, "o": slot * 1000 + index})
        return []
    committed(build, contended_ms)

db.close()
print(json.dumps({
    "slot": slot, "acknowledged": acknowledged, "conflicts": conflicts,
    "escapes": escapes, "disjoint_ms": disjoint_ms, "contended_ms": contended_ms,
}), flush=True)
'''

READER = r'''
import json, sys, time, random
sys.path.insert(0, sys.argv[1])
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

root, seconds, seed = sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
rnd = random.Random(seed)
db = connect(root)
point_ms, aggregate_ms, scan_ms = [], [], []
escapes, torn = [], []
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    try:
        reader = db.begin("read")
        try:
            key = rnd.randint(900_001, 900_010)
            t0 = time.perf_counter()
            reader.execute("MATCH (i:Item) WHERE i.id = $k RETURN i.owner", {"k": key})
            point_ms.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            first = reader.execute("MATCH (i:Item) RETURN count(*)").rows[0][0]
            aggregate_ms.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            rows = reader.execute(
                "MATCH (i:Item) RETURN i.id ORDER BY i.id LIMIT 50").rows
            scan_ms.append((time.perf_counter() - t0) * 1000)

            second = reader.execute("MATCH (i:Item) RETURN count(*)").rows[0][0]
            if first != second or len(rows) != len(set(rows)):
                torn.append({"first": first, "second": second, "rows": len(rows)})
        finally:
            reader.rollback()
    except GrafxError:
        pass
    except BaseException as escaped:
        escapes.append(f"{type(escaped).__name__}: {repr(escaped)[:120]}")
db.close()
print(json.dumps({
    "point_ms": point_ms, "aggregate_ms": aggregate_ms, "scan_ms": scan_ms,
    "escapes": escapes, "torn": torn,
}), flush=True)
'''


def main() -> int:
    root = tempfile.mkdtemp(prefix="grafx-timed-")
    print(f"database: {root}")
    print(f"{WRITERS} writers  ({DISJOINT_TXNS} disjoint txns x {ROWS_PER_TXN} rows, then "
          f"{CONTENDED_TXNS} contended updates over {len(CONTENDED_KEYS)} shared rows)")
    print(f"{READERS} readers  (point read / count(*) / ORDER BY+LIMIT 50, throughout)\n")

    db = connect(root)
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Item(id INT64, owner INT64, body STRING, PRIMARY KEY(id))")
    with db.begin("write") as txn:
        for key in CONTENDED_KEYS:
            txn.execute("CREATE (:Item {id: $i, owner: -1, body: 'contended'})", {"i": key})
    db.close()

    started = time.perf_counter()
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, SRC, root, str(slot), str(DISJOINT_TXNS),
             str(ROWS_PER_TXN), str(CONTENDED_TXNS), json.dumps(CONTENDED_KEYS)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for slot in range(1, WRITERS + 1)
    ]
    readers = [
        subprocess.Popen([sys.executable, "-c", READER, SRC, root, "45", str(97 + n)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for n in range(READERS)
    ]

    writer_reports, reader_reports, crashed = [], [], []
    for role, fleet, reports in (("writer", writers, writer_reports),
                                 ("reader", readers, reader_reports)):
        for index, process in enumerate(fleet):
            out, err = process.communicate(timeout=600)
            if process.returncode != 0 or not out.strip():
                crashed.append((role, index, (err or out)[-300:]))
                continue
            reports.append(json.loads(out.strip().splitlines()[-1]))
    elapsed = time.perf_counter() - started

    # ---- latency report ------------------------------------------------------------------
    disjoint = [ms for r in writer_reports for ms in r["disjoint_ms"]]
    contended = [ms for r in writer_reports for ms in r["contended_ms"]]
    conflicts = sum(r["conflicts"] for r in writer_reports)
    point = [ms for r in reader_reports for ms in r["point_ms"]]
    aggregate = [ms for r in reader_reports for ms in r["aggregate_ms"]]
    scan = [ms for r in reader_reports for ms in r["scan_ms"]]

    print(f"elapsed {elapsed:.1f}s\n")
    print("WRITE latency (committed transaction, end to end, retries included)")
    print(f"  disjoint  ({ROWS_PER_TXN} rows/txn)   {profile(disjoint)}")
    print(f"  contended (1 update/txn)   {profile(contended)}   [{conflicts} retryable conflicts]")
    if disjoint:
        rows_written = sum(len(r["acknowledged"]) for r in writer_reports)
        print(f"  write throughput           {rows_written / elapsed:6.1f} rows/s across "
              f"{WRITERS} writers ({rows_written} rows)")
    print("\nREAD latency (under full write load, per statement)")
    print(f"  point read by key          {profile(point)}")
    print(f"  count(*) aggregate         {profile(aggregate)}")
    print(f"  ORDER BY id LIMIT 50       {profile(scan)}")
    reads = len(point) + len(aggregate) + len(scan)
    print(f"  read throughput            {reads / elapsed:6.1f} statements/s across {READERS} readers")

    # ---- correctness, or the numbers above are about a broken database -------------------
    acknowledged = [i for r in writer_reports for i in r["acknowledged"]]
    escapes = [e for r in writer_reports + reader_reports for e in r["escapes"]]
    torn = [t for r in reader_reports for t in r["torn"]]

    db = connect(root)
    stored = [row[0] for row in db.execute("MATCH (i:Item) RETURN i.id").rows]
    live_findings = db.verify("all").findings
    db.close()
    reopened = connect(root)
    stored_after = [row[0] for row in reopened.execute("MATCH (i:Item) RETURN i.id").rows]
    reopened_findings = reopened.verify("all").findings
    owners = reopened.execute(
        "MATCH (i:Item) WHERE i.body = 'contended' RETURN i.id, i.owner").rows
    reopened.close()

    problems = []
    missing = sorted(set(acknowledged) - set(stored))
    phantom = sorted(set(stored) - set(acknowledged) - set(CONTENDED_KEYS))
    if crashed:
        problems.append(f"{len(crashed)} process(es) died: {crashed[:2]}")
    if escapes:
        problems.append(f"{len(escapes)} non-Grafx escape(s): {escapes[:3]}")
    if torn:
        problems.append(f"{len(torn)} torn read(s): {torn[:2]}")
    if missing:
        problems.append(f"{len(missing)} acknowledged row(s) LOST: {missing[:8]}")
    if phantom:
        problems.append(f"{len(phantom)} row(s) nobody acknowledged: {phantom[:8]}")
    if len(stored) != len(set(stored)):
        problems.append("a row is stored more than once")
    if sorted(stored) != sorted(stored_after):
        problems.append("the reopened database disagrees with the live one")
    if live_findings or reopened_findings:
        problems.append(f"verify(): live={[f.kind for f in live_findings[:3]]} "
                        f"reopened={[f.kind for f in reopened_findings[:3]]}")
    if any(owner == -1 for _identity, owner in owners):
        problems.append("a contended row was never updated by anybody")

    expected = WRITERS * DISJOINT_TXNS * ROWS_PER_TXN
    print(f"\nrows: acknowledged {len(acknowledged)}/{expected} disjoint, stored "
          f"{len(stored)} total ({len(CONTENDED_KEYS)} contended seeds)")
    print("=" * 78)
    if problems:
        print(f"SMOKE: {len(problems)} PROBLEM(S)")
        for problem in problems:
            print(f"  * {problem}")
    else:
        print("SMOKE: PASSED -- nothing lost, nothing duplicated, no phantom, no torn read, "
              "no escape, verify clean live and after reopen")
    shutil.rmtree(root, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
