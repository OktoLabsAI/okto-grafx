# Whole-pattern MERGE through Pulse: source and installed packages

September 12, 2026. Qualification of the existing
[general MERGE implementation](../specs/GENERAL_MERGE_V1.md), not a new feature
round, global upgrade, release or completed functional-parity claim.

## Tested boundary

New Community `tests/test_grafx_general_merge.py` exercises the actual
`CommunityGrafxGraphTransaction` and `CommunityGrafxCypherExecutor`, native stored
graphs and pure/NumPy codecs. Its nine cases cover package-origin admission,
complete three-node/two-edge MERGE repeated within one transaction, commit and
rollback, an independent reader's private-write isolation, JSON path conversion
and qualified endpoint identities, postcommit read-only result conversion, late
NULL rollback preserving a prior statement, and a lost writer fence.

The user read-only door specifically raises Core's `TierPowerError` with
`code="unsafe_cypher"` before resolving a database. This test cannot pass from
an unrelated TypeError or native execution failure. Native write support does
not grant UI query users write permission. No adapter-side graph evaluator,
backend-specific Core contract, new setting or recovery toggle was introduced.

No production implementation changed in either Pulse repository during this
follow-up. It uses the preceding Community value conversion and transaction
adapter changes. Core remains clean at
`33e3a5fe32c3c31c38ec6b4b2913e570ccba40a0` on
`perf/v0.3.3-kg-active-filter`. Community base is
`a070e09b1a3e729360a4d5859f863a1952ff69f4` on
`fix/v0.3.3-grafx-transparent-recovery`; Grafx is dirty `feature/v0.0.6` source.
Tests use fresh directories under Grafx's `.grafx-tmp`, not the user's DATA_DIR.

## Source regression

The normal Community conftest, explicit paired repository environment variables
and source PYTHONPATH are used. Package origins are asserted against all three
checkout roots. Five files are selected: `test_grafx_general_merge.py`,
`test_grafx_updating_subqueries.py`, `test_grafx_native_result_values.py`,
`test_grafx_graph_transaction.py` and `test_grafx_cypher_executor.py`.

The initial run passed **137 tests**, no failures/errors/skips, 62.694 seconds;
`.grafx-tmp/general-merge-pulse-source.xml`, SHA-256
`d09ba105b2632cd1261dbab469ef9ee71cbbc10ac3834f09aed4087bf6792506`.
It predates the strengthened exact read-only error and installed-origin assertion;
the final source rerun uses the same five-file selection. There were no test skips,
expected-value rewrites or production fixes needed to pass this integration.

The final source run also passes **137 tests**, no failures/errors/skips, 64.282
seconds, terminal exit 0: `.grafx-tmp/general-merge-pulse-source-final.xml`, SHA-256
`b8bac49371adfc5127764b23f2c187ef4f7e3426cac00c5b2e22d3c60b0571aa`.
It includes the stronger exact-error and package-origin checks. The earlier
source receipt is not additional distinct coverage. Generated Grafx API/docs
validation (39 settings and 11 preserved plans), affected Python lint and both
repositories' whitespace checks pass.

## Installed three-package qualification

The three current wheels were built from their checkouts and installed with
`--no-deps` into `.grafx-tmp/general-merge-wheels/venv`. The installed modules'
origins resolve exclusively under that environment's `Lib/site-packages`.
For each package, the complete set of Python source paths was compared to the
wheel, and every payload compared byte-for-byte to both checkout and installed
file. Wheel hashes, not the unchanged development version strings, identify these
specific artifacts:

| Wheel in `.grafx-tmp/general-merge-wheels/dist/` | Python files | SHA-256 |
| --- | ---: | --- |
| `okto_grafx-0.0.6-py3-none-any.whl` | 239 | `b2af1653f196c850d8818a0cf80e36b0d71f1b3390cab64794974034160e3553` |
| `okto_pulse-0.3.3-py3-none-any.whl` | 298 | `b736d90572976380c50793d214a66ecafc7d3d8c4c0e3837c2345c7465c43ff1` |
| `okto_pulse_core-0.3.3-py3-none-any.whl` | 760 | `54c1c2e81b2170fde44bd0bb0efb06c2151092125a08c90d43b118cc5425c43b` |

Installed qualification passes **39 tests**, no failures/errors/skips, 27.815
seconds, terminal exit 0. Receipt `.grafx-tmp/general-merge-pulse-installed.xml`,
SHA-256 `5d9f3d6f725a4b6bcc05ae5a5f49ee20fa22ee3275e95f47a7d6c316c3e58569`.
These are the new general-MERGE file plus native-result and updating-subquery
files. Their test sources are collected with `--import-mode=importlib` and
`--noconftest`: deliberately no repository bootstrap may replace installed
packages with source paths. Package origins are asserted inside this run, not
inferred from the environment name. Source and installed counts overlap.

This is **package-isolated, not dependency-hermetic** qualification. CPython 3.13.1
starts with `-I`; the user's existing site-packages directory is explicitly
appended after the private environment for test/runtime dependencies only. Native
packages remain the three private installs, as checked above. Observed dependencies:
NumPy 2.5.2, google-crc32c 1.8.0, pytest 8.3.4, pytest-asyncio 0.25.3, Pydantic
2.13.5 and SQLAlchemy 2.0.49. The first `-I -m pytest` attempt lacked pytest and
terminated before collection; adding the dependency path did not change engine,
adapter or test semantics. No failed test run is presented as passed.

## Limits and remaining acceptance

Follow-up: [installed HTTP/UI qualification](PULSE_HTTP_UI_PARITY_QUALIFICATION.md)
now covers bounded graph pagination and logical edge reads with a corrected
Community wheel. The artifact and scope claims below describe this earlier run.

This qualifies Python adapter consumption and JSON envelopes of installed wheels.
It is not an HTTP/MCP request, full Pulse application startup or real-browser
test, and does not refresh every historical Pulse test. Those requirements and
the full frozen profile remain in FP-8. No installed/global Pulse was stopped,
restarted or upgraded; no production graph, SQLite, consolidation or backfill was
touched. No commit, push or release was performed. Label scope and the stored-map
oracle decision remain unresolved; this evidence does not waive them.
