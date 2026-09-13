# Profile V3: native multiple labels and retained nested properties

[Delivery plan](../specs/FUNCTIONAL_PARITY_PLAN.md#authorized-multiple-labels-and-retained-nested-storage)
· [Active ledger](FP_CASE_LEDGER_V3.json) · [Historical V2](PROFILE_V2.md)

The user authorized two separate decisions on September 12, 2026:

- `FP-MULTILABEL-20260912`: implement multiple labels per node, withdrawing the
  model exclusion rather than extending it to failed label-action cases.
- `FP-NESTED-STORAGE-20260912`: retain native lists of maps and record only the
  opposite pinned Set1 #0010 expectation as an explicit architectural divergence.

| Profile | Required | Architectural divergences | Source cases |
| --- | ---: | ---: | ---: |
| Historical V1 | 3,470 | 427 | 3,897 |
| Historical V2 | 3,875 | 22 | 3,897 |
| Active V3 | 3,896 | 1 | 3,897 |

The 22 former multiple-label divergences become requirements. The 29 label-action
failures already required by V2 are still required; none are waived. Set1 #0010
is the only newly excluded oracle. The 23 scope changes carry original case hashes,
previous/current profiles and individual decision identifiers. Every source file,
expanded case, expectation, owner and historical observation remains unchanged.
The 22 supplemental extension contracts also remain required. This is a scope
transition, **not evidence of implementing multiple labels or passing the profile**.

## Exact nested-storage divergence

Pinned revision: `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23` (TCK 2024.3).
Case: `clauses/set/Set1.feature#0010`,
SHA-256 `101a110fe6773e7cc0e3d04f65833ad656e14105c34ed0cd677212689b4564fd`.

```cypher
CREATE (a)
SET a.maplist = [{num: 1}]
```

The unchanged upstream oracle expects **TypeError at runtime,
InvalidPropertyType**. Grafx deliberately succeeds and durably preserves the
native list/map/int types. No storage restriction is added to make that negative
case pass. Other SET cases, nonfinite-value rejection and invalid-value rollback
remain requirements; this decision is not a general exemption for SET failures.

The native Set1 family rerun produced **10 passed / one failed upstream oracle**,
with 3,886 cases outside selection. Required-profile failures in that selection:
zero. The runner exits **1**, honestly retaining the divergence as a failed
upstream expectation. It does not synthesize an error or count the case as passed.
Receipt: `.grafx-tmp/fp-v3-set1-native.json` (hash below).
Independent positive tests execute that exact query with pure and NumPy codecs,
checkpoint/reopen through pure, and reject nested NaN without losing prior data:
`tests/query/test_any_properties.py`.

## Verification and reproduction

V3 requires **both** frozen predecessors: V2 and its V1 ancestor. The verifier
reconstructs V2 from V1 and V3 from V2 before comparing the complete document.
It verifies source/case hashes, exact expectations, scope changes, provenance,
counts and preserved observations. The new divergence is tied to the exact case
hash above, not a broad pattern or an allowlist inferred from failures.

```powershell
python -m tools.review_tck_profile --checkout .grafx-tmp/opencypher-2024.3 --output path/to/fresh-v3.json --expand-multilabel-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --decision FP-MULTILABEL-20260912 --nested-storage-decision FP-NESTED-STORAGE-20260912
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output path/to/fresh-v3-check.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V3.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V2.json --ancestor-ledger docs/conformance/FP_CASE_LEDGER_V1.json
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output path/to/fresh-set1-run.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V3.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V2.json --ancestor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/set/Set1.feature
```

The review generator refuses overwrites. Existing V1/V2 recipes remain valid for
historical reproduction. Source tests: `tests/tools/test_tck_profile_v3.py`,
`tests/tools/test_tck_profile_succession.py` and the existing profile/ledger suite.
No runtime capability, format bit, configuration, release or Pulse installation
is introduced by this bookkeeping change. Native multiple-label work and final
full-profile/Pulse acceptance remain outstanding.

## Receipts and immutable predecessors

| Artifact | SHA-256 |
| --- | --- |
| Historical V1 ledger (unchanged) | `f5158a4010c52e73b7901ff0401c5e789c09eb5eae4f5f58378132c5c7ecbd84` |
| Historical V2 ledger (unchanged) | `4334116c3dc52cbe78e07b0889d68f990c33bca6b850a62551761f937b89255e` |
| Active V3 ledger | `494ac0251a96990f8dc5da9d7b4dcf611a41061317ec582f140447efdc3e2b50` |
| `.grafx-tmp/fp-v3-set1-native.json` | `f74fc851764712979d602af678f4372df76d6393bb3ec8e9f84f44cddb90cc79` |

V3's canonical predecessor digest is
`cac535b6dcd78c85ade94a1c84eeed8d1185874aa0ca0b98056eb6f7f9c3fa62`;
it is the sorted canonical JSON digest, distinct from V2's file hash.

Focused profile/ledger/native nested-storage regression: **71 passed**, zero
failures/errors/skips, 6.542 s, `.grafx-tmp/fp-v3-first.xml`, SHA-256
`ad0e5f352184de07f23c0ba2f38e4b864eeca27e0808c870dc7acfa7224bcd79`.
Subsequent complete conformance/qualification-tool boundary selection, including
the new report/draft overwrite guards: **434 passed**, zero failures/errors/skips,
17.469 s, `.grafx-tmp/fp-v3-boundary-regression.xml`, SHA-256
`3bec03bfdb84b22cda95c118ec607f7e19e9d9a1080c1945ce424ed4dfb78f38`.
These overlapping selections are not added, and neither replaces a complete native
TCK run on the final implementation.

The final [type-transfer corrective regression](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md#logical-artifacts-and-the-resumable-error-correction)
also reruns this complete profile tooling: **558 passes**, zero failures/errors/skips,
including the subsequently extended installed-importer verifier and native
consumer/crash-resume tests. Its receipt is separate from the upstream Set1 result.
