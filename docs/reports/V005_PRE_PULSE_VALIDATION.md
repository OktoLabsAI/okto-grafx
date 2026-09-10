# 0.0.5 pre-Pulse candidate validation

Date: September 9, 2026. Candidate: `feature/v0.0.5` at
`8c3f6f2a6be8c8bdcdd15113fd5f37b2b965a173` (implementation commit `65ab659`).
Local platform: Windows 11 build 26200, Python 3.13.1.

**Passed for the next controlled Pulse integration test:** the complete final
regression passed with 16,228 tests, 18 attributed POSIX-only skips and zero
failures/errors; pytest exit status 0. Product evolution is paused at the user's
request. This check does not install into Pulse, access its
data, activate database capabilities there or approve publication. All databases
created by this validation are temporary test stores.

## Candidate and artifact identity

Local branch and remote branch both resolved to the candidate above. No tracked
implementation changes were present. Existing unrelated untracked files were
preserved and excluded from the build. Artifacts were built from `git archive`
of the exact commit, not a directory containing old build outputs.

Local evidence/artifact root: `.grafx-tmp/pre-pulse-8c3f6f2/`.

| Artifact | SHA-256 |
| --- | --- |
| `dist/okto_grafx-0.0.5-py3-none-any.whl` | `5395fdd8d9619342c0c35e952b5b1422f810ef18c978a0d39ad3614d6718f529` |
| `dist/okto_grafx-0.0.5.tar.gz` | `15ed64701bde13727e64ba7b0bebe58300aa600aea8dab8d41d388123292c6fa` |

All 186 packaged files (185 Python files and `py.typed`) matched the committed
source archive byte-for-byte. A separate installed-wheel check matched those same
bytes against `site-packages`. Import provenance was asserted outside the checkout.
The wheel was built from the generated sdist by the normal isolated build flow.

## Completed checks

- Full-repository Ruff: passed. API reference generation check,
  documentation links/anchors/configuration/DTO coverage and `git diff --check`:
  passed. This is not a new full-project static typing certification.
- Isolated build of wheel and sdist: passed. Twine metadata validation: both
  passed. Setuptools emitted a non-fatal deprecation warning for the license TOML
  table; no license terms or packaging configuration were changed in this check.
- Separate fresh wheel and sdist environments, no optional dependencies initially:
  install, `pip check` and `tests/consumer/smoke.py` with `-I` passed.
  The installed sdist's same 186 package files also matched the candidate wheel.
- Bare installed wheel: `tools/check_installed_eight_followup.py` passed migration
  resumption/dry-run, nodes/edges, durable FTS, incident hybrid retrieval, cancelled
  vector search, index rehash, backup/restore and cold `verify('all')`.
- Wheel environment then installed declared acceleration/interoperability extras:
  NumPy 2.5.2, google-crc32c 1.8.0, Arrow 19.0.1, Pandas 2.2.3, Polars 1.44.2 and
  NetworkX 3.6.1. `pip check` remained clean. Pulse/global packages were not changed.
- Five installed documentation examples from FTS, hybrid search, logical transfer
  and schema migrations passed against the exact committed docs and wheel.
- Eight continuation recipe tests plus three compatibility tests passed against
  the installed wheel: **11 passed / 12.34 s**. All loaded Grafx modules remained
  inside the isolated environment. This includes Polars, CSV/JSONL, graph exchange,
  prepared algorithms, Arrow/vector and Pandas/Parquet examples; strict/generation
  descriptors with independent readers/writers; accelerated write/pure reopen.
  Report: `installed-recipes-configured.xml`.
- Strict mypy installed-consumer smoke: no issues in the one consumer source file.
  Installed `oktografx --help` returned successfully.
- Real 0.0.4 wheel upgrade matrix: all four existing cases passed against current
  0.0.5 source. Default data remained readable by 0.0.4 after a 0.0.5 append; sparse
  hash, FTS statistics history and free-page-index opt-ins correctly caused old
  reader refusal. Legacy wheel hash:
  `bb44971934a5cadb77e1621e2ad202451a7450f277240ec86e55562aa763a532`.
  Command: `python tools/check_v005_upgrade.py --legacy-wheel <tag-build-wheel>`.
  This four-case matrix is not evidence of every possible downgrade/capability.

## Full regression — completed

The initial two disjoint groups covered the complete `tests` directory: core is everything
except `tests/api`, `tests/corpus`, `tests/tools`; the other group covers exactly
those three directories. No performance threshold, test timeout or skip policy
was relaxed. Full-suite tests use source from the named candidate, not wheel
imports; installed-consumer evidence above is separate.

Environment: `PYTHONPATH=<repo>/src;<repo>/.grafx-tmp/round-a4-optional` (the latter
provides Polars); core/community corpus baseline variables point to verified
local checkouts of `f602c7cc2f6a9f5ef446d4c991309196bd4667c7` and
`befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595`, respectively.

Both initial groups timed out on physical barriers with test stores on D:. The
serial API retry on D: also timed out (Arrow import, writer-lease publication,
`os.fsync`), so concurrency between suites alone does not explain the failure.
Those incomplete invocations are not acceptance and produce no complete JUnit.

A fresh single-process whole-suite invocation used a previously nonexistent
private basetemp on C:, without changing any test or timeout. Command:

```text
python -u -m pytest tests -vv -p no:cacheprovider -o junit_family=xunit1
  --junitxml=<local-temp>/grafx-pre-pulse-8c3f6f2-full.xml
  --basetemp=<local-temp>/grafx-pre-pulse-8c3f6f2-full --durations=20
```

Final report: `full.xml`, with `full.log`, copied into the evidence root from local
Temp. **16,246 collected; 16,228 passed; 18 skipped; 0 failures; 0 errors.**
Pytest process exit status: **0**. Wall time reported by pytest: **2570.18 seconds
(42 min 50 s)**; JUnit suite time: 2569.063 seconds. This is one complete successful
invocation, not a merge of partial results. No code changes were made between the
candidate commit and this execution.

All 18 skips are source-attributed POSIX cases (FIFO probes, flock, directory
fsync, symlink/namespace replacement and open-file unlink semantics). No optional
feature was skipped for a missing dependency. This proves local coverage under
the installed profile, not that the POSIX counterparts were executed here.
Do not count incomplete invocations or add overlapping installed/focused checks
to the distinct full-regression total. The longest reported case was a concurrent
thread test at 40.98 seconds; the configured timeouts were retained throughout.

### Filesystem observations and focused replay

Three private-file 512-byte barriers on each volume, without Grafx, measured:

| Temporary location | Three fsync samples, milliseconds |
| --- | --- |
| C: user Temp | 1.7304, 0.1928, 0.1832 |
| D: candidate evidence directory | 2813.2473, 51.1639, 4588.9531 |

These tiny observations are diagnostic, not a disk benchmark or proof of the
underlying hardware/driver/filter cause. A separate point-in-time observation
showed high antivirus activity. Both disks reported Healthy/Online; that does not
exclude intermittent latency. No antivirus exclusion, durability bypass or
system setting was applied. D: storage performance remains an operational caveat.

The two identified timeout fixtures were rerun on C: with unchanged 60-second
limits: **3 passed / 1.19 s** (`fsync-cases-c.xml`). Calls took 0.44 s and 0.33 s
for commit-history metrics on/off and 0.14 s for the stale-primary-index case.
These cases also passed in the complete final C: regression. The measured
volume-dependent latency, not a code or timeout modification, is the distinction
between the interrupted attempts and final successful execution.

## Initial attempts and diagnosis

- First API/corpus/tools invocation failed at collection because baseline-path
  environment variables were absent. Correct pinned checkouts were found and
  verified; this was test setup, not a graph query failure.
- Configured API group later hit the existing 60-second per-test guard while the
  core suite and other validation work were active. The stack ended in
  `storage_local.durable_barrier -> os.fsync`, during COMMIT control bootstrap.
  The newest fixture was `test_metrics_parity_and_full_verify[True]`. This attempt
  is failed/incomplete, not acceptance. The core group subsequently timed out at
  `coordination_local._publish_once -> os.fsync` while refreshing a reader, with
  fixture `test_a_stale_index_is_never_planned_and_the_answer_stays_right` active.
  The serial retry and C: observations above narrow this to volume-dependent
  physical-barrier latency; they do not establish a specific driver/hardware cause.
- An older helper, `tools/check_installed_search_followup.py`, was initially
  invoked but compares against a hardcoded wheel from an earlier delivery. Its
  byte-parity assertion failed against this newer candidate. The independent
  check was corrected to bind the exact candidate archive/wheel/install and then
  passed all 186 files plus the five documentation examples. No runtime code was
  changed to accommodate this stale helper.
- The first isolated recipe invocation passed seven cases and failed to import
  the test-only `tests.*` graph fixture for one case. The replacement harness adds
  only the committed repository root for those helpers, not `src`; all 11 recipe
  and compatibility cases then passed, with installed module origins checked.

## Limits and next step

This is local Windows/Python 3.13 validation, not remote CI, minimum optional
dependency versions or cross-platform certification. The result qualifies this
exact artifact for a **controlled Pulse installation and integration test**,
not certification of all Pulse UI/workers on a database not yet exercised here.
Before that installation, retain a recoverable copy of the live data and record
the installed Pulse/Grafx identities. Enabling new persistent capabilities may
prevent downgrade; replacing the wheel alone is not a general data rollback.
Final wheel/sdist hashes were rechecked after the regression and remained equal
to the table above. The artifact for that next step is the validated local wheel
with `[accel]`; there has been no PyPI publication or global/Pulse installation.
