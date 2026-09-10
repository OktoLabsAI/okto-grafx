# Logical views v1: bounded persistence contract

The first GX-CAP-10 view slice is a named, parameter-typed **read query**, consumed
through `db.views`, not new Cypher syntax or a materialized graph. Dependencies are
explicitly labelled base tables. Nested views, CALL, unlabelled graph-wide scans,
DDL and updating queries are refused; the expansion depth is therefore zero.

Persistence uses an ordinary native node table `_grafx_views_v1`, with `name`
(STRING primary key), `body` (STRING), and `sha256` (STRING). The owner row is
`_owner`, body `grafx-logical-views-v1`, and its SHA-256. Definitions use `v_` plus
the case-sensitive view name. Bodies are canonical ASCII JSON arrays containing
version 1, query text, ordered parameter declarations, output column names and
sorted `[table_name, schema_sha256]` dependencies. Hashes cover exact ASCII bytes.
The schema digest covers the complete native TableDef, including table identity
and schema version, encoded as sorted canonical JSON. There is no executable code,
pickle, user callback or dynamic import in this representation.

Limits: query UTF-8 <=64 KiB; <=32 parameters; <=64 table dependencies; persisted
body <=256 KiB; introspection returns <=1024 definitions per call. Existing native
query/transaction budgets additionally apply. No new connection setting is added.

Preparation is explicit and idempotent. Creation, replacement and drop each use
one native transaction. Definition validation occurs before staging. Concurrent
updates use existing primary-key and OCC enforcement, with no automatic retry or
global writer lock. Read execution fetches the definition at the caller's native
read snapshot, verifies its checksum and dependencies, then runs only its validated
read AST at that same snapshot. The engine's normal catalog/index-epoch query cache
remains authoritative; no serialized physical plan or index handle is retained.
Physical indexes are optional access paths, not semantic view dependencies.

All effects are ordinary native table/heap/index WAL effects. Before COMMIT none
are authoritative; after durable COMMIT definition replacement is atomic and
replay-idempotent under native recovery. An uncertain acknowledgement is propagated;
callers inspect `get` after reopen before deciding whether to retry. Physical backup
preserves definitions. Logical transfer includes this ordinary table only when
selected; dependency IDs must be revalidated/recreated at the destination.

No required format bit is introduced: old readers can decode this ordinary table
but do not acquire the new API. The namespace is an API ownership convention, not
an authorization barrier against an unrestricted writer. Direct edits are unsupported;
checksum/owner checks and read-AST validation fail closed, not cryptographic security.
Missing, altered or replaced dependencies refuse execution, never silently retarget.
