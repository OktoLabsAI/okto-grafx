# Procedure schema authority (0.0.6 development)

Status: implemented native capability; evidence is recorded in
[the schema qualification receipt](../reports/FP7_SCHEMA_QUALIFICATION.md).
This is not full Cypher compatibility or installed Pulse qualification.

## Registration and consumption

`TabularProcedure(..., mode="write", schema_write=True,
required_permissions=frozenset({"schema"}))` requests schema authority in addition
to ordinary graph writes. `schema_write` defaults to `False` and accepts an exact
boolean. The literal permission `schema` must appear in the immutable descriptor
permission set **and** be granted by the connection's ExtensionRegistry. Other
declared permissions must also be granted. Read mode cannot request schema.

The engine-issued `ProcedureWriter.schema(query: str) -> None` accepts one native
`CREATE NODE TABLE`, `CREATE REL TABLE`, `CREATE VECTOR SPACE` or `CREATE INDEX`
statement. It has no parameter argument or result rows. Names are parsed normally;
there is no string-based dispatch to host methods. Use `writer.execute()` for
result-free DML and `writer.query()` for native materialized results.

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

def seed(writer):
    writer.schema("CREATE NODE TABLE Item(id INT64, score INT64, PRIMARY KEY(id))")
    writer.execute("CREATE (:Item {id:1,score:10})")
    writer.schema("CREATE INDEX item_score FOR (n:Item) ON (n.score)")
    return writer.query("MATCH (n:Item {score:10}) RETURN n.id").rows

extensions = ExtensionRegistry(
    trusted=True,
    procedures=(TabularProcedure(
        "app.seed", (), (("id", "INT64"),), seed,
        mode="write", schema_write=True,
        required_permissions=frozenset({"schema"}),
        max_schema_statements=4,
    ),),
    procedure_permissions=frozenset({"schema"}),
)
with connect(":memory:", extensions=extensions) as db:
    db.ensure_identity_indexes()  # catalog-v2 prerequisite, outside the callback
    with db.begin("write") as tx:
        assert tx.execute("CALL app.seed() YIELD id RETURN id").rows == ((1,),)
```

This is a non-idempotent seed, not an automatic retry recipe. Native duplicate-name
and key validation still applies on a second call. No callback is run to discover
its signature or permissions.

## Implicit schema and visibility

Schema-enabled `execute()`/`query()` may create undeclared flexible node models,
unlabeled nodes and relationship endpoint groups through native CREATE/MERGE.
Heterogeneous properties use existing native storage, not callback-specific tables
or a sidecar. Ordinary writing capabilities still refuse implicit schema creation
without schema authority.

Each subsequent child query is planned against the current transaction catalog.
Returned entities retain invocation-witnessed authority and can be returned from
CALL or used by downstream native SET. Parent contexts adopt new definitions and
add new index authority without replacing generations already selected by the plan.

**A precompiled outer scan does not dynamically expand its table set.** If `New`
did not exist when the outer query was compiled,
`CALL app.seed() WITH 1 AS x MATCH (n:New) RETURN n` does not discover it in that
same compiled scan. Run MATCH in a subsequent child query or transaction statement,
or return the new entity from the procedure. General continuation replanning is
not implemented by this capability.

## Budgets, permissions and failure contract

| Descriptor option | Default / accepted values | Meaning |
| --- | --- | --- |
| `schema_write` | `False` / exact bool | Requires write mode and literal `schema` permission |
| `max_schema_statements` | `32` / exact int 1..1024 | Actual explicit/implicit schema operations, accumulated per procedure name across the outer statement |
| `max_write_statements` | Existing 128 / int 1..1024 | Also charged by each schema operation; a DML query and its implicit schema operations are separate charges |

These are trusted Python descriptor fields, not DatabaseConfig/environment
settings. Native row, staging, index-build and result budgets remain unchanged.
Admission precedes the affected operation's effects. Schema-limit exhaustion raises
`GrafxQueryBudgetExceeded` with resource `procedure_schema_statements`; the shared
write limit uses `procedure_statements`.

Every ancestor of a schema-enabled call must also declare schema authority. An
ordinary DML parent cannot escalate by calling a schema-enabled descendant even
if the connection grants that descendant's permissions. Permissions and depth are
checked before the descendant callback. Existing thread ownership, revocation,
reentrancy and poison rules cover `schema()` too: catching a failed capability
call in host code does not make the enclosing statement successful. Native parse,
plan and quota errors retain their taxonomy when re-raised after such a catch.

Schema uses a transaction-private catalog clone and a precise artifact journal.
Callback, output, cleanup and native failures discard the statement's schema/data
and unwind only its artifact claims. Earlier statements survive proven statement
rollback. Nested calls cannot independently commit. Other participants cannot see
an uncommitted definition through the durable catalog. Original-snapshot OCC and
the no-automatic-callback-retry policy remain intact.

## Composable custom indexes

In catalog v2, textual CREATE INDEX (also outside procedures) composes with
earlier/later DML and table DDL in one transaction. Existing tables are fenced
across all row partitions. Under the existing schema artifact publication section,
Grafx builds a complete durable-base generation under a fresh, unreachable nonce.
Only the creating transaction observes it. Pending inserts, updates and deletes
are added by the normal index/row WAL path at commit. New tables start with a
durably initialized empty generation.

Construction is synchronous foreground DDL and scans retained heap versions;
it is not a new O(N) step on ordinary reads or commits. Other commit publishers
wait while the schema publication section builds the shadow, as for existing
endpoint-identity DDL. That section is released before control returns to the
callback: it is not held for the callback or transaction lifetime. Independent
readers retain normal snapshot semantics; concurrent staging is not disabled.

Catalog commit is the sole publication point. A crash before COMMIT may leave an
unreachable nonced file; it does not publish an index or uncommitted rows. Cold
recovery accepts only committed catalog/data effects. There is no independent
transaction, intermediate user-data commit, weaker OCC or writer-policy change.
A foreign write before or after shadow construction makes a conflicting older
transaction lose instead of publishing an incomplete index.

Existing textual layouts retain their restrictions: hash, sparse_hash,
posting_hash and ordered (TIMESTAMP then STRING). Property indexes remain
node-only and require supported typed columns. A committed table whose schema
itself changed in the transaction is refused instead of being mistaken for a new
empty table. This is not arbitrary ALTER/index migration support.

Legacy catalog-v1 index activation still requires its dedicated transaction.
Before mixing index DDL with other work/procedure calls on such a store, run
`db.ensure_identity_indexes()` outside that transaction. Python maintenance
`db.create_index()` remains separately owned; it does not join the caller.
No private maintenance freshness seal was removed.

## Boundaries and upgrade

No arbitrary ALTER/DROP, plugin loader, cross-store transaction or callback COMMIT
is exposed. Host callbacks remain trusted code, not a sandbox; filesystem/network
effects are not rolled back. Blocking host code is not forcibly interrupted by
native quotas. Existing DDL validation and format prerequisites remain native.

Existing descriptors retain `schema_write=False`; consumers explicitly opt in and
grant permission. These fields add no stored procedure metadata or capability bit.
Format capabilities of the actual DDL/layout are still required: upgrade all
participants before activating one. FP-6 persisted parameterized types, pending
profile decisions and complete FP-8/Pulse qualification remain separate work.
