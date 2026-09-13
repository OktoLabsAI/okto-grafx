# Detached entity result model — 0.0.6 development contract

Native [procedure entity signatures](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md) now
export these observations to trusted callbacks and restore only references issued
by that invocation. This is not promotion of arbitrary DTOs or a public attachment
API; copied, foreign and previous-call objects remain unauthorized.

`startNode(r)` / `endNode(r)` produce native node values inside a query and
detached `NodeValue` results at the public boundary. Endpoint direction is physical,
not reversed by an undirected walk. Aliases and sorted WITH stages retain native
write authority through transaction-qualified reacquisition, not DTO promotion.
[Contract](specs/WRITE_PATTERN_CONTRACTS_V1.md#undirected-merge-and-native-endpoint-functions).

Native `keys(entity)` and `entity[string_key]` now read actual property names and
owner-current values, including collected entities and path components. This does
not admit detached DTOs as parameters or writable handles. NULL/missing keys,
typed constraints and property REMOVE are documented in
[the query contract](QUERY_LANGUAGE.md#dynamic-entity-properties-and-property-removal).

Within one native query, entity collections now retain node/relationship authority
through UNWIND, including collect, explicit lists, compatible CASE/coalesce,
slices and identity-preserving/filtering comprehensions. Physical authority lost
to spill is reacquired against the transaction snapshot, not reconstructed from
detached properties. Rematch/SET/DELETE use the actual entity identity, including
polymorphic collections. This does not make detached result DTOs writable or
permit external maps to impersonate entities.
[Examples and transaction/spill contract](QUERY_LANGUAGE.md#native-entity-collections-and-unwind).

An empty collection proves no element kind, but remains compatible with a
zero-edge relationship-range pattern and neutral alongside proven node/edge
lists in CASE/coalesce or concatenation. This proof survives aliases/subquery
imports without assigning a fictitious entity kind to UNWIND elements. All-NULL
collections likewise do not invent a kind; their NULL elements propagate through
entity/path scalar functions instead of being treated as relationships.
Literal list selections with a provably invalid scalar type fail
during planning; dynamic selections retain execution-time validation.

Native unlabeled nodes expose `NodeValue.label: str | None` as `None` and
`NodeValue.labels == ()`. `to_dict()` uses JSON `label: null`; its qualified
identity is unchanged. Properties expose the user map, never the physical bag
column or generated table name as a label. In the native-label development
checkpoint, typed or flexible nodes can also carry a complete versioned label set.
`NodeValue.labels` is authoritative; `label` is its first canonical (lexically
sorted) name, or `None` for the empty set. It is not physical table identity.
`to_dict()` adds the JSON `labels` array to `grafx.node.v1`, preserving the singular
convenience field. Consumers must use `labels` for membership and
`identity.table_id` for physical identity. The optional constructor field
`node_labels` accepts only a canonical tuple and must agree with `label`; omission
retains singleton/empty construction. This DTO does not activate a storage format.
The [native-label contract](specs/NODE_LABELS_V1.md) includes CREATE/MERGE,
SET/REMOVE, retained history, copy and transfer/resume. Its
[installed format matrix](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md) qualifies
old-reader refusal and durable admission for the recorded candidate.
See [native creation and durable semantics](architecture/FLEXIBLE_GRAPH_V1.md).

[API reference](API_REFERENCE.md) · [FP-3 evidence](conformance/FP3_PROGRESS.md) ·
[Roadmap](../ROADMAP.md#functional-parity-expansion-plan)

This page documents the **0.0.6 development API**, not a published release or
full Cypher conformance. Native `execute` and cursors return `NodeValue`
for typed and polymorphic nodes and `RelationshipValue` for relationships, also
inside lists/maps. Both types, `EntityIdentity` and `EntityProvenance`, are exported
from `okto_grafx`. UNION/UNION ALL now preserve these entities, including nested
values and aggregate spill. `PathValue` is also exported and returned by the
native bounded capture, including UNION and cursors. Named paths support typed,
untyped and alternative-type ranges, zero hops and composed clauses under shared
resource limits. [Current profile evidence](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md)
and [paired Pulse operational qualification](reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md)
remain distinct from a full-product parity or release claim.

This is an intentional breaking result change, with no legacy-result mode. Replace
assumptions that an entity is a table-local integer or a mutable `label/properties`
map with the explicit attributes below. Scalar projections such as `RETURN n.id`
are unchanged. The installed production Pulse has not been upgraded by this work.

Inside native queries, `n:Person` tests the node's owner-visible label set and `r:KNOWS`
tests the relationship's logical type. Both return BOOL/NULL, including native
entities carried through list/path expressions and aliases.
It neither modifies labels nor grants write authority to detached results.
See [node-label predicates](QUERY_LANGUAGE.md#node-label-predicates).

```python
from okto_grafx import NodeValue, RelationshipValue

node = db.execute("MATCH (n:Person) RETURN n LIMIT 1").rows[0][0]
assert isinstance(node, NodeValue)
user_key = node.properties["id"]
qualified_id = node.identity
json_observation = node.to_dict()
```

Root imports and fields:

| Type | Fields |
|---|---|
| `EntityIdentity` | `database_uuid: bytes`, `table_id: int`, `kind: str`, `record_id: int \| None`, `provisional_id: bytes \| None`; `committed: bool` property |
| `EntityProvenance` | `read_lsn: int`, `schema_version: int`, `version_lsn: int \| None`, `pending: bool` |
| `NodeValue` | `identity: EntityIdentity`, `label: str | None` (None for unlabeled), `properties: Mapping[str, object]`, `provenance: EntityProvenance`; `labels: tuple[str, ...]` property |
| `RelationshipValue` | Same observation fields, plus `source: EntityIdentity` and `target: EntityIdentity` |
| `PathValue` | `nodes: tuple[NodeValue, ...]`, `relationships: tuple[RelationshipValue, ...]`; `len(path)` returns the relationship count |

Each type offers `to_dict() -> dict[str, object]`. These are detached data values,
not alternate mutation or parameter APIs. Query parameters still refuse entities.
`QueryValue`, also exported from `okto_grafx`, annotates result scalars, nodes,
relationships, paths and recursive result lists/maps. It is distinct from the stored/
parameter `Value` grammar. QueryResult rows/dictionaries and cursor fetch methods
use this result alias; it does not widen parameter or column-type admission.

## Identity and observations

An entity identity includes the logical database UUID, table ID, kind (`node` or
`relationship`) and record incarnation. RecordIds are allocated monotonically:
updating properties retains identity, while deleting and recreating the same user
primary key does not. Current table IDs are append-only; table-ID reuse would need
an explicit additional incarnation policy. A copied filesystem path alone is not
a new logical database identity.

Snapshot provenance is separate: `read_lsn`, observed `schema_version`, committed
source `version_lsn` when known, and `pending` for transaction-private values.
A source version cannot be newer than the read snapshot. A pending insertion has
no invented future commit LSN. A provisional entity uses a separate 16-byte opaque
nonce instead of a durable RecordId. Assembly supplies a distinct opaque namespace
per engine handle. A keyed digest of the authenticated transaction-local identity
keeps the nonce stable across statements without exposing raw pending tokens.
Separate handles do not collide just because their process-local counters agree.
An insert cancelled before publication gets a separate statement-local identity.
An observed provisional identity does not silently become a durable identity after
commit: a new read returns the committed identity instead.

`NodeValue` and `RelationshipValue` equality and hashing use only qualified entity
identity, not label, properties or provenance. Observations before and after a
property update can therefore compare equal while exposing different values.
An identity is not proof of current existence. Returned values must never be used
as write authority; the current parameter boundary already refuses these DTOs.

## Owned properties and endpoints

Native relationship groups (`CREATE REL TABLE GROUP`) may give several physical
endpoint-table pairs the same logical type. `RelationshipValue.label` and
`type(r)` carry that logical name. The identity retains the member's physical
`table_id` and record incarnation; equal local record IDs in two members remain
different entities through paths, DISTINCT and UNION. Endpoints retain their
qualified node-table identities. Physical member names are introspection/scan
addresses, not alternative type labels. See the
[group contract](QUERY_LANGUAGE.md#relationship-types-spanning-endpoint-tables)
for current admission and consumer limits.


Nodes carry a first-canonical-name `label`, complete `labels` tuple, identity, properties
and provenance. Relationships additionally carry qualified `source` and `target`
node identities. Endpoints must belong to the relationship's logical database.
A non-pending relationship cannot claim a provisional endpoint.

Property materialization copies lists into tuples, dictionaries into owned
read-only mappings and mutable byte arrays into bytes. Timestamp, UUID and vector
values are copied into their domain representations. Caller mutations and edits
to an exported JSON dictionary cannot change a held observation. Metadata slots
are frozen. This is data ownership, not a Python sandbox or protection against
deliberate use of Python's object-internals escape hatches.

Properties admit the current closed stored-value families; they do not accept live
bindings, transactions, callbacks or entities as stored properties. The current
finite-number policy is unchanged. Existing depth, list/map cardinality and hard
query-value size ceilings bound materialization; final result integration must
also enforce the caller's configured query/result budgets. No new configuration
option is introduced by this internal model.

## Paths

`properties()` consumes node/relationship bindings or maps, `labels()` consumes
nodes, and `type()` consumes relationships. They also accept NULL and may be used
on native path components. The sparse map from `properties(entity)` omits NULL
columns, whereas the public entity DTO retains its declared property observation;
neither API exposes a writable handle. See the
[function contract](QUERY_LANGUAGE.md#native-entity-scalar-consumption).

SET refreshes live path components as well as direct aliases before subsequent
expression evaluation; this does not mutate previously detached public DTOs or
another reader's snapshot.

`MATCH p=(n:Person) RETURN p` captures an existing node as a path with one node
and no relationships. Unlabelled or anonymous node-only patterns use normal node
enumeration; equal local IDs in different tables remain distinct paths. Native
components retain snapshot/pending provenance through aliases, UNION/DISTINCT,
nested aggregate results and cursor detachment. A missing/NULL anchor is not a
fabricated zero-node path. See [query examples](QUERY_LANGUAGE.md).

Omitted-upper typed captures share the query's fixed 30-hop resource ceiling:
a valid unused continuation beyond it raises an explicit budget error, not a
successful truncated result. The `PathValue` DTO itself does not certify complete
enumeration; previously consumed cursor rows can precede a later resource error.
Explicit bounds, snapshot identity and component ordering remain unchanged. See
[query semantics](QUERY_LANGUAGE.md) for LIMIT/close behavior.

Native typed path capture returns `PathValue`, replacing
the former `_NODES`/`_RELS` maps. There is no legacy map-result switch.
`nodes(p)`/`relationships(p)` return native entity tuples with the same qualified
identity and observed properties as direct node/edge returns. For example:

```python
from okto_grafx import PathValue

path = db.execute("MATCH p=(a:Person)-[r:Knows]->(b:Person) RETURN p").rows[0][0]
assert isinstance(path, PathValue)
assert path.relationships[0].source == path.nodes[0].identity
serialized = path.to_dict()
```

User properties named `_ID`, `_LABEL`, `_SRC` or `_DST` are ordinary properties,
separate from identity/endpoint attributes; the old structural-key ban is removed.
Pending path components use the same authenticated opaque identities as returned
nodes/edges, not execution-local negative numbers. Commit produces new durable
identities without mutating already-returned paths.

A path owns ordered node and relationship tuples. It has one more node than
relationships; a zero-hop path owns one node. Each relationship must connect its
adjacent nodes in either direction. Ordering distinguishes forward and reverse
walks. All observations in one path must share the database and read snapshot.

Path equality/hash use the ordered entity identities, not property observations.
Repeated nodes and cycles are representable. The DTO represents a walk; the query
executor must separately enforce the pattern's relationship-uniqueness/trail rules
and traversal/cancellation budgets. Constructing this DTO does **not** implement
general variable-length named traversal. Native capture currently supports typed
bounded ranges (including zero), concatenated segments, either direction,
WHERE/WITH/UNWIND, row windows,
DISTINCT/order, typed OPTIONAL MATCH and returning subqueries. An unmatched
optional capture is NULL, not a fabricated empty path. Path aliases and path-or-NULL
UNION subquery exports retain their type for `length`/`nodes`/`relationships`.
Single-hop untyped/type-alternative segments support polymorphic endpoints and
the same native capture/identity contract. Bounded variable ranges over multiple
relationship tables now retain qualified identities and physical endpoint
orientation at every hop; zero-hop anchors may span all node tables. Written
range lists retain their type through imports/exports and list-or-NULL UNION.
Inline relationship maps constrain each annotated edge/range before emission,
without replacing entity identity or changing the captured property values. See
[range composition](QUERY_LANGUAGE.md#heterogeneous-bounded-relationship-ranges).
Zero length returns the anchor, not NULL: its path has one node and no relations.
Concatenation does not duplicate junction nodes. Range variables hold relationship
tuples even for a written `*1..1`; an unstarred hop returns one relationship.
Relationships cannot be reused across segments/patterns within one MATCH clause;
separate MATCH clauses may reuse them. This is distinct from node repetition.
Entities selected by a proved WITH coalesce/CASE keep their original qualified
identities and snapshot bindings. A list assembled from relationship entities
can constrain a subsequent ranged MATCH by ordered identity; no list or detached
value is thereby converted into independent write authority.
Bidirectional read spelling (`<-->`) uses undirected traversal and preserves each
edge's physical source/target; it neither creates reverse identities nor duplicates
a self-loop. Directed neighboring segments still constrain their own orientation.
Such reuse preserves the incoming edge identity and its existing binding, even
when later candidates share endpoints/properties or table-local record numbers.
No detached result becomes a writable handle as a consequence. Bound-edge reads
currently filter native scan/traversal candidates; they do not promise direct
endpoint lookup or sublinear enumeration for all patterns.
UNION/ALL and result cursors preserve path identity and snapshot. Temporary path
spill uses the bounded row codec and refuses malformed cardinality, nonentity
components, invalid endpoint connections and future source versions. Private
execution bindings are materialized before the public result boundary; they
cannot themselves be supplied as public path values or parameters.

## JSON grammar

`to_dict()` allocates independent JSON-safe dictionaries/lists. Top-level format
tags are `grafx.node.v1`, `grafx.relationship.v1`, `grafx.path.v1`; identities use
`grafx.entity-id.v1`. Wide RecordIds, table IDs and LSNs use decimal strings so
JavaScript does not round them. Database/provisional UUID bytes use hexadecimal.

Entity property dictionaries have string keys. Their values use this closed
encoding; nested maps are tagged so user properties cannot impersonate a type tag:

| Native property value | JSON property value |
|---|---|
| NULL, BOOL, STRING, finite DOUBLE | JSON null, boolean, string, number |
| INT64 | `{"type":"int64","value":"9223372036854775807"}` |
| BYTES | `{"type":"bytes","hex":"6162"}` |
| TIMESTAMP | `{"type":"timestamp","micros":"-5"}` |
| UUID | `{"type":"uuid","hex":"...32 hexadecimal digits..."}` |
| VECTOR | `{"type":"vector","dtype":"float64","space_ref":"1","components":[1.0,2.0]}` |
| DATE | `{"type":"date","epoch_day":"0"}`; proleptic Gregorian days from 1970-01-01 |
| LOCALTIME | `{"type":"localtime","nanoseconds":"1"}`; nanos since local midnight |
| TIME | `{"type":"time","nanoseconds":"1","offset_seconds":3600}` |
| LOCALDATETIME | `{"type":"localdatetime","epoch_day":"0","nanoseconds":"1"}` |
| DATETIME | `{"type":"datetime","epoch_seconds":"0","nanosecond":1,"offset_seconds":3600,"zone":"Europe/Paris"}`; zone may be null |
| DURATION | `{"type":"duration","months":"1","days":"2","seconds":"3","nanoseconds":4}` |
| DECIMAL | `{"type":"decimal","coefficient":"1230","precision":10,"scale":3}`; exact value 1.230, no DOUBLE conversion |
| LIST | `{"type":"list","items":[...encoded values...]}` |
| MAP | `{"type":"map","entries":{"key":...encoded value...}}` |

Temporal native values, including nested entity properties and path components,
are validated and copied with their exact codec. JSON retains native type,
nanoseconds, recorded offset/zone and independent duration components. Wide integer
coordinates use decimal strings; subsecond remainders and bounded offsets use
JSON integers. Serialization never consults current timezone rules. These tags
extend the development v1 observation grammar, not a graph write-handle API or
general-purpose temporal import/export claim. [Native temporal value contract](TEMPORAL_VALUES.md).
Native decimal properties now use the explicit tag above, including nested values.
Scalar CLI JSON uses the same validated tag, and typed local CSV/JSONL/SQLite
imports accept it under an explicit DECIMAL declaration. These preserve p/s;
ordinary JSON maps and CLI parameters are not automatically interpreted as native
values. See [tagged decimal fields](LOCAL_TEXT_IMPORT.md#native-decimal-fields-006-development).
Their owned observations retain declared precision/scale; integer coefficients use
strings to avoid JSON consumer precision loss. This observation export does not
qualify CLI/import/history/transfer support. [Decimal scope](specs/DECIMAL_VALUES_V1.md).

## Deleted bindings versus detached observations

A live query binding cannot read properties or labels after its owner instruction
deletes the entity. This includes aliases, list/aggregate traversal and spill
restoration. Such access raises the structured runtime
`plan_error` / `deleted_entity_access` and rolls back the whole instruction.
Previously computed scalars, identity, counts and immutable relationship type
remain valid. A returned detached DTO is an observation, not a live handle; it
retains its documented values even if the entity was deleted. This does not
change independent readers' snapshots. See the
[query examples and exact error fields](QUERY_LANGUAGE.md#content-access-after-deletion).

Normal content reads without a deletion in the instruction do not scan transaction
delete intents. A spilled binding has no physical write authority; its qualified
record identity is checked against lazily decoded deleted headers, at most once
per deleted reference. This lookup does not scan the stored graph or restore
write authority from temporary bytes. Existing statement/memory limits apply.

## Native execution and remaining integration

Materialization stages returned inserts into the transaction's private intent set
under the already-active whole-statement rollback mark. It does not commit or
publish to other participants. Late engine conversion and public string-admission
failures are tested to roll back that statement while preserving earlier successful
statements. Independent cursors retain their original snapshot while writers commit.

Sorting and DISTINCT spill preserve entity bindings and source-version metadata;
pending references are resolved to an existing authenticated transaction reference,
never newly minted from temporary bytes. Materialization shares reduced overlay
state per table/revision instead of rescanning all transaction writes for each row.
`RETURN DISTINCT n ORDER BY n.id` can read a property of the returned entity;
properties of discarded variables remain refused.

UNION deduplicates by qualified entity identity, not properties or a table-local
integer. UNION ALL retains duplicates. Returning UNION subqueries preserve known
same-kind table alternatives for subsequent property access and grouping; a NULL
branch does not discard the peer's entity type. `collect`, `collect(DISTINCT ...)`,
`min` and `max` preserve entity observations through the bounded spill codec.
Mixed scalar/entity results remain values, not a claim that every returned column
has one entity kind. Different output headings are refused during planning with
`reason="different_columns_in_union"`. Alias-expanded typing retains the existing
expression-depth budget even when an entity is a legal output.
One chain cannot mix UNION and UNION ALL; combine different policies in separate
returning subquery scopes. This replaces the former mixed-chain extension without
introducing a legacy mode or changing the transaction/snapshot boundary.

```cypher
CALL () {
  MATCH (n:Person) RETURN n
  UNION ALL
  MATCH (n:Person) RETURN n
}
WITH n, count(*) AS copies
RETURN n, copies ORDER BY n.id
```

Named paths, aggregate/spill and alias/scope composition participate in the
[complete required-profile execution](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md)
and the [supplemental native contracts](conformance/EXTENSION_COVERAGE.md).
Community maps native node/relationship/path observations and temporal values
into plain result envelopes without provider-specific Core code. The
[installed/API/browser/MCP evidence](reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md)
records exercised migration flows. The roadmap tracks final whole-delivery
acceptance separately; no production upgrade is implied by this API contract.
