# FP-7 bounded writing-procedure qualification

Date: 2026-09-12. Source: `feature/v0.0.6`, development worktree. No release,
commit, push, installation or production/Pulse-data operation is implied.

## Delivered contract

[Writing procedure specification](../specs/WRITING_PROCEDURES_V1.md) defines explicit
write mode/permissions, the restricted ProcedureWriter mutation door, exact syntax
limits, revocation, shared budgets and statement rollback. Read callbacks retain
their existing invocation contract. Registry-derived effects reach read-only
admission, eager phase planning and the public-result rollback guard.

Feature coverage includes native CREATE/MERGE/SET/DELETE; downstream entity
refresh; unit/tabular forms; named outputs; outer subqueries and updating UNION;
zero-row read-only refusal; callback-free EXPLAIN; cursor refusal; unselected
column failure; swallowed operation errors; successful/failing generator cleanup;
operation/output/native-write budgets; cross-thread and expired authority; public
result-conversion failure; earlier successful transaction statements; verification
and cold reopen with pure/NumPy codecs. Independent participants can read and
write while a callback holds private effects. Subprocesses exit abruptly inside
the callback, after its statement and after COMMIT: only the committed effect
survives native reopen.

## Receipts

Counts below are overlapping selections, **not** one full repository regression.

| Selection | Terminal result | Receipt |
| --- | --- | --- |
| Initial extension, numeric/unit/invocation, fixture/error regression | 178 passed; no failures/errors/skips; 12.690 s; exit 0 | `.grafx-tmp/fp7-writing-focused.xml` |
| Expanded query/transaction selection | 357 passed; no failures/errors/skips; 51.675 s; exit 0 | `.grafx-tmp/fp7-writing-regression-final.xml` |
| First broad public-contract/prepared-cache selection | 3,538 passed, two failures; 87.938 s; exit 1 | `.grafx-tmp/fp7-writing-qualified.xml` |
| Complete mutation feature/fault selection | 44 passed; no failures/errors/skips; 16.562 s; exit 0 | `.grafx-tmp/fp7-writing-feature-final.xml` |
| Corrected cache instrumentation and working-set checks | 8 passed; no failures/errors/skips; 1.105 s; exit 0 | `.grafx-tmp/fp7-writing-cache-corrective.xml` |
| Final combined feature, invocation/tabular, cache and public-contract acceptance | 3,543 passed; no failures/errors/skips; 84.869 s; exit 0 | `.grafx-tmp/fp7-writing-acceptance.xml` |
| Subsequent authority-argument hardening and affected extension regression | 139 passed; no failures/errors/skips; 23.094 s; exit 0 | `.grafx-tmp/fp7-writing-authority-final.xml` |
| Original FP-7 native TCK owner | 52 passed; zero failed/unexecuted selected cases; exit 0 | `.grafx-tmp/fp7-writing-native-20260912.json` |

The two broad-selection failures were stale instrumentation:
`tests/query/test_prepared_plan_cache.py` looked for `query_engine.analyze`, removed
by the earlier signature-resolution increment. Its assertions were not weakened:
the tests now instrument `planner.analyze`, where native analysis actually occurs,
and require one parse/plan, the three existing internal admission/lowering analysis
passes on first compilation, and **zero additional passes** on cache reuse. The
former probe observed only an external preliminary analysis, not these internal
passes. A follow-up diagnostic (`fp7-writing-contracts-final.xml`) also caught
that the new probe needed to forward `bindings=`; that failed receipt is retained.
No production cache
or planner relaxation was made to satisfy that test.

The final 139-test follow-up includes three additional direct-invocation cases:
a read procedure refuses every non-None forged `writer` argument **before** its
callback, not only exact ProcedureWriter instances. This tightens the newly added
keyword admission after the combined acceptance run; all 47 current writing
feature tests plus affected unit/numeric/tabular tests pass on the final source.

Earlier diagnostic receipts are retained: `fp7-writing-first.xml` has one expected
exception-class mismatch for a non-returning cursor refusal (implementation used
the established GrafxUnsupportedOperation, not a transaction-state error). The
first `fp7-writing-regression.xml` attempt had a nonexistent prepared-test filename
and did not constitute regression evidence. Neither is represented as passing.

SHA-256:

- Focused: `7f2ae2a300761878881c8e2f8988836a3af02f186d098abbc44c12019f783252`.
- Expanded: `46476ac0f8714f459a41b5a36ac95da0921b5802b447eb63f5dfad9a5776a9ac`.
- First broad selection (failed): `7bb9e1b3198404aa0f05d40157269184c5df005a89104780425af6b59bb31d7e`.
- Feature/fault selection: `7582e2bd22bb6d2694ef39a100d727c986b0ae8c2ed7ec7f25b630ac19379d91`.
- Corrective cache selection: `9ac6067297de6f9984273a71925625aea5ff3f66ad50df43b8b87617b36bd8d0`.
- Combined acceptance: `12c2c31ec5bd825daf1815c8b30cb75663279aa56839a5932f5f7088299da24f`.
- Final authority hardening: `266083ff52c39b28f5863b031610ac652fdc70a0da8f2db32fd9ec569d030957`.
- Follow-up instrumentation diagnostic (failed): `b931e2b75941c508c14ce4f9ed713dedde322826279b97296036062d01b3c33f`.
- Original TCK owner: `74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`.

The native owner result is identical to the prior numeric-signature receipt.
The pinned TCK revision, V2 ledger and V1 predecessor are unchanged. The remaining
3,845 inventory entries in this owner-filtered run were not selected, not waived.
Original TCK procedure cases do not test this new writing capability; the feature
and fault tests provide the additional evidence.

Generated API documentation, documentation links/anchors and all 39 configuration
fields, public signatures, 11 preserved source plans, Ruff over all changed/new
Python files and `git diff --check` also pass. README, roadmap, capability/query,
configuration, extension, compatibility, comparison and API guides reference the
new contract. The latest documented results are development evidence, not a
substitute for final full-profile/installed-Pulse qualification.

## Remaining scope

FP-7 is **partial**, not closed. This mutation-only capability does not yet return
read/query results, permit recursive registered CALL, create schema/models or
admit broader entity/temporal/collection signatures. FP-6 persisted DECIMAL and
parameterized collection types, pending FP-4 decisions and final FP-8/Pulse
qualification remain separate outstanding work in the unchanged parity plan.
This increment does not supersede the last full TCK inventory or certify current
global Pulse installation, power-loss behavior of hardware, or every storage
failure point. No multi-reader/writer, OCC, WAL or COMMIT guarantee was relaxed.
