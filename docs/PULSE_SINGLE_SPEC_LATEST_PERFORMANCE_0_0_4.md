# Latest performance revision — one real Pulse consolidation

Executed September 8, 2026. One newly authorized live sample, not a performance
gate or completion of the full evolution plan.

## Runtime identity

Pulse **0.3.3**, PID **4212**, already uses installed Grafx **0.0.4**, code
`fa8f18807885a47704fb5461a3069b54adc89eab`. Latest performance branch:
`feature/v0.0.4@61fc44da2a8298caa07ec29e318aab29597f228b` (documentation tip).
All **145 package files** match the installed distribution after normalizing
Windows CRLF/LF endings. Process start: 03:15:49 local, after the installed
Python files' latest modification at 03:15:45.

Grafx imports from Python 3.13 user site-packages, with NumPy **2.5.2** and
google-crc32c **1.8.0**. Community remains `7158383`; Core remains `9303f98`
(phase-observation code `0a38312`). Data home: `C:/Users/jpamb/.okto-pulse`.
No replacement/restart was needed. The unfinished `feature/gx-cap-1` worktree
was **not installed or activated**.

## Workload and protocol

Board `15877207-c147-4805-96d7-d53a625571df`, **Okto Pulse**. Spec
`c2e4996a-dcfd-5e92-9025-313d9afb27cb`, **[0.3.3] Canonical Project structure
contracts and card projections**, Done, edition 2, version 113.

Read session/KG instructions, policy guidance, board guidelines and full Spec
context. Eleven formal Decisions already existed; no Alternative existed under
this Spec's source prefix. Authored eleven summaries of explicitly rejected paths:
aggregate ownership, optional-state semantics, vocabulary/notes, resource limits,
atomic validation, parent kinds, removal impact, Card projections, Code Evidence,
human/agent parity and whole-Spec export.

Session **`kgses_9a10a220e5654df1`** began with full context for deduplication and
`deterministic_candidates=[]`; `nothing_changed=false`. Eleven similarity reads
returned no matches; all reconciliation hints were ADD. No overrides or manual
provenance edges. One commit produced **11 Alternative nodes and 22 edges**:
eleven Decision-to-Alternative judgement links and eleven automatic source links.
Connectivity was enforced and passed without violations. No Decision was duplicated.

## Measured result

Prior sample: [previous live run](PULSE_SINGLE_SPEC_INSTALLED_0_0_4.md).

| Measurement | Previous sample | This sample |
| --- | ---: | ---: |
| New nodes / edges | 5 / 10 | **11 / 22** |
| Full commit MCP call | 22.981 s | **10.726 s** |
| Global outbox creation to processed ACK | 131.489 s | **54.952 s** |

Observed elapsed times are **53.3% lower for commit** and **58.2% lower for
Global delivery**, with more authored nodes/edges. Different Specs and runtime
states mean this is not an identical-input A/B test, attributable engine speedup,
scaling guarantee or throughput benchmark.

Preparation tool calls totaled **2.516 s**, excluding reading/reasoning, client
startup and between-call delays. Commit timing includes Pulse orchestration,
graph work and relational/transport boundaries; it is **not native-only writing**.
Global latency includes scheduling and verification. Do not add overlapping
boundaries to claim an exact end-to-end duration.

Phase-observation code is deployed, but no session phase observations were
captured in process output. Native-write attribution is **unknown**, not zero.
No second spec was consumed to obtain instrumentation. Alternative has no vector
index in the exposed schema, so this does not establish indexed Decision/vector
write performance.

## Verification and durable completion

- Eleven titles, bodies and exact source references equal the authored inputs.
- All eleven judgement endpoint pairs and eleven source-root pairs match exactly;
  no readback was truncated. MCP reads: **0.376 / 0.408 / 0.182 s** respectively;
  executor: **91.1 / 167.2 / 74.2 ms**.
- Exact-title natural retrieval returned the new Code Evidence Alternative first,
  similarity 1.0, in **2.709 s**. This does not establish broad-query ranking.
- Read-only SQLite audit confirms 11 additions, 22 edges, eleven node references,
  committed at **10:05:52.644055 UTC**, error details null.
- Event **`evt_c75e16698ea646d9`**, outbox row
  `c16db6a4-6743-4a01-b948-85a366896973`, created **10:05:53.316642 UTC**,
  processed **10:06:48.268465 UTC**: **54.951823 s**, retry count **0**, last error
  **null**. The graph commit was never repeated.
- One cognitive item closed through MCP with the real session,
  `outcome_type=relation_created` and concrete evidence. The other **39 ledger
  items are identical** before/after. Final: **19 pending, 21 consolidated,
  zero in progress/failed/skipped; total 40**.

Health returned Board/Discovery healthy, metrics available, active queue zero and
Board DLQ zero. Historical Global DLQ **224**, policy projection DLQ **439** and
canonical debt remain; overall is `at_risk`. Some snapshots are stale and runtime
budget is incomplete. Cached total_nodes is not used to prove this write.
An outbox checkout-age warning occurred during delivery, which nevertheless
finished without retry. This is not an all-green operational audit.

Private evidence: `.grafx-tmp/newbench-final-evidence.json`, SHA-256
`2dfbae7de6d8b5914cd705c7717afceb4c614ed291f957b3ac45ec144cef072a`.
Manifest includes receipt hashes, timings, counts and preserved-ledger checks.
Source context and credential-bearing configuration are not copied here.

## Scope closed

One real consolidation completed on the latest installed performance revision.
**19 pending Specs remain reserved**. No reset, rebuild, redrive, performance gate,
protocol relaxation, CAP activation, PyPI publication or main merge occurred.
Before another authorized benchmark, verify INFO observation delivery if phase
attribution is needed; do not infer native cost from full-call duration.
