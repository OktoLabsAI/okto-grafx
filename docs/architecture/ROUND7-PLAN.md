# Round-7 execution plan — four work items, in priority order

Status: **authorized by JP 2026-08-23**, written for an implementer with a fresh context. Every
claim below marked *(verified)* was read or measured in the round-6 session at `092a60f`; anchors
are given as UNIQUE STRINGS, not line numbers, because lines drift. Read first:
`docs/architecture/CONTRACT.md` §8.5/§8.7/§14 (frozen), `docs/architecture/LESSONS.md` L7, L12,
L24, L26, L28–L32, `docs/architecture/W6-WRITE-CEILING.md` (the option-1 decision record), and the
PUNCHLIST entries named per item.

The items, in the order they must land (each its own commit, suite-green before each):

1. **HNSW partial-graph race** — wrong results through a public door. Fix now.
2. **WAL truncation race** — a second `connect()` truncates another process's in-flight append.
3. **Dead configuration knobs** — `checkpoint_interval_records`, `vector_recall_target`.
4. **Option 1: identity-range leasing** — the authorized §8.5-adjacent amendment (W6 record).

A blind-critic round reviews the whole series at the end (§9). History says plan for it to find
exactly one unheld guard: six prior rounds each found one, always in the regime the author's tests
did not run.

---

## 0. Execution protocol (read before touching anything)

**Environment quirks, all hit repeatedly in round 6:**
- Foreground bash calls are capped at 600 s. The full suite (~7,950 tests) takes ~9–11 min idle —
  run it in TWO halves, both must be green:
  `python -m pytest --ignore=tests/smoke --ignore=tests/txn --ignore=tests/storage_core --ignore=tests/storage_adapters -q -p no:randomly`
  then `python -m pytest tests/smoke tests/txn tests/storage_core tests/storage_adapters -q -p no:randomly`.
- Background jobs get killed by a supervisor after ~10–25 min. For long batteries use the
  resumable pattern: a state file listing completed units, one unit per foreground call
  (`FLEET_UNITS` env), and a **source-integrity gate** that sha-checks every guarded file against
  `git show <commit>:<path>` before any mutant and restores what a kill left mutated (L7/L26 —
  a mutant that did not run is not a kill; a poisoned fork voids every later verdict).
- Bash heredocs containing Python triple-quoted strings break unpredictably on this box. Write
  patch scripts with the Write tool into the scratchpad and run `python script.py`. Patch scripts
  use an `edit(path, pairs)` helper that asserts `s.count(old) == 1` per anchor and writes the
  file once after all pairs — a mid-list assertion failure must not leave half a file patched.
- Worktree files are LF; `git` prints CRLF warnings on commit — ignore them.

**Codebase landmines, all real:**
- `__slots__` everywhere: `QueryEngine`, `TransactionManager`, `BufferPool`, `Database`,
  `VectorHnswIndex`, `IndexStore`, `TransactionContext`. A new attribute needs its slot added or
  assembly fails with "no __dict__".
- The vector engine's public surface is ENUMERATED by
  `tests/vector/test_space_lifecycle.py::test_the_engine_exposes_nothing_that_could_generate_an_embedding`
  — any new public method must be added to that list with a BR-4 justification comment.
- `Database.metrics` is a `ContainedMetricsSink` wrapper; the raw sink is `.inner`. Lifecycle
  doors (publisher, close-publish) use `.inner`; recording goes through the wrapper.
- The storage device REFUSES case-colliding file names with `GrafxUnsupportedOperation` (even from
  `exists()`); index-attach decline guards therefore catch
  `(GrafxIndexError, GrafxUnsupportedOperation)`.
- The query harness double (`tests/query/stack.py::TransactionDouble`) must mirror the real
  context's doors (L12): it has `page_images` dict + `staged_pages()` method + `txn_id` (always 1)
  and NO `mode` attribute — mode guards must treat an absent `mode` as permitted.
- **Never `git stash push` with an untracked file in the pathspec** — git refuses, creates no
  stash, and a following `git checkout <rev> -- files` then DESTROYS the working tree (this
  happened in round 6; recovery cost an hour). To run tests against a pre-fix tree, either use a
  scratchpad fork from `git archive`, or copy-restore single files via `git show rev:path > file`
  with sha verification.

**Per-fix discipline (non-negotiable, §14):**
1. Reproduce the defect first (scripts inline below).
2. Fix; write the regression; **prove the regression fails at the pre-fix tree** (archive fork).
3. Kill-check every new guard with a targeted mutant (revert the load-bearing line, watch the
   named test fail, restore, verify `git status` clean).
4. Update docs in the same commit: `COMPONENTS.md` (a CF-19/20/21/22 entry per item: defect →
   cause → fix → tests → measurements), `PUNCHLIST.md` (close/annotate entries), `CHANGELOG.md`,
   `PERFORMANCE.md` where numbers change.
5. Commit message: imperative subject; body = defect, cause, fix, tests, numbers; end with the
   configured `Co-Authored-By` + `Claude-Session` lines.

---

## 1. HNSW partial-graph race (wrong results; fix first)

> **Delivered 2026-08-24 (W0-A, branch `w0a/p0.5-hnsw-snapshot`), with two changes to the design
> below, both from Codex's review of this plan and both accepted before code:** (1) the four
> separate publications (three maps, mark, graph) are replaced by ONE `_GraphSnapshot` (its maps edited in place by the warm path)
> published by a single assignment, because four assignments are four interleaving windows and a
> failing builder must not `invalidate_graph()` another builder's success; (2) the mark is the
> header reading taken BEFORE the walk, so a commit landing during the build is never certified
> into a picture that lacks it -- and, after the cross-review, is caught up before publication. The WARM-half probe below found a second defect -- `commit()`
> certified a graph another process had left behind -- fixed in the same series. Record:
> COMPONENTS.md "C9 round 7"; LESSONS L33; tests `tests/vector/test_first_use_concurrency.py`
> and `tests/vector/test_warm_graph_across_processes.py`.

### Verified facts

`src/okto_grafx/engine/vector_engine.py`, class `VectorHnswIndex`:
- Module header states *"This engine holds no lock"* — there is NO thread guard anywhere in the
  vector path *(verified)*. The buffer pool got its injected guard in CF-13; this engine did not.
- `graph()` — anchor `def graph(self) -> HnswGraph:` — publishes the graph BEFORE building it:
  ```
  if self._graph is not None:
      return self._graph
  graph = HnswGraph(...)
  self._graph = graph            # <-- published empty, deliberately
  self._node_of_ref = {}
  ...
  for entry in sorted(self.walk(), ...):
      self._install(entry)       # _install reaches the graph through self._graph
  ```
  The in-code comment explains the early publication: `_install` reads `self._graph`. The
  round-2 C9 fix handled the FAILURE-mid-build case (the `except BaseException: invalidate` —
  keep it); the CONCURRENT-READER case is the same shape one seat over: thread B calls a search,
  `graph()` takes the `is not None` fast path, and answers out of a fragment. **Codex reproduced:
  two simultaneous first-use searches, one returned 5 neighbours, the other 1, no error, 8 valid
  vectors, final graph complete.**
- The maps `_node_of_ref`, `_entry_of_node`, `_record_of_node` are also reset before the loop and
  filled during it — a concurrent search reads those too.
- `_install(entry)` → `_install_fresh(graph, entry, encoded)`: `_install_fresh` already takes the
  graph as a PARAMETER, but registers entries/records into the `self.` maps *(verify its body
  before refactoring)*.

### Reproduction (deterministic, single process)

Two threads; the build is slowed by wrapping `index.walk()` so each install yields the GIL:

```python
# scratchpad/repro_hnsw.py -- expect "PARTIAL ANSWER OBSERVED" on the pre-fix tree
import shutil, sys, tempfile, threading, time
sys.path.insert(0, r"D:\Projetos\Techridy\okto_grafx\src")
from okto_grafx import connect

root = tempfile.mkdtemp(); db = connect(root)
with db.begin("write") as t:
    t.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
    t.execute("CREATE NODE TABLE V(id INT64, e VECTOR(s), PRIMARY KEY(id))")
with db.begin("write") as t:
    for i in range(1, 9):
        t.execute("CREATE (:V {id: $i, e: [$a, $b, 0.0, 0.0]})",
                  {"i": i, "a": 1.0 - i * 0.05, "b": i * 0.05})

index = db.vectors.index("s")
original_walk = index.walk
class Dribbler:
    def __init__(self, entries): self._entries = list(entries)
    def __iter__(self):
        for e in self._entries:
            time.sleep(0.02); yield e
index.walk = lambda: Dribbler(original_walk())

results = {}
def probe(name, delay):
    time.sleep(delay)
    r = db.begin("read")
    try:
        hits = db.vectors.search(space="s", k=5, query=[1.0, 0.0, 0.0, 0.0],
                                 snapshot=r.context.snapshot)
        results[name] = hits.achieved_k
    finally:
        r.rollback()
a = threading.Thread(target=probe, args=("A", 0.0))
b = threading.Thread(target=probe, args=("B", 0.06))
a.start(); b.start(); a.join(); b.join()
print(results, "PARTIAL" if min(results.values()) < 5 else "complete")
db.close(); shutil.rmtree(root, ignore_errors=True)
```

### Fix design — build privately, publish atomically

1. Refactor the install path to explicit parameters. New private signatures:
   `_install_into(graph, node_of_ref, entry_of_node, record_of_node, entry)` and thread the maps
   through `_install_fresh` likewise (it must not touch `self.` maps). Keep the
   discard-on-any-refusal semantics for the WARM path (`_install` stays as the warm wrapper that
   passes the LIVE maps and keeps its `invalidate_graph()` on failure — that behavior is pinned
   by C9 round-2 tests).
2. `graph()` builds into LOCALS: local graph + three local dicts; the loop installs into them; on
   success, publish in this order — maps first, `self._graph_mark = self.built_through_lsn`, and
   `self._graph = graph` **LAST** (readers gate on `_graph is not None`; CPython's GIL makes the
   preceding assignments visible once the gate opens). On failure, publish NOTHING (the except
   may keep `invalidate_graph()` for idempotence; there is no previous state to preserve because
   the cold path only runs when `_graph is None`).
3. Two concurrent cold builders may now both build and both publish complete states — wasted work,
   correct answers, no lock across the `VectorMath` PORT (A91: math is host-suppliable through
   the registry; a lock held across it is the violation the pool guard's design explicitly
   avoids). Say this in the docstring.
4. `search()` must capture the graph reference ONCE (`graph = self.graph()`) and traverse the
   captured object — verify it already does; if it re-reads `self._graph` mid-flight, fix that
   too.

### The WARM half — probe, decide, do not improvise

`_note()` (anchor `def _note(self, change: IndexChange)`) inserts into the LIVE published graph on
every commit apply, while searches traverse it with no guard. Same race class, unproven. Protocol:
1. Write a 30 s hammer probe: one thread committing single-row vector inserts in a loop, three
   threads searching k=5 continuously, assert every search returns `min(k, live_count_at_start)`
   neighbours. Run 5×.
2. **If it reproduces**: STOP. Do not redesign HNSW concurrency inside this item. Write the
   design record first (options: per-index injected guard with math calls hoisted OUTSIDE it —
   hard, `HnswGraph.insert` interleaves math with linking; clone-and-swap per commit — O(n) per
   commit; invalidate-on-insert — O(n) rebuild on next search). Record as its own defect with the
   repro, ship the COLD fix alone, and take the warm design to JP with the trade table.
3. **If it does not reproduce** (5×): record in PUNCHLIST as a suspected race with the probe
   script and the reasoning (searches capture the object; `_note` runs inside the commit
   sections; the residual hazard is mutation-during-traversal of shared adjacency).

### Regression tests (new file `tests/vector/test_first_use_concurrency.py`)

- `test_two_first_use_searches_both_answer_from_a_complete_graph` — the repro above as a test,
  with the Dribbler and a `threading.Barrier` for determinism; assert both `achieved_k == 5`.
  **Must fail at the pre-fix tree** (verify via archive fork).
- `test_a_search_never_observes_the_graph_before_its_publication` — monkeypatch `HnswGraph`
  construction? Simpler: assert via the Dribbler that during a slowed build a second search either
  waits-by-building-its-own or answers complete — the first test covers it; add instead a
  mutant kill-check: revert "publish last" (re-order to publish-first) and watch the first test
  fail.
- Keep/extend the existing C9 pinned tests green (the failure-path invalidate).

Battery: mutants = publish-first reorder; maps published after graph; `_graph_mark` not set
(freshness check must then rebuild — is that observable? if not, punch-list it).

Docs: COMPONENTS entry CF-19 (quote the Codex observation as the finder), CHANGELOG "Fixed".

---

## 2. WAL truncation race — recovery vs a live writer's in-flight append

### Verified facts and what to verify

- The COMMIT appends and barriers INSIDE the exclusive cross-process section: anchor in
  `src/okto_grafx/engine/txn_manager.py` — `with self._coordinator.exclusive(  COMMIT_SECTION,`
  wraps steps 3.1–3.7 including `append_many` + `barrier()` *(verified in round 6)*.
- Codex reproduced: a second `connect()` while another process is mid-append reads a torn tail,
  treats it as crash damage, and TRUNCATES — taking down the healthy writer. No false durability
  was observed (the un-barriered suffix was never acknowledged), so this is an availability
  defect under D1, not data loss — but the writer dies through no fault of its own.
- **To verify before fixing** (the implementer must read these):
  - Where recovery runs in `src/okto_grafx/api/assembly.py` (grep `RecoveryManager` /
    `recover`): confirm it runs at open, and whether it holds ANY coordinator section (expected
    answer: none — that is the defect).
  - The truncation site in `src/okto_grafx/engine/recovery_manager.py` (grep `truncate`): what it
    scans, what it quarantines, and its decision inputs.
  - The `COMMIT_SECTION` constant's home (txn_manager) — recovery must take the SAME section
    name, or there is no mutual exclusion with the in-flight append. If importing it from
    txn_manager into recovery/assembly creates a cycle, move the constant to a shared home
    (`engine/coordination.py` or `domain/txn/`) and import it in both.

### Fix design

The invariant to restore: **a tail can only be judged torn when no commit can be mid-append** —
and since every append+barrier happens inside `COMMIT_SECTION`, the whole recovery
scan-and-truncate must run inside that same section.

1. In assembly (or wherever recovery is invoked at open): wrap the recovery pass in
   `coordinator.exclusive(COMMIT_SECTION, timeout=config.commit_lock_timeout_seconds)`. The
   coordinator exists before recovery runs in the assembly order *(verify)*.
2. Semantics that fall out, and must be tested:
   - A live writer mid-commit: the opener WAITS (bounded by `commit_lock_timeout_seconds`); when
     it acquires, the append either completed (barrier done → tail intact → ordinary replay) or
     the writer died inside the section (dead-owner takeover path — the coordinator already has
     `detect_dead_owner`/`takeover`; verify the exclusive() door handles a dead holder, which the
     coordination tests exercise).
   - Timeout expiry: a TYPED refusal from `connect()` (whatever `exclusive` raises —
     lease/section timeout is retryable), never a truncation.
   - A genuinely crashed writer (no live holder): behavior unchanged — truncate at the last
     intact record, quarantine, ledger. The existing recovery tests pin this; they must stay
     green untouched.
3. Do NOT add liveness heuristics beyond the section (no "check the lease then decide" — the
   section IS the mutual exclusion; a second mechanism answering the same question is A67).

### Reproduction / regression (multiprocess, deterministic)

Use the existing helpers in `tests/coordination/coordination_child.py` (`hold_section`,
`build_coordinator`, marker-file handshakes — read that module first; it exists precisely for
this shape). New file `tests/recovery/test_recovery_respects_the_commit_section.py`:

- `test_a_second_open_waits_for_an_in_flight_commit_instead_of_truncating`: child process
  acquires `COMMIT_SECTION` via the helper and parks on a marker file; parent, after writing a
  half-record... simpler and honest: child = REAL writer that begins a commit and parks INSIDE
  the section via a fault/hook? Simplest deterministic shape: child holds the section (helper),
  parent appends nothing but calls `connect()` on a database whose WAL carries a deliberately
  torn tail (write garbage bytes to the segment file directly BEFORE the child takes the
  section? No — then truncation is CORRECT). The faithful shape: parent writes a valid database;
  child process opens it, starts a commit, and is PAUSED mid-append. Achieve the pause with the
  fault-injecting storage? The child can use `FaultInjectingStorageDevice` wrapping local
  storage with a "block on Nth log append until marker file appears" hook — check
  `adapters/storage_fault.py` for a blocking/latency facility; if none, add a
  tiny `_HoldingDevice` in the test that wraps append_log to wait on a marker file. While the
  child is parked mid-append INSIDE the section, the parent calls `connect(root)`:
  * pre-fix: parent truncates the suffix; child's barrier then fails / next validate dies →
    the test observes the child reporting failure (this is the shape that must FAIL post-fix);
  * post-fix: parent blocks on the section, child is released (marker), child's commit
    completes, parent's recovery sees an intact tail, both processes read the committed row,
    `verify()` clean.
  Assert: child exit 0, its row visible to the parent, no ledger/quarantine entries created.
- `test_a_dead_writer_mid_append_is_still_truncated`: child killed with `os._exit` while parked
  mid-append (marker never written) → parent `connect()` acquires (dead-owner path), truncates,
  ledger records it, database opens clean, the un-acknowledged suffix is gone. This pins that
  the fix did not narrow the crash path (L1: nothing that refuses may be quietly weakened).
- `test_open_times_out_typed_when_the_section_never_frees`: child holds the section past
  `commit_lock_timeout_seconds=1`; parent's `connect()` raises the typed retryable refusal, and a
  retry after release succeeds.

Mark all three `@pytest.mark.multiprocess` (+ timeout margins per the house pattern).

Docs: CF-20; PUNCHLIST if any residue (e.g., the timeout default for `connect()` under a stuck
writer); CHANGELOG.

---

## 3. The two dead knobs (public-contract lies)

Both are in `DatabaseConfig` (anchor block `path: str` → `read_only: bool = False` in
`src/okto_grafx/runtime/config.py`), both validated, both documented in README §Configuration,
neither consulted anywhere *(Codex, static — re-verify with grep before wiring:
`grep -rn checkpoint_interval_records src/` shows config/validation only)*.

### 3a. `checkpoint_interval_records` — auto-checkpoint

Design: after a successful WRITE commit, if `last_committed_lsn - checkpoint_lsn >=
checkpoint_interval_records`, run `checkpoint()` once. Wiring:
- Placement: `engine/database.py`, `Transaction.commit()` — AFTER the commit report and
  `_settle_schema`, call `self._database._maybe_checkpoint()`. Never inside the engine's commit
  (the section is held there; `checkpoint()` takes its own lease + section).
- `Database._maybe_checkpoint()`: read the published state (the txn manager exposes
  `published_state()`/`published_lsn()` — grep; checkpoint_lsn is in `CommitState`), compare the
  delta to the interval (`Database` must receive the interval — check whether it holds config;
  if not, thread `checkpoint_interval_records: int` through the constructor from assembly, slot
  included). On threshold: `try: self.checkpoint() except GrafxError: <emit event, continue>` —
  maintenance must never fail the commit that already returned durable; a refusal (lease
  contention) is retried by the next commit's check. Guard against reentry
  (`self._checkpointing` bool slot) since checkpoint itself commits nothing but shares doors.
- Interval semantics documented: "at least every N appended records, measured on the published
  log positions, checked after each committing write transaction".

Tests (`tests/api/test_auto_checkpoint.py`):
- `test_commits_past_the_interval_trigger_a_checkpoint_without_being_asked` — interval=8 via
  `connect(..., checkpoint_interval_records=8)`; commit ~12 single-row txns; assert the published
  `checkpoint_lsn` advanced and (stronger, device-visible) WAL segments were recycled vs a
  control database with a huge interval. Use a small `wal_segment_bytes` so recycling is
  observable.
- `test_commits_below_the_interval_do_not_checkpoint` — the control half.
- `test_a_refused_auto_checkpoint_does_not_fail_the_commit` — wrap storage with the fault device
  refusing the checkpoint's barrier; the commit still returns durable; the next commit retries.
- Kill-check: neuter `_maybe_checkpoint` body → first test fails.

### 3b. `vector_recall_target` — wire it to what the spec says, not to an invention

Protocol (the semantics must come from the spec, per L31-style honesty):
1. `grep -rn -i "recall" docs/specs/ docs/architecture/CONTRACT.md src/okto_grafx/engine/vector_engine.py bench/`
   and read SPEC-VEC's clauses (FR-7/VTS — the round-6 session recalls `vector_recall_target:
   float = 0.90` beside `vector_exact_scan_threshold: int = 4096`, and a C13 note that the bench
   gate's `require_recall` is "not passed in the workflow").
2. Two legitimate outcomes; implement whichever the spec supports, and say which in the CF entry:
   - **Runtime semantics** (if SPEC-VEC binds the approximate regime to a recall target): pass
     the value into `VectorEngine` (assembly → ctor → slot) and use it where the two-regime
     search chooses parameters — the defensible mechanical wiring is
     `effective_ef_search = max(self._ef_search, ceil(k * calibration(target)))` ONLY if a
     calibration mapping already exists in `bench/` (grep `recall` there); if no principled
     mapping exists in-tree, do NOT invent one — fall through to:
   - **Calibration-gate semantics**: the knob is the target the calibration harness verifies
     (`bench/harness/gate` has a recall input; the C13 CI job runs it). Wire the gate's
     `require_recall` default from the config value, close the C13 observation, and update
     README's table to say the knob "sets the recall target the calibration gate enforces; it
     does not reshape an individual query". A documented knob that does exactly what its docs
     say is the fix; a knob secretly steering ef_search by a made-up formula is a new lie.
3. Either way: a test that fails when the knob is disconnected again (gate: pass a config with
   target 0.99 against a calibration report below it → gate exit nonzero; runtime: ef grows).

Docs: CF-21 covering both knobs; README §Configuration rows updated to describe the REAL
behavior; CHANGELOG "Fixed: two accepted-but-inert configuration options".

---

## 4. Option 1 — identity-range leasing per participant (authorized amendment)

Read `docs/architecture/W6-WRITE-CEILING.md` first; it is the decision record. Baselines to beat,
measured (PERFORMANCE.md §2): disjoint 4-writer median 313 ms / p90 3.45 s / 254 conflicts /
10.9 rows/s; single-writer unit cost 95–112 ms; instrument `tools/measure_concurrency.py`.

### Verified facts

- Directory entry layout *(verified)*: anchor
  `table_id, first_page, last_page, page_count, next_record_id = _DIRECTORY_ENTRY.unpack(raw)`
  in `src/okto_grafx/engine/heap_store.py` — the counter LIVES on heap page 0 per table.
- Identity allocation *(verified)*: anchor `identity = extent.next_record_id` inside the
  "record identity" section of heap_store; its docstring states the safety rule: *"a counter
  behind an id in use would hand that identity out"* twice — the invariant the design must keep
  is **durable `next_record_id` > every id any physical record header carries**. Equality is
  already behind because the counter itself is what the next insert takes.
- Gaps are sanctioned *(verified, txn_manager docstring)*: *"an id burned by a commit that then
  fails leaves a GAP in the sequence, which no reader can observe."*
- Ids are allocated inside `_write_rows`, which runs INSIDE the commit section; the participant
  section serializes the threads of one process — so per-process allocation is single-flight
  *(verified)*. Two PROCESSES renewing concurrently produce two conflicting page-0 images and
  OCC refuses one — that is the mechanism that keeps leases disjoint, and it must remain: **a
  lease renewal MUST keep page 0 in its write set** (it does naturally: the renewal writes the
  page, `_attempt_pages()` measures it, `_declare_page_interest` declares it).
- The verifier checks the counter from an independent device scan: page 0 and every physically
  decodable heap header are read without the pool, catalog chain or visibility filter. Ended,
  provisional, no-CSN and orphaned headers constrain it. Leasing must keep the counter strictly
  ahead, and the kill/abort tests below must prove it.

### Design

Add to `HeapStore` (slots!): `_identity_leases: dict[int, _IdentityLease]` where

```python
@dataclass(slots=True)
class _IdentityLease:
    next: RecordId          # the next id to hand out
    durable_end: RecordId   # ids below this are covered by a COMMITTED counter
    speculative_end: RecordId  # ids below this are covered by a counter write staged in the
                               # CURRENT commit attempt; equals durable_end between attempts
```

Constant `IDENTITY_LEASE_BLOCK: int = 1024` with a docstring: why 1024 (a u64 id space makes the
burn irrelevant; one renewal per ~1024 rows makes page-0 writes ~0.1% of insert commits; N=1
restores today's behavior and is the fallback if a defect appears).

`allocate_identity(table)` (rename/extend the existing allocation site):
1. If the lease for `table_id` has `next < speculative_end`: hand out `next`, bump. **No page
   write.**
2. Else: renewal — read the entry (as today), write the entry with
   `next_record_id += IDENTITY_LEASE_BLOCK` through the pool (dirty page, exactly like today's
   single-step advance: the commit's measured page set picks it up, logs it, declares it), set
   `next = old_counter`, `speculative_end = old_counter + BLOCK`, leave `durable_end` unchanged
   (it becomes durable only when THIS commit commits).

**The abort rule — this is the correctness heart, get it exactly right:** the renewal's page-0
image and provisional row headers are part of the same commit attempt, but eviction can put either
on the device before the WAL accepts the batch. Therefore rollback depends on the proof already
made by `_undo_pages`: whether any attempt page escaped. Hook the same seams the round-6 undo uses:
- On commit SUCCESS (after step 3.7 publishes): promote `durable_end = speculative_end` for every
  table this commit renewed. Track renewals per attempt in the txn context or a heap-side
  per-attempt list keyed like `_dirty_mark` is (set at the mark point, consumed at
  success/abandon).
- If NO attempt page escaped, `_undo_pages` discards every touched frame. That is proof that no
  physical header carries the attempt's ids, so the attempt-start `next` may be restored and a
  speculative renewal may fall back to `durable_end`.
- If ANY page escaped, `_undo_pages` writes every touched page back in its invisible form. The
  provisional/no-CSN headers remain physical, so every id handed to that attempt is BURNED even
  when it came from an already-covered range. Keep page 0 strictly above their maximum and keep
  `next` past them; for an escaped renewal, preserve/promote its end or invalidate the lease and
  reload the device counter. Reusing an invisible physical id would defeat the verifier invariant.
- If escape detection or cleanup fails, assume escape, burn conservatively and retain
  `recovery_required`; uncertainty never authorizes reuse.
- Kill between renewal-commit and use: reopen reads the durable counter (= extended) → gaps only.
- Kill between handing ids and commit: rows remain invisible, but their physical ids still count;
  page 0 must cover them after reopen. Invariant proof obligation for the critic: **no persisted
  header, visible or not, may carry an id ≥ the device-resident next_record_id**. The renewal image
  rides in the same attempt as the first row that needs it, and escaped-abort handling preserves
  that coverage.

Cross-process disjointness: renewals from two processes conflict on page 0 (both write the whole
entry) → OCC retries one → it re-reads the advanced counter → disjoint blocks. No new mechanism.

**Extent hints are OUT OF SCOPE this round** (they still put page 0 in ~1/9 of 5-row-commit write
sets via chain growth). Measure after; the W6 record says hint-offloading is the follow-up if
numbers demand.

### Tests (`tests/txn/test_identity_leasing.py`, plus multiprocess)

- `test_ids_are_unique_across_processes_under_concurrent_load` — 3 writer processes × 40 txns × 5
  rows on ONE table, then assert all ids distinct, verify clean, reopen identical (extend or
  reuse the `tests/smoke` child pattern).
- `test_a_renewal_rides_the_commit_that_needs_it` — WAL inspection: the commit that first
  allocates past the durable window carries a page-0 WRITE_PAGE image (decode as in
  `test_a_committed_schema_change_logs_the_catalog_header_page`).
- `test_an_unescaped_aborted_attempt_rolls_the_lease_back` — txn A consumes ids without any page
  reaching the device; conflict-abort it, then prove the attempt-start cursor is restored.
- `test_an_escaped_aborted_attempt_burns_every_physical_id` — force provisional headers and page 0
  onto the device before append refuses; txn B and a fresh process must allocate strictly after
  those headers, with no duplicate physical id and `verify("all")` clean.
- `test_a_kill_between_renewal_and_use_leaves_only_a_gap` — child renews (commit with one row),
  `os._exit(9)` before using the rest; parent reopens, inserts many rows, asserts uniqueness +
  verify clean + the gap exists (max id jumped).
- `test_the_verifier_still_holds_the_counter_invariant` — after all of the above, `verify("all")`
  findings == () and a hand-corrupted counter (write a LOWER counter into page 0 via the pool in
  a test double) is still REFUSED (L1: the guard must not be narrowed — find and keep the
  existing refusal test).
- A93 2×2 for the abort rule: (renewal, abort) × (renewal, kill).
- Kill-checks: `speculative_end` promotion removed → uniqueness test fails after abort+retry;
  unconditional lease reset on abandon → escaped-abort/verifier test fails; reset removed entirely
  → unescaped rollback test fails; BLOCK=0 → behaves as today (equivalence smoke).

### Measurement (the point of the whole item)

`python tools/measure_concurrency.py` before (record: the PERFORMANCE.md §2 table) and after, on
an idle machine. Success criteria from the W6 record: disjoint-phase conflicts → near zero
(chain-growth renewals only), p90 collapses toward the section's fair queue (~2–4× the ~100 ms
unit cost). Throughput will NOT move (~10.8 rows/s — the exclusive section; say so, do not let
the numbers be read as a regression). Update PERFORMANCE.md §2 with a before/after table and the
W6-WRITE-CEILING.md status line; the next lever (Windows publication fix) stays recorded.

Docs: CF-22 with the amendment note (identity density: ids may jump by up to BLOCK across
process lifetimes; gaps were always sanctioned, now they are routine); CHANGELOG; README only if
the id-density note belongs in "limitations" (it does: one line).

---

## 5. Sequencing, commits, and the closing critic round

1. Item 1 (HNSW cold) → suite in halves → commit `fix(vector): ...` → push.
2. Item 2 (WAL race) → suite → commit `fix(recovery): ...` → push.
3. Item 3 (knobs) → suite → commit `fix(config): ...` → push.
4. Item 4 (leasing) → suite + `tools/measure_concurrency.py` before/after → commit
   `feat(txn,heap): identity-range leasing ...` → push.
5. Blind-critic round over the whole series (one agent), on a fork from
   `git archive <final-commit>`, `.battery-root` stamped, main tree untouchable, §14 applied
   literally, instructed to run a mutation battery over every new guard and to assume there is
   exactly one unheld guard to find (the six-round base rate). Feed it the per-item "what to
   attack" lists: the publish-order in `graph()`; the section acquisition in recovery (and the
   dead-writer path NOT narrowed); `_maybe_checkpoint` reentry and refusal-swallowing; the lease
   abort rule (promote/rollback seams), the renewal-in-same-commit invariant, and cross-process
   disjointness under kill storms.
6. Fix what it finds (plan for one round), update COMPONENTS/PUNCHLIST, final push.

Item 4 may spill into its own session; land items 1–3 first regardless — they are user-facing
defects, and item 1 is wrong results.
