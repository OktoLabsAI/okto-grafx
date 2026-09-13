# Numeric procedure signatures — FP-7 development

Status: native signatures implemented; all **52 original procedure cases pass**,
and 317 affected regression tests pass. The final numeric/public-contract selection
also passes all 3,510 tests. This increment extends [registered invocation](PROCEDURE_INVOCATION_V1.md);
it does not complete transaction-scoped writing procedures or the other parity
work packages.

## Registration and values

`TabularProcedure.argument_types` and each declared output column now accept
`NUMBER`, in addition to `BOOL`, `INT64`, `DOUBLE`, `STRING`, `BYTES`, `TIMESTAMP`
and `UUID`. NUMBER is a **procedure signature union**, not a new persisted type.
There is no NUMBER DDL column, storage tag, activation, database setting or format
change. `ScalarFunction` keeps its existing exact signatures and does not accept
NUMBER or gain implicit integer-to-DOUBLE conversion in this increment.

| Declaration | Accepted non-NULL input/output | Native callback/result representation |
| --- | --- | --- |
| `NUMBER` | Exact built-in int in signed INT64 range; finite built-in float; native DecimalValue | Preserve the native kind without conversion; DECIMAL defensively copied with exact coefficient/p/s |
| `DECIMAL` | Exact native DecimalValue | Validated detached native value; no implicit float/int/text conversion or rescaling |
| `DOUBLE` | Finite built-in float; exact built-in int in signed INT64 range | Convert integer input/output to Python float (binary64) |
| `INT64` | Exact built-in int in signed INT64 range | Preserve integer; float is not narrowed |
| Other existing scalar types | Same exact type contract as before | No coercion |

Every declaration still admits NULL. Procedure callbacks receive None rather than
being skipped; unit callbacks still return exactly None. BOOL is never an integer
or NUMBER, even though Python bool subclasses int. Host numeric subclasses, `decimal.Decimal`,
Fraction, strings and arbitrary conversion protocols are not implicitly executed.
Native integer range and value-size checks occur **before** float conversion.

DOUBLE conversion uses ordinary binary64 rounding. Integers above `2**53` are not
all exactly representable: `2**53 + 1` becomes `9007199254740992.0`. This applies
both to callback inputs declared DOUBLE and integer cells returned for DOUBLE
columns. Choose NUMBER to preserve integer identity/precision, or INT64 for an
integer-only contract. Do not select DOUBLE when exact large integers are required.
There is no silent conversion to or from native DECIMAL. DECIMAL/NUMBER preserve
the native value's p/s; a typed storage assignment separately applies its exact
rescaling rule. The signature name is `DECIMAL`, not `DECIMAL(p,s)`. ScalarFunction
does not gain DECIMAL registration from this procedure extension.

NaN and infinities remain rejected at this trusted extension boundary, even though
the native expression evaluator admits NaN in its own expression contract.
Their storage prohibition is unchanged. Query parameters outside the native
INT64 range are refused by public admission; direct procedure invocation also
validates that range and cannot bypass it through widening.

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

echo = TabularProcedure(
    "app.echo", ("NUMBER",), (("value", "NUMBER"),),
    lambda value: ((value,),), argument_names=("input",),
)
with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(echo,))) as db:
    result = db.execute("CALL app.echo", {"input": 2**53 + 1})
    assert result.rows == ((2**53 + 1,),)
    assert type(result.rows[0][0]) is int
```

## Planning, composition and failures

Native planning admits known INT64/DOUBLE/DECIMAL arguments for NUMBER;
DOUBLE admits INT64/DOUBLE only. It
rejects known incompatible types. Parameter-bound validation uses the same
assignability rule. NUMBER outputs retain a row-dependent numeric kind rather
than falsely declaring every output DOUBLE or allocating a fictitious storage
enum. Per-row signature validation remains authoritative through aliases,
aggregation, DISTINCT, UNION, subqueries, arithmetic, typed writes and read cursors.
NULL aggregation and numeric grouping use the existing native query semantics.

Both selected and unselected result cells are validated and normalized before
the row is yielded. A late bad numeric result triggers the enclosing statement's
rollback, preserving earlier successful statements in that transaction. Validation
does not publish an intermediate commit, retry callbacks or acquire another writer.
The host must still supply trusted, thread-safe callbacks; external effects cannot
be rolled back or sandboxed by the database.

The subsequent DECIMAL extension charges its complete **19-byte native frame** to
`max_value_bytes` before validation/copy; nested ANY/LIST/MAP use that same tariff.
Original direct INT64/DOUBLE tariffs remain unchanged. `max_value_bytes` still
checks every input/output value. `max_result_bytes` charges
the encoded normalized output, and `max_rows` still bounds a tabular invocation.
Type/range/nonfinite result failures retain the `GrafxPlanError` value-validation
contract; native query parameter boundary failures remain typed configuration
errors where appropriate. Budget violations remain `GrafxQueryBudgetExceeded`.
No resource budget is disabled for numeric signatures.

## Qualification

The 317-test affected selection includes numeric, unit, tabular, invocation and
scalar extension APIs; UNION; bound numeric/scalar parameters; aggregate
determinism; query-memory spill; reference fixture admission and procedure error
mapping. Direct and query invocation cover integer extremes, NULL, large-integer
rounding, booleans, nonfinite values, all-column validation, budget failures,
pure/NumPy failed-statement rollback, verification and cold reopen. Typed INT64
storage through NUMBER output preserves `2**53 + 1` exactly.

Receipt `.grafx-tmp/fp7-numeric-regression.xml`: **317 passed**, zero failures/errors/
skips, 41.336 seconds, terminal exit 0; SHA-256
`64132d82385c076bd4ee57f1b55a6f89f9071b004442b347eabde3929dea821a`.
The earlier focused 117-test selection also passed; its receipt
`.grafx-tmp/fp7-numeric-focused.xml` has SHA-256
`ce7f197b52b4490f9e935009a05d8842fbc12783541c3dd7ed85cc334a3c7762`.
Counts overlap. A preceding baseline retained the old fixture test expecting
NUMBER admission to fail; that negative probe now uses unsupported DECIMAL,
while the unchanged upstream NUMBER fixtures exercise actual numeric union inputs.

Original TCK owner FP-7: **52 passed / zero failed / zero selected not run**,
terminal exit 0. The report's 3,845 not-run entries are all outside this selection.
Receipt `.grafx-tmp/fp7-numeric-native-20260912.json`, SHA-256
`74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`.
Same pinned revision and unchanged V2/V1 ledger verification as the
[invocation receipt](../reports/FP7_INVOCATION_QUALIFICATION.md), with `--owner FP-7`.
NUMBER fixture signatures now use native registrations; FLOAT fixtures remain
strictly checked as declared, and integer-to-float conversion occurs in Grafx,
not by rewriting fixture rows or the query under test.

Final strict numeric-error and public-surface/annotation/import/language selection:
**3,510 passed**, zero failures/errors/skips, 74.488 seconds, terminal exit 0.
Receipt `.grafx-tmp/fp7-numeric-contracts.xml`, SHA-256
`f818fc4eb7fd231dd8671296dc56f4ffb0bd1042ee0d539cd7d615ea5b8e818a`.
This reruns the numeric features after replacing broad exception assertions with
native error classes and fields. API reference regeneration, documentation checking
(39 configuration fields/11 preserved source plans), Ruff across all changed and
untracked Python files and whitespace checking pass.

This is an owner-selection result, not a new complete TCK run or completed FP-7.
Writing capabilities, broader entity/temporal/collection signatures, FP-6 stored
types and final multi-package qualification remain open.
