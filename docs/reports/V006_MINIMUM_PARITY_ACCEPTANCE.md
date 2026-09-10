# 0.0.6 minimum-parity implementation acceptance

Date: 2026-09-10. Branch: `feature/v0.0.6`. This describes the local source
working tree, not a published wheel, merge or installation in Pulse.

Subsequent correction: the expanded copy/phrase checkpoint found and fixed the
missing postponed-annotation import in `domain/query/scalars.py`. The scalar
feature/parameter/extension suites were rerun in its 916-test passing regression.
[Follow-up evidence](V006_COPY_PHRASE_ACCEPTANCE.md). Earlier results below remain
the original checkpoint's scope, not a retroactive full import-policy pass.

## Scope delivered

| ID | Implementation | Consumer contract / focused tests |
| --- | --- | --- |
| MP-1 | CLI table/space schema, secondary/vector metadata and path-free build capabilities | [CLI](../CLI.md); `test_schema_inventory.py`, `test_discovery_search.py` |
| MP-2 | Text/vector/hybrid CLI calls, one reader, typed filter/vector, bounded k/deadline, native diagnostics | [CLI](../CLI.md); real corpus ranking/filter/refusal tests |
| MP-3 | JS/TS subprocess recipe with exit/envelope checks, resource limits and cleanup | [Recipe](../../examples/cli-consumer/README.md); Node acceptance and strict TS compilation |
| MP-4 | Native lower/upper/trim/abs in query planning/evaluation | [Language](../QUERY_LANGUAGE.md); `test_native_scalars.py` |
| MP-5 | Detached HTML/SVG viewer with escaped schema, no scripts/CDN and explicit display truncation | [HTML](../HTML_SNAPSHOTS.md); `test_html_snapshot.py`, Chrome acceptance |
| MP-6 | Bounded read-only SQLite selection, close source before atomic Grafx staging | [SQLite](../LOCAL_SQLITE_IMPORT.md); `test_sqlite_import.py` |
| MP-7 | Two top-level read-only UNION ALL branches, duplicate preservation and existing type/budget/snapshot rules | [Language](../QUERY_LANGUAGE.md); `test_union_all.py` plus existing UNION regression |
| MP-8 | Iterative O(V+E) topological ordering with typed cyclic/blocked result | [Algorithms](../GRAPH_PROJECTIONS.md); stdlib graphlib oracle, loops, multiedges, deep chain and limits |

## Validation

Final grouped Python regression: **3419 passed, 0 failed, in 497.11 seconds**.
The command covers `tests/query`,
`tests/cli`, projection/algorithm/new HTML/new SQLite/native text/hybrid API tests,
packaging and the snapshot/OCC/commit-protocol/commit-redo suites.

Additional acceptance:

- Node: **4 passed, 0 skipped**, including invocation of the source Grafx CLI.
- TypeScript: strict `--noEmit --module nodenext --target es2022` compile passed.
- Browser: headless Chrome rendered the synthetic 5-node/6-edge picture, schema
  and complete counts, with no scripts or remote requests; screenshot inspected.
- Final scalar/write and HTML focused rerun: **23 passed**.
- Ruff, documentation links/config/public signatures and generated API reference checks passed.

The initial grouped run had 3223 passes and one outdated test assumption that every
CLI command accepts connection options. The path-free capabilities command correctly
refuses those; the test was corrected and its suite rerun. A separate adversarial
test caught a real UNION ALL budget bypass when UnionRows became the result node.
The engine now retains the pair's intermediate-row budget without reintroducing
DISTINCT; the new budget tests pass. These findings are not deferred debt.

Reproduction (from the source checkout):

```sh
python -m pytest tests/query tests/cli tests/api/test_projections.py tests/api/test_projection_algorithms.py tests/api/test_topological_order.py tests/api/test_html_snapshot.py tests/api/test_sqlite_import.py tests/api/test_fulltext.py tests/api/test_hybrid_search.py tests/foundation/test_packaging.py tests/txn/test_snapshot.py tests/txn/test_occ.py tests/txn/test_commit_protocol.py tests/recovery/test_commit_redo.py --tb=short --maxfail=5
python tools/check_documentation.py
python tools/generate_api_reference.py --check
node --test examples/cli-consumer/test.mjs
tsc --noEmit --strict --module nodenext --target es2022 examples/cli-consumer/schema.mts
```

The Node real-engine case requires `GRAFX_PYTHON` and source `PYTHONPATH` as
documented in the recipe. Browser tooling is optional external test infrastructure,
not a new Grafx runtime dependency. Local regression logs and screenshots were
generated under `.grafx-tmp/v006-*`; these are temporary evidence, not packaged data.

## Boundaries retained

No persistent-format, WAL durability, OCC/snapshot or multi-reader/writer policy
changes; no new connection configuration or runtime dependency; no Pulse/core
integration, install, publication, commit or push in this checkpoint. Validation
was local Windows/Python 3.13; this is affected-area regression, not a claim that
the entire repository matrix or hosted/POSIX CI ran. The eight bounded additions
do not close full Cypher, native multi-language drivers, federation, a live GUI,
GDS parity, arbitrary serializability, RBAC or HA roadmap items.
