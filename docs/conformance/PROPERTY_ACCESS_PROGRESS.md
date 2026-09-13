# Property-access checkpoint: FP-3 and FP-4

September 12, 2026, development `feature/v0.0.6`. This is progress under the
[functional parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md), not full-profile
acceptance or a new reduced profile.

## Owner-wide baseline after SQ-13 and Pulse source qualification

Both original owner selections ran without fixture/schema adaptation, verified
against frozen V2 and predecessor V1, and reached terminal exit 1:

| Receipt in `.grafx-tmp/` | Original passes | Required failures | Declared divergence failures | Outside selection | SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- |
| `fp3-post-sq13-owner-native.json` | 516 | 12 | 17 | 3,352 | `af8f9b54f6db720ef92556a75a02e356207cfc26187e4f20166d61de948d7c7a` |
| `fp4-post-sq13-owner-native.json` | 147 | 138 | 5 | 3,607 | `d61b0ed63912a30070186e18814bec5ac598f6b084ef8822ab0dadabb0b85ca9` |

Every selected case executed. Counts from different runs or earlier feature
selections are not summed to manufacture a latest full-profile result.
FP-3's required failures are Graph7 dynamic entity properties (3), Graph8
`keys(entity)` (8), and Graph5 #0002 (1). A direct original-fixture probe proves
the last failure is `A node label cannot reuse a relationship type`,
`field=label`, `value=T2`; it is not reclassified as a multiple-label divergence.
Separate node-label/relationship-type namespaces remain required.

FP-4 includes mutations and existential expressions, not just the supplemental
modern CALL profile. Its 138 required failures include absent property/label
REMOVE, map-wide SET, MERGE actions, expression DELETE, existential subqueries,
large-query admission and native error/effect contracts. These remain in scope;
the already-passing writing-CALL/UNION tests do not close this owner selection.

## Implemented increment

- `keys()` admits native nodes/relationships, maps and NULL through shared entity
  scalar typing and owner-overlay property materialization. Entity NULL values
  and physical endpoint columns are absent; input-map NULL entries remain keys.
- Entity string-key subscripts see the current owner overlay using native identity,
  including aliased/collected entities. Missing properties/NULL return NULL;
  non-string keys and inaccessible/deleted entity content refuse. Typed column
  lookup remains direct; there is no per-result full-graph scan.
- Property `REMOVE` lowers to native SET-to-NULL. The shared SET path now treats
  a present nullable entity binding as a no-op; an absent/invalid binding still
  refuses. Same statement marks, write classification, eager effects, quotas,
  typed constraints, original snapshot/OCC and WAL/COMMIT behavior remain active.

No public API method, setting, dependency or durable layout is added. No original
TCK query, expected value/effect/error or frozen inclusion decision is changed.
The typed error mapper adds KEYS only under native entity-function reason/phase
evidence; it never consults a scenario's expected error.

The first focused run exposed genuine nullable-target, missing typed dynamic-key
and stale collected-entity reads; those were corrected. Two test assumptions were
also corrected: keys are not implicitly sorted, and lazy runtime-parameter errors
do not require accepting statically invalid literal arguments.

## Qualification

Focused feature/entity-scalar tests passed: 78 tests, zero failures/errors/skips,
27.572 s in `entity-keys-remove-focused.xml`; SHA-256
`e54a1e80546a2b5ccc102b0d286a4460f5115c8b5a42606bed4954e88ba73c4f`.

The final-code FP-3 owner rerun reaches **527 original passes / one required
failure / 17 declared divergence failures / 3,352 outside selection**. All 545
selected cases executed, with no schema adaptations. The only required failure
is Graph5 #0002, the distinct namespace requirement described above. Receipt
`fp3-property-access-final-native.json`, SHA-256
`1ec7c303467150b0bf738800df1be298d7d0109d5876277d0c7eb505e19021e0`;
terminal exit 1 is expected evidence of that remaining failure, not a green
full-profile claim. The original eleven KEYS/dynamic-property cases now pass.

The original REMOVE family now has 21 passes, 10 remaining required failures
(label removal) and two declared multiple-label divergence failures, with 3,864
outside selection. Receipt `remove-native-first.json`, SHA-256
`2527c2ccaeb5fe419804d7cc5a00f51892e3513dae60818fc7d1eaa2e78d4e94`.
All property-removal cases pass; the family as a whole remains incomplete.

Final grouped regression: **1,911 passed, zero failures/errors/skips**, terminal
exit 0, 346.740 s. Receipt `entity-keys-remove-regression.xml`, SHA-256
`eb6fa0e75e97144271cb300b2b8918a0d495791bba2b8eecb436284f6cffba14`.
It selects 50 files under `tests/query`, `tests/tools`, `tests/txn` whose paths
match `test_.*(entity|scalar|subscript|postfix|set|write|updat|union|optional|read_execution_control|memory_spill|parser|analysis|ast_structure|query_engine|tck_errors|iterator_cleanup|projection|transactional_schema)`.
This covers native entity/scalar/subscript and mutation composition, parser/AST/
analysis boundaries, memory/cursor/iterator safety, identity concurrency/faults
and prior CALL/UNION recovery cuts. The 78 focused tests overlap this total.
It is not a full-repository or new installed-Pulse run. The first focused failing
receipt is superseded; the previous owner baselines stay visible as diagnostics.

Public query, entity/composition usage, comparison, ROADMAP and plan-status links
are updated. API generation, documentation/39-setting coverage, modified/new
Python lint and diff whitespace checks pass. No limits or skips were introduced
to obtain the result.

Reproduce using `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps` and
`python tools/check_opencypher.py --checkout .grafx-tmp/opencypher-2024.3
--execute-stateful --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json
--predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --owner FP-3
--output <fresh-receipt>`; substitute FP-4 or `--feature-prefix clauses/remove`
for the other selections. Do not add `--infer-fixture-schema`.

Installed Pulse, production data, release, commit and push are unchanged.
