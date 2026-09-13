# Bounded local CSV and JSON Lines

Originally 0.0.5 development, continuation after `a4dd85a`; 0.0.6 adds
[explicit collection JSON](COLLECTION_JSON.md) through StoredType declarations.
Pure-Python ingestion uses
one existing native `Transaction.executemany` staging savepoint. No Arrow dependency,
external query scans, COPY syntax, schema inference, remote sources or new WAL path.
The explicit statement must be an updating statement supported by `executemany`.

## Consumption and atomicity

`read_csv_batches` / `read_jsonl_batches` take a path plus required `allowed_root`,
`columns` and `types`; they yield tuples of named-parameter dictionaries. Columns must
be an exact tuple of 1..256 distinct nonempty names (each <=256 characters), and types
an equally sized tuple of scalar names or collection-root StoredType declarations.
Descriptors are validated and privately cloned at admission. Names are parameters,
not automatically interpolated into DDL.
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

JSONL requires one complete typed row object per UTF-8 physical line. Empty file
is allowed; blank lines, top-level arrays, undeclared nesting, duplicate keys, unknown keys and missing keys
refuse. Key order may differ; explicit JSON `null` is NULL and is distinct from a
missing key. Nonstandard NaN/Infinity tokens and nonfinite numeric overflow refuse.
Nested values require explicitly declared temporal/DECIMAL tags below or
[StoredType collection schemas](COLLECTION_JSON.md); no MAP/LIST/entity inference
is performed. Collection processing additionally charges max_work by native encoded
value bytes. The same whole-call rollback and source identity rules apply.

| Declared type | CSV representation | JSONL representation |
| --- | --- | --- |
| BOOL | Exactly `true` / `false` | JSON boolean, not number |
| INT64 | Decimal signed integer, no plus/leading zeros | JSON integer within signed 64-bit range; booleans/floats refuse |
| DOUBLE | Finite decimal with optional fraction/exponent | Finite number; integer-to-float conversion must preserve the integer exactly |
| STRING | Literal decoded text | JSON string |
| BYTES | Validated Base64 | Base64 string |
| UUID | Canonical lowercase hyphenated UUID | Same string |
| TIMESTAMP | Signed int64 microseconds since Unix epoch | JSON int64 microseconds, not ISO text |
| DECIMAL | Canonical decimal tag object encoded as a quoted CSV cell | Decimal tag object, or a string containing that JSON object |
| DATE / LOCALTIME / TIME / LOCALDATETIME / DATETIME / DURATION | JSON temporal tag object encoded as a quoted CSV cell | Temporal tag object, or a string containing that JSON object |

DOUBLE decimal text uses float64 rounding, not decimal arbitrary precision; CSV
DOUBLE integral text has the same decimal-to-float interpretation. Scalar DOUBLE
NaN is valid in native expressions/transient Arrow, **not stored properties**, and absent from these text
formats. `BYTES` is the ingestion descriptor; **DDL names that type `BLOB`**.
Vectors, arbitrary nested values and entity objects are outside this slice.

### Native temporal fields (0.0.6 development)

Declare a temporal type in `types`; it is not inferred from a string or tag. The
tag must match that declaration. These are the same explicit temporal tags used by
[entity/CLI observations](ENTITY_VALUES.md#json-grammar), not query constructor maps:

| Type | Exact JSON object fields (in addition to `type`) |
| --- | --- |
| DATE | `type="date"`; `epoch_day` decimal string |
| LOCALTIME | `type="localtime"`; `nanoseconds` decimal string |
| TIME | `type="time"`; `nanoseconds` decimal string, `offset_seconds` integer |
| LOCALDATETIME | `type="localdatetime"`; `epoch_day` and `nanoseconds` decimal strings |
| DATETIME | `type="datetime"`; `epoch_seconds` decimal string, `nanosecond` and `offset_seconds` integers, `zone` string or NULL |
| DURATION | `type="duration"`; `months`, `days`, `seconds` decimal strings, `nanoseconds` integer |

All fields are required, including `zone` even when NULL. No extra fields are
accepted. Decimal strings use ASCII `0` or signed nonzero digits, at most 19 digits,
without `+`, leading zeros, negative zero, whitespace, decimal point or exponent.
Native ranges apply after decoding. Required integer fields do not accept booleans,
floats or strings; duration nanos must already be normalized to `0..999999999`.
Only `zone` may be NULL inside an object; NULL values use the existing CSV token
or JSON null. An encoded string `"null"` is not a native NULL temporal object.

Years, nanos and recorded offsets/zones survive exactly, without host datetime
conversion or timezone lookup. A zone need not exist in the installed rule package.
Bare ISO text is deliberately not auto-parsed: use a declared STRING and an explicit
query constructor when construction/rule resolution is wanted. Importing tagged
values preserves recorded data rather than reconstructing it under current rules.

Example JSONL row for `columns=("id","day")`, `types=("INT64","DATE")`:

```json
{"id":1,"day":{"type":"date","epoch_day":"19782"}}
```

Equivalent CSV:

```csv
id,day
1,"{""type"":""date"",""epoch_day"":""19782""}"
```

With an existing `Event(id INT64,day DATE,PRIMARY KEY(id))` table in catalog v2,
use `import_jsonl(tx, "CREATE(:Event {id:$id,day:$day})", path,
allowed_root=input_root, columns=("id","day"), types=("INT64","DATE"))`.
Native admission and the existing whole-call savepoint remain mandatory. Duplicate
keys and nonfinite JSON constants are rejected even inside encoded text cells.
For object-valued JSONL fields, `max_field_bytes` also bounds the compact UTF-8 JSON
representation after shape validation; record limits already bound parser input.

### Native DECIMAL fields (0.0.6 development)

Declare `types=("INT64","DECIMAL")` for `columns=("id","amount")`. The input
descriptor is the native family `DECIMAL`, **not** the target DDL `DECIMAL(p,s)`.
Each cell carries its own exact type parameters. Example JSONL:

```json
{"id":1,"amount":{"type":"decimal","coefficient":"1234500","precision":12,"scale":4}}
```

This is exact **123.4500**, not 1,234,500. The object has exactly these four fields:
lowercase `type="decimal"`, string `coefficient`, integer `precision` and integer
`scale`. Coefficient spelling is ASCII `0` or signed nonzero digits, at most 38
digits, with no plus, leading zeros, negative zero, whitespace, decimal point or
exponent. `1 <= precision <= 38`, `0 <= scale <= precision` and the coefficient
must fit that precision. BOOL/float/string parameters, numeric coefficients,
extra/missing/duplicate fields and NULL children refuse. Whole-value NULL uses
JSON null or the existing CSV null token; encoded text `null` is not a decimal.

CSV cells contain the same JSON object, with the usual CSV quote escaping. No
bare decimal-text, JSON number or host `decimal.Decimal` inference occurs. Readers
return native `DecimalValue` objects preserving coefficient/p/s. Use declared
STRING plus an explicit query `decimal(...)` when parsing decimal-number text is
intended. Import into a typed target applies the normal **exact assignment** rule:
its declared p/s are retained, rescaling must be exact and overflow/inexact values
refuse. ANY targets keep the offered native metadata.

Existing `max_field_bytes` applies to encoded text and object-valued JSONL cells;
canonical shape is checked before serializing objects for size accounting. A late
bad field or target assignment rolls back the entire import call while preserving
earlier caller statements. No commit, retry, schema creation or new setting is
added. [Decimal interface qualification](reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md).

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
