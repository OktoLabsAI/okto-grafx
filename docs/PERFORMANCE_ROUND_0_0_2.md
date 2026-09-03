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
| Pulse counterpart | `d50c03404bd72873b596596f1c4848d56dbcd437` |
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
