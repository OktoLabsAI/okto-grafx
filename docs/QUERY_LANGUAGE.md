# Query language reference

## Native typed collections (0.0.6 development)

After explicit catalog-v2 activation, node/relationship properties accept
`LIST<T>`, string-keyed `MAP<T>`, `ARRAY<T,n>` and `STRUCT<name:T,...>`.
Append `NOT NULL` at any level to require that value; nullable is the default.
Nested DECIMAL/temporal/ANY leaves remain native. Arrays require exact length;
structs reject unknown fields and fill missing nullable fields with NULL.
CREATE/SET and bound parameters validate the complete assignment. `STRUCT<>`
is valid without changing the expression operator `<>`. Collection primary keys
and custom indexes refuse. [Examples, storage and consumer limits](specs/TYPED_COLLECTIONS_V1.md).

## EXISTS query expressions

`EXISTS { MATCH(n)-->(m) WHERE m.active=true }` tests whether a native read body
produces a row. It supports implicit outer imports, local inner names, optional
RETURN/MATCH, aggregation/windows, UNION and nesting. A NULL/false projection
still counts as a row; a body with zero rows returns false. Writes inside the body
are rejected before execution, even for empty outer input. Existing query APIs,
snapshot authority, cancellation and resource limits are shared. See the
[full contract and runnable examples](specs/EXISTS_SUBQUERIES_V1.md).

The closed read-only `CALL grafx.search_text(index, query, k [, record_ids])`
procedure is supported through materialized `execute`; see [FTS procedure syntax](FULL_TEXT_SEARCH.md#procedure-query).
That specialized procedure does not support suffix composition or cursor/explain.
Separately, trusted typed `CALL app.name(...) YIELD ...` supports composition;
see [composable queries](COMPOSABLE_QUERIES.md). Use `Database.create_text_index`
to create FTS indexes; textual full-text DDL is not supported.

[Documentation index](README.md) · [Python API](API_REFERENCE.md)



An explicitly bounded openCypher/Kùzu-inspired subset, not full compatibility with
either engine. Unsupported syntax/shape/type combinations refuse; acceptance by
the parser alone is not proof that a query can plan or execute. One statement per
Python `execute()` call; CLI multi-statement arguments remain separate statements.

| Supported | Notes |
|---|---|
| `CREATE NODE TABLE` / `CREATE REL TABLE` | with `PRIMARY KEY`, typed columns, `FROM`/`TO` |
| `CREATE INDEX` | equality-only exact index over one or more ordered node properties; optional deterministic sizing |
| `CREATE VECTOR SPACE` | dimension, metric, storage dtype |
| `CREATE` (nodes and relationships) | inline properties, unlabeled and multiple-label nodes; undeclared labels and relationship types create native flexible models under explicit owner-resolution rules; typed physical constraints remain enforced |
| `MATCH` … `WHERE` … `RETURN` | equality, comparison, `STARTS WITH`, `ENDS WITH`, boolean operators |
| `MERGE` | node and fixed-length whole-path patterns emit every complete match; an empty match creates all unbound entities; conditional `ON CREATE SET` / `ON MATCH SET` property and node-label actions |
| `SET` / `REMOVE` | native node/relationship properties, map replacement/overlay and node-label membership; relationship endpoints are immutable and physical typed constraints remain enforced |
| `DELETE` | expressions selecting native nodes, relationships or paths; NULL is a no-op |
| `DETACH DELETE` | ends every relationship incident on the node together with it |
| `UNWIND $rows AS r` | repeated list expansion in ordered pipelines; NULL/empty carriers emit no rows; proven native node/relationship collections retain entity identity for rematch and writes |
| `WITH` … `WHERE` | scalar/aggregate projection stages; `WITH *` carries the incoming named scope and can append explicit items; its WHERE runs after projection. Optional-hop aggregation has the closed forms below |
| `MATCH (n)` | reads every node table as one set, including in composed read pipelines, inline maps and OPTIONAL MATCH; compatible-property checks, filters, aggregation and windows apply across the union; undeclared properties read as NULL |
| Traversal | one hop, bounded ranges including `[:REL*0..3]`, both directions, relationship isomorphism |
| `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT` | |
| Native temporal expressions | Six constructor families, scoped clocks, fields, duration arithmetic, truncation/differences, distinct predicate/order rules and native persisted values. [Complete usage and remaining limits](TEMPORAL_VALUES.md) |
| `RETURN *` | All visible variables, alphabetically ordered, followed by explicit additional items; no anonymous/local/hidden bindings; existing projection budget applies |
| Aggregates | `count`, `sum`, `avg`, `min`, `max`, `collect`, `percentileDisc`, `percentileCont`; scalar/aggregate type restrictions apply |
| Scalar functions | text, numeric, conversion and list families below, native [temporal constructors/clocks/truncation/differences](TEMPORAL_VALUES.md#query-functions), plus graph/vector extensions |
| Conditional/list expressions | searched/simple `CASE`; zero-based/negative indexing and half-open slices; list and pattern comprehensions, all/any/none/single, reduce, concatenation; case-sensitive map access |
| Parameters | `$name`, `$1` or backtick-quoted names; refused before anything runs if one is missing |
| `OPTIONAL MATCH` | Correlated typed patterns and whole-clause null extension; optimized incident-hop execution retained |
| `UNION`, `UNION ALL` | Up to 64 read/write RETURN branches or all-unit writing branches; same ordered output names, heterogeneous/native entity values; ordered private effects and whole-statement rollback; one duplicate policy per chain; [contract](COMPOSABLE_QUERIES.md#updating-union-branches) |
| CALL subqueries | Returning read/write and unit writing bodies; explicit/global `CALL (x)` / `CALL (*)`, empty `CALL ()`, branch-local leading-WITH imports; one statement rollback boundary, maximum nesting 16; [scope](COMPOSABLE_QUERIES.md#subquery-import-scopes) and [writes](COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries) |
| Typed CALL/YIELD | Trusted per-handle registry, explicit permissions/budgets, composed execution; standalone implicit arguments and automatic/wildcard outputs. [Invocation contract](specs/PROCEDURE_INVOCATION_V1.md) |
| Writing procedure CALL | Explicit `mode="write"`, revocable same-transaction execute/query capabilities and shared budgets; nested calls retain permissions and rollback. [Query authority](specs/PROCEDURE_QUERY_AUTHORITY_V1.md), [nesting](specs/PROCEDURE_NESTING_EFFECTS_V1.md) |
| Schema procedure CALL | Explicit `schema_write=True` plus `schema` permission; native CREATE table/vector-space/index DDL and implicit flexible schema with shared rollback. Catalog-v2 index composition and compiled-scan limits are explicit. [Schema authority](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md) |
| Native procedure values | Temporal/DECIMAL, LIST/MAP/ANY and vector signatures; NUMBER retains int/float/DECIMAL. Owned callbacks, recursive bounds, native persistence and rollback. Does not widen scalar UDFs or add parameterized stored types. [Value contract](specs/PROCEDURE_NATIVE_VALUES_V1.md) |
| Native procedure entities | NODE/RELATIONSHIP/PATH and typed entity lists; detached callback observations restore only invocation-witnessed native references. [Identity, budgets and limits](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md) |
| Native procedure queries | Permissioned graph_read callbacks and writer.query with native returning writes, cumulative result budgets and outer snapshot/rollback. [Query API](specs/PROCEDURE_QUERY_AUTHORITY_V1.md) |
| Recursive native procedures | Self/mutual CALL with inherited depth, root budgets and explicit default-volatile determinism; nested schema operations require the separately declared schema authority and permissions. [Nesting/effects contract](specs/PROCEDURE_NESTING_EFFECTS_V1.md) |
| Unit CALL | Empty registered output schema, callback returns None, no YIELD, preserves input rows; standalone/terminal call has no results. [Contract](specs/UNIT_PROCEDURES_V1.md) |
| Path projection | Native `PathValue` for typed, untyped and alternative-type bounded ranges, zero length and multiple segments in either direction; composed MATCH/OPTIONAL MATCH, aliases, subqueries, `length`, `nodes`, `relationships`; omitted upper bounds preserve explicit resource refusal |

### Native entity collections and UNWIND

UNWIND preserves entity kind and actual source-table identity when the query
produces a node/relationship collection: `collect(n)`, explicit entity lists,
compatible CASE/coalesce branches, slices, and identity-preserving/filtering list
comprehensions. List-local names shadow outer names; a projection of scalar
properties is a scalar collection, not a collection of writable entities.

```cypher
MATCH (a:Task)
WITH collect(a) AS tasks
WITH tasks, [a IN tasks | a.status] AS previousStatuses
UNWIND tasks AS task
SET task.status = 'ready'
RETURN task.status, previousStatuses
```

`previousStatuses` is the previously materialized scalar list. Each unwound entity
observes the latest changes of its own transaction. Repeating an entity in the
collection preserves multiplicity: two occurrences execute a downstream SET
twice, rather than collapsing identity or reusing the first occurrence's old
properties. This applies to stored and same-instruction newly created entities.
Relationships retain their actual type and endpoints. Rematching the unwound
binding restricts that identity instead of starting an unrelated table scan.

With `query_memory_budget_bytes` enabled, temporary entity records do not carry
write authority. UNWIND reacquires a stored entity's physical reference through
the existing snapshot-visible identity path and applies the private transaction
overlay before use. Unknown/missing stored identity refuses; no identity is
synthesized from a property value. Maps or parameter lists that resemble nodes
cannot grant entity write authority. Deleted-entity content/SET refuses without
resurrection; identity/count operations retain the existing deleted-entity rules.

Late failures discard all instruction effects, preserving earlier successful
instructions. Other readers retain their snapshots and conflicting writers retain
OCC rejection. Existing list, staging, spill and traversal budgets apply; this
adds no configuration, public method or persistent format. Detached Python
`NodeValue`/`RelationshipValue` results remain data, not writable handles.

Implicit relationship DDL may create endpoint identity indexes during the same
instruction (for example UNWIND → node MERGE → relationship MERGE). Its runtime
authority is extended only for newly activated catalog entries, preserving already
selected stores and cached endpoint paths. Registry/refusal errors propagate;
there is no fallback around a damaged selected index, no inner COMMIT and no
change to recovery/durability guarantees.

### RETURN aliases and grouping expressions

Projection errors are native planning refusals (`GrafxPlanError`,
`query_phase="planning"`), before any instruction effects:

| Invalid operation | Native reason / field | Conformance category |
| --- | --- | --- |
| Duplicate explicit/derived RETURN or WITH name, including WITH wildcard collision | `column_name_conflict`; `alias`/`column` for RETURN, `item` for WITH | `ColumnNameConflict` |
| Computed WITH item without `AS name` (a bare carried variable needs no alias) | `no_expression_alias`; `item` | `NoExpressionAlias` |
| Row aggregate inside a list-local predicate/body, including a reduction body | `invalid_aggregation_context`; `iteration` | `InvalidAggregation` |
| `size(path)` rather than `length(path)` | `size_path_argument_type`; field `function`, value `SIZE` | `InvalidArgumentType` |

An aggregate may still **produce the list source**: `RETURN [x IN collect(n) | x]`.
`size()` continues accepting strings, lists and NULL. These are more precise causes
for existing refusals, not new storage restrictions, configuration flags or relaxed
transaction semantics. Prior/later successful instructions in the same explicit
transaction remain usable; a refused instruction creates no partial effects.

ORDER BY may use a RETURN alias inside property access and scalar expressions,
not just as a bare sort key: `RETURN r AS edge ORDER BY edge.id` and
`RETURN -n AS n ORDER BY n+1`. Output aliases take precedence over same-spelled
incoming bindings. This also applies to entity aliases after DISTINCT and to
bounded sorting/spill. Cached input expressions cannot override a shadowing
output value; already computed aggregate results remain group results.

Grouped or DISTINCT RETURN can reuse a **complete projected expression** inside
a compound ORDER BY key, including an aggregate result:

```cypher
MATCH (n:Event)
RETURN n.category AS category, count(*) AS total
ORDER BY total + count(*) DESC, category
```

The repeated `count(*)` reads the projected count; it does not aggregate again.
Unaliased expressions retain their public column headings. Aliases used in type
validation resolve to their input definitions without rewriting execution or
reevaluating producers (including `rand()`). Expression-local variables still
shadow same-named result aliases. This works with ordinary sorting and bounded
spill under the existing `query_memory_budget_bytes` setting.

This is structural reuse, not algebraic inversion or permission to introduce a
new aggregate. `RETURN count(*) AS c ORDER BY sum(c)` refuses. Grouped/DISTINCT
sorting cannot read discarded input properties: `RETURN DISTINCT n.category
ORDER BY n.other` raises `GrafxPlanError` with `field="variable"`,
`reason="undefined_variable"`, `query_phase="planning"`. Ambiguous grouping is
checked before substitution. Plain RETURN retains its existing input scope for
sorting. A late sort failure rolls back the whole instruction, not prior
successful instructions in the same explicit transaction. No public method,
result DTO, storage format, concurrency mode or additional setting is introduced.

Outside aggregate arguments, a mixed aggregate expression must use independently
projected grouping variables/properties, constants, parameters or expression-local
variables. For example, `RETURN n, n+count(*)` is valid, while
`RETURN n+count(*)` has an implicit, ambiguous grouping value. Projecting `a.x+b.x`
does not independently group by `a.x` and `b.x`. The native planning error is
`GrafxPlanError`, `field="aggregation"`,
`reason="ambiguous_aggregation_expression"`, `query_phase="planning"`.
The conformance mapper reports `AmbiguousAggregationExpression` only from this
proven cause. WITH wildcard-carried values participate in grouping as ordinary
projected values. Invalid grouping refuses before graph effects.

After a grouped WITH, an unprojected input used by a new sort aggregate raises
the existing `undefined_variable` planning cause. Nested aggregates refuse with
`field="function"`, `reason="nested_aggregation"`, `query_phase="planning"`
(TCK `NestedAggregation`). Direct nondeterministic aggregate arguments now refuse
as described below; this increment does not claim complete aggregate conformance.

### Stable aggregate arguments and volatile row values

Arguments to `count`, `sum`, `avg`, `min`, `max`, `collect`, `percentileDisc` and `percentileCont` must be deterministic
expressions. For example, `count(rand())`, `sum(1+rand())` and even
`count(coalesce(1,rand()))` refuse during planning, before drawing randomness or
performing graph effects. The native `GrafxPlanError` has `field="aggregation"`,
`reason="non_deterministic_aggregate_argument"`, `query_phase="planning"`;
the reference mapping is compile-time `NonConstantExpression`.

To aggregate random values, materialize each row's value in a preceding WITH:

```cypher
UNWIND [1,2,3] AS i
WITH rand() AS sample
RETURN avg(sample), count(sample)
```

For independent draws, use separate aliases (`WITH rand() AS a, rand() AS b
RETURN sum(a), sum(b)`). This preserves independent per-row evaluation and keeps
aggregate arguments stable. Parameters and ordinary scalar expressions remain
valid arguments. Random expressions outside aggregate arguments, including
row-window expressions, retain their existing execution contract. Registered UDFs
retain their existing trusted deterministic declaration; this check does not
inspect arbitrary callback internals.

This replaces the development behavior that accepted direct `sum(rand())`; there
is no legacy switch or new configuration. The RETURN aggregation family (Return6)
passes all 21 original cases. Percentile support is documented below; the
[complete V3 execution](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md) records
the current required-profile result separately from final integrated acceptance.

### Percentile aggregates

`percentileDisc(value, p)` returns the sample at the nearest-rank position
`max(ceil(p*N)-1, 0)` in the sorted non-NULL samples. An integer sample remains an
integer. `percentileCont(value, p)` returns DOUBLE, interpolating at `p*(N-1)`;
`p=0` and `p=1` select the minimum and maximum. The formulas follow the
[reference aggregate contract](https://neo4j.com/docs/cypher-manual/4.4/functions/aggregating/#functions-percentilecont).

```python
result = db.execute(
    "UNWIND $samples AS x RETURN percentileDisc(x,$p) AS d, percentileCont(x,$p) AS c",
    {"samples": [10, 20, 30], "p": 0.25},
)
assert result.rows == ((10, 15.0),)
```

Both arguments are positional. The sample must be numeric or NULL, and the
percentile must be numeric in `[0,1]` (BOOL is not numeric). NULL samples do not
participate; empty/all-NULL groups yield NULL. Each group's first non-NULL sample
fixes its percentile. The percentile expression is evaluated on non-NULL input
rows; use a literal/parameter or a group-stable expression for clear intent.
Empty/all-NULL groups do not evaluate it. `DISTINCT` deduplicates sample values,
not `(sample,p)` pairs. Grouping, WITH, returning subqueries and scalar composition
use the normal aggregate pipeline. Direct volatile arguments refuse; materialize
them with WITH if needed. Expression NaN samples use native numeric ordering and
may produce NaN; a NaN percentile is out of range. Stored nonfinite values remain
prohibited. Continuous interpolation avoids the `high-low` overflow for opposite
finite extremes; its output has normal DOUBLE precision.

Native runtime `GrafxPlanError` causes use `field="percentile"`,
`query_phase="execution"`, with `reason="percentile_argument_bounds"` for p
outside `[0,1]`, `"percentile_argument_type"` for a nonnumeric p, and
`"percentile_sample_type"` for a nonnumeric sample. Bounds map to TCK
`ArgumentError` / `NumberOutOfRange`; runtime type errors map to `TypeError` /
`InvalidArgumentType`. Known static sample types may refuse during planning.
Any late group failure rolls back the whole instruction, not just that group.

With `query_memory_budget_bytes=None`, samples are retained and sorted in memory.
With a budget configured, grouping, optional DISTINCT and numeric ordering use
the existing external runs. Only one group and the two selected values are
retained, not the sample list; all sorted records are consumed and checked against
the accepted count. Truncated runs fail closed. Exact percentiles require all
samples and sorting (O(N log N) comparisons in the general case), not an approximate
sketch or a promise of sublinear work. Existing memory/work/temporary-storage
limits and cleanup apply. No new setting, public result DTO or durable format is
introduced. All 35 original aggregation-expression scenarios pass in the focused
native run; this does not establish complete Cypher conformance.

### WITH ordering scope

A plain (non-DISTINCT, non-aggregating) `WITH` can order by incoming bindings
that it does not export, as well as by its projected aliases:

```cypher
MATCH (a:Person)
WITH a.name AS name ORDER BY a.age DESC LIMIT 10
RETURN name
```

Output aliases shadow input names in ordering; all projection items still read
the incoming scope together. The executor retains only the input bindings needed
by the sort/filter, without adding them to public result columns, and strips them
after the attached sort/window/filter. Private operator columns preserve these
values through bounded spill. Following clauses cannot access those private names. Projection
expressions are not evaluated again when stripping them, including `rand()`.
Bounded sort/spill uses the same contract and existing memory limits; there is no
new setting. DISTINCT and aggregate ordering do not reopen discarded input rows.

Exact projected expressions can also be referenced inside attached ordering/filter
expressions after DISTINCT/grouping: `WITH DISTINCT a.name AS name ORDER BY a.name`
and `WITH a.name AS name, count(*) AS n ORDER BY count(*)` reuse the already
projected values. This includes larger modifiers containing those expressions,
but not algebraically different expressions or unrelated properties of `a`.
Output aliases and list-local binders retain precedence; expression substitution
cannot capture a same-spelled local variable. A plain attached `WHERE` can read
unexported source bindings, for example `WITH other WHERE r IS NULL`; these names
do not become available to a later RETURN. DISTINCT/grouping instead require
projected values; they do not select arbitrary source representatives.

All 19 original WITH-WHERE scenarios pass in the focused native run. Wider
aggregate-ordering expressions, error distinctions and temporal dependencies remain
required conformance work, not new exclusions; see [current evidence](conformance/FP2_PROGRESS.md).

### Row-independent SKIP and LIMIT expressions

`RETURN` and `WITH` accept nonnegative INT64 row counts computed by expressions
that do not depend on input rows. Parameters, scalar arithmetic and functions,
including `rand()`, are supported. Locally bound list-expression variables are
not outer row dependencies. Aggregates and references to input variables refuse
during planning with `reason="non_constant_window"`.

```cypher
UNWIND [1,2,3,4] AS n
RETURN n ORDER BY n SKIP $offset + 1 LIMIT toInteger(rand()*4)
```

Each reached logical window evaluates its expression once per invocation, not per
input row and not once for the lifetime of a cached plan. Correlated subqueries
evaluate their windows for each invocation. EXPLAIN does not draw random values.
An unreached branch/window does not promise evaluation. A final `RETURN LIMIT 0`
still executes all preceding writes; `WITH ... LIMIT` controls downstream input.

Wrong-type literals (including BOOL and NULL) and signed negative integer literals
fail at planning. Invalid parameter/computed results fail at execution. Native
errors are `GrafxPlanError` with `field="skip"` or `"limit"`, `query_phase`, and
`reason="window_argument_type"` or `"negative_window"`; errors preserve the same
whole-instruction rollback boundary as other query failures.

Literal/parameter windows retain top-K sorting and eligible vector/keyset
optimizations. General expressions use the canonical operators so physical
optimizations cannot draw `rand()` twice and disagree about the result window.
This can require full ordering before slicing; existing query memory/spill and
work limits remain authoritative. No new setting or alternate compatibility mode
is introduced.

### Content access after deletion

Within an instruction, `DELETE` (including incident edges ended by `DETACH DELETE`)
invalidates live property access, `properties(entity)`, `labels(node)` and label
predicates on that entity. Reading a missing property is also content access.
The native API raises `GrafxPlanError` (`plan_error`) with
`field="entity"`, `reason="deleted_entity_access"`, `query_phase="execution"`
and `entity_kind="node"` or `"relationship"`. The complete instruction rolls back,
including its schema changes; prior successful instructions in the transaction
remain intact. The TCK adapter maps this proven cause to runtime
`EntityNotFound` / `DeletedEntityAccess`.

Capture values before deleting if they are needed in the result:

```cypher
MATCH (n:Person) WITH n, n.name AS removed_name
DETACH DELETE n RETURN removed_name
```

Entity identity, `count(entity)`, NULL checks, immutable `type(relationship)` and
previously computed scalars remain usable. Returning an entity itself retains
the detached observation contract; it does not grant live access to deleted
content. Independent readers keep their own snapshot. These rules also apply to
private inserts and bindings restored from temporary spill; no configuration
switch or weaker transaction mode is introduced.

### Returning the visible scope

`RETURN *` expands the current lexical variables, not every stored property.
For example, `WITH 2 AS z, 1 AS a RETURN *, 3 AS extra` returns columns
`a, z, extra` and row `(1, 2, 3)`. WITH discards/rebindings are respected;
anonymous patterns and list-comprehension locals do not introduce output columns.
Named nodes, relationships and paths retain their detached native identities.
OPTIONAL MATCH preserves NULL bindings. DISTINCT, aggregation, ORDER BY,
SKIP/LIMIT, read cursors and returning read subqueries use ordinary projection
semantics. UNION compares the fully expanded ordered column names.

No visible variables is a compile-time `NoVariablesInScope` refusal, even if
additional explicit items follow the star. Duplicate output names and expansion
beyond 256 items refuse before effects; the wildcard introduces no new setting.
Read APIs still reject write plans and a late projection failure rolls back the
whole statement. Parser ASTs retain `ReturnClause.include_existing`; semantic
analysis exposes the complete output columns after resolving scope.

### Node MERGE matching and creation

Use an explicit write transaction (`with db.begin("write") as tx:`), even if a
MERGE is expected only to match existing nodes. Autocommit `db.execute()` is a
read API; read transactions and read-only cursors refuse write plans before
execution, including pipelines with zero input rows or matches requiring no writes.

`MERGE (n:Person {name:$name})` returns **every** matching Person per input row,
not only the first. Unspecified properties do not constrain the match. An empty
map or no map matches every row of the selected table; missing required creation
columns do not invalidate an existing match. Only an empty match set enters
native insertion: existing typed tables retain nullability, type and uniqueness
checks; an undeclared single label creates a flexible table with no implicit PK. Property
expressions are evaluated once per input and reused by the insertion branch.
NULL property values refuse rather than matching NULL or inserting it via MERGE.

With a new variable `n`, `MERGE (n)` and `MERGE (n {id:$id})` may match across all node tables, preserving
qualified identities and multiplicity. Missing columns do not match supplied
properties. If no node matches, execution creates a native unlabeled node with
those properties. The enclosing statement retains atomic schema/data rollback
on later failure, preserving earlier successful statements.
Zero input rows perform no match or creation in a write transaction.

Implicit labeled models support heterogeneous properties without prior DDL, e.g.
`CREATE (:Person {value:'text'}), (:Person {value:2})`. Schema and data are one
atomic statement; read APIs and EXPLAIN never create the model. A pristine empty
catalog upgrades in that transaction; a nonempty v1 catalog requires explicit
`db.maintenance.ensure_identity_indexes()` first. Multiple labels remain unsupported.
See [flexible model, persistence and bounds](architecture/FLEXIBLE_GRAPH_V1.md).

Polymorphic read pipelines may compose with standalone node MERGEs and with
writes to real bound entities as described below. The subsequent
[whole-pattern MERGE contract](specs/GENERAL_MERGE_V1.md) also supports unbound
endpoints and fixed multi-hop paths. Searches
use owner-visible scans with private inserts/updates/deletes, existing query
budgets and iterator cleanup; no indexed MERGE performance claim is made here.
Concurrency/OCC, WAL publication and persisted-value rules are unchanged.

### Conditional MERGE property actions

Consecutive CREATE patterns execute as a flat, ordered native program, preserving
per-input dependencies and captured paths without recursive operator depth per
pattern. The statement remains atomic, including implicit schema. Source admission
is bounded at 65,536 characters, 32,768 tokens and 1,024 pipeline clauses; expression
and physical-plan depth guards remain separate. UNION branches and MERGE actions
retain their 64-item limits. [Program/API and exact bounds](specs/WRITE_PATTERN_CONTRACTS_V1.md#large-create-pipelines).

`CREATE p = pattern` and `MERGE p = pattern` capture native paths, usable in actions
and subsequent clauses. Single-node capture has zero edges; MERGE also supports
unbound and fixed multi-hop paths, directed or plain undirected. An undirected
MERGE searches both orientations and creates left-to-right only if none matches;
parallel matches remain distinct and a self-loop is processed once. A preceding mutation clause finishes
for all input rows before MERGE reads its match set, inside the same transaction.
Bound nodes must be referenced as bare edge endpoints, not redeclared with
labels/properties or as a standalone CREATE/MERGE node.
[Examples, binding/error rules and phase evidence](specs/WRITE_PATTERN_CONTRACTS_V1.md).

```python
with db.begin("write") as tx:
    result = tx.execute("""
        MERGE (n:Person {name:$name})
        ON CREATE SET n.visits = 1
        ON MATCH SET n.visits = n.visits + 1
        RETURN n.name, n.visits
    """, {"name": "Ada"})
```

Every matching node or bound-endpoint relationship receives `ON MATCH`; creation
and `ON CREATE` occur only if no match exists. Both action kinds are optional,
may appear in either order and may contain repeated SET clauses. Selected clauses
run sequentially under the same statement; the unselected branch is not evaluated
at runtime, but its bindings/parameters must still be valid. These are property
assignments, including whole-property maps, but not label changes. Existing typed schemas must
declare assigned properties; implicit flexible nodes retain their dynamic model.
No inner commit is introduced. Read-only refusal, whole-statement rollback,
snapshot/OCC, stored-value checks and query budgets remain in force. See the
[complete action contract and evidence](specs/MERGE_ACTIONS_V1.md).

### Replacing and merging property maps

```python
with db.begin("write") as tx:
    tx.execute("MATCH(n:Person {name:$name}) SET n += $patch",
               {"name": "Ada", "patch": {"visits": 2, "obsolete": None}})
    tx.execute("MATCH(n:Person {name:$name}) SET n = $properties",
               {"name": "Ada", "properties": {"name": "Ada", "visits": 3}})
```

`=` replaces all logical properties; `+=` keeps unspecified properties. NULL map
entries remove keys; a NULL whole map refuses. Native nodes/relationships and
`properties(entity)` may supply the map. Targets retain identity/type/endpoints;
typed tables retain required columns, primary-key uniqueness and type checks.
Flexible tables retain heterogeneous values, but stored NaN/infinity still refuse.
Assignments in one SET evaluate RHS against clause input; separate SET clauses
observe preceding updates. MERGE actions and writing CALL/UNION share the same
statement rollback. No additional configuration is needed.
[Complete map contract and evidence](specs/SET_PROPERTY_MAPS_V1.md).

### Writes through polymorphic bindings

`DELETE` and `DETACH DELETE` also accept native entity/path selections from lists,
maps and conditional expressions, such as `DELETE edges[0]` or
`DETACH DELETE records.key`. A path selects its nodes and relationships; whole
lists/maps are not implicitly flattened. Targets and input predicates are prepared
before deletion under existing memory/row bounds. Read-only refusal, snapshot/OCC,
connected-node validation and statement rollback remain mandatory.
[Complete deletion contract, API change and evidence](specs/DELETE_EXPRESSIONS_V1.md).

In a write transaction, real node/relationship bindings may retain several
candidate tables across MATCH/WITH and then participate in SET, DELETE or
DETACH DELETE. Each operation uses the actual bound row's schema and identity;
there is no first-table choice. On typed-column tables, SET of an undeclared column or incompatible type
refuses, including a mismatch reached only in a later table. Whole-statement
rollback removes earlier effects of that statement, not earlier successful
statements. Scalars, maps, detached/foreign parameter values and relationship
lists do not become writable row authority. NULL write targets and returning/unit
writing subqueries follow the [native write contract](COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries)
and the current required-profile execution; detached values never grant authority.

CREATE/MERGE of a relationship between bound polymorphic nodes chooses the
unique declared member for their **actual endpoint-table pair**. When several
members are possible, the plan carries a closed candidate set; runtime resolution
is followed by the existing catalog, physical/pending identity, owner visibility
and endpoint read-dependency proofs. A missing pair refuses atomically, rather
than choosing the first member or skipping that input. New relation bindings
retain their actual table for subsequent SET and detached output. The later
[general MERGE increment](specs/GENERAL_MERGE_V1.md) adds multi-edge patterns and
unbound endpoints with whole-pattern, not partial-upsert, creation semantics.

New nodes may use an explicit typed label or native unlabeled creation. Unlabeled
nodes hold heterogeneous dynamic properties; setting a property to NULL removes
it. Their physical store is not a user-visible label. This separate model
extension uses a durable catalog capability, not a configuration toggle or inner
commit. [Examples, format and remaining integration](architecture/FLEXIBLE_GRAPH_V1.md).

Undeclared relationship types are now created natively with flexible property maps
for the actual endpoint pair. These automatic types can grow additional pairs;
explicit typed groups retain the closed-pair rules above. Schema/data rollback is
atomic. Later MATCH/OPTIONAL/path and scoped pattern reads in the same statement
see the new type. `collect()` and homogeneous entity-list concatenation/indexing
preserve native entity identity for subsequent bound writes.

`MATCH` in a write transaction sees that owner's earlier node inserts, updates and deletes. A
dirty node table plans a scan plus the private overlay instead of consulting an index that only
describes committed rows. A `CREATE` may name a node an earlier statement of the same
transaction created, and both private endpoint identities are resolved before the first heap
write. Traversal reads that owner's combined view: relationships it created, relationships it
updated or ended, and endpoint nodes it created, updated or ended, with multiplicity, direction
and self-loops preserved. Pending relationship inserts force one grouped scan plus the overlay,
because endpoint indexes contain only committed edges; update/delete-only overlays can still use
fresh indexes and validate or suppress their candidates. A start node created by this transaction
takes the scan path even when the relationship table has no pending insert, because its private
identity has no index encoding. A statement can create endpoints and their relationships,
and subsequently SET or delete them. Endpoint identities are authenticated transaction-private
intents, not intermediate commits; a later statement failure discards its earlier phases.
Vector search over a dirty table is fail-closed. Updates of
relationship properties are owner-visible, while their `_from`/`_to` layout columns remain
immutable. A table declared inside a transaction is usable by that transaction's own later
statements and becomes visible to every other transaction when it commits — schema changes are
transactions like any other.

## Relationship types spanning endpoint tables

The 0.0.6 development line admits a native logical relationship type with several
declared endpoint pairs:

```cypher
CREATE REL TABLE GROUP REL(FROM A TO B, FROM B TO C, num INT64)
CREATE (a:A {id:1})-[:REL {num:1}]->(b:B {id:1})-[:REL {num:2}]->(c:C {id:1})
MATCH p=(a:A)-[:REL*2..2]->(c:C) RETURN relationships(p)
```

Create the node tables first. Schema and row writes use a write transaction;
reads may use `db.execute`. Groups require catalog v2: call
`db.maintenance.ensure_identity_indexes()` outside an active transaction before
the group DDL if the store is still v1. A v1 refusal occurs before member creation
and includes that remedy. This is explicit format admission, not an automatic
inner transaction or a new configuration option.

All endpoint pairs precede the property columns. Pairs are distinct, ordered
source/target node-table names; every member shares the property schema. A group
with one pair is valid. The logical name must not collide with an existing group
or physical table. Ordinary `CREATE REL TABLE` remains unchanged.

Creation installs physical members and endpoint indexes plus the logical group
under one DDL statement/transaction rollback boundary. Each member retains its
own table-qualified row identity. Physical member names use `_gx_rel_<table-id
in eight hex digits>`; a conflicting physical name is refused, not overwritten.
These names appear in physical schema/scan APIs but are not additional Cypher
relationship types. `type(r)` and detached edge/path results report `REL`.

Logical types participate in untyped/type-alternative reads, bounded directional
and undirected paths, zero hops, OPTIONAL, subqueries and entity UNION. CREATE and
MERGE select the unique member for statically proved or runtime-bound endpoint
tables; an undeclared pair is refused. Real multi-member bindings retain
SET/DELETE support with per-row schema checks and whole-statement rollback.
See [runtime write authority](#writes-through-polymorphic-bindings). Remaining
FP-3/model and group evolution work is not complete.

Inspect groups through `db.catalog.catalog.relationship_types()` and
`relationship_tables("REL")`. Group-wide nullable-column evolution is not yet
implemented; changing one member is refused. Logical graph export/import now
preserves typed/flexible group authority through [format 2](LOGICAL_TRANSFER.md#artifact-v2-flexible-models-and-relationship-groups).
Bounded copy into existing targets preserves grouped types and remaps native
flexible/no-PK identities under an atomic receipt; equal bags are not deduplicated.
[Copy contract](CATALOG_COPY.md#flexible-and-no-pk-entity-identity). Opt-in history
also preserves model flags and selected historical group membership.
Broader projection and final consumer qualification remain in the
[relationship-type integration contract](architecture/RELATIONSHIP_TYPES_V1.md).
No claim of complete Cypher/profile conformance follows from this increment.

## Values and Python mapping

Development `DECIMAL(p,s)` columns and `okto_grafx.DecimalValue` parameters/results
now preserve exact coefficient, precision and scale. Assignment is exact-only;
ANY nesting retains native metadata. Explicit catalog-v2 activation is required.
See the [usage, format and support matrix](specs/DECIMAL_VALUES_V1.md#native-storage-contract-development).
Native `decimal(value,p,s[,rounding])`, +/−/×/÷, signs/abs, SUM/AVG/MIN/MAX and
exact numeric comparisons/grouping/ordering are implemented, including spill.
Typed decimal equality indexes normalize only mathematically exact probes.
DOUBLE arithmetic is never implicitly mixed with DECIMAL; explicit conversion is
required. [Numeric query semantics and limits](specs/DECIMAL_VALUES_V1.md#native-numeric-query-contract).

Decimal parameter names remain exact string keys: `$1` binds `{"1": value}`, and
`$01` is distinct from `$1`. The numeric spelling does not coerce keys to integers.
Digit-leading alphanumeric names such as `$1name` require backticks (`$` followed
by a backtick-quoted name). Existing name-length, detachment and missing-parameter
checks apply equally to reads and writes.

In the 0.0.6 development line, returning a node (typed or polymorphic) yields
`NodeValue`; returning an edge yields `RelationshipValue`, including in nested
lists/maps. Their properties are immutable observations and their identities
include database/table/kind/incarnation. `to_dict()` provides owned tagged JSON.
This replaces prior integer/mutable-map entity output, not scalar `n.id` values.
See [entity/UNION result contract and remaining path work](ENTITY_VALUES.md).

Standalone label-free node patterns compose with preceding UNWIND/WITH, multiple
MATCH clauses/patterns and returning read subqueries. `MATCH ()` preserves node
multiplicity without publishing a variable. `MATCH (n {id: $id})` compares that
property across node tables; a table missing the key contributes NULL rather than
an exception. Different property families remain per-row values, with dynamic
operator checks rather than a blanket refusal. Reusing a bound node, including through a WITH alias or explicit
subquery import, tests the existing binding instead of scanning again. A NULL
introduced by OPTIONAL MATCH never becomes an unbound scan; a label on a rematch
filters the bound node's table. See [composition and remaining bounds](COMPOSABLE_QUERIES.md#polymorphic-node-read-composition).

Integer query literals accept decimal, hexadecimal (`0x`/`0X`) and octal (`0o`)
spellings, for example `RETURN 0x2A AS hex, 0o52 AS octal`. Based literals permit
single underscores before digits (`0x_FF`, `0o7_7`), not trailing or repeated
underscores. Values remain exact signed INT64, including `-0x8000000000000000`;
malformed digits and overflow refuse before statement effects. This introduces
no new configuration, storage type or Python parameter contract.

An adjacent non-keyword identifier suffix such as `9223372h54775808`, `-12abc`
or `1.2e3xyz` now has a native `invalid_numeric_literal` parse cause:
`GrafxParseError`, field `number_literal`, query phase `planning`. Token offsets
distinguish it from a separate unexpected expression (`12 abc`), while valid
keyword/operator continuations retain their grammar. The original literal text
is available in `value`, with source line/column/offset in the error.

An unsupported query character carries `unsupported_query_character`, field
`character`, phase `planning`. ASCII characters such as `#` outside a string or
quoted identifier map to `UnexpectedSyntax`; malformed numeric suffixes map to
`InvalidNumberLiteral`. A key containing a symbol remains valid when quoted,
for example ``{`k1#k`: 1}``. Existing Unicode, overflow, based-digit and string
escape refusals are unchanged. Parsing finishes before instruction effects, so
these failures cannot partially create or update graph data.

Numeric tokens have a fixed 2,048-character ceiling, excluding an external unary
sign. This supports long finite DOUBLE decimal spellings, including exact binary64
subnormal expansions. DOUBLE literals still need a decimal point or exponent;
an oversized integer is not implicitly converted to DOUBLE. Decimal integers are
checked against INT64 magnitude before host integer conversion, independently of
Python's process-global digit limit. Overflow/non-finite DOUBLE literals refuse
before statement effects. Query text and token budgets still apply.

### NaN expressions versus persistent properties

NaN is an expression DOUBLE, distinct from NULL. `RETURN 0.0 / 0.0 AS value`
returns Python `float('nan')`; mixed integer/DOUBLE zero division does likewise.
NaN can also be supplied as a Python parameter and returned inside lists/maps.
Arithmetic propagates it and comparisons against numbers are unordered:
`NaN = NaN` is false, `NaN <> NaN` is true, and numeric `<`, `<=`, `>` and `>=`
are false. Ordering comparisons against a string return NULL. NULL propagation
is unchanged. ORDER BY places NaN after finite numbers ascending (before them
descending); temporary query spill preserves these expression values.

This does not permit persisted NaN: CREATE, MERGE, SET and native tuple admission
reject NaN/infinity in DOUBLE properties and inside stored LIST/MAP values.
Failure rolls back the statement; earlier successful statements in the same
transaction remain available. Batch imports retain their whole-call rollback.
Vector components remain finite. No setting enables nonfinite storage, no format
change is needed, and existing files are not automatically erased or rewritten.
The transient value/spill codec is not a storage-admission API.

Integer `0 / 0` and nonzero `1.0 / 0.0` still refuse: this addition does not enable
infinity-producing division or relax finite literal/function-domain checks.
JSON has no standard NaN number; use the documented CLI output representation,
not a nonstandard JSON parameter token. Python consumers can test `math.isnan`.

Unaliased expression headings preserve the original source spelling: `RETURN
size(null)` produces the column `size(null)`, not `size(NULL)`. Spaces,
parentheses and quoted literal spelling inside the expression are preserved;
leading/trailing trivia outside its token span are not. A returned variable uses
its logical name without backticks; explicit `AS` always takes precedence.
Consumers needing stable dictionary keys should use explicit aliases. Source
spelling metadata does not participate in AST equality or normalized rendering;
prepared-cache keys continue to use exact query text.

Property and index postfixes compose on expression results, including functions,
CASE, grouped operations and list comprehensions: `RETURN
coalesce(null, {a: [{b: 7}]}).a[0].b AS value` returns `7`. Missing map keys and
NULL subjects propagate NULL. Provably invalid subject/index types refuse before
rows; unknown result types are checked when evaluated. Calls are not evaluated
by planning/binding just to discover a field value.

Maps projected with WITH retain chained property access through successive aliases,
CASE, parameters, NULL, grouped/sorted results and returning read-subquery
imports/exports. A projected map is not required to have a graph table. For example:

```python
result = db.execute(
    "WITH $document AS m RETURN m.author.name AS name",
    {"document": {"author": {"name": "Ada"}}},
)
assert result.rows == (("Ada",),)
```

The planner follows selected literal map/list paths through alias definitions only
as a type proof; it does not replace the executable projection or reevaluate its
source. `WITH {a:{b:rand()}} AS m RETURN m.a.b,m.a.b+1` draws once per input row,
not once per field use, and planning draws nothing. Unknown projected field types
remain runtime checks; known nonmap subjects or incompatible leaf types still
refuse. Re-executing a prepared query binds the current parameter map, not the
previous call's values. A late invalid nested subject rolls back the whole write
instruction. Existing AST/plan, result-value and spill budgets apply; this adds no
configuration, persistent type or alternate execution mode.

Map expressions accept the empty string as a key, with both indexed and dotted
access:

```cypher
WITH {``: {``: 7}} AS m
RETURN m[''][''] AS value, m.``.`` AS same_value
```

Missing empty keys propagate NULL just like other missing map keys. Duplicate
keys still refuse. This does not permit empty variable, alias, function or schema
identifiers, and does not change stored column-name validation.

Property/subscript type refusals carry additive `GrafxPlanError.details` evidence:
`query_phase="planning"` for statically proven invalid types and
`query_phase="execution"` for parameter binding or row evaluation. Their `reason`
is `property_subject_type`, `subscript_subject_type`, `map_key_type` or
`list_index_type`. Parameter validation can precede the first row or write and is
still execution, not static planning. This is a scoped contract for these
refusals, not a promise that every error already carries phase evidence.
Grammar-token mismatches raised by the parser expose
`GrafxParseError.details.reason="unexpected_syntax"` and
`query_phase="planning"`, alongside `expected`, `found` and the source location.
Other parse refusals (such as a resource limit) are not mislabeled as token mismatches.
Analysis also emits `undefined_variable` for missing/dropped/same-WITH aliases
and `invalid_aggregation_context` for an aggregate in a non-grouped clause
context, with `query_phase="planning"`. An invalid WITH sort aggregation is
checked across all sort keys before resolving those keys against the new scope.
Other unsupported capabilities are not mislabeled with these reasons.

`AND`, `OR`, `XOR` and `NOT` accept only BOOL or NULL operands. Numbers, strings,
lists and maps are neither truthy/falsy nor silently converted to unknown. Known
invalid types refuse at planning, including an otherwise short-circuited branch
or an empty scan. Bound invalid parameters refuse during execution before rows
or writes. Unknown row-dependent types are checked only when evaluated: runtime
short-circuiting still skips an unneeded operand. Refusals carry
`reason="boolean_operand_type"`, `field="operator"` and the corresponding
`query_phase`. A late failure rolls back the statement, not earlier successful
statements in the same transaction. This replaces the previous non-boolean-to-NULL
behavior; consumers must write explicit predicates instead of implicit truthiness.

`IN` accepts any query value on its left and only LIST or NULL on its right.
Known invalid right-hand types fail at planning (also over an empty input).
Invalid bound parameters fail before scanning/writing; unknown row values are
checked when evaluated. These `GrafxPlanError` refusals expose
`field="operator"`, `value="IN"`, `reason="membership_operand_type"` and the
actual `query_phase`. NULL/nested equality and dynamic boolean short-circuiting
are unchanged; a late failure rolls back the complete statement.

Scalar literal expression identity distinguishes BOOL, INT64 and DOUBLE. A
grouped projection of `true`, `1` and `1.0` preserves their respective result types;
one literal cannot overwrite another in an expression memo. This does not change
value comparison: `1 = 1.0` is true and `true = 1` is false, nor does it change the
documented numeric grouping equivalence for data values.

Equality/ordering operators (`=`, `<>`, `<`, `<=`, `>`, `>=`, plus the `!=` alias)
form adjacent-pair chains: `1 < x <= 5` means `1 < x AND x <= 5`, not a comparison
of a boolean with 5. Explicit parentheses retain their meaning. Three-valued AND
applies: a false pair decides false; otherwise any unknown pair makes the chain
NULL. Predicates retain only true. The existing short-circuit and expression-depth
budgets apply; operands are not granted a new single-evaluation guarantee. Bind
a value with `WITH expression AS x` when it must be reused. IN, string predicates
and null checks bind above simple comparisons; they are not themselves chainable
equality/order operators. [Reference](https://neo4j.com/docs/cypher-manual/4.4/syntax/operators/#cypher-operations-chaining).

`rand()` takes no arguments and returns a pseudorandom DOUBLE in `[0, 1)`. The
composition root owns a random stream per handle; no seeding/configuration option
is exposed. This is not cryptographic randomness or an identifier generator.
Each evaluated occurrence draws a value: no constant folding, cross-execution
result cache or common-subexpression memoization. Separate occurrences also stay
distinct in grouping/aggregation; an explicit alias reuses its already drawn value.
CASE/COALESCE and boolean short-circuiting do not evaluate unselected branches.
An empty pipeline produces no projection draws. ORDER BY an alias sorts the drawn
values; a write stores its drawn DOUBLE normally and recovery never redraws it.
Rollback undoes graph writes, not the pseudorandom stream's consumption. Durable
logical view definitions reject rand, including nested/unreachable occurrences;
index definitions still accept declared properties, not random expressions.

| Column/value | Python boundary / restrictions |
| --- | --- |
| `INT64` | Signed 64-bit integer; bool is not an integer substitute |
| `DOUBLE` | Python float; numeric promotion where the specific expression contract permits it |
| `STRING` | Unicode string; parameter/result and source-literal limits are independent |
| `BOOL` / `BOOLEAN` | Python bool |
| `BLOB` | Detached bytes; encode explicitly at JSON boundaries |
| `UUID` | Grafx `Uuid` wrapper, available from `okto_grafx.domain.model`; use `Uuid.from_hex(text)` or `Uuid(raw=...)`, `.canonical()` for JSON |
| `TIMESTAMP` | `Timestamp(micros=...)`, signed UTC microseconds since Unix epoch; `Timestamp.from_wall(seconds)` is explicit Python conversion |
| `DATE`, `LOCALTIME`, `TIME`, `LOCALDATETIME`, `DATETIME`, `DURATION` | Native temporal wrappers, typed/ANY/nested persistence and parameter/results; require catalog v2. [Constructors, example and current integration limits](TEMPORAL_VALUES.md) |
| `ANY` | Heterogeneous persisted property, retaining each concrete value type; requires catalog v2. No ANY property indexes or unchecked embeddings; [contract and example](architecture/HETEROGENEOUS_PROPERTIES_V1.md) |
| `VECTOR(space)` | `VectorValue(values, space_ref, dtype)` on storage/result boundary; numeric lists/tuples can bind to declared vector columns; space/dimension/dtype/finite-component checks apply |
| Null | Python `None`; not an absent result row |
| Query lists/maps | Input list/tuple and string-keyed maps; output lists detach to tuples. Also persistable inside ANY; standalone LIST/MAP column DDL remains separate work |

Graph row projections are backend-specific detached values. Prefer explicit scalar
property projections for stable integrations. Do not assume `uuid.UUID` or
`datetime.datetime` is automatically accepted in place of Grafx wrappers.

Encapsulated vector write parameters obey the same target-space admission as numeric
lists: identity, precision, dimension, active state and normalized declaration are
checked before staging. [Arrow vector interop](EXTENSIONS_AND_ARROW.md#explicit-native-vectors)
does not bypass those checks. [Detached graph algorithms](GRAPH_PROJECTIONS.md) are
Python APIs over a captured snapshot, not new Cypher syntax or a relaxation of the
query-path restrictions above.

`WHERE` retains true predicates; null is not true. `IS NULL`/`IS NOT NULL` test
null explicitly. Missing properties on polymorphic label-free reads are null;
different declared property families retain their actual per-row values, with
dynamic operator checks rather than a blanket pre-stream refusal. Parameter-map
keys are case-sensitive for dot and string-key bracket access; missing keys return
null. `a` and `A` are distinct keys. See the breaking-language migration in
[the compatibility contract](CYPHER_COMPATIBILITY.md).

## Expressions and functions

Supported expression operators include arithmetic `+ - * / %`, comparison
`= <> != < <= > >=`, boolean `AND OR NOT`, null predicates, `IN`, string predicates
`STARTS WITH`/`ENDS WITH`, list extraction and CASE. They remain typed operations,
not Python coercion of arbitrary objects. Unsupported operators/functions refuse.

### Heterogeneous properties across tables

An untyped/polymorphic match can read a property declared STRING in one table and
INT64 in another. Projections retain those values without stringification or
changing stored column types. Missing properties return NULL. Ordering comparisons
between a number and a string return NULL; therefore
`WHERE n.var > 'te' OR n.var IS NOT NULL` retains non-NULL numeric rows through
the second operand. Equality of a number and a string is false. Existing ORDER BY
type ordering is unchanged and differs from predicate comparison semantics.

Where eligible tables agree, the planner retains the provable type, including the
existing INT64/DOUBLE common-type rule. Different families leave the expression
type dynamic; scalar functions/arithmetic validate values when evaluated. For
example, `upper(n.var)` rejects a selected integer, while a CASE or preceding
filter may exclude that value. This composes with aliases, optional hops,
subqueries, pattern comprehensions, grouping and existing query spill. Native
stored-column admission stays strict; this is not a new ANY/union column type.
Late execution failures roll back the whole statement and preserve prior
successful transaction work. No configuration or stored-format change is needed.

### Function overview

| Function / expression | Semantics important to an integrator |
| --- | --- |
| `count(*)`, `count(expr)`, aggregate `DISTINCT` | Counts rows / non-null values respectively; duplicate removal follows the typed value rules |
| `sum(expr)`, `avg(expr)` | Ignore NULL inputs and refuse other nonnumeric values; integer-only SUM preserves integer precision; empty/all-NULL SUM is 0, AVG is NULL |
| `min(expr)`, `max(expr)`, `collect(expr)` | Typed aggregation; collect creates a real public tuple, not a lazy/disk proxy; memory/row budgets can refuse |
| `coalesce(value, ...)` | At least one positional argument; evaluate only until first non-null; heterogeneous/list/map values allowed; chosen value retains its type |
| `split(text, separator)`, `string_split(...)` | Two strings, NULL propagates; all empty fields retained; empty separator splits code points; two empty inputs yield one empty string |
| `size(value)` | One string/list; code-point/element count; null propagates |
| `label(binding)` | One matched node/relationship; physical table name; null propagates; not a logical Pulse relationship type mapper |
| `labels(node)` | Native node or NULL; singleton tuple containing its declared table label, or NULL; relationships and paths are not nodes |
| `type(relationship)` | Native relationship or NULL; logical group name for a grouped relationship, ordinary table name otherwise, or NULL; a written range binds a list, not one relationship |
| `properties(value)` | Native node/relationship, map or NULL; owned property map or NULL; entity NULL columns are omitted, input-map NULL entries preserved |
| `timestamp(value)` | Timestamp passthrough, null, or ISO-8601 string normalized to UTC; no zone means UTC; numeric epoch arguments refuse |
| `similarity(n.embedding, $q, space => 'space')` | Planned vector-search extension, not an arbitrary scalar UDF; literal/bound space, declared vector column and supported query shape required |
| `udf('app.name', value, ...)` | Optional trusted per-connection scalar registry; literal name, positional exact typed scalars, NULL propagation; [SPI and restrictions](EXTENSIONS_AND_ARROW.md) |
| `similarity_score()` | Zero arguments; only where a similarity operator has supplied a score |
| `CASE WHEN ... THEN ... ELSE ... END` / simple CASE | Evaluate conditions in order and only the chosen result; absent ELSE null; `CASE null WHEN null` does not match. Static scope/type checks still cover all arms |
| `list[index]` | Zero-based positive index, negative from end; null and out-of-range return null; noninteger indices refuse |
| `list[start..end]` | Half-open slice, negative/clipped/omitted bounds; an explicit NULL bound yields NULL |
| `map[key]`, `map.key` | Case-sensitive string keys; absent keys return null; integer map keys refuse |

Use `db.explain(text)` and actual execution on representative values to validate a
new query shape. There is no blanket `PROFILE`/arbitrary procedure or Neo4j function
catalogue. Statistics show selected operators/work, not a comprehensive SQL profiler.

## OPTIONAL, UNION and traversal boundaries

### Native entity scalar consumption

```cypher
MATCH p=(a:Person)-[r:Knows]->(b:Person)
RETURN labels(a), type(r), properties(a), properties(r), properties(nodes(p)[0])
```

Each function takes exactly one positional argument; no DISTINCT, star or named
argument form. `properties()` exposes user properties only, never physical edge
endpoint columns or identity metadata. User properties named `_ID` or `_SRC` remain
ordinary keys. All stored property values, including vectors, are materialized.
NULL-valued entity columns represent absent graph properties and are omitted.
For a supplied map all entries, including explicit NULLs, are preserved. The
public map is detached: mutation cannot change the graph or a caller's parameter.
It grants no write authority.

`labels()` returns the node version's complete sorted label tuple, including an
empty tuple for an unlabeled node. The native-label development checkpoint also
supports multiple names on one identity. `type()` returns the native
logical relationship type, not an internal physical endpoint-pair member name.
Aliases, polymorphic nodes, path components, returning subqueries, UNION,
aggregation and cursors compose normally. NULL input, including OPTIONAL null
extension, remains NULL rather than an empty map, list or name.

For entity functions, selecting an element of a heterogeneous list retains its
joined dynamic element type, even with a literal index: `labels([n, 1][1])`
refuses at execution when a row reaches it, not during planning. This preserves
the original Graph3 #0009 error phase and zero-row behavior. Direct proven scalar
or wrong entity-kind arguments and invalid subscript shapes still refuse during
planning. A runtime refusal remains inside the statement rollback boundary.

`startNode(r)` / `endNode(r)` return native source/target nodes in the physical
direction of the relationship, including when traversal or MERGE was undirected.
They take exactly one relationship or NULL; wrong types refuse. Returned nodes
compose with aliases, property functions, CASE/list selection, MATCH, SET and DELETE,
using owner-visible values and the reader's snapshot. Reading an endpoint from an
already deleted relationship refuses. Public results use the existing detached
`NodeValue`, not a writable external handle.
[Endpoint, undirected-MERGE and sorted-write contract](specs/WRITE_PATTERN_CONTRACTS_V1.md#undirected-merge-and-native-endpoint-functions).

Known invalid argument types fail in planning. Unknown row/parameter types are
checked when invoked: non-selected CASE arms and zero-row input do not call the
function. Native errors carry `field=function`, the uppercase function name,
`reason=entity_function_argument_type` and `query_phase=planning` or `execution`.
Late execution errors retain whole-statement rollback. Owner-visible SET changes
are read under the existing transaction; independent readers retain their
snapshot. Existing value, memory, row and cancellation budgets apply. No new
connection option or storage format is introduced.

### Untyped and alternative-type single hops

`MATCH (a)-[r]->(b)` reads all eligible relationship tables; `[r:A|B]` selects
the declared alternatives. Repeating a type does not repeat its edges; distinct
parallel edges remain distinct. All three directions, anonymous bindings, typed
or polymorphic endpoints, filters, node property maps and bound targets work in
composed MATCH/OPTIONAL, WITH/UNWIND, returning subqueries and UNION. A NULL bound
endpoint is not a fresh variable. An undirected self-loop is one physical match.

```cypher
MATCH p=(a:Person {id:$id})-[r:KNOWS|MENTORS]->(b)
RETURN p, type(r), labels(b), properties(r)
```

Named paths can concatenate these single-hop segments with other admitted
segments; the same relationship cannot be reused within a MATCH clause.
Separate MATCH clauses may reuse it. An incoming relationship variable constrains
the next pattern to the same qualified identity; it is not replaced by a new
candidate, even across aliases, different relationship tables, OPTIONAL MATCH
or captured path segments. A conflicting relationship type gives no match, not
a replacement edge. With OPTIONAL, only newly introduced variables become NULL.
Repeated relationship variable names inside a single connected read pattern
are a static `relationship_uniqueness_violation`; reuse across comma-separated
patterns still obeys the clause-wide disjoint-edge rule and yields no match.
Candidate filtering uses existing traversal/scan operators and shared budgets;
this is not yet a dedicated bound-edge endpoint lookup optimization.
A written `*1..1` binds a relationship
tuple, whereas an unwritten single hop binds one relationship. Bounded variable
ranges also work across untyped/alternative types as described below. Inline
relationship property maps are native equality predicates; see below.
Node tables remain typed.

### Read arrows and type alternatives

Read arrows `<-->()` / `<-[r:R]->()` use the same undirected semantics as
`--()` / `-[r:R]-()`: either physical orientation may match, without inventing
a reverse edge or doubling a self-loop. Each segment retains its own direction;
mixing a bidirectional segment with a directed segment does not relax the latter.
Relationship alternatives accept both `:R|S` and `:R|:S`, including escaped type
names. Repeated alternatives do not duplicate candidate edges. AST descriptions
normalize optional colons but retain double-arrow spelling to distinguish invalid
writes. CREATE requires a single directed type. MERGE also permits a plain
undirected single edge between bound nodes; double-arrow writes remain invalid.

### Inline relationship property maps

`MATCH (a)-[r:R {kind:$kind}]->(b)` constrains candidate relationship properties.
Anonymous edges, typed/untyped alternatives, captured paths, OPTIONAL MATCH,
existential predicates and pattern comprehensions support the same syntax.
Multiple entries are conjunctive. Missing properties compare as NULL and do not
match; `{key:null}` is not an IS NULL test. An empty map imposes no condition.
Values may use parameters or admitted incoming expressions. Use a literal map
with explicit keys; this does not add `MATCH ()-[r $map]->()` syntax.
Bare parameter maps in node/relationship patterns refuse during parsing with
`reason="invalid_parameter_use"`, `field="pattern_properties"`, the parameter
name in `value` and `query_phase="planning"`. A parameter **value** inside an
explicit map, such as `{kind:$kind}`, is supported as before.

For `[rs:R*0..3 {kind:$kind}]`, **every relationship in the candidate range**
must satisfy the map. Zero hops satisfy it vacuously and return an empty edge
tuple. Filtering occurs inside the complete MATCH condition, before OPTIONAL
null extension; a re-matched edge retains its incoming identity. Existing
comparison/NULL and lazy expression semantics apply without coercing stored
values. CREATE/MERGE storage validation is unchanged.

The planner lowers maps to native equality predicates and `all` for ranges.
Existing scan/traversal, list-iteration, cancellation and statement rollback
budgets apply. Candidate paths may be enumerated before filtering: this does not
promise property-based traversal pruning or a new relationship index access path.

Missing properties on polymorphic bindings yield NULL; different declared
property families retain their per-row types. Missing alternatives contribute no
edges, while a corrupt/missing endpoint declaration is not silently discarded.
`TraverseAnyRelationship` draws upstream input once and shares snapshot, owner
overlay, intermediate rows, expansion/path, memory and cancellation limits.
Indexed anchors retain their access plan; unbound polymorphic roots can scan node
tables. This does not claim general cross-table index selection. Native entity
and path DTOs preserve qualified identity through cursor, sort and spill.
No Pulse-specific names, new option, storage format or transaction are required.

### Node-label predicates

`MATCH (n) RETURN n:Person AS is_person` evaluates a native boolean predicate.
Label names are static, case-sensitive identifiers; backticks permit quoted
spellings. Consecutive labels are a conjunction: `n:Person:Person` is equivalent
to `n:Person`. An unknown label is false for a non-NULL node, not a missing-table
error. `null:Person` and a NULL OPTIONAL MATCH binding produce NULL. NOT/AND/OR
retain their usual three-valued rules and original unaliased source headings.

The subject may be a node or relationship carried through aliases, CASE/list
selection, pattern comprehensions or subqueries. `r:T2` compares a relationship's
logical type (the same exact name returned by `type(r)`), not its physical member
table. Known scalar/path subjects are rejected during planning; dynamic wrong
types are rejected when evaluated, so an unselected CASE arm is not executed.
`r:T2:T2` is a conjunction; `r:T2:Other` is false. Native `LabelPredicate(subject,
labels)` syntax snapshots preserve an immutable, nonempty tuple of label names.

These expressions do not themselves mutate storage. In the native-label
development checkpoint, `n:Person:Company` can be true for one stable node identity
with both labels. Label-constrained MATCH selects conservative physical candidates
and then checks version membership; removing a table's base label also excludes
its node from PK/index hits for that label. Physical table constraints remain in
force. Dynamic `:$label` is not introduced. Existing expression/token,
cancellation and query budgets apply; the predicate itself has no new setting.

### Versioned label mutations (partial development checkpoint)

```cypher
MATCH (n:Person {id: 1})
SET n:Employee:Reviewed
REMOVE n:Person
RETURN labels(n), n:Employee:Reviewed, n
```

SET adds names and REMOVE subtracts them without moving/copying the node, changing
its properties or retargeting edges. Repeated names and unchanged membership are
no-ops; empty sets are allowed. NULL targets are no-ops, relationship/scalar
targets refuse. Property and label items preserve written order, and a failure
unwinds the entire statement, including newly admitted catalog candidates.
Owner queries see staged labels; other participants retain snapshot visibility
and the ordinary OCC/COMMIT requirements. ON CREATE SET / ON MATCH SET use this
same mutation door. Label-name admission inside a writing procedure requires its
schema authority; changing membership within already admitted candidates does not.

No new connection setting is added. Logical names are case-sensitive UTF-8,
backtick-quoted when necessary; the native count/byte limits and resource accounting
are in [the label contract](specs/NODE_LABELS_V1.md). `labels(n)` and detached
`NodeValue.labels` expose the complete sorted tuple. The existing nonstandard
`label(n)` extension still identifies the physical table, not full membership;
use `labels(n)` for logical labels.

### Creating and merging multiple labels (development checkpoint)

```cypher
CREATE (n:Person:Employee {id: 1})
RETURN n, labels(n)
```

This creates one node, not a copy per label. The first **written** label selects
the physical node table when it is a valid physical identifier. If that table
exists, its typed columns and primary key still apply; secondary labels do not
silently combine schemas. Otherwise ordinary flexible-schema creation applies.
A first label that cannot name a physical table (for example `São Paulo`) uses
the generated unlabeled flexible owner and stores the complete logical label set.
Unicode/quoted names keep their exact case and spelling. Repeated names are
idempotent. Existing legacy catalogs require explicit catalog-v2 activation with
`db.maintenance.ensure_identity_indexes()` before versioned label admission;
a refusal does not partially install the new membership.

```cypher
MERGE (n:Employee:Person {id: 1})
ON MATCH SET n.reviewed = true
RETURN n
```

MERGE searches candidate physical owners and checks each version's complete
membership, irrespective of written label order. It reuses every matching node;
extra labels are allowed. The creation owner is installed only if no match exists.
Properties and the match set are fixed before that invocation's ON MATCH actions.
The retained matches count against `query_memory_budget_bytes`; exhaustion fails
the statement atomically. Prior private writes may be materialized into
owner-private phases to stabilize identities, never into an inner COMMIT.
Nested CALL/EXISTS, rematches, comprehensions and named-path reads can observe
earlier labels in the same statement. A zero-row input installs no prospective
creation table or labels. Late errors unwind private phases and label admission.

Native [retained history](SYSTEM_TIME_HISTORY.md#historical-node-labels),
[existing-target copy](CATALOG_COPY.md#native-node-label-membership) and
[fresh-store transfer/resume format 4](LOGICAL_TRANSFER.md#node-label-artifact-format-4)
also preserve logical membership. The [installed package matrix](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
qualifies their old-reader boundary. **Not a completed multi-label release:** full
repository/profile regression and final installed Pulse qualification remain required. Do not activate
the internal development format on production data. See
[status and evidence](specs/NODE_LABELS_V1.md#query-create-merge-and-scan-checkpoint).

#### Independent node/relationship names

The development catalog supports a node label and relationship type with the same
case-sensitive name: `CREATE (n:R)-[:R]->(n)` creates distinct entity kinds.
`MATCH(n:R)` resolves nodes; `MATCH()-[r:R]->()` resolves relationships. An absent
name in one kind is not populated by its presence in the other kind.
Both ordinary typed physical tables and logical relationship groups participate.

An overlapping catalog requires v2 capability `graph_namespaces_v1` (bit 24).
For a nonempty legacy v1 catalog, upgrade with
`db.maintenance.ensure_identity_indexes()` before introducing the model; existing
old binaries must not participate after activation. No table ID or endpoint is
renamed. See the [integration/qualification contract](specs/GRAPH_NAMESPACES_V1.md)
for unfinished transport, index and installed-consumer qualification; a passing
query profile alone is not release acceptance.

Public `db.catalog.catalog.table("R", kind="node")` and `kind="rel"` select
physical tables; `table_by_id(id)` remains unambiguous. Bare `table("R")` refuses
if both physical kinds exist (`ambiguous_table_name`). `has_table` accepts the
same optional kind. Logical groups still resolve via `relationship_tables("R")`.
`reader.scan_rows_v1("R", kind="rel", limit=100)` offers the same qualification;
a continuation cannot switch to the same-name sibling. Other name-only APIs
without a kind selector must refuse ambiguity, not guess an entity kind.

### Existential pattern predicates

WHERE accepts a relationship pattern as a BOOL/NULL predicate, for example:

```cypher
MATCH (n:Person)
WHERE (n)-[:KNOWS|MENTORS*1..3]->() AND NOT (n)<-[:BLOCKS]-()
RETURN n.id
```

The predicate asks whether a matching trail exists; multiple matching paths do
not multiply the outer row. It stops after the first witness and closes the
inner iterator. An unsuccessful search must exhaust its admitted search space;
exceeding a traversal/cancellation/resource limit raises rather than claiming
false. An omitted upper bound retains the ordinary traversal ceiling and
explicit overflow contract, not an unlimited search promise.

Named nodes and relationships must already be bound in the incoming scope.
Anonymous nodes/edges may be searched, and a named relationship constrains its
qualified physical identity. NULL references propagate NULL and are never
reinterpreted as unbound scans. Directions, alternatives, explicit ranges,
zero-hop ranges, bound endpoints and inline node/relationship-property conditions
reuse native MATCH planning. AND/OR/NOT keep their normal lazy three-valued evaluation.

Predicates are also available in WITH WHERE and inside admitted returning read
subqueries. They share the caller's transaction snapshot, pending writes,
catalog/index authority and cumulative traversal budgets; no nested transaction
or runtime query-string generation is used. There is no new configuration.
Raw pattern values in RETURN/WITH projections and SET remain syntax errors;
pattern comprehensions use the explicit list syntax below. A standalone
node/relationship/path is not a boolean WHERE predicate and is rejected during
semantic analysis, even over an empty graph.

### Pattern comprehensions

`MATCH (n:N) RETURN [(n)-[r:R]->(m) WHERE r.weight > $minimum | m.name] AS names`
materializes one projected list per outer row. The pattern may declare local
nodes/relationships and a named path (`[p=(n)-[:R*0..3]->() | p]`). Already-bound
endpoints/relationships constrain the match by entity identity. Local names do
not escape; names dropped by WITH are not accidentally reused as correlations.
Nested comprehensions and graph references from list iteration are supported:
`MATCH p=(n:N)-->() RETURN [x IN nodes(p) | size([(x)-->() | 1])] AS degrees`.
Scalar values or relationships used as node anchors are rejected, not rescanned.

No matching path produces an empty list; a NULL correlated anchor produces NULL.
Each matching trail contributes one element, including duplicate projections or
NULL projected values. Incoming/undirected patterns, relationship alternatives,
zero-length and bounded/omitted-upper paths use the native traversal semantics.
There is no implicit list ordering guarantee. Use UNWIND followed by ORDER BY to
obtain ordered rows. Comprehensions may be used in RETURN, WITH/grouping, predicates
and returning read subqueries; aggregates inside the local projection/WHERE are
not allowed. An outer `count(comprehension)` counts non-NULL lists, including empty
ones. Entity/path elements become detached public values at the result boundary.

Execution uses native correlated subplans, not generated query text or another
transaction. Writes earlier in the same statement are privately staged before a
following comprehension reads them; they are not committed early. Projection,
budget and cancellation failures retain the existing whole-statement rollback.
Prepared execution observes the invocation's snapshot and transaction overlay.

Traversal expansion/path limits and cancellation are shared with the statement.
Comprehension elements share the fixed 100,000 statement iteration allowance with
list iteration; exceeding it refuses rather than truncates. When configured,
`query_memory_budget_bytes` also bounds cumulative comprehension materialization
across outer rows and nesting: conservative logical charges include strings,
collections and entity/path payloads, not just IDs. Shared/repeated payloads may
be charged again. This is not RSS measurement or a retained-live-object estimate;
charges reset at the next statement. These list values must materialize and do
not spill themselves; downstream supported sort/group/DISTINCT operators retain
their own spill behavior. No new configuration option or storage format is added.

### Heterogeneous bounded relationship ranges

`MATCH p=(a)-[r:A|B*0..5]->(b) RETURN p,r` can change relationship and node tables
at every hop. Omitting types (`[r*0..5]`) selects all eligible relationship tables.
Incoming/undirected ranges, anonymous nodes/edges, repeated nodes and cycles are
supported, but each physical relationship can occur only once in a MATCH trail.
Same table-local IDs in different tables are distinct. Parallel edges stay distinct;
an undirected self-loop is one edge. Depth-first output has no shortest-path or
implicit ordering guarantee: use ORDER BY when order matters.

The final target label/property or previously bound target filters completed
paths; it must not prune an intermediate node in a different table. Minimum zero
includes a real anchor path even when its node table is unrelated to the selected
edge types. A NULL anchor/destination is never rebound. Multiple captured segments
concatenate without duplicating junction nodes; clause-wide edge uniqueness still
applies. Explicit counts stay within 0..30. Written range variables are tuples of
native relationships, including an empty tuple for zero hops; list typing survives
WITH, subquery imports/exports and proved list-or-NULL UNION outputs.

`TraverseRelationshipAlternatives` selects schema-reachable tables and uses the
same depth-first iterator stack, snapshot/owner overlays and expansion/path quotas
as typed ranges. The final label does not limit intermediate schema reachability.
The engine retains one active sibling iterator per depth, not all complete paths
in a breadth-wide queue. Node/path values also retain normal memory/value limits.
Cursor close/cancellation closes active iterators. Late statement errors preserve
rollback; earlier successful statements remain only under the usual proof.

For `*`, `*n..` or `*..`, the 30-hop count is a resource ceiling, not a silent
answer bound. An eligible unused edge at hop 31 raises `GrafxQueryBudgetExceeded`
with `field=max_traversal_hops`, `limit=30`, `observed=31`. Schema selection includes
types first reachable by that completeness probe. Owner deletions and old reader
snapshots apply to it as to ordinary expansion. LIMIT/cursor close may stop before
the probe; ORDER BY may require it. Explicit `*0..30` deliberately excludes hop 31.
No new setting, storage format or application-specific behavior is introduced.

### Absent tables in read patterns

A missing node label or positive-length relationship type in a read pattern
means no match, not a catalog error or implicit table creation. `MATCH
(n:Missing) RETURN count(*)` returns `0`; `OPTIONAL MATCH (n:Missing) RETURN
properties(n)` returns one NULL on an otherwise empty input pipeline. Optional
failure null-extends only new bindings and preserves incoming values, including
already-bound NULLs. Static name, entity-kind and supported-syntax checks still
apply even when a pattern cannot produce rows. Writes continue to require schema.

An absent relationship type is not necessarily an empty variable-length pattern:
`MATCH p=(a:Person)-[:Missing*0..]->(b:Person) RETURN p` can match the zero-edge
path at each eligible anchor. `a` and `b` must have the same qualified identity;
an already-bound NULL destination never becomes a fresh node. Node label and
property predicates still apply. A captured path has one node and no edges, or
retains the preceding path when this is a zero-edge continuation. Positive
minimum ranges have no matches. No relationship table or fake endpoint is created.

EXPLAIN exposes `ZeroHopRelationship` for that zero-edge branch. It preserves
indexed anchor access and uses the shared `max_traversal_paths` and cancellation
budgets without relationship expansion. Empty positive patterns still consume
upstream operations, so they cannot suppress earlier writes in the same statement.
Statement errors retain rollback; plans are invalidated by subsequent schema
creation. These semantics add no option and do not relax the typed storage model.

`OPTIONAL MATCH (n:Person) RETURN n.id` produces a null-extended row when no
candidate qualifies. Typed optional patterns can correlate with incoming values;
all new bindings are NULL only if the complete optional clause fails. The existing
optimized incident-hop path is also retained:

```cypher
MATCH (d:Decision)
OPTIONAL MATCH (d)-[r]-()
RETURN d.id, count(r)
```

This is generic native execution. Additional incident OPTIONAL hops from the same
anchor may be interleaved with WITH projections/aggregations; carry the anchor
forward. Aggregate one degree before the next expansion to avoid a cross-product.
Fresh aliases, direction, optional WHERE and target-label checks apply. No matching
edge yields `count(r)=0`, `count(*)=1`; an empty mandatory root yields no anchor.
An undirected self-loop has one physical match (`count(r)=1`,
`count(DISTINCT r)=1`); distinct parallel edges retain multiplicity. Generic typed optional application is separate from that
specialized fast path. Untyped multi-hop, named-path and vector restrictions are
not removed merely by supporting clause composition.

`RETURN 1 AS n UNION RETURN 1.0 AS n` returns one value, retaining the first
occurrence's type. UNION ALL returns both `1` and `1.0` without conversion.
Branches share a snapshot and budgets and must publish identical ordered column
names. Branch-local DISTINCT/windows remain local. Each chain uses only UNION or
only UNION ALL; mixed policies fail during planning. Returning subqueries provide
separate scopes for combining policies and an explicit outer projection/sort.
Write branches remain refused. See [composition, scopes and bounds](COMPOSABLE_QUERIES.md).

### Native scalar additions (0.0.6)

`lower`, `upper`, `trim` and `abs` take exactly one positional argument; no star,
DISTINCT or named arguments. NULL propagates. String functions accept STRING only:
lower/upper use Python Unicode case conversion (not locale-specific collation or
casefold; output length may change), and trim removes leading/trailing Unicode
whitespace, preserving internal whitespace. Unicode tables follow the supported
Python runtime; these are not persistent index normalization changes.

`abs` accepts INT64/DOUBLE/native DECIMAL, not BOOL or numeric strings. It preserves numeric type
and refuses signed-INT64-min overflow and infinite results rather than widening
or wrapping; expression NaN propagates. Known schema/literal and bound parameter type errors are rejected
before rows, including empty matches; row-dependent numeric overflow is checked
when evaluated. Composed expressions remain subject to the existing query subset
and budgets. These functions are built in, not trusted host UDFs.

Additional native families in this development round:

| Family | Functions / contract |
|---|---|
| String | `ltrim`, `rtrim`, `toLower`, `toUpper`, `substring(text, start [, length])`, `left(text, count)`, `right(text, count)`, `replace(text, search, replacement)` |
| Numeric | `ceil`/`ceiling`, `floor`, `sqrt`, `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2(y, x)`, `degrees`, `radians`, `sign`, unary `round`; `pi()` and `e()` |
| Conversion | `toInteger`, `toFloat`, `toBoolean`, `toString`; explicit scalar conversion, not implicit result-column coercion |
| List/map/entity | `head`, `last`, `tail`, `reverse` (also STRING), `keys(map\|node\|relationship\|NULL)`, inclusive `range(start, end [, step])` |

NULL propagates through these functions. Empty head/last returns NULL; empty tail
is empty. Bounds/counts require integers (not BOOL); negative substring/count
arguments and zero range step refuse. Materialized ranges have a hard cap of
100,000 elements, checked before allocation. Direct `UNWIND range(...)` uses an
allocation-free sequence and the same cap on **consumed elements per incoming
row**; exceeding it fails explicitly. A downstream LIMIT can therefore consume a
bounded prefix of a larger range. Aliasing/materializing the list first still
uses the full-list cap. Existing intermediate/result/write budgets and cursor
cancellation remain authoritative. No new configuration is introduced.

For example, `UNWIND range(1000000,2000000) AS i WITH i LIMIT 3000 RETURN sum(i)`
returns `3004498500` without allocating a million-element carrier. LIMIT does not
pull an extra discarded read row, including at zero, and closes its input on
exhaustion/failure/early close. Final RETURN LIMIT still preserves **all** writes
before it, including LIMIT 0; a WITH LIMIT before a write controls input rows.
If the consumed-range or write budget fails, the entire statement rolls back.

`range` checks operand types and step bounds **when evaluated**, for both direct
UNWIND and materialized results. Static inference reports LIST/NULL without
evaluating the operands. Invalid literals therefore do not fail in an unselected
CASE branch or over an empty input. Non-NULL operands must be INT64 (BOOL is not
an integer); invalid types are rejected before NULL propagation. A zero step or
out-of-INT64 operand is rejected after NULL propagation. Arity and literal syntax
remain compile-time checks. Runtime refusals are `GrafxPlanError` with
`field="function"`, `value="RANGE"`, `query_phase="execution"` and reason
`range_argument_type` or `range_argument_bounds`. Late failures roll back all
effects of that statement and retain only proven successful prior statements.

Replacement output is bounded by the 1 MiB
hard query-value character ceiling, with the configured public value limit still
applied at the boundary. Finite-input math domain errors and infinite results
refuse. Numeric math functions propagate an existing expression NaN, except
`sign(NaN)`, which returns integer zero. ROUND currently accepts
one argument only. Invalid textual numeric conversions return NULL; boolean/string
conversion uses explicit supported scalar families, not arbitrary Python objects.
`toInteger` parses ASCII decimal/exponent strings exactly and truncates toward zero;
the original value must fit INT64. It does not round through DOUBLE, depend on a
host decimal context or allocate integers proportional to a written exponent.
These fixed bounds have no new connection option.

### Conversion and arithmetic error contracts

Conversions validate the **evaluated value**, without host-object coercion:

| Function | Accepted non-NULL input families | Unconvertible text |
| --- | --- | --- |
| `toBoolean` | BOOL, STRING | NULL |
| `toInteger` | BOOL, INT64, DOUBLE, STRING | NULL |
| `toFloat` | INT64, DOUBLE, STRING (not BOOL) | NULL |
| `toString` | BOOL, INT64, DOUBLE, STRING | Already a string |

NULL returns NULL. Lists, maps, nodes, relationships and paths are not implicitly
converted or rendered with Python callbacks. Unsupported value types raise
`GrafxPlanError` with `field="function"`, the uppercase function in `value`,
`reason="conversion_argument_type"`, and `query_phase="execution"`. The native
TCK mapping is `TypeError` / runtime / `InvalidArgumentValue`; malformed numeric or
boolean **text** remains NULL, not this error. Unselected conversion calls in CASE
and calls over empty input are not evaluated. Arity/name checks still occur during
planning. Native DECIMAL now additionally supports explicit `decimal(value,p,s[,rounding])`,
`toString`, `toInteger` and `toFloat`; their exactness/refusal policies are documented
[separately](specs/DECIMAL_VALUES_V1.md#native-numeric-query-contract). No general
temporal cast or automatic decimal-to-float promotion is implied.

The planner now retains the common known element type of literal-list sources for
lexically unique local binders. Nested and sibling list binders do not share type
state; a heterogeneous, parameter-dependent or unknown source remains unknown.
Reduce accumulators are not assigned a permanent type from their initial value.
Arithmetic subexpressions with provably incompatible types refuse during planning,
including modulo in quantifiers; BOOL is not a numeric operand. The native cause is
`field="expression"`, `operator` naming the operation,
`reason="arithmetic_operand_type"`, `query_phase="planning"` and `value` containing
the expression description. It maps to `SyntaxError` / compile time /
`InvalidArgumentType`. This is an admission change: static invalid arithmetic no
longer requires a row to execute before refusal.

When operand types are only known at binding/evaluation, arithmetic type refusal
uses `field="operator"`, the operation in `value`, the same reason and
`query_phase="execution"`; it maps to `TypeError` / runtime /
`InvalidArgumentType`. Existing CASE parameter-type validation can occur before
rows or before a branch is selected; runtime phase does not promise lazy parameter
admission. Numeric NULL propagation and supported string/list addition are unchanged.
These structured fields replace formerly unclassified errors; consumers should
inspect causes, not assume the old two-field error dictionary is exhaustive.

Late conversion/arithmetic failures discard the entire instruction's writes,
including prior input rows, and preserve only independently successful earlier
instructions. No new configuration, storage format or transaction mode is added.
For data-only rollback with unchanged catalog identity and no schema-effect suffix,
the bounded owner landing memo may remain allocated. Reuse still checks transaction,
snapshot and schema identity, heap epoch and the complete intent/held-row fingerprint;
an intermediate poisoned view must rebuild. Schema rollback retires that memo, and
endpoint/primary-key memo invalidation is unchanged. This avoids decoding unchanged
landings again without bypassing read visibility or rollback validation.

Typed traversal supports bounded directions/ranges and preserves relationship
isomorphism and parallel-edge multiplicity. Explicit upper bounds can reach **30**;
zero-length and empty ranges are supported; negative/over-ceiling bounds refuse.
Omitted upper bounds use the resource-failure policy below.
Label-free typed endpoints and untyped relationships have closed forms,
not permission for every arbitrary pattern. Independent traversal expansion/path
budgets remain authoritative even with LIMIT.

The typed one-hop `MATCH p=(a:A)-[r:R]->(b:B) RETURN p`
returns a native `PathValue` with `nodes` and `relationships` tuples containing
`NodeValue` and `RelationshipValue`. Components share the qualified identity,
property and snapshot contract of direct entity returns. User properties live
separately from metadata, so `_ID`/`_LABEL`/`_SRC`/`_DST` property names no longer
conflict. Native typed capture supports aliases,
multiple RETURN expressions and `length(p)`, `nodes(p)`, `relationships(p)`;
NULL input returns NULL. Nodes/relationships return detached native components,
which may be consumed by list expressions. Arbitrary maps are not accepted as
paths. `size(p)` and `p.property` refuse. Captures compose with WHERE, WITH (including
star expansion), UNWIND, DISTINCT, ordering, SKIP/LIMIT, typed OPTIONAL MATCH and
returning subqueries. Incoming and undirected walks retain the physical relationship
endpoints while ordering the path's nodes in walking order. An unmatched optional
clause yields a NULL path. Schema-resolvable unlabelled endpoints and anonymous
relationship variables are supported. Typed and heterogeneous bounded ranges and
multiple segments are captured natively; inline relationship maps filter all
relationships of each annotated range.
UNION and cursors preserve the captured
path, including pending identity and spill metadata. See [native paths](ENTITY_VALUES.md#paths)
for the breaking result/JSON contract. This is not a claim of general path algebra.

```cypher
MATCH p=(a:Person {id:$id})-[r:Knows*0..3]->(b:Person)
RETURN p, length(p), nodes(p), relationships(p), r
ORDER BY length(p)
```

An explicitly bounded typed segment accepts `0 <= minimum <= maximum <= 30`.
Node-only named patterns also work: `MATCH p=(n:Person) RETURN p` captures one
matched node with no relationships. `MATCH p=(n)` and `MATCH p=()` enumerate
node tables under the ordinary typed/polymorphic node rules; no relationship table
is needed. Inline node properties and WHERE retain normal node access paths,
including available index seeks. `length(p)=0`, `nodes(p)` has one native node,
and `relationships(p)` is empty. NULL or unmatched anchors produce no path;
OPTIONAL may null-extend the result. These captures compose with aliases, WITH,
returning subqueries, UNION/DISTINCT, aggregates and cursors. Each materialized
capture consumes one configured traversal-path unit but no edge expansion.
Unused decorative path names need not materialize a path.
After SET, direct node/relationship components of a bound path use the refreshed
versions, so property access through `nodes(p)[0]` agrees with the updated alias
in that statement. Independent readers retain their own snapshots.

`*0` returns its anchor as a path with one node and no relationships, including
isolated nodes; the two endpoint variables name that same qualified node. A bound
target still filters that identity. Zero-length matches need not belong to the
relationship table's endpoints, since they traverse no edge. NULL anchors produce
no path, and OPTIONAL may then null-extend the clause.

An explicit interval whose lower bound exceeds its upper bound (`*2..1`,
`*1..0`, `*..0`) is empty: it produces no path, not a zero-length path. Both
bounds must independently be integers in `0..30`. The AST preserves the bounds;
planning emits a consuming false filter instead of a traversal, retaining prior
upstream effects and applying normal OPTIONAL null extension and zero-row
aggregation. Node/relationship bindings remain typed for later static checks.
Expression-local patterns receive the same bound validation. Runtime traversal
operators still refuse malformed direct plans rather than accepting unsafe bounds.
Missing `*` before `..` and negative bounds report a native planning-phase
`invalid_relationship_pattern` parsing error; over-ceiling bounds retain the
existing explicit resource refusal.

Every written range binds its relationship variable to a tuple of relationships,
including `*1..1` (one element) and `*0` (empty). An unstarred single hop binds one
relationship entity. Adjacent captured segments concatenate at their common node
without duplicating that junction. Their relations remain unique across the walk.
WITH can construct an ordered relationship list (`[r1,r2]`) and re-match it as
`[rs*...]`; candidate paths must have that exact sequence and satisfy direction,
bounds and endpoint constraints. The list is not an unordered edge set or a
single relationship. Same-kind coalesce/CASE branches preserve this binding
kind and existing node/edge/path kinds without evaluating them during planning.
See [expression provenance and limits](COMPOSABLE_QUERIES.md#clause-order-and-scope).
Depth-first execution streams paths without retaining a breadth-wide frontier;
it is not a shortest-path ordering guarantee. Use ORDER BY where ordering matters.
Cursor close/limits stop and close unconsumed expansion iterators.

Omitting the upper bound (`*`, `*..`, `*n..`, including `*0..`) requests complete
trail enumeration, not an implicit range of 20. The AST preserves
`upper_bound_omitted`; EXPLAIN reports `hops: "n.."`, that flag and a fixed
`max_traversal_hops: 30` resource ceiling. At depth 30 the executor probes for a
further unused, snapshot-visible relationship and landing node. If one exists,
it raises `GrafxQueryBudgetExceeded` (`field="max_traversal_hops"`, `limit=30`,
`observed=31`) instead of returning an apparently complete truncated answer.
Dead ends and reused-edge-only continuations can finish at the ceiling.

The probe uses the same snapshot, pending-owner overlay, cancellation and
expansion/path quotas as ordinary traversal. A smaller configured quota may fail
first. Endpoint/WHERE filters do not exempt work needed to enumerate candidates.
`*25..` is now legal; an explicit count above 30 still refuses before traversal.
Write `*1..30` to deliberately restrict results to 30 hops. A streaming `LIMIT`
or explicit cursor close can stop before complete enumeration/probing is needed;
ORDER BY or another blocking operator may need full input and still refuse.
Cursor rows already consumed are a prefix, not a completeness certificate: a
later fetch can raise the resource error. Statement writes roll back on that
error while earlier successful statements retain their normal isolation.
This replaces the previous implicit-20 policy without a compatibility toggle.

Within one MATCH clause, relationship occurrences across its patterns/segments
must be disjoint, including anonymous relationships and variable-hop segments.
Qualified relationship identity keeps equal local IDs in different tables distinct.
Separate MATCH clauses may observe the same relationship again. Repeated nodes
alone do not violate trail uniqueness. The clause check runs before OPTIONAL null
extension; it uses normal bounded expression evaluation, not a new transaction or
an unbounded global graph scan.

## Dynamic entity properties and property removal

Native `n[key]` / `r[key]` read a string-named property of a node or relationship,
including entities carried through lists/maps and path components. A missing key
or NULL subject/key returns NULL; a non-string entity key refuses. Lookup sees
the owner's current private values under the original snapshot, not stale values
captured when a collection was constructed. It uses the revision-cached owner
overlay and direct column/map lookup, not a graph scan. Physical relationship
endpoint columns are not exposed as dynamic properties.

`keys(node|relationship)` returns non-NULL property names, excluding physical
endpoints. `keys(map)` retains keys whose map values are NULL; `keys(NULL)` is
NULL. Key order is not a sorting contract. Invalid argument types refuse;
runtime parameters in an unselected CASE arm are not evaluated, while statically
invalid argument types may refuse during planning.

```cypher
MATCH (n:Note)
REMOVE n.obsolete, n.temporary
RETURN keys(n), n[$property_name]
```

Property `REMOVE` compiles to the existing native NULL-assignment operator.
Thus it shares SET's typed constraints, snapshot/OCC, write quotas, staging and
whole-statement rollback; normalized plans show NULL assignments, not a second
mutation implementation. A nullable optional entity is a no-op, not an unknown
variable. Flexible missing properties are no-ops; explicit typed constraints
(including primary keys and required columns) are not weakened. Discarding
output with WHERE/SKIP/LIMIT does not discard required earlier removals.
Nested CALL/UNION removal is classified as writing before read-only admission.
No new API method, durable format or configuration is introduced.

SET/REMOVE of labels remain separate, required implementation work. Whole-map
SET is covered by the [map contract](specs/SET_PROPERTY_MAPS_V1.md). Current
evidence and remaining cases are tracked in
[the property-access checkpoint](conformance/PROPERTY_ACCESS_PROGRESS.md).

## Schema, indexes and query costs

CREATE may refer to a node declared earlier in the same pattern:
`CREATE (n:Person {id:1})-[:KNOWS]->(n)` inserts one node and one self-loop,
provided KNOWS declares Person-to-Person endpoints. Nonadjacent references also
close cycles without recreating nodes. Repeated occurrences are references, not
additional property assignments; use SET for updates. The first new node still
needs its declared table, and kind/label/endpoint/property checks remain active.
All staged nodes and relationships share the existing whole-statement rollback
boundary, including a later row failure. No new connection option is introduced.

Create tables and spaces inside write transactions. A node PK is automatically
indexed. Relationship endpoints are immutable; use a new relationship to change
them. `DELETE` refuses a node with surviving incident relationships; explicitly
delete those relationships in the same statement or use `DETACH DELETE`, which
ends incident relations atomically and retains broader corruption checks, so it may scan.

Custom index DDL/Python maintenance is detailed in [indexes](INDEXES_AND_VECTORS.md).
General ALTER/DROP/migration/view APIs are roadmap work, not implied by transactional
CREATE. Use [query budgets](CONFIGURATION.md) for untrusted cardinality. Streaming
the public result does not turn every sort/group/traversal into constant memory.

The engine [normative query contract](architecture/CONTRACT.md#89-enginequery_enginepy-c10)
and [Pulse compatibility corpus](specs/PULSE-QUERY-CORPUS-1.0.md) retain exact edge
cases and test provenance; they do not make every Pulse wrapper transformation a
native Grafx feature.
