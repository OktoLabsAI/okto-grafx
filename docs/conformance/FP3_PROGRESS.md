# FP-3 working evidence: polymorphic reads and native entity results

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Full acceptance requirements](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-3--entity-identity-polymorphism-and-paths) ·
[Query contract](../COMPOSABLE_QUERIES.md#polymorphic-node-read-composition)

September 11, 2026, `feature/v0.0.6` development. This is an implementation
increment, **not FP-3 acceptance**, complete Cypher parity or a release.

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

## Remaining mandatory FP-3 work

- Complete node-only capture, type alternatives and broader path expressions;
  complete all hostile/public/budget and entity observation combinations.
- Broader aggregate/spill and subsequent-clause combinations beyond the verified
  entity UNION cases; incompatible/missing-property and schema combinations.
- Untyped/ambiguous relationship traversal and original fixture compatibility;
  omitted-upper-bound semantics without silently truncating complete enumeration.
- Cross-table index access when compatible keys can anchor new polymorphic
  bindings, without skipping possible results or silently truncating scans.
- All required original/supplemental scenarios, package regression and affected
  isolated Pulse consumer checks at checkpoint B and final FP-8.

No production Pulse data, installation, Core adapter boundary or published
version was changed. The full goal and frozen case ledger remain unchanged.
