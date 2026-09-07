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
