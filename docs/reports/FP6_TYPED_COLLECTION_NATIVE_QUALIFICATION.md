# FP-6 native typed collection qualification

[Contract and remaining scope](../specs/TYPED_COLLECTIONS_V1.md) ·
[Functional parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md) · [Roadmap](../../ROADMAP.md)

Development source on `feature/v0.0.6`, based on HEAD
`c7aaf3809544745e8bf19620706b875729c859e6` plus the existing uncommitted parity work.
These are local Python 3.13/Windows results, not an installed-reader checkpoint,
full Grafx/TCK/Pulse acceptance, commit, publication or production deployment.

## Native capability delivered

- Public StoredType and owned ColumnDef metadata; DDL LIST/MAP/ARRAY/STRUCT with
  nested nullability, exact DECIMAL/temporal/ANY leaves and empty STRUCT/ARRAY.
- Catalog-v2 typed_collections_v1 bit 27, column marker 252 and bounded GXT1 frame.
  Nested declared capabilities accompany schema publication, even with no rows.
- Canonical assignment before intent/quota/encoding proofs; strict stored reads
  reject missing STRUCT fields, wrong array lengths, wrong native families and
  noncanonical decimal p/s, including projected-away columns.
- Rollback of late invalid rows and uncommitted DDL/data/capabilities; independent
  reader snapshots and original OCC conflicting-writer rejection on native stores.
- Real process loss before COMMIT and after proven COMMIT/before apply, pure and
  NumPy: schema/data appear together only for the proven durable outcome, with
  repeated reopen and verification.
- Native parameters/results, equality, UNION, UNWIND, nested access, mutable-input
  ownership and scalar indexes on other properties of collection-bearing tables.
- CLI schema descriptor JSON and tagged nested values; exact target-schema copy,
  idempotent receipts, nullable append, normal/resumable logical transfer, retained
  history via scan/index, delete/recreate and physical backup/restore.
- Import metadata/value mismatches refuse before opening destination/workspace,
  including checksummed data whose changed declaration would otherwise permit
  normalization. Copy refuses different descriptors even for empty packages.

Collection PK/custom index definitions refuse explicitly. No arbitrary element
indexes, new row tags, weaker durability, lock bypass, cross-store transaction or
global-authority cache is introduced. Existing native query list/map semantics
remain distinct from stronger schema assignment constraints.

## Findings and corrections

The first collection run did not execute native tests: it imported a nonexistent
private tuple-projection helper. It now uses the actual projection decoder.
The next 140-case run had eight failures. One native parser bug was corrected:
`STRUCT<>` must interpret the lexer's existing `<>` comparison token as an empty
type field list only in this grammar production. Expression `<>` is unchanged.

The other failures were test setup/contract errors: Catalog uses `copy()`, not
`clone()`; a copy source needs identity indexes and tracked commit provenance
before producing rows; crash workers must not depend on an unconfigured tests
package import. Frozen catalog observations may be memoized, so forcibly mutating
one does not promise a fresh observation on the next property access. The test
now verifies native schema and writes/reopen remain unaffected. Likewise, a result
memoizes its own plan; independent results/plans and engine authority must remain
separate. Tests now check that real contract, including recursively nested fields.

The first grouped regression found an older dispatch-inventory test that treated
DECIMAL as a preexisting isinstance-dispatched class. Exact native DTO admission
intentionally differs from builtin/subclass dispatch, as temporal types already
did. The test separates those groups and adds explicit native decimal acceptance,
correct stored tag and host-subclass refusal; no production dispatch was weakened.

## Terminal receipts

All paths are under `.grafx-tmp/`; counts overlap across attempts and must not be
added as independent coverage. XML hashes below are SHA-256.

| Receipt | Outcome | Seconds | Exit |
| --- | --- | ---: | ---: |
| fp6-typed-native-features-first.xml | One collection error; no executed pass | 1.064 | 1 |
| fp6-typed-native-features-second.xml | 132 passed, eight failed | 23.013 | 1 |
| fp6-typed-native-features-corrective.xml | 138 passed, two failed | 25.069 | 1 |
| fp6-typed-native-consumers.xml | 159 passed, one failed | 48.671 | 1 |
| fp6-typed-descriptor-ownership.xml | One passed | 1.021 | 0 |
| fp6-typed-native-grouped.xml | 3,042 passed, one failed | 124.457 | 1 |
| **fp6-typed-native-grouped-final.xml** | **3,048 passed; zero failures/errors/skips** | **118.744** | **0** |
| **fp6-typed-native-contracts.xml** | **2,447 passed; zero failures/errors/skips** | **56.081** | **0** |

```text
fp6-typed-native-features-first.xml       33cadc15c7e93d01b11db6967c698b17e5161d1781de72433a295f3ac745e064
fp6-typed-native-features-second.xml      d1f6eb9d391b2db147c9c1f05ad3ed19ce31febf310ceeaab031b47c14ad09da
fp6-typed-native-features-corrective.xml  8b7b6e064127594ac2a26550993d1b84f85a039908c3add701659ac3a3fddddd
fp6-typed-native-consumers.xml            f65f95c5e1d3696c3ee8509cc88fdefc35779a7f270b2150eea8a9b9051e2562
fp6-typed-descriptor-ownership.xml        a0c07c852fbcb19862d79164502fc0c60ceaf64f94e86c274d40fd881fe41d09
fp6-typed-native-grouped.xml              ee4cc5849706543cf52a85d1b11f07e43d4a5916227731c055c99378677fba64
fp6-typed-native-grouped-final.xml        2bfea8192182528c4a064eadd7a888309473447f7572b1f0bab8652656997d1c
fp6-typed-native-contracts.xml            dacefe532e955aae95f980085c4438d33e90f53dead82eb84fe0f4e46e37b372
```

The final grouped selection includes every `tests/storage_core` test, the new
native/consumer/query/CLI collection tests, nullable columns, public query/plan
ownership, catalog-view memo, parser/lexer/planner, schema transactionality/refusal,
DDL catalog copying, prepared plans and table-local index authority. The contracts
selection covers public surface, FP annotations, import boundaries and executable
documentation, including the new collection recipe.

```powershell
$env:PYTHONPATH = 'src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/storage_core tests/api/test_typed_collection_storage.py `
  tests/api/test_typed_collection_consumers.py tests/cli/test_typed_collection_json.py `
  tests/api/test_nullable_columns.py tests/api/test_catalog_view_memo.py `
  tests/api/test_query_result_plan_door.py tests/api/test_public_query_boundaries.py `
  tests/query/test_parser.py tests/query/test_lexer.py tests/query/test_planner.py `
  tests/query/test_schema_transactionality.py tests/query/test_schema_refusal_atomicity.py `
  tests/query/test_ddl_catalog_copy.py tests/query/test_prepared_plan_cache.py `
  tests/query/test_typed_collection_queries.py tests/index/test_table_local_catalog_authority.py `
  -q --tb=short --junitxml=.grafx-tmp/fp6-typed-native-grouped-final.xml
python -m pytest tests/foundation/test_public_surface.py `
  tests/foundation/test_fp_annotation_contracts.py tests/test_import_boundary.py `
  tests/consumer/test_documentation.py -q --tb=short `
  --junitxml=.grafx-tmp/fp6-typed-native-contracts.xml
```

Generated API reference, documentation links/anchors (39 configuration fields,
11 retained source plans), changed-Python Ruff and `git diff --check` pass.

## Source fingerprints

Hashes are for actual working files, not the branch HEAD alone.

```text
domain/model/stored_types.py  87c0dd179a84d06aa328a5183ec910b1a406e21bf1bddd67cf85aba19ff25b26
domain/model/schema.py        a3f5d8b9e05e10137bc41010def50d1db45e95995a1885cbbf20927f7228f4b4
domain/model/catalog.py       eb58b813e2fab3718a831049fb313e8fd2b374db3203131d400220cb3a4a4a3e
domain/query/parser.py        01e341fc113f2a7cccd156bfebed141836366d187e2f9fefa0ca20e6a60a8dd9
domain/query/ast.py           32b5a2dc9a7faad38e31f74dfc65b4738c14f05678c5727bbb3a1d317f754134
domain/query/planner.py       9f5a9894c6623ae563f65b3658d98b96f50d5646bb6f7bf48e753632e812f944
domain/txn/context.py        dd939ecfa8b5c2c7ba8dd109aaac3c05a157153972dde52a10e4477219899cb0
engine/public_views.py       3d1ac63f4b4cd1c24cf8ba85d01f5c7a3a5cbd1dc554ab5a43c5de6508d3b8eb
transfer.py                 4d3e46dde9f01cd8058c95098d40294627edd57ba2e742c5a48c80e5ba49377d
catalog_copy.py             7667077b2f8b306dcbac2ca3e3494766fc5d5a016e56dcb17c03c9db2b2d9d0a
cli/commands.py             85577e4f3bafaa80d7b27541ac254c5e8e33dd4239ec915f086fc00c54368c6c
__init__.py                 6ea72ef6f1ae1f092e3da0ad890a0dedf3c5cc1777834393dd25d19aa0e427ba
```

## Work not closed by these receipts

Parameterized collection Arrow/Pandas/Polars/Parquet and local text/SQLite
interchange, complete procedure/parameter/result support matrix, installed
old/new-reader temporal/DECIMAL/collection checkpoint C, the pending frozen-profile
policy decisions, final repository/TCK/supplemental/competitor qualification and
paired installed Pulse/API/browser/MCP acceptance remain required by the original
plan. No new exclusion, relaxed failure expectation, release or global install is
authorized by this report.
