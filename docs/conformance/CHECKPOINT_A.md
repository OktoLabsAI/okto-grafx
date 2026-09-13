# FP-1 checkpoint A: frozen input matrix and initial blockers

This checkpoint freezes the work before FP-2. It is **not** functional-parity
acceptance, a complete TCK pass, or a reduction of FP-2–FP-8.

## Source-bound profile decision FP-A-20260911

The approved plan explicitly excludes schema-free storage/arbitrary multiple labels
and nonfinite arithmetic. The source review applies those boundaries to **427**
cases with concrete counterexamples: 414 require new unlabeled nodes, 22 require
multiple stored labels, and three expect NaN results (categories overlap).
These cases remain in the original 3,897-case upstream inventory and must never
be counted as passes merely because they are outside the typed local-first profile.

Examples: `clauses/create/Create1.feature#0001` requires `CREATE ()` without a
declared node type; `#0005` requires a node with `A:B:C:D`; the two
`clauses/return-orderby/ReturnOrderBy1.feature#0011`/`#0012` cases explicitly expect
NaN. The ledger binds each reviewed step/counterexample to the unchanged source
checksum. Source ASTs and expected literals establish these classifications,
**not** Grafx execution failures. Quoted strings resembling patterns and invalid
operations in negative scenarios do not trigger operation-based exclusion.

The other **3,470 cases remain required**. Missing functions, unsupported grammar,
unit/writing procedures, precise errors and difficult fixture admission are not
exclusions. In particular named-tree-2 endpoint families remain required work, not
automatically a model divergence. Any newly identified architectural exclusion
requires an explicit recorded decision; this checkpoint is not permission to
change the profile after seeing failures.

The TCK's schema-free fixtures exclude some upstream graph-scalar scenarios, not
the underlying feature requirements. The separate
[22 extension/value/consumer contracts](EXTENSION_SCENARIOS_V1.json) explicitly retain
typed graph scalars, entity identity/UNION, paths, modern subqueries, all stored
types, procedure authority and Pulse integration. They are requirements, not
executed tests. FP-8 requires executable evidence for each one.

## Coverage and initial blockers

All **3,897 cases, 220 features and 15 step families** are assigned. Primary package
ownership is the upstream capability family (see the ledger); dependencies remain
cross-package. Temporal expressions nested in list/quantifier families require
FP-5 even where the upstream family's initial owner is FP-2. A package cannot be
claimed fully accepted while one of its required dependencies remains failing.

The tested runner handles sequential fixture/operation/control steps, ordered and
bag results, primitive/container/graph literals, explicit error expectations,
independent stored-row effect observations and durable reopen. It records typed
schema adaptations separately from upstream passes. The initial focused regression
passed 179 tests with zero failures/errors/skips; scope-verifier tests are added
at this checkpoint. Reference binaries are pinned in
[REFERENCE_VERSIONS_V1.json](REFERENCE_VERSIONS_V1.json), with actual comparisons
still pending FP-8.

| Initial blocker | Owner | Required disposition |
|---|---|---|
| Numeric syntax, faithful unaliased column names, chained/postfix expressions, rand | FP-2 | Native implementation and original expected results |
| Graph result normalization/identity, named paths, compatible polymorphic tables and endpoint-family fixtures | FP-3 | Exact typed results/multiplicity and documented source-preserving fixture admission |
| Missing query composition in remaining setup/scenario queries, unit/writing subqueries | FP-4 | Shared transaction/statement rollback, no inner commits |
| Temporal overloads/precision and value-aware fixture admission | FP-5 | Independent temporal oracles plus native persistence |
| Nested stored values/DECIMAL, typed fixture admission and transfer | FP-6 | Exact storage/interop and coordinated format safety |
| Unit/NUMBER signatures, modern YIELD shapes and writing authority | FP-7 | Useful native procedures and fail-closed lifetime/rollback tests |
| Unmapped native negative details/phases | Owning language package; FP-8 audits all | Prove actual error phase; do not map arbitrary refusal to expected runtime failure |
| Remaining original not-run cases and modern supplemental scenarios | FP-8, with owners above | No required failure/unexplained not-run can survive final acceptance |

This is a frozen list of blockers, not a demand to implement later-package features
inside the FP-1 harness. No new model, transaction or production operation is
introduced by the checkpoint. The full severe regressions remain at checkpoints
B/C and final FP-8, with focused correctness/fault tests during implementation.
