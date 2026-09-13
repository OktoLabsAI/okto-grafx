# FP-7 native procedure invocation qualification — 2026-09-12

Subsequent [native numeric signatures](../specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md)
close the six cases remaining in this historical checkpoint: **52/52 now pass**.
The original receipts/counts below remain unchanged; writing capabilities are
still required before FP-7 completion.

## Implemented scope

[Invocation contract](../specs/PROCEDURE_INVOCATION_V1.md): explicit immutable
argument names, standalone implicit parameters, automatic standalone outputs,
standalone `YIELD *`, and native signature/alias/parameter-name diagnostics.
Resolution is handle-local and callback-free. It validates canonical AST fields,
does not compare or format literal payloads, and refuses forged standalone flags
that would replace an explicit RETURN expression. Existing explicit tabular and
unit callbacks retain their permissions, budgets and statement rollback.

Missing parameter names now carry `reason=missing_parameter` and
`query_phase=planning` at the actual pre-operator admission boundary. Evaluated
parameter values are not automatically classified as compile-time failures.
No new storage capability, database setting, graph-writing callback authority or
implicit transaction retry is introduced. Nothing was installed into Pulse or
published by this increment.

## Regression evidence

All final selections below have zero failures/errors/skips and terminal exit 0.
Counts overlap; do not sum them into one regression result.

| Selection | Passed | Seconds | Receipt under `.grafx-tmp/` |
| --- | ---: | ---: | --- |
| Initial feature/extension/fixture and error mapper tests | 87 | 5.235 | `fp7-invocation-qualified.xml` |
| Combined query/extension/API and complete tools regression | 1,230 | 181.400 | `fp7-invocation-regression.xml` |
| Query engine, expression dispatch, public parameter boundary and CLI discovery/search | 394 | 41.446 | `fp7-invocation-parameter-regression.xml` |
| Final forged-AST/literal hardening, extension APIs, fixture and error mapper tests | 101 | 6.134 | `fp7-invocation-hardening.xml` |

SHA-256, in that order:

- `c01caca59e743458ad268a4b899ff2fe05417fedf278f66622e00d62a309b985`
- `9b4f1845a577f69c05c7cfc38c5155ebefd9a2e5e5bd4c0ca33df4c141aace11`
- `5959f0b76a67dc27641b83e4cee33536ab44d977281d3237dec3b166a1c982c2`
- `690747a23d388efdd32c4eda82c2c29923a5fa369d430ce2c15dbd3c4b730024`

The combined selection includes procedure/unit/scalar APIs, parser, canonical AST,
analysis, import scopes, writing subqueries, limits, WITH wildcard, public plan
ownership and all `tests/tools`. The final 101-test run follows the extra
standalone-projection/literal-payload checks; the two broader selections ran the
preceding implementation. No whole-repository or final multi-package acceptance
is inferred from these selections.

Preliminary failures remain recorded: the prior unit test expected standalone
tabular output omission to be refused; that authorized feature is now accepted,
and the negative test continues to refuse omission **inside** a query. A new
direct planner test incorrectly passed a public CatalogView where the domain API
requires Catalog; it was corrected without changing the API. The first expanded
TCK run admitted in-query `YIELD *` incorrectly. The fixed profile requires its
refusal; the implementation and negative tests now enforce standalone-only use.

## Frozen original procedure family

The final 52-case FP-7 selection records **46 passed, two failed, four not run**.
All 27 passes from the unit-procedure checkpoint remain passes. The two failures
are `Call3 #0005/#0006`, integer inputs to a FLOAT procedure. Four not-run cases
are `Call3 #0001..#0004`, whose NUMBER signatures are not yet admitted. Those
remaining features are required work, not new exclusions or waived failures.

Receipt `.grafx-tmp/fp7-invocation-qualified-native-20260912.json`, SHA-256
`fdbc946152964122f37cbbb01f750f103e49bc816ea53b5a0282134dcdd38ada`.
Terminal exit **1**, correctly preserving the remaining failures. Its 3,849
not-run entries include **3,845 outside this owner selection** and four actual
procedure fixture blockers. The final rerun follows the AST hardening.

```powershell
$env:PYTHONPATH = 'src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 `
  --output .grafx-tmp/fp7-invocation-qualified-native-20260912.json `
  --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json `
  --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json `
  --execute-stateful --owner FP-7
```

The fixture adapter passes declared argument names into the same native registry;
it does not rewrite the query under test or expected results. Signature expansion
for lexical preflight uses the same native resolver as build_plan/execute. Error
mapping requires exact native reason, field and phase evidence; mapper tests
remove each piece independently and prove an arbitrary exception cannot match.

The [prior complete inventory](FP_FULL_PROFILE_20260912.md) remains a historical
full run, not recomputed by substituting these owner results. NUMBER, numeric
coercion, broader native value signatures, restricted writing capabilities and the
other functional-parity work packages remain open. This is not final FP-7/FP-8
acceptance or a claim of full Cypher compatibility.

## Final documentation and public surface

All **3,459** selected public-surface, annotation, import-boundary and language/
docstring checks pass, zero failures/errors/skips, 70.854 seconds, terminal exit 0.
Receipt `.grafx-tmp/fp7-invocation-public-contracts.xml`, SHA-256
`84987194c6ffbb5e2ba9e44190754e6db1a8b07bc5acee111f0f26324c643a74`.
The new native resolver participates in the explicit runtime annotation check.
API reference regeneration, documentation links/anchors and 39 configuration-field
coverage, Ruff across all changed/untracked Python and whitespace checking pass.
The invocation specification and query/configuration/extension guides document
the new registration field, syntax, error phases, authority and remaining limits;
roadmap and compatibility accounting retain original and selected-run evidence.
