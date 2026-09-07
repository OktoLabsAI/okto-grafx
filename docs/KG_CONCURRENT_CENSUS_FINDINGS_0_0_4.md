# Concurrent census findings and rejected aggregate projection

## Live Pulse request observations — 2026-09-07

Pulse PID 15796 remained on the `6eabc14` implementation; the single-adoption
startup improvement `eaa7c65` had not been deployed into that process. Using
the authenticated browser against the existing board, ordinary read-only API
requests returned:

| Request | Isolated, sequential (ms) | Graph + stats + Health together (ms) |
| --- | ---: | ---: |
| Graph, first 500 nodes | 884 | 1610 |
| Stats | 3770 | 10995 |
| Health | not requested | 1423 |

Both graph answers contained 500 nodes / 703 edges with successful edge
diagnostics. Both censuses counted 2779 nodes. Every response was HTTP 200.
The concurrent case had warm service-cache node reads (2.0 ms subgraph,
5.4 ms stats), yet edge counting took 10327.5 ms versus 2658.7 ms in the
isolated request. Authority and dispatch were below 1 ms per phase in both.

This demonstrates a workload-dependent residual, not proof of a specific lock,
GIL or storage cause. Health can return while bounded background probes finish;
its response time does not bound all probe work. The requests were not a
controlled cache-cold experiment. No worker was disabled, no read-lane count
changed and no UI operation was serialized to obtain these observations.

## Tested candidate: aggregate node projection — not integrated

The native closed node-projection proof does not pass through AggregateRows.
An experimental patch extended it for scalar grouping/count pipelines, treating
non-distinct `count(n)` as an entity-presence observation. Its bounded spill
codec carried presence instead of detaching all entity properties. Vector/entity
consumers and distinct entity counts retained full decoding.

The focused node-projection, optional-aggregation and spill suite passed
66 tests in 32.29 s, including small-budget and snapshot/corruption cases.
An initially failing full-entity fixture exceeded the 1024-byte spill budget;
the final fixture compared the identical canonical typed refusal rather than
raising the budget or weakening its contract.

The exact Pulse grouped-node census query and parameters were then run through
native read-only connections against the real board, alternating the full and
candidate proofs. All six answers had total 2779 and fingerprint
`26ff679f5861bc526821181e892a0c2b9c8944d0b9a2d7d904868193f851554e`.

| Round | Full decode (s) | Projected aggregate (s) |
| --- | ---: | ---: |
| 0 | 0.704 | 0.307 |
| 1 | 0.273 | 0.305 |
| 2 | 0.287 | 0.357 |
| Median | 0.287 | 0.307 |

The first full-decode call had a cold cost, and the warm sample did not show a
consistent gain. This is not a statistically established regression, but it is
also not evidence supporting deployment for the present performance objective.
Decision: **do not integrate**. The experimental source and tests were removed
with a scoped reverse patch; their files compare identically to HEAD. Private
diagnostic copies remain in `.grafx-tmp/rejected-aggregate-*.patch` and
`.grafx-tmp/node_census_projection_bench.py`, not in production or the release.

Do not reopen this candidate with prolonged marginal timing gates. The bounded
spill presence optimization is not being introduced separately as new scope.
The next existing target remains the dominant cost of concurrent real Pulse
reads. Do not infer permission to cache authority, weaken physical validation,
disable Health or restrict multi-reader/writer behavior from this experiment.

No graph reset, recovery job, redrive or spec consolidation was initiated.
Cognitive ledger hash remained
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

## Attribution follow-up after suffix-verification deployment

The browser's retained resource timing supplied the missing concurrent stats
observation for PID 36660 without replaying any request: graph started at page
time 57.370 s and took 28.756 s; Health started at 57.371 s and returned after
2.257 s; stats started at 57.372 s and took 18.472 s. The Health response does
not mean all its background probes had completed. The events request was a
long-lived SSE subscription, not a 212-second finite graph query. The later
cursor page took 1.396 s. See the deployment evidence in
`VERIFICATION_VERSION_SUFFIXES_0_0_4.md`.

Re-examining the existing PID 24152 sample also narrows, rather than proves, the
CPU attribution: of 76 `json.raw_decode` leaf samples, 67 belonged to SQLAlchemy
ORM row hydration with no application frame visible above the greenlet boundary.
Only five were clearly attributed to canonical source hashing. It would be
incorrect to label all JSON decoding as the Core source-history hash or to
optimize the latter on that basis. The sample does not identify the ORM table
or establish that its cost remains dominant in PID 36660.

Two finite, read-only diagnostics against the current data then checked simpler
suspects. No worker action, graph mutation or writable Grafx admission was used:

- `CommunityBoardSourceReader.fetch` produced a complete snapshot of 980 rows in
  0.588 s **with cProfile enabled**. Of that, content hashing consumed 0.281 s
  inclusive and quality-context preparation 0.073 s. This measures current board
  sources, not the complete independent cognitive-revision/rebuild audit. No
  lexical JSON shortcut or audit suppression was implemented for this small cost.
- The ordinary routed schema read took 1.969 s instrumented, including 1.809 s
  in read-only admission. A following routed 500-node read took 2.287 s, including
  a different read lane's 1.369 s admission and 0.821 s in native Database.execute.
  Both newly admitted readers adopted 183 indexes once; the previous duplicate
  adoption has not returned. Table high-water discovery still visits 81 tables
  (0.701/0.419 s inclusive), matching the already recorded persisted-high-water
  decision queue. No physical freshness proof was removed to reduce that cost.

These isolated, instrumented reads are not equivalent to concurrent first-load
HTTP requests and cannot prove their cause. They rule out treating the isolated
source reader or single native admission as sufficient explanations of 28.756 s.
The existing CONC-CPU-1 target remains attribution of overlapping first-load
queries and background work, including the ORM hydration boundary; no new
acceptance threshold, source feature, permission expansion or restart was added.
Diagnostic artifacts are local `.grafx-tmp/current_board_sources.prof`,
`profile_current_board_sources.py` and the existing `routed_cold_profile.py`.

## Scoped metadata follow-up — 2026-09-07

Community `9617912` adds schema current_version/validate to the existing scoped
reader scheduler. Admission and all metadata reads remain charged until cleanup;
legacy resolvers, writer bootstrap/migration and original validation semantics
are preserved. The adapter does not claim a new atomic metadata snapshot.
96 focused/surrounding tests passed in 9.04 s, including failure cleanup and
composition wiring. The patch awaits accumulated source deployment, not loaded
in the observed PID 9544.

A 25-second aligned 30-Hz idle-inclusive profile on that PID captured 17469
samples without errors: 132 graph, 174 stats, 783 Health, 16341 other, 39 empty.
Graph/stats returned HTTP 200 in 4.224/5.796 s, with 500 nodes / 703 edges and
zero failed edge tables. Of the graph stacks, 27 entered participant sections
and 15 ended in coordination sleep; for stats those counts were 9 and 4.
These are inclusive sampled stacks, not additive seconds or CPU percentages.
Two earlier short captures missed the actual Refresh and are not attribution
evidence. Private artifact: `.grafx-tmp/pulse9544-refresh-aligned-20260907.json`.

The settings API confirmed generation descriptor revalidation, 8192-byte pages,
64 MiB per handle, no advanced overrides and no pending configuration restart.
The cold/warm gap therefore is not explained by a demonstrated strict-versus-
generation configuration mismatch. No configuration changed and no spec was
consumed. This warm observation neither explains the 59-second cold load nor
proves an end-to-end benefit from the not-yet-deployed metadata patch.

## Operation-local layout mapping

The subsequent 30-second profile of PID 3944 captured 1306 samples, zero errors:
764 Health / 109 census / 58 graph / 375 other. Native batched COUNT was active.
Graph/stats returned HTTP 200 in 4.880/6.614 s during this sample. Repeated layout
mapping still acquired the board route per table before executing queries.

Community `c6a386e` now groups these pure layout translations under one freshly
acquired route/window, without retaining authority across operations. Actual
queries independently reacquire their route and snapshot. Missing/revoked routes
still fail, and incomplete name batches fall back to per-layout diagnostics.
Page eligibility, visibility parameters, query text and counts are unchanged.
89 focused related tests passed in 15.10 s. Read-only 69-name mapping comparison
preserved its ordered digest: scalar 0.324/0.331 s versus batch 0.00888/0.01038 s.
This removes repeated route work, not the larger residual Health/native cost.
See Community `docs/KG_RELATIONSHIP_LAYOUT_BATCHING.md` for exact contracts.

### Runtime validation of layout grouping

Pulse PID 13084 now loads Community `c6a386e`. Previous PID 3944 closed its board
and global graphs with zero failures. A launch attempted during shutdown exited
before serving; the replacement was started only after confirmed terminal state.
The measurement below began after the API reported startup complete.

First Refresh: graph HTTP 200 in 41.543 s (500 nodes / 703 edges / zero failed
layouts), stats HTTP 200 in 28.770 s (2779 nodes / 4424 edges / zero failed tables).
Stats phases: schema 11.262 s, node sample 6.004 s, node counts 6.874 s, edge counts
4.496 s. Graph phases: nodes 24.142 s, edges 16.973 s. The substantial cold cost
already precedes layout mapping; these samples do not establish end-to-end gain.
No timing gate was added and this residual remains open for diagnosis.

Pagination added 500 unique IDs and 897 edges in 3.101 s, zero failures, UI
1000 / total 2779. Specs ledger hash remains unchanged. The next investigation
must isolate first-reader/schema/node admission and concurrent Health work,
rather than extrapolate the isolated mapping improvement to the whole UI.

## Native isolation follow-up and consumer correction

A separate native process opened three explicit read-only participants and
queried every physical relationship table once. Sequential lane wall/CPU times
were 2.261/2.234, 2.786/2.750 and 2.703/2.672 seconds. Concurrent wall times
were 4.800, 4.794 and 4.866 seconds, with per-thread CPU 2.297, 2.438 and 2.391
seconds. This workload made useful concurrent progress, but is not a general
multi-process scaling benchmark or proof of a single GIL/lock cause.

Repeating with three transactions sharing one native handle yielded concurrent
completion times 3.144, 4.697 and 6.196 seconds (per-thread CPU about 2 seconds).
Thus handle sharing alone did not reproduce the roughly 23-second live relation
phases. Do not replace that evidence with an assertion that pool selection is
the established root cause.

All native answers covered **69 unique physical layouts / 4424 edges**, exposing
the consumer's duplicate `supersedes(Decision,Decision)` declaration. Community
`9c9ef1a` deduplicates exact layout triples, preserving order and all distinct
layouts. It passed 55 focused tests in 11.33 s. This fixes inflated census and
verification work, not just a label. Earlier 70-statement/4425 totals included
one physical edge twice; they do not show a graph data mutation.

Pulse PID 7896 now runs this correction and the single-adoption Grafx `eaa7c65`.
REST confirmed 2779 nodes / 4424 edges / 69 layouts / no failures. The browser
loaded 500 nodes and paged to 1000, with zero failed edge tables. Live graph/stats
requests still took 24.758/24.555 seconds even after an isolated census warmed
the instance. The relation phases were 23.717/23.603 seconds. The full Pulse
workload remains the target, not another admission-only or marginal query gate.
No reserved spec was consumed (21 pending / 0 in progress / 19 consolidated).

## Live profile: Health history work overlaps census

A 40-second stack-only profile on PID 7896 captured 1992 samples, zero errors.
The ordinary UI Refresh returned graph in 5.768 s and census in 24.944 s.
Health returned in 2.532 s but its background work continued. Classification:
1257 Health stacks, 244 census stacks, 58 graph-edge stacks, 433 other stacks;
these are not additive wall-time/CPU percentages. Health included 466 JSON
encoding leaf samples and 590 stacks through source enumeration. Census still
spent substantial native work resolving relationship endpoints and validating
storage paths; this profile does not excuse remaining Grafx costs.

The relational cognitive history contains 290 base records plus 5134 revisions,
approximately 53 MB of JSON payload characters. Community's revision decoder
serialized every revision twice consecutively for identical fingerprint
validation. It now uses the freshly computed DTO fingerprint for the storage
comparison, eliminating 5134 redundant hashes without trusting stored digests
or skipping superseded history. Core remains backend-agnostic and unchanged.
Read-only comparison preserved all 5424 records and the final 290-record digest.
Details, timing caveats and test evidence belong to Community
`docs/KG_HEALTH_COGNITIVE_ENUMERATION_COST.md`.

No health probe was disabled, no read participant was serialized, no authority
cache introduced, and no reserved spec consumed. This is an integration cost
reduction, not a claim that Grafx concurrency or the full UI workload is solved.

## Host restart and restored diagnostic baseline

Windows reported `LastBootUpTime=2026-09-07 15:17:45` (local time). The running
Pulse PID 2816 and its exec handle were gone, with neither service port listening.
Two loose Git refs (`heads/feature/v0.0.4` and its origin tracking ref) contained
41 zero bytes. The commit object and reflog were intact; `git ls-remote` confirmed
`b117db7576db1b461d497caef3e017df32fea059`, and the worktree diff against that
explicit object was empty. No claim is made about the cause of the host restart.

The two malformed refs were preserved in the ignored private directory
`.grafx-tmp/git-ref-recovery-20260907-151745/` and restored with `git update-ref`
to the exact remote/reflog-confirmed commit. No checkout/reset, source replacement,
graph reset or database repair was used. Pulse PID 24624 subsequently started
with the same source paths. A read-only API check returned 50 nodes / 73 edges,
69 layouts considered / 60 scanned / 9 skipped / zero failures. Both ports are
listening; pending specs remain 21 / in progress 0 and the ledger hash is unchanged.

A separate routed cProfile after restart localized the remaining native census
cost: 8850 landing-view lookups reduced to 2960 identity resolutions through the
existing memo. Those 2960 scalar exact-index views accounted for 5.061 s inside
the 8.313 s profiled first batch (4.860 of 8.359 s in the second). These cumulative
times include callees and cProfile overhead and must not be compared directly
to uninstrumented UI timings. The private script still included the old duplicate
layout during this capture (70 statements, sum 4425); it was corrected to 69
unique layouts afterward. The production consumer remains corrected throughout.

The next native candidate is bounded grouping of these endpoint validations
inside the existing exact pre/post certificate mechanism, not retained authority
across operations. Existing `validated_versions_many` provides full rows, whereas
the vector-free identity landing path remains scalar. Any integration must retain
snapshot visibility, both endpoint checks, owner overlays, bounded retention,
vector-data validation, custom-hook fallback, whole-batch retry/refusal and query
consumer semantics. This is a candidate for the already-open relation-read target,
not a completed feature or a new acceptance/performance gate.

Follow-up: the bounded COUNT implementation is now integrated in `936515a`,
validated by the focused regression and loaded in Pulse PID 18528. It reuses
the existing bounded landing memo with separate presence keys, reducing native
census certificates from 2960 to 92 with identical counts. Native warm sample
1.462 -> 0.612–0.642 s; live cold graph/stats 19.352/20.855 s and warm stats
2.336 s. The cold full-app residual remains, rather than being reclassified as
complete. See `BATCHED_RELATIONSHIP_COUNT_0_0_4.md` for proofs and limitations.

## Health query groups after the native COUNT improvement

A 35-second stack-only profile of PID 18528 captured 1553 samples, zero errors:
1079 Health, 101 census, 95 graph, 278 other stacks. Health still included 230
JSON encoding leaves and 505 result-collection stacks. These classifications
are not wall-time/CPU percentages. A UI Refresh completed graph/stats in 8.206 s;
the source-history audit and other Health graph work remain material.

Core commit `2a45364` groups the 11 existing relevance queries and 11 layer/maturity
queries into two optional backend-neutral read batches. Query text, parameters,
limits, arithmetic and degradation policy are preserved. Failed/incomplete
batches contribute no prefix and retry the authorized scalar reads. The adapter
owns the engine transaction; Core acquires no Grafx-specific configuration.

Read-only routed scalar/batch/batch/scalar comparison: 22 calls become 2 groups;
all answers identical (2960 nodes, 2555 default scores, mean 0.4939, 2779 canonical
/ 181 working, zero failed node types). Cold scalar 5.532 s, batches 1.012/0.985 s,
warm scalar 1.243 s: approximately 19–21% for these two warm families, not a
fivefold UI improvement. Core's `docs/KG_HEALTH_QUERY_BATCHING.md` records digest,
contract and fallback costs. Tests: 61 Core + 3 Community schema checks passed.
No health probe/history check disabled, no spec consumed, no authority cache.

### Live deployment of Health batching

Pulse 0.3.3 PID 3944 (source runtime, not a rebuilt global wheel) loads Core
`2a45364` plus whitespace-only `7396dbb` and Grafx's native COUNT improvement.
The first Refresh ran before API startup completed and failed to fetch; it is
excluded from timing comparisons. Retry subsequently loaded the graph, but did
not issue a stats request, so a diagnostic waiting for both timed out. A normal
Refresh then returned both HTTP 200: graph 3.981 s (500 nodes / 703 edges), stats
21.527 s (2779 nodes / 4424 edges / 69 tables / zero failures). Stats phases:
schema 90.8 ms, nodes 1007.1 ms, node counts 4281.5 ms, edge counts 16069.5 ms.
This observation does not establish a global latency win; census under the live
workload remains slow despite the isolated improvements.

UI pagination added 500 unique node IDs and 897 edges in 2.673 s, zero failed
edge tables; UI showed 1000 / total 2779. A subsequent read-only Health request
returned HTTP 200 in 0.745 s, graph healthy and layer counts correct (2779/181,
no failed types). This endpoint timing is not proof that all background Health
work completed in 0.745 s. Board/global storage identity correctly reports Grafx.
Pending ledger SHA256 remains
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
