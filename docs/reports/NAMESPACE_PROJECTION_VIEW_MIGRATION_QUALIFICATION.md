# Namespace consumer qualification: projections, views and migration ledgers

September 12, 2026, `feature/v0.0.6` source. Required consumer follow-up to
[independent namespaces](../specs/GRAPH_NAMESPACES_V1.md), not a new performance
gate or completion of the functional-parity plan.

## Corrected behavior

1. `project_graph` resolves the already separate node/relationship selections in
   their respective namespaces and carries kind through every paged scan. A
   homonymous relationship is not misclassified as a selected node. Endpoints,
   relationship weight columns, parallel edges, table-local identity, budgets and
   caller-owned reader snapshots retain their existing contracts.
2. `migrate_schema` resolves its ledger by node kind. A same-named edge neither
   blocks an absent node ledger nor supplies a valid ownership marker. Existing
   schema/checksum checks, detached dry-run and per-version transaction boundaries
   remain in force.
3. Logical views deduplicate dependencies by physical table ID, not name. The
   public sorted `(name, full_schema_hash)` tuple retains both entries with a
   repeated name; hashes include kind and ID. Schema mutation of either owner
   invalidates the view. The 64-dependency budget counts physical tables. Registry
   lookup/prepare is node-qualified, with its ownership validation unchanged.

No public signature or format/capability bit changed. The existing namespace bit
24 remains required for overlapping stores. Unique-name view definitions retain
their prior encoding; views recorded by earlier development code with an
incomplete dependency inventory refuse with `stale_dependency` and need explicit
validated replacement, not automatic read-side repair. Public DTO consumers must
not collapse dependency pairs into a name-keyed dictionary.

Consumer guides: [projections](../GRAPH_PROJECTIONS.md),
[views](../LOGICAL_VIEWS.md#independent-namespaces-006-development),
[migrations](../SCHEMA_MIGRATIONS.md). No new connection settings or weaker native
transaction, storage-finiteness, recovery or admission rules were introduced.

## Discriminating evidence

- Projection: shared spelling with different node/edge weight types; batch size
  one; parallel edges; explicit missing-endpoint refusal; edge budget exhaustion;
  pinned old reader against an independent handle's committed write; detached
  weighted shortest path and cold reopen.
- Migration: homonymous ledger relationships before and after the owned ledger,
  read-only dry-run without commits, idempotence, reopen, sibling preservation and
  refusal of a missing node ownership marker even with a same-named edge present.
- Views: independently calculated complete schema fingerprints; schema change to
  either physical kind; preserved old-reader data, explicit replacement and cold
  reopen; old incomplete but checksum-valid inventory refusal; both registry
  creation orders and a 65-physical/33-name detached-plan budget counterexample.
- Recovery: eight real subprocess cuts, pure/NumPy × view/migration × before
  COMMIT/after durable COMMIT before page application. Exact exit codes prove each
  cut ran. Reopen checks committed-versus-absent metadata, explicit retry,
  unchanged relationship siblings, verification and two subsequent read-only opens.

The first combined run retained **84 passes / three failures**. One new quota
fixture omitted required endpoint non-nullability. Two existing projection tests
tried to store NaN/Infinity, now correctly forbidden by the authorized FP policy.
The fixture was corrected; the old tests now require storage refusal/no edge
effects and separately inject invalid detached scan values to retain defensive
consumer-validation coverage. No production admission or verifier was weakened.

## Receipts

Paths relative to `.grafx-tmp`; counts overlap and must not be summed as unique
whole-repository coverage.

| Receipt | Result | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `namespace-consumers-qualified.xml` | 87 passed, zero failures/errors/skips | 42.274 | `7ab5e849f87f7de3aa384303b901c6533b884b356eee36c509a2c66f06c4a950` |
| `namespace-consumers-recovery.xml` | 8 passed, zero failures/errors/skips | 46.811 | `fd358ebf14cdbbc420dca56309a35f69c5ec8b3ae5cfe7e928e534feb7fe332d` |
| `namespace-consumers-interop-contracts.xml` | 2,356 passed, zero failures/errors/skips | 55.853 | `fa6731ef888c481c65f6ce1f9a71d593ead7e5bd995055c421899891521cd8a1` |
| `namespace-consumers-combined.xml` (retained first run) | 84 passed, 3 failed | 43.649 | `184502e8d86bd65d6444d560fc36d4099639e24fa08e2d58582103ade4959c9b` |

The 87-case selection contains the new view/projection/migration cases and all
existing projection, algorithm, acceleration, weight, migration and logical-view
tests. The recovery receipt is a separate native fault matrix, not mocked commits.
All successful runs reached terminal exit 0. The final interop/contract selection
includes the 12 projection/migration tests (two additional actual homonymous
NetworkX/Arrow exports), existing projection Polars/Arrow, prepared graph exchange,
HTML snapshot and projected-scan tests, plus the full public-surface and import-
boundary suites. This is not 2,356 new feature cases or a full repository run.
API generation, links/anchors, 39 configuration fields, 11 preserved source plans,
changed/new Python lint and whitespace checks pass.

## Remaining obligations

This does not qualify every public importer, arbitrary nested view query shape,
the installed latest wheel or Pulse. The source search in the paired Pulse trees
found no consumers of these Grafx projection/view/application-ledger APIs; similarly
named Pulse KG migration operations are a different interface. No Pulse source,
production database, installed runtime, commit, push or release was changed here.
Remaining namespace consumers, old logical-artifact importers, broader Pulse
qualification and FP-4–8 retain the obligations in the main plan.
