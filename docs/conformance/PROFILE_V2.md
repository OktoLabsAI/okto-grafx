# Profile V2: authorized flexible-model scope expansion

Historical profile, superseded by [V3](PROFILE_V3.md) after explicit multiple-label
and nested-storage decisions. The ledger and results below remain unchanged.

Decision `FP-MODEL-EXPANSION-20260911` implements the user's authorization for
native unlabeled/heterogeneous storage and expression NaN. It changes required
scope, **not results or the definition of a passing case**. Full FP-2–8 acceptance
remains outstanding under the [unchanged delivery plan](../specs/FUNCTIONAL_PARITY_PLAN.md).

| Profile | Required | Architectural divergences | Source cases |
| --- | ---: | ---: | ---: |
| Historical [V1](FP_CASE_LEDGER_V1.json) | 3,470 | 427 | 3,897 |
| Historical [V2](FP_CASE_LEDGER_V2.json) | 3,875 | 22 | 3,897 |

All 220 upstream feature files remain pinned to revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`. Source/case hashes, expected rows,
errors/phases/effects, ownership and historical baseline observations are preserved.
The [22 supplemental contracts](EXTENSION_SCENARIOS_V1.json) remain mandatory.

## Exact source-reviewed transition

- 402 cases excluded solely for unlabeled creation become required.
- Three cases combining unlabeled creation and expected expression NaN become
  required: ReturnOrderBy1 #0011/#0012 and WithOrderBy1 #0022. Their recorded
  nonfinite counterexample is exactly `NaN`, not stored nonfinite data or infinity.
- Nine cases containing both unlabeled and multiple-label creation retain only
  their concrete multiple-label counterexamples.
- Thirteen multiple-label-only exclusions are unchanged. The resulting 22
  divergences remain visible; they are neither passed nor newly excluded cases.

The V2 `scope_changes` array records all 414 changed decisions with case checksums,
previous profiles and current profiles. No execution failure was used to decide
scope. V1 is preserved byte-for-byte (file SHA-256
`f5158a4010c52e73b7901ff0401c5e789c09eb5eae4f5f58378132c5c7ecbd84`).
V2 file SHA-256:
`4334116c3dc52cbe78e07b0889d68f990c33bca6b850a62551761f937b89255e`.
Its separate predecessor canonical-JSON digest is
`6fe672df0a2af481c83f0cf35345468070866dc48e0573846102b6ef1be45baf`;
this digest intentionally does not depend on file whitespace.

## Verification and reproduction

The verifier requires the predecessor, verifies both against source, reconstructs
the monotone authorized transition and compares the complete successor. Mutated
expectations, ownership, observations, scope counts, decisions or audit provenance
refuse. It does not turn an old failure or not-run observation into a pass.

```powershell
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/v2-check.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/v2-fp2-run.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-2
python -m tools.review_tck_profile --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/v2-reproduced.json --expand-ledger docs/conformance/FP_CASE_LEDGER_V1.json --decision FP-MODEL-EXPANSION-20260911
```

Use a fresh output path for the review generator; it refuses overwriting existing
reviews/ledgers. Draft inventory generation also refuses replacing either frozen
profile. The production database format and configuration are unaffected.
Source tests: `tests/tools/test_tck_profile_succession.py`, existing ledger/profile
tests and the inventory CLI. Native execution receipts remain separate in
[FP-2 evidence](FP2_PROGRESS.md) and [FP-3 evidence](FP3_PROGRESS.md).

The final 683-test boundary regression includes the complete conformance tooling
suite, successor tamper/CLI tests and the affected query boundaries; it passes
without failures/errors/skips. Exact receipt/hash and non-additive scope are in
[FP-2 regression evidence](FP2_PROGRESS.md#regression-evidence).
