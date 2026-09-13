# Transaction-scoped writing procedures — FP-7 development

Status: bounded native implementation on `feature/v0.0.6`, not a release or full
FP-7 completion. This extends [CALL invocation](PROCEDURE_INVOCATION_V1.md) and
[numeric signatures](PROCEDURE_NUMERIC_SIGNATURES_V1.md). No persisted layout,
capability bit, recovery rule or independent-reader/writer policy changes.

## Registration and consumption

`TabularProcedure.mode` is exactly `"read"` (default) or `"write"`. Read mode
keeps the pure host-callback contract and receives only declared arguments.
Write mode requires at least one named `required_permissions` entry; all must
also be present in `ExtensionRegistry.procedure_permissions` on this connection.
The callback receives an engine-created `ProcedureWriter` **before** its declared
positional arguments. This extra capability is not a Cypher argument or a column.

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, ProcedureWriter, TabularProcedure

def increment(writer: ProcedureWriter, identity: int):
    writer.execute("MATCH (n:Counter {id:$id}) SET n.value = n.value + 1", {"id": identity})
    return ((identity,),)

extensions = ExtensionRegistry(
    trusted=True,
    procedures=(TabularProcedure(
        "app.increment", ("INT64",), (("id", "INT64"),), increment,
        mode="write", required_permissions=frozenset({"update_counters"}),
        max_write_statements=32,
    ),),
    procedure_permissions=frozenset({"update_counters"}),
)
with connect(":memory:", extensions=extensions) as db:
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Counter (id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:Counter {id:1, value:0})")
        assert tx.execute("CALL app.increment(1) YIELD id RETURN id").rows == ((1,),)
        assert tx.execute("MATCH (n:Counter) RETURN n.value").rows == ((1,),)
    # Only the transaction owner above commits, after the callback has expired.
```

Use an empty `columns=()` for a unit callback returning exactly None. Tabular
callbacks keep typed nullable outputs, NUMBER/DOUBLE rules, implicit argument
names, standalone output expansion and named YIELD. Validate all columns, even
ones not selected. Writing CALL composes inside ordinary returning/unit subqueries
and updating UNION. LIMIT/WHERE on its outputs do not execute only a prefix of
the graph mutations. EXPLAIN never runs the callback and reports
`access="transaction_write"`, `effects=("graph_mutation",)`.

## Exact mutation door and present limits

`ProcedureWriter.execute(query, parameters=None) -> None` accepts one native,
result-free graph mutation: CREATE, MERGE, SET, DELETE/DETACH DELETE and their
supported native query composition. Parameters must be an exact Python dict
(or None); values are detached and validated through the native parameter contract.
The operation runs immediately in the **same transaction and snapshot**. Later
operations and subsequent outer reads observe its private changes.

The execute() door does not return query rows and rejects actual RETURN columns,
read-only queries, DDL and UNION at its root. Writing unit CALLs are now admitted
by the [native nesting contract](PROCEDURE_NESTING_EFFECTS_V1.md), including their
synthetic empty RETURN. Without the subsequent [schema opt-in](PROCEDURE_SCHEMA_AUTHORITY_V1.md),
node tables and relationship endpoint pairs must already be declared and implicit
creation is refused. Schema-enabled writers may use native flexible CREATE/MERGE
and the separate schema() DDL method. Existing flexible property bags hold their
supported native values. This is not arbitrary writing-procedure parity. Broader read/result
capabilities are added by [native query authority](PROCEDURE_QUERY_AUTHORITY_V1.md):
writer.query() returns bounded native results, including returning writes, and
graph_read supplies a permissioned reader. Native recursion/effect declarations
are covered by the nesting contract; schema authority has separate explicit limits. The subsequent
[entity signatures](PROCEDURE_ENTITY_SIGNATURES_V1.md) now qualify invocation-local
NODE/RELATIONSHIP/PATH and typed entity lists for both callback modes.
The subsequent [native value signatures](PROCEDURE_NATIVE_VALUES_V1.md) cover
temporal, LIST/MAP/ANY and vector arguments/results for both modes. Do not
substitute a captured Database handle to bypass the remaining authority limits.

## Authority, failure and visibility

- Registration is immutable and connection-local. Signature resolution derives
  write effects from the authorized registration, not a caller-controlled AST hint.
  It occurs before native read-only admission and the public result rollback guard.
- A writing CALL is refused in a read transaction even with zero input rows.
  Read cursors never stream a write prefix. Use `tx.execute` in an explicit writer.
- The supplied object exposes no commit, rollback, second transaction, store
  selection, WAL, file, page, lock or lease door. Cross-thread use, reentrant use
  and use after expiration are refused with `field="procedure_authority"`.
- Output is fully consumed and validated, including generator cleanup, before any
  row goes downstream. Successful generator finalization can still use the writer;
  a failed/cancelled invocation revokes it. The writer expires before downstream
  query evaluation and on every failure, releasing its engine-operation closure.
- Any failed mutation poisons this invocation: catching that error inside the
  Python callback cannot make its earlier mutations commit. Callback, output,
  cleanup and public-result failures roll back **the entire outer statement**,
  preserving successful earlier statements in that transaction. Normal explicit
  transaction rollback still discards all its statements.
- Native Grafx failures retain their typed error; ordinary host callback/cleanup
  failures become `GrafxPlanError`. A cleanup failure never replaces a primary
  failure. No automatic callback retry or independent commit is introduced.
- Other participants retain native snapshot reads and independent writes. These
  writes still undergo the ordinary OCC validation, WAL, durable COMMIT proof
  and recovery. Concurrent conflicting work may still raise a write conflict;
  procedure registration is not an isolation bypass or a retry guarantee.

Python callbacks remain **trusted code, not a sandbox**. Captured host resources,
network/filesystem effects, manually acquired handles and arbitrary reflection are
outside this supplied capability and are not rolled back. Callbacks must not use
those mechanisms to reenter Grafx or change transaction lifecycle. Native code
cannot forcibly preempt a blocking host callback. No wall-clock timeout promise is
added by this increment.

## Configuration and shared budgets

These options belong to the trusted Python descriptor, not DatabaseConfig, a file,
CLI switch or persisted executable code. Supply the registry on each connection.

| Field | Default / admission | Writing scope |
| --- | --- | --- |
| `mode` | `"read"`; exact `"read"` or `"write"` | Explicit effect/authority declaration |
| `required_permissions` | Empty for read; nonempty required for write | All names must be granted on this handle |
| `max_write_statements` | 128; exact int 1..1024, not bool | Per procedure name across **all invocations in one outer statement** |
| `max_rows` | 10,000; existing registration bounds | Both per invocation and summed across writing invocations |
| `max_result_bytes` | 8 MiB; existing registration bounds | Encoded output bytes, summed across writing invocations |
| `max_value_bytes` | 1 MiB; existing registration bounds | Every argument and every output cell |

`max_statement_writes` includes outer mutations and every nested mutation; it is
not reset by each capability call. Native traversal counters are shared with the
outer statement. Intermediate-row counters share the outer operator inventory,
including repeated prepared nested plans. Existing per-operator query-memory,
spill and native transaction-wide intent/page budgets remain in force. These are
logical budgets, not a bound on arbitrary callback allocations or CPU time.

Invalid registration raises GrafxConfigurationError. Unsupported mutation syntax
uses `GrafxPlanError(field="procedure_write_query")`; non-dict parameter containers
use `procedure_write_parameters`. Shared operation overflow uses
`GrafxQueryBudgetExceeded(resource="procedure_statements")`; cumulative output
overflow uses `resource="procedure_outputs"`. Native statement-write overflow
remains `GrafxTransactionBudgetExceeded(field="max_statement_writes")`.

## Qualification

Feature cases: `tests/api/test_writing_procedures.py`. The initial 178-test
extension/TCK-fixture selection passes (`.grafx-tmp/fp7-writing-focused.xml`).
The expanded query/transaction selection passes 357 tests. Combined acceptance
passes 3,543 feature/cache/public-contract tests; the final authority-argument
hardening passes 139 affected tests, including all 47 current writing feature
cases. These overlapping selections and source boundaries are recorded in
[the qualification report](../reports/FP7_WRITING_QUALIFICATION.md).
Original upstream procedure cases cover the earlier read/tabular feature family;
they are not a substitute for these additional native authority/failure tests.
