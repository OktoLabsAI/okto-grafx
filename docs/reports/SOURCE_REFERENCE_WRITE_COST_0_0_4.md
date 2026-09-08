> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Source-reference indexes: isolated write attribution

## Scope — 2026-09-07

This follows the already selected cognitive-write/provenance investigation,
not a new throughput gate. No pending spec, cognitive session, repair, redrive,
rebuild or live graph write was triggered. Pulse stays on its existing runtime.

Two private writable copies were made from the previously SHA256-verified,
quiescent pre-source-index board backup. One retains its original indexes; the
other adds the 11 Community source indexes using normal fenced-capable bootstrap
policy. Each runs three synthetic transactions, each creating four Alternative
nodes (384-dimensional vectors) and eight edges: four Decision-to-Alternative
judgement links and four Alternative-to-Entity provenance links. Existing
endpoints are selected from the backup. These are native schema-valid fixtures,
not cognitively authored assertions or an end-to-end Pulse consolidation.

The public native API retains normal snapshot, OCC, WAL, commit and checkpoint
behavior. Each transaction resolves an Entity source root, stages the batch,
commits and checkpoints. The third commit is profiled and is excluded from the
uninstrumented timing comparison. Both copies use page size 8192, generation
descriptor revalidation and the native default buffer budget; metrics are noop.

## Uninstrumented observations

| Phase | Original indexes, samples 1 / 2 | With source indexes, samples 1 / 2 |
| --- | ---: | ---: |
| Source-reference resolution | 105.95 / 108.71 ms | 3.67 / 2.12 ms |
| Stage four nodes and eight edges | 30.44 / 18.91 ms | 35.34 / 20.98 ms |
| Durable commit | 49.36 / 51.95 ms | 56.91 / 51.02 ms |
| Explicit checkpoint | 761.55 / 763.03 ms | 726.21 / 652.82 ms |

Writable opening was 3.155 / 3.038 s. Additive index DDL took 6.627 s in the
indexed arm, outside the measured transactions. These small, baseline-first
samples do not control filesystem cache state, host activity or growing history;
they are not p95, a concurrency test or a general speedup percentage. They show
that the source seek gain remains visible inside a write transaction, while the
new index maintenance did not cause a large observed commit regression here.
They do not establish the cost of larger writes or repeated updates/deletes.

The checkpoint is materially longer than the commit in this fixture. This does
not authorize skipping, weakening or postponing a required durable checkpoint.
Pulse health/admission, embedding/reconciliation, full graph-source validation,
relational finalization and Global delivery are outside this harness. Therefore
these roughly 50 ms commits do not contradict the earlier 26.960 s full cognitive
call, and do not prove that full application write latency is resolved.

## Correctness evidence

Both private copies passed native `verify(all)` with zero findings using separate
read-only handles. Cold readback found all 12 synthetic nodes in each. Separate
read-only relationship queries found 12 judgement and 12 provenance edges per
copy, and both ordered endpoint-pair outputs matched exactly between arms.
No benchmark directory was deleted. Private artifacts remain under Grafx
`.grafx-tmp/source-write-n18i5kyd`, including per-arm commit profiles. The local
harness is `.grafx-tmp/source_index_write_cost.py`; it only copies the fixed
quiescent backup, and never opens the live board for writing.

An initial harness attempt incorrectly passed Python `datetime` instead of the
native immutable `Timestamp`. The public boundary correctly refused the value
before the synthetic transaction committed. The harness was corrected, not the
engine's value-domain contract.

## Instrumentation cost discovered, not yet corrected

A preceding OpenMetrics-enabled diagnostic produced 59.076 s writable opening,
1.894 / 1.186 s unprofiled commits, and 9.384 / 7.843 s checkpoints in its original-
index arm. A third profiled commit took 4.395 s; cumulative profiler evidence
attributes 3.626 s to 222 calls of `BufferPool._retained_bytes_estimate`.
Stack inspection also found this estimator on read admission during recovery
and full verification. These figures are instrumented observations, not normal
noop write performance. After the three transactions checkpointed, the expensive
instrumented verification was interrupted; it is not reported as a passed gate.
The subsequent noop run and independent read-only verifications completed.

`BufferPool._usage_reading` performs a whole retained-object traversal on each
reported residency-topology change. Filling N resident frames can therefore
perform quadratic aggregate diagnostic work. The estimator does not authorize
eviction or establish durability; nevertheless simply dropping or falsely caching
its value would change the documented telemetry contract. A bounded/coalesced
sampling design must preserve the explicit fresh `Database.pool` diagnostic,
callback containment, per-pool ownership, and unchanged nominal admission.
This is a measured complexity defect in the existing attribution path, not a
reason to disable integrity checks or introduce a new performance acceptance gate.
No telemetry setting was changed in the live Pulse, and this experiment does
not prove its current request latency is caused by metrics.

## Adjacent provenance preparation fix

Core commit `e1e9d08`: `primitives._auto_attach_provenance_edges` and
`_inherit_supersede_provenance_edges` repeatedly scanned the growing candidate
edge list once per node to find an outgoing `belongs_to`. Both now build a local
source-id set once per phase, updated on generated edges. The second phase
rebuilds it, so edges added by the first remain visible. Only candidate-batch
membership is indexed: graph source/identity queries, self-loop checks, curated
override, supersedence and connectivity validation are unchanged. No graph read
or authority result is cached, and Core remains backend-agnostic.

Ordinary batches change membership preparation from O(C*E + C²) to O(C+E), using
O(E) local space, without claiming the whole consolidation becomes linear. If an
inherited generated edge ID replaces an incumbent, membership is rebuilt so a
displaced edge cannot leave stale provenance credit for another candidate. That
exceptional collision fallback is excluded from the linear-work claim. A structural
250-candidate fixture visits 250 input edges once in the first phase and 500
resulting edges once in the second. Every root query still executes, including a
second-candidate failure for the same source reference. Three differential cases
match the old repeated-scan membership oracle (missing/Entity/Bug roots); empty
input does not inspect edges. Existing self-loop/connectivity/supersedence tests
also passed: the final 24-test slice in 20.29 s includes the collision regression
(the initial overlapping slice had 23 passes). The Core change awaits accumulated deployment;
no consolidation was triggered to test it against the reserved corpus.
