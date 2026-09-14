# v0.0.7 performance round closure

September 14, 2026. The operator closed this implementation round after regression
and requested an estimated accumulated gain. Runtime candidate `a504417` contains
wave A plus exact scalar sort dispatch; its `src` tree is identical to `0919c06`.
The reference is the released **v0.0.6** source, `1e01be5`. This report closes the
development performance round; it does not assert a published release or fresh
installed-wheel qualification.

## Accumulated estimate

The fixed native basket has **19.7% estimated lower latency** in this sample:
median paired reference/final ratio **1.245**. The median same-code
control/final ratio is **0.996**. The basket's median elapsed sums
are 1450.338 ms for v0.0.6 and
1159.805 ms for the final candidate. The paired-ratio
median need not equal the ratio of these wall medians.

This is a direct final-versus-v0.0.6 measurement, not a sum or product of earlier
wave-A percentages and the incremental 7.6% scalar-helper result. The weights
were fixed in `PROTOCOL.md` before this campaign ran: one of each of seven native
queries and one of each of four write patterns. A write pattern includes its
staging and commit; those subintervals are not counted again. The ten-transaction
case represents the complete ten-transaction pattern. These synthetic weights do
not describe actual customer traffic. The estimate applies to this basket, not
to every Grafx workload or to Pulse latency.

Reference/final ratios across all six blocks: **1.184, 1.320, 1.238, 1.252, 2.041, 1.194**.
Same-code/final ratios: **1.005, 0.987, 1.518, 0.960, 1.033, 0.974**. These are descriptive samples on one
workstation, not a confidence interval. The workstation was not certified idle;
before/after CPU snapshots retain matching PID/birth-time observations but omit
short-lived processes. Same-code variation is reported, not subtracted as a
statistical correction. Every block is retained, including adverse results.

The median control alone understates the observed variation: five control pairs
are 0.960–1.033, but block 2 reaches 1.518; the reference reaches 2.041 in block 4.
Neither observation is discarded. All six reference/final basket pairs favor
final, but this limited, noisy experiment supports an approximate **20% estimate
for the named basket**, not a precise production guarantee.

## All basket scenarios

Walls are medians across six fresh processes per arm. Ratios are medians of six
paired comparison/final ratios. Positive reduction favors final; a negative value
means more latency in this sample. Small changes must be read with the controls.

| Native workload | v0.0.6 ms | Final ms | Reference/final | Estimated latency reduction | Same-code/final |
|---|---:|---:|---:|---:|---:|
| Indexed lookup | 2.549 | 1.511 | 1.682 | +40.6% | 0.977 |
| Integer top-50 of 200 | 48.971 | 32.704 | 1.455 | +31.3% | 0.995 |
| Integer top-50 of 2,000 | 78.307 | 58.275 | 1.324 | +24.4% | 1.038 |
| 200 distinct destinations, ordered | 98.377 | 27.433 | 3.496 | +71.4% | 1.106 |
| 200 destinations, streaming | 89.885 | 85.742 | 1.065 | +6.1% | 1.001 |
| 100 overlapping sources, ordered | 235.915 | 154.036 | 1.515 | +34.0% | 1.016 |
| 100 overlapping sources, streaming | 406.920 | 391.801 | 1.035 | +3.4% | 0.971 |
| 50 node statements, staging + commit | 88.084 | 63.150 | 1.422 | +29.7% | 0.981 |
| UNWIND 50 nodes, staging + commit | 67.465 | 60.075 | 1.116 | +10.4% | 1.111 |
| 50 relationship statements, staging + commit | 127.081 | 109.852 | 1.130 | +11.5% | 0.981 |
| 10 nodes in 10 write transactions | 205.928 | 164.017 | 1.246 | +19.8% | 1.007 |

Ordered traversal is the strongest repeated direction: about 71% lower latency
for the wide ordered shape and 34% for overlapping sources in this campaign,
consistent with the conditional gains in wave A. The wide traversal's six
reference/final ratios (2.924–5.641) all exceed its same-code range (0.964–1.498).
The 2,000-row scalar case estimates 24% accumulated reduction versus v0.0.6;
the earlier 7.6% result isolates only the later helper change and is not added to it.
Individual-node and relationship write totals estimate about 30% and 12% here,
with the latter retaining a reversed pair.

Streaming traversals have small, inconsistent differences relative to the
controls. UNWIND's reference ratio 1.116 is almost the same as its control ratio
1.111, so its apparent 10% reduction is not an established gain. Lookup, the
small sort and one-transaction-per-node percentages vary substantially from the
earlier campaign; their current observations remain in the basket and table,
but are not promoted to stable isolated optimization claims. Staging/commit
detail and all individual pairs remain in `summary.json`.

## Protocol and correctness

All **18 measured processes exited zero** and their read and write result digests
agree. Two qualification processes, one reference and one final, are excluded.
The six blocks cover every permutation of final, reference and same-code final.
Each process discards read pass zero, taking the median of passes one through
three. It uses two new write stores, discarding round zero. Each retained round
creates 110 nodes and 50 relationships and verifies exact values after checkpoint
and read-only reopen. The shared native seed has 2,000 Item nodes, 101 sources,
200 destinations and 3,400 relationships; expected answers are independently
constructed in Python. Public query execution/materialization and write intervals
are timed; setup, answer checks and checkpoint/reopen validation are outside them.

The instrument is a byte-identical copy of wave A's `native_arm.py`. All arms use
CPython 3.11.14, NumPy 2.4.4 and native google-crc32c 1.8.0 on Windows, with Grafx
imports pinned to the selected source. No Pulse code, schemas, adapters or services
are imported. Each arm has a separate read-only board copy; original seed files
remain byte-identical. Empty per-transaction coordination lock files are the only
permitted additional files. Each write round has its own new store. No heavy test
or benchmark ran alongside this campaign. This does not measure cold start,
concurrent throughput, p99, installed-wheel behavior or live browser requests.

The complete regression at `a504417` passed **24,808 tests**, with **41
attributed skips**, zero failures/errors and exit **0**. The
[scalar-sort report](PERF_V007_SCALAR_SORT.md) retains the earlier interrupted and
failed runs, diagnosis, focused tests and deliberately incorrect variants. Full
local reproduction requires the pinned Core/Community corpus and the historical
v0.0.6 wheel-audit receipts named there. Those audits are historical inputs, not
an installed-wheel claim for this candidate.

Final checks pass: generated API-reference verification, documentation links and
anchors, Ruff on the changed Python files, and `git diff --check`. Focused
consumer/public-adapter documentation tests have **19 passes and two attributed
skips for absent pyarrow**. Three additional independent multi-process smoke
invocations each pass their single test (2.95, 2.81 and 2.91 seconds in pytest).
These checks run after the latency campaign. The full regression above includes
the smoke test as well; none of these focused results replaces its verdict.

## Round disposition and retained evidence

Implementation is closed for this v0.0.7 round. Delivered platform mechanisms
include private coordination sections with bounded waiting, descriptor reuse,
eligible vector-free traversal landing batches, statement-local sort free-name
memoization and exact built-in scalar sort dispatch. The new cleanup correction
keeps known Windows descendants reachable after launcher loss while continuing
to refuse measurements without termination proof.

READ-1, residual alias metadata and M3 are not additional work for this round.
The [roadmap](../../ROADMAP.md#next-iteration-assessment-featurev007) retains them
for a future decision, with their evidence and contract constraints. Rejected
checkpoint and refuted micro-optimizations stay rejected or stopped. The prior
[wave-A report](PERF_V007_WAVE_A.md) remains the consumer evidence: it did not
establish a consistent overall Pulse improvement. No such percentage is inferred
from this synthetic native basket, and recovery of every v0.0.5 performance gap
is not claimed.

Raw evidence is retained locally under `.grafx-tmp/perf07/closure-v007/`:
`PROTOCOL.md`, exact source/instrument/seed hashes, commands, all process receipts,
CPU snapshots, result files and `summary.json`. The full regression is in
`sort-b/qualified-gate/`. Databases and raw metrics are intentionally untracked.
The recorded drivers are `run_closure.py` and `summarize.py`, invoked with the
repository `.venv/Scripts/python.exe -u -B`. After branch integration, reproduce
in new isolated worktrees at the named revisions and a new evidence directory;
the driver deliberately refuses revision drift and overwriting this campaign.
