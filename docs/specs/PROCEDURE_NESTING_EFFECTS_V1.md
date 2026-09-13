# Native procedure nesting and effect declarations — FP-7

Status: implemented on `feature/v0.0.6`, development only. Extends
[query authority](PROCEDURE_QUERY_AUTHORITY_V1.md), not full procedure parity.
[Qualification](../reports/FP7_NESTING_QUALIFICATION.md).

## Native nesting and recursion

`ProcedureReader.query()` and `ProcedureWriter.query()` can invoke registered
procedures, including self-recursion, mutual recursion, native subqueries and
read-only EXISTS bodies. `ProcedureWriter.execute()` can invoke writing unit
procedures: a standalone unit CALL's synthesized empty RETURN is result-free.
It still refuses actual output columns; use query() when results are needed.

Every native child CALL resolves the connection's immutable registry and its
permissions. A ProcedureReader cannot invoke a writing procedure, even inside a
write transaction or a zero-row write query. Read-only EXISTS still rejects writes.
A child cannot reenter an ancestor's busy authority object; use its own supplied
authority. Denied capabilities, callback/output/cleanup failures and caught
operation failures preserve poisoning and the outer rollback boundary.

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

def factorial(reader, n):
    result = 1 if n <= 1 else n * reader.query(
        "CALL app.factorial($n)", {"n": n - 1}
    ).rows[0][0]
    return ((result,),)

procedure = TabularProcedure(
    "app.factorial", ("INT64",), (("value", "INT64"),), factorial,
    graph_read=True, required_permissions=frozenset({"graph"}),
    max_call_depth=8, deterministic=True,
)
extensions = ExtensionRegistry(trusted=True, procedures=(procedure,),
                               procedure_permissions=frozenset({"graph"}))
with connect(":memory:", extensions=extensions) as db:
    assert db.execute("CALL app.factorial(6)").rows == ((720,),)
```

All callbacks remain trusted synchronous Python code. Native nesting does not
control arbitrary Python recursion or revoke unrelated objects a host callback
captured itself. No native authority can commit, select another store, acquire
another writer or remain usable after its invocation stream closes.

## Depth, budgets and transaction ownership

`TabularProcedure.max_call_depth` defaults to **8**, accepts exact integers
**1..16** (not bool), and counts registered callback invocations in the active
native call chain. The outermost callback is depth 1; ordinary child queries or
Cypher subqueries without another registered CALL do not add callback depth.
Sibling pipeline calls are separate invocations, not recursive descendants.

Admission uses the minimum limit declared by every active ancestor and the next
callee, so a callee with limit 16 cannot override its parent's limit 2. Depth is
checked before invoking the next callback. Exceeding it raises
GrafxQueryBudgetExceeded with `resource="procedure_call_depth"`, `limit` and
`observed`. Native query syntax/operator limits still apply independently.

The complete chain shares its outer transaction, snapshot, temporal clocks,
cancellation, private writes and rollback. Native statement-write and traversal
budgets are charged to the root statement, not reset at an intermediate child.
Intermediate-row accounting also stays shared. Per-procedure query counts,
query-result row/byte quotas, write-statement quotas and writing-callback output
quotas accumulate across recursive and sibling invocations of that registered
name. Different procedures keep their declared per-name quotas, but cannot evade
the inherited depth or root native budgets by alternating names.

Result cells crossing multiple native query boundaries are admitted/charged at
each boundary. A nested native CALL can count as a query/write statement of its
caller in addition to the callee's own operations. Discarding results or catching
a failure does not refund quota. A new outer statement starts fresh counters;
prior successful transaction statements are not rolled back by a failed chain.

Owner-private entity references survive nested query results, including entities
created at a deeper level. Each boundary issues its own observation witnesses;
foreign/copied DTOs still are not authority. Successful child deletions invalidate
ancestor content aliases. Independent readers/writers can progress while a nested
callback waits; the chain keeps its original snapshot.

## Determinism and native effects

`TabularProcedure.deterministic` is an exact bool, default **False**. This is a
deliberate change from implicitly treating every read procedure as deterministic.
Declare True only when the callback's outputs depend solely on its arguments and
authorized fixed-snapshot input, with no observable volatile/external behavior
whose execution frequency matters. Host-code truthfulness is a trusted contract,
not verified by introspection or a Python sandbox.

| Declaration | Native effect / optimization contract |
| --- | --- |
| `mode="read", graph_read=False` | No engine-issued graph access; volatile by default |
| `mode="read", graph_read=True` | Permissioned reads only; volatile by default |
| Read mode with `deterministic=True` | Opt-in deterministic promise; native child queries must also pass deterministic effect admission |
| `mode="write"` | Permissioned graph writes; deterministic=True is refused at registration |

The registry supplies write/result/determinism metadata to the AST before native
analysis. User-supplied AST flags cannot override a registered descriptor. The
optimizer no longer treats an undeclared read callback as safe for deterministic
expression reuse. The declaration does not promise that a callback will be called
exactly once, always memoized or automatically retried.

A declared deterministic graph reader refuses child queries containing functions
or procedures classified as volatile, before execution. An undeclared callee
remains volatile even if its Python implementation happens to return a constant.
The current native effect classifier conservatively classifies the temporal
function family as volatile, including constructors; use the default False for
such callbacks. This does not disable temporal functionality, change its values,
or relax the finite-value/storage contract. Scalar UDF declarations are unchanged.

## Configuration, upgrades and remaining scope

These two fields are descriptor options, not global connect settings. Set a smaller
depth for callbacks that do not need long chains; raise it only with corresponding
query/output/native budgets. Keep deterministic=False for clocks, randomness,
external I/O and unknown callbacks. Review aggregate/expression consumers that
previously relied on implicit determinism: project volatile results through WITH
before aggregating, or truthfully declare determinism where appropriate. There is
no legacy mode and no automatic Pulse extension registration change.

The subsequent [schema authority contract](PROCEDURE_SCHEMA_AUTHORITY_V1.md) adds
explicit CREATE DDL and implicit schema with separate permissions and quotas.
Transaction control, arbitrary module import and backend-specific built-in
procedure emulation are not supplied by these increments. FP-6 parameterized storage,
pending FP-4 policy decisions and full FP-8/Pulse/competitor qualification remain
outstanding. No persisted format/tag/capability or durability policy changes.
