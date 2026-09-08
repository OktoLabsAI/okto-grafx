# Documentation refactor — September 8, 2026

[Documentation index](../README.md) · [Roadmap](../../ROADMAP.md)

## Scope and authority

Documentation-only product refactor over
`485ea074bf2b9e174200b44bb74c7adb54c1ecab` (`feature/gx-cap-1`, source 0.0.4).
No graph/storage/transaction behavior, installed Pulse, live data, package version,
branch, PyPI release, tag or production process was changed. The one production
Python edit corrects CLI **help text** about DDL rollback and durable commit errors.

## Delivered structure

- README: concise product entry point, truthful concurrency/recovery boundaries,
  installation identity, measured-performance table and direct roadmap link.
- Consumer guides: getting started, integration/services/workers/agents, API/DTO
  reference, all 36 configuration fields, query subset/types/functions, indexes/
  vectors, operations/recovery/upgrade/error outcomes and complete command inventory.
- `ROADMAP.md`: only active queue, separating completed checkpoints, partial
  capabilities, known limitations/corrective work, finite performance residuals,
  optional Agent/MCP product and deferred/rejected directions.
- Eleven former active/legacy plans consolidated into one immutable archive:
  **1,383,247 UTF-8 bytes of normalized source**, with original paths, baseline
  revision, hashes and line counts. Only Markdown link destinations were rebased.
  Original files removed from their former locations; every source remains
  recoverable in the archive and Git. No source requirement was discarded.
- Twenty-four report/history documents relocated to `docs/reports/`, with a
  navigable index and explicit historical/measurement boundaries. The previous
  README is preserved as superseded evidence, not a current integration guide.
- Spec source links now route directly to the appropriate archived full
  requirements; current status/priority routes to the single roadmap.
- Contributor workflow, feature template and CI documentation checks updated.
  Existing adapter example coverage was relocated, not dropped.

The unrelated untracked `FABLE_SHARDING_GRAFX.md`, runtime screenshot, release
artifacts, browser artifacts and `uv.lock` were preserved. The sharding survey
was not silently promoted to an approved implementation plan.

## Important corrections validated

- Different rows do not guarantee nonconflicting physical page writes; publication
  remains exclusive while transactions/participants can legitimately overlap.
- Exceptions after the durable barrier must not trigger duplicate mutations.
- Read-only **open** requires checkpoint-complete state, distinct from a read-only
  **query transaction** on a normally admitted handle. The first draft's async
  example exposed this through a failing consumer test; the guide now states the
  condition, and a dedicated refusal/checkpoint/reopen test pins it.
- Repeated async cancellation retains the execution slot until worker I/O settles;
  it does not pretend that cancelling an await interrupted a disk operation.
- Ordered indexes, correlated OPTIONAL/WITH aggregation, public RecordIdFilter,
  fetchone/fetchmany, close_complete and operational inventories are documented.
- Explicit checksum `native`, process-global selection, persisted versus per-open
  options, strict/generation prerequisites and actual budget meanings are explained.
- Latest recorded Pulse performance is `0.0.4@fa8f188`, not the later CAP-1 recovery
  candidate. Native, MCP, outbox and UI boundaries are not conflated.

## Validation

Local Windows / Python 3.13; no long benchmark or additional live consolidation.

| Check | Result |
| --- | --- |
| Consumer docs + original adapter examples + full CLI suite | **458 passed, 49.91 s** |
| Final consumer/adapter slice, including added repeated-cancellation test | **15 passed, 2.89 s**; overlaps the preceding run, not additive coverage |
| Offline docs checker | PASS: consumer links/anchors, exact 36-field config inventory, generated public signatures/DTOs, all 11 archive boundaries/hashes/line counts |
| Independent archive comparison against baseline Git objects | All eleven bodies equal originals after normalizing only Markdown link destinations; original hashes also match |
| All non-archive Markdown path links, including reports/specs | No broken local target paths found |
| Ruff on changed Python surfaces | PASS |
| `git diff --check` | PASS; Windows line-ending notices only |

The environment emitted an existing pytest-asyncio fixture-loop-scope deprecation
warning; no tests were waived for it. No claim of a new full-engine, cross-platform
or release-binary certification is made by these documentation checks.

## Ongoing checks

`tools/generate_api_reference.py --check` detects public signature/DTO drift.
`tools/check_documentation.py` checks links, field coverage and archive preservation
without opening a database. Both are wired into the lint CI job; actual remote CI
execution remains a later push/PR event. Consumer tests execute the documented
recipes against memory/temp databases. Future edits update the authored semantics
as well as the generated appendix; name coverage alone is not semantic correctness.
