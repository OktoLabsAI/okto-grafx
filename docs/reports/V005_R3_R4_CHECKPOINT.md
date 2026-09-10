# 0.0.5 R3–R4: overflow reuse and consistent physical backup/restore

Date: September 8, 2026. Branch: `feature/v0.0.5`.
R1–R2 checkpoint committed and pushed as `e2acb25a691463fe9dbf8cbe0d54aa8b65b2f0d4`.
The operator authorized R3–R4 after that checkpoint. No extra roadmap item,
production reset, reserved spec consolidation, install or release is included.

## R3 — persisted overflow FREE-page reuse

`HeapStore._retired_overflow_candidates` supplies reuse indices to the existing
`write_chain` implementation during ordinary row materialization. The protocol
reuses N3's durable evidence instead of introducing a mutable on-disk free-list:

- N3's quiescent ownership/coverage pass, relinking and WAL-atomic FREE publication
  remain unchanged; the required `heap_reclaim_v1` capability and snapshot floor
  must already exist. No catalog bit/disk-format bump was needed.
- Only empty terminal FREE pages with canonical empty flags/reserved fields and a
  committed page LSN at/below the current durable read view qualify. LSN-zero
  abandoned pages do not qualify. Future or malformed FREE images fail closed.
- Current page state is revalidated inside existing commit ordering; a foreign
  writer's consumed page cannot be reused from an old participant cache. Normal
  page-sequence/LSN stamping, both OCC passes, quota admission and WAL-before-data
  own the subsequent allocation. No free-list head can become durable before its
  transaction. Unqualified low-level compositions append instead of guessing.
- Only overflow payload pages are reused: heap slots and `RecordRef`s are never
  reassigned. Old snapshots below the persisted floor remain refused.
- Discovery uses O(1) advisory cursor memory and an incremental fixed-extent scan
  per participant/reclaim floor, not a full scan on every allocation. A new floor
  restarts it. Databases without the reclaim capability avoid the extra header IO.

Measured capacity acceptance: after reopening a 512-byte-page fixture with retired
overflow, a 4,200-character replacement appended **zero heap pages**, with exact
readback and clean verification. This is not a latency/UI benchmark, truncation,
an O(1) indexed allocator, or proof of bounded process RSS. Cold discovery remains
O(heap pages) amortized over allocations. Aborted speculative work may burn local
candidates/unreachable capacity, following the existing no-shrink cleanup contract.

Tests cover reopen, fixed extent, cursor continuation/reset, two independent writers,
a retained reader, an explicitly pre-cached foreign FREE image, stale record
references/ABA, quota refusal before allocation, malformed/future pages, low/high
buffer budgets and refusal after writing overflow. Fault storage exercises before
WAL COMMIT, after WAL barrier, heap apply, index apply and publication: recovery and
a second reopen preserve exactly pre-commit or complete post-commit state.

## R4 — bounded physical snapshot, not no-pause streaming backup

Public functions live in `okto_grafx.backup`, an outer composition module. The pure
engine gained no OS/filesystem dependency and Pulse Core gained no Grafx-specific code.

- `create_backup` uses the existing monolithic checkpoint transition to capture a
  complete durable cut while commit publication and WAL recycling are fenced.
  Other participants remain legitimate; publication waits during checkpoint/capture.
  Memory is explicitly budgeted. Source capture has a between-read time budget.
- Destination IO, read-back/hash validation and complete observational reopen/verify
  run after releasing source fences. Tests prove writer progress there, both for
  another handle/thread and another process. Source commits after capture are absent
  from the artifact and present in the continuing source, as expected.
- An artifact contains a closed manifest and numbered objects, not an openable
  same-UUID database. Canonical file inventory includes metadata, heap, catalog,
  indexes, commit history, retained WAL, commit state and writer-epoch lineage;
  reader registrations/locks/temp files are excluded.
- Restore requires `confirm_original_offline=True`, a new disjoint destination,
  exact sizes/SHA-256, semantic verification and matching identity/checkpoint.
  It releases only the private copied lease while preserving epoch lineage and
  verifies again before no-replace promotion. Original bytes are never deleted.
- Budget, timeout, malformed manifest, duplicate JSON keys, traversal, corruption,
  altered UUID/cut, IO failure, interruption and destination appearance all refuse
  before successful promotion. Manifest validation is exercised under `python -O`.
  An authoritative source CRC failure is refused without source repair.

The [consumer contract](../BACKUP_RESTORE.md) documents every parameter, report field,
error behavior, memory/temporary-space cost, Windows/Linux publication mechanisms,
process-death staging residue, hardware/platform limits and offline assertion.
No generic logical export, automatic repair, independently writable same-UUID fork,
streaming/custom-storage backup or zero writer-pause claim is included.

## Acceptance evidence

Implementation and local acceptance are complete. No production installation or
release is implied. Evidence (counts overlap):

| Scope | Result |
| --- | --- |
| Grouped storage, transactions, recovery, WAL, indexes, API, bootstrap, import boundary and language contracts | **5,986 passed, 1 platform skip, 592.35 s** |
| Focused R3/R4, crash cuts, documentation and public API annotations | 1,388 passed, 64.63 s; overlaps the grouped suite |
| Coordination, including native participant/lease/reader contracts | 562 passed, 3 platform skips, 23.74 s |
| Final R3 guard delta, heap-store regression and backup tests | 279 passed, 24.62 s; covers final capability fast path and FREE flag checks added after the grouped run started |
| Final complete backup-specific suite | **20 passed, 18.54 s**; also covers relationships, vectors, WAL-v2 capability and abrupt process death during artifact output |
| Pulse Community transaction/provider, independent read lanes and orchestrator integration | **114 passed, 116.60 s** against current Grafx source; isolated fixtures, not live boards |
| Latest wheel build/install into the separate validation environment | PASS: overflow reuse, backup, restore, identical provenance, writable reopen; earlier metadata/history/qualified-transfer wheel smoke also passed |
| Static checks | Changed Python files pass Ruff; backup module passes selected mypy; generated API and documentation checks pass |

Local receipts (not shipped or production data):

- `.grafx-tmp/v005-r3-r4-regression.txt`
- `.grafx-tmp/v005-r3-r4-focused-final.txt`
- `.grafx-tmp/v005-r3-r4-coordination.txt`
- `.grafx-tmp/v005-r3-r4-final-delta.txt`, `v005-r4-final.txt`
- `.grafx-tmp/v005-r3-r4-pulse.txt`
- `.grafx-tmp/v005-r3-r4-build.txt`, `v005-r3-r4-mypy.txt`
- `.grafx-tmp/v005-r3-r4-install.txt`, `v005-r3-r4-wheel.txt`

An initial focused run exposed missing helper annotations in the new public module;
those annotations were added and the public-surface group passed. Test fixture
setup/clock-injection mistakes were corrected before the reported passing runs.
Counts overlap: do not add them as distinct coverage. Local Windows/Python 3.13
evidence is not a full OS/Python compatibility matrix or production deployment.
