# Logical relationship types over physical endpoint tables

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[FP-3 evidence](../conformance/FP3_PROGRESS.md) ·
[Full parity scope](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-3--entity-identity-polymorphism-and-paths)

Status: native catalog/codec/replay, DDL, read/path/value and statically bound
write integration in the 0.0.6 development line. Full FP-3 acceptance and wider
consumer integration remain pending. Do not activate metadata by calling domain
catalog methods on a live store. Use the
[native DDL and its admission contract](../QUERY_LANGUAGE.md#relationship-types-spanning-endpoint-tables).

## Required behavior and representation

Automatic flexible types additionally support append-only endpoint-pair growth
inside native CREATE. This does not loosen explicit typed groups: only groups
whose physical members all have the flexible property model can be extended.
See the [flexible graph contract](FLEXIBLE_GRAPH_V1.md#automatic-relationship-types)
for dynamic read phases, rollback and recovery-prefix validation.

The frozen required cases `expressions/path/Path2.feature#0001` and `#0002`
use a single `REL` type between different node-table pairs. Multiple alternatives
such as `:AB|BC` do not fulfill that requirement. Expected queries, relationship
types and results must not be renamed in the TCK adapter to manufacture a pass.

An explicit logical type groups existing *physical schema definitions*. Every
member retains its immutable table ID/name, fixed source/target node tables,
heap tuple, endpoint indexes and table-qualified entity identity. No endpoint
record is reinterpreted as a globally unique ID and no heap format is changed.
The native DDL creates the member tables and logical definition atomically;
the catalog method alone does not perform that storage operation.

This design reuses the heterogeneous traversal engine and existing physical
write/index protocols. It does not introduce schema-free nodes, arbitrary labels,
a cross-store transaction, a shared writer lock or weaker OCC/WAL semantics.

## Implemented domain contract

`domain.model.relationship_type.RelationshipTypeDef(name, table_ids)` is a frozen
value. The name is an exact ASCII identifier, at most 128 characters. Members
are a nonempty exact tuple of strictly ascending distinct u32 table IDs; booleans
and mutable/noncanonical containers are refused rather than normalized silently.

The internal `Catalog` provides:

| Operation | Contract |
| --- | --- |
| `add_relationship_type(definition)` | Requires catalog v2; validates the complete definition before installing any group/capability change. Not a live-store activation API. |
| `relationship_types()` | Explicit groups sorted by logical name; ordinary single-table types remain implicit. |
| `relationship_tables(name)` | Group members ordered by physical ID, or a singleton for an ungrouped relationship table; missing/node/member-physical names do not become additional logical types. |
| `relationship_type_name(table_id)` | Logical group name, or the ungrouped physical relationship name; rejects node tables. |
| `tables()`, `table(name)`, `table_by_id(id)` | Unchanged physical schema/identity lookup; logical names never alias physical IDs. |

Installation refuses a logical name colliding with any physical table or existing
group; missing/non-relationship members; ownership by another group; duplicate
endpoint pairs; missing/non-node endpoints; or differing member property schemas.
Members share columns, types, nullability and primary-key declaration. Each member
keeps its own physical schema/version history. Group identity and membership are
immutable through this operation; re-registering an existing name is an error.

After grouping, a physical table cannot be created under the group's logical name.
Single-member nullable-column alteration is refused, because it would split the
logical property schema. Atomic group-wide evolution remains integration work,
not an excuse to mutate one member behind the catalog's back. Copies share frozen
definitions but own their maps, reverse ownership and serialization memo.

## Durable encoding and admission

Catalog format remains v2. Required capability **bit 19**, spelling
`relationship_types_v1`, guards an extension following all ordinary index
definitions and preceding the whole-catalog CRC32C:

```text
group_count: u32, nonzero
for each group in strictly ascending logical-name order:
    name: ordinary catalog length-prefixed text
    member_count: u32, nonzero
    table_id: u32 repeated member_count times, strictly ascending
```

No extension or capability is emitted for ordinary catalogs. Their previous byte
encoding is unchanged. The group count and each member count cannot exceed the
declared table count. Decoding verifies byte coverage, canonical order, ownership,
endpoint/schema references and the same value invariants before returning a usable
catalog. Valid checksums do not bypass these checks. Persisted invalid definitions
raise corruption; caller-supplied invalid definitions raise configuration errors.

An unsupported required bit is rejected before body interpretation. The current
test simulates an older capability inventory; it does **not** replace the planned
installed-old/new-binary admission test. Activation through
`CREATE REL TABLE GROUP` requires prior catalog v2; use the existing
`maintenance.ensure_identity_indexes()` outside a transaction when needed. The
DDL refuses v1 before member effects. No direct catalog mutation or implicit
inner commit is authorized.

The extension is inside the existing WAL-covered complete catalog page image,
not a sidecar or inferred relationship-name convention. Native recovery decodes
and validates it before applying pages. Across consecutive catalog snapshots in
the selected committed WAL range, established logical names, membership and
physical member identity/endpoints cannot disappear or change. New disjoint
groups are allowed. An individual valid snapshot does not authorize an invalid
transition. This range check does not invent an absent checkpoint baseline.

Tests cover exact independently assembled bytes, copy isolation, malformed and
truncated extensions (with recalculated CRC), count/order/reference violations,
missing capabilities, page-image validation without host reads/writes, committed
idempotent replay, no effects without COMMIT, and refusal of invalid snapshots or
identity transitions before any page application.

## Native integration status and remaining acceptance

1. Native DDL/AST/plans and detached catalog inspection now expose one logical
   type and its endpoint pairs. Member creation/index attachment and group
   publication share a statement rollback boundary, including second-member
   fault injection. A current-version participant opened before the group adopts
   its committed schema; installed older-binary refusal remains to be qualified.
2. MATCH/OPTIONAL/ranges and CREATE/MERGE with static or runtime-bound endpoints
   resolve the logical type natively. Multi-member SET/DELETE use the actual
   row schema. A dynamic creation plan carries a closed candidate set and selects
   one member for the actual endpoint pair before the unchanged identity and
   endpoint-dependency/OCC proofs. Unknown pairs refuse atomically, never select
   the first member. Late failure rolls back preceding deletes/inserts/updates.
3. `type(r)`, detached relationships/path values and UNION results expose the
   logical name while retaining physical identities. There is no second Cypher
   type equal to a grouped member's physical name. Broader spill/aggregation,
   projection and consumer combinations still require acceptance. Opt-in history
   now stores logical group names and exposes selected historical membership,
   preserving empty members and excluding future/unselected members.
4. Application migrations preserve complete native catalog metadata during
   their fenced preview. Atomic group-wide schema evolution,
   broader projection and CLI semantic inventory
   remain integration work. Logical export/import now preserves typed/flexible
   groups through [artifact format 2](../LOGICAL_TRANSFER.md#artifact-v2-flexible-models-and-relationship-groups),
   remapping endpoints and attaching logical authority through native transactional
   schema staging. Bounded existing-target copy now preserves typed groups over
   PK-bearing nodes, resolving target members by logical name and endpoint pair,
   not physical name/ID. [Copy contract](../CATALOG_COPY.md#typed-logical-relationship-groups).
   Flexible/no-PK copy also uses native target bindings and remapped endpoint pairs,
   preserving distinct equal-valued nodes without copying physical source IDs.
5. Original Path2 cases #0001/#0002 now execute with explicit native typed schema
   adaptation and unmodified queries/results. The path selection is 4 original
   and 3 adapted passes, no selected failures/not-run. Full frozen-profile,
   installed-binary, concurrency/fault and isolated Pulse acceptance remain open.

No new configuration knob is needed. Query budgets and transaction/durability
contracts remain in force. This development checkpoint changes neither installed
Pulse, its Core boundary, a production graph nor the public parity claim.
