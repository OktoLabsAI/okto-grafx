# 0.0.5 performance implementation and bounded validation

September 8, 2026. Evidence, not an additional active plan. Status and outstanding
work remain in the [roadmap](../../ROADMAP.md#next-iteration-assessment-featurev005).
The first sections preserve the initial implementation checkpoint. The
[follow-up](#bounded-follow-up-and-disposition-of-the-six-selected-items) closes
the six bounded 0.0.5 actions, not every broader PERF/OPS workstream.

## Scope and implementation

- Query preparation: 256 retained plans aligned with 256 parsed statements. A
  160-statement cycle now builds each plan once across two passes. Retention has
  a 16,384-character text ceiling and 32 MiB conservative per-engine plan tariff.
  Over-capacity statements execute without retained plans; eviction releases root
  ownership. Statement-authority memo admission requires retained parse identity.
  Catalog, index-generation/staleness and dirty-owner key fences remain unchanged.
- Numeric IN: bounded detached numeric/text/bytes/null lists use statement-local
  membership keys. Numeric keys reproduce the existing float-normalized `_equal`,
  including int64 rounding above 2**53, signed zero and infinity; NaN never matches.
  Booleans, custom numeric objects, nested values and oversized inputs retain the
  canonical path. The existing 4,096-element shared statement quota remains.
  This removes the repeated list walk for supported probes; it does not eliminate
  required heap/index validation or make general graph enumeration constant-time.
- Writer preflight: an exact native WRITE transaction with no staged effects may
  reuse scalar PK values for read-only statements. Every read still opens its
  fresh stable-view certificate. Write plans, staged owners and custom witnesses
  retain the canonical door. Foreign-writer snapshot and subsequent OCC conflict
  tests, read-partition parity and failed post-certificate refusals are retained.
- Top-k: use `heapq` for push/replace/pop instead of Python heap-maintenance loops.
  Preserve the comparator, stable ordinal tie-breaker, SKIP+LIMIT retention,
  deferred projection, budget checks and complete required input validation.
  This is lower Python overhead, not a GIL-release or lock-free-publication claim.

No new public options, on-disk capability or WAL/OCC protocol was introduced.
The incomplete CAP-1 writer/public paths remain disabled. Pulse Core is unchanged.

## Latest native synthetic observation

Command, with this checkout's `src` on PYTHONPATH:

```text
python -m tools.perf_round.round005_native
```

The harness refuses an installed-package import, creates fresh temporary local
stores, populates one two-column table, checks exact first/next-page rows, writes
11 nodes, checkpoints and verifies count after reopen. It never opens Pulse data.
Windows 11, Python 3.13.1, Grafx source 0.0.5, default connect options. No relationships
or indexed vectors are in this fixture; it is not a complete KG rendering workload.
Each read is sampled twice; table below shows the last/warm observation only.
Tracemalloc is enabled for the write phases and affects their elapsed times.

| Initial nodes | Selective first page | Selective next page | Unindexed range page | 20 preflight reads | Stage 11 nodes | Commit 11 nodes | Python traced write peak |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 128 | 8.068 ms (128 rows) | 0.932 ms (empty) | 3.964 ms | 26.139 ms | 18.554 ms | 37.006 ms | 191,466 bytes |
| 512 | 28.484 ms (500 rows) | 2.422 ms (12 rows) | 14.141 ms | 25.264 ms | 15.721 ms | 34.439 ms | 194,140 bytes |
| 2,048 | 31.516 ms (500 rows) | 31.067 ms (500 rows) | 38.900 ms | 25.901 ms | 18.013 ms | 41.878 ms | 197,341 bytes |

Selective pages use explicit numeric ID lists, not a general pagination token.
The unindexed range still scans N rows. Different result sizes in the small fixtures
must not be presented as constant-time scaling. Python traced allocations exclude
native/OS memory and earlier allocations; they do not establish a process RSS cap.

Independent-handle runs used 1, 2 and 4 threads. The 4-participant case had two
writers committing four distinct rows each and two readers making four point reads
each; exact final count passed with zero retries in this sample. Writer elapsed
times were 122.304/147.058 ms and reader times 35.832/35.016 ms. Workloads are small,
not throughput/p99 measurements, and this harness is not a multiprocess crash test.

Private JSON: `.grafx-tmp/round005-native-final.json`, SHA-256
`7e90a6915a10e9ad7b073562a042e042c06e44ea4f857b65c5899bdb222eeef5`.
It records query operation counts, all samples, reopen/checkpoint phases and source
hashes. `query_engine.py` SHA-256 at measurement:
`368540b4f0e753246d1f10184fe16cc120c50952f638cbe32003496b83f4d9ae`;
harness SHA-256 `5d4b33409b8c50dd5b83629c4502b43170c73fa9d10d69c7283682d78d4d729e`.

## Validation and limits

- Regression cases reproduced the old 160-shape cyclic planning and unsupported
  numeric memo before implementation. Numeric membership includes differential
  hostile/edge values and a test forbidding linear fallback for 1,000 supported
  probes into a 500-element list.
- Query suites plus retained catalog views, public-boundary concurrency, import
  boundaries and documentation consumers: **2,841 passed / 306.28 s**. One grouped
  run covers the accumulated changes; no latency threshold was imposed.
- Final numeric/cache/writer slice: **137 passed / 5.04 s**, overlapping the group,
  including final read-partition parity and warmed-preflight foreign OCC refusal.
- Community source `7158383` with its existing local changes and Core `9303f98`:
  `test_grafx_cypher_executor.py`, `test_grafx_read_lanes.py`,
  `test_grafx_graph_store.py`, `test_kg_routes_grafx_relationship_layout.py`:
  **113 passed / 28.45 s** with Grafx 0.0.5 imported from this checkout. These include
  real engine store tests and stubbed routing contracts, not a full live UI benchmark.
  The first invocation selected an obsolete Core worktree and failed collection;
  using the documented matching Core checkout resolved the environment mismatch.
- Ruff passes changed Python files. Strict mypy with explicit MYPYPATH and separate
  nonincremental caches has **254 identical pre-existing diagnostics** against the
  tagged 0.0.4 query engine after normalizing line offsets; no new diagnostics.

The initial checkpoint left route/render, Global, churn/RSS and concurrency
attribution open. The bounded follow-up below supersedes that checkpoint status;
it does not turn a synthetic experiment into a production-consolidation receipt.

## Bounded follow-up and disposition of the six selected items

September 8, 2026. No further engine change was necessary after the four changes
above: the follow-up closes their consumer and lifetime checks, and records where
larger work is **not** established by these measurements. No timing floor was used.

### 1 and 6: actual consumer statements, route and renderer

`tools/perf_round/round005_pulse.py` creates the real Community board manifest,
seeds 128/512/1,100 Decision nodes and a hub with up to 128 `supersedes` edges,
checkpoints/closes, then opens an independent handle. It invokes real KGService,
routed facade, Grafx executor, `/kg/boards/{board_id}/graph`, JSON serialization
and cursor codec. Only authorization, route authority and service result caching
are fixture replacements; no production SQLite, workers or credentials are used.

The initial fixture accidentally supplied a backend executor directly to a route
that expects the routed facade's board-qualified signature. It failed with exact
per-layout errors. Using the actual facade fixed the fixture; no product patch or
suppression of diagnostics was needed.

| Fixed workload | Latest observation | Checked result |
| --- | ---: | --- |
| New handle open, full Pulse schema | 2,718 ms | Separate from request timing; not an OS-cold-cache guarantee |
| First HTTP page on new handle, 1,100 nodes | 428.332 ms | 500 exact identities; 128 incident edges |
| Warm first HTTP page | 221.157 ms | 110.637 ms nodes / 75.710 ms edges; remaining time includes HTTP/JSON |
| Warm next HTTP page | 220.581 ms | 500 different identities, exact incident edge set |
| Warm first page at 128 / 512 nodes | 108.625 / 227.891 ms | 128 / 500 identities |
| Warm prepare rebuilds, one node query + 13 relation queries | 0 parse / 0 plan | 16 total retained plans, 2,161,808 tariff bytes |

The page type selects **13 of 69 distinct layouts**, skipping 56. This selection
and one-snapshot relation batching already existed in Pulse; they were not
reimplemented. The warmed node iterator examined **128/500/500 candidates** at
the three graph sizes, not all 1,100 nodes for the final 500-row page. Count returns
the exact total 1,100. Full result enumeration/verification still scales with data.
This one-type fixture does not exercise every possible mixed-layout working set;
the separate 160-shape regression covers the cache's larger cyclic capacity.

The profiler locates repeated work in ordered candidate validation/heap decoding,
projection and relationship reads. Preparation is not the dominant warm KG cost.
Do not add an authority cache, persisted prepared API or another batch abstraction
to this release on the basis of this fixture.

Browser verification uses `tools/perf_round/pulse_ui/`: **actual Pulse API client
and GraphCanvas/WebGL renderer**, with small fixture controls instead of the full
KnowledgeGraphPage shell. Chrome visibly loaded 500, 1,000 and 1,100 nodes, retained
128 edges and disabled the next-page button at end of cursor; reset returned to
500 without duplicate identities. Latest warm reset: **269.7 ms HTTP/JSON + 67.6 ms
to the second animation frame**. Next-500 observation: **309.4 + 367.2 ms**. The
API was already warm before the first browser request. Frame timing is not GPU
completion or ForceAtlas2 convergence (the component has a 2,500 ms settling timer).
Vite development transforms/browser extensions and host load affect these samples.
This is not a full authenticated live-Pulse or Ladybug A/B benchmark.

### 2: fixed Global write, dispatch and verification boundaries

The real Community Global runtime creates a Board and four 384-dimensional
DecisionDigest vectors and their links. Each write keeps its fence callbacks and
ordinary durable commit. Core GlobalOutboxProcessor's unchanged digest/outgoing/
inbound inventory functions run through its real off-loop graph dispatcher.

Latest individual digest writes: **30.430–36.846 ms**; link writes:
**26.024–35.283 ms**. The three four-row inventories took **5.763 / 9.394 / 7.881 ms**,
with a **26.571 ms** dispatch round trip including creation of the test event loop.
Explicit full native verification took **838.249 ms** and checkpoint **188.709 ms**.
All four links/digests and inbound multiplicities matched, verification found no
issues and cold reopen retained all four digests. Full `verify('all')` here is an
explicit maintenance measurement, **not** a claim that every outbox ACK invokes it.

Phase timing/cancellation/fence/inventory tests also pass (39 Core tests). No new
spec or LLM operation was needed. Production SQLite admission, live queue waiting,
embedding generation and the full 54.952 s historical ACK remain **unattributed**:
they are outside this isolated fixture, not zero and not evidence of a native
30-second commit. Closing this bounded Grafx experiment does not claim that the
historical end-to-end Pulse latency has been solved. A new production receipt is a
separate deployment/consumer follow-up, not a reason to remove verification now.

### 3 and 4: skew, index paths, update/delete lifetime and RSS

`round005_lifetime.py` holds one old read snapshot while 1/2/4 additional handles
perform 12 cycles. Each cycle updates eight of 64 rows with 5,003-byte strings and
an indexed category, commits a temporary node and deletes it in a subsequent
transaction, then executes 24 new statement shapes per handle. Every current read
sees the new value; the pinned snapshot always sees the original. Final count is
64; eight rows have the final indexed category. Full verification and reopen pass.

| Active handles, plus one pinned reader | Latest cycle RSS | Process peak working set | Cache outcome |
| --- | ---: | ---: | --- |
| 1 | 50,401,280 bytes | 52,273,152 bytes | At most 256 plans per handle |
| 2 | 51,363,840 bytes | 53,506,048 bytes | Same bound, accurate released owner budget |
| 4 | 54,210,560 bytes | 56,078,336 bytes | Same bound, under 32 MiB tariff per handle |

These are sequential observations in one process; peaks include previous samples.
RSS after closing handles need not drop immediately because the Python allocator
retains arenas. In the separate full-Pulse import/fixture process, initial RSS was
270,880,768 bytes and process peak 359,698,432 bytes: do not attribute all imported
FastAPI/SQLAlchemy/Pulse memory to Grafx caches.

The small churn fixture's index files stayed at **1,597,440 bytes**, while heap
storage ended at **802,816 bytes** (81,920 after the first update cycle). Old/overflow
storage is not reclaimed by this change. The run did not reach the 4,096-bucket
directory cap or prove a new index-growth bottleneck. Existing overflow reclamation,
file shrinking and wider index-generation retention remain OPS-3/4/5; no unsafe
physical reclamation was substituted for the snapshot contract.

### 5: CPU versus non-CPU time with the actual page shape

Reuse the 1,100-node fixture with 1/2/4 independent handles and a synchronized
start. Every participant reads the exact 500-node query twice; even slots also
commit two updates. Four participants therefore include **two legitimate writers**.
All expected writes persisted, all page identities matched, and no conflict retry
was needed in this sample. There is no throughput floor or linear-scaling claim.

One participant: **418.676 ms wall**. Four participants: readers **897.867/898.808 ms**,
writers **1,299.789/1,249.122 ms**. Per-thread CPU is also recorded; non-CPU intervals
include GIL scheduling, OS scheduling and I/O, not just a measured database lock.
Windows CPU-clock granularity can make CPU slightly exceed wall time. The evidence
supports retaining independent participants and the existing CPU-local fixes, not
claiming that adding Python threads makes CPU-bound queries linearly faster. Native
kernels/process execution/publication redesign remain explicit later OPS-6 work.

### Reproduction and final checks

Set `PYTHONPATH` to this checkout's `src`, and `OKTO_PULSE_COMMUNITY_REPO` /
`OKTO_PULSE_CORE_REPO` to matching source checkouts. Runs here used Community
`7158383` and Core `9303f98` with existing local changes. The optional harnesses
require the consumer's test dependencies and `psutil`; they add **no runtime
dependency to Grafx**. Run `python -m tools.perf_round.round005_pulse --nodes 1100`
and `python -m tools.perf_round.round005_lifetime`. For browser inspection add
`--serve` and run the consumer's existing Vite binary with
`--config tools/perf_round/pulse_ui/vite.config.mjs`. Both listeners bind loopback
18105/18106, use synthetic data only, and were stopped after verification.

- Additional Community group: **92 passed / 61.82 s** (ordered indexes, Global
  lifecycle/visibility/link batching, executor/read lanes and layout diagnostics).
- Core phase/cancellation/fence/inventory group: **39 passed / 4.75 s**.
- Final numeric/preflight/preparation/documentation slice: **149 passed / 9.43 s**,
  overlapping earlier coverage. Ruff, documentation links/contracts and generated
  API-reference freshness checks pass; strict mypy baseline remains as recorded above.
- Candidate 0.0.5 wheel builds and passes isolated **stdlib-only** install smoke:
  version/import origin, 160-shape cycles, numeric IN, ordered results, checkpoint,
  close/reopen and complete verification. This is not a live Pulse installation.
- Private final artifacts: `round005-pulse-{128,512,1100}-final.json` and
  `round005-lifetime-final.json` under `.grafx-tmp/`. The 1,100-node JSON SHA-256 is
  `8c167e59a7a7f4b6855161eb51d31819403f87f3572066a53ea41cbc014b0a0f`;
  lifetime JSON: `c144a55b5457a6d6a608fec571cf3f41cdcd53e370927a6fcd167625706a7eaf`.
  JSON also captures engine/harness hashes. Wheel SHA-256:
  `cef004adf7d04bda8ec02dc410b9867c2212b0729fab20c2fc2d1ec7acd58203`.

The six **bounded 0.0.5 actions** are closed: four engine optimizations plus their
consumer, scale, preparation, memory and concurrency validation/dispositions.
This is not closure of the entire PERF/OPS roadmap, production deployment, a new
real-spec consolidation, PyPI publication or a new durability protocol.
