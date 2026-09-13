# Functional parity: final native and consumer qualification

[Delivery status](../../ROADMAP.md#functional-parity-expansion-plan) ·
[Fixed scope](../specs/FUNCTIONAL_PARITY_PLAN.md) ·
[22 supplemental contracts](../conformance/EXTENSION_COVERAGE.md)

September 13, 2026. **The complete native regression passes.** The frozen required
query profile, mapped native supplemental contracts and isolated installed Pulse
flows are qualified. **This delivery is complete with the user's explicit Neo4j
deferral** (`FP-NEO4J-DEFERRED-20260913`). Comparative execution was not performed
and is not counted as passed. This is 0.0.6 development evidence, not a
release, global installation, full upstream conformance or all-vendor parity claim.

## Complete repository regression

The terminal run of `python -m pytest tests --tb=short` has **25,077 passes,
zero failures/errors and 19 skips**. JUnit records 25,096 entries and 5,516.265 s;
the pytest console records 5,520.09 s including its wider reporting boundary.
The launcher hashed **1,307 inputs** in source, tests, tools, docs and packaging
before/after: terminal exit 0, `inputs_unchanged=true`, `changed_inputs=[]`.

This is a new complete run, not the sum of focused selections. The
[earlier 24-failure run and corrections](FP_INTEGRATED_REGRESSION_CORRECTIONS.md)
are retained unchanged. No test selection, skip/xfail policy, upstream oracle,
transaction policy or durability contract was relaxed to obtain the final result.

Environment: Windows, CPython 3.13.1; `PYTHONPATH` includes `src` followed by
`.grafx-tmp/v006-optional-test-deps`. The immutable Pulse corpus paths are
`D:/Projetos/Techridy/okto-pulse-community-corpus-baseline` and
`D:/Projetos/Techridy/okto-pulse-core-corpus-baseline`, supplied through
`PULSE_COMMUNITY_BASELINE` and `PULSE_CORE_BASELINE`. This is not a Linux run or a
claim about untested optional/dependency combinations.

### Skips and warnings

Every skipped test ID and reason equals the preceding complete run's inventory:
18 POSIX-specific namespace/locking/directory-barrier cases on Windows, and one
collection-level optional Ladybug benchmark skip in this Grafx interpreter.
Windows counterpart tests run normally. These skips are not hidden parity cases;
the separate pinned Ladybug comparison uses its own verified private environment.
No POSIX success is inferred from the Windows counterparts.

The 346 warnings come from the typed-collection Polars tests: Polars announces a
future `explode(empty_as_null=...)` default change. Current collection round trips
pass, including empty/NULL parents. The conversion filters empty/NULL parent lists
before exploding and reconstructs their offsets/masks. This warning is recorded,
not silently suppressed or treated as qualification of future Polars 2.0.

## Fixed plan reconciliation

The rows below locate evidence for the **original** packages; they do not replace
their detailed type, authority and resource contracts with a smaller test profile.

| Package | Implemented capability and authoritative qualification |
| --- | --- |
| FP-1 | All 3,897 source-bound cases accounted for, original queries/oracles retained; [V3](../conformance/PROFILE_V3.md) requires 3,896 and records only Set1 #0010 as divergent. Stateful runner tests are included in the complete regression. |
| FP-2 | Scalar numeric/postfix syntax, chained comparisons, volatile `rand`, faithful headings and expression NaN; [query reference](../QUERY_LANGUAGE.md) and [complete original-case execution](FP_V3_INTEGRATED_QUERY_QUALIFICATION.md). Stored nonfinite values still refuse. |
| FP-3 | Qualified node/edge/path identity, composed polymorphic/optional patterns and bounded trails; native flexible/ANY models, unlabeled/multilabel nodes and qualified consumers. [Entity contract](../ENTITY_VALUES.md), [label format and installed readers](FP_NODE_LABEL_WHEEL_QUALIFICATION.md), supplemental rows 1–5. |
| FP-4 | Returning/unit writing calls, explicit/star/leading-WITH scopes and updating UNION share native statement rollback. [Write contract](../COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries), supplemental rows 6–11, including independent participants and actual COMMIT/recovery cuts. No inner commits. |
| FP-5 | Six exact temporal families, constructors/operations and persistence; [public temporal contract](../TEMPORAL_VALUES.md), supplemental rows 12–13 and [installed combined type qualification](FP6_TYPE_WHEEL_QUALIFICATION.md). |
| FP-6 | Exact DECIMAL and owned typed LIST/MAP/ARRAY/STRUCT, native/JSON/text/columnar/history/copy consumers; [support/refusal matrix](../TYPE_SUPPORT.md), supplemental rows 14–17, installed type/label readers and [coordinated admission](../V006_COMPATIBILITY.md). Unsupported key/index combinations remain explicit, not silently coerced. |
| FP-7 | Trusted typed read/write/unit procedures, native entity/value signatures, nested query/schema authority and shared budgets; [permissions/lifecycle](../EXTENSIONS_AND_ARROW.md), supplemental rows 18–20. No retained capability, implicit callback retry, sandbox or external-effect rollback promise. |
| FP-8 | Native regression/profile/supplemental reconciliation and [final installed Pulse](FP_FINAL_PULSE_QUALIFICATION.md) qualify their scopes. [16 pinned Ladybug observations](FP_BOUNDED_LADYBUG_COMPARISON.md) complete; Neo4j execution explicitly deferred by the user, not counted as passed. Public docs and representative [current cost observations](FP_NATIVE_COST_OBSERVATIONS.md) describe limits without performance gates. |

The required query result is **3,896 passed / zero failed / zero not run**.
The unchanged upstream result is **3,896 passed / one failed / zero not run**:
Set1 #0010 expects a runtime property-type error for lists of maps, while the user
explicitly retained their native storage. Its expected error is still executed
and recorded, not rewritten as an upstream pass. All former multilabel divergences
are now required and passed. This is not full openCypher conformance.

### Supplemental outcome reconciliation

The immutable 22-ID inventory retains SHA-256
`b87801ba4952bbe9d4921edbf7aeea84f1ef8bea54fb40f9b4d50d1be026f3a0`.
All **20 native contract maps** resolve to passing tests in the qualified complete
run, spanning **51 unique modules**. Reconciliation checks current test-function
presence, individual parameterized outcomes, module hashes against the frozen
run and the JUnit/proof binding. Overlapping module counts must not be summed.

Assertion review additionally checked entity store/table/kind/incarnation and
old snapshots; trails against an independent edge-subset oracle; all-invocation
write rollback and pre/post-COMMIT cuts; exact temporal/collection reopen, backup
and history; independent procedure readers/writers and expired/denied authority.
Passing paths/counts alone are not the evidence for those semantics.

Rows 21/22 stay external in the reconciliation tool. Row 21 has the separate
installed/API/browser/MCP evidence linked above. Row 22 has Ladybug observations
but no Neo4j execution. Native counts cannot make either external row pass.

## Exact receipts and reproduction

Paths are relative to `.grafx-tmp/`; these local artifacts may not ship in a clone.
The tracked reports preserve their identity and the tools preserve reproduction.

| Receipt | SHA-256 |
| --- | --- |
| `fp-integrated-full-regression-corrected.xml` | `6942f440cdafe6e691234b3936abfacd5b57ca7edb219c2eb323029a0f4b5dcd` |
| `fp-integrated-full-regression-corrected-proof.json` | `0e288950d7307f65a9f761fd17408aae474e71fe26721324c3af59319025b0f8` |
| `fp-extensions-final-native-reconciliation.json` | `46de0996c8a44db61c5852b58a2d7701fe935b27fa782afd6bc6f370b445c801` |
| `fp-multilabel-integrated-v3.json` | `9aed1496d346d23dd9a140fb55ae4549d6d31eedd695ef35bc0ae92ef87e20a9` |

Run the complete pytest command with the environment above and a **fresh** JUnit
path. Reconcile the native contracts with [the tracked tool](../../tools/fp_reconcile_extensions.py):

```powershell
python tools/fp_reconcile_extensions.py --junit .grafx-tmp/fp-integrated-full-regression-corrected.xml --proof .grafx-tmp/fp-integrated-full-regression-corrected-proof.json --output .grafx-tmp/fp-extensions-recheck.json
```

The output path must not already exist. Without the terminal bound input proof,
the tool reports inventory/outcomes but cannot label them qualified-current.
Documentation was reconciled **after** the full run's input freeze ended. Runtime
source and tests remain the qualified payload; prose/tool-only follow-up checks
are separate from that full-run receipt. Do not claim that a later wheel's README
metadata bytes are the same as the already-qualified wheel.

### Post-freeze documentation and tooling checks

The final documentation reconciliation passes the link/anchor checker (including
the new reports and all changed capability entry points), the exact 39 non-path
configuration fields, public signatures/DTOs, 11 preserved plan archives and the
generated API-reference `--check`. Pulse's Settings inventory counts **40** fields
because it also includes `path`; these are not conflicting inventories.

The consumer/continuation/history/public-adapter documentation grouping passes
**30 tests**, zero failures/errors/skips, JUnit 10.439 s. Receipt
`fp-final-documentation-qualification.xml`, SHA-256
`598a22318ad3146d5b55a0bae83767036f114be344f5bb0719ae91b4393195f6`.
The test launcher also reports an existing pytest-asyncio future-default warning;
no event-loop policy was changed by this documentation follow-up.

Tracked observer tools pass Ruff. The pinned Neo4j driver environment runs all
**11 offline observer tests** successfully (0.323 s); its manifest generation
reproduces the same 16-case manifest hash. The driver's test-only error fixtures
emit its documented internal-helper deprecation warning; these are not engine
observations. The native cost tool has identical bytes to the measured script
and its private-installed CLI help succeeds without starting a sample.

The final documentation-map reconciliation also retains 22 IDs, with every native
row passing and both external rows explicitly requiring separate evidence:
`fp-extensions-documentation-reconciliation.json`, SHA-256
`85a21fa78b5b80be327586a0a662e93708124d3e48ebd0fe4238ba0dae1f3cea`.
Its documentation hash differs from the pre-edit map, while the requirements,
native module hashes and complete-regression receipt remain unchanged.

A final hash comparison confirms all **251 Grafx package payload files** still
match both the full-run source candidate and the private installed wheel. No
runtime or native test input was changed during this documentation/tool promotion.
These checks are deliberately separate from the complete 25,077-test run, not
additional counts added to it or a relabeling of the historical failed run.

## Deferred comparative work — not a delivery condition

The user explicitly decided not to perform the proposed Neo4j run now on September
13, 2026. [Recorded scope decision](../specs/FUNCTIONAL_PARITY_PLAN.md#delivery-decision-defer-neo4j-execution).
The 16 prepared scenarios and fixed version remain available for future comparison,
with no Docker startup or engine result. This removes only the external comparison
from current acceptance; no required native case, Pulse check or invariant changes.
The implemented local-first profile, tested consumers and documentation are
delivered. Full upstream conformance and Neo4j behavioral parity remain unclaimed.
