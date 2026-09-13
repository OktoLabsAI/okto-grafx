# Native temporal and structured procedure signatures — FP-7

Status: implemented on `feature/v0.0.6`, development only. This extends
[invocation](PROCEDURE_INVOCATION_V1.md), [numeric signatures](PROCEDURE_NUMERIC_SIGNATURES_V1.md)
and [writing callbacks](WRITING_PROCEDURES_V1.md), not full FP-7/FP-6 completion.

## Signatures, representation and ownership

`TabularProcedure.argument_types` and output columns accept these additional exact
uppercase names. Every signature admits None/NULL; callbacks still run on NULL.

| Signature | Non-NULL input | Callback / direct invoke representation |
| --- | --- | --- |
| `DATE` | Exact DateValue | New validated native value |
| `LOCALTIME` | Exact LocalTimeValue | New validated native value |
| `TIME` | Exact TimeValue | New value with detached time and recorded offset |
| `LOCALDATETIME` | Exact LocalDateTimeValue | New value with detached date/time |
| `DATETIME` | Exact DateTimeValue | New value preserving nanos, offset and recorded zone |
| `DURATION` | Exact DurationValue | New value preserving months/days/seconds/nanos |
| `DECIMAL` | Exact DecimalValue | New validated value preserving coefficient, precision and scale |
| `LIST` | Exact Python list or tuple of supported native values | New recursively detached mutable list |
| `MAP` | Exact Python dict with exact string keys | New recursively detached dict |
| `VECTOR_F32` / `VECTOR_F64` | Exact VectorValue with matching dtype | New VectorValue, preserving space_ref/components |
| `ANY` | Native scalar (including DECIMAL), temporal, vector, LIST and MAP union | Original scalar kind; compound values detached as above |

The original BOOL/INT64/DOUBLE/NUMBER/STRING/BYTES/TIMESTAMP/UUID signatures remain.
NUMBER preserves int/float/DECIMAL; DOUBLE alone performs its documented INT64 widening.
ANY does not coerce integers to floats, stringify values or infer an entity type.
Temporal inputs are native Grafx classes, not host datetime/date, ISO strings or
subclasses. Native query constructors produce admissible values. Copying validates
even forged frozen components without loading timezone rules or reinterpreting a
recorded offset/zone. Native temporal classes are exported from `okto_grafx`.
Native `okto_grafx.DecimalValue` receives the same defensive capture for direct
DECIMAL, NUMBER and nested ANY/LIST/MAP values. It does not execute host decimal
conversions, round/rescale to an inferred type or reuse a callback-owned instance.
Query constructors, ProcedureReader.query/ProcedureWriter.query and normal native
writes can consume these values. Storage still requires its native capability and
exact target assignment. [Interface qualification](../reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md).

Both arguments and output cells are copied. A callback can change its private
list/dict without changing parameters or incoming row bindings. A generator that
reuses and changes an output container cannot mutate an earlier yielded row.
Repeated shared children are copied per occurrence; cycles are rejected.
Public query results/cursors keep their existing **tuple** list representation;
maps are dicts with recursively detached values. Direct `procedure.invoke()`
returns the callback-boundary list representation, not public-query conversion.

MAP keys must be strings, even though a lower-level codec supports other keys.
NaN/infinity are rejected recursively at the extension boundary; their availability
in native expressions does not admit them here or in storage. Arbitrary mappings,
generators, bytearray, host `decimal.Decimal`/Fraction, conversion protocols, subclasses and other
host objects are not admitted. A subsequent [entity signature contract](PROCEDURE_ENTITY_SIGNATURES_V1.md)
admits invocation-witnessed NODE/RELATIONSHIP/PATH values, including inside ANY,
LIST and MAP. Detached objects supplied externally still grant no native authority.

## Consumption and persistence

```python
from okto_grafx import connect, DateValue
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

def normalize(payload):
    payload["dates"].append(DateValue(2024, 2, 29))  # Owned callback copy.
    return ((payload,),)

procedure = TabularProcedure(
    "app.normalize", ("MAP",), (("payload", "MAP"),), normalize,
    argument_names=("input",),
)
extensions = ExtensionRegistry(trusted=True, procedures=(procedure,))
original = {"dates": []}
with connect(":memory:", extensions=extensions) as db:
    result = db.execute("CALL app.normalize", {"input": original})
    assert original == {"dates": []}
    assert result.rows == (({"dates": (DateValue(2024, 2, 29),)},),)
    db.ensure_identity_indexes()  # Existing catalog-v2 admission for temporal/ANY DDL.
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Event (id INT64, payload ANY, PRIMARY KEY(id))")
        tx.execute("CALL app.normalize($input) YIELD payload "
                   "CREATE (:Event {id:1, payload:payload})", {"input": original})
```

These signatures also work with `mode="write"`: its engine-supplied ProcedureWriter
remains the first callback argument, before the declared native values. Its mutation
parameters accept the owned values under the existing native parameter contract.
Permissions, expiry, snapshot, no-own-COMMIT policy, shared budgets and rollback do
not change. Unit callbacks still return exactly None; tabular callbacks return
tuples matching every declared column, including unselected outputs.

LIST/MAP postfix operations, UNWIND, aggregates, temporal functions, subqueries,
UNION, cursors and typed writes reuse native semantics. Known type mismatches fail
at planning, including zero-input pipelines; dynamic/ANY and bound parameters
remain validated at execution. ANY output stays dynamically typed, not a fictitious
persisted ANY tag. EXPLAIN/signature resolution never executes callbacks.

Temporal outputs can persist in native temporal columns or nested inside ANY.
Lists/maps use existing heterogeneous storage. This does not implement stored
`LIST<T>`, `MAP<T>`, ARRAY, STRUCT or DECIMAL (FP-6), nor widen ScalarFunction/UDF
signatures. Vector signatures do not authorize/create a space: destination column
space, dimension and dtype validation still apply. Existing native index/history/
interchange support and restrictions remain; no new transport format is introduced.

There is no new wire tag, feature bit or DatabaseConfig field. Query-only use does
not activate storage metadata. First actual temporal persistence follows existing
atomic capability admission, catalog-v2 prerequisites and old-reader fencing.
Failed statements preserve previous transaction statements and publish neither
their temporal effects nor a first temporal capability. Durable COMMIT remains the
authority for recovering both rows and metadata after an apply failure.

## Budgets and errors

No new options are required. Existing descriptor `max_value_bytes`, `max_rows`,
`max_result_bytes` and writing shared budgets apply. New value families use this
recursive ownership tariff; prior direct INT64/DOUBLE/scalar tariffs stay unchanged:

| Part | Charge to `max_value_bytes` |
| --- | --- |
| NULL / BOOL / INT64 or DOUBLE | 1 / 2 / 9 bytes |
| STRING, including map keys | 5 + 4 × characters; UTF-8 validated after admission |
| BYTES | 5 + payload length |
| LIST / MAP | 5-byte header plus all recursively charged children/keys |
| TIMESTAMP / UUID | 9 / 17 bytes |
| Native temporal | Actual bounded canonical frame length |
| DECIMAL, including direct NUMBER | 19 bytes; validation/detachment preserves coefficient/p/s |
| Vector | 9 + 8 × components; conservative for float32 |

Limits: depth 64, 1,024 elements per list, 256 entries per map, 1,048,576 characters
per string, and the native vector dimension/finite/space-reference bounds. Repeated
aliases are charged repeatedly. Charges precede large string/vector encoding and
recursive container allocation; temporal frames have a small fixed maximum
including the zone field. These are logical budgets, not arbitrary Python heap/CPU
limits. Result-byte quotas charge actual native encoding **after detachment**;
writing cumulative quotas include every column and invocation.

Invalid registration: GrafxConfigurationError. Known argument mismatch keeps
`GrafxPlanError(field="procedure_type")`. Recursive validation uses
`field="procedure_value"`, with reasons including `type_mismatch`, `cycle`, `depth`,
`list_elements`, `map_entries`, `map_key_type`, `int64_range`, `nonfinite`,
`unsupported_value` and `invalid_native_value`. Forged components produce a value
error, not a fabricated on-disk corruption diagnosis. Byte overflow is
`GrafxQueryBudgetExceeded(resource="procedure_value")`; existing result/shared
budget codes remain. Every selected and unselected output is checked before use.

Callback/stream/public-result failures still roll back the entire outer instruction.
No callback retry, sandbox, external-effect rollback or forced host-code preemption
is added. Trusted callbacks must follow the existing read-purity/write-authority
contract and must not use captured resources to bypass transaction lifecycle.

## Qualification and remaining work

Feature tests: `tests/api/test_procedure_native_values.py`. Initial affected
extension selection: 198 passing tests. Broader native regression, pure/NumPy
durable-fault evidence and exact receipts:
[qualification](../reports/FP7_NATIVE_VALUES_QUALIFICATION.md).

Subsequent entity/query/nesting contracts and [schema authority](PROCEDURE_SCHEMA_AUTHORITY_V1.md)
extend this value increment. FP-6 parameterized collection/DECIMAL storage remains
in the original plan. This increment is not full Cypher/procedure parity or an
installed-Pulse qualification and does not change the frozen TCK ledger.
