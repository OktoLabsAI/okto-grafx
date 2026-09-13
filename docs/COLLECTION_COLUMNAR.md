# Typed collection columnar interchange

Available in the 0.0.6 development source. Declare a collection-root
`okto_grafx.StoredType` in the `types` tuple of the existing Arrow, Pandas, Polars
and Parquet APIs. Scalars still use their existing string/ArrowDecimalType/
ArrowVectorType declarations. No schema inference, connection option or new native
row/catalog/WAL format is introduced by this transport.

## API and ownership

The accepted collection roots are LIST, MAP, ARRAY and STRUCT. Their complete
recursive declarations come from the [native StoredType contract](specs/TYPED_COLLECTIONS_V1.md).
Every operation owns its descriptor before consuming rows: synchronous imports
copy it at call admission; generator exports/readers copy it on first iteration.
Changes before the first iteration are therefore input changes; changes after
admission cannot change later batches. Do not mutate inputs concurrently.

- `okto_grafx.arrow.to_arrow_batches` / `import_arrow_batches`: exact RecordBatch schemas.
- `okto_grafx.tabular.to_pandas` / `import_pandas`: exact ArrowDtype columns and mandatory `frame.attrs["grafx.arrow_schema"]`.
- `okto_grafx.polars.to_polars` / `import_polars`: eager PolarsFrame plus mandatory original Arrow schema.
- `okto_grafx.parquet.write_parquet`, `read_parquet_batches`, `import_parquet`: local explicit-root files, preserved schema metadata, atomic no-overwrite export.

[Full signatures/defaults](API_REFERENCE.md#module-okto_grafx-arrow) and
[frame/file limits](TABULAR_AND_PARQUET.md) remain authoritative. Import stages
through one executemany savepoint across **all** batches, never commits/retries,
and preserves prior caller statements on a proven rollback. Native destination
assignment can rescale a nested decimal exactly, but transport itself never
repairs missing STRUCT fields, changes precision/scale or fills required NULLs.
Independent readers/writers, snapshots, WAL and COMMIT authority are unchanged.

## Exact physical representation

| Declared native value | Physical Arrow/Pandas/Parquet representation | Polars normalization on import |
| --- | --- | --- |
| LIST<T> | `list<T>` | Large-list offsets may narrow safely; children checked recursively. |
| ARRAY<T,n> | `list<T>` plus exact `n` in mandatory descriptor | Same list representation; every non-NULL value must contain exactly `n` elements. |
| MAP<T> | `map<string,T>` | List of `{key,value}` entries is accepted only with the matching descriptor; duplicate/NULL keys or NULL entries refuse. |
| Nonempty STRUCT | Ordered named Arrow struct fields | Exact field names/order and recursively compatible physical types required. |
| Empty STRUCT | `struct<$empty: bool>`; non-NULL value has `$empty=true` | Same sentinel; false/NULL sentinel refuses. NULL parent remains NULL, not `{}`. |
| ANY leaf | Binary `native-value-v1` payload, or Arrow NULL | Binary offset width may change; payload must remain canonical and completely consumed. |
| DECIMAL leaf | Exact decimal128(p,s) | No scale/precision conversion, float or decimal256 substitution. |
| Temporal leaf | Existing exact components-v1 structs | Only documented offset-width/child-nullability differences, not coordinate changes. |
| Other scalar leaves | Existing BOOL/int64/float64/UTF-8/binary/UUID/timestamp types | No numeric narrowing/widening or timestamp timezone/unit conversion. |

ARRAY uses variable Arrow lists deliberately: nullable fixed-size lists and
zero-length arrays do not round-trip reliably across the tested libraries.
Its **native fixed-length semantics are not relaxed**; even very large declared
lengths do not reserve physical slots for NULL values. Empty STRUCT's private
sentinel avoids conflating empty structures with NULL or dropping columns in
Parquet. These choices apply recursively, not only to the outermost column.

Arrow's [map type](https://arrow.apache.org/docs/python/generated/pyarrow.map_.html)
provides explicit key/item fields. Polars documents that unsupported Arrow types
may be [converted to a supported representation](https://docs.pola.rs/api/python/stable/reference/api/polars.from_arrow.html);
Grafx accepts only the listed exact representation differences. In particular,
there is no generic `cast` from arbitrary structs/lists to a declared map. Parquet
[stores the Arrow schema in metadata](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html),
which is required to preserve Grafx's native descriptor identity.

Physical child fields are portable nullable fields. The full logical descriptor
is the authority for root/element/field nullability, ARRAY length and decimal
coordinates, and is checked against values after conversion. A NULL parent masks
its physical children; invisible child placeholders are not native values.

## Mandatory field metadata

All four fields must equal the explicit declared descriptor, including on all-NULL
columns and empty materialized frames/files:

| Key | Value |
| --- | --- |
| `grafx.type` | `LIST`, `MAP`, `ARRAY` or `STRUCT` |
| `grafx.collection` | `nested-v1` |
| `grafx.stored_type` | Lowercase ASCII hexadecimal of the canonical GXT1 descriptor |
| `grafx.any` | `native-value-v1` |

ASCII hexadecimal avoids feeding non-UTF-8 binary metadata into Polars' FFI while
preserving every descriptor byte. It is not a lossy JSON schema summary. Missing,
changed or unknown metadata refuses before native staging of that batch; earlier
batches in the same call are rolled back. Metadata is a schema declaration, not a
signature or proof of trusted source data.

Only ANY leaves are opaque binary. Known LIST/MAP/STRUCT/ARRAY structure and typed
leaves stay columnar. ANY retains all native scalar families, mixed lists and maps
with native hashable non-string keys. The payload is one canonical `encode_value`
value: trailing bytes, unknown tags, duplicate native map keys, embeddings,
nonfinite numbers and encoded NULL inside a non-NULL binary cell refuse. Use Arrow
NULL for ANY null. Ordinary user maps containing `type` or `coefficient` never
turn into a native decimal by inference. This protocol is not pickle and executes
no host code. [Exact JSON collection interchange](COLLECTION_JSON.md) is the
human-readable alternative; neither format is an arbitrary graph-entity protocol.

## Bounds and failures

Existing operation-local limits apply, especially `max_batch_bytes=16777216`,
row/batch bounds and frame/file bounds. No new connection setting exists.

Collection descriptors add **32 logical bytes per encoded GXT1 byte** to the batch
schema charge (equivalently 16 per hexadecimal metadata byte). Nested conversion
adds 128 per visited value/key occurrence, including NULL; repeated aliases are
charged repeatedly. Native export additionally charges 64 per string character,
16 per binary byte and 1,024 per decimal/temporal value (plus recorded zone text).
Import bounds visible string/binary buffers before materializing them, adds 1,024
per decimal/temporal leaf and validates exact native range/shape after conversion.
ANY import additionally reserves **128 per encoded payload byte before decoding**
and charges the decoded native tree. This prevents a small encoded list of NULLs
from bypassing the logical allocation budget. Polars map reconstruction runs
under the same bounded conversion rules before native import. Its initial
series-to-Arrow pass charges 128 per visited schema node, 128 per series row and
16 times its estimated buffer bytes before filtering/copying. LIST/STRUCT buffers
are reconstructed recursively from shallow leaves, avoiding the C Data interface's
schema-recursion ceiling without converting wide timestamps through host dates.

These conservative operation-workspace estimates are not hard RSS ceilings,
third-party decoder limits, global transaction quotas or streaming materialization
promises. A budget error never authorizes partial commits. Large source batches
may need smaller batch sizes or a deliberately larger existing byte limit.
StoredType's depth/size bounds remain unchanged. Invalid native types/shape,
metadata or canonical payloads raise typed Grafx errors; budget failures raise
`GrafxQueryBudgetExceeded`. No silent stringification, empty substitution or
lossy data repair is allowed.

## Executable example

```python
from okto_grafx import StoredType, DecimalValue, QueryResult, connect
from okto_grafx.arrow import to_arrow_batches, import_arrow_batches

amounts = StoredType("MAP", element=StoredType("DECIMAL", precision=12, scale=4))
types = ("INT64", amounts)
rows = QueryResult(columns=("id", "amounts"),
                   rows=((1, {"price": DecimalValue(12500, 12, 4)}), (2, None)))
batches = list(to_arrow_batches(rows, types=types, batch_rows=1))
with connect(":memory:") as db:
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE Invoice(id INT64,amounts MAP<DECIMAL(12,4)>,PRIMARY KEY(id))")
        report = import_arrow_batches(tx,
            "CREATE(:Invoice {id:$id,amounts:$amounts})", batches, types=types)
        assert report.statements == 2
    assert db.execute("MATCH(n:Invoice) RETURN n.id,n.amounts ORDER BY n.id").rows == rows.rows
```

To use a frame or local file, pass the same `types` to the corresponding functions
listed above. Keep the metadata wrapper/attrs with the frame. Do not strip schema
metadata when moving Parquet files between applications. No native storage
capability is activated merely by exporting values or importing all-NULL columns
into an existing ANY column; typed destination DDL follows its own native rules.

The [consolidated support matrix](TYPE_SUPPORT.md) and
[installed old/new-reader qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
now cover this type package. Final full-profile/Pulse acceptance remains separate.

[Local tests, fixes, exact versions and receipts](reports/FP6_TYPED_COLLECTION_COLUMNAR_QUALIFICATION.md).
