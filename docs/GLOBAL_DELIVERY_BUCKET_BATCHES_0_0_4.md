# Global delivery: read attribution and shared batch bucket traversal

2026-09-08, `feature/v0.0.4`. Follow-up to the single-spec installed experiment,
not a new consolidation, timing gate or change of concurrency/durability contract.

## Read-only attribution

The original private diagnostic's output was lost. Its handle was subsequently
missing and process inventory confirmed no matching process. A new read-only run
persisted each phase before proceeding; it was not restarted on an observation
timeout. Both fixtures are preserved private copies, not the live application.

| Existing phase | Seconds | Result count |
| --- | ---: | ---: |
| Board active source inventory | 0.797 | 2,786 |
| Board revoked inventory | 0.309 | 33 |
| Board source-layer metadata | 0.658 | 2,786 |
| Board fresh source revalidation | 0.307 | 2,786 |
| Global digest inventory | 1.096 | 2,261 |
| Global outgoing edge inventory | 4.778 | 2,253 |
| Global inbound edge inventory | 4.698 | 2,253 |

Board reads use the real Community executor with a private read-only graph resolver;
Global reads use Core's existing query helpers against native Grafx. Board repeated
inventory and metadata identities agree. These old fixtures differ from the live
board; timings exclude opening and are not an attribution of the entire 131.489 s
live delivery. No currently dominant Board inventory cost was demonstrated here.

A separate profile of the outgoing Global query found 2,253 destination identity
lookups, 23,046 landing reads and 824,869 entry-image checks. Its 16.508 s profiled
runtime includes profiler overhead; do not compare it as an uninstrumented sample.
Repeated bucket traversal and retained-version validation dominate this shape.
Historical candidate validation cannot simply be skipped: unreadable candidates
remain corruption even when invisible to the current snapshot.

## Native implementation

`IndexStore._candidate_groups_unchecked` groups already-canonical batch keys by hash
bucket. `_scan_bucket` validates each requested chain once and collects matching
entries in that pass. `IndexManager.validated_versions_many`, its generation-bound
reuse variant and `validated_identity_counts_many` consume these groups. Existing
batched PK seeks and closed relationship counts therefore use the optimization.

Guarantees retained:

- Every entry image in a visited bucket is validated, including unrequested keys.
- Chain type checks, cycle detection and the independent length bound are unchanged.
- Every matching candidate is decoded/validated against the heap, including history;
  key, table and snapshot predicates are unchanged. Counts retain multiplicity.
- Per-key chain/slot order and final input-position order (including duplicates and
  misses) are unchanged. Intermediate cross-key validation can run in bucket order:
  if multiple faults exist, which fault is reported first may change; no partial
  result escapes. The batch's pre/post-certificate and complete retry remain intact.
- There is no persistent bucket cache, cross-transaction authority reuse, new format,
  skipped WAL check, altered OCC or reader/writer serialization.
- Specialized index implementations or scalar candidate/scan hooks use the original
  scalar path. Single-key buckets also retain their scalar path.

Auxiliary memory is O(requested keys + matching entries in the current bucket),
in addition to existing result storage. This is not constant-memory enumeration or
history removal. Bucket result staging is private to one attempt and discarded;
it does not grow across statements. Existing bounded query frontiers remain bounded.

## Evidence and limits

New structural/adversarial tests in `tests/index/test_many_bucket_traversal.py`
prove one pin per chain page, scalar entry-order parity, whole-batch retry/refusal,
corruption in an unrequested entry, chain cycles and specialized-hook fallback.
The existing many-key tests additionally cover snapshot visibility, duplicate keys,
wrong-key candidates, foreign tables, proximity refusal and cached-generation reuse.

Combined index suite plus batched relationship counts, vector-free traversal and
scalar/batched/incremental PK regressions: **729 passed in 85.70 s**. Ruff and
`git diff --check` passed. Initial six new tests exposed only an invalid test
instrumentation attempt to replace a slotted BufferPool instance method; moving
the observation to its class fixed the harness, with all six passing in 0.44 s.

One 256-key native identity-count batch on the retained Global fixture returned
256 identical counts of one in both implementations. Bucket-page pins fell
**512 -> 128** (64 buckets). Single sequential timing sample:
**0.597 s -> 0.361 s**. Different cache warmth/order limits timing attribution;
the structural 75% reduction in bucket pins is directly observed. This does not
measure total commit/delivery or prove a corresponding application speedup.

At this primitive checkpoint, the profiled outgoing Global query still resolved scalar destinations.
This checkpoint removes repetition in the existing batch primitive; bounded
destination batching for that full scalar-projection query remains the next
integration step, with snapshot, overlay, result-order and refusal tests required.
The 4.778 s query is **not** claimed fixed by this primitive alone.

Subsequent implementation of the bounded destination integration and its separate
unchanged-query measurements are in `GLOBAL_DESTINATION_BATCHING_0_0_4.md`.

## Deployment and preserved state

Source-only checkpoint. Pulse PID 2124 still uses installed Grafx 0.0.4 at 2db169d
and its previously loaded Core code. Core's local linear edge-count optimization
is committed separately at 9e91ea9. Native retention optimization 78aaf93 is also
queued for accumulated deployment. No additional spec, redrive, rebuild, reset or
historical DLQ operation was performed. Twenty pending specs remain reserved.
The previously recorded nine static architecture findings remain open, not waived.

Private evidence:

- `.grafx-tmp/global-delivery-read-phases-20260908.json`, SHA-256
  `622433DCCF863D18A2E562B5884A52C04974F60609907C776A9D43E424FE2F4F`.
- `.grafx-tmp/global-edge-reads-20260908.prof` (instrumented query profile).
- `.grafx-tmp/global-bucket-batches-20260908.json`, SHA-256
  `3D518B6FEA365216D5E40D279C21726598ACE7C4333DF0EBC5C14CF3F3A31DDD`.
