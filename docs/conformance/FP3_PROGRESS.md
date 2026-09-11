# FP-3 working evidence: composed polymorphic reads

[Roadmap](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Full acceptance requirements](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-3--entity-identity-polymorphism-and-paths) ·
[Query contract](../COMPOSABLE_QUERIES.md#polymorphic-node-read-composition)

September 11, 2026, `feature/v0.0.6` development. This is an implementation
increment, **not FP-3 acceptance**, complete Cypher parity or a release.

## Implemented scope

Standalone label-free node reads now compose with multiple MATCH clauses and
patterns, UNWIND, inline property maps, WITH aliases/star, OPTIONAL MATCH and
returning read subqueries. Anonymous node patterns preserve multiplicity without
publishing a variable. The planner reuses the existing AllNodesScan input operator
and its snapshot/private-overlay behavior; no new transaction, WAL or storage
format was introduced.

The former statement-wide one-shape guard is narrowed to its remaining dynamic
write restriction. Detection follows clause order and discarded/reintroduced
names, so dropping a typed binding cannot bypass that restriction. A rematch of
an already-bound polymorphic node is a binding/null/table predicate, not another
scan. A typed label on that rematch filters the binding's table. Inline maps lower
to ordinary equality terms and validate referenced property families even when
the source contains no explicit Property expression. Missing properties return
NULL; incompatible declared types remain a planning refusal.

Polymorphic binding metadata crosses WITH aliases and explicit read-subquery
imports/exports. Internal bindings retain the physical table/record identity;
same user primary keys in different tables do not collapse in DISTINCT/grouping.
This does **not** implement the new detached public database/table/incarnation
identity representation required by FP-3.

## Independent tests and original scenarios

| Local receipt | Coverage | Result |
|---|---|---|
| `fp3-composed-polymorphic-regression.xml` | New composed-read matrix, existing polymorphic/planner tests, WITH star and public cursors | 179 passed; zero failures/errors/skips; 18.236 s |
| `fp3-composed-read-boundaries.xml` | Expanded identity/rematch tests, correlated/root optionals, optional aggregation, UNION, deferred projection, public query boundaries and write conflicts | 310 passed; zero failures/errors/skips; 68.796 s |
| `fp3-composed-conversions.json` | Original 47-case `expressions/typeConversion/` family, verified frozen ledger | 21 original passes, four fixture-adapted passes, zero failures, 22 selected fixture blockers; 3,850 cases outside the selection |

The JSON retains all 3,897 original cases at pinned upstream revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`. SHA-256:
`b19926aa4459c7338a19ab1c2b11a77a53ba645beddb350f46af276abc8f7b65`.
Reproduce using the stateful verified-ledger runner with
`--feature-prefix expressions/typeConversion/ --infer-fixture-schema`.
The three conversion queries blocked first by WITH star and then by the
polymorphic multi-MATCH guard now pass **with explicit inferred fixture schema**.
Their query text and expected values are unchanged. They are not relabeled as
unadapted upstream passes; 22 unlabeled/multilabel fixture blockers remain visible.

Tests independently check:

- Exact cross-product multiplicities, null extension of a complete multi-pattern
  optional clause, inline missing keys, compatible types and before-row refusal
  of incompatible columns.
- Repeated bound-node patterns/aliases/imports without a second scan; discarded
  names receive fresh scope identities. An optional NULL cannot rebind as a scan.
- Same-ID nodes from different tables remain distinct; group counts are exact.
- Typed anchor index access remains present in a composed plan. New polymorphic
  variables still use an all-node scan; no cross-table index speedup is claimed.
- The writer's private insert/update/delete overlay, an independent old read
  transaction through commit, and the new post-commit snapshot return exact rows.
- Cursor early close and a globally bounded cross product release transactions.
- Existing caller-supplied AST/analysis and dynamic-write refusal tests remain
  enforced. Older tests that intentionally refused the newly supported read
  shapes now assert supported planning/results; write refusals are retained.

The initial focused run had one test harness import error for a nonexistent
control API; the final test uses the existing `connect(max_intermediate_rows=3)`
contract and verifies the expected budget failure and transaction cleanup.

## Internal entity identity foundation

`domain/query/entity_identity.py` adds immutable, capability-free identity and
provenance values. This is an internal foundation only: it is not root-exported
and is not yet wired into native query results or cursors. The public result
contract has not changed in this checkpoint.

Identity distinguishes database UUID, table ID, entity kind and record
incarnation. Native lifecycle tests establish that updates retain a RecordId,
while deleting and recreating the same primary key allocates a different one,
including within one transaction and across reopen. Independent databases with
the same local RecordId remain distinct. Snapshot/version LSN and observed schema
version are separate provenance, not identity. JSON metadata uses decimal strings
for wide identifiers, avoiding JavaScript integer precision loss.

The foundation relies on the current append-only table catalog and monotonic
record allocator. Future table-ID reuse requires an explicit incarnation policy.
Provisional identities have a separate opaque nonce namespace; allocating those
nonces in the owning transaction and integrating detached immutable properties
remain pending. No transaction, pending-row reference or write authority is
exposed. Tests reject identity DTOs as query parameters before write effects and
reject provenance claiming a committed version newer than its snapshot.

Local receipt `fp3-identity-storage-regression.xml`: **324 passed**, zero
failures/errors/skips, 7.105 s; covers the identity foundation, record-ID wiring,
catalog and heap-store regressions. This does not constitute FP-3 acceptance.

The combined pre-push checkpoint `fp2-fp3-push-checkpoint.xml` reports **395
passed**, zero failures/errors/skips, 79.731 s. It reruns the changed numeric,
range, membership, WITH star, composed polymorphic, identity, node-seek, planner
and TCK error-mapping tests together. Documentation validation, changed-file
Ruff checks and whitespace checks also pass. This is targeted regression, not a
new full-repository or Pulse acceptance run.

## Remaining mandatory FP-3 work

- Public detached node/relationship/path values with database, table/schema,
  kind and incarnation identity; provenance, immutable materialization,
  equality/hash/JSON and stale/foreign mutation-handle refusal.
- Entity UNION, nested/entity-valued results and subsequent clauses using that
  public contract; broader incompatible/missing-property and schema combinations.
- Relationship-type alternatives and general named variable-length trails,
  directions, zero length/cycles, uniqueness, bounds and cancellation.
- Cross-table index access when compatible keys can anchor new polymorphic
  bindings, without skipping possible results or silently truncating scans.
- All required original/supplemental scenarios, package regression and affected
  isolated Pulse consumer checks at checkpoint B and final FP-8.

No production Pulse data, installation, Core adapter boundary or published
version was changed. The full goal and frozen case ledger remain unchanged.
