# Current performance and measurement boundaries

[Documentation index](README.md) · [Roadmap](../ROADMAP.md#remaining-performance-work)

Updated September 8, 2026. “Current” means the **latest recorded observation for
the stated workload/build**, not a new benchmark of every file in HEAD.
Current source is 0.0.4 with CAP-1 recovery work; latest live measurement used
`0.0.4@fa8f188`, not that later recovery checkpoint. No new live benchmark or
spec consolidation was performed for this documentation refactor.

## Latest real Pulse sample

Pulse 0.3.3; Grafx 0.0.4 `fa8f188`; Windows, Python 3.13; NumPy 2.5.2 and
google-crc32c 1.8.0 installed. Eleven Alternative nodes and 22 edges, no vector
index on that table. One sample, no retries/errors; all readback identities and
edge pairs matched and Global delivery reached processed ACK.

| Operation | Latest sample | Interpretation |
| --- | ---: | --- |
| Commit via MCP | 10.726 s | Includes Pulse orchestration and graph/relational/transport work |
| Outbox creation → Global processed ACK | 54.952 s | Includes scheduling and verification; not pure write latency |
| Read eleven nodes | 0.376 s MCP / 91.1 ms executor | Exact readback, not a 500-node KG page |
| Read eleven judgement links | 0.408 s MCP / 167.2 ms executor | Exact endpoint pairs |
| Read eleven source links | 0.182 s MCP / 74.2 ms executor | Exact endpoint pairs |
| Exact-title natural retrieval | 2.709 s | One title; does not establish broad-query ranking/recall |
| Full KG UI cold/warm on latest source | **Not measured** | Do not substitute native timings for UI |
| Native-only portion of latest real commit | **Unknown** | Phase instrumentation was deployed but output was not captured |
| Latest-source writer throughput / p50 / p99 / peak RSS | **Not measured** | No current cross-platform/SLO claim |

[Full receipt and limitations](reports/PULSE_SINGLE_SPEC_LATEST_PERFORMANCE_0_0_4.md).
The earlier sample (5 nodes/10 edges) took 22.981 s / 131.489 s. Specs and runtime
states differ: the observed reductions are **not an isolated engine speedup**.
Do not add overlapping timing boundaries. Historical policy DLQ/canonical debt
and stale diagnostic snapshots mean this is not an all-green operational audit.

## Native/component evidence in the 0.0.4 development line

These measurements are useful for attribution, not replacements for the latest
real-consumer sample. Each link pins workload, build/checkpoint and limitations.
Only the latest measured implementation is shown; ranges retain its observed samples.

| Operation / sample | Latest measured result | Scope and evidence |
| --- | ---: | --- |
| Native durable commit, 4 nodes / 8 edges, private quiescent copies, noop metrics | 51–57 ms | [Source-reference experiment](reports/SOURCE_REFERENCE_WRITE_COST_0_0_4.md); small synthetic batches with source indexes |
| Source lookup in that write transaction | 2–4 ms | Same experiment, with source indexes; not whole-consolidation latency |
| Explicit checkpoint in that experiment | 653–726 ms | With source indexes; separate maintenance cost, not per-row latency |
| Native Global reopen on private copy | 2.409 s | [Recovery-floor reuse](reports/RECOVERY_FLOOR_PHOTO_0_0_4.md); 2,261 IDs verified |
| Global incoming endpoint inventory, 2,253 rows | 3.231 s | [Destination batching](reports/GLOBAL_DESTINATION_BATCHING_0_0_4.md); private-copy observation |
| Global destination preflight, 2,253 rows | 2.979 s | [Unstaged-write preflight](reports/UNSTAGED_WRITE_PREFLIGHT_0_0_4.md); one sample |
| OPTIONAL degree query, 953 rows | 2.22–2.30 s | [Vector-free landing](reports/OPTIONAL_DEGREE_VECTOR_LANDINGS_0_0_4.md); ordered digest verified |
| Verification retained value versions | 2,261 | [Retained history](reports/VERIFICATION_RETAINED_HISTORY_0_0_4.md); memory metric, not execution time |

Early 0.0.4 KG samples (read-only open 0.925 s; first/second 500-node page
0.667/0.385 s) precede later work. They are retained in the
[0.0.4 source record](archive/ROADMAP_SOURCES.md#source-performance-round-0-0-4),
not presented as latest-source cold/warm UI results.

## How to reproduce and compare responsibly

1. Pin engine/application commits, Python/OS/filesystem/hardware, dependency
   versions, effective selectors/budgets and number of participants.
2. Use a declared fresh synthetic database or a verified **quiescent copy**.
   Never infer that a live file copy is a consistent snapshot.
3. Separate open/adoption, query, native commit, checkpoint/verify, transport,
   scheduling and rendering. State cold/warm cache and concurrent host load.
4. Verify exact result digests/counts/multiplicity, error outcomes and durable
   reopen. Compare identical inputs in alternating rounds before causal claims.
5. Report N, row/vector widths, fan-out, batch/transaction size, sample count and
   latency distribution when measured. Missing data is unknown, not zero.

In-tree instruments live in `tools/` and `tools/perf_round/`; the historical
[instrument guide](reports/PERFORMANCE_HISTORY.md) names the original harnesses
and rigs. Do not run production-copy or long concurrency harnesses blindly from
a documentation example. Detailed [reports](reports/README.md) preserve their
commands/conditions; artifact paths outside the repository may be unavailable.

## Performance policy

Timings, D5 ratios and former throughput floors are **informational**, not release
gates. Quality gates still reject corruption, semantic divergence, broken
durability/recovery, concurrency violations and unexplained operation timeouts.
Run focused tests during implementation, then proportional grouped regressions;
do not repeatedly spend hours proving marginal latency changes.

Grafx does not currently claim Ladybug parity, constant-time general traversal,
linear CPU/GIL scaling or an RSS bound equal to `buffer_budget_bytes`. Historical
cross-platform comparisons remain [historical](reports/PERFORMANCE_HISTORY.md).
