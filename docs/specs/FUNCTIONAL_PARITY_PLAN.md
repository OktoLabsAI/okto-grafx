# Functional parity expansion: language, values and executable conformance

[Roadmap and delivery status](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Current compatibility](../CYPHER_COMPATIBILITY.md) ·
[Product comparison](../FEATURE_COMPARISON.md)

Prepared September 11, 2026; subsequently authorized for implementation by the user.
This document preserves the agreed scope, including all six functional fronts and
the unresolved TCK inventory. It does not itself certify any package, change a
version/branch, authorize a release or modify the installed Pulse. ROADMAP.md
remains the single delivery-status authority.

## 1. Outcome and fixed boundaries

Deliver a substantially broader, tested native query/value contract for Python
local-first applications, with coordinated Pulse consumption. Address **all six
requested fronts**, not just the current Pulse queries. Pulse need not already use
a capability for that capability to be included.

Two different claims must remain separate:

1. **Functional parity in the declared profile:** all agreed capabilities and
   scenarios pass with explicit types, errors, effects and operational contracts.
2. **Full openCypher conformance:** all 3,897 pinned cases execute and pass their
   original expectations, without semantic substitutions or profile exclusions.
   This plan does not promise that claim while
   intentional schema/model/arithmetic divergences remain. Passing a selected
   profile must never be relabeled as passing the entire TCK or all Neo4j features.

Non-negotiable invariants: independent local readers/writers, existing snapshot/OCC
and publication fencing, whole-statement failure isolation, WAL/durable commit,
verified recovery, bounded resources, and an agnostic production Pulse Core.
No cross-store transaction, external-effect rollback or universal serializability
promise is introduced.

Explicitly outside this delivery: schema-free storage/arbitrary multiple labels,
NaN/infinity storage, Bolt/native language drivers, APOC/GDS parity, server RBAC,
HA/replication, remote federation, valid-time/bitemporality, independent inner
commits (`CALL ... IN TRANSACTIONS`), general persistent tagged-union/ANY columns,
and unlimited traversal. These are not prerequisites for this local-first profile.
Limits must produce explicit refusal, not silent result truncation.

## 2. Baseline: evidence, not estimates of implementation effort

The pinned reference remains **openCypher TCK 2024.3**, commit
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`: 220 feature files / 3,897 expanded cases.
The recorded positive-expression diagnostic is **1,004 passed / 1,077 failed /
1,816 not run**. This is not a full-suite execution or a parity percentage.
[Current contract and reproduction](../CYPHER_COMPATIBILITY.md).

Read-only grouping of `.grafx-tmp/opencypher-expressions-final.json` found:

| First observed failure | Cases | Planning consequence |
|---|---:|---|
| Missing `datetime`, `localdatetime`, `date`, `time`, `duration`, `localtime` | 483 | A coherent temporal value/function family, not hundreds of unrelated patches |
| Missing `rand` | 63 | Native nondeterministic function semantics and independent tests |
| Parser message `RETURN ends a query` | 482 | Inspect actual syntax; the message is not a diagnosis of 482 illegal clause sequences. Hex literals are one confirmed example. |
| Unaliased result-column spelling | 5 | Preserve expression source spelling at the result boundary |
| Other first failures | 44 | Assign individual causes: chained comparisons, additional syntax/functions, named paths and explicit policy limits |

These disjoint buckets total 1,077; they are **not** projected pass gains. Fixing
the first blocker can expose another. JSON SHA-256:
`05a2b445bf1ec87656501f724ec90f71ea4127177070c0ae986fcc1ec47b3734`.
The local receipt is reproducible evidence, not a runtime dependency.

Current implementation evidence guiding the work:

- [`tools/check_opencypher.py`](../../tools/check_opencypher.py), especially
  `run_read_case`, only executes a supported set of read/empty-graph steps;
  missing fixture/error/effect support explains part of `not_run`.
- [`COMPOSABLE_QUERIES.md`](../COMPOSABLE_QUERIES.md) explicitly restricts entity
  UNION output, subquery imports/writes and pure tabular procedures.
- [`domain/model/value.py`](../../src/okto_grafx/domain/model/value.py) already has
  LIST/MAP encoding and depth limits. The nested-column task is public typed
  schema/planner/index/interop completion, not reinventing that codec.
- [`domain/query/extensions.py`](../../src/okto_grafx/domain/query/extensions.py)
  currently grants no graph transaction to callbacks. Writing procedures need a
  controlled transaction capability, not an unrestricted Database handle.
- [Latest Pulse acceptance](../reports/PULSE_V006_INTEGRATION_REGRESSION.md)
  provides the consumer regression baseline; it is not a full historic Pulse suite.

## 3. Ordered delivery packages

Effort is relative and includes tests/docs: **M** = several existing components;
**L** = planner/executor/API changes; **XL** = persistent format or broad value
semantics. These are not calendar estimates. Each package must deliver native
execution and public consumption, not only grammar or test-harness support.

| Order | ID | Deliverable | Effort | Principal prerequisite |
|---|---|---|---|---|
| 1 | FP-1 | Fixed capability/case matrix and stateful TCK runner | L | Existing pinned inventory |
| 2 | FP-2 | Remaining scalar syntax/functions and faithful column names | M | FP-1 |
| 3 | FP-3 | Qualified entity results, polymorphic patterns and named paths | L–XL | FP-1; FP-2 simplifies expression composition |
| 4 | FP-4 | Returning/unit read-write subqueries and scope imports | L | FP-3 entity binding; existing statement rollback |
| 5 | FP-5 | Native temporal values, constructors/operations and persistence | XL | FP-1; FP-2 postfix expressions |
| 6 | FP-6 | DECIMAL and typed persisted collections/structures | XL | FP-5 shared type/format admission machinery |
| 7 | FP-7 | Transaction-scoped writing procedures | L | FP-3/4; FP-5/6 signature/value integration |
| 8 | FP-8 | Final profile/TCK accounting, Pulse migration and delivery evidence | L | FP-1 through FP-7 |

Independent value/codec work can run alongside query work after FP-1, provided
shared type contracts are settled first. The default implementation order remains
the table above. FP-8 is final integration, **not the first time these features get
tested**.

### FP-1 — Freeze the matrix and make the TCK executable

Create a versioned case ledger keyed by upstream case ID + source checksum, with
required capability, package owner, observed result, root-cause family, expected
error phase/effects, and profile inclusion or explicit architectural divergence.
Account for all 3,897 cases. Preserve the original source queries and expectations.
Publish separate **upstream** and **Grafx-profile** results; never rewrite queries
or expected answers and report the adapted result as upstream conformance.

Extend the runner to handle graph fixtures, sequential setup/query steps, typed
parameters/results (including entities/paths and later new values), empty results,
ordered/bag comparison, writes, expected errors and observable side-effect counts.
Verify no unintended effects after failures using a subsequent transaction; use
durable reopen for mutation families, not a full filesystem scan per scalar case.
The reference's error phase is evidence: an earlier refusal is not automatically
the expected runtime error. Compare against independent expected values, not the
Grafx evaluator as its own oracle.

If typed schema must be declared to load a graph fixture, record that adaptation.
Cases depending on schema-free writes/multiple labels remain visible divergences;
do not evade them by rewriting the operation under test. Tests of the runner must
deliberately inject wrong rows, missing/extra effects and wrong errors to show the
runner actually fails.

**Exit:** every case classified and assigned; all step families inventoried;
runner paths for compatible fixture/write/error cases tested; a frozen list of
initial blockers, not a requirement that unimplemented engine features already
pass. Modern subquery/procedure extensions get separately versioned scenarios,
since their coverage cannot be inferred from this older TCK.

**Checkpoint A:** present exact case/capability coverage before FP-2. A new
model/transaction requirement outside the six fronts is not silently added here.
Every in-scope blocker remains assigned even if first revealed in a later package.

### FP-2 — Expressions, scalar functions and column spelling

Implement hexadecimal/octal literals, including signed boundaries and consistent
overflow errors; chained comparisons with the reference's NULL/evaluation rules;
and general property/index postfix access on expression results rather than a
special case for named variables. Preserve the original expression text for
unaliased result-column names without changing normalized planning/cache keys.

Add native `rand()` with declared nondeterminism: no constant folding or cached
result across executions, and no admission to deterministic index/view definitions
unless their contract permits it. Test range/type/evaluation placement through a
controllable random source, not fragile assertions that two draws must differ.
Assign `properties`/`labels` and any entity-dependent scalar cases to FP-3; they
must not vanish from the matrix because they are not one of the temporal functions.

**Exit:** all FP-2-owned cases pass; original five column-name failures are covered
without mandatory user aliases; numeric/NULL/overflow and runtime cost regressions
pass. No NaN/infinity policy change is hidden inside arithmetic compatibility.

### FP-3 — Entity identity, polymorphism and paths

Define one public detached representation for nodes, relationships and paths,
separate from internal live execution bindings. Identity must distinguish database,
table/schema identity, element kind and record incarnation; a table-local ID alone
is insufficient. Specify snapshot provenance, deleted/recreated objects, property
materialization, equality/hash and serialization. Returned objects must not keep
transactions alive or permit using foreign/stale entities as writable handles.

Use this identity across `UNION`/`UNION ALL`, DISTINCT/grouping, nested list/map
results and subsequent query clauses. Test equal IDs in different tables/stores,
aliases, old/new snapshots, duplicate entities and edge endpoints. Document the
public result change and provide detached JSON representations for consumers.

Generalize reads over compatible node tables and relationship-type alternatives
inside composed MATCH/OPTIONAL MATCH/subqueries. Missing properties and NULLs must
follow the declared profile, not disappear because the first table lacks a column.
Support named variable-length paths, both directions/undirected traversal, zero
length, cycles, bounds, relationship uniqueness, `nodes`, `relationships`, `length`,
`type`, `labels` and `properties`. Repeated nodes and repeated relationships are
not interchangeable: freeze the reference's trail semantics per pattern/clause.

Use a bounded streaming traversal and shared query cancellation/work budgets.
An omitted syntactic upper bound may use that budget but must never silently
pretend a truncated enumeration is complete. Avoid global scans when an endpoint
or compatible index can anchor the query; do not promise that all graph enumeration
can be sublinear. Shortest-path modes beyond the frozen scenarios are not implied.

**Exit:** entity UNION and general named-path fixtures pass with exact multiplicity,
null extension and identity; cross-table collisions cannot merge different nodes;
cursor close/cancellation releases resources; read/write snapshots remain isolated.

### FP-4 — Subqueries that read and write

Support returning and unit subqueries, explicit imports and the agreed leading-WITH
import form. Freeze modern extension cases separately from the pinned TCK before
implementation. Define per-input-row evaluation, imported-name shadowing/collisions,
aggregation over zero rows and output cardinality: unit subqueries preserve the
outer row; returning subqueries contribute the rows they actually return.

Allow inner writes and subsequent reads, including composed updating branches
where permitted by the frozen profile. All use the **same outer transaction**,
snapshot, quotas and single statement rollback boundary. No inner commit and no
cross-store work. An error late in any invocation or in final result conversion
discards effects from every invocation of that statement. Earlier successful
statements are retained only when rollback is proven; otherwise abort the transaction.

Reject a write subquery before effects when invoked through a read transaction or
read-only cursor. Define interruption of streamed write results explicitly so an
abandoned consumer cannot accidentally finalize only part of a statement.

**Exit:** correlated/uncorrelated, zero/multiple-row and nested scenarios pass;
fault injection across inner writes and final output proves all-or-nothing effects;
independent readers see only committed publication and conflicting writers retain OCC.

**Checkpoint B:** FP-2–4 close the first query package. Run a grouped query/transaction
regression and affected Pulse tests before persistent type changes.

### FP-5 — Complete temporal values, not string-only constructors

Provide `date`, `time`, `localtime`, `datetime`, `localdatetime` and `duration`
constructors/properties and their agreed conversion, comparison and arithmetic
forms. Enumerate exact overloads from the frozen temporal cases and a reviewed
capability list, including documented operations not exercised by this TCK slice.

Recommended contract: represent date, local/zoned time and local/zoned datetime
distinctly; preserve precision/offset/zone information rather than silently folding
everything into the existing microsecond UTC `TIMESTAMP`. New types preserve
nanosecond precision where required by the frozen reference, or explicitly refuse
out-of-range values rather than rounding silently. Keep calendar months/days
separate from elapsed-time components in durations. Define leap dates, month ends,
negative durations, normalization, precision and range errors. Ambiguous or invalid
local times require a documented explicit resolution, not machine-dependent guessing.
Implicit clock/timezone behavior must be explicit; inject clocks for tests and
qualify timezone-data dependencies. Existing `TIMESTAMP` is an instant type, not
a legacy compatibility switch for the new local/zoned types.

Deliver expression values **and stored columns**: schema declarations, parameter
binding, result conversion, pure/accelerated codecs, comparison/index support,
WAL/recovery/verification, retained history, copy/backup and import/export. If a
particular index/type combination is unsupported, refuse it at definition time.

**Exit:** temporal matrix and independent calendar/round-trip tests pass; exact
values survive commit, restart, history and supported transfers. Older binaries
refuse new layouts before mutation; no precision loss or timezone drift is accepted.

### FP-6 — DECIMAL and persisted nested values

Recommended initial decimal profile: `DECIMAL(p,s)`, `1 <= p <= 38`,
`0 <= s <= p`, exact coefficient/scale representation. Freeze arithmetic result
precision, casts, aggregation, ordering/equality/hash, overflow and explicit
rounding rules before coding. Do not route exact values through DOUBLE or silently
inherit the host's mutable decimal context. Cross-type comparisons must retain
exactness and consistent index/grouping keys.

Complete typed `LIST<T>`, string-keyed `MAP<T>`, fixed-size `ARRAY<T,n>` and named
`STRUCT` fields, including element/field nullability, nesting, copying/ownership,
DDL/introspection, updates, equality and serialization. Exact syntax is proposed,
not an existing API. Reuse existing LIST/MAP encoding where sound; mutable nested
values must not invalidate cached encoding proofs or mutate a held snapshot.

Publish a type-support matrix covering primary keys, equality/ordered indexes,
query parameters/results, Arrow/Pandas/Polars/Parquet, CSV/JSONL/SQLite, CLI JSON,
logical transfer and history. New scalar types need coherent supported index
semantics; nested values need not be legal primary keys or acquire arbitrary
element indexes. Unsupported interchange types must fail before partial import,
not silently stringify or lose decimal scale. JSON needs explicit tagged forms
where ordinary numbers/strings cannot preserve the native type.

**Exit:** all declared stored types work across creation, updates, rollback,
restart, verification, history and supported copy paths. Test malformed/truncated
payloads, depth/size bounds, decimal edge cases and pure/NumPy equivalence.

**Checkpoint C:** FP-5–6 close together with one documented format/type migration
plan and installed-wheel old/new-reader tests. This is a quality checkpoint, not
permission to erase an incompatible database or continue with mixed unsafe binaries.

### FP-7 — Writing procedures within transaction authority

Extend registration with explicit read/write mode, typed signatures/output schema,
permissions, determinism/effect declarations and shared budgets. A writing callback
receives only a restricted transaction-scoped capability: it cannot commit, switch
stores, acquire a second independent writer, retain authority after return or bypass
the outer statement's rollback. Freeze `YIELD *`/named yield, unit procedures,
nesting and the new entity/value signature rules as explicit cases.

Check authorization/read-only mode before invocation. Validate every yielded value,
including unselected columns. Callback/iterator/final-output failures and close
errors must preserve the primary error and proven rollback. Never retry an entire
callback implicitly: host code may have nontransactional side effects. Document
that callbacks are trusted host code, not a sandbox; external network/filesystem
effects are neither intercepted nor rolled back by the database. Blocking host
code still cannot be forcibly preempted by cooperative query cancellation.

**Exit:** procedures can perform useful native graph writes through CALL/YIELD;
denied writes have zero effects; late failure rolls back all graph effects;
expired/foreign capabilities are rejected; concurrent writers/readers remain safe.

### FP-8 — Qualify the profile and update Pulse

Run the full frozen inventory and supplemental extension cases. Publish original
pass/fail/not-run counts and a separate profile result with every exclusion listed.
Every required-profile case must pass: zero unclassified cases, unexplained
`not_run`, in-profile failures or expected-failure waivers. Every retained model
divergence needs an explicit counterexample and reason, not a renamed pass.

Execute equivalent bounded scenarios on fixed competitor versions. Use Ladybug
0.20.3 for continuity; choose and record one available Neo4j CE binary/image version
and digest at FP-1 rather than inventing an installed version from rolling docs.
Do not depend on availability of a competitor to run Grafx's independent oracles.
If that comparative execution is unavailable, withhold the corresponding parity
claim rather than reporting it as passed. Results need exact values/types,
multiplicity, error/effect contracts and a published dialect adaptation log.

For Pulse, map changed templates and result/value consumers before each affected
package lands: cancellation/stale enumeration, query executor, logical transfer,
related context/key decisions, KG paging/edges/counts, schema/Settings, recovery,
and JSON/API/MCP serialization. Core uses neutral contracts/DTOs and explicit
capability unavailability where appropriate. Native value/identity/procedure
implementation stays in Community. Do not expose arbitrary writing procedures to
Pulse users merely because the engine now supports them.

Accept coordinated breaking changes without a legacy query mode, but provide a
precise upgrade order, backing artifacts and safe refusal for incompatible storage.
Use temporary data and installed paired wheels for regression. Final Pulse coverage
must include API and real-browser checks on an isolated instance, not just React
mocks. Do not consolidate remaining production specs or reset the user's graph as
test preparation. Global installation, restart and publication remain separate acts.

**Exit:** full Grafx regression, frozen profile, fault/concurrency/format tests,
affected Pulse regression and paired-wheel checks pass; all public docs and the
comparison state exactly what was delivered and what remains different.

## 4. Test cadence and safeguards against moving targets

- Each small change runs focused positive, negative and relevant prior-regression
  cases. Cache **unchanged passing test evidence**, not query outcomes or authority.
- Group broad regressions at checkpoints B/C and final delivery; do not run the
  entire 17k+ suite after every scalar/parser edit. New WAL/format or rollback risks
  require their focused fault tests immediately, not only at final delivery.
- FP-1 establishes versioned family selections so every failed case has an owner.
  An in-scope defect discovered later is corrected within its owner package.
  Unrelated enhancements go to the existing roadmap and do not block this round.
- Scope is not reduced after failures appear. Changing a frozen exclusion or
  adding a new architecture requirement requires an explicit recorded decision.
- No per-percentage performance gate. Record representative small/large query,
  path, write and Pulse API timings with fixed inputs and operation counts. Address
  material regressions/unbounded allocations; do not keep reopening the delivery
  to chase marginal gains. Establish any quantitative thresholds before the change.
- Planned format/capability changes require coordinated admission of all readers
  and writers. They never justify weakening COMMIT proof, WAL barriers or fences.

## 5. Documentation, packaging and claim checklist

Every package updates its owned capability examples and restrictions in
`QUERY_LANGUAGE.md`, `COMPOSABLE_QUERIES.md`, `CYPHER_COMPATIBILITY.md` and
`API_REFERENCE.md`; `CONFIGURATION.md` only gains justified knobs with defaults,
bounds, costs and failure behavior. Reuse existing resource limits where possible.
Types also update `CONTRACT.md`, compatibility/upgrade, history, indexes and
interop docs; procedures update extension permissions/lifecycle documentation.
Check paths/signatures/examples with the documentation validator and executable tests.

Update `FEATURE_COMPARISON.md` when capabilities actually pass, not at plan approval.
Update ROADMAP package status and link immutable acceptance reports/artifact hashes.
Do not promote the current 0.0.6 development source to a published release in prose.
Release/branch allocation is intentionally not decided by this plan.

Permitted final claim: **functional parity for the published local-first profile,
on the tested versions and cases**. Forbidden shortcuts: “full Cypher”, “all TCK
passes”, “same performance as Ladybug/Neo4j”, or “production certified” based only
on a green internal test count. If the frozen profile still has a failing required
capability, report the delivery incomplete rather than redefining parity.

## 6. Primary references

- [openCypher TCK 2024.3](https://github.com/opencypher/openCypher/tree/2024.3/tck):
  upstream scenarios define initial graph, query, expected result/error and effects;
  this is why FP-1 must execute more than parse/read-only expressions.
- [Neo4j CALL subqueries](https://neo4j.com/docs/cypher-manual/current/subqueries/call-subquery/):
  reference for the separately frozen returning/unit/import extension profile,
  not an assertion that current rolling Neo4j equals the 2024.3 TCK.
- [Ladybug data types](https://docs.ladybugdb.com/cypher/data-types/): comparative
  source for exact decimal and nested stored values, not proof of identical DDL,
  binary representation or transaction semantics.
