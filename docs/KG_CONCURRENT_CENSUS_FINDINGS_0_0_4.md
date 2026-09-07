# Concurrent census findings and rejected aggregate projection

## Live Pulse request observations — 2026-09-07

Pulse PID 15796 remained on the `6eabc14` implementation; the single-adoption
startup improvement `eaa7c65` had not been deployed into that process. Using
the authenticated browser against the existing board, ordinary read-only API
requests returned:

| Request | Isolated, sequential (ms) | Graph + stats + Health together (ms) |
| --- | ---: | ---: |
| Graph, first 500 nodes | 884 | 1610 |
| Stats | 3770 | 10995 |
| Health | not requested | 1423 |

Both graph answers contained 500 nodes / 703 edges with successful edge
diagnostics. Both censuses counted 2779 nodes. Every response was HTTP 200.
The concurrent case had warm service-cache node reads (2.0 ms subgraph,
5.4 ms stats), yet edge counting took 10327.5 ms versus 2658.7 ms in the
isolated request. Authority and dispatch were below 1 ms per phase in both.

This demonstrates a workload-dependent residual, not proof of a specific lock,
GIL or storage cause. Health can return while bounded background probes finish;
its response time does not bound all probe work. The requests were not a
controlled cache-cold experiment. No worker was disabled, no read-lane count
changed and no UI operation was serialized to obtain these observations.

## Tested candidate: aggregate node projection — not integrated

The native closed node-projection proof does not pass through AggregateRows.
An experimental patch extended it for scalar grouping/count pipelines, treating
non-distinct `count(n)` as an entity-presence observation. Its bounded spill
codec carried presence instead of detaching all entity properties. Vector/entity
consumers and distinct entity counts retained full decoding.

The focused node-projection, optional-aggregation and spill suite passed
66 tests in 32.29 s, including small-budget and snapshot/corruption cases.
An initially failing full-entity fixture exceeded the 1024-byte spill budget;
the final fixture compared the identical canonical typed refusal rather than
raising the budget or weakening its contract.

The exact Pulse grouped-node census query and parameters were then run through
native read-only connections against the real board, alternating the full and
candidate proofs. All six answers had total 2779 and fingerprint
`26ff679f5861bc526821181e892a0c2b9c8944d0b9a2d7d904868193f851554e`.

| Round | Full decode (s) | Projected aggregate (s) |
| --- | ---: | ---: |
| 0 | 0.704 | 0.307 |
| 1 | 0.273 | 0.305 |
| 2 | 0.287 | 0.357 |
| Median | 0.287 | 0.307 |

The first full-decode call had a cold cost, and the warm sample did not show a
consistent gain. This is not a statistically established regression, but it is
also not evidence supporting deployment for the present performance objective.
Decision: **do not integrate**. The experimental source and tests were removed
with a scoped reverse patch; their files compare identically to HEAD. Private
diagnostic copies remain in `.grafx-tmp/rejected-aggregate-*.patch` and
`.grafx-tmp/node_census_projection_bench.py`, not in production or the release.

Do not reopen this candidate with prolonged marginal timing gates. The bounded
spill presence optimization is not being introduced separately as new scope.
The next existing target remains the dominant cost of concurrent real Pulse
reads. Do not infer permission to cache authority, weaken physical validation,
disable Health or restrict multi-reader/writer behavior from this experiment.

No graph reset, recovery job, redrive or spec consolidation was initiated.
Cognitive ledger hash remained
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

## Native isolation follow-up and consumer correction

A separate native process opened three explicit read-only participants and
queried every physical relationship table once. Sequential lane wall/CPU times
were 2.261/2.234, 2.786/2.750 and 2.703/2.672 seconds. Concurrent wall times
were 4.800, 4.794 and 4.866 seconds, with per-thread CPU 2.297, 2.438 and 2.391
seconds. This workload made useful concurrent progress, but is not a general
multi-process scaling benchmark or proof of a single GIL/lock cause.

Repeating with three transactions sharing one native handle yielded concurrent
completion times 3.144, 4.697 and 6.196 seconds (per-thread CPU about 2 seconds).
Thus handle sharing alone did not reproduce the roughly 23-second live relation
phases. Do not replace that evidence with an assertion that pool selection is
the established root cause.

All native answers covered **69 unique physical layouts / 4424 edges**, exposing
the consumer's duplicate `supersedes(Decision,Decision)` declaration. Community
`9c9ef1a` deduplicates exact layout triples, preserving order and all distinct
layouts. It passed 55 focused tests in 11.33 s. This fixes inflated census and
verification work, not just a label. Earlier 70-statement/4425 totals included
one physical edge twice; they do not show a graph data mutation.

Pulse PID 7896 now runs this correction and the single-adoption Grafx `eaa7c65`.
REST confirmed 2779 nodes / 4424 edges / 69 layouts / no failures. The browser
loaded 500 nodes and paged to 1000, with zero failed edge tables. Live graph/stats
requests still took 24.758/24.555 seconds even after an isolated census warmed
the instance. The relation phases were 23.717/23.603 seconds. The full Pulse
workload remains the target, not another admission-only or marginal query gate.
No reserved spec was consumed (21 pending / 0 in progress / 19 consolidated).
