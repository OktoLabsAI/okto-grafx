# Final 0.0.6 native cost observations

September 13, 2026. [Current performance](../PERFORMANCE.md) ·
[Native qualification](FP_FINAL_NATIVE_QUALIFICATION.md) · [Roadmap](../../ROADMAP.md#remaining-performance-work).
These measurements record current costs, not a before/after speedup or performance gate.

## Candidate and boundaries

Private installed Grafx 0.0.6 wheel SHA-256
`d666704a14492d737c43f0293e71605aeaf279aad65b4086d08bcb0160020b70`.
Windows/CPython 3.13.1, NumPy 2.5.3, google-crc32c 1.8.0. All 251 installed package
files equal the supplied wheel before/after the sample. Default persistent local
configuration: 8,192-byte pages, 64 partitions, 64 MiB buffer, strict descriptor
revalidation, native normal WAL/durable commit, NumPy codecs. No durability switch,
mock storage, production dataset or in-memory database was used.

Two fresh directed-chain fixtures: 64 nodes/63 edges and 1,024 nodes/1,023 edges.
IDs are 0..n-1, initial `v=2*id`. These are two bounded sizes, not a claim about
production-scale graphs. Four read queries each run five times on the same live
handle: first-call cost is retained in the receipt; the table shows the median of
the four subsequent calls. Reads include public autocommit and materialization.
Three measured write transactions each update the same 32 distinct nodes once;
all three include begin, execute and COMMIT, with no excluded warm-up write.

The independent full regression was running simultaneously. No machine-idle,
tail-latency, RSS, throughput SLO or isolated version-to-version speedup is claimed.
No performance threshold was used to approve/reject implementation.

## Latest measured values

| Operation | 64 nodes / 63 edges | 1,024 nodes / 1,023 edges |
| --- | ---: | ---: |
| Indexed exact lookup, one returned value | 2.335 ms | 3.248 ms |
| Sorted first page, 50 exact IDs | 4.394 ms | 43.537 ms |
| Complete count and exact sum | 3.177 ms | 38.336 ms |
| Named outgoing trail, 0..3 hops, four exact rows | 6.066 ms | 8.589 ms |
| Durable transaction updating 32 nodes | 149.304 ms | 992.746 ms |

The update uses `UNWIND range(0,31) AS i MATCH(n:N {id:i}) SET n.v=n.v+1
RETURN count(n) AS total`. Its scalar driving key currently retains scan work;
do not describe this as the fastest available indexed batch API.

Fixture preparation and maintenance are separate in the receipt. The final
indexed map-batch seed wrote 1,024 nodes in 1,657.202 ms and 1,023 edges in
3,920.131 ms. Post-write checkpoints took 98.620/167.104 ms and are not part of
the commit medians. The read-only cold-open contract requires that checkpoint.

Readback after close/reopen verifies every exact `(id,v)` and edge count.
`verify('all')` reports zero findings on both stores: respectively 266/280 pages,
223/2,143 records and 446/4,286 index entries, including retained update versions.
The graph has exactly the requested node/edge counts, not those physical version
counts. No silent missing rows, paths or failed write were accepted.

## Confirmed existing optimization opportunity — future roadmap, not a new gate

The actual public plans distinguish two existing endpoint-binding shapes:

- `UNWIND range(...) AS i ... {id:i} ... {id:i+1}`: two `NodeScan` operators followed
  by filtering. In the attempted 1,024-node fixture this led to an expensive repeated
  scan/cross-product preparation, which was intentionally stopped.
- `UNWIND $rows AS row ... {id:row.source} ... {id:row.target}`: two `IndexSeek`
  operators. The final fixture uses this already-supported batch shape, with all
  input pairs and the actual plans recorded before writes.

Planner `_seekable_key` currently admits literals/parameters and direct properties
of the UNWIND element, not a scalar UNWIND variable or arithmetic over it. A future
optimizer improvement can widen safe driving-row bindings after proving binding
order, NULL/type/error behavior and dependencies. It must not probe a matched
variable that has not been produced. This is a known cost limitation, not evidence
of wrong results or a reason to change the frozen parity scope. No product change
was made here. It is recorded as `Future / PERF-DRIVING-KEYS` in the performance roadmap.

## Reproduction and retained attempts

Run [the tracked sample](../../tools/fp_native_cost_sample.py) with the final private
interpreter, `-I`, the exact wheel and a fresh output directory. It creates only
its explicitly selected new fixture directory. Script SHA-256
`fb08e974fd70b011f6f431efda01709f0686885efe4487138735b937c4e30340`.
Final receipt `.grafx-tmp/fp-final-wheel-qualification/native-cost-indexed-setup/report.json`,
SHA-256 `bf7d18cbe66014921d378a39418f2fe1da253889af00d29dc77272aa707240ef`.

The first attempt (`native-cost-first`) correctly refused a cold read-only open
after uncheckpointed writes. The observer was corrected to checkpoint explicitly
after the measured commits; no runtime admission/recovery policy was changed.
The next attempt (`native-cost-qualified`) completed the 64-node case but was
stopped during scalar-key preparation of the 1,024-node case. Its existing receipt
and data are retained, and `termination.json` identifies exact test PID 25468 and
why it was stopped. A stale `status=running` in that earlier partial receipt is
not a claim that the process is still alive or that the attempt passed.
Only the fresh indexed-preparation run completed both full samples.

```powershell
.grafx-tmp/fp-final-wheel-qualification/venv/Scripts/python.exe -I tools/fp_native_cost_sample.py --wheel .grafx-tmp/fp-final-wheel-qualification/dist/okto_grafx-0.0.6-py3-none-any.whl --output .grafx-tmp/fp-native-cost-recheck
```

The output directory must not exist. Timings will depend on load; exact rows,
reopened values and zero verification findings are the correctness assertions.

