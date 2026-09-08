# Architecture boundary debt: query synchronization and pure algorithms

2026-09-08, `feature/v0.0.4`. This addresses the nine findings already recorded
in `RETAINED_METRIC_SAMPLING_0_0_4.md`; it is not a new performance acceptance gate.
The complete architecture gate remains **failing**, with five occurrences below.

## Corrected engine synchronization placement

The query engine directly imported `threading.Lock` and `_thread.LockType` for
compiled-predicate publication and lazy result-plan materialization. G2 requires
mechanism creation in the composition, not in domain/engine code.

- `api.assembly` now supplies the dedicated compiled-predicate lock and a factory
  producing one independent lock per lazy public result.
- `QueryEngine` accepts a context-manager guard. Its lookup/publication/eviction
  sections are unchanged; compilation, evaluation and I/O remain outside them.
  Manual compositions omitting this dedicated guard reuse their existing injected
  endpoint guard. The standard API still supplies separate locks, not one global
  lock or a newly serialized reader/writer path.
- `Database` passes the plan-guard factory through the existing public snapshot
  boundary. `_OwnedPlanDoor` consumes a supplied guard instead of creating one.
  The API still clones each result's plan only on first access, exactly once under
  concurrent readers, independently of other results and after database close.
- Manual pure compositions without a plan-guard factory detach plans eagerly;
  they do not publish an unsynchronized lazy door. The result/plan contents and
  public `connect` configuration are unchanged. A supplied factory failure is
  normalized at the existing result boundary, not ignored.

No WAL, OCC, snapshot, on-disk format, query semantics or owner cache budget changed.

## Reconciled pure algorithm classification

The `heapq` imports in HNSW and the query engine implement deterministic heap
operations over caller-owned lists. They do not obtain locks, I/O, clocks, entropy
or process state. This meets the existing G2 purity criterion just as the already
accepted `bisect`, `math` and `itertools` do. The static test allowlist was missing
this algorithm module; it now admits `heapq` with an explicit rationale.

This is not an exception for host mechanism. `threading`, `_thread`, `contextvars`,
`weakref` and `inspect` remain excluded, with explicit negative assertions.
Synthetic checker cases accept domain/engine heap algorithms, reject an unrelated
`heapq_network` prefix and still reject a mixed heap-algorithm/thread-lock import.
HNSW implementation, scores, tie-breaking and asymptotic cost were not replaced
with a slower handwritten algorithm to satisfy an obsolete module list.
The frozen architecture contract was not changed.

## Public value-graph test synchronization

The broader boundary slice found two additional failures because its exact enum
list omitted the existing public `IndexLayout` (`HASH`, `ORDERED`). Both failures
were reproduced against the installed **a82d3bf** wheel, before these source edits;
they are not a new capability leak caused by guard injection. The test now includes
only that exact domain enum. A new negative case proves a foreign enum with the
same spelling is still rejected; the rule was not widened to arbitrary enums.

## Evidence

- Initial lazy-plan slice: all eight existing tests passed.
- Guard-factory/fallback tests plus compiled-predicate concurrency, cursor and
  public boundary concurrency: **65 passed in 3.98 s**.
- Combined query-result, compiled-predicate, cursor, public concurrency/query/
  operation/collaborator boundaries and the complete import gate:
  **610 passed, 5 failed in 10.80 s**. All five failures are the four unresolved
  modules plus the aggregate zero-budget assertion; they are not skipped/xfail.
- Ruff and `git diff --check` passed. These runs preserve strict markers and the
  existing 60-second per-test thread timeout; no hang guard was removed.

## Remaining existing architecture debt

| Module | Occurrences | Mechanism still to separate |
| --- | ---: | --- |
| domain/control_record.py | 1 | `vars` for explicitly declared fused-read capability selection |
| domain/model/schema.py | 2 | thread lock and weak-key lifetime registry for tuple-encoding proofs |
| engine/index_manager.py | 1 | context-local live-commit authority/projection state |
| engine/recovery_manager.py | 1 | static descriptor inspection for recovery capability selection |

These guards/proofs may not simply be removed: forwarded storage capabilities,
encoding-proof authenticity/lifetime, concurrent commit isolation and recovery
admission must retain their existing behavior when mechanism moves outward.
The original nine occurrences are now five, not zero. This document is a partial
correction checkpoint, not completion of the architecture/evolution objective.

Source-only; accumulate with the remaining corrections before the next deployment.
Pulse PID 34048 remains on installed a82d3bf, with Core 9e91ea9 code. No additional
spec consolidation, recovery, redrive, reset, live graph write or restart was done.
