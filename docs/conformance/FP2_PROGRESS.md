# FP-2 working evidence: numeric literals, headings and postfix access

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

One profile inconsistency needs an explicit decision, not a silent exception:
`Comparison1.feature#0028`–`#0031` and `Comparison2.feature#0012`–`#0015` remain
required but compare NaN from `0.0 / 0.0`, contrary to checkpoint A's finite
arithmetic policy. The user has been asked whether to retain that policy with an
explicit eight-case divergence decision or support NaN expression values without
storage. Until a decision is recorded, these eight cases stay required and failing;
neither the ledger nor numeric semantics has been changed to waive them.

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
