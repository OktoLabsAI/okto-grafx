# Verification history payload retention — 2026-09-08

## Evidence and selected scope

The authorized single-spec Pulse run completed its MCP commit in 22.981 s but
its Global event took 131.489 s from creation to ACK. Those receipts do not
attribute time within delivery. A stack profile from the previous day covers
an older execution and is not evidence for the current 131-second breakdown.

Read-only certification of the already preserved private Global fixture
`.grafx-tmp/global-postflush-audit-20260907` with installed Grafx `2db169d`
took 32.004 s under cProfile. Native `verify(all)` accounted for 31.901 s;
index verification 16.290 s and record verification 13.219 s. The profile
decoded 27,240 heap versions and checked 102,763 index entries. Profiling
overhead is material: this is not uninstrumented production latency and cannot
be subtracted from the 131.489 s as a proven phase duration.

The finite change addresses memory held while verifying history-rich tables,
not removal of whole-store verification or weakened post-flush ACK gates.

## Implementation and preserved semantics

`Verifier._canonical_versions` formerly materialized all decoded historical
versions until the last built-in index of that table was checked. Coverage
already ignored versions whose `HeapVersion.live` is false, but their large
tuples/vectors stayed resident in the verification memo.

The canonical scan still fully decodes **every** version, including tuple and
overflow validation. Only then does it discard values that the unchanged
coverage predicate will not use. All successfully decoded exact RecordRefs are
retained separately for index-reference resolution. Both sets of evidence are
published only after the complete table scan succeeds. A late failure publishes
neither partial rows nor partial resolution proofs and keeps the old per-entry
fallback/finding behavior. Catalog identity checks still precede seeding.

`live` is the existing committed-birth/open-end predicate, not a new snapshot
visibility policy. Abandoned births remain excluded from coverage and a
provisional end retains its existing interpretation. This does not vacuum or
remove historical records, change a reader's snapshot, or shorten version chains.
Foreign version objects are retained without eagerly evaluating their live
property; custom collaborators keep the existing noncanonical protocol.

State remains local to one verification call and is released at the last index
of the table. Memory for validated physical references is still O(all versions),
while retained decoded payloads are O(coverage-live versions), plus the scan's
ordinary transient page/row state. There is an additional reference set; this is
not a claim of constant memory or reduced file I/O. No persistent cache,
authority bundle, configuration, file format, WAL/OCC or reader/writer change.

## Validation

- Initial verifier slice: 101 passed.
- Combined index/verifier/suffix/retention slice: **701 passed in 34.57 s**.
- Ruff and `git diff --check` passed.
- New cases cover 8/64-version histories; complete report equality against the
  unshared reference path; all historical refs resolved without extra heap reads;
  repeated fresh calls; corrupt old tuples still producing unresolved/coverage
  findings; late scan failure without partial proofs; last-index release; and
  foreign live-property observation behavior.

One read-only private comparison used the previous full-retention algorithm and
the candidate against the same preserved fixture, reopening each arm. The full
reports were equal: **15,003 pages, 27,240 records, 102,763 entries, zero findings**.

| Table | Decoded value objects retained, before → after | Validated references, both |
| --- | ---: | ---: |
| DecisionDigest | 23,304 → 2,261 | 23,304 |
| Board | 1,675 → 1 | 1,675 |
| CONTAINS_DECISION | 2,261 → 2,253 | 2,261 |

DecisionDigest retained rows represent 78,748,424 stored payload bytes before,
7,666,675 after (~90.3% reduction); Board 5,366,700 → 3,204 bytes. These are
stored-payload equivalents associated with retained decoded objects, **not RSS
or measured Python heap bytes**. Actual memory includes objects, references,
sets, buffers and allocator overhead.

Wall time was **13.327 s before / 15.163 s after**. The comparison overlapped
the test slice and is not controlled; there is no demonstrated wall-time gain.
Retain the change for the proven reduction in history-sized payload residency,
not as evidence that full delivery is now fast. No repeated marginal timing
gate was run. Tools: `.grafx-tmp/profile_global_certification_current.py`,
`.grafx-tmp/compare_certification_retention.py`; profile
`.grafx-tmp/global-certification-current-20260908.prof`.

## Deployment and remaining task

Source checkpoint only. Pulse PID 2124 continues using the installed `2db169d`
wheel; no restart, live consolidation or historical delivery replay was used.
The 20 reserved specs remain untouched. Queue this change for accumulated
deployment, not a restart for this memory-only slice. Attribution of current
Global apply/reconcile versus flush/reopen/verification remains the existing
write-performance task; no exact allocation of the live 131.489 s is claimed.
The previously recorded nine static architecture findings remain open.
