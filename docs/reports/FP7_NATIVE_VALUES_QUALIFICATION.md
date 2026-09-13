# FP-7 native procedure-value qualification

Date: 2026-09-12. Branch: `feature/v0.0.6`, current development worktree.
No commit/push, release, global installation or production/Pulse data mutation.

## Native implementation and contract

[Procedure native values](../specs/PROCEDURE_NATIVE_VALUES_V1.md) adds DATE,
LOCALTIME, TIME, LOCALDATETIME, DATETIME, DURATION, LIST, MAP, native-value ANY and
VECTOR_F32/F64 signatures. Registration, native planning/bound argument admission
and runtime output validation use the declared families. ANY stays dynamic, not
a fabricated storage type. ScalarFunction retains its previous exact signatures.

`domain/query/procedure_values.py` supplies bounded, recursively owned copies,
exact native classes, independent duplicate children, cycle/depth/cardinality
admission, UTF-8 and finite/range validation, and temporal component reconstruction.
`domain/query/extensions.py` integrates the native signatures with both read and
writing callbacks, including unit results and unselected output columns. Native
storage layouts, capability bits, writer/read snapshot policy and WAL were not
changed to admit procedure values.

Feature source: `tests/api/test_procedure_native_values.py` (68 current cases).
Coverage includes direct and parameter/constructor-based invocation, implicit
argument names, NULL, callbacks receiving owned mutable containers, mutation of
reused generator outputs, alias independence, native postfix/UNWIND/aggregation,
UNION, cursors, typed temporal writes and pure/NumPy cold reopen. Refusal tests
cover invalid signatures/values, native-class forgery, recorded future zone keys,
wrong dtypes, recursive nonfinite values, cycles, depth/cardinality/byte ceilings,
unselected invalid output, ordinary subclasses and hostile metaclass comparison/
hash methods. Large strings/vectors are refused before their encoding starts.

The writing integration additionally proves failed nested temporal writes retain
prior statements and publish neither their rows nor a first temporal capability.
Post-durable `_apply_images` failures carry committed/durable proof; reopening
restores the actual nested temporal values and required capability together. These
tests use native transaction/recovery paths, not mocked query answers. They do not
claim arbitrary hardware power-loss coverage or every commit failure point.

## Executed evidence

Overlapping selections, not one complete repository regression:

| Selection | Terminal result | Local receipt |
| --- | --- | --- |
| First native-value feature probe | 41 passed, 18 failed; 25.329 s; exit 1 | `.grafx-tmp/fp7-values-first.xml` |
| Corrected feature and existing extension families | 198 passed; 31.447 s; exit 0 | `.grafx-tmp/fp7-values-focused.xml` |
| Native values, writing/invocation/numeric procedures, temporal storage/query, UNION and spill | 416 passed; 78.245 s; exit 0 | `.grafx-tmp/fp7-values-regression.xml` |
| Feature tests plus public annotations/import boundaries/language documentation | 3,516 passed; 85.641 s; exit 0 | `.grafx-tmp/fp7-values-contracts.xml` |
| Final hostile-type hardening and all affected procedure families | 241 passed; 34.705 s; exit 0 | `.grafx-tmp/fp7-values-final.xml` |
| Original FP-7 native TCK owner | 52 passed; zero failed/unexecuted selected cases; exit 0 | `.grafx-tmp/fp7-values-native-20260912.json` |

All passing pytest receipts have zero failures, errors and skips. The final
241-test run covers all 68 current new-feature cases after replacing foreign-type
equality-based membership with identity-only checks. It also covers the existing
numeric, invocation, tabular, unit and writing procedure families on that source.

The first feature probe is retained, not relabeled green. Twelve failures were
missing **existing catalog-v2 activation** in the newly written temporal-DDL test
setup. Six used list expectations where native public query results already return
tuples. Tests were aligned with these established contracts; no storage fence or
public result-shape rule was relaxed. Subsequent tests distinguish direct callback
list copies from public query tuples explicitly.

SHA-256:

- First feature probe: `e1caa083123de42bb5e816778e3a4a656c84e3437391dc2512a0cf34d95efa38`.
- Focused: `3a818fa091d9e38393a4722d6220f91afe6c583fa080f86be7cdcf345820a250`.
- Expanded regression: `a7fbac8b0a9e9770b066404a881843a3add3edcd209c53a520075788f94bea44`.
- Public contracts: `184590cce4107d22e47b956bf231dc3e78d1927b30b1f17a070ebdc7811cf5bd`.
- Final affected regression: `249e6a3aa9f6343e6d1ccb36f228e07b410cbe6847add121fd89038c3e1224cd`.
- Original TCK owner: `74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`.

The pinned TCK revision remains `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`.
V2 ledger/V1 predecessor and all exclusions are unchanged. The original procedure
receipt is byte-identical to the preceding numeric/writing owner result; 3,845
inventory entries outside this owner are unselected, not newly waived or certified.
These original cases do not replace the new native-value/fault tests.

## Reproduction and documentation

Set `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps` in the qualification shell.
The final affected pytest selection is:

```text
python -m pytest tests/api/test_procedure_native_values.py tests/api/test_procedure_invocation.py tests/api/test_procedure_numeric_signatures.py tests/api/test_tabular_procedures.py tests/api/test_unit_procedures.py tests/api/test_writing_procedures.py -q --tb=short
```

The native owner command is:

```text
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp7-values-native-20260912.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-7
```

README, roadmap, API/configuration, extension/composition/query guides, temporal
usage, comparison/compatibility and FP-7 specs reference the new contract. Generated
API, documentation links/anchors and configuration coverage, Ruff for changed/new
Python and whitespace checks are part of final acceptance.

## Remaining scope

Historical checkpoint note: the subsequent [entity increment](FP7_ENTITY_QUALIFICATION.md)
now qualifies invocation-scoped NODE/RELATIONSHIP/PATH and typed entity lists.
The following paragraph records the scope at this native-value checkpoint.

FP-7 remains partial: entity signatures and nested entities need explicit identity/
authority admission; broader read/result capabilities, recursive procedures and
schema mutation remain unimplemented. FP-6 parameterized stored LIST/MAP/ARRAY/
STRUCT and DECIMAL are separate outstanding work. Pending FP-4 decisions and full
FP-8 frozen-profile/Pulse qualification are unchanged. No narrower definition of
functional parity or new waiver is introduced by these additional signatures.
