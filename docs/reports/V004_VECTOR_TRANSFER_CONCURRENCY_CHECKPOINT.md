> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# 0.0.4 — vector, transfer and concurrency checkpoint

Date: 2026-09-07. Native source: `1b53f57`, `feature/v0.0.4`.
Execution continues without Claude. No cognitive spec was consolidated, no live
generation was rebound, and no Pulse rebuild/redrive was dispatched.

This is the bounded complement to Wave 3 item 4, not a performance threshold or
a declaration that every item in the 0.0.4 plan is complete. Earlier page,
relationship, WAL and crash evidence remains in the round plan; it was not
rerun after each small change.

## Isolated synthetic checkpoint

Community reproducer: `tools/grafx_v004_checkpoint.py`, using a NEW output path.
Completed run: Grafx `.grafx-tmp/checkpoint-v004-20260907-b` (exit 0).
128 nodes, 256 relations including parallel duplicates, 384 properties and
128 deterministic 384-dimensional vectors. Logical fingerprint:
`d20d7f167184f9a4ee11c7bb57f36f9bb62b8c2ff03b470efe58298fce2e2cd3`.

| Operation | Observation |
|---|---|
| Exact vector searches, three observations | 59.62 / 16.38 / 15.52 ms |
| ANN searches, three observations | 525.29 / 8.50 / 8.35 ms |
| Backup, including artifact verification | 358.64 ms; 1,176,019 bytes |
| Restore including cold certification | 2.190 s |
| Two readers and two writers | 16 acknowledged inserts; 1.457 s total |
| Writable recovery open / explicit checkpoint | 202.56 / 95.81 ms |

Exact and ANN returned stable identities/scores across their repetitions; the
top-five sets matched in this tiny fixture (observed recall@5 = 1). This is not
a general recall guarantee, an old-version A/B, or justification for changing
vector defaults. Cold ANN cost is explicitly visible.

Independent readers retained 128-row snapshots before, during and after writer
commits. Two independent writers each acknowledged eight inserts, with zero
observed conflicts. Cold validation found exactly 144 rows, no missing or
duplicated acknowledged identity, and clean `verify("all")`. This short run
does not claim heavy contention coverage. Existing vector thread/process and
read-your-own-writes tests separately passed: **3 tests, 3.54 s**.

The initial run-a harness attempted a read-only cold open before checkpointing
acknowledged WAL and was correctly refused by the native safety contract.
The corrected harness measures normal writable recovery and checkpoint before
its final read-only validation. No native contract was weakened; run-a is not
reported as an engine corruption or a passing run.

## Full real-board transfer

Community reproducer: `tools/grafx_board_transfer_checkpoint.py`.
Source was opened **read-only** and closed after publishing the verified backup.
Candidate remained isolated and unbound at Grafx
`.grafx-tmp/real-board-transfer-v004-20260907-a/restored`.

Source open: **1.206 s**. Verified logical backup: **12.366 s**, 36,151,066 bytes.
Counts: **2,961 nodes, 4,424 relations, 161,213 properties, 2,960 vectors**.
The logical count includes BoardMeta; do not compare it unqualified to UI totals.

The original restore process had exited when execution context was recovered;
its terminal output and exit status were unavailable. Consequently neither its
total duration nor its original terminal certification is claimed here.
Instead, `tools/grafx_verify_transfer_checkpoint.py` independently reopened the
existing candidate read-only, performed full physical verification, exported its
complete logical contents and compared scope, counts, schema digest and logical
fingerprint against the fully verified original artifact. No reimport or live
source access was needed for this reconciliation.

Reconciliation passed: cold open **1.575 s**, clean `verify("all")` **5.299 s**,
complete logical export/comparison **15.222 s**. Fingerprint for both copies:
`8bd21c793b3af31dfe48233fc0410f4e3e11ac891a8885f6c9a700443eaed6f5`.
Schema digest:
`cde8b536644269693c3a5717454c970a2427a902860ca6035908a5263d80bd59`.
Machine-readable evidence is retained in the isolated checkpoint directory at
`cold-reconciliation/report.json`; artifacts contain board data and are not
committed to the repository.

## Preservation and remaining scope

Cognitive ledger SHA256 remains
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`:
the 21 reserved pending specs were not consumed. Pulse PID 35824 remained running.
All three checkpoint tools passed Ruff and are versioned in Community commit
`6e369c5`; they are not claimed packaged or published with Grafx.

No WAL/OCC/durability, snapshot, multi-reader/writer, schema or vector-default
policy was changed by this checkpoint. The incident-layout query fan-out,
explicit decision queue and older DLQ/Health liabilities are not marked fixed.
