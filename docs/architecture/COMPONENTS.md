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

### CF-11 — BR-10 log reclamation is never driven; the WAL grows without bound (C5/C11 wiring)

Found by C4's blind critic, round 2. `WalManager.recycle` has **no caller anywhere in `src/`**, and
`recyclable_horizon` is never called outside its own definition. The reclamation machinery is built,
tested and unreachable from the assembled system, so a long-running database's log grows forever.

Not a C4 defect -- the doors exist and work. A wiring gap owned by whoever drives the snapshot horizon
(C5) and the composition root (C11). Not blocking under §13 (no loss, duplication or wrong result), so
it does not stop a sign-off, but it is an operational failure in any real deployment and must be closed
before W6 ships.

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
