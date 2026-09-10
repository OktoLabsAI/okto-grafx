# Typed Pandas and local Parquet

Available in 0.0.5 development. These optional functions reuse the
[Arrow scalar/vector contract](EXTENSIONS_AND_ARROW.md), not a second query or
transaction engine. Install `okto-grafx[pandas]` for Pandas (Pandas >=2, PyArrow >=14),
or `okto-grafx[arrow]` for Parquet alone. Imports remain lazy; selecting an absent
dependency raises `GrafxUnsupportedOperation`. No connection option, environment
variable, required storage capability or database file format is added.

## Exact types and ownership

Every operation requires an ordered `types` tuple of 1..256 entries, matching all
unique nonempty column names (at most 256 characters). Scalars are BOOL, INT64,
DOUBLE, STRING, BYTES, TIMESTAMP (microseconds, UTC), UUID (16 bytes); vectors use
`ArrowVectorType(space_ref, dimension, dtype)`. No dtype inference, int32 widening,
timezone conversion, dictionary decoding, nested/entity guessing or cross-store
vector remapping. Scalar field metadata, when present, must agree; vector space,
dimension and precision metadata are mandatory. Query columns become named
statement parameters during import. Destination schema must already exist.

Scalar DOUBLE NaN is preserved as NaN, not NULL; vectors still reject non-finite or
NULL components through native admission. A vector itself may be NULL. Input
DataFrames/files must not be mutated concurrently. Returned frames/batches own
their Arrow buffers independently of database pages. Caller owns source cursors
and must close them even after export refusal; the helpers never close a cursor.

Both imports return `ExecuteManyReport` and stage through one native executemany
savepoint covering the entire call. A late producer, schema, quota or statement
failure discards this call while retaining earlier caller staging. They do not
commit, retry, split a large input into independent transactions or relax native
transaction/WAL limits. Caller owns the transaction and handles its commit outcome.

## Pandas API

`okto_grafx.tabular.to_pandas(source, *, types, batch_rows=256,
max_batch_bytes=16777216, max_rows=100000, max_bytes=67108864)` accepts a native
QueryResult or QueryCursor and returns one materialized Arrow-backed DataFrame,
including exact empty-result schema. This is not a streaming/zero-copy guarantee.
The immutable schema is attached as `frame.attrs["grafx.arrow_schema"]`.

`import_pandas(transaction, statement, frame, *, types, max_batch_rows=256,
max_batch_bytes=16777216, max_rows=1000000, max_batches=4096)` requires an exact
DataFrame with exact `pandas.ArrowDtype` columns. Ordinary NumPy/object-backed
DataFrames refuse rather than guessing. Convert explicitly in application code
only when that conversion preserves the desired NULL/NaN/type semantics. Index
values are ignored. Metadata attrs are checked when present; vectors require them.
Operations that discard Pandas attrs cannot silently reidentify vector spaces.

```python
from okto_grafx import QueryResult
from okto_grafx.tabular import to_pandas, import_pandas

rows = QueryResult(columns=("id", "title"), rows=((1, "alpha"), (2, None)))
frame = to_pandas(rows, types=("INT64", "STRING"), max_rows=100)
with db.begin() as writer:
    report = import_pandas(writer, "CREATE (:Document {id:$id,title:$title})",
                           frame, types=("INT64", "STRING"))
```

## Local Parquet API

Functions live in `okto_grafx.parquet`; complete signatures/DTOs are in the
[API reference](API_REFERENCE.md). `allowed_root` is mandatory and must be an
existing, trusted caller-owned local directory. `path` is a single filename or an
absolute path directly inside that root. No child directories, parent traversal,
URLs, UNC shares, alternate data streams, symlinks/reparse points or nonregular
files. Directory ancestors are checked too. No directories are created.

This policy is **not an OS security sandbox** against an adversary concurrently
renaming directory ancestors. Keep the directory namespace trusted and stable;
do not grant untrusted actors write access to it. Root identity is checked before
and after I/O. Reads also compare open-file size/mtime at completion; this detects
ordinary concurrent changes, not malicious same-size writes with restored metadata.

`read_parquet_batches(path, *, allowed_root, types, ...)` yields exact RecordBatch
objects. Wrap in `contextlib.closing` when stopping early. Earlier yielded batches
cannot be retracted if a later group fails. Schema/metadata and bounds are checked;
native value/target-space validation happens on import, not as a whole-file proof
from merely creating a lazy iterator.

`import_parquet(transaction, statement, path, *, allowed_root, types, ...)` owns
and closes its iterator and file on success/refusal. It uses the whole-call native
savepoint described above. It does not COPY directly into heap or bypass indexes.

`write_parquet(source, path, *, allowed_root, types, ...)` accepts a QueryResult or
QueryCursor and returns frozen `ParquetExportReport(path, rows, batches, bytes)`.
It writes an uncompressed temporary file in the root, closes its writer/footer,
checks final size and fsyncs the file, then publishes with an atomic no-replace
hard link. Existing destinations are always refused; there is no overwrite mode.
Unsupported filesystems fail closed, without a weaker publication fallback.
An export failure before publication leaves no partial final file. Ordinary cleanup
removes the temporary; cleanup failure is reported, and process termination can
leave an orphan `.grafx-parquet-*` file for operator review. A completed destination
may exist if cleanup fails after publication. Do not automatically retry over it.

File publication is atomic visibility, **not crash-durable directory publication**
or a database durability receipt. It is not a portable graph backup: relationships,
catalog, WAL and qualified commit identities are not inferred. For recovery use
[physical backup/restore](BACKUP_RESTORE.md), for graph transfer use
[logical transfer](LOGICAL_TRANSFER.md).

Nullable vectors are stored as variable lists with native vector metadata plus
`grafx.parquet.vector=list-v1`, then reconstructed and shape-checked as fixed-size
lists at the batch API. This avoids Arrow's nullable fixed-list Parquet read failure.
Unmarked variable lists and unknown encoding tags refuse; ordinary fixed-size-list
files are accepted when Arrow can decode them and their metadata matches. No NULL
vector is silently changed into a zero vector. Scalars keep their exact Arrow types.

```python
from okto_grafx import QueryResult
from okto_grafx.parquet import write_parquet, import_parquet

rows = QueryResult(columns=("id", "title"), rows=((3, "local"), (4, None)))
exported = write_parquet(rows, "documents.parquet", allowed_root=export_root,
                         types=("INT64", "STRING"), max_rows=100)
with db.begin() as writer:
    imported = import_parquet(writer, "CREATE (:Document {id:$id,title:$title})",
                               "documents.parquet", allowed_root=export_root,
                               types=("INT64", "STRING"), max_rows=100)
```

The examples assume an existing `Document(id INT64, title STRING, PRIMARY KEY(id))`
table and, for Parquet, an existing trusted `export_root` directory with no destination.

## Bounds and failure contract

All numeric bounds are exact positive integers, maximum 2^31 (bool refused), except
batch row limits which stop at 65,536. Defaults:

| Operation | Option | Default / scope |
| --- | --- | --- |
| All | types | Required; no guessing |
| Pandas export / Parquet export | batch_rows | 256 |
| Imports / Parquet reader | max_batch_rows | 256 |
| All | max_batch_bytes | 16 MiB logical batch workspace |
| Pandas export | max_rows / max_bytes | 100,000 / 64 MiB materialized frame |
| Imports / Parquet reader/export | max_rows / max_batches | 1,000,000 / 4,096 |
| Parquet | max_file_bytes | 256 MiB input size or completed output including footer |
| Parquet reader/import | max_row_group_bytes | 64 MiB declared uncompressed row-group size |
| Parquet | allowed_root | Required; no ambient default |

Batch tariff is 256 + 256C + 80RC + 16 × Arrow nbytes; Pandas export accumulates
this charge plus 4,096 fixed bytes. Export conversion also uses the existing Arrow
per-batch bound. Parquet row-group count is capped by max_batches in addition to
the number of emitted batches. Row-group sizes and total rows are checked from
metadata before decoding; Arrow thrift string/container metadata caps are fixed at
1,048,576 bytes and 100,000 elements. Batch logical size is checked after Arrow
decoding and again after vector normalization. These are **not hard native allocator
or RSS ceilings**; caller-owned inputs and third-party decoder temporaries are
outside the accounting. No time deadline or preemptible kernel claim is made.

Invalid API options raise `GrafxConfigurationError`; declared-type/path/dependency
refusals and Arrow/file failures raise `GrafxUnsupportedOperation`; logical limits
raise `GrafxQueryBudgetExceeded`. File failures preserve the original exception as
their cause. Native statement/transaction errors retain their existing types. No
silent truncation, commit retry, type coercion or overwriting is performed.

Underlying optional APIs: [Arrow Pandas integration](https://arrow.apache.org/docs/python/pandas.html)
and [ParquetFile batch reader](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html).

[Configuration routing](CONFIGURATION.md) · [Roadmap](../ROADMAP.md)
## Polars companion (continuation after a4dd85a)

Install `okto-grafx[polars]`. `okto_grafx.polars.to_polars` has the same explicit
`types`, `batch_rows=256`, `max_batch_bytes=16777216`, `max_rows=100000` and
`max_bytes=67108864` controls as `to_pandas`; it returns `PolarsFrame(frame,
arrow_schema)`, not a bare DataFrame. The frozen wrapper holds an eager Polars
DataFrame and its native Arrow metadata separately because Polars transformations
need not preserve field metadata. The DataFrame itself is mutable: do not modify
it concurrently with import or infer new vector identity from its contents.

`import_polars(transaction, statement, frame, types=..., max_batch_rows=256,
max_batch_bytes=16777216, max_rows=1000000, max_batches=4096)` requires that wrapper,
validates metadata/types, and stages the entire call with the native Arrow import
savepoint. It does not commit or retry. Exact float/integer/timestamp/vector dtypes
are required; safe conversion of Polars large string/binary offsets to native Arrow
offsets is explicit. UUID binary width is checked by the safe fixed-width cast.
No LazyFrame, inferred nested/vector semantics, arbitrary numeric coercion or zero-copy
promise. NULL and scalar NaN remain distinct. Limits are logical charges, not RSS.
Missing dependency, invalid options/types, native errors and budget refusal follow
the same categories as Pandas/Arrow. Caller owns source cursors and prior staging.

See upstream [Arrow export](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.to_arrow.html)
and [Arrow import](https://docs.pola.rs/api/python/stable/reference/api/polars.from_arrow.html).
An executable Polars example is in [the companion recipe](POLARS_RECIPE.md).
CSV/JSONL native staging has [its own exact local text contract](LOCAL_TEXT_IMPORT.md).
