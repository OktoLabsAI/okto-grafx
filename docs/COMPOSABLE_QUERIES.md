# Composable query execution (0.0.6 development)

[Query reference](QUERY_LANGUAGE.md) · [Fixed compatibility contract](CYPHER_COMPATIBILITY.md)

This documents the locally validated implementation, not full Cypher conformance
or a published release. The eight-item round's feature tests, complete Grafx
regression and affected Pulse migration tests passed; see the
[acceptance record](reports/V006_QUERY_LANGUAGE_ROUND.md) for exact coverage.

## Clause order and scope

Clauses execute in written order. `WITH` projects a new scope: omitted names are
not available later. Its expressions all read the incoming scope, so aliases do
not become visible to neighboring expressions in the same projection. Reusing a
name in a later scope is supported, including entity renaming. `UNWIND` can follow
`WITH`/`MATCH` and can be repeated; a NULL carrier, like an empty list, emits no
rows. Non-list, non-NULL carriers are refused, not iterated as Python strings/maps.

```cypher
WITH [1, 2] AS first, [3, 4] AS second
UNWIND first AS a
UNWIND second AS b
WITH a + b AS total
RETURN DISTINCT total ORDER BY total
```

`WITH` supports DISTINCT, aggregation, ORDER BY, SKIP, LIMIT and WHERE. A later
MATCH consumes its actual output, including its row window. Typed OPTIONAL MATCH
can correlate on incoming values and include multiple patterns: the complete
optional clause (including WHERE) succeeds or all its new bindings become NULL.
Multiplicity is preserved; optional joins can multiply rows. A subsequent mandatory
MATCH does not turn a NULL entity binding into a new unbound scan.

## Values and breaking semantics

| Construct | Contract |
|---|---|
| `xs[i]` | Zero based, negative from end, out-of-range/NULL yields NULL; BOOL is not an integer index |
| `xs[start..end]` | Half-open slice; negative bounds, clipped bounds and omitted ends; explicit NULL bound yields NULL |
| Maps | Exact case-sensitive string keys; absent property/key yields NULL |
| `split(s, separator)` | Preserves all empty fields; empty separator splits code points; `split('', '')` returns a one-element empty-string list |
| `string_split` | Alias of the same split policy, not a legacy empty-token/index mode |
| CASE/COALESCE | Lazy runtime selection, heterogeneous results allowed, no implicit conversion of the chosen INT64 to DOUBLE |
| Equality | Recursive three-valued list/map equality; NULL is not equal to NULL in predicates |
| DISTINCT/grouping | NULLs group together; numerically equal integers/floats share a key; BOOL remains distinct; spill uses the same equality |
| List ordering | Lexicographic; an earlier unequal element decides before later NULLs; an undecidable compared element yields NULL |
| Heterogeneous sorting | Lists precede strings, then booleans, then numbers; NULL sorts last ascending; min/max use the same total keys, not Python container repr |
| Operator precedence | IS NULL/IS NOT NULL bind above comparison and below arithmetic; exponentiation associates left-to-right, with signs binding above exponentiation |
| SUM/AVG | NULL inputs are ignored; integer-only SUM retains exact integers instead of rounding through DOUBLE; empty/all-NULL SUM is 0 and AVG is NULL; nonnumeric non-NULL inputs refuse |

Static name checks cover all branches. Provably invalid operand types and invalid
bound parameters are refused before row production where type information is
available; heterogeneous row-dependent values are checked when evaluated. A query
error discards its own logical writes even if the caller catches the exception
and subsequently commits earlier successful statements.

### List-local expressions

`[x IN xs WHERE predicate | expression]` filters and maps a list. Either WHERE or
the mapping may be omitted; omitted mapping returns the selected elements. A NULL
source returns NULL, and NULL predicates do not select elements. `all`, `any`,
`none` and `single` use `name(x IN xs WHERE predicate)` and three-valued boolean
logic, with short-circuit evaluation where the answer is determined. On an empty
list their results are true, false, true and false respectively.

`reduce(acc = initial, x IN xs | expression)` carries the previous result as `acc`.
Empty input returns `initial`; NULL input returns NULL. Binders are lexical and may
shadow outer names without modifying or exporting them. Nested binders receive
independent identities. Row aggregates are not permitted in the local predicate or
body (aggregate the input list first). Input lists can themselves be aggregates.

The `+` operator concatenates lists or prepends/appends a scalar to a list, without
flattening nested elements. NULL on either side returns NULL. Concatenation and
iteration are bounded to 100,000 elements; nested list iterations additionally share
a 100,000-step statement budget and participate in cooperative read cancellation.
These are fixed safety bounds, not a compatibility switch.

## UNION and subqueries

Up to 64 read-only RETURN branches can be combined. Every branch must publish
the same column names in the same order (use explicit aliases). Values may differ
in type: UNION ALL does not convert `1` into `1.0`. UNION deduplicates with numeric
equality; ALL preserves multiplicity. Each branch's sort/window is local. Mixed
UNION/ALL operators associate left-to-right; this mixed-chain policy is a Grafx
extension, not blanket openCypher TCK conformance. No updating UNION branches.

UNION currently publishes scalar/list/map values, not graph entities (including
entities nested in lists/maps). Project stable application keys or properties
instead. This prevents table-local record identifiers from being confused across
tables at the public result boundary; list slicing does not bypass that check.

```cypher
RETURN 1 AS value
UNION ALL RETURN 'two' AS value
UNION ALL RETURN NULL AS value
```

`CALL { ... RETURN ... }` is an independent read subquery. The explicit import
form `CALL (name, ...) { ... RETURN ... }` evaluates once per incoming row. Only
imported names are visible inside; only returned names escape. An output name must
not collide with an existing outer binding. Zero inner rows remove the incoming
row. Both levels share the same transaction/snapshot and execution limits.

```cypher
MATCH (p:Person)
CALL (p) {
  MATCH (q:Person) WHERE q.age > p.age
  RETURN count(*) AS older
}
RETURN p.id, older
```

Subqueries are returning and read-only in this implementation; unit/writing
subqueries and implicit imports via a leading WITH are not supported. Nesting is
limited to 16 subqueries, alongside the existing token/expression/plan limits.
This CALL syntax is documented separately from the pinned 2024.3 semantic target
and from tabular procedure calls below.

## Typed tabular procedures

```python
from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure

words = TabularProcedure(
    name="app.words",
    argument_types=("STRING",),
    columns=(("word", "STRING"), ("length", "INT64")),
    implementation=lambda text: ((word, len(word)) for word in text.split()),
    required_permissions=frozenset({"tokenize"}),
)
registry = ExtensionRegistry(
    trusted=True,
    procedures=(words,),
    procedure_permissions=frozenset({"tokenize"}),
)
with connect(":memory:", extensions=registry) as db:
    result = db.execute(
        "CALL app.words($text) YIELD word AS w, length AS n "
        "WHERE n > 1 RETURN upper(w), n", {"text": "a longer word"}
    )
```

Names must be explicitly namespaced and registered on this handle. YIELD names
must exist in the declared schema; aliases cannot collide with incoming bindings.
YIELD WHERE filters after binding. Named YIELD is required; YIELD * is not supported.
A standalone procedure without RETURN returns its yielded columns. Calls compose
with UNWIND, MATCH, WITH, UNION and read subqueries, and work through execute,
explain and read cursors. Cursor close closes the callback iterator.

Arguments and cells use exact BOOL, INT64, DOUBLE, STRING, BYTES, TIMESTAMP or UUID
types. NULL cells/arguments are allowed (callbacks must handle NULL themselves).
No graph handle, implicit database write capability, arbitrary module import,
persisted executable code, sandbox or transactional external side effects are
provided. These are **trusted pure tabular callbacks**, not graph-writing procedures.
The host is responsible for their purity and termination. Permission sets are
host-configured allowlists, not user authentication or operating-system isolation.

| Registration option | Default | Meaning / limit |
|---|---:|---|
| `required_permissions` | empty frozenset | All names must be granted on this handle before planning/execution |
| `max_rows` | 10,000 | Per callback invocation; 1..2^31 |
| `max_result_bytes` | 8 MiB | Encoded result cells per invocation; 1..2^31 |
| `max_value_bytes` | 1 MiB | Per argument/result value; 1..2^31; strings conservatively charged at four bytes per code point |
| Registry size | 128 | Maximum procedures per immutable registry |
| Signature size | 32 / 64 | Maximum arguments / output columns (at least one output) |

Wrong signatures/permissions fail before callback invocation. Wrong result arity,
type and callback exceptions are typed `GrafxPlanError`; resource violations are
`GrafxQueryBudgetExceeded`. Every returned cell is checked, including columns not
selected by YIELD. Cleanup failures are typed and do not overwrite an earlier
query failure. Global intermediate/result-row and transaction quotas still apply
across invocations. Cooperative query cancellation cannot preempt blocking host
code inside an arbitrary callback.

The existing standalone `grafx.search_text` procedure retains its documented
specialized contract; registering tabular callbacks does not broaden that syntax.

## Write and recovery boundaries

WITH separates a write phase from subsequent reads. Reads see the phase's held
changes through the transaction's own view; no intermediate COMMIT is issued.
Creating an edge between newly created endpoints can stage earlier phases as private
transaction intents to obtain authenticated endpoint identities. These intents are
invisible to other participants. One statement rollback boundary spans every phase,
including final result validation; a failure preserves earlier successful statements
in the same transaction and discards every effect of the failing statement.
If discarding those effects itself fails and rollback cannot be proven, the native
transaction is aborted instead; earlier uncommitted statements cannot then be
committed. The error remains visible rather than allowing an ambiguous partial
commit or silently retrying an effectful statement.
Statement write quotas accumulate across phases rather than restarting at each one.
Property updates to a newly staged relationship retain the exact authenticated
endpoint promises of its original insert; replacing an endpoint, supplying an
equal-but-unissued token, or resurrecting a deleted pending edge is refused.
SET updates the bindings observed by subsequent clauses and RETURN, including aliases
of the same entity. Newly created relationships can be named, updated and deleted.
Plain DELETE of a node with surviving incident relationships is refused. Use
DETACH DELETE or explicitly delete those relationships in the same instruction.
Incident-table reads participate in OCC to prevent concurrent edge phantoms.

The storage/WAL format, commit durability, writer fencing and read snapshots are
unchanged. No setting restores the old language semantics. Writing CALL subqueries
and graph-writing host procedures remain unsupported; ordered native write clauses
do not grant callbacks a database handle or an external-side-effect rollback guarantee.
