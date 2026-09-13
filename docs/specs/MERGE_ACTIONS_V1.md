# MERGE conditional property actions — FP-4

September 12, 2026, development `feature/v0.0.6`. This increment belongs to the
[functional parity plan](FUNCTIONAL_PARITY_PLAN.md), not completion of FP-4 or a
new reduced profile.

## Native contract

`MERGE pattern ON CREATE SET ... ON MATCH SET ...` attaches conditional SET
clauses to the pattern's actual outcome. Either action kind is optional and may
appear first. Repeated clauses of the selected kind run in written order; later
clauses see preceding owner-visible updates. A clause retains ordinary SET's
assignment semantics; actions are not flattened into one SET.

Every matching node or bound-endpoint relationship contributes an output and
receives ON MATCH. ON CREATE runs only for an empty match set, after native
creation. Anonymous relationship matches retain multiplicity. Pattern properties
are evaluated once per input; an unselected action's expressions are not evaluated
at runtime. Static structure, variable binding, parameter and aggregate admission
still apply to both branches before effects. Missing/NULL and stored-type rules
are those of SET; persisted NaN/infinity remain prohibited.

Scope rewriting traverses actions in nested/imported writing subqueries and
updating UNION branches. Returned native entity identities follow later
owner-visible updates; a scalar projected inside a subquery preserves the value
at that projection. There is no independent inner commit. Late action, output or
callback failure remains under the existing whole-statement rollback mark.
Earlier successful statements survive only if rollback is proven. Read-only doors
refuse before writes, independent readers retain their pinned snapshots and
conflicting writers retain OCC.

The implementation reuses `_write_assignments`, held-row identity and native
publication/recovery. Relationship matching now streams every matching row,
including owner-private inserts/updates, instead of returning the first match.
It closes its iterators and charges observed rows to existing query budgets.
No global result materialization or graph-global-ID assumption is introduced.
Actions are bounded by existing `MAX_CLAUSES=64` per MERGE, with existing
expression/query resource ceilings; there is no new database configuration knob.

## Still required, not delivered by this increment

The subsequent [whole-map SET increment](SET_PROPERTY_MAPS_V1.md) extends property
actions with `SET n = map` and `SET n += map`. The later
[general MERGE increment](GENERAL_MERGE_V1.md) adds unbound/fixed multi-hop patterns.
Label mutation and remaining original error/effect
discrepancies remain assigned to FP-4. The family does not gain arbitrary
multi-label storage, independent inner transactions or weaker durability.
Namespace/Pulse qualifications in the main plan remain open independently.
The later [written-pattern increment](WRITE_PATTERN_CONTRACTS_V1.md) adds named
paths, completes binding/null error contracts and fixes mutation-to-MERGE read
ordering. Its undirected/endpoint follow-up now reaches 70 MERGE passes, four
required label-action failures and one retained divergence; the receipts below
remain the earlier action checkpoint.

## Evidence

All original runs below retain source fixtures, queries, expected answers and
frozen V2/predecessor V1 ledgers. They use `--execute-stateful --feature-prefix
clauses/merge/`, without `--infer-fixture-schema`; all 75 selected cases execute.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `merge-actions-original-first.json` | 50 original passes, 24 required failures, one retained multiple-label divergence failure; 3,822 outside selection | `779b738e8423801ea564f19d18a756e8786638f05e4e1303a457f54722b6ffb9` |
| `merge-actions-original-second.json` | 51 original passes, 23 required failures, one retained divergence failure; 3,822 outside selection | `8ec7b56a965ce89c974d198f82495f9e117e358a53ccffee425b7b685ed5f0d7` |
| `merge-actions-second.xml` | 77 passed, no failures/errors/skips; 34.552 s | `56da29947e841cdd8a18729de5cde37973790e9f8f41c12c816d9a014fdc453f` |
| `merge-actions-parser-planner-recovery.xml` | 386 passed, no failures/errors/skips; 27.434 s | `59de3d3b537c5164e1d344db5f02bf425e1dd9aa262e49bfd0351bf595e32006` |
| `merge-actions-subquery-regression.xml` | 229 passed, no failures/errors/skips; 73.136 s | `4428cbe3ab4d248034d95263f189b2e3f2aecc1e2c58c6246f4fb205f3187877` |
| `merge-actions-final-affected.xml` | 333 passed, no failures/errors/skips; 32.534 s | `3c1eff6b6975a1f854e54bcc870bddcf4fbf2619a4d714384405f54beeb24a3a` |

The first focused receipt (`merge-actions-first.xml`) retained one local test
failure: it expected an entity returned by an earlier subquery invocation to keep
its earlier properties after a later owner update. The corrected test separately
asserts final native identity properties and the earlier projected scalar; it
does not change runtime identity semantics or any upstream expectation.

The original multiplicity failure `Merge5 #0003` changed from failing to passing
after enumerating both matching relationships. The remaining 24 original failures
are not waived. Both original runners exit 1; neither receipt is a full-profile
pass. Focused checks cover typed/flexible nodes, both directions, repeated inputs,
all node/edge matches, skipped runtime errors, invalid static actions, read-only
refusal, snapshots, late rollback and schema preservation. Four real process cuts
cover pure/NumPy, pre-COMMIT and durable-COMMIT-before-page-application, followed
by repeated reopen and `verify("all")`.
The subquery regression includes new same-row conflicting writers (OCC refusal),
AST action-shape/limit checks and existing subquery/import/updating-UNION plus
their real crash/recovery files.
The final affected selection includes every action test plus complete query-engine,
schema-transactionality, implicit-index-authority and graph-namespace files.
Additional action tests prove updating-UNION visibility/rollback and single
evaluation of nondeterministic pattern properties on node/edge creation.

Counts from different selections overlap and are not additive. Full Grafx/Pulse
regression and installed-pair/API/browser acceptance remain required by the plan.
