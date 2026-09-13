# Trusted extensions and Arrow import/export

[Schema-enabled procedures](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md) add explicit
`schema_write=True`, literal `schema` permission and `ProcedureWriter.schema()`.
Native table/vector-space/index DDL and implicit flexible CREATE/MERGE retain
whole-statement rollback. Catalog-v2 indexes compose with pending DML; the contract
covers quotas, compiled-scan visibility and legacy activation prerequisites.

[Nested native procedures and effects](specs/PROCEDURE_NESTING_EFFECTS_V1.md) now
support recursive CALL under inherited depth and shared budgets. Procedure
determinism defaults to False and must be declared truthfully for deterministic
effect admission; ScalarFunction's independent contract is unchanged.

Native [procedure query authority](specs/PROCEDURE_QUERY_AUTHORITY_V1.md) adds
permissioned opt-in readers and writer queries with results, same-snapshot graph
access and cumulative query budgets. Ordinary read callbacks remain pure by
default. No authority can commit independently or escape its invocation lifetime.

The 0.0.6 development line also supports trusted typed tabular procedures through
`ExtensionRegistry(procedures=..., procedure_permissions=...)`. See the complete
[CALL/YIELD contract and example](COMPOSABLE_QUERIES.md#typed-tabular-procedures).
This is distinct from CALL subqueries. Explicit
[writing procedures](specs/WRITING_PROCEDURES_V1.md) now supply a short-lived native
mutation capability with named permissions, shared budgets and outer-statement
rollback; the bounded door is not arbitrary procedure parity.
An empty output schema now declares a [unit procedure](specs/UNIT_PROCEDURES_V1.md):
the callback returns None, CALL preserves incoming rows and standalone execution
returns no result rows. Default `mode="read"` supplies no graph-writing authority;
`mode="write"` receives an engine-issued ProcedureWriter first.
Standalone calls can also use [declared implicit argument names and automatic or
wildcard outputs](specs/PROCEDURE_INVOCATION_V1.md); callbacks are never inspected
or invoked to discover their signatures.
Procedure [numeric signatures](specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md) now support
NUMBER inputs/outputs and validated integer-to-DOUBLE widening. This does not alter
the stricter scalar UDF contract below or introduce a stored NUMBER column type.
The subsequent [native value signature contract](specs/PROCEDURE_NATIVE_VALUES_V1.md)
adds temporal, DECIMAL, LIST/MAP/ANY and vector procedure values, owned callback copies,
recursive budgets and native persistence. This does not widen ScalarFunction or
the independent interchange type contracts below.
NUMBER now also preserves native DECIMAL cells without casting; DOUBLE does not
accept them implicitly. DECIMAL uses a 19-byte ownership/result charge and retains
its p/s through native query/write callbacks. See the
[decimal interface contract](specs/DECIMAL_VALUES_V1.md#json-local-imports-and-procedure-signatures).

[Entity procedure signatures](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md) add NODE,
RELATIONSHIP, PATH and typed node/relationship lists. Native CALL supplies detached
observations and restores only references witnessed in that invocation; copied,
foreign and previous-call entities are refused. Existing descriptor budgets apply.

For typed DataFrames and local Parquet files, see [Pandas/Parquet](TABULAR_AND_PARQUET.md).
Those optional adapters reuse this native batch contract and whole-call savepoint;
they do not add Cypher external scans or a second graph backup format.

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
an explicit tuple using the scalar names/mappings, `ArrowVectorType`,
`ArrowDecimalType` or a collection-root `StoredType`. Exact
Arrow types are required: no int32 widening, dictionary decoding, timezone/unit
inference, arbitrary nested types or lossy coercion. Optional scalar `grafx.type`
field metadata must agree; vector, temporal, decimal and collection metadata are mandatory. NULL is
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
when any column is a vector, temporal struct, decimal or collection, reserving conversion workspace).
Decimal columns additionally charge 1,024 bytes per row/cell as specified below. One batch
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
type names above or vector/decimal descriptors below; arbitrary lists/maps/entities are refused rather than
converted to lossy JSON. Mappings: BOOL→bool, INT64→int64, DOUBLE→float64,
STRING→UTF-8, BYTES→binary, TIMESTAMP→timestamp[us, UTC], UUID→fixed-size binary[16].
Every field is nullable and carries `grafx.type` metadata. No numeric/string
inference, bool-to-int or precision-losing coercion is performed.

`batch_rows` is 1..65,536 (default 256). `max_batch_bytes` is 1..2^31 (default
16 MiB), a conservative per-batch logical budget: 256 fixed +256 per column,
then 64 per cell plus four bytes per STRING character, BYTES length or 16 for
fixed/null legacy scalar cells. Temporal cells use the tariff below. It excludes the already-detached source rows and PyArrow/allocator
overheads; it is not an RSS cap. Conversion retains at most one fetched batch.
The generator validates on first iteration. Failure emits no partial current batch,
but earlier yielded batches remain valid. Empty input yields no batches.

Results/batches are independent of Grafx page buffers; returned Arrow objects own
their memory and survive database close. A `QueryResult` is already materialized;
export does not turn it into a streaming query or acquire a snapshot. A cursor
uses its existing fixed MVCC snapshot and fetch budget. The caller owns and must
context-manage/close that cursor on early break or conversion failure; export does
not transfer or silently close it. Do not concurrently consume one cursor.

### Exact native temporal values (0.0.6 development)

Both functions accept `DATE`, `LOCALTIME`, `TIME`, `LOCALDATETIME`, `DATETIME`
and `DURATION` in `types`. Values must be the exact corresponding
[native Python classes](TEMPORAL_VALUES.md), not strings or host `datetime` objects.
Their Arrow representation is a **struct of primitive coordinates**, not Arrow's
narrow date/timestamp types. This preserves the full native year range, nanoseconds,
recorded per-row zones/offsets and independent duration components.

| Native type | Ordered Arrow struct fields |
| --- | --- |
| DATE | `epoch_day: int64` |
| LOCALTIME | `nanoseconds: int64` |
| TIME | `nanoseconds: int64`, `offset_seconds: int32` |
| LOCALDATETIME | `epoch_day: int64`, `nanoseconds: int64` |
| DATETIME | `epoch_seconds: int64`, `nanosecond: int32`, `offset_seconds: int32`, `zone: string` |
| DURATION | `months: int64`, `days: int64`, `seconds: int64`, `nanoseconds: int32` |

Every temporal field requires `grafx.type=<UPPERCASE TYPE>` and
`grafx.temporal=components-v1` metadata. Missing/mismatched/unknown tags refuse;
the same physical struct is not authority to infer a type. Field order, names and
integer widths are exact. Physical struct children are nullable for transport
portability, but **inside a non-NULL value only `zone` may be NULL**. Native decoding
checks ranges and canonical duration nanos (`0..999999999`) without normalization,
string parsing or zone lookup. A parent NULL remains a native NULL. The recorded
zone need not be installed or currently resolvable. TIMESTAMP retains its separate
microsecond-UTC contract; it is not substituted for DATETIME.
These coordinate structs are transport representations, not a promise that a
consumer's struct sorting/arithmetic reproduces Grafx temporal operators (especially
duration ordering or calendar arithmetic). Use the documented native semantics.

```python
from okto_grafx import QueryResult, DateValue
from okto_grafx.arrow import to_arrow_batches, import_arrow_batches

batches = list(to_arrow_batches(
    QueryResult(columns=("day",), rows=((DateValue(2024, 2, 29),), (None,))),
    types=("DATE",),
))
# Destination Event(day DATE) must already exist in catalog v2.
with db.begin() as tx:
    report = import_arrow_batches(tx, "CREATE (:Event {day:$day})", batches, types=("DATE",))
```

Late temporal metadata/component failures roll back the **whole import call**,
not just the last batch. Earlier caller staging remains subject to the normal
savepoint/transaction contract. Export charges 1,024 bytes per temporal cell
(including NULL), plus four bytes per DATETIME zone character, on top of the fixed
64-byte cell charge. Import uses the structured multiplier of **16 × batch.nbytes**
whenever a temporal or vector column is present, plus the usual fixed/row/column
charges. These are bounded logical workspace estimates, not allocator/RSS limits.

Pandas, Polars and Parquet use this same schema and native import validation; see
[tabular consumption](TABULAR_AND_PARQUET.md). Arbitrary nested property maps and
entity DTOs remain outside this typed scalar transport. This does not qualify
UDF declarations or materialized-view key types. Temporal CSV/JSONL/SQLite
consumption has a separate [tagged local-input contract](LOCAL_TEXT_IMPORT.md#native-temporal-fields-006-development).

### Exact native decimals (0.0.6 development)

`okto_grafx.arrow.ArrowDecimalType(precision, scale)` is an immutable descriptor
accepted in the `types` tuple by Arrow, Pandas, Polars and Parquet import/export.
Both fields are exact integers (bool refused), with `1 <= precision <= 38` and
`0 <= scale <= precision`. Constructing it does not import PyArrow or activate
storage. The physical type is `pyarrow.decimal128(precision, scale)`, with mandatory
field metadata `grafx.type=DECIMAL` and `grafx.decimal=decimal128-v1` (byte keys and
values). Precision/scale are authoritative in the physical type, not redundant
free-form metadata. External producers must explicitly attach this schema too.

Export requires exact native `DecimalValue` objects with **matching p/s**, even
when different declarations would represent the same number. Use an explicit
native `decimal(value,p,s)` query cast or `value.rescale(p,s)` first if changing
the declaration is intentional. INT64, DOUBLE, strings, host `decimal.Decimal`,
decimal256, dictionary encodings, missing/unknown tags and mismatched declarations
are not inferred. Whole-value NULL is supported; all-NULL input does not activate
the decimal storage capability. Empty materialized frames/files retain their schema;
empty Arrow export yields no batches, as before.

The boundary constructs/extracts a host Decimal's digit tuple without decimal
arithmetic. It never uses float, `quantize`, `scaleb` or the ambient decimal context;
38-digit coefficients, signs and trailing-zero scale remain exact. Native Grafx
codecs/arithmetic still do not depend on the host decimal module. Arrow's
[scaled-integer decimal type](https://arrow.apache.org/docs/python/generated/pyarrow.decimal128.html)
and Python's [exact tuple constructor](https://docs.python.org/3/library/decimal.html#decimal.Decimal)
define the external representations used here. Import validates each coefficient
against the declared precision, including malformed buffers that Arrow can expose.

```python
from okto_grafx import DecimalValue, QueryResult, connect
from okto_grafx.arrow import ArrowDecimalType, to_arrow_batches, import_arrow_batches

kinds = ("INT64", ArrowDecimalType(12, 4))
source = QueryResult(columns=("id", "amount"),
                     rows=((1, DecimalValue(1234500, 12, 4)), (2, None)))
batches = list(to_arrow_batches(source, types=kinds, batch_rows=1))
with connect(":memory:") as db:
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE Invoice(id INT64,amount DECIMAL(12,4),PRIMARY KEY(id))")
        imported = import_arrow_batches(tx,
            "CREATE(:Invoice {id:$id,amount:$amount})", batches, types=kinds)
        assert imported.statements == 2
    assert db.execute("MATCH(n:Invoice) RETURN n.amount ORDER BY n.id").rows == (
        (DecimalValue(1234500, 12, 4),), (None,))
```

Transport preserves its declared p/s. A differently declared **destination column**
may then apply normal exact native assignment: DECIMAL(3,2) 1.25 can enter
DECIMAL(12,4) as 1.2500, but a nonzero discarded digit or overflow refuses the whole
import call. ANY destinations retain the offered declaration. This is not rounding
or a per-batch commit. Prior caller statements, WAL/OCC, rollback and cursor ownership
are unchanged. Recovery after a proven-durable commit is native Grafx recovery;
an import report alone is not a durability receipt.

Each decimal export cell adds **1,024 logical bytes**, including NULL, on top of
the fixed 64-byte cell charge. Arrow import uses its structured multiplier
**16 × batch.nbytes** and adds **1,024 bytes per decimal cell** to fixed/row/column
charges. Pandas/Polars/Parquet batch/frame accounting uses the same extra cell
tariff, including for untagged physical input inspected before admission. Existing
batch/frame limits apply; no new connection option or storage format is introduced.
These are workspace estimates, not hard third-party allocator/RSS limits.

[Tabular metadata and file rules](TABULAR_AND_PARQUET.md) ·
[Native decimal contract](specs/DECIMAL_VALUES_V1.md) ·
[Qualification](reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md).

### Exact typed collections (0.0.6 development)

The [columnar collection contract](COLLECTION_COLUMNAR.md) documents collection-root
`StoredType` declarations across Arrow/Pandas/Polars/Parquet, native typed children,
exact descriptor metadata, NULL/empty distinctions, ANY payloads and additional
bounded conversion tariffs. Declared structure remains columnar; metadata loss,
inexact conversion and malformed late values refuse with whole-call rollback.
This is explicit native collection transport, not arbitrary nested/entity inference.

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
mapping. Untyped nested inference, graph entity export, zero-copy and external scans
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
