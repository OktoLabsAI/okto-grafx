# FP-6 typed persisted collections and structures

[Functional parity plan](FUNCTIONAL_PARITY_PLAN.md#fp-6--decimal-and-persisted-nested-values)
· [Roadmap](../../ROADMAP.md#functional-parity-expansion-plan)

Status: **native column/DDL/catalog integration implemented and locally qualified**
in 0.0.6 development. [Evidence](../reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md).
Exact JSON/text/SQLite and [columnar collection consumers](../COLLECTION_COLUMNAR.md)
are also implemented with [local qualification](../reports/FP6_TYPED_COLLECTION_COLUMNAR_QUALIFICATION.md).
`StoredType` is exported from `okto_grafx` and
`ColumnDef.stored_type` carries a complete, owned descriptor. Typed columns require
catalog v2 and `typed_collections_v1`. The [support matrix](../TYPE_SUPPORT.md) and
[60-case installed-reader checkpoint C](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
are complete for the recorded type-package candidate. Final FP-8/Pulse acceptance remains open. This
is not a released-version claim. Existing untyped LIST/MAP and heterogeneous ANY
storage retain their existing contracts.

## Required final result

Deliver the existing FP-6 scope: typed LIST elements, string-keyed MAP values,
fixed-size ARRAY elements and named STRUCT fields; nested nullability, schema
introspection, updates, value ownership, equality, serialization, durability and
supported interchange. ARRAY uses the existing LIST value family and STRUCT uses
MAP; the persistent schema must distinguish their stronger declarations. Do not
invent a second graph or a string encoding of native collections.

Expression operations continue to operate on native lists/maps. Assignment back
to a declared column validates the complete resulting value. Type metadata belongs
to the schema, not to every LIST/MAP row frame. Exact decimals and temporals nested
inside containers retain their native scalar tags/coordinates. A dynamic ANY value
must not bypass the existing prohibition on nonfinite stored values or embeddings
without declared vector-space authority.

## Public descriptor and DDL contract

`okto_grafx.StoredType` is an immutable, recursively validated model value with
these fields. DDL accepts `LIST<T>`, `MAP<T>`, `ARRAY<T,n>` and
`STRUCT<field:T,...>` on node and relationship property columns. Every level may
append `NOT NULL`; absence means nullable. Empty `STRUCT<>` and zero-length arrays
are valid. MAP keys are strings; its parameter describes values, not keys.

| Field | Contract |
| --- | --- |
| `kind` | Exact uppercase native scalar name, DECIMAL, ANY, LIST, MAP, ARRAY or STRUCT. The descriptor spelling is BYTES; DDL uses the existing BLOB alias. |
| `nullable=True` | Exact bool applying to this value, including nested elements/fields. NULL is nullability, not an independent typed collection leaf declaration. |
| `element=None` | Required StoredType for LIST/MAP/ARRAY; forbidden for other kinds. |
| `length=None` | ARRAY only: exact integer 0..2^32-1, matching the existing list-count encoding. Actual values remain subject to native resource/row limits. Zero means exactly the empty list. |
| `fields=()` | STRUCT only: ordered tuple of `(name, StoredType)`, up to 256 unique ASCII identifier names of 1..128 characters. Empty STRUCT is valid. Other types cannot carry fields. |
| `precision=None`, `scale=None` | Required for DECIMAL only; native p=1..38, s=0..p rules. |

Arbitrary host classes and descriptor subclasses are not admitted. Recursive type
depth reuses `MAX_VALUE_DEPTH=64`; maximum expanded descriptor nodes is 4,096.
Repeated shared Python subtrees count once **per occurrence**, and cycles refuse.
The compact encoded descriptor is at most 65,536 bytes. All encode/admission uses
must defensively revalidate frozen objects; a forged Python slot is not authority.
These are descriptor-format bounds, not new connection settings or graph-size limits.

`StoredType.value_type` maps ARRAY to LIST and STRUCT to MAP for query typing;
ANY returns None (dynamic family). `describe()` emits canonical declaration text
including nested nullability and decimal p/s. Nested vectors are not accepted: the
existing independently declared space/type/ownership requirements cannot be
bypassed by putting a VectorValue inside ANY or a container.

`ColumnDef(name, descriptor.value_type, nullable=descriptor.nullable,
stored_type=descriptor)` accepts collection-root descriptors only and clones the
complete tree. A mismatching column family/nullability refuses. Scalar DECIMAL
columns retain `decimal_precision`/`decimal_scale`; this argument does not replace
that contract. Existing positional arguments are unchanged.

```python
from okto_grafx import connect

with connect(":memory:") as db:
    db.ensure_identity_indexes()  # coordinated catalog-v2 prerequisite
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE Sample(id INT64, "
                   "values LIST<INT64 NOT NULL>, "
                   "info STRUCT<price:DECIMAL(12,4),tags:ARRAY<STRING,2>>, "
                   "PRIMARY KEY(id))")
        tx.execute("CREATE(:Sample {id:1,values:[1,2],"
                   "info:{price:decimal('1.25',3,2),tags:['a',null]}})")
    row = db.execute("MATCH(n:Sample) RETURN n.values,n.info").rows[0]
    assert row[0] == (1, 2)
    assert row[1]["price"].coefficient == 12500
    descriptor = db.catalog.catalog.table("Sample").columns[2].stored_type
    assert descriptor.describe() == "STRUCT<price:DECIMAL(12,4),tags:ARRAY<STRING,2>>"
```

DDL uses the existing caller transaction. Public plans are detached from engine
authority and other results; one result may memoize its own materialized plan.
Catalog observations are immutable, detached snapshots that may be reused within
a generation. Bypassing frozen Python objects is unsupported and cannot modify
native schema authority; observations are not editable configuration.

## Assignment versus stored-value validation

`normalize_typed_value(descriptor, value)` returns an owned native value:

- LIST/ARRAY accept native list/tuple input and produce owned tuples; ARRAY length
  must match exactly. Each nested element validates independently.
- MAP accepts dict/read-only map input, requires exact string keys and validates
  every value. Keys and strings must be valid native UTF-8 values.
- STRUCT rejects unknown fields. A missing nullable field becomes explicit NULL;
  a missing/non-nullable field refuses. Result fields are emitted in declared order.
- DECIMAL assignment rescaling is exact only; wrong source kind, overflow and
  inexact scale reduction refuse. Scalar conversion is not inferred from strings,
  integers, floats or host Decimal objects.
- ANY retains each supported native family recursively and owns every occurrence.
  Values containing nonfinite numbers or embeddings refuse, including under maps.
- The original input is never mutated; shared input containers/native scalar objects
  are detached independently. Depth/cycles produce native typed errors, not an
  uncontrolled recursion failure.

`validate_typed_value(descriptor, value)` checks an already-stored value without
normalizing it: a missing STRUCT field or different native decimal p/s refuses.
It must not silently repair bytes on read. Both functions return typed schema
errors on a mismatch. They do not grant schema authority or commit. Native
transactions normalize before intent/quota/encoding proofs, under existing capture,
work/byte and row limits. Stored tuple decoding validates the whole typed value
even when its column is projected away. Malformed persisted content raises
`GrafxCorruptionDetected`, not an implicit rescale or field repair.

## Catalog publication and descriptor bytes

`encode_stored_type` emits magic/version `GXT1`, then a recursive tree. Every node
has a one-byte kind and exact one-byte 0/1 nullability. Scalar kinds reuse native
ValueType identifiers; descriptor-only ANY=255, ARRAY=254 and STRUCT=253 are **not
new row value tags**. DECIMAL adds p/s bytes; ARRAY adds a little-endian u32 length;
LIST/MAP/ARRAY add their element node. STRUCT adds a u16 field count and, for each
field, a u8 ASCII name length, name bytes and child node.

`decode_stored_type` consumes exactly one bounded frame, rejecting unknown kinds,
bad flags, invalid/duplicate names, malformed parameters, truncation, trailing
data and depth/count/size violations with GrafxCorruptionDetected. It returns the
same canonical declaration without host coercion.

A typed column uses catalog type marker **252**, then existing nullable and
vector-space fields, then a little-endian u32 descriptor length and its GXT1 frame.
Length must be 6..65,536 before decoding. Catalog-v2 capability bit **27**
(`typed_collections_v1`) is mandatory. Declared nested ANY, DECIMAL and temporal
families also require their own capabilities, even on empty tables. Missing known
bits on stored descriptors are corruption; unknown required bits make older
readers refuse before interpreting table bodies. Capability admission is atomic
with schema/data COMMIT and uses the existing WAL protocol. There is no new row
value tag, isolation mode or connection option.

Collection columns cannot be primary keys or custom hash/ordered/fulltext index
keys. Definitions refuse before catalog effects; arbitrary element indexes are
not promised by FP-6. Other scalar columns keep their supported indexes. Native
record-id/endpoint identity indexes remain. Query list/map operations and equality
use native semantics; ARRAY/STRUCT add assignment constraints, not runtime tags.

## Native consumers and format boundaries

- Nullable column append preserves the descriptor and old-row implicit NULL
  semantics. Root NOT NULL is not a nullable append operation.
- Retained system-time schemas carry descriptors and native tuple/map values.
  Historical queries, updates, delete/recreate and physical backup/restore retain
  existing API and retention boundaries.
- Logical transfer preserves `stored_type` through exact JSON (`kind`, `nullable`
  and only the kind-specific `element`, `length`, `fields`, `precision`, `scale`
  keys). Existing artifact formats 1/2/3 remain. Older readers without this column
  keyword refuse instead of importing generic collections; the
  [installed logical-importer proof](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md#logical-artifacts-and-the-resumable-error-correction)
  records exact old error behavior and no destination/workspace effects. No lossy
  descriptor omission is permitted.
- Copy digests/target-schema matching include the entire descriptor, even on empty
  data. Matching the LIST/MAP tag alone is insufficient. Checksums do not authorize
  repairing already-stored values, and invalid imports refuse before destination
  effects.
- CLI `schema --json` adds `stored_type` only for typed columns; query values retain
  existing nested JSON and explicit decimal/temporal tags.
- Native query parameters/results use tuple/map values and exact native leaves,
  not a descriptor with every value. Procedure LIST/MAP/ANY signatures describe
  native families, not parameterized schema descriptors.
- [Exact collection JSON](../COLLECTION_JSON.md) now supplies explicit native
  leaf/ANY tags and CSV/JSONL/SQLite StoredType declarations, with owned descriptors,
  exact transport validation and whole-call rollback. Reader field/work bounds
  apply without new storage capabilities.
- [Exact columnar collections](../COLLECTION_COLUMNAR.md) now connect StoredType
  to Arrow/Pandas/Polars/Parquet with complete metadata, bounded native conversion
  and atomic imports. MAP representation changes, empty structures, NULL arrays
  and deep Polars schemas are handled without losing native semantics. This does
  not imply arbitrary external schema inference or nested entity ingestion.

## Integration still required (original FP-6 scope)

The [consolidated support/refusal matrix](../TYPE_SUPPORT.md), exact consumers and
[installed old/new-reader checkpoint](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
with the combined temporal/DECIMAL migration rules are now qualified locally.
Run the final full-profile/Pulse qualification required by the parent plan, including
procedure/parameter/result consumption through the installed integration. Keep final
API/configuration/usage evidence aligned with that exact candidate. No required
consumer is waived by declaring a partial receipt complete.

## Historical foundation verification

The subsequent native grouped regression passes **3,048 tests** and the public/
annotation/import/executable-documentation selection passes **2,447 tests**, both
with zero failures/errors/skips. [Commands, intermediate failures and hashes](../reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md).
These receipts qualify the native increment, not the remaining work above.

`tests/storage_core/test_stored_types.py` covers recursive descriptor codec,
declaration refusals, scalar families inside all four containers, exact decimal
assignment, stored read refusal instead of repair, nullability/default fields,
owned input aliases, vectors/nonfinite rejection, descriptor/value cycles, expanded
node/depth/byte limits, every truncation of a composite descriptor, invalid flags,
malformed coordinates/duplicate field names and 300 deterministic mutated frames.
Mutation iterations are cases inside a test, not extra pytest counts. No catalog
or durable-column completion is claimed by this model-only verification.

Initial functional receipt: `.grafx-tmp/fp6-typed-collections-foundation-first.xml`,
**86 passed**, zero failures/errors/skips, 0.372 seconds, terminal exit 0;
SHA-256 `94a56cdf4390d50974d3042ac7a1a09a0b1806c85f4ec4c6a0247e5c34e6f4a0`.
The grouped model/public-surface/import-boundary/documentation run recorded
**2,679 tests, two failures**, zero errors/skips, 52.652 seconds, terminal exit 1:
`.grafx-tmp/fp6-typed-collections-foundation-contracts.xml`, SHA-256
`f3a3d8af32876c2564ba77eb7e365f0fc67d5b4378bf40b7ad6b9a7f124c6a22`.
Both were annotation contract failures: nested codec visitors lacked complete
annotations, and a cross-module string Value alias could not resolve Timestamp
in this module's namespace. The visitors are now annotated and the returned native
value union is locally resolvable. No normalization or schema rule was relaxed.

Corrective model/runtime-annotation/import-boundary selection: **428 passed**, zero
failures/errors/skips, 13.258 seconds, terminal exit 0, receipt
`.grafx-tmp/fp6-typed-collections-foundation-corrective.xml`, SHA-256
`da221c3d0d61a86b603f0c3a236cd8685185e72ad0bdc34e17132f6cc0973c17`.
All **eight module public-surface checks** also pass, 1.011 seconds, exit 0:
`.grafx-tmp/fp6-typed-collections-surface-corrective.xml`, SHA-256
`135074f5cd79ce384b59c0e25340da0693a8cd4c5dabcea4464769c6b6c3fde8`.
Counts overlap; the broad initial selection was not relabeled as a clean rerun.
At that historical foundation checkpoint, changed-source Ruff, documentation/API/
links/configuration checks and whitespace validation passed. Native columns were
not yet integrated then; the current native increment is described above.
