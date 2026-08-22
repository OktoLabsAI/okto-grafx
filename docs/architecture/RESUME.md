# RESUME — state at the session-limit kill (2026-08-22, resets 03:00 America/Sao_Paulo)

Tree verified after the kill: no `.battery-root`, no journal, no `mutations.json` anywhere under the
shared tree; the only live Python processes were the user's own services. **Full suite exit 0.**
Nothing to repair. §13.1 governs every round below: fix the blockers, one test each, punch-list the
rest, round cap one.

## Killed mid-work — resume with these assignments

| agent | state at kill | assignment on resume |
|---|---|---|
| **C4 WAL r3** | both blockers reproduced; surveying `open()` before the D5 fix | BD-1 `removed_records` negative; BD-2 segment-name reuse (CF-6 returning, BR-4 violated); **plus D5 item: index the WAL by LSN** (see below — the profile makes this bigger than the replay fix) |
| **C5 txn** | 186/0/0/0; battery over the new update/delete probes | `stage_row_update` / `stage_row_delete`; sign-off re-confirmation (round-1 predates the E1 rework); **plus D5 items 3/4/5** |
| **C6 recovery r3** | main battery 21/23 killed, files pristine; `verifier.py` re-pass pending | B1 unwritten page reported as `page_checksum`; B2 retirement on six non-damage classes |
| **C7 index r2** | caught its own A80.1 (background battery mutating the fork its foreground runs read) | `mark_stale` durability only |
| **C8 observability** | punch list recorded; battery near done | blocking-only close, then ship |
| **C11 public API** | (killed with no partial) | nothing owed — both blockers closed and reported before the kill |
| **C12 CLI** | 20/42 killed, 2 of 6 determinism runs | first delivery; CLI must demonstrate the `db.begin("write")` door |
| **C13 bench/CI r2** | drafting punch list | B1 dotted-prefix coverage hole; B2 ci.yml discarding C0's gate |

## Landed while they ran

- **C9 vector: ready for blind review.** 301 tests x 5 identical runs, battery 34 mutations 21 killed /
  13 survivors, none claimed equivalent. R20 (a deleted row rankable in the approximate regime) killed.
- **C10 query engine: REJECTED, 3 blockers.** Relationship `MERGE` never matches; node `MERGE` blind to
  rows the same transaction staged; a `LIMIT` above a write silently drops writes. All three share two
  roots: staged rows are invisible to `_matching_row`, and `LimitRows` sits above a lazy write operator.
- **D5 commit ceiling: MEASURED, do not amend.** See COMPONENTS.md — the cost is a missing WAL LSN
  index (quadratic), a pure-Python CRC-32C at 1.3 MiB/s, and four control-file publications per commit.
  fsync is 0.2%. Group commit explicitly rejected at ~1.002x.

## Next after the resumed rounds

C9 and C10 blind reviews; C1's catalog staging door (E2) restarted; CF-11 (BR-10 reclamation has no
caller in `src/`, so the log grows without bound) routed to C5/C11.
