# Catalog copy v1: bounded existing-target transaction protocol

Implementation contract for approved continuation item 3 (GX-CAP-2).

## Scope and authority

Capture an explicit set of complete tables in one source read snapshot, within
row/byte/table bounds. Relationship endpoints must belong to captured node tables.
Apply a detached immutable package to an explicitly prepared existing destination
with compatible schemas/spaces. Conflict policy v1 is `fail`; no implicit merge,
schema alteration, cross-store edge or distributed transaction exists.
Node tables must have primary keys; this first slice does not copy anonymous
nodes. Native typed properties, vectors and relationship properties remain supported.

Preparation explicitly enables existing identity indexes/commit history and creates
an ordinary application ledger `_grafx_copy_receipts_v1`. This is a composition
API like application migrations, not a new physical format: required commit
catalog v1 admission remains unchanged. The ledger has native columns `key STRING
PRIMARY KEY`, `digest STRING`, `source STRING`, `target STRING`, `rows INT64`.
Owner row `key=owner` binds format `grafx-copy-receipts-v1` to destination UUID.
Request rows use `key=request:<idempotency_key>`; keys are 1–128 ASCII identifiers.
The namespace is reserved by this API, not an authorization boundary against a
caller with unrestricted direct database access. Callers must not edit its rows.

## Package and receipt

The detached package stores source-qualified CommitId, native immutable table and
space definitions, `(record_id, encoded_values)` rows and a SHA-256 digest over a
versioned canonical, length-delimited representation. Bounds cover captured rows,
encoded payload/schema bytes and tables. Revalidate the entire package before any
target transaction; reject changed checksums, duplicate identities and missing
endpoints. A package is checksummed, not authenticated. Preserve the same package
for retries; recapture after source changes is different input.

Request SHA-256 binds package digest, target UUID, conflict policy and canonical
caller metadata. The native COMMIT metadata includes `grafx_copy_v1=request_hash`.
Receipt rows contain request hash, source CommitId token, target UUID and row count.
Their heap birth sequence supplies target CommitId, so no guessed/preallocated CSN
or second receipt-fixup transaction is required. Read the receipt through the native
exact primary-key index and validate that its birth commit exists with matching
metadata. No scan of all commit-history entries is permitted as receipt lookup.

## Atomicity, retries and recovery

All copied rows plus the receipt are staged in **one** native target transaction.
Node creation and endpoint remapping use ordinary parameterized `executemany`
queries with primary keys, not raw intent staging or source physical IDs.
Native quotas, unique keys, endpoint checks, dual OCC, publication
fencing, WAL-before-data and durable COMMIT continue to decide admission.

Before durability: rollback/crash leaves neither data nor receipt committed.
After durability but before ACK: native recovery replays both; retrying the same
package/key returns the prior receipt. Same key/different request refuses. An OCC
conflict is not success; reopen/retry explicitly. Do not catch uncertain durable
outcomes and start a different request. Different requests remain independent native
transactions; no process/global writer mutex is introduced by this helper.

## Compatibility and operations

Old engines supporting the existing required capabilities can decode the ordinary
ledger rows; they do not gain a copy API or permission to edit ledger semantics.
Unknown/mismatched ledger schema/owner or receipt evidence refuses. No automatic
downgrade or deletion of receipts is provided. Receipts must remain for as long as
their idempotency keys can be retried; this first slice has no retention sweeper.
Native physical backup/recovery retains the same data and receipt proof. Logical
transfer into a fresh UUID must not reuse old receipts as authority: the target
UUID and commit-catalog proof will refuse. Provision a new receipt namespace/store
through an explicit migration before treating that fresh database as a copy target.

Validation includes source mutation after capture; conflict rollback; key mismatch;
indexed repeat without writes; missing/mutated provenance; reopen/checkpoint;
before/after-COMMIT process death; concurrent identical requests and endpoint remap.
No wall-time performance ratio is an acceptance threshold.
