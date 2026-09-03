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

Development of isolated P1 branches may proceed after this freeze, but none may be benchmarked,
promoted or integrated until P0.1–P0.4 are closed as specified by the governing plan.

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
  every retained coordinate with endpoint lookup counters and refuses truncation. It also records
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
  and independently proves that the clone and serve-lock inventory stayed unchanged.
- The census walks only private headers with `copy_content=False` and emits aggregates, never record
  IDs, page IDs, references, CSNs or payloads. MVCC “dead” means only “not live under the current
  `HeapVersion.live` predicate”; it does not prove vacuum eligibility. Vector counts are static
  catalog presence and do not claim search/build activity. The one-shot runner reconciles verifier,
  heap-slot, MVCC-class and locality totals before accepting the artifact.

The milestone regression covered 98 focused instrument tests in one combined run. Ruff,
`py_compile`, `git diff --check`, command-line contract checks, an isolated real-Grafx census smoke
and a synthetic py-spy 0.4.2 multi-thread smoke were also successful. The synthetic profiler
artifact reconciled 43 samples and zero errors; it is developmental evidence only. No default or
live Pulse data home was opened, copied or profiled. Consequently P0.3 is not yet marked complete,
and no P1 branch is promoted on the strength of tooling alone.

## P1.1 — commit-window instrumentation (D-26)

Status: **developed on an isolated branch; not promoted**.

Branch `perf/v002-d26-commit-metrics`, commit
`5abfd396ea352eb4eac9e20902d4ab55f0bc8898`, adds closed-cardinality timings for writer-lease and
commit-section wait/hold windows, timings for the ten internal commit phases, and counters for WAL
pages/bytes, examined frames, flush calls, foreign commits and retargets. Metric delivery remains
outside the exclusive sections and uncertain acquisition/release paths suppress the local trace.

Focused commit, containment, catalog and import-boundary tests passed, together with Ruff and
dashboard JSON validation. Promotion remains deferred until P0.3/P0.4 can run after the live
backfill drains, as required by the governing plan.

## P1.2/P1.3 — isolated Python-path optimizations

Status: **developed on isolated branches; not promoted**.

- D-01 is published as `perf/v002-d01-header-peek` at
  `1239a0e519295b9f2b127d88d3155c7fdd352daf`. It peeks the three visibility fields with one
  header unpack for rejected versions and retains full `RecordHeader` construction for accepted
  versions. The focused heap suite and Ruff passed.
- D-04 is published as `perf/v002-d04-index-version` at
  `e898fe766b29e61c867b44679a5f0f1e77b724d9`. It reuses the heap version already validated by
  the index path while preserving ended/changed fallback filtering. The focused primary-key-index
  suite and Ruff passed.

The D-02 tail-first endpoint prototype was not retained: with a corrupt visible duplicate near the
head, it could accept a later row and hide the corruption currently surfaced by the public scan
order. No safe performance change was proven without a uniqueness/min-max access path, so P1.4 is
deferred to the existing P2-ID decision instead of weakening fail-closed behaviour. P1.5 remains
blocked by that locator dependency; P1.6 and P1.7 remain conditional on post-drain measurements.

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
| 2026-09-03 | P1.1 D-26 isolated implementation | focused commit/metrics/containment/catalog tests and Ruff passed; branch published, promotion pending P0.3/P0.4 |
| 2026-09-03 | P1.2 D-01 isolated implementation | full focused heap suite and Ruff passed; branch published, promotion pending P0.3/P0.4 |
| 2026-09-03 | P1.3 D-04 isolated implementation | focused primary-key-index suite and Ruff passed; branch published, promotion pending P0.3/P0.4 |
| 2026-09-03 | P1.4 D-02 prototype | rejected because it could mask visible duplicate corruption; deferred to P2-ID |
| 2026-09-03 | P0.2 fail-closed primitives | receipts, authenticated copy and independent-series runner integrated at `c276dec`; 31 focused tests and static checks passed; driver completion is recorded in the next row |
| 2026-09-03 | P0.2 authenticated Pulse card driver | integrated at `be286fa` + `2d43d75` from `perf/v002-p0-card-driver@e8a6be0`; 46 focused tests and static checks passed; synthetic RAW/instrumented lifecycle smokes passed without touching the live board; P0.3/P0.4 execution remains pending |
| 2026-09-03 | P0.3 profiler/census tooling | published on `perf/v002-p0-census@222a854`; endpoint locality, dynamic vector activity, guarded py-spy capture and reconciled read-only census implemented; combined milestone regression 98/98 passed; real corpus execution waits for the live backfill to drain |
