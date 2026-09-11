# Grafx query compatibility contract

## Reference and scope

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
Untyped/type-alternative capture and the legacy omitted-upper-bound policy remain pending.
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
The combined latest consumer run passed all **191 tests** across this migration,
query execution, key decisions,
reflective queries, read lanes, transactions, relationship queries, related-context
filters and replacement atomicity. These batches do not certify the full Core
suite: its existing bootstrap still imports legacy modules removed by the current
Grafx-only Community. Separately, all **23 stale-sweep Core unit tests** passed
without the unrelated legacy bootstrap (`--noconftest`), using an explicitly
composed `CoreSettings()` snapshot and the tests' existing fake ports. This is
unit evidence, not a claim of end-to-end Core-suite acceptance. No removed adapter
was restored to bypass that limitation.

Community's dependency is pinned to `okto-grafx[accel]==0.0.6`: do not install the
changed owner-reference queries with an older Grafx runtime. This is a coordinated
source/dependency update, not a global Pulse installation or a PyPI publication.
Until release, integration builds must resolve the locally built 0.0.6 wheel.

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
| Explicit execution divergence | Finite arithmetic only; division by zero/domain errors refuse rather than publishing NaN/infinity; bounded traversal/list/query resources |
| Missing expression syntax/functions | Reference temporal constructors/property syntax (`date`, `time`, `datetime`, local variants, `duration`), `rand`, hexadecimal/octal literal spellings and chained comparisons |
| Public result contract | Unaliased expressions preserve submitted source spelling; node/relationship UNION results use qualified detached identities instead of ambiguous table-local IDs. Native path migration remains pending. |
| Missing query composition | Unit/writing subqueries, implicit WITH imports, unrestricted polymorphic patterns, general named variable-length paths and graph-writing callback procedures |
| Unmeasured | TCK graph fixtures and negative error-phase/side-effect scenarios not implemented by the adapter; their `not_run` records remain explicit |

These boundaries are not hidden passes and not a claim that the entire language
is complete. The eight delivery criteria above apply to the documented finite
round, with full Grafx regression and the affected real Pulse query contracts.

## Planned functional follow-up

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
owner-wide diagnostic counts and the unresolved NaN profile decision.
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
