# Heterogeneous properties v1

Contract developed on `feature/v0.0.6` and shipped in 0.0.6; not a broader compatibility claim.
This is the storage/query foundation of the authorized flexible graph model,
**not completion of FP-3/FP-6**. Native unlabeled node creation is now a separate
implemented layer: [flexible graph contract](FLEXIBLE_GRAPH_V1.md).

## Declaration versus value

`SchemaType.ANY` (from `okto_grafx.domain.model.schema`) is a schema declaration,
not a new `ValueType`. The catalog column tag is 255. Its required catalog-v2
capability is `heterogeneous_properties_v1`, bit 20. Concrete row values retain
the existing value tags 0–11, plus native temporal tags 12–17 when the separate
`temporal_values_v1` capability is active; a payload with value tag 255 is corruption.
No stringification, first-row type inference or implicit numeric conversion occurs.

ANY accepts NULL according to column nullability; finite BOOL/INT64/DOUBLE,
STRING/BYTES, Grafx TIMESTAMP/UUID wrappers, the six
[native temporal families](../TEMPORAL_VALUES.md), and recursively nested LIST/MAP
values. The first native temporal write, including a nested value, publishes its
additional capability in the same transaction. Existing integer, UTF-8, string-key, nesting and transaction/query limits
remain enforced. Query lists detach as tuples. NaN/infinity are prohibited in
stored properties, including nested values; expression NaN remains supported.
Arbitrary Python objects and detached graph entities are not stored values.
Embeddings still require a declared `VECTOR(space)` column; ANY, including nested
ANY collections, cannot bypass embedding-space identity/precision validation.

## Usage and transaction contract

```python
from okto_grafx import connect

with connect("./example.grafx") as db:
    # Explicit, idempotent catalog-v2 activation, outside an active transaction.
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Item(id INT64, value ANY, PRIMARY KEY(id))")
        tx.execute("CREATE (:Item {id:1,value:'start'}), (:Item {id:2,value:2})")
        tx.execute("MATCH (n:Item {id:1}) SET n.value = {nested:[true,3]}")
    rows = db.execute("MATCH (n:Item) RETURN n.value ORDER BY n.id").rows
```

Both node and relationship property columns support ANY. `SET` may change the
concrete type without a schema rewrite; NULL remains NULL, not a record deletion.
Operators check the concrete per-row type, including property/subscript access.
An invalid later row rolls back the entire statement while preserving prior
successful statements of the transaction. Readers retain their own snapshot;
same-record writes from independent participants retain OCC conflict detection.

Installing ANY columns and their capability is one native catalog mutation on the
transaction's working copy. Rollback cannot publish the schema/capability. Existing
v1 catalogs refuse ANY DDL with an explicit activation remedy. Nullable-column
append also installs the capability and retains old row layouts. An ANY primary
key or secondary property index currently refuses: mixed-key equality/order and
index normalization need a separate contract. Typed identity/endpoint indexes are
unaffected; typed key columns may coexist with ANY properties.

## Durable read and recovery

Catalog decode refuses ANY without its required capability, even with a valid
checksum. Older readers reject the unknown capability before serving the catalog
or applying its committed recovery pages. Complete native COMMIT proof is still
required; replay is idempotent. No WAL, OCC, page ownership or durability fence is
weakened. The tuple decoder validates concrete tags and storage admission even
when projection omits the ANY property. This initial projection path materializes
and discards compound values to perform that validation; a bounded streaming
optimization remains future work, not an exemption from validation.

## Integration matrix and outstanding qualification

| Surface | Current contract |
|---|---|
| Native DDL/CREATE/SET/read | ANY node/edge columns, dynamic expression type checks |
| Python schema/plan/result snapshots | Exact `ValueType | SchemaType` enum capture; not arbitrary union objects |
| Logical graph export/import | Concrete value codec and ANY schema retained; target activates catalog v2 |
| Primary/secondary indexes on ANY | Explicit refusal; use a separate typed key column |
| Vector indexing | Separate typed vector column and declared embedding space |
| Arrow/Parquet/Pandas | No ANY/union column type advertised; existing explicitly typed projections remain the interface |
| Label-free node creation, property-key growth, empty-label entity DTO | Implemented in the separate flexible graph layer, including automatic undeclared relationship creation and endpoint-pair growth |
| Retained history | Flexible flags and selected logical relationship membership preserved with explicit model-history capability fencing; [contract](../SYSTEM_TIME_HISTORY.md#durable-model-admission-and-compatibility) |
| Existing-target copy/promotion | Native target identity mapping preserves equal-valued distinct nodes and actual endpoints, with atomic receipts; [contract](../CATALOG_COPY.md#flexible-and-no-pk-entity-identity) |
| Broad engine/acceleration and Pulse qualification | Remain part of final flexible-model qualification; these increments are not that gate |

No new configuration option is introduced. Safety/resource options remain in
[configuration](../CONFIGURATION.md); the remaining acceptance scope is in the
[functional parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties).
Tests: `tests/storage_core/test_any_properties.py`, `tests/query/test_any_properties.py`.
