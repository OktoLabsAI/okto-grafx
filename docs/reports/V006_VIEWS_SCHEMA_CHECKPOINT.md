# 0.0.6 logical views and nullable-schema checkpoint

Date: September 10, 2026. Branch `feature/v0.0.6`.
Base `4ee4d2e2f9ad5295efbe2faac2f1fbdd2a1867da`, followed by local working-tree
implementation. This is not a release, installed Pulse build or new published SHA.

## Scope and remaining work

This checkpoint adds continuation items 5–6 to the previously implemented bounded
[catalog/workspace](V006_CATALOG_WORKSPACE_ACCEPTANCE.md) and
[copy/phrase](V006_COPY_PHRASE_ACCEPTANCE.md) slices:

- [Logical views](../LOGICAL_VIEWS.md): persisted typed base-table read queries,
  checksummed definitions, explicit dependencies, read snapshots, bounded
  introspection, native atomic create/replace/drop and explicit preparation.
  Nested views, CALL/DDL/update bodies and materialized views are not supported.
- [Nullable columns](../NULLABLE_COLUMNS.md): one-column typed append, exact
  historical decode widths, required bit 13, no heap rewrite, stale-writer
  refusal, catalog epoch/cursor handling and current-state logical transfer.
  Other ALTER operations, defaults and temporal schema queries are not delivered.

**The complete eight-item request remains unfinished.** Item 7 has an
[operation-count assessment](V006_REPEATED_KEY_ASSESSMENT.md), not a new posting
layout. Item 8's durable opt-in temporal history remains unimplemented. Item 4
verifies exact analyzed phrases in candidates; it does not persist positions or
return position lists. No full CAP-2/3/5/10 closure is inferred from bounded slices.

## Regression evidence

The expanded run covers all `tests/query`, `tests/storage_core`, `tests/txn`,
`tests/index`, `tests/recovery`, `tests/wal`, plus the selected public API suites
listed below: **7,535 passed in 994.36 s (16 min 34 s)**. Log:
`.grafx-tmp/v006-capability-continuation-regression.log`.

```powershell
python -m pytest tests/query tests/storage_core tests/txn tests/index tests/recovery tests/wal tests/api/test_nullable_columns.py tests/api/test_logical_views.py tests/api/test_repeated_key_evidence.py tests/api/test_catalogs.py tests/api/test_workspace.py tests/api/test_catalog_copy.py tests/api/test_fulltext_phrase.py tests/api/test_fulltext.py tests/api/test_fulltext_prefix.py tests/api/test_fulltext_relationships.py tests/api/test_fulltext_durable.py tests/api/test_fulltext_history.py tests/api/test_fulltext_incremental.py tests/api/test_hybrid_search.py tests/api/test_executemany.py tests/api/test_connect_lifecycle.py tests/api/test_connect_options.py tests/api/test_identity_and_errors.py tests/api/test_read_only_and_doors.py tests/api/test_read_only_storage_wiring.py tests/api/test_public_boundary_concurrency.py tests/api/test_public_query_boundaries.py tests/api/test_public_operation_outputs.py tests/api/test_commit_history.py tests/api/test_commit_history_concurrency.py tests/api/test_transaction_descriptor_reuse.py tests/api/test_logical_transfer.py tests/api/test_physical_backup.py tests/api/test_v2_schema_growth.py tests/api/test_schema_migrations.py tests/api/test_hot_key_pages.py tests/api/test_large_hash_distribution.py tests/test_import_boundary.py tests/test_optional_package_boundary.py -o addopts= -q --tb=short --durations=10
```

A late targeted review found that compound LIST/MAP landing-cache admission had
to include the virtual appended NULL bytes, not only the physical payload length.
The regression test demonstrated the missing charge before correction. The fix
changes optional retention accounting, not row values, visibility or WAL effects;
unknown layout information declines optional caching. The final supplemental run
includes this correction and additional copy/layout-bound/old-reader tests:
**155 passed in 144.57 s (2 min 24 s)**. Log:
`.grafx-tmp/v006-schema-final-supplement.log`.

```powershell
python -m pytest tests/api/test_nullable_columns.py tests/api/test_logical_views.py tests/query/test_lazy_owner_landing.py tests/query/test_st6_owner_landing_memo.py tests/query/test_read_your_own_writes.py tests/api/test_catalog_copy.py tests/api/test_logical_transfer.py tests/api/test_physical_backup.py -o addopts= -q --tb=short
```

Do not add overlapping run counts or represent the expanded run as having included
a correction made after it started. Earlier checkpoints: 24 view tests, 267 grouped
view/schema/public-boundary tests, 159 schema/catalog tests and the prior 916-test
copy/phrase regression. Those are historical intermediate results, not new totals.

## Storage and concurrency evidence

- Real subprocess deaths for view replacement before COMMIT, after durable COMMIT
  and before page application. Native reopen/checkpoint/reopen verifies old-or-new
  definition, never a partial replacement.
- Real subprocess deaths for nullable-column DDL before COMMIT, after COMMIT,
  before applying pages and after the first page application. Recovery verifies
  matching schema/capability and old rows; repeated recovery is stable.
- Unsupported-reader capability-mask simulation refuses pending schema WAL before
  changing the physical catalog. This is not an actual historical-wheel/POSIX run.
- Concurrent view creation has one winner; pinned readers preserve prior definitions.
  Two staged schema additions cannot silently overwrite one another.
- Old-schema row writers refuse before heap materialization/WAL. Failed schema
  staging rolls back; heap insert/update/delete hooks prove append does not rewrite rows.
- Existing/new exact indexes, relationships, FTS, physical backup, fresh logical
  import and existing-target receipt copy are exercised after column addition.
- Unknown/future heap versions, malformed layout sequences, incompatible flags,
  over-limit layout history, tampered view definitions and stale dependencies refuse.

## Consumer documentation and exclusions

README/docs index, roadmap capability/status rows, changelog, configuration and
generated API signatures/DTOs describe actual additions. Both persisted formats
have versioned contracts; schema/transfer and frozen-contract routing are updated.
There are still 39 connection configuration fields: new settings are operation-local.

Checks: `python tools/check_documentation.py`,
`python tools/generate_api_reference.py --check`, scoped `ruff check`, and
`git diff --check`. No benchmark duration is advertised as application throughput.
No Pulse production store, installation, commit/push, release/tag or PyPI operation
belongs to this checkpoint. Full repository optional-dependency/benchmark/POSIX and
hosted release matrices are not claimed by the selected Windows/Python 3.13 runs.
