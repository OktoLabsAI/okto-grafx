# RF12 capacity qualification — pending campaigns

This is the decision record for RF12 N2. No temporal result has been entered, and
no local tuning or default change is qualified by this file. Raw campaigns must
be produced by the calibrated RF01 executor on immutable baseline and candidate
revisions. Preserve each raw JSON file and its SHA-256 alongside the generated
campaign report. Run each partition independently; the repeated reference must
be measured again within that partition.

| Partition | Required canonical scenarios | Controlled question | Raw SHA-256 | Report decision |
| --- | --- | --- | --- | --- |
| Q1 | `rf12-{retained,released,external}-buf64-cp512-lease64-batch32` | Reader ownership and retention; semantically different states | pending | pending |
| Q2 | `rf12-released-buf{16,64,256}-cp512-lease64-batch32` | Buffer budget, with equal work | pending | pending |
| Q3 | `rf12-released-buf64-cp{128,512,2048}-lease64-batch32` | Observed read effect across checkpoint intervals | pending | pending |
| Q4 | `rf12-external-buf64-cp512-lease{1,32,64,128,512}-batch32` | Lease size with 32 rows in every arm | pending | pending |
| Q5 | `rf12-external-total512-lease64-txbatch{1,32,128,512}` | Transaction size with 512 total rows in every arm | pending | pending |

The expressions expand to 18 scenario participations and 15 distinct names.
The other six catalog scenarios describe load scaling or lease/batch interactions;
they cannot substitute for a controlled partition. The qualified campaign parser
rejects partial partitions, changed configurations, unrelated families, an
uncalibrated executor, or a CPU ceiling above 20%. The aggregate
`capacity_qualification.qualification_report()` recomposes all five raw campaigns
and refuses reused process invocation IDs. Its output is a local review record,
never a new default, vacuum authorization, or rehash authorization.

## Evidence to enter for every partition

- Source revisions and clean-worktree proofs for both arms; format, dependency
  versions, seed, full scenario configuration, query and contract hashes.
- Three process pairs in AB/BA/AB order, five warmups and twenty retained
  samples per scenario and arm, CPU calibration and observed background load.
- Raw ordered result hash and count, snapshot oracle, errors, logical-memory
  peak under the 1 MiB query quota, process RSS and buffer-pool budget as
  separate quantities, physical work, WAL/disk allocated deltas.
- For every external writer: durable ACK by atomic unit, offsets and row counts,
  post-checkpoint read-only reopen and verification. Preserve first-row and
  full-consumption timing separately from fixture, writer and checkpoint work.
- Local objective, observed benefit, cost, dispersion, confounders, chosen
  decision (maintain, local adjustment for later review, or new research), and
  rollback criterion. A failed semantic gate or inconclusive partition leaves
  the setting unchanged. Only one complete replay of an inconclusive partition
  is allowed, linked to its previous raw campaign.

Q1 does not isolate a reader's latency contribution. Q2 does not equate pool
budget with RSS. Q3's read interval does not measure isolated checkpoint cost or
automatic checkpoint cadence. Q4 preserves work while changing the ID lease; Q5
preserves total work while changing transaction size and ACK count. The separate
functional proof must verify foreign-writer files, barrier, retained snapshot and
read-only reopen for checkpoint intervals 128, 512 and 2048; it cannot replace
Q3. HASH skew has only bounded, read-only diagnosis here: rehash or a new index
requires its own investigation and specification. `horizon_eligible` never
establishes `vacuum_safe`; this record cannot authorize pruning or closing a
legitimate reader.
