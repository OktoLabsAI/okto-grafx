# Okto Grafx roadmap

**Single active product backlog — reconciled September 13, 2026; baseline updated
September 14, 2026.** Published/main baseline: `0.0.6`, tag `v0.0.6`, main merge
`1e01be5ae142eb3aa51cb97e8dff6cdd3741dbba`, published on PyPI on September 13, 2026.
The `feature/v0.0.6` MP-1–MP-8 checkpoint and the authorized native-history/catalog/search
follow-up shipped in that release. Development continues on `feature/v0.0.7` with the
source version bumped to 0.0.7 and no functional change yet. Latest isolated native/Pulse
observations use 0.0.6; the latest production spec-consolidation sample uses
`0.0.4@fa8f188`. See [performance](docs/PERFORMANCE.md).

This roadmap includes **new capabilities, corrective work, known limitations,
operational hardening and developer experience**. It replaces the execution
authority of the former evolution/agent/performance/round plans. It does not
reopen completed work, authorize production data changes or imply release approval.

## Current functional-parity acceptance

The [final complete native regression](docs/reports/FP_FINAL_NATIVE_QUALIFICATION.md)
passes **25,077 tests**, zero failures/errors, with the same 19 attributed skips.
All 1,307 inputs were unchanged during the run. All **3,896 required V3 cases**
pass; Set1 #0010 remains the sole authorized upstream divergence for native lists
of maps. All **20 native supplemental maps** pass on that qualified run.

The [final privately installed Pulse candidate](docs/reports/FP_FINAL_PULSE_QUALIFICATION.md)
passes 158 installed tests and actual API/UI/MCP checks, including 500 + 11 unique
KG nodes, exact types, search/key decisions, Settings/schema and recovered data.
The preceding live fault-to-next-ordinary-writer sequence proves automatic native
recovery without restarting Pulse during that sequence. Core remains agnostic;
no global installation or production-data mutation was used for this acceptance.

FP-1–FP-7 are implemented and natively qualified within their published contracts.
FP-8 is **complete for this delivery**. On September 13 the user explicitly
deferred the proposed Neo4j execution (`FP-NEO4J-DEFERRED-20260913`); it is a future
comparative task, not a current acceptance gate. Keep its pins/tooling and
unexecuted status, without starting Docker or claiming Neo4j equivalence.
The 16-case Ladybug run is complete (seven matches, nine documented differences).
No native capability, required test, invariant or documentation requirement was
waived. [Scope decision](docs/specs/FUNCTIONAL_PARITY_PLAN.md#delivery-decision-defer-neo4j-execution).

Current measured costs and the confirmed scalar-driving-key optimization limit
are [documented](docs/reports/FP_NATIVE_COST_OBSERVATIONS.md); the latter is a future
performance item, not reopened scope. Current docs are checked separately after
the full regression's input freeze; runtime/tests remain the qualified payload.
Documentation links/configuration/API checks, 30 executable documentation tests,
11 offline comparative-observer tests and tool lint now pass. The latter are not
Neo4j engine results. All 251 Grafx package files still match the privately
installed qualified wheel; there is no remaining native/doc implementation gate.

## Historical functional-parity checkpoint trail

The chronological paragraphs below retain each original candidate's observations.
Their former "pending", "failed" and "latest" descriptions are superseded for
current status by the acceptance section above and package table below. Historical
failed receipts remain failed; they are not additional open copies of the work.

Current authorized scope: [profile V3](docs/conformance/PROFILE_V3.md) contains
**3,896 required cases and one explicit nested-storage divergence**, retaining all
3,897 original cases and V1/V2 history. Multiple labels per node are now required
native work; the former exclusion is removed. Set1 #0010 keeps its original
negative oracle but is explicitly divergent because Grafx retains lists of maps.
The native Set1 rerun has 10 passes and that one expected upstream divergence;
this is not full-profile acceptance or completion of multiple labels.

Latest native-label checkpoint: query mutations/CREATE/MERGE, retained history,
existing-target copy and fresh-store transfer/resume format 4 are implemented.
The [installed candidate qualification](docs/reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
passes **36 native scenarios and 36 transfer worker checks** with exact old-reader
refusal and no file changes. All 251 candidate package files match installed wheel
and source. The transfer consumer regression has **236 passes**, including all 58
new label-transfer cases. The subsequent [complete V3 query execution](docs/reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md)
passes **all 3,896 required cases**, with only the authorized Set1 #0010 upstream
divergence and zero not run. Final current-source full repository regression,
supplemental/competitor evidence and paired Pulse qualification remain open;
no release or global install occurred. The chronological checkpoints below retain
their original scope and receipts, not additional unimplemented copies of this work.

Latest Pulse consumer checkpoint: [exact value/label-transfer qualification](docs/reports/FP_PULSE_CURRENT_VALUES_QUALIFICATION.md)
passes **255 grouped source tests and 65 installed-package tests** (overlapping).
Community now preserves exact JSON observations for DECIMAL/nonfinite expression
results and scalar frontiers, and refuses label loss in Pulse's single-type
logical artifact before export. Core is unchanged; no Settings knob or global
installation is introduced. Current real HTTP/browser/MCP, full Grafx regression
and comparative acceptance remain required.

The same candidate subsequently passes 11 exact HTTP checks and real-browser
pagination (500 → 510), Key Decisions results and node detail. Broader installed
source/search/Settings/recovery/MCP acceptance is still required; this does not
replace the full repository run or competitor comparison.
The next checkpoint also passes six authenticated real MCP calls, preserving
exact DECIMAL/NaN output and typed read-only refusal in the neutral v2 envelope.
The subsequent [operational checkpoint](docs/reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md)
passes **121 affected source and 158 installed tests**, with source lifecycle,
Settings/schema, nine actual MCP calls and positive semantic search in the real UI.
An actual post-barrier writer crash is recovered by the next ordinary Pulse writer,
without restarting Pulse; a read alone retained the last published snapshot.
Community now maps schema unavailability and missing spec delivery context to
their typed public errors. No native/Core code or new configuration changed in
that checkpoint. Full Grafx regression, supplemental reconciliation and pinned
competitor execution remain required before the full parity DoD can close.

Final regression checkpoint: **25,053 passed / 24 failed / 19 attributed skips**
on the complete repository selection. Corrective work addresses documentation
recipe selection, current public/dependency/corpus inventories, immutable CRC
fixture inputs, hostile-plan fixture construction and source annotation/docstring/
ASCII requirements. The original failed receipt is retained; this is not a green
regression claim. The [bounded Ladybug comparison](docs/reports/FP_BOUNDED_LADYBUG_COMPARISON.md)
now executes all 16 pinned scenarios (seven matches, nine documented differences);
Neo4j execution remains pending. No required profile case or invariant was waived.
The [corrective checkpoint](docs/reports/FP_INTEGRATED_REGRESSION_CORRECTIONS.md)
records 3,566 directed passes with three further source-surface failures, followed
by **3,270 passes with zero failures/errors/skips** after the complete helper audit.
Every original failing ID is covered by the corrective receipts; a fresh complete
regression and final candidate packaging remain required, not inferred as green.

Earlier native-label foundation: [node-label storage v1](docs/specs/NODE_LABELS_V1.md)
adds per-version GXL1 membership, catalog capability bit 28, immutable cached
candidate summaries, native-authority admission and replay transition checks.
Stable record/table identity, prior snapshots and existing tuple/overflow formats
are preserved. The final grouped storage/transaction/query/contract selection
passed **3,168 tests**, with no failures/errors/skips in 108.879 s; the specification
records its exact receipt and scope. The native Set1 rerun keeps the same ten
passes and one explicit divergence. This foundation
does not yet enable multiple-label Cypher, retained-history/transfer consumption
or installed-Pulse use; the remaining authorized integration is still required.

Next native-label increment: [transactional publication](docs/specs/NODE_LABELS_V1.md#transactional-publication-checkpoint)
now carries explicit membership through staging, reduction, quota checks, proved
schema admission and all canonical heap write paths in the existing COMMIT.
No-op candidate admission avoids repeat schema writes; public table snapshots
preserve candidate metadata. The final grouped selection passed **719 tests**,
zero failures/errors/skips, 210.218 s, including older snapshots on an independent
current-reader handle and pre-WAL failure/rollback under buffer pressure. A pure
engine import violation was corrected without relaxing its architectural policy;
the specification records the original and corrective evidence.
At that checkpoint, history refused unsupported label effects rather than
discarding them; the native-history increment below replaces those temporary guards.

The following [query-label mutation checkpoint](docs/specs/NODE_LABELS_V1.md#query-label-mutation-checkpoint)
adds ordered SET/REMOVE labels, staged-owner predicates/labels(), candidate-table
MATCH with exact membership checks, indexed-hit filtering, native edge/optional
landings, metadata-preserving spill and detached entity labels. First-canonical
DTO `label` is only a convenience: consumers use `labels` for membership, not
physical owner identity. New schema candidates honor procedure schema permission;
unchanged label sets stage no row. The following
[CREATE/MERGE and scan increment](docs/specs/NODE_LABELS_V1.md#query-create-merge-and-scan-checkpoint)
adds multi-label patterns, cross-owner MERGE, nested composition and native-label
TCK state observation. FP-4 now has **289 required passes** and the single authorized
Set1 #0010 divergence. Fresh-store logical transfer was still pending at that
checkpoint and is qualified above; complete profile/package regressions and paired
Pulse acceptance remain outstanding.
The subsequent complete V3 query run has **3,895 passes / two failures / zero
unexecuted cases**: the authorized Set1 divergence and Graph3 #0009 error phase.
The latter is corrected with 61 original graph-expression cases passing; this
does not relabel the full run as green. After the 172-pass codec checkpoint, the
[native retained-history integration](docs/specs/NODE_LABELS_V1.md#native-retained-history-integration)
now preserves membership in activation, COMMIT, scan/index reads, version/diff
DTOs, retention/compaction, verification and physical backup/restore. Its affected
regression has **238 passes**, including real-process cuts with pure/NumPy;
subsequent focused checks cover label-only verification disagreement and pre-WAL
rollback. The public history contract documents label deltas and bounded costs.
The subsequent [existing-target copy integration](docs/specs/NODE_LABELS_V1.md#existing-target-copy-integration)
adds bounded GXL1 copy frames/v4 digest binding and native label/endpoint/receipt
publication. Physical-PK skip preserves target labels even after base-label
removal, independent of other owners sharing a logical label. Existing copy
regression: **77 passes**; final copy/history-codec/contract collection: **421
passes**, including all 25 label-copy cases and six real pure/NumPy process cuts.
Two new fixture-provenance failures were corrected without weakening the API;
their failed receipt remains recorded. The subsequent format-4 transfer and
installed-reader qualification is recorded above. Final full regression/profile
and paired Pulse qualification remain open. No overlapping counts or focused
receipts substitute for final acceptance.
The 5,917-test grouped query/transaction/API run recorded six obsolete-test or
fault-hook mismatches. All are addressed by the final **392-pass** combined
corrective collection; the original grouped run remains failed, with final
current-source package regression still required. Exact receipts and limits are
in the linked node-label contract.
This native-label delivery shipped in the 0.0.6 package; the checkpoint above records its pre-release qualification state, not an upgrade guarantee.
The corrected query/transaction/procedure group has **1,110 passes**, zero
failures/errors/skips; [the contract](docs/specs/NODE_LABELS_V1.md#query-label-mutation-checkpoint)
records original/corrective evidence and the final read-boundary follow-up.

Latest type-package checkpoint: the [consolidated support/refusal matrix](docs/TYPE_SUPPORT.md)
and [60-case installed-wheel qualification](docs/reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
close the FP-5/FP-6 combined old/new-reader checkpoint for the recorded candidate.
Independent DECIMAL/typed-collection bits and the full temporal/decimal/nested
bundle are qualified with pure and accelerated writes, pure recovery/readback,
old live handles, interrupted durable COMMIT and all-file non-mutation proofs.
At that checkpoint, all 250 candidate package files matched its installed wheel
and checkout; it does not qualify the subsequent label-format changes. The
[combined upgrade procedure](docs/V006_COMPATIBILITY.md#combined-native-type-upgrade-procedure)
requires draining/upgrading participants, verified backup and no in-place downgrade.
No global Pulse installation/release occurred. Multiple-label native implementation,
final full regression/profile/supplemental/competitor and paired Pulse acceptance
remain; historical pending statements below describe their original checkpoints.

The same checkpoint's logical-artifact follow-up adds **30 installed scenarios**
for separate DECIMAL/collection descriptors and the combined type bundle, normal
and resumable imports, exact old refusal and no partial destination/workspace.
It exposed a resumable malformed-artifact exception leak; the current API now
normalizes it to `GrafxRecoveryRefused(artifact_invalid)` like normal import.
The corrected installed candidate differs from the native-matrix package only
in `transfer.py`; all other 249 package files are identical. Archived raw errors
are explicitly recorded, not patched or represented as clean typed refusals.
The final affected conformance/type-transfer regression passes **558 tests**,
zero failures/errors/skips, including real process-cut recovery and malformed
manifest refusal before destination/workspace effects; [exact receipt](docs/reports/FP6_TYPE_WHEEL_QUALIFICATION.md#logical-artifacts-and-the-resumable-error-correction).

Latest FP-6 increment: [native decimal storage](docs/specs/DECIMAL_VALUES_V1.md#native-storage-contract-development)
adds public DecimalValue parameters/results, DECIMAL(p,s) schema, exact staged
assignment, a 19-byte frame and atomic decimal_values_v1 capability admission.
Focused durability/storage qualification passes **441 tests**, including pure/NumPy,
real process cuts, recovery and concurrent snapshots. [Evidence and remaining work](docs/reports/FP6_DECIMAL_NATIVE_QUALIFICATION.md).
The next increment adds exact native arithmetic/casts/aggregates, numeric
comparison/grouping/order, spill parity and typed-decimal equality indexes, with
**818 passing affected regression tests**. [Numeric qualification](docs/reports/FP6_DECIMAL_QUERY_QUALIFICATION.md).
The subsequent [history/copy/logical-transfer increment](docs/reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md)
preserves decimal precision/scale across retained versions, backup/restore,
parameter-bound copy receipts, native logical artifacts and crash resume. Schema
mismatches and malformed frames refuse before import destination/workspace effects.
Final grouped affected regression: **529 passed**; public/API/architecture and
executable documentation contracts: **2,427 passed**, zero failures/errors/skips.
The latest [interface increment](docs/reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md)
adds canonical CLI JSON/schema, typed CSV/JSONL/SQLite imports and owned
DECIMAL/NUMBER procedures, with exact values, existing bounds and native rollback.
Grouped interface regression: **1,240 passed**. Two architecture failures had one
cause (an unrestricted regex import); direct bounded ASCII parsing fixed it, with
**342 passing** import-boundary/decimal-transport corrective tests.
The [columnar increment](docs/reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md) now adds
ArrowDecimalType(p,s) and exact decimal128 Arrow/Pandas/Polars/Parquet, mandatory
metadata, bounded workspace and whole-call import rollback. **347 grouped tests
pass**, including earlier scalar/vector/temporal consumers, independent snapshot
reads and proven-durable apply-failure recovery on both codecs.
Final public/API/architecture/executable-documentation contracts: **2,474 passed**,
zero failures/errors/skips. This closes the preceding pure-core import violation.
Typed collections and installed-reader coverage,
unsupported index families and checkpoint C remain open; no installation or release.

The [typed-collection integration](docs/specs/TYPED_COLLECTIONS_V1.md) connects
public StoredType/ColumnDef, recursive DDL, catalog-v2 capability bit 27 and strict
native tuple validation. Assignment precedes intent/quota proofs. CLI schema,
history, nullable append, copy and logical transfer preserve full descriptors.
Empty STRUCT parsing is corrected without changing expression comparisons;
frozen public observations remain separate from engine authority. [Native qualification](docs/reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md)
passes **3,048 grouped tests** and **2,447 public/annotation/import/documentation
tests**, zero failures/errors/skips. Intermediate fixture errors and an outdated
decimal-dispatch inventory test were corrected, not hidden. Parameterized external interchange, installed-reader
checkpoint C and full FP-8/Pulse remain open; no installation or release.
Earlier foundation receipts (86 model, 428 corrective and eight module-surface
passes, plus the initial annotation failures) remain historical evidence in the spec.

The next [collection JSON/text increment](docs/COLLECTION_JSON.md) adds exact,
owned JSON helpers and StoredType-aware CSV/JSONL/SQLite imports. Explicit ANY tags
preserve map/scalar identity; INT64 strings preserve full precision. Transport
schema validation is separate from exact target assignment. Descriptors are owned,
existing budgets and whole-call rollback remain, and cumulative binary charging
precedes Base64 allocation. [Qualification](docs/reports/FP6_TYPED_COLLECTION_TEXT_QUALIFICATION.md):
**506 grouped tests** and **2,474 public/annotation/import/documentation/executemany
tests** pass with zero failures/errors/skips. No new native format or connection
knob. At this checkpoint, parameterized columnar transport, checkpoint C and
FP-8/Pulse remained open; the following increment advances the columnar portion.

The [typed collection columnar increment](docs/COLLECTION_COLUMNAR.md) now supports
Arrow/Pandas/Polars/Parquet with complete owned descriptors, exact nested native
values and atomic import staging. Metadata is canonical UTF-8-safe ASCII, MAP
conversions reject duplicate keys, ARRAY length remains exact, and empty STRUCT
is distinct from NULL. Bounded recursive Polars conversion preserves native depth
without passing deep schemas through the Arrow C Data recursion ceiling. No new
native row/WAL/catalog format or connection option is introduced. Its support
matrix and installed-reader checkpoint C are now covered by the later increment
above; final FP-8/Pulse acceptance remains open.

[Columnar qualification](docs/reports/FP6_TYPED_COLLECTION_COLUMNAR_QUALIFICATION.md):
660 grouped regression tests, 2,483 contract/documentation tests and 161 final
feature tests pass (overlapping selections, not summed). The report retains the
intermediate depth/declaration failures and documents their corrections.

Preceding FP-7 schema increment: [permissioned native schema authority](docs/specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md)
adds schema_write/max_schema_statements, explicit CREATE DDL and implicit flexible
schema under the caller's artifact journal. Catalog-v2 textual custom indexes now
compose with earlier/later DML using a fenced durable base plus normal WAL deltas.
[Qualification](docs/reports/FP7_SCHEMA_QUALIFICATION.md) records successes and
intermediate failures. Compiled outer scans do not dynamically replan after DDL;
v1 activation and changed-existing-schema index builds retain explicit prerequisites.
FP-6, pending FP-4 decisions and full FP-8/Pulse qualification remain open.
Schema checkpoint: all 62 new cases pass in the final 107-case selection; 348
consumer/configuration/extension cases and original 52/52 procedure TCK pass.
The expanded 7,122-case run had 7,120 passes and two fixture failures, both
corrected and revalidated without production-source changes. The receipt preserves
the failed run; no second complete clean-exit execution is claimed.

Prior FP-7 nesting/effect increment: [recursive CALL and explicit determinism](docs/specs/PROCEDURE_NESTING_EFFECTS_V1.md)
adds self/mutual native procedure recursion, inherited bounded depth, root mutation/
traversal budgets and per-name counters shared throughout the chain. Native read
authority cannot escalate to a writer. Callback determinism is now explicit and
defaults to False; registry metadata controls optimizer effect admission.
[Qualification](docs/reports/FP7_NESTING_QUALIFICATION.md). Schema authority is added
above; FP-6, pending FP-4 decisions and FP-8 remain open.
Acceptance: 610 combined regressions, 1,466 feature/static-contract tests and 138
final affected tests pass in overlapping runs; all 36 new cases and the original
52/52 procedure TCK cases are covered.

Prior FP-7 query increment: [permissioned native query/result authority](docs/specs/PROCEDURE_QUERY_AUTHORITY_V1.md)
adds opt-in ProcedureReader and ProcedureWriter.query(), bounded materialized
results and invocation-witnessed entities, including returning native writes.
Child queries preserve the caller's snapshot, clocks, cancellation and rollback.
Query count/row/byte limits span input-row invocations; no independent transaction
or commit is exposed. [Qualification](docs/reports/FP7_QUERY_QUALIFICATION.md).
Acceptance: 562 combined regressions, 1,462 feature/static-contract tests and 125
final query/writer/cancellation tests pass in overlapping selections; 33 new
feature cases and the original 52/52 procedure TCK cases are covered.
Nesting/effect and native schema declarations are added above with explicit limits.
FP-6/FP-8 and pending FP-4 decisions are not closed by this increment.

Prior FP-7 entity increment: [NODE, RELATIONSHIP, PATH and typed entity lists](docs/specs/PROCEDURE_ENTITY_SIGNATURES_V1.md)
preserves native committed/provisional identity through callback observations,
UNWIND, query writes and results. Only current-invocation witnesses are accepted;
foreign/copied/expired observations grant no native authority. Existing budgets,
statement rollback and durability are unchanged. [Qualification](docs/reports/FP7_ENTITY_QUALIFICATION.md).
Native query/result, nesting and bounded schema authority are added above; this
does not close FP-7 or the full functional-parity plan.
Acceptance: 27 entity feature tests, 452 combined affected regressions and 1,455
feature/static-contract tests pass in overlapping selections; original FP-7 TCK
remains 52/52. Child-DELETE alias invalidation was corrected and regression-tested.

Prior FP-7 value increment: [temporal, LIST/MAP/ANY and vector signatures](docs/specs/PROCEDURE_NATIVE_VALUES_V1.md)
adds native value arguments/results with recursive ownership, bounded encoding,
typed persistence and whole-statement rollback. The existing temporal capability
still publishes only with durable data. No new storage format or scalar-UDF
contract is introduced. [Qualification](docs/reports/FP7_NATIVE_VALUES_QUALIFICATION.md).
Validation: 416 expanded regressions, 3,516 public-contract/feature tests and 241
final affected tests pass (overlapping runs); original procedure owner remains 52/52.
The subsequent entity increment is recorded above; broader procedure capabilities remain FP-7 work; parameterized
collection/DECIMAL storage remains FP-6.

Prior FP-7 writing increment: [native transaction-scoped mutation authority](docs/specs/WRITING_PROCEDURES_V1.md)
adds explicit write registration/permissions, revocable same-thread authority,
shared operation/output/native mutation budgets and whole-statement rollback.
The initial door accepts result-free DML on declared models; it does not expose
commit, DDL, nested procedure calls or arbitrary query-result authority.
[Qualification and remaining scope](docs/reports/FP7_WRITING_QUALIFICATION.md).
Acceptance: 357 expanded query/transaction tests, 3,543 combined feature/cache/
public-contract tests and 139 final authority/extension tests pass (overlapping
selections); the separate original procedure family remains 52/52 passing.
FP-7 stays partial until broader signatures and capability contracts are qualified.

Prior FP-7 numeric increment: [NUMBER signatures and DOUBLE widening](docs/specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md)
close **all 52 original procedure cases**, with no failed/unexecuted selected cases.
317 affected regression tests pass; exact integer preservation, finite/range
validation, conversion precision, budgets and statement rollback are documented.
The final numeric/public-contract selection passes 3,510 tests; generated API and
documentation checks also pass. Counts overlap rather than form one combined run.
The subsequent bounded writing increment above does not close broader signatures.

Prior FP-7 invocation increment: [standalone CALL and signature resolution](docs/specs/PROCEDURE_INVOCATION_V1.md)
advances the original procedure family to **46 passed / two failed / four not run**.
The remaining six cases require numeric signature/coercion support. Combined
1,230-test, parameter/API/search 394-test and final 101-test hardening selections
pass; [scope, receipts and remaining work](docs/reports/FP7_INVOCATION_QUALIFICATION.md).
Writing callback authority is still pending; no new model exclusions were added.

Full frozen-profile checkpoint: **3,816 passed / 73 failed / eight not run**;
the required subset has 51 failures and the same eight not-run cases. The subsequent
[unit-procedure increment](docs/specs/UNIT_PROCEDURES_V1.md) passes 513 affected tests
and advances the separate FP-7 selection to 27 passed / 21 failed / four not run.
[Baseline, remaining causes and exact receipts](docs/reports/FP_FULL_PROFILE_20260912.md).
No profile exclusion changed; FP-6/7 and final acceptance remain open.
The wider documentation/API surface follow-up also passes **3,445 tests** after
completing 128 missing docstrings in 29 prior-increment modules; executable ASTs
are unchanged. Its initial failures and final receipt are retained in that report.

FP-6 preparation: [exact decimal contract](docs/specs/DECIMAL_VALUES_V1.md) fixes
precision/scale, exact fitting, explicit rounding and finite numeric keys. Internal
integer-only arithmetic, explicit conversion and exact aggregate finalization pass
118 functional tests; earlier grouped contract checks are recorded in the spec.
The newer native storage increment above supersedes the earlier absence of a
format/parameter type. Native numeric query integration is now exercised above;
typed collections, full consumer coverage and checkpoint C remain open. No new connection configuration.

Grouped query/transaction checkpoint: [corrective follow-up](docs/reports/FP_QUERY_TRANSACTION_CHECKPOINT.md)
records 6,171 passes and five old-contract fixture failures in the full selected
run, followed by **427 passing corrective tests** covering all five causes.
Actual post-attachment DDL failure, UNWIND target authority, qualified vector owner
selection and vector-list commit/rollback remain tested. The original failed run
is not relabeled green; frozen-profile and final checkpoint acceptance remain open.

Latest paired consumer check: [installed Pulse HTTP/UI](docs/reports/PULSE_HTTP_UI_PARITY_QUALIFICATION.md)
qualifies 500 → 510 node pagination and eight HTTP operations on a synthetic board.
It corrects false-empty logical relationship reads in Community with **153 passing
source tests** and **41 installed-package tests** (overlapping coverage); Core remains agnostic. Broader logical query syntax, full Pulse/MCP
regression and full-profile acceptance remain open. No global install or release.

Preceding FP-4 consumer follow-up: [general MERGE in Pulse](docs/reports/GENERAL_MERGE_PULSE_QUALIFICATION.md)
passes 39 adapter/result tests against three privately installed, byte-verified
wheels, plus **137 affected source tests**. No Core implementation change or new UI
write permission. This closes that Python consumer check, not HTTP/browser/full
profile acceptance. Global installations and production data remain untouched.

Preceding FP-4 increment: [whole-pattern MERGE](docs/specs/GENERAL_MERGE_V1.md)
adds unbound endpoints and fixed multi-hop patterns with complete-match-or-create
semantics. Partial matches never reuse unbound nodes implicitly. **307 affected
regression tests pass**, followed by **2,390 contract/fault tests** including all
29 new native cases and four real process cuts (overlapping coverage). Shared-node
actions now see earlier owner updates; read snapshots and conflicting-writer OCC
are tested. Original MERGE stays at 70 passes/five label-related failures. Label
scope/oracle decisions and the broader parity/Pulse acceptance remain open.
No new format or configuration, installation, commit, push or release.

Preceding CLI consumer increment: [vector owner selection](docs/reports/CLI_VECTOR_OWNER_QUALIFICATION.md)
adds `--table` / `--table-kind node|rel` to vector search and advertises them through
help/build capabilities. It delegates to the existing snapshot-bound Python API;
ambiguous owners refuse, text index identity and hybrid node ownership remain
unchanged. **324 command/process/output tests pass**. No new format or connection
configuration; no release/Pulse claim.

Preceding query-consumer correction: [view expression dependencies](docs/reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md)
captures physical schema dependencies inside EXISTS and pattern expressions,
including nested scopes and logical group members. It closes reproduced empty-
inventory/reserved-read bypasses without disabling native expressions. **176
affected tests pass**, including namespace-consumer process cuts; a subsequent
**2,341-case contract/boundary selection passes**, including all 19 current view
expression tests (overlapping coverage). No new format
or configuration; this source increment is not installed-Pulse/full-parity acceptance.

Preceding namespace acceptance: [installed logical transfer](docs/reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md)
passes 42 worker checks: 10 seeds, 16 imports and 16 exact ordinary/resumable format
refusals. **67 affected tests pass**. Actual archived 0.0.5/0.0.6 packages and a
current candidate are identified by hashes. Old format-1 import is qualified at
their default batch size; their reproduced tiny-batch identity-floor defect is
explicitly not a passing claim. Current one-row imports pass. This closes the
selected old-importer qualification, not full parity, Pulse or release acceptance.

Preceding namespace increment: [projection, view and migration consumers](docs/reports/NAMESPACE_PROJECTION_VIEW_MIGRATION_QUALIFICATION.md)
resolve known kind through projection scans and metadata ownership. Logical views
retain both physical dependencies with a shared spelling and refuse stale/incomplete
inventories. **87 affected tests and eight real process cuts pass**, followed by
**2,356 interop/public-contract/boundary checks** (overlapping coverage); no new format,
configuration, installed-Pulse or full-profile claim.

Preceding namespace increment: [component and legacy vector repair](docs/reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
qualifies five lower-level mutation/maintenance operations by physical table ID
and verifies explicit reconstruction of missing historical vector indexes.
**609 affected tests pass**, including owner isolation, pure/NumPy execution and
process interruption. A separate old-installed-writer/current-source matrix passes
**12/12 scenarios**, including eight process cuts. Read-only opening does not silently recreate missing files;
writable opening does not declare a stale placeholder healthy. No new format or
configuration, no installed-Pulse or full-parity claim.

Preceding quality increment: [declaration and boundary qualification](docs/reports/FP_STATIC_CONTRACT_QUALIFICATION.md)
corrects the 75 public-surface failures: **2,357 final static/boundary/runtime-contract checks pass**, with
**1,976 behavioral tests** and a subsequent **695-case algorithm/boundary suite**
passing. Exact temporal algorithm imports are narrowly admitted; module-wide
mechanism access remains forbidden. Counts overlap; no full-profile/package claim.

Preceding namespace increment: [qualified vector maintenance](docs/specs/VECTOR_QUALIFIED_MAINTENANCE_V1.md)
adds public memory/rebuild selection and detached physical IDs. **95 affected tests
pass**, including both codecs/kinds, sibling preservation, stale failure/retry and
cold reopen. Its expanded foundation audit exposed the 75 static-contract
failures corrected above; the failed receipt remains historical evidence.

Preceding namespace repair: [physical vector ownership](docs/specs/VECTOR_PHYSICAL_OWNERS_V1.md)
qualifies local attachment/epochs and native/public/hybrid searches by table,
installs relationship vector indexes through journaled DDL, and fixes typed
vector SET conversion in property/overlay/replacement forms. **219 affected tests
pass**, including eight real process cuts and pinned-reader/foreign-writer checks.
The subsequent [durable naming repair](docs/specs/VECTOR_OWNER_NAMES_V1.md)
preserves existing names and selects table-ID/column-position names for new
colliding or oversized names, with catalog capability 25. It passes 185 affected
tests, a subsequent 20-case API suite (overlapping coverage), and 24 installed-wheel
admission cases, including intact legacy artifact adoption. The latest component
and missing-artifact qualification above completes those separate vector obligations;
no installed-Pulse, complete parity or release claim.

Preceding FP-4 increment: [native EXISTS subqueries](docs/specs/EXISTS_SUBQUERIES_V1.md)
implement correlated boolean read bodies, implicit imports, optional RETURN,
aggregation/UNION and nested scopes. All 10 original existential-subquery cases
pass natively. The final combined regression passes 978 tests, including four
pure/NumPy subprocess recovery cuts and public-plan/cancellation boundaries;
documentation/API/configuration checks pass. Exact receipts are in that contract. This is
not completion of FP-4, the full parity plan or installed-Pulse qualification.

## Functional parity expansion plan

September 12 namespace-consumer follow-up: qualified bloat/vacuum and nullable
column addition preserve node/relationship identity, including same-name tables.
Affected regression: 104 passed; subprocess schema-recovery matrix: eight passed.
Pulse Community logical transfer now qualifies catalog/scans and validates the
complete `(kind, name)` inventory; Core is unchanged. See [contracts, receipts
and remaining acceptance](docs/specs/GRAPH_NAMESPACES_V1.md). The later vector
runtime and durable naming repairs above resolve shared-space local ownership
and intact legacy adoption; qualified maintenance and missing-artifact repair
are now covered by the later increments above. Remaining consumer and complete
package acceptance retain their own requirements.
No production installation is implied.

Current FP-4 increment: SQ-13 adds native returning/unit updating UNION, ordered
private branch effects, stable inserted-entity identity before DISTINCT/spill,
shared rollback and read/cursor refusal. [Usage](docs/COMPOSABLE_QUERIES.md#updating-union-branches)
and [implementation/receipts](docs/specs/WRITE_SUBQUERIES_V1.md#sq-13-implementation-and-qualification).
The focused selection passes 338 tests; eight additional interruption/API-control
tests pass. Final grouped qualification passes **6,528 distinct tests**: 5,542
across all 178 query/tools files and 986 across all 49 transaction files, with zero
failures/errors/skips and no duplicate cases between those three receipts.
Hashes and exact selections are recorded in the supplemental profile. Focused
counts overlap this total. This is not full-repository/frozen-profile acceptance,
installed-Pulse validation or completion of the overall parity plan.

Preceding SQ-12 increment: import scopes support persistent explicit and
wildcard imports plus branch-local leading-WITH imports. Native authority is
preserved across UNION and real sorting spill without promoting detached values.
[Scope contract](docs/COMPOSABLE_QUERIES.md#subquery-import-scopes) and
[corrective trace/evidence](docs/specs/WRITE_SUBQUERIES_V1.md#sq-12-persistent-explicit-imports-and-branch-local-importing-with).
The final focused selection passes 190 tests, including bounded sorting, recovery and public
plan isolation; it does not qualify updating UNION or installed Pulse.
After the final correction, the complete grouped query/tools regression passes
**5,468 distinct tests across 177 files**, zero failures/errors/skips. Both final
receipts and SHA-256 hashes are recorded in the supplemental profile; the failed
intermediate regression is superseded. Focused counts overlap this total.

Preceding FP-4 increment: native explicit-import returning and unit writing calls
are implemented, including invocation-local write/read phases, owner-current
entity values, nested write refusal in read APIs and exact pending-identity
witnesses. All invocations retain the outer statement's rollback boundary.
[Usage](docs/COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries) and
[scope/corrective trace/receipts](docs/specs/WRITE_SUBQUERIES_V1.md).
Final grouped regression passes **5,372 distinct tests** across all 176 query/tools
test files, zero failures/errors/skips. The focused 37-test receipt contains 35
feature tests (included in that regression) and two additional real subprocess
COMMIT/recovery cuts. Counts are not added as if disjoint. That checkpoint left
updating UNION open; SQ-13 above adds it. Complete checkpoint B/Pulse qualification
and the overall parity plan remain open.

Current temporal checkpoint: **1,004/1,004 original temporal-family cases pass**
through native query execution, with unchanged V2/V1 sources and expectations;
2,893 cases are outside this selection. Public constructors, scoped clocks,
fields, duration arithmetic, comparison/ordering, truncation/differences,
toString and native parameter/typed/ANY storage are implemented.
[Usage and boundaries](docs/TEMPORAL_VALUES.md),
[matrix and exact receipts](docs/specs/TEMPORAL_VALUES_V1.md#current-evidence).
This does not close FP-5: the follow-ups below qualify index/history/copy/transport
and the current temporal old-wheel fence, but package acceptance, the combined
FP-5/FP-6 cross-version checkpoint and Pulse qualification remain required.
Broader FP-2 qualification is tracked
separately, never inferred by adding focused counts.

Temporal lifecycle follow-up: logical transfer now activates catalog v2 for typed
temporal schemas (the missing prerequisite previously blocked export validation).
Focused integration covers exact temporal values in typed/ANY/flexible node/edge
properties through transfer, existing-target copy/receipt replay, indexed equality,
as-of/diff history and physical backup/reopen. See
[the consumption contract](docs/TEMPORAL_VALUES.md#transfer-copy-and-history).
This is additional FP-5 coverage, not full format/transport/Pulse acceptance.
Its qualification passes **259 tests**, zero failures/errors/skips: 27 focused
temporal lifecycle/adversarial cases, 119 transfer/copy regressions and 113
history/backup regressions. These are scoped native integration suites, not a
whole-repository or installed-Pulse regression.

Next temporal consumption increment: scalar/nested CLI JSON now uses explicit
temporal tags; Arrow/Pandas/Polars/Parquet use exact coordinate structs with
mandatory `components-v1` metadata, bounded conversion and whole-call atomic
imports. [Types, schema and bounds](docs/EXTENSIONS_AND_ARROW.md#exact-native-temporal-values-006-development).
No host datetime narrowing, timezone lookup or NaN storage admission is introduced.
Final qualification passes **718 distinct tests**, zero failures/errors/skips:
115 interchange/component tests and 603 CLI/entity/expression tests in disjoint
selections. New tests include malformed late batches, metadata loss, independent
cursor/writer snapshots and pure/NumPy round-trips. Full FP-5 remains open.

Local temporal ingestion now accepts all six native families in CSV, JSONL and
SQLite, using explicit canonical JSON tags under a declared type. This preserves
full native values without string/date inference or zone lookup. Existing source
limits, NULL rules, source-connection release and whole-call staging are unchanged.
[Usage and field grammar](docs/LOCAL_TEXT_IMPORT.md#native-temporal-fields-006-development),
[SQLite contract](docs/LOCAL_SQLITE_IMPORT.md). This is FP-5 consumption progress,
not completion of the remaining index/lifecycle/cross-version/Pulse qualification.
Qualification: **228 tests passed**, zero failures/errors/skips, across 160 local
import/component regressions and 68 shared temporal transport/entity/lifecycle
regressions. The focused 101-test receipt overlaps these and is not added again.

Temporal index qualification now covers all six typed primary keys, exact secondary
and composite keys across hash/sparse/posting layouts, updates/rollback/delete,
snapshot isolation, duplicate-writer rejection, rehash/rebuild and durable recovery.
[Support/refusal matrix](docs/INDEXES_AND_VECTORS.md#native-temporal-key-support-006-development).
The existing ANY-index, ordered-type and FTS-type exclusions are tested before
catalog publication; no new exclusion or relaxed mixed-key policy is introduced.
This checkpoint adds 76 focused index tests; final disjoint regressions pass
**798 tests**, zero failures/errors/skips (157 API/query/transaction and 641 index
tests). Existing native key encoding needed no production change. Package-level
acceptance, cross-version and Pulse qualification are still not inferred from it.

The native temporal old-wheel boundary is now qualified in **24 installed-wheel
scenarios**: two archived binaries × materialized/pending-WAL/already-open handle
states × read/write attempts × pure/accelerated writers. Every old refusal leaves
all files unchanged; the candidate recovers all six exact values and reopens twice.
The accompanying focused regression passes **138 tests**, zero failures/errors/
skips, including unlabeled/ANY properties and expression-NaN storage rejection.
The original required Match4 #0004 also passes again without fixture adaptations.
[Wheel identities, reproduction and receipt hashes](docs/specs/TEMPORAL_VALUES_V1.md#installed-wheel-temporal-fence-qualification).
These counts are separate evidence, not a whole-repository/Pulse regression or
completion of the combined FP-5/FP-6 checkpoint.

The subsequent complete native **FP-2 owner selection passes 1,976/1,976 required
cases**, zero failures and 1,921 outside selection. Its previous 65 temporal
dependencies are closed, including whole-entity observations with temporal
properties. [Owner receipt](docs/conformance/FP2_PROGRESS.md#latest-owner-wide-checkpoint-temporal-dependencies-closed).
Complete multi-package/Pulse acceptance is still open.

Final grouped regression for this increment: **6,812 passed**, zero failures,
errors or skips, covering all query/tools tests plus temporal storage, provider
and selected native API suites. The final-code FP-2 rerun again passes all 1,976
required cases. [Scope, corrective trace and per-group hashes](docs/specs/TEMPORAL_VALUES_V1.md#grouped-regression-trace).

The following foundation checkpoints are chronological evidence. Their earlier
query/storage “pending” statements are superseded by the current checkpoint above.

The remaining malformed-literal contracts now pass the full 131-case original
literal family, with 384 regression tests passing. This closes the three literal
taxonomy blockers in the FP-2 baseline without fixture/ledger changes.

FP-5 implementation has started with six internal calendar/local/zoned/duration
containers, including year zero and expanded fixed-offset/named-zone years, and a
bounded package-backed timezone provider. Recorded transition instants remain
exact; only annual future rules use Gregorian-cycle equivalence. Provider tests
cover all 598 names in the tested timezone package, and the selected grouped
regression passes 1,314 tests. Query/storage integration remains open. The
[temporal integration matrix](docs/specs/TEMPORAL_VALUES_V1.md) inventories all
1,004 original temporal-family cases and preserves full query/storage/lifecycle
requirements. This foundation is not yet a public constructor, parameter or
stored-value capability; FP-5 remains open.

The internal temporal text layer now constructs all six value families, including
calendar/week/ordinal/quarter forms, named offsets and signed/fractional durations.
All 53 Temporal2 input/result pairs pass as component tests, not as executed TCK
queries; the grouped regression passes 1,441 tests. Exact round-trip/negative coverage and remaining integrations are recorded
in the same [temporal contract](docs/specs/TEMPORAL_VALUES_V1.md#internal-text-construction).

Internal temporal map/selection builders and exact epoch helpers now retain
calendar/clock provenance, ISO week years and zone-assignment versus
instant-conversion semantics. Unchanged zoned selections preserve recorded
offsets without reinterpreting them with newer rules. The grouped regression
passes 1,552 tests. [Component contracts and remaining integration](docs/specs/TEMPORAL_VALUES_V1.md#internal-map-construction-and-selection)
remain explicit: public query overloads, clock context and durable admission are
not enabled or qualified by these component tests.

The internal temporal clock protocol now provides exact transaction/statement
captures and fresh realtime reads, independent of liveness/lease clocks. Context
defaults and per-call timezone overrides remain isolated, and named conversion
must preserve the captured instant. The grouped regression passes 1,594 tests.
[Capture contract and required lifecycle wiring](docs/specs/TEMPORAL_VALUES_V1.md#internal-temporal-clock-scopes)
remain distinct: the transaction manager/executor and public clock overloads are
not yet connected to this context.

Internal temporal arithmetic now covers the 27 original Temporal8 input/result
pairs as component tests, including duration scaling and each instant family.
Additional proofs cover month-end clamping, date whole-second days, negative
precision, DST day-versus-24-hour behavior, repeated/skipped local times and
overflow refusal. The grouped regression passes 1,657 tests. The
[arithmetic contract](docs/specs/TEMPORAL_VALUES_V1.md#internal-temporal-arithmetic)
documents the semantics and remaining operator/query/storage integration;
component evidence does not qualify Temporal8's native write/read scenarios.

Internal temporal accessors and separate predicate/order contracts now cover all
seven Temporal5 result vectors and 18 Temporal7 pairs at component level. Negative
duration components, recorded offsets, pre-epoch precision, int64 field overflow,
NULL/incomparable predicates and duration order ties are tested. The selected
grouped regression passes 1,713 tests. [Field and comparison contracts](docs/specs/TEMPORAL_VALUES_V1.md#internal-accessors-and-comparison)
remain separate from native property/operator, spill/index and storage wiring;
neither TCK family is marked qualified from internal component evidence.

Internal truncation now covers all five instant targets, calendar/ISO week
boundaries, bounded smaller-field overrides and same-local timezone replacement.
All 322 Temporal9 argument/result pairs match in a component diagnostic; the
grouped selected regression passes 1,892 tests, including independent calendar
checks and DST/negative/precision bounds. The [truncation contract](docs/specs/TEMPORAL_VALUES_V1.md#internal-truncation)
records signed expanded-year semantics and explicit named-time context. Native
query/default-clock and persisted-value qualification remain open.

Internal `between`/`inMonths`/`inDays`/`inSeconds` now implement mixed-family
component inheritance, complete calendar units and exact elapsed residues.
The selected regression passes 1,971 tests; all 131 Temporal10 examples match in
a component diagnostic (clock examples use explicit captured test values, not
native query clock execution). [Difference contracts](docs/specs/TEMPORAL_VALUES_V1.md#internal-differences-between-instants)
record named-rule requirements, full-range and dateless semantics. Query type,
operator/function, parameter/spill/index and durable admission remain open;
these receipts do not close FP-5 or change native conformance counts.

The six [native temporal values](docs/TEMPORAL_VALUES.md) now have public Python
wrappers, query parameter/result support, typed DDL and general/ANY/nested tuple
persistence. Existing tags 0–11 are unchanged; tags 12–17 retain nanos, offsets,
zone identity and calendar duration components. Catalog-v2 bit 23 is published by
typed DDL or automatically in the same transaction as the first temporal ANY
write, preserving already-staged schema and original-snapshot OCC. After activation,
ordinary row commits avoid a new schema admission lock. Rollback, pre-WAL retry,
independent reader snapshots and durable-fault row recovery are tested.
This closes native row admission, not the full FP-5 package: complete
index/history/copy/transfer/JSON/tabular qualification and
Pulse consumption remain open. The [chronological receipts and remaining matrix](docs/specs/TEMPORAL_VALUES_V1.md#current-evidence)
preserve earlier preparatory evidence without changing native TCK qualification.
Native storage checkpoint: **4,121 passed**, zero failures/errors/skips, 296.426 s;
all storage-core, transaction and recovery tests plus selected API/query regressions.
The run includes a corrected close/commit ownership regression and real temporal
row recovery. [Detailed receipt](docs/specs/TEMPORAL_VALUES_V1.md#current-evidence).

Native entity collections now retain identity and source-table authority across
UNWIND, rematch and writes, including bounded spill and repeated identities.
Implicit relationship DDL adopts newly created endpoint indexes without switching
previously selected authorities. All seven original List12 and 14 Unwind1 cases
pass, closing the four collection/MERGE blockers in the FP-2 baseline inventory.
[Usage/API contract](docs/QUERY_LANGUAGE.md#native-entity-collections-and-unwind)
and [FP-3 evidence](docs/conformance/FP3_PROGRESS.md). Full package/profile and Pulse
qualification remain outstanding; there is no installation or release in this increment.

Five required negative-contract blockers are closed: duplicate RETURN/WITH
headings, missing computed WITH alias, a row aggregate in a list-local expression,
and size(path). Native errors preserve cause and planning phase; the conformance
mapper consumes that evidence, not expected scenario output. Existing admission,
transaction rollback and persistence boundaries are unchanged.
[Public error contract](docs/QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions)
and [qualification/remaining failures](docs/conformance/FP2_PROGRESS.md).

Grouped/DISTINCT RETURN now supports compound sort keys over complete projected
expressions and aggregate results without reopening discarded inputs. Alias type
proof is separate from execution; local shadowing and single producer evaluation
are preserved. Original ReturnOrderBy6 and ReturnOrderBy2 selections pass all five
and 14 cases, respectively. [Language/API contract](docs/QUERY_LANGUAGE.md#return-aliases-and-grouping-expressions)
and [test receipts](docs/conformance/FP2_PROGRESS.md). Full FP-2/profile closure
remains pending; no case reclassification or fixture adaptation was used.

Projected nested maps now work across aliases, parameters, CASE, NULL and returning
read-subquery imports/exports without requiring a graph table or reevaluating the
source expression. Literal selected paths retain type inference; unknown fields
are checked at use. The original With2 family passes both cases without fixture
adaptation. [Consumption contract](docs/QUERY_LANGUAGE.md#values-and-python-mapping)
and [increment evidence](docs/conformance/FP2_PROGRESS.md). Remaining FP-2 work is
still tracked separately; this does not declare full parity.

Scalar conversion failures now expose native evaluated-type/phase evidence, while
unconvertible text remains NULL and arbitrary host coercion remains forbidden.
The focused 47-case original conversion family passes. Homogeneous literal-list
element types now reach local expressions without mixing lexical scopes; known
invalid arithmetic refuses at planning and dynamic type failures retain execution
evidence. All 604 original quantifier scenarios pass, and the final query/tools
regression passes 5,013 tests without failures/errors/skips. Data-only rollback
retains bounded owner landing caches only under the existing identity/content
proofs; schema rollback still retires them.
[Error/API contract](docs/QUERY_LANGUAGE.md#conversion-and-arithmetic-error-contracts)
and [qualification and remaining work](docs/conformance/FP2_PROGRESS.md).
This is a development increment, not closure of FP-2 or the full parity profile.

RETURN alias properties/compound sort expressions, grouping ambiguity and grouped
WITH name-resolution precedence are implemented and tested. The focused original
WithOrderBy4 family passes all 20 cases. Nested aggregates have a proven error
category; Return6 now passes all 21 original cases. Direct volatile aggregate
arguments refuse at planning; WITH materialization retains random aggregation.
[Contract](docs/QUERY_LANGUAGE.md#stable-aggregate-arguments-and-volatile-row-values)
and [evidence](docs/conformance/FP2_PROGRESS.md). Overall FP-2 is not closed.

Native `percentileDisc` / `percentileCont` now close the 13 percentile cases,
including numeric range errors and composed grouping. All 35 original cases in
the aggregation-expression family pass. Both ordinary and memory-bounded execution,
DISTINCT, numeric extremes, NULLs, NaN expressions and late rollback are tested;
truncated external runs refuse. [Usage and costs](docs/QUERY_LANGUAGE.md#percentile-aggregates).
This does not close FP-2 or the full profile; no fixture adaptations or exclusions
were introduced.

Row-independent `SKIP`/`LIMIT` expressions are implemented with single evaluation
per logical invocation and phase-proven type/negative/dependency errors. The
focused RETURN window family passes all 31 original cases; the WITH window family
now passes all 9 original cases after correcting plain WITH ordering scope.
[Usage and optimization cost](docs/QUERY_LANGUAGE.md#row-independent-skip-and-limit-expressions)
and [regression evidence](docs/conformance/FP2_PROGRESS.md). This does not close FP-2.

Plain WITH ORDER BY can now use unexported input bindings while output aliases
shadow the input names. Private sort inputs are dropped before later clauses.
The wider WITH-ordering inventory now reports 227 original passes / 65 failures;
all remaining failures in this selection stop at missing functions.
[Scope contract](docs/QUERY_LANGUAGE.md#with-ordering-scope).

WITH modifiers now reuse exactly projected expressions after DISTINCT/grouping;
plain attached WHERE can temporarily read unexported source bindings. Those private
inputs survive bounded sorting and are removed before downstream export. All 19
original WITH-WHERE cases pass. Alias/local shadowing, spill and rollback have
focused tests; full functional parity and installed-Pulse qualification remain open.

**Historical V2 scope: [V2](docs/conformance/PROFILE_V2.md)** retains all 3,897 source
cases and makes 3,875 mandatory, with 22 existing multiple-label divergences.
It withdraws 405 obsolete exclusions without modifying V1, expectations or
observed outcomes. Native `RETURN *` now projects visible lexical variables through
UNION/read subqueries and cursors; [usage and limits](docs/QUERY_LANGUAGE.md#returning-the-visible-scope).
The independent TCK literal reader now admits Gherkin-unescaped newlines, allowing
the three previously unexecuted string cases to run. FP-2/profile closure remains
incomplete; [diagnostics and focused evidence](docs/conformance/FP2_PROGRESS.md).

Native content access after owner deletion now fails with a proven runtime cause
and whole-instruction rollback, including aliases and spill-restored bindings.
Counts, immutable relationship type and detached observations remain supported;
[contract](docs/QUERY_LANGUAGE.md#content-access-after-deletion). Return2's 18
original cases pass in the focused run; this is not full-profile acceptance.

**Authorized model expansion:** the user chose native label-free nodes and
persisted heterogeneous properties instead of excluding Match4 #0004. This
FP-3/FP-6 work is in progress, not complete. The first foundation implements
native node/edge `ANY` columns, concrete tagged persistence, dynamic reads/updates,
catalog-v2 capability fencing and logical transfer. Native implicit single-label and label-free CREATE/MERGE,
dynamic property-key growth/removal and empty-label DTOs now extend that foundation:
[flexible node contract](docs/architecture/FLEXIBLE_GRAPH_V1.md).
Automatic flexible relationship types now create/extend actual endpoint pairs
transactionally, including same-statement reads. The original Match4 #0004 passes
without fixture adaptation. Fresh-store logical transfer now preserves typed and
flexible groups/maps through [artifact format 2](docs/LOGICAL_TRANSFER.md#artifact-v2-flexible-models-and-relationship-groups),
including endpoint remapping and resumable private publication. Existing-target
flexible/no-PK copy now preserves distinct equal-valued nodes through native target
bindings and remapped endpoints, with atomic receipts; Pulse qualification remains
pending. [Identity contract](docs/CATALOG_COPY.md#flexible-and-no-pk-entity-identity).
Typed-group existing-target
copy now resolves logical type and endpoint pairs independently of physical names/IDs,
with atomic receipts; [contract](docs/CATALOG_COPY.md#typed-logical-relationship-groups).
Opt-in system history now preserves
model flags/logical types through scan/index, retention/compaction and backup, with
catalog bit 22 fencing old writers; this is not completion of
the entire flexible-model package.
See the [heterogeneous-property contract](docs/architecture/HETEROGENEOUS_PROPERTIES_V1.md)
for current index/embedding restrictions and outstanding qualification. Preserve typed-table constraints,
actual empty labels, original value types, atomic schema/data writes and existing
durability/concurrency/nonfinite-storage safeguards. Audit related V1 divergences
into a versioned successor profile; do not rewrite old expectations or receipts.
See the [complete acceptance scope](docs/specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties).

The three recorded MATCH/WHERE blockers are corrected: heterogeneous per-row
property reads and exact native error-phase/category evidence. All 26 required
cases pass (two original, 24 schema-adapted), without new exclusions. Broader
MATCH and remaining FP packages still need qualification. The combined MATCH /
MATCH-WHERE owner diagnostic now records 363 required passes (246 original,
117 schema-adapted), zero execution failures and one fixture-admission blocker out of 364
required cases. Native scope categories respect declaration order; re-matched
relationships preserve incoming identity, including optional/path reads, and
duplicate names in connected read patterns refuse statically. Bound-edge direct
access and broader bound-list patterns remain pending. No failing case was
excluded. See [FP-3 progress](docs/conformance/FP3_PROGRESS.md).

Inline relationship maps now use native equality predicates, including every
edge of a bounded range, before OPTIONAL null extension. Named paths and
predicate/comprehension composition are supported; existing query budgets and
whole-statement rollback are retained. Property-based traversal pruning remains
access-path work, not a claim of this syntax completion.

Read patterns also accept `<-->` / `<-[r]->` as undirected segments and the
optional colon in `:R|:S`. Stored edge orientation, cycle/self-loop multiplicity,
directed neighboring segments and single-direction write admission are retained.
Bare pattern-map parameters carry an explicit native parsing error category;
parameters inside literal maps remain supported. No frozen case was rewritten.

Empty explicit relationship intervals (`*2..1`, `*1..0`, `*..0`) now return no
paths without traversal, preserving upstream effects and OPTIONAL/aggregate
semantics. Both bounds remain independently limited to `0..30`, including local
pattern expressions and caller-built ASTs. Malformed negative/missing-star syntax
has an explicit native error category. Match5 #0025–#0029 now pass through
input-derived setup schema and native runtime-bound writes. Match4 #0004 now
passes without adaptation using the authorized flexible model; broader model
integration remains pending.

Standalone node MERGE now emits all existing matches per input, including
unlabeled searches across node tables. Only an empty match set creates a node,
with native unlabeled creation for an untyped pattern and typed creation for an
explicit label. Empty maps match all candidates, and matching does not demand
unspecified creation columns. Read transactions refuse write plans before
execution even when the pipeline is empty or mutation-free. Remaining MERGE
pattern shapes and indexed MERGE access are not declared complete.
See [semantics](docs/QUERY_LANGUAGE.md#node-merge-matching-and-creation) and
[acceptance evidence](docs/conformance/FP3_PROGRESS.md#standalone-node-merge-multiplicity-and-typed-creation).

Bound polymorphic SET/DELETE now validate each actual row schema. Relationship
CREATE/MERGE resolves the declared member for the actual bound endpoint pair,
then runs the unchanged catalog/identity/visibility/OCC proofs. Late missing-pair
or type failures roll back the whole statement. Typed setup fixtures may infer
property/addition types and correlated endpoint pairs without rewriting input
queries or consulting expected results. See [runtime write contracts](docs/QUERY_LANGUAGE.md#writes-through-polymorphic-bindings).

WITH now retains structurally proved entity/list kinds through coalesce and
CASE, and literal ordered relationship lists can constrain ranged MATCHes.
Written ranges have a distinct relationship-list binding kind; explicit subquery
imports retain it. NULL alias provenance is preserved across renamed projections.
The four recorded expression-binding failures pass; broader provenance/access
paths and supplemental FP-3 obligations still require completion.

Node-label predicates now execute natively. The latest expression-owner
diagnostic passes all **69 required FP-3 expression cases** (38 original,
31 explicitly schema-adapted); broader clause/pattern, type and integration
packages remain incomplete. The frozen ledger is unchanged. See
[label and runner evidence](docs/conformance/FP3_PROGRESS.md#node-label-predicates-and-expression-owner-checkpoint).

Pattern comprehensions now have native correlated list execution, hygienic local
scopes, typed path/entity results, statement rollback and materialization limits.
All five required Pattern2 cases pass with recorded fixture-schema adaptation;
this does not close broader FP-3 or the full profile. See [FP-3 execution evidence
and known limits](docs/conformance/FP3_PROGRESS.md#pattern-comprehension-native-execution).

**Authorized; checkpoint A recorded, FP-2 and its FP-3 dependencies in progress.** The user requested a concrete plan for
the six remaining functional fronts plus the unresolved TCK inventory. The
[functional parity specification](docs/specs/FUNCTIONAL_PARITY_PLAN.md) defines
scope, dependencies, acceptance, Pulse migration and checkpoints. This section is
the single status authority; the specification is not a second execution backlog.
No release number, branch change or publication is implied by plan approval.

| Order | ID | Planned delivery | Status |
|---|---|---|---|
| 1 | FP-1 | Frozen capability/case matrix; stateful fixture/write/error/effect TCK runner | Implemented and qualified. Frozen V3 preserves all 3,897 upstream IDs: 3,896 required passes and one authorized lists-of-maps divergence. Stateful runner and native accounting are in the complete 25,077-pass regression; no oracle rewrite. |
| 2 | FP-2 | Numeric/postfix syntax, chained comparisons, rand and faithful result-column names | Implemented and qualified in the complete required V3 profile and repository regression: numeric/postfix syntax, chained comparisons, volatile rand, original headings and expression-only NaN. [Query contract](docs/QUERY_LANGUAGE.md). |
| 3 | FP-3 | Qualified entity UNION output, polymorphic patterns and named variable-length paths | Implemented and qualified: detached entity identity, bounded polymorphic/named paths, flexible/ANY models, unlabeled/multiple labels and supported history/copy/transfer consumers. Original V3 cases and supplemental maps 1–5 pass; installed format/consumer evidence is in the final audit. [Entity contract](docs/ENTITY_VALUES.md). |
| 4 | FP-4 | Returning/unit read-write subqueries and variable-import rules | Implemented and qualified: returning/unit writing CALL and UNION, native private phases, explicit/star/leading-WITH imports, independent readers/writers and shared statement rollback. V3, supplemental maps 6–11 and affected installed Pulse checks pass. [Write contract](docs/specs/WRITE_SUBQUERIES_V1.md). |
| 5 | FP-5 | Temporal functions/operations and exact native stored values | Implemented and qualified: all six native temporal families, query operations and exact supported persistence/consumers. All 1,004 original temporal cases pass; combined 60-case installed type checkpoint and supplemental maps 12–13 pass. [Temporal contract](docs/TEMPORAL_VALUES.md). |
| 6 | FP-6 | DECIMAL and typed LIST/MAP/ARRAY/STRUCT persistence and interoperability | Implemented and qualified: exact DECIMAL and typed LIST/MAP/ARRAY/STRUCT with supported indexes, history/copy, text and columnar consumers; installed checkpoint C and supplemental maps 14–17 pass. Unsupported combinations remain explicit in the [type matrix](docs/TYPE_SUPPORT.md). |
| 7 | FP-7 | Authorized transaction-scoped writing procedures | Implemented and qualified: native read/write/unit calls, value/entity signatures, bounded recursion/effects and permissioned query/schema authority. All 52 original procedure cases and supplemental maps 18–20 pass. [Schema contract](docs/specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md). |
| 8 | FP-8 | Final conformance accounting, paired Pulse validation and public contracts | Delivered: complete native regression/profile/supplemental maps, final paired Pulse API/UI/MCP and documentation checks pass; all 16 Ladybug observations recorded. Neo4j execution explicitly deferred by the user, not counted as passed (`FP-NEO4J-DEFERRED-20260913`). [Final qualification](docs/reports/FP_FINAL_NATIVE_QUALIFICATION.md). |

Latest FP-3/4 follow-up: native entity `keys()` and dynamic string-key property
reads, plus property `REMOVE` using the atomic SET path. The
[property-access evidence](docs/conformance/PROPERTY_ACCESS_PROGRESS.md) preserves
the owner-wide baselines and current regression receipts. The subsequent
[independent namespace increment](docs/specs/GRAPH_NAMESPACES_V1.md) implements
kind-qualified catalog/native queries and relationship-type predicates, bringing
the FP-3 original required cases to 528/528. The subsequent consumer increment
implements kind-qualified copy/transfer/resume/history and full-text ownership;
transfer format 3 and copy digest domain v3 preserve overlapping names. Qualified
hybrid search and 24 installed catalog/WAL admission scenarios pass. Vector
maintenance/missing-artifact repair is covered by the subsequent increments above;
remaining consumers/artifact importers and Pulse qualification remain open. This
is not package acceptance.
The [conditional MERGE increment](docs/specs/MERGE_ACTIONS_V1.md) implements
`ON CREATE SET` / `ON MATCH SET` property actions and enumerates every matching
bound-endpoint relationship. Original MERGE family: 51 passes, 23 required failures
and one retained divergence failure; all 75 selected cases executed. This is not
completion of FP-4. The subsequent [whole-map SET increment](docs/specs/SET_PROPERTY_MAPS_V1.md)
adds replacement/overlay from maps and native entity properties, including MERGE
actions. Original SET: 37 passes/16 required failures; original MERGE: 55 passes,
19 required failures/one retained divergence. The nested-property negative case
is recorded explicitly without changing the frozen profile. Label changes, broader MERGE pattern forms,
existential forms and remaining error/effect cases stay
required; these are not covered merely by the completed writing-CALL/UNION work.
Map-update source qualification: 996 affected tests passed, including independent
reader/writer behavior, index/history checks and subprocess COMMIT/recovery cuts.
This does not replace the remaining full-profile and installed Pulse qualification.
The subsequent full FP-4 owner refresh executes all 290 selected cases: 201 pass,
84 required failures and five retained divergences. All 147 previously passing
post-SQ-13 owner cases remain passing; the owner is not complete.
The subsequent [DELETE increment](docs/specs/DELETE_EXPRESSIONS_V1.md) implements
expression/path targets, freezes predicates/targets before deletion and passes
all 41 original DELETE cases. It closes that family without reclassifying cases;
label mutations, broader MERGE and existential forms remain required.
Its 359-test affected regression and separate public-plan ownership test pass,
including four subprocess crash/recovery cases. Predicate-free direct node scans
remain streaming; other delete inputs obey the existing memory bound. The earlier
memory/spill regression is fixed without relaxing its test or configuring a larger budget.
The subsequent [written-pattern increment](docs/specs/WRITE_PATTERN_CONTRACTS_V1.md)
adds native named CREATE/MERGE paths, explicit binding/type/direction/null errors
and a private phase boundary before MERGE reads preceding mutations. Grouped
regression: 1,023 passes. CREATE: 74 passes/two required failures/two retained
divergences; MERGE: 67 passes/seven required failures/one retained divergence.
These family receipts do not replace the remaining owner/full-package acceptance.
The final 168-case namespace/import/diagnostic/fault collection also passes,
including four new pure/NumPy crash cases proving that mutation-to-MERGE private
phases and captured-path writes share one durable COMMIT outcome.

The follow-up implements bound-edge undirected MERGE and native
`startNode()`/`endNode()`; its MERGE family now passes 70/75 (four required
label-action failures and one retained divergence). It also corrects native
write-binding loss after externally sorted WITH projections. Existing snapshot
identity validation, OCC, transaction rollback and memory limits remain mandatory.
This is the next increment of the same [contract](docs/specs/WRITE_PATTERN_CONTRACTS_V1.md),
not completion of the owner/full-package gate or a new roadmap scope.
Final affected regression: **1,016 passes, zero failures/errors/skips**, including
pure/NumPy directed/undirected MERGE and endpoint-write crash recovery. The initial
two sequential-action regressions were corrected and the entire collection rerun;
the contract preserves both failed and passing receipts. Documentation and public
API/configuration checks pass. Match4 #0004 is separately reconfirmed natively.
The subsequent large-CREATE increment implements flat ordered native instruction
programs and separates 1,024 pipeline clauses from the existing 64 UNION/action
limits. Lexer admission is 32,768 tokens under the unchanged 65,536-character
source ceiling. Both original large-CREATE scenarios now pass; all 76 required
CREATE cases pass, with only two retained multiple-label divergences. Expression/
physical-plan depth and transaction/write quotas remain enforced. No independent
commits, source rewriting, storage format or new connection setting is introduced.
Large-CREATE qualification: **947 grouped regression passes** and **14 focused
passes**, including four pure/NumPy subprocess recovery cuts. All documentation,
API/configuration and changed-source Ruff checks pass. The new complete FP-4 owner
receipt records **245 passes / 40 required failures / five retained divergence
failures**, with no loss among its previous 201 passes. Pending required cases:
29 label mutations, ten existential subqueries and one nested-storage negative
oracle conflicting with the authorized broader model. Several required label
actions also imply multiple labels; the model/profile conflict is recorded, not
silently waived. [Receipts and remaining scope](docs/specs/WRITE_PATTERN_CONTRACTS_V1.md#large-create-qualification-receipts).

The later [EXISTS increment](docs/specs/EXISTS_SUBQUERIES_V1.md) closes those ten
existential cases. Its full owner run has **255 passes / 30 required failures /
five retained divergence failures**, with no loss of any previous 245 pass.
Label mutations and the nested-storage oracle conflict remain pending; this
does not claim completion of general MERGE, FP-4, Pulse qualification or full parity.

These packages detail CMP-Q/T and the existing extension/type contracts; they do
not reopen completed bounded rounds or silently add HA, server security, drivers
or other unapproved model changes. The later explicit flexible-model authorization
is recorded below. Multi-reader/multi-writer, WAL/durability and agnostic Pulse
Core boundaries remain mandatory. Checkpoints: matrix freeze after FP-1; grouped
query/transaction validation after FP-4; format/upgrade validation after FP-6;
final severe regression after FP-8. Focused tests run throughout. No moving
percentage performance gate or rewriting TCK expectations to obtain a pass.

FP-1 working evidence and reproduction: [conformance tooling](docs/conformance/README.md).
The [checkpoint A decision](docs/conformance/CHECKPOINT_A.md) binds all 3,897 cases
and 15 step families: 3,470 required, 427 reviewed source-based divergences within
the plan's explicit model exclusions, never inferred from test failures. Native
observation uses independent stored-row scans and durable reopen. All 22 supplemental
feature/value/consumer contracts remain mandatory. This is not completed functional
parity; FP-2–FP-8 and all required native failures remain pending.
Latest focused fixture/oracle plus prior query/procedure regression: 179 passed,
zero failures/errors/skips. Actual CALL reference diagnostic: 19 passed, 25 failed,
8 not run; named-tree diagnostic: 10 failed after fixture load, 9 not run for
incompatible endpoint-table families. Failures remain visible. Neo4j CE 5.26.0 image
digests are pinned for later comparison, not claimed as executed comparative evidence.

FP-2 [working evidence](docs/conformance/FP2_PROGRESS.md): all five original
column-heading failures now pass unchanged expectations; combined numeric,
heading/postfix, cache and public-boundary checkpoint passed 187 tests. Required
negative-contract failures and cross-package case closure remain open; this is
not package acceptance or a full regression of the current development line.

The subsequent combined FP-2 checkpoint passed **728 tests** (zero failures,
errors or skips), including comparison chains, rand occurrence identity and
architecture boundaries. All **64 original Quantifier9–12 cases** also passed
stateful execution after the rand grouping fix. Precise negative contracts and
later-package dependencies still prevent FP-2/full-profile acceptance.

The publication checkpoint combines the feature and error-phase suites:
**1,048 passed**, zero failures/errors/skips. All **44 original map cases** now
pass with explicit native type-error phase evidence; other required-case gaps
remain open. See the working evidence above for scope and receipts.

The next FP-2 correction makes boolean operand types strict (known types at
planning, parameters before effects, dynamic types at evaluation) and prevents
BOOL/INT64/DOUBLE literal memo collisions in grouped projections. The focused
query/planner/compiled-predicate regression passed **667 tests**; the original
boolean family records **141 passes**, with remaining cases/dependencies retained.
The owner-wide diagnostic and durable zero-effect-write checks are recorded in
the FP-2 working evidence. The explicit NaN decision is now implemented: expression
NaN is supported, nonfinite storage is rejected, and all eight original NaN
comparison cases pass without changing the frozen required-case inventory.

Further FP-2 closure: empty map keys, explicit scope/aggregation error evidence
and bounded streaming `UNWIND range` are implemented. All **149 required boolean
family cases** pass; the original million-span/3,000-row sum also passes without
materializing the full list or raising its quota. The combined map/scope/range
regression passed **587 tests**, including cursor cleanup and write rollback.
This does not close the full FP-2 package: entity/value dependencies and remaining
required cases stay tracked in the same working evidence.

The next FP-2 increment implements evaluation-phase RANGE operand errors and
admits long finite DOUBLE literals with a bounded 2,048-character numeric token.
The unchanged original families now pass **67 range cases** and **27 float cases**.
INT64 pre-conversion overflow admission, non-finite refusal, quotas and statement
rollback remain enforced. Detailed focused regression evidence is tracked in
[FP-2 progress](docs/conformance/FP2_PROGRESS.md); overall FP-2 remains in progress.

IN now validates static/bound/dynamic right-hand types with explicit phase evidence;
all **46 original List5 cases** pass and the membership/seek/predicate regression
passes **283 tests**. `WITH *` and mixed star/explicit projections are implemented
through native lexical scope expansion, preserving the existing projection cap,
grouping and rollback. Conversion cases previously stopped by its grammar now
reach the still-pending FP-3 polymorphic multi-MATCH boundary; they are **not**
reported as passing. See the same progress record for feature/regression evidence.

FP-3 now expands standalone label-free reads through multiple MATCH patterns,
UNWIND, inline maps, OPTIONAL MATCH, WITH aliases and returning read subqueries.
Rematching a bound entity preserves its identity instead of scanning again; an
optional NULL cannot be rebound. The three conversion cases above now pass with
explicit fixture-schema adaptation: the family reports 21 original passes,
four adapted passes and 22 selected fixture blockers. See
[FP-3 evidence and remaining work](docs/conformance/FP3_PROGRESS.md). This starts
FP-3; it does not complete its public-entity/path/UNION or indexing requirements.

## Authorized query-language compatibility round

Status: **all eight implemented, tested and documented; released in 0.0.6 on September 13, 2026**.
The user authorized items 1–8 in order,
including breaking query semantics with coordinated Pulse migration and no legacy
mode. The fixed reference, scope, item-by-item acceptance criteria and reproducible
TCK inventory are in [the compatibility contract](docs/CYPHER_COMPATIBILITY.md).
No item is considered closed by parser acceptance alone. Final DoD includes feature
tests, severe regression of Grafx and affected Pulse consumers, and updated roadmap,
capabilities, configuration and API documentation.

Validated implementation and consumer contracts: [ordered pipelines, scope, lists,
UNION/subqueries, tabular procedures and write boundaries](docs/COMPOSABLE_QUERIES.md).
Added failure evidence covers logical-statement rollback through public result
validation and independent procedure-stream cleanup. The final complete Grafx
regression passed **17,174 tests, zero failures/errors, 19 attributed skips** in
2,545.47 s. Installed-wheel validation passed 150 feature tests, plus four real
Pulse owner-query tests; 208 Python files matched source/wheel/installed bytes.
The affected Community regression passed 191 tests and Core stale-sweep units
passed 23 tests. Pulse's source-reference queries and Community dependency migrate
together, without Grafx imports in Core or a legacy semantics switch.
The [consumer integration follow-up](docs/reports/PULSE_V006_INTEGRATION_REGRESSION.md)
removes the shared Core bootstrap's legacy dependency and validates actual source
lifecycle writes and routed natural search: **1,575 Community, 111 Core and 252
frontend tests passed**, plus six repeated cases against the installed paired
wheels. No failures/errors/skips in those final batches. Remaining direct legacy test-helper
imports are not a claimed full Core-suite pass. This closes the documented finite round, **not full
Cypher/TCK conformance**. See the [acceptance record](docs/reports/V006_QUERY_LANGUAGE_ROUND.md)
for exact receipts, scope, reference failures and remaining language limitations.

## Index

- [Functional parity expansion plan](#functional-parity-expansion-plan)
- [Authorized query-language compatibility round](#authorized-query-language-compatibility-round)

September 10, 2026 — `okto-grafx==0.0.5` published on PyPI using the same wheel
validated and installed in Pulse. See the [publication receipt](docs/reports/PYPI_0_0_5_PUBLICATION.md).
Publication and merge/tag are separate events; the 0.0.5 main/tag baseline above
was independently verified in Git when preparing the 0.0.6 checkpoint commit.

September 10, 2026 — approved 0.0.5 default-acceleration update: NumPy and
google-crc32c move to base dependencies; `[accel]` remains a compatibility alias.
New connection codec/vector-math defaults become `numpy`, with explicit pure
overrides preserved. No persistent format or concurrency/durability change.
Pulse Community Settings derives these defaults from Grafx; saved overrides
are preserved. Targeted Windows validation: 574 regression tests, 50 bootstrap
tests (including mixed pure/NumPy snapshots and durable reopen), and 370 initial
configuration/codec/vector/packaging checks passed; these overlapping batches are
not a full release-wide regression. See [configuration](docs/CONFIGURATION.md).

- [Rules and status vocabulary](#rules-and-status-vocabulary)
- [Current delivery boundary](#current-delivery-boundary)
- [Latest authorized eight-item follow-up](#authorized-follow-up-after-204bd2e)
- [Native-history consumer and access-path round](#native-history-consumer-and-access-path-round-nhc-18)
- [Approved capability continuation after 4ee4d2e](#approved-capability-continuation-after-4ee4d2e)
- [Approved continuation after a4dd85a](#approved-continuation-after-a4dd85a)
- [Approved continuation after 970aa1e](#approved-continuation-after-970aa1e)
- [Approved continuation after 7dde256](#approved-continuation-after-7dde256)
- [Approved continuation after 69ed311](#approved-continuation-after-69ed311)
- [Approved continuation after 3f3819f](#approved-continuation-after-3f3819f)
- [Search and resumable-transfer follow-up](#search-and-resumable-transfer-follow-up)
- [Approved four-item follow-up](#approved-four-item-follow-up)
- [Proposed next round after the 0.0.5 checkpoint](#proposed-next-round-after-the-005-checkpoint)
- [Next proposed round after N1–N4 closure](#next-proposed-round-after-n1n4-closure)
- [Known limitations and corrective work](#known-limitations-and-corrective-work)
- [Comparative feature gaps and minimum parity](#comparative-feature-gaps-and-minimum-parity)
- [Operational checkpoint after R1–R4](#operational-checkpoint-after-r1r4)
- [Remaining performance work](#remaining-performance-work)
- [Next iteration assessment: feature/v0.0.5](#next-iteration-assessment-featurev005)
- [Database capabilities and dependencies](#database-capabilities-and-dependencies)
- [CAP-2 through CAP-4 opportunity assessment](#cap-2-through-cap-4-opportunity-assessment)
- [Optional agent product](#optional-agent-product)
- [Completed foundations](#completed-foundations)
- [Legacy requirement register](#legacy-requirement-register)
- [Deferred and rejected directions](#deferred-and-rejected-directions)
- [Acceptance and maintenance](#acceptance-and-maintenance)

## Rules and status vocabulary

1. Preserve legitimate multi-process/multi-thread read/write access, snapshots,
   both OCC validations, writer fencing, WAL order, durability and fail-closed
   validation. Performance does not authorize weaker guarantees.
2. Keep the database generic. Grafx-specific Pulse behavior belongs in Community
   adapters/composition; Pulse Core remains backend-agnostic.
3. Fix demonstrated defects first. Prefer high-impact bounded work over repeated
   marginal profiling. Group regression tests after cohesive small changes;
   semantic/durability failures are blockers, historical timing ratios are not.
4. One active status here; specs define contracts and reports supply evidence.
   A specification, internal codec or passing isolated test is not a shipped API.
5. No release date or complete version scope is promised by a row below. Version
   0.0.4 was published with the operator; later release approval remains explicit.

| Status | Meaning |
| --- | --- |
| Implemented checkpoint | Named implementation/evidence exists; scope is bounded, not universal certification |
| Partial | Some foundation exists; remaining work is mandatory before consumer exposure |
| Planned | Accepted product direction, not currently callable |
| Open limitation | Present constraint or unclosed acceptance requirement |
| Historical / revalidate | Older finding retained; do not assert it still reproduces without checking current source |
| Deferred / rejected | Not an active implementation task; reopening needs evidence/decision |

## Approved capability continuation after 4ee4d2e

### Authorized follow-up after 204bd2e

The operator approved the following fixed sequence with **one final delivery only
after all eight meet feature tests, grouped regression and consumer documentation**.
Interim observations are progress reports, not partial acceptance or new scope.

| Order | Work | Status |
| --- | --- | --- |
| 1 | Native system-time activation, atomic current/history publication, WAL and recovery | Complete in development; native crash/repeated recovery and regression validated |
| 2 | Typed as-of/versions and historical delete/recreate/relationship semantics | Complete in development; typed APIs, lineage/schema and snapshot tests validated |
| 3 | History verify, backup/restore, explicit transfer rules and protected retention | Complete bounded payload-redaction slice; pins/backup/cuts validated; no physical compaction claim |
| 4 | JSON CLI inventory for catalogs/workspaces | Complete in development; explicit-root/read-only CLI contracts validated |
| 5 | Bounded selected catalog copy and explicit skip-conflict policy | Complete bounded slice; indexed node selection, bounded fallback and skip-node receipts validated |
| 6 | Durable positional FTS postings for eligible phrase queries | Complete opt-in bit 16; phrase/snapshot/rebuild and damage/verification validated |
| 7 | Bounded, correctly invalidated posting_hash decode memo | Complete; immutable content identity, mutation refusal and bounds validated |
| 8 | 0.0.6 real-package upgrade/old-reader/accelerator compatibility matrix | Complete local ten-cell real-wheel matrix; 203 source modules match; remote platform workflow defined, not claimed executed |

**DoD closed for all eight together.** Broad regression plus explicit corrective
reconciliation: 16,828 distinct passing cases, 19 attributed skips, no unresolved
failures. Final grouped delivery run: **1,306 passed, zero failures/skips** (151.05 s).
Consumer documentation, generated API contracts, 39 connection fields, links and
source/wheel parity passed validation. Overlapping test batches are not added.
This closes the fixed round, not every remaining CAP-2/CAP-3/CAP-5 product ambition.

No release, installation in Pulse or mutation of production stores is implied.
Consumer docs: [system time](docs/SYSTEM_TIME_HISTORY.md), [CLI](docs/CLI.md),
[copy](docs/CATALOG_COPY.md), [FTS](docs/FULL_TEXT_SEARCH.md),
[posting memo](docs/POSTING_HASH.md), [matrix](docs/V006_COMPATIBILITY.md).
[Round evidence](docs/reports/V006_NATIVE_HISTORY_ROUND.md) tracks final acceptance.

### Native-history consumer and access-path round (NHC-1–8)

**Complete in development on feature/v0.0.6; local DoD satisfied.** These are slices of the
existing roadmap, not additional acceptance requirements for the completed round.
Effort is relative; benefits below are expected, not measured speedups.

All eight implementations, feature/crash tests and consumer documentation are
complete. Final affected regression: **269 passed**; final corrective regression:
**2,774 passed**, covering all nine failures found by the initial full run
(16,929 passed, 19 attributed skips). No failure remains unaccounted for; the
initial full run is not relabeled as green. The real-wheel matrix passed all
14 cells, with 207 packaged modules matching the final source. No Pulse install,
commit/push or package publication is implied by this implementation checkpoint.
[Scope, evidence and explicit limits](docs/reports/V006_NHC_ROUND.md).

| Order | Existing initiative | Completed bounded delivery | Effort / expected benefit |
| --- | --- | --- | --- |
| 1 / NHC-1 | GX-CAP-5 | Typed bounded return of matched token positions/field identities, using the existing positional evidence and owning snapshot. Token positions are not original-text character offsets. | Low–medium; enables explanations and consumer highlighting integrations without a second search. |
| 2 / NHC-2 | GX-CAP-5 | Explicit ordered proximity/slop for phrase search, built on item 1 and durable positional chunks; fixed work/output bounds, repeated-term and field-boundary semantics. | Medium; more flexible text retrieval, no universal latency improvement promised. |
| 3 / NHC-3 | GX-CAP-2 | Opt-in endpoint closure for explicitly selected relationship copies, within the same source snapshot and aggregate budgets; still one-store target COMMIT/receipt. No recursive arbitrary traversal or merge. | Medium; eliminates caller-side endpoint lookup/remapping orchestration and incomplete selections. |
| 4 / NHC-4 | GX-CAP-4 on the delivered CAP-3 slice | Typed bounded same-store diff between two retained system-time commits: nodes, relationships, properties and explicit schema changes, preserving delete/recreate identity. No valid-time or bitemporal writes. | Medium; immediate audit/change inspection from existing history; initial scan cost remains explicit. |
| 5 / NHC-5 | GX-CAP-3 / PERF-SCALE | Optional version access path keyed by table/record identity and commit, with activation/build, native update/recovery, verification and validated fallback contracts. First accelerate system_versions, not all temporal queries. | High; avoids whole-history decoding for one lineage when the access path is eligible. Operation-count evidence required. |
| 6 / NHC-6 | GX-CAP-3 / PERF-SCALE | Bounded indexed as-of reconstruction, extending item 5 with a proved baseline/interval access path; preserve historical schema, endpoints, pins and fail-closed validation. | High; reduce repeated replay of irrelevant historical events; result-size and validation costs remain. |
| 7 / NHC-7 | GX-CAP-5 / OPS-5 | Explicit same-name analyzer replacement through a prepared new index generation and atomic publication, retaining old-reader safety and crash recovery. No in-place reinterpretation of existing postings. | High; operational evolution of search without manual drop/recreate gaps. |
| 8 / NHC-8 | GX-CAP-3 / OPS-3 | Explicit quiescent physical history compaction after retention, preserving horizons, pins and lineage with atomic replacement, recovery and backup rules. No online compaction or erasure of old WAL/backups. | High; reclaim history-file space that current payload redaction does not reclaim. |

Items 1–4 consume the delivered foundations; 5–6 address history growth and depend
on controlled persisted-format integration. Item 7 reuses index lifecycle; item 8
requires its own replacement/recovery proof. None relaxes multi-reader/writer,
both OCC checks, durability or consistency. Each implementation keeps the same
feature/regression/documentation DoD; no marginal performance gate is introduced.

### Previous checkpoint

The subsection below records the historical state **at `204bd2e`**. Its pending
native-history/CLI/selected-copy/positional-posting statements are superseded by
the authorized follow-up above, not a second active delivery queue.

Approved September 10, 2026, on `feature/v0.0.6`; these are **not** the already
completed minimum-parity MP-1–MP-8. Fixed order and DoD: feature tests, grouped
affected regression, roadmap plus capability/configuration/API docs. No Pulse
installation, PyPI release or production store mutation is implied.

| Order | Existing initiative | Bounded delivery | Current status |
| --- | --- | --- | --- |
| 1 | GX-CAP-2 | CatalogSession: aliases, ownership/permissions and single-store pinned transactions | Complete in development; grouped affected regression passed |
| 2 | GX-CAP-2 | Optional explicit-root/allowlist/bounded-marker workspace resolver | Complete in development; grouped affected regression passed |
| 3 | GX-CAP-2 | Bounded copy/promotion into an existing target, atomic data plus durable idempotency receipt | Implemented and tested: bounded PK-table/fail-policy slice; broader copy policies remain outside this slice |
| 4 | GX-CAP-5 | Positional full-text phrase search | Implemented and tested: exact analyzed phrase verification; durable positional postings/position-return APIs remain open |
| 5 | GX-CAP-10 | Read-only logical views | Implemented and tested: bounded typed base-table views, snapshots and atomic definitions. [Evidence](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md) |
| 6 | GX-CAP-10 | Nullable column addition with old-row/schema compatibility | Implemented and tested: typed append-only column API, required bit 13 and exact prior layouts. [Evidence](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md) |
| 7 | OPS-5 / PERF-SCALE | Repeated-key posting layout, based on confirmed hotspot evidence | Implemented and tested: opt-in `posting_hash`, bounded same-key INSERT preparation, native snapshots/recovery/maintenance. [Usage](docs/POSTING_HASH.md), [acceptance](docs/reports/V006_POSTING_TEMPORAL_CHECKPOINT.md) |
| 8 | GX-CAP-3 | Opt-in durable system-time history, first persistence checkpoint | Partial internal foundation: typed event codec, bounded immutable append-image plans and complete append-transition validation tested. **Not connected to native commit/recovery, not a consumer history API.** [Exact remaining boundary](docs/specs/SYSTEM_HISTORY_APPEND_DRAFT.md) |

Items 1–2 share **437 passing affected regression tests** (51.06 s, Windows).
[Acceptance evidence and exclusions](docs/reports/V006_CATALOG_WORKSPACE_ACCEPTANCE.md).
The subsequent 1–4 checkpoint passed **916 grouped tests** (241.58 s), including
an inherited scalar import-contract correction. [Evidence and explicit boundaries](docs/reports/V006_COPY_PHRASE_ACCEPTANCE.md).
The 1–6 expanded checkpoint passed **7,535 tests** (994.36 s), followed by
**155 supplemental tests** (144.57 s) after the virtual-NULL cache-admission fix.
Counts overlap and are not added. [Commands, crash cuts and remaining work](docs/reports/V006_VIEWS_SCHEMA_CHECKPOINT.md).
The item-7 checkpoint passed **6,830 affected regression tests**, followed by
**1,314 final supplemental tests** after reference-preflight and documentation
corrections. Item 8 has a tested internal prototype, not native history recording.
[Exact build/test boundaries and remaining delivery](docs/reports/V006_POSTING_TEMPORAL_CHECKPOINT.md).
At that historical checkpoint, the entire eight-item delivery was **in progress**. Items 1–2 introduce no
persistent format changes or new connection flags. Public consumption
and limits: [catalogs/workspaces](docs/CATALOGS_AND_WORKSPACES.md). Their completion
does not close the full CAP-2 spec: arbitrary subgraph selection and CLI inventory remain
separately tracked. [Copy consumption and limitations](docs/CATALOG_COPY.md),
[phrase semantics](docs/FULL_TEXT_SEARCH.md#exact-analyzed-phrases-006-development).
A snapshot or commit journal is **not** retained graph history.
Existing logical transfer into a fresh destination does **not** satisfy item 3's
atomic update and replay contract for an existing destination.

Persistence work must freeze exact capabilities, bytes, recovery/refusal and
backup/transfer behavior before claiming completion. This is the existing spec's
prerequisite, not a new performance gate. No marginal timing target is added.

## Current delivery boundary

| ID | Status | Deliverable / exit condition |
| --- | --- | --- |
| V006-HISTORY-CATALOG-SEARCH | All eight approved items complete; released in 0.0.6 | Native temporal publication/query/operations, explicit catalog/workspace CLI, selected copy/skip, positional FTS, posting decode memo and real-wheel matrix. Feature/regression/corrective tests and consumer documentation validated. [Exact evidence and limitations](docs/reports/V006_NATIVE_HISTORY_ROUND.md). Published in 0.0.6 on September 13, 2026; Pulse installation is not implied. |
| REL-004 | Published September 8, 2026 | [PR #2](https://github.com/OktoLabsAI/okto-grafx/pull/2) merged; tag `v0.0.4` points to `425362a`. Wheel/sdist built from the tag, Twine validation, 149 package-file parity checks, isolated consumer smoke and public PyPI install smoke passed; remote SHA-256 values matched. [PyPI 0.0.4](https://pypi.org/project/okto-grafx/0.0.4/). GitHub Actions could not start because of account billing; local evidence does not certify the remote/platform matrix. |
| DOC-1 | Implemented in this documentation refactor | One README entry point, public API/configuration/query/operations references, measured-performance table, one roadmap and a preserved source archive. Links, examples, API/config field coverage and preservation hashes are checked; [validation receipt](docs/reports/DOCUMENTATION_REFACTOR_2026_09_08.md). |
| CAP-1B | Implemented checkpoint | `6b5163e`: native journal preflight is connected to page application/checkpoint; UUID, activation/COMMIT coverage, target/resident LSN and file extents validated. 2,433 tests / 77.27 s recorded; no new typing diagnostics in the isolated comparison. This is recovery correctness, not a latency benchmark. |
| GX-CAP-1 remainder | Implemented and locally validated in 0.0.5 development | Metadata-at-begin/retry, qualified lookup/paging, full journal verification, metrics and coordinated transfer/restore/fork identity semantics. [Consumer contract](docs/COMMIT_HISTORY.md), [acceptance evidence and limits](docs/reports/V005_N3_N4_ACCEPTANCE.md). Opt-in activation is not a production rollout or release. |
| PULSE-005 candidate | Local validation passed; product evolution paused by user | `8c3f6f2`: one complete Windows/Python 3.13 regression passed (16,228 passed, 18 POSIX skips, zero failures), plus clean wheel/sdist builds and isolated installed consumption. [Receipt and D: storage-latency caveat](docs/reports/V005_PRE_PULSE_VALIDATION.md). Next step is controlled Pulse installation/testing with the exact validated wheel and `[accel]`, not more Grafx capability work. No live installation, data operation or publication has occurred in this validation. |
| PULSE-005 adoption | First Community batch installed; broader adoption pending | Exact candidate installed with `[accel]` in both local Pulse environments; bounded Board readers, scoped vector reads and the complete 0.0.5 Settings catalog. 350 backend + 20 UI tests passed, frontend rebuilt and isolated installed-consumer smoke passed. Core contracts remain neutral; no real-data backfill/consolidation, no publication. [Checkpoint and remaining scope](docs/reports/PULSE_V005_ADOPTION_CHECKPOINT.md). |
| PULSE-BENCH | Reserved workload | Latest recorded authorized run consumed one spec; 19 remain reserved. Do not consolidate more, redrive, rebuild or reset data to improve this document. New live runs need a deliberate workload decision. |

## Search and resumable-transfer follow-up

### Approved continuation after a4dd85a

The completed eight-item delivery is committed and pushed as `a4dd85a` on
`feature/v0.0.5`: 16,121 distinct tests passed, 18 attributed skips, no unresolved
failures. **All eight subsequent items are implemented and locally validated:
16,228 distinct tests passed, 18 attributed skips, no unresolved failures.**
Acceptance combines grouped regression and the documented corrective reruns; it
is not a claim that every initial invocation was green. The table preserves the approved pre-implementation evidence,
not current defects or a reopening of that delivery. It reuses PERF-SCALE/MEM and GX-CAP-8/9 requirements; it does not
authorize release, production Pulse workloads or changes to concurrency/durability.

Current surface: `with_pagerank`, `with_simple_topology`, `to_networkx`,
`label_propagation`, `projection_arrow_batches`, `PolarsFrame`/`to_polars`/`import_polars`,
`TextImportLimits` and CSV/JSONL batch readers plus atomic import facades.
Checkpoint 1–4: 8 passed; combined new feature/isolation checks: 43 passed;
eight executable documentation recipes passed. Final algorithm coverage: 30 passed;
text ingestion: 48 passed; final interop/examples/isolation: 12 passed. The final
wheel matched all 185 source Python files and passed an isolated native consumer
commit/reopen smoke. No source storage/WAL/OCC change.
[Implementation evidence and final acceptance status](docs/reports/V005_AFTER_A4DD85A.md).

Implementation and evidence committed and pushed as `65ab659` on `feature/v0.0.5`.

| Order / IDs | Bounded candidate and inspected evidence | Effort | Expected value / dependency |
| --- | --- | --- | --- |
| 1 / PERF-SCALE, GX-CAP-9 | Explicit reusable PageRank transition preparation owned by the immutable projection. `_pagerank` currently recomputes normalized edge shares/dangling nodes and recreates source/target tuples and NumPy arrays per call, even with retained CSR. Retain only opt-in derived data with complete logical-memory charges, immutable ownership and backend/weight identity; do not retain personalization-specific ranks or storage authority. | Medium | Avoid repeated O(V+E) preparation when ranking one snapshot with multiple seeds. Iteration complexity is unchanged. Measure preparation separately once; stop if the real saving is marginal rather than introduce a cache merely to complete this row. |
| 2 / PERF-SCALE, GX-CAP-9 | Optional reusable simple-undirected topology. `_k_core` rebuilds neighbor sets and collapses physical parallel edges on every invocation. Retain a bounded immutable representation that algorithms can reuse, while keeping the original directed multigraph untouched. | Medium | Amortize deduplication and allocation for repeated analytics; prerequisite for item 4. Building the picture still scans V+E and k-core still performs peeling. |
| 3 / GX-CAP-8/9 | Optional NetworkX export and conformance fixtures. The spec explicitly retains graph exchange and NetworkX acceptance as outstanding; current oracles are independent local implementations, not NetworkX runs. Preserve store/table/record identity, directed physical edge keys, parallel edges, loops and captured weights in a bounded MultiDiGraph export; no arbitrary object ingestion or storage writes. | Small–medium | Developer integration and an additional independent oracle. No runtime performance promise; keep dependency lazy/optional. Compare algorithms only under equivalent graph semantics. |
| 4 / GX-CAP-9 | Bounded read-only label propagation over item 2, one algorithm from the existing second package. Specify simple-undirected/unweighted interpretation, deterministic node order and label ties, isolates, iteration/work/memory caps and explicit convergence status. Do not silently equate this with Louvain/modularity optimization. | Medium | New community discovery capability, not faster database reads/writes. Depends on 2; uses small independently checked fixtures from 3 where update policies agree. No write-back or clustering-quality guarantee. |
| 5 / GX-CAP-8/9 | Bounded Arrow batch export for detached projection nodes/edges and explicitly supplied aligned algorithm results. Current `to_arrow_batches` accepts native query results/cursors, not GraphProjection. Publish identity/endpoints/weight/result schemas and snapshot provenance; validate alignment and preserve multiplicity without first building a whole DataFrame. | Medium | Bridge analytics results into the existing tabular/Parquet ecosystem with bounded additional output batches. Does not make the projection or already-computed algorithm output streaming/O(1)-memory. |
| 6 / GX-CAP-8 | Optional explicitly typed Polars bridge over the existing Arrow contract. Current `tabular.py` exposes Pandas only. Preserve NULL versus NaN, vector metadata and exact types through an explicit metadata-bearing contract; reject metadata loss rather than infer vector identity. Whole-call import staging uses the existing native savepoint and caller commit. | Medium | Completes the other named DataFrame integration; no zero-copy or throughput promise without evidence. No LazyFrame query-engine integration or mandatory Polars dependency. |
| 7 / GX-CAP-8 | Typed bounded local CSV batch reader and atomic native import facade, outside Cypher. Reuse the trusted local-directory policy of Parquet. Declare UTF-8, header/delimiter/quote/NULL rules, exact scalar codecs and file/record/field/row/batch limits before exposing functions. Report malformed row/column; late errors roll back this call only. | Medium–large | Practical local ingestion without user-written parsers; common foundation for 8. No schema inference, globs, URLs, COPY syntax, graph backup or vector/nested data in this initial slice. |
| 8 / GX-CAP-8 | Typed bounded local JSON Lines reader and atomic native import using 7's file/codec foundation. Require one object per bounded line, explicit columns, duplicate/unknown-key and missing-versus-NULL policy, exact numeric admission and localized errors; reject arbitrary nesting and nonstandard NaN/Infinity tokens. | Medium | Another existing local JSON requirement in a finite format. JSON arrays, remote sources and query external scans remain deferred. Preserve whole-call staging rollback and caller-owned commit. |

Evidence inspected: [PageRank/k-core preparation](src/okto_grafx/projection_algorithms.py),
[NumPy adapter](src/okto_grafx/adapters/numpy_projection.py),
[projection surface](src/okto_grafx/projections.py), [Arrow boundary](src/okto_grafx/arrow.py),
[Pandas bridge](src/okto_grafx/tabular.py), [local-file policy](src/okto_grafx/parquet.py),
[external-data scope](docs/specs/SPEC-GX-CAP-8.md) and
[algorithm/NetworkX scope](docs/specs/SPEC-GX-CAP-9.md).

Recommended sequence: 1–8, checkpoint after 1–4. Each implemented slice needs
feature/failure tests, optional-dependency isolation where relevant, resource and
ownership contracts, executable usage examples and API/configuration/roadmap updates;
one grouped final regression covers the batch. These are primarily analytics and
interoperability improvements, **not evidence of faster Pulse KG loading or writes**.
No new percentage gate, persisted format, authority cache, mandatory dependency,
single-writer premise or live-data consumption is included.

### Approved continuation after 970aa1e

Candidate queue recovered after the eight-item delivery was committed and pushed
as `970aa1e` on `feature/v0.0.5`. **All eight delivered; final grouped regression
passed.** The candidate table below preserves the pre-implementation
evidence, not present-tense defects. [Current receipt](docs/reports/V005_AFTER_970AA1E.md).
The 16,046-pass preceding delivery is closed; these are not new acceptance gates for it. First
three items target demonstrated code-level costs; the others extend existing
GX-CAP-8/9 scope. Benefits are hypotheses until measured, not promised speedups.

Current delivery: immutable identity lookup and discovery-sized BFS (1), bucket
k-core (2), explicit NumPy PageRank (3), snapshot weights (4), bounded Dijkstra (5),
weighted/personalized PageRank (6), typed Arrow-backed Pandas (7), local bounded
Parquet with atomic no-overwrite publication and native import savepoint (8).
Checkpoint 1–4: 15 passed; weighted oracles: 4 passed; interop checkpoint: 10 passed.
Final consolidated acceptance: **16,121 distinct passed, 18 attributed skips,
no unresolved failures/errors**. The final documentation/public-surface rerun
after the host crash passed all 2,450 tests; completed regression reports survived
and were not needlessly rerun. Counts include overlapping focused coverage only once.
Consumer guides, operation defaults, API signatures/DTOs, capability specs and
executable examples are part of this delivery. No Pulse, release or commit/push
action is implied by this implementation approval.

| Order / IDs | Bounded candidate and current evidence | Effort | Benefit / dependency |
| --- | --- | --- | --- |
| 1 / PERF-SCALE, GX-CAP-9 | Optional immutable node-identity lookup retained with projection topology. `_bfs` in `projection_algorithms.py` uses `nodes.index` for source/target even when CSR is already retained; capture's `offsets` map is discarded. Reuse a bounded derived lookup and charge path workspace by admitted/discovered state where safe. No durable cache authority. | Small–medium | Remove repeated O(V) identity lookup for local path calls on a prepared picture; one-time O(V) construction remains. Preserve BFS order, quotas and exact paths. |
| 2 / PERF-SCALE, GX-CAP-9 | Linear bucket-based k-core peeling. `_k_core` uses heap push/pop and retained stale pairs today. Keep simple-undirected deduplication, ignored self-loops and identical core numbers; retain the current implementation as an independent comparison during validation. | Medium | Target O(V+E) peeling after simple-neighbor construction, eliminating heap's logarithmic factor and stale entries; no claim that reading E edges can be avoided. Independent of 1. |
| 3 / CONC-CPU-1, PERF-MEM, GX-CAP-9 | Explicit opt-in NumPy PageRank execution path through the optional acceleration boundary. `_pagerank` currently distributes ranks in Python for each physical edge/iteration. Keep Python as the default/reference, bound temporary numeric buffers, document finite numeric tolerance and check cancellation between kernels/iterations. Missing selected dependency must refuse, not silently fall back. | Medium | Reduce Python edge-loop overhead on larger projections; benchmark a fixed fixture, not a marginal ratio gate. Preserve multiplicity, dangling behavior and explicit non-convergence. No change to database read/write concurrency. |
| 4 / GX-CAP-9 | Optional scalar relationship weights captured in the same snapshot via projected scans. `project_graph` retains only endpoints today. Start with explicitly selected numeric columns, finite non-negative weights and an explicit NULL/missing policy; default topology stays unweighted. Charge retained values and preserve physical edge identities. | Medium | Foundation for weighted analysis without subsequent storage reads; a capability, not an automatic performance gain. |
| 5 / GX-CAP-9 | Bounded non-negative weighted shortest paths (Dijkstra), with distance/path result, direction, deterministic equal-cost ties and clear work/output/memory limits. Existing shortest_path is unweighted BFS. Do not reuse BFS depth semantics without defining hop-constrained behavior; negative weights and Bellman–Ford remain outside this slice. | Medium | Cost-aware routing/dependency analysis; depends on 4 and reuses 1. Default unweighted API remains unchanged. |
| 6 / GX-CAP-9 | Weighted and personalized PageRank: explicit seed distribution, normalization and dangling-mass policy; node-aligned results, residual/convergence and bounded inputs. `_pagerank` currently hardcodes uniform teleportation and equal outgoing-edge shares. All-zero/unknown-node/invalid weights must have explicit refusal or documented semantics. | Medium | Domain-specific relevance over a captured graph; depends on 4. Python and any selected accelerated path from 3 must agree within the published numeric tolerance. |
| 7 / GX-CAP-8 | Optional Pandas bridge through the existing typed Arrow boundary. Start with bounded DataFrame-to-batch import and explicitly materialized result-to-DataFrame export; preserve nullable scalar/vector schema and metadata without dtype guessing or silently converting NaN/NULL. Do not claim full-DataFrame export is streaming or zero-copy. | Small–medium | Easier Python analytics integration and less caller conversion code, not an engine throughput promise. Reuse whole-call native import atomicity. Polars remains a separate later slice. |
| 8 / GX-CAP-8 | Local Parquet batch read/write on the existing Arrow contract: explicit schema/vector metadata, row/byte/batch limits, caller-owned resources and localized failures. Restrict to explicitly permitted local files, no URL resolution; no overwrite by default and no visible partial final file on export failure. Feed import through the existing staging savepoint. | Medium–large | Interop with larger local datasets using bounded batches; no COPY/query external scans, portable graph backup or unbounded transaction promise. Independent of 4–7, reuses current Arrow. |

Evidence: [current algorithms](src/okto_grafx/projection_algorithms.py),
[capture](src/okto_grafx/projections.py), [Arrow implementation](src/okto_grafx/arrow.py),
[remaining graph scope](docs/specs/SPEC-GX-CAP-9.md) and
[remaining interop scope](docs/specs/SPEC-GX-CAP-8.md).
Recommended order is 1–8, with a checkpoint after 1–4. Every selected item keeps
feature/failure tests, independent reference checks when applicable, grouped final
regression, and roadmap/feature/configuration/API documentation in its DoD.
Algorithms run on detached pictures; none of this promises faster Pulse KG pages
unless that consumer actually uses the new path. No Pulse installation/data use,
reserved spec consolidation, release, storage-format change, authority bundle,
multiwriter weakening or persistent algorithm write-back is part of this proposal.

### Approved continuation after 7dde256

Approved by the user after commit `7dde256`; **all eight items are implemented and
locally validated**. Consolidated final regression: **16,046 distinct passed,
18 attributed skips, no unresolved failures**, after the corrective runs below.
Feature/failure tests, executable examples and consumer/API/configuration docs passed.
Checkpoint 1–4 passed: 30 focused scan/projection/algorithm tests in 9.00 s.
The [implementation and acceptance receipt](docs/reports/V005_AFTER_7DDE256.md)
records the final test boundary and the native vector-admission correction.
These are new bounded items, not missing acceptance from the
15,994-pass delivery below and not additional mandatory gates for its release.
Order favors reusable performance foundations before dependent capabilities.

| Order / existing IDs | Approved scope and original source evidence | Effort | Expected benefit / dependency |
| --- | --- | --- | --- |
| 1 / OPS-8, PERF-MEM, GX-CAP-9 | Public snapshot-bound physical scan with explicit column selection and bounded pages. Reuse the internal projected-read foundation in `engine/heap_store.py` (`scan_projected`/`_read_if_projected`) instead of exposing private heap access. Existing `Transaction.scan_rows_v1` retains its default full-row contract. Preserve validation of skipped payloads, record identity, cursor ownership and snapshot. | Medium | Potentially high allocation/decode reduction for wide rows/vectors when only identities/endpoints are needed; no promise to avoid physical integrity I/O. |
| 2 / PERF-SCALE, GX-CAP-9 | Use item 1 in `project_graph` with row/byte-bounded batches, cancellation/deadline boundaries and work diagnostics. The baseline scanned `limit=1` and decoded unretained properties. Preserve identical nodes, physical edges and snapshot with fewer scan/admission calls. | Small–medium | Direct reduction of per-row API/coordination overhead; depends on 1. Capture remains a selected-table census, not O(1). |
| 3 / PERF-MEM, GX-CAP-9 | Optional immutable compact adjacency owned by a detached projection; bounded construction and explicit accounting. The baseline retained an edge tuple but no reusable adjacency. Keep physical parallel-edge/self-loop identity and no persistent catalog/cache authority. | Medium | Reuse topology for repeated traversal/algorithms without rebuilding Python adjacency each time; foundation for 4–6, not a storage-format change. |
| 4 / GX-CAP-9 | Iterative strongly connected components with work/memory/cancellation bounds and deterministic labels. The baseline exposed degrees and weak components only. Independent oracle checks cycles, isolated nodes, parallel edges and loops. | Medium | New directed-dependency/cycle analysis; depends on 3. No recursion-depth or write-back dependency. |
| 5 / GX-CAP-9 | Read-only reachability and unweighted shortest paths on the detached projection. Explicit source/target, direction, depth/output/work limits and edge-identity tie semantics; reuse BFS rather than rescan storage per frontier. | Small–medium | Repeated path/dependency analysis over one captured snapshot; depends on 3. Weighted paths remain outside this slice. |
| 6 / GX-CAP-9 | Bounded unweighted PageRank: explicit damping, tolerance, iteration cap, dangling-node and parallel/self-loop semantics; distinguish declared non-convergence from resource refusal. No graph mutation or embedding service. | Medium | New graph-importance ranking, not faster database writes; depends on 3. |
| 7 / GX-CAP-9 | Bounded k-core decomposition over an explicitly defined undirected interpretation; document parallel-edge/self-loop treatment and test against an independent oracle. Stored/projected physical edges are not removed or rewritten. | Medium | New cohesion/dense-subgraph analysis; shares projection controls but builds simple neighbors directly, avoiding unused CSR. |
| 8 / GX-CAP-8 | Extend optional Arrow import/export to declared vector columns with fixed-size lists, explicit space/dimension/precision metadata, NULL and shape/range validation. The baseline admitted only seven scalar types. Preserve whole-call import staging atomicity and caller-owned commit. | Medium | Typed analytics/ML interop without manual list conversion; no zero-copy promise, external scans or new persisted format. |

Implemented surface: `scan_rows_v1(columns=, max_batch_bytes=, timeout_seconds=,
cancellation=)`; batched `project_graph` and `ProjectionDiagnostics`;
`with_adjacency`, `strongly_connected_components`, `reachable`, `shortest_path`,
`pagerank`, `k_core`; `ArrowVectorType` in both Arrow functions. Native writes now
validate encapsulated vector parameters against their declared target space rather
than letting them bypass dimension/identity/precision admission. No new format bit,
WAL effect or connection setting. [Usage and algorithm contracts](docs/GRAPH_PROJECTIONS.md),
[scan contract](docs/INTEGRATION.md#bounded-physical-scans),
[Arrow contract](docs/EXTENSIONS_AND_ARROW.md#explicit-native-vectors).

Approved checkpoints: **1–4**, then **5–8**. The
existing DoD applies: focused positive/failure tests, grouped final regression,
executable examples and roadmap/feature/configuration/API documentation. Publish
work counts or timings only after measurement, without marginal speed thresholds.
These candidates do not authorize a Pulse deployment or consuming reserved specs.
Multi-reader/writer, both OCC checks, WAL/durability and fail-closed validation stay
unchanged. Sharding, online reclamation, persisted algorithm write-back and temporal
storage are intentionally outside this bounded round.

### Approved continuation after 69ed311

Approved September 9, 2026 on `feature/v0.0.5`, in the order below. This finite
round preserves multi-reader/writer access, both OCC checks and WAL durability.
Checkpoint after items 1–4; all eight are authorized. No release, push or Pulse
data mutation is included. Each item requires feature/failure tests and updated
consumer/API/configuration contracts; final grouped regression closes the round.

| Order | Approved bounded deliverable | Status |
| --- | --- | --- |
| 1 / OPS-5 | Page-wise sparse-directory traversal for diagnostics/maintenance | Implemented checkpoint: 8,192 empty buckets / 72 directory visits |
| 2 / OPS-4/5 | Configurable repeated-key memo and immutable diagnostics | Implemented checkpoint: page/byte caps, zero disables, exact-image authority preserved |
| 3 / OPS-5/6 | Batch absent sparse-head materialization inside one existing publication | Implemented checkpoint: up to 64 heads per barrier; native crash/replay tests |
| 4 / OPS-4 | Aggregate per-handle HNSW logical-memory budget across retained pictures | Implemented checkpoint: live/retired shared reservations; not RSS |
| 5 / GX-CAP-8 | Typed bounded Arrow batch import with explicit atomicity | Implemented checkpoint: whole-call staging savepoint, prior writes preserved on failure |
| 6 / GX-CAP-5 | Bounded prefix full-text search | Implemented checkpoint: bit 11, exact expanded-term scoring; no phrase/position search |
| 7 / GX-CAP-5 | Full-text indexes over relationship text properties | Implemented checkpoint: bit 12, physical edge identities, replay/backup/transfer |
| 8 / GX-CAP-9 | Read-only snapshot-bound graph projections, degrees and connected components | Implemented checkpoint: detached directed multigraph, degree/WCC, bounds/cancellation; no persisted catalog or writes |

All eight bounded items are implemented and locally validated. Consolidated
regression: **15,994 distinct passes, 18 platform skips, no unresolved failures**;
two missing-helper-docstring failures were corrected and revalidated. Ruff and
documentation coverage passed. This is grouped Windows/Python 3.13 evidence, not
a clean uninterrupted run, remote cross-platform certification or a release.
[Round evidence and limits](docs/reports/V005_AFTER_69ED311.md).

These are extensions of the completed checkpoint below, not a reopening of its
DoD. Performance figures must be measured; there is no percentage gate.

### Approved continuation after 3f3819f

Approved September 9, 2026, in this order on `feature/v0.0.5`. This is a finite
implementation round, not deployment, publication or a relaxation of concurrency,
WAL, OCC, snapshots or fail-closed validation. Internal checkpoints follow 1–2 and
3–5; all eight are authorized. Feature/failure tests, grouped regression and
roadmap/capability/configuration/API documentation are required for completion.

| Order | Bounded scope | Status |
| --- | --- | --- |
| 1 / OPS-4, PERF-MEM | Explicit HNSW derived-picture construction/cache logical-memory budget and diagnostics, including safe warm-cache retirement after durable writes | Implemented and locally validated; cold admission, warm retirement and grouped regression |
| 2 / GX-CAP-5 | Bounded retained durable corpus summaries for eligible historical FTS snapshots; exact census when retention cannot prove coverage | Implemented and locally validated; snapshots, retention, interval verification, crash/replay and grouped regression |
| 3 / OPS-3 | Indexed discovery of already retired overflow FREE pages; revalidate ownership/horizon at allocation, preserve quiescent retirement | Implemented and locally validated; native crash cuts, valid-CRC corruption, backup, capability refusal and verifier corrective regression |
| 4 / OPS-5 | Opt-in sparse hash-directory allocation, with explicit format/capability, recovery, rebuild and old-reader contracts | Implemented and locally validated; first-head crash cuts, changed directory images, backup/transfer and grouped regression |
| 5 / OPS-5, PERF-SCALE | Repeated-key overflow access; preserve all visible versions and completeness, not just wider bucket sizing | Implemented and locally validated: bounded exact-image decoded-page memo. Still O(chain pages + candidates), no posting-tree or constant-time claim |
| 6 / GX-CAP-7 | Trusted explicit extension registry and typed scalar functions first; no mutable storage/WAL access or sandbox claim | Implemented and locally validated: per-handle immutable allowlist and typed scalar query/direct calls |
| 7 / GX-CAP-8 | Optional Arrow result-batch export, with exact type/ownership/snapshot/budget contracts; after the item 6 foundation | Implemented and locally validated: optional copied scalar batches over native results/cursors |
| 8 / OPS-10, GX-CAP-11 | Reproducible 0.0.5 compatibility/upgrade/accelerator/platform matrix; report unavailable platform rows, never imply they executed | Implemented workflow and isolated real-wheel upgrade tool; 3 local selector tests and 4 real 0.0.4 upgrade cases passed; unexecuted OS/Python rows remain unmeasured |

Performance value is workload-dependent; no unmeasured percentage is promised.
Persisted items require their concrete format/recovery contracts before code.
This round does not include online vacuum, sharding, full temporal history,
arbitrary UDF procedures, external Arrow scans or production Pulse workloads.
**Local DoD closed:** 15,904 distinct tests passed and 18 platform skips across full
grouped coverage plus corrective reruns; no unresolved failures. This is not one
uninterrupted clean invocation or a remote-platform certification. Documentation
checks cover links, 36 connect fields, public APIs/DTOs and preserved plans.
Initial defects, their dispositions and exact acceptance boundaries are
recorded in the [continuation receipt](docs/reports/V005_NEXT_EIGHT_PROGRESS.md).

### Approved eight-item continuation after e34a9e7

The operator approved items 1–8 in order. This is a finite continuation on
`feature/v0.0.5`, not release/deployment approval. Each slice requires feature and
failure-path tests, grouped regression and updated API/configuration/usage contracts.
No percentage performance gate or weaker multi-reader/writer/WAL/OCC guarantee is
authorized. The first four form an internal checkpoint, not a request to reauthorize
the remaining four. Durable changes require explicit format/recovery/upgrade contracts
before implementation; unimplemented parts must not be advertised as available.

| Order | Scope | Status |
| --- | --- | --- |
| 1 | Single-pass query-term frequencies for BM25; identical scores/order, bounded temporary memory | Delivered; locally validated |
| 2 | Index-driven hybrid incident expansion with certified completeness or safe scan fallback | Delivered; locally validated |
| 3 | Cooperative cancellation/deadline inside native vector loops, never interrupting commits | Delivered; locally validated |
| 4 | Integrated hybrid logical-memory accounting and consumption diagnostics, not an RSS cap | Delivered; locally validated |
| 5 | Opt-in durable snapshot-qualified FTS statistics, complete-COMMIT replay and verification; bit 6 | Delivered; locally validated |
| 6 | Explicit 65,536-bucket ceiling, bit 7, bounded physical distribution/skew and optional assisted-growth suppression | Delivered; locally validated |
| 7 | Default disk-spooled chunked physical capture/readback; optional memory mode, unchanged consistent-cut writer pause | Delivered; locally validated |
| 8 | Application checksum ledger/dry-run; additive NODE/REL TABLE and VECTOR SPACE only, atomic per version | Delivered; locally validated |

Acceptance: broad functional coverage produced **15,274 passes**, with 18 POSIX-only
skips on Windows and 11 facade/planner/docstring expectation failures. All were
corrected; final affected-surface/feature regression passed **3,653 tests with
zero failures**, in addition to verifier and installed-wheel validation. Counts
overlap and are not additive. No unresolved test failure remains. Full commands,
failure dispositions, final wheel hash and limits: [delivery receipt](docs/reports/V005_EIGHT_ITEM_CHECKPOINT.md).

Consumer contracts: [FTS](docs/FULL_TEXT_SEARCH.md), [hybrid](docs/HYBRID_SEARCH.md),
[index sizing](docs/INDEXES_AND_VECTORS.md), [backup](docs/BACKUP_RESTORE.md),
[application migrations](docs/SCHEMA_MIGRATIONS.md), [configuration](docs/CONFIGURATION.md)
and generated [API signatures/DTOs](docs/API_REFERENCE.md). Remaining scope is
explicit at that checkpoint: historical durable FTS summaries, sparse/sharded hash directories,
no-pause/resumable physical backups, arbitrary ALTER/views/derived graphs are not
part of that delivery. Historical summaries and sparse allocation were subsequently
implemented in the [continuation after 3f3819f](#approved-continuation-after-3f3819f).
No production installation or release is implied.

The operator approved all four candidates after `b16bf1f`, on `feature/v0.0.5`.
This finite delivery changes no release/version, production data or Pulse installation.
All four items are implemented and locally validated. Final broad regression:
**12,643 passed, 15 POSIX-only skips, zero failures**; final feature/consumer group:
**83 passed**. Package parity and executable examples also passed. Detailed scope,
initial-failure disposition and commands are in
[the delivery receipt](docs/reports/V005_SEARCH_RESUME_CHECKPOINT.md).

| Item | Implementation and acceptance boundary |
| --- | --- |
| 1. Reuse pure FTS analysis | Bounded operation/transaction memo reuses exact text/analyzer derivations during quota counting, staging, search and verification. No row/page/visibility authority cached; commit/abort release; saturation computes uncached. [Contract](docs/FULL_TEXT_SEARCH.md). |
| 2. Incremental snapshot BM25 statistics | Advance a validated same-generation summary through a complete bounded committed native WAL interval. Snapshot scores agree with independent census. Missing/recycled/oversized/unsupported proof declines to census; new handles still start cold. No new durable aggregate or WAL format. [Limits](docs/FULL_TEXT_SEARCH.md#budgets-and-performance-boundaries). |
| 3. GX-CAP-6 v1 | Typed weighted RRF, bounded union/intersection, source ranks/scores/coverage, exact/ANN regimes, opt-in missing-source partial disposition and bounded graph boost/filter on one reader. Single target table and single vector binding; graph relationship scans are bounded, not incident-only. [Usage](docs/HYBRID_SEARCH.md). |
| 4. OPS-2 resume extension | Explicit private workspace, normal WAL recovery, checked durable row prefixes and lease-safe identity remapping. Resume after batch/WAL/publication cuts; no partial target, overwrite or reinsertion of proved batches. Full artifact/prefix validation remains linear. [Usage](docs/LOGICAL_TRANSFER.md#opt-in-resumable-import). |

These close the four approved slices, not every future search/operations capability.
At that four-item checkpoint the remaining limits included cold corpus census,
graph scanning and separate source/fusion envelopes. The eight-item continuation
above adds opt-in durable summaries, indexed incident expansion and aggregate
logical accounting; bounded source-window recall, ineligible scan/census fallbacks
and no existing-target merge remain.
No marginal timing threshold is added; semantic/recovery/regression failures block closure.
The quality pass also closes root-export fixture/order drift and missing
cancellation/deadline labels in the query metrics catalog. Allocation attribution
checks remain strict but run in isolated processes; this is regression repair,
not a fifth product initiative.

## Approved four-item follow-up

Approved after the six bounded 0.0.5 actions below. This is a finite follow-up,
not a reopening of their acceptance criteria. Work remains on `feature/v0.0.5`.

| Item | Status | Delivery / acceptance |
| --- | --- | --- |
| 1. Ordered-page projection | Implemented checkpoint | Materializes only demanded columns; validates omitted payloads, index keys, both certificates and original snapshots. Fixed foreign-root/stale-heap reference failure. Latest 128-row wide projected page: 20.247 ms. |
| 2. Open/reopen admission | Implemented checkpoint | Removed duplicate index admission inside the startup artifact section; retained gap recovery, catalog refresh, existing-only recovery baseline, identity checks and fenced creation. Latest full-schema + 128-node writable open: 2,521.420 ms. |
| 3. Full synthetic Pulse consolidation | Implemented checkpoint; production scope remains separate | Real Community/SQLite/Grafx/primitives/outbox/ACK passed: four nodes, four edges, four refs/digests, one ACK, empty second tick. Commit 1,404.860 ms; worker through ACK 2,509.933 ms. Dominant residual cost is first independent-reader joins/checkpoint, not SQLite; no reserved specs consumed or guarantees removed. |
| 4. Typed connection configuration | Implemented checkpoint | All 35 config keywords and selector literals exported through `ConnectOptions`; positive/negative static consumers, runtime errors, docs, custom registry signature and isolated installed-wheel export validated. |

[Full evidence, failure disposition, boundaries and reproduction](docs/reports/V005_FOUR_ITEM_FOLLOWUP.md).
The grouped run had 4,198 passes, five investigated failures and one skip; the
corrected affected/adjacent set passed all 138 tests. Four failures were invalid
physical-page fixtures reproduced on untouched HEAD; their corruption refusal is
now retained as a separate negative test. No performance gate or release action
was added. Further cold-reader/open work must preserve independent participants
and include checkpoint cost, not move it outside the timing boundary.

## Proposed next round after the 0.0.5 checkpoint

Selected September 8, 2026: **N1 and N2 approved; checkpoint required before N3
and N4.** The committed 0.0.5 build was installed in both local global Pulse 0.3.3
environments before starting this source work. The previous six-plus-four bounded actions remain
closed; these are residual product slices, not additional gates on that delivery.
No version bump, new branch, production workload or release is implied.

| Order / existing IDs | Bounded proposed delivery | Effort / expected value | Precedence and acceptance boundary |
| --- | --- | --- | --- |
| N1 / PERF-COLD, PERF-WRITE | Reduce demonstrated repeated work in independent-reader admission and first-join checkpoint. The full consolidation profile identifies four reader opens and one checkpoint, not SQLite commit, as the dominant reconciliation cost. | Medium to large / highest measured latency opportunity in the small cold workload; percentage unknown | Reuse the existing full synthetic consolidation. Separate native admission/checkpoint changes from Community participant lifetime. Preserve independent lanes and fresh identity/recovery checks; moving checkpoint into warmup is not a speedup. Verify cold/warm and concurrent-reader/writer behavior. |
| N2 / BATCH-REL-1, PERF-COLD | Reduce remaining per-layout preparation/dispatch in a KG page. Existing filtering already reduces 69 layouts to 13; do not repeat that implementation. Share only bounded derived work that still preserves each layout's result and error. | Medium / conditional warm-page benefit, smaller than N1 for the cold fixture | After N1, use the fixed first/next-page workload. Exact nodes/edges, multiplicity, totals, snapshot boundaries, corruption refusal and independent error diagnostics must remain. No authority bundle; no public batch API unless measured residual dispatch justifies it. Stop if that overhead is marginal. |
| N3 / OPS-3, PERF-MEM | Extend foreground vacuum to reclaim horizon-eligible overflow history safely. The existing churn fixture demonstrates retained disk growth; it does not justify an online vacuum or a file-shrinking claim. | Large / high long-lived-store and disk-growth value, not an immediate UI speedup | Reuse pinned-reader/churn fixtures. Deliver only overflow lifetime/reclamation with WAL/crash/reopen proofs and refusal when quiescence is absent. File truncation, online reclamation and immutable-index orphan cleanup remain separate scopes. |
| N4 / GX-CAP-1 remainder | Complete native durable commit provenance: writer publication, public qualified lookup/metadata, verification/metrics and the transfer semantics required by the existing spec. | Large / high integration/recovery foundation value; may add write overhead, not a performance optimization | Separate capability checkpoint after the performance slices. Reuse the implemented journal/recovery foundation. Require atomic metadata/COMMIT behavior, replay idempotency, bounded lookup and concurrency/crash coverage; measure added commit cost. No temporal-history, FTS or attached-catalog scope bundled into this item. |

Execution boundary: deliver the N1–N2 performance checkpoint; leave N3 and N4
unstarted until that checkpoint is reviewed. Keep snapshot/WAL/durability tests as blockers,
group regressions after cohesive changes and impose no timing percentage floor.
Evidence: [four-item follow-up](docs/reports/V005_FOUR_ITEM_FOLLOWUP.md),
[lifetime and page workload](docs/reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md),
[CAP-1 contract](docs/specs/SPEC-GX-CAP-1.md).

**N1–N2 implemented checkpoint:** native existing-only index admission now
combines its initial structural observations; fresh certificates and the complete
first-join checkpoint remain. Scalar parameter preparation avoids redundant
dynamic container checks in Grafx and the Pulse Community adapter, with no Core
change or new batch API. 1,009 grouped tests and 31 Community executor tests
passed; exact 1,100-node pagination/concurrency and full synthetic ACK evidence
are recorded in the [checkpoint report](docs/reports/V005_N1_N2_CHECKPOINT.md).
Cold-open costs remain, and no overall percentage improvement is claimed.
The operator reviewed that checkpoint and subsequently authorized N3 and N4.

**N3 implemented bounded retirement:** foreground vacuum now removes eligible
overflow-backed versions and WAL-logs their exclusively owned pages as empty FREE
pages. Whole-store ownership/coverage checks precede any retirement effects;
quiescence remains an explicit operator assertion, not inferred from reader TTL.
No automatic allocator reuse or truncation: disk-growth control is still partial.

**N4 implemented and locally validated:** native publication includes
physical OCC interests, first-file initialization, final-LSN/segment-roll rebinding,
internal identity-floor commits and recovery. Development public APIs now provide
explicit activation, metadata-at-begin/retry, qualified snapshot lookup/paging,
full journal verification, privacy-safe metrics and coordinated logical-import
mapping hooks. Offline physical restore preserves identity; logical import/fork
uses a new store and atomically records the qualified source reference. This is
not a general graph backup engine or automatic directory cloning tool. The broad
regression and corrective reruns, native crash/concurrency tests, isolated wheel
and synthetic Pulse validations are recorded in the
[acceptance report](docs/reports/V005_N3_N4_ACCEPTANCE.md).
No new live spec was consumed; deployment and release remain separate.
[Consumer contract](docs/COMMIT_HISTORY.md),
[prior checkpoint evidence](docs/reports/V005_N3_N4_PROGRESS.md).

## Next proposed round after N1–N4 closure

Proposed September 8, 2026 when committing the completed checkpoint. The operator
subsequently approved **R1–R2 only before a checkpoint**. That checkpoint was committed
and pushed as `e2acb25`; the operator then approved R3–R4. Their bounded implementation
and local acceptance are complete. These items do not reopen
N1–N4 or the earlier six-plus-four deliveries. No new branch/version, production run
or release is implied. Gains below are hypotheses, not measured speedup promises.

| Order / existing IDs | Bounded next delivery | Effort / expected benefit | Precedence and stop condition |
| --- | --- | --- | --- |
| R1 / PERF-COLD, PERF-WRITE | Reduce residual cold participant admission and first-reader checkpoint work in the existing complete synthetic Pulse consolidation. Profile the remaining repeated index/catalog/page work once, then implement the dominant safe reduction. | Medium to large / highest evidenced remaining cold-load and reconciliation latency opportunity; percentage unknown | N1 removed duplicate existing-index admission, not the checkpoint. Preserve independent readers, fresh identity proofs, all checkpoint phases and end-to-end measurement. If residual work is necessary or savings marginal, report that and stop; no authority bundle or warmup relocation |
| R2 / OPS-7 | Make checksum-provider selection isolated per database/composition rather than process-global, preserving the current pure/native/auto choices and identical checksum bytes. | Medium / stronger embedding isolation and predictable accelerator use; no guaranteed latency gain | Independent localized hardening. Two handles with different settings must not change each other's provider; cover custom ports, explicit provider absence, concurrent use and wheel consumption. Do not change the disk format or weaken checksums |
| R3 / OPS-3, PERF-MEM | Reuse the overflow pages already retired as FREE by N3 so eligible space can serve subsequent overflow allocations. Start with the existing quiescent maintenance contract and an explicit persisted reuse protocol. | Large / potentially high reduction in growth under update/delete churn; not a file-shrinking or immediate UI-speed claim | Depends on N3, now delivered. Require ownership, reclaimed-snapshot floor, stale-reference/ABA, WAL/crash/reopen and allocation-quota proofs. No online vacuum, truncation or immutable-index orphan cleanup bundled in this slice |
| R4 / OPS-1 | Deliver a consistent physical backup and restore into a new directory, with a checked manifest, identity/provenance preservation, interruption handling and verify/reopen before success. | Large / high operational recoverability value; no throughput claim | Uses completed CAP-1 identity semantics; does not require R3. Prove the snapshot/WAL retention boundary with concurrent writers and refuse unsafe/incomplete promotion. No logical export engine, automatic repair of authoritative corruption or independently writable same-UUID fork |

**R1–R2 checkpoint:** exact-string ASCII identifier validation now removes repeated
Python per-character dispatch in cold catalog/index admission. Required file
identity checks and checkpoint phases remain; cold IO is not claimed solved.
Connections now retain a validated, execution-local checksum selection independent
of other handles, including custom registries and nested/threaded operations.
Grouped regression: 5,953 passes and one platform skip; complementary public-surface
and documentation group: 1,402 passes; Pulse adapter group: 105 passes. Full synthetic
consolidation and isolated wheel smoke passed. [Evidence and limits](docs/reports/V005_R1_R2_CHECKPOINT.md).

R3 consumes existing persisted FREE overflow images under the ordinary commit fence,
with floor/current-page/LSN validation and O(1)-memory incremental discovery. No new
mutable free-list format was necessary. R4 provides bounded checkpoint-fenced
capture plus verified artifact/restore publication; source commit publication waits
during capture, not during destination IO or verification. This is explicitly not
no-pause streaming backup. [Backup contract](docs/BACKUP_RESTORE.md).

Acceptance: 5,986 grouped passes / one platform skip; 562 coordination passes /
three platform skips; 114 Pulse adapter passes; isolated installed-wheel smoke
passed. Final focused guards and consumer-surface checks also passed; overlapping
counts and remaining platform/operational limits are in the
[R3–R4 checkpoint](docs/reports/V005_R3_R4_CHECKPOINT.md).

R3–R4 use existing isolated fixtures, focused
semantic tests and grouped regressions; do not consume the 19 reserved specs.
FTS (GX-CAP-5), general logical export/import (OPS-2), larger hash directories and
immutable-index orphan reclamation remain visible below but are not additional
requirements for this proposed four-item round.

## Operational checkpoint after R1–R4

Items **1–2 were completed, committed and pushed** at `692cc26`. The operator
subsequently approved **3–4**, including tests and documentation as part of their
delivery. Work stays on `feature/v0.0.5`; this continuation does not request a
release, global Pulse installation or production data changes.

| Order / ID | Scope / status |
| --- | --- |
| 1 / OPS-5 | Implemented checkpoint, locally tested: explicit quiescent orphan-index census, dry run and removal; preserve every catalog state and retained-WAL dependency. No online GC or catalog-generation retirement. |
| 2 / OPS-8 | Implemented checkpoint, locally tested: cooperative cancellation/deadlines on materialized reads and cursors, typed errors and resource cleanup. No write/commit interruption or preemptive I/O deadline. |
| 3 / OPS-2 | Implemented v1; locally validated: streaming versioned schema/data/vector export/import, identity/endpoint remapping and verified fresh-store promotion. [Usage and limitations](docs/LOGICAL_TRANSFER.md). |
| 4 / GX-CAP-5 | Implemented v1; locally validated: persisted inverted postings, versioned analyzers, weighted BM25, filters, bounded typed/procedure search, update/tombstones, recovery and generation rebuild. [Usage and limitations](docs/FULL_TEXT_SEARCH.md). |

[API contracts and limits](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). Existing multi-reader/
multi-writer, OCC, WAL/durability and recovery guarantees remain required. This
round adds disk-hygiene and responsiveness controls, not a measured throughput claim.
Grouped regression, final API/query coverage, explicit closure of every observed
failure, 114 Pulse adapter passes and final installed-wheel acceptance are recorded
in the [checkpoint report](docs/reports/V005_OPS5_OPS8_CHECKPOINT.md). That report
certifies neither implementation nor acceptance of items 3–4.

Items 3–4 now have their own [implementation, regression, package and documentation
audit receipt](docs/reports/V005_OPS2_FTS_CHECKPOINT.md). It records the initial failures
and their corrective runs, including actual historical recovery floors and native
journal/index-seal interoperability. Final affected transaction/feature regression:
991 passed. The earlier 5,300-pass and corrective 2,709-pass groups overlap; their
counts are not an all-green single run or additive unique total. FTS cold/warm and
transfer observations are in the [performance reference](docs/PERFORMANCE.md).
No global Pulse deployment or publication is part of this checkpoint.

### Documentation included in the items 3–4 delivery

Documentation is part of the approved implementation, not a separate follow-up
or an expansion of the feature scope. Update it alongside each implemented
contract; do not advertise proposed interfaces as callable features.

| Area | Required documentation alongside tested implementation |
| --- | --- |
| OPS-2 logical transfer | Public entry points and result types; executable export/import examples; versioned artifact schema and supported values, schema and vectors; source snapshot and identity/endpoint remapping; budgets, resumability or its explicit limitations; validation, failure/retry and fresh-directory promotion. Distinguish logical transfer from same-identity physical restore and state what history/index state is preserved, rebuilt or excluded. |
| GX-CAP-5 full-text search | Index creation/search/rebuild APIs and supported procedure/query forms; analyzers and their versioned behavior; BM25, field weights, filters and result fields; transactional visibility and freshness; work/memory/time/cancellation limits; persistence, recovery, compatibility/refusal and backup/transfer behavior. Document supported behavior only, keeping deferred capabilities explicit. |
| Shared public references | Update the [API reference](docs/API_REFERENCE.md), generated signatures/DTOs and [configuration reference](docs/CONFIGURATION.md) for actual additions. Explain defaults, accepted values, tradeoffs and when to use or avoid each new option; distinguish connection settings from per-operation parameters. Update query, index, integration and operations guides where their contracts change. |
| Discovery and traceability | Link consumer guides from [README](README.md) and the [documentation index](docs/README.md); update this roadmap's status and known limitations with code/test evidence. Keep one active roadmap, not another competing plan. |

Acceptance includes focused feature and failure-path tests, a grouped relevant
regression, validation of executable examples, and the existing documentation
link/configuration/API-drift checks. Record commands, results and remaining
limitations in the delivery evidence; documentation checks do not substitute for
runtime tests. Publish performance figures only when measured, with their workload
and conditions, using the latest measurement rather than an unmeasured gain claim.
No additional marginal performance threshold is introduced by this requirement.

## Known limitations and corrective work

| ID / legacy mapping | Status and impact | Required work / acceptance |
| --- | --- | --- |
| OPS-1 / §8.1 | R4 plus item 7: bounded physical backup/restore with chunked capture | Checked manifest, checkpoint-fenced disk spool (default) or memory capture, verified no-replace publication and offline same-UUID replacement. No no-pause/resumable hot backup, generic custom-storage backup or independently writable fork. [Contract](docs/BACKUP_RESTORE.md). |
| OPS-2 / §8.2 | Implemented bounded v1 plus explicit resume extension | Streaming checksummed logical schema/data/vectors, parallel edges, current-ID mappings, FTS declarations, private-workspace crash resumption and verified no-replace promotion. Current state only; no existing-target merge or historical journal copy. [Contract](docs/LOGICAL_TRANSFER.md), [latest acceptance](docs/reports/V005_SEARCH_RESUME_CHECKPOINT.md). |
| OPS-3 / P1.4, §8.3 | Partial: quiescent vacuum, validated overflow reuse and opt-in indexed candidate discovery | Retirement and immutable candidate directories are WAL-published. Default discovery remains amortized O(heap pages); indexed discovery visits candidates/stale entries/directory pages and is not constant time. Quiescence remains an operator assertion; truncation and online vacuum remain unimplemented. [Indexed discovery](docs/OPERATIONS.md#indexed-retired-overflow-discovery-005-development). |
| OPS-4 / P1.8 | Partial: per-picture and aggregate per-handle HNSW admission, configurable key memo and diagnostics; still not RSS caps | Cross-handle/provider/temporary retention and measured process envelopes remain. Preserve deterministic refusal; do not derive RSS promises from nominal page bytes. [Memory contract](docs/INDEXES_AND_VECTORS.md#continuation-after-69ed311-bounded-maintenance-and-memory). |
| OPS-5 / P1.12 | Partial: growth/rebuild, quiescent cleanup, 65,536-bucket sizing, sparse page-wise maintenance/batched heads, configurable HASH decoding and opt-in repeated-key posting layout | Cleanup preserves catalog-owned STALE/BUILDING and retained-WAL dependencies. Online/catalog-generation retirement and sharding remain. [Posting layout](docs/POSTING_HASH.md), [index contract](docs/INDEXES_AND_VECTORS.md), [cleanup](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). |
| OPS-6 / P1.11 | Partial: sparse head-initialization barriers shared inside one committed publication; physical conflicts and exclusive publication remain | Remove only proven redundant publication/page work. Disjoint logical rows may still conflict; do not promise linear writer scaling or replace multiwriter with an application-wide single-writer premise. |
| OPS-7 / P2.6, P2.9 | Implemented bounded DX/isolation checkpoint in 0.0.5 | Typed `Unpack[ConnectOptions]` plus per-connection checksum selection, including custom registries. Standalone low-level installers retain their legacy default outside database operations. No checksum algorithm or persisted byte semantics changed. [R2 evidence](docs/reports/V005_R1_R2_CHECKPOINT.md). |
| OPS-8 / §8.5 | Partial: reusable Query/cursor and cooperative read cancellation/deadlines exist | `Query` is not a durable prepared plan or HTTP token. Broader streaming/prepared APIs remain; controls do not preempt blocking calls, run watchdog cleanup or interrupt commits. [Contract](docs/READ_CONTROL_AND_INDEX_CLEANUP.md). |
| OPS-9 / P1.16 | Deferred configuration capability | Expose HNSW construction knobs only with persistent identity/defaults, cold/incremental parity expectations and a controlled 8,192×384 recall profile. `vector_ef_search` already configures runtime beam; it is not construction tuning. |
| OPS-10 / P2.8 | Acceptance obligation | Exercise supported Python versions/platforms, wheels/sdists, optional accelerators, version refusal and upgrade fixtures. A local Windows run is not a claim that every matrix row passed. |
| OPS-11 / query gaps | Open subset boundary | No full Cypher compatibility, arbitrary UNION/OPTIONAL combinations, general CALL/procedures or arbitrary schema ALTER/DROP contract. Extend through explicit query specs, budgets, null/ordering/error and read/write overlay tests. |
| OPS-12 / recovery | Open operational limit, not an auto-repair promise | CRC-invalid authoritative pages, foreign database identity, future/conflicting LSN, unsupported capability and unproved effects remain fail-closed. Product recovery must distinguish safe replay, evidence-preserving refusal and operator restore. |
| OPS-13 / historical static and mutation debts | Historical / revalidate | Retain all original survivors and component findings in the source register. Reproduce against current source before reopening; close with a named discriminating test, not a broad “all fixed” assertion. |
| PULSE-OPS | Consumer debt, not engine defect by default | Latest live report still lists historical Global/policy DLQ, canonical debt, stale diagnostics and incomplete runtime-budget attribution. Keep visible; generic Grafx changes do not automatically resolve Pulse policy/source-authority data. |

## Remaining performance work

This is a finite residual queue, not a requirement to rediscover every historic
micro-optimization. [Measurements](docs/PERFORMANCE.md) distinguish component,
native API, HTTP/MCP and UI boundaries.

| Priority / ID | Status / next bounded action | Success evidence |
| --- | --- | --- |
| 1 / PERF-WRITE | Open: attribute complete consolidation and Global delivery cost | Capture already-deployed phase observations in the next authorized real run; separate admission, graph commit, SQLite/outbox, verification and scheduling. Latest 10.726/54.952 s do not identify native-only cost. |
| 2 / PERF-COLD | Open: full KG cold-load and remaining relationship fan-out | Profile selected UI/API route with fixed result digest and error table; separate cold handle/index admission, per-layout work, verification and rendering. Do not label early 0.0.4 samples as final-source latency. |
| 3 / BATCH-REL-1 | Partial: transaction-local PK/identity batches and grouped endpoint verification implemented | Remaining per-layout statement fan-out may share bounded preparation only when independent statement errors, snapshots and budgets remain observable. Re-profile after landed batching before new API work. |
| 4 / CONC-CPU-1 | Open: CPU/GIL limits after legitimate I/O parallelism | Compare bounded independent participants, decode/expression cost and memory before adding processes/native kernels. No inference that more handles implies linear throughput. |
| 5 / PERF-SCALE | Partial: ordered cursor OIX work, index growth and vector access paths landed | Measure increasing N, fan-out, skew and churn on selected real query shapes. Canonical fallback may still be O(N); preserve it where completeness/validation requires it. |
| 6 / PERF-MEM | Partial: bounded caches, spill, vector-free landings and retained metrics/history improvements landed | Measure peak memory with realistic handle count and indexed writes; logical counters are not RSS. Do not discard validation to reduce allocations. |
| Future / AUTH-BUNDLE | Deferred, explicitly not needed to unblock current work | Possible cache/bundle of authority requires a separate security/invalidations design. The selected protocol remains complete pre-validation → operation with the real 30 s budget → post-validation/close, every phase fail-closed. Do not expand the authority surface merely to pass a gate. |
| Future / PERF-DRIVING-KEYS | Confirmed optimizer limitation, deferred from functional parity: scalar UNWIND variables and arithmetic driving keys retain node scans; direct properties of map-batch elements already use indexed seeks | The final 0.0.6 synthetic sample records two NodeScan operators for scalar endpoint binding versus two IndexSeek operators for the supported map-element batch. Widen eligibility only after proving binding order, NULL/type/error and snapshot/rollback equivalence; never seek from an unproduced matched variable. This is a future bounded optimization, not a new parity gate or a claimed speedup. |

Completed Wave 0–3, OIX-0–3, source-reference seeks, query-expression reuse,
vector-free relationship landings, grouped endpoint/count checks, bucket batching,
recovery-floor reuse and telemetry sampling stay completed within their recorded
scope. Negative experiments are not silently put back into this queue.

## Next iteration assessment: feature/v0.0.5

Assessment opened September 8, 2026 at the operator's request. Local branch
`feature/v0.0.5` starts at the released main merge `425362a`. This section orders
existing backlog items; it is not another independent plan. The operator subsequently
selected performance items 1–6 for delivery 0.0.5; the bounded implementation and
follow-up validation are now complete as recorded below,
and package/source versions are bumped to 0.0.5. Product-capability rows below remain
unselected. No live Pulse mutation is implied. Effort is relative: small
means a localized change, medium crosses a few components, large changes persisted
or concurrent protocols. Potential impact is a hypothesis, not a measured speedup.

### Performance: recommended execution order

| Order / existing IDs | Concrete opportunity and evidence | Effort / potential impact | Bounded first action and stop condition |
| --- | --- | --- | --- |
| 1 / PERF-COLD, BATCH-REL-1 | KG first page, next 500 nodes and relationship statement fan-out. Latest-source UI latency is unknown; existing 64-step destination batches already remove much per-identity certification overhead, so repeating that implementation is not new work. [Batch evidence](docs/reports/GLOBAL_DESTINATION_BATCHING_0_0_4.md). | Small assessment; medium implementation / potentially high visible latency benefit | One fixed read-only workload, first/next page, cold/warm and one independent writer; separate open, query/layout, transport and render costs. Select the dominant repeated work only. Preserve exact nodes/edges, multiplicity, totals and independent errors. No new public batch API unless remaining overhead justifies it. |
| 2 / PERF-WRITE | Attribute native commit versus admission, SQLite/outbox, verification and scheduling. Latest full calls are 10.726 s commit and 54.952 s Global ACK, while a different private native workload measured 51–57 ms; these are not comparable A/B samples. [Measurement boundaries](docs/PERFORMANCE.md). | Small attribution; implementation depends on result / potentially high end-to-end benefit | Validate phase-observation capture first. Use a fixed synthetic representative write before any newly authorized real spec. Remove repeated generic engine work only where measured; consumer scheduling/relational costs stay in Pulse, not Grafx Core. Do not consume all reserved specs or keep repeating runs without a discriminating question. |
| 3 / PERF-SCALE, OPS-5 | Avoid whole-graph work for selective lookups and bounded pages; inspect skew, relationship fan-out and growth. Ordered cursors, indexed hybrid BFS, explicit 65,536-bucket directories and skew diagnostics exist; completeness fallbacks can remain O(N). | Medium to large / high at larger N if an affected access path dominates | Fixed selective queries at three declared sizes, with operation/page counts and identical semantics. Identify actual scan/overflow pressure before another index change. Full graph enumeration and verification remain proportional to data; no universal O(1) promise or removal of corruption detection. |
| 4 / PERF-MEM, OPS-3/4/5 | Reduce retained state and growth under updates/deletes: multiple handles, immutable index generations, overflow history. N3 retirement and R3 reuse address eligible overflow growth; file shrinking and indexed free-page discovery remain absent. [Current maintenance limits](docs/OPERATIONS.md#maintenance-backup-and-upgrades). | Medium diagnosis; large reclamation work / high long-lived-store value | A bounded churn workload with peak RSS, disk growth and a pinned reader. Separate cache/accounting fixes from physical reclamation. Reclamation requires snapshot/lifetime and crash proofs; it is not a quick online-vacuum toggle. |
| 5 / CONC-CPU-1, OPS-6 | Reduce demonstrated CPU/decode/expression or publication contention after legitimate I/O parallelism. Independent readers already exist; more handles do not remove the GIL or exclusive durable publication. | Medium; large for native/process changes / conditional | Reuse the same fixtures with 1/2/4 participants and identify wait versus CPU. Prefer bounded local hot-path work. Native kernels, processes or publication redesign are selected only if that cost dominates; retain both OCC checks and multiwriter semantics. |
| 6 / OPS-8, BATCH-REL-1 | Reusable prepared execution and bounded shared statement preparation, beyond existing expression reuse. A reusable public Query is not a persisted prepared plan. | Medium / conditional, lower priority until preparation is material | Count parse/plan/catalog preparation in the chosen KG workload. Cache only derived immutable preparation with explicit schema/snapshot/config invalidation and budgets. Do not cache authority or silently combine independent statement failures. |

Orders 1–2 form the first checkpoint: one bounded attribution pass per workload,
then the demonstrated high-impact localized fixes and one grouped regression.
No percentage floor or repeated marginal timing gate. If the dominant cost is
outside Grafx, record that fact and route the integration change appropriately;
do not invent an engine rewrite to keep the queue busy. Orders 3–6 were handled
after that checkpoint; the outcomes below are not six new mandatory rewrites.

### 0.0.5 implementation checkpoint — September 8, 2026

The six selected **bounded 0.0.5 actions are complete**, including their follow-up
measurements and dispositions below. This does not close the broader PERF/OPS
backlog. Four bounded engine changes are implemented and tested: layout-sized plan retention with admission
limits, numeric IN membership, unstaged-writer scalar PK reuse and standard-library
top-k heaps. [Evidence and latest synthetic measurements](docs/reports/V005_NATIVE_PERFORMANCE_CHECKPOINT.md).
Version/package metadata is 0.0.5; no on-disk format, public configuration, WAL/OCC
protocol or automatic commit-history capability was changed.

| Selected item | 0.0.5 bounded outcome | Explicit remaining product/consumer work, not silently included |
| --- | --- | --- |
| 1 / KG loading | Closed: exact route/service/adapter fixture at 128/512/1,100 nodes; 13 of 69 layouts selected; real Pulse API client + GraphCanvas loaded 500/1,000/1,100 nodes and terminated pagination | Full authenticated production KnowledgeGraphPage/SQLite overhead and latest-source Ladybug parity are not measured by fixture controls. No redundant layout implementation |
| 2 / Write/Global delivery | Closed for the selected fixed synthetic write: fresh-certified preflight reuse; native phases, actual Global vector writes, Core dispatch and integrity inventories measured; phase/fence/cancellation capture tests pass | Historical full production commit/54.952 s ACK still lacks graph-versus-SQLite/scheduling attribution. Not claimed solved; requires a separate consumer receipt after deployment, not another native timing floor |
| 3 / Scale | Closed bounded experiment: numeric IN no longer walks each list for every numeric match; three native sizes plus Pulse sizes and 128-edge hub; ordered page examines 500 candidates at both 512 and 1,100 nodes | Whole enumeration remains O(N); 4,096-bucket limit not reached, so no speculative format/sharding rewrite. Wider skew/index growth remains PERF-SCALE/OPS-5 |
| 4 / Retained memory | Closed cache slice and lifetime experiment: text/byte admission and owner release; 12 update/delete/overflow cycles with 1/2/4 handles plus pinned reader, measured RSS, exact old/current answers and clean reopen | Historical experiment predates N3 retirement and R3 reuse; it is not a measurement of the new allocator. No truncation or immutable-index orphan cleanup. Physical allocation/index history remains OPS-3/4/5 |
| 5 / CPU/concurrency | Closed attribution: actual page shape at 1/2/4 independent handles, including two writers; correct reads/durable updates; wall/thread CPU/non-CPU recorded | More threads did not yield linear scaling. Non-CPU includes GIL/OS/I/O, not proven lock-only wait. Native kernels/processes/publication redesign remain OPS-6 |
| 6 / Preparation | Closed: 160-shape cyclic cache regression fixed within limits; real warm KG cycle built zero parses/plans, with 16 retained plans | Preparation is not the dominant warm KG cost. Durable public prepared-plan APIs remain OPS-8; no authority cache added |

Validation: 2,841 grouped query/API/import/documentation tests passed in 306.28 s;
113 consumer integration tests in 28.45 s; final focused 137 tests in 5.04 s overlap
that coverage and include the final writer OCC/read-partition cases. Follow-up:
92 additional/overlapping Community tests in 61.82 s, 39 Core phase/fence/inventory
tests in 4.75 s, browser fixture and isolated 0.0.5 wheel smoke all passed. The final
focused/documentation slice passed 149 tests in 9.43 s (overlapping prior coverage).
Strict mypy
retains the identical 254 baseline diagnostics in query_engine.py after normalized
line offsets; no new diagnostics, not a type-clean-engine claim. No live Pulse
restart, production deployment, spec consolidation, backfill, commit/push or release
occurred. Isolated UI listeners were stopped. The linked report preserves exact
measurement boundaries and every remaining limitation; closure is of the six
bounded actions, not a promise to finish every referenced OPS milestone in 0.0.5.

### Product evolution: proximity versus value

| Recommended position / existing IDs | Next usable capability | Effort / value / dependency |
| --- | --- | --- |
| Small independent DX improvement / OPS-7 | Typed keyword discoverability for connect options, matching the documented configuration and runtime validation; preserve custom-adapter consumption. | Small to medium / immediate integration usability, no expected throughput gain. Keep checksum-provider isolation as a separate deeper item; autocomplete does not solve process-global selection. |
| First substantial capability / GX-CAP-1 remainder | Delivered as N4: durable writer publication, qualified lookup, metadata/history, verification/metrics and coordinated transfer/restore/fork semantics. | Local development acceptance closed; release/deployment remain separate. Adds measured write/retention cost, not a speedup or full temporal history. [Evidence and limits](docs/reports/V005_N3_N4_ACCEPTANCE.md). |
| Operational evolution / OPS-1, OPS-2 | R4 bounded physical backup/restore and OPS-2 logical transfer with opt-in crash resumption. | Implemented development APIs. No no-pause physical hot backup, generic custom-storage restore or journal-history transfer. [Logical contract](docs/LOGICAL_TRANSFER.md). |
| Delivered search foundation / GX-CAP-5/6 | Native FTS, single-pass query frequencies, operation-local analysis reuse, WAL or opt-in durable statistics, explainable hybrid retrieval, indexed incident evidence and aggregate memory diagnostics. | Ineligible/legacy cold census and scan fallback remain linear; pure reuse is capped; hybrid ranks bounded source windows. [FTS](docs/FULL_TEXT_SEARCH.md), [hybrid](docs/HYBRID_SEARCH.md). |
| After commit provenance / GX-CAP-2, GX-CAP-3 | Attached catalog sessions and opt-in system-time history. | Large each / strategic multi-store/temporal value. CAP-1 first; one-store writes remain the rule. Neither is required to fix current KG loading. |
| Later dependent capabilities / GX-CAP-4/7/8/9/10, AGENT | Bitemporal retrieval, extension SPI, Arrow/exchange, general graph algorithms, schema evolution and optional agent packages. | Medium to large individual slices / valuable but not all immediate. Arrow/external scans currently depend on CAP-7 and existing streaming/bulk. Hybrid v1 does not implement agent ContextPacks or general cross-table traversal. |

The selected performance/DX checkpoint and CAP-1 N4 scope are delivered. Physical
backup was subsequently implemented; logical transfer and FTS were explicitly
approved and are in the items 3–4 acceptance checkpoint above. Existing
multi-read/write, durability, consistency and fail-closed
constraints apply throughout. Group commit, indexed-only DETACH DELETE, authority
bundles and sharding remain in their existing rejected/deferred dispositions.

## Database capabilities and dependencies

Generic database functionality owns commit identity, catalog sessions, temporal
history, FTS and hybrid retrieval. Temporal history is **logical retained history**,
not accidental access to MVCC versions. One transaction writes one physical store;
physical edges do not cross stores. Embedding generation stays outside the engine.

| Milestone | Status | Scope, prerequisites and contract |
| --- | --- | --- |
| GX-CAP-0 | Implemented checkpoint | Database-first boundaries, six ADRs, capability manifest, isolated package/import checks. [Spec](docs/specs/SPEC-GX-CAP-0.md). |
| GX-CAP-1 | Implemented bounded N4 checkpoint | Qualified CommitId, opt-in durable metadata/history, monotonic logical commit time, lookup/pagination, verification and coordinated transfer/restore semantics. Not full temporal row history; logical graph v1 transfer excludes historical journal entries. [Evidence](docs/reports/V005_N3_N4_ACCEPTANCE.md), [usage](docs/COMMIT_HISTORY.md), [spec](docs/specs/SPEC-GX-CAP-1.md). |
| GX-CAP-2 | Partial: sessions/workspaces, CLI and selected bounded copy implemented | Explicit permissions/ownership and one-store transactions; JSON inventory/resolution; selected RIDs, skip-node/fail policies and atomic receipt replay. NHC adds opt-in direct endpoint closure. Arbitrary predicates, recursive subgraph closure, anonymous nodes, merge and federation remain open. [Spec](docs/specs/SPEC-GX-CAP-2.md), [NHC acceptance status](docs/reports/V006_NHC_ROUND.md). |
| GX-CAP-3 | Native bounded system-time slice plus NHC access/maintenance implementation | Atomic activation/current/history WAL, typed as-of/versions, historical nullable schema/edges/delete-recreate, durable pins, bounded payload redaction, verification and physical backup. NHC adds optional persistent temporal indexing and quiescent physical compaction. Full temporal logical export, online compaction and embedding-space timelines remain open. [Usage](docs/SYSTEM_TIME_HISTORY.md), [spec](docs/specs/SPEC-GX-CAP-3.md), [NHC acceptance status](docs/reports/V006_NHC_ROUND.md). |
| GX-CAP-4 | Partial: bounded system-time diff implemented in NHC | Typed graph/schema/property diff with lineage; valid time, bitemporal semantics and two-coordinate diff remain planned. [Usage](docs/SYSTEM_TIME_HISTORY.md), [spec](docs/specs/SPEC-GX-CAP-4.md), [NHC acceptance status](docs/reports/V006_NHC_ROUND.md). |
| GX-CAP-5 | Implemented v1 plus NHC position/proximity/replacement surfaces | Native FTS, frozen analyzers, weighted BM25, snapshot scores and full lifecycle, prefixes, relationship fields and durable/history statistics. NHC adds bounded token-position results, ordered slop and same-name analyzer replacement. Same-snapshot heap validation remains; original-character offsets and ineligible-statistics census optimization remain open. [Usage](docs/FULL_TEXT_SEARCH.md), [spec](docs/specs/SPEC-GX-CAP-5.md), [NHC acceptance status](docs/reports/V006_NHC_ROUND.md). |
| GX-CAP-6 | Implemented bounded v1 plus certified incident expansion and shared controls/memory | Weighted RRF, union/intersection, source explanations, graph boost/filter, explicit missing-source partial policy, indexed BFS and per-phase logical peaks. No embedding provider, cross-table fusion, graph-only candidate expansion or universal recall promise. [Usage](docs/HYBRID_SEARCH.md), [spec](docs/specs/SPEC-GX-CAP-6.md), [latest evidence](docs/reports/V005_EIGHT_ITEM_CHECKPOINT.md). |
| GX-CAP-7 | Partial: trusted scalar SPI implemented | Explicit immutable per-handle registry, typed scalar UDFs/direct calls, NULL/value budgets and typed failures. Aggregate/table/procedure extensions, durable manifests and sandboxing are not implemented. [Usage](docs/EXTENSIONS_AND_ARROW.md), [remaining spec](docs/specs/SPEC-GX-CAP-7.md). |
| GX-CAP-8 | Partial: scalar/vector Arrow, Pandas/Polars, local Parquet/CSV/JSONL and detached graph exchange implemented | Explicit native vector identity, nullable typed batches, atomic import staging and caller-owned commit; bounded local files and NetworkX/projection Arrow exports. External query scans/COPY, arbitrary nested/entity ingestion and graph import remain. [Usage](docs/TABULAR_AND_PARQUET.md), [text](docs/LOCAL_TEXT_IMPORT.md), [exchange](docs/GRAPH_EXCHANGE.md), [remaining spec](docs/specs/SPEC-GX-CAP-8.md). |
| GX-CAP-9 | Partial: weighted projections, retained lookup/CSR/transitions/simple topology, degree/WCC/SCC, BFS/Dijkstra, PageRank, k-core and label propagation implemented | Bounded read-only algorithms, cancellation, explicit NumPy and independent/NetworkX oracles for declared algorithms. Persistent catalog, mutation/write modes, Louvain and broader algorithm packages remain. [Usage](docs/GRAPH_PROJECTIONS.md), [remaining spec](docs/specs/SPEC-GX-CAP-9.md). |
| GX-CAP-10 | Partial: additive migrations, bounded logical views and nullable-column evolution | Atomic per-version CREATE DDL; typed persisted base-table read views; typed append-only nullable columns with exact old-row layouts. Arbitrary ALTER, other S1/S2/S3 migrations, nested views and derived/materialized graphs remain. [Migrations](docs/SCHEMA_MIGRATIONS.md), [views](docs/LOGICAL_VIEWS.md), [columns](docs/NULLABLE_COLUMNS.md), [remaining spec](docs/specs/SPEC-GX-CAP-10.md). |
| GX-CAP-11 | Partial: 0.0.5 and 0.0.6 compatibility automation and local upgrade evidence implemented | Real-wheel upgrade/refusal, cross-selector reopen and Windows/Ubuntu × Python 3.11–3.13 workflows. Remote matrix runs and release acceptance are not implied. [Current evidence](docs/V006_COMPATIBILITY.md), [previous evidence](docs/V005_COMPATIBILITY.md), [spec](docs/specs/SPEC-GX-CAP-11.md). |

Implementation precedence: close release boundary → complete CAP-1 → catalog/
system-time/FTS foundations → bitemporal/hybrid → extension/interoperability/
algorithms/schema evolution → combined hardening. Independent design can overlap;
shared WAL/format changes must be integrated in a controlled sequence, not competing
branches that independently redefine the protocol.

## CAP-2 through CAP-4 opportunity assessment

Historical assessment before the capability continuation. Current implemented
slices and remaining limits are in the capability table immediately above and
the authorized follow-up; do not read the original gap estimates as current status.

0.0.6 consumption additions also extend the bounded CAP-8/9 surfaces above:
[SQLite ingestion](docs/LOCAL_SQLITE_IMPORT.md), [offline HTML inspection](docs/HTML_SNAPSHOTS.md)
and [topological ordering](docs/GRAPH_PROJECTIONS.md#topological-ordering-006).
These do not implement CatalogSession, temporal history or the other remaining
CAP-8/9 capabilities.

Assessed September 9, 2026 against `65ab659`, after the preceding eight-item
implementation was committed and pushed. **Assessment only: CAP-2/3/4 remain
Planned.** No new version assignment, implementation approval, release or Pulse
data operation is implied. This section refines the existing backlog; the linked
specs, ADRs and preserved source requirements remain authoritative, including
requirements not repeated here. It does not reopen the preceding delivery's DoD.

### Existing foundations versus missing capability

| Capability | Reusable, implemented foundation | Actual remaining work | Effort / value |
| --- | --- | --- | --- |
| GX-CAP-2 | Explicit database handles, read-only admission, transaction lifecycle, store UUIDs, CAP-1 provenance, checksummed logical export and resumable import into a fresh store. | CatalogSession, alias/permission/path policy, pinned catalog selection, deterministic workspace resolver, ownership-safe detach/close, CLI inventory, selected copy into an existing target and durable idempotency receipts. | Large overall; session-only slice medium. High integration value for project/user/reference stores, not an automatic engine speedup. |
| GX-CAP-3 | Qualified CommitId, monotonic ordered commit time, journal lookup/paging, stable record identities, WAL/OCC/recovery, snapshot reads and bounded maintenance. | Durable opt-in node/edge versions and historical schema, atomic current/history effects, lineage, indexed typed historical reads, timestamp resolution, retention pins/horizons, verification and temporal transfer semantics. | Very large; highest storage/protocol risk of these items. Enables reproducible historical graph queries and auditing; introduces write/storage amplification. |
| GX-CAP-4 | CAP-1 time/identity values and ordinary typed timestamps. CAP-3 is still missing. | Application validity periods and overlap policy, concurrent enforcement, two-coordinate queries, graph/version/property diff with lineage, full retention policies and later query syntax/CLI. | Large to very large after CAP-3. Enables retroactive corrections and “what was known then about what was valid then”; not a shortcut to faster current-state reads. |

Concrete evidence:

- [Database.begin](src/okto_grafx/engine/database.py) currently accepts mode and
  commit metadata, not a catalog or temporal query context. There is no public
  CatalogSession/workspace resolver or temporal query facade in this source tree.
- [TableDef](src/okto_grafx/domain/model/schema.py) carries schema version and
  relationship endpoint declarations, but no system/valid-time policy. Endpoint
  RecordIds are already stable across updates; that helps lineage, but does not
  retain deleted rows or historical schema by itself.
- [Commit identities](src/okto_grafx/domain/txn/commit_identity.py) are store-qualified;
  [journal lookup/history](src/okto_grafx/engine/commit_catalog_store.py) address
  commits. [CAP-1's consumer contract](docs/COMMIT_HISTORY.md) explicitly excludes
  graph time travel and does not make correlation metadata an idempotency key.
- [Logical transfer](docs/LOGICAL_TRANSFER.md) and its
  [implementation](src/okto_grafx/transfer.py) export current state only; import
  commits batches in a private fresh-UUID store and publishes the directory.
  Resumable import can recover a lost publication acknowledgement, but this is
  **not** a selected, one-transaction merge into an existing target catalog.
- [Heap reclamation](src/okto_grafx/engine/heap_store.py) has a physical snapshot
  horizon. Keeping its old MVCC versions or keeping WAL forever would not satisfy
  the separate durable temporal-history contract.

### Recommended finite implementation order

The order below is a proposal, not an additional authorization. CAP-2 and CAP-3
are siblings over CAP-1; CAP-3 does **not** depend on completing CAP-2. CAP-4 does
depend on CAP-3. Existing CAP-1 implementation satisfies the basic provenance
prerequisite, but copy receipts and temporal storage still need their own design.

| Order | Bounded checkpoint | Required exit evidence |
| --- | --- | --- |
| 1 / CAP-2 session | CatalogSession attach/detach/list/use/begin; unique ASCII aliases, reserved main, read-only default, immutable identity binding, explicit handle ownership and one catalog pinned at begin. | Two stores and concurrent participants; default changes cannot reroute active transactions; writes through a read-only attachment refuse before mutation; in-use detach refuses; attach failures release resources; close attempts all owned handles and reports cleanup failures. |
| 2 / CAP-2 workspace | Optional resolver outside the engine: explicit config, supplied root, bounded allowed-marker search, policy-permitted cwd; user/global opt-in. Canonical-path/root policy, explicit configuration and CLI status/list. | Independent processes resolve the same workspace; denied roots, alias/path/UUID ambiguity, Windows junction/symlink policy and resolution failures refuse. No directory creation on denied resolution; no hidden global store or paths derived from retrieved text. |
| 3 / CAP-2 copy | Bounded snapshot selection and checksummed package, node/edge identity mapping, explicit conflict policy, one target write transaction, durable receipt bound to source UUID/commit, target, selection hash, policy and idempotency key. | Endpoint closure, concurrent same-key attempts, input-mismatch refusal and crash before/after target COMMIT including lost acknowledgement. Data and receipt share the commit. Oversized atomic copies refuse at declared limits; do not silently batch visible target mutations. |
| 4 / CAP-3 durable history | Specify physical format/capability/upgrade/replay first; table opt-in, activation boundary, retained schema and lineage; publish node/edge current and history effects atomically. | Create/update/delete/recreate and DETACH DELETE; concurrent writers; cold reopen and repeated recovery at publication/crash cuts; old readers refuse unsupported required capabilities. No invented pre-activation history and no temporal overhead silently enabled for ordinary tables. |
| 5 / CAP-3 read and maintenance | Typed system-as-of by qualified commit or ordered timestamp, between/versions and historical traversal; entity/commit-range access paths, explicit scan diagnostics, temporal verify, manual bounded resumable retention, reader pins and backup/transfer horizon semantics. | Edge plus both matching-lineage endpoints visible at the selected time; clock ties/regression; wrong-store/future/unavailable coordinates; pinned pruning and vacuum independence; corruption distinct from expired history; reopen/export/import preserves or explicitly maps supported identities/horizons. |
| 6 / CAP-4 valid time | Explicit valid-time intervals independent of system time, interval typing, configurable overlap rule and indexed concurrent enforcement. | Half-open/unbounded interval boundaries, invalid ranges, retroactive corrections and competing overlapping writers. A backdated business correction must not rewrite what the database previously knew. |
| 7 / CAP-4 bitemporal and diff | Combine system and valid coordinates; bounded node/relationship/property diff with before/after, endpoints, commit provenance and explicit update versus delete/recreate; complete retention policies. | Two-coordinate fixtures, lineage/PK reuse, empty versus unavailable history, bounded pagination/work, indexed range plans and retention interaction. Specify diff meaning for both coordinates; never compare only current PK values. |
| 8 / CAP-4 consumption closure | Historical query syntax and CLI after typed contracts stabilize; finish configuration, API/error contracts, examples and operational guidance. | Typed API/CLI/query equivalence for supported operations, documented limitations, executable examples and one grouped regression of integrated capabilities. Earlier checkpoints also require their own docs and focused tests. |

### Decisions to settle within those checkpoints

These are finite contract decisions already implied by the specs, not permission
to expand into distributed transactions, a new agent product or arbitrary DDL:

- **Session authority:** permissions constrain operations through that session;
  a Python library is not an OS sandbox for unrelated code/handles. Define
  canonical-path and store-identity revalidation and ownership explicitly. One
  store per transaction does not mean one writer per application or a global lock.
- **Promotion:** specify fail/skip/explicit-merge behavior for PKs, schemas,
  relationship multiplicity and endpoint mappings. Distinguish partial selection
  from partial success. Require or explicitly activate CAP-1 before promising
  commit-backed provenance; never manufacture a commit from an arbitrary LSN.
  Receipt lookup must be indexed, not a scan of all commit metadata. Define
  receipt retention/idempotency horizon so pruning cannot turn a retry into a
  duplicate. Do not confuse artifact checksums with source authentication.
- **History activation/schema:** define the initial current-state baseline for
  pre-existing rows and history availability for non-temporal tables/endpoints.
  Either retain all required endpoint history or reject unsupported mixed-history
  traversals explicitly; never silently substitute today's endpoints. Retain the
  schema required to decode historical payloads without requiring arbitrary ALTER
  implementation as a prerequisite. Define disable/re-enable behavior before
  exposing any such operation.
- **Time/access paths:** store-local CommitId is ordering authority; raw wall clock
  is not. Timestamp resolution selects by monotonic ordered_at. Plan efficient
  entity history, time containment and commit-range diff paths rather than
  repeatedly scanning all versions. Full unfiltered output still costs at least
  the size of the requested result; do not promise universal O(1) queries.
- **Retention and transport:** keep manual CAP-3 pruning distinct from CAP-4
  none/keep-last/keep-for/keep-since policies. Publish available horizons and typed
  expiry errors. Version the logical-transfer contract before claiming history
  preservation; current-state-only v1 must remain explicit, not silently lose
  requested history. Writable copies get remapped identities, not cloned authority.
- **History search:** existing current-state FTS/vector indexes are not certified
  historical indexes. Declare which temporal operations are supported and refuse
  unsupported combinations; extending every search/analytics feature is not an
  implicit extra milestone in this assessment.

Quality closure preserves multi-reader/multiwriter access, both OCC checks,
fencing, durable WAL order and fail-closed recovery. Run focused feature/failure
checks per checkpoint and grouped regression per cohesive delivery. Measure
opt-out overhead, temporal write amplification, retained bytes and query p50/p99
on fixed bounded synthetic workloads; report the observed cost without a moving
performance percentage gate or using reserved Pulse specs.

Scope references: [CAP-2 spec](docs/specs/SPEC-GX-CAP-2.md),
[catalog ADR](docs/architecture/ADR-GX-004-ATTACHED-CATALOGS.md),
[CAP-3 spec](docs/specs/SPEC-GX-CAP-3.md),
[CAP-4 spec](docs/specs/SPEC-GX-CAP-4.md),
[temporal ADR](docs/architecture/ADR-GX-002-TEMPORAL.md) and
[complete preserved requirements](docs/archive/ROADMAP_SOURCES.md).
Federated search with per-store independent snapshot tokens and explicit source
failures remains a later catalog milestone; cross-catalog query grammar, physical
cross-store edges and distributed commit are not silently included here.

## Optional agent product

These are optional consumer packages, not required dependencies of `okto-grafx`.
No mandatory MCP daemon, prompt storage or automatic “winning” contradiction is
introduced. Identities and capabilities are injected by the host; `project`,
`user/global` and `shared` are conventions over explicit stores/catalogs.

| Source milestone | Status / scope |
| --- | --- |
| AGENT-0 | Planned: contracts, ADRs, package boundaries and ports |
| AGENT-1 | Planned: deterministic workspace resolver, scoped stores and promotion rules; generic catalog foundation reused |
| AGENT-2 | Planned: AgentProfile/AgentSession/AgentOperation, provenance and idempotency; generic commit identity reused |
| AGENT-3 | Planned: MemoryItem, Claim, Evidence, semantic relationships and knowledge API |
| AGENT-4 | Planned: optional local MCP stdio preview, tools/resources, errors and host configuration |
| AGENT-5 | Planned: governed namespaces/domain mutations, policies, schema-poisoning/scope-leakage protection |
| AGENT-6 | Planned: hybrid ContextPack with evidence, lineage and bounded graph expansion; CAP-5/6 reused |
| AGENT-7 | Planned: temporal memory lifecycle, contradictions, forgetting/compaction without lost lineage; temporal database foundations reused |
| AGENT-8 | Planned: hardening, conformance, observability, DX and release |

[GX-AGENT-0](docs/specs/SPEC-GX-AGENT-0.md) groups AGENT-0–4 plus required
AGENT-5 governance for preview; [GX-AGENT-1](docs/specs/SPEC-GX-AGENT-1.md)
groups integrated knowledge/hybrid/temporal hardening. Neither summary deletes the
full original requirements, security rules or exit gates preserved in the register.

## Completed foundations

Do not treat the initial audit's P0/P1 descriptions as current unresolved defects.
The execution record includes recovery/publication and bootstrap corrections,
read-only refusal, concurrent HNSW publication, index redo/freshness, checkpoint/WAL
policies, query/transaction quotas, OpenMetrics loopback policy, typed facade,
custom-adapter conformance, identity range leasing, two-slot control publication,
catalog-v2 identity indexes, explicit rehash/rebuild, vacuum v1, WAL compression,
streaming/bulk API and substantial 0.0.2–0.0.4 performance work.

These statements close only the documented implementation slice. They do not claim
universal RSS bounds, lock-free writes, hot backup, full Cypher or completion of
GX-CAP-1. Release/compatibility certification remains version/platform-specific.

The DELETE quota correction is retained: a DELETE intent has an empty tuple and
must not be re-encoded as a row just to compute quota; charge its fixed intent
entry. Keep its regression when evolving write accounting.

## Comparative feature gaps and minimum parity

Registered September 10, 2026 at the user's request, from
[Where Grafx is less complete](docs/FEATURE_COMPARISON.md#where-grafx-is-less-complete).
This records the complete gap set without replacing existing capability specs.
The broad CMP register remains **assessment/backlog**. Its selected MP-1–MP-8
minimum-parity slices were subsequently approved and implemented for 0.0.6,
as recorded below. This does not authorize Pulse changes, release or relaxation
of multi-read/write, WAL, consistency or isolation guarantees.

### Complete comparative gap register

| ID | Gap retained from the comparison | Existing owner / bounded direction | Effort and disposition |
| --- | --- | --- | --- |
| CMP-Q | Query-language breadth | Extend the closed query surface in explicit slices; broader joins/subqueries/procedures are not supplied by parser acceptance. Reuse CAP-7 for extension contracts. | Small-to-large by slice; no full Cypher compatibility claim. |
| CMP-T | Stored value/type breadth | Evaluate DATE, DECIMAL and nested-column needs against current typed values; coordinate schema evolution with CAP-10 and upgrade/refusal with CAP-11. | Medium-to-large; persistent types require format/recovery/interop decisions, not just Python wrappers. |
| CMP-D | Language drivers | First consider documented structured CLI consumption from another language. A real network protocol/client or native binding is a separate supported surface. | Small for recipes; medium-to-large for genuine drivers. |
| CMP-U | Graphical tooling | Start with detached read-only graph/schema inspection using bounded exports; later consider an optional local explorer. Pulse UI is not a bundled Grafx GUI. | Small-to-medium for a static viewer; larger for interactive administration. |
| CMP-F | Remote federation and attached catalogs | CAP-2 owns CatalogSession, resolution/permissions/workspaces and one-store writes. CAP-8 owns external scans/interoperability; bounded import is not federation. | Large overall; selected ingestion adapters can be smaller. |
| CMP-HA | Distributed availability | Retain replication/synchronization direction with explicit conflict policy; cluster/consensus/sharding still requires a separate architecture/scope decision. | Very large; deferred, not promoted into the current local-first delivery by this register. |
| CMP-S | Built-in authentication/authorization | Assess optional service-boundary auth and scoped read/write policy before any server exposure. Distinguish API permissions from direct filesystem access; never claim a wrapper secures untrusted local file owners. | Medium-to-large plus threat modeling. At-rest encryption retains its separate key-lifecycle prerequisite. |
| CMP-H | Full temporal history and bitemporal queries | CAP-3 owns retained historical schema/nodes/edges and time travel; CAP-4 adds valid time/bitemporal semantics and graph/version diff. | Large/very large; depends on CAP-1 and then CAP-3, not satisfied by commit metadata or current MVCC snapshots. |
| CMP-E | Ecosystem | Broader examples, language recipes and optional integrations; CAP-7/8/9 and AGENT remain the owners of real extension/interop/analytics/agent capabilities. | Small for bounded recipes; medium-to-large for maintained integrations. |
| CMP-O | Operational evidence and maturity | CAP-11 plus existing operational validation: supported-version/platform results, reproducible crash/reopen cases, mixed-workload measurements and explicitly bounded SLO evidence. Publication/test counts alone do not prove production maturity. | Small for an evidence map; sustained work for certification/SLOs. No endless percentage-based performance gate. |

These ten entries decompose all four bullets in the comparison; they do not add
ten competing execution programs. Missing catalog/temporal/security/HA features
remain missing until their own acceptance contracts are met.

### Relatively inexpensive minimum-parity candidates

Execution approved for the 0.0.6 development line (`feature/v0.0.6`).
MP-1–MP-8 are **delivered and validated locally**: 3419 passing grouped regression
tests, 4 Node tests (including the real CLI), strict TypeScript compilation and
Chrome viewer acceptance. See the [acceptance report](docs/reports/V006_MINIMUM_PARITY_ACCEPTANCE.md)
for the exact test scope and remaining platform/release boundaries. Their minimum
contracts are documented below; larger CMP gaps remain open. No persistent-format,
multi-reader/writer, WAL/durability or default connection changes were required.

| Delivered slice | Public consumption and focused evidence |
| --- | --- |
| MP-1 | [CLI schema/spaces/indexes/build capabilities](docs/CLI.md); `tests/cli/test_schema_inventory.py`, `tests/cli/test_discovery_search.py` |
| MP-2 | [CLI text/vector/hybrid search](docs/CLI.md); native search/filter/refusal acceptance in `tests/cli/test_discovery_search.py` |
| MP-3 | [JS/TS subprocess recipe](examples/cli-consumer/README.md); Node tests including real CLI, timeout/output/JSON/unsafe-integer failures; strict TypeScript compile |
| MP-4 | [Native scalar contracts](docs/QUERY_LANGUAGE.md#native-scalar-additions-006); `tests/query/test_native_scalars.py` |
| MP-5 | [Offline HTML picture](docs/HTML_SNAPSHOTS.md); `tests/api/test_html_snapshot.py` plus headless Chrome acceptance |
| MP-6 | [Bounded SQLite ingestion](docs/LOCAL_SQLITE_IMPORT.md); `tests/api/test_sqlite_import.py`, including whole-call rollback/source release |
| MP-7 | [Two-branch UNION ALL](docs/QUERY_LANGUAGE.md); `tests/query/test_union_all.py`, including shared budgets and old/new snapshots |
| MP-8 | [Topological order/cycle result](docs/GRAPH_PROJECTIONS.md#topological-ordering-006); `tests/api/test_topological_order.py` against an independent stdlib oracle |

Effort is a comparative engineering estimate, **including focused tests and docs**,
not a measured schedule. Low means a bounded wrapper/recipe over existing contracts;
low–medium adds UI or adapter behavior; medium changes a closed query/algorithm
surface. None provides complete parity with Ladybug or Neo4j.

| Order / ID | Minimum useful delivery | Comparison target and reuse | Effort / expected benefit | Explicit boundary and acceptance |
| --- | --- | --- | --- | --- |
| 1 / MP-1 | CLI schema/index/capability inventory as stable JSON | Minimum schema-inspection convenience of Ladybug Explorer/Neo4j tooling; reuse public catalog/index views and existing CLI envelopes. CMP-U/D/E. | Low / better discovery for developers and agents. | Read-only, deterministic schema, no index creation; cover empty stores, stale handles, budgets and CLI exit codes. Inventory APIs already exist; this is a new consumption path, not a new engine capability. |
| 2 / MP-2 | CLI text/vector/hybrid search subcommands | Expose existing search APIs without requiring a Python script. CAP-5/6, CMP-D/E. | Low / immediate search accessibility. | Typed vector/space/filter input, one owned read snapshot, bounded output and faithful error/partial-result status; no new ranking engine or embedding provider. |
| 3 / MP-3 | Tested JavaScript/TypeScript CLI-consumer recipe | Minimal non-Python consumption alongside competitors' drivers. Reuse existing `--json`, subprocess lifetime and error envelopes. CMP-D/E. | Low / onboarding with no server deployment. | No shell interpolation, bounded stdout/timeouts and process cleanup. Explicitly not a native driver, connection pool, remote service or long-lived transaction API. |
| 4 / MP-4 | Small native scalar-function batch: lower/upper/trim and abs | Reduce routine Cypher adaptation; reuse current scalar planning/evaluation and trusted-UDF experience. CMP-Q/CAP-7. | Low–medium / common query compatibility. | Freeze arity, NULL/Unicode/numeric/overflow semantics; test planner and execution agreement. No general casts, date arithmetic or language-complete claim. |
| 5 / MP-5 | Standalone read-only HTML graph/schema snapshot viewer | Minimum visualization comparable in purpose to Explorer/Browser; reuse bounded detached projections/exports. CMP-U. | Low–medium / useful inspection without Pulse. | Node/edge cap, visible truncation metadata, escaped labels, no executable property content or external CDN dependence. Static snapshot only: not live administration, authentication or editing. |
| 6 / MP-6 | Bounded SQLite-to-Grafx ingestion recipe/adapter | Minimum interoperability with a common local relational source; reuse SQLite reads and typed atomic import staging. CMP-F/E, CAP-8. | Low–medium / simpler local adoption. | Read-only source, explicit query/type/root/NULL policy, row/byte limits, rollback tests. Not SQL pushdown/federation, CDC or a cross-database transaction; do not hold unrelated source locks across long commits. |
| 7 / MP-7 | Two-branch read-only `UNION ALL` | Narrow query-language parity beyond current duplicate-eliminating UNION. CMP-Q. | Medium / fewer client-side merges. | Preserve duplicates, compatible types/arity, NULLs, budgets and documented branch/order/window rules. No chains, nesting, write branches or general set-operator expansion. |
| 8 / MP-8 | Bounded topological ordering with typed cycle detection | Small graph-analytics addition using detached projections, adjacency and existing work/cancellation budgets. CMP-E, CAP-9. | Medium / useful dependency-graph analysis without a full analytics subsystem. | Directed graph only; deterministic order, disconnected graphs, self-loops and cycles tested against an independent oracle. No persisted algorithm catalog, mutation or claim of GDS parity. |

Checkpoint order: MP-1–3 before MP-4–6; MP-7–8 remain
separate bounded query/algorithm slices. Prioritize actual consumer demand; a wrapper that
duplicates an already sufficient API need not be implemented just to gain a checkbox.

Not cheap substitutes: nested persistent types, phrase-position FTS, general
federation, native multi-language bindings, full temporal storage, arbitrary
serializability, production RBAC and HA/consensus. A demo or wrapper does not close
those contracts. All selected code changes require feature/adversarial tests,
grouped regression and updated public API/configuration/usage documentation.

### ACID is a clarification, not a missing engine feature

The first comparison used “ACID” for competitors but listed Grafx's mechanisms,
creating a misleading asymmetry. Corrected: Grafx implements single-store ACID
properties with snapshot/OCC isolation and documented host/filesystem boundaries.
[ACID scope and evidence](docs/OPERATIONS.md#acid-scope). This does not promise
general serializability or independent certification.

`CMP-O` includes a bounded public guarantee-to-test evidence map and any genuinely
uncovered anomaly/recovery cases discovered by review. **Do not create an
“implement ACID” backlog item or count ACID as a new performance feature.** A
future stronger isolation mode would need its own supported workload, anomaly
tests and concurrency/throughput analysis, not an automatic serialization of writers.

## Legacy requirement register

All former plans are consolidated **in full**, not replaced by lossy summaries:
[source archive](docs/archive/ROADMAP_SOURCES.md), with original paths, hashes,
line counts and explicit boundaries in the [manifest](docs/archive/ROADMAP_SOURCES_MANIFEST.json).
Use the archive for exact requirements/evidence, this roadmap for current status.

| Source / original identifiers | Current destination |
| --- | --- |
| Main evolution P0.1–P0.5 | Completed recovery/read-only/bootstrap/HNSW foundations; OPS-12 for current safety boundaries |
| P1.1–P1.4, P2.1–P2.2, P2.10 | Index/recovery work and historical evidence; OPS-3/12/13 and vector validation obligations |
| P1.5–P1.13, P2.3 | Remaining performance queue, OPS-4/5/6; completed budgets, indexes, WAL compression and vacuum slices |
| P1.14–P1.16, P2.4–P2.9 | Configuration/DX/adapter work; OPS-7/9/10; existing closed slices stay closed |
| Main §8.1–8.9 | OPS-1/2/3/8, CAP-1/7/8/9/10 and changefeed direction below |
| Main §9 / M-PULSE milestones | Consumer compatibility contracts, query/path specs and dated Pulse reports; Core stays agnostic |
| Main phases 0–3 | Completed foundations, operational backlog, finite performance residuals and database capability track |
| Complementary capabilities A–K / GX-CAP-0–11 | Database table above, six ADRs and individual specs; full scope/gates preserved |
| Agent-first AGENT-0–8 | Optional product table; full models, policies, tools/resources, namespace rules and acceptance preserved |
| Fable D-01–D-37 / NT-1 and final round batches | Historical disposition/evidence, not 37 new tasks; residuals/rejections above/below |
| Performance 0.0.2/0.0.3/0.0.4, Wave 0–3, OIX-0–3 | Completed slices and finite residual queue; detailed measurements remain in archive/reports |
| Round 7 / PUNCHLIST / RESUME | Frozen historical implementation/critic record; surviving unverified debts mapped to OPS-13 |

Future product directions retained from the initial plan: generic logical
changefeed with stable cursor/retention and acknowledged commit semantics; optional
synchronization/replication with explicit distributed-conflict policy; encryption
at rest only after a threat model and key lifecycle. These are **planned/deferred**,
not existing engine doors or dependencies added to 0.0.4.

## Deferred and rejected directions

- Group commit was rejected for a measured approximately 1.002× ceiling; do not
  reopen merely because it is a common database technique.
- Indexed-only DETACH DELETE was rejected because it stopped detecting pinned
  non-incident corruption cases. Faster success cases do not authorize that loss.
- Block-delta/physiological WAL and cross-shard commit protocols are not covered
  by current full-page zlib compression. They require a separate format/recovery
  decision and proof; no multi-read/write relaxation is authorized.
- Raising every buffer pool to 256 MiB, changing vector `auto` to NumPy,
  quantization, Bloom filters, wider codec rewrites and native/process-pool work
  are not automatic follow-ups. Follow measured workload, memory and semantics.
- Cluster/consensus/sharding/distributed transactions are outside the selected
  database-first scope. The untracked local `FABLE_SHARDING_GRAFX.md` was not
  modified, promoted to an accepted plan or used to silently expand this roadmap.
- Historical numeric gates, D5 ratios and endless marginal-gain rounds are
  superseded. Correctness failures and unexplained timeouts are not waived.

## Acceptance and maintenance

For each selected item, record scope, implementation commit, focused tests,
adversarial/corruption cases where relevant, grouped regressions, measured workload
and residual limitations. Never label a microbenchmark as UI speedup or an
observation as causal A/B evidence. Preserve errors, multiplicity, snapshots,
ordering, budgets, WAL barriers and cross-process behavior.

Documentation maintenance: update consumer reference + this roadmap status +
changelog for public changes; attach a dated report only when it adds evidence.
Do not create another `NEXT_STEPS`, `EVOLUTION_PLAN` or version-specific active
roadmap. Run `python tools/check_documentation.py` and the documentation consumer
tests. The archive is immutable provenance; new progress belongs here.
