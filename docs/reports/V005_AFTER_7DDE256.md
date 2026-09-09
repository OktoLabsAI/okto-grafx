# Projection throughput and algorithms after 7dde256

Approved eight-item implementation on `feature/v0.0.5`; no release, commit/push,
installation or live Pulse data action in this implementation task.

## Checkpoint 1–4

Implemented additive projected/bounded public scan, batch projection capture,
immutable bidirectional CSR adjacency and iterative strongly connected components.
Default full-row cursor behavior remains; projection identity is bound to the
one-shot cursor. Omitted properties remain schema/payload validated. Captures do
not bypass public read transactions or grant durable authority to adjacency.

`.grafx-tmp/round-7dde-checkpoint.xml`: **30 passed, 9.00 s**. Covers existing scan
contracts, old reader/foreign writer, byte limits, cancellation, corrupt omitted
payload, no omitted vector materialization, SCC oracle and a 1,500-node chain.
Twenty wide/vector-bearing nodes with batch_rows=8 used **3 scan calls**, maximum
batch 8; this is an operation-count observation, not a latency/RSS promise.

## Items 5–8 and corrective admission

Implemented bounded BFS reachability/unweighted shortest paths, PageRank with
explicit iteration-cap non-convergence, simple-undirected k-core, and optional
fixed-size-list vector Arrow import/export. No algorithm writes storage; immutable
adjacency is caller-retained topology, never a durable freshness certificate.

SCC is checked against an independent transitive-reachability oracle; PageRank
against a stationary linear-system solver; k-core against repeated k-peeling on
twelve seeded multigraphs. Path fixtures cover reverse/both directions, loops,
parallel-edge identity, isolates, equal-hop ties, depth and discovered-node bounds.
Logical-memory/work/cancellation failures refuse without partial results.

Arrow tests cover float32/float64, nullable vectors, schema metadata, fixed shape,
non-finite/NULL components, row/byte budgets, database reopen and native verification.
Late malformed batches roll back the entire import call while preserving earlier
caller staging. No silent precision or cross-store space remapping is introduced.

Review found `_stored_value` accepted existing `VectorValue` parameters without
checking their target embedding space. That bypass is closed in the native query
write door: store-local identity, precision and the existing vector engine's
dimension/active/finite/range/normalized admission now apply equally to encapsulated
parameters and lists. Native parameter-batch failures are tested for staging
atomicity. This is required for safe Arrow import, not a new storage format.

Focused checkpoint `.grafx-tmp/round-7dde-features.xml`: **34 passed / 14.821 s** across Arrow
import/vector interop, projected scan and algorithms. Additional deterministic
deadline tests in `.grafx-tmp/round-7dde-deadline.xml`: **5 passed / 2.115 s**, including
expiration during payload decode in both direct scan and graph capture, leaving
the caller reader usable. Existing scalar Arrow contracts remain tested.

## Documentation and compatibility

Updated README feature inventory, documentation index, configuration routing,
generated API/DTO reference, physical scan integration contract, graph algorithm
guide, Arrow vector guide, GX-CAP-8/9 routing specs, changelog and ROADMAP status.
The algorithm and Arrow vector examples are executed directly from the guides.
Performance records only the latest measured operation count, not inferred latency.

No version bump beyond the existing 0.0.5, new connection field, capability bit,
WAL record or file-format change. Public scan defaults remain full-row; new kwargs
are additive. Invalid encapsulated vector writes formerly admitted by the bypass
now fail closed. No production Pulse, reserved spec, deployment or platform-matrix
claim is inferred. Broader weighted algorithms/catalog/write-back remain roadmap.

## Final grouped regression

**Closed: all eight items delivered, 16,046 distinct passed, 18 attributed skips,
zero unresolved failures/errors.** Counts use the latest result for each
`classname::name`, not the sum of overlapping runs. The first broad run was not
all-green; its two findings and final-source reruns are explicitly retained below.

| Group / JUnit artifact under `.grafx-tmp/` | Result | Duration |
| --- | --- | ---: |
| `round-7dde-regression-core.xml` | 14,664 passed, 17 skipped, 2 failures later closed | 1,306.616 s |
| `round-7dde-regression-api.xml` | 1,374 passed, 1 skipped | 667.554 s |
| `round-7dde-final-query.xml` | 2,637 passed | 438.287 s |
| `round-7dde-final-corrective.xml` | 904 passed, including six new native admission tests | 27.424 s |
| `round-7dde-final-doc-surface.xml` | 2,388 passed | 40.693 s |

The broad root suite excludes API/corpus; those run separately with
`PULSE_CORE_BASELINE=D:/Projetos/Techridy/okto-pulse-core-corpus-baseline` and
`PULSE_COMMUNITY_BASELINE=D:/Projetos/Techridy/okto-pulse-community-corpus-baseline`.
Both use `PYTHONPATH=src`, default 60-second per-test timeout and the native suite's
existing markers. The final heap run covers cooperative decode-boundary checks
added after broad collection. No timeout, skip policy or semantic assertion was relaxed.

Reproduction commands (prepend those environment settings on Windows):

```text
python -m pytest tests --ignore=tests/api --ignore=tests/corpus --junitxml=.grafx-tmp/round-7dde-regression-core.xml -q
python -m pytest tests/api tests/corpus --junitxml=.grafx-tmp/round-7dde-regression-api.xml -q
python -m pytest tests/query --junitxml=.grafx-tmp/round-7dde-final-query.xml -q
python -m pytest tests/test_language_surface.py tests/query/test_write_preparation.py tests/api/test_arrow_vectors.py tests/api/test_projection_algorithms.py tests/api/test_continuation_documentation.py --junitxml=.grafx-tmp/round-7dde-final-corrective.xml -q
python -m pytest tests/test_language_surface.py tests/consumer/test_documentation.py tests/foundation/test_public_surface.py --junitxml=.grafx-tmp/round-7dde-final-doc-surface.xml -q
```

Corrective findings:

1. The new native validation rebuilt a valid canonical `VectorValue`, violating an
   existing identity-preservation test. Canonical immutable values now retain their
   identity **after** all admission checks; values needing float32 rounding receive
   a newly canonical value. The original assertion remains unchanged. Direct query
   tests additionally cover wrong space/dimension/precision, retirement, normalized
   declaration and rounding; the normalized test uses an explicitly normalized
   real database, not the general query fixture whose space is non-normalized.
2. The language checker interpreted English `indices` as Portuguese `indice` after
   its naive plural stripping. The docstring now uses `positions`; the checker is
   neither disabled nor exempted. This was a wording false positive, not a graph error.

Source behavior remains unchanged after these corrections; subsequent edits only
clarified docstrings/documentation, followed by the final documentation/surface run.

Completed final-source focused checks:

- `.grafx-tmp/round-7dde-final-heap.xml`: **249 passed / 2.011 s**.
- `.grafx-tmp/round-7dde-final-contracts.xml`: **1,563 passed / 28.210 s**;
  public surface, consumer/adapter documentation and executable new examples.
- `python tools/generate_api_reference.py --check`, `python tools/check_documentation.py`,
  `python -m ruff check .`, `git diff --check`: passed at this checkpoint.
- `python -I -S` with only this checkout's `src` explicitly added to `sys.path`:
  detached SCC/BFS/shortest path/PageRank/k-core passed after database close;
  vector descriptor construction succeeded and Arrow export refused its missing
  optional dependency. NumPy, PyArrow and google_crc32c were not imported. This
  is bare-source evidence, not an installed bare-wheel/platform-matrix claim.
