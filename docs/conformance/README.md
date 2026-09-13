# Functional parity conformance tooling

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Agreed scope and DoD](../specs/FUNCTIONAL_PARITY_PLAN.md) ·
[Historical results and public query contract](../CYPHER_COMPATIBILITY.md)

## Status: checkpoint A recorded; functional parity remains in progress

**Active requirements are [profile V3](PROFILE_V3.md): 3,896 required cases and
one explicit Set1 #0010 native nested-storage divergence.** Multiple labels are now
required, including the 22 former exclusions. V1/V2 accounts below are historical;
source expectations and observations remain unchanged. V3 verification uses its
V2 predecessor and V1 ancestor. Scope expansion does not assert new passes.

Latest [complete native query-profile run](../reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md):
**3,896 passes, one failure, zero unexecuted cases**. All required V3 cases pass.
Set1 #0010 remains the sole upstream divergence; the runner honestly exits 1.
The corrected Graph3 phase now passes in a new complete execution, not a renamed
historical receipt. The [final full repository/supplemental qualification](../reports/FP_FINAL_NATIVE_QUALIFICATION.md)
and [final paired Pulse qualification](../reports/FP_FINAL_PULSE_QUALIFICATION.md)
now pass their scopes. Neo4j execution was explicitly deferred by the user and
does not block this delivery; no Neo4j observed result is claimed.

## Historical owner and implementation checkpoints

The earlier counts and former pending statements below apply to their recorded
candidates, not the current complete V3/native/Pulse results above. Original
observations, including failures and divergences, remain unchanged.

The [native namespace increment](../specs/GRAPH_NAMESPACES_V1.md) passes all 528
required FP-3 cases, with 17 retained multiple-label divergence failures and
3,352 outside selection. This is an owner-scoped query receipt, not full-profile
or package acceptance; transport/index/consumer qualification remains open.

The native temporal-family selection now passes **1,004/1,004 original cases**,
with 2,893 outside selection. It includes native query execution, not merely
component-function matching. [Matrix and exact receipts](../specs/TEMPORAL_VALUES_V1.md#current-evidence).
Full FP-5 consumption/format qualification and complete profile acceptance remain
open; do not infer the FP-2 owner result from this separate family selection.
The independently executed [FP-2 owner selection](FP2_PROGRESS.md#latest-owner-wide-checkpoint-temporal-dependencies-closed)
also passes all 1,976 required cases, with 1,921 outside selection.

[FP_CASE_LEDGER_V1.json](FP_CASE_LEDGER_V1.json) records all **3,897** expanded
cases from **220** unchanged feature files at upstream revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`. Each entry records the case checksum,
source-file checksum, primary package owner, capability, original expected error
phase and effect counts, baseline observation and explicit inclusion decision.
Original queries/fixtures/parameters/results remain in the pinned upstream source
and generated full inventory; checksums bind these expectations without rewriting
them. Cucumber-generated AST IDs do not affect semantic checksums.

The [checkpoint A decision](CHECKPOINT_A.md) freezes **3,470 required cases** and
**427 source-reviewed architectural divergences** within the approved model
boundaries. Divergences remain inventoried and never count as passes. The
[22 supplemental contracts](EXTENSION_SCENARIOS_V1.json) retain the functional
requirements beyond schema-free upstream fixtures; they are not execution evidence.
The [execution coverage map](EXTENSION_COVERAGE.md) links all 22 to native suites
and outstanding external qualification. Its seven-test literal-query receipt does
not stand in for the compound contracts or final integration regression.
A family owner is not a diagnosed root cause. No percentage/conformance claim follows
from these counts. The frozen ledger's baseline is inventory-only (`not_run`);
historical 1,004/1,077/1,816 observations remain in the linked compatibility report,
not substituted for fresh execution evidence.

The subsequent [authorized model expansion](../specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties)
adds native label-free nodes and heterogeneous persisted properties. V1 remains
historical frozen evidence; the first [source-reviewed successor](PROFILE_V2.md)
reclassifies affected divergences. Match4 #0004 remains required and now passes
without fixture adaptation through native flexible nodes/relationships. This is
not completion of the broader model acceptance matrix; see the
[implementation receipts](FP3_PROGRESS.md#automatic-flexible-relationships-and-original-match4-closure).

Native implicit single-label creation now removes the need for schema inference
in the latest MATCH/MATCH-WHERE selection: 410 original passes and five
multiple-label fixture failures, including 364/364 selected V1-required passes.
The other 3,482 cases are outside that run. Historical V1 divergences include 46
of those native passes; V2 changes their required status without rewriting that
historical run. See [current model contract](../architecture/FLEXIBLE_GRAPH_V1.md).

| Primary package | Cases |
|---|---:|
| FP-2: expression/clause semantics | 1,976 |
| FP-3: entity/pattern/path semantics | 545 |
| FP-4: updates and subqueries | 290 |
| FP-5: temporal families | 1,004 |
| FP-7: procedures | 52 |
| FP-8: integrated use cases | 30 |

Ownership by upstream family does not imply that cross-family dependencies are
absent. In particular expression families can embed temporal/entity values. FP-6
stored types and modern FP-4/FP-7 extensions need separate frozen scenarios:
zero directly owned upstream cases must not be interpreted as zero work.

All **15 step families** are inventoried. They include 933 setup-query occurrences,
33 control queries, 695 expected errors, 250 effect-count assertions, 19 named
graph fixtures and 50 procedure fixtures. Named graph scripts are additionally
bound by checksum in the inventory and ledger, and modified upstream graph files
refuse inventory admission. Unknown steps or capabilities refuse
ledger generation; missing/duplicate cases and changed expectations refuse ledger
verification. The ledger's package/profile decisions are review artifacts, not
cryptographically authorized execution policy.

## Runner contract

`tools/tck_stateful.py` validates supported steps and literal expectations before
issuing any fixture/query. It supports sequential setup and operation/control
queries, scalar/list/map parameters, ordered/bag and list-order-insensitive
results, empty results, exact error type/detail/phase comparison and side effects.
List-order-insensitive comparison still preserves duplicate counts and column
positions. `tools/tck_values.py` independently parses expected nodes, relationships,
directed paths, nested containers, primitive and IEEE special values from the TCK
notation. Supporting these oracle literals does not admit NaN/infinity into native
storage. Function calls and executable expressions cannot execute through this parser.
Native entity result normalization remains FP-3 work; a mismatched result fails.

`tools/tck_native.py` owns one temporary database per case. It accepts no user
database path. Setup and successful mutations use public write transactions;
control queries use read transactions. Independent fresh-transaction
`scan_rows_v1` pages observe identities, relationships/endpoints, labels actually
present and non-null properties. Endpoint storage columns are not graph properties.
Before/after multiset differences detect additions/removals and changed values,
not just net object counts. A failed operation must leave the same observed graph.
Mutation scenarios close and reopen the database and compare its durable state.
This includes parsed write attempts that fail or affect zero rows, even without
a setup fixture. The native backend inspects updating clauses, UNION branches and
nested subqueries; procedure calls conservatively request the same durable check.
The flag is observation metadata, not permission to write. Parser-refused text
never opens an execution transaction. Scalar read-only cases do not incur this
additional reopen. Tests inject effects appearing only after reopen and require
the verifier to fail rather than accept the earlier empty in-memory observation.

Error equivalence is intentionally strict: lexer/parser/analysis boundaries establish
compile phase. A narrow mapper covers proven invalid Unicode, integer/float overflow,
malformed Unicode escapes and unknown functions, using actual native fields/signatures,
never the expected scenario error. Unmapped details retain their native code. Other
proven mappings include grammar-token mismatches and property/subscript type errors
with explicit native reason/phase evidence. Parameter binding belongs to execution
even when it precedes rows/effects. Unmapped engine failures report phase `unknown`
and cannot satisfy an expected runtime error.
Broader phase/detail mapping remains work assigned to the affected packages. Unexpected execution/observation
exceptions fail the scenario; they are not retrospectively changed to `not_run`.

An explicit typed-schema fixture can be supplied to the development backend. With
`--infer-fixture-schema`, typed CREATE fixtures and MATCH/DELETE/CREATE setup
pipelines can have scalar column types inferred from inputs, never expected results. Fixture
queries are unchanged and no primary keys/values are invented. Label-only node
tables receive an unset nullable layout column (no added graph property), recorded
in the adaptation. Fixed relationship endpoint pairs can use native logical
relationship-group DDL, explicitly recorded with catalog-v2 activation.
MATCH type proofs use previously declared node/edge schema, fixed-hop endpoint
constraints and alias domains. Direct edge endpoint-pair correlation is retained
for reversal rather than replaced by a Cartesian product of labels. Property
reads and statically typed STRING/numeric addition may infer stored column types;
no predicates, random functions or setup expressions are executed to guess them.
Other nonliteral computations, optional/ranged setup binding proofs, unresolved
types and label-free/multilabel fixtures still refuse that optional inference
route. Native execution without the flag now admits unlabeled and implicit
single-label flexible models directly; multiple labels remain unsupported.
The
query under test remains unchanged; such a successful scenario reports
`adapted_passed`, never upstream `passed`. Named graph scripts are loaded unchanged
from the checksummed inventory. Relationship types spanning several endpoint pairs
are not rejected merely for that multiplicity; the inputs must determine all pairs
and compatible stored columns. This is not blanket admission of every named fixture.

On an otherwise empty graph, one CREATE-only action with explicit typed patterns
and literal scalar properties may supply initial schema even if it has RETURN.
Only input pattern declarations are used: the action is not executed early,
RETURN/expected values are not schema clues, and original effects/results remain
the oracle. Receipts label this **initial CREATE schema adaptation**. Existing
fixtures and other/multiple action pipelines retain their admission. Unavailable
action inference runs the original action without invented schema, not as a new
runner exclusion. See [coverage and evidence](FP3_PROGRESS.md#node-label-predicates-and-expression-owner-checkpoint).

Table-defined procedure fixtures register trusted scalar-signature callbacks with
exact input filtering and duplicate output preservation. No-argument unit fixtures
with an explicit empty table now use native zero-column callbacks returning None,
without fabricated columns or rows. Native standalone implicit invocation and
output expansion now pass; native NUMBER signatures and integer-to-FLOAT widening
subsequently close **52/52 original procedure cases**. Broader writing/value
contracts remain FP-7 work. [Numeric qualification](../specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md),
[prior invocation evidence](../reports/FP7_INVOCATION_QUALIFICATION.md),
[complete baseline and unit follow-up](../reports/FP_FULL_PROFILE_20260912.md).
Named/procedure fixtures are prevalidated before database creation. General fixture
admission, temporal value integration, complete negative taxonomy coverage,
execution of the supplemental extension scenarios remain pending. The architectural
decision is frozen at checkpoint A; further exclusions require an explicit decision.

## Comparative references

[REFERENCE_VERSIONS_V1.json](REFERENCE_VERSIONS_V1.json) pins openCypher 2024.3,
Ladybug 0.20.3 and Neo4j Community 5.26.0. The Neo4j official registry's manifest
was resolved with `docker buildx imagetools inspect`: both the multi-platform index
and Linux/amd64 image digest are recorded. This is availability evidence only;
the comparative execution remains `not_run`. It does not require changing any
production Docker deployment. The [official image source](https://github.com/neo4j/docker-neo4j-publish/tree/f82529102b6fcdd531cd088947f374ac7467958a/5.26.0/bullseye/community)
is pinned to the revision reported by that manifest.

## Reproduction

### Initial working evidence

Pre-commit checkpoint validation: **341 passed**, zero failures/errors/skips
(`.grafx-tmp/commit-parity-checkpoint.xml`, 6.48 s), covering the conformance
tooling, numeric literals and existing lexer/parser tests. The unchanged upstream
octal family passed **10/10** (`.grafx-tmp/commit-fp2-octal.json`); the other 3,887
cases were outside that execution filter. Documentation checks and focused Ruff
checks passed. This is a focused checkpoint, not a fresh full-engine regression
or acceptance of the remaining FP-2–FP-8 work.

Latest fixture/oracle/registry/error-mapper regression plus existing query/procedure
tests: **179 passed**, zero failures/errors/skips, 10.92 s
(`.grafx-tmp/fp1-fixture-regression-final.xml`). Coverage includes exact unchanged
fixture scripts, no invented primary keys, named-graph checksum tampering,
procedure input filtering/duplicate output preservation, malformed table rows,
node/relationship/path literal parsing and native compile-phase error evidence.
One initial test incorrectly supplied a nonrectangular Gherkin source table (which
Gherkin correctly rejects before the registry); the corrected test injects the bad
width into a compiled fixture to verify the intended boundary. No skip was added.

Additional actual upstream diagnostics, not acceptance gates:

| Family | Passed | Failed | Not run within family | Receipt |
|---|---:|---:|---:|---|
| Procedure calls (52 cases) | 19 | 25 | 8 | `fp1-call-fixtures.json` |
| Triadic selection / named trees (19 cases) | 0 | 10 | 9 | `fp1-triadic-fixtures.json` |

The eight unexecuted CALL cases require unit or NUMBER procedure signatures. The
nine unexecuted tree cases require one relationship type with differing endpoint
table pairs. All remain visible in the frozen ledger; execution failures are not
marked passed. Ten tree-1 scenarios now reach the unchanged operation under test
after loading the graph; their pattern/query failures remain assigned to later
native-language work. Each JSON still accounts for the entire 3,897-case inventory.

Earlier initial checkpoint (retained for traceability):

The focused runner/ledger plus prior query-profile regression passed **99
tests**, zero failures/errors/skips (`.grafx-tmp/fp1-runner-regression-final.xml`, 6.54 s).
It includes native unchanged typed-fixture queries, late write failure isolation,
scan-based edge/property observations and durable reopen, alongside deliberately
wrong fake-backend results/effects/error phases that the verifier must reject.

The first actual stateful upstream mathematical slice ran all six cases:
**2 passed / 4 failed**, with 3,891 cases outside that slice still not run
(`.grafx-tmp/fp1-stateful-mathematical.json`). The four failures are retained:
one schema-free fixture cannot be loaded yet, one precise Unicode error detail is
not mapped, and two unaliased expression-column names differ. This small sample
is diagnostic evidence, not acceptance or a reason to remove those cases.

### Commands

Install the project's development dependencies and check out the pinned TCK as
described in the compatibility document. From the repository root:

```console
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp1-inventory.json --ledger-output .grafx-tmp/fp1-draft-ledger.json
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp1-check.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V1.json
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp1-stateful.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --infer-fixture-schema --feature-prefix clauses/call/
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp2-owner-v3.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V3.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V2.json --ancestor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-2
python -m pytest tests/tools/test_tck_ledger.py tests/tools/test_tck_stateful.py tests/query/test_compatibility_profile.py
```

Execution filters restrict only execution, never inventory. Failed executed cases
are never excluded based on the owner filter. `--owner FP-1` through `FP-8`
requires `--verify-ledger` and uses its recorded ownership, intersected with any
`--feature-prefix`. `execution_selection` records the filters and selected count;
all 3,897 source cases remain in the report. This filter does not omit declared
model divergences or turn unexecuted cases into passes. Failed executed cases
produce a nonzero exit; unexecuted cases remain counted separately. `--execute-reads`
retains the historical read-only diagnostic; it cannot be combined with
`--execute-stateful`. These are development tools, not new database settings or
runtime public API. No production Pulse installation or data mutation is involved.
