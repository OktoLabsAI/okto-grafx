# Procedure native query authority — FP-7

Status: implemented on `feature/v0.0.6`, development only. Extends
[writing procedures](WRITING_PROCEDURES_V1.md) and [entity signatures](PROCEDURE_ENTITY_SIGNATURES_V1.md).
[Qualification](../reports/FP7_QUERY_QUALIFICATION.md). This is not complete FP-7.

## Public API and effects

Import `ProcedureReader`, `ProcedureWriter`, `ProcedureResult`, `TabularProcedure`
and `ExtensionRegistry` from `okto_grafx.extensions`. Applications register callbacks;
the engine supplies the authority object as their first argument. Do not construct
an authority object or capture a Database/Transaction to bypass this boundary.

| Descriptor | First callback argument | Allowed graph operations |
| --- | --- | --- |
| Default `mode="read", graph_read=False` | Only declared value arguments | No engine-issued graph access; existing pure callbacks unchanged |
| `mode="read", graph_read=True` | ProcedureReader, then declared arguments | Native read queries, including RETURN, UNION and subqueries; no writes |
| `mode="write"` | ProcedureWriter, then declared arguments | Existing result-free `execute()` plus read and returning-write `query()` |

`graph_read` must be an exact bool and is only accepted in read mode. Both graph
readers and writers require nonempty `required_permissions`, all granted by the
connection-local immutable registry. No callback is executed during signature
resolution. Denied access and read-only write attempts fail before graph effects,
including queries whose write branch would produce zero rows.

```python
from okto_grafx.extensions import TabularProcedure, ExtensionRegistry

def selected(reader, threshold):
    return reader.query(
        "MATCH (n:Item) WHERE n.score >= $minimum RETURN n",
        {"minimum": threshold},
    ).rows

procedure = TabularProcedure(
    "app.selected", ("INT64",), (("node", "NODE"),), selected,
    graph_read=True, required_permissions=frozenset({"catalog_read"}),
    max_query_statements=16, max_query_rows=500, max_query_bytes=1024 * 1024,
)
extensions = ExtensionRegistry(
    trusted=True, procedures=(procedure,),
    procedure_permissions=frozenset({"catalog_read"}),
)
# On a database with declared Item schema:
# CALL app.selected(10) YIELD node RETURN node.id, node.score
```

`reader.query(text, parameters=None)` and `writer.query(...)` return
`ProcedureResult(columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...])`.
Results expose no plan, transaction, cursor or database handle. Native scalar,
temporal/vector, list/map and entity cells are owned and validated under the
procedure value contract. Nested lists/maps are callback-owned copies. Result
tuples and DTO fields are structurally immutable; owned containers remain mutable.

Entity results are newly witnessed in the current invocation. They may be returned
through matching NODE/RELATIONSHIP/PATH or container columns and regain their native
identity downstream, including nodes created by a returning write query before
COMMIT. Copying/reconstructing an entity DTO does not grant authority. Query
parameters still use the native public parameter contract: exact dict or None,
with supported values, **not detached entity handles**. Pass scalar properties to
query predicates; return witnessed entities instead of inventing identities.

## Transaction, lifetime and failure

Every child query uses the outer transaction, snapshot, current private intents,
catalog, statement/transaction temporal clocks and native mutation budgets. Realtime
clock functions remain realtime. A writer can query its previous writes;
independent participants keep their own snapshots and can progress while a trusted
callback is waiting. This feature creates no extra writer, second COMMIT, lease
policy or serialization lock.

Authorities are synchronous, owner-thread bound, non-reentrant and expire when the
callback's output stream closes, fails or is cancelled. A graph reader's iterator
can be consumed lazily through a read cursor; closing it revokes the capability.
Writing callbacks still drain/validate all output columns and late rows before
downstream publication, even when LIMIT would hide them. No callback is retried
implicitly. An operation failure poisons the invocation even if the callback
catches it; the existing foreign-thread refusal does not let another thread poison
an active legitimate owner.

The outer statement owns rollback, including effects of earlier `execute()` or
returning `query()` calls, result validation and callback cleanup. Earlier successful
statements in the transaction remain intact. Parent deletion invalidation applies
to child writes. Native read cancellation/deadline control is propagated to child
queries; arbitrary blocking Python code is still not forcibly preempted. Host
network/filesystem effects are trusted, not sandboxed or transactionally undone.

## Descriptor limits and accounting

These are descriptor fields, not new global `connect()` settings:

| Field | Default | Allowed range / accounting |
| --- | --- | --- |
| `graph_read` | False | Exact bool, read mode only, explicit permissions required |
| `max_query_statements` | 128 | Exact int 1..1024; cumulative `query()` calls for this procedure across input rows |
| `max_query_rows` | 10,000 | Exact int 1..2^31; cumulative child result rows, refused before retaining row limit + 1 |
| `max_query_bytes` | 8 MiB | Exact int 1..2^31; cumulative admitted child result cell bytes |

Existing `max_value_bytes` also bounds every child result cell. Native query
operator/row/memory budgets remain in force before result publication. Rows are
counted during child collection; cells are detached/admitted before being handed
to the callback. Entity headers/property/path occurrences use the existing logical
entity tariff, not an exact Python heap-size promise. Query budgets accumulate
separately from callback output `max_rows`/`max_result_bytes`: discarding a child
result does not reclaim its allowance. Write-bearing `query()` calls also consume
`max_write_statements`, shared with `execute()`. A result-free query has no result
rows, but still consumes query/write statement budgets. Budgets reset with the
outer statement, not between input-row invocations.

Use smaller limits for high-fanout callbacks. Prefer projected scalar fields when
complete entity properties are unnecessary. This is bounded materialized child
query access, not a second streaming cursor API.

## Remaining scope and nonclaims

Child queries accept native Query/UnionQuery forms but still reject DDL, transaction
control. Implicit schema additionally requires the subsequent explicit
[schema authority](PROCEDURE_SCHEMA_AUTHORITY_V1.md); DDL uses writer.schema(),
not query(). The [nesting/effect contract](PROCEDURE_NESTING_EFFECTS_V1.md)
adds recursive registered CALL, inherited depth and explicit default-volatile
determinism. Native read/write subqueries retain their existing effect rules.
Schema authority retains its documented native DDL boundaries. Do not work around remaining
items by capturing another Database. No persisted tag, capability flag or global
installation change is introduced; full FP-8/Pulse qualification remains separate.
