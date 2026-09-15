# Grafx query compatibility contract

## Reference and scope

Latest complete native [V3 query-profile execution](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md):
**3,896 passed / one failed / zero not run**. All 3,896 required cases pass; the
only upstream failure is the explicit Set1 #0010 lists-of-maps divergence.
The earlier Graph3 #0009 error-phase failure is corrected and passes in this new
complete execution; its earlier failed receipt is preserved. Native transfer and its
[installed old/new-reader matrix](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
are qualified. The [final repository regression and supplemental reconciliation](reports/FP_FINAL_NATIVE_QUALIFICATION.md)
and [installed Pulse qualification](reports/FP_FINAL_PULSE_QUALIFICATION.md) now pass.
Neo4j execution was explicitly deferred by the user; the sole upstream divergence prevents
a full openCypher-conformance claim regardless of competitor availability.

Current capability entry points:

| Capability | Current native contract |
| --- | --- |
| Model and entity results | Typed/flexible heterogeneous properties; zero/one/multiple labels; qualified detached node/relationship/path identities. [Models and queries](QUERY_LANGUAGE.md), [entities](ENTITY_VALUES.md). |
| Expressions, patterns and paths | Native scalar/list/temporal families, exact source headings, polymorphic/optional patterns and bounded named trails. [Query reference](QUERY_LANGUAGE.md). |
| Subqueries and writes | Returning/unit writing CALL, scoped imports, read/write UNION and shared whole-statement rollback. [Composition](COMPOSABLE_QUERIES.md). |
| Stored types and consumers | Native temporals, DECIMAL and typed LIST/MAP/ARRAY/STRUCT; explicit support/refusal matrix and format admission. [Types](TYPE_SUPPORT.md), [upgrade](V006_COMPATIBILITY.md). |
| Registered procedures | Trusted typed read/write/unit procedures, nested query/schema authority, revocation and cumulative budgets. [Extensions](EXTENSIONS_AND_ARROW.md). |

The one authorized upstream divergence keeps native lists of maps instead of
rejecting them as Set1 #0010 expects. NaN is supported in expressions but not
persisted properties; infinity storage, unlimited traversal, inner transaction
commits, full APOC/GDS, Bolt drivers and all vendor-specific syntax are not claimed.
Use the [frozen requirements](conformance/PROFILE_V3.md) and
[delivery roadmap](../ROADMAP.md#functional-parity-expansion-plan) for acceptance.

## Historical implementation checkpoints

The following chronological receipts describe earlier candidates. Their words
"latest", "pending", "not run" and their family counts apply at that checkpoint,
not to the current capability table or complete V3 result above. Original failed
and schema-adapted observations are preserved rather than relabeled as passes.

The latest [procedure query authority](specs/PROCEDURE_QUERY_AUTHORITY_V1.md) adds
native read/results and returning-write access inside trusted permissioned
callbacks. [Nesting and effects](specs/PROCEDURE_NESTING_EFFECTS_V1.md) now add bounded
recursive CALL and explicit callback determinism. [Schema authority](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md)
adds permissioned native CREATE DDL, flexible schema and composable v2 indexes. This adds
functional coverage without changing the frozen TCK ledger or claiming full parity.

Native procedure signatures now include temporals, LIST/MAP/ANY and vectors with
recursive ownership and native persistence. A subsequent [entity contract](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md)
adds invocation-witnessed native entities; FP-6 typed collections/DECIMAL and broader
procedure authority remain open. [Value contract](specs/PROCEDURE_NATIVE_VALUES_V1.md),
[qualification](reports/FP7_NATIVE_VALUES_QUALIFICATION.md). This is additional
functional coverage, not a change to the frozen TCK counts/exclusions below.

The subsequent [writing procedure increment](specs/WRITING_PROCEDURES_V1.md) adds
explicitly permitted, transaction-scoped native mutations with shared budgets and
revocation. Its declared-model/result-free door is narrower than arbitrary writing
procedures. [Authority and regression evidence](reports/FP7_WRITING_QUALIFICATION.md)
supplements, rather than replaces, the original TCK results below.

The latest native numeric-signature follow-up passes **52/52 original procedure
cases**, with no failed or unexecuted selected cases. NUMBER inputs/outputs and
validated integer-to-DOUBLE widening close the six cases left by the invocation
checkpoint below. This does not certify writing callbacks or full Cypher parity.
[Numeric contract and receipts](specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md).

The subsequent native procedure invocation checkpoint records **46/52 passed,
two failed and four not run**. Standalone implicit arguments/default outputs and
`YIELD *` are implemented; in-query calls retain named outputs. Numeric procedure
signatures/coercion and writing authority remain pending. [Exact source and native
qualification](reports/FP7_INVOCATION_QUALIFICATION.md).

The latest **complete** frozen native inventory records **3,816 passed, 73 failed,
eight not run**. The required profile contains 51 failures; 22 other failures are
the already-recorded architectural divergences. A subsequent, separate unit-CALL
increment passes 513 affected source tests and records 27/52 passing procedure
cases, 21 failing and four not run. Neither result asserts full conformance.
[Complete baseline and follow-up receipts](reports/FP_FULL_PROFILE_20260912.md).

General fixed-length MERGE now supports unbound endpoints and multi-hop paths,
matching the whole pattern or creating every unbound entity. Independent native
tests cover partial matches, constraints, action visibility and transaction
recovery. The original MERGE family was rerun: still 70 passed, four required
label-action failures and one retained multiple-label divergence. This is no
additional full-TCK pass claim. [Contract and receipts](specs/GENERAL_MERGE_V1.md).

Latest family qualification after native written paths, binding diagnostics,
mutation-to-MERGE read phases and undirected MERGE/native endpoint functions:
CREATE **76 passed / zero required failures / two
retained divergences** (78 selected); MERGE **70 passed / four required failures /
one retained divergence** (75 selected). These are separate family selections,
not a combined or full-profile result. Large CREATE uses a native flat program,
not adapted source queries. [Evidence](specs/WRITE_PATTERN_CONTRACTS_V1.md).

Subsequent DELETE-family qualification passes **41/41 original cases**, including
native expression/path targets and error phases, with 3,856 outside selection.
This is a family result, not a new full-profile count.
[Contract and receipts](specs/DELETE_EXPRESSIONS_V1.md).

The post-large-CREATE FP-4 original owner selection recorded **245 passes, 40 required failures,
five retained divergence failures**, all 290 selected cases executed; 3,607 outside
selection. All 201 passes in the previous post-SET-map owner baseline still pass.
This is progress, not a conformant full profile.
[Receipt, remaining causes and source regression](specs/WRITE_PATTERN_CONTRACTS_V1.md#large-create-qualification-receipts).

The subsequent complete owner run with native EXISTS records **255 passes, 30
required failures and five retained divergence failures**, all 290 selected cases
executed. Every preceding 245 pass remains a pass. The 10 original EXISTS cases
also pass on final source after composed-list follow-ups. Pending required cases
are 29 label mutations and the existing Set1 #0010 nested-storage oracle conflict;
neither is silently waived. These results do not certify the full profile or
installed Pulse. [Exact scope and qualification](specs/EXISTS_SUBQUERIES_V1.md#qualification).

Native conditional `MERGE` property actions (`ON CREATE SET` / `ON MATCH SET`)
now run under the outer statement boundary, and bound-edge MERGE enumerates all
matching relationships. Whole-map SET replacement/overlay now also works in those
actions. The earlier whole-map checkpoint passed 55 of 75 MERGE cases (superseded
by the 70-pass family run above). SET passes 37 of 53, with 16 required failures: fifteen label
mutations and one negative case incompatible with authorized nested storage.
This is not a full-profile result. Labels and broader MERGE forms remain pending.
[Actions](specs/MERGE_ACTIONS_V1.md), [map contract and receipts](specs/SET_PROPERTY_MAPS_V1.md).

The latest FP-3 owner run passes **528/528 required original cases**, with 17
retained multiple-label divergences failing and 3,352 cases outside selection.
Independent node/relationship namespaces and `r:TYPE` predicates close Graph5
#0002 without changing the frozen oracle. Qualified transport/history/search and
24 installed catalog admission scenarios pass. Subsequent vector repairs and
projection/view/ledger ownership now have separate affected
regression and recovery evidence. Remaining consumers, Pulse and full-package
qualification remain open. [Exact receipt](specs/GRAPH_NAMESPACES_V1.md).

The latest complete FP-2 owner selection passes **1,976 of 1,976 required cases**,
zero failures, with 1,921 outside the selection. It is not a full-TCK result or
completed multi-package acceptance. The previous 65 temporal dependencies are
closed by native value/query/whole-entity integration, not reclassification.
All 131 original literal cases pass;
malformed numeric suffixes and unsupported ASCII query symbols carry native
cause/phase evidence. [Current receipts](conformance/FP2_PROGRESS.md).

The [temporal value foundation and 1,004-case integration matrix](specs/TEMPORAL_VALUES_V1.md)
now include public Python temporal parameter/results and typed/ANY/nested native
row storage, native constructors/scoped clocks, fields, arithmetic,
comparison/ordering, truncation/differences and toString. The original temporal
selection passes **1,004/1,004 cases**, with 2,893 outside selection; this is a
native query receipt, not a count inferred from component tests. Complete
index/history/transport and Pulse qualification remain implementation work.
[Current temporal API](TEMPORAL_VALUES.md),
[receipt and unchanged matrix](specs/TEMPORAL_VALUES_V1.md#current-evidence).
The final grouped query/tools/temporal regression passes 6,812 tests, zero
failures/errors/skips; [scope and receipts](specs/TEMPORAL_VALUES_V1.md#grouped-regression-trace).

Native entity collections support UNWIND → rematch/SET/DELETE with real identity,
including polymorphism, slices/comprehensions, bounded spill and repeated entities.
Implicit relationship MERGE adopts its newly created endpoint index authorities
without changing previously captured generations. All seven original List12 and
14 Unwind1 cases now pass. The two List12 failures recorded in the earlier
error-contract increment below are historical and closed by this increment.
[Contract](QUERY_LANGUAGE.md#native-entity-collections-and-unwind).

Projection-name conflicts, computed WITH items without aliases, row aggregates
inside list-local bodies/predicates and `size(path)` now carry native static
cause/phase evidence. Return4, With4 and List6 pass all 11, seven and 17 original
cases respectively. List12#0007 passed at that checkpoint; its two collected-node
write failures were closed subsequently, as recorded above.
[Error contract](QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

Grouped/DISTINCT RETURN can order compound expressions using complete projected
values, including aggregate results. Discarded inputs raise a native
`undefined_variable` planning cause; new aggregates are not silently introduced.
All five ReturnOrderBy6 and 14 ReturnOrderBy2 original cases pass. This is structural reuse,
not arbitrary aggregate-ordering or full Cypher conformance.
[Contract](QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

Projected maps retain nested access through aliases, parameters, CASE/NULL and
returning subquery boundaries. Planning resolves known selected paths without
executing their producers. The two original With2 cases and all 44 original map
expression cases pass natively; the targeted 922-test regression also passes.
[Contract](QUERY_LANGUAGE.md#values-and-python-mapping).

Scalar conversions preserve explicit runtime type causes, distinguish unsupported
types from unconvertible text, and never coerce graph entities through host callbacks.
Known homogeneous list-local types now support planning-time arithmetic checks;
heterogeneous/unknown inputs retain dynamic checks.
[Public type/phase contract](QUERY_LANGUAGE.md#conversion-and-arithmetic-error-contracts).
The focused original conversion and quantifier families pass 47 and 604 cases
respectively; the 5,013-test query/tools regression also passes. These overlapping
selections do not certify the whole TCK or close the remaining FP-2 work.

Native percentileDisc/percentileCont support exact interpolation/nearest-rank,
NULL/empty groups, DISTINCT, grouping and bounded external sorting. All 35 original
aggregation-expression scenarios pass, including the 13 previously blocked
percentile scenarios. [Contract and remaining limits](QUERY_LANGUAGE.md#percentile-aggregates).

RETURN aliases now participate in compound/property sort expressions with output
shadowing. Ambiguous grouping and nested aggregates carry precise native planning
causes, and grouped WITH resolves missing inputs before rejecting new aggregates.
All 20 original WithOrderBy4 cases pass; Return6 now passes all 21 original cases.
Direct volatile aggregate arguments refuse, with an explicit WITH-based recipe
for aggregating random samples. [Contract](QUERY_LANGUAGE.md#stable-aggregate-arguments-and-volatile-row-values).

Row-independent `SKIP`/`LIMIT` expressions now support scalar arithmetic/functions
and per-invocation nondeterminism. Native raise sites distinguish static literals
from parameter/computed runtime failures. All 31 original RETURN window cases
pass; WITH windows now pass all 9 original cases after plain ordering-scope repair.
[Contract](QUERY_LANGUAGE.md#row-independent-skip-and-limit-expressions).

Plain WITH ordering admits unexported source bindings temporarily, with projected
aliases taking precedence; downstream scope remains closed. Full WITH-ordering
conformance is still incomplete (227 original passes / 65 missing-function failures).
[Scope and remaining work](QUERY_LANGUAGE.md#with-ordering-scope).

Exactly projected subexpressions and aggregate results are now reusable in WITH
ordering/filters after DISTINCT/grouping, without reopening source rows. Plain
WITH-WHERE can read unexported input bindings until that stage ends. All 19 original
WITH-WHERE cases pass in the focused native run; source fixtures remain unchanged.

The active scope is now [profile V3](conformance/PROFILE_V3.md): 3,896 required
cases and one explicit Set1 #0010 nested-storage divergence. Multiple labels are
required implementation work. V1/V2 remain historical evidence; scope expansion
is not a passing result. Native `RETURN *` now resolves
visible variables across projections, UNION and returning read subqueries, with
source-proven empty-scope refusal. [Query contract](QUERY_LANGUAGE.md#returning-the-visible-scope).

The native Return2 family now passes all 18 original cases, including runtime
`DeletedEntityAccess` for properties/labels after owner deletion. Immutable
relationship type and counts remain valid; full instruction rollback is retained.
[Deletion contract](QUERY_LANGUAGE.md#content-access-after-deletion) and
[focused evidence](conformance/FP2_PROGRESS.md). This does not close FP-2.

Standalone node MERGE now emits all existing matches per input, including
unlabeled searches; an empty untyped match set creates a native unlabeled node.
The latest native MATCH/MATCH-WHERE selection reports **410 original passes** and
five failures, all involving multiple-label fixtures. All **364 V1-required cases**
in that selection pass without fixture-schema inference. This supersedes the
earlier 247 original / 117 adapted required-case result; it does not rewrite the
frozen V1 ledger or establish full conformance.
Match5 #0025–#0029 now pass with typed input-derived fixture proofs and native
runtime-bound writes. Match4 #0004 now passes its original fixture and query via
native label-free nodes, heterogeneous properties and automatic relationships.
The broader model integration is not complete. See
[MERGE contract](QUERY_LANGUAGE.md#node-merge-matching-and-creation) and
[evidence](conformance/FP3_PROGRESS.md#automatic-flexible-relationships-and-original-match4-closure).

Native WITH inference now preserves same-kind entity coalesce/CASE branches and
literal relationship lists for subsequent identity-constrained MATCH. Range-list
imports retain their kind. Match4 #0008, Match7 #0022 and Match9 #0006/#0007 pass
their original queries with explicit fixture-schema adaptation. This is not full
FP-3/profile acceptance; see [scope and proof limits](COMPOSABLE_QUERIES.md#clause-order-and-scope).

Explicit inverted intervals are native empty reads rather than parse errors;
they do not fabricate zero-hop paths. Match5 #0011–#0013 and malformed-range
Match4 #0009/#0010 now pass with recorded fixture-schema adaptation. Bounds,
query budgets and omitted-upper completeness refusal remain enforced. See
[traversal contracts](QUERY_LANGUAGE.md) and [FP-3 evidence](conformance/FP3_PROGRESS.md).

Native parsing accepts bidirectional read arrows as undirected segments and an
optional colon in relationship alternatives. Match3 #0008 and Match6 #0012/#0013
pass their original queries with recorded typed fixture adaptation. Bare map
parameters retain refusal but now carry a native InvalidParameterUse cause
(Match1 #0006 and Match2 #0008 original passes). These results do not close the
remaining required profile. See [syntax and identity rules](QUERY_LANGUAGE.md#read-arrows-and-type-alternatives).

Inline relationship property maps now execute as native read predicates in
typed/polymorphic MATCH, OPTIONAL, captured paths, predicates and comprehensions.
Annotated ranges require every edge to match; zero hops satisfy the map
vacuously. Original Match3 #0005 and Match7 #0011 queries now pass with explicitly
recorded fixture-schema adaptation. This is not full-profile acceptance. See
[map semantics](QUERY_LANGUAGE.md#inline-relationship-property-maps).

Incoming relationship variables now retain their qualified identity when
re-matched in a later MATCH/OPTIONAL clause, including within named paths.
Conflicting relationship types yield no match; a duplicate relationship name
inside one connected read pattern is a compile-time uniqueness error. These
are native semantic corrections, not rewritten TCK queries. Bound-edge direct
access optimization and broader bound-list patterns remain pending; see
[FP-3 evidence](conformance/FP3_PROGRESS.md).

The native 0.0.6 development line now declares a logical relationship type over
several endpoint pairs through `CREATE REL TABLE GROUP`. Reads/path values and
statically resolved writes retain the logical type and physical qualified
identity. The pinned `expressions/path` selection now has **4 original passes,
3 schema-adapted passes, zero selected failures/not-run**; the other 3,890 cases
are outside that selection. Path2 #0001/#0002 no longer require a renamed type
or query: the native DDL supplies their typed schema, explicitly recorded as an
adaptation. This is not full-profile acceptance. See
[grammar and remaining integration](QUERY_LANGUAGE.md#relationship-types-spanning-endpoint-tables)
and [evidence](conformance/FP3_PROGRESS.md).

FP-3 adds native `properties(node|relationship|map|NULL)`, `labels(node|NULL)` and
`type(relationship|NULL)`, with typed planning/runtime refusals and own-write
visibility. The subsequent [native-label checkpoint](specs/NODE_LABELS_V1.md)
adds complete versioned label sets, including multiple names or an empty set,
with native CREATE/MERGE and SET/REMOVE. Its FP-4 selection passes all 289 required
cases; the sole other selected case is the authorized list-of-maps divergence.
Original and schema-adapted graph-function TCK outcomes remain separate in
[FP-3 evidence](conformance/FP3_PROGRESS.md). Native MATCH/OPTIONAL now treat
absent-table reads as no match, preserving zero-length paths and static type
checks. A missing-label read does not create schema. See
[read semantics](QUERY_LANGUAGE.md#absent-tables-in-read-patterns).

FP-3 now returns qualified `NodeValue`/`RelationshipValue` objects from native
execute/cursors, including nested values, entity UNION and aggregate/sort/DISTINCT
spill. This changes
the Python result representation, not the schema-free storage policy or the
remaining generalized path requirements. See [entity contracts](ENTITY_VALUES.md)
and [working evidence](conformance/FP3_PROGRESS.md); this is not full conformance.

The admitted typed bounded named path now returns native `PathValue`, including through
UNION and cursors. `nodes()`/`relationships()` return the same entity DTOs as
direct projections. This removes the old metadata-map output and structural-key
property restrictions. Single-hop captures now compose with predicates, WITH,
UNWIND, row windows, DISTINCT/order, typed OPTIONAL MATCH and returning subqueries,
including path-or-NULL UNION exports. Reverse/undirected walks preserve physical
endpoint identity. Explicit typed ranges now support zero length, cycles and
multiple segments with relationship uniqueness, using depth-first streaming.
Written range variables hold relationship tuples even for `*1..1`.
Untyped/type-alternative single-hop segments now support native capture and
composition, without a literal Pulse query recognizer. Bounded variable ranges
over multiple relationship tables now use the same native trail identity and
snapshot contract, including zero length and explicit completeness refusal.
Omitted upper bounds preserve
omission. Node-only named capture (`MATCH p=(n)`) now produces a native zero-edge
path with qualified node identity, including polymorphic/optional composition.
Omitted-upper traversals preserve
omission and refuse a valid continuation beyond the 30-hop resource ceiling,
instead of silently truncating at 20. This is not unlimited traversal.
The original expressions/path diagnostics remain
in the working evidence rather than being presented as passed.

The original 12 UNION-family cases now yield 10 original passes and 2 passes with
declared typed-fixture setup adaptation, zero selected failures/not-run. The
reference's compile-time column mismatch and mixed-UNION-policy errors are
explicit native refusals, not query rewrites. One chain now uses one duplicate
policy; separate returning subqueries can compose UNION with UNION ALL. This is
a documented breaking change from the earlier mixed-chain extension, not a
full-profile acceptance claim.

Current FP-2 development evidence additionally covers all 67 original List11
range cases and all 27 Literals5 float cases: evaluation-phase argument errors,
bounded streaming/materialization, long finite DOUBLE spellings and overflow
refusal. See [working evidence](conformance/FP2_PROGRESS.md). This focused result
does not replace the incomplete owner-wide/profile inventory or qualify all FP-2.

The subsequent increment passes all 46 List5 membership cases and implements
native `WITH *` scope expansion. Three conversion scenarios now advance past
parsing but still fail the composed polymorphic-MATCH restriction assigned to
FP-3. That intermediate receipt remains historical evidence. The first FP-3
increment now passes those three queries with explicit fixture-schema adaptation;
the conversion family has 21 original passes, four adapted passes and 22 selected
fixture blockers. See [FP-3 progress](conformance/FP3_PROGRESS.md); broader entity,
path and final profile qualification remain incomplete.

The authorized language round uses the **openCypher TCK 2024.3**, commit
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`, as a fixed semantic reference.
See the [upstream TCK](https://github.com/opencypher/openCypher/tree/2024.3/tck)
and its Apache-2.0 license and attribution notice. This is not a claim that
Grafx already passes that TCK or implements the entire Neo4j product.
The reference does not move when Neo4j releases new syntax.

Grafx retains typed node/relationship tables, explicit schema, local-first storage,
multi-reader/multi-writer OCC and durable WAL recovery. Schema-flexible storage,
arbitrary label sets, Neo4j wire protocols, APOC and GDS are not included in this
language round. Scenarios dependent on those differences must be reported as
model divergences, not counted as passes. Modern `CALL { ... }` extensions must
be documented separately from reference conformance and from procedure `CALL`.

## Delivery checklist

The [acceptance record](reports/V006_QUERY_LANGUAGE_ROUND.md) maps each item to
feature tests, corrective findings, consumer migration and packaging evidence.

All eight items are **implemented, tested and documented in development** within
the finite boundaries below. The final complete Grafx run passed 17,174 tests with
zero failures/errors and 19 attributed skips. Affected Pulse tests and installed
wheel tests also passed; see the acceptance record for their separate coverage.
This is not a release or full-language conformance claim.

| Order | Work | Acceptance boundary |
|---|---|---|
| 1 | Reference contract and TCK | Pinned, reproducible inventory; execution evidence distinct from parse acceptance; explicit missing/divergent cases |
| 2 | Expression semantics | NULL, lists/maps, equality, short circuit, aggregation, scope and errors have deterministic tested contracts |
| 3 | Composable execution | Ordered clauses share operators and resource budgets rather than query-shape bypasses |
| 4 | MATCH / OPTIONAL MATCH | Correlation, multiplicity, filtering and null extension work in composed clauses |
| 5 | UNION / subqueries | Multiple branches, column/scope rules and subquery composition are tested |
| 6 | Functions / paths | Documented expression families with runtime, type, NULL and bound checks |
| 7 | Composed writes | Atomicity, read-your-own-writes, failure rollback and concurrent readers/writers preserved |
| 8 | CALL / YIELD | Typed tabular registry, composition, permissions and budgets; no arbitrary code evaluation |

## Reproducing the inventory

Install the development extra. Check out the exact upstream tag outside the
distributed Grafx package, then run:

```console
git clone --depth 1 --branch 2024.3 https://github.com/opencypher/openCypher.git .grafx-tmp/opencypher-2024.3
python tools/check_opencypher.py --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/opencypher-inventory.json
```

The tool verifies HEAD and rejects locally modified feature files. Cucumber's
Gherkin compiler expands outlines and backgrounds. Every case receives a stable
feature-relative ordinal, source checksum, original steps and parse diagnostics.
`conformance: not_run` is deliberate: parsing alone is never a successful semantic
test. Negative scenarios also require the expected error phase and no side effects.
No runtime dependency on Cucumber is added to Grafx.

## Breaking changes and Pulse

The user authorized replacing incompatible language policies without a legacy
switch. Pulse is the only current consumer. Its Core must remain graph-provider
agnostic: shared query semantics may change, backend-specific implementation stays
in Community adapters. Existing unrelated Pulse changes must be preserved.

Initial migration audit found source-reference splitting in Core
`events/handlers/cancellation_decay.py` and `kg/canonical_stale_reconciler.py`.
Their list positions have moved to the zero-based contract in the active consumer
pair: `okto-pulse-core-kg5-codex` and `okto-pulse-v003-kg-load-codex`. Four real-query
tests in Community `test_grafx_query_semantics_migration.py` passed, covering owner
IDs, child sources, card aliases, malformed references and keyset pagination.
The original consumer run passed all **191 tests** across this migration,
query execution, key decisions,
reflective queries, read lanes, transactions, relationship queries, related-context
filters and replacement atomicity. The historical 23-case stale-sweep run used
`--noconftest`; it is superseded by the [paired consumer
follow-up](reports/PULSE_V006_INTEGRATION_REGRESSION.md), which fixes the shared
bootstrap through the neutral schema port. The [FP-4 source
qualification](reports/FP4_PULSE_SOURCE_QUALIFICATION.md) tracks subsequent native
entity/temporal conversion and updating CALL/UNION in Community, with normal
Core conftest. Neither follow-up represents the full historical Core suite or
restores a removed backend to bypass failures.

Community's dependency is pinned to `okto-grafx[accel]==0.0.6`: do not install the
changed owner-reference queries with an older Grafx runtime. This is a coordinated
source/dependency update, not a global Pulse installation. Grafx 0.0.6 was published on
PyPI on September 13, 2026, so integration builds can resolve the published wheel;
locally built 0.0.6 wheels were only required before that.

Validated policies in this development round:

- List position `0` selects the first element; out-of-range returns NULL.
- Map keys are case-sensitive, dot/bracket missing keys return NULL.
- CASE and COALESCE only evaluate the selected/needed runtime expressions;
  static name/type checks remain mandatory for all written expressions.
- Simple CASE does not equate NULL with NULL. Recursive list/map query equality
  uses three-valued logic; DISTINCT grouping remains a separate operation.
- Numeric equality and the hashed `IN` path do not round INT64 through DOUBLE.
- WITH aliases derived from parameters participate in pre-execution type binding.

No configuration flag re-enables the old policies. Persistent pages, WAL,
transaction modes and connection defaults are unchanged by these language edits.

The inventory currently contains **220 feature files / 3,897 expanded cases**.
`--execute-reads --feature-prefix expressions/` also runs positive read scenarios
whose steps the adapter fully understands through a public read transaction. Other
fixtures and negative error taxonomies stay `not_run`, with reasons. Failures are
retained; this is a diagnostic baseline, not a blanket TCK pass.

### Executed diagnostic baseline

Historical read-only baseline, superseded for current native query qualification
by the complete V3 execution above. The table below records that diagnostic's
then-observed gaps and subsequent intermediate notes; it is not a list of current
missing temporal, schema-free, multiple-label or native-path functionality.

The expanded positive-expression run currently records **1,004 passed / 1,077
failed / 1,816 not run** across the same 3,897-case inventory. Reproduce with
`--execute-reads --feature-prefix expressions/`; the exit code is nonzero when
any executed case fails. This is not a release pass percentage: unexecuted
fixtures/error taxonomies are not coverage, and unsupported language remains failed.
The accepted queries in this run had no remaining result-value mismatches after
the fixes to list ordering, mixed min/max and operator precedence. Five cases
still differ in unaliased expression-column spelling; use explicit aliases.

| Status | Boundary still not equivalent to the pinned reference |
|---|---|
| Model divergence | Typed schema/columns, immutable typed relationship endpoints, explicit tables and vector spaces; no arbitrary label sets/schema-free CREATE |
| Explicit execution divergence | Expression NaN from floating zero/zero is supported; integer division by zero, nonzero floating division by zero and finite-input math domain errors still refuse. NaN/infinity storage is prohibited; traversal/list/query resources remain bounded. |
| Missing expression syntax/functions | Reference temporal constructors/property syntax (`date`, `time`, `datetime`, local variants, `duration`), `rand`, hexadecimal/octal literal spellings and chained comparisons |
| Public result contract | Unaliased expressions preserve submitted source spelling; node/relationship UNION results use qualified detached identities instead of ambiguous table-local IDs. Native path migration remains pending. |
| Remaining query composition qualification | Returning/unit writing calls, persistent explicit/wildcard imports, branch-local leading-WITH imports and updating/unit UNION branches are native. Bounded graph-writing callbacks now have an explicit [authority contract](specs/WRITING_PROCEDURES_V1.md); broader signatures/capabilities and complete FP-4/Pulse qualification remain open. Wider patterns and full-profile accounting retain their tracked FP-3/FP-8 scope. [CALL contract](COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries), [import scope](COMPOSABLE_QUERIES.md#subquery-import-scopes), [updating UNION](COMPOSABLE_QUERIES.md#updating-union-branches) |
| Unmeasured | TCK graph fixtures and negative error-phase/side-effect scenarios not implemented by the adapter; their `not_run` records remain explicit |

These boundaries are not hidden passes and not a claim that the entire language
is complete. The eight delivery criteria above apply to the documented finite
round, with full Grafx regression and the affected real Pulse query contracts.

## Planned functional follow-up

Historical planning/qualification trail. The original scope was subsequently
implemented and required-query-qualified as summarized at the top; the active
[functional parity plan](specs/FUNCTIONAL_PARITY_PLAN.md) and roadmap retain the
remaining integrated acceptance work. These earlier partial counts are not
current failures or additional architecture exclusions.

The latest frozen-ledger expression-owner check passes all **69 required FP-3
expression cases**: 38 original and 31 with explicit typed-schema adaptation.
Native node-label predicates and initial typed CREATE schema preparation close
the last two blockers in that selection. The full 118-case owner selection also
retains five failures and 44 not-run from existing architectural divergences;
those are not passes. The complete FP-3 clause profile and broader parity plan
remain unfinished. See [exact receipt and coverage](conformance/FP3_PROGRESS.md#node-label-predicates-and-expression-owner-checkpoint).

The native existential pattern-predicate family now passes all **39 Pattern1
cases**: 19 original passes and 20 passes with recorded typed fixture schema.
Queries/results and negative error expectations are unchanged. The five required
Pattern2 comprehension cases also pass, all with recorded typed fixture schema.
Six other Pattern2 cases retain their already-frozen schema/model divergence;
they are not execution passes. The broader FP-3/profile remains incomplete.
See [predicate semantics](QUERY_LANGUAGE.md#existential-pattern-predicates).

Same-pattern CREATE node references now support native self-loops and cycles.
The comparison selection passes all 52 cases (46 original, six with explicit
typed fixture schema), including the formerly failing self-loop fixture. This
does not certify the remaining pattern/create families or the full profile;
see [FP-3 receipts and remaining work](conformance/FP3_PROGRESS.md).

The [functional parity plan](specs/FUNCTIONAL_PARITY_PLAN.md) addresses the remaining
expression, subquery, pattern/path, entity-result, persisted-type and procedure
capabilities together with stateful TCK execution. Implementation is authorized;
FP-1 checkpoint A is recorded and FP-2 is in progress, with hexadecimal/octal
INT64 literals, source-faithful unaliased headings, expression-result postfix
planning/binding, adjacent comparison chains and nondeterministic rand implemented.
The next correction adds strict BOOL/NULL logical operands and type-distinct
scalar literal expression identity in aggregate projections. It also proves
native grammar/type error phases and durable reopen for zero-effect write attempts.
The boolean family has 141 original passes with nine remaining failures; the
[FP-2 evidence](conformance/FP2_PROGRESS.md) records exact remaining causes,
owner-wide diagnostic counts. The subsequent user decision enables expression NaN
without storage: all eight original NaN comparison cases pass, without ledger exclusions.
Empty map keys now close the remaining required boolean cases (149 passes; the
one excluded unlabeled fixture remains unexecuted). Bounded direct UNWIND range
also passes the original million-span/3,000-row sum without a larger allocation
quota. Scope/aggregation context errors now have native planning evidence;
entity-dependent and other required families still prevent package acceptance.
The four quantifier families involving rand passed all 64 original cases; remaining
required negative contracts and cross-package dependencies still need work.
Full functional parity is not yet accepted. The
[working case ledger and stateful-runner contract](conformance/README.md) do not
change the historical results or exclusions recorded above. Package status belongs
to the [roadmap](../ROADMAP.md#functional-parity-expansion-plan).

The earlier 191-Community/23-Core consumer numbers above describe the initial
language checkpoint. The subsequent [Pulse integration regression](reports/PULSE_V006_INTEGRATION_REGRESSION.md)
passed 1,575 Community, 111 Core and 252 frontend tests, plus six repeated tests
against installed wheels. That follow-up removed the shared Core bootstrap's eager
legacy import; other historical modules retain direct old test-helper imports.
Neither checkpoint represents an entire Pulse test-inventory pass.
