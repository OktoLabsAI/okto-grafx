# Integrated V3 native query-profile qualification

[Active profile](../conformance/PROFILE_V3.md) · [Supplemental coverage](../conformance/EXTENSION_COVERAGE.md)
· [Native labels](../specs/NODE_LABELS_V1.md) · [Parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md)

The complete current-source native execution on September 12, 2026 has
**3,896 passed / one failed / zero not run** against the original TCK expectations.
All **3,896 required V3 cases pass**. The only upstream failure is the explicitly
authorized `clauses/set/Set1.feature#0010` lists-of-maps divergence.

This closes the required **query-profile execution**, not the entire parity DoD.
The complete repository regression, remaining supplemental/competitor evidence and
final installed paired Pulse API/browser/MCP acceptance are separate requirements.
No full openCypher, all-Neo4j-features, performance or production-certification claim
is made. No global install, production mutation, release or publication occurred.

## Complete selection and unchanged oracles

Pinned source: openCypher 2024.3, revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`, 220 features, 3,897 expanded cases.
Selection has an empty feature prefix, no owner filter and all 3,897 unique IDs.
V3, its V2 predecessor and V1 ancestor were verified against source before execution.
No inferred fixture-schema option was supplied; native flexible/multiple-label
fixtures and procedure registrations execute through the stateful backend.

| Primary owner | Required passes | Upstream divergence failures |
| --- | ---: | ---: |
| FP-2 expressions/clause semantics | 1,976 | 0 |
| FP-3 entities/patterns/paths | 545 | 0 |
| FP-4 updates/subqueries | 289 | 1 |
| FP-5 temporal families | 1,004 | 0 |
| FP-7 procedures | 52 | 0 |
| FP-8 integrated upstream cases | 30 | 0 |
| Total | **3,896** | **1** |

These are classifications of one complete run, not sums of historical focused
receipts. FP-6 storage/consumer contracts and modern extensions are supplemental;
absence of an FP-6-owned upstream case does not remove those requirements.

Set1 #0010 expects TypeError/runtime/InvalidPropertyType for storing `[{num: 1}]`.
Grafx intentionally succeeds. The report preserves the actual failed upstream
assertion, and the runner terminates with **exit 1** rather than disguising it as
a pass. The separate profile summary reports 3,896 required passes and one failed
architectural divergence. There are no additional divergences, unclassified cases
or unexecuted required cases.

The earlier complete run's Graph3 #0009 error-phase defect is corrected and passes
here, together with every other required original case. The earlier two-failure
receipt remains unchanged; this is a new integrated execution after history,
copy and transfer integration, not a relabeling of that receipt.

## Receipt and candidate

Receipt: `.grafx-tmp/fp-multilabel-integrated-v3.json`.
SHA-256 `9aed1496d346d23dd9a140fb55ae4549d6d31eedd695ef35bc0ae92ef87e20a9`.
Post-run audit checked exact unique ID equality with the frozen V3 ledger,
complete selection, owner counts and the sole non-passing case's identity/reason.

This run imports Grafx from the checkout on Windows/CPython 3.13.1. Its 251 package
files match the [installed label candidate](FP_NODE_LABEL_WHEEL_QUALIFICATION.md),
wheel SHA-256 `3fbd942891d8594feebe998bdddea55b6dd8fff801e004369761a7e2958dbb13`.
The query run is source execution; the wheel matrix separately proves installed
format/old-reader behavior. Neither substitutes for installed Pulse execution.

## Reproduce

Use a fresh output filename, the unchanged pinned checkout and all three ledgers:

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output path/to/fresh-full-v3.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V3.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V2.json --ancestor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful
```

Expected for this candidate: upstream 3,896/1/0, required profile 3,896/0/0,
and nonzero runner exit for the preserved upstream divergence. Do not filter out
Set1 #0010 or turn other failures into acceptable results to obtain a zero exit.
