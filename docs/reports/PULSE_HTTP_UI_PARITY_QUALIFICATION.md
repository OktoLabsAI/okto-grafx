# Pulse HTTP/UI qualification of the 0.0.6 development candidate

Date: September 12, 2026. Scope: the paired Pulse 0.3.3 Community application,
private installed wheels and synthetic data. This extends the
[general-MERGE consumer qualification](GENERAL_MERGE_PULSE_QUALIFICATION.md);
it is not completion of FP-8, the frozen profile or the full parity plan.

## Defect found and corrected

The native fixture contains 510 canonical `Decision` nodes and 20 physical
`depends_on__Decision__Decision` edges. Public HTTP returned 20 for a typed
endpoint query but **zero** for the valid logical query:

```cypher
MATCH (a)-[r:depends_on]->(b) RETURN count(*) AS total
```

Community's name translator required a unique proven endpoint pair. An ambiguous
logical spelling survived translation and reached Grafx as a nonexistent physical
type, yielding an empty result. This was an adapter defect, not missing data or
a rendering problem. The fix is in `grafx_relationship_query.py`, used by both
`grafx_cypher_executor.py` and `grafx_graph_transaction.py` in Community. Core
and Grafx source did not need changes for this correction.

Validated read-only callers now expand recognized fixed logical patterns to the
complete declared physical type alternatives when a unique pair cannot be proven.
Native matching retains endpoint constraints, direction, scope, aggregation and
row semantics. Proven unique pairs still use their single physical table. Writes
retain the existing unique-pair rule and fence; read alternatives grant no write
authority. No new public Grafx API, persisted format or Settings option is added.

The translator remains bounded: backtick identifiers, variable-length syntax and
caller-written type-alternative lists are not newly translated by this change.
Use physical names for those shapes when supported by the native query contract;
broader logical-name translation remains a consumer limitation. A custom narrowed
transaction layout must also provide the matching physical resolver: its existing
default intentionally preserves logical names.

## Source regression and failed attempts

Final source collection: **153 passed**, zero failures/errors/skips, 62.104 seconds,
terminal exit 0. It includes logical reads with both pure/NumPy codecs through the
standalone executor and actual transaction scope, translator unit cases, existing
executor/transaction tests, general MERGE and updating subqueries.

Receipt `.grafx-tmp/pulse-logical-read-final.xml`, SHA-256
`b94e0ee64eb0a15b787d4859454dcfec9a212a2dd6cb3c3bd651ae414e028d31`.

Failed runs were not waived: the original two-codec regression reproduced the
false empty result. A first fix over-expanded proven typed pairs and failed one
existing exact-translation test; production code was corrected without weakening
that assertion. An additional transaction test then failed twice because its
fixture used physical tables with the custom-layout logical resolver. The fixture
now explicitly supplies `resolve_relationship_table`, matching its storage and
production composition. The final 153-test run includes that assertion.

## Real application and HTTP results

The public CLI initialized a fresh data home at
`.grafx-tmp/pulse-http-20260912`. Full application startup and normal worker
lifecycles ran from installed packages, with explicit data paths, stub embeddings
and disabled external metrics. API/UI used `127.0.0.1:18100`; MCP listener used
18101. No production SQLite, graph, consolidation or backfill was touched.
The fixture was written through native Grafx while this test server was stopped,
then checked with `verify_all_findings()` and checkpointed.

All eight HTTP checks passed:

| Operation | Verified result | Observed seconds |
| --- | --- | ---: |
| Graph first page, limit 500 | 500 nodes, next cursor, no failed edge tables | 3.366 |
| Graph next page | 10 new nodes, no duplicates, terminal cursor, no failed edge tables | 0.171 |
| Typed node count | 510 | 0.108 |
| Untyped-endpoint logical `depends_on` count | 20 | 0.157 |
| Native DATE and list result through JSON | `2026-09-12` and `[1,2]` | 0.033 |
| MERGE through read-only HTTP endpoint | 400 `unsafe_cypher` | 0.029 |
| Anonymous endpoints under canonical-only policy | 503 `canonical_filter_unenforceable` | 0.028 |
| Invalid graph cursor | 410 `invalid_cursor` | 0.009 |

The anonymous-node refusal is the existing fail-closed Core policy, not a new
engine failure: name endpoints or explicitly request `include_working=true` when
appropriate. No policy was bypassed to get a passing check. Page boundary edges
may repeat as context; only node identities were required to be disjoint.
These timings are single local observations, not a benchmark or performance gate.

Receipt `.grafx-tmp/pulse-http-receipt-qualified.json`, SHA-256
`186dd731c1b4db033a527aa9f30250ab5dfe22683508e487c53f6e28ab18485d`.
Earlier HTTP receipts preserve the anonymous-policy refusal and the actual logical
false-zero failure; neither is presented as a passing run.

## Browser acceptance

Using the computer-use skill's browser workflow, an actual Chrome tab opened the
isolated application, navigated Menu → Knowledge Graph and showed
`NODE TYPES (SHOWING 500 / TOTAL 510)`. Clicking `Load more (500+)` completed:
`NODE TYPES (SHOWING 510)`, an accessible graph with 510 nodes and no remaining
load-more button. A screenshot inspection confirmed the rendered nodes and linked
chain, not just response JSON. The screenshot was inspected in the session; no
on-disk screenshot artifact is claimed. No frontend source changes were needed.

## Installed artifact provenance

Grafx and Core wheels remain the byte-verified artifacts in the preceding report:
Grafx SHA-256 `b2af1653f196c850d8818a0cf80e36b0d71f1b3390cab64794974034160e3553`;
Core SHA-256 `54c1c2e81b2170fde44bd0bb0efb06c2151092125a08c90d43b118cc5425c43b`.

HTTP and browser checks used Community wheel SHA-256
`8a827d1c5eae8c20245757b2fdd659a8d7a5bec18977e06445c12271b8857eb9`,
at `.grafx-tmp/pulse-logical-read-wheel/okto_pulse-0.3.3-py3-none-any.whl`.
The final rebuilt wheel in `.grafx-tmp/pulse-logical-read-final-wheel/` is SHA-256
`d09aae0e988bcfbb7f9983ab6c874304497c6c9b14bd72dba2862600d4cce44f`.
Its only package-resource difference is the translator module docstring;
AST comparison after removing that docstring confirms executable code equality.

The final wheel was privately installed with `--no-deps`. For all three packages,
module origins, complete Python file sets and payload equality against checkout,
wheel and installed files were asserted: Grafx 239, Core 760, Community 298 files.
As in the preceding qualification, this is package-isolated but not dependency-
hermetic: `-I` plus the private environment is used, with the existing user site
appended afterward for dependencies. No global install or release occurred.

The final installed wheels pass **41 tests**, no failures/errors/skips, 23.289
seconds, terminal exit 0: general MERGE, native result values, updating subqueries
and both logical-read codec cases. Test collection uses `--noconftest` and
`--import-mode=importlib` with installed-origin assertions, avoiding repository
bootstrap injection. Coverage overlaps the source run; counts are not additive.
Receipt `.grafx-tmp/pulse-logical-read-installed.xml`, SHA-256
`88d125b291ceb0db6effece721ffa8d599e054e1b42f11523955f7c974905ce9`.
An initial launcher attempt imported pytest before appending its dependency path
and exited before collection; the corrected launcher produced this passing run.

Final checks pass: generated API reference, documentation links/anchors, all 39
configuration fields, public signatures/DTOs and 11 preserved plans, Ruff for
changed Python files, and whitespace checks in both Grafx and Community.

## Remaining scope

This is bounded API/UI acceptance on one synthetic board, not a complete Pulse
regression, a real-data rebuild/consolidation benchmark, MCP request qualification
or a full frozen-profile run. The test server was stopped after the checks;
synthetic data and receipts were preserved for reuse. Independent readers/writers,
OCC, WAL, COMMIT proof and durability contracts are unchanged. Remaining parity
items and model/oracle decisions stay in the active plan.
