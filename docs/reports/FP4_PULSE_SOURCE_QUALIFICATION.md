# FP-4 follow-up: paired Pulse source qualification

September 12, 2026. The initial qualification follows native SQ-13, not a new
Grafx feature round or release; that consumer follow-up did not change Grafx
runtime files. The separately recorded namespace follow-up below accompanies
subsequent Grafx maintenance-selector changes and has its own receipts.

## Sources and isolation

The tested Grafx source is the dirty `feature/v0.0.6` development checkout,
including the preceding [SQ-13 implementation and grouped
regression](../specs/WRITE_SUBQUERIES_V1.md#sq-13-implementation-and-qualification).
The paired consumers are:

| Component | Checkout / branch | Base commit |
| --- | --- | --- |
| Community | `okto-pulse-v003-kg-load-codex` / `fix/v0.3.3-grafx-transparent-recovery` | `a070e09b1a3e729360a4d5859f863a1952ff69f4` |
| Core | `okto-pulse-core-kg5-codex` / `perf/v0.3.3-kg-active-filter` | `33e3a5fe32c3c31c38ec6b4b2913e570ccba40a0` |

Community was modified locally as described below; Core remains unchanged.
Each pytest process uses a distinct temporary `DATA_DIR`, normal conftest and
explicit `OKTO_PULSE_CORE_REPO` / `OKTO_PULSE_COMMUNITY_REPO`. The final native
runs assert all three imported module paths belong to those source checkouts.
No global installation, live server, cognitive job, graph reset, commit or push
is part of this qualification.

## Compatibility changes

The initial 882-case Community run found **879 passes and three failures**:
two path consumers received the new native `PathValue` rather than a Pulse map;
one old projection-cleanup fixture tried to seed stored NaN, now correctly refused.
The nine new native-value baseline cases independently reproduced missing
node/path and temporal normalization. Baseline receipts are retained, not
relabelled successful.

Community adds `grafx_query_values.py`, using public Grafx DTOs/formatters only.
It preserves detached properties, qualifies IDs by database/table/kind and
record/provisional identity, retains pending provenance and renders six native
temporal types with nanosecond precision. Paths expose ordered `_NODES` / `_RELS`
lists. Reserved metadata cannot destroy colliding properties: `_PROPERTIES`
retains the original payload. Wide IDs remain JSON strings. TIMESTAMP conversion
uses exact integer microseconds, preserving its existing UTC text contract.

The existing path assertions now require the expanded qualified-ID contract.
The cleanup test seeds a valid finite edge and supplies an invalid NaN receipt;
it still verifies refusal before deletion and unchanged stored data. No durable
type restriction, native result identity rule, TCK case or expected error is
waived to make these tests pass.

Nineteen additional cases exercise native updating/unit UNION and correlated
CALL through `CommunityGrafxGraphTransaction`: commit/rollback, independent
reader isolation, ordered effects, late-statement failure preserving prior
work, fence refusal and rejection by the user read-only endpoint before database
resolution. The native implementation needs no adapter query evaluator or
Core capability tied to Grafx. No new Settings/configuration flag is introduced.

The Community contract is documented in `docs/GRAFX_NATIVE_RESULT_VALUES.md`
and indexed from its existing query-migration document.

## Qualification receipts

All **43 Community `test*grafx*.py` files**, sorted and split into alternating
selections, passed on final code: **912 distinct tests**, zero failures/errors/
skips and zero duplicate case identities between the two receipts. Both processes
reached terminal exit 0. The 61-case focused receipt overlaps this total; two
additional identity/reopen cases were added before the final grouped runs.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp4-pulse-community-final-0.xml` | 472 | 463.123 | `3f2e638498adbdbf0bd5f82c8f9c15dfa41b2f7d5b3d7261e1cadebae2a3cd5c` |
| `fp4-pulse-community-final-1.xml` | 440 | 466.626 | `35d8098f262b8e964b1f76e3706232c7ec51ff44f959d3c07aef727cb77e05cb` |
| `fp4-pulse-native-values-focused.xml` | 61 | 22.806 | `cf960dcd29870fc30cc12266794a1f701949716179b57f5b388be9c7013107aa` |
| `fp4-pulse-core-final.xml` | 111 | 58.742 | `6d4a0234e66e426f78066d6c4d74de054d242c70aaff8fa18e6b02d53d3c13c6` |

The final Core process also reached terminal exit 0, with no failures/errors/
skips, after the Community corrections. Its normal-conftest selection is
`test_kg_query_contract_v1.py`, `test_kg_tier_power.py`,
`test_async_graph_io_gate.py`, `test_kg_graph_io_cancellation.py`,
`test_card8_stale_sweep.py`, `test_cancellation_projection_utc.py` and
`test_logical_transfer_boundary.py` under Core's `tests/`. The earlier
`fp4-pulse-core-contracts.xml` is superseded by this final paired-code run, not
counted twice. Source-path assertions were enabled here as in both native shards.

Durations are JUnit suite durations per process, not additive elapsed time or
engine benchmarks. The first shard retains the existing `record_property` /
JUnit xunit2 warning, not a failure. The initial new write tests had an incorrectly
keyword-only fence double; fixing its signature to the actual port yielded all
19 passes, subsequently included in the final regression. Baselines and
intermediate receipts are not counted as additional passing coverage.

Reproduce from the Community checkout with source PYTHONPATH entries for Grafx,
the paired Community/Core and Grafx's optional test-dependency directory. Set the
two repository variables above and a fresh temporary `DATA_DIR`. For shard `s`
(0 or 1), use the sorted `rg --files tests -g 'test*grafx*.py'` file array and
select indices `s, s+2, ...`; execute `python -m pytest <files> -q --tb=short
--junitxml=<receipt>`. No `--noconftest`, expected-value rewrite, skip or timeout
increase is used. Production Core source remains unchanged.

The new Community normalizer's SHA-256 is
`a0ee22dba7ac5b0645a3130e2912bdf152c8049e7dc90df934aa84169a309db7`.
Generated API documentation, links/anchors, 39 configuration fields, 11 preserved
source plans, modified/new Python lint and whitespace checks pass.

## Namespace transfer follow-up

The same Community/Core checkouts were tested against the current Grafx source
with isolated temporary `DATA_DIR` and normal conftest. Imported module paths
were explicitly observed under all three checkout roots before the grouped run.
The adapter now qualifies every catalog lookup and paginated scan as node or
relationship and compares complete `(kind, name)` schema inventories. An extra
relationship sharing an expected node's name can no longer disappear from schema
validation. Endpoint ownership, bounded disk-backed maps and failure cleanup are
unchanged. Core, global installations and production data remain untouched.

The initial three-file source receipt
`.grafx-tmp/namespace-pulse-source-first.xml` passes **57 tests**, zero failures,
errors or skips, 13.929 s; SHA-256
`431c98c4037712466a284723d8a62e0f30540fa5e5b4296024591d17ef4bb081`.
It includes six same-name/unique-name endpoint-map success/failure cases and
explicit refusal of an extra same-spelling relationship.

The broader five-file receipt
`.grafx-tmp/namespace-pulse-source-regression.xml` records **97 passed, two
failed**, no errors/skips, 87.514 s; SHA-256
`ad690e45932f3e2b49492e4697685b860bad02a5593f2dc4cc6219513f033d90`.
One old scan double lacked `kind`; another attempted to create a dangling edge
using ordinary DELETE, which the engine now correctly rejects. The tests now
forward kind and assert DELETE refusal, then explicitly inject the missing
endpoint scan to preserve the adapter's integrity/cleanup test. Production
referential integrity was not weakened; the failed receipt is retained.

The corrected selection reached terminal exit 0:
`.grafx-tmp/namespace-pulse-source-final.xml`, **99 passed**, zero failures/errors/
skips, 80.987 s; SHA-256
`de4d48845e71dae8d15ef2aa26a1ada9714e78ef5e14c509672174f57a6edf2c`.
This includes complete board/global round trips and physical-transfer failure
paths. Counts overlap the 57-case receipt and must not be added to it.

Removing an unused local assignment in the physical round-trip test for lint
did not change its side effects. Both affected round trips were re-executed
afterward: `.grafx-tmp/namespace-pulse-roundtrip-lint-final.xml`, **2 passed**,
zero failures/errors/skips, 18 deselected, 34.120 s; SHA-256
`83f86521496abda5359b8162371336009eda479a7b7832d0e1ed01fb97c0622a`.
Grafx generated API/documentation validation (39 config fields, 11 preserved
plans), changed/new Python lint and both repositories' affected whitespace
checks pass. Core remains clean at the recorded commit.

The five files are `test_logical_transfer_grafx.py`,
`test_logical_transfer_schema.py`, `test_logical_transfer_values.py`,
`test_logical_transfer_factories.py` and
`test_logical_transfer_physical_matrix.py`. This is source-only affected-area
qualification, not a refresh of the earlier 912-test Community gate, frozen TCK,
installed-wheel or Pulse API/browser acceptance. Consumer documentation is
updated in `docs/GRAFX_NATIVE_RESULT_VALUES.md`; no Core backend reference or
new configuration/Settings option is introduced.

## Remaining acceptance

The later [general-MERGE follow-up](GENERAL_MERGE_PULSE_QUALIFICATION.md) adds
137-test affected source regression and 39 adapter/value cases against three
private installed wheels with package-origin/payload checks. That bounded
qualification supersedes the installed gap for those Python scenarios only;
the HTTP/browser/full-Pulse requirements below remain open.

This is source-level affected-adapter qualification, not all historical Pulse
tests, installed paired wheels, live HTTP/MCP or real-browser acceptance. Those
remain tracked by FP-8. Frozen-profile accounting and remaining FP-3 work are
not closed by these consumer tests. Nor does this report certify every possible
query scalar against every Pulse response schema. The shared Core conftest works
through the neutral schema port; historical modules with direct removed-helper
imports remain outside the selected Core contracts.
