# Unit procedures — FP-7 increment

This increment describes default read/pure callbacks. Subsequent explicit
`mode="write"` unit/tabular callbacks use the separate
[writing authority contract](WRITING_PROCEDURES_V1.md).

Status: native explicit-invocation increment implemented; 513 affected regression
tests pass. [Full baseline and subsequent family evidence](../reports/FP_FULL_PROFILE_20260912.md).
This is part of [functional parity](FUNCTIONAL_PARITY_PLAN.md#fp-7--writing-procedures-within-transaction-authority),
not completion of FP-7 or authorization for unscoped callback writes.

## Registration and invocation

An explicitly empty `TabularProcedure.columns` tuple declares a **unit**
procedure. Its trusted callback accepts the same exact nullable scalar arguments
as a tabular procedure and must return exactly `None`. A tuple, iterable,
generator or arbitrary object is not an empty result: it is a contract error.
No returned generator is executed to infer the callback's intent.

`CALL app.observe(1)` is the explicit invocation syntax. Arguments retain the
existing arity, native value, type and size checks. Registration and permission
resolution precede callback execution, including when an upstream branch is
empty. Unit procedures have no columns to expose with `YIELD`; attempting to
yield a column is rejected before invocation.

A standalone unit call executes once and returns no columns and no result rows.
It may also terminate a multi-clause pipeline without a RETURN clause.
Inside a query it executes once per incoming row and, on success, preserves that
row without adding bindings. Empty input executes it zero times. NULL is passed
to the callback; unlike scalar UDFs, it does not short-circuit the invocation.
A downstream limit or cursor close may leave later input rows unconsumed.

The callback result is not a row stream, so `max_rows` and `max_result_bytes`
remain validated descriptor fields but do not bound the number of incoming
invocations. Existing query budgets and cooperative cancellation remain active;
`max_value_bytes` still checks arguments. No new `DatabaseConfig` field, storage
format, persisted executable code or registry discovery is introduced.
The existing plan API reports `ProcedureRows.details()["access"] == "pure_unit"`
and empty output columns; explain never invokes the callback. A non-returning
call uses `execute()`, not the read-result cursor API.

## Failure and authority

Callback exceptions are `GrafxPlanError(field="procedure_callback")`; a non-None
return is `GrafxPlanError(field="procedure_result")`. Invalid arguments or denied
permissions do not invoke the callback. An error in a later invocation rolls back
the enclosing statement's graph effects, preserving earlier successful statements
in the same transaction. Closing an unfinished result releases its read snapshot.

The host must provide trusted, thread-safe callbacks with no external effects and
must not re-enter Grafx. This increment supplies **no transaction capability** and
does not permit a callback to write graph data. Python closures are not sandboxed:
external effects are not undone, callback code cannot be forcibly interrupted,
and callbacks are not automatically retried by the procedure operator. A caller's
explicit transaction retry can execute them again.

## Remaining FP-7 work

The subsequent [invocation contract](PROCEDURE_INVOCATION_V1.md) adds implicit
argument names, standalone implicit tabular outputs and standalone `YIELD *`.
Broader numeric/entity/value signatures and transaction-scoped writing capabilities
remain separate work. This contract does not mark any frozen TCK case as excluded or
change the required profile. Qualification must retain both the original baseline
and the affected native/TCK results.
