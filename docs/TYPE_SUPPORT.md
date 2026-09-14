# Native type support matrix

This is the consolidated FP-5/FP-6 support/refusal contract for **0.0.6** (published on
PyPI on September 13, 2026), not a declaration of full Cypher/TCK parity. The tables
distinguish native storage, query values, callback signatures
and external transport: support in one does not imply support in every other one.
Existing operation/transaction bounds, explicit permissions and storage capability
admission remain applicable to every supported cell.

## Type inventory and ownership

| Family | Declaration / native Python value | Important boundary |
| --- | --- | --- |
| Existing scalars | BOOL/bool, INT64/int, DOUBLE/float, STRING/str, BLOB/bytes, UUID/Uuid, TIMESTAMP/Timestamp | Exact native families/ranges. TIMESTAMP is a microsecond UTC instant, not the new local/zoned temporal family. External APIs spell the binary declaration BYTES. |
| DATE | DATE / DateValue | Native calendar date; expanded years, no implicit host datetime range restriction. |
| LOCALTIME | LOCALTIME / LocalTimeValue | Nanoseconds within a day; no implicit zone. |
| TIME | TIME / TimeValue | Local time plus explicit signed UTC offset. |
| LOCALDATETIME | LOCALDATETIME / LocalDateTimeValue | Native date plus local nanosecond time. |
| DATETIME | DATETIME / DateTimeValue | Instant/nanoseconds plus recorded offset and optional zone identity. |
| DURATION | DURATION / DurationValue | Months/days remain distinct from elapsed seconds/nanoseconds. |
| DECIMAL | DECIMAL(p,s) / DecimalValue | 1 ≤ p ≤ 38; 0 ≤ s ≤ p; exact coefficient, precision and scale. |
| Heterogeneous properties | ANY / concrete native scalar, list or map values | Persisted values retain concrete tags. No nonfinite stored numbers or embeddings inside ANY. ANY is not a native scalar tag or a promise of every host object. |
| Typed LIST | LIST<T> / tuple or list on assignment | Recursively declared element type/nullability; stored/read values are detached native containers. |
| Typed MAP | MAP<T> / string-keyed mapping | Recursively declared value type/nullability. Non-string keys belong only inside a native ANY value, not the declared MAP key domain. |
| Typed ARRAY | ARRAY<T,n> / tuple or list on assignment | Exact fixed element count, including n=0. Length is a constraint, not an allocation reservation. |
| Typed STRUCT | STRUCT<name:T,...> / mapping | Ordered declared fields; empty structures supported. Assignment may fill missing nullable fields; strict stored/interchange reads never repair missing fields. |
| Vectors | VECTOR(space) / VectorValue or supported native assignment input | Store-local space identity, dimension and float32/float64 precision; not nested StoredType leaves. |
| Existing generic LIST/MAP | Existing native codec families without a StoredType descriptor | Do not infer element/nullability contracts from these tags. Use an explicit matching StoredType when selecting typed external transport. |

`StoredType` describes schema, not a wrapper around every returned value. ARRAY
and STRUCT reuse native LIST/MAP value encodings; `ColumnDef.stored_type` preserves
the distinction, full field/element schema, nullability and decimal coordinates.
Values/descriptors are owned at the documented admission boundaries. Mutating a
returned observation never grants engine/catalog/snapshot authority.

[Query and parameter mapping](QUERY_LANGUAGE.md#values-and-python-mapping) ·
[temporal semantics](TEMPORAL_VALUES.md) · [decimal semantics](specs/DECIMAL_VALUES_V1.md) ·
[collection semantics](specs/TYPED_COLLECTIONS_V1.md).

## Keys and indexes for the expanded families

| Column family | Primary key / typed equality indexes | Ordered/range index | Full-text / vector index |
| --- | --- | --- | --- |
| Each of the six native temporal types | Supported PK and hash/sparse_hash/posting_hash, including composite keys | Refused; the existing ordered codec accepts only a TIMESTAMP-then-STRING pair | Refused; FTS requires declared STRING and vector search requires a typed vector space |
| DECIMAL(p,s) | Supported PK and typed hash/sparse_hash/posting_hash. Equality probes normalize exactly to the column's p/s; inexact probes cannot match | Refused; query ORDER BY does not imply an ordered index | Refused |
| ANY, including a temporal/decimal value inside ANY | Primary/secondary property indexes refused; use a separate typed key column | Refused | No implicit full-text/vector index over heterogeneous properties |
| Typed LIST/MAP/ARRAY/STRUCT | PK and custom property hash indexes refused | Refused | Refused; no arbitrary nested-element index is implied |

These are **definition-time refusals**, not missing values hidden by an empty
index result. Record-identity/endpoint indexes and supported indexes on other
scalar columns remain usable on the same table. Primary-key constraints apply to
node tables; relationship tables use their native relationship/endpoint identity.

Temporal key identity retains recorded coordinates, offset and zone; equal instants
with different recorded representations can therefore be different keys. Decimal
numeric query equality ignores trailing-zero representation while storage and
transport preserve p/s. BOOL is not a number for decimal comparison. Explicit
normalization should match the consuming application's uniqueness semantics.

The existing scalar/compound/string/vector index contracts are indexed in
[Indexes and vectors](INDEXES_AND_VECTORS.md). This expansion does not generalize
the closed ordered index codec into arbitrary scalar range indexing.

## Query and trusted extension consumption

| Family | Parameters / results / native writes | TabularProcedure arguments and outputs | ScalarFunction signatures |
| --- | --- | --- | --- |
| Existing seven scalar families | Supported under their native parameter/assignment contract | Exact scalar signatures; DOUBLE additionally admits the documented INT64 widening; NUMBER preserves int/float/decimal family | Existing BOOL/INT64/DOUBLE/STRING/BYTES/TIMESTAMP/UUID set only |
| Six temporal families | Native constructors, values, parameters, returned properties and stored writes | Native family names, with owned values, recorded coordinates and shared invocation budgets | Not added to the scalar-UDF signature set |
| DECIMAL | Native DecimalValue, explicit decimal(...) conversions, numeric operators/aggregates and exact assignment | DECIMAL and NUMBER; signatures are not DECIMAL(p,s) column declarations | Not added |
| Typed LIST/ARRAY | Native containers with normal query list semantics; destination column validates the descriptor | LIST or ANY; no parameterized StoredType signature or fixed-length promise at the callback boundary | Not added |
| Typed MAP/STRUCT | Native mappings with normal property/map expressions; destination validates schema | MAP for string-keyed maps or ANY; field sets/nullability belong to the destination schema | Not added |
| ANY / generic containers | Concrete native value families and nested query values; not arbitrary host-object coercion | ANY/LIST/MAP as documented; read/write graph authority remains separate | Not added |
| Vectors | Existing native vector parameter/result/assignment contract | VECTOR_F32 / VECTOR_F64 with native validation | Not added |

NODE, RELATIONSHIP, PATH and entity-list procedure signatures are **query entity
references**, not persistable property-column types or arbitrary foreign entity
handles. Writing procedures use the same outer transaction and require explicit
permissions; they do not acquire commit, cross-store or filesystem rollback authority.
See [procedure contracts](EXTENSIONS_AND_ARROW.md), [entity values](ENTITY_VALUES.md)
and [composable query semantics](COMPOSABLE_QUERIES.md).

Procedure MAP and ANY mappings remain **string-keyed recursively**, including
mappings nested in LIST. Broader native ANY storage support for non-string keys
does not imply callback support for those keys. No parameterized StoredType or
additional scalar-UDF signature family is introduced by the transport expansion.

Expression NaN is supported where specified by the query language; NaN/infinity
cannot become stored graph properties, including nested values. There is no
implicit lossy Decimal-to-DOUBLE promotion or host decimal-context arithmetic.

## External consumption and exactness

| Value family | Arrow / Pandas / Polars / Parquet | CSV / JSONL / SQLite readers | CLI JSON and exact data movement |
| --- | --- | --- | --- |
| Existing seven scalars | Explicit scalar names; exact physical types and existing NULL/vector-independent rules | Explicit supported scalar declarations and documented file-cell grammar | CLI is a bounded observation; choose native logical transfer for a full exact graph |
| Six temporal families | Explicit native type names, exact component structs and mandatory temporal metadata | Explicit family name and exact tagged component object/text; SQLite cells use TEXT or SQL NULL | CLI emits temporal tags; native logical transfer/history preserve native values and schemas |
| DECIMAL | ArrowDecimalType(p,s), mandatory decimal128-v1 metadata, exact p/s | `types=("DECIMAL",...)`, canonical coefficient/p/s tag object or its JSON text; not SQLite REAL or inferred numeric strings | CLI emits exact decimal tags; native graph transfer preserves column coordinates |
| Typed LIST/MAP/ARRAY/STRUCT | Collection-root StoredType, nested-v1 metadata and complete descriptor; typed structure remains columnar, ANY leaves use canonical binary | Collection-root StoredType and [exact collection JSON grammar](COLLECTION_JSON.md); CSV/SQLite use JSON TEXT, JSONL also accepts structured values | Schema JSON carries stored_type. Use collection_json_value/from_json_value or native graph transfer for exact data, not the CLI observation protocol |
| ANY / generic unparameterized values | No direct mixed top-level ANY declaration. Explicitly declare a matching scalar/collection shape, or use a typed wrapper/logical transfer | No automatic ANY inference. Choose an explicit matching supported declaration; mixed nested values use StoredType ANY leaves | Native logical transfer preserves concrete tags; CLI observations do not promise exact non-string map keys or unrestricted depth |
| Vectors | ArrowVectorType(space_ref,dimension,dtype) and mandatory identity metadata | Not inferred by the bounded scalar/collection text readers | Use the existing vector/graph transfer contracts for space identity and mapping |

All external declarations describe the **offered source value**, not a request to
repair a malformed source or redefine the destination schema. A native target may
apply its documented exact assignment (for example exact decimal rescaling).
Unknown metadata, missing STRUCT fields, wrong ARRAY length, duplicate map keys,
nonfinite stored values and malformed typed payloads refuse; an import's whole-call
savepoint rolls back preceding batches on a late failure.

Pandas requires ArrowDtype and retained schema attrs; Polars requires PolarsFrame
metadata and only the documented representation normalizations; Parquet requires
an explicit allowed root and preserves schema metadata. The collection
[physical representation/bounds](COLLECTION_COLUMNAR.md), [scalar/temporal/decimal
Arrow contract](EXTENSIONS_AND_ARROW.md), [frame/file contracts](TABULAR_AND_PARQUET.md),
[text](LOCAL_TEXT_IMPORT.md), [SQLite](LOCAL_SQLITE_IMPORT.md) and
[CLI limitations](CLI.md) specify exact APIs and defaults.

## Durability, history, copy and transport

| Route | New temporal, DECIMAL and typed collection support | Boundary |
| --- | --- | --- |
| Native create/update/rollback/commit/reopen/verify | Implemented through native row/catalog/WAL admission and pure/NumPy codecs | New capabilities must be known before storage effects; transient expression values do not authorize stored nonfinite values |
| System-time history | Native values and historical schema metadata preserved, including nullable-column evolution and delete/recreate lineage | Explicit activation, retention and TemporalLimits; not implicit unlimited history |
| Physical backup/restore | Retained native database state, including enabled history and schema descriptors | Recovery accepts only its proven commit/durability protocol; backup artifacts are not downgraded layouts |
| Catalog copy/promotion | Complete source schema/coordinates in captured package, digest and target compatibility checks | Current state only; source history is not copied; receipt/provenance rules remain explicit |
| Logical export/import/resume | Typed schema descriptors and canonical native values preserved in supported artifact forms | Current-state graph transfer, fresh destination and endpoint mapping; no automatic history transfer or lossy conversion |
| Old binary encountering new layout | Must refuse safely according to required capability and checkpoint consistency | Installed old/new-reader evidence is checkpoint C, not inferred from source tests |

Stored native temporal values use tags 12–17 and capability bit 23; DECIMAL uses
tag 18 and bit 26. Typed collection schema uses marker 252 plus a complete GXT1
descriptor and bit 27, while row containers reuse LIST/MAP tags 6/7. Nested ANY,
temporal or decimal declarations additionally require their own capabilities
(bits 20, 23 or 26), even for empty typed tables. These bits are durable metadata,
not new user tuning flags. Activate catalog v2 explicitly as documented before
using these types; drain and upgrade all participants before format activation.

[Compatibility/upgrade](V006_COMPATIBILITY.md) · [history](SYSTEM_TIME_HISTORY.md) ·
[copy](CATALOG_COPY.md) · [logical transfer](LOGICAL_TRANSFER.md) ·
[recovery/backup operations](OPERATIONS.md).

## Evidence and remaining acceptance

The individual support cells are backed by the linked native, numeric/index,
consumer, procedure, text and columnar contracts and their qualification receipts:
[temporal](specs/TEMPORAL_VALUES_V1.md),
[decimal](specs/DECIMAL_VALUES_V1.md),
[native collections](reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md),
[collection text](reports/FP6_TYPED_COLLECTION_TEXT_QUALIFICATION.md),
[collection columnar](reports/FP6_TYPED_COLLECTION_COLUMNAR_QUALIFICATION.md).
The [60-case installed type checkpoint C](reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
now qualifies the combined type package, exact old refusal and current recovery.
This matrix does not turn an unexecuted platform/Pulse/full-profile check into a
pass. The subsequent [final native/profile reconciliation](reports/FP_FINAL_NATIVE_QUALIFICATION.md)
and [installed Pulse qualification](reports/FP_FINAL_PULSE_QUALIFICATION.md) now
record that evidence; Neo4j execution is separately deferred by user decision.
