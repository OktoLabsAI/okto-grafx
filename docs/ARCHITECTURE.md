# Okto Grafx — Architecture

This document describes how Okto Grafx is built: the layers, the components, the protocols between
them, and the on-disk formats. It is the working map. The normative source is
`docs/architecture/CONTRACT.md`, which is frozen; where this document and the contract disagree, the
contract wins and this document is wrong.

For the port protocols and how to substitute an adapter, see [PORTS.md](PORTS.md).

---

## 1. The shape and why

Okto Grafx is a **hexagonal** (ports and adapters) system, and the boundary is load-bearing rather
than decorative.

```
  ┌───────────────────────────────────────────────────────────────────────────┐
  │  api/            connect(), Database, Transaction, QueryResult            │
  │  cli/            oktografx: status, verify, query, recovery, ledger, …    │
  └────────────────────────────────┬──────────────────────────────────────────┘
                                   │
  ┌────────────────────────────────▼──────────────────────────────────────────┐
  │  runtime/        DatabaseConfig · PortRegistry · bootstrap                │
  │                  THE COMPOSITION ROOT. The only place that chooses an     │
  │                  adapter, opens a socket, or reads the environment.       │
  └────────────────────────────────┬──────────────────────────────────────────┘
                                   │
  ┌────────────────────────────────▼──────────────────────────────────────────┐
  │  engine/         BufferPool · HeapStore · CatalogStore · WalManager       │
  │                  TransactionManager · QueryEngine · IndexManager          │
  │                  VectorEngine · RecoveryManager · Verifier · Coordination │
  │                  MECHANISM-FREE. Reaches the world only through ports.    │
  └────────────────────────────────┬──────────────────────────────────────────┘
                                   │
  ┌────────────────────────────────▼──────────────────────────────────────────┐
  │  domain/         page formats · records · schema · values                 │
  │                  query AST, analyzer, planner · error taxonomy            │
  │                  metric catalogue · ports/ (PROTOCOLS ONLY)               │
  │                  PURE. No I/O, no clock, no threading primitive.          │
  └────────────────────────────────┬──────────────────────────────────────────┘
                                   │ protocols
  ┌────────────────────────────────▼──────────────────────────────────────────┐
  │  adapters/       storage_local · storage_memory · storage_fault           │
  │                  clock_system · codec_v1 · coordination_local             │
  │                  metrics_noop/json/openmetrics · events_logging           │
  │                  vectormath_pure/numpy · checksum_pure/native             │
  │                  EVERY LINE OF OS ACCESS, AND ONLY HERE.                  │
  └───────────────────────────────────────────────────────────────────────────┘
```

**The rule is enforced, not asked for.** `tests/test_import_boundary.py` walks the import graph and
fails the build if `domain/**` or `engine/**` imports `os`, `time`, `socket`, `threading`,
`pathlib`, or any adapter module. Three things follow from that, and they are the reason the rule
exists rather than a nice side effect:

1. **The engine is testable against a hostile device.** `FaultInjectingStorageDevice` refuses the
   *n*-th write, or reports a full device, or fails a barrier — and the engine's response is
   observable without a real disk that behaves that way.
2. **Windows and POSIX are two adapters, not two code paths.** Binding decision D9 makes both
   families equal citizens. A platform difference that leaked into the engine would be a branch that
   only one CI machine could ever exercise.
3. **Time is injectable.** Lease expiry, liveness and takeover are tested with a `ManualClock` that
   advances exactly as much as the test says, so a race is deterministic rather than slept on.

### Layer responsibilities

| Layer | Owns | Must not |
|---|---|---|
| `domain/` | Formats, the model, the query language, the taxonomy, the metric catalogue, the port protocols | Touch anything outside itself |
| `engine/` | The behaviour of `CONTRACT.md` | Import a mechanism or an adapter |
| `adapters/` | One concrete answer per port | Contain policy the engine should own |
| `runtime/` | Choosing adapters, validating configuration, wiring | Be imported by the engine |
| `api/`, `cli/` | Public doors, presentation, exit codes | Contain engine logic |

---

## 2. Components

The engine is fourteen components, C0–C13. Each has a frozen surface in `CONTRACT.md` and a sign-off
record in `docs/architecture/COMPONENTS.md`.

| | Component | Module | Owns |
|---|---|---|---|
| C0 | Foundation | `runtime/`, `domain/errors.py` | Config, registry, bootstrap, the error taxonomy |
| C1 | Storage core | `engine/buffer_pool.py`, `domain/page/` | Pages, the buffer pool, checksums, the torn-read protocol |
| C2 | Storage adapters | `adapters/storage_*.py` | Real files, memory, fault injection |
| C3 | Process coordinator | `engine/coordination.py`, `adapters/coordination_local.py` | Leases, epochs, sections, reader registration |
| C4 | WAL | `engine/wal_manager.py` | Segmented log, records, barriers, recycling |
| C5 | Transaction manager | `engine/txn_manager.py` | MVCC, the commit protocol, OCC validation |
| C6 | Recovery manager | `engine/recovery_manager.py` | Replay, truncation, ledger, quarantine |
| C7 | Index framework | `engine/index_manager.py` | Hash and proximity indexes, dual visibility, staleness |
| C8 | Observability | `adapters/metrics_*.py`, `adapters/events_logging.py` | The metric catalogue and its sinks |
| C9 | Vector engine | `engine/vector_engine.py` | Spaces, HNSW, search regimes |
| C10 | Query engine | `engine/query_engine.py`, `domain/query/` | Parse, analyze, plan, execute |
| C11 | Public API | `api/`, `engine/database.py` | `connect`, `Database`, `Transaction` |
| C12 | CLI | `cli/` | Commands, output shapes, exit codes |
| C13 | Verification | `engine/verifier.py`, `domain/verify/` | The walk and its findings |

---

## 3. Data flow

### 3.1 Opening a database

```
connect(path, **options)
  └─ DatabaseConfig            validate every option, refuse naming the field the caller wrote
  └─ build_default_registry    choose an adapter per port from the configuration
  └─ install_checksum          install CRC-32C process-wide, proving it against the reference
  └─ PortRegistry.require      fail closed: name EVERY missing slot in one error
  └─ open the device, the identity file, the catalog, the heap, the log
  └─ RecoveryManager.recover   replay the log idempotently; truncate a torn tail; preserve evidence
  └─ re-adopt the indexes      primary-key indexes, then declared vector indexes
  └─ IndexManager.open         compare each index's claimed position against the published one
  └─ Database                  ready
```

Two details worth knowing. Recovery runs **before** any transaction can open, so a caller never sees
a half-replayed database. And a `read_only=True` open writes nothing at all — including recovery,
which is why a read-only handle can be pointed at a database another process owns.

### 3.2 A read

```
db.execute("MATCH …")                        # the autocommit read (CONTRACT §10)
  └─ begin("read")
       └─ participant section (threads of this process take turns)
       └─ register as a reader at the published position  ← holds the recycle horizon down
       └─ pool.begin_read_view(published_lsn)             ← drop frames cached before a foreign commit
       └─ Snapshot(read_lsn)
  └─ parse → analyze → plan                  # one operator tree, chosen once
  └─ execute the tree
       ├─ IndexSeek      when an index covers the predicate exactly and is FRESH
       │    └─ IndexManager.lookup → candidates validated against the heap under this snapshot
       └─ NodeScan       otherwise: walk the table's page chain under this snapshot
  └─ rollback → withdraw the registration
```

`pool.begin_read_view` is the piece that makes a long-lived process correct. Every epoch the pool
keeps is process-local; nothing moves it when *another* participant commits. Without this door, a
participant that had already read a table went on answering from frames it cached before that
commit — no error, no missing file, just fewer rows than exist, which is the worst shape a wrong
answer can take.

### 3.3 A write

The full protocol is `CONTRACT.md` §8.5. In outline:

```
with db.begin("write") as txn:
    txn.execute("CREATE …")        # staged: an intent, not a row. Nothing is on the device.
# __exit__ → commit
```

```
 1. staged nothing?  →  finish without appending a record
 2. take the LEASE, validate the EPOCH                        ← BR-7: no byte before this
 3. enter the COMMIT SECTION (exclusive across processes)
    3.1  validate the lease again
    3.2  read the published position; pool.begin_read_view    ← decide against NOW, not the cache
    3.3  freeze original interests; run OCC against the transaction snapshot
    3.4  sync durable artifacts; materialize rows from NOW; OCC only the new physical-page delta
    3.5  append the records; wal.barrier()                    ← DURABLE HERE
    3.6  apply the page images and index changes; flush to the device
    3.7  publish the new commit state
 4. drop the lease; withdraw the reader registration
```

Three properties of that ordering are worth stating because everything else follows from them.

**A page image replaces the whole page.** So two commits that write one page conflict however
disjoint the rows they thought they were touching were, and the pages a row write lands on are only
known *after* the row is written — which is why validation runs twice.

The two validations do not share a moving baseline. The first one always covers the complete
logical and pre-staged interest frozen at the transaction's original snapshot. Only physical pages
that did not exist in that frozen set and were discovered while materializing from the current
durable view use that current view as the second validation's baseline. A pre-staged page, a late
logical interest or a page that overlaps the frozen set cannot move to the newer baseline. Durable
index adoption and artifact-provenance checks happen only after the first validation and before any
WAL byte is appended.

**The data files are not fsynced at 3.6, but they are written.** The log is the authority on
durability and the redo is idempotent, so a barrier on the data files would buy nothing. But a page
that exists only in one process's memory is invisible to every other process, and the commit is
about to publish a position that says the page is there — so it is written, unsynced.

**What a refused attempt leaves behind is decided all at once.** An attempt that is refused takes its
pages back either by dropping every frame unwritten (when the device has never seen them) or by
writing every one of them back in restamped form (when the device has seen any of them). Mixing the
two left a chain whose link was written and whose target was not.

### 3.4 Recovery

```
open → RecoveryManager
  └─ read the control state: last committed position, checkpoint position
  └─ scan the log from the checkpoint
       ├─ a record whose checksum fails, or a torn tail  →  truncate at the last intact record,
       │                                                    quarantine the bytes, write a ledger entry
       └─ otherwise                                      →  redo, idempotently, through the same
                                                            doors the live path applies through
  └─ advance every index that saw a complete replay
  └─ publish
```

The redo goes through **C1's apply door**, the same one a live commit uses — not a second
implementation. The rule (grow the file if the page is missing, apply when the resident page is free
or older, leave it alone when it is not) is written once, so the live path and the recovery path
cannot disagree about one invariant.

---

## 4. On-disk formats

```
mydb/
  identity.dat     one page: what this database is. A mismatched open is refused, not adapted.
  catalog.dat      the schema: tables, columns, primary keys, vector spaces
  heap.dat         page 0 is the table directory; every other page is a slotted data page
  index/           one file per secondary index (pk_<Table>.idx, vector_<Table>_<space>.idx)
  wal/             000000000001.wal, … — segments with an LSN mark index
  control/         lease, commit state, reader registrations — published atomically
  ledger/          forensic entries: what was found, where, and what was done
  quarantine/      the damaged bytes themselves, preserved
```

### Pages

Every page is `page_size` bytes (8192 by default, fixed for the life of the database) and carries:

- a **CRC-32C** over the rest of the page, in the first four bytes;
- a **sequence counter** that is always *even* in a durable image. A reader that meets an odd
  counter, or a checksum that does not match, is looking at a write that did not complete: it reads
  again, up to a bounded number of times, and never sleeps inside the engine. That is the torn-read
  protocol of `CONTRACT.md` §6.3.
- a **page type** (`FREE`, `META`, `HEAP`) and, for a heap page, a `next_page` link and a slot
  directory.
- **Page 0 of every paged file is a reserved header** (amendment A2). It is written when the file is
  created, never from the log, and it is the one page no replay repairs.

An **all-zero page is not damage.** It is what an ordinary crash between allocating a page and
writing it leaves, and `is_unwritten_image` proves that state disjoint from every written page: for
the first four bytes to be zero, the rest would have to checksum to zero while also being zero,
which no page size in the accepted range permits. So it is reported as `page_unwritten`, not as
corruption, and no checksum counter moves — because no checksum was verified.

### Heap records

A table's rows live in a chain of slotted pages, starting at the `first_page` its directory entry
names and following `next_page` to the end. Slot 0 of every data page is a descriptor naming the
table it belongs to, so a page that answers for the wrong table is caught rather than producing a
wrong answer.

Each version carries an `xmin` (the commit number that made it visible) and an `xmax` (the commit
number that ended it). A snapshot sees a version when `xmin ≤ snapshot < xmax`. A version with no
commit number is refused outright by the visibility predicate — which is what makes an abandoned
write invisible whatever happens to the page afterwards.

### The log

Segmented, with a mark index from LSN to offset so a replay does not scan from the beginning.
Records are self-describing and carry their own CRC. Segments are recycled only when they are both
below the checkpoint **and** below the oldest live reader's position, which is why a long-running
read holds the log down and why a stalled reader is eventually released.

Record types: `WRITE_PAGE` (a whole page image), `INDEX_WRITE`, `INDEX_RECONCILE`, `PAGE_ALLOC`,
`COMMIT` (carrying the snapshot position and the read and write partition sets that OCC validates
against).

### Checkpoint data barriers

A normal checkpoint with no transaction open in that participant is split into three phases so a
slow data-file durability barrier does not hold the cross-process writer fence:

1. **A, fenced:** take the writer lease, `COMMIT_SECTION` and one WAL-tail picture; complete any
   durable gap, redo and flush; freeze an immutable target and exact paged-file inventory.
2. **B, unfenced:** release both lease and commit section, then run only the data-file durability
   barriers captured by A. This phase never publishes state or recycles WAL.
3. **C, fenced:** reacquire and revalidate all authority and current state, redo any suffix that
   arrived during B, publish the maximum of the already-published checkpoint and A's barriered
   target, advance the participant pin and only then recycle.

A failure in B publishes nothing; a regression observed in C is refused. Commits completed during
B may be replayed for visibility but are not certified by A's barriers. Index rebuild claim/clear
and any checkpoint while the participant owns an open transaction retain the monolithic path.

---

## 5. Concurrency

### Between processes

The coordinator publishes three things on the filesystem under `<db>/control`:

- **The lease** — who may write, with an **epoch**. Every byte a writer sends to the device is
  authorised by an epoch that was validated inside the commit section. A writer that was paused past
  its TTL and wakes up finds its epoch stale and is refused: `GrafxStaleEpoch`.
- **The commit state** — the last committed position, the last commit number, the checkpoint
  position. It is the shared signal every participant watches; it moves when *anyone* commits, which
  is what lets a pool know its cache may be behind.
- **Reader registrations** — each holding the recycle horizon down at the position it opened.

Takeover is explicit: a coordinator that finds a lease whose owner is not live reports a
`DeadOwnerReport`, takes the lease with a **new epoch**, and the dead owner's epoch is thereafter
refused. Liveness is measured on a **monotonic** clock, so a wall-clock adjustment cannot make a live
writer look dead.

### Between threads of one process

- **The participant section** is re-entrant and process-wide: the threads of one participant take
  turns at the acquire-commit-release window rather than ending one another's epoch.
- **The buffer pool runs every door under an injected guard.** The pool is process-wide state that
  several threads reach at once — a commit applying pages inside the section, and searches and scans
  pinning pages outside any section. Its doors were sequences of dictionary steps that were
  individually atomic and jointly not. The guard is *injected* by the composition root, because the
  pure core imports no mechanism; the default is a no-op context, and the body of `pinned()` runs
  outside the guard so no caller ever holds the pool's lock while working with a page.

### Isolation

Snapshot isolation with optimistic validation. A reader is entitled to a stable view for its own
lifetime; a *new* read view in the same participant must see everything committed since that is at
or below its snapshot.

---

## 6. Indexes

Two contracts, and `CONTRACT.md` §8.7 keeps them apart deliberately.

**EXACT** (`HashIndex`). A hit is a **candidate**. `IndexManager.lookup` validates every one against
the heap under the caller's snapshot: is this version visible, does the row still carry the key it
was filed under, does the location belong to this table? A candidate that fails any of them is
dropped silently, because being a superset is the *contract* rather than a defect. This is what lets
a primary key be indexed without the index having to understand visibility.

**PROXIMITY** (vector). Entries are versioned with tombstones and a horizon, and they are already the
answer — the heap is deliberately not consulted.

Vector indexes are **sparse for nullable embeddings**. A row whose vector is `NULL` remains a normal
heap row but has no vector entry, cannot enter either search regime and is omitted by rebuild and
coverage verification. A `NULL`-only write has no index WAL payload, yet its empty staged
observation advances the vector index through that commit; otherwise the next read would correctly
classify the index as behind. Nullable scalar exact indexes retain their ordinary encoded `NULL`
key, so sparsity is a definition-level policy rather than a global null rule.

**Staleness.** An index compares the position it claims to cover against the position the database
published. A stale index is a *subset* of the heap, and a subset is exactly what validation cannot
repair: it removes hits that should not be there and cannot invent ones that are missing. So a stale
index is withheld from the planner *and* from the uniqueness check, and both fall back to the scan
they did before any index existed. It is reported through `Database.stale_indexes` and cleared only
by a rebuild.

---

## 7. Query pipeline

```
text ──parse──► Statement ──analyze──► Analysis ──plan──► PlanNode tree ──execute──► QueryResult
```

The planner produces **one** operator tree per statement, and its shape does not depend on the
snapshot — so `explain` describes the query that actually runs. Everything that does depend on the
view (which versions are visible, how large the filtered set turns out to be) is decided while the
plan runs, by the components that own those questions.

Operators: `SingleRow`, `NodeScan`, `IndexSeek`, `TraverseRelationship`, `Filter`, `Project`,
`Aggregate`, `Distinct`, `Sort`, `Skip`, `Limit`, and the write operators.

The planner chooses `IndexSeek` when an index's **whole key** is constrained by equality — the whole
key and nothing less, because a hash index stores the encoding of all its key columns as one key, so
a seek that constrained only some of them would ask for a key that was never written.

---

## 8. Verification

`verify(scope)` walks the database and returns findings with a location precise enough to act on.
Scopes: `pages`, `records`, `indexes`, `all`.

The report carries **counts as well as findings**, and they are load-bearing rather than decorative:
"no findings" and "nothing was checked" are the same empty tuple. A clean database returns an empty
`findings` **and** non-zero counts, so a caller can tell a verification that passed from one that
never ran.

One page image is one finding however many walks read it, and the page walk asks whether a page is
unwritten *before* it asks the codec to decode it — because handing all-zero bytes to the codec
produces `corruption_detected` for a page that is intact, which is precisely the false integrity
incident the design exists to prevent.

---

## 9. Testing

| | |
|---|---|
| Suite | 7900+ tests, 0 failures, 5 attributed platform skips |
| Import boundary | A test walks the graph; the engine importing a mechanism fails the build |
| Fault injection | A device that refuses the *n*-th write, reports full, or fails a barrier |
| Multi-process | Real OS processes against one database, spawned with `spawn` |
| Smoke | `tests/smoke/` drives the **public door only**, is slow on purpose, and found a blocking defect a green 7858-test suite did not |
| Mutation | Batteries with per-mutant verdicts; survivors are recorded with why, not hidden |
| Determinism | Repeated runs must produce identical test-id sets |
| Platform parity | A test that exercises one OS family must declare its counterpart; a skip must be attributed |

The rule the suite is built around, learned the hard way and recorded in
`docs/architecture/LESSONS.md`: **the regime that breaks is the one nothing runs.** Every concurrency
test in this repository once did one of three things — drove the engine below the public door, read
with a fresh short-lived process, or contended hard enough that writers serialised — and the defect
that made a heap unreadable needed all three to be false at once.

---

## 10. Where to go next

- [PORTS.md](PORTS.md) — every port, its protocol, its default adapter, how to write your own.
- `docs/architecture/CONTRACT.md` — the normative, frozen substrate.
- `docs/architecture/COMPONENTS.md` — the register, the sign-offs, every carried finding with its
  measurement.
- `docs/architecture/LESSONS.md` — what went wrong here and what it taught.
- `docs/architecture/PUNCHLIST.md` — the known gaps.
