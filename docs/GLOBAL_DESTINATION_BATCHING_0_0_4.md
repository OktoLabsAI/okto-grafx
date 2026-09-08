# Global integrity queries: bounded destination batching

2026-09-08, `feature/v0.0.4`. Implements the scalar-destination integration left
open by `GLOBAL_DELIVERY_BUCKET_BATCHES_0_0_4.md`; does not close total delivery
latency or the wider evolution plan.

## Implementation

`IndexManager.validated_identity_landings_many` validates record-id-derived exact
keys within the same pre/post-certificate protocol as existing batch reads. Every
matching historical candidate is read through the vector-validating landing decoder;
table, identity and snapshot checks remain. One lost post-certificate repeats the
complete batch; an exhausted retry refuses without a prefix. Column-derived indexes
refuse the landing contract before a view opens.

The query engine feeds at most **64 detached relationship steps** to a batch when
the existing closed vector-free projection proof is satisfied and a blocking sort
or aggregate necessarily consumes the traversal. This accelerates the unchanged
Core Global outgoing/inbound inventory queries, not a backend-specific replacement
query in Pulse Core.

Streaming projections/LIMIT, inner row windows, configured intermediate/traversal/
query-memory quotas, writer overlays, multi-hop/undirected/bound-target/path shapes
and specialized scalar witnesses keep the original path. This preserves bounded
consumption and quota refusal ordering rather than reading past a streaming limit.
An empty edge stream never touches destination-index authority. Missing identity
indexes retain canonical resolution. No fallback hides an adopted-index failure.

The existing `_OwnerLandingView` gains a batch accessor for its **same** private
`vector_free` keys, snapshot/epoch lifetime, LRU tariff and global owner cache budget.
Only misses are sent to native validation. This is important for repeated targets:
batching must not turn one cached Board resolution into thousands of history scans.
It introduces no new authority cache or cross-transaction cache. Full rows/vectors
cannot consume projected markers. Duplicate-identity checks finish before new
batch payloads are retained. Refused retention/eviction does not lose answers;
exceptions and transaction settlement release the same leases and charges.

Candidate/edge validation may be performed before earlier rows reach a blocking
filter; with multiple corruptions, first reported fault order can differ. Every
relevant image remains validated, and no partial aggregate/sort result is published.
No writer/read concurrency, WAL, snapshot, OCC, file format or durability rule changes.

## Actual unchanged-query comparison

Read-only preserved Global fixture with 2,253 inventory rows. Core query text and
all result fields were unchanged; complete results compared equal, including order
for outgoing rows and exact per-digest inbound multiplicity.

| Phase | Scalar time | Batched time | Post-certificates scalar → batched |
| --- | ---: | ---: | ---: |
| Outgoing inventory | 4.771 s | 4.464 s | 2,255 → 38 |
| Inbound inventory | 5.627 s | 3.231 s | 2,253 → 36 |

One sequential private sample, not a controlled full-application benchmark: different
warmth and scheduling limit timing attribution. The small outgoing timing change
was not made a performance gate and was not repeatedly tuned. The structural drop
in certificate calls and identical results are directly observed. The live **131.489 s**
outbox delivery and **22.981 s** commit have not been rerun or declared solved.

Private evidence: `.grafx-tmp/global-landing-batches-20260908.json`, SHA-256
`59F58442F72BEC66CA21357CD39958B1FFF59B5C1BA3A281035F6BFBC45A2D18`.

## Quality and deployment boundary

`tests/query/test_batched_landing_traversal.py`: **29 passed in 15.43 s**.
Covers grouping/order/statistics, 64-step frontiers, duplicate destinations and
cache reuse, streaming/full-vector fallback, pending writer edges, independent
writer with old reader snapshot, empty source, missing index, four operational
quota policies, full-batch certificate retry/refusal, duplicate identity, invisible
candidate validation, specialized hooks, cache eviction/disabled retention, omitted
vector corruption, capability absence and wrong index derivation. An initial test
attempt used unsupported WITH LIMIT syntax and an implicit read transaction for a
write; the fixtures were corrected to the existing supported contracts, not the engine.

Grouped regression covering the new path, relationship counts, full/vector-free
traversals, owner memo lifecycle, lazy landings, cursors, operational budgets,
optional landings, native many-key reads and exact-view fences:
**214 passed in 61.98 s**. Ruff and `git diff --check` pass.

Source-only until an accumulated deployment is explicitly recorded. Pulse PID 2124
remains on installed Grafx 2db169d. No further spec was consolidated, and the twenty
reserved specs, historical DLQ and SQLite data were not changed. The nine recorded
static architecture findings remain open; focused regression is not an all-plan pass.

## Accumulated deployment completed — 2026-09-08 00:57 local

This section supersedes the source-only status above. Built and installed
Grafx **0.0.4@a82d3bf** into Python 3.13 user site-packages, accumulating retention
78aaf93, shared bucket walks 10e79d4 and destination batching a82d3bf. Wheel SHA-256:
`51879D0485B400C5D2A597AD2F1E5BC5D125157988D942947842533E3EE95B57`.
numpy 2.5.2 and google-crc32c 1.8.0 remain installed. No PyPI/main/UV-environment
publication or installation was performed. The earlier wheel remains available.

Pulse PID 2124 was interrupted through its owned session; shutdown completed,
the session was terminal (exit 1), process absent and both ports free before
restart. New Pulse **0.3.3 PID 34048**, session 25464, listens on **8100/8101**.
Core **9e91ea9** and Community **7158383** are source imports; Grafx alone is the
installed wheel. Data home remains `C:/Users/jpamb/.okto-pulse`. Community's unrelated
pre-existing worktree modifications were preserved.

Installed/source hashes agree for query_engine, index_manager and verifier:

- query_engine.py: `360A2C80F049E8689DC5D571AC56B3A2182551D839836EE61C2734A2794BBF59`
- index_manager.py: `28E112E0B0A2F5FB63349B3E4980802EEA0BCBB2CDAF30DE89AAD294C7B6E6A6`
- verifier.py: `094346606CAD7A3CB0BB6C54ACDCDBBED6EC8457F3C7A0E5037E7452E88CAEF0`

Post-deploy evidence:

- Frontend HTTP root returns 200; API and MCP share PID 34048. This is availability
  evidence, not a new browser rendering/performance audit.
- Schema introspection succeeds. Canonical MCP Cypher returns the five previously
  consolidated Alternatives and one Decision link each, 5 rows, no truncation:
  0.333 s full tool call / 183.2 ms executor. No commit was replayed.
- All 20 pending cognitive items exactly equal their pre-stop API projections.
- First cold Health reported budget-limited pending diagnostics, not corruption.
  Follow-up reports board **healthy**, discovery **healthy**, metrics **available**,
  total_nodes **2,965**, queue_depth **0**, board DLQ **0**. Overall remains
  **at_risk** due to historical policy DLQ (439); Global DLQ 224 and one canonical
  debt remain. Some snapshots are stale/refresh-scheduled and native runtime budget
  is incomplete. These are not hidden or relabeled all-green.

Bounded private deployment/readback/Health/pending evidence:
`.grafx-tmp/deploy-a82d3bf-live-evidence.json`, SHA-256
`BD2C759154375D5F0E0D5D87D78C46042EDE43137A630FBFE936F56ADD17CC36`.
