# Functional-parity declaration and boundary qualification

September 12, 2026; source qualification on `feature/v0.0.6`, not a release,
installed-Pulse qualification or completion of the functional-parity plan.

## Findings and corrections

The broader vector-maintenance run found 75 failing public-surface assertions:
21 modules with incomplete public-function annotations, 23 without postponed
annotation evaluation, and 31 without explicit export declarations. Counts are
per test case and overlap by module; they are not 75 runtime defects. The original
failed receipt remains recorded in [qualified vector maintenance](../specs/VECTOR_QUALIFIED_MAINTENANCE_V1.md).

Corrections declare the actual exports and type contracts, including structural
catalog selection shared by live/detached catalogs, temporal result unions,
query-tree transformations, history authority, Arrow types and streaming copy/
traversal results. Internal heterogeneous AST visitors intentionally take/return
`object`; typed public operations preserve their declared concrete results.
`TemporalValue` now has one model-level definition, re-exported unchanged from
the arithmetic module. The native `search_for_table` seam explicitly declares
the search/control parameters it forwards instead of an unannotated keyword bag.
No value grammar, transaction policy, resource setting or durable format changes.

Exports expose the module's intended definitions, not accidentally imported
helpers. This is not a promise that private engine helpers are a stable application
API. Integrations continue through the documented package/Database contracts.

## Exact pure-algorithm boundary

The import audit also found five inadmissible imports in four temporal modules
(five failing tests including the aggregate gate): `Fraction` and regular-expression
algorithms. These are deterministic algorithms over bounded input, not I/O,
clock, randomness, locale or platform decisions. Moving native grammar/arithmetic
into a mechanism adapter or substituting floating-point arithmetic would distort
the architecture or exact semantics.

Admission is therefore explicit and narrow in `EXACT_PURE_ALGORITHM_IMPORTS`:

- `fractions.Fraction`, unaliased, only in `domain.temporal_arithmetic`,
  `domain.temporal_components` and `domain.temporal_text`;
- `re.compile` as `_compile_pattern`, `fullmatch` as `_fullmatch`, `search` as
  `_search` and `split` as `_split`, only in `domain.temporal_text`;
- `re.fullmatch` as `_fullmatch`, only in `domain.model.temporal_interchange`.

Neither module was added to the general stdlib allowlist. Bare module imports,
other symbols/bindings/consumers and mixed permitted/forbidden imports still
refuse. Eight positive and twelve negative probes lock down this boundary.
Additional tests prove that admitted regex calls use literal ASCII grammars and
that temporal rational results do not depend on Python's decimal precision or
rounding context. Existing temporal tests cover input bounds and exact values.
This is a documented algorithm admission, not an exception for mechanism access.

## Evidence

Receipts under `.grafx-tmp/`; all passing rows have zero failures/errors/skips.
Counts overlap and must not be added to claim a full repository regression.

| Receipt | Result | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `fp-public-contracts-corrected.xml` | 2,025 passed | 30.210 | `1deec9270337cc90ce318d90020c9bef59043b0fcdee8c5a9b0686241173482b` |
| `fp-contracts-behavior-regression.xml` | 1,976 passed | 291.929 | `f2fcf9c366193c7efdbee83c898fa02241b734cd05531101309c8317429fc34c` |
| `fp-contracts-boundary-qualified.xml` | 695 passed | 11.551 | `9422d318d9d0c483b68443d66d19c4c823ad519f6cd4146913737c0659aaf280` |
| `fp-contracts-final-quality.xml` | 2,357 passed | 35.440 | `393c6e4b871d7b4cf3bbed787f1cc3ae559aa9bcbfe2a8967e0bb57d6e8108c6` |

The final quality run follows every source change and combines the entire
public-surface gate, entire import-boundary gate and the new runtime annotation/
algorithm assertions. Documentation validation passes all links/anchors, 39
configuration fields, public signatures/DTOs and 11 preserved plans; changed/new
Python lint and whitespace checks also pass.

The 1,976-case suite covers native temporal primitives/codecs/admission/arithmetic/
text/components/truncation/zones, temporal query/results, EXISTS and subquery
imports, Arrow, temporal storage/transfer, flexible copy, namespace history/copy/
transfer, physical vector owners, qualified maintenance, resumable transfer and
runtime annotation/export resolution. It preceded the narrow regex-import
rewiring; the 695-case suite subsequently covers the affected algorithms, runtime
contracts and boundary enforcement. No original TCK fixture/oracle/profile ledger
was edited, and these are pytest counts, not a newly executed TCK profile.

The initial import-audit failure is retained:
`fp-contracts-import-boundary.xml`, 272 passed / 5 failed, SHA-256
`00d9bce0fc4e4589f030f067a1c157b08e09ee321980dab0cfe332896b386996`.
No source exceptions were silently skipped or renamed as passing.

## Remaining delivery scope

This closes the identified declaration failures and pure-algorithm admission.
It does not close lower-level shared-space vector mutation qualification, missing
legacy artifact repair, remaining required FP-3/FP-4 cases, later FP-6/FP-7 features,
full frozen-profile execution or paired Pulse/package acceptance. No production
data, global installation, commit/push, release or branch change was performed.
