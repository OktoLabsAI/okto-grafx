# Bounded relationship census with exact endpoint validation

## Scope

The native closed `MATCH (a:A)-[r:R]->(b:B) RETURN count(r)` / `count(*)`
pipeline now groups endpoint validation into frontiers of at most 64 pairs.
There is no filter, grouping, DISTINCT, owner write or entity/property consumer
in this eligible shape. The public query language/result schema is unchanged.
Other shapes keep their existing operators. Configured intermediate-row,
traversal and query-spill budgets keep the canonical admission path as well.
This is an optimization selection, not removal of any query capability.

`IndexManager.validated_identity_counts_many` verifies a record-id index's
distinct keys under one existing exact pre/post certificate. Each candidate
still passes `read_landing`, table identity, snapshot visibility and derived
key checks. Vector bodies are validated without building vectors. It returns
cardinalities, aligned to input keys; multiple visible witnesses are refused by
the query with the scalar duplicate-identity error. A changing certificate
retries the entire callback; exhausted retry or corruption returns no prefix.
This does not retain certificates/authority across operations.

The relationship heap is still scanned and validated. Both endpoints must be
visible; a missing source short-circuits its target. Only integer endpoint IDs
are buffered, not relationship/node payload collections. The aggregate returns
the same zero row for an empty relation and retains census statistics.

## Transaction-local memo and lifecycle

The first grouping-only candidate reduced certificate count but repeated
endpoint decodes across the census statements. The integrated form uses the
existing `_OwnerLandingView` transaction/snapshot/catalog/heap-epoch selection,
LRU budget, active-I/O lease and retirement lifecycle. A distinct `presence`
key stores only 0/1 at a conservative 512-byte tariff. Duplicate witnesses are
never retained as successful proofs. This key cannot answer either full rows
or vector-free scalar rows. No new engine-wide cache or unbounded table map is
introduced. Budget exhaustion declines retention or evicts results; it does
not change counts or remove physical validation on cache misses.

Native exact-type/method gates preserve specialized full-reader/index hooks.
Writes keep owner overlays on the old operator. No WAL, OCC, catalog format,
recovery, reader/writer exclusion, lease duration or durability setting changes.

## Real-board read-only comparison

One read-only connection, explicit generation revalidation, 64 MiB page pool,
69 distinct physical relationship layouts, separate read transactions per arm:

| Arm, execution order | Wall time (s) | Exact post-certificates |
| --- | ---: | ---: |
| Scalar | 2.348 | 2960 |
| Batch + bounded presence memo | 0.642 | 92 |
| Batch + bounded presence memo | 0.612 | 92 |
| Scalar | 1.462 | 2960 |

All answers counted 4424 relationships, fingerprint
`2ca4f6d86e7b2f9b57f6e567cf5c660f8ba7694aaf3c2b81034faefb34b6599e`.
The deterministic improvement is 2960 -> 92 certificates (~96.9% fewer).
The warm scalar vs batch pair is about 2.3-2.4x faster in this native sample;
the cold first scalar arm is not used to amplify the claim. This is not yet
an end-to-end UI speedup measurement. No timing threshold becomes a release gate.

The earlier grouping-only trial had 174 certificates but no consistent warm
time improvement (batch 3.416/3.492 s vs final scalar 3.162 s). That form is
superseded by the memo integration, not a second product mode or setting.

Tests cover count/star/aliases and statistics parity, 64-pair frontier bounds,
repeated/missing keys, retries/exhaustion, foreign-table candidates, duplicate
visible identities, corrupt omitted vectors, owner writes, independent-writer
snapshot isolation, presence/full-vector separation, memory exhaustion/eviction,
operational-budget fallback and unrelated query shapes. Existing vector-free
tests disable both optimizations when selecting their full-decode oracle;
retention assertions still require less memory and complete release.

Validation checkpoint: the combined count/vector-free/multi-key/ST-1/ST-6 slice
passed 107 tests in 55.80 s. Two additional boundary tests exposed a zero-count
statistics difference (`rows_scanned: 0` vs absent); the implementation now keeps
the canonical absent key. The final count-specific suite passed all 24 tests in
18.69 s. Counts overlap and are not a combined distinct total. Ruff and scoped
whitespace checks passed. No regression run took hours.

No reserved spec was consumed. Initial implementation and tests are in source;
live deployment evidence is recorded separately after validation.
