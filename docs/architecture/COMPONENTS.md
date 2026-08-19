# Okto Grafx — Component map, build waves and the builder/critic protocol

Read `CONTRACT.md` first. This file says **who builds what, in what order, and how the review loop works**.

## Component register

| ID | Component | Owns (write only here) | Realizes |
|----|-----------|------------------------|----------|
| **C0** | Foundation | `domain/errors.py`, `domain/ids.py`, `domain/ports/**`, `runtime/**`, `pyproject.toml`, `src/okto_grafx/{__init__.py,errors.py,py.typed}`, `tests/conftest.py`, `tests/foundation/**`, `tests/test_import_boundary.py`, `tests/test_language_surface.py`, `tests/test_platform_parity.py` | TR-1, TR-2, TR-6, TR-9, BR-8, G1–G5 |
| **C1** | Storage core | `domain/model/**`, `domain/page/**`, `engine/buffer_pool.py`, `engine/heap_store.py`, `engine/catalog_store.py`, `adapters/codec_v1.py` | FR-13, BR-8, page/record formats |
| **C2** | Storage adapters | `adapters/storage_local.py`, `adapters/storage_memory.py`, `adapters/storage_fault.py` | FR-1, FR-5, FR-16, TR-3, AC-9, AC-10, AC-11 |
| **C3** | Process coordinator | `adapters/coordination_local.py`, `adapters/clock_system.py`, `engine/coordination.py` | FR-7, BR-7, AC-6, AC-7 |
| **C4** | WAL | `domain/wal/**`, `engine/wal_manager.py` | FR-5, FR-6, TR-4, BR-4, BR-10, AC-8, AC-9 |
| **C5** | Transaction manager | `domain/txn/**`, `engine/txn_manager.py` | FR-2, FR-3, FR-4, BR-6, BR-9, AC-1, AC-2, AC-3 |
| **C6** | Recovery · ledger · quarantine · verify | `domain/recovery/**`, `domain/ledger/**`, `domain/verify/**`, `engine/recovery_manager.py`, `engine/ledger_store.py`, `engine/quarantine.py`, `engine/verifier.py` | FR-8, FR-9, FR-10, FR-11, TR-5, BR-1, BR-2, BR-3, AC-4, AC-5, AC-12 |
| **C7** | Index framework | `domain/index/**`, `engine/index_manager.py` | FR-12, BR-11, SD-3 |
| **C8** | Observability | `adapters/metrics_*.py`, `adapters/events_logging.py`, `engine/metrics_catalog.py`, `dashboards/**` | FR-14, TR-7, BR-12, OR-1..OR-6 |
| **C9** | Vector subsystem | `domain/vector/**`, `adapters/vectormath_pure.py`, `adapters/vectormath_numpy.py`, `engine/vector_engine.py` | VEC FR-1..FR-3, FR-5..FR-7, FR-9, VEC BR-1..BR-5, BR-7 |
| **C10** | Query engine | `domain/query/**`, `engine/query_engine.py` | D3, VEC FR-4, VEC BR-6, AC-7 |
| **C11** | Public API | `api/**`, `engine/database.py` (may EXTEND `src/okto_grafx/__init__.py` re-exports, which C0 seeded) | FR-1, FR-11, public surface |
| **C12** | CLI | `cli/**` | operator surface |
| **C13** | Bench · calibration · CI | `bench/**`, `.github/workflows/**`, `tests/bench/**` | FR-15, FR-16, TR-8, VEC FR-8, AC-14, VTS-9, VTS-10 |

## Build waves

```
W0  C0                       (blocking — everything depends on the frozen ports)
W1  C1 ‖ C2 ‖ C3 ‖ C8
W2  C4 ‖ C5
W3  C6 ‖ C7 ‖ C9
W4  C10 ‖ C11
W5  C12 ‖ C13
W6  integration hardening: full suite, cross-platform, spec-scenario coverage matrix
```
A wave starts only after every component of the previous wave has a **critic sign-off**.

## Builder/critic protocol

**Builder**
1. Read `docs/architecture/CONTRACT.md`, `docs/specs/SPEC-M1.md`, `docs/specs/SPEC-VEC.md`, and the
   already-delivered code it depends on.
2. Write production code **only inside its owned paths**, plus `tests/<component>/`.
3. Run `python -m pytest tests -q` — the **whole** suite, not just its own — and leave it green.
4. Report: files written, public symbols added, spec IDs covered, test counts, and any contract
   conflict it hit (never silently deviate).

**Critic (blind)**
1. Receives ONLY: the component's scope, the contract, the specs, and the file paths.
   It does **not** receive the builder's self-report, rationale, or claims of success.
2. Reads the actual code, runs the tests itself, and tries to break the implementation
   (adversarial probes, edge cases, concurrency, platform assumptions, hidden state).
3. Produces a verdict: `SIGN-OFF` or `REJECT` plus a numbered defect list — each defect with
   file:line, why it is wrong, and the spec/contract clause it violates.
4. A critic must never fix code. It only finds and proves defects.

**Loop**: `REJECT` → the defect list goes straight back to the builder → builder fixes → a fresh
blind critic pass. Repeat until `SIGN-OFF`. No component advances to the next wave on a `REJECT`.

**Quality bar for sign-off** — all of:
- every Definition-of-Done item in `CONTRACT.md` §11 satisfied;
- no defect the critic can demonstrate with a failing test it writes;
- the component's owned spec IDs are each traceable to a test;
- the whole `pytest` suite green on the current platform.
