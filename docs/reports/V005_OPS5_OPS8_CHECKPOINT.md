# 0.0.5 operational checkpoint: orphan files and read control

September 8, 2026. Baseline `c5402a9210e0c6b97fb509387464b8fc478fe106`,
branch `feature/v0.0.5`. This report supplies evidence for the operator-approved
**items 1–2 only**. [Roadmap](../../ROADMAP.md#operational-checkpoint-after-r1r4)
owns status and [consumer guide](../READ_CONTROL_AND_INDEX_CLEANUP.md) owns usage.

## Implemented boundary

1. `Maintenance.cleanup_indexes`: default preview plus explicitly quiescent
   removal of unreferenced native generation/displaced-artifact files. Complete
   candidate proof before deletion; catalog/registry, every catalog generation
   state, retained page/index WAL and historical catalog dependencies protected.
   Work/report limits, exact path spellings, deferred-deletion reporting and safe
   partial-prefix retry. Buffer retirement checks resident and doomed holders,
   discards orphan frames without writeback and retires allocation hints.
2. `CancellationToken`, per-call `timeout_seconds` and cancellation/deadline
   `GrafxQueryError` subclasses. Materialized read, explicit read transaction and
   cursor lifecycle coverage; no callback tokens, write-transaction controls,
   background watchdog or durable-commit interruption. Operator stream/counter
   checks are cooperative; independent writer/read participants remain independent.

No new persistent format, global engine cache, lock topology or connect option.
No production Pulse/spec consumption, global installation, release or work on
logical export/import (item 3) or FTS (item 4).

## Focused adversarial coverage

- Cancelled-before-begin admission, cancellation during scan/filter/sort/count/
  Cartesian work, cursor and materialized doors, and buffered fetch/iteration.
- Expiration during execution, consumer idle time and public result detachment;
  fresh deadline per reusable-query cursor; invalid/overflowing timeout values.
- Explicit read ownership remains with the caller; native engine and facade both
  refuse controlled write transactions before effects; later ordinary commits work.
- Real threaded cancellation and a separate participant committing while a
  controlled reader is paused. Real spill files are created and removed on refusal.
- Preview versus deletion, repeatability, live catalog generations after rehash,
  strict path eligibility, quiescence/read-only/open-transaction/budget refusal.
- Retained page/index and historical catalog WAL protection; malformed/unknown
  records and scan failure refuse before the first file is removed.
- Dirty-cache and retired-but-held-page cases; deferred platform deletion,
  KeyboardInterrupt after a removed prefix, and subprocess `os._exit(73)` after
  one reclamation followed by clean reopen/verify and retry.

## Validation record

Acceptance is closed against the final source, with every observed failure
rechecked successfully. Local receipts below are
ignored operational outputs, not required package inputs or remotely hosted CI.

| Group | Result / local receipt |
| --- | --- |
| New features + public surface/docs | 1,432 passed in 44.67 s, `.grafx-tmp/v005-ops5-ops8-final-focused.txt`; later final-result and native-write guards are additionally covered by the API/query run |
| Coordination | 562 passed, 3 platform skips in 23.58 s, `.grafx-tmp/v005-ops5-ops8-coordination.txt` |
| Error taxonomy | 59 passed in 0.30 s; new error codes are recorded in CONTRACT.md section 2 and its frozen-table test |
| Pulse Community adapter fixtures | 114 passed in 114.02 s, `.grafx-tmp/v005-ops5-ops8-pulse-adapters.txt` |
| Grouped native regression | Initial snapshot: 8,650 passed, 1 platform skip, 4 failures in 1,150.64 s; `.grafx-tmp/v005-ops5-ops8-regression.txt`. All four are closed by the rechecks described below. |
| Final API/query delta | 3,760 passed, 1 platform skip, 1 already-corrected maintenance-allowlist failure in 832.29 s, `.grafx-tmp/v005-ops5-ops8-final-api-query.txt`. The process had collected that test before its allowlist update; the six-case module and final four-case closure below are green. |
| Boundary recheck | 107 passed in 29.70 s, `.grafx-tmp/v005-ops5-ops8-boundary-delta.txt`; covers all new feature cases plus public cleanup/descriptor boundaries on final runtime code |
| Maintenance surface recheck | 6 passed in 0.70 s, after adding the new operation and its exact output type to the frozen public-method contract |
| Final closure of every original failing case | 4 passed in 0.37 s after both grouped processes finished, `.grafx-tmp/v005-ops5-ops8-closure.txt`; no observed failure remains open |
| Isolated wheel | Final-source wheel built and installed with `--no-index --no-deps` in the private validation venv. Read control/cleanup/reopen and prior overflow/backup/provenance smoke scripts both passed; all 156 packaged Python files matched source bytes. No source-tree imports (`python -I`) or global installation. |

The initial grouped process had loaded the implementation before a compatibility
adjustment: default autocommit must still delegate through `Transaction.execute`,
preserving its failure/descriptor cleanup behavior. Three injected-failure tests
identified that contract; all passed in the 107-case recheck after restoring the
default delegation. The fourth failure was the maintenance surface allowlist that
needed to include the newly approved method. Its six-case module passed after
adding the method **and** checking `IndexCleanupReport` as the exact return type.
No production data or durability failure was observed; failures are recorded here,
not counted as successful initial executions.

Counts overlap; do not sum them as unique tests. The current Pulse fixture pair is
`okto-pulse-v003-kg-load-codex` plus `okto-pulse-core-kg5-codex`, not the user's live
home. Ruff, targeted mypy for the two new modules, generated API freshness and
documentation link/config/DTO/archive checks pass. The Windows/Python 3.13 local
run is not certification of every supported OS/Python version or remote CI.

## Honest limits and expected benefit

Orphan cleanup can reduce unused disk allocation, but intentionally keeps
catalog-owned STALE/BUILDING files and conservatively retains generations while
historical catalog/unmapped logical WAL remains. It neither forces WAL rollover
nor implements online/catalog-generation GC. Quiescence is an operator assertion,
not a reader-TTL inference. A deletion failure may leave a safe removed prefix.

Read control improves caller control and abandonment behavior, not query throughput.
There is no hard wall-clock preemption of locks, I/O, parsing/planning or native
kernels, and no automatic idle-cursor resource release. Defaults do not poll the
deadline clock. No new timing ratio or performance gate is claimed for this round.
