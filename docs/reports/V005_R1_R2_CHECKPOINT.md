# 0.0.5 R1–R2 checkpoint: cold validation and checksum isolation

September 8, 2026. `feature/v0.0.5`, base `710dcb53541205b4a28dace787bfc656953c73ba`
plus working changes. [Active roadmap](../../ROADMAP.md#next-proposed-round-after-n1n4-closure).
R1/R2 are locally validated within the bounded scope below. R3/R4 were not started.
No commit/push, release, production data change or global Pulse installation occurred.

## R1 — bounded reduction in cold catalog admission

The existing complete synthetic Pulse consolidation again exposed independent
reader admission and first-reader checkpoint as residual costs. Profiling retained
the checkpoint in the operation: no warmup relocation or skipped phase. The profile
showed substantial time in file opening/revalidation and complete catalog decoding;
`schema.is_identifier` accounted for 1,119.989 ms cumulative in that **instrumented**
run. Profiling Python per-character calls exaggerates their uninstrumented relative
cost, so that number is attribution, not a predicted wall-clock saving.

For exact built-in strings, identifier validation now uses `str.isascii()` and
`str.isidentifier()` with the unchanged length bound. Over ASCII the latter accepts
the same grammar as the original letter/underscore plus alphanumeric/underscore
walk. Subclasses keep the original path, including its hooks. This removes the
Python generator/frame dispatch for each character from repeated catalog/index
validation. No identifier, catalog object, page or authority is cached.

Tests exhaust all 16,384 ASCII pairs, length boundaries, non-ASCII codepoints below
65,536, invalid object types and subclass behavior. A call-frame test proves the
exact-string path no longer dispatches the old per-character Python generator.
Latest component sample: **2.615 ms median for 10,000 validations**, seven repetitions.
There is no timing assertion or performance percentage gate.

This is a localized CPU improvement, **not closure of all cold-open cost**. File
identity checks, index admission, recovery preflight, checkpoint phases and both
OCC validations remain. The required IO/revalidation cost was not removed or hidden
behind an authority cache. Further redesign is outside this bounded checkpoint.

## R2 — one validated selection per database

Normal `connect` / `open_database`, including caller-supplied `PortRegistry`, now
capture the existing validating checksum installer in an execution-local selection
scope. Each returned database retains an immutable function/name pair. Public
operations, transaction-manager participant sections and close bind that pair and
restore the previous one on exit, including nested cross-database calls, exceptions
and process-control signals. The transport is supplied by runtime through the pure
`ScopedValue` contract; Domain/Engine do not import thread/task mechanisms.

Opening another database does not modify this selection or the standalone default.
No operation-wide global lock is introduced. The bounded native acceptance-proof
memo remains shared; it stores validation evidence, not mutable per-handle policy.
Native absence still refuses explicit `native` before store IO, while `auto` may
select pure. Injected providers retain runtime oracle verification. All accepted
providers produce exactly the same CRC-32C bytes; no format/WAL/capability change.

For compatibility, standalone `install_crc32c` / `install_checksum` still select
the low-level default outside database operations. Manual low-level construction
does not implicitly bind a database selection. A callback invoking the standalone
installer cannot replace the active database's captured provider.
[Configuration and integration boundary](../CONFIGURATION.md#checksum-isolation-in-005).

Tests cover real pure/auto providers, ambient installer changes, two simultaneous
threaded writers, nested calls and exception restoration, explicit missing-native
refusal and automatic fallback, custom registries, different selectors on one
physical store with a pinned reader, checkpoint, verify and reopen.

## Validation receipts

Counts overlap and must not be added as distinct coverage.

| Scope | Result |
| --- | --- |
| Grouped storage/schema/checksum, transactions, recovery, WAL, indexes, public APIs, bootstrap, import boundary and documentation/language contracts | **5,953 passed, 1 platform skip, 517.73 s** |
| Complementary configuration/public-surface/documentation and isolation group | **1,402 passed, 30.30 s** |
| Final seven checksum-isolation tests, including cases added after the grouped run started | **7 passed, 7.96 s** |
| Final identifier grammar and call-frame tests | **3 passed, 0.13 s** |
| Pulse Community transaction/provider, read-lane and orchestrator tests against current Grafx source | **105 passed, 36.74 s** |
| Full isolated synthetic consolidation | PASS: **4 nodes, 4 edges, 4 references, 4 discovery digests, 1 ACK**, empty second tick |
| Wheel build and install into separate validation environment | PASS; installed-wheel schema/data, metadata/history, checkpoint/reopen and qualified transfer smoke passed |
| Static/docs | Changed Python files pass Ruff, generated API freshness and documentation checks pass. Checksum strict mypy retains two pre-existing integer-narrowing diagnostics; no new diagnostic in the selected comparison |

The grouped run had no failures. An initial profile invocation omitted the repository
root from `PYTHONPATH` and could not import its helper; corrected invocation succeeded.
That setup failure is not runtime validation or a product failure.

Latest full synthetic values (same host also running regression): reconciliation
**3,791.805 ms**, consolidation commit **1,680.525 ms**, worker through durable ACK
**2,618.327 ms**. They certify completion/shape, not a controlled A/B speedup; native,
SQLite, scheduling and cold admission remain inside their respective boundaries.
The 19 reserved production specs were not consumed.

Local receipts:

- `.grafx-tmp/v005-r1-r2-regression.txt`
- `.grafx-tmp/v005-r2-closure.txt`, `v005-r2-final-isolation.txt`
- `.grafx-tmp/v005-r1-final-identifiers.txt`, `v005-r1-identifier-cost.txt`
- `.grafx-tmp/v005-r1-r2-pulse.txt`, `v005-r1-r2-final-consolidation.txt`
- `.grafx-tmp/v005-r1-profile-2.txt`, `v005-r1-deep.txt`
- `.grafx-tmp/v005-r1-r2-build.txt`, `v005-r2-mypy-final.txt`

Reproduction (Grafx source on `PYTHONPATH`):

```text
python -m pytest tests/storage_core tests/txn tests/recovery tests/wal tests/index tests/api tests/foundation/test_bootstrap.py tests/test_import_boundary.py tests/test_language_surface.py -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
python -m pytest tests/api/test_checksum_isolation.py tests/storage_core/test_identifier_fast_path.py -q
python tools/generate_api_reference.py --check
python tools/check_documentation.py
```

Pulse fixture uses Community `okto-pulse-v003-kg-load-codex` and Core
`okto-pulse-core-kg5-codex` through `OKTO_PULSE_COMMUNITY_REPO` and
`OKTO_PULSE_CORE_REPO`; the profiled instrument also needs the Grafx repository
root on `PYTHONPATH`. All results are local Windows/Python 3.13 evidence, not a
claim that a full OS/Python matrix or production deployment was performed.
