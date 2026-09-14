# Performance v0.0.7: read and write paths inside the multi-writer model

Implementation round closed September 14, 2026 at the operator's request.
The [closing report](../reports/PERF_V007_CLOSURE.md) records the final
direct comparison and regression. Undelivered proposals below remain
historical assessment or future roadmap decisions, not additional v0.0.7 work.

[Roadmap and delivery status](../../ROADMAP.md#next-iteration-assessment-featurev007) ·
[Measured evidence](../PERFORMANCE.md) ·
[Reproduction rules](../PERFORMANCE.md#how-to-reproduce-and-compare-responsibly) ·
[Performance policy](../PERFORMANCE.md#performance-policy)

Prepared September 14, 2026 on `feature/v0.0.7`, which starts at the released
`main` merge `1e01be5` (v0.0.6); the version bump is `b0e4f51`. This plan orders an
initiative. It does not certify a package, authorize a release, change a persisted
format or a public default, or modify the installed Pulse. `ROADMAP.md` remains the
delivery-status authority and [`docs/PERFORMANCE.md`](../PERFORMANCE.md) the
measured-evidence authority. The evidence base is the untracked reader reports under
`.grafx-tmp/perf07/recon/` (`SYNTHESIS.md` plus
`{BACKLOG,WRITE,READ,CKPT,INSTRUMENTS,GUARDRAILS}/REPORT.md`), read at `1e01be5`.
Every number keeps its report label: **MEDIDO** (measured in that round, with a
command), **CITADO** (recorded earlier, with `path:line`), **INFERIDO** (derived,
not measured), **A_MEDIR** (not measured yet). The initial ordering was a value
hypothesis falsifiable by Phase 0. Delivery and independent qualification through
`852b655` are recorded in the [wave-A report](../reports/PERF_V007_WAVE_A.md)
and the roadmap; the original cost estimates below are not current speedups.
The subsequent [native scalar-sort report](../reports/PERF_V007_SCALAR_SORT.md)
records exact scalar dispatch, separate latency controls and a new full
regression. It implements a bounded platform lead from wave A; residual
metadata work remains subject to fresh evidence in the existing roadmap.

## 1. Outcome and fixed boundaries

Deliver measured reductions in Grafx platform read and write cost **inside** the
existing concurrency model. The operator clarified on September 14 that Pulse
is one consumer, not the optimization scope. Use representative native workloads
to establish the platform effect and consumer workloads to validate its practical
consequences. Keep mechanism counters, native latency and consumer latency
separate; a micro-benchmark alone never establishes universal or consumer speedup.

**Many concurrent writers and readers across processes is the product premise.**
No change may weaken OCC and read/write conflict detection, page-0 and descriptor
certificates, identity leasing and the durable identity floor, reader registration
and the horizon fence, WAL and recovery lineage rules, checkpoint durability,
corruption detection (`verify`), or consistency and persistence. An optimization
that needs one of those relaxed is not an optimization here; it is a decision
request and belongs in §3.4.

**Persisted-format changes, public default changes and capability activations are
user decisions.** They are listed with their numbers in §3.4 and are never
implemented speculatively, never landed "behind a flag" as a preview, and never
introduced as a side effect of an accepted item.

**Timings are evidence, never gates**, per the
[performance policy](../PERFORMANCE.md#performance-policy): timings, D5 ratios and
former throughput floors are informational, while quality gates still reject
corruption, semantic divergence, broken durability/recovery and concurrency
violations.

### Execution policy

Implementation, measurement and code surveys are done by Opus agents in workflows;
the orchestrating model plans, reviews diffs, arbitrates adversarial reviews and
decides GO/NO-GO. Every delivered item ships with **all** of: (1) discriminating
tests **proved by mutation**, each made to fail on purpose before being accepted as
a guard; (2) call counters **at the port**, never a grep of call sites; (3) an
alternating-arm A/B **on disk** (`okto_grafx.connect` on a temporary directory,
medians, round 0 discarded, the board copied per arm, separate processes, a null
arm); (4) the measured fraction of a **real consumer denominator** on the v0.0.7
baseline — Pulse logical transfer, KG page and fan-out, production vector search,
or `verify('all')`; (5) one line in [`docs/PERFORMANCE.md`](../PERFORMANCE.md) and
one [`CHANGELOG.md`](../../CHANGELOG.md) entry.

No GO without the consumer fraction. **Do not sum gains of items that attack the
same cost.** Never run anything heavy while an Amdahl measurement is running.

## 2. Baseline: evidence, not estimates

**Every recorded consumer denominator is stale, and the 0.0.6 line contains no
performance commit.** `git log --oneline 83cc313..1e01be5` is 19 commits, all
functional parity; the same range with `--grep=perf -i` is empty (MEDIDO, BACKLOG
report §0). Transfer 58.49 s, KG page 0.866–0.96 s, fan-out 0.76–0.85 s and vector
search 112–137 ms are CITADO from `17f1f76`/`acf63c8`/`a726744`, roughly 50
functional-parity commits behind HEAD, and none was re-measured in 0.0.5 or 0.0.6.
Functional parity also added planner/executor surface (polymorphic hops,
heterogeneous paths, entity scalars, subqueries, procedures) with no regression
measurement, so the baseline needs an arm at `83cc313` to separate parity cost from
machine drift.

Phase 0 therefore precedes any code: strictly sequential, one process at a time,
quiescent machine, Pulse stopped, round 0 discarded, native `google-crc32c`
installed (MEDIDO: it is). Anything over ten minutes runs detached
(`Start-Process` plus `Monitor`), never as a background shell; a 71.9 s
contamination from running something heavy during an Amdahl has already been
observed.

| # | Step | Measures |
| --- | --- | --- |
| 0 | Preserve the levantamento-2 harnesses and boards under `.grafx-tmp/` | Nothing; without it the baseline is not repeatable in a future session |
| 1 | Pulse logical-transfer Amdahl on HEAD (detached, partial JSON per round) | transfer, DDL, write_nodes, write_relations, checkpoint, certify, restore_read, runtime |
| 1b | Extra arm with `buffer_budget_bytes = 256 MiB`, if the instrument accepts connect options | Answers Q15/LV-1 with a number instead of a CITADO range |
| 2 | The same Amdahl with a worktree arm at `83cc313` (v0.0.5), rounds alternating with step 1 | What the 19 functional-parity commits cost, separated from machine drift |
| 3 | KG page and fan-out A/B on the production-shaped board, with the exact Pulse statement | Whether the Pulse fan-out has the anchored form A2/READ-1 needs |
| 4 | Production vector-search A/B with `vector_math=numpy` (the default since `4b632c0`) | The vector denominator, re-measured against the numpy adapter |
| 5 | `verify('all')` A/B with the report-digest oracle, on a **cold** handle | The A1/CKPT-2 denominator |
| 6 | Native micro: checkpoint / open / concurrency | open, populate, reopen, concurrent write with `GrafxWriteConflict` |
| 7 | Native micro: write | DML commit plus checkpoint plus verify, on disk |
| 8 | Native micro: read | ordered page with a 384-d vector and overflow |
| 9 | Checkpoint replay bench — **only if** checkpoint is material in step 1 | Bucket-floor replay, with `index/*.idx` SHA-256 as the oracle |

Only step 1 writes partial JSON per round; steps 3, 5 and 9 write once at the end,
so an interruption loses them — that is why the Amdahl runs first.

**Deliverable:** [`docs/reports/PERF_V007_BASELINE.md`](../reports/PERF_V007_BASELINE.md),
tracked, recording exact commands, HEAD, machine, medians, per-phase composition and
the comparison against the last recorded numbers; raw JSON under
`.grafx-tmp/perf07/baseline/`. No package below may be declared GO before that report
exists.

**Phase 0 outcome (September 14, 2026).** The report exists. Paired alternated arms
put the Pulse logical transfer at **0.945** (HEAD 70.19 s versus 66.31 s — HEAD 5.5 %
slower, 3 of 3 rounds below 1), the whole of that difference sitting in `write_nodes`
0.847 and `write_relations` 0.871, while `checkpoint` runs the other way at 1.128,
`certify` at 1.044 and DDL at 0.977. On the read side the KG page is **0.814** and
the fan-out 0.834, whereas production vector search (0.886) and cold-handle
`verify('all')` (0.936) have bands containing 1 and establish no difference. **The
machine was not quiescent and could not be made so**: a live Pulse server and a
concurrent agent runtime held 1.4–3.1 of the 16 logical cores for the entire session,
so absolute walls are inflated by an unknown, time-varying amount and only the paired
ratios are defended — the plan's quiescence precondition was not met and that is
declared, not hidden. Two instrument gaps remain open: step 1b was not run because
`amdahl_pulse.py` exposes no flag for connect options (a one-line change to its sink
construction, A_MEDIR), and step 9 failed because `lb3_bench.py` is stale against
0.0.6+ (an unexpected `_resolved` keyword), so LV-1 and LB-3/D-3 still have no
measurement. `psutil` is absent, so `tools/perf_round/baseline_runs.py` could not be
the runner and the three native micros ran as four fresh processes each. The
re-ranking this outcome forces is applied below: **A0 is new and first, A2 is last in
Wave A.**

## 3. Ordered delivery packages

Effort includes tests and documentation: **S** = one function, line or parameter;
**M** = one path with a new discriminating test; **L** = design, formal proof or
format change. These are not calendar estimates.

### 3.1 Phase 0 — baseline on HEAD

Mandatory and first, as specified in §2. Its output re-ranks everything below.

### 3.2 Wave A — inside the model, no user decision

Delivery checkpoint (September 14): READ-4 and FIX-W merged; READ-6 is NO_CHANGE,
READ-9 unchanged; READ-3 merged for eligible vector-free/blocking shapes.
CKPT-2 is rejected after spurious `index_entry_unresolved` findings under
eviction, CKPT-4 stopped, and W-07 refuted. W-08 remains Q16. The following
diagnostic costs retain their original measurement scope; see the
[wave-A report](../reports/PERF_V007_WAVE_A.md) for integration and qualification.

Each package carries an owner, mechanism references, discriminating tests, the A/B
and the denominator. **The order is provisional until the Phase 0 numbers land.**

**A0 — PARITY-REG: attribute and correct the 0.0.6 functional-parity cost on the
write execute and KG read paths.** The 19 commits `83cc313..1e01be5` added
planner/executor surface — polymorphic hops, bounded heterogeneous paths, entity
scalars, UNION up to 64 branches, subqueries and procedures — with no regression
measurement anywhere in that line, and the baseline now measures node `execute`
**0.847**, relationship `execute` **0.871**, the KG page **0.814** and the fan-out
**0.834** as paired alternated ratios (MEDIDO). Bounded action, in order: (1) a
cProfile diff of the **same on-disk workloads** between the two arms, comparing call
counts and cumtime per function, to name the functions that appeared or grew; (2) a
bisect across the 19 commits, paired alternated arms, on the cheapest proxy that
still reproduces the ratio. Fix only what is an **inefficiency** — an added pass, a
copy, a longer dispatch chain, a revalidation repeated per row — inside the existing
model, each with a discriminating test proved by mutation. **If the cost is a
correctness requirement of parity** (the added surface must be consulted for the
answer to stay right), it is not optimized here: it goes to §3.4 with its number.
Denominator: the transfer write phases and the KG page — steps 1 and 3 of the
baseline. Effort M–L. Ranked first because it is the largest measured lever, does not
overlap the other packages and needs no user decision.

**A1 — CKPT-2: `verify('all')` reads and CRC-checks every index page twice.**
`_verify_pages` reads from the device (`engine/verifier.py:422`), deliberately
outside the pool and without populating it; the `indexes` scope then asks for the
same pages through `BufferPool.pin`, misses, and reads them again. MEDIDO: 3,870
device reads of index files for 1,950 pages, and `verify('indexes')` alone with a
warm pool = **0** device reads — 48% of one call's device reads. CITADO: certify
was 14.22 s of the 58.49 s transfer, and `verify` 9.2% of it. The admissible form
is **call-local** reuse of images already decoded and CRC-checked, the same
discipline as VERIFY-2 and CKPTCERT-1; seeding the buffer pool is cheaper and
**worse**, since it installs device images underneath a live handle's read-view.
Keep `verify` reading from the **device**, keep the structural oracle independent,
dedupe only within one call. Pin: a damaged image produces exactly one finding per
call; the ledger never becomes an attribute; a reused verifier does not suppress a
finding on the next call. Effort M; denominator step 5.

**A2 — READ-1: NODE-IN-SEEK does not apply to the anchor of a pattern with a
relationship.** `planner.py:2967` computes `standalone = not pattern.relationships
or …`, which is False for `MATCH (a:T)-[r:E]->(b:T)`, while `_node_multi_key_seek`
(`planner.py:3074`) requires `standalone`; the one-hop fan-out — the shape READ-1
assumed Pulse's `_fetch_edges_for_nodes` uses, refuted by the Phase 0 outcome
below — plans a full `NodeScan`. MEDIDO (READ probe): 3.22 s
anchored versus 0.86 s rewritten with `WITH` = **3.74x** with identical rows;
`rows_scanned` 4,000 versus 500+500; 10,072 versus 2,556 syscalls per operation;
2,000 versus 500 descriptor re-proofs. Replicate the refusal proof at
`planner.py:3081-3089`: no term is consumed by the seek, so both routes must refuse
and count the predicate identically. Pin (a) a row the `NodeScan` refuses by type
still refuses when the seek eliminates it, (b) binding order preserved, (c) the
`IndexManager.validated_versions*` port counts exactly one per operation, not one
per landing; mutate by widening `standalone` without the proof and watch (a) go
red. Effort M. **Initial Phase 0 ranking used the Pulse workload.** The production statement
carries no id list at all: Community `72a2df3` sends
`MATCH (a:{from})-[r:{rel}]->(b:{to}) WHERE <visibility(a)> AND <visibility(b)>
RETURN a.id, b.id, r.confidence LIMIT 5000` and applies the page's node ids in Python
afterwards (`kg_routes.py:606-615` and `:625`, quoted verbatim in
[the baseline](../reports/PERF_V007_BASELINE.md)), and `IN $` does not occur anywhere
in that Community tree. NODE-IN-SEEK on an anchored pattern therefore does not serve
the production shape and has no Pulse denominator here. The operator's platform
scope clarification removes “after every other package” as an automatic priority:
measure representative native anchored queries and retain the refusal proof
before choosing the implementation priority. Pulse's lack of this query form is
not evidence against its value to Grafx. Recorded separately as consumer feedback outside this
repository, not as work in this plan: that fan-out fetches up to 5,000 edges per
layout and filters the page's ids in Python.

**A3 — CKPT-4 and CKPT-3: repeated work in cold open and recovery.** Effort S each.
**CKPT-3 gate:** deliver only after a design check that narrowing does not
reintroduce the OPEN-1 circularity (an index header attesting to itself against the
heap watermark that should verify it); if it does, CKPT-3 moves to §3.4 and is not
implemented.

| ID | Mechanism | Evidence and discriminating test |
| --- | --- | --- |
| CKPT-4 | `_read_header` (`index_manager.py:1015-1018`) computes `present` and discards it; `open()` (`:903`) calls `_finish_open(header)` without `page_count` and `_finish_open` (`:969`) recomputes it, while the sibling `_open_existing` (`:929`) already passes `page_count=present` — the equivalence proof is in the same file | MEDIDO: 60 of 303 `page_count` calls per cold open (20%). Pin a counter on the `storage.page_count` port asserting the exact number per cold open; mutation: pass a wrong `page_count` and watch the open refuse |
| CKPT-3 | `recovery_manager.py:1545` calls `manager.table_watermark_photo()` with no arguments right after redo, even when no heap page was installed, while the checkpoint path (`txn_manager.py:4673-4681`) passes `refresh_table_ids` derived from `replay_watermark_scope` | MEDIDO: cold open = 3 photographs = 30 `committed_high_water` = 3 complete logical heap scans, whereas `checkpoint()` = 3 photographs and **0** `committed_high_water`. The device-level gain is nil (the pool serves the second and third), so measure cold-open CPU, not syscalls |

**A4 — pure-Python per-row work on the read path.**

| ID | Mechanism | Evidence, condition and effort |
| --- | --- | --- |
| READ-4 | `free_variables` recomputed per row inside `_sort_value` (`query_engine.py:13225`, `:13233`; `ast.py:1213` walks the whole tree) although the set is a plan constant | MEDIDO: 2,000 calls per query, tottime 0.5544 s (4.7%) and cumtime 0.9211 s (7.8%) of 11.84 s; 2–4% of wall after deflation. Memoize per plan node / `SortItem`, pin alias precedence and refusals, and mutate the memo to return the wrong set so an alias-precedence test goes red. Effort S |
| READ-6 | Full decode of every scanned row under a retained LIMIT: `HeapStore.scan` materializes each visible row's whole tuple and `_top_rows_from` keeps 500 of 2,000, although `scan_projected` and `_decode_tuple_projection` already exist | MEDIDO `heap_store.scan` cumtime 3.29 s of 11.84 s; ceiling ~21% of the page profile INFERIDO. The cut is decode versus one extra page pin per survivor, and there is no GO without that cut measured; pin that a row becoming invisible between the two passes can never appear in the result. Effort M |
| READ-9 | Value detachment at the public boundary (`_query_value_snapshot`), the public-boundary capability guard | 5,500 calls per query, tottime 5.0%; 2–4% INFERIDO. Low confidence, listed for completeness, admissible only with an explicit design. Effort M |

**A5 — READ-3: engage the grouped endpoint landings that already exist.**
`_batched_landing_steps` (`query_engine.py:7975`, the BATCH-REL-1 read side)
groups endpoint certificates, but `_admits_batched_landings` ends in
`return blocking` (`:7973`) and the `vector_free` gate (`:8079`) also applies.
MEDIDO: adding `ORDER BY b.pk` changes neither the invalidations (500 split, 2,000
anchored) nor the wall on a table with no vector columns — the batch did not engage
even with a blocking consumer. First **diagnose** which gate refuses (three
candidates: `_closed_vector_free_landings`, the canonical `__func__` values of the
index manager, `_endpoint_identity_index`); that is effort S and the cheapest route
into the READ-2 cost bag. Then engage the landings through the existing validated
`validated_versions_many_reusing` port (`index_manager.py:7635`) **without changing
what is certified**. Delivered in `a46e0d2`: the frontier is per source, with
cached destinations excluded. The corrected probe observes `64+64+64+8` for one
source and 200 distinct destinations; an overlap fixture has 100 batched calls
versus 131 scalar certificates. A blanket “8 invalidations for 500 rows” is
incorrect. The streaming LIMIT used by the Pulse fan-out still does not qualify.

**A6 — commit-local repeated work.**

| ID | Mechanism | Evidence, condition and effort |
| --- | --- | --- |
| W-08 | Element-wise revalidation of the 384-d vector per statement: `public_views.py:1867` `_query_parameters_snapshot` → `:2160-2183` copies into a tuple and runs `all(type(item) is float …)` over 384 elements | MEDIDO 19,250 calls in a 50-row commit; 1.5–3% of the commit INFERIDO. The copy exists so a hostile callback cannot withdraw a pin mid-operation: that property is preserved, not traded. Effort S |
| W-07 | Identical key lookups repeated inside one commit: `KeyPageMemo.matches` (`key_page_memo.py:48`) memoizes *decoding*, not *reading* | MEDIDO: a 50-relation commit reads `index/pk_Doc.idx` 55 times for 4 distinct ids. Any seek-result memo must be **scoped to the commit and discarded at its boundary**, must follow the D-29 rules (callable identified by `is`, one identity per slot with eviction at both ports, serialization in the adapter, a test counting exactly one proof per port) and must not replace the index's freshness authority. Effort M |

**A7 — small items, admitted only if a measured fraction >= 1% of their own
denominator survives on the v0.0.7 baseline**; otherwise recorded and dropped, not
carried forward: LV-3, `active_indexes_for` resolved twice per row
(`index_manager.py:5128`, `:6196`) inside `COMMIT_SECTION`, 0.66% of transfer
CITADO; VECTOR-7, `_vector_column_of` scanning 44 columns linearly per candidate
(`query_engine.py:10039`), 1.9% of the vector query CITADO; C6,
`_retarget_commit_batch` running on 10 of 12 node commits (`txn_manager.py:5196`)
and never explained, ~1.5% of transfer CITADO.

**Consumer guidance, publishable now without any engine change.** These are public
`connect()` options a consumer passes; they change no default and no format, so
they carry no decision number. Publish them for Pulse-shaped loads once steps 1/1b
put a v0.0.7 number on them.

| Guidance | Mechanism | Evidence and price |
| --- | --- | --- |
| `buffer_budget_bytes = 256 MiB` | The 64 MiB default (`runtime/config.py:266`) is 8,192 frames, while a Pulse-shaped candidate builds 162 indexes × 65 pages = 10,530 pages = 86 MB | CITADO: node phase 1.10–1.15x, −18% physical reads in the relationship phase, one-hop 1.38x, 3.5–10.2% of transfer. Price +192 MiB per handle; a 128 MiB middle arm and the null arm are mandatory, because the previous round had an instrument conflict in the relationship phase. **Step 1b was not run**: `amdahl_pulse.py` passes no `connect_options` to the sink and exposes no flag for one, so this guidance still has no v0.0.7 number (A_MEDIR; the one-line instrument change is recorded in §1b of the baseline report) |
| `identity_lease_size >= batch size` (1024 for a 1,024-row batch) | `identity_lease_size` is 64 (`config.py:265`) and `_prepare_identity_plan` discards the remainder when it does not fit (`txn_manager.py:6933`, `:6942`), so every 50-row commit first pays a full durable CN-1 sub-commit | MEDIDO: a 50-node commit is 2 `append_many` and 6 fsync; with 1024 it is 1 and 4 (wall gain on this machine only 1.017x, because `nt.fsync` is cheap here) |
| Batch statements inside one read transaction where the per-transaction envelope dominates (READ-7, COMMITSTATE-1) | `begin("read")` publishes a reader-registration file per transaction | MEDIDO: `with db.begin("read"): pass` costs 3.58 ms against a 3.16 ms point query |

### 3.3 Wave B — proof-gated, no premise change if the proof holds

W-01/W-02 are now merged in `0e8c13a`, with the private-wait starvation
correction in `852b655`. The 154-entry observation below precedes FIX-W
integration; the integrated 50-statement fixture has 104 participant entries.
Its fresh recheck records zero participant file locks and four other file-lock
acquisitions on both revisions. The historical 1.226x arm remains a ceiling.

**W-01 / W-02 — the participant section is process-local by construction but is
implemented as an OS advisory file lock.** The section name is
`txn-<crc32c(owner_id)>` (`txn_manager.py:976-979`) and its own docstring
(`:8344-8368`) states that no other process ever waits on it. Even so,
`_take_file_lock` (`coordination_local.py:1989`) takes and releases an advisory
file lock and revalidates it with `fstat` + `stat` on every re-entry, and
`database.py:2653` (mark), `:2604` (execute) and `:2660` (settle) enter it three
times per write statement plus four per commit. MEDIDO: 154 acquisitions in a
50-row commit (50×3 + 4); 318 `msvcrt.locking`, 153 `fstat` and 153 `stat`; 18.4%
of the commit wall, with an A/B **ceiling** of 1.226x (0.4551 → 0.3713 s, n=18,
alternating medians) when only that lock is replaced by an in-process lock.

That arm is a ceiling, not an implementation. Admission requires, in writing: a
proof that the section identity is **unique per process** and that no cross-process
semantics rely on the file lock; a two-process discriminating test showing that two
threads of the same participant still serialize and two processes still do not wait
on each other, with `tests/coordination/test_exclusive_sections.py`,
`test_multiprocess.py` and `tests/txn/test_lifecycle_serialization.py` green and
mutation-proved; and a real implementation in the coordinator adapter, since the
engine may not import `threading` (composition boundary G2). W-02 (fusing the three
per-statement entries) is independent of W-01 and cheaper to justify, but must
preserve the reversibility window the `_logical_statement_publication` docstring
describes: a statement that fails validation leaves no visible effect. The bridge
from 18.4% of a 50-row commit to the transfer denominator is **A_MEDIR**. **If the
proof fails, this becomes a user decision and is not implemented.**

### 3.4 Decision queue — user decisions, never implemented speculatively

Listed with their numbers and what each changes. This plan neither decides nor
ranks them.

| # | ID | What changes | Recorded evidence |
| --- | --- | --- | --- |
| Q1 | CONCUR-2 | One epoch snapshot instead of a certificate per access; 2.5% of the probes the fence refuses today stop being refused | 9.9–13.9% of transfer, 26–39% of fan-out (CITADO); needs a formal per-epoch snapshot equivalence proof |
| Q2 | D-8 / STORID-2 | Minimal `_still_names` form (`fstat` + no-follow `stat`) in `strict`, conditional on Q1 | ~190 µs of ~209 µs per exact-read certificate (CITADO); worth 2–3x more in `strict` than in `generation` |
| — | READ-2 | One device certificate per **operation** instead of per endpoint landing | ~30% of the fan-out profile MEDIDO (2,000 invalidations for 500 returned rows); same cost bag as A2/A5 — **do not sum** |
| Q8 | READ-5 | Widen the `OrderedNodeMerge` totality proof to admit `RETURN n` | The KG page scans 2,000 rows to return 500 MEDIDO; touches an explicit correctness guarantee |
| Q9 | READ-7 | Reuse a reader registration across sequential read transactions of one handle | Envelope 3.58 ms versus a 3.16 ms point query MEDIDO; interacts with the BR-10 `reader_horizon` regression |
| Q10 | CKPT-1 | Narrow `barrier_files` to actually touched files; requires accepting the fsync induction in writing | 37 `durable_barrier`/fsync for 5 `write_page` MEDIDO; touches durability |
| — | CKPT-5 | Avoid the second `IndexStore.open()` per index on cold write open | 30 extra `page_count` and 30 extra page-0 reads MEDIDO; both points sit outside `COMMIT_SECTION` |
| — | COMMITSTATE-1 | Any memo over the publication authority (one commit-state read per statement in autocommit) | ~65% of `_run_statement` cumulative for a point read MEDIDO; the old harness shares a transaction, but pinned Pulse `72a2df3` calls `database.execute` per fan-out table. The real fraction remains A_MEDIR |
| Q13/Q15 | Public defaults | `buffer_budget_bytes` 64 → 256 MiB; `identity_lease_size` 64 → 1024 | As in §3.2; opt-in today, default only by decision |
| Q3 | D-3 / LV-2 | Bucket sizing port (`DEFAULT_BUCKET_COUNT = 64`, 99% empty) | DDL = 10.6% of transfer, 99% of it empty-bucket writes (CITADO); **persisted format** |
| Q4 | STORAGE-6 | Persisted ordered spine (design B-spine) | x2.54 on top-K when the LIMIT bites (CITADO); **persisted format**; the memo warns the value is asymptotic, not wall-clock at current N |
| Q5 | WAL v2 zlib | Implemented but dormant unless `enable_wal_page_compression()` is called. Does Pulse call it (A_MEDIR, cheap), and should it become the default (**one-way** activation)? | MEDIDO on a fresh board: `format_version 1`, `required_capabilities []`, `wal_v2_capable False` |
| Q6 | EXEC-1 | Per-statement Python code generation, with `<grafx-codegen>` tracebacks and code absent from the repository | +16% of the KG page over the already-landed EXEC-CSE (CITADO); an alternative to EXEC-CSE, **not** additive |
| Q7 | EXEC-5 | An inexact selectivity statistic chooses the scan-versus-seek arm | Fraction 0 today; cliff 1.39–2.36x, up to x3.3 under churn (CITADO) |
| Q11 | Determinism | The cross-machine determinism contract for `vector_math='numpy'`, which became the default in `4b632c0` without the contract the consensus required | Every vector number before `4b632c0` measured the pure adapter |
| Q12 | EXEC-MP | Whether the `MappingProxyType` seal covers the query engine's hot dispatch tables or only the storage core | Residual after EXEC-CSE is 0.6% (CITADO); conflicts with `tests/storage_core/test_no_shared_state.py` (AST scan) |
| Q14 | Measurement environment | `ladybug` bench extra, `psutil`, and copying the levantamento-2 boards into `.grafx-tmp/` | Current tests/measurements supply `psutil` and isolated board copies. The Ladybug comparison remains UNMEASURED; this does not authorize a new competitor campaign |
| Q16 | W-08 | Add `operator.is_` outside the frozen standard-library allowlist | Reported 0.24% of commit; no implementation authorized by this plan |
| M3 | READ-9 / NodeValue | Change the public value-materialization trust boundary | Bisected to `6b6ff12..69af5db`; a redundant-copy claim does not prove an engine collaborator can be trusted |

### 3.5 Explicit exclusions and already-delivered work

Excluded with a reason; do not re-propose without new evidence. **W-05**
(memoizing file size per page port: 1.008x, noise, and it breaks the multi-process
model). **Relaxing descriptor revalidation** — refuted three times in one round:
`generation` removes no syscall on the certified path and its A/B median is *above*
`strict` (commit 59.77 versus 57.58 ms, read 14.04 versus 13.05 ms MEDIDO). **D-4**
(expected premium is zero). **LOAD-4 `retain_lease`** (a lease held between commits
serializes writers; the capability exists at `txn_manager.py:792`, is unreachable
from the public API, and is recorded so it is not rediscovered). **LADYBUG-M4**
(x19.7 MEDIDO, but a semantic/policy change). **LOAD-1/2/3** bulk-load mode.
**EXEC-6** (x1.10 and it changes the answer). **The storage layout family**
(locality 0, Bloom 0.00005, 32 KiB pages −1.09x cold). **The vector ANN family**
(SQ8 changes 13.3% of the top-10; P1.16, VEC-6 and the 4,096 threshold are explicit
user exclusions). **The platform family** (PyPy, 3.13/3.14, free-threaded,
same-handle threads). **Native compiled kernels** (no toolchain or channel, and
`ctypes` loses at 0.78x). **CONCUR-3/5** (−5.6% to −7.5% on the real paginated
endpoint). **OPEN-1** (circular, confirmed absent from the code at HEAD — and note
that CKPT-3 is *not* OPEN-1).

Already delivered at HEAD, verified by code and not by name: WAL v2 zlib
(dormant by default — see Q5); the numpy `vector_math`/`codec` defaults
(`4b632c0`); QUERY-1/2/3, KG-1/4, KGRUN-M3/M4, RELSEEK-M4, EXEC-CSE, NODE-IN-SEEK
(standalone only — that restriction is the root of A2, not a re-proposal) and
STO-M1; CKPTCERT-1 and VERIFY-2; the write-path deliveries (per-file dirty queue,
per-index freshness memo, TXN-1, CE-1 two slots, HEAP-2, fused control-record read,
recovery-floor photo reuse, scoped checkpoint photograph); and C3 plan cloning
(MEDIDO at HEAD: `_query_plan_view` called 0 times in 50 executes).

## 4. Test cadence and safeguards

- Each package runs its focused positive, negative and prior-regression cases during
  implementation; grouped regressions run at the end of each wave, not after every
  edit. New WAL, format or rollback risk requires its focused fault tests immediately.
- After integrating a coordination-section change, run
  `tests/api/test_vector_concurrency.py` and
  `tests/api/test_public_boundary_concurrency.py` before the grouped gate.
  In `coordination_local.py::_wait_for_local_lock`, park only with a real
  `SystemClock` and real sleeper, keep the wait bounded, and preserve sampling
  for manual/foreign clocks.
- **Make every test used as proof fail on purpose before accepting it as a guard.**
- **Count calls per operation with counters at the port, never by grepping call
  sites.** Do not deflate syscall fractions derived from a profile: cProfile
  inflates Python frames, not syscalls.
- **A/B protocol:** alternating arms, medians, round 0 discarded, a mandatory null
  arm, on **disk** — never on the in-memory fixture — for any "fewer
  certificates/syscalls" claim. Install the same CRC the consumer uses before timing
  any path that hashes pages. Treat missing data as unknown, never as zero.
- **Never run anything heavy while an Amdahl measurement is running.**
- Preserve the full run's exit status and JUnit independently. Reproduce new
  failures on the relevant ancestor before attribution. A documented tooling-only
  exception may permit unchanged-runtime measurements after focused correction;
  it never turns a failed full run into a passing gate.
- Do not sum gains of items that attack the same cost; the overlap columns of the
  source synthesis are normative.

Mechanisms this initiative may not weaken (condensed; `file:line` from the
GUARDRAILS report §11, whose §12 lists the mandatory test sets per area):

| # | Imperative | Mechanism |
| --- | --- | --- |
| 1 | Keep the OCC conflict as the intersection read∪write × write; never reduce it to "another writer exists" | `engine/txn_manager.py:5802` |
| 2 | Preserve the two OCC passes and the interest frozen between them | `engine/txn_manager.py:5510` |
| 3 | Keep `_declare_page_interest` and the refusal of "wrote something, declared nothing" | `engine/txn_manager.py:5458`, `:5496` |
| 4 | Keep the lock order lease → commit section; never hold the section while asking for the lease | `engine/txn_manager.py:56-60` |
| 5 | Keep identity ranges burn-only and keep refusing an explicit id below the durable floor | `engine/txn_manager.py:336`, `:7352` |
| 6 | Register the reader before selecting the snapshot, and let `refresh_reader` recreate a pruned registration | `engine/txn_manager.py:3885`, `adapters/coordination_local.py:1335` |
| 7 | Keep the CE-1 two-slot envelope with its predecessor proof; never let `commit.state` `format_version` descend | `domain/control_record.py:480`, `engine/commit_state_store.py:225`, `:255`, `:281` |
| 8 | Keep `_still_names` on every warm descriptor hit in `strict`; it is the fence that closes defect D1 | `adapters/storage_local.py:2322`, `:2109` |
| 9 | Keep the page-0 CAS with name re-proof before comparing the clock | `engine/buffer_pool.py:2951-2999` |
| 10 | Keep `check_freshness` as the only staleness detector and never persist its verdict in read-only | `engine/index_manager.py:1520`, `:1600` |
| 11 | Let `verify()` read from the device with an independent structural oracle, and keep `page_unwritten` distinct from corruption with one finding per image per call | `engine/verifier.py:9-20`, `:473` |
| 12 | Run `verify()` on a **cold** handle whenever it is proof; live `verify('all')` is known to be incoherent | `engine/verifier.py:9-20` |
| 13 | Keep capability bits as the open fence with no implicit activation, and keep `MappingProxyType` on the storage-core dispatch tables | `domain/model/catalog.py:199` |
| 14 | Keep `SimulatedCrash` deriving from `BaseException` | `adapters/storage_fault.py:32-36` |
| 15 | Cut the DML crash matrix **after** the last `wal/` barrier: the first heap write is the CN-1 identity reservation and carries no `INDEX_WRITE` | `engine/recovery_manager.py:1438` |

## 5. Documentation, packaging and claim checklist

- Every delivered item adds one line to [`docs/PERFORMANCE.md`](../PERFORMANCE.md)
  naming workload, build, boundaries and limitations, and one
  [`CHANGELOG.md`](../../CHANGELOG.md) entry under the 0.0.7 development headings.
- **Every speedup claim names its measured workload and denominator on the
  v0.0.7 baseline.** Native API measurements can establish bounded platform gains;
  consumer measurements establish the corresponding application effect. A
  micro-benchmark ratio is attribution evidence, not a universal or consumer gain.
- **No persisted-format change, public default change or capability activation
  without the user's recorded decision**, quoted by its number from §3.4; a one-way
  activation is recorded as one-way.
- Update the ROADMAP row and status when a package lands; do not update
  `FEATURE_COMPARISON.md` or any comparative claim from this initiative, and do not
  promote the 0.0.7 development source to a published release in prose (0.0.6 was
  published on September 13, 2026).
- Forbidden shortcuts: "N times faster" without the denominator, summing overlapping
  items, quoting a ceiling arm as a delivered gain, and reporting a ratio measured on
  the in-memory fixture as a disk result.
- Run the documentation validator and the affected focused tests before declaring any
  package complete; timings remain informational, per the
  [performance policy](../PERFORMANCE.md#performance-policy).

## 6. Primary references

- [`ROADMAP.md`](../../ROADMAP.md) — delivery-status authority: the
  [remaining performance work](../../ROADMAP.md#remaining-performance-work) queue and
  the [0.0.7 assessment](../../ROADMAP.md#next-iteration-assessment-featurev007).
- [`docs/PERFORMANCE.md`](../PERFORMANCE.md) — measured-evidence authority,
  [reproduction rules](../PERFORMANCE.md#how-to-reproduce-and-compare-responsibly) and
  [performance policy](../PERFORMANCE.md#performance-policy).
- [`docs/specs/FUNCTIONAL_PARITY_PLAN.md`](FUNCTIONAL_PARITY_PLAN.md) — the 0.0.6
  functional-parity line this baseline must re-measure, and this plan's structure.
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md) — the connect options named in §3.2;
  [`docs/OPERATIONS.md`](../OPERATIONS.md) — checkpoint, verify, vacuum and recovery
  contracts the checkpoint and verify packages must not weaken;
  [`docs/reports/README.md`](../reports/README.md) — where
  [`docs/reports/PERF_V007_BASELINE.md`](../reports/PERF_V007_BASELINE.md) and every
  subsequent receipt are registered.
- `.grafx-tmp/perf07/recon/SYNTHESIS.md` and the six reader reports under
  `.grafx-tmp/perf07/recon/{BACKLOG,WRITE,READ,CKPT,INSTRUMENTS,GUARDRAILS}/REPORT.md`
  — the untracked evidence base for every label above.
