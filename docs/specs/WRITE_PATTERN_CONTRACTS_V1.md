# Written paths, binding admission and MERGE read phases — FP-4

September 12, 2026, source `feature/v0.0.6`, under the
[functional parity plan](FUNCTIONAL_PARITY_PLAN.md). This is not completion of
FP-4, a release, or installed Pulse qualification.

## Named CREATE and MERGE paths

`CREATE p = pattern` captures the nodes and relationships actually created or
referenced by the pattern, in written traversal order. `MERGE p = pattern`
captures the actual selected match or newly created pattern. MERGE still supports
single-node and bound-endpoint single-edge forms, now including undirected MERGE.
Naming the path does not implement unbound/multi-hop MERGE.

```python
with db.begin("write") as tx:
    result = tx.execute("""
        CREATE (a:Person {name:'Ada'}), (b:Person {name:'Grace'})
        MERGE p = (a)-[r:KNOWS]->(b)
        ON CREATE SET r.hops = length(p)
        RETURN p, nodes(p), relationships(p)
    """)
```

The name is in scope for conditional actions and subsequent clauses. A single
node is a zero-edge path. CREATE chains support actual incoming/outgoing edge
orientation, cycles and repeated node references. Anonymous edges receive internal
bindings for capture, not invented public variables. Paths retain native identity
and the existing detached `PathValue` representation; they are not string/map
substitutions. Existing `nodes`, `relationships`, `length`, equality and DELETE
can consume them. Multiple MERGE edge matches produce distinct paths rather than
collapsing to the first edge. Updates through native aliases remain owner-visible.
Detached paths returned to callers do not grant later transaction write authority.

The AST retains `PatternPath.variable`; `CreateRelationships` and `MergePattern`
public plan snapshots add `path_variable: str | None`, with the `path` detail.
Parser/analysis/plan validation still rejects malformed names, shape mismatches and
collisions with node/relationship variables. No persistent format or configuration
field changes.

## Undirected MERGE and native endpoint functions

`MATCH(a:A),(b:B) MERGE p=(a)-[r:R]-(b)` searches both physical
orientations. Every matching edge receives its conditional action; parallel edges
remain distinct and a self-loop is processed once. Only if neither orientation
matches does it create the written left-to-right edge. Searching a reverse match
does not install an unused forward member of a flexible relationship type. A typed
schema permits only its declared endpoint pairs: a reverse existing edge can match,
but a missing forward pair still refuses creation rather than reversing the write.
Owner views for both orientations are fixed before applying conditional actions.

Captured paths follow written node order; returned relationship source/target
identities retain physical orientation. `startNode(r)` and `endNode(r)` return the
actual source and target **native nodes**, respectively, not IDs or property maps.
They accept one relationship argument, propagate NULL, and reject other argument
types. Stored, pending and owner-updated endpoints resolve using the transaction's
qualified node identity and visibility rules, including same-name node/relationship
namespaces. They can feed property reads, aliases, CASE, lists/UNWIND, MATCH,
SET and DELETE. Reading an endpoint from a relationship deleted by the current
transaction refuses live-content access and rolls back the failing statement.
Detached result DTOs do not become parameters with write authority.

```cypher
MATCH(a:A),(b:B)
MERGE p=(a)-[r:R]-(b)
ON CREATE SET r.created=true
RETURN p, startNode(r), endNode(r)
```

Plain undirected CREATE remains invalid. Double-arrow writing (`<-[r:R]->`)
also remains invalid for CREATE/MERGE; the AST's `both_directions_written` boolean
preserves this spelling so a canonical description/plan-cache lookup cannot turn
an invalid write into a valid undirected MERGE. Read double-arrows keep their
existing undirected semantics. This is an AST/spelling addition, not a durable
catalog or configuration change.

### Sorted write bindings

Qualification exposed a pre-existing `WITH ... ORDER BY ... SET` failure when
`query_memory_budget_bytes` enables external sorting. The private sort payload
now preserves only bindings retained by its projection. A stored entity's write
reference is reacquired against the existing snapshot-qualified identity access
path when restoring a retained binding and at SET/map-SET/DELETE consumption,
never from its detached property values.
Owner-visible result bindings are refreshed after SET. DISTINCT/order/windows
must not drop native authority, restore a discarded variable, or promote maps to
entities. Temporary binding inventories are validated; existing memory/workspace
limits and cleanup remain enforced. No per-result global authority cache, new
setting, weakened OCC, or additional COMMIT is introduced.

## Large CREATE pipelines

Consecutive CREATE patterns now form a native `CreateSequence` with one input and
an ordered tuple of immutable `CreatedPattern` instructions. Each input row runs
every instruction in written order before the next input. The instructions retain
separate node/relationship property evaluation, actual bound endpoint identities
and named-path capture. This is not a merged property map or reordered bulk load:
later patterns can read nodes and paths created by earlier patterns. SET, MERGE,
WITH and other intervening operators remain boundaries; they are not folded into
the CREATE program. Single-pattern CREATE retains `CreateRelationships`.

Execution is iterative in the number of consecutive patterns, so a large source
does not add one Python generator/plan frame per CREATE. Public EXPLAIN and
`result.plan` expose the ordered instructions without a deeply nested operator
tree. Their snapshots remain independently owned, including nested expressions.
The actual write statistics and statement/transaction quotas still apply to every
insert; `create_patterns_executed` additionally counts executed program steps.
`max_intermediate_rows` remains per physical operator, not a count of source clauses.

The syntax admission contract now distinguishes source pipelines from UNION and
MERGE actions: 65,536 source characters, 32,768 lexer tokens excluding END,
1,024 written pipeline clauses (including RETURN), 64 UNION branches and 64
conditional SET clauses per MERGE. The existing expression depth 48, physical
operator depth 192 and per-clause pattern bounds remain unchanged. Long non-CREATE
pipelines can still hit the physical-plan guard; raising source admission is not
an unlimited stack or execution promise. `MAX_PIPELINE_CLAUSES` is exported;
`MAX_CLAUSES=64` now denotes the separate UNION/action ceiling. No connection
configuration field was added and no process-global recursion limit is changed.

The same outer statement rollback mark covers every instruction, private read
phase and newly installed flexible schema. No implicit COMMIT or transaction
splitting occurs. A late invalid/nonfinite property or budget failure discards the
whole statement; proven earlier statements can still commit. Empty input and
read-only refusal install no schema. Independent readers/writers and recovery
remain under the existing snapshot/OCC/WAL protocol. Public write transactions
retain their existing refusal of read-only deadline/cancellation controls; this
increment does not advertise a new write-cancellation API.

```python
with db.begin("write") as tx:
    result = tx.execute("""
        CREATE(a {value:1})
        CREATE(b {value:a.value+1})
        CREATE p=(a)-[:NEXT]->(b)
        RETURN a.value,b.value,length(p)
    """)
    assert result.rows == ((1,2,1),)
```

The original Create4 fixtures contain 212 CREATE clauses / 7,561 tokens and
759 CREATE clauses / 17,617 tokens respectively, including the lexer END token.
Both now execute their unchanged queries and expected effects. The complete CREATE
family passes **76/78**, with zero required failures and two retained multiple-label
divergence failures; all 78 selected cases execute and 3,819 are outside selection.
These figures do not claim completion of general MERGE, label mutations or FP-4.

### Large-CREATE qualification receipts

Original fixtures, expected effects and both frozen ledgers are unchanged. The
first local run's single failure was the previous 64-clause parser expectation;
the replacement pins the new written-clause bound while retaining the separate
64-branch/action policy. A subsequent local test expected `field=clauses` from
direct-AST admission, but that existing door reports `field=clause, value=clauses`;
the test now checks that actual structured contract, without changing the oracle.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `large-create-original-first.json` | Both original Create4 cases passed; 3,895 outside | `aedcb5407bc389712db76456cd5319e7c80970ec1ca7b0ecee77ede1021e1934` |
| `large-create-family-qualified.json` | 76 required passes / two retained divergence failures; 78 selected, 3,819 outside | `24f12572004060c959810310fcee17efd8d8fce7bcf5916acdf864509f521603` |
| `large-create-focused-qualified.xml` | 14 passed, zero failures/errors/skips; 22.095 s | `3daaffa5a793e3f1031ccff176b0767dcaa76627ac246f425471e85b89732e97` |
| `large-create-regression.xml` | 947 passed, zero failures/errors/skips; 131.929 s | `789b35b9808708a388b8adb328c718641f63b72f373b53b0300178ef89199910` |
| `fp4-post-create-sequence-native.json` | Complete FP-4 owner: 245 passed / 40 required failures / five retained divergence failures; 290 selected, 3,607 outside | `11625ca31240fbbe72cef1ae37f77528295d3390aa09c89ceb2cbd7c89db258a` |

The focused tests cover actual 1,024-CREATE execution/reopen with pure and NumPy,
constant physical depth, independent literal token/clause bounds and direct AST,
source dependencies/named paths, read-only/empty input, quota refusal/late rollback,
public snapshot mutation isolation, old reader snapshots and conflicting writers.
Four subprocess cuts exercise a 139-step node/edge program: before COMMIT versus
after durable COMMIT before page application, in pure and NumPy, then reopen and
verify twice. No uncommitted prefix survives; all committed nodes/edges survive.
The 947-test grouped regression covers the affected syntax, plan/public snapshots,
query writes, nested calls/UNION, scope/entity composition, namespaces, endpoint
authority, NaN storage and read-control boundaries. Collections overlap.

All 201 passes in the previous complete post-SET-map owner selection remain
passing; the new owner receipt is not inferred by adding family counts. The 40
required failures are 29 label-mutation cases, ten existential-subquery cases and
one unchanged negative storage case, Set1 #0010, that expects nested map-list
storage to fail despite the explicitly authorized broader storage capability.
Some required label-action cases also demand adding a second label, although the
plan excludes arbitrary multiple labels. These conflicts remain visible and
unwaived; neither functionality nor the frozen ledger was silently changed to
resolve them. Further owner completion, model/profile reconciliation and final
Pulse/wheel qualification remain outstanding.

A source search found no `CreateRelationships`, `MAX_CLAUSES` or `MAX_TOKENS`
consumer in the tracked Pulse Community/Core working copies. This is only a
consumer-mapping check, not installed/API/browser qualification. No Pulse source,
production data, global installation, branch, release or persistent format changed.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/query/test_create_sequence.py tests/txn/test_create_sequence_recovery.py -q --tb=short
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/large-create-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/create/
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp4-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-4
```

## Binding rules and explicit errors

A standalone CREATE/MERGE node must be a new variable in that scope.
`MATCH(n) MERGE(n)` and `MATCH(n) CREATE(n)` are errors, not no-ops. An already bound
node can be referenced as an edge endpoint by its **bare variable**, but a writing
pattern cannot redeclare its labels/properties: use `MATCH(n:Person) CREATE(n)-[:R]->(m)`
instead of repeating `(n:Person)` or `(n {...})`. Use SET to modify properties.
A newly declared node can still be referenced later in the same pattern, including
a self-loop. Relationships cannot be rebound or declared twice in a writing pattern.
These checks occur during planning, including when upstream input is empty.

Native diagnostics identify the actual source contract:

| Native reason / field | Phase | Reference error |
| --- | --- | --- |
| `variable_already_bound` / `variable` | planning | `VariableAlreadyBound` |
| `no_single_relationship_type` / `types` | planning | `NoSingleRelationshipType` |
| `creating_variable_length` / `hops` | planning | `CreatingVarLength` |
| `requires_directed_relationship` / `direction` | planning | `RequiresDirectedRelationship` |
| `merge_null_property` / `properties` | execution | `MergeReadOwnWrites` |

Relationship pattern property expressions are evaluated once per input. A NULL
value supplied to a MERGE pattern refuses at runtime before matching/inserting;
this is different from removing a property with `SET ... = NULL`. The same rule
applies to node patterns. The conformance runner maps explicit reason/field/phase
contracts, not message text or the test's expected error.

## MERGE after mutations

MERGE reads the owner-visible graph. A preceding mutation clause must finish for
all inputs before MERGE decides whether to match or create. The planner now adds
the existing private read-phase boundary when a pending write precedes MERGE.
For `MATCH(a:A) DELETE a MERGE(n:A)`, MERGE cannot see a node awaiting deletion
from the previous input row. The same ordering applies after SET, CREATE or MERGE.

This reuses native eager phase preparation, query/intermediate-row bounds,
transaction-private publication and the outer statement rollback mark. **It is
not a COMMIT**, independent inner transaction or a global writer lock. Independent
readers retain their snapshots, other writers retain OCC, and a later failure must
discard every private phase of that statement. Absent COMMIT cannot make any
phase durable; recovery still requires native COMMIT proof.

## Undirected/endpoint follow-up qualification

The original Match4 #0004 is reconfirmed without fixture adaptation or coercion.
Its entire selected file has nine required passes and one retained multiple-label
divergence failure, with 3,887 outside selection. No new divergence was registered.

The focused endpoint/write suite passes 54 tests after correcting the issues found
during qualification: lost WITH bindings through sort, reacquisition of committed
write references, refreshed SET outputs, and sequential MERGE actions retaining
the identity of private inserts. Both pure and NumPy are covered; tests include
reverse-only typed/flexible MERGE, distinct parallel matches, self-loops, sorted
endpoint writes, wrong types/arity, NULL, owner updates, old reader snapshots and
conflicting writers. Additional existing endpoint-ownership/spill checks passed
in a 138-test selection. These overlapping collections are not additive.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `flexible-match4-reconfirmed.json` | Nine required passes / one retained divergence failure; Match4 #0004 passes | `c7544ce8328e8b6d5d9168049b3170bef4b5bd64aea54476235c74adbe663a8c` |
| `undirected-endpoints-merge-qualified.json` | 70 passed / four required failures / one retained divergence failure; 75 selected, 3,822 outside | `55de64d98a4f5a5305b5544263987a8c76ff63e50a5d122c8ca01994fc6c6da7` |
| `undirected-actions-final.xml` | 54 passed; zero failures/errors/skips; 13.087 s | `0cb547b28b6a0983813d74b629b2c291258ab11ff406361b30e8ee36a27907cf` |
| `undirected-endpoints-regression-final.xml` | 1,016 passed; zero failures/errors/skips; 226.530 s | `b2a00bbcaaf21d65eaca726c841a4f5b17b8e2e7d77b825b94b44bbab3c130dd` |
| `undirected-sorted-authority-final.xml` | 138 passed; zero failures/errors/skips; 46.028 s; before the final sequential-action correction | `81a8755b4dd7ef4471feeca016e277420a30ae69db1ef756177a111dedd70223` |

The initial broad regression (`undirected-endpoints-regression.xml`, SHA-256
`c3ddf6e96d1d8c0f0abffb6230e3c6322b845f034358a10acad447179b1f24b9`)
had 1,012 passes and two failures in the unchanged sequential-MERGE-action tests.
Unnecessarily replacing an uncommitted binding before SET changed its private
version identity; the graph/result refresh no longer agreed. The correction limits
write-reference reacquisition to stored bindings that actually lost their physical
reference. The failed receipt is preserved, not presented as a passing gate.

The final 1,016-test regression reruns the complete affected collection after that
correction: parsing, analysis, planning, native query/entity composition, projected
scope, sorted and DISTINCT writes, map SET, expression DELETE, conditional actions,
writing subqueries/UNION/imports, namespaces, public plans and fault/recovery.
The written-path crash matrix now covers directed and undirected MERGE in pure
and NumPy, before COMMIT and after durable COMMIT before page application, including
an update through `endNode(r)`. Reopening/verifying twice proves the expected single
transaction outcome. This is source qualification, not a new wheel/Pulse installation
or complete owner/TCK result. Documentation/link/configuration and changed-source
Ruff checks also pass; no new configuration field was introduced.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/query/test_undirected_merge.py tests/query/test_relationship_endpoints.py tests/query/test_sorted_write_bindings.py tests/query/test_merge_actions.py tests/txn/test_written_path_recovery.py -q --tb=short
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/undirected-merge-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/merge/
```

## Evidence and remaining requirements

No original fixtures, expected rows/errors/effects or frozen V2/V1 ledgers changed.
Receipt names below are under `.grafx-tmp/`; selections and local regressions
overlap and are not additive.

| Receipt | Result | SHA-256 |
| --- | --- | --- |
| `write-pattern-contracts-original-second.json` | CREATE only: 74 passed / two required failures / two retained multiple-label divergence failures; all 78 selected executed, 3,819 outside | `1912de6ebc8b068e0fc42c9c39c0771a621e20aec10e37d9137229033c8fc88e` |
| `write-pattern-contracts-merge-final.json` | MERGE: 67 passed / seven required failures / one retained divergence failure; all 75 selected executed, 3,822 outside | `4283291a82373902f3af3c673b1733039775c469db084177d4040eaa004fe7c1` |
| `write-pattern-contracts-second.xml` | 1,023 passed / zero failures, errors or skips; 97.901 s | `1d54ba25015145e5a9b88ecadcdb2a216225f2fd8f4a28ebb376c33bc2146d8d` |
| `written-paths-focused.xml` | 16 passed / zero failures, errors or skips; 4.498 s | `3a4cc3338631cf4d5f71d446cd6d5dba7770646dee255d869525525d7f55a4ef` |
| `write-pattern-contracts-final.xml` | 168 passed / zero failures, errors or skips; 126.977 s | `2fe72fcb5631df8691df10eef5e7904905d9a8a447ec91ba2e4f48fab8c6a3a6` |

The runner accepts one `--feature-prefix`; the CREATE receipt above is explicitly
CREATE-only, despite its generic filename. It is not combined CREATE/MERGE or a
full-owner result. The initial MERGE receipt had 64 passes / 11 failures while
an old blanket writing-path analysis guard still rejected captures and the
DELETE-to-MERGE phase bug remained. The first local regression retained two
outdated local expectations (all written names must fail parsing; bound-property
redefinition must report `field=properties`). Those local tests now assert the
new actual path/admission contract; original oracle files were not rewritten.
Initial receipts remain available: `write-pattern-contracts-merge-first.json`
SHA-256 `6960d92b7a89aa3339ee8c3ce8a94479db0a1462add67617259ed218c2cb55eb`,
and `write-pattern-contracts-first.xml`
SHA-256 `127c225576113e354110f45e5348e692b266f4fa86b02c2ae18e8abd779b8ba4`.

The 1,023-test grouped regression covers syntax, analysis/planning, native query
execution, captured paths, conditional actions, map SET, expression DELETE,
deleted-content access, writing subqueries/imports/UNION and public-plan snapshots.
The focused tests cover pure/NumPy direction/cycles, distinct edge paths,
path-dependent actions, same-statement delete/MERGE ordering, nested rollback,
invalid redeclarations with empty input and NULL MERGE property rollback.
The final 168-test collection includes these cases, explicit diagnostic-mapper
negative tests, namespace/import/public-plan regressions and both existing
writing-CALL/UNION/MERGE crash tests and four new phase/capture crash cases.
The new cases use pure/NumPy codecs, cut before COMMIT or after durable COMMIT
before page application, and reopen/verify twice. Deleting two old nodes, creating
one replacement through repeated MERGE input and capturing/creating its edge
recover as one transaction outcome. No private read phase survives without COMMIT.

The original 67-pass MERGE receipt predates the undirected/endpoint increment.
Its follow-up passes **70/75**: the three undirected cases now pass; the four
label-action cases and retained multiple-label fixture divergence remain.
General unbound/fixed multi-hop MERGE is implemented by the subsequent
[whole-pattern increment](GENERAL_MERGE_V1.md), with separate native tests;
those shapes are not inferred merely from named paths working.
The two CREATE failures in the historical receipts below arose
from the former 64-clause / 8,192-token admission; the large-CREATE increment above
closes both without rewriting their fixtures. Remaining labels, existential subqueries, namespace consumers, stored
types, writing procedures and final Grafx/Pulse acceptance remain in the main plan.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/write-patterns-merge-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/merge/
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/write-patterns-create-reproduced.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix clauses/create/
python -m pytest tests/query/test_written_paths.py tests/txn/test_written_path_recovery.py tests/txn/test_merge_actions_recovery.py tests/txn/test_write_subquery_recovery.py tests/txn/test_updating_union_recovery.py tests/tools/test_tck_write_pattern_errors.py tests/query/test_fp3_created_node_references.py tests/query/test_graph_namespaces.py tests/query/test_subquery_imports.py tests/api/test_query_result_plan_door.py -q --tb=short --junitxml=.grafx-tmp/write-patterns-reproduced.xml
```
