# Functional parity conformance tooling

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Agreed scope and DoD](../specs/FUNCTIONAL_PARITY_PLAN.md) ·
[Historical results and public query contract](../CYPHER_COMPATIBILITY.md)

## Status: checkpoint A recorded; functional parity remains in progress

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
A family owner is not a diagnosed root cause. No percentage/conformance claim follows
from these counts. The frozen ledger's baseline is inventory-only (`not_run`);
historical 1,004/1,077/1,816 observations remain in the linked compatibility report,
not substituted for fresh execution evidence.

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

Error equivalence is intentionally strict: lexer/parser/analysis boundaries establish
compile phase. A narrow mapper covers proven invalid Unicode, integer/float overflow,
malformed Unicode escapes and unknown functions, using actual native fields/signatures,
never the expected scenario error. Unmapped details retain their native code. Other
engine failures report phase `unknown` and cannot satisfy an expected runtime error.
Broader phase/detail mapping remains work assigned to the affected packages. Unexpected execution/observation
exceptions fail the scenario; they are not retrospectively changed to `not_run`.

An explicit typed-schema fixture can be supplied to the development backend. With
`--infer-fixture-schema`, CREATE-only, single-label/fixed-endpoint fixtures can have
their scalar column types inferred from inputs, never expected results. Fixture
queries are unchanged and no primary keys/values are invented. Label-only node
tables receive an unset nullable layout column (no added graph property), recorded
in the adaptation. Untyped/multilabel graphs, incompatible endpoint families,
ambiguous stored types and nonliteral fixture computation remain explicit blockers.
The
query under test remains unchanged; such a successful scenario reports
`adapted_passed`, never upstream `passed`. Named graph scripts are loaded unchanged
from the checksummed inventory. `binary-tree-1` is admitted with inferred schema;
`binary-tree-2` currently refuses its relationship type spanning X/X and X/Y endpoints.

Table-defined procedure fixtures register trusted scalar-signature callbacks with
exact input filtering and duplicate output preservation. Unit procedures and NUMBER
union signatures remain FP-7 blockers, not fabricated output columns or coercions.
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
python -m pytest tests/tools/test_tck_ledger.py tests/tools/test_tck_stateful.py tests/query/test_compatibility_profile.py
```

Execution filters restrict only execution, never inventory. Failed executed cases
produce a nonzero exit; unexecuted cases remain counted separately. `--execute-reads`
retains the historical read-only diagnostic; it cannot be combined with
`--execute-stateful`. These are development tools, not new database settings or
runtime public API. No production Pulse installation or data mutation is involved.
