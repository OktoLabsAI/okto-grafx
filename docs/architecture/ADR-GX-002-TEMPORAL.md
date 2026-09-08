# ADR GX-002 — Logical temporal history, not retained MVCC

Status: accepted semantic contract; GX-CAP-3/4 implementation pending.
Source: complementary plan §7 in full; agent-first §§13, 20.

## Decision

System time uses store-local CommitId intervals; wall-clock observations are not
ordering authority. Intervals are half-open [from, to), with an explicit unbounded
upper endpoint. Timestamp lookup resolves the greatest committed identity whose
ordered_at is <= the requested instant. Valid time is application-supplied and
independent; bitemporal queries select both coordinates explicitly.

Tables opt in. History is durable product data published atomically with current
state, not accidental physical heap versions and not reconstruction from recycled
WAL. MVCC vacuum cannot remove it. No internal history structure is exposed as a
mutable user table. Physical representation is not selected in this milestone.

Update closes the preceding version and opens the next at the same commit.
Delete closes the current version; recreate of the same key creates a new lineage.
Explicit resurrection, if later supported, needs a separate policy. DETACH effects
on a node and its relationships share the same logical commit.

Historical traversal requires visible relationship and both visible endpoints
with matching logical lineage. Resolving the same primary key to a later recreated
node is forbidden. Diff distinguishes create/update/delete/recreate and reports
before/after, identity/table, endpoints, commit/time and allowed provenance.

Retention is separate maintenance: current state is never pruned, historical pins
protect their intervals, pruning is transactional/resumable, and the retained
horizon is exported. Reads below that horizon must refuse with a typed error, never
return a misleading empty result. Retention modes and all §7.9 rules remain required.

## Interfaces and recovery gate

Typed as-of, between, versions, valid-time, bitemporal and diff contexts precede any
Cypher grammar. The examples in the source plan are proposed APIs, not available
methods. Parameter errors (inverted intervals, mixed-store identities, invalid
coordinates) must be rejected before I/O; expired horizons and corrupt history
have distinct typed classifications.

GX-CAP-3 must specify bytes, catalog capability, upgrade and replay before writing
history. Crash tests cut before/after WAL durability, each current/history effect,
publication and ACK; repeated recovery must yield the same logical state with no
overlap/gap/orphan or duplicate version. Concurrent writers/readers, clock ties and
regressions, pinned pruning, cold reopen, independent node/edge corruption,
vacuum and export/import are required, not substitutes for one happy path.
Write amplification and query/history growth are reported without a new speed gate.
