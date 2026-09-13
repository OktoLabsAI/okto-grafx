# Bounded Grafx / Ladybug execution evidence

[Frozen references](../conformance/REFERENCE_VERSIONS_V1.json) ·
[Supplemental requirements](../conformance/EXTENSION_COVERAGE.md) ·
[Product comparison](../FEATURE_COMPARISON.md)

September 13, 2026. Sixteen bounded scenarios now execute on **Ladybug 0.20.3**
in a private Windows/CPython 3.13.1 environment. There are **7 matches, 9
differences and zero unavailable cases**. The existing installed Grafx 0.0.6
candidate passes all 16 independent expected results. The ordered case IDs,
query text and typed expected values were compared and are identical across
the two receipts. This is not full vendor/TCK conformance or a performance gate.
Neo4j execution remains unclosed; this report does not infer a Neo4j result.

## Observations, including differences

| Scenario | Ladybug 0.20.3 observation |
| --- | --- |
| NaN expression comparison | Matches: NaN equality evaluates False. |
| `lower` / `upper` / `trim` / `abs` | Matches exact string/integer results. |
| Entity identity | Matches four equal-property pairs but two DISTINCT entities. |
| Polymorphic OPTIONAL | Matches all four rows, including duplicate edges and NULL landing. |
| Date value | Matches exact 2024-02-29 date coordinates. |
| DECIMAL persistence | Matches exact 1.2500 under DECIMAL(12,4); explicit Ladybug CAST replaces the Grafx constructor, without a DOUBLE intermediate. |
| Nested persisted collection | Matches two native struct/map elements under the explicitly equivalent STRUCT[] versus LIST<STRUCT> declaration. |
| Hex/octal/chained expression | Parser refuses at `0x10`; that combined query does not separately prove support or refusal of the later expressions. |
| Unaliased `1 + 2` | Value 3 matches, but column name is `+(1,2)` instead of `1 + 2`. |
| Entity UNION in scoped CALL | Parser refuses `CALL ()`; no conclusion about standalone UNION entity identity follows. |
| Named `R*0..3` path | Different row multiplicity and zero-hop behavior on the fixed three-edge graph: 10 rows versus Grafx's seven trail rows. This is an observed query-semantic difference, not corruption or a speed comparison. |
| Multiple labels with SET/REMOVE | Parser refuses label SET; post-query node IDs remain 1, 2, 3. |
| NULL primary-key update/atomicity | Binder refuses any update of that primary key, rather than the expected NULL-specific error; both original rows retain value 7. Difference is error/admission policy, **not lost atomicity**. |
| Unit writing subquery | Parser refuses scoped CALL; T remains empty rather than receiving IDs 1 and 2. |
| Returning subquery with zero rows | Parser refuses scoped CALL; expected row-filtering contract does not execute. |
| Leading-WITH import | Parser refuses `CALL {`; expected imported result does not execute. |

The first two storage dialect adaptations were declared in the scenario definitions
before execution. The remaining queries were not rewritten after differences
appeared. Exact scalar types distinguish bool from int; DECIMAL never compares
through float. Multiplicity, columns, expected errors and declared post-error
effects are observed independently. A vendor difference is not a Grafx profile
waiver and does not reduce any required frozen case.

## Environment and artifact proof

Private root: `.grafx-tmp/fp-competitor-qualification`. No global package, system
PATH, registry, production Pulse or production graph changed. Each scenario owns
its own newly created local database, with 128 MiB buffer pool, two threads and
1 GiB maximum database size on the Ladybug side.

The first Ladybug receipt recorded 16 unavailable opens. Its native extension
could not load its OpenSSL 3 DLL dependencies. The successful run uses private
OpenSSL 3.5.8 x64 DLLs from the publisher's portable ZIP. The downloaded archive
matches the [publisher's SHA-256](https://kb.firedaemon.com/support/solutions/articles/4000121705-openssl-installers-zip-file-binary-distributions-for-microsoft-windows),
and both DLLs have valid FireDaemon Technologies Limited Authenticode signatures.
`os.add_dll_directory` scopes resolution to the test process; no DLL was renamed,
patched, copied into another application or installed system-wide.

| Artifact | SHA-256 |
| --- | --- |
| Ladybug 0.20.3 cp313 win_amd64 wheel, all 18 installed package payload files verified | `4be90b0e07b02b517406184f4b89fd2d3b8c70a95c7eff8f7e37215090a6c56e` |
| `wheels/openssl-3.5.8.zip` | `a5377866b476c1661f329d3f25fc35912fa5ef01e614043b9b9a27baf8dd76a4` |
| `libssl-3-x64.dll` | `cb278c68bc4dc13ef5a76486427553ac10153ba4c6915f4a09a11370020d8eb1` |
| `libcrypto-3-x64.dll` | `1e3f19a75809b4f45058a5296c094bef2a56f185dc120d2429d0696a3e71f454` |
| `ladybug-qualified/report.json` | `b9a0df57768ce215dd43a929f05706de0224b67ee92ef11a193b0271d44faee3` |
| `grafx-first/report.json` | `cf49b8a85d8223e092b44419fc1e4972d8cab12a11b63801a57e50b47d92f38d` |

The Grafx receipt uses wheel
`3fbd942891d8594feebe998bdddea55b6dd8fff801e004369761a7e2958dbb13`.
It precedes the final annotation/docstring/export-order corrective checkpoint;
it is not mislabeled as a byte-identical final wheel. Each receipt records its
own harness hash; the scenarios/expected observations are checked equal, not
assumed equal from file names. Final current-candidate qualification remains
subject to the full regression and packaging evidence.

## Reproduce without global installation

With the exact private Ladybug environment, downloaded/verified ZIP and wheel in
the paths above, the test-only launcher `.grafx-tmp/fp_ladybug_qualified.py` uses
`-I`, asserts interpreter origin and verifies the ZIP hash before opening the DLL
scope. It runs `tools/qualify_parity_references.py --engine ladybug --wheel <wheel>
--output <fresh-directory>`. Never overwrite a prior receipt or reuse its databases.
Launcher SHA-256: `4da1a3ef54b96fec8d89a33b8c017702d7a2c72bbb59dcd183b849413a72708b`.

The reference command exits zero when all scenarios execute, even when semantics
differ; the Grafx command requires every independent expected result to match.
This distinction is intentional and visible in the report's comparison column.
