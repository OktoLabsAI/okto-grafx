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
