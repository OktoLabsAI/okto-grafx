# Global write-cost attribution — 2026-09-08

Continuation of the existing 22.981 s commit / 131.489 s Global delivery
investigation, not a new performance gate. Baseline native code is `9b41f36`.
The installed Pulse remains on that code (PID 28232). No pending spec, historical
event, redrive, recovery or live graph mutation was used.

## Candidate evaluated and withdrawn

The existing certification profile included full decoding of historical vectors
in the heap scan used for index coverage. A private candidate applied the existing
landing decoder to coverage-dead versions, retaining full values for committed
births with open ends (including provisional deaths). All records, overflow,
schema and vector boundaries still passed validation; public scans and customized
scan/decoder hooks retained full values. No authority cache was introduced.

New tests covered inline/overflow history, provisional births/deaths, deletion
flags, old-vector corruption, malformed overflow, schema mismatch, custom decoder
signatures and exact complete-report equality. Initial fixture failures were
missing embedding-space registration and an incorrect test import; these were
corrected in the harness, not by weakening the catalog. The candidate passed
14 targeted tests, then **747 recovery/landing/import-boundary tests in 17.44 s**
with strict markers and the 60-second thread timeout. Ruff passed.

One sequential read-only comparison on the preserved Global fixture:

| Full verify(all) | Seconds | Pages / records / index entries | Findings |
| --- | ---: | --- | ---: |
| Current full historical vectors | 7.422 | 15,003 / 27,240 / 102,763 | 0 |
| Candidate coverage-live vectors | 7.951 | 15,003 / 27,240 / 102,763 | 0 |

Complete reports matched, but **no wall-time gain was demonstrated**. Cache warmth,
scheduling and one sequential observation do not establish an attributable 7%
regression either. The candidate was withdrawn from production code; its temporary
test was preserved privately alongside its patch. No repeated marginal tuning or
installation followed. The already implemented retention optimization remains
unchanged; this experiment is not its rollback.

Private reconstruction artifacts: `.grafx-tmp/history-vector-candidate.patch`,
`.grafx-tmp/test_verifier_history_vector_validation.py.candidate`, and
`.grafx-tmp/compare_history_vector_validation.py`. The comparison script requires
the candidate patch; it is not a benchmark of current source without that patch.

## Actual Community flush lifecycle on a private copy

Copied the preserved `global-postflush-audit-20260907` fixture to a new, separate
`global-flush-phases-20260908` directory. Called the **actual** Community
`CommunityGrafxGlobalDiscoveryRuntime.flush_after_write_batch`, timing its native
methods without suppressing the checkpoint, cold reopen, schema check or full
vector-index certification. Source code was restored to baseline before this run.

| Phase | Seconds |
| --- | ---: |
| Initial writable open, outside lifecycle | 3.724 |
| Flush | 0.00022 |
| Checkpoint | 0.098 |
| First close | 0.00092 |
| Fresh writable reopen | 4.304 |
| Full verification inside certification | 7.518 |
| Final close | 0.00132 |
| Complete flush lifecycle | **11.946** |

Certification checked 15,003 pages, 27,240 records and 102,763 index entries,
with zero findings. Method timings are nested where applicable, not additive
independent samples. The native opening resolver used default budgets and a no-op
outer application fence **only in this private diagnostic**; production admission,
routing and lease cost are not measured. No event was staged in the copied graph,
so the cheap checkpoint is a quiescent-fixture observation, not a measurement of
the live event's dirty checkpoint. The copy remains available; nothing was deleted.

This narrows the existing follow-up: attribute fresh opening/certification and
the remaining application apply/reconcile work before claiming the 131.489 s
is fixed. It does not authorize removing cold verification or relaxing concurrency,
WAL, OCC, durability, fail-closed behavior or any adoption proof. No additional
performance acceptance threshold or new reserved-spec consumption is required.

Evidence: `.grafx-tmp/global-write-attribution-20260908.json`, SHA-256
`A24DC0CB70584F68920EF60358A5ECEA382C90EEAEFEA13D6BBCB10722A3120E`.
Harness: `.grafx-tmp/measure_global_flush_phases.py` (private copied graph only).

## Board graph commit isolated through the actual Core and Community — 2026-09-08

Replayed the already authored five node/five edge candidates only on a new private
copy, `core-commit-phases-20260908`, of the preserved pre-source-index board fixture.
The actual Community bootstrap created 11 source indexes before timing. The actual
Core `_do_graph_commit` then used `CommunityGrafxGraphTransaction` and native Grafx
0.0.4@807bce6, with connectivity, provenance, reconciliation, scoring and commit intact.
Only the private resolver/outer fence and embedding provider were substituted;
the latter returned an existing valid 384-dimensional vector on each of five calls.

The profiled graph phase took **1.896 s**: 5 nodes and 10 edges added, 26 audit
records and 5 source records returned, no warnings. Post-commit checkpoint and full
`verify('all')` reported zero findings; all five new nodes were read back from the
private copy. No live session, outbox, source ledger or reserved spec was modified.

| Nested profile boundary | Calls | Cumulative seconds |
| --- | ---: | ---: |
| Full `_do_graph_commit` | 1 | 1.896 |
| Native `Database.execute` | 208 | 1.433 |
| Relevance recomputation | 1 batch / 11 nodes | 0.655 |
| Scoring input reads (inside recomputation) | 11 | 0.602 |
| Public result owned-plan recipe construction (inside execute) | 208 | 0.361 |
| Native transaction commit | 1 | 0.347 |

These are overlapping cumulative profiler boundaries, not additive phase timings.
The run excludes real embedding/model work, production health admission, routing
fences, relational audit/outbox persistence, MCP and session finalization. The
copied graph also has different history and cache state. Therefore **1.896 s is
not a new live consolidation result and is not an attributable reduction from
22.981 s**. It localizes work without consuming another reserved spec. No additional
optimization was selected solely from the sub-second entries in this table.

Private harness `.grafx-tmp/profile_core_graph_commit.py` and cProfile artifact
`.grafx-tmp/core-graph-commit-20260908.prof` are retained. The harness mutates its
private copy; rerunning it requires a fresh copy and a distinct private session.
