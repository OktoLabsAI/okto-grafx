# FP-6 exact decimal contract

[Functional parity plan](FUNCTIONAL_PARITY_PLAN.md#fp-6--decimal-and-persisted-nested-values)

Status: **native storage and numeric queries implemented, full FP-6 in progress** in 0.0.6 development. Public
`DecimalValue` parameters/results, `DECIMAL(p,s)` columns and bounded native frames
are now implemented, together with explicit casts, +/−/×/÷, SUM/AVG/MIN/MAX,
exact comparison/grouping/ordering and typed-column equality-index seeks. Typed
collections and supported consumers are now locally qualified; the combined
[installed type checkpoint](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md) is complete
for its candidate. Final full-profile/Pulse acceptance remains required.
Internal arithmetic/conversion qualification remains below; the native storage
contract and qualification are separate from those helper-level results.

## Representation and arithmetic policy

`DECIMAL(p,s)` has `1 <= p <= 38`, `0 <= s <= p`. Its value is a signed integer
coefficient divided by `10**s`, with `abs(coefficient) < 10**p`. Precision and
scale are declared metadata; trailing fractional zeros survive rendering. Negative
zero is canonical zero. No host decimal context or binary float participates in
exact decimal arithmetic, conversion into DECIMAL or rounding. Explicit `toFloat`
is a separate lossy conversion. Stored nonfinite values remain forbidden.

The internal immutable value records coefficient, precision and scale. Numeric
comparison and grouping keys ignore representational trailing zeros, while the
value object's structural equality retains its declared type. Integration must
use the numeric key, not dataclass equality, for Cypher equality/grouping/indexes.
Finite DOUBLE comparisons use its exact integer ratio, not its displayed decimal
spelling; BOOL and arbitrary host coercions are not numeric evidence. Expression
NaN/unordered and infinity handling remains the query layer's responsibility.

The native query arithmetic contract is:

| Operation | Result policy |
| --- | --- |
| `+`, `-` | Align scales, calculate with integers, return precision 38 at the larger input scale. |
| `*` | Multiply coefficients and add scales; return precision 38. |
| Exact-result fitting | If scale or coefficient exceeds the result envelope, remove only trailing coefficient zeros until it fits. If nonzero digits would be lost, raise `decimal_overflow`. Zero may reduce an excessive scale to 38. |
| `/` | Round once, HALF_EVEN, to the largest scale in 0–38 that fits precision 38. Determine scale from the exact integer quotient, not floating logarithms. If rounding carries across an integer boundary, recompute from the original ratio at the next scale. Division by zero refuses. |
| Unary sign / abs | Preserve declared precision and scale; INT64 conversion is not an intermediate. |
| SUM | Accumulate exact aligned integer coefficients under the query resource budget; fit once at finalization, not per input. Empty/all-null input follows existing query semantics. |
| AVG | Exact sum/count ratio followed by the same division finalization; never average rounded intermediate values. |

Implicit storage assignment/rescaling is **exact only**. Explicit conversion may
select EXACT, HALF_EVEN, HALF_UP (ties away from zero), DOWN (toward zero), FLOOR
or CEILING. Overflow after rounding refuses. NULL propagation and expression NaN
are handled before this finite-value layer. No automatic DOUBLE promotion is allowed.
The same input must produce the same result regardless of `decimal.getcontext()`.

### Explicit conversions

The internal `decimal_from_text(text, precision, scale, rounding="EXACT")` consumes
an exact built-in string, 1–1,024 ASCII characters. It accepts an optional sign,
decimal point and signed `e`/`E` exponent (`.5`, `1.`, `1.25e-2`); whitespace,
underscores, non-ASCII digits, NaN/Infinity and host string coercion refuse. Exponent
magnitude never controls a power allocation: obviously overflowing values refuse
first; values below one tenth of a target unit round from their sign. Zero retains
the requested scale even with an extreme exponent.

`decimal_from_number(value, precision, scale, rounding="EXACT")` is an explicit
conversion of an exact built-in int or finite float. A float uses its exact binary
ratio: converting `0.1` exactly to scale 1 refuses, while converting text `"0.1"`
succeeds. Select an explicit rounding mode if rounding that binary value is intended.
BOOL, host Decimal/Fraction objects and arbitrary conversion hooks are not inferred.
The native `decimal(value,p,s[,rounding])` query function now uses these same
rules. Public native parameters use `DecimalValue` directly, not implicit
conversion from host decimal objects.

### Aggregate finalization

`decimal_sum(values)` and `decimal_average(values)` consume native decimals/NULLs
once, retaining an integer total and count, not a list of inputs. Input scale is
aligned without intermediate rounding or precision-38 overflow. Thus
`[maximum, maximum, -maximum]` sums to maximum in every order, and the average of
`[maximum, maximum]` is valid even though that sum cannot be a stored DECIMAL.
SUM fits only at finalization; AVG divides the exact total by count and scale once.
Empty/all-NULL SUM yields decimal zero; AVG yields NULL. An invalid later element
or iterator error propagates; there is no partial aggregate result or implicit retry.
The query caller must enforce its existing row/work/memory budgets when wiring
these helpers; they neither replace those controls nor open a transaction.

## Remaining full qualification (not waived)

Native decimal storage/query/index, history/copy/transfer, CLI/local imports,
procedure signatures and columnar consumption are now implemented with the local
qualification linked below. This replaces the earlier consumer implementation
backlog, not its historical receipts. The declared ordered/FTS index refusals remain
explicit; supported typed equality indexes do not imply every index family.
Typed collection-column integration and the combined installed old/new-reader
checkpoint C now have [qualification](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md).
The final full profile/paired Pulse qualification remains required by FP-6/FP-8.
Pulse conversion belongs in Community. No deployment or complete parity claim is
implied by these local functional, concurrency and durable-recovery tests.

## Foundation qualification (September 12, 2026)

`domain/model/decimal_values.py` implements immutable representation, numeric
ratio keys, exact mixed finite comparison, scale-preserving rendering, explicit
rescaling, addition/subtraction, multiplication, division, negation and absolute
value. It does not import the host decimal module. Native query aggregate integration
is not implemented by these helpers.

Final functional tests: **77 passed**, zero failures/errors/skips, 0.414 seconds,
terminal exit 0. `tests/storage_core/test_decimal_values.py` covers all 39 scales,
38-digit bounds, negative values, inexact assignment, rounding ties/directions,
overflow, division by zero, exact comparison against DOUBLE and refusal of host
coercions. It also executes 250 Fraction/Decimal arithmetic examples, 1,000
rescaling examples and 1,000 full-width division examples against independent
standard-library oracles. Host decimal context is deliberately changed in the
independence test. These loops are cases inside tests, not additional pytest counts.

Receipt `.grafx-tmp/fp6-decimal-foundation-final.xml`, SHA-256
`a6dc92043936a384633546e14532801381ae051d5e239fc34517d594f1926714`.

The preceding combined foundation/public-surface/import-boundary collection passed
**2,407 tests**, zero failures/errors/skips, 52.568 seconds. Receipt
`.grafx-tmp/fp6-decimal-contracts.xml`, SHA-256
`f11d50c6708ac909857ad74036016d918e3cd1c6fb93ea9ee2d73575fc035bfd`.
It predates the final unary helpers and expanded division oracle. After those
additions, all **nine module-specific surface/import checks** passed again:
`.grafx-tmp/fp6-decimal-final-surface.xml`, SHA-256
`dadb4262e3c485e9766e86f2572b2387d2ff2bae795f6384e95c667d85e18b85`.
Counts overlap; no whole-repository or durable-type qualification is implied.

Generated API documentation, documentation links/configuration coverage, Ruff and
whitespace checks pass. No public API/configuration is added; the query guide
explicitly retains the lack of DECIMAL admission. The separate grouped query and
transaction checkpoint is not represented by these receipts and must complete
before its own acceptance is claimed.

## Conversion and aggregate qualification

Both decimal test modules now pass **118 tests**, no failures/errors/skips,
0.659 seconds, terminal exit 0. Receipt
`.grafx-tmp/fp6-decimal-conversion-aggregation.xml`, SHA-256
`51a2ddf52baeca17be0ee479be20a5c32289e42c924b3b498503c0a36a498893`.
This supersedes the earlier functional collection without adding overlapping counts.

The new cases cover exact/scientific text, syntax and size refusals, both signs of
900-digit exponents with all six rounding modes, exact finite DOUBLE conversion,
permutation-independent cancellation, average finalization beyond the temporary
38-digit total, NULL/empty input and late bad elements. Another 200 aggregate
fixtures use independent Fraction/Decimal oracles and 2,400 text round trips.
These are helper-level tests, not native query/storage admission tests.

The first follow-up surface run rejected the new unrestricted `re` import. The
production parser was changed to bounded ASCII character parsing; no allowlist or
test expectation was relaxed. The final combined selection passes **127 tests**
(118 functional plus nine module surface/import checks), zero failures/errors/skips,
1.849 seconds, terminal exit 0. Receipt
`.grafx-tmp/fp6-decimal-conversion-qualified.xml`, SHA-256
`42e49ae3e701a107a107c9b50d3864f334fec64ff3488fb633d1087c92dcee63`.
API generation, documentation validation, Ruff and whitespace checks pass afterward.
The failed surface receipt is preserved as
`.grafx-tmp/fp6-decimal-conversion-surface.xml`; it is not a passing result.

## Native integration map

The integration map retains the full FP-6 obligations. Initial native storage
coverage does not close the matrix or qualify other consumers automatically:

| Consumer | Existing location | Required invariant |
| --- | --- | --- |
| Schema/DDL | `domain/model/schema.py` ColumnDef; query ColumnSpec/parser/planner | Precision/scale must survive declaration, introspection and copies; a bare enum tag cannot carry them. |
| Catalog | `domain/model/catalog.py` table encoder/decoder and required capabilities | Versioned parameter metadata and capability refusal precede any new column/row publication. |
| Value and tuple codecs | `domain/model/value.py`, schema tuple codecs | Preserve native type, scale and coefficient; malformed frames refuse, both codecs agree, ANY nesting is covered. |
| Execution and spilling | `engine/query_engine.py`, domain query scalars/planner | Bind/cast/operate/aggregate/compare with native values; spill and detached entities cannot stringify or round them. |
| Indexes | `domain/index/keys.py`, `ordered_keys.py` | Numeric equality, ordering and grouping must agree across scales and admitted numeric families. |
| Durability/history/transfer | transaction/recovery and `engine/system_history_*`; `transfer.py`, `graph_interop.py` | Same value/type after replay, retained history, copy and supported interchange; unsupported routes refuse before partial import. |

The required typed LIST/MAP/ARRAY/STRUCT descriptors share this schema-parameter
boundary and remain in the full FP-6 scope.

## Native storage contract (development)

```python
from okto_grafx import connect, DecimalValue

with connect(":memory:") as db:
    db.ensure_identity_indexes()  # Explicit persistent catalog-v2 prerequisite.
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Invoice(id INT64, amount DECIMAL(10,3), PRIMARY KEY(id))")
        tx.execute("CREATE (:Invoice {id:1, amount:$amount})",
                   {"amount": DecimalValue(123, 4, 2)})  # 1.23
        assert tx.execute("MATCH (n:Invoice) RETURN n.amount").rows == ((DecimalValue(1230, 10, 3),),)
```

`DecimalValue(coefficient, precision, scale)` is an immutable, exact native value
exported from `okto_grafx`. Its helpers `as_integer_ratio()`, `numeric_key()`,
`to_string()` and `rescale(precision, scale, rounding="EXACT")` follow the rules
above. Native parameter capture and detached entity results revalidate/copy the
value. BOOL, binary float and host `decimal.Decimal` are not implicit assignments
to a DECIMAL column. NULL follows the column's existing nullability policy.

`ColumnDef` adds trailing `decimal_precision` and `decimal_scale` fields, required
only for `ValueType.DECIMAL`. DDL requires both parameters; no bare DECIMAL or
parameter expressions in declarations. Declared metadata survives catalog views,
nullable-column addition and reopening. Assignments rescale exactly **before** row
intents/quotas and returned write observations are retained, not only when encoding
bytes. Inexact rescaling and overflow refuse without rounding. An unchanged native
value retains identity; ordinary nondecimal rows do not gain another column walk.

The wire frame is exactly **19 bytes**: tag **18**, precision and scale (one byte
each), then signed little-endian 128-bit coefficient. Nested ANY/LIST/MAP values
preserve the offered native metadata. Typed columns persist their declared metadata;
a stored frame with different precision/scale is corruption even when numerically
equal. Omitted-column projections also validate the complete frame. Generic query
spill encoding remains distinct from tuple admission: expression NaN is allowed
in query values but remains forbidden in stored properties, including nested values.

Catalog-v2 capability **`decimal_values_v1`**, bit **26**, is monotonic. DECIMAL
columns append their two type parameters to the existing column record. The schema
commit publishes the capability with that metadata. First native values in ANY
publish the capability in the **same user COMMIT** as their rows; there is no
separate commit, automatic retry or new lock policy. The shared admission walk can
discover temporal and decimal values together and preserves private DDL images and
their exact provenance. Already admitted families require no new schema admission
lock. Original-snapshot OCC, reader snapshots and WAL durability remain unchanged.
Readers without the capability must refuse before catalog/data replay effects.

No new connection setting is added. Existing `codec="pure"`/`"numpy"`, transaction
budgets and explicit catalog activation apply. No current Pulse installation or
production database has been upgraded for this type by this increment.

| Consumer | Current native increment |
| --- | --- |
| Python parameters, scalar projection, typed/ANY properties | Implemented; owned values, exact typed assignment, update/rollback/reopen. |
| Node/relationship observations | Native properties retained; `to_dict()` adds `{"type":"decimal","coefficient":"1230","precision":10,"scale":3}` without DOUBLE conversion. |
| Column declaration/introspection/nullable append | Implemented with native p/s metadata and old-row NULL behavior. |
| Storage/WAL/fence/reader snapshots | Focused fault, crash and pure/NumPy coverage; complete installed-reader checkpoint still pending. |
| Arithmetic/casts/aggregates, comparisons/grouping/ordering | Implemented native +/−/×/÷, signs/abs, decimal conversion, SUM/AVG/MIN/MAX, recursive equality, DISTINCT/grouping and ordering with memory/spill agreement; bounded scope below. |
| Decimal PK/equality indexes | Typed DECIMAL PK and hash/sparse-hash/posting-hash equality indexes use exact declared-scale probes; wide integers, cross-scale equality, updates, snapshots and cold reopen are covered. Composite keys are supported. |
| Decimal ordered/full-text indexes | Unsupported by the existing index families; creation refuses before publication. Query ORDER BY is implemented but is not an ordered index access path. |
| History and physical backup/restore | Implemented with exact native values and historical p/s; scan/index, nullable append, update/delete/recreate, reopen and real-process commit/replay cuts are tested. |
| Existing-target catalog copy | Exact values and p/s-bound schema/digest; matching target declarations required even for empty tables; one data/receipt commit, snapshot isolation and idempotent retry. |
| Fresh-store logical transfer/resume | Explicit p/s metadata, native frames, typed/ANY/flexible/grouped namespaces, endpoint remapping and index rebuild; full preflight refuses malformed or schema-mismatched frames before target/workspace creation. No implicit rescaling. |
| CLI JSON and schema inventory | Scalar/nested results use the same exact native tag as entity properties; schema JSON includes declared p/s. No parameter type inference. |
| CSV/JSONL/SQLite import | Explicit DECIMAL family decodes canonical tagged objects/text; native values retain p/s. Exact target assignment, whole-call rollback and existing source/batch budgets remain enforced. |
| Registered procedure signatures/owned value capture | DECIMAL and NUMBER accept owned native decimals; nested ANY/LIST/MAP, reader/writer queries, result budgets and atomic writes are covered. DOUBLE does not cast decimals implicitly; ScalarFunction is unchanged. |
| Arrow/Pandas/Polars/Parquet | `ArrowDecimalType(p,s)` maps exact native values to decimal128 with mandatory metadata. All four routes preserve coefficients/p/s/NULL, validate native bounds, charge decimal workspace and retain whole-call import rollback. No implicit export rescaling, object dtype, decimal256 or dictionary inference. [Contract](../EXTENSIONS_AND_ARROW.md#exact-native-decimals-006-development), [qualification](../reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md). |
| Typed LIST/MAP/ARRAY/STRUCT descriptors | **Pending**; ANY nesting is not a substitute. |

See [native qualification](../reports/FP6_DECIMAL_NATIVE_QUALIFICATION.md) for exact
receipts, corrected failures and remaining full-profile/Pulse obligations.

## Native numeric query contract

```python
from okto_grafx import connect, DecimalValue

with connect(":memory:") as db:
    result = db.execute("UNWIND [decimal('1.20',4,2),2,null] AS v RETURN sum(v), avg(v)")
    assert result.rows[0][0] == DecimalValue(320, 38, 2)
    assert result.rows[0][1].to_string() == "1.6000000000000000000000000000000000000"
    assert db.execute("RETURN decimal('1.25',3,2)=1.25, decimal('0.1',1,1)=0.1").rows == ((True, False),)
```

`decimal(value,p,s[,rounding])` accepts native DECIMAL, exact INT64, finite DOUBLE
or bounded decimal text. NULL propagates. Its optional mode is an exact uppercase
name from the six modes above, default EXACT. Precision/scale/inexact/overflow and
nonfinite conversion failures are structured `GrafxPlanError` errors with the
underlying `decimal_*` reason and execution phase. `toString(decimal)` preserves
scale; `toInteger(decimal)` explicitly truncates toward zero and returns NULL
outside INT64; `toFloat(decimal)` is explicitly lossy. `toBoolean(decimal)` refuses.

Native arithmetic accepts DECIMAL with DECIMAL or INT64; integers are promoted
exactly to precision 38, scale 0. It never implicitly combines DOUBLE, even if the
float happens to be integral. Unary +/- and `abs` retain the declared type;
`sign` returns INT64. Decimal `%`, `^`, transcendental/other numeric functions and
percentile aggregates are not added by this contract. Explicitly convert only
when losing decimal precision is intended. Decimal division by zero refuses; the
existing DOUBLE `0.0/0.0` expression NaN remains valid but cannot be stored/cast to
DECIMAL. Expression-only evaluation does not activate the storage capability.

Comparison uses exact ratios across DECIMAL/INT64/DOUBLE. Thus binary DOUBLE 0.1
does **not** equal exact decimal text 0.1, while 1.25 does. Use
`decimal('0.1',p,s)`, not a floating literal, when exact decimal equality is wanted.
NaN comparisons are false, unlike NULL comparisons (unknown); infinity orders
outside finite decimals. BOOL is not numeric. Lists/maps use the same recursive
numeric equality. Native DTO equality still compares coefficient/precision/scale;
it is intentionally different from query numeric equality.

DISTINCT/grouping unify numerically equal representations, including nested values.
Internal spill signatures preserve exact ratios and validate a private decimal
ordering marker; it is never a stored/user type. ORDER BY puts decimals in the
numeric family; mixed-family and NULL/NaN ranks keep their existing policy.
SUM/AVG stream aligned integer coefficients and round/fit only at finalization;
temporary totals can exceed 38 digits without overflow when the final result fits.
SUM retains the maximum observed decimal scale (subject to exact result fitting),
including DISTINCT duplicates, so representative order does not change its type.
Any observed decimal fixes the numeric aggregate family; DECIMAL/DOUBLE mixtures
refuse even when DISTINCT would discard one. Empty/all-NULL SUM is the existing
integer zero and AVG is NULL. Work/row limits remain in force; bounded aggregate
workspaces account for the exact coefficient state. No list of SUM/AVG inputs is
retained. MIN/MAX use the same numeric order in memory and spill.

Typed decimal equality indexes retain their existing `columns` durable derivation:
stored values already have the declared p/s. Native probes rescale DECIMAL exactly,
or convert finite INT64/DOUBLE exactly to that declaration. Inexact, out-of-range
or nonnumeric probes cannot equal a value of the column and yield no match. Other
column families retain existing completeness/fallback rules. This is not general
ANY numeric indexing, a new ordered-key format, or a lossily rounded probe.
Primary-key memo identity includes the declared key column, including p/s.

The [numeric query/index qualification](../reports/FP6_DECIMAL_QUERY_QUALIFICATION.md)
is additional evidence, not full consumer, installed-wheel or Pulse qualification.

## History, copy and logical-transfer consumption

`system_as_of`, `system_versions` and `system_diff` expose native decimal values
and full historical `ColumnDef` metadata. Nullable-column append keeps earlier
schema versions intact; later schemas read absent older fields as NULL. Historical
edge identity is lineage, so delete/recreate does not reuse the deleted version.
`TemporalLimits` and physical backup/restore contracts remain unchanged.

`capture_copy`/`copy_graph` retain declared p/s in the package schema, digest and
target compatibility check. Matching numeric value is insufficient when column
declarations differ. Same-key receipt retry remains idempotent and reader snapshots
do not observe the newly copied application rows. Source commit provenance and
preparation are still explicit. Current-only copy does not transfer source history.

`export_graph` writes `decimal_precision`/`decimal_scale` only on DECIMAL columns;
`import_graph` validates the complete artifact before opening a private destination
or resume workspace. Canonical stored metadata is required: a different p/s refuses,
even when exact assignment could rescale it. Native tag 18 and the schema parameters
require a DECIMAL-aware reader. Graph-model artifact formats 1/2/3, trusted-artifact
requirements, `TransferLimits`, fresh UUID, endpoint-ID mapping and promotion/resume
protocols are unchanged. Logical transfer remains current-state-only; physical
backup/restore retains history instead. No new configuration or public method is
needed for these routes.

See the [consumer qualification](../reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md)
for exact evidence, real-process cuts and remaining installed-reader/interchange
work. This does not qualify all FP-6 consumers or the currently installed Pulse.

## JSON, local imports and procedure signatures

The canonical JSON object is `{"type":"decimal","coefficient":"1234500","precision":12,"scale":4}`
for exact 123.4500. Coefficients use bounded canonical ASCII integer strings, not
JSON numbers; p/s are exact integers obeying native bounds. No fields are optional.
No negative-zero/plus/leading-zero/exponent spellings, duplicate keys, inferred
host types, extra fields or NULL components. Whole-value NULL remains allowed.
Scalar/nested CLI output and entity observations share this validated encoding;
CLI `--parameter` JSON maps are not automatically decoded. CLI schema inventory
publishes `decimal_precision` and `decimal_scale` for DECIMAL columns only.

Local imports declare the family `types=("DECIMAL",)`: JSONL accepts the tag object
or its encoded JSON text, while CSV and SQLite TEXT carry its JSON spelling.
SQLite INTEGER/REAL and untagged decimal strings are not inferred as native DECIMAL.
The reader preserves offered p/s; a typed destination uses normal exact assignment
to its own declared p/s. ANY preserves the offered metadata. Existing whole-call
rollback preserves prior caller statements after a late malformed cell, budget
failure or inexact assignment. Source/work/batch/field limits remain explicit.
See [text grammar and examples](../LOCAL_TEXT_IMPORT.md#native-decimal-fields-006-development)
and [SQLite source rules](../LOCAL_SQLITE_IMPORT.md).

`TabularProcedure` accepts `DECIMAL` argument/output signatures. `NUMBER` is now
the INT64/finite DOUBLE/native DECIMAL signature union, with each native kind
preserved. A direct or nested decimal is validated and copied using its 19-byte
native frame; each occurrence is independently owned and charged. NULL behavior,
required permissions, shared budgets and outer rollback remain unchanged. Native
ProcedureReader.query/ProcedureWriter.query results, typed/ANY writes and durable
recovery preserve the value. Even unselected output columns must validate before
the next row is exposed. No DOUBLE conversion or implicit p/s rescaling occurs at
the callback boundary; native aggregate rules still reject mixed DOUBLE/DECIMAL
SUM/AVG. The signature is `DECIMAL`, not a parameterized column declaration.
ScalarFunction retains its earlier explicit scalar signature set.

```python
from okto_grafx import connect, DecimalValue
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

echo = TabularProcedure("app.decimal", ("DECIMAL",), (("value", "DECIMAL"),),
                        lambda value: ((value,),))
registry = ExtensionRegistry(trusted=True, procedures=(echo,))
value = DecimalValue(1234500, 12, 4)
with connect(":memory:", extensions=registry) as db:
    assert db.execute("CALL app.decimal($v)", {"v": value}).rows == ((value,),)
```

These are trusted callbacks, not a host-code sandbox or cross-store transaction.
No new configuration, storage tag or release is introduced by these consumers.
[Interface qualification and remaining work](../reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md).

## Columnar consumers

Arrow, Pandas, Polars and Parquet use the shared `ArrowDecimalType(p,s)` contract
described in [the complete guide](../EXTENSIONS_AND_ARROW.md#exact-native-decimals-006-development).
Export requires matching value metadata; import preserves offered metadata before
normal exact destination assignment. Tagged decimal128 uses no decimal arithmetic
at the host boundary and cannot silently admit float or arbitrary host parameters.
The optional dependencies remain lazy. The new descriptor is not a storage type,
format activation or tuning option; pure/NumPy write/reopen/recovery continue through
the existing native transaction door. [Evidence](../reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md).
Typed persisted collections and installed old/new-reader checkpoint C are covered
by the subsequent [combined type qualification](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md).
