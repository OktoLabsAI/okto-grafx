# 0.0.5: N3/N4 implementation and regression closure

September 8, 2026. Branch `feature/v0.0.5`; base commit
`b12500d40e7c00edfa5175e7e050c4f07136ec30` plus the uncommitted working changes.
This report supersedes the incomplete status of the
[N3/N4 native checkpoint](V005_N3_N4_PROGRESS.md), not its historical evidence.
[Roadmap](../../ROADMAP.md) · [Consumer contract](../COMMIT_HISTORY.md).

Status: implementation and local regression closure complete within the bounded
N3/N4 scope. This is a development checkpoint, not a
release, production deployment or certification of every platform.

## Delivered scope

| Slice | Delivered and exercised | Explicit boundary |
| --- | --- | --- |
| N1/N2 and preceding 0.0.5 work | Existing-index admission, bounded scalar preparation, projected pages, cold admission, complete synthetic Pulse ACK and typed options participate in the broad regression | No new performance threshold, changed snapshot authority or production benchmark consumption |
| N3 | Foreground retirement of eligible overflow versions and exclusively owned overflow pages, through ordinary WAL/OCC; quota, alias/corruption refusal, crash and reopen tests | Actual operator-enforced quiescence required. No automatic page reuse, file shrinking or online vacuum |
| N4 publication | Native metadata/data journal publication, first-file initialization, both OCC checks, final-COMMIT-LSN rebinding, segment rolls, compression, internal maintenance identities and recovery | Opt-in, one-way activation. Earlier commits remain an explicit untracked interval |
| N4 consumer | Immutable bounded metadata captured before begin IO and retained on retry; UUID-qualified lookup and bounded snapshot history; full verification and payload-free metrics | No graph time travel, authenticated actor identity, metadata idempotency key or audit retention policy |
| N4 transfer | Checked logical record envelope, fresh-target import/fork with atomically persisted source mapping; offline physical restore retaining identity | Coordinated hooks, not a general graph backup engine. No independently writable OS-level clones of one UUID |

`Database.enable_commit_history`, `begin`/`transaction(metadata=...)`,
`Database`/`Transaction.lookup_commit` and `commit_history` are documented in the
consumer guide and generated reference. `prepare_commit_import` produces bounded
metadata and a checked source/target mapping. Large metadata and nested transfer
overhead must fit explicit limits; nothing is silently truncated.

History reads add no global reader lock. Each bounded observation revalidates
published sequence, qualified page stamps, head and extents. A moving publication
refuses retryably after four attempts; stable corruption does not become an empty
history. Full verification scans the whole journal, not just its newest record.

## Regression evidence and corrections

The broad source regression executed **15,129 passes, 79 failures and 18 declared
skips in 1,501.22 s**. This is recorded as a failing run, not relabelled green.
All failure groups were investigated; affected groups are rerun after correction.
Overlapping counts below must not be added as distinct tests.

| Validation | Result / evidence |
| --- | --- |
| Public boundaries, terminal lifecycle, foundation/export contracts, documentation/language/import/platform checks, observability/dashboards, packaging and performance instruments | **5,013 passed, 338.78 s**; `.grafx-tmp/v005-repair-regression-2.txt` |
| Native lock descriptor identity, including real Windows unlink refusal | **30 passed, 1 POSIX-only skip, 0.41 s**; `.grafx-tmp/v005-coordination-final.txt` |
| Regenerated corpus closure | **45 passed, 223.05 s**, including exact regeneration and hostile freeze checks. Two count-pinning tests had loaded their old assertions before the edit; both passed in the post-edit rerun (**2 passed, 0.55 s**). Receipts: `.grafx-tmp/v005-corpus-final.txt`, `.grafx-tmp/v005-corpus-counts-final.txt` |
| Public history/import, two independent writer processes plus pinned reader, metadata retry, large cross-page metadata, compression/roll, native publication fault cuts and corpus inventory | **81 passed, 1 corpus-drift failure, 331.65 s**; `.grafx-tmp/v005-final-functional.txt`. All selected native functional tests passed; corpus closure is tracked below |
| Earlier focused publication/activation/history/transfer/crash closure | **50 passed, 29.00 s**; `.grafx-tmp/v005-history-final.txt` |
| Actual wheel built and installed in a separate validation environment | PASS: public metadata/history, schema/data commit, checkpoint, read-only reopen and qualified logical transfer; `.grafx-tmp/v005-wheel-smoke.txt` |
| Pulse Community adapter regression against Grafx source | Final transaction/provider, read-lane and orchestrator group: **105 passed, 37.90 s**; `.grafx-tmp/v005-pulse-adapter-closure-2.txt`. Earlier focused group: 31 passed, 18.10 s |
| Complete synthetic Pulse consolidation | PASS: **4 nodes, 4 edges, 4 relational references, 4 discovery digests, 1 outbox ACK** and empty second tick; `.grafx-tmp/v005-pulse-consolidation-validation.txt` |
| Synthetic Pulse HTTP pagination/global checks | PASS on an isolated **1,100-node** fixture; `.grafx-tmp/v005-pulse-pagination-validation.txt` |
| Static/docs | Changed Python files pass Ruff; generated API freshness and documentation checks pass. Focused new modules pass strict mypy; expanded engine check retains the same **89 pre-existing diagnostics**, not a globally clean typing claim |

Corrections made rather than excluding tests:

- Preserve the existing no-metadata `begin`/retry call shape for lifecycle hooks.
  Metadata admission also refreshes the validated activation capability after
  checkpoint, so first use does not incorrectly refuse a stale local capability.
- Declare module/public exports, postponed annotations and missing operation
  documentation, including earlier-phase modules found by the broad checks.
- Register the three new provenance metrics in the contract, pinned catalog and
  dashboard; cover the existing `snapshot_reclaimed` error label. The server
  privacy test now inspects HTTP headers, not legitimate metric HELP text.
- Preserve the safer package selector `okto_grafx` plus `okto_grafx.*`, and pin the
  intentional new public names in the clean-interpreter contract.
- Forward the existing `landing` keyword through the Pulse card measurement
  wrapper. Performance-series tests use a real clean isolated Git pin instead of
  requiring the developer's working source to be clean; production provenance
  and dirty-source refusal checks are not removed.
- Use Pulse checkouts whose HEAD equals each required corpus pin. Regeneration
  then exposed stale evidence from earlier chained OPTIONAL MATCH support:
  I64 now plans, and one wider unsupported chain reports a more specific refusal.
  Review retained **97 entries**, identical Pulse sources, row/column contracts
  and raw-contract admission. Classifications become **83 supported / 12 gaps**.
  These are parser/planner classifications of historical templates, not a claim
  that all twelve are current Pulse runtime failures.

Some setup attempts failed collection (missing/wrong corpus pin, wrong Pulse Core
checkout, or colliding bare `conftest` imports in a mixed focused invocation).
They are not counted as runtime coverage. Coordination was rerun separately;
the full regression had already collected that module correctly.

## Cost and reproduction

Latest bounded sample using the public activation/history APIs:
**24.233 ms median** for a journaled single-row update, 512-byte pages,
16 measured iterations after two warmups, alternating with an untracked control.
The raw receipt preserves both samples; this document reports only the latest
journaled result, not a before/after headline. No latency threshold was applied.
This is added durability/provenance work, not a promised speedup or a live Pulse
consolidation measurement.

The host was also running regression work during the latest sample; it is not an
idle-machine performance baseline or evidence of a timing regression.

```powershell
$env:PYTHONPATH = 'D:/Projetos/Techridy/okto_grafx/src'
$env:PULSE_COMMUNITY_BASELINE = 'D:/Projetos/Techridy/okto-pulse-grafx-mpulse1'
$env:PULSE_CORE_BASELINE = 'D:/Projetos/Techridy/okto-pulse-core-corpus-baseline'
python -m pytest -q -o addopts='--strict-markers --timeout=60 --timeout-method=thread'
python tools/perf_round/round005_commit_journal.py
python tools/generate_api_reference.py --check
python tools/check_documentation.py
```

Corpus pins: Community `befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595`, Core
`f602c7cc2f6a9f5ef446d4c991309196bd4667c7`. Current Pulse integration tests instead
use Community `okto-pulse-v003-kg-load-codex` and Core `okto-pulse-core-kg5-codex`;
these are intentionally different from the historical corpus checkouts.

Source-tree receipt: SHA-256
`a4b888e03d81eda0a699f564a54223b3545c22a876f481ea538186442f05f750`.
Input is the sorted `src/**/*.py` list, each line `forward/slash/path` plus a
space and its lowercase SHA-256, joined by LF without a final LF. Test logs/raw
receipts under `.grafx-tmp` are local evidence, not distributed package contents.

No production vacuum, history activation, backfill or cognitive consolidation
was run. The **19 reserved production specs remain untouched**. The global Pulse
installation was not replaced during this task. No commit, push, tag, merge or
release was performed. Windows/Python 3.13 was exercised locally; other supported
OS/Python combinations and production deployment remain distinct checks.
