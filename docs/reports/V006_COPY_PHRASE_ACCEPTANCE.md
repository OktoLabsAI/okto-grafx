# 0.0.6 bounded copy and exact phrase checkpoint

Date: September 10, 2026. Branch: `feature/v0.0.6`.
Base commit: `4ee4d2e2f9ad5295efbe2faac2f1fbdd2a1867da`; subsequent local
working-tree changes, not a new release, installed Pulse build or published SHA.

## Delivered boundaries

Continuation item 3: [bounded existing-target copy](../CATALOG_COPY.md), using
one ordinary native write transaction for copied rows plus an indexed durable
receipt. Explicit target setup reuses existing identity/commit-history capabilities.
The source package is detached and checksummed. Target schemas must already match;
node PKs are required and conflict policy is `fail`. Anonymous nodes, arbitrary
subgraph selection, automatic schema creation and skip/merge are not claimed.

Continuation item 4: [typed exact phrase search](../FULL_TEXT_SEARCH.md#exact-analyzed-phrases-006-development),
verifying contiguous analyzed positions in existing-index candidates, in one
certified snapshot. No durable positional-posting layout, returned position list,
slop/proximity query or CLI/procedure/hybrid phrase option is claimed. Index bytes,
required bits and default whole-term semantics remain unchanged.

At this checkpoint, items 5–8 were still open. The subsequent
[views/schema checkpoint](V006_VIEWS_SCHEMA_CHECKPOINT.md) implements bounded 5–6
and records expanded regression evidence. The **entire eight-item request is not
complete**; consult the [single roadmap](../../ROADMAP.md#approved-capability-continuation-after-4ee4d2e).

## Final grouped regression

**916 passed in 241.58 seconds**, Windows / Python 3.13.
Log: `.grafx-tmp/v006-copy-phrase-final-regression.log`.

```powershell
python -m pytest tests/api/test_catalog_copy.py tests/api/test_catalogs.py tests/api/test_workspace.py tests/api/test_fulltext_phrase.py tests/api/test_fulltext.py tests/api/test_fulltext_prefix.py tests/api/test_fulltext_relationships.py tests/api/test_fulltext_durable.py tests/api/test_fulltext_history.py tests/api/test_fulltext_incremental.py tests/api/test_hybrid_search.py tests/api/test_executemany.py tests/api/test_connect_lifecycle.py tests/api/test_connect_options.py tests/api/test_identity_and_errors.py tests/api/test_read_only_and_doors.py tests/api/test_read_only_storage_wiring.py tests/api/test_public_boundary_concurrency.py tests/api/test_commit_history.py tests/api/test_commit_history_concurrency.py tests/api/test_transaction_descriptor_reuse.py tests/txn/test_reader_lifecycle.py tests/txn/test_snapshot.py tests/txn/test_lifecycle_serialization.py tests/query/test_lifecycle.py tests/query/test_primary_key_index.py tests/query/test_read_your_own_writes.py tests/query/test_native_scalars.py tests/query/test_scalar_parameter_validation.py tests/api/test_scalar_extensions.py tests/storage_adapters/test_read_only_local_lifecycle.py tests/recovery/test_commit_catalog_native_replay.py tests/test_import_boundary.py tests/test_optional_package_boundary.py -o addopts= -q --tb=short
```

This is an affected-surface regression, not the complete repository/POSIX/hosted
CI/release matrix. Earlier overlapping runs (80 new/catalog tests, 10 phrase tests,
260 import-boundary tests) are not added to the final count.

An initial grouped run had 879 passes and one inherited failure: the prior
minimum-parity `domain/query/scalars.py` lacked the required postponed-annotation
import. That omission was fixed, both import-policy suites passed, and the final
group above reran the scalar feature/parameter/extension checks as well. The
failed initial run is not represented as a passing result.

## Direct feature evidence

Copy tests include existing target rows, logical endpoint remapping, self-store
refusal, whole-transaction conflict rollback, detached input after source mutation,
same key/different metadata, changed checksum, bounds, missing endpoints, target
schema mismatch, unsupported anonymous nodes and reserved receipt ownership.
Vector values remap distinct source/target space IDs; nullable values and empty
relationship properties are covered. Session copy honors alias permission and
keeps detach refused during commit.

Receipts survive checkpoint/reopen and must have matching native COMMIT metadata.
Rewriting a receipt in an unrelated commit refuses even when its values match.
Repeated lookup is tested with heap scans and commit-history scans disabled;
it reads the exact PK index and qualified commit lookup. Replay leaves the
published sequence unchanged. Concurrent identical requests leave one copy and
one request receipt; a losing request can explicitly retry to obtain that outcome.

Real child processes are killed at three controlled points: before the copy
commit, after its completed commit, and after its durability barrier but before
page application. The last cut is gated on the **copy transaction** being committed,
not an earlier internal allocation-reservation commit. Recovery sees neither data
nor receipt before durability, or the complete data/receipt afterward. Replay,
edge queries, native verification, checkpoint and a second reopen validate it.

Phrase tests cover order, repeated tokens, field boundaries, case/punctuation
normalization, all four declared analyzers, empty queries, filters, query/candidate/
memory budgets, cancellation, updates under an older reader, rebuild and read-only
reopen, plus relationship fields. Exhaustive short binary-token sequences compare
the linear KMP verifier with a simple contiguous-subsequence oracle. Default
whole-term, prefix, durable/history statistics and hybrid regressions also pass.

## Documentation and implementation checks

Roadmap, README capability inventory, consumer guides, configuration references,
API signatures/DTOs and routing specs were updated. Successful checks:

```powershell
python tools/generate_api_reference.py --check
python tools/check_documentation.py
python -m ruff check src/okto_grafx/catalog_copy.py src/okto_grafx/catalogs.py src/okto_grafx/engine/fulltext.py src/okto_grafx/domain/query/scalars.py tests/api/test_catalog_copy.py tests/api/test_fulltext_phrase.py tests/api/catalog_copy_worker.py
git diff --check
```

No new performance acceptance ratio, WAL weakening, global writer serialization,
production graph mutation, commit/push, installation or publication accompanies
this checkpoint. Test durations are not application performance benchmarks.
