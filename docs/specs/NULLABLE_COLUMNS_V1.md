# Nullable appended columns v1

Implemented development contract for continuation item 6. The
[Windows acceptance checkpoint](../reports/V006_VIEWS_SCHEMA_CHECKPOINT.md)
records feature, recovery and grouped regression evidence; it is not a release or
cross-platform certification.

The only operation is appending one nullable non-vector column to an existing
node/relationship table in a dedicated native transaction. No rename, drop, default,
PK/endpoint alteration, non-null backfill, type conversion or physical row rewrite.
Existing v2 identity indexes must be explicitly enabled first.

Catalog v2 required capability `nullable_columns_v1` uses bit 13. For each table,
immediately following its existing column definitions, enabled catalogs append a
little-endian u16 layout count followed by `(u16 schema_version, u16 column_count)`
pairs. Tables without evolution have count zero. At most 64 prior layouts; each
successive layout appends exactly one column and increments schema version by one.
All appended columns are nullable and existing columns remain the identical prefix.
The existing catalog CRC covers these bytes. Without the required capability no
extension bytes exist and nonempty layout metadata is invalid.

The heap header still records the version under which its payload was written.
Only an exact recorded prior layout may decode an old payload: validate its original
column types and full byte consumption, then pad the appended columns with NULL.
Unknown/future versions refuse. Current writes encode the complete current layout.
This is decoding compatibility, not temporal/historical schema querying.

Catalog after-images and required capability publish in the same ordinary WAL-backed
COMMIT. No heap rewrite or external index-file effect is required because existing
column positions, identity, PK, endpoints and vector columns are unchanged. Old
binaries refuse bit 13 rather than interpreting shortened tuples. Rollback discards
the staged catalog; native recovery validates COMMIT and replays the same after-images.
Uncertain acknowledgement propagates; inspect schema after reopen before retrying.

Concurrent schema mutations use the native catalog page OCC interests. Old writer
tuples must not be silently reinterpreted under a changed layout; commits must fail
typed if incompatible. Readers use native schema-epoch handling and may refuse stale
continuation cursors; this operation does not promise a retained historical schema.
Physical backup retains layout metadata; logical export emits decoded current values
for a fresh destination. Views detect dependency changes and require explicit replacement.
