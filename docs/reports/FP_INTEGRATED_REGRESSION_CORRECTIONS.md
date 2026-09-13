# Integrated final-regression corrective checkpoint

[Parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md) ·
[Required V3 query result](FP_V3_INTEGRATED_QUERY_QUALIFICATION.md) ·
[Pulse operational qualification](FP_PULSE_OPERATIONAL_QUALIFICATION.md)

The subsequent [complete final run](FP_FINAL_NATIVE_QUALIFICATION.md) passes
**25,077 tests**, with zero failures/errors and the same 19 skips. Its terminal
proof confirms all 1,307 inputs stayed unchanged. Final installed Pulse qualification
also passes; pinned Neo4j remains separate. The failed checkpoints below are
preserved as historical evidence, not current failures.

September 13, 2026. The earlier full repository run terminated normally with **25,053
passes, 24 failures, zero errors and 19 skips**. The JUnit inventory contains
25,096 entries, including one collection-level skip, and records 5,841.443 s.
The 18 test skips are Windows/POSIX-attributed cases with their declared platform
counterparts; the collection skip is the optional legacy Ladybug benchmark module,
absent from this Grafx test interpreter. No additional skip/xfail was introduced.

The full failed receipt is `.grafx-tmp/fp-integrated-full-regression-qualified.xml`,
SHA-256 `70befb787c877c94db2945c0c27dd671304b452128c014dcf3797be0a7425b55`.
Despite its historical filename, it **failed** and is retained unchanged.

## Diagnosed and corrected failure groups

| Original failures | Cause and correction |
| ---: | --- |
| 2 documentation consumers | The Arrow vector test selected a positional snippet that became a DATE recipe; it now executes the unique printed `ArrowVectorType` recipe. History documentation gained a fifth example; all five now execute, including an assertion against its printed membership helper. No public feature was removed to recover old snippet counts. |
| 1 hostile public-plan fixture | Native label admission now rejects a string subclass at TableDef construction. The test constructs an admitted table and injects the hostile returned name afterward, preserving the no-callback/owned-output boundary checks without weakening constructor admission. |
| 2 pinned Pulse corpus inventories | The already-regenerated, source-pinned observations now admit native REMOVE and one additional extracted query. Tests explicitly require 79 admitted/8 refused raw probes and 84 supported/11 gap entries, while preserving the public contract's `unsafe_cypher` refusal of REMOVE and the mismatched-name UNION refusal. No baseline query, source pin or read-only policy changed. |
| 3 packaging inventories | Native temporal support added the declared tzdata dependency, and native values/entities added 14 public exports. Both source and built-wheel dependency tests retain exact inventories; the public export list and its explicit test inventory now include and alphabetically order all names. |
| 8 checksum fixtures | Catalog mutation tests passed bytearray to a CRC port declared for bytes. Freeze their locally mutated buffers before calculating the independent checksum; corrupt capability/model admission still must refuse. Neither checksum implementation, corpus proof, storage format nor recovery check was changed. |
| 8 source-surface checks | Added missing annotations and descriptive docstrings to nested helpers and replaced one non-ASCII punctuation mark in a transfer docstring. Corrected the planner module's obsolete single-label/single-pattern description. No query, transaction, replay or storage algorithm changed in this corrective group. |

## Directed regression and remaining scope

The 13 affected test modules ran together: **3,566 passed / 3 failed**, zero
errors/skips, 284.354 s. The remaining failures exposed subsequent missing nested
helper annotations/docstrings in the same files because the original per-file
checks stop at their first failure. A complete AST inspection enumerated and
corrected the remaining helpers instead of changing or relaxing those checks.

The next surface/schema/node-label grouping passes **3,270 tests**, zero
failures/errors/skips, 65.486 s. The selected files include the whole annotation
and language-surface checks, typed collection schema and native label assignments.
Counts overlap; they must not be added to claim one larger regression. Together,
the corrective receipts cover every original failed test ID; they do not replace
the required final whole-repository run.

| Receipt (relative to `.grafx-tmp`) | SHA-256 |
| --- | --- |
| `fp-integrated-corrective.xml` (retained three-failure checkpoint) | `9ec1de95bae8c2b456d5062ac4da37d3070cf2357277acd0be2ffcca0d0a99a4` |
| `fp-integrated-surface-qualified.xml` | `03953c2221abbaaf2b6b24d08baeeaa52dd087ca1b19f0c3dc3ebf7d91dd5e2c1` |

Ruff passes for the changed package/test files. Documentation validation passes
links/anchors, configuration inventory, public signatures/DTOs and all 11 retained
source-plan archives; generated API-reference `--check` also passes.

The following full run uses a fresh receipt and freezes source, tests, tools and
documentation inputs for its duration. Any changed input invalidates its candidate
proof. Final installed-wheel refresh and pinned Neo4j execution remain separate;
neither the earlier 25,053 passes nor this corrective grouping is a final parity
claim. The active V3 ledger and its sole authorized lists-of-maps divergence are
unchanged, as are multi-reader/writer, snapshot/OCC, COMMIT proof and WAL durability.
