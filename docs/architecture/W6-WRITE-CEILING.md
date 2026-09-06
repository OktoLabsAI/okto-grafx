# W6 decision record — the write ceiling and the three-plus-one options

Status: **historical decision record; option 1 and the Windows publication complement are
delivered.** Measured premises were verified 2026-08-23 at `903d611`; instruments
`tools/measure_concurrency.py` and the D5 harness. Identity leasing landed in `40b2b43`. The CE-1
two-slot control protocol landed in `1512199`, `93a3ee3` and `98e52dd`, replacing the hot
temp-file/rename publication path. The remaining group-commit proposal was subsequently measured
at approximately `1.002x` and rejected. Options 2 and 3 remain unselected.

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

**Implementation outcome:** delivered in `40b2b43` after the separately reviewed V7 state
machine. The bullets below preserve the pre-implementation risk analysis that led to that design;
their words "pending" and "blocked" are historical, not current status.

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

1. **Windows publication — CLOSED by CE-1.** `commit.state`, writer lease and reader records now
   publish in place through three-page/two-slot records plus `write_page` and
   `durable_barrier`. `atomic_replace` remains only on bootstrap/downgrade paths. A 2026-09-05
   Pulse-shaped profile on the `0.0.2` line observed zero `nt.replace` calls per ordinary commit;
   commit-state and lease publication together were approximately 1% of commit time. The old
   16.5 ms rename denominator is therefore not a remaining target.
2. **Group commit — REJECTED after measurement.** The WAL barrier represented too little of the
   contemporary commit cost and the measured ceiling was approximately `1.002x`. Batching ready
   transactions would still amend §8.5 and is not justified by that evidence.

## Recommended sequence

The executed sequence is **identity leasing → CE-1 two-slot publication → group commit measured
and rejected**. Per-table directory pages remain conditional on real table-disjoint contention;
mergeable directory records remain rejected. This line is closed unless new evidence changes the
denominator materially; the historical estimates above must not be reintroduced as current work.
