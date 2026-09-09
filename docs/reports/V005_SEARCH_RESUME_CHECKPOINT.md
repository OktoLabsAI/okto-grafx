# 0.0.5 search and resumable-import checkpoint

Baseline `b16bf1feb8e74775033cff5e8e3dcd0895d02499`, branch `feature/v0.0.5`.
Four operator-approved items: pure FTS reuse, incremental snapshot statistics,
native hybrid retrieval and resumable logical import. No new release, installed
Pulse build or production workload is included.

## Implementation boundaries

- FTS memo stores only immutable text/analyzer-derived tokens; quota/staging and
  verification reuse them within bounded ownership. Saturation computes uncached.
- Scalar corpus summaries are advanced only through complete bounded committed
  native WAL intervals on the same index generation. Missing/ambiguous proof falls
  back; normal snapshot/index/heap certificates and recovery remain authoritative.
- Native hybrid v1 uses one reader, weighted RRF and bounded source windows, strict
  table/space identity, explicit source regimes and optional bounded graph evidence.
  Identity-index endpoint validation prevents dangling relations becoming evidence.
- Resume revalidates artifact/schema/index/row prefixes and rebuilds fresh identity
  mappings from actual committed target IDs. It does not reuse abandoned lease IDs,
  overwrite a destination or expose staging as a completed database.

Consumer contracts: [FTS](../FULL_TEXT_SEARCH.md), [hybrid](../HYBRID_SEARCH.md),
[logical transfer](../LOGICAL_TRANSFER.md). The roadmap remains the only active queue.

## Final validation — passed

Windows/Python 3.13.1; source explicitly selected with
`$env:PYTHONPATH=(Resolve-Path src).Path` for parent **and child processes**.

- Complementary boundary/language/platform/coordination/tools and final
  hybrid/resume/documentation tests: **2,511 passed, 3 skipped in 227.30 s**.
- Incremental-statistics focused checks, including weighted multiple fields,
  null/empty text, complete deletion, independent writer/old snapshot, fallback,
  generation replacement and operation memo lifecycle: **5 passed in 3.95 s**.
- Final cohesive feature/consumer run on the final source: **83 passed in 80.50 s**.
  This includes FTS, incremental statistics, hybrid, ordinary/resumable transfer,
  all published examples and documentation checks. It was rerun after the final
  workspace guard that preserves an unowned incomplete `resume.next` file and the
  retired-vector-space resumption fix described below.
- Four complete Python documentation examples executed with `python -I` against
  the private installed wheel; **165 Python package files** matched source, wheel
  and installed bytes. No global environment or Pulse process was changed.
- Final wheel SHA-256: `8e77d7a264999a1571f03e76972d7e3b5d7bdd6699a05b33f3a6773cddaaaaa7`.
- Changed/new Python lint and offline documentation checks passed (all connection
  fields, FTS/transfer/hybrid options, generated signatures/DTOs, links and the eleven
  preserved roadmap-source hashes).
- Broad API/index/recovery/transaction/WAL/storage/vector/foundation/query/consumer
  regression initially finished with **12,635 passes, 5 failures, 15 skips** in
  1,326.94 s. Those failures and corrections are detailed below. The final complete
  repeat passed: **12,643 passed, 15 skipped, zero failures in 1,407.01 s**.
  All fifteen skips are POSIX-only filesystem cases on this Windows host (FIFO,
  directory fsync, symlink/rename and open-file unlink semantics). No new skip or
  weakened assertion was introduced to close the run. Local artifacts:
  `.grafx-tmp/four-items-final-green.txt` and `four-items-final-green.xml`.
  JUnit SHA-256: `471f80573a7018a8bda936fc1d303aed13802e6912316f7e9ce396cdf3a5b654`.

The initial resumed-import tests caught JSON tuple/list representation mismatch in
index declaration comparison; comparison now uses canonical JSON. Incremental
statistics tests caught the native INSERT convention (unversioned `csn=0`, unlike
tombstone's COMMIT LSN); the proof now enforces the actual native grammar. Assertions
also corrected result-field names and a misplaced test cleanup block; no product
assertion was weakened. All observed focused failures were retested after correction.

An initial broad run was stopped because child processes resolved the older global
Grafx installation instead of this checkout. Four FTS crash tests consequently hit
the expected old-build capability refusal; a separate adjacent run reproduced that
environment cause. The broad run was restarted with absolute PYTHONPATH. Aborted
run counts are not acceptance evidence and are not added to final totals.

The completed broad run then found two genuine integration omissions and one
measurement-attribution issue:

1. The pinned root-export fixture omitted CancellationToken/FTS from the earlier
   slice and the new hybrid DTOs; root exports also needed their canonical ordering.
   The explicit fixture and sorted export list now agree, without removing any API.
2. The finite query-error metric label domain omitted `query_cancelled` and
   `query_deadline_exceeded`. Emission into the real validated OpenMetrics sink
   could raise a configuration error; both codes are now declared and all reachable
   errors are tested by actual emission.
3. Process-global no-op allocation readings differed by one block/64 bytes only
   in the large mixed run. The unchanged tests passed alone (13/13) and with all
   observability/packaging (701/701 after the above fixes). They now execute in
   fresh interpreter processes to isolate allocator attribution, retaining the
   exact zero-extra-allocation assertions and the deliberately allocating-sink
   counterexample. No runtime no-op code or tolerance was changed. The first child
   harness attempt used isolated imports that excluded the user's pytest install;
   normal fresh-process imports with explicit source selection corrected that
   harness dependency, and **338/338** targeted tests passed in 20.35 s.

The final wheel was rebuilt after export/metric corrections and again passed
165-file source/install parity and all four consumer documentation examples.

A final import compatibility check reproduced a second retirement after an
interruption before publication: the normal retirement API correctly rejects that
operation. Import finalization now retires only an active space when the artifact
requires retirement; it never reactivates a retired space. Prefix validation rejects
unexpected target retirement when the artifact requires an active space. The native
retirement API itself is unchanged. The all-value-types fixture now covers both
ordinary import and interrupted/resumed retired-space import. Failing-before evidence:
1 failure/1 pass; corrected final feature group: 83 passes. This small import-only
change was made while the broad repeat was running; the final feature group and
rebuilt installed-wheel checks explicitly cover its final source.

Real child-process cuts cover after a committed batch, before/after the WAL barrier,
and before/after no-replace publication; resume verifies all rows, endpoints and FTS,
and repeat-after-lost-ACK returns the same mapping. Negative cases cover ownership,
different destination, changed prefix, duplicate metadata fields and concurrent
workspace lock refusal. Native hybrid tests cover fixed snapshots, native ANN reopen,
source-only/union/intersection/RRF reference, filters, directions, cycles/edge budgets,
invalid options/readers, missing sources and corruption that partial mode must not hide.
The broad regression and complementary/focused groups overlap; their pass counts
must not be added as a unique-test total. Benchmarks are informational, not timing
gates; this Windows run does not certify native POSIX execution or remote CI.
All four approved implementation slices are closed for this local checkpoint.
No observed functional, recovery, packaging or observability failure remains open
in the executed matrix. The documented performance/product limits are retained,
not converted into additional gates on this delivery. Source remains on
`feature/v0.0.5`; no release, commit/push or global Pulse installation is inferred.

## Measurements and reproduction

Final regression commands (separate invocations; no overlapping count sum):

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$suites = @('tests/api','tests/index','tests/recovery','tests/txn','tests/wal',
  'tests/storage_core','tests/vector','tests/foundation','tests/query',
  'tests/consumer','tests/observability','tests/storage_adapters','tests/cli','tests/smoke')
python -m pytest @suites -q --tb=short -o 'addopts=--strict-markers --timeout=60 --timeout-method=thread'
$complement = @('tests/test_import_boundary.py','tests/test_language_surface.py',
  'tests/test_optional_package_boundary.py','tests/test_platform_parity.py',
  'tests/coordination','tests/tools','tests/api/test_ops2_fts_documented_examples.py',
  'tests/api/test_hybrid_search.py','tests/api/test_transfer_resume.py')
python -m pytest @complement -q --tb=short -o 'addopts=--strict-markers --timeout=60 --timeout-method=thread'
$features = @('tests/api/test_fulltext.py','tests/api/test_fulltext_incremental.py',
  'tests/api/test_hybrid_search.py','tests/api/test_logical_transfer.py',
  'tests/api/test_transfer_resume.py','tests/api/test_ops2_fts_documented_examples.py',
  'tests/consumer/test_documentation.py')
python -m pytest @features -q --tb=short -o 'addopts=--strict-markers --timeout=60 --timeout-method=thread'
```

Latest-only timing tables are in [performance](../PERFORMANCE.md). Commands:

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
python tools/benchmark_ops2_fts.py
python tools/benchmark_hybrid_search.py
python tools/check_documentation.py
python tools/generate_api_reference.py --check
.grafx-tmp/v005-validation-env/Scripts/python.exe -I tools/check_installed_search_followup.py
```

The installed-wheel checker assumes that the just-built wheel is in
`.grafx-tmp/search-resume-dist/` and installed in an isolated environment. It refuses
source-tree imports and checks byte parity before executing examples.

FTS fixture: 200 five-word documents, 64 buckets; after one durable insert the unique
term search took **4.356 ms**, visited **21 postings + 11 WAL records**, and agreed
exactly with a separate full census visiting 1,227 postings. Ordinary transaction
time was **27.256 ms**. These are one-run observations, no before/after timing claim.
The workload provides an operation-count proof that an eligible write no longer
requires a full corpus statistics pass.

Hybrid fixture: 200 three-word documents with 2D unit-circle vectors, candidate window
100 and k=20. Cold **181.030 ms**; warm median **5.280 ms**, maximum **5.763 ms** over
20 calls. Native source rank/score fusion matched the independently assembled RRF
reference. Fused recall@20 versus an exact-source run was **1.0 for this one query**.
This is not general semantic recall or a p99 estimate; peak RSS was not measured.
The host also ran regression tests. No Pulse benchmark, reserved spec, durability
tradeoff or marginal timing threshold is part of this delivery.
