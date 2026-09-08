# SPEC-GX-CAP-0 — Database-first boundaries and contracts

Status: ten contract/enforcement deliverables implemented and locally validated;
not a release or completion of downstream capabilities.
Branch: feature/gx-cap-0, based on feature/v0.0.4@61fc44d.
Date: 2026-09-08.

## Authority and finite scope

This implements the ten GX-CAP-0 deliverables in complementary plan §17.
It does not reopen M-PULSE-0..7, replace the ongoing performance record or certify
GX-CAP-1..11 / GX-AGENT-0..1 as implemented. Both source roadmaps remain required
in full under the main plan's governance decision. No additional spec consolidation
is authorized or performed here; 20 reserved specs remain outside this milestone.

## Deliverable mapping

| Requirement | Artifact |
|---|---|
| 1. Database-first / feature ownership | [ADR GX-001](../architecture/ADR-GX-001-DATABASE-FIRST.md) |
| 2. Temporal semantics | [ADR GX-002](../architecture/ADR-GX-002-TEMPORAL.md) |
| 3. CommitId | [ADR GX-003](../architecture/ADR-GX-003-COMMIT-IDENTITY.md) |
| 4. Catalogs / single-store transactions | [ADR GX-004](../architecture/ADR-GX-004-ATTACHED-CATALOGS.md) |
| 5. FTS / hybrid semantics | [ADR GX-005](../architecture/ADR-GX-005-SEARCH.md) |
| 6. Extension trust | [ADR GX-006](../architecture/ADR-GX-006-EXTENSION-TRUST.md) |
| 7. Package/import graph | ADR GX-001; all-core optional-package source gate |
| 8. Manifest strategy | [Capability strategy](CAPABILITY_MANIFEST_STRATEGY.md) |
| 9. Existing-plan ownership | Formal list below; no duplicate implementations |
| 10. Separate milestone specifications | [Capability index](CAPABILITY_MILESTONES.md) |

Each downstream spec routes to all source requirements, names owning modules,
error categories, persistence/crash prerequisites and acceptance criteria.
Public API examples remain proposals until implemented and verified.

## Formal exclusions owned by the existing evolution plan

Complementary §2.1 is incorporated without removal. These are dependencies, not
second implementations or requirements waived by GX-CAP-0:

- M0 recovery, durability, bootstrap, read-only and concurrent HNSW publication.
- M1 honest configuration, budgets, public typing, health and maintenance.
- Identity-range leasing.
- Global-lock, os.walk, stat/fstat, metadata-amplification and publication costs.
- Genuinely sublinear HNSW access paths.
- Aggregation, top-N and traversal landing lookups.
- Bulk ingest, executemany, batching and group commit.
- Physical vacuum and MVCC bloat reporting.
- Consistent backup/restore.
- Logical export/import and physical-format migration.
- Generic secondary/composite/range/prefix indexes, B+tree, statistics, cost model,
  EXPLAIN and PROFILE.
- Streaming cursors and prepared statements.
- Already enumerated Cypher operations and graph capabilities.
- Logical changefeed.
- Synchronization, replication and distributed conflict policy.
- Encryption at rest.
- Full Pulse compatibility and migration.

Do not infer those items are all complete merely because ownership is assigned.
The finite performance plan retains the layout fan-out, cold latency and full-write
attribution debts. No new speed threshold or live benchmark is added here.

## Implemented gate and concrete packaging correction

The old setuptools selector `okto_grafx*` also matches sibling product import roots.
It is narrowed to `okto_grafx` and `okto_grafx.*`. The real-wheel fixture now stages
five sibling product packages so exclusion is tested, not vacuously asserted.
The fixture omits the private .grafx-tmp runtime/benchmark corpus from its copy;
it still contains actual src, tests, tools and docs, plus injected siblings.

The new source gate scans the complete core wheel, not just domain/engine, and
tests dependency reversals via imports, relative from-imports, type-only imports
and named/literal dynamic imports. Trusted arbitrary dynamic Python is not sandboxed.
The existing stricter pure-core gate is unchanged.
Actual optional-package source/typing/consumer gates are required when those wheels
exist; synthetic examples do not certify implementations that do not exist.

## Invariants, APIs, errors, persisted effects

No public runtime API, error class, on-disk bytes, WAL effect, default budget,
transaction protocol or query result changes. No Pulse restart/deployment is needed.
Packaging and static architectural enforcement are the only executable changes.
A violated build/source dependency fails a test/build gate, not a runtime query.
Recovery/crash matrices are not applicable to this non-persisting delta; future
persisting milestones must define them before code.

## Execution evidence

Final grouped command:

```text
python -m pytest tests/test_optional_package_boundary.py tests/test_import_boundary.py
  tests/foundation/test_packaging.py tests/storage_core/test_catalog_v2.py
  tests/index/test_ordered_format_discrimination.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

Result: **296 passed in 14.98 s**. This includes a real wheel built with five
sentinel sibling packages, exact core-only artifact inventory, no mandatory runtime
dependencies, packaging discovery against the historical selector, complete pure-
core/all-core import gates and existing catalog capability refusal tests.
The first source group (232/8.08 s) and intermediate group (294/16.67 s) overlap;
they are not additional totals. Final adversarial cases reject importing private
composition through `okto_grafx.api`, not just through `engine` or `adapters`.

Ruff passes on both changed test modules; diff-check is clean. All relative links
in the 22 new contract/specification documents resolve. The review was performed
by Codex alone per the current user instruction, not an independent Claude audit.
This is proportional packaging/architecture regression, not a full engine suite,
new performance measurement or completed matrix for future persisted capabilities.
No live Pulse operation, restart, consolidation or deployment was performed.
