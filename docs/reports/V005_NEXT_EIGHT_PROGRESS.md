# 0.0.5 continuation after 3f3819f — local acceptance receipt

All eight bounded items are implemented and locally validated, September 9, 2026.
The [roadmap](../../ROADMAP.md#approved-continuation-after-3f3819f) owns their status.
No production Pulse workload, deployment, commit/push or publication is implied.

Final coverage: **15,904 distinct tests passed, 18 platform skips, no unresolved
failures**, across the grouped regression and corrective reruns detailed below.
This is not a claim that one uninterrupted full-suite invocation passed: initial
collection issues, a timeout and a verifier regression are preserved with their
dispositions. The remote/platform matrix remains unexecuted locally.

## Item 1: HNSW memory

Optional per-picture `vector_hnsw_memory_budget_bytes`, immutable
`Database.vector_memory_usage(space)` observations and conservative explicit
tariffs. Header admission precedes retaining the full collection/resolving vectors.
Warm-cache budget refusal retires derived state without failing a durable write.
This is not a shared multi-handle or process-RSS cap; defaults/format/recall unchanged.

Evidence: 11 new feature tests passed (1.80 s). Vector/config/public/language/import/
hybrid grouped suite: **3,299 passed, zero failures, 92.40 s**, recorded in
`.grafx-tmp/next-eight-hnsw.xml`. This invocation predates subsequent FTS/allocator
changes; it is not a claim that those later changes were included.

## Item 2: historical FTS summaries

Opt-in capacity 1..32, fixed page-0 series with complete committed intervals,
new required capability bit 8; default capacity zero preserves existing bytes.
Exact WAL/census remains when the interval cannot be proved. Physical backup
preserves series; logical transfer/rebuild creates destination-generation totals.

Initial historical tests exposed a verifier assumption: shared coverage retains
live payloads only. Historical verification now independently streams retained
heap versions once and compares snapshot totals, rather than mistaking that cache
for history. It also refuses an omitted retained heap transition between markers.
A test-edit ordering mistake briefly left a 9-row assertion in the independent
10-row transfer fixture; the rebuild assertions were moved back to their owner.

Final FTS/durable/incremental/catalog/verifier group: **162 passed, zero failures,
44.01 s**, `.grafx-tmp/next-eight-fts-final.xml`. After the extra transition check,
all **17 historical tests passed in 18.58 s**. Includes independent writers,
retention eviction/gaps, exact BM25 oracle, rollback/delete/null/rebuild, valid-CRC
tampering, before/after summary process death, repeated replay, backup/transfer
and old-capability refusal. Final-round coverage is recorded below.

## Item 3: indexed retired-page discovery

First prototype: mutable free-list root. Native process death before COMMIT under
four-page buffer pressure left an incomplete list while preserving graph rows;
that prototype is rejected, not documented as successful. Replacement: immutable
candidate-directory pages published only by quiescent vacuum, with an ordinary
local traversal cursor and physical revalidation before reuse. Ordinary writes
never remove directory membership. Previously consumed OVERFLOW candidates are
skipped; malformed/future candidates refuse.

Current grouped feature/legacy-reuse suite: **21 passed, zero failures, 24.83 s**,
`.grafx-tmp/next-eight-free-immutable.xml`, including the formerly failing native
process-death cut. Additional matrix/negative tests and final regression followed.
Cost is candidates plus directory pages, not every heap page; a fresh participant
can revisit consumed candidates, so this is not an O(k) or universal O(1) allocator.

Hardening plus vacuum/recovery group subsequently passed **29 tests in 43.52 s**,
`.grafx-tmp/next-eight-free-final.xml`. Includes valid-CRC future/omitted/reserved
directory damage, physical backup and a masked older capability reader.

## Items 4–5: sparse directories and repeated-key decoding

Sparse exact property indexes use bit 10/header format 4, compact META pointer
pages and lazy heads. First-head publication barriers the unreachable empty page;
four native process-death cuts replay one complete committed row on repeated reopen.
Rebuild/rehash, pinned old readers, backup and logical import were exercised.
One directory-image decode memo compares exact current bytes; selected head extent
is still checked. No broader directory/writer authority is cached.

Repeated-key decoding retains up to 64 pages/1 MiB logical per index; every reuse
compares all current slot images/request/decoder, and chain and heap proofs remain.
The 60-entry warm fixture performs zero entry decoder calls while retaining every
candidate. This is not a new posting-tree layout or sublinear enumeration of matches.

Sparse/header targeted run: **12 passed in 52.44 s** before the directory decode
memo. Subsequent combined sparse/hot-key/scalar/Arrow suite: **20 passed in 35.22 s**,
`.grafx-tmp/next-eight-consumption.xml`; later negative tests enter final regression.
An index group found one hardcoded future-format fixture still using newly assigned
version 4; changed that fixture to actually-unknown version 5, not the refusal rule.

## Items 6–7: trusted scalar SPI and optional Arrow

Explicit `ExtensionRegistry`/`ScalarFunction`, `connect(extensions=...)`, literal
`udf('app.name', ...)` and direct scalar invocation. Frozen per-handle declarations,
exact scalar types/NULL propagation, logical value bounds and typed refusals; no
database-driven code loading, storage access passed to callbacks or sandbox claim.
UNION typing consumes declarations without evaluating callbacks.

Optional `to_arrow_batches` supports exact typed native scalar results/cursors,
copied Arrow ownership, per-batch logical admission, fixed cursor snapshots and
caller-owned early close. No implicit coercion or external scan capability.
Focused scalar/Arrow/selector group: **14 passed in 10.35 s**; missing-dependency
and additional negative tests are included in final regression.

## Item 8 and final regression status

Windows 11/Python 3.13.1: **3 selector-transition tests passed**. A real 0.0.4 tag
wheel (SHA-256 recorded in the matrix) created four stores in isolated subprocesses.
Current 0.0.5 opened/appended each; the old wheel reopened the unchanged default
case and refused sparse/history/indexed-free capability activations. All **4 upgrade
cases passed**. The free-index case explicitly activates identity indexes first,
as vacuum's legacy-catalog contract requires. No user store was opened.

New Windows/Ubuntu × Python 3.11–3.13 × bare/extras workflow records JUnit; existing
whole-suite extras include Arrow so optional tests are not skipped on every leg.
Unexecuted platform rows are [explicitly unmeasured](../V005_COMPATIBILITY.md).

Foundation/index/consumer group initially reported **4,661 passed and 6 failures**:
one expected optional-extra inventory change and missing annotations/future imports
in new modules. Corrected, then focused boundary/public-surface/packaging checks
passed **1,764 tests in 64.09 s**. No failed run is relabelled as passing.

Documentation links, 36 configuration fields, generated API/DTO references and
Ruff passed before launch and again after the verifier correction. Final local
acceptance is established by the grouped coverage and corrective reruns below.

The first full collection stopped before executing tests because the frozen Pulse
baselines were not selected in the environment. Their exact existing commits were
verified and selected through `PULSE_CORE_BASELINE`/`PULSE_COMMUNITY_BASELINE`;
no baseline source or corpus assertion was changed. The configured full run then
stopped at the existing 60-second per-test timeout in corpus construction: repeated
whole-module AST walks were discovering the same enclosing function scopes. That
run did not produce a completed acceptance report and is not counted as passing.

The corpus scanner now indexes function scopes once per immutable parse tree, with
an exact tree-identity witness, retention bounded to 64 modules and the existing
per-build reset. Nested-scope selection and baseline/classification rules are
unchanged. Two new equivalence/bounded-retention tests cover this optimization.
With the same timeout, the inventory plus those tests passed **48 tests in 223.202 s**
(`.grafx-tmp/next-eight-corpus.xml`); the remaining corpus module passed **1 test in
0.069 s** (`.grafx-tmp/next-eight-final-corpus-other.xml`). Final API and remaining
suite groups run separately, retaining independent JUnit reports. The two scanner
tests also belong to the remaining suite and must not be double-counted.

The final API group passed **1,239 tests with one POSIX-only FIFO skip in 456.688 s**
(`.grafx-tmp/next-eight-final-api.xml`). Passing the remaining directories as
separate pytest roots caused 18 collection import errors: coordination modules
resolved `from conftest import CoordinatorFactory` to the transaction conftest.
That invocation executed no tests and is not acceptance evidence. The rerun uses
the original `tests` root with only the already-covered `tests/api` and
`tests/corpus` excluded; no test import or assertion is changed for this invocation
issue. Its report is `.grafx-tmp/next-eight-final-rest-root.xml`.

That remaining-suite invocation completed with **14,616 passed, 17 platform skips
and one failure in 1,215.637 s**. The new free-page verifier consulted the cached
catalog after `_verify_records` had already reported an unreadable catalog; the
second access escaped the diagnostic boundary. Correction: perform capability
admission/census inside `_verify_records`, using its already freshly decoded
catalog and existing error boundary. An unreadable catalog remains an explicit
`CATALOG_UNREADABLE` finding, not a clean result or a misleading heap failure.
No repair or mutation is introduced.

Corrective validation covered **the complete recovery suite**, free-page indexing,
historical FTS, sparse indexes and the cross-selector matrix: **730 passed, zero
failures/skips in 101.241 s**,
`.grafx-tmp/next-eight-final-verifier-correction.xml`. The catalog-corruption test
was then strengthened to cover both `records` and `all`, exactly one catalog
finding and no false table-unreadable finding. The complete verifier module passed
**81 tests in 0.522 s**, `.grafx-tmp/next-eight-final-verifier-scopes.xml`.

The final coverage count merges testcase identities from these six reports:

| Report in `.grafx-tmp/` | Observed result |
| --- | --- |
| `next-eight-final-api.xml` | 1,239 passed; 1 platform skip |
| `next-eight-corpus.xml` | 48 passed, including two scanner tests also present in the remaining suite |
| `next-eight-final-corpus-other.xml` | 1 passed |
| `next-eight-final-rest-root.xml` | 14,616 passed; 17 platform skips; the one verifier failure corrected below |
| `next-eight-final-verifier-correction.xml` | 730 passed; replaces affected earlier outcomes |
| `next-eight-final-verifier-scopes.xml` | 81 passed; includes both final catalog-corruption cases |

Deduplication uses `classname::name`, takes the latest corrective result and drops
the obsolete unparameterized catalog-corruption identity, replaced by two scoped
cases. Result: **15,922 current testcase identities = 15,904 passes + 18 skips**.
All skips require POSIX behavior unavailable on Windows (FIFO, flock, directory
fsync or replacement/unlink of open descriptors). No optional-dependency or
unattributed skip is hidden in that count. The rest-root command was
`python -m pytest tests --ignore=tests/api --ignore=tests/corpus`; the other groups
cover both excluded directories in full. No unrelated suite was omitted.

A separate `python -I -S` source probe (site packages disabled) passed scalar query,
sparse index write/read/verification and typed missing-Arrow refusal; numpy,
google_crc32c and pyarrow were absent from imported modules. This is bare-source
evidence, not a claim of an installed bare wheel on every supported platform.
