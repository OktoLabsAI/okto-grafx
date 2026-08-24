# W6 decision record — the write ceiling and the three-plus-one options

Status: **preferred direction; crash-safe leasing design pending.** Measured premises verified
2026-08-23 at `903d611`; instruments `tools/measure_concurrency.py` and the D5 harness. This record
does not authorize implementation until durable-before-use reservation and burn-only cursor
semantics have an independently reviewed state machine.

## The system truth that frames every option

Verified in `engine/txn_manager.py`: commit steps 3.1–3.7 run inside ONE exclusive cross-process
section. Even with zero conflicts, commits serialize there — the throughput ceiling is
~1/commit-cost regardless of any option below (~10/s on Windows at ~100 ms, ~70/s on Linux at
~14 ms). What heap page 0 adds ON TOP is retry waste and unfair tails: every insert spends
`next_record_id` in the page-0 directory entry, so all writers intersect, and OCC turns the queue
into a retry storm (measured: 254 conflicts and a 3.45 s p90 in a workload whose keys never touch;
three caller-backoff policies all pinned at ~10.8 rows/s). The options therefore buy back FAIRNESS
and WASTE, and unlock throughput only together with the complements at the end.

## The options, under the evolutionary-server lens (resilience first, performance second)

### Option 1 — identity-range leasing per participant  ← RECOMMENDED FIRST

A participant may allocate a reserved range from memory only after advancing `next_record_id` has
committed durably **before the first handout**. Its in-memory cursor is burn-only after handout;
only future durable reservations and chain-growth commits should need to touch page 0.

* **Pros, if the pending design proves them:** no on-disk format change (the counter advances in
  steps); crash gaps are already sanctioned ("an id burned ... leaves a GAP, which no reader can
  observe"); the verifier's invariant (`next_record_id` strictly greater than every physical
  record id) remains the acceptance oracle; N=1 can remain the operational fallback.
* **Cons:** page-0 conflicts become rare, not zero (renewal + chain growth, ~1 in 9–50 commits in
  the measured workload).
* **Resilience risk: DESIGN BLOCKED after the verifier prerequisite.** A same-attempt counter
  update and first row is insufficient because either dirty page may escape first. A kill after a
  durable reservation and before use may leave a sanctioned gap; after any handout, abort and
  uncertainty burn the identity and never rewind the cursor. Cross-process range ownership and
  fencing still require a proof.
* **Performance:** conflicts →~0 for disjoint ingest; tails collapse to the section's fair queue
  (~2–4× unit cost); unlocks ~70 commits/s on Linux; Windows stays ~10/s until the publication fix.
* **Surfaces to prove:** reservation durability, §8.5 interaction, cross-process ownership, reopen
  and identity density. None is frozen by this record.
* **Complexity / effort:** pending the separate state-machine design and critic. Required evidence
  includes multi-process uniqueness, abort/kill windows, reopen plus verifier checks, a rewind
  mutant, and before/after measurement via `tools/measure_concurrency.py`.

### Option 2 — per-table directory pages

* **Pros:** isolates tenants; table-per-client multi-tenancy scales across tables; aligns with a
  future per-table commit section.
* **Cons:** same-table writers STILL serialize — and a hot table is the typical server shape, so
  the marginal gain over option 1 is small today; multiplies the reserved-header invariants (A2)
  per table; **breaks the on-disk format** — bootstrap, recovery, verifier and `status` all change.
* **Resilience risk: MEDIUM** (format migration or break; rewrites in the zone where a mistake is
  data loss).
* **Performance:** parallelizes only ACROSS tables, and adds ~zero throughput while the global
  section stands.
* **Effort:** high; **3–5 rounds** plus a migration decision.

### Option 3 — mergeable (logical) directory records

* **Pros:** removes all false sharing, including extent hints; the theoretical maximum.
* **Cons:** forks the redo model — today's foundation is "the bytes on the page and the bytes in
  the log are identical; replaying twice changes nothing" through one apply door (A22). A second
  replay semantics with its own idempotence discipline is exactly the seam class where this phase
  found its worst defects (E3, CF-14).
* **Resilience risk: HIGH — which is the reason not to do it while data preservation is priority
  one.**
* **Performance:** indistinguishable from option 1 while the section stands.
* **Effort:** very high; **6+ rounds** with an extended recovery battery. Recommended disposition:
  record as REJECTED with this reasoning unless options 1+complements prove insufficient.

## The complements no option works without (server throughput)

1. **The Windows publication fix** (already in W6: 16.5 ms control-file publication vs 0.13 ms on
   Linux; `CreateFileW` on the source is 11.4 ms of it). This is the denominator: without it the
   ~10 commits/s ceiling stands whatever else is done.
2. **Group commit** (batch ready transactions into one barrier + one publication inside the
   section): the multiplier that turns 1/cost into batch/cost. For a server it buys more than
   options 2 and 3 combined. Also a §8.5 amendment — recorded here as **option 4, complementary**,
   to be sequenced after option 1 and the publication fix.

## Recommended sequence

**1 → measure → Windows publication fix → group commit → (2 only if table-per-tenant
materializes) → (3 rejected).** Each step is an amendment with the full builder + blind-critic
cycle, measured before/after with the in-tree instruments.
