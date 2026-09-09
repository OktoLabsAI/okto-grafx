# Reusable analytics and local ingestion after a4dd85a

September 9, 2026, `feature/v0.0.5`; approved eight-item continuation after
`a4dd85a` / queue commit `6dec5b5`. **All eight slices are implemented and locally
validated. DoD closed: 16,228 distinct passed, 18 attributed skips, no unresolved
failures/errors**, after the corrective reruns explicitly described below.
No commit/push, release, Pulse installation or live-data consumption in this task.

## Delivered scope

1. `with_pagerank`: opt-in immutable normalized transition/share/dangling state,
   backend/weight-specific, with immutable NumPy byte buffers and no cached ranks/seeds.
2. `with_simple_topology`: expected-linear deduplication, immutable retained neighbors
   reused by k-core/communities; original directed physical multigraph preserved.
3. `to_networkx`: optional bounded MultiDiGraph copy with store/table/record identity,
   physical edge keys, loops/multiplicity/weights; independent small-graph conformance.
4. `label_propagation`: deterministic node-order asynchronous voting, minimum identity
   tie break, unweighted simple topology, explicit iteration/convergence/work bounds.
5. `projection_arrow_batches`: bounded node/edge/aligned scalar result export with
   explicit endpoints and UUID/LSN provenance; physical IDs are lossless decimal strings.
6. `PolarsFrame`/`to_polars`/`import_polars`: eager optional metadata-bearing bridge,
   exact dtype/NULL/NaN/vector identity, bounded batches and native staging savepoint.
7. Typed local CSV readers/import: required header/schema/root, strict bounded UTF-8
   multiline parsing, explicit NULL/dialect/scalar codecs, whole-call rollback.
8. Typed JSONL readers/import: exact scalar object keys, duplicate/missing/unknown/
   nested/nonstandard numeric refusals, shared bounds/root policy and native atomicity.

No native engine storage/transaction code was modified. Multi-reader/writer access,
both OCC checks, fencing, COMMIT proof, WAL and durability remain unchanged. Detached
analytics/exchange own no live store lease. Import uses the existing public
`executemany` savepoint and caller-owned commit; file batching is not partial commit.

## Evidence and corrective checkpoints

- `round-a4-checkpoint.xml`: 8 new tests passed for 1–4; 19 existing projection,
  weighted-path and PageRank tests also passed.
- `round-a4-features-final.xml`: 43 combined new feature/isolation tests passed.
- `round-a4-recipes-final.xml`: all 8 documentation recipe tests passed.
- `round-a4-adversarial.xml`: 10 prepared-graph tests passed, including an independent
  label-propagation sweep, exact avoided 5,000-edge k-core preparation walk and a
  prepared projection surviving another participant's write and source close.
- `round-a4-text-final.xml`: 48 text tests passed, including localized scalar errors,
  quoted NULL/empty strings, malformed headers/quotes, midstream cancellation rollback,
  byte/row/batch/work limits, native reopen and file-change/early-close checks.
- `round-a4-tools.xml`: 365 passed, no skips/failures, 140.440 seconds.
- `round-a4-api.xml`: 1,449 collected, 1,446 passed, 1 attributed skip and the two
  already-corrected recipe setup failures, 769.708 seconds. That process had collected
  the old setup before its correction; the full 8-recipe rerun above passes. Do not
  describe the initial API invocation as uninterrupted green acceptance.
- `round-a4-algorithms-final.xml`: 30 passed after final resource-accounting review.
  Label propagation now charges both initial and returned label copies; an isolated
  100-node picture refuses at 99/299 work units and admits the complete 300-unit run.
  This closes undercounted cooperative work, without changing graph answers.
- `round-a4-public-docs.xml`: 1,605 public-surface/documentation checks passed,
  no skips/failures, 53.669 seconds. Full annotations and operation-limit documentation
  checks include the new modules and all eight TextImportLimits fields.
- `round-a4-core.xml`: 14,412 collected, 14,391 passed, 17 attributed skips and four
  missing-annotation failures, 1,538.509 seconds. The failures were annotations in
  `numpy_projection.py`, `graph_interop.py`, `polars.py` and `text_import.py`, fixed
  before the full public-surface/documentation rerun above. No runtime, durability,
  concurrency or native storage test failed in this group. Existing timeout limits
  and platform markers were retained, not relaxed.
- `round-a4-final-interop-docs.xml`: 12 passed / 4.178 seconds, including empty-frame
  Polars dtype admission and wrong vector-space metadata, final recipes and isolation.
- Consolidation keys are `classname::name`, with corrected focused results replacing
  earlier failures, not adding overlapping passes. Inputs are core, API/corpus, tools,
  public/docs, final algorithms, final text and final interop/docs reports in that order.
  Checkpoint counts are not separately added. This is complete grouped local coverage
  with corrective reruns, not one uninterrupted green process or remote certification.
- NetworkX SCC, degree, unweighted path and simple k-core conformance uses seeded
  1/5/20-node fixtures; it is not every NetworkX algorithm or a label-update parity claim.
- Python `-I -S` tests import the new modules without optional site packages and
  assert typed refusal when absent NumPy/Arrow/Polars/NetworkX features are selected.
- New fixtures initially used non-exported `Uuid`, DDL `BYTES` instead of `BLOB`, a
  NULL token longer than a deliberately tiny field limit and Windows CRLF with an LF
  expectation. Corrected the fixtures without changing native types or newline bytes.
  Recipe setup also initially tried DDL through read-only `db.execute`; setup now
  uses an explicit write transaction. These failed attempts are not acceptance runs.

## Performance decision

A single cProfile investigation over 5,000 nodes/30,000 edges (seed 970), with five
weighted NumPy PageRank calls using different seeds, recorded 0.873 seconds under
instrumentation; numeric kernel cumulative time was about 0.061 seconds. Transition
normalization/preparation dominated, justifying opt-in reuse. Profiling overhead makes
these attribution numbers unsuitable as absolute latency or a Pulse speedup claim.
No percentage acceptance gate was added. After all regressions finished,
`PYTHONPATH=src python tools/measure_prepared_analytics.py` measured the fixed synthetic
5,000-node/30,000-edge fixture with five different personalization seeds. Python
3.13.1 / NumPy 2.5.2, weighted float64 PageRank tolerance 1e-10, at most 100 iterations.
All rank calls converged, prepared/reference scores agreed within 1e-11 and k-core
outputs matched exactly. Source capture/CSR/import warm-up are outside the timed calls.

| Latest prepared operation | Median ms | Five samples ms | One-time preparation ms |
| --- | ---: | --- | ---: |
| Weighted personalized NumPy PageRank | 7.645 | 9.933, 7.645, 11.780, 7.290, 7.168 | 39.586 |
| k-core with retained simple topology | 34.985 | 40.519, 35.524, 34.985, 33.207, 32.437 | 32.199 |

The alternating unprepared control calls in that same latest source had medians
63.562 ms (rank) and 57.982 ms (k-core); these are runtime option comparisons,
not historical version-to-version or Pulse measurements. Retained picture logical
charges are 48,336,384 bytes (rank) and 40,656,384 bytes (simple topology), not RSS.
No machine-idle attestation, native DB I/O, write-throughput or UI-speedup claim.
The instrument records both control and prepared samples and checks answer parity,
without a timing threshold. Preparation is paid once and is optional.

## Documentation and acceptance boundary

README capability inventory, roadmap, API signatures/DTOs, operation defaults,
graph/interop/text guides, executable examples, capability routing and CI optional
profiles are updated. `DatabaseConfig` remains 39 fields. Optional extras add
`networkx>=3` and `polars>=1` (with `pyarrow>=14`). Polars was installed into a local
ignored test dependency directory, not the global Pulse environment.
Local tests do not certify minimum dependency versions, remote CI or other platforms.

The local 0.0.5 wheel built with `python -m build --wheel --no-isolation`; all 185
package Python files matched current source bytes. A separate `python -I` consumer
asserted wheel-origin imports (not the editable checkout), then exercised CSV/JSONL
and Polars native staging, weighted prepared NumPy PageRank, simple topology/communities,
NetworkX/Arrow exchange, commit and persistent reopen. It passed. Optional packages
were explicitly exposed from the existing user site/local test directory; that probe
is distinct from the dependency-free `-I -S` test. No installation or publication.
The wheel was rebuilt after the final label-copy work-accounting fix; source-byte
parity and the complete isolated consumer smoke passed again on the final artifact.

Read the explicit limits: logical memory is not RSS; optional kernels/parsers are not
preemptible; trusted local roots are not hostile-race sandboxes; text input metadata
checks do not provide a filesystem content snapshot; exported batches can precede a
later failure; detached source objects remain caller-owned. These are documented
boundaries, not claims of solved external scans, graph backup, persisted catalog or
automatic write-back.
