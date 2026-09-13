# Vector component ownership and legacy artifact repair

September 12, 2026; `feature/v0.0.6` source qualification. This closes the
component-selection and genuinely missing-artifact portions of the existing
[namespace repair](../specs/VECTOR_PHYSICAL_OWNERS_V1.md). It does not certify the
full functional-parity plan, an installed current wheel or Pulse.

## Implementation and consumer contract

The five lower-level mutation/maintenance methods now select a physical owner
with keyword-only `table_id`: `stage_insert`, `stage_delete`, `commit`, `rollback`
and `reconcile`. The returned reconciliation value is typed as `ReconcileReport`.
An omitted selector is accepted only for a unique owner. Invalid, unknown or
ambiguous selectors refuse before effects; sibling staging remains independent
even inside one transaction. No catalog bit, storage layout or configuration was
added by this increment.

The existing assembly path already skips absent derived indexes on read-only
opening and attaches them **stale** on writable opening. The newly qualified
public rebuild selector lets callers repair exactly that owner. No new automatic
repair path or weakening of recovery was necessary. Graph rows survive; vector
search for the missing/stale owner refuses until explicit rebuild. Follow
[usage and limitations](../specs/VECTOR_QUALIFIED_MAINTENANCE_V1.md#missing-derived-artifacts),
including the existing post-rebuild live-handle coverage fence.

## Discriminating tests

`tests/vector/test_component_owner_selection.py` adds 22 cases: two owners with
the same record ID but different physical references/vectors; independent staging,
commit and rollback; selected deletion/reconciliation; old-snapshot visibility;
invalid/ambiguous refusal with unchanged staging; and retired-space cleanup.

`tests/api/test_missing_vector_repair.py` adds eight cases across pure/NumPy,
catalog v1/v2 and missing node/relationship indexes. Only a test-created derived
file is removed. Read-only opening preserves every non-control file hash;
writable opening marks the new placeholder stale; selected rebuild, verification
and two cold reopens restore correct owner-specific search results. Control files
are excluded only because successful reader registration legitimately changes
participant coordination. Heap/catalog/WAL/index files are not excluded.

The initial component receipt retained two failures caused by a standalone
fixture that applied REMOVE without publishing its logged reconciliation horizon.
The fixture now replays the committed REMOVE as native checkpoint/replay does;
the selected horizon advances and its sibling's does not. No production horizon
was inflated and no verifier check was relaxed. This component exercise is not
itself a native transaction/recovery qualification.

## Real historical writers and process interruption

`tools/qualify_legacy_vector_repair.py` creates fresh stores with actual installed
archived 0.0.5 and early 0.0.6 packages. Those writers persist a relationship
vector but leave its index absent; no file is manually deleted for this matrix.
The current workers explicitly import the supplied `src` under isolated Python
execution, asserting the import origin. This is **old installed writer to current
source** evidence, not an installed-current-wheel result.

The matrix combines two old binaries, pure/accelerated adapters, and ordinary
repair / process exit before COMMIT / exit after durable COMMIT before page
application. Exit codes 71 and 73 prove the requested interruption hook ran.
Recovery must preserve stale refusal before explicit retry, leave the node
sibling healthy, verify the resulting store and support two read-only reopens
with correct node/edge cosine scores and unchanged non-control data hashes.

Run with existing isolated interpreters containing the declared dependencies:

```powershell
python tools/qualify_legacy_vector_repair.py --source src `
  --old-python <old005-python.exe> --old-python <old006-python.exe> `
  --current-python <current-dependencies-python.exe> --output <fresh-directory>
```

The tool rejects an existing output directory and neither installs dependencies
nor deletes data. Reports record source-file hashes, import origins and every
worker's output/expected and actual exit status. The first two retained matrices
each had nine passes and three old-0.0.5 accelerated seed failures: missing native
CRC provider, then missing NumPy. The private environment was completed with
`google-crc32c==1.8.0` and `numpy==2.5.3`; its old Grafx package was unchanged.
Those failures are not relabeled successful recovery tests.

## Receipts

Paths are relative to `.grafx-tmp`; overlapping counts are not additive.

| Receipt | Result | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `vector-component-owners-qualified.xml` | 96 passed, zero failures/errors/skips | 9.718 | `e5f61f70eec99abaae7c62a6ab048f0894ff98a94edf56eff8152375e4113682` |
| `vector-missing-artifact-source.xml` | 8 passed, zero failures/errors/skips | 12.658 | `79f6278909ecd6b70417c862f7508fdbe58db3acc3d7262c07e58f19498bc3e1` |
| `vector-owners-repair-final.xml` | 609 passed, zero failures/errors/skips | 197.306 | `645295f4c6e6f0857059c64523d31ee5040b5c4ae24e20c3b35dad78979456b7` |

The 609-case selection includes all `tests/vector`, the missing-artifact and
qualified-maintenance tests, existing rebuild tests, physical-owner and durable-name
API tests and native owner-recovery cuts. It reached terminal exit 0. API generation,
documentation links/anchors, all 39 configuration fields, 11 preserved plans,
changed/new Python lint and whitespace checks also pass.

The completed historical-writer matrix reached terminal exit 0: **12/12 passed**,
including eight actual process interruptions. Receipt
`.grafx-tmp/legacy-vector-repair-complete/report.json`, SHA-256
`75bf50d50f2dc250f67692fbfe0d6de4299e14ba244f8d26a7e7db8da9e5e050`.
All 239 recorded source Python hashes matched the current tree after completion.
This matrix is separate from the 609-case regression and uses the explicit-current-
source mode described above. Retained failed predecessors are
`legacy-vector-repair-qualified/report.json` and `legacy-vector-repair-final/report.json`.

## Remaining acceptance

This closes the stated vector repair scope, not all namespace/parity acceptance.
Remaining consumers/artifact importers, broader Pulse execution, current installed
package qualification and the frozen full-profile requirements retain their
existing obligations. No original TCK fixture/oracle or frozen ledger changed;
no production database, global installation, commit, push or release was touched.
