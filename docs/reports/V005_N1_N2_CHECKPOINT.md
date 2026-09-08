# 0.0.5 N1–N2 checkpoint

September 8, 2026. Source base `b12500d40e7c00edfa5175e7e050c4f07136ec30`,
branch `feature/v0.0.5`. [Roadmap authority](../../ROADMAP.md#proposed-next-round-after-the-005-checkpoint).
N3 (overflow reclamation) and N4 (commit provenance) were **not started**.

## Installation before development

Installed the committed Grafx 0.0.5 wheel in both existing local Pulse 0.3.3
environments: Python 3.13 user-site and the UV global tool environment. Preserved
each installed Pulse distribution's code/assets by repacking its own RECORD
inventory, changing only its Grafx requirement to `okto-grafx[accel]==0.0.5`.
Both interpreters report Pulse 0.3.3, Grafx 0.0.5 and import NumPy/google-crc32c.
UV's 121-package dependency check passes. The shared global Python environment
reports unrelated dependency conflicts (LangChain, Omni, OpenTelemetry,
Streamlit and others); those packages were not changed to conceal the findings.

The previous Pulse process exited; its replacement uses the same default data
home and Grafx Board/Global backends. Startup completed, both ports 8100/8101
belong to the replacement process and the root HTTP request returned 200.
This is installation/startup verification, **not** a complete live KG audit.
No reserved specs, redrive or rebuild were manually initiated. Normal startup
workers resumed. The new N1–N2 source edits below are not installed in that
running process; the installed wheel is the committed pre-checkpoint build.

Wheel SHA256: `a5c30b8e242c629984d9c3b862617334495af25f0a98107a5e06c0e8784cea7e`.

## N1: bounded existing-index admission

`IndexManager.register(existing_only=True)` formerly called `exists`,
`is_created`, then `open`; the native path repeated header/extent observations.
`IndexStore._open_existing` now combines creation-shape and open checks using
one call-local extent and pinned header. Missing/empty/pristine structures are
not repaired, the bucket extent is checked and digest/visibility/artifact nonce
refusals remain. The observation is neither stored nor shared across operations.

Overridden public admission hooks and `_read_header` retain the canonical path.
The native hook identities are captured before runtime overrides. Registration
still runs independent freshness checks and pre/post heap-view certificates.
The discriminating test counts one explicit extent observation in the combined
admission; it does not claim only one physical IO for an entire cold connection.

The first-reader checkpoint remains in the measured consolidation boundary.
Its A/B/C protocol, registry synchronization, replay preflight, barriers and
fresh phase-C authority were **not skipped**: doing so needs a separate proof
and is not justified by the observed duplication alone. Cold admission remains
a residual cost; this checkpoint does not claim it has been eliminated.

## N2: scalar parameter preparation, no new batch API

The real 1,100-node page fixture already uses one read snapshot for the 13
selected relationship layouts, with zero warm parser/plan builds. Profiling
identified recursive scalar parameter dispatch as a bounded remaining cost.

Two small changes avoid dynamic container checks for exact built-in scalar
leaves (`str`, `int`, `float`, `bool`, `None`):

- Native `_validate_parameter_map_value`: scalar IDs cannot contain map-key
  collisions; nested/custom mappings still receive complete validation.
- Community `_grafx_query_parameter_value`: scalar IDs need no datetime/container
  conversion. Nested timestamps are still converted and mutable containers copied.

No result/authority cache, cross-statement validation witness or public batch API
was added. Subclasses remain on the recursive path, including a string subclass
that implements Mapping. Rebinding a mutated map still detects new collisions.
Pulse Core was not edited. The Community change is in
`src/okto_pulse/community/adapters/grafx_graph_transaction.py` in the matching
consumer checkout and must accompany a later Pulse deployment.

## Evidence and measurement limits

Fresh temporary stores; Windows/Python 3.13.1, full synthetic Pulse schema,
fixture authorization and real graph IO. The local production Pulse remained
running on the same host; OS caches/frequency and background load were not
controlled. Historical latency differences are **not causal speedup evidence**.
No new performance threshold was imposed.

Focused validation: 77 index/admission tests, three scalar-validation tests and
31 Community executor tests passed. The synthetic consolidation reached four
nodes, four edges, four refs/digests, one durable ACK and an empty second tick.
That consolidation ran after N1 and before N2's scalar-only changes.

Final grouped regression: **1,009 passed in 92.32 s** (all `tests/index`, query
engine/scalar/ordered-projection tests, read-only/writable admission, startup
artifact section and checkpoint/reclamation/watermark tests). Ruff, documentation
links/config/API coverage and generated-reference freshness checks passed.

The final N1+N2 page run verifies exactly 1,100 total nodes, distinct first/next
500-node pages, expected incident edges, no failed layouts and zero warm
parser/plan builds. Independent 1/2/4-participant read/write cases pass, as do
Global digest/link readback, verify-all and durable reopen. No linear thread
scaling claim follows from these correctness tests.

| Operation | Latest sample | Boundary |
| --- | ---: | --- |
| Full-schema writable open | 2,808.201 ms | Separate from HTTP, OS cache not flushed |
| First 500-node HTTP page | 488.462 ms | New handle; JSON included |
| Warm first / next 500-node HTTP page | 264.038 / 314.126 ms | Exact page/edge assertions |
| Community parameter conversion, 14 statements | 5.221 ms | Profiled attribution, not uninstrumented latency |
| Native parameter binding, 14 statements | 4.741 ms | Same profiled cycle; no authority reuse |
| Four-candidate reconciliation | 4,476.345 ms | N1 consolidation; includes cold joins/checkpoint |
| Four-node/four-edge consolidation commit | 1,621.423 ms | N1; graph dispatch 975.800 ms within total |
| Worker through durable ACK | 2,908.809 ms | N1; includes flush/reopen verification |

The measured parameter work is now small enough that a new public batch API or
shared validation cache is not justified for this slice. The broader cold-open
and concurrency throughput limits remain in the roadmap; they are not new gates
on this delivery. **Checkpoint reached; N3/N4 remain unstarted.**

Reproduction uses `PYTHONPATH=<grafx>/src` and the matching
`OKTO_PULSE_COMMUNITY_REPO` / `OKTO_PULSE_CORE_REPO` source roots:

```text
python tools/perf_round/round005_consolidation.py
python tools/perf_round/round005_pulse.py --nodes 1100
python tools/check_documentation.py
python tools/generate_api_reference.py --check
```

Local receipts: `.grafx-tmp/n12-consolidation.json`,
`.grafx-tmp/n12-pulse-1100.json` (N1 attribution),
`.grafx-tmp/n12-pulse-final.json` (N1+N2), `.grafx-tmp/n12-regression.txt`.
These are ignored local evidence, not distributed datasets. Harnesses retain
source hashes; the initial consolidation receipt predates adding index-manager
hash coverage to that harness.

Receipt SHA256 values:

- Final page: `b11a3234e60089b57014580b9579b1c59fd566e2f67a8fca69215535877e5c98`.
- Consolidation: `c833c97329b1acd6d2eebd63535021e32d8aed6c550d5e768e6d76ff20f80dce`.
- Index manager used in both runs: `e3883ab9ce6a23dd21615c9fc180ae6e353f59331a8c075f283286c474d75f6c`.
