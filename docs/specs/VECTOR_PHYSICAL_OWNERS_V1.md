# Physical vector ownership — namespace acceptance repair

September 12, 2026, `feature/v0.0.6` development. This repairs a required blocker
in [independent namespaces](GRAPH_NAMESPACES_V1.md), not a new performance gate,
release, installation or change to Pulse Core.

## Implemented runtime contract

An embedding space describes compatible components; it does not identify a table.
Attachment maps, speculative claims, replacement epochs, durable local mappings
and maintenance timestamps now use `(space name, physical table ID)`. Each owner
retains its own index and cache. Rolling back one owner's DDL cannot restore,
remove or replace its sibling. Exact committed-definition validation still checks
table identity, positions and the complete space semantics.

Ordinary and grouped relationship DDL now attaches vector columns through the
same journaled attachment/observation path as node DDL. Native direct, materialized
and filtered vector searches carry the proven physical table. Optional candidate
proofs return unavailable for the wrong owner rather than borrowing its counts.
Space-level coverage/entries aggregate all physical owners; age reports the oldest
maintained owner. Metrics labels and connection settings are unchanged.

The supported public search is:

```python
with db.begin("read") as reader:
    result = db.search_vectors(
        reader, table=("node", "Document"), space="semantic",
        query=[1.0, 0.0], k=10, timeout_seconds=5,
    )
```

`table` accepts a unique physical name or `("node", name)` / `("rel", name)`.
The CLI exposes the same choice as `search vector --table NAME --table-kind node|rel`;
see [CLI qualification](../reports/CLI_VECTOR_OWNER_QUALIFICATION.md).
Omit it only for a uniquely attached space. Multiple owners without selection
raise `GrafxIndexError`, `reason="ambiguous_vector_owner"`; a wrong/missing owner
refuses, never falls back to another table. Table IDs and record IDs are distinct:
hits remain table-local and must not be merged across owner selections by record
ID alone. `db.vectors.indexes()` exposes every captured index; singular
`db.vectors.index(space)` refuses ambiguity instead of returning the first one.

Explicit selection is validated before participant coordination, resolved inside
the normal active-transaction page-access section, and observes that transaction's
snapshot. Dirty rows in another owner no longer block a clean selected table,
but read-your-own-vector-writes in the selected table still refuses. Invalid
vectors, closed/foreign transactions, missing owners and cancelled reads retain
their typed refusal paths. Cooperative controls and memory limits are unchanged.
Custom vector collaborators need the explicit `search_for_table` door (and
owner-aware controlled search for controlled requests); unsupported collaborators
refuse rather than silently ignoring the selection.

Hybrid search already names its node target. It now qualifies that vector owner,
allowing another table in the same space. The target must still have exactly one
column in that space. Ranking/filtering never obtains a graph-global top-k and
then drops other tables; retrieval happens against the selected owner.

## SET correction discovered by concurrent-reader qualification

Typed property SET previously checked/converter-validated a supplied list but
discarded the returned stored value, staging the original list into a vector
column. Commit correctly refused it. Both scalar property SET and typed map
`SET n += ...` / `SET n = ...` now retain the checked `VectorValue`. CREATE and
SET use the same vector-space, dimension, precision, finite-component and active-
space admission rules. NULL remains supported only where the column allows it.
This does not authorize stored NaN/Infinity or vector read-your-own-writes.

## Explicit remaining work before full acceptance

The original runtime increment did not change names or format bits. Its subsequent
[durable-name implementation](VECTOR_OWNER_NAMES_V1.md) adds capability 25 and
per-table identity naming when a new table would collide or exceed name bounds,
preserving existing unambiguous artifacts. See that contract for separate native
and installed-wheel qualification; the earlier 219-test receipt below predates
this format increment. Preexisting missing relationship-vector artifacts are not
silently created on read. The subsequent [component/legacy repair qualification](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
tests explicit selected-owner reconstruction, including real historical writers.

The subsequent [qualified maintenance increment](VECTOR_QUALIFIED_MAINTENANCE_V1.md)
adds public memory/rebuild selectors and detached physical IDs. Lower-level
component mutation doors now select `table_id` with independent staging and
reconciliation qualification; ambiguity refuses. Arbitrary merged
multi-table vector ranking and multiple vector columns in the same table/space
are not implemented by this selector. No on-disk rename, weaker OCC, global
single-writer mode, unlogged repair or production mutation is included.

## Qualification

`tests/api/test_vector_physical_owners.py` covers independent node/node and
node/relationship owners with pure/NumPy, exact/approximate regimes, native and
controlled public searches, two reopens, verification, sibling DDL rollback,
hybrid retrieval and clean-owner reads during a dirty sibling transaction.
Separate handles retain pinned old answers while a foreign writer commits and
fresh readers adopt the new selected-owner version.

`tests/txn/test_vector_owner_recovery.py` cuts real processes before COMMIT and
after durable COMMIT before page application, for node/relationship second owners
and pure/NumPy. Two reopens, checkpoint, search and verification require either
no second owner or its complete committed schema/data/index effects.

`tests/query/test_set_vector_storage.py` covers property/overlay/replacement SET
for both kinds and codecs, invalid dimension/NaN/Infinity refusal, committed
reopen, NULL removal and index verification. These are affected-area source tests,
not installed-Pulse, historical-binary or whole-profile acceptance.

### Receipts

All paths below are under `.grafx-tmp/`; counts overlap and are not additive.

| Receipt | Result | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `vector-owner-combined-final.xml` | **219 passed**, zero failures/errors/skips | 113.995 | `9deec94d487e3b421249b21b4d85cc4afcaa9839798d367b0311c932181ec1d9` |
| `vector-owner-recovery-qualified.xml` | 20 passed, including eight process cuts | 50.601 | `069e6137f790720c818c15a8323d2f3e30d9fba668cf3b1e9ae15e39adf481a1` |
| `vector-owner-set-qualified.xml` | 54 passed, including 12 new vector SET cases | 20.096 | `687d7fc9d42fadf8235bcfe427875bd3464deb43f7a4f3d79bb7db874be7da18` |

The final 16-file selection contains the three new files above plus
`test_vector_ddl_attach_atomicity.py`, `test_ddl_artifact_provenance.py`,
`test_vector_total_memory.py`, `test_vector_read_your_own_writes.py`,
`test_graph_namespaces_search.py`, `test_vector_access_path.py`,
`test_filtered_vector_access_path.py`, `test_metrics_and_events.py`,
`test_space_lifecycle.py`, `test_index_contract.py`, `test_hybrid_search.py`,
`test_set_property_maps.py` and `test_merge_actions.py`. Every run listed in the
table reached terminal exit 0. Documentation/API generation, all 39 configuration
fields, 11 preserved source plans, changed/new Python lint and whitespace pass.

Failed receipts remain retained: `vector-owner-first.xml` had 48 passes and one
wrong-owner optional-proof failure, corrected to return unavailable instead of
throwing. `vector-owner-regression-qualified.xml` had 152 passes and two obsolete
local expectations (the exact collaborator-door inventory and rejecting a second
space owner in explicitly targeted hybrid search). Those now assert the new
explicit door and preserved target-only results. The first recovery-focused and
next regression receipts exposed the real SET-conversion defect, fixed in both
typed assignment paths before the final combined run. An intervening collection
attempt (`vector-owner-regression.xml`) named a nonexistent hybrid test filename;
it is not passing coverage. No original TCK fixture, oracle or frozen ledger was
changed by this repair.
