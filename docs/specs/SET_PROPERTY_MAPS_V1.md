# Whole-property SET maps — FP-4

The subsequent [vector-owner repair](VECTOR_PHYSICAL_OWNERS_V1.md) corrects typed
vector assignment: property SET, map overlay and map replacement retain the
validated stored vector instead of staging its input list. Dimension/nonfinite
refusal, NULL semantics and whole-statement rollback remain enforced.

September 12, 2026, `feature/v0.0.6` source increment of the
[functional parity plan](FUNCTIONAL_PARITY_PLAN.md). This is not full FP-4,
full-profile acceptance, a released wheel or an installed Pulse qualification.

## Consumer contract

Inside an explicit write transaction, `SET n = expression` replaces the logical
properties of a native node/relationship. `SET n += expression` overlays them,
retaining unspecified properties. The source must evaluate to a map or a native
entity; the latter supplies its owner-current logical properties, not its identity,
labels, type or physical relationship endpoints. `properties(entity)` is also a
valid map source. Parameter maps are detached; mutating a caller-owned nested
collection cannot mutate the staged or durable value.

Map entries containing NULL remove properties. `SET n = {}` clears a flexible
entity's properties; `SET n += {}` is a no-op without a staged update. A NULL whole
map is a type error, not an empty map. A NULL optional target does nothing and
does not evaluate its RHS at runtime; static binding/parameter checks still apply.
Non-map scalars/lists, non-string keys and attempts to assign `_from`/`_to` on
relationships refuse. Detached entity DTO parameters do not confer native write
authority.

Explicit typed tables remain typed: replacement must include required/primary-key
columns, omitted nullable columns become NULL, unknown columns and wrong types
refuse. Flexible tables support heterogeneous/nested stored values under their
existing bounds. Neither mode permits persisted NaN or infinity, even nested.
Replacing properties preserves native identity, labels, relationship type and
endpoints. It is not label mutation or migration between tables.

Assignments to aliases of the same entity fold into one held row in written
order. RHS expressions read the input to that SET clause; use another SET clause
when an expression must observe the previous assignment. For example, starting
at `n.v=1`, `SET n += {v:2}, n.copy=n.v` leaves `copy=1`, whereas
`SET n += {v:2} SET n.copy=n.v` leaves `copy=2`. This explicit existing Grafx
clause-input rule is not a claim of all mixed-assignment dialect equivalences.

Map actions also work in `MERGE ... ON CREATE SET ... ON MATCH SET ...`, imported
writing subqueries and updating UNION branches, sharing the outer statement's
rollback boundary. Discarded output does not discard required updates. A late
type, uniqueness or output failure cannot leave a partially applied statement;
earlier successful statements survive only when rollback is proven.

## Implementation, API and configuration

The native SET path prepares private row copies, then uses ordinary mutation,
index/history publication and WAL/COMMIT recovery. Read-only refusal, snapshot
isolation and OCC remain intact; no separate storage or weaker commit is added.
Existing limits bound expressions, parameters, rows and stored values. No new
configuration field, database method, persistent format or capability bit is added.

The returned public plan's `PropertyAssignment.target` now admits canonical
`Property | Variable`, with `merge: bool = False` distinguishing `=` and `+=`.
The public plan snapshot still rejects forged/noncanonical target objects.
`properties_set` counts assignments per actual target rather than charging all
statement assignments to every target. Map counts include incoming keys and,
for replacement, existing flexible keys or typed logical columns being cleared;
it is an operation counter, not the TCK's net graph-difference oracle.

## Original profile evidence and remaining work

Original fixtures, expectations and frozen V2/V1 ledgers remain unchanged.
`set-maps-original-third.json` under `.grafx-tmp/` executes all 53 SET-family cases:
37 pass, 16 required failures, 3,844 outside selection. All ten original Set4/Set5
whole-map cases pass. Fifteen failures need label mutation. Set1 #0010 expects a
list of maps to be rejected as a property, but Grafx's authorized broader storage
model accepts it. That incompatibility remains visible as a required failure;
it is neither silently waived nor used to remove supported nested storage.

`set-maps-merge-original.json` executes all 75 original MERGE cases: 55 pass,
19 required failures, one retained multiple-label divergence failure and 3,822
outside selection. Label changes, broader/named MERGE patterns, deleted-entity
handling and remaining error contracts are still required. No aggregate
full-profile pass count is inferred from overlapping family runs.

## Local regression and reproducible receipts

The subsequent **complete FP-4 owner selection** (`fp4-post-set-maps-native.json`)
executes all 290 selected original cases: **201 pass, 84 required failures and
five retained divergence failures**, with 3,607 outside selection. SHA-256:
`3efc37b9aebdd671a498f81730a35104c7bd7ee1cd479d57565deb1e740df9d9`.
Every one of the 147 passes in `fp4-post-sq13-owner-native.json` still passes.
This records the cumulative property REMOVE, conditional MERGE and map work;
the gain is not attributed solely to this last increment. The owner remains open.

All receipt paths below are under `.grafx-tmp/`. Passing local collections overlap;
their counts are not additive. Original failing selections are not presented as
successful gates.

| Receipt | Outcome | SHA-256 |
| --- | --- | --- |
| `set-maps-original-third.json` | 37 original SET passes / 16 required failures | `2363bc4260d0e3b389511e3a58553c3748b1fdfddb7986c7189d79d1cb929288` |
| `set-maps-merge-original.json` | 55 original MERGE passes / 19 required failures / one divergence failure | `f801e4be7a9037f4528583536caa4c56042b6b492d69949c1d86489ee6adf618` |
| `set-maps-index-history.xml` | 2 passed; 2.734 s | `416892cb343e71cb429f0c9b8bb3a0f74391fbf853fb8817b480b5d0bbc5ec7c` |
| `set-maps-affected-qualified.xml` | 996 passed, zero failures/errors/skips; 122.308 s | `4b4d7a0d234c6bb94c773ab80b6d550691be36582f3cb133bf6dc3f75bba4e59` |

The 996-test collection covers lexer/parser/analysis/planner, query execution,
AST/public-plan doors, imported writing subqueries, updating UNION, conditional
MERGE, property removal and map updates. It includes typed constraints and
primary-key collision rollback, aliases, caller-owned nested values, null/error
admission, independent writer OCC, read-only refusal and pinned readers.
Two codec variants verify exact/full-text indexes, namespaced node/relationship
history, checkpoints and reopen. Eight subprocess crash cases cover scalar/map
actions, pure/NumPy codecs and cuts before COMMIT or after durable COMMIT before
page application, followed by repeated recovery and `verify("all")`.

Earlier receipts remain failures as recorded: `set-maps-integrations.xml` had
87 passes and two history-fixture activation errors; `set-maps-affected-regression.xml`
had 994 passes and two failures (a missing catalog-v2 activation in a local fixture
and a parser test that still required all entity-target SET syntax to refuse).
The fixtures now activate their required capability. The parser test accepts
canonical map targets while retaining rejection of literal targets; runtime tests
still reject scalar whole-map values. Frozen original fixtures were not edited.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/query/test_set_property_maps.py tests/api/test_set_maps_integrations.py tests/txn/test_merge_actions_recovery.py tests/api/test_query_result_plan_door.py tests/query/test_analysis.py tests/query/test_parser.py tests/query/test_lexer.py tests/query/test_planner.py tests/query/test_query_engine.py tests/query/test_merge_actions.py tests/query/test_write_subqueries.py tests/query/test_subquery_imports.py tests/query/test_updating_union.py tests/query/test_entity_keys_remove.py tests/query/test_ast_structure.py -q --tb=short --junitxml=.grafx-tmp/set-maps-affected-reproduced.xml
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/set-maps-original-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/set/
```
