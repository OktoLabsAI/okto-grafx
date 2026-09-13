# Current Pulse value and label-transfer qualification

September 13, 2026. This closes a bounded source/installed consumer correction
within [FP-8](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-8--qualify-the-profile-and-update-pulse),
not final HTTP/browser/MCP acceptance or full regression. No global installation,
production-data mutation, Core change or release occurred.

## Defects and implemented boundary

New independent Community tests reproduced eight conversion defects: DECIMAL and
NaN leaked into JSON, BLOB/UUID lacked conversion, int64 TIMESTAMP endpoints
overflowed Python's calendar, and stringifying map keys collapsed entries such
as `1` and `"1"`. Multiple-label identity and old-snapshot observations already passed.

`community/adapters/grafx_query_values.py` now emits exact plain observations:
DECIMAL coefficient as text plus precision/scale, tagged expression NaN, hex BLOB,
canonical UUID, tagged microseconds outside Python's calendar, and entry-pair maps
when keys are not all strings. Ordinary objects, tuple/list behavior, vectors and
native temporal ISO formatting remain unchanged. Both the read-only executor and
the fenced transaction port use this adapter. These are output observations, not
an input decoder or universal round-trip format; tag-shaped user maps remain data.
No nonfinite storage, procedure permission or transaction weakening is introduced.
Community's `docs/GRAFX_NATIVE_RESULT_VALUES.md` documents the JSON shapes and
logical `_LABEL`/`_LABELS` versus the unchanged physical owner in `_ID.table`.

Four further tests reproduced silent label loss in Pulse's logical transfer. Its
neutral artifact declares one business type per node. The adapter now validates
native membership during complete snapshot preparation: implicit or matching
explicit singleton membership is accepted; empty, renamed or additional labels
raise `LogicalSchemaError` before exposing the snapshot. Temporary endpoint maps
and transactions close on refusal. A held canonical snapshot still exports its
original rows after a concurrent mutation; a new snapshot detects that mutation.

This is explicit refusal of a representation Pulse's artifact cannot express,
not a label-set transfer format for Pulse. Grafx's own
[format-4 transfer](../LOGICAL_TRANSFER.md) preserves full/empty membership.
Core remains clean at `33e3a5fe32c3c31c38ec6b4b2913e570ccba40a0`; no Core DTO,
driver dependency or Settings knob was added.

## Source receipts

All receipts are under `.grafx-tmp/`; counts overlap and must not be summed.

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `fp-pulse-extended-values-baseline.xml` | 1 pass / 8 failures: missing conversions | `bbe5fc616de1e0bb5002694b2161f355631dac539b8b43ebad560ec0f0c64268` |
| `fp-pulse-values-corrective.xml` | 146 passes | `ec48fe8dd493d7b00675129249ad1d9b412cb370e2c988c033c2e6cb3fb77b32` |
| `fp-pulse-label-transfer-baseline.xml` | Four failures: silent label loss | `1a70e3384726dca8d42508e142d336e73764276dd4a0b8d7eadbf7c4477958be` |
| `fp-pulse-values-label-transfer-corrective.xml` | 24 passes / two missing catalog-v2 activation fixture failures | `81dc3af2a5880bfc1e872a7c6c108faa7e020be3928f585103f3fc11f6f51f81` |
| `fp-pulse-values-transfer-grouped.xml` | **255 passes**, zero failures/errors/skips, 196.767 s | `3ebb5eb5101cd6425077f8cde32150d17797f1f38c90ed1263cfdddc4ae84dc0` |

The final 11-file grouping includes new extended values and label-transfer cases,
existing native results/executor/transaction/general-MERGE/updating-subquery tests,
and logical-transfer values/schema/physical-matrix/factories. Exact null, identity,
endpoints, rollback, fences and cleanup assertions remain. New indexed-DDL fixtures
explicitly activate identity indexes; no engine admission check was removed.
Negative label tests now also close an unexpectedly accepted snapshot to avoid
test-owned temporary-map leaks on assertion failure. An initial collection selected
another sibling Community; explicit `OKTO_PULSE_CORE_REPO` and
`OKTO_PULSE_COMMUNITY_REPO` select the actual working pair without changing packages.

## Installed-package qualification

Private environment: `.grafx-tmp/fp-pulse-current/venv`, CPython 3.13.1.
Complete Python path inventories and every packaged resource were compared
byte-for-byte to checkout and installed files, before and after tests. These are
dirty development candidates, identified by hashes rather than version strings.

| Package | Python / total payload files | Wheel SHA-256 |
| --- | ---: | --- |
| Grafx 0.0.6 | 250 / 251 | `3fbd942891d8594feebe998bdddea55b6dd8fff801e004369761a7e2958dbb13` |
| Pulse Community 0.3.3 | 298 / 382 | `4b062b3186fc4c1b67221455b593ffdba6ce0ab3de2aa50d6052255a984ec159` |
| Pulse Core 0.3.3 | 760 / 825 | `54c1c2e81b2170fde44bd0bb0efb06c2151092125a08c90d43b118cc5425c43b` |

**65 installed tests pass**, zero failures/errors/skips, 47.690 s: general MERGE
(pure/NumPy), updating subqueries, native/extended values and the logical-transfer
source. Collection uses `--noconftest --import-mode=importlib` with `-I`, installed
origin assertions and no source bootstrap. Receipt
`fp-pulse-current/installed-qualified.xml`, SHA-256
`8141cf12dfabb84db01acaef795b0ad44f6054d80f3196a84859c6be91486e34`.
Proof `fp-pulse-current/installed-proof-qualified.json`, SHA-256
`f03905d0cf22db3c4f299512a1239053214e75016ae88a5fc9002f68642ddc33`.

This is package-isolated, not dependency-hermetic: existing user-site dependencies
are appended after private wheels. Private dependencies include NumPy 2.5.3,
google-crc32c 1.8.0, tzdata 2026.4 and pywin32 312; observed versions also include
pytest 8.3.4, pytest-asyncio 0.25.3, Pydantic 2.13.4 and SQLAlchemy 2.0.49.
Initial installed collection failed before tests because the appended dependency
path did not activate pywin32's Windows support. Installing it privately resolved
that; the first `installed.xml` and proof remain unchanged. No adapter or oracle
changed to hide a dependency/collection failure.

## Current HTTP and real-browser checkpoint

The same installed packages initialized a new synthetic home at
`fp-pulse-current/http-data`, board `5176dc59-8739-4670-a12c-ef2a1562dabe`.
Its native fixture contains 510 canonical Decisions and 20 `depends_on` edges,
verified and checkpointed before starting the server. The 187.709-second fixture
time includes writes, verification and checkpoint during a concurrent full-suite
run; it is not a standalone write-throughput benchmark or performance gate.

The live CLI uses explicit `--api-port 18100 --mcp-port 18101`, stub embeddings
and disabled external metrics. An initial test launch supplied only environment
port hints; CLI defaults overrode them and the bind failed on occupied 8100/8101.
That process terminated. The corrected launcher explicitly fixes the CLI ports;
the user's production process was not stopped or replaced.

All **11 HTTP checks pass** in `fp-pulse-current/http-first.json`, SHA-256
`3df29b5bf2cb27c8abe8b6bd0636c81d090cb38981034e26da8155353e11c849`:

| Operation | Exact result | Observed seconds |
| --- | --- | ---: |
| Graph first page | 500 nodes, next cursor, no failed edge tables | 3.389233 |
| Graph next page | 10 new nodes, 510 unique overall, terminal cursor, no failed edge tables | 0.228407 |
| Decision count | 510 | 0.125306 |
| Logical untyped-endpoint edge count | 20 | 0.181589 |
| DATE and list | ISO date and JSON array | 0.045362 |
| DECIMAL scalar | Exact coefficient/precision/scale tag | 0.046394 |
| Expression NaN | Named JSON tag, no invalid JSON number | 0.048447 |
| Nested DECIMAL | Exact tagged value inside list/map | 0.042727 |
| MERGE through read-only endpoint | 400, `unsafe_cypher` | 0.015258 |
| Anonymous endpoints under canonical-only policy | 503, `canonical_filter_unenforceable` | 0.014664 |
| Invalid graph cursor | 410 | 0.028490 |

Actual Chrome interaction also confirmed Menu → Knowledge Graph showing
`SHOWING 500 / TOTAL 510`; clicking `Load more (500+)` reached `SHOWING 510`,
with the button gone and the graph/linked chain visually rendered. Global
Discovery → Key Decisions returned 100 actual decision rows, placing two-connection
chain interiors ahead of one-connection endpoints and disconnected decisions.
Opening the first result showed `fp-ui-10`, its title and synthetic content in
the detail panel. These observations were read from the live accessibility tree
and screenshot, not a mocked React component. No on-disk screenshot is claimed.
The remaining discovery/operational flows are not implied by this bounded check.

The four neutral Core audits also pass: import boundary, package manifest,
dependency policy and import-side-effect smoke. Reproduce with
`python -m okto_pulse.core.application.boundary.cli audit --source-root <core-repo>/src --mode blocking --format text`.
An initial invocation incorrectly supplied the nested `src/okto_pulse/core` path,
so its manifest check could not find pyproject; no Core code was changed to fix
that invocation.

## Authenticated current MCP checkpoint

The real Streamable HTTP MCP endpoint on 18101 initialized successfully with the
test-only agent credential and exposed 338 tools. Six calls to
`okto_pulse_kg_query_cypher` passed through the version-2 neutral outcome envelope:
node count 510, logical edge count 20, exact DECIMAL, tagged NaN, MERGE rejected as
`unsafe_cypher` with protocol `isError=true`, and a subsequent Decision count 510.
Success calls require `outcome=success`, `isError=false` and exact nested rows;
the refusal requires the precise error code, not merely a failed request.
The count control proves Decision cardinality, not a whole-filesystem no-mutation
claim. Full pre-execution write refusal has separate source tests above.

Receipt `fp-pulse-current/mcp-queries-qualified.json`, SHA-256
`4c8cef27c1217d5be33d7519f36253ca0de9e63e56f5615befc98365a1cbbc3f`.
An initial observer incorrectly expected rows at the response root; it received
a successful version-2 envelope with rows under `data` and failed its own lookup.
That receipt is preserved as `mcp-queries.json`. The corrected observer verifies
the documented neutral outcome contract/version and both success/error protocol
states; no application result or expected row changed.

## Remaining acceptance

The full Grafx regression remains a separate execution. Its preflight required
the exact pinned Pulse corpus worktrees rather than newer development heads. The
provided generator refreshed two runtime-acceptance observations, without changes
to query text, source pins, security policy or the TCK ledger. Corrected preflight
passes 24 tests: `fp-integrated-preflight-qualified.xml`, SHA-256
`a42e6a299a2f962652a6d45ebd5ed5be0f3d61f591bdceb84d867dee7e52ddca`.
This does not make the unfinished full regression pass.

The subsequent [operational qualification](FP_PULSE_OPERATIONAL_QUALIFICATION.md)
adds 121 affected source and 158 installed passes, source lifecycle, current
Settings/schema, nine actual MCP requests, native post-barrier crash recovery and
real-browser semantic search. It records the recovery publication boundary and
observer corrections explicitly. Full Grafx regression, supplemental reconciliation
and bounded competitor checks remain required. No final parity or publication
claim follows from these bounded acceptance results.
