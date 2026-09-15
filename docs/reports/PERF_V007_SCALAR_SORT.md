# v0.0.7 native scalar sort dispatch

September 14, 2026. Development-source evidence for a Grafx platform optimization.
Pulse is a potential beneficiary; this round measures native Grafx queries and
does not measure Pulse HTTP, browser latency or installed production behavior.

## Decision and mechanism

Keep the bounded scalar dispatch change in `0919c06ac8597af3a77454d68be7b61ce3336b55`,
based on `7d52f8a43195307437f1e33f8d121c0c567ead15`. The previous
[wave-A diagnostic](PERF_V007_WAVE_A.md) identified repeated general-kind probes
inside `_sort_key` on a 2,000-row native ordering query. Its profile was an
attribution lead, not a latency estimate. This round implements that lead and
measures public query execution separately.

Exact built-in `int`, `float`, `str` and `bool` values now return their existing
ordering keys before the Mapping, entity, path and collection probes. The change
adds thirteen lines to the helper. Mixed-kind ranks, numeric ties, NaN handling,
signed zero, null ordering and recursive keys retain their existing definitions.
Subclasses retain the original general path: a numeric or string subclass can
also implement Mapping, whose precedence must remain intact. The helper also
serves MIN/MAX; the optimization is not tied to a consumer or a query template.

Alias metadata remains a separate unimplemented lead. This change does not alter
the public value trust boundary, planner admission, persisted formats, defaults,
locking or materialization. The scalar profile did not justify M3's proposed
public-boundary change, and savings from this dispatch must not be added to the
older READ-4 attribution ceilings.

## Correctness and discriminating checks

`tests/query/test_scalar_sort_dispatch.py` contains 23 cases. Before the production
change it produced **one failure and 22 passes**: exact scalar inputs made 13
abstract Mapping probes. With the change those probes are zero. This count proves
removal of work at the intended boundary; it is not a speedup percentage.

The focused selection passed **510 tests in 43.45 seconds**, covering scalar
dispatch, the query engine, memory spill, top-N, ORDER BY free-name memoization,
alias/grouping precedence, aggregate ordering, ordered projection and decimal
numeric behavior. Ten separately injected incorrect helper variants each caused
an assertion failure with no collection or JUnit errors: original dispatch,
widened int/float/string dispatch, wrong numeric/string/boolean ranks, broken NaN
normalization, lost negative-zero sign and wrong map rank. Each mutation ran in a
fresh process; no mutated source entered the benchmark or full regression.

The first complete-suite attempt at `0919c06` reported a packaging-guard failure:
the exact development-extra list in `test_packaging.py` had not been updated when
`9e3421d` declared `psutil>=5.9`. The same test failed independently at baseline
`7d52f8a`, before this sort optimization. Commit `99cd113` corrects the expected
list and retains the exact-extra assertion; its packaging module passes **21
tests in 7.68 seconds**. Runtime source remains identical to `0919c06`.

The first suite was deliberately interrupted after 2,599.86 seconds to restart
the entire suite with this correction. Its partial log, interruption reason and
termination status are retained under `sort-b/full-gate/`; it has no completed
JUnit and is not a passing full regression. The metadata-only auxiliary
collection identified the failed test before termination.

The second complete suite at `99cd113` finished with **24,804 passes, one failure,
43 skips and exit 1** (4,347.70 seconds in pytest; 4,358.90 seconds launcher wall).
It is retained under `sort-b/final-gate/`. The failure was the Windows timeout
cleanup test: `taskkill` lost the launcher's ancestry, and the fallback attempted
to discover descendants only after the root had disappeared. A descendant wrote
the late-output sentinel after the test failed. This was an actual escaped child,
not merely a refused sample. The original focused test passed once at baseline,
so the failure is intermittent; its tooling code was unchanged by scalar dispatch.

Commit `a504417` snapshots psutil's process objects before Windows `taskkill`,
letting fallback cleanup reach known descendants after root loss. Nonzero or
unavailable Windows termination proof still raises `RunRefused`; cleanup does not
turn an unproven measurement into a sample. POSIX process-group handling remains
unchanged. This addresses known descendants, not proof of every unobserved fork.
The integration test now waits for an acknowledged descendant before timeout,
requires every captured process to be dead, and uses independent final cleanup.
A deterministic root-loss test catches the old helper. Two additional incorrect
variants—accepting unproven termination and killing only the parent—also fail
assertions, with no JUnit errors. The affected measurement and profiling modules
pass **43 tests in 70.61 seconds**; the initial focused selection passes three.

Two of the second suite's skips were unattributed historical wheel-receipt audits:
the isolated worktree lacked `fp5-wheel-qualification/run-4` and `run-5` reports
already retained in the primary workspace. The final gate supplies byte-identical
copies, with source paths and SHA-256 hashes in `historical_receipt_inputs.json`
and the gate manifest. All **20 tests** in that audit module pass with these
inputs. They audit historical v0.0.6 receipts; they are not a fresh installed-wheel
qualification of this v0.0.7 candidate. No fabricated fixtures or relaxed skip
rules were used. These retained, untracked inputs are explicit prerequisites for
reproducing the full local gate.

The final complete run below starts from both corrections and retains collection
identities and immediate failure diagnostics as well as final JUnit and exit
status. Its `src` tree is still identical to measured revision `0919c06`.

The complete regression on revision `a504417` passed **24,808 tests**,
with **41 attributed skips**, zero failures/errors and process exit **0**.
Pytest reports 4,300.02 seconds; launcher wall is
4,313.18 seconds. Full log, JUnit, exit status and source manifest
are retained under `sort-b/qualified-gate/`. The source and test tree stayed
unchanged during this gate. This is a new full run, separate from the failed
wave-A run and its subsequent focused environment corrections.

The full gate uses the repository venv: CPython 3.11.14, NumPy 2.4.6,
google-crc32c 1.8.0, pytest 9.1.1, pytest-timeout 2.4.0 and psutil 7.2.2.
This environment is separate from the common latency interpreter below; both
arms of each timing experiment use the same interpreter and dependencies.

Ruff, generated API-reference verification and the documentation checker pass.
Consumer/public-adapter documentation tests pass: **19 passed, two attributed
optional-dependency skips**. Final smoke receipts are recorded in the
[round-closure report](PERF_V007_CLOSURE.md).

## Measurement boundary and provenance

All latency intervals use public `db.execute(...).rows`, including public query
execution and result materialization. Read-only disk handles use the existing
native fixture: 2,000 `Item` nodes with INT64 id/rank and STRING title. The seed was
created and closed/checkpointed in the earlier native campaign; there are no Pulse
imports, schema, adapters, data or services in these queries. Collection cases use
240 parameter rows. Expected rows/order are independently calculated and checked
after every timed interval; entity canonicalization is outside timing.

The common measurement interpreter is CPython 3.11.14, NumPy 2.4.4 and native
google-crc32c 1.8.0 on Windows 10.0.26200. The interpreter happens to reside in a
consumer development venv; imports are pinned to the selected Grafx source.
The engine SHA-256 is
`efd52b0c1ae56640bac446af9563009959bbd88381cbbe8dacc3e15c9984dda6`.
No heavy test or benchmark campaign ran concurrently with these latency runs.
The workstation was not certified idle; same-code controls expose meaningful
variation. These are small, warm-workload experiments, not production percentiles,
cold-start measurements, scaling studies or proof of platform-wide speedup.

Local evidence is retained under `.grafx-tmp/perf07/sort-b/`: source and instrument
hashes, commands, raw timings, independent answer oracles, copied boards, focused
and mutation logs/JUnit, and full-regression manifest/log/JUnit/exit status.
Databases and raw metrics are intentionally not committed. Reproduction requires
these retained instruments/seed; a clean checkout alone does not contain them.

## Independent-process comparison

Three arms: candidate `0919c06`, reference `7d52f8a`, and another invocation of the
candidate as the same-code control. Each arm has an independent copy of the same
disk board. Six blocks use every permutation of the three arms. Each fresh
process executes six passes per workload, discards pass zero and retains the
median of passes one through five. All **18 accepted processes exited zero** and
all result digests agree. The initial qualification invocation is excluded.

Ratios below are medians of six paired reference/candidate or control/candidate
ratios; greater than one favors candidate. Walls are medians of the six process
values in milliseconds. A ratio median is not the ratio of the wall medians.

| Native workload | Candidate ms | Reference ms | Reference/candidate | Same-code/candidate |
|---|---:|---:|---:|---:|
| Integer top-50 of 200 | 36.518 | 35.387 | 0.958 | 0.968 |
| Integer top-50 of 2,000 | 58.973 | 67.482 | 1.141 | 0.876 |
| Full integer order, 2,000 | 56.555 | 58.524 | 1.052 | 0.951 |
| String top-50 | 49.791 | 53.671 | 1.020 | 1.019 |
| Floating alias top-50 | 55.518 | 60.000 | 1.069 | 1.080 |
| Entity results, scalar keys | 52.004 | 55.769 | 1.031 | 0.955 |
| Entity identity top-50 | 39.059 | 37.691 | 0.957 | 1.028 |
| Indexed lookup control | 1.684 | 1.580 | 0.917 | 1.022 |
| Nested mixed values | 6.980 | 6.852 | 0.970 | 1.051 |
| Empty map keys | 8.345 | 8.186 | 0.947 | 0.983 |

For top-50 of 2,000 rows, the reference/candidate ratio is 1.141, but the separate
candidate control is also materially faster than candidate. Both optimized arms
beat reference in this workload; the 12.3% apparent reduction is not a defensible
causal estimate by itself. Unchanged indexed lookup also appears slower, confirming
that separate-process timing drift is material. This discrepancy motivated the
following additional comparison rather than selecting the most favorable run.

## Paired helper comparison within each process

Three fresh processes each alternate the exact original and new `_sort_key`
function bodies in the same engine module, public database handle and disk board.
All other runtime code stays at candidate. The original function is extracted
from Git object `7d52f8a`; no source file is edited. The control uses the identical
candidate function object. Switching functions and validating results happen
outside timing. Recursive calls resolve to the selected function.

Each process first runs the frozen public workload/oracle provider for
qualification and warmup, whose calibration timings are excluded. Each workload
then uses all six arm permutations, with three public queries per arm interval.
That gives 18 paired intervals per workload across three processes. All retained
result files contain every validated workload; run 2 also has an explicit exit-0
receipt. Runs 0 and 1 retain complete result files and logs but lack process-exit
receipts; their artifact completion is not represented as a captured exit code.

The table reports each process's median reference/candidate ratio, the pooled
median of all 18 ratios, and the pooled same-code control. Pooling describes this
sample; it does not make 18 independent machine-level experiments or establish a
confidence interval. The elapsed medians belong only to this instrument.

| Native workload | Candidate ms | Reference ms | Per-process ratios | Pooled ratio | Same-code ratio |
|---|---:|---:|---|---:|---:|
| Integer top-50 of 200 | 34.558 | 33.133 | 1.035 / 1.035 / 0.955 | 1.015 | 0.998 |
| Integer top-50 of 2,000 | 53.860 | 58.640 | 1.077 / 1.122 / 1.093 | 1.082 | 1.017 |
| Full integer order, 2,000 | 61.802 | 66.259 | 1.068 / 1.042 / 1.053 | 1.062 | 0.952 |
| String top-50 | 54.121 | 56.203 | 1.053 / 1.123 / 1.010 | 1.047 | 0.996 |
| Floating alias top-50 | 53.032 | 57.676 | 1.052 / 1.074 / 1.107 | 1.072 | 1.005 |
| Entity results, scalar keys | 48.082 | 52.048 | 1.096 / 1.075 / 1.123 | 1.081 | 1.015 |
| Entity identity top-50 | 35.573 | 35.517 | 0.986 / 0.968 / 1.037 | 0.986 | 1.020 |
| Indexed lookup control | 1.192 | 1.237 | 1.001 / 1.069 / 1.014 | 1.021 | 0.993 |
| Nested mixed values | 6.746 | 7.466 | 1.034 / 1.029 / 1.393 | 1.034 | 1.000 |
| Empty map keys | 7.128 | 7.738 | 1.028 / 1.019 / 1.043 | 1.025 | 1.009 |

The top-50/2,000 integer case improves in all three process medians, with ratios
1.077–1.122. The pooled ratio 1.082 corresponds to about **7.6% lower latency**
relative to the original helper in this sample. String keys, floating alias keys,
full integer ordering and entity results ordered by scalar keys also favor the
change in all three process medians. Their pooled apparent reductions are about
4.4–7.5%; the full-sort control itself differs by about 5%, so the exact full-sort
percentage is less well isolated. The top-200 case and entity-identity ordering
change direction across processes. The mixed-value case has one conspicuous
outlying process; it is retained, with no strong gain claim.

These observations support the mechanism's bounded usefulness. They do not
establish a gain for every type/size, exclude all non-scalar regressions, or measure
Pulse's resulting experience. Indexed lookup is a negative control, not an
optimization claim. Empty-map and entity-identity cases check non-scalar query
shapes; they do not cover every possible extension object or workload.

## Reproduction and remaining scope

Recorded driver commands for the original experiment layout are:

```powershell
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/sort-b/compare.py
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/sort-b/run_paired_final.py
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/sort-b/summarize_paired.py
.venv/Scripts/python.exe -u -B .grafx-tmp/perf07/sort-b/run_qualified_gate.py
```

The drivers pin revisions and refuse source drift or overwriting a completed
gate. After branch integration, these commands are not an in-place replay recipe:
prepare isolated worktrees at the named revisions and a new evidence directory,
then point a new driver at those paths. Preserve this round's instruments and
results. `source_manifest.json` freezes the first experiment. `paired_summary.json`
records the separate follow-up instrument/result hashes and the receipt limits;
`paired_2.receipt.json` holds its exact command. The paired helper instrument has
SHA-256 `93a57441bbf46269aed7dfa9f59b290cbd42231d8692dd54575fef3fbebd5acc`.
Run latency work before the heavy full gate. The latter uses pinned Core
`f602c7c` and Community `befaf1e` corpus objects, with both checkout paths explicitly
set; it does not substitute current consumer source for the frozen corpus.
It also requires the two retained historical wheel receipts described above.

This round addresses dispatch only. No claim is made that it closes the entire
v0.0.5 ordering gap, improves current Pulse requests, or implements READ-1 or M3.
The [roadmap](../../ROADMAP.md#next-iteration-assessment-featurev007) remains the
single execution backlog. Residual alias-metadata work needs a new profile and
bounded evidence before implementation; native relationship-anchor workloads
remain eligible independently of Pulse's current query shape.

The operator subsequently closed this implementation round. The
[round-closure report](PERF_V007_CLOSURE.md) consolidates a direct comparison of
final runtime against v0.0.6; the remaining leads above are future-iteration
decisions, not additional v0.0.7 work.
