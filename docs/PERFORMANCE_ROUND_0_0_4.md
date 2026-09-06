# Performance round 0.0.4 — finite execution plan

This document is the execution authority for the Grafx 0.0.4 performance round. It consolidates
Claude's measured surveys, Codex's adversarial review, and direct measurements of the current
Okto Pulse workload. It is deliberately finite: later profiling may reorder or reject an item,
but it does not add work to the round unless it exposes a correctness defect or an unbounded
complexity cliff on an already selected path.

Branch: `feature/v0.0.4`

Base: `ea4b5ff` (`0.0.3`)

Package version: `0.0.4`

## Non-negotiable invariants

Every optimization must preserve:

- independent multi-process and multi-thread readers and writers;
- snapshot isolation, both OCC validations, and writer fencing;
- WAL order, acknowledged durability, recovery and restore;
- catalog, heap and index consistency, including fail-closed corruption checks;
- the public result, error, pagination, ordering and budget semantics;
- bounded memory with deterministic refusal or canonical fallback.

Performance evidence never authorizes weakening one of these properties. Grafx-specific behavior
in Okto Pulse belongs to Community adapters and composition roots; Pulse Core remains backend
agnostic.

## Evidence used for selection

The evidence set is:

- `FABLE_PERFORMANCE_GRAFX.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_5.md`;
- `.grafx-tmp/levantamento2/L5_ADENDO_MEMORIA_QUENTE.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_6.md`;
- `.grafx-tmp/levantamento2/LEVANTAMENTO_6_TECNICAS.md`;
- the previous execution record in `docs/PERFORMANCE_ROUND_0_0_3.md`;
- direct Grafx and installed-Pulse measurements below.

On the real board database, with Pulse stopped, a read-only Grafx handle measured:

| Operation | Observed time/result |
|---|---:|
| Read-only open | 0.925 s |
| First 500-node page | 0.667 s; 2,069 rows scanned |
| Second 500-node cursor page | 0.385 s; 2,069 rows scanned |
| 66 relationship statements, 770 rows, cold | 1.833 s |
| Same relationship batch, warm | 1.100 s |
| Two 500-node reads sequentially | 1.603 s |
| Two 500-node reads on independent handles | 1.492 s |
| Read during a public writer transaction | 0.671 s |

After the Community integration stopped executing blocking graph work on the async event loop,
two concurrent graph REST requests took 4.32 s and 7.92 s while `/boards` still completed in
0.22 s. The event-loop serialization is closed; the remaining overlap is engine CPU/GIL and
per-statement work.

## Adversarial corrections to the surveys

1. The original KGRUN-2 estimate does not apply to the production predicate. Seven repeated
   `coalesce` subexpressions fall back to the oracle, and a naive prototype ignored
   `row.computed`. The selected design is closure-based evaluation plus a `coalesce` leaf and
   lazy per-row common-subexpression elimination (EXEC-CSE), with an explicit computed-row
   guard. Measured opportunity: 19.6–20.3% of the KG page and 17.3% of vector search.
2. A 64 MiB buffer pool cannot hold the eager directory of the Pulse schema's indexes:
   162 indexes × 65 pages = 10,530 pages, versus 8,192 frames. A 256 MiB budget is therefore a
   consumer configuration candidate, not a new engine default.
3. CONCUR-2 has the largest measured upside, but also changes the granularity of physical
   identity proof. It is not Wave 1 work. It requires a formal equivalence proof and hostile
   multi-process tests before promotion.
4. `vector_math="auto"` currently selects the pure implementation. NumPy measured 9.2% faster
   for the vector workload, but changing the default is a determinism policy decision, not a
   transparent code optimization.
5. KGRUN-6 has zero value on the measured denominator because the accelerated port already runs.
   The useful decode target is skipping the 39 columns the consumer did not request.
6. Building indexes at the end, operator batching that changes error timing, larger pages,
   Bloom filters, quantization and PyPy were measured as neutral, slower or semantically unsafe;
   they are not implementation targets for this round.

## Newly identified opportunities

### CURSOR-1 — ordered cursor access

Each 500-node page scanned all 2,069 visible rows. Walking all pages is therefore `O(P*N)`, even
though a cursor is supplied. An ordered `(created_at, id)` access path, or a generation-fenced
cursor materialization, could make continuation proportional to the remaining page. This is a
structural candidate: record and design after Wave 2, do not improvise a format change in Wave 1.

### BATCH-REL-1 — shared preparation for relationship-table batches

The warm relationship batch still costs about 1.10 s for only 771 scanned rows across 66
statements. The transactional primary-key memo may remove much of that fixed work. Re-profile
after STO-M1; only then consider a batch API that shares a snapshot and statement preparation
while preserving each statement's independent result and error boundary.

### CONC-CPU-1 — CPU/GIL after legitimate I/O concurrency

Independent Grafx handles no longer block unrelated Pulse HTTP work, but two heavy graph reads do
not scale linearly. EXEC-CSE and decode/projection work therefore precede adding more application
parallelism. Process pools or a native accelerator are not selected in Wave 1.

## Fixed implementation order

### Wave 0 — release line and existing transaction-local PK work

Status: **implemented and pushed** (`10b7520`).

- keep the existing dirty-table automatic primary-key index overlay;
- prove insert/update/key-change/delete and pending-reference containment;
- prove two endpoint seeks do not fall back to a node-table scan;
- bump package and README versions to `0.0.4`;
- do not overwrite unrelated pre-existing worktree changes.

The focused baseline passes 65 primary-key/dirty-table tests. The full query slice reached 100%;
one instrumentation-only expectation was updated from one unrelated landing decode to zero after
the new exact dirty-table seek, and its focused rerun passed. Ruff and `git diff --check` pass.

### Wave 1 — highest return without protocol changes

Engine/query lane:

1. KGRUN-M4: fix landing-memo charging so the cache does not fall from a performance cliff;
   enforce a deterministic memory cap and canonical fallback.
2. KGRUN-M3: choose scan versus seek before encoding up to two 500-key sets.
3. RELSEEK-M4: avoid vector materialization only for an internal endpoint landing that does not
   project the vector; all validation and refusal paths remain.
4. EXEC-CSE, including KGRUN-M1: closures, exact `coalesce` leaf, lazy per-row CSE and the
   `row.computed` guard. No `exec` code generation in this wave.
5. KGRUN-M2(a): stateless pure cosine scorer preparation; no authority-bearing memo.
6. VECTOR-6: test the identity-index gate before the O(N) proof.
7. LV-3: resolve active indexes once per row in commit accounting/staging. **Implemented
   locally:** the canonical manager now carries one immutable table-local projection from WAL
   quota prediction into delete/insert staging, while custom managers and overridden hooks keep
   the observable legacy path. The produced-versus-expected WAL invariant remains mandatory.

Storage/identity lane:

8. STORID-1 + STORID-M2: remove the redundant existence probe before page count and enumerate
   once, preserving exact missing/case/symlink error ordering and types. **Implemented locally:**
   established files now take one page-count proof; only the ambiguous zero/missing boundary of a
   narrow storage collaborator pays `exists`; legacy nonce inventory performs one directory list.
   The complete index plus storage-device/fault-adapter regression slice passes. Pushed in
   `cbab991`; `abe068c` additionally preserves adapters that report exact absence as Python's
   `FileNotFoundError`, without translating permission, case, link or corruption failures.

Pulse Community lane, maintained in the Pulse repository rather than Grafx:

9. set the Grafx handle/pool budget to 256 MiB for the Pulse schema after measuring 128/256 MiB;
10. deliver vector components through the Community sink so Grafx remains the validator;
11. batch safe `IN` lookups, reduce sink commits only across explicitly recoverable units, and
    collapse the Grafx-specific two-hop fan-out without changing Core abstractions.

### Wave 2 — medium changes after Wave 1 re-profile

1. WRITE-1, then NATVER-2, with peak-memory and retained-lifetime measurements.
2. NATVER-3 only after a discriminating CRC microbenchmark.
3. CKPTCERT-1 with heap and index corruption injected independently.
4. One generated decode plan for KGRUN-1/LADYBUG-M1/NATVER-1; it must pass a 10,000-case hostile
   corpus with the same exception class, field and offset as the canonical decoder.
5. KGRUN-3: project only retained rows after the bounded top-k/order stage.
6. LADYBUG-M4: indexed `DETACH DELETE` in both directions, including pending transaction rows.
7. STO-M1: transaction-scoped primary-key resolution memo. First re-profile the Wave 0 overlay;
   implement only the residual and keep ownership/generation inside the transaction.

### Wave 3 — small residuals and one re-profile

1. WRITE-4/M1/M2 with a crash matrix at every segment boundary.
2. WRITE-M3 and WRITE-5.
3. STORID-M1: Windows exact-path fast path with a secure cross-platform fallback and parity for
   Unicode, case, symlinks and junctions.
4. Re-run the direct KG page, relationship fan-out, vector, transfer, open, recovery and
   concurrent-reader workloads once for the accumulated implementation.
5. Decide whether CURSOR-1 and BATCH-REL-1 remain material; do not add smaller residuals.

## Explicit decision queue

These items are not silently included in the waves above:

| Priority | Item | Required proof/decision |
|---:|---|---|
| 1 | CONCUR-2, one exact certificate per file/transaction | Snapshot-by-epoch equivalence, publication races, file replacement, corruption, crash and two-process matrix; must preserve fail-closed behavior. |
| 2 | D-8/STORID-2 minimal descriptor identity proof | Exact difference between `strict` and `generation`, including Windows open-descriptor semantics. |
| 3 | `exec` code generation | Security/debuggability decision; only the incremental gain over EXEC-CSE counts. |
| 4 | `vector_math="numpy"` default | Determinism, dependency and cross-machine ranking policy. |
| 5 | `retain_lease` for bulk load | Explicit writer-exclusion policy and bounded timeout behavior. |
| 6 | persisted high-water and CURSOR-1 | On-disk format/migration/recovery design. |

No decision above may remove multi-reader/multi-writer operation or weaken WAL, durability,
snapshot, OCC or consistency.

## Test cadence and acceptance

- Each item gets focused correctness, hostile-boundary and component A/B tests.
- Several low-risk items are accumulated before the related regression slice.
- Long crash, recovery, multi-process and full-suite runs occur at coherent wave checkpoints,
  not after every small patch.
- A marginal or noisy wall-time result is recorded without becoming a gate. A structural
  reduction may be accepted when equivalence tests pass and the removed work is counted.
- Any exception or semantic divergence found during implementation is fixed before the item is
  accepted; no known blocker is carried into the next wave.

## Coordination record

Claude and Codex reached consensus on 2026-09-06 after the L5/L6 surveys and adversarial review.
The first isolated implementation handoff is
`hof_75b4003eac9f4ebe84f16b2f2bf42f68` (KGRUN-M4 + KGRUN-M3 + RELSEEK-M4), with creator
verification required. Codex owns Wave 0, the first storage/identity lane and final integration.
Pulse-specific work remains in Community adapters and composition roots.
