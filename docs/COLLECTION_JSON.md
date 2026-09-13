# Exact collection JSON (0.0.6 development)

Implemented and locally tested: [506 grouped tests plus 2,474 public/transaction/documentation checks](reports/FP6_TYPED_COLLECTION_TEXT_QUALIFICATION.md).

[Documentation index](README.md) · [Typed storage](specs/TYPED_COLLECTIONS_V1.md) ·
[CSV/JSONL](LOCAL_TEXT_IMPORT.md) · [SQLite](LOCAL_SQLITE_IMPORT.md)

This transport preserves a declared LIST/MAP/ARRAY/STRUCT value as JSON primitives.
It does not stringify persisted collections, guess schemas, create tables or open
a graph. Native rows still use the original LIST/MAP codecs. Arrow/Pandas/Polars/
Parquet collection transport and installed-reader qualification remain separate work.

## APIs and ownership

Import from `okto_grafx.collection_json`:

- `collection_json_value(descriptor, value, *, max_bytes=65536)` returns owned JSON
  primitives from a native collection already satisfying its StoredType.
- `collection_from_json_value(descriptor, value, *, max_bytes=65536)` returns an
  owned tuple/map/None from JSON primitives, not JSON text.

Both require a collection-root `okto_grafx.StoredType`. Invalid descriptors raise
`GrafxConfigurationError`; malformed values raise a native schema error.
File importers localize malformed cells as typed `GrafxUnsupportedOperation`
errors. Budget failures raise `GrafxQueryBudgetExceeded`.
The public decoder assumes its caller's JSON parser rejects duplicate object
keys and nonfinite constants; native file readers enforce this themselves.

`max_bytes` is an exact integer 1..2^31. It bounds compact UTF-8 JSON size,
traversed occurrences and accumulated text. Native value depth remains 64;
the JSON tree has a bounded allowance for explicit ANY wrappers. Binary leaves
are cumulatively charged before Base64 allocation. These are bounded materialized
values, not a process-RSS promise. File readers pass their existing
`max_field_bytes`; no connection knob is added.

## Exact representation

| Declared nested family | JSON representation |
| --- | --- |
| LIST / ARRAY | JSON array of recursively typed elements; ARRAY length must match |
| MAP | JSON object with string keys and typed values |
| STRUCT | JSON object with exactly the declared fields; nullable fields must be present, possibly null |
| INT64 / TIMESTAMP | Canonical signed-int64 decimal **string**, including inside ANY |
| BOOL / STRING | JSON boolean/string without host-class or string coercion |
| DOUBLE | Finite JSON number; integer inputs only when conversion is exact |
| BYTES | Canonical Base64 string |
| UUID | Canonical lowercase hyphenated string |
| DECIMAL / temporal | Existing exact native tag objects, with p/s and temporal coordinates preserved |
| ANY | Null directly; otherwise explicit scalar/list/map tags below |

INT64 strings avoid loss through JSON consumers whose numbers cannot exactly
represent every signed 64-bit integer. Scalar `types=("INT64",)` in the older
flat text readers retains its existing numeric representation; this table governs
leaves inside the new collection transport.

Known MAP/STRUCT fields are interpreted by their descriptor. ANY never treats
an ordinary user map as a scalar merely because it contains a `type` field:

```json
{"type":"list","value":[{"type":"int64","value":"1"},{"type":"string","value":"1"}]}
```

Dynamic scalar tags use lowercase `bool`, `int64`, `double`, `string`, `bytes`,
`uuid` or `timestamp`, with a `value` member. DECIMAL/temporals retain their
existing complete tagged objects. Dynamic lists use `type: list` and `value`.
Dynamic maps use `{"type":"map","entries":[[tagged_key,tagged_value],...]}`.
Entry pairs preserve native non-string keys and distinguish user maps from
tagged scalars. Duplicate native keys, unhashable keys, unknown tags/fields,
nonfinite values and embeddings refuse. Null is plain JSON null, not a tagged
`type: null` object.

Both directions require canonical values for the transport descriptor. Missing
nullable STRUCT fields or different DECIMAL p/s are **not** silently normalized.
Later assignment to the Grafx target schema is separate and may perform its
existing exact decimal rescale. A transported LIST of DECIMAL(12,4), for example,
may be assigned to LIST of DECIMAL(14,5) if every value fits exactly.

The CLI's bounded human-oriented JSON observation is not this exact interchange
format: it can render bytes as hexadecimal, stringify map keys and report its
nesting ceiling. Use these helpers or native logical transfer for faithful data
movement, not an assumption that every CLI report is directly importable.

## CSV, JSONL and SQLite use

Declare `types: tuple[str | StoredType, ...]` in the existing readers/importers.
Scalar strings retain their contracts. CSV carries a JSON text cell; JSONL accepts
a structured value or encoded JSON text. SQLite requires TEXT containing that
JSON value, never INTEGER/REAL/BLOB inference. CSV's null token, JSON null and SQL
NULL are subject to the collection root's nullability.

Readers validate and privately clone descriptors at admission. Changing a caller's
descriptor after a lazy reader has yielded cannot change subsequent rows. Direct
readers cannot retract yielded data; import facades retain whole-call native
staging rollback. A late malformed row, bound or target-schema error discards the
entire import call and preserves the caller's prior staging.

```python
import json
from okto_grafx import connect, StoredType, DecimalValue
from okto_grafx.collection_json import collection_json_value
from okto_grafx.text_import import import_jsonl

# input_root is a trusted existing directory owned by this example.
descriptor = StoredType("STRUCT", fields=(
    ("ids", StoredType("LIST", element=StoredType("INT64", nullable=False))),
    ("cost", StoredType("DECIMAL", precision=12, scale=4)),
    ("data", StoredType("ANY")),
))
value = {"ids": (1, 9223372036854775807), "cost": DecimalValue(12500, 12, 4),
         "data": {"type": "decimal", "this_is": "an ordinary user map"}}
path = input_root / "collection.jsonl"
path.write_text(json.dumps({"id": 1, "value": collection_json_value(descriptor, value)}),
                encoding="utf-8")
with connect(":memory:") as db:
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute(f"CREATE NODE TABLE Receipt(id INT64,value {descriptor.describe()},PRIMARY KEY(id))")
        report = import_jsonl(tx, "CREATE(:Receipt {id:$id,value:$value})", path,
            allowed_root=input_root, columns=("id", "value"), types=("INT64", descriptor))
        assert report.statements == 1
    assert db.execute("MATCH(n:Receipt) RETURN n.value").rows == ((value,),)
```

The transport declaration need not be installed as the target column type:
an ANY target can retain concrete nested values without activating
typed_collections_v1. Required capabilities still follow actual native storage.

Nested processing charges the existing `max_work` by native encoded value bytes,
in addition to existing row/field/SQLite instruction work. Existing field, batch,
file, row and transaction limits remain. SQLite closes its bounded source snapshot
before Grafx staging. No Arrow dependency, implicit schema creation, cross-store
transaction or new durability path is introduced.
