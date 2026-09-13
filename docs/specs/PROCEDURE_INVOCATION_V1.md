# Registered procedure invocation — FP-7 development

Subsequent [writing procedures](WRITING_PROCEDURES_V1.md) reuse these invocation
rules with explicit mode, permissions and a restricted transaction capability.
The historical qualification below did not exercise that later authority.

Status: native invocation expansion implemented and affected regressions qualified;
1,230 combined tests, 394 parameter/API/search tests, 101 final hardening tests and
3,459 public-contract checks pass. [Receipts and remaining scope](../reports/FP7_INVOCATION_QUALIFICATION.md).
This extends [unit procedures](UNIT_PROCEDURES_V1.md), not their graph
authority. Transaction-scoped writing callbacks and broader value signatures remain
part of the [functional-parity plan](FUNCTIONAL_PARITY_PLAN.md#fp-7--writing-procedures-within-transaction-authority).

## Immutable registration

`TabularProcedure` adds the optional trailing field
`argument_names: tuple[str, ...] | None = None`. If supplied, it must name every
positional argument in declaration order, with unique valid identifiers. The
existing maximum of 32 argument types also bounds the names. A list, duplicate,
empty name or mismatched arity is `GrafxConfigurationError(field="argument_names")`.
The empty tuple is valid for a zero-argument procedure.

None means the host did not declare parameter names; explicit positional calls
continue to work. A zero-argument procedure needs no names even when called
implicitly. Names are never inferred from Python reflection, callback variable
names, graph properties or a database record. This field belongs to the trusted
per-handle registration, not `DatabaseConfig` or a persisted executable manifest.
`ScalarFunction` does not gain implicit arguments.

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

procedure = TabularProcedure(
    "app.increment", ("INT64",), (("next_value", "INT64"),),
    lambda value: ((value + 1,),), argument_names=("value",),
)
with connect(":memory:", extensions=ExtensionRegistry(
    trusted=True, procedures=(procedure,),
)) as db:
    assert db.execute("CALL app.increment", {"value": 4}).rows == ((5,),)
    assert db.execute("CALL app.increment(4)").columns == ("next_value",)
    assert db.execute("CALL app.increment(4) YIELD *").rows == ((5,),)
```

## Invocation and output rules

| Syntax | Contract |
| --- | --- |
| `CALL app.proc(...)` as the entire query | Return all declared columns in declaration order; a unit call returns no columns/rows |
| `CALL app.proc` as the entire query | Bind parameters with the registered argument names; do not substitute NULL for a missing name |
| `CALL app.proc(...) YIELD *` as the entire query | Expand all declared columns; a unit procedure has none and refuses |
| Standalone named `YIELD field AS alias, ...` | Return the selected columns in written order with the written aliases |
| Named `YIELD ... WHERE ...` or standalone `YIELD * WHERE ...` | Filter after output bindings exist |
| In-query `CALL app.proc(...) YIELD field ... RETURN ...` | Explicit arguments and named outputs; compose with MATCH, UNWIND, WITH, UNION and subqueries |
| In-query implicit arguments or `YIELD *` | Refused during planning; an explicit RETURN makes a call in-query, even with no preceding MATCH |

Standalone means the outermost statement is one CALL without an explicit RETURN.
A CALL embedded in a subquery or UNION branch cannot use implicit argument
passing. Unit calls with explicit arguments may still appear inside pipelines or
terminate them without returning rows. Names introduced by YIELD must not collide
with preceding bindings or other output aliases. To sort or further compose a
tabular call, name its outputs and use an explicit RETURN/ORDER BY.

Output expansion is native signature resolution before lexical/type analysis,
not a callback preview. No callback runs for explain, missing permissions, invalid
arity or unsupported invocation mode. The original parsed syntax is not mutated;
plans are resolved independently for each handle's immutable authorized registry.
Returned columns are identical through execute and read cursors. Declaration
budgets and all-column output validation remain active.

The parser records `ProcedureCall.implicit_arguments`, `yield_all` and `standalone`
as exact boolean fields. Native `build_plan(..., procedures=...)` resolves them
before analysis and does not trust a stale caller-supplied analysis after expansion.
The domain `resolve_procedure_calls` helper is callback-free, validates canonical
AST structure first and preserves literal payloads without invoking their methods.
A bare registry-free `analyze` is not a substitute for signature-aware planning.

## Error and safety contract

Native `GrafxPlanError` exposes these planning-phase details:

| Field | Reason | Meaning |
| --- | --- | --- |
| `procedure` | `procedure_not_found` | Registration missing or required permissions not granted |
| `procedure_arguments` | `procedure_argument_mode` | Implicit arguments used outside a standalone call |
| `procedure_arguments` | `procedure_argument_names` | The descriptor lacks names for implicit positional inputs |
| `procedure_arity` | `procedure_arity` | Wrong number of positional arguments |
| `procedure_type` | `procedure_argument_type` | A statically known argument violates the declared type |
| `yield` | `procedure_yield_mode` | Wildcard output used inside a query |
| `yield` | `variable_already_bound` | Output alias collides with a visible variable |
| `parameter` | `missing_parameter` | A required query parameter name was not supplied |

Missing parameter **names**, including explicit `$parameter` uses outside CALL,
are rejected in pre-execution admission with `query_phase="planning"`; this does
not reclassify evaluated parameter-value or dynamic expression errors as compile
errors. The refusal occurs even when upstream rows would be empty and before any
callback/write operator. Existing `value` names remain present in parameter errors.
The TCK mapper requires the actual native field, reason and phase; it never derives
an expected error from a case name or an arbitrary exception.

Names/default outputs introduce no graph access or writing capability, store
switching, implicit commit, automatic callback retry, new storage format or new
runtime configuration field. Independent reader/writer and statement rollback
protocols are unchanged. Callback host effects are not transactional or sandboxed.
Implicit calls have the same value checks as explicit calls. The subsequent
[numeric signature increment](PROCEDURE_NUMERIC_SIGNATURES_V1.md) adds NUMBER
inputs/outputs and validated INT64-to-DOUBLE widening; scalar UDFs remain exact.
