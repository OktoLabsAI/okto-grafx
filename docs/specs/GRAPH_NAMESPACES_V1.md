# Independent graph namespaces — FP-3/4 integration contract

The required original Graph5 #0002 has a node label `T2` and relationship type
`T2` simultaneously. Before this increment, native fixture execution refused with
`field=label, value=T2`. This remains mandatory; do not rename fixture labels,
physical entities, expectations or ledger ownership to satisfy it.

## Representation and admission

Node and relationship names are exact, case-sensitive names in distinct kind
namespaces. Existing physical table IDs, record IDs, endpoint identities and
schema versions are preserved. Permit both ordinary node/relationship tables
with one spelling and a node whose name equals a logical relationship group.
Do not solve only the automatic-group fixture while leaving ordinary typed
tables unable to represent the same graph.

Catalog v2 capability `graph_namespaces_v1`, bit 24, is required when a catalog
first contains such an overlap. The existing table/group encoding already records
kind and IDs; no extra body extension is needed. Catalogs without overlap keep
their existing bytes/capabilities. Old binaries reject the new required bit before
serving or mutating an overlapping graph. Missing capability with overlapping
body definitions is corruption, never inferred admission. Current participants
must adopt the complete catalog through the existing schema/registry/WAL protocol.

Domain and detached public catalog `table(name, *, kind=None)` and
`has_table(name, *, kind=None)` accept `kind="node"` / `"rel"`. A bare physical
table lookup remains valid only if unique; ambiguity refuses explicitly with
`reason="ambiguous_table_name"`. Numeric `table_by_id` remains unambiguous.
Logical relationships resolve through `relationship_tables(name)` independently
of node labels. Same-kind duplicates and competing ordinary/group relationship
types remain errors. Endpoint-name resolution is always in the node namespace.

## Required integrations before completion

1. Catalog construction, copy, codec, malformed/missing-capability admission and
   detached public views; no capability mutation before full validation.
2. Native DDL/implicit CREATE/MERGE and MATCH/OPTIONAL/path/property/type/label
   resolution in either creation order, including an already existing ordinary
   relationship table. Alias/entity identity never comes from a name alone.
3. Index/registry ownership, endpoint staging, schema refresh and recovery transitions
   use table identity/kind; no first-match lookup, store-name collision or weakened
   digest check. Whole-statement rollback covers late schema/data failures.
4. Verification/history/copy/transfer and public by-name operations either accept
   explicit kind/identity or clearly refuse ambiguous requests before effects;
   required transport paths must actually preserve and consume both entities.
5. API/CLI documentation and Pulse Community callers qualify known node/relationship
   intent without coupling Core to Grafx. Existing unique-name consumers retain
   their behavior; no global installation is implied.
6. Original Graph5 #0002, owner profile, focused fault/concurrency/codec tests,
   grouped affected regression and installed old/new-binary admission qualify
   the capability. A green fixture alone is not package acceptance.

This contract does not add multiple labels, label mutation, cross-store commits,
weaker OCC/read isolation, external-effect rollback or unbounded resources. Label
SET/REMOVE and the other pending FP-4 forms retain their existing separate scope.

## Status

September 12, 2026, development implementation — not release/package acceptance.

Implemented: kind-keyed catalog, capability/codec guards, qualified detached
catalog and `scan_rows_v1(..., kind=...)`; native DDL/implicit CREATE/MATCH/OPTIONAL/
paths and relationship type predicates; node-qualified endpoint/materialization
and ID-qualified committed index authority. No name/ID rewrite or inner commit.
Original source, expected results and the frozen ledgers remain unchanged.

Native owner receipt `.grafx-tmp/fp3-graph-namespaces-first-native.json`:
528 original required passes, zero required failures; 17 retained multiple-label
divergences fail; 3,352 outside selection. All 545 selected cases executed with
`--owner FP-3 --execute-stateful`, both V2 and predecessor V1 ledger verification,
without `--infer-fixture-schema`. Terminal exit 1 includes those explicit
divergences; it is not a zero-failure full-TCK receipt. SHA-256:
`2dd765328e09e09c946b48952240160ec63200d422d26921a938e0037907b944`.

The subsequent consumer increment implements qualified copy, logical transfer/
resume, system-time reads/activation/pins/retention and full-text index creation/
replacement. Selectors for copy/history accept `(kind, name)`; scans and full-text
creation take explicit `kind`. Endpoint closure always resolves nodes. Custom
exact indexes retain their existing node-only contract and resolve that kind.
Verification uses qualified history selections instead of ambiguous names.
Hybrid search also qualifies its node target and physical relationship expansion
in both incident-index and scan regimes; lexical/vector result ownership remains
checked by table identity, not the common spelling.

Logical transfer emits `okto-grafx-logical-3` for overlapping namespaces, qualifies
custom-index ownership and supplies `RecordIdMapping.kind`; consumers key mappings
by `(kind, table, source_record_id)`. Import allocates against each target table's
durable identity floor, including fresh small-batch imports, and resumes using
qualified prefix inventories. Copy uses digest domain `grafx-copy-package-v3`
when its selected schemas overlap. Neither change introduces inner commits into
copy, weakens receipt idempotency or changes source identities. See
[logical transfer](../LOGICAL_TRANSFER.md), [copy](../CATALOG_COPY.md),
[history](../SYSTEM_TIME_HISTORY.md) and [indexes](../INDEXES_AND_VECTORS.md).

The [component and missing-artifact follow-up](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
now covers those vector obligations. The [projection/view/migration follow-up](../reports/NAMESPACE_PROJECTION_VIEW_MIGRATION_QUALIFICATION.md)
qualifies known-kind projection scans and metadata ledgers, and retains both
homonymous view dependencies. It passes 87 affected tests and eight process cuts.
The subsequent [installed logical-transfer matrix](../reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md)
qualifies actual old importers for format admission/refusal, with their tiny-batch
limitation disclosed separately. Remaining before completion: audit remaining
by-name API consumers, broader Pulse integration and package acceptance.
The subsequent [expression-local view follow-up](../reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md)
captures both physical owners inside EXISTS/pattern expressions, including logical
group growth; 176 affected tests pass. This is source, not installed acceptance.
The [CLI selector follow-up](../reports/CLI_VECTOR_OWNER_QUALIFICATION.md) adds
physical `--table` / `--table-kind` selection for shared-space vector search and
verifies unchanged text-index/hybrid-node ownership. It does not merge owners.
Qualified maintenance and the Pulse source-transfer adapter now have the focused
evidence below, not installed-Pulse acceptance. Installed catalog admission passed
for the two archived binaries and profiles below; this does not certify every
historical binary, platform or integration.
Do not activate this development capability in production based on the query
receipt alone.

### Focused and affected regression evidence

- `.grafx-tmp/namespace-maintenance-regression.xml`: **104 passed**, zero
  failures/errors/skips, 124.206 s. Bloat, vacuum (including overflow/recovery),
  nullable columns, namespace history/transfer/catalog/recovery. Sixteen new
  maintenance cases qualify pure/NumPy, separate owners/reopen, ambiguity before
  capability effects, invalid selectors and reserved internal names.
  SHA-256 `f441dd95b85b69cab582aa0d1157a20b595ef27c2a33175f40771f1356624908`.
  The initial focused receipt contained four failing tests that accessed a
  nonexistent detached catalog property; corrected assertions inspect the
  authoritative internal catalog in those tests, without changing production
  behavior or relabeling that failed receipt.
- `.grafx-tmp/namespace-column-recovery.xml`: **8 passed**, zero failures/errors/
  skips, 38.821 s. Real subprocess cuts before COMMIT and after durable COMMIT
  before page application, both physical kinds and pure/NumPy. Reopen twice,
  checkpoint and verify preserve only the selected schema change when committed.
  SHA-256 `5a04fea447e7505bb27a846b9c9d8334f5c46c965f27dddc6ee45e091ff1c70e`.
  Public `add_nullable_column`, `maintenance.bloat` and `vacuum` accept the common
  `(kind, name)` selector, refuse ambiguous bare names before effects and preserve
  physical IDs. Report names are qualified only when the catalog is ambiguous.
  Grouped-column restrictions, vacuum quiescence and WAL/OCC guarantees remain.
- Paired Pulse source transfer: **99 passed**, zero failures/errors/skips,
  including complete board/global physical round trips and six qualified
  endpoint-map success/failure cases. Adapter catalog/scans and complete physical
  inventory validation now distinguish node/relationship kinds. Core is unchanged.
  See [paired source receipts](../reports/FP4_PULSE_SOURCE_QUALIFICATION.md#namespace-transfer-follow-up)
  for source identities, the initial failed regression and the corrected final
  run. No installed runtime or API/browser qualification is claimed.

- `.grafx-tmp/graph-namespaces-search-requalification.xml`: **116 passed**, zero
  failures/errors/skips, 88.417 s. All logical-transfer and grouped-copy tests
  requalified after the two contract updates; four new pure/NumPy/reverse-DDL
  search cases plus existing hybrid, vector access-path, filtered-vector,
  speculative DDL ownership and active-index projection files pass. The new
  hybrid cases first reproduced ambiguous node-target refusal (four failures in
  `graph-namespaces-search-first.xml`), then pass node-qualified text/vector
  fusion and relationship-qualified incident/scan expansion after reopen.
  SHA-256 `517a6995f13e55d4e739e8fbdb7d9aa629320fed36f295a49c2a03b61e744e44`.

- `.grafx-tmp/graph-namespaces-consumption-regression.xml`: **344 passed, two
  failed**, zero errors/skips, 494.645 s. Includes all 33 new copy/transfer/history
  cases: both physical kinds with equal names and IDs, logical groups, flexible
  properties, pure/NumPy transfer, qualified full-text replacement, retained
  snapshots, pins/pruning, repeated verification/reopen and real process cuts
  around transfer batches/promotion and history COMMIT/page application. The
  wider selection covers related existing APIs, custom-index preparation and the
  planner. SHA-256
  `e766a82f3f3840e53485bb851bcfd76ae7d956d025387306e58c018c65da5aee`.
  The two retained failures asserted a graph-global destination-ID sequence and
  a node/group name prohibition. Updated tests require qualified table-local
  mapping with correct endpoints and refusal of a missing destination group,
  respectively; this receipt is **not relabeled green**. Two intermediate local
  requalification attempts each passed 33 and failed one malformed-fixture field
  expectation; they remain separate failed receipts, not native runtime failures.

- `.grafx-tmp/graph-namespaces-focused-final.xml`: **113 passed**, zero failures,
  errors or skips, 39.031 s. Catalog guards, native overlap in either creation
  order, optional/path/entity predicates, qualified scan/cursor sibling refusal,
  separate handles with concurrent node/edge writers and pinned old reader,
  late statement rollback and four real process cuts (ordinary/grouped overlap,
  pre-COMMIT / post-durable-COMMIT-before-page-application), repeated reopen and
  `verify("all")`. Includes explicit legacy-v1 upgrade-before-effects admission.
  SHA-256 `06e50f7dbc3986b3f9947d0e675c3748d1434aad3d9ac0a2a85a07cece84073d`.
- `.grafx-tmp/graph-namespaces-scan-regression.xml`: **33 passed**, zero other
  outcomes, 9.544 s. Projected bounded scans, endpoint/incident/seekable scans
  and node scan projection. SHA-256
  `297ecd1193b5b422feb77ae41ad3d7fd059e16cda46e01f2815610a1bbd24f16`.
- `.grafx-tmp/graph-namespaces-planner-contract.xml`: **151 passed**, zero other
  outcomes, 18.040 s. Complete planner and typed-endpoint files, including the
  updated absent-kind semantics. SHA-256
  `d6be68c6fbdeab5a203db7793861d4a9df5956ecc7ff65615ee7b1558aa110db`.
- `.grafx-tmp/graph-namespaces-affected-regression.xml`: **1,108 passed, three
  failed**, zero errors/skips, 403.615 s. Selection: files in query/txn/storage/API
  matching `graph_namespaces|relationship_type_catalog|catalog_v2|fp3_|polymorphic|
  typed_endpoints|implicit_node_labels|flexible_relationships|unlabeled|
  schema_transactionality|schema_transactional|scan_rows|structured_scan|
  write_subquery_recovery|updating_union_recovery|implicit_index_authority|
  catalog_views|public_views`. SHA-256
  `dd0f35728eb82f85d57b36e01f5ea98a1a2899d4bc78539abb50bbbd828ec7f0`.
  All three failures were local negative tests for the superseded cross-kind
  name prohibition; they now assert successful creation/empty absent-kind reads
  and preserved schema/data. This retained receipt is **not relabeled green**.
- `.grafx-tmp/graph-namespaces-contract-requalification.xml`: **112 passed**,
  zero failures/errors/skips, 50.615 s. Re-executes all three affected files
  (`test_implicit_node_labels.py`, `test_fp3_relationship_groups.py`,
  `test_typed_endpoints.py`) after replacing those obsolete local expectations
  with positive namespace tests. SHA-256
  `9de6d22645747d731717a961d9f1847ba9515ae261754938391f9be8feca6a24`.

Counts overlap and are not additive. These are source-tree, affected-area
checks, not a new whole-repository or installed-Pulse acceptance run. No global
installation, production database, release, commit or push was changed here.

### Confirmed vector-owner blocker (runtime and durable naming repaired)

The initial audited `engine/vector_engine.py` kept attachment/publication maps keyed by
space name (`_by_space`, `_durable_by_space`, epochs and maintained horizons).
`index(space)` was singular and the derived index name used table spelling, not
kind/physical identity. Ordinary relationship DDL did not attach vector columns
through the same path as node DDL; refresh/reopen could therefore select a different
physical owner. This is not merely a CLI naming issue.

The local isolated reproducer `.grafx-tmp/namespace-vector-audit.py` committed an
empty node table R and relationship table Edge, each with a column in the same
two-dimensional cosine space. Before close the sole attached index was
`vector_R_semantic`, table ID 1; after reopen it was `vector_Edge_semantic`, ID 2.
Using R as both table names instead exposed just `vector_R_semantic`, ID 1,
before and after reopen: that observation does **not** prove coexistence or a
successful second owner. No production data was involved. These are diagnostic
observations, not passing vector qualification or a measured performance gain.

The subsequent [runtime repair](VECTOR_PHYSICAL_OWNERS_V1.md) qualifies local
ownership/claims/epochs, attaches relation vectors during DDL, and propagates
physical owners into native/public/hybrid search. It also fixes discarded SET
vector conversion. **219 affected tests pass**, including shared-space independent
tables, pinned-reader/foreign-writer checks, both codecs and eight process cuts.
The subsequent [durable naming repair](VECTOR_OWNER_NAMES_V1.md) adds capability
25 and collision-free names for new owners without renaming existing artifacts.
It passes 185 affected tests, a subsequent 20-case API suite and 24 installed-wheel
admission cases, including adoption of an intact index written by old binaries.
Public diagnostic/rebuild selectors subsequently pass [95 affected tests](VECTOR_QUALIFIED_MAINTENANCE_V1.md).
The subsequent [component/legacy repair qualification](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
covers genuinely missing old indexes and lower-level owner-selected mutation.
This is not full namespace acceptance or an installed-Pulse claim.

Required repair: qualify attachment/staging/search/publication/maintenance by
physical owner, provide collision-free durable index identity with explicit
adoption semantics, and carry known ownership from native queries and public
selectors. Bare ambiguous space requests must refuse, never pick an owner.
Preserve speculative-DDL rollback, epochs, committed-mapping validation, mixed-
version admission and recovery. Test both kinds and different names, both DDL
orders, shared spaces, writes/search before and after reopen, concurrent handles,
failed DDL and process cuts. Rejecting currently admitted multi-owner DDL merely
to hide the defect is not the planned resolution. The linked runtime, naming,
maintenance and legacy-repair evidence together cover this vector repair scope;
the earlier maintenance receipt alone did not close it. Remaining consumer and
full-package namespace acceptance still requires its own evidence.

### Installed catalog/WAL admission

The namespace mode of `tools/qualify_temporal_wheels.py` passed **24/24** real
installed-wheel scenarios on Windows/Python 3.13: two archived wheels (0.0.5 and
an earlier 0.0.6), pure and accelerated writers, materialized/pending-WAL/live
handle states, read and write admission. Both old wheels lack bit 24. A plain
old store remains readable after a current idempotent update until namespace
activation. After activation, each old reader/writer refuses with the exact
capability error; a read-only pending-WAL open instead refuses checkpoint
consistency before recovery. Every file hash remains unchanged across refusal,
without WAL/control exclusions. The current wheel recovers both same-named kinds,
node/edge properties and endpoint identity, verifies all stores and reopens twice.

Candidate wheel:
`.grafx-tmp/graph-namespaces-wheel-qualification/candidate/okto_grafx-0.0.6-py3-none-any.whl`,
SHA-256 `01d51080e660f16f2acca3d4dbd05e87c9923e5a8fa8cdaf29fd92517981c90c`.
At the namespace checkpoint, before the later MERGE-action increment, all 238
source Python modules matched their installed candidate bytes. Archived
binaries are the same isolated installations identified in the
[preceding temporal wheel matrix](TEMPORAL_VALUES_V1.md#installed-wheel-temporal-fence-qualification),
not fabricated old-capability masks. Worker `-I` invocations assert imported
origins beneath the selected interpreter prefix.

| Receipt under `.grafx-tmp/graph-namespaces-wheel-qualification/` | Result | SHA-256 |
| --- | --- | --- |
| `pure/report.json` | 12 passed | `9db3251d9bed5175ccbea58f287e2aaedea28045499834a19c66c28a92ba8897` |
| `accelerated/report.json` | 12 passed | `065ab2bee1ddfeaac5b4a89145129a0a6a9447df1c330224cb6ce5c545dd0f95` |

Verifier unit tests: `.grafx-tmp/graph-namespaces-wheel-verifier.xml`, 15 passed,
zero failures/errors/skips, 0.142 s; SHA-256
`f8399d82df643616ff5594932a08c5f4f08323edf56b62e4eb4b241b06c3233b`.
The successful matrix qualifies fail-closed mixed-version admission, **not**
mixed-version operation after activation. Upgrade all participants before use.
No global installation or Pulse runtime was changed.
