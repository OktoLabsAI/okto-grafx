# Functional parity expansion: language, values and executable conformance

[Roadmap and delivery status](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Current compatibility](../CYPHER_COMPATIBILITY.md) ·
[Product comparison](../FEATURE_COMPARISON.md)

Prepared September 11, 2026; subsequently authorized for implementation by the user.
This document preserves the agreed scope, including all six functional fronts and
the unresolved TCK inventory. It does not itself certify any package, change a
version/branch, authorize a release or modify the installed Pulse. ROADMAP.md
remains the single delivery-status authority.

Current September 13 qualification: [complete native regression and fixed-scope audit](../reports/FP_FINAL_NATIVE_QUALIFICATION.md)
passes 25,077 tests with 19 attributed skips; all 3,896 required V3 cases and the
20 native supplemental maps pass. [Final installed Pulse](../reports/FP_FINAL_PULSE_QUALIFICATION.md)
qualifies the exercised API/UI/MCP flows. Ladybug comparison is recorded; pinned
Neo4j execution is deferred by the explicit decision below. Implementation paragraphs and counts below retain
their historical checkpoint chronology: their former "pending" statements are not
new scope or current failures. Normative package requirements and invariants remain
unchanged, and the roadmap alone owns current delivery status.

### Delivery decision: defer Neo4j execution

On September 13, 2026 the user explicitly deferred the proposed Docker/Neo4j
comparative run: it is not required for this delivery. Decision
`FP-NEO4J-DEFERRED-20260913` supersedes that external acceptance condition below,
not any native capability, required V3 case, supplemental native contract, Pulse
check, regression or documentation requirement. Retain the pins, prepared tooling
and unexecuted status for possible future work; do not start Docker for it now.
No Neo4j execution, behavioral equivalence or cross-product parity is claimed.
With that recorded deferral, the qualified native/Pulse/documentation delivery is
complete. Historical receipts and the frozen 22-ID inventory remain unchanged.

## 1. Outcome and fixed boundaries

Deliver a substantially broader, tested native query/value contract for Python
local-first applications, with coordinated Pulse consumption. Address **all six
requested fronts**, not just the current Pulse queries. Pulse need not already use
a capability for that capability to be included.

Two different claims must remain separate:

1. **Functional parity in the declared profile:** all agreed capabilities and
   scenarios pass with explicit types, errors, effects and operational contracts.
2. **Full openCypher conformance:** all 3,897 pinned cases execute and pass their
   original expectations, without semantic substitutions or profile exclusions.
   This plan does not promise that claim while
   intentional schema/model/arithmetic divergences remain. Passing a selected
   profile must never be relabeled as passing the entire TCK or all Neo4j features.

Non-negotiable invariants: independent local readers/writers, existing snapshot/OCC
and publication fencing, whole-statement failure isolation, WAL/durable commit,
verified recovery, bounded resources, and an agnostic production Pulse Core.
No cross-store transaction, external-effect rollback or universal serializability
promise is introduced.

Explicitly outside this delivery: NaN/infinity storage,
Bolt/native language drivers, APOC/GDS parity, server RBAC,
HA/replication, remote federation, valid-time/bitemporality, independent inner
commits (`CALL ... IN TRANSACTIONS`),
and unlimited traversal. These are not prerequisites for this local-first profile.
Limits must produce explicit refusal, not silent result truncation.

### Authorized model expansion: label-free nodes and heterogeneous properties

Implementation checkpoint: native node/edge `ANY` columns and tagged persistence,
dynamic query use, catalog capability/recovery fencing and logical transfer are
implemented and regression-tested. That foundation alone did not close the flexible
model or Match4 #0004; the subsequent increments below extend it. See [scope and receipts](../conformance/FP3_PROGRESS.md#authorized-flexible-model-foundation-persisted-any-properties)
and [current storage/API contract](../architecture/HETEROGENEOUS_PROPERTIES_V1.md).
Native implicit single-label and unlabeled CREATE/MERGE, dynamic property maps and empty-label DTOs now
extend this foundation; [flexible graph contract](../architecture/FLEXIBLE_GRAPH_V1.md).
Automatic undeclared relationship types now support transactional endpoint-pair
growth, dynamic properties and same-statement reads. The original Match4 #0004
passes without fixture adaptation. Full package qualification remains pending,
including installed-Pulse validation and wider model/profile qualification.
Typed-group existing-target copy now preserves logical authority with endpoint-pair
resolution and atomic receipts. Flexible/no-PK copy now preserves distinct native
target identities and remaps actual endpoints without property-based deduplication;
the [copy contract](../CATALOG_COPY.md#flexible-and-no-pk-entity-identity) defines its
existing-schema, bounds, skip and history semantics.
Opt-in retained
history now preserves flexible/grouped models with explicit old-writer fencing;
activation remains per physical table and does not invent earlier history. Fresh-store
logical transfer now preserves typed/flexible relationship groups and maps through
versioned artifact metadata, native transactional schema attachment and endpoint
identity remapping; it does not transplant retained history.

The user explicitly chose the broader native functionality instead of making
Match4 #0004 a model divergence. **Schema-free storage and persisted heterogeneous
property values are now in scope**, superseding their original exclusions above.
The original case remains required; neither fixture labels nor expected values
may be synthesized to satisfy it. This is a general model capability, not a
special-case loader or a string encoding of mixed values.

Implement as a coordinated FP-3/FP-6 model package before declaring the query
profile complete:

1. Native label-free node creation, identity and read/write binding. `labels()`
   and detached values expose the actual empty label set, never an internal
   physical table name. Existing explicit typed tables retain their constraints.
2. Persist heterogeneous property values with faithful native type tags, including
   different supported value types for one property name across nodes and over
   updates. Missing/removed properties and NULL follow the query model; MAP/LIST
   nesting does not permit arbitrary Python objects or nonfinite persisted values.
3. Native relationship creation between these nodes, including undeclared fixture
   relationship types, with real endpoint identity and whole-statement schema/data
   rollback. No disconnected sidecar graph or implicit second transaction.
4. Integrate property/label predicates, MERGE matching/creation, paths, OPTIONAL,
   aggregation/DISTINCT/UNION, detached values and JSON serialization. Publish
   explicit index/constraint support for flexible properties; no silent coercion,
   mixed-type key collision or loss of original value types.
5. Define and validate the durable layout/capability before writing it. Cover
   pure/accelerated codec equivalence, old-reader refusal before mutation,
   COMMIT/WAL replay, malformed pages, crash/restore, verification, retained history
   and supported copy/transfer/import/export. Unsupported routes refuse explicitly
   until completed; such refusal is not completion of a required integration.
6. Execute the original mixed-type chain fixture without rewriting it. Add
   independent tests for type-changing updates, deletes/recreates, NULL/missing
   properties, rollback after late schema/data changes, concurrent snapshots and
   conflicting writers. Requalify affected Pulse consumers through neutral Core
   contracts and Community adapters at the existing checkpoints.

Audit existing schema-free/heterogeneous-value divergences against this expansion
and publish a versioned successor to the frozen case profile with unchanged source
checksums/expectations. Preserve V1 and its receipts as historical evidence; do not
silently reinterpret their counts. Related required cases remain visible until
native execution succeeds. This decision does **not** relax multi-reader/writer,
consistency, durability, resource ceilings or the explicit NaN/infinity storage
prohibition, and does not authorize installation, release or production-data changes.

The first scope succession was recorded in [profile V2](../conformance/PROFILE_V2.md):
3,875 required cases, 22 retained multiple-label divergences, unchanged source and
historical observations. This closes profile reclassification, not the broader
model qualification or required-case execution.

### Authorized multiple labels and retained nested storage

On September 12, 2026 the user explicitly authorized **multiple labels per node**,
removing that model exclusion, and separately authorized **keeping lists of maps
as native properties** despite the opposite Set1 #0010 expectation. Decisions:
`FP-MULTILABEL-20260912` and `FP-NESTED-STORAGE-20260912`.

[Profile V3](../conformance/PROFILE_V3.md) makes the 22 former multiple-label
divergences required and reclassifies only the source-bound Set1 #0010 as an
explicit native nested-storage divergence. All 3,897 upstream cases remain:
**3,896 required / one divergence**. V1/V2 and their observations are preserved;
no source query, expected error, effect, row or past result is rewritten. The
29 already-required label-action failures also remain implementation requirements.
These counts describe scope, not passing execution or full TCK conformance.

Deliver multiple labels as native membership on one stable node identity, not
duplicate nodes per label, an adapter-only graph or table-name aliases. Cover
CREATE/MERGE, MATCH and label predicates, SET/REMOVE (including conditional MERGE
actions), empty sets and idempotency, labels()/detached entities, same-statement
visibility, traversal/relationship endpoints and result identity. Preserve typed
table constraints and document label-versus-physical-schema resolution explicitly.
Label mutations must share native statement rollback, snapshots, OCC and durable
publication. Qualify the chosen layout/capability, old-reader refusal, WAL recovery,
verification, retained history, copy/transfer and affected external consumers
before declaring this increment complete. Existing resource ceilings still apply.

Implementation foundation: [native node-label storage v1](NODE_LABELS_V1.md)
now defines the per-version prefix, required capability bit 28, candidate
authority, resource bounds and storage tests. The final 3,168-pass grouped selection
qualifies that foundation only. The subsequent native transaction
checkpoint adds intent/reducer/quota/schema-journal/COMMIT integration and
temporary fail-closed history boundaries, with **719 grouped regression passes**
and no failures/errors/skips on the corrected pure-engine implementation.
The [query-label mutation checkpoint](NODE_LABELS_V1.md#query-label-mutation-checkpoint)
now implements ordered SET/REMOVE, staged-owner label predicates and labels(),
candidate-table/index membership checks, native traversal/optional landings,
spill metadata and full detached labels. New label-candidate admission honors
procedure schema permissions. The following
[CREATE/MERGE and scan checkpoint](NODE_LABELS_V1.md#query-create-merge-and-scan-checkpoint)
adds complete creation label sets, owner-wide MERGE membership, private-phase
identity, nested read composition and independent scan-based TCK label observation.
The original FP-4 selection has **289 required passes** and only the authorized
Set1 #0010 divergence. Fresh-store transfer labels were still pending at that
checkpoint; their subsequent qualification is recorded below. The final full
profile/consumer/package checkpoints are not marked complete.
The complete native V3 query execution subsequently produced **3,895 passes / two
failures / zero not run**. Graph3 #0009 was the sole required failure and its error
phase is corrected, with 61 original graph-function cases passing. The other
failure remains the authorized Set1 divergence. This is not a green full rerun
or final parity acceptance. Following the 172-pass history-codec checkpoint,
[native retained-history integration](NODE_LABELS_V1.md#native-retained-history-integration)
now carries labels through publication, temporal DTOs, scan/index/diff,
retention/compaction, verification and recovery. Its affected regression has
238 passes, including real process cuts; public contracts include membership
states and label-delta budgets. This removes the temporary history refusals;
transfer and installed-reader qualification have their own subsequent receipts.
The [existing-target copy integration](NODE_LABELS_V1.md#existing-target-copy-integration)
now preserves logical membership, bounded copy frames/digests, physical-PK skip,
native candidate admission, target history and data-plus-receipt recovery.
The existing 77-case copy regression passes; the final corrective copy/history-codec/
contract selection has 421 passes, including all 25 new label-copy cases. These
collections do not themselves qualify the fresh-store export/import/resume path.
The subsequent [format-4 transfer integration](NODE_LABELS_V1.md#fresh-store-transfer-format-4)
preserves explicit empty/full/implicit membership, candidate authority, type
descriptors, endpoint identity and bounded normal/resumable import. Its grouped
consumer selection has **236 passes**, including 58 label-transfer cases with
corruption, concurrent snapshots and actual process cuts. The
[installed-wheel checkpoint](../reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
passes **36 native scenarios and 36 transfer worker checks**. Bit-28 refusal,
old live handles, pending COMMIT, all-file non-mutation and installed candidate
identity against all 251 source/wheel files are proved. These close the affected
transfer/installed-reader checks, not final full regression or consumer acceptance.
The subsequent [complete native V3 query run](../reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md)
passes **all 3,896 required cases**, with only the authorized Set1 #0010 upstream
failure and zero not run. This closes current required-query-profile execution;
full repository/supplemental/competitor and paired Pulse acceptance remain open.
The grouped query/transaction/API run has 5,911 passes and six failures; its
stale-model expectations and private fault-hook mismatches are all corrected in
the final 392-pass combined collection. This closes those diagnosed causes,
not the final current-source full regression or remaining consumer checkpoints.

Retaining nested properties is not permission to persist NaN/infinity, unsupported
host objects or malformed values. Set1 #0010's original TypeError/runtime/
InvalidPropertyType oracle remains recorded and executed as a divergence, never
reported as an upstream pass. Its native positive persistence tests remain separate.

## 2. Baseline: evidence, not estimates of implementation effort

The pinned reference remains **openCypher TCK 2024.3**, commit
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`: 220 feature files / 3,897 expanded cases.
The recorded positive-expression diagnostic is **1,004 passed / 1,077 failed /
1,816 not run**. This is not a full-suite execution or a parity percentage.
[Current contract and reproduction](../CYPHER_COMPATIBILITY.md).

Read-only grouping of `.grafx-tmp/opencypher-expressions-final.json` found:

| First observed failure | Cases | Planning consequence |
|---|---:|---|
| Missing `datetime`, `localdatetime`, `date`, `time`, `duration`, `localtime` | 483 | A coherent temporal value/function family, not hundreds of unrelated patches |
| Missing `rand` | 63 | Native nondeterministic function semantics and independent tests |
| Parser message `RETURN ends a query` | 482 | Inspect actual syntax; the message is not a diagnosis of 482 illegal clause sequences. Hex literals are one confirmed example. |
| Unaliased result-column spelling | 5 | Preserve expression source spelling at the result boundary |
| Other first failures | 44 | Assign individual causes: chained comparisons, additional syntax/functions, named paths and explicit policy limits |

These disjoint buckets total 1,077; they are **not** projected pass gains. Fixing
the first blocker can expose another. JSON SHA-256:
`05a2b445bf1ec87656501f724ec90f71ea4127177070c0ae986fcc1ec47b3734`.
The local receipt is reproducible evidence, not a runtime dependency.

Current implementation evidence guiding the work:

- [`tools/check_opencypher.py`](../../tools/check_opencypher.py), especially
  `run_read_case`, only executes a supported set of read/empty-graph steps;
  missing fixture/error/effect support explains part of `not_run`.
- [`COMPOSABLE_QUERIES.md`](../COMPOSABLE_QUERIES.md) explicitly restricts entity
  UNION output, subquery imports/writes and pure tabular procedures.
- [`domain/model/value.py`](../../src/okto_grafx/domain/model/value.py) already has
  LIST/MAP encoding and depth limits. The nested-column task is public typed
  schema/planner/index/interop completion, not reinventing that codec.
- [`domain/query/extensions.py`](../../src/okto_grafx/domain/query/extensions.py)
  currently grants no graph transaction to callbacks. Writing procedures need a
  controlled transaction capability, not an unrestricted Database handle.
- [Latest Pulse acceptance](../reports/PULSE_V006_INTEGRATION_REGRESSION.md)
  provides the consumer regression baseline; it is not a full historic Pulse suite.

## 3. Ordered delivery packages

Effort is relative and includes tests/docs: **M** = several existing components;
**L** = planner/executor/API changes; **XL** = persistent format or broad value
semantics. These are not calendar estimates. Each package must deliver native
execution and public consumption, not only grammar or test-harness support.

| Order | ID | Deliverable | Effort | Principal prerequisite |
|---|---|---|---|---|
| 1 | FP-1 | Fixed capability/case matrix and stateful TCK runner | L | Existing pinned inventory |
| 2 | FP-2 | Remaining scalar syntax/functions and faithful column names | M | FP-1 |
| 3 | FP-3 | Qualified entity results, polymorphic patterns and named paths | L–XL | FP-1; FP-2 simplifies expression composition |
| 4 | FP-4 | Returning/unit read-write subqueries and scope imports | L | FP-3 entity binding; existing statement rollback |
| 5 | FP-5 | Native temporal values, constructors/operations and persistence | XL | FP-1; FP-2 postfix expressions |
| 6 | FP-6 | DECIMAL and typed persisted collections/structures | XL | FP-5 shared type/format admission machinery |
| 7 | FP-7 | Transaction-scoped writing procedures | L | FP-3/4; FP-5/6 signature/value integration |
| 8 | FP-8 | Final profile/TCK accounting, Pulse migration and delivery evidence | L | FP-1 through FP-7 |

Independent value/codec work can run alongside query work after FP-1, provided
shared type contracts are settled first. The default implementation order remains
the table above. FP-8 is final integration, **not the first time these features get
tested**.

### FP-1 — Freeze the matrix and make the TCK executable

Create a versioned case ledger keyed by upstream case ID + source checksum, with
required capability, package owner, observed result, root-cause family, expected
error phase/effects, and profile inclusion or explicit architectural divergence.
Account for all 3,897 cases. Preserve the original source queries and expectations.
Publish separate **upstream** and **Grafx-profile** results; never rewrite queries
or expected answers and report the adapted result as upstream conformance.

Extend the runner to handle graph fixtures, sequential setup/query steps, typed
parameters/results (including entities/paths and later new values), empty results,
ordered/bag comparison, writes, expected errors and observable side-effect counts.
Verify no unintended effects after failures using a subsequent transaction; use
durable reopen for mutation families, not a full filesystem scan per scalar case.
The reference's error phase is evidence: an earlier refusal is not automatically
the expected runtime error. Compare against independent expected values, not the
Grafx evaluator as its own oracle.

If typed schema must be declared to load a graph fixture, record that adaptation.
Cases depending on the remaining model exclusions stay visible divergences;
schema-free writes are now required by the authorized model expansion above.
do not evade them by rewriting the operation under test. Tests of the runner must
deliberately inject wrong rows, missing/extra effects and wrong errors to show the
runner actually fails.

**Exit:** every case classified and assigned; all step families inventoried;
runner paths for compatible fixture/write/error cases tested; a frozen list of
initial blockers, not a requirement that unimplemented engine features already
pass. Modern subquery/procedure extensions get separately versioned scenarios,
since their coverage cannot be inferred from this older TCK.

**Checkpoint A:** present exact case/capability coverage before FP-2. A new
model/transaction requirement outside the six fronts is not silently added here.
Every in-scope blocker remains assigned even if first revealed in a later package.

### FP-2 — Expressions, scalar functions and column spelling

Implement hexadecimal/octal literals, including signed boundaries and consistent
overflow errors; chained comparisons with the reference's NULL/evaluation rules;
and general property/index postfix access on expression results rather than a
special case for named variables. Preserve the original expression text for
unaliased result-column names without changing normalized planning/cache keys.

Add native `rand()` with declared nondeterminism: no constant folding or cached
result across executions, and no admission to deterministic index/view definitions
unless their contract permits it. Test range/type/evaluation placement through a
controllable random source, not fragile assertions that two draws must differ.
Assign `properties`/`labels` and any entity-dependent scalar cases to FP-3; they
must not vanish from the matrix because they are not one of the temporal functions.

**Exit:** all FP-2-owned cases pass; original five column-name failures are covered
without mandatory user aliases; numeric/NULL/overflow and runtime cost regressions
pass. Explicit user decision (2026-09-11): support NaN in expressions, while
forbidding NaN/infinity in newly stored properties, including nested collections.
The eight NaN comparison cases remain required, not model divergences. This does
not authorize infinity-producing division or change integer division-by-zero,
vector finiteness, transaction atomicity, WAL or durability contracts.

### FP-3 — Entity identity, polymorphism and paths

The [property-access checkpoint](../conformance/PROPERTY_ACCESS_PROGRESS.md)
passed 527 of 528 required FP-3 cases without schema adaptation. The subsequent
[independent namespaces increment](GRAPH_NAMESPACES_V1.md) passes all **528 required
cases**, including original Graph5 #0002, without changing the ledger or fixtures.
Native entity KEYS/dynamic properties, property REMOVE and relationship type
predicates are implemented. Qualified copy/transfer/resume/history and full-text
index ownership are also implemented in the subsequent consumer increment.
Qualified hybrid search and 24 installed catalog/WAL admission scenarios also pass.
Qualified bloat/vacuum/nullable-column operations pass 104 affected tests and
eight separate process-cut recovery cases. The Pulse Community transfer adapter
now qualifies physical kind without changing Core. The [vector runtime repair](VECTOR_PHYSICAL_OWNERS_V1.md)
passes 219 affected tests: table-qualified ownership/search/hybrid and typed SET
vector conversion, with pinned-reader/foreign-writer and process-cut evidence.
The [durable naming repair](VECTOR_OWNER_NAMES_V1.md) passes 185 affected tests,
a subsequent 20-case API suite and 24 installed-wheel scenarios, preserving intact
legacy artifacts and fencing unsupported readers with capability 25. Public
diagnostic/rebuild selectors pass [95 affected tests](VECTOR_QUALIFIED_MAINTENANCE_V1.md).
The broader static-contract audit's 75 failures are corrected with
[static, behavioral and boundary evidence](../reports/FP_STATIC_CONTRACT_QUALIFICATION.md);
full-profile/package acceptance remains open. Subsequent [component/legacy repair evidence](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
covers selected mutation/reconciliation and explicit reconstruction of genuinely
missing historical indexes; 609 affected regression tests pass. The next
[consumer qualification](../reports/NAMESPACE_PROJECTION_VIEW_MIGRATION_QUALIFICATION.md)
corrects projection selection, physical view dependency identity and node-owned
view/migration ledgers: 87 affected tests and eight process-cut cases pass. The
[installed transfer matrix](../reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md)
then covers two archived importers, formats 1/2/3 and exact no-mutation refusals;
their tiny-batch limitation remains explicit. Full package acceptance remains
open: remaining public consumers/type-specific interchange, broader Pulse
qualification and the broader gates below still
require completion; the owner-wide query receipt does not replace them.
The later [logical-view expression correction](../reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md)
extends dependency authority to EXISTS, pattern predicates/comprehensions and
nested reads, with explicit stable-view admission and 176 affected tests passing.
The subsequent [CLI consumer increment](../reports/CLI_VECTOR_OWNER_QUALIFICATION.md)
exposes the physical vector owner selector and preserves lexical/hybrid ownership
for homonymous tables through the public command interface.

Define one public detached representation for nodes, relationships and paths,
separate from internal live execution bindings. Identity must distinguish database,
table/schema identity, element kind and record incarnation; a table-local ID alone
is insufficient. Specify snapshot provenance, deleted/recreated objects, property
materialization, equality/hash and serialization. Returned objects must not keep
transactions alive or permit using foreign/stale entities as writable handles.

Use this identity across `UNION`/`UNION ALL`, DISTINCT/grouping, nested list/map
results and subsequent query clauses. Test equal IDs in different tables/stores,
aliases, old/new snapshots, duplicate entities and edge endpoints. Document the
public result change and provide detached JSON representations for consumers.

Generalize reads over compatible node tables and relationship-type alternatives
inside composed MATCH/OPTIONAL MATCH/subqueries. Missing properties and NULLs must
follow the declared profile, not disappear because the first table lacks a column.
Support named variable-length paths, both directions/undirected traversal, zero
length, cycles, bounds, relationship uniqueness, `nodes`, `relationships`, `length`,
`type`, `labels` and `properties`. Repeated nodes and repeated relationships are
not interchangeable: freeze the reference's trail semantics per pattern/clause.

Use a bounded streaming traversal and shared query cancellation/work budgets.
An omitted syntactic upper bound may use that budget but must never silently
pretend a truncated enumeration is complete. Avoid global scans when an endpoint
or compatible index can anchor the query; do not promise that all graph enumeration
can be sublinear. Shortest-path modes beyond the frozen scenarios are not implied.

**Exit:** entity UNION and general named-path fixtures pass with exact multiplicity,
null extension and identity; cross-table collisions cannot merge different nodes;
cursor close/cancellation releases resources; read/write snapshots remain isolated.

### FP-4 — Subqueries that read and write

The [native EXISTS increment](EXISTS_SUBQUERIES_V1.md) now implements read-only
query expressions with implicit imports and non-exporting scopes, aggregation,
optional RETURN, UNION and shared snapshot/budgets. The first original family run
passes all 10 required cases without source adaptation. Its composed-scope and
resource/public-plan regression is recorded separately; this does not waive any
remaining mutation, checkpoint or full-profile requirement.

The complete owner refresh with EXISTS reaches **255 passes, 30 required failures
and five retained divergence failures**, all 290 selected cases executed; all
245 preceding passes remain. The remaining required cases are 29 label mutations
and the Set1 #0010 negative expectation conflicting with authorized nested storage.
The existential family is reconfirmed on the final composed-scope source. Exact
receipts and the distinction from full FP acceptance are in the EXISTS contract.
The final combined regression passes **978 tests**, zero failures/errors/skips,
including pure/NumPy recovery cuts; API/configuration/usage/roadmap documentation
and changed-source checks pass. This completes this increment, not checkpoint B.

Supplemental extension cases and current native implementation evidence are
recorded in [WRITE_SUBQUERIES_V1.md](WRITE_SUBQUERIES_V1.md). Returning/unit writing
calls use invocation-local read phases and shared statement rollback. Explicit
and wildcard imports now persist across WITH; leading-WITH imports have independent
branch-local scope. Updating/unit UNION now has branch-local private phases,
identity-stable outputs and shared rollback. Complete qualification remains required;
this increment is not completion of FP-4 or checkpoint B.
The subsequent [conditional MERGE increment](MERGE_ACTIONS_V1.md) adds property
`ON CREATE SET` / `ON MATCH SET` actions under that same boundary and all-match
bound-edge enumeration. Its original MERGE-family run has 51 passes and 23
required failures plus one retained divergence. Subsequent [whole-property SET](SET_PROPERTY_MAPS_V1.md)
supports map replacement/overlay and native entity sources, including conditional
actions; its MERGE rerun passes 55, with 19 required failures and one retained
divergence. SET passes 37/53; label mutation and the nested-storage negative-oracle
conflict remain explicit. Other pending mutation forms are not waived.
The full FP-4 owner refresh after these increments records 201 passes, 84 required
failures and five retained divergence failures, with all 290 selected cases run.
No previously passing post-SQ-13 owner case regressed. This is not checkpoint B.
Subsequent [expression/path DELETE](DELETE_EXPRESSIONS_V1.md) closes all 41 original
DELETE-family cases, including native nested selectors, repeated identities and
error contracts. Remaining owner requirements are not waived by that family result.
The following [written-pattern increment](WRITE_PATTERN_CONTRACTS_V1.md) implements
named CREATE/MERGE capture, binding/error contracts and private read-phase ordering
after mutations. CREATE now passes 74 selected cases (two required failures/two
divergences); MERGE passes 67 (seven required failures/one divergence). General
MERGE forms and the full checkpoint remain required.
The next increment adds bound-edge undirected MERGE and native `startNode()` /
`endNode()` functions. The MERGE family reaches 70 passes, four required label-action
failures and one retained divergence, without changing original scenarios or the
ledger. Its qualification also corrects sorted WITH write-binding loss under a
memory budget. This does not close general MERGE, label mutations or checkpoint B.
The final affected collection passes 1,016 tests, including fault/recovery on both
codecs and sequential conditional actions; documentation/API/configuration checks
pass. The [qualified receipt](WRITE_PATTERN_CONTRACTS_V1.md#undirectedendpoint-follow-up-qualification)
retains the earlier failed regression as evidence of the correction, not a waiver.
The [large-CREATE increment](WRITE_PATTERN_CONTRACTS_V1.md#large-create-pipelines)
now executes both original Create4 queries as flat ordered native programs, with
bounded source admission and unchanged transaction guarantees. All 76 required
CREATE cases pass; two multiple-label divergences remain. This closes the explicit
large-query blockers, not the remaining FP-4/full-profile requirements.
Qualification passes 947 affected regression tests plus 14 overlapping focused/
fault cases. A new complete owner execution passes 245, with 40 required failures
and five retained divergence failures; none of the preceding 201 passes regressed.
The remaining causes are explicitly classified in the
[qualification receipt](WRITE_PATTERN_CONTRACTS_V1.md#large-create-qualification-receipts).
At that historical checkpoint, label additions conflicted with the model exclusion
and Set1 #0010 conflicted with authorized nested storage. The explicit decisions
above now resolve both scope questions: implement multiple labels and retain native
nested storage with one visible divergence. Historical results are not relabeled.
The [paired Pulse source follow-up](../reports/FP4_PULSE_SOURCE_QUALIFICATION.md)
tracks native result conversion in Community and fenced updating queries through
the neutral transaction port; installed/API/browser qualification remains separate.

Support returning and unit subqueries, explicit imports and the agreed leading-WITH
import form. Freeze modern extension cases separately from the pinned TCK before
implementation. Define per-input-row evaluation, imported-name shadowing/collisions,
aggregation over zero rows and output cardinality: unit subqueries preserve the
outer row; returning subqueries contribute the rows they actually return.

Allow inner writes and subsequent reads, including composed updating branches
where permitted by the frozen profile. All use the **same outer transaction**,
snapshot, quotas and single statement rollback boundary. No inner commit and no
cross-store work. An error late in any invocation or in final result conversion
discards effects from every invocation of that statement. Earlier successful
statements are retained only when rollback is proven; otherwise abort the transaction.

Reject a write subquery before effects when invoked through a read transaction or
read-only cursor. Define interruption of streamed write results explicitly so an
abandoned consumer cannot accidentally finalize only part of a statement.

**Exit:** correlated/uncorrelated, zero/multiple-row and nested scenarios pass;
fault injection across inner writes and final output proves all-or-nothing effects;
independent readers see only committed publication and conflicting writers retain OCC.

**Checkpoint B:** FP-2–4 close the first query package. Run a grouped query/transaction
regression and affected Pulse tests before persistent type changes.

The [grouped follow-up](../reports/FP_QUERY_TRANSACTION_CHECKPOINT.md) completed
6,176 query/transaction cases: 6,171 passes and five old-contract test failures.
All five are addressed with 427 passing affected regressions, preserving negative
authority/rollback checks. This does not turn the original run green or close
the required profile, pending model/oracle decisions or full-plan acceptance.

The subsequent [general MERGE increment](GENERAL_MERGE_V1.md) closes unbound
endpoints and fixed multi-hop match-or-create, preserving full-pattern semantics,
conditional action visibility, rollback, independent readers and OCC. The grouped
307-test regression and 2,390-test final contract/fault collection pass (overlapping
coverage). All 70 original MERGE passes remain; four required label failures and
one retained divergence remain. This closes that explicit shape requirement,
not checkpoint B, label decisions, package-wide Pulse qualification or FP-5–8.
The [paired Pulse follow-up](../reports/GENERAL_MERGE_PULSE_QUALIFICATION.md)
passes affected source tests and 39 privately installed three-package adapter
cases, with byte/origin assertions. No Core implementation changes were needed;
this closes the general-MERGE Python consumer check, not full HTTP/browser
acceptance or the remaining checkpoint requirements.

The subsequent [installed HTTP/UI check](../reports/PULSE_HTTP_UI_PARITY_QUALIFICATION.md)
qualifies graph pagination from 500 to 510 nodes and eight public HTTP operations.
It found and corrected false-empty untyped-endpoint logical relationship reads
in Community, with 153 affected source tests passing. Native matching handles
physical type alternatives; Core policy and write fences remain unchanged.
This closes that bounded browser/API check, not full FP-8, MCP acceptance or the
broader logical-name syntax limitations recorded in the report.

### FP-5 — Complete temporal values, not string-only constructors

Execution matrix and current native query/storage integration status:
[native temporal integration contract](TEMPORAL_VALUES_V1.md). Its staged work
does not narrow this package's original complete query-and-storage exit.

Provide `date`, `time`, `localtime`, `datetime`, `localdatetime` and `duration`
constructors/properties and their agreed conversion, comparison and arithmetic
forms. Enumerate exact overloads from the frozen temporal cases and a reviewed
capability list, including documented operations not exercised by this TCK slice.

Recommended contract: represent date, local/zoned time and local/zoned datetime
distinctly; preserve precision/offset/zone information rather than silently folding
everything into the existing microsecond UTC `TIMESTAMP`. New types preserve
nanosecond precision where required by the frozen reference, or explicitly refuse
out-of-range values rather than rounding silently. Keep calendar months/days
separate from elapsed-time components in durations. Define leap dates, month ends,
negative durations, normalization, precision and range errors. Ambiguous or invalid
local times require a documented explicit resolution, not machine-dependent guessing.
Implicit clock/timezone behavior must be explicit; inject clocks for tests and
qualify timezone-data dependencies. Existing `TIMESTAMP` is an instant type, not
a legacy compatibility switch for the new local/zoned types.

Deliver expression values **and stored columns**: schema declarations, parameter
binding, result conversion, pure/accelerated codecs, comparison/index support,
WAL/recovery/verification, retained history, copy/backup and import/export. If a
particular index/type combination is unsupported, refuse it at definition time.

**Exit:** temporal matrix and independent calendar/round-trip tests pass; exact
values survive commit, restart, history and supported transfers. Older binaries
refuse new layouts before mutation; no precision loss or timezone drift is accepted.

### FP-6 — DECIMAL and persisted nested values

The [decimal contract](DECIMAL_VALUES_V1.md) now includes native `DecimalValue`
parameters/results, typed `DECIMAL(p,s)` columns, ANY nesting and the versioned
frame/capability admission. Initial storage/fault/reopen qualification is recorded
[separately](../reports/FP6_DECIMAL_NATIVE_QUALIFICATION.md). Native numeric
operators/casts/SUM/AVG/MIN/MAX, exact comparison/grouping/ordering and typed
equality-index probes now have [query/index qualification](../reports/FP6_DECIMAL_QUERY_QUALIFICATION.md).
History (including physical backup/restore), parameter-bound catalog copy and
fresh-store logical transfer/resume now have [consumer qualification](../reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md).
The following [interface increment](../reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md)
adds exact CLI JSON/schema, typed CSV/JSONL/SQLite ingestion and owned procedure
DECIMAL/NUMBER values with native query/write integration. The subsequent
[columnar increment](../reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md) implements
exact decimal128 Arrow/Pandas/Polars/Parquet via ArrowDecimalType(p,s), preserving
native coordinates, mandatory metadata, existing budgets and whole-call rollback.
The later increments below implement typed collections and their interchange
consumers and qualify checkpoint C; the matrix distinguishes unsupported index families.

Recommended initial decimal profile: `DECIMAL(p,s)`, `1 <= p <= 38`,
`0 <= s <= p`, exact coefficient/scale representation. Freeze arithmetic result
precision, casts, aggregation, ordering/equality/hash, overflow and explicit
rounding rules before coding. Do not route exact values through DOUBLE or silently
inherit the host's mutable decimal context. Cross-type comparisons must retain
exactness and consistent index/grouping keys.

Complete typed `LIST<T>`, string-keyed `MAP<T>`, fixed-size `ARRAY<T,n>` and named
`STRUCT` fields, including element/field nullability, nesting, copying/ownership,
DDL/introspection, updates, equality and serialization. Exact syntax is proposed,
not an existing API. Reuse existing LIST/MAP encoding where sound; mutable nested
values must not invalidate cached encoding proofs or mutate a held snapshot.

The [typed-collection integration](TYPED_COLLECTIONS_V1.md) connects bounded
descriptors to public StoredType/ColumnDef, DDL, catalog publication, owned exact
assignment and strict stored reads. History, nullable schema evolution, copy,
logical transfer and CLI schema preserve descriptors. [Native qualification](../reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md)
passes 3,048 grouped tests and 2,447 public/annotation/import/documentation tests;
the subsequent [exact JSON/text/SQLite collection interfaces](../COLLECTION_JSON.md)
add owned descriptor admission and unambiguous ANY tags. The subsequent
[columnar collection integration](../COLLECTION_COLUMNAR.md) adds exact
Arrow/Pandas/Polars/Parquet with full descriptors, bounded native conversion and
atomic late refusals. The support matrix and installed-reader checkpoint C are
now completed for the recorded candidate; final profile/Pulse acceptance remains required.

The [consolidated support/refusal matrix](../TYPE_SUPPORT.md) now records primary keys,
indexes, parameters/results/procedures, all implemented external transports and
history/copy routes. It distinguishes native schema from transport declarations
and unsupported combinations from unexecuted installed-package qualification.

The [combined installed-wheel checkpoint](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
passes all 60 selected scenarios with exact package/wheel/source provenance, pure
and accelerated writes, pure recovery, interrupted durable COMMIT and all-file
old-refusal proofs. [One combined format/type upgrade procedure](../V006_COMPATIBILITY.md#combined-native-type-upgrade-procedure)
now covers FP-5/FP-6. This closes the type-package checkpoint for that candidate,
not final parity, Pulse acceptance or future multiple-label storage changes.

Publish a type-support matrix covering primary keys, equality/ordered indexes,
query parameters/results, Arrow/Pandas/Polars/Parquet, CSV/JSONL/SQLite, CLI JSON,
logical transfer and history. New scalar types need coherent supported index
semantics; nested values need not be legal primary keys or acquire arbitrary
element indexes. Unsupported interchange types must fail before partial import,
not silently stringify or lose decimal scale. JSON needs explicit tagged forms
where ordinary numbers/strings cannot preserve the native type.

**Exit:** all declared stored types work across creation, updates, rollback,
restart, verification, history and supported copy paths. Test malformed/truncated
payloads, depth/size bounds, decimal edge cases and pure/NumPy equivalence.

**Checkpoint C:** FP-5–6 close together with one documented format/type migration
plan and installed-wheel old/new-reader tests. This is a quality checkpoint, not
permission to erase an incompatible database or continue with mixed unsafe binaries.

### FP-7 — Writing procedures within transaction authority

Latest schema increment: [explicit schema authority](PROCEDURE_SCHEMA_AUTHORITY_V1.md)
adds schema_write, literal schema permission and max_schema_statements, native
CREATE DDL and implicit flexible CREATE/MERGE. Private schema/artifact journals
preserve whole-statement rollback; catalog-v2 custom index creation composes with
pending DML and original-snapshot OCC. [Qualification](../reports/FP7_SCHEMA_QUALIFICATION.md).
Compiled outer scans retain their table sets; v1 activation remains an explicit
prerequisite for mixed index DDL. General ALTER/DROP and continuation replanning
are not claimed. Full FP-8 and the remaining FP-6/profile work remain open.

Latest nesting/effect increment: [native procedure recursion](PROCEDURE_NESTING_EFFECTS_V1.md)
supports self/mutual calls, inherited max_call_depth and root native budgets,
shared per-name query/output/write counters, permission revalidation and outer
rollback. Deterministic callback behavior is now explicit, defaults to False and
is registry-derived for optimization/effect admission. [Qualification](../reports/FP7_NESTING_QUALIFICATION.md).
Schema-changing authority is added above within explicit native DDL limits; this is not FP-8.

Latest native query increment: [procedure query authority](PROCEDURE_QUERY_AUTHORITY_V1.md)
adds permissioned opt-in readers and writer.query(), native materialized scalar/entity
results and returning writes under the caller's snapshot, clocks, cancellation and
rollback. Query budgets accumulate across input rows. [Qualification](../reports/FP7_QUERY_QUALIFICATION.md).
Recursion, effects and bounded schema authority are added above; this does not
complete the full parity qualification.

Latest entity increment: [native entity signatures](PROCEDURE_ENTITY_SIGNATURES_V1.md)
adds NODE/RELATIONSHIP/PATH and typed node/relationship lists, including witnessed
entities nested in generic containers. Native references survive pending writes,
query composition and output materialization; detached metadata is not authority.
[Qualification](../reports/FP7_ENTITY_QUALIFICATION.md). Native query/result authority
and recursion are added above, followed by bounded schema-changing capabilities.

Latest value increment: [native procedure signatures](PROCEDURE_NATIVE_VALUES_V1.md)
now include all six native temporal families, LIST/MAP/ANY and both vector dtypes.
Inputs and results are recursively detached with type/size checks; typed and nested
temporal persistence reuses native capability admission, rollback and durable
recovery. [Qualification](../reports/FP7_NATIVE_VALUES_QUALIFICATION.md). Broader
capability composition and FP-6 parameterized/DECIMAL storage
remain open; this does not claim arbitrary signature or full procedure parity.

Latest bounded increment: [writing procedures](WRITING_PROCEDURES_V1.md) now perform
native result-free DML against declared node models and relationship pairs through
an explicitly permitted ProcedureWriter. Mutation/output budgets span invocations;
capabilities expire before downstream results and cannot commit, change store or
cross threads. Outer statement rollback includes nested effects and public result
failures. [Qualification](../reports/FP7_WRITING_QUALIFICATION.md). This is partial
FP-7 delivery at that checkpoint. Native schema authority, entity and
temporal/container signatures are covered by the subsequent increments above.

Completed bounded increment: [explicit unit procedures](UNIT_PROCEDURES_V1.md)
reuse immutable registration with an empty output schema, preserve incoming row
cardinality, require a None callback result and retain statement rollback.
513 affected tests pass; the separate original FP-7 selection now has 27 passes,
21 failures and four NUMBER fixture blockers. [Full-profile baseline and receipts](../reports/FP_FULL_PROFILE_20260912.md).
This supplies no writing authority and does not close FP-7.

The subsequent [native invocation increment](PROCEDURE_INVOCATION_V1.md) adds
declared implicit parameters, standalone default/wildcard outputs and proven
signature/name diagnostics. The original owner selection now passes 46/52 cases;
two FLOAT widening cases fail and four NUMBER fixtures remain unadmitted.
[Qualification and remaining scope](../reports/FP7_INVOCATION_QUALIFICATION.md).

The [numeric signature increment](PROCEDURE_NUMERIC_SIGNATURES_V1.md) subsequently
closes all **52 original FP-7 procedure cases**, with NUMBER input/output unions
and native validated integer-to-DOUBLE widening. 317 affected regression tests
pass. No profile waiver or fixture-value coercion was introduced. This closes the
original procedure family, **not** the broader FP-7 writing-capability package.

Extend registration with explicit read/write mode, typed signatures/output schema,
permissions, determinism/effect declarations and shared budgets. A writing callback
receives only a restricted transaction-scoped capability: it cannot commit, switch
stores, acquire a second independent writer, retain authority after return or bypass
the outer statement's rollback. Freeze `YIELD *`/named yield, unit procedures,
nesting and the new entity/value signature rules as explicit cases.

Check authorization/read-only mode before invocation. Validate every yielded value,
including unselected columns. Callback/iterator/final-output failures and close
errors must preserve the primary error and proven rollback. Never retry an entire
callback implicitly: host code may have nontransactional side effects. Document
that callbacks are trusted host code, not a sandbox; external network/filesystem
effects are neither intercepted nor rolled back by the database. Blocking host
code still cannot be forcibly preempted by cooperative query cancellation.

**Exit:** procedures can perform useful native graph writes through CALL/YIELD;
denied writes have zero effects; late failure rolls back all graph effects;
expired/foreign capabilities are rejected; concurrent writers/readers remain safe.

### FP-8 — Qualify the profile and update Pulse

Latest bounded consumer checkpoint: [current value and label-transfer qualification](../reports/FP_PULSE_CURRENT_VALUES_QUALIFICATION.md)
passes 255 grouped source tests and 65 privately installed adapter tests. Community
handles exact native JSON observations and refuses label loss in Pulse's declared
single-type artifact; Core is unchanged. The subsequent [operational qualification](../reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md)
adds 121 affected source/158 installed passes and actual source lifecycle,
Settings/schema, API/MCP/search UI and post-barrier recovery observations. Complete
Grafx regression, supplemental reconciliation and comparative evidence below
remain required; neither checkpoint alone closes FP-8.

Run the full frozen inventory and supplemental extension cases. Publish original
pass/fail/not-run counts and a separate profile result with every exclusion listed.
The [22-contract execution map](../conformance/EXTENSION_COVERAGE.md) identifies
the existing native suites and external acceptance evidence without adding scope.
Every required-profile case must pass: zero unclassified cases, unexplained
`not_run`, in-profile failures or expected-failure waivers. Every retained model
divergence needs an explicit counterexample and reason, not a renamed pass.

Execute equivalent bounded scenarios on fixed competitor versions. Use Ladybug
0.20.3 for continuity; choose and record one available Neo4j CE binary/image version
and digest at FP-1 rather than inventing an installed version from rolling docs.
Do not depend on availability of a competitor to run Grafx's independent oracles.
If that comparative execution is unavailable, withhold the corresponding parity
claim rather than reporting it as passed. Results need exact values/types,
multiplicity, error/effect contracts and a published dialect adaptation log.

For Pulse, map changed templates and result/value consumers before each affected
package lands: cancellation/stale enumeration, query executor, logical transfer,
related context/key decisions, KG paging/edges/counts, schema/Settings, recovery,
and JSON/API/MCP serialization. Core uses neutral contracts/DTOs and explicit
capability unavailability where appropriate. Native value/identity/procedure
implementation stays in Community. Do not expose arbitrary writing procedures to
Pulse users merely because the engine now supports them.

Accept coordinated breaking changes without a legacy query mode, but provide a
precise upgrade order, backing artifacts and safe refusal for incompatible storage.
Use temporary data and installed paired wheels for regression. Final Pulse coverage
must include API and real-browser checks on an isolated instance, not just React
mocks. Do not consolidate remaining production specs or reset the user's graph as
test preparation. Global installation, restart and publication remain separate acts.

**Exit:** full Grafx regression, frozen profile, fault/concurrency/format tests,
affected Pulse regression and paired-wheel checks pass; all public docs and the
comparison state exactly what was delivered and what remains different.

## 4. Test cadence and safeguards against moving targets

- Each small change runs focused positive, negative and relevant prior-regression
  cases. Cache **unchanged passing test evidence**, not query outcomes or authority.
- Group broad regressions at checkpoints B/C and final delivery; do not run the
  entire 17k+ suite after every scalar/parser edit. New WAL/format or rollback risks
  require their focused fault tests immediately, not only at final delivery.
- FP-1 establishes versioned family selections so every failed case has an owner.
  An in-scope defect discovered later is corrected within its owner package.
  Unrelated enhancements go to the existing roadmap and do not block this round.
- Scope is not reduced after failures appear. Changing a frozen exclusion or
  adding a new architecture requirement requires an explicit recorded decision.
- No per-percentage performance gate. Record representative small/large query,
  path, write and Pulse API timings with fixed inputs and operation counts. Address
  material regressions/unbounded allocations; do not keep reopening the delivery
  to chase marginal gains. Establish any quantitative thresholds before the change.
- Planned format/capability changes require coordinated admission of all readers
  and writers. They never justify weakening COMMIT proof, WAL barriers or fences.

## 5. Documentation, packaging and claim checklist

Every package updates its owned capability examples and restrictions in
`QUERY_LANGUAGE.md`, `COMPOSABLE_QUERIES.md`, `CYPHER_COMPATIBILITY.md` and
`API_REFERENCE.md`; `CONFIGURATION.md` only gains justified knobs with defaults,
bounds, costs and failure behavior. Reuse existing resource limits where possible.
Types also update `CONTRACT.md`, compatibility/upgrade, history, indexes and
interop docs; procedures update extension permissions/lifecycle documentation.
Check paths/signatures/examples with the documentation validator and executable tests.

Update `FEATURE_COMPARISON.md` when capabilities actually pass, not at plan approval.
Update ROADMAP package status and link immutable acceptance reports/artifact hashes.
Do not promote the current 0.0.6 development source to a published release in prose.
Release/branch allocation is intentionally not decided by this plan.

Permitted final claim: **functional parity for the published local-first profile,
on the tested versions and cases**. Forbidden shortcuts: “full Cypher”, “all TCK
passes”, “same performance as Ladybug/Neo4j”, or “production certified” based only
on a green internal test count. If the frozen profile still has a failing required
capability, report the delivery incomplete rather than redefining parity.

## 6. Primary references

- [openCypher TCK 2024.3](https://github.com/opencypher/openCypher/tree/2024.3/tck):
  upstream scenarios define initial graph, query, expected result/error and effects;
  this is why FP-1 must execute more than parse/read-only expressions.
- [Neo4j CALL subqueries](https://neo4j.com/docs/cypher-manual/current/subqueries/call-subquery/):
  reference for the separately frozen returning/unit/import extension profile,
  not an assertion that current rolling Neo4j equals the 2024.3 TCK.
- [Ladybug data types](https://docs.ladybugdb.com/cypher/data-types/): comparative
  source for exact decimal and nested stored values, not proof of identical DDL,
  binary representation or transaction semantics.
