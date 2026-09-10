# 0.0.5 items 3–4: logical transfer and native full-text search

Date: 2026-09-08. Branch: `feature/v0.0.5`; base `692cc26` plus the current
development changes. This is local implementation/acceptance evidence, not a
PyPI release, main merge, Pulse deployment or universal platform certification.

## Delivered contracts

| Item | Implementation | Consumer documentation |
| --- | --- | --- |
| 3 / OPS-2 | Versioned streaming logical schema/rows/vectors artifact; bounded admission, immutable snapshot, schema-race refusal, checksums; fresh UUID and row/endpoint/space mapping; native batch transactions; index reconstruction; exact readback and verified no-replace promotion | [Logical transfer](../LOGICAL_TRANSFER.md) |
| 4 / GX-CAP-5 v1 | Persisted native HASH postings; four pinned analyzers, field weights/BM25, typed and closed procedure APIs, snapshot/filter/work bounds; transactional insert/update/delete, detached rebuild, verification, replay and backup/transfer | [Full-text search](../FULL_TEXT_SEARCH.md), [wire format](../specs/FULLTEXT_V1_FORMAT.md) |

The import/export module is outer composition, not a second storage implementation.
FTS uses existing index generations, row intents, WAL effects and commit fences.
Neither feature disables the two OCC passes, durable ACK or independent participants.

## Defects found and closed during integration

1. Recovery used `min(latest_table_stamp, checkpoint)` as a historical watermark.
   A later write could therefore invent a table write at an unrelated checkpoint
   and wrongly mark a valid older generation stale before replay. Native heaps now
   inspect actual stamps at/before the floor **only for ambiguous older indexes**.
   Already-covered floors retain their existing scoped-photo cost. Positive real
   crash replay and a negative genuinely omitted pre-floor write are both tested.
2. Detached index publication after enabling commit history rejected its own native
   journal's physical write interests as a broken transaction seal. The final seal
   now admits exactly the journal partitions derived from the owned prepared append.
   It does not accept arbitrary extra partitions, row intents, page images or changed
   read interests. The early seal and both OCC checks remain. Tests cover provenance,
   rebuild/reopen and an injected extra interest that must still refuse.

## Test evidence

Run with `PYTHONPATH=src`, Windows, Python 3.13.1. Common pytest options:

```text
-q --tb=short -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

The grouped regression covered `tests/api tests/index tests/recovery tests/txn
tests/wal tests/storage_core tests/test_import_boundary.py
tests/test_optional_package_boundary.py`: **5,300 passed, 11 failed, 1 skipped in
580.37 s**. All eleven failures were instrumentation wrappers missing the new
historical-watermark keyword. They were corrected without weakening assertions.

The corrective group covered all four affected instrumentation modules, staleness,
heap storage, the full recovery directory, public-surface/import contracts, consumer
documentation and both new features: **2,709 passed, 1 failed in 170.38 s**. This
closed the eleven failures and exposed defect 2 above in the added FTS/provenance
test. A final transaction/feature regression was run after that correction; see
the final acceptance entry below. Counts overlap and must not be summed as unique
tests. No failed run is presented as a clean gate.

New feature tests include parallel writers/foreign handles, old snapshots, rollback,
delete/update, fixed Unicode/code corpus, ranking/filtering, all retention limits,
cancellation, stale/corrupt refusal, provenance, physical/logical transfer, and native
subprocess cuts after durable WAL but before page publication for create/update/delete/
rebuild. Recovery is repeated to assert idempotence. This is not every imaginable
device fault or a newly run Windows/POSIX certification matrix.

Logical-transfer tests cover empty tables, parallel edges/self-loops, all native value
families, nested vectors/retired spaces, remapping, source writes during export,
schema races, artifact corruption/truncation/budget/identity refusal, repeat imports
and publication failure preserving the source/existing destination.

The previous build was archived without modifying the worktree:

```text
git archive --format=zip --output=.grafx-tmp/v005-pre-fts.zip HEAD src
python tools/check_fts_previous_build.py .grafx-tmp/v005-pre-fts-baseline/src
```

Four cases passed: read-only/writable open after checkpoint and after durable-WAL-only
activation. Old writable opens and checkpointed read-only opens reject capability
bit 5; an old read-only open of an uncheckpointed store refuses recovery admission.
Catalog, heap, meta, index generations, commit state and retained WAL hashes remain
unchanged in each old-build attempt. The current build then recovers and searches
the store successfully. The archived baseline is pre-FTS `692cc26`, not a claim to
have run every previously published wheel.

The built `okto_grafx-0.0.5-py3-none-any.whl` was force-installed with `--no-index
--no-deps` into the existing **private validation environment**, not global Pulse.
An isolated `python -I` verified the site-packages import path and executed/asserted
both complete published Python examples. The source examples also run in pytest.

## Documentation audit of previous deliveries

| Earlier delivery | Audit result / correction |
| --- | --- |
| N4 commit provenance | The consumer guide and implementation spec existed, but README capability coverage, milestone index, commit ADR and capability strategy still included private-only/pending language. Updated current status and linked bounded N4 evidence; historical checkpoint text remains historical. |
| R4 physical backup/restore | Guide existed; `BackupReport` and module-level entry points were missing from the generated API appendix. Added DTO/defaults and top-level signatures for backup as well as transfer/connect. |
| Typed configuration | Kept all 36 connection fields and defaults covered; corrected the authored connect signature to `Unpack[ConnectOptions]`. New operation-local FTS/transfer options have separate complete field/default/limit tables and automated drift checks. |
| R1–R3 storage/performance and OPS-5/OPS-8 | Existing guides/changelog/checkpoints already describe scope, risks and controls. Added cross-links for interactions with FTS/transfer; no invented public knob or changed default. |
| Roadmap/source history | One current queue, explicit implemented-v1 versus residual scope. Eleven archived plans remain hash/line-count checked and unchanged. |

README, roadmap, guide index, API, configuration, query language, index/vector,
integration, operations, changelog, capability routing and format documents are
cross-linked. API generation now includes public standalone functions and new DTOs.
Documentation checks validate links/anchors, exact option fields, generated signatures
and preserved sources. Ruff and documentation checks pass.

## Explicit limitations, not hidden completion claims

- Logical transfer is current-state-only; commit-history transfer hooks are separate.
  There is no mid-import resume, existing-target merge, physical-settings copy or
  automatic downgrade. Source RecordId mappings are returned, not applied to arbitrary
  application property references.
- FTS-v1 has OR terms, no phrase/prefix/relationship search or arbitrary CALL/YIELD.
  Analyzer changes require a new named index. Frozen Unicode semantics are documented.
- Cold FTS corpus statistics remain O(stored postings + document text), with eight
  scalar snapshot/certificate cache entries per handle. No broad graph-load speedup,
  full Pulse validation, RSS guarantee or marginal timing gate is claimed.
- Required format capability activation is one-way. Back up/export before mixed-build
  rollout. Corruption or uncertain commit outcomes do not authorize blind repair.

## Final acceptance

After defect 2 and its adversarial regression were implemented:

```text
python -m pytest tests/api/test_fulltext.py tests/api/test_logical_transfer.py
  tests/api/test_ops2_fts_documented_examples.py tests/api/test_commit_history.py
  tests/api/test_commit_history_concurrency.py
  tests/api/test_commit_catalog_publication_recovery.py tests/txn
  -q --tb=short -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**991 passed in 246.03 s**. No observed runtime failure remains open from these
runs. The public-surface, complete recovery, instrumentation and documentation
checks had passed in the preceding corrective group; the final group specifically
reran the changed transaction boundary and feature/provenance integration.
After final documentation edits, the documented-workflow and consumer-documentation
modules passed again: **14 passed in 4.72 s**. Final Ruff, generated-reference,
link/configuration/preservation and `git diff --check` checks passed.

Wheel SHA-256:
`c528a34d16ea2ecb6b93810ce5a9e9d36e4695d6c60d17d7dda117d5bec0523c`.

Bounded performance run, after the test processes finished:
`PYTHONPATH=src python tools/benchmark_ops2_fts.py`. Fresh local storage/default
configuration, Windows/Python 3.13.1, google-crc32c 1.8.0 and NumPy 2.5.2 installed.
200 documents, one STRING field, five words per document, ten shared group terms,
64 hash buckets. Query `group3`, top-20, exactly 20 results. No concurrent workload
was injected; this is a development-host sample, not an isolated production SLO.

| Operation | Latest measured value |
| --- | ---: |
| Build persisted index | 458.654 ms |
| Cold search | 57.658 ms / 1,220 postings visited |
| Warm search (20 repeats, median) | 3.903 ms / 20 postings visited |
| One new document, begin/stage/durable commit | 44.092 ms |
| Export 201 documents, manifest and verification | 165.526 ms / 13,344 bytes |
| Import/rebuild/verify/checkpoint/reopen/promotion | 1,554.748 ms |

Every benchmark operation asserts identities/counts or verifies/reopens its output.
Cold versus warm denotes cache regimes of this same implementation, **not before/
after evolution**. No earlier-engine, Ladybug, full KG UI, memory/RSS, p99 or
large-corpus throughput comparison was performed.
