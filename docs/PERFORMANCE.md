# Current performance and measurement boundaries

[Documentation index](README.md) · [Roadmap](../ROADMAP.md#remaining-performance-work)

Updated September 8, 2026. “Current” means the **latest recorded observation for
the stated workload/build**, not a new benchmark of every file in HEAD.
Current development source is 0.0.5; published baseline is 0.0.4 with CAP-1 recovery work.
The latest live measurement used
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

## Native synthetic checkpoint in the 0.0.5 development line

R3 storage-growth acceptance (September 8, isolated 512-byte-page fixture): a
4,200-character replacement after quiescent overflow retirement appended **0 heap
pages**, including after reopen. This measures reused capacity, not latency or file
shrinking. Discovery still amortizes a heap-extent scan per participant/reclaim floor;
no new UI or commit-throughput claim is made for R3/R4. Backup/restore is an
operational feature with a bounded checkpoint/capture publication pause, not a
performance optimization.

Fresh local stores, one two-column node table, Windows/Python 3.13.1; latest warm
read observations only. This is not the Pulse UI, a relationship-heavy KG or a
comparison against Ladybug. [Full workload, source hashes and validation](reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md).

| Operation | Latest measured result | Scope |
| --- | ---: | --- |
| Selective first 500-node page | 31.516 ms | 2,048-node table, explicit numeric ID list |
| Selective next 500-node page | 31.067 ms | Same table, next non-overlapping ID list |
| Unindexed ordered range page | 38.900 ms | Still scans all 2,048 rows |
| Twenty unstaged-writer PK preflight reads | 25.901 ms | Repeated same key; tracemalloc enabled |
| Stage / commit eleven nodes | 18.013 / 41.878 ms | Separate native phases, tracemalloc enabled; no edges/vector index |
| Python traced write-phase peak | 197,341 bytes | Not peak process RSS or a memory guarantee |

The component workloads below are different experiments and are not superseded
by this narrower fixture. Full authenticated production UI latency remains unmeasured.

## Pulse consumer fixture in the 0.0.5 development line

Fresh full Pulse schema, 1,100 synthetic Decision nodes, 128 hub edges; actual
service, routed executor and HTTP route. Authorization/route authority are fixture
replacements and result caching is bypassed. Browser checks use the actual Pulse
API client and GraphCanvas with fixture controls, **not the full live application**.
[Conditions, assertions, all six dispositions and reproduction](reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md#bounded-follow-up-and-disposition-of-the-six-selected-items).
Latest HTTP/native observations are from the [N1–N2 checkpoint](reports/V005_N1_N2_CHECKPOINT.md);
browser and churn rows retain their earlier named experiment. Background Pulse
was running; these observations do not establish a causal speedup.

| Operation | Latest measured result | Scope |
| --- | ---: | --- |
| New handle open | 2,808.201 ms | Full schema, separate from HTTP; OS cache not flushed |
| First HTTP 500-node page on new handle | 488.462 ms | Exact nodes/incident edges, no failed layout |
| Warm first / next 500-node HTTP pages | 264.038 / 314.126 ms | Two distinct pages, JSON included |
| Warm browser reset | 269.7 ms HTTP/JSON + 67.6 ms to second frame | Real renderer; not ForceAtlas2 convergence |
| Next-500 browser request | 309.4 ms HTTP/JSON + 367.2 ms to second frame | 1,000 accumulated nodes rendered |
| Global digest / link writes | 27.552–43.988 / 19.980–39.985 ms | Four synthetic digests with 384-dimensional vectors |
| Global inventory dispatch | 23.567 ms | Four digests, three actual inventory queries; test event-loop startup included |
| Explicit Global verify-all / checkpoint | 716.379 / 171.577 ms | Separate maintenance; not full outbox ACK |
| Churn RSS, four handles plus pinned reader | 54,210,560 bytes | Small 64-row fixture after 12 update/delete cycles; not full Pulse RSS |

Exact page candidate counts were 128/500/500 at graph sizes 128/512/1,100.
The real warm KG cycle rebuilt zero parses/plans. Two concurrent writers and two
readers retained correct results, but thread throughput did not scale linearly.
Overflow heap growth, complete production SQLite/ACK timing and larger-scale index
limits are not claimed resolved by this bounded release work.

## Latest four-item 0.0.5 follow-up

Different bounded workloads from the 1,100-node fixture above. Windows/Python
3.13.1, fresh temporary stores, stub embeddings, no production specs or UI run.
The consolidation uses real Community composition, SQLite, Grafx and the outbox
worker, including verification before ACK. [Full evidence and limits](reports/V005_FOUR_ITEM_FOLLOWUP.md).
Consolidation rows use the latest [R1–R2 run](reports/V005_R1_R2_CHECKPOINT.md),
recorded while the same host also ran regression tests;
the projection and 128-node rows retain their separate earlier measurements.

| Operation | Latest measured result | Scope |
| --- | ---: | --- |
| Ordered projected page | 20.247 ms | Warm median; 128 of 256 rows, two tables, 384d vectors and 12,000-character payloads |
| Writable full-schema open | 2,521.420 ms | 128-node Pulse fixture; independent of HTTP |
| Warm 128-node HTTP page | 118.435 ms | 127 edges, zero failed layouts; fixture authority, real graph IO |
| Reconcile four synthetic candidates | 3,791.805 ms | Includes cold independent-reader admission/checkpoint |
| Consolidation commit, four nodes / four edges | 1,680.525 ms | Graph dispatch is 935.872 ms of this total |
| SQLite commit | 1.519 ms | After graph/audit staging |
| Immediate outbox tick through ACK | 2,618.327 ms | Includes application, flush/reopen and verification |
| Exact-ASCII identifier admission, 10,000 names | 2.615 ms | Median of seven component repetitions; no IO or latency threshold |

Four references and digests were verified, one event ACKed, and a second worker
tick was empty. Setup and periodic scheduling wait are excluded. These are not
comparable to the older live ACK duration or a claim that first-reader admission,
production backlog or all write latency is solved. Timing tables show latest
measurements only; regression checks have no performance thresholds.

The separate [N4 public publication sample](reports/V005_N3_N4_ACCEPTANCE.md) measured
**24.233 ms median** per journaled single-row update (512-byte pages, 16 measured
iterations). This opt-in development capability adds durable provenance; the
measurement is neither a Pulse timing nor a performance improvement claim.

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
