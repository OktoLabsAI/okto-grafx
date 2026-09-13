# FP-2 working evidence: numeric literals, headings and postfix access

## Latest owner-wide checkpoint: temporal dependencies closed

The refreshed original FP-2 owner selection passes **1,976/1,976 required cases**,
zero failures, with 1,921 outside selection; terminal exit 0. V2 and predecessor
V1 inventories both verify. Receipt under `.grafx-tmp/`:
`fp2-post-temporal-entities-native.json`; SHA-256
`1fedb130068d2f84d8dbaf12bc3970d9738453c1263a4da1ea5aaaeb51c6b693`.

The final-code rerun `fp2-temporal-final-qualified-native.json` has the same
passing result and byte-identical SHA-256. The final grouped regression passes
6,812 tests with zero failures/errors/skips;
[exact scope and group receipts](../specs/TEMPORAL_VALUES_V1.md#grouped-regression-trace).

The previous 65 failures needed native temporal query/storage capability.
Constructors/operators alone closed 15; the other 50 then failed on whole-entity
materialization, not temporal sorting itself. The owned entity grammar now
preserves native temporal properties in nodes, relationships, paths and nested
results. Original fixtures, queries, expected values/effects and inclusion
decisions are unchanged. [Temporal contract](../TEMPORAL_VALUES.md),
[entity JSON contract](../ENTITY_VALUES.md#json-grammar).

Separately, all 1,004 original temporal-family cases pass; that result is not
added to this owner count or used to infer a full-profile receipt. The broad
regression and remaining FP-3–8/transport/Pulse work retain their own acceptance
requirements. The earlier failing checkpoints below are historical evidence,
superseded by this latest owner result.

## Previous owner-wide checkpoint and malformed literals

The refreshed native FP-2 selection reaches terminal exit 1 with **1,911 passed /
65 failed / 1,921 outside selection**, all 1,976 selected cases required. The 65
failures are missing temporal functions: 50 first stop during fixture execution
and 15 during the tested query. They remain mandatory dependencies, not exclusions.
This supersedes the 1,859/117 owner-wide baseline below; focused case counts are
not added together to invent a current profile total. V2 and V1 are verified.

The three malformed-literal blockers are closed natively. An adjacent non-keyword
identifier suffix carries `invalid_numeric_literal` with source offsets; a
separated name retains ordinary syntax handling. Unsupported ASCII symbols carry
their own proven lexical cause. The mapper consumes field/cause/phase, not the
scenario expectation. Quoted keys, numeric keyword continuations, finite numeric
values, based literals and their preexisting overflow/Unicode errors retain their
contracts. [Public error fields](../QUERY_LANGUAGE.md#values-and-python-mapping).

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-post-entity-literals-native.json` | Full FP-2 owner: 1,911 passed, 65 failed, 1,921 outside selection; terminal exit 1 | `bd5adbf34569f15e7efd9567d515cd1a75c83a54a1976528dda2cb42e4f8d204` |
| `fp2-malformed-literals-native.json` | All 131 original literal cases passed; 3,766 outside selection; terminal exit 0 | `702195747c19662891c28b59516280ec712cd34025f0a3f8a04f72741c909796` |
| `fp2-malformed-literal-regression.xml` | 384 passed, zero failures/errors/skips; 9.876 s; terminal exit 0 | `83cf347a2ec356a32c152fb6e0cd1be8f21f3b139e882c1be17d77012b40c502` |
| `fp5-foundation-literals-regression.xml` | 654 passed, zero failures/errors/skips; 8.566 s; terminal exit 0 | `1706076e852db7f09b37c766b96c63d4a74e8435683132bc1b37b2a4c773fb14` |

Regression covers malformed suffix/symbol refusal before graph effects, preserved
prior/later instructions, store verification, source positions, quoted identifiers,
separated/comment-delimited names, exact numeric boundaries, lexer/parser and
negative mapper evidence. No native fixture, query, expected output or ledger is
adapted. No commit/push/install/release or production mutation was performed.

FP-5 work has begun with internal integer calendar/nanosecond/duration primitives;
its [full temporal matrix](../specs/TEMPORAL_VALUES_V1.md) preserves native query,
stored columns, precision, clock/zone and lifecycle requirements. The foundation
does not admit new public parameters or stored types and does not close these
65 failures. Full FP-2–8/Pulse acceptance is still outstanding.

Subsequent native entity-UNWIND/implicit-index work closes all four collected-entity
and MERGE blockers in the baseline below. List12 passes all seven original cases
and Unwind1 all 14, with no fixture/ledger changes. Earlier failing receipts in
this document remain historical evidence, not current failures. See
[implementation and regression evidence](FP3_PROGRESS.md#native-entity-unwind-and-implicit-index-authority).

## Current increment: native projection/list/path error contracts

Five remaining required negative cases now expose native cause/phase evidence:
duplicate RETURN/WITH headings, a computed WITH item without alias, a row aggregate
inside a list-local expression and size(path). These were refusals already; the
raise sites now identify the precise cause and planning phase, and the mapper
uses only that evidence. No fixture/query/expectation/ledger adaptation is used.
The language reference documents fields, valid alternatives and transaction
behavior in the [projection contract](../QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-projection-error-contract-final.xml` | 953 passed; zero failures/errors/skips; 43.659 s | `e83eb0fde0de5c03dba6f0396ffa72c03706e3f7e252629175f2025815dfab2a` |
| `fp2-projection-errors-return4-native.json` | All 11 original Return4 cases passed; 3,886 outside selection | `138538e4f9f94cea8fe6413064bfd5bfe95ee80e04eee29714861c149080cfaa` |
| `fp2-projection-errors-with4-native.json` | All seven original With4 cases passed; 3,890 outside selection | `913be676860df13d000ddaa79992e1371d386a3b21dc9bb7e8cdbe98c5da6126` |
| `fp2-projection-errors-list6-native-final.json` | All 17 original List6 cases passed; 3,880 outside selection | `d8866f1ede044247a7fd465e50cd8f9d9306550b998d96afaa62a61ca6d2dfa5` |
| `fp2-projection-errors-list12-native.json` | Five original cases passed, including #0007; #0001/#0002 still fail collected-node writes; 3,890 outside selection | `be4e1b4e6cff26641db1ed878dd59de029e16292438c49ee59c3da7dead9644c` |

Every command terminated. The first four results are successful; List12 is an
explicitly incomplete family, not a successful gate. Native selections verify V2
and V1. Focused tests additionally cover explicit/derived/wildcard name collisions,
bare carried WITH variables, map/reduction predicates and bodies, valid collection
sources produced by aggregates, and aliased paths. Refusals after a written CREATE
prefix cause no effects; earlier/later instructions survive, verification passes
and durable reopen preserves exactly those successful writes. Forged cause, field
or phase combinations are not mapped as conformant errors. All five original
negative shapes also cross the real NativeScenarioBackend in regression tests.

The initial native size(path) run exposed a missing planner-boundary mapper route;
that route is now covered and all 17 cases pass. The first regression also exposed
three exact-dictionary assertions predating the new cause/phase fields. They now
assert the full new dictionaries, with the same invalid queries still refused.
Final regression includes parser/lexer, analysis/planner/engine, list iteration,
compound RETURN ordering and native error/window mapper tests. No test is waived.
No new configuration, public method/result DTO, persistence format or concurrency
policy is added. There is no commit, push, installation or release. Full FP-2 and
the complete profile remain open; the baseline disposition table below is updated.

## Previous increment: compound RETURN ordering over projected aggregates

Grouped/DISTINCT RETURN sorting reuses complete projected expression values inside
compound keys. This preserves public unaliased headings and already computed
aggregates, including when spill no longer retains source bindings. Type proof
uses alias definitions in their input scope without changing executable producers.
Expression-local shadowing, source/output shadowing and single evaluation remain
intact. Ambiguous grouping is checked before substitution; missing discarded
inputs have native `undefined_variable` planning evidence. New unprojected
aggregates remain refusals. [Usage/API contract](../QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-return-aggregate-sort-qualified.xml` | 722 passed; zero failures/errors/skips; 64.206 s | `562fc6a130fa87b2f6b88f2f1cd8c76211adc7a243abcb7f472349d985fb7d1e` |
| `fp2-return-aggregate-sort-native-qualified.json` | All 14 original ReturnOrderBy2 cases passed; zero selected failures/not-run; 3,883 outside selection | `838662c6e69c69e9fafa0c5767f8c42b61c3bf24ccbc4b22d3907d6e857c1938` |
| `fp2-return-aggregate-sort6-native-qualified.json` | All five original ReturnOrderBy6 cases passed; zero selected failures/not-run; 3,892 outside selection | `6b1b69ec89c20172ef97586caa03fe9a36bc2014898b2f43c3c28b3521b447c9` |

All three commands reached terminal exit 0; native runs verify frozen V2 and its
immutable V1 predecessor. This closes the four previously failing ReturnOrderBy6
cases and ReturnOrderBy2#0013 without query/fixture/result/ledger changes.
Feature tests cover real multirow groups, empty groups, parameters, aliased and
unaliased outputs, DISTINCT, ordinary/bounded memory, map/local alias shadowing,
planning without random draws, exactly one projected random draw, and whole
instruction rollback after a late sort division failure with prior/later writes
preserved and store verification. Analysis/planner/engine, grouping, projected
maps, expression scope, arithmetic, rand, Top-N, spill and optimized ordered
projection/node-merge regressions are included. Two older analysis tests now
assert the exact missing-variable cause/phase instead of a generic sort field;
their queries remain refused. No tests were waived.

Earlier exploratory receipts include four failures before alias type proof was
separated, then two old error-field assertions. An invalid regression invocation
named a nonexistent test file and ran no tests; it is not qualification evidence.
The runner accepts one feature prefix, so ReturnOrderBy6 and ReturnOrderBy2 use
separate receipts; repeating that option selects only its final value.
The successful receipts above supersede these attempts, not the historical
5,013-test query/tools baseline. No full-suite rerun, installation, commit, push
or release is claimed. No new configuration/public DTO/persistence/concurrency
policy is introduced. Full FP-2 and the complete parity profile remain open.

## Previous increment: projected maps and native chained access

Native nested map access now works through WITH literal/parameter/CASE projections,
successive aliases, NULL and returning read-subquery imports/exports. The planner
no longer treats every Variable property subject as a graph table. It follows
selected literal map/list paths through existing alias definitions for type proof,
without rewriting the executable expression, evaluating parameter payloads or
calling a scalar function to discover its result. Unknown projected fields remain
runtime checks; known invalid subjects/leaf types retain planning refusal.

This closes the original required `clauses/with/With2.feature#0002`, without query,
fixture, expected-result or ledger adaptation. Grouping/sorting spill uses the
existing value codec, not a second representation. Public examples and execution
semantics are in [values and Python mapping](../QUERY_LANGUAGE.md#values-and-python-mapping)
and [composition](../COMPOSABLE_QUERIES.md).

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-projected-maps-regression.xml` | 922 passed; zero failures/errors/skips; 83.370 s | `8daa38748d413131516b2798a8702af0a8ba3163dc5583a50a49fb354206b83a` |
| `fp2-projected-maps-native.json` | 44 original map cases passed; zero selected failures/not-run; 3,853 outside selection | `3a0420d33a1683fec4b3d67a8a9cbd4c5378559d8d654cac18977f1dc4ea1b77` |
| `fp2-projected-maps-with2-native.json` | Both original With2 cases passed; zero selected failures/not-run; 3,895 outside selection | `078960812d0d7cf74d45384944d726d6146e3b9ad4887e0e6716a05946b90d52` |

All commands reached terminal exit 0; native runs verify V2 and the immutable V1
predecessor. Focused evidence includes ordinary/bounded memory, map/list mixed
postfix chains, current parameter rebinding after a refused value, shadowing,
NULL/missing keys, CASE, returning subquery boundaries, UNION, scalar composition,
DISTINCT/sort spill and full instruction rollback with preserved prior/later writes
and store verification. A controlled random source proves planning performs no
draw and repeated field access performs exactly one source draw; 18 shared alias
projections preserve that property. Existing postfix, alias/grouping, list iteration,
analysis/planner/engine, scalar parameter, entity UNION, rand and spill regressions
are included. The previous 5,013-test query/tools qualification remains a historical
baseline; it is not relabeled as a run of this subsequent source increment.

No configuration, persistent type, public result DTO, transaction/concurrency mode
or installed-Pulse runtime changed. Language, composition, roadmap, compatibility
and generated API documentation are updated. Documentation links/anchors and all
39 configuration fields, changed-Python Ruff and whitespace checks pass. There is
no commit, push, release or installation. This closes one required native blocker,
not FP-2, the full profile or FP-3–8. The remaining inventory below stays active.

## Previous increment: scalar error contracts and lexical list element types

Native conversions now distinguish unsupported **value types** from unconvertible
text. The former carry an execution-phase `conversion_argument_type` cause;
the latter remain NULL. No host-object coercion is introduced. The mapper consumes
the native cause/function/phase, never a scenario expectation. Public tests cover
NULL/text behavior, unsupported primitive/collection/entity/path values, unselected
CASE/empty input, host callbacks that must never run, and late failure with whole
instruction rollback, prior/later writes, verification and durable reopen.

The quantifier failures were not only missing error labels: literal-list local
binders had no retained element types. Planning now derives their homogeneous
source type, using existing unique lexical binding identities and the same source
inference as UNWIND. Unknown, heterogeneous and parameter-dependent sources stay
unknown; reduce accumulator types are not guessed from an initial value. Arithmetic
subexpressions are checked at planning when their types are known. Dynamic type
errors retain runtime evidence. Nested/sibling shadowing, outer captures, NULLs,
parameter lists, conversion composition and transaction rollback have focused tests.
The public [contract and admission change](../QUERY_LANGUAGE.md#conversion-and-arithmetic-error-contracts)
distinguishes planning from execution/bind-time validation. No new setting,
approximation, storage format or concurrency policy is introduced.

### Native receipts and refreshed baseline

All runs verify V2 against its immutable V1 predecessor. No original query,
expectation, fixture or ledger was edited in this increment.

| Receipt under `.grafx-tmp/` | Original selected result | SHA-256 |
| --- | --- | --- |
| `fp2-conversions-native-qualified.json` | 47 passed; zero failures/selected not-run; 3,850 outside selection | `c77918eb2831342ed619f38b6faf316923bee81b338ef0a484e3fccae8c7d9b6` |
| `fp2-quantifier-types-native-final.json` | 604 passed; zero failures/selected not-run; 3,293 outside selection | `b81fb6f8cf5b743054c13db94cfc50040a4eee741a492c6f2369604a3a032356` |
| `fp2-post-percentile-native.json` | Before this increment: FP-2 selected 1,976, with 1,859 passed and 117 failed; 1,921 outside selection | `b4f4bb69aed17702ebe053f5b40baa87ceccd0f20ef0c70b4c55091fdbeddaa3` |

The first two commands reached terminal exit 0. The broad baseline command reached
terminal exit 1 because of the recorded failures, not an interrupted process. It
loaded source before this increment's fixes and is **not the final current FP-2
pass count**. Focused results overlap; their counts are not added to certify a full
profile. The 22 architectural multiple-label divergences are outside this FP-2
selection and are not relabeled passes.

The 117-failure baseline is now an explicit work inventory:

| Baseline family | Cases | Current disposition |
| --- | --- | --- |
| Temporal constructors/fixtures used by ordering | 65 | Still open; native FP-5 value/storage work is required, not fixture substitution |
| Conversion runtime type evidence | 22 | Closed in the 47-case native family above |
| Known quantifier arithmetic types | 12 | Closed in the 604-case native family above |
| `size(path)` static error evidence | 1 | Closed in the projection-error increment; all 17 List6 originals pass |
| RETURN ORDER BY composed aggregate scope | 4 | Closed; all five ReturnOrderBy6 originals pass |
| UNWIND/MERGE index and collected-entity binding/writes | 4 | Closed; List12 and Unwind1 original families pass; see FP3_PROGRESS |
| Nested projected map property inference | 1 | Closed in the subsequent projected-map increment above |
| Duplicate projection headings | 2 | Closed; Return4 and With4 original families pass |
| WITH expression without alias | 1 | Closed; With4 original family passes |
| Aggregate inside list comprehension | 1 | Closed; List12#0007 passes; its two formerly failing write cases also pass in the subsequent entity-UNWIND increment |
| Malformed literal error taxonomy | 3 | Closed; all 131 original literal cases pass in the latest checkpoint |
| DISTINCT ORDER BY unprojected property scope | 1 | Closed; all 14 ReturnOrderBy2 originals pass |

The next query work remains these required native scope/value cases, followed by
the rest of FP-3/4 and the full FP-5–8 packages. FP-2 is not closed, and temporal
cases are not removed from its ownership/accounting to claim closure.

### Regression qualification

The 186-test conversion/scalar/error-mapper selection passed after fixing the new
test fixture to open an explicit write transaction. A subsequent wider regression
exposed six assertions tied to the former unclassified error dictionaries or
runtime-only boolean arithmetic admission; they now assert the explicit native
cause/phase, retaining the original refusal and CASE binding requirements.
The first full query/tools run reached terminal exit 1: **5,006 tests, 5,002
passed, four failures, zero errors/skips**, 966.527 s. Receipt
`.grafx-tmp/fp2-scalar-error-query-tools.xml`, SHA-256
`3d862afa74913c2503b124b332d15a258dd9cc372161b0e6b48b540fc1df0d95`.
It remains a failed historical run, not a qualified full regression.

Two seek predicate tests still assumed the old STRING-as-unknown boolean behavior.
The original STRING queries now explicitly assert planning refusal, for both
matching/missing keys and indexed/scanned tables. Separate current-contract tests
retain full-conjunction plan assertions, false residual filtering and a missed
leading equality that must avoid a division-by-zero residual on the canonical
scan. The numeric-bound test still pinned 40 although FP-2 had already documented
and implemented 2,048 characters; it now pins 2,048 and independently tests exact
2,048/2,049-character admission/refusal, not a boundary derived from the constant.

The fourth failure found unnecessary owner-landing memo invalidation after every
statement rollback. The engine now preserves the bounded owner memo only when the
working catalog identity is unchanged and no schema-effect suffix exists. Its
existing transaction/snapshot/schema/heap-epoch and full intent/held-row content
checks remain mandatory. Schema rollback still retires it; endpoint/primary-key
memo invalidation is unchanged. Tests retain zero repeat decodes for unchanged
landings, reject stale intermediate poisoned views and cover schema-change
retirement, subsequent statement reuse and store verification.

The initial repair selection passed **164 tests**, zero failures/errors/skips,
39.366 s, `.grafx-tmp/fp2-scalar-repair-focused.xml`, SHA-256
`197c3dc75814196ccbbef8c2c9f01961dcdbb344eca2cb65f2c07100a998490a`.
The new schema-retirement fixture required explicit identity-catalog activation
before testing implicit ANY schema; that setup was corrected without changing the
native old-catalog refusal. The final repair selection reached exit 0: **49 passed**,
zero failures/errors/skips, 9.702 s,
`.grafx-tmp/fp2-scalar-repair-qualified.xml`, SHA-256
`e222662ccc03d2cf8febe2246476d9bc47036a03330b5eacebae0e8fb2bae103`.
The full final `tests/query tests/tools` regression reached **terminal exit 0**
against the corrected source: **5,013 passed**, zero failures/errors/skips,
**961.527 s**. Receipt `.grafx-tmp/fp2-scalar-error-query-tools-final.xml`, SHA-256
`9e63c55b846f818260a76d71d997d32ec12469ba09791fe26994d277e38aa797`.
The process is terminal and no matching regression worker remains. This qualifies
the complete query/tools test selection for this increment, including the four
repaired cases and the seven added regression cases. It is not a full-repository,
installed-Pulse, whole-profile or FP-2 completion claim. The earlier failed receipt
remains preserved separately.

During that run, read-only native probes further bounded the next existing map
blocker: nested property access fails after WITH literal/parameter/CASE projections,
successive aliases, read-subquery imports/exports and NULL projection. Equivalent
UNWIND map access and literal-list postfix access succeed. The planner's
`_static_postfix_target` does not follow WITH alias definitions, and its Variable
property path falls through to table lookup for projected maps. This remains open;
no runtime/source changes were made while the final regression was running.

Documentation covers the public error/API contract, roadmap and compatibility.
The configuration set is unchanged. A read-only scan of both Pulse source trees
found no direct `GrafxPlanError` or field-based consumers matching these new causes;
no Pulse source/runtime or production data was changed, and installed-Pulse
qualification is not implied. Generated API reference, documentation links/anchors
and all 39 configuration fields, changed-Python Ruff and whitespace checks pass.
No commit, push, release or installation occurred.

## Previous increment: native exact percentiles and shared aggregate folding

`percentileDisc(value,p)` and `percentileCont(value,p)` now execute natively,
including numeric/NULL samples, endpoints, interpolation, per-group first non-NULL
percentile selection, DISTINCT samples, WITH, returning subqueries and composed
results. Signatures are positional; direct volatile arguments are rejected by
the shared effect classifier. The implementation does not substitute fixture
results or change original TCK queries, expectations or either ledger.

With a configured query memory budget, sample ordering uses the existing external
sorters and retains the selected positions instead of an unbounded sample list.
The complete sorted run is consumed and checked against the accepted count:
even a missing tail after an already selected minimum fails closed. DISTINCT
uses the existing external sample deduplication. Without a budget configured,
the explicit in-memory execution mode retains and sorts the samples. There is
no new option, approximate mode or durable format. See the
[public functions, types, errors and resource contract](../QUERY_LANGUAGE.md#percentile-aggregates).

An adversarial repeated-expression test exposed an existing in-memory aggregate
bug: one shared accumulator was folded once per projection occurrence, duplicating
samples and totals. The engine now folds each distinct aggregate call once per
input row. Percentile, sum and count repeated-expression regressions pass in both
memory modes, including a WITH boundary. The external path already maintained
separate accumulators and did not exhibit the incorrect result.

The final native selection reports **35 original aggregation-expression passes**,
zero selected failures/not-run; 3,862 cases are outside this selection. This closes
the 13 previously pending percentile cases, not the whole FP-2 or full profile.
V2 and the immutable V1 predecessor are verified. Receipt
`.grafx-tmp/fp2-percentile-aggregation-native-qualified.json`, SHA-256
`09c474e19ae40575100558b6262f5929bbf5453b662968201134cd225e8a5e95`.

The final regression receipt `.grafx-tmp/fp2-percentile-regression-qualified.xml`
reports **820 passed**, zero failures/errors/skips, **45.554 s**, SHA-256
`e13543907c4d76c3895f9a50aeeabee9cd6f68a8a12667c139136a92ad677f40`.
Both commands reached terminal exit 0. The earlier 810-pass receipt predates the
additional runtime-type/cleanup tests and bound-expression admission check; it is
not the final qualification evidence.

Coverage includes bounded and in-memory execution, empty/all-NULL groups, numeric
identity, extreme finite interpolation, expression NaN and its storage prohibition,
sample/p argument errors and negative TCK mapping controls, repeated expressions,
late-group rollback, truncated-run detection, and simultaneous primary read and
secondary close failures. Cleanup attempts the other percentile sorter, preserves
the primary cause, removes temporary files and allows earlier/later instructions
to commit; full store verification passes. The regression also covers aggregate
determinism, rand, alias/grouping/projection scope, analysis/planning/execution,
spill and executemany. It is not a full-repository or installed-Pulse qualification.

Language, configuration/resource contract, composable queries, architecture,
comparison, compatibility and roadmap documentation are updated. API reference
generation, documentation links/anchors and all 39 configuration fields, changed
Python Ruff checks and whitespace checks pass. No Pulse source/runtime, production
data, release, commit or push was changed by this increment. Wider FP-2 temporal
function failures and the remaining functional parity fronts remain open.

## Previous increment: stable aggregate arguments

Native aggregate admission now rejects arguments containing a declared volatile
function, using the existing owned-AST effect classifier. This applies to all
currently supported aggregates and includes unreachable CASE/coalesce branches;
it is a planning rule, not an observed random outcome. No random value is drawn
and no graph effect is staged by that rejected instruction. The native cause is
`aggregation` / `non_deterministic_aggregate_argument` / planning; the TCK mapper
requires that exact evidence for `NonConstantExpression`.

The former development tests that directly summed rand calls now materialize
samples with WITH, then aggregate aliases. Controlled draw sequences and exact
results are preserved, including independent parallel sample columns. This is an
explicit syntax/contract correction, not a waiver: direct calls refuse; preceding
WITH, parameter values and deterministic expressions retain the capability.
Random grouping/output expressions and row windows remain supported. Existing
trusted UDF determinism declarations are unchanged. See the
[public migration recipe and error fields](../QUERY_LANGUAGE.md#stable-aggregate-arguments-and-volatile-row-values).

Native Return6 now reports **21 original passes**, zero selected failures/not-run;
3,876 cases are outside selection. V2 and the immutable V1 predecessor are verified.
Receipt `.grafx-tmp/fp2-aggregate-determinism-native.json`, SHA-256
`ed7aeb3ac515a59b29cc3b54a4c2be09b1e3a2846e3a459bb5339a025b997595`.
No scenario query, expectation, fixture or ledger was changed.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-aggregate-determinism-features.xml` | 56 passed, zero failures/errors/skips, 1.550 s | `a6242e65b65f4dc1193de977f6675f619fc580f4b161c1e0e2701a2a44efcd93` |
| `fp2-aggregate-determinism-regression.xml` | 740 passed, zero failures/errors/skips, 39.235 s | `6585565cbb43e0a0d0d2045f96a52541fcd093f94962a5fc249d4264c234184c` |

Focused tests cover all six aggregates, direct/nested/unreachable volatile inputs,
no draw on refusal, exact mapping with negative evidence controls, preserved prior
writes and full verification, parameters and materialized values. The regression
adds controlled rand, alias/grouping/projection scope, analysis/planner/execution,
memory spill, NaN/storage prohibition and TCK errors. These overlapping selections
do not establish full-repository or installed-Pulse qualification.

A read-only scan of both Pulse repositories' Python source found no rand or
percentile calls; no Pulse source or installed runtime was changed. The remaining
13 native aggregation-expression failures are specifically percentileDisc/Cont
cases, including range errors and composed degree/grouping. They remain mandatory
and require native bounded-memory execution, not fixture adaptation. The broader
functional parity objective and FP-2 are not complete.

API reference generation, documentation links/anchors and all 39 configuration
fields, Ruff and whitespace checks pass. There is no new configuration or legacy
mode. No commit, push, release, installation or production-data mutation occurred.

## Previous increment: RETURN alias expressions and grouping evidence

RETURN aliases are now resolved throughout ORDER BY expressions, including entity
properties and scalar composition. The analysis scope includes output aliases with
their entity kind and output precedence. Runtime compound sorts obey that same
precedence; input expression caches depending on a shadowed name cannot override
the output, while already computed aggregate results remain intact. An initial
new test exposed `RETURN -n AS n ORDER BY n+1` incorrectly reading the input n;
the source correction passes the repeated regression. The failed intermediate
receipt remains historical, not relabeled successful.

Mixed aggregate expressions now reject implicit grouping leaves that were not
independently projected. A composite grouping key does not grant access to its
components; constants, parameters and local binders are not ungrouped inputs.
WITH wildcard-carried variables count as projected grouping values. A native
`aggregation` / `ambiguous_aggregation_expression` / planning cause is required
for TCK `AmbiguousAggregationExpression`. Grouped WITH resolves dropped inputs
before diagnosing a new illegal aggregate. Nested aggregates now carry native
`function` / `nested_aggregation` / planning evidence instead of generic refusal.
[Usage and exact contracts](../QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions).

All native runs verify V2 and its immutable V1 predecessor; no expected values,
fixture queries or ledger classifications changed:

| Receipt under `.grafx-tmp/` | Original outcomes | SHA-256 |
| --- | --- | --- |
| `fp2-order-grouping-native.json` | WithOrderBy4: 20 passed, zero selected failures/not-run; 3,877 outside | `1163680d052cea6aa59ca78716b50a40d8de54a5e87dc8c629ea0985fd91ca32` |
| `fp2-alias-grouping-order-final.json` | WITH ordering: 227 passed / 65 failed, zero selected not-run; 3,605 outside | `e3b9deacfd23479a3f8ffb14a2acb8e62972daca7eedece12d51c9d00387a3fa` |
| `fp2-alias-grouping-return-final.json` | Return6: 20 passed / one failed, zero selected not-run; 3,876 outside | `f8e4cb443967c60bc7ef7cef4344697fa18b3ff09758fc93eb5214d7b33bf04e` |
| `fp2-grouping-aggregations-native.json` | Aggregation expressions: 22 passed / 13 failed, zero selected not-run; 3,862 outside | `6b179471b703f01271363dfb17d6a56a40b011a968a5f0f1b3d966bc76d164d4` |

All 65 WITH-ordering failures now stop at missing functions (50 fixture, 15 action).
All 13 aggregation-expression failures also stop at missing functions; seven
therefore lack their expected runtime numeric-range errors. Return6's mandatory
`count(rand())` still succeeds where the reference requires NonConstantExpression.
Existing native rand-in-aggregate behavior/tests require coordinated revision,
not an exclusion or a generic-error mapping. Full aggregate/FP-2 parity is open.

| Regression receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-alias-grouping-regression-final.xml` | 670 passed, zero failures/errors/skips, 33.942 s | `c5934803321e502020d1b1949c24f0dbb368b74669d7f7e16115dcc6814f8ba0` |
| `fp2-alias-grouping-final-evidence.xml` | 219 passed, zero failures/errors/skips, 11.672 s | `c1acf04d8d19a7d05b8dbc513a7e28ace20cad498bc44c1a3fb61da88ca676ac` |
| `fp2-alias-grouping-star-final.xml` | 44 passed, zero failures/errors/skips, 3.745 s | `438d44e2fe9d7dd8db5042269a02b8f3d56e63bb3f35d0d7191c00f53ee623bb` |

The large selection covers new alias/grouping cases, analysis/planner/execution,
projection/WITH scope, memory spill and TCK error evidence. The next selection
also covers final nested-aggregate cause, rand, NaN/storage prohibition, RETURN
wildcard and canonical AST admission. The last reruns grouping/WITH wildcard
after explicitly including star-carried grouping variables. Cases overlap; these
are not full-repository or installed-Pulse receipts. Documentation/API generation,
links/anchors, all 39 configuration fields, Ruff and whitespace checks pass.
No new configuration or consumption API was added. No release, installation,
commit, push or production-data change was performed. Overall parity stays open.

## Previous increment: projected references and attached WITH filters

WITH modifiers now resolve exactly projected subexpressions to their output
bindings. This supports grouped/DISTINCT property ordering and filters, expressions
containing projected properties, and reuse of an already projected aggregate.
The normalization is applied to the analyzed AST passed to lexical lowering;
it does not adapt a TCK fixture or rewrite a query in the conformance adapter.
Largest complete expressions are substituted before their children, with memoized
owned-AST traversal. Literal/parameter payloads are never traversed. Output alias,
list-local and pattern-local shadowing prevent capture; algebraically different
expressions are not inferred equivalent. DISTINCT/grouping do not regain access
to arbitrary source nodes or unprojected properties.

Plain WITH-WHERE may temporarily read source-only bindings, including NULLs from
OPTIONAL MATCH. Those names are preserved in private operator columns through
the existing bounded sort codec, then removed by the closing projection after
the attached filter. They never enter later public output or DISTINCT identity.
No new spill format, compatibility setting or public API signature is required.
[Public contract](../QUERY_LANGUAGE.md#with-ordering-scope).

Final native receipts (V2 and immutable V1 predecessor verified):

| Receipt under `.grafx-tmp/` | Original outcomes | SHA-256 |
| --- | --- | --- |
| `fp2-projection-with-where-final.json` | 19 passed; zero selected failures/not-run; 3,878 outside selection | `43d5ae5fd734651f61b0fa0d76cf62ac9420994fb2101a656ce1f37e11a14fbc` |
| `fp2-projection-with-order-final.json` | 223 passed / 69 failed; zero selected not-run; 3,605 outside selection | `c88371b20799b07b2783cb8e3a1335c37f3dcdd14b0718950db2cfdc5eed4b2c` |

The ordering failures remain required: 50 unknown-function failures in fixture
setup, 15 unknown-function action failures, one undefined-variable positive case,
two aggregation-versus-undefined-variable error mismatches and one missing
ambiguous-aggregation cause. The family is not closed. Intermediate WITH-WHERE
evidence had 17 passes / two failures before source-only filters were implemented;
that receipt remains historical rather than relabeled successful.

`.grafx-tmp/fp2-projection-scope-regression.xml`: **625 passed**, zero
failures/errors/skips, 54.234 s; SHA-256
`c83ab81903c82913298af729717be43985576ddcfd9c0000dded71e9b05798e7`.
It covers projected/scope features, analysis/planner/execution, bounded memory
spill, WITH wildcard, row windows, deleted-entity rollback and native entity DTOs.
Feature tests include full-sort/private entity decoding, exact output-column
closure, alias and local shadowing, DISTINCT/grouped refusals for unrelated
properties, projected aggregates without reaggregation, and source-filter errors
rolling back a CREATE while preserving earlier successful instructions.
Counts overlap earlier receipts; they do not establish full-repository or Pulse
qualification. Overall FP-2/FP-3/functional parity remains incomplete.

Final cross-boundary selection `.grafx-tmp/fp2-projection-scope-final.xml`:
**114 passed**, zero failures/errors/skips, 12.302 s; SHA-256
`f886f14553d9d75f3d03084d50328846bd7f94e1077517453b5b8a921102e527`.
It reruns projected-expression features, controlled rand, expression NaN/storage
prohibition, RETURN wildcard and canonical AST admission. API reference generation,
documentation links/anchors and all 39 configuration fields, Ruff on changed
Python files and whitespace validation pass. No commit, push, release, installation
or production-data change was performed.

## Previous increment: plain WITH ordering scope

`WithSkipLimit3 #0003` now executes its original query: the ORDER BY attached to
a plain WITH may read an incoming binding not exported by that projection.
Projected aliases shadow incoming names only after the projection expressions
have been resolved. Analysis validates the temporary ordering scope, lexical
lowering preserves old/new identities, and planning retains only the input names
needed by the sort. A closing projection drops those private names without
re-evaluating computed values. DISTINCT/grouping do not reopen discarded rows.
[Contract and known remaining scope work](../QUERY_LANGUAGE.md#with-ordering-scope).

Native evidence, both verified against V2 and its frozen V1 predecessor:

| Receipt under `.grafx-tmp/` | Original outcome | SHA-256 |
| --- | --- | --- |
| `fp2-with-window-scope-native.json` | 9 passed, zero selected failures/not-run; 3,888 outside selection | `5ab253acb4b63c78ca288b8aae3a8d0981326dfefb5946a91585abefd8f6e079` |
| `fp2-with-order-native.json` | 212 passed / 80 failed, zero selected not-run; 3,605 outside selection | `a025362758eddf449f1b55e435ebce5d7f241289d4406dd109789c4b196d1c40` |

The wider family is **not** closed: 50 failures stop in fixture setup at unknown
functions, 15 at action unknown functions, seven at undefined variables, four at
invalid aggregation, three have the wrong aggregation-versus-variable error and
one lacks the required ambiguous-aggregation cause. Temporal dependencies,
projected-expression substitution, aggregate ordering and WITH-WHERE visibility
remain required work, not waived cases. No fixture, expectation or ledger changed.

Regression `.grafx-tmp/fp2-with-scope-regression.xml`: **605 passed**, zero
failures/errors/skips, 44.587 s, SHA-256
`8f945487ea702f475462327de3d3dcbd9511851d7862f1f7221a3c9359a86b7b`.
It covers the new feature, analysis/planner/execution, memory spill, WITH wildcard,
row-window expressions, deleted-content rollback and native entity results.
The focused nine tests additionally verify alias shadowing, no downstream/RETURN
star leakage, repeated scopes and correlated subqueries, no double rand evaluation,
and real sorted-row decoding with a bounded memory configuration.
Focused receipt `.grafx-tmp/fp2-with-scope-features-verified.xml`: nine passed,
zero failures/errors/skips, 0.845 s; SHA-256
`cd274416dcd2706443e9f7d697613d8f1e1dafcf6e845bcfa2442a81604e2f69`.
An intermediate spill instrumentation assertion watched the general row decoder,
not sorting's projected-row decoder; corrected instrumentation proves the actual
path. That failed intermediate receipt is retained. Subsequent final tests also
avoid freezing the existing, overly strict WITH-WHERE behavior as a desired
contract, or treating an exactly projected DISTINCT expression as a discarded one.

Final feature/scope selection `.grafx-tmp/fp2-with-scope-final.xml`: **85 passed**,
zero failures/errors/skips, 6.974 s; SHA-256
`3014e7ab71874d360c98debae8727af8d71f3fa7b713222e6e0514f442dfb079`.
It reruns the final scope features, RETURN/WITH wildcard and row-window expressions.
API reference generation, documentation links/anchors and all 39 configuration
fields, Ruff on changed Python files and whitespace validation pass.

No new configuration or public consumption method is needed. Existing memory,
snapshot, rollback and output-column boundaries remain in effect. No installation,
release, commit, push or production-data change was made. These focused receipts
overlap and do not establish full repository, Pulse or FP-2 acceptance.

## Previous increment: row-independent window expressions

Native SKIP/LIMIT formerly accepted only a literal or parameter. Both RETURN and
WITH now accept row-independent scalar expressions and reject free outer variables
or aggregates with `non_constant_window`. Signed negative integer literals and
wrong-type literals carry planning evidence; parameter/computed result errors
carry execution evidence (`negative_window` / `window_argument_type`). The TCK
compile/runtime mapper uses these exact native fields, never scenario expectations.
[Public contract, examples and cost](../QUERY_LANGUAGE.md#row-independent-skip-and-limit-expressions).

Each reached logical window evaluates once per invocation. New expressions are
not duplicated into top-K sorting, vector fusion or ordered-node-merge windows;
literal/parameter fast paths are unchanged. This avoids two random draws yielding
inconsistent physical/logical limits. There is no statement-global random cache:
correlated subqueries redraw per invocation and repeated executions redraw.
Canonical full sorting remains memory/spill bounded, with its cost documented.
No public setting or signature changed. EXPLAIN does not consume random draws.

Final native RETURN window evidence: **31 original passes**, zero selected
failures/not-run; 3,866 outside selection, verified against V2 and frozen V1 parent.
Receipt `.grafx-tmp/fp2-return-window-native-final.json`, SHA-256
`10194d2aac428f1538ab65219c7499cc910c9c367989fbf06356529453d78ff4`.
An intermediate run passed 21 and failed 10 because the compile-time mapper had
not yet forwarded the new proven causes; that forwarding now has positive and
negative evidence tests. No fixture/query/expectation/ledger was changed.

WITH windows: **8 original passes, one failure**, zero selected not-run; 3,888
outside selection. `.grafx-tmp/fp2-window-native.json`, SHA-256
`9d46ea2ffa0b60a37d81ae47ddc7959bb7b806326dbd92d631aa0d2557875b5b`.
`WithSkipLimit3 #0003` remains required: `WITH a.count AS count ORDER BY a.count`
incorrectly loses the source binding in ORDER BY (`UndefinedVariable`). It belongs
to the remaining ordering-scope work, not an authorized divergence. The first CLI
invocation repeated a single-valued feature-prefix argument, so this receipt covers
only WITH windows; the separate RETURN receipt above supplies its own evidence.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp2-window-features.xml` | 27 passed, zero failures/errors/skips, 1.971 s | `56e11b27b5752472f8609f25802e894373842fb3c14b70b41a57a9f7c1f7333d` |
| `fp2-window-regression.xml` | 682 passed, zero failures/errors/skips, 41.411 s | `55b2be0e217eca5c89338fc6c008a1923d3b89778d279564f8d50d0d0b8bac68` |
| `fp2-window-final-evidence.xml` | 152 passed, zero failures/errors/skips, 7.166 s | `debefa5714051c1a2a675a32f4b5dc42902f2a9bdc3daac4cd9ff633918b995c` |

Features test type/dependency phases, parameter refusals with whole-instruction
rollback, preservation of prior instructions, real verification, zero-expression
final LIMIT preserving all writes, closed list-local variables, parameter arithmetic,
and controlled nondeterminism through RETURN/WITH with and without sorting and
correlated subqueries. The regression adds analysis/planner/query execution,
vector access paths, rand, memory spill and TCK errors. The final selection reruns
features and both general/window mapper tests after compile mapping changed.
Counts overlap, not a full-repository or installed-Pulse qualification.
Documentation/API generation, all 39 configuration fields, Ruff and whitespace
checks pass. No commit, push, installation, release or production data changed.
FP-2 and the overall functional parity objective remain open.

## Previous increment: deleted entity content

Return2 #0015–#0017 previously returned properties/labels of entities deleted by
the same instruction instead of raising runtime `DeletedEntityAccess`. Native
content evaluation now checks owner deletion before accessing a property (including
a missing property), properties/labels functions or a label predicate. Aliases,
private inserts, DETACH-deleted edges, compiled filters and spill-restored bindings
use the same rule. Ordinary reads without a deletion do not scan transaction intents.
Spilled bindings retain no physical write authority; deleted headers are resolved
lazily once per reference to qualified table/record identities.

The native exception remains `GrafxPlanError`, with exact cause documented in
[the query contract](../QUERY_LANGUAGE.md#content-access-after-deletion). Mapping
to TCK `EntityNotFound` / runtime / `DeletedEntityAccess` requires those native
fields; message matching or a generic failure is insufficient. No expectation,
fixture, ledger or mandatory classification changed. The full Return2 family
passes **18 original cases, zero selected failures/not-run**; 3,879 cases are
outside selection. Final-source receipt: `.grafx-tmp/fp2-deleted-return-native.json`,
SHA-256 `1391606860c76a51c934443129b2740dacf7cddc392192f114699f2c3070f481`.
V2 and its frozen V1 predecessor are verified by that run.

Identity/NULL checks, counts, immutable relationship type (Return2 #0014),
precomputed scalars and detached result observations remain supported. Tests
prove whole-instruction rollback preserves prior successful instructions and
private inserts, and that independent old snapshots survive committed deletion.
No new configuration or weaker concurrency/durability policy was introduced.

The first successful regression receipt,
`.grafx-tmp/fp2-deleted-content-regression.xml`, records **221 passed**, zero
failures/errors/skips, 38.349 s; SHA-256
`e66ee851b852e2d8df4018cb4805fcf5927e7ace1601dd0ee004ea7b766689ec`.
It includes deleted-content features, native entity results, expression NaN/storage
refusal, memory spill, schema transactionality, executemany and TCK errors.
An earlier spill fixture exceeded its legitimate memory budget before content
evaluation; reducing that fixture's row count preserves the budget and asserts
actual spill restoration rather than assuming it happened.

Read-only inspection of Pulse discovery removal queries found count-based removal
receipts and scalar capture before deletion. A native regression preserves the
`DELETE ... WITH count(entity) AS removed RETURN removed` pattern. This is not
installed-Pulse qualification; no Pulse source, installation or data changed.

The first broader boundary run exposed two outdated test contracts: vector DELETE
returned a property after deletion, and a minimal scan context lacked the native
table resolver. The vector test now captures the scalar before deleting while
retaining its exact all-rows-deleted/LIMIT assertions; the context now resolves its
declared table. Failed intermediate receipts are retained, not reported as passes.

Final boundary receipt `.grafx-tmp/fp2-deleted-content-boundaries-final.xml`:
**463 passed**, zero failures/errors/skips, 54.739 s; SHA-256
`5c68170a8791dd0b74c63f8b135199ac63b496358dc9c65755399499bbb856df`.
This covers deleted-content features (including Pulse-style removal counts), vector
access paths, query execution, polymorphic writes and exact TCK error mapping.
It overlaps the 221-test selection. API reference generation, documentation
links/anchors and all 39 configuration fields, Ruff on changed Python files,
and whitespace checks pass. No release, installation, commit or push was made.

FP-2, the model expansion and full-profile/Pulse acceptance remain open. These
focused counts do not update the earlier aggregate inventory or sum to a full suite.

The first [FP-3 increment](FP3_PROGRESS.md) now closes the three conversion-query
failures noted at the end of this historical record, with explicit fixture-schema
adaptation. Its separate receipt does not overwrite the earlier diagnostics.

This is a development checkpoint on `feature/v0.0.6`, not acceptance of FP-2 or
functional parity. [Fixed scope](../specs/FUNCTIONAL_PARITY_PLAN.md) and
[delivery status](../../ROADMAP.md#functional-parity-expansion-plan) remain authoritative.

## Implemented contract

- Exact hexadecimal/octal INT64 literals, signed boundaries and pre-effect refusal
  of malformed/overflowing literals; committed in `0a7ed34` with the FP-1 checkpoint.
- Source-faithful unaliased result headings. Token end offsets delimit the exact
  expression, excluding trailing comments/trivia and preserving internal spelling.
  `ReturnItem.source_text` does not participate in AST equality/hash or normalized
  rendering. Explicit aliases win; bare variables retain logical names. Existing
  prepared-plan keys still use exact input text and the existing authority image.
- General property/index postfixes over function, CASE, group and list results.
  The grammar already supported these shapes; static typing and the parameter
  binder were the remaining blockers. Unknown field types defer to evaluation;
  provably bad types still refuse. Calls are not executed during type discovery.
  Written/parameter-only postfix proofs retain their previous validation path.
- Adjacent equality/order comparison chains lower to the existing AND tree, not
  boolean-valued left association. NULL, explicit parentheses, short-circuit and
  depth bounds are tested. IN/string predicates bind above simple comparisons,
  matching the pinned grammar. This does not promise one evaluation of a shared
  middle operand: use WITH to bind and reuse a value explicitly.
- Native nondeterministic `rand()` with an injected per-handle pseudorandom source.
  It returns an exact DOUBLE in `[0,1)` and never folds or memoizes sampled values.
  Separate source occurrences retain identity through aggregate/grouping maps.
  Pure scalar evaluation without an execution source refuses; views reject
  nondeterministic definitions before metadata writes, including nested queries.

Consumption examples and restrictions are in [the query reference](../QUERY_LANGUAGE.md#values-and-python-mapping),
[composable queries](../COMPOSABLE_QUERIES.md#values-and-breaking-semantics) and
[public API](../API_REFERENCE.md#entry-points-and-supported-imports). No new
configuration, persistence format or transaction protocol is introduced.

## Verified evidence

| Receipt under `.grafx-tmp/` | Scope | Outcome |
|---|---|---|
| `commit-parity-checkpoint.xml` | FP-1 tooling, numeric/lexer/parser checkpoint | 341 passed, no failures/errors/skips |
| `commit-fp2-octal.json` | Unchanged upstream octal family | 10 passed; 3,887 outside execution filter |
| `fp2-columns-regression.xml` | Headings, lexer/parser, plan cache, public boundaries, existing scalars/lists/dispatch | 365 passed, no failures/errors/skips; before the postfix typing fix |
| `fp2-checkpoint-expressions.xml` | Combined numeric/heading/postfix features, scalar parameter validation, existing lists/scalars, cache and public boundaries | 187 passed, no failures/errors/skips, 3.94 s |
| `fp2-all-features-final.xml` | All five FP-2 feature fronts, parser/lexer, scalar parameter validation, native scalars, cache/dispatch, public boundaries, logical views and import/architecture boundaries | 728 passed, no failures/errors/skips |

The final checkpoint supersedes the earlier combined check. That earlier run
found a missing postponed-annotations import in the new pure effect-declaration
module; it was fixed, not waived. An additional adversarial grouping test exposed
two separate random expressions collapsing onto the last sampled value. Per-source
occurrence identity fixes that collision; separate aggregates and nested grouping
expressions now preserve their own draws. Tests also prove no planner/binder draws,
per-row/per-execution behavior, lazy branches, ORDER BY/LIMIT placement, alias
reuse, bad-source rollback and restart without redrawing stored values.

After that fix, all **64** original cases in `Quantifier9.feature` through
`Quantifier12.feature` passed stateful execution: 17 / 8 / 22 / 17. Receipts are
`fp2-final-quantifier9.json` through `fp2-final-quantifier12.json`. The original
diagnostic had 63 rand-blocked cases; the 64-case family total includes one other
case. Each receipt still inventories all 3,897 cases and reports cases outside
its execution filter separately. No reference expectation or profile exclusion
was changed to obtain these results.

The combined checkpoint covers repeated executions, logical scope/aliases,
UNION/subquery headings, escaped-token extents, long headings, call counts proving
no planning/binding probes, empty-row invalid parameter refusals, input ownership,
and failure isolation retaining an earlier successful write. The initial postfix
tests exposed six real typing/binding failures; they pass after the fix. Two old
public-boundary expectations required the former normalized heading and were
updated to the source-spelling contract without removing size-bound coverage.

All five originally diagnosed column-name failures now pass their **unchanged**
upstream expectations:

- `expressions/list/List6.feature#0004`
- `expressions/map/Map1.feature#0003`
- `expressions/map/Map3.feature#0003`
- `expressions/mathematical/Mathematical8.feature#0001`
- `expressions/mathematical/Mathematical8.feature#0002`

Receipts: `fp2-size-upstream.json`, `fp2-map-upstream.json`,
`fp2-columns-upstream.json`. Those diagnostics also retain failures: map families
have 35 passed/9 failed (precise negative error type/phase mismatch); List6 has
3 passed/14 failed (fixture admission and negative contracts). They are not full
family acceptance and no case has been removed from the frozen ledger.

The subsequent native error-phase fix closes those nine map failures:
`fp2-map-phase-final.json` records **44 passed**, with 3,853 cases outside the
execution filter. `fp2-error-phase-regression.xml` records **336 passed**, no
failures/errors/skips, covering planning, aliases, composition and explicit
raise-site phase evidence. Static property types refuse at planning; parameter
types refuse at execution. The reference mapper only translates matching native
field/reason/phase evidence; missing or inconsistent evidence remains unknown.
These results supersede the earlier map diagnostic, not the remaining List6 or
full-profile gaps.

Before publishing this checkpoint, the combined feature, query-composition,
planner, error-evidence, public-boundary and architecture suites were rerun:
`fp2-publish-checkpoint.xml` records **1,048 passed**, zero failures/errors/skips,
105.809 s. This is the union of the earlier 728-test and 336-test selections,
deduplicated by test module, not a full-repository or Pulse regression.

## Remaining work and consumer impact

### Subsequent owner-wide diagnostic and boolean correction

`fp2-owner-current.json` executed the frozen **1,976 FP-2-owned cases**, retaining
all 3,897 inventory entries. Its pre-boolean/pre-grammar-fix baseline is **1,210
passed / 24 adapted_passed / 455 failed / 287 not_run within the selection**;
the other 1,921 cases were outside execution. This is not package acceptance.
SHA-256: `7760456ba659a4e1a93abbbca4490f00feb61d7825c73522ebc54863a6a1b05d`.

The diagnostic exposed actual non-boolean operands silently becoming UNKNOWN.
Native AND/OR/XOR/NOT now validate proven static types and bound parameters;
dynamic operand checks preserve evaluation-time short-circuiting. Explicit
`boolean_operand_type` and `query_phase` evidence distinguishes planning from
execution. Late failure retains proven statement isolation. A separate reproduced
grouping bug returned INT64 for a projected `true` (or BOOL for `1`, depending on
column order): scalar literal expression identity now includes its type. Numeric
value equality/grouping is not changed by expression identity.

`fp2-boolean-regression.xml` passed **667 tests**, no failures/errors/skips,
40.958 s, covering the new contract, comparisons/rand, compiled predicates,
equality pushdown, query engine, planner and parser. Two former pushdown tests
expected a statically invalid STRING boolean operand to survive a false prefix;
they now require planning refusal. Dynamic short-circuit remains separately tested.
The computed-row fallback test still proves canonical evaluation, now using a
legitimate computed NULL rather than relying on the fixed BOOL/INT64 collision.
An additional **400 tests** passed with no failures/errors/skips in
`fp2-error-boundaries-final.xml` (21.321 s): runner/selection/error evidence,
public result boundaries, prepared cache, expression dispatch, original headings
and import/architecture boundaries. Lint and documentation validation passed.

`fp2-boolean-upstream.json` records **141 passed / 9 failed** across the original
150-case boolean family, before the literal-identity correction. Eight failures
require empty quoted map keys; one uses an excluded unlabeled fixture. The earlier
owner diagnostic had 31 boolean passes, 118 failures and one unexecuted fixture.
No original expectation or exclusion was changed. SHA-256:
`4cf0928bfbdeff1a9176e369fd5af8633976592bd9b4259df7319700ff6676b2`.

Grammar-token errors now have explicit native evidence; the focused grammar/phase
suite passed **187 tests** (`fp2-grammar-error-evidence.xml`). The original List6
family subsequently records **11 passed / 2 failed / 4 not_run** in
`fp2-list6-grammar-phase.json`: remaining native blockers are stored list-property
assignment and named variable-length path admission. Those require FP-6 and FP-3;
the four fixture refusals remain visible, not inferred passes.

The runner also verifies durable reopen after a parsed write attempt with zero
observed effects, including failed/zero-row writes without setup. An injected
effect appearing only after reopen fails the verifier. **43 tests** passed in
`fp2-runner-durable-final.xml`; package selection now requires a verified ledger
and never drops inventory or filters by observed failure.

Historical profile inconsistency (resolved by the later explicit NaN decision below):
`Comparison1.feature#0028`–`#0031` and `Comparison2.feature#0012`–`#0015` remain
required but compare NaN from `0.0 / 0.0`, contrary to checkpoint A's finite
arithmetic policy. The user has been asked whether to retain that policy with an
explicit eight-case divergence decision or support NaN expression values without
storage. Until a decision is recorded, these eight cases stay required and failing;
neither the ledger nor numeric semantics had been changed to waive them at that
checkpoint. The explicit decision and current results are recorded below.

All five implementation fronts now have focused feature evidence. Remaining
required-case failures and cross-package dependencies remain open. General temporal property values and
entity identity/graph scalars belong to FP-5 and FP-3 respectively. No current
result justifies claiming all FP-2-owned cases pass.

### Empty keys, scope evidence and streamed ranges

Native map expressions now support empty quoted string keys, indexed/dotted reads,
parameter maps, NULL/missing-key behavior and source-faithful headings. Empty
variable/alias/function/schema names still refuse, as do duplicate map keys.
The lexical test now checks the quoted token and its extent; contextual refusal
is verified at the parser. Two initial test expectations used mutable lists where
the public API returns tuples; corrected tests preserve the existing result API.
An initial missing check for empty node variables was fixed, along with the other
direct token-to-name paths, rather than accepting empty symbols accidentally.

`fp2-empty-map-keys-final.xml`: **331 passed**, zero failures/errors/skips,
7.579 s. `fp2-boolean-emptykeys-final.json`: **149 passed**, one unexecuted
unlabeled fixture already excluded by FP-A-20260911, and 3,747 cases outside
the family filter. Every required case in this boolean family now passes; that
does not certify the rest of FP-2 or alter any exclusion.

Analysis records explicit `undefined_variable` and `invalid_aggregation_context`
reasons with planning-phase proof. Invalid WITH sort aggregation is checked over
all sort keys before argument scope resolution, so a dropped name cannot mask
the invalid aggregation. The actual `WithOrderBy2` family has **25 passed / 20
failed / 38 not_run within the family** in `fp2-withorderby2-context-final.json`;
remaining row mismatches include the missing detached entity result contract
(FP-3), and fixture admission remains recorded. They are not evidence that sorting
has been independently qualified. Source cases/expectations are unchanged.

Direct `UNWIND range(...)` now consumes an allocation-free inclusive sequence;
the existing 100,000-element cap bounds consumption per incoming row, while
materialized range results retain total-size admission before allocation. LIMIT
does not pull an extra discarded row and closes its input on every exit. Existing
eager write barriers remain authoritative, including final LIMIT 0. Tests prove
all preceding writes survive output limiting, and a late quota failure rolls back
the complete statement while preserving an earlier successful statement.

The original `Aggregation3.feature#0002` passes unchanged in
`fp2-streamed-range-upstream.json`: a 1,000,001-element potential span, only 3,000
consumed values, exact sum `3004498500`. No query/expected-value substitution or
budget increase was used. `fp2-streamed-range-focused.xml`: **162 passed**;
`fp2-map-scope-range-regression.xml`: **587 passed**, zero failures/errors/skips,
52.312 s, including cursors, early close, rollback/reopen, primary/cleanup failure,
query engine, aliases, boolean/rand and parser/analysis evidence.
`fp2-range-query-boundaries.xml` adds **472 passed**, zero failures/errors/skips,
45.755 s: deferred projection, compiled/equality predicates, path projection,
optional aggregation, prepared cache and architecture/import boundaries. Lint,
documentation links/anchors, configuration inventory and public API validation
also pass. These focused selections are not a new full-repository/Pulse regression.
The [latest performance observation](../PERFORMANCE.md#latest-006-native-range-prefix-observation)
records only the current 14.03 ms median, not a comparative speedup or timing gate.

Pulse consumers using explicit `AS`, simple `n.id` headings or positional rows
retain their contract. Consumers keyed by normalized expression spelling must use
the submitted spelling or add an explicit alias; this is a coordinated breaking
change, not a legacy mode. Relevant existing Community tests include
`test_grafx_cypher_executor.py`, `test_grafx_discovery_key_decisions.py`,
`test_grafx_graph_transaction.py` and `test_grafx_query_semantics_migration.py`.
These Pulse suites have **not** been rerun for this checkpoint: affected-consumer
regression remains required at checkpoint B and final FP-8. No Pulse production
data, global installation or Core provider-specific code was changed.

## RANGE runtime errors and long finite DOUBLE literals

The next increment closes two native root causes without editing reference
queries, expectations, the case ledger or exclusions:

- RANGE's type inference no longer preempts evaluation with an early type refusal.
  Materialized and direct-UNWIND evaluation share argument admission and explicit
  `range_argument_type` / `range_argument_bounds`, with `query_phase="execution"`.
  Empty inputs/unselected CASE branches do not evaluate them. Arity/syntax remain
  compile-time checks. Native late-write tests verify complete statement rollback
  plus preservation of a prior successful statement through durable reopen.
- The previous 40-character numeric-token ceiling rejected valid finite DOUBLE
  spellings. The fixed bound is now 2,048 characters, sufficient for full binary64
  decimal expansions, including subnormals. Decimal INT64 magnitude is checked
  lexically before converting at most 19 significant digits; a host integer-digit
  limit cannot turn a large literal into an uncaught ValueError. No integer-to-
  DOUBLE fallback, non-finite admission, new stored type or connection knob is added.

Unchanged original TCK families (all 3,897 cases remain inventoried):

| Receipt | Selected family | Result | Outside filter |
|---|---|---|---|
| `fp2-range-errors-upstream.json` | `expressions/list/List11.feature` | 67 passed, zero failed/not-run in selection | 3,830 |
| `fp2-long-floats-upstream.json` | `expressions/literals/Literals5.feature` | 27 passed, zero failed/not-run in selection | 3,870 |

SHA-256 respectively:
`928c1cc1394efebece5cb605a7a10c4af3ce1f515f5c376369036ae458483648` and
`1e6b549f564fd39dd79b632d5b4e97f1c46670924ede52a6ef75a1206c18ed42`.
Reproduce with the existing stateful verified-ledger command and the corresponding
`--feature-prefix` shown above. Both reports use pinned revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23` without fixture/query substitutions.

`fp2-range-float-regression.xml`: **468 passed**, zero failures/errors/skips,
19.333 s, covering numeric literals, lexer/parser, RANGE, scalar/boolean rules,
WITH, stateful runner and exact error mapping. Tests use independent expected
binary64 decimal expansions, maximum/minimum normal/subnormal values, hostile
integer lengths, pre-effect float overflow, late range failure and reopen.
Mapper negative tests ensure incomplete/wrong function, reason or phase evidence
does not become a reference match. Initial new-test failures were harness mistakes
(missing backend admission/control and integer rather than DOUBLE test spelling),
corrected without relaxing expected values or the engine's integer refusal.

`fp2-range-float-boundaries.xml`: **161 passed**, zero failures/errors/skips,
15.810 s. This adds parameter validation, expression dispatch, prepared-cache,
deferred projection and public cursor coverage; it also reruns the feature tests
with explicit exact-token-boundary and NULL/type/bounds-precedence cases. Lint,
documentation/public-API/configuration validation and whitespace checks pass.

This is not full FP-2 acceptance, a new full-repository/Pulse regression or a
changed NaN policy. The previously recorded owner-wide diagnostic remains a
historical baseline; required dependencies and other failures are still open.

## Membership type phases and WITH star scope expansion

IN now checks a provably invalid right operand during planning, including an
empty input; invalid bound parameters refuse before index/scan/write work and
unknown row values are checked when evaluated. All use explicit
`membership_operand_type` / `query_phase` evidence. Nested three-valued equality,
NULL, hash-memo versus scan results and dynamic boolean short-circuiting remain
unchanged. Tests prove a late failure rolls back the complete statement and
preserves a prior statement through reopen. One older seek regression expected a
scan before rejecting a non-list parameter; it now asserts zero scans and the
native binding-phase refusal, not a weaker result assertion.

`fp2-membership-upstream.json`: all **46 List5 cases passed**, with 3,851 cases
outside the selection, original queries/expectations unchanged. SHA-256:
`d4154ce8a657fcd9cc1124b103b181e8c47a600d47e87a042e454fcdf8c2f118`.
`fp2-membership-regression.xml`: **283 passed**, zero failures/errors/skips,
77.193 s: native phase tests, IN memo/seek differential coverage, compiled and
equality predicates, boolean semantics and error-mapper rejection of wrong evidence.

Native `WITH *` / `WITH *, expression AS alias` use a parser marker, semantic
incoming-scope validation and ordinary lexical scope lowering. The lowered plan
contains explicit projections and uses existing execution/grouping/write
operators; no separate runtime fast path, new authority or compatibility mode
is introduced. Star carries named bindings, not schema columns, discarded names,
list-local variables or unimported outer scope. Duplicate names and the existing
256-item ceiling are checked after expansion. The AST's `column_names()` reports
explicit items; unresolved star names require the incoming scope. The marker's
Python type is checked even on caller-built ASTs.

`fp2-with-star-regression.xml`: **288 passed**, zero failures/errors/skips,
41.690 s: star-only/mixed projections, empty scopes, grouping/DISTINCT, windows,
WHERE, repeated names, dropped variables, expression-local shadowing, typed entity
writes/reopen/rollback, optional NULL bindings, explicit subquery imports, YIELD,
UNION, cursor cleanup, prepared plans and public query boundaries. Lint, docs/API/
configuration validation and whitespace checks pass.

Re-running the original conversion family is diagnostic, **not acceptance**:
`fp2-conversions-with-star.json` records 21 original passes, one fixture-adapted
pass, three failures and 3,872 not-run (3,850 outside the 47-case selection and
22 selected fixture blockers). SHA-256:
`11e4f2bcabc618e30c979b22968e66a88af01cf78e541f0a8abe28a4fdc6466d`.
The three failing queries (`TypeConversion2#0007`, `TypeConversion3#0005`,
`TypeConversion4#0007`) now parse but hit the explicit polymorphic multi-MATCH
shape refusal, an FP-3 requirement. They have not been rewritten as typed MATCHes,
reclassified as passes or dropped from the ledger. No Pulse installation or
production data was changed; grouped consumer regression remains due at B/FP-8.

## Explicit NaN expression decision (2026-09-11)

The user chose native NaN expression support while prohibiting storage. This
supersedes the earlier unresolved finite-arithmetic decision; the frozen ledger
is unchanged. Floating zero/zero (including mixed INT64/DOUBLE operands) produces
NaN, numeric comparisons follow the eight required cases, and numeric functions
propagate existing NaN except SIGN's documented integer-zero result. Integer
division by zero, nonzero floating division by zero, finite-input math domain
errors, vector finiteness and bounded execution retain their existing rules.

The investigation also reproduced a pre-existing admission gap: a Python NaN
parameter could be stored in DOUBLE. Native tuple validation now rejects
NaN/infinity, including values nested in LIST/MAP, and SET validates during the
statement rather than waiting for commit-time encoding. No existing store is
erased or rewritten. Generic value encoding still supports transient spill and
is not a substitute for stored-tuple admission. Ordinary multi-reader/writer,
OCC, COMMIT/WAL and durability mechanisms are unchanged.

Independent tests cover the eight expectations, Python parameters/nested results,
NULL and division boundaries, arithmetic/functions, sorting/min/max and forced
spill, CREATE/MERGE/SET, relationship properties, late batch rollback, prior
transaction work, reopen and verification. Historical tests that persisted NaN
now generate it in expressions instead. Pandas/Polars/Parquet tests distinguish
transient export from rejected storage import, without substituting NULL; a late
Parquet fixture uses distinct keys so uniqueness cannot mask nonfinite rejection.

| Receipt under `.grafx-tmp/` | Result |
| --- | --- |
| `fp2-nan-admission-regression.xml` | 809 passed; zero failures/errors/skips; 40.714 s |
| `fp2-nan-storage-interop-regression.xml` | 480 passed; zero failures/errors/skips; 34.048 s |
| `fp2-nan-tck-comparisons.json` | 46 original passes, 5 schema-adapted passes, 1 failure, 3,845 outside selection |

Receipt SHA-256 values, respectively:

- `897b041400b2ee6f8c6ebbd701dc445087fddbd7af637a3d328f3d7f85609037`
- `030802c0fac4748fe501b57f95f8e2b2f04f7c5c29852cb7fb6e6a27a5c72a76`
- `ba9c4a4e8e4aadf765a1c1fdb545b10cba1746ef9d2590fe027d8bbb14a4e6cb`

Both selected regressions overlap in feature coverage; they are not a count of
distinct tests or a full-repository regression. All **eight original NaN cases**
pass: Comparison1 #0028–#0031 and Comparison2 #0012–#0015. The comparison-family
failure is Comparison1 #0040, whose self-loop CREATE fixture still fails planning;
it is an independent FP-3 requirement, not hidden as an exclusion or NaN pass.
Full FP-2/profile and isolated Pulse qualification remain outstanding. No package
was installed or published and no production graph was changed.

Follow-up confirmation on the current development tree:
`nan-confirmation-regression.xml` reports **500 passed, zero failures/errors/skips,
30.417 s** (SHA-256
`bbffcde8e338c914ae185c2f487a21b831dbd732709ece34881770a75e73d5a3`).
This selected run covers NaN expression/storage admission, native scalars, query
execution and spill, schema transactionality, Pandas/Polars/Parquet interop,
list iteration and pattern-comprehension syntax. It is not a full-repository
or Pulse qualification. Documentation validation and targeted Ruff checks also
passed; the eight NaN comparisons remain required in the unchanged frozen ledger.

A subsequent confirmation on the current development tree,
`nan-current-confirmation.xml`, reports **373 passed, zero failures/errors/skips,
35.253 s** (SHA-256
`508401f37a73f181a10256093f6939511fd636a9c903b788fb180d9b94b20da5`).
This selected regression covers expression/storage NaN boundaries, query execution,
spill, schema transactionality and Pandas/Polars/Parquet interop. Documentation
validation also passed. This is not a full-repository or Pulse qualification.

## Native FP-2 inventory, RETURN wildcard and profile V2

The diagnostic `.grafx-tmp/fp2-native-current.json` executes the FP-2 owner
selection without `--infer-fixture-schema`: 1,976 selected cases, **1,770 original
passes, 203 failures and three not-run**, with 1,921 outside the selection.
SHA-256: `1ea282c73122a86d7345fedcccfc957a0ac6e49a1e321c40bcb06409a8873002`.
The run verified V1; re-accounting its unchanged observations under the
[V2 successor](PROFILE_V2.md) makes all selected FP-2 cases required. This is not
a new execution or an accepted package. V1 reports 1,632 required passes and
140 failures; its other selected observations retain their historical divergence
classification. Missing temporal functions and remaining ordering/scope,
aggregation, deleted-entity access, literal/function and error-phase requirements
remain open. Later-package dependencies cannot be hidden by declaring FP-2 complete.

### Native RETURN wildcard

`RETURN *` is parsed as an explicit flag and expanded from analyzed visible
bindings before lexical identity lowering. Variables are alphabetical, followed
by explicit extra items. Anonymous/expression-local names do not leak; node, edge
and path bindings retain native provenance. UNION compares expanded columns;
nested returning read subqueries publish those columns without importing other
outer variables. Empty scope carries native planning evidence for
`NoVariablesInScope`. The existing 256-item projection ceiling and query/transaction
budgets apply. [Public contract](../QUERY_LANGUAGE.md#returning-the-visible-scope).

Original Return7 #0001/#0002 both pass in
`.grafx-tmp/fp2-return-star-native.json`, SHA-256
`7b23572d18493cb4fc3e5b6faf57b349770cdf0114db59a2f41234cdcffbb600`;
3,895 other cases are outside that run. It verifies V2 and its frozen V1 parent.

### Gherkin newline oracle correction

String8/String9/String10 #0005 were not executed because Gherkin had already
decoded expected quoted strings into physical newlines, which Python literal
parsing then refused. The independent reader now escapes CR/LF only inside
already-tokenized quoted literals, preserving exact values, literal backslashes,
Unicode and duplicate-map-key detection. Executable expressions still refuse;
queries/expectations are unchanged and the Grafx evaluator is not the oracle.
The complete string family now reports **32 original passes, zero selected
failures/not-run**, with 3,865 outside selection:
`.grafx-tmp/fp2-string-native-final.json`, SHA-256
`189f8abdbc098c8229a69fecbac73bd56d24675c1601e0f5d5d5784f7d3c4951`.

### Regression evidence

| Receipt under `.grafx-tmp/` | Completed result | SHA-256 |
| --- | --- | --- |
| `fp2-return-star-regression.xml` | 1,182 passed, zero failures/errors/skips, 215.996 s | `5e3592e0d19b60c2096015b25c89df3dcbd6cc31befa920941c0c2f3053b166d` |
| `fp2-final-feature-check.xml` | 72 passed, zero failures/errors/skips, 4.567 s | `bd6341e4a88fd86ab9d64a9fcfde0f3f160b7ea9f48e9278c923a92ecf85891b` |
| `fp2-return-star-boundaries-final.xml` | 683 passed, zero failures/errors/skips, 147.517 s | `176fb8ce51d4da69d95564c65019f60df70748394e4f42a55cd631dbfc7c0251` |

The first selection covers wildcard features, compatibility profile,
AST/parser/analysis/planner, entity UNION/composed paths, implicit labels and
conformance tooling. The second confirms final wildcard, successor verification/
CLI and independent literal-reader behavior after the final identity-only AST
rewrite check. Counts overlap and are not additive or full-repository/Pulse proof.
The final boundary selection includes all conformance-tool tests, RETURN/WITH
wildcards and optional-unused-landing controls. The former optional wildcard
parse-refusal case now checks complete results against the unoptimized reader.
A prior run exposed an outdated WITH-flag error-message assertion: malformed
boolean fields now refuse at canonical AST admission. Its replacement checks the
exact `ast`/`invalid_ast_structure` evidence, not merely that any error occurred.
The failed intermediate boundary receipt is retained, not relabeled successful.
Documentation/API generation, links/configuration inventory, Ruff and whitespace
checks pass. No installed Pulse, production graph, release, commit or push changed.

Initial new-test errors used nonexistent `QueryCursor.fetchall()` and assumed
diagnostic AST descriptions serialize quoted identifiers; the corrected tests
consume the documented iterator and check results of the original submitted query.
The initial succession fixture used a noncanonical Gherkin outcome phrase, then
was corrected. First V2 CLI execution exposed missing predecessor forwarding to
summary verification; that forwarding is fixed and covered, not bypassed.
