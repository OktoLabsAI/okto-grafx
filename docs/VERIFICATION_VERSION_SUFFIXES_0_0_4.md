# Linear healthy MVCC history verification — 2026-09-07

## Scope and evidence

This addresses the already recorded full post-flush verification cost in
`PULSE_COGNITIVE_WRITE_AUDIT_2026_09_07.md`, not a new acceptance gate. Pulse's
Community adapter still calls `database.verify("all")` before certifying Global
vector indexes. The Core and the adapter's publication, durable-header, coverage
and ACK requirements are unchanged.

A read-only profile of the preserved private Global copy (publication LSN
140805, heap 102,473,728 bytes) found 22,717 `HeapStore.version_chain` calls and
1,809,661 `_read_slot` calls. `Verifier._verify_record` walked the complete older
history for every stored version carrying a predecessor. A history with V
versions therefore caused roughly V(V+1)/2 slot visits. This is avoidable
repeated work, not WAL/fsync latency.

The heavily instrumented cProfile run took 304.28 s, with 262.18 s inclusive in
`version_chain`. **Do not use that time as an uninstrumented production baseline.**
Local diagnostic artifacts, not portable fixtures:
`.grafx-tmp/global-verification-20260907.prof` and
`.grafx-tmp/profile_global_verification.py`.

## Implementation and guarantees

- `HeapStore.version_chain` delegates to the same canonical walker and still
  returns every reference, newest first. Its public signature is unchanged.
- Only `Verifier._verify_versions` supplies a private dictionary of successful
  suffix lengths to `_walk_version_chain`. Each new prefix is checked using the
  original slot reader, header decoder, cycle check and finite physical bound.
  A previously successful suffix terminates repeated traversal. Its complete
  length still participates in the independent finite-walk bound.
- No partial result from a failed walk enters the dictionary. Failed chains keep
  the full refusal path and per-record findings; corrupted histories are not
  promised linear-time diagnosis.
- The dictionary lives for one table pass in one verification call. It contains
  physical reference integers and lengths, not payloads or vectors. Memory is
  O(distinct visited versions), released before the next table. It is not stored
  on the heap, verifier or database and cannot authorize a later call.
- Only the exact built-in heap with unchanged public chain and slot-reading
  methods uses the optimization. Custom collaborators/hooks retain the original
  per-record calls.
- Independent device-page decoding, whole-file ownership/orphan/counter checks,
  every stored record header/length/lifetime (including historical versions),
  all index walks and heap coverage remain intact. No checksum, dead version,
  vector index or table is sampled or skipped.
- The existing fresh-read-view/participant section remains unchanged. This does
  not add a cross-process lock, extend authority across calls, or turn verification
  into a new atomic-snapshot guarantee. Ordinary updates append versions; the
  historical predecessor links are not rewritten by this optimization. Existing
  quiescence requirements for destructive history maintenance remain in force.

For healthy histories the repeated chain work becomes O(V), independently of
oldest-first versus newest-first traversal. Other verification work is unchanged.
There is no new configuration, format, durability mode or Pulse Core dependency.

## Bounded comparison

One uninstrumented A/B used separate read-only handles on the same preserved
private copy. The baseline disabled only the new private verifier optimization;
both runs used `verify("all")`, page size 8192 and descriptor revalidation
`generation`. The baseline ran first, so OS-cache/order effects are not controlled.
This is a component observation, not p95 or an end-to-end consolidation claim.

| Result | Full suffix rewalk | One-call suffix reuse |
|---|---:|---:|
| Verification seconds | 64.359 | 16.075 |
| Pages checked | 15,003 | 15,003 |
| Records checked | 27,240 | 27,240 |
| Index entries checked | 102,763 | 102,763 |
| Findings | 0 | 0 |
| Publication before and after | 140805 | 140805 |

The complete `VerificationReport` objects compared equal. Observed reduction:
approximately 75%, or 4.0x for this stage. Deterministic 32/128-version tests also
prove exactly V newly walked references instead of quadratic repeated suffixes;
the complexity assertion does not depend on timing.
Diagnostic: `.grafx-tmp/compare_verification_suffixes.py`.

## Quality and delivery

- Initial affected slice: 353 tests passed in 4.14 s, covering new suffix tests,
  existing verifier, index verification and heap storage tests.
- Final focused/foreign-publication slice: 16 passed in 3.94 s, including 14
  suffix tests (overlapping the first slice) and existing cross-process freshness
  and long-lived read-only foreign-index adoption tests.
- Corruption parity includes cycles, self-cycles, absent pages, descriptor-slot
  references, freed slots and malformed headers; full reports match the original
  path. Reusing the same verifier after injected damage must still report it.
- Public full-chain results, custom chain/slot hooks, successful newest-first
  suffix reuse, no cache poisoning on failure and finite bounds are covered.
- Final native suffix file: 15 passed in 3.45 s (overlaps above), adding a
  long-lived read-only verifier that sees an independently committed new history
  version after checkpoint, with record counts increasing from four to five.
- Community Global discovery/index slice: 24 passed in 135.46 s. This includes
  cold durable-header certification, coverage/publication refusal, exact lookup,
  replacement/reopen and ACL behavior. The initial collection selected a different
  checkout and failed imports; rerun with both explicit Core/Community repository
  overrides passed. Test data home was isolated from production.
- Ruff and whitespace checks passed. Live Pulse `/health` remained HTTP 200,
  healthy, version 0.3.3. The reserved cognitive ledger remained byte-identical:
  SHA256 `4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

Source milestone only until an explicit accumulated Pulse restart is recorded.
No live consolidation, delivery replay, redrive, rebuild, graph reset or debt/DLQ
cleanup was performed. The 21 pending specs remain reserved for future write
benchmarks. This closes the quadratic verification subtask, not all write latency
or the complete 0.0.4 performance plan.

### Accumulated runtime deployment

Grafx source commits `c0cc3e1` (unused optional endpoint vectors) and `0fee5b4`
(history suffix verification) were loaded into Pulse 0.3.3 PID 36660 on the same
default data home. PID 24152 closed both graphs with zero board failures and
reached terminal exit before replacement; both listener ports were absent.
Startup explicitly reported the Grafx source path and both new capabilities.
This is source-runtime integration, not a globally installed wheel or PyPI release.

The browser rendered 1000 / 2779 canonical nodes after one `Load more` action.
Observed real UI graph responses: 500 nodes/703 edges in 28.756 s after restart,
then 500 nodes/897 edges in 1.396 s, both HTTP 200, `edge_read_status=ok` and zero
failed tables. These are two different pages and cold/warm conditions, **not**
an A/B comparison. An authenticated stats GET returned HTTP 200 in 2.579 s,
2779 nodes and 4424 edges across 69 relationship layouts with zero failures.
The first-load latency remains open; no further load/restart was repeated to
chase a marginal timing result. Initial browser observer setup needed a local
compatibility correction; requests were not replayed because of that observer error.

Visual evidence: local `.grafx-tmp/grafx-0fee5b4-runtime-1000-nodes-20260907.png`.
The overlay was closed after the check. `/health` reports healthy 0.3.3, and the
cognitive API confirms 21 pending, zero in progress, 19 consolidated, zero failed,
40 total. The ledger SHA256 above is unchanged after restart and UI/API reads.
No consolidation, redrive, rebuild, delivery replay or reset was initiated.
