# DELETE expressions and paths — FP-4

September 12, 2026, source `feature/v0.0.6`. This increment of the
[functional parity plan](FUNCTIONAL_PARITY_PLAN.md) closes the original DELETE
family, not the full FP-4 owner, full profile or installed Pulse acceptance.

## Native consumer contract

`DELETE expression [, expression ...]` and `DETACH DELETE expression ...` accept
expressions yielding a native node, relationship, path or NULL. List/map indexing,
nested property access, CASE and coalesce may select those values. A whole list
or map is not implicitly flattened: select its entity/path element or use UNWIND.
Parameter data and detached DTOs do not become live deletion authority. Known
scalar/container targets refuse during planning; dynamic invalid values refuse
at execution. Missing values/NULL do nothing.

```python
with db.begin("write") as tx:
    tx.execute("""
        MATCH (n:Person)
        WITH collect(n) AS people
        DETACH DELETE people[$position]
    """, {"position": 0})
```

A path targets its relationships and nodes, preserving actual table-qualified
native identity; it does not mean only its end node. Repeated entities, cycles,
self-loops and repeated targets are deleted once. A zero-length path targets its
single node. Plain DELETE checks incident relationships after all statement
targets are applied, so deleting a node and its edges is independent of target
order. Surviving incident relationships refuse and roll back the statement.
DETACH also deletes incident relationships outside the selected path.

For inputs that can observe deleted content, the operator evaluates its complete
upstream input and target expressions before performing deletions. A direct,
predicate-free single node scan with only its literal variable as target remains
streaming: it visits each node once and cannot observe another deletion's properties.
Filters, Cartesian inputs, traversals and expression selectors do not qualify for
that fast path. A bidirectional match can therefore evaluate both copies of
an edge's predicate without the first deletion invalidating the second. This is
not a waiver of deleted-entity errors: later property/label access in downstream
clauses still refuses. An earlier completed mutation clause remains visible.

The implementation buffers prepared inputs/targets, bounded by existing
`query_memory_budget_bytes` and intermediate-row limits. Memory charges cover
row values, native target payloads and container overhead; the reported operator
is `delete_inputs`. It is a conservative logical allocation limit, not exact RSS.
No deletion-specific setting or spill format is added. This buffer does not spill:
insufficient configured memory refuses explicitly before deletion, without partial
results or implicit batches/commits. `None` retains the existing opt-out for that
memory limit; callers should keep limits for untrusted or large workloads.

## Transactions, API and durable effects

Imported writing CALL and updating UNION share the same outer statement boundary.
Discarded output still drains effects; a later failure restores all that statement's
deletions. Previously successful statements are retained only after proven rollback.
Read-only doors refuse before writes; readers retain snapshots and conflicting
writers retain OCC. Deletions use the existing index/history and WAL/COMMIT path.
There is no new storage format, capability bit or Pulse Core dependency.

The public plan node `DeleteEntities` now exposes `targets: tuple[Expression, ...]`
instead of `variables: tuple[str, ...]`; its details use `targets` expression text.
AST `DeleteClause.targets` likewise admits expressions. This is an intentional
development API change, not a legacy mode. The existing public snapshot validator
still accepts only canonical expression objects. `Transaction.execute()` is the
consumption door; no new database method is needed.

Native errors carry explicit `reason` and `query_phase`: `invalid_delete` for
label/type deletion syntax, `delete_argument_type` for wrong target types, and
`connected_node_delete` for a node with surviving edges. The test-runner mapping
uses those fields and runtime table identifiers, never the case's expected error
or message text as an oracle.

## Original evidence

Both selections execute all **41** original DELETE cases without fixture/schema
adaptation or ledger changes, retaining 3,856 cases outside selection.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `delete-expressions-original-first.json` | 37 passed / four error-contract mapping failures | `e85f678df7b701efca756267f0bc7d3d6524ef132395a51bf05861dda97602b6` |
| `delete-expressions-original-second.json` | **41 passed, zero failures** | `6bc2328189f7d772481edd705ad3f690f70ec42a60553acb1118ae8528e2d33b` |
| `delete-expressions-first.xml` | 701 passed / no failures, errors or skips; 34.417 s | `c4110e52937bf126facf2a519c3962fe2ebd3f8abae6fb982f62b5a68a31ea6e` |
| `delete-expressions-focused.xml` | 125 passed / no failures, errors or skips; 9.690 s | `e6d48871d845636c4fd3a1918bbca1bfea378cb0f09a67f4af9a033ed7cb4708` |
| `delete-expressions-original-final.json` | 41 original passes after streaming fast-path qualification | `6bc2328189f7d772481edd705ad3f690f70ec42a60553acb1118ae8528e2d33b` |
| `delete-expressions-final-qualified.xml` | 359 passed / no failures, errors or skips; 101.461 s | `85054a9bdde70d0b36802f244ab56c8e915f4933e967c2f2ca714a646c045846` |
| `delete-plan-targets.xml` | one public-plan ownership/refusal test passed; 0.169 s | `0fe19a9247d77da649356bb2f546b51e44c93de1434f1b1fc64595efe6c24265` |

Local collections overlap; their counts are not additive. The initial broad
collection covers parser, analyzer, planner, query engine, property removal and
writing subqueries. The final 359-test collection includes all new selectors,
path cycles, target-order independence, repeated bidirectional matches, nulls,
wrong-type/foreign-data refusal, nested late-error rollback, read-only admission,
pinned readers, OCC conflicts and explicit input-memory refusal with a usable
transaction afterward. Existing deleted-content tests still reach actual downstream
spill decoding and refuse stale content, not merely an earlier memory error.
Four subprocess cases cover pure/NumPy codecs and crash cuts before COMMIT or
after durable COMMIT before page application; two subsequent opens and
`verify("all")` prove the path deletion and unrelated update share one outcome.

The first grouped receipt, `delete-expressions-qualified.xml`, remains recorded
as **358 passes / one failure**, SHA-256
`ed055eef33ad009ba9899968974bf717eed58b478bb5b68a0045233f00979817`.
Its deletion buffer exhausted a 32 KiB budget before the existing downstream
deleted-content/spill test could reach its expected refusal. The fix retains
streaming only for the statically proven direct node-scan case described above;
neither that test's budget/assertions nor the general preparation limit was relaxed.

A read-only search of the paired Pulse Community Grafx adapters found no consumers
of `DeleteEntities` or its former `.variables` field. That is not a substitute for
the pending paired-wheel/API/browser qualification in the main plan.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/delete-expressions-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/delete/
python -m pytest tests/query/test_deleted_entity_access.py tests/query/test_delete_expressions.py tests/txn/test_delete_expression_recovery.py tests/tools/test_tck_delete_errors.py tests/query/test_entity_unwind.py tests/query/test_write_subqueries.py tests/query/test_subquery_imports.py tests/query/test_updating_union.py tests/query/test_set_property_maps.py tests/query/test_merge_actions.py tests/api/test_query_result_plan_door.py tests/api/test_delete_plan_targets.py -q --tb=short --junitxml=.grafx-tmp/delete-expressions-reproduced.xml
```
