> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Single index adoption during read-only admission

## Existing cold-open residual

Pulse opens independent Grafx reader participants to avoid serializing UI reads
behind its writable handle. The real-board cold profile showed two calls to
assembly `sync_indexes(existing_only=True)` per reader, producing 366 exact-index
adoptions for 183 definitions. This redundant work scales with the number of
active indexes and is paid by every fresh reader handle.

The first call followed read-only consistency/catalog checks; the second followed
TransactionManager construction. That constructor reads catalog capability flags
and initializes transaction state, but does not consume index artifacts. Writable
recovery, in contrast, needs its existing index baseline before applying redo.

## Change and guarantees

Assembly separates catalog loading from index adoption. Read-only admission
still proves consistency from durable state/WAL, requires published stores and
loads the catalog before constructing TransactionManager. It adopts all declared
exact and vector indexes once at the **existing final** read-only admission point.

Writable recovery keeps its load-and-adopt callback unchanged in effect. Writable
artifact creation remains fenced. No index header/path/identity validation is
removed from the remaining adoption; active missing/torn generations still
refuse without repair. Legacy missing accelerators keep their existing behavior.
All later transaction synchronization remains in place, including foreign DDL.

This is neither a cache of authority nor an extension of any snapshot's lifetime.
No configuration/API, writer lease, read registration, WAL, OCC, corruption or
durability contract changes. The read-only data-plane capability remains intact.

## Quality evidence

- Initial startup/recovery slice: 15 passed in 9.75 s.
- Read-only storage/doors, power-loss and identity activation slice, including
  the new exact/vector admission tests: 57 passed in 31.56 s.
- Additional foreign-DDL synchronization test: 1 passed in 1.89 s.
- The first two slices overlap; their totals are not a unique-test count.
- Ruff and diff checks passed.

The new tests count one adoption per exact index for legacy and managed catalogs,
check its ordering after consistency, compare all non-control data file hashes,
inject missing/truncated active artifacts between constructor and final adoption,
exercise old/new snapshots with an independent writer, run vector search after
read-only reopen, and verify that later foreign DDL still enters the registry.

The first damage fixture selected a retained legacy-named file, which is not
managed-catalog authority. It was corrected to resolve the active generation
from its catalog definition. The final test asserts that generation identity;
an inactive file is not accepted as evidence of active-index corruption coverage.

## Short real-board paired measurement

All handles were explicitly read-only, page size 8192, generation descriptor
revalidation and 64 MiB buffer. Three alternating pairs opened the same live
board in a separate process. The comparison arm reconstructed the previous
extra pre-constructor adoption through the existing callback; both arms retained
the final admission. Timed interval: `connect`, not query or close. Queries after
each open counted the same 2961 nodes; all handles closed.

| Round | Previous double adoption (s) | Single adoption (s) |
| --- | ---: | ---: |
| 0 | 1.332 | 1.107 |
| 1 | 1.514 | 1.242 |
| 2 | 1.479 | 1.415 |
| Median | 1.479 | 1.242 |

Every double arm performed 366 adoptions; every single arm performed 183 over
the same 183 indexes. The ~16% median opening reduction is indicative for this
small sample, **not** a claimed 16% improvement to total Pulse KG loading.
No extended timing gate was added. Diagnostic:
`.grafx-tmp/read_only_adoption_bench.py` (local, not versioned).

## Deployment status

Source implementation and tests complete. The active Pulse PID 15796 remains
on the prior `6eabc14` runtime; this change is queued for the next accumulated
deployment, avoiding another immediate restart. No new consolidation or recovery
job was started. The reserved cognitive ledger remains unchanged:
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

The full cold UI load remains pending. See also
[relationship scan optimization](RELATIONSHIP_SCAN_VECTOR_FREE_0_0_4.md).

Deployment follow-up: Pulse PID 7896 loaded this change at the accumulated
Community `9c9ef1a` checkpoint. Graph, census and pagination succeeded; the
21 reserved specs remain untouched. See
`KG_CONCURRENT_CENSUS_FINDINGS_0_0_4.md` for current measurements and the still
unresolved cost of the full UI workload.
