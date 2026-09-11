# Composable query execution (0.0.6 development)

[Query reference](QUERY_LANGUAGE.md) · [Fixed compatibility contract](CYPHER_COMPATIBILITY.md)

This documents the locally validated implementation, not full Cypher conformance
or a published release. The eight-item round's feature tests, complete Grafx
regression and affected Pulse migration tests passed; see the
[acceptance record](reports/V006_QUERY_LANGUAGE_ROUND.md) for exact coverage.

## Clause order and scope

Native `properties()`, `labels()` and `type()` consume entity bindings through
aliases, returning subqueries, UNION, path components, list selection and CASE.
Unknown entity elements remain part of heterogeneous list typing; they are not
discarded to infer a misleading primitive type. Dynamic arguments are checked
at invocation, preserving lazy CASE/zero-row behavior and statement rollback.
See [signatures and NULL rules](QUERY_LANGUAGE.md#native-entity-scalar-consumption).

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

`WITH *` carries every **currently bound named variable**, not every schema
column or a name discarded by an earlier projection. `WITH *, expression AS alias`
appends explicitly named values; the star must appear once, first. Duplicate
output names, including an explicit alias that collides with a carried name,
refuse. All explicit expressions read the incoming scope, not their neighbors.
For example:

```cypher
WITH 2 AS x
WITH *, x + 1 AS y
RETURN x, y
```

Star expansion shares existing DISTINCT/grouping, ORDER BY/window/WHERE and write
operators. With aggregates, carried variables are grouping keys. It preserves
entity bindings and optional NULLs, and works inside returning read subqueries
with **only their explicit imports**. An empty incoming scope contributes no
names. The existing 256-item projection ceiling applies **after expansion**;
exceeding it refuses before effects. It neither exposes internal scope identities
nor broadens named-path, polymorphic MATCH or subquery-write admission. No new
runtime configuration or persistent format is introduced.

### Polymorphic node read composition

Standalone node patterns without labels now compose in read pipelines:

```cypher
UNWIND [1, 2] AS wanted
OPTIONAL MATCH (n {id: wanted})
WITH wanted, n
RETURN wanted, label(n) AS table_name, n.id AS id
ORDER BY wanted, table_name
```

Each incoming row drives the union of node tables once. Multiple independent
patterns form the requested cross product, bounded by the existing intermediate
row and result/work limits. A typed first pattern retains its index access when
available. A new label-free pattern currently uses the all-node scan; broad
cross-table index selection is still FP-3 work, not an asserted optimization.

Rematching an already-bound polymorphic node (including aliases, explicit imports
and single-query subquery exports) preserves its table-qualified internal binding.
It does not rescan or merge same-ID nodes from different tables. Optional NULL
bindings remain NULL; a subsequent mandatory rematch filters them out. An optional
multi-pattern clause null-extends all of its new names only if the complete clause
fails. Inline property maps use ordinary equality and the same property typing as
WHERE: missing keys yield NULL; incompatible declared families still refuse.

These operators use the existing snapshot and transaction-private overlay; they
do not create another participant/transaction or expose another writer's pending
rows. Cursor/row budgets remain authoritative. Dynamic polymorphic writes,
general path/relationship-alternative expansion remains pending
under [FP-3](conformance/FP3_PROGRESS.md). Native typed/polymorphic node and edge
results now use [qualified detached values](ENTITY_VALUES.md), also through
lists/maps, cursors, sorting and DISTINCT. This is a breaking development contract,
not completed FP-3/Pulse acceptance.

## Values and breaking semantics

The FP-2 development checkpoint additionally preserves unaliased expression
headings from source text and admits general expression-result postfix composition
through planning and binding. See [query value and heading contracts](QUERY_LANGUAGE.md#values-and-python-mapping).
Previously synthesized headings such as `size(NULL)` now retain `size(null)` when
written that way. Use explicit aliases for application keys and UNION branches
whose source spellings differ. This does not change value evaluation or scoped
variable names, and does not imply completion of the remaining functional-parity plan.

FP-2 also adds adjacent-pair equality/order chains and nondeterministic `rand()`.
Use `WITH rand() AS r` to draw once for each incoming row and reuse `r`; separate
written calls remain separate, including when nested in aggregates or grouping
keys. No random result is cached across executions. Existing clause cardinality,
short-circuit, statement rollback and resource bounds remain in effect.

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
| Boolean operands | AND/OR/XOR/NOT require BOOL or NULL; invalid known/bound types refuse before rows, evaluated dynamic invalid types refuse with statement rollback |
| IN right operand | LIST/NULL only; static invalid types refuse at planning, invalid parameters before reads/writes, dynamic invalid values when evaluated; nested/NULL equality unchanged |
| Empty map keys | Empty quoted keys and empty string indexing work; missing keys yield NULL; duplicate keys and empty schema/variable names still refuse |
| Direct UNWIND range | Constant-size carrier; 100,000 consumed elements per input row, with existing row/cancellation budgets; LIMIT may consume a bounded prefix |
| List ordering | Lexicographic; an earlier unequal element decides before later NULLs; an undecidable compared element yields NULL |
| Heterogeneous sorting | Lists precede strings, then booleans, then numbers; NULL sorts last ascending; min/max use the same total keys, not Python container repr |
| Operator precedence | IS NULL/IS NOT NULL bind above comparison and below arithmetic; exponentiation associates left-to-right, with signs binding above exponentiation |
| SUM/AVG | NULL inputs are ignored; integer-only SUM retains exact integers instead of rounding through DOUBLE; empty/all-NULL SUM is 0 and AVG is NULL; nonnumeric non-NULL inputs refuse |

RANGE operand-type/step errors occur at evaluation, not static inference: empty
input and unselected CASE branches do not evaluate them. Both materialized and
streamed forms share these rules and statement-wide rollback. Long finite DOUBLE
spellings use the fixed 2,048-character numeric-token limit; integer magnitude,
non-finite refusal and query budgets remain unchanged. See the
[query contract](QUERY_LANGUAGE.md#values-and-python-mapping) for precise error details.

Static name checks cover all branches. Except for functions with an explicit
evaluation-phase contract such as RANGE, provably invalid operand types and invalid
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
equality; ALL preserves multiplicity. Each branch's sort/window is local. A single
chain must use only UNION or only UNION ALL. Mixing them is a compile-time
`mixed_union_composition` refusal, including through direct AST admission. This
intentionally replaces the earlier left-associated mixed-chain extension to meet
the frozen reference profile; there is no legacy toggle. Different policies can
still compose in separate returning subquery scopes:

```cypher
CALL () { RETURN 1 AS x UNION RETURN 1 AS x }
RETURN x UNION ALL RETURN 1 AS x
```

This returns two rows containing 1. No updating UNION branches in this increment.

UNION publishes scalars, lists/maps and qualified detached node/relationship
values, including nested and aggregate entities. Entity deduplication retains
table/incarnation identity; two tables' local record ID 1 is not one entity.
Returning subqueries export known same-kind entity table alternatives for later
property access/grouping. [Entity results and JSON](ENTITY_VALUES.md) describe the
breaking development contract. The admitted typed bounded `PathValue` also survives
UNION/ALL and cursors, including import/export through returning subqueries and
path-or-NULL branches consumed by path functions. Captures also compose with
WITH/UNWIND, DISTINCT/order/windows and typed OPTIONAL MATCH; optional failure
extends the path as NULL. Within one MATCH, all relationship occurrences must be
disjoint; separate MATCH clauses may reuse relationships. Typed zero-length and
multiple captured segments concatenate without duplicating junction nodes.
Untyped/type-alternative capture remains pending. Omitted upper bounds preserve
omission. Node-only capture (`MATCH p=(n)`) is supported without a relationship
schema, including polymorphic/anonymous nodes, optional null extension, returning
subqueries and UNION/aggregate composition. Omitted-upper traversals retain
omission and refuse a valid continuation beyond the 30-hop resource ceiling,
instead of silently truncating at 20. See [traversal semantics](QUERY_LANGUAGE.md).
All branches
share one snapshot and existing resource limits, including the alias-expanded
typing-depth ceiling. Column-name mismatches fail before execution with
`reason="different_columns_in_union"`, `query_phase="planning"`.

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
