# FP-2 working evidence: numeric literals, headings and postfix access

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

All five implementation fronts now have focused feature evidence. Remaining
required-case failures and cross-package dependencies remain open. General temporal property values and
entity identity/graph scalars belong to FP-5 and FP-3 respectively. No current
result justifies claiming all FP-2-owned cases pass.

Pulse consumers using explicit `AS`, simple `n.id` headings or positional rows
retain their contract. Consumers keyed by normalized expression spelling must use
the submitted spelling or add an explicit alias; this is a coordinated breaking
change, not a legacy mode. Relevant existing Community tests include
`test_grafx_cypher_executor.py`, `test_grafx_discovery_key_decisions.py`,
`test_grafx_graph_transaction.py` and `test_grafx_query_semantics_migration.py`.
These Pulse suites have **not** been rerun for this checkpoint: affected-consumer
regression remains required at checkpoint B and final FP-8. No Pulse production
data, global installation or Core provider-specific code was changed.
