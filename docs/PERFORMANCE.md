# Current performance and measurement boundaries

[Documentation index](README.md) · [Roadmap](../ROADMAP.md#remaining-performance-work)

Updated September 14, 2026. “Current” means the **latest recorded observation for
the stated workload/build**, not a new benchmark of every file in HEAD.
Current development source is 0.0.7; published baseline is 0.0.6.
The latest isolated installed Pulse/native observations below use the final
0.0.6 candidate. The latest production spec-consolidation measurement still uses
`0.0.4@fa8f188`; no production spec was consolidated for these tests.

## 0.0.7 performance round closure

The [closing comparison](reports/PERF_V007_CLOSURE.md) estimates **19.7%
lower latency in a fixed eleven-scenario native basket** for final runtime
`a504417` versus released v0.0.6 source `1e01be5`. Six balanced blocks include
an identical-code control (median control/final ratio 0.996).
The basket has explicit synthetic weights and includes every native scenario
from wave A. It is not a universal or Pulse percentage. Earlier incremental
gains are not added to this direct comparison. Implementation is closed for
this round; individual results, full regression and limits are in the report.

## 0.0.7 native scalar sort dispatch

The [scalar-sort report](reports/PERF_V007_SCALAR_SORT.md) measures `0919c06`
against `7d52f8a`. Exact built-in scalar keys avoid general-kind probes while
retaining mixed-kind ordering and the subclass fallback. Three processes
alternating the old/new helper in one public-query runtime favor the change
for top-50 of 2,000 integer rows (pooled ratio 1.082, about 7.6% lower latency
in this sample). Other scalar-key cases also favor the change, with workload
and same-code variation recorded individually. The independent-process
comparison was noisier; it is retained rather than presented as a precise
gain. This does not establish a universal or Pulse speedup.

The new complete regression has 24,808 passes, 41 attributed skips,
zero failures/errors and exit 0. Focused tests and ten detected mutations
separately qualify the mechanism. Alias metadata and the public-value trust
boundary remain separate from this implementation.

## 0.0.7 wave A: merged mechanisms and independent qualification

The objective is Grafx platform performance. Native API workloads evaluate
general mechanisms; Pulse workloads evaluate one consumer's resulting experience.
Neither a Pulse-specific result nor one native micro-benchmark establishes a
universal gain.

The [wave-A report](reports/PERF_V007_WAVE_A.md) records development source
`852b655`, its pre-wave and 0.0.5 comparisons, the original full regression
(24,780 passed, 2 failed, 41 skipped), reproduction of both failures before wave A,
and separate focused tooling corrections. The failed full run remains failed.
READ-4 memoization, FIX-W section fusion, READ-3 vector-free admission and
W-01/W-02 private participant coordination are delivered mechanisms; their
individual ceiling measurements must not be added together. The private-wait
hotfix retains bounded waiting and manual-clock behavior. The fresh 50-statement
port recheck observes 104 private participant entries, zero participant file
locks and four other file-lock acquisitions, both before and after that hotfix.
Consumer timing boundaries and results are recorded separately in the report.

The new native matrix establishes conditional benefits outside Pulse: ordered
traversals to 200 distinct destinations and to overlapping destinations have paired
pre-wave/head ratios 3.060/1.592; staging 50 individual node/relationship writes
has ratios 1.496/1.246. Streaming, bulk-write and commit cases do not show uniform
gains. These are bounded workload observations, not additive package speedups.
The current Pulse shape and total transfer do not establish gains beyond the
same-code variation. A scalar 2,000-row sort still trails v0.0.5; the report records
a native call-profile diagnostic and the resulting platform priorities.

## Pre-wave 0.0.7 baseline: HEAD versus v0.0.5 on the same machine

September 14, 2026, Phase 0 of the 0.0.7 performance initiative. `b0e4f51`
(`feature/v0.0.7`, the v0.0.6 tag `1e01be5` plus the version bump) against `83cc313`
(v0.0.5), arms alternated round by round in one window. Windows 11, Intel i7-11800H,
16 logical cores, CPython 3.11.14, native `google-crc32c`; `buffer_budget_bytes`
64 MiB, `codec` and `vector_math` `numpy` — the defaults in **both** arms. Medians of
three measured rounds, round 0 discarded. **The machine was not quiescent**: a live
Pulse server and a concurrent agent runtime held 1.4–3.1 of the 16 cores throughout,
so absolute walls are inflated by an unknown, time-varying amount and **only the
paired ratios are defended**. Ratios are v0.0.5 wall ÷ HEAD wall; below 1 the v0.0.5
arm was faster.

| Workload / phase | HEAD | v0.0.5 | Paired ratio | Label |
| --- | ---: | ---: | --- | --- |
| Pulse logical transfer, phases 1–6 | 70.19 s | 66.31 s | 0.945, 3 of 3 rounds below 1 | MEDIDO |
| — `write_nodes` | 14.90 s | 12.51 s | 0.847 | MEDIDO |
| — `write_relations` | 27.22 s | 23.95 s | 0.871 | MEDIDO |
| — `checkpoint` | 5.26 s | 5.93 s | 1.128 | MEDIDO |
| — `certify` | 15.11 s | 15.78 s | 1.044 | MEDIDO |
| — DDL `begin_candidate` | 5.96 s | 6.19 s | 0.977 | MEDIDO |
| KG page, 500 rows, exact Pulse statement | 0.551 s | 0.441 s | 0.814 | MEDIDO |
| KG fan-out, 69 layouts, older harness form | 2.069 s | 1.861 s | 0.834 | MEDIDO |
| Production vector search, per query | 74.9 ms | 69.6 ms | 0.886, band contains 1 | MEDIDO |
| `verify('all')`, cold handle, copied board | 0.610 s | 0.571 s | 0.936, band contains 1 | MEDIDO |

Reading: HEAD measures **approximately 5.8% slower on the Pulse logical transfer, 3 of 3 rounds**,
and all of that difference sits in the two write phases while checkpoint moves the
other way; the vector and `verify('all')` bands contain 1, so no difference is
established there. No cause is established for any ratio. On HEAD the transfer
composes as relationship `execute` 29.9 %, node `execute` 10.3 %, node `commit`
8.9 %, relationship `commit` 8.3 %, `verify("all")` 7.9 %, `checkpoint` 7.5 %, DDL
`execute` 7.3 %, cold `connect_ro` 4.5 %, the Pulse `_prepare` 3.2 % and the Core
fingerprint 2.6 %. The baseline also settles a statement question: the production
Pulse fan-out sends `MATCH (a:{from})-[r:{rel}]->(b:{to}) WHERE <visibility> RETURN
a.id, b.id, r.confidence LIMIT 5000` with **no id list**, filtering the page's ids in
Python afterwards. Not measured: the 256 MiB `buffer_budget_bytes` arm (the Amdahl
instrument takes no connect options) and the checkpoint bucket-floor replay (its
bench is stale against 0.0.6+). Native micros on HEAD in the same session: DML commit
23.82 ms with commit history off, 30.69 ms on; projected ordered page 28.56 ms
against a 33.52 ms full-decode oracle.
[Commands, provenance, per-operation composition and every limitation](reports/PERF_V007_BASELINE.md)
· [the initiative that ordered it](specs/PERFORMANCE_V007_PLAN.md).

## Latest 0.0.6 installed native graph observations

Windows/CPython 3.13.1, NumPy 2.5.3, google-crc32c 1.8.0; exact privately installed
wheel `d666704a...`. Fresh persistent chains, default 8,192-byte pages, 64 partitions,
64 MiB buffer and strict descriptor revalidation. Reads: median of four subsequent
calls after a retained first call, including public autocommit/materialization.
Writes: median of three transactions, each including begin, 32 updates and durable
COMMIT; no excluded warm-up write. The full regression ran concurrently.

| Operation | 64 nodes / 63 edges | 1,024 nodes / 1,023 edges |
| --- | ---: | ---: |
| Indexed exact lookup | 2.335 ms | 3.248 ms |
| Sorted first page, 50 exact IDs | 4.394 ms | 43.537 ms |
| Complete count and exact sum | 3.177 ms | 38.336 ms |
| Named outgoing trail, 0..3 hops | 6.066 ms | 8.589 ms |
| Durable transaction updating 32 nodes | 149.304 ms | 992.746 ms |

These are two bounded sizes, not a production-scale or throughput benchmark.
The scalar UNWIND update retains scan work; indexed map-element driving keys
already have a faster plan shape. Post-write checkpoints are measured separately,
not hidden in the commit table. Exact cold-reopen values and zero verification
findings are required on both stores. [Queries, raw boundary definitions, artifact
hashes and reproducible tool](reports/FP_NATIVE_COST_OBSERVATIONS.md).

## Latest isolated Pulse 0.3.3 API observations with Grafx 0.0.6

One final qualification request per operation, loopback HTTP, 510 synthetic
Decisions, one canonical Board Entity and 20 logical edges. Stub embeddings and
normal background workers; the full native regression was active. These measure
client-observed API work, **not** browser rendering, pure engine time or percentiles.

| Operation | Latest observed request |
| --- | ---: |
| First KG page, 500 of 511 unique nodes | 4.183 s |
| Cursor continuation, remaining 11 nodes | 228.684 ms |
| Exact Decision count, 510 | 112.050 ms |
| Logical relationship count, 20 | 542.285 ms |
| Runtime Settings identity | 82.068 ms |

Rows, cursor exhaustion and totals were checked independently. Real-browser
pagination/search/detail also passed, without a measured paint-to-interactive
latency. [Final paired candidate and exact HTTP receipt](reports/FP_FINAL_PULSE_QUALIFICATION.md).

## Latest 0.0.6 native range-prefix observation

Windows/Python 3.13, September 11, current development source; one in-memory handle,
five consecutive `db.execute()` calls, timing only execute (first call includes
planning, later calls reuse the prepared cache). No machine-idle certification.

| Operation | Latest measured median |
|---|---:|
| `UNWIND range(1000000,2000000) AS i WITH i LIMIT 3000 RETURN sum(i)` | 14.03 ms |

The exact result is `3004498500`; only 3,000 input values are consumed, with no
million-element list allocation. Raw samples: 17.821, 14.734, 14.030, 13.156,
12.785 ms. This is a scalar query observation, not a Pulse/graph-write benchmark
or a performance gate. Tests in `tests/query/test_fp2_streamed_range.py` verify
the carrier path, exact LIMIT pulls, budgets, rollback/reopen and cursor cleanup.

## Latest 0.0.6 physical posting evidence

Windows/Python 3.13, September 10: 60 rows repeating one 80-character string,
512-byte pages and one hash bucket, explicit `posting_hash`: **4 bucket pages**.
A separate 120-row same-key INSERT batch performs **1 membership chain scan**.
These are deterministic structural counts, not latency/throughput or a Pulse
measurement. See [usage and limits](POSTING_HASH.md) and
`tests/api/test_posting_hash.py`; full results remain O(output + bucket pages).

## Latest production spec-consolidation Pulse sample

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

### Continuation after 970aa1e: prepared algorithms

Windows/Python 3.13.1, NumPy 2.5.2; September 9, 2026. One seeded synthetic
detached picture, 5,000 nodes and 30,000 physical directed edges (seed 970).
Median of three calls per configuration, after import/CSR/identity preparation and
NumPy warm-up; preparation, capture, database I/O and UI are excluded. Recorded
after the grouped regressions finished, without a machine-idle certification.

| Operation / current configuration | Latest measured median |
| --- | ---: |
| PageRank, Python backend, tolerance 1e-10 | 389.27 ms |
| PageRank, explicit NumPy backend, tolerance 1e-10 | 12.69 ms |
| Simple-undirected bucket k-core | 33.31 ms |

Both PageRank configurations converged in 24 iterations, residual approximately
7.70e-11; scores agreed within absolute 1e-11. Retained picture charge: 30,732,288
logical bytes, not measured RSS. Raw samples and limitations are in the
[round receipt](reports/V005_AFTER_970AA1E.md). Reproduce with
`PYTHONPATH=src python tools/measure_projection_kernels.py` (set the environment
variable using your shell's syntax). This is not an isolated before/after release
comparison, a timing gate, database write throughput or a Pulse KG-page measurement.

Separately, a prepared 10,000-node picture's two-node local BFS passes with
`max_work=10` and workspace for two discoveries, proving that this operation no
longer scans every node identity. One-time capture and topology preparation still
cost O(V+E). See `tests/api/test_projection_acceleration.py`.

### Continuation after 7dde256: latest capture work observation

Windows/Python 3.13 source tests. These are counts, not latency or UI measurements.

| Operation / fixture | Latest measured result |
| --- | ---: |
| Projected topology capture, 20 nodes with 600-character properties and vectors, batch_rows=8 | 3 public scan calls; maximum 8 rows/batch; no omitted vector materialization |

Evidence: `tests/api/test_projected_scan_batches.py`. All omitted source payloads
remain validated; batching does not eliminate the selected-table census or physical
integrity I/O. Retained immutable CSR reuses adjacency for repeated algorithms;
new algorithms add capabilities, not a measured writer-throughput improvement.
See the [round receipt](reports/V005_AFTER_7DDE256.md). No before/after latency or
new live Pulse claim is inferred.

### Continuation after 69ed311: latest bounded work observations

Windows/Python 3.13 source tests, September 9, 2026. These are structural counts,
not elapsed-time improvements, throughput estimates or Pulse UI measurements.

| Operation / fixture | Latest measured result |
| --- | ---: |
| Empty sparse distribution, 8,192 buckets, 512-byte pages | 72 directory-page visits; budget 71 refuses |
| 24 inserts populating at least 16 sparse heads, 4-page buffer | 1 head-initialization barrier in that committed publication |

Evidence: `tests/api/test_sparse_directory_walk.py` and
`tests/api/test_sparse_batch_heads.py`, with foreign-reader and native crash/replay
checks. Total commit barriers include other WAL/storage obligations; the table
counts only sparse-head initialization. Pointer examination remains O(bucket count).
Prefix indexing trades additional write/storage work for bounded indexed expansion;
detached projection capture remains an explicit selected-table census. Neither is
advertised as a universal throughput improvement.

### Continuation after 3f3819f: latest bounded work observations

Windows/Python 3.13.1 source tests, September 9, 2026. Counts, not latency/SLO claims:

| Operation / fixture | Latest measured result |
| --- | ---: |
| Empty sparse hash, 65,536 buckets, 512-byte pages | 571 allocated pages |
| Retired-overflow indexed allocation, 3 selected pages | At most 7 page pins, confined to header/directory/candidates |
| Warm repeated-key capture, 60 exact entries in one overflow chain | 0 entry decoder calls; all 60 candidate results preserved |

Tests: `test_sparse_hash.py`, `test_free_page_index.py`, `test_hot_key_pages.py`.
The page memo does not remove page/heap verification or O(result count). Sparse
first-bucket insertion adds a durability barrier. No Pulse timing was taken here.

### Eight-item continuation: latest small installed-wheel probe

September 9, 2026; isolated installed 0.0.5 wheel, Windows/Python 3.13.1, 512-byte
pages, 24 documents, 23 edges, three-dimensional vectors and default source windows.
This is a small consumer correctness probe with one timing sample during another
regression run, not a Pulse benchmark, tail-latency result or large-graph speedup.
Reproduce with `python -I tools/check_installed_eight_followup.py` from the isolated
environment after building/installing the matching source wheel.

| Operation / regime | Latest measured result |
| --- | ---: |
| Cold native FTS using durable corpus summary, unique term | 2.924 ms; 1 posting visited |
| Hybrid retrieval with one-hop indexed graph evidence | 11.741 ms; 1 incident edge visited |
| Aggregate hybrid logical-memory peak | 8,491,776 bytes (logical tariff, not RSS) |
| Disk-spooled physical backup payload | 4,607,121 bytes; restored and verified |

The probe also checks per-version migration idempotence, vector cancellation with
reader reuse, 8,192-bucket text-index rehash, offline restore and exact hit parity.
Separate feature tests compare indexed graph expansion against full scanning with
identical hits/scores on disconnected-edge fixtures, and count exactly one token
iteration per candidate field for query-term frequencies. These demonstrate
bounded work elimination, not a universal latency multiplier. Durable summaries
avoid only the eligible corpus-statistics census; posting/candidate work remains.

### Latest FTS and logical-transfer sample

Fresh local default-config stores, Windows/Python 3.13.1; 200 five-word documents,
one indexed STRING field, 64 buckets, 20 exact `group3` hits. CRC accelerator 1.8.0
installed. These are new capability observations, not Pulse latency or a speedup
comparison. [Reproduction, assertions and limits](reports/V005_SEARCH_RESUME_CHECKPOINT.md).

| Operation | Latest measured result |
| --- | ---: |
| Native text index build | 229.289 ms |
| Cold BM25 top-20 | 35.603 ms / 1,220 postings visited |
| Warm BM25 top-20, median of 20 calls | 2.870 ms / 20 postings visited |
| One indexed document, complete durable transaction | 27.256 ms |
| Search for newly written unique term, incremental statistics | 4.356 ms / 21 postings + 11 WAL records |
| Export 201 documents | 92.791 ms / 13,344 artifact bytes |
| Import with index rebuild, verification and promotion | 938.954 ms |

Cold and warm are cache regimes, not evolution de/para. This sample uses default
`statistics_mode="wal"`, whose cold statistics remain linear; eligible writes
advance scalar statistics with bounded native WAL evidence. Opt-in durable mode
is measured separately above on a different workload, not as an A/B speedup.
The new-term result was checked against an independent census with identical hits
and scores. No RSS, production SLO or large-graph claim. These observations were
taken while a separate regression process was running; host contention was not isolated.

### Native hybrid sample

Separate synthetic 200-document corpus, two-dimensional unit-circle embeddings,
three-word text, 64 FTS buckets, source windows 100, result k=20. Native ANN explicitly
selected (`vector_exact_scan_threshold=0`, `vector_ef_search=256`); graph disabled.

| Operation | Latest measured result |
| --- | ---: |
| Cold hybrid top-20, including first vector-index use | 181.030 ms |
| Warm hybrid top-20, median of 20 calls | 5.280 ms |
| Warm maximum of those 20 calls | 5.763 ms |
| Fused recall@20 against an exact-source run, one fixed query | 1.0 |

Native-source RRF ranks/scores matched the independently assembled reference.
This tiny low-dimensional query is a conformance fixture, not a representative
semantic recall evaluation. Maximum of 20 calls is not a p99 estimate; peak RSS
and cold distribution remain unmeasured. No Pulse workload was consumed.

### Previous native workloads, latest observations retained

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

## Latest prepared analytics in 0.0.5 development

September 9, 2026, continuation after `a4dd85a`; synthetic detached 5,000-node /
30,000-edge graph, seed 970, Python 3.13.1 / NumPy 2.5.2. Five calls per operation
after grouped regressions completed; source capture, CSR and dependency warm-up
excluded. Preparation is explicit and separately measured, never hidden in a warm-up.

| Current operation/configuration | Latest median | One-time preparation |
| --- | ---: | ---: |
| Weighted personalized PageRank, NumPy with retained transitions | 7.645 ms | 39.586 ms |
| k-core, retained simple-undirected topology | 34.985 ms | 32.199 ms |

All rank results converged and matched unprepared controls within 1e-11; core numbers
matched exactly. Extra retention is an opt-in tradeoff, not a single-call guarantee.
Picture logical charges: 48,336,384 bytes for prepared rank, 40,656,384 for simple
topology; not RSS. No Pulse loading/write latency, database capture/storage cost,
machine-idle certification or universal speedup is claimed.
[Samples, current-mode controls and reproduction](reports/V005_AFTER_A4DD85A.md).

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
