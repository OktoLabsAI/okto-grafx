# Vector-free landing materialization — 0.0.4

## Scope and evidence

This is a native allocation optimization on the already selected Pulse adjacency
and Global digest-link paths, not a new query feature or a weaker reader policy.
The September 7 live Global profile found 25 of 205 samples at the vector decoder
leaf while scalar link/upsert work ran. The existing planner already mirrors a
typed single-hop query toward its seekable endpoint; reversing Community's
incoming query text again would not remove that work.

`_closed_vector_free_landings` proves a closed scalar read over one typed,
directed, single-hop traversal. It permits filters, projection, scalar aggregate
inputs, ordering, DISTINCT and row windows. Every relevant expression is walked.
Reading any vector property of the destination, passing the whole destination
to a function, returning that entity, WITH, optional/variable-length/projected
paths, an already-bound target, writes and unknown operators decline the proof.
`label(destination)` needs only table identity. Source rows remain fully decoded.

The same traversal visits exactly the same nodes and relationship rows in the
same order, with the same multiplicity, frontier and budgets. No eager cross-node
batching is introduced. Pulse Core requires no Grafx-specific changes.

## Validation and cache containment

Qualified landings use the existing record-identity-only
`validated_identity_landings` capability: all index certificates, companion heap
checks, visibility, vector tags/dimensions/spaces/body validation and corruption
refusals remain in place. Only allocation of vector component objects is omitted.
Missing capability takes the full decoder. Specialized heap/full-index readers
and a specialized full-version decoder keep their original full-read hooks.

The identity decoder's private vector proof is converted to the existing guarded
unmaterialized-column marker before becoming an internal row. An accidentally
unproved property access refuses, rather than publishing a marker or a false NULL.
Partial results use a separate private key in the existing transaction-local,
bounded owner landing LRU; they never answer full-vector/entity reads. Their key
and payload have explicit conservative charges; overflow uses existing eviction
or non-retention behavior. Commit, rollback, heap epoch and owner-intent changes
retain their existing invalidation/cleanup. Pending owner updates replace the
physical tuple with their complete staged values. No authority is cached across
transactions or physical-generation changes.

No WAL, OCC, publication, reader/writer participation, lock policy or on-disk
format changed. Durable-header inspection introduced by the earlier repair is
independent of this optimization.

## Focused evidence

The focused 55-test landing/owner-memo slice passes, including new differential
queries, aggregate/ordering, duplicate edges, full-vector fallback, capability
absence, specialized hooks, bad-proof refusal, independent-writer snapshot
isolation, owner updates, reader cursor cleanup and corrupt omitted-vector
payload refusal. The same error dictionary is required for the latter case.
The accumulated complete query slice passed **2,324 tests in 357.63 seconds**;
Community graph-store/link integration passed **37 tests in 46.64 seconds**.
Ruff and `git diff --check` passed. These accumulated tests include the earlier
OPTIONAL/aggregate/path fixes as well as this optimization, rather than rerunning
the full database regression after each individual patch.

Testing also exposed a pre-existing accounting hazard: a pending `SET` can carry
a wrong-typed vector value before its canonical encoding refusal. Optional cache
accounting no longer assumes `.values` exists and raises `AttributeError` first;
it declines retention and preserves the canonical schema refusal/rollback. This
does not make a LIST accepted as a stored VECTOR or introduce SET coercion.

## Bounded A/B observation (not an endpoint speedup claim)

An isolated 160-destination, 384-dimensional, 480-edge graph ran six alternating
canonical/candidate rounds. All twelve results contained 480 identical rows and
SHA256 `652fc4f1ff17b37b1cc6bed554e3bb84ea25899ad28b667d0845518893a2d0b7`.
Median traversal transaction time was 165.67 ms canonical versus 149.84 ms
candidate (~9.6% lower), with mixed per-round ordering, so timing is noisy.
Owner-cache charged retention fell from 2,114,960 to 150,160 bytes (~92.9% lower)
and returned to zero after every transaction. This is a conservative budget
tariff, not process RSS or a measured native allocation peak.

Local reproducer: `.grafx-tmp/vector_free_traversal_bench.py`; its databases are
isolated under `.grafx-tmp/vector-free-*`. It never opens Pulse data. No cognitive
spec was consumed. The initial checkpoint did not restart Pulse; the deployment
validation below records the subsequent source-runtime restart.
The residual incident-layout fan-out in `find_by_artifact` is **not** claimed
eliminated; this change reduces decoding/retention within its existing queries.

## Source-runtime integration checkpoint

Pulse 0.3.3 was restarted onto native commit `1b53f57`, with explicit default
DATA_DIR and source roots for Community/Core/Grafx. PID 35824 serves ports 8100
and 8101. The previous process exceeded the 15-second HTTP shutdown window, but
logged successful Global/board graph closure and was confirmed exited before
the new process started; no OS force-kill or overlapping writer was used.

An authenticated typed incoming Constraint-to-Entity query through Pulse REST
returned HTTP 200 and 20 rows (1,264 ms executor-reported, cold observation, not a
paired speedup). A diagnostic unsupported-function query exposed a separate REST
error-handling gap: native planning correctly refused, but an uncaught neutral
error became plain HTTP 500. Core now defines only a backend-neutral invalid-query
failure; Community maps the native types and returns structured HTTP 400. The
exact original request was retested after restart. Authorization/error integration
passed 97 tests; details are in Community `docs/GRAFX_READ_QUERY_ERRORS.md`.

REST still reports 21 pending cognitive specs, zero in progress, 19 consolidated
and an unchanged ledger hash. The graph is queryable with no required recovery,
and active queue depth is zero. Historical DLQ/debt and bounded Health probe
timeouts are not claimed resolved. This is source-runtime validation, not a
new package publication or proof that the entire 0.0.4 plan is complete.
