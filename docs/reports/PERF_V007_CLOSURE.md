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

### Remote CI follow-up

PR #6's initial remote full-suite jobs stopped at collection because the runners
did not have the pinned Community/Core corpus sources. The workflow now provisions
both public checkouts at the freezer's exact revisions, retaining Git history.
Two historical wheel-receipt audits also reproduced unattributed skips in a clean
worktree. Their absence is now explicitly attributed and listed as exactly two
coverage-debt entries; their assertions remain active when real receipts exist.
[CI-WHEEL-RECEIPTS](../../ROADMAP.md#ci-prerequisite-correction-ci-wheel-receipts)
records that remaining tooling debt. This does not change the runtime, the local
24,808-pass result, or the performance estimate, and does not claim remote
execution of the two historical audits.

The initial push run also exceeded the D5 durable-commit ceiling (10.32x on
Windows and 20.79x on POSIX, against 10x), while both PR D5 jobs passed on the
same head. Those failed observations are retained. The ceiling, durability and
coordination policy are unchanged; a successful repetition does not erase them.
Remote full-suite completion and independent code-owner approval remain separate
requirements from the local regression evidence above.

Focused validation of this CI correction: 268 passes, two attributed historical
receipt skips and two attributed pyarrow skips, zero failures/errors and both
pytest exits zero. This covers the corpus, packaging, leg verdict, cross-family
coverage, skip attribution and documentation regressions. With real receipts
present both audits pass; an altered recovered-value receipt fails its assertion.
The workflow parses and its two checkout revisions/environment paths agree with
the freezer. Documentation/API checks, Ruff and diff checks pass. Evidence is
retained under `.grafx-tmp/perf07/merge-main/`; it is not a new full regression.

The previously invalid `v006-compatibility.yml` also needed a block scalar for
the `--only-binary=:all:` command. Once GitHub could parse it, both Linux and
Windows upgrade workers failed on missing `tzdata`. The workflow now installs
the built candidate and its declared runtime dependencies. A fresh Windows /
Python 3.11.14 environment reproducing that workflow passes all 14 installed
0.0.5-to-0.0.7 upgrade cells and its 38 selected regression tests (93.97 seconds),
with zero exits. This is bounded upgrade qualification, not a full installed
platform qualification. All three workflow files parse locally.

The PR's next D5 POSIX observation at `5e3cb96` still fails durable commit at
11.42x against 10x; point read, open replay and vector recall meet their targets.
This remains failed evidence, independently of CI dependency corrections.

### Correcting the stale CI timing policy

The subsequent inspection found a policy mismatch: the performance guide and
roadmap have treated D5 timings as informational since `8380da3a` (September 8),
and the v0.0.7 plan repeats that decision, but the workflow still used the original
strict M1 comparator. CI now explicitly selects `--informational-ceilings`.
Valid overflows retain their values and an `informational_ceilings_exceeded`
status. Missing or invalid evidence, recall below the frozen floor, and the
unconditional/fatal execution guards remain blocking. Negative or duplicate D5
samples are now rejected in either mode. The default CLI preserves strict
comparison for historical reproduction.

No numeric ceiling, recall floor, raw artifact or database runtime was changed.
Original failed job receipts remain failed. Applying the policy to the exact
downloaded `5912a6f` metrics retains POSIX durable commit 19.89x and Windows
12.56x against 10x, with recall 0.9953 against 0.9; both now receive the explicit
informational status. Their metric-file hashes stay unchanged. This is a CI
policy correction, not an additional performance gain.

Focused gate, recall and workflow-freeze regression: 198 passes. The 20 new
policy cases fail before implementation. Deliberately removing the timing policy,
hiding a low recall behind a timing overflow, or enabling informational behavior
implicitly each fails the strengthened tests. Evidence is retained under
`.grafx-tmp/perf07/ci-policy/`.
The complete bench group plus consumer/public-adapter documentation regressions
passes 473 tests with three attributed optional-dependency skips in 36.02 seconds,
exit zero. Documentation/API checks, changed-file Ruff and diff checks pass.

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
