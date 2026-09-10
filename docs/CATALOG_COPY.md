# Bounded copy into an existing catalog

[Documentation index](README.md) · [Catalog sessions](CATALOGS_AND_WORKSPACES.md) · [Protocol](specs/CATALOG_COPY_V1.md)

0.0.6 development introduces `okto_grafx.catalog_copy`. It copies explicit,
complete selected tables into **existing compatible target tables**, with all
copied nodes/relationships and one durable idempotency receipt in one native
transaction. This differs from [logical transfer](LOGICAL_TRANSFER.md), which
imports an artifact into a new destination using a private staging database.

## Example

```python
from okto_grafx import connect
from okto_grafx.catalog_copy import capture_copy, prepare_copy_target, copy_graph

with connect("./source") as source, connect("./target") as target:
    # Explicit setup, not a side effect of ordinary capture/copy.
    source.ensure_identity_indexes()
    source.enable_commit_history()
    # Make source data commits after enabling provenance.
    # Create compatible Person / Knows tables and vector spaces in target first.
    prepare_copy_target(target)

    with source.begin("read") as reader:
        package = capture_copy(reader, tables=("Person", "Knows"))
    receipt = copy_graph(package, target, idempotency_key="promotion-123")
    assert receipt.rows >= 0
    again = copy_graph(package, target, idempotency_key="promotion-123")
    assert again.replayed
    assert again.target_commit == receipt.target_commit
```

Keys accept 1–128 ASCII letters/digits plus `_ . : -`, starting with a letter,
digit or underscore. A key is scoped to the target store. Retain the same detached
`CopyPackage` for retries. Recapturing after source changes creates different
input and must not be silently replayed under an existing key. No file transport
or untrusted pickle decoder is provided by this API.

`CatalogSession.apply_copy(package, *, target="alias", idempotency_key=...)`
provides the same operation with session permissions, transaction counts and
attachment lifetime tracking. Prepare the database explicitly before attachment;
the target alias must grant writes. Detach/close refuses during its copy commit.
Source capture can use `session.begin("read", catalog="source")`; it remains one
independent source snapshot, not a distributed read/write transaction.

## Supported scope and refusals

- Explicit tuples of complete table names, with all selected relationship
  endpoints included in selected node tables. No arbitrary selection predicates
  or induced-subgraph API is exposed in this first slice.
- Node tables require primary keys; anonymous nodes are explicitly unsupported.
- Native typed/null properties and vector values are copied. Vector space IDs are
  remapped by compatible space names/definitions, including nested vector values;
  used target spaces must be active. Source physical IDs are never reused as
  target record IDs or physical cross-store endpoints.
- Target table names, kinds, columns, nullability, PK and endpoint declarations
  must match. The operation neither creates nor evolves application schemas or
  copies custom indexes. Existing target indexes participate through native writes.
- `conflict="fail"` is the only v1 policy. An existing primary key aborts the
  entire transaction; skip/merge and automatic retries are not silently selected.
- The `_grafx_` source namespace is not copied by this API. Receipt infrastructure
  is separate from copied application data.

## Atomicity and retry contract

Preparation explicitly enables existing identity/provenance capabilities and
creates the ordinary ledger `_grafx_copy_receipts_v1`. These setup stages can
remain completed if a later setup step fails; they do not copy application rows.
Preparation is idempotent after completion. An incompatible ledger refuses.

Each copy uses one native write transaction and parameterized `executemany` calls.
Those calls retain native uniqueness checks, read/write partition registration,
quotas, endpoint validation, dual OCC, WAL and publication fencing. There is no
new process-wide writer lock. A conflict or pre-durability exception rolls back
both copied data and the receipt, not just the latest batch.

The receipt binds the source-qualified commit, request checksum, destination UUID,
row count and target-qualified commit. Its heap birth sequence must have matching
native commit metadata. Exact primary-key lookup verifies the receipt without
scanning the entire history. Same key/different package, policy or caller metadata
refuses. Replaying an existing proven receipt rolls back its empty transaction;
it does not append a new commit or duplicate rows.

After an uncertain durable ACK or process crash, reopen/recover normally and retry
the **same** package/key/metadata. Do not allocate a different key to hide the
uncertain outcome. A request may receive an ordinary conflict while a concurrent
identical request commits; an explicit retry then reads the winner's receipt.

Receipts record historical completion, not a claim that later application updates
never changed the copied rows. Do not modify/delete the ledger or its provenance.
There is no receipt-expiration policy yet. Direct database access is not an
authorization sandbox; checksums and owner records are not signatures.

## Configuration and result contracts

All options are operation-local. Neither `ConnectOptions` nor environment defaults
gain new fields. Revalidate limits on both capture and apply.

| `CopyLimits` field | Default | Allowed range / meaning |
| --- | --- | --- |
| `max_rows` | 10,000 | 1–1,000,000; total selected rows |
| `max_bytes` | 32 MiB | 1–1 GiB; encoded package/schema bound |
| `max_row_bytes` | 4 MiB | 1–`max_bytes`; one encoded row |
| `max_tables` | 64 | 1–256; selected tables |

The package additionally allows at most 256 captured embedding-space definitions.
These are logical admission bounds, not a Python RSS guarantee or overrides of
native transaction/query budgets. A package fitting these bounds can still exceed
a stricter native transaction limit and refuse atomically. Capture reads pages of
at most 256 rows and checks total bounds; no partial package is returned on refusal.

`CopyPackage` is frozen: source `CommitId`, tuple of `CopyTable(schema, rows)`,
space definitions and SHA-256. Encoded rows include source record IDs. Apply
revalidates definitions, types, bounds, uniqueness of source identities, endpoint
closure and checksum before beginning the target transaction.

`CopyReceipt` is frozen: `source_commit`, `target_commit`, `request_sha256`,
`rows` and `replayed`. `metadata=CommitMetadata(...)` is optional. The reserved
attribute `grafx_copy_v1` carries request proof and cannot be supplied by callers.
Ordinary metadata admission limits still apply to the resulting metadata.

## Errors, recovery and transfer

`GrafxConfigurationError` identifies invalid packages, limits, keys, source/target
identity or incompatible schemas/spaces. `GrafxUnsupportedOperation` identifies
missing preparation/provenance or unsupported copy policy/node kind.
`GrafxLedgerError` refuses owner/schema/receipt/provenance mismatches. Session
permissions/lifecycle use `GrafxTransactionStateError`. Native conflicts, corruption,
budget exhaustion and uncertain outcomes propagate without being converted to success.

The helper adds no physical page/WAL type or required capability bit: it reuses
the existing commit-catalog capability and native application rows. Physical
backup/recovery preserves data and proof together. Logical import into a new UUID
does not transplant receipt authority: its UUID/commit proof will fail validation.
Do not treat a logically copied receipt ledger as a newly prepared target.
Existing older engines can decode ordinary rows if they support the required
native capabilities, but do not acquire this copy/retry API.
