# 0.0.6 query-language round — acceptance record

Status: **all eight items implemented, tested and documented in development**.
Local acceptance completed September 10, 2026; not a publication or global install.

This record covers the fixed eight-item round on `feature/v0.0.6`, after
`afcfc9bc63264cbad83304c8bebb3746d5798779`. It does not redefine historical
acceptance records or certify the entire Cypher language.

## Implemented contracts and feature evidence

| Item | Implemented boundary | Principal executable evidence |
|---|---|---|
| 1 | Pinned openCypher 2024.3 inventory, exact feature hashes, explicit execution/parse distinction and unsupported cases | `tests/query/test_compatibility_profile.py`; `tools/check_opencypher.py` |
| 2 | Zero-based lists, case-sensitive maps, three-valued equality, lazy branches, exact numeric aggregation and scoped names | `test_compatibility_profile.py`, `test_composable_lists.py`, `test_list_iteration.py`, `test_query_memory_spill.py` |
| 3 | Ordered clauses, WITH scope/windows, UNWIND composition and shared execution budgets | `test_with.py`, `test_unwind.py`, `test_read_control.py`, `test_deferred_projection.py` |
| 4 | Typed correlated MATCH/OPTIONAL, whole-clause filtering/null extension and multiplicity | `test_optional_match.py`, `test_optional_aggregate_pipeline.py`, `test_optional_unused_landings.py` |
| 5 | Up to 64 UNION branches and explicitly scoped returning read subqueries | `test_union.py`, `test_union_all.py`, `test_composable_lists.py`, `tests/api/test_logical_views.py` |
| 6 | Documented scalar families, list-local expressions and bounded one-hop path components | `test_native_scalars.py`, `test_list_iteration.py`, `test_generic_path_projection.py`, `test_path_projection.py` |
| 7 | One atomic logical statement across private write phases; authenticated fresh endpoints, MERGE/SET read-your-own-writes, failure rollback and OCC | `test_fresh_endpoint_composition.py`, `test_composed_write_conflicts.py`, `test_read_your_own_writes.py`, `tests/txn/test_pending_relationship_endpoints.py` |
| 8 | Trusted typed tabular CALL/YIELD, declared permissions, exact value contracts, finite budgets and stream cleanup | `tests/api/test_tabular_procedures.py` |

Unqualified test files above are under `tests/query/`. Public consumption and
fixed limits are in [composable queries](../COMPOSABLE_QUERIES.md),
[query language](../QUERY_LANGUAGE.md), [configuration](../CONFIGURATION.md),
[API reference](../API_REFERENCE.md) and [extensions](../EXTENSIONS_AND_ARROW.md).

## Regression reconciliation

Environment: Windows 11, Python 3.13.1, default NumPy/CRC accelerators, optional
test dependencies from `.grafx-tmp/v006-optional-test-deps`. Both historical Pulse
baseline environment variables point to their immutable corpus checkouts. No
per-test timeout increase or new skip waiver was introduced.

The initial broad run began before the final corrections. Its result must not be
reported as a single clean run of the final source. Corrective runs retain the
original intended invariants and add positive coverage for deliberately replaced
policies: left-associative powers, zero-based indexing, native path output,
fresh relationship endpoints and connected-node DELETE refusal.

Concrete defects found and corrected include integer SUM precision loss, list
ordering through string representations, named/anonymous MERGE binding identity,
updates to properties of an uncommitted edge, statement rollback after a public
result failure, and cleanup behavior after unprovable rollback. Pending endpoint
updates preserve exact issued identities and original endpoints; they do not
permit endpoint reassignment or foreign/forged pending references.

The historical Pulse corpus was regenerated against the same pinned source SHAs.
All 97 callsites and 87 probe texts remain unchanged. One extracted query moves
from parse refusal to typed-plan refusal, unregistered CALL now refuses during
planning, and the historical UNION probe with `n.id` versus `m.id` becomes an
explicit output-name mismatch (78 engine-accepted / 9 refused probes). This is a
documented breaking policy, not a claim that the old mismatched query still works;
use matching aliases on each branch. Corpus refusal messages and its full-payload
digest were refreshed, without modifying historical Pulse source checkouts.

The **final complete run passed: 17,174 passed, zero failures/errors, 19 attributed
skips; 17,193 total records, 2,545.474 s (42 min 25 s)**. Its receipt is
`.grafx-tmp/language-final-full.xml`, exit code 0. Unlike the initial 18-failure
diagnostic, this is one complete clean run on the final implementation, not a
sum of overlapping corrective runs. The skip-attribution gate passed; the
18 POSIX-specific cases and one missing-Ladybug benchmark module are not claimed as successful tests or
as validation of POSIX/remote CI. No unresolved Grafx regression failure remains.

| Additional acceptance run | Result | Local receipt |
|---|---|---|
| Installed Grafx wheel: new functions, lists, procedures, atomic writes and conflicts | 150 passed, no failures/skips; 16.53 s | `language-wheel-features.xml` |
| Installed Grafx/Core wheels: actual Pulse owner queries | 4 passed, no failures/skips; 4.96 s | `language-wheel-pulse-final.xml` |
| Final-source affected Community integration | 191 passed, no failures/skips; 116.10 s | `language-pulse-release-candidate.xml` |
| Explicitly composed affected Core unit tests | 23 passed, no failures/skips; 2.43 s | `language-pulse-core-latest.xml` |
| Corrected corpus and public-definition documentation | 998 passed, no failures/skips; 229.22 s | `language-corpus-surface-final.xml` |

These runs overlap the source suite or test the same behavior in another package
environment; their counts must not be added as distinct coverage. Ruff over the
repository, documentation links/anchors, 39 configuration fields, public API/DTO
signatures, 11 preserved plans and whitespace checks passed.

The final run uses the complete suite, not a selection of previously failed tests:

```powershell
$env:PYTHONPATH = "src;.grafx-tmp/v006-optional-test-deps"
$env:PULSE_CORE_BASELINE = "D:/Projetos/Techridy/okto-pulse-core-corpus-baseline"
$env:PULSE_COMMUNITY_BASELINE = "D:/Projetos/Techridy/okto-pulse-community-corpus-baseline"
python -m pytest -q -x --tb=short --junitxml=.grafx-tmp/language-final-full.xml
```

The two baseline paths are machine-specific locations of the pinned checkouts,
not the active consumer worktrees. The optional-dependency path identifies this
machine's staged test dependencies; it is not a runtime Grafx requirement.

## Independent semantic reference

The pinned upstream revision is
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23` (220 feature files, 3,897 expanded
cases). The latest positive-expression diagnostic records **1,004 passed,
1,077 failed and 1,816 not run**. This command intentionally returns nonzero:

```console
python tools/check_opencypher.py --checkout .grafx-tmp/opencypher-2024.3 --execute-reads --feature-prefix expressions/ --output .grafx-tmp/opencypher-expressions-final.json
```

The [compatibility contract](../CYPHER_COMPATIBILITY.md) lists the missing syntax,
model divergences, finite-arithmetic policy, normalized column spelling and
unmeasured fixtures/error taxonomy. These failures are not converted to skips or
passes. Full Cypher, temporal constructors, unrestricted polymorphic patterns,
general named variable-length paths and writing/unit subqueries are not delivered
by this finite round.

## Pulse migration and packaging boundary

The active consumer pair is `okto-pulse-core-kg5-codex` and
`okto-pulse-v003-kg-load-codex`. Core's cancellation and stale-sweep source-owner
queries use provider-neutral `split` with positions `[0]` / `[1]`. No Grafx import
or provider-specific branch was added to Core. Community pins Grafx 0.0.6 and
documents the coordinated migration in `docs/GRAFX_V006_QUERY_MIGRATION.md`.

Four real-query tests passed against installed Grafx/Core wheels in an isolated
Python 3.13.1 environment; module paths were verified under that environment,
not the source checkouts or global installation. **150 feature tests** also passed
against the installed Grafx wheel (16.53 s, no failures/skips), covering native
functions/lists, composed fresh-endpoint writes, competing transactions and typed
procedures. All **208 Python files** matched source/wheel/installed bytes.
Wheel SHA-256: `74728ca0dcc93956b84717b46388fac9dad7a48a04a46cc0892fd9b027120f57`.
Paired local Core 0.3.3 wheel SHA-256:
`4ee30380a2ea36f080b2c73c17380fd380df4c5f8529aa7f5a7c7cd4d721d9a4`.
The local Community lock check passed using the paired wheel directory.
The final-source affected Community run passed **191 tests** in 116.10 s
(`language-pulse-release-candidate.xml`), without failures/skips. The installed
wheel's four owner-query tests passed separately (`language-wheel-pulse-final.xml`).
At this checkpoint, Core's shared bootstrap still imported removed Community
modules; its 23 affected unit tests used explicit CoreSettings and `--noconftest`.
The [consumer follow-up](PULSE_V006_INTEGRATION_REGRESSION.md) subsequently removed
that eager dependency and passed 81 Core tests with normal conftest, including
real routed natural search. Other historical test modules retain direct legacy
helper imports; neither checkpoint claims a full Core-suite acceptance.

No global Pulse install/restart, graph rebuild, production data change, PyPI
publication, commit or push is implied by this implementation record.
