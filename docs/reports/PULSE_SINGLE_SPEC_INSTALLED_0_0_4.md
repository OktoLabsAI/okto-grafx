> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Installed Grafx 0.0.4 — one real Pulse consolidation

Executed September 7, 2026 in America/Sao_Paulo (receipts September 8 UTC).
This closes only the operator-authorized single-spec experiment, not the complete
0.0.4 performance or evolution plan.

## Deployment identity

- Native Grafx `feature/v0.0.4@2db169d`, version **0.0.4**.
- Built wheel `okto_grafx-0.0.4-py3-none-any.whl`; SHA-256
  `8E119D8FE5CF742B88D414A4326732FF60D4CB0BA6D262BDD880E00101FF3F12`.
- Installed into the Python 3.13 user environment, replacing Grafx 0.0.3.
  Actual import resolves to user `site-packages/okto_grafx`, not the source tree.
  Existing accel dependencies: numpy 2.5.2 and google-crc32c 1.8.0.
- Pulse **0.3.3**, Community source `7158383` (code `a5a5c3c`), Core source
  `e1e9d08`. Only Community/Core are on PYTHONPATH; native Grafx is the installed wheel.
- Prior PID 23228 terminated and closed graph participants without close failures;
  both ports were free before restart. New PID **2124** serves 8100/8101.
  Data home remains `C:/Users/jpamb/.okto-pulse`. No reset, rebuild or redrive.
- No PyPI publication or additional UV-environment installation in this checkpoint.

## One source-grounded cognitive transaction

Board `15877207-c147-4805-96d7-d53a625571df`, spec
`cbd4eedd-a16b-51ae-93b6-5f850467fb4d`, **Community persistence, API and whole-Spec
export**, Done, edition 2, version 96.

Read the Pulse preflight, policy/KG instructions, board guidance and full-context
response. The five formal decisions were already canonical; no Alternative existed
under this spec's source prefix. Synthesized five rejected-path alternatives from
the decisions' explicit alternatives and rationale: optional-state storage,
independent transport mutation paths, partial batch persistence, new assessment
gates, and standalone/UI-limited exports. These are real knowledge, not benchmark
assertions. No deterministic nodes or manual belongs_to edges were submitted.

Session **`kgses_43a75608efd34f80`** used `deterministic_candidates=[]` and full
artifact context for hashing; `nothing_changed=false`. All five similarity reads
returned no matches; all reconciliation hints were ADD and were reviewed without
overrides. Commit returned success with enforced connectivity and no violations:
**5 nodes added, 0 updated/superseded/merged, 10 edges added** (5 judgement links
to existing Decisions and 5 automatic source-provenance links).

## Actual observed times

| Boundary | Time |
| --- | ---: |
| Sum of begin, 5 adds, 5 similarity reads, 5 edge adds and reconciliation MCP calls | 1.781 s |
| Complete commit MCP call | **22.981 s** |
| Readback of all five new nodes, MCP / executor | 0.179 s / 35.3 ms |
| Readback of five judgement pairs, MCP / executor | 1.016 s / 603.5 ms |
| Readback of five provenance pairs, MCP / executor | 1.871 s / 699.0 ms |
| Downstream outbox creation → processed ACK | **131.489 s** |
| Exact-title natural retrieval of a new Alternative | 4.597 s |

Commit timing excludes agent reasoning, Python/MCP client initialization, candidate
preparation and later asynchronous delivery. It includes Pulse orchestration,
validation, graph writes, relational audit/outbox and finalization; it is **not
native transaction timing**. Sum of preparation and commit tool calls is 24.762 s,
not elapsed human/session duration. Delivery latency includes scheduling/queue work;
it is not 131 seconds of proven physical writing. Board commit and delivery can
overlap; do not blindly add their durations as an exact end-to-end measure.

The earlier successful spec took 26.960 s for 4 nodes/8 edges. This sample is
3.979 s lower despite 5 nodes/10 edges, but differing source content, warm state,
instrumentation and other changes prevent an attributable speedup claim. The
large private telemetry microbenchmark gain has **not** translated into a similarly
large measured full-Pulse improvement in this sample. In particular, do not infer
the live metrics sink was enabled from that microbenchmark.

## Durable completion and preserved corpus

SQLite audit independently confirms the session, counts, committed time
`2026-09-08T02:51:24.019009Z` and null error details. Five Alternative references
are persisted. All five node bodies/source refs, five Decision-to-Alternative pairs,
and five Alternative-to-spec-root pairs were read back through Pulse's canonical
Cypher interface. Exact-title natural search returned the new export Alternative
first, similarity 1.0. This does not imply every broad query ranks it in its top 10.

Global event **`evt_07f41318afbf4fa7`**, outbox row
`e0ccd892-15d0-4d53-8d27-724855d46736`, was created at
`02:51:24.811891Z` and acknowledged at **`02:53:36.300435Z`**, retry_count **0**,
last_error **null**. The board transaction was never replayed.

Ledger item `cogn_f3823fcbdea156279865a52db601435e` was closed through MCP with
the actual session, `outcome_type=relation_created` and five concrete KG refs.
An initial ledger-only request lacking outcome_type was correctly refused without
mutation; the qualified request succeeded. Aggregate: **20 pending, 20 consolidated,
0 in progress/skipped/failed, total 40**. Every remaining pending item is exactly
equal to its pre-test API projection. No other spec was consumed.

Final health returned board/discovery **healthy**, metric_status **available**,
queue_depth 0 and board DLQ 0. Some diagnostic snapshots remained explicitly stale
with refresh scheduled and runtime budget incomplete; this is not a fully fresh
all-green diagnostic report. Historical 224 Global DLQ items, 439 policy projection
DLQ items and one canonical debt remain outside this experiment and were not hidden.

## Finite follow-up

The existing full-write investigation should now prioritize attribution of the
**131.489 s Global delivery**, then the **22.981 s full commit**. Reuse immutable
receipts and isolated copies/profiles before consuming another reserved spec.
This is prioritization of the existing write-cost task, not another timing gate.
No change to multi-reader/writer, snapshots, OCC, WAL, durability or fail-closed
authority/corruption was made for this run. The previously documented nine static
architecture findings remain open; this experiment does not waive them.

Local bounded MCP/SQLite evidence (no credential URL):
`.grafx-tmp/single-spec-live-20260908-evidence.json`, SHA-256
`BB2669DE475D0990D0B801CE49F6827227B33195366500F95C4A7BABE3B95944`.
It contains receipts, candidates' preparation timings, readbacks and before/after
pending inventories; full source context is not copied into the tracked report.
