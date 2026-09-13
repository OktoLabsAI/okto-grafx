# FP-6 decimal query and equality-index qualification

September 12, 2026, source `feature/v0.0.6`. This follows the
[native storage checkpoint](FP6_DECIMAL_NATIVE_QUALIFICATION.md), not a release,
installed-Pulse qualification or full FP-6/profile completion. No frozen case,
exclusion or transactional concurrency/durability policy changed.

## Evidence scope

Native tests now cover explicit exact/rounded conversion; arithmetic and signs;
INT64 promotion versus refused implicit DOUBLE arithmetic; wide values, finite
DOUBLE exact-ratio comparisons, infinity/NaN/NULL/BOOL, recursive equality and IN;
SUM/AVG cancellation beyond the temporary 38-digit envelope and final overflow;
MIN/MAX; declaration-preserving storage after query updates; rollback/reopen.

Memory/spill tests intercept `LocalQuerySpillFactory.open` and require an actual
workspace, comparing full results against unbounded execution. They exercise
DISTINCT, grouping, count-distinct, mixed order, TopK and aggregates including
nested decimals and large keys. A malformed private decimal order marker refuses.
Separate input-order tests cover mixed DECIMAL/DOUBLE refusal before DISTINCT,
and consistent INT64/DECIMAL aggregate type/scale even when the native decimal
representative is discarded. Fraction is an independent finite comparison oracle.

Typed equality-index tests cover pure/NumPy and all three hash layouts. A forbidden
table-scan hook proves the exact probes actually take the index path, not merely
an EXPLAIN node with a runtime fallback. Tests include cross-scale and exact float
probes, binary 0.1's inexact refusal, PK uniqueness/MERGE, values beyond 2**53,
updates, rollback, reader snapshots, reopen and verify. Composite keys and schema
parameter-sensitive PK memo identity are exercised. Unsupported decimal ordered
and full-text index creation must leave the committed LSN/definitions unchanged.

## Local receipts

XML artifacts under `.grafx-tmp/`; selections overlap and must not be added.
All have zero errors/skips. Terminal exit is 0 except the two-failure first native
run, whose expected exception type was corrected as explained below.

| Receipt stem | Tests | Failures | Seconds | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| fp6-decimal-query-baseline | 137 | 0 | 10.963 | 68992fa6395ac03359734adcfd5ad1a984e05b1cb3dc8f7b7cbcc7b92ad5b879 |
| fp6-decimal-query-first | 60 | 2 | 10.671 | 900ff34db8d798c1d8cb43c69dbd41c8a129f6d94fa53c8d4a4f9f142293ecb6 |
| fp6-decimal-spill-first | 9 | 0 | 13.425 | f23973d3cf335e85388b6e8280dd233466274102dfb35dcd16112cc6d9fc44e9 |
| fp6-decimal-query-index-first | 78 | 0 | 20.310 | 2d8e94a0a675fbc37783741912043f48121bd923deb2d859fc3fda85d2c29fb2 |
| fp6-decimal-numeric-regression | 818 | 0 | 89.328 | 9051e133277736211adf0231fca5872335cf67f64c8152949453c0fe1d51c333 |
| fp6-decimal-final-numeric | 201 | 0 | 24.122 | 91177e93625b6d45d303351d402515c0633a84ae33ed54258f995c94dc8a8ecf |
| fp6-decimal-index-final | 37 | 0 | 15.878 | 68208bc4728704e0e7c584b54dff6becc9aeb037d9485f4fdd7cb71d2b9f7279 |
| fp6-decimal-numeric-final-contracts | 2547 | 0 | 82.819 | 0f429fecfdc804013c3e4a5a3e209f80b63b61a2a61bbdbcf5a83b92ad745d8f |

The first native run's two failures were fixture expectations for pure/NumPy:
inexact typed **assignment** correctly raises SchemaMismatchError, whereas the
test expected GrafxPlanError from arithmetic. The corrected tests assert the exact
`decimal_inexact` reason **and unchanged prior row values**. No storage refusal or
statement rollback was weakened. Later numeric/scalar failures continue using the
explicit query error taxonomy.

The 818 regression selection includes new decimal query/spill/index/storage tests,
ordinary spill and scalars, arithmetic/numeric parameter/NaN/IN contracts,
index-predicate and PK memo semantics, percentile/aggregate/ordering families,
optional aggregation, parser and planner. Subsequent bounded follow-ups exercise
the final DISTINCT scale policy, 76-digit temporary cancellation, expression-only
catalog preservation, composite keys and PK memo descriptor identity. These are
not a second full repository regression or a full original TCK profile rerun.

Final terminal exit **0**, **2,547 passes**, zero failures/errors/skips: complete
current decimal numeric/spill/index modules, PK memo/index-predicate regression,
public-surface/FP annotation/import-boundary contracts and executable documentation
(both native storage and numeric query examples). No production source changed
after this collection began. All prior failures are corrected and included in the
passing selection; no case is skipped or waived. All pytest handles are terminal.

Generated API reference, documentation links/anchors/configuration coverage and
preserved plans, Ruff across changed/new Python files and whitespace checks pass.
No new connection configuration is introduced. The existing pytest-asyncio
fixture-loop-scope deprecation warning is not a failure or skip.

## Remaining objective

FP-6 typed LIST/MAP/ARRAY/STRUCT and complete history/copy/transport/index
support/refusal matrix; checkpoint C with installed old/new readers; frozen and
supplemental profile, fixed competitors and paired installed Pulse API/UI remain
required. Decimal ordered/full-text indexing and percentile/other numeric function
families are not implied by equality indexes or SUM/AVG. No Pulse source/runtime,
global installation, production data, commit/push or release action accompanies
this checkpoint. See the [full contract](../specs/DECIMAL_VALUES_V1.md) and
[functional parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md).
