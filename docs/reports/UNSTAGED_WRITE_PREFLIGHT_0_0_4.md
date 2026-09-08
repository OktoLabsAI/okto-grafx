> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Unstaged writer preflights and failed index-view proof

2026-09-08, `feature/v0.0.4`. Continues the existing full-write/Global attribution
task. No new performance gate, concurrency mode or native format.

## Bounded landing batches before owner staging

Community's `delete_invalid_board_digest_links` opens a write transaction, reads
all current Board-to-digest links, validates the complete expected set and only
then stages any required DELETE. Its read query was excluded from the existing
bounded destination batches solely because its transaction mode was WRITE.

`_admits_batched_landings` now also admits an exact native WRITE context whose
`wrote` property is false: no row intents, pending WAL records, physical page
images or write partitions. All existing plan and collaborator restrictions
remain: closed read-only scalar projection with a blocking consumer, at most
64 steps per batch, no streaming frontier expansion, unconsumed vectors still
fully validated, independent stable index/heap certificates, operational quotas
and custom witnesses retaining their scalar route.

Eligibility is checked per statement. DML or DDL staging disables this extension;
the existing owner overlay and memo invalidation handle subsequent writes and
full-vector reads. There is no transaction-wide authority certificate and no
change to either OCC, snapshot, writer fencing, WAL or durability.

One exact Community preflight query on a separate private Global copy:

| Unstaged write transaction | Rows | Index read views | Seconds |
| --- | ---: | ---: | ---: |
| Original scalar path | 2,253 | 2,255 | 3.624 |
| Existing batching, now eligible | 2,253 | 38 | 2.979 |

The second arm made 36 landing batches covering 2,253 identities. Ordered rows
and recorded read partitions were identical, neither transaction staged anything,
both rolled back and all payload retention was released. Read partitions were
empty in both arms; this is not a new promise of serializable read dependencies.
Both OCC checks and the existing snapshot-isolation conflict rules remain intact.
One sequential sample has no cache/order/host control and does not prove an
end-to-end speedup or resolve the previous 131.489 s live Global ACK.

## Correctness defect exposed by the concurrency test

With batching both enabled and forced off, a warmed long-lived writer could read
a newly published index bucket while its companion heap frame and carried
pre-certificate still named the preceding generation. A new slot then raised
`unsupported_operation` before `_stable_view` reached the fresh post-certificate.
The existing successful-result retry never ran on that failure path.

`IndexStore._stable_view` now completes the same post-proof for a `GrafxError`
raised by materialization. If the generation is unchanged, the exact original
exception and traceback are re-raised. Only a proven generation transition
retries, within the unchanged existing retry budget. Continuous churn produces
the existing retryable `index_view_changed`; a failed/unsafe post-proof still
refuses. Arbitrary host exceptions and process-control signals are not retried.
No corruption finding is suppressed under a stable certificate, and no successful
read loses any pre/post check.

The public race regression now reads its original snapshot after the foreign
publication, stages the conflicting change and reaches the expected write-conflict
refusal instead of a spurious slot error. Scalar and batched arms both preserve
the winning writer's value. This fixes a baseline correctness defect, not a
performance-only exception waiver.

## Quality checkpoint

The final grouped run passed **973 tests in 61.99 s**: the complete index suite,
unstaged writers, bounded and vector-free landings, pending relationship overlays,
public concurrency and full import-boundary gate. Strict markers and the
60-second thread timeout were enabled. Focused precursor: 31 passed in 6.64 s;
it overlaps the final run and is not an additional total. Ruff and diff checks
pass. Initial failures in the new hostile-test facade were corrected without
changing production API mutability; the two subsequent failures exposed the real
baseline slot/generation defect fixed above.

## Diagnostic boundaries

An earlier broad private reconcile run was deliberately stopped: its Board and
Global copies differed by more than the five newly authored candidates, causing
many unrelated private identity repairs. It is **not** an equivalent replay of
the live event, has no complete timing or clean-verification claim, and is not
an acceptance result. Only that profiler PID 10200 was terminated; its owning
session exited 1 and the partial private graph remains in
`.grafx-tmp/global-reconcile-phases-20260908` (requires normal recovery before
reuse). Pulse PID 15940 was untouched. The bounded preflight comparison uses a
different fresh private copy and made no graph writes.

Harness: `.grafx-tmp/compare_unstaged_writer_landings.py`.
Evidence: `.grafx-tmp/unstaged-writer-landings-20260908.json`.
SHA-256: `DF5DC19452C2F12F7DF57E8191908B6ADA7DBF14D660903715BEF3AEBAA27E36`.
Twenty pending specs remain reserved; no live event, consolidation, redrive,
rebuild or reset was issued.

## Accumulated deployment

Installed **Grafx 0.0.4@fa8f188**, wheel SHA-256
`86E0329E66297B641B797E7F834AD23059B9D29C966CD43CAE4934723AA001D3`.
The isolated wheel passed 22 focused tests in 6.80 s, with its import path
checked and repository pythonpath injection disabled. After verified graceful
shutdown of Pulse PID 15940 (owning terminal exit 1, PID absent and ports free),
the wheel replaced the Python 3.13 user installation. Changed installed module
hashes match source; NumPy 2.5.2 and google-crc32c 1.8.0 remain installed.

Pulse **0.3.3 PID 4212** serves 8100/8101, with Community `7158383`, Core
`0a38312` (including phase observations), and native Grafx from installed
site-packages, not native-source injection. HTTP root returned 200. The five
canonical Alternative/Decision rows and the complete 40-item cognitive ledger
are identical before/after: 20 pending, 20 consolidated, zero in progress,
failed or skipped. No new consolidation or delivery replay was used.

The initial cold Health response exposed unavailable metrics while refreshing;
the next response reported Board/Global healthy, metrics available, 2,965 nodes
and queue zero. Overall remains at_risk from the historical policy DLQ. Historical
DLQ and canonical debt were not reset or hidden. No end-to-end performance claim
is derived from these readiness checks. No main merge or PyPI publication.

Sanitized deployment evidence: `.grafx-tmp/deploy-fa8f188-live-evidence.json`,
SHA-256 `98FAA5C2CE13F703CF169E6C5E692E1C161301C526895FF8E0DED21CE054F9AB`.
