# Qualified vector diagnostics and rebuild

September 12, 2026, 0.0.6 development. This closes the public diagnostic/rebuild
selection portion of [physical vector ownership](VECTOR_PHYSICAL_OWNERS_V1.md).
It adds no format bit or runtime configuration and does not certify the full
functional-parity plan or an installed Pulse.

## Public contract

`VectorIndexView.table_id` is a required integer field in the detached DTO. Its
identity comes from the catalog-validated index definition, not filename parsing.
`VectorEngineView.index(space, *, table_id=None)` selects that owner; an omitted
identity refuses if the space has multiple owners. Invalid IDs (including booleans),
unknown owners and ambiguity refuse. Captured views work after database close.

`Database.vector_memory_usage(space, *, table=None)`,
`Database.rebuild_vector_index(space, *, table=None)` and its `Maintenance` facade
accept the same `TableSelector` as search: a unique name, or `("node", name)` /
`("rel", name)`. Names resolve to physical IDs; homonymous bare names refuse. The
table must own an index for that space. Invalid, absent and ambiguous selections
refuse before the durable rebuild claim.

```python
owner = db.catalog.catalog.table("R", kind="rel").table_id
observation = db.vectors.index("s", table_id=owner)
memory = db.vector_memory_usage("s", table=("rel", "R"))
receipt = db.maintenance.rebuild_vector_index("s", table=("rel", "R"))
assert receipt.table_id == owner
```

Memory observation does not build a graph or prove freshness. Rebuild still opens
its own transaction and preserves checkpoint/claim, table-partition OCC fencing,
deferred stale clear and post-COMMIT failure handling. Selected identity is checked
before claim and before stale clear; a sibling cannot supply the target's proof.

## Coverage and sibling behavior

The existing live-handle rebuild fence is **unchanged**: the receipt describes
coverage at the scan snapshot, not invented coverage at the later COMMIT. A newer
query on that handle may raise retryable `index_view_unavailable`. A cold reopen
validates the durable checkpoint proof; an ordinary later indexed write can
advance the live proof. This limitation must not be hidden by inflating an LSN.
The sibling remains independently queryable.

Checkpoint may advance a sibling's coverage header. Tests check its identity,
artifact nonce, flags and other metadata (except monotonic coverage), plus
byte-identical non-header pages. Requiring the whole file to remain identical
would incorrectly prohibit normal checkpoint coverage. No sibling entries reset.

## Tests and remaining scope

`tests/api/test_vector_qualified_maintenance.py` covers both physical kinds and
codecs, exact/approximate reads, both facades, independent memory observation,
immutable detached IDs, refusal before claim, sibling bytes/identity, pre-COMMIT
failure, cold stale preservation and successful retry/reopen. The 16-case suite
passes in 17.655 s; receipt `.grafx-tmp/vector-qualified-maintenance-api-final.xml`,
SHA-256 `78df9a856d3d078e13cf9e22779df34c2ae3160d23ec6770913215865201fde2`.
Existing rebuild fault-injection tests retain stale/unknown-coverage assertions;
synthetic DTOs now carry the physical ID and selector-aware mocks.

The final six-file affected regression passed **95/95**, zero failures/errors/skips
in 59.886 s: new qualified maintenance, existing rebuild, memory budget, maintenance
facade, durable owner names and physical owner search. Receipt
`.grafx-tmp/vector-qualified-maintenance-combined-final.xml`, SHA-256
`4543c097834510187ed9235f49b6ec69815a10127e7051e51e0cb13880aa8039`.
Counts overlap with the 16-case suite and are not additive.

Initial failed receipts remain retained: new test expectations were corrected to
respect the established live-handle coverage fence and checkpoint header changes;
a fixture incorrectly called the `header` property as a method. No production
coverage check was relaxed. The installed-wheel bit-25 evidence predates this
subsequent API-only increment; it is not an installed qualification of these doors.

The subsequent [component and legacy repair qualification](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
covers owner-selected component mutations and genuinely absent old-wheel artifacts.
Remaining: broader consumers/Pulse and full package acceptance. A source search found
no `VectorIndexView` constructors or calls to these diagnostic/rebuild doors in
the paired Pulse Community/Core source trees; neither tree was edited/installed.

## Missing derived artifacts

An old store may contain valid vector-bearing graph rows but lack the relationship
vector index. Read-only opening does not create that file; ordinary graph reads
remain available, while vector search for the missing owner refuses. Writable
opening can create the missing derived file **marked stale**, not declare it usable.
Reconstruct only the selected owner's index with the explicit rebuild API above.
The heap is the source; this is not a repair for corrupt heap pages or a permission
to discard uncommitted WAL. Observe the existing live-handle fence described above.

## Engine component contract

For engine integrators, `VectorEngine.stage_insert`, `stage_delete`, `commit`,
`rollback` and `reconcile` now accept keyword-only `table_id=None`. Select the
catalog-validated positive physical ID when a space is shared. Omission is valid
only for a unique owner; invalid, unknown or ambiguous identities refuse before
staging/application. Commit and rollback affect only the selected owner's staged
entries, including when both owners participate in the same transaction.

`reconcile(..., txn=None, table_id=...)` measures without removing entries. With
a transaction it stages horizon-bounded removals; native checkpoint/replay still
owns durable reconciliation-watermark publication. These are engine components,
not a substitute for the public transaction/rebuild lifecycle. Applications must
not call them to bypass WAL, OCC, pinned readers or committed-page publication.
Space-level metrics remain aggregated; table-local record IDs are not global IDs.

## Broader quality finding and subsequent correction

The expanded run also included `tests/foundation/test_public_surface.py`:
**2,072 cases, 1,997 passed, 75 failed**, zero errors. All 75 failures are static
contract assertions in other modules: 21 missing function annotations, 23 missing
postponed-annotation declarations, 31 missing explicit export declarations.
These are case counts, not necessarily distinct symbols/files. Receipt
`.grafx-tmp/vector-qualified-maintenance-regression.xml`, SHA-256
`3657150dcf1137b555c99f903e38fc7808f692ed2ee1a6ffd4d77761163111ba`.
The subsequent [declaration/boundary correction](../reports/FP_STATIC_CONTRACT_QUALIFICATION.md)
resolves these failures with new passing receipts. This failed run remains
historical evidence, not relabeled success. Neither increment certifies the full
functional-parity package.
