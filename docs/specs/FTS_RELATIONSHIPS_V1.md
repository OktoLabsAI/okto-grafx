# Relationship text indexes — continuation after 69ed311

Approved item 7: existing `create_text_index` / typed `search_text` may target
declared STRING relationship properties. Native physical positions include two
endpoint RecordIds before declared properties; those endpoints cannot be indexed
as text. Hits identify physical relationship RecordIds in the named index's table,
preserving parallel occurrences, direction, self-loops and fixed snapshots.
`TableDef.columns` and `column_index()` already include those endpoints: consumers
must not add another offset to the schema's physical position.

Required capability `fulltext_relationships_v1` (bit 12, depends on bit 5) is
published with the catalog definition. Existing posting/WAL formats are reused;
old readers refuse the new capability. Prefix/statistics/history capabilities
remain independently required when selected. Normal endpoint integrity checks,
transaction quotas, dual OCC, crash replay, verification and immutable index
generation lifecycle apply. Transfer must translate physical positions back to
declared column names and rebuild against remapped destination endpoints.

The existing scalar-ID `CALL grafx.search_text` procedure also supports these
indexes without manufacturing Node values. Hybrid retrieval remains node-only;
new procedure syntax or cross-kind hybrid fusion is not implicitly enabled.

Acceptance: parallel/self-loop relationships, NULL fields, update/delete/rollback,
old/current readers, prefix and durable statistics, verification, backup/logical
transfer and crash recovery. [Round evidence](../reports/V005_AFTER_69ED311.md).
