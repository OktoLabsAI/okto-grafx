# Native EXISTS subqueries

Implemented on `feature/v0.0.6` and shipped in the 0.0.6 release; not a
complete FP-4/Cypher-parity claim. This closes the existential-subquery family
without rewriting upstream fixtures or relaxing the storage/transaction model.

## Query and consumption contract

The subsequent [view consumer correction](../reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md)
captures expression-local physical dependencies in persistent logical views and
enforces their explicit-label/type and reserved-metadata rules across nested
scopes. This does not restrict ordinary native EXISTS queries.

```python
from okto_grafx import connect

with connect("./example.grafx") as db:
    with db.begin("write") as tx:
        tx.execute("CREATE(a:Person {name:'Ana'})-[:KNOWS]->(:Person {name:'Bia'})")
    result = db.execute("""
        MATCH(person:Person)
        RETURN person.name AS name,
               EXISTS { (person)-[:KNOWS]->() } AS has_contact
        ORDER BY name
    """)
    assert result.rows == (("Ana", True), ("Bia", False))
```

The existing `execute`, query builder, prepared execution and cursor APIs consume
this syntax; there is no separate transaction, session or configuration option.
The result is a Python `bool` (JSON boolean). The body is a native read query:
`MATCH`, `OPTIONAL MATCH`, `WHERE`, `WITH`, `UNWIND`, supported read `CALL`,
aggregation, ordering/windows, nested EXISTS and `UNION`/`UNION ALL` compose under
their normal contracts. A pattern plus optional WHERE may omit MATCH, including
named paths. This does not add other Cypher 25 conditional-query syntax.

Examples:

```cypher
MATCH(n) WHERE EXISTS { MATCH(n)-->(m) WHERE m.active=true } RETURN n
MATCH(n) RETURN EXISTS { MATCH(n)-->(m) WITH count(*) AS c WHERE c>2 RETURN c }
MATCH(n) RETURN EXISTS { MATCH(n)-[:R]->() UNION MATCH(n)-[:S]->() }
MATCH p=(a)-->(b) RETURN [n IN nodes(p) | EXISTS { (n)-->() }]
RETURN EXISTS { UNWIND [] AS x RETURN count(*) }
```

The last example returns true: a zero-input aggregate without grouping still
produces one row. EXISTS tests row existence, not whether a projected value is
true/non-NULL. `EXISTS { RETURN null }` is true; `EXISTS { RETURN true LIMIT 0 }`
is false. OPTIONAL null-extension is a row and can therefore make EXISTS true.
A NULL correlated entity never becomes an unbound full-graph scan.

RETURN is optional. If present, expressions need not be aliased and their names
are not exported. All UNION branches must agree on RETURN presence; ordinary
UNION column/duplicate-policy rules still apply when RETURN is present. Omitted
RETURN is normalized to a private constant projection, only inside EXISTS.
Ordinary standalone read queries still require RETURN.

## Lexical scope and side effects

Outer variables are implicitly visible throughout the body, including across
WITH. They cannot be shadowed by a different inner binding. Inner variables never
escape. Each input row supplies its own captured values; an uncorrelated body
does not acquire accidental dependencies on unrelated outer bindings. List-local
variables and pattern-comprehension scopes retain their identities. Native entity
provenance is required for graph anchors; parameter maps/IDs do not grant it.

Parameters anywhere in the body are admitted with the enclosing statement,
including unreachable branches. Names, types, arity and write refusal are checked
at planning, even for empty outer inputs or skipped boolean branches.
`EXISTS { ... SET ... }`, CREATE/DELETE/MERGE and writing CALL bodies refuse with
`GrafxPlanError`, `reason="existential_write"`, `query_phase="planning"`. Inconsistent
UNION RETURN presence uses `existential_return_mismatch`. The conformance mapper
maps these explicit causes to compile-time `InvalidClauseComposition`; it does
not recognize errors by message substrings.

Native expression runtime errors in a reached body propagate. Lazy boolean/CASE
evaluation can skip a body; the first produced row can stop further enumeration.
This is not permission to skip cardinality-affecting aggregation or row windows.

## Execution, limits and authority

Each expression compiles one correlated native read descriptor as part of the
outer plan. It does not parse/plan another text query per row. Invocations use
the outer snapshot, read/index authority and cancellation/traversal/memory budgets.
The argument slot and inner iterator are closed on success, early termination,
runtime failure and cancellation. Primary exceptions retain precedence over
cleanup failures. Independent readers/writers keep their original snapshot/OCC
and lease contracts.

No result cache or new tunable is introduced. Existing subquery nesting (16),
source/expression/plan limits, UNION branch bounds and query memory controls apply.
An existential predicate may avoid work after its first match; an aggregate or
sort can still consume its whole input. Sorting/grouping retain native bounded
spill behavior. Memory limits are logical operator limits, not a promise about
all Python allocations or RSS. Broad graph enumeration is not guaranteed O(1).

EXISTS can be used by an outer write statement, such as a CREATE result or SET
value. It reads the relevant native private read phase, does not commit, and a
later failure retains whole-statement rollback. No WAL, persisted type, catalog
capability or database-format change is introduced.

The public plan contains an owned `ExistsSubquery(query, imports)` expression.
Its query/UNION and read-clause inventories are canonical frozen syntax; separate
results get independent nested snapshots. `imports` maps inner spellings to
outer lexical identities, not to storage handles. The compiled read descriptor
remains execution-internal, as with pattern comprehensions. Forged syntax,
collections and cycles do not become trusted merely by occurring inside EXISTS.

## Qualification

Original source selection:

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/exists-original-first.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --feature-prefix expressions/existentialSubqueries
```

The first native run passes **10/10 required cases**, 3,887 outside selection,
with original source queries and no fixture adaptations. It predates the final
composed-scope/boundary regression; it is not a whole-profile gate.
Feature and hostile-input tests are in `tests/query/test_exists_subqueries.py`
and `tests/api/test_exists_plan.py`. The full parity plan remains active.

Final native family receipt: `.grafx-tmp/exists-original-qualified.json`, again
**10/10 passed**, zero selected failures, 3,887 outside selection. SHA-256
`631ad682368b195825c16de193585607754870d9b392c70663434c43941f7943`.
The first and final family artifacts have the same digest; no expectation changed.

The complete owner run `.grafx-tmp/fp4-post-exists-native.json` records **255 passes,
30 required failures, five retained divergence failures**, all 290 cases selected
and 3,607 outside. SHA-256
`4d75f3ef8b3b3c84bfd3344d2259a8803a6c1e264a7045e2e7f550fff005ee79`.
Comparison by case ID confirms all 245 previous owner passes remain. This owner
run preceded the final list-local-only follow-up; the final family receipt above
and final local regression qualify the latter, not an invented rerun of all owners.
The remaining 30 required failures comprise 29 label mutations and Set1 #0010's
negative nested-storage oracle. Multiple-label/model conflicts and that negative
oracle still require a versioned decision; no case was reclassified here.

Focused final receipt `.grafx-tmp/exists-final-focus.xml`: **52 passed**, zero
failures/errors/skips, 17.504 s, SHA-256
`6f93ade910b384e95e18a681666196c186e1cc51be282eb1f4c3554d6fcdc8d8`.
The independent write/recovery receipt `.grafx-tmp/exists-write-recovery.xml`
passes **four subprocess cuts**, pure/NumPy before COMMIT and after durable COMMIT
before page application, 17.605 s, SHA-256
`0600b130eb02f9bb36a06c021680b1c219303214f50fbb915c072d8319836742`.
The child combines CREATE, SET-from-EXISTS and a post-SET EXISTS read; two reopens
verify complete committed effects or none, while retaining unrelated saved data.

The first broad receipt `.grafx-tmp/exists-regression-first.xml` is retained as
failure evidence: 969 tests, two failures (named-path shorthand and a stale
pre-expression-DELETE semantic-only assertion), SHA-256
`39e14d2960e80d1782642f115c62e5b16f60a3da285a194ff64b2494dcf60e1b`.
The latter test now asserts catalog-aware scalar DELETE refusal and unchanged
data, rather than forbidding all expression aliases. The repeated 969-test receipt
`.grafx-tmp/exists-regression-qualified.xml` passes, 194.856 s, SHA-256
`f506bd651099885a7c67d530db063bcf81d64ff31bfa4a7ed48b318750c14bf4`.
It precedes the final nested-list/NULL/dependency follow-up; the final combined
regression is recorded separately. Focused and broad counts overlap, not add up.

**Final combined regression:** `.grafx-tmp/exists-final-regression.xml`, **978
passed, zero failures/errors/skips**, 191.897 s, SHA-256
`03fab23d7ee530f18cb1c481ed19569a6c28a438f7ef77070851748ddf5fd3ee`.
It includes the 52 feature/public-plan cases, explicit conformance error mapping,
parser/AST/analysis/bounds, WITH/UNION, list/entity scopes, predicates and pattern
comprehensions, cancellation/cursors, read/write CALL, independent public-plan
snapshots and all selected subprocess recovery cases, including the four new
EXISTS write cuts. It does not claim a full repository/FP-8 or installed-Pulse run.
Generated API documentation, documentation/configuration validation (39 fields,
11 preserved plans), changed-source Ruff and whitespace checks pass.
No commit/push, package installation, release or production-data mutation was
performed for this increment.

The semantic reference is the [Neo4j EXISTS documentation](https://neo4j.com/docs/cypher-manual/current/subqueries/existential/),
checked alongside the pinned openCypher scenarios. This feature does not establish
general Neo4j version compatibility or remove the other pending FP requirements.
