# FP-6 exact DECIMAL CLI, local-import and procedure interfaces

Development `feature/v0.0.6`; isolated synthetic data, no installed Pulse change,
production data mutation, commit/push, package release or full-parity assertion.
This follows [storage/query](FP6_DECIMAL_QUERY_QUALIFICATION.md) and
[history/copy/transfer](FP6_DECIMAL_CONSUMER_QUALIFICATION.md) qualification.

## Implemented scope

- One native JSON grammar validates the same type/coefficient/p/s object for
  scalar CLI output, entity observations and explicitly typed local imports.
  Coefficients are canonical bounded strings, not lossy JSON numbers or repr.
  CLI schema inventory now includes decimal_precision/decimal_scale on decimal
  columns only. Expression-only output does not activate stored type capability.
- CSV/JSONL/SQLite declare the DECIMAL input family and decode canonical tagged
  objects or JSON text. Existing field/work/row bounds and duplicate/nonfinite
  rejection remain. Readers retain native metadata; target typed assignments
  rescale exactly or refuse. A bad late row preserves prior caller statements
  and leaves no partial imported rows/native capability. SQLite closes its
  read snapshot before staging and on invalid input.
- Procedure DECIMAL signatures preserve owned native values; NUMBER now includes
  INT64/finite DOUBLE/DECIMAL without converting the offered kind. DOUBLE does not
  implicitly cast DECIMAL. ANY/LIST/MAP recurse through the same native ownership
  boundary. Each decimal occurrence charges its 19-byte frame before copying.
  Invalid/forged values and even unselected bad output columns must refuse.
- ProcedureReader.query/ProcedureWriter.query can carry native decimals under
  existing snapshot, permission, budget and outer-statement rollback contracts.
  Typed/ANY writes reuse native capability/COMMIT/WAL publication. ScalarFunction
  retains its earlier explicit type set; this does not silently expand that API.

Source: `domain/model/decimal_interchange.py`, `domain/query/entity_values.py`,
`domain/query/procedure_values.py`, `domain/query/extensions.py`, `cli/output.py`,
`cli/commands.py`, `text_import.py` and `sqlite_import.py` under `src/okto_grafx`.
No connection option, native storage tag, capability, rounding default or new
transaction/retry mechanism is introduced by these interfaces.

## Tests and corrected failure

`test_decimal_interchange.py` checks extreme coefficients/scales, host decimal
context independence, malformed coordinates/spellings/host types and forged native
values. `test_decimal_json.py` exercises actual CLI query/schema commands, nested
and entity output, exact tags and expression-only storage preservation.
`test_decimal_text_import.py` covers all three local formats, pure/NumPy,
typed/ANY destinations, NULL, valid exact rescaling, invalid last rows, field bounds,
inexact destination scale and SQLite snapshot release.
`test_procedure_decimal_values.py` covers direct/native invocation, static/bound
types, NULL, NUMBER/DISTINCT/arithmetic composition, cursor output, per-occurrence
ownership, 18/19-byte limits, unselected bad output, reader/writer snapshots,
typed/ANY writes and proven-durable apply-failure recovery on both codecs.

Initial baseline: one test called `required_capabilities()` on the intentionally
restricted public CatalogView. The assertion was corrected to use the internal
catalog capability probe; the public view API was not widened. That test is
included in the grouped regression. No native-data failure was waived.

## Local receipts

JUnit XML under `.grafx-tmp/`; overlapping selections must not be added. All
listed runs have zero errors/skips; the initial fixture failure remains visible.

| Receipt stem | Tests | Failures | Seconds | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| fp6-decimal-interface-baseline | 160 | 1 | 13.367 | b5ccc31dafcb67eae7b73841e8d86756bb80ecaa4e23df9257a73fbfd9125c6e |
| fp6-decimal-interface-features-first | 75 | 0 | 19.547 | ddc3560dc8deafa6d1696a300f79d4d545a4a19eca630b88474350f08908b3b9 |
| fp6-decimal-interface-regression | 1240 | 0 | 221.241 | 6ea9f2f1c34e28dd64a45e225ccaa04ca2c5e88fc0cf35f87dc1bca3717c4c16 |
| fp6-decimal-interface-contracts | 2473 | 2 | 47.571 | a105f70a3ef7f96f643b80af446dbcb88508d5ef8817cf9c4f57ebe5a5498ffb |
| fp6-decimal-interface-boundary-corrective | 342 | 0 | 12.448 | 782bd70af0603f2655d92dfa0d0792d4963c82537d87b7c6a6dd3109a3c36c65 |

The grouped regression covers procedure/scalar/local-import API suites, the CLI
suite, decimal transport/numeric/entity helpers and procedure error translation.
The contract run found **two failures with one cause**: `decimal_interchange`
imported unrestricted `re` in the pure model. The implementation now validates the
same bounded canonical ASCII grammar directly; no architecture rule was relaxed.
The 342-test corrective run covers all import boundaries and decimal transport
fixtures and exits 0. The following [columnar qualification](FP6_DECIMAL_COLUMNAR_QUALIFICATION.md)
records the final combined feature/public-contract verification after this correction.
This is not whole-repository, full-profile or installed-Pulse qualification.

## Remaining full objective

Arrow/Pandas/Polars/Parquet decimal conversion, metadata, limits and atomic import
are now implemented in the [following increment](FP6_DECIMAL_COLUMNAR_QUALIFICATION.md).
Typed LIST/MAP/ARRAY/STRUCT, checkpoint C
with installed old/new readers and the complete frozen/supplemental/competitor
profile and paired installed Pulse API/browser qualification remain mandatory.
The unresolved label-policy and Set1 storage-oracle decisions are not changed by
these interfaces. See the [support matrix](../specs/DECIMAL_VALUES_V1.md) and
[full parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md). No release is implied.
