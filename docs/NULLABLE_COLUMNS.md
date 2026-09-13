# Append nullable columns without a row rewrite

Development collection additions accept `ColumnDef.stored_type`: a collection-root
`StoredType` with matching LIST/MAP family and nullable root. Nested children may
be non-nullable; preexisting rows receive NULL for the entire new column. Descriptors
and their capabilities are published atomically through the existing operation.
Non-nullable root additions remain refused. [Usage and metadata](specs/TYPED_COLLECTIONS_V1.md).

Grafx 0.0.6 development adds the typed `Database.add_nullable_column(table, column)`
operation. It runs a dedicated native transaction and returns the new immutable
`TableDef`. Existing rows acquire NULL in the added column when read; new/updated
rows use the complete new layout.

```python
from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType

with connect("graph") as db:
    # Explicit, one-way prerequisite; existing table Note was created earlier.
    db.ensure_identity_indexes()
    schema = db.add_nullable_column("Note", ColumnDef("summary", ValueType.STRING))
    print(schema.schema_version, schema.schema_layouts)
    print(db.execute("MATCH (n:Note) RETURN n.summary").rows)
```

## Supported scope

- `table` accepts a unique name or `("node", name)` / `("rel", name)`.
  When both kinds use the same name, qualify the intended table, for example
  `db.add_nullable_column(("rel", "R"), ColumnDef("note", ValueType.STRING))`.
  A bare ambiguous name refuses before schema/capability publication. The returned
  `TableDef` retains the selected physical identity; the other kind is unchanged.
  Grouped relationship schema additions remain unsupported; a qualified selector
  does not bypass that guard or the `_grafx_` reserved-table guard.
- Append exactly one nullable, non-vector column to a node or relationship table.
  Existing columns, table identity, PK and relationship endpoints are unchanged.
- No table scan or heap row rewrite during the operation. Existing exact/FTS/vector
  access paths retain their column positions. Explicitly create an index on the new
  column if needed. Old payload decoding has additional CPU/allocation cost; no
  universal read-throughput or elapsed-time improvement is claimed.
- At most 64 prior layouts per table, 65,535 columns and schema version <=65,535.
  These are fixed format/API bounds, not connection settings.
- Column definitions use native types and nullability. Duplicate names, non-null or
  vector additions, missing tables, reserved `_grafx_` tables and legacy catalogs
  without explicit v2 activation refuse without committing schema changes.
- This does not add ALTER TABLE syntax, defaults, rename, drop, type conversions,
  online non-null backfills or integration into the string-only migration ledger.

## Transactions, readers and recovery

Development DECIMAL additions use
`ColumnDef("amount", ValueType.DECIMAL, decimal_precision=10, decimal_scale=3)`.
The public capture and returned catalog preserve both parameters; old rows still
read NULL. The same schema COMMIT publishes `decimal_values_v1`. Exact typed
equality indexes have separate qualification; ordered indexes and full interchange
remain outside the current supported matrix. [Full scope](specs/DECIMAL_VALUES_V1.md).

Catalog and required capability publish atomically under normal WAL/OCC. A failed
pre-COMMIT attempt discards its staged catalog. An ambiguous post-COMMIT result is
not permission to blindly retry: reopen and inspect the returned column/type and
schema version first. Repeating a successful add encounters the duplicate name;
the operation is not an idempotency-key API.

Concurrent DDL cannot overwrite another successful schema change. A row writer
bound to the previous schema is refused before materialization/WAL after a foreign
schema change; open a new transaction and rerun the operation. No global single-
writer mode or weaker OCC is introduced. Current-layout row reads take the existing
decoder path; prior layouts are accepted only if their exact version/width appears
in the catalog and the original payload passes native decoding. Unknown or future
versions are not treated as compatible.

Native catalog epoch invalidation remains authoritative. A continuation cursor
bound to a prior schema can refuse with a typed schema mismatch; restart pagination
under the current schema. This feature is not retained historical-schema querying.
Logical views depending on the altered table refuse stale dependency hashes until
explicitly replaced/revalidated.

## Compatibility, backup and transfer

The operation activates catalog-v2 required bit 13, `nullable_columns_v1`. Older
builds unable to decode this layout must refuse normal open/recovery, not guess
tuple widths. Activation is one-way; there is no in-place downgrade or removal of
the bit. See [exact bytes and protocol](specs/NULLABLE_COLUMNS_V1.md).

Physical backup/restore preserves all prior layouts. Logical export emits complete
decoded current rows and resets exported table schema versions to 1 for fresh-table
import: source physical layouts are intentionally not retained. This does not export
temporal history or certify that an older consumer supports every other used feature.

Public errors remain native `GrafxConfigurationError`/schema errors for invalid
definitions, `GrafxSchemaVersionMismatch` for incompatible layouts, and existing
conflict, recovery, durability and read-only errors. There is no automatic retry,
background job, new port/service or Pulse-specific dependency.
