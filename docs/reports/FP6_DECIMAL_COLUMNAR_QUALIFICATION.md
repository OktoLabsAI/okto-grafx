# FP-6 exact DECIMAL columnar qualification

Scope: the current `feature/v0.0.6` worktree, September 12, 2026. This continues
the [CLI/local-import/procedure increment](FP6_DECIMAL_INTERFACE_QUALIFICATION.md),
not a release or final functional-parity claim. No installed Pulse or production
data was changed. All test databases/files are synthetic temporary artifacts.

## Implemented contract

- `okto_grafx.arrow.ArrowDecimalType(precision, scale)` declares p=1..38, s=0..p
  with exact integer validation. Optional imports remain lazy.
- `arrow.py` exports native DecimalValue to decimal128, with exact matching p/s
  and `grafx.type=DECIMAL`/`grafx.decimal=decimal128-v1` metadata. It imports native
  values using validated finite digit tuples, not float or host-context arithmetic.
  A forged coefficient outside declared precision refuses before that row stages.
- `tabular.py`, `polars.py` and `parquet.py` share the descriptor/schema/atomic
  native import. Pandas requires ArrowDtype and metadata attrs; Polars requires
  exact actual Decimal dtype and matching wrapper schema; Parquet retains the
  physical decimal declaration/tags. No precision or scale inference/coercion.
- Native destination assignment is separate: exact rescaling to its declared
  column is allowed, inexact/overflow refuses the entire import call. ANY retains
  the offered p/s. NULL/empty inputs do not spuriously activate storage capability.
- Export/import/frame tariffs reserve an additional 1,024 logical bytes per
  decimal cell. Arrow import uses the structured 16x physical-byte multiplier.
  Whole-call savepoints, caller-owned transactions/cursors and all existing
  row/batch/frame/file/native transaction limits remain intact.
- No storage format, capability bit, connection configuration, transaction or
  lock protocol changed. Public guide: [exact decimals](../EXTENSIONS_AND_ARROW.md#exact-native-decimals-006-development).

## Verification scope

`tests/api/test_decimal_tabular.py` covers all four routes, typed and ANY targets,
pure/NumPy commit/verification/checkpoint/reopen, coefficients at the 38-digit
limits, all 39 supported scales, negative/zero/NULL, sliced Arrow offsets and
context precision=1 with strict Inexact/Rounded/FloatOperation traps.

Adversarial cases include descriptor/host type mismatch, forged native values,
unknown/missing metadata, wrong physical p/s, decimal256/dictionary/float refusal,
raw Arrow coefficients beyond declared precision, Pandas actual dtype versus
attrs, Polars actual dtype versus wrapper and externally constructed bad Parquet.
Late target assignment failures, row/batch/byte limits and late export failures
preserve prior staging or prevent final Parquet publication. File rename after
failed imports checks that Windows source handles are closed. Tariff boundary
tests exercise one byte below and exactly at the declared decimal charge.

An independent writer commits during cursor consumption; the old reader retains
its snapshot and emitted batches survive database close. For every route and
both codecs, injected post-durable-COMMIT apply failure exposes committed/durable
outcome and two reopens recover the exact rows once. This complements, but does
not replace, prior real-process native storage/history recovery tests.

Runtime dependencies inspected locally: PyArrow **19.0.1**, Pandas **2.2.3**, Polars
**1.44.2** on Python 3.13/Windows. This is not an all-version/platform claim.
The first test selection had one **fixture construction** failure: `pa.array`
does not build the requested decimal dictionary directly. The fixture now uses
the public DictionaryArray constructor, reaching the intended native refusal.
No production error or acceptance expectation was waived.

## Receipts

JUnit XML in `.grafx-tmp/`; selections overlap and counts must not be added.
All rows have zero errors/skips. Terminal exit is 1 for the initial failed fixture
run and 0 for the grouped regression.

| Receipt stem | Tests | Failures | Seconds | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| fp6-decimal-columnar-features-first | 119 | 1 | 23.671 | 7a0afdce436204253b789de86083b3d54a6c8fc31c102b063dc93ed265f60478 |
| fp6-decimal-columnar-regression | 347 | 0 | 78.111 | 3fead7e62ed8ba7dcf58252a605e034985c741d5cbf9f8e71fb1e674737e8573 |
| fp6-decimal-columnar-contracts | 2474 | 0 | 52.098 | 035ab74a94a09ab171b346bc5bcd3dc97e1dd7df88a8aaae85d4d8c12e3e907d |

The grouped run includes every API test file matching arrow/tabular/parquet/polars,
plus decimal JSON transport, local text imports, native procedures and CLI JSON.
The final contract run exits 0 and covers package public surfaces, parity runtime
annotations, all import boundaries, executable documentation (including the new
decimal128 recipe), generated API/links/configuration coverage and decimal JSON
transport. The earlier pure-core regex violation remains fixed; no allowlist or
test was weakened. All changed Python passes Ruff; documentation generation/check
and `git diff --check` pass. Functional source stayed unchanged during these runs.

## Remaining whole objective

Typed LIST/MAP/ARRAY/STRUCT persistence and its consumer matrix remain FP-6 work.
Combined temporal/decimal/collection installed-reader checkpoint C, the complete
frozen/supplemental/competitor profile and final installed paired Pulse regression
including API/browser/MCP remain required. The label-mutation and Set1 policy
decisions remain unresolved, unchanged by this increment. This is not complete
Cypher conformance or full-repository regression. See the
[unreduced plan](../specs/FUNCTIONAL_PARITY_PLAN.md).
