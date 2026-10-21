# Current performance and measurement boundaries

## RF10 reading recipes: choose the snapshot contract

| Application need | Recipe | Visibility and lifetime |
| --- | --- | --- |
| Several reads compose one business result | `with db.begin("read") as tx`, then `tx.execute(...)` | One fixed snapshot; later commits are not refreshed between statements. |
| Independent operations need a newly published view | Separate `db.execute(...)` calls | Each new transaction selects its own view; the group is not one atomic read. |
| Large incremental result | `with db.query(...).cursor(...) as rows` | Cursor owns its snapshot until exhaustion or close. Close before waiting for network/user input. |
| Stable keyset export | Pages inside one explicit read transaction | Stable membership at that snapshot; history is retained for the entire export. |
| Independent keyset pages | New read transaction for each page | Concurrent changes can affect membership; pages do not form one snapshot. |

### Sixteen primary-key reads

Given an existing `Item(id INT64, value INT64, PRIMARY KEY(id))` table and an
open `db`, these recipes explicitly choose their consistency contract:

```python
query = "MATCH (n:Item {id: $id}) RETURN n.value"
keys = range(16)
with db.begin("read") as tx:
    related = [tx.execute(query, {"id": key}).rows for key in keys]

independent = [db.execute(query, {"id": key}).rows for key in keys]
```

The first group uses one snapshot and transaction boundary; the second opens
sixteen independent transactions. An external writer between related reads is
visible only after a new transaction starts. Equality on an unchanged fixture
does not make the concurrent semantics interchangeable. Grouping can amortize
transaction work when shared consistency is required; no universal speedup is
promised. Deterministic tests count successful opens and settlements (1 versus
16). RF10's separate measurement card owns latency/campaign evidence.

### Keyset pages and ownership

Use a unique, stable ordering key. For the same table:

```python
page_query = (
    "MATCH (n:Item) WHERE n.id > $after "
    "RETURN n.id ORDER BY n.id LIMIT 4"
)
all_ids = []
with db.begin("read") as tx:
    after = -1  # This fixture uses nonnegative IDs.
    while page := tx.execute(page_query, {"after": after}).rows:
        all_ids.extend(row[0] for row in page)
        after = page[-1][0]
```

For independent pages, omit the outer transaction and call `db.execute` inside
the loop. A concurrent delete can remove an unseen row; an insert ahead of the
continuation can add one. Changing sort keys can skip or repeat business entities.
A single snapshot avoids these cross-page changes but holds history longer.
This recipe does not prove an indexed seek plan or the RF05 optimization.
`db.query(...).cursor()` owns its own snapshot; do not transfer it into another TX.

```python
materialized = db.execute("MATCH (n:Item) RETURN n.id")
with db.query("MATCH (n:Item) RETURN n.id").cursor(batch_size=1) as rows:
    first = rows.fetchone()  # The block closes even after partial consumption.
# Explicit rows.close() is also supported and idempotent.
```

Materialized rows remain detached after their transaction and handle close.
Keep the handle open while consuming a cursor. Consumer exceptions and query
cancellation leave through the context manager; cleanup preserves the primary
error even when cleanup itself fails. See
[snapshot diagnosis](OPERATIONS.md#read-snapshot-lifetime-and-diagnosis).

Executable coverage: [RF10 recipes](../tests/api/test_read_snapshot_recipes.py),
[cursor lifecycle](../tests/query/test_query_cursor.py), and
[reader registration](../tests/txn/test_reader_lifecycle.py). These establish
consistency and cleanup, not temporal qualification.

The RF10 campaign has eight slices: four read recipes with warm and reopened
handles. Use a positive, common query-memory budget in both source arms.
The sixteen-statement recipes use the sequence logical-memory observer, which
tracks the simultaneous retained tariff across the complete group; the single
statement recipes use the ordinary observer. First-row timing belongs to the
measured read. Transaction counts, partial-cursor closure, standing pins and
handle closure come from a separate untimed diagnostic and are required in
every qualified sample. Compare each recipe only with itself across revisions;
the snapshot contracts above remain distinct.

## Ingestion atomicity and cost

Choose the application atomic unit before choosing a faster ingestion shape.
These recipes use existing public APIs; they introduce no automatic chunking or
retry helper. All writes below require an explicit write transaction.

| Shape | Atomic boundary and failure | Consumption/cost boundary |
| --- | --- | --- |
| Repeated `txn.execute(text, params)` | Each successful statement remains staged; an escaping exception rolls the transaction back. Catching an error is not permission to assume all previous statements were undone. | Repeated statement execution; one durable acknowledgement only when the enclosing write transaction commits. |
| `txn.executemany(text, iterable)` | One call rolls back its own successful prefix on failure, preserving work staged before that call. It does not commit. An error escaping the transaction block rolls back the whole transaction. | Lazy parameter consumption, no result rows, one parsed statement reused. Iterator side effects cannot be rolled back. |
| `UNWIND $ids AS id CREATE (:Item {id: id})` | One statement inside a transaction; success is still staged until commit. A transaction failure does not make any prefix durable. | A collection parameter, not a lazy `executemany` iterator; consider parameter size and logical/transaction budgets. |
| Several transactions containing chunks | Each acknowledged chunk is independently durable. A later failure leaves the earlier committed prefix. | More commits/ACKs; bounded per-transaction work. Use only when partial progress is acceptable and recorded by the application. |

Given an existing `Item(id INT64, PRIMARY KEY(id))` table, an atomic lazy batch is:

```python
txn = db.begin("write")
try:
    with txn:
        txn.executemany("CREATE (:Item {id: $id})", ({"id": i} for i in range(100)))
finally:
    if txn.active:
        txn.rollback()  # also retire a refused commit at context exit
report = txn.report  # commit occurred at successful context exit
```

Do not split this transaction to fit a limit if all 100 rows must commit together.
Handle a refused commit with the [outcome and cleanup policy](OPERATIONS.md#commit-outcomes-and-retries).
To accept partial progress explicitly instead:

```python
acknowledged = []
for chunk_id, ids in enumerate(((0, 1), (2, 3))):
    txn = db.begin("write")
    try:
        with txn:
            txn.executemany("CREATE (:Item {id: $id})", ({"id": i} for i in ids))
    finally:
        if txn.active:
            txn.rollback()
    acknowledged.append((chunk_id, txn.report.csn))
```

The in-memory `acknowledged` list is illustrative, not a crash-safe resume ledger.
An exception after a durable barrier can precede that append: reconcile using the
commit report/durable state and application idempotency before replay. Close owned
cursors and retire any active refused transaction before retry/backoff; do not
swallow data/type/budget errors or blindly retry an iterator that has been consumed.
For `UNWIND`, use `txn.execute("UNWIND $ids AS id CREATE (:Item {id: id})",
{"ids": [0, 1]})` inside the same explicit transaction. Business recomputation on a
fresh snapshot is distinct from reusing a literal parameter batch.

Compare equal atomic contracts: report rows attempted/committed, transactions,
durable write ACKs, conflicts/retries and latency per atomic unit alongside
rows/second. State chunk size, limits, storage durability and writer count (1/2/4).
One ACK per row and one ACK per thousand rows have different failure contracts;
their throughput alone cannot establish an engine improvement. These recipes and
deterministic tests make no temporal qualification claim.

The RF09 campaign report groups recipes only when row count, payload size and
writer count match. Its `matched_write_loads` table shows each shape's atomic
unit and per-pair throughput, durable ACK count and observed commit-call latency;
it does not calculate a cross-shape speedup. Each `write_tradeoff` retains the
individual ACK durations with writer, offset and row count. Allocated WAL/file
byte deltas and runtime physical counters belong to the whole sample. Values
divided by the ACK count are labelled amortized costs, not bytes or latency
attributable to an individual commit. Missing physical counters are reported as
unavailable; logical `rows_created` is listed separately from physical work.

[Documentation index](README.md) · [Roadmap](../ROADMAP.md#remaining-performance-work)

Updated October 21, 2026. “Current” means the **latest recorded observation for
the stated workload/build**, not a new benchmark of every file in HEAD.
Current development source is 0.0.7; published baseline is 0.0.6.
The latest isolated installed Pulse/native observations below use the final
0.0.6 candidate. The latest production spec-consolidation measurement still uses
`0.0.4@fa8f188`; no production spec was consolidated for these tests.

## RF01 physical scan statistics (development)

RF01 adds integer diagnostics to `QueryResult.statistics` and cursor statistics.
`physical_node_versions_examined` and `physical_edge_versions_examined` count
a stored version header actually examined by an instrumented heap read,
before snapshot visibility and owner-overlay filtering. Ended or otherwise
invisible versions count; pending inserts that have not reached the heap do not.
Repeated scans count their repeated work. Existing `rows_scanned`, `edge_scans`
and cooperative cancellation/quota checkpoints retain their meanings.

Coverage includes canonical node scans (full and projected), grouped traversal
and relationship scans, index candidate reads, identity landings, visible-record
cursors and `scan_page`. An absent counter means no instrumented operation
reported that unit; it does not prove zero work through a custom collaborator.
An executed empty scan, or the existing proven pageless relationship shortcut,
reports zero. Custom ports retain their behavior; results returned by a custom
port are never counted as a substitute for observing its physical work.

| Counter | Unit |
| --- | --- |
| `index_probe_keys` | Distinct keys actually sent to a physical bucket lookup, counted again when retried |
| `certificate_attempts` | Exact-read certificate attempts actually started |
| `certificate_retries` | Additional attempts actually started after a refusal |
| `landing_candidates` | Physical candidates submitted to identity validation, including invisible candidates and retries |
| `landing_batches` | Identity groups submitted through a grouped validation door; a cache-only reuse does not add a batch |

Result multiplicity does not multiply a reused physical read. A certificate
retry counts its discarded work and is not itself a fallback. The first guard
that actually selects a consumed alternative sets one integer diagnostic
`fallback_reason_<reason>=1`. The closed reasons are `ineligible_shape`,
`unproven_binding`, `unproven_totality`, `nonselective_probe_set`, `fan_limit`,
`index_unavailable`, `index_stale`, `owner_overlay`, `noncanonical_port`,
`quota_frontier`, `memory_guard` and `streaming_guard`. No such key means `none`
for the instrumented execution. Planning an alternative alone does not set a
reason; `unproven_binding` is reserved and is not inferred from a scan plan.
The integer representation preserves the public statistics value contract.

The canonical walk inspects a page's headers before yielding its rows. Early
close therefore includes the headers already inspected on that page, while
later pages remain unvisited. Counts accumulate locally and publish after each
page pin is released. Observation is bound to one execution or cursor pull and
revoked before returning control to the consumer. Interleaved cursors retain
separate dictionaries, and nested native procedure statements contribute to
their parent statement. Counts cover evaluation and referential checks; they
do not include a later transaction commit. Metrics publication occurs outside
the cursor observation scope. These diagnostics do not extend the frozen
`MetricsSink` catalogue; no data, query text or parameters become labels.

RF01 remains in progress. The versioned campaign and report validator are
implemented; real six-process runs validated the artifact contract, while the
three-pair overhead qualification remains pending. These deterministic counters establish measurement semantics;
they do not establish a speedup or compliance with the overhead budget.

### RF01 campaign evidence and memory boundary

The development campaign under `bench/harness/campaign.py` records six separate
process runs in AB/BA/AB order, with five warm-ups and twenty retained samples
per selected scenario and process. Its versioned raw JSON keeps source revisions,
import provenance, environment and configuration, seed, query and parameter
hashes, typed result hashes, actual statistics, timing boundaries, cache state,
WAL and disk deltas, and the measured plan. The independent fixture oracle must
match every sample before ratios are computed. The report gives per-pair median
and descriptive p95 ratios; it is not a production percentile or SLA estimate.
The selected scenario list is part of the evidence; unselected dimensions are
not implicitly covered.

`python -m bench.harness.campaign calibrate --output <new-file.json>` produces
an unsigned local executor receipt before a qualification run. It records five
warm-ups, twenty retained fixed CPU controls, five host CPU observations,
environment, timestamps, the producing command and hashes of the harness
sources. `run --calibration <file>` embeds and validates that receipt. Its fixed
hash workload checks repeatability and host load; it is not a disk or Grafx
throughput calibration. A boolean calibration claim alone cannot produce a
qualifying verdict. Invalid supplied receipts are rejected and a noisy or old
receipt leaves the comparison inconclusive.

The opt-in memory observer reports the peak *simultaneous admitted logical
tariffs* of query budgets plus retained owner and endpoint handle budgets.
It does not measure Python allocations, unbudgeted operators, result rows,
buffer frames or process RSS. RSS is recorded separately as a process-lifetime
peak. An observation requires a configured query-memory budget, one statement
and a private handle whose cursor is fully consumed or closed within the
interval. A missing observer, overlapping statement, foreign execution or
incomplete cursor makes logical memory unavailable. Sequential operator peaks
are never summed. This private measurement door changes neither quota admission
nor the public `MetricsSink` catalogue.

Native full-text search in this fixture verifies membership of sorted record
IDs, not ranking or scores. Its native memory is outside the query-budget
observer. Concurrent writer peaks are individual process measurements, never
added into a fictional shared RSS peak. A qualification attempt must report
unavailable coverage explicitly instead of substituting zero. Reopen after a
durable write ACK checks the stored result but does not claim a cleared OS cache.

The first real run of the new harness used `b18e8f1` on both arms, selected only
the constant control, and validated six clean process outputs in AB/BA/AB order
with 150 raw samples. It is exploratory: host CPU reached 59%, calibration did
not meet the registered noise policy, and query memory was unavailable because
that run omitted the budget. Its purpose was to test orchestration, provenance
and the raw schema, not counter overhead.

A second complete run compared clean control `4d9c02e` (derived from `3a2239`
with the same real logical-memory observer, without RF01 physical counters)
against candidate `b18e8f1`, with the same 64 MiB query budget on both arms.
All 150 samples validated with matching typed results and available logical
memory coverage. Candidate/base median ratios across pairs were 1.060, 1.178
and 1.192; descriptive p95 ratios were 1.011, 1.023 and 1.104. Host CPU reached
91.5%, and within-process dispersion exceeded the registered limit. The verdict
is **inconclusive**, irrespective of individual ratios. This control tests the
incremental counter cost over a common observer, not the observer's own cost
against the historical source.

The earlier pre-harness counter-overhead comparison also kept identical results
and raw samples under high, varying host load; its result remains inconclusive.
Its preregistered repeat stopped at a five-minute readiness gate without starting
either arm (minimum host CPU 28.2%). None of these runs proves the RF01 overhead
limit or a speedup. A complete overhead claim must account for the observer's
cost as well as the counters, under the fixed three-pair protocol and a calibrated
executor. Artifacts for the new runs are retained locally under
`.grafx-tmp/rf01-smoke-b18/` and `.grafx-tmp/rf01-control-comparison-1/`;
their `campaign.json` SHA-256 digests are respectively
`fa699b373dc5f3157b99c497bd8f304caeef281b4b5893135cee58eebe2ce8bd` and
`4bf11896a5f5552c7f19446f0d93e5e43fb9576b727e501e88cf524cfaddc420`.
They contain synthetic fixtures and all raw samples, but are not release evidence.

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

## RF04 endpoint qualification with RF03 interaction

The RF04 recipe uses the existing connected N/B/E fixture: 1,024 starts, 128
destinations and 1,151 weighted edges. The selective target has 16 distinct
keys. A second fixture puts at least 80% of edges on 1% of starts; controls
exercise 65 starts (the fan-limit is 64) and 1,024 probes against 16 starts
(actual nonselective node-scan fallback). The oracle derives every ordered row
and parallel-edge occurrence from fixture input, independently of Grafx.

Four **clean, committed** source revisions are needed on one RF01-instrumented
foundation: base, RF03 only, RF04 only, and both. The source isolation is a
separate code-diff/receipt claim; the campaign report checks revision lineage
and never infers patch content from commit hashes. Run four calibrated,
three-pair A/B campaigns with a common positive logical-memory budget:

| Campaign | A -> B | Query profile | Target physical counter |
| --- | --- | --- | --- |
| `rf03` | base -> RF03 only | Original connected `IN` | Node versions examined |
| `rf04` | base -> RF04 only | `IN` + `WITH` + connected traversal | Edge versions examined |
| `incremental` | RF03 only -> both | Original connected `IN` | Edge versions examined |
| `combined` | base -> both | Original connected `IN` | Edge versions examined |

Select all four named cases of the profile in each campaign:
`rf04-{original|with}-{target|fan_limit|hub|fallback}`. Use
`python -m bench.harness.campaign run --mode optimization --intent qualification`
with `--scenario` repeated for each case, `--baseline`, `--candidate`,
`--output`, `--calibrated-executor`, `--calibration`, `--memory-budget` and the
same `--max-cpu-percent` on all runs. For `rf03`, override only the target with
`--target rf04-original-target:physical_node_versions_examined`; the other
targets already select physical edge work. Join the four `campaign.json` files
with `python -m bench.harness.campaign rf04-report --rf03 FILE --rf04 FILE
--incremental FILE --combined FILE --output NEW_FILE`.

The join rejects a missing control, changed fixture/query/budget, changed
executor environment or CPU policy, incomplete A/B, missing physical counters,
semantic divergence, and incorrect source-arm lineage. RF04 candidate targets
must plan the conditional seek and actually issue 16 endpoint lookups with no
global edge scan. `EXPLAIN` may include an unexecuted NodeScan alternative;
runtime `edge_scans`, `edge_lookups`, fallback reasons and physical version
counters decide which path ran. Each sample retains process CPU time, host CPU
observations, logical peak with its observer coverage, and process-lifetime RSS
peak. RSS is not a query allocation. The report includes raw campaign hashes,
per-pair distributions and controls; qualification requires the target's
physical work to fall, target median ratio <=0.90 in every pair, and each
control's median <=1.05 and p95 <=1.10. Noise makes a result inconclusive, with
at most one complete rerun under the existing campaign protocol. No effects
from separate phases are added together as a speedup.

## RF07 streaming landing demand

Open streaming traversals retain scalar destination validation. A known `LIMIT`
does not authorize certifying a larger destination window: a later destination
may fail after the last row the caller needs. Catching and delaying that error
does not undo the reads, checks or observed generation. A published prefix must
never be replayed when a subsequent certificate cannot be stabilized.

This is a negative feasibility result, with no streaming acceleration candidate
or gain benchmark. Existing closed blocking consumers retain their bounded
batches where the proof permits them. Adding `ORDER BY` changes the consumer
and does not demonstrate an improvement to streaming. Reopening the research
requires a proved speculative-read contract without observable effects, or an
explicit consumer demand/failure contract. RF07 introduces neither.

The [protection tests](../tests/query/test_streaming_landing_guard.py) and
[standalone unittest entry points](../tests/query/test_streaming_landing_evidence.py)
use one source, three destinations and two parallel edges per destination.
Faults are injected at real heap and certificate doors, preserving the canonical
port fences so the control can still reach the existing batch implementation.
These protect an existing correct guard; they do not claim to fix a new defect.

| Case | Demand and permitted destination work | Error and cleanup | Unittest method on `StreamingLandingEvidence` |
| --- | --- | --- | --- |
| LIMIT 0 | No destination validation | No destination fault; transaction/cache released | `test_demand_prefix` |
| LIMIT 1 | First valid destination only | Later corruption or duplicate identity remains unobserved until demanded | `test_demand_prefix` |
| Partial close | One row with iterator `batch_size=1` | Idempotent close; no further validation; cursor/buffer/transaction/cache released | `test_demand_prefix` |
| Filter / DISTINCT | Consume until one accepted/distinct result, preserving parallel-edge processing | Later destination fault unobserved; transaction/cache released | `test_demand_prefix` |
| Cancellation / deadline between pulls | No destination validation after control expires | Control error precedes later fault; cursor/buffer/transaction/cache released | `test_controls_cleanup_and_blocking` |
| Generation rejection after published prefix | Bounded retries of the subsequent certificate only | `index_view_changed`, no prefix replay; cursor/buffer/transaction/cache released | `test_generation_failure_no_replay` |

The demand test also forces unsafe batch admission in its isolated test process:
`LIMIT 1` then observes the later corruption before returning its required row.
The compatibility test checks the retained blocking batch: three distinct keys,
six relationships and exact grouped results. No test relaxes CRC, MVCC or
certificate validation to obtain a result.

Demand follows the existing API: iteration may request its configured batch
size, while `fetchone()` requests one row. Early close does not undo work for
rows already requested in a batch. These tests do not promise that the default
iterator batch validates only one row.

The unittest file requires no pytest installation. It imports Grafx from the
`src` directory adjacent to the tested repository snapshot and refuses an
already imported Grafx package from elsewhere. Each named method is executable
by the Pulse external unittest runner. Local runs prove the assertions only;
authenticated scenario evidence additionally requires a registered runner,
the committed revision and the Pulse execution/Delivery workflow.

## Performance policy

Timings, D5 ratios and former throughput floors are **informational**, not release
gates. Quality gates still reject corruption, semantic divergence, broken
durability/recovery, concurrency violations and unexplained operation timeouts.
Run focused tests during implementation, then proportional grouped regressions;
do not repeatedly spend hours proving marginal latency changes.

CI selects `--informational-ceilings` in `python -m bench.harness.gate` to apply
this policy. Valid timing overflows retain their values and an explicit
`informational_ceilings_exceeded` status. Missing or malformed measurements and
recall below the frozen floor still fail. The default CLI mode retains strict
D5 comparison for historical reproduction; CI does not use that mode as a
release gate. Numerical ceilings, raw measurements and the recall target are
unchanged.

Grafx does not currently claim Ladybug parity, constant-time general traversal,
linear CPU/GIL scaling or an RSS bound equal to `buffer_budget_bytes`. Historical
cross-platform comparisons remain [historical](reports/PERFORMANCE_HISTORY.md).

## RF12 capacity decision protocol

The [operational runbook](OPERATIONS.md#capacity-retention-and-maintenance-runbook)
defines the four candidate areas: memory, implicit-ID leases, HASH-key skew and
checkpoint cadence. They are hypotheses for a particular workload, not global
performance gains. Record a separate decision for each one:

1. Pin source revision, database format, configuration, data/seed, concurrency,
   transaction and cursor ownership, warmup, machine load and measurement window.
   Keep the existing setting as control; vary one setting or index action at a
   time on equivalent disposable copies.
2. Collect raw per-run throughput/latency and tails alongside process RSS, logical
   query peaks, page-pool budget, WAL/checkpoint lag, file growth and reader age
   where applicable. Label absent observers as unavailable. Never convert a
   logical tariff, record-slot estimate or configured budget into RSS.
3. Prove exact result and ID semantics, fresh and retained snapshot visibility,
   rollback/commit outcomes, recovery and external-participant adoption on both
   arms. For checkpoint experiments, include a foreign writer's committed files
   and a later reader's visibility across the checkpoint barrier. For a bloat
   experiment, show the pinned report, reader close, separate checkpoint and
   post-checkpoint report without treating the census as mutation.
4. State the benefit and cost with variance and confounders, then choose
   **maintain**, **local adjustment**, or **new investigation/spec**. If a
   correctness check fails or the campaign cannot distinguish the arms, keep the
   current setting. Do not silently change a project default or activate a
   one-way storage capability from this diagnostic exercise.

The proof for a proposed adjustment must belong to that adjustment. A faster
lookup sample does not qualify an ID lease, a lower bloat estimate does not
authorize vacuum, and a shorter checkpoint lag does not establish foreign-writer
visibility. The runbook and existing `test_bloat_report.py` cover the read-only
horizon distinction; fixture-level paired campaigns and explicit decision records
are tracked separately from this documentation change.

The RF12 external-writer matrix includes a 64-lease/32-row reference, equal-work
controls that vary only the lease, batch-load slices at lease 64, and mixed
lease/batch interaction slices. Changing batch also changes the number of rows
in this fixture, so those cuts describe load scaling rather than a causal
setting benefit. Attribute a local lease effect only through an equal-work
pair. Four additional external-writer slices keep the same 512 records and
lease 64, varying only durable transaction size (1, 32, 128 or 512 records).
Their returned ACKs, offsets, row counts, commit-call times and full write
interval are kept separately from read latency; the complete 512-row result
is checked after a read-only reopen. Those equal-work slices can support a
local transaction-size decision. Interaction slices cannot by themselves
qualify an adjustment. The timed interval covers only the read;
update, checkpoint and bloat census costs remain separate diagnostics.

## RF11 full-text statistics matrix

Use the [FTS source-selection recipe](FULL_TEXT_SEARCH.md#choosing-and-checking-a-statistics-source)
to select an index declaration for a particular corpus. The existing `wal` mode
is the control; `durable` is opt-in and activates its already defined capability.
Do not modify a mode in place or infer a new default from a synthetic sample.

| Workload cut | Required comparison | Main benefit/cost question |
| --- | --- | --- |
| First search on a newly opened handle, then repeated search in one snapshot | `wal` versus `durable`; record `snapshot_cache`, `wal_delta`, `durable_summary` or `full_census` actually observed | Does a proved summary remove census work when the local snapshot cache is cold? |
| Read-only reopen after complete write and checkpoint | Same query, dataset and result oracle; separate process-local cache from OS file cache | Is cold/reopen read cost worth summary maintenance and format compatibility? |
| Reader retained across updates, with history capacities 0, 8 and 32 where page size accepts each | Covered and uncovered snapshots, including marker eviction | Does bounded historical coverage justify larger page-0 records and affected-commit copies? |
| Insert/update/delete and concurrent writer controls | One complete durable ACK per atomic unit, replay/reopen, exact scores and corpus | How much write latency, WAL, verification and storage cost moves to maintain the summary? |

For each arm retain source revision, format/capabilities, index declaration, seed,
document/term distribution, query and reader snapshot, cache state, all raw timing
samples, `statistics_regime`, `statistics_wal_records`, `postings_visited`,
`candidates`, `corpus_documents`, hit IDs/scores/order and the exact census oracle.
Include process RSS and logical-memory measures where available, marking absent
observers unavailable. Report write ACKs, WAL bytes, disk growth and verification
separately from read latency. A `full_census` result can be correct; only a
covered summary may claim census avoidance. Corrupt summary metadata must refuse
rather than masquerade as a fallback. Keep timing in the calibrated campaign;
the deterministic CI oracles in `test_fulltext_durable.py` and
`test_fulltext_history.py` establish semantics and recovery, not a speedup.

The maintained `fts-matrix-*` slices in `bench/harness/fts_qualification.py`
separate a newly opened handle from a primed handle, a WAL delta after one
committed update, and a reader pinned across four commits. The input-derived
oracle checks every returned ID, exact BM25 score, tie order and corpus size.
The campaign requires the expected `full_census`, `snapshot_cache`,
`wal_delta` or `durable_summary` source for each slice; merely recording a
source does not count as proving summary coverage. The native FTS path has no
Cypher query-budget logical-memory observation: its peak remains null with an
explicit reason, while process RSS is reported separately. This limits a
recipe comparison to a workload tradeoff, not a comparative logical-memory
claim.

The raw samples keep setup duration and allocated database/WAL file sizes.
For update slices they keep each durable commit ACK duration, total update
duration and signed allocated-byte deltas outside search timing. Allocated WAL
file growth is not logical bytes appended or a count of fsync operations;
setup includes seed insertion, index creation and checkpoint, so it cannot
isolate index-maintenance cost by itself. The OS file cache is uncontrolled
even when the process-local statistics cache is freshly opened. Run only
selected slices with the campaign's `recipe` mode and calibrated three-pair
protocol; these synthetic results do not change the `wal` default.

## RF01 historical total-overhead qualification

RF01 edition 3 permits `run --historical-noop --mode noop --intent qualification`
only against the immutable original baseline
`3a2239a0bda80223c298a68d6d14f3c951f133d2`, with a distinct committed candidate.
Supply `--memory-budget` explicitly and `--max-cpu-percent 20` (or lower),
alongside the existing baseline/candidate, scenario, output and calibration arguments.
This profile currently supports single-statement read controls, including cursor
consumption; FTS, concurrent writers and retained-reader profiles are rejected.

The worker verifies imported engine paths and Git blob hashes. Arm A retains
`logical_memory_peak_bytes: null` with the sole accepted reason
`revision_has_no_logical_memory_observer`; it never fabricates zero or patches the
historical engine. Both arms require real process lifetime peak RSS. RSS is not a
per-query allocation measurement. Arm B requires real logical observation with
the same explicitly configured operator budget as A. This exception qualifies
only RF01 total noop timing, never comparative logical memory or RF02+ gains.
An observer-patched control remains diagnostic rather than the historical baseline.

Each candidate worker also produces separate, untimed lifecycle evidence: twenty
successful sort executions, twenty execution errors after retention, and twenty
partially consumed cursors cancelled on the same private handle per configured
budget. The evidence checks simultaneous logical peaks, individual operator
ceilings, no open transactions, return of live accounting to its prior state and
no retained growth across those repetitions. Operator peaks are never summed.
This is bounded evidence for these lifecycle paths and instrumented accounting,
not a proof that all Python allocations or every query are leak-free. Receipts
bind the procedure digest, engine hashes, revision, process and invocation;
they are unsigned local evidence, not independent authenticated certification.

Semantic hashes, identical contracts, calibration, host CPU at control points
at most 20%, within-process CV at most 10%, three AB/BA/AB pairs, five warmups
and twenty retained samples per arm remain mandatory. Noop limits remain 3%
median and 5% p95; only one entire repeat after an inconclusive campaign is
allowed. Missing candidate memory or missing memory outside this exact profile
still fails closed. Implementation and functional tests do not establish an
overhead pass: a valid campaign is still required, and no gain is claimed.

## RF05 and RF06 qualification selectors

RF05 uses the six `rf05-{first,membership,dynamic_membership,keyset,skip,no_index}-n1024-p16`
cases in an A/B optimization campaign. Select `rf05-first-n1024-p16` as the
target with `--target rf05-first-n1024-p16:physical_node_versions_examined`;
the other five remain timing controls. Apply the same positive `--memory-budget`
to both revisions. The ordered oracle retains the exact public sequence,
including timestamp ties and keyset boundary. The first page must show bounded
`ordered_candidates_examined` and lower physical node work; fallback controls
must not be credited as index wins. The optimization threshold is a median
candidate/base ratio at most 0.90 per pair, with controls at most 1.05 median
and 1.10 p95, calibrated executor, real CPU/RSS and raw hashes as in RF01.
The worker uses the materializing `db.execute` API: `elapsed_ns` and
`consume_all_ns` cover the complete page, while `first_row_ns` is `null`.
This campaign makes no first-row latency claim.

RF06 uses `--rf06-split-memory` and the **exact** six-case matrix returned by
`quota_qualification.qualification_matrix(Scenario)`, in its declared order.
For each of `max_intermediate_rows`, `max_traversal_paths` and
`max_traversal_expansions`, the 128-destination target keeps that traversal
quota at 128 in both revisions and has no `query_memory_budget_bytes`; this
permits the quota-aware batch. The paired control uses the same 128-destination
fixture, query and traversal quota, adding only a 1 MiB query-memory budget in
both revisions. This must retain the scalar path. Do not supply `--memory-budget`, `--target`,
historical flags or a candidate-only memory configuration. Both source arms
run the identical query and configuration for each case.

The RF06 target's logical peak is honestly `null` with reason
`query_memory_budget_disabled` in **both** arms. The target retains real
process-lifetime RSS, CPU and physical `certificate_attempts`; its baseline
must be scalar and its candidate must form two groups of 64 without changing
the 128 candidate landings. Each budgeted scalar control must report a covered
logical peak at or below its budget and no group. After timing, five extra
reads on one control handle
prove that owner/endpoint retained bytes return to the same baseline and no
read transaction remains. This proves the scalar fallback under budget, not a
per-query allocation bound for the unbudgeted target. The report explicitly
marks comparative target logical memory unavailable. The usual A/B temporal,
semantic, physical-work, calibration and noise gates still apply. A final
qualified claim also needs the RF06 Spec's formally revised memory criterion;
this harness profile alone does not revise that criterion.
