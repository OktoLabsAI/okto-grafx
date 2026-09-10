# Weighted analytics and tabular interop after 970aa1e

September 9, 2026; approved eight-item continuation on `feature/v0.0.5`.
All eight implementation slices are delivered; the final grouped regression passed.
This task does not publish, commit/push, install Pulse or consume reserved live data.

## Scope and checkpoints

1. Immutable retained ProjectionLookup with expected constant-time identity lookup;
   BFS workspace follows discovery instead of preallocating/charging all V.
2. Simple-undirected bucket k-core replaces heap/stale-pair peeling, O(V+E).
3. Explicit NumPy PageRank with bounded numeric buffers, Python default, typed
   missing-dependency refusal and cooperative kernel/iteration checks.
4. Per-relationship numeric weights captured by projected scans in one snapshot;
   explicit finite non-negative values, NULL policy and retained-memory charge.
5. Non-negative Dijkstra with deterministic physical-edge ties and bounded heap,
   discovery, distance and cooperative work.
6. Weighted/personalized PageRank with seed-based dangling redistribution,
   overflow-safe normalization and independent stationary-system oracle.
7. Explicit Arrow-backed Pandas frames; bounded materialization and whole-call
   atomic native staging without NaN/NULL or dtype inference.
8. Local Parquet batches with exact metadata, file/group/batch limits, trusted
   directory policy and complete atomic no-overwrite export publication.

Checkpoint 1–4: `.grafx-tmp/round-970-checkpoint.xml`, 15 passed. A prepared
10,000-node picture admits a two-node local BFS path with max_work=10 and workspace
for two discovered nodes, demonstrating no repeated V identity scan. K-core matches
the previous heap oracle with observed work <=10(V+E+1) on five deterministic sizes.
This is operation-count evidence, not a Pulse UI speedup or RSS measurement.

Weighted checkpoint: `round-970-weighted.xml`, 4 passed. Dijkstra agrees with
Bellman–Ford on 12 seeded multigraphs in all directions; weighted/personalized
PageRank agrees with an independent linear-system solver on Python and NumPy.

Interop checkpoint: `round-970-interop.xml`, 10 passed. The first run exposed
Arrow's inability to decode outer-NULL fixed-size lists from Parquet. The file
adapter now uses tagged variable lists and validates/reconstructs fixed dimensions
at the API, preserving NULL and vector identity. Two original failure fixtures
incorrectly expected scalar DOUBLE NaN rejection; native DOUBLE supports NaN.
Tests now assert NaN preservation and use a real late primary-key conflict to
prove rollback of the whole call while retaining prior caller staging.

## Contract and documentation

No native storage format, capability bit, connection field, OCC rule, read/write
right, COMMIT proof or WAL barrier changes. Default projection/PageRank behavior is
unweighted Python. New algorithms are detached and do not hold reader leases.
Imports reuse native executemany and caller-owned commit, not another write path.

README inventory, docs index, roadmap, capability specs, generated API/DTO appendix,
configuration routing, graph guide, Pandas/Parquet guide, changelog and compatibility
notes are updated. Recipes execute directly from their Markdown. No broad capability
completion, minimum-dependency/platform certification or live Pulse gain is implied.

Parquet publication is atomic visibility, not crash-durable directory publication;
the trusted directory policy is not a sandbox against hostile namespace races.
Native Arrow/NumPy kernels and allocator temporaries are not preemptible/RSS-capped.
These boundaries are explicit in the consumer guides, not hidden acceptance debt.

## Final grouped regression

Consolidated acceptance: **16,121 distinct passed, 18 attributed skips, no
unresolved failures/errors**. Counts deduplicate `classname::name` across the
general, API/corpus, tools and final focused reports, with the latest result
replacing earlier coverage; checkpoints are not added to this total.

API/corpus group: `round-970-regression-api.xml`, **1,401 passed, 1 skipped,
0 failures/errors, 834.883 s**. Final interop/recipe/optional-isolation run:
`round-970-final-interop.xml`, **19 passed**, including network path refusal before
any filesystem lookup, publication races and malformed vector encodings.

General root (excluding API/corpus/tools): `round-970-final-core.xml`, **14,353
passed, 17 attributed skips, 0 failures/errors, 1,592.437 s**. The default
60-second timeout and existing explicit longer markers (for example, the native
multi-process smoke's 900 seconds) were retained, not increased.

Final algorithms/capture: `round-970-final-algorithms.xml`, **24 passed**. Oversized
weight selections refuse by length before copying their keys. The complete tools
group: `round-970-final-tools-external.xml`, **365 passed / 109.894 s**, no skips.

Final documentation/public-surface coverage: `round-970-final-doc-surface.xml`,
**2,450 passed / 71.429 s**, no skips/failures/errors. The host crashed after the
completed long-regression reports had been saved, during this final check. On
resumption their XML reports were verified intact; only the unfinished check was
rerun. This host incident is not evidence of database crash-recovery correctness.
Documentation links/anchors, all 39 connection fields, public signatures/DTOs,
11 preserved source plans, generated API freshness and Ruff checks passed.

The first general attempt was interrupted around 86% by the Windows timeout while
`tests/tools/test_perf_round.py` cloned the full local repository history. It emitted
no final JUnit and is **not counted as acceptance**. The clean-pin fixture now uses
an independent shallow local-transport clone, verifies the exact pinned SHA and
bounds clone/checkout subprocesses to 30/20 seconds, without raising test timeouts.
A packaging assertion was also updated for the approved new optional Pandas extra.

Moving tool fixtures inside the repository then produced three legitimate
provenance/source-overlap refusals. These instruments require non-checkout temporary
paths. Their complete rerun at the standard external temp location passed; no
production guard was weakened. The remaining general suite uses its own temporary
directory on D: and excludes API/corpus/tools, which have separate complete reports.

## Packaging and dependency isolation

A `python -I -S` subprocess with no site packages passes the Python graph path and
typed missing NumPy/Arrow refusals. Separate optional-dependency tests cover installed
NumPy 2.5.2, Pandas 2.2.3 and PyArrow 19.0.1 on Windows/Python 3.13.1.

The local 0.0.5 wheel built without isolation; its 182 Python files matched source
bytes at the checkpoint. Wheel-origin imports (asserted, not editable-source imports)
passed weighted graph/NumPy and Pandas/Parquet consumer recipes with native commit
and persistent reopen. No global installation or Pulse action. The wheel probe
explicitly exposes the existing optional site packages, whereas the bare subprocess
does not; they are different checks, not a claim of dependency-free NumPy execution.

The optional `pandas` extra and its exact packaging assertion are added together.
Both CI extras profiles include it; the compatibility workflow now includes the
new algorithm/interop tests. The remote matrix has not been run in this local task.

## Latest short kernel observation

`PYTHONPATH=src python tools/measure_projection_kernels.py`, run after all long
regressions had finished. Windows/Python 3.13.1, NumPy 2.5.2; seed 970, 5,000 nodes,
30,000 physical edges, prepared lookup/CSR, three calls per kernel. Prepared
logical charge is 30,732,288 bytes; this is not an RSS measurement. No machine-idle
attestation, capture/storage cost or Pulse UI speedup is claimed.

| Current kernel/configuration | Median ms | Three samples ms |
| --- | ---: | --- |
| PageRank Python | 389.270 | 328.785, 442.595, 389.270 |
| PageRank NumPy | 12.687 | 12.828, 12.202, 12.687 |
| Bucket k-core | 33.308 | 35.017, 33.308, 24.433 |

Both rank paths converged in 24 iterations at tolerance 1e-10 with residual about
7.70e-11, and their scores passed absolute 1e-11 parity. Maximum core number was 8.
This fixed observation does not establish a universal speedup or add a timing gate.
