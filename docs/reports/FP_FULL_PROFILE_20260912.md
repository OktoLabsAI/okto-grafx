# Full native functional-parity profile — 2026-09-12

## Complete baseline

The complete frozen inventory ran to completion on `feature/v0.0.6`, before the
unit-procedure increment. No owner/feature filter, inferred fixture schema or new
exclusion was used. All 3,897 scenarios remain accounted for; eight procedure
fixtures could not be admitted and therefore are **not** passes.

| Owner | Passed | Failed | Not run |
| --- | ---: | ---: | ---: |
| FP-2 expressions/results | 1,976 | 0 | 0 |
| FP-3 patterns/paths | 528 | 17 | 0 |
| FP-4 mutations/subqueries | 255 | 35 | 0 |
| FP-5 temporal expressions | 1,004 | 0 | 0 |
| FP-7 procedures | 23 | 21 | 8 |
| FP-8 upstream use cases | 30 | 0 | 0 |
| **Entire upstream inventory** | **3,816** | **73** | **8** |

The frozen required profile has **3,816 passed, 51 failed, eight not run**.
The other 22 failures are the already-recorded multiple-label architectural
divergences: 17 in FP-3 and five in FP-4. Required FP-4 failures remain 29 label
mutations and the Set1 #0010 nested-storage oracle conflict. Required procedure
failures cover implicit invocation/results, type/signature diagnostics and numeric
signature/coercion semantics. Four unit and four NUMBER fixtures caused the eight
not-run cases. No new waiver was introduced.

All 30 upstream use-case cases pass, but that is **not** completion of FP-8:
supplemental contracts, remaining feature packages, final repository regression
and final Pulse acceptance still apply. Persisted DECIMAL/parameterized collection
types and writing procedures cannot be certified by these upstream counts.

Receipt: `.grafx-tmp/fp-full-profile-native-20260912.json`, SHA-256
`f80d330d1dd5bd73010f514b24c354e2a059be657960c4324232ba11ca72fde4`.
Terminal exit **1**, as required for a profile containing failures. The process
completed before any native unit-procedure source change was applied.

```powershell
$env:PYTHONPATH = 'src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 `
  --output .grafx-tmp/fp-full-profile-native-20260912.json `
  --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json `
  --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful
```

Reference revision: `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`, 220 features.
Ledger V2 SHA-256:
`4334116c3dc52cbe78e07b0889d68f990c33bca6b850a62551761f937b89255e`.
The original V1 predecessor and all required expectations remain unchanged.

## Unit-procedure follow-up

The [native unit contract](../specs/UNIT_PROCEDURES_V1.md) was first implemented in
an isolated source copy while the complete baseline ran. The initial 16 tests
confirmed the missing registration; an initial candidate command still selected
the root package through pytest's `pythonpath` configuration and is **not**
candidate qualification. Overriding that configuration explicitly produced a
40-test passing candidate run. A subsequent 502-test candidate selection had one
new test assertion error (`details` is a method); the test was corrected without
changing the native API. A 20-test unit rerun passed. These preliminary runs do
not replace the final root-source receipt below.

After the complete baseline exited, the native patch and reference fixture
admission were applied to the root source. The final affected regression passes
**513 tests, zero failures/errors/skips**, 35.011 seconds, terminal exit **0**.
This covers unit/tabular/scalar extension APIs, parser, AST, analysis, imports,
writing subqueries, limits, WITH wildcard, public plan ownership and fixture
admission. The unit cases include both codecs, cold reopen, failed-statement
rollback with earlier successful work retained, NULL/empty input, early cursor
close, permission isolation and callback-free explicit `pure_unit` plans.

Receipt: `.grafx-tmp/fp7-unit-qualified-regression.xml`, SHA-256
`4deaf1581e31cfeff4da238b042f249512bb18dad496a0071c55fd7e6102fd15`.

A separate **52-case FP-7** native rerun records **27 passed, 21 failed, four not
run**. All preceding 23 passes remain passes. Call1 #0001/#0003/#0004 now pass
through native unit procedures; #0012 also passes through its original undefined
variable error. #0002 is now executed and fails because implicit no-parentheses
invocation is not implemented. The four NUMBER fixtures remain not run.
No input query or expected result was rewritten.

Receipt: `.grafx-tmp/fp7-unit-native-20260912.json`, SHA-256
`6e3df3a4bd7f4a6998c041a00e28cfa44a18dcd1286ff3dbe1cd5787ab218fd2`.
Same command as above with `--owner FP-7` and that output path; terminal exit **1**.
The report's 3,849 not-run entries include **3,845 outside this selection** and
four actual fixture blockers. Do not combine this selection with the historical
complete run and present the result as a newly executed full profile.

This is a bounded completed increment, not completed functional parity, a release,
a global installation or a new production-data qualification.

## Public-contract and documentation follow-up

The wider source-surface selection initially ran **3,445 tests, 29 failures**,
zero errors/skips, 56.457 seconds, terminal exit 1. All failures were missing
docstrings in 29 modules, including temporal values, internal decimal arithmetic,
query scope helpers and entity/transport code from earlier parity increments.
Receipt `.grafx-tmp/fp7-unit-public-contracts.xml`, SHA-256
`64f9d393a26195ad66b715c79da3a15407b281ce5571cbbc5bc4e89ea322fb55`.

Added the 128 missing definition docstrings, including nested functions covered by
the existing test policy, without weakening that policy. Before/after ASTs for
all **240 source modules** were compared with docstrings removed: no executable
AST changed. The original failed receipt is retained. The subsequent identical
selection passes **3,445 tests, zero failures/errors/skips**, 63.439 seconds,
terminal exit 0. Receipt `.grafx-tmp/fp7-unit-public-contracts-final.xml`, SHA-256
`ca8083216200609f7d5f0de501f96fdcfe41d8ed4def0ba6794e73857eb7eb9a`.

The selection contains `test_public_surface`, `test_fp_annotation_contracts`,
`test_import_boundary` and `test_language_surface`. API reference regeneration,
documentation checking (39 configuration fields and 11 preserved source plans),
Ruff on all changed/untracked Python files and `git diff --check` pass.
README, roadmap, query/configuration/extension guides and the unit specification
now expose the new contract. These checks qualify the documented increment;
they do not erase the remaining frozen-profile failures or certify a final release.
