# Okto Grafx

An embedded graph database for Python: correct under multi-process concurrency, verifiable on disk,
and recoverable by construction.

Okto Grafx runs inside your process, stores a database as a directory of files, and lets **several
processes and several threads read and write it at the same time**. There is no server to run and
no daemon to keep alive. The core is pure Python and the standard library is its only runtime
requirement.

**Version 0.0.2 — pre-alpha.** The on-disk format, the public API and the query surface may all
change. Read [Status and limitations](#status-and-limitations) before you rely on it.

---

## Contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command line](#command-line)
- [Architecture](#architecture)
- [Ports and adapters](#ports-and-adapters)
- [Configuration](#configuration)
- [Errors](#errors)
- [Status and limitations](#status-and-limitations)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## Features

### Concurrency that is real, not serialized behind a lock

- **N processes and N threads, reading and writing.** A commit is refused only when its partitions
  genuinely intersect another commit's, never because another writer exists. Two writers touching
  different rows both succeed.
- **Snapshot isolation.** A reader sees the state of the instant it opened, for its whole life,
  while other processes commit. Readers never block writers; writers never block readers.
- **Optimistic concurrency control.** A conflict is reported as `GrafxWriteConflict` with
  `retryable=True` and the partitions that intersected, so a caller retries with a fresh snapshot
  instead of guessing.
- **Process coordination on the filesystem.** A lease with an epoch, a liveness clock that is
  monotonic, and takeover of a dead writer's lease with stale-epoch rejection — so a process that
  was paused, swapped out or SIGSTOPped cannot write after its lease was taken.

### Durability that does not lie

- **Write-ahead log.** A commit returns only after its records survive the log's barrier. What is
  published is logged; what is applied is logged.
- **Self-describing records** with CRC-32C checksums, in segments with an LSN mark index, recycled
  only above the checkpoint and below the oldest live reader.
- **Recovery at open.** The log is replayed idempotently; a torn tail is truncated at the last
  intact record and the evidence is preserved rather than discarded.
- **Crash-tested.** A process killed with `os._exit(9)` immediately after a commit returns loses
  nothing: the rows are in the log and the next open replays them.

### Verifiability

- **`verify()` walks the database and reports findings with their location** — which file, which
  page, which slot, which sequence number, which index. A clean database reports nothing, and the
  report carries counts as well as findings so a caller can tell "nothing was wrong" from "nothing
  was checked".
- **A forensic ledger and quarantine.** Damaged bytes are preserved and located rather than
  silently repaired or thrown away.

### Query

openCypher in the Kùzu dialect, executed by a planner that produces one operator tree per statement.

| Supported | Notes |
|---|---|
| `CREATE NODE TABLE` / `CREATE REL TABLE` | with `PRIMARY KEY`, typed columns, `FROM`/`TO` |
| `CREATE VECTOR SPACE` | dimension, metric, storage dtype |
| `CREATE` (nodes and relationships) | patterns with inline properties |
| `MATCH` … `WHERE` … `RETURN` | equality, comparison, `STARTS WITH`, `ENDS WITH`, boolean operators |
| `MERGE` | matches on the properties the pattern NAMED |
| `SET` | on node properties and matched relationship properties; relationship endpoints are immutable |
| `DELETE` | nodes, and relationships a `MATCH` bound |
| `DETACH DELETE` | ends every relationship incident on the node together with it |
| `UNWIND $rows AS r` | one leading list source, followed directly by `RETURN` or by one single-node `MATCH` and `SET` |
| `WITH` … `WHERE` | non-aggregating projection stages; each one replaces the scope with the names it projects, and its `WHERE` runs after the projection |
| `MATCH (n)` | a node with no label reads every node table as one set; filters, `label(n)`, aggregates, `ORDER BY` and windows apply to the union, and an undeclared property reads as null |
| Traversal | one hop, bounded ranges `[:REL*1..3]`, both directions, relationship isomorphism |
| `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT` | |
| Aggregates | `count`, `min`, `max`, and friends |
| Scalar functions | `coalesce`, `string_split`, `size`, `label`, `timestamp` |
| Conditional/list expressions | searched and simple `CASE`; one-based and negative `list[index]` |
| Parameters | `$name`, refused before anything runs if one is missing |

`MATCH` in a write transaction sees that owner's earlier node inserts, updates and deletes. A
dirty node table plans a scan plus the private overlay instead of consulting an index that only
describes committed rows. A `CREATE` may name a node an earlier statement of the same
transaction created, and both private endpoint identities are resolved before the first heap
write. Traversal reads that owner's combined view: relationships it created, relationships it
updated or ended, and endpoint nodes it created, updated or ended, with multiplicity, direction
and self-loops preserved. Pending relationship inserts force one grouped scan plus the overlay,
because endpoint indexes contain only committed edges; update/delete-only overlays can still use
fresh indexes and validate or suppress their candidates. A start node created by this transaction
takes the scan path even when the relationship table has no pending insert, because its private
identity has no index encoding. Two same-statement shapes stay refused rather than guessed: an edge
whose endpoint that very statement is creating, and a `DETACH DELETE` of a node an edge held by
that same statement points at. Vector search over a dirty table is fail-closed. Updates of
relationship properties are owner-visible, while their `_from`/`_to` layout columns remain
immutable. A table declared inside a transaction is usable by that transaction's own later
statements and becomes visible to every other transaction when it commits — schema changes are
transactions like any other.

### Indexes

- **A declared `PRIMARY KEY` gets an index automatically**, created by the DDL and re-adopted at
  every later open. A keyed read plans an index seek; an unkeyed predicate plans a scan.
- **A relationship table gets an index per endpoint** (`ef_`/`et_`), so traversal expands a
  bounded frontier by lookup instead of reading every edge, switching to one grouped scan when
  the frontier grows past the point where the scan is cheaper.
- **Dual visibility (CONTRACT §8.7).** An EXACT index returns candidates that are validated against
  the heap under the caller's own snapshot — so the index may be a superset and can never be a wrong
  answer. A PROXIMITY index is versioned with tombstones and a horizon, and its entries are the
  answer.
- **A stale index is never used.** A stale index is a *subset* of the heap, which validation cannot
  repair, so both the planner and the uniqueness check fall back to the scan they did before any
  index existed: slower, and right.

### Embeddings, first class

- `CREATE VECTOR SPACE` declares a dimension, a metric (`cosine`, `l2`, `dot`) and a storage dtype.
- A node table declares a `VECTOR(space)` column, and the index that makes it searchable is created
  with the table.
- `db.search_vectors(reader, space=…, k=…, query=…)` returns the nearest rows visible to an active
  read transaction, reporting the **regime** it answered in (`exact` or `approximate`) and the
  `achieved_k`, so a caller can tell an exhaustive answer from an approximate one. The database
  validates that the transaction is active and belongs to it; no raw transaction context or
  mutable vector engine is exposed.

### Observability

- A frozen metric catalogue: every metric declared once, in one place, so a name that leaves the
  catalogue breaks the import rather than a scrape in production.
- Three sinks: no-op (allocates and formats nothing), OpenMetrics over a loopback-by-default
  endpoint, and JSON documents to a rotating file.
- A sanitised, bounded event sink on a standard-library logger.

### Windows and POSIX as equal citizens

Both families are supported in behaviour, and the suite enforces it: a test that exercises one
family must declare its counterpart for the other, and a skip must be attributed.

---

## Installation

Python 3.11, 3.12 or 3.13.

```bash
pip install okto-grafx              # pure Python, no runtime dependency
pip install "okto-grafx[accel]"     # recommended: same answers, measurably faster
```

### What `[accel]` adds, and why it is safe

Two accelerators behind ports the engine already declares.

**`google-crc32c`** — a native CRC-32C. **The accelerated checksum is not a different answer.**
`install_crc32c` replays an acceptance corpus against the pure-Python reference and refuses a
candidate that disagrees on any input *before* installing it, so a database written by one build
reads identically in the other. It is worth installing: on Linux the durable-commit multiple against
the reference engine falls from ~17× to **~5×** with it, which is inside the ceiling that binding
decision D5 sets.

**`numpy`** — accelerated vector math. Selected only by `vector_math="numpy"`, never automatically.
The pure and accelerated adapters agree to a *stated tolerance* rather than exactly, so a selector
that silently bound whichever adapter happened to be installed would make the ranking of a query
depend on the machine it ran on. `auto` therefore binds the pure oracle deliberately, and `"numpy"`
refuses when the extra is absent rather than falling back to something the caller did not ask for.

### From source

```bash
git clone https://github.com/OktoLabsAI/okto-grafx.git
cd okto-grafx
pip install -e ".[dev,accel]"
pytest -q
```

---

## Quick start

### Two doors, and the line between them

`db.execute(...)` is the **autocommit read**. Writes go through a transaction. That distinction is
`CONTRACT.md` §10 and it is worth learning first, because it is the thing new callers most often
get wrong.

```python
from okto_grafx import connect

db = connect("./mydb")

# --- schema -------------------------------------------------------------------
with db.begin("write") as txn:
    txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, city STRING, PRIMARY KEY(id))")
    txn.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")

# --- rows ---------------------------------------------------------------------
with db.begin("write") as txn:
    txn.execute("CREATE (:Person {id: 1, name: 'Ada',   city: 'London'})")
    txn.execute("CREATE (:Person {id: 2, name: 'Grace', city: 'New York'})")
    txn.execute("CREATE (:Person {id: 3, name: 'Alan',  city: 'London'})")

# MATCH sees what this transaction staged, so the edge can be created beside the nodes it joins.
# The endpoints are named by identities the commit resolves before its first write; what a
# statement cannot name is a node that SAME statement is creating, which has nothing to resolve.
with db.begin("write") as txn:
    txn.execute("CREATE (:Person {id: 4, name: 'Edsger', city: 'Rotterdam'})")
    txn.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 4}) CREATE (a)-[:Knows {since: 1994}]->(b)"
    )

# --- reads --------------------------------------------------------------------
db.execute("MATCH (p:Person) WHERE p.id = 1 RETURN p.name").rows
# (('Ada',),)

db.execute("MATCH (p:Person) WHERE p.city = $c RETURN p.name ORDER BY p.name", {"c": "London"}).rows
# (('Ada',), ('Alan',))

db.execute("MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.name, b.name").rows
# (('Ada', 'Grace'),)

# --- updates and deletes ------------------------------------------------------
with db.begin("write") as txn:
    txn.execute("MATCH (p:Person) WHERE p.id = 3 SET p.city = 'Cambridge'")
with db.begin("write") as txn:
    txn.execute("MATCH (p:Person) WHERE p.id = 3 DELETE p")

# --- operations ---------------------------------------------------------------
db.verify("all")     # walks pages, records and indexes; a clean database reports nothing
db.checkpoint()      # puts committed state on the platter and reclaims the log
db.close()
```

### Handling a write conflict

A conflict is the protocol working, not an error in your code. Retry with a fresh transaction.

```python
from okto_grafx.domain.errors import GrafxError

import random, time

def transfer(db, statement, parameters, attempts=20):
    for attempt in range(attempts):
        try:
            with db.begin("write") as txn:
                txn.execute(statement, parameters)
            return
        except GrafxError as refused:
            if not getattr(refused, "retryable", False):
                raise
            # Exponential backoff with full jitter, scaled to your platform's COMMIT cost --
            # a backoff smaller than one commit is a busy-wait. Measured: this halves the
            # median latency and the conflict count under contention; it cannot fix the tail,
            # which is a fairness property (see docs/PERFORMANCE.md).
            time.sleep(random.uniform(0.0, min(0.08 * 2 ** min(attempt, 4), 0.5)))
    raise RuntimeError("gave up after too many conflicts")
```

`retryable` is part of the taxonomy, not a guess: `GrafxWriteConflict` and `GrafxLeaseTimeout`
carry it, `GrafxCorruptionDetected` does not.

### Several processes on one database

Nothing special is required — open it in each process.

```python
# process A                          # process B, at the same time
db = connect("./mydb")               db = connect("./mydb")
with db.begin("write") as txn:       n = db.execute(
    txn.execute("CREATE (...)")          "MATCH (p:Person) RETURN count(*)"
                                     ).rows[0][0]
```

### Embeddings

```python
with db.begin("write") as txn:
    txn.execute("CREATE VECTOR SPACE minilm {dimension: 384, metric: 'cosine'}")
    txn.execute(
        "CREATE NODE TABLE Chunk(id INT64, body STRING, embedding VECTOR(minilm), PRIMARY KEY(id))"
    )

with db.begin("write") as txn:
    txn.execute(
        "CREATE (:Chunk {id: 1, body: 'the text', embedding: $e})",
        {"e": [0.1] * 384},
    )

reader = db.begin("read")
try:
    hits = db.search_vectors(reader, space="minilm", k=10, query=[0.1] * 384)
    print(hits.regime, hits.achieved_k)   # 'exact' or 'approximate', and how many it reached
finally:
    reader.rollback()
```

### Bounded physical scans

Integrations that need a lossless logical export can page stored rows without materializing an
`ORDER BY` query. The cursor stays inside one active read transaction, so every page observes the
same MVCC snapshot:

```python
with db.begin("read") as txn:
    cursor = None
    while True:
        page = txn.scan_rows_v1("Chunk", limit=256, cursor=cursor)
        for row in page.rows:
            print(row.record_id, row.values)  # values follow TableDef.columns
        cursor = page.next_cursor
        if cursor is None:
            break
```

Relationship values start with `_from` and `_to`, and duplicate/parallel occurrences are returned
separately. `ScanCursorV1` is opaque, non-serializable, single-use and cannot cross a transaction,
table or database. This is a physical scan primitive for adapters, not a portable backup format
or bulk import API.

### Safe observations

Properties such as `db.catalog`, `db.indexes`, `db.wal`, `db.storage` and `db.metrics` are frozen
snapshots for schema, inventory and diagnostics. They never retain the storage device, page pool,
WAL, transaction manager or adapter callbacks. Writes go through transactions or explicit gated
database methods (`checkpoint`, `recover`, `flush`, `publish_metrics`); there is no `unsafe=True`
escape. `Transaction` exposes `snapshot`, `mode`, `txn_id`, `active` and `report`, but never its
mutable engine context.

### In memory

`connect(":memory:")` selects the in-memory device, which has the same transactional semantics as
the directory-backed one. Useful for tests; nothing survives the process.

---

## Command line

Installing the package provides `oktografx`.

```
oktografx status PATH                 # what state this database is in
oktografx verify PATH [--scope all]   # walk it and report every finding
oktografx query PATH STMT             # autocommit read; add --write for a write transaction
oktografx recovery PATH               # what the recovery pass at open did
oktografx ledger list|inspect|export  # the forensic evidence preserved when something went wrong
oktografx quarantine list|inspect|read
oktografx metrics PATH                # the endpoint and the current value of every metric
```

**Exit codes are a contract**, so a CI job can switch on them:

| code | meaning |
|---|---|
| `0` | clean |
| `1` | findings — the database is readable but `verify` reported something |
| `2` | unreadable command line |
| `3` | typed refusal (a `Grafx*` error the caller should act on) |
| `4` | damaged bytes |
| `5` | retryable refusal — try again |
| `6` | inconclusive |
| `70` | internal error |
| `130` | interrupted |

---

## Architecture

Okto Grafx is **hexagonal**, and the boundary is enforced by a test rather than by convention.

```
                      ┌──────────────────────────────────────────┐
   your code  ───────►│  okto_grafx.connect() / Database / CLI   │   api/, cli/
                      └───────────────────┬──────────────────────┘
                                          │
                      ┌───────────────────▼──────────────────────┐
                      │              runtime/                    │   composition root:
                      │  config · port registry · bootstrap      │   builds and wires everything
                      └───────────────────┬──────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │                             engine/                               │
        │  buffer pool · heap store · catalog store · WAL manager           │
        │  transaction manager · query engine · index manager               │
        │  vector engine · recovery manager · verifier · coordination       │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │                             domain/                               │
        │  page layout · records · schema · values · query AST and planner  │
        │  error taxonomy · metric catalogue · PORTS (protocols only)       │
        └─────────────────────────────────┬─────────────────────────────────┘
                                          │  protocols
        ┌─────────────────────────────────▼─────────────────────────────────┐
        │                            adapters/                              │
        │  every line of OS access lives here, and only here                │
        └───────────────────────────────────────────────────────────────────┘
```

**`domain/` and `engine/` are mechanism-free.** They import no `os`, no `time`, no `socket`, no
`threading` primitive — every one of those arrives through a port. `tests/test_import_boundary.py`
walks the import graph and fails the build if that stops being true. This is not an aesthetic
choice: it is what makes the engine testable against a fault-injecting device, and what makes
Windows and POSIX two adapters rather than two code paths.

### The layers

| Layer | Holds | Rule |
|---|---|---|
| `domain/` | Page and record formats, the schema and value model, the query AST and planner, the error taxonomy, the metric catalogue, and the **port protocols** | Pure. No mechanism, no I/O, no clock. |
| `engine/` | The components that implement the protocols of `CONTRACT.md` | Talks to the world only through ports. |
| `adapters/` | Concrete implementations of each port | The only place the operating system is touched. |
| `runtime/` | Config, the port registry, the bootstrap | The composition root. Chooses adapters; nothing below it does. |
| `api/`, `cli/` | The public doors | Thin. Assembly and presentation. |

### How a commit works

The protocol is `CONTRACT.md` §8.5 and is worth reading in outline, because most of the guarantees
follow from its ordering:

1. A transaction that staged nothing finishes without appending a record.
2. The writer takes a **lease** and confirms its **epoch** before a byte can reach the device.
3. Inside the **commit section** (exclusive across processes):
   1. the lease is validated again;
   2. the published commit position is read, and the buffer pool starts a fresh read view — the
      commit decides against the picture as it is *now*, not as this pool last cached it;
   3. **optimistic validation**: a conflict exists when a COMMIT record appended after this
      transaction's snapshot wrote a partition this transaction read or wrote — intersection, never
      the mere existence of another writer;
   4. rows are written and the pages they landed on are declared, then validation runs again on the
      page half;
   5. the records are appended and the log takes its **barrier** — the commit is durable here;
   6. the page images and index changes are applied and flushed to the device;
   7. the new commit state is published.
4. The lease is dropped and the reader registration withdrawn.

Two consequences worth stating. A page image replaces the *whole* page, so two commits that write
one page conflict however disjoint the rows they thought they were touching were. And the data files
are deliberately **not** fsynced at step 6 — the log is the authority on durability and the redo is
idempotent — but they *are* written, because a page that exists only in one process's memory is
invisible to every other process.

### Storage layout

```
mydb/
  identity.dat        # what this database is; refuses a mismatched open
  catalog.dat         # the schema
  heap.dat            # rows, in slotted pages chained per table
  index/              # one file per secondary index
  wal/                # segmented write-ahead log
  control/            # lease, commit state, reader registrations
  ledger/             # forensic evidence
  quarantine/         # preserved damaged bytes
```

Every page carries a CRC-32C over its own bytes and a sequence counter that is always even in a
durable image, so a reader that meets an odd counter is looking at a write that did not complete and
reads again.

---

## Ports and adapters

Seven ports. The registry is **fail-closed**: an empty slot is never filled with a silent default
and never degrades into a no-op — opening a database with an incomplete registry raises
`GrafxPortNotConfigured` naming *every* missing slot in one error.

| Port | Protocol | What it abstracts | Default adapter | Also shipped |
|---|---|---|---|---|
| `storage` | `StorageDevice` | Files, pages, growth, durability barriers, directory listing | `LocalStorageDevice` — a directory on the real filesystem | `MemoryStorageDevice` (`:memory:`), `FaultInjectingStorageDevice` (tests) |
| `clock` | `Clock` | Monotonic time for liveness, wall time for human-facing stamps only | `SystemClock` | — |
| `coordinator` | `ProcessCoordinator` | Leases, epochs, exclusive sections, reader registration, dead-owner takeover | `LocalProcessCoordinator` — lock files under `<db>/control` | same class, `lock_directory=None` for in-memory process-wide sections |
| `codec` | `PageCodec` | Encoding and decoding a page image, checksum included | `PageCodecV1` | — |
| `metrics` | `MetricsSink` | Counters, gauges, histograms, timers | `NoOpMetricsSink` | `OpenMetricsSink`, `JsonMetricsSink` |
| `events` | `EventSink` | Structured, sanitised, bounded event records | `LoggingEventSink` — standard-library `logging` | — |
| `vector_math` | `VectorMath` | Distance and similarity kernels | `PureVectorMath` | `NumpyVectorMath` (needs `[accel]`) |

There is an eighth pluggable thing that is **not** a registry slot, because it is installed
process-wide rather than injected per object: the **CRC-32C implementation**. `checksum="pure"`
always binds the reference; `checksum="auto"` accelerates when `google-crc32c` is installed, and
`install_crc32c` checks byte-identical, unsigned 32-bit digests against the reference before
installing anything and checks injected callables again on real inputs. The closed
`google-crc32c`/`crc32c` provider list uses the corpus-validated fast path; a vendored provider can
make the same trust decision explicitly through `NativeCrc32c(..., verify_runtime=False)`.
Supplying a custom registry replaces the seven ports, but does not disable this process-wide
`checksum` selection.

### Substituting an adapter

Anything that satisfies the protocol is acceptable — the registry checks structurally, so you do not
inherit from anything.

<!-- okto-grafx-doc-test -->

```python
from contextlib import nullcontext

from okto_grafx import DatabaseConfig, connect
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports

class CountingMetrics:
    """Any object with the MetricsSink members is a MetricsSink."""
    enabled = True
    def register(self, descriptor): ...
    def increment(self, name, value=1.0, labels=None): ...
    def observe(self, name, value, labels=None): ...
    def set_gauge(self, name, value, labels=None): ...
    def time(self, name, labels=None): return nullcontext()
    def snapshot(self): return {}

config = DatabaseConfig(path=":memory:", metrics="noop")
registry = build_default_registry(config)
registry.bind("metrics", CountingMetrics())
try:
    with connect(":memory:", registry=registry) as db:
        assert db.metrics.enabled
finally:
    # A caller-supplied registry stays caller-owned after Database.close().
    release_ports(registry)
```

A device is the interesting one to substitute: `FaultInjectingStorageDevice` is how the suite
proves that a refused write, a full device or a failed barrier produce a typed refusal and never a
half-written page.

---

## Configuration

`connect(path, **options)` builds a `DatabaseConfig`. Every option is validated and copied to exact
built-in scalar values before any adapter or persisted descriptor sees it; an invalid option is
refused with the field name the caller actually wrote.

| Option | Default | Notes |
|---|---|---|
| `page_size` | `8192` | Fixed for the life of the database |
| `partitions_per_table` | `64` | Conflict granularity — more partitions, fewer false conflicts |
| `identity_lease_size` | `64` | Burn-only row-id range reserved durably per refill; larger values reduce heap page-0 metadata commits at the cost of wider harmless gaps after close/crash |
| `buffer_budget_bytes` | `64 MiB` | Per database, never shared; must hold at least two configured pages |
| `max_open_files` | `128` | Local descriptor-cache budget; tune down for descriptor-constrained hosts |
| `descriptor_revalidation` | `"strict"` | `"strict"` proves every cached descriptor hit; `"generation"` amortizes proofs for a closed canonical-file whitelist and requires an exclusively Grafx/Pulse-managed directory |
| `recovery_policy` | `"replay"` | What the pass at open is allowed to do |
| `lease_ttl_seconds` | `5.0` | How long a writer's lease stays valid without renewal |
| `lease_timeout_seconds` | `10.0` | How long to wait for another writer's lease |
| `commit_lock_timeout_seconds` | `30.0` | How long to wait at the commit section |
| `reader_stall_threshold_seconds` | `15.0` | When a reader stops holding the horizon down |
| `wal_segment_bytes` | `4 MiB` | Log segment target, from 256 B through the reader's 1 GiB ceiling; an exceptional batch that would cross the ceiling is refused before writing |
| `wal_max_bytes` | `None` | Optional soft high-water trigger: after a durable write, checkpoint when live WAL bytes reach this value; reader pins, atomic batches and deferred recycling may retain more without data loss |
| `checkpoint_interval_records` | `512` | After a durable write, checkpoint when the published WAL distance reaches this many records; a failed attempt is reported and retried after the next write |
| `max_statement_writes` | `None` | Optional hard limit on logical row writes retained by one statement |
| `max_result_rows` | `None` | Optional hard limit on public result rows; row N+1 is refused before it is retained and before any remaining input is consumed |
| `max_intermediate_rows` | `None` | Optional hard limit per non-terminal physical operator over one execution; it is not a cumulative query-wide count |
| `max_traversal_expansions` | `None` | Optional cumulative per-query limit on relationship candidates examined by graph-pattern operators; candidate N+1 is refused before derived landing/filter work |
| `max_traversal_paths` | `None` | Optional cumulative per-query limit on visible paths admitted by graph-pattern operators; path N+1 is refused before frontier retention or return |
| `max_query_value_characters` | `65536` | Per-string parameter/result boundary; configurable from 1 through the hard 1,048,576-character guard; query-source literals keep their separate 16,384-character ceiling |
| `max_transaction_rows` | `None` | Optional hard limit on retained `row_intents` in one transaction |
| `max_transaction_bytes` | `None` | Optional hard limit on encoded row tuples, staged logical-record `encoded_length()` values and retained page-image generations; ordinary replacement charges the byte delta, while a rollback preimage held by a live statement mark remains charged until settle/discard |
| `max_wal_batch_bytes` | `None` | Optional hard limit on the sum of final record `encoded_length()` values, including `COMMIT` and excluding `SEGMENT_HEADER`; checked before WAL append |
| `metrics` | `"noop"` | `"noop"`, `"openmetrics"`, `"json"` |
| `metrics_destination` | `None` | Required file path for `"json"`; for `"openmetrics"`, `None` means `127.0.0.1:0` and an explicit IPv6 destination uses `[address]:port` |
| `allow_remote_metrics` | `False` | Exact boolean, valid only for `"openmetrics"`; permits a hostname or non-loopback address when explicitly `True` |
| `vector_math` | `"auto"` | `"auto"` and `"pure"` both bind the pure oracle; `"numpy"` requires `[accel]` |
| `checksum` | `"auto"` | `"auto"` accelerates when available; `"pure"` pins the reference |
| `vector_exact_scan_threshold` | `4096` | Below this many candidates, search is exhaustive |
| `vector_ef_search` | `320` | Base HNSW beam in the approximate regime; integer from 1 through 1,048,576 |
| `read_only` | `False` | Opens without writing anything, including recovery |

`descriptor_revalidation="strict"` is the safe default: on every cached hit, the local adapter
proves that the logical name still names the physical file held by its descriptor. The opt-in
`"generation"` mode keeps control records, `grafx.meta` and unknown names strict, but amortizes that
proof for canonical heap, catalog, index and WAL files. It must be used only when Okto Grafx and
Okto Pulse are the exclusive writers of the database directory. An external replacement of a
whitelisted file can otherwise remain undetected until a directed proof of that name, a full
generation invalidation or reopen, and a stale descriptor can read or write an inode no longer
named by the directory. The option is inert for `":memory:"`; a caller-supplied registry is validated
but its storage adapter is not reconfigured by it. See
[`ST2_DESCRIPTOR_REVALIDATION.md`](docs/architecture/ST2_DESCRIPTOR_REVALIDATION.md) for the exact
whitelist, transition table, coexistence rules, advantages and risks.
The read-only `database.descriptor_revalidation` property reports the effective process-local mode;
it is deliberately not part of the persisted `database.identity` record.

Without an override, an OpenMetrics destination must name a literal IP address that
`ipaddress.ip_address(host).is_loopback` classifies as loopback, for example IPv4 `127/8` or IPv6 `::1`.
Hostnames are not resolved for this decision, so even `localhost` is refused. A remote address or
hostname requires `allow_remote_metrics=True`; setting it for the no-op or JSON sink is itself
refused. Each remote-address or hostname publisher admitted by that override emits one
`RuntimeWarning` when it starts. This consent changes only where OpenMetrics may bind: it does not
add authentication, TLS or a firewall.

Configure IPv6 loopback as `metrics_destination="[::1]:0"`. The publisher binds `::1` with
`AF_INET6`, and `Database.metrics_endpoint` reports the usable bracketed URL
`http://[::1]:<chosen-port>/metrics`.

Recall is an offline calibration result, not a per-database runtime promise. The former
`vector_recall_target` connection option was removed; passing it now returns a typed migration
error. Set the benchmark floor with
`python -m bench.harness.gate --metrics <metrics.json> --require-recall --recall-target <floor>`.
Use `vector_ef_search` when the intended change is the HNSW work performed by runtime queries.

The four transaction limits are opt-in: `None` preserves the unbounded behaviour. Exceeding one
raises the non-retryable `GrafxTransactionBudgetExceeded`. A refused statement restores its exact
pre-statement staging, and a refused final WAL batch is rejected before append; these refusals do
not truncate the WAL or persist a partial statement.

The two query row limits are opt-in positive integers. `max_result_rows` counts the public
terminal incrementally; it consumes row N+1 only to refuse it, before retaining it or consuming the
rest of the stream and before `context.release()`. `max_intermediate_rows` counts each non-terminal
physical operator separately for the whole execution. A public terminal is charged only as result;
a terminal with no public columns is charged as intermediate. Overrun raises the non-retryable
`GrafxQueryBudgetExceeded`, without truncating state or releasing a partial write statement.

The two traversal limits are likewise opt-in positive integers, but are cumulative across every
graph-pattern operator in one query. Variable and untyped traversal charge an expansion for each
candidate yielded by their selected endpoint source, before repeat-edge and landing checks; a
fixed relationship scan charges each stored or pending relationship it encounters, before its
pushed predicate and endpoint checks. A path is charged only after the applicable pushed predicate
and landing visibility checks, immediately before the path can enter a variable-length frontier or
be returned by a one-hop scan. The first over-limit unit is refused before it is retained or
returned. These limits cover Cypher relationship traversal and scans, not the separate internal
HNSW navigation performed by a vector-search operator. Physical rows read once to construct a
grouped endpoint fallback are auxiliary scan work and are not charged as candidate expansions.
When disabled the limits do not add traversal counters to `QueryResult.statistics`; when enabled,
the corresponding `traversal_expansions` or `traversal_paths` statistic records admitted work on
successful queries.

`max_query_value_characters` bounds each string parameter and each string copied across the public
query-result boundary. It defaults to 65,536 characters, while query-source string literals retain
their independent 16,384-character lexer ceiling. Applications may lower the value or raise it up
to the hard 1,048,576-character guard; values above the effective ceiling are refused before page
access. The option is process-local and does not change the on-disk format.

These are admission limits, not a complete query-memory budget. They do not bound payload bytes,
internal structures, auxiliary scans, RSS, deadlines, spill or stream results. Sort,
aggregate, distinct and eager operators may retain up to the configured rows or states before their
first yield; the memory of those payloads and structures is not bounded here.

---

## Errors

Failures produced by the engine and the adapters shipped with Okto Grafx are `GrafxError`
subclasses carrying a machine-readable `code`, a `retryable` flag and located `details`. A custom
adapter is trusted host code: the registry validates its shape without executing it, but does not
translate exceptions it raises later. Such an exception can therefore propagate unchanged.

| Error | `retryable` | Means |
|---|---|---|
| `GrafxWriteConflict` | ✅ | Partitions intersected another commit's — retry with a fresh snapshot |
| `GrafxLeaseTimeout` | ✅ | Another writer held the lease too long |
| `GrafxLeaseStolen` / `GrafxStaleEpoch` | ❌ | This writer was superseded; its writes are refused |
| `GrafxCorruptionDetected` | ❌ | Bytes that cannot be trusted, with the location |
| `GrafxDeviceFull` / `GrafxStorageError` | ✅ | The device refused |
| `GrafxDurabilityBarrierFailed` | ❌ | An fsync failed — nothing may be acknowledged as durable |
| `GrafxRecoveryRefused` | ❌ | Recovery would not be safe; the evidence is preserved |
| `GrafxBufferBudgetExceeded` | ✅ | The working set exceeded the budget |
| `GrafxTransactionBudgetExceeded` | ❌ | An enabled statement, transaction or final WAL-batch limit was exceeded before partial persistence |
| `GrafxQueryBudgetExceeded` | ❌ | An enabled public-result or per-operator intermediate row limit was exceeded before statement release |
| `GrafxSchemaVersionMismatch` | ❌ | This build cannot read this database |
| `GrafxPortNotConfigured` | ❌ | An incomplete registry, naming every missing slot |
| `GrafxTransactionStateError` | ❌ | The transaction is not in a state that allows this |
| `GrafxQueryError` / `GrafxParseError` / `GrafxPlanError` | ❌ | The statement |
| `GrafxIndexError` | ❌ | An index refused, including a stale one asked to answer |
| `GrafxVectorValidationError`, `GrafxEmbeddingSpaceMismatch`, `GrafxSpaceRetired` | ❌ | Embeddings |
| `GrafxConfigurationError` | ❌ | An option, naming the field |
| `GrafxUnsupportedOperation` | ❌ | Declared not to exist, rather than silently ignored |

---

## Status and limitations

**0.0.2 is pre-alpha.** It is tested hard — multi-process smoke tests, crash-and-recover
tests, a mutation battery with per-mutant verdicts — and it is still young. What that means in
practice:

- **The on-disk format is not stable.** A database written by one pre-alpha version may not open in another.
  There is no migration path yet.
- **Write throughput is currently platform-bound and serialized** — ~300 ms per durable commit on
  Windows, and all writers intersect on the table directory page, so disjoint writers queue. Reads
  are unaffected (measured: 1.5 ms indexed point reads under full write load). The numbers, their
  conditions, and the instruments to re-run them are in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).
- **Performance is not at parity on Windows.** Binding decision D5 sets relative ceilings against a
  reference engine; they are met on POSIX with `[accel]` and missed on Windows, where control-file
  publication costs ~16.5 ms against ~0.13 ms on Linux. The measurements and the analysis are in
  `docs/architecture/COMPONENTS.md`.
- **Traversal with an unbound target resolves landings by scanning the landing table** (edges
  store record identities, and identities carry no index yet). Endpoint indexes cover the edges
  themselves, so the old every-edge-per-node scan is gone, but a hop that lands on a large free
  table still pays one scan of it per traversal.
- **A plain `DELETE` of a node ends the node, not its relationships.** They stay on the pages
  as rows no traversal will follow — a landing whose snapshot cannot see the node is not
  reached — so the absence an ordinary `DELETE` promises is a logical one. `DETACH DELETE` is
  what ends the incident relationships with the node, physically, in the same commit.
- **Known gaps are written down** rather than hidden: see `docs/architecture/PUNCHLIST.md`.

**Deployment responsibility.** Okto Grafx is an embedded library for local or controlled
single-tenant use. It has no authentication or authorization and no network listener other than the
optional OpenMetrics endpoint. That endpoint is loopback-only by default; a caller can permit a
remote address or hostname only with `allow_remote_metrics=True`, which adds no authentication, TLS
or firewall. Operators are responsible for filesystem permissions, access control, backup, metrics
endpoint exposure, and for keeping the database directory off shared network filesystems whose
locking semantics differ from a local disk. A deployment that selects
`descriptor_revalidation="generation"` must additionally keep every live rename, replacement,
removal, restore and synchronization of database files under the Grafx/Pulse protocol; otherwise use
the default `"strict"` mode.

---

## Documentation

| Document | What it holds |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The detailed architecture: components, protocols, data flow, on-disk formats |
| [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) | Measured numbers with their conditions and the in-tree instruments that reproduce them — reads under load, traversal, scaling, and the D5 cross-platform record |
| [`docs/PORTS.md`](docs/PORTS.md) | Every port, its protocol, its default adapter, and how to write your own |
| `docs/specs/` | The two validated specifications this is built against |
| `docs/architecture/CONTRACT.md` | The frozen coordination substrate: error taxonomy, on-disk formats, the commit protocol, the metric catalogue, and the Definition of Done every component is reviewed against |
| `docs/architecture/COMPONENTS.md` | The component register, the sign-off record, and every carried finding with the measurement behind it |
| [`docs/architecture/ST2_DESCRIPTOR_REVALIDATION.md`](docs/architecture/ST2_DESCRIPTOR_REVALIDATION.md) | The strict/default and generation/opt-in descriptor identity policies, exact whitelist, risks and deployment guidance |
| `docs/architecture/LESSONS.md` | What went wrong while building this and what it taught |
| `docs/architecture/PUNCHLIST.md` | Known gaps, written down rather than hidden |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, per release |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security vulnerabilities using
[SECURITY.md](SECURITY.md), not a public issue.

## License

Copyright 2026 Okto Labs.

Okto Grafx is distributed under the **Elastic License 2.0** together with the project's SaaS,
competing-service, internal-use, and attribution addendum.

The licensor's intent, stated in the addendum itself: **building applications on Okto Grafx is
unrestricted; selling Okto Grafx is not.**

- **Permitted**, including commercially and including in a multi-tenant SaaS: embedding Okto Grafx in
  your own application as its storage engine. An application that uses Okto Grafx to store its data
  is providing *its* features, not the software's.
- **Permitted**: internal use at any scale, single-tenant deployments, consulting and managed
  operations, adapters and tools you publish under your own license, and benchmarking — including
  publishing results, favorable or otherwise.
- **Prohibited**: offering Okto Grafx *itself* to third parties — a database-as-a-service, a hosted
  graph API, a white-label or OEM redistribution, or a competing product built from it.
- **Required**: the Okto Labs and Okto Grafx names, the LICENSE file, and the package metadata stay
  intact in any redistribution. An application that merely embeds the library does **not** have to
  show Okto Labs branding in its own interface; a mention in its dependency or acknowledgements
  listing is enough.

Read the complete [LICENSE](LICENSE) before use or redistribution. If a use is genuinely ambiguous,
contact dev@oktolabs.ai — the addendum says to ask.
