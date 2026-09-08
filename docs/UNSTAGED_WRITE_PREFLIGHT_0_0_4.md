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
The source change is not yet installed. Twenty pending specs remain reserved;
no live event, consolidation, redrive, rebuild or reset was issued.
