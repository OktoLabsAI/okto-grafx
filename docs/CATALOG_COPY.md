# Bounded copy into an existing catalog

Development typed collection columns participate in copy fingerprints with their
complete descriptor. Matching only LIST/MAP family tags is insufficient: nested
nullability, ARRAY length, STRUCT fields and decimal p/s must match, including
empty packages. Invalid schema/rows refuse before native target effects. Copy
keeps its existing provenance prerequisite, idempotency and transaction boundaries.
[Typed collection contracts](specs/TYPED_COLLECTIONS_V1.md).

[Documentation index](README.md) · [Catalog sessions](CATALOGS_AND_WORKSPACES.md) · [Protocol](specs/CATALOG_COPY_V1.md)

0.0.6 development introduces `okto_grafx.catalog_copy`. It copies explicit,
complete selected tables into **existing compatible target tables**, with all
copied nodes/relationships and one durable idempotency receipt in one native
transaction. This differs from [logical transfer](LOGICAL_TRANSFER.md), which
imports an artifact into a new destination using a private staging database.

All six [native temporal property types](TEMPORAL_VALUES.md) preserve exact values
through capture/copy, endpoint remapping, receipt replay and reopening, including
nested ANY/flexible maps and recorded zones not available from a rule provider.
This does not copy system-time history. Typed target columns must already have
compatible temporal types, and the source data commit must have tracked provenance.

### Native DECIMAL declarations

`DECIMAL(p,s)` and nested `DecimalValue` properties retain coefficient, precision
and scale through capture, endpoint remapping, atomic copy, receipt retry and
reopen. No conversion to DOUBLE or text occurs. Target typed columns must match
**both precision and scale**, even for an empty source table or values that could
be rescaled exactly. Copy is not an assignment/schema migration API.

The package digest binds decimal column parameters as well as native row frames.
Replacing a declaration changes that digest; recomputing a checksum does not
authorize a row frame whose decimal metadata differs from its column. Invalid
packages fail before the target transaction; incompatible target schemas fail
without a commit or receipt. Existing `CopyLimits`, preparation, source commit
provenance and explicit `history="current-only"` rules remain mandatory. There
is no new setting, cross-store transaction or automatic retry.
See the [decimal consumer qualification](reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md).

## Example

Automatic vector indexes belong to the target's physical table IDs and naming
policy. Copy preserves logical vector values/space contracts, not source index
filenames or `TableDef.vector_identity_names`. Compatible targets may have the
opposite creation order and naming policy for homonymous node/relationship tables.
Both codecs are covered by the [durable owner transport tests](specs/VECTOR_OWNER_NAMES_V1.md).

### Explicit physical kind selection

`tables` accepts unique physical names or `(kind, name)` tuples, with kind
`"node"` / `"rel"`. For same-spelled physical tables use, for example:

```python
with source.begin("read") as reader:
    package = capture_copy(reader, tables=(("node", "R"), ("rel", "R")))
receipt = copy_graph(package, target, idempotency_key="qualified-r")
```

Target schemas and the copy receipt ledger must already be prepared. For subset
capture, `record_ids` uses exactly the selectors supplied in `tables`, e.g.
`record_ids={("rel", "R"): (edge_id,)}`. With `include_endpoints=True`, node
endpoints are added independently of a same-named relationship. Duplicate resolved
table IDs refuse, even when selected once by string and once by tuple. Bare
ambiguous names refuse with `ambiguous_table_name`; no first-match choice occurs.

Copy packages containing overlaps without label metadata use the `grafx-copy-package-v3` digest domain.
The schema/kind/ID, logical groups, endpoint closure and all row bytes remain
bound to the package and idempotent request. Same-kind duplicates still refuse.
The existing atomic data-plus-receipt commit and all package/transaction bounds
remain unchanged. Packages without label metadata retain their previous domains.

### Native node-label membership

Capture preserves logical labels from each node version, including explicitly
empty sets and labels unrelated to the physical table name. Both full scan and
identity-index subset capture read native metadata under the source snapshot.
Copy preserves one target identity per admitted source node and remaps edge
endpoints to those identities; it never creates one node per label. Typed table
constraints continue to belong to the physical owner. Complete frame bytes count
toward `CopyLimits.max_row_bytes` and `max_bytes` before target work.

`CopyTable.rows` remains a tuple of `(source_record_id, bytes)` pairs. The bytes
now may contain a canonical `GXL1` membership prefix before the native property
tuple. No prefix means implicit source membership; explicit empty is different
from a named physical table's implicit singleton. These are opaque copy frames:
consumers must not assume that every row can be decoded as a bare property tuple.
Packages containing explicit membership or nonempty schema candidates use digest
domain `grafx-copy-package-v4`, binding all candidate trailers and full row bytes.
Old copy implementations do not understand that digest/frame; they must refuse,
not discard metadata. The indexed receipt protocol and ledger schema remain v1.

The target must already contain compatible tables/columns/endpoints. Ordinary
native label operators admit needed candidate names for **inserted** nodes in the
same transaction as data and receipt; this does not create a table, widen columns,
copy indexes, or copy unused source candidate summaries into the target. Target
candidate sets may contain extra names. Changing labels leaves physical schema
and PK ownership intact, and late failure rolls back candidate admission as well
as rows and receipt. Copy is logical, not a physical layout backup: semantically
equivalent implicit and explicitly encoded singleton/unlabeled-empty states may
use the target's implicit representation.

`conflict="skip"` resolves the PK in the **target physical node table**, even if
its base label was removed; a same-PK node in another owner sharing that label is
not a conflict. A skipped node keeps **both its properties and all its labels**;
copy does not union or replace them. Edges reference that existing identity. Newly
inserted nodes preserve the source logical set, including empty membership in a
named table. The native copy path also handles an older implicit-only package
when the target has activated versioned labels.

Label-aware native copy shares application-row `max_statement_writes` and
`max_intermediate_rows`, in addition to copy/transaction quotas. It uses the normal
native creation, label mutation, exact-PK reads and owner overlays; source IDs or
detached entities do not confer write authority. No cross-store atomicity or
automatic retry is introduced. Source retained history still requires explicit
`history="current-only"`; target history, if enabled, records these labels at the
new target commit rather than importing source intervals. See the
[implementation and evidence](specs/NODE_LABELS_V1.md#existing-target-copy-integration).

### Basic unique-name workflow

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

- Explicit tables, optionally narrowed by `record_ids={"Person": (1, 2),
  "Knows": (5,)}` on `capture_copy`. Every selected table must be present in the
  mapping; an empty tuple selects no rows. Missing IDs, duplicate/bool IDs and
  missing selected endpoints refuse. Node identity indexes are used when present;
  remaining table selections use a bounded scan charged against the same row/byte
  budgets. Default behavior has no endpoint expansion; the explicit option below
  adds bounded one-hop closure. No arbitrary predicate or unbounded scan.
- Typed nodes with or without primary keys and native flexible/unlabeled nodes
  are supported. Nodes without a PK are copied as distinct entities, not matched
  by equal properties. See the identity-remapping contract below.
- Native typed/null properties and vector values are copied. Vector space IDs are
  remapped by compatible space names/definitions, including nested vector values;
  used target spaces must be active. Source physical IDs are never reused as
  target record IDs or physical cross-store endpoints.
- Target table names, kinds, columns, nullability, PK and endpoint declarations
  must match, except grouped physical relationship names/IDs are remapped as
  described below. The operation does not create application tables, evolve their
  columns or copy custom indexes. Native candidate-label admission is the explicit
  exception described above. Existing target indexes participate through native writes.
- `conflict="fail"` (default) aborts the entire transaction on an existing PK.
  Explicit `conflict="skip"` preserves existing target **nodes** by PK and maps
  copied relationship endpoints to them. Nodes without a PK are always inserted,
  including equal property bags. It does not merge/update existing properties.
  Relationships have no deduplication identity here: selected relationships are
  appended, and same-key receipt replay prevents duplicate effects for that request.
  A different request key is a different copy, not implicit relationship deduplication.
  Neither mode retries automatically or bypasses native OCC/uniqueness checks.
- A source with selected native temporal tables requires explicit
  `history="current-only"` on capture. Only current rows/schema are copied; no
  temporal events, pins or history UUID authority transfer to the target.
- The `_grafx_` source namespace is not copied by this API. Receipt infrastructure
  is separate from copied application data.

### Typed logical relationship groups

Bounded copy preserves declared logical relationship groups over PK-bearing node
tables. Capture still selects **physical** member names (obtain them from
`db.catalog.catalog.relationship_tables("R")`); it may select one member or an
empty member without copying the whole group. Each `CopyTable` carries
`logical_type: str | None = None`: the native logical group name for grouped
relationships, `None` for nodes and ordinary ungrouped relationships.

The target resolves a grouped relationship by its logical name and exact declared
endpoint-table pair. Its physical name and table ID may differ from the source.
Endpoint nodes remain matched by table name and PK; values, nullability and model
flags must match. Additional unselected target members are allowed; missing pairs,
different group names, grouped/ungrouped substitutions and incompatible properties
refuse before application rows are written. Selected source members cannot repeat
an endpoint pair, disagree on property schema or collide with selected table names.
The package checksum detects mutation but is not source authentication.

Only packages containing group metadata use hash domain `grafx-copy-package-v2`;
each non-NULL logical name is a length-delimited, byte-budgeted component. Ordinary
typed/ungrouped package hashes remain unchanged. This does not change the native
receipt ledger or introduce a new storage capability: existing relationship-type
catalog admission and native data/receipt COMMIT/recovery remain authoritative.
`conflict="skip"` still skips only PK-existing nodes, never relationship rows.
Receipt replay adds no new effects, including after a lost commit acknowledgement.

No new configuration parameter is introduced. Existing `CopyLimits`, query and
transaction budgets apply. Flexible/no-PK support extends this grouped contract
with the identity rules below.

### Flexible and no-PK entity identity

Capture preserves each source-qualified `(table, record_id)` as a separate row,
even when several nodes contain identical values. The native target execution
creates new node bindings and remaps every copied edge to those target bindings.
The source record numbers are **only map keys**: they never become target IDs,
pending-reference tokens or cross-store endpoints. Self-edges, parallel edges and
edges joining keyed, no-PK and flexible nodes retain their actual topology.

Unlabeled nodes resolve the target's single native unlabeled store by its catalog
flag, not by an internal table name. Labeled node tables resolve by declared name.
Grouped edge members resolve by logical type and the **remapped target** endpoint
table pair. Schema kinds, flags, column types/nullability and PK declarations must
match after name remapping. Target schemas must already exist; missing stores or
pairs refuse without implicitly creating schemas or widening typed constraints.

`conflict="skip"` applies only to declared PKs. Without a PK there is no implicit
entity deduplication, even under `skip`. Repeating the exact package/key replays
its durable receipt without creating more nodes; a new key requests a new copy.
An existing PK node keeps its existing properties and supplies its real target
identity to copied edges. The returned `skipped_rows` counts skipped PK nodes only.

Expanded packages (groups, flexible schemas or no-PK nodes) use the v2 hash domain.
Flexible/unlabeled flags are included in the hashed schema shape. Malformed bags,
top-level NULL property entries, nested nonfinite stored values, duplicate source
identities and multiple unlabeled source stores refuse even if a caller recomputes
the checksum. The checksum still does not authenticate a source.

Flexible values pass through native storage admission. Keys are parameter-map
data, not interpolated query syntax; native heterogeneous types, NULL/missing
rules and nested maps/lists remain intact. This route uses native creation and
read operators inside one private rollback boundary, not unchecked raw row
staging or a detached entity converted into a writable handle. In this flexible/no-PK
execution path, all application rows share `max_statement_writes` and `max_intermediate_rows` in addition to
`CopyLimits` and transaction quotas. No phase commits independently or resets
the write quota when pending endpoints acquire native transaction references.
The receipt is a subsequent native statement in that same transaction; transaction
row/byte quotas cover both application data and receipt.

When source history is tracked, capture requires `history="current-only"`. If
the target already tracks these tables, its native history records the copied
rows at the **new target commit**; source intervals/pins are not transplanted.
Commit fences, independent reader snapshots, OCC, WAL/recovery and atomic
data-plus-receipt publication are unchanged. See executable cases in
`tests/api/test_flexible_catalog_copy.py`.

Here is a complete disposable example. In production, prepare compatible target
schemas explicitly; **do not empty an existing application's target** as this
in-memory demonstration does:

```python
from okto_grafx import connect
from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target

with connect(":memory:") as source, connect(":memory:") as target:
    for db in (source, target):
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin("write") as writer:
            writer.execute("CREATE(a {v:'same'})-[:R {weight:2}]->(b {v:'same'})")
    # Demonstration only: retain the target schema and clear its fixture rows.
    with target.begin("write") as writer:
        writer.execute("MATCH(n) DETACH DELETE n")
    prepare_copy_target(target)
    names = tuple(table.name for table in source.catalog.catalog.tables())
    with source.begin("read") as reader:
        package = capture_copy(reader, tables=names)
    receipt = copy_graph(package, target, idempotency_key="flex-example")
    assert receipt.rows == 3
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((2,),)
    assert target.execute("MATCH(a)-[:R]->(b) RETURN a=b").rows == ((False,),)
    assert copy_graph(package, target, idempotency_key="flex-example").replayed
```

## Atomicity and retry contract

### Automatic endpoint closure

`capture_copy(reader, tables=("Knows",), record_ids={"Knows": (5,)},
include_endpoints=True)` includes the directly referenced endpoint nodes and
their declared table schemas. The input mapping still covers every explicitly
selected table. Missing endpoints refuse; no arbitrary recursion is performed.
Caller-supplied selections are not mutated. Additional endpoint schemas count
against `CopyLimits.max_tables`; selected and scanned rows/bytes share the same
aggregate limits. Node identity indexes are used where available.

Closure reads the same owning source snapshot, including when a concurrent writer
changes an endpoint. The resulting package is identical to the equivalent fully
closed manual selection and uses the same target COMMIT/idempotency receipt.
`include_endpoints` must be exactly boolean and requires explicit `record_ids`.
Temporal endpoint tables also require `history="current-only"`; that policy does
not transfer their history. No new persistent format or connection setting is added.

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
| `max_rows` | 10,000 | 1–1,000,000; captured rows, including rows inspected by a bounded selection scan |
| `max_bytes` | 32 MiB | 1–1 GiB; encoded package/schema bound |
| `max_row_bytes` | 4 MiB | 1–`max_bytes`; one encoded row |
| `max_tables` | 64 | 1–256; selected tables |

The package additionally allows at most 256 captured embedding-space definitions.
These are logical admission bounds, not a Python RSS guarantee or overrides of
native transaction/query budgets. A package fitting these bounds can still exceed
a stricter native transaction limit and refuse atomically. Capture reads pages of
at most 256 rows and checks total bounds; no partial package is returned on refusal.

`CopyPackage` is frozen: source `CommitId`, tuple of `CopyTable(schema, rows, logical_type=None)`,
space definitions and SHA-256. Encoded rows include source record IDs. Apply
revalidates definitions, types, bounds, uniqueness of source identities, endpoint
closure and checksum before beginning the target transaction.

`CopyReceipt` is frozen: `source_commit`, `target_commit`, `request_sha256`,
`rows`, `replayed` and `skipped_rows` (default 0). `rows` counts inserted application
rows, excluding the receipt; skipped nodes are reported separately. Both counts
are reconstructed from the durable receipt and bound package on replay. The
conflict policy participates in the request digest, so switching it under an
existing key refuses. `metadata=CommitMetadata(...)` is optional. The reserved
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
