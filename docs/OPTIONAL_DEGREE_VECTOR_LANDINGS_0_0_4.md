# Optional degree queries: avoid unused landing vectors

## Existing workload and decision

The Pulse orphan audit uses one query per label with outgoing and incoming
OPTIONAL MATCH, separated by aggregate WITH. A stack-only 40-second/40-Hz
capture of Pulse PID 24152 on 2026-09-07 collected 715 samples, zero errors:
326 Health, 75 graph-edge loading, 63 census and 251 other. Within Health,
147 samples traversed orphan-integrity scanning and 118 traversed
`_connected_node_ids`. These inclusive counts are not additive CPU/wall-time
percentages. The diagnostic browser collector mistakenly called the fetch
response's `status` property as a method, so its endpoint wall times were lost;
they are not claimed as performance evidence. Server logs confirmed successful
GETs; no requests were replayed to repair the observer result.

The native `_traverse_any` path retained complete vector-bearing endpoint rows
even when an anonymous OPTIONAL target was never consumed. This is the same
already selected unused-materialization opportunity in the actual Health read
path, not a new authority-cache, query-rewriting or health-suppression initiative.

## Closed proof and unchanged guarantees

`_unused_optional_landings` accepts only a linear, known read pipeline over a
single typed NodeScan/IndexSeek anchor. Every optional hop must start at that
same anchor, and its distinct target must be absent from every expression,
WITH/output alias and other hop source. Full-target/property access, forwarding,
unknown operators, paths, UNION and writes decline. Unsupported query syntax
retains its existing parser refusal.

The selected `TraverseAnyRelationship` uses the existing metered owner landing
view with `materialize_vectors=False`. Exact built-in heap/index identities and
canonical full-read hooks are required; specialized collaborators retain their
previous path. Anchor rows, edge rows, grouping and returned values are unchanged.
Omitted vectors are still decoded structurally and validated, including their
space/shape. This avoids retaining their components; it does not skip bytes or
trust a stored checksum instead of validation.

The frontier, edge order/multiplicity, optional null extension, both directions,
snapshot visibility, owner overlay and row/traversal admission remain unchanged.
There is no replacement of the physical pre/post certificates, OCC checks, WAL,
writer fencing or recovery policy. No authority is retained across operations.
The existing landing budget and full-value fallback continue to apply.

## Bounded read-only comparison

Used the already isolated real-board restore, never a new consolidation. The
Entity degree query returned 953 rows in each arm, with identical ordered digest
`abceb2f2a581dc9870d3230f6de807a03a9d50ce85f44eccdc035ec17d1499c1`.

| Execution order | Full vectors (s) | Unused vectors omitted (s) |
|---|---:|---:|
| full, candidate | 2.5424 | 2.2167 |
| candidate, full | 2.4001 | 2.3009 |

This is a modest favorable sample, not a sustained throughput or whole-UI claim.
No prolonged marginal timing gate was opened. The deterministic benefit is
smaller retained landing payloads with the same rows, columns and scanned-row
counts. A focused test compares the actual owner-budget charges in separate
read transactions and verifies every charge is returned on exit.

## Quality and deployment boundary

The affected optional/aggregate/vector-free/batched-count slice passed 107 tests
in 57.16 s. A disjoint related optional/spill/lazy-landing slice passed 87 tests
in 11.10 s. The new tests cover unused endpoints, full-value consumers, both
directions, parallel edges, isolated nodes, independent writer snapshots, owner
inserts, custom hooks, omitted-vector corruption and identical typed budget
refusal/cleanup. Fixture corrections checkpointed before read-only admission and
classified unsupported grammar as parser refusal; no runtime guarantee was
relaxed to pass them.

The final 14-test focused slice (7.29 s) additionally proves lower metered
retention and unchanged wildcard/cross-anchor parser refusals. It overlaps the
107-test slice and is not an additional disjoint total. Ruff and whitespace
checks passed.

Pulse PID 24152 has not loaded this native change. Deployment is reserved for
the next accumulated restart, not claimed hot-loaded or globally installed.
No Core/Community code change, spec consolidation, redrive, rebuild or data reset
was performed. The 21-spec cognitive ledger remains byte-identical, SHA256
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
Cold UI load and full write latency remain open; this closes only the selected
unused-vector work inside the existing optional degree query.

Deployment follow-up: accumulated restart loaded this change and `0fee5b4` into
Pulse 0.3.3 PID 36660. The previous participant terminated before replacement.
UI pagination reached 1000 / 2779 nodes with zero failed edge tables, and the
reserved cognitive ledger remained byte-identical. See the runtime evidence in
`VERIFICATION_VERSION_SUFFIXES_0_0_4.md`; this smoke is not an isolated degree-query
speedup measurement and does not close first-load latency.
