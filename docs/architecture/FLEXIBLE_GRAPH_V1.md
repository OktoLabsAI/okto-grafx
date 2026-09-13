# Native flexible graph: implicit labels, unlabeled nodes and relationships

Development on `feature/v0.0.6`; not a release or full schema-free/TCK parity claim.
This implements unlabeled nodes, implicit single-label creation, dynamic properties
and automatic undeclared relationship types. Full package qualification remains
in progress; arbitrary multiple labels are not implemented.

## Native query contract

```python
from okto_grafx import connect

with connect("./flex.grafx") as db:
    with db.begin("write") as tx:
        tx.execute("CREATE (a {value:'start'}), (b {value:2}), (empty)")
        tx.execute("MATCH (n {value:2}) SET n.value={nested:[true,3]}, n.extra='yes'")
        tx.execute("MATCH (n {extra:'yes'}) SET n.extra=null")
    nodes = db.execute("MATCH(n) RETURN n").rows
    assert all(row[0].labels == () and row[0].label is None for row in nodes)
```

`CREATE (n)` / `CREATE (n {properties...})` allocate real heap entities with
qualified node identity. `MERGE (n {properties...})` searches all node tables and
creates an unlabeled node only if none matches. Matching preserves multiplicity;
MERGE with a NULL property still refuses. Zero-input writes do not create a store.

Properties are heterogeneous native values, using the
[ANY admission contract](HETEROGENEOUS_PROPERTIES_V1.md). Adding a key or changing
its value type does not require a catalog rewrite. Missing properties read NULL;
`SET n.key=null` removes the key. `properties(n)` and detached entity properties
expose only user keys; a user key spelled `_properties` remains a normal key.
Explicit typed-table nodes keep their column/primary-key constraints.
`CREATE (:NewLabel {value:1})` and single-node `MERGE` now create a previously
undeclared single label as a native flexible node table, without inferred fixture
DDL. Subsequent nodes may store different supported value types under the same
key. The table has no implicit primary key. Existing typed tables are never
silently converted. Node labels and relationship types now have independent
[namespaces](../specs/GRAPH_NAMESPACES_V1.md); equal names do not collide.

Creation is lazy: zero input rows, EXPLAIN and read APIs install no schema.
Same-statement MATCH, OPTIONAL, paths, pattern comprehensions and returning read
subqueries resolve the created models using their actual runtime table identities.
Prospective planner IDs are not storage authority: an intervening relationship
allocation or a skipped CREATE can change actual allocation order.

For unlabeled nodes, `labels(n)` is the empty tuple; the singular Python `NodeValue.label` and JSON
`label` are `None`/`null`. Label predicates do not match the internal physical
store name. Graph identity still carries its qualified physical table ID; the
absence of labels does not make nodes with different identities equal.
Implicit labeled nodes instead expose their actual singleton label, including
through transfer, copy and opt-in system history.

## Automatic relationship types

```python
with connect("./edges.grafx") as db:
    with db.begin("write") as tx:
        tx.execute("CREATE(a {v:'start'})-[:LINK {weight:2}]->(b {v:1})")
        tx.execute("MATCH()-[r:LINK]->() SET r.weight='changed', r.note=null")
    edges = db.execute("MATCH(a)-[r:LINK]->(b) RETURN a,r,b,type(r)").rows
```

An undeclared relationship type is created using the **actual bound endpoint
table pair**, with a flexible property map and real endpoint/relationship identity.
Its native logical type can subsequently gain additional pairs. Physical members
remain separate heap tables with durable endpoint indexes; their generated names
are not additional user relationship types. Existing explicitly declared typed
groups remain closed: neither their endpoint pairs nor column constraints are
silently widened. Flexible keys accept heterogeneous values and NULL removal
under the same admission rules as nodes.

The schema change and all node/edge writes belong to the outer statement. A late
error rolls them back together, including a newly appended endpoint member. Zero
input does not install a relationship type. Reads after the write phase in the
same statement resolve only the planned logical type names against that phase's
catalog: MATCH, OPTIONAL, paths, pattern predicates/comprehensions and explicitly
imported read subqueries see the created edges. EXPLAIN traversal details expose
these names as `dynamic_types`; resolution does not waive endpoint authority.

Node/relationship lists preserve entity provenance through `collect()`, homogeneous
list construction/concatenation and index extraction. This permits constructing
a chain from collected nodes without replacing them with unqualified IDs. Scalar
or mixed-kind lists do not acquire entity write authority.

For the existing single-edge, bound-endpoint MERGE contract, flexible properties
match only keys named in the pattern, not the complete stored map. Extra keys are
retained, private inserts/updates are visible, and NULL pattern values refuse.
This does not expand MERGE to arbitrary multi-edge/unbound-endpoint patterns.

The original mandatory Match4 #0004 fixture now executes without inferred DDL,
synthetic labels, value coercion or query rewriting. Its `var` property genuinely
stores both strings and numbers. This is not a full TCK-parity declaration.

## Schema and durable representation

`TableDef` adds exact boolean `flexible_properties` and `unlabeled`, both false
for ordinary typed tables. An unlabeled table must be a flexible node store.
Flexible layout is one non-null ANY column `_properties` (after the two physical
endpoints for a relationship layout). The value is a string-keyed map with no
top-level NULL entries; nested NULLs remain legal. No Python sidecar graph exists.

Catalog-v2 capability bit 21, `flexible_graph_v1`, depends on bit 20
`heterogeneous_properties_v1`. When present, each table has one flags byte after
its existing definition/prior-layout metadata: bit 0 flexible properties, bit 1
unlabeled; other bits refuse. Catalogs without this capability have no flags byte.
At most one unlabeled node store exists per catalog. Flags are independent of its
generated physical name. Record values still use concrete value tags, not new
untyped wire blobs. Old readers refuse unknown capability before recovery applies
pages. COMMIT remains required; replay is idempotent and rejects changing the
property/label model of an established table across committed after-images.
Logical relationship groups retain capability bit 19, `relationship_types_v1`.
Recovery permits append-only members for flexible groups, preserving their prior
member-ID prefix, and refuses removal/redefinition before page application. Typed
groups retain their immutable-member contract; no new wire format is introduced.

The first actual creation installs its table and capability through the same
native schema journal and outer statement boundary as its data. A pristine empty
v1 catalog can activate v2 within that operation. A nonempty v1 catalog requires
the existing explicit `db.maintenance.ensure_identity_indexes()` operation before
flexible creation; no inner maintenance transaction is silently opened. A later
statement error discards schema and data created by that statement. Restoration
includes the working catalog and exact schema-journal suffix, not only staged
pages. Earlier successful statements remain intact; failed `executemany` removes
the entire batch's effects. Unproven cleanup aborts the transaction rather than
allowing a later commit. Snapshot/OCC,
independent participants, WAL and publication rules are unchanged.

## Bounds and integration status

No new tuning flag is introduced. Property map depth, values, transaction bytes,
query memory/traversal and cancellation limits remain enforced. Flexible scans
retain the complete bag; mixed-type property indexes and bag projection pushdown
are not claimed. Typed identity/endpoint indexes remain separate.

Logical graph transfer preserves flexible node/edge flags, property maps, empty
labels and logical relationship groups in a versioned format-2 artifact, with
fresh target identities, endpoint remapping and resumable private publication.
See [transfer format and semantics](../LOGICAL_TRANSFER.md#artifact-v2-flexible-models-and-relationship-groups).
Copy/promotion into existing compatible targets now preserves separate native
identities even for equal bags, remaps unlabeled stores and actual endpoint pairs,
and publishes data with one idempotent receipt. No-PK nodes are never implicitly
deduplicated; [identity contract](../CATALOG_COPY.md#flexible-and-no-pk-entity-identity).
Schema growth of an entity's keys uses its map,
not nullable-column append. Opt-in retained history now preserves flexible flags,
logical relationship names, intervals and selected historical membership through
scan/index reads, retention/compaction and physical backup; it requires the
[separate model-history capability](../SYSTEM_TIME_HISTORY.md#durable-model-admission-and-compatibility).
Wider acceleration/interop and installed-Pulse qualification remain in the
[full acceptance plan](../specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties).

Focused sources: `tests/storage_core/test_flexible_graph_catalog.py`,
`tests/query/test_unlabeled_nodes.py`, `tests/query/test_fp3_merge_nodes.py`,
`tests/query/test_flexible_relationships.py`, `tests/query/test_implicit_node_labels.py`
and the original upstream Match4 case.

## Authorization checkpoint: native Match4 semantics

The broader-model decision keeps Match4 #0004 mandatory: unlabeled nodes and
heterogeneous properties are product capabilities, not architectural exclusions.
Resource limits, explicit typed-table constraints, finite storage admission and
the transactional protocol remain enforced. This does not claim arbitrary
multi-label support or completion of the full functional-parity plan.

Re-execution with the frozen V2 ledger and its V1 predecessor is recorded in
`.grafx-tmp/flexible-model-authorization-match4.json`, SHA-256
`6da17004095bb965c0ce239bd175c8280af6c46465892c86cd8bd5cc7cc43df2`.
All nine required Match4 cases pass, including #0004 with an empty adaptations
list. The one retained multiple-label divergence (#0005) still fails; 3,887 cases
are outside this selection. Neither fixtures nor expected results were changed.

The first complementary regression receipt,
`.grafx-tmp/flexible-model-authorization-reconfirmed.xml`, records 194 passes and
one failed test assertion, not a green run (195 tests, zero errors/skips;
SHA-256 `d371f152e86654a377c6ca0e3071b3eb67ace5e553f173b0f1b62930a22f432e`).
The existing MERGE NULL refusal correctly occurred, but the test matched uppercase
`NULL` against lowercase `null`. The test now verifies `GrafxPlanError`, native
reason `merge_null_property` and execution phase instead of message capitalization.

The complete corrected selection passes: **195 tests, zero failures/errors/skips**,
113.888 s, `.grafx-tmp/flexible-model-authorization-qualified.xml`, SHA-256
`197147b9b0bff7f156f80bd160d31d1b9a7f23a18770714449e38d574104db2b`.
It covers unlabeled nodes, ANY properties, expression NaN/storage rejection,
flexible relationships/catalogs, logical transfer, copy, system history and its
codec. Snapshot/conflicting-writer, rollback and committed recovery tests are
included. This focused receipt is not a whole-repository or installed-Pulse gate.
