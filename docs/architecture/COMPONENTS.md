# Okto Grafx — Component map, build waves and the builder/critic protocol

Read `CONTRACT.md` first. This file says **who builds what, in what order, and how the review loop works**.

## Component register

| ID | Component | Owns (write only here) | Realizes |
|----|-----------|------------------------|----------|
| **C0** | Foundation | `domain/errors.py`, `domain/ids.py`, `domain/ports/**`, `runtime/**`, `pyproject.toml`, `src/okto_grafx/{__init__.py,errors.py,py.typed}`, `tests/conftest.py`, `tests/foundation/**`, `tests/test_import_boundary.py`, `tests/test_language_surface.py`, `tests/test_platform_parity.py` | TR-1, TR-2, TR-6, TR-9, BR-8, G1–G5 |
| **C1** | Storage core | `domain/model/**`, `domain/page/**`, `engine/buffer_pool.py`, `engine/heap_store.py`, `engine/catalog_store.py`, `adapters/codec_v1.py` | FR-13, BR-8, page/record formats |
| **C2** | Storage adapters | `adapters/storage_local.py`, `adapters/storage_memory.py`, `adapters/storage_fault.py` | FR-1, FR-5, FR-16, TR-3, AC-9, AC-10, AC-11 |
| **C3** | Process coordinator | `adapters/coordination_local.py`, `adapters/clock_system.py`, `engine/coordination.py` | FR-7, BR-7, AC-6, AC-7 |
| **C4** | WAL | `domain/wal/**`, `engine/wal_manager.py` | FR-5, FR-6, TR-4, BR-4, BR-10, AC-8, AC-9 |
| **C5** | Transaction manager | `domain/txn/**`, `engine/txn_manager.py` | FR-2, FR-3, FR-4, BR-6, BR-9, AC-1, AC-2, AC-3 |
| **C6** | Recovery · ledger · quarantine · verify | `domain/recovery/**`, `domain/ledger/**`, `domain/verify/**`, `engine/recovery_manager.py`, `engine/ledger_store.py`, `engine/quarantine.py`, `engine/verifier.py` | FR-8, FR-9, FR-10, FR-11, TR-5, BR-1, BR-2, BR-3, AC-4, AC-5, AC-12 |
| **C7** | Index framework | `domain/index/**`, `engine/index_manager.py` | FR-12, BR-11, SD-3 |
| **C8** | Observability | `adapters/metrics_*.py`, `adapters/events_logging.py`, `engine/metrics_catalog.py`, `dashboards/**` | FR-14, TR-7, BR-12, OR-1..OR-6 |
| **C9** | Vector subsystem | `domain/vector/**`, `adapters/vectormath_pure.py`, `adapters/vectormath_numpy.py`, `engine/vector_engine.py` | VEC FR-1..FR-3, FR-5..FR-7, FR-9, VEC BR-1..BR-5, BR-7 |
| **C10** | Query engine | `domain/query/**`, `engine/query_engine.py` | D3, VEC FR-4, VEC BR-6, AC-7 |
| **C11** | Public API | `api/**`, `engine/database.py` (may EXTEND `src/okto_grafx/__init__.py` re-exports, which C0 seeded) | FR-1, FR-11, public surface |
| **C12** | CLI | `cli/**` | operator surface |
| **C13** | Bench · calibration · CI | `bench/**`, `.github/workflows/**`, `tests/bench/**` | FR-15, FR-16, TR-8, VEC FR-8, AC-14, VTS-9, VTS-10 |

## Build waves

```
W0  C0                       (blocking — everything depends on the frozen ports)
W1  C1 ‖ C2 ‖ C3 ‖ C8
W2  C4 ‖ C5
W3  C6 ‖ C7 ‖ C9
W4  C10 ‖ C11
W5  C12 ‖ C13
W6  integration hardening: full suite, cross-platform, spec-scenario coverage matrix
```
A wave starts only after every component of the previous wave has a **critic sign-off**.

## Builder/critic protocol

**Builder**
1. Read `docs/architecture/CONTRACT.md`, `docs/specs/SPEC-M1.md`, `docs/specs/SPEC-VEC.md`, and the
   already-delivered code it depends on.
2. Write production code **only inside its owned paths**, plus `tests/<component>/`.
3. Run `python -m pytest tests -q` — the **whole** suite, not just its own — and leave it green.
4. Report: files written, public symbols added, spec IDs covered, test counts, and any contract
   conflict it hit (never silently deviate).

**Critic (blind)**
1. Receives ONLY: the component's scope, the contract, the specs, and the file paths.
   It does **not** receive the builder's self-report, rationale, or claims of success.
2. Reads the actual code, runs the tests itself, and tries to break the implementation
   (adversarial probes, edge cases, concurrency, platform assumptions, hidden state).
3. Produces a verdict: `SIGN-OFF` or `REJECT` plus a numbered defect list — each defect with
   file:line, why it is wrong, and the spec/contract clause it violates.
4. A critic must never fix code. It only finds and proves defects.

**Loop**: `REJECT` → the defect list goes straight back to the builder → builder fixes → a fresh
blind critic pass. Repeat until `SIGN-OFF`. No component advances to the next wave on a `REJECT`.

**Quality bar for sign-off** — all of:
- every Definition-of-Done item in `CONTRACT.md` §11 satisfied;
- no defect the critic can demonstrate with a failing test it writes;
- the component's owned spec IDs are each traceable to a test;
- the whole `pytest` suite green on the current platform.

## Carried findings — decisions owed by a later wave

Raised by a component's critic as correct-but-unresolved at its own boundary. Each names the wave
that must decide; none is a defect in the component that raised it.

| # | Raised by | Owed by | The decision |
|---|-----------|---------|--------------|
| **CF-1** | C3 (round: hardening) | **W2 C4 / W3 C6** | A single damaged `.reader` file wedges `reader_horizon()` **permanently** — it raises `GrafxCorruptionDetected` on every call and the file is never discarded, so **C4 can never recycle again**. Failing closed is the defensible direction and C3's suite asserts it, but there is no sanctioned way out. C4 (recycling) and C6 (quarantine) must agree who may retire a damaged reader record, and on what evidence. Do not let this be settled implicitly by whoever writes code first. **Extended (C3 round 4):** the LEASE-file equivalent is the same shape — a zero-length or damaged `control/writer.lease` makes every writer path fail closed with a non-retryable `corruption_detected`, and C3 offers no way out. Whatever rule retires a damaged reader record must answer for the lease record too, or say why the two differ. |
| **CF-2** | C3 (round: hardening) | **W2 C5** | `register_reader` publishes before the registration is visible to other processes, so a horizon pass in that window can miss a brand-new reader. C3's port cannot close this. **C5 must order registration against snapshot selection** so a reader is never chosen a snapshot the horizon has already passed. Carry with A46 (C5 owns reader-refresh scheduling) into C5's brief. |
| **CF-3** | C1 (round 5) | **W3 C6** | `HeapStore.apply_page_image` (the A22 redo door) accepts any checksum-valid image, and C1 is hardening against malformed slot 0 and against relinking that strands the tail cache. C6 must state what the redo path guarantees about page images it replays, so the two components do not each assume the other validates. |

### Carried findings — update

| # | Raised by | Owed by | The decision |
|---|-----------|---------|--------------|
| **CF-3 (resolved in part)** | C1 → decided by C1 | — | The unbounded-growth bound on the redo door is **C1's, not C6's**: C1 does the allocating, so C1 refuses (`MAX_REDO_GAP_PAGES = 4096`, measured 60 000 pages in 12.6 s before, refusal in 0.000 s after). C6 still decides what a refused redo *means* — truncation, quarantine, forensic entry — and can only do that because this door now hands it a typed refusal instead of a full disk. The remaining CF-3 question (what the redo path guarantees about page images it replays) still stands for W3. |
| **CF-4** | C1 (round 7) | **W3 C6** | `CatalogStore.save()` now refuses when the structure epoch has moved under it, rather than silently reloading — a silent reload would discard the caller's in-memory schema. **Caller-visible consequence: after replaying catalog pages, recovery MUST `load()` before it may `save()`.** The refusal is `transaction_state`, deliberately not `GrafxStaleEpoch`, because that code belongs to C3's writer lease and its recovery is a handover rather than a reload (A11-revised). C6's replay sequence must be written against this. **CORRECTED (C1 round 8):** as delivered the refusal fires on a mere `pool.invalidate()` -- the epoch bumps although no page changed -- and `load()`, the remedy recorded here, DISCARDS the caller's in-memory tables (measured `['Person','Second']` -> `['Person']`), which is the outcome the guard exists to prevent. Do not write C6 against this row until C1 has settled it: the epoch must distinguish 'the pages changed' from 'the cache was dropped', and the remedy must not be a schema-destroying reload. |

## Sign-off register

A component signs off when a blind critic finds no BLOCKING defect under CONTRACT §13. Punch-list
items travel with it to W6 and do not block the wave.

| Component | Status | Evidence |
|---|---|---|
| **C2 Storage adapters** | **SIGNED OFF** (re-confirmed after CF-5) | 274 tests / 0 failures / 3 skipped x 5 runs. CF-5 fixed with a POSIX-semantics rename (`FileRenameInfoEx`), verified by ~25,000 cross-process observations with zero mixes and 1,500/1,500 publications over a held control file. The fix's own UTF-16-units defect (a non-`Grafx*` escape, and a silent mis-publication reporting success) was caught by the blind critic and fixed; **coordinator verified the fix independently** — sizing in code units, the non-BMP test present, suite green. Battery 6/7 with the survivor explicitly NOT claimed equivalent (12 stated cells, gaps written down). |
| **C3 Process coordinator** | **SIGNED OFF** (round 5) | 541 tests x 6 runs, 0 failures, no non-determinism; 603 grants across 603 distinct epochs with no epoch issued to two owners; 12 thundering-herd races, one winner each; ~162,600 decode calls with zero non-`Grafx*` escapes; **oracle proved able to fail** (unguarded control produced 32 grants over 29 epochs). 8 punch-list items. |
| **C0 Foundation** | **SIGNED OFF** (round 10, with recorded risk) | 763 gate + 1677 foundation tests, 0 failures, **0 skips**, on 3.13 AND 3.11 -- the 3.11 half measured twice on separate forks under different contention, identical counts. Three round-9 defeats closed: the closed set is now the D9 support matrix per reading (`PLATFORM_DOMAINS`), a name bound more than once at module level is refused rather than followed, and the collection expectation comes from pytest's own `--collect-only`. 22 permanent probes; 12/12 acceptance. **Coordinator re-ran all three demonstrated defeats independently: all refused.** |
| **C1 Storage core** | **SIGNED OFF** (round 9) | 757 tests x 6 runs, 0 failures, no non-determinism; refusal swept through every refusing step on both `_append` branches (4 seams x 3 error classes x 3 budgets x 12 scenarios), growing branch **proved reached** by page-count growth; 18 x 300 mixed ops with ~1,734 injected refusals, warm and cold row sets identical every run; injection route **proved able to fail**. Critic withdrew **969 findings** as its own instrument. 9 punch-list items. |
| C8 Observability | round 5 final review | 581 tests, battery 83/83, 5/5 deterministic under load |
| **C4 Write-Ahead Log** | **SIGNED OFF** (round 1) | 242 tests x 8 runs identical; 550 bounded crash runs under a reordering write cache with 0 problems; 3,348 damage-fuzz probes with **zero non-`Grafx*` escapes, zero hangs, zero non-idempotent replays, always openable**; BR-4 and BR-10 boundaries exact; §6.5 layout verified byte-for-byte. **The control-plane claim C6 rests on was verified independently** by recording every port call with every argument across five path families. Battery 24/30, all six survivors behaving correctly. 8 punch-list items. |
| **C5 Transaction manager** | **SIGNED OFF** (round 1) | 138 tests x 6 runs identical; **1,788 oracle rounds** of snapshot isolation under 3 concurrent writer processes with 0 violations, **oracle proved able to fail** (swapped steps -> 18 violations in 74 rounds); OCC exact in both directions, 100 commits / 152 refusals / counter exactly 100; §8.5 walked step by step against a real device trail; 93 crash points all idempotent; CF-2 verified as an ordering under forced interleaving. Battery 12/12. 12 punch-list items; **6 findings withdrawn** as instrument faults. |

### CF-5 — BLOCKING: `atomic_replace` cannot publish a control file any participant has read (Windows)

Raised by C5 while driving real multi-process commits; **independently reproduced by the coordinator**,
same-process and cross-process, against the delivered `LocalStorageDevice`:

```
read through the port -> b'ORIGINAL'
descriptor cached?      ['writer.lease']
os.replace ONTO held-open file: FAILED  winerror=5  Access denied
os.remove  of held-open file: SUCCEEDED
CROSS-PROCESS os.replace onto held-open file: FAILED winerror=5
```

**Why it matters.** §6.1 publishes `control/writer.lease` and `control/commit.state` with
`atomic_replace`; D1 requires full multi-process read AND write; Windows is the primary platform.
`LocalStorageDevice` caches descriptors (`_handles`), so once **any** participant has merely *read*
either control file through the port, **no other participant can publish it**. C3 reads via
`_read_lease_record` and publishes via `storage.atomic_replace` (`:1575`), so the coordinator is on
this path in production even though its own multi-process suite passed.

**A16 covers the wrong verb.** Its claim is about `remove`, which does succeed. `os.replace` ONTO a
held-open target needs DELETE access on that target, and CPython's `os.open` opens with
`FILE_SHARE_READ|FILE_SHARE_WRITE` only -- **not** `FILE_SHARE_DELETE`. A handle opened that way
blocks a rename onto its name from any process.

**Owner: C2** (`adapters/storage_local.py`). The device must open files it may later have to publish
over with sharing that permits deletion -- `CreateFileW` with `FILE_SHARE_READ|WRITE|DELETE` and
`msvcrt.open_osfhandle`, or release the cached descriptor around the rename on both sides (which
cannot help the cross-process case and is therefore not sufficient alone).

**Blast radius to re-check once fixed:** C2's sign-off, C3's sign-off (its 603 multi-process grants
were measured on a path that avoided this window -- establish why before trusting them), C5's commit
publication, C4's segment publication, and any W3+ component that publishes by rename.

### CF-1 — RESOLVED by C4, and binding on C6

C4's position, stated in `engine/wal_manager.py`'s module docstring and **asserted by a test** rather
than promised:

1. **C4 never retires a reader record, a lease record, or any control-plane file.** It opens nine
   storage-port doors and every name it passes begins with its own log directory;
   `test_the_log_never_reaches_outside_its_own_directory` asserts that across every public method.
2. **An unknown horizon is not a horizon.** When `reader_horizon()` raises, nothing is recycled. The
   cost is bounded, visible growth reported through `oktografx_wal_truncation_lag_segments` and
   `oktografx_wal_segments` -- diagnosable, never silent.
3. **Retirement belongs to C6**, as a deliberate recovery act, in this order: quarantine the record
   with a manifest -> write the forensic ledger entry -> retire the name -> report it in the recovery
   report. **Never as a side effect of a hot path.**
4. **The lease record follows the same rule.** The two differ only in the consequence of failing
   closed (blocked reclamation vs blocked writing), which argues for building C6's path promptly --
   never for letting a writer delete the file that is refusing it.
5. **C4 asks C6 to re-derive the horizon after any retirement**; C4 caches no horizon across a pass.

### C4/C5 format ownership — converged without arbitration

C4 owns the WAL format (§1), and landed §6.5's COMMIT payload in `domain/wal/commit.py`. C5 had
written the same layout independently, **deleted its copy and re-exported C4's** (A24). Two further
seams settled the same way: `append_many` may advance `last_lsn` by more than `len(records)` because
a `SEGMENT_HEADER` consumes an LSN like any other record -- C5 found this by driving the real WAL
rather than a double, and stamps each image once with `last_lsn + len(batch)`, which is still strictly
above every LSN assigned before the batch. §6.5 freezes the SEGMENT_HEADER type code but not its
payload; C4 defines it as `<QQd` (segment number, previous last LSN, wall stamp).

### C0's sign-off carries a recorded risk (CONTRACT §13)

C0's skip-attribution gate was defeated in **seven consecutive rounds**. Round 10's fixes were
verified by the builder and its three specific defeats re-verified independently by the coordinator,
but round 10 did **not** receive a full blind adversarial sweep. That is a deliberate, recorded
acceptance rather than an oversight, on three grounds:

1. **The frame was wrong, not the effort** (LESSONS **L4**). The gate asks whether an author's stated
   reason for skipping is honest, from code the author wrote. A89's diagnostic -- *can the author add
   a member to the satisfying set?* -- kept answering yes in a new dimension each round, because the
   author writes the whole input. An eighth round would very likely find an eighth dimension.
2. **The durable guarantee moves to where it can hold.** Every test must be **observed to run** on at
   least one family across the D9 matrix, from real runs, which no author can extend. That is a
   cross-run check owned by **C13 (bench/CI)** and **W6**. Until it exists, this risk is open.
3. **What remains is a fast local smoke alarm**, and a good one -- it caught every evasion any critic
   demonstrated, including sub-expression smuggling, rebinding, and seven dynamic-collection shapes.

**Owed in W6:** the cross-family coverage check, after which this risk closes. Do not treat the
conftest rule as the guarantee in the meantime.

### CF-6 — BLOCKING elsewhere: the real `WalManager` cannot serve multi-process writes

Raised by **C5's critic** while driving real commits through the shipped WAL. **Not C5's defect** and
not C4's fault under the frozen text -- §8.5 has no re-open step and §8.3 does not say `append_many`
re-derives the tail. It needs a decision, which is why it is here rather than on a punch list.

`WalManager` holds its segment index in memory from `open()` (`wal_manager.py:370`, `:755`), so a
second participant's `last_lsn` and `read_from` never observe the first's appends. Measured through
`TransactionManager.commit()`:

```
A commits (csn 3) | B opens | A commits again (csn 5) | B commits and ALSO reports csn 5
cold reader -> GrafxCorruptionDetected: "The log expected sequence number 6 here and the record carries 4"
```

That is **silent log corruption from ordinary multi-process operation**, which D1 requires. C5's own
multi-process suite passes because it drives `LogWal`, a C5-owned double that rescans the segment;
its one real-WAL conflict test calls `second.wal.open()` by hand. **AC-1/AC-2/AC-3 are therefore not
closed against the shipped WAL.**

**Decision: C4 re-derives the tail inside the commit section.** The alternative -- amending §8.5 to add
a re-open step -- pushes the cost onto every caller and leaves the trap in place for the next one.
**C5 additionally adds the cheap guard its critic identified (P3):** at step 3.2 it holds `current`
and at 3.4 `committed`, and `committed <= current` is impossible for a fresh commit. One comparison
turns silent corruption into a typed refusal, and it is defence in depth even after C4's fix.

### CF-A — page granularity vs partition granularity, owed by W6

Two transactions writing **disjoint partitions** that land on the same physical page both commit --
BR-6 requires it -- and the second full-page image replaces the first. Measured: `alice=1`, then
`bob=1`, page 3 holds only `bob=1`. §8.5 step 3.3 freezes the OCC predicate as partition-only, and
adding a page term would refuse legal commits, so C5 cannot fix this from inside. **Whoever stages
page images (C1/C7/C11) must guarantee that disjoint-partition transactions never stage the same
page, or W6 revisits the granularity.** Do not let this be settled implicitly.

### CF-4 — REFINED by C6, and its reading is better than the one I recorded

I recorded the remedy as `read_from_pages()` -> `adopt()` -> `save()`. C6, building the actual
recovery path, took `read_from_pages()` -> `adopt()` and **deliberately does not `save()`**:

> a save writes pages the log never covered.

That is the correct reading. What CF-4 requires is that the catalog store not be left **refusing**,
and `adopt()` alone achieves that -- proven by `test_the_catalog_can_save_after_recovery_replayed_its_pages`.
Adding a `save()` would have recovery durably write state that no log record justifies, which is the
opposite of what a recovery pass may do. **The recorded remedy is amended to read/adopt, never save.**

### CF-7 — a freed slot must not be classified as damage (owner: C1)

`domain/page/slotted.py:559` raises `GrafxCorruptionDetected` for a **legitimately freed slot**, and
C7 frees slots on purpose (`index_manager.py:1013`). A11-revised reserves that class for damaged
bytes because FR-8/FR-10 turn it into truncation, quarantine and a forensic ledger entry -- so an
ordinary state currently routes into the damage machinery. C6's verifier had to guard with
`is_slot_free` rather than trust the exception, which is the workaround shape that indicates a wrong
contract rather than a careless caller. **Routed to C1**, with A66.1 applied: every path that turns
"slot is not live" into `corruption_detected` shares the defect.

### The §6.6 reason-code mapping — C6's reading, recorded so it is not re-litigated

§6.6 freezes **six** reason codes; C4's `FailureReason` has **seven** verdicts. C6 read the codes as
naming *why work was discarded*, not the shape of the damage: `CHECKSUM_FAILURE` -> 2, every other
undecodable range -> `1 truncated_tail` (it **is** the tail being cut), with the exact decoder verdict
travelling in the payload envelope. `UNSUPPORTED_VERSION` maps to nothing and instead **stops
recovery** with `schema_version_mismatch`, rather than truncating intact bytes a newer build wrote.
That last choice is the important one and it is right: recovery must not destroy data it merely fails
to understand.

### CF-8 — `ReconcileReport` name collision: DECIDED, C9's rename stands

C7 owns `ReconcileReport(index, horizon, scanned, reclaimable, removed, retained, pages_touched)` --
a report about **page reclamation**. C9 needed to hand back WAL records, because **BR-3 requires every
cleanup to be a replayable log record and a count is not one**, and renamed its own to
`VectorReconcileReport` rather than invent a second framework or edit C7's file.

**Decision: keep them distinct by name. C7's report does NOT grow a records field.** They are two
different concepts that happen to share a verb -- one answers *what space was reclaimed*, the other
*what must be replayed to reproduce the cleanup*. Collapsing them would make C7's report carry a
payload only one caller wants, and A24 is satisfied by there being exactly one `ReconcileReport` in
the build. C9 handled this correctly: it adopted C7's real types where they fit (`IndexVisibility.PROXIMITY`
imported identity-equal, `walk()` yielding C7's `IndexEntry`, `isinstance(VectorIndex, SecondaryIndex)`
verified True) and renamed only where the meaning genuinely differs.

### CF-9 — the key-size constants disagree, and an index key is not a payload

`MAX_INDEX_KEY_BYTES = 65535` (C7) versus `MAX_VECTOR_DIMENSION = 16384` (C1): a float64 space above
**8189** dimensions -- float32 above 16378 -- produces a key that C7's `IndexEntry` refuses. The
failure is a typed `GrafxIndexError` from `walk()` only; searches and staging are unaffected.

**The constants are the symptom. The question is why a vector is in the index key at all.** A
16384-dimension float64 vector is ~131 KB, and an index key that large is not an index key -- it is
the payload wearing the key's name. Raising C7's limit to accommodate it would bless that.

**Owed by C9, with C7 as the reader:** state whether the raw vector must be in the key, and if so
why the identifier plus a heap reference will not serve. If it must, then C1's `MAX_VECTOR_DIMENSION`
and C7's `MAX_INDEX_KEY_BYTES` need a **documented relationship** rather than two independently chosen
literals -- one derived from the other, pinned per A56/A68, so they cannot drift again.

### C0's forward-guard tests have met their forward

Three `tests/foundation` failures (`test_packaging` x2, `test_bootstrap` x1) are **C11's facade
landing against C0's "not yet" assertions** -- `connect` now resolves to `okto_grafx.api`, and
`_DEFAULT_PORT_FACTORIES` is no longer empty. Those assertions were correct when written and are now
describing a world that has ended. C0 updates them; this is expected churn, not a regression, and it
is the first evidence that the composition root is real.

### CF-6 — RESOLVED, and it created a binding constraint on every WAL caller

C4 re-derives the end of the log from the device at the top of `append_many`. Cost is pinned by
tests, not merely intended: an unchanged log costs one `list_files` plus one `log_size` and **reads
zero bytes**; a moved tail reads **only the new bytes** (the scan resumes at the last known record
boundary, so cost is proportional to the other participant's work, not to the log); only a segment
that **shrank** falls back to a full pass. It does not reintroduce P2a -- the re-derivation records
`_damage` rather than raising, so the repair door stays reachable.

**The new constraint, and it is load-bearing rather than advisory:**

> The re-derivation is sound **because** `append_many` runs inside `coordinator.exclusive("commit")`.
> Called outside that section, another writer can append between the refresh and the write, and
> **CF-6 returns**.

§8.5 already mandated the commit section; it is now the reason the fix works. **Any component that
appends to the WAL must do so inside it** -- C5 does, and C6's replay path and C11's composition must
not find a shortcut around it. A caller that appends outside the section reopens duplicate-LSN
corruption with no guard to catch it.

C4 also strengthened the CF-1 test the round-1 critic called weaker than its own claim: the recorder
now captures every call whatever the argument type, so `durable_barrier(None)` is visible and
asserted absent, and it drives the undo and damaged-open paths.

### C0's recorded risk is CLOSED — the L4 cross-family coverage check exists and was proven able to fail

C13 delivered the check LESSONS L4 assigned it, and proved it on **two real families** (Windows 11 /
CPython 3.13.1 and Linux / CPython 3.12.13):

- It planted a **legitimate** `platform_specific` test -- real family condition, live counterpart --
  that could run on neither family. **Both sessions exited 0 and C0's per-session gate accepted both
  skips.** The cross-family check returned **exit 1**, naming the node.
- Control with the plant removed: exit 0. Also proven red: a Windows-only matrix (exit 2, required
  family absent), a leg relabelled as another family, and a leg that wrote no junit.

**Real matrix reading: 6,756 nodes, 6,756 observed to run, 0 ran nowhere, 0 debt, 35 declared partial.**

Why it cannot be argued with, where seven rounds of the per-session gate could: it reads pytest's own
`--junitxml` and needs no hook, and the debt allowlist lives **inside C13's scope**, so a test author
cannot add a member to the satisfying set from the file they are editing (A89 applied structurally).

**What it cannot catch, stated by C13 rather than discovered later**: a test never *collected* on any
family (C0's per-session A69 reconciliation owns that -- neither check subsumes the other); a test
that runs but asserts nothing; platform-dependent parametrised ids; anything about a leg the matrix
did not run. The two gates are complements, and both are now load-bearing.

### D9 finally has evidence: the first real POSIX run found 8 failures

Seven are C8's -- five `Errno 98 Address already in use` (the port not released on stop) and two where
the `RotatingFileWriter` ownership guard does not fire on POSIX -- and one is C0's skip-attribution.
C8 had been reviewed **six times on Windows alone**. Routed to C8 as blocking on that family.

### CF-9 SUPERSEDED / CF-10 — DECIDED: `VectorIndex` builds on C7's `IndexStore`

C7 reports the full divergence, measured against C9's delivered file. `VectorIndex` matches the frozen
protocol **shape** exactly (all 8 members, matching signatures) and imports
`IndexVisibility.PROXIMITY` identity-equal -- C9 did adopt the seam where it fits. But it also:
re-declares `ReconcileReport` with different fields under a name §8.7 gives to **one** type;
re-declares `SnapshotLike`/`TransactionLike`; carries its own operation codes and payload codec; is
**memory-resident** (no `index/*.idx`, no durable header, no staleness detection); and **cannot
register with `IndexManager`**, which needs `definition`, `create`, `check_freshness`, `commit`,
`rollback`.

C7 deliberately did **not** relax registration, and it was right: accepting a bare protocol object
would let `commit`/`verify` raise `AttributeError` out of a public door.

**Decision: C9 builds its vector index on `IndexStore`**, inheriting durability, staleness detection
and `verify` rather than reimplementing them. Reasons, in order:

1. **Two index frameworks is the outcome both were told to avoid.** C7 owns the seam; C9 adopted half
   of it and rebuilt the other half.
2. **Memory residence has a cost D5 has already measured.** A vector index that rebuilds from the WAL
   on open adds to open-with-replay, and that ceiling is **already missed at 3.51x**. A persistent
   index with a durable `built_through_lsn` is the cheaper answer to a problem we now have numbers for.
3. **Staleness detection is not optional for a proximity index.** C7 marks an index stale *durably* and
   refuses lookups rather than returning a short answer. A vector index without that returns a
   confidently wrong top-k after a crash -- the exact "wrong results" §13 rejects for.

**If C9 can show a specific reason the vector index cannot sit on `IndexStore`** -- a property of HNSW
that the bucketed store genuinely cannot express -- I want that reason with a demonstration, not an
assertion. Absent one, adopt it. This supersedes CF-9's narrower key-size question, which dissolves if
the vector payload stops living in an index key.

### C5 seam — index records are staged and never committed

C7 reports that `TransactionManager` holds `index_manager` and **never calls it**. It must call
`IndexManager.commit(txn, csn)` after the barrier (§8.5 step 6 region) and `rollback(txn)` on abort.
Until then, index records staged into a transaction are written to the WAL but never applied to the
index -- BR-11's "appended inside the same commit" is half-satisfied. Routed to C5.

Also from C7: C5 stamps its own records with the lease epoch but appends `pending_records` untouched,
so an index record carries the epoch known at **staging** time rather than at commit.

### Operational note, disclosed by C7 rather than discovered

While clearing a file lock, C7 used a process filter keyed on the **session-directory id**, which is
shared with sibling agents, and it stopped several other components' `pytest` runs (C6's
`tests/recovery`, C8's `tests/observability`) around 15:00. No battery driver and no source was
mutating, so nothing is damaged -- but **an aborted run in that window is not evidence of a defect**.
Re-run rather than diagnose. C7 narrowed the filter to its own fork path afterwards. Same family as
L3: a shared identifier used as if it were private.

### MILESTONE — the stack assembles end to end (C11, W4)

`connect()` -> identity page -> recovery -> DDL through `txn.execute()` -> durable commit (WAL append
+ barrier + page apply + `commit.state` publish) -> `close()` -> reopen -> recovery replays ->
`verify("all")` clean -> `db.execute()` read. **All seven port slots wired, thirteen engines composed,
nothing stubbed.** Row *writing* is refused by C10 with a stated cross-component reason (nobody
allocates `RecordId`; nobody applies staged index work at the commit number) -- its seam, not C11's.

The **error taxonomy composes**, verified by **identity** rather than type: a `GrafxCorruptionDetected`
and a `GrafxStorageError` planted in `StorageDevice.read_page` arrive at `Database.execute()`
unchanged through QueryEngine -> HeapStore -> BufferPool; a `GrafxDurabilityBarrierFailed` arrives at
`Transaction.commit()` with `details["retryable"]` intact. The one conversion is a **foreign**
(non-`Grafx`) exception from a caller-supplied adapter, chained with `from`.

### Residual contract gap — §6.2's `page_size` cannot diagnose what it exists for

Reopening at the wrong page size reported `corruption_detected`, because the device divides the file
by the *configured* size and refuses before the identity record can be read. C11 now refuses with
`GrafxSchemaVersionMismatch` when the identity file is not a whole number of configured pages --
but **when the wrong size still divides evenly, the page decode reports corruption and cannot be told
from real damage.** Closing it needs a fixed-offset header readable independently of the configured
page size. Recorded for W6; do not let a component "fix" it locally by loosening the corruption check.

Also settled by C11: **§6.2's meta page was claimed by no component.** C6 reads `grafx.meta` page 0 as
C1's `FileHeaderPage`, which carries none of FR-1's `database_uuid`/`created_at_wall`/
`partitions_per_table`. C11 put the §6.2 record in **slot 1** of that same header page, so C6's slot-0
read is untouched, and kept its own CRC-32C even though the page checksum already covers it.

### CF-9 — DECIDED: `IndexDefinition` declares its key derivation; a vector index keys on a DIGEST

C9 adopted C7's `IndexStore` (CF-10 settled) and then found CF-9 does **not** dissolve. It tried the
identity key and `IndexManager.verify` refused every entry: verify checks
`definition.key_for(version.values) != entry.key`, and `key_for` is `encode_value(row[position])`.
**The framework's rule is that the key IS the encoded column value** -- for a vector column, the whole
vector.

Measured consequence: two of C7's own rules (key = encoded column value; an entry is never split
across pages) make the maximum embedding dimension a function of `page_size`.

| page_size | key budget | max f32 dims | max f64 dims | `DOUBLE[384]` |
|---|---|---|---|---|
| 512 (minimum) | 449 B | 107 | 53 | **refused** |
| 4096 | 4033 B | 1003 | 501 | fits |
| 8192 (default) | 8129 B | 2027 | 1013 | fits |

Note this makes `MAX_VECTOR_DIMENSION = 16384` unreachable at **every** page size -- 16384 float64 is
~131 KB against an 8129 B budget -- so the constant was already a claim the code could not honour.

**Decision: `IndexDefinition` declares its own key derivation, and `verify` uses the declared rule
rather than assuming `encode_value`.** A vector index declares a **digest of the vector** as its key.

Why this over the alternatives:

- It **keeps the drift detector C9 correctly valued.** A changed heap vector produces a different
  digest, so verify still catches index-heap divergence -- at constant key size instead of linear.
- Accepting and documenting the cap would leave `MAX_VECTOR_DIMENSION` a lie and make the supported
  dimension depend on a storage parameter, which no caller would expect.
- Restricting verify to column-derived indexes would weaken it for **exactly the index kind that most
  needs it** -- the one whose results a caller cannot eyeball.

**Owner: C7** implements the declared derivation in `IndexDefinition` and `verify`. **C9** then
declares the digest. Pin whatever digest is chosen per A56/A68 -- it is a format decision, not a
tuning knob.

## W5b — ROW WRITING: the decision, and who owns each half

`connect`, DDL, durable commit, close, reopen-with-replay, `verify("all")` and read queries all work
end to end (measured). **Writing a row does not**, and `query_engine.py:1106` refuses with a typed
`GrafxUnsupportedOperation` naming two precise reasons. Both are seams between existing pieces, not
missing machinery:

> *"Nothing in this build assigns the RecordId of a new row."*
> *"A row cannot be written at its commit number... The transaction manager holds an index_manager and
> never calls its commit(txn, csn)."*

**What already exists and must not be rebuilt:** `HeapStore.insert(table, record_id, values, xmin)`
already takes **both** the identity and the commit number, and validates both (`_require_record_id`,
`_require_commit_number`). The catalog already carries durable monotonic counters -- `next_table_id`,
`next_space_id`, frozen into §6's layout -- with the refusal that a stored counter may never sit below
the identifiers in use. **The pattern for a record-id allocator is already proven in this codebase.**

### Ownership

**C1 — a durable per-table `next_record_id`.** Model it on the existing catalog counters. Three
invariants, and the third is the one to design against:
1. allocated once, **never reused**, including across a crash -- so the counter is durable before a row
   is acknowledged;
2. **stable across versions** (§3) -- update and delete carry the same id the insert allocated;
3. **replay must not re-allocate.** A WAL insert record carries its `record_id`, so recovery uses the
   logged id and the counter need only end up **at or above** every id in use -- exactly the rule the
   catalog already enforces for table ids. Choose where the counter lives with cost in mind: the
   catalog page is rewritten rarely, `TableExtent` is already rewritten on every append.

**C5 — stage the row, apply it at the commit number.** The csn is known at §8.5 step 3.2, before pages
are written at 3.6. So a transaction **stages the row intent** (table, values) and, inside the commit
section, allocates the id and calls `HeapStore.insert(..., xmin=csn)`. This is the same shape as the
`pending_records` staging C5 already has for WAL records. **And call `IndexManager.commit(txn, csn)`
after the barrier and `rollback(txn)` on abort** -- already routed, and it is half of this gap.

**C10 — emit a write plan the transaction can execute**, replacing the refusal at `:1106`.

### The invariant that binds all three

**Nothing becomes reachable before every step that can still refuse has succeeded.** C1 was rejected
twice for this shape and C4 once; a row must not appear in the heap, the index, or an id counter until
the commit that owns it has passed every refusal. Report the counterfactual per site (A66/A66.1).

## W5c — two contract gaps C10 surfaced, both DECIDED

### 1. §8.9's own example violates §7.2's identifier rule — the EXAMPLE is wrong

`CONTRACT.md:792` writes `space => 'minilm-v2'`. §7.2 binds `EmbeddingSpaceDef.name` to C1's identifier
rule, which refuses a hyphen. C10 parses the example faithfully and refuses at **plan** time rather
than silently rewriting `-` to `_`, which is the correct behaviour and I am keeping it.

**Decision: the example is wrong, not the rule.** An identifier that names a durable object ends up in
a file name, an index name and a digest; loosening it to accommodate an illustration would be the tail
wagging the dog. §8.9's example should read `minilm_v2`. **Nothing in code changes** -- this is a
documentation correction, recorded here because the next reader will hit the same contradiction.

### 2. A relationship has nowhere to store the two rows it connects — DECIDED

§7.2 gives a relationship table its endpoint **tables** (`from_table`, `to_table` at `CONTRACT.md:549`)
and its property columns, **and no place for the two rows a stored relationship actually connects.**
So traversal and relationship writes refuse today, and **this is the remaining half of VEC AC-7**:
similarity plus a node filter is proven in one tree, while the relationship filter and the 2-hop
traversal wait on this clause. A graph database that cannot say which two nodes an edge joins is not
finished.

The contract is frozen at A95 for *raising the bar mid-review* (§13). **This is not that**: it is a
missing normative rule that blocks a stated requirement, and filling it is the opposite of moving the
goalposts. Recorded here as a decision rather than an amendment.

**Decision: a relationship row carries the `RecordId` of its source and its target**, as two reserved
columns ahead of the user's property columns. Reasons, in order:

1. **The identity it needs was built for exactly this and landed today.** C1's `RecordId` is §3's
   *"stable logical identity across versions"* -- which is precisely the property an edge endpoint
   requires, since an endpoint must survive its node being updated. A `RecordRef` would not: it names a
   page and slot, and both move.
2. **It reuses the allocator's guarantees** -- allocated once, never reused, replay-safe -- rather than
   inventing a second identity scheme for edges.
3. **`from_table`/`to_table` already pin the tables**, so the pair `(table, record_id)` is complete
   without widening the catalog.

**Ownership:** **C1** reserves the two columns in the relationship record layout and states how they
are encoded; **C10** parses `CREATE (a)-[:KNOWS {...}]->(b)` and traversal against them; **C5** stages
them like any other row. The binding invariant is unchanged: **nothing becomes reachable before every
step that can still refuse has succeeded** -- an edge naming a row that does not exist must refuse
before anything is staged, not after.

### E1 — BLOCKING: optimistic validation can never refuse a schema commit (owners C5 + C10)

Found by **C11's critic** driving three OS processes through the real composition root. Deterministic,
3/3 on a fork and again on the current tree.

```
P2 commit csn=3 durable=True     (CREATE NODE TABLE B)
P3 commit csn=5 durable=True     (CREATE NODE TABLE C)
P1 commit csn=7 durable=True     (CREATE NODE TABLE A)   <-- no GrafxWriteConflict
fresh reopen sees ['A'];  LOST ['B','C'];  verify("all") findings = ()
WAL still holds all 7 records -- the log kept them, the catalog page image did not.
```

**Root cause.** `TransactionContext.stage_page_image` (`domain/txn/context.py:324`) never calls
`note_write`, and DDL reaches the log only through `QueryEngine._stage`
(`engine/query_engine.py:603`). A schema commit therefore carries an **empty read set and an empty
write set**, and `TransactionManager._find_conflict` (`engine/txn_manager.py:648`) **short-circuits on
an empty interest set** -- a transaction declaring interest in nothing can never conflict with
anything. `CatalogStore._require_derived_from_current_pages` does not cover it either: it keys on a
**process-local** `BufferPool.structure_epoch`, which is the same root as the cross-participant
staleness routed to C1.

**Why it outranks the rest of the open work:** three participants each received `durable=True` and two
were lying, and `verify("all")` reports **clean** -- a consistency check agreeing with the loss, which
this build has consistently treated as worse than a crash.

**The property owed:** no commit may reach the log with an **empty interest set** while having staged
durable state. "I touched nothing" and "I declared nothing" are currently the same value and must stop
being so. Whether the guard lives at the staging door or in `_find_conflict`, and whether C10 declares
the interest or C5 infers it, is for C5 and C10 to settle -- **through the contract, reporting rather
than guessing.** C11's wiring is faithful and its verdict is not charged with this.

**Distinct from PUNCHLIST #130**, which is a store that never read. This is a store that **read at
open, is stale, and is reachable through the documented open sequence.**

### E2 — a refused DDL leaves the catalog reachable (open, C1 owes the door)

Found by C10 while verifying E1's fix. `_schema` does `catalog.add_table(...)` then `CatalogStore.save()`
**before** the commit; `save()` writes through the buffer pool immediately, so a refused DDL leaves
uncommitted catalog pages in the pool. The winning transaction then applies only its own images over a
subset and the header stops describing the chain:

    GrafxCorruptionDetected: The catalog of 'catalog.dat' declares 125 bytes but its chain carries 63

Rows already have the right shape (`stage_row_insert`, applied at the csn). The catalog has no
equivalent. C10 is correctly refusing to invent one; C1 owes `CatalogStore.stage(catalog) -> pages`
beside `read_from_pages()` / `adopt()`, settled with C5 who owns the staging container.

### E1 — closed on the conflict half

C5 made `stage_page_image` call `write_partitions.add(page_partition(file, page_index))`, so declaring
is **implied by staging** rather than remembered by each caller — the structural option, not the
per-caller one. Verified through the real composition root: three schema commits at one snapshot, all
three declaring the same catalog page, one durable and two refused `write_conflict retryable=True`.
Pinned by two tests in `tests/query/test_lifecycle.py`.

### C4 -> C6 surface change

`TruncationReport.truncated_segment` now names the **new** segment that ends at the cut; the old name
appears in `removed_segments`. Follows from a cut never rewriting a segment in place. C6 consumes this.

### W5d — a vector index covers a (table, space) pair, not a space (C9, verified)

`create_space()` does not make a space searchable. The index framework's definition names a **table and
a key column**, so a space alone has no table to name and no column position to key on. Therefore
`CREATE NODE TABLE … VECTOR(space)` must call `engine.attach(table, space_name)` **once per vector
column, after adding the table to the catalog**. `engine.index(name)` refuses with `GrafxIndexError`
until a table attaches, which makes the ordering visible rather than silent.

`VectorEngine` requires `pool=` and `indexes=` from the composition root; without them it refuses
`attach()` with a typed `GrafxConfigurationError` naming the field rather than falling back to a
memory-resident index — a fallback would be a second index implementation, which C9 was told not to
have. Construction still succeeds, so only `attach()` is affected.

**On reopen, the same call.** `attach()` is safe to repeat: `IndexStore.create()` opens an existing
file rather than replacing it (G6), so re-attaching adopts the durable index; the caller then runs
`indexes.open(published_lsn)` and rebuilds whatever reports stale.

Verified rather than asserted: a second composition over the same device (new pool, stores, registry
and engine) attaches to the existing index, reads 6 durable entries, reports `stale is False`, and
returns byte-identical record ids and scores to the first engine, with no log replay. Routed to C11.

### M1 — first durable end-to-end round trip through the section-10 public door (coordinator-verified)

Verified by the coordinator against the shared tree, independently of C10's report, on **disk** (not
`:memory:`), through the exact door CONTRACT §10 specifies:

```
DDL   : CREATE NODE TABLE / CREATE REL TABLE          -> ok
INSERT: CREATE (:Person {id: 1, name: 'Ada'}) x2      -> {'rows_created': 1} each
EDGE  : MATCH (a),(b) CREATE (a)-[:Knows {since}]->(b) -> {'rows_scanned': 6, 'relationships_created': 1}
READ  : db.execute("MATCH (p:Person) RETURN p.id, p.name") -> [(1,'Ada'), (2,'Grace')]
verify("all")                                          -> ()
close -> reopen -> replay -> same rows, verify()       -> ()
SECOND OS PROCESS reading the live database            -> same rows, verify() -> ()   (D1)
SET / DELETE                                           -> GrafxUnsupportedOperation naming the missing door
```

`Database.execute()` opening a **read** transaction is §10 as written ("autocommit read"), not a
defect — writes go through `db.begin("write")` and `txn.execute(...)`. The coordinator's first probe
mis-read this as a blocker; recorded because the same shape (an obvious-looking public door that is
deliberately narrower than it appears) will be read the same wrong way by users, and C12's CLI and the
README both owe an example that shows the write door.

Standing gaps at M1: `SET` and `DELETE` (C5 owes `stage_row_update` / `stage_row_delete`), traversal
(C10, unblocked now that `_from`/`_to` are readable), E2 (C1 owes `CatalogStore.stage`), and the
`VectorEngine` wiring in `assembly.py` (C11, shape supplied by C9 as W5d).

### D5 — both ceiling misses reproduce independently (blind critic, C13 round 1)

Re-run in the critic's own fork with the artefact parameters (`--iterations 30 --warmup 5
--records 2000 --rows 512`), against a real LadybugDB subprocess per operation:

| ceiling | C13 | independent re-run | limit | verdict |
|---|---|---|---|---|
| durable_commit | 109.40x | **123.63x** | 10x | MISSED, both |
| point_read | 0.0235x | 0.0278x | 5x | MET, both |
| open_replay | 3.51x | **3.55x** | 3x | MISSED, both |

Status `consult_jp`, exit 1, both runs. The commit spread (109 vs 124) is machine load — eleven agents
were running and **both sides slowed together** (subject median 234 -> 284 ms, baseline 2.14 -> 2.29 ms),
so the verdict is unchanged either way.

**The replay attribution is confirmed and it belongs to C4, not to C13.** On the same 2000-record log:
`WalManager.open()` median **476.70 ms**, `scan_all()` median **480.48 ms** -- the log is walked twice,
at equal cost. `domain/page/checksum.py:crc32c` is called **4002 times (2 x 2001)** for **0.824 s of a
1.173 s** profiled run, ~70% under cProfile against C13's 79%. Both halves of the diagnosis hold: the
cost is C4's `open()`-then-`scan_all()` shape, and removing the second walk lands replay near ~1.8x
without touching `checksum.py`.

Stated limit of the re-run: the whole-repo cross-family reading (6,756 nodes / 35 partial) could not be
re-taken, because WSL's CPython is 3.10 and `okto_grafx` needs 3.11+ (`typing.Self`). That figure is
neither confirmed nor refuted.

**The commit ceiling is the open decision and it belongs to the user**, not to a component.

### CF-11 — BR-10 log reclamation and automatic checkpoint policy (CLOSED, M1)

The original C4 blind review found a real wiring gap: the recycle machinery existed but no assembled
database drove it. The first closure added `TransactionManager.checkpoint()` under the commit section
and the explicit `Database.checkpoint()` door. M1 closes the operational half: after a durable write
commit and schema settlement, `Database` reads the published state under its participant section and
runs a checkpoint when `last_committed_lsn - checkpoint_lsn >= checkpoint_interval_records`.

The attempt is single-flight per handle. A failure cannot change the already-published commit report;
ordinary maintenance failures emit `checkpoint.auto_failed`, remain pending even when checkpoint state
was published before a later recycle/index-refresh failure, and retry after the next successful write.
Read-only, read and empty-write commits never enter the policy. Regressions in
`tests/api/test_auto_checkpoint.py` cover the exact threshold, concurrent/re-entrant attempts, failed
and late-failed retry, hostile diagnostics/events, process-control identity, close re-entry, real WAL
recycling, verification and reopen.

Related, and to be decided with it: `QuarantineStore.restore`'s `PROTECTED_PREFIXES` covers `index/`
but not `wal/`, so a whole-segment quarantine entry could be restored into a freed `wal/NNN.wal`.
C6-owned, no caller in `src/` today.

### D5 durable_commit — MEASURED. Do not amend the ceiling. (coordinator-commissioned profile)

**The 234 ms is not fsync.** A bare `os.fsync` on this volume is 0.29 ms; the one barrier §8.5 step 3.5
requires measures **0.76 ms -- 0.2% of the commit**. Durability is not the cost.

| phase | excl ms | share |
|---|---|---|
| `crc32c` (pure Python, 95 calls) | 224.2 | **59.4%** |
| `atomic_replace` (4 control-file publications) | 100.5 | **26.6%** |
| all 9 fsyncs | 11.1 | 2.9% |
| every other pure-Python engine frame | ~9 | ~2.4% |

**Root cause 1 -- the WAL has no LSN index, and OCC runs twice.** `WalManager.read_from(lsn)`
(`wal_manager.py:881`) calls `_walk_registered()`, which walks every segment **from byte 0**, decodes
and CRC-checks **every record in the log**, then filters `record.lsn >= start`. `_find_conflict` drives
it, and §8.5 step 3.3 executes **twice per commit** (`txn_manager.py:629` and `:634`).

**The 234 ms is a point on a rising line, not a stable cost.** OCC CRC bytes grow 84,266 -> 571,814 over
30 samples -- exactly 2 x one commit's 8,417-byte WAL footprint per commit -- and sample wall time rises
monotonically **207 ms -> 871 ms**. The measured 109x and 123x are both readings of a quadratic.

**Root cause 2 -- pure-Python CRC-32C at 1.3 MiB/s** (`domain/page/checksum.py:60`, a byte-at-a-time
table reduction in a Python `for` loop). `zlib.crc32` does the same shape at 239.8 MiB/s: **184x**.
The commit checksums **377,582 bytes to write 16,846** -- 22x more than it writes.

**Ranked, all measured, none a protocol change:**

| # | change | owner | effect |
|---|---|---|---|
| 1 | index the WAL by LSN so `read_from` starts where asked | **C4** | **1.9-2.1x**, and kills the quadratic |
| 2 | native CRC-32C behind the port D2 already allows | **C1** + C2 adapter | **2.5x**, and flattens the curve |
| 3 | call `_find_conflict` once, not twice | C5 | 1.2x; dominated by #1 |
| 4 | hold the writer lease across commits instead of acquire+release per commit | **C5** | ~1.6x of the residual; removes 4 of 9 fsyncs |
| 5 | reuse one reader registration per manager instead of one per `begin` | C5/C3 | ~1.25x of the residual |
| 6 | drop the redundant temp-file fsync in `_publish_once` / `_publish` | C3/C2 | halves control fsyncs 8 -> 4 |
| 7 | cache the directory listing in `_resolve_identity` (69 `os.listdir` per commit) | C2 | ~1.04x |
| **9** | **group commit** | C5 + a §8.5 amendment | **~1.002x -- negligible** |

#1+#2 measured **2.3-2.6x combined**, taking ~110x to roughly **45x**; +#4/#5 to **15-20x**; +#6/#7 to
**8-12x** on contended hardware, better on a quiet machine. **A74 and §8.5 step 2 require validating the
epoch before any device call and require the granting coordinator -- neither requires acquiring the
lease per commit.** That placement is a C5 implementation choice, not frozen text.

**Group commit is explicitly rejected**: it amortises the WAL barrier, which is 0.76 ms of 377 ms, and
it cannot be done under FROZEN §8.5 without changing what `CommitReport.csn` means for each member of a
batch. Do not amend §8.5 for a 0.2% cost.

**Verdict: the ceiling stands. The 234 ms is a missing index, a missing native checksum, and four
control-file publications where the protocol needs one.** Write amplification is 1,404x (a full 8 KiB
page image logged *and* written per 12-byte row), which is what sets the walk's growth rate; byte-level
redo is item 8 and a format change, deferred.

Load disclosure: measured under heavy contention (absolute medians 280-545 ms where the bench read
234 ms). Every *share* is a within-run decomposition, which is the load-robust part; counterfactuals
were interleaved with unmodified re-runs in the same session.

### D5 item 1 — WAL LSN index LANDED and verified by the coordinator

`read_from` now enters at the record it was asked for, via a strided mark index
(`LSN_INDEX_STRIDE_BYTES`, `_note_mark`, `_read_plan`). Measured on the shared tree:

```
records in log   read_from(last) decodes
    100                16
    600                12
   1000                 3
   2000                17          <- bounded by the stride, NOT by log size
```

Commit drift over 35 warm commits fell from the profile's **4.2x** (207 -> 871 ms) to **1.22x**.
The quadratic is gone. Absolute commit median ~161 ms, and the profile has changed shape:

| | before | now |
|---|---|---|
| `crc32c` calls per commit | 95 | **28** |
| `_windows_posix_replace` per commit | 6 | 4 |

**Remaining D5 cost, re-attributed from a fresh profile of 20 commits:**
- `_windows_posix_replace` **61 ms/commit** over 4 calls (~15 ms each, against ~3.4 ms for plain
  `os.replace`) -- control-file publication. Item 4 (hold the writer lease across commits) removes
  2 of the 4 and 4 of the 9 fsyncs. **Owner C5. Largest single remaining win.**
- `crc32c_reference` **49 ms/commit** over 28 calls -- still the pure-Python implementation.
  Item 2. **Owner C1.**

BD-1 (`removed_records` negative) and BD-2 (segment-name reuse) are both fixed in the code. BD-1 had
**no proving test**; the coordinator added `test_removed_records_is_the_drop_the_log_actually_took`
(4 params) and verified it load-bearing: against the pre-fix shape it fails 4/4 while **the entire
275-test wal suite passes**. Fork restored, sha verified.

### E3 — no commit ever populated any index (CLOSED by the coordinator)

Found by C9's blind critic while isolating the empty-similarity-search defect, and correctly
attributed AWAY from C9: `IndexManager.stage_row_insert` / `stage_row_update` / `stage_row_delete`
had **zero callers anywhere in `src/`**. A commit wrote rows to the heap (`_write_rows`) and applied
whatever had been staged into the indexes (`_apply_index_changes`); nothing between the two ever
staged anything.

**Not a vector defect.** The critic proved it framework-wide with a non-vector control: an ordinary
`HashIndex` registered on the same table over the same rows through the same public write door was
equally empty. Every index in the product stayed empty for ever, reporting `stale=False` and a
correct `live_count` while a lookup answered nothing and a similarity search returned no hits for
rows a `MATCH` plainly returned -- `regime='exact'`, where recall is 1.0 by construction.

**The fix, in `txn_manager`.** `_write_rows` now carries the table and both value tuples on each
`_RowWrite` (the old values read from the heap, which is the authority inside the commit section),
and `_stage_index_changes` stages delete-then-insert on every covering index.

The csn circularity is broken by counting rather than by a provisional stamp: the commit number an
index change carries is the sequence number of the COMMIT record, which depends on the batch length,
which these records lengthen. `_index_record_count` computes how many will be produced, the predicted
number accounts for them, and `_stage_index_changes` re-counts what it actually staged and **refuses**
when the two disagree -- so the one invariant kept in two places (A66) cannot drift silently.

**A second defect surfaced only once indexes had entries.** `Verifier`'s coverage walk called
`index_key(values, positions)` directly -- the COLUMN derivation -- while a vector index keys on a
digest of the embedding (`vector_digest_v1`). It therefore computed a key no entry could match and
reported every live row as missing an entry it in fact had: a false `corruption` verdict on a correct
database, which is what A11-revised exists to prevent. Now `_expected_key` asks the definition, which
is the same call the staging path makes, so the two cannot drift. The walk's own docstring already
said guessing the key would be "inventing an oracle rather than using one"; calling `index_key`
directly WAS the guess.

**Measured after the fix**, through `connect()`: a hash index goes 0 -> 2 entries on two inserts,
2 -> 3 on an update (old entry ended, new one created), and stays at 3 with the live count falling to
1 on a delete (an EXACT index returns a superset, so a delete ends rather than removes). A similarity
search returns `achieved_k=3`, hits `[1, 3, 2]`, `verify("all") == ()` before and after a cold reopen.

**Counterfactual: with the seam reverted, 1,615 tests across `query`, `txn`, `index`, `vector` and
`recovery` pass GREEN.** Three new tests in `tests/query/test_lifecycle.py` are the only things in
the repository that see it.

### E4 — a MATCH inside the transaction that created a row cannot bind it (known limitation, by design)

Found by the coordinator while driving traversal: `CREATE (:P {id:1})` followed in the SAME
transaction by `MATCH (a:P {id:1}) ... CREATE (a)-[:R]->(b)` creates no edge, and
`MATCH (p:P {id:1}) RETURN p` returns nothing, until the transaction commits.

This follows from W5b: a row's RecordId is allocated by the commit, so a staged row has no identity
a NodeScan could bind and no identity an edge could reference. C10 already refuses an edge to a node
the same STATEMENT creates, with a remedy ("create the nodes first and match them"); MERGE consults
the staged rows explicitly (`_uncommitted_rows`) so it matches its own work. NodeScan does not, and
answers silently.

openCypher / Kuzu read-your-own-writes would have the MATCH see the row. Closing this means one of:
(a) allocating the RecordId at staging time (W5b reversed -- an abandoned commit then burns an id,
which W5b judged acceptable as a GAP but not as a REUSE, so the allocator would have to be durable
before the commit), or (b) synthesising pending RowBindings from staged intents in NodeScan with a
provisional identity and rewriting them at commit. Both are a W6 design decision, not a patch.

Recorded here because the silent empty answer is the worse half; the remedy for a caller is to
commit the rows before matching them, which every test and every example in this repository does.
Routed to W6 as a decision; the CLI and README must state it.

### Coordinator closures after the method change (all verified by counterfactual)

| item | closure | proving test | counterfactual |
|---|---|---|---|
| **PRIMARY KEY uniqueness** (blocking, C10 critic) | `_require_unique_primary_key` at materialisation: snapshot + this txn's staged rows minus its deletes; the CONCURRENT case is step 3.3 (both commits write the partition the key hashes to). Refuses `GrafxQueryError` `constraint=primary_key`. | 4 tests in `test_lifecycle.py` + 1 in `test_write_preparation.py`; measured 2 OS processes at one snapshot -> one `COMMITTED`, one `GrafxWriteConflict`, one row | -- |
| **Traversal** (functional gap) | `_traverse`: walk over `_from`/`_to` under the snapshot; edge followed only when the snapshot sees edge AND landing node; relationship isomorphism (no edge reused on a path); single hop binds the edge, a range binds the tuple; a bound target is a filter | 8 tests in `test_lifecycle.py` (out/in/undirected, property, `*2`, `*1..3`, cycle `*3` vs `*4`, bound target, deleted endpoint, isolated node) | the obsolete refusal test removed; its subject no longer exists |
| **CF-11 log reclamation + checkpoint** | `TransactionManager.checkpoint()` under the commit section: **redo the log onto the device from the old checkpoint** (the P4 window -- logged and barriered, page write refused -- would otherwise lose its only copy), `pool.checkpoint()`, publish `checkpoint_lsn = last_committed`, then `wal.recycle(horizon)`. `Database.checkpoint()` is the public door; read-only refuses. | `tests/api/test_checkpoint_and_reclamation.py` (4: 20 segments -> 1, idempotent, read-only refuses, B killed unflushed after A reclaimed -> 30/30 rows); `test_wal_integration.py::test_a_checkpoint_installs_a_durable_commit_whose_pages_never_reached_the_device` | **without redo: `assert 0 == 3`** -- page never on device, segment gone |
| **C9 B1/B2/B3** | `fsum` third exit typed; `_require_unit_length` guarded; graph build discards on ANY refusal | 3 tests in `test_overflow_and_ordering.py` | 3/3 fail reverted; 301 others green |
| **E3 index seam** | see above | 3 tests | 1,615 green without it |
| **D5 item 2** | `install_checksum` in bootstrap; `checksum` selector `auto/pure/native`; `accel` extra declares `google-crc32c` | 5 tests in `test_bootstrap.py` + 1 in `test_packaging.py` | -- |

**Known limitation recorded, not fixed (E4):** a MATCH inside the transaction that created a row
cannot bind it (RecordId is allocated by the commit, W5b). The remedy is to commit before matching.
Routed to W6 as a design decision.

**Still open, performance only (D5):** commit ~161 ms. `_windows_posix_replace` 4/commit (the
`retain_lease` option now exists in C5 -- measure it on), pure CRC when no provider is installed,
69 `os.listdir` per commit in `_resolve_identity`. None is a protocol change.

### D5 item 4 — retaining the writer lease: MEASURED, NOT OFFERED (needs a yield protocol in C3)

`TransactionManager(retain_lease=True)` exists (C5). The coordinator wired it through `connect()` as
`retain_writer_lease`, measured it, and took it back out. Numbers, interleaved runs, one writer:

```
retain off : median 187.84 / 173.90 ms
retain on  : median 122.60 / 115.64 ms      -> ~1.5x on the whole commit
```

And the cost, measured with a second process: while a retaining holder commits continuously, another
writer's first commit returns **after 5.3 s** -- it must wait for the lease to be taken over on C3's
schedule (`lease_ttl_seconds`) instead of finding it free. Two retaining writers interleaving ten rows
each did not finish inside the suite's timeout. Not starvation (BR-6 holds: nobody is *refused* for
another writer existing) but a takeover latency of one TTL per contended commit, which in a library
whose reason to exist is multi-process writing is a trap, not an option.

**What would make it offerable:** the holder yields when a waiter exists. C3 would have to expose
"someone is waiting for the lease" cheaply (a waiter file or a flag in the lease record), and the
retaining holder would release at its next commit boundary when that flag is up. That is a C3+C5
seam for W6. Until then the internal option stays internal and the default lease-per-commit stands.

Remaining D5 levers that are NOT policy: the pure-Python CRC when no provider is installed (the
adapter is wired; install `okto-grafx[accel]`), and 69 `os.listdir` per commit in
`storage_local._resolve_identity` (C2, ~1.04x).

### Round-2 blind reviews of the coordinator's own work — C5 (4 blockers), C10 (6), C9 (1): ALL CLOSED

Every fix carries a proving test whose counterfactual was run in a private fork with the fix reverted.

**C5** (`txn_manager.py`, `buffer_pool.py`): B1 a refusal inside `_write_rows` now abandons the
intents already written (the frame that wrote them is the one that abandons them); B2 `BufferPool.
discard` drops an abandoned attempt's dirty frames WITHOUT write-back (a pinned frame is marked clean
instead) -- the stale picture can no longer land over another participant's committed page; B3 a
refusal after index staging unstages (`_unstage_index_changes`: trims `pending_records`, drops the
index staging) so a re-commit carries one set with the log's number; B4 `_require_log_retains_from`
refuses optimistic validation (retryable) when the log has recycled the records above the snapshot.
Tests: `tests/txn/test_round_two_regressions.py` (4) -- with all four reverted, 4/4 fail and the rest
of `tests/txn` passes. The critic's own four attack drivers re-run against the fixed tree: PHANTOM
gone (both scenarios), LOST ROWS none, index recommit 9 records / 0 csn mismatches, pruned-pin commit
refused.

**C10** (`query_engine.py`, `planner.py`, `parser.py`): B1 target-node inline properties become
filter terms above the traversal; B2 a key-changing SET declares the partitions of the keys the row
HAD (`hold_update(previous_keys=)`); B3 `_node_scan` skips rows this transaction ended, and C5's
settle treats a delete as terminal (second line); B4 `_write_assignments` keyed by the stored ROW,
building on this transaction's latest version; B5 one ordered `_transaction_row_view` answers "what
exists" for MERGE, the key check, `_current_values` and `_uncommitted_rows` -- replaced versions are
not rows, ended rows are not rows, and a SET on a row the same statement created rewrites the held
insert; B6 `*0` refused at parse. Tests: `tests/query/test_round_two_regressions.py` (11) + parser
(3) -- with all reverted, 10/10 positive tests fail and the rest of `tests/query` passes. The
critic's 13-check reproduction: 12 PASS, the 13th ends in the parse refusal its text accepts.

**C9** (`vector_engine.py`): B4 the WARM path (`_note` -> `_install`) discards the graph on ANY
refusal, not only the late one -- `_install_fresh` wrapped. Test
`test_a_refusal_on_a_WARM_graph_discards_it_too`; reverted: `assert HnswGraph(nodes=5) is None`.

Punch list from the reviews recorded in PUNCHLIST.md (segment-roll stamp = COMMIT-1; BR-6 for rows
via the heap header page; relationship DELETE unsupported; RETURN after SET projects pre-SET values;
non-DETACH DELETE leaves orphan edges; `SET` on `_from`/`_to` accepted; undirected self-loop twice).

### CF-12 — a long-lived participant never saw another process's commits (C2; CLOSED)

Found by C9's round-3 blind critic while chasing a vector defect, and it is the most serious finding
of the build: `LocalStorageDevice` cached the descriptor of every file it had opened, and another
process's `atomic_replace` (the CF-5 POSIX-semantics rename) moved the directory entry from under it.
The cached handle kept reading the REPLACED file. A participant that had once read
`control/commit.state` kept reading the number it saw first, never learned of anyone else's commits,
answered stale rows for ever, and had every commit of its own refused as a write conflict with a
world it could not see. A fresh process saw everything -- which is why every multi-process test in
the repository, all of which used a fresh process for the read, passed (L23/L24: the safe regime).

Measured before the fix, one parent process and one child: parent `published=5`, child publishes 17,
parent still `[(1,)]` / `published=5`, parent commit `GrafxWriteConflict`, fresh process `[1..5]`.

**Fix (C2):** `_descriptor` re-checks, on every cached hit, that the name still names the file the
descriptor holds -- identity `(st_dev, st_ino)` from `os.stat(path)` vs `os.fstat(fd)`, which on
Windows is the volume serial and the 64-bit file index -- and closes and reopens when it does not.
CF-5's rename was only ever half of publication; this is the reader's half.

Four adapter tests had pinned the defect as the property ("the reader that opened the file before it
happened keeps reading what it opened"): rewritten to assert the published content. One test asserted
an unlinked file still readable through the held descriptor: now refuses missing, which is the same
honesty. New: `tests/api/test_cross_process_visibility.py` (fails with the check reverted).

### C9 round 3 — B5 and B6 CLOSED (coordinator)

**B5 (threads):** a traversal could reach a node whose entry was not registered yet (`graph.insert`
links before the maps were written), and a graph discarded under a search in flight replaced the maps
the traversal was reading: bare `KeyError` out of the §8.8 door, 1 in 173 answers under two readers
and one writer. Now: the entry and record are registered BEFORE the node becomes reachable; a search
fixes its picture (graph + maps) at its start and finishes on it; `invalidate_graph` replaces the maps
rather than clearing them; a REMOVE discards the graph instead of editing it under a traversal.
Tests: `tests/api/test_vector_concurrency.py::test_a_search_never_escapes_while_another_thread_commits_vectors`
(probabilistic net, 2 readers + 1 writer, 8 s) and the deterministic
`test_a_search_finishes_on_the_picture_it_started_with_when_the_graph_is_discarded_under_it`
(the host's own filter discards the graph mid-traversal; reverted: `KeyError: 1`).

**B6 (processes):** the warm graph's only invalidation signals were this process's own commits --
L22 again. Now the graph records the index header's `built_through_lsn` at build and after each local
commit/redo, and a search compares it with the header on the device (fresh after `begin()`'s read
view): a foreign commit that touched the index moves it, the graph is discarded and rebuilt. Test:
`test_a_warm_graph_learns_what_another_process_committed` (child inserts 8 / deletes 4; reverted: the
approximate regime returns deleted row 2). Unmasked by CF-12 (the descriptor identity fix in C2).

### C10 round 3 — B1, B2, R01 CLOSED (coordinator)

**B1**: a refusal INSIDE `release()` (a DELETE of a row the same statement created reached the
transaction's door with no reference) left the statement's other rows staged, and a commit made half
a statement durable. Three fixes: a DELETE of a statement-created row takes the held insert back (net
no-op, Cypher's reading); `release()` is all-or-nothing on the transaction (`staging_mark` /
`discard_since` around the handover); a MERGE-matched row this transaction UPDATED comes back WITH its
reference (the latest values on a real `RowBinding`) so later clauses work. A DELETE of a row created
by an EARLIER statement refuses with a typed error naming W5b (E4's family).

**B2**: the held insert was identified by VALUES; two created rows with identical values (a table
without a primary key) sent a second SET clause to the wrong row. Now each held insert carries a token
tied to the pending binding's version object; `_rewrite_held_insert`, `_current_values` and the
pending DELETE all resolve by token.

**R01**: the key check inside the rewrite is pinned (`MERGE (a {id:1}) SET a.id = 5` with 5 taken).

Tests: 7 new in `tests/query/test_round_two_regressions.py` (+ the read-side ended-row test pinning
N01/E01 independently of C5's settle); counterfactual with all five fixes reverted: 6/6 fail. The
critic's rewrite probe: 9/10 (the 10th is a cross-statement MERGE-SET on an earlier-created row, a
typed refusal recorded under E4).

### CF-13 — the buffer pool was not safe across the threads of one participant (C1; CLOSED)

Found by the coordinator's own thread-concurrency test under suite load, and confirmed by
experiment: readers on a SECOND handle -> 0 findings; no readers -> 0; readers on the SAME handle
-> `index_entry_missing` in 2 of 2 runs, with no refusal anywhere. The participant section IS
thread-exclusive (measured), so `begin()` and a commit never interleave -- but searches and scans pin
pages OUTSIDE any section, and the pool's doors were sequences of dictionary steps that were
individually atomic and jointly not: a reader's eviction between a writer's lookup and its pin
handed the same page out twice as two objects; the writer's entry landed in the orphan and was
never written back. Silent loss.

**Fix (C1 + C11):** `BufferPool(guard=...)` -- a re-entrant lock INJECTED by the composition root
(the pure core imports no mechanism; the default is a no-op context), every door decorated
`@_guarded`; `pinned()` runs its body outside the guard (A91). Measured: 0 findings in 3/3 runs.

**Second half, surfaced by the first:** with the guard in place a reader's pin made the writer's
`begin()` REFUSE (`begin_read_view` -> `invalidate` -> "pinned and cannot be invalidated"). A read
view now DOOMS a pinned frame instead: it leaves the table so the next pin reads the device, the
holder releases exactly the object it pinned (`unpin(page=)`), the last release drops it
unwritten; explicit `invalidate()` keeps refusing. Tests: `test_every_door_of_the_pool_runs_under_
the_injected_guard`, `test_a_read_view_dooms_a_pinned_frame_instead_of_refusing_the_begin`, and the
probabilistic `tests/api/test_vector_concurrency.py` thread test (now with the writer's refusals
recorded in the assertion message).

Also closed on the way: the P4 window's INDEX half -- a commit whose apply fails after the barrier
is REDONE from the log (`_recover_post_barrier`: drop the index staging, redo WRITE_PAGE and
INDEX_WRITE records of this commit through the idempotent doors, publish; failing that, mark the
covering indexes stale). Test `test_a_commit_whose_index_apply_fails_after_the_barrier_is_redone_
from_the_log`; reverted: `assert 0 == 2`.

### CF-14 — a heap append relinked a page that the log, the interest set and the abandonment all missed (C1/C5; CLOSED)

Found by a real multi-process smoke test, not by the suite. Three processes appending to one table
through `connect()` left the heap PERMANENTLY unreadable in under a minute: `MATCH (i:Item) RETURN
count(*)` in a fresh process -- after every writer had exited cleanly and recovery had replayed --
refused with `corruption_detected` naming a page "reachable from a chain but has never been
written". Nothing had crashed. Two of three writers died mid-run; 0 of 576 acknowledged rows were
reachable. **7858 tests were green.**

**Discriminated by experiment** (four arms, same workload, one variable each). These figures and
the "2 of 3 writers died" one below were measured with scratch scripts against code that predates
this fix, so they are UNVERIFIABLE by a later reader -- kept because they are how the defect was
found and what its shape was, not as a claim anyone can re-run. The re-runnable evidence is
`tests/smoke/test_concurrent_writers.py` and the figures in PUNCHLIST.md, which name the tree state
each was taken against (L31):

| arm | writers died | rows readable | findings |
|---|---|---|---|
| 3 writers, one shared table | 2 of 3 | 0 of 576 | 10 |
| 3 writers, one table each | 0 | 576/576 | 24 |
| 3 writers + forced contention | 0 | all | 0 |
| 1 writer | 0 | all | 0 |

Contention HIDES it: a shared row serialises the writers and the window closes, which is why the
arm with the most conflicts was the clean one. Two hypotheses were refuted before the cause was
found -- "a refused transaction leaks its allocation" (84 conflicts, 0 findings) and "a committed
page is not on the device" (`_apply_images` already flushes for exactly that reason, and a byte-level
probe found no unwritten page). The cause came from a forensic dump of the damaged file: the chain
read `1→3→5→8→10→13→15→17→**18**` with page 18 all zeros and page 19 written and orphaned.

**Cause, one omission with three consequences.** `_pages_touched_by` re-derives the pages a commit
changed from where its ROWS landed. `HeapStore._append` also relinks the previous last page --
`page.next_page = new_index`, the only thing that makes the new page reachable -- and no row lands
there. The same set feeds three jobs, so the link was never LOGGED (no redo could reproduce it),
never DECLARED (two commits could rewrite one tail page and neither conflict), and never UNDONE. The
third is what corrupted databases: a refused attempt left the pool holding a tail page pointing at
the page it had just abandoned, and the next commit of any participant flushed that link to the
device.

**Fix (C1 + C5):** the set is MEASURED, not re-derived. `BufferPool.dirty_pages()` reports what the
pool holds modified; the commit takes a mark before it writes anything and `_attempt_pages()` is the
difference. All three consumers take the union with it. An enumeration has to name every site that
touches a page and is short by one the day a site is added; a measurement cannot be short.

**Also closed:** the leak the same omission left behind. A refused append that had grown the file
abandoned that page for good (G6 forbids shrinking), at 10-24 all-zero pages a minute under three
writers, each a `page_unwritten` finding on a healthy database. `BufferPool` now reclaims a page it
grew the file for and never wrote back -- safe precisely because such a page has never been readable
by anybody, and no other participant can be given an index this one already has. Measured over six
runs: findings 10-24 -> 0 in five and 1 in the sixth; the heap for the same data 20-26 pages -> 15.
The residual (a process that abandons and then exits) is in PUNCHLIST.

**Round 2 -- the blind critic refuted the first fix's central claim, and it was right.** The
measurement was `dirty_pages() - mark`, read from the pool's still-dirty frames. A page the attempt
changed and the pool then EVICTED under budget pressure was written back and marked clean, so it
left the difference and the log with it -- the same consequence #1, still open, in exactly the
regime the docstring said could not happen. Measured by the critic: one commit of 400 rows against
a 256-frame budget replayed to **2 of 402 rows**, the rest in the log and unreachable. Not a
regression (the pre-change code lost them at every budget), but the register said CLOSED and the
code did not support it, which is the gap that made it a finding.

**Round-2 fix:** a write-back REMEMBERS the page instead of forgetting it. `BufferPool` keeps the
pages written back since `forget_modified()`, and `modified_pages()` returns those plus the frames
still dirty, so an eviction can no longer take a page out of the answer. The relink test is now a
matrix over buffer budgets (4, 5, 6, 8, 12, 64, 1024 frames): reverting just the line that
remembers kills it at 4, 5, 6, 8 and 12 and leaves 64 and 1024 green, which is the shape of the
defect and the reason a single-budget test could never see it.

**Also round 2, from the same review:** a write-back withdrew the page from `_grown` but not from
the reuse list, so a page whose image had become real could still be handed out and blanked -- the
reclaim-time safety check was never re-taken at hand-out time. Both lists are now cleared, and the
candidate is re-checked for pins when it is chosen. `grow_to`, `IndexStore._grow_buckets` and
`write_chain` now allocate with `reuse=False`: the first two wait on a FILE's length, and the third
builds a list of distinct pages the pool must not inject a duplicate into.

**Round 3 -- the blind critic found the UNDO half was a guard no test held.** Deleting
`touched.update(self._attempt_pages())` from `_abandon_rows` left the entire suite green, while an
ordinary caller reached `corruption_detected` in under a second. The reason every existing test
missed it, measured by instrumenting `_abandon_rows`: they all declare the SAME key partition, so
the refusal lands on the ROW half of validation, which runs BEFORE `_write_rows` -- nothing has been
appended, nothing relinked, and the batch handed to the undo is empty. Reaching the undo needs the
PAGE half, which needs two participants declaring DIFFERENT key partitions that still collide on a
page (any two row-writing commits do, because every one of them writes the table directory on heap
page 0). `test_a_refusal_on_the_PAGE_half_undoes_the_link_the_append_had_already_made` does that and
kills the mutant with the original corruption error. Two more guards from the same review are now
asserted by construction rather than by scheduling: page 0 is never offered for reuse, and
`reuse=False` never spends an abandoned page.

**Mutation battery (14.1.5), round 2/3 surface.** Baseline: full suite 7879 tests, 0 failures,
0 errors, 5 skipped. Run one mutant at a time against the tests named.

| mutation | verdict |
|---|---|
| `_write_back` no longer records the page in `_modified` | KILLED (relink matrix at 4, 5, 6, 8, 12 frames; 64 and 1024 survive, which is the defect's shape) |
| `_write_back` no longer withdraws from `_grown` | KILLED (`..._already_on_the_device_is_never_handed_out_again`) |
| `discard` no longer calls `_reclaim` | KILLED |
| `_attempt_pages` dropped from `_build_records` | KILLED |
| `forget_modified` call dropped | KILLED |
| `grow_to` loses `reuse=False` | KILLED (`...never_spends_a_page_it_meant_to_add`) |
| `allocate` ignores `reuse=False` | KILLED (two tests) |
| `_reclaim` loses the page-0 guard | KILLED (`...reserved_header_page_is_never_offered_for_reuse`) |
| `settle_abandoned` dropped from `close()` | KILLED 1 run in 6 by the smoke test -- probabilistic, recorded as such |
| **`_attempt_pages` dropped from `_abandon_rows`** | **SURVIVED in round 2; KILLED in round 3** |
| `_write_back` no longer withdraws from `_abandoned` | SURVIVES -- no production path reaches it (a page on the reuse list is not resident, so nothing writes it back); defensive, PUNCHLIST |
| `_reusable_index` pin guard | SURVIVES -- same, PUNCHLIST |
| `_grow_buckets` loses `reuse=False` | SURVIVES -- the loop still terminates and still reaches its target; costs a wasted page, PUNCHLIST |

**Round 4 -- the undo could not undo a page the attempt had already written.** `_abandon_rows`
takes a page back by restamping its rows invisible IN THE FRAME and then discarding the frame
unwritten, and both halves only work while the device has never seen the page. Round 2 taught the
measurement to include pages the pool had already written back -- correctly, for the LOG -- and the
undo then discarded those too, which throws away the corrected frame and leaves the attempt's bytes
on the device. Measured by the round-3 review at a 512-byte page size, one refused attempt of 60
rows:

| frames | outcome for a participant that did none of the writing |
|---|---|
| 4 to 58 | rows of the refused transaction are READABLE, and `verify()` reports clean |
| 59 to 62 | `corruption_detected`: the chain link was written and its target was not |
| 63 and above | correct -- nothing had been evicted, so nothing had escaped |

Not a regression (the pre-fix tree was worse at every budget), and blocking anyway for the reason
L31 states: the register said CLOSED without naming the regime, and three docstrings asserted the
undo was total.

**Round-4 fix:** the undo is ALL-OR-NOTHING at the device. If any page of the attempt reached the
device, every page of it is WRITTEN there in its restamped form -- a chain the device can walk whose
rows carry no commit number and are invisible to every snapshot, which is space leaked exactly like
the page an append abandons (G6) and never a row a reader can meet. If no page reached it, every
frame is dropped as before. The page-half test is now a matrix over eight budgets; reverting to the
old always-discard undo kills it at 4, 12, 24, 40, 59 and 62 and leaves 64 and 1024 green.

**Also round 4:** `_reusable_index` removing the candidate from the reuse list was a survivor whose
reversion hands one index to two owners -- reproduced through `connect()` with three writers as
`the page chain of table 'Item' returns to page 7`. Every reuse test took exactly ONE page from the
list, and so does the smoke workload, which is the regime that cannot see it. Two more declarations
that nothing held are now held directly: the relinked page being in the interest set, and the mark
subtraction that keeps the undo off a page the attempt never touched.

**Tests:** `tests/smoke/test_concurrent_writers.py` (symptom; fails without the fix with 10
`page_unwritten`; 7 consecutive green runs after the settle), `tests/txn/test_chain_relink_
regressions.py` (cause: nothing of a refused attempt left dirty; the relinked page is carried by the
log AT EVERY BUDGET; the abandoned page is handed out again; a page already on the device never is;
an abandoned page is settled as FREE; `grow_to` adds what it reports). Recorded honestly: one test
in that file passes WITHOUT the fix and says so in its own docstring -- scripting the interleaving
deterministically was attempted three times and did not reproduce it. LESSONS L29, L30.

### CF-15 — a complete index framework that nothing ever put an index in (C7/C10; CLOSED)

Found by a smoke test that would not finish: building a 2500-node graph with 5400 edges through
`connect()` never completed, and the reason was not the writing.

`CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))` registered NO index. `db.indexes.indexes()`
returned `()`, there is no `CREATE INDEX` in the grammar, and `_index_for` therefore searched an
empty list -- so `IndexSeek` existed in the planner and was never once chosen. Everything else was
built and tested: `HashIndex`, the EXACT contract of section 8.7, `IndexManager.lookup`'s heap
validation, `_index_definitions` feeding the planner, and the commit seam that populates indexes
(itself the subject of defect E3). The only missing piece was that nobody ever created a definition.

**Measured before, with the fixed per-call cost separated from the marginal cost:**

| rows | point read (marginal) | insert/row | one edge via a two-pattern MATCH |
|---|---|---|---|
| 200 | 5.97 ms | 16.1 ms | 292 ms |
| 800 | 25.4 ms | 32.5 ms | 587 ms |
| 3200 | 57.4 ms | 89.3 ms | 1953 ms |

Four times the data, 4.3 times the time: a scan. D5 sets the point-read ceiling at five times a
reference engine's ~0.9 ms on this platform, so the ceiling was missed by ~23x at 200 rows and ~78x
at 3200, **and by more the larger the table**. The uniqueness check on every insert scanned the same
way, which made a bulk load quadratic; `MATCH (a {...}), (b {...})` multiplied two scans.

**Fix.** `primary_key_index()` in C7 is the single factory, used by two callers for the two moments
a table's index has to exist: the DDL that declares the primary key, and the composition that
re-adopts what is on disk at the next open. One factory rather than an f-string at each site,
because the name is a FILE name -- two spellings would make the second open create an empty index
beside the first. The uniqueness check now asks the index rather than scanning. A relationship table
declares no key and gets none.

**Measured after, same machine, same script:**

| rows | point read | insert/row | one edge |
|---|---|---|---|
| 200 | 0.71 ms | 11.9 ms | 86.5 ms |
| 800 | 1.10 ms | 13.6 ms | 96.6 ms |
| 3200 | 1.96 ms | 14.1 ms | 98.1 ms |

Point read 29x faster at 3200 rows and now inside the D5 ceiling; insert 6.3x and no longer growing,
so a bulk load is linear; edge creation 20x and flat.

**The correctness rule that makes it safe, and it is not optional.** A STALE index is withheld from
the planner and from the uniqueness check. A stale index is a SUBSET of what the heap holds --
entries it never received -- and a subset is exactly what the EXACT contract cannot repair:
validating a candidate against the heap removes hits that should not be there and cannot invent ones
that are missing. A plan built on one answers a keyed read with fewer rows than exist, and a
uniqueness check built on one accepts a duplicate under a declared primary key. Both fall back to the
scan they did before any index existed: slower, and right. A database written before this change has
no index file, so its index is created empty over a populated heap, is therefore stale, and is
correctly not used until a rebuild.

**A pre-existing defect this exposed, and it was the more serious half.** An index went stale the
moment ANY commit happened after it was created and before it received its first entry -- and
`_advance` refuses to move a stale index, so nothing could ever lift it. "Create the schema in one
session, load the data in the next" therefore left an index that no amount of loading would repair
and only a rebuild could. Reproduced on the VECTOR index, which predates any of this work:

```
create table with a vector column, close, reopen -> stale: ('vector_V_s',)
insert rows                                      -> stale: ('vector_V_s',)   # never clears
```

`IndexManager.commit` now advances an index whose table this transaction wrote NO row of: such a
commit had nothing to stage for it because there was nothing to stage, so it has seen everything
through that position. The condition is narrow on purpose -- an index whose table WAS written and
which staged nothing is the shape of defect E3, and that case still goes stale, which is the alarm
that found E3.

**Tests:** `tests/query/test_primary_key_index.py` (10: the index exists and covers the right
column; a rel table gets none; a keyed read plans a seek and an unkeyed one a scan; seek and scan
return the same rows; a deleted row is not returned; an updated row moves keys; the duplicate-key
refusal survives the index answering it; the index comes back fresh across two reopens and a write;
a stale index is never planned and the answer stays right; a stale index does not let a duplicate
through). Seven existing tests that named the vector index as the only index there could be were
widened, not filtered: each expectation GAINED the primary-key index, so each still fails if an
index nobody asked for appears.

**Cost, measured after the machine was free:** 572 s before this change, 870 s with a blind-critic
agent sharing the box, **610 s** on the same tree with nothing else running. So index maintenance on
every commit costs about **6.6%** of the suite, and the 52% figure an earlier draft of this entry
carried was almost entirely the other agent. Recorded because it is the kind of number that gets
quoted: a measurement taken on a loaded machine is a measurement of the load.

### CF-15 round 2 — the index could refuse a DDL, and then refuse every later open (C7/C10; CLOSED)

CF-15 above was recorded as CLOSED and was not. A blind review of `3f0a10e` found two blocking
defects, both reachable through `connect()` alone -- no concurrency, no fault injection.

**1. A DDL that could not create its index bricked the database.** `catalog.add_table` mutates the
live catalog BEFORE the attach runs, so a refusal there left the table INSTALLED and the statement
REFUSED. The next committed schema change wrote that table to disk, and the re-adoption at every
later open raised the same refusal: every row unreachable through the only door there is. Two
ordinary inputs reach it -- a table name of 126 characters or more (`pk_` plus 126 exceeds the
128-character identifier budget), and two tables whose names differ only by case, which the catalog
accepts as two tables and which fold to one index file name. **A regression introduced by CF-15**:
the parent commit accepts both and reopens.

**2. `_published_lsn_for_new_index` could only ever return 0.** It tested `callable()` on
`IndexManager.published_lsn`, which is a PROPERTY, so the test was always False and
`advance_built_through(0)` was a no-op. Every table declared in a session after the first got an
index that registered behind the published position, was marked stale on the spot, and could never
be lifted -- `_advance` refuses to move a stale index. **Exactly the failure the code's own
docstring says it prevents.** Answers stayed correct, because a stale index is withheld and the
query falls back to a scan, so the only symptom was that the feature silently did nothing for that
entire regime.

**Why the tests missed both.** Every test of the feature created its table in the FIRST session,
where the published position is 0 and defect 2 cannot show; none used a table name near the
identifier budget or two names differing by case. L24/L30 again.

**Fix.** Defect 1 by the rule the code's own docstring already stated and the code did not honour:
an index is an ACCELERATOR, not a semantic -- it may not fail a statement and may never make a
database unopenable. It declines instead, the table works with a scan, and the name is reported
through `QueryEngine.skipped_indexes`. Defect 2 by reading the property, and by moving the advance
inside `IndexManager.register` via `complete_through=` so it lands BEFORE freshness is judged; an
advance after the check is a no-op, which is why the first attempt at this fix did not work.

**Tests:** three regressions that fail against `3f0a10e` and pass now (`..._leaves_no_room_for_an_
index_name_is_still_created`, `..._differing_only_by_case_do_not_brick_the_database`,
`..._declared_in_a_later_session_gets_a_FRESH_index`), plus one guard that passes both ways and
says so (`..._every_primary_keyed_table_gets_its_index_back_on_reopen`, which kills the surviving
mutant "re-adopt only the first table's index").

**Two corrections to what was written about round 1**, recorded because the register is read as
fact:

* The commit message for `b94a4cf` says "Five regressions added". It is **four**, and one of them
  is a guard rather than a regression.
* The commit message for `3f0a10e` says each of the seven edited tests "still fails if an index
  nobody asked for appears". Measured by the review: **six of seven**.
  `tests/query/test_lifecycle.py::test_verify_keys_a_row_the_way_the_index_it_checks_keys_it` now
  looks the index up by name, and a lookup by name cannot notice an extra index. The change is the
  better test of the vector index's key derivation; the claim about it was wrong.
* The docstring of `_rows_carrying_key` claimed "the catalog is what refuses" two table names
  differing only by case. Measured: it does not. That false claim is what made defect 1 look
  impossible.

### CF-16 — a schema change was not a transaction (C10, owed since the C1 scope cut; CLOSED)

The DDL path called `CatalogStore.save()` -- which writes through the buffer pool the moment it is
called -- and mutated the LIVE catalog at statement time. The staged door `stage()` had been built
and documented one round earlier and nothing walked through it; the punch list recorded the defect
as "still live end to end". Measured through `connect()` alone before fixing:

* **A rolled-back CREATE TABLE survived a REOPEN.** The rollback dropped the transaction's records,
  but the pool held save()'s pages and the next flush carried them to the device: an uncommitted
  schema change made durable, `verify()` clean. In-session, the retry was refused with "already has
  a table named", and the DDL's index registration stayed registered.
* **A committed CREATE TABLE's file header reached the device outside every log record** -- save()
  returns the chain pages only, so page 0 (root_page, payload_length) was never staged and a crash
  between the barrier and the flush lost the schema, unreachable to redo.

**Fix.** Every schema statement builds on a per-transaction WORKING COPY of the catalog
(`QueryEngine._working_catalog`), stages the images `stage()` returns -- chain, freed pages, and
page 0, unconditionally -- and the live catalog learns about the change when the commit applies
them. Planning and execution INSIDE the declaring transaction resolve names from the working copy
(the planner, the row materialiser's vector-space lookup, and the traversal's endpoint tables), so
the quick start's one-block schema still works. A rollback drops the copy and prunes the two side
effects DDL makes outside the transaction: registered indexes (by table id, against the live
catalog a rollback never touched) and the vector engine's per-space map (`discard_unknown`, a new
door on C9's enumerated surface).

**Behavior change, deliberate and recorded:** a table is visible to OTHER transactions only once
its declaring transaction commits. Statement-time visibility was not a feature; it was the leak.

**Tests:** `tests/query/test_schema_transactionality.py` -- six, and measured by the round-6
review ALL SIX fail against the pre-fix tree (the compatibility guard too, through its reopen
assertion on the endpoint indexes); five were written as regressions and one as a guard, and the
guard's docstring still says so. Originally recorded as: six, five failing against the pre-fix
tree (rollback leaves nothing anywhere; the vector map is pruned; an uncommitted table is invisible
to others; the catalog HEADER page is in the commit's WAL batch; two schema transactions allocate
distinct ids), one compatibility guard that passes both ways and says so (the one-block
schema-and-rows contract). The five statement-time tests of `test_query_engine.py` were rewritten
to the staged contract -- each now also asserts the live catalog does NOT hold the table before the
images apply -- and the harness double gained the real context's doors (`page_images` +
`staged_pages()`), which L12 requires and the old double did not have.

### CF-17 — traversal read every edge of the table, per frontier node (C7/C10; CLOSED)

After CF-15, the slowest thing left in a real graph: a reverse hop into a well-referenced entity of
a 2500-node graph read all 3600 edges and cost 1.46 s (1.83 s on the CF-17 rig before the fix). A
stored relationship row leads with its endpoints (W5c), so "the edges leaving this node" is exactly
what an EXACT index over stored positions 0 and 1 answers.

**Fix.** Every `CREATE REL TABLE` gets two endpoint indexes (`ef_<T>` over `_from`, `et_<T>` over
`_to`), created by the DDL, re-adopted at open, populated by the same commit seam every index uses,
and DECLINING rather than failing a statement -- the same three rules as the primary key's index.
The traversal expands a frontier node through them, with two deliberate qualifications:

* **A fan limit** (`_EDGE_LOOKUP_FAN_LIMIT = 64` distinct start nodes) past which one grouped edge
  scan takes over. The limit is a cost model, not a hedge: a lookup costs a few bucket probes
  however large the edge table, so a bounded frontier wins by index; a whole-table frontier pays
  one lookup per node, which past a point costs more than reading the edges ONCE -- measured, a
  scan-shaped plan paid 1800 lookups for 1.83 s where the grouped scan pays 221 ms.
* **A stale endpoint index is never consulted** -- same subset argument as the planner's rule --
  and the fallback groups the scan by endpoint, so even the no-index path stopped being
  O(frontier x edges).

**Measured, same rig, same query** (`MATCH (c:C)-[:M]->(e:E {id: 11})`, 1800 nodes, 3600 edges):
1834 ms before, **221 ms** after; forward hop from one node 29 ms; two hops out and back 56 ms.
Index-vs-scan equality asserted by test on every shape.

**What remains, recorded not hidden:** the landing of a traversal with a FREE target is resolved by
one scan of the landing table per traversal (edges store record identities, and no identity index
exists); and the planner does not reorder a pattern to start from its seekable side, so
`MATCH (c)-[:M]->(e {id: k})` still walks from `c`. Both in PUNCHLIST as the next levers.

### CF-18 — round-6 reviews: the schema statement did not hold until complete, and a hostile
### metrics sink could turn a durable commit into a reported failure (C10/C5/C8; CLOSED)

Two blind critics ran against `9348a2b` — one over the production delta, one auditing C8/C12/C13
for sign-off. Three blocking defects between them, all closed here.

**R5-B1 (C10).** A refused schema STATEMENT left the transaction poisoned: `add_table` mutated the
remembered working copy in place, and the vector attach — which has no decline guard by design —
raised after the mutation. Reproduced through `connect()` alone with two tables differing only by
case: the refused statement left the phantom in the copy, `CREATE (:person {id: 1})` was ACCEPTED,
and the commit made durable rows for a table no catalog would ever describe — unreachable,
unreported, `verify()` clean. **Fix:** the statement works on a CLONE adopted only at the end, and
every out-of-transaction effect is journalled — index registered, space attached, skip reported,
index FILE created — with a refusal replaying the journal in reverse. The journal is by NAME:
pruning by table id was tried first and fails exactly when it matters, because a loser's ids and
the winner's ids come from the same committed pages.

**R5-B2 (C10/C11).** `Database.retry()` never settled the conflicted loser, so the successor's
re-executed DDL was refused by its own predecessor's leftovers — first the registration, then the
orphan index FILE, whose digest can never match the successor's definition. The documented BR-6
retry loop could not succeed for a schema change, and continuing anyway reached R5-B1's orphan
rows. A third route: the read door (`db.execute("CREATE TABLE ...")`) passed the callable guard and
registered indexes BEFORE its refusal. **Fix:** `retry` settles the loser (its whole journal, files
included); the mode is refused up front; rollback replays the transaction journal — which also
closes the recorded "orphan index files are harmless" residue, whose harmless-ness was false.

**C8-B1 (C8/C5/C1).** A host-supplied metrics sink that raises — the registry validates shape, not
behaviour — escaped public doors raw, and raising on the POST-COMMIT gauge made a durably
committed transaction report failure; the caller's ordinary retry then duplicated the row.
**Fix:** `ContainedMetricsSink` wraps whatever sink the composition resolves: every recording door
absorbs what the inner sink raises (telemetry is never load-bearing — G7 freezes the catalogue,
not the delivery), and `publish` — the one door a caller acts on — stays typed. Measured: a sink
raising on every door, and one raising only after durability, both leave commits truthful and rows
single.

**Also landed from the same reviews:** `Database.unindexed_tables` is now real (it was referenced
in comments and existed nowhere — the open-time twin of `skipped_indexes`); `metrics="json"` now
actually writes its file, at `close()` (nothing ever called `publish()`, so the documented outcome
was unreachable and the flag silently inert); the decline guards accept the storage device's
case-collision refusal (`GrafxUnsupportedOperation`), which the pre-register file probe surfaced.

**Verdicts elsewhere in the same round:** **C13 SIGNED OFF** (skip-attribution gate proven both
ways, platform parity verified, battery lock exclusion and stale-break proven, ci.yml matches its
obligations, the D5 gate exercised at every exit). **C12: no code defect found** across ~70
subprocess invocations of every documented command — the one §14 gap is that its mutation battery
was never completed or reported (a mid-run "20/42" note is all that exists), which is recorded in
PUNCHLIST as an open obligation. **C8 otherwise sound** (catalogue exact both ways, disabled sink
allocation-free, OpenMetrics scrape strict-parsed clean, JSON rotation and typed refusals, events
sanitised and bounded under hostile payloads).

**A fleet report was voided, and the voiding is the record:** a background battery re-score and a
determinism probe were launched over the WORKING TREE while it was being edited. Every battery
verdict was the same collection error (the fork lacked `bench/` — a mutant that does not run is
not a kill, L7), and the determinism hash was the sha256 of EMPTY input (L28). Both debts are
re-run against an immutable `git archive` of this commit instead; L26's rule — instruments and
work must not share a mutable substrate — now includes the author's own background jobs.

### The round-6 verification fleet, completed against the archive of 51c469c

The voided report's three debts, discharged on an immutable `git archive` with a source-integrity
gate (every guarded file sha-checked against the archive before any mutant, and restored when a
kill left one mutated -- the gate fired four times).

**Determinism (14.1.3): HELD.** Five consecutive full-suite runs, every one `7950 tests,
0 failures, 0 errors, 5 skipped`, and the sha256 of the sorted test-id set identical across all
five (`8d62db0e...`), read from the junit files rather than from a pipe that can silently produce
nothing.

**Battery, full suite per mutant** (killer-set first -- a kill on a subset is final -- then two
half-suite runs for every survival claim):

| mutant | verdict |
|---|---|
| journal never replayed on rollback/retry | KILLED by `test_retry_settles_the_loser...` |
| working-copy clone skipped | KILLED by `test_a_refused_statement_poisons_nothing...` |
| metrics containment re-raises | KILLED by `test_a_post_commit_raise_cannot_make_a_durable...` |
| M05, M07, M15 (operator-door guards on `_tables_written_by`) | SURVIVED, full scope -- unreachable through `connect()`, recorded |
| M14/M32 (the staleness rule's narrowness) | SURVIVED, full scope -- the E3 counterfactual is the evidence, recorded |
| M17 (advance loses its flush) | SURVIVED, full scope -- costs a rebuild, never a wrong answer, recorded |
| M27/M30 (`_rows_carrying_key` guards) | SURVIVED, full scope -- recorded |
| N01/N03 (traversal never indexed / fan limit 1) | SURVIVED, full scope -- correctness-neutral by construction, the recorded usage gap |

Every round-6 blocking fix therefore has a mutant that dies in its own regression, and every
survivor is one already dispositioned in this punch list -- now confirmed against the whole suite
rather than the 2,402-test subset the round-4 report was limited to.

**C12's battery obligation: DISCHARGED, with its scope named.** Six mutants shifting each
contracted exit code (FINDINGS, USAGE, REFUSED, DAMAGED, RETRY, INCONCLUSIVE): **six of six
KILLED** by `tests/cli`. Together with the sign-off audit's ~70-invocation behavioural sweep over
every documented command and the 389-test suite, C12's sign-off is now earned rather than pending.
The scope is the exit-code mapping, not the whole CLI surface; the audit's behavioural sweep is
what covers the rest, and saying so is the difference between this record and the "20/42" note it
replaces.

### D5 durable_commit — 'consult JP' DISCHARGED: the ceiling stands, W6 owns the Windows gap

SPEC-M1 `fr_18f8eff7` anticipated this: *"se o multiplo de commit exceder 10x, o pipeline PARA em
estado explicito 'consult JP' -- a troca para lease justo so por Q&A com o JP (altera D1)"*. That
state was entered, the Q&A happened, and this is its record.

**Measured side by side, same machine, same operation** (`durable_commit`: one auto-commit CREATE of
one small node, the bench harness's own definition), Grafx and LadybugDB 0.16.0 in the same run:

| configuration | Linux (ext4, CPython 3.12) | Windows (NTFS, CPython 3.13) |
|---|---|---|
| baseline LadybugDB | 2.45 / 3.61 / 2.75 ms | 0.97 / 0.92 ms |
| pure CRC, lease per commit (default) | 42.6 / 46.2 / 47.2 ms -- **17.4x / 12.8x / 17.2x** | 100 / 93 ms -- **103x / 102x** |
| **native CRC, lease per commit** | **13.9 / 17.6 / 16.2 ms -- 5.65x / 4.89x / 5.88x -- MET** | 77 / 75 ms -- **79x / 82x** |
| native CRC + lease retained | 10.0 / 10.7 / 10.0 ms -- 4.08x / 2.97x / 3.65x -- MET | 40 / 40 ms -- 41x / 43x |

**The ceiling is MET on POSIX with the optional accelerator, using the SAFE default lease
behaviour** -- 3 runs of 3. No lease retention, no yield protocol, no change to D1. It is MISSED on
Windows by roughly 8x even in the best configuration.

**Where the Windows cost is**, measured by phase: a coordinator control-file publication costs
16.5 ms, of which `atomic_replace` is 14 ms, of which `CreateFileW` on the source is 11.4 ms. The
same publication on Linux costs **0.13 ms** -- a factor of ~100. Four such publications happen per
commit (lease acquire, lease release, reader registration, commit state).

**What the Windows 11 ms is NOT** (each measured, each ruled out): not the directory (a probe
`CreateFileW` in `control/` costs 1.2 ms), not the device's open `FILE_SHARE_DELETE` handles
(releasing every cached descriptor before each commit changes nothing), not fsync (0.07 ms/commit
for all nine), not the advisory lock (the reader publication holds only an in-process lock and costs
the same 14 ms), and not the `ctypes.Structure` built per call (0.07 ms). The txn manager's own
`commit.state` publication, the same seven-step sequence through the same device, costs **2.65 ms**.
That asymmetry is unexplained and it is the W6 investigation's starting point.

**Decision (JP): the ceiling stands as written; do not amend.** Reasons, in order:

1. **SPEC-M1 requires BOTH families green** (`ac_7f69d7dc`: "o gate agregado so fica verde com as DUAS
   familias verdes"; D9 makes Windows and POSIX equal citizens). Meeting it on POSIX alone does not
   discharge the gate, and recording it as met would be false.
2. **Every remaining lever is a reduction in durability-adjacent bookkeeping.** The four publications
   are epoch ownership (BR-7, AC-6, A74), the reader horizon that stops recycling from eating what a
   reader needs (BR-10), and the published snapshot source (section 8.5 step 3.7). Optimising the
   commit IS touching the machinery that makes multi-process safe. That needs a design and a blind
   critic, not a deadline.
3. **The one lever that would help most is measured harmful.** `retain_lease` buys 36 ms on Windows
   and costs another writer **5.3 s** for its first commit; two retaining writers did not finish ten
   rows each inside the suite timeout. It stays internal. Offering it would trade the property the
   product exists for -- multi-process writing -- for a benchmark number, which is what SPEC-M1 means
   by "altera D1".

**Done now, at no cost to safety:** `okto-grafx[accel]` is documented as the recommended install.
`install_crc32c` replays the acceptance corpus against the reference and refuses a candidate that
disagrees on any input BEFORE installing, so the accelerated path either produces byte-identical
digests or never becomes the implementation -- a performance option with no correctness surface.

**W6 owns, in this order:** (1) explain the 11 ms `CreateFileW` asymmetry between the coordinator's
publications and the txn manager's -- same sequence, same device, 5x apart; (2) cut publications per
commit: a reader registration per MANAGER rather than per `begin`, and a lease yield protocol (the
holder releases at its next commit boundary when C3 reports a waiter) which is the only safe form of
retention; (3) the redundant temp-file fsync in `_publish_once` (the target is fsynced after the
rename on the same volume). None is a protocol change; all three are C2/C3/C5 seams.

### C9 round 7 (W0-A, P0.5) — the derived graph is ONE picture, published by ONE assignment

**Defect** (EVOLUTION_PLAN_CODEX.md P0.5; ROUND7-PLAN §1). `VectorHnswIndex.graph()` assigned
`self._graph` and THEN installed the entries. Two searches on the first use: the second found
`_graph is not None`, judged it not current (`_graph_mark` still unset), invalidated it -- replacing
the three maps under the first thread's build -- and rebuilt; the first thread's `_install` then
found `self._graph is None` mid-loop and returned, so ITS build "completed" with three of eight
nodes and answered 3, `stale` False, while the exact regime answered 8. Reproduced
deterministically: `GatingMath`, a `VectorMath` bound through the port registry, parks the first
scoring thread (the cold builder) at its 4th `score()` call, and a second search is started while
it is parked. Pre-fix at `12c67c8`: `(3, 'approximate')` for the search that built, `(8, ...)` for
the one that interrupted it.

**Design -- Codex's critique of the plan, accepted before code.** ROUND7-PLAN §1 proposed
publishing the three maps, then the mark, then the graph. Four assignments are four interleaving
windows: two builders can leave A's graph with B's maps, and a builder failing after another
published would `invalidate_graph()` the success. Delivered instead:

- `_GraphSnapshot` -- a frozen dataclass: graph + `node_of_ref` + `entry_of_node` +
  `record_of_node` + `mark`. Built in locals (`_build`), published by one assignment under the
  guard, captured ONCE by `search()`, never read a second time by anything that answers.
- `GraphGuard` -- a protocol the assembly satisfies with `threading.Condition()` (the CF-13
  pattern: mechanism is handed in, the pure core imports none; `_UnguardedBuild` stands in for a
  direct composition). Held over reference operations ONLY -- publish, `_retire`
  (compare-and-drop of the captured picture), `_certify` (compare-and-republish with a newer
  mark) -- never over the walk, the resolver, the pool, the candidate filter or the math port
  (A91, L2). The header is read OUTSIDE it because reading it pins a page: lock order is pool,
  then guard, never the reverse.
- Generation: the builder reads `built_through_lsn` BEFORE the walk and the picture carries that
  reading. A commit landing during the build finds nothing published to note into and certifies
  nothing; the mark stays behind the header and the next search rebuilds. The search that built
  answers correctly regardless: that commit's CSN is above its snapshot (MVCC). A build that
  stamped the header at the END would certify a picture missing the row for ever -- pinned by the
  third regression. Cross-review addendum: `SnapshotLike` is structural (A19 takes it by shape), so
  a permissive predicate is a supported caller, and under one `main` answered that commit while
  `8c88e9d` did not. The build therefore CATCHES UP before publishing (`_catch_up`): it re-reads
  the header and, while it moved, REBUILDS the snapshot from a walk taken after that reading
  (patching the old picture kept a live copy of an entry reconciled away meanwhile -- the
  verification's finding), bounded to `_BUILD_CATCH_UP_PASSES`; pinned by the permissive-snapshot regression.
- Single-flight: a search meeting a build in flight waits on the guard in 0.5 s slices and takes
  the published picture when it wakes; after 120 slices it builds for itself (wasted work, never a
  wrong answer: both pictures are complete and publication keeps the fresher mark).
- Failure: a build that raises drops its locals, wakes the waiters, and leaves the published
  picture untouched. The warm path keeps its discard: `_note` retires the picture it contaminated,
  and only that one.

**The warm-path probe -- the handoff asked for a probe, not an improvisation -- found a second
defect.** `commit()` and `apply()` noted their OWN changes and then stamped `_graph_mark =
built_through_lsn`, whatever the header said. Process X warm at three rows; process Z commits row 4
and exits; X commits row 5: X's stamp certified a graph missing row 4, and X's next search answered
`(4, 'approximate')` for `k=5`, `stale` False, `verify()` clean. Same class as B6/L22, one path over
from where L22 was fixed (the cold path re-checks the header before answering; the warm path
never did). Fix: the commit captures the picture once and decides `current = picture.mark ==
built_through_lsn` BEFORE `super().commit()` moves the store -- the header the transaction manager
refreshed from the device at step 3.2 -- and then: not current → `_retire`; current → note →
`_certify`. Pinned by `tests/vector/test_warm_graph_across_processes.py` (two interpreters;
fails pre-fix with exactly that tuple).

**Evidence.** Pre-fix at `12c67c8`: (a) mid-build search FAILS, `(3, 'approximate') != (8, ...)`;
(b) failure-after-success FAILS (the interrupting search answered short); (c) commit-during-build
PASSES for the wrong reason -- the commit was noted into the partial graph that had already been
published -- so that test guards the FIXED protocol, and a variant that stamps the header at the
end fails it; (d) cross-process warm FAILS, `(4, 'approximate') != (5, ...)`. Post-fix: all four
pass; `tests/vector` and the import-boundary gate green; full suite: 7953 passed, 5 skipped, 0 failed on a clean copy of the final tree (`tests/smoke` deselected; exit 0). A five-mutant battery over the new guards -- publish before the loop, mark read after the build, no verdict before the store moves, a warm refusal keeping the picture, a cold failure invalidating the published picture -- was killed 5/5, each by the regression written for it (`M9`/`M16`/`M5` by `test_first_use_concurrency.py`, `M4` by `test_warm_graph_across_processes.py`, `M8` by the existing half-reachable-entry test).

**Adapted tests.** `test_overflow_and_ordering.py` and `test_index_visibility.py` asserted on the
old private fields (`_graph` and the three maps); they now assert `_snapshot is None` / `is not
None` -- the same property (derived state discarded / retained) in the new shape. `VectorFixture`
takes a `guard`; the engine's own suite composes without one.

**Residue** recorded in PUNCHLIST ("C9 — round 7").

### C6/C5 round 7 (M0-B) — a durable COMMIT is mandatory work, never a hint

**Defects.** Recovery inspected and truncated WAL without the section held by a live commit, so a
second opener could classify an in-flight append as crash damage. More deeply, commit, recovery and
checkpoint had separate effect-dispatch/publication paths: a durable COMMIT followed by a page or
index replay refusal could leave the participant usable, allow a stale frame to flush on close, or
certify an index past work it had not received. `commit.state` could also be treated as a stronger
source than the retained COMMIT, allowing an old restored control record to hide mandatory replay.

**Protocol.** `CommittedReplay` is the single WAL grammar: effects are replayable only with exactly
one terminal COMMIT, no effect after the terminal, and no duplicate or conflicting outcome.
`CommitRedo` decodes every page and index payload, validates page checksums, redo-gap bounds,
index definition/version/key limits and the whole dispatch plan before mutation. `CommitStateStore`
is the single strict reader and durable atomic publisher. Commit completion, checkpoint and startup
use those primitives; startup holds `COMMIT_SECTION` from observation through forensic capture,
truncation, redo and publication. Any post-COMMIT failure latches the local participant before the
section is released; all writing/page-observing doors refuse or serialize against that latch, and
close deliberately drops its possibly stale cache rather than flushing it over another process's
repair. Read-only startup performs the same fenced proof but changes no byte and refuses whenever
WAL, checkpoint and published state do not prove a checkpoint-complete image.

Before WAL durability, a heap mutation carries the domain sentinel `PROVISIONAL_CSN = MAX_U64`:
a provisional birth is never visible, while a provisional end leaves the prior committed version
open. The commit batch is materialised into private page copies, never by publishing a predicted
CSN into resident frames. `WalManager.planned_terminal_lsn()` includes a possible segment-header
LSN; index staging and page copies are retargeted to that exact terminal, and
`append_many(expected_terminal_lsn=...)` repeats the decision before its first physical byte.
Thus heap headers, page LSNs, logical index changes, COMMIT and `CommitReport` agree even across a
roll. A failed append proves an exact preimage or leaves a sticky `append_uncertain` latch.

Durability proof during recovery does not rely on `_unflushed`, which is only a performance cache.
`force_barrier_range()` re-derives and flushes the complete retained segment range before any redo
or publication. If a partial-tail observation later grows, the WAL discards the damaged cursor and
rebuilds from byte zero; resuming at the old cut could otherwise treat the final checksum byte as
an invented record header.

**Evidence.** Public and engine-level regressions cover a writer parked mid-append, dead-owner
takeover and timeout; crash-at-every-recovery-write idempotence; a checksum-damaged or missing first
effect followed by a surviving COMMIT; incomplete exact-boundary transactions; restored old or
damaged `commit.state`; catalog recovery; indexes ahead of publication or behind the checkpoint;
missing checkpoint segments; `recovery_policy='refuse'` byte identity; logical index redo; public
page-staging capability proof; and lifecycle cleanup under `RuntimeError`, `KeyboardInterrupt` and
`SystemExit`. Anchors: `tests/recovery/test_recovery_respects_the_commit_section.py`,
`test_recovery_crash_matrix.py`, `test_commit_redo.py`, `tests/api/test_public_crash_recovery.py`,
`test_startup_recovery_gate.py`, `tests/txn/test_commit_protocol.py`,
`test_wal_integration.py`, `tests/wal/test_wal_manager_append.py`, and the provisional-CSN suites
under `tests/index`, `tests/storage_core` and `tests/txn`.

**Residue.** Public exposure of raw collaborators, unfenced direct construction without a process
coordinator, and control-record probe-to-retire TOCTOU remain a separate boundary-hardening
milestone; they are recorded in PUNCHLIST and are not claimed closed by M0-B.
