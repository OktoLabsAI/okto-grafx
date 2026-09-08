> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Amortized retained-memory telemetry — 0.0.4

## Motivation and finite scope

The source-reference write-attribution experiment found telemetry itself on the
critical path: with OpenMetrics enabled, 222 full retained-object walks consumed
3.626 s of a 4.395 s profiled four-node/eight-edge commit. Writable opening took
59.076 s, compared with a separate noop run at 3.155 s. These are diagnostic
observations, not a controlled claim about the live Pulse. See
`SOURCE_REFERENCE_WRITE_COST_0_0_4.md` for the fixture and limitations.

Previously `BufferPool._usage_reading` traversed the complete retained object
graph after each residency-topology report. Filling N resident frames could
therefore traverse roughly N*(N+1)/2 frames just to report memory. The estimator
does not control admission, eviction, visibility, corruption or durability.

## Implemented policy

The nominal used-byte gauge is still captured and reported on every topology
report. The retained-memory gauge now samples immediately on the first report,
then after `max(1, last_estimate_bytes // page_size)` reports. This work-based
interval uses the existing estimate's page equivalents, not a hardware-calibrated
constant or time gate. Each sample uses the unchanged callback-free `python-v2`
walk. Skipped samples do not emit a cached value as a new measurement.

One integer counter belongs to each pool, under the same pool guard. It is not
a graph certificate, does not survive a pool lifetime, and is never shared across
readers/writers. No clock callback, timer thread, new connection setting, metrics
label or port is introduced. A disabled sink performs no retained walk and does
not advance the countdown. Existing host-callback containment remains in place.

`BufferPool.retained_bytes_estimate()` and immutable `Database.pool` remain
unconditionally fresh, including page slot growth and transient bookkeeping.
Explicit diagnostics do not consume the background gauge's cadence. The
sampling counter's slot belongs to the normal pool object-size estimate; its
scheduling value is not recursively retained data or a new estimator formula.

### When to use each observation

- Use the automatic gauge for low-overhead sampled memory trends under load.
- Use `Database.pool` when an operator explicitly needs the current estimate.
- Do not use the gauge as a peak detector, exact per-transition reading, or a
  memory-admission limit. A sudden shrink may precede its next sample; idle time
  does not refresh it. The policy promises no wall-time freshness bound.

These sampling semantics are documented in PERFORMANCE and the architecture
contract. No native data/query semantics or telemetry names are removed. The
noop path adds no sampling work.

## Focused validation

The buffer, single-flight, descriptor telemetry and commit-metrics slice passed
223 tests in 9.85 s. The initial slice had 221 passes and two new fixture failures:
one retained page need not produce an interval greater than one. Fixtures now
load two pages before asserting a deferred next sample; no sampling constant was
changed to satisfy them.

New structural cases exercise both legacy guarded and production single-flight
paths. Loading 512 frames traverses fewer than 3*N resident frames across all
automatic samples, rather than N*(N+1)/2, while nominal usage still reaches the
exact budget. Cases also cover explicit fresh slot-growth diagnostics, no fake
re-emission, current values at the next due sample, disabled sinks and independent
per-pool countdowns. Existing tests retain corruption/read protocols, dirty
eviction, retired-pinned memory, callback containment and commit phase accounting.

## Private real-schema comparison and quality boundary

The OpenMetrics-enabled copy run completed with normal transactions and native
checkpoints. Original-index arm opening was 7.054 s, unprofiled commits
0.100 / 0.164 s, checkpoints 2.320 / 2.923 s. The preceding every-report run
observed opening 59.076 s and commits 1.894 / 1.186 s. The new third profiled
commit took 0.324 s, with no full estimator walk due in that particular commit,
versus 222 walks in the old profile. This does not promise every commit avoids
sampling, nor a controlled speedup: runs were separate and the new run overlapped
quality tests. The indexed arm observed commits 0.327 / 0.246 s and checkpoints
7.361 / 6.452 s, so other instrumentation/host-work costs remain visible.

Both private copies passed full read-only verification and cold readback of all
12 synthetic nodes. Ordered judgement/provenance endpoint pairs were identical,
12 of each per arm. Artifacts: `.grafx-tmp/source-write-taxkq4fu`. No live spec was
consumed during this proof. Explicit source-identity counting with production
single-flight measured 9 samples / 658 frames traversed for 512 admissions and
10 / 1,400 for 1,024, versus 131,328 / 524,800 with every-page sampling.

The additional API containment/concurrency/architecture slice had 255 passes and
7 failures in 29.88 s. All failures are the static architecture gate: 9 findings
in six unchanged modules plus its aggregate assertion. Scanning the exact Git
HEAD sources before this change reproduces the same 9 findings; the candidate
buffer module has zero. These are not waived or described as a green full gate:
`vars` in control_record; threading/weakref in schema; heapq in hnsw/query_engine;
contextvars in index_manager; _thread/threading in query_engine; inspect in
recovery_manager. They require a separate architecture correction/reconciliation
without undoing concurrency or silently broadening the purity gate.

The operator subsequently authorized deploying the latest Grafx and measuring
one real spec consolidation. That run completed with installed Grafx `2db169d`:
5 nodes/10 edges, full Pulse commit 22.981 s and downstream delivery 131.489 s,
without retry. Twenty specs remain reserved. This is not an attributable live
speedup from telemetry sampling. Installation, receipts, verification and timing
boundaries: `PULSE_SINGLE_SPEC_INSTALLED_0_0_4.md`.
