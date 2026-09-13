# FP-6 exact collection JSON and local import qualification

[Usage and complete grammar](../COLLECTION_JSON.md) ·
[Storage contract](../specs/TYPED_COLLECTIONS_V1.md) · [Roadmap](../../ROADMAP.md)

Local Python 3.13/Windows qualification of current `feature/v0.0.6` source.
This extends the preceding native collection checkpoint, not the installed
Pulse, a published release, the full frozen profile or the entire Grafx suite.

## Implemented scope

Public `collection_json_value` / `collection_from_json_value` use a collection-root
StoredType and exact native JSON representations. LIST/MAP/ARRAY/STRUCT preserve
their declarations and nested nullability. INT64/TIMESTAMP are canonical decimal
strings; BYTES uses Base64, UUID canonical text, and decimal/temporal leaves retain
their existing exact tags. ANY tags non-null values explicitly and uses entry
pairs for maps, preserving non-string native keys without confusing ordinary user
maps with scalar tags. Unknown tags, duplicate native keys, nonfinite values,
embeddings, host scalar subclasses and lossy/missing metadata refuse.

CSV, JSONL and SQLite declarations accept `tuple[str | StoredType, ...]` and own
their admitted descriptors. CSV carries JSON text, JSONL structured values or
encoded JSON text, and SQLite JSON TEXT or SQL NULL. No source inference, external
query engine or automatic target schema is added. Reader processing applies
existing field/batch/row/file/work budgets and charges nested native encoded bytes
against max_work. Root nullability is enforced, including SQL NULL/CSV null tokens.

Transport validation does not rescale decimals or repair missing STRUCT fields.
Later target assignment remains separate and may exactly rescale to the target
column's declared p/s. Any late malformed input or target error rolls back the
whole import call and preserves prior caller staging. SQLite finishes its detached
source read before staging. Capability publication follows actual native storage,
not a transport declaration alone; an ANY target does not acquire the typed-column
capability merely because the reader used StoredType.

## Qualification and bounded-allocation review

Independent JSON oracles cover all native scalar families inside every collection
kind; native wide integers, decimal p/s, temporal coordinates, user-map tag
collisions, heterogeneous map keys, NULL/empty values, canonical tags/base64,
descriptor/value cycles, depth, malformed shapes, UTF-8 bounds and ownership are
tested. Reader/import tests cover all three formats and both codecs, held-reader
snapshots, commit/checkpoint/reopen, target exact rescaling, lazy descriptor
ownership, field/work limits and late failure without surviving import rows or
native capability activation. SQLite rejects BLOB/INTEGER/REAL collection inference
and releases its source connection.

Review found that checking the size of each binary leaf alone was insufficient:
many individually legal leaves could be Base64-allocated before the final cell
bound. The encoder now cumulatively charges encoded binary lengths **before** each
allocation. The regression observes the encoder and proves the next conversion is
not called after the budget is exhausted. This is a bounded transport correction,
not an altered graph quota or claimed process RSS ceiling.

## Terminal receipts

All receipts are under `.grafx-tmp/`, with zero failures/errors/skips and exit 0.
Selections overlap and their counts must not be summed as distinct test coverage.

| Receipt | Passed | Seconds | Scope |
| --- | ---: | ---: | --- |
| fp6-typed-json-first.xml | 86 | 0.500 | Initial independent helper/oracle tests |
| fp6-typed-text-features.xml | 246 | 62.617 | New JSON/local collection features plus prior decimal/temporal imports |
| **fp6-typed-text-regression.xml** | **506** | **99.059** | Final helper/import/native collection regression, including allocation correction |
| **fp6-typed-text-contracts.xml** | **2,474** | **57.237** | Public surface, runtime annotations, import boundaries, executable docs and executemany |

```text
fp6-typed-json-first.xml       2e0463b4d40c6c09c291eeb119c23c88ce31415649b9ab98bf34827e8d6574a9
fp6-typed-text-features.xml    8ab5db1b9ec5a1e182c805fb22dd17b69a597ea981d33755edfaa323fe2b69b6
fp6-typed-text-regression.xml  30f5f930d0780d76ccb4892f214d50b38b9681aab920e2bfea205115238b2491
fp6-typed-text-contracts.xml   20a7616cc45af9ccd5696f32c8c120053cf629a4f237b8365c9b564a20ac30ee
```

```powershell
$env:PYTHONPATH = 'src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/api/test_collection_json.py tests/api/test_typed_collection_text_import.py `
  tests/api/test_local_text_import.py tests/api/test_sqlite_import.py `
  tests/api/test_decimal_text_import.py tests/api/test_temporal_text_import.py `
  tests/api/test_typed_collection_storage.py tests/api/test_typed_collection_consumers.py `
  tests/query/test_typed_collection_queries.py tests/storage_core/test_stored_types.py `
  tests/storage_core/test_typed_collection_schema.py tests/storage_core/test_decimal_interchange.py `
  -q --tb=short --junitxml=.grafx-tmp/fp6-typed-text-regression.xml
python -m pytest tests/foundation/test_public_surface.py `
  tests/foundation/test_fp_annotation_contracts.py tests/test_import_boundary.py `
  tests/consumer/test_documentation.py tests/api/test_executemany.py -q --tb=short `
  --junitxml=.grafx-tmp/fp6-typed-text-contracts.xml
```

API reference regeneration/checking, documentation links/anchors (39 connection
fields, 11 retained source plans), changed-source Ruff and whitespace checking pass.
The new JSON import recipe is executed against an isolated directory/database.

Actual working-file SHA-256, under `src/okto_grafx/`:

```text
collection_json.py  6a2b60165a27e0e87f002ded8c9a311a34e4ca52c39f8ac8ab0f122015cbc02b
text_import.py      324154a30235021628ca39cedb6ea5bb2cdd35ffe63651b167e39bfdf54783c8
sqlite_import.py    1b0f42f701aa062e8749ab008658b3db398a1ecc56761d3c4648be99be0b8379
```

Test source hashes:

```text
tests/api/test_collection_json.py               ac83962d616954a6b9a34ab1bd2568d244cd34114cb9d73dd1da93dd7578fbad
tests/api/test_typed_collection_text_import.py  1daf4e45783ef1fc868e9b6163f0a0ccc3dcdc1fa6af8f6a9157925ff9eb5515
```

## Remaining original-plan work

Parameterized collection Arrow/Pandas/Polars/Parquet, the consolidated complete
type-support matrix, combined installed old/new-reader checkpoint C, frozen-profile
policy decisions and full repository/TCK/supplemental/competitor/Pulse acceptance
remain open. No native row/catalog/WAL format changed in this transport increment.
CLI JSON remains a bounded observation, not a universal lossless input protocol;
the new helpers and logical transfer provide explicit exact movement routes.
No global install, production data mutation, commit/push or release was performed.
