# Query language reference

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
| `CREATE` (nodes and relationships) | patterns with inline properties |
| `MATCH` … `WHERE` … `RETURN` | equality, comparison, `STARTS WITH`, `ENDS WITH`, boolean operators |
| `MERGE` | matches on the properties the pattern NAMED |
| `SET` | on node properties and matched relationship properties; relationship endpoints are immutable |
| `DELETE` | nodes, and relationships a `MATCH` bound |
| `DETACH DELETE` | ends every relationship incident on the node together with it |
| `UNWIND $rows AS r` | repeated list expansion in ordered pipelines; NULL/empty carriers emit no rows |
| `WITH` … `WHERE` | scalar/aggregate projection stages; `WITH *` carries the incoming named scope and can append explicit items; its WHERE runs after projection. Optional-hop aggregation has the closed forms below |
| `MATCH (n)` | reads every node table as one set, including in composed read pipelines, inline maps and OPTIONAL MATCH; compatible-property checks, filters, aggregation and windows apply across the union; undeclared properties read as NULL |
| Traversal | one hop, bounded ranges including `[:REL*0..3]`, both directions, relationship isomorphism |
| `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT` | |
| Aggregates | Exactly `count`, `sum`, `avg`, `min`, `max`, `collect`; scalar/aggregate type restrictions apply |
| Scalar functions | text, numeric, conversion and list families below, plus graph/vector extensions |
| Conditional/list expressions | searched/simple `CASE`; zero-based/negative indexing and half-open slices; list comprehensions, all/any/none/single, reduce, concatenation; case-sensitive map access |
| Parameters | `$name`, refused before anything runs if one is missing |
| `OPTIONAL MATCH` | Correlated typed patterns and whole-clause null extension; optimized incident-hop execution retained |
| `UNION`, `UNION ALL` | Up to 64 read-only RETURN branches; same ordered column names, heterogeneous values and qualified node/edge entities, no implicit numeric conversion; one duplicate policy per chain |
| CALL subqueries | Independent or explicitly imported returning read subqueries; maximum nesting 16 |
| Typed CALL/YIELD | Trusted per-handle tabular registry, explicit permissions/budgets, composed execution |
| Path projection | Native `PathValue` for typed bounded ranges, zero length and multiple segments in either direction; composed MATCH/OPTIONAL MATCH, aliases, subqueries, `length`, `nodes`, `relationships`. Untyped/type-alternative capture remains pending |

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

## Values and Python mapping

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
an exception. Tables declaring incompatible types for a referenced property still
refuse before rows. Reusing a bound node, including through a WITH alias or explicit
subquery import, tests the existing binding instead of scanning again. A NULL
introduced by OPTIONAL MATCH never becomes an unbound scan; a label on a rematch
filters the bound node's table. See [composition and remaining bounds](COMPOSABLE_QUERIES.md#polymorphic-node-read-composition).

Integer query literals accept decimal, hexadecimal (`0x`/`0X`) and octal (`0o`)
spellings, for example `RETURN 0x2A AS hex, 0o52 AS octal`. Based literals permit
single underscores before digits (`0x_FF`, `0o7_7`), not trailing or repeated
underscores. Values remain exact signed INT64, including `-0x8000000000000000`;
malformed digits and overflow refuse before statement effects. This introduces
no new configuration, storage type or Python parameter contract.

Numeric tokens have a fixed 2,048-character ceiling, excluding an external unary
sign. This supports long finite DOUBLE decimal spellings, including exact binary64
subnormal expansions. DOUBLE literals still need a decimal point or exponent;
an oversized integer is not implicitly converted to DOUBLE. Decimal integers are
checked against INT64 magnitude before host integer conversion, independently of
Python's process-global digit limit. Overflow/non-finite DOUBLE literals refuse
before statement effects. Query text and token budgets still apply.

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
| `VECTOR(space)` | `VectorValue(values, space_ref, dtype)` on storage/result boundary; numeric lists/tuples can bind to declared vector columns; space/dimension/dtype/finite-component checks apply |
| Null | Python `None`; not an absent result row |
| Query lists/maps | Input list/tuple and string-keyed maps; output lists detach to tuples. These are query values, not arbitrary LIST/MAP column DDL |

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
incompatible declared property families refuse before streaming. Parameter-map
keys are case-sensitive for dot and string-key bracket access; missing keys return
null. `a` and `A` are distinct keys. See the breaking-language migration in
[the compatibility contract](CYPHER_COMPATIBILITY.md).

## Expressions and functions

Supported expression operators include arithmetic `+ - * / %`, comparison
`= <> != < <= > >=`, boolean `AND OR NOT`, null predicates, `IN`, string predicates
`STARTS WITH`/`ENDS WITH`, list extraction and CASE. They remain typed operations,
not Python coercion of arbitrary objects. Unsupported operators/functions refuse.

| Function / expression | Semantics important to an integrator |
| --- | --- |
| `count(*)`, `count(expr)`, aggregate `DISTINCT` | Counts rows / non-null values respectively; duplicate removal follows the typed value rules |
| `sum(expr)`, `avg(expr)` | Ignore NULL inputs and refuse other nonnumeric values; integer-only SUM preserves integer precision; empty/all-NULL SUM is 0, AVG is NULL |
| `min(expr)`, `max(expr)`, `collect(expr)` | Typed aggregation; collect creates a real public tuple, not a lazy/disk proxy; memory/row budgets can refuse |
| `coalesce(value, ...)` | At least one positional argument; evaluate only until first non-null; heterogeneous/list/map values allowed; chosen value retains its type |
| `split(text, separator)`, `string_split(...)` | Two strings, NULL propagates; all empty fields retained; empty separator splits code points; two empty inputs yield one empty string |
| `size(value)` | One string/list; code-point/element count; null propagates |
| `label(binding)` | One matched node/relationship; physical table name; null propagates; not a logical Pulse relationship type mapper |
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
An undirected self-loop has two directional matches (`count(r)=2`,
`count(DISTINCT r)=1`). Generic typed optional application is separate from that
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

`abs` accepts INT64/DOUBLE, not BOOL or numeric strings. It preserves numeric type
and refuses signed-INT64-min overflow and non-finite results rather than widening
or wrapping. Known schema/literal and bound parameter type errors are rejected
before rows, including empty matches; row-dependent numeric overflow is checked
when evaluated. Composed expressions remain subject to the existing query subset
and budgets. These functions are built in, not trusted host UDFs.

Additional native families in this development round:

| Family | Functions / contract |
|---|---|
| String | `ltrim`, `rtrim`, `toLower`, `toUpper`, `substring(text, start [, length])`, `left(text, count)`, `right(text, count)`, `replace(text, search, replacement)` |
| Numeric | `ceil`/`ceiling`, `floor`, `sqrt`, `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2(y, x)`, `degrees`, `radians`, `sign`, unary `round`; `pi()` and `e()` |
| Conversion | `toInteger`, `toFloat`, `toBoolean`, `toString`; explicit scalar conversion, not implicit result-column coercion |
| List/map | `head`, `last`, `tail`, `reverse` (also STRING), `keys(map)`, inclusive `range(start, end [, step])` |

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
applied at the boundary. Math domain errors/non-finite results refuse; Grafx does
not promise the reference engine's NaN/infinity behavior. ROUND currently accepts
one argument only. Invalid textual numeric conversions return NULL; boolean/string
conversion uses explicit supported scalar families, not arbitrary Python objects.
`toInteger` parses ASCII decimal/exponent strings exactly and truncates toward zero;
the original value must fit INT64. It does not round through DOUBLE, depend on a
host decimal context or allocate integers proportional to a written exponent.
These fixed bounds have no new connection option.

Typed traversal supports bounded directions/ranges and preserves relationship
isomorphism and parallel-edge multiplicity. Omitted upper bound means **20 hops**,
not infinity; explicit upper bound can reach **30**; invalid/zero/reversed ranges
refuse. Label-free typed endpoints and untyped relationships have closed forms,
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
relationship variables are supported. Typed bounded ranges and multiple segments
are also captured natively; fully untyped/ambiguous relationship alternatives and
relationship inline maps remain pending.
UNION and cursors preserve the captured
path, including pending identity and spill metadata. See [native paths](ENTITY_VALUES.md#paths)
for the breaking result/JSON contract. This is not a claim of general path algebra.

```cypher
MATCH p=(a:Person {id:$id})-[r:Knows*0..3]->(b:Person)
RETURN p, length(p), nodes(p), relationships(p), r
ORDER BY length(p)
```

An explicitly bounded typed segment accepts `0 <= minimum <= maximum <= 30`.
`*0` returns its anchor as a path with one node and no relationships, including
isolated nodes; the two endpoint variables name that same qualified node. A bound
target still filters that identity. Zero-length matches need not belong to the
relationship table's endpoints, since they traverse no edge. NULL anchors produce
no path, and OPTIONAL may then null-extend the clause.

Every written range binds its relationship variable to a tuple of relationships,
including `*1..1` (one element) and `*0` (empty). An unstarred single hop binds one
relationship entity. Adjacent captured segments concatenate at their common node
without duplicating that junction. Their relations remain unique across the walk.
Depth-first execution streams paths without retaining a breadth-wide frontier;
it is not a shortest-path ordering guarantee. Use ORDER BY where ordering matters.
Cursor close/limits stop and close unconsumed expansion iterators.

The legacy omitted-upper-bound policy (`*`, `*n..` → an upper bound of 20) has not
yet been replaced in this increment. It remains an explicitly assigned FP-3 gap:
full-profile completion requires budget-governed refusal rather than treating a
truncated enumeration as complete. Prefer explicit bounds for the current contract.

Within one MATCH clause, relationship occurrences across its patterns/segments
must be disjoint, including anonymous relationships and variable-hop segments.
Qualified relationship identity keeps equal local IDs in different tables distinct.
Separate MATCH clauses may observe the same relationship again. Repeated nodes
alone do not violate trail uniqueness. The clause check runs before OPTIONAL null
extension; it uses normal bounded expression evaluation, not a new transaction or
an unbounded global graph scan.

## Schema, indexes and query costs

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
