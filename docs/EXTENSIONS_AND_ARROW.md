# Trusted extensions and Arrow export

These are opt-in Python integration capabilities in 0.0.5 development. Neither
changes a database format, enables a server, imports code from a database nor
weakens WAL, OCC or independent reader/writer rules.

## Scalar extension SPI

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, ScalarFunction

extensions = ExtensionRegistry(
    scalars=(ScalarFunction("app.upper", ("STRING",), "STRING", str.upper),),
    trusted=True,
)
with connect(":memory:", extensions=extensions) as db:
    result = db.execute("RETURN udf('app.upper', $value) AS normalized",
                        {"value": "hello"})
    assert result.rows == (("HELLO",),)
```

`connect(..., extensions=None)` keeps extensions disabled. This factory parameter
is a trusted object, **not** a serializable `DatabaseConfig` field; lower-level
`open_database`/port assembly does not implicitly discover registrations. Supply
the registry again for every connection/process that executes UDF queries.
Stored scalar outputs remain normal values, readable without the callback.

Registry and descriptors are frozen, exact types, with at most 128 distinct,
case-sensitive names. Names must be dot-separated identifiers, at most 128
characters, with a namespace such as `app.upper`. No builtin can be replaced.
The query name must be a string literal; parameters remain supported for values.
No module names, paths, entry points, wildcard discovery or installation hooks are
resolved. `registry.scalars` provides explicit capability inspection.

`ScalarFunction(name, argument_types, return_type, implementation,
max_value_bytes=1048576)` takes at most 32 positional arguments. Type names are
`BOOL`, `INT64`, `DOUBLE`, `STRING`, `BYTES`, `TIMESTAMP`, `UUID`. Python types are
exact: bool is not INT64, int is not DOUBLE, strings are not timestamps/UUIDs.
Native finite/range/UTF-8 constraints apply. Container/vector/entity callbacks
are not supported. Every type is nullable: any NULL argument returns NULL without
calling the implementation; a callback may also return NULL. Other arguments are
validated even when one is NULL. No implicit coercion occurs.

`max_value_bytes` is 1..2^31, per input/output scalar; STRING is conservatively
charged four bytes per character, BYTES its length and fixed scalars 16 bytes.
It does not bound callback allocations, CPU time or total process memory.
Arity and actual value types are checked on invocation; malformed query syntax
and unregistered literal names are rejected during planning. `call_scalar(name,
arguments_tuple)` also provides typed direct invocation, without a database.
Type/arity/callback failures are `GrafxPlanError`; value budgets raise
`GrafxQueryBudgetExceeded(resource="udf_value")`; invalid registrations raise
`GrafxConfigurationError`. Callback error details do not interpolate its values.

Queries may use these scalars in projections, filters, supported UNION expressions
and write values. UNION typing uses declarations, never executes a callback for
planning. Callbacks must be deterministic, side-effect-free, thread-safe, and must
not re-enter Grafx. Evaluation may be skipped for an empty/short-circuited branch
or repeated during execution/retry; there is no exactly-once callback guarantee.
No transaction, storage device, WAL or index handle is passed to a callback.
**This is not a sandbox:** a trusted Python closure can still access its host's
resources. Do not accept code from untrusted users. Cooperative cancellation does
not preempt a running callback. Aggregates, table functions, optimizer plugins and
durable function manifests remain outside this initial SPI.

## Optional Arrow batches

Install `okto-grafx[arrow]` (or `.[arrow]` in this checkout). Core import/connect
does not import PyArrow. Missing optional dependency is a typed
`GrafxUnsupportedOperation` when export is requested.

```python
from okto_grafx.arrow import to_arrow_batches

with db.query("MATCH (d:Document) RETURN d.id, d.title ORDER BY d.id").cursor() as cursor:
    for batch in to_arrow_batches(cursor, types=("INT64", "STRING"),
                                  batch_rows=256, max_batch_bytes=16777216):
        consume(batch)
```

`source` is an exact native `QueryResult` or `QueryCursor`. `types` is an explicit
tuple, one entry per column, including empty/all-NULL results. It uses the scalar
type names above; nested lists/maps/vectors/entities are refused rather than
converted to lossy JSON. Mappings: BOOL→bool, INT64→int64, DOUBLE→float64,
STRING→UTF-8, BYTES→binary, TIMESTAMP→timestamp[us, UTC], UUID→fixed-size binary[16].
Every field is nullable and carries `grafx.type` metadata. No numeric/string
inference, bool-to-int or precision-losing coercion is performed.

`batch_rows` is 1..65,536 (default 256). `max_batch_bytes` is 1..2^31 (default
16 MiB), a conservative per-batch logical budget: 256 fixed +256 per column,
then 64 per cell plus four bytes per STRING character, BYTES length or 16 for
fixed/null cells. It excludes the already-detached source rows and PyArrow/allocator
overheads; it is not an RSS cap. Conversion retains at most one fetched batch.
The generator validates on first iteration. Failure emits no partial current batch,
but earlier yielded batches remain valid. Empty input yields no batches.

Results/batches are independent of Grafx page buffers; returned Arrow objects own
their memory and survive database close. A `QueryResult` is already materialized;
export does not turn it into a streaming query or acquire a snapshot. A cursor
uses its existing fixed MVCC snapshot and fetch budget. The caller owns and must
context-manage/close that cursor on early break or conversion failure; export does
not transfer or silently close it. Do not concurrently consume one cursor.

## Evidence and known boundaries

The [round receipt](reports/V005_NEXT_EIGHT_PROGRESS.md) records actual tests and
the [compatibility matrix](V005_COMPATIBILITY.md) distinguishes local evidence
from unexecuted platform rows. These APIs are not Pulse deployment evidence.
