# FP-4: transaction-scoped read/write subqueries

Supplemental extension profile, frozen before native FP-4 implementation. This
does not alter the pinned 2024.3 TCK or claim full Neo4j compatibility. It implements
the existing [functional parity package](FUNCTIONAL_PARITY_PLAN.md#fp-4--subqueries-that-read-and-write).
Reference semantics: [Neo4j CALL subqueries](https://neo4j.com/docs/cypher-manual/current/subqueries/call-subquery/),
reviewed September 11, 2026. No independent inner commits are permitted.

## Required execution cases

| ID | Contract to qualify |
| --- | --- |
| SQ-01 | Explicit imported scalar/entity values; absent imports and output collisions refuse |
| SQ-02 | Returning CREATE/SET/DELETE/MERGE per input row, with zero/multiple returned rows |
| SQ-03 | Unit calls drain their body and preserve exactly one outer row, including zero inner matches |
| SQ-04 | Nested calls share the outer snapshot, staging mark, quotas and rollback boundary |
| SQ-05 | Successive invocations observe previous private writes; downstream reads observe completed calls |
| SQ-06 | Returning zero rows or an outer LIMIT does not undo/omit already-required writes |
| SQ-07 | Late inner, later invocation, outer expression and final detachment failure discard the whole statement |
| SQ-08 | Earlier successful statements survive a failed statement only after proven rollback |
| SQ-09 | Read transactions and read cursors refuse all nested writes, even with zero input |
| SQ-10 | Independent readers retain snapshots; conflicting writers retain OCC; commit/reopen verifies |
| SQ-11 | Implicit schema/endpoint creation and late rollback work across nested scope boundaries |
| SQ-12 | Explicit/all-variable and leading-WITH imports, persistence/shadowing and branch-local scope |
| SQ-13 | Updating UNION branches, common output names, effects/cardinality and late branch rollback |
| SQ-14 | Bounded memory/intermediate rows/writes/depth, interruption and iterator cleanup |

The first increment implements explicit-import returning/unit writing calls and
their invocation-local read phases. The next increment implements SQ-12 import
scopes as documented below. SQ-13 now implements updating/unit UNION; its evidence
is recorded separately below. Complete package/Pulse qualification remains open.
These increments are not exclusions or a claim of package completion. Test evidence and status
belong in ROADMAP.md and this document as implementation proceeds.

## Transaction and consumption policy

Calls never begin/commit an independent transaction or expose private data to
other participants. Preparing read phases of a correlated body must happen only
after its incoming arguments are bound, and must be repeated for each invocation.
Statement classification includes deeply nested writes. The existing execute API
materializes writing results before returning; read cursors cannot stream writes.
No new tuning flags, persistent layout or dependency is introduced. Existing
transaction, query, intermediate-row, traversal and nesting limits remain active.

## Explicit-import writing increment: implementation and evidence

Native parsing/analysis/planning now include nested writes in the outer query's
classification and admit returning or unit writing bodies. Unit outputs are not
confused with zero-column returning rows. The planner completes writing calls
before subsequent reads/windows; the executor prepares each body's write/read
phases only after binding that invocation's arguments, then drops its phase cache.
It uses the existing outer transaction mark and does not recursively call the
public execute API or open an inner transaction.

Three defects found by independent tests were corrected, not waived:

1. `_plan_writes` previously walked only attributes `child`, `left`, `right`,
   missing a subquery's `inner`. It now traverses the complete operator-owned
   children. Zero-input nested writes refuse in read transactions/cursors.
2. Imported entity aliases retained properties from an earlier invocation.
   Phase/import refresh now observes the owner's latest values in native entities,
   lists/maps and paths, while preserving projected scalar observations. Container
   traversal rejects cycles/depth violations and memoizes shared structures.
3. Pending insertion tokens were keyed solely by Python object ID. Unit-body rows
   could become unreachable between invocations, allowing ID reuse and incorrect
   token association. Registration now retains the exact HeapVersion witness and
   requires object identity on lookup. Witnesses are statement-local, released with
   the execution context, and subject to the same finite statement/intermediate
   work limits. The endpoint/transaction ownership refusal itself was not relaxed.

Focused final source evidence under `.grafx-tmp/`:

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `fp4-write-subqueries-identity-qualified.xml` | 81 passed, no failures/errors/skips, 31.043 s | `3e89d9efa42e7afbc251fa18634dbaf5fc637178e1f979505b32b834eb8b1985` |
| `fp4-explicit-call-final-feature-recovery.xml` | 37 passed, no failures/errors/skips, 14.946 s | `d33c51ea27ea03179d977f5f769f3cda6e0d64d3e8d2336fda35df8514da7841` |

The 81-test selection covers explicit calls, subprocess recovery, fresh endpoint
composition, flexible relationships and native entity results. The 37-test
selection has 35 explicit-call tests (including deterministic stale-ID injection)
and two real process cuts: before COMMIT and after durable COMMIT before page
application. Both recover/verify/checkpoint and reopen twice, retaining prior
committed rows and either all new schemas/nodes/edges or none. Counts overlap and
must not be added. Earlier focused failures and interrupted broad runs are
superseded evidence, not accepted regression receipts.

Coverage includes per-input/zero/multiple cardinality, nested units and returning
calls, LIMIT 0/1, CREATE/SET/MERGE/DELETE, lazy implicit tables/relations, imported
entities/lists/paths, grouping/spill, late expression/public detachment rollback,
prior-statement preservation, shared write/intermediate budgets, read-only
refusal, independent snapshots and conflicting writers. At that first checkpoint,
SQ-12/13 import and UNION semantics remained required; this was not checkpoint B or installed-Pulse
qualification. No original TCK case, expected error or frozen ledger was changed.

### Final grouped regression

All **176 test files** under `tests/query` and `tests/tools` were executed on the
final runtime code, in two disjoint selections. PowerShell selection is the
sorted result of `rg --files tests/query tests/tools -g 'test_*.py'`, alternating
zero-based file indexes between the two processes. Each runs
`python -m pytest @selected -q --tb=short --junitxml=<receipt>` with
`PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps`. This is local Windows/Python
3.13 source qualification, not an installed-wheel or Pulse run.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp4-call-identity-final-regression-0.xml` | 2,230 | 429.990 | `108dc06c3caefe573071c5a7204a6462ddbb88755f35a0969d8340d3ca5c9e39` |
| `fp4-call-identity-final-regression-1.xml` | 3,142 | 681.145 | `94151d6d894f4cb92d2f46c2eafbc6284335bc7fc788f2d302e04a718b6c95f1` |

Total: **5,372 distinct passed tests**, zero failures/errors/skips; testcase
identities were checked for overlap. Times are per process, not additive elapsed
time. The 35 new query tests are included. The two subprocess recovery tests in
the focused receipt are additional; the 37 focused or 81 earlier tests must not
be added wholesale to this regression count. Initial/intermediate failed receipts
and deliberately stopped regression processes are not accepted evidence.

API generation, documentation links/anchors, coverage of all 39 configuration
fields, lint of modified/new Python files and diff whitespace validation pass.
README, query/API consumption, comparison, roadmap and the functional parity
plan link this contract. There are no new settings, dependencies or wire-format
changes. That first increment did not claim the remaining SQ-12/13 work, broader
FP-4 checkpoint or paired Pulse/API/UI validation complete.

## SQ-12: persistent explicit imports and branch-local importing WITH

Implemented native forms: explicit names, all visible names (`CALL (*)`), empty
scope (`CALL ()`) and bare CALL with a leading importing WITH. The
[usage contract](../COMPOSABLE_QUERIES.md#subquery-import-scopes) specifies
persistence, collisions, modifier restrictions, wildcard and per-branch visibility,
aggregation, row-window constants and the intentional scope-semantics change.
At the SQ-12 checkpoint, updating UNION (SQ-13) was pending; read-only UNION imports
were not evidence of write-branch qualification. The next section qualifies that
separate implementation.

Resolution is lexical and branch-specific. `Query.scope_imports` restricts each
branch's argument admission; `Query.global_imports` records only persistent names.
Analysis refuses undeclared metadata and carries native entity kind only when all
non-null UNION outputs prove the same kind. Scalars/maps never acquire node write
authority. Planner tables, alias proofs and polymorphic metadata are filtered with
each branch's lexical inputs. Another branch's import cannot capture a local name.

`RestoreImports` reinstates invocation-owned bindings after projection,
aggregation, DISTINCT and sorting; it does not insert hidden grouping keys or
DISTINCT columns. Zero-row global aggregation can still use the original imported
constants. Explicit imports can parameterize row windows once per invocation, but
row-local variables remain refused there. Scope metadata grants no store, page or
transaction authority. No new configuration, persistent layout or dependency.

Adversarial tests and regression found and corrected three native defects in this increment:

1. UNION analysis dropped native output kind, rejecting a valid imported-node
   UNION followed by SET. Kind is now preserved only with matching branch proofs;
   native-node/scalar and native-node/map alternatives still refuse before effects.
2. Disk sorting detached the imported node and discarded bindings, so a later SET
   could not use it. Imports are now rebound after sort from the invocation's exact
   native witnesses. Serialized temporary values are never promoted to write
   capabilities. Actual disk-spill tests verify both final values and the store.
3. Restoring imports immediately after a bounded sort separated it from the exact
   SKIP/LIMIT proof required by plan validation. Restoration now follows the whole
   window. The top-N optimization and strict validation are retained; neither is
   disabled. Regression caught the existing nested-call ordering case, and a new
   actual-spill test checks SET after SKIP 2 / LIMIT 3.

The first public-plan test incorrectly expected a fresh tree on every property
read. The established API deliberately materializes one detached tree per result;
the corrected test checks stable same-result identity, independent nodes across
results and isolation after adversarial mutation. No production cache or clone
contract was weakened to satisfy it.

Final focused receipt: `.grafx-tmp/fp4-imports-window-qualified.xml`, **190
passed**, zero failures/errors/skips, 34.145 s, SHA-256
`725e4f1f5b4146bd134237218656c917eed3ad2e46a8326ebc7e53cbb3f29092`.
Command: `python -m pytest tests/query/test_subquery_imports.py
tests/query/test_with_ordering_scope.py tests/query/test_top_n.py tests/query/test_plan_shape.py
tests/query/test_write_subqueries.py tests/txn/test_write_subquery_recovery.py
tests/api/test_query_result_plan_door.py -q --tb=short --junitxml=<receipt>` using
the PYTHONPATH above. Coverage includes actual disk spill, grouped zero input,
empty/nested scopes, independent UNION branches, per-invocation windows, native
entity writes, cursor snapshot under a concurrent commit, late rollback,
subprocess COMMIT recovery, bounded sorting and public plan isolation. The earlier
143-test receipt preceded the final bounded-sort correction. The first grouped
regression had 5,466 passes and one ordering failure; it is not an accepted final
receipt. Intermediate failed receipts are superseded. Feature and regression counts
overlap and must not be added wholesale.

### SQ-12 final grouped regression

After the bounded-sort correction, all **177 files** under `tests/query` and
`tests/tools` were run again using the two disjoint sorted/alternating selections
and PYTHONPATH documented above. Both processes finished with exit code 0.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp4-import-scope-window-final-0.xml` | 2,601 | 685.096 | `ada55a613ab936d28b85222b75442913e1e0e9835c432fb7354f608261eb99ac` |
| `fp4-import-scope-window-final-1.xml` | 2,867 | 710.023 | `bf71dd0ec70e5a78153a3d3bd1a33f49d15fa04bf009c69e0931f52bb46a76ba` |

Total: **5,468 distinct passed tests**, zero failures/errors/skips, with testcase
identities checked for overlap (none). Durations are per process, not additive
elapsed time. The 96 import-scope tests and 35 writing-call tests are included.
The focused 190-test receipt additionally exercises the two subprocess recovery
tests and eleven public-plan API tests outside these directories; do not add the
whole focused count to the grouped total.

Documentation/API generation and link/anchor/configuration coverage checks,
modified/new Python lint and diff whitespace checks pass. README, roadmap,
query/import usage, comparison, API and compatibility documents describe the new
scope behavior. This qualifies SQ-12 on the local Windows/Python 3.13 source build,
not the whole FP-4 package, installed Pulse, a release artifact or full TCK parity.
That checkpoint left SQ-13 and the remaining plan open. No frozen TCK fixture, result, expected
error or required-case ledger was changed for this increment.

### SQ-13 implementation and qualification

The native implementation now preserves branch-order effects: later branches observe
earlier writes, never the reverse. Common output names and UNION duplicate policy
remain independent of those effects.
[Reference: composed UNION queries](https://neo4j.com/docs/cypher-manual/current/queries/composed-queries/combined-queries/).

Implemented changes, tied to the original SQ-13 work:

- `union_refusal` admits writing branches with common ordered output names or
  all-unit writing bodies. It still rejects mixed unit/returning branches, mismatched
  names, mixed duplicate policies, malformed trees and exceeded branch limits.
- Query and UnionQuery share the iterative, cycle-guarded write classifier. Engine
  and public result-conversion rollback doors include root UNION, while read
  transaction/cursor checks walk the full physical tree even with zero input.
- `_union` propagates lazy unlabeled and relationship-group authority between
  branch planners. Native publication remains transactional and cannot leak future
  branch effects into earlier reads.
- `UnionRows.writes` makes phase preparation branch-local. Nested UNION and CALL
  bodies prepare only when their inputs and preceding private graph are available;
  branch-local phase caches are released on success/failure.
- A writing branch uses the existing eager barrier with private publication before
  its result expressions. This is not COMMIT. Native outputs have stable staged
  identities across branch boundaries before DISTINCT or spill consumes them.
- `_union_rows` drains unit bodies without exporting artificial empty rows.
  Returning branches preserve duplicate policy/cardinality; writing CALL barriers
  ensure downstream LIMIT cannot skip effects from required branches.

The [public consumption contract](../COMPOSABLE_QUERIES.md#updating-union-branches)
documents Python usage, returning/unit cardinality, imports, windows, rollback,
native identity, resource limits and read-only controls. No independent transaction,
store format, configuration, dependency or relaxed recovery policy is introduced.

#### Identity defect corrected, not excluded

An actual-spill regression produced 80 rows for 40 nodes when each node appeared
first as a new insertion and then as a read in the following branch. Before private
publication, the first branch had an execution-held identity; after publication,
the second had its authenticated pending reference. DISTINCT correctly treated
those different internal keys as different, exposing the missing phase boundary.
Completing branch writes before result projection fixes the identity mismatch,
without changing `_binding_identity`, weakening DISTINCT or trusting detached maps.
Tests cover direct nodes, renamed aliases, aggregate lists and nested maps.

The 40-node DISTINCT/spill/downstream-SET test now returns 40 rows at both 4 KiB
and 16 KiB, checks actual disk-run creation and workspace closure, and verifies
all 40 stored values. With a 512-byte budget it refuses explicitly and preserves
the earlier statement. The original 4 KiB refusal disappeared when authenticated
pending references replaced heavier held-identity witnesses; no budget guard was
removed or widened.

#### Focused source evidence

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp4-updating-union-final-focused.xml` | 338 | 101.415 | `fd93b3535bdd90af59a308652c66a4a40dbae6d7394d74a1465fb0cd1843a265` |
| `fp4-updating-union-interruption-qualified.xml` | 8 | 2.043 | `2b690d0488fc4eaab748fed9c9cf38288cffb2d1bccea09bffa4f2dd4be59343` |

Both have zero failures/errors/skips. The first selection is `test_updating_union`,
`test_union`, `test_union_all`, `test_subquery_imports`, `test_write_subqueries`
under `tests/query`, `test_updating_union_recovery` and `test_write_subquery_recovery`
under `tests/txn`, plus `tests/api/test_query_result_plan_door.py`. The additional
eight tests were added afterward and run with `-k 'interruption or read_controls'`.
Commands use the same PYTHONPATH and pytest/XML flags as earlier sections.

Coverage includes ordered effects, nested/correlated branches, zero results,
LIMIT 0, unit/returning distinction, CREATE/SET/MERGE/DELETE, implicit endpoint-pair
growth, exported entities, shared budgets, late branch/downstream/engine/public
conversion failure, independent reader snapshots and conflicting writers. Six
new subprocess cuts qualify returning, unit and correlated UNION before COMMIT
and after durable COMMIT/before page application, with verification/checkpoint
and two reopen cycles. No-COMMIT cuts preserve only prior data/schema; committed
cuts recover every expected node/edge. Two prior CALL recovery tests are retained.

Interruption tests inject KeyboardInterrupt/SystemExit after actual private intent
publication and verify whole-statement rollback and prior-statement preservation.
Public cancellation/deadline keywords are still refused for write transactions;
separate tests preserve that documented API policy. Earlier tests initially used
an incorrect public detachment hook/error expectation and an unsupported write
control invocation; tests were corrected to the existing public contracts, not
production weakened to satisfy them. Failed/intermediate receipts are superseded.

#### Final grouped query and transaction regression

The final runtime ran all **178 query/tools test files** in two sorted alternating
selections, plus all **49 transaction test files** in one independent process.
Commands use the earlier PYTHONPATH/pytest flags. The transaction command is
`python -m pytest tests/txn -q --tb=short --junitxml=<receipt>`.
All three processes reached terminal exit 0.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp4-updating-union-regression-0.xml` | 2,328 | 575.915 | `9aebb3306a7926d10da0fbe1b5d95fc73a50e1fa90af53e9bc89ad74019fd488` |
| `fp4-updating-union-regression-1.xml` | 3,214 | 790.698 | `9d4e6730f5ed95ab5c0677acce9e707b6ad327c4f3eac2cc25a5d5b3f525121d` |
| `fp4-updating-union-transaction-regression.xml` | 986 | 332.523 | `efd477bfe03aefed4542f0b3964e0202aaef3a0d85d4f86fc5c0cf78ac71b1d0` |

Total: **6,528 distinct tests**, zero failures/errors/skips. Case identities were
checked across all three receipts: no overlap. Durations are per process, not
additive elapsed time. Query/tools contribute 5,542 tests, including all 75 new
updating-UNION tests. The six new UNION and two prior CALL recovery cuts are
included in the 986 transaction tests. The eleven public-plan API cases from the
focused run are outside these directories; focused tests otherwise overlap the
grouped total and must not be summed wholesale.

One prior negative unit-test parameter explicitly forbidding a valid writing
UNION was removed as the intended admission contract changed; mismatched columns,
unit/returning mixtures and malformed-tree tests remain. Native positive writing
tests now exercise those semantics. No original TCK case, expected value/error,
fixture, profile exclusion or required-case ledger was changed.

API generation, documentation link/anchor and 39-configuration-field coverage,
modified/new Python lint and diff whitespace checks pass. README, query usage,
import/UNION contracts, comparison, compatibility and ROADMAP are updated.
This closes the native SQ-13 implementation and this local Windows/Python 3.13
query/transaction regression. It is not full repository/FP-8 acceptance, complete
frozen-profile qualification, an installed-wheel release or a current Pulse run.

The subsequent [paired Pulse source qualification](../reports/FP4_PULSE_SOURCE_QUALIFICATION.md)
exercises these statements through the Community transaction port and migrates
native entity/temporal result normalization without a Grafx dependency in Core.
Its receipts and remaining installed/API/browser scope are separate from the
native query/transaction totals above.
The paired Community/Core sources were located read-only for the next checkpoint;
their older receipts are not reused as evidence for this new Grafx build. The
overall parity plan and complete checkpoint B remain open.
