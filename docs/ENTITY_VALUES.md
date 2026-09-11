# Detached entity result model — FP-3 work in progress

[API reference](API_REFERENCE.md) · [FP-3 evidence](conformance/FP3_PROGRESS.md) ·
[Roadmap](../ROADMAP.md#functional-parity-expansion-plan)

This page documents the **0.0.6 development API**, not a published release or
completed FP-3 acceptance. Native `execute` and cursors now return `NodeValue`
for typed and polymorphic nodes and `RelationshipValue` for relationships, also
inside lists/maps. Both types, `EntityIdentity` and `EntityProvenance`, are exported
from `okto_grafx`. `PathValue` remains internal: native path-result integration,
entity UNION and coordinated Pulse migration are still pending.

This is an intentional breaking result change, with no legacy-result mode. Replace
assumptions that an entity is a table-local integer or a mutable `label/properties`
map with the explicit attributes below. Scalar projections such as `RETURN n.id`
are unchanged. The installed production Pulse has not been upgraded by this work.

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
| `NodeValue` | `identity: EntityIdentity`, `label: str`, `properties: Mapping[str, object]`, `provenance: EntityProvenance`; `labels: tuple[str, ...]` property |
| `RelationshipValue` | Same observation fields, plus `source: EntityIdentity` and `target: EntityIdentity` |

Each type offers `to_dict() -> dict[str, object]`. These are detached data values,
not alternate mutation or parameter APIs. Query parameters still refuse entities.
`QueryValue`, also exported from `okto_grafx`, annotates result scalars, nodes,
relationships and recursive result lists/maps. It is distinct from the stored/
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

Nodes carry a typed-table `label`, singleton `labels` tuple, identity, properties
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

A path owns ordered node and relationship tuples. It has one more node than
relationships; a zero-hop path owns one node. Each relationship must connect its
adjacent nodes in either direction. Ordering distinguishes forward and reverse
walks. All observations in one path must share the database and read snapshot.

Path equality/hash use the ordered entity identities, not property observations.
Repeated nodes and cycles are representable. The DTO represents a walk; the query
executor must separately enforce the pattern's relationship-uniqueness/trail rules
and traversal/cancellation budgets. Constructing this DTO does **not** implement
general variable-length named traversal.

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
| LIST | `{"type":"list","items":[...encoded values...]}` |
| MAP | `{"type":"map","entries":{"key":...encoded value...}}` |

There is no durable format change or entity-deserialization/write-handle API in
this increment. JSON values are observations, not executable references. Temporal
and nested stored-type expansion in FP-5/6 must extend this grammar explicitly.

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

Path output/functions, entity-valued UNION and all aggregate/spill combinations
still need complete integration and frozen-case validation. Broader alias/scope
composition, projection budgets, public adversarial tests and the full query/txn
checkpoint remain open. Pulse result/JSON consumers must be mapped and tested
before the public breaking change is accepted as a coordinated delivery.
