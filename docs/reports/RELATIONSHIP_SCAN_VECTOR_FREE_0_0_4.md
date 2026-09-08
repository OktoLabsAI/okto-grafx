> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Scalar relationship-scan endpoints without vector materialization

## Why this path

The Pulse KG census already sends its 70 physical relationship counts in one
read transaction. A routed, read-only profile on the real board showed the
residual in native `RelationshipScan`: 8,850 endpoint accesses, 3,102 identity
resolutions and full endpoint vectors decoded for `RETURN count(r)`. The count
batch took 8.44/8.94 seconds with cProfile attached; these are instrumented
diagnostic times, not UI or uninstrumented throughput figures.

The existing scalar traversal optimization covered `TraverseRelationship`, not
edge-first `RelationshipScan`. The extension belongs in Grafx, with no query
rewrite or backend-specific policy added to Pulse Core.

## Implementation and safety boundary

`engine/query_engine.py::_closed_vector_free_landings` now also accepts an exact
RelationshipScan over SingleRow when the complete closed consumer pipeline
cannot inspect a vector from either endpoint. Filters, grouping, ordering and
result expressions participate in the proof. Bare endpoint entities, vector
properties, aliased endpoint collisions, unknown/nonlinear operators and paths
outside that proof keep the canonical full decode. No-vector schemas do not
take the new path.

`_relationship_scan` uses the existing validated identity-landing capability
for both endpoint variables under the same exact native-component/full-hook
guards as scalar traversals. It still scans the same relationships in the same
order, resolves both endpoints, applies owner changes and snapshot visibility,
and charges the same traversal limits. The decoder validates stored vector
bodies without constructing their component tuples. It does not skip storage,
schema or corruption checks.

Partial values retain the existing guarded sentinel and separate bounded memo
key, so they cannot answer a later full-entity/vector read. Owner updates and an
independent writer still follow their existing visibility rules. Budget
admission/eviction and transaction cleanup are unchanged. Missing optional
capabilities or specialized validation hooks use the full path.

No cache/bundle of authority, new setting, snapshot reuse across transactions,
WAL/OCC change, weaker corruption contract or multi-reader/writer restriction is
introduced.

## Focused evidence

- Initial traversal/relationship-scan slice: 48 passed in 37.61 s.
- Final vector-free, landing decoder, owner-memo and query-budget slice:
  64 passed in 53.60 s. The slices overlap; do not sum them as unique tests.
- Tests compare scalar/count results to the full-decode oracle, prove both
  vector-bearing endpoints are covered, reject vector/entity exposure, preserve
  custom hooks and optional-capability fallback, exercise partial then full
  reads and owner updates, verify old-reader/new-writer snapshots and exact
  corruption errors, and prove lower retained memory with settlement back to
  zero under the unchanged budget.
- A two-endpoint filter fixture initially planned a traversal rather than the
  intended relationship scan. The final scan cases assert the physical operator
  explicitly; existing traversal tests remain enabled.
- Ruff and whitespace checks pass.

Three short alternating count-batch pairs through the actual Community routed
composition against the live read-only graph returned the same ordered counts
for all 70 tables, totaling 4,425 relationships each time. Count fingerprint:
`5a61483b71862bb1d99827ba5cbfd619ebb87946db94a1ec23f08f682a7aaa6f`.

| Round | Full decode (s) | Candidate (s) |
| --- | ---: | ---: |
| 0 | 3.866 | 3.628 |
| 1 | 2.722 | 2.382 |
| 2 | 2.837 | 2.698 |

The sample indicates only a small wall-time reduction (medians 2.837/2.698 s),
not a material fix for total cold KG load. No further timing gate is warranted
for this residual. The structural reduction and correctness tests support the
change; admission and other live-process read costs remain separate pending
work. These diagnostics are not full HTTP or UI A/B tests and neither create nor
consolidate specs. The 21 reserved specs remain untouched.

## Live Pulse verification

The previous Pulse PID 22748 closed its global and board graphs with zero
failures (95 ms). New PID 15796 / Pulse 0.3.3 loaded this Grafx implementation
from source on API 8100 / MCP 8101, plus the Community `270d84a` cursor boundary.

Browser evidence after restart: first graph page HTTP 200 in 30.930 s, one census
HTTP 200 in 43.558 s; 500 visible nodes and total 2779. Pagination reached 1000
nodes in 2.586 s and then 1500 nodes with a 500-unique-ID page, 753 edges, zero
failed edge tables (65 scanned, 5 pruned by page types), HTTP 200 in 1.591 s
including the automation click/response observation. The first pagination
response body was not captured because of a browser-tool listener error; its
resource timing and rendered result were independently inspected. A subsequent
stale-button selector timeout did not issue another request; the final action
used the observed `Load more (1000+)` control.

The cold measurements again do **not** demonstrate an improvement in overall
KG loading. This is a working native optimization and live compatibility check,
not closure of the cold-admission/census performance issue.

Cognitive endpoint remained pending 21 / in progress 0 / consolidated 19, and
ledger SHA256 stayed
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
No reserved benchmark input was consumed.
