# FP-7 native nesting and effects qualification

Date: September 12, 2026. Source: dirty `feature/v0.0.6` development worktree.
[Feature/configuration/API contract](../specs/PROCEDURE_NESTING_EFFECTS_V1.md).
No commit, push, release, global install or production-data operation is claimed.

## Delivered behavior and evidence

Native query authority now supports registered CALL at child query/subquery depth,
including self/mutual recursion. Execute() accepts writing unit calls, including
the standalone unit resolver's synthesized empty RETURN. It still rejects actual
result columns, DDL and root UNION; query() is the returning door.

Every child retains registry permission checks and read/write mode restrictions.
Depth admission applies the minimum descriptor limit in the active chain, before
the next callback. Root statement-write/traversal budgets and per-name query,
query-result, write-statement and writing-output counters no longer reset at
deeper evaluation contexts. Nested results re-witness native entities, not DTO
metadata. Native transaction/snapshot/clock/cancellation/rollback remain shared.

Determinism is an explicit exact-bool descriptor, default False. The registry
replaces forged AST flags; native deterministic admission refuses volatile child
queries/callees. Writers cannot declare True. The conservative temporal effect
classification is documented; ScalarFunction's existing contract is unchanged.

The final **36 new feature cases** cover factorial and cursor reads, recursive
unit writes through execute/query on pure/NumPy with cold reopen and exact root
write allowance, infinite/mutual recursion, depth 1/3/8/16 and ancestor limits,
query/row/byte/root write/traversal budgets, permission denial and read-to-write
escalation refusal, provisional entities, late rollback, parent-capability
reentrancy/poisoning, deep delete invalidation, cancellation and concurrent writer
progress during a nested callback wait. They also check descriptor validation,
deterministic native composition, volatile functions/default callbacks, registry
metadata authority, recursive read EXISTS and continued rejection of writes in EXISTS.

## Receipts

Passing selections have zero failures/errors/skips. Runs overlap; do not add their
counts to claim distinct coverage. These are qualification timings, not benchmarks.

| Receipt under `.grafx-tmp/` | Result | Seconds / exit |
| --- | --- | --- |
| `fp7-nesting-first.xml` | 23 passed, 2 implementation failures | 6.512 / 1 |
| `fp7-nesting-focused.xml` | 107 nesting/query/writing tests passed | 21.741 / 0 |
| `fp7-nesting-regression.xml` | 610 combined procedure/subquery/UNION/deletion/cache/spill/EXISTS/volatile/cancellation tests passed | 121.661 / 0 |
| `fp7-nesting-budgets.xml` | 35 feature cases passed | 4.345 / 0 |
| `fp7-nesting-contracts.xml` | 1,466 feature/public-annotation/adapter/import/language tests passed | 38.295 / 0 |
| `fp7-nesting-final.xml` | 138 final nesting/query/writing/volatile tests passed, including all 36 new cases | 23.182 / 0 |

The initial failure was genuine admission logic: standalone unit CALL has an
empty synthetic ReturnClause, which execute() mistook for a query returning data.
Admission now checks actual items, not merely the presence of that clause. No
transaction protection or result-column validation was relaxed.

Four older local exclusion cases were replaced by the new feature contract:
writing capability tests no longer expect recursive CALL rejection, and query
capability tests no longer broadly reject self-CALL/EXISTS nesting. New positive,
depth/permission/rollback and read-only EXISTS tests cover those paths. The frozen
upstream ledger and architectural exclusions were not changed or waived.

SHA-256 receipts:

```text
fp7-nesting-first.xml 74946e54b8fd01742ba96d5ece6a4263c89414fbeab9f6da1b1416a06951031a
fp7-nesting-focused.xml ab7d6eb6bc36c6dc618dbf0a336edd3cc6c5a10acc6bcf929888de3292431c94
fp7-nesting-regression.xml 39c319e39ca928c8be37de56e76d5eb1c17d54315957e5d5c13d6711c36c33ce
fp7-nesting-budgets.xml a1ac56c50d7753f1d9006c20bdc73c3252feab7fab0fff1dabd837f3bb90c6b3
fp7-nesting-contracts.xml 07d76b4df1c046b5f00a363b631d67dc7937439b16771a5645058f1aced06791
fp7-nesting-final.xml 6518f92f59b77956d6a017afe813a6b9be541aa363e9c296c87d19a63a8e391e
```

The separate original FP-7 TCK selection remains **52/52 passed**, exit 0.
`fp7-nesting-native-20260912.json` has SHA-256
`74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`, identical to prior
owner results. The other 3,845 inventory entries are unselected, not certified.
Pinned upstream revision remains `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`.

## Reproduction and consumers

With `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps`:

```text
python -m pytest tests/api/test_procedure_recursion.py tests/api/test_procedure_queries.py tests/api/test_writing_procedures.py tests/query/test_fp2_rand.py -q --tb=short
python -m pytest tests/api/test_procedure_recursion.py tests/api/test_procedure_queries.py tests/api/test_procedure_entities.py tests/api/test_writing_procedures.py tests/api/test_procedure_native_values.py tests/api/test_procedure_invocation.py tests/api/test_procedure_numeric_signatures.py tests/api/test_unit_procedures.py tests/api/test_tabular_procedures.py tests/query/test_write_subqueries.py tests/query/test_updating_union.py tests/query/test_deleted_entity_access.py tests/query/test_prepared_plan_cache.py tests/query/test_query_memory_spill.py tests/query/test_exists_subqueries.py tests/query/test_fp2_rand.py tests/query/test_read_control.py -q --tb=short
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp7-nesting-native-20260912.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-7
```

README, roadmap, configuration/API, extension/composition/query guides, comparison,
compatibility and FP-7 specs document the new capability and default determinism
change. Generated-reference, documentation link/anchor/configuration coverage,
changed-source Ruff and whitespace checks accompany acceptance.

A read-only source search for TabularProcedure/ProcedureReader/ProcedureWriter/
ExtensionRegistry found no registrations/references under the paired Community
`okto-pulse-v003-kg-load-codex/src` or Core `okto-pulse-core-kg5-codex/src`. No Pulse
source mutation was needed for these descriptor defaults. This is a source-only
impact check, not installed-wheel/API/browser qualification; final FP-8 still
requires those checks and Core remains backend-agnostic.

## Remaining scope

Schema-changing native procedure authority remains FP-7 work. The complete goal
also retains FP-6 persisted parameterized types/DECIMAL, pending FP-4 policy
decisions and the full FP-8 inventory/competitor/Pulse qualification. The original
52-case procedure family and targeted regressions do not prove full Cypher parity.
