# v0.0.7 performance wave A: platform qualification and consumer measurements

September 14, 2026. Development-source evidence, not release qualification.
This report separates merged mechanisms, prior evidence, newly observed results
and measurements that remain unavailable. `MEDIDO` means observed in this round;
`CITADO` means retained earlier evidence; `INFERIDO` means a stated inference;
`A_MEDIR` means not yet measured.

Grafx platform performance is the objective. Pulse is one consumer used to
evaluate the consequences, not the definition of an eligible optimization.
The operator clarified this scope during the transfer campaign. A separately
declared native matrix therefore measures public Grafx writes, lookup, ordering
and traversal without Pulse imports, schema, adapters or data. Generality must be
supported across the measured workloads; a conditional admission benefit is not
a universal gain, and an unchanged Pulse scenario does not refute a useful native
mechanism. Both sets of measurements retain their own denominators.

## Provenance and delivery boundary

| Item | Revision / boundary |
|---|---|
| Measured primary | `852b655e003be09b7c035650c6cc796c698a0706`, `feature/v0.0.7` |
| Pre-wave source | `b0e4f51`, released 0.0.6 plus the source-version bump |
| Older comparison | `83cc313`, published 0.0.5 |
| Same-code control | A separate invocation of `852b655`, independent disk candidate |
| Pulse Core | `ea11b767f561394d5fa3b3388deca2db44b83277` |
| Pulse Community | `72a2df3447617d87fcfbcb65d058562378fb73c8`; 26 modified frontend files, Python-source hashes retained separately |
| Measurement interpreter | CPython 3.11.14, NumPy 2.4.4, google-crc32c 1.8.0 with its C implementation, Windows 10.0.26200 |
| Raw evidence | `.grafx-tmp/perf07/waveA-ab/`; runner, per-process commands/status/wall, source hashes and answer oracles |

The primary source stays unchanged during the existing full regression and the
measurements. Documentation is prepared in `perf07/DOCS-wave-A`, initially at the
same source revision. No installed Pulse state, public default, persisted format,
capability activation or public trust boundary is changed by this work.

## What was merged

| Package | Delivery | Mechanism and limitation |
|---|---|---|
| READ-4 / READ-6 | `dc048a4`, merged in `4ac516b` | ORDER BY free names are memoized by expression identity within one statement, retaining the expression and not memoizing refusals. READ-6 was measured as NO_CHANGE; READ-9 is unchanged. |
| PARITY-REG / FIX-W | `01511bf`, corrected by `f6fd327`, merged in `661373b` | The write statement's staging mark shares its existing page-access section. Public result settlement keeps its own section. Parsing is decided outside the section. |
| READ-3 | `2d533f2`, tests/counter clarification `08dbfe2`, merged in `a46e0d2` | Batched endpoint landings admit tables with no vector column, subject to the existing canonical-port and blocking-consumer guards. A streaming LIMIT alone does not qualify. |
| W-01 / W-02 | `4f0f013`, `6200be0`, `b222f60`, merged in `0e8c13a` | The declared instance-private participant section uses an in-process lock. Exact name grammar and held/pending declaration fences preserve the mechanism boundary. Cross-process sections keep file locks. |
| Private-wait hotfix | `494c2b7`, merged in `852b655` | `_wait_for_local_lock` waits on the lock with a bounded timeout only with the real sleeper and a `SystemClock`; foreign/manual clocks retain sampling. This corrects starvation introduced by the private-section path. |

These are delivery facts, not five additive speedups. FIX-W and W-01/W-02 remove
overlapping coordination cost. The measured denominator decides the combined
effect; counting each change's saving again would double-count it.

## Prior attribution and discriminating evidence

The retained bisect attributes M1 to `66abf1f` (extra write-statement sections),
M2 to `5ca03a9` (free-name analysis repeated per row), and M3 to the public
NodeValue materialization boundary (`6b6ff12..69af5db`). M3 remains an owner
decision: a reported redundant copy is not authorization to trust an untrusted
engine collaborator. These are CITADO from the handoff and retained wave-A
reports, not newly executed bisects.

| Observation | Evidence and scope |
|---|---|
| FIX-W port tuple | CITADO, `f6fd327`: page-access / participant / stat / advisory-lock calls per write statement become `2/2/2/4`, from `3/3/3/6`; v0.0.5 was `1/1/1/2`. `test_statement_port_budget.py` asserts section counts and parse placement. |
| READ-3 frontier | CITADO, `08dbfe2`: one source with 200 distinct destinations receives `64+64+64+8`. Overlapping destinations give 100 batched port calls versus 131 scalar certificates, because the frontier is per source and cached destinations avoid proofs. This is not “8 invalidations for 500 rows.” |
| W-01/W-02 removal | CITADO, `b222f60`: 159 section entries, 154 participant entries in its 50-row shape; 306 revalidation syscalls and 308 advisory-lock calls removed. The historical 1.226x arm is a ceiling, not a delivered consumer gain. |
| Hotfix bisect | CITADO, `hotfix-vc/bisect/`: pre-wave, READ-4, FIX-W and READ-3 revisions pass the vector concurrency test; `0e8c13a` and `b222f60` time out. |
| Starvation guard | CITADO, `hotfix-vc/final/mutation_sampling_run{1,2,3}.log`: forcing the sampling wait produces one failure in the private-section module in all three retained runs. |
| Bounded-wait guard | CITADO, `hotfix-vc/final/mutation_unbounded_run{1,2,3}.log`: removing the lock timeout fails the bound test in all three retained runs. |
| Focused primary checks | CITADO, `hotfix-vc/final/postmerge_*`: public-boundary concurrency 33 passed; terminal close 31 passed; vector concurrency 2 passed per retained invocation. |

The commit message's claim of “three runs each” for the 50-row port counters has
only one retained result per tree. A new recheck cannot establish that the missing
historical runs happened. The current recheck is recorded separately below.

MEDIDO by Git/source inspection: `b222f60..0e8c13a` contains the two FIX-W
database commits, and `_LogicalStatementPublication` appears only after that
integration. The 154 participant entries reported on the W-01 branch therefore
precede FIX-W's removal of one entry per statement; 104 on the integrated
50-statement shape is a different code state. Do not present these as conflicting
measurements of the same revision or sum their syscall-saving headlines.

## Independent review of the handoff

The existing full regression should finish. Its verdict requires both the
process exit status and JUnit: the skip-attribution gate can set a nonzero status
without a JUnit failure. A new failure is not automatically attributable to the
hotfix merely because it is the most recent commit; first reproduce it on the
relevant ancestor. There was no passing full pre-wave result in the handoff that
would support attribution by elimination.

The old KG harness is retained only as a historical control. It uses an ID-list
predicate, generation descriptor revalidation and one explicit read transaction
for the fan-out. The pinned Pulse route uses visibility predicates without an
ID list, and the pinned executor invokes `database.execute` separately for each
relationship table. The current-shape measurement uses those templates and that
executor with strict revalidation. It does not measure HTTP, live board routing,
authentication or the user's running service.

Both queries have a streaming LIMIT without a blocking sort/aggregate; the
preserved board also has vector columns. Neither timing can by itself establish
READ-3's benefit on an eligible vector-free workload. A separate admission probe
observes that boundary without wrapping canonical index ports, since replacing
those ports would itself cause admission to be refused.

Worktree/branch deletion is not necessary to establish regression or timing
results. Existing unmerged/rejected work is preserved during this task.

After the operator's platform-scope clarification, READ-1 is no longer deferred
solely because the pinned Pulse route has no ID-list predicate. Its implementation
priority requires representative native evidence and the existing refusal proof.
The old 3.74x query-rewrite proxy is useful attribution, not an implemented planner
gain or a reason to change consumer queries automatically.

## Regression and current counter recheck

The original full run completed on `852b655`: **24,780 passed, 2 failed,
41 skipped**, pytest exit **1**, in 4,673.38 seconds of pytest time (4,689 seconds
of launcher wall). Its JUnit contains exactly the two failures named below.
The terminal gate additionally identified two un-attributed Arrow skips.
Source manifest, JUnit, exit status and complete log are under
`.grafx-tmp/perf07/gate-852b655/`. This is a failed full run; it is never relabeled
green by the subsequent focused checks.

| Failure / gate issue | Independent diagnosis and correction |
|---|---|
| `test_a_series_runs_warmup_plus_three_fresh_processes_and_reports_a_distribution` | Reproduced at pre-wave `b0e4f51`. All fake child runs exited zero and emitted 101/102/103 as intended, but missing `psutil` left RSS/USS unavailable, so the runner correctly rejected them as clean measurements. Declare `psutil` in the development dependencies. |
| `test_synthetic_profile_uses_spawned_direct_child_and_ready_go_order` | Reproduced at `b0e4f51`. The uv Windows venv launcher inserts an intermediate process, violating the test's direct-child premise. The corrected happy-path test preflights and uses the actual interpreter while preserving the PID, parent and executable identity assertions. |
| Two graph-export skips | The Arrow/NetworkX-dependent test lacked the source-visible dependency markers. Add both; missing integrations remain explicitly skipped rather than weakening skip attribution or fabricating an export result. |

The relevant source/test files are byte-unchanged across wave A. Corrections are
in `9e3421d` on `fix/perf07-tool-test-environment`: two tooling modules **42 passed**;
graph namespace/projection/migration module **10 passed, 2 attributed skips**;
ruff, documentation and lockfile checks passed. There is no `src/` diff. The
lockfile was also synchronized with the current manifest; its previous root still
described 0.0.4. No second full run was performed for these test/environment
corrections. Their focused results are separate evidence, not a replacement JUnit
for the original full run.

`waveA-ab/gate_review.json` records the bounded decision to permit measurements of
unchanged runtime `852b655`, retaining hashes of the failed full JUnit, the
baseline reproduction and the passing focused reports. This does not establish
a green new full gate or release qualification.

The new deterministic port recheck on September 14 independently observed the
same tuple on `0e8c13a` and `852b655`: **4** file-lock acquisitions, **104** private
participant entries and **0** participant file-lock acquisitions for 50 individual
write statements. `waveA-ab/ports_recheck.json` retains imported paths, exact
revisions, results and the original instrument hash. These are new receipts, not
reconstructed pre-commit runs.

## Consumer measurement protocol and results

Four fixed balanced blocks: `head, base, v005, null`;
`base, null, head, v005`; `null, v005, base, head`;
`v005, head, null, base`. Each arm occupies each position and every ordered
adjacent pair occurs once. Ratios mean comparison wall / head wall: greater than
one favors head. Report the individual pairs and the null control as well as
medians. Four blocks provide descriptive evidence, not a high-powered statistical
proof or immunity to nonstationary background load.
For a comparison/head ratio `r`, head's latency reduction relative to that
comparison is `1 - 1/r`. Consequently the old `v005/head = 0.945` implies about
5.8% excess head latency relative to v005, rather than the older prose's 5.5%.
The original measured ratio is retained; the percentage's denominator is corrected.

Transfer uses the unmodified retained `amdahl_pulse.py` with 300 nodes/type,
60 relationships/layout, batch 500, native CRC and fresh disk candidates.
Each process has two rounds; discard its first round and retain its second.
The instrument already includes certification, restoration and runtime samples,
so separate broad vector/verify/micro campaigns are not repeated without a
specific discrepancy.

KG has a copied board per arm and a fresh process per block/shape. Each process
has four passes; discard pass zero and use the median of passes one through
three. This differs explicitly from the inherited harness's minimum-of-warm-pass
summary. Every pass and arm must agree on its page and edge digests/counts;
different query shapes retain separate answer oracles.
Both new KG shapes use the common Pulse interpreter with NumPy 2.4.4; the
earlier KG baseline used the repository interpreter with NumPy 2.4.6. This is a
new controlled comparison, not an exact replay of the earlier environment.

The retained driver commands, from the primary repository, are:

```powershell
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/waveA-ab/run_measurements.py transfer
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/waveA-ab/run_measurements.py kg
```

Each driver launches the pinned Pulse interpreter specified in the provenance
table. `source_manifest.json` fixes runtime sources and instruments; each
`*.receipt.json` retains the exact child command, timestamps, exit status and
before/after process CPU observations. Source drift is refused. These local
artifacts include copied databases and metrics and are intentionally not committed.
Reproduction requires the retained instruments and the named consumer checkouts;
this report does not imply that a clean Grafx checkout contains those fixtures.

### Consumer observations (MEDIDO)

All 16 transfer invocations and 32 KG invocations exited zero. Transfer counts
and batches agree across arms; both KG shapes return the same 500 page rows and
1,375 edges on this fixture, with matching digests in every pass.

Walls below are medians in seconds; ratios are medians of the four paired
comparison/head ratios, not ratios of the two wall medians.

| Workload | Head | Pre-wave | v0.0.5 | Same-code control | Pre-wave/head | v0.0.5/head | Control/head |
|---|---:|---:|---:|---:|---:|---:|---:|
| Transfer total | 39.751 | 42.821 | 37.777 | 37.755 | 1.045 | 0.948 | 0.912 |
| Write nodes | 7.881 | 9.107 | 7.357 | 7.197 | 1.184 | 0.942 | 0.875 |
| Write relationships | 14.067 | 16.430 | 13.448 | 14.585 | 1.155 | 0.934 | 0.974 |
| Checkpoint | 3.149 | 3.240 | 3.037 | 2.926 | 0.941 | 1.019 | 0.950 |
| Certify | 9.277 | 9.405 | 8.151 | 8.407 | 0.910 | 0.929 | 0.894 |
| Restore/read | 4.558 | 4.511 | 4.054 | 4.072 | 0.963 | 1.020 | 0.861 |
| Vector sample (30 queries) | 1.736 | 1.709 | 1.681 | 1.861 | 1.020 | 0.957 | 1.044 |
| KG legacy page | 0.328 | 0.398 | 0.269 | 0.350 | 1.186 | 0.817 | 1.078 |
| KG legacy fan-out | 1.221 | 1.209 | 1.059 | 1.282 | 1.050 | 0.867 | 0.993 |
| KG current page | 0.306 | 0.317 | 0.308 | 0.307 | 1.011 | 0.923 | 0.984 |
| KG current fan-out | 4.442 | 4.520 | 4.032 | 4.617 | 1.004 | 0.911 | 1.034 |

Individual paired ratios, in the predeclared block order:

| Workload | Pre-wave/head pairs | v0.0.5/head pairs | Same-code/head pairs |
|---|---|---|---|
| Transfer total | 0.851, 1.072, 1.316, 1.018 | 0.699, 0.926, 1.172, 0.970 | 0.776, 0.981, 1.075, 0.843 |
| KG legacy page | 1.213, 1.158, 1.467, 0.952 | 0.656, 0.857, 0.919, 0.776 | 0.717, 1.302, 1.390, 0.854 |
| KG legacy fan-out | 1.073, 1.076, 1.027, 0.930 | 0.705, 0.927, 1.036, 0.807 | 0.840, 1.142, 1.265, 0.844 |
| KG current page | 0.909, 0.968, 1.126, 1.053 | 0.851, 0.996, 0.821, 1.211 | 0.838, 1.015, 1.071, 0.953 |
| KG current fan-out | 1.010, 0.999, 1.031, 0.993 | 0.884, 1.124, 0.889, 0.932 | 1.014, 1.048, 1.020, 1.065 |

The transfer median ratio of 1.045 corresponds to a descriptive 4.3% latency
reduction versus pre-wave, but its pairs range from 0.851 to 1.316 while the
same-code pairs range from 0.776 to 1.075. **This campaign does not establish a
reliable total-transfer gain.** Current-shape KG page/fan-out ratios are only
1.011/1.004, versus control medians 0.984/1.034; neither supports a latency gain.
The old shape's larger page ratio also comes with a wide null range and must not
be presented as current Pulse endpoint performance.

The write-execution components show a favorable descriptive direction versus
pre-wave: node execute median 3.472 s (paired ratio 1.348) and relationship execute
10.766 s (1.200), contributing median per-round fractions 8.6% and 28.0% of head
transfer. Their corresponding commit ratios are 0.968 and 0.991. These components
locate the possible effect; their variable pairs and overlapping mechanisms do
not establish separately additive FIX-W and W-01 gains. Native workloads below
provide a separate platform assessment.

Vector samples, cold verification within certification, and restore/read all
retain broad paired/null variation. No additional performance regression is
established by those timings, and no broad repeat campaign was triggered.

Before/after CPU snapshots describe matching surviving processes only. They omit
processes born or exited during each arm, so they are not a complete load oracle
or a correction to elapsed time. The same-code variation remains part of the
reported uncertainty. See `reviewed_results.json` and the raw receipts.

### Native platform observations

The native matrix uses only Grafx public APIs and a deterministic scalar fixture:
2,000 Item nodes, 101 source nodes, 200 destination nodes and 3,400 relationships.
The wide shape has one source and 200 distinct destinations; the overlapping
shape has 100 sources and 32 destinations each. Expected tuples are constructed
independently in Python, including order where requested, then checked against
every result. Writes create 110 nodes and 50 relationships per round and are
checked again after checkpoint and read-only reopen.

All 16 accepted native comparison invocations passed. Each process uses two
fresh write stores (first discarded), and four passes per read workload (first
discarded; median of the remaining three). Same versions, interpreter, native
CRC and balanced blocks as above; default strict revalidation. Setup, checkpoint
for reopen validation and probes are outside latency samples. This is native
API evidence, not a concurrency-throughput or universal-scale benchmark.

Three preflight failures are retained separately: an oversized seed parameter
list; v0.0.5 refusing UNWIND CREATE; and a worker attempting a read-only reopen
without the required checkpoint. They caused instrument corrections before the
accepted `native_v2_*` matrix. Seed setup uses scalar writes. UNWIND is unavailable
on v0.0.5, so that arm populates equivalent later data with untimed scalar writes
and has no bulk latency value. The ten-transaction case uses explicit write
transactions: `database.execute` is an autocommit **read** API.

Walls are milliseconds; ratio columns are medians of paired comparison/head.

| Native workload | Head | Pre-wave | v0.0.5 | Control | Pre-wave/head | v0.0.5/head | Control/head |
|---|---:|---:|---:|---:|---:|---:|---:|
| PK lookup | 1.495 | 1.704 | 1.556 | 1.459 | 1.101 | 1.007 | 0.987 |
| Top 50 / 200 scalar rows | 35.607 | 34.656 | 29.132 | 33.023 | 0.949 | 0.831 | 0.941 |
| Top 50 / 2,000 scalar rows | 67.941 | 77.787 | 40.577 | 57.235 | 1.164 | 0.596 | 0.855 |
| 200 distinct destinations, ordered | 28.554 | 88.907 | 75.000 | 31.533 | 3.060 | 2.602 | 1.105 |
| 200 destinations, streaming | 96.982 | 92.580 | 88.362 | 90.603 | 0.940 | 0.944 | 0.934 |
| 100 overlapping sources, ordered | 146.582 | 227.211 | 168.204 | 155.626 | 1.592 | 1.113 | 1.129 |
| 100 overlapping sources, streaming | 342.353 | 320.166 | 317.900 | 331.495 | 0.960 | 0.900 | 0.952 |
| 50 node statements: stage | 31.626 | 46.648 | 28.721 | 31.482 | 1.496 | 0.891 | 1.002 |
| 50 node statements: commit | 33.676 | 35.006 | 31.110 | 34.816 | 1.049 | 0.925 | 1.053 |
| 50 node statements: total | 65.933 | 82.587 | 60.136 | 66.414 | 1.265 | 0.910 | 1.029 |
| UNWIND 50 nodes: stage | 19.991 | 19.883 | — | 20.356 | 1.007 | — | 0.999 |
| UNWIND 50 nodes: commit | 41.628 | 40.297 | — | 41.476 | 0.978 | — | 1.001 |
| UNWIND 50 nodes: total | 63.576 | 61.501 | — | 62.580 | 0.996 | — | 1.006 |
| 50 relationship statements: stage | 65.443 | 80.099 | 58.696 | 66.526 | 1.246 | 0.915 | 1.045 |
| 50 relationship statements: commit | 46.536 | 47.808 | 45.438 | 48.283 | 1.027 | 0.986 | 1.037 |
| 50 relationship statements: total | 112.740 | 130.326 | 105.403 | 115.115 | 1.144 | 0.935 | 1.049 |
| 10 nodes / 10 write transactions | 171.211 | 179.773 | 171.549 | 183.403 | 1.023 | 1.002 | 1.035 |

Key paired ratios (the full set remains in `native_summary.json`):

| Native workload | Pre-wave/head pairs | v0.0.5/head pairs | Control/head pairs |
|---|---|---|---|
| 200 distinct destinations, ordered | 2.793, 4.400, 3.183, 2.936 | 2.656, 2.638, 2.565, 2.526 | 1.136, 1.073, 0.921, 1.173 |
| 100 overlapping sources, ordered | 1.589, 1.596, 1.684, 1.403 | 1.796, 1.051, 1.033, 1.174 | 1.156, 0.807, 1.145, 1.113 |
| Top 50 / 2,000 scalar rows | 1.120, 1.399, 1.207, 0.982 | 0.607, 0.585, 0.611, 0.543 | 1.218, 0.881, 0.829, 0.727 |
| 50 node statements: stage | 1.549, 1.267, 1.766, 1.443 | 0.946, 0.836, 1.121, 0.826 | 1.076, 0.910, 1.103, 0.927 |
| 50 relationship statements: stage | 1.281, 1.197, 1.260, 1.233 | 0.937, 0.938, 0.893, 0.877 | 1.034, 1.056, 0.977, 1.059 |
| 50 relationship statements: total | 1.269, 1.138, 1.151, 1.128 | 0.977, 0.936, 0.934, 0.920 | 1.070, 1.049, 0.993, 1.048 |

The ordered wide and overlapping traversals show substantial conditional platform
benefit: paired ratios 3.060 and 1.592 versus pre-wave, corresponding to 67.3%
and 37.2% less latency in these shapes. Every pre-wave pair exceeds the observed
control range. Both READ-3 admission and READ-4 sort memoization participate;
this campaign does not isolate their individual timing contributions.

Staging 50 separate node/relationship statements improves by descriptive paired
latency reductions of 33.1%/19.8%, with all four pairs above the respective control
ranges. Relationship staging plus commit improves by 12.6% in this fixture.
Node total includes one reversed pair; commits alone, bulk UNWIND and one write
transaction per node do not establish general gains. This supports general
per-statement engine work as a useful target without claiming every transaction
or batching pattern becomes faster.

Streaming traversal and standalone top-K do not establish a latency gain against
pre-wave beyond the observed same-code variation. The 2,000-row scalar sort also
retains a material difference versus v0.0.5: 67.941 ms head, 57.235 ms same-code
control, 40.577 ms v0.0.5; all v0.0.5/head pairs are 0.543–0.611. A bounded
call-profile diagnostic follows, rather than attributing this residual to the
last merge or automatically changing a public value boundary.

Untimed probes in `probe_summary.json` confirm native wide/overlap ordered
queries admit batching once each on head, while their streaming counterparts
refuse. Neither old nor current KG admits it (old: zero admission calls;
current: 69 calls, zero accepts). Native sort name-walk calls fall from 400/4,000
to two at the two scales; the overlapping ordered traversal falls from 6,400 to
two. These are direct mechanism observations, not profiled speedup estimates.

Reproduce the native campaign with
`.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/waveA-ab/native_driver.py`;
`native_source_manifest.json`, `NATIVE_PROTOCOL.md` and per-process receipts
retain its exact source/instrument boundary and the preflight history.

### Bounded scalar-sort diagnostic and next priorities

The retained `profile_native_sort_{head,base,v005}.json/.prof` runs use ten
warmed executions of the native 2,000-row top-K query, with exact answers checked.
These are call-profile diagnostics; their instrumented seconds are not additional
latency samples or fractions of the unprofiled wall.

| Calls across ten queries | Head | Pre-wave | v0.0.5 |
|---|---:|---:|---:|
| Full tuple decode | 20,000 | 20,000 | 20,000 |
| Sort-key construction | 40,000 | 40,000 | 40,000 |
| Sort-expression evaluation | 40,000 | 40,000 | 40,000 |
| AST free-name analysis | 20 | 40,000 | 0 |
| Public scalar value snapshot | 1,000 | 1,000 | 1,000 |

READ-4 removed the repeated AST walk, but each row still pays sort dispatch and
alias-precedence work. Source inspection shows current `_sort_key` handles the
expanded mixed-kind ordering and reaches integers after Mapping/entity/path/list
checks; `_sort_value` retains the per-row shadowing check, even with memoized
free names. Property evaluation also carries the newer entity-content rules.
These are concrete profiling leads, not permission to remove their semantics.
The public scalar snapshot cost and call count are essentially unchanged in this
diagnostic, so the result does **not** support selecting M3's public-value
trust-boundary change as the remedy for this scalar-sort gap.

The next bounded platform investigation should target scalar sort-key dispatch
and preparation of sort-expression metadata, retaining mixed-kind rank, NULL/NaN,
alias precedence, refusals and entity-content checks. READ-1's anchored-query
priority should be measured on native workloads rather than decided from Pulse's
query shape. Neither is implemented in this testing/documentation continuation.
W-08's 0.24% commit proposal and the rejected checkpoint branches have no new
evidence that would justify displacing those targets.

## Remaining owner decisions

Contract decisions Q1–Q15 remain unchanged. Q14's measurement prerequisites are
partly supplied by this task (`psutil` and isolated board copies); the Ladybug
comparison remains unmeasured. Q16 is W-08's proposed use of `operator.is_`, outside the
frozen standard-library allowlist, for a reported 0.24% of the commit. M3 changes
the NodeValue trust boundary. Neither is implemented here. CKPT-2 remains
rejected after spurious `index_entry_unresolved` findings under eviction; CKPT-4
remains stopped; W-07 was refuted by its measurement. A missing consumer gain does
not itself authorize a contract change or another implementation campaign.

See the [performance plan](../specs/PERFORMANCE_V007_PLAN.md),
[baseline](PERF_V007_BASELINE.md), [performance guide](../PERFORMANCE.md) and
[roadmap](../../ROADMAP.md#next-iteration-assessment-featurev007).
