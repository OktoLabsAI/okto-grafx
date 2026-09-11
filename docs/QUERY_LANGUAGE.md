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
| `WITH` … `WHERE` | scalar/aggregate projection stages; each replaces scope with projected names; its WHERE runs after projection. Optional-hop aggregation has the closed forms below |
| `MATCH (n)` | a node with no label reads every node table as one set; filters, `label(n)`, aggregates, `ORDER BY` and windows apply to the union, and an undeclared property reads as null |
| Traversal | one hop, bounded ranges `[:REL*1..3]`, both directions, relationship isomorphism |
| `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT` | |
| Aggregates | Exactly `count`, `sum`, `avg`, `min`, `max`, `collect`; scalar/aggregate type restrictions apply |
| Scalar functions | text, numeric, conversion and list families below, plus graph/vector extensions |
| Conditional/list expressions | searched/simple `CASE`; zero-based/negative indexing and half-open slices; list comprehensions, all/any/none/single, reduce, concatenation; case-sensitive map access |
| Parameters | `$name`, refused before anything runs if one is missing |
| `OPTIONAL MATCH` | Correlated typed patterns and whole-clause null extension; optimized incident-hop execution retained |
| `UNION`, `UNION ALL` | Up to 64 read-only RETURN branches; same ordered column names, heterogeneous values, no implicit numeric conversion |
| CALL subqueries | Independent or explicitly imported returning read subqueries; maximum nesting 16 |
| Typed CALL/YIELD | Trusted per-handle tabular registry, explicit permissions/budgets, composed execution |
| Path projection | Outgoing typed one-hop path map, aliases, `length`, `nodes`, `relationships`; not general variable-length named paths |

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
names. Branch-local DISTINCT/windows remain local. Multi-branch chains associate
left-to-right; RETURN subqueries provide an explicit outer projection/sort.
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
arguments and zero range step refuse. Generated ranges have a hard cap of 100,000
elements, checked before allocation. Replacement output is bounded by the 1 MiB
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

The narrow outgoing typed one-hop `MATCH p=(a:A)-[r:R]->(b:B) RETURN p`
returns a map with `_NODES` and `_RELS`. Node maps carry `_ID`, `_LABEL` and
properties; relation maps carry `_SRC`, `_DST`, `_LABEL`, `_ID` and properties.
IDs are opaque backend-local numbers; lists are native tuples. Structural-key
collisions refuse before streaming. The same one-hop capture supports aliases,
multiple RETURN expressions and `length(p)`, `nodes(p)`, `relationships(p)`;
NULL input returns NULL. Nodes/relationships return detached component maps,
which may be consumed by list expressions. Arbitrary maps are not accepted as
paths. `size(p)` and `p.property` refuse. Other directions, variable-length named
paths, WHERE/WITH, DISTINCT, ordering and SKIP remain outside this capture contract;
a literal nonnegative LIMIT is supported. This is not a claim of general path algebra.

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
