# Query language reference

The closed read-only `CALL grafx.search_text(index, query, k [, record_ids])`
procedure is supported through materialized `execute`; see [FTS procedure syntax](FULL_TEXT_SEARCH.md#procedure-query).
This does not enable arbitrary CALL/YIELD, suffix composition, cursor/explain support
or textual full-text DDL. Use `Database.create_text_index` to create FTS indexes.

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
| `UNWIND $rows AS r` | one leading list source, followed directly by `RETURN` or by one single-node `MATCH` and `SET` |
| `WITH` … `WHERE` | scalar/aggregate projection stages; each replaces scope with projected names; its WHERE runs after projection. Optional-hop aggregation has the closed forms below |
| `MATCH (n)` | a node with no label reads every node table as one set; filters, `label(n)`, aggregates, `ORDER BY` and windows apply to the union, and an undeclared property reads as null |
| Traversal | one hop, bounded ranges `[:REL*1..3]`, both directions, relationship isomorphism |
| `ORDER BY`, `SKIP`, `LIMIT`, `DISTINCT` | |
| Aggregates | Exactly `count`, `sum`, `avg`, `min`, `max`, `collect`; scalar/aggregate type restrictions apply |
| Scalar functions | `coalesce`, `string_split`, `size`, `label`, `timestamp` |
| Conditional/list expressions | searched and simple `CASE`; one-based and negative `list[index]` |
| Parameters | `$name`, refused before anything runs if one is missing |
| `OPTIONAL MATCH` | Root labelled node or correlated incident-hop pipeline, as detailed below; not arbitrary optional joins |
| `UNION` | Exactly two top-level read-only RETURN branches, equal arity, global DISTINCT; no UNION ALL/chains/nesting |
| Path projection | Narrow outgoing typed one-hop path map; no arbitrary path functions/projection shapes |

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
identity has no index encoding. Two same-statement shapes stay refused rather than guessed: an edge
whose endpoint that very statement is creating, and a `DETACH DELETE` of a node an edge held by
that same statement points at. Vector search over a dirty table is fail-closed. Updates of
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

`WHERE` retains true predicates; null is not true. `IS NULL`/`IS NOT NULL` test
null explicitly. Missing properties on polymorphic label-free reads are null;
incompatible declared property families refuse before streaming. Parameter-map
keys are case-insensitive for dot access, and colliding keys refuse at binding.

## Expressions and functions

Supported expression operators include arithmetic `+ - * / %`, comparison
`= <> != < <= > >=`, boolean `AND OR NOT`, null predicates, `IN`, string predicates
`STARTS WITH`/`ENDS WITH`, list extraction and CASE. They remain typed operations,
not Python coercion of arbitrary objects. Unsupported operators/functions refuse.

| Function / expression | Semantics important to an integrator |
| --- | --- |
| `count(*)`, `count(expr)`, aggregate `DISTINCT` | Counts rows / non-null values respectively; duplicate removal follows the typed value rules |
| `sum(expr)`, `avg(expr)` | Numeric aggregates; null inputs do not become zeros merely because the caller expects a number |
| `min(expr)`, `max(expr)`, `collect(expr)` | Typed aggregation; collect creates a real public tuple, not a lazy/disk proxy; memory/row budgets can refuse |
| `coalesce(value, ...)` | At least one positional argument; all evaluated left-to-right before selecting first non-null. Scalar families must agree, numeric INT64/DOUBLE promotes; no general map/list coalesce |
| `string_split(text, separator)` | Two strings, null propagates; repeated separators compress internal empty fields, final empty field retained; empty separator splits code points; both inputs empty refuses |
| `size(value)` | One string/list; code-point/element count; null propagates |
| `label(binding)` | One matched node/relationship; physical table name; null propagates; not a logical Pulse relationship type mapper |
| `timestamp(value)` | Timestamp passthrough, null, or ISO-8601 string normalized to UTC; no zone means UTC; numeric epoch arguments refuse |
| `similarity(n.embedding, $q, space => 'space')` | Planned vector-search extension, not an arbitrary scalar UDF; literal/bound space, declared vector column and supported query shape required |
| `similarity_score()` | Zero arguments; only where a similarity operator has supplied a score |
| `CASE WHEN ... THEN ... ELSE ... END` / simple CASE | Eager written-expression evaluation, first match, absent ELSE null; scalar-family checks and numeric promotion; do not depend on short-circuiting to hide an invalid expression |
| `list[index]` | One-based positive index, negative from end; null propagates; zero/out-of-range/noninteger refuses. Not map-key bracket access |

Use `db.explain(text)` and actual execution on representative values to validate a
new query shape. There is no blanket `PROFILE`/arbitrary procedure or Neo4j function
catalogue. Statistics show selected operators/work, not a comprehensive SQL profiler.

## OPTIONAL, UNION and traversal boundaries

`OPTIONAL MATCH (n:Person) RETURN n.id` admits the narrow root labelled-node
form: no qualifying candidates gives a null-extended row. For correlated optional
relations, begin with a mandatory labelled node and expand incident hops:

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
`count(DISTINCT r)=1`). Optional ranges/maps/named paths, extra mandatory MATCH,
UNWIND, writes or vector predicates inside that optional pipeline remain restricted.

`RETURN 1 AS n UNION RETURN 1.0 AS n` returns one numerically promoted value.
Both branches share a snapshot and budgets, names come from the left, and each
branch's ORDER BY/SKIP/LIMIT is local. No post-UNION sort/window, chaining, nested
UNION, UNION ALL or write branches. Incompatible/unprovable result families refuse.

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
collisions refuse before streaming. Wider path aliases/functions/directions,
filters/projections/windows and UNION path outputs remain outside this contract.

## Schema, indexes and query costs

Create tables and spaces inside write transactions. A node PK is automatically
indexed. Relationship endpoints are immutable; use a new relationship to change
them. `DELETE` ends the node without physically ending its incident relationships;
traversal cannot land on the invisible node. `DETACH DELETE` also ends incident
relations atomically and retains broader corruption checks, so it may scan.

Custom index DDL/Python maintenance is detailed in [indexes](INDEXES_AND_VECTORS.md).
General ALTER/DROP/migration/view APIs are roadmap work, not implied by transactional
CREATE. Use [query budgets](CONFIGURATION.md) for untrusted cardinality. Streaming
the public result does not turn every sort/group/traversal into constant memory.

The engine [normative query contract](architecture/CONTRACT.md#89-enginequery_enginepy-c10)
and [Pulse compatibility corpus](specs/PULSE-QUERY-CORPUS-1.0.md) retain exact edge
cases and test provenance; they do not make every Pulse wrapper transformation a
native Grafx feature.
