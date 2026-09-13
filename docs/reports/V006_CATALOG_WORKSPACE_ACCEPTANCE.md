# 0.0.6 CatalogSession/workspace checkpoint

Date: September 10, 2026. Branch: `feature/v0.0.6`.
Baseline commit: `4ee4d2e2f9ad5295efbe2faac2f1fbdd2a1867da`; this report describes
the subsequent local working-tree changes, not a new published commit/release.

## Implemented scope

Items **1–2 only** of the
[approved capability continuation](../../ROADMAP.md#approved-capability-continuation-after-4ee4d2e):

- `okto_grafx.catalogs`: explicit owned/borrowed handles, read-only-by-default
  aliases, store UUID/path checks, native single-catalog transactions, lifecycle
  refusal/cleanup and bounded in-flight acquisition/begin/detach work.
- `okto_grafx.workspace`: immutable policies/results, explicit root precedence,
  allowlist-bounded parent search, marker configuration and user/cwd opt-ins.
- Public exports, generated API appendix, consumer guide, option table,
  integration/index/README and roadmap/spec status updates.

No engine page/WAL format, query behavior, commit protocol or on-disk required
capability was changed. No store/Pulse installation or PyPI publication was made.
CAP-2 as a whole remains open; items 3–8 are not certified by this report.

## Evidence

Final grouped affected regression: **437 passed in 51.06 seconds**, Windows,
Python 3.13. Log: `.grafx-tmp/v006-catalog-workspace-final-regression.log`.

```powershell
python -m pytest tests/api/test_catalogs.py tests/api/test_workspace.py tests/api/test_connect_lifecycle.py tests/api/test_connect_options.py tests/api/test_identity_and_errors.py tests/api/test_read_only_and_doors.py tests/api/test_read_only_storage_wiring.py tests/api/test_public_boundary_concurrency.py tests/api/test_commit_history.py tests/api/test_commit_history_concurrency.py tests/api/test_transaction_descriptor_reuse.py tests/txn/test_reader_lifecycle.py tests/txn/test_snapshot.py tests/txn/test_lifecycle_serialization.py tests/query/test_lifecycle.py tests/storage_adapters/test_read_only_local_lifecycle.py tests/recovery/test_commit_catalog_native_replay.py -o addopts= -q --tb=short
```

The new tests cover:

- Catalog selection/default switching while native transactions stay pinned;
  independent writes and preservation of borrowed ownership.
- Reserved/invalid/case-normalized aliases, duplicated physical clone UUIDs,
  permissions, closed handles and active detach/close refusal.
- Slot bounds including pending begins, attaches and detach cleanup; failures
  release reservations; blocked work on one store does not block another.
- First-failure cleanup aggregation, body-exception preservation and acquired
  handle cleanup after attachment validation failure.
- Existing checkpoint-complete read-only opens: pre-existing bytes unchanged;
  only newly created **empty native transaction coordination lock files** allowed.
  An uncheckpointed read-only open refuses instead of recovering with writes.
- Real concurrent subprocess commits to distinct explicit catalogs, plus
  deterministic workspace resolution in two subprocesses.
- Discovery precedence/count bounds, no creation or implicit environment/home
  scope, user opt-in, root escapes, invalid options and changed directory identity.
- Actual Windows junction rejection; drive/stream/trailing-dot/space path alias
  rejection. POSIX symlink branch exists but was not exercised on this host.

Earlier focused checkpoints (41, 51, 53 and 432 tests) overlap the final batch;
do not add their counts. A broader all-API/transaction selection was interrupted
and is **not a passing regression result**. It included an over-strict new test
that treated empty reader coordination lock files as graph mutation; the final
test validates unchanged existing bytes and permits only those exact empty lock
files. No read-only recovery safeguard was relaxed to pass it.

Additional successful checks:

```powershell
python -m ruff check src/okto_grafx/catalogs.py src/okto_grafx/workspace.py tests/api/test_catalogs.py tests/api/test_workspace.py tools/generate_api_reference.py
python tools/generate_api_reference.py --check
python tools/check_documentation.py
git diff --check
```

Documentation validation covers links/anchors, the existing 39 connection fields,
generated API contracts and 11 preserved source plans. New session/workspace
options are operation-local and documented separately; they are not connection
fields. This is an affected-surface regression, **not** the complete repository,
POSIX, hosted CI or release certification matrix.

## Next implementation boundary

Item 3 must add atomic copy plus an indexed durable idempotency receipt to an
**existing** target. `transfer._import_into` currently commits batches into a
private fresh destination, so wrapping it does not satisfy that contract.
The current implementation deliberately does not advertise a copy API or reuse
commit history/MVCC as retained graph history. Remaining order and status live in
the single roadmap; this report does not establish a second backlog.
