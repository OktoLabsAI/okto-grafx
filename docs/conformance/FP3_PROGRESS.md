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

## Remaining mandatory FP-3 work

- Complete remaining schema/pattern and broader path-expression cases;
  complete all hostile/public/budget and entity observation combinations.
- Remaining entity-dependent scalar/label-expression cases and broader untyped
  OPTIONAL semantics required by the FP-3 profile; the named absent-table and
  native function cases alone are not full upstream/profile qualification.
- Broader aggregate/spill and subsequent-clause combinations beyond the verified
  entity UNION cases; incompatible/missing-property and schema combinations.
- One relationship type across different endpoint-table pairs (including required
  Path2 #0001/#0002), plus remaining original fixture compatibility. Do not turn
  this required native schema/identity gap into a test-adapter label rewrite.
- Cross-table index access when compatible keys can anchor new polymorphic
  bindings, without skipping possible results or silently truncating scans.
- All required original/supplemental scenarios, package regression and affected
  isolated Pulse consumer checks at checkpoint B and final FP-8.

No production Pulse data, installation, Core adapter boundary or published
version was changed. The full goal and frozen case ledger remain unchanged.
