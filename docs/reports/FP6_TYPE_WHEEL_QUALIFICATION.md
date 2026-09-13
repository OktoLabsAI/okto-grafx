# FP-5/FP-6 installed type-package checkpoint C

[Unified type support](../TYPE_SUPPORT.md) · [Upgrade procedure](../V006_COMPATIBILITY.md#combined-native-type-upgrade-procedure)
· [Parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md)

Status: **60 native installed scenarios and 30 logical-transfer scenarios passed**,
Windows/Python 3.13, isolated local environments. This closes the temporal/DECIMAL/
typed-collection installed-reader checkpoint for the exact candidates below.
The logical follow-up changes only the transfer error wrapper, with package-file
delta proof; it does not rerun or relabel the 60-cell native receipt. It is not a full Grafx regression,
cross-platform certification, final profile acceptance, release or Pulse install.
Later native multiple-label changes require their own affected format checks;
this report is not evidence for a layout that did not yet exist.

## Package identity and runtime isolation

Candidate: `.grafx-tmp/fp6-type-wheel-qualification/candidate/okto_grafx-0.0.6-py3-none-any.whl`.
SHA-256 `eda707afc626ac40cdc873f7d13b6ced37aee854c66b4d2f1d1659a2b09cfadc`.
All **250 package files**, not just Python modules, match both the installed venv
and `src/okto_grafx` at this checkpoint. Only bytecode/cache files are excluded
from package identity. Package versions alone cannot distinguish these development
builds, so every binary is identified by its wheel hash and imported origin.

Candidate venv: `.grafx-tmp/fp6-type-wheel-qualification/current`.
NumPy 2.5.3, google-crc32c 1.8.0 and tzdata 2026.3 are installed there. Workers
use `-I`, assert their selected installed package origin and never import Grafx
from the checkout. The pure profile selects pure codecs/checksums/vector math;
the accelerated profile selects NumPy and native CRC. **All recovery/readback
uses pure**, including a second verified checkpoint/reopen. Dependencies being
installed does not make the pure profile accelerated.

| Old installed binary | Known capability mask | Wheel SHA-256 |
| --- | ---: | --- |
| 0.0.5, `v005-final-dist` | 31 | `18bde32649aedce143bd083b5c6224347233074ea615b48350201ab2cacc28e8` |
| 0.0.6 before temporal values, `language-wheel` | 524287 | `74728ca0dcc93956b84717b46388fac9dad7a48a04a46cc0892fd9b027120f57` |
| 0.0.6 with temporal values, `fp5-wheel-qualification/candidate` | 16777215 | `2a79d9150075a810b22935dbad885015d8966e71fb90b669f9acba7bd365b5b6` |

All old installed package contents match their respective wheels too. The old
temporal venv was retained unchanged, not replaced by the new candidate.
No global environment, Pulse process or production database was modified.

## Executed matrix and independent boundaries

Every selected old binary crosses three states × two attempted modes:
materialized layout, durable-COMMIT/pending-page-application, and an already-open
old handle; then read or write. Each pair is run with pure and accelerated
candidate writes. The declared 60-cell matrix is complete without duplicate cells.

| Activated family | Old binaries | Pure | Accelerated | Total |
| --- | ---: | ---: | ---: | ---: |
| DECIMAL alone (bit 26) | Temporal-capable 0.0.6 | 6 | 6 | 12 |
| Typed collections without other new families (bit 27) | Temporal-capable 0.0.6 | 6 | 6 | 12 |
| Combined temporal/DECIMAL/typed collections/nested ANY (bits 23/26/27/20) | All three | 18 | 18 | 36 |
| Total | | 30 | 30 | **60** |

This intentionally tests each new bit independently against the immediately
preceding capable reader and the complete bundle against all three historical
binaries. It does **not** claim all historical versions × all individual bits.
Earlier standalone temporal, namespace and vector-owner receipts remain separate.

Each scenario proves:

1. The old binary creates an ordinary store. A candidate ordinary idempotent write
   without type activation remains readable by the old binary.
2. Candidate DDL/rows activate the required capability. The pending case cuts its
   own process with exit 73 after durable COMMIT but before page application.
3. The old binary refuses precisely: pending read uses `unsupported_operation`
   with `field=read_only_consistency`; other combinations use
   `schema_version_mismatch` with **exactly** the activated bits unknown to that
   binary. An unrelated error, a timeout, success or a different mask fails.
4. Every database file path and SHA-256 is identical before/after old refusal.
   There are no WAL/control exclusions; additions, removals and changes all fail.
5. The candidate recovers, checks exact schema/value coordinates, runs
   `verify("all")`, checkpoints and reopens again. Value bytes agree across old
   versions and pure/accelerated scenarios for each fixture.

Fixtures include DECIMAL(38,19) with maximal negative coefficient; int64 extremes,
nullable array slots, empty STRUCT, zero-length ARRAY and an all-NULL row; all six
temporal families with expanded years, nanoseconds, signed offsets, maximal months
and an unavailable recorded zone. A nested STRUCT combines MAP<DECIMAL>, LIST<DATE>
and MAP<ANY>, including mixed value types and native non-string ANY keys. This is
native persistence/readback evidence, not a claim that every external callback
accepts non-string map keys; see the support matrix.

## Reproduction

Build/install the desired candidate and historical wheels into **separate** venvs.
Use matching wheel/interpreter pairs and a fresh output directory for every run:

```powershell
python tools/qualify_temporal_wheels.py --current-python path/to/current/Scripts/python.exe --current-wheel path/to/current.whl --source-root . --old-python path/to/old/Scripts/python.exe --old-wheel path/to/old.whl --capability decimal --profile pure --output path/to/fresh-decimal-pure
```

Repeat for `--profile accelerated`, `--capability typed_collections`, and
`--capability type_bundle`; for the bundle repeat `--old-python`/`--old-wheel`
three times in matching order. Unix venvs use `bin/python`. The supplied script
does not install dependencies or remove retained stores. `--current-wheel`,
`--old-wheel` and `--source-root` were all supplied for this qualification; omitting
them cannot substantiate the same wheel/source provenance claim.

## Exact receipts and audit

All paths below are relative to `.grafx-tmp/fp6-type-wheel-qualification/`:

| Report | Passed cases | SHA-256 |
| --- | ---: | --- |
| `decimal-pure/report.json` | 6 | `c9960b4cec22ab58e72e8ba24dc3355b73f6da0620b9b4e539670563a2c452b6` |
| `decimal-accelerated/report.json` | 6 | `5f7c814eef5b9e06d76cacd92fcb449a58979012ba8bb8cf9572109d74e467f5` |
| `typed_collections-pure/report.json` | 6 | `6ef12532ff74e1746d4428ff8331725cc51fbd8980c5d3e26a2be4afb211a6a6` |
| `typed_collections-accelerated/report.json` | 6 | `99b3f45e6497474cb902893ba57c8662fb918da1fbcf6010cc4774e0855b5c0b` |
| `bundle-pure/report.json` | 18 | `cb4801ffc45b608e6120a0ecf456ce2491e69786208ae42f15e4339294f6a167` |
| `bundle-accelerated/report.json` | 18 | `94622eb57f89dbc044def1fa6af4ef896b034bff19fbe6da99cf44e8728633b3` |

A post-run audit rechecked complete unique cell coverage, report success, exact
unknown-bit refusals against each recorded known mask, all-file non-mutation,
package/wheel/source hashes, imported origins, exit-73 pending cuts and identical
pure readback values across profiles. All checks passed.

Verifier regression: `tests/tools/test_qualify_temporal_wheels.py` and
`tests/tools/test_qualify_type_wheels.py`, **81 passed**, 0 failures/errors/skips,
0.951 s. Receipt `.grafx-tmp/fp6-type-wheel-verifier-corrective.xml`, SHA-256
`b201a711c4368c183f9a631bab1c885add366dfaeeefb7a290f1a05b68c3a796`.
It tests exact and incorrect refusal masks, pending-read exception, current-reader
misclassification, fixture activation and package/source tampering. Initial receipt
`fp6-type-wheel-verifier-first.xml` retains the test collection import typo;
the test imported `_CAPABILITY_BITS` instead of `_CAPABILITY_TO_BIT`. It was fixed
before the successful corrective suite, not counted as a pass or hidden.

The prior native/text/columnar/procedure regressions remain linked in
[TYPE_SUPPORT.md](../TYPE_SUPPORT.md). Their overlapping counts are not added to
this installed matrix. Final full-profile/supplemental/competitor/Pulse acceptance
and release decisions remain separate work.

## Logical artifacts and the resumable error correction

`tools/qualify_transfer_wheels.py --types-only` adds the previously pending
installed logical-importer evidence: **30 terminal passing worker scenarios**,
three shapes × two profiles × five operations. Shapes are DECIMAL alone, typed
LIST<MAP<ANY>> alone, and combined DATE/DECIMAL/typed nested structures. Operations
are current export, current normal import, current resumable import, old normal
refusal and old resumable refusal. Every artifact contains two nodes, two parallel
edges and a self-loop; current imports preserve five mapped identities, exact
descriptors/values, a new database UUID, verification and two read-only reopens.
Resumable imports also repeat the completed call and require the identical receipt.
Export uses pure/accelerated selectors; import uses the public default target
configuration, followed by readback in the selected profile.

The old importer is the **temporal-capable 0.0.6** wheel identified above. It
accepts the existing format-1 envelope but does not understand DECIMAL or
`ColumnDef.stored_type`. Normal import returns `GrafxRecoveryRefused` with
`operation=logical_transfer`, `reason=artifact_invalid`, and the exact underlying
unknown-type/keyword cause. Its resumable path exposes the same raw `KeyError` or
`TypeError` before workspace creation. That legacy exception is **not** presented
as a clean typed refusal. The verifier records it separately, accepts only its
exact type/message in the archived resumable path and proves identical artifact
files/directories and an unchanged empty destination parent. Unrelated exceptions,
timeouts or any side effect fail the evidence. No archived wheel was patched.

The current source had the same missing normalization around `resume_import`.
`import_graph(..., resume_directory=...)` now wraps low-level malformed-artifact
exceptions in the same structured `GrafxRecoveryRefused(artifact_invalid)` as
normal import. Existing Grafx typed errors and OS errors retain their taxonomy;
there is no retry, data conversion, ignored error or changed publication protocol.
Eight source tests cover four malformed manifest shapes × normal/resumable modes,
checking exact error details and **all files/directories unchanged** before any
target/workspace creation. Existing crash-resume and value-consumer tests remain
in the grouped corrective regression.

Corrective candidate:
`.grafx-tmp/fp6-type-transfer-corrective/candidate/okto_grafx-0.0.6-py3-none-any.whl`,
SHA-256 `da881f537314e707266128a93556061c05b786da7a8d5a88ad11a463e3028305`.
Its isolated venv is `.grafx-tmp/fp6-type-transfer-corrective/current`; dependencies
match the native candidate. All 250 files match wheel, installed package and
current source. Compared with the native candidate above, **249 files are identical**
and only `transfer.py` differs:

- Before: `4d3e46dde9f01cd8058c95098d40294627edd57ba2e742c5a48c80e5ba49377d`.
- After: `29e5ad6acce5a517c83e8fdc87c70f4e0b5eda42464515e7fa9b937ec4828a67`.

Thus no engine, row, catalog, WAL, index, history or columnar code changed between
these installed matrices. The candidate delta is bounded to the documented
error contract. No global Pulse install or restart occurred.

```powershell
python tools/qualify_transfer_wheels.py --types-only --current-python path/to/current/Scripts/python.exe --old-python path/to/temporal-capable-old/Scripts/python.exe --output path/to/fresh-type-transfer-matrix
```

The complete corrective report is
`.grafx-tmp/fp6-type-transfer-corrective/matrix/report.json`, SHA-256
`c3a70d43d9e3ff9038ef18c41ffdb5c39b91d2ada205e245f11024fefef7f65b`.
A post-run audit proves all 30 unique cells, exact old errors and non-mutation,
current schema/value equality across profiles, all worker installed-package hashes,
and the one-file candidate delta. Unlike the native runner, this transfer runner
records installed hashes but does not itself accept wheel paths; the separate
audit compared them to the exact supplied wheels and checkout.

The first matrix `.grafx-tmp/fp6-type-transfer-qualification/report.json` is retained:
four successes followed by the archived raw-exception discovery, not a green run.
The initial combined regression receipt `fp-v3-type-transfer-regression.xml`
retains a test-tool indentation error during collection; it was corrected before
the corrective run, without changing runtime semantics or skipping affected tests.

Final grouped corrective regression: **558 passed**, zero failures/errors/skips,
203.217 s, `.grafx-tmp/fp-v3-type-transfer-regression-corrective.xml`, SHA-256
`c0f035693615e29cf496334dde65c628fc396c3e66d9632b1638f4bd49dcf22e`.
Selection: all `tests/tools/test_tck_*` and `test_qualify_*`, native ANY properties,
transfer resume (including real crash cuts and the eight malformed-manifest cases),
typed collection consumers and decimal transfer. This includes and supersedes the
earlier 434-test tool selection for the changed boundaries; counts overlap.
It is an affected regression, not an assertion that the full repository passed.

Retained failed artifacts: initial installed logical matrix SHA-256
`d85350e9790f8e52f8b2d7031b33f9ff8de3194819de38f0c538449254d6e6ed`;
initial collection-error regression SHA-256
`aba2b51a9f24bbff02731e25ddeb012051d85bf71f1b9b5a9ce172b43fa420a3`.

Executable documentation, parity annotation contracts and architecture/import
boundaries: **361 passed**, zero failures/errors/skips, 19.011 s,
`.grafx-tmp/fp-v3-type-contracts.xml`, SHA-256
`3f60f6ec513831a2f5a080284bf5297663dbf81f54ab7fb1286113389b063303`.
The documentation checker passes links/anchors, 39 configuration fields,
public signatures/DTOs and 11 preserved source plans. Changed-file Ruff and
`git diff --check` also pass. These source/documentation selections do not imply
an installed Pulse test or a new platform/performance result.
