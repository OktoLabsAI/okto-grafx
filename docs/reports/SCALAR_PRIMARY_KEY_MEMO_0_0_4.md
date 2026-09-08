> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Scalar primary-key anchors — 0.0.4

## Selected workload and change

The pending Community `find_by_artifact` adjacency path issues one statement per
incident relationship layout/direction. A read-only profile of the isolated
full-board restore found **36 statements for 105 neighbours of one Entity**.
Each statement repeated the same central-node PK bucket lookup and full payload
decode. The existing vector-free landing proof was already selected; it was not
missing from the production-shaped query.

`_scalar_primary_key_group` now gives native scalar PK seeks the same bounded
decoded-answer memo already used by `NodeMultiKeySeek` and incident endpoint
batches. It only applies to an exact native **read transaction**. Every statement
still opens its own stable exact-index view and performs the existing index/heap
authority checks. This is **not CONCUR-2**, certificate caching across statements,
a new batch API, or removal of the layout query fan-out.

No Community/Core code or public configuration changes are needed to consume it.
Core remains backend-agnostic. Source checkout changes do not hot-reload an
already loaded engine. The subsequent controlled deployment on 2026-09-07 loaded
this optimization into Pulse 0.3.3 (PID 18604), with the module path and scalar
helper source SHA256 printed by the same server process at startup:
`276ba5d4a745f22835acb96d664ceecad02cd069bed1076eebd9498dbd29110e`.
This was a local source runtime, not a published 0.0.4 package.

## Equivalence and fallback

- The one-key batch walks the same candidate sequence, checks the same table,
  visibility and derived key, and keeps the complete native values. No projected
  vector sentinel is introduced by this change.
- Results are reused only for the same transaction/snapshot, exact store,
  registry revision, heap read epoch and index generation. A moved authority
  forces revalidation using the existing memo protocol.
- Scalar predicates, owner handling, row counters and statement result/error
  boundaries stay in the existing `IndexSeek` pipeline. Writer transactions keep
  the original scalar path, including read-your-own-writes and uniqueness checks.
- Native heap read/decode/slot and scalar index validation hooks must be canonical.
  Specialized implementations and unavailable multi-key capability retain the
  scalar door. Unknown transaction contexts are declined without invoking an
  additional `mode` callback.
- Hit and miss retention share the existing bounded owner budget and LRU.
  Failed admission only changes cost. Transaction settlement releases retention;
  no application-level, cross-transaction or cross-process value cache is added.
- A failed physical post-certificate is propagated even after a cache hit;
  there is no error-triggered fallback to cached success or a different query.

WAL, both OCC validations, durable publication, file format, reader/writer
participation and writer leases are unchanged.

## Evidence (2026-09-07)

Focused/integrated slice: **156 passed in 102.74 s**, covering the first scalar
memo tests, incident seeks, node IN seeks, vector-free traversals and multi-key
index validation. Final guards and scalar contract slice: **53 passed in 16.73 s**
(scalar memo, primary keys, seek predicate semantics and query budgets). These
counts overlap; they are not presented as 209 distinct cases. Ruff and
`git diff --check` passed. The entire six-minute query suite was not repeated for
this isolated reuse extension.

Tests include repeated hit/miss with a stable view per call, local heap/registry
changes, a foreign writer with an old reader snapshot, new-transaction visibility,
writer-owned key updates, bounded admission/cleanup, custom hooks, full-vector
and duplicate sequence equivalence, and refusal at the post-certificate and slot
boundaries after warming the cache.

The real-board A/B used only the already restored, unbound database at
`.grafx-tmp/real-board-transfer-v004-20260907-a/restored`. Six alternating rounds
used a new read transaction per adjacency call; the first round was excluded from
warm medians, not compared as a cold speedup. All twelve results had 105 rows and
the same ordered logical result SHA256:
`c9f3f0429e8811dfddd8919b9f6d724603a11ef331412918cc87eec74953a89c`.

| Measurement | Canonical scalar | Candidate |
|---|---:|---:|
| Central Entity PK bucket probes per adjacency call | 36 | 1 |
| Warm median, five observations | 150.12 ms | 131.10 ms |
| Owner budget bytes after settlement | 0 | 0 |

The observed warm reduction is about **12.7% for this adjacency call**, not an
end-to-end UI speedup, a write-throughput claim or a new acceptance threshold.
The stronger claim is the removed repeated anchor work with identical answers.
Local reproducers: `.grafx-tmp/profile_incident_layout.py` and
`.grafx-tmp/scalar_pk_anchor_ab.py`. No board content/backup is committed.

The 21 pending cognitive specs remain untouched; ledger SHA256:
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
The full layout fan-out and the explicit performance decision queue remain
pending; this extension removes a measured repeated operation within that path.
