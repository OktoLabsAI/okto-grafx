# 0.0.6 posting layout and temporal-foundation checkpoint

September 10, 2026. Branch `feature/v0.0.6`, working tree after
`4ee4d2e2f9ad5295efbe2faac2f1fbdd2a1867da`. No commit, push, installation, tag or
release is asserted by this report. Earlier continuation 1–6 changes remain in
the same working tree and are preserved.

## Delivery boundary

**Item 7 implemented and tested:** explicit `layout="posting_hash"`, catalog
required bit 14, format-5 index header, page-local key dictionary and stable
reference slots. Native exact candidates, heap visibility, both OCC checks,
WAL/index effect ordering and generation publication remain authoritative.
Same-key INSERT batches have bounded, attempt-local membership preparation.
No automatic conversion of existing indexes or change to the default layout.

Measured work: 60 repetitions of one 80-character string in 512-byte pages and
one bucket occupy **4 bucket pages**; a 120-row same-key INSERT batch performs
**one membership chain scan**. These are synthetic structural counts, not elapsed
time or Pulse measurements. Warm dictionary decoding remains a limitation;
unique keys may cost more. [Consumption and trade-offs](../POSTING_HASH.md).

**Item 8 is NOT delivered as a native persistence feature.** An internal bounded
typed event codec and immutable append-image planner are implemented and tested.
They preserve supplied RecordIds, table schemas and native values; validate UUID,
sequence, framing, hashes and complete chunk coverage; and bind fixed-cardinality
images to a terminal CommitId without writing. The file is intentionally not an
admitted native redo target, and no database capability/API is activated.
The transition validator additionally checks the complete append against one
predecessor root, regenerating canonical after-images at the supplied terminal.
It refuses chunk substitution, changed chain/activation/ordinal, missing/duplicate
images and old-page overwrite. Its cost is append-bounded and it reads the prior
root once; it does not prove that the caller's CommitId is committed. Neither a
partial resident installation nor its root can stand in for native replay proof.
[Prototype contract and exact remaining integration](../specs/SYSTEM_HISTORY_APPEND_DRAFT.md).
Native atomic current/history publication, semantic interval/endpoint proof,
activation, time-travel APIs and real temporal recovery tests remain necessary.
Passing memory-image tests is not durable temporal-history certification.

## Evidence

Primary affected regression: **6,830 passed in 768.64 s (12 min 48 s)**.
Log: `.grafx-tmp/v006-posting-regression.log`.

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m pytest tests/query tests/storage_core tests/txn tests/index tests/recovery tests/wal tests/api/test_posting_hash.py tests/api/test_sparse_hash.py tests/api/test_catalogs.py tests/api/test_workspace.py tests/api/test_catalog_copy.py tests/api/test_fulltext_phrase.py tests/api/test_logical_views.py tests/api/test_nullable_columns.py tests/api/test_physical_backup.py tests/api/test_logical_transfer.py -o addopts= -q --tb=short
```

This process started before the additional posting-reference preflight guard,
new vacuum case and temporal prototype were finalized. It does not certify those
later changes. The final supplement tests the current source and those changes:
**1,314 passed in 54.44 s, zero failures/skips**.
Log: `.grafx-tmp/v006-posting-final-acceptance.log`.

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src') + [IO.Path]::PathSeparator + (Join-Path (Get-Location) '.grafx-tmp/v006-optional-test-deps')
python -m pytest tests/api/test_posting_hash.py tests/recovery/test_commit_redo.py tests/txn/test_system_history_store.py tests/api/test_vacuum.py tests/api/test_vacuum_overflow.py tests/api/test_vacuum_recovery.py tests/test_import_boundary.py tests/test_language_surface.py tests/api/test_continuation_documentation.py tests/consumer/test_documentation.py tests/api/test_html_snapshot.py tests/api/test_sqlite_import.py -o addopts= -q --tb=short
```

The optional Polars recipe used Polars/runtime 1.44.2 installed only under the
ignored `.grafx-tmp/v006-optional-test-deps` target. No global Pulse environment
was changed. Counts overlap and must not be added. This is an affected Windows /
Python 3.13 regression, not the complete repository/POSIX/hosted release matrix.

Specific posting checks cover query results, retained snapshots and independent
handles; update/delete; rehash/rebuild; vacuum and subsequent insertion; clean
read-only reopen; physical backup/restore and logical transfer; malformed slots,
dictionary links, sentinels, future stamps and duplicate keys; and simulated
unsupported-reader capability admission. Real subprocess deaths during overflow
posting placement, after posting flush and after commit are followed by two
successful recoveries/readbacks with full verification. The placement cuts are
inside an already durable user COMMIT, not pre-WAL activation crash cuts.

The two added native redo tests prove that invalid posting references refuse
before an otherwise-valid page-effect prefix is applied. Existing format tests
now use header format 6 as the future-version probe: format 5 is implemented,
not accepted without its matching layout/visibility discriminator.

The supplement includes **34 internal temporal tests** and two additional native
dispatcher refusal tests: an unadmitted `system-history.dat` image cannot move
even an ordinary preceding page, with a supplied COMMIT and with either plain or
compressed framing. This is a negative admission guarantee, not native temporal
publication/recovery certification. No production store was activated.

The supplement exposed and corrected documentation-surface debt in earlier
catalog/copy/scalar/CLI/SQLite/HTML additions (missing docstrings and a non-ASCII
HTML source glyph, now an equivalent entity). It also rejected JSON inside the
new pure engine module; historical table encoding now reuses the native binary
catalog codec. No boundary-test exemption was added.

Documentation checks: `tools/check_documentation.py`, generated API reference
freshness, scoped Ruff and `git diff --check`. README, roadmap, capabilities,
operation configuration, API contract, format specification and current
performance evidence are updated. There are still 39 connection fields.

**The requested 7–8 delivery is incomplete because item 8 remains partial.**
No new authorization requirement or marginal performance gate is introduced.
