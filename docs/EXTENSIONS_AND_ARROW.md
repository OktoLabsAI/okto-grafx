# Trusted extensions and Arrow import/export

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

### Atomic typed batch import

```python
from okto_grafx.arrow import import_arrow_batches

with db.begin("write") as transaction:
    report = import_arrow_batches(
        transaction, "CREATE (:Document {id:$id, title:$title})", batches,
        types=("INT64", "STRING"), max_rows=10000,
    )
    assert report.statements <= 10000
```

The iterable must yield exact PyArrow `RecordBatch` objects. Fields are named
parameters in the supplied statement. All batches have the same ordered, unique,
nonempty field names (at most 256 characters each) and 1..256 columns. `types` is
an explicit tuple using the scalar names/mappings or `ArrowVectorType` below. Exact
Arrow types are required: no int32 widening, dictionary decoding, timezone/unit
inference, arbitrary nested types or lossy coercion. Optional scalar `grafx.type`
field metadata must agree; vector metadata is mandatory. NULL is
accepted according to the native target schema; timestamp microseconds, including
negative values, and UUID bytes retain exact native meanings. Query DDL spells the
binary property type `BLOB`, whereas this scalar interop type is `BYTES`.

One native `Transaction.executemany` staging savepoint covers **every batch** in
the call. Late malformed input, producer exceptions, budget errors or statement
failures discard that call's staged effects but preserve earlier transaction work.
Nothing is auto-committed; the caller owns commit/rollback, uncertain-commit handling
and retries. Native transaction quotas remain active, so this is not unbounded
streaming ingestion. The report is `ExecuteManyReport`, not one result per row.

| Parameter | Default | Bound |
| --- | ---: | --- |
| `max_batch_rows` | 65,536 | 1..65,536 |
| `max_batch_bytes` | 16 MiB | 1..2^31 logical bytes |
| `max_rows` | 1,000,000 | 1..2^31 across the call |
| `max_batches` | 4,096 | 1..2^31, including empty batches |

Bool is not an integer option. Before Python scalar conversion, a batch is charged
256 + 256 per column + 80 per row/column cell + four times `batch.nbytes` (16 times
when any column is a vector, reserving component-conversion workspace). One batch
is consumed at a time. This is not RSS, caller-owned Arrow buffers, whole-transaction
staging memory, or a deadline on arbitrary input producers. The source is neither
closed nor retried by Grafx. Unsupported/mismatched types are typed unsupported
operations; malformed shape/options are configuration errors; budget refusals use
`GrafxQueryBudgetExceeded(resource="arrow_import")`. This does not add COPY syntax,
Parquet/CSV sources, external scans or zero-copy ownership.

Install `okto-grafx[arrow]` (or `.[arrow]` in this checkout). Core import/connect
does not import PyArrow. Missing optional dependency is a typed
`GrafxUnsupportedOperation` when import or export is requested.

```python
from okto_grafx.arrow import to_arrow_batches

with db.query("MATCH (d:Document) RETURN d.id, d.title ORDER BY d.id").cursor() as cursor:
    for batch in to_arrow_batches(cursor, types=("INT64", "STRING"),
                                  batch_rows=256, max_batch_bytes=16777216):
        consume(batch)
```

`source` is an exact native `QueryResult` or `QueryCursor`. `types` is an explicit
tuple, one entry per column, including empty/all-NULL results. It uses the scalar
type names above or vector descriptors below; arbitrary lists/maps/entities are refused rather than
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

### Explicit native vectors

Use `ArrowVectorType(space_ref, dimension, dtype="float32")` as the corresponding
entry in `types` for both functions. `space_ref` is an exact integer 1..2^32−1,
`dimension` is 1..16,384 and `dtype` is exactly `float32` or `float64`. No PyArrow
import is needed to construct the descriptor. The Arrow field is a **fixed-size
list**, whose child is float32/float64 and whose list size is dimension. Export
requires exact native `VectorValue` objects, not arbitrary Python lists.

Mandatory field metadata (UTF-8/ASCII byte keys and values) is:

| Field key | Value |
| --- | --- |
| grafx.type | VECTOR_F32 or VECTOR_F64, matching dtype |
| grafx.space_ref | Decimal store-local space ID |
| grafx.dimension | Decimal dimension |
| grafx.dtype | float32 or float64 |

All four must match the explicit descriptor on import. Variable-size lists, wrong
child precision/dimension, missing metadata, NULL components, non-finite values
and mismatched native space/precision are refused. The whole vector may be NULL
when the target permits it. Export validates native finite/range rules, preserving
declared precision: float32 uses native float32 representational rounding, never
silently changes a float64 vector to float32. The export tariff adds 128 + 32 ×
dimension per vector cell (including NULL) on top of the fixed 64-byte cell charge.

Native write admission validates target **space identity, dimension, active state,
precision and normalized declaration** even for already-encapsulated `VectorValue`
parameters. Arrow import uses that same native door, within the whole-call savepoint;
a later invalid vector rolls back this import's prior rows, not prior caller writes.
Target-schema violations are native typed Grafx errors, not partial import reports.

Space IDs are **store-local**, not universal embedding/model identifiers. This
interop performs no cross-store space mapping; equal numeric IDs in different
stores do not prove semantic equivalence. Establish matching target definitions
explicitly, or use [logical graph transfer](LOGICAL_TRANSFER.md) for graph/space
mapping. Arbitrary nested data, graph entity export, zero-copy and external scans
remain out of scope.

```python
from okto_grafx.arrow import ArrowVectorType, to_arrow_batches, import_arrow_batches

# Documents and Copies declare v VECTOR(emb); both use the same store-local space.
space = db.catalog.catalog.space("emb")
vector_type = ArrowVectorType(space.space_id, space.dimension, space.storage_dtype)
source = db.execute("MATCH (d:Documents) RETURN d.id AS id,d.v AS v ORDER BY d.id")
batches = to_arrow_batches(source, types=("INT64", vector_type))
with db.begin("write") as transaction:
    report = import_arrow_batches(transaction, "CREATE (:Copies {id:$id,v:$v})",
                                  batches, types=("INT64", vector_type))
```

## Evidence and known boundaries

The [round receipt](reports/V005_NEXT_EIGHT_PROGRESS.md) records actual tests and
the [vector/projection continuation](reports/V005_AFTER_7DDE256.md) records this slice;
the [compatibility matrix](V005_COMPATIBILITY.md) distinguishes local evidence
from unexecuted platform rows. These APIs are not Pulse deployment evidence.
