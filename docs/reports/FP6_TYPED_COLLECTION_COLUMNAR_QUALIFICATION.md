# FP-6 typed collection columnar qualification

Scope: the 0.0.6 development source on `feature/v0.0.6`, Windows/Python 3.13.
Optional dependencies actually exercised: PyArrow 19.0.1, Pandas 2.2.3,
Polars 1.44.2, from `.grafx-tmp/v006-optional-test-deps`.
This is local source/consumer evidence, not installed Pulse, cross-version
checkpoint C, full-profile acceptance or a release.

## Delivered contract

[The complete consumption guide](../COLLECTION_COLUMNAR.md) documents:

- Collection-root StoredType in every existing Arrow/Pandas/Polars/Parquet API;
  descriptor copies at admission and mandatory canonical full metadata.
- Typed LIST/MAP/ARRAY/STRUCT, nullable nested scalar/decimal/temporal values,
  empty structures, zero-length arrays and ANY with exact heterogeneous map keys.
- MAP remains an Arrow map. Polars entry-list representation is normalized only
  after exact shape/type checks; duplicate keys/NULL entries refuse, never merge.
- ARRAY uses a descriptor-bound portable variable list. Its fixed native length
  remains enforced; all-NULL arrays with declared length 2^32-1 allocate no slots.
- Empty STRUCT's explicit true sentinel preserves `{}` separately from NULL.
- ANY has one canonical native-value-v1 binary payload; strict full consumption,
  finite/native type validation and re-encoding reject ambiguity and duplicate keys.
- Bounded nested conversion and metadata charges, with ANY expansion admitted
  before decoding; no unbounded host JSON/datetime/decimal inference.
- Existing native executemany whole-call rollback, caller-owned commit/cursors,
  snapshots and independent writers remain in effect.

No row/catalog/WAL format, connection option, native lock/fence policy or package
version changes in this increment. Known collection structure stays columnar;
only dynamically typed ANY leaves carry opaque tagged binary values.

## Problems discovered and resolved

Direct library probes showed that nullable fixed-size lists cannot round-trip
through the tested Parquet reader, zero-sized fixed lists fail through Polars,
and Parquet refuses zero-field structs. The documented portable representation
preserves these native values rather than refusing them or converting NULL to empty.

The first Polars probe also exposed a Rust FFI panic on non-UTF-8 descriptor
metadata. Canonical GXT1 hexadecimal avoids this without dropping schema identity.
Metadata values are ASCII and the importer still requires an exact descriptor match.

A subsequent deep-value test found that `DataFrame.to_arrow` fails at 63 nested
list levels and `Series.to_arrow` at 64 because of Arrow's C Data schema recursion
limit. The final bridge reconstructs LIST/STRUCT buffers from shallow scalar leaves,
with owned masks/offsets and bounded filtering; it never converts wide timestamps
through Python datetime. Native depths 16, 63 and 64 now round-trip across all four
routes. Removing empty/NULL parents before explode avoids phantom placeholder rows;
real NULL elements of nonempty lists remain.

Three contract tests found missing postponed annotations, `__all__` and method
annotations in the new private helper module. Those declarations were added, and
the full contract selection passed on rerun. No checker or assertion was weakened.
The declaration-count admission bound was tightened to refuse >256 types before
copying descriptors; final feature tests prove refusal before producer iteration.

Polars 1.44.2 emits a deprecation warning about its future empty-list explode default.
This bridge filters empty and NULL parents before explode, so that future default
is not used for the data it passes. The known warning is not hidden or counted as a
failure. The existing pytest-asyncio fixture-loop-default warning also remains.

## Terminal evidence

Every listed process is terminal. Successful rows have exit 0 and zero errors/skips.
Selections overlap; their counts must not be summed as distinct coverage. Failed
intermediate receipts are retained and are not relabeled green.

| Receipt in `.grafx-tmp/` | Tests | Failures | Seconds | Scope |
| --- | ---: | ---: | ---: | --- |
| fp6-typed-columnar-first.xml | 102 | 0 | 48.119 | Initial feature/round-trip cases |
| fp6-typed-columnar-regression.xml | 327 | 0 | 114.124 | Initial grouped external-consumer regression |
| fp6-typed-columnar-depth.xml | 9 | 1 | 8.284 | Exposed Polars C Data schema recursion failure |
| fp6-typed-columnar-polars-depth-corrective.xml | 49 | 0 | 23.329 | Corrected deep/Polars cases |
| fp6-typed-columnar-contracts.xml | 2,483 | 3 | 65.631 | Exposed private-module declaration omissions |
| **fp6-typed-columnar-regression-final.xml** | **660** | **0** | **212.029** | Grouped columnar + prior native/text/history/copy/collection regressions |
| **fp6-typed-columnar-contracts-final.xml** | **2,483** | **0** | **64.542** | Full selected API/annotation/import/documentation/executemany contracts |
| **fp6-typed-columnar-features-final.xml** | **161** | **0** | **67.141** | Feature rerun after final declaration/admission cleanup, including three added early-refusal cases |

Tests include 4 real routes × 2 codecs × 8 representative collection shapes with
all scalar families, committed restart and verification. Other cases cover exact
physical oracles, sliced/lazy ownership, descriptor alteration, metadata removal,
wrong numeric width/scale, invalid shape/nullability, duplicate map keys, malformed
ANY payloads, late atomic refusal, frame/file/batch budgets, no partial Parquet
publication, empty/all-NULL inputs without unintended capability activation,
64-deep lists, huge all-NULL ARRAY declarations, pre-decode expansion limits and
an export cursor retaining its snapshot across an independent writer's commit.

```text
fp6-typed-columnar-first.xml d50ac72ff12286164ff0b5ff8f034a5f82a0aca07ae5ee7325d525dae1dca067
fp6-typed-columnar-regression.xml f0d275f80b56f422ee78e192719f8c5a5a683cf84637037ee057ef4bbc49feca
fp6-typed-columnar-depth.xml 34d2516618c18059622e0dd4e197fc260feb87bc6a2ae3fecbefbf72eeb24f53
fp6-typed-columnar-polars-depth-corrective.xml 47f4f4e4da6be2edf74b7395bd046e1a38564eba2c73ac5a5c09c2d5ce5748ee
fp6-typed-columnar-contracts.xml de7b2cdf9e6c1523fc40a9a194639032b6cfd45cf810ea42eaf848e006e10737
fp6-typed-columnar-regression-final.xml d899f0d4a1ecb0473fe0403358ef7d92401daab258102124fa703c5b11b883a0
fp6-typed-columnar-contracts-final.xml 61466799da356cadcca7c4f22d68ce1e28e0d896654f02e99193493d166fef71
fp6-typed-columnar-features-final.xml 9291c0dba2b7a028c7cf37855cb8b346493e0e554951e57c6b2364b0fb068519
```

```powershell
$env:PYTHONPATH = 'src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/api/test_typed_collection_tabular.py tests/api/test_decimal_tabular.py `
  tests/api/test_temporal_tabular.py tests/api/test_tabular_interop.py `
  tests/api/test_tabular_optional.py tests/api/test_parquet_interop.py `
  tests/api/test_arrow_missing.py tests/api/test_projection_polars_export.py `
  tests/api/test_typed_collection_storage.py tests/api/test_typed_collection_consumers.py `
  tests/api/test_typed_collection_text_import.py tests/api/test_collection_json.py `
  tests/query/test_typed_collection_queries.py tests/storage_core/test_stored_types.py `
  tests/storage_core/test_typed_collection_schema.py -q --tb=short `
  --junitxml=.grafx-tmp/fp6-typed-columnar-regression-final.xml
python -m pytest tests/foundation/test_public_surface.py `
  tests/foundation/test_fp_annotation_contracts.py tests/test_import_boundary.py `
  tests/consumer/test_documentation.py tests/api/test_executemany.py -q --tb=short `
  --junitxml=.grafx-tmp/fp6-typed-columnar-contracts-final.xml
python -m pytest tests/api/test_typed_collection_tabular.py -q --tb=short `
  --junitxml=.grafx-tmp/fp6-typed-columnar-features-final.xml
```

The current feature command includes the three added admission tests; running the
current grouped selection therefore collects three more tests than its retained
660-test receipt. This is not hidden test removal or a replacement of evidence.
The source-level contract cleanup and declaration-count change are qualified by
the final contract/feature reruns; the earlier grouped result is preserved as run.

Source SHA-256 at final feature qualification (under `src/okto_grafx/`):

```text
_arrow_values.py 93aa45759c1f81aed380b27d5f19c158d0590b190195f8e3bf124a474991383b
arrow.py 379784aa5c6cfd7f41e4f29d686242da7e5989ab4c399525f382eb4253c5da06
tabular.py 3ce146ba1c7c32623ab63ab377c2aaf75a7707a9f7935acd79c2521375578c5e
polars.py acb1c30661f31367a622c2a548472f10498d1762f2dc41ecf76b379a61253621
parquet.py 5a52e78475818b1de2d62b16a410633cd920db38f2d1d8ee122b06d0f1c4df28
tests/api/test_typed_collection_tabular.py 1595667b37ba9163acc7211d0522e73979333103975651bd335629d8ec5d1ea3
```

Generated API reference, executable collection recipe, documentation links/anchors,
39 connection-field inventory, 11 retained source plans, changed-Python Ruff and
whitespace checks pass. README, roadmap, native collection spec, parity plan,
configuration, columnar guides and native feature comparison are updated.

## Remaining original-plan work

The consolidated full type-support matrix, combined installed old/new-reader
checkpoint C, profile policy decisions, full repository/TCK/supplemental/competitor
qualification and final installed paired Pulse/API/browser/MCP acceptance remain
open. No production graph mutation, global install, commit/push or publication was
performed. This increment closes parameterized collection columnar consumption,
not the complete functional-parity plan.
