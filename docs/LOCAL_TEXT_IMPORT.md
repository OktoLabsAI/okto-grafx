# Bounded local CSV and JSON Lines

0.0.5 development, continuation after `a4dd85a`. Pure-Python scalar ingestion uses
one existing native `Transaction.executemany` staging savepoint. No Arrow dependency,
external query scans, COPY syntax, schema inference, remote sources or new WAL path.
The explicit statement must be an updating statement supported by `executemany`.

## Consumption and atomicity

`read_csv_batches` / `read_jsonl_batches` take a path plus required `allowed_root`,
`columns` and `types`; they yield tuples of named-parameter dictionaries. Columns must
be an exact tuple of 1..256 distinct nonempty names (each <=256 characters), and types
an equally sized tuple. Names are parameters, not automatically interpolated into DDL.
Readers are lazy: validation begins on iteration. Always close on early exit:

```python
from contextlib import closing
from okto_grafx.text_import import read_csv_batches, TextImportLimits

# `input_root` is a trusted existing local directory; input.csv has header id,title.
with closing(read_csv_batches("input.csv", allowed_root=input_root,
        columns=("id", "title"), types=("INT64", "STRING"),
        limits=TextImportLimits(batch_rows=64))) as reader:
    row_count = sum(len(batch) for batch in reader)
```

`import_csv` / `import_jsonl` additionally take `(transaction, statement, path)`.
They consume the entire file in one native savepoint, close on success/error, and
return `ExecuteManyReport`. A malformed later row, bound, cancellation, source-change
check or native conflict rolls back **this call**, preserving prior transaction
staging. The caller owns commit, transaction lifetime, retries and uncertain durable
outcomes. Batching does not split the call into independently committed transactions;
native transaction quotas can refuse before file row limits are reached.

```python
from okto_grafx.text_import import import_csv, import_jsonl

with db.begin() as tx:
    csv_report = import_csv(tx, "CREATE (:Document {id:$id,title:$title})",
        "input.csv", allowed_root=input_root,
        columns=("id", "title"), types=("INT64", "STRING"))
with db.begin() as tx:
    json_report = import_jsonl(tx, "CREATE (:Document {id:$id,title:$title})",
        "input.jsonl", allowed_root=input_root,
        columns=("id", "title"), types=("INT64", "STRING"))
```

Direct readers cannot revoke batches already consumed when a later error occurs.
Use the import facade when whole-call native staging atomicity is required.

## Formats and exact codecs

CSV requires UTF-8 without BOM and an exact ordered header; empty files/incorrect
headers refuse, header-only files yield zero rows. Default delimiter is comma;
one ASCII character other than double quote, CR/LF/NUL may be selected. Quotes are
double quotes with doubled-quote escaping, no escape character, no whitespace
trimming. Quoted multiline fields preserve their literal LF/CRLF. Blank records
do not silently disappear. The standard-library strict CSV dialect is used; this is
not a claim of rejecting every non-RFC form accepted by that dialect.

`null_token="\\N"` is a required nonempty configurable token, limited by
`max_field_bytes`. It maps to NULL after CSV decoding, **even when quoted**. Therefore
that exact literal cannot be represented as a string with this token; choose another
token if needed. Empty STRING is empty, not NULL; empty non-string fields refuse.

JSONL requires one complete scalar-valued object per UTF-8 physical line. Empty file
is allowed; blank lines, arrays, nesting, duplicate keys, unknown keys and missing keys
refuse. Key order may differ; explicit JSON `null` is NULL and is distinct from a
missing key. Nonstandard NaN/Infinity tokens and nonfinite numeric overflow refuse.

| Declared type | CSV representation | JSONL representation |
| --- | --- | --- |
| BOOL | Exactly `true` / `false` | JSON boolean, not number |
| INT64 | Decimal signed integer, no plus/leading zeros | JSON integer within signed 64-bit range; booleans/floats refuse |
| DOUBLE | Finite decimal with optional fraction/exponent | Finite number; integer-to-float conversion must preserve the integer exactly |
| STRING | Literal decoded text | JSON string |
| BYTES | Validated Base64 | Base64 string |
| UUID | Canonical lowercase hyphenated UUID | Same string |
| TIMESTAMP | Signed int64 microseconds since Unix epoch | JSON int64 microseconds, not ISO text |

DOUBLE decimal text uses float64 rounding, not decimal arbitrary precision; CSV
DOUBLE integral text has the same decimal-to-float interpretation. Scalar DOUBLE
NaN is valid in the native database/Arrow but intentionally absent from these text
formats. `BYTES` is the ingestion descriptor; **DDL names that type `BLOB`**.
Vectors, nested values and entity objects are outside this slice.

## Limits, errors and file policy

All functions accept `limits=TextImportLimits()` and `cancellation=None`.

| TextImportLimits field | Default | Meaning |
| --- | ---: | --- |
| `batch_rows` | 256 | Maximum dictionaries per batch; <=65,536 |
| `max_batch_bytes` | 16 MiB | Logical decoded batch, not RSS |
| `max_rows` | 1,000,000 | Data rows, excluding CSV header |
| `max_batches` | 4,096 | Nonempty batches yielded |
| `max_file_bytes` | 256 MiB | Initial file size and actual bytes read |
| `max_record_bytes` | 1 MiB | Physical line and aggregate multiline CSV record, including line endings |
| `max_field_bytes` | 65,536 | UTF-8 field length; configurable only downwards |
| `max_work` | 10,000,000 | Physical-line and row/column observations |

All are positive exact integers (bool refused), <=2^31 except the two <=65,536
fields. Decoded batch tariff: 4,096 + 512 per field + 16 times native encoded value
bytes. File/record caps apply before unbounded line allocation; field/batch limits
apply after parsing the bounded record. JSON/CSV parser internals are not preemptible
or RSS-capped. No process-global CSV field limit is modified; an application that
lowers that interpreter-global limit can cause an additional typed CSV refusal.

The same [trusted local-directory policy](TABULAR_AND_PARQUET.md) as Parquet applies:
one direct regular child, explicit existing root, no links/reparse paths, URL/UNC,
ADS, globbing or recursive discovery. Namespace must remain trusted and stable;
this is **not a sandbox against hostile directory races**. Reads validate the opened
file, root identity and final size/mtime/identity; detected changes refuse. A writer
that evades metadata checks is not excluded by a file-content snapshot guarantee.

Invalid options raise `GrafxConfigurationError`; malformed input, denied paths,
changed files, I/O/UTF-8 failures raise `GrafxUnsupportedOperation` with localized
row/column (CSV parser failures have physical line). Native transaction errors remain
native. Budget refusal is `GrafxQueryBudgetExceeded`; cancellation is
`GrafxQueryCancelled` at cooperative line/field boundaries, not interruption of OS
I/O, an executing statement or a durable commit. No automatic retry.

[API reference](API_REFERENCE.md) · [Configuration](CONFIGURATION.md) · [Roadmap](../ROADMAP.md)
