> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Endpoint-version reuse evaluation — not integrated

This is a bounded decision on the existing incident-layout fan-out residual,
not another performance gate or a new implementation target.

## Current-path profile

The existing read-only restored board was used, not the live Pulse graph:
`.grafx-tmp/real-board-transfer-v004-20260907-a/restored`.
The existing incident-layout probe returned 105 neighbours through 36 statements.
The second unprofiled warm-up took 123.71 ms. The instrumented pass made 210
`HeapStore.read` calls and took approximately 239 ms; profiling overhead means
the latter is not comparable to the unprofiled wall times.

`_index_lookup_versions` intentionally retains only refs for general endpoint
indexes: the native exact validator reads each candidate, then the query layer
reads accepted versions again. Keeping every decoded hub payload would grow
memory with degree times payload. Automatic PK queries already reuse their
semantically bounded validated versions.

## Candidate and decision

A local prototype retained only a bounded subset of endpoint versions while
leaving full exact validation and pre/post certificates in place. It reserved
space in the existing shared decoded-value budget, with per-call ceilings of
256 KiB and 256 entries. Each stable-view attempt reset retention. Overflow,
ended rows and specialized collaborators retained the original read path.

Six alternating pairs, each using a new read transaction, returned the same
105 rows and ordered SHA256
`c9f3f0429e8811dfddd8919b9f6d724603a11ef331412918cc87eec74953a89c`.
Excluding the first pair from warm medians:

| Mode | Warm median |
|---|---:|
| Current path | 112.37 ms |
| Prototype | 113.93 ms |

Individual pairs were mixed. This sample demonstrates no consistent speedup;
it does not establish a statistically significant regression either. The added
retention/cleanup/retry surface is not justified by this result. **The prototype
was withdrawn before quality qualification and was not committed or deployed.**
No claim is made that its corruption/concurrency matrix passed: that matrix was
not run for a rejected cost experiment. Do not promote it based solely on result
parity or reduced duplicate reads.

Local artifacts: `.grafx-tmp/endpoint_version_ab.py` and
`.grafx-tmp/endpoint-version-rejected.patch`. The benchmark requires the rejected
prototype and is not directly runnable against the restored canonical source.
The production runtime was not restarted or modified. No cognitive spec,
consolidation, repair or redrive was used. Keep the remaining finite-plan work;
do not prolong this marginal candidate into another timing gate.
