# Clean recovery: reuse the pre-redo heap watermark picture

2026-09-08, `feature/v0.0.4`. Implements a repetition found in the existing
Global writable-opening investigation, not a new performance gate.

## Finding and implementation

The private writable-open profile attributed 7.665 s of an instrumented 8.015 s
open to four complete table-watermark passes (44 table walks over 11 tables).
Profiler overhead means those times are not ordinary open latency. The first
two passes answer the same heap question before replay: first classify stale
indexes without persistence, then persist the verdict after policy acceptance.

Recovery now takes one complete table-watermark picture for these two checks
only when the WAL is clean, the ledger is healthy, the policy permits replay,
the recovery/index/ledger implementations are concrete native collaborators,
and the floor/photo/ledger-repair methods retain their canonical implementations.
The existing commit-section permit excludes cooperating heap publishers throughout
this interval. Native ledger repair is a no-op in this admitted case. No data page
or catalog is changed between the checks.

Both index-header checks still execute and read their own fresh headers. The
picture is only the per-table heap high water, not a cached index certificate,
snapshot or authority bundle. A later stale header remains visible. The existing
check also scans a newly active table absent from its supplied picture.

Damaged WAL/state/ledger, refuse policy and custom collaborators retain the old
separate observations. Actual redo still takes a new picture after page apply
and catalog adoption; final admission after releasing the recovery section takes
another. Nothing survives the call. WAL forcing, quarantine/ledger ordering,
redo, publication, OCC, read/write concurrency and durability are unchanged.

## Evidence

- New tests prove three instead of four walks on a clean one-table reopen,
  identical complete verification reports and fresh observations on later opens.
- Custom floor/photo/ledger hooks retain their prior observations; injected stale
  headers are seen by the second check while the optimization is active.
- Independent-writer updates remain visible, damaged ledgers still repair through
  the original route, and refuse policy leaves the damaged ledger unchanged.
- Focused new/ST-7 slice: 16 passed in 3.91 s before two additional ledger cases.
  Grouped final recovery, new API, checkpoint scope, startup, ST-7 and full import
  boundary suite: **756 passed in 20.87 s**. Strict markers and the 60-second thread
  timeout were enabled. Ruff and diff checks pass.

One sequential private comparison on the separate Global flush fixture:

| Writable connect | Heap-watermark walks | Seconds |
| --- | ---: | ---: |
| Original separate pre-redo pictures | 44 | 2.835 |
| Shared clean pre-redo picture | 33 | 2.409 |

Both returned the exact same 2,261 ordered digest IDs. Timing excludes query and
close, has no cache/order/host control, and is not p95 or an attributable full-Pulse
speedup. The 25% reduction in full table walks is structurally observed. The live
22.981 s commit / 131.489 s delivery measurements have not been repeated or solved.

Local evidence: `.grafx-tmp/recovery-floor-photo-20260908.json`, SHA-256
`6FE1D155588D7E3DAF5BE77C82D1E9087734CBE4C825F6BFBE8A0FDB98DBDD7D`.
Profile: `.grafx-tmp/global-writable-open-20260908.prof`; bounded harness:
`.grafx-tmp/compare_recovery_floor_photo.py`.

## Deployment

Source milestone only, queued for accumulated deployment. Pulse PID 28232 remains
on installed Grafx 0.0.4@9b41f36. No restart, spec consolidation, delivery replay,
rebuild, reset or DLQ redrive was performed. Twenty pending specs remain reserved.
The wider evolution and full-write latency tasks remain open.
