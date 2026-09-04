# Performance round 0.0.2 — provenance and execution log

This document records reproducible facts for the bounded round governed by
`GRAFX_PERFORMANCE_ROUND_FINAL.md`. It is an execution receipt, not a second roadmap.

## P0.0 — frozen base

Status: **complete**.

| Input | Pinned value |
|---|---|
| Grafx integration base | `c5ab19d874e59962ba1b66eaa7ab682d1e8b7fac` |
| Grafx public ancestor | `ead05a4cfad5f5ca60c8677b330cc16bb6824b9b` |
| Working branch | `feature/v0.0.2` |
| First round commit | `7f2a692` |
| Pulse Community counterpart | `d50c03404bd72873b596596f1c4848d56dbcd437` |
| Pulse Core counterpart | `f602c7cc2f6a9f5ef446d4c991309196bd4667c7` |
| Python | CPython 3.13.1, 64-bit |
| Operating system | Windows 11 `10.0.26200`, build `26200` |
| Filesystem | NTFS on drive `D:` |
| CPU | Intel Core i7-11800H, 8 physical / 16 logical cores |
| RAM | 31.7 GiB |

The source tree reports `okto_grafx.__version__ == "0.0.2"`. The ambient Python distribution
metadata still reports an unrelated installed `okto-grafx 0.1.0`; therefore no official result may
resolve Grafx from ambient distribution metadata. Benchmarks must import this pinned checkout or a
wheel built from it, and must record `okto_grafx.__file__` with the result.

### Effective baseline configuration

| Setting | Grafx baseline | Pulse run |
|---|---:|---:|
| `page_size` | 8192 bytes | 8192 bytes |
| `buffer_budget_bytes` | 64 MiB | 64 MiB unless the Pulse receipt proves another value |
| `checksum` | `auto` | `auto` |
| installed `google-crc32c` | 1.8.0 | 1.8.0 |
| `descriptor_revalidation` | `strict` | `generation` |

Installed measurement dependencies at capture time: NumPy 2.5.1, psutil 7.2.2, py-spy 0.4.2 and
pytest 8.3.4. An official run must repeat this inventory rather than inheriting these versions by
assumption.

### Isolation decision

The live Pulse backfill is still consuming CPU in the default data home. P0.3 profiling and P0.4
same-code baselines are therefore deferred until it drains. The live board must not be opened,
copied while mutable, or profiled. Read-only operating-system counters remain observational only.

The original order blocked P1 promotion until P0.1–P0.4 closed. The user's later decision removed
performance gates: P0.3/P0.4 remain post-backfill evidence, while H5 and every quality gate remain
mandatory before promotion.

## P0.1 — H5 attach/rebuild diagnosis

Status: **complete** for the isolated engine protocol; static Pulse-flow relevance is recorded
separately and does not change the result below.

- Generic attach followed by commits on unrelated tables preserved exact seeks
  (`rows_seeked=1`, `rows_scanned=0`); that broad form of H5 was falsified.
- A handle that itself completes a vector rebuild retains the existing process-local rebuild
  fence at the scan position. Reads above that position remain retryably refused until indexed
  work advances the generation or the handle is reopened. This is distinct from durable STALE
  state and from insufficient header coverage; the round did not relax that fail-closed contract.
- The experiment did reproduce a separate availability defect: after a foreign cold open moved
  the durable page-0 sequence, a live writer could reuse its clean resident page 0 and fail after
  the WAL barrier. The commit path now discards/rebases that clean frame; a pinned clean frame is
  doomed until its holder releases it, while any dirty page 0 remains refused.
- The literal three-process arm creates/rebuilds/closes in A, attaches/rebuilds/commits elsewhere
  in B, then performs a cold verification in C. It records all three refusal sites, bounded retry
  behaviour, seek/scan statistics, stale state and a clean `verify("all")` result using only a
  temporary database.

Promoted commits: `7a9414c`, `e8c3583`, `cef70db`.

Static review of the pinned Pulse Community tree at
`d50c03404bd72873b596596f1c4848d56dbcd437` found no production call to
`rebuild_vector_index` or to the Grafx maintenance facade. Pulse rejects a candidate with stale
indexes instead of rebuilding one in-process. Therefore the same-handle rebuild fence is not
reachable from the pinned backfill flow and is not a remaining Pulse P0 blocker. The review used
source code only, did not access the live data home, and was independently checked before Nexus
handoff `hof_5278e93d6f09409cbce04bbbcbe787f1` was accepted.

## P0.2 — reproducible instruments

Status: **complete; the post-drain execution remains P0.3/P0.4 work**.

Commit `c276dec` (source branch `perf/v002-p0-instruments-claude@9635146`) versions the
canonical receipt, authenticated board-copy and independent-series primitives under
`tools/perf_round/`. They bind each run to the exact Grafx/Pulse SHAs, source roots, Python,
configuration, seed, thermal/kind labels and the instrument code actually executed. Direct Python
scripts and inline `-c` payloads are supported; module execution and decoy paths are refused because
their executed bytes cannot be proved by the receipt.

The copy protocol rejects live data homes and their ancestors, proves source-before, copy and
source-after inventories, and never publishes an unproved staging directory. Timeouts terminate the
whole process tree and fail closed when termination cannot be proved. The series runner requires
independent homes/copies, child provenance, finite nonnegative samples, process-tree memory and
explicit dispersion. Hash sidecars detect accidental or local tampering; they are deliberately not
presented as authority signatures.

The concrete Pulse driver was integrated in `be286fa` and `2d43d75` from immutable source branch
`perf/v002-p0-card-driver@e8a6be0`. It pins Community and Core independently, requires a fresh
full-data-home clone for every run, validates the imported source files and the authenticated Grafx
route, isolates exactly one eligible historical card in one SQLite write transaction, calls the
public `ConsolidationProcessor.process_batch()` lifecycle and proves one ACK plus one committed
target audit without emitting row values.

RAW runs install no hooks. Instrumented runs use bounded, reversible hooks for heap walks, header
and tuple decodes, buffer-pool load calls, index candidates, query statistics and transaction
outcomes. Recorder failure invalidates the sample but cannot replace the result or exception of the
operation observed. A pre-existing pooled handle is reported separately from actual `connect`
calls. Queue/audit equality receipts use an ephemeral process-keyed HMAC, preventing the published
digest from becoming a low-entropy payload oracle.

The driver enforces only thermal and memory configurations it can make true: `warm` performs a
proved sequential read of the active graph before open, `mixed` explicitly means uncontrolled OS
cache, and `cold` is refused because portable user-space code cannot prove cache eviction. The
pinned Pulse adapter does not expose Grafx buffer sizing, so any value other than its actual 64 MiB
default is refused before runtime initialization. A parent-PID marker blocks accidental direct use;
like the self-hashed manifest, it is not claimed as an authority boundary against a malicious local
operator. A signed authority bundle remains a separately governed future item.

The focused regression passed 46 tests, plus Ruff, `py_compile` and `git diff --check`. Two
development smokes on synthetic disposable Pulse homes — one RAW and one instrumented — each
processed exactly one card, appended one target audit and preserved the Grafx route. They are not a
RAW-vs-instrumented comparison and are not P0.3 evidence. No live board was accessed. P0.3 still
owns the authenticated post-drain run, external `py-spy` profile and separate unique physical
census; P0.4 still owns the independent same-code series.

## P0.3 — post-drain profile and census

Status: **tooling complete and published; execution on the Pulse corpus remains pending until the
live backfill drains**.

The clean local source roots selected for the eventual run are
`D:\Projetos\Techridy\okto-pulse-perf-st2` (Community `d50c034`) and
`D:\Projetos\Techridy\okto-pulse-core-corpus-baseline` (Core `f602c7c`, version 0.3.3).
The runners re-check both SHAs, package origins and source-subtree cleanliness before every run;
these recorded paths are not a substitute for that admission check.

Branch `perf/v002-p0-census`, through commit `222a854`, adds the two distinct observations required
by the frozen plan without conflating either with a baseline:

- The instrumented replay now records actual endpoint lookup hits and resolves their distance to
  the post-workload table-chain tail without subtracting opaque page identifiers. It reconciles
  the exact per-page hit weights with endpoint lookup counters; the weights are bounded by
  the pages observed, never by a hit cap, so long cards cannot fail on truncation. It also records
  dynamic calls and failures for vector search and vector rebuild.
- `profile_pulse_card.py` creates exactly one fresh full-home clone, starts the replay as its direct
  child and attaches exactly py-spy 0.4.2 as a sibling to the internal `Popen` PID. A nonce-bound
  READY/GO protocol releases the real operation only after py-spy confirms attachment. The CLI has
  no PID escape hatch and the fixed profiler command excludes locals, full filenames, native frames
  and subprocess following. Its speedscope sample count must match py-spy's terminal summary with
  zero errors.
- Profiler process-tree CPU and I/O are sampled deltas from a mandatory snapshot immediately before
  GO. They are explicitly lower bounds; RSS/private are tree peaks. The profiler sibling is excluded,
  while post-validation and runtime close remain inside the profile scope and are labeled as such.
- `pulse_graph_census_once.py` performs one separate, untimed structural census. It launches the
  hash-pinned census child on another byte-identical clone, authenticates the Grafx binding, opens
  it read-only with `recovery_policy="refuse"`, requires clean positive page and record verification,
  and inventories the clone without wildcard exclusions. Every pre-existing file and all durable
  graph bytes must remain unchanged. Raw before/after digests, counts and sizes are reported
  honestly. The only admitted delta is zero with the exact lock already present and empty, or one
  newly created `<authenticated graph>/control/txn-<8hex>.lock` for the handle's participant
  section, regular, zero-byte and carrying the SHA-256 of empty content; every other delta fails
  closed. The independent parent authenticates the persisted board binding before reconciling it.
- The census walks only private headers with `copy_content=False` and emits aggregates, never record
  IDs, page IDs, references, CSNs or payloads. MVCC “dead” means only “not live under the current
  `HeapVersion.live` predicate”; it does not prove vacuum eligibility. Vector counts are static
  catalog presence and do not claim search/build activity. The one-shot runner reconciles verifier,
  heap-slot, MVCC-class and locality totals before accepting the artifact.

Hardening after the adversarial review (`perf/v002-p03-hardening-claude`): the profiler runs
a direct-interpreter preflight before the py-spy protocol and refuses, with an operational
message, any launcher that starts Python as a grandchild (uv trampoline, `py` launcher, Store
alias), because the PID/READY/GO proof and the attach would otherwise target the wrong
process; and endpoint locality is aggregated incrementally per page, so the former 100 000-hit
cap and its truncation failure no longer exist.

Zero-byte participant locks may accumulate across processes. Cleanup is future control-plane
hygiene, not a current engine correction, and requires a concurrency-safe ADR plus multiprocess
proof that reclamation cannot create split-lock behavior.

The milestone regression covered 98 focused instrument tests in one combined run. Ruff,
`py_compile`, `git diff --check`, command-line contract checks, an isolated real-Grafx census smoke
and a synthetic py-spy 0.4.2 multi-thread smoke were also successful. The synthetic profiler
artifact reconciled 43 samples and zero errors; it is developmental evidence only. No default or
live Pulse data home was opened, copied or profiled. Consequently P0.3 is not yet marked complete,
and no P1 branch is promoted on the strength of tooling alone.

## P1.1 — commit-window instrumentation (D-26)

Status: **integrated on `feature/v0.0.2`**.

Branch `perf/v002-d26-commit-metrics`, commit
`5abfd396ea352eb4eac9e20902d4ab55f0bc8898`, adds closed-cardinality timings for writer-lease and
commit-section wait/hold windows, timings for the ten internal commit phases, and counters for WAL
pages/bytes, examined frames, flush calls, foreign commits and retargets. Metric delivery remains
outside the exclusive sections and uncertain acquisition/release paths suppress the local trace.

The implementation was promoted as `7aa410d`; `59020a4` then moved foreign durable-gap completion
out of the `occ` phase and documented it under `other`, so recovery work cannot be mistaken for
local validation cost. The focused commit file passed 18/18 after integration, together with Ruff
and `git diff --check`. P0.3/P0.4 remain evidence runs, not promotion gates, by the user's explicit
decision; correctness, integrity, WAL and concurrency gates remain mandatory.

## P1.2/P1.3 — isolated Python-path optimizations

Status: **integrated on `feature/v0.0.2`**.

- D-01 is published as `perf/v002-d01-header-peek` at
  `1239a0e519295b9f2b127d88d3155c7fdd352daf`. It peeks the three visibility fields with one
  header unpack for rejected versions and retains full `RecordHeader` construction for accepted
  versions. It was promoted in `b23bcbc` + `07dfb02`; the full focused heap suite and Ruff passed.
- D-04 is published as `perf/v002-d04-index-version` at
  `e898fe766b29e61c867b44679a5f0f1e77b724d9`. It reuses the heap version already validated by
  the index path while preserving ended/changed fallback filtering. It was promoted and hardened
  in `2e6bbf9` + `f6e7531` + `873f419` + `bc3ede4`: only automatic PK seeks retain the decoded
  version, while endpoint/general exact indexes keep lazy ref reads so memory cannot grow with hub
  degree. The focused PK/endpoint/visibility suites and Ruff passed.

The D-02 tail-first endpoint prototype was not retained: with a corrupt visible duplicate near the
head, it could accept a later row and hide the corruption currently surfaced by the public scan
order. Its strict replacement is integrated as `49a9b03` + `e26af74` (with the conservative quota
comment aligned in `fb984a7`). A transaction/snapshot/table-private canonical-prefix cursor keeps
head-to-tail order, peeks a whole page before returning a candidate and fully revalidates only the
requested physical row. Memo, table slot, identity/ref and visited-page proof are admitted under a
64 MiB/1M-entry per-handle cap; accounting and registry operations share an injected `RLock`, while
heap I/O stays outside it. Saturation, stale state or a concurrently active cursor uses the
unchanged canonical lookup; stored-data errors remain fail-closed. This amortizes repeated endpoint
checks from `O(E*N)` to `O(N+E)` while the working set fits, with the fallback worst case stated
honestly. The same reader was proved before and after a foreign-process reconciliation without
crossing its old snapshot. P1.5 consumes this internal port; the promoted P1.5/P1.6 outcomes are
recorded below.

## P1.5 — bounded lazy owner landing (D-03)

Status: **integrated on `feature/v0.0.2` as `1e2997e` + `13a5bda`; R1 maintained,
threshold/R2 not selected**.

Traversal, untyped traversal and relationship scan now resolve only the node identities named by
surviving edges through the D-02 internal identity door. A transaction reuses requested landings,
including misses, without building and retaining every visible row of the landing table. The
owner overlays remain complete for `changed`, `ended`, inserted rows and `PendingRowRef`.

All retained state shares a per-handle ceiling of 32 MiB and 131,072 entries: transaction memo,
table slots, views, overlays, the complete invalidation fingerprint and decoded results are
reserved before publication. The first candidate was rejected before integration because repeated
updates of one ref could retain an `O(k)` raw fingerprint behind an `O(1)` reduced-overlay charge,
and because a view retained its statement `_Context`. `13a5bda` charges every fingerprint entry
plus its encoded payload at a conservative multiplier and supplies statement context only to the
individual lookup. Heap I/O stays outside the shared `RLock`; settlement can retire an active view
and releases its remaining charge when that lookup exits.

`DELETE` is an explicit accounting case: it remains in the fingerprint and pays the fixed entry
charge, while its empty tuple is only an absence marker and is not passed to `encode_tuple`. The
regression proves that a view containing a delete remains admitted and reusable (the second
traversal performs zero landing decodes), while the settlement tests return the shared accounting
to zero.

On the focused wide-table proof, 96 nodes and nine edge landings naming two distinct identities
produce zero landing-table scans and two payload decodes; repeating the traversal in the same
transaction produces zero new decodes. While D-02 and D-03 remain inside their quotas, repeated
landing work is amortized from `O(E*N)` to `O(N+E)`. If both quotas saturate, canonical fallback
can still cost `O(N)` for each distinct requested identity. A full-scan R2/threshold was therefore
not selected without the evidence and separately admitted memory design required by the plan.

The integrated focused batch passed 107 tests, plus Ruff and `git diff --check`.

## P1.6 — projected incident relationship endpoints (D-05)

Status: **integrated on `feature/v0.0.2` as `bd12a5c`**.

`DETACH DELETE` still walks every visible relationship and reconstructs every overflow payload,
but it retains only the two fixed endpoints instead of materializing all properties and a complete
`HeapVersion`. The materializing and validation-only modes share one value parser. Schema version,
payload length, tags, complete nested shape, trailing bytes and `header.previous` keep the previous
validation order; corruption in a visible non-incident relationship remains fail-closed before
any partial delete is handed to the transaction. Pending/deleted overlays and self-loops retain
their prior semantics.

The original requirement for a real-card >=10% census was superseded by the user's later decision
to remove performance gates; no such census is claimed. Promotion rests on quality: 250 focused
tests passed after integration, two independent adversarial reviews passed, and an additional
deterministic differential probe compared 44,000 malformed payloads without finding a difference
in exception type, message, details or cause. Ruff and `git diff --check` also passed.

The versioned synthetic decoder benchmark on Python 3.13.1 and the integrated SHA used a 6,321-byte
payload and measured median `107452.15 -> 78707.15 ns/row`, or `1.365x`, over seven rounds of 2,000
iterations. This is directional evidence for this decode/materialization component only. Strings
still allocate temporary text to validate UTF-8, and the complete operation remains
`O(|R| + bytes of visible payloads)`; no end-to-end `DETACH DELETE` speedup is claimed.

## Vector lane — exact fenced live cardinality (D-12)

Status: **integrated on `feature/v0.0.2`**.

The vector planner still enters the existing page-0 exact-view fence, but after the first canonical
count it no longer walks and decodes every index entry on each search. Local warm updates maintain
the count with the durable entry identity `(key, ref)`; foreign page-0 movement, RESET, REMOVE,
rebase and graph retirement discard it and force one exact recount. The first implementation was
rejected adversarially because key-mismatched no-op tombstones and same-ref/different-key inserts
proved that `ref` alone is not an index-entry identity. The promoted sequence is `df09c2e` +
`6f6b410` + `16fbc0a`; the last hardening ensures a derived-count inconsistency is discarded and
can never fail an already durable commit. The incremental HNSW update remains intact, so the change
does not trade the removed count walk for an `O(N)` rebuild after each local write.

## P1.7 — single WAL image per materialised page (D-09)

Status: **integrated on `feature/v0.0.2` as `c3ef29f` + `69368c3`**.

- A page this process materialised is now logged from an independent copy of its resident
  frame (`Page.copy()`), stamped with the predicted commit number and encoded once, instead of
  encode → decode(verify) → encode. The frame stays provisional until the WAL barrier returns.
- A page a collaborator pre-staged as bytes keeps the full `decode_page(verify=True)` before
  anything is stamped; a corrupt pre-staged image is still refused before the log is touched.
- A WAL segment roll re-stamps and re-encodes the already validated page values; it no longer
  decodes the logged bytes again. `apply_page_image` is untouched and still decodes with
  verification after the barrier (A22).
- The new generator is proved byte-identical to the old one, before the flush, over a page
  corpus with free and relocated slots, compaction, flags, reserved word, `next_page`, page LSN,
  sequence, overflow/index page types and both page sizes; mutants that drop a copied field or
  share the payload buffer are caught. Gain is measured only by the synthetic microbench
  (`tools/perf_round/microbench_wal_image.py`, `[MEDIDO-micro]`); the hold fraction stays
  `A_MEDIR` under the D-26 instrumentation.

The isolated synthetic receipt on a loaded Python 3.13 environment measured p50
`5009 → 2031 µs/page` for an 8 KiB/40-slot image (`2.47x` for this materialisation step). This is
not a whole-commit claim. Eight focused tests, six field/share mutants, the 75-test focused
commit/WAL integration batch, Ruff and diff-check passed; no Pulse data was accessed.

## Test cadence

- Each implementation gets focused tests for its changed contract and nearby regressions.
- Related low-risk implementations may be accumulated before broader storage/query regression.
- The full suite, multiprocess quality gates and cold verification run at milestone boundaries, not
  after every patch.
- Performance measurements never replace correctness, corruption, recovery or concurrency gates.

## Milestone log

| Date | Milestone | Result |
|---|---|---|
| 2026-09-02 | Version bump and branch bootstrap | packaging and CLI version tests passed |
| 2026-09-03 | P0.1 H5 diagnosis and page-0 repair | 14 isolated multiprocess tests, neighboring index/recovery suites and Ruff passed; no live board accessed |
| 2026-09-03 | P0.1 Pulse-flow relevance audit | pinned Community source contains no production vector-index rebuild call; H5 same-handle fence is not on the backfill path |
| 2026-09-03 | P1.1 D-26 integrated | `7aa410d` + `59020a4`; focused commit/metrics/containment/catalog tests and Ruff passed; foreign gap is not charged to OCC |
| 2026-09-03 | P1.2 D-01 integrated | `b23bcbc` + `07dfb02`; full focused heap suite and Ruff passed |
| 2026-09-03 | P1.3 D-04 integrated | `2e6bbf9` + `f6e7531` + `873f419` + `bc3ede4`; focused PK/endpoint/visibility suites and Ruff passed; high-cardinality indexes remain lazy |
| 2026-09-03 | P1.4 D-02 canonical-prefix locator | `49a9b03` + `e26af74` + `fb984a7`; hard-capped per handle, atomic registry/accounting, no heap I/O under its guard, canonical fallback on saturation/stale/busy; isolated 16/16, grouped 60/60, integrated 35/35, multiprocess old-snapshot and adversarial concurrency proofs, Ruff/diff-check passed |
| 2026-09-03 | P1.5 D-03 bounded lazy landing | origins `e977285` + `7b152a9`, integrated as `1e2997e` + `13a5bda`; first candidate blocked for uncharged fingerprint/context retention, final R1 fully admitted; `DELETE` pays fixed fingerprint entry without encoding its empty marker; integrated 107/107, Ruff/diff-check passed; R2 not selected |
| 2026-09-03 | P1.6 D-05 endpoint projection | origin `7751ea1`, integrated as `bd12a5c`; 250/250 post-integration, two adversarial reviews and 44,000-case malformed differential passed; integrated microbench `1.365x` only for the synthetic decoder, with full fail-closed validation and unchanged `O(|R|)` scan |
| 2026-09-03 | P0.2 fail-closed primitives | receipts, authenticated copy and independent-series runner integrated at `c276dec`; 31 focused tests and static checks passed; driver completion is recorded in the next row |
| 2026-09-03 | P0.2 authenticated Pulse card driver | integrated at `be286fa` + `2d43d75` from `perf/v002-p0-card-driver@e8a6be0`; 46 focused tests and static checks passed; synthetic RAW/instrumented lifecycle smokes passed without touching the live board; P0.3/P0.4 execution remains pending |
| 2026-09-03 | P0.3 profiler/census tooling | published on `perf/v002-p0-census@222a854`; endpoint locality, dynamic vector activity, guarded py-spy capture and reconciled read-only census implemented; combined milestone regression 98/98 passed; real corpus execution waits for the live backfill to drain |
| 2026-09-03 | P0.3 profiler/census hardening | `89cb893`; exact per-page endpoint weights replace the 100k-hit cap, direct-interpreter preflight prevents wrong-PID attach; focused 28/28 and Ruff passed; no Pulse data accessed |
| 2026-09-03 | Vector D-12 integrated | `df09c2e` + `6f6b410` + `16fbc0a`; exact page-0-fenced count becomes hot `O(1)`, entry identity is `(key, ref)`, local HNSW updates stay incremental, foreign/recovery changes fall back to an exact walk; integrated focused vector/concurrency suite, Ruff and diff-check passed |
| 2026-09-03 | P1.7 D-09 integrated | `c3ef29f` + `69368c3`; local WAL image is copied/stamped/encoded once, external bytes remain fully verified, retarget cache is attempt-bound; 8 focused + 75 commit/WAL neighboring tests, six mutants, Ruff and diff-check passed |
| 2026-09-03 | Cold read-only census hardening | `b445736`; v2 receipts retain raw before/after inventory and admit only the exact empty participant lock under the independently authenticated Grafx binding. A disposable real-process proof added one lock with total bytes unchanged (`41,944 -> 41,944`) and clean `verify("all")`; 43/43 focused tests, Ruff lint/format, `py_compile`, diff-check and local/Nexus adversarial reviews passed |
| 2026-09-03 | Grouped P1 regression closeout | the broad run ended with `9,628 passed, 17 skipped, 1 failed`; the sole failure was a missing docstring on nested callback `count`. Commit `8f0af84` fixed only that documentation and the affected slice passed 66/66. The full suite was not rerun after the behavior-neutral fix |
| 2026-09-03 | Multiprocess quality gate | 500/500 operations acknowledged in 46.6 s, 510 stored rows including ten contention seeds, 44 retryable conflicts absorbed, zero loss/duplicate/phantom/torn read, and clean live/reopen `verify("all")` |
| 2026-09-03 | Finite P2 selection | `none`; P2-ID, P2-DIRTY and P2-VAC did not meet their frozen post-P1 evidence triggers, so no structural change was implemented by hypothesis |
| 2026-09-03 | P2-ID custom exact indexes | `2fa81b1`; transactional `CREATE INDEX`, `Database.create_index()` and maintenance delegation add ordered compound keys and deterministic sizing. The ACTIVE receipt includes certified nonce/metadata/horizons; query equality retains NULL/numeric semantics, hostile logical descriptors cannot execute through inventory, and schema-derived vector/proximity indexes remain visible in v2. Composed gates passed 394/394 + 281/281 with live/cold verification, Ruff, compile, diff-check and no adversarial blocker remaining |
| 2026-09-03 | P2-ID growth-only foreground rehash | `72694bd`; exact indexes rotate through a distinct durable shadow, retaining one STALE descriptor; v1 coactivation builds once, recovery converges old-or-new, long-lived/read-only handles adopt foreign ACTIVE authority, and unchanged catalog bytes avoid full inventory/header scans. Focused gate 163/163 plus two adversarial reviews passed |
| 2026-09-03 | Storage metric-contract drift | `0374dc9`; the retained-memory estimate already emitted and documented since `283cffa` is now present in the storage-core executable roster. Test-only correction; the focused contract/catalog batch and Ruff passed |
| 2026-09-03 | Post-round D-29(c/d): closed-provider corpus proof memoized | `NativeCrc32c` proved the corpus (108 inputs, 169,529 bytes, 8 published vectors) against the pure reference on every construction, ~34 ms per `connect()` on the 3.13.1 box `[MEDIDO-micro]`; and `install()` replayed it again in the domain door (another ~30 ms); the successful proof of a `load_provider` result is now memoized per process under the provider's strong identity in BOTH doors, so a repeat `connect()` cycle costs ~0.05 ms for both. Each closed module/attribute slot retains only its current wrapper/proofs, raw functions match strictly by `is`, and one adapter lock makes concurrent first opens run each independent corpus once. Refusals are never memoized, a replaced function proves again, and injected providers and `verify_runtime=True` keep their per-construction/per-call semantics |

## Post-P1 bounded queue — items 1–9 checkpoint

Status: **approved on `feature/v0.0.2` on 2026-09-03**.

The finite queue recorded in `EVOLUTION_PLAN_CODEX.md` is integrated: constant-state scalar
aggregates; bounded checksum-provider proof memoization; stable top-N; traversal budgets; retained
memory and descriptor telemetry; hit-driven ANN top-k; exception-safe single-flight cold page
loads; cursor plus bounded blocking-query spill; and atomic streaming `executemany`. The promoted
commits and the semantic boundaries of every item are recorded in that central plan.

The final composed quality gate covered query, bulk/public boundaries, buffer/single-flight,
checksum, descriptor telemetry, the metrics catalog, configuration, packaging and import
boundaries and passed **2,916/2,916 tests**. The preceding composed run found one real integration
regression: a buffer assembled with an enabled metrics sink kept emitting after that sink was
disabled. `60fc96b` makes emission honor the current disabled state while retaining the existing
fail-closed rule against unsafe late enablement; its focused regression passed before the final
gate. Ruff lint, `compileall` and `git diff --check` also passed.

Checkpoint SHA: `60fc96b`. Reproducible grouped invocation:

```text
python -m pytest tests/query tests/api/test_executemany.py tests/api/test_public_query_boundaries.py tests/api/test_public_boundary_concurrency.py tests/storage_core/test_buffer_pool.py tests/storage_core/test_buffer_pool_single_flight.py tests/storage_core/test_checksum.py tests/storage_adapters/test_descriptor_cache_telemetry.py tests/observability/test_metrics_catalog.py tests/foundation/test_config.py tests/foundation/test_packaging.py tests/test_import_boundary.py -q
```

Collection and result: `2,916 collected / 2,916 passed`.

The repository-wide Ruff formatting baseline still contains 256 historical files that would be
reformatted. It was not mechanically rewritten in this performance milestone. New item-8 files
and the directly changed hot paths passed scoped format checks; this distinction is intentional
and no repository-wide formatting success is claimed.

The earlier `Finite P2 selection = none` row remains the outcome of its frozen evidence gate. A
later explicit product decision authorized items 10–13 only after this checkpoint: P2-ID with
sizing/rehash and identity/secondary indexes, MVCC vacuum/compaction, delta/chunked/physiological
WAL, and a native codec beyond CRC. That authorization does not waive each item's ADR,
migration/compatibility contract or recovery and concurrency quality gates; it only removes the
need to repeat the prior performance-selection gate.

## Item 10 / P2-ID — identity and exact-index lifecycle

Status: **complete on `feature/v0.0.2` through `72694bd`**.

The milestone now covers catalog-v2 ACTIVE projection, unsigned record-identity lifecycle,
statement-stable endpoint routing, explicit automatic activation, transactional custom exact
indexes and foreground growth-only rehash. Rehash uses a distinct immutable shadow, retains the
immediate predecessor as STALE, leaves older files as non-reused orphans and publishes only after
the normal first/second OCC plus a physical durability barrier. Catalog-v1 automatic indexes are
coactivated and resized in one build; process-local custom v1 definitions are refused.

The read boundary also adopts foreign catalog authority without turning ordinary DML into an
`O(total_indexes)` inventory scan. A bounded WAL delta identifies catalog writes; conservative
checkpoint/large-delta paths compare the immutable persisted catalog image and skip header opens
when it is unchanged. Missing or malformed selected generations remain latched fail-closed across
later begins. The focused ten-module gate passed 163/163 tests, including crash recovery,
multiprocess `strict`/`generation`, read-only adoption, cold reopen and verification; Ruff,
`compileall`, diff-check and two independent adversarial reviews passed. These are quality results,
not a new throughput claim. Item 11 (MVCC vacuum/compaction) is the next authorized boundary.

Reproducible 163-test invocation:

```text
python -m pytest tests/index/test_identity_activation_domain.py tests/txn/test_bounded_read_view.py tests/txn/test_index_rehash_preparation.py tests/api/test_index_rehash.py tests/api/test_index_rehash_recovery.py tests/api/test_index_rehash_multiprocess.py tests/api/test_maintenance_facade.py tests/api/test_cross_process_visibility.py tests/api/test_read_view_own_exemption_multiprocess.py tests/storage_core/test_catalog_store.py -q
```

## Item 11 / P2-VAC — MVCC bloat and vacuum

Status: **complete on `feature/v0.0.2` as `6d3e62c` + `75e799f`**.

`db.maintenance.bloat(table=None)` now performs a deterministic, header-only census at a
non-pruning observation of the checkpoint-capped recyclable horizon. TTL-stalled reader records
remain conservative pins. It reports per-table and aggregate page, slot,
stored/ended-version, overflow-reference and record-slot-byte counts. Eligible plus retained is
an exact partition of ended versions; live and provisional versions remain only in
`stored_versions - ended_versions`. Overflow-page bytes and slot-directory bytes are explicitly
outside the byte estimate. The immutable report keeps `vacuum_safety_established=False`, so no
caller can mistake the WAL-recycling horizon for proof that physical deletion is safe.

The slice adds no WAL record, capability, page-format mutation, slot reuse or background work.
It is available on read-only handles and follows a fresh-view/recovery latch that refuses dirty
state instead of flushing it. The non-pruning horizon leaves stale, empty and temporary reader
artifacts unchanged. The affected gates passed 196 focused API/lifecycle/hostile-boundary tests,
plus buffer/read-view/coordination tests, scoped Ruff, `compileall` and `git diff --check`. This is
observability, not a throughput claim.

The destructive half was subsequently authorized under that exact restricted contract and is now
`db.maintenance.vacuum(table=None, *, confirm_quiescent=False, max_versions=None)`. The exact
`confirm_quiescent=True` assertion means every other Grafx process/handle, including pre-fence
binaries, is stopped for the whole call. Catalog v2 must already be active. A first metadata
transaction publishes required capability `heap_reclaim_v1`; older builds then reject the unknown
bit. One ordinary WAL-before-data transaction advances a monotonic heap-global snapshot floor,
relinks retained chains, frees and compacts eligible inline slots, and reconciles every ACTIVE
index of the selected tables at the same horizon. New transactions below the floor fail retryably
with `GrafxSnapshotReclaimed`.

The implementation deliberately does not infer safety from reader TTL, run online/background,
truncate the heap, reclaim overflow chains or reuse durable page/slot/`RecordRef` identities.
`max_versions` bounds only deterministic heap candidates; index reconciliation remains complete
before any selected ref disappears. Crash injection covered the WAL barrier, heap page images,
index application and commit-state publication. Live/cold query, vector search, verifier,
capability downgrade fence, zero-write repeat, quota drain, read-only refusal and rehash-shadow
lifecycle all passed. The grouped result was 490/490 directly affected tests plus 736/736
transaction/public-boundary tests, Ruff, format, `compileall` and diff-check. Four failures in the
unfiltered transaction command were reproduced unchanged on baseline `6d3e62c` and excluded by
exact node id from the delta gate; they are not attributed to this milestone. Nexus handoff
`hof_df837d11c96a430786856895c7f7c744` was corrected after adversarial challenge and then verified
PASS: chain relinking and overflow retention are covered, ACTIVE reconcile shares the reclaim
commit, and an interrupted rehash artifact is never resumed or promoted.

The expected gain is bounded and honest: dead tuple payload bytes disappear from churned inline
pages, and future walks skip freed slots before header/tuple decoding. File length, historical page
chains and slot directories remain, so this milestone reduces constants and resident payload but
does not yet remove every `O(history pages)` path.

## Item 12 / D-34 — full-page WAL compression v1

Status: **completed and published on `feature/v0.0.2` as `24f2f63`**.

The finite milestone is full-post-image compression, not delta or physiological redo. After an
explicit v1-only transaction publishes catalog capability `wal_record_v2`, a later no-roll commit
may encode a `WRITE_PAGE` as WAL v2 with REQUIRED + PAGE_IMAGE_ZLIB1 flags when zlib level 1 is
strictly smaller. The file/page prefix stays clear for bounded CE-3 target extraction; full preflight
uses bounded exact inflation and the ordinary page checksum before mutation. The final compressed
record lengths are authoritative for `max_wal_batch_bytes`.

Raw batches that require `SEGMENT_HEADER` remain v1. This avoids a size/roll/retarget feedback loop
and preserves the exact terminal commit CSN without a new append protocol. Incompressible pages also
fall back byte-exactly to v1. Unknown required v2 semantics refuse strict read, append, recycle and
recovery as schema mismatch without changing WAL bytes. The catalog capability remains a downgrade
fence after record recycling.

The adversarial review retracted physical chunking: a real kill between multiple appends leaves
complete effects without COMMIT, which current recovery intentionally refuses as ambiguous because
effects may already have reached data pages. `_undo_append` covers returned exceptions, not process
death. Chunking and physiological WAL are therefore NO-GO in this milestone. Block-delta is deferred
until a separate ADR supplies pre-image/base-LSN authority, full-page-write checkpoint rules and the
four-way idempotent redo decision. This decision is recorded in
`docs/architecture/WAL_PAGE_COMPRESSION_V1.md` and verified Nexus handoff
`hof_44e3743219b2476a95ecc02d00ac0eb6` revision 3. Performance ratios are evidence only, never an
acceptance gate.

An in-memory indicative sample with 8 KiB pages and one 50-row transaction produced three
compressible page records: the final batch occupied 7,966 bytes versus 31,510 bytes for the same
records with v1 full images, a 74.72% reduction (`3.96x` byte ratio). This sample is intentionally
not a throughput claim or SLO; page occupancy and payload entropy determine the result, and segment
roll batches keep the v1 size by contract.

The grouped quality gate passed 1,496/1,496 selected cases after nominally deselecting four
historical failures reproduced outside the changed path. Focused tests, Ruff, `compileall` and
`git diff --check` also passed. Design handoff `hof_44e3743219b2476a95ecc02d00ac0eb6`
revision 3 and implementation review `hof_b192c61ffcea4297852c7b2b8f00064b` were both verified
PASS; the latter's final recommendation made unknown v2 records skippable only under the exact
explicit `SKIPPABLE` grammar.

## Item 13 / D-19 — byte-identical NumPy page codec

Status: **completed on `feature/v0.0.2` as `b720f7e` + `d644dc3`**.

`DatabaseConfig(codec="numpy")` explicitly binds `NumpyPageCodecV1` per database. The default
remains `"pure"`; there is no automatic selection, and an unavailable optional dependency refuses
instead of falling back. Both implementations emit and consume exactly page format v1, so this
item adds no durable capability, migration, WAL grammar, cache authority or concurrency state.
The NumPy adapter packs directories from 16 slots and validates them from 96 slots. Smaller pages,
invalid geometry, checksum failures and provider exceptions return to the pure codec, which remains
the sole authority for public refusals. `Page._from_decoded` is the canonical post-validation
assembly path for both codecs.

The differential contract covers exact bytes, freed/compacted slots, hostile/out-of-range u16
entries across NumPy 1.x/2.x behavior, 1,000 seeded mutations at each of 4, 8 and 32 KiB, persistent
write/reopen through the other codec and two simultaneous in-process receipts. The receipt names
the per-instance `implementation` separately from the effective process-wide
`process_checksum_implementation`. A compiled C/Rust extension remains NO-GO without a toolchain
and wheel matrix; a future scan kernel must be injected per instance, never installed globally.

The focused set collected and passed 556/556. The broader storage/recovery run collected 1,531 and
first exposed one module-level mutable catalog mapping; `b720f7e` made all capability/tag tables
immutable and the affected 661/661 correction slice passed. The 1,293-case transaction/API
partition passed 1,289 and retained exactly the four historical failures already reproduced and
excluded by the previous milestone; no new failure remained in the codec path. Ruff, scoped
format, `compileall` and diff-check passed. Initial adversarial review
`hof_fd26b1d96ce74dac80c9671e00a55999` and final six-blocker review
`hof_19b2cca000214564832d7ff15cf77618` were verified PASS.

On the recorded Windows/Python 3.13 host with native CRC, the final 200-slot/8 KiB microbenchmark
measured encode `164.85 -> 53.87 us` (`3.06x`) and decode `150.48 -> 82.12 us` (`1.83x`). These are
component ratios only; end-to-end commit latency still includes unchanged WAL, durability,
coordination and index work.

## Item 14 — bounded query, heap, transaction and vector hot paths

Status: **completed and published on `feature/v0.0.2` as `9603115`**.

This milestone removes repeated fixed work without changing page/WAL formats, OCC, durability or
multiwriter/multireader semantics. Heap and catalog bootstrap now avoid repeated device
`exists/page_count` calls, but still pin and validate the resident header on every fast-path entry;
catalog/index synchronization runs only when CE-3 proves an authority change. A statement builds
one ACTIVE-index projection, and the second control-record read is skipped only while the standing
pin still proves the same observation. Catalog read views are retained only in the production
composition where every catalog mutation is WAL-logged; direct non-WAL VectorEngine lifecycle
operations remain refused there.

Query parsing and prepared-plan views use per-`Database`, strongly owned and bounded LRUs (256
parse entries, 128 plan entries and 128 public memo entries). Cached values contain structural
plans only, never storage or transaction authority, and hostile/unowned plans still cross full
validation. Batch slots use direct struct pack/unpack, index bucket page counts are lazy, scalar
decoding is preplanned, and high-water inspection reuses an already accepted header.

The vector lane adds header-first exact search, block validation/decode, optional
`PreparedVectorMath` and less allocation in distance/beam processing. The optional math protocol
preserves the host-adapter fallback. Header-first exact search deliberately does not decode payloads
of rows already excluded by snapshot visibility or filters; corruption in those unreachable
payloads is still found by verifier/scan, while every admitted row remains fail-closed. This is a
change in detection timing/read surface, not a relaxation of durable verification.

Measured component evidence from the reviewed Nexus handoff
`hof_47afe28f5f004696a473718eeca9d980`:

- vector component validation: `2.59x` to `3.19x`, depending on dimension and f32/f64;
- HNSW N=500/d=64/cosine: build `1.77x`, search `1.93x` at ef64 and `2.09x` at ef320, with
  identical ranking;
- exact vector search, 800 rows/d=128: `2.99x` with 10% visible (800 -> 80 tuple decodes) and
  `1.47x` with 100% visible; sample insert path `1.20x`.

The query-cache gains remain estimates from the design survey (roughly 19--28% for prepared work,
plus about 12.9% for the public-view component), not fresh end-to-end claims. Quality evidence:
3,459 tests passed in Python 3.13 with NumPy before two historical contract-roster mismatches were
corrected; 897 passed and 3 skipped in Python 3.11 without NumPy before the same roster correction;
21/21 mutants were killed. The two historical mismatches were then fixed and their 29-case slice
passed, alongside 247 heap, about 293 catalog/storage/index, 378 query/public, 74 vector and 29
reader/cross-process focused cases. Ruff lint, compileall and diff-check passed. Whole-repository
`ruff format --check` still reports 29 historically unformatted tracked files; no unrelated bulk
format was performed.

## Scale-removal batch 1 — recovery, resident state, schema growth and index commits

Status: **completed through `592fd22` on `feature/v0.0.2`; further scale work continues in
separate milestones**.

This batch was ordered by implementation precedence: remove cheap fixed and linear work first so
later validation also becomes cheaper. It preserves the existing page/WAL formats, first and
second OCC rules, durability publication, and multiwriter/multireader model.

- `c1d537e` coalesces safe repeated page effects only in originally page-only replay and places
  data-file barriers before commit-state publication. `2afd876` reuses one passage-bound,
  content-checked preflight so the accepted page image is decoded once; a mixed replay projected
  into a page subplan remains sequential.
- `9ee51f3` fuses local exact-name observation and the bounded two-slot control read without
  changing the frozen storage port or weakening strict identity checks.
- `2be59c9` replaces full resident-frame scans with revalidated dirty candidates. At 8,192 clean
  frames, an empty flush fell from `3.99–5.24 ms` to `3.21–7.00 us`; the representative 600-row
  query moved by a noisy `+0.668 ms` (`+1.93%`), an accepted bounded bookkeeping cost.
- `210b685` retains a defensive heap directory `table_id -> slot` hint. A 200-table
  `find + write + find` sample measured `663.21 -> 107.22 us` (`6.19x`). Every hit rechecks the
  current table-id; stale hints use the canonical zero-copy scan and can never overwrite another
  table's extent.
- `c12e67c` raises the lazy per-database descriptor ceiling to 256 while retaining the explicit
  override. In a 192-artifact sample, cap 64 produced 384 misses/320 evictions; cap 256 produced
  192 misses/zero evictions/192 hits (`1.97 -> 0.96 s`, directional only). Several large databases
  in one process may lower the ceiling for descriptor headroom.
- `8604661` makes tombstone-backlog metrics one exact seed walk followed by O(1) deltas, with
  conservative invalidation on rebase/reopen/failure. It also replaces quadratic multiset
  comparison by `Counter`; a 1,000-change sample measured `291.53 -> 8.77 ms` (`~33x`) while
  preserving duplicate and missing-effect refusals.
- `592fd22` avoids the unconditional `list_files("index/")` inventory when every relevant ACTIVE
  definition already has its exact registered object. Any unresolved generation still performs
  one canonical inventory and the final fresh physical certificate remains mandatory before WAL.

Validation was deliberately grouped rather than rerunning the hours-long global suite after each
patch: 195 focused recovery/transaction cases, 536 heap/storage/configuration cases, 73 index
metric/multiset cases plus 9 authoritative-facade inventory cases, discriminating mutation/failure
tests, Ruff, compile checks and diff checks passed. These
component measurements are receipts, not throughput gates.

## Scale-removal batch 2 — accumulated writes and repeated query boundaries

Status: **completed through `82bd176` on `feature/v0.0.2`; index-build and commit-stamping work
continues in later isolated milestones**.

This batch removes work that grew with transaction length, schema size or relationship-table size
without changing WAL/OCC, page format or the multiwriter/multireader contract.

- `02e6f41` incrementally folds only new primary-key row intents. Rewrites/truncation rebuild from
  the canonical reducer, and statement-held changes overlay the transaction fold without sharing
  state across tables or transactions. At 1,000 accumulated inserts the measured hot path moved
  from `1,225.93 ms` to `18.25 ms` (`67.16x`); the 250-row sample moved `70.33 -> 4.57 ms`
  (`15.38x`). A 320-operation seeded differential found and corrected one first-seen-order drift
  before integration.
- `e0e18f8` removes Python `Catalog` object identity from the prepared-plan key while retaining the
  exact immutable catalog bytes, index definition/freshness picture and transaction dirty-table
  set. This is structural hardening, not an independently measured speedup: after `3b72d4a`, the
  old identity-based key already hit 19 of 20 times because the same catalog object remained
  resident. The earlier `1.11x–1.35x` estimate belonged to the CAT-1 adoption-churn problem solved
  by `3b72d4a`, and must not be attributed to `e0e18f8`.
- `855c9cf` memoizes the complete ACTIVE index projection on its owning catalog value and
  invalidates it on every table, space, capability or index-authority mutation. `3b72d4a` keeps
  that object resident only across a same token, a proven own publication or a complete CE-3
  interval without catalog pages. Twenty steady-state statements perform zero catalog
  serialize/deserialize, cache-drop or plan rebuild after baseline. A 64-table prototype measured
  the CAT-1 component `31.43 -> 10.40 ms`; CAT-2 added a directional `1.24x` over that result.
- `82bd176` lets a fresh endpoint index serve an exact one-hop traversal below a streaming
  `LIMIT`, including forward and reverse direction. It preserves the canonical prefix in focused
  equivalence tests and touches fewer than all six candidates for `LIMIT 1`; blocking `ORDER BY`
  and `DISTINCT`, wider shapes and stale authority remain on the grouped scan. The discovery
  sample at 2,000 edges measured `2.56x`, with no claim beyond this bounded plan shape.

Focused validation was accumulated: 58 PK/regression cases, 75 catalog projection/store cases,
34 read-view/cross-process/plan cases and 155 traversal/limit/optional/overlay cases passed, plus
scoped Ruff and diff checks. These are structural/component receipts; marginal timing variance is
not used as a gate.

## Scale-removal batch 3 — generation build, recovery, commit and checkpoint

Status: **completed through `aaf72f2` on `feature/v0.0.2`; the next milestone targets bounded
vector filtering and the diagnosed relationship-query scale cliff**.

This batch removes repeated work from foundational write/recovery paths first, so later integration
and regression runs exercise the cheaper implementation. It does not change page/WAL format,
multiwriter/multireader, OCC, visibility or durability rules.

- `c1f1a1f` gives a new empty exact-index generation an ephemeral first-fit directory while it is
  being materialized. RESET and detached shadow builds no longer rescan the growing physical image
  to locate space, but the final pages remain byte-identical to the canonical path, including
  tombstones and removals. Measured build time changed from `27.68 -> 13.48 ms` at 64 entries
  (`2.05x`), `77.98 -> 17.54 ms` at 128 (`4.45x`) and `323.00 -> 34.24 ms` at 256 (`9.43x`).
- `b8eff8a` makes concrete recovery-port discovery inspect attributes statically before invoking a
  dynamic fallback. A native cold recovery now performs exactly one authoritative WAL walk rather
  than an eager damage scan plus replay walk; proxy/custom wrappers retain dynamic `__getattr__`
  behavior and missing ports retain the typed refusal.
- `26baa86` groups immutable page-local MVCC stamps once per materialized attempt. The previous
  `O(P x R)` page-by-row inspection becomes `O(R + A)`, and publication retargeting becomes `O(A)`,
  where `A <= 2R`. At 128 pages/intents this removes 16,384 inspections in favor of at most 256
  grouping/retarget operations. Byte/WAL equivalence covers insert, delete, same-page and
  cross-page update, file collision and retargeting; the original sorted page order is preserved.
- `46fa9fe` classifies same-handle heap changes by an ephemeral structural signature. Payload,
  MVCC, LSN and durable allocation-hint changes no longer evict extent/tail/endpoint locators;
  growth, relink, owner/topology changes and foreign read views still do. After warming, sixteen
  one-row commits caused zero directory walks, tail walks or false structural-epoch increments.
- `aaf72f2` samples `reader_horizon()` once in both checkpoint protocols and reuses that exact
  observation for reader presence and the recyclable horizon. The coordinator scan falls from two
  to one per checkpoint while the known freshly published checkpoint LSN replaces a redundant
  state reread.

Validation remained grouped and risk-proportional: 27 independent index-build cases, focused cold
recovery/refusal cases, 70 transaction materialization cases, 117 heap/commit/redo cases and 11
checkpoint/recycling cases passed, with scoped Ruff and diff checks green. A pre-existing catalog
page-type failure in `test_three_participants_writing_one_catalog_page_cannot_all_commit` remains
explicitly outside these deltas and is not presented as passing.

## Scale-removal batch 4 — selective vectors and registered identities

Status: **VEC-4, CAT-5 and index-seek stability completed through `5938a03`; both transparent EDGE
prototypes were rejected and reverted**.

- `c77d414` lets an exact vector query pass an immutable, engine-sealed candidate certificate into
  the vector engine only when it belongs to the same owner, space, table, column and `read_lsn`.
  Selected rows are revalidated against heap MVCC and `(key, ref)` is authenticated once per
  touched bucket. Any incomplete proof, NULL, deletion, duplicate, wrong reference or metadata
  drift falls back to the canonical walk before scoring or output. The conservative gate is
  `4R <= min(E, B)`: in the focal case the path used 4 heap reads instead of 64 and performed zero
  full `index.walk()`. This is a structural selective gain, not an `O(K)` claim; with fixed bucket
  count the authentication remains `Θ(U·E/B)`, and corruption outside visited rows/buckets remains
  covered by full scan/`verify`.
- `40552df` obtains v2 artifact nonces directly from their already registered immutable definitions.
  Only legacy nonce-zero artifacts open and validate the header; alternate/narrow registries retain
  that fallback. The allocator sample at 64 generations measured about `352 -> 61 ms` (`5.8x`),
  while the sampled DDL path measured `1,085 -> 789 ms` (`1.37x`). These are component receipts.
- `b236697` attempted to move local equalities below Cartesian scans and was rejected by adversarial
  validation. It changed the observable error channel, could hide an inner scan failure and could
  turn `GrafxQueryBudgetExceeded` into a committed relationship write. `030b39d` removes the
  transformation and freezes those cases in regression tests. The measured cliff remains real:
  two unindexed endpoints produce `N + N²` logical rows.
- `bf8ed4e` then tried to preserve logical work while replaying a completed inner snapshot. The
  component sample was promising (`0.160 -> 0.044 s`, physical inner scans 80 -> 1 at the same
  6,480 logical rows), but the optimization violated CONTRACT A63 under buffer pressure: a page
  corrupted after the first pass was refused by the canonical later scan, while replay could
  return rows or commit writes without rereading it. `4bcdad0` removes the cache byte-for-byte from
  production and adds a regression in which the second inner pass must fail before any edge can
  persist. The timing is retained only as evidence of the physical cost, not as a delivered gain.
- `f5152de` fixes a separate, pre-existing plan-dependent error channel in exact seeks. A residual now
  rechecks the original conjunction over index hits, while a potentially observable term before
  the indexed equality declines to `NodeScan`. `5938a03` preserves a later pattern's safe seek
  across total equalities on already-bound rows without crossing partial terms. The point-lookup
  access path therefore remains available without making query semantics depend on index presence.

Focused evidence was grouped: VEC-4 passed its 28-case final/query slice plus 57 adjacent cases and
an independent adversarial review; CAT-5 passed 20 allocator cases plus 15 schema/index cases. The
final EDGE corrective slice passed 16 dedicated cases including the later-pass A63 refusal.
The index-seek correction passed 135 focused cases. A broader run found the cross-pattern seek
regression after 405 prior passes; after `5938a03`, the corrected combined focal set and the
remaining `tests/query` file slice both completed with exit code zero. Ruff lint and diff checks
passed.

### Deferred HNSW construction tuning

The cold builder sorts entries canonically, while same-process incremental `commit/apply` inserts
in staging order. Approximate graph shape and ranking may therefore differ between those lifecycle
paths even for the same final exact set. This does not change exact results or the existing HNSW
contract, which promises reproducibility only for the same seed and insertion sequence. Public
`ef_construction`/neighbour-count knobs are deliberately deferred until a full 8,192 x 384 profile
and a recall harness that does not visit nearly the entire graph establish a material gain. They
are not a gate for the current scale-removal work.

## Scale-removal batch 5 — WAL observation and cold inventory decisions

Status: **no production change selected after bounded prototypes and adversarial review**.

- A TXN-1 prototype retained at most 512 records / 2 MiB decoded by the incremental refresh of an
  outer `hold_tail`, then offered the raw records to later read-view and OCC consumers. Each
  consumer still ran its own COMMIT/replay rules. In a nine-run component sample it reduced 754 to
  250 record decodes and moved the median from `30.211 ms` to `11.156 ms` (`2.71x`). This is not a
  delivered gain.
- Nexus review `hof_0dc74badf8504e09af643bd9ae4357bd` produced NO-GO. The foreign-tail
  observation authenticated only newly appended bytes, while a strict sparse-mark read could
  require an older physical prefix. It could consequently hide pre-existing or warm corruption.
  The prototype also substituted logical encoded bytes for `read_bounded`'s frozen physical-plan
  budget and initially let a lazy iterator outlive its hold. Correcting the iterator alone did not
  close the first two blockers; re-reading/re-authenticating the physical plan would consume the
  intended saving. The complete code and test delta was therefore removed before commit.
- CAT-4 considered memoizing `LocalStorageDevice.list_files()` inventory. A device/lifetime cache
  was rejected because another process can publish, remove or corrupt namespace entries without
  changing that Python object's state. A safe version is limited to one `COMMIT_SECTION`, must be
  invalidated around recovery/catalog apply and still retains the first `O(N)` walk. The measured
  directional saving for the two reusable writable-open pairs was about `4 ms` at 16 entries and
  `73 ms` at 4,096 entries; the Pulse-sized case is expected to save only a few milliseconds, while
  `592fd22` already removed steady-state resolved inventory. No CAT-4 code was introduced.

These decisions apply the agreed precedence rule: a locally fast prototype is not promoted when
its authority is narrower than the canonical read or when the safe repair removes most of the
gain. Multiwriter/multireader, WAL/OCC, durability and fail-closed behavior remain unchanged.
