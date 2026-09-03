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
| 2026-09-03 | P1.1 / D-26 implementation | lease, commit-section, phase, WAL, page, flush, foreign-commit and retarget metrics implemented; focused commit/concurrency/catalog/import suite and Ruff passed; measurement and promotion remain deferred until P0 closes |

