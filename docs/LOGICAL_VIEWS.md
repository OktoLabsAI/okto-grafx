# Logical read views

Grafx 0.0.6 adds persistent named read queries through `Database.views`. These are
logical views, not materialized results. Import parameter declarations from
`okto_grafx.views`; native types are `okto_grafx.domain.model.value.ValueType`.

```python
from okto_grafx import connect
from okto_grafx.domain.model.value import ValueType
from okto_grafx.views import ViewParameter

with connect("graph") as db:
    # Existing base table: Note(id INT64 PRIMARY KEY, body STRING).
    db.views.prepare()  # explicit, idempotent registry creation
    db.views.create(
        "note_by_id",
        query="MATCH (n:Note) WHERE n.id=$id RETURN n.body",
        parameters_schema=(ViewParameter("id", ValueType.INT64),),
    )
    result = db.views.execute("note_by_id", {"id": 7})
    print(result.columns, result.rows)
    print(db.views.get("note_by_id"))
    print(db.views.list(limit=100))
```

## Semantics and limits

- `prepare` is explicit; `create` never implicitly creates the registry. Repeating
  preparation validates ownership and adds no commit. It requires a writable handle.
- Names are case-sensitive ASCII identifiers, letter first, <=64 characters.
- `create(..., replace=True)` atomically replaces an existing definition, or creates
  an absent one. Default `replace=False` refuses duplicates. There is no automatic
  OCC retry. `drop(name)` returns whether a definition was deleted; absent adds no commit.
- Query text is <=64 KiB UTF-8. All node labels and relationship types must be
  explicit base tables. MATCH/RETURN, projections, aggregations and the existing
  two-branch UNION subset are supported to the extent the native query planner supports them.
- CALL, DDL, updates, reserved `_grafx_` metadata tables and nested view expansion
  are refused. Views cannot call themselves or each other. No new Cypher `SHOW VIEW`
  or view-as-table syntax is introduced; `get`/`list` provide typed introspection.
- Declare exactly the query's parameters, at most 32, as an immutable tuple of
  `ViewParameter`. Allowed types: BOOL, INT64, DOUBLE, STRING, BYTES, TIMESTAMP, UUID.
  `nullable=True` allows None but does not make the argument optional. Missing,
  extra and incompatible values refuse; normal native value/resource bounds apply.
- Definitions record output names and hashes of <=64 complete table schemas.
  Changed dependencies refuse; use validated explicit replacement to adopt the new
  schema. Adding an unrelated table does not invalidate a labelled view. Physical
  indexes are optional access paths, not fixed dependencies: normal native
  catalog/index epoch invalidation replans them. No physical plan is persisted.
- `execute(..., snapshot=tx)` accepts an active read transaction of that exact
  database handle. Definition and data are read at that same snapshot; replacing
  or dropping a definition does not change a pinned reader's visible definition.
  Without a snapshot the operation opens and closes its own read transaction.
- `execute(..., timeout_seconds=...)` forwards the native query deadline; it is
  not an end-to-end deadline for registry lookup and schema validation.
- `list(after=name, limit=100, snapshot=tx)` pages lexicographically, maximum 1024
  definitions per call. Reuse a pinned snapshot for stable pages. This bounds the
  result, not the total registry scan work of ORDER BY.

## Durability and errors

Each create/replace/drop is a normal WAL-backed transaction. Native OCC, multi-reader/
writer behavior and recovery remain in force. No service, global writer lock, new
connection setting, background refresh or persistent query result is added.

`GrafxLedgerError` classifies registry issues (`not_prepared`, `invalid_owner`,
`already_exists`, `not_found`, `definition_checksum`, `stale_dependency`, etc.).
Invalid arguments use `GrafxConfigurationError`; unsupported query shapes use native
plan/unsupported errors. Native transaction conflicts and uncertain durable outcomes
propagate unchanged. After uncertain ACK, reopen and inspect the definition before retrying.

Physical backups preserve definitions. They are stored in an owned ordinary table;
do not mutate `_grafx_views_v1` directly. This is not an authorization sandbox or
tamper-proof secret store. Logical copying/importing the registry does **not** certify
destination schema identities: recreate/revalidate views there. See the exact
[persistence and compatibility contract](specs/LOGICAL_VIEWS_V1.md) and
[API signatures](API_REFERENCE.md#logicalviews).
