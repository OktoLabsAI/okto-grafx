# 0.0.5 approved four-item follow-up

September 8, 2026. Development branch `feature/v0.0.5`, version `0.0.5`.
This is evidence for the four approved actions, not a new roadmap or release.
The live Pulse installation and reserved specs were not changed.

## Delivered scope

1. **Ordered-page selective materialization.** Query planning proves the complete
   linear read pipeline, retaining predicate, projection and ordered-key columns.
   Heap decoding still validates all stored columns and overflow payloads, including
   omitted vectors. Whole-entity/custom hooks and staged owner overlays keep their
   canonical paths. Pre/post ordered-root certificates are unchanged.
   Implementation and named tests are listed below.
2. **Single final writable-startup admission.** Startup still completes the committed
   gap, refreshes the catalog and holds `COMMIT_SECTION` plus the WAL-tail section.
   Its body performs the complete admission once, instead of repeating an
   existing-only admission immediately before it. Recovery's independent baseline
   callbacks, physical identity validation and read-only admission are unchanged.
3. **Full synthetic Pulse consolidation and attribution.** A real Community
   composition runs SQLite, native Grafx, reconciliation/commit/finalization and the
   actual `GlobalOutboxProcessor.process_once()`. It verifies four node references,
   four global digests, one processed event and an empty second worker tick. The
   worker's flush/reopen/post-verification protocol precedes ACK. This closes the
   adapter-only evidence gap, not every production latency/operational issue.
4. **Typed connection configuration.** Public `ConnectOptions` covers all 35
   non-path `DatabaseConfig` fields with optional keys and selector literals.
   `connect(**options: Unpack[ConnectOptions])` keeps the same validating runtime
   and separate custom-registry keyword. No runtime dependency or format change.

## Correctness finding resolved during item 1

A pinned reader followed a newly committed ordered-index reference into its old
resident heap page. Both strict and generation modes raised `Slot 4 does not exist`
after an independent writer updated the ordered timestamp. It was not permissible
to skip that reference blindly, ignore malformed slots, or advance the snapshot.

Ordered reads now pair their freshly proved root with the companion physical heap
view. A changed foreign root discards **only clean** frames, then record headers
apply the caller's original MVCC snapshot. Locally published roots retain their
own heap frames. Dirty refresh refuses; a heap from a different buffer pool
refuses. This is a cache-coherence token, not cached authorization: both root
certificates are still read and compared on every operation, including early close.
The materialized and lazy ordered doors share this protection.

## Latest measurements

Windows/Python 3.13.1; installed accelerators available, no hardware or SLO claim.
OS filesystem cache was not flushed. Projection is a four-sample warm median;
other values are individual observations. No performance pass/fail threshold.

| Workload | Latest measured value | Boundary |
| --- | ---: | --- |
| Ordered projected page, 128 of 256 rows | 20.247 ms | Two tables, non-null 384d vectors and 12,000-character overflow payloads; full result equality checked against the canonical decoder |
| Writable new handle, full Pulse schema + 128 nodes | 2,521.420 ms | Open/admission only, separate from HTTP |
| Warm Pulse HTTP page, 128 nodes / 127 edges | 118.435 ms | Actual route/service/adapters; fixture authority, no result-cache hit; 0 failed edge layouts |
| Synthetic reconciliation, four candidates | 3,508.247 ms | Includes first independent-reader joins; not pure graph mutation |
| `commit_consolidation`, four nodes / four edges | 1,404.860 ms | Includes health admission and graph/relational staging |
| Graph-dispatch portion of that commit | 790.214 ms | Includes executor dispatch and graph work; nested within the preceding row |
| Health-admission portion | 584.054 ms | Nested within `commit_consolidation` |
| SQLite transaction commit | 1.226 ms | After consolidation staging |
| Outbox worker including processed ACK | 2,509.933 ms | Immediate explicit tick; no periodic scheduling wait |
| Outbox application portion | 1,330.514 ms | Nested within the worker duration |
| Outbox flush/reopen/verification portion | 1,133.135 ms | Nested within the worker duration |

Schema setup is excluded from those operational phases: SQLite 3,096.851 ms,
board schema 11,264.939 ms, global schema 1,402.449 ms. Stub embeddings avoid
network/model latency but still exercise real vector storage/search. No similarity,
health, routing, graph IO, SQLite, outbox or ACK result was mocked. Timing wrappers
delegate exactly once to production operations. The fixture explicitly installs
the Community relational effects and coordination providers and initializes both
routes; failing to compose those dependencies correctly refuses rather than
silently bypassing them. The fixture uses the existing soft board-write-barrier
setting; it does not certify an admin recovery lane or every production policy.

### Attribution and remaining limits

A separate profiled run (times include profiler overhead and are **not** latency
samples) places 7.655 s of the 8.300 s reconciliation call under
`read_database`: four actual `connect` calls account for 4.019 s and one first-join
checkpoint for 3.569 s. Source: Community
`routed_board_graph_composition.py:read_database` and Core
`primitives.py:_find_existing_graph_matches`. This identifies independent-reader
admission/first-join maintenance as the dominant small-fixture cost, not SQLite
commit. The delivered startup optimization removes demonstrated duplicate
admission, but it does not eliminate all reader joins/checkpoints.

Do not move checkpoint time outside the measured boundary and call that an engine
speedup, share a writer handle with the readers again, or suppress required
recovery checks. Further reader-lifetime/open work remains bounded future
performance work in the roadmap. The old live 54.952-second ACK observation
included different data and scheduling; this fixture is **not** a causal speedup
comparison against that run. No linear writer scaling, live UI latency,
overflow-space reclamation or complete production remediation is claimed.

## Validation and source references

- Grouped `tests/query tests/index tests/api`: **4,198 passed, 5 failed, 1 skipped**
  in 660.52 s. The failures were investigated, not omitted from the result.
- One lifecycle assertion expected eight obsolete per-name existence probes;
  updated to require zero while other tests prove fenced byte admission.
- The four physical-WAL fixture failures were reproduced from untouched `HEAD`
  (`425362a`) in an isolated archive: **4 failed, 22 passed**. They labeled arbitrary
  bytes as a HEAP table descriptor. The corrected fixture uses opaque OVERFLOW
  payload pages; a new negative test retains the malformed-HEAP refusal.
- Final affected/adjacent set: **138 passed / 33.79 s**, including all five failed
  cases, startup corruption, strict/generation projection, old/new snapshots,
  customized hooks, certificate loss, API options and documentation consumers.
- Documentation links/anchors, all config fields, public signatures/DTOs and
  the preserved eleven-plan archive pass. Ruff passes for changed implementation,
  tests and tools.
- `tools/check_connect_typing.py`: valid kwargs/dictionaries/custom registries
  accepted; exactly three expected errors for a misspelled keyword, wrong type and
  invalid selector. Mypy targets Python 3.11 syntax. This is not an executed
  multi-Python or cross-platform matrix.
- Strict mypy on query/ordered/options: 253 existing query diagnostics; comparison
  against the 254-diagnostic baseline adds none and removes one. Ordered/options
  have no diagnostics in this isolated invocation. The entire repo is not type-clean.
- Candidate wheel installed into the isolated stdlib-only venv passes version,
  typed export, query, durable reopen and verify-all smoke. **Not installed into
  production Pulse, committed, pushed or published by this follow-up.**

Primary implementation/tests:

- `src/okto_grafx/engine/query_engine.py`: `_closed_node_scan_projections`, `_ordered_node_merge`.
- `src/okto_grafx/engine/heap_store.py`: `_read_if_projected`, `_decode_version_with_header`.
- `src/okto_grafx/engine/ordered_index.py`: `_prepare_ordered_heap_view`, `_iter_visible_desc`, `visible_desc`.
- `src/okto_grafx/api/assembly.py`: `assemble_database`, final `schema_artifact_section`.
- `src/okto_grafx/api/options.py`, `src/okto_grafx/api/__init__.py` and top-level exports.
- `tests/query/test_ordered_projection.py`, `tests/api/test_writable_single_index_admission.py`,
  `tests/api/test_connect_options.py`, `tests/api/test_end_to_end.py`.

## Reproduction and receipts

Run from the Grafx checkout with `PYTHONPATH` pointing at its `src`. Pulse fixtures
require matching `OKTO_PULSE_COMMUNITY_REPO` and `OKTO_PULSE_CORE_REPO` source trees;
this run used `okto-pulse-v003-kg-load-codex` and `okto-pulse-core-kg5-codex`.
Do not point their isolated data identity at a live installation.

```text
python -m tools.perf_round.round005_projection
python -m tools.perf_round.round005_pulse --nodes 128
python -m tools.perf_round.round005_consolidation
python -m tools.perf_round.round005_consolidation --profile-reconcile
python tools/check_connect_typing.py
python tools/check_documentation.py
python tools/generate_api_reference.py --check
```

Ignored local receipts retain detailed phases and measured source hashes:

- `.grafx-tmp/next-consolidation-final.json`, SHA256 `cd563384a0e6f8f8f975ab35759792a68bb80da634af106fd1a9494b7abbdfa0`.
- `.grafx-tmp/next-pulse-128-final.json`, SHA256 `4631392746b24029f4aa7d8b3788bdd46717cac2bd8c3c662b0d7aaa6f10083f`.
- `.grafx-tmp/next-projection-bench.json`, `.grafx-tmp/next-consolidation-profile.json`.
- `.grafx-tmp/next-grouped-regression.txt`, `.grafx-tmp/next-final-focused.txt`,
  `.grafx-tmp/next-end-to-end-baseline.txt`, `.grafx-tmp/next-static-consumer.txt`.
- Wheel `.grafx-tmp/v005-followup-wheel/okto_grafx-0.0.5-py3-none-any.whl`, SHA256
  `a5c30b8e242c629984d9c3b862617334495af25f0a98107a5e06c0e8784cea7e`.

Only [ROADMAP.md](../../ROADMAP.md) controls future work.
