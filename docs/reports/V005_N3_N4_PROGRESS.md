# 0.0.5: N3 delivery and N4 native publication progress

September 8, 2026; branch `feature/v0.0.5`, on top of the N1–N2 working changes.
No commit/push, release, production vacuum, capability activation or Pulse
deployment was performed in this step. Reserved production specs were untouched.
[Active roadmap](../../ROADMAP.md#proposed-next-round-after-the-005-checkpoint).

## N3: implemented retirement boundary

`HeapStore.plan_vacuum` now selects horizon-eligible overflow versions as well as
inline ones. It builds detached images; before returning them, a whole-catalog
pass proves exclusive ownership and complete coverage of every reachable overflow
chain. Selected/unselected tables and retained versions all participate. Shared
pages, cycles, invalid addresses, wrong types/slots and missing/extra payload
coverage refuse. No selected chain becomes FREE before these checks complete.

The existing transaction atomically logs retained-chain relinks, freed version
slots, empty FREE overflow pages, index reconciliation and the durable snapshot
floor. Both OCC validations and WAL-before-data order remain. New report fields:
`TableVacuumReport.eligible_overflow_versions`, `reclaimed_overflow_pages` on
table/aggregate reports. `complete` includes both eligible inline and overflow
versions. `skipped_overflow_versions` now identifies eligible overflow versions
left by the selected-version quota.

Important limits, not omitted work claimed complete:

- Quiescence remains the existing **operator assertion**. The same-handle active
  transaction is rejected, but `confirm_quiescent=True` does not discover or stop
  every other handle/process. All others must actually be stopped. A test initially
  asserted a nonexistent foreign-handle detector; it was corrected to the real
  contract, not used as proof of automatic foreign-process exclusion.
- Retired pages are empty but the file length is unchanged. There is no new free
  allocator, automatic reuse, truncation or online vacuum. This removes retained
  history; it does not yet stop future disk growth.
- Ownership verification is foreground O(reachable overflow pages) and holds a
  page ownership set; `max_versions` bounds selected versions, not total IO/memory.
  This cost is not added to ordinary reads/writes.

Tests cover page sizes 512/8192, bounded passes, detached planning, current answer
preservation, FREE-page counts, repeated vacuum, clean verify/reopen and an alias
in an unselected table refusing before WAL. The existing five-cut vacuum fault
matrix now also runs with overflow payloads. It proves full pre/post state after
WAL append/barrier, heap/index apply and publication interruptions.

## N4: native writer connected; overall item still incomplete

Automatic journal publication now follows the existing private activation fence:

1. First OCC retains all caller/snapshot interests.
2. Under current commit authority, journal preparation validates the independently
   qualified durable head. Only the exact empty activation interval may initialize
   missing files. The immutable stream header is included in that first commit.
3. New journal page interests join the physical second OCC with the current
   materialization baseline; caller pre-staged pages are unchanged.
4. The complete journal body and page images bind to the final COMMIT LSN. Segment
   roll rebinds both body and stamps; required raw/compressed journal framing is
   retained. It never falls back to unmarked legacy journal records.
5. Normal WAL barrier, page application/flush and control publication acknowledge
   data/history together. Internal identity-floor commits are journaled too;
   dedicated maintenance preparation records the maintenance kind.

Attempt retention is bounded and terminal cleanup removes owned plans. Transaction
byte quotas include journal images; existing WAL batch budgets still apply. Clock
conversion refuses unrepresentable readings before append. No-writing completion
does not create an entry. The temporary blanket writer guard is replaced only for
identity-qualified native compositions; activation remains private.

**Not yet delivered from N4's original scope:** public metadata-at-begin and qualified
concurrent lookup/typed history, full public verifier/metrics integration, transfer
and restore/fork identity semantics, their hostile-input/concurrency tests and the
complete public capability acceptance matrix. No `CommitId`/`CommitMetadata` export
or public activation has been advertised. Native tests are not certification of
those unimplemented consumer doors. These are remaining original requirements,
not new performance gates.

Publication tests exercise schema/data, an already-open second participant, forced
identity lease refills, 4 KiB segment rolls and 64 KiB segments, compression on/off,
private full-history verification, checkpoint and subsequent writes after reopen.
A real fault-device matrix interrupts before/after first-journal file writes,
WAL append/barrier and control publication; recovery produces old data with the
activation boundary or new data with exactly one matching history record. A second
checkpoint/reopen remains consistent. This is a first-publication matrix with no
user metadata, not the entire CAP-1 acceptance matrix.

## Validation and cost

- Grouped transaction/recovery/storage/vacuum/checkpoint run: **2,925 passed,
  two investigated failures, 234.38 s**.
- One failure was the superseded “overflow is retained” expectation. It now checks
  detached retirement images, counts and non-mutating planning.
- The metrics failure reproduced against the untouched earlier baseline. Its
  `LogWal` fixture returned `LogRecord` doubles where replay requires native
  `WalRecord` commit envelopes. That one test now uses its existing real WAL
  factory; recovery validation was not relaxed.
- Corrected affected/adjacent group: **305 passed in 16.82 s**.
- Final publication/activation/vacuum/crash closure after maintenance classification:
  **31 passed in 8.47 s**. Totals overlap; they are not additive.
- Ruff, generated public-reference freshness and documentation checks are run at
  closure. No whole-repository static typing or platform matrix claim.

Latest small native sample: **20.259 ms median** for an update with journal,
512-byte pages, one row, 16 measured iterations after two warmups. A paired
untracked control was run in alternating order to expose added cost; the raw
receipt retains both sets. This is not a Pulse consolidation, p99 or general
throughput promise. N4 adds durability/provenance work; it is not a speedup.

Reproduce with source `PYTHONPATH`:

```text
python tools/perf_round/round005_commit_journal.py
python -m pytest tests/txn/test_commit_catalog_publication.py tests/api/test_commit_catalog_publication_recovery.py tests/api/test_vacuum_overflow.py
python tools/generate_api_reference.py --check
python tools/check_documentation.py
```

Ignored local receipts:

- `.grafx-tmp/n34-regression.txt`, SHA256 `a3282d7670c661de92bbfc4d3116008dd17ae4128e1b2fed6b93048707f6dab6`.
- `.grafx-tmp/n34-final-focused.txt`, SHA256 `49885136659570c4429a2c8fb38b902ee22e3d633bc2eeb55b5f2fb37c748cca`.
- `.grafx-tmp/n34-closure.txt`, `.grafx-tmp/n34-metrics-baseline.txt`.
- `.grafx-tmp/n4-journal-cost-final.json`, SHA256 `29a3488dbc606fb961d369f98c011881faecd3b86b19c7084b2336a456b465c6`; includes measured source hash.
