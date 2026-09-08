# SPEC-GX-CAP-1 — Commit identity and provenance

Status: CAP-1A domain admission implemented and validated; persistent/public capability not certified.
CAP-1B record codec is also implemented; paged store/publication/recovery wiring is pending.
Date: 2026-09-08. Branch: feature/gx-cap-1, based on c310675.
Dependencies: M1 typed API; GX-CAP-0.

## Normative scope

[Complementary roadmap](../../GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md), §6; §17 GX-CAP-1.
[Agent-first roadmap](../../AGENT_FIRST_EVOLUTION_PLAN_CODEX.md) remains incorporated
in full under the [main plan](../../EVOLUTION_PLAN_CODEX.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: CommitId; bounded immutable CommitMetadata; commit lookup/catalog; replay-idempotent recovery; typed views; metrics and verification; coordinated logical export/import hooks.

## Contract and implementation boundary

Binding decision: [ADR-GX-003-COMMIT-IDENTITY.md](../architecture/ADR-GX-003-COMMIT-IDENTITY.md).
Planned owning modules: Domain commit DTO/codec; engine transaction/recovery/catalog orchestration; runtime clock/identity ports; public API/verify; logical transfer hooks.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
commit identity/type/range, metadata type/byte/key bounds, outcome ambiguity, missing identity, corrupt catalog, unsupported capability. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Prove multiprocess ordering, metadata atomicity, clock tie/regression handling, all new crash cuts, full regression and mutation tests. Source §6 requirements all remain mandatory.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.

## CAP-1A — Pure identity, metadata admission and logical time

This first implementation slice adds no exported public facade, begin parameter,
WAL record, format flag or durable effect. Domain modules are internal until the
complete capability is usable. Scope: `domain/txn/commit_identity.py` and
`domain/txn/commit_metadata.py`, with dedicated domain tests.

Current evidence: `CommitReport.csn` is the COMMIT record LSN; `_build_records`
predicts it and `_retarget_commit_batch` accounts for a segment-roll header before
append. `_commit_without_writing` returns the snapshot CSN without creating a new
commit. Therefore only a durable writing outcome can receive a newly published
CommitId. The eventual LSN mapping still requires persistent lookup, retention,
restore/fork and crash proofs; this slice does not certify it from a type alias.

CommitId is a value reference `(database_uuid: bytes[16], sequence: int)` with
`0 < sequence < PROVISIONAL_CSN`. Bool/subclass coercions are refused. Equality is
qualified by store; ordering different stores refuses instead of sorting UUIDs.
The canonical external token is 32 lowercase UUID hex digits, colon, 16 lowercase
sequence hex digits. Parsing validates exact spelling and reserved ranges.
Constructing an identity is not proof that the referenced commit exists.

Logical time uses existing signed-microsecond Timestamp values. Under the future
serialized publication boundary, `ordered_at=max(observed_at, previous+1us)`;
the first commit uses observed_at. Clock ties/regression set clock_adjusted. Signed
overflow refuses before publication; time is never sampled or used for leases by
this pure module. The persisted previous timestamp is still an integration task.

CommitMetadata admits optional actor/origin/correlation_id/reason strings and an
attributes tree of exact built-in dict/list/tuple/string/int64/finite-float/bool/None.
Lists become immutable tuples; dicts become private immutable mappings with
canonical UTF-8 key order. Unsupported objects are refused without invoking their
conversion/repr hooks. All input data are copied/normalized before later use.
Metadata equality/hash use canonical admission bytes, so bool/int and signed zero
are not silently conflated. Constructor and repr/errors do not disclose payloads.
Python hash remains process-local; it is not a persisted idempotency fingerprint.
Caller mutation after admission cannot change the value; callers must not mutate
input trees concurrently with their admission if they require one coherent source
snapshot. Limits are copied and revalidated on entry, not trusted by object type.

MetadataLimits are immutable and caller-configurable within hard admission bounds:

| Limit | Default | Allowed range |
|---|---:|---:|
| max_bytes (complete canonical admission encoding) | 16,384 | 14..65,536 |
| max_attributes (entries in any map) | 64 | 0..256 |
| max_key_bytes (UTF-8) | 128 | 1..1,024 |
| max_string_bytes (field or value, UTF-8) | 4,096 | 1..16,384 |
| max_depth (container depth; attributes root map is zero) | 4 | 0..16 |
| max_values (attributes containers/scalars, including root; not keys) | 256 | 1..4,096 |

The internal canonical admission representation is tagged and length-delimited,
with a five-byte discriminator, four optional fields, then the attributes map.
It permits deterministic byte-budget accounting without calling an external
serializer. At CAP-1A this was not a frozen on-disk format or WAL extension. The
subsequent CAP-1B [record/recovery contract](../architecture/COMMIT_CATALOG_V1.md)
selects it as the nested metadata v1 body and adds a strict bounded decoder. No
paged store, WAL grammar or activation is enabled before complete replay support.

Invalid types/encoding/shape/identity/time/limits use GrafxConfigurationError;
exhaustion of metadata admission budgets uses
GrafxTransactionBudgetExceeded with bounded field/limit/observed diagnostics.
Neither errors nor repr includes user-supplied key/content text. Identity mismatch
is not a write conflict and is not automatically retryable. Resource admission
performs no I/O and is independent of a transaction object.
Shallow type/size guards precede allocation and deeper validation: exhausting a
budget does not certify that the unvisited suffix would otherwise be valid.

Required slice tests: limits and exact boundary bytes, deep/nested/cyclic input,
caller mutation, unsupported subclasses/hooks, ordering/canonical hashes,
Unicode/surrogates, int64/float edge cases, cross-store comparison, token parsing,
clock tie/regression/overflow, no new public export, and the existing pure/import
gates. The remaining CAP-1 gate still requires all persisted integration, crash,
multiprocess, lookup/verify, metrics and logical-transfer work listed above.

### CAP-1A execution evidence — 2026-09-08

Immutable implementation checkpoint: `474335962eeae20a937d6a444bcc3f7b2c2f110f`.

The tests were written first and the initial run refused collection because the
new identity module did not exist. After implementation, the dedicated slice is
**87 passed in 0.23 s**. The all-transaction/public-output/import-boundary group
is **985 passed in 114.31 s**, using:

```text
python -m pytest tests/txn tests/test_import_boundary.py
  tests/test_optional_package_boundary.py tests/api/test_public_operation_outputs.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

Strict mypy (`--strict --follow-imports=silent`) passes both new domain modules;
Ruff passes both modules and their tests; diff-check is clean. Earlier 312-test
admission/import results overlap this final group and are not an additional total.

Adversarial follow-ups before the grouped run: reject oversized keys before sorting;
prove a too-large sequence does not reach the copying hook with a working small-
input sentinel; revalidate a host-mutated limits object; reject inconsistent time
coordinates; check signed-zero/type distinctions and exact length-delimited bytes;
refuse surrogates, deep/cyclic containers and hostile conversion/iteration hooks.
These tests certify pure admission only. Existing transaction/crash tests remain
green, but no new metadata publication crash matrix has been exercised because
this slice has no persistent effects. No Pulse operation, extra spec consolidation,
deployment, benchmark speedup claim, main merge or release occurred.

### Next required vertical slice (CAP-1B), not optional debt

Specify and implement the durable commit catalog and activation/recovery protocol,
then connect admission before begin/publication. Prove the current COMMIT-LSN mapping
across roll, gap completion, checkpoint/recycle, old stores and restore/fork; no-write
transactions must not fabricate new identities. Define how internal maintenance
commits are represented and how the legacy-history boundary is exposed. Metadata
objects remain caller-owned values, not physical authority or proof of admission
at a later public boundary. Publication must validate its own immutable captured
representation, not trust a Python class name or a mutable host field.

Only after the persistent catalog/replay/lookup is usable may the facade export
CommitId/CommitMetadata and accept metadata at begin. CAP-1C still owes metrics,
verify, logical transfer and the complete concurrent/crash/release conformance.
The full GX-CAP-1 remains **in progress**, not complete.

### CAP-1B record codec checkpoint — 2026-09-08

Implemented typed CommitCatalogEntry/CommitKind, checksummed record encoding,
bounded nested metadata decoding, expected store/sequence validation and explicit
unknown-format refusal. The format contract and required crash matrix are in
[COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md). They distinguish a
verified record value from evidence of durable publication.

Final command:

```text
python -m pytest tests/txn/test_commit_catalog_record.py
  tests/txn/test_commit_provenance_values.py tests/test_import_boundary.py
  tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**379 passed in 7.82 s**, including 3,000 bounded mutated inputs and the maximum
65,596-byte record. Strict mypy passes identity/metadata/catalog modules; Ruff/diff
pass. This extends CAP-1A's admission contract; it is not a rerun or replacement of
that checkpoint's 985-test transaction group. Paged directory/record storage,
required-capability/WAL discrimination, commit/recovery integration, lookup/verify,
metrics and transfer are still required. No public export, live mutation or deploy.
