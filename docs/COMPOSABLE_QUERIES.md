# Composable query execution (0.0.6 development)

[Schema-enabled procedures](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md) can create
native tables, vector spaces and indexes and use flexible CREATE/MERGE under
the caller's rollback boundary. `schema_write=True` and literal `schema` permission
are explicit; child calls cannot escalate an ancestor. Native v2 index DDL composes
with pending data, but outer scans retain their precompiled table sets.

Large consecutive CREATE pipelines now use a flat native `CreateSequence`, keeping
instruction order, named paths, intervening clause boundaries and whole-statement
rollback. This is not application-side batch rewriting or independent commits.
[Syntax limits, public plan and transaction contract](specs/WRITE_PATTERN_CONTRACTS_V1.md#large-create-pipelines).

Named CREATE/MERGE paths now compose with actions and imported writing calls.
Unbound and fixed multi-hop MERGE now uses complete-match-or-create semantics;
partial matches never silently reuse unbound entities. Conditional actions retain
owner-visible updates and one statement rollback boundary, including imported
CALL. [Contract, resource policy and evidence](specs/GENERAL_MERGE_V1.md).
MERGE following a mutation reads only after the preceding clause's inputs finish,
using the existing private phase/outer rollback boundary, not an inner COMMIT.
[Written-path and phase contracts](specs/WRITE_PATTERN_CONTRACTS_V1.md).

Bound-edge undirected MERGE searches both orientations before creating left-to-right.
Native `startNode()`/`endNode()` compose with aliases, CASE/list/UNWIND and entity
writes. Sorted WITH bindings also retain native authority under bounded external
sorting; no public entity DTO is promoted into a writable handle.

Expression/path DELETE now composes with imported writing subqueries and updating
branches under their existing outer rollback boundary. NULL is a no-op and native
entity identity is preserved through selectors; parameter data cannot grant writes.
[Deletion contract](specs/DELETE_EXPRESSIONS_V1.md).

Conditional `MERGE ... ON CREATE SET ... ON MATCH SET ...` property actions now
compose with imported writing subqueries and updating branches. Each match gets
its selected action; a later failing invocation rolls back the entire outer
statement, not just that invocation. Native entity references retain owner-visible
identity while explicitly projected scalar results capture their projection-time
value. [Syntax, limits and evidence](specs/MERGE_ACTIONS_V1.md).

Whole-property `SET n = map/entity` and `SET n += map/entity` use that same
boundary in actions, imported subqueries and updating branches. No independent
inner commit is introduced. [Map contract](specs/SET_PROPERTY_MAPS_V1.md).

Property `REMOVE n.key` shares native SET-to-NULL semantics through CALL/UNION,
including full effect draining despite discarded output and late-error rollback.
`keys(entity)` and dynamic string-key reads use native entity authority and
owner-current properties. [Contract and remaining label forms](QUERY_LANGUAGE.md#dynamic-entity-properties-and-property-removal).

Entity-valued collections now cross UNWIND without losing node/relationship
identity: collected nodes can be updated and rematched, and collected relations
keep their type/endpoints for SET/DELETE. Repeated identities preserve row
multiplicity and owner-current values. Slices and identity-preserving/filtering
comprehensions retain structural provenance; scalar/external collections do not
grant writes. Bounded spill reacquires identity authority before use.
[Consumption and failure semantics](QUERY_LANGUAGE.md#native-entity-collections-and-unwind).

Grouped/DISTINCT RETURN ordering now reuses complete projected expressions inside
compound keys, including aggregate results and unaliased columns. Type proof
resolves output definitions without reevaluating them; discarded input bindings
stay inaccessible, including after spill. See the
[RETURN scope, examples and native errors](QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

Projected maps now retain chained property access through WITH aliases, CASE,
parameters, NULL and returning subquery imports/exports. Type inference follows
literal selected paths without reexecuting the materialized expression; unknown
fields retain dynamic validation. Grouping/sorting spill uses the existing map
value representation. [Usage and error contract](QUERY_LANGUAGE.md#values-and-python-mapping).

Native percentileDisc/percentileCont now compose through grouping, WITH and
returning subqueries, including DISTINCT samples. The configured memory ceiling
uses external numeric sorting rather than retaining a full sample list. See
[signatures, formulas, errors and costs](QUERY_LANGUAGE.md#percentile-aggregates).

Aggregate arguments now reject native volatile calls during planning. Preserve
random aggregation by projecting samples first: `WITH rand() AS r RETURN sum(r)`.
This is a single development contract, not a compatibility mode. The
[migration recipe and error fields](QUERY_LANGUAGE.md#stable-aggregate-arguments-and-volatile-row-values)
also explain independent multiple draws and unchanged UDF declarations.

RETURN aliases are visible inside ORDER BY expressions and entity property reads,
including output/input shadowing. Grouping expressions now refuse unprojected
implicit leaves with a proven ambiguity cause, and grouped WITH distinguishes
missing inputs from illegal new aggregates. Nested aggregate refusals carry an
explicit native cause. See [examples and error fields](QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

Plain WITH ordering and attached WHERE now read both incoming bindings and output aliases, with
aliases taking precedence. Source-only sort inputs are retained privately only
through that sort/window/filter, then dropped without re-evaluating the projection.
They do not leak through `RETURN *` or into later clauses. DISTINCT/grouped
ordering does not reopen discarded rows. See the
[ordering contract and remaining scope work](QUERY_LANGUAGE.md#with-ordering-scope).

DISTINCT/grouped modifiers can reuse exactly projected subexpressions and aggregate
results; they cannot recover unrelated source properties. Substitution is lexical,
not algebraic, and respects output alias/list-local shadowing. All 19 original
WITH-WHERE cases pass and are included in the
[complete V3 required-profile execution](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md).
The declared lexical/scope rules still apply; this is not unrestricted Cypher compatibility.

`WITH`/`RETURN` row windows now accept row-independent scalar expressions.
Each reached window evaluates once per invocation, including repeated correlated
subquery calls; nondeterministic expressions are not duplicated into top-K/vector
operators. Literal/parameter fast paths remain eligible. See the
[window contract and cost](QUERY_LANGUAGE.md#row-independent-skip-and-limit-expressions).

[Query reference](QUERY_LANGUAGE.md) · [Fixed compatibility contract](CYPHER_COMPATIBILITY.md)

## EXISTS read subqueries

`EXISTS { ... }` is now a native boolean expression, with implicit outer imports,
local inner names, optional RETURN, aggregation/windows and nested read subqueries.
It shares the outer snapshot and budgets and stops after the first produced row
when the inner operators allow it. Writes inside the body refuse during planning,
even if the outer input is empty. An outer write statement can use the expression
without creating an inner commit. See [syntax, examples, API, errors and qualification](specs/EXISTS_SUBQUERIES_V1.md).

Native `RETURN *` now expands the visible lexical scope, including within
returning read subqueries and UNION branches, without exporting expression-local
or anonymous bindings. Actual entity/path identity survives projection. Expanded
columns remain bounded and duplicate/colliding exports refuse. See
[projection semantics and examples](QUERY_LANGUAGE.md#returning-the-visible-scope).

This documents the locally validated implementation, not full Cypher conformance
or a published release. The eight-item round's feature tests, complete Grafx
regression and affected Pulse migration tests passed; see the
[acceptance record](reports/V006_QUERY_LANGUAGE_ROUND.md) for exact coverage.

## Clause order and scope

WITH now preserves a proved entity kind through same-kind `coalesce` and CASE
result branches (ignoring NULL alternatives). Literal lists of relationships,
including an empty list, become relationship-list bindings; coalesce/CASE may
preserve that list kind as well. A subsequent MATCH tests the selected entity or
ordered relationship sequence rather than scanning and overwriting its alias.
Qualified table identities remain distinct and optional NULL nodes do not become
unbound scans. A relationship list can bind `[rs*...]`, not an unstarred `[rs]`.
Range lists also retain their kind across explicit subquery imports.

The proof inspects expression structure and existing bindings, never runtime
values or CASE conditions. Mixed scalar/entity branches and unproved expression
forms are not promoted to graph bindings. This checkpoint does not close all
expression/subquery provenance combinations required by FP-3. A single proved
table retains existing typed write admission. Multiple candidate tables now
retain real per-row write bindings; they do not allow a scalar/map or a detached
parameter to impersonate an entity. Actual schema checks still apply to each row.

Polymorphic reads can compose with standalone node MERGEs: each input contributes
all existing matches, followed by normal WITH/OPTIONAL/aggregation processing.
An unlabeled MERGE creates a native unlabeled node only when no node matches. A typed
MERGE creates only for an empty match set. All require a write transaction, even
when no mutation occurs. Bound-entity SET/DELETE and relationship creation use
[runtime schema/endpoint proofs](QUERY_LANGUAGE.md#writes-through-polymorphic-bindings).
See [matching, creation and rollback](QUERY_LANGUAGE.md#node-merge-matching-and-creation).

A captured path needs a fresh name relative to its own entities, earlier
comma-separated patterns and incoming bindings (`variable_already_bound`).
Using an already captured path as a node/relationship in a later pattern instead
reports `variable_type_conflict`. Reusing any entity binding as a different kind
remains a static error. These refusals carry explicit native scope/phase metadata;
they are not inferred from reference-test expectations.

Native node-label expressions (`n:Person`, `n:Person:Person`) compose with RETURN,
WHERE/WITH, list/pattern comprehensions, grouping and read subqueries. They return
NULL for a NULL node, preserve alias scope and do not introduce label writes or
multi-label storage. See [subject typing and examples](QUERY_LANGUAGE.md#node-label-predicates).

WHERE pattern predicates now use planned correlated reads. A witness admits the
outer row once; additional witnesses do not multiply it. WITH WHERE and returning
read subqueries retain lexical reference scope, lazy boolean evaluation and the
same statement snapshot/budgets. Named pattern entities must already be bound;
NULL is not an unbound lookup. Raw pattern projections and pattern comprehensions
are distinct contracts, not implicit lists of paths. See
[existential predicates](QUERY_LANGUAGE.md#existential-pattern-predicates).

Pattern comprehensions now execute as correlated native list projections, including
nested/list-local references, scalar/entity/path elements, WITH grouping and read
subqueries. A NULL anchor returns NULL; zero matches return an empty list. Writes
and subsequent reads share the outer statement's private staging/rollback boundary.
No inner transaction is introduced. See [semantics, examples and cumulative
materialization limits](QUERY_LANGUAGE.md#pattern-comprehensions).

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
entity bindings and optional NULLs, and works inside subqueries with only their
admitted imports and locally bound names. An empty incoming scope contributes no
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
cross-table index selection is not an asserted optimization. This scan cost is a
documented limit, not missing polymorphic-query functionality or a new parity gate.

Rematching an already-bound polymorphic node (including aliases, explicit imports
and single-query subquery exports) preserves its table-qualified internal binding.
It does not rescan or merge same-ID nodes from different tables. Optional NULL
bindings remain NULL; a subsequent mandatory rematch filters them out. An optional
multi-pattern clause null-extends all of its new names only if the complete clause
fails. Inline property maps use ordinary equality and the same property typing as
WHERE: missing keys yield NULL; different declared families retain per-row types
and use runtime operator checks. See [heterogeneous-property semantics](QUERY_LANGUAGE.md#heterogeneous-properties-across-tables).

These operators use the existing snapshot and transaction-private overlay; they
also preserve [absent-table read semantics](QUERY_LANGUAGE.md#absent-tables-in-read-patterns)
through aliases and returning subqueries. A missing table does not erase the
static entity kind or turn an OPTIONAL NULL into permission to rebind a node.
Absent positive-length patterns produce no rows while consuming prior stages;
zero-length absent-type branches retain eligible anchors and preceding paths.
They do not create another participant/transaction or expose another writer's pending
rows. Cursor/row budgets remain authoritative. Bound polymorphic writes use the
actual row's schema. Native pattern-expression and entity behavior is covered by
the [complete V3 required profile](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md),
with separate supplemental contracts. Native typed/polymorphic node and edge
results now use [qualified detached values](ENTITY_VALUES.md), also through
lists/maps, cursors, sorting and DISTINCT. This is a breaking development contract,
not a claim of full Cypher conformance or arbitrary detached write authority.

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

Up to 64 read/write branches can be combined. Returning branches must publish
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

This returns two rows containing 1. Updating branches are also native; their
effects and unit-result policy are described below.

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
disjoint; separate MATCH clauses may reuse relationships. Reusing an incoming
edge name retains that edge's full identity instead of assigning another edge.
Bidirectional arrows (`<-->`, `<-[r]->`) execute as undirected read segments;
neighboring directed segments keep their direction and captured edges keep their
physical orientation. Alternatives permit an optional colon after each `|`.
This applies to typed, untyped, aliased, optional and captured-path reads.
Conflicting type constraints produce an empty match (or optional NULL extension
for new variables), not a schema rebinding. A repeated edge name within one
connected pattern is rejected statically. Inline relationship maps compose as
equality predicates, including `all` relationships in annotated ranges; zero-hop
ranges satisfy their map vacuously. OPTIONAL applies these conditions before
null extension. See [map semantics and costs](QUERY_LANGUAGE.md#inline-relationship-property-maps).
Typed zero-length and
multiple captured segments concatenate without duplicating junction nodes.
Inverted explicit hop intervals are empty reads, not syntax errors or zero-hop
paths. They consume upstream work without traversal; OPTIONAL null-extends newly
introduced bindings and aggregates retain their normal zero-input behavior.
Both bounds remain independently limited to `0..30`, including local expressions.
Untyped/type-alternative bounded ranges also capture paths and compose with
these clauses. Written relationship lists retain their type through aliases,
imports/exports and proved list-or-NULL UNION results. See
[heterogeneous ranges](QUERY_LANGUAGE.md#heterogeneous-bounded-relationship-ranges).

A native logical relationship group resolves into physical candidates before
these traversal/composition rules apply. `:REL|REL` does not scan a member twice;
OPTIONAL and named ranges preserve qualified endpoints, and exported/UNION edge
values report the logical type without merging identities. CREATE/MERGE resolve
statically proved or runtime-bound endpoint pairs, followed by unchanged identity,
visibility and OCC validation. SET/DELETE retain the actual member's schema.
[Grammar and current limits](QUERY_LANGUAGE.md#relationship-types-spanning-endpoint-tables).
Omitted upper bounds preserve
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

`CALL { ... }` imports no outer names unless its body starts with an importing
WITH. The forms `CALL (name, ...) { ... }`, `CALL (*) { ... }` (all currently
visible names) and `CALL () { ... }` (none) evaluate once per incoming row. Only
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

### Native writing and unit subqueries

Subqueries may now perform native CREATE, MERGE, SET and DELETE within the
**same outer transaction and statement rollback boundary**. A body ending in an
update (or a nested unit writing call) may omit RETURN: it drains completely and
preserves one outer row, even when no inner pattern matches. It exports no names.
Returning calls keep the usual inner-result cardinality; zero returned rows remove
the outer row but do not discard the body's successful writes.

```cypher
UNWIND [1, 2, 3] AS i
CALL (i) { CREATE (n:Item {value:i}) }
RETURN i LIMIT 1
```

All three nodes are created; LIMIT shapes the returned rows, not the writes.
A standalone `CALL () { CREATE (n:Item {value:1}) }` produces no result columns.
Use `with db.begin("write") as tx: tx.execute(query)`; no new Python API or
configuration flag is required. Writing results are materialized before execute
returns. Read transactions and query cursors refuse nested writes before effects,
including when an UNWIND supplies zero rows.

Each invocation sees preceding invocations' private writes. Reads following the
call see its completed effects. Imported native entities, collections and paths
refresh owner-current properties at phase boundaries; already projected scalar
values retain their earlier values. Schema/relationship creation remains lazy,
and later failure discards the entire statement's schema and data, not merely the
last invocation. Failure during final public result conversion is included.
Earlier successful statements remain available after proven rollback. No inner
COMMIT, new snapshot, cross-store work or concurrent-participant visibility is
introduced; conflicting writers still undergo OCC.

Explicit import names cannot be duplicated, undeclared or silently inherited;
returned names cannot overwrite outer bindings. This increment does not introduce
read-only bodies without RETURN. Nesting is limited to 16 subqueries,
alongside shared statement-write, transaction, intermediate-row and query limits.
Native entity-container refresh rejects cycles/excessive nesting and memoizes
shared containers rather than expanding a shared graph exponentially.
Pending insertion identities retain exact version witnesses for the statement's
lifetime; short-lived unit-body rows cannot acquire another row's identity through
Python object-ID reuse. These witnesses are not persisted or shared across calls
to the public execute API.
The [supplemental profile and qualification](specs/WRITE_SUBQUERIES_V1.md) tracks
remaining cases without changing the pinned TCK.
This CALL syntax is documented separately from the pinned 2024.3 semantic target
and from tabular procedure calls below.

### Subquery import scopes

| Form | Names admitted | Lifetime inside the body |
| --- | --- | --- |
| `CALL (x, n) { ... }` | Only the listed existing names | Persistent across every WITH; cannot be reassigned or shadowed |
| `CALL (*) { ... }` | All names visible at that call site | Same persistent semantics, including inside each UNION branch |
| `CALL () { ... }` | None | A nested call does not inherit its parent's globals implicitly |
| `CALL { WITH x, n ... }` | Direct names in the first WITH of that branch | Ordinary WITH scope: later WITH may drop or rebind them |
| `CALL { WITH * ... }` | All names visible at that call site | Grafx's leading-WITH wildcard form; ordinary WITH scope thereafter |

```cypher
WITH 2 AS x
CALL (x) { WITH 3 AS y RETURN x + y AS total }
RETURN total
```

Returns 5: the first WITH inside the body does not drop explicit global `x`.
`WITH 3 AS x` there is refused; carrying the same variable as `WITH x` is allowed.
By contrast, `CALL { WITH x WITH x + 1 AS x RETURN x AS total }` returns 3.
Alias an imported name when exporting it: `RETURN x AS copy`, not `RETURN x`,
because the latter collides with outer `x`.

An importing first WITH accepts direct variable references only: no aliases,
expressions, DISTINCT, WHERE, ORDER BY, SKIP or LIMIT. Use a second WITH for those
operations. A first WITH using only local expressions (for example `WITH 7 AS y`)
does not implicitly import anything. Each bare-CALL UNION branch must import its
own names; importing `x` in the first branch does not grant it to another branch.

Persistent globals remain available after aggregation over zero input, DISTINCT,
sorting and disk spill. They are not silently added to grouping or DISTINCT keys.
They may parameterize SKIP/LIMIT as constants per invocation; ordinary row-local
variables, including nonpersistent leading-WITH imports, are not window constants.
The resulting window still requires a nonnegative integer and the usual bounds.

Import restoration uses the invocation's original native entity witnesses, not
maps or entity values deserialized from spill. Grouping/sorting cannot manufacture
write authority. Read cursors keep their original snapshot even while another
participant commits. Statement rollback, OCC, storage validation and budgets do
not change. Public plans may include `RestoreImports`; each result owns its one
stable detached plan, independently of other results. No new setting or Python
method is required. This is an intentional scope-semantics expansion, not an
optional legacy mode; see [0.0.6 compatibility](V006_COMPATIBILITY.md).

### Updating UNION branches

Each branch starts with its own lexical inputs. Branches execute in source order:
later branches see earlier private writes; earlier branches never see future
writes. The original transaction snapshot remains unchanged for other participants.
Use the same native API as other writes:

```python
with db.begin("write") as tx:
    result = tx.execute("""
        CREATE (n:Item {value:1}) RETURN n.value AS value
        UNION ALL
        MATCH (n:Item) RETURN n.value AS value
    """)
```

On an empty graph this creates one node and returns two rows containing 1.
`UNION` would return one row, but duplicate elimination never undoes either
branch's writes. All returning branches must have the same ordered column names.
Use aliases explicitly; mismatched names or mixing returning and unit branches
refuses before effects. The existing limit of 64 branches and one duplicate policy
per chain remains. Both policies may compose through separate CALL scopes.

All-unit writing branches are also supported:

```cypher
CALL () {
  CREATE (a:Item {value:1})
  UNION ALL
  CREATE (b:Item {value:2})
}
RETURN 7
```

Both nodes are created and the unit CALL preserves one outer row. A top-level
all-unit UNION exports no columns or rows. Unit branches must write (possibly
through another unit CALL). Read-only bodies without RETURN are not introduced.

RETURN windows shape a branch's result, not its preceding writes. A LIMIT after a
writing CALL cannot skip the required effects of later UNION branches, even when
LIMIT is zero. WITH windows still select the rows reaching subsequent updates.
Explicit and leading-WITH imports keep their documented per-branch scope.

Branch write phases are completed and privately staged before their outputs are
exposed. Native inserted entities therefore carry the same transaction identity
when reread in a later branch. DISTINCT, grouping and disk spill must not count
one node twice merely because its internal reference changed during staging.
Aliases, entity lists/maps, returned nodes and downstream SET preserve that
authority; no detached dictionary can impersonate a native entity.

Failure in a later branch, downstream expression, budget, process interruption or
final public result conversion rolls back the **whole statement**, including lazy
schema/relationship creation. Earlier successful statements survive only after
proven rollback. Read transactions and read cursors refuse the complete write
plan, including nested and zero-input writes. COMMIT/recovery, independent
snapshots and conflicting-writer OCC retain their existing guarantees.
Public cancellation/deadline keyword arguments remain read-transaction controls;
they are refused for write transactions before effects. Writing results are fully
materialized, never exposed through an abandonable write cursor.

No new configuration, persistence bit or dependency is added. Branch preparation
uses the existing eager write barriers and shared execution budgets. Spill does
not promise that arbitrary values fit any memory budget: an insufficient merge or
identity workspace explicitly refuses and rolls back. Public `UnionRows.writes`
identifies this execution mode; the dedicated executemany fast-path admission is
not broadened into batch execution of arbitrary UNION statements.
[Acceptance and receipts](specs/WRITE_SUBQUERIES_V1.md).

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
YIELD WHERE filters after binding. Named YIELD is required for a nonempty output
schema inside a query. A standalone call may omit YIELD to return all declared
columns, or use YIELD *. Unit procedures use no YIELD.
A standalone procedure without RETURN returns its selected columns. Calls compose
with UNWIND, MATCH, WITH, UNION and read subqueries, and work through execute,
explain and read cursors. Cursor close closes the callback iterator.

Arguments and cells use BOOL, INT64, DOUBLE, NUMBER, STRING, BYTES, TIMESTAMP or UUID;
the subsequent [native value contract](specs/PROCEDURE_NATIVE_VALUES_V1.md) adds
DATE/LOCALTIME/TIME/LOCALDATETIME/DATETIME/DURATION, LIST/MAP/ANY and VECTOR_F32/F64.
Containers are detached before callbacks and before output publication. LIST/MAP
signatures do not add parameterized stored collection types.
[Entity signatures](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md) additionally support
NODE/RELATIONSHIP/PATH and typed node/relationship lists, including entities nested
in generic containers. Only observations issued by the current native invocation
can become native bindings; public detached entities are not query parameters or
an attachment API. Explicit types preserve downstream entity analysis.
NUMBER preserves native int/float values; DOUBLE widens validated INT64 inputs and
outputs to binary64. Other types remain exact. [Numeric limits and precision tradeoffs](specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md).
NULL cells/arguments are allowed (callbacks must handle NULL themselves).
No graph handle, implicit database write capability, arbitrary module import,
persisted executable code, sandbox or transactional external side effects are
provided by default. These examples use **trusted pure tabular/unit callbacks**
(`mode="read"`). Explicit [writing procedures](specs/WRITING_PROCEDURES_V1.md)
instead receive a restricted, expiring mutation capability in the caller's
transaction, with named permissions and shared budgets.
The subsequent [query authority contract](specs/PROCEDURE_QUERY_AUTHORITY_V1.md)
adds writer.query() with native results and optional graph_read=True for read
callbacks. ProcedureReader/ProcedureResult, lifetime and cumulative query limits
are explicit; no extra transaction is opened and default pure callbacks are unchanged.
Native registered calls now compose recursively through that query authority;
writing unit calls also work through execute(). `max_call_depth` is inherited and
all levels retain root native budgets. Procedure `deterministic=False` replaces
the former implicit read-callback determinism assumption. [Nesting, effects and upgrade](specs/PROCEDURE_NESTING_EFFECTS_V1.md).
The host is responsible for their purity and termination. Permission sets are
host-configured allowlists, not user authentication or operating-system isolation.

| Registration option | Default | Meaning / limit |
|---|---:|---|
| `required_permissions` | empty frozenset | All names must be granted on this handle before planning/execution |
| `mode` | `"read"` | `"write"` requires nonempty permissions and receives ProcedureWriter first |
| `max_write_statements` | 128 | 1..1024; summed per procedure across one outer statement's writing invocations |
| `argument_names` | None | Optional unique tuple naming every positional input; enables standalone implicit parameter passing |
| `max_rows` | 10,000 | Per callback invocation; 1..2^31 |
| `max_result_bytes` | 8 MiB | Encoded result cells per invocation; 1..2^31 |
| `max_value_bytes` | 1 MiB | Per argument/result value; 1..2^31; strings conservatively charged at four bytes per code point |
| Registry size | 128 | Maximum procedures per immutable registry |
| Signature size | 32 / 64 | Maximum arguments / output columns; an empty output tuple declares a unit callback |

Wrong signatures/permissions fail before callback invocation. Wrong result arity,
type and callback exceptions are typed `GrafxPlanError`; resource violations are
`GrafxQueryBudgetExceeded`. Every returned cell is checked, including columns not
selected by YIELD. Cleanup failures are typed and do not overwrite an earlier
query failure. Global intermediate/result-row and transaction quotas still apply
across invocations. Cooperative query cancellation cannot preempt blocking host
code inside an arbitrary callback. [Standalone invocation, implicit arguments,
wildcard outputs and diagnostics](specs/PROCEDURE_INVOCATION_V1.md) document the
signature-aware expansion contract; in-query calls still name their outputs.

The existing standalone `grafx.search_text` procedure retains its documented
specialized contract; registering tabular callbacks does not broaden that syntax.

### Unit procedures

```python
def validate_positive(value):
    if value is None or value <= 0:
        raise ValueError("A positive value is required")
    # Successful unit callbacks return exactly None.

unit = TabularProcedure("app.validate_positive", ("INT64",), (), validate_positive)
registry = ExtensionRegistry(trusted=True, procedures=(unit,))
with connect(":memory:", extensions=registry) as db:
    assert db.execute("CALL app.validate_positive(1)").rows == ()
    assert db.execute(
        "UNWIND [1, 2] AS x CALL app.validate_positive(x) RETURN x"
    ).rows == ((1,), (2,))
```

An empty output schema does not mean an iterable of empty rows: the callback
must return exactly None. Standalone CALL has no columns/results; within a
pipeline it preserves each input row and may terminate the pipeline without
RETURN. Empty input invokes it zero times. No database handle is passed. A late
callback failure rolls back the enclosing graph statement, not earlier successful
statements. Host effects are not transactional and callbacks are not implicitly
retried by the procedure operator. Cursor close may skip later inputs.
Non-returning CALL uses `execute()`; a returning pipeline also supports cursors.

`max_value_bytes` still bounds arguments; the descriptor's validated `max_rows`
and `max_result_bytes` do not bound incoming unit invocations, because no callback
result stream exists. Existing query limits remain authoritative. No new runtime
configuration field or storage format is introduced. Unit plans report
`access="pure_unit"`; explain does not invoke the callback. A standalone unit call
may omit parentheses when its parameter names are declared (or it has no arguments).
[Detailed unit contract and qualification](specs/UNIT_PROCEDURES_V1.md).

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
and explicit graph-writing procedures now have separate native contracts:
[subqueries](#native-writing-and-unit-subqueries) and
[scoped callback mutations](specs/WRITING_PROCEDURES_V1.md). Neither grants a raw
database handle or an external-side-effect rollback guarantee.
