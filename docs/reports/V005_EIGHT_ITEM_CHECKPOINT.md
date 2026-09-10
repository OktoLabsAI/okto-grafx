# 0.0.5 eight-item continuation

Date: September 9, 2026. Branch: `feature/v0.0.5`; baseline `e34a9e7`.
Status: all eight items delivered; final affected-surface/feature regression and
installed-wheel validation passed, with no unresolved failures. Broad-run failure
dispositions and Windows-only coverage limits are recorded below.
This receipt is evidence for the [active roadmap](../../ROADMAP.md), not a new backlog.
No commit, push, release, live Pulse deployment or production-data operation is implied.

## Implemented scope

| Order | Delivered boundary | Focused evidence / consumer contract |
| --- | --- | --- |
| 1 | BM25 query-term frequencies computed once per field; exact original accumulation order and scores | Counting-iterator test plus independent BM25 oracle, multi-field weights/old reader; `test_search_eight_followup.py`; [FTS](../FULL_TEXT_SEARCH.md) |
| 2 | Hybrid BFS through certified `ef_`/`et_` indexes; missing paths choose scan before adoption, adopted corruption never falls back | Both/in/out parity with scan, disconnected edges, old snapshot and corruption; `test_hybrid_search.py`; [hybrid](../HYBRID_SEARCH.md) |
| 3 | Shared cooperative controls through exact reads, targeted witnesses, HNSW wait/build/catch-up and scoring loops | Cancellation inside exact, cold ANN and warm ANN work; owned reader remains usable; [vector controls](../INDEXES_AND_VECTORS.md#cooperative-vector-read-control) |
| 4 | Simultaneous logical source/fusion/graph envelope, overall and per-phase peaks | Compound budget fails even where each separate source fits; explicit non-RSS exclusions; [hybrid options](../HYBRID_SEARCH.md#options) |
| 5 | Opt-in durable FTS count/length summary, bit 6, snapshot-qualified reads, complete-COMMIT replay and private-build census | Update/delete/null/rollback/rebuild/transfer; child process death before/after scalar publication; reopen/checkpoint/read-only, old capability refusal, malformed and semantic tamper; [format](../specs/FTS_DURABLE_STATISTICS.md) |
| 6 | Explicit 65,536-bucket ceiling, bit 7, bounded physical skew census and optional growth suppression | Native 8,192-bucket rehash, old snapshot/reopen, max-domain codecs, old-reader refusal, repeated-key skew and resource refusal; [indexes](../INDEXES_AND_VECTORS.md#explicit-distribution-diagnostics-and-wide-directories) |
| 7 | Disk-spooled chunked capture/readback by default, explicit memory mode | Verified backup/restore/provenance, corruption/fault injection, real foreign writer during capture, progress during artifact output, process death before promotion, chunk ceiling; [backup](../BACKUP_RESTORE.md) |
| 8 | Complete 1..N application plan, checksums and ownership ledger; additive NODE/REL TABLE and VECTOR SPACE, atomic per version | Dry-run unchanged WAL/catalog, idempotence, changed checksums/ahead/gaps/ownership refusal, partial-plan runtime failure, bounded OCC retry, independent concurrent migrators, abrupt death before/after COMMIT; [migrations](../SCHEMA_MIGRATIONS.md) |

Multi-reader/writer, both OCC stages, native committed-effect preflight, publication
fences and WAL barriers remain. No authority cache, extra global writer lock,
silent partial traversal, destructive migration or recovery guess was introduced.

## Test receipts

Counts below overlap and must not be summed as distinct tests.

| Run | Result |
| --- | --- |
| Initial items 1–4 grouped regression | 8,469 passed, 11 failures, 1 skip; failures were public type/export/future annotations and documentation drift, subsequently corrected |
| Durable FTS / large hash / physical backup focused run | 29 passed in 46.21 s |
| Migration, durable FTS and catalog-bound focused run | 68 passed in 21.77 s |
| Final all-eight feature plus public-surface delta | 1,571 passed in 116.56 s |
| Final verifier/retained-history/index verification plus maintenance-facade and durable FTS revalidation | 127 passed in 20.87 s |
| Final planner/parser, maintenance delegation and index-bound contracts | 368 passed in 1.94 s; retains explicit above-ceiling refusal assertions and checks both skew modes |
| Broad functional regression, including corpus/coordination/storage/CLI | 15,274 passed, 11 failed, 18 skipped in 1,740.70 s; the three outdated facade/planner expectations and eight missing-docstring module checks were corrected and are included in final clean revalidation below |
| Final clean revalidation: all eight features, verifier, public/import/language/platform/consumer contracts and every failing suite | **3,653 passed, zero failures in 154.75 s** |
| Source lint and documentation coverage | PASS: `ruff check src tests tools`; links/anchors, all connection settings, operation options, API signatures/DTOs and preserved roadmap archive |
| Rebuilt installed wheel | PASS: 170 Python files identical across source/wheel/install; five executable documentation examples |
| Installed all-eight public-API probe | PASS: migrations, durable search, indexed hybrid, cancellation/reader reuse, logical-memory diagnostics, wide rehash, disk backup/restore and verification |

The broad invocation began before the final two-file verifier correction. The
1,571-test delta and 127-test verification regression ran against that final code.
They include the newly added direct-device tamper case, which the older invocation
could not have collected. This is explicit changed-path revalidation, not a claim
that an earlier process hot-reloaded newer Python modules.
After the broad result, only test expectations and missing function docstrings
changed. The final clean suite rechecks the full affected surface and features;
it is not another 29-minute repeat of unchanged tests. All counts and initial
failures are retained here rather than relabeling the earlier invocation green.

### Failures found and closed while implementing

- Initial detached FTS builds needed to accumulate the corpus scalar from their
  already-generated live length entries, instead of leaving the initial zero record.
- The public verifier is a separate path from index-build verification; it now
  independently recomputes totals from canonical versions and validates the scalar.
- An adversarial checksum-valid **device-only** alteration exposed a warm-pool
  blind spot. Public summary verification now reads/decode-validates page 0 from
  the device, including native file/generation identity; normal certified search
  and private-build validation keep their respective resident-page paths.
- Extending bounds required updating old 4,096/262,144 upper-bound fixtures to
  the canonical domain constants, not deleting boundary assertions.
- Backup tests were moved from the former `_put` object writer to the actual new
  chunked create/append path; concurrency/crash hooks now assert they ran.
- The migration ledger's primary-key nullability follows the native planner;
  read-only reopen tests checkpoint first, as required by the unchanged open protocol.
- Public facade/type/export and documentation expectations were updated to new
  signatures, including explicit default/opt-in `check_skew` delegation.

The first all-directory collection attempt could not locate its pinned Pulse
baseline automatically. Existing immutable local corpus checkouts were verified
at the exact pinned commits and selected through the documented environment
variables. No Pulse source, corpus or live board was modified to make tests pass.

## Reproduction and package identity

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$env:PULSE_CORE_BASELINE='D:/Projetos/Techridy/okto-pulse-core-corpus-baseline'
$env:PULSE_COMMUNITY_BASELINE='D:/Projetos/Techridy/okto-pulse-community-corpus-baseline'
$suites = @('tests/api','tests/index','tests/recovery','tests/txn','tests/wal',
  'tests/storage_core','tests/vector','tests/foundation','tests/query',
  'tests/consumer','tests/observability','tests/storage_adapters','tests/cli',
  'tests/smoke','tests/coordination','tests/tools','tests/corpus',
  'tests/test_import_boundary.py','tests/test_language_surface.py',
  'tests/test_optional_package_boundary.py','tests/test_platform_parity.py')
python -m pytest @suites --tb=short -o 'addopts=--strict-markers --timeout=60 --timeout-method=thread'
python -m ruff check src tests tools
python tools/generate_api_reference.py --check
python tools/check_documentation.py
python -m build --wheel --outdir .grafx-tmp/search-resume-dist
# Install the resulting wheel into a disposable environment, not production Pulse.
# Run these with that environment's interpreter and isolated import mode:
python -I tools/check_installed_search_followup.py
python -I tools/check_installed_eight_followup.py
```

The 18 broad-run skips are POSIX-only filesystem/locking/directory-fsync branches;
this receipt does not claim they executed on Windows. The optional `tests/bench`
performance gates were not run: the operator explicitly
removed latency gates. Functional correctness/performance-operation-count tests
remain included. Platform skips are not equivalent to a POSIX run on this Windows host.

Final wheel SHA-256:
`0968aeb6e7cb6acb35a692fdd02a13335d64e5abd03b3c94394e8032bf89d4b8`.
Build/source version remains **0.0.5**. The wheel is a local test artifact, not PyPI publication.

Local detailed logs: `.grafx-tmp/eight-final-regression-valid.txt` and matching XML,
`eight-final-delta.txt/.xml`, `eight-verifier-regression.txt`,
`eight-final-acceptance.txt/.xml`, `eight-contract-regression.txt`,
`eight-wheel-examples-final.txt`, `eight-installed-smoke-final.txt`.
Latest-only native measurements and their workload limitations are in
[performance](../PERFORMANCE.md#eight-item-continuation-latest-small-installed-wheel-probe).

## Deliberately remaining limitations

No historical durable-summary retention; old/ineligible FTS snapshots can census.
No full-graph O(1) promise, sparse/sharded hash or automatic hot-key redistribution.
Hybrid source windows still bound recall, and logical-memory tariffs are not an
RSS ceiling or a charge for already-owned HNSW construction caches. Cooperative
controls do not preempt one blocked device call. Physical capture still pauses
commit publication and needs spool/artifact/verification disk space. Application
migrations do not implement ALTER, backfills, downgrade, views, derived graphs or
all-plan atomicity. These remain in the consolidated roadmap, not new delivery gates.
