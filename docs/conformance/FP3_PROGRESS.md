# FP-3 working evidence: polymorphic reads and native entity results

The [independent namespaces increment](../specs/GRAPH_NAMESPACES_V1.md) now passes
all **528 required FP-3 cases** with original fixtures/queries. The subsequent
increment adds qualified copy/transfer/resume/history and full-text ownership;
hybrid search and 24 installed catalog/WAL admission cases pass. Vector, remaining
consumer and full-package qualification remains open. The preceding
[property-access checkpoint](PROPERTY_ACCESS_PROGRESS.md) records the unadapted
baseline (527 passes, one required failure), native entity KEYS/dynamic lookup
and property REMOVE. Earlier sections below retain their dated, narrower receipts;
they are not a current full-profile total.

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Full acceptance requirements](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-3--entity-identity-polymorphism-and-paths) ·
[Query contract](../COMPOSABLE_QUERIES.md#polymorphic-node-read-composition)

September 11, 2026, `feature/v0.0.6` development. This is an implementation
increment, **not FP-3 acceptance**, complete Cypher parity or a release.

## Temporal observations and all-NULL collection correction

FP-5 integration now carries all six native temporal values through node,
relationship and path observations, including nested properties, cursor results,
commit/reopen and explicit lossless JSON property tags. The general scalar-result
path alone was insufficient: 50 original FP-2 ordering cases still refused when
materializing whole entities. The correction extends the owned-value grammar;
it does not grant new parameter/write authority to detached entities.
[Value/JSON contract](../ENTITY_VALUES.md#json-grammar).

The grouped query regression also exposed all-NULL lists being inferred as
relationship collections. Empty/all-NULL lists now prove no graph kind, preserving
NULL propagation in path and entity functions. Two old assertions expected runtime
errors for literal list selections now statically proven invalid; the tests
retain a separate dynamic-index runtime case instead of weakening either error
contract. No original TCK fixture/expectation/ledger was edited.

`fp5-adjacent-null-entity-regression.xml` under `.grafx-tmp/`: **183 passed**, zero
failures/errors/skips, 65.427 s; SHA-256
`c9192891cc8cfdf67d5f318fb9f54adbf5c2b23d5cdc42f365ef0431458a32ee`.
`fp5-temporal-entity-results.xml`: **14 passed**, zero failures/errors/skips,
4.271 s; SHA-256
`6b4d1b6cc09b019ce3d54421dce182a47924a5e18c6d05d9174322f4d69ab159`.
These focused receipts are not a claim that the complete repository regression
or FP-3/FP-5 acceptance is finished.

## Native entity UNWIND and implicit index authority

UNWIND now carries structural node/relationship provenance from its source into
analysis and planning. It supports collected entities, explicit compatible entity
lists, CASE/coalesce, slices and identity-preserving/filtering list comprehensions.
Local binders shadow outer names; scalar/parameter collections do not gain graph
write authority. The planner shares the existing source-table proof machinery
with WITH instead of rescanning the graph or inferring identity from properties.

The initial feature run exposed two genuine runtime gaps beyond admission:

- Bounded spill retains logical identity but deliberately strips physical write
  authority. UNWIND now reacquires a stored entity against the existing
  snapshot-visible identity path, then applies current private transaction values.
  Missing identity refuses, not a best-effort scan from a user property.
- Repeated entity occurrences retained stale captured properties. Each occurrence
  now sees the owner's latest values while preserving row multiplicity and native
  pending-insert tokens. Materialized scalar lists remain their earlier values.
  SET checks deleted-entity content explicitly; counting a deleted identity stays
  permitted under the existing contract, and no row is resurrected.

The remaining original Unwind1 MERGE case exposed another real boundary: implicit
relationship DDL created endpoint identity indexes that were missing from the
statement's pre-DDL authority projection. Adoption now adds only newly activated
catalog entries, through transaction-scoped registry validation. Existing selected
stores, planning selections and cached endpoint paths are not replaced. Registry
failures propagate and all schema/index/data effects remain under the instruction's
rollback mark; there is no inner transaction or relaxed publication fence.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-entity-unwind-regression.xml` | 401 passed, zero failures/errors/skips; 72.055 s | `c60ee25049e434cdefadfc5a2dd25630543a4926b68d40f69282b587c5e57307` |
| `fp3-entity-unwind-owner-final.xml` | 372 passed, zero failures/errors/skips; 51.745 s | `f64b558082f4ab6548ae539d2193b070e8488d7a6d6a5f08a8cca062c0108249` |
| `fp3-entity-unwind-typed-qualified.xml` | Both ordinary/bounded typed-schema cases passed; zero failures/errors/skips; 1.452 s | `7bc1a42205729ad9bf030dde513239e4a1a8bef37298f6e901b6ff8dd4420dc1` |
| `fp3-entity-unwind-list12-native-final.json` | All seven original List12 cases passed; 3,890 outside selection | `0ce8ac800d51602a6622eb6eb48d100eef0d972a793a8faf3a106e92e3e1a828` |
| `fp3-entity-unwind-unwind1-native-final.json` | All 14 original Unwind1 cases passed; 3,883 outside selection | `56c2f895a509c6410bac15a0a87804653c83c0eb79abdf79f94c4f8500cc78d6` |

Every command reached terminal exit 0. Native receipts verify frozen V2 and its
V1 predecessor without fixture, query, result or ledger changes. They close
List12#0001/#0002 and Unwind1#0006/#0012, all four corresponding blockers in the
FP-2 baseline. The earlier feature receipt records the spill/repeated-identity
failures before their fixes and is not qualification evidence.

Feature/regression evidence includes typed and polymorphic source bindings,
different tables with equal user properties, node rematch, relationship type and
endpoint preservation, SET/DELETE, same-instruction pending entities, repeated
identities, local shadowing and outer captures, scalar/unproven write refusal,
ordinary/bounded memory, late-write whole-instruction rollback, durable reopen and
store verification. Implicit-DDL tests cover repeated MERGE idempotence, late
failure/retry, injected authority refusal and a structural proof that captured
stores are retained while omitted existing authority is not reopened. The final
selection adds independent reader snapshot/writer conflict tests and the full
query-engine plus owner-landing/cache regressions. The selections overlap; their
counts are not summed as unique tests or a full-repository qualification.
The final typed-schema check independently proves successful updates plus
primary-key-collision and wrong-column-type refusals, instruction rollback and
durable reopen under both memory modes. Its first invocation used invalid inline
PRIMARY KEY fixture syntax; the fixture now uses the existing table-level
`PRIMARY KEY(id)` syntax. No production admission was changed to accommodate it.

[Language/API consumption](../QUERY_LANGUAGE.md#native-entity-collections-and-unwind),
entity results, composition, roadmap and compatibility documentation are updated.
No new configuration/public DTO/storage format is introduced. No Pulse install,
production mutation, commit, push or release was performed. The full FP-2/FP-3
packages, temporal and remaining FP-4–8 work, and Pulse qualification stay open.

## Initial composed-read increment (historical)

Standalone label-free node reads now compose with multiple MATCH clauses and
patterns, UNWIND, inline property maps, WITH aliases/star, OPTIONAL MATCH and
returning read subqueries. Anonymous node patterns preserve multiplicity without
publishing a variable. The planner reuses the existing AllNodesScan input operator
and its snapshot/private-overlay behavior; no new transaction, WAL or storage
format was introduced.

The former statement-wide one-shape guard is narrowed to its remaining dynamic
write restriction. Detection follows clause order and discarded/reintroduced
names, so dropping a typed binding cannot bypass that restriction. A rematch of
an already-bound polymorphic node is a binding/null/table predicate, not another
scan. A typed label on that rematch filters the binding's table. Inline maps lower
to ordinary equality terms and validate referenced property families even when
the source contains no explicit Property expression. Missing properties return
NULL; incompatible declared types remain a planning refusal.

Polymorphic binding metadata crosses WITH aliases and explicit read-subquery
imports/exports. Internal bindings retain the physical table/record identity;
same user primary keys in different tables do not collapse in DISTINCT/grouping.
That initial increment did **not** implement the detached public identity model;
subsequent sections record its native integration and UNION progress.

## Independent tests and original scenarios

| Local receipt | Coverage | Result |
|---|---|---|
| `fp3-composed-polymorphic-regression.xml` | New composed-read matrix, existing polymorphic/planner tests, WITH star and public cursors | 179 passed; zero failures/errors/skips; 18.236 s |
| `fp3-composed-read-boundaries.xml` | Expanded identity/rematch tests, correlated/root optionals, optional aggregation, UNION, deferred projection, public query boundaries and write conflicts | 310 passed; zero failures/errors/skips; 68.796 s |
| `fp3-composed-conversions.json` | Original 47-case `expressions/typeConversion/` family, verified frozen ledger | 21 original passes, four fixture-adapted passes, zero failures, 22 selected fixture blockers; 3,850 cases outside the selection |

The JSON retains all 3,897 original cases at pinned upstream revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`. SHA-256:
`b19926aa4459c7338a19ab1c2b11a77a53ba645beddb350f46af276abc8f7b65`.
Reproduce using the stateful verified-ledger runner with
`--feature-prefix expressions/typeConversion/ --infer-fixture-schema`.
The three conversion queries blocked first by WITH star and then by the
polymorphic multi-MATCH guard now pass **with explicit inferred fixture schema**.
Their query text and expected values are unchanged. They are not relabeled as
unadapted upstream passes; 22 unlabeled/multilabel fixture blockers remain visible.

Tests independently check:

- Exact cross-product multiplicities, null extension of a complete multi-pattern
  optional clause, inline missing keys, compatible types and before-row refusal
  of incompatible columns.
- Repeated bound-node patterns/aliases/imports without a second scan; discarded
  names receive fresh scope identities. An optional NULL cannot rebind as a scan.
- Same-ID nodes from different tables remain distinct; group counts are exact.
- Typed anchor index access remains present in a composed plan. New polymorphic
  variables still use an all-node scan; no cross-table index speedup is claimed.
- The writer's private insert/update/delete overlay, an independent old read
  transaction through commit, and the new post-commit snapshot return exact rows.
- Cursor early close and a globally bounded cross product release transactions.
- Existing caller-supplied AST/analysis and dynamic-write refusal tests remain
  enforced. Older tests that intentionally refused the newly supported read
  shapes now assert supported planning/results; write refusals are retained.

The initial focused run had one test harness import error for a nonexistent
control API; the final test uses the existing `connect(max_intermediate_rows=3)`
contract and verifies the expected budget failure and transaction cleanup.

## Internal entity identity foundation

`domain/query/entity_identity.py` adds immutable, capability-free identity and
provenance values. This is an internal foundation only: it is not root-exported
and is not yet wired into native query results or cursors. The public result
contract has not changed in this checkpoint.

Identity distinguishes database UUID, table ID, entity kind and record
incarnation. Native lifecycle tests establish that updates retain a RecordId,
while deleting and recreating the same primary key allocates a different one,
including within one transaction and across reopen. Independent databases with
the same local RecordId remain distinct. Snapshot/version LSN and observed schema
version are separate provenance, not identity. JSON metadata uses decimal strings
for wide identifiers, avoiding JavaScript integer precision loss.

The foundation relies on the current append-only table catalog and monotonic
record allocator. Future table-ID reuse requires an explicit incarnation policy.
Provisional identities have a separate opaque nonce namespace; allocating those
nonces in the owning transaction and integrating detached immutable properties
remain pending. No transaction, pending-row reference or write authority is
exposed. Tests reject identity DTOs as query parameters before write effects and
reject provenance claiming a committed version newer than its snapshot.

Local receipt `fp3-identity-storage-regression.xml`: **324 passed**, zero
failures/errors/skips, 7.105 s; covers the identity foundation, record-ID wiring,
catalog and heap-store regressions. This does not constitute FP-3 acceptance.

The combined pre-push checkpoint `fp2-fp3-push-checkpoint.xml` reports **395
passed**, zero failures/errors/skips, 79.731 s. It reruns the changed numeric,
range, membership, WITH star, composed polymorphic, identity, node-seek, planner
and TCK error-mapping tests together. Documentation validation, changed-file
Ruff checks and whitespace checks also pass. This is targeted regression, not a
new full-repository or Pulse acceptance run.

## Detached value model (internal; native result wiring pending)

`domain/query/entity_values.py` now defines node, relationship and ordered-path
observations on the qualified identity foundation. [Model and JSON contract](../ENTITY_VALUES.md).
Properties are deep-owned, node/relationship equality and hashing ignore
observation payload, endpoints reject foreign databases, and path validation
checks cardinality, adjacency and a single read snapshot. Zero-hop, reverse and
cyclic walks are represented without confusing repeated nodes with the executor's
relationship-uniqueness rule. JSON preserves wide IDs, INT64 values and current
domain scalar types through explicit tags; nested-map tags cannot collide with
ordinary user keys.

Tests include independent expected JSON, mutation of source containers/exported
dictionaries, exact list/map/depth bounds, pending metadata, malformed paths,
parameter rejection before write effects, and materialization of values read
from a real database before/after a concurrent commit and after database close.
The native fixture uses actual snapshot LSNs and commit reports, not fabricated
version metadata. It explicitly constructs the internal DTOs: **native query
output has not been switched**, and this evidence must not be used to claim it has.

Final local receipt `fp3-entity-materialization-boundaries.xml`: **287 passed**,
zero failures/errors/skips, 20.724 s. The selection combines the new value model,
identity lifecycle, composed polymorphic reads, prior polymorphic tests and public
collaborator-boundary regressions. A hostile mapping-proxy fixture lies about its
length; materialization refuses after enumerating 257 entries rather than trusting
that length or exhausting the source. Documentation, changed-file Ruff and diff
whitespace checks pass. No full-repository or Pulse acceptance claim is made here.

## Native node/relationship output (subsequent increment)

The earlier internal-only status above is superseded for nodes and relationships:
native execute/cursor output now uses the root-exported `NodeValue` and
`RelationshipValue`, also nested in result lists/maps. `EntityIdentity`,
`EntityProvenance` and the result-only `QueryValue` alias are public imports.
[Contracts, constructors/fields, migration and JSON](../ENTITY_VALUES.md).
Paths remain on their existing native representation pending the next integration.

Assembly injects the database UUID and an independent handle namespace. Keyed
digests qualify authentic transaction-local pending references without exposing
tokens; subsequent statements retain the same provisional identity. Returned
inserts are staged privately under the still-active statement rollback mark before
conversion. Engine/public conversion failures roll back those effects; prior
successful statements remain intact and survive reopen. No WAL/publication rule
or cross-participant visibility contract changed.

Native tests cover typed/polymorphic equivalence, nested aliases, optional NULL,
equal primary keys in different tables, source/target identity for edges before
and after commit, old cursors concurrent with committed updates, distinct pending
identities from independent handles, and create/delete/recreate observations.
Reduced overlays are shared by table and actual intent/held-row revision, avoiding
an O(transaction writes) reduction for each returned entity.

Spill regression exposed a real earlier defect: projected SortRows detached entity
bindings to integers. Sorting now uses the operator row codec, retaining source
version and pending provenance. Pending metadata resolves to an existing authentic
transaction reference. A deliberately forged future version in spill fails closed.
`RETURN DISTINCT n ORDER BY n.id` is admitted because the whole entity was retained;
dropped-input-property refusals are preserved. Three old polymorphic result tests
were updated from mutable maps to explicit identity and immutable-property checks.
An intermediate cleanup edit caused seven NameError failures in aggregate spill;
it was corrected before the final grouped regression, not waived.

| Final local receipt | Scope | Result |
|---|---|---|
| `fp3-native-output-grouped.xml` | Cursor, sort/aggregate/DISTINCT spill, polymorphic/composed reads, planner, hostile public boundaries, detached/native identity and independent TCK oracles | 491 passed; zero failures/errors/skips; 35.942 s |
| `fp3-native-output-transaction-boundaries.xml` | Final public annotations, native entities, forged future spill version, late public conversion rollback, cursors, pending row intents and TCK adapter | 74 passed; zero failures/errors/skips; 8.274 s |

The frozen original `clauses/return/Return2.feature` was also run unchanged through
the verified-ledger stateful backend: **3 original passes, 1 fixture-adapted pass,
3 failures and 11 selected not-run cases**, plus 3,879 cases outside selection.
Receipt `fp3-native-return2.json`, SHA-256
`616350b79d544559fe9c3081e02abbdd8e8d69035989300b21cf171d757d60f1`.
Failures #0012/#0013 still require generalized unlabeled relationship endpoints
(the native planner specifically refuses `(n)` in this shape), while #0016 needs
dynamic polymorphic DELETE and runtime deleted-entity access semantics. They stay
assigned to FP-3; no case/query/expected answer or exclusion was changed.
Native node/edge DTOs are translated into independent reference literals by the
backend, not compared using identity equality. Tests deliberately use two equal
entity identities with different properties to prove a wrong result still fails.

### Pulse consumer mapping (not yet migrated or installed)

Read-only inspection of the current Community worktree identifies
`community/adapters/grafx_graph_transaction.py::_normalize_value` as the shared
normalization seam. It currently handles scalar/container values but would leave
the new DTOs unchanged. `grafx_cypher_executor.py::pulse_value`, `_envelope` and
transactional read/batch consumers depend on it. These Community adapters must
translate native observations into the agreed provider-neutral Pulse result/JSON
contract, with paired tests. `project_path_sequences` currently expects `_NODES`
and `_RELS`; its migration must be coordinated with native path output.
Logical transfer and scalar-projection paths need regression but are not assumed
to require an entity-output rewrite. No Grafx import/condition belongs in Core.
No production installation, graph data or running Pulse process was changed.

## Development checkpoint: native entity UNION (2026-09-11)

This checkpoint includes native node/relationship UNION values, nested entity
values, entity-preserving aggregate spill, and entity metadata exported by
returning UNION subqueries. The focused receipt `fp3-entity-union-focused.xml`
records 26 passed tests, including in-memory and spill variants.

The fresh combined checkpoint receipt `fp3-commit-checkpoint.xml` records
**165 passed, 2 failed, zero errors/skips (45.901 s)**. The failures are
`test_combined_alias_depth_is_refused_by_a_typed_budget_before_the_stack` and
`test_alias_expansion_accepts_the_ceiling_and_refuses_the_next_level`, both in
`tests/query/test_union.py`: the expected alias expansion budget refusal is
missing. They were open regressions at that checkpoint, not exclusions or waived
requirements; the follow-up below fixes both.
Documentation checks, Ruff on changed Python files and `git diff --check` pass.

This is a development snapshot, not a completed FP-3 delivery or a release-ready
checkpoint. Existing descriptions of entity UNION as pending refer to acceptance;
the implementation above is present but still requires the remaining validation
and budget corrections. No full repository regression or paired Pulse acceptance
was performed for this snapshot. Pulse adapter migration described above remains
mandatory before installing these breaking entity-result changes into Pulse.

## Native UNION follow-up: alias admission and reference semantics

The former entity-refusal call also enforced the alias-expanded typing depth.
Allowing entity results accidentally removed that admission step. Planning now
expands and validates the shared typing DAG without reinstating the scalar-only
restriction. The inclusive depth ceiling and linear shared-alias traversal are
covered by the existing regression cases, unchanged.

Column mismatch now carries `different_columns_in_union` and `planning` at the
native raise site. The reference mapper requires those fields plus the exact
UNION/column classification; unproven messages or execution-phase errors do not
map to the expected compile-time code. Tests prevent scanning before refusal.

The two required `Union3` scenarios exposed a genuine policy divergence: the
former extension admitted mixed UNION/UNION ALL chains. To implement the approved
reference profile, each chain now has one duplicate policy, with an explicit
`mixed_union_composition` planning refusal. Direct AST admission also checks
right-nested trees. Different policies remain composable in separate returning
subqueries; tests verify both combinations and exact entity multiplicities.
There is no legacy toggle, changed ledger exclusion or rewritten upstream query.

All 12 original UNION-family scenarios now execute: **10 original passes,
2 typed-fixture-adapted passes, zero selected failures/not-run**. The full ledger
still accounts for 3,897 cases, including 3,885 outside this selection. Receipt
`fp3-native-union-profile.json`, SHA-256
`ff7f6892ab603adca631c9072307bf2fa48712fcf28dea7f8232c24f794c0cf8`.
The two adapted passes are not relabeled as original conformance.

Native tests additionally cover grouping after UNION exports, overlapping local
IDs, nested entities/aggregates, private updates/inserts, cursor old snapshots
through concurrent commits, and early cursor close. Memory and spill paths are
tested independently. Follow-up local receipts:

| Receipt | Scope | Result |
| --- | --- | --- |
| `fp3-union-alias-budget-fixed.xml` | Existing UNION aliases/bounds and native entity UNION | 139 passed; zero failures/errors/skips; 34.998 s |
| `fp3-union-grouped-regression.xml` | UNION/ALL, aggregate spill, optional aggregates, native identity/results and independent TCK oracles, before the mixed-chain policy correction | 302 passed; zero failures/errors/skips; 69.362 s |
| `fp3-union-profile-final.xml` | Corrected policy with UNION/spill/grouping/reference regression | 309 passed; 1 outdated mixed-chain expectation failed; zero errors/skips; 70.907 s |
| `fp3-union-public-final.xml` | Updated explicit-subquery migration examples, both mixed-chain refusals, all native entity UNION tests, cursors, composed polymorphism and hostile public boundaries | 297 passed; zero failures/errors/skips; 39.245 s |

The single intermediate failure expected the deliberately removed mixed-chain
extension. Its replacement explicitly nests the bag union before the outer set
union and retains the same result, with the raw mixed syntax separately tested
as a refusal. Both orderings pass in the final receipt. Test sets overlap; counts
must not be added as a unique test total. Ruff on all changed Python files,
documentation links/anchors/configuration/public contracts and diff whitespace
checks also pass. This is not the full repository or checkpoint-B regression.

Read-only inspection of the current Pulse Community/Core Python source found no
built-in graph query template mixing the two policies; Community relational SQL
unions are unrelated and unchanged. This is template inspection, not paired-wheel
Pulse acceptance. The public entity normalization migration above remains open.

## Native one-hop path result integration

The existing outgoing typed one-hop capture now materializes `PathValue` through
the same native entity boundary as nodes/relationships. `nodes()` and
`relationships()` return the corresponding live query bindings during expression
evaluation, then detached native entities at output. Their qualified identity,
source version, read snapshot and pending status match direct entity projections.
UNION, cursors and temporary spill retain those components; the owned public DTO
keeps no transaction/page authority. `QueryValue` and CLI tagged JSON include paths.

The former private detached-marker classes, execution-local negative path IDs and
public `_NODES`/`_RELS` conversion were removed. Metadata-named user properties
are now separate from identity/endpoint fields; their former planner ban is gone.
Native path endpoint/schema validation remains. The temporary codec verifies path
cardinality, row kinds, authenticated pending references, source-version bounds
and adjacent endpoint connections. The public boundary rebuilds components and
rejects forged/cyclic/disconnected values; path parameters remain refused.

The cross-table path fixture exposed a native creation defect:
`_write_pattern` passed all unstaged pattern rows into each table's PK check.
It now filters those candidates by table ID. A1 and B1 can coexist within one
created pattern, even when their key columns occupy different positions. A
duplicate within A remains refused; tests prove whole-statement rollback retains
earlier effects and survives commit/reopen. This does not change OCC, WAL or
concurrent uniqueness guarantees.

Local receipts (overlapping selections; not cumulative unique counts):

| Receipt | Scope | Result |
| --- | --- | --- |
| `fp3-native-path-grouped.xml` | Existing path admission, native paths/entities/UNION, aggregate spill, composed write conflicts, hostile public boundaries and TCK oracle | 523 passed; zero failures/errors/skips; 70.457 s |
| `fp3-native-path-final-boundaries.xml` | Final native path contract, malformed spill injection, public detachment and existing typed path schema/admission | 185 passed; zero failures/errors/skips; 21.102 s |

The final boundary receipt additionally covers injected disconnected/empty path
spill payloads and the refusal of context-free private path conversion. Native
tests compare qualified components, private inserts, old/new snapshots, exact
properties, immutable JSON ownership and independent reference directions.

The unchanged original `expressions/path` selection is still **5 failed,
2 selected not-run**, with 3,890 outside selection. Failures require OPTIONAL
named paths with NULL anchors, general zero/variable-length capture, and exact
compile-time errors for `length(node/relationship)`. The two fixture blockers
use one relationship type across incompatible endpoint tables. Receipt
`fp3-native-path-tck.json`, SHA-256
`12ce19e1c7a8d5829ca1ee399b61c5692e8c081da5f2c3b24ae5995ece594639`.
No query/expectation or ledger exclusion was changed. These remain FP-3 work,
not a claim that path conformance passed. The DTO supports zero/reverse/multihop
walk representation, but constructing it is not native traversal implementation.

Pulse's Community `project_path_sequences` still consumes the old path map and
must migrate alongside the shared entity normalizer before installation. The
Core remains unchanged and provider-agnostic. No installed Pulse, graph or
published package was modified.

## Unqualified composed-path checkpoint (2026-09-11)

This work-in-progress checkpoint also includes composed single-hop captures,
reverse/undirected walking, nullable optional results, path aliases and subquery
scope propagation. It is not a qualified release or a completed FP-3 delivery.
The earlier passing receipts above predate these additional changes and must not
be read as regression approval of this checkpoint.

- `fp3-composed-paths-fixed.xml`: 32 passed, zero failures/errors/skips,
  11.776 s (focused composed-path scenarios).
- `fp3-composed-path-prior-contracts.xml`: 562 tests, 87 failures, zero errors
  or skips, 64.344 s. Failures are in named-path (34), path-projection (50)
  and generic-path (3) tests. Admission expectations and any genuine regressions
  still require individual triage; these failures are not waived.
- The subsequent UNION path-type propagation and typed path-argument diagnostic
  changes have not yet received a new validating regression receipt.
- Checkpoint lint reports four F811 fixture-name redefinitions in
  `test_fp3_composed_paths.py`; documentation validation and `git diff --check`
  pass. User-facing admission documentation still needs reconciliation with the
  expanded composed-path contract before delivery.

Pulse native entity/path consumer migration and paired integration validation
remain pending. No installed runtime or production graph was changed. This
checkpoint is retained on Grafx `feature/v0.0.6`; the existing Pulse Community
and Core integration checkpoints remain on their remote `feature/v0.3.3`.

## Composed capture validation and clause trails (2026-09-11)

This increment resolves the 87 failures recorded at the preceding WIP checkpoint;
it does not retroactively qualify that commit. Fifty failures exposed a genuine
loss of hostile-AST admission when the old literal path recognizer stopped being
the gate. Native `validate_structure` now checks canonical AST classes, exact
tuple/scalar/enum fields and cycles before semantic dispatch. It never uses
caller-provided analysis as authority. Shared DAGs remain valid; tuple reuse is
checked against every field's type, and foreign metaclasses are not hashed or
compared. Literal payloads and semantic depth/resource bounds retain their own
validation. No public settings or durability protocol changed.

Other prior assertions froze the old unreadable/decorative path restriction.
They were individually migrated to exact-result tests for newly supported reads,
while wrong endpoint schemas, name collisions, malformed trees and unimplemented
ranges remain negative tests. Supplied-analysis tests now use real catalog labels,
so missing tables cannot accidentally hide a path typing/scope failure.

Captures now compose across WHERE/WITH/UNWIND, DISTINCT/order/windows, typed
OPTIONAL MATCH and returning CALL. Path-or-NULL UNION exports and imported paths
retain path-function typing. Incoming/undirected walks preserve physical endpoint
identity. Directed source inference, and undirected source inference when the
target schema identifies it, support omitted source labels. Full untyped and
ambiguous endpoint alternatives are still incomplete.

Exact-result review also found that two patterns in one MATCH reused the same
relationship. The planner now binds anonymous occurrences internally and filters
overlapping relationship identities within the clause, including variable-hop
segments. Separate MATCH clauses retain independent trails; equal local IDs in
different relationship tables are distinct. Parallel-edge fixtures prove exact
multiplicity rather than only testing that a query executes. These checks use
existing bounded expression evaluation and precede optional null extension.

Local receipts (overlapping selections, not cumulative unique counts):

| Receipt | Scope | Result |
| --- | --- | --- |
| `fp3-composed-path-regression.xml` | Named/generic/native path admission, fresh endpoints, optional aggregates and query engine | 608 passed, zero failures/errors/skips; 82.843 s |
| `fp3-composed-path-final-contract.xml` | Composed/native path contracts, UNION/subqueries, trail multiplicity, spill variants and compatibility profile | 188 passed, zero failures/errors/skips; 49.312 s |
| `fp3-composed-path-safety.xml` | Final structural boundary, budgets, memory spill, cursors, composed write conflicts and existing UNION suites | 167 passed, zero failures/errors/skips; 40.527 s |

SHA-256, in the table's order:

- `cb0d7f9a0407974d796273d766c02e0514e2274b254b1b8def4658bd355fd2c6`
- `0a270784b39480da0cd740db0a1415d27feecd005131869eab0983d86c8de6ed`
- `74a2ed0ea6c59adb3fdb74fc0e65b31673af73b10d2464392f27579c07707a27`

The unchanged original `expressions/path` selection is **1 passed, 4 failed,
2 selected not-run** (3,890 outside selection). The static `length(node)` error
now has native phase/type evidence. Remaining failures are untyped NULL optional
captures (2), zero/variable-length capture (1), and the untyped relationship
pattern blocking the expected `length(relationship)` diagnostic (1). Two fixture
setups still span incompatible endpoint tables for one relationship type. No
original query, expectation or ledger exclusion was changed. Receipt
`fp3-composed-path-tck.json`, SHA-256
`054362e248a3bdcbf9edbea9d38051543a61687d3b412f2beb194f399aacfa10`.

Ruff and documentation validation pass; the checkpoint's fixture-import lint
failure was corrected without weakening lint policy. ROADMAP, query/composition,
API, entity value, configuration, compatibility and comparison documentation now
describe the expanded contract. These receipts do not constitute a full Grafx
regression, required-profile success or paired Pulse acceptance. Consumer migration
and all remaining plan packages stay mandatory; no installed runtime was modified.

## Explicit typed variable paths and zero length (2026-09-11)

Native capture now handles explicit typed ranges with zero through 30 hops per
segment, plus concatenated segments in outgoing/incoming/undirected directions.
The executor carries qualified node/relationship bindings throughout the walk;
final `PathValue` conversion, pending identity and spill use the existing owned
entity contract. Concatenation does not duplicate junction nodes or allow a
relationship to reappear in the same trail. A written range variable is a tuple,
including `*1..1`; an unstarred relationship remains one entity. Written ranges
are rejected in CREATE/MERGE, including `*1..1`.

Zero length yields the start node and an empty relationship tuple. A previously
bound target still filters qualified identity. No relationship is traversed, so
an anchor may belong to a node table outside the relationship declaration; a
label-free zero-hop anchor enumerates all eligible node tables. Proven NULL
aliases are different: they fail a rematch rather than starting a fresh scan.
That behavior is tested through WITH aliases, returning subqueries and explicit
subquery imports. Typed OPTIONAL then null-extends the whole failed clause.

Expansion now uses a depth-first iterator stack instead of retaining a complete
breadth frontier. Existing adjacency/index views remain; this is not a claim that
all traversal memory is constant or that graph enumeration is sublinear. Traversal
depth, path/expansion quotas, intermediate/result limits and cancellation remain
authoritative. Each zero path consumes a path unit and no expansion unit. Closing
a cursor before its first expansion performs no edge probe; closing after its
first hop closes the outstanding source iterator without consuming siblings.
Unordered result order is not a shortest-path guarantee.

An independent small-graph trail enumerator exposed duplicate undirected self-loop
results. Both indexed and grouped-scan candidate sources now emit a self-loop only
once; equal IDs across different endpoint tables are not mistaken for self-loops.
Tests cover parallel edges, repeated nodes/cycles, snapshot retention during a
writer commit, owner-pending self-loops and rollback. A late arithmetic failure
after range-driven SET is observed after actual assignments ran; the whole
statement rolls back while an earlier successful statement remains committable.

Local receipts (overlapping selections, not cumulative unique counts):

| Receipt | Scope | Result |
| --- | --- | --- |
| `fp3-variable-path-regression.xml` | Initial typed ranges/oracle, named/generic/path admission, parser, query engine and query budgets | 700 passed, zero failures/errors/skips; 57.278 s |
| `fp3-variable-path-safety-final.xml` | Expanded range boundaries, implicit-bound prior contracts, batched/vector-free landing paths, native path values, write conflicts and cursors | 175 passed, zero failures/errors/skips; 79.589 s |
| `fp3-variable-path-final-verified.xml` | Final zero/NULL scope, native range contracts, composed paths, memory spill, UNION and analysis | 324 passed, zero failures/errors/skips; 75.635 s |
| `fp3-variable-path-write-observed.xml` | Additional proof that assignments actually precede the late error, memory/spill variants | 2 passed, zero failures/errors/skips; 1.255 s |

SHA-256, respectively:

- `b292923330a4c5549ac4a81fef83834d89ea39f17984bc393a4345a23bf3ec42`
- `f8ce33b36a5d03e9f1d378bd652ca424e250c5abb09badcc937e63475cbf5cdb`
- `55a35a379b96accfc1ad5edc701da37d933789b80ceee4c8dcb24f5024300805`
- `fbb590b70561a738bb0d36be07496525b2c612fd69848f6186271c00fdc08b6e`

Intermediate failures in `fp3-variable-path-safety.xml`,
`fp3-variable-path-boundaries.xml`, `fp3-variable-path-final-contract.xml` and
`fp3-variable-path-null-scope.xml` were resolved and superseded by the receipts
above. Hostile range tests retain typed refusal; formerly refused zero ranges now
have positive execution assertions, and negative bounds remain refused.

The original `expressions/path` selection still reports **1 passed, 4 failed,
2 selected not-run**; 3,890 cases are outside selection. Untyped relationship
patterns block the original zero-path/NULL/type-error scenarios; two fixtures
still span incompatible endpoint tables under one relationship type. Native typed
oracle tests are not substituted for those upstream cases. Receipt
`fp3-variable-path-tck.json`, SHA-256
`c2173a8a0199082615b8a214f34abda6f2b23362575e629c96083e9cf1dc0cf2`.

The old omitted-upper policy still normalizes a missing upper bound to 20; this
increment does not qualify it as complete enumeration. Replacing that policy with
budget-governed explicit refusal remains mandatory in FP-3. Untyped/type
alternatives, node-only named capture and other unresolved original scenarios also
remain assigned. Public docs/ROADMAP now describe this implemented typed contract
and its limits. No production Pulse runtime, graph, WAL format or concurrency
premise changed; full-profile/package and paired Pulse qualification remain open.

## Commit checkpoint validation (2026-09-11)

Before publishing this increment on `feature/v0.0.6`, a combined selection of
AST structure, variable/composed/generic/named paths, implicit bounds, parser and
TCK error-contract tests passed: **413 tests, 0 failures, 0 errors, 0 skipped**
in 63.205 seconds. Receipt: `.grafx-tmp/fp3-commit-checkpoint.xml`, SHA-256
`f9beef6e83bb41d1fb3baa52884ce8a29d15911b55449e9c3c9db6ff5d92ba52`.
Documentation validation, Ruff on changed Python files and `git diff --check`
also passed. This focused checkpoint does not replace full-profile conformance
or the outstanding paired Pulse qualification.

## Omitted-upper traversal resource contract (2026-09-11)

This increment supersedes the implicit-20 limitation recorded above. Parser/AST
and plan now preserve `upper_bound_omitted` for `*`, `*..`, `*n..` and `*0..`.
The existing fixed 30-hop limit is an execution ceiling, not a substituted
syntactic bound. EXPLAIN distinguishes omission from an explicit `*1..30`.
The unused `DEFAULT_TRAVERSAL_HOPS` constant was removed; no legacy switch or new
connection knob was introduced. The new exact-boolean field is checked at
analysis, supplied-analysis planning and execution boundaries.

At the ceiling, demand for continued enumeration probes one valid unused edge
and its visible landing under the original reader snapshot or current owner
overlay. A possible continuation raises `GrafxQueryBudgetExceeded` with
`field=max_traversal_hops`, `limit=30`, `observed=31`. A dead end or only already-used
reverse edges can finish. The probe shares expansion/path quotas and cancellation;
smaller quotas may fail first. Filtering does not waive traversal work. Explicit
LIMIT/cursor close may stop without probing unrequested tails; a blocking sort may
need complete input and still fail. All probe/stack iterators close on failure.
An error after range-driven SETs rolls back that statement, preserving earlier
successful statements under the existing transaction contract.

Evidence from the unchanged production implementation across these runs:

| Receipt in `.grafx-tmp/` | Passed | Failures/errors/skips | Seconds | Coverage |
| --- | ---: | --- | ---: | --- |
| `fp3-omitted-upper-regression.xml` | 479 | 0/0/0 | 79.190 | Native/composed/generic/named paths, AST, parser, original implicit-bound guards, cancellation/deadlines and TCK error contracts |
| `fp3-omitted-upper-safety.xml` | 92 | 0/0/0 | 47.136 | Batched/vector-free landings, cursors, query budgets, composed-write conflicts and native entity UNION |
| `fp3-omitted-upper-boundaries.xml` | 64 | 0/0/0 | 15.974 | Expanded omitted-range cases, incoming/undirected continuation, per-query probe quotas and hostile supplied-analysis admission |

These selections overlap; their counts are not a unique-test or full-profile
total. Boundary tests were added after the 479-test selection; they are covered by
the final 64-test receipt. Initial focused selection: 214 passing tests in 12.469 s.
Independent chain expectations prove 31-edge failure, exact-30 completion,
25-through-30 outputs, relationship uniqueness, snapshot retention across a writer
commit, owner deletion, LIMIT versus ORDER BY, cursor cancellation/cleanup and
rollback after instrumented observed writes. No expected-failure marker was added.

SHA-256, in table order:

- `64b99899c2013c56bcb114fa7387cbf0b6fbbdb49864f4cfa3f76c4202438690`
- `c08847f5123295e0039b5a59aa9b24e9dcd46146ed81d61cf9690dc85981e604`
- `0d9e5ed3bacdbdb203c02899a5a629c22ee892021eb00ce86cf33b5877a50df5`

Consumer mapping, inspected read-only: Pulse Community integration worktree
`a070e09`, `grafx_cypher_executor.py::_prepare`, still calls
`auto_bound_var_length_path(cleaned, MAX_TRAVERSAL_DEPTH)` before native execution.
Pulse Core `33e3a5f`, `tier_power.py`, defines that application policy at 20.
Those explicit rewritten queries retain their requested bound under this change;
they do not exercise native omission. Review the application policy during the
already-required paired Pulse migration; no engine-specific Core condition or
production installation change was made here. These reads are not Pulse runtime
acceptance evidence.

Public query/API/configuration/entity/composition/compatibility/comparison docs and
ROADMAP were updated. Documentation validator, changed-file Ruff and whitespace
checks pass. No new TCK full-profile result, package qualification, competitor
parity or Pulse integration pass is claimed from this increment.

## Node-only named capture (2026-09-11)

`MATCH p=(n:Label)`, `MATCH p=(n)` and `MATCH p=()` now capture a native
zero-edge path without requiring relationship schema. `CaptureNodePath` consumes
the ordinary node plan (including index seeks), preserves qualified identity and
snapshot, and shares path/row/cancellation budgets. It opens no new transaction.
NULL/missing anchors are not fabricated paths; OPTIONAL null-extends normally.
Aliases, WITH, returning/importing subqueries, UNION/ALL/DISTINCT, nested aggregate
results, pending-owner observations and cursors are covered. Configured memory
variants do not imply every small fixture actually spills.

The operator is explicitly admitted to the closed public-plan grammar. Tests
materialize independent plan clones and mutate one without affecting another or
later execution. The initial 35-test selection had 31 failures because that
registry entry was missing; it was corrected, not waived.

A follow-up write probe found that after SET the detached path showed the new
property but `nodes(p)[0].mark` still read the old internal binding.
`_write_assignments` now refreshes live path components with the updated versions
used for direct aliases. Tests cover node-only and one-edge paths plus observed
writes before late failure, preserving earlier successful statements on rollback.
A preliminary probe also encountered unimplemented `properties()`; that function
remains assigned below. Passing direct-access tests do not claim function support.
No original TCK query/expectation was rewritten.

| Receipt in `.grafx-tmp/` | Passed | Failures/errors/skips | Seconds | Scope |
| --- | ---: | --- | ---: | --- |
| `fp3-node-path-contract.xml` | 147 | 0/0/0 | 45.664 | Node, named and composed captures after public-plan registration |
| `fp3-node-path-safety.xml` | 188 | 0/0/0 | 51.349 | Capture, variable/omitted ranges, quotas, cursors, cancellation, AST and sealed plans; before SET fix |
| `fp3-node-path-write-final.xml` | 94 | 0/0/0 | 38.187 | Final node tests including SET observation, composed-write conflicts, pending relationship overlays and entity UNION |
| `fp3-node-path-spill-final.xml` | 63 | 0/0/0 | 23.896 | Final node tests plus existing blocking-operator spill regression; instrumented physical spill of 200 node paths |

Counts overlap; these are not full-profile totals. SHA-256 in table order:

- `e64a22a1c442f7706cfeb129895c21124003288149a3cb65a21c3e6236fdb904`
- `8172598962530a9f343794cca84a4d19946dfb7034b37e3eba6cec146e429c92`
- `14d5bc55cd9c88a9677925ef207432aea7412b7384cfcb6e9733eb9ee74aabc6`
- `3414b6b38f4a17f4d6f6bb1559f91cc1af1f272a5ca39cc65266d615fafc07dd`

The final spill test instruments actual `_Sorter._write_pair` writes, requires
more than 8,192 payload bytes to have reached disk, then independently verifies
200 descending ordinals, native one-node/zero-edge paths, one qualified identity
and no remaining reader. This is actual spill evidence, unlike budget
configuration alone. No production implementation changed between the final
write and spill runs.

Query/API/entity/configuration/composition/comparison docs and ROADMAP reflect
this increment. No new knob, storage format, WAL policy, installed Pulse change,
release or full-profile qualification is implied. The complete plan remains active.

## Native entity scalar family (2026-09-11)

Implemented `properties(node|relationship|map|NULL)`, `labels(node|NULL)` and
`type(relationship|NULL)` in the native analyzer, signature/type inference,
parameter/result metadata and executor. `properties(entity)` excludes physical
edge endpoints and NULL-valued columns; user `_ID`/`_SRC` keys remain properties.
Input maps retain explicit NULL entries. Public results are owned maps, singleton
label tuples and physical table-name strings. No callback registry, adapter query
rewrite, new option or storage format is required.

The first tests found missing entity-kind admission and lost UNION export typing;
both were corrected. Later adversarial tests exposed premature runtime-parameter
refusal in an unselected CASE arm and incomplete typing of entity/scalar lists.
Result type inference is now separate from runtime invocation checks, and an
unknown entity element cannot be discarded to invent a homogeneous primitive list.
Tests cover selected/unselected CASE, zero input rows, list selection, polymorphic
nodes, path components, imported/UNION entities and dynamic invalid types.

Property materialization shares the revision-cached owner overlay, avoiding a new
intent scan per entity. Tests prove own-SET values, removal of NULL properties,
pending nodes/relationships, unchanged independent readers, vector landing
materialization and cursor snapshot retention. A late invalid dynamic argument
after instrumented observed writes rolls back the whole statement while keeping
earlier successful statements. Mutating a returned parameter-derived map cannot
mutate the supplied parameter. New typed native error mappings require the exact
function/reason/phase fields and do not consult expected TCK answers.

| Receipt in `.grafx-tmp/` | Passed | Failures/errors/skips | Seconds | Scope |
| --- | ---: | --- | ---: | --- |
| `fp3-entity-scalars-final.xml` | 238 | 0/0/0 | 65.668 | Functions, native node paths/entity UNION, vector landings, read control and error mapping; before final heterogeneous-list inference fix |
| `fp3-entity-scalars-final-regression.xml` | 630 | 0/0/0 | 82.892 | Final function cases plus analysis, query engine, list iteration, dispatch, spill, deferred projection, entity UNION and error mappings |

Overlapping selections are not a unique-test/full-profile count. SHA-256 in order:

- `7e4511ef3d6f39fa110a09447de5fff3a268917ee21ed5fdd744c9f8f8f225e1`
- `7b5983c6076fdb5907d4eba95ddb0685812a24a454dbf1e49ac45c93d5072534`

Final pinned TCK `expressions/graph` selection: **11 original passed, 1
schema-adapted passed, 11 failed, 38 selected not-run**; 3,836 cases outside the
selection. The 38 not-run fixtures require the recorded unlabeled/multilabel model
review; they are not counted as passes. Specifically Graph9 (`properties`) has
four original passes, one schema-adapted pass, one failure and one not-run. The
remaining Graph9 NULL scenario references absent `DoesNotExist`/`NOT_THERE` tables:
a read-only reproduction in the temporary native backend confirms schema `()`
and `GrafxPlanError(field=label,value=DoesNotExist)` before function invocation.
Absent-table OPTIONAL semantics remain assigned below; no reference query,
expected result or frozen exclusion was changed. This is not full graph-function
or Cypher conformance. Receipt `fp3-entity-scalars-tck-final.json`, SHA-256
`fe7a50e612240ef6c693be5420b8f9ed5b9ddbf9e987f0235f7d4d74a6579f89`.

Public query/API/configuration/entity/composition/comparison docs and ROADMAP now
describe signatures, sparse property maps, lazy dynamic errors, isolation and
physical versus logical names. No production Pulse installation or Core-specific
dependency was changed. Documentation validation, changed-file Ruff and whitespace
checks pass; the complete functional-parity plan remains active.

## Publication checkpoint: absent-table reads in progress (historical)

The development checkpoint also preserves the initial absent-table MATCH/OPTIONAL
planner work and `ZeroHopRelationship` operator. An impossible positive-length
pattern consumes its upstream input without fabricating catalog tables; an
absent relationship type can still admit a zero-length range. This work is
**not qualified for acceptance**: focused missing-schema, type/scope, zero-hop,
public-plan, schema-cache and rollback tests remain pending. The historical TCK
receipt above predates this work and must not be treated as its validation.

Pre-commit regression of entity scalars, node paths, omitted bounds, parser and
TCK error mapping: **375 passed, 0 failures/errors/skips in 58.376 s**.
Receipt `.grafx-tmp/fp3-commit-checkpoint.xml`, SHA-256
`d0aae278e995658762fccb9dbcb5ca8660bef7b86ee47dffa7d6ef30fab336f8`.
This selection does not replace the pending absent-table tests or full parity
regression. Documentation, changed-file Ruff and whitespace checks also pass.

This checkpoint is on Grafx `feature/v0.0.6`. Pulse Community and Core remain
published on `feature/v0.3.3`; no production installation or release is included.

## Absent-table reads and zero-hop binding validation (2026-09-11)

Native positive-length MATCH over an absent named node/relationship table now
produces no matches. OPTIONAL null-extends only newly introduced bindings;
aggregates see zero mandatory input rows. No synthetic catalog object is created.
The empty operator consumes its input, retaining preceding statement writes and
their ordinary rollback boundary. Existing type/AST/name/typed-model admission
is not waived merely because the read is empty.

An absent relationship type with a zero-minimum range retains its legal zero-hop
matches via `ZeroHopRelationship`. Anchors keep normal indexed/polymorphic access;
target labels/properties and qualified bound-target identity remain required.
Native paths contain the real anchor and no edges, or preserve the prior segment
when appended. Paths consume the existing path quota with zero edge expansions.
The returned public plan is detached and cannot modify a subsequent execution.

Adversarial testing found and fixed three admission/binding problems in the
initial checkpoint: a NULL alias destination could be overwritten by an anchor;
empty table candidates lost node/relationship type information; and an absent
table could bypass multi-label model refusal. Kind metadata now survives WITH,
imports, returning subqueries and homogeneous entity/NULL UNION exports without
inventing TableDefs. A label-free scan over an empty catalog also retains node
typing. Cursor cancellation, independent-reader snapshots, schema-cache
invalidation after both node and relationship DDL, and late failure after
instrumented observed SET writes have native tests.

The grouped regression additionally found six outdated assertions from earlier
FP-3 changes: node-only named paths were expected to refuse, two AST cases
expected the old diagnostic field, self-loop counting/budget assumed duplicated
directions, and nested pending entities were expected to be integer IDs. Tests
now assert accepted path analysis, exact structural refusal, one physical
self-loop plus independent parallel-edge budget work, and immutable detached
NodeValue identity/properties. These are contract updates, not xfail waivers.
The query and architecture contracts now consistently state one self-loop match.

Pinned TCK `expressions/graph` after this work: **14 original passed, 1
schema-adapted passed, 8 failed, 38 selected not-run** (3,836 outside selection).
Graph9 properties now has five original passes, one adapted pass and one not-run;
its original absent-table NULL scenario passes without query/expectation changes.
Graph3 NULL labels and Graph4 invalid node argument also pass. The remaining
Graph6 untyped OPTIONAL on a catalog with no relationship tables belongs to the
still-open untyped-pattern work; Graph5 label-expression parsing also remains
open. Schema-free/multilabel writes remain visible in the original inventory.
No ledger exclusions or upstream source files were changed.
Receipt `.grafx-tmp/fp3-absent-tck-complete.json`, SHA-256
`48fe66d4a680decaf9a4f40b4878547c1e936e0fb372b939e41a28cb7e2f5f04`.

Final native grouped regression: **695 passed, 0 failures/errors/skips in
176.182 s**, covering the new absent-table cases, native scalars/node paths/
omitted ranges/entity UNION, polymorphic composition, optional pipelines and
landing validation, schema/plan cache, planner shapes, composed write conflicts,
owner writes and UNION variants. Receipt
`.grafx-tmp/fp3-absent-regression-final.xml`, SHA-256
`09937b388791986bbaaadbd8e4ecfd813dd93a25f1e6c96dd98148ecdb2fd526`.
The earlier six-failure grouped run is superseded by this passing run, not
counted as acceptance. This is a package-focused selection, not the full suite.
Documentation validation (39 configuration fields, 11 preserved source plans),
changed-file Ruff and whitespace validation also pass.

Public query, API, configuration, composition, comparison, architecture and
ROADMAP documentation were updated. No new configuration, persistent format,
Pulse Core dependency, production install or publication is included. This is
not FP-3 acceptance or completion of the full functional-parity plan.

## Generic untyped/type-alternative single hops (2026-09-11)

Native MATCH now accepts one-hop relationships with no explicit type or with
type alternatives, in any direction and with anonymous, typed or polymorphic
endpoints. Type names are deduplicated without collapsing physical parallel
edges. The prior `untyped_one_hop_source` literal recognizer and its hard-coded
Decision/source/target/return constants were removed, not retained as a legacy
mode. Exact immutable AST admission still rejects forged containers, subclasses
and non-boolean flags; supplied analyses cannot alter the statement's semantics.

The planner uses `TraverseAnyRelationship` over one ordinary source access plan,
with real candidate tables, endpoint schema validation and polymorphic property
metadata. Bound targets compare qualified identity and cannot be rebound from
NULL. OPTIONAL null-extends the entire failed clause, while nested/returning
subqueries, aliases, UNION, filters, grouping and windows use the same query
pipeline. `*1..1` binds a tuple, unlike an unwritten single hop. Inline
relationship property maps and generic variable ranges over multiple tables
remain pending; this increment does not claim those features.

Named single-hop segments produce native paths and concatenate with other
admitted segments. Prefix and clause-wide relationship uniqueness use physical
identity, including equal local IDs in different node/relationship tables.
Separate MATCH clauses may reuse an edge. Direction checks now verify the
anchor table and both endpoint values before admitting a candidate: scanning
several tables must not match another table's node just because its ID is equal.
Each physical undirected self-loop is emitted once.

The existing indexed-anchor/grouped-scan regime and transaction-private overlays
are retained. Traversal draws upstream input once, shares expansion/path and
intermediate-row quotas across candidate tables, and explicitly closes its
incoming/candidate iterators. Unbound polymorphic roots may still scan all node
tables; general endpoint/index access-path optimization remains a separate FP-3
requirement, not an inferred performance guarantee.

The first tests exposed the optimized correlated OPTIONAL route overwriting an
already-bound NULL destination. That route now declines bound destinations so
the generic binding-aware operator handles them. Earlier refusal-only tests
were updated to test generic admission and actual results while preserving
typed-model/unsupported-range/forged-AST refusals. The formerly refused chain of
two OPTIONAL hops is checked against full landing materialization and an
independent count: three targets times two parallel outgoing edges times two
incoming matches equals twelve, since separate clauses may reuse relationships.

Native tests exercise cross-table endpoint collisions, duplicate alternatives,
parallel/self-loop counts, zero-row OPTIONAL, node/edge/path DTOs, predicates,
bound/null targets, composed segments, imported and UNION paths, schema-cache
invalidation, pending writes/independent readers, index retention and cursor
cancellation. Instrumented forced disk spill preserves path identity and exact
ordering; an observed SET followed by a dynamic argument failure rolls back the
whole statement while retaining an earlier successful statement.

Pinned graph-function TCK selection: **15 original passed, 1 adapted passed,
7 failed, 38 selected not-run** (3,836 outside selection). The original Graph6
NULL relationship-property case now passes against an empty catalog without
rewriting its query or expectation. Receipt `.grafx-tmp/fp3-polyhop-tck.json`,
SHA-256 `832d0352befe6d954802da5f6a3f93b367d2726a4b9513249dcec52c434c8075`.
Remaining original failures and frozen model divergences are still visible;
the result is not full TCK or profile acceptance.

Query/API/configuration/entity/composition/comparison/architecture docs and
ROADMAP describe the new native path and its boundaries. No new connection
option, persistent format, Pulse dependency, installation or release is included.

Final grouped regression: **409 passed, 0 failures/errors/skips in 141.966 s**.
Selection includes generic-hop positive/negative/owner/spill tests, historical
untyped access-path/forged-AST tests, correlated OPTIONAL and unused-landing
validation, optional aggregation, absent-table cases, native entity functions,
entity UNION/node paths/polymorphic composition, plan shapes, expression dispatch
and prepared-plan cache. Receipt `.grafx-tmp/fp3-polyhop-regression-final.xml`,
SHA-256 `ab557d6e7d50757b79c15f953f0f5bace461c3b9427a029809cecfdbc9dcb42f`.
The earlier one-failure run is superseded; no expected-failure marker was added.
Documentation validation (39 configuration fields, 11 preserved source plans),
changed-file Ruff and whitespace validation pass. These are focused FP-3
regressions, not full-package/profile/Pulse acceptance.

## Heterogeneous bounded ranges (2026-09-11)

`TraverseRelationshipAlternatives` now plans named/unnamed bounded trails across
explicit relationship-type alternatives or all eligible tables. It shares the
native typed depth-first engine instead of introducing a second traversal
semantics. Real schema endpoints determine reachability at every depth; the final
target label/property is only a completed-path filter, not an intermediate-node
restriction. Anonymous/polymorphic starts, either direction, zero hops, repeated
nodes, parallel edges and single physical self-loops retain qualified identity.
Captured prefixes and clause-wide relationship exclusion prevent edge reuse.

Static selection includes the extra schema depth needed for omitted-upper
completeness: an edge type first reachable at hop 31 must not disappear from the
probe at a 30-hop ceiling. A 32-node-table/31-relationship-table fixture verifies
this case, with equal local record IDs across tables. Explicit bounds, streaming
LIMIT, blocking ORDER BY, owner deletion/rollback and independent-reader probes
are covered separately. No silent upper-bound truncation or new option is added.

Both range operators explicitly close the input, candidate, probe and active DFS
iterators. Typed batching/vector-free eligibility is not widened to the new plan
class; unsupported projection proofs fall back to full value materialization.
The existing shared intermediate/expansion/path/value/memory limits and statement
rollback boundary remain unchanged.

A recursive independent oracle enumerates small-graph edge subsets and compares
full ordered node/edge signatures as bags for outgoing, incoming and undirected
ranges, zero/positive minima, duplicate/missing alternatives, cycles and parallel
edges. Native tests also cover bound and NULL targets, prefixes, list element
metadata, owner overlays, cancellation, indexed anchors, detached plans,
instrumented spill and observed-write late-failure rollback.

The tests exposed two metadata defects, now corrected: an explicitly typed edge
could lose its table proof merely because its source was polymorphic (blocking
existing SET/DELETE), and range lists could be exported from a subquery as if
they were single entities. Explicit single-edge table proofs are preserved;
range list types now survive aliases, imported/returning subqueries and proved
list-or-NULL UNION outputs. Invalid `type(list)` fails during planning, while
valid list exports retain native relationship elements. A historical untyped
`*1..2` refusal assertion is replaced by bounded-range admission, not an xfail.

The pinned `expressions/path` selection has **4 original passes, 1 adapted pass,
0 failures and 2 selected not-run** (3,890 cases outside selection). Receipt
`.grafx-tmp/fp3-multirange-tck-paths.json`, SHA-256
`1ec0c08a21ff766f088b468717211349fa90cb48f677b124c7d6958891332b5b`.
The two not-run cases are **required**, not declared divergences:
`expressions/path/Path2.feature#0001` and `#0002` use the same `REL` type for
different endpoint-table pairs. The native fixture adapter still refuses this
schema. Supporting multiple physical type alternatives is not the same as
supporting one relationship type across several endpoint pairs; the remaining
native schema/type-identity design is retained in FP-3, with no ledger exemption
or rewritten expected result. These counts do not establish full path/profile
acceptance.

Public query, API/EXPLAIN, configuration, entity, composition, comparison and
architecture contracts and ROADMAP were updated. No persistent format, Pulse
installation, core-specific dependency or release was changed in this increment.

| Receipt in `.grafx-tmp/` | Passed | Failures/errors/skips | Seconds | Scope |
| --- | ---: | --- | ---: | --- |
| `fp3-multirange-regression-final.xml` | 398 | 0/0/0 | 195.385 | Final heterogeneous oracle/limits/list/spill cases and preceding omitted ranges, single hops, native node/entity paths, scalar functions, optional pipelines, plan cache and untyped admission |
| `fp3-multirange-typed-safety.xml` | 236 | 0/0/0 | 123.934 | Existing typed variable/composed/native paths, vector-free/batched landing proofs, owner writes, composed write conflicts and endpoint indexes |

SHA-256 in table order:

- `8a6c9fa3061e26596e6e56ac6f41ac250fd358e4545d17d1ab485d31d6b898d4`
- `323421024075c0e89ef9d43e95f06b54f7b3041a363d69061ec8922328201efe`

The earlier initialization indentation error, owner/list-metadata failures and
outdated refusal assertion are superseded by the passing final runs, not counted
as acceptance. Documentation validation (39 configuration fields, 11 preserved
source plans), changed-file Ruff and whitespace checks pass. Full FP-3/profile,
package-wide and coordinated Pulse acceptance remain outstanding.

## Logical relationship-type catalog foundation (2026-09-11)

The required Path2 #0001/#0002 endpoint-pair gap now has a native catalog
foundation, not a test-adapter renaming workaround. Frozen `RelationshipTypeDef`
values map one logical type to distinct fixed-endpoint physical relationship
tables. The existing physical IDs, heap layout and endpoint-index authority stay
separate. Group names/membership, complete endpoint/property-schema validation,
exclusive member ownership and isolated catalog copies are implemented.

The catalog-v2 extension has required bit 19 (`relationship_types_v1`), canonical
ordering, bounded counts and complete checksummed coverage. Ordinary catalogs
retain their previous encoding. Invalid configuration leaves the catalog and its
serialized memo unchanged. Invalid persisted groups refuse as corruption even
with valid recalculated checksums. A simulated older capability inventory refuses
before interpreting the extension; installed-version admission remains pending.

Native committed page-image replay preserves grouping and is repeatable. Without
COMMIT there are no group effects. Invalid group images and transitions that
remove/rename/reassign an established group or change physical member identity or
endpoints refuse before any page application. Valid later snapshots may add new
disjoint groups. This proof covers consecutive snapshots in the selected WAL
range, not an invented baseline outside that range.

At this historical foundation checkpoint the public feature was **not enabled**.
DDL/member creation and index attachment,
capability publication with existing participants, query/write type resolution,
logical `type(r)`/detached values, group-wide schema changes, historical/copy/CLI
consumption and isolated Pulse integration must be connected and qualified.
The two required upstream scenarios remain not-run; no expected results or frozen
ledger exclusions changed. The exact implemented internal contracts and remaining
native integration are in
[RELATIONSHIP_TYPES_V1](../architecture/RELATIONSHIP_TYPES_V1.md).

Final grouped regression: **480 passed, 0 failures, 0 errors, 0 skips**, 43.464 s.
Receipt `.grafx-tmp/fp3-relationship-catalog-acceptance.xml`, SHA-256
`9d8a69fa759fd113f0e7762e39ee325ca1eae6c22cd3e956a2df7c2d25141c3c`.
The selection includes the new independent byte/identity/corruption/transition
oracles, catalog v1/v2/copy/store, complete-image preflight, catalog/commit-state
coactivation, existing CommitRedo/native journal replay, DDL copy and nullable
column API regressions. This is not the complete Grafx or frozen-profile suite.
Changed-file Ruff, whitespace and documentation checks pass. No configuration
knob, installed application, production store, release or public parity claim
changed.

## Native relationship-group integration and CREATE references

The catalog foundation is now connected to native `CREATE REL TABLE GROUP`,
atomic physical member/index creation, schema capability publication, detached
catalog views, MATCH/OPTIONAL/ranges, statically bound CREATE/MERGE endpoints and
single-member SET/DELETE. Logical `type(r)` and detached edge labels preserve
qualified physical identities. Migration previews copy the complete native
catalog. Existing-participant schema refresh, late index-attachment failure,
reopen, verification and durable statement rollback have focused tests.

Group-wide schema evolution and transfer/history/projection integration are not
complete. Logical export and copy of selected grouped edges currently refuse
before artifact publication, avoiding silent metadata loss; those refusals are
not claimed as feature completion. See the [group contract](../architecture/RELATIONSHIP_TYPES_V1.md).

Group integration receipts (before the subsequent NaN admission change):

- `fp3-groups-ddl-integration-final.xml`: 271 passed, no failures/errors/skips,
  74.410 s; SHA-256 `b3ea0f9a31b14aef8e90a290edb2d66d5c7d28993edaab06863838f0f1407fc5`.
- `fp3-groups-query-boundaries.xml`: 700 passed, no failures/errors/skips,
  179.507 s; SHA-256 `c169c65d735de6a36a2fa7baa33b6294ab016e6ef92b91a447c2be8a1769b147`.
- `fp3-groups-tck-paths.json`: all seven selected path cases pass, four original
  and three explicitly schema-adapted; no selected failure/not-run. SHA-256
  `cd93499c11309d5f03692ee2af9b1c1fe4bad735339fc00fdcda7d12f33048bd`.

The required Path2 #0001/#0002 now execute original queries and expectations with
explicit native group DDL, not relationship-label rewrites. NaN's later storage/
interop regression also reran native group tests; broader receipts above are
historical scoped evidence, not a full current-code acceptance claim.

CREATE now recognizes node declarations earlier in the same pattern, enabling
`CREATE (n:A)-[:R]->(n)` and nonadjacent repeated nodes without creating duplicate
nodes or requiring a second label. Kind/label/property checks still apply; there
is no schema-free write or new transaction protocol. Native regression includes
incoming/outgoing self-loops, separate patterns, qualified returned endpoints,
direction-independent self-loop path equality, undirected multiplicity, reader
isolation, late edge failure, retained prior statements and durable reopen.

`fp3-created-references-regression.xml`: **172 passed**, no failures/errors/skips,
31.403 s; SHA-256 `f8b5139ed3988cac3ab2518e29be7fe29db936a612f5aadd6ffe04e4262bfd3d`.
`fp3-created-references-comparisons.json`: **46 original and 6 schema-adapted
passes**, zero selected failures/not-run; 3,845 outside selection. SHA-256
`12b4de6ea3911d2ceff51dedee6e79c860db2a5c9a5e97c31a486de0f52d7968`.
This supersedes the Comparison1 #0040 fixture failure recorded in the NaN
checkpoint without changing the source query, expectations or frozen ledger.

## Current expression-owner diagnostic

Historical diagnostic before pattern/label completion: after the CREATE-reference correction, `fp3-expression-owner-diagnostic.json`
executes the 118 expression cases assigned to FP-3, retaining the full 3,897-case
inventory. SHA-256:
`f5ea001c2225648b5527512b38e45b9092b966109c88bc52bf8b19579d6d5cc6`.
Observed: 23 original passes, four schema-adapted passes, 47 failures and 44
selected not-run (3,779 additional cases outside the selection).

Joining by exact IDs against the unchanged frozen ledger gives **69 required
cases: 23 original passes, four adapted passes, 42 failures and zero not-run**.
The remaining 49 selected cases are existing declared model divergences, not new
waivers. This is diagnostic evidence, not profile acceptance.

The next native implementation front is pattern predicates: 40 required failures
belong to expressions/pattern, including positive syntax, undefined-variable
phases and invalid-argument checking. They require reusable expression/scoping/
traversal semantics, not rewriting the original queries into application templates.
The other required gaps are Graph5 #0009 (label predicate syntax on an OPTIONAL
NULL) and Graph3 #0004 (schema admission for a write-under-test without setup DDL).
For Graph3, a separate native probe with explicit Person/Dog/OWNS schema already
returns the expected `labels(n)`; the report's failure is not proof of a native
labels evaluator defect. The runner must record any action-schema adaptation
and preserve original effects/errors; it must not silently count it as upstream
conformance. Full clause-owner diagnostics remain due as FP-3 advances.

## Existential pattern predicates

Pattern1's native implementation now has an immutable `PatternPredicate` AST,
bounded parser lookahead that distinguishes arrows from arithmetic, incoming-only
named-variable scope, exact negative error evidence and preplanned correlated
read descriptors. Ordinary traversal supplies directions, alternatives/ranges,
node conditions, qualified endpoints and relationship identity restrictions.
Existence stops at one witness, not a materialized list of paths; false requires
successful exhaustion. NULL references stay NULL. The expression shares the
caller's snapshot, write overlay, index authority, traversal counters and query
control. Iterators and argument slots close on success/failure/early witness.
There is no runtime query-string translation or separate graph transaction.

WITH WHERE, admitted returning read subqueries and OPTIONAL's pre-null-extension
filter use the same contract. OPTIONAL clauses containing these predicates select
the full correlated plan, not the specialized one-hop predicate path. Ordinary
OPTIONAL without a WHERE retains its existing path. Public plan cloning accepts
only exact immutable syntax classes; internal subplan/argument authority is not
exported. Raw pattern projections remain syntax errors. Pattern comprehensions,
label predicates and the remaining FP-3 integration requirements are not claimed
complete by this predicate feature.

`fp3-pattern-predicate-tck.json`: **39/39 selected passed**, with **19 original and
20 explicitly schema-adapted** passes, zero selected failures/not-run, 3,858
outside selection. SHA-256:
`25a47253bbcbca2ed1c4b0bd64afeeb6c8220d34ce76fbe24f07f890eb3cc58f`.
Queries, answers, negative phases and the frozen case ledger are unchanged.

`fp3-pattern-predicate-native.xml`: **354 passed**, zero failures/errors/skips,
29.031 s; parser/analyzer/group/feature and error mapping coverage at the first
checkpoint. SHA-256:
`e4035196f6bb709a1acb3511c923a84c2060bb74dc82f9a8bc7e6e6beda36896`.
The later execution/public-boundary regression supersedes preliminary test
failures: a field-name mistake in an adversarial AST test, an outer-anchor budget
assumption (now isolated before predicate evaluation), and the OPTIONAL guard's
missing-None check. None was hidden by an xfail, skip or weaker expected result.

Final execution/public-boundary receipt:
`fp3-pattern-predicate-execution-final.xml`, **496 passed**, zero failures/errors/
skips, 36.385 s; SHA-256
`c5765e6b70df906686f6f2ed21a2a4e3f1d9f0b17859ca2b9818f06dfc9ab082`.
The selection covers the feature's positive/negative cases, bound relationship
identity, NULL/OPTIONAL ordering, pending-write isolation, prepared-plan reuse
after commit, shared expansion quotas and lazy OR, compiled predicates, hostile
AST structure, cursors, general query execution, exact error-mapper evidence,
detached plan doors and public query boundaries. These scoped receipts overlap;
they do not replace the checkpoint/final full Grafx and isolated Pulse runs.
Ruff, documentation/configuration/API checks and whitespace validation pass.

## Pattern-comprehension syntax and lexical-scope foundation

Pattern2 implementation has begun with native immutable
`PatternComprehension(pattern, projection, predicate=None)` syntax and semantic
scopes. Named/unnamed paths, local node/edge/path names, inline property parameters,
WHERE conditions, nesting and list-local graph references are represented without
rewriting user query text. The parser preserves ordinary list comparisons such
as `[x = 1]`; a named comprehension requires an actual following relationship
pattern. Existing expression-depth and pattern/token limits remain in force.

The semantic pass binds pattern-local names in an isolated scope, retains outer
bindings, checks undefined names and incompatible entity reuse, and refuses
aggregates inside local projections/conditions. Local names do not leak through
RETURN or WITH. Expression-local lowering now rewrites node/edge/path variable
fields alongside Variable expressions, so `[x IN nodes(p) | [(x)-->(y) | y]]`
does not accidentally refer to an outer variable also spelled `x`. Repeated
list-local node references retain their first declaration's label constraints.

`free_variables()` treats names declared by the comprehension as local to its
body; whether an endpoint is correlated must be resolved using the incoming
query scope by the catalog-aware planner. It must not be inferred from body-only
free-variable inventory. This distinction is required for dependency tracking,
projection pruning, NULL propagation and plan reuse during execution integration.

**Historical syntax-only checkpoint (superseded by native execution below).** A temporary explicit planning refusal,
`pattern_comprehension_execution_pending`, prevents parsed syntax from becoming
an empty success or failing only after preceding statement writes. A native
test proves that refusal preserves earlier transaction work through commit and
reopen. It is not a frozen exclusion or the final feature contract: the guard
and its intermediate test will be replaced by actual correlated list projection
and its failure-isolation tests when execution is connected.

Integration remaining at that checkpoint: hygienic correlated descriptors for local scopes and
outer/list-bound endpoints; native path/scalar/collection projection; cumulative
materialization budgets and cancellation; NULL, ordering, grouping/DISTINCT/spill
and cursor semantics; public value/plan cloning; all original required Pattern2
cases and independent typed fixtures for the broader capability. No Pattern2
execution pass or full-profile acceptance is claimed by grammar/analysis tests.

Final scope-foundation regression: `fp3-comprehension-scope-final.xml`, **412
passed**, zero failures/errors/skips, 8.738 s. SHA-256:
`a6a212542c4bc62efe35f2f8cf51bb246fc9751f02ba0c35d5d468ccd86b79ff`.
Coverage includes syntax/round-trip, local versus free names, parameter inventory,
list-local lowering and WITH correlation, non-leaking scopes, repeated-reference
label checks, invalid aggregates/body names, ordinary list comparisons, malformed
immutable AST fields, depth limits and pre-effect execution refusal/reopen. The
same selection reruns prior pattern predicates, parser/analyzer, AST admission,
list iteration and native scalar suites. Ruff, documentation/API/configuration
and whitespace checks pass. No installation, production graph or frozen ledger
was changed, and the full parity objective remains active.

## Pattern-comprehension native execution

The temporary `pattern_comprehension_execution_pending` refusal is removed.
Hygienic native descriptors now plan local node/edge/path scopes and incoming
correlations separately. Execution uses ArgumentRows plus existing MATCH/path
operators and projection, inside the same transaction, snapshot and budgets.
Bound relationship identity is enforced instead of rebinding a fresh edge.
NULL anchors return NULL; absent matches return an empty tuple-valued list.
Results preserve multiplicity, nested scalar/entity/path values and final public
detachment. Local names do not leak or reconnect to names dropped by WITH.

Coverage includes nested comprehensions, list-local node anchors, local WHERE
with existential predicates, incoming/undirected/alternative/variable-length
patterns, WITH grouping, UNWIND and returning read subqueries. Path functions now
accept dynamically carried paths after UNWIND and validate dynamic types at use;
statically known node/relationship and scalar arguments still refuse. A historic
test expecting inferred relationship-type reads to fail was replaced by an
independent positive expectation for the capability already delivered.

Two integration defects exposed by actual execution were fixed:

- A comprehension after writes needs the existing private write/read phase before
  scanning. This is staging, not early COMMIT; a late failure rolls back all effects
  of this statement while preserving preceding successful statements.
- Outer-only table/projection proofs omitted dependencies introduced by expression
  patterns. Pattern anchors now participate in expression dependency walks so path
  results retain full properties. Queries with expression patterns conservatively
  use the existing full catalog-fenced index-authority projection until closed
  analysis covers nested scopes. No new authority cache, index bypass or stale
  global handle is introduced. Narrowing that projection remains an optimization,
  not a correctness waiver.

Both forms of expression patterns retain the existing shared traversal limits.
Comprehension elements also share the 100,000 statement list-iteration allowance.
Configured `query_memory_budget_bytes` bounds cumulative conservative list/payload
materialization across rows/nesting; repeated/shared payloads may be charged again.
The list itself does not spill. Existing downstream spill operators are unchanged.
Cancellation and start/iteration/close faults release argument slots; cleanup
failures preserve primary error evidence. Failed writes survive neither commit nor
durable reopen. No connection option or persisted format was added.

Evidence under `.grafx-tmp/`:

| Receipt | Verified result | SHA-256 |
| --- | --- | --- |
| `fp3-comprehension-consolidated.xml` | 1,004 passed, zero failures/errors/skips; 77.402 s | `ee6441adaba77ea4dc36b4348b4c4a2ce83d09070d96f87d5920455c434c0153` |
| `fp3-comprehension-faults-final.xml` | 36 passed, zero failures/errors/skips; 2.281 s; includes the final stream-creation cleanup hardening | `cb385fccc89a534b6ea0f1e5ca2d77a62fe08fc3d04a19c6348e53ad88561185` |
| `fp3-patterns-consolidated-tck.json` | Pattern1/2: 19 original passes, 25 schema-adapted passes, zero selected failures; six pre-existing model-divergence cases not run; 3,847 outside selection | `5700eb296baa944cfb89071b009ea8c5f59ad4a341c5a29edd8937fd65e64702` |

The required Pattern2 cases are #0001, #0002, #0003, #0008 and #0009; all five
pass with explicitly inferred typed fixture schemas and unchanged queries/results.
The other six remain visible under the frozen schema/model divergence policy,
not counted as successful execution. Independent typed tests exercise their
comprehension capabilities without relabeling them as upstream passes. Regressions
overlap; counts are not additive distinct coverage or full-repository acceptance.
The final 36-test run follows the consolidated run's last small cleanup change.
Full FP-3/FP-8 qualification and isolated Pulse validation remain outstanding.

## Node-label predicates and expression-owner checkpoint

Native immutable `LabelPredicate(subject, labels)` now parses consecutive static
label names as a conjunction. It evaluates through native node bindings in
projections, WHERE/WITH, CASE/list selection, grouping, comprehensions and read
subqueries. NULL propagates. Unknown labels are false for non-NULL nodes;
repeated names do not change truth and case remains significant. Escaped labels
round-trip and unaliased columns retain submitted source spelling.

Known scalar/relationship/path subjects refuse during planning. Dynamic subjects
are validated when selected, preserving lazy CASE and whole-statement rollback.
Predicates neither admit multi-label storage nor implement label writes: different
labels cannot both match a node in the typed one-label model. Relationship type
inspection remains `type(r)`, not the ignored upstream edge-label scenario. BOOL
results can be stored with ordinary property admission; commit/reopen tests prove
that doing so does not change node labels or identity. Public plan clones include
the new canonical AST class without exposing execution authority. No setting or
stored layout was added.

The remaining Graph3 #0004 diagnostic failure was schema preparation, not a
`labels()` evaluator defect. The optional typed-schema runner now admits one
CREATE-only action on an otherwise empty graph when its own explicit node labels,
edge types/endpoints and literal scalar properties determine schema. It does not
execute the action early, inspect RETURN/expected values, synthesize keys/labels,
or alter original query/effect expectations. This is recorded as **initial CREATE
schema adaptation**. Existing fixtures and multiple/other action pipelines retain
their admission; inability to infer action schema falls back to executing the
unchanged action without invented schema, not a new `not_run` exclusion.
Independent runner tests verify empty pre-action state, exact effects, no hidden
properties, durable reopen, unchanged case data and no influence from expected
answers. Existing wrong-row/effect/durability/error oracle tests rerun as well.

Evidence under `.grafx-tmp/`:

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `fp3-labels-final.xml` | 565 passed; zero failures/errors/skips; 28.225 s | `b09014e8003eb4d5ab3fdd653b7e5e7be47b7f4bfc4cfb8274b317f1191258cf` |
| `fp3-expression-owner-after-labels.json` | 118 selected FP-3 expression cases: 38 original passes, 31 adapted passes, five failures, 44 not-run; 3,779 outside selection | `9666f903be6f389bbd40115f973b31e7ef58483f0d6b34f9c6d69d77030e6332` |
| `fp3-union-owner-diagnostic.json` | All 12 FP-3-owned UNION cases pass: ten original, two adapted; 3,885 outside selection | `16f51b15b2226f1ae004043360cd69710a8b735689c43bfed6240da26f6d2a38` |

Joining those 118 IDs against the unchanged frozen ledger proves **all 69 required
FP-3 expression cases pass**, with zero required failures/not-run. The other 49
were already declared model divergences; their five failures and 44 not-run remain
visible, not renamed as passes. Graph5 #0009 (NULL label predicate) and Graph3 #0004
(CREATE followed by labels) pass with recorded schema adaptation. This is not full
FP-3 acceptance: the frozen required inventory also contains 338 MATCH, 26
MATCH/WHERE and 12 UNION cases, plus the supplemental requirements and consumer
checks. No original case query, expectation, checksum or inclusion was changed.
The separate UNION selection above is also green; it does not include the full
MATCH or MATCH/WHERE owner families.

Historical next diagnostic at that checkpoint, `fp3-match-where-owner-diagnostic.json` (SHA-256
`2e33e515a87a074fac615185687ac30ea99a4e4e5a712dd7695c245711cfc588`),
executes the 26 FP-3-owned MATCH/WHERE cases: 23 adapted passes and three required
failures, with 3,871 cases outside this selection. These are concrete remaining
requirements, not proposed exclusions; they are corrected in the next section:

- MatchWhere1 #0014: path-property refusal lacks the exact reference
  compile-time `InvalidArgumentType` classification.
- MatchWhere1 #0015: aggregate-in-WHERE refusal occurs at compile time but lacks
  the reference `InvalidAggregation` classification.
- MatchWhere5 #0004: a polymorphic landing reads `var` from STRING and INT64
  tables, comparing it to a string under OR. `_polymorphic_property_type` still
  refuses incompatible declared families instead of permitting the required
  per-row comparison/NULL semantics. This needs native typing/evaluation work,
  not a fixture or expected-result rewrite.

The full MATCH owner family has 338 required cases and is not covered by this
diagnostic. Those cases and supplemental FP-3 contracts remain in scope.

The 565-test regression also covers existing parser/analyzer/structural admission,
list and pattern expressions, detached entities, query spill and stateful runner
fault oracles. Documentation/API/configuration validation, Ruff and whitespace
checks pass. This selected evidence does not replace the later full repository
and isolated Pulse qualification. No production data or installation was changed.

## Heterogeneous property reads and native scope-error evidence

The three MATCH/WHERE blockers above are corrected without fixture/expectation
changes. `_polymorphic_property_type` retains a common-type proof when available
but otherwise leaves heterogeneous property reads dynamically typed. Each row
keeps its actual value; missing properties remain NULL. Existing comparison,
lazy boolean/CASE and runtime scalar checks determine results, rather than a
blanket cross-table family refusal. Stored-column validation is unchanged and
late arithmetic/type failures retain statement rollback and durable prior work.
Independent tests cover node/relationship properties, no-row queries, filtered
reads, incoming context/optional hops, aliases, subqueries, comprehensions,
sorting/spill regressions and strict storage admission. Previous tests expecting
the superseded blanket refusal now assert independent positive results.

MATCH/WHERE aggregate refusal uses the existing `invalid_aggregation_context`
reason with its original `field="predicate"`; other contexts retain their fields.
Path-property refusal carries `path_property_type` and a proven planning phase.
The runner maps these exact native facts to InvalidAggregation/InvalidArgumentType.
Mappings neither inspect expected answers nor guess an execution phase from
an exception class/message alone; wrong/missing-field/phase tests guard them.

The broader MATCH diagnostic exposed missing structured scope categories.
Reusing an entity binding as a different entity kind now records
`variable_type_conflict`. Captured paths reserve fresh names relative to their own
entities, earlier comma-separated patterns and incoming bindings; collisions
record `variable_already_bound`. A later node/relationship use of an already
captured path instead records `variable_type_conflict`. The intermediate
whole-clause precheck incorrectly erased that source-order distinction; it is
corrected and both orders have independent tests. The public two-field
`named_path_refusal` helper remains compatible, with
private cause metadata attached at the native semantic boundary. Static malformed
AST, path, analyzer and error-oracle tests retain their checks.

Receipts under `.grafx-tmp/` (overlapping selections, not additive/full-suite counts):

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `fp3-match-where-correction.json` | All 26 required MATCH/WHERE cases pass: two original, 24 adapted; 3,871 outside selection | `192b3e34ae316795030f2ad56067936f2ddfcaeda9130b9be644089c6d66e88d` |
| `fp3-match-contracts-final.xml` | 750 passed, zero failures/errors/skips; 97.297 s | `cb3e425b491a55dbd122f4eb754292f414c5327412ad5caccddcbcfb6a04674c` |
| `fp3-scope-errors-final.xml` | 244 passed, zero failures/errors/skips; 24.651 s; follows the final cross-pattern declaration classification | `6e4d0d5e416d63e4a7f7a27d9d6f16a796a48754186cebd60bfdb3d4e289d306` |
| `fp3-scope-order-final.xml` | 281 passed, zero failures/errors/skips; 18.743 s; source-order correction plus NaN expression/storage regression | `8463ef8c99f938fd330ee859b3e0f997d58a3ec68a08d5266cb46568ea8b4e8b` |
| `fp3-match-source-order.json` | Combined MATCH/MATCH-WHERE selection: 243 original passes, 96 adapted passes, 19 failures, 49 selected not-run; 3,490 outside selection | `280ee8af816b8f2e843f9c3899a071b5723f8ea4761ff18f836f65d776a928a6` |

Joining that last diagnostic to the frozen ledger gives **339 passes, 19 failures
and six not-run among 364 required cases**. The other 43 selected not-run cases
were already model divergences. The intermediate whole-clause category check
(`fp3-match-owner-corrected.json`, SHA-256
`9ef8e4ab025f12ccc8c573fa39e697ea84f548aacce49885c13895968a268427`)
still had 47 failures; source-order correction resolves 28, not by editing the
upstream oracle or ledger. This remains a failing diagnostic, not acceptance.

Concrete remaining requirements include parameter-map / relationship-pattern
error categories, same-clause repeated relationship-variable refusal,
bound-relationship range/path results, additional pattern grammar and write
capture. The six required fixture blockers remain Match4 #0004 (fixture schema
admission) and Match5 #0025–#0029 (non-CREATE setup). They are not newly excluded
or counted as passes. Native work and runner admission work remain separate;
the required case list is unchanged.

The selected regressions include former polymorphic APIs, query execution and
memory spill, pattern/list/label expressions, native scalars and TCK error oracles.
Documentation/API/configuration validation, Ruff and whitespace checks pass.
No new setting, storage format, publication/COMMIT rule, installation or Pulse
Core dependency was introduced. The frozen ledger and complete FP-1–FP-8 scope
remain unchanged; a green MATCH/WHERE slice is not full MATCH/profile acceptance.

## Bound relationship identity and connected-pattern uniqueness

The Match4 #0007 row-count failure was a native binding defect: a later
expansion could overwrite incoming `r` rather than constrain candidates to its
qualified identity. MATCH planning now gives the candidate a private name and
filters it against the retained incoming edge using existing entity equality.
This applies before a pattern contributes results, including typed edge-first
scans, untyped/reverse/undirected traversal and named path segments. The optional
fast path declines an already-bound edge and uses ordinary correlated ApplyRows;
only newly introduced variables are null-extended. Incompatible relationship
type constraints now produce no match without rewriting the incoming binding's
type proof. Node-label and write declaration policies are unchanged.

One connected read pattern cannot declare the same relationship variable twice.
MATCH, pattern predicates and comprehensions enforce this at native semantic
admission with `relationship_uniqueness_violation`, `field="variable"`, the
name in `value` and a proven planning phase. The TCK mapper requires those exact
facts for RelationshipUniquenessViolation; adversarial tests reject incomplete
or mismatched evidence. Comma-separated pattern reuse still obeys existing
clause-wide disjoint-edge semantics, while later MATCH clauses may reuse edges.

Independent tests prove the original 32-path multiplicity, full edge identity
inside captured paths, parallel edges, equal values in different tables, aliases,
cursor consumption, optional NULL preservation (including successive optimized
optionals), null anchors, owner-visible pending edges, failure isolation, durable
reopen and verification. No expected upstream query/result or frozen inclusion
was edited. Candidate filtering is correctness work; direct bound-edge endpoint
lookup and broader expression-produced bound relationship lists remain required
access-path/pattern work, not silently declared complete.

Receipts under `.grafx-tmp/` (overlapping selections):

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `fp3-bound-edge-focused-final.xml` | 195 passed, zero failures/errors/skips; 15.643 s; initial binding/optional/analyzer/oracle checks | `e610cc8363dc850626a2cc49cae870d0c9f71110e3106ee2724837b496f4db18` |
| `fp3-bound-edge-regression.xml` | 747 passed, zero failures/errors/skips; 170.549 s; includes final optional-fast-path and expression-pattern guards | `e6065d46f5af60b57abb3ed1daf28f5e51285a215dadac36002081bba4fe42a5` |
| `fp3-edge-error-oracles.xml` | 88 passed, zero failures/errors/skips; 4.191 s; includes final wrong/missing uniqueness-metadata checks | `3c637848a2afde527768c89df5739a15b34f08375a004414699f3c3decefb2c1` |
| `fp3-match-bound-edges.json` | 244 original passes, 97 adapted passes, 17 failures, 49 selected not-run; 3,490 outside selection | `2419d33c5ac94294dbdde14711275b6a328df53525eca6b965bc6d140c9ed7df` |

The frozen required join is **341 passes / 17 failures / six not-run among 364
required MATCH/MATCH-WHERE cases**. Match3 #0029 is now an original pass;
Match4 #0007 passes with recorded typed fixture adaptation and its original query
and count. The six required fixture blockers and 43 selected model divergences
remain visible. This diagnostic is still failing and is not FP-3 acceptance.

The regression includes previous named paths, polymorphic hops/ranges, optional
reads, predicates/comprehensions, analyzer, query engine and error mapping.
No storage format, public DTO, new configuration, COMMIT/WAL rule or participant
concurrency policy changed. Production Pulse and global installation were not
touched; the complete profile, later FP packages and paired Pulse checks remain
mandatory.

## Native relationship property-map predicates

Inline relationship maps now execute through native MATCH planning rather than
being refused. Each entry lowers to ordinary property equality on the candidate
edge; annotated ranges lower to `all` over the bound relationship tuple. An empty
map adds no condition, and zero hops satisfy a range map vacuously. Missing/NULL
properties follow existing three-valued comparison semantics. The conditions
join the complete MATCH predicate before OPTIONAL null extension, including
anonymous/typed/untyped edges, alternatives, named paths, incoming edge identity,
existential predicates and pattern comprehensions. No TCK query is rewritten.

The original named-path inline-map refusal test was replaced with independent
positive and empty-result assertions; its wrong-endpoint schema neighbors still
refuse. The first grouped run exposed only that obsolete negative expectation;
after updating the superseded contract test, the same grouped selection is green.
Native tests also cover parameters, row-dependent expressions, conjunction,
NULL/missing/empty maps, single and zero/multiple hops, both path components and
counts, reverse traversal, multiple segments, cursors and late-error rollback
with durable prior work and verification. CREATE/MERGE storage semantics are
unchanged; read-map syntax does not grant write authority.

Candidate enumeration remains in the existing traversal/scan operators and
generated `all` expressions use shared list-iteration budgets. This is not yet
property-driven traversal pruning. No new configuration, public value type,
storage layout, WAL/COMMIT rule or reader/writer policy was introduced.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-edge-maps-focused.xml` | 16 passed, zero failures/errors/skips; 5.003 s | `2eef6a98f12f9e5aedd48754173810a22252250ea2f23d483fd9ace6f51843f5` |
| `fp3-edge-maps-regression-final.xml` | 574 passed, zero failures/errors/skips; 59.002 s | `410814854740e988b59f2207f5a2698bf5658cbe071dacaf9eb67633e6453c49` |
| `fp3-match-edge-maps.json` | 244 original passes, 99 adapted passes, 15 failures, 49 selected not-run; 3,490 outside selection | `c39689d367b43b0624fa6c2aca83dc8314cca63529b4b6c17af77a69dc89bf7b` |

The grouped selection includes bound-edge identity, pattern predicates and
comprehensions, named paths, optional execution, query engine/spill, analysis
and hostile AST structure. These overlapping receipts are not full-repository
or Pulse regression. Documentation/API/configuration checks, Ruff and whitespace
checks pass.

Required MATCH/MATCH-WHERE status is now **343 passes / 15 failures / six
fixture blockers among 364 required cases**. Match3 #0005 and Match7 #0011
are schema-adapted passes using the original source queries and expectations.
The frozen ledger and its 43 selected model divergences are unchanged. Remaining
failures include parameter/relationship-pattern error categories, alternative
type spelling, reversed/empty hop bounds, bidirectional arrow syntax, bound-list
and coalesced-node bindings, plus the schema-free-looking MERGE case requiring
native/fixture analysis. These remain assigned requirements, not exclusions.
Full FP-1–FP-8 acceptance and isolated paired Pulse qualification remain pending;
no installation or production data was changed.

## Read-pattern spelling and parameter-error evidence

Bidirectional arrow spelling (`<-->` / `<-[r]->`) now parses to the existing
`Direction.UNDIRECTED`, executing through the same native traversal operators.
This does not create a reverse edge, relax a neighboring directed segment,
double a self-loop or change physical endpoint identities. The AST description
uses canonical undirected spelling. Relationship alternatives accept an optional
colon after each `|`, including escaped names; repeated alternatives retain
existing deduplicated candidate-table semantics. CREATE/MERGE still require one
type and a single direction, with no effects when admission fails.

Bare node/relationship property-map parameters remain unsupported; parsing now
reports a specific `invalid_parameter_use` reason, `field="pattern_properties"`,
the parameter name in `value` and `query_phase="planning"`. Parameters inside
explicit literal maps remain supported. The reference error mapper requires
those native facts to emit InvalidParameterUse, with wrong/missing-field/phase
tests proving it does not guess from expected errors.

The superseded parser test that rejected both arrowheads now checks canonical
undirected interpretation. Independent execution tests prove the original four-
and two-path cycle multiplicities, physical edge orientations, type alternatives,
escaped/malformed delimiters, zero/ranged traversals, maps, OPTIONAL, predicates,
comprehensions, cursor results and rejected writes preserving prior statement
work. No upstream query, expected result or ledger inclusion was changed.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-pattern-spelling-focused.xml` | 177 passed, zero failures/errors/skips; 4.974 s; initial parser/native spelling checks | `ba10c6c348884799a23d8ae20a9ff8fb16ac24cda1da4a40d288f65dff14cc1d` |
| `fp3-pattern-spelling-regression.xml` | 939 passed, zero failures/errors/skips; 162.057 s; includes final parameter-error mapping checks | `5e5a79d2c8a04a6a74361bdcb0c6d3f62be7a4299c6040bcf7a76cdada3cbd9d` |
| `fp3-match-pattern-spelling.json` | 246 original passes, 102 adapted passes, ten failures, 49 selected not-run; 3,490 outside selection | `7b5d135fb0ba441b6ebcb7192167e6d52f1d9dcfaee662ccd68d7cc49cb5afbd` |

The frozen required join is **348 passes / ten failures / six fixture blockers
among 364 required MATCH/MATCH-WHERE cases**. Match3 #0008 and Match6 #0012/#0013
are adapted passes; Match1 #0006 and Match2 #0008 are original passes. The ten
remaining failures cover bound relationship-list expressions, malformed-range
error categories, inverted/empty range semantics, a coalesced node binding and
the MERGE fixture/native admission case. No failure became an exclusion and the
43 selected model divergences remain visible.

The grouped regression covers parser, named paths, polymorphic hops/ranges,
property maps, retained edge identity, pattern predicates/comprehensions,
analysis, query execution and reference error oracles. Documentation/API/config
validation, Ruff and whitespace checks pass. These selected receipts overlap
and are not full repository/profile or paired Pulse qualification. No new
configuration, stored type, format, concurrency policy, WAL/COMMIT rule or
production installation changed. The complete FP-1–FP-8 scope remains active.

## Empty explicit intervals and range admission

Explicit inverted relationship bounds now describe an empty read interval,
including `*2..1`, `*1..0` and `*..0` (implicit lower bound one). They are distinct
from `*0..0`, which still emits an anchor path. Parser/AST retain the supplied
bounds; each bound is independently constrained to integer `0..30`, with existing
written/omitted flags validated. Planning emits a consuming false filter rather
than a traversal, preserving upstream writes, OPTIONAL null extension and normal
zero-row aggregation. Symbolic output bindings still participate in static checks.

The empty-plan shortcut validates ranges before admission, including expression-
local predicates/comprehensions and supplied AST/analysis. Negative or excessive
counts cannot evade checks by forming an empty interval. Runtime traversal plans
still require ordered safe bounds, because native empty intervals never become
traversal operators. Tests explicitly forbid both traversal execution functions
while proving empty results, zero counts and optional rows. Former parser/AST
tests expecting inverted-range rejection now test preserved bounds or substitute
an over-ceiling empty interval to retain hostile-resource coverage.

Negative bound syntax and missing `*` before `..` now carry the native
`invalid_relationship_pattern` cause, `field="hops"` and planning phase. The
reference mapper requires this evidence; wrong/missing-field/phase tests remain
negative. Over-ceiling refusal, omitted-upper completeness probes, query budgets,
relationship uniqueness and write-pattern admission are unchanged.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-empty-range-focused.xml` | 219 passed, zero failures/errors/skips; 9.168 s; initial parser/AST/native interval checks | `bc3b610d923f1fc8caa829a8ddc75ec6c7fa9d0cf1875b5bf9ded130a87f06c5` |
| `fp3-empty-range-regression.xml` | 1,007 passed, zero failures/errors/skips; 161.865 s; includes final expression-local admission and error evidence | `fd59ec4150dd6e352e8ae5fc0dd60b8e0b9a73ed311f42dbbfe46cec6dcd84cd` |
| `fp3-empty-range-public-ast.xml` | 40 passed, zero failures/errors/skips; 3.776 s; includes final supplied-analysis empty-plan positive control | `6b616c34d4eba8ce1252e81d71c76d1f591b8348f96b157edde85b3405adea96` |
| `fp3-match-empty-ranges.json` | 246 original passes, 107 adapted passes, five failures, 49 selected not-run; 3,490 outside selection | `d52f68eb2cc1214bcecab9cb3920ee7723619417424e7466bc3cc9073e8d97f5` |

The frozen required selection has **353 passes / five failures / six fixture
blockers among 364 MATCH/MATCH-WHERE cases**. Match5 #0011–#0013 and Match4
#0009/#0010 pass with explicit fixture-schema adaptation and unchanged original
queries/expectations. The five remaining failures are Match4 #0008 and Match9
#0006/#0007 (bound relationship-list expressions), Match7 #0022 (coalesced node
binding) and Match8 #0002 (MERGE admission). Six fixture blockers and 43 selected
model divergences remain visible; no ledger inclusion changed.

The grouped regression includes omitted-bound failure controls, typed and
heterogeneous ranges, variable/named paths, predicate/comprehension composition,
parser, analyzer/hostile AST, query execution and error oracles. Focused cases
prove prior statement effects, no unmatched writes, reopen and verification.
Receipts overlap and do not establish full repository/profile or Pulse acceptance.
Documentation was updated for semantics, API metadata and configuration impact;
the old 20-hop text in CONTRACT is replaced, and superseded 0.0.4 path-map prose
is explicitly historical with current contract links. Documentation, Ruff and
whitespace validation pass. No new setting, storage format, WAL/COMMIT or
concurrency rule, global installation or production data changed. The complete
FP-1–FP-8 delivery remains active.

## Expression-produced entity and relationship-list bindings

Native WITH analysis and planning now share a structural provenance proof for
same-kind coalesce/CASE result branches and literal relationship lists. NULL
alternatives do not erase a proved entity kind. The proof does not evaluate
conditions, draw randomness, inspect current rows or treat maps/scalars as graph
objects. Existing variables supply qualified candidate-table provenance; selected
entities keep their real bindings rather than being overwritten by a new scan.
One proved table can retain ordinary typed write admission; no implicit
multi-table write authority was introduced.

Written relationship ranges now use a distinct semantic `Binding.entity` value
`"relationship list"` (exported as `ENTITY_RELATIONSHIP_LIST`), not the kind of an
individual edge. Literal ordered lists and compatible coalesce/CASE selections
can constrain a subsequent `[rs*...]` by ordered entity equality, including
direction/bounds and bound endpoints. Lists cannot occupy an unstarred edge
slot. Explicit range-list imports into returning subqueries preserve that kind.
The public Binding dataclass shape and detached result DTOs remain unchanged.

Review found a consistency gap for a NULL alias renamed before use in coalesce:
analysis knew it was NULL, but planning did not retain that proof. Native
`binding_types` now carries this fact through projection; follow-up tests verify
the selected node/list is not replaced by a scan. Mixed or otherwise unproved
expression shapes remain explicit admission limitations, not new profile
exclusions; broader expression/subquery provenance remains in the full FP-3 work.

Independent tests cover non-NULL and all-NULL selection, CASE and nested
coalesce, duplicate user IDs across tables, ordered edge sequences with forward
and reverse endpoint constraints, edge aliases, cursor ownership, range-list
subquery imports, empty lists, rejection of node/list/scalar kind confusion,
typed SET, late failure isolation, durable reopen and verification.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-expression-bindings-focused.xml` | 12 passed, zero failures/errors/skips; 5.292 s; initial independent feature cases | `742c50ce316285deef691918c8ad57b4812c9ce7de55b3f78013b126cd3f6be5` |
| `fp3-expression-bindings-regression-final.xml` | 688 passed, zero failures/errors/skips; 108.145 s; includes renamed NULL alias follow-up | `166e599600ef0617b1b9fb80e46092109eeef3b15984189fe3d5f33d68e945a6` |
| `fp3-expression-bindings-union.xml` | 173 passed, zero failures/errors/skips; 37.655 s; entity UNION, UNION/ALL and composable lists | `bfd0a9354055131b988137f2645ac25e51e8d8ed53101409d77650567cb3b77f` |
| `fp3-match-expression-bindings-final.json` | 246 original passes, 111 adapted passes, one failure, 49 selected not-run; 3,490 outside selection | `023074ddfb198a05ceca56a648927b9859cdff79c6be9eb4517c87b77047f6c2` |
| `fp3-match7-null-binding-final.json` | All 26 selected Match7 cases pass after the renamed-NULL follow-up: two original, 24 adapted; 3,871 outside selection | `94cfbcb9221296ab66417688169e5b256cb8ec0a8a61262f922b2162d44bb4ab` |

The broader MATCH/MATCH-WHERE diagnostic (before the renamed-NULL follow-up)
joins to **357 passes / one failure / six fixture blockers among 364 required
cases**. Match4 #0008, Match7 #0022 and Match9 #0006/#0007 now pass their original
queries/expectations with explicit fixture adaptation. Match8 #0002 remains a
required failure; fixture blockers and 43 selected model divergences remain
visible. The follow-up reruns the affected original Match7 family rather than
claiming a second full inventory execution. No case inclusion changed.

Regressions cover previous analysis, optional/path execution, native entity
scalars and path DTOs, predicates/comprehensions, retained edge identity, query
execution/spill, plus UNION/list composition. Overlapping receipts are not a
full repository/profile or isolated Pulse qualification. Documentation/API and
configuration validation, Ruff and whitespace checks pass. No new setting,
stored format, WAL/COMMIT fence or concurrency policy changed; no production
installation/data was touched. All FP-1–FP-8 requirements remain in scope.

## Standalone node MERGE multiplicity and typed creation

Node MERGE now separates matching from insertion. Native execution emits every
matching node per input, preserving qualified identities across tables. Empty
maps/no properties match every candidate, and existing matches do not require
unspecified mandatory creation columns. Property expressions are evaluated once
per input and reused on insertion. Only an empty match set enters normal typed
creation. An unlabeled search with no matches refuses with `field="labels"`;
no anonymous table, schema-free node or inferred creation label is introduced.

Polymorphic reads may compose with standalone node MERGEs; broader dynamic
SET/DELETE and relationship-write admission remains unchanged. Matching reads
the owner's private overlay under its existing snapshot, with scan/cancellation
accounting and iterator cleanup. There is no new indexed MERGE access-path claim.
Whole-statement rollback covers earlier inputs and phases, while prior successful
statements remain intact. Runtime read admission now rejects write plans before
execution even for zero-row pipelines or mutation-free MERGE matches. Cursors
remain read-only. Database.execute remains an autocommit **read** API; native
MERGE tests and examples use an explicit write transaction.

Independent tests cover typed/untyped all-match cardinality, empty maps, omitted
creation columns, colliding IDs across tables, missing properties, per-input
creation reuse, owner inserts/updates/deletes, concurrent old readers, late
unlabeled-create failure, durable reopen/verification, nondeterministic property
evaluation exactly once, SET across all typed matches and budget-failure rollback.
The original MATCH/MERGE/OPTIONAL scenario returns six rows with zero effects.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-merge-final-controls.xml` | 136 passed; zero failures/errors/skips; 19.610 s | `c4dff232915c1ad586f46a1dd1f3f64dae311874bebb8c74588c3097263a2053` |
| `fp3-merge-regression-final.xml` | 835 passed; zero failures/errors/skips; 94.685 s | `31676fdf7194e59140e27ec51cf040e558a10e4029cb27355029cac4a5e3c9a5` |
| `fp3-match8-merge.json` | Two adapted passes; 3,895 outside/admission not-run | `c3b91bb28f0c7f46a0e6b836ff0f48c4cc680e3e3f0b4678ab451d611ee2a279` |
| `fp3-match-merge-final.json` | 246 original passes, 112 adapted passes; 49 selected not-run and 3,490 outside selection | `f3adbebf7fa840a99b9c01c7b58b0ccf3ff928c4e240d15607f169d66484994f` |

The full MATCH/MATCH-WHERE selection joins to **358 passes, zero execution
failures and six fixture blockers among 364 required cases**. Match8 #0002's
original query and expectations are unchanged. Match4 #0004 (unlabeled,
mixed-property-type setup) and Match5 #0025–#0029 (non-CREATE-only setup) remain
mandatory blockers. A decision on Match4's conflict with the declared storage
boundary was requested, not assumed; the frozen ledger is unchanged. The other
43 selected not-run cases remain explicit model divergences.

Expanded regression exposed two historical tests that still required rejecting
untyped relationship reads and zero-hop ranges, both already implemented in
earlier FP-3 work. They now positively assert native traversal selection and
exact zero/one-hop results; this is not a new TCK exclusion. The final controls
also cover the strengthened late-invocation rollback and cardinality-budget tests.
These receipts do not establish full repository/profile or isolated Pulse
qualification. No format, WAL/OCC policy, production data or installation changed.

The final grouped regression includes analyzer/planner, polymorphic composition,
owner-visible writes, write preparation, earlier blocking regressions, lifecycle,
fresh endpoints, logical relationship groups, controlled randomness, query engine,
schema transactionality, NaN admission and spill. Documentation validation,
targeted Ruff and whitespace checks pass. The full FP-1–FP-8 goal remains active.

## Input-derived fixtures and runtime-bound graph writes

The five remaining Match5 fixture blockers are resolved without query/expectation
rewrites. Fixture schema inference admits sequential typed CREATE/MATCH/DELETE
setups. MATCH derives candidate node tables from prior fixture schema and fixed
edge constraints; direct endpoint-pair correlation survives reversal, rather
than inventing a Cartesian product of endpoint labels. Scalar property reads
and proved STRING/numeric addition supply column types. Expected results,
predicate execution, random draws and arbitrary host expression evaluation are
not inference sources. Unknown/ambiguous types and unsupported setup shapes
refuse before a temporary database is created. Initial action admission is still
the separately recorded CREATE-only rule.

Native writes no longer reject a query merely because its read bindings span
tables. Bound node/relationship SET and DELETE use the actual row's schema.
CREATE/MERGE can reuse bound polymorphic endpoints. A statically unique
relationship member remains a normal typed plan; otherwise
`CreatedRelationship.table=None` and `candidate_tables` describe the closed
set of physical members. Runtime selects exactly one declared endpoint pair,
then runs the existing catalog equality, private/physical identity, visibility
and endpoint read-dependency/OCC validation. No first-member fallback or skipped
input is introduced. New relationship variables keep their real physical schema
through subsequent SET and detached output.

Independent tests cover reversal with equal table-local IDs, old-reader isolation,
late missing-pair rollback after deletes/inserts, typed mismatch in a later table,
per-member SET/MERGE, node SET/typed CREATE/DELETE/DETACH DELETE, owner-private
endpoints, durable reopen/verification and concurrent endpoint deletion causing
the writer to conflict. Detached/foreign values and NULL endpoints are explicit
authority-refusal controls, not writable handles. No storage format, COMMIT/WAL
rule, index authority or concurrency policy was relaxed.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp3-fixture-match-regression-final.xml` | 68 passed; zero failures/errors/skips; 4.003 s | `acb0fa6e2f52a28ddf60203915c5c7ea044d8325103488c46798d56e7000f1b0` |
| `fp3-dynamic-write-focused.xml` | 138 passed; zero failures/errors/skips; 35.878 s | `8136fa8651284177d1a3822c46b989a4f8a250d757295d0309b81baf166d1dd0` |
| `fp3-dynamic-write-regression-confirmed.xml` | 958 passed; zero failures/errors/skips; 180.531 s | `0b5e922bd2313853f9b184d55995580b2b53737a6f70e498b7de349e1e1cf7a7` |
| `fp3-dynamic-write-authority-final.xml` | 10 passed; zero failures/errors/skips; 18.680 s | `b7a96842ded9780466e835479f1f0cf3840205f34c39ca042066cc36b112f2be` |
| `fp3-typed-endpoints-confirmed.xml` | 53 passed; zero failures/errors/skips; 13.986 s | `9b3e838096d25dc33cffbd066456087db3ca50e3a29cc093fa9a145a42f6dab6` |
| `fp3-match5-native-writes.json` | All 29 Match5 cases adapted-pass; 3,868 outside selection | `34a193e40adc417bfc9139209a498a5f8e46ff4bdddac194a37935ee8e71fac8` |
| `fp3-match-dynamic-writes-final.json` | 246 original passes, 117 adapted passes; 44 selected not-run, 3,490 outside selection | `2e9d99a0a2d500ddcef4a854c67844b68fc4bef1896d5bbd7e3fb533cc01f103` |

The full MATCH/MATCH-WHERE V1 selection now joins to **363 required passes,
zero execution failures and one required fixture blocker** (Match4 #0004).
The user chose the broader native label-free/heterogeneous-property model for
that blocker, not a divergence. The [authorized expansion](../specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties)
is recorded in the plan and roadmap; its storage implementation and profile
successor are still pending. The current V1 ledger/source checksums are unchanged.
Its 43 selected model divergences remain historical V1 classifications, not
proof that the expanded profile is complete.

The expanded endpoint regression also identified historical tests that still
expected refusals for already-supported incoming/undirected/ranged/anonymous
reads, maps, WITH/UNWIND and absent types. They now assert exact independent rows,
direction/multiplicity and no schema mutation, while retaining invalid
relationship-list property access, wrong-kind type, read-only writes and
missing-RETURN refusals. Caller-built AST tests inspect real candidate tables
and direction without claiming an access-path optimization that is not selected.
These are native test-contract updates, not changes to original TCK expectations.

The confirmed grouped regression covers analysis/planning, polymorphic reads and
writes, standalone MERGE, created-node aliases, owner views, previous blocking
regressions, typed/fresh endpoints, endpoint indexes/locators, query budgets,
schema transactionality, query execution/spill, pending endpoint commit handling
and fixture/stateful runner selection. The authority follow-up adds detached and
foreign parameter refusals and a NULL endpoint control after broad collection;
receipts overlap and must not be summed as distinct tests. Documentation checks,
Ruff over all changed Python files and whitespace validation pass. No installed
Pulse, production data or released package changed. This is not full FP-1–FP-8,
expanded-profile or paired-wheel Pulse qualification.

## Remaining mandatory FP-3 work

- Complete remaining schema/pattern and broader path-expression cases;
  complete all hostile/public/budget and entity observation combinations.
- Broader untyped OPTIONAL and clause-level semantics required by the FP-3
  profile; the now-green expression-owner selection alone is not full
  upstream/profile qualification.
- Broader aggregate/spill and subsequent-clause combinations beyond the verified
  entity UNION cases; incompatible/missing-property and schema combinations.
- Complete logical relationship-group schema evolution and
  transfer/history/projection, remaining write shapes and the newly authorized
  flexible model. Native Path2 #0001/#0002 and Match5 #0025–#0029 are now covered above.
- Cross-table index access when compatible keys can anchor new polymorphic
  bindings, without skipping possible results or silently truncating scans.
- All required original/supplemental scenarios, package regression and affected
  isolated Pulse consumer checks at checkpoint B and final FP-8.

No production Pulse data, installation, Core adapter boundary or published
version was changed. The full goal and frozen case ledger remain unchanged.

## Authorized flexible-model foundation: persisted ANY properties

Implemented the first part of the broader model decision, not a fixture waiver.
Native node/relationship `ANY` columns retain concrete value tags across inserts,
type-changing SET, nested access, rollback, independent participant snapshots/OCC
and reopen. Schema declaration `SchemaType.ANY` is separate from `ValueType`;
catalog-v2 bit 20 is `heterogeneous_properties_v1`. Legacy/unknown readers refuse;
COMMIT proof and page-replay fences remain unchanged. Logical export/import retains
ANY schema and concrete payloads. Public plan snapshots accept only the exact
declared enum union, not arbitrary union field objects. Owner-landing accounting
uses the actual type, avoiding scalar undercharging of large ANY collections.

Native scalar/null/list/map properties work; nonfinite stored values and unchecked
embeddings refuse. Mixed-type key indexes are explicitly pending, not silently
normalized. [Complete current contract, example and matrix](../architecture/HETEROGENEOUS_PROPERTIES_V1.md).

Receipts (September 11, 2026):

| Receipt | Result | SHA-256 |
|---|---|---|
| `.grafx-tmp/any-properties-regression.xml` | 2,099 passed, zero failures/errors/skips, 115.925 s | `ef04d166becb193ca7bbe37839563a98e3b2f4a94b83d8d551e17586c4e2f2f2` |
| `.grafx-tmp/any-properties-focused-confirmed.xml` | 38 passed, zero failures/errors/skips, 3.950 s | `8035ab23d973198f65dfc23a0010ea0f0f46b191107ebe64f1bc139ac2977a50` |

The broad receipt covers all `tests/storage_core`, targeted parser/planner/query,
schema transactionality, NaN storage, polymorphic writes, spill, owner landing,
public plan snapshot, logical transfer and catalog-copy suites. The final focused
receipt also corrects the index-refusal test to use supported CREATE INDEX syntax
and assert the actual ANY capability restriction (not a parse refusal). No engine
code changed after broad regression. Counts overlap and must not be added.
Documentation links/API generation, Ruff and diff whitespace checks pass.

Still pending: native label-free creation/entity labels, automatic property growth
and undeclared relationship creation in the same statement, broader ANY indexing,
history/copy/acceleration/installed-Pulse qualification and successor profile audit.
Match4 #0004 remains required and fixture-blocked; no new TCK pass is claimed for
this foundation. FP-3/FP-6 and the full functional parity goal remain incomplete.

## Native unlabeled nodes and dynamic properties

This subsequent milestone implements the node-creation/property-growth/empty-label
items marked pending in the preceding ANY foundation. `CREATE ()` and the empty
match branch of untyped node MERGE create native heap nodes, not fixture labels or
a sidecar graph. The first creation journals schema/capability and data under the
same outer statement. Empty input creates no schema. Dynamic key addition/removal
uses the per-entity property map, preserving concrete values and typed-table rules.

`TableDef.flexible_properties` and `unlabeled` are explicit metadata protected by
catalog-v2 `flexible_graph_v1` (bit 21, depending on ANY capability bit 20).
Exact flags bytes, unknown-reader refusal before page application, malformed model
admission, commit-required/idempotent replay and model-transition refusal are
covered independently. No multi-reader/writer, OCC, WAL or publication fence was
removed. `NodeValue.label` is now `str | None`; real unlabeled nodes expose `None`
and empty `labels`, never their physical store name as a user label.

The same tests exposed and fixed an adjacent pending-entity observation defect:
`properties()` looked up every not-yet-published insert under a shared `None`
reference. It now resolves that entity's held-insert identity; independent typed
and flexible multiple-insert cases prove the properties cannot cross between nodes.

Logical transfer retains flexible node flags and values. Copy/promotion receipts
and flexible relationship transfer explicitly refuse rather than discard metadata;
they remain pending integrations. Nonempty legacy-v1 stores require the existing
explicit identity-index/catalog-v2 maintenance step; a pristine empty store may
activate inside native creation. [Complete format/API contract and example](../architecture/FLEXIBLE_GRAPH_V1.md).

Confirmation receipt `.grafx-tmp/unlabeled-native-regression-confirmed.xml`:
**2,362 passed, zero failures/errors/skips, 157.801 s**;
SHA-256 `2478ce407324cd32d2a5213c858c583eda4b697b1ee2ee02acc90faa36cba7f2`.
It covers all `tests/storage_core`; the new unlabeled node and ANY suites; schema
transactionality, parser/planner/analysis/execution, polymorphic/MERGE writes,
entity scalars and pending identities; spill and owner-landing regressions;
public plan snapshots, logical transfer, copy and TCK fixture/state/value observers.
The node tests include late-expression/schema rollback, old-reader snapshots,
independent conflicting writers, delete/recreate identity, UNION/OPTIONAL/WITH,
empty labels and native scan-based effects after reopen. The original broad run
had three stale assumptions (anonymous CREATE refusal and an incomplete table
test double); the final receipt updates those while preserving negative tests for
multiple labels and uninferred typed parameters. Documentation/API generation,
Ruff and diff whitespace validation pass.

The original Match4 #0004 is still required and not certified: its remaining
native automatic relationship-type creation and fixture admission need completion.
The wider schema-free profile successor, history/copy/acceleration/installed-Pulse
qualification and FP-3–FP-8 acceptance remain open. No production install, Pulse
data, release, branch or commit/push operation was performed by this milestone.

## Automatic flexible relationships and original Match4 closure

This checkpoint supersedes the preceding pending status for automatic relationship
creation and Match4 #0004, not the remaining full model/integration acceptance.
Undeclared relationship types create real flexible physical members for the actual
endpoint pair, with native schema journaling and endpoint identity indexes in the
same outer statement. Additional pairs append only to flexible groups. Typed
groups retain closed pairs and schema constraints. Late failure rolls back the
member/type and data together; recovery validates the prior member prefix before
applying any pages. No COMMIT, durability, snapshot or OCC rule is weakened.

Subsequent MATCH/OPTIONAL/path, pattern predicates/comprehensions and explicit
read-subquery imports resolve planned new types in the current read phase. Entity
lists preserve provenance through collect/concatenation/indexing, without giving
scalar, mixed-kind or external list elements endpoint authority. Flexible MERGE
under the existing single-edge/bound-endpoint contract matches named keys only,
retains extra keys, observes owner updates and refuses NULL pattern properties.

The original `clauses/match/Match4.feature#0004` passes with no adaptations:
unlabeled nodes genuinely store strings and integers under `var`, and native
relationships form the original 21-edge chain. No expected value, query, ledger
entry or fixture label was rewritten. The fixture runner admits wholly unlabeled
CREATE/MATCH/WITH/UNWIND setup directly, without inferring a schema from results.

Evidence (local receipts under `.grafx-tmp/`, independent runs overlap):

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `flexible-native-regression.xml` | 2,461 passed, 0 failures/errors/skips, 190.515 s | `c391214743f5a6f180db329b8e66afa1b74a2485dcbb03fa26455dc591033938` |
| `flexible-relationships-confirmed.xml` | Final MERGE/concurrency/authority follow-up: 400 passed, 0 failures/errors/skips, 46.878 s | `147fa1c9d9aefd99cea8ee2bd4725a42438879d6d3ab152d23bad44cebfadb44` |
| `flexible-match-required.json` | 364 V1-required MATCH/MATCH-WHERE passes: 247 original + 117 adapted; 0 required failures/blockers | `4caf2ba38798c112d15b07d1e413360936407264085b573198899771700fb252` |

The broad regression includes all storage-core tests, schema/query/planner/analysis,
polymorphic/merge/pattern-comprehension/owner-landing/spill suites, public plan DTOs,
logical transfer/copy and TCK observers. Follow-up tests cover flexible MERGE,
independent-reader snapshots, conflicting writers, bad extensions and replay.

The full diagnostic also executes historical V1 model divergences: 35 now pass,
15 remain unexecuted, and MatchWhere2 #0002 reaches a parser refusal for numeric
parameter names (`$1`, `$2`). Its former unlabeled-fixture blocker is gone. This
is an explicitly outstanding expanded-profile item, not a required-V1 failure
or reason to alter the immutable V1 ledger. Source-reviewed profile succession,
relationship transfer/copy/history and installed-Pulse qualification remain open.
Documentation/API generation, Ruff and diff whitespace validation pass. No full
parity, production installation or release is claimed.

## Flexible and grouped logical transfer qualification

Fresh-store logical export/import now preserves typed and automatic relationship
groups, flexible node/edge flags, empty labels and native heterogeneous values.
Artifact format 2 names physical members explicitly; normal typed/ungrouped schemas
retain format 1. Group membership is validated before destination creation and
reconstructed with target table IDs through the native schema journal. Physical
table names remain stable for mapping/index declarations, but database/table/record
identities are freshly allocated and edge endpoints are remapped. Reimports are
independent, and imported flexible groups continue growing actual endpoint pairs.

The internal attachment operation is not executable artifact query text or a new
public write door. Its transaction contains all imported schema definitions; a
fault after group attachment rolls back their table/index/catalog effects. Imports
retain private row batches, exact readback, verify/checkpoint/reopen and final
promotion. Resume now compares logical memberships and model flags as well as the
existing row/index prefix proofs. Stored schema admission is checked in the first
artifact scan, not only when staging rows; even checksummed transient NaN, NULL
bags, non-map bags and top-level NULL entries refuse before a private database opens.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `flexible-transfer-regression.xml` | 191 passed, 0 failures/errors/skips, 60.417 s | `54fdccae94fc633b426d68d63f5c29aa85a16828d856ea343da0c9fa421a1866` |
| `flexible-transfer-crash.xml` | 13 passed, 0 failures/errors/skips, 55.482 s | `eabcf83f94727b9f4a994991c0e9a2bb259797373f96162481a8d6cc7d42f855` |
| `numeric-parameters.xml` | 215 passed, 0 failures/errors/skips, 1.425 s | `f8d3ff22add168069d14e272e4c5c4bcb72046cd38d1969dd3800d11da713ec1` |
| `flexible-matchwhere2.json` | Original MatchWhere2 #0002 passed; #0001 adapted-pass; 3,895 outside selection | `2c9f2774f058f5a456b417d875c276e9b9513d694dc38a7be7ddc5a49a8b7449` |

The transfer regression includes ordinary transfer/retired-vector-space controls,
model artifacts, grouped/flexible/unlabeled/ANY queries, schema transactionality,
catalog/replay validation and public plan DTOs. Real child processes terminate at
after-batch, before/after WAL barrier and before/after promotion cuts for both typed
and flexible relationship schemas; retry resumes committed prefixes and handles
lost publication acknowledgements. Adversarial checks retain no destination on
malformed membership/model/schema/value data and reject durable group drift on resume.

The numeric-parameter parser blocker discovered by the preceding expanded-model
diagnostic is also fixed: exact decimal string keys, including leading zeros,
remain distinct; digit-leading alphanumeric names still require quoting. The
original MatchWhere2 query/parameters are unchanged. Its historical V1 divergence
is not silently rewritten. These receipts overlap and are not additive full-suite
counts. Documentation/API generation, Ruff and diff whitespace validation pass.

Existing-target copy/promotion, retained-history qualification, versioned profile
succession, wider FP-3 acceptance and FP-4–8 remain open. This milestone adds no
production installation, Pulse data mutation, release, commit or push.

## Flexible and grouped retained-history qualification

This checkpoint supersedes the retained-history pending status above. Native
system-time history now persists flexible/unlabeled schema flags and logical
relationship names with each historical schema/row record. Snapshot groups contain
only the physical members selected at that historical boundary, never members
borrowed from the current catalog. New physical members still require explicit
history activation; no past is invented. Scan and indexed reads preserve the same
model, including typed relationship groups, delete/recreate lineage and nested
heterogeneous values. Diff reports flexible changes in the lossless physical
`_properties` slot rather than flattening user keys into endpoint/schema columns.

Required capability `system_history_models_v1` (bit 22, requiring bit 15) fences
readers/writers unaware of the model trailer. Ordinary typed, ungrouped history
retains its existing encoding. The canonical trailer and immutable catalog
authority are validated before reads, replay and index/prune/compaction mutations.
Missing metadata is refused, not reconstructed or silently upgraded. Full physical
backup preserves history; explicit current-only logical export preserves the
current model but does not claim to transfer historical versions.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `flexible-history-confirmed.xml` | 216 passed, 1 failed, 0 errors/skips, 163.686 s; sole failure was the documentation test's stale count of three examples | `303ab8a5f87081f5819df955c70f0a862cab623cdba71f873ea09d2712a2b99f` |
| `flexible-history-maintenance-final.xml` | 55 passed, 0 failures/errors/skips, 42.354 s | `3e623ab7a434c89d8903b2c82d64f99fd8fe62554c1abf6541d735ea87d873bf` |
| `flexible-history-operations-confirmed.xml` | 26 passed, 0 failures/errors/skips, 51.110 s; includes execution of all four documented examples after fixing the stale expectation | `e79bef47d922cd764673ab52b9470e8435f9e81c5dfdee8d6da6459c6477f615` |

The final focused selection includes real process termination before COMMIT,
before apply, after current pages, after the history root/chunk and after COMMIT,
followed by repeated reopen/recovery and verification. It also covers independent
reader snapshots, group growth, scan/index agreement, retention, compaction,
backup/restore, current-only transfer, malformed canonical bytes, missing model
capability and refusal of maintenance with lost historical model authority.
The operations selection rechecks ordinary/adversarial history, indexes,
compaction and executable documentation. These selections overlap; they are not
additive counts or a new full-suite certification. The failed broad receipt is
retained explicitly rather than relabeled as a passing run.

Existing-target copy/promotion, versioned profile succession, wider FP-3 acceptance,
Pulse qualification and FP-4–8 remain open. No production installation, data
mutation, release, commit or push is included in this checkpoint.

## Typed-group existing-target copy qualification

Bounded capture/apply now carries `CopyTable.logical_type` for declared typed
relationship groups. Capture names selected physical members; apply resolves the
target by logical name and endpoint-table pair, allowing different member names
and IDs. Nodes still require PKs and match target schemas by name. Selected group
members must have distinct pairs and consistent property definitions; target
authority, exact schema/model flags and endpoint closure are validated before
application mutation. An ungrouped table cannot impersonate a grouped member.
Additional unselected target members need not be copied. This closes the typed
group route, not the still-required flexible/no-PK identity-remapping route.

Group-bearing packages use hash domain `grafx-copy-package-v2`, with each logical
name length-delimited and charged against the existing byte budget. An independent
digest oracle checks that ordinary typed/ungrouped package hashes remain unchanged.
This adds no native storage capability, tuning switch, implicit schema mutation,
cross-store transaction or detached writable entity handle. All inserted rows and
the request receipt share the original native target transaction and COMMIT proof.
Source history still requires explicit current-only capture and is not transplanted.

Final command (with `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps`):

```text
python -m pytest tests/api/test_grouped_catalog_copy.py tests/api/test_catalog_copy.py tests/api/test_copy_endpoint_closure.py tests/api/test_catalogs.py tests/api/test_workspace.py tests/query/test_fp3_relationship_groups.py -q --tb=short --junitxml=.grafx-tmp/grouped-copy-confirmed.xml
```

Result: **133 passed, 0 failures/errors/skips, 118.934 s**. Receipt SHA-256:
`9af4ea1abaf1b71bf05d3a5467268b99177014e419cf28032b55c210cc5a7a68`.
Coverage includes cross-store table-ID/name differences, independent read snapshots,
subset/one-hop endpoint closure, skip-node behavior, replay/reopen, adversarial
metadata/schema/identity edits, late-write rollback, current-only history policy,
ordinary copy controls and catalog/workspace session contracts. Real subprocesses
terminate before COMMIT, after its durable barrier but before heap apply, and after
COMMIT; recovery/retry proves data and receipt agree, without duplicate effects.

The earlier `grouped-copy-focused.xml` exposed the incorrect assumption that
physical member names match across stores; native logical/pair resolution fixes
that defect. `grouped-copy-regression.xml` then had 132 passes and one failure in
the new test helper, whose one-byte row bound made its aggregate-budget search
impossible. The helper was corrected and the complete selection above rerun green;
neither earlier receipt is relabeled as a passing run. Counts overlap prior evidence.

The copy guide, configuration routing, generated API reference, group architecture,
comparison, roadmap and parity plan document this increment. Documentation checks,
Ruff and whitespace checks pass. Flexible/no-PK copy, versioned profile succession,
wider FP-3 qualification, installed-Pulse validation and FP-4–8 remain outstanding.
No production install, data mutation, release, commit or push was performed.

## Flexible and no-PK existing-target copy

This increment supersedes the pending flexible/no-PK copy status above. Capture
now admits native flexible schemas and typed node tables without primary keys.
Expanded packages use the v2 digest domain and hash native model flags; malformed
bags, nonfinite stored values, duplicate source identities and multiple unlabeled
stores refuse before mutation even with recomputed checksums. A single unlabeled
target is resolved by catalog flags, and grouped edges by logical name plus the
remapped actual endpoint-table pair. Target schemas must already exist and match;
copy does not introduce implicit schema migration.

The private native execution context creates target node bindings through the
existing write operators, resolves keyed skip candidates through ordinary native
planning/reads, and supplies only target-owned bindings to edge creation. Source
record IDs are map keys, never target physical IDs or invented pending tokens.
Equal properties do not merge no-PK entities. `skip` still affects only declared
PKs; same-key receipt replay prevents duplicate copy effects. Statement/intermediate
budgets span all copied application rows and the private node-to-edge phase;
transaction quotas additionally cover the subsequent receipt statement. Native
storage validation, PK uniqueness, endpoint proofs, OCC and WAL/COMMIT remain in
force. Current-only copying into a history-enabled target creates new target
history intervals rather than transplanting the source's intervals.

Completed receipts under `.grafx-tmp/`:

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `flexible-copy-regression.xml` | 211 passed, 0 failures/errors/skips, 196.087 s | `4a709cc25048799cdffffc38c263bc8ab388a2aa63d172344455594cfa0d7f70` |
| `flexible-copy-final-features.xml` | 23 passed, 0 failures/errors/skips, 44.462 s | `46cc4dfef97afba23377de60664535899c66596abbe28a61d8007b789400ebd2` |
| `flexible-copy-boundaries-final.xml` | 295 passed, 0 failures/errors/skips, 16.340 s | `ad73475fce9b2c842f82faba2d5bfef169e60af2690b520ea0c64313294927e7` |
| `flexible-copy-traversal-confirmed.xml` | 1,302 passed, 0 failures/errors/skips, 468.900 s | `1268ab9643515bb1c0e098fc25fe37bf6724a0b9c2afb65432d255fdb066f942` |

The copy regression includes existing typed/grouped copy, catalog/workspace
sessions, endpoint closure, flexible system history, native creation, schema
rollback and PK controls. Feature cases cover equal-valued distinct nodes,
self-edges, typed/flexible endpoints with different physical names, nested values,
inert property-key syntax, partial selection, no-PK concurrent same-key requests,
snapshot isolation, skipped-endpoint deletion forcing OCC refusal, statement quota
refusal after private node staging, caught failure retaining earlier transaction
work, and repeated process-death/recovery at pre-COMMIT, pre-apply and post-COMMIT.
The complete disposable guide example executes as printed.

Architecture checks also exposed inherited namespace/`sys` accesses and missing
postponed annotations. Canonical AST classes now come from the explicit AST
export inventory, independently checked for complete dataclass coverage. Iterator
cleanup captures the original exception through lexical control flow, closes all
remaining streams even if cleanup fails, and handles a live DFS stack without
interpreter-global exception state. The existing architecture restrictions were
not weakened. Dedicated tests retain original errors/GeneratorExit/KeyboardInterrupt
and attach subsequent cleanup evidence. The boundary receipt includes these checks;
the following traversal receipt qualifies the executor changes more broadly.

That final traversal selection runs every `tests/query/test_*.py` whose filename
matches `path|travers|polymorph|implicit_bounds|fp3_|ast_structure|iterator_cleanup|query_memory_spill|read_execution_control`,
plus the complete flexible and grouped catalog-copy feature files. It covers
named/zero-hop/bounded and implicit-bound paths, polymorphic composition, entity
results, spill/resource behavior and the copy scenarios on the corrected executor.
The copy helper also closes its native PK lookup stream explicitly on all outcomes.
Documentation links, generated public API contracts, Ruff and whitespace checks
pass after these changes. No production data, installed Pulse or release was changed.

Intermediate receipts remain failures, not green evidence: the first copy run
found a missing empty row-binding argument; subsequent new-test errors concerned
public exception wrapping and the `enable_system_history()` return contract.
The architecture run first exposed the violations above and then another missing
annotation import. All were corrected before the final selections reported here.
Counts overlap and are not additive or a full parity certification. Wider model
and profile succession, installed-Pulse qualification and FP-4–8 remain open.
In particular, unknown node labels still require prior table declaration; implicit
single-label creation must be reviewed against the authorized schema-free model
before its affected V1 divergences can be accepted under a successor profile.

## Implicit single-label creation and schema-statement restoration

This increment supersedes the preceding unknown-label limitation. Native CREATE
and single-node MERGE now lazily install flexible labeled tables, without fixture
DDL inference. Already declared typed tables retain their column/PK constraints.
EXPLAIN, read APIs and zero-input writes do not install schema. Prospective plan
models resolve to exact runtime catalog models: provisional table IDs cannot be
used as authority when intervening relationships or skipped writes change the
actual allocation order. Same-statement reads, optional matches, bounded/zero-hop
paths, pattern comprehensions and returning read subqueries use those real IDs.

The new regressions exposed a real rollback defect: after an earlier successful
schema statement, a subsequent failed statement could leave its catalog in the
working cache; a later write could resurrect the refused schema. Statement marks
now capture the adopted immutable working catalog and exact schema-journal suffix.
Rollback restores both alongside staged pages and retires owner/index memos.
The public result-conversion boundary and complete executemany batch have the
same protection. Failed cleanup triggers whole-transaction refusal/rollback,
not permission to commit an unproven state. Existing WAL, OCC, publication and
multi-participant snapshot boundaries are unchanged.

Native TCK receipt: `.grafx-tmp/implicit-labels-match-native.json`, SHA-256
`03e00f113a631c1f2f3f39e510504737583f9b91a5ee673692e02357d4b2a9d0`.
Command (PYTHONPATH includes `src` and the existing optional test dependencies):

```powershell
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/implicit-labels-match-native.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/match --owner FP-3
```

No `--infer-fixture-schema` flag was used. Of 415 selected cases, **410 pass with
original fixtures/queries and five fail**; 3,482 cases are outside the selection.
All 364 selected V1-required cases pass natively, including Match4 #0004. Another
46 native passes retain their historical V1 divergence classification until the
source-reviewed successor profile is published. The five remaining failures are
Match1 #0003, Match3 #0007/#0026, Match4 #0005 and Match7 #0023: their fixtures
require multiple labels, still outside the agreed profile. This does not waive a
required case, alter the frozen V1 ledger or certify full TCK/FP-3 acceptance.

The TCK receipt precedes the subsequent schema-restoration fix; it is evidence
for native matching, not a crash/rollback qualification of the final executor.
`tests/query/test_implicit_node_labels.py` additionally covers repeated recovery
at pre-COMMIT, pre-apply and post-COMMIT process-death cuts, concurrent same-label
creation with OCC refusal, independent snapshots, late result/batch failures,
fail-closed cleanup, explicit v1 upgrade and pure/NumPy transfer/copy/history.

Completed focused integration receipt: `.grafx-tmp/implicit-labels-final.xml`,
**603 passed, zero failures/errors/skips**, 172.365 s, SHA-256
`755591edc2ddb974499897d867ee344ca2e81d79eb31ceb49ee7ef2992daa3b7`.
Selection: implicit/unlabeled nodes, flexible relationships, planner/schema
transactionality, node MERGE, composed polymorphism, relationship groups,
executemany, flexible copy/history, fixture tooling and import/package boundaries.
The preceding `implicit-labels-confirmed.xml` is not a passing receipt: one new
test called a nonexistent `Query.open()` instead of the public `Query.cursor()`;
the corrected full selection above passed. Earlier failing rollback receipts are
retained as failure evidence, not counted as successes.

The additional traversal selection completed in
`.grafx-tmp/implicit-labels-traversal-final.xml`: **1,301 passed, one failed**,
zero errors/skips, 532.112 s; SHA-256
`2bb34b75d4f61629746e53e9ec8445b0ff9646ccfa77fdd1ffb3551d581ec1dc`.
It selects query files matching
`path|travers|polymorph|implicit_bounds|fp3_|ast_structure|iterator_cleanup|query_memory_spill|read_execution_control`,
plus flexible/grouped copy tests. Its sole failure was an obsolete negative test:
`CREATE (:Missing {id:1})` through the read API used to fail during planning. Now
that implicit labels are valid, it correctly fails at the read-transaction write
guard with `GrafxTransactionStateError`. The test now checks that exact refusal
and verifies that no Missing schema was installed. No implementation was changed
after this traversal receipt; its 1,301 successful cases remain evidence, but the
receipt itself is not relabeled green. The corrected boundary is requalified with
the entire absent-table, implicit-label, schema-transactionality and executemany
files rather than repeating the unaffected long selection.

That final boundary receipt, `.grafx-tmp/implicit-labels-read-boundary-final.xml`,
completed with **93 passed, zero failures/errors/skips**, 42.848 s, SHA-256
`e4955496f4fda5720332519c0e79018b70851ca0cdb9e1f86eaa766dc2280a3d`.
Counts overlap and are not additive. Generated API documentation, documentation
links/anchors/configuration inventory, Ruff and whitespace checks pass. No
production data, installed Pulse, release, commit or push was changed here.
