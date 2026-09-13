# Native procedure entity signatures — FP-7

Status: implemented in the `feature/v0.0.6` development line. This extends the
[native value contract](PROCEDURE_NATIVE_VALUES_V1.md), not complete procedure
parity. [Qualification and remaining scope](../reports/FP7_ENTITY_QUALIFICATION.md).

## Signatures and callback representation

| Signature | Non-NULL callback input/output | Native downstream semantics |
| --- | --- | --- |
| `NODE` | `NodeValue` observation issued by this invocation | Node properties, identity, projection and authorized query writes |
| `RELATIONSHIP` | `RelationshipValue` observation issued by this invocation | Relationship identity, endpoints, properties and query writes |
| `PATH` | `PathValue`, with registered node/relationship components | Native path functions, identity and projection |
| `LIST<NODE>` | Owned list of witnessed NodeValue or NULL | Typed UNWIND into native node bindings |
| `LIST<RELATIONSHIP>` | Owned list of witnessed RelationshipValue or NULL | Typed UNWIND into native relationship bindings |

Every signature admits NULL. Generic `ANY`, `LIST` and `MAP` also carry these
entities and nested paths, but do not infer a more specific static entity/list
type. Use the explicit signatures when downstream analysis needs that proof.
There is no new persisted entity tag or stored `LIST<T>` column type.

## Identity, authority and lifetime

The engine exports detached observations, **not** its row bindings, snapshot,
transaction or database handle. Each observation is associated privately with its
native reference for exactly one input-row invocation. Returning it restores that
reference before downstream query execution. Committed and provisional identities
use the existing [entity contract](../ENTITY_VALUES.md); uncommitted nodes remain
the same logical nodes, not duplicate records or guessed numeric IDs.

Only exact objects issued by the current invocation are accepted. A reconstructed
object with identical metadata, an entity read from another connection/database,
or an object retained from an earlier invocation is refused. Path components are
registered too: a callback may return `path.nodes[0]` as a NODE column. Lists/maps
may be reorganized or copied while retaining their witnessed entity elements.

Returned observations are refreshed from the caller's private overlay, not from
callback-supplied metadata/properties. Changing an observation with Python's
low-level mutation facilities cannot redirect a native write or supply new stored
properties. Deleted content follows the existing native deleted-entity rules.
Witnesses are cleared on exhaustion, failure and cursor/iterator closure.
As with other trusted extensions, this is a contract boundary, **not a sandbox**
against arbitrary Python code with access to process internals.

Direct `procedure.invoke()` has no native observation scope and rejects non-NULL
entity objects. Public query parameters and `ProcedureWriter.execute` parameters
do not acquire entity authority from this feature. The invocation resolver is an
engine integration hook, not an application-supplied graph access API.

## Example

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

def choose(node):
    # Read detached properties and return this invocation's observed entity.
    return ((node,),) if node.properties["value"] >= 10 else ()

extensions = ExtensionRegistry(trusted=True, procedures=(TabularProcedure(
    "app.choose", ("NODE",), (("chosen", "NODE"),), choose,
),))
with connect(":memory:", extensions=extensions) as db:
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:T {id:1,value:10})")
        result = tx.execute("MATCH (n:T) CALL app.choose(n) YIELD chosen "
                            "SET chosen.value=11 RETURN chosen.id, chosen.value")
        assert result.rows == ((1, 11),)
```

A `mode="write"` procedure may receive/return entities too. Its separate
[ProcedureWriter](WRITING_PROCEDURES_V1.md) remains permissioned, thread-bound,
revocable and unable to commit independently. All output columns, even unselected
columns and late rows hidden by LIMIT, are validated before publishing callback
writes. A failed statement rolls back its own effects, preserving prior successful
statements in the transaction. OCC, WAL and durable COMMIT rules are unchanged.

## Budgets, configuration and limitations

Use the existing descriptor `max_value_bytes`, `max_result_bytes`, `max_rows` and
write-mode `max_write_statements`. No new global configuration field is added.
Entity properties are fully materialized under native container/depth and value
byte limits before the callback. A node/relationship costs a logical 256-byte
observation header plus label and property values; a path costs 64 bytes plus all
component occurrences. These are admission/accounting units, not a serialized
entity format or an exact Python heap estimate. Repeated occurrences are charged;
path components are admitted incrementally before allocating the next observation.
Writing procedures also retain their cumulative output budget across input rows.

Large entities can exceed a small value budget even if the callback would inspect
only their ID. Pass scalar properties instead when complete observations are not
needed. Generic collections preserve entity values but do not promise inferred
static entity types. Typed LIST<PATH> and arbitrary detached-entity attachment are
not implemented here. The subsequent [schema authority](PROCEDURE_SCHEMA_AUTHORITY_V1.md)
adds explicit native DDL/implicit schema permissions. [Query authority](PROCEDURE_QUERY_AUTHORITY_V1.md)
adds permissioned native queries/results and witnesses their returned entities.
[Native nesting](PROCEDURE_NESTING_EFFECTS_V1.md) additionally preserves those
witnesses across recursive procedure query results, with inherited limits/effects.
FP-6 parameterized stored types and
the full FP-8/Pulse qualification remain separate outstanding work.
